# syntax=docker/dockerfile:1
#
# Golden image for the `jq` component — built by BuildManager's DockerfileBuildStrategy
# (doc §8, Phase 6) via `POST /v1/components/jq/build`, not a manual `docker build`
# like every Phase 1-5 component's Dockerfile.

FROM debian:12-slim

# No root, no admin — ever (doc §6 Layer 1), same convention as components/base.
RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox \
    && apt-get update \
    && apt-get install -y --no-install-recommends jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace && chown -R 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT []
CMD ["jq", "--version"]
