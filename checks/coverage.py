"""Dated-sentence coverage: sentences on a page that name a future date or period and that no proposed row quotes.

    dated_sentences(words, today)           every sentence of the page that names a date or period later than today
    uncovered(sentences, quotes)            the ones no row's quote covers
    sentence_covered(text, quotes)          True when a quote holds the sentence, or the sentence holds a quote, or
                                            a quote holds nearly every word of it

Plain code, no model: the page's words are cut into sentences (at a full stop, question mark or exclamation mark
followed by a capital letter, a digit, a bullet or the end of the page; a leading bullet is dropped; common abbreviations such as "Inc." do not end one),
each sentence is searched for a date or period with the same parsers the date check uses (event_checks.parse_*),
and a sentence counts when the first day of a date or period it names is after `today`. A run of words with no
sentence end for more than MAX_WORDS words (a table, a list of figures) is not a sentence and is skipped.
This is how a person sees what the reader skipped: runs/coverage.py turns each uncovered sentence into an open row.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from checks.event_checks import _norm_quote, parse_exact_date, parse_period, vague_period_window

MAX_WORDS = 80
COVERED_SHARE = 0.8   # a quote that holds this share of a sentence's words covers it

_ABBREVIATIONS = {"inc.", "corp.", "co.", "ltd.", "no.", "u.s.", "dr.", "mr.", "ms.", "mrs.", "vs.", "e.g.", "i.e.",
                  "approx.", "st.", "mt.", "fig.", "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.",
                  "sept.", "oct.", "nov.", "dec."}
_INITIAL = re.compile(r"^[A-Z]\.$")
_END = re.compile(r"[.!?][\"')\]]*$")
_NEXT_STARTS = re.compile(r"^[\"'(\[]?[A-Z0-9]")
_BULLETS = {"·", "•", "●", "▪", "◦", "-", "–", "—", "*"}

_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_ORD = r"(?:first|second|third|fourth|1st|2nd|3rd|4th)"
# Where a date or period can be printed. Each match is handed to the date check's own parsers.
_MENTION = re.compile(
    rf"(?<![\w-])(?:"
    rf"{_MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}}"                             # March 12, 2027
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"                                        # 2027-03-12
    rf"|(?:(?:early|mid|late)[- ]?)?(?:q[1-4]|[1-4]q|{_ORD}\s+quarter(?:\s+of)?)\s*\d{{4}}"   # Q3 2027, mid-Q2 2027
    rf"|(?:h[12]|[12]h|{_ORD}\s+half(?:\s+of)?)\s*\d{{4}}"              # 2H 2027, second half of 2027
    rf"|(?:mid|early|late|end of|the end of|year[- ]end|beginning of)[- ]?\s*\d{{4}}"        # mid-2027
    rf"|{_MONTH}\.?,?\s+\d{{4}}"                                        # March 2027
    rf"|(?<=in )\d{{4}}|(?<=by )\d{{4}}|(?<=during )\d{{4}}|(?<=until )\d{{4}}|(?<=through )\d{{4}}"  # in 2027
    rf")(?![\w-])", re.I)


def period_start(text: str) -> date | None:
    """The first day of the date or period `text` names, or None when the parsers cannot read it."""
    d = parse_exact_date(text)
    if d is not None:
        return d
    p = parse_period(text)
    if p is not None and p[0] is not None:
        return p[0]
    w = vague_period_window(text)
    return w[0] if w is not None else None


def sentences(words: list[dict]) -> list[dict]:
    """The page's words cut into sentences: {text, word_ids, box}. Words are taken in the order the file holds
    them (reading order)."""
    out, cur = [], []

    def flush():
        if cur and len(cur) <= MAX_WORDS:
            out.append({"text": " ".join(w["text"] for w in cur), "word_ids": [w["id"] for w in cur],
                        "box": [min(w["x0"] for w in cur), min(w["y0"] for w in cur),
                                max(w["x1"] for w in cur), max(w["y1"] for w in cur)]})
        cur.clear()

    for i, w in enumerate(words):
        t = w["text"]
        if t in _BULLETS and not cur:
            continue                      # a bullet opens a sentence; it is not part of it
        cur.append(w)
        nxt = words[i + 1]["text"] if i + 1 < len(words) else None
        if _END.search(t) and t.lower() not in _ABBREVIATIONS and not _INITIAL.match(t) \
                and (nxt is None or _NEXT_STARTS.match(nxt) or nxt in _BULLETS):
            flush()
    flush()
    return out


def dated_sentences(words: list[dict], today: date) -> list[dict]:
    """Sentences that name a date or period starting after `today`: each is {text, word_ids, box, date_text,
    date, date_end} for the first such date or period in it."""
    out = []
    for s in sentences(words):
        for m in _MENTION.finditer(s["text"]):
            start = period_start(m[0])
            if start is not None and start > today:
                p = parse_period(m[0])
                end = p[1] if p is not None and p[1] is not None else (vague_period_window(m[0]) or (None, None))[1]
                out.append({**s, "date_text": m[0], "date": start.isoformat(),
                            "date_end": end.isoformat() if end else None})
                break
    return out


def sentence_covered(text: str, quotes: list[str | None]) -> bool:
    """True when a quote holds the whole sentence as a run of words, or holds COVERED_SHARE of the sentence's tokens
    counting repeats (a word that occurs three times in the sentence needs three in the quote to be covered three
    times). A quote that is only a piece of the sentence does not cover it."""
    s = _norm_quote(text).lower()
    tokens = Counter(s.split())
    total = sum(tokens.values())
    for q in quotes:
        if not q:
            continue
        nq = _norm_quote(q).lower()
        if f" {s} " in f" {nq} ":
            return True
        have = Counter(nq.split())
        if total and sum(min(n, have[t]) for t, n in tokens.items()) / total >= COVERED_SHARE:
            return True
    return False


def uncovered(found: list[dict], quotes: list[str | None]) -> list[dict]:
    return [s for s in found if not sentence_covered(s["text"], quotes)]
