"""Unit tests for the cpu/memory quantity-string <-> raw-unit conversions (doc §20
Phase 5 needs the round trip once a sandbox's total resource footprint — main plus
sidecars — has to be summed/maxed in a common unit, then re-expressed as a k8s
quantity string for a ResourceQuota/LimitRange)."""

from __future__ import annotations

from app.provisioners.resources import (
    format_bytes_to_memory,
    format_nanocpus_to_cpu,
    parse_cpu_to_nanocpus,
    parse_memory_to_bytes,
)


def test_format_nanocpus_to_cpu_whole_cpus():
    assert format_nanocpus_to_cpu(2_000_000_000) == "2"


def test_format_nanocpus_to_cpu_millicpus():
    assert format_nanocpus_to_cpu(600_000_000) == "600m"


def test_format_bytes_to_memory_prefers_largest_exact_unit():
    assert format_bytes_to_memory(2 * 1024**3) == "2Gi"
    assert format_bytes_to_memory(256 * 1024**2) == "256Mi"
    assert format_bytes_to_memory(512 * 1024) == "512Ki"


def test_format_bytes_to_memory_falls_back_to_raw_bytes_when_not_aligned():
    assert format_bytes_to_memory(1500) == "1500"


def test_cpu_round_trips_through_parse_and_format():
    for cpu in ["100m", "500m", "1", "2", "4"]:
        assert format_nanocpus_to_cpu(parse_cpu_to_nanocpus(cpu)) == cpu


def test_memory_round_trips_through_parse_and_format():
    for memory in ["64Mi", "256Mi", "1Gi", "2Gi"]:
        assert format_bytes_to_memory(parse_memory_to_bytes(memory)) == memory


def test_summing_main_and_sidecar_nanocpus_then_formatting():
    total = parse_cpu_to_nanocpus("500m") + parse_cpu_to_nanocpus("100m")
    assert format_nanocpus_to_cpu(total) == "600m"


def test_summing_main_and_sidecar_memory_then_formatting():
    total = parse_memory_to_bytes("256Mi") + parse_memory_to_bytes("128Mi")
    assert format_bytes_to_memory(total) == "384Mi"
