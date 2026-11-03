"""Job 21: fixes from the first glass check (a person tried the screens)."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from checks import event_checks
from checks.common import COULD_NOT_RUN, FAIL, PASS
from checks.selector_match import PICTURE_REASON, selector_match
from runs import build_entry
from runs.calendar import calendar_entries, on_calendar as state_on_calendar
from runs.recheck import recheck_row, row_checks
from runs.state import current_state
from server.app import RunStore, create_app

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
STORE = RunStore(RUNS)


def twelve_row(row_id):
    run = STORE.run("twelve-4")
    row = next(r for r in run["rows"] if r["id"] == row_id)
    source = next(s for s in run["sources"] if s["id"] == row["source_id"])
    return row, source, STORE.words_doc("twelve-4", source, row["page_number"])


def recheck_of(row_id, **fixed):
    row, source, words = twelve_row(row_id)
    field = next(iter(fixed))
    line = recheck_row("2026-10-29T00:00:00+00:00", {**row, **fixed}, source, words, field=field, before=row)
    return line["recheck"]


def by_name(checks):
    return {c["name"]: c for c in checks}


# ---------------------------------------------------------------- 1. re-check of a person's fix


def test_a_date_typed_as_printed_in_the_quote_passes_the_recheck():
    # e96: "... this afternoon, August 5, 2026, at 4:30 p.m. ...": the quote names no event word next to the date
    rc = recheck_of("e96", date_text="August 5, 2026")
    assert by_name(rc["checks"])["date_match"]["result"] == "pass"
    assert rc["result"] == "pass"


def test_the_day_may_be_typed_the_way_the_quote_prints_it():
    row, source, words = twelve_row("e96")
    got = {c.check: c for c in row_checks({**row, "date": "August 5, 2026"}, source, words, chosen=True)}
    assert got["date_match"].result == PASS


def test_a_today_row_is_rechecked_against_the_release_day():
    # e81 prints "today at 4:30 pm Eastern"; the release was filed 2026-10-28
    rc = recheck_of("e81", date="2026-10-28")
    assert by_name(rc["checks"])["date_match"]["result"] == "pass"
    # PIVOT: type interpretation is outside this mechanical re-check.
    assert rc["result"] == "pass" and by_name(rc["checks"])["type_match"]["result"] == "not_needed"
    wrong = recheck_of("e81", date="2026-10-29")
    assert by_name(wrong["checks"])["date_match"]["result"] == "fail"
    assert wrong["result"] == "fail"


def test_the_recheck_reason_is_the_new_checks_reason_not_the_old_sentence():
    rc = recheck_of("e81", date="2026-10-29")
    date_check = by_name(rc["checks"])["date_match"]
    assert date_check["reason"] in rc["reason"]
    assert "run again" not in rc["reason"]
    # a check that could not run says why itself
    rc = recheck_of("e10", quote="Comprehensive Phase 2b Program")        # not printed, and no box under it
    assert rc["result"] in ("fail", "could_not_run")
    first = next(c for c in rc["checks"] if c["result"] == rc["result"])
    assert first["reason"] in rc["reason"]


def test_a_literal_date_match_preserves_negation_for_review():
    q = "The launch is not expected in Q3 2027, and is now planned for Q4 2027."
    kw = dict(event_type="launch", title="Launch")
    assert event_checks.date_match("Q3 2027", "2027-07-01", q, "2027-09-30", chosen=True, **kw).result == PASS
    from tests.pivot_assertions import assert_proposal_held
    assert_proposal_held(q, "launch", date_text="Q3 2027", start="2027-07-01", end="2027-09-30")


def test_both_printed_continuation_dates_require_event_review():
    q = "The launch is not expected in Q3 2027, and is now planned for Q4 2027."
    kw = dict(event_type="launch", title="Launch")
    assert event_checks.date_match("Q4 2027", "2027-10-01", q, "2027-12-31", chosen=True, **kw).result == PASS
    assert event_checks.date_match("Q3 2027", "2027-07-01", q, "2027-09-30", chosen=True, **kw).result == PASS


def test_the_old_value_is_still_judged_without_the_person_choice():
    row, source, words = twelve_row("e82")        # a replay date in a call-in sentence, event type unknown
    got = {c.check: c for c in row_checks(row, source, words)}
    assert got["date_match"].result == PASS  # literal support does not depend on a person choice
    got = {c.check: c for c in row_checks(row, source, words, chosen=True)}
    assert got["date_match"].result == PASS


# ---------------------------------------------------------------- 2. calendar gate


@pytest.fixture
def client(tmp_path):
    d = tmp_path / "runs"
    shutil.copytree(RUNS / "sample", d / "sample")
    return TestClient(create_app(runs_dir=d, ui_dir=d / "no-ui"))


def decide(client, target, action, fix=None, note=None):
    r = client.post("/api/runs/sample/decisions", json={"target": target, "action": action, "note": note,
                                                         "fix": fix})
    assert r.status_code == 200, r.text
    return r.json()


def on_calendar(client):
    return {e["id"] for e in client.get("/api/runs/sample/calendar").json()["entries"]}


def test_a_corrected_row_requires_keep_after_its_recheck_passes(client):
    # FIX-THEN-KEEP-001: pass/fix is not publication; explicit Keep is.
    # CALENDAR-RULE-001: clean, e1 is listed as placed automatically, never as kept
    assert all(e["inclusion"] != "kept" for e in client.get("/api/runs/sample/calendar").json()["entries"])
    out = decide(client, "e1", "fix", {"field": "date", "from": "2027-03-12", "to": "March 12, 2027"})
    assert out["written"][1]["recheck"]["result"] == "pass"
    assert out["state"]["group"] == "corrected"
    assert not out["state"]["accepted"] and out["state"]["inclusion"] != "kept"
    assert out["state"]["decision_label"] == "Fixed by you — not yet accepted"
    decide(client, "e1", "keep")
    entries = client.get("/api/runs/sample/calendar").json()["entries"]
    assert [e["inclusion"] for e in entries if e["id"] == "e1"] == ["kept"]


def test_a_fixed_but_still_open_row_is_not_on_the_calendar(client):
    decide(client, "e1", "keep")  # fresh acceptance before the invalidating edit
    assert "e1" in on_calendar(client)
    # a failing re-check: the day no longer matches the quote
    decide(client, "e1", "fix", {"field": "date", "from": "2027-03-12", "to": "2027-03-13"})
    assert "e1" not in on_calendar(client)
    # kept afterwards by the person: it is on the calendar again
    decide(client, "e1", "keep")
    assert "e1" in on_calendar(client)


def test_a_fix_whose_recheck_could_not_run_or_whose_readers_are_undecided_stays_off(client):
    # e8: the readers disagree; a fix of the title is no answer to that, so it stays undecided
    decide(client, "e8", "fix", {"field": "title", "from": client.get("/api/runs/sample").json()["state"]
                                 ["targets"]["e8"]["values"]["title"], "to": "A new title"})
    state = client.get("/api/runs/sample").json()["state"]["targets"]["e8"]
    assert state["group"] == "corrected" and state["status"] == "undecided"
    assert "e8" not in on_calendar(client)
    decide(client, "e8", "keep")
    assert "e8" in on_calendar(client)


def test_a_row_corrected_by_code_stays_off_calendar_until_human_acceptance():
    run = STORE.run("sample")
    row = next(row for row in run["rows"] if row["id"] == "e3")
    state = current_state(run, [])
    target = state["targets"][row["id"]]
    assert target["group"] == "corrected" and target["status"] == "matched"
    # GROUP-AXIS-001: even a demonstrated code correction is not acceptance.
    # CALENDAR-RULE-001: clean, it is placed automatically — listed as such, never as kept
    assert not target["decided_by_person"] and target["inclusion"] != "kept"
    assert not target["needs_attention"] and not target["accepted"]
    assert [e["inclusion"] for e in calendar_entries(run, state) if e["id"] == row["id"]] == [target["inclusion"]]

    pending = current_state(run, [{"by": "person", "target": row["id"], "action": "fix",
                                   "fix": {"field": "title", "from": row["title"], "to": row["title"] + " revised"},
                                   "at": "t"}])
    pending_target = pending["targets"][row["id"]]
    assert pending_target["needs_recheck"] and not state_on_calendar(pending_target)
    assert row["id"] not in [entry["id"] for entry in calendar_entries(run, pending)]


# ---------------------------------------------------------------- 3. check source


def test_check_source_returns_the_function_text(client):
    r = client.get("/api/checks/source", params={"ref": "checks/event_checks.py:date_match"})
    assert r.status_code == 200
    body = r.json()
    assert body["ref"] == "checks/event_checks.py:date_match"
    assert body["source"].startswith("def date_match(")
    r = client.get("/api/checks/source", params={"ref": "checks/selector_match.py:selector_match"})
    assert r.status_code == 200 and "def selector_match(" in r.json()["source"]


@pytest.mark.parametrize("ref", ["", "server/app.py:create_app", "runs/state.py:current_state",
                                 "checks/../server/app.py:create_app", "../checks/event_checks.py:date_match",
                                 "checks/event_checks.py:nothing_here", "checks/missing.py:date_match",
                                 "checks/event_checks.py:re", "checks/event_checks.py", "/etc/passwd"])
def test_check_source_refuses_anything_outside_checks(client, ref):
    r = client.get("/api/checks/source", params={"ref": ref})
    assert r.status_code == 404
    assert r.json()["detail"].endswith(".") and "checks/" in r.json()["detail"]


def test_every_code_ref_in_the_committed_runs_can_be_shown(client):
    refs = {(c["code_ref"]["file"], c["code_ref"]["function"])
            for run_id in ("sample", "twelve-4") for r in STORE.run(run_id)["rows"] for c in r["checks"]}
    assert refs
    for f, fn in refs:
        assert client.get("/api/checks/source", params={"ref": f"{f}:{fn}"}).status_code == 200, (f, fn)


# ---------------------------------------------------------------- 4. boxes


def test_boxes_in_twelve_4_for_ionis_rows_are_the_bounds_of_their_quote_words():
    """The saved Ionis row boxes carry no offset: each equals the bounds of the words the quote is printed in."""
    from checks.common import normalize
    from checks.selector_match import _quote_run
    run = STORE.run("twelve-4")
    seen = 0
    for row in run["rows"]:
        if "-ions-" not in row["source_id"] or not row.get("box"):
            continue
        source = next(s for s in run["sources"] if s["id"] == row["source_id"])
        words = STORE.words_doc("twelve-4", source, row["page_number"])["words"]
        found = _quote_run(normalize(row["quote"]), words, row["box"])
        if not found:
            continue
        bounds = [min(w["x0"] for w in found), min(w["y0"] for w in found),
                  max(w["x1"] for w in found), max(w["y1"] for w in found)]
        assert all(abs(a - b) <= 0.5 for a, b in zip(row["box"], bounds)), row["id"]
        seen += 1
    assert seen >= 6


def test_a_hidden_text_layer_does_not_replace_the_readers_box():
    """The PTGX slides are pictures with their words stacked in one 0.6 pt strip; a box drawn there is not where the
    quote is printed, so the reader's own box is kept."""
    words = [{"id": f"w{i}", "text": t, "x0": 31.75 + i * 0.5, "y0": 399.3, "x1": 32.1 + i * 0.5, "y1": 399.86}
             for i, t in enumerate("Phase 1 initiation in Q1 ’27".split())]
    hint = [100.0, 170.0, 300.0, 190.0]
    assert build_entry.locate_box("Phase 1 initiation in Q1 ’27", hint, words) is None
    real = [{**w, "y0": 170.0, "y1": 182.0, "x0": 100 + 30 * i, "x1": 125 + 30 * i} for i, w in enumerate(words)]
    assert build_entry.locate_box("Phase 1 initiation in Q1 ’27", [0.0, 0.0, 5.0, 5.0], real) is not None


def test_the_twelve_4_slide_rows_that_were_given_a_sliver_box_are_flagged_by_the_citation_check():
    row, source, words = twelve_row("e27")
    assert row["box"][3] - row["box"][1] < 1.0, "the committed run keeps the sliver box it was saved with"
    check = selector_match(row["quote"], row["box"], words["words"], words["page_width"], words["page_height"],
                           quote_lines=True)
    assert check.result == COULD_NOT_RUN and check.reason == PICTURE_REASON


# ---------------------------------------------------------------- 5. picture, not text


PAGE = [{"id": f"w{i}", "text": t, "x0": 50 + 40 * i, "y0": 100, "x1": 85 + 40 * i, "y1": 112}
        for i, t in enumerate("Quarterly update from the company about nothing".split())]


def test_a_quote_not_in_the_text_with_no_text_near_the_box_is_a_picture():
    box = [50, 500, 400, 540]
    got = event_checks.quote_match("Phase 1 initiation is planned for Q1 2027.", PAGE, box)
    assert got.result == COULD_NOT_RUN and got.reason == PICTURE_REASON
    got = selector_match("Phase 1 initiation is planned for Q1 2027.", box, PAGE, 612, 792, quote_lines=True)
    assert got.result == COULD_NOT_RUN and got.reason == PICTURE_REASON


def test_a_quote_not_in_the_text_with_text_under_the_box_still_fails():
    box = [50, 98, 400, 114]
    got = event_checks.quote_match("Phase 1 initiation is planned for Q1 2027.", PAGE, box)
    assert got.result == FAIL
    assert selector_match("Phase 1 initiation is planned for Q1 2027.", box, PAGE, 612, 792,
                          quote_lines=True).result == FAIL


def test_a_quote_that_is_in_the_text_passes_whatever_the_box():
    got = event_checks.quote_match("Quarterly update from the company", PAGE, [50, 500, 400, 540])
    assert got.result == PASS


def test_the_recheck_of_a_quote_over_a_picture_could_not_run():
    row, source, words = twelve_row("e27")
    checks = row_checks({**row, "quote": "Feb ’27* is the date of a different sentence"}, source, words)
    assert checks[0].result == COULD_NOT_RUN and checks[0].reason == PICTURE_REASON


# ---------------------------------------------------------------- 6. Truist


def test_truist_date_is_read_as_written():
    row, source, words = twelve_row("e76")
    assert "Truist" in row["quote"] and row["date_text"] in row["quote"]
    got = {c.check: c for c in row_checks(row, source, words)}
    assert got["date_match"].result == PASS, got["date_match"].reason


def test_a_year_inside_a_name_is_not_the_date_of_the_event():
    q = "Truist 2026 BioPharma Symposium New York, NY November 12, 2026"
    assert [m[0] for m in event_checks.named_dates(q)] == ["November 12, 2026"]
    assert [m[0] for m in event_checks.named_dates("Data expected in 2026 Ph3")] == ["2026"]
    assert event_checks.date_match("November 12, 2026", "2026-11-12", q, event_type="conference",
                                   title="Truist 2026 BioPharma Symposium").result == PASS
    # the name's own year is still no stand-in for the whole of 2026
    assert event_checks.date_match("2026", "2026-01-01", q, "2026-12-31", event_type="conference").result == FAIL
