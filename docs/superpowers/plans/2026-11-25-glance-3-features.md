# Glance Plan 3 — Autocomplete, Send-guard, Sharpener, Select-anything, Ctrl+G Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the remaining v1 features on top of Plans 1–2: Banglish-aware grammar suppression, ghost-text autocomplete, send-guard, Prompt Sharpener, select-anything rewrite for apps without capture, and the Claude Code Ctrl+G flow (`glance-edit` + a `claude` zsh wrapper).

**Architecture:** Each feature is a small class under `src/glance/features/` that attaches to the Plan 1 `Controller` / Plan 2 `AiCoordinator` hooks and talks to the desktop only through ports. All AI calls go through `BackendSupervisor` and the Plan 2 shielding path (`shield` / `rewrite_text`). `glance-edit` is a tiny client: it forwards the prompt text to `glanced` over `ctl.sock`, and falls through to the user's real editor whenever anything is not right.

**Tech Stack:** unchanged from Plan 2 (system Python 3.10, GTK 3, python-xlib, Harper, httpx). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-25-glance-design.md`. Spike facts used: S5.2 (ghost font factor), S5.3 (free hotkeys), S7.1–S7.2 (Ctrl+G invocation and the "called by Claude Code" rule) in `docs/superpowers/spike/findings.md`.

**Plan sequence:** 0 spike → 1 foundation → 2 backends/tone → **3 features (this)** → 4 install/hardening. Uses unchanged: `Controller` (`current`, `generation`, `request_apply`, `extra_chip_inputs`, `extra_panel`), `AiCoordinator` (`analysis()`, `resolved()`, `language`), `BackendSupervisor`, `BaseBackend._run_stream`, `rewrite_text`, `RewriteOutcome`, `shield`, `IssueFilter`, `IgnoredRules`, `HotkeyPort`, `OverlayPort`, `WriteBackEngine`, `ChipInputs.guard`, `ChipState.GUARD`, `TONES`, `Profile.guard`.

## Global Constraints

- All Plan 1 and Plan 2 Global Constraints still apply (no typed text on disk/logs/argv, shield before any backend, apply only on an explicit key or click, focus-free overlays, budgets).
- Autocomplete: ghost text is grey, in a transparent click-through overlay right after the caret; Tab accepts all, Ctrl+→ accepts one word, typing anything else dismisses. Tab and Ctrl+→ are grabbed **only while a suggestion is visible** and released immediately after. Context sent = field text up to the caret, ≤ 2000 chars, plus the audience profile. Completions ≤ 1 sentence, same language and tone, cancelled the moment a key is pressed. On-demand = Super+Alt+Enter, every backend. Live = after ~600 ms pause, default on only for `api`; live calls count toward the daily cap. Off in password/denylisted contexts.
- Send-guard is non-blocking: an amber chip only. Glance never grabs, delays or swallows Enter.
- Banglish: detect non-English/Banglish spans per sentence and suppress local grammar/spelling flags there; LLM analysis still runs and is told the text may mix English and Banglish; rewrites preserve the user's language.
- `$EDITOR` is never changed globally. Only the `claude` zsh wrapper sets `EDITOR=glance-edit`, only for Claude Code. `glance-edit` invoked by anything else passes straight through to the real editor.
- Terminals get Ctrl+G and select-anything only; no live capture and no automatic paste into a terminal.
- Every warning can be silenced ("don't flag this again" per rule/app).

## Review Focus

1. Text selected earlier in a password manager is still the PRIMARY selection when the hotkey is pressed in Slack → the selection's *owner* window is gated, not just the active window, and a selection owned by another window is not used (Task 6, `test_selection_owned_by_denylisted_app_is_refused`, `test_selection_from_other_window_is_not_used`).
2. Technical English ("kubectl rollout restart the nginx ingress") or a message full of names is mistaken for Banglish and grammar silently goes quiet → jargon alone never flips a sentence (Task 1, `test_technical_english_is_not_banglish`).
3. `glanced` is not running, hangs, or dies while `glance-edit` waits → the user's real editor opens with their prompt intact; they are never stuck inside Ctrl+G (Task 7, `test_falls_through_when_daemon_down`, `test_falls_through_when_daemon_dies_mid_session`).
4. A suggestion stays on screen (daemon busy, missed event) with Tab still grabbed → the grab is released by a watchdog after 10 s at most, and on every hide path (Task 2, `test_grab_released_on_every_hide_path`, `test_watchdog_releases_grab`).
5. The user presses Tab on a suggestion computed for text that has since changed → nothing is inserted and the ghost disappears; text is never inserted at a stale position (Task 2, `test_stale_accept_inserts_nothing`).

## File Structure

```
src/glance/grammar/lang.py              sentence split, Banglish/non-English detection
src/glance/features/__init__.py
src/glance/features/autocomplete.py     Autocomplete engine + clean_completion
src/glance/features/sendguard.py        guard_reason, SendGuard
src/glance/features/sharpener.py        sharpen/add-structure prompts
src/glance/features/select_anything.py  SelectAnything
src/glance/features/edit_session.py     EditSession (Ctrl+G window logic)
src/glance/ui/ghost_gtk.py  selection_xlib.py  edit_window_gtk.py     (desktop)
src/glance/edit_shim/main.py            glance-edit
src/glance/integrations/claude_wrapper.py   zsh wrapper install/uninstall
```

---

### Task 1: Banglish / non-English span detection

**Files:**
- Create: `src/glance/grammar/lang.py`
- Modify: `src/glance/grammar/filters.py` (issue-aware span filters), `src/glance/daemon/main.py` (wire detector into `IssueFilter` and `AiCoordinator(language=…)`)
- Test: `tests/test_lang.py`

**Interfaces:**
- Consumes: `Issue`, `Span`, `IssueFilter`, `PersonalDictionary`.
- Produces (`lang.py`):
  - `split_sentences(text:str) -> list[Span]` (kind `sentence`; split on `.?!` followed by space/end, and on newlines; spans cover the text without the separators).
  - `BANGLISH_MARKERS:frozenset[str]` — at least these casefolded function words: `ami, amra, tumi, tomra, apni, tui, amar, tomar, apnar, amader, ki, keno, kothay, kobe, kivabe, kemon, eta, ota, oita, ekta, ekhon, ekhane, okhane, kintu, karon, tai, jodi, tahole, ache, achi, acho, achen, nai, nei, hobe, hoye, hoyeche, hocche, korbo, korbe, korte, korchi, korcho, korlam, koro, koren, kora, bolo, bolen, bolchi, jani, janina, lagbe, parbo, parbe, thik, bhalo, valo, khub, onek, ektu, aro, shob, kaj, bhai, apu, dhonnobad, accha, hae, na`. Words that are also common English (`na` excluded from the count when the sentence has no other marker; `ki`, `tai`, `kora`, `ache` count only with a second marker).
  - `sentence_is_non_english(sentence:str, misspelled:int, words:int) -> bool` — true when any character is in U+0980–U+09FF; or strong-marker count ≥ 2; or strong-marker count ≥ 1 and `misspelled / words >= 0.25`; or `words >= 4` and `misspelled / words >= 0.6` and marker count ≥ 1. Never true on misspelling ratio alone.
  - `class LanguageDetector(dictionary:PersonalDictionary)`: `spans(text:str, issues:list[Issue]) -> list[Span]` (kind `lang`; `misspelled` = count of `SpellCheck` issues inside the sentence whose word is not in the dictionary), `is_mixed(text:str) -> bool` (any non-English sentence by markers/script alone — usable without Harper results).
- Produces (`filters.py`): `IssueFilter.__init__(…, issue_span_filters:list[Callable[[str,list[Issue]],list[Span]]]=())`; `apply` drops any issue with `source == "harper"` inside those spans. LLM issues are not dropped.

- [ ] **Step 1: Write the failing tests**

```python
def test_split_sentences():
    t = "Hi there. ami kal ashbo\nok?"; assert [t[s.start:s.end] for s in split_sentences(t)] == ["Hi there", "ami kal ashbo", "ok"]

@pytest.mark.parametrize("sentence, misspelled, words, want", [
    ("ami kal office e jabo na", 4, 6, True),
    ("tumi ki korcho ekhon", 3, 4, True),
    ("bhai eta thik ache", 2, 4, True),
    ("আমি ভালো আছি", 0, 3, True),
    ("I will send the report tomorrow", 0, 6, False),
    ("pls chk teh reprot asap thx", 5, 6, False),                       # sloppy English, no markers
    ("kubectl rollout restart the nginx ingress", 4, 6, False),         # jargon, no markers
    ("Zebulon and Rahim met Tanvir at Dhanmondi", 4, 7, False),           # names
    ("na I think its fine", 0, 5, False)])                              # 'na' alone
def test_sentence_classification(sentence, misspelled, words, want):
    assert sentence_is_non_english(sentence, misspelled, words) is want

def test_technical_english_is_not_banglish():
    t = "kubectl rollout restart the nginx ingress. Then chek teh logs."
    assert LanguageDetector(dic).spans(t, spell_issues_for(t, ["kubectl", "nginx", "chek", "teh"])) == []

def test_mixed_message_suppresses_only_banglish_sentence():
    t = "ami kal ashbo na. I will sned the file."
    out = IssueFilter(dic, ign, [], issue_span_filters=[LanguageDetector(dic).spans]).apply(t, spell_issues_for(t, ["ami", "kal", "ashbo", "sned"]), "slack")
    assert [t[i.start:i.end] for i in out] == ["sned"]

def test_llm_issues_survive_in_banglish_span():
def test_is_mixed_without_harper():
    d = LanguageDetector(dic); assert d.is_mixed("ok. ami tomar sathe kotha bolbo") and not d.is_mixed("ok. see you tomorrow")
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_lang.py -v` — FAIL.
- [ ] **Step 3: Implement**; wire in `daemon/main.py`: `IssueFilter(…, issue_span_filters=[detector.spans])`, `AiCoordinator(…, language=detector.is_mixed)`.
- [ ] **Step 4: Run** — PASS; `.venv/bin/pytest` — PASS.
- [ ] **Step 5: Commit** `feat: banglish-aware grammar suppression`.

---

### Task 2: Autocomplete engine

**Files:**
- Create: `src/glance/features/__init__.py`, `src/glance/features/autocomplete.py`
- Modify: `src/glance/backends/base.py` (`BaseBackend.complete`), `src/glance/backends/prompts.py` (`complete_system`), `src/glance/backends/supervisor.py` (`complete`, `live_autocomplete`), `src/glance/ui/ports.py` (`GhostPort`), `src/glance/config.py` (hotkey `complete = "Super+Alt+Return"`, `GeneralCfg.autocomplete_idle_ms = 600`, `UiCfg.ghost_font_factor = 0.75`), `tests/fakes.py` (`FakeGhost`, `FakeHotkeys`)
- Test: `tests/test_autocomplete.py`

**Interfaces:**
- Consumes: `Controller`, `BackendSupervisor`, `AudienceResolver`, `shield`, `HotkeyPort`, `Clock`, `Edit`, `ApplyResult`.
- Produces:
  - `prompts.complete_system(ctx:Ctx) -> str` — continue the text in `<glance_text>` from exactly where it stops; output only the continuation (no repetition of the given text), at most one sentence, same language (Banglish stays Banglish when `mixed_language`) and tone as `ctx.target_tone`; empty output if no natural continuation.
  - `BaseBackend.complete(prefix:str, ctx:Ctx) -> Iterator[str]` — `_run_stream(complete_system(ctx), user_block(prefix), 8.0)`.
  - `BackendSupervisor.complete(prefix, ctx) -> Iterator[str]` (same budget/breaker/fallback path as `rewrite`); `live_autocomplete() -> bool` — `cfg.live_autocomplete` if set, else `active() == "api"`.
  - `class GhostPort(Protocol): show(text:str, x:int, y:int, font_px:int, max_width:int) -> None; hide() -> None`
  - `clean_completion(raw:str, prefix:str) -> str|None` — drops an echoed copy of the prefix tail, any `[[GLANCE_` placeholder (→ `None`), surrounding quotes/fences; cuts at the first newline and after the first sentence terminator; max 200 chars; guarantees exactly one space between `prefix` and the completion when the prefix ends in a word character and the completion starts with one, and no leading space when the prefix already ends in whitespace; `None` when empty.
  - `class Autocomplete(controller, supervisor, resolver, ghost:GhostPort, hotkeys:HotkeyPort, clock, run_bg, cfg:Callable[[],Config], language:Callable[[str],bool])` with `attach()`, `trigger()` (on-demand hotkey), `on_snapshot(s)`, `on_focus_lost()`, `accept_all()`, `accept_word()`, `dismiss()`, `visible:bool`. Rules:
    - Eligible field: not gated, not paused, `caret_rect` present, caret at end of text or immediately before `\n`, prefix ≥ 15 chars.
    - Live: `autocomplete_idle_ms` after the last text change, only when `supervisor.live_autocomplete()` and `supervisor.auto_allowed()`. On-demand: `trigger()` on any backend, needs only an eligible field.
    - Request: `prefix = shield(text[:caret][-2000:]).text`; result accepted only if `field_id`, `generation` and caret are unchanged.
    - Show: `ghost.show(completion, caret_rect.x + caret_rect.w, caret_rect.y, int(caret_rect.h * ghost_font_factor), field_rect.right − caret_rect.x)`; then grab `ac_tab = "Tab"` and `ac_word = "Control+Right"`; start a 10 000 ms watchdog that calls `dismiss()`.
    - Any snapshot whose text or caret differs from the one the suggestion was computed for, any focus loss, gate, pause → `dismiss()` (hide, ungrab both, cancel the in-flight call, cancel the watchdog). Exception: the snapshot that results from `accept_word` and equals the expected text keeps the remainder visible, re-anchored at the new caret.
    - `accept_all()` → `controller.request_apply(Edit(caret, caret, completion), "auto")` with the hash of the suggestion's snapshot, then `dismiss()`. `accept_word()` → same with the first word (leading space included); the remainder is retained pending the confirming snapshot. A `stale`/`refused`/`failed` result → `dismiss()`, nothing re-tried.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("raw, prefix, want", [
    ("tomorrow morning.", "I will send it", " tomorrow morning."),
    (" tomorrow morning.", "I will send it ", "tomorrow morning."),
    ("I will send it tomorrow.", "I will send it", " tomorrow."),               # echoed prefix
    ("tomorrow. Then we can talk.", "see you", " tomorrow."),                    # one sentence
    ("first line\nsecond", "ok", " first line"),
    ('"by Friday"', "done", " by Friday"),
    ("use [[GLANCE_S1]] here", "the key is", None), ("   ", "x", None)])
def test_clean_completion(raw, prefix, want): assert clean_completion(raw, prefix) == want

def test_on_demand_shows_ghost_and_grabs_keys():
    ac.trigger(); backend.finish_complete(["tomorrow ", "morning."])
    assert ghost.shown.text == " tomorrow morning." and set(hotkeys.grabbed()) >= {"ac_tab", "ac_word"}
def test_no_grab_when_nothing_visible():
    assert "ac_tab" not in hotkeys.grabbed()                 # before trigger, and after a None completion
def test_typing_dismisses_and_ungrabs_and_cancels():
    # visible; snapshot with one more char -> ghost.shown is None; grabs gone; while pending: supervisor.cancel_all called
def test_grab_released_on_every_hide_path():
    for path in (lambda: ac.dismiss(), lambda: ac.on_focus_lost(), lambda: c.toggle_pause(), lambda: c.handle_snapshot(snap(gated="password", text="")),
                 lambda: ac.accept_all(), lambda: c.handle_capture_disconnected()):
        show_suggestion(); path(); assert not ac.visible and "ac_tab" not in hotkeys.grabbed() and "ac_word" not in hotkeys.grabbed()
def test_watchdog_releases_grab():
    show_suggestion(); clock.advance(10_000); assert not ac.visible and "ac_tab" not in hotkeys.grabbed()
def test_accept_all_inserts_at_caret():
    show_suggestion(text="I will send it", completion=" tomorrow."); ac.accept_all()
    assert capture.applies[-1].edit == Edit(14, 14, " tomorrow.")
def test_stale_accept_inserts_nothing():
    show_suggestion(); ac.accept_all(); c.handle_apply_result(ApplyResult(capture.applies[-1].apply_id, "stale", ""))
    assert not ac.visible and len(capture.applies) == 1
def test_accept_word_keeps_remainder():
    show_suggestion(text="see you", completion=" tomorrow morning."); ac.accept_word()
    assert capture.applies[-1].edit == Edit(7, 7, " tomorrow")
    c.handle_snapshot(snap(text="see you tomorrow", caret=16)); assert ghost.shown.text == " morning." and "ac_tab" in hotkeys.grabbed()
def test_live_only_when_enabled():       # choice claude: advance(600) -> 0 complete calls; choice api -> 1; cfg.live_autocomplete=True + claude -> 1
def test_live_respects_auto_allowed():   # limit / battery -> 0 calls; trigger() still calls
def test_not_eligible_mid_line_or_short_or_no_caret_rect():
def test_context_is_shielded_and_capped_at_2000():
    # 5000-char text with a secret near the end -> backend prefix len <= 2000 + placeholder growth, contains [[GLANCE_S1]], not the secret
def test_late_completion_for_old_generation_dropped():
def test_gated_field_never_completes():
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_autocomplete.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: autocomplete engine with transient key grabs`.

---

### Task 3: Ghost overlay and real key grabs

**Files:**
- Create: `src/glance/ui/ghost_gtk.py`
- Modify: `src/glance/ui/hotkeys_xlib.py` (plain-key and Control chords; `ungrab` flushes synchronously), `src/glance/daemon/main.py` (wire `Autocomplete`, hotkey `complete`)
- Test: `tests/desktop/test_ghost.py`

**Interfaces:**
- Consumes: `GhostPort`, `Autocomplete`, `XlibHotkeys`.
- Produces: `class GtkGhost` implementing `GhostPort` — POPUP window, RGBA, fully transparent background, empty input shape (`input_shape_combine_region(cairo.Region())`) so clicks pass through, `set_accept_focus(False)`; draws the text in 50 % grey at `font_px`, baseline aligned to the caret rect bottom, ellipsised at `max_width` and clamped to the caret's monitor.

- [ ] **Step 1: Write the desktop tests (`@pytest.mark.desktop`)** against `gtk_app.py` and `glanced` with `GLANCE_FAKE_BACKEND=1` (fake completion `" tomorrow morning."`):
  - `test_tab_untouched_when_no_suggestion` — 200 XTEST Tab presses in the `Gtk.TextView`; the buffer gains exactly 200 `\t` (zero stolen keystrokes).
  - `test_tab_accepts_suggestion` — type `I will send it`, press Super+Alt+Return, wait for `status()["autocomplete_visible"]`, press Tab → buffer is `I will send it tomorrow morning.` and contains no `\t`.
  - `test_tab_normal_again_after_accept` — next Tab inserts `\t`.
  - `test_ctrl_right_accepts_one_word` — buffer `I will send it tomorrow`; ghost still visible.
  - `test_typing_dismisses` — type `x` → not visible within 200 ms; Tab inserts `\t`.
  - `test_ghost_is_click_through_and_unfocused` — XTEST click at the ghost's position moves the TextView caret; active window stays `glance-test-app`.
- [ ] **Step 2: Run** `.venv/bin/pytest -m desktop tests/desktop/test_ghost.py -v` — FAIL.
- [ ] **Step 3: Implement** `ghost_gtk.py`, the hotkey changes, add `autocomplete_visible` to `Controller.status()` through the Plan 1 `controller.extra_status` list, wire in `daemon/main.py`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: ghost-text overlay and tab/ctrl-right grabs`.

---

### Task 4: Send-guard

**Files:**
- Create: `src/glance/features/sendguard.py`
- Modify: `src/glance/daemon/main.py`, `src/glance/ui/overlay_gtk.py` (amber GUARD chip; panel banner with "Don't warn about this here")
- Test: `tests/test_sendguard.py`

**Interfaces:**
- Consumes: `Analysis.flags`, `Profile.guard`, `IgnoredRules`, `find_protected`, `Controller.extra_chip_inputs`, `AiCoordinator.analysis()`/`resolved()`.
- Produces:
  - `GUARD_LABELS = {"hostile": "Sounds hostile", "missing_attachment": "Attachment mentioned — attached?", "missing_link": "Link mentioned — none here"}`
  - `local_guard(text:str) -> str|None` — `missing_link` when the text matches `\b(here'?s|see|check|at) (the|this|my) link\b|\blink (below|attached|here)\b` and contains no `url` span and no U+FFFC; `missing_attachment` when it matches `\b(attached|attaching|attachment|see the (screenshot|file|doc|pdf)|i'?ve attached)\b`, contains no U+FFFC, and no `url` span. (An attachment that lives outside the text box cannot be seen; the label is therefore a question, and the user can silence it per app.)
  - `guard_reason(text:str, analysis:Analysis|None, profile:Profile, ignored:IgnoredRules, wm_class:str) -> str|None` — `None` unless `profile.guard`; priority `hostile` (from `analysis.flags`) → `missing_attachment` → `missing_link` (local first, then flags); a reason whose rule `guard:<reason>` is ignored for the app or globally is skipped.
  - `class SendGuard(controller, ai:AiCoordinator, ignored)` with `attach()` — contributes `{"guard": GUARD_LABELS[reason]}` to chip inputs at settle time (local reasons need no AI and appear with the chip; `hostile` appears when the analysis lands); `silence(scope:str)` → `ignored.ignore(f"guard:{reason}", wm_class or None)`.
  - No hotkey or key grab is registered by this module.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text, want", [
    ("I've attached the report for you", "missing_attachment"),
    ("see the screenshot", "missing_attachment"),
    ("I've attached the report ￼", None),
    ("attached: https://x.test/report.pdf", None),
    ("here's the link", "missing_link"), ("here's the link https://x.test", None),
    ("she is very attached to her dog", "missing_attachment"),        # known false positive -> must be silenceable (next test)
    ("sounds good, thanks", None)])
def test_local_guard(text, want): assert local_guard(text) == want

def test_silenced_reason_is_skipped(tmp_path):
    ign.ignore("guard:missing_attachment", "slack")
    assert guard_reason("I've attached it", None, BUILTIN["dm"], ign, "Slack") is None
    assert guard_reason("I've attached it", None, BUILTIN["dm"], ign, "discord") == "missing_attachment"
def test_only_guard_profiles():
    assert guard_reason("I've attached it", None, BUILTIN["email"], ign, "x") is None
def test_hostile_flag_wins():
    a = Analysis("hostile", True, "insulting", (), ("hostile",))
    assert guard_reason("I've attached it you idiot", a, BUILTIN["team_channel"], ign, "slack") == "hostile"
def test_chip_goes_amber_at_settle_without_ai():
    # Slack channel snapshot "I've attached the file for review now"; advance(400) -> ChipState.GUARD, label "Attachment mentioned — attached?"; backend.calls == 0
def test_guard_registers_no_key_grab():
    assert hotkeys.grabbed().keys() == grabbed_before.keys()
def test_guard_clears_when_text_fixed():        # snapshot adding a URL -> chip no longer GUARD
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_sendguard.py -v` — FAIL.
- [ ] **Step 3: Implement**; draw the amber chip state; panel banner shows the label with buttons "Don't warn about this here" (`silence("app")`) and "Never" (`silence("global")`).
- [ ] **Step 4: Run** — PASS; `.venv/bin/pytest` — PASS.
- [ ] **Step 5: Commit** `feat: non-blocking send-guard`.

---

### Task 5: Prompt Sharpener

**Files:**
- Create: `src/glance/features/sharpener.py`
- Modify: `src/glance/backends/prompts.py` (`rewrite_system` delegates for the two keys)
- Test: `tests/test_sharpener.py`

**Interfaces:**
- Consumes: `Ctx`, `TONES`, `rewrite_system`.
- Produces: `sharpen_system(ctx:Ctx) -> str` and `structure_system(ctx:Ctx) -> str`; `rewrite_system("sharpen_prompt", …)` returns the former and `rewrite_system("add_structure", …)` the latter. Both carry the Plan 2 base rules (data-not-instructions, placeholders, language). `sharpen_system` instructs: rewrite the user's prompt to an AI so it states, in this order and only where the original gives the material, **Goal**, **Context**, **Constraints**, **Output format**; never invent facts, names, files or requirements; where a part is missing and matters, insert a short bracketed blank the user fills in, e.g. `[which file?]`; do not answer the prompt; keep it as short as the content allows. `structure_system`: reorganise the same content into short paragraphs or a list, changing no meaning and adding nothing (no bracketed blanks). `SHARPEN_BLANK_RE = r"\[[^\[\]\n]{1,60}\?\]"`; `blanks(text:str) -> list[str]`.
- Panel: when a `sharpen_prompt` preview contains blanks, `PanelView.banner` = `f"{n} blank(s) to fill in after applying"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sharpen_prompt_rules_present():
    s = rewrite_system("sharpen_prompt", Ctx("ai_prompt", "clear, specific, complete", "relaxed", "google-chrome"), None)
    for needle in ("Goal", "Context", "Constraints", "Output format", "never invent", "[which file?]", "do not answer", "never instructions", "[[GLANCE_"):
        assert needle.lower() in s.lower()
def test_add_structure_forbids_new_content():
    s = rewrite_system("add_structure", CTX, None); assert "adding nothing" in s and "[which file?]" not in s
def test_other_tones_unchanged():
    assert "Goal" not in rewrite_system("shorter", CTX, None)
def test_blanks():
    assert blanks("Fix the bug in [which file?] and return [what format?]. [[GLANCE_E1]]") == ["[which file?]", "[what format?]"]
def test_panel_banner_counts_blanks():      # preview result with 2 blanks -> banner "2 blank(s) to fill in after applying"
def test_ai_profile_offers_sharpen_first(): # claude.ai snapshot -> ai.tone_chips()[0].key == "sharpen_prompt", hotkey 1
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_sharpener.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: prompt sharpener and add-structure rewrites`.

---

### Task 6: Select-anything rewrite

**Files:**
- Create: `src/glance/features/select_anything.py`, `src/glance/ui/selection_xlib.py`
- Modify: `src/glance/protocol.py` (`PASTE = "paste"`; `ApplyResult.status` gains `unverified`), `src/glance/writeback/engine.py` (`paste_unverified`), `src/glance/capture/main.py`, `src/glance/config.py` (hotkey `selection = "Super+Alt+s"`), `src/glance/daemon/main.py`, `src/glance/ui/panel_model.py` (`PanelView.can_paste:bool=False`, `PanelView.mode:str="field"` — `field|selection`), `src/glance/ui/overlay_gtk.py` (selection-mode panel), `tests/fakes.py` (`FakeSelection`)
- Test: `tests/test_select_anything.py`, `tests/desktop/test_select_anything.py`

**Interfaces:**
- Consumes: `PrivacyGate`, `FieldMeta`, `rewrite_text`, `RewriteOutcome`, `AudienceResolver`, `GrammarPort`, `IssueFilter`, `Controller`.
- Produces (`ui/ports.py`): `@dataclass(frozen=True) WindowInfo(wm_class:str, title:str, pid:int, xid:int, rect:Rect|None)`; `class SelectionPort(Protocol): primary_text(max_bytes:int) -> str|None; owner() -> WindowInfo|None; active() -> WindowInfo|None; pointer() -> tuple[int,int]`. `selection_xlib.py`: `XlibSelection` — `owner()` resolves `XGetSelectionOwner(PRIMARY)` to its top-level client window (walk up to the window with `WM_CLASS`/`_NET_WM_PID`).
- Produces (`engine.py`): `WriteBackEngine.paste_unverified(text:str) -> ApplyResult` — clipboard save (refuse if not restorable) → `set_secret_text` → `chord("ctrl","v")` → restore; status `unverified`. Never called for terminal windows.
- Produces (`select_anything.py`): `class SelectAnything(controller, selection:SelectionPort, gate:Callable[[],PrivacyGate], cfg, grammar, issue_filter, supervisor, resolver, overlay, capture, notifier, run_bg)` with `trigger()`, `preview(tone, custom=None)`, `copy()`, `paste_over()`, `close()`. Rules:
  - Refuse with a one-line notification when: no PRIMARY text or it is whitespace ("Select some text first"); longer than `max_field_chars`; `owner()` is `None` or `owner().xid != active().xid` ("Select text in this window first"); the owner/active window is denylisted or incognito by `gate.evaluate` using a `FieldMeta` built with `role="selection"`, `states={"editable"}`, `url="selection:"` so the browser `url_unknown` rule does not block it while `app_denylist` and `incognito` still do. Terminals are allowed (explicit user action).
  - If the controller has a live, ungated snapshot for the active window whose text contains the selection exactly once, the request is handed to the normal verified path: `controller.request_apply(Edit(start, end, rewritten))`.
  - Otherwise selection mode: panel at the pointer shows Harper issues for the selection (read-only list), tone chips from `resolver` (profile from wm_class/title only), preview via `rewrite_text`. Default action (Enter / primary button) is **Copy rewrite**. Secondary button **Paste over selection** (hidden for terminal wm_classes) sends `PASTE` to capture; result `unverified` shows the hint "Pasted — Ctrl+Z in the app undoes it". Glance's own revert hotkey does nothing for unverified pastes.
  - The selection text is held in memory only for the life of the panel.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_selection_notifies():                     # primary_text None -> notifier "Select some text first"; overlay.panel is None
def test_selection_owned_by_denylisted_app_is_refused():
    sel.set(text="hunter2 master password", owner=win("keepassxc", xid=7), active=win("keepassxc", xid=7))
    sa.trigger(); assert overlay.panel is None and backend.calls == [] and harper.calls == []
def test_selection_from_other_window_is_not_used():
    sel.set(text="hunter2", owner=win("keepassxc", xid=7), active=win("Slack", xid=9))
    sa.trigger(); assert overlay.panel is None and notifier.calls[-1][0] == "Select text in this window first"
def test_incognito_window_refused():                  # active title "… (Incognito)" -> refused
def test_terminal_allowed_but_copy_only():
    sel.set(text="pls review teh patch", owner=win("com.mitchellh.ghostty", xid=3), active=same)
    sa.trigger(); assert overlay.panel is not None and not overlay.panel.can_paste
def test_secret_in_selection_is_shielded():           # selection with sk-ant-… -> backend text has [[GLANCE_S1]]
def test_copy_is_default_and_puts_rewrite_on_clipboard():
def test_paste_over_sends_paste_and_reports_unverified():
    sa.paste_over(); assert capture.pastes == ["Please review the patch."]
def test_uses_verified_path_when_field_is_captured():
    # controller.current text "aa pls review teh patch bb" in same window; selection "pls review teh patch" -> request_apply Edit(3, 23, …); capture.pastes == []
def test_ambiguous_selection_in_captured_field_falls_back_to_selection_mode():   # selection occurs twice -> no request_apply
def test_too_long_selection_refused():
def test_paste_unverified_refuses_when_clipboard_not_restorable():   # engine-level: status "refused", no ctrl+v
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_select_anything.py -v` — FAIL.
- [ ] **Step 3: Implement** logic + protocol/engine changes.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Desktop test (`@pytest.mark.desktop`)** — select all in `gtk_app.py` TextView (XTEST Ctrl+A), Super+Alt+S, fake backend; `test_captured_field_uses_verified_apply` (buffer replaced, one Ctrl+Z restores); with `glance-capture` told to ignore the app (`[apps.gtk_app] capture = false`): `test_uncaptured_app_paste_over` (click "Paste over selection" via the overlay's test hook → buffer replaced, clipboard sentinel restored).
- [ ] **Step 6: Implement** `selection_xlib.py`, selection-mode panel; run desktop tests — PASS.
- [ ] **Step 7: Commit** `feat: select-anything rewrite`.

---

### Task 7: `glance-edit` and the Ctrl+G window

**Files:**
- Create: `src/glance/features/edit_session.py`, `src/glance/ui/edit_window_gtk.py`, `src/glance/edit_shim/__init__.py`, `src/glance/edit_shim/main.py`
- Modify: `pyproject.toml` (script `glance-edit = glance.edit_shim.main:main`), `src/glance/daemon/main.py` (ctl message `edit_session`)
- Test: `tests/test_edit_shim.py`, `tests/test_edit_session.py`, `tests/desktop/test_edit_window.py`

**Interfaces:**
- Consumes: `JsonLineClient`, `ctl_sock`, `GrammarPort`, `IssueFilter`, `rewrite_text`, `BackendSupervisor`, `BUILTIN["ai_prompt"]`, `apply_edit`, `issue_edit`.
- Produces (`edit_shim/main.py`):
  - `is_claude_code(env:Mapping[str,str], ancestors:Sequence[str]) -> bool` — the rule recorded in findings S7.2 (env var name set by Claude Code; fallback: any ancestor process `comm` equal to `claude`). `ancestors()` reads `/proc/<pid>/stat` + `/proc/<pid>/comm` upward, max 8 levels.
  - `real_editor(env) -> list[str]` — first non-empty of `GLANCE_REAL_EDITOR`, `VISUAL`, `EDITOR` whose first word is not `glance-edit`, split with `shlex`; else the first of `sensible-editor`, `nano`, `vi` found on `PATH`.
  - `main(argv:list[str]|None=None) -> int`. Order: (1) no file argument or more than one non-flag argument, or not `is_claude_code` → `exec` real editor with the original argv. (2) Read the file (UTF-8; on decode error → exec real editor). (3) Connect to `ctl_sock()` with `timeout_s=0.5`; failure → exec real editor. (4) `request("edit_session", {"text": text, "window": active-window xid from $WINDOWID or 0}, timeout_s=None)`; reply `{"action":"apply","text":T}` → write `T` to the file (same trailing-newline state as the original), return 0; `{"action":"editor","text":T}` → write `T`, exec real editor on the file; `{"action":"cancel"}` → return 0 with the file untouched. (5) Disconnect or malformed reply while waiting → exec real editor on the untouched file. The shim never writes anything except to the file it was given, and logs nothing.
- Produces (`edit_session.py`): `class EditSession(text:str, grammar, issue_filter, supervisor, ctx:Ctx, run_bg)` — `text:str`, `issues() -> list[Issue]` (recomputed on change), `set_text(t)`, `accept(issue_id, choice=0)` (edits `text` directly with `apply_edit`), `fix_all_safe()`, `preview(tone, custom=None, on_update=…)` (via `rewrite_text`; outcome kept only if `text` is unchanged since the request), `apply_preview() -> bool` (replaces `text`; refuses when `RewriteOutcome.applicable` is false — "Copy rewrite" still offered), `chips` = `ai_prompt` chips + custom, `undo()` (single-level: restores the text before the last accept/apply).
- Produces (`edit_window_gtk.py`): `class EditWindow(session:EditSession, over:Rect|None, on_done:Callable[[str,str],None])` — a normal, focused, keep-above GTK window centred over the terminal rect (`over`), titled "Glance — edit prompt"; editable `Gtk.TextView` with the prompt (issues underlined in the view — this window is Glance's own, so drawing there is fine), tone-chip row with Sharpen prompt first, diff preview pane, issue list; buttons **Apply** (Ctrl+Enter) → `on_done("apply", text)`, **Open in my editor** (Ctrl+E) → `on_done("editor", text)`, **Cancel** (Esc, or closing the window) → `on_done("cancel", "")`. One window at a time: a second `edit_session` request while one is open is answered `{"action":"cancel"}` for the *new* request after presenting the existing window.
- Daemon: `edit_session` handler creates `EditSession` with `Ctx` from `BUILTIN["ai_prompt"]` and `app_label="claude-code"`, opens `EditWindow`, replies when `on_done` fires. Paused state does not block it (the user asked explicitly). The prompt text lives only in the session object and is dropped on reply.

- [ ] **Step 1: Write the failing tests**

```python
def test_passthrough_when_not_claude_code(exec_spy, tmp_path):
    f = tmp_path / "COMMIT_EDITMSG"; f.write_text("msg")
    main_with(env={"GLANCE_REAL_EDITOR": "vim -u NONE"}, ancestors=["zsh", "git"], argv=[str(f)])
    assert exec_spy.argv == ["vim", "-u", "NONE", str(f)] and f.read_text() == "msg"
def test_real_editor_never_resolves_to_itself():
    assert real_editor({"EDITOR": "glance-edit", "VISUAL": "glance-edit"})[0] in ("sensible-editor", "nano", "vi")
def test_falls_through_when_daemon_down(exec_spy, tmp_path, monkeypatch):
    # claude ancestors, XDG_RUNTIME_DIR empty -> exec real editor within 1 s; file untouched
def test_falls_through_when_daemon_dies_mid_session(exec_spy, fake_ctl, tmp_path):
    # fake ctl accepts edit_session then closes the connection -> exec real editor; file untouched
def test_apply_writes_file_and_returns_0(fake_ctl, tmp_path):
    f.write_text("fix teh bug\n"); fake_ctl.reply = {"action": "apply", "text": "Fix the bug in [which file?]"}
    assert run() == 0 and f.read_text() == "Fix the bug in [which file?]\n"      # trailing newline preserved
def test_cancel_leaves_file_untouched(fake_ctl, tmp_path):
def test_editor_action_writes_then_execs(exec_spy, fake_ctl, tmp_path):
def test_malformed_reply_falls_through(exec_spy, fake_ctl, tmp_path):   # {"action": "weird"} -> exec real editor, file untouched
def test_undecodable_file_falls_through(exec_spy, tmp_path):
def test_multiple_file_args_pass_through(exec_spy):

def test_session_accept_edits_buffer():
    s = session("fix teh bug"); s.accept(s.issues()[0].id); assert s.text == "fix the bug"
def test_session_preview_dropped_if_text_changed():
def test_session_apply_preview_and_undo():
    # preview sharpen -> apply_preview() True, text == rewritten; undo() -> original
def test_session_refuses_unapplicable_outcome():   # outcome reason "placeholders" -> apply_preview() False, text unchanged
def test_session_text_is_shielded_for_backend():
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_edit_shim.py tests/test_edit_session.py -v` — FAIL.
- [ ] **Step 3: Implement** the shim and the session.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Desktop test (`@pytest.mark.desktop`, fake backend)** — `test_edit_window_apply_round_trip`: run `glance-edit <tmpfile>` as a subprocess with `GLANCE_TEST_FORCE_CLAUDE=1` (honoured only with `GLANCE_TEST=1`); the window appears and has focus; XTEST Ctrl+Enter → process exits 0, file holds the window text. `test_edit_window_escape_cancels`: Esc → exit 0, file unchanged. `test_open_in_editor_chains`: `GLANCE_REAL_EDITOR` = a script that appends `X` → Ctrl+E → file ends with `X`.
- [ ] **Step 6: Implement** `edit_window_gtk.py` and the daemon handler; run — PASS.
- [ ] **Step 7: Commit** `feat: glance-edit shim and ctrl+g edit window`.

---

### Task 8: `claude` zsh wrapper

**Files:**
- Create: `src/glance/integrations/__init__.py`, `src/glance/integrations/claude_wrapper.py`
- Modify: `src/glance/cli.py` (`glance wrapper install|uninstall|status` — used by Plan 4's installer and doctor), `README.md`
- Test: `tests/test_claude_wrapper.py`

**Interfaces:**
- Produces:
  - `BEGIN = "# >>> glance claude wrapper >>>"`, `END = "# <<< glance claude wrapper <<<"`
  - `SNIPPET` — exactly (between the markers):

    ```zsh
    claude() {
      GLANCE_REAL_EDITOR="${VISUAL:-${EDITOR:-}}" VISUAL=glance-edit EDITOR=glance-edit command claude "$@"
    }
    ```

    (`VISUAL` is set as well only if findings S7.1 shows Claude Code prefers it; the assignments are command-scoped, so the shell's own `$EDITOR`/`$VISUAL` never change.)
  - `status(zshrc:Path) -> str` — `installed | absent | conflict` (`conflict`: a `claude()` function, `function claude`, or `alias claude=` exists outside the marked block).
  - `install(zshrc:Path) -> str` — appends the block once (idempotent); returns `conflict` and writes nothing when a conflicting definition exists; creates the file if missing; atomic write; preserves mode.
  - `uninstall(zshrc:Path) -> bool` — removes exactly the marked block (and the single blank line the installer added before it); every other byte of the file is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_install_is_idempotent(tmp_path):
    z = tmp_path / ".zshrc"; z.write_text("export FOO=1\n")
    assert install(z) == "installed" and install(z) == "installed" and z.read_text().count(BEGIN) == 1
def test_uninstall_restores_bytes_exactly(tmp_path):
    original = "export FOO=1\n# comment\n\nalias ll='ls -l'\n"; z.write_text(original); install(z); assert uninstall(z) and z.read_text() == original
def test_conflict_with_existing_function_or_alias(tmp_path):
    for existing in ("claude() { command claude --foo \"$@\" }\n", "alias claude='claude --bar'\n", "function claude { :; }\n"):
        z.write_text(existing); assert install(z) == "conflict" and z.read_text() == existing
def test_missing_zshrc_created_and_uninstall_leaves_empty_file(tmp_path):
def test_file_mode_preserved(tmp_path):                # 0o600 before -> 0o600 after
def test_snippet_does_not_export_editor():
    assert "export" not in SNIPPET and "command claude" in SNIPPET

@pytest.mark.skipif(shutil.which("zsh") is None, reason="needs zsh")
def test_wrapper_scopes_editor_to_claude_only(tmp_path):
    # ZDOTDIR=tmp with the snippet; PATH has a fake `claude` printing "$EDITOR|$GLANCE_REAL_EDITOR"
    # zsh -ic 'EDITOR=vim; claude; echo "after=$EDITOR"'  ->  "glance-edit|vim" then "after=vim"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_claude_wrapper.py -v` — FAIL.
- [ ] **Step 3: Implement** module and CLI subcommand (operates on `${ZDOTDIR:-$HOME}/.zshrc`; on `conflict` prints the conflicting line number and the manual snippet, exit 2).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Manual acceptance.** `glance wrapper install`; open a new Ghostty tab; `claude`; type `fix teh bug in login`; Ctrl+G → the Glance window opens over the terminal with the prompt, `teh` flagged, Sharpen prompt first; Ctrl+Enter returns the edited prompt to Claude Code; repeat with Esc (prompt unchanged) and Ctrl+E (real editor opens). `echo $EDITOR` in the shell is unchanged. `git commit` in a scratch repo opens the normal editor. Do not submit the prompt.
- [ ] **Step 6: Commit** `feat: claude zsh wrapper for ctrl+g`.
