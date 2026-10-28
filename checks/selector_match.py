"""Check: is the value actually printed inside the box the reader proposed?

Shown on screen as "Citation located".
"""

from __future__ import annotations

from numbers import Real

from checks.common import COULD_NOT_RUN, FAIL, PASS, CheckResult, join_words, normalize

CHECK_ID = "selector_match"
LABEL = "Citation located"


def _valid_box(box) -> bool:
    return (
        isinstance(box, (list, tuple))
        and len(box) == 4
        and all(isinstance(v, Real) and not isinstance(v, bool) for v in box)
    )


def words_in_box(box, words: list[dict]) -> list[dict]:
    x0, y0, x1, y1 = box
    inside = []
    for w in words:
        cx = (w["x0"] + w["x1"]) / 2
        cy = (w["y0"] + w["y1"]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            inside.append(w)
    return inside


def selector_match(
    value: str | None,
    box,
    words: list[dict],
    page_width: float,
    page_height: float,
) -> CheckResult:
    """Pass if the words whose centres fall inside the box read exactly as the value."""
    want = normalize(value)

    def result(res: str, reason: str, **compared) -> CheckResult:
        return CheckResult(CHECK_ID, LABEL, res, reason, {"value": want, "box": box, **compared})

    if box is None:
        return result(COULD_NOT_RUN, "The reader did not propose a place on the page for this value.")
    if not _valid_box(box):
        return result(FAIL, "The proposed place on the page is not a usable box.")
    x0, y0, x1, y1 = (float(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return result(FAIL, "The proposed box is empty.")
    if x1 <= 0 or y1 <= 0 or x0 >= page_width or y0 >= page_height:
        return result(FAIL, "The proposed box is off the page.")

    inside = words_in_box((x0, y0, x1, y1), words)
    found = join_words(inside)
    ids = [w["id"] for w in inside]
    if not inside:
        return result(FAIL, "There is no text inside the proposed box.", found="", word_ids=ids)
    if normalize(found) == want:
        return result(PASS, "The text inside the box reads the same as the value.", found=found, word_ids=ids)
    return result(
        FAIL,
        "The text inside the box reads differently from the value.",
        found=found,
        word_ids=ids,
    )
