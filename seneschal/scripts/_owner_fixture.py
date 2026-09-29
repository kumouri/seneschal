#!/usr/bin/env python3
"""Test helper: a throwaway owner identity + state dir for the send-gate family of tests.

The send gate decides "is this the owner?" from `persona/identity.json` (`owner.email`,
`owner.emails`, `assistant.email`), which is gitignored and absent in CI — and may be a real file on
a developer's machine. A test that depends on who the owner is must therefore pin both: this module
writes a fixture identity into a temp dir, points `identity_common.IDENTITY_PATH` at it, and points
`SENESCHAL_STATE_DIR` at a temp state dir so no test ever reads or writes real state.

Not a test module itself (no `test_` prefix); imported by `test_send_*.py` and the outbound-script
tests.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import identity_common  # noqa: E402

#: The fixture owner's primary address (`owner.email`).
OWNER = "owner@example.com"
#: A second address of the owner's (`owner.emails`).
OWNER_ALT = "owner.alt@example.org"
#: The assistant's own send-from address (`assistant.email`) — counts as self.
ASSISTANT = "assistant@example.com"


def use_fixture_owner(testcase, *, owner: str | None = OWNER, emails=(OWNER_ALT,),
                      assistant: str | None = ASSISTANT) -> str:
    """Pin the identity and the state dir for one test (undone by `addCleanup`). Returns the temp
    state dir."""
    root = tempfile.mkdtemp(prefix="send-gate-test-")
    ident_path = os.path.join(root, "identity.json")
    with open(ident_path, "w", encoding="utf-8") as fh:
        json.dump({"owner": {"email": owner, "emails": list(emails)},
                   "assistant": {"email": assistant}}, fh)
    state = os.path.join(root, "state")
    os.makedirs(state, exist_ok=True)
    for patcher in (mock.patch.object(identity_common, "IDENTITY_PATH", ident_path),
                    mock.patch.dict(os.environ, {"SENESCHAL_STATE_DIR": state})):
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return state
