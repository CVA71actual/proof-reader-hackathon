"""From Red Team round 1 (branch redteam/r1, tests/redteam/test_round1.py): the attacks that landed
(four against main, eleven more against R3), copied here so they run in CI. Each assertion is the result the checks should give."""

import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from checks import event_checks
from checks.selector_match import selector_match
from checks.status import check_cell
from conftest import FIXTURES

RUNS = FIXTURES.parent / "runs"
RUN_SCHEMA = json.loads((RUNS / "schema" / "run.schema.json").read_text())


def _load(name):
    return json.loads((RUNS / name / "run.json").read_text())


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


# ---------------------------------------------------------------- R3b: the eleven that still landed on R3

# from Red Team round 1
@pytest.mark.parametrize("tag,who", [
    ("Model", "date_match"),
    ("Model", "Muse Spark and date_match"),
    ("Code", "Muse Spark"),
])
def test_schema_rejects_a_trail_step_with_the_wrong_actor(tag, who):
    run = _load("sample")
    run["rows"][0]["trail"][1]["tag"] = tag
    run["rows"][0]["trail"][1]["who"] = who
    errors = list(Draft202012Validator(RUN_SCHEMA).iter_errors(run))
    assert errors, f"schema accepted tag {tag!r} who {who!r}"


# from Red Team round 1. Since the Codex PR9 review the check needs the row's event (event_type and title)
# to find the clause that dates it; each case now carries a row title, and every case still fails.
@pytest.mark.parametrize("text,start,end,quote,title", [
    ("mid-2027", "2027-01-01", "2027-01-02", "The company expects data in mid-2027.", "Phase 3 data"),
    ("2027", "2027-01-01", "2027-12-31", "Results are expected in Q3 2027.", "Phase 3 results"),
    ("2026", "2026-01-01", "2026-12-31",
     "We reaffirm Q3 2027 guidance. The 2026 figure was preliminary.", "Readout guidance"),
    ("Q3 2026", "2026-07-01", "2026-09-30", "Data in Q3 2027, compared with Q3 2026.", "Phase 3 data"),
    ("Q3 2027", "2027-07-01", "2027-09-30", "The trial is not expected in Q3 2027.", "STAR trial"),
    ("Q3", None, None, "Data in Q3 2027.", "Phase 3 data"),
])
def test_date_match_rejects_a_period_the_quote_does_not_state(text, start, end, quote, title):
    r = event_checks.date_match(text, start, quote, date_end=end, event_type="trial_readout", title=title)
    assert r.result == "fail", r.reason


# from Red Team round 1
def test_the_runs_words_copy_is_bound_to_its_hash():
    """Changing runs/demo/pages/p95.words.json by one character left the suite green."""
    run = _load("demo")
    page = run["sources"][0]["pages"][0]
    digest = hashlib.sha256((RUNS / "demo" / page["words"]).read_bytes()).hexdigest()
    assert page.get("words_sha256") == digest


# from Red Team round 1
def test_the_runs_page_image_is_bound_to_its_hash():
    """Changing one byte of runs/demo/pages/p95.png left the suite green."""
    run = _load("demo")
    page = run["sources"][0]["pages"][0]
    digest = hashlib.sha256((RUNS / "demo" / page["image"]).read_bytes()).hexdigest()
    assert page.get("image_sha256") == digest
