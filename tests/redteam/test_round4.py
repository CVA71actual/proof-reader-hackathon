"""Red Team round 4. A failure is a finding: each assertion is the result the code should have given.

Base: product main 525f06e. No model call, no network, no key. The readers are fakes.
Scope: a quote that passes on a character prefix, a date the clause rule attaches to the wrong word,
October read out of "Sept/Oct", a box located on the farther copy of a quote, a Wonder call past a
quarter of the spending limit that still replaces a row, a resume that pays for the second reader's
image call twice, and a send-back that smuggles a dollar sign onto a sentence that is printed.
"""

import json

import pytest

from capture import read_table
from checks.coverage import sentence_covered
from checks.event_checks import date_match
from checks.selector_match import selector_match
from runs import build_entry, prices
from server import jobs
from tests.test_escalation import FakeWonder, job_of, reread_right, run_of, start, wrong_date_events
from tests.test_r12_fixwave import _q1_quote, _reader, _start
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond, start as start_second

MARKED_Q1 = "Q1 \u201927"  # the year-less date the first reader writes; the send-back must not replace it


def _no_keys(monkeypatch, tmp_path):
    """Point the key file away and drop key names before any reader can load them. Values are never read."""
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    for name in ("MODEL_API_KEY", "BASETEN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _line(y0, prefix):
    words = []
    x = 10.0
    for i, text in enumerate("Phase 3 starts in Q3 2027".split()):
        words.append({"id": f"{prefix}{i}", "text": text, "x0": x, "y0": y0, "x1": x + 40.0, "y1": y0 + 10.0})
        x += 50.0
    return words


# ---------------------------------------------------------------- quotes and dates


def test_a_character_prefix_is_not_a_run_of_words():
    """'See May 1' is not the words 'See May 12'. Gap 8: a quote covers a sentence as a run of words."""
    quote = "See May 12, 2027 in the full calendar and the appendix."
    assert sentence_covered("See May 1", [quote]) is False


def test_printed_dates_do_not_establish_the_event_type():
    """DATE-LITERAL-001: original keyword traps stay intact; meaning stays held."""
    from tests.pivot_assertions import assert_proposal_held
    reported = date_match("2026", "2026-01-01", "The company reported cash on hand in 2026.", "2026-12-31",
                          event_type="trial_results")
    quote = "The database locks in Q3 2026. Topline data are expected in Q4 2027."
    q3 = date_match("Q3 2026", "2026-07-01", quote, "2026-09-30", event_type="trial_results")
    q4 = date_match("Q4 2027", "2027-10-01", quote, "2027-12-31", event_type="trial_results")
    assert (reported.result, q3.result, q4.result) == ("pass", "pass", "pass")

    assert_proposal_held("The company reported cash on hand in 2026.", "trial_results",
                         date_text="2026", start="2026-01-01", end="2026-12-31")
    for text, start, end in (("Q3 2026", "2026-07-01", "2026-09-30"),
                             ("Q4 2027", "2027-10-01", "2027-12-31")):
        assert_proposal_held(quote, "trial_results", date_text=text, start=start, end=end)


def test_october_is_not_the_date_printed_as_sept_slash_oct():
    """The page prints Sept/Oct 2026, not the month of October. Gap 8."""
    got = date_match("Oct 2026", "2026-10-01", "Enrollment completed in Sept/Oct 2026.", "2026-10-31",
                     event_type="milestone")
    assert got.result == "fail"


# ---------------------------------------------------------------- the box


def test_a_box_hint_locates_the_nearer_copy_of_the_quote():
    """Two copies of one sentence. A hint just above the lower copy overlaps neither, so both overlaps are zero
    and the first copy wins. Gap 1: the box is the run nearest the reader's box."""
    upper, lower = _line(20.0, "u"), _line(700.0, "d")
    words = upper + lower
    quote = "Phase 3 starts in Q3 2027"
    hint = [10.0, 680.0, 300.0, 695.0]
    page = (max(w["x1"] for w in words), max(w["y1"] for w in words))
    assert selector_match(quote, hint, words, page[0], page[1], quote_lines=True).result != "pass"
    nearer = [min(w["x0"] for w in lower), min(w["y0"] for w in lower),
              max(w["x1"] for w in lower), max(w["y1"] for w in lower)]
    assert build_entry.locate_box(quote, hint, words) == nearer


# ---------------------------------------------------------------- Wonder, resume, send-back


def test_a_wonder_call_past_a_quarter_of_the_limit_does_not_replace_the_row(tmp_path, words_doc, monkeypatch):
    """The pre-check looks at Wonder's spend before the call. A call that costs $1 on a $2 limit (the quarter
    is $0.50) is sent and its reading replaces the row. The run's own limit is not what stops it: the first
    reader costs $0.01 and the run finishes under $2. Gap 10: a fix past the cap is not a corrected row.
    """
    _no_keys(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    first = FakeReader(cost=0.01, answer=lambda prompt, n: json.dumps({"events": wrong_date_events()}))
    wonder = FakeWonder(tokens=(250_000, 0), answer=reread_right(words_doc))
    _, rid = start(runs, first, None, wonder, spending_limit_usd=2)
    run, job = run_of(runs, rid), job_of(runs, rid)
    assert run["run"]["status"] == "done" and run["run"]["spend_usd"] < 2
    row = next(r for r in run["rows"] if r.get("date_text") == "Q1 2027")
    assert row["status"] == "failed" and row["group"] != "corrected"
    assert "replaced" not in job.get("escalated_rows", {}).values()
    assert not [m for m in run["mistakes"] if m["target"] == row["id"] and m["caught_by"] == "code"]


def test_a_resume_does_not_pay_for_the_second_readers_image_call_twice(tmp_path, monkeypatch):
    """The image answer cannot be read, and the turn limit stops the run before the words fallback. A resume
    sends the image prompt again, then the words. Gap: a resume does not pay for a call twice. The image
    prompt is the one with no word list (no '|' line)."""
    _no_keys(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 2)
    runs = tmp_path / "runs"
    runs.mkdir()

    def answer(prompt, n):
        if "|" not in prompt:
            return "not json at all"
        return FakeReader.default(prompt)

    second = FakeSecond(answer=answer, tokens=(1000, 100))
    client, rid = start_second(runs, FakeReader(cost=0.01), second)
    assert run_of(runs, rid)["run"]["status"] == "paused_turn_limit"
    assert client.post(f"/api/runs/{rid}/resume").status_code == 202
    run = run_of(runs, rid)
    kinds = ["words" if "|" in prompt else "image" for prompt in second.prompts]
    one = prices.cost_usd(jobs.price_name(second.model), 1000, 100)
    assert run["run"]["status"] == "done"
    assert kinds == ["image", "words"]
    assert run["run"]["spend_usd"] == pytest.approx(0.01 + 2 * one)


def test_a_send_back_cannot_smuggle_a_dollar_sign_onto_the_printed_sentence(tmp_path, monkeypatch):
    """The answer puts '$' on the sentence that is printed. The compare-form drops a leading '$', so the
    sentence is taken and the row becomes matched. Gap 1 and gap 10: the quote on the row is the printed
    sentence, and a send-back that is not that sentence does not clear the date."""
    _no_keys(monkeypatch, tmp_path)
    calls = []
    printed = _q1_quote()
    run, _ = _start(tmp_path, _reader(lambda i: {"id": i, "quote": "$" + printed, "date_text": "Q1 2027"}, calls))
    assert calls == ["read", "send-back"]
    row = next(r for r in run["rows"] if printed in (r.get("quote") or "").replace("$", ""))
    assert (row["quote"], row["date_text"], row["status"]) == (printed, MARKED_Q1, "failed")
