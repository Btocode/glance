"""The local correction path (Harper + the confident fixes) on the accuracy bank: no tag may
score below tests/accuracy/baseline.json. Offline and quick (about two seconds).

After an intended change, see the scores with `PYTHONPATH=src python
scripts/eval-accuracy.py --fails` and store them with `--write-baseline`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import accuracy_harness as H

pytest.importorskip("glance_core")

TOL = 1e-9


@pytest.fixture(scope="module")
def report() -> dict:
    local = H.LocalPath.create()
    cases = H.load_corpus()
    return H.aggregate([H.score_case(c, H.local_output(local, c.input), "minimal")
                        for c in cases])


def test_local_path_no_regression(report):
    baseline = json.loads(H.BASELINE.read_text(encoding="utf-8"))["local_minimal"]
    worse = []
    for tag, base in baseline.items():
        now = report.get(tag)
        if now is None:
            worse.append(f"{tag}: missing")
            continue
        for key in ("exact", "f1"):
            if now[key] < base[key] - TOL:
                worse.append(f"{tag}.{key}: {now[key]} < {base[key]}")
        if now["false_correction"] > base["false_correction"] + TOL:
            worse.append(f"{tag}.false_correction: {now['false_correction']} > "
                         f"{base['false_correction']}")
    assert not worse, "local accuracy regressed:\n" + "\n".join(worse) + "\n\n" + \
        H.table(report)



def test_local_path_leaves_correct_and_banglish_text_alone(report):
    assert report["banglish"]["false_correction"] <= 0.03
    assert report["correct"]["false_correction"] <= 0.03
