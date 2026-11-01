"""Job 51j: on a run with no spending limit, a call answered only on a second try after a possibly-billed timeout, by a
model with no price, is counted as an unpriced call (every possibly-billed call is counted, limit or not). The run
pauses at the one gate before the next call is sent: the reply itself was not an error, so the run goes on to the
next check before a call, and that check stops it on the unknown cost. No test reaches the network."""

import httpx

from tests.test_r38_timeouts import FlakyThenAnswers, job_of, no_sleep, run_of, runs_dir, start  # noqa: F401

PLAIN = ("A call may have been billed but could not be priced. A person must accept the unknown cost before this run "
         "can resume.")


def test_no_limit_unpriced_retry_after_a_possibly_billed_timeout_is_counted_and_pauses(runs_dir):
    reader = FlakyThenAnswers(httpx.ReadTimeout("x"), priced=False)
    rid = start(runs_dir, reader, companies=["PTGX", "IONS"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert run["run"]["choices"]["spending_limit_usd"] is None
    assert job["unpriced_calls"] == 1 and job["retried_after_timeout"] == 1 and not job.get("retries_charged")
    assert job["retried_models"] == [reader.model]
    assert run["run"]["turns_total"] == 1                    # paused at the gate before the next call was sent
    assert run["run"]["status"] == "needs_input"
    assert run["run"]["status_reason"].startswith(PLAIN)
    assert "accept_unknown_cost" not in run["run"]["status_reason"]      # no field name on screen


def test_no_limit_priced_retry_is_charged_and_not_counted_unpriced(runs_dir):
    rid = start(runs_dir, FlakyThenAnswers(httpx.ReadTimeout("x")), companies=["PTGX"])
    run, job = run_of(runs_dir, rid), job_of(runs_dir, rid)
    assert job["unpriced_calls"] == 0 and job["retries_charged"] == 1 and run["run"]["status"] == "done"
