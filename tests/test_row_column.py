"""R3: the value's box must sit on its row label's line and under its column header."""

import json

import pytest

from checks.common import COULD_NOT_RUN, FAIL, PASS
from checks.row_column_match import LABEL, row_column_match
from checks.selector_match import selector_match
from checks.status import FAILED, MATCHED, PROPOSED, check_cell, check_reading
from conftest import FIXTURES, box_of, find_word

READING = json.loads((FIXTURES / "ions_ars_2026_p95.reading.json").read_text())
TABLE = READING["table"]
COLUMNS = TABLE["columns"]


def _row(label):
    return next(r for r in TABLE["rows"] if r["label"] == label)


def _cell(label, column):
    return next(c for c in _row(label)["cells"] if c["column"] == column)


def _doc(words, words_doc):
    return {"words": words, "page_width": words_doc["page_width"], "page_height": words_doc["page_height"]}


def test_every_real_cell_is_on_its_row_and_under_its_column(words):
    results = [row_column_match(r["label"], c["column"], c["box"], words, COLUMNS)
               for r in TABLE["rows"] for c in r["cells"]]
    assert len(results) == 69
    assert [x.reason for x in results if x.result != PASS] == []
    assert {x.label for x in results} == {LABEL}


def test_every_real_cell_is_matched_with_all_three_checks(words_doc):
    checked = check_reading(READING, words_doc)
    assert checked["counts"] == {MATCHED: 69, FAILED: 0, PROPOSED: 0}
    for row in checked["rows"]:
        for c in row["cells"]:
            assert [k["check"] for k in c["checks"]] == ["text_layer_match", "selector_match", "row_column_match"]


def test_a_2024_value_under_the_2025_column_fails(words, words_doc):
    other = _cell("Royalty revenue", "2024")
    r = row_column_match("Royalty revenue", "2025", other["box"], words, COLUMNS)
    assert r.result == FAIL
    assert r.reason == "The box sits under 2024, not 2025."
    assert r.compared["value"] == "row Royalty revenue, column 2025"
    assert r.compared["found_column"] == "2024"
    cell = {"column": "2025", "value": other["value"], "box": other["box"]}
    got = check_cell(cell, _doc(words, words_doc), "Royalty revenue", COLUMNS)
    assert got["status"] == FAILED
    # the other two checks pass: only the new check catches it
    assert [k["result"] for k in got["checks"]] == [PASS, PASS, FAIL]


@pytest.mark.parametrize("column", ["2024", "2023"])
def test_a_2025_value_under_a_later_column_fails(words, column):
    r = row_column_match("Net loss", column, _cell("Net loss", "2025")["box"], words, COLUMNS)
    assert r.result == FAIL and r.reason == f"The box sits under 2025, not {column}."


def test_another_rows_value_fails(words, words_doc):
    sales = _cell("Product sales, net", "2025")
    r = row_column_match("Royalty revenue", "2025", sales["box"], words, COLUMNS)
    assert r.result == FAIL
    assert r.reason == "The row label 'Royalty revenue' is not on this line; found 'Product sales, net'."
    assert r.compared["found"] == "row Product sales, net, column 2025"
    got = check_cell({"column": "2025", "value": sales["value"], "box": sales["box"]},
                     _doc(words, words_doc), "Royalty revenue", COLUMNS)
    assert got["status"] == FAILED


def test_the_dollar_sign_is_not_part_of_the_label(words):
    # "Net loss" and "Basic and diluted ..." have a "$" between the label and the 2025 value
    for label in ("Net loss", "Basic and diluted net loss per share", "Product sales, net"):
        assert row_column_match(label, "2025", _cell(label, "2025")["box"], words, COLUMNS).result == PASS


def test_without_the_columns_list_the_header_line_gives_the_bands(words):
    r = row_column_match("Royalty revenue", "2025", _cell("Royalty revenue", "2024")["box"], words)
    assert r.result == FAIL and "under 2024" in r.reason
    assert row_column_match("Royalty revenue", "2023", _cell("Royalty revenue", "2023")["box"], words).result == PASS


def test_a_label_printed_over_two_lines(words):
    """Move the start of a long label onto the line above, as a narrow page would print it."""
    label = "Interest expense related to sale of future royalties"
    line_y0 = find_word(words, "royalties")["y0"]
    wrapped = []
    for w in words:
        if abs(w["y0"] - line_y0) < 1 and w["x1"] < 177.5:   # "Interest expense related to sale of"
            wrapped.append({**w, "y0": w["y0"] - 12, "y1": w["y1"] - 12})
        elif abs(w["y0"] - (line_y0 - 12)) < 1:              # the row above makes room
            continue
        else:
            wrapped.append(w)
    box = _cell(label, "2025")["box"]
    assert row_column_match(label, "2025", box, wrapped, COLUMNS).result == PASS
    r = row_column_match("Interest expense", "2025", box, wrapped, COLUMNS)
    assert r.result == FAIL and "found 'future royalties'" in r.reason


def test_a_wrapped_label_does_not_borrow_a_line_with_values(words):
    # the line above "Royalty revenue" holds values, so it is never read as the start of the label
    r = row_column_match("Product sales, net Royalty revenue", "2025", _cell("Royalty revenue", "2025")["box"],
                         words, COLUMNS)
    assert r.result == FAIL


def test_could_not_run_without_a_box(words):
    assert row_column_match("Royalty revenue", "2025", None, words, COLUMNS).result == COULD_NOT_RUN


def test_could_not_run_when_the_header_is_not_on_the_page(words):
    r = row_column_match("Royalty revenue", "2026", _cell("Royalty revenue", "2025")["box"], words,
                         ["2026", "2024", "2023"])
    assert r.result == COULD_NOT_RUN and "2026" in r.reason


def test_could_not_run_when_the_headers_are_below_the_box(words):
    # a box above the header line: no header above it
    r = row_column_match("Year Ended December 31,", "2025", box_of(find_word(words, "Year")), words, COLUMNS)
    assert r.result == COULD_NOT_RUN


def test_could_not_run_when_no_label_is_on_the_line(words, words_doc):
    box = _cell("Royalty revenue", "2025")["box"]
    no_labels = [w for w in words if not (abs(w["y0"] - box[1]) < 1 and w["x1"] < 300)]
    r = row_column_match("Royalty revenue", "2025", box, no_labels, COLUMNS)
    assert r.result == COULD_NOT_RUN and "No row label" in r.reason


def test_no_row_label_given_leaves_the_value_proposed(words, words_doc):
    c = _cell("Royalty revenue", "2025")
    got = check_cell({"column": "2025", "value": c["value"], "box": c["box"]}, _doc(words, words_doc))
    assert got["checks"][2]["result"] == COULD_NOT_RUN
    assert got["status"] == PROPOSED


def test_a_broken_box_fails(words):
    assert row_column_match("Royalty revenue", "2025", [1, 2, 3], words, COLUMNS).result == FAIL
    assert row_column_match("Royalty revenue", "2025", [10, 10, 5, 20], words, COLUMNS).result == FAIL


# ------------------------------------------------------------ the tighter box rule


W, H = 612.0, 792.0


def test_selector_fails_a_box_reaching_into_the_next_cell(words):
    word = find_word(words, "285,529")
    nxt = find_word(words, "257,307")
    box = [word["x0"] - 0.5, word["y0"] - 0.5, nxt["x0"] + (nxt["x1"] - nxt["x0"]) * 0.3, word["y1"] + 0.5]
    r = selector_match("285,529", box, words, W, H)
    assert r.result == FAIL and r.reason == "The box also covers 257,307."


def test_selector_fails_a_box_holding_two_cells_and_names_the_extra(words):
    a, b = find_word(words, "285,529"), find_word(words, "257,307")
    r = selector_match("285,529", box_of(a, b), words, W, H)
    assert r.result == FAIL and r.reason == "The box also covers 257,307."


def test_selector_fails_a_box_covering_half_the_value(words):
    w = find_word(words, "285,529")
    box = [w["x0"] - 0.5, w["y0"] - 0.5, (w["x0"] + w["x1"]) / 2, w["y1"] + 0.5]
    r = selector_match("285,529", box, words, W, H)
    assert r.result == FAIL and "partly covers 285,529" in r.reason


def test_selector_ignores_a_word_the_box_only_grazes(words):
    w = find_word(words, "285,529")
    above = find_word(words, "115,315")
    # reach 0.5pt into the line above: about 5% of that word's height
    box = [w["x0"] - 0.5, above["y1"] - 0.5, w["x1"] + 0.5, w["y1"] + 0.5]
    assert selector_match("285,529", box, words, W, H).result == PASS


def test_selector_still_passes_every_real_cell(words):
    for r in TABLE["rows"]:
        for c in r["cells"]:
            assert selector_match(c["value"], c["box"], words, W, H).result == PASS, (r["label"], c)


# ------------------------------------------------------------ Codex review of 71b370b (PR #7)
# The check assesses the row and column the saved table shows. Nothing the reader adds (a label on the
# cell, a shorter list of columns) can change that association or hide a header printed on the page.

import copy
from itertools import combinations

from capture.read_table import parse_table
from runs.build_run import build_table
from runs.recheck import recheck_cell


def _reading_from(table: dict) -> dict:
    """The table as the model would answer it, through the real parser, as a reading."""
    parsed, problem = parse_table(json.dumps(table))
    assert problem is None
    return {**READING, "table": parsed, "raw_response": json.dumps(table)}


def _built(reading, words_doc):
    return build_table(reading, words_doc, table_id="t1", source_id="src", page_number=95)


def _moved_sales_table(label="Product sales, net"):
    """Product sales' 2025 value and box saved in the Royalty revenue row, with a cell label."""
    table = copy.deepcopy(TABLE)
    sales = _cell("Product sales, net", "2025")
    row = next(r for r in table["rows"] if r["label"] == "Royalty revenue")
    i = next(i for i, c in enumerate(row["cells"]) if c["column"] == "2025")
    row["cells"][i] = {"column": "2025", "value": sales["value"], "box": sales["box"], "label": label}
    return table


def test_codex7_a_cell_label_does_not_override_the_row_it_is_saved_in(words_doc):
    reading = _reading_from(_moved_sales_table())
    checked = check_reading(reading, words_doc)
    row = next(r for r in checked["rows"] if r["label"] == "Royalty revenue")
    cell = next(c for c in row["cells"] if c["column"] == "2025")
    rc = cell["checks"][2]
    assert rc["check"] == "row_column_match" and rc["result"] == FAIL
    assert rc["reason"] == ("The reader labelled this cell 'Product sales, net' but it sits in row "
                            "'Royalty revenue'.")
    assert rc["compared"]["reader_label"] == "Product sales, net"
    assert cell["status"] == FAILED
    assert checked["counts"] == {MATCHED: 68, FAILED: 1, PROPOSED: 0}
    # the saved table: the Royalty revenue row does not hold the moved value as matched
    built = _built(reading, words_doc)
    saved = next(r for r in built["rows"] if r["label"] == "Royalty revenue")
    moved = next(c for c in saved["cells"] if c["column"] == "2025")
    assert moved["value"] == _cell("Product sales, net", "2025")["value"]
    assert moved["status"] == FAILED
    assert [c["result"] for c in moved["checks"]] == [PASS, PASS, FAIL]


def test_codex7_without_the_cell_label_the_moved_value_still_fails_on_its_row(words_doc):
    table = _moved_sales_table()
    for r in table["rows"]:
        for c in r["cells"]:
            c.pop("label", None)
    checked = check_reading(_reading_from(table), words_doc)
    row = next(r for r in checked["rows"] if r["label"] == "Royalty revenue")
    cell = next(c for c in row["cells"] if c["column"] == "2025")
    assert cell["status"] == FAILED
    assert "found 'Product sales, net'" in cell["checks"][2]["reason"]


def test_codex7_a_cell_label_equal_to_its_row_label_is_fine(words_doc):
    table = copy.deepcopy(TABLE)
    for r in table["rows"]:
        for c in r["cells"]:
            c["label"] = "  " + r["label"] + " "
    assert check_reading(_reading_from(table), words_doc)["counts"] == {MATCHED: 69, FAILED: 0, PROPOSED: 0}


def test_codex7_a_cell_label_in_a_row_with_no_label_fails(words_doc):
    sales = _cell("Product sales, net", "2025")
    table = {"title": "", "columns": COLUMNS, "rows": [
        {"cells": [{"column": "2025", "value": sales["value"], "box": sales["box"],
                    "label": "Product sales, net"}]}]}
    checked = check_reading(_reading_from(table), words_doc)
    cell = checked["rows"][0]["cells"][0]
    assert checked["rows"][0]["label"] == ""
    assert cell["status"] == FAILED
    assert cell["checks"][2]["reason"] == ("The reader labelled this cell 'Product sales, net' but it sits in a "
                                           "row with no label.")


def test_codex7_the_recheck_path_uses_the_saved_row_label(words, words_doc):
    sales = _cell("Product sales, net", "2025")
    cell = {"id": "t1.1.0", "column": "2025", "value": sales["value"], "box": sales["box"],
            "label": "Product sales, net"}
    line = recheck_cell("2026-10-28T00:00:00+00:00", cell, _doc(words, words_doc),
                        row_label="Royalty revenue", columns=COLUMNS)
    rc = next(c for c in line["recheck"]["checks"] if c["name"] == "row_column_match")
    assert rc["result"] == FAIL and "sits in row 'Royalty revenue'" in rc["reason"]


def test_codex7_a_cell_checked_on_its_own_keeps_its_label(words, words_doc):
    c = _cell("Royalty revenue", "2025")
    got = check_cell({"column": "2025", "value": c["value"], "box": c["box"], "label": "Royalty revenue"},
                     _doc(words, words_doc))
    assert got["status"] == MATCHED


# ---- the omitted header

OMIT_2024 = ["2025", "2023"]
REAL_2024_BOX = [472.42, 153.13, 504.96, 164.15]


def test_codex7_the_static_geometry_of_the_omission_case(words):
    """Codex's numbers: with 2024 left out, the 2025/2023 midpoint is left of the real 2024 cell's centre,
    so bands from the reader's list alone would have put it under 2023."""
    real = _cell("Royalty revenue", "2024")
    assert real["value"] == "257,307" and real["box"] == REAL_2024_BOX
    h25, h23 = find_word(words, "2025"), find_word(words, "2023")
    mid = ((h25["x0"] + h25["x1"]) / 2 + (h23["x0"] + h23["x1"]) / 2) / 2
    assert round(mid, 3) == 481.215
    assert round((REAL_2024_BOX[0] + REAL_2024_BOX[2]) / 2, 2) == 488.69 > mid


def test_codex7_an_omitted_header_does_not_widen_another_column(words):
    r = row_column_match("Royalty revenue", "2023", REAL_2024_BOX, words, OMIT_2024)
    assert r.result == FAIL
    assert r.reason == "The box sits under 2024, not 2023."
    assert r.compared["found_column"] == "2024"


def test_codex7_the_omission_case_through_parsing_check_reading_and_build_table(words_doc):
    table = copy.deepcopy(TABLE)
    table["columns"] = OMIT_2024
    row = next(r for r in table["rows"] if r["label"] == "Royalty revenue")
    row["cells"] = [c for c in row["cells"] if c["column"] != "2023"]
    for c in row["cells"]:
        if c["column"] == "2024":
            c["column"] = "2023"
    reading = _reading_from(table)
    checked = check_reading(reading, words_doc)
    crow = next(r for r in checked["rows"] if r["label"] == "Royalty revenue")
    claimed = next(c for c in crow["cells"] if c["column"] == "2023")
    assert claimed["value"] == "257,307"
    assert [k["result"] for k in claimed["checks"]] == [PASS, PASS, FAIL]
    assert claimed["checks"][2]["reason"] == "The box sits under 2024, not 2023."
    assert claimed["status"] == FAILED
    built = _built(reading, words_doc)
    srow = next(r for r in built["rows"] if r["label"] == "Royalty revenue")
    assert next(c for c in srow["cells"] if c["column"] == "2023")["status"] == FAILED


def test_codex7_the_omission_case_on_the_recheck_path(words, words_doc):
    cell = {"id": "t1.1.1", "column": "2023", "value": "257,307", "box": REAL_2024_BOX}
    line = recheck_cell("2026-10-28T00:00:00+00:00", cell, _doc(words, words_doc),
                        row_label="Royalty revenue", columns=OMIT_2024)
    rc = next(c for c in line["recheck"]["checks"] if c["name"] == "row_column_match")
    assert rc["result"] == FAIL and rc["reason"] == "The box sits under 2024, not 2023."


def test_codex7_omitting_the_first_column_does_not_widen_the_next(words):
    box = _cell("Royalty revenue", "2025")["box"]
    r = row_column_match("Royalty revenue", "2024", box, words, ["2024", "2023"])
    assert r.result == FAIL and r.reason == "The box sits under 2025, not 2024."


def test_codex7_omitting_the_last_column_does_not_widen_the_one_before(words):
    box = _cell("Royalty revenue", "2023")["box"]
    r = row_column_match("Royalty revenue", "2024", box, words, ["2025", "2024"])
    assert r.result == FAIL and r.reason == "The box sits under 2023, not 2024."


@pytest.mark.parametrize("size", [1, 2, 3])
def test_codex7_every_real_cell_passes_under_any_list_of_columns(words, size):
    """Leaving columns out never changes a true answer: each real cell still passes when its own column is
    in the list, whatever else is left out (the complete three-column list included)."""
    for cols in combinations(COLUMNS, size):
        for r in TABLE["rows"]:
            for c in r["cells"]:
                if c["column"] in cols:
                    got = row_column_match(r["label"], c["column"], c["box"], words, list(cols))
                    assert got.result == PASS, (cols, r["label"], c["column"], got.reason)


@pytest.mark.parametrize("size", [1, 2])
def test_codex7_every_wrong_year_fails_under_any_short_list(words, size):
    """Every real value claimed under another listed year fails, however the list is shortened."""
    for cols in combinations(COLUMNS, size):
        for r in TABLE["rows"]:
            for c in r["cells"]:
                for claim in cols:
                    if claim == c["column"]:
                        continue
                    got = row_column_match(r["label"], claim, c["box"], words, list(cols))
                    assert got.result == FAIL, (cols, r["label"], c["column"], claim)
                    assert got.reason == f"The box sits under {c['column']}, not {claim}."


def test_codex7_a_stub_heading_on_the_header_line_is_not_a_column(words):
    """A heading such as "(in thousands)" left of the year headers does not become a value column, so the
    row labels are still read."""
    h = find_word(words, "2025")
    stub = {**h, "text": "(in thousands)", "x0": 60.0, "x1": 120.0}
    with_stub = words + [stub]
    for label in ("Royalty revenue", "Net loss"):
        c = _cell(label, "2025")
        assert row_column_match(label, "2025", c["box"], with_stub, COLUMNS).result == PASS
        assert row_column_match(label, "2025", c["box"], with_stub, ["2025"]).result == PASS
