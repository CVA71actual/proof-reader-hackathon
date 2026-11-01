"""One decision, shared by the row path and the cell path: may word-geometry checks run on this page?

Word positions that come from a transcription (derived words), from approximate geometry, or from a text
layer nobody can see (hidden_text) can support a narrow text-occurrence fact, but they cannot establish
where a quote, a value or its row/column sits on the source page. The citation-place check
(selector_match) and the row/column check therefore do not run on such a page, and say why.

`page_restriction(visual_evidence, derived_words_present, box=...)` answers from the page capability alone:
    visual_evidence        the page record's classification (runs/sources.visual_evidence), or None
    derived_words_present  the page carries derived (transcribed) words
    box                    the place the reader proposed, if any

The answer is a Restriction(restricted, kind, reason):
    hidden_text   the page's own reason (a fixed sentence when the record has none)
    derived       the derived/approximate sentence for the asking path (row or cell keep their own words)
    no box        on a restricted page with no proposed place, when the caller passes `box` (the row path):
                  the selector's own no-place sentence. The cell path does not pass `box`, so a box-less
                  cell keeps the page's reason, as it always has.
    native        restricted False, reason None: the geometry checks run as they always have
"""

from __future__ import annotations

from typing import NamedTuple

HIDDEN_TEXT = "hidden_text"
DERIVED = "derived"

HIDDEN_TEXT_REASON = "The page has no visible text-word geometry."
CELL_DERIVED_REASON = "Word and row/column alignment cannot run on derived or approximate geometry."
ROW_DERIVED_REASON = ("SAM 3.1 found no region to compare the box with; word alignment cannot run on derived or "
                      "approximate geometry.")
# The selector's own sentence when there is no box (checks/selector_match.py).
NO_PLACE_REASON = "The reader did not propose a place on the page for this value."

ROW, CELL = "row", "cell"
_DERIVED_REASON = {ROW: ROW_DERIVED_REASON, CELL: CELL_DERIVED_REASON}
_NO_BOX = object()


class Restriction(NamedTuple):
    restricted: bool
    kind: str | None          # HIDDEN_TEXT, DERIVED, or None on a native page
    reason: str | None


def page_kind(visual_evidence: dict | None, derived_words_present: bool) -> str | None:
    """HIDDEN_TEXT, DERIVED, or None (native): the page capability, nothing else."""
    visual = visual_evidence or {}
    if visual.get("origin") == HIDDEN_TEXT:
        return HIDDEN_TEXT
    if visual.get("origin") in {"approximate", "derived"} or visual.get("derived_words") or derived_words_present:
        return DERIVED
    return None


def page_restriction(visual_evidence: dict | None, derived_words_present: bool, *, path: str,
                     box=_NO_BOX) -> Restriction:
    """May word-geometry checks run? `path`: ROW or CELL (only picks the derived sentence's wording).
    `box`: pass the proposed box (None when there is none) to get the no-place reason on a restricted page."""
    kind = page_kind(visual_evidence, derived_words_present)
    if kind is None:
        return Restriction(False, None, None)
    if box is None:
        return Restriction(True, kind, NO_PLACE_REASON)
    if kind == HIDDEN_TEXT:
        return Restriction(True, kind, (visual_evidence or {}).get("reason") or HIDDEN_TEXT_REASON)
    return Restriction(True, kind, _DERIVED_REASON[path])


def capability(visual_evidence: dict | None, derived_words_present: bool) -> dict:
    """The page-capability inputs a check was computed with, as the assessment binding records them."""
    visual = visual_evidence or {}
    return {"visual_origin": visual.get("origin"),
            "derived_words_present": bool(derived_words_present or visual.get("derived_words"))}


def page_capability(page: dict | None) -> dict:
    """The capability a page record grants now (what check_current compares a binding against)."""
    page = page or {}
    return capability(page.get("visual_evidence"), bool(page.get("derived_words")))
