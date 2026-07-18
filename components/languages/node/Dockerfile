# syntax=docker/dockerfile:1
#
# Golden image for the `node` component (docs/ARCHITECTURE_AND_PLAN.md §8). Built once
# at publish time, never inside a request path. For local dev (no BuildManager/Kaniko
# yet — that's Phase 6), build and tag it by hand:
#
#   docker build -t kubesandbox/node:20.15.0-slim components/languages/node
#
# which matches this component's manifest source.image reference exactly.

FROM node:20-slim

# No root, no admin — ever (doc §6 Layer 1).
RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox

RUN mkdir -p /workspace /opt/kubesandbox/runners \
    && chown -R 10001:10001 /workspace /opt/kubesandbox

COPY --chown=10001:10001 runner.js /opt/kubesandbox/runners/node_runner.js

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT []
CMD ["node"]
