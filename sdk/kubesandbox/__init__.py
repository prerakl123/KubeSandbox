"""KubeSandbox client SDK (doc §17: "a thin client SDK (`sdk/`, Python first) wraps
these for the workflow-builder's code block").

```python
from kubesandbox import KubeSandboxClient

with KubeSandboxClient("https://kubesandbox.internal", api_key="...") as ks:
    result = ks.execute(language="python", code="print('hello')")
    assert result.ok
```

`attach` (interactive PTY, doc §5.2) is deliberately not re-exported here: it needs the
optional `websockets` extra, and importing it eagerly would make the base install fail
for the batch-only workflow-builder that is this SDK's primary consumer. Import it
explicitly instead: `from kubesandbox.attach import attach`.
"""

from __future__ import annotations

from .async_client import AsyncKubeSandboxClient
from .client import KubeSandboxClient
from .errors import (
    AuthenticationError,
    BadRequestError,
    ConflictError,
    KubeSandboxAPIError,
    KubeSandboxError,
    NotFoundError,
    PermissionDeniedError,
    ProvisionerError,
    QuotaExceededError,
    ServiceUnavailableError,
)
from .models import (
    ApiKeySummary,
    BatchRunResult,
    BillingAccount,
    Build,
    Component,
    CreatedApiKey,
    CreditRequest,
    Features,
    FileEntry,
    Identity,
    Page,
    Principal,
    RunRecord,
    Sandbox,
    Template,
    UsageRecord,
    Workspace,
    WorkspaceStatus,
)

__version__ = "0.1.0"

__all__ = [
    "ApiKeySummary",
    "AsyncKubeSandboxClient",
    "AuthenticationError",
    "BadRequestError",
    "BatchRunResult",
    "BillingAccount",
    "Build",
    "Component",
    "ConflictError",
    "CreatedApiKey",
    "CreditRequest",
    "Features",
    "FileEntry",
    "Identity",
    "KubeSandboxAPIError",
    "KubeSandboxClient",
    "KubeSandboxError",
    "NotFoundError",
    "Page",
    "PermissionDeniedError",
    "Principal",
    "ProvisionerError",
    "QuotaExceededError",
    "RunRecord",
    "Sandbox",
    "ServiceUnavailableError",
    "Template",
    "UsageRecord",
    "Workspace",
    "WorkspaceStatus",
    "__version__",
]
