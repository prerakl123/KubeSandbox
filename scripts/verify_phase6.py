#!/usr/bin/env python3
"""One-shot live-verification script for Phase 6 (build system & golden images,
doc §8) — not part of the app itself, a throwaway tool for the same relayed
hand-off loop Phases 3-5 used, just packaged as a single script instead of a
sequence of pasted shell commands.

Brings up infra, applies migrations, starts the app if it isn't already running,
then walks through: building `jq` (dockerfile strategy) and running it, removing
its pushed image tag to prove DockerProvisioner genuinely pulls from the local
registry rather than reusing a cached tag, building `ripgrep` (compose strategy)
and running it, building `httpie` (pipeline strategy) twice to show the second
build hit its cache (no re-run steps), and building `demo-echo` (helm strategy,
skipped automatically if `helm` isn't on PATH).

Deliberately stdlib-only (urllib/subprocess/json, no httpx/requests) and calls the
venv's own interpreter by absolute path (`.venv/bin/python`/`.venv/bin/alembic`)
rather than `uv run` — so it works the same whether invoked directly or via `sudo`,
which may not see the shell's normal PATH/venv activation.

Usage (run from the repo root, or anywhere — paths are resolved off this file's
own location):
    sudo python3 scripts/verify_phase6.py
    # or, without sudo, if your user is already in the `docker` group:
    python3 scripts/verify_phase6.py

Review this script before running it with sudo, same as any script you'd run with
elevated privileges — it only touches this repo's own docker-compose stack, the
`kubesandbox/*`/`localhost:5000/kubesandbox/*` image tags, and localhost:8000.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
VENV_ALEMBIC = REPO_ROOT / ".venv" / "bin" / "alembic"
BASE_URL = "http://localhost:8000"
SERVER_LOG = REPO_ROOT / "scripts" / ".verify_phase6_server.log"

_CLEAN_TAGS = [
    "kubesandbox/jq:1.0",
    "localhost:5000/kubesandbox/jq:1.0",
    "kubesandbox/ripgrep:1.0",
    "localhost:5000/kubesandbox/ripgrep:1.0",
    "kubesandbox/httpie:1.0",
    "localhost:5000/kubesandbox/httpie:1.0",
]


def _hr(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        print(f"\n'{cmd[0]}' failed (exit {result.returncode}) — stopping here.")
        sys.exit(1)
    return result


def http(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def server_healthy() -> bool:
    try:
        status, _ = http("GET", "/healthz")
        return status == 200
    except Exception:
        return False


def wait_for_server(timeout_seconds: int) -> bool:
    for _ in range(timeout_seconds):
        if server_healthy():
            return True
        time.sleep(1)
    return False


def poll_build(build_id: str, *, timeout_seconds: int = 180) -> dict:
    body: dict = {}
    for _ in range(timeout_seconds // 2):
        _, body = http("GET", f"/v1/builds/{build_id}")
        print(f"  build {build_id} -> {body.get('status')}")
        if body.get("status") in ("succeeded", "failed"):
            return body
        time.sleep(2)
    print("  gave up waiting — last known state above")
    return body


def build_component(name: str, *, language: str | None, code: str | None) -> dict:
    _hr(name)
    _, trigger = http("POST", f"/v1/components/{name}/build")
    print("trigger response:", json.dumps(trigger, indent=2))
    if "id" not in trigger:
        print(f"could not trigger a build for {name!r} — see response above")
        return trigger
    result = poll_build(trigger["id"])
    print("final build record:", json.dumps(result, indent=2))
    if result.get("status") == "succeeded" and language is not None:
        _, run_result = http("POST", "/v1/execute", {"language": language, "code": code or ""})
        print("execute result:", json.dumps(run_result, indent=2))
    return result


def main() -> None:
    _hr("1. Infra up + migrations")
    run(["docker", "compose", "up", "-d"], check=True)
    run([str(VENV_ALEMBIC), "upgrade", "head"], check=True)

    _hr("2. Clean slate (ignore errors for tags that don't exist yet)")
    for tag in _CLEAN_TAGS:
        run(["docker", "rmi", "-f", tag])

    server_process = None
    if server_healthy():
        _hr("3. App already running on :8000 — reusing it")
    else:
        _hr("3. Starting the app")
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_file = SERVER_LOG.open("w")
        server_process = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "uvicorn", "app.main:app", "--port", "8000"],
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        if not wait_for_server(30):
            print(f"Server did not become healthy in time — see {SERVER_LOG}:")
            print(SERVER_LOG.read_text())
            sys.exit(1)
        print("Server is up.")

    try:
        build_component("jq", language="jq", code='{"hello": "world"}')

        _hr("Prove the local-registry pull-through (jq)")
        run(["docker", "rmi", "localhost:5000/kubesandbox/jq:1.0"])
        _, run_result = http("POST", "/v1/execute", {"language": "jq", "code": '{"hello": "world"}'})
        print("execute after removing the pushed tag:", json.dumps(run_result, indent=2))

        build_component("ripgrep", language="ripgrep", code="hello\nworld\n")

        build_component("httpie", language="httpie", code="")
        _hr("httpie again — expect a cache hit (no re-run steps) this time")
        build_component("httpie", language=None, code=None)

        if shutil.which("helm") is None:
            _hr("demo-echo (helm) — skipped, `helm` not found on PATH")
        else:
            build_component("demo-echo", language=None, code=None)
    finally:
        if server_process is not None:
            _hr("Stopping the server this script started")
            server_process.terminate()
            try:
                server_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_process.kill()
            print(f"Full server log: {SERVER_LOG}")

    _hr("Done")


if __name__ == "__main__":
    main()
