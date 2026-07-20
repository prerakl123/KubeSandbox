"""Shared exception types used across services, provisioners, and API error handling."""

from __future__ import annotations


class KubeSandboxError(Exception):
    """Base class for all domain errors raised by the control plane."""


class ManifestValidationError(KubeSandboxError):
    """A Component/SandboxTemplate manifest failed schema or semantic validation."""


class ComponentNotFoundError(KubeSandboxError):
    """Referenced component/version does not exist in the registry."""


class TemplateNotFoundError(KubeSandboxError):
    """Referenced SandboxTemplate does not exist in the registry."""


class EntitlementError(KubeSandboxError):
    """Caller is not entitled to see/use the requested component or template."""


class QuotaExceededError(KubeSandboxError):
    """Tenant/user quota (concurrency, credits, storage) would be exceeded."""


class ProvisionerError(KubeSandboxError):
    """The underlying provisioner (Docker/Kubernetes) failed to satisfy a request."""


class ExecutionTimeoutError(KubeSandboxError):
    """A batch run exceeded its wall-clock limit."""


class SandboxNotFoundError(KubeSandboxError):
    """Referenced sandbox id does not exist or has already been torn down."""


class BuildError(KubeSandboxError):
    """A BuildStrategy failed to produce a golden image/artifact (doc §8, Phase 6)."""


class BuildNotFoundError(KubeSandboxError):
    """Referenced build id does not exist."""
