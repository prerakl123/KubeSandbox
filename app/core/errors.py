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


class BillingAuthorizationError(QuotaExceededError):
    """Sandbox creation blocked: insufficient credit balance (credit mode) or the
    configured spend cap would be exceeded (PAYG mode) — doc §13. A QuotaExceededError
    subclass (doc §11 groups credit balance/spend cap with quotas), so it inherits the
    existing 429 mapping in app/main.py with no new exception handler needed."""


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


class ConfigurationError(KubeSandboxError):
    """A setting is internally inconsistent or selects an unimplemented backend —
    raised at startup/config-validation time, never mid-request (doc §9's own
    "stubs fail loudly and immediately" contract)."""


class SecretNotFoundError(KubeSandboxError):
    """A SecretsProvider was asked for a secret name that doesn't exist in the
    configured backend (doc §9). A domain error rather than a bare KeyError so a
    caller can distinguish "not configured" from a real transport failure, which
    propagates as whatever the backend client itself raised."""
