"""Prometheus metrics (doc §14, §20 Phase 9).

Every metric doc §14 names by name is here, plus the handful the existing code paths
made cheap to add. Two deliberate deviations from the doc's literal metric list, both
because the Prometheus data model wants them this way:

* **`pool_hit_rate`** is exposed as a pair of counters
  (`kubesandbox_pool_claims_total{result="hit"|"miss"}`), not a pre-computed ratio
  gauge. A ratio computed in-process is a lie the moment the process restarts or a
  second replica exists; the rate is derived at query time instead:

      sum(rate(kubesandbox_pool_claims_total{result="hit"}[5m]))
        / sum(rate(kubesandbox_pool_claims_total[5m]))

* **`sandboxes_active`** is a per-replica, in-process gauge of sandboxes *this* process
  currently holds in flight — incremented on acquire, decremented on release/destroy.
  It is deliberately NOT the cluster-wide count of live sandbox rows: that lives in
  Postgres, outlives any control-plane process, and would need either a DB-querying
  collector on every scrape or a push from the reconciler (a separate process with no
  HTTP server of its own, doc §4.1). `sum(kubesandbox_sandboxes_active)` across
  replicas is the useful aggregate, and it resets to 0 on a replica restart — which is
  correct for "in flight in this process" and wrong for "live in the data plane".
  Reconciler-reaped sandboxes are the concrete case where the two genuinely differ.

Collection is in-process against `prometheus_client`'s default registry, i.e. one
registry per pod. That's only correct with a single uvicorn worker per pod — the Helm
chart in `deploy/helm/kubesandbox` runs exactly one and scales via HPA replicas instead
(see its `values.yaml`), rather than multi-worker pods, which would need
`PROMETHEUS_MULTIPROC_DIR` and a shared-mmap registry.

The metric objects are always live and always updated: an in-process counter increment
is a dictionary lookup plus an add, so gating the *recording* on
`observability.metrics_enabled` would buy nothing and add a branch to every call site.
That flag gates whether `GET /metrics` is mounted at all (`app/main.py`).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_NS = "kubesandbox"

# Latency buckets: sandbox provisioning is expected in the 0.1–10s range (image already
# pulled, doc §8's whole golden-image premise), with a long tail when a node has to
# scale up — hence buckets out to 120s rather than prometheus_client's default 10s cap.
_PROVISION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)

# Runs are wall-clock-capped at `limits.default_wall_clock_seconds` (60s by default),
# so the top bucket sits just past that cap to make "hit the cap" visible as its own
# bucket rather than being lost in +Inf.
_RUN_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 90.0)

# Builds (doc §8) are minutes-scale: a Kaniko/dockerfile build or a multi-step pipeline
# is nothing like a run.
_BUILD_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)


sandboxes_active = Gauge(
    f"{_NS}_sandboxes_active",
    "Sandboxes this control-plane process currently holds in flight (see module docstring).",
    ["backend", "weight_class"],
)

provision_latency_seconds = Histogram(
    f"{_NS}_provision_latency_seconds",
    "Time to obtain a usable sandbox, by whether it came from the warm pool or was created fresh.",
    ["backend", "weight_class", "source"],
    buckets=_PROVISION_BUCKETS,
)

provision_failures_total = Counter(
    f"{_NS}_provision_failures_total",
    "Sandbox acquisitions that raised instead of returning a handle.",
    ["backend", "weight_class"],
)

run_duration_seconds = Histogram(
    f"{_NS}_run_duration_seconds",
    "Batch run wall-clock duration as measured by the provisioner (doc §5.1).",
    ["language", "outcome"],
    buckets=_RUN_BUCKETS,
)

runs_total = Counter(
    f"{_NS}_runs_total",
    "Batch runs completed, by outcome: ok (exit 0) | error (non-zero exit) | timeout | truncated.",
    ["language", "outcome"],
)

build_duration_seconds = Histogram(
    f"{_NS}_build_duration_seconds",
    "Golden-image build duration (doc §8), by source strategy and outcome.",
    ["strategy", "outcome"],
    buckets=_BUILD_BUCKETS,
)

pool_claims_total = Counter(
    f"{_NS}_pool_claims_total",
    'Warm-pool claim attempts, by result ("hit" | "miss") — divide to get doc §14\'s pool_hit_rate.',
    ["weight_class", "result"],
)

attach_sessions_active = Gauge(
    f"{_NS}_attach_sessions_active",
    "Interactive PTY attach sessions currently held open by this process (doc §5.2).",
)

usage_cost_total = Counter(
    f"{_NS}_usage_cost_total",
    "Cumulative priced usage recorded by BillingService (doc §13), in the pricing rule's currency.",
    ["resource_type", "mode"],
)

credit_balance = Gauge(
    f"{_NS}_credit_balance",
    "Last-observed credit wallet balance per tenant (doc §13). Cardinality is per tenant — "
    "fine for the tens-to-hundreds of tenants this is sized for, not for a per-user label.",
    ["tenant_id"],
)

billing_denials_total = Counter(
    f"{_NS}_billing_denials_total",
    "Sandbox creations blocked by billing pre-authorization (doc §13), by billing mode.",
    ["mode"],
)


def run_outcome(*, exit_code: int, timed_out: bool, truncated: bool) -> str:
    """Single source of truth for the `outcome` label, so `run_duration_seconds` and
    `runs_total` can never disagree about how one run is classified. Ordered
    most-severe-first: a truncated *and* non-zero run is reported as truncated, since
    that's the sandbox-level fault, not the user program's exit code."""
    if timed_out:
        return "timeout"
    if truncated:
        return "truncated"
    return "ok" if exit_code == 0 else "error"
