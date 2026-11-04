"""RUN9-COUNTERS (CVA + Codex 2026-11-03): the export no longer shows a "Mistakes caught by code" aggregate (run 9
showed 0 while two date checks failed, eight could not run, the independent review made no calls and LettuceDetect
did not run). It states, per company, whether the independent review and LettuceDetect ran, from their saved
records. Row-level check results are unchanged. Synthetic records plus a read-only look at the saved run 9."""
import copy
import json
from pathlib import Path

import pytest

from report.data import build_view, load_run_dir, stage_lines
from tests.test_report_layout import FIXED_TIME, RUNS, _view

RUN9 = Path("/Users/cva26/proof-reader-candidate/runs/run-9/run.json")


def _research_run(review=None, support=None, requested="lettucedetect"):
    run = copy.deepcopy(load_run_dir(RUNS["e2e_brief"])[0])
    run["run"]["choices"]["support_check"] = requested
    run["run"]["research"]["review"] = review or {}
    if support is not None:
        run["run"]["research"]["support_check"] = support
    else:
        run["run"]["research"].pop("support_check", None)
    return run


def test_lettucedetect_ran_with_flags_and_review_completed():
    run = _research_run(
        review={"PTGX": {"status": "complete", "reviewer": "DeepSeek V4 Pro",
                         "review": {"findings": [{}] * 6, "usage": {"model_requests": 4}}}},
        support={"engine": "lettucedetect", "companies": {"PTGX": {"no_finding": 16, "unsupported_spans": 13,
                                                                   "could_not_run": 0, "worker": {}}}})
    assert stage_lines(run) == ["Independent review (PTGX): DeepSeek V4 Pro, completed, 6 findings",
                                "LettuceDetect (PTGX): ran on 29 rows, 13 flags (advisory)"]


def test_partial_and_not_run_states_stay_distinct():
    partial = _research_run(support={"companies": {"PTGX": {"no_finding": 3, "unsupported_spans": 1,
                                                            "could_not_run": 2, "worker": {"reason": "timed out"}}}})
    assert stage_lines(partial)[1] == ("LettuceDetect (PTGX): ran on 4 rows, 1 flag (advisory); could not run on "
                                       "2 rows (timed out)")
    missing = _research_run()
    assert stage_lines(missing) == ["Independent review (PTGX): none recorded",
                                    "LettuceDetect (PTGX): not run (no result was saved)"]
    off = _research_run(requested=None)
    assert stage_lines(off)[1] == "LettuceDetect (PTGX): not requested for this run"


def test_page_mode_run_has_no_stage_lines_and_no_code_counter():
    view = _view("sample")
    assert view["stage_lines"] == []
    assert "Mistakes caught by code" not in view["counters_line"]


@pytest.mark.skipif(not RUN9.is_file(), reason="saved run 9 is not on this machine")
def test_run9_export_states_both_stages_did_not_run():
    run = json.loads(RUN9.read_text())
    before = RUN9.read_bytes()
    view = build_view(run, [], generated_at=FIXED_TIME)
    assert view["stage_lines"] == [
        "Independent review (PTGX): no calls made — A person stopped the run, so the independent review was not "
        "run; nothing was reviewed.",
        "LettuceDetect (PTGX): not run (stopped by a person)"]
    assert "Mistakes caught by code" not in view["counters_line"]
    # row-level check results are unchanged: the saved date checks still carry e10's and e25's reasons
    by_id = {r["id"]: r for r in run["rows"]}
    assert {c["result"] for c in by_id["e10"]["checks"] if c["name"] == "date_match"} == {"fail"}
    assert RUN9.read_bytes() == before
