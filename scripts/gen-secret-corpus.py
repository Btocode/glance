#!/usr/bin/env python3
"""Generate the labelled redaction corpus (deterministic, synthetic: no real secret in it).

    scripts/gen-secret-corpus.py --seed 7 [--out tests/acceptance/corpus]

secrets.jsonl  600 lines {"text", "secret", "kind"}: 100 each of api_key, jwt, pem, card,
               assignment, entropy, each inside one of 30 sentence templates.
benign.jsonl   400 lines {"text"}: prose, URLs, short git SHAs, phone and order numbers, long
               ordinary words, paths, slugs. Full-length 40-hex SHAs and UUIDs are left out on
               purpose: Glance may redact those.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import string
from pathlib import Path

B62 = string.ascii_letters + string.digits
B64URL = string.ascii_letters + string.digits + "-_"
KINDS = ("api_key", "jwt", "pem", "card", "assignment", "entropy")

TEMPLATES = (
    "{S}",
    "{S} is what I pasted by mistake.",
    "Here it is: {S}",
    "Use {S} for the deploy.",
    "Can you check {S} please?",
    "The value was \"{S}\" in the config.",
    "I wrote `{S}` in the notes.",
    "Hey, my login is {S} and it stopped working.",
    "Don't share this, but {S} is what they gave me.",
    "Put {S} in the settings file and restart.",
    "Reminder to self: {S}",
    "Ticket says the leaked value is {S}, rotate it.",
    "Thanks! Then I typed {S} and nothing happened.",
    "Please review the following before lunch: {S} and tell me what you think.",
    "In the screenshot you can see ({S}) at the top.",
    "First line.\nSecond line has {S} in it.\nThird line.",
    "He said: '{S}' and walked off.",
    "Try again with {S}, it should work this time.",
    "Subject: access\n\nHi team, {S}\n\nRegards, Sam",
    "Copied from the dashboard: {S}; please do not forward.",
    "Our staging notes: {S}, expires Friday.",
    "I think {S} was the one from the old account.",
    "Paste: {S} (end of paste)",
    "As discussed, {S}, and the rest is unchanged.",
    "The log line ended with {S}",
    "Quick question about {S} before the meeting tomorrow morning.",
    "\"{S}\"",
    "config -> {S} <- do not commit",
    "Is it okay that my message contains {S} in the middle of a sentence like this one?",
    "Last thing. {S}",
)
assert len(TEMPLATES) == 30


def rstr(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def mixed(rng: random.Random, alphabet: str, n: int) -> str:
    """Random token with at least one letter and one digit, as real generated keys have."""
    while True:
        t = rstr(rng, alphabet, n)
        if any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
            return t


def luhn_complete(prefix: str) -> str:
    for check in range(10):
        digits = prefix + str(check)
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch)
            if i % 2:
                d = d * 2 - 9 if d > 4 else d * 2
            total += d
        if total % 10 == 0:
            return digits
    raise AssertionError


def api_key(rng: random.Random, i: int) -> str:
    makers = (
        lambda: "sk-" + mixed(rng, B62 + "-_", rng.randint(24, 48)),
        lambda: "sk-ant-api03-" + mixed(rng, B64URL, rng.randint(40, 90)),
        lambda: "sk-proj-" + mixed(rng, B64URL, rng.randint(30, 60)),
        lambda: rng.choice(("gh" + "p_", "gh" + "o_", "gh" + "s_")) + mixed(rng, B62, 36),
        lambda: "github" + "_pat_" + mixed(rng, B62 + "_", rng.randint(40, 82)),
        lambda: "AK" + "IA" + mixed(rng, string.ascii_uppercase + string.digits, 16),
        lambda: rng.choice(tuple("xo" + "x" + c + "-" for c in "bpars"))
        + "-".join(rstr(rng, string.digits, rng.randint(10, 13)) for _ in range(2))
        + "-" + mixed(rng, B62, 24),
        lambda: "AIza" + mixed(rng, B64URL, 35),
        lambda: rng.choice(("sk", "rk", "pk")) + "_" + rng.choice(("live", "test")) + "_"
        + mixed(rng, B62, rng.randint(16, 32)),
        lambda: "glpat-" + mixed(rng, B64URL, rng.randint(20, 26)),
        lambda: "Bearer " + mixed(rng, B64URL + ".", rng.randint(24, 60)),
    )
    return makers[i % len(makers)]()


def jwt(rng: random.Random, _i: int) -> str:
    def part(n: int) -> str:
        raw = bytes(rng.randrange(256) for _ in range(n))
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": rng.choice(("HS256", "RS256", "ES256")), "typ": "JWT"}).encode()
    ).decode().rstrip("=")  # always starts with eyJ
    body = "eyJ" + part(rng.randint(30, 80))
    return f"{header}.{body}.{part(rng.randint(24, 48))}"


def pem(rng: random.Random, _i: int) -> str:
    label = rng.choice(("RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY",
                        "OPENSSH PRIVATE KEY", "CERTIFICATE"))
    lines = [rstr(rng, B62 + "+/", 64) for _ in range(rng.randint(2, 6))]
    return "-----BEGIN " + f"{label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----"


def card(rng: random.Random, i: int) -> str:
    length = (16, 16, 16, 15, 13, 19)[i % 6]
    head = rng.choice(("4", "51", "52", "53", "34", "37", "6011", "35"))
    number = luhn_complete(head + rstr(rng, string.digits, length - 1 - len(head)))
    style = rng.choice(("plain", "spaced", "dashed"))
    if style == "plain":
        return number
    sep = " " if style == "spaced" else "-"
    return sep.join(number[k:k + 4] for k in range(0, len(number), 4))


def assignment(rng: random.Random, _i: int) -> tuple[str, str]:
    key = rng.choice(("password", "passwd", "pwd", "secret", "token", "api_key", "api-key",
                      "apikey", "Password", "SECRET", "Token"))
    value = rng.choice((
        lambda: rstr(rng, string.ascii_lowercase, rng.randint(5, 10)) + rstr(rng, string.digits, 2),
        lambda: rstr(rng, B62 + "!#$%", rng.randint(8, 16)),
        lambda: rng.choice(("hunter", "correct", "letmein", "winter", "dragon")) + str(
            rng.randint(10, 9999)) + rng.choice("!#$"),
    ))()
    sep = rng.choice((": ", "=", " = ", ":"))
    quote = rng.choice(("", "", '"', "'"))
    if quote:
        value = value.replace(quote, "x")
    return f"{key}{sep}{quote}{value}{quote}", value


def entropy(rng: random.Random, i: int) -> str:
    if i % 4 == 0:  # long hex digests / keys that are not 40-char git SHAs
        return mixed(rng, "0123456789abcdef", rng.choice((32, 48, 64)))
    return mixed(rng, B62 + rng.choice(("", "-_", "+/")), rng.randint(20, 56))


def secrets(rng: random.Random) -> list[dict[str, str]]:
    out = []
    for kind in KINDS:
        for i in range(100):
            if kind == "assignment":
                shown, secret = assignment(rng, i)
            else:
                secret = {"api_key": api_key, "jwt": jwt, "pem": pem, "card": card,
                          "entropy": entropy}[kind](rng, i)
                shown = secret
            template = TEMPLATES[(i * 7 + KINDS.index(kind)) % len(TEMPLATES)] \
                if kind != "pem" else rng.choice(
                    [t for t in TEMPLATES if "\n" in t or t == "{S}" or t.endswith("{S}")])
            out.append({"text": template.replace("{S}", shown), "secret": secret, "kind": kind})
    return out


WORDS = ("the", "meeting", "tomorrow", "morning", "please", "review", "draft", "report", "budget",
         "team", "update", "thanks", "plan", "launch", "client", "invoice", "schedule", "garden",
         "coffee", "weather", "project", "deadline", "holiday", "dinner", "train", "ticket",
         "design", "feedback", "release", "roadmap", "sketch", "question", "answer", "paragraph")
LONG_WORDS = ("internationalization", "counterrevolutionaries", "uncharacteristically",
              "incomprehensibilities", "electroencephalography", "overenthusiastically",
              "pseudopseudohypoparathyroidism", "dichlorodifluoromethane", "institutionalization",
              "telecommunications", "misunderstandings", "unconstitutionally",
              "hippopotomonstrosesquippedaliophobia", "supercalifragilisticexpialidocious",
              "characteristically", "indistinguishability")
HOSTS = ("example.test", "docs.example.org", "shop.example.net", "blog.example.com")
DIRS = ("src", "docs", "tests", "usr", "home", "var", "etc", "lib", "projects", "notes")
FILES = ("report.txt", "main.py", "README.md", "budget_2024.xlsx", "notes-final.docx", "a.out")


def benign(rng: random.Random) -> list[dict[str, str]]:
    def prose() -> str:
        n = rng.randint(6, 18)
        s = " ".join(rng.choice(WORDS) for _ in range(n))
        return s[0].upper() + s[1:] + rng.choice((".", "!", "?"))

    def url() -> str:
        path = "/".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4)))
        q = f"?id={rng.randint(1, 99999)}&page={rng.randint(1, 40)}" if rng.random() < 0.4 else ""
        return f"See https://{rng.choice(HOSTS)}/{path}{q} for details."

    def sha() -> str:
        return f"Fixed in commit {rstr(rng, '0123456789abcdef', rng.randint(7, 12))}, please pull."

    def phone() -> str:
        return rng.choice((
            f"Call me on +1 {rng.randint(200, 999)} {rng.randint(200, 999)} {rng.randint(1000, 9999)}.",
            f"My number is 07{rng.randint(100, 999)} {rng.randint(100000, 999999)}.",
            f"Office: ({rng.randint(200, 999)}) {rng.randint(200, 999)}-{rng.randint(1000, 9999)}",
        ))

    def order() -> str:
        return rng.choice((
            f"Order #{rng.randint(10_000, 999_999_999)} has shipped.",
            f"Your invoice INV-{rng.randint(2020, 2026)}-{rng.randint(1, 9999):05d} is due.",
            f"Tracking number {rng.randint(10**8, 10**11)} is out for delivery.",
        ))

    def longword() -> str:
        return f"That was {rng.choice(LONG_WORDS)} difficult, honestly, and {rng.choice(LONG_WORDS)}."

    def path() -> str:
        p = "/".join(rng.choice(DIRS) for _ in range(rng.randint(2, 4))) + "/" + rng.choice(FILES)
        return f"Open /{p} and check the first lines."

    def slug() -> str:
        return "Read the post " + "-".join(rng.choice(WORDS) for _ in range(rng.randint(3, 7))) + "."

    makers = (prose, prose, url, sha, phone, order, longword, path, slug)
    out = []
    for i in range(400):
        out.append({"text": makers[i % len(makers)]()})
    rng.shuffle(out)
    return out


def write(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1] / "tests/acceptance/corpus")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    write(args.out / "secrets.jsonl", secrets(rng))
    write(args.out / "benign.jsonl", benign(rng))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
