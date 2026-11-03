"""tools/bench_score.py: the offline benchmark scorer (no model, network or paid call).

Fixtures: tests/fixtures/bench/ptgx.json (known PTGX catalysts, public facts) and the synthetic run folder
tests/fixtures/bench/run-ptgx-synthetic/ (made by make_run_fixture.py; rows e1..e9 cover found, misplaced,
missed, duplicate and unmatched cases; see its docstring). The run is loaded through the real loaders
(runs.check_load, runs.state.current_state, runs.calendar.run_interval_partition), so the scorer sees what the
UI shows: e1 is saved with no date fields and gets its January 2027 period from the date check on load.
"""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from tools import bench_score as bs

BENCH_DIR = Path(__file__).parent / "fixtures" / "bench"
RUN_DIR = BENCH_DIR / "run-ptgx-synthetic"
PTGX = BENCH_DIR / "ptgx.json"


@pytest.fixture(scope="module")
def result() -> dict:
    return bs.score(RUN_DIR, [PTGX])


@pytest.fixture(scope="module")
def ptgx(result) -> dict:
    (t,) = result["tickers"]
    return t


def item(t: dict, item_id: str) -> dict:
    return next(r for r in t["items"] if r["id"] == item_id)


# ------------------------------------------------------------------------------------------------- found

def test_found_rows_match_keywords_and_timing(ptgx):
    r = item(ptgx, "pn881-ph2b-psoriasis")
    assert r["category"] == "found"
    assert r["best"]["row_id"] == "e1"
    assert r["best"]["matched_words"] == ["PN-881", "Phase 2b"]
    assert r["best"]["row_placement"] == r["best"]["expected_placement"] == "upcoming"
    # the loader derived the period (January 2027) from the date check; the row itself saved no date fields
    assert "row 2027-01-01..2027-01-31" in r["best"]["timing"] and "overlap" in r["best"]["timing"]
    assert item(ptgx, "pn8047-ph1")["best"]["row_id"] == "e2"
    assert item(ptgx, "jnj-enterprise-business-review")["category"] == "found"


def test_found_never_claims_correctness(ptgx, result):
    assert "keyword+timing" in result["disclaimer"].lower()
    for r in ptgx["items"]:
        if r["category"] == "found":
            assert "keyword+timing match" in r["why"]
            assert "correct" not in r["why"]


# --------------------------------------------------------------------------------------------- misplaced

def test_misplaced_when_the_calendar_places_it_as_context(ptgx):
    r = item(ptgx, "takeda-capital-markets-day")
    assert r["category"] == "misplaced"
    assert r["best"]["row_id"] == "e3"
    assert r["best"]["timing_compatible"] is True
    assert r["best"]["row_placement"] == "context" and r["best"]["expected_placement"] == "upcoming"
    assert "placed as context, expected upcoming" in r["why"]


def test_misplaced_when_the_period_does_not_overlap(ptgx):
    r = item(ptgx, "pn477o-ph1")
    assert r["category"] == "misplaced"
    assert r["best"]["row_id"] == "e4"
    assert r["best"]["timing_compatible"] is False
    assert "expected 2027-01-01..2027-06-30 (1H 2027); row 2027-07-01..2027-12-31" in r["best"]["timing"]
    assert "no overlap" in r["best"]["timing"]
    assert "timing does not overlap" in r["why"]


# ------------------------------------------------------------------------------------------------ missed

def test_missed_items_have_no_candidates(ptgx):
    missed = {r["id"] for r in ptgx["items"] if r["category"] == "missed"}
    assert {"conf-wolfe-2026-11-10", "conf-truist-2026-11-12", "conf-ubs-2026-11-16", "conf-jefferies-2026-11-17",
            "iconic-psa2-primary-completion", "jnj-psa-data-2026", "icotrokinra-ole-completion",
            "australia-tga-review", "acr-psa-late-breaking", "takeda-275m-payments"} == missed
    for r in ptgx["items"]:
        if r["category"] == "missed":
            assert r["candidates"] == [] and r["best"] is None


# -------------------------------------------------------------------------------------------- duplicates

def test_duplicates_by_benchmark_item_and_by_program_action_period(ptgx):
    by_rule = {(d["rule"], tuple(d["rows"])): d for d in ptgx["duplicates"]}
    same_item = by_rule[("same benchmark item", ("e5", "e6"))]
    assert same_item["item"] == "jnj-enterprise-business-review"
    assert set(same_item["detail"]) == {"e5", "e6"}
    coincide = by_rule[("same program+action+period", ("e7", "e8"))]
    assert coincide["detail"] == {"program": "rusfertide", "action": "commercial launch",
                                  "period": "2026-10-01..2026-12-31"}
    assert len(ptgx["duplicates"]) == 2


# --------------------------------------------------------------------------------------------- unmatched

def test_unmatched_run_rows_are_listed(ptgx):
    assert [u["row_id"] for u in ptgx["unmatched"]] == ["e7", "e8", "e9"]
    e9 = next(u for u in ptgx["unmatched"] if u["row_id"] == "e9")
    assert e9["title"] == "Third quarter earnings call" and e9["placement"] == "upcoming"


# ------------------------------------------------------------------------------------------------ counts

def test_counts_per_ticker_and_total(result, ptgx):
    assert ptgx["counts"] == {"benchmark_items": 15, "run_rows": 9, "found": 3, "misplaced": 2, "missed": 10,
                              "duplicate_groups": 2, "unmatched_rows": 3}
    assert result["totals"] == ptgx["counts"]
    assert result["run"]["window"] == {"start": "2026-11-03", "end": "2027-05-03"}
    assert result["rows_without_benchmark"] == []


def test_totals_add_up_over_several_benchmark_files(tmp_path):
    other = tmp_path / "roiv.json"
    other.write_text(json.dumps({"ticker": "ROIV", "items": [
        {"id": "x", "description": "nothing in this run", "keywords": ["brepocitinib"], "timing": "Q1 2027"}]}))
    res = bs.score(RUN_DIR, [PTGX, other])
    assert [t["ticker"] for t in res["tickers"]] == ["PTGX", "ROIV"]
    roiv = res["tickers"][1]["counts"]
    assert roiv == {"benchmark_items": 1, "run_rows": 0, "found": 0, "misplaced": 0, "missed": 1,
                    "duplicate_groups": 0, "unmatched_rows": 0}
    assert res["totals"]["benchmark_items"] == 16 and res["totals"]["missed"] == 11


# ---------------------------------------------------------------------------------- never writes the run

def test_scorer_never_mutates_the_run_folder(tmp_path):
    work = tmp_path / "run"
    shutil.copytree(RUN_DIR, work)
    before = bs.folder_digest(work)
    out = tmp_path / "out" / "score.json"
    assert bs.main([str(work), str(PTGX), "--json", str(out)]) == 0
    bs.score(work, [PTGX])
    assert bs.folder_digest(work) == before          # same files, same bytes
    assert out.is_file() and json.loads(out.read_text())["totals"]["found"] == 3
    assert not list(work.glob("*.json.bak")) and sorted(p.name for p in work.iterdir()) == sorted(
        p.name for p in RUN_DIR.iterdir())


# --------------------------------------------------------------------------------- CLI and rendering

def test_cli_prints_table_and_writes_json(tmp_path, capsys):
    out = tmp_path / "score.json"
    assert bs.main([str(RUN_DIR), str(PTGX), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Keyword+timing match only" in text
    assert "pn881-ph2b-psoriasis" in text and "FOUND" in text and "MISPLACED" in text and "MISSED" in text
    assert "Possible duplicates:" in text and "e5, e6: same benchmark item" in text
    assert "Unmatched run rows" in text
    assert "PTGX: found 3, misplaced 2, missed 10 of 15 benchmark items" in text
    assert "TOTAL: found 3, misplaced 2, missed 10 of 15" in text
    doc = json.loads(out.read_text())
    assert doc["disclaimer"] == bs.DISCLAIMER and doc["tickers"][0]["ticker"] == "PTGX"


def test_every_match_is_explainable(ptgx):
    for r in ptgx["items"]:
        for c in r["candidates"]:
            assert c["matched_words"] and c["searched"]
            assert all(bs.phrase_in(w, c["searched"]) for w in c["matched_words"])
            assert c["timing"].startswith("expected ")


# ---------------------------------------------------------------------------- deterministic matching rules

def test_normalize_aliases():
    assert bs.normalize("PN-881") == bs.normalize("PN881") == "pn881"
    assert bs.normalize("J&J") == "j and j" and bs.phrase_in("J&J", "The J&J business review")
    assert bs.phrase_in("Phase 2b", "PN-881 Phase 2b psoriasis") and not bs.phrase_in("Phase 2", "Phase 2b study")
    assert bs.phrase_in("PN-477o", "PN-477o Phase 1 initiation") and not bs.phrase_in("PN-477", "PN-477o Phase 1")


@pytest.mark.parametrize("spec, start, end", [
    ("Q1 2027", date(2027, 1, 1), date(2027, 3, 31)),
    ("1H 2027", date(2027, 1, 1), date(2027, 6, 30)),
    ("H2 2026", date(2026, 7, 1), date(2026, 12, 31)),
    ("2026-11-10", date(2026, 11, 10), date(2026, 11, 10)),
    ("December 8, 2026", date(2026, 12, 8), date(2026, 12, 8)),
    ("Feb 2027", date(2027, 2, 1), date(2027, 2, 28)),
    ("2027", date(2027, 1, 1), date(2027, 12, 31)),
    ({"start": "2026-11-03", "end": "2026-12-31", "text": "later this year"}, date(2026, 11, 3), date(2026, 12, 31)),
])
def test_parse_timing(spec, start, end):
    t = bs.parse_timing(spec)
    assert (t["kind"], t["start"], t["end"]) == ("dated", start, end)


def test_parse_timing_undated_approx_and_errors():
    assert bs.parse_timing("undated")["kind"] == "undated"
    approx = bs.parse_timing("~2027-02-10")
    assert approx["slack_days"] == 14 and approx["text"] == "~2027-02-10"
    with pytest.raises(ValueError):
        bs.parse_timing("sometime soon")


def test_compare_timing_overlap_and_slack():
    q1 = bs.parse_timing("Q1 2027")
    jan = {"kind": "dated", "start": date(2027, 1, 1), "end": date(2027, 1, 31), "text": "January 2027"}
    assert bs.compare_timing(q1, jan)["compatible"]
    h2 = {"kind": "dated", "start": date(2027, 7, 1), "end": date(2027, 12, 31), "text": "H2 2027"}
    assert not bs.compare_timing(q1, h2)["compatible"]
    feb10 = bs.parse_timing("~2027-02-10")
    feb20 = {"kind": "dated", "start": date(2027, 2, 20), "end": date(2027, 2, 20), "text": "Feb 20"}
    assert bs.compare_timing(feb10, feb20)["compatible"] and "slack 14d" in bs.compare_timing(feb10, feb20)["explanation"]
    undated_row = {"kind": "undated", "start": None, "end": None, "text": "undated"}
    assert not bs.compare_timing(q1, undated_row)["compatible"]
    assert bs.compare_timing(bs.parse_timing("undated"), feb20)["compatible"]


def test_bench_file_validation(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"ticker": "X", "items": [{"id": "a", "keywords": ["k"], "placement": "somewhere"}]}))
    with pytest.raises(ValueError, match="placement"):
        bs.load_bench(bad)
    bad.write_text(json.dumps({"ticker": "X", "items": [{"id": "a", "keywords": [["k1"], ["k2"]], "min_groups": 3}]}))
    with pytest.raises(ValueError, match="min_groups"):
        bs.load_bench(bad)
    bad.write_text(json.dumps({"ticker": "X", "items": [{"id": "a", "keywords": ["k"]}, {"id": "a", "keywords": ["k"]}]}))
    with pytest.raises(ValueError, match="unique id"):
        bs.load_bench(bad)
    ok = bs.load_bench(PTGX)
    assert ok["ticker"] == "PTGX" and len(ok["items"]) == 15
    assert len(set(item_ids(ok))) == 15                        # order is the file's order; ids are unique


def item_ids(bench: dict) -> list[str]:
    return [i["id"] for i in bench["items"]]


def test_placement_class_words():
    assert bs.placement_class({"inclusion": "included"}, "calendar") == "upcoming"
    assert bs.placement_class({"inclusion": "context"}, "past") == "context"
    assert bs.placement_class({"inclusion": "kept"}, "past") == "context"
    assert bs.placement_class({"inclusion": "kept"}, "calendar") == "upcoming"
    assert bs.placement_class({"inclusion": "review"}, None) == "review"
    assert bs.placement_class({"inclusion": "excluded"}, None) == "excluded"
    assert bs.placement_class({"inclusion": "undated", "placement": "timing_unresolved"}, "timing_unresolved") == "undated"
