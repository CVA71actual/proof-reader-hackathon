"""Code checks for one catalyst event row, and the per-row status.

The citation box is checked with the same selector_match used for table values. These add:
    quote_match    "Quote found on page"         the quote is printed on the cited page, word for word
    date_match     "Date read as written"        the date the reader gave is the date printed in the quote;
                                                  the date must sit in the part of the quote that names the
                                                  event, not negated or replaced there; a period needs a year
    source_rule    "Source is more than a calendar"  a third-party calendar is never a source on its own

Row statuses (same words as table values, plus two):
    matched     no check failed, nothing was left unrun, at least one check passed, and no second
                reader disagreed
    failed      at least one check failed
    undecided   no check failed, but a second reader disagreed with the first reading
    unreadable  the reader's answer for this row could not be understood
    proposed    anything else: the reader's row stands as a proposal
"""

from __future__ import annotations

import re
from datetime import date

from checks.common import COULD_NOT_RUN, FAIL, NOT_NEEDED, PASS, CheckResult, join_words, normalize

QUOTE_ID, QUOTE_LABEL = "quote_match", "Quote found on page"
DATE_ID, DATE_LABEL = "date_match", "Date read as written"
SOURCE_ID, SOURCE_LABEL = "source_rule", "Source is more than a calendar"

THIRD_PARTY_CALENDAR = "third_party_calendar"

MATCHED, FAILED, PROPOSED, UNREADABLE, UNDECIDED = "matched", "failed", "proposed", "unreadable", "undecided"


# ---------------------------------------------------------------- quote


def _norm_quote(text: str | None) -> str:
    # Quotes may carry typographic quote marks or a trailing full stop the page prints differently.
    s = normalize(text).replace("’", "'").replace("“", '"').replace("”", '"')
    return s


def quote_match(quote: str | None, words: list[dict]) -> CheckResult:
    """Pass if the quote equals some run of consecutive page words (any length)."""
    want = _norm_quote(quote)

    def result(res: str, reason: str, found=None, word_ids=None) -> CheckResult:
        compared = {"value": want, "found": found}
        if word_ids is not None:
            compared["word_ids"] = word_ids
        return CheckResult(QUOTE_ID, QUOTE_LABEL, res, reason, compared)

    if not words:
        return result(COULD_NOT_RUN, "The page has no text layer to compare against.")
    if not want:
        return result(COULD_NOT_RUN, "The reader gave no quote for this row.")
    n = len(want.split(" "))
    for i in range(0, len(words) - n + 1):
        run = words[i : i + n]
        if _norm_quote(join_words(run)) == want:
            return result(PASS, "The quote is printed on the page, word for word.",
                          found=join_words(run), word_ids=[w["id"] for w in run])
    return result(FAIL, "The quote is not printed anywhere on the page.")


# ---------------------------------------------------------------- date

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_LONG = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_exact_date(text: str | None) -> date | None:
    """'March 12, 2027', 'Mar. 12 2027' or '2027-03-12' -> date. Anything vaguer -> None."""
    s = normalize(text).rstrip(".")
    m = _ISO.match(s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = _LONG.match(s)
        if m and m[1].lower() in _MONTHS:
            return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    except ValueError:
        return None
    return None


_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_QUARTER = re.compile(r"^(?:q([1-4])|([1-4])q|(first|second|third|fourth|1st|2nd|3rd|4th) quarter(?: of)?)"
                      r"\s*(\d{4})$")
_HALF = re.compile(r"^(?:h([12])|([12])h|(first|second|1st|2nd) half(?: of)?)\s*(\d{4})$")
_MONTH_YEAR = re.compile(r"^([a-z]+)\.?,?\s+(\d{4})$")
_YEAR = re.compile(r"^(?:full[- ]year |fiscal year |fy\s*)?(\d{4})$")
_VAGUE = re.compile(r"^(?:mid|early|late|end of|the end of|year[- ]end|beginning of|the beginning of)[- ]?\s*(\d{4})$")
_QUARTER_START = {1: (1, 3, 31), 2: (4, 6, 30), 3: (7, 9, 30), 4: (10, 12, 31)}


def parse_period(text: str | None) -> tuple[date | None, date | None] | None:
    """A named period -> (first day, last day). 'Q3 2027' -> (2027-07-01, 2027-09-30).

    Quarters, halves, months and years have fixed first and last days. Vaguer periods
    ('mid-2027', 'early 2027', 'late 2027') return (None, None): the words name a period but
    not where it starts or ends. Anything else (an exact day, 'later this year') -> None.
    """
    s = normalize(text).rstrip(".").lower()
    s = re.sub(r"^(?:in |during |by )?(?:the )?", "", s)
    m = _QUARTER.match(s)
    if m:
        q = int(m[1] or m[2] or _ORDINAL[m[3]])
        y = int(m[4])
        first, last, end_day = _QUARTER_START[q]
        return date(y, first, 1), date(y, last, end_day)
    m = _HALF.match(s)
    if m:
        h = int(m[1] or m[2] or _ORDINAL[m[3]])
        y = int(m[4])
        return (date(y, 1, 1), date(y, 6, 30)) if h == 1 else (date(y, 7, 1), date(y, 12, 31))
    m = _MONTH_YEAR.match(s)
    if m and m[1] in _MONTHS:
        y, mo = int(m[2]), _MONTHS[m[1]]
        nxt = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
        return date(y, mo, 1), date.fromordinal(nxt.toordinal() - 1)
    m = _YEAR.match(s)
    if m:
        y = int(m[1])
        return date(y, 1, 1), date(y, 12, 31)
    if _VAGUE.match(s):
        return None, None
    return None


_VAGUE_WINDOW = re.compile(r"^(mid|early|beginning of|the beginning of|late|end of|the end of|year[- ]end)[- ]?\s*(\d{4})$")
_WINDOW_MONTHS = {"early": (1, 4), "beginning of": (1, 4), "the beginning of": (1, 4), "mid": (5, 8),
                  "late": (9, 12), "end of": (9, 12), "the end of": (9, 12), "year-end": (9, 12),
                  "year end": (9, 12)}
_WINDOW_WORDS = {(1, 4): "January to April", (5, 8): "May to August", (9, 12): "September to December"}


def vague_period_window(text: str | None) -> tuple[date, date, str] | None:
    """The rough window a vague period stands for: early = January to April, mid = May to August,
    late / end of = September to December. 'mid-2027' -> (2027-05-01, 2027-08-31, 'May to August')."""
    m = _VAGUE_WINDOW.match(_canon(text))
    if not m:
        return None
    first, last = _WINDOW_MONTHS[m[1]]
    y = int(m[2])
    nxt = date(y + 1, 1, 1) if last == 12 else date(y, last + 1, 1)
    return date(y, first, 1), date.fromordinal(nxt.toordinal() - 1), _WINDOW_WORDS[(first, last)]


def _canon(text: str | None) -> str:
    s = normalize(text).rstrip(".").lower()
    return re.sub(r"^(?:in |during |by )?(?:the )?", "", s)


# Every date or period a quote can name, longest form first, so '2027' inside 'Q3 2027' is not read
# as the bare year.
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD_WORDS = "first|second|third|fourth|1st|2nd|3rd|4th"
_NAMED = re.compile(
    r"\b(?:"
    rf"(?:{_MONTH_WORDS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}"
    rf"|(?:q[1-4]|[1-4]q|(?:{_ORD_WORDS}) quarter(?: of)?)\s*\d{{4}}"
    r"|(?:h[12]|[12]h|(?:first|second|1st|2nd) half(?: of)?)\s*\d{4}"
    r"|(?:mid|early|late|the end of|end of|year[- ]end|the beginning of|beginning of)[- ]?\s*\d{4}"
    rf"|(?:{_MONTH_WORDS})\.?,?\s+\d{{4}}"
    r"|(?:full[- ]year |fiscal year |fy\s*)?\d{4}"
    r")\b", re.I)
_YEARLESS = re.compile(
    rf"^(?:q[1-4]|[1-4]q|h[12]|[12]h|(?:{_ORD_WORDS}) (?:quarter|half)|mid|early|late|end of|"
    rf"beginning of|year[- ]end|{_MONTH_WORDS})$")
# Words that say a date is not, or is no longer, the event's date, anywhere in its clause.
_NEGATION = re.compile(r"\b(?:not|no longer|never|cannot|previous(?:ly)?|prior|superseded)\b|n't\b", re.I)
# Words that move the event to the date that follows them ("now expected in Q4 2027"). They speak against a
# date only when that date comes before them, or the date after them is a different one.
_MOVED = re.compile(r"\b(?:now expected|moved to|delayed|pushed)\b", re.I)
# A date "introduced" as the new one: now / to / until / into in the three words before it, or "instead" just
# after it. A date after "instead of", "rather than" or "from" is the old one.
_NEW_BEFORE = re.compile(r"\b(?:now|to|until|into)\b", re.I)
_OLD_BEFORE = re.compile(r"\b(?:instead of|rather than|from)\s*$", re.I)
_NEW_AFTER = re.compile(r"^\s*instead\b(?!\s+of)", re.I)

# Clause boundaries: sentence ends, ; : , and the joining words below. A reference clause (one that begins
# "follow", "compared with", "versus", "than", "relative to") names a comparison, never the event.
_SPLIT_AFTER = re.compile(r"[.!?](?=\s)|[;:,]")
_SPLIT_BEFORE = re.compile(r"(?<=\s)(?:that|which|and|but|while|follow(?:s|ed|ing)?|compared (?:with|to)|"
                           r"versus|vs\.?|than|relative to)\b", re.I)
_REFERENCE = re.compile(r"^(?:follow(?:s|ed|ing)?|compared (?:with|to)|versus|vs\.?|than|relative to)\b", re.I)

# The event words for each kind of event (a small fixed list). The row's event_type picks one or more.
_ANCHORS = {
    "trial results": ("results", "data", "readout", "topline", "report"),
    "regulatory decision": ("pdufa", "decision", "approval", "crl", "action date"),
    "launch": ("launch", "commercial availability"),
    "conference": ("present", "presentation", "conference", "late-breaker"),
    "filing": ("submit", "submission", "filing", "nda", "bla", "maa"),
    "milestone": ("dosed", "enrolled", "initiated", "completed", "first patient"),
}
_TYPE_KEYS = (
    (("trial", "readout", "result", "topline", "data"), "trial results"),
    (("regulatory_decision", "decision", "approval", "pdufa", "crl"), "regulatory decision"),
    (("launch",), "launch"),
    (("conference", "presentation", "meeting_presentation"), "conference"),
    (("filing", "submission"), "filing"),
    (("milestone", "dosing", "enrollment", "initiation"), "milestone"),
)
_UNSURE = "Could not tell which date in the quote belongs to the event."


def event_anchors(event_type: str | None, title: str | None = None, program: str | None = None) -> list[str]:
    """The words that mark the part of a quote about this event: the fixed words for its event_type, plus the
    longest word (4 letters or more) of the row's title and the drug or program name, if given."""
    words: list[str] = []
    et = (event_type or "").lower()
    for keys, group in _TYPE_KEYS:
        if any(k in et for k in keys):
            words += [w for w in _ANCHORS[group] if w not in words]
    longest = max(re.findall(r"[A-Za-z]{4,}", title or ""), key=len, default=None)
    for extra in (longest, (program or "").strip() or None):
        if extra and extra.lower() not in words:
            words.append(extra.lower())
    return words


def _anchor_in(text: str, anchors: list[str]) -> str | None:
    for a in anchors:
        if re.search(r"(?<![A-Za-z])" + re.escape(a), text, re.I):
            return a
    return None


def _clauses(q: str, mentions: list[re.Match]) -> list[tuple[int, int]]:
    """Split the quote into clauses; a boundary never falls inside a date ('March 12, 2027' stays whole)."""
    cuts = {m.end() for m in _SPLIT_AFTER.finditer(q)} | {m.start() for m in _SPLIT_BEFORE.finditer(q)}
    cuts = sorted(c for c in cuts if 0 < c < len(q) and not any(m.start() < c < m.end() for m in mentions))
    edges = [0, *cuts, len(q)]
    return [(a, b) for a, b in zip(edges, edges[1:]) if q[a:b].strip()]


def _is_new(q: str, m: re.Match, start: int) -> bool:
    before = q[start:m.start()].split()[-3:]
    return bool(_NEW_BEFORE.search(" ".join(before))) or bool(_NEW_AFTER.match(q[m.end():]))


def _is_old(q: str, m: re.Match, start: int) -> bool:
    return bool(_OLD_BEFORE.search(q[start:m.start()]))


def _supporting_clause(dt: str, q: str, anchors: list[str]) -> tuple[str, str, str | None] | None:
    """Which clause of the quote gives the event's date, and what it says about date_text.

    Returns None when the clause supports date_text, else (result, reason, found)."""
    mentions = list(_NAMED.finditer(q))
    want = _canon(dt)
    clauses = []
    for a, b in _clauses(q, mentions):
        text = q[a:b]
        ms = [m for m in mentions if a <= m.start() < b]
        clauses.append({"start": a, "text": text, "mentions": ms, "anchor": _anchor_in(text, anchors),
                        "reference": bool(_REFERENCE.match(text.strip()))})
    events = [c for c in clauses if c["anchor"] and not c["reference"] and c["mentions"]]
    holding = [c for c in clauses if any(_canon(m[0]) == want for m in c["mentions"])]
    own = next((c for c in holding if c in events), holding[0])
    if not events:
        return COULD_NOT_RUN, _UNSURE, None
    dates = {_canon(m[0]) for c in events for m in c["mentions"]}
    if len(events) > 1 and dates != {want}:
        return COULD_NOT_RUN, _UNSURE, None
    if own not in events:
        event = events[0]
        names = [m[0] for m in event["mentions"]]
        new = [m[0] for m in event["mentions"] if _is_new(q, m, event["start"])] or names
        other = " and ".join(dict.fromkeys(new))
        if _NEGATION.search(own["text"]) or _MOVED.search(own["text"]):
            return FAIL, f"The quote says {dt} is no longer the expected period; it now names {other}.", other
        role = "a comparison" if own["reference"] else "not the date the quote gives for the event"
        return FAIL, f"The quote attaches {other} to the {event['anchor']}; {dt} is {role}.", other
    for c in events:
        verdict = _judge_clause(dt, q, c)
        if verdict is not None:
            return verdict
    return None


def _judge_clause(dt: str, q: str, c: dict) -> tuple[str, str, str | None] | None:
    """The clause names the event and holds date_text: is date_text negated or replaced there?"""
    want = _canon(dt)
    start, text = c["start"], c["text"]
    hit = next(m for m in c["mentions"] if _canon(m[0]) == want)
    others = [m for m in c["mentions"] if _canon(m[0]) != want]
    if others and _NEGATION.search(text):
        # A denied move does not establish either endpoint as the event date.
        # Resolve this before interpreting "from" / "to" as an actual transition.
        return COULD_NOT_RUN, _UNSURE, None
    if _is_old(q, hit, start):
        names = " and ".join(dict.fromkeys(m[0] for m in others)) or None
        reason = f"The quote says {dt} is no longer the expected period" + (f"; it now names {names}." if names
                                                                             else ".")
        return FAIL, reason, names or hit[0]
    if others:
        new_others = [m for m in others if _is_new(q, m, start) and not _is_old(q, m, start)]
        dt_new = _is_new(q, hit, start)
        if new_others and not dt_new:
            names = " and ".join(dict.fromkeys(m[0] for m in new_others))
            return FAIL, f"The quote says {dt} is no longer the expected period; it now names {names}.", names
        if dt_new and not new_others:
            return None
        return COULD_NOT_RUN, _UNSURE, None
    if _NEGATION.search(text):
        return FAIL, f"The quote says the event is not in {dt}.", hit[0]
    for mv in _MOVED.finditer(text):
        if start + mv.start() > hit.start():
            return FAIL, f"The quote says {dt} is no longer the expected period.", hit[0]
    return None




def vague_period_year(text: str | None) -> int | None:
    """The year a vague period names ('mid-2027' -> 2027), or None if it names no year."""
    s = normalize(text).rstrip(".").lower()
    s = re.sub(r"^(?:in |during |by )?(?:the )?", "", s)
    m = _VAGUE.match(s)
    return int(m[1]) if m and m.lastindex and m[1] else None


def date_match(date_text: str | None, date_iso: str | None, quote: str | None,
               date_end: str | None = None, *, event_type: str | None = None, title: str | None = None,
               program: str | None = None) -> CheckResult:
    """Pass if date_text is printed in the quote and the row's date says the same thing.

    An exact day ('March 12, 2027'): the row's date must be that day and date_end must be empty.
    A period ('Q3 2027', 'second half of 2027', 'mid-2027'): there is no exact day to compare, so the
    check passes when the period words are printed in the quote and the row's date and date_end, if
    given, are the first and last day of that period. A vague period ('mid-2027') has no first or
    last day, so date and date_end must fall inside its rough window: early = January to April,
    mid = May to August, late / end of = September to December.
    The quote must state the date for the event itself: the date text must be a whole date or period in
    the quote ('2027' inside 'Q3 2027' is not the year 2027), it must sit in the clause that names the event
    (see _supporting_clause and runs/README.md, "Which date belongs to the event"), and that clause must not
    negate or replace it ('Q3 2027 is no longer expected'). When the quote does not show which date belongs
    to the event, the check could not run. The order of the dates in the quote is never evidence.
    A period with no year ('Q3') fails.
    Anything vaguer ('later this year') with no date given: not_needed.
    """
    dt = normalize(date_text)
    looked_for = (f"{date_iso} to {date_end}" if date_end else date_iso) or dt

    def result(res: str, reason: str, found=None) -> CheckResult:
        return CheckResult(DATE_ID, DATE_LABEL, res, reason, {"value": looked_for, "found": found})

    if not dt:
        return result(COULD_NOT_RUN, "The reader gave no date text for this row.")
    if _YEARLESS.match(_canon(dt)):
        return result(FAIL, f"“{dt}” names no year; a period needs a year before it can be compared.",
                      found=dt)
    q = _norm_quote(quote)
    if dt not in q:
        return result(FAIL, "The date text is not part of the quote.", found=None)
    if _NAMED.fullmatch(_canon(dt)):
        named = list(_NAMED.finditer(q))
        same = [m for m in named if _canon(m[0]) == _canon(dt)]
        if not same:
            around = next((m[0] for m in named if dt in m[0]), None) or (named[0][0] if named else None)
            claim = f"the whole of {dt}" if parse_period(dt) and re.fullmatch(r"\d{4}", _canon(dt)) else dt
            return result(FAIL, f"The quote names {around}, the row claims {claim}.", found=around)
        verdict = _supporting_clause(dt, q, event_anchors(event_type, title, program))
        if verdict is not None:
            res, reason, found = verdict
            return result(res, reason, found=found)
    parsed = parse_exact_date(dt)
    if parsed is not None:
        if date_end is not None:
            return result(FAIL, "The quote names one day but the row gives a period.", found=parsed.isoformat())
        if date_iso is None:
            return result(FAIL, "The quote names an exact day but the row gives none.", found=parsed.isoformat())
        if parsed.isoformat() == date_iso:
            return result(PASS, "The day in the row is the day printed in the quote.", found=parsed.isoformat())
        return result(FAIL, "The day in the row differs from the day printed in the quote.",
                      found=parsed.isoformat())
    period = parse_period(dt)
    if period is not None:
        start, end = period
        found = f"{start.isoformat()} to {end.isoformat()}" if start else dt
        if date_iso is None and date_end is None:
            return result(PASS, "The quote names a period, not a day; the period words are printed in it.",
                          found=found)
        if date_iso is None or date_end is None:
            return result(FAIL, "The quote names a period but the row gives only one day.", found=found)
        if date_iso > date_end:
            return result(FAIL, "The row's period ends before it starts.", found=found)
        if start is None:
            window = vague_period_window(dt)
            if window is not None:
                w_start, w_end, words = window
                if not (w_start.isoformat() <= date_iso <= date_end <= w_end.isoformat()):
                    return result(FAIL, f"{dt} means roughly {words} {w_start.year}; the row's dates are not "
                                        f"in that range.", found=f"{w_start.isoformat()} to {w_end.isoformat()}")
                return result(PASS, f"The row's dates fall inside {dt} ({words} {w_start.year}).",
                              found=f"{w_start.isoformat()} to {w_end.isoformat()}")
            year = vague_period_year(dt)
            if year is not None and not (date_iso[:4] == date_end[:4] == str(year)):
                return result(FAIL, f"The quote names a period in {year} but the row's dates are not both "
                                    f"in {year}.", found=found)
            return result(PASS, "The quote names a period, not a day; the period words are printed in it.",
                          found=found)
        if (start.isoformat(), end.isoformat()) == (date_iso, date_end):
            return result(PASS, "The period in the row is the period printed in the quote.", found=found)
        return result(FAIL, "The period in the row differs from the period printed in the quote.", found=found)
    if date_iso is None and date_end is None:
        return result(NOT_NEEDED, "The quote names no exact day and no period, so there is nothing to compare.",
                      found=dt)
    return result(FAIL, "The row gives a date but the quote names no day and no period.", found=dt)


# ---------------------------------------------------------------- house rule


def source_rule(source_kind: str, other_source_kinds: list[str] | None = None) -> CheckResult:
    """House rule: a third-party calendar is never a source on its own."""
    others = list(other_source_kinds or [])
    compared = {"value": source_kind, "found": ", ".join(others) if others else None}
    if source_kind != THIRD_PARTY_CALENDAR:
        return CheckResult(SOURCE_ID, SOURCE_LABEL, PASS,
                           "The source is not a third-party calendar.", compared)
    if any(k != THIRD_PARTY_CALENDAR for k in others):
        return CheckResult(SOURCE_ID, SOURCE_LABEL, PASS,
                           "The calendar entry is backed by another kind of source.", compared)
    return CheckResult(SOURCE_ID, SOURCE_LABEL, FAIL,
                       "A third-party calendar is never a source on its own.", compared)


# ---------------------------------------------------------------- status


def row_status(results: list[CheckResult | dict], *, readable: bool = True,
               second_reader_agrees: bool | None = None) -> str:
    if not readable:
        return UNREADABLE
    res = [r.result if isinstance(r, CheckResult) else r["result"] for r in results]
    if FAIL in res:
        return FAILED
    if second_reader_agrees is False:
        return UNDECIDED
    if res and all(r in (PASS, NOT_NEEDED) for r in res) and PASS in res:
        return MATCHED
    return PROPOSED
