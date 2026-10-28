"""Check: is the value actually printed inside the box the reader proposed?

Shown on screen as "Citation located".

A word counts as inside the box when at least INSIDE_SHARE of its area is inside it; a box on a word's
centre alone does not hold the word. A word the box cuts through (more than CUT_SHARE of its area inside,
but less than INSIDE_SHARE) makes the box fail: the box reaches into text that is not the value, for
example the neighbouring cell. Words the box only grazes (CUT_SHARE or less) are ignored.
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


INSIDE_SHARE = 0.6
CUT_SHARE = 0.1


def share_inside(box, w: dict) -> float:
    """The share of the word's area that lies inside the box (0 to 1)."""
    x0, y0, x1, y1 = box
    area = (w["x1"] - w["x0"]) * (w["y1"] - w["y0"])
    if area <= 0:
        return 0.0
    ix = max(0.0, min(x1, w["x1"]) - max(x0, w["x0"]))
    iy = max(0.0, min(y1, w["y1"]) - max(y0, w["y0"]))
    return ix * iy / area


def words_in_box(box, words: list[dict]) -> list[dict]:
    """Words with at least INSIDE_SHARE of their area inside the box."""
    return [w for w in words if share_inside(box, w) >= INSIDE_SHARE]


def words_cut_by_box(box, words: list[dict]) -> list[dict]:
    """Words the box cuts through: more than CUT_SHARE but less than INSIDE_SHARE of their area inside."""
    return [w for w in words if CUT_SHARE < share_inside(box, w) < INSIDE_SHARE]


def selector_match(
    value: str | None,
    box,
    words: list[dict],
    page_width: float,
    page_height: float,
) -> CheckResult:
    """Pass if the words inside the box read exactly as the value and the box cuts through no other word."""
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

    rect = (x0, y0, x1, y1)
    inside = words_in_box(rect, words)
    cut = words_cut_by_box(rect, words)
    touched = [w for w in words if share_inside(rect, w) > CUT_SHARE]  # page order
    found = join_words(touched)
    ids = [w["id"] for w in touched]
    if not touched:
        return result(FAIL, "There is no text inside the proposed box.", found="", word_ids=ids)
    if cut:
        held = normalize(join_words(inside)) == want
        named = ", ".join(w["text"] for w in cut)
        if held:
            return result(FAIL, f"The box also covers {named}.", found=found, word_ids=ids)
        return result(FAIL, f"The box only partly covers {named}.", found=found, word_ids=ids)
    if normalize(found) == want:
        return result(PASS, "The text inside the box reads the same as the value.", found=found, word_ids=ids)
    extra = _extra_words(inside, want)
    if extra:
        return result(FAIL, f"The box also covers {', '.join(w['text'] for w in extra)}.",
                      found=found, word_ids=ids)
    return result(
        FAIL,
        "The text inside the box reads differently from the value.",
        found=found,
        word_ids=ids,
    )


def _extra_words(inside: list[dict], want: str) -> list[dict]:
    """If a run of the inside words reads as the value, the inside words outside that run; else []."""
    for i in range(len(inside)):
        for j in range(i + 1, len(inside) + 1):
            if normalize(join_words(inside[i:j])) == want:
                return inside[:i] + inside[j:]
    return []
