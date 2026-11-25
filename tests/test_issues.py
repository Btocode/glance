import pytest

from glance.grammar.issues import Issue, apply_edit, issue_edit, merge_edits, utf16_to_cp
from glance.protocol import Edit


def test_utf16_columns_map_to_codepoints():
    text = "I 😀 recieve it"  # 😀 is 2 UTF-16 units, 1 code point
    assert utf16_to_cp(text, 0, 5) == 4 and text[4:11] == "recieve"


def test_utf16_second_line():
    assert utf16_to_cp("ab\ncd", 1, 1) == 4


def test_utf16_clamps_past_end_of_line():
    assert utf16_to_cp("ab\ncd", 0, 99) == 2
    assert utf16_to_cp("ab\ncd", 5, 0) == 5


def test_issue_id_and_edit():
    i = Issue("harper:SpellCheck:0:3", "harper", "SpellCheck", "m", 0, 3, ("the", "tea"), False)
    assert issue_edit(i) == Edit(0, 3, "the")
    assert issue_edit(i, 1) == Edit(0, 3, "tea")


def test_merge_edits_minimal_span():
    t = "teh cat sat on teh mat"
    e = merge_edits(t, [Edit(0, 3, "the"), Edit(15, 18, "the")])
    assert e == Edit(0, 18, "the cat sat on the") and apply_edit(t, e) == "the cat sat on the mat"


def test_merge_edits_accepts_unsorted_and_touching():
    t = "abcdef"
    e = merge_edits(t, [Edit(3, 6, "Z"), Edit(0, 3, "Y")])
    assert apply_edit(t, e) == "YZ"


def test_merge_edits_rejects_overlap():
    with pytest.raises(ValueError):
        merge_edits("abcdef", [Edit(0, 3, "x"), Edit(2, 4, "y")])


def test_merge_edits_rejects_empty():
    with pytest.raises(ValueError):
        merge_edits("abc", [])


def test_apply_edit():
    assert apply_edit("abc", Edit(1, 2, "XY")) == "aXYc"
