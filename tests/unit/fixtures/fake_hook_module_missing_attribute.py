"""A module deliberately missing the module-level `hook` instance load_hook() expects
— see test_hooks.py::test_load_hook_raises_clearly_when_module_has_no_hook_attribute.
"""

from __future__ import annotations

not_a_hook = object()
