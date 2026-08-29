# Sandbox security hardening — audit

A point-by-point audit of the standard sandbox-hardening checklist against what
KubeSandbox actually does, as of the post-Phase-9 pass. Written because "we do defence in
depth" is unfalsifiable; each row below names the code that implements it or says plainly
that it isn't implemented.

Status key: **Done** (was already in place), **Closed** (implemented in this pass),
**Partial**, **Not done**.

---

## Summary

| # | Control | Status | Where |
|---|---|---|---|
| 1 | Secure runtimes (gVisor / Kata / Firecracker) | Done | `runtime_class: gvisor`, `deploy/manifests/base/runtimeclass.yaml`, `deploy/azure/provision-nodepools.sh` |
| 2 | Rootless / non-root | Done | uid 10001 everywhere, control plane included |
| 3 | Dropped capabilities | Done | `CapDrop: ALL` / `capabilities.drop: ["ALL"]` |
| 4 | Read-only root filesystem | Done | `ReadonlyRootfs` / `readOnlyRootFilesystem`, tmpfs+emptyDir for writable paths |
| 5 | Resource limits (CPU, RAM, **swap**, disk) | **Closed** | swap and per-spec tmpfs sizing were the gaps |
| 6 | Fork-bomb protection (PID limit) | Partial | Docker enforced; Kubernetes is node-level, see below |
| 7 | Metadata endpoint blocking (169.254.169.254) | **Closed** | `_deny_metadata_egress`, both overlays |
| 8 | Network isolation (default-deny) | Done | `NetworkMode: none` / per-namespace default-deny NetworkPolicy |
| 9 | Egress control via proxy/firewall | Partial | Overlay-owned by design; no proxy shipped |
| 10 | Seccomp + AppArmor/SELinux | **Closed** | explicit and configurable on both backends |
| 11 | Ephemeral sessions | Done | ephemeral by default; recycle wipes `/workspace` |
| 12 | Least-privilege backend IAM | Done | `ClusterRole` with no `secrets`, no wildcards |
| 13 | Externalized logging / audit trail | **Closed** (audit) / Partial (shipping) | `AuditService`; log shipping is a deploy concern |

Five of the thirteen needed work. Details below, including what was found and what is
deliberately still open.

---

## 1. Secure runtimes — Done

`provisioner.runtime_class: gvisor` in `aks-prod.yaml` puts `runtimeClassName` on every
sandbox pod, and `deploy/manifests/base/runtimeclass.yaml` declares the RuntimeClass.
Neither does anything without a node pool carrying the shim, which is what
`deploy/azure/provision-nodepools.sh` provisions (`--workload-runtime
KataMshvVmIsolation`, on nested-virtualization-capable VM sizes).

The honest caveat, unchanged: **this has never been exercised on a real gVisor node.**
There has been no AKS cluster in any session of this project. The pod spec is correct and
the node pool script is correct as written; the combination is unverified.

Firecracker is not used and is not planned: Azure exposes microVM isolation through Pod
Sandboxing/Kata, and adding a second mechanism for the same guarantee would mean a second
node-pool shape to provision and reason about.

## 2. Rootless and non-root — Done

Sandboxes run as uid/gid 10001 with `runAsNonRoot: true`. The base image creates that user
(`components/base/Dockerfile`), so it isn't merely asserted at the pod level.

The control plane itself also runs non-root with the same hardening
(`deploy/helm/kubesandbox/values.yaml`'s `podSecurityContext`/`securityContext`) — it holds
the credentials that can create pods, so §6 Layer 1 applies to it at least as much as to a
sandbox.

Not rootless *Docker*: the `local` backend talks to a normal daemon socket. That is a local
development posture, and `local` is explicitly not a multi-tenant environment.

## 3. Dropped capabilities — Done

`CapDrop: ["ALL"]` (Docker) and `capabilities.drop: ["ALL"]` (Kubernetes), with
`allowPrivilegeEscalation: false` / `no-new-privileges` alongside.

One documented exception: a persistent-workspace container gets `CapAdd: ["CHOWN"]`,
because a fresh named volume's mount point is root-owned and the sandbox uid otherwise
cannot write into its own workspace. It sits unused in the bounding set — user code always
execs as the non-root sandbox user explicitly, never as root — so it is reachable only by
the provisioner's own one-shot ownership fix.

## 4. Read-only root filesystem — Done

`readOnlyRootFilesystem` with writable paths provided as tmpfs (Docker) or emptyDir
(Kubernetes), mounted `nosuid,nodev`. Those two flags are the load-bearing part: without
them a writable mount can hold a setuid binary or a device node, which turns "the user can
write files" into a privilege-escalation primitive.

`exec` **is** granted on those mounts, deliberately. Docker silently mounts an unqualified
tmpfs `noexec`, which breaks every compiled language (`go run` compiles into `$GOTMPDIR`
under `/tmp` and then execs it). It doesn't weaken containment: the sandboxed user can
already run arbitrary code through the interpreter or compiler itself.

## 5. Resource limits — Closed this pass

Already present: CPU (`NanoCpus` / `limits.cpu`), memory, and ephemeral storage
(Kubernetes `ResourceQuota`).

Two real gaps found and fixed:

- **Swap was unbounded.** Docker's default `MemorySwap` is *twice* the memory limit, so a
  sandbox declaring 512 MiB could actually touch 1 GiB before the OOM killer intervened —
  with the extra half landing on host swap, where it is both unaccounted for and orders of
  magnitude slower. Now pinned to `Memory`, which is Docker's documented way to say "no
  swap at all". Applied to sidecars too.
- **tmpfs size ignored the component's declaration.** Every writable path got a hardcoded
  `size=1g` regardless of the `ephemeralStorageMB` the component asked for. Now derived
  from the spec (`_tmpfs_size_mb`).

Note the interaction that makes the tmpfs limit safe rather than cosmetic: tmpfs pages are
charged to the container's memory cgroup, so a tmpfs bomb OOM-kills the sandbox instead of
filling the host disk — but only because swap is now disabled. The two fixes reinforce each
other.

Kubernetes disk limits come from `ResourceQuota`'s `limits.ephemeral-storage`, which the
kubelet enforces by eviction rather than by a hard write failure.

## 6. Fork-bomb protection — Partial

**Docker: enforced.** `PidsLimit` is set from the component's `access.limits.processes`
(128 by default), on both the main container and every sidecar.

**Kubernetes: not per-pod, and cannot be.** Kubernetes has no pod-level PID limit field —
it's a kubelet flag (`--pod-max-pids`, or `podPidsLimit` in the kubelet config), set per
node by whoever provisions the cluster. So on `aks-prod` this control exists only if the
node pool sets it.

This is a genuine gap between the two backends and it is not closeable from application
code. It belongs in the node-pool provisioning story; `deploy/azure/README.md` should
carry it as a required kubelet setting. Until then, a fork bomb on `aks-prod` is bounded by
the pod's memory limit rather than by a PID ceiling — which does stop it, just less
cleanly and with more collateral scheduling noise.

## 7. Cloud metadata endpoint blocking — Closed this pass

The highest-value fix in this pass. On a node with a managed identity, IMDS
(`169.254.169.254`) hands out IAM credentials to anything that can reach it, so a sandbox
that can talk to it can escalate straight out of the platform's trust boundary.

It was already unreachable — Docker sandboxes have `NetworkMode: none` and Kubernetes
sandboxes get a default-deny NetworkPolicy — but only *incidentally*, and that is the
problem. **NetworkPolicy is additive and has no deny primitive:** a pod's effective egress
is the union of every rule selecting it. The moment anyone adds the obvious rule — "allow
`0.0.0.0/0` on 443 so `pip` works" — IMDS reopens, and nothing about that rule looks wrong.

Fixed by making the exclusion survive that union. `_deny_metadata_egress` creates a second
policy per sandbox namespace that allows `0.0.0.0/0` **except** `169.254.0.0/16`; the same
pattern is appended to both deploy overlays. With no allowlist present the default-deny
still permits nothing; the guard only becomes load-bearing once someone opens egress,
which is exactly when it's needed.

The whole `/16`, not the single address: Azure answers on `169.254.169.254`, AWS also uses
`169.254.170.2` for ECS task credentials, and GCP resolves `metadata.google.internal` into
the same range. Nothing legitimate lives in link-local space.

## 8. Network isolation — Done

Docker: `NetworkMode: none` — no connectivity at all, and DB sidecars join the main
container's network namespace so they're reachable only over localhost.

Kubernetes: a default-deny `NetworkPolicy` (both directions, empty rule lists) per sandbox
namespace. Intra-pod traffic is unaffected, which is what lets a sidecar work while nothing
external can reach it.

Sandbox-to-sandbox traffic is blocked by construction: each sandbox is its own namespace
with its own default-deny.

## 9. Egress control via proxy/firewall — Partial, by design

Doc §12 makes the egress allowlist "entirely a deployment-overlay concern", and components
only ever declare *intent* (`access.network.egress: intent-only`). The control plane never
grants egress at runtime. That is the right split — the network posture belongs to whoever
owns the VNet, not to a request handler.

What is **not** shipped: an actual package-mirror proxy or egress firewall. `local` has no
mirror, and `aks-prod`'s overlay has a `REPLACE_ME` CIDR for private endpoints. So a
deployment that needs `pip install` to work has to stand up its own mirror and add the
rule — and if it adds that rule carelessly, item 7's guard is what stops it from also
opening IMDS.

## 10. Seccomp and AppArmor — Closed this pass

**Was:** Kubernetes set `seccompProfile: RuntimeDefault`; Docker relied on the daemon's
implicit defaults and Kubernetes set no AppArmor profile at all.

**Now:**

- Docker names both explicitly — `seccomp=builtin`, `apparmor=docker-default` — because
  relying on a default means a daemon started with `--seccomp-profile=unconfined` runs an
  unfiltered sandbox with nothing in the logs to say so. Naming them makes an unsupported
  host fail at create time instead.
- Both are **configurable** (`provisioner.seccomp_profile`, `provisioner.apparmor_profile`),
  and this matters: naming an AppArmor profile on a host without AppArmor fails *every*
  container create, and RHEL-family hosts use SELinux instead (applied by the daemon's own
  labelling). Setting `apparmor_profile: null` is the supported way to run there. Hardcoding
  the profile would have made the platform undeployable on those hosts.
- Kubernetes now sets `securityContext.appArmorProfile: RuntimeDefault` (a first-class
  field since 1.30) on both the sandbox pod and the workspace utility pod. `RuntimeDefault`
  rather than a named profile so it resolves to whatever the runtime ships — which is what
  lets it work on a gVisor/Kata node pool that may not carry a custom profile.

No custom seccomp profile is shipped. A tighter one than Docker's builtin (~44 blocked
syscalls including `mount`, `ptrace`, `kexec_load`, `bpf`) would have to be distributed to
and read from the host filesystem, which is a deployment concern rather than something
application code can guarantee. The config field accepts a path for anyone who does.

## 11. Ephemeral sessions — Done

Ephemeral is the default: `POST /v1/execute` acquires, runs, and tears down within one
request. A pooled sandbox that is reused has `/workspace` wiped and a health check re-run
before it returns to the pool, and any sandbox whose run timed out or hit an output cap is
destroyed rather than recycled — a sandbox that hit a resource limit isn't trusted to be
clean.

Persistent workspaces are the deliberate exception (doc §10.2) and are opt-in per
environment, per user, and per request.

## 12. Least-privilege backend IAM — Done

The control plane's `ClusterRole` (`deploy/helm/kubesandbox/templates/rbac.yaml`, mirrored
in the Kustomize base) grants namespaces, pods, `pods/exec`, `pods/log`, quotas,
limitranges, PVCs, and networkpolicies — and nothing else. No `secrets`, no `deployments`,
no node writes, no wildcard verb anywhere. A test asserts the absence of `"*"` and of
`secrets`, so a future edit can't quietly widen it.

Azure-side, the workload identity needs Key Vault `get`, Blob Data Contributor, and
optionally `AcrPush` — enumerated in `deploy/azure/README.md` rather than left to
"whatever works".

## 13. Externalized logging and audit trail — Audit closed, shipping partial

**Closed:** `audit_logs` had existed since Phase 0 with **nothing writing to it**, which
made doc §6 Layer 5's "full audit log of every command/run (who, what, when, exit code)"
plainly false. `AuditService` now records sandbox create/destroy/run/attach, auth
login/failure, API-key create/revoke, admin role/billing/credit/quota mutations, and every
quota, billing, and rate-limit denial. `GET /v1/admin/audit-logs` reads it back.

The design choice worth flagging: entries **join the caller's transaction** rather than
being best-effort. An action that rolls back leaves no entry claiming it happened, and an
action that commits cannot commit without its entry. An audit trail with silent holes is
worse than none, because it looks complete. Only events with no surrounding transaction (a
login, a rejected credential) use a separate best-effort write.

What is **deliberately not** recorded: no code, stdin, stdout, or credentials — identifiers,
counts, and outcomes only. An audit log that accumulates user source becomes both a
compliance liability and the most attractive table in the database. A test asserts submitted
code never appears in an entry.

**Partial — log shipping.** Logs are structured JSON on stdout (`structlog`), which is
collectable by any cluster log agent, and the API pods carry Prometheus scrape annotations.
But **no log shipper is configured by this repo**, and that matters for the specific
threat this control addresses: anyone who can reach the database can edit `audit_logs`, so
the tamper-resistant record has to be the shipped copy. The reconciler prunes entries older
than `audit.retention_days` (a year by default) on that assumption. If shipping isn't
configured, lengthen the retention or set it to `null`.

---

## Controls beyond the list

Worth naming, since they're part of the same posture:

- **Quotas** (`QuotaService`) — concurrency, cpu/memory, and monthly-minute ceilings per
  tenant. Distinct from billing: billing asks whether a tenant can *afford* something,
  quotas whether it should be *allowed* that much at once. Without them, a tenant with
  billing disabled (the default) had nothing at all bounding its concurrency.
- **Rate limiting** (`RateLimiter`) — Redis-backed sliding window per identity, keyed on
  the user (or tenant for a service account) rather than per API key, since any
  authenticated caller can mint more keys.
- **Role grants cannot come from a token.** `AuthService` reads no `roles`/`wids`/`groups`
  claim; the only paths to `admin` are an existing admin's `PATCH .../role`, the operator
  config allowlist, or the seed CLI. A test asserts a token claiming every admin-shaped
  role still produces a plain user.
- **Tenant isolation reported as 404, never 403**, so ids can't be probed for existence.

---

## Still open

Ranked by how much they'd matter in production:

1. **Kubernetes per-pod PID limits** (item 6) — needs a kubelet setting on the node pool.
   Not closeable from application code.
2. **No egress proxy / package mirror** (item 9) — a deployment needing `pip install` must
   provide one, and must add the rule carefully.
3. **No log shipping configured** (item 13) — the audit table is queryable but not
   tamper-resistant on its own.
4. **gVisor never verified on a real node** (item 1) — the pod spec and node-pool script
   are written but have never run against AKS.
5. **Rootless Docker not used** in `local` — acceptable for a single-developer environment,
   not for anything multi-tenant.
6. **Quota accounting uses per-weight-class budgets**, not each sandbox's resolved limits.
   Conservative (errs toward refusing), but approximate; the exact fix is persisting
   resolved cpu/memory on the `sandboxes` row.
