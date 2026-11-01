"""SLICE-1B-001: checks are bound to the page capability they were computed with.

- one shared restriction helper (checks/restriction.py) decides for rows and cells;
- the page-capability arguments are required, so an unflagged call raises TypeError;
- the capability is a bound input: a check computed without it (or with another page's) is stale on load;
- text matches on a restricted page say they compared transcribed words.
"""

import copy
import hashlib
import json

import pytest

from checks import restriction
from checks.common import COULD_NOT_RUN, PASS
from checks.event_checks import QUOTE_LABEL, quote_match
from checks.selector_match import LABEL as SELECTOR_LABEL
from checks.status import MATCHED, REQUIRED_CHECKS, check_cell, check_reading
from checks.text_layer_match import LABEL as TEXT_LABEL, text_layer_match
from conftest import FIXTURES
from runs import claim_assessment, recheck, sources
from runs.build_run import build_table
from runs.check_load import bind_on_load

READING = json.loads((FIXTURES / "ions_ars_2026_p95.reading.json").read_text())
TABLE = READING["table"]
COLUMNS = TABLE["columns"]
HIDDEN = {"origin": "hidden_text", "overlays_supported": False,
          "reason": "The page's text layer has only nonvisual sliver geometry."}
NATIVE = {"visual_evidence": None, "derived_words_present": False}
GEOMETRY = ("selector_match", "row_column_match")


def _first_cell():
    row = TABLE["rows"][0]
    return row, row["cells"][0]


def _slivers(words_doc):
    """The real page's words, each squeezed to a nonvisual sliver: the page classifies as hidden_text."""
    slivers = copy.deepcopy(words_doc)
    for word in slivers["words"]:
        middle = (word["y0"] + word["y1"]) / 2
        word["y0"], word["y1"] = middle - 0.28, middle + 0.28
    return slivers


def _write(run_dir, rel, raw: bytes) -> str:
    path = run_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _hidden_page(run_dir, words_doc):
    slivers = _slivers(words_doc)
    page = {"page_number": 95, "words": "pages/p95.words.json",
            "width_pt": slivers["page_width"], "height_pt": slivers["page_height"]}
    page["words_sha256"] = _write(run_dir, page["words"], json.dumps(slivers).encode())
    page["visual_evidence"] = sources.visual_evidence(page, slivers)
    assert page["visual_evidence"]["origin"] == "hidden_text"
    consumed = {"file": page["words"], "sha256": page["words_sha256"], "kind": "captured"}
    return page, slivers, consumed


def _derived_page(run_dir, words_doc):
    """A page whose words come from a transcription of its image (derived words; geometry approximate)."""
    page = {"page_number": 95, "image": "pages/p95.png", "words": "pages/p95.words.json",
            "width_pt": words_doc["page_width"], "height_pt": words_doc["page_height"]}
    page["image_sha256"] = _write(run_dir, page["image"], b"page image bytes")
    page["words_sha256"] = _write(run_dir, page["words"], json.dumps(words_doc).encode())
    derived_sha = _write(run_dir, "pages/p95.derived.json", json.dumps(words_doc).encode())
    page["derived_words"] = {"kind": "model_transcription", "file": "pages/p95.derived.json",
                             "sha256": derived_sha, "based_on_image_sha256": page["image_sha256"]}
    page["visual_evidence"] = sources.visual_evidence(page, words_doc)
    consumed = {"file": "pages/p95.derived.json", "sha256": derived_sha, "kind": "derived"}
    return page, consumed


def _run(source, table):
    return {"schema_version": "1", "run": {"id": "slice1b"}, "sources": [source], "rows": [], "tables": [table]}


def _context(row, cell):
    return {"page_number": 95, "value": cell.get("value"), "box": cell.get("box"), "column": cell.get("column"),
            "row_label": row.get("label"), "columns": list(COLUMNS)}


# ---------------------------------------------------------------- E1: build -> load on a hidden_text page


def test_direct_build_table_on_a_hidden_text_page_loads_could_not_run_and_stays_current(tmp_path, words_doc):
    page, slivers, consumed = _hidden_page(tmp_path, words_doc)
    source = {"id": "src", "kind": "filing", "published_date": "2026-10-27", "pages": [page]}
    table = build_table(READING, slivers, table_id="t1", source_id="src", page_number=95, source=source,
                        consumed_artifact=consumed, visual_evidence=page["visual_evidence"],
                        derived_words_present=False)
    built = [cell for row in table["rows"] for cell in row["cells"]]
    assert len(built) == 69
    for cell in built:
        by_name = {c["name"]: c for c in cell["checks"]}
        assert cell["status"] != MATCHED
        for name in GEOMETRY:
            assert (by_name[name]["result"], by_name[name]["reason"]) == (COULD_NOT_RUN, HIDDEN["reason"])
        # the stamp is consumed by the binding: run.json never stores it
        assert all("page_capability" not in c for c in cell["checks"])
        assert by_name["selector_match"]["assessment"]["inputs"]["page_capability"] == {
            "visual_origin": "hidden_text", "derived_words_present": False}

    run = _run(source, table)
    (tmp_path / "run.json").write_text(json.dumps(run))
    loaded = bind_on_load(json.loads((tmp_path / "run.json").read_text()), tmp_path)
    for row in loaded["tables"][0]["rows"]:
        for cell in row["cells"]:
            assert "check_history" not in cell          # bound with the right capability: current, not re-run
            by_name = {c["name"]: c for c in cell["checks"]}
            assert cell["status"] != MATCHED
            for name in GEOMETRY:
                assert (by_name[name]["result"], by_name[name]["reason"]) == (COULD_NOT_RUN, HIDDEN["reason"])


# ---------------------------------------------------------------- E2: unflagged calls raise TypeError


def test_unflagged_calls_raise_type_error(words_doc):
    row, cell = _first_cell()
    with pytest.raises(TypeError):
        check_cell(cell, words_doc)
    with pytest.raises(TypeError):
        check_cell(cell, words_doc, row["label"], COLUMNS, visual_evidence=None)
    with pytest.raises(TypeError):
        check_reading(READING, words_doc)
    with pytest.raises(TypeError):
        build_table(READING, words_doc, table_id="t1", source_id="src", page_number=95)
    with pytest.raises(TypeError):
        recheck.recheck_cell("2026-11-01T00:00:00+00:00", cell, words_doc, row_label=row["label"], columns=COLUMNS)
    with pytest.raises(TypeError):
        recheck._recheck_cell("2026-11-01T00:00:00+00:00", cell, words_doc, False, None, 95)
    from accuracy.score import score_planted
    with pytest.raises(TypeError):
        score_planted(READING, words_doc, [])


@pytest.mark.xfail(strict=True, reason="TODO(SLICE-1B-001 after PERF-001): runs/check_load.py:73 must pass "
                                        "capability= before bind_contract_checks can drop its stamp default")
def test_unflagged_bind_contract_checks_raises_type_error(words_doc):
    row, cell = _first_cell()
    checks = check_cell(cell, words_doc, row["label"], COLUMNS, **NATIVE)["checks"]
    with pytest.raises(TypeError):
        claim_assessment.bind_contract_checks(checks, _context(row, cell), {"id": "src", "pages": []})


def test_an_unstamped_check_binds_no_capability_and_is_never_current(words_doc):
    """Until the TypeError above holds, a check that carries no capability binds None (fail closed)."""
    row, cell = _first_cell()
    checks = [{k: v for k, v in c.items() if k != "page_capability"}
              for c in check_cell(cell, words_doc, row["label"], COLUMNS, **NATIVE)["checks"]]
    page = {"page_number": 95, "words": "w.json", "words_sha256": "a" * 64,
            "visual_evidence": {"origin": "text_layer", "overlays_supported": True, "reason": None}}
    source = {"id": "src", "kind": "filing", "pages": [page]}
    consumed = {"file": "w.json", "sha256": "a" * 64, "kind": "captured"}
    bound = claim_assessment.bind_contract_checks(checks, _context(row, cell), source, consumed_artifact=consumed)
    for check in bound:
        assert check["assessment"]["inputs"]["page_capability"] is None
        assert not claim_assessment.check_current(check, _context(row, cell), source, consumed_artifact=consumed)


# ---------------------------------------------------------------- E3: a binding without capability re-runs on load


@pytest.mark.parametrize("bound_with", ["absent", "native"])
def test_a_hidden_text_pass_bound_without_its_capability_reruns_on_load(tmp_path, words_doc, bound_with):
    page, slivers, consumed = _hidden_page(tmp_path, words_doc)
    source = {"id": "src", "kind": "filing", "published_date": "2026-10-27", "pages": [page]}
    row, cell = _first_cell()
    # the old hole: geometry run as if the page were native, on the hidden page's own (real-x) words
    checked = check_cell(cell, words_doc, row["label"], COLUMNS, **NATIVE)
    assert [c["result"] for c in checked["checks"]] == [PASS, PASS, PASS]
    checks = checked["checks"]
    if bound_with == "absent":
        checks = [{k: v for k, v in c.items() if k != "page_capability"} for c in checks]
    bound = claim_assessment.bind_contract_checks(checks, _context(row, cell), source, consumed_artifact=consumed)
    for check in bound:
        assert not claim_assessment.check_current(check, _context(row, cell), source, consumed_artifact=consumed)

    saved = {"id": "t1.0.0", "column": cell["column"], "value": cell["value"], "box": cell["box"],
             "status": MATCHED, "checks": bound}
    table = {"id": "t1", "source_id": "src", "page_number": 95, "columns": list(COLUMNS),
             "rows": [{"label": row["label"], "cells": [saved]}]}
    (tmp_path / "run.json").write_text(json.dumps(_run(source, table)))
    loaded = bind_on_load(json.loads((tmp_path / "run.json").read_text()), tmp_path)
    got = loaded["tables"][0]["rows"][0]["cells"][0]
    by_name = {c["name"]: c for c in got["checks"]}
    for name in GEOMETRY:
        assert (by_name[name]["result"], by_name[name]["reason"]) == (COULD_NOT_RUN, HIDDEN["reason"])
    assert got["status"] != MATCHED
    assert got["check_history"] == bound                    # the old PASS checks are history, not deleted
    for check in got["checks"]:
        assert claim_assessment.check_current(check, _context(row, cell), source, consumed_artifact=consumed)


def test_a_derived_page_pass_bound_without_its_capability_reruns_on_load(tmp_path, words_doc):
    page, consumed = _derived_page(tmp_path, words_doc)
    assert page["visual_evidence"]["origin"] == "approximate"
    source = {"id": "src", "kind": "filing", "published_date": "2026-10-27", "pages": [page]}
    row, cell = _first_cell()
    checked = check_cell(cell, words_doc, row["label"], COLUMNS, **NATIVE)
    assert [c["result"] for c in checked["checks"]] == [PASS, PASS, PASS]
    bound = claim_assessment.bind_contract_checks(checked["checks"], _context(row, cell), source,
                                                  consumed_artifact=consumed)
    for check in bound:
        assert not claim_assessment.check_current(check, _context(row, cell), source, consumed_artifact=consumed)
    saved = {"id": "t1.0.0", "column": cell["column"], "value": cell["value"], "box": cell["box"],
             "status": MATCHED, "checks": bound}
    table = {"id": "t1", "source_id": "src", "page_number": 95, "columns": list(COLUMNS),
             "rows": [{"label": row["label"], "cells": [saved]}]}
    (tmp_path / "run.json").write_text(json.dumps(_run(source, table)))
    loaded = bind_on_load(json.loads((tmp_path / "run.json").read_text()), tmp_path)
    got = loaded["tables"][0]["rows"][0]["cells"][0]
    by_name = {c["name"]: c for c in got["checks"]}
    for name in GEOMETRY:
        assert (by_name[name]["result"], by_name[name]["reason"]) == (COULD_NOT_RUN,
                                                                      restriction.CELL_DERIVED_REASON)
    assert got["status"] != MATCHED
    assert got["check_history"] == bound
    assert by_name["text_layer_match"]["reason"] == "The value appears in the page's transcribed words."


def test_a_derived_flag_alone_is_a_different_capability():
    native = restriction.capability(None, False)
    assert restriction.capability(None, True) != native
    assert restriction.capability({"origin": "text_layer", "derived_words": True}, False) != native
    assert restriction.page_capability({"derived_words": {"file": "d"}}) == restriction.capability(None, True)


# ---------------------------------------------------------------- E4: one shared helper, both paths


CASES = [
    ({"origin": "hidden_text", "reason": "Own reason."}, False),
    ({"origin": "hidden_text"}, False),                         # no "reason" key: must not raise
    ({"origin": "approximate", "reason": "x"}, False),
    ({"origin": "derived"}, False),
    ({"origin": "text_layer", "derived_words": True}, False),
    ({"origin": "text_layer", "reason": None}, True),
    ({"origin": "text_layer", "reason": None}, False),
    (None, False),
]


@pytest.mark.parametrize("visual, derived", CASES)
def test_row_and_cell_paths_take_the_same_decision_from_the_same_inputs(visual, derived):
    row = restriction.page_restriction(visual, derived, path=restriction.ROW, box=[1, 2, 3, 4])
    cell = restriction.page_restriction(visual, derived, path=restriction.CELL)
    assert (row.restricted, row.kind) == (cell.restricted, cell.kind)
    if row.kind == restriction.HIDDEN_TEXT:
        assert row.reason == cell.reason == ((visual or {}).get("reason") or restriction.HIDDEN_TEXT_REASON)
    elif row.kind == restriction.DERIVED:
        # the two paths keep their own current sentences
        assert (row.reason, cell.reason) == (restriction.ROW_DERIVED_REASON, restriction.CELL_DERIVED_REASON)
    else:
        assert row.reason is cell.reason is None


def test_no_box_on_a_restricted_page_gives_the_selectors_own_no_place_reason():
    got = restriction.page_restriction({"origin": "hidden_text"}, False, path=restriction.ROW, box=None)
    assert got == (True, restriction.HIDDEN_TEXT, restriction.NO_PLACE_REASON)
    from checks.selector_match import selector_match
    assert selector_match("x", None, [], 612.0, 792.0).reason == restriction.NO_PLACE_REASON
    assert restriction.page_restriction(None, False, path=restriction.ROW, box=None).restricted is False


def _row_on(page, words_doc):
    row = {"id": "e1", "source_id": "src", "page_number": 95, "quote": "Royalty revenue", "box": [1, 1, 50, 20],
           "date_text": None, "date": None, "date_end": None, "event_type": "other", "title": "t", "program": None}
    source = {"id": "src", "kind": "filing", "published_date": "2026-10-27", "pages": [page]}
    results = recheck.row_checks(row, source, words_doc)
    return {r.check: r for r in results}


@pytest.mark.parametrize("visual, derived", [c for c in CASES if restriction.page_kind(*c) is not None])
def test_the_row_path_and_the_cell_path_use_the_helper(words_doc, visual, derived):
    page = {"page_number": 95, "visual_evidence": visual}
    if derived:
        page["derived_words"] = {"file": "d.json"}
    row_checks = _row_on(page, words_doc)
    want_row = restriction.page_restriction(visual, derived, path=restriction.ROW, box=[1, 1, 50, 20])
    assert (row_checks["selector_match"].result, row_checks["selector_match"].reason) == (COULD_NOT_RUN,
                                                                                        want_row.reason)
    row, cell = _first_cell()
    got = check_cell(cell, words_doc, row["label"], COLUMNS, visual_evidence=visual, derived_words_present=derived)
    want_cell = restriction.page_restriction(visual, derived, path=restriction.CELL)
    for check in got["checks"][1:]:
        assert (check["result"], check["reason"]) == (COULD_NOT_RUN, want_cell.reason)


def test_hidden_text_without_a_reason_key_does_not_raise_on_either_path(words_doc):
    row, cell = _first_cell()
    got = check_cell(cell, words_doc, row["label"], COLUMNS, visual_evidence={"origin": "hidden_text"},
                     derived_words_present=False)
    assert got["checks"][1]["reason"] == restriction.HIDDEN_TEXT_REASON
    checks = _row_on({"page_number": 95, "visual_evidence": {"origin": "hidden_text"}}, words_doc)
    assert checks["selector_match"].reason == restriction.HIDDEN_TEXT_REASON


# ---------------------------------------------------------------- E5: wording on restricted vs native pages


def test_text_layer_match_says_transcribed_words_only_on_a_restricted_page(words_doc):
    row, cell = _first_cell()
    native = check_cell(cell, words_doc, row["label"], COLUMNS, **NATIVE)["checks"][0]
    hidden = check_cell(cell, words_doc, row["label"], COLUMNS, visual_evidence=HIDDEN,
                        derived_words_present=False)["checks"][0]
    derived = check_cell(cell, words_doc, row["label"], COLUMNS, visual_evidence=None,
                         derived_words_present=True)["checks"][0]
    assert native["reason"] == "The value appears in the page text."
    assert hidden["reason"] == derived["reason"] == "The value appears in the page's transcribed words."
    assert native["label"] == hidden["label"] == derived["label"] == TEXT_LABEL == "Matches source text"
    assert (text_layer_match("no such value", words_doc["words"], transcribed=True).reason
            == "The value does not appear anywhere in the page's transcribed words.")
    assert (text_layer_match("no such value", words_doc["words"]).reason
            == "The value does not appear anywhere in the page text.")


def test_quote_match_says_transcribed_words_only_on_a_restricted_page(words_doc):
    native = _row_on({"page_number": 95, "visual_evidence": {"origin": "text_layer"}}, words_doc)["quote_match"]
    hidden = _row_on({"page_number": 95, "visual_evidence": HIDDEN}, words_doc)["quote_match"]
    derived = _row_on({"page_number": 95, "derived_words": {"file": "d.json"},
                       "visual_evidence": {"origin": "approximate"}}, words_doc)["quote_match"]
    assert native.result == hidden.result == derived.result == PASS
    assert native.reason == "The quote is printed on the page, word for word."
    assert hidden.reason == derived.reason == "The quote appears in the page's transcribed words, word for word."
    assert native.label == hidden.label == QUOTE_LABEL == "Quote found on page"
    assert (quote_match("no such quote at all here", words_doc["words"], transcribed=True).reason
            == "The quote does not appear anywhere in the page's transcribed words.")
    assert SELECTOR_LABEL == "Citation located"
    assert REQUIRED_CHECKS == ("text_layer_match", "selector_match", "row_column_match")
