"""Chrome's own CPU cost of accessibility (spec §8.4), report-only: no gate, the numbers go into
`docs/acceptance/report.md`.

    .venv/bin/pytest -m apps tests/acceptance/test_chrome_overhead.py -v -s

Three runs on `textarea.html`, each 60 s idle then 30 s typing at 6 keys/s, in a throwaway
profile on the private session:
  flag   Chrome with --force-renderer-accessibility, Glance running (the installed setup)
  auto   Chrome without the flag, Glance running (Chrome may turn accessibility on by itself
         when an AT-SPI client is active)
  bare   Chrome without the flag, no Glance (the baseline)
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.acceptance.harness import BROWSERS, Typist
from tests.acceptance.perf import HARPER, TimedGlanced, children, proc_cpu, type_paced

pytestmark = [
    pytest.mark.apps,
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
    pytest.mark.skipif(not Path(BROWSERS["chrome"]).exists(), reason="Chrome is not installed"),
]


def run_chrome(session, server, base, profile_root, flag: bool, typist) -> tuple[float, float]:
    profile = Path(tempfile.mkdtemp(prefix="chrome-ovh-", dir=profile_root))
    argv = [BROWSERS["chrome"], f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-position=0,0",
            "--window-size=1100,700", *(["--force-renderer-accessibility"] if flag else []),
            f"{base}/textarea.html"]
    proc = subprocess.Popen(argv, env=dict(session.env), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.wait_state(lambda s: "seq" in s, 40.0)
        server.cmd("focus")
        time.sleep(5.0)
        pids = [proc.pid, *children(proc.pid)]
        idle = proc_cpu(pids, 60.0)
        text = "I will recieve the report from teh team today and then reply soon. " * 3
        pids = [proc.pid, *children(proc.pid)]
        typing = proc_cpu(pids, 30.0, during=lambda: type_paced(typist, text[:180], 1 / 6))
        return idle, typing
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # our own throwaway browser
        shutil.rmtree(profile, ignore_errors=True)


def test_chrome_a11y_overhead(session, fixture_server, profile_root, tmp_path):
    server, base = fixture_server
    typist = Typist(session)
    results = {}
    try:
        with TimedGlanced(session, tmp_path / "g1") as g:
            assert g.status() is not None
            time.sleep(8.0)
            results["flag"] = run_chrome(session, server, base, profile_root, True, typist)
        with TimedGlanced(session, tmp_path / "g2"):
            time.sleep(8.0)
            results["auto"] = run_chrome(session, server, base, profile_root, False, typist)
        results["bare"] = run_chrome(session, server, base, profile_root, False, typist)
    finally:
        typist.close()
    print("\nChrome CPU, % of one core, (idle 60 s, typing 30 s):")
    for name, (idle, typing) in results.items():
        print(f"  {name}: idle {idle:.2f}  typing {typing:.2f}")
