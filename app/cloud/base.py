"""Shared plumbing for the doc §9 CloudProvider bundle.

Two things live here rather than being repeated across `secrets.py`/`storage.py`/
`registry.py`:

1. `ComingSoonProvider` — the common base for every AWS/GCP stub. Doc §9 is explicit
   that these must "fail loudly and immediately (raise, not silent no-op) so a
   misconfiguration is caught at startup/config-validation time, not mid-request".
   Inheriting a marker base (instead of each stub independently raising a bare
   `NotImplementedError`) is what gives the startup check below a real type-level seam
   to test for, rather than string-matching an exception message.
2. `assert_cloud_provider_usable()` — the startup half of that contract. Constructing a
   stub is deliberately *not* itself an error (the factories in `app/core/bootstrap.py`
   have to be able to hand one back for the check to inspect), so the failure is raised
   here, once, with the concern name and the configured provider id in the message.
"""

from __future__ import annotations

from typing import ClassVar, NoReturn

from app.core.errors import ConfigurationError


class ComingSoonProvider:
    """Marker base for a doc §9 "Coming Soon" cloud stub.

    Subclasses set `coming_soon` to the doc's own literal wording for their concern
    ("S3/GCS support coming soon", etc.) and route every interface method through
    `_raise()`, so a stub that somehow slipped past the startup check still fails at
    the first real call rather than silently returning `None`.
    """

    coming_soon: ClassVar[str] = "support coming soon"

    def _raise(self) -> NoReturn:
        raise NotImplementedError(self.coming_soon)


def assert_cloud_provider_usable(provider: object, *, concern: str, configured: str) -> None:
    """Fail fast (doc §9) when `<concern>.provider` selects an unimplemented cloud.

    Called from `app/core/bootstrap.py::validate_cloud_providers()` at startup for
    every concern at once, so a deployment pointed at AWS/GCP dies during lifespan
    with one clear message naming *which* setting is wrong — not mid-request, on the
    first build or workspace archive that happens to touch it.
    """
    if isinstance(provider, ComingSoonProvider):
        raise ConfigurationError(
            f"{concern}.provider = {configured!r} is not implemented: {provider.coming_soon}"
        )
