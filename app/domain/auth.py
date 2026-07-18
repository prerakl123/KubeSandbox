"""Authenticated-caller identity, shared by the API layer (app/api/deps.py) and any
service that needs to reason about who's asking (e.g. EntitlementService) without
importing the API layer itself and risking a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str | None
    role: str
