"""Parses the Kubernetes-style quantity strings used throughout manifests ("100m",
"512Mi") into the raw units each backend's API actually wants. Kubernetes accepts these
strings natively; Docker's Engine API wants nanocpus (int) and bytes (int) — hence a
shared, independently testable module rather than duplicating parsing per-provisioner.
"""

from __future__ import annotations

_MEMORY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def parse_cpu_to_nanocpus(cpu: str) -> int:
    """"100m" -> 100_000_000 (0.1 CPU); "2" -> 2_000_000_000 (2 CPUs)."""
    cpu = cpu.strip()
    if cpu.endswith("m"):
        millis = int(cpu[:-1])
        return millis * 1_000_000
    return int(cpu) * 1_000_000_000


def parse_memory_to_bytes(memory: str) -> int:
    """"128Mi" -> 134217728; "2Gi" -> 2147483648."""
    memory = memory.strip()
    for suffix, multiplier in _MEMORY_SUFFIXES.items():
        if memory.endswith(suffix):
            return int(memory[: -len(suffix)]) * multiplier
    return int(memory)
