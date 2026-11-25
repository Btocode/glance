import pytest

from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue
from glance.grammar.lang import LanguageDetector, sentence_is_non_english, split_sentences
from glance.grammar.store import IgnoredRules, PersonalDictionary


@pytest.fixture
def dic(tmp_path):
    return PersonalDictionary(tmp_path / "dictionary.txt")


@pytest.fixture
def ign(tmp_path):
    return IgnoredRules(tmp_path / "ignored.json")


def spell_issues_for(text, words, source="harper"):
    out, pos = [], 0
    for w in words:
        s = text.index(w, pos)
        pos = s + len(w)
        out.append(Issue(f"{source}:SpellCheck:{s}:{s + len(w)}", source, "SpellCheck", "m",
                         s, s + len(w), ("x",), False))
    return out


def test_split_sentences():
    t = "Hi there. ami kal ashbo\nok?"
    assert [t[s.start:s.end] for s in split_sentences(t)] == ["Hi there", "ami kal ashbo", "ok"]
    assert {s.kind for s in split_sentences(t)} == {"sentence"}


@pytest.mark.parametrize("sentence, misspelled, words, want", [
    ("ami kal office e jabo na", 4, 6, True),
    ("tumi ki korcho ekhon", 3, 4, True),
    ("bhai eta thik ache", 2, 4, True),
    ("আমি ভালো আছি", 0, 3, True),
    ("I will send the report tomorrow", 0, 6, False),
    ("pls chk teh reprot asap thx", 5, 6, False),
    ("kubectl rollout restart the nginx ingress", 4, 6, False),
    ("Zebulon and Rahim met Tanvir at Dhanmondi", 4, 7, False),
    ("na I think its fine", 0, 5, False),
    ("what ki is this", 0, 4, False),
    ("ami thik", 0, 2, True),
    ("ami went home today", 1, 4, True),
    ("ami went home today", 0, 4, False)])
def test_sentence_classification(sentence, misspelled, words, want):
    assert sentence_is_non_english(sentence, misspelled, words) is want


def test_technical_english_is_not_banglish(dic):
    t = "kubectl rollout restart the nginx ingress. Then chek teh logs."
    assert LanguageDetector(dic).spans(t, spell_issues_for(t, ["kubectl", "nginx", "chek", "teh"])) == []


def test_mixed_message_suppresses_only_banglish_sentence(dic, ign):
    t = "ami kal ashbo na. I will sned the file."
    out = IssueFilter(dic, ign, [], issue_span_filters=[LanguageDetector(dic).spans]).apply(
        t, spell_issues_for(t, ["ami", "kal", "ashbo", "sned"]), "slack")
    assert [t[i.start:i.end] for i in out] == ["sned"]


def test_llm_issues_survive_in_banglish_span(dic, ign):
    t = "ami kal ashbo na. I will sned the file."
    f = IssueFilter(dic, ign, [], issue_span_filters=[LanguageDetector(dic).spans])
    llm = spell_issues_for(t, ["ashbo"], source="llm")
    harper = spell_issues_for(t, ["ashbo"])
    assert f.apply(t, llm + harper, "slack") == llm


def test_dictionary_words_do_not_count_as_misspelled(dic):
    # "ami went home" has one marker; two dictionary-known misspellings must not tip it
    dic.add("wnet")
    dic.add("hom")
    t = "ami wnet hom"
    assert LanguageDetector(dic).spans(t, spell_issues_for(t, ["wnet", "hom"])) == []


def test_is_mixed_without_harper(dic):
    d = LanguageDetector(dic)
    assert d.is_mixed("ok. ami tomar sathe kotha bolbo")
    assert not d.is_mixed("ok. see you tomorrow")
