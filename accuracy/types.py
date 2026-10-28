"""The fixed list of mistake types. Every caught mistake carries one of these codes.

    type_for_check(name, compared, reason)  one failed check -> one type code (None if the check did not fail
                                            or names no mistake)
    primary_type(checks)                    the one type that best names what went wrong on an item, from all
                                            its checks (None if no check failed)
    type_for_field(field)                   the type for a fix of one field, when no failed check names it

The codes never change; the labels are the plain words shown on screen.
"""

from __future__ import annotations

WRONG_VALUE = "WRONG_VALUE"
WRONG_ROW = "WRONG_ROW"
WRONG_COLUMN = "WRONG_COLUMN"
BOX_DRIFT = "BOX_DRIFT"
QUOTE_NOT_ON_PAGE = "QUOTE_NOT_ON_PAGE"
DATE_NOT_IN_QUOTE = "DATE_NOT_IN_QUOTE"
PERIOD_MISREAD = "PERIOD_MISREAD"
DATE_IN_PAST = "DATE_IN_PAST"
WRONG_EVENT_TYPE = "WRONG_EVENT_TYPE"
WRONG_COMPANY = "WRONG_COMPANY"
SOURCE_NOT_ALLOWED = "SOURCE_NOT_ALLOWED"
DUPLICATE = "DUPLICATE"
UNREADABLE_OUTPUT = "UNREADABLE_OUTPUT"

LABELS = {
    WRONG_VALUE: "Value not as printed",
    WRONG_ROW: "Value placed in the wrong row",
    WRONG_COLUMN: "Value placed in the wrong column",
    BOX_DRIFT: "Box does not sit on the value",
    QUOTE_NOT_ON_PAGE: "Quote not printed on the page",
    DATE_NOT_IN_QUOTE: "Date not part of the quote",
    PERIOD_MISREAD: "Date or period read differently from the page",
    DATE_IN_PAST: "Event date already past",
    WRONG_EVENT_TYPE: "Wrong kind of event",
    WRONG_COMPANY: "Wrong company",
    SOURCE_NOT_ALLOWED: "Source not allowed on its own",
    DUPLICATE: "Same item proposed twice",
    UNREADABLE_OUTPUT: "Reader's answer could not be read",
}
CODES = tuple(LABELS)

CAUGHT_BY = ("code", "second_reader", "person")

FAIL = "fail"

# Which failed check names the mistake best when several failed on one item. A value that is printed nowhere on
# the page is a wrong value whatever its box says; a box in the wrong row or column says more than a box that
# reads differently.
_PRIORITY = (
    "text_layer_match", "quote_match", "row_column_match", "date_match", "selector_match", "source_rule",
    "event_in_past",
)

_FIELD_TYPES = {
    "value": WRONG_VALUE,
    "quote": QUOTE_NOT_ON_PAGE,
    "date_text": PERIOD_MISREAD,
    "date": PERIOD_MISREAD,
    "date_end": PERIOD_MISREAD,
    "event_type": WRONG_EVENT_TYPE,
    "company": WRONG_COMPANY,
    "ticker": WRONG_COMPANY,
}

_EXCLUSION_TYPES = {
    "third_party_calendar_only": SOURCE_NOT_ALLOWED,
    "event_in_past": DATE_IN_PAST,
}


def label(code: str | None) -> str:
    return LABELS.get(code, "No type recorded") if code else "No type recorded"


def _name(check: dict) -> str | None:
    # run.json checks say "name"; a CheckResult dict says "check"
    return check.get("name") or check.get("check")


def _split_place(text: str | None) -> tuple[str | None, str | None]:
    """'row Product sales, net, column 2025' -> ('Product sales, net', '2025')."""
    if not text or not text.startswith("row ") or ", column " not in text:
        return None, None
    row, col = text[4:].rsplit(", column ", 1)
    return (None if row == "?" else row), (None if col == "?" else col)


def _row_column_type(compared: dict, reason: str) -> str:
    want_row = compared.get("looked_for_row")
    want_col = compared.get("looked_for_column")
    found_row = compared.get("found_row")
    found_col = compared.get("found_column")
    if want_row is None and want_col is None:
        want_row, want_col = _split_place(compared.get("looked_for") or compared.get("value"))
        found_row, found_col = _split_place(compared.get("found"))
    if "not a usable box" in reason:
        return BOX_DRIFT
    if found_col is not None and want_col is not None and found_col != want_col:
        return WRONG_COLUMN
    return WRONG_ROW  # a row label not on the box's line, or a cell labelled for another row


def type_for_check(name: str | None, compared: dict | None = None, reason: str | None = None,
                   result: str = FAIL) -> str | None:
    """The type code for one failed check. None if the check did not fail."""
    if result != FAIL:
        return None
    compared = compared or {}
    reason = reason or ""
    if name == "text_layer_match":
        return WRONG_VALUE
    if name == "selector_match":
        # the box holds whole words that read as something else: the box sits on a value, not this one
        if "reads differently" in reason:
            return WRONG_VALUE
        return BOX_DRIFT
    if name == "row_column_match":
        return _row_column_type(compared, reason)
    if name == "quote_match":
        return QUOTE_NOT_ON_PAGE
    if name == "date_match":
        if "not part of the quote" in reason:
            return DATE_NOT_IN_QUOTE
        return PERIOD_MISREAD
    if name == "source_rule":
        return SOURCE_NOT_ALLOWED
    if name == "event_in_past":
        return DATE_IN_PAST
    return None


def check_type(check: dict) -> str | None:
    return type_for_check(_name(check), check.get("compared"), check.get("reason"), check.get("result"))


def failed_types(checks: list[dict]) -> list[str]:
    """Every type named by a failed check, in check order, without repeats."""
    out = []
    for c in checks or []:
        t = check_type(c)
        if t and t not in out:
            out.append(t)
    return out


def primary_type(checks: list[dict]) -> str | None:
    failed = [c for c in checks or [] if c.get("result") == FAIL and check_type(c)]
    if not failed:
        return None
    failed.sort(key=lambda c: _PRIORITY.index(_name(c)) if _name(c) in _PRIORITY else len(_PRIORITY))
    return check_type(failed[0])


def type_for_field(field: str | None) -> str | None:
    return _FIELD_TYPES.get(field)


def type_for_exclusion(rule: str | None) -> str | None:
    return _EXCLUSION_TYPES.get(rule)


def type_for_status(status: str | None) -> str | None:
    return UNREADABLE_OUTPUT if status == "unreadable" else None
