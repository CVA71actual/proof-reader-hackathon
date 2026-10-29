"""R6-lite: cost per step and per kept row, earnings event words, early / mid / late quarters."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from capture import read_events
from checks import event_checks
from checks.event_checks import date_match, event_anchors, parse_period
from runs import build_entry, build_sample, prices
from runs.state import current_state

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text())


def load(name):
    return json.loads((REPO / "runs" / name / "run.json").read_text())


def model_steps(run):
    return [s for r in run["rows"] for s in r["trail"] if s["tag"] == "Model"]


# ---------------------------------------------------------------- 1. cost on the record


def test_price_table_is_used(monkeypatch):
    assert prices.cost_usd("Muse Spark", 1_000_000, 0) == prices.PRICES_PER_1M_USD["Muse Spark"]["input"]
    assert prices.cost_usd("Muse Spark", 0, 1_000_000) == prices.PRICES_PER_1M_USD["Muse Spark"]["output"]
    monkeypatch.setitem(prices.PRICES_PER_1M_USD, "Muse Spark", {"input": 10.0, "output": 100.0})
    assert prices.cost_usd("Muse Spark", 2000, 300) == pytest.approx(0.02 + 0.03)
    assert prices.model_cost_fields("Muse Spark", 2000, 300)["cost_usd"] == pytest.approx(0.05)


def test_the_confirmed_prices_are_the_ones_dev_meta_ai_lists_and_only_spark_open_is_a_placeholder():
    t = prices.PRICES_PER_1M_USD
    assert (t["Muse Spark"]["input"], t["Muse Spark"]["output"], t["Muse Spark"]["cached_input"]) == (1.25, 4.25, 0.15)
    assert (t["Muse Wonder"]["input"], t["Muse Wonder"]["output"], t["Muse Wonder"]["cached_input"]) == (4.00, 20.00, 0.20)
    assert prices.PRICES_PER_IMAGE_USD["SAM 3.1"] == 0.0025 and prices.image_cost_usd("SAM 3.1", 1000) == 2.5
    assert prices.cost_usd("Muse Spark", 1_000_000, 1_000_000) == 5.5
    assert prices.cost_usd("Muse Wonder", 1_000_000, 1_000_000) == 24.0
    status = prices.PRICE_STATUS
    assert set(status) == set(t) | set(prices.PRICES_PER_IMAGE_USD)
    assert [m for m, s in status.items() if s.startswith("placeholder")] == ["Muse Spark Open"]
    assert all(s.startswith("confirmed 2026-10-29") for m, s in status.items() if m != "Muse Spark Open")
    src = (REPO / "runs" / "prices.py").read_text()
    assert "PRICE LIST" not in src and "price list to confirm against dev.meta.ai before submission" not in src
    assert "PLACEHOLDER (Baseten)" in src and src.count("PLACEHOLDER") >= 2


def test_unknown_model_or_missing_tokens_cost_nothing_recorded():
    assert prices.cost_usd("Some Other Model", 100, 100) is None
    assert prices.model_cost_fields("Muse Spark", None, 10) == {"tokens": None, "cost_usd": None}
    assert prices.run_spend_usd([]) is None
    rows = [{"trail": [{"tag": "Model", "cost_usd": 0.5}, {"tag": "Model", "cost_usd": None}]}]
    assert prices.run_spend_usd(rows) is None   # one unrecorded step: the sum would understate the spend


def test_split_tokens_adds_up():
    assert prices.split_tokens(10586, 2) == [5293, 5293]
    assert prices.split_tokens(7, 3) == [3, 2, 2]
    assert sum(prices.split_tokens(3518, 3)) == 3518
    assert prices.split_tokens(None, 2) == [None, None]


@pytest.mark.parametrize("name", ["sample", "ptgx-r4a"])
def test_every_model_step_carries_tokens_and_cost(name):
    run = load(name)
    steps = model_steps(run)
    assert steps
    for s in steps:
        assert isinstance(s["tokens"]["prompt"], int) and isinstance(s["tokens"]["completion"], int)
        assert s["cost_usd"] == prices.cost_usd(s["who"], s["tokens"]["prompt"], s["tokens"]["completion"])
    assert run["run"]["spend_usd"] == pytest.approx(sum(s["cost_usd"] for s in steps))
    for r in run["rows"]:
        first = r["trail"][0]
        assert first["step"] == "Instructions sent"
        assert isinstance(first["tokens"]["prompt"], int) and first["tokens"]["completion"] is None
        assert "cost_usd" not in first


def test_ptgx_cost_comes_from_the_saved_usage():
    reading = json.loads(read_events.READING_PATH.read_text())
    usage = reading["usage"]
    run = load("ptgx-r4a")
    steps = model_steps(run)
    assert sum(s["tokens"]["prompt"] for s in steps) == usage["prompt_tokens"]
    assert sum(s["tokens"]["completion"] for s in steps) == usage["completion_tokens"]
    assert all(s["tokens"]["call_rows"] == len(run["rows"]) for s in steps)
    assert all(r["trail"][0]["tokens"]["prompt"] == usage["prompt_tokens"] for r in run["rows"])
    whole = prices.cost_usd("Muse Spark", usage["prompt_tokens"], usage["completion_tokens"])
    assert run["run"]["spend_usd"] == pytest.approx(whole, abs=1e-6)


def test_fake_reading_records_no_cost(tmp_path):
    fake = read_events.read_fake(read_events.load_words(), build_entry.load_entry(build_entry.PIN))
    run = build_entry.build_entry_run(fake, tmp_path / "practice")
    assert run["run"]["spend_usd"] is None
    assert not model_steps(run)
    assert all(r["trail"][0]["tokens"] is None for r in run["rows"])


def test_reading_without_usage_records_no_cost(tmp_path):
    reading = json.loads(read_events.READING_PATH.read_text())
    reading.pop("usage")
    run = build_entry.build_entry_run(reading, tmp_path / "ptgx-r4a")
    assert run["run"]["spend_usd"] is None
    assert all(s["tokens"] is None and s["cost_usd"] is None for s in model_steps(run))
    assert current_state(run, [])["cost_per_kept_row_usd"] is None


def test_sample_numbers_are_the_fixed_sample_numbers():
    run = load("sample")
    assert run["run"]["sample"] is True and all(r["sample"] for r in run["rows"])
    for s in model_steps(run):
        assert (s["tokens"]["prompt"], s["tokens"]["completion"]) == build_sample.SAMPLE_TOKENS[s["step"]]


def test_demo_has_no_recorded_spend():
    run = load("demo")
    assert run["run"]["spend_usd"] is None
    assert current_state(run, [])["cost_per_kept_row_usd"] is None


def test_cost_per_kept_row_follows_the_decisions():
    run = load("sample")
    decisions = [json.loads(l) for l in (REPO / "runs" / "sample" / "decisions.jsonl").read_text().splitlines()]
    st = current_state(run, decisions)
    kept = st["counts"]["rows"]["kept"] + st["counts"]["rows"]["corrected"]
    assert kept > 0
    assert st["cost_per_kept_row_usd"] == pytest.approx(run["run"]["spend_usd"] / kept, abs=1e-6)
    # removing a kept row spreads the same spend over one row fewer
    kept_id = next(t for t, s in st["targets"].items() if s["kind"] == "row" and s["group"] == "kept")
    more = decisions + [{"at": "2026-10-27T17:00:00+00:00", "target": kept_id, "action": "remove",
                         "note": "x", "fix": None, "by": "person"}]
    assert current_state(run, more)["cost_per_kept_row_usd"] == pytest.approx(run["run"]["spend_usd"] / (kept - 1),
                                                                              abs=1e-6)


def test_cost_per_kept_row_is_null_with_no_kept_row():
    run = copy.deepcopy(load("sample"))
    for r in run["rows"]:
        r["group"] = "open"
    assert current_state(run, [])["cost_per_kept_row_usd"] is None


def test_schema_takes_and_bounds_the_cost_fields():
    v = Draft202012Validator(SCHEMA)
    run = copy.deepcopy(load("sample"))
    assert not list(v.iter_errors(run))
    step = next(s for s in run["rows"][0]["trail"] if s["tag"] == "Model")
    step["cost_usd"] = -1
    assert list(v.iter_errors(run))
    step["cost_usd"] = 0.1
    step["tokens"] = {"prompt": 10}
    assert list(v.iter_errors(run))
    step["tokens"] = {"prompt": 10, "completion": 2, "dollars": 3}
    assert list(v.iter_errors(run))


# ---------------------------------------------------------------- 2. earnings event words


def test_earnings_event_words():
    assert event_anchors("earnings")[:6] == ["results", "earnings", "revenue", "net loss", "quarter ended",
                                             "year ended"]
    assert event_anchors("other") == []
    assert event_anchors("other", title="Investor day", program=None) == ["investor"]


def test_sample_e10_date_check_now_runs_and_passes():
    e10 = next(r for r in load("sample")["rows"] if r["id"] == "e10")
    date = next(c for c in e10["checks"] if c["name"] == event_checks.DATE_ID)
    assert date["result"] == "pass"
    assert date["reason"] == "The day in the row is the day printed in the quote."
    assert e10["status"] == "proposed"   # still proposed: no box, so the citation check could not run


@pytest.mark.parametrize("quote, date_text, iso, result", [
    ("Net loss for the quarter ended June 30, 2026 was $12 million.", "June 30, 2026", "2026-06-30", "pass"),
    ("Revenue for the year ended December 31, 2025 grew.", "December 31, 2025", "2025-12-31", "pass"),
    ("Revenue for the year ended December 31, 2025 grew.", "December 31, 2025", "2025-12-30", "fail"),
    ("The company will hold a call on August 5, 2026.", "August 5, 2026", "2026-08-05", "could_not_run"),
])
def test_earnings_rows_use_the_earnings_words(quote, date_text, iso, result):
    r = date_match(date_text, iso, quote, event_type="earnings", title="Q2")
    assert r.result == result, r.reason


# ---------------------------------------------------------------- 3. early / mid / late quarters

PN881 = ("PN-881 (oral IL-17 antagonist peptide): Pharmacokinetic data from the Phase 1 study supports the decision "
         "to advance PN-881 into a comprehensive Phase 2 psoriasis program, with initiation expected in early Q1 2027.")


@pytest.mark.parametrize("text, first, last", [
    ("early Q1 2027", "2027-01-01", "2027-01-31"),
    ("mid-Q2 2027", "2027-05-01", "2027-05-31"),
    ("mid Q2 2027", "2027-05-01", "2027-05-31"),
    ("late Q4 2027", "2027-12-01", "2027-12-31"),
    ("early first quarter of 2027", "2027-01-01", "2027-01-31"),
    ("late 3Q 2027", "2027-09-01", "2027-09-30"),
])
def test_quarter_part_is_one_month(text, first, last):
    a, b = parse_period(text)
    assert (a.isoformat(), b.isoformat()) == (first, last)


def test_pn881_early_q1_2027_passes_with_the_month():
    r = date_match("early Q1 2027", "2027-01-01", PN881, "2027-01-31", event_type="milestone",
                   title="Phase 2 initiation", program="PN-881")
    assert r.result == "pass", r.reason
    r = date_match("early Q1 2027", None, PN881, None, event_type="milestone", title="Phase 2 initiation",
                   program="PN-881")
    assert r.result == "pass", r.reason


@pytest.mark.parametrize("iso, end", [("2027-01-01", "2027-03-31"), ("2027-02-01", "2027-02-28"),
                                      ("2027-01-01", None)])
def test_pn881_wrong_bounds_fail(iso, end):
    r = date_match("early Q1 2027", iso, PN881, end, event_type="milestone", title="Phase 2 initiation",
                   program="PN-881")
    assert r.result == "fail", r.reason


def test_whole_quarter_claim_on_early_q1_fails():
    r = date_match("Q1 2027", "2027-01-01", PN881, "2027-03-31", event_type="milestone",
                   title="Phase 2 initiation", program="PN-881")
    assert r.result == "fail"
    assert r.reason == "The quote names early Q1 2027, the row claims Q1 2027."


@pytest.mark.parametrize("quote", [
    "Initiation is no longer expected in late Q4 2027.",
    "Initiation was previously expected in mid-Q2 2027.",
])
def test_quarter_part_not_affirmed_fails(quote):
    text = "late Q4 2027" if "late" in quote else "mid-Q2 2027"
    a, b = parse_period(text)
    r = date_match(text, a.isoformat(), quote, b.isoformat(), event_type="milestone", title="Initiation")
    assert r.result == "fail", r.reason


def test_quarter_part_with_no_year_fails():
    r = date_match("early Q1", None, "Initiation expected in early Q1.", None, event_type="milestone")
    assert r.result == "fail"
    assert "names no year" in r.reason
