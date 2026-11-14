#!/usr/bin/env python3
"""Tests for `seneschald-control.ps1 -Action Stop` — the hand-over to `service.py stop`.

The Windows Stop's logic lives in Python (`service.py`, tested in test_service.py on every OS); the ps1's
Stop writes the deliberate-stop sentinel first and then delegates, keeping its legacy steps only as the
fallback for a host where python cannot run it (docs/service-supervision-spec.md §3). These tests
dot-source the REAL script into a throwaway layout and replace the two effects with recording stubs
(PowerShell resolves a function before a cmdlet of the same name), so nothing here can reach a real task
or a real process.

Windows + a responsive pwsh only (an AMSI-wedged pwsh hangs every launch), like test_seneschald_control.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

PWSH = shutil.which("pwsh")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _pwsh_responsive() -> bool:
    if not PWSH or os.name != "nt":
        return False
    try:
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", "1"],
                              capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


PWSH_OK = _pwsh_responsive()

_HARNESS = r"""
$ErrorActionPreference = 'Stop'
. '{script}'
$script:calls = @()
function Invoke-ServiceStop {{ $script:calls += 'service.py stop'; return {service_ok} }}
function Stop-ScheduledTask {{ param($TaskName, $ErrorAction) $script:calls += "Stop-ScheduledTask $TaskName" }}
function Stop-Process {{ param($Id, [switch]$Force, $ErrorAction) $script:calls += "Stop-Process $Id" }}
Stop-Seneschald
@{{ calls = $script:calls; sentinel = (Test-Path $StoppedFile); lock = (Test-Path $LockFile) }} | ConvertTo-Json -Compress
"""


@unittest.skipUnless(PWSH_OK, "Windows + a responsive pwsh only")
class StopDelegation(unittest.TestCase):
    def run_stop(self, service_ok: bool) -> dict:
        root = tempfile.mkdtemp(prefix="seneschald_stop_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        scripts = os.path.join(root, "seneschal", "scripts")
        state = os.path.join(root, "seneschal", "state")
        os.makedirs(scripts)
        os.makedirs(state)
        script = os.path.join(scripts, "seneschald-control.ps1")
        shutil.copy(os.path.join(SCRIPTS_DIR, "seneschald-control.ps1"), script)
        with open(os.path.join(state, "presence.lock"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999}, fh)
        harness = os.path.join(root, "harness.ps1")
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(_HARNESS.format(script=script.replace("'", "''"),
                                     service_ok="$true" if service_ok else "$false"))
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", harness],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_stop_writes_the_sentinel_then_hands_over_to_service_py(self):
        out = self.run_stop(service_ok=True)
        self.assertTrue(out["sentinel"])
        calls = out["calls"] if isinstance(out["calls"], list) else [out["calls"]]
        self.assertEqual(calls, ["service.py stop"], "no legacy step runs when service.py stopped it")

    def test_the_legacy_steps_are_the_fallback(self):
        out = self.run_stop(service_ok=False)
        self.assertTrue(out["sentinel"])
        self.assertEqual(out["calls"], ["service.py stop", "Stop-ScheduledTask seneschald", "Stop-Process 999999"])
        self.assertFalse(out["lock"])


if __name__ == "__main__":
    unittest.main()
