"""What the default corrected sentence may change: corrections only, never rewording.

The collapsed strip shows the user's own sentence with its spelling, grammar, punctuation and
capitalisation fixed, in their words and tone. Two sources feed it:

- Harper: its one-replacement (safe) fixes, plus the spelling fixes it is confident about
  (`confident_spelling`): a word split ("likethe" -> "like the"), a case-only fix ("ai" ->
  "AI"), a lone suggestion, or a top suggestion that is a near miss of the typed word.
- The AI analysis: only issues of a correction kind (`CORRECTION_KINDS`) whose replacement is a
  minimal edit of the quoted text (`minimal_edit`). Clarity, style, wording and tone
  suggestions stay in the drawer's list.
"""
from __future__ import annotations

import dataclasses
import difflib
import re

from glance.grammar.issues import Issue

SPELL_RULE = "SpellCheck"
SPLIT_RULE = "SplitWords"
# AI issue kinds that only correct the text (anything else is a suggestion, not a fix).
CORRECTION_KINDS = frozenset({
    "spelling", "grammar", "punctuation", "capitalization", "agreement", "missing_word"})
# AI issue kinds that may reword: offered in the drawer, never in the corrected sentence.
SUGGESTION_KINDS = ("clarity", "style", "wording", "tone")

# The AI's sentence-level rewordings in fluent mode (`backends.corrected`): never asked of
# the model as a kind, only made by the filter that accepted the reworded sentence.
FLUENT_KINDS = frozenset({"rephrase"})

# Harper rules that restyle correct text rather than fix it ("FYI" -> "For your
# information", "config" -> "configuration", "out of date" -> "out-of-date", the Oxford
# comma): listed in the drawer, never typed by the corrected sentence.
STYLE_RULES = frozenset({
    "Gonna", "ForYourInformation", "OutOfDate", "ToDoHyphen", "PhrasalVerbAsCompoundNoun",
    "RoadMap", "NumericRangeEnDash", "OxfordComma"})
_STYLE_PREFIXES = ("Expand",)  # ExpandConfiguration, ExpandMinimum, ExpandTimeShorthands, ...

# Chat words, abbreviations and tech words the spell checker does not know: never "fixed"
# without asking ("thx" -> "the", "lol" -> "low", "wifi" -> "wiki").
CHAT_WORDS = frozenset("""
lol lmao rofl thx tnx ty np pls plz btw imo imho tbh idk omg brb afk ttyl fyi asap eta
gonna wanna gotta kinda sorta dunno lemme gimme ya yall y'all yep yup nope nah ok okay
u ur r k kk hmm hmmm hm uh um yeah shoutout cc cc'd bcc'd wifi repo repos config
configs dev devs prod env auth admin infra todo lgtm nit nits sec min mins req reqs
""".split())  # noqa: SIM905 - a word list

# Function words: articles, pronouns, auxiliaries, prepositions, conjunctions, and their
# contractions (with or without the apostrophe). Swapping one for another ("is" -> "are",
# "which" -> "who", "dont" -> "doesn't") is grammar, not rewording.
_FUNCTION = """
a an the this that these those some any each every either neither no all both few many much
more most less least other another such own same
i me my mine myself you your yours yourself we us our ours they them their theirs he him his
she her hers it its itself one ones who whom whose which what whatever where when why how
there here then than
be am is are was were been being have has had having do does did done doing go goes went gone
going get gets got gotten will would shall should can could may might must ought
to of in on at by for with from into onto about as so and or but if because while after
before until since though although whether up out over off down too also just very really
still yet only even not never
im ive id ill youre youve youd youll were weve wed well theyre theyve theyd theyll hes shes
its itll thats theres whats whos lets
dont doesnt didnt isnt arent wasnt werent havent hasnt hadnt wont wouldnt cant cannot couldnt
shouldnt mustnt neednt
"""
FUNCTION_WORDS = frozenset(_FUNCTION.split())
_NEGATIONS = frozenset({"not", "never", "no", "cannot"})

# Words people swap for one another ("their" / "there" / "they're"): a homophone fix.
_CONFUSABLE_GROUPS = (
    "their there theyre", "your youre", "its it's", "whose whos", "to too two", "then than",
    "lose loose", "loses looses", "losing loosing", "affect effect", "affects effects",
    "accept except", "brake break", "past passed", "were we're where", "principle principal",
    "later latter", "of have", "weather whether", "quite quiet", "advice advise",
    "breath breathe", "lead led", "peak peek", "site sight cite", "hear here", "know no",
    "knew new", "buy by bye", "week weak", "right write", "piece peace", "aloud allowed",
    "desert dessert", "complement compliment", "fair fare", "whole hole", "a an",
    "every everyday", "who which that", "would had", "will would",
)
_CONFUSABLE: dict[str, frozenset[str]] = {}
for _g in _CONFUSABLE_GROUPS:
    _words = frozenset(w.replace("'", "") for w in _g.split())
    for _w in _words:
        _CONFUSABLE[_w] = _CONFUSABLE.get(_w, frozenset()) | _words

_INFLECTIONS = frozenset({"", "s", "es", "ed", "d", "ing", "er", "est", "r", "st", "ly",
                          "ies", "ied", "y", "e", "n", "en"})


def _bare(word: str) -> str:
    return word.lower().replace("'", "").replace("’", "")


def is_function_word(word: str) -> bool:
    return _bare(word) in FUNCTION_WORDS


def negations(words: list[str]) -> int:
    """How many negations ("not", "never", "don't", "cant") the words hold."""
    n = 0
    for w in words:
        b = _bare(w)
        n += b in _NEGATIONS or (b.endswith("nt") and b in FUNCTION_WORDS)
    return n


def same_stem(a: str, b: str) -> bool:
    """`b` is an inflection of `a` or the reverse ("crash" / "crashes", "fast" / "faster",
    "stop" / "stopped", "try" / "tried")."""
    a, b = a.lower(), b.lower()
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    if p < 3:
        return False
    ra, rb = a[p:], b[p:]

    def ok(r: str) -> bool:
        return r in _INFLECTIONS or (len(r) >= 3 and r[0] == a[p - 1]
                                     and r[1:] in ("ed", "ing", "er", "est"))
    return ok(ra) and ok(rb)


def confusable(a: str, b: str) -> bool:
    return _bare(b) in _CONFUSABLE.get(_bare(a), ())


def related(a: str, b: str) -> bool:
    """`b` may stand where `a` was in a correction: the same word but for a typo, an
    inflection, a homophone or a contraction."""
    return (a.lower() == b.lower() or _bare(a) == _bare(b) or _close(a, b)
            or same_stem(a, b) or confusable(a, b))


def swap_ok(old: str, new: str) -> bool:
    """One word swapped for another in a correction: related words, or function words
    either side ("is" -> "are", "which" -> "who"), never a different content word ("plan"
    -> "strategy")."""
    return (related(old, new) or (is_function_word(old) and is_function_word(new))
            or (len(old) <= 3 and len(new) <= 3))  # a short typo ("sal" -> "all")


_WORD = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_TOKEN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]")


def osa_distance(a: str, b: str) -> int:
    """Edit distance counting an adjacent transposition as one edit."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def _sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip()
    return not before or before[-1] in ".!?…\n"


def confident_spelling(text: str, issue: Issue) -> str | None:
    """The replacement for a Harper spelling (or word-split) issue that is safe to apply
    without asking, or None. Names, acronyms and very short words are left alone (a
    two-letter typo has too many readings); the personal dictionary, protected entities and
    Banglish never get here (the issue filter drops them first)."""
    if issue.source != "harper" or issue.rule not in (SPELL_RULE, SPLIT_RULE):
        return None
    word = text[issue.start:issue.end]
    repl = issue.replacements
    if not repl or not _WORD.fullmatch(word):
        return None
    low = word.lower()
    if _bare(word) in CHAT_WORDS or low in CHAT_WORDS:
        return None  # "thx", "lol", "wifi": the writer's own words
    for r in repl:  # "werent" -> "weren't", "dosent" -> "doesn't": a lost apostrophe
        if ("'" in r or "’" in r) and osa_distance(_bare(r), _bare(word)) <= 1 \
                and _WORD.fullmatch(r):
            return r[0].upper() + r[1:] if word[0].isupper() else r
    for r in repl:  # "likethe" -> "like the"; never "a dress" for "adress"
        if " " in r and r.replace(" ", "").lower() == low \
                and all(len(p) > 1 for p in r.split()):
            return r
    if issue.rule != SPELL_RULE:
        return None
    if word.isupper() or any(c.isupper() for c in word[1:]):
        return None  # an acronym or a camelCase name
    if word[0].isupper() and not _sentence_start(text, issue.start):
        return None  # a capitalised word mid-sentence: likely a name
    for r in repl:  # "ai" -> "AI" (an acronym; "sal" -> "Sal" is a guess at a name: no)
        if r != word and r.lower() == low and r.isupper() and len(word) > 1:
            return r
    if len(word) < 3:
        return None
    first = repl[0]
    if not _WORD.fullmatch(first) or first.lower() == low:
        return None  # ("sal" -> "Sal": capitalised, it is a guess at a name)
    if dialect_variant(low, first.lower()):
        return None  # "colour" -> "color": British spelling is not a typo
    if len(repl) == 1:
        return first
    if first[0].lower() == low[0] and osa_distance(low, first.lower()) <= 2:
        return first
    return None


_DIALECT = ((r"our\b", "or"), (r"our(?=[a-z])", "or"), (r"is(?=(e|ed|es|ing|ation|ations)\b)", "iz"),
            (r"ys(?=(e|ed|es|ing)\b)", "yz"), (r"tre\b", "ter"), (r"tres\b", "ters"),
            (r"ll(?=(ed|ing|er)\b)", "l"), (r"ogue\b", "og"), (r"ence\b", "ense"))


def dialect_variant(a: str, b: str) -> bool:
    """`a` and `b` are one word in British and American spelling ("colour" / "color",
    "organised" / "organized", "travelled" / "traveled")."""
    def american(w: str) -> str:
        for pat, rep in _DIALECT:
            w = re.sub(pat, rep, w)
        return w
    return a != b and american(a) == american(b)


def ai_kind(issue: Issue) -> str:
    return issue.rule.removeprefix("llm:")


def near_miss(a: str, b: str) -> bool:
    """`b` is `a` with a typo or an inflection (case aside)."""
    return _close(a, b)


def _close(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or osa_distance(a, b) <= max(1, min(2, len(a) // 3))


def minimal_edit(quote: str, replacement: str) -> bool:
    """True when `replacement` corrects `quote` rather than rewording it: the words it drops
    or swaps are near misses of the words it puts in (a typo, an inflection, a split or a
    join), plus at most two words added, dropped or swapped one for one (a missing or doubled
    word, agreement: "is" -> "are")."""
    a, b = _TOKEN.findall(quote), _TOKEN.findall(replacement)
    if a == b:
        return quote != replacement  # whitespace only
    sm = difflib.SequenceMatcher(a=[t.lower() for t in a], b=[t.lower() for t in b],
                                 autojunk=False)
    budget = 2
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        old, new = a[i1:i2], b[j1:j2]
        if op == "equal" or "".join(old).lower() == "".join(new).lower():
            continue  # unchanged but for case, or a split or a join
        if op == "replace" and abs(len(old) - len(new)) <= 1 and all(
                any(_close(o, n) for n in new) for o in old if o.isalnum()):
            continue  # near misses ("its" -> "it is")
        if negations(old) != negations(new):
            return False  # "can" -> "can't" changes the meaning
        words = [t for t in old + new if t[0].isalnum()]
        if op == "replace" and words and all(is_function_word(t) for t in words):
            budget -= max(len(old), len(new))  # grammar words ("would have" -> "had")
        else:
            budget -= len(old) if op == "replace" and len(old) == len(new) \
                else len(old) + len(new)
        if budget < 0:
            return False
    return True


def ai_correction(text: str, issue: Issue) -> str | None:
    """The replacement of an AI-analysis issue that may go into the corrected sentence."""
    if issue.source != "llm" or not issue.replacements:
        return None
    r = issue.replacements[0]
    if ai_kind(issue) in FLUENT_KINDS:
        return r  # a reworded sentence `backends.corrected` accepted (fluent mode only)
    if ai_kind(issue) not in CORRECTION_KINDS:
        return None
    return r if minimal_edit(text[issue.start:issue.end], r) else None


def strip_replacement(text: str, issue: Issue) -> str | None:
    """What the corrected sentence puts in place of `issue`'s span, or None to leave it."""
    if issue.source == "harper":
        if issue.rule in STYLE_RULES or issue.rule.startswith(_STYLE_PREFIXES):
            return None  # restyles correct text: the drawer offers it
        if issue.safe and issue.replacements:
            return issue.replacements[0]
        return confident_spelling(text, issue)
    if issue.source == "llm":
        return ai_correction(text, issue)
    return None


def _strip_first(text: str, i: Issue) -> Issue:
    """`i` with the replacement the strip uses first: the drawer's fix list, a click on a
    fix and the strip's corrected text then all say the same ("sal -> say" is never listed
    beside a strip that typed something else)."""
    r = strip_replacement(text, i)
    if r is None or (i.replacements and i.replacements[0] == r):
        return i
    return dataclasses.replace(i, replacements=(r, *(x for x in i.replacements if x != r)))


def _respaced(text: str, i: Issue) -> Issue | None:
    """A space removed before punctuation that runs into the next word ("me .See") moves
    after it ("me. See") instead of leaving "me.See"."""
    end = i.end
    if (i.source != "harper" or i.replacements != ("",) or not text[i.start:end].isspace()
            or end + 1 >= len(text) or text[end] not in ",.;:!?" or not text[end + 1].isalnum()):
        return i
    if text[end] == "." and not text[end + 1].isupper():
        return None  # "add it to .gitignore": a file name, not a stray space
    return dataclasses.replace(i, end=end + 1, replacements=(text[end] + " ",),
                               id=f"{i.source}:{i.rule}:{i.start}:{end + 1}")


def mechanical_fix(text: str, issue: Issue) -> bool:
    """A local fix that changes only case, spacing or punctuation, or puts back a lost
    apostrophe ("hey" -> "Hey", "i" -> "I", "doing ," -> "doing,", "dont" -> "don't"):
    safe to keep beside the AI's corrections."""
    r = strip_replacement(text, issue)
    if r is None:
        return False
    old = text[issue.start:issue.end]
    if old.lower() == r.lower():
        return True
    if re.search(r"\w \w", r) and not re.search(r"\w \w", old):
        return False  # a split ("pgvector" -> "pg vector") is a guess, not a mechanical fix
    squash = re.compile(r"[\W_]+")
    return squash.sub("", old).lower() == squash.sub("", r).lower()


def _capitalised(text: str, ai: Issue, local: list[Issue]) -> Issue:
    """The AI's fix of a word the local checker capitalises at a sentence start, with that
    capital ("dont" -> "don't" and "dont" -> "Dont" make "Don't")."""
    if not ai.replacements or not ai.replacements[0][:1].islower():
        return ai
    for h in local:
        r = strip_replacement(text, h)
        old = text[h.start:h.end]
        if (h.start == ai.start and r and r[:1].isupper() and old[:1].islower()
                and r[1:] == old[1:]):
            rep = ai.replacements[0]
            return dataclasses.replace(ai, replacements=(rep[0].upper() + rep[1:],
                                                         *ai.replacements[1:]))
    return ai


def combine_sources(text: str, kept: list[Issue], ai_corrects: bool) -> list[Issue]:
    """The issues the strip and the drawer use, from the filtered local (Harper) and AI
    ones, in position order. `ai_corrects`: the AI's corrected text is back for exactly this
    text, so its corrections replace the quick local ones (which were the preview)."""
    kept = [r for r in (_respaced(text, i) for i in kept) if r is not None]
    if ai_corrects:
        # The AI's corrections replace the local ones, but for the mechanical local fixes
        # (case, spacing, a lost apostrophe) on text the AI left as it was: a quick model
        # sometimes misses "hey" -> "Hey" or "i" -> "I". Where it changed the text, it wins.
        ai = [i for i in kept if i.source != "harper"]
        local = [h for h in kept if h.source == "harper" and mechanical_fix(text, h)]
        ai = [_capitalised(text, i, local) for i in ai]
        local = [h for h in local if not any(h.start < i.end and i.start < h.end for i in ai)]
        return sorted((_strip_first(text, i) for i in local + ai),
                      key=lambda i: (i.start, i.end))
    local = [i for i in kept if i.source == "harper"]
    # Where the AI analysis corrects a word the local checker only says is misspelled,
    # the AI's choice wins (it reads the context: "fo" -> "of", not "for").
    spell = [h for h in local if h.rule in (SPELL_RULE, SPLIT_RULE)]
    overrides = [i for i in kept if i.source == "llm"
                 and ai_correction(text, i) is not None
                 and any(i.start < h.end and h.start < i.end for h in spell)
                 and not any(i.start < h.end and h.start < i.end
                             for h in local if h not in spell)]
    local = [h for h in local
             if not any(h.start < o.end and o.start < h.end for o in overrides)]
    # Otherwise the local checker wins where another source flags the same text.
    others = [i for i in kept if i.source != "harper"
              and not any(i.start < h.end and h.start < i.end for h in local)]
    return sorted((_strip_first(text, i) for i in local + others),
                  key=lambda i: (i.start, i.end))
