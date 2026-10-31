"""The open rows for dated sentences the reader skipped (checks/coverage.py finds them).

    possible_event_rows(words_doc, rows, source, ...)   one open row per uncovered sentence

Each is a row a person can see in Judgment needed: title "Possible event not read: <sentence>", status
"undecided", group "open", the sentence as its quote with its box, the first future date or period it names, no
checks (nothing proposed it, so nothing was checked) and `found_by: "coverage"`. The trail says plainly that no
model was asked: code found it. Scoring never counts these rows as proposals (accuracy/score.py).
"""

from __future__ import annotations

import re
from datetime import date

from checks import coverage
from checks.event_checks import EVENT_WORDS, TYPE_PRIORITY, parse_exact_date

SCAN_WHO = "checks.coverage"
TITLE = "Possible event not read: "
TITLE_MAX = 240

# ---- Cleaning: a slide's sentence arrives with its page number, footnote marks and trademark signs attached.
_SYMBOLS = re.compile(r"[®™©℠†‡§¶¹²³•●▪◦■□▲►]+")
_NUM_TOKEN = re.compile(r"^(\d{1,3})\.?$")
_FOOTNOTE_TOKEN = re.compile(r"^(\d{1,2})\.?$")
SMALL_SHARE = 0.7     # a footnote mark's box is under this share of its neighbour's height, or sits above its baseline
BASELINE_TOL = 0.1    # of the neighbour's height


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", _SYMBOLS.sub(" ", s)).strip()
    s = re.sub(r"^[^\w\"'(\[]+", "", s)                       # stray leading punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[\s,;:*]+$", "", s)


def _is_break(w: dict | None) -> bool:
    return w is None or bool(w.get("break"))


def _superscript(w: dict, nb: dict) -> bool:
    h, nh = w["y1"] - w["y0"], nb["y1"] - nb["y0"]
    if nh <= 0:
        return False
    return h < SMALL_SHARE * nh or w["y1"] < nb["y1"] - BASELINE_TOL * nh


def clean_sentence(text: str, words: list[dict] | None = None, page_words: list[dict] | None = None) -> str:
    """The sentence without (c)/TM signs and bullet glyphs, page numbers and footnote marks, spaces collapsed. The
    sentence's own full stop stays (the quote must still match the page).

    `words`: the sentence's words (bbox, as coverage.sentences gives them); `page_words`: all of the page's words in
    order, with their break flags. With words, numbers are found by geometry, never by text: a token is the page
    number only when a line break stands before AND after it (or it opens/closes the page's words); a token is a
    footnote mark only when it is 1-2 digits and its box is under 70% of its neighbour's height or its baseline sits
    above the neighbour's. Any other number stays. Without words only symbols are stripped, nothing numeric."""
    if not words:
        return _tidy(text or "")
    pw = page_words or words
    pos = {w["id"]: i for i, w in enumerate(pw)}
    keep = []
    for i, w in enumerate(words):
        t = w["text"]
        if _NUM_TOKEN.match(t):
            j = pos.get(w["id"])
            if j is not None and _is_break(pw[j - 1] if j > 0 else None) and _is_break(pw[j + 1] if j + 1 < len(pw) else None):
                continue                                            # page number: set apart by line breaks
            nb = words[i - 1] if i > 0 else (words[i + 1] if len(words) > 1 else None)
            if nb is not None and _FOOTNOTE_TOKEN.match(t) and _superscript(w, nb):
                continue                                            # footnote mark: smaller or raised
        keep.append(t)
    return _tidy(" ".join(keep))


# ---- Guesses: what code can see in the sentence. Each is a guess a person confirms; what it cannot see stays None.
_NOT_PROGRAMS = {"FDA", "NDA", "BLA", "MAA", "IND", "PDUFA", "EU", "US", "USA", "UK", "CHMP", "EMA", "NASDAQ", "LLC",
                 "INC", "CEO", "CFO", "COO", "CMO", "SEC", "HO", "IV", "II", "III", "PK", "PD", "AND", "THE", "FOR",
                 "NEJM", "ASCO", "EHA", "ASH", "ESMO", "AACR"}
_VERBS = re.compile(r"\b(?:is|are|was|were|will|expected|expects?|planned|plans?|anticipated|scheduled|set|to|"
                    r"on track|targeted|begins?|starts?|announces?)\b", re.I)
_BRAND = re.compile(r"^(?:[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*|[A-Z]{2,}-\d+[A-Za-z]*|[A-Z][a-z]+[A-Z][A-Za-z]*)$")
_QUARTERISH = re.compile(r"^(?:\d[A-Z]\d*|[A-Z]\d)$")


def guess_program(sentence: str, known: list[str] | None = None) -> str | None:
    """A known program of the run named in the sentence; else the first all-caps or brand-cased word before the
    verb ("IMCIVREE HO Country-level Launches Expected in 2027" -> IMCIVREE); else None."""
    tokens = [t.strip("()[]\",.;:") for t in sentence.split()]
    for k in known or []:
        for t in tokens:
            if k and t.lower() == k.lower():
                return t
    m = _VERBS.search(sentence)
    head = (sentence[:m.start()] if m else sentence).split()
    for t in (t.strip("()[]\",.;:") for t in head):
        if _BRAND.match(t) and t.upper() not in _NOT_PROGRAMS and not _QUARTERISH.match(t):
            return t
    return None


_EXTRA_WORDS = {"milestone": ("initiat", "dos", "first patient", "enroll")}   # "initiation", "dosing", "enrolling" ...


def guess_type(sentence: str) -> str | None:
    """The event type the sentence's words point at (the app's own words table, checks.event_checks.EVENT_WORDS):
    the first of TYPE_PRIORITY with a word in the sentence, else a milestone word, else None."""
    for t in (*TYPE_PRIORITY, "milestone"):
        if any(re.search(rf"(?<![\w-]){re.escape(w)}(?:e?s|ed|ing)?(?![\w-])", sentence, re.I)
               for w in (*EVENT_WORDS[t], *_EXTRA_WORDS.get(t, ()))):
            return t
    return None


def in_words(text: str | None) -> str | None:
    """An exact day ('2026-08-04', 'August 4, 2026') as '4 Aug 2026'; a period or anything else as printed."""
    d = parse_exact_date(text) if text else None
    return f"{d.day} {d.strftime('%b %Y')}" if d is not None else text


def possible_event_rows(words_doc: dict, rows: list[dict], source: dict, *, first_index: int, at: str,
                        today: date, ticker: str, company: str) -> list[dict]:
    """`rows`: the rows the reader proposed from this page (their quotes cover the sentences they came from).
    `today`: a date or period must start after it to count."""
    found = coverage.dated_sentences(words_doc["words"], today)
    quotes = [r.get("quote") for r in rows if r.get("found_by") != "coverage" and r.get("status") != "unreadable"]
    page = source["pages"][0]["page_number"]
    out = []
    known = sorted({r["program"] for r in rows if r.get("program")})
    today_words = f"{today.day} {today.strftime('%b %Y')}"
    for i, s in enumerate(coverage.uncovered(found, quotes)):
        sw = {w["id"]: w for w in words_doc["words"]}
        clean = clean_sentence(s["text"], [sw[i] for i in s["word_ids"]], words_doc["words"]) or s["text"]
        named = in_words(s["date_text"])
        title = TITLE + clean
        out.append({
            "id": f"e{first_index + i}", "ticker": ticker, "company": company, "event_type": "unknown",
            "title": title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…",
            "date_text": s["date_text"], "date": s["date"], "date_end": s["date_end"], "quote": clean,
            "raw_sentence": s["text"], "kind": "missed",
            "status_text": f"Both readers skipped this sentence. It names {named}. Is it an event?",
            "guess": {"date_text": s["date_text"], "date": s["date"], "date_end": s["date_end"],
                      "program": guess_program(clean, known), "event_type": guess_type(clean)},
            "program": None, "indication": None, "source_id": source["id"], "page_number": page, "box": s["box"],
            "proposed_by": "Dated-sentence coverage (code)", "found_by": "coverage", "status": "undecided",
            "group": "open",
            "reason": (f"The reader did not turn this sentence into a row. It names {named}, a date or "
                       f"period after {today_words}. Look at it: keep it as an event, fix it, or remove it."),
            "sample": False, "checks": [],
            "trail": [
                {"step": "Instructions sent", "tag": "Code", "who": SCAN_WHO, "at": at,
                 "text": (f"No model was asked. Code cut the page's words into sentences, looked in each for a "
                          f"date or period starting after {today_words}, and compared each with the quotes of "
                          f"the rows the reader proposed.")},
                {"step": "Found", "tag": "Code", "who": SCAN_WHO, "at": at,
                 "text": f"No proposed row quotes this sentence. It names {named}."},
            ],
        })
    return out
