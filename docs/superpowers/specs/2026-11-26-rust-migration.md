# Glance in Rust — migration plan

Status: Phase 1 implemented on branch `rust-core` (this document records its results). Later
phases are planned, not started. Companion to [2026-11-25-glance-design.md](2026-11-25-glance-design.md),
[2026-11-26-glance-macos-design.md](2026-11-26-glance-macos-design.md) and
[2026-11-26-glance-wayland-design.md](2026-11-26-glance-wayland-design.md): every contract
those documents state (controller, grammar pipeline, privacy, write-back with verify + one
undo step, the ports) holds through every phase. The migration changes the language behind
the ports, never what crosses them.

## Why, and the rules of the road

Today Glance is ~24 k lines of Python (3.10–3.14) plus a separate `harper-ls` process for
grammar. The costs that motivate Rust are concrete:

- **Grammar runs out of process.** `harper-ls` 3.3.0 over LSP/stdio: ~165–205 MB RSS of its
  own, a supervisor with respawn logic, a 0.5 s + 2 s timeout dance, and LSP quirks we work
  around (UTF-16 columns, one code-action request per diagnostic, each re-linting the whole
  text — 0.16 s for a clean 600-word text, 2.1 s for a typo-dense one).
- **macOS permissions attach to the binary that asks.** Accessibility is granted to
  `python3.x`, not "Glance"; a Python upgrade silently revokes it.
- **Distribution** needs a system Python with GTK/PyObjC bindings, a venv, and a fetched binary.

Rules for every phase:

1. **Strangler, not rewrite.** Each phase replaces one piece behind an interface that already
   exists in Python; the rest of the program cannot tell. Python stays the integration layer
   until Phase 5.
2. **The existing tests are the spec.** A phase is done when the Python suite passes
   unchanged against the Rust implementation (plus the phase's own parity tests). Tests
   change only where they reach into a private detail of the piece being replaced.
3. **Always a fallback until the phase after.** The Python (or harper-ls) path stays
   selectable for one release after its Rust replacement ships, then is deleted.
4. **Users never need Rust.** Prebuilt, SHA-256-pinned artifacts only; building from source
   is an opt-in (`--with-rust`).
5. **No panic reaches the user.** Every FFI entry point runs under `catch_unwind`; a panic is
   a Python exception, logged, and treated as "no result" for that call.

## Phase 1 — grammar in process (`glance_core`, PyO3) — done

**Scope.** A Cargo workspace at `rust/` with one crate, `rust/glance_core`: a PyO3 (abi3,
CPython ≥ 3.10) extension built with maturin, linking `harper-core` **pinned to the git tag
`v3.3.0`** (crates.io only has 3.2.0; the tag is the exact engine of the harper-ls 3.3.0
release Glance shipped), default features as harper-ls builds it.

**Interfaces.**

- Rust: `glance_core.Grammar(dialect="American", disabled_rules=(), enabled_rules=(),
  words=(), dictionary=None)`; `check(text) -> list[dict]` with `rule` (Harper's rule key,
  the same string harper-ls sent as the diagnostic `code`), `kind`, `message`, `start`/`end`,
  `priority`, `suggestions` (the lint's own, Harper's order) and `replacements` (what
  harper-ls offered as code actions — see below); `add_word`, `load_dictionary(path)`,
  `set_words`, `set_disabled_rules`, `rules()`; `GrammarPanic(RuntimeError)`.
  Offsets are **Python `str` indices**: Harper's spans are Unicode-scalar (`char`) indices,
  which is exactly what CPython indexes, so no UTF-8/UTF-16 mapping exists on this path.
  (`Issue` uses code-point offsets — `issues.py` header — confirmed.)
- Every call releases the GIL (`py.detach`), takes the engine's mutex (one `Grammar` is
  safe to share between threads) and runs under `catch_unwind`; after a panic the engine
  rebuilds itself (its caches may be half-written) and the call raises `GrammarPanic`.
- Python: `glance.grammar.harper_native.NativeHarperClient` has `HarperClient`'s interface
  (`start`/`stop`/`alive`/`degraded`/`check`/`set_disabled_rules`/`reload_dictionary`), so
  `SupervisedHarper` and the `ChildSupervisor` wrap it unchanged (it simply never dies).
  `make_client()` picks it when `glance_core` imports, unless `GLANCE_GRAMMAR=harper-ls`.
  Lints from **both** engines become `Issue`s through one function,
  `issues.harper_issues` (rule-key fallback to `SpellCheck` when the key is also a
  replacement — Harper names some rules after a word, e.g. `The` for "Teh" — dedup, `safe`),
  so the filter, `corrections.py` and the strip behave identically.
- The personal dictionary (store.py's file) is loaded at start and re-read when its
  mtime/size changes (a `stat` per check), so an added word stops being flagged by Harper
  itself; harper-ls only read it at restart.
- `start()` runs one throwaway check on a nonsense word: Harper lazily builds a ~40 MB
  lookup structure the first time it meets a word far from every dictionary word, which cost
  the first such check ~1 s. Paid on the start thread now, never by a check.

**What harper-ls did that is reproduced exactly** (`rust/glance_core/src/engine.rs`):
`LintGroup::new_curated(curated FST dict ⊕ personal words tagged with the dialect)`,
Glance's rule overrides on top of the curated config, `Document::new(text, &PlainEnglish)`,
no lint above 120 000 bytes, `organized_lints` → `remove_overlaps_map` (rule-key order), and
— the subtle one — **replacements are the union of the suggestions of every lint overlapping
the diagnostic's first character** (before overlap removal, by priority), because that is
what harper-ls's code-action handler returns and what Glance's client collected. A word that
is both SpellCheck and SplitWords therefore offers both sets, which `confident_spelling`
relies on ("likethe" → "like the").

**Deliberately not reproduced** (host state glanced never asked for): harper-ls's own
`~/.config/harper-ls/dictionary.txt` (it used it whenever Glance's dictionary file did not
exist), the `.harper-dictionary.txt` of glanced's working directory, per-file dictionaries,
ignored-lint stores, stats.

**Parity** (`tests/test_grammar_parity.py`, real harper-ls 3.3.0 with HOME/XDG in a temp
dir vs `glance_core`, Issue for Issue: span, rule, message, replacements, `safe`): **74
texts** (the grammar sentences of the existing tests, the user-report fixture, and varied
texts: Banglish, Bangla script, code, URLs, e-mail, emoji and other non-BMP, accents,
multi-line, all caps, identifiers), **117 harper-ls issues: 73 texts identical, 1
explained difference, 0 unexplained.** The difference is a harper-ls bug: its code-action
handler reads the range's UTF-16 column as a char index, so on a line with non-BMP
characters before a lint it looks up suggestions further right (`"𝓗𝓮𝓵𝓵𝓸 wrold"`: harper-ls
offers nothing for "wrold", native offers world/wrote/wrong). The recorded
`user_report_ai_sentence.json` output is matched exactly. No difference is due to engine
version (same tag).

**Measured** (Linux x86_64, release build, a process with glanced's imports and GTK loaded
but no display; "first" includes an unfamiliar word):

| | harper-ls (glanced + child) | native (glanced) |
|---|---|---|
| RSS, after use | 45.5 + 205.0 = **250.5 MB** | **238.2 MB** |
| start | 0.70 s (spawn + LSP init) | 0.53 s build + 1.2 s warm-up (start thread) |
| 50 words, first check | 988 ms | 20 ms |
| 50 words, warm | 21 ms (dense) / 5 ms (clean) | 1.6 ms / 2.2 ms |
| 600 words, warm, clean prose (10 issues) | 164 ms | 15 ms |
| 600 words, warm, typo-dense (~240 issues) | 2.1–2.3 s (over the 0.5 s budget) | 15 ms |

Memory: Harper's data (curated dictionary, ~200 rule automata, the lazily built ~40 MB
fuzzy-lookup structure) dominates; it now lives in glanced instead of a child, saving only
the child's runtime (~12 MB) — the win is latency, the stall, and the supervisor, not RSS.
`malloc_trim` after each engine build returns ~35 MB of build temporaries to the OS.
Without Harper's optional `thesaurus` and `context-ranking` features: see "Phase 1 notes".
The extension is a 17 MB `.so` (abi3, stripped, fat LTO); harper-ls was 68 MB.

**Distribution.** `.github/workflows/core-wheels.yml` builds abi3 wheels for manylinux
x86_64/aarch64 and macOS arm64/x86_64 on a `core-v*` tag, attaches them and `SHA256SUMS`
to the release, and opens a PR filling `scripts/core-wheels.sha256`. `scripts/fetch-core.sh`
(called by `install.sh` and `scripts/install-macos.sh` after harper-ls) installs the
pinned wheel for the machine if there is one and its SHA-256 matches; `--with-rust` builds
from `rust/` with cargo instead; otherwise nothing happens and glanced runs harper-ls.
**Decision:** source builds are opt-in, never automatic when cargo exists (a release build
takes minutes and >1 GB of build cache; a user with cargo for other reasons should not pay
that silently). The pins file is empty until the first `core-v0.1.0` release, so today every
install falls back to harper-ls. harper-ls is still fetched on every install (the fallback).
`glance doctor` reports the engine in use (`grammar_engine` check), and `glance status`
carries `grammar_engine`.

**Tests.** `cargo test --no-default-features` (engine unit tests without linking Python);
`tests/test_harper_native.py` (selection, the client with a fake engine — panic, dictionary
reload, disabled rules — and the real extension: str offsets with non-BMP text, panic
recovery, words/dictionary, sharing one `Grammar` across threads);
`tests/test_grammar_parity.py`; `tests/test_fetch_core.py` (pinned/mismatched/absent wheel,
other platforms, `--with-rust` without cargo, shellcheck). CI builds the crate into the venv
(`maturin develop --release`) and runs the parity test with `GLANCE_REQUIRE_CORE=1` so a
broken build fails rather than skips.

**Risks.** (1) harper-core is a git dependency: a force-moved tag would change the engine —
`Cargo.lock` pins the commit (`8ad653e6`). (2) A Harper upgrade can change lints; parity
then compares against the matching harper-ls release, and differences are reviewed, not
waved through. (3) The engine is ~200 MB in glanced's address space: a leak there is now
glanced's leak (the acceptance resource test still watches glanced's RSS). (4) A panic is
caught, but an abort (OOM, stack overflow) is not; harper-ls isolated those.

**Done when** the default suite passes with `glance_core` installed, parity shows no
unexplained difference, and the installers fall back cleanly without a pinned wheel. Met.

### Phase 1 notes

- `GLANCE_GRAMMAR=harper-ls` forces the old engine (for A/B and bug reports).
- `set_disabled_rules` is wired but glanced never calls it today (neither did it for
  harper-ls): the per-app ignored rules are filtered in Python after the check.
- Building harper-core without its default `thesaurus` and `context-ranking` features
  (measured, not adopted): RSS 234 MB instead of 238 MB and a 10 MB instead of 17 MB `.so`,
  but spelling suggestions are ranked differently (parity: 40 of 74 texts identical, e.g.
  "recieve" → receive/relieve/**recipe** instead of **retrieve**). Four megabytes do not buy
  a behaviour change; the crate keeps harper-ls's features.

## Phase 2 — a signed Rust launcher `.app` for macOS

**Scope.** A tiny Rust binary, `Glance.app/Contents/MacOS/Glance`, signed with a Developer
ID and notarized, that macOS sees as the process asking for Accessibility and Input
Monitoring. It embeds CPython (PyO3 `auto-initialize`, the venv's interpreter) and runs
`glance.platform.macos.daemon.main` *in its own process* — so the TCC grant attaches to
"Glance" (bundle id `io.github.btocode.glance`), survives Python upgrades, and shows the
right name and icon in System Settings. `glance-capture` runs as a second invocation of the
same binary (argv switch), so it shares the grant.

**Interfaces.** The LaunchAgent's `ProgramArguments` point at the `.app` binary instead of
`python -m glance...`. The binary: locate the venv (Info.plist key or
`~/Library/Application Support/Glance/venv`), set `PYTHONHOME`/`sys.path`, call the
existing `main()`; exit code passes through. No Python code changes beyond `paths.py`
learning the bundle location.

**Risks.** Embedding a uv-managed CPython (relocatable builds; `PYTHONHOME` must be exact);
code signing with hardened runtime needs `com.apple.security.cs.disable-library-validation`
(the venv's PyObjC and `glance_core` dylibs are not signed by us) — alternatively sign them
at install time, which needs the team's identity on the user's machine (no); notarization
in CI needs an Apple Developer account (secrets: certificate, app-specific password) —
**P2 cannot start without one**; existing users' TCC grants to python are orphaned once:
the installer must say so and open the right Settings pane.

**Tests.** Unit: argv dispatch and venv discovery (Rust tests). CI: build + `codesign
--verify --strict` + `spctl -a -t exec` on the notarized artifact. Manual on a Mac (the
`macos` marker suite, `GLANCE_MAC_TEST=1`): grant once, upgrade Python in the venv, the grant
still holds; `glance doctor` names "Glance" in the Accessibility check.

**Done when** a fresh install on macOS 13 and 15 asks for Accessibility for "Glance", the
grant survives a venv rebuild, and the macOS suite passes.

## Phase 3 — the display-free core moves to the crate, one module at a time

**Scope.** In this order (each a separate change, each shipping with its Python twin as
fallback for one release):

1. **privacy** — `privacy/entities.py` (`find_protected`, spans), `privacy/redact.py`,
   `privacy/gate.py`. Pure functions over strings; highest value (they run on every
   keystroke and guard what leaves the machine), smallest surface.
2. **grammar glue** — `grammar/filters.py`, `lang.py`, `corrections.py`, `issues.py`
   helpers; then the whole check pipeline (`text → filtered issues`) is one Rust call.
3. **scheduler/controller logic** — `daemon/scheduler.py`, the pure parts of
   `daemon/controller.py`, `ui/strip_model.py`, `ui/chip_model.py`, `diffing.py`: state
   machines driven through the `Clock` port. Python keeps the port adapters.
4. **backends** — `backends/*` (CLI/API backends, budget, usage ledger, supervisor):
   process spawning and HTTP (`reqwest` + rustls), the keystore via the OS keyring crates.
5. **config/usage** — `config.py` (TOML with `toml_edit`, preserving the user's comments),
   `backends/usage.py` files.

**Interfaces.** Each module keeps its Python signature; the Python file becomes a thin
re-export of `glance_core.<module>` with the old implementation behind
`GLANCE_PURE_PYTHON=1`. Data crosses as plain dicts/tuples or `#[pyclass]` frozen types that
mirror the dataclasses (`Issue`, `Span`, `Edit`).

**Risks.** Behavioural drift in regex-heavy code (Python `re` vs Rust `regex`: no
look-around, different `\w`/case folding) — every pattern gets a corpus parity test like
Phase 1's; object churn across the FFI (keep calls coarse: one call per check, not per
issue); the controller is under active development in Python — move it last and only once
its interface has been stable for a release.

**Tests.** The existing unit tests run against both implementations (a pytest fixture
parametrised over `GLANCE_PURE_PYTHON`), plus a property test per module (`proptest` on
the Rust side, Hypothesis against the Python twin) for offsets and redaction.

**Done when** each module's tests pass against Rust, the parity/property tests show no
difference, and the Python twin is deleted one release later.

## Phase 4 — per-platform capture, write-back and overlay in Rust

**Scope.**
- **macOS:** AX capture (`ax_port.py`, `ax_text.py`), CGEvent key synthesis, the
  clipboard, Carbon hotkeys, and the AppKit overlay/strip/tray via `objc2` +
  `objc2-app-kit`/`objc2-application-services` — inside the Phase 2 binary.
- **X11:** capture over AT-SPI with the `atspi` crate (zbus), window tracking and key
  synthesis with `x11rb` (XTest), clipboard; the overlay with GTK4-rs or a plain X11 window
  (decide by prototype: GTK3's input-shape tricks used today must be reproduced).
- **GNOME Wayland:** the GNOME Shell extension **stays JavaScript** (it must run inside
  gnome-shell); only its D-Bus peer (`shellbus.py`, `*_shell.py`) moves to `zbus`.

**Interfaces.** The existing ports (`CapturePort`, `WritebackPort`, `OverlayPort`,
`GhostPort`, `HotkeyPort`, `TrayPort`, `SelectionPort`, `NotifierPort`) become Rust traits
with the same methods; `glance-capture`'s JSON-lines socket protocol (`protocol.py`) is the
contract between processes and does not change in this phase.

**Risks.** The largest phase and the one most coupled to real desktops; AT-SPI quirks per
toolkit (Chrome, Electron, LibreOffice) are encoded in Python today and only partly in
tests — the `desktop`/`apps`/`xvfb`/`wayland` suites must be run against the Rust capture
before it becomes the default; write-back's verify + undo contract is the safety property
and gets its own Rust test harness on Xvfb.

**Tests.** The xvfb, wayland and macos marker suites, unchanged, against the Rust
processes; the acceptance latency/resource tests.

**Done when** those suites pass with the Rust processes on all three platforms.

## Phase 5 — one binary per platform, Python dropped

**Scope.** `glanced` and `glance-capture` (and the `glance` CLI, `glance-edit`) become one
Rust binary per platform with subcommands; the venv, the system-Python requirement and the
Python sources go. Releases ship a signed/notarized `.app` (macOS), and a tarball plus
`.deb` (Linux) with SHA-256 pins in the installer.

**Interfaces.** The CLI surface (`glance …` commands, flags, exit codes), the config file,
the data files (dictionary, ignored rules, usage), the socket protocol and the GNOME
extension's D-Bus interface stay byte-compatible, so an upgrade keeps the user's state.

**Risks.** The CLI and acceptance tests are Python today: keep them as black-box tests
driving the binary (pytest stays as the test runner, not as product code). Losing a
contributor-friendly language — mitigated by the strict layering the earlier phases leave.

**Tests.** The black-box CLI/acceptance suite against the binary; install/uninstall tests
(`test_install_scripts.py` style) against the new artifacts.

**Done when** a fresh machine installs, runs and uninstalls Glance with no Python present,
and every acceptance test passes.
