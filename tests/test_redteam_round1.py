"""From Red Team round 1 (branch redteam/r1, tests/redteam/test_round1.py): the four attacks that landed
against main, copied here so they run in CI. Each assertion is the result the checks should give."""

import json

from checks.selector_match import selector_match
from checks.status import check_cell
from conftest import FIXTURES


def _reading():
    return json.loads((FIXTURES / "ions_ars_2026_p95.reading.json").read_text())


def _doc(words, words_doc):
    return {"words": words, "page_width": words_doc["page_width"], "page_height": words_doc["page_height"]}


def _word(words, text):
    return next(w for w in words if w["text"] == text)


def _neighbour(words, word):
    hits = [w for w in words if abs(w["y0"] - word["y0"]) < 1 and w["x0"] > word["x1"]]
    return min(hits, key=lambda w: w["x0"])


# from Red Team round 1
def test_selector_rejects_a_box_that_only_covers_the_centre(words, words_doc):
    """The glyph is about 32pt wide. A 0.02pt box on its centre does not contain it."""
    word = _word(words, "115,315")
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["y0"] + word["y1"]) / 2
    box = [cx - 0.01, cy - 0.01, cx + 0.01, cy + 0.01]
    r = selector_match("115,315", box, words, words_doc["page_width"], words_doc["page_height"])
    assert r.result == "fail", r.reason


# from Red Team round 1
def test_selector_rejects_a_box_that_overlaps_the_neighbouring_cell(words, words_doc):
    """The box enters the next cell's glyph and stops short of that glyph's centre."""
    word = _word(words, "115,315")
    neighbour = _neighbour(words, word)
    ncx = (neighbour["x0"] + neighbour["x1"]) / 2
    box = [word["x0"] - 0.5, word["y0"] - 0.5, ncx - 0.01, word["y1"] + 0.5]
    assert neighbour["x0"] < box[2] < ncx
    r = selector_match("115,315", box, words, words_doc["page_width"], words_doc["page_height"])
    assert r.result == "fail", r.compared.get("found")


# from Red Team round 1
def test_a_2024_number_in_the_2025_column_is_not_matched(words, words_doc):
    royalty = next(r for r in _reading()["table"]["rows"] if r["label"] == "Royalty revenue")
    by_year = {c["column"]: c for c in royalty["cells"]}
    assert by_year["2024"]["value"] != by_year["2025"]["value"]
    cell = {"column": "2025", "value": by_year["2024"]["value"], "box": by_year["2024"]["box"]}
    got = check_cell(cell, _doc(words, words_doc))
    assert got["status"] == "failed", [(c["check"], c["result"]) for c in got["checks"]]


# from Red Team round 1
def test_another_rows_number_is_not_matched(words, words_doc):
    rows = _reading()["table"]["rows"]
    sales = next(r for r in rows if r["label"] == "Product sales, net")
    sales_2025 = next(c for c in sales["cells"] if c["column"] == "2025")
    cell = {"column": "2025", "label": "Royalty revenue",
            "value": sales_2025["value"], "box": sales_2025["box"]}
    got = check_cell(cell, _doc(words, words_doc))
    assert got["status"] == "failed", got["status"]
