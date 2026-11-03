"""tools/bench_score.py: the offline benchmark scorer (no model, network or paid call).

Fixtures: tests/fixtures/bench/ptgx.json (known PTGX catalysts, public facts) and the synthetic run folder
tests/fixtures/bench/run-ptgx-synthetic/ (made by make_run_fixture.py; rows e1..e16 cover found, misplaced,
missed, duplicate, distinct-event and unmatched cases; see its docstring). The run is loaded through the real loaders
(runs.check_load, runs.state.current_state, runs.calendar.run_interval_partition), so the scorer sees what the
UI shows: e1 is saved with no date fields and gets its January 2027 period from the date check on load.
"""
from __future__ import annotations

import json
import os
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


def bench_file(tmp_path: Path, items: list[dict], ticker: str = "PTGX", name: str = "bench.json") -> Path:
    f = tmp_path / name
    f.write_text(json.dumps({"ticker": ticker, "items": items}))
    return f


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
    assert {"iconic-psa2-primary-completion", "jnj-psa-data-2026", "icotrokinra-ole-completion",
            "australia-tga-review", "acr-psa-late-breaking", "takeda-275m-payments"} == missed
    for r in ptgx["items"]:
        if r["category"] == "missed":
            assert r["candidates"] == [] and r["best"] is None


# -------------------------------------------------------------------------------------------- duplicates

def test_duplicates_need_program_action_and_period_identity(ptgx):
    by_rows = {tuple(d["rows"]): d for d in ptgx["duplicates"]}
    assert all(d["rule"] == "same program+action+period" for d in ptgx["duplicates"])
    coincide = by_rows[("e7", "e8")]
    assert coincide["detail"] == {"program": "rusfertide", "action": "commercial launch",
                                  "period": "2026-10-01..2026-12-31"}
    # two rows for the J&J business review match the same benchmark item (both are listed as its candidates) but
    # carry no program: shared words are not identity, so they are not called duplicates
    assert [c["row_id"] for c in item(ptgx, "jnj-enterprise-business-review")["candidates"]] == ["e5", "e6"]
    assert not any({"e5", "e6"} <= set(d["rows"]) for d in ptgx["duplicates"])


# ---------------------------------------------------------------------------- event identity (named events)

def test_named_conferences_are_separate_found_items_without_duplicates(ptgx):
    expected = {"conf-wolfe-2026-11-10": "e10", "conf-truist-2026-11-12": "e11",
                "conf-ubs-2026-11-16": "e12", "conf-jefferies-2026-11-17": "e13"}
    for item_id, row_id in expected.items():
        r = item(ptgx, item_id)
        assert r["category"] == "found", (item_id, r["why"])
        assert r["best"]["row_id"] == row_id
        # the generic word "conference" is in every conference row; only the named one is a candidate
        assert [c["row_id"] for c in r["candidates"]] == [row_id]
        assert r["best"]["name"] in r["best"]["matched_words"]
    conference_rows = set(expected.values())
    assert not any(conference_rows & set(d["rows"]) for d in ptgx["duplicates"])


def test_named_event_needs_its_name_not_just_generic_words(tmp_path):
    # "conference" alone would hit all four conference rows; with the name only the Wolfe row matches, and a
    # Wolfe row at another time is not the same event (listed as a near miss, never a candidate)
    f = bench_file(tmp_path, [
        {"id": "wolfe", "name": ["Wolfe", "Wolfe Research"], "keywords": [["conference", "present"]],
         "timing": "2026-11-10", "placement": "upcoming"},
        {"id": "wolfe-other-year", "name": "Wolfe", "keywords": [["conference", "present"]],
         "timing": "2027-11-10", "placement": "upcoming"},
        {"id": "nobody", "name": "Goldman", "keywords": [["conference", "present"]],
         "timing": "2026-11-10", "placement": "upcoming"}])
    (t,) = bs.score(RUN_DIR, [f])["tickers"]
    assert item(t, "wolfe")["category"] == "found" and item(t, "wolfe")["best"]["row_id"] == "e10"
    other = item(t, "wolfe-other-year")
    assert other["category"] == "missed" and other["candidates"] == []
    assert [n["row_id"] for n in other["near_misses"]] == ["e10"] and "no overlap" in other["near_misses"][0]["timing"]
    assert "e10" in other["why"]
    assert item(t, "nobody")["category"] == "missed" and item(t, "nobody")["near_misses"] == []
    assert not any("e10" in d["rows"] for d in t["duplicates"])


def test_named_event_items_require_a_name(tmp_path):
    for words in (["conference"], ["Capital Markets Day"], ["investor day"], ["business review"], ["fireside"]):
        f = bench_file(tmp_path, [{"id": "a", "keywords": [words], "timing": "2026-11-10"}])
        with pytest.raises(ValueError, match="name"):
            bs.load_bench(f)
    f = bench_file(tmp_path, [{"id": "a", "name": "", "keywords": [["conference"]], "timing": "2026-11-10"}])
    with pytest.raises(ValueError, match="name"):
        bs.load_bench(f)
    ok = bs.load_bench(bench_file(tmp_path, [{"id": "a", "name": "Wolfe", "keywords": [["conference"]],
                                              "timing": "2026-11-10"}]))
    assert ok["items"][0]["name"] == ["Wolfe"]


def test_same_program_action_period_rows_are_possible_duplicates(ptgx):
    (group,) = [d for d in ptgx["duplicates"] if d["detail"].get("program") == "pn881"]
    assert group["rows"] == ["e14", "e15"]
    assert group["detail"] == {"program": "pn881", "action": "initiate phase 2b", "period": "2027-01-01..2027-03-31"}
    # e1 is the same event but the loader derived a January period for it: not the same period key
    assert "e1" not in group["rows"]


def test_distinct_events_of_one_program_are_never_grouped(ptgx):
    # e16 (PN-881 Phase 1 completion) shares the program, the period and the word "psoriasis" with e14/e15
    # (it is even a keyword candidate of the Phase 2b item) but it is a different event
    assert "e16" in [c["row_id"] for c in item(ptgx, "pn881-ph2b-psoriasis")["candidates"]]
    assert not any("e16" in d["rows"] for d in ptgx["duplicates"])
    assert item(ptgx, "pn881-ph2b-psoriasis")["best"]["row_id"] == "e1"


# --------------------------------------------------------------------------------------------- unmatched

def test_unmatched_run_rows_are_listed(ptgx):
    assert [u["row_id"] for u in ptgx["unmatched"]] == ["e7", "e8", "e9"]
    assert ptgx["counts"]["run_rows"] == 16
    e9 = next(u for u in ptgx["unmatched"] if u["row_id"] == "e9")
    assert e9["title"] == "Third quarter earnings call" and e9["placement"] == "upcoming"


# ------------------------------------------------------------------------------------------------ counts

def test_counts_per_ticker_and_total(result, ptgx):
    assert ptgx["counts"] == {"benchmark_items": 15, "run_rows": 16, "found": 7, "misplaced": 2, "missed": 6,
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
    assert res["totals"]["benchmark_items"] == 16 and res["totals"]["missed"] == 7


# ------------------------------------------------------------------------------- candidate order (BENCH-ORDER)

def test_compatible_candidate_wins_over_stronger_keyword_match_with_wrong_timing(tmp_path):
    # e4 (PN-477o Phase 1 initiation, obesity; 2H 2027) hits three groups but the wrong half-year; e2 (PN-8047
    # Phase 1 initiation; Q1 2027) hits two groups with compatible timing and placement: the item is found on e2
    f = bench_file(tmp_path, [{"id": "ph1-start", "keywords": [["Phase 1"], ["initiation"], ["obesity"]],
                               "min_groups": 2, "timing": "Q1 2027", "placement": "upcoming"}])
    (t,) = bs.score(RUN_DIR, [f])["tickers"]
    r = item(t, "ph1-start")
    assert r["category"] == "found" and r["best"]["row_id"] == "e2"
    assert r["best"]["timing_compatible"] and r["best"]["placement_ok"]
    assert "keyword+timing match" in r["why"]
    # every candidate stays in the output; the stronger lexical match is still shown, after the compatible one
    assert [c["row_id"] for c in r["candidates"]] == ["e2", "e4"]
    assert r["candidates"][1]["groups_hit"] == "3/3" and not r["candidates"][1]["timing_compatible"]


def test_lexical_tie_break_only_within_compatible_candidates(tmp_path):
    # both e14 and e15 are compatible; e15 hits more groups ("Phase 2b" + "psoriasis" + "study") and is chosen
    # over e14 and e1 even though e1 comes first in row order and e4 (wrong timing) also hits three groups
    f = bench_file(tmp_path, [{"id": "x", "keywords": [["PN-881", "PN-477o"], ["Phase 2b", "Phase 1"],
                                                        ["in psoriasis", "obesity"]],
                               "min_groups": 2, "timing": "Q1 2027", "placement": "upcoming"}])
    (t,) = bs.score(RUN_DIR, [f])["tickers"]
    r = item(t, "x")
    assert r["category"] == "found" and r["best"]["row_id"] == "e15"
    ids = [c["row_id"] for c in r["candidates"]]
    assert ids.index("e15") < ids.index("e1") and ids.index("e1") < ids.index("e4")


def test_still_misplaced_when_no_candidate_is_compatible(ptgx):
    r = item(ptgx, "pn477o-ph1")
    assert r["category"] == "misplaced" and r["best"]["row_id"] == "e4"


# ------------------------------------------------------------------------- current_state values (BENCH-VALUES)

def fix(target: str, field: str, from_value, to_value) -> str:
    return json.dumps({"at": "2026-11-03T10:00:00+00:00", "target": target, "action": "fix", "by": "person",
                       "note": None, "fix": {"field": field, "from": from_value, "to": to_value}})


def test_cleared_field_fix_is_not_revived_from_the_original_row(tmp_path):
    work = tmp_path / "run"
    shutil.copytree(RUN_DIR, work)
    (work / "decisions.jsonl").write_text(fix("e7", "program", "rusfertide", "") + "\n" +
                                          fix("e9", "title", "Third quarter earnings call", "") + "\n")
    run, state, partition, _ = bs.load_run(work)
    assert state["targets"]["e7"]["values"]["program"] == ""            # the fix took effect through current_state
    assert state["targets"]["e9"]["values"]["title"] == ""
    rows = {r["id"]: r for r in bs.run_rows(run, state, partition)}
    assert rows["e7"]["program"] == "" and rows["e9"]["title"] == ""
    assert "rusfertide" not in bs.normalize(rows["e7"]["program"] or "")
    assert not bs.phrase_in("Third quarter earnings call", rows["e9"]["text"])
    (t,) = bs.score(work, [PTGX])["tickers"]
    assert not any(d["rows"] == ["e7", "e8"] for d in t["duplicates"])      # no program: no identity, no group
    e9 = next(u for u in t["unmatched"] if u["row_id"] == "e9")
    assert e9["title"] == ""


def test_fixed_value_replaces_the_original_and_absent_fields_fall_back(tmp_path):
    work = tmp_path / "run"
    shutil.copytree(RUN_DIR, work)
    (work / "decisions.jsonl").write_text(fix("e9", "title", "Third quarter earnings call", "Q3 2026 earnings call") + "\n")
    run, state, partition, _ = bs.load_run(work)
    rows = {r["id"]: r for r in bs.run_rows(run, state, partition)}
    assert rows["e9"]["title"] == "Q3 2026 earnings call" and "Third quarter" not in rows["e9"]["text"]
    assert rows["e9"]["summary"] == "Routine quarterly call."                  # relevance: not fixed, from the row
    assert rows["e9"]["action"] == "host earnings call"


# ---------------------------------------------------------------------------------- never writes the run

def test_scorer_never_mutates_the_run_folder(tmp_path):
    work = tmp_path / "run"
    shutil.copytree(RUN_DIR, work)
    before = bs.folder_digest(work)
    out = tmp_path / "out" / "score.json"
    assert bs.main([str(work), str(PTGX), "--json", str(out)]) == 0
    bs.score(work, [PTGX])
    assert bs.folder_digest(work) == before          # same files, same bytes
    assert out.is_file() and json.loads(out.read_text())["totals"]["found"] == 7
    assert not list(work.glob("*.json.bak")) and sorted(p.name for p in work.iterdir()) == sorted(
        p.name for p in RUN_DIR.iterdir())


# ------------------------------------------------------------------- output path never inside the run (BENCH-READONLY)

@pytest.fixture
def work_run(tmp_path) -> Path:
    work = tmp_path / "run"
    shutil.copytree(RUN_DIR, work)
    return work


def unchanged(work: Path) -> bool:
    return bs.folder_digest(work) == bs.folder_digest(RUN_DIR) and sorted(p.name for p in work.iterdir()) == sorted(
        p.name for p in RUN_DIR.iterdir())


@pytest.mark.parametrize("inside", ["run.json", "decisions.jsonl", "scores/score.json", "pages/score.json", "."])
def test_refuses_json_output_inside_the_run_folder(work_run, capsys, inside):
    target = work_run / inside
    with pytest.raises(ValueError, match="run folder"):
        bs.refuse_unsafe_output(target, work_run, [PTGX])
    assert bs.main([str(work_run), str(PTGX), "--json", str(target)]) == 2
    assert "run folder" in capsys.readouterr().err
    assert unchanged(work_run)                                  # run.json, decisions.jsonl intact, no new file or folder


def test_refuses_json_output_through_a_symlink_into_the_run_folder(work_run, tmp_path, capsys):
    link = tmp_path / "elsewhere" / "link"
    link.parent.mkdir()
    os.symlink(work_run, link, target_is_directory=True)
    for target in (link / "score.json", link / "run.json", tmp_path / "elsewhere" / ".." / "run" / "score.json"):
        assert bs.main([str(work_run), str(PTGX), "--json", str(target)]) == 2
        assert "run folder" in capsys.readouterr().err
    file_link = tmp_path / "elsewhere" / "alias.json"
    os.symlink(work_run / "run.json", file_link)
    assert bs.main([str(work_run), str(PTGX), "--json", str(file_link)]) == 2
    assert "run folder" in capsys.readouterr().err
    assert unchanged(work_run)


def test_refuses_json_output_over_a_benchmark_file(work_run, tmp_path, capsys):
    bench = tmp_path / "bench" / "ptgx.json"
    bench.parent.mkdir()
    shutil.copy(PTGX, bench)
    before = bench.read_bytes()
    with pytest.raises(ValueError, match="benchmark file"):
        bs.refuse_unsafe_output(bench, work_run, [bench])
    assert bs.main([str(work_run), str(bench), "--json", str(bench)]) == 2
    assert "benchmark file" in capsys.readouterr().err
    assert bench.read_bytes() == before and unchanged(work_run)
    alias = tmp_path / "bench" / "same.json"
    os.symlink(bench, alias)
    assert bs.main([str(work_run), str(bench), "--json", str(alias)]) == 2
    assert bench.read_bytes() == before


def test_accepts_json_output_outside_the_run_folder(work_run, tmp_path, capsys):
    out = tmp_path / "reports" / "nested" / "score.json"
    bs.refuse_unsafe_output(out, work_run, [PTGX])                 # no error
    assert bs.main([str(work_run), str(PTGX), "--json", str(out)]) == 0
    assert "JSON written to" in capsys.readouterr().out
    assert json.loads(out.read_text())["run"]["event_rows"] == 16 and unchanged(work_run)
    sibling = tmp_path / "run-score.json"                          # shares the run folder's name prefix: fine
    assert bs.main([str(work_run), str(PTGX), "--json", str(sibling)]) == 0 and sibling.is_file()


# --------------------------------------------------------------------------------- CLI and rendering

def test_cli_prints_table_and_writes_json(tmp_path, capsys):
    out = tmp_path / "score.json"
    assert bs.main([str(RUN_DIR), str(PTGX), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Keyword+timing match only" in text
    assert "pn881-ph2b-psoriasis" in text and "FOUND" in text and "MISPLACED" in text and "MISSED" in text
    assert "Possible duplicates:" in text and "e14, e15: same program+action+period" in text
    assert "e7, e8: same program+action+period" in text and "same benchmark item" not in text
    assert "Unmatched run rows" in text
    assert "PTGX: found 7, misplaced 2, missed 6 of 15 benchmark items" in text
    assert "TOTAL: found 7, misplaced 2, missed 6 of 15" in text
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
