#!/usr/bin/env python3
"""KubeSandbox batch runner for the Python component (doc §5.3).

Executes user code in an isolated namespace so the runner's own imports/locals never
leak into the captured variable dump, then serializes the final namespace to
VAR_DUMP_PATH for the provisioner to read after the process exits.

stdout/stderr are deliberately left untouched (inherited from this process) — the
provisioner captures those at the container level, not here. stdin is also inherited:
the provisioner writes the batch run's up-front stdin then closes the pipe (EOF) before
or at execution start, so a user `input()` call either gets real data or an immediate
EOFError — it never blocks waiting on a live client (doc §5.1).
"""

from __future__ import annotations

import json
import sys
import traceback
import types

VAR_DUMP_PATH = "/tmp/.kubesandbox_vars.json"


def _is_json_safe(value: object) -> bool:
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False


def _dump_namespace(namespace: dict) -> dict:
    dumped: dict[str, object] = {}
    for name, value in namespace.items():
        if name.startswith("__"):
            continue
        if callable(value) or isinstance(value, types.ModuleType):
            continue
        dumped[name] = value if _is_json_safe(value) else repr(value)
    return dumped


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python_runner.py <file>", file=sys.stderr)
        return 2

    source_path = sys.argv[1]
    with open(source_path, encoding="utf-8") as fh:
        source = fh.read()

    namespace: dict[str, object] = {"__name__": "__main__", "__file__": source_path}
    exit_code = 0
    try:
        code = compile(source, source_path, "exec")
        exec(code, namespace)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    except BaseException:
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            with open(VAR_DUMP_PATH, "w", encoding="utf-8") as fh:
                json.dump(_dump_namespace(namespace), fh)
        except OSError as dump_exc:
            # Still best-effort (a missing dump just yields variables=null upstream),
            # but silently swallowing this hid a real bug once already — never again.
            print(f"kubesandbox_runner: failed to write variable dump: {dump_exc}", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
