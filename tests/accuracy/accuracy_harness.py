"""The correction-accuracy bank: load the corpus, run a correction path, score it.

Two paths, both the product's own code from the text to what the strip's "Use" would type:

- local: Harper (glance_core) -> the issue filter (protected spans, Banglish) -> the
  confident fixes (`grammar.corrections`) -> `strip_fix`.
- ai: the same, plus an analysis from a backend (`--backend` in scripts/eval-accuracy.py)
  through `daemon.ai._entry` (shielding, `backends.corrected`), its corrections replacing
  the local ones as they do in glanced.

Scores, per tag and overall:

- exact: the output is the expected text (or one of its `alt`s);
- edit precision / recall / F1: word-level edits (whitespace and punctuation tokens count)
  of the output against those of the expected text, both from the input;
- false corrections: the share of cases with any edit the expected text does not make.

The corpus (corpus.jsonl) has one case per line: {"id", "input", "minimal", "fluent"
(only when it differs from "minimal"), "alt" (other acceptable outputs, optional), "tags"}.
"""
from __future__ import annotations

import difflib
import json
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus.jsonl"
BASELINE = HERE / "baseline.json"
MODES = ("minimal", "fluent")
# Cases that must come back unchanged (or changed only where the expected text says):
# what "false corrections" is read on.
GUARDED = ("correct", "protected", "banglish")


@dataclass(frozen=True)
class Case:
    id: str
    input: str
    minimal: str
    fluent: str
    tags: tuple[str, ...]
    alt: tuple[str, ...] = ()

    def expected(self, mode: str) -> str:
        return self.fluent if mode == "fluent" else self.minimal

    def references(self, mode: str) -> tuple[str, ...]:
        """Minimal mode also accepts the fluent text: a fix that rephrases a little more
        is not a mistake there, only a miss is (fluent mode accepts no less than fluent)."""
        if mode == "fluent":
            return (self.fluent, *self.alt)
        return (self.minimal, *self.alt, self.fluent)


def load_corpus(path: Path = CORPUS) -> list[Case]:
    out, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        o = json.loads(line)
        cid = o["id"]
        if cid in seen:
            raise ValueError(f"line {n}: duplicate id {cid}")
        seen.add(cid)
        minimal = o.get("minimal", o["input"])
        out.append(Case(cid, o["input"], minimal, o.get("fluent", minimal),
                        tuple(o["tags"]), tuple(o.get("alt", ()))))
    return out


# ---------------------------------------------------------------------------- edits

_TOKEN = re.compile(r"\w+(?:['’]\w+)*|\s+|[^\w\s]")


def edits(src: str, dst: str) -> set[tuple[int, int, str]]:
    """The word-level edits that turn `src` into `dst`: (first token, end token, new text)
    over `src`'s tokens (words, whitespace runs, punctuation). A one-for-one run of replaced
    tokens is one edit per token, so two fixes side by side count as two."""
    a, b = _TOKEN.findall(src.strip()), _TOKEN.findall(dst.strip())
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: set[tuple[int, int, str]] = set()
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and i2 - i1 == j2 - j1:
            out |= {(i1 + k, i1 + k + 1, b[j1 + k]) for k in range(i2 - i1)
                    if a[i1 + k] != b[j1 + k]}
            continue
        out.add((i1, i2, "".join(b[j1:j2])))
    return out


@dataclass
class CaseScore:
    id: str
    tags: tuple[str, ...]
    exact: bool
    tp: int
    fp: int
    fn: int
    output: str
    expected: str

    @property
    def false_correction(self) -> bool:
        return self.fp > 0


def score_case(case: Case, output: str, mode: str) -> CaseScore:
    """Against the reference the output is closest to (fewest wrong and missed edits)."""
    out = output.strip()
    best = None
    for ref in case.references(mode):
        gold, got = edits(case.input, ref), edits(case.input, out)
        tp = len(gold & got)
        s = CaseScore(case.id, case.tags, out == ref.strip(), tp, len(got) - tp,
                      len(gold) - tp, out, ref)
        if best is None or (s.exact, -(s.fp + s.fn)) > (best.exact, -(best.fp + best.fn)):
            best = s
    assert best is not None
    return best


@dataclass
class TagScore:
    n: int = 0
    exact: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    false_corrections: int = 0

    def add(self, s: CaseScore) -> None:
        self.n += 1
        self.exact += s.exact
        self.tp += s.tp
        self.fp += s.fp
        self.fn += s.fn
        self.false_corrections += s.false_correction

    def summary(self) -> dict[str, float | int]:
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        return {"n": self.n, "exact": round(self.exact / self.n, 3) if self.n else 0.0,
                "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
                "false_correction": round(self.false_corrections / self.n, 3)
                if self.n else 0.0}


def aggregate(scores: list[CaseScore]) -> dict[str, dict[str, float | int]]:
    tags: dict[str, TagScore] = defaultdict(TagScore)
    for s in scores:
        tags["ALL"].add(s)
        for t in s.tags:
            tags[t].add(s)
    return {t: tags[t].summary() for t in sorted(tags, key=lambda t: (t != "ALL", t))}


def table(report: dict[str, dict[str, float | int]], title: str = "") -> str:
    head = f"{'tag':<13}{'n':>4}{'exact':>8}{'P':>7}{'R':>7}{'F1':>7}{'falsecorr':>11}"
    lines = [title, head] if title else [head]
    for tag, r in report.items():
        lines.append(f"{tag:<13}{r['n']:>4}{r['exact']:>8.3f}{r['precision']:>7.3f}"
                     f"{r['recall']:>7.3f}{r['f1']:>7.3f}{r['false_correction']:>11.3f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- paths

PROFILE_IGNORED: frozenset[str] = frozenset()  # the "default" profile ignores no rule


@dataclass
class LocalPath:
    """Harper and the issue filter as glanced builds them, with an empty dictionary."""

    grammar: object = None
    filter: object = None
    detector: object = None
    _tmp: object = field(default=None, repr=False)

    @classmethod
    def create(cls) -> LocalPath:
        from glance.daemon.main import build_issue_filter
        from glance.grammar.harper_native import NativeHarperClient

        tmp = tempfile.TemporaryDirectory(prefix="glance-accuracy-")
        d = Path(tmp.name)
        client = NativeHarperClient(d / "dictionary.txt", warm=False)
        client.start()
        flt, detector = build_issue_filter(d / "dictionary.txt", d / "ignored.json")
        return cls(client, flt, detector, tmp)

    def kept(self, text: str, extra: list | tuple = ()) -> list:
        """The issues glanced keeps: Harper's and `extra` (the AI's) through the filter."""
        issues = self.grammar.check(text) + list(extra)
        return self.filter.apply(text, issues, "glance-accuracy", extra_ignored=PROFILE_IGNORED)

    def is_mixed(self, text: str) -> bool:
        return self.detector.is_mixed(text)


def apply_issues(text: str, issues: list, ai_corrects: bool) -> str:
    """What "Use" would type: the strip's corrected text (caret at the end)."""
    from glance.grammar.corrections import combine_sources
    from glance.ui.strip_model import strip_fix

    fix = strip_fix(text, len(text), combine_sources(text, issues, ai_corrects))
    if fix is None:
        return text
    return text[:fix.edit.start] + fix.edit.replacement + text[fix.edit.end:]


def local_output(local: LocalPath, text: str) -> str:
    return apply_issues(text, local.kept(text), False)


def ai_output(local: LocalPath, text: str, corrected: str | None, mode: str) -> str:
    """The output once the AI's corrected text for `text` is back. `corrected` None: the
    call failed, the quick local fixes are all there is."""
    from glance.backends.base import Analysis
    from glance.daemon.ai import _entry
    from glance.privacy.redact import shield

    if corrected is None:
        return apply_issues(text, local.kept(text), False)
    shielded = shield(text)
    analysis = Analysis(tone="neutral", mismatch=False, reason=None, issues=(), flags=(),
                        corrected=corrected)
    entry = _entry(shielded, analysis,
                   **({} if mode == "minimal" else {"correction": mode}))
    return apply_issues(text, local.kept(text, entry.issues), entry.corrects)


# ---------------------------------------------------------------------------- batching

SEP = "\n\n"


def batches(cases: list[Case], size: int, mixed: frozenset[str]) -> list[list[Case]]:
    """Cases in groups of about `size`, the Banglish ones apart (glanced tells the model a
    text is mixed only when it is, so English drafts are sent without that line). Inputs
    are joined by a blank line into one draft per call (the paid budget is small)."""
    plain = [c for c in cases if not set(c.tags) & mixed]
    mix = [c for c in cases if set(c.tags) & mixed]
    out = []
    for group in (plain, mix):
        k = max(1, -(-len(group) // size)) if group else 0
        step = -(-len(group) // k) if k else 0
        out += [group[i:i + step] for i in range(0, len(group), step)] if step else []
    return out


def split_batch(cases: list[Case], corrected: str) -> list[str | None]:
    """The model's corrected draft back into one corrected text per case: by blank lines
    when it kept them, else by each case's own line count when it closed them up to single
    line breaks; None for each when neither fits (scored as the local fixes alone)."""
    text = corrected.strip()
    parts = [p.strip() for p in re.split(r"\n\s*\n", text)]
    if len(parts) == len(cases):
        return parts
    lines = [ln for ln in text.split("\n") if ln.strip()]
    sizes = [c.input.count("\n") + 1 for c in cases]
    if len(lines) != sum(sizes):
        return [None] * len(cases)
    out, pos = [], 0
    for n in sizes:
        out.append("\n".join(lines[pos:pos + n]).strip())
        pos += n
    return out
