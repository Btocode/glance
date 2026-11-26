#!/usr/bin/env python3
"""Score Glance's corrections on the accuracy bank (tests/accuracy/corpus.jsonl).

    PYTHONPATH=src python scripts/eval-accuracy.py                  # local only (Harper)
    PYTHONPATH=src python scripts/eval-accuracy.py --write-baseline # store the local scores
    GLANCE_PAID=1 PYTHONPATH=src python scripts/eval-accuracy.py --ai --mode minimal \\
        --backend claude --model haiku --record tests/accuracy/runs/minimal.json
    PYTHONPATH=src python scripts/eval-accuracy.py --replay tests/accuracy/runs/minimal.json

The AI path sends the corpus in drafts of about `--batch` cases joined by blank lines (one
call each, so a full pass costs ~300/batch calls), Banglish cases in their own drafts. Each
answer is recorded (`--record`) so it can be scored again for free (`--replay`) after a
change to the filters; a prompt change needs new calls. Real backends need GLANCE_PAID=1.

`--backend` is a glance CLI backend (claude, codex, gemini, opencode) or `module:factory`,
a callable taking the model name and returning an object with `_json(system, user, schema,
timeout_s) -> dict`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "accuracy"))
if not os.environ.get("PYTHONPATH"):  # else it may point at another checkout: keep it
    sys.path.insert(0, str(ROOT / "src"))

import accuracy_harness as H

_PH = re.compile(r"\[\[GLANCE_[A-Z]\d+\]\]")


def make_backend(name: str, model: str, effort: str = ""):
    if ":" in name:
        mod, _, fn = name.partition(":")
        return getattr(importlib.import_module(mod), fn)(model)
    from glance.backends.proc import ProcRunner

    classes = {
        "claude": ("glance.backends.claude", "ClaudeBackend"),
        "codex": ("glance.backends.codex", "CodexBackend"),
        "gemini": ("glance.backends.gemini", "GeminiBackend"),
        "opencode": ("glance.backends.opencode", "OpencodeBackend"),
    }
    mod, cls = classes[name]
    backend = getattr(importlib.import_module(mod), cls)(ProcRunner(), model)
    if effort and name == "claude" and "--effort" not in backend.argv_json("", {}):
        plain = backend.argv_json

        def argv_json(system: str, schema: dict) -> list[str]:
            argv = plain(system, schema)
            return [*argv[:2], "--effort", effort, *argv[2:]]  # after "claude -p"
        backend.argv_json = argv_json
    elif effort and hasattr(backend, "effort"):
        backend.effort = effort
    return backend


def ctx_for(mixed: bool, mode: str):
    from glance.backends.base import Ctx

    kw = {} if mode == "minimal" else {"correction": mode}
    return Ctx(profile="default", target_tone="clear, neutral", strictness="normal",
               app_label="glance-accuracy", mixed_language=mixed, **kw)


def renumber(case_input: str, batch_part: str, corrected_part: str) -> str:
    """The model's corrected text of one case, with the draft's placeholders renamed to the
    ones the case alone would have had."""
    from glance.privacy.redact import shield

    mine = _PH.findall(shield(case_input).text)
    theirs = _PH.findall(batch_part)
    mapping = dict(zip(theirs, mine)) if len(mine) == len(theirs) else {}
    return _PH.sub(lambda m: mapping.get(m.group(), m.group()), corrected_part)


def run_ai(cases, args) -> dict:
    from glance.backends.prompts import ANALYZE_SCHEMA, analyze_system, user_block
    from glance.privacy.redact import shield

    if args.backend != "fake" and os.environ.get("GLANCE_PAID") != "1":
        sys.exit("the AI path makes paid calls: set GLANCE_PAID=1")
    local = H.LocalPath.create()
    backend = make_backend(args.backend, args.model, args.effort)
    groups = H.batches(cases, args.batch, frozenset({"banglish"}))
    if len(groups) > args.max_calls:
        sys.exit(f"{len(groups)} calls needed, --max-calls is {args.max_calls}")
    record = {"mode": args.mode, "backend": args.backend, "model": args.model,
              "effort": args.effort or "default",
              "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "batches": []}
    for n, group in enumerate(groups, 1):
        joined = H.SEP.join(c.input for c in group)
        shielded = shield(joined)
        mixed = local.is_mixed(joined)
        system = analyze_system(ctx_for(mixed, args.mode))
        entry = {"ids": [c.id for c in group], "mixed": mixed, "draft": shielded.text,
                 "system_sha": hashlib.sha256(system.encode()).hexdigest()[:12]}
        print(f"call {n}/{len(groups)}: {len(group)} cases", file=sys.stderr, flush=True)
        try:
            obj = backend._json(system, user_block(shielded.text), ANALYZE_SCHEMA,
                                args.timeout)
            entry["corrected"] = obj.get("corrected") if isinstance(obj, dict) else None
        except Exception as e:  # noqa: BLE001 - recorded, scored as the local fixes
            entry["corrected"] = None
            entry["error"] = f"{type(e).__name__}: {e}"[:300]
        record["batches"].append(entry)
    return record


def score_record(cases, record, mode: str, local, refs: str) -> list:
    by_id = {c.id: c for c in cases}
    scores = []
    for b in record["batches"]:
        group = [by_id[i] for i in b["ids"] if i in by_id]
        if len(group) != len(b["ids"]):
            group_ids = set(by_id)
            group = [by_id[i] for i in b["ids"] if i in group_ids]
        parts_in = b["draft"].split(H.SEP)
        parts_out = H.split_batch(group, b["corrected"]) if b.get("corrected") \
            and len(parts_in) == len(b["ids"]) else [None] * len(group)
        index = {i: k for k, i in enumerate(b["ids"])}
        for c, out in zip(group, parts_out):
            corrected = None if out is None else renumber(c.input, parts_in[index[c.id]], out)
            scores.append(H.score_case(c, H.ai_output(local, c.input, corrected, mode), refs))
    return scores


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ai", action="store_true", help="run the AI path (paid)")
    ap.add_argument("--replay", type=Path, help="score a recorded AI run again")
    ap.add_argument("--mode", choices=H.MODES, default=None)
    ap.add_argument("--refs", choices=H.MODES, default=None,
                    help="score against these expected texts (default: --mode)")
    ap.add_argument("--backend", default="claude")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--effort", default="",
                    help="claude: pass --effort (low, medium, ...); default: the CLI's own")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--max-calls", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--record", type=Path)
    ap.add_argument("--tags", help="only cases with any of these tags (comma-separated)")
    ap.add_argument("--fails", action="store_true", help="list the cases that differ")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--write-baseline", action="store_true")
    args = ap.parse_args()

    cases = H.load_corpus()
    if args.tags:
        want = set(args.tags.split(","))
        cases = [c for c in cases if want & set(c.tags)]
    if args.ai:
        args.mode = args.mode or "minimal"
        record = run_ai(cases, args)
        if args.record:
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
    elif args.replay:
        record = json.loads(args.replay.read_text(encoding="utf-8"))
        args.mode = args.mode or record["mode"]
    else:
        record = None
        args.mode = args.mode or "minimal"

    local = H.LocalPath.create()
    refs = args.refs or args.mode
    if record is None:
        scores = [H.score_case(c, H.local_output(local, c.input), refs) for c in cases]
        title = f"local ({refs} references)"
    else:
        scores = score_record(cases, record, args.mode, local, refs)
        failed = sum(1 for b in record["batches"] if not b.get("corrected"))
        title = (f"ai {record['backend']}/{record['model']}/effort="
                 f"{record.get('effort', 'default')} prompt={record['mode']} "
                 f"filter={args.mode} refs={refs} ({len(record['batches'])} calls, {failed} failed)")
    report = H.aggregate(scores)
    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(H.table(report, title))
    if args.fails:
        for s in scores:
            if not s.exact:
                print(f"\n[{s.id}] {','.join(s.tags)}  fp={s.fp} fn={s.fn}\n"
                      f"  in : {next(c.input for c in cases if c.id == s.id)!r}\n"
                      f"  out: {s.output!r}\n  exp: {s.expected!r}")
    if args.write_baseline:
        if record is not None or args.tags:
            sys.exit("--write-baseline takes the whole corpus, local only")
        H.BASELINE.write_text(json.dumps({"local_minimal": report}, indent=1) + "\n",
                              encoding="utf-8")
        print(f"wrote {H.BASELINE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
