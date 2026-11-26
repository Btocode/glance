from glance.config import AiCfg, _parse
from glance.wordlimit import count_words, limit_notice, word_window


def numbered(n):
    return " ".join(f"Sentence number {i} has five words." for i in range(n))


def test_count_words_is_a_whitespace_split():
    assert count_words("ami tomake bhalobashi 🙂  ok\nশুভ সকাল") == 7
    assert count_words("") == 0


def test_a_short_text_is_all_sent():
    t = numbered(10)
    assert word_window(t, 3, 600) == (0, len(t))


def test_the_window_is_around_the_caret_and_aligned_to_sentences():
    t = numbered(300)  # 1500 words
    caret = t.index("number 150 ")
    w0, w1 = word_window(t, caret, 600)
    part = t[w0:w1]
    assert w0 < caret < w1
    assert count_words(part) <= 600 and count_words(part) > 550
    assert part.startswith("Sentence number ") and part.endswith("words.")
    # at the start and the end of the text
    w0, w1 = word_window(t, 0, 600)
    assert w0 == 0 and t[:w1].endswith("words.") and count_words(t[:w1]) == 600
    w0, w1 = word_window(t, len(t), 600)
    assert w1 == len(t) and t[w0:].startswith("Sentence")


def test_paragraph_boundaries_count_too():
    para = " ".join(["word"] * 99) + "\n"
    t = para * 20  # 1980 words in paragraphs without a full stop
    w0, w1 = word_window(t, len(t) // 2, 300)
    assert t[w0 - 1] == "\n" and t[w1] == "\n"


def test_notice_text():
    assert limit_notice(600, 1450) == \
        "Long text: AI checked 600 of 1,450 words around your cursor"
    assert limit_notice(600, 1450, verb="rewrote", where="at the start of your selection") == \
        "Long text: AI rewrote 600 of 1,450 words at the start of your selection"


def test_max_words_config():
    assert AiCfg().max_words == 600
    assert _parse({"ai": {"max_words": 1200}}).ai.max_words == 1200
    for bad in (10, 6000, "lots", True, 49):
        cfg = _parse({"ai": {"max_words": bad, "monthly_cost_limit_usd": 2.0}})
        assert cfg.ai.max_words == 600 and cfg.ai.monthly_cost_limit_usd == 2.0
    assert _parse({"ai": {"max_words": 50}}).ai.max_words == 50
    assert _parse({"ai": {"max_words": 5000}}).ai.max_words == 5000
