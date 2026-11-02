"""Run15/e7: punctuation around quoted speech is not a changed word.

Public PTGX capture retained with real word boxes; no model calls or live data edits.
"""
import json
from pathlib import Path

import pytest

from checks.event_checks import quote_match
from checks.selector_match import selector_match
from runs.recheck import row_checks


@pytest.fixture
def captured():
    return json.loads((Path(__file__).parent / "fixtures/run15_e7_quote.json").read_text())


def locate(d, quote=None, box=None, quote_lines=True):
    return selector_match(quote or d["quote"], box or d["box"], d["words"],
                          d["page_width"], d["page_height"], quote_lines=quote_lines)


def test_actual_e7_passes_both_text_and_geometry_without_rewriting_evidence(captured):
    before = json.dumps(captured, sort_keys=True)
    text = quote_match(captured["quote"], captured["words"], captured["box"])
    geometry = locate(captured)
    assert text.result == geometry.result == "pass"
    assert text.compared["found"].startswith('"The')
    assert text.compared["word_ids"] == geometry.compared["word_ids"]
    assert json.dumps(captured, sort_keys=True) == before


def test_production_row_recheck_uses_same_comparison(captured):
    row = {"quote": captured["quote"], "box": captured["box"], "page_number": 1,
           "source_id": captured["source_id"], "event_type": "unknown",
           "date_text": "second quarter of 2026", "date": "2026-04-01", "date_end": "2026-06-30"}
    source = {"id": captured["source_id"], "kind": "filing", "pages": [
        {"page_number": 1, "visual_evidence": {"origin": "text_layer", "overlays_supported": True}}]}
    checks = {c.check: c for c in row_checks(row, source, captured)}
    assert checks["quote_match"].result == checks["selector_match"].result == "pass"
    assert checks["type_match"].result == "not_needed"  # no new semantic clearance


@pytest.mark.parametrize("opening,closing", [('"', ''), ('“', ''), ('"', '"'), ('“', '”')])
def test_prose_quote_delimiters(opening, closing):
    text = opening + "The company expects results in 2027" + closing
    words = [{"id": str(i), "text": t, "x0": i * 40, "x1": i * 40 + 35,
              "y0": 20, "y1": 30} for i, t in enumerate(text.split())]
    quote = "The company expects results in 2027"
    assert quote_match(quote, words).result == "pass"
    assert selector_match(quote, [0,20,280,30], words, 612,792,quote_lines=True).result == "pass"


@pytest.mark.parametrize("old,new", [("2026", "2027"), ("successful", "unsuccessful"),
                                    ("ICOTYDE", "OTHER"), ("The", "the")])
def test_real_word_number_and_case_changes_still_fail(captured, old, new):
    quote = captured["quote"].replace(old, new, 1)
    assert quote_match(quote, captured["words"], captured["box"]).result == "fail"
    assert locate(captured, quote).result == "fail"


def test_wrong_box_still_fails(captured):
    assert locate(captured, box=[28.5, 350, 542.86, 365]).result != "pass"


def test_table_comparison_not_relaxed(captured):
    assert locate(captured, quote_lines=False).result == "fail"


@pytest.mark.parametrize("page,claim", [('Length is 5"', 'Length is 5'),
                                     ("Company's study starts", "Company study starts"),
                                     ('The "approved" drug', 'The approved drug'),
                                     ('Revenue is (50)', 'Revenue is 50')])
def test_meaningful_punctuation_not_erased(page, claim):
    words = [{"id": str(i), "text": t} for i,t in enumerate(page.split())]
    assert quote_match(claim, words).result == "fail"
