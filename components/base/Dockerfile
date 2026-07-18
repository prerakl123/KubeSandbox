# syntax=docker/dockerfile:1
#
# Golden image for the `base` component (docs/ARCHITECTURE_AND_PLAN.md §3.4, §8).
# This is the shared foundation a SandboxTemplate.spec.base.ref points to, and — until
# BuildManager (Phase 6) can merge per-component Dockerfile fragments into one golden
# image — the same tag doubles as the image for any "mainTool"-kind component whose
# capability is already baked in here (see components/languages/bash,
# components/tools/git). Built once at publish time, never inside a request path; for
# local dev this is a one-time manual build, same pattern as the python component:
#
#   docker build -t kubesandbox/base:1.0 components/base

FROM debian:12-slim

# No root, no admin — ever (doc §6 Layer 1).
RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash coreutils git ca-certificates dtach \
    && rm -rf /var/lib/apt/lists/*
# dtach: interactive PTY attach (doc §5.2, Phase 4) runs `dtach -A ...` so a reattach
# after disconnect resumes the same shell (cwd/env/jobs), not a fresh one. Not tmux:
# confirmed live that tmux >=3.4's per-pane cgroup-via-systemd-dbus move is fatal in
# this non-systemd container (no dbus session bus) — dtach has no such dependency.

RUN mkdir -p /workspace && chown -R 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT []
CMD ["bash"]
