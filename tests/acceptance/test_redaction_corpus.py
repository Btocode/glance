"""Secret redaction against a labelled synthetic corpus (spec §8.5): recall >= 99 % overall and
>= 97 % per kind, nothing secret in the shielded text, false positives <= 3 % on benign text.

The corpus is `scripts/gen-secret-corpus.py --seed 7`, generated at test time; it holds no real secret.
"""
from __future__ import annotations

import atexit
import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from glance.privacy.redact import find_secrets, shield

REPO = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).parent / "corpus"  # holds benign.jsonl only; secrets are generated


def _generate(out: Path) -> None:
    subprocess.run([sys.executable, str(REPO / "scripts" / "gen-secret-corpus.py"),
                    "--seed", "7", "--out", str(out)], check=True)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# Synthetic secret-shaped tokens are never committed (push protection, scrapers): generate them
# deterministically (seed 7) into a temp dir at import time.
_TMP = tempfile.mkdtemp(prefix="glance-corpus-")
atexit.register(shutil.rmtree, _TMP, True)
_generate(Path(_TMP))
SECRETS = _rows(Path(_TMP) / "secrets.jsonl")
BENIGN = _rows(CORPUS / "benign.jsonl")


def covered(row: dict) -> bool:
    """Every occurrence-span of the labelled secret lies wholly inside a found span."""
    text, secret = row["text"], row["secret"]
    start = text.find(secret)
    assert start >= 0, row
    end = start + len(secret)
    return any(s.start <= start and end <= s.end for s in find_secrets(text))


def test_corpus_shape():
    kinds = defaultdict(int)
    for r in SECRETS:
        kinds[r["kind"]] += 1
    assert len(SECRETS) == 600 and set(kinds.values()) == {100} and len(kinds) == 6
    assert len(BENIGN) == 400


def test_recall_overall_at_least_99():
    missed = [r for r in SECRETS if not covered(r)]
    recall = 1 - len(missed) / len(SECRETS)
    print(f"recall {recall:.4f} ({len(SECRETS) - len(missed)}/{len(SECRETS)})")
    assert recall >= 0.99, [(r["kind"], r["secret"]) for r in missed[:10]]


def test_recall_per_kind_at_least_97():
    per: dict[str, list[bool]] = defaultdict(list)
    for r in SECRETS:
        per[r["kind"]].append(covered(r))
    rates = {k: sum(v) / len(v) for k, v in per.items()}
    print("per kind", rates)
    assert all(rate >= 0.97 for rate in rates.values()), rates


def test_shielded_text_never_contains_secret():
    # What is not found is the recall tests' business; a found secret must always vanish.
    leaked = [r for r in SECRETS if covered(r) and r["secret"] in shield(r["text"]).text]
    assert leaked == []
    assert all("[[GLANCE_S" in shield(r["text"]).text for r in SECRETS if covered(r))


def test_benign_false_positive_rate_at_most_3_percent():
    flagged = [r for r in BENIGN if find_secrets(r["text"])]
    rate = len(flagged) / len(BENIGN)
    print(f"false positives {rate:.4f} ({len(flagged)}/{len(BENIGN)})")
    assert rate <= 0.03, [r["text"] for r in flagged[:10]]


def test_corpus_is_reproducible(tmp_path):
    _generate(tmp_path)
    assert filecmp.cmp(tmp_path / "secrets.jsonl", Path(_TMP) / "secrets.jsonl", shallow=False)
    assert filecmp.cmp(tmp_path / "benign.jsonl", CORPUS / "benign.jsonl", shallow=False)
