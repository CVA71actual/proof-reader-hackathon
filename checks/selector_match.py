"""Check: is the value actually printed inside the box the reader proposed?

Shown on screen as "Citation located".

A word counts as inside the box when at least INSIDE_SHARE of its area is inside it; a box on a word's
centre alone does not hold the word. A word the box cuts through (more than CUT_SHARE of its area inside,
but less than INSIDE_SHARE) makes the box fail: the box reaches into text that is not the value, for
example the neighbouring cell. Words the box only grazes (CUT_SHARE or less) are ignored.

Quote boxes (`quote_lines=True`, used for event rows): a quote is a sentence, and a sentence rarely starts at the
margin or ends at the other one, so the strict rule above would fail a box that is right. The box passes when every
word of the quote lies inside it and it does not reach above the quote's first line or below its last line; other
words on those same lines (the end of the sentence before, the start of the next) are allowed. Table values keep
the strict rule.
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


PICTURE_REASON = "The words here are a picture, not text; the quote could not be checked"
NEAR_PT = 18.0               # "near the box": the box grown by this much on every side
MIN_WORD_HEIGHT_PT = 2.0     # a word shorter than this is not printed where the page shows it (a hidden text layer)
LITTLE_TEXT_WORDS = 3        # fewer real words than this under or near the box: the page is a picture there


def words_near(box, words: list[dict]) -> list[dict]:
    """The words that really sit under or near the box: touching it grown by NEAR_PT, and tall enough to be
    printed text."""
    x0, y0, x1, y1 = (float(v) for v in box)
    grown = (x0 - NEAR_PT, y0 - NEAR_PT, x1 + NEAR_PT, y1 + NEAR_PT)
    return [w for w in words if w["y1"] - w["y0"] >= MIN_WORD_HEIGHT_PT and share_inside(grown, w) > 0]


def is_picture_at(box, words: list[dict]) -> bool:
    """True when the page has little or no text under or near the box: what is there is a picture, not text."""
    return len(words_near(box, words)) < LITTLE_TEXT_WORDS


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
    quote_lines: bool = False,
) -> CheckResult:
    """Pass if the words inside the box read exactly as the value and the box cuts through no other word.
    `quote_lines`: the value is a quote; use the quote rule in the module note instead."""
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
    if quote_lines and is_picture_at(rect, words):
        return result(COULD_NOT_RUN, PICTURE_REASON)
    if quote_lines:
        quoted = _quote_run(want, words, rect)
        if quoted:
            return _quote_box(quoted, rect, words, result)
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


REGION_SLACK_PT = 2.0   # a box may reach this far past a region's edge: the reader's grid is coarse


def region_match(box, regions: list[dict] | None, page_width: float, page_height: float) -> CheckResult:
    """The citation check for a page with no text layer, where there are no words to check a box against: the box must
    lie inside a region SAM 3.1 found on the page (`regions`: [{id, concept, box}] in PDF points). Same id and label as
    selector_match ("Citation located"). could_not_run when no region was found, or the reader gave no box."""
    def result(res: str, reason: str, **compared) -> CheckResult:
        return CheckResult(CHECK_ID, LABEL, res, reason, {"value": "a box inside a region SAM 3.1 found", "box": box,
                                                          **compared})

    if box is None:
        return result(COULD_NOT_RUN, "The reader did not propose a place on the page for this value.")
    if not _valid_box(box):
        return result(FAIL, "The proposed place on the page is not a usable box.")
    x0, y0, x1, y1 = (float(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        return result(FAIL, "The proposed box is empty.")
    if x1 <= 0 or y1 <= 0 or x0 >= page_width or y0 >= page_height:
        return result(FAIL, "The proposed box is off the page.")
    if not regions:
        return result(COULD_NOT_RUN, "SAM 3.1 found no region on this page to compare the box with.")
    # These names are fallback geometry, not regions SAM located. A whole-page
    # box says nothing about where a citation sits.
    fallback_regions = [r for r in regions if r.get("concept") in {"page", "whole page"}]
    regions = [r for r in regions if r.get("concept") not in {"page", "whole page"}]
    if not regions:
        reason = ("SAM 3.1 found no region on this page to compare the box with."
                  if any(r.get("concept") == "whole page" for r in fallback_regions) else
                  "SAM 3.1 found no table or text block on this page; its only region is the whole "
                  "page, which says nothing about where the quote is.")
        return result(COULD_NOT_RUN, reason)
    s = REGION_SLACK_PT
    for r in regions:
        rx0, ry0, rx1, ry1 = r["box"]
        if x0 >= rx0 - s and y0 >= ry0 - s and x1 <= rx1 + s and y1 <= ry1 + s:
            return result(PASS, f"The box lies inside {r['id']}, a {r['concept']} region SAM 3.1 found.",
                          found=r["id"], region=r["box"])
    return result(FAIL, "The box is not inside any region SAM 3.1 found on the page.",
                  found=", ".join(r["id"] for r in regions))


def _fold(s: str) -> str:
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _quote_run(want: str, words: list[dict], rect) -> list[dict] | None:
    """The run of page words that reads as the quote; when it is printed more than once, the run the box overlaps
    most, and among equal overlaps the run nearest the box. None when the quote is not printed (the strict rule then reports what is in the box)."""
    want = _fold(want)       # the same typographic marks quote_match folds: a quote it finds is found here too
    n = len(want.split(" ")) if want else 0
    runs = [words[i : i + n] for i in range(len(words) - n + 1)
            if n and _fold(normalize(join_words(words[i : i + n]))) == want]
    if not runs:
        return None
    return max(runs, key=lambda r: (sum(share_inside(rect, w) for w in r), -_gap(rect, r)))


def _gap(rect, run: list[dict]) -> float:
    """The distance between the box and the nearest word of the run (0 when a word touches or overlaps the box)."""
    x0, y0, x1, y1 = rect

    def one(w: dict) -> float:
        dx = max(w["x0"] - x1, x0 - w["x1"], 0.0)
        dy = max(w["y0"] - y1, y0 - w["y1"], 0.0)
        return (dx * dx + dy * dy) ** 0.5

    return min(one(w) for w in run)


def _quote_box(run: list[dict], rect, words: list[dict], result) -> CheckResult:
    x0, y0, x1, y1 = rect
    ids = [w["id"] for w in run]
    out = [w for w in run if share_inside(rect, w) < INSIDE_SHARE]
    if out:
        say = ", ".join(w["text"] for w in out[:4]) + (" ..." if len(out) > 4 else "")
        return result(FAIL, f"The box does not hold every word of the quote (missing {say}).",
                      found=join_words(words_in_box(rect, words)), word_ids=ids)
    heights = sorted(w["y1"] - w["y0"] for w in words)
    tol = max(1.0, 0.25 * heights[len(heights) // 2])
    first_top = min(w["y0"] for w in run if abs(((w["y0"] + w["y1"]) / 2) - ((run[0]["y0"] + run[0]["y1"]) / 2))
                    <= (run[0]["y1"] - run[0]["y0"]) / 2 + tol)
    last_bottom = max(w["y1"] for w in run if abs(((w["y0"] + w["y1"]) / 2) - ((run[-1]["y0"] + run[-1]["y1"]) / 2))
                      <= (run[-1]["y1"] - run[-1]["y0"]) / 2 + tol)
    if y0 < first_top - tol:
        return result(FAIL, "The box reaches above the first line of the quote.", found=join_words(run), word_ids=ids)
    if y1 > last_bottom + tol:
        return result(FAIL, "The box reaches below the last line of the quote.", found=join_words(run), word_ids=ids)
    return result(PASS, "Every word of the quote is inside the box, and the box stays on the quote's lines.",
                  found=join_words(run), word_ids=ids)


def _extra_words(inside: list[dict], want: str) -> list[dict]:
    """If a run of the inside words reads as the value, the inside words outside that run; else []."""
    for i in range(len(inside)):
        for j in range(i + 1, len(inside) + 1):
            if normalize(join_words(inside[i:j])) == want:
                return inside[:i] + inside[j:]
    return []
