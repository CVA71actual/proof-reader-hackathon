"""Job 25: every mistake carries a type and a model; the two-reader bucket is named 'Both readers'."""

import json
from pathlib import Path

import pytest

from accuracy import types as T
from accuracy.export import readers
from accuracy.report import build_report
from accuracy.score import BOTH_READERS, EVENT_BUCKETS
from runs import state

RUNS = Path(__file__).resolve().parent.parent / "runs"


@pytest.mark.parametrize("name", ["sample", "twelve-4"])
def test_every_mistake_has_a_type_and_a_model(name):
    run = json.loads((RUNS / name / "run.json").read_text())
    path = RUNS / name / "decisions.jsonl"
    decisions = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    for m in state.current_state(run, decisions)["mistake_list"]:
        assert m["type"] in T.CODES, m
        assert m["model"], m


def test_mistake_type_falls_back_when_no_check_names_one():
    assert T.mistake_type(None, T.DATE_NOT_IN_QUOTE) == T.DATE_NOT_IN_QUOTE
    assert T.mistake_type({"name": "date_match", "result": "pass"}, T.DATE_NOT_IN_QUOTE) == T.DATE_NOT_IN_QUOTE
    assert T.mistake_type({"name": "quote_match", "result": "fail"}, T.WRONG_VALUE) == T.QUOTE_NOT_ON_PAGE


def test_report_keeps_both_shapes_of_the_bucket():
    key = {"page_number": 1, "cells": [], "events": []}
    ev = {b: [] for b in EVENT_BUCKETS}
    ev.update({"model": BOTH_READERS, "models": ["Muse Spark", "Muse Spark Open"], "M_in_window": 0, "M": 0,
               "counts": {b: 0 for b in EVENT_BUCKETS}, "window": None,
               "outside_window": {"count": 0, "items": [], "proposed_excluded": 0, "proposed_not_excluded": 0,
                                  "never_proposed": 0},
               "unplaced": {"count": 0, "items": [], "proposed_excluded": 0, "proposed_not_excluded": 0,
                            "never_proposed": 0}})
    rep = build_report(key, events=ev)
    assert list(rep["models"]) == ["Both readers"]
    assert rep["models"]["Both readers"]["models"] == ["Muse Spark", "Muse Spark Open"]
    assert readers({}, rep)["first"] == ["Muse Spark", "Muse Spark Open"]


def test_an_event_type_mismatch_is_typed_as_a_wrong_event_type():
    check = {"name": "type_match", "label": "Event type matches the quote", "result": "fail",
             "reason": "The quote's words (results) point to trial results, not milestone."}
    assert T.check_type(check) == T.WRONG_EVENT_TYPE
    assert T.mistake_type(check, T.WRONG_VALUE) == T.WRONG_EVENT_TYPE
    assert T.primary_type([check]) == T.WRONG_EVENT_TYPE
