"""Code checks for one catalyst event row, and the per-row status.

The citation box is checked with the same selector_match used for table values. These add:
    quote_match    "Quote found on page"         the quote is printed on the cited page, word for word
    date_match     "Date read as written"        the date the reader gave is the date printed in the quote;
                                                  for a period ("Q3 2027") the period words are in the quote
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


def vague_period_year(text: str | None) -> int | None:
    """The year a vague period names ('mid-2027' -> 2027), or None if it names no year."""
    s = normalize(text).rstrip(".").lower()
    s = re.sub(r"^(?:in |during |by )?(?:the )?", "", s)
    m = _VAGUE.match(s)
    return int(m[1]) if m and m.lastindex and m[1] else None


def date_match(date_text: str | None, date_iso: str | None, quote: str | None,
               date_end: str | None = None) -> CheckResult:
    """Pass if date_text is printed in the quote and the row's date says the same thing.

    An exact day ('March 12, 2027'): the row's date must be that day and date_end must be empty.
    A period ('Q3 2027', 'second half of 2027', 'mid-2027'): there is no exact day to compare, so the
    check passes when the period words are printed in the quote and the row's date and date_end, if
    given, are the first and last day of that period. A vague period ('mid-2027') has no first or
    last day, so date and date_end only need to be in order and, when the words name a year, both
    inside that year.
    Anything vaguer ('later this year') with no date given: not_needed.
    """
    dt = normalize(date_text)
    looked_for = (f"{date_iso} to {date_end}" if date_end else date_iso) or dt

    def result(res: str, reason: str, found=None) -> CheckResult:
        return CheckResult(DATE_ID, DATE_LABEL, res, reason, {"value": looked_for, "found": found})

    if not dt:
        return result(COULD_NOT_RUN, "The reader gave no date text for this row.")
    if dt not in _norm_quote(quote):
        return result(FAIL, "The date text is not part of the quote.", found=None)
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
