"""Boundary cases left by the round-4 fixes: word characters around an event word, and a Wonder reply that costs
exactly its share of the spending limit."""

import json

import pytest

from capture import read_table
from checks.event_checks import date_match
from tests.test_escalation import FakeWonder, job_of, reread_right, run_of, start, wrong_date_events
from tests.test_run_controls import FakeReader


@pytest.mark.parametrize("word", ["data_code", "data2", "_data", "2data"])
def test_an_event_word_is_not_part_of_a_longer_word_of_digits_or_underscores(word):
    got = date_match("Q4 2027", "2027-10-01", f"The company expects {word} in Q4 2027.", "2027-12-31",
                     event_type="trial_results")
    assert got.result == "could_not_run"


def _run(tmp_path, monkeypatch, tokens):
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    for name in ("MODEL_API_KEY", "BASETEN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    runs = tmp_path / "runs"
    runs.mkdir()
    first = FakeReader(cost=0.01, answer=lambda prompt, n: json.dumps({"events": wrong_date_events()}))
    return runs, first, tokens


def test_a_wonder_reply_costing_exactly_its_share_is_applied(tmp_path, words_doc, monkeypatch):
    """$2 limit, quarter $0.50, reply costs $0.50 (125,000 prompt tokens at $4 per 1M): at the cap, not past it."""
    runs, first, _ = _run(tmp_path, monkeypatch, None)
    wonder = FakeWonder(tokens=(125_000, 0), answer=reread_right(words_doc))
    _, rid = start(runs, first, None, wonder, spending_limit_usd=2)
    run, job = run_of(runs, rid), job_of(runs, rid)
    assert job["wonder_spend_usd"] == pytest.approx(0.5)
    assert job["escalated_rows"] and set(job["escalated_rows"].values()) == {"replaced"}
    assert len(wonder.prompts) == 1


def test_a_wonder_reply_a_cent_past_its_share_is_not_applied(tmp_path, words_doc, monkeypatch):
    runs, first, _ = _run(tmp_path, monkeypatch, None)
    wonder = FakeWonder(tokens=(127_500, 0), answer=reread_right(words_doc))
    _, rid = start(runs, first, None, wonder, spending_limit_usd=2)
    assert set(job_of(runs, rid)["escalated_rows"].values()) == {"kept"}
