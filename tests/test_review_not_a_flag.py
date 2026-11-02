"""REVIEW-NOT-A-FLAG-001 (a): every proposal needs review by design, so needing review is not an issue.

`issues` lists only real problems on a row, in plain English, and is empty when there are none. It never
changes needs_attention, review_label, accepted or on_calendar (GROUP-AXIS-001). All data here is synthetic.
"""
import copy
import json

from report.data import build_view
from runs.state import current_state
from tests.test_pivot_proposal_flow import AT, CASES, decision, kept_decision, make_run


def target(run, log=()):
    return current_state(run, list(log))["targets"]["e1"]


def test_plain_proposal_with_all_checks_passed_has_no_issues():
    _, run = make_run(CASES[0])
    state = current_state(run, [])
    t = state["targets"]["e1"]
    assert {c["result"] for c in t["effective_checks"]} <= {"pass", "not_needed"}
    assert t["issues"] == []
    assert state["counts"]["issues"] == 0
    # Needing review is unchanged and is not an issue.
    assert t["needs_attention"] and t["review_label"] == "Proposed — needs review"


def test_could_not_run_alone_is_not_an_issue():
    _, run = make_run(CASES[3])
    t = target(run)
    assert "could_not_run" in {c["result"] for c in t["effective_checks"]}
    assert "fail" not in {c["result"] for c in t["effective_checks"]}
    assert t["issues"] == []


def test_failed_current_check_is_named_in_plain_words():
    _, run = make_run(CASES[1], event_updates={"quote": "Nothing like this is printed on the page at all."})
    t = target(run)
    assert "Quote found on page failed" in t["issues"]
    assert all(i.endswith(" failed") for i in t["issues"])


def test_stale_saved_failure_is_history_not_an_issue():
    _, run = make_run(CASES[1], event_updates={"quote": "Nothing like this is printed on the page at all."})
    run["sources"][0]["pages"][0]["words_sha256"] = "0" * 64  # the bound evidence no longer matches
    t = target(run)
    assert t["effective_checks"] == [] and t["issues"] == []


def test_open_disagreement_is_an_issue_until_kept():
    _, run = make_run(CASES[0])
    run["rows"][0]["disagreement"] = {"field": "date", "first_reader": "2026-11-12",
                                      "second_reader": "2026-11-13"}
    t = target(run)
    assert t["issues"] == ["The two readers disagree on the date"]
    keep = kept_decision(run)
    assert target(run, [keep])["issues"] == []


def test_one_sided_disagreement_is_not_an_issue():
    _, run = make_run(CASES[0])
    run["rows"][0]["disagreement"] = {"field": "title", "first_reader": "Results", "second_reader": None}
    assert target(run)["issues"] == []


def test_second_reader_mistake_is_an_issue_but_a_code_catch_is_not():
    _, run = make_run(CASES[0])
    run["mistakes"] = [{"id": "m1", "target": "e1", "caught_by": "second_reader", "type": "WRONG_COMPANY",
                        "what_changed": "Company changed."}]
    assert target(run)["issues"] == ["The second reader caught a mistake on this row"]
    run["mistakes"][0]["caught_by"] = "code"
    assert target(run)["issues"] == []


def test_unreadable_answer_and_unreadable_page_are_issues():
    _, run = make_run(CASES[0])
    row = run["rows"][0]
    row.update(status="unreadable", group="open", checks=[],
               reason="The reader's answer could not be read three times; sent to a person.")
    assert target(run)["issues"] == ["The reader's answer could not be read"]
    _, run = make_run(CASES[0])
    run["run"]["page_outcomes"] = [{"source_id": "synthetic-source", "page_number": 1, "captured": True,
                                    "attempted": True, "read": False, "unreadable": True, "skipped": False,
                                    "unread": True}]
    assert target(run)["issues"] == ["The page could not be read"]


def test_expired_keep_is_an_issue():
    _, run = make_run(CASES[1])
    keep = kept_decision(run)
    assert target(run, [keep])["issues"] == []
    run["rows"][0]["conditions"] = []  # the proposal changed after the Keep
    t = target(run, [keep])
    assert t["review_label"] == "Previous Keep — needs your review"
    assert t["issues"] == ["Your earlier Keep expired because the evidence changed"]


def test_accepted_and_excluded_rows_have_no_issues_and_counts():
    _, run = make_run(CASES[1], event_updates={"quote": "Nothing like this is printed on the page at all."})
    second = copy.deepcopy(run["rows"][0])
    second["id"] = "e2"
    run["rows"].append(second)
    state = current_state(run, [])
    assert state["counts"]["issues"] == 2
    before = {k: state["targets"]["e1"][k] for k in ("needs_attention", "review_label", "accepted", "on_calendar")}
    removed = current_state(run, [{**decision("remove"), "note": "Not an event."}])
    assert removed["targets"]["e1"]["issues"] == [] and removed["counts"]["issues"] == 1
    kept = current_state(run, [kept_decision(run)])
    assert kept["targets"]["e1"]["accepted"] and kept["targets"]["e1"]["issues"] == []
    assert kept["counts"]["issues"] == 1
    # The issue field leaves the acceptance projection exactly as it was.
    assert before == {"needs_attention": True, "review_label": "Proposed — needs review",
                      "accepted": False, "on_calendar": False}


def test_report_view_carries_issues():
    _, run = make_run(CASES[1], event_updates={"quote": "Nothing like this is printed on the page at all."})
    view = build_view(run, [], generated_at=AT)
    assert view["issues"] == 1
    assert view["open_rows"][0]["issues"] == ["Quote found on page failed", "Date period found in quote failed"]


def test_api_target_state_and_run_list_carry_issues(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app

    make_run(CASES[1], tmp_path / "synthetic", event_updates={"quote": "Nothing like this is printed on the page at all."})
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        body = client.get("/api/runs/synthetic").json()
        assert "Quote found on page failed" in body["state"]["targets"]["e1"]["issues"]
        assert body["state"]["counts"]["issues"] == 1
        listed = {r["id"]: r for r in client.get("/api/runs").json()}
        assert listed["synthetic"]["counts"]["issues"] == 1
        # A failed row is still a proposal that needs review; the issue is separate.
        assert listed["synthetic"]["unresolved"] == 1
        json.dumps(body)


def test_free_text_disagreement_is_not_an_issue():
    _, run = make_run(CASES[0])
    run["rows"][0]["disagreement"] = {
        "field": "type_hint", "first_reader": "earnings release", "second_reader": "quarterly results",
        "also": [{"field": "title", "first_reader": "Q3 results", "second_reader": "Third-quarter results"}]}
    t = target(run)
    assert t["status"] == "undecided"  # still held for review; only the issue flag is withheld
    assert t["issues"] == []
    # A factual field among the "also" entries is still an issue.
    run["rows"][0]["disagreement"]["also"].append(
        {"field": "program", "first_reader": "CB-100", "second_reader": "CB-200"})
    assert target(run)["issues"] == ["The two readers disagree on the program"]
