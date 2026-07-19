#!/usr/bin/env python3
"""Guard: no daemon-tree subprocess spawn may pop a visible console window on Windows.

The presence daemon is frequently **console-less** — it self-respawns with ``DETACHED_PROCESS`` on a
graceful reload. On Windows, a console-subsystem child (``claude``, a python helper CLI) spawned by a console-less parent gets
a **fresh visible console window** unless ``CREATE_NO_WINDOW`` is set — those were the "random Claude
windows" that popped up. The fix is to pass
``creationflags`` on every spawn: ``CREATE_NO_WINDOW`` for the console children, ``DETACHED_PROCESS`` for
the detach itself.

This test parses the source and asserts **every** ``subprocess.Popen``/``subprocess.run`` call in the
daemon's process tree passes a ``creationflags`` keyword (or forwards ``**kwargs`` that carry it). It is a
structural guard: it fails the moment a new spawn is added without the flag, before it can regress the
popup fix. It is platform-independent (source inspection only), so it runs everywhere including CI.
"""
from __future__ import annotations

import ast
import os
import unittest

# The daemon's own process tree: the resident daemon and the sibling helper library it shells out
# through. Every subprocess spawn in these must be windowless.
DAEMON_TREE = ["presence.py", "sentinel.py"]


def _spawn_calls(tree: ast.AST):
    """Yield every ``subprocess.Popen(...)`` / ``subprocess.run(...)`` Call node in an AST."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr in ("Popen", "run")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            yield node


class WindowlessSpawnTest(unittest.TestCase):
    def test_every_daemon_spawn_passes_creationflags(self):
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        checked = 0
        for fname in DAEMON_TREE:
            with open(os.path.join(here, fname), encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fname)
            for call in _spawn_calls(tree):
                checked += 1
                has_flags = any(kw.arg == "creationflags" for kw in call.keywords)
                forwards_kwargs = any(kw.arg is None for kw in call.keywords)  # **kwargs carries the flag
                if not (has_flags or forwards_kwargs):
                    offenders.append(f"{fname}:{call.lineno}")
        self.assertEqual(
            offenders, [],
            "subprocess spawn(s) missing `creationflags` (would pop a visible window from the "
            f"console-less daemon on Windows): {offenders}")
        self.assertGreater(checked, 0, "found no subprocess spawns to check — did the files move?")


if __name__ == "__main__":
    unittest.main()
