"""AI usage ledger: what Glance's AI calls used, aggregated per (day, backend, model).

One entry per day and backend/model holds counts and token/cost sums, never text, prompts or
replies. Tokens and cost are recorded only when the backend's own output reports them
(claude: `usage` and `total_cost_usd`; api: `usage`; codex: `turn.completed` usage); there is no
pricing table, so a backend that reports no cost has an unknown (None) cost, never a guess.
The file is rewritten atomically (mode 0600) on every record; 90 days are kept.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

KEEP_DAYS = 90
PURPOSES = ("analyze", "rewrite", "complete", "sharpen", "select", "shot", "other")
# Backends that run through a CLI the user signed in to (usually a subscription).
SUBSCRIPTION_BACKENDS = ("claude", "codex", "gemini", "opencode")
SUBSCRIPTION_NOTE = ("cost shown is the API-equivalent figure the CLI reports; "
                     "on a subscription you are not billed per call")
MONTH_LIMIT_MESSAGE = "Monthly AI cost limit reached"


@dataclass(frozen=True)
class Usage:
    """What one call (or several attempts of it) used, as far as the backend reported."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    def __add__(self, other: Usage) -> Usage:
        def add(a: Any, b: Any) -> Any:
            return b if a is None else a if b is None else a + b

        return Usage(add(self.input_tokens, other.input_tokens),
                     add(self.output_tokens, other.output_tokens),
                     add(self.cost_usd, other.cost_usd))

    @property
    def empty(self) -> bool:
        return self.input_tokens is None and self.output_tokens is None and self.cost_usd is None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _money(value: Any) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return float(value) if ok else None


def make_usage(input_tokens: Any = None, output_tokens: Any = None,
               cost_usd: Any = None) -> Usage | None:
    """A Usage from untrusted backend output (bad values count as not reported); None if empty."""
    u = Usage(_count(input_tokens), _count(output_tokens), _money(cost_usd))
    return None if u.empty else u


def _blank(backend: str, model: str) -> dict[str, Any]:
    return {"backend": backend, "model": model, "calls": 0, "failed": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "token_calls": 0, "cost_calls": 0, "speculative": 0, "purposes": {}}


def _valid_entry(e: Any) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("backend"), str)
            and isinstance(e.get("model"), str)
            and all(type(e.get(k)) is int and e[k] >= 0 for k in
                    ("calls", "failed", "input_tokens", "output_tokens", "token_calls",
                     "cost_calls", "speculative"))
            and isinstance(e.get("cost_usd"), (int, float)) and isinstance(e.get("purposes"), dict))


class UsageLedger:
    """Thread-safe, persistent. A store that cannot be read starts empty (the unreadable file
    is kept as `.bad`); one that cannot be written never breaks an AI call."""

    def __init__(self, path: Path, today: Any = date.today) -> None:
        self._path, self._today = path, today
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, dict[str, Any]]] = {}
        self._load()

    # -- store ---------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError):
            return
        try:
            days = json.loads(raw)["days"]
            clean: dict[str, dict[str, dict[str, Any]]] = {}
            for day, entries in days.items():
                date.fromisoformat(day)
                clean[day] = {k: e for k, e in entries.items() if _valid_entry(e)}
            self._days = clean
        except (ValueError, KeyError, TypeError, AttributeError):
            try:
                os.replace(self._path, self._path.with_name(self._path.name + ".bad"))
            except OSError:
                pass

    def _save(self) -> None:
        tmp = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".usage-ledger-",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:  # mkstemp creates it 0600
                json.dump({"version": 1, "days": self._days}, f)
            os.replace(tmp, self._path)
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _prune(self) -> None:
        cutoff = (self._today() - timedelta(days=KEEP_DAYS)).isoformat()
        for day in [d for d in self._days if d < cutoff]:
            del self._days[day]

    # -- recording -----------------------------------------------------------
    def record(self, backend: str, model: str, purpose: str, speculative: bool,
               outcome: str, usage: Usage | None) -> None:
        """One call. `outcome`: ok | failed | cancelled (a cancelled call still counts)."""
        if purpose not in PURPOSES:
            purpose = "other"
        with self._lock:
            day = self._today().isoformat()
            entry = self._days.setdefault(day, {}).setdefault(
                f"{backend}/{model}", _blank(backend, model))
            entry["calls"] += 1
            if outcome == "failed":
                entry["failed"] += 1
            if speculative:
                entry["speculative"] += 1
            entry["purposes"][purpose] = entry["purposes"].get(purpose, 0) + 1
            if usage is not None:
                if usage.input_tokens is not None or usage.output_tokens is not None:
                    entry["token_calls"] += 1
                    entry["input_tokens"] += usage.input_tokens or 0
                    entry["output_tokens"] += usage.output_tokens or 0
                if usage.cost_usd is not None:
                    entry["cost_calls"] += 1
                    entry["cost_usd"] = round(entry["cost_usd"] + usage.cost_usd, 6)
            self._prune()
            self._save()

    # -- reading -------------------------------------------------------------
    def _period(self, start: date, end: date) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {}
        with self._lock:
            for day, entries in self._days.items():
                if not start.isoformat() <= day <= end.isoformat():
                    continue
                for key, e in entries.items():
                    r = rows.setdefault(key, {"backend": e["backend"], "model": e["model"],
                                              "calls": 0, "failed": 0, "input_tokens": 0,
                                              "output_tokens": 0, "token_calls": 0,
                                              "cost_usd": 0.0, "cost_calls": 0,
                                              "speculative": 0, "purposes": {}})
                    for k in ("calls", "failed", "input_tokens", "output_tokens",
                              "token_calls", "cost_calls", "speculative"):
                        r[k] += e[k]
                    r["cost_usd"] += e["cost_usd"]
                    for p, n in e["purposes"].items():
                        r["purposes"][p] = r["purposes"].get(p, 0) + n
        out = sorted(rows.values(), key=lambda r: (-r["calls"], r["backend"], r["model"]))
        total = {"calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
                 "tokens_known": False, "cost_usd": None}
        for r in out:
            for k in ("calls", "failed", "input_tokens", "output_tokens"):
                total[k] += r[k]
            total["tokens_known"] = total["tokens_known"] or r["token_calls"] > 0
            if r["cost_calls"]:
                total["cost_usd"] = (total["cost_usd"] or 0.0) + r["cost_usd"]
            r["tokens_known"] = r.pop("token_calls") > 0
            r["cost_usd"] = round(r["cost_usd"], 6) if r.pop("cost_calls") else None
        if total["cost_usd"] is not None:
            total["cost_usd"] = round(total["cost_usd"], 6)
        return {"start": start.isoformat(), "end": end.isoformat(), "rows": out, "total": total}

    def month_cost(self) -> float:
        """Reported cost so far this calendar month (0.0 when nothing reported one)."""
        today = self._today()
        return self._period(today.replace(day=1), today)["total"]["cost_usd"] or 0.0

    def periods(self, days: int | None = None) -> dict[str, dict[str, Any]]:
        """Today / last 7 days / this month, or just the last `days` days when given."""
        today = self._today()
        if days is not None:
            return {f"last_{days}_days": self._period(today - timedelta(days=days - 1), today)}
        return {"today": self._period(today, today),
                "last_7_days": self._period(today - timedelta(days=6), today),
                "this_month": self._period(today.replace(day=1), today)}


# -- presentation (pure) ---------------------------------------------------------------


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "n/a"
    return f"${cost:.2f}" if cost >= 0.01 or cost == 0 else f"${cost:.4f}"


def summary_line(label: str, total: dict[str, Any]) -> str:
    """"Today: 26 calls · 12.3k tokens · $0.04" (tokens/cost shown only when known)."""
    n = total["calls"]
    parts = [f"{n} call{'' if n == 1 else 's'}"]
    if total["tokens_known"]:
        parts.append(f"{fmt_tokens(total['input_tokens'] + total['output_tokens'])} tokens")
    if total["cost_usd"] is not None:
        parts.append(fmt_cost(total["cost_usd"]))
    return f"{label}: " + " · ".join(parts)


def _cells(r: dict[str, Any]) -> list[str]:
    calls = f"{r['calls']} ({r['failed']})"
    known = r.get("tokens_known", True)
    return [calls, fmt_tokens(r["input_tokens"]) if known else "n/a",
            fmt_tokens(r["output_tokens"]) if known else "n/a", fmt_cost(r["cost_usd"])]


def _table(rows: list[dict[str, Any]], total: dict[str, Any]) -> list[str]:
    head = ["backend / model", "calls (failed)", "tokens in", "tokens out", "cost"]
    body = [[f"{r['backend']} / {r['model'] or 'default'}", *_cells(r)] for r in rows]
    body.append(["total", *_cells(total)])
    widths = [max(len(row[i]) for row in [head, *body]) for i in range(5)]

    def line(row: list[str]) -> str:
        return (row[0].ljust(widths[0]) + "  "
                + "  ".join(c.rjust(w) for c, w in zip(row[1:], widths[1:], strict=True)))

    return [line(head), *[line(b) for b in body[:-1]], line(body[-1])]


def render_report(report: dict[str, Any]) -> str:
    """The text `glance usage` prints (and the tray's Details window shows)."""
    titles = {"today": "Today", "last_7_days": "Last 7 days", "this_month": "This month"}
    out: list[str] = []
    cli_cost = False
    for key, period in report["periods"].items():
        title = titles.get(key) or key.replace("_", " ").capitalize()
        out.append(f"{title} ({period['start']}" + (f" to {period['end']})"
                   if period["end"] != period["start"] else ")"))
        if not period["rows"]:
            out += ["  no AI calls", ""]
            continue
        out += ["  " + ln for ln in _table(period["rows"], period["total"])]
        out.append("")
        cli_cost = cli_cost or any(r["backend"] in SUBSCRIPTION_BACKENDS
                                   and r["cost_usd"] is not None for r in period["rows"])
    b = report.get("budget") or {}
    if b:
        out.append(f"Calls today: {b['used_today']} of {b['per_day']} (daily limit); "
                   f"limit {b['per_minute']} per minute")
    limit = report.get("monthly_cost_limit_usd")
    if limit is not None:
        out.append(f"Month cost: {fmt_cost(report.get('month_cost_usd'))} of "
                   f"{fmt_cost(limit)} (monthly limit)")
    if report.get("limit_reason") == "month":
        out.append(MONTH_LIMIT_MESSAGE)
    out.append("Tokens and cost appear only where the backend reports them; "
               "n/a means it did not.")
    if cli_cost:
        out.append(f"Note: {SUBSCRIPTION_NOTE}.")
    return "\n".join(out)


def tray_lines(report: dict[str, Any]) -> tuple[str, ...]:
    """The tray submenu's lines: Today, This month, and the limit message when it applies."""
    p = report.get("periods") or {}
    lines = [summary_line(label, p[key]["total"])
             for label, key in (("Today", "today"), ("This month", "this_month")) if key in p]
    if report.get("limit_reason") == "month":
        lines.append(MONTH_LIMIT_MESSAGE)
    return tuple(lines)
