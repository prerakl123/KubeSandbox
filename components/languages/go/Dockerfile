# syntax=docker/dockerfile:1
#
# Golden image for the `go` component (docs/ARCHITECTURE_AND_PLAN.md §8). Built once at
# publish time, never inside a request path. For local dev (no BuildManager/Kaniko yet
# — that's Phase 6), build and tag it by hand:
#
#   docker build -t kubesandbox/go:1.22.5-slim components/languages/go
#
# which matches this component's manifest source.image reference exactly. Debian-based
# (not alpine/musl) to match this repo's other golden images and avoid cgo surprises.

FROM golang:1.22.5-bookworm

# No root, no admin — ever (doc §6 Layer 1).
RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox

# dtach: interactive PTY attach (doc §5.2, Phase 4) runs `dtach -A ...` so a reattach
# after disconnect resumes the same shell (cwd/env/jobs), not a fresh one. Not tmux:
# confirmed live that tmux >=3.4's per-pane cgroup-via-systemd-dbus move is fatal in
# this non-systemd container (no dbus session bus) — dtach has no such dependency.
# bash: `golang:*-bookworm` (full, not slim) likely already ships it, but naming it
# explicitly here removes any doubt rather than relying on an upstream base image detail.
RUN apt-get update \
    && apt-get install -y --no-install-recommends dtach bash \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace && chown -R 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

# GOCACHE/GOPATH default under $HOME, which this non-root, no-home user doesn't have;
# /tmp is already a writable tmpfs mount at runtime (access.filesystem.writablePaths),
# so pointing both there needs no extra Dockerfile-level directory setup.
ENV GOCACHE=/tmp/go-cache
ENV GOPATH=/tmp/go-path

ENTRYPOINT []
CMD ["go"]
