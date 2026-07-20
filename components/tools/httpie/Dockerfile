# syntax=docker/dockerfile:1
#
# Golden image for the `httpie` component — packaged by BuildManager's
# PipelineBuildStrategy after its declared steps succeed (doc §8, Phase 6), via
# `POST /v1/components/httpie/build`.

FROM debian:12-slim

RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox \
    && apt-get update \
    && apt-get install -y --no-install-recommends httpie ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace && chown -R 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT []
CMD ["http", "--version"]
