import json
import os

from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.privacy.entities import Span


def issue(text, word, rule="SpellCheck", source="harper"):
    s = text.index(word)
    return Issue(f"{source}:{rule}:{s}:{s + len(word)}", source, rule, "m", s, s + len(word),
                 ("x",), False)


def make(tmp_path, spans=()):
    d = PersonalDictionary(tmp_path / "dictionary.txt")
    ig = IgnoredRules(tmp_path / "ignored.json")
    return d, ig, IssueFilter(d, ig, list(spans))


def test_filter_drops_issue_inside_mention_url_code(tmp_path):
    _, _, f = make(tmp_path)
    t = "@jhon https://exmaple.test `teh` and teh"
    issues = [issue(t, w) for w in ("@jhon", "https://exmaple.test", "`teh`")]
    prose = Issue("harper:SpellCheck:37:40", "harper", "SpellCheck", "m", 37, 40, ("the",), False)
    assert t[37:40] == "teh"
    assert f.apply(t, issues + [prose], "gedit") == [prose]


def test_filter_drops_dictionary_word_and_ignored_rule(tmp_path):
    d, ig, f = make(tmp_path)
    d.add("Banglish")
    ig.ignore("LongSentences", "slack")
    t = "banglish is teh thing"
    spell = issue(t, "banglish")
    other = issue(t, "teh")
    long_ = issue(t, "is", rule="LongSentences")
    assert f.apply(t, [spell, other, long_], "Slack") == [other]
    assert f.apply(t, [spell, other, long_], "gedit") == [other, long_]


def test_global_ignore_applies_everywhere(tmp_path):
    _, ig, f = make(tmp_path)
    ig.ignore("LongSentences", None)
    t = "a teh"
    i = issue(t, "a", rule="LongSentences")
    assert f.apply(t, [i], "anything") == []


def test_extra_ignored_rules(tmp_path):
    _, _, f = make(tmp_path)
    t = "a teh"
    i = issue(t, "a", rule="Tone", source="llm")
    assert f.apply(t, [i], "gedit", extra_ignored=frozenset({"Tone"})) == []
    assert f.apply(t, [i], "gedit") == [i]


def test_extra_span_filter_suppresses(tmp_path):
    _, _, f = make(tmp_path, [lambda t: [Span(0, len(t), "lang")]])
    assert f.apply("teh", [issue("teh", "teh")], "gedit") == []


def test_dictionary_file_contains_only_words(tmp_path):
    d = PersonalDictionary(tmp_path / "dictionary.txt")
    d.add("Zebulon")
    d.add("zebulon")  # casefold duplicate
    assert (tmp_path / "dictionary.txt").read_text() == "Zebulon\n"
    assert d.contains("ZEBULON") and d.words() == frozenset({"Zebulon"})
    assert PersonalDictionary(tmp_path / "dictionary.txt").contains("zebulon")


def test_ignored_rules_json_shape_and_persistence(tmp_path):
    p = tmp_path / "ignored.json"
    ig = IgnoredRules(p)
    ig.ignore("A", None)
    ig.ignore("B", "Slack")
    assert json.loads(p.read_text()) == {"global": ["A"], "apps": {"slack": ["B"]}}
    ig2 = IgnoredRules(p)
    assert ig2.global_rules() == {"A"} and ig2.is_ignored("B", "SLACK")
    assert not ig2.is_ignored("B", "gedit") and not ig2.is_ignored("C", "slack")


def test_stores_write_atomically_without_leftovers(tmp_path):
    d = PersonalDictionary(tmp_path / "sub" / "dictionary.txt")
    d.add("x")
    assert os.listdir(tmp_path / "sub") == ["dictionary.txt"]


def test_corrupt_ignored_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "ignored.json"
    p.write_text("{not json")
    ig = IgnoredRules(p)
    assert ig.global_rules() == set()
    ig.ignore("A", None)
    assert IgnoredRules(p).global_rules() == {"A"}
