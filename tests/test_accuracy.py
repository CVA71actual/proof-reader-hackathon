"""R4b-1: the accuracy scorer, the mistake type codes, the answer-key checker and the report."""

import copy
import hashlib
import json
import re

import pytest

from accuracy import types as T
from accuracy.key import KeyRefused, check_key, load_key, raise_if_refused
from accuracy.mistake_log import load_run, mistake_log
from accuracy.report import build_report, render_text
from accuracy.run import main as run_main, score_reading
from accuracy.score import model_family, period_of, score_events, score_planted, score_table
from checks.event_checks import date_match, quote_match, source_rule
from checks.row_column_match import row_column_match
from checks.selector_match import selector_match
from checks.status import check_cell, check_reading
from checks.text_layer_match import text_layer_match
from runs.state import current_state
from tests.conftest import FIXTURES, REPO

KEYS = REPO / "accuracy" / "keys"
IONS_KEY = KEYS / "ions_ars_2026_p95.key.json"
PTGX_KEY = KEYS / "ptgx_8k_2026-08-05_ex991_p3.key.json"
PLANTED = KEYS / "ions_ars_2026_p95.planted.json"
READING = FIXTURES / "ions_ars_2026_p95.reading.json"
WORDS = FIXTURES / "ions_ars_2026_p95.words.json"
PTGX_RUN = REPO / "runs" / "ptgx-r4a"
needs_ptgx = pytest.mark.skipif(not (PTGX_RUN / "run.json").exists(),
                                reason="runs/ptgx-r4a arrives with R4a (PR #11); scored once it is on main")


@pytest.fixture(scope="module")
def key():
    return load_key(IONS_KEY)


@pytest.fixture(scope="module")
def reading():
    return json.loads(READING.read_text())


@pytest.fixture(scope="module")
def checked(reading, words_doc):
    return check_reading(reading, words_doc, visual_evidence=None, derived_words_present=False)


@pytest.fixture(scope="module")
def plants():
    return json.loads(PLANTED.read_text())["plants"]


# ---------------------------------------------------------------- key


def test_ions_key_passes_its_check(key, words_doc):
    assert key["sealed"] is False
    assert len(key["cells"]) == 69
    assert check_key(key, words_doc, WORDS.read_bytes()) == []


def test_ions_key_was_written_from_the_page_not_copied_from_the_reading(key, reading):
    # same 69 entries, but the key's file is not the reader's: it holds every (row, column, value) once
    read = {(r["label"], c["column"], c["value"]) for r in reading["table"]["rows"] for c in r["cells"]}
    assert {(c["row_label"], c["column"], c["value"]) for c in key["cells"]} == read


def test_key_checker_refuses_a_typo(key, words_doc):
    bad = copy.deepcopy(key)
    bad["cells"][0]["value"] = "115,351"         # the page prints 115,315
    bad["cells"][5]["row_label"] = "Royalty revenues"
    problems = check_key(bad, words_doc)
    assert [(p["where"], p["index"]) for p in problems] == [("cells", 0), ("cells", 5)]
    assert "115,351 is not printed" in problems[0]["problem"]
    assert "Royalty revenues" in problems[1]["problem"]
    with pytest.raises(KeyRefused) as e:
        raise_if_refused(bad, words_doc)
    assert len(e.value.problems) == 2


def test_key_checker_refuses_a_duplicate_and_a_quote_off_the_page(key, words_doc):
    bad = copy.deepcopy(key)
    bad["cells"].append(dict(bad["cells"][0]))
    bad["events"] = [{"ticker": "IONS", "program": "WAINUA", "event_type": "launch", "date_text": "Q1 2027",
                      "quote": "WAINUA launch expected in Q1 2027."}]
    whys = [p["problem"] for p in check_key(bad, words_doc)]
    assert any("twice" in w for w in whys)
    assert any("quote is not printed" in w for w in whys)


def test_key_checker_refuses_another_words_file(key, words_doc):
    problems = check_key(key, words_doc, WORDS.read_bytes() + b" ")
    assert problems and "not the one the key was written from" in problems[0]["problem"]


def test_refused_key_stops_the_run(tmp_path, key, capsys):
    bad = copy.deepcopy(key)
    bad["cells"][3]["value"] = "35,005"
    p = tmp_path / "bad.key.json"
    p.write_text(json.dumps(bad))
    assert run_main(["--key", str(p), "--reading", str(READING), "--words", str(WORDS)]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "35,005" in err


# ---------------------------------------------------------------- table score


def recheck(cell, row_label, words_doc):
    return check_cell({k: cell[k] for k in ("column", "value", "box")}, words_doc, row_label,
                      ["2025", "2024", "2023"], visual_evidence=None, derived_words_present=False)


def test_score_table_on_the_real_reading(checked, key, reading):
    sc = score_table(checked, key, reading["model"])
    assert sc["model"] == "Muse Spark"
    assert sc["N"] == 69
    assert sc["counts"] == {"found": 69, "wrong_value": 0, "wrong_place": 0, "missed": 0, "extra": 0, "unscored": 0}


def test_score_table_on_the_demo_run_table(key):
    run = json.loads((REPO / "runs" / "demo" / "run.json").read_text())
    sc = score_table(run["tables"][0], key)
    assert sc["model"] == "Muse Spark" and sc["counts"]["found"] == 69 and sc["counts"]["missed"] == 0


def test_score_table_buckets(checked, key, words_doc):
    t = copy.deepcopy(checked)
    rows = t["rows"]
    rows[0]["cells"][0] = recheck({**rows[0]["cells"][0], "value": "115,316"}, rows[0]["label"], words_doc)
    rows[1]["cells"][0]["status"] = "proposed"; rows[1]["cells"][0]["checks"] = []          # place not shown
    removed = rows[2]["cells"].pop(0)                                                      # missed
    rows[3]["cells"].append(dict(rows[3]["cells"][0]))                                     # duplicate
    rows[3]["cells"].append({**rows[3]["cells"][0], "column": "2022"})                     # extra
    sc = score_table(t, key, "muse-spark-1.3")
    c = sc["counts"]
    assert c == {"found": 66, "wrong_value": 1, "wrong_place": 1, "missed": 1, "extra": 2, "unscored": 0}
    assert c["found"] + c["wrong_value"] + c["wrong_place"] + c["missed"] == 69
    assert sc["wrong_value"][0]["type"] == T.WRONG_VALUE and sc["wrong_value"][0]["expected"] == "115,315"
    assert sc["wrong_place"][0]["type"] == T.BOX_DRIFT
    assert sc["missed"][0]["expected"] == removed["value"]
    assert sorted(i["type"] for i in sc["extra"]) == [T.DUPLICATE, T.WRONG_COLUMN]


def test_unreadable_reading_misses_every_value(key):
    sc = score_table({"status": "unreadable", "rows": []}, key, "muse-spark-1.3")
    assert sc["counts"]["missed"] == 69 and sc["missed"][0]["type"] == T.UNREADABLE_OUTPUT


# ---------------------------------------------------------------- planted


def test_planted_twelve_caught(reading, words_doc, plants):
    assert len(plants) == 12
    sc = score_planted(reading, words_doc, plants, visual_evidence=None, derived_words_present=False)
    assert (sc["caught"], sc["total"]) == (12, 12)
    assert sc["by_kind"] == {"wrong_value": {"total": 4, "caught": 4}, "wrong_place": {"total": 4, "caught": 4},
                             "box_drift": {"total": 4, "caught": 4}}
    for it in sc["items"]:
        assert it["type"] == it["expected_type"], it


def test_planted_scoring_does_not_claim_hidden_text_geometry(reading, words_doc, plants):
    plant = next(p for p in plants if p["expected_type"] == "WRONG_COLUMN")
    reason = "The page's text layer has only nonvisual sliver geometry."
    sc = score_planted(reading, words_doc, [plant],
                       visual_evidence={"origin": "hidden_text", "reason": reason}, derived_words_present=False)
    item = sc["items"][0]
    assert item["status"] == "proposed" and item["caught"] is False
    assert item["type"] is None and item["types"] == [] and item["failed_checks"] == []
    assert item["type_as_planted"] is False


def test_score_reading_classifies_sliver_words_before_planted_scoring(tmp_path, key, reading, words_doc, plants):
    slivers = copy.deepcopy(words_doc)
    for word in slivers["words"]:
        middle = (word["y0"] + word["y1"]) / 2
        word["y0"], word["y1"] = middle - 0.28, middle + 0.28
    raw = json.dumps(slivers).encode()
    words_path = tmp_path / "p95.slivers.words.json"
    words_path.write_bytes(raw)
    bound_key = copy.deepcopy(key)
    bound_key["words_sha256"] = hashlib.sha256(raw).hexdigest()
    sliver_reading = copy.deepcopy(reading)
    sliver_reading["words_sha256"] = bound_key["words_sha256"]
    report = score_reading(bound_key, sliver_reading, words_path, "ions.key.json", plants)
    planted = report["planted"]
    assert planted["total"] == 12
    assert planted["caught"] == 2
    assert all(not item["caught"] for item in planted["items"] if item["expected_type"] in
               {T.WRONG_COLUMN, T.WRONG_ROW, T.BOX_DRIFT})


def test_cli_does_not_count_native_height_picture_transcription_as_geometry_support(
        tmp_path, key, reading, words_doc, plants):
    picture_words = copy.deepcopy(words_doc)
    picture_words["kind"] = "picture"
    picture_words["words_from"] = "sam+image reader"
    raw = json.dumps(picture_words).encode()
    words_path = tmp_path / "transcription.words.json"
    words_path.write_bytes(raw)

    cli_key = copy.deepcopy(key)
    cli_key["words_sha256"] = hashlib.sha256(raw).hexdigest()
    key_path = tmp_path / "key.json"
    key_path.write_text(json.dumps(cli_key))
    cli_reading = copy.deepcopy(reading)
    cli_reading["words_sha256"] = cli_key["words_sha256"]
    reading_path = tmp_path / "reading.json"
    reading_path.write_text(json.dumps(cli_reading))
    planted_path = tmp_path / "planted.json"
    planted_path.write_text(json.dumps({"plants": plants}))

    assert run_main(["--key", str(key_path), "--reading", str(reading_path), "--words", str(words_path),
                     "--planted", str(planted_path), "--out", str(tmp_path / "out")]) == 0
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    planted = report["planted"]
    geometry = {T.WRONG_COLUMN, T.WRONG_ROW, T.BOX_DRIFT}
    assert planted["total"] == 12
    assert all(not item["caught"] for item in planted["items"] if item["expected_type"] in geometry)
    assert planted["caught"] == 2


def test_planted_values_come_from_the_real_reading(reading, plants):
    cells = {(r["label"], c["column"]) for r in reading["table"]["rows"] for c in r["cells"]}
    for p in plants:
        assert (p["row_label"], p["column"]) in cells


def test_an_unchanged_plant_is_not_caught(reading, words_doc):
    sc = score_planted(reading, words_doc, [{"id": "x", "kind": "wrong_value", "row_label": "Net loss",
                                             "column": "2025", "set": {}, "expected_type": None}], visual_evidence=None, derived_words_present=False)
    assert sc["caught"] == 0 and sc["items"][0]["status"] == "matched"


# ---------------------------------------------------------------- events


@needs_ptgx
def test_ptgx_key_passes_its_check():
    run = json.loads((PTGX_RUN / "run.json").read_text())
    page = run["sources"][0]["pages"][0]
    raw = (PTGX_RUN / page["words"]).read_bytes()
    assert check_key(load_key(PTGX_KEY), json.loads(raw), raw) == []


@needs_ptgx
def test_score_events_on_ptgx_r4a():
    run = json.loads((PTGX_RUN / "run.json").read_text())
    sc = score_events(run["rows"], load_key(PTGX_KEY), run["exclusions"])
    assert sc["model"] == "Muse Spark"
    assert (sc["counts"]["found"], sc["M"]) == (2, 2)
    assert sum(sc["counts"].values()) == 2


@needs_ptgx
def test_run_cli_on_ptgx_r4a(tmp_path, capsys):
    assert run_main(["--key", str(PTGX_KEY), "--run", "ptgx-r4a", "--out", str(tmp_path)]) == 0
    assert "Muse Spark values not scored, 2/2 events" in capsys.readouterr().out
    assert json.loads((tmp_path / "report.json").read_text())["models"]["Muse Spark"]["events"]["found"] == 2


PTGX_EVENTS = {"key_version": 1, "sealed": False, "source_sha256": "0" * 64, "page_number": 3, "cells": [],
               "events": [{"ticker": "PTGX", "program": "PN-8047", "event_type": "milestone", "date_text": "Q1 2027",
                           "quote": "q"},
                          {"ticker": "PTGX", "program": "PN-458o", "event_type": "milestone",
                           "date_text": "second half of 2027", "quote": "q"}]}


def _row(i, **kw):
    base = {"id": f"e{i}", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone", "date_text": "Q1 2027",
            "status": "matched", "checks": [], "proposed_by": "Muse Spark", "page_number": 3}
    return {**base, **kw}


def test_score_events_buckets():
    rows = [
        _row(1),                                                                    # found
        _row(2),                                                                    # duplicate
        _row(3, program="PN-458o", date_text="Q4 2027"),                            # wrong date
        _row(4, program="PN-9999"),                                                 # invented
        _row(5, ticker="IONS"),                                                     # wrong company
        _row(6, status="unreadable"),                                               # unreadable
    ]
    sc = score_events(rows, PTGX_EVENTS)
    c = sc["counts"]
    assert (c["found"], c["wrong_date"], c["missed"], c["invented"]) == (1, 1, 0, 4)
    assert sc["wrong_date"][0]["type"] == T.PERIOD_MISREAD
    assert sorted(i["type"] for i in sc["invented"] if i["type"]) == sorted(
        [T.DUPLICATE, T.WRONG_COMPANY, T.UNREADABLE_OUTPUT])


def test_score_events_wrong_type_unsupported_and_missed():
    failed_quote = {"name": "quote_match", "result": "fail", "reason": "The quote is not printed anywhere on the page."}
    rows = [_row(1, event_type="trial_results"),
            _row(2, program="PN-458o", date_text="the second half of 2027", status="failed", checks=[failed_quote])]
    sc = score_events(rows, PTGX_EVENTS)
    assert sc["counts"]["wrong_type"] == 1 and sc["wrong_type"][0]["type"] == T.WRONG_EVENT_TYPE
    assert sc["counts"]["unsupported"] == 1 and sc["unsupported"][0]["type"] == T.QUOTE_NOT_ON_PAGE
    assert sc["unsupported"][0]["caught"] is True
    assert sc["counts"]["missed"] == 0
    assert score_events([], PTGX_EVENTS)["counts"]["missed"] == 2


def test_period_of_reads_the_same_period_however_it_is_written():
    assert period_of("the second half of 2027") == period_of("2H 2027") == ("2027-07-01", "2027-12-31")
    assert period_of("Q1 2027") == ("2027-01-01", "2027-03-31")
    assert period_of("March 12, 2027") == ("2027-03-12", "2027-03-12")
    assert period_of("mid-2027") == ("2027-05-01", "2027-08-31")


def test_model_family():
    assert model_family("Muse Spark (muse-spark-1.3)") == "Muse Spark"
    assert model_family("muse-spark-1.3") == "Muse Spark"
    assert model_family("muse-spark-open-1") == "Muse Spark Open"
    assert model_family(None) == "unknown model"


# ---------------------------------------------------------------- type codes


def _d(r):
    return r.to_dict()


def test_type_codes_are_the_fixed_list():
    assert T.CODES == ("WRONG_VALUE", "WRONG_ROW", "WRONG_COLUMN", "BOX_DRIFT", "QUOTE_NOT_ON_PAGE",
                       "DATE_NOT_IN_QUOTE", "PERIOD_MISREAD", "DATE_IN_PAST", "WRONG_EVENT_TYPE", "WRONG_COMPANY",
                       "SOURCE_NOT_ALLOWED", "DUPLICATE", "UNREADABLE_OUTPUT", "MISSED_DATED_SENTENCE")
    assert all(T.LABELS[c] for c in T.CODES)
    schema = json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text())
    assert tuple(schema["$defs"]["mistake"]["properties"]["type"]["enum"]) == T.CODES


def test_type_for_each_check(words, words_doc):
    w, h = words_doc["page_width"], words_doc["page_height"]
    cell_box = [407.64, 141.13, 440.22, 152.15]           # 115,315 under 2025 on "Product sales, net"
    assert T.check_type(_d(text_layer_match("115,316", words))) == T.WRONG_VALUE
    assert T.check_type(_d(selector_match("285,529", cell_box, words, w, h))) == T.WRONG_VALUE
    assert T.check_type(_d(selector_match("115,315", [407.64, 141.13, 480, 152.15], words, w, h))) == T.BOX_DRIFT
    assert T.check_type(_d(selector_match("115,315", [0, 0, 1, 1], words, w, h))) == T.BOX_DRIFT
    cols = ["2025", "2024", "2023"]
    assert T.check_type(_d(row_column_match("Product sales, net", "2024", cell_box, words, cols))) == T.WRONG_COLUMN
    assert T.check_type(_d(row_column_match("Royalty revenue", "2025", cell_box, words, cols))) == T.WRONG_ROW
    assert T.check_type(_d(row_column_match("Royalty revenue", "2025", cell_box, words, cols,
                                            reader_label="Net loss"))) == T.WRONG_ROW
    assert T.check_type(_d(quote_match("Not on this page at all.", words))) == T.QUOTE_NOT_ON_PAGE
    assert T.check_type(_d(date_match("Q4 2027", None, "Data are expected in Q3 2027."))) == T.DATE_NOT_IN_QUOTE
    assert T.check_type(_d(date_match("Q3 2027", "2027-01-01", "Data are expected in Q3 2027.", "2027-03-31",
                                      event_type="trial_results"))) == T.PERIOD_MISREAD
    assert T.check_type(_d(source_rule("third_party_calendar"))) == T.SOURCE_NOT_ALLOWED
    assert T.type_for_check("event_in_past") == T.DATE_IN_PAST
    assert T.type_for_check("text_layer_match", result="pass") is None
    assert T.type_for_exclusion("event_in_past") == T.DATE_IN_PAST
    assert T.type_for_exclusion("third_party_calendar_only") == T.SOURCE_NOT_ALLOWED
    assert T.type_for_status("unreadable") == T.UNREADABLE_OUTPUT
    assert T.type_for_field("event_type") == T.WRONG_EVENT_TYPE and T.type_for_field("ticker") == T.WRONG_COMPANY


def test_type_from_run_json_checks():
    # run.json checks carry "name" and compared {looked_for, found} strings
    rc = {"name": "row_column_match", "result": "fail", "reason": "The box sits under 2024, not 2025.",
          "compared": {"looked_for": "row Royalty revenue, column 2025", "found": "row ?, column 2024"}}
    assert T.check_type(rc) == T.WRONG_COLUMN
    rr = {"name": "row_column_match", "result": "fail", "reason": "The row label 'x' is not on this line; found 'y'.",
          "compared": {"looked_for": "row x, column 2025", "found": "row y, column 2025"}}
    assert T.check_type(rr) == T.WRONG_ROW


def test_primary_type_prefers_the_value_over_the_box(words, words_doc):
    w, h = words_doc["page_width"], words_doc["page_height"]
    box = [407.64, 141.13, 440.22, 152.15]
    checks = [_d(selector_match("115,316", box, words, w, h)), _d(text_layer_match("115,316", words))]
    assert T.primary_type(checks) == T.WRONG_VALUE
    assert T.failed_types(checks) == [T.WRONG_VALUE]


# ---------------------------------------------------------------- mistake log


def test_sample_mistakes_carry_a_type():
    run, _ = load_run(REPO / "runs" / "sample")
    assert [m["type"] for m in run["mistakes"]] == [T.PERIOD_MISREAD, T.WRONG_COMPANY, T.WRONG_EVENT_TYPE]


def test_mistake_log_on_the_sample():
    run, decisions = load_run(REPO / "runs" / "sample")
    log = mistake_log(run, decisions)
    assert log["total"] == 3
    assert {(c["type"], c["model"], c["caught_by"], c["n"]) for c in log["counts"]} == {
        (T.PERIOD_MISREAD, "Muse Spark", "code", 1),
        (T.WRONG_COMPANY, "Muse Spark", "second_reader", 1),
        (T.WRONG_EVENT_TYPE, "Muse Spark", "person", 1),
    }
    assert log["by_caught_by"] == {"code": 1, "second_reader": 1, "person": 1}


def test_mistake_log_counts_a_demonstrated_fix_by_its_failed_check():
    run, decisions = load_run(REPO / "runs" / "sample")
    e7 = next(row for row in run["rows"] if row["id"] == "e7")
    source = next(source for source in run["sources"] if source["id"] == e7["source_id"])
    page = next(page for page in source["pages"] if page["page_number"] == e7["page_number"])
    from runs.sources import load_page_evidence
    from runs.recheck import recheck_row
    words, consumed = load_page_evidence(REPO / "runs" / "sample", page)
    quote = "Samplebio Six will report final results on July 19, 2027."
    fixed = {**e7, "date": "2027-07-19", "date_text": "July 19, 2027", "quote": quote}
    before = {**fixed, "quote": e7["quote"]}
    recheck = recheck_row("t", fixed, source, words, field="quote", before=before,
                          consumed_artifact=consumed)["recheck"]
    ds = [
        {"at": "t", "target": "e7", "action": "fix", "note": None, "by": "person",
         "fix": {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}},
        {"at": "t", "target": "e7", "action": "fix", "note": None, "by": "person",
         "fix": {"field": "date_text", "from": "July 9, 2027", "to": "July 19, 2027"}},
        {"at": "t", "target": "e7", "action": "fix", "note": None, "by": "person",
         "fix": {"field": "quote", "from": e7["quote"], "to": quote}},
        {"at": "t", "target": "e7", "action": "recheck", "note": None, "fix": None,
         "by": "code", "recheck": recheck},
    ]
    st = current_state(run, ds)
    fixes = [m for m in st["mistake_list"] if m["recorded_in"] == "decisions"]
    assert len(fixes) == 1 and fixes[0]["caught_by"] == "person"
    assert fixes[0]["type"] == T.QUOTE_NOT_ON_PAGE         # the first failed check on the old value
    assert fixes[0]["model"] == "Muse Spark"
    log = mistake_log(run, ds)
    assert log["by_caught_by"]["person"] == 2 and log["total"] == 4


def test_state_mistake_counts_unchanged_by_the_listing():
    run, decisions = load_run(REPO / "runs" / "sample")
    st = current_state(run, decisions)
    assert sum(st["mistakes"].values()) == len(st["mistake_list"])


# ---------------------------------------------------------------- report


def test_report_builds_and_holds_the_numbers(checked, key, reading, words_doc, plants, tmp_path, capsys):
    rep = build_report(key, "accuracy/keys/ions_ars_2026_p95.key.json",
                       table=score_table(checked, key, reading["model"]),
                       planted=score_planted(reading, words_doc, plants, visual_evidence=None, derived_words_present=False), planted_model="Muse Spark")
    m = rep["models"]["Muse Spark"]
    assert m["values"]["found"] == 69 and m["values"]["N"] == 69
    assert (m["planted"]["caught"], m["planted"]["total"]) == (12, 12)
    text = render_text(rep)
    assert "Muse Spark 69/69 values" in text and "planted errors caught 12/12" in text
    assert "Every miss: none." in text
    assert json.loads(json.dumps(rep))["key"]["key_check"] == "passed"

    assert run_main(["--key", str(IONS_KEY), "--reading", str(READING), "--words", str(WORDS),
                     "--planted", str(PLANTED), "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Muse Spark 69/69 values, events not scored; planted errors caught 12/12" in out
    assert (tmp_path / "report.txt").read_text() == out


def test_report_lists_every_miss_with_page_and_expected(checked, key, reading, words_doc):
    t = copy.deepcopy(checked)
    t["rows"][0]["cells"][0] = recheck({**t["rows"][0]["cells"][0], "value": "115,316"}, "Product sales, net",
                                       words_doc)
    text = render_text(build_report(key, None, table=score_table(t, key, reading["model"])))
    assert "Muse Spark 68/69 values" in text
    assert "page 95, wrong value: row 'Product sales, net', column 2025: expected 115,315, got 115,316 " \
           "[WRONG_VALUE; flagged by the checks]" in text


def test_run_cli_on_the_sample_run(capsys):
    assert run_main(["--key", str(IONS_KEY), "--run", "sample"]) == 0
    out = capsys.readouterr().out
    assert "Muse Spark 69/69 values" in out
    assert "Mistake log of the run (type, model, caught by): 3" in out
    assert "WRONG_COMPANY (Wrong company), Muse Spark, caught by second_reader: 1" in out


BANNED = re.compile(r"verif|prove|correct", re.I)


def test_banned_words_absent(checked, key, reading, words_doc, plants):
    for p in sorted((REPO / "accuracy").rglob("*")):
        if p.is_file() and p.suffix in (".py", ".json"):
            assert not BANNED.search(p.read_text()), p
    text = render_text(build_report(key, None, table=score_table(checked, key, reading["model"]),
                                    planted=score_planted(reading, words_doc, plants, visual_evidence=None, derived_words_present=False)))
    assert not BANNED.search(text)
    assert not BANNED.search(" ".join(T.LABELS.values()))


# ---------------------------------------------------------------- Job 3b: scorer semantics


def _partial(key, n=10, **over):
    return {**key, "complete": False, "cells": key["cells"][:n], **over}


def test_a_key_must_say_whether_it_is_complete(key, words_doc):
    silent = {k: v for k, v in key.items() if k != "complete"}
    problems = check_key(silent, words_doc)
    assert [p["problem"] for p in problems] == ["The key must say whether it is complete: complete true or false."]
    assert check_key({**key, "complete": "yes"}, words_doc)
    assert check_key({**key, "complete": False}, words_doc) == []
    assert load_key(IONS_KEY)["complete"] is True and load_key(PTGX_KEY)["complete"] is True


def test_on_a_partial_key_what_is_proposed_outside_it_is_unscored_not_a_mistake(checked, key, reading):
    """ENTA-style: 10 of a 69-cell table in the key, all 10 right. The other 59 are not mistakes."""
    partial = _partial(key)
    sc = score_table(checked, partial, reading["model"])
    assert sc["N"] == 10 and sc["complete"] is False
    assert sc["counts"] == {"found": 10, "wrong_value": 0, "wrong_place": 0, "missed": 0, "extra": 0, "unscored": 59}
    assert all(i["type"] is None for i in sc["unscored"])
    rep = build_report(partial, None, table=sc)
    assert rep["models"]["Muse Spark"]["mistakes_by_type"] == {} and rep["mistake_log"]["from_scoring"] == []
    assert rep["misses"] == [] and "10/10 values" in render_text(rep)
    assert "unscored 59" in render_text(rep) and "partial" in render_text(rep)
    # the same table against a complete key of the same ten cells: 59 extras, each typed
    complete = {**partial, "complete": True}
    sc2 = score_table(checked, complete, reading["model"])
    assert sc2["counts"]["extra"] == 59 and sc2["counts"]["unscored"] == 0


def test_a_partial_key_still_counts_what_it_does_hold(checked, key, reading, words_doc):
    t = copy.deepcopy(checked)
    t["rows"][0]["cells"][0] = recheck({**t["rows"][0]["cells"][0], "value": "115,316"}, "Product sales, net", words_doc)
    sc = score_table(t, _partial(key), reading["model"])
    assert sc["counts"]["wrong_value"] == 1 and sc["counts"]["found"] == 9 and sc["counts"]["unscored"] == 59
    assert sum(sc["counts"][b] for b in ("found", "wrong_value", "wrong_place", "missed")) == sc["N"]
    dup = copy.deepcopy(checked)
    dup["rows"][1]["cells"].append(dict(dup["rows"][1]["cells"][0]))
    assert score_table(dup, _partial(key), reading["model"])["counts"]["extra"] == 1, "a repeat of a key cell stays an extra"


def test_a_key_value_never_proposed_is_missed_on_a_partial_key_too(checked, key, reading):
    t = copy.deepcopy(checked)
    t["rows"][0]["cells"].pop(0)
    assert score_table(t, _partial(key), reading["model"])["counts"]["missed"] == 1


def test_partial_key_events_outside_it_are_unscored(words_doc):
    partial = {**PTGX_EVENTS, "complete": False}
    rows = [_row(1), _row(2, program="PN-9999"), _row(3, ticker="IONS"), _row(4, status="unreadable"),
            _row(5, program="PN-458o", event_type="launch")]
    sc = score_events(rows, partial)
    c = sc["counts"]
    assert (c["found"], c["unscored"]) == (1, 1)
    assert sorted(i["type"] for i in sc["invented"]) == [T.UNREADABLE_OUTPUT, T.WRONG_COMPANY]
    assert [i["program"] for i in sc["unscored"]] == ["PN-9999"] and sc["unscored"][0]["type"] is None
    assert sc["wrong_type"][0]["type"] == T.WRONG_EVENT_TYPE
    complete = score_events(rows, {**PTGX_EVENTS, "complete": True})
    assert complete["counts"]["invented"] == 3 and complete["counts"]["unscored"] == 0
    assert build_report(partial, None, events=sc)["models"]["Muse Spark"]["mistakes_by_type"].get("NO_TYPE") is None


@pytest.mark.parametrize("name", ["FY2025", "December 31, 2025", "Dec. 31, 2025", "fiscal 2025"])
def test_a_column_the_reader_names_as_a_dated_year_is_the_key_column(key, reading, words_doc, name):
    r = copy.deepcopy(reading)
    r["table"]["columns"] = [name if c == "2025" else c for c in r["table"]["columns"]]
    for row in r["table"]["rows"]:
        for c in row["cells"]:
            if c["column"] == "2025":
                c["column"] = name
    sc = score_table(check_reading(r, words_doc, visual_evidence=None, derived_words_present=False), key, reading["model"])
    assert sc["counts"] == {"found": 69, "wrong_value": 0, "wrong_place": 0, "missed": 0, "extra": 0, "unscored": 0}


def test_a_key_may_name_the_column_as_a_dated_year_but_not_twice(key, words_doc):
    named = {**key, "cells": [{**c, "column": "December 31, 2025"} if c["column"] == "2025" else c
                              for c in key["cells"]]}
    assert check_key(named, words_doc) == []
    twice = {**key, "cells": key["cells"] + [{**key["cells"][0], "column": "FY2025"}]}
    assert [p["problem"] for p in check_key(twice, words_doc)] == ["The key lists this row and column twice."]


ALIASED = {**PTGX_EVENTS, "complete": True,
           "events": [{**PTGX_EVENTS["events"][0], "program": "PN-8047", "aliases": ["Hepcidin mimetic", "PN8047"]},
                      PTGX_EVENTS["events"][1]]}


def test_a_program_alias_is_matched_by_the_scorer():
    rows = [_row(1, program="Hepcidin mimetic"), _row(2, program="pn8047", date_text="Q2 2027"),
            _row(3, program=None, title="PN8047 Phase 1", quote="Phase 1 of PN8047 is expected in Q1 2027.")]
    sc = score_events(rows, ALIASED)
    assert [i["id"] for i in sc["found"]] == ["e1"]
    assert sc["counts"]["found"] == 1 and sc["counts"]["invented"] == 2, "e2 and e3 repeat the same event"
    assert [i["id"] for i in sc["wrong_date"]] == [] and {i["type"] for i in sc["invented"]} == {T.DUPLICATE}
    only_alias = score_events([_row(1, program="Hepcidin mimetic", date_text="Q4 2027")], ALIASED)
    assert only_alias["counts"]["wrong_date"] == 1 and only_alias["counts"]["invented"] == 0
    named_in_quote = score_events([_row(1, program=None, title="x", quote="PN8047 results in Q1 2027")], ALIASED)
    assert named_in_quote["counts"]["found"] == 1


def test_key_aliases_are_checked(words_doc):
    key = load_key(PTGX_KEY)
    words = json.loads((FIXTURES / "ptgx_8k_2026-08-05_ex991_p3.words.json").read_text())
    assert check_key(key, words) == []
    ok = {**key, "events": [{**key["events"][0], "aliases": ["Hepcidin mimetic"]}, key["events"][1]]}
    assert check_key(ok, words) == []
    bad_type = {**key, "events": [{**key["events"][0], "aliases": "x"}, key["events"][1]]}
    assert [p["problem"] for p in check_key(bad_type, words)] == ["The aliases must be a list of names."]
    shared = {**key, "events": [{**key["events"][0], "aliases": ["pn-458o"]}, key["events"][1]]}
    assert check_key(shared, words) == [], "the same alias may stand on several events"
    bad_ind = {**key, "events": [{**key["events"][0], "indication_aliases": [""]}, key["events"][1]]}
    assert [p["problem"] for p in check_key(bad_ind, words)] == ["The indication aliases must be a list of names."]


def test_the_report_lists_right_value_check_failed_apart_with_the_checks_reason(checked, key, reading, words_doc):
    t = copy.deepcopy(checked)
    cell = t["rows"][0]["cells"][0]
    drifted = dict(cell, box=list(t["rows"][0]["cells"][1]["box"]))
    t["rows"][0]["cells"][0] = recheck(drifted, "Product sales, net", words_doc)
    sc = score_table(t, key, reading["model"])
    assert sc["counts"]["wrong_place"] == 1
    reason = sc["wrong_place"][0]["stopped_by"]["reason"]
    assert reason and sc["wrong_place"][0]["stopped_by"]["result"] == "fail"
    rep = build_report(key, None, table=sc)
    [item] = rep["right_but_stopped"]
    assert (item["row_label"], item["column"], item["value"]) == ("Product sales, net", "2025", "115,315")
    assert item["stopped_by"]["reason"] == reason
    text = render_text(rep)
    assert "Right value, check failed (1)" in text and reason in text
    assert text.count("Product sales, net") == 1, "listed there, not again under the misses"
    assert rep["misses"], "the JSON keeps it among the misses"


def test_an_event_a_check_stopped_is_right_but_stopped_with_its_reason():
    failed = {"name": "quote_match", "result": "fail", "reason": "The quote is not printed on the page."}
    sc = score_events([_row(1, status="failed", checks=[failed])], PTGX_EVENTS)
    rep = build_report(PTGX_EVENTS, None, events=sc)
    [item] = rep["right_but_stopped"]
    assert item["event"]["program"] == "PN-8047" and item["stopped_by"]["reason"] == failed["reason"]
    assert "The quote is not printed on the page." in render_text(rep)


def test_a_check_that_could_not_run_gives_its_reason_too():
    cnr = {"name": "selector_match", "result": "could_not_run", "reason": "The reader gave no box."}
    sc = score_events([_row(1, status="proposed", checks=[cnr])], PTGX_EVENTS)
    assert sc["unsupported"][0]["stopped_by"] == {"check": "selector_match", "result": "could_not_run",
                                                  "reason": "The reader gave no box."}
