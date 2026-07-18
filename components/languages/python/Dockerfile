# syntax=docker/dockerfile:1
#
# Golden image for the `python` component (docs/ARCHITECTURE_AND_PLAN.md §8).
# Built once at publish time, never inside a request path. For local dev (no
# BuildManager/Kaniko yet — that's Phase 6), build and tag it by hand:
#
#   docker build -t kubesandbox/python:3.12.4-slim components/languages/python
#
# which matches this component's manifest source.image reference exactly.

FROM python:3.12-slim

# No root, no admin — ever (doc §6 Layer 1).
RUN groupadd --gid 10001 kubesandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin kubesandbox

RUN mkdir -p /workspace /opt/kubesandbox/runners \
    && chown -R 10001:10001 /workspace /opt/kubesandbox

COPY --chown=10001:10001 runner.py /opt/kubesandbox/runners/python_runner.py

WORKDIR /workspace
USER 10001:10001

ENTRYPOINT []
CMD ["python3"]
