#!/usr/bin/env python3
"""Imported FIRST by `unittest discover` in this root — `test_0_…` sorts before every other
`test_*.py` — so the whole suite's temp traffic lands in one run root that is removed at exit.
Why, and what `install()` does: `seneschal/scripts/tmproot.py`. Loaded by file path, never via `sys.path`, so
`seneschal/scripts` cannot shadow a same-named module in this tree. The cases are the blocking half
of the temp-leak regression guard; keep this file's name sorting first."""
from __future__ import annotations

import importlib.util
import os
import sys

_IMPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "seneschal", "scripts", "tmproot.py")
_spec = importlib.util.spec_from_file_location("seneschal_tmproot", _IMPL)
tmproot = sys.modules.setdefault("seneschal_tmproot", importlib.util.module_from_spec(_spec))
if not hasattr(tmproot, "install"):
    _spec.loader.exec_module(tmproot)

tmproot.install()

RunRootInForceTest = tmproot.guard_tests(__file__)
