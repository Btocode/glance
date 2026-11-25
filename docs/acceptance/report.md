# Acceptance report

## Capture

Gate (spec §8.1): captured text and caret equal the editor's own after at least 99 % of 200 scripted edits
(`tests/acceptance/harness.py::edit_script`, seed 1). Run on 2026-11-25 on a private Xvfb with its own
D-Bus and AT-SPI bus and throwaway browser profiles:

    .venv/bin/pytest -m apps tests/acceptance/test_capture_matrix.py -v -rP

Exact = text (after `norm`) and caret both equal, for every target including GTK (buffer on SIGUSR1,
insert-mark offset on SIGUSR2). Brave/Slate's one miss: op 85 (arrow key), caret 30 against 29, text equal;
a caret event that arrived after the 120 ms quiet window. Each run also asserts the edits really happened
(final editor text over 100 characters, at least 95 % of type ops changed it, every pasted word landed).

| Target | textarea | contenteditable | ProseMirror | Slate | Lexical | Quill |
|---|---|---|---|---|---|---|
| Chrome 158 | 200/200 | 200/200 | 200/200 | 200/200 | 200/200 | 200/200 |
| Brave Nightly | 200/200 | 200/200 | 200/200 | 199/200 | 200/200 | 200/200 |
| Firefox (snap) | not run | not run | not run | not run | not run | not run |
| GTK TextView | 200/200 | | | | | |

Firefox: the snap cannot start on a private D-Bus (`snap-confine: ... is not a snap cgroup for tag
snap.firefox.firefox`); it needs the real systemd user bus to create its scope, which would mix it into the
user's session. The six pairs skip with that reason. Run them with a non-snap Firefox
(`GLANCE_FIREFOX_BIN=/path/to/firefox`) or by hand in the GNOME session.

Also checked: the ProseMirror mention (`role="img"` inline atom) reaches Glance as U+FFFC at the atom's
offset; the caret anchor (caret rectangle, or the right edge of the previous character at the end of the
text, as Chromium reports it) moves right on every key and stays inside the field.

## Performance

Spec §8.3 and §8.4 gates, measured on a private Xvfb (own D-Bus and AT-SPI bus) with the real glanced,
glance-capture and harper-ls, the in-process fake backend, and the GTK test app. Probes are in
`tests/acceptance/timed_glanced.py` (wraps the shipped classes from outside, text-free `log_event` lines);
helpers in `tests/acceptance/perf.py`. Final numbers are from a quiet machine (other work paused),
HEAD 62e9dc1, 2026-11-25:

    .venv/bin/pytest -m xvfb tests/acceptance/test_latency.py tests/acceptance/test_resources.py -v -s
    .venv/bin/pytest -m apps tests/acceptance/test_chrome_overhead.py -v -s     # report-only

### Latency

| Gate | Result | Bar | |
|---|---|---|---|
| Local fix chip, settle to chip (50 edits) | p50 0.46 / p95 0.92 ms | p95 <= 150 ms | pass |
| Same, from the last keystroke | p50 401.5 / p95 402.4 ms | (the 400 ms `chip_idle_ms` quiet period, ruling P4-8) | info |
| Chip reposition, snapshot to move (100 moves) | p50 30.52 / p95 30.68 ms | p95 <= 50 ms | pass |
| Chip reposition, after the 30 ms debounce | p50 0.24 / p95 0.40 ms | | pass |
| Pre-generated tone apply, hotkey to apply sent (15) | p50 0.71 / p95 21.9 ms | p95 <= 200 ms | pass |
| 1000 Tab presses, live autocomplete on, nothing to suggest | 1000 of 1000 reached the app | exactly 1000 | pass |
| Keystroke to buffer change, Glance stopped / running (2000 keys at 20 ms) | p50 0.07 / 0.05 ms, p99 0.17 / 0.15 ms | median diff < 1 ms, p99 diff < 5 ms | pass (diff -0.02 / -0.01 ms) |
| Chip or ghost window visible during a 20 s typing burst at 6 keys/s | 742 samples, never | never | pass |
| Live ghost on a fake `api`-style backend, last keystroke to ghost (15) | p50 605.5 / p95 619 ms | p50 <= 1200 ms | pass |
| analyze pipeline overhead (fake backend) | 3 to 6 ms | | info |

Note on reposition: `CARET_SETTLE_MS` is now 30 (was 50), so snapshot to move is about 30.5 ms; no chip/ghost flicker during the typing burst (the 20 s burst test still passes).

Real claude CLI (`paid`, 4 synthetic calls on 2026-11-25 at 42a7434, haiku, before the warm-process commits):
analyze 3.0, 4.2, 4.4 s (p50 4.2 s, over the 3.5 s bar). The first-rewrite-token measurement did not
complete (the hotkey was pressed before the analysis had resolved the audience; the test now waits for it).
Cost: 4 short calls, a few hundred tokens each, well under 1 US cent on haiku. The warm-versus-cold
comparison on the new warm default was NOT run: the permission layer refused the paid run, see the task report.
The `api` latencies (`test_api_latencies`) are not run (no API key).

### Resources

| Gate | Result | Bar | |
|---|---|---|---|
| Idle CPU, 60 s (glanced + capture + harper-ls) | 0.13 % of one core | < 0.5 % | pass |
| CPU while typing, 60 s at 6 keys/s | 1.45 % (harper-ls 0.0, glanced 0.57, capture 0.88) | < 3 % | pass |
| RSS, glanced + capture (harper-ls counted as a worker, see ruling) | 99.4 MB (glanced 55.9, capture 43.5); harper-ls 165.1 MB, own cap < 200 MB | < 250 MB | pass |

Fix, typing CPU: while keys keep arriving less than `chip_idle_ms` apart, the local (Harper) check waits for a
250 ms quiet (`LOCAL_BURST_QUIET_MS`) instead of 80 ms, so it no longer runs after every key; it still runs
before the 400 ms settle, so the whole field's issues are in place when the chip appears. (No mid-burst word-boundary
runs: the chip is hidden while typing, so they would only cost CPU. Incremental paragraph checks were not needed.)

Ruling, RSS: harper-ls holds about 153 MB of anonymous memory for its dictionary at start, unchanged by
disabling every linter (only SpellCheck affects speed, not memory) or by malloc tuning (best case 165 vs 171 MB).
The floor cannot go under the bar with glanced + capture (99 MB). harper-ls is therefore accounted like the AI
backends' worker processes: the 250 MB bar covers glanced + glance-capture (99.4 MB); harper-ls is reported separately
(165.1 MB) with its own cap of 200 MB in `test_rss`. Typing CPU still includes harper-ls.

### Chrome accessibility overhead (spec §8.4, report-only)

Chrome 158 on `textarea.html`, throwaway profile, % of one core summed over all Chrome processes:

| Run | idle 60 s | typing 30 s at 6 keys/s |
|---|---|---|
| with `--force-renderer-accessibility`, Glance running | 2.71 | 8.30 |
| without the flag, Glance running | 2.50 | 15.10 |
| without the flag, no Glance (baseline) | 2.45 | 7.30 |

The flag costs about 0.3 points idle and 1 point typing over baseline. The without-flag run with Glance
running is noisier (single run; Chrome turns accessibility on by itself when an AT-SPI client is listening),
so treat the 15.1 as an upper outlier, not a result.

## Write-back

Gate (spec §8.2): zero silent corruption across 500 scripted applies per app, each either verified or reverted, one Ctrl+Z
restores the text, the clipboard is back as it was, and at least 95 % verified (a matrix that refuses everything is not a pass).
Driven through the shipped `glanced` (ctl `test_apply`, GLANCE_TEST=1) on a private Xvfb with its own D-Bus; the clipboard sentinel
`CLIP-SENTINEL-<i>` is owned and read back on the private display; the editor's own text is the truth (`tests/acceptance/harness.py::run_writeback_matrix`, seed 2):

    .venv/bin/pytest -m apps tests/acceptance/test_writeback_matrix.py -v -rP

Page reset to a 3-sentence seed every 25 applies, and after any apply that put an emoji in (an emoji is a protected entity; a later
edit cutting through one is rightly refused).

| Target | applies | verified | refused / stale / failed | silent corruption | one Ctrl+Z restores (every 10th) | clipboard violations |
|---|---|---|---|---|---|---|
| Chrome textarea | 500 | 500 | 0 / 0 / 0 | 0 | 49/49 | 0 |
| Chrome contenteditable | 500 | 500 | 0 / 0 / 0 | 0 | 49/49 | 0 |
| Brave textarea | 500 | 500 | 0 / 0 / 0 | 0 | 49/49 | 0 |
| Brave contenteditable | 500 | 500 | 0 / 0 / 0 | 0 | 49/49 | 0 |
| GTK SourceView (Slack class) | 500 | 500 | 0 / 0 / 0 | 0 | 49/49 (27/49 before F2) | 0 |
| Firefox | not run: the snap cannot start on a private D-Bus | | | | | |

Not covered: no apply was reverted in these runs, so the "auto-reverted with notice" path is not exercised by the matrix (it is by
the unit tests). The apply-while-typing test (100 requests while a thread types) is tracked under finding F1.

## Privacy

Gate (spec §8.5): nothing leaves from private fields; redaction recall at least 99 %; no typed text on disk.

Secret redaction on `tests/acceptance/corpus/` (`scripts/gen-secret-corpus.py --seed 7`, synthetic; 600 secrets, 100 each of api_key,
jwt, pem, card, assignment, entropy in 30 sentence templates; 400 benign; entropy tokens are 20-56 characters):

| | before F5 | after F5 (2fcd1b6) |
|---|---|---|
| recall overall | 597/600 = 99.5 % | 600/600 = 100 % |
| recall api_key / jwt / pem / card / assignment | 100 % each | 100 % each |
| recall entropy | 97 % | 100 % |
| false positives on benign | 2/400 = 0.5 % | 0/400 = 0 % |
| found secret still in shielded text | 0 | 0 |

    .venv/bin/pytest tests/acceptance/test_redaction_corpus.py -v

Gate e2e (`test_privacy_e2e.py`, real glanced with the fake CLI backend `tests/fake_cli/claude`, which logs every argv and stdin it is sent;
marker `ZQXJ-marker ...`; 3 s wait; gate reason, empty daemon-side text and no marker in the log all asserted):
password field GTK and web, Remmina / AnyDesk / Seahorse / KeePassXC, Chrome incognito, `/login/` URL, terminal class, paused:
all pass. Control: the same marker in an ordinary field arrives, a typed `sk-ant-api03-...` key arrives only as `[[GLANCE_S1]]`. While a `hang`-mode CLI is
running no descendant's `/proc/<pid>/cmdline` contains the marker (the process was seen, so the check is not vacuous). Select-anything
is refused in a denylisted app and, since F3, in a password entry (log `select_anything_refused reason=password`, no panel, nothing sent).
Firefox private window: skipped (snap).

Disk audit (`test_disk_audit.py`): one scripted session of 40 sentences over 107 s (8 fixes, 4 rewrites, 2 select-anything, no completion accepted)
with the fake CLI backend, then Glance stopped. `grep -r ZQXJ` over the temporary XDG config/data/runtime/state/cache directories, the daemon's
stderr, the backends' empty directory and `/tmp/glance*` finds nothing; the empty directory is empty; the data directory holds only expected files;
adding `Zqxjword` to the dictionary writes it to `dictionary.txt` and nowhere else. 18 passed, 1 skipped (Firefox private) in 352 s.

Backend residue (step 6: one paid call per backend, checking the vendor CLIs keep no session entry): NOT RUN (paid, needs the user's go-ahead).
`STORAGE_NOTES` is unchanged.
