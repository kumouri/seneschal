#!/usr/bin/env python3
"""Imported FIRST by `unittest discover` in this root — `test_0_…` sorts before every other
`test_*.py` — so the whole suite's temp traffic lands in one run root that is removed at exit.
Why, and what `install()` does: `tmproot.py`. The cases are the blocking half of the temp-leak
regression guard; keep this file's name sorting first and its import side effect at the top."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tmproot  # noqa: E402

tmproot.install()

RunRootInForceTest = tmproot.guard_tests(__file__)
