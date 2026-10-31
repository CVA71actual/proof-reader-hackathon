"""Job 16: the sealed-set scorer respects the run's time window.

A small fake run and key. Window: the run started 2026-10-29 with 6 months, so [2026-10-29, 2027-04-29].
"""

from datetime import date

from accuracy.export import window_block
from accuracy.mistake_log import log_from_score
from accuracy.report import build_report, header_line, render_text
from accuracy.score import add_months, in_window, run_window, score_events

WINDOW = (date(2026, 10, 29), 6)


def kev(program, date_text, event_type="milestone"):
    return {"ticker": "ZZZ", "program": program, "event_type": event_type, "date_text": date_text,
            "quote": f"{program} {date_text}."}


KEY = {"complete": True, "sealed": False, "page_number": 1, "source_sha256": "0" * 64, "cells": [],
       "events": [
           kev("ALPHA", "March 1, 2027"),          # in window, proposed and kept: found
           kev("BETA", "September 4, 2026"),       # before the start: proposed, left out by the window
           kev("GAMMA", "November 1, 2028"),       # after the end: never proposed
           kev("DELTA", "mid-2027"),               # vague: never proposed
           kev("EPSILON", "late 2027"),            # vague: proposed, kept
           kev("ZETA", "April 1, 2027"),           # in window, never proposed: missed
       ]}


def row(i, program, date_text, **kw):
    base = {"id": f"e{i}", "ticker": "ZZZ", "program": program, "event_type": "milestone", "date_text": date_text,
            "status": "matched", "checks": [], "proposed_by": "Muse Spark", "page_number": 1}
    return {**base, **kw}


ROWS = [
    row(1, "ALPHA", "March 1, 2027"),
    row(2, "BETA", "September 4, 2026", status="proposed"),
    row(3, "EPSILON", "late 2027"),
    row(4, "OMEGA", "February 1, 2027"),                          # invented in the window
    row(5, "SIGMA", "August 1, 2026"),                            # not in the key, removed by the window
]
EXCLUSIONS = [{"id": "x1", "target": "e2", "rule": "outside_time_window", "by": "code"},
              {"id": "x2", "target": "e5", "rule": "outside_time_window", "by": "code"}]


def scored(**kw):
    return score_events(ROWS, KEY, EXCLUSIONS, window=WINDOW, **kw)


def test_the_window_comes_from_the_run_start_and_months():
    run = {"run": {"started_at": "2026-10-29T04:46:17+00:00", "choices": {"time_window_months": 6}}}
    assert run_window(run) is None
    assert run_window({"run": {"started_at": "2026-10-29T04:46:17+00:00", "choices": {"time_window_months": None}}}) is None
    assert run_window({"run": {"started_at": None, "choices": {"time_window_months": 6}}}) is None
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)


def test_in_window_flag():
    assert in_window("March 1, 2027", WINDOW) is True
    assert in_window("October 29, 2026", WINDOW) is True and in_window("April 29, 2027", WINDOW) is True
    assert in_window("October 28, 2026", WINDOW) is False and in_window("April 30, 2027", WINDOW) is False
    assert in_window("Q1 2027", WINDOW) is True                  # a period counts by its first day
    assert in_window("Q3 2027", WINDOW) is False
    assert in_window("mid-2027", WINDOW) is None and in_window("late 2027", WINDOW) is None
    assert in_window("mid-2027", None) is True                   # no window: everything is in it


def test_in_window_events_are_scored_alone():
    sc = scored()
    c = sc["counts"]
    assert (c["found"], c["missed"], c["wrong_date"], c["wrong_type"], c["unsupported"]) == (1, 1, 0, 0, 0)
    assert [m["expected"]["program"] for m in sc["missed"]] == ["ZETA"]
    assert sc["M"] == 6 and sc["M_in_window"] == 2


def test_out_of_window_proposed_and_excluded_is_not_unsupported():
    sc = scored()
    assert sc["unsupported"] == [] and sc["wrong_date"] == []
    ow = sc["outside_window"]
    assert ow["proposed_excluded"] == 1 and ow["never_proposed"] == 1 and ow["proposed_not_excluded"] == 0
    assert {(i["expected"]["program"], i["state"]) for i in ow["items"]} == {
        ("BETA", "proposed_excluded"), ("GAMMA", "never_proposed")}
    assert all(i["in_window"] is False for i in ow["items"])


def test_vague_dates_are_unplaced_with_the_same_split():
    up = scored()["unplaced"]
    assert up["count"] == 2 and up["never_proposed"] == 1 and up["proposed_not_excluded"] == 1
    assert {(i["expected"]["program"], i["state"], i["in_window"]) for i in up["items"]} == {
        ("DELTA", "never_proposed", None), ("EPSILON", "proposed_not_excluded", None)}


def test_every_key_event_lands_in_exactly_one_bucket_and_they_sum_to_m():
    sc = scored()
    in_buckets = sum(len(sc[b]) for b in ("found", "wrong_date", "wrong_type", "unsupported", "missed"))
    assert in_buckets == sc["M_in_window"] == 2
    assert in_buckets + sc["outside_window"]["count"] + sc["unplaced"]["count"] == sc["M"] == 6


def test_invented_splits_into_in_window_and_excluded_by_window():
    sc = scored()
    assert [i["program"] for i in sc["invented"]] == ["OMEGA"]
    assert [i["program"] for i in sc["excluded_by_window"]] == ["SIGMA"]
    assert sc["counts"]["invented_in_window"] == 1 and sc["counts"]["excluded_by_window"] == 1
    mistakes = [m["item"]["program"] for m in log_from_score(sc)]
    assert "SIGMA" not in mistakes and "OMEGA" in mistakes, "only the in-window invented row counts against the model"


def test_without_a_window_the_old_numbers_stand():
    sc = score_events(ROWS, KEY, EXCLUSIONS)
    assert sc["M_in_window"] == sc["M"] == 6
    assert sc["outside_window"]["count"] == 0 and sc["unplaced"]["count"] == 0
    assert [i["id"] for i in sc["unsupported"]] == ["e2"]        # the removed row on a key event, as before
    assert [i["id"] for i in sc["excluded_by_window"]] == ["e5"]  # the rule's own removal is still not invented
    assert sum(sc["counts"][b] for b in ("found", "wrong_date", "wrong_type", "unsupported", "missed")) == 6


def test_a_wrong_date_for_an_in_window_event_still_counts_against_the_model():
    rows = [row(1, "ALPHA", "May 1, 2027", status="matched")]
    key = {**KEY, "events": [kev("ALPHA", "March 1, 2027")]}
    sc = score_events(rows, key, [], window=WINDOW)
    assert sc["counts"]["wrong_date"] == 1 and sc["outside_window"]["count"] == 0


def test_report_and_export_carry_the_buckets_beside_the_old_fields():
    rep = build_report(KEY, None, events=scored())
    ev = rep["models"]["Muse Spark"]["events"]
    assert ev["found"] == 1 and ev["M"] == 2 and ev["M_total"] == 6          # found / M is the in-window number
    for old in ("wrong_date", "wrong_type", "unsupported", "missed", "invented", "unscored"):
        assert old in ev
    assert ev["outside_window"] == {"count": 2, "proposed_excluded": 1, "proposed_not_excluded": 0, "never_proposed": 1}
    assert ev["unplaced"]["count"] == 2 and ev["excluded_by_window"] == 1 and ev["invented_in_window"] == 1
    assert ev["window"] == {"start": "2026-10-29", "months": 6, "end": "2027-04-29"}
    assert header_line("Muse Spark", rep["models"]["Muse Spark"]).startswith("Muse Spark values not scored, 1/2 events")
    text = render_text(rep)
    assert "outside the window: 2 (proposed and left out by the window 1" in text
    assert "unplaced (vague date): 2" in text and "2026-10-29 to 2027-04-29" in text
    block = window_block(rep["events"])
    assert block["M"] == 6 and block["M_in_window"] == 2 and len(block["outside_window"]["items"]) == 2
    assert block["excluded_by_window"] and block["excluded_by_window"][0]["program"] == "SIGMA"
