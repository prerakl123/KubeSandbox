"""Unit tests for the ComponentHook loader (doc §3.5, Phase 5). Uses a real importable
fake hook module (tests/unit/fixtures/fake_hook_module.py) rather than mocking
importlib, since load_hook's whole job is a real dotted-path import."""

from __future__ import annotations

import pytest

from app.extensions.hooks import load_hook


def test_load_hook_imports_module_level_hook_instance():
    hook = load_hook("tests.unit.fixtures.fake_hook_module")
    assert hook.__class__.__name__ == "FakeHook"


def test_load_hook_raises_on_missing_module():
    with pytest.raises(ModuleNotFoundError):
        load_hook("tests.unit.fixtures.does_not_exist")


def test_load_hook_raises_clearly_when_module_has_no_hook_attribute():
    with pytest.raises(ValueError, match="no module-level `hook`"):
        load_hook("tests.unit.fixtures.fake_hook_module_missing_attribute")
