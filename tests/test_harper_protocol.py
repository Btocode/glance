from glance.grammar.harper import FrameReader, diagnostics_to_issues, frame

TEXT = "I 😀 recieve it"


def diag(code, sc, ec, line=0, msg="m"):
    return {"code": code, "message": msg, "source": "Harper",
            "range": {"start": {"line": line, "character": sc},
                      "end": {"line": line, "character": ec}}}


def fix(new):
    return {"kind": "quickfix", "title": f"Replace with: “{new}”",
            "edit": {"changes": {"file:///x.txt": [{"range": {}, "newText": new}]}}}


def test_frame_reader_handles_split_and_joined_frames():
    r = FrameReader()
    a, b = frame({"id": 1}), frame({"id": 2})
    assert r.feed(a[:5]) == [] and r.feed(a[5:] + b) == [{"id": 1}, {"id": 2}]


def test_frame_reader_counts_bytes_not_characters():
    r = FrameReader()
    assert r.feed(frame({"t": "é😀"})) == [{"t": "é😀"}]


def test_diagnostics_to_issues_uses_codepoints():
    [i] = diagnostics_to_issues(TEXT, [diag("SpellCheck", 5, 12)], {0: [fix("receive")]})
    assert (i.start, i.end, i.rule, i.replacements, i.safe) == (
        4, 11, "SpellCheck", ("receive",), False)
    assert i.source == "harper" and i.id == "harper:SpellCheck:4:11"


def test_safe_flag_from_diagnostics():
    t = "i think so"
    [one] = diagnostics_to_issues(t, [diag("CapitalizePersonalPronouns", 0, 1)], {0: [fix("I")]})
    assert one.safe is True
    [two] = diagnostics_to_issues(
        t, [diag("CapitalizePersonalPronouns", 0, 1)], {0: [fix("I"), fix("Me")]})
    assert two.safe is False and two.replacements == ("I", "Me")
    [spell] = diagnostics_to_issues(t, [diag("SpellCheck", 2, 7)], {0: [fix("thing")]})
    assert spell.safe is False
    [none] = diagnostics_to_issues(t, [diag("CapitalizePersonalPronouns", 0, 1)], {0: []})
    assert none.safe is False and none.replacements == ()


def test_code_that_is_the_replacement_word_is_spellcheck():
    # Harper puts the replacement word in `code` for the "Teh" typo.
    acts = {0: [fix("The"), fix("Th"), fix("The"), fix("Tech")]}
    [i] = diagnostics_to_issues("Teh cat", [diag("The", 0, 3)], acts)
    assert i.rule == "SpellCheck" and i.replacements == ("The", "Th", "Tech") and not i.safe


def test_actions_without_edit_are_ignored():
    acts = {0: [{"title": "Ignore", "command": {"command": "HarperIgnoreLint"}}, fix("I")]}
    [i] = diagnostics_to_issues("i go", [diag("CapitalizePersonalPronouns", 0, 1)], acts)
    assert i.replacements == ("I",)


def test_multiline_ranges_and_dedup_by_range_and_code():
    t = "ab\nteh x"
    ds = [diag("SpellCheck", 0, 3, line=1), diag("SpellCheck", 0, 3, line=1)]
    out = diagnostics_to_issues(t, ds, {0: [fix("the")], 1: [fix("the")]})
    assert [(i.start, i.end) for i in out] == [(3, 6)]
