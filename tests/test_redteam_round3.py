"""From Red Team round 3 (Grok Red Team, redteam/r3, tests/redteam/test_round3.py in proof-reader-redteam): the eight attacks,
copied here so they run in CI. The only change from the original is the signature of the stubbed SecondReader._ask (it now takes
the image the second reader is shown). A failure is a finding: the assertion is the result the code should have given.
Base of the original: product main 4557556.

Scope: the second reader (runs/compare.py, capture/second_reader.py), the run controls (server/jobs.py),
and the coverage scan (checks/coverage.py). A failure is a finding. No model call, no network, no key.
"""

import json

import pytest
from fastapi.testclient import TestClient

from capture import read_events, read_table
from capture.reader import Reply
from capture.second_reader import NO_KEY, OPENROUTER_MODEL, SecondReader
from checks.coverage import sentence_covered, uncovered
from checks.event_checks import _norm_quote
from runs import compare, prices
from server import jobs
from server.app import create_app
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond

REPEATED = "Q3 2027 is planned and Q3 2027 is planned and Q3 2027 is planned."
REPEATED_QUOTE = "planned and is Q3 2027"
TWO_PERIODS = ("The Phase 3 of drug A starts in Q3 2027 and the Phase 3 of drug A starts in Q4 2027.")
TWO_PERIODS_QUOTE = "The Phase 3 of drug A starts in Q3 2027 and Q4"


def _occurrence_share(sentence: str, quote: str) -> float:
    """Share of the sentence's words a quote can cover if each of its words is used once."""
    pool = _norm_quote(quote).lower().split()
    tokens = _norm_quote(sentence).lower().split()
    hit = 0
    for token in tokens:
        if token in pool:
            pool.remove(token)
            hit += 1
    return hit / len(tokens)


def test_repeated_words_do_not_let_a_short_quote_cover_the_sentence():
    """Five words do not hold 80% of a fourteen-word sentence, however many times the sentence repeats them."""
    share = _occurrence_share(REPEATED, REPEATED_QUOTE)
    assert share < 0.8
    assert sentence_covered(REPEATED, [REPEATED_QUOTE]) is False, share
    assert uncovered([{"text": REPEATED}], [REPEATED_QUOTE]) == [{"text": REPEATED}]


def test_a_bag_of_words_does_not_hide_the_second_period():
    """The quote names Q3 and then the token Q4. It does not hold the sentence that states Q3 2027 and Q4 2027."""
    share = _occurrence_share(TWO_PERIODS, TWO_PERIODS_QUOTE)
    assert share < 0.8
    assert TWO_PERIODS_QUOTE.lower() not in TWO_PERIODS.lower()
    assert sentence_covered(TWO_PERIODS, [TWO_PERIODS_QUOTE]) is False, share


def test_a_reordered_handful_of_words_is_not_the_same_event():
    """The second reader's quote is not the first reader's passage. It must not be consumed as agreement."""
    row = {"quote": REPEATED, "program": "Drug A", "indication": None, "event_type": "milestone",
           "date": "2027-07-01", "date_end": "2027-09-30", "status": "matched",
           "checks": [{"result": "pass"}], "reason": "matched", "trail": []}
    event = {**row, "quote": REPEATED_QUOTE, "trail": None, "checks": None, "reason": None}
    assert compare.match_events([row], [event]) == {}
    reading = {"prompt_text": "Read the page.", "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    rows, new_rows = compare.apply_events([row], [event], reading, {}, {}, "2026-10-29T00:00:00+00:00", 2)
    assert new_rows, "a passage the first reader did not write stays a second-reader row"
    assert not any("read the same" in step["text"] for step in rows[0]["trail"])


def test_a_second_value_for_the_same_cell_is_not_dropped():
    """The second reader emitted two values for one cell. The one that differs is still a disagreement."""
    check = {"result": "pass"}
    first = {"rows": [{"label": "Net loss", "cells": [
        {"id": "t1.0.0", "column": "2025", "value": "(1,234)", "status": "matched", "checks": [check]}]}]}
    second = {"rows": [
        {"label": "Net loss", "cells": [{"column": "2025", "value": "(1,234)"}]},
        {"label": "Net loss", "cells": [{"column": "2025", "value": "(9,999)"}]},
    ]}
    assert compare.apply_table(first, second) == ["t1.0.0"]
    cell = first["rows"][0]["cells"][0]
    assert cell["status"] == "undecided"
    assert cell["disagreement"] == {"field": "value", "first_reader": "(1,234)", "second_reader": "(9,999)"}


def _start(tmp_path, reader, second=None, **choices):
    runs = tmp_path / "runs"
    runs.mkdir()
    body = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
            "spending_limit_usd": 0.5, "note_for_agent": None, **choices}
    client = TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui", reader=reader, second_reader=second,
                                   background=False))
    response = client.post("/api/runs/new", json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    run = json.loads((runs / run_id / "run.json").read_text())
    job = json.loads((runs / run_id / "job.json").read_text())
    return run, job


def test_the_call_that_crosses_the_spending_limit_stops_the_run(tmp_path):
    """One page, one call, cost $0.60, limit $0.50. The run stops over the limit. It is not finished."""
    run, _job = _start(tmp_path, FakeReader(cost=0.60))
    meta = run["run"]
    assert meta["spend_usd"] == pytest.approx(0.60)
    assert meta["status"] == "needs_input"
    assert meta["status_reason"].startswith("Stopped at the spending limit")
    assert meta["finished_at"] is None


def test_the_second_readers_call_is_inside_the_same_spending_limit(tmp_path):
    """The first call stays under the limit and the second reader's call crosses it. That stops the run."""
    run, _job = _start(tmp_path, FakeReader(cost=0.30), FakeSecond(tokens=(1_500_000, 0)))
    meta = run["run"]
    # Spark Open is priced at the confirmed $1.25 / $4.25 / cached $0.15, so 1.5M prompt tokens cost $1.875, not the old placeholder's $0.30.
    assert meta["spend_usd"] == pytest.approx(2.175)
    assert meta["status"] == "needs_input"
    assert meta["status_reason"].startswith("Stopped at the spending limit")
    assert meta["finished_at"] is None


def test_an_unpriced_model_name_on_the_reply_stops_a_limited_run(tmp_path):
    """The reader advertises a priced model. The reply names one with no price. The limit cannot be kept."""

    class Slip:
        model = "muse-spark-1.3"

        def ready(self):
            return None

        def call(self, prompt):
            return Reply(FakeReader.default(prompt), "not-a-priced-model", prompt_tokens=10, completion_tokens=10)

    run, job = _start(tmp_path, Slip())
    meta = run["run"]
    assert job["unpriced_calls"] >= 1 and meta["spend_usd"] is None
    assert meta["status"] == "needs_input"
    assert "could not be counted" in meta["status_reason"]
    assert meta["finished_at"] is None


def test_the_openrouter_cap_does_not_fail_the_run_by_saying_the_key_is_missing(tmp_path, monkeypatch):
    """The fallback key is present. Once its spend is over the cap, the next second read is skipped.
    The run is not failed, and the reason does not say the key was missing."""
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    one_call = prices.cost_usd("Muse Spark Open", 10_000_000, 10_000_000)
    monkeypatch.setattr(jobs, "OPENROUTER_CAP_USD", one_call * 0.5)
    calls = []

    def ask(self, route, prompt, image=None):
        calls.append(route)
        return Reply('{"events": []}', OPENROUTER_MODEL, prompt_tokens=10_000_000, completion_tokens=10_000_000,
                     extra={"route": route})

    monkeypatch.setattr(SecondReader, "_ask", ask)
    run, job = _start(tmp_path, FakeReader(), SecondReader(), companies=["PTGX", "IONS"], spending_limit_usd=None)
    meta = run["run"]
    assert calls == ["openrouter"]
    assert meta["status"] == "done", meta["status_reason"]
    assert NO_KEY not in (meta["status_reason"] or "")
    assert any(item["status"] == "not_run" for item in job["second"].values())
    assert all(NO_KEY not in (item["reason"] or "") for item in job["second"].values())
