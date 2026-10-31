"""Code checks for one catalyst event row, and the per-row status.

The citation box is checked with the same selector_match used for table values. These add:
    quote_match    "Quote found on page"         the quote is printed on the cited page, word for word
    date_match     "Date read as written"        the date the reader gave is the date printed in the quote;
                                                  the date must sit in the part of the quote that names the
                                                  event, not negated or replaced there; a period needs a year
    type_match     "Event type matches the quote"    the row's event type is one the quote's own words fit
                                                      (the fixed words in _ANCHORS), not another kind of event
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
from checks.selector_match import PICTURE_REASON, _valid_box, is_picture_at

QUOTE_ID, QUOTE_LABEL = "quote_match", "Quote found on page"
DATE_ID, DATE_LABEL = "date_match", "Date read as written"
SOURCE_ID, SOURCE_LABEL = "source_rule", "Source is more than a calendar"
TYPE_ID, TYPE_LABEL = "type_match", "Event type matches the quote"

THIRD_PARTY_CALENDAR = "third_party_calendar"

MATCHED, FAILED, PROPOSED, UNREADABLE, UNDECIDED = "matched", "failed", "proposed", "unreadable", "undecided"


# ---------------------------------------------------------------- quote


def _norm_quote(text: str | None) -> str:
    # Quotes may carry typographic quote marks or a trailing full stop the page prints differently.
    s = normalize(text).replace("’", "'").replace("“", '"').replace("”", '"')
    return s


def quote_match(quote: str | None, words: list[dict], box=None) -> CheckResult:
    """Pass if the quote equals some run of consecutive page words (any length).
    `box`: the box the reader proposed. A quote that is not in the text layer, with little or no text under or near
    that box, is a picture: the check could not run (it is not a failure)."""
    want = _norm_quote(quote)

    def result(res: str, reason: str, found=None, word_ids=None, near_match=None, differs=None) -> CheckResult:
        compared = {"value": want, "found": found}
        if word_ids is not None:
            compared["word_ids"] = word_ids
        return CheckResult(QUOTE_ID, QUOTE_LABEL, res, reason, compared,
                           near_match=near_match, differs=list(differs or []))

    picture = bool(words) and _valid_box(box) and is_picture_at(box, words)
    if not words:
        return result(COULD_NOT_RUN, "The page has no text layer to compare against.")
    if not want:
        return result(COULD_NOT_RUN, "The reader gave no quote for this row.")
    n = len(want.split(" "))
    for i in range(0, len(words) - n + 1):
        run = words[i : i + n]
        if any(w.get("break") for w in run):        # rebuilt picture words: a run never crosses a region break
            continue
        if _norm_quote(join_words(run)) == want:
            return result(PASS, "The quote is printed on the page, word for word.",
                          found=join_words(run), word_ids=[w["id"] for w in run])
    if picture:
        return result(COULD_NOT_RUN, PICTURE_REASON)
    near = _near_run(want.split(" "), words, n)
    if near:
        run, differs = near
        k = len(differs)
        shown = "; ".join(f"quote \u2018{q}\u2019, page \u2018{p}\u2019" for q, p in differs[:3])
        more = f" (and {k - 3} more)" if k > 3 else ""
        what = "1 word differs" if k == 1 else f"{k} words differ"
        return result(FAIL, f"The quote nearly matches the page; {what} \u2014 {shown}{more}.",
                      near_match=join_words(run), differs=differs)
    return result(FAIL, "The quote is not printed anywhere on the page.")


NEAR_RATIO = 0.9
_EDGE = re.compile(r"^\W+|\W+$")


def _fold(word: str) -> str:
    return _EDGE.sub("", _norm_quote(word)).casefold()


def _near_run(qwords: list[str], words: list[dict], n: int):
    """Best same-length run of page words that matches the quote at >= NEAR_RATIO word by word (case and edge
    punctuation ignored), with the (quote_word, page_word) pairs that differ. None when there is no such run."""
    qf = [_fold(w) for w in qwords]
    best = None
    for i in range(0, len(words) - n + 1):
        run = words[i : i + n]
        if any(w.get("break") for w in run):
            continue
        same = sum(1 for a, w in zip(qf, run) if a == _fold(w["text"]))
        if same / n >= NEAR_RATIO and (best is None or same > best[0]):
            best = (same, run)
    if best is None:
        return None
    run = best[1]
    differs = [(q, w["text"]) for q, w, f in zip(qwords, run, qf) if f != _fold(w["text"])]
    if not differs:     # only case or punctuation differs: say which words
        differs = [(q, w["text"]) for q, w in zip(qwords, run) if q != _norm_quote(w["text"])]
    return (run, differs) if differs else None


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
# 'early Q1 2027' / 'mid-Q2 2027' / 'late Q4 2027': one month of the quarter (early = first, mid = middle,
# late = last), so it has a first and last day like any month.
_QUARTER_PART = re.compile(r"^(early|mid|late)[- ]?\s*(?:q([1-4])|([1-4])q|(first|second|third|fourth|1st|2nd|3rd|4th)"
                           r" quarter(?: of)?)\s*(\d{4})$")
_PART_OFFSET = {"early": 0, "mid": 1, "late": 2}


def quarter_part_period(text: str | None) -> tuple[date, date] | None:
    """'early Q1 2027' -> (2027-01-01, 2027-01-31); 'mid-Q2 2027' -> May 2027; 'late Q4 2027' -> December 2027."""
    m = _QUARTER_PART.match(_canon(text))
    if not m:
        return None
    q = int(m[2] or m[3] or _ORDINAL[m[4]])
    y, mo = int(m[5]), _QUARTER_START[q][0] + _PART_OFFSET[m[1]]
    nxt = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
    return date(y, mo, 1), date.fromordinal(nxt.toordinal() - 1)


_SHORT_YEAR = re.compile(r"(?<=[A-Za-z0-9])\s*['’‘](\d{2})\b")
# '1Q27', '4Q26': digit, Q, two digits, no apostrophe. Read as a quarter only when the two digits are 20-39
# ('1Q27' = Q1 2027); anything else ('2Q45', a part number) is left alone.
_DIGIT_Q_YEAR = re.compile(r"(?<![A-Za-z0-9])([1-4])Q([23]\d)(?![A-Za-z0-9])", re.I)
_CAL_YEAR = re.compile(r"\bcalendar year\s+", re.I)


def expand_short_year(text: str | None) -> str:
    """A two-digit year written with an apostrophe is 20YY: 'Q1 ’27' -> 'Q1 2027', 'Aug ’26' -> 'Aug 2026', '1H ’27' ->
    '1H 2027'. Slide-style dates. Without the apostrophe 'Aug 26' is a day, so it is left alone. The quarter forms
    '1Q27' and '4Q26' (no apostrophe) are Q1 2027 and Q4 2026 when the two digits are 20-39."""
    s = _SHORT_YEAR.sub(lambda m: f" 20{m[1]}", text or "")
    return _DIGIT_Q_YEAR.sub(lambda m: f"Q{m[1]} 20{m[2]}", s)


def parse_period(text: str | None) -> tuple[date | None, date | None] | None:
    """A named period -> (first day, last day). 'Q3 2027' -> (2027-07-01, 2027-09-30).

    Quarters, halves, months and years have fixed first and last days, and so does part of a quarter:
    'early Q1 2027' is its first month, 'mid-Q2 2027' its middle month, 'late Q4 2027' its last. Vaguer periods
    ('mid-2027', 'early 2027', 'late 2027') return (None, None): the words name a period but
    not where it starts or ends. Anything else (an exact day, 'later this year') -> None.
    """
    s = _CAL_YEAR.sub("", expand_short_year(normalize(text))).rstrip(".").lower()
    s = re.sub(r"^(?:in |during |by )?(?:the )?", "", s)
    part = quarter_part_period(s)
    if part is not None:
        return part
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
    s = _CAL_YEAR.sub("", expand_short_year(normalize(text))).rstrip(".").lower()
    return re.sub(r"^(?:in |during |by )?(?:the )?", "", s)


# A date as a slide or press release prints it: with a weekday ("Thursday, August 6, 2026"), a time of day ("Friday,
# Sept. 4 at 10:00 a.m. ET"), "calendar year 2028", or no year at all ("Sept. 4"). clean_date_text takes off what is not
# part of the date; yearless_day counts the year from the filing date: the first such day on or after it.
_DAYS = (r"(?:today|tomorrow|yesterday|mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?"
         r"|sat(?:urday)?|sun(?:day)?)")
_WEEKDAY = re.compile(rf"^{_DAYS}\.?,?\s+", re.I)
_CLOCK = r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?"
_TIME = re.compile(rf",?\s*(?:at\s+)?{_CLOCK}(?:\s*[-–]\s*{_CLOCK})?(?:\s*(?:[A-Za-z]{{2,3}}|(?:eastern|pacific|central|mountain)(?: (?:standard |daylight )?time)?))?\.?$",
                   re.I)
_MONTH_WORDS = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DAY_ONLY = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?$")


def fix_commas(text: str) -> str:
    """'November 14,2026' -> 'November 14, 2026' (the date parser wants a space after the comma)."""
    return re.sub(r",(?=\d)", ", ", text or "")


def clean_date_text(text: str | None) -> str:
    """The date alone: no leading weekday, no trailing time of day, 'calendar year' read as 'year'."""
    s = fix_commas(normalize(text))
    s = _WEEKDAY.sub("", s)
    s = _TIME.sub("", s)
    s = _CAL_YEAR.sub("", s)
    return s.strip().strip("()*†‡¹²³ ").strip()          # brackets and footnote marks around a date are not the date


_DAY_RANGE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})\s*[-–—]\s*(\d{1,2}),?\s+(\d{4})$")


def parse_day_range(text: str | None) -> tuple[date, date] | None:
    """'November 14 – 17, 2026' -> (2026-11-14, 2026-11-17): days of one month. None for anything else."""
    m = _DAY_RANGE.match(clean_date_text(text).rstrip("."))
    if not m or m[1].lower() not in _MONTHS:
        return None
    try:
        first, last = date(int(m[4]), _MONTHS[m[1].lower()], int(m[2])), date(int(m[4]), _MONTHS[m[1].lower()], int(m[3]))
    except ValueError:
        return None
    return (first, last) if first <= last else None


def yearless_day(text: str | None, filed: date | str | None) -> date | None:
    """'Friday, Sept. 4 at 10:00 a.m. ET' filed 2026-08-05 -> 2026-09-04: a month and a day with no year take the first
    such day on or after the filing date ('December 12' filed 2026-12-20 -> 2027-12-12). None for anything else, or when
    there is no filing date."""
    if isinstance(filed, str):
        try:
            filed = date.fromisoformat(filed[:10])
        except ValueError:
            return None
    if filed is None:
        return None
    m = _DAY_ONLY.match(clean_date_text(text).rstrip("."))
    if not m or m[1].lower() not in _MONTHS:
        return None
    for year in range(filed.year, filed.year + 5):          # five years reach any 29 February
        try:
            d = date(year, _MONTHS[m[1].lower()], int(m[2]))
        except ValueError:
            continue
        if d >= filed:
            return d
    return None


# "today", "announced today", "this morning", "earlier today" in a release mean the day the release is dated: the
# dateline ("SOUTH SAN FRANCISCO, Calif., August 5, 2026") printed near the top of the words, found by code. With no
# dateline on the page, the source's published (filing) date.
_TODAY = re.compile(r"^(?:announced |reported |said )?(?:earlier )?(?:today|this (?:morning|afternoon|evening))$")
_DASH = r"\s*(?:--+|[\u2013\u2014])\s*"        # a dash between the city and the date: en dash, em dash, double hyphen
_CREDIT = r"(?:\(|/)[A-Za-z ]{3,30}(?:\)|/)"        # a wire credit: (BUSINESS WIRE), (GLOBE NEWSWIRE), /PRNewswire/
_DATELINE = re.compile(
    rf"(?<![\w,])([A-Z][A-Za-z.'&\-]*(?: [A-Za-z.'&\-]+){{0,3}}(?:, [A-Z][A-Za-z.]{{1,20}})?)(?:, |{_DASH})"
    rf"(?:{_CREDIT}(?:{_DASH}|\s+))?"
    rf"((?i:{_MONTH_WORDS}))\.? (\d{{1,2}}), (\d{{4}})\b")
_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DATELINE_WORDS = 150        # the dateline is looked for in this many words from the top of the page


def is_today_word(text: str | None) -> bool:
    """'today', 'announced today at 8:00 a.m. ET': the clock is taken off first, the today-word stays."""
    return bool(_TODAY.match(_TIME.sub("", _canon(text)).strip()))


def dateline_date(words: list[dict] | None) -> date | None:
    """The date of the 'CITY, Month D, YYYY' line near the top of the words, or None when none is printed."""
    head = " ".join(w["text"] for w in (words or [])[:DATELINE_WORDS])
    for m in _DATELINE.finditer(head):
        if m[1].split(" ")[-1].lower() in _WEEKDAY_NAMES:       # "Thursday, August 6, 2026" is a day, not a dateline
            continue
        try:
            return date(int(m[4]), _MONTHS[m[2].lower()], int(m[3]))
        except ValueError:
            continue
    return None


def release_day(words: list[dict] | None, published: date | str | None) -> tuple[date, str] | None:
    """(the day 'today' means, 'release dateline' or 'filing date'), or None when there is neither."""
    d = dateline_date(words)
    if d is not None:
        return d, "release dateline"
    if isinstance(published, str):
        try:
            published = date.fromisoformat(published[:10])
        except ValueError:
            return None
    return (published, "filing date") if published is not None else None


# Periods a filing states without a year ("by year-end", "later this year", "next quarter"): their year comes from the
# source's filing date. Returns (first day, last day) of the window the words stand for, or None.
_REL_YEAR_END = re.compile(r"^(?:year[- ]end|(?:the )?end of (?:the |this )?year)$")
_REL_THIS_YEAR = re.compile(r"^(?:this year|(?:later|further) (?:this|in the) year)$")
_REL_QUARTER = re.compile(r"^(?:(?:later|further) )?(this|next|the (?:current|next)) quarter$")


def relative_period(text: str | None, filed: date | str | None) -> tuple[date, date] | None:
    """'year-end' filed 2026-08-05 -> (2026-09-01, 2026-12-31) (the same September-to-December window as
    vague_period_window); 'this year' -> that year; 'later this year' -> the filing date to its year end; 'this quarter'
    and 'next quarter' -> that calendar quarter. None for anything else, or when there is no filing date."""
    if isinstance(filed, str):
        try:
            filed = date.fromisoformat(filed[:10])
        except ValueError:
            return None
    if filed is None:
        return None
    s = _canon(text)
    y = filed.year
    if _REL_YEAR_END.match(s):
        return date(y, 9, 1), date(y, 12, 31)
    if _REL_THIS_YEAR.match(s):
        return (filed if s.startswith(("later", "further")) else date(y, 1, 1)), date(y, 12, 31)
    m = _REL_QUARTER.match(s)
    if m:
        q = (filed.month - 1) // 3 + (1 if m[1].endswith("next") or m[1] == "next" else 0)
        qy, q = y + q // 4, q % 4 + 1
        first, last, end_day = _QUARTER_START[q]
        return date(qy, first, 1), date(qy, last, end_day)
    return None



# Every date or period a quote can name, longest form first, so '2027' inside 'Q3 2027' is not read
# as the bare year. Four digits joined to a code by a hyphen ('PN-8047') are a name, not a year.
_ORD_WORDS = "first|second|third|fourth|1st|2nd|3rd|4th"
_NAMED = re.compile(
    r"(?<![\w/])(?:"
    rf"(?:mid|early|late)[- ]?\s*(?:q[1-4]|[1-4]q|(?:{_ORD_WORDS}) quarter(?: of)?)\s*\d{{4}}"
    rf"|(?:{_MONTH_WORDS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    r"|\d{4}-\d{2}-\d{2}"
    rf"|(?:q[1-4]|[1-4]q|(?:{_ORD_WORDS}) quarter(?: of)?)\s*\d{{4}}"
    r"|(?:h[12]|[12]h|(?:first|second|1st|2nd) half(?: of)?)\s*\d{4}"
    r"|(?:mid|early|late|the end of|end of|year[- ]end|the beginning of|beginning of)[- ]?\s*\d{4}"
    rf"|(?:{_MONTH_WORDS})\.?,?\s+\d{{4}}"
    r"|(?:full[- ]year |fiscal year |fy\s*)?(?<![A-Za-z]-)\d{4}"
    r")\b", re.I)
def _is_name_year(q: str, m: re.Match) -> bool:
    """A bare year inside a name ("Truist 2026 BioPharma Symposium", "ASCO 2026 Annual Meeting"): a capitalised word
    right before it and another right after it. It names the event; it is not the date the quote gives for it."""
    if not re.fullmatch(r"\d{4}", m[0]):
        return False
    before, after = q[:m.start()].split()[-1:], q[m.end():].split()[:1]
    return bool(before and after and before[0][0].isupper() and after[0][0].isupper()
                and before[0].lower().strip(".,") not in _MONTHS)


def named_dates(q: str) -> list[re.Match]:
    """Every date or period the quote names (_NAMED), without a bare year that is part of a name."""
    return [m for m in _NAMED.finditer(q) if not _is_name_year(q, m)]


_YEARLESS = re.compile(
    rf"^(?:(?:(?:mid|early|late)[- ]?\s*)?(?:q[1-4]|[1-4]q|(?:{_ORD_WORDS}) quarter)|h[12]|[12]h|"
    rf"(?:{_ORD_WORDS}) half|mid|early|late|end of|"
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

# ---- The event-type words: ONE table, used by the reader prompt (capture/read_events.py, both readers), by type_match,
# and for the anchors that find the clause naming the event. A quote holding a word of a type reads as that type;
# a quote holding none of the words of the other types is a milestone ("else milestone"). Whole words only.
EVENT_WORDS = {
    "trial_results": ("results", "data", "readout", "topline", "presentation of data", "report"),
    "regulatory_decision": ("approval", "decision", "pdufa", "chmp opinion", "crl", "action date"),
    "launch": ("launch", "commercial availability"),
    "conference": ("conference", "symposium", "investor day", "webcast", "present", "presentation", "late-breaker"),
    "filing": ("submit", "submitted", "submission", "nda", "bla", "maa", "ind", "filed", "filing"),
    "milestone": ("dosed", "enrolled", "initiated", "initiation", "completed", "first patient"),
}
EVENT_TYPES = tuple(EVENT_WORDS)
# When one sentence holds words of several types, the first of these wins (a decision on a filing is a decision).
TYPE_PRIORITY = ("regulatory_decision", "filing", "trial_results", "launch", "conference")
_ANCHORS = {
    **{t.replace("_", " "): w for t, w in EVENT_WORDS.items()},
    "earnings": ("results", "earnings", "revenue", "net loss", "quarter ended", "year ended"),
    "other": (),
}


def _word_re(word: str) -> re.Pattern:
    """A fixed word as a whole word. The edge of a word is anything that is not a letter, digit, underscore, hyphen
    or slash: data-room, data/set, data2, data_code are not 'data'; 'data.' and '(data)' are."""
    return re.compile(rf"(?<![\w/-]){re.escape(word)}(?![\w/-])", re.I)


def _has_word(text: str, word: str) -> bool:
    return bool(_word_re(word).search(text))


def event_type_for(text: str | None) -> str:
    """The event type the words of `text` stand for: the first of TYPE_PRIORITY with a word in it, else 'milestone'."""
    for t in TYPE_PRIORITY:
        if any(_has_word(text or "", w) for w in EVENT_WORDS[t]):
            return t
    return "milestone"


_ACRONYMS = ("nda", "bla", "maa", "ind", "pdufa", "crl", "chmp")


def type_guide(types: tuple[str, ...] | None = None) -> str:
    """The event-type words as the reader prompt prints them, one line per type, from EVENT_WORDS. `types`: only
    the types the run asks for (default all)."""
    types = types or EVENT_TYPES
    caps = lambda w: " ".join(x.upper() if x in _ACRONYMS else x for x in w.split(" "))
    lines = [f'  {t}: {", ".join(caps(w) for w in EVENT_WORDS[t])}' for t in TYPE_PRIORITY if t in types]
    if "milestone" in types:
        lines.append("  milestone: anything else (for example " + ", ".join(EVENT_WORDS["milestone"][:4]) + ")")
    return "\n".join(lines)


_TYPE_KEYS = (
    (("trial", "readout", "result", "topline", "data"), "trial results"),
    (("regulatory_decision", "decision", "approval", "pdufa", "crl"), "regulatory decision"),
    (("launch",), "launch"),
    (("conference", "presentation", "meeting_presentation"), "conference"),
    (("filing", "submission"), "filing"),
    (("milestone", "dosing", "enrollment", "initiation"), "milestone"),
    (("earnings",), "earnings"),
    (("other",), "other"),
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
        if _has_word(text, a):
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


def _supporting_clause(dt: str, q: str, anchors: list[str],
                       chosen: bool = False) -> tuple[str, str, str | None] | None:
    """Which clause of the quote gives the event's date, and what it says about date_text.

    Returns None when the clause supports date_text, else (result, reason, found).
    `chosen`: a person typed this date as the event's date (a fix). Where the quote alone cannot say which of its
    dates belongs to the event (no event word near the date, several event clauses), the person's choice settles
    it; a clause that negates or replaces the date still counts against it."""
    mentions = named_dates(q)
    want = _canon(dt)
    clauses = []
    for a, b in _clauses(q, mentions):
        text = q[a:b]
        ms = [m for m in mentions if a <= m.start() < b]
        clauses.append({"start": a, "text": text, "mentions": ms, "anchor": _anchor_in(text, anchors),
                        "reference": bool(_REFERENCE.match(text.strip()))})
    events = [c for c in clauses if c["anchor"] and not c["reference"] and c["mentions"]]
    holding = [c for c in clauses if any(_canon(m[0]) == want for m in c["mentions"])]
    if chosen and holding and not events:
        events = [holding[0]]
    if not events and holding and len({_canon(m[0]) for m in mentions}) == 1 and _anchor_in(q, anchors):
        events = [holding[0]]       # the quote names one date only, and an event: no other date it could belong to
    own = next((c for c in holding if c in events), holding[0])
    if not events:
        return COULD_NOT_RUN, _UNSURE, None
    dates = {_canon(m[0]) for c in events for m in c["mentions"]}
    if len(events) > 1 and dates != {want}:
        if chosen and any(c in events for c in holding):
            events = [c for c in events if c in holding]
        else:
            return COULD_NOT_RUN, _UNSURE, None
    if chosen and own not in events and not own["reference"]:
        events = [own]      # the person picked the date of a clause that continues the event's ("... and is now Q4 2027")
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
        verdict = _judge_clause(dt, q, c, chosen)
        if verdict is not None:
            return verdict
    return None


def _judge_clause(dt: str, q: str, c: dict, chosen: bool = False) -> tuple[str, str, str | None] | None:
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
        return None if chosen else (COULD_NOT_RUN, _UNSURE, None)
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
               program: str | None = None, filed: date | str | None = None,
               today: date | str | None = None, chosen: bool = False) -> CheckResult:
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
    A period with no year ('Q3') fails. A day with no year ('Friday, Sept. 4') is the first such day on or after
    `filed` (yearless_day); a weekday or time of day around a date is not part of it (clean_date_text).
    A relative period ('by year-end', 'this year', 'next quarter') takes its
    year from `filed`, the source's filing date (relative_period): the row's dates must lie inside that window.
    'today' / 'announced today' / 'this morning' / 'earlier today' is the day in `today` (the release dateline, or the
    filing date when no dateline is printed; release_day): the row's date must be that day.
    Anything vaguer ('later this year') with no date given: not_needed.
    """
    dt = _norm_quote(date_text)     # the same marks as the quote: a typographic apostrophe in both reads alike
    looked_for = (f"{date_iso} to {date_end}" if date_end else date_iso) or dt

    def result(res: str, reason: str, found=None) -> CheckResult:
        return CheckResult(DATE_ID, DATE_LABEL, res, reason, {"value": looked_for, "found": found})

    if not dt:
        return result(COULD_NOT_RUN, "The reader gave no date text for this row.")
    if is_today_word(dt):
        if dt not in _norm_quote(quote):
            return result(FAIL, "The date text is not part of the quote.", found=None)
        if isinstance(today, str):
            today = date.fromisoformat(today[:10])
        if today is None:
            return result(COULD_NOT_RUN, f"“{dt}” means the day of the release, and no release date is known.")
        if date_end is not None:
            return result(FAIL, f"“{dt}” is one day but the row gives a period.", found=today.isoformat())
        if date_iso is None:
            return result(FAIL, f"“{dt}” is {today.isoformat()} but the row gives no day.", found=today.isoformat())
        if date_iso == today.isoformat():
            return result(PASS, f"The day in the row is the release's own day ({today.isoformat()}), what “{dt}” means.",
                          found=today.isoformat())
        return result(FAIL, f"“{dt}” is the release's own day ({today.isoformat()}); the row says {date_iso}.",
                      found=today.isoformat())
    rel = relative_period(dt, filed)
    if rel is not None:
        if dt not in _norm_quote(quote):
            return result(FAIL, "The date text is not part of the quote.", found=None)
        start, end = rel
        found = f"{start.isoformat()} to {end.isoformat()}"
        if date_iso is None and date_end is None:
            return result(PASS, f"“{dt}” is counted from the filing date ({found}); the row gives no day.", found=found)
        if date_iso is None or date_end is None:
            return result(FAIL, f"“{dt}” is a period ({found}) but the row gives only one day.", found=found)
        if not (start.isoformat() <= date_iso <= date_end <= end.isoformat()):
            return result(FAIL, f"“{dt}” counted from the filing date is {found}; the row's dates are not in it.",
                          found=found)
        return result(PASS, f"The row's dates fall inside {dt}, counted from the filing date ({found}).", found=found)
    if _YEARLESS.match(_canon(dt)):
        return result(FAIL, f"“{dt}” names no year; a period needs a year before it can be compared.",
                      found=dt)
    q = _norm_quote(quote)
    if dt not in q:
        return result(FAIL, "The date text is not part of the quote.", found=None)
    dt, q = expand_short_year(dt), expand_short_year(q)     # 'Q1 ’27' is Q1 2027, in the quote as in the date text
    day = yearless_day(dt, filed)
    dt = clean_date_text(dt)                                 # no weekday or time of day; still printed in the quote
    q = _CAL_YEAR.sub("", fix_commas(q))
    days = parse_day_range(dt)
    if days is not None:
        found = f"{days[0].isoformat()} to {days[1].isoformat()}"
        if (days[0].isoformat(), days[1].isoformat()) == (date_iso, date_end):
            return result(PASS, "The days in the row are the days printed in the quote.", found=found)
        return result(FAIL, "The days in the row differ from the days printed in the quote.", found=found)
    if day is not None:
        found = day.isoformat()
        how = "the year counted from the filing date"
        if date_end is not None:
            return result(FAIL, "The quote names one day but the row gives a period.", found=found)
        if date_iso is None:
            return result(FAIL, "The quote names a day but the row gives none.", found=found)
        if date_iso == found:
            return result(PASS, f"The day in the row is the day printed in the quote, with {how}.", found=found)
        return result(FAIL, f"The day in the row differs from the day printed in the quote ({how}: {found}).",
                      found=found)
    if _NAMED.fullmatch(_canon(dt)):
        named = named_dates(q)
        same = [m for m in named if _canon(m[0]) == _canon(dt)]
        if not same:
            around = next((m[0] for m in named if dt in m[0]), None) or (named[0][0] if named else None)
            claim = f"the whole of {dt}" if parse_period(dt) and re.fullmatch(r"\d{4}", _canon(dt)) else dt
            return result(FAIL, f"The quote names {around}, the row claims {claim}.", found=around)
        verdict = _supporting_clause(dt, q, event_anchors(event_type, title, program), chosen)
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
        if named_dates(q):
            return result(COULD_NOT_RUN, f"“{dt}” is not a date this check can read, and the quote names another "
                                         f"date; it cannot tell which belongs to the event.", found=dt)
        return result(NOT_NEEDED, "The quote names no exact day and no period, so there is nothing to compare.",
                      found=dt)
    return result(FAIL, "The row gives a date but the quote names no day and no period.", found=dt)


# ---------------------------------------------------------------- event type


# The six kinds of event the reader may name (capture/read_events.py), each with the fixed words from _ANCHORS.
TYPE_GROUPS = ("trial results", "regulatory decision", "launch", "conference", "filing", "milestone")


def type_group(event_type: str | None) -> str | None:
    """'trial_results' -> 'trial results'; None for a type with no fixed words (earnings, other, unknown)."""
    et = (event_type or "").lower()
    for keys, group in _TYPE_KEYS:
        if any(k in et for k in keys):
            return group if group in TYPE_GROUPS else None
    return None


# Two kinds of event that one sentence often has both kinds of words for ("Phase 1 initiation ... data"): when a quote
# holds words of both, the quote does not say which the event is, and the type is not held against the reader.
AMBIGUOUS_TYPES = ("milestone", "trial results")


def group_key(group: str) -> str:
    return group.replace(" ", "_")


def _words_in(quote: str, group: str) -> list[str]:
    return [w for w in _ANCHORS[group] if _has_word(quote, w)]


def type_match(event_type: str | None, quote: str | None) -> CheckResult:
    """The row's event type must be one the quote's words fit. pass: the quote holds a fixed word for the row's
    type. fail: it holds none for that type but does for another ("results, data" in a quote saved as a
    milestone). not_needed: the quote holds no fixed word for any kind of event (nothing to compare, and a row is
    not held back for it), or the type has no fixed words (earnings, other), or the quote holds words of both milestone and trial results (it does not say
    which; the quote's words decide only when they are one family's). could_not_run: the row has no quote."""
    compared = {"value": event_type, "found": None}

    def result(res: str, reason: str, found: str | None = None) -> CheckResult:
        return CheckResult(TYPE_ID, TYPE_LABEL, res, reason, {**compared, "found": found})

    group = type_group(event_type)
    if group is None:
        return result(NOT_NEEDED, f"The event type {(event_type or '?').replace('_', ' ')} has no fixed words to compare the quote with.")
    if not normalize(quote):
        return result(COULD_NOT_RUN, "The row has no quote to compare the event type with.")
    if group in AMBIGUOUS_TYPES and all(_words_in(quote, g) for g in AMBIGUOUS_TYPES):
        both = "; ".join(f"{g}: {', '.join(_words_in(quote, g))}" for g in AMBIGUOUS_TYPES)
        return result(NOT_NEEDED, f"The quote holds words of both milestone and trial results ({both}), so there is "
                                  f"nothing decisive to compare the type with.", both)
    own = _words_in(quote, group)
    first = event_type_for(quote)
    if own and first not in (group_key(group), "milestone") and _words_in(quote, first.replace("_", " ")):
        return result(FAIL, f"The quote's words ({', '.join(_words_in(quote, first.replace('_', ' ')))}) put it first "
                            f"under {first.replace('_', ' ')}, not {group}.",
                      "; ".join(f"{g}: {', '.join(w)}" for g in (first.replace("_", " "), group) if (w := _words_in(quote, g))))
    if own:
        return result(PASS, f"The quote's words ({', '.join(own)}) fit {group}.", ", ".join(own))
    other = {g: w for g in TYPE_GROUPS if g != group and (w := _words_in(quote, g))}
    if other:
        found = "; ".join(f"{g}: {', '.join(w)}" for g, w in other.items())
        first_group, first_words = next(iter(other.items()))
        return result(FAIL, f"The quote's words ({', '.join(first_words)}) point to {first_group}, not {group}.", found)
    return result(NOT_NEEDED, f"The quote holds no fixed word for {group} or any other kind of event, so there is "
                              f"nothing to compare the type with.")


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
