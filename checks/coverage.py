"""Dated-sentence coverage: sentences and items on a page that name a future date or period and that no proposed
row quotes.

    dated_sentences(words, today)           one candidate per date or period a sentence names that starts after today
    uncovered(sentences, quotes)            the ones no row's quote covers
    sentence_covered(text, quotes)          True when a quote holds the sentence, or the sentence holds a quote, or
                                            a quote holds nearly every word of it

Plain code, no model: the page's words are cut into sentences and list items on marker evidence alone — a
sentence end (a full stop, question mark or exclamation mark followed by a capital letter, a digit, a bullet or
the end of the page, with smart quotes closing it too), a bullet glyph, a dash/star/'o' that opens a line or an
item, or a region break. A new block or a changed indent never cuts by itself, so a wrapped sentence stays
whole. Common abbreviations such as "Inc." do not end a sentence. Each piece is searched for a date or period
with the same parsers the date check uses (event_checks.parse_*), and a mention counts while it is in the one
window the date check defines (event_checks.in_window): a period is past only when its end is before `today`.
The candidates follow the action, not the date count: one piece per timing phrase the words support, and a
piece that still holds several phrases is one unresolved several-events candidate, never the first date for
all. A run of words with no boundary for more than MAX_WORDS words (a table, a list of figures) is not a
sentence and is skipped.
This is how a person sees what the reader skipped: runs/coverage.py turns each uncovered candidate into an open row.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from checks.event_checks import (_EXPAND_SUBS, _FIX_SUBS, _norm_quote, expand_short_year, fix_wording,
                                 in_window, parse_exact_date, parse_period, vague_period_window)

MAX_WORDS = 80
COVERED_SHARE = 0.8   # a quote that holds this share of a sentence's words covers it

_ABBREVIATIONS = {"inc.", "corp.", "co.", "ltd.", "no.", "u.s.", "dr.", "mr.", "ms.", "mrs.", "vs.", "e.g.", "i.e.",
                  "approx.", "st.", "mt.", "fig.", "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.",
                  "sept.", "oct.", "nov.", "dec."}
_INITIAL = re.compile(r"^[A-Z]\.$")
_END = re.compile(r"[.!?][\"'”’)\]]*$")
_NEXT_STARTS = re.compile(r"^[\"'“‘(\[]?[A-Z0-9]")
_GLYPH_BULLETS = {"·", "•", "●", "▪", "◦", "»", "‣", "■", "□", "▲", "►"}
_PUNCT_BULLETS = {"-", "–", "—", "*"}
_BULLETS = _GLYPH_BULLETS | _PUNCT_BULLETS
_SUB_BULLET = "o"             # slides print a lowercase 'o' as a sub-bullet — a bullet at a line or item start,
                              # a letter anywhere else ("H e m a t o c r i t" axis labels spell a word with it)

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

# A bare year is a mention only when a list joins it to a real one ("in 2027 and 2028, respectively"): the text
# between them may hold only and/or/,/;/& and whitespace. It never counts alone — a column of years is not a date.
_BARE_YEAR = re.compile(r"(?<![\w-])\d{4}(?![\w-])")
_CONNECTIVES = re.compile(r"(?:\s|,|;|&|\band\b|\bor\b)+")
_CONNECTOR = re.compile(r",|;|&|\band\b|\bor\b")

# Between two mentions, list connectives make one shared timing list ("2027 and 2028"); a range word makes one
# interval ("Q1 2027 through Q3 2027"); anything else ends the timing phrase but the default there is STILL one
# action — only a new subject splits it. Qualifier words keep company with a phrase, they are not an action.
_LIST_WORDS = {"and", "or", ",", ";", "&"}
_RANGE_WORDS = {"through", "to", "until", "til"}
_FILLER_WORDS = {"and", "or", "to", "through", "until", "til", "from", "in", "of", "on", "at", "for", "by",
                 "with", "as", "the", "a", "an", "respectively"}
_QUALIFIER_WORDS = {"approximately", "about", "around", "roughly", "either", "later", "earlier", "possibly",
                    "potentially", "maybe", "expected"}

# What counts as a program name for GUESSING — ONE definition, shared with runs/coverage.guess_program: a
# brand-shaped token (PN-200, IMCIVREE) that is not a regulator, venue or quarter.
_NOT_PROGRAMS = {"FDA", "NDA", "BLA", "MAA", "IND", "PDUFA", "EU", "US", "USA", "UK", "CHMP", "EMA", "NASDAQ", "LLC",
                 "INC", "CEO", "CFO", "COO", "CMO", "SEC", "HO", "IV", "II", "III", "PK", "PD", "AND", "THE", "FOR",
                 "NEJM", "ASCO", "EHA", "ASH", "ESMO", "AACR"}
_BRAND = re.compile(r"^(?:[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*|[A-Z]{2,}-\d+[A-Za-z]*|[A-Z][a-z]+[A-Z][A-Za-z]*)$")
_QUARTERISH = re.compile(r"^(?:\d[A-Z]\d*|[A-Z]\d)$")


def _program_token(t: str) -> bool:
    return bool(_BRAND.match(t) and t.upper() not in _NOT_PROGRAMS and not _QUARTERISH.match(t))


# A known-program list identifies names, not actions (Operator ruling, Job 2j): a sentence or bullet with
# two or more timing phrases is ONE unresolved candidate — within-sentence splitting was removed entirely.
# Explicit sentence/bullet boundaries still separate candidates.


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
    """The page's words cut into sentences and items: {text, word_ids, box}. Words are taken in the order the
    file holds them (reading order). A cut needs marker evidence — a sentence end, a bullet, a region break;
    a new block or a changed indent alone never cuts, so a wrapped sentence stays whole whatever the layout."""
    out, cur = [], []
    line = None                         # the (block, line) we are on: only to see where a line starts

    def flush():
        if cur and len(cur) <= MAX_WORDS:
            out.append({"text": " ".join(w["text"] for w in cur), "word_ids": [w["id"] for w in cur],
                        "box": [min(w["x0"] for w in cur), min(w["y0"] for w in cur),
                                max(w["x1"] for w in cur), max(w["y1"] for w in cur)]})
        cur.clear()

    for i, w in enumerate(words):
        t = w["text"]
        if w.get("break"):
            flush()                       # a region break ends the sentence; a box never spans two regions
            line = None
            continue
        key = (w.get("block"), w.get("line")) if w.get("line") is not None else None
        at_line_start = key is not None and key != line
        if at_line_start:
            line = key
        if t in _GLYPH_BULLETS or (t in _PUNCT_BULLETS or t == _SUB_BULLET) and (at_line_start or not cur):
            flush()                       # a bullet ends the item before it; it is not part of the next one
            continue                      # (a dash, star or 'o' counts at a line or item start only)
        cur.append(w)
        nxt = words[i + 1]["text"] if i + 1 < len(words) and not words[i + 1].get("break") else None
        if _END.search(t) and t.lower() not in _ABBREVIATIONS and not _INITIAL.match(t) \
                and (nxt is None or _NEXT_STARTS.match(nxt) or nxt in _BULLETS or nxt == _SUB_BULLET):
            flush()
    flush()
    return out


def _connected(gap: str) -> bool:
    """True when the gap between two date mentions holds only list connectives, and at least one connector."""
    return bool(_CONNECTIVES.fullmatch(gap)) and bool(_CONNECTOR.search(gap))


def _expanded(text: str) -> tuple[str, list[int]]:
    """The text as the date parsers read it — _EXPAND_SUBS then _FIX_SUBS, the one catalogue — and for every
    position in the expanded text the position in `text` it came from. The map is built while the subs run, so
    each printed label keeps its own occurrence: "Q1 '27 Q1 2027" gives two different spans."""
    amap = list(range(len(text) + 1))
    out = text
    for pattern, repl in _EXPAND_SUBS + _FIX_SUBS:
        pieces, nmap, pos = [], [], 0
        for m in pattern.finditer(out):
            pieces.append(out[pos:m.start()]); nmap += amap[pos:m.start()]
            rep = m.expand(repl) if isinstance(repl, str) else repl(m)
            pieces.append(rep); nmap += [amap[m.start()]] * len(rep)
            pos = m.end()
        pieces.append(out[pos:]); nmap += amap[pos:]
        out, amap = "".join(pieces), nmap
    return out, amap


def _mapped_spans(text: str, expanded: str, amap: list[int], pattern: re.Pattern) -> list[tuple[int, int]]:
    """Pattern matches in `expanded` mapped back to the printed spans in `text` through `amap`."""
    spans = []
    for m in pattern.finditer(expanded):
        a, b = amap[m.start()], amap[m.end()]
        a += len(text[a:b]) - len(text[a:b].lstrip(" \t([{"))
        b = a + len(text[a:b].rstrip(" \t)]}.,;:!?"))
        spans.append((a, b))
    return spans


def _mentions(text: str) -> list[tuple[int, int]]:
    """The spans of the date or period mentions in `text`: scanned on the text as the parsers see it
    (expand_short_year + fix_wording — '27, ’27, 1Q27, YE’26, CY2027 — the one catalogue), so coverage finds a
    mention wherever the date check would read one. Plus bare years a list joins to one of them ("in 2027 and
    2028" counts 2028; "2026 2025" in a table row counts nothing)."""
    expanded, amap = _expanded(text)
    spans = [(a, b, True) for a, b in _mapped_spans(text, expanded, amap, _MENTION)]
    spans += [(a, b, False) for a, b in _mapped_spans(text, expanded, amap, _BARE_YEAR)
              if not any(x < b and a < y for x, y, _ in spans)]
    spans.sort()
    qualified = {i for i, (_, _, q) in enumerate(spans) if q}
    grown = True
    while grown:
        grown = False
        for i, (a, b, _) in enumerate(spans):
            if i in qualified:
                continue
            if (i > 0 and i - 1 in qualified and _connected(text[spans[i - 1][1]:a])) \
                    or (i + 1 < len(spans) and i + 1 in qualified and _connected(text[b:spans[i + 1][0]])):
                qualified.add(i)
                grown = True
    return [(a, b) for i, (a, b, _) in enumerate(spans) if i in qualified]


def _gap_kind(gap: str) -> str | None:
    """"range" when a range word (with only fillers or qualifiers around it) stands between two mentions —
    "Q1 2027 through approximately Q3 2027" is still one interval; "list" when only list connectives do, or
    between adjacent mentions. Either way the mentions make one timing phrase. None when the gap holds real
    words — but even then the default is same action, just a new phrase."""
    toks = [t.strip(",;&()") for t in gap.split() if t.strip(",;&()")]
    if not toks:
        return "list"
    if all(t in _LIST_WORDS | _FILLER_WORDS | _QUALIFIER_WORDS for t in toks):
        return "range" if any(t in _RANGE_WORDS for t in toks) else "list"
    return None


def _groups(text: str, ms: list[tuple]) -> list[dict]:
    """Mentions joined only by list connectives or a range word make one timing phrase: {"ms": spans,
    "interval": True when a range word links them}. A gap holding real words starts a new phrase."""
    groups = []
    prev = None
    for m in ms:
        kind = _gap_kind(text[prev[1]:m[0]]) if prev is not None else None
        if kind is None:
            groups.append({"ms": [m], "interval": False})
        else:
            groups[-1]["ms"].append(m)
            groups[-1]["interval"] |= kind == "range"
        prev = m
    return groups


def dated_sentences(words: list[dict], today: date) -> list[dict]:
    """The page's candidates: {text, word_ids, box, date_text, date, date_end, dates, several} — one per
    sentence or item the words support, never only the first date for all of them.

    A sentence holding one qualifying mention is one candidate. Mentions joined only by list connectives or
    a range word make one timing phrase: "Q1 2027 through Q3 2027" is one interval candidate; "2027 and
    2028" is a shared list, not two actions. A sentence or bullet that still holds several timing phrases —
    a respectively-list, a slide's date column, two actions of one program, a conditional second date — is
    ONE unresolved candidate (`several` is set): a program list names programs, not actions, and code does
    not pair actions to dates inside a sentence. It keeps the whole passage for a person instead of
    stamping the first date on every copy."""
    out = []
    for s in sentences(words):
        ms = []
        for a, b in _mentions(s["text"]):
            mention = s["text"][a:b]
            start = period_start(mention)
            end = parse_exact_date(mention)
            if end is None:
                p = parse_period(mention)
                end = p[1] if p is not None and p[1] is not None else (vague_period_window(mention) or (None, None))[1]
            if start is None or not in_window(start, end, today, date.max):
                continue                    # the ONE window rule: a period is past only when its end is before the run date
            ms.append((a, b, mention, start, end))
        if not ms:
            continue
        gs = _groups(s["text"], ms)
        pms = [m for g in gs for m in g["ms"]]
        several = len(pms) > 1 and not (len(gs) == 1 and gs[0]["interval"])
        first, last = pms[0], pms[-1]
        if several:
            start, ends = min(m[3] for m in pms), [m[4] for m in pms if m[4] is not None]
            end = max(ends) if ends else None
        else:
            start, end = first[3], last[4]
        out.append({**s, "date_text": first[2] if len(pms) == 1 else s["text"][first[0]:last[1]],
                    "date": start.isoformat(), "date_end": end.isoformat() if end else None,
                    "dates": [m[2] for m in pms], "several": several})
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
