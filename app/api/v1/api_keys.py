"""API-key issuance and revocation (doc §11's "service accounts with API keys (hashed at
rest)") — a cross-cutting gap until Phase 9: the `api_keys` table and the hashed-lookup
auth path both worked, but nothing could create or revoke a key except a direct DB
insert.

This is also the bridge between the two consumers in doc §1. A human signs into the UI
with OIDC, then mints a key *here* for the workflow-builder to use — so the programmatic
consumer never needs an interactive login, and the key it holds can be revoked without
touching the person's identity.

**The plaintext key is returned exactly once, at creation, and is unrecoverable
afterwards.** Only its SHA-256 hash is stored (doc §11), so there is nothing to return
later even for an admin — losing a key means revoking it and minting another. Listing
shows a non-reversible prefix so a caller can tell their keys apart in a UI table
without the key itself being readable from the list.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_audit_service, get_current_principal
from app.api.pagination import Page, PageParamsDep, paginate
from app.api.ratelimit_deps import rate_limit_mutation
from app.persistence.db import get_session
from app.persistence.models import ApiKey
from app.services import audit_service as audit
from app.services.audit_service import AuditService

router = APIRouter(prefix="/v1/api-keys", tags=["API keys"])

_KEY_PREFIX = "ks_"
_KEY_ENTROPY_BYTES = 32
"""256 bits from `secrets.token_urlsafe` — this is a bearer credential with no second
factor and no expiry, so it has to be infeasible to guess outright."""

_DISPLAY_PREFIX_LENGTH = 12
"""How much of the plaintext key is stored in the clear for display. Long enough to be
recognizable in a list (`ks_7Fq2...`), far too short to narrow a brute force against
256 bits of entropy."""


def _hash_api_key(raw_key: str) -> str:
    # Must stay identical to `app/api/deps.py::_hash_api_key` — the lookup on the auth
    # path hashes the presented key the same way. A plain SHA-256 rather than a
    # password KDF on purpose: this is a 256-bit random secret, not a human-chosen
    # password, so there is no dictionary to slow down, and the auth path runs on
    # every request.
    return hashlib.sha256(raw_key.encode()).hexdigest()


class CreateApiKeyRequest(BaseModel):
    label: str = Field(
        max_length=255,
        description="Human-readable name shown in listings, e.g. 'workflow-builder prod'. "
        "The only way to tell keys apart later, so it's required rather than optional.",
    )


class ApiKeySummary(BaseModel):
    id: str
    label: str | None
    prefix: str | None = Field(
        description="First few characters of the key, in the clear, so a caller can "
        "match a listing row against the key they saved. Null for keys created before "
        "this column existed (by direct DB insert)."
    )
    revoked: bool
    created_at: datetime
    last_used_at: datetime | None = Field(
        description="Last time this key successfully authenticated a request, or null if "
        "it never has. The one signal that makes 'is this key still in use?' answerable "
        "before revoking it."
    )


class CreateApiKeyResponse(ApiKeySummary):
    api_key: str = Field(
        description="The full key, in plaintext. Shown ONLY in this response — only its "
        "hash is stored, so it cannot be retrieved again. Store it now."
    )


def _summarize(row: ApiKey) -> ApiKeySummary:
    return ApiKeySummary(
        id=row.id,
        label=row.label,
        prefix=row.prefix,
        revoked=row.revoked,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


@router.post(
    "",
    response_model=CreateApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key for this tenant",
    description=(
        "Creates a service-account key scoped to the caller's tenant (doc §11). The "
        "plaintext key is in this response and nowhere else — only its SHA-256 hash is "
        "persisted, so it cannot be retrieved later. A key authenticates as the tenant "
        "with role `service`, never as the person who minted it, so it can never be "
        "used to reach admin endpoints."
    ),
    dependencies=[Depends(rate_limit_mutation)],
)
async def create_api_key(
    body: CreateApiKeyRequest,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    audit_svc: AuditService = Depends(get_audit_service),
) -> CreateApiKeyResponse:
    raw_key = f"{_KEY_PREFIX}{secrets.token_urlsafe(_KEY_ENTROPY_BYTES)}"
    row = ApiKey(
        tenant_id=principal.tenant_id,
        key_hash=_hash_api_key(raw_key),
        label=body.label,
        prefix=raw_key[:_DISPLAY_PREFIX_LENGTH],
        # None when the caller is itself a service account (an API key minting another
        # key) — there's no person to attribute it to in that case.
        created_by_user_id=principal.user_id,
    )
    session.add(row)
    await session.flush()  # populates row.id for the audit entry below
    # Minting a long-lived credential is one of the most security-relevant actions
    # available to a non-admin, and the `prefix` is what ties an audit entry to a key
    # later — the key itself is never recorded anywhere.
    audit_svc.record(
        session,
        action=audit.APIKEY_CREATE,
        principal=principal,
        target=row.id,
        detail={"label": row.label, "prefix": row.prefix},
    )
    await session.commit()
    await session.refresh(row)
    summary = _summarize(row)
    return CreateApiKeyResponse(**summary.model_dump(), api_key=raw_key)


@router.get(
    "",
    response_model=Page[ApiKeySummary],
    summary="List this tenant's API keys",
    description=(
        "Newest first. Includes revoked keys — a UI needs to show that a key was "
        "revoked rather than have it silently vanish, which looks identical to it "
        "never having existed. Never returns key material."
    ),
)
async def list_api_keys(
    params: PageParamsDep,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Page[ApiKeySummary]:
    statement = (
        select(ApiKey)
        .where(ApiKey.tenant_id == principal.tenant_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
    )
    rows, total = await paginate(session, statement, params)
    return Page[ApiKeySummary](
        items=[_summarize(r) for r in rows], total=total, limit=params.limit, offset=params.offset
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    description=(
        "Marks the key revoked; the auth path rejects it from the next request onward. "
        "Idempotent — revoking an already-revoked key succeeds. The row is kept rather "
        "than deleted so the audit trail of what existed survives."
    ),
    responses={404: {"description": "No such key in the caller's tenant."}},
)
async def revoke_api_key(
    key_id: str = Path(description="Key id, as returned by POST /v1/api-keys."),
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
    audit_svc: AuditService = Depends(get_audit_service),
) -> None:
    row = await session.get(ApiKey, key_id)
    # A key belonging to another tenant reports 404, not 403 — the same rule
    # `SandboxService.get_sandbox` follows, so a caller can't probe for valid key ids.
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such API key")
    if not row.revoked:
        row.revoked = True
        audit_svc.record(
            session,
            action=audit.APIKEY_REVOKE,
            principal=principal,
            target=row.id,
            detail={"label": row.label, "prefix": row.prefix},
        )
        await session.commit()
