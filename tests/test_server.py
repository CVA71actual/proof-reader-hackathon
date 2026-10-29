"""R2: the decisions server. Every test works on a temp copy of runs/sample; the committed sample is never touched."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from server.app import create_app

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
SAMPLE = RUNS / "sample"
SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "decision.schema.json").read_text()))

E7_PRINTED = "Samplebio Six will report final results on July 19, 2027."


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(folder.rglob("*")):
        if f.is_file():
            h.update(f.relative_to(folder).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def committed_digest():
    return _digest(SAMPLE)


@pytest.fixture
def runs_dir(tmp_path, committed_digest):
    d = tmp_path / "runs"
    shutil.copytree(SAMPLE, d / "sample")
    shutil.copytree(RUNS / "demo", d / "demo")
    yield d
    assert _digest(SAMPLE) == committed_digest, "the committed sample was modified"


@pytest.fixture
def client(runs_dir):
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui"))


def lines(runs_dir):
    return (runs_dir / "sample" / "decisions.jsonl").read_text().splitlines()


def post(client, target, action, note=None, fix=None, **extra):
    return client.post("/api/runs/sample/decisions",
                       json={"target": target, "action": action, "note": note, "fix": fix, **extra})


def fix(client, target, field, frm, to):
    return post(client, target, "fix", fix={"field": field, "from": frm, "to": to})


def state_of(client, target):
    return client.get("/api/runs/sample").json()["state"]["targets"][target]


# ---------------------------------------------------------------- reads


def test_list_runs(client):
    runs = {r["id"]: r for r in client.get("/api/runs").json()}
    assert set(runs) == {"demo", "sample"}
    s = runs["sample"]
    assert (s["number"], s["label"], s["sample"]) == (2, "Sample run", True)
    assert s["started_at"] and s["finished_at"]
    # counts come from the current state (the five example decisions applied), not from run.json
    assert s["counts"]["rows"] == {"kept": 5, "corrected": 3, "removed": 2, "unverified": 1, "open": 0}
    assert s["counts"]["excluded_by_person"] == 1
    assert runs["demo"]["counts"]["cells"]["kept"] == 69


def test_get_run_with_state(client):
    body = client.get("/api/runs/sample").json()
    assert body["run"]["id"] == "sample"
    assert len(body["rows"]) == 11
    st = body["state"]
    assert st["targets"]["e8"]["group"] == "kept" and st["targets"]["e8"]["decided_by_person"]
    assert st["targets"]["e10"]["group"] == "removed"
    assert st["ignored"] == []


def test_get_decisions(client, runs_dir):
    got = client.get("/api/runs/sample/decisions").json()
    assert got == [json.loads(x) for x in lines(runs_dir)]


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/..").status_code == 404
    assert post(client, "e1", "keep").status_code == 200
    assert client.post("/api/runs/nope/decisions", json={"target": "e1", "action": "keep"}).status_code == 404


def test_page_images_are_served(client):
    r = client.get("/runs/sample/pages/p95.png")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/runs/sample/pages/p95.words.json").json()["words"]
    assert client.get("/runs/sample/pages/missing.png").status_code == 404
    assert client.get("/runs/sample/pages/..%2Frun.json").status_code == 404


def test_ui_folder_is_served_when_present(runs_dir, tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<p>screens</p>")
    c = TestClient(create_app(runs_dir=runs_dir, ui_dir=ui))
    assert c.get("/").text == "<p>screens</p>"
    assert c.get("/api/runs").status_code == 200


# ---------------------------------------------------------------- decisions


def test_keep(client, runs_dir):
    # e9 could not be read (a scan); keeping it moves the group, never the status
    before = state_of(client, "e9")
    assert before["group"] == "unverified"
    n = len(lines(runs_dir))
    r = post(client, "e9", "keep", note="I read the scan myself.")
    assert r.status_code == 200
    st = r.json()["state"]
    assert st["group"] == "kept" and st["status"] == before["status"] == "unreadable"
    assert st["reason"] == "I read the scan myself."
    assert r.json()["unresolved"] == 0
    assert st["decided_by_person"] and st["last_action"] == "keep"
    assert len(lines(runs_dir)) == n + 1
    line = json.loads(lines(runs_dir)[-1])
    assert line["by"] == "person" and line["at"].endswith("+00:00")
    assert list(SCHEMA.iter_errors(line)) == []


def test_fix_with_wrong_from_is_refused(client, runs_dir):
    n = len(lines(runs_dir))
    r = fix(client, "e3", "date", "2027-06-03", "2027-06-29")
    assert r.status_code == 422
    assert r.json()["detail"] == "The fix starts from '2027-06-03' but the current value is '2027-06-30'."
    assert len(lines(runs_dir)) == n


def test_fix_the_quote_supports_is_rechecked_and_counted(client, runs_dir):
    # e7's reader misread "July 19" as "July 9". A person fixes the quote, then the date words, then the day.
    person0 = client.get("/api/runs/sample").json()["state"]["mistakes"]["person"]
    n = len(lines(runs_dir))
    r1 = fix(client, "e7", "quote", "Samplebio Six will report final results on July 9, 2027.", E7_PRINTED)
    r2 = fix(client, "e7", "date_text", "July 9, 2027", "July 19, 2027")
    assert r1.json()["state"]["recheck"]["result"] == "fail"   # the date words are still "July 9"
    assert r2.json()["state"]["recheck"]["result"] == "fail"   # the day is still the 9th
    assert r2.json()["mistakes"]["person"] == person0          # a fix that failed its re-check is not counted
    r3 = fix(client, "e7", "date", "2027-07-09", "2027-07-19")
    assert r3.status_code == 200
    body = r3.json()
    st = body["state"]
    assert st["group"] == "corrected" and st["status"] == "matched" and not st["needs_recheck"]
    assert st["recheck"]["result"] == "pass"
    assert {c["name"]: c["result"] for c in st["recheck"]["checks"]} == {
        "quote_match": "pass", "selector_match": "pass", "date_match": "pass", "source_rule": "pass",
        "type_match": "pass"}
    assert body["mistakes"]["person"] == person0 + 1
    assert body["counts"]["fixes_pending"] == 0
    assert len(lines(runs_dir)) == n + 6                       # each fix: the person's line + code's re-check
    last = [json.loads(x) for x in lines(runs_dir)[-2:]]
    assert [d["action"] for d in last] == ["fix", "recheck"]
    assert last[1]["by"] == "code" and last[1]["recheck"]["result"] == "pass"
    for d in last:
        assert list(SCHEMA.iter_errors(d)) == []
    for c in last[1]["recheck"]["checks"]:
        assert c["code_ref"]["file"].startswith("checks/")


def test_fix_that_fails_its_recheck_goes_back_to_open(client, runs_dir):
    person0 = client.get("/api/runs/sample").json()["state"]["mistakes"]["person"]
    n = len(lines(runs_dir))
    r = fix(client, "e3", "date", "2027-06-30", "2027-07-30")   # the quote says June 30
    assert r.status_code == 200
    st = r.json()["state"]
    assert st["group"] == "open" and st["status"] == "failed"
    assert st["recheck"]["result"] == "fail"
    assert {c["name"]: c["result"] for c in st["recheck"]["checks"]}["date_match"] == "fail"
    assert r.json()["mistakes"]["person"] == person0
    assert len(lines(runs_dir)) == n + 2


def test_fix_whose_recheck_could_not_run_stays_corrected_and_needs_attention(client, runs_dir):
    # e10 has no box: the citation check has nothing to compare, so the re-check could not run. Not a failure.
    s0 = client.get("/api/runs/sample").json()["state"]
    before = s0["targets"]["e10"]
    r = fix(client, "e10", "title", "Full-year 2025 results in the annual report", "Full-year 2025 results")
    assert r.status_code == 200
    body = r.json()
    st = body["state"]
    assert st["group"] == "corrected" and st["status"] == "proposed" and st["decided_by_person"]
    assert st["recheck"]["result"] == "could_not_run" and not st["needs_recheck"]
    assert st["reason"] == "No check compares title; the fix is recorded but not demonstrated."
    assert {c["name"]: c["result"] for c in st["recheck"]["checks"]}["selector_match"] == "could_not_run"
    assert body["mistakes"] == s0["mistakes"]                       # not a person mistake
    assert body["counts"]["fixes_pending"] == s0["counts"]["fixes_pending"] + 1
    assert body["unresolved"] == s0["unresolved"] + 1               # e10 was removed; now it needs attention
    rc_line = json.loads(lines(runs_dir)[-1])
    assert rc_line["recheck"]["result"] == "could_not_run" and list(SCHEMA.iter_errors(rc_line)) == []
    u = post(client, "e10", "undo").json()
    for k in ("group", "status", "values", "recheck", "needs_recheck"):
        assert u["state"][k] == before[k], k
    assert u["counts"] == s0["counts"] and u["mistakes"] == s0["mistakes"] and u["unresolved"] == s0["unresolved"]


def test_fix_settles_the_readers_disagreement(client, runs_dir):
    # e8: the two readers named different event types (undecided). The person's fix, re-checked as pass, settles it.
    # No check compares event_type, so the same checks also pass on the old value: the choice settles the
    # disagreement but does not show the old value was wrong, so it is not counted as a mistake.
    s0 = client.get("/api/runs/sample").json()["state"]
    assert s0["targets"]["e8"]["status"] == "undecided"
    r = fix(client, "e8", "event_type", "regulatory_decision", "regulatory_submission")
    st = r.json()["state"]
    assert st["group"] == "corrected" and st["status"] == "matched"
    assert st["recheck"]["result"] == "pass"
    assert st["recheck"]["reason"].startswith("Settles the readers' disagreement by the person's fix. ")
    assert st["recheck"]["before"]["result"] == "pass" and st["recheck"]["after"]["result"] == "pass"
    assert st["reason"] == st["recheck"]["reason"] + " The value already passed its checks before the edit."
    assert r.json()["mistakes"]["person"] == s0["mistakes"]["person"]
    assert r.json()["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"] + 1
    assert r.json()["unresolved"] == s0["unresolved"]
    assert st["fixes"][-1]["settles"] is True


def test_period_fix_is_checked_as_a_period(client):
    # e2 is "Q3 2027": the period must stay 2027-07-01 to 2027-09-30
    r = fix(client, "e2", "date_end", "2027-09-30", "2027-12-31")
    assert r.json()["state"]["group"] == "open"
    dm = {c["name"]: c for c in r.json()["state"]["recheck"]["checks"]}["date_match"]
    assert dm["result"] == "fail" and "period" in dm["reason"]


def test_table_value_fix_is_rechecked_with_the_table_checks(client, runs_dir):
    n = len(lines(runs_dir))
    bad = fix(client, "t1.0.0", "value", "115,315", "115,316")
    assert bad.json()["state"]["group"] == "open"
    assert {c["name"] for c in bad.json()["state"]["recheck"]["checks"]} == {"text_layer_match", "selector_match",
                                                                         "row_column_match"}
    good = fix(client, "t1.0.0", "value", "115,316", "115,315")
    assert good.json()["state"]["group"] == "corrected" and good.json()["state"]["status"] == "matched"
    assert len(lines(runs_dir)) == n + 4


def test_remove(client, runs_dir):
    n = len(lines(runs_dir))
    excluded0 = client.get("/api/runs/sample").json()["state"]["counts"]["excluded_by_person"]
    r = post(client, "e1", "remove", note="Not an upcoming event.")
    body = r.json()
    assert body["state"]["group"] == "removed"
    assert body["counts"]["excluded_by_person"] == excluded0 + 1
    assert {"target": "e1", "rule": "person_removed", "reason": "Not an upcoming event.", "by": "person"} \
        in body["exclusions"]
    assert body["mistakes"] == client.get("/api/runs/sample").json()["state"]["mistakes"]
    assert len(lines(runs_dir)) == n + 1


def test_undo_takes_back_the_fix_and_its_recheck(client, runs_dir):
    before = state_of(client, "e3")
    summary0 = client.get("/api/runs/sample").json()["state"]
    fix(client, "e3", "date", "2027-06-30", "2027-07-30")
    n = len(lines(runs_dir))
    r = post(client, "e3", "undo")
    assert r.status_code == 200
    assert len(lines(runs_dir)) == n + 1                    # undo is one new line; nothing is edited
    st = r.json()["state"]
    for k in ("group", "status", "values", "recheck", "needs_recheck", "decided_by_person"):
        assert st[k] == before[k], k
    assert st["recheck"] is None
    assert r.json()["counts"] == summary0["counts"] and r.json()["mistakes"] == summary0["mistakes"]


def test_undo_with_nothing_to_undo_is_refused(client, runs_dir):
    n = len(lines(runs_dir))
    r = post(client, "e1", "undo")
    assert r.status_code == 422 and r.json()["detail"] == "There is nothing to undo for this target."
    assert len(lines(runs_dir)) == n


def test_the_file_only_grows(client, runs_dir):
    first = lines(runs_dir)
    post(client, "e1", "keep")
    fix(client, "e3", "date", "2027-06-30", "2027-07-30")
    post(client, "e3", "undo")
    post(client, "e9", "nonsense")
    now = lines(runs_dir)
    assert now[: len(first)] == first
    assert len(now) == len(first) + 4


@pytest.mark.parametrize("body, reason", [
    ({"target": "e1", "action": "keep", "by": "model"}, "Only a person's decisions are taken here."),
    ({"target": "e1", "action": "recheck", "recheck": {"result": "pass"}}, None),
    ({"target": "e1", "action": "recheck"}, "Only code writes a re-check."),
    ({"target": "e1", "action": "delete"}, "The action must be keep, fix, remove or undo."),
    ({"target": "zz", "action": "keep"}, "No row or table value has the id 'zz'."),
    ({"target": "e1", "action": "fix", "fix": None}, "A fix needs a field, a from value and a to value (text or null)."),
    ({"target": "e1", "action": "fix", "fix": {"field": "title", "from": "Nope", "to": "X"}},
     "The fix starts from 'Nope' but the current value is 'Phase 3 STAR-1 topline results'."),
    ({"target": "e1", "action": "fix", "fix": {"field": "box", "from": None, "to": "1"}},
     "The field 'box' cannot be fixed here."),
    ({"target": "e1", "action": "keep", "fix": {"field": "title", "from": "a", "to": "b"}},
     "Only a fix carries a fix; a keep does not."),
])
def test_refused_decisions_write_nothing(client, runs_dir, body, reason):
    n = len(lines(runs_dir))
    r = client.post("/api/runs/sample/decisions", json=body)
    assert r.status_code == 422
    if reason:
        assert r.json()["detail"] == reason
    assert len(lines(runs_dir)) == n


def test_server_never_reads_the_key(monkeypatch, runs_dir):
    import os
    monkeypatch.setenv("MODEL_API_KEY", "should-never-be-read")
    src = (REPO / "server" / "app.py").read_text()
    assert "MODEL_API_KEY" not in src and "dotenv" not in src and ".env" not in src
    body = TestClient(create_app(runs_dir=runs_dir)).get("/api/runs/sample").text
    assert "should-never-be-read" not in body
    assert os.environ["MODEL_API_KEY"] == "should-never-be-read"


# ---------------------------------------------------------------- Codex PR #4 review regression cases

UNCOVERED_TITLE = "No check compares title; the fix is recorded but not demonstrated."
SETTLES = "Settles the readers' disagreement by the person's fix. "


def summary(client):
    return client.get("/api/runs/sample").json()["state"]


def test_codex1_title_only_fix_on_e8_leaves_the_disagreement_open(client):
    # e8's readers disagree on event_type. Changing only its title settles nothing and shows no mistake.
    s0 = summary(client)
    r = fix(client, "e8", "title", "Application filing", "Application filing date")
    assert r.status_code == 200
    body = r.json()
    st = body["state"]
    assert st["status"] == "undecided" and st["group"] == "corrected"
    assert st["recheck"]["result"] == "could_not_run"
    assert st["recheck"]["reason"] == UNCOVERED_TITLE and not st["recheck"]["reason"].startswith(SETTLES)
    assert st["fixes"][-1]["settles"] is False
    assert body["mistakes"] == s0["mistakes"]
    assert body["counts"]["fixes_pending"] == s0["counts"]["fixes_pending"] + 1
    assert body["unresolved"] == s0["unresolved"] + 1           # e8 was kept; now it needs attention again
    # the disputed field can still settle it afterwards
    r2 = fix(client, "e8", "event_type", "regulatory_decision", "regulatory_submission").json()
    assert r2["state"]["status"] == "matched" and r2["state"]["recheck"]["reason"].startswith(SETTLES)
    assert r2["mistakes"]["person"] == s0["mistakes"]["person"]         # settled, but no old error shown
    assert r2["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"] + 1
    assert r2["unresolved"] == s0["unresolved"]


def test_codex1_checked_field_fix_on_e8_does_not_settle_the_disagreement(client):
    # A date fix is checked, but the readers did not disagree on the date: the row stays undecided even after
    # a fix that passes its re-check (here after a first fix that failed), and it stays under Needs attention.
    s0 = summary(client)
    bad = fix(client, "e8", "date", "2027-08-02", "2027-08-03").json()["state"]
    assert (bad["group"], bad["status"]) == ("open", "failed")
    good = fix(client, "e8", "date", "2027-08-03", "2027-08-02").json()
    st = good["state"]
    assert st["recheck"]["result"] == "pass"
    assert (st["group"], st["status"]) == ("corrected", "undecided")
    assert st["recheck"]["reason"] == "The checks were run again on the fixed value and passed."
    assert good["unresolved"] == s0["unresolved"] + 1


def test_codex1_harmless_title_fix_on_matched_e1_is_not_a_mistake(client):
    s0 = summary(client)
    r = fix(client, "e1", "title", "Phase 3 STAR-1 topline results", "Phase 3 STAR-1 topline data").json()
    st = r["state"]
    assert st["recheck"]["result"] == "could_not_run" and st["recheck"]["reason"] == UNCOVERED_TITLE
    assert st["status"] == "matched"                         # the checks still say what they said
    assert r["mistakes"] == s0["mistakes"]
    assert r["counts"]["fixes_pending"] == s0["counts"]["fixes_pending"] + 1


def test_codex1_resolving_the_disputed_field_records_the_person_apart_from_the_checks(client, runs_dir):
    run = json.loads((runs_dir / "sample" / "run.json").read_text())
    e8 = next(r for r in run["rows"] if r["id"] == "e8")
    assert e8["disagreement"] == {"field": "event_type", "first_reader": "regulatory_decision",
                                  "second_reader": "regulatory_submission"}
    fix(client, "e8", "event_type", "regulatory_decision", "regulatory_submission")
    fx, rc = [json.loads(x) for x in lines(runs_dir)[-2:]]
    assert (fx["action"], fx["by"], fx["fix"]["field"]) == ("fix", "person", "event_type")   # the person's judgement
    assert (rc["action"], rc["by"]) == ("recheck", "code")                                 # what code checked
    assert rc["recheck"]["reason"].startswith(SETTLES)
    assert {c["name"] for c in rc["recheck"]["checks"]} == {"quote_match", "selector_match", "date_match",
                                                             "source_rule", "type_match"}
    assert all(c["kind"] == "Code" for c in rc["recheck"]["checks"])


def test_codex2_second_fix_from_the_same_old_value_is_refused(client, runs_dir):
    n = len(lines(runs_dir))
    a = fix(client, "e1", "title", "Phase 3 STAR-1 topline results", "Topline results A")
    b = fix(client, "e1", "title", "Phase 3 STAR-1 topline results", "Topline results B")
    assert a.status_code == 200 and b.status_code == 422
    assert b.json()["detail"] == ("The fix starts from 'Phase 3 STAR-1 topline results' "
                                  "but the current value is 'Topline results A'.")
    assert len(lines(runs_dir)) == n + 2


def test_codex2_two_simultaneous_fixes_from_the_same_value_only_one_is_taken(client, runs_dir, monkeypatch):
    import threading
    import time

    import server.app as server_app
    real = server_app.recheck_row

    def slow_recheck(*a, **k):          # widen the window between reading the log and appending to it
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(server_app, "recheck_row", slow_recheck)
    n = len(lines(runs_dir))
    start = threading.Barrier(2)
    results = {}

    def go(to):
        start.wait()
        results[to] = fix(client, "e1", "title", "Phase 3 STAR-1 topline results", to)

    threads = [threading.Thread(target=go, args=(to,)) for to in ("Topline A", "Topline B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 422]
    new = [json.loads(x) for x in lines(runs_dir)[n:]]
    assert [d["action"] for d in new] == ["fix", "recheck"]
    won = next(r for r in results.values() if r.status_code == 200).json()
    assert new[0]["fix"]["to"] == won["state"]["values"]["title"]
    committed = summary(client)
    assert committed["ignored"] == []                       # no accepted decision is dropped on replay
    assert committed["targets"]["e1"] == won["state"]       # the accepted answer is the committed state
    assert committed["unresolved"] == won["unresolved"] and committed["counts"] == won["counts"]


def _move_page(runs_dir, *, row=None, table=None, page=None):
    f = runs_dir / "sample" / "run.json"
    run = json.loads(f.read_text())
    for r in run["rows"]:
        if r["id"] == row:
            r["page_number"] = page
    for t in run["tables"]:
        if t["id"] == table:
            t["page_number"] = page
    f.write_text(json.dumps(run))


def test_codex3_a_missing_cited_page_is_never_replaced_by_another(client, runs_dir):
    # e8's source has only page 1. If e8 cites page 2, the page checks could not run; page 1 is not used.
    _move_page(runs_dir, row="e8", page=2)
    s0 = summary(client)
    r = fix(client, "e8", "event_type", "regulatory_decision", "regulatory_submission").json()
    st = r["state"]
    assert st["recheck"]["result"] == "could_not_run"
    assert st["recheck"]["reason"] == "The re-check could not run: Page 2 was not captured."
    by = {c["name"]: c for c in st["recheck"]["checks"]}
    for name in ("quote_match", "selector_match"):
        assert by[name]["result"] == "could_not_run" and by[name]["reason"] == "Page 2 was not captured."
    assert st["status"] != "matched"
    assert r["mistakes"] == s0["mistakes"]


def test_codex3_a_table_value_on_a_missing_page_could_not_run(client, runs_dir):
    _move_page(runs_dir, table="t1", page=96)
    r = fix(client, "t1.0.0", "value", "115,315", "115,316").json()["state"]
    assert r["recheck"]["result"] == "could_not_run"
    assert r["recheck"]["reason"] == "The re-check could not run: Page 96 was not captured."
    assert {c["result"] for c in r["recheck"]["checks"]} == {"could_not_run"}


def test_codex4_fix_e10_then_remove_then_undo(client):
    s0 = summary(client)                                    # e10 starts removed by the person
    assert s0["targets"]["e10"]["group"] == "removed"
    # 1. fix: the re-check could not run; e10 is back under Needs attention and pending, not excluded
    f = fix(client, "e10", "title", "Full-year 2025 results in the annual report", "Full-year 2025 results").json()
    assert f["state"]["recheck"]["result"] == "could_not_run"
    assert f["unresolved"] == s0["unresolved"] + 1
    assert f["counts"]["fixes_pending"] == s0["counts"]["fixes_pending"] + 1
    assert f["counts"]["excluded_by_person"] == s0["counts"]["excluded_by_person"] - 1
    # 2. remove: excluded again, out of Needs attention and out of pending; the unrun check stays visible
    rm = post(client, "e10", "remove", note="Not an upcoming event.").json()
    assert rm["state"]["group"] == "removed"
    assert rm["counts"]["excluded_by_person"] == s0["counts"]["excluded_by_person"]
    assert rm["unresolved"] == s0["unresolved"]
    assert rm["counts"]["fixes_pending"] == s0["counts"]["fixes_pending"]
    assert rm["state"]["recheck"] == f["state"]["recheck"]
    assert rm["state"]["fixes"][-1]["recheck"]["result"] == "could_not_run"
    # 3. undo the remove: both come back
    u = post(client, "e10", "undo").json()
    assert u["state"]["group"] == "corrected"
    assert u["unresolved"] == f["unresolved"]
    assert u["counts"]["fixes_pending"] == f["counts"]["fixes_pending"]
    assert u["counts"]["excluded_by_person"] == f["counts"]["excluded_by_person"]
    assert u["state"]["recheck"] == f["state"]["recheck"]


def test_codex4_keep_after_a_fix_that_could_not_run_clears_needs_attention(client):
    s0 = summary(client)
    f = fix(client, "e10", "title", "Full-year 2025 results in the annual report", "Full-year 2025 results").json()
    k = post(client, "e10", "keep", note="I checked it.").json()
    assert k["state"]["group"] == "kept"
    assert k["unresolved"] == f["unresolved"] - 1 == s0["unresolved"]
    assert k["counts"]["fixes_pending"] == f["counts"]["fixes_pending"]   # the fix itself is still not shown
    assert k["state"]["recheck"]["result"] == "could_not_run"              # and its check stays visible


# ---------------------------------------------------------------- Codex PR4 re-review: a demonstrated mistake

NO_ERROR = "The value already passed its checks before the edit."


def test_codex_dollar_sign_edit_on_a_matched_value_is_not_a_mistake(client, runs_dir):
    # t1.0.0 is matched at "115,315". "$115,315" reads the same to the checks: both pass before and after.
    s0 = summary(client)
    r = fix(client, "t1.0.0", "value", "115,315", "$115,315")
    assert r.status_code == 200
    body = r.json()
    st = body["state"]
    assert st["group"] == "corrected" and st["status"] == "matched"     # the edit is recorded and stands
    rc = st["recheck"]
    assert rc["result"] == "pass" and rc["before"]["result"] == "pass" and rc["after"]["result"] == "pass"
    assert rc["after"]["checks"] == rc["checks"]
    assert body["mistakes"] == s0["mistakes"]
    assert body["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"] + 1 == 1
    f = st["fixes"][-1]
    assert f["mistake"] is False and f["edit_without_error"] is True and f["not_counted_reason"] == NO_ERROR
    assert st["reason"].endswith(NO_ERROR)
    written = [json.loads(x) for x in lines(runs_dir)[-2:]]
    for line in written:
        assert not list(SCHEMA.iter_errors(line))
    assert written[1]["recheck"]["before"]["result"] == "pass"
    # changing it back is again an edit without an error, never a mistake
    back = fix(client, "t1.0.0", "value", "$115,315", "115,315").json()
    assert back["mistakes"] == s0["mistakes"] and back["counts"]["edits_without_error"] == 2


def test_codex_whitespace_only_edit_is_not_a_mistake(client):
    s0 = summary(client)
    body = fix(client, "t1.1.0", "value", "285,529", " 285,529 ").json()
    assert body["state"]["recheck"]["result"] == "pass"
    assert body["state"]["recheck"]["before"]["result"] == "pass"
    assert body["mistakes"] == s0["mistakes"]
    assert body["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"] + 1


def test_a_demonstrably_wrong_value_fixed_is_a_mistake(client):
    # e7's date words and day were misread; with the quote and words fixed, the old day fails and the new passes.
    fix(client, "e7", "quote", "Samplebio Six will report final results on July 9, 2027.", E7_PRINTED)
    fix(client, "e7", "date_text", "July 9, 2027", "July 19, 2027")
    s0 = summary(client)
    body = fix(client, "e7", "date", "2027-07-09", "2027-07-19").json()
    rc = body["state"]["recheck"]
    assert rc["before"]["result"] == "fail" and rc["after"]["result"] == "pass"
    f = body["state"]["fixes"][-1]
    assert f["mistake"] is True and f["before_undemonstrated"] is False
    assert body["mistakes"]["person"] == s0["mistakes"]["person"] + 1
    assert body["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"]


def test_undo_takes_back_only_what_the_fix_counted(client):
    s0 = summary(client)
    fix(client, "t1.0.0", "value", "115,315", "$115,315")
    u = post(client, "t1.0.0", "undo").json()
    assert u["mistakes"] == s0["mistakes"] and u["counts"] == s0["counts"]
    fix(client, "e7", "quote", "Samplebio Six will report final results on July 9, 2027.", E7_PRINTED)
    fix(client, "e7", "date_text", "July 9, 2027", "July 19, 2027")
    mid = summary(client)
    counted = fix(client, "e7", "date", "2027-07-09", "2027-07-19").json()
    assert counted["mistakes"]["person"] == mid["mistakes"]["person"] + 1
    u2 = post(client, "e7", "undo").json()
    assert u2["mistakes"] == mid["mistakes"] and u2["counts"] == mid["counts"]


# ---------------------------------------------------------------- Codex PR9 review: page hashes checked at load


@pytest.mark.parametrize("name", ["p95.png", "p95.words.json"])
def test_codex9_a_changed_page_file_refuses_the_run(runs_dir, client, name):
    f = runs_dir / "sample" / "pages" / name
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 1
    f.write_bytes(bytes(data))
    want = f"This run is not served: its file pages/{name} does not match the hash recorded in run.json."
    r = client.get("/api/runs/sample")
    assert r.status_code == 409 and r.json()["detail"] == want
    for other in ("p95.png", "p95.words.json", "smp-release-a-p1.png"):
        assert client.get(f"/runs/sample/pages/{other}").status_code == 409, other
    before = lines(runs_dir)
    assert post(client, "e8", "keep").status_code == 409
    assert lines(runs_dir) == before                              # nothing written to a refused run
    runs = {r["id"]: r for r in client.get("/api/runs").json()}
    assert runs["sample"] == {"id": "sample", "refused": want}
    assert client.get("/api/runs/demo").status_code == 200        # the untouched run is still served


def test_codex9_a_missing_page_file_refuses_the_run(runs_dir, client):
    (runs_dir / "sample" / "pages" / "smp-filing-b-p1.words.json").unlink()
    r = client.get("/api/runs/sample")
    assert r.status_code == 409
    assert r.json()["detail"] == "This run is not served: its file pages/smp-filing-b-p1.words.json is missing."


def test_codex9_a_page_file_not_named_in_the_run_is_not_served(runs_dir, client):
    (runs_dir / "sample" / "pages" / "extra.png").write_bytes(b"not in run.json")
    assert client.get("/runs/sample/pages/extra.png").status_code == 404
    assert client.get("/runs/sample/pages/p95.png").status_code == 200


def test_codex9_a_page_changed_after_load_is_not_served(runs_dir, client, monkeypatch):
    import server.app as server_app
    real = server_app.verify_page_files

    def verify_then_change(run_dir, run):
        real(run_dir, run)
        (run_dir / "pages" / "p95.png").write_bytes(b"changed after the run was checked")

    monkeypatch.setattr(server_app, "verify_page_files", verify_then_change)
    r = client.get("/runs/sample/pages/p95.png")
    assert r.status_code == 409
    assert "pages/p95.png does not match" in r.json()["detail"]


# ---------------------------------------------------------------- Codex PR9 fix 0fa435e: the words a re-check reads are the words hashed

E7_WORDS = "pages/smp-release-e-p1.words.json"   # the page e7's fix is re-checked against
E7_FIX = {"field": "quote", "from": "Samplebio Six will report final results on July 9, 2027.", "to": E7_PRINTED}


def _e7_page(run):
    return next(p for s in run["sources"] for p in s.get("pages", []) if p.get("words") == E7_WORDS)


def _after_load(monkeypatch, change):
    """Let the load-time check pass, then change the page or its run entry before the re-check reads it."""
    import server.app as server_app
    real = server_app.verify_page_files

    def verify_then_change(run_dir, run):
        real(run_dir, run)
        change(run_dir, run)

    monkeypatch.setattr(server_app, "verify_page_files", verify_then_change)


def _refused_fix(client, runs_dir, detail):
    before = lines(runs_dir)
    r = post(client, "e7", "fix", fix=E7_FIX)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == detail
    assert lines(runs_dir) == before                      # no fix line, no re-check line


def test_codex9b_words_changed_after_load_refuses_the_fix(client, runs_dir, monkeypatch):
    def rewrite(run_dir, run):
        # Valid JSON with the same words: it would support the fix, but these bytes were never verified.
        f = run_dir / E7_WORDS
        f.write_text(json.dumps(json.loads(f.read_text()), indent=1))

    _after_load(monkeypatch, rewrite)
    _refused_fix(client, runs_dir,
                 f"This run is not served: its file {E7_WORDS} does not match the hash recorded in run.json.")


def test_codex9b_words_removed_after_load_refuses_the_fix(client, runs_dir, monkeypatch):
    _after_load(monkeypatch, lambda run_dir, run: (run_dir / E7_WORDS).unlink())
    _refused_fix(client, runs_dir, f"This run is not served: its file {E7_WORDS} is missing.")


def _outside(run_dir, run):
    # Same bytes and hash, but outside the run's folder: only the folder rule can refuse it.
    outside = run_dir.parent / "outside.words.json"
    outside.write_bytes((run_dir / E7_WORDS).read_bytes())
    _e7_page(run)["words"] = "../outside.words.json"


def test_codex9b_words_outside_the_run_folder_refuses_the_fix(client, runs_dir, monkeypatch):
    _after_load(monkeypatch, _outside)
    _refused_fix(client, runs_dir,
                 "This run is not served: its file ../outside.words.json is outside the run's folder.")


def test_codex9b_words_with_no_hash_refuses_the_fix(client, runs_dir, monkeypatch):
    _after_load(monkeypatch, lambda run_dir, run: _e7_page(run).pop("words_sha256"))
    _refused_fix(client, runs_dir, f"This run is not served: its file {E7_WORDS} has no hash recorded in run.json.")


@pytest.mark.parametrize("change, detail", [
    (_outside, "This run is not served: its file ../outside.words.json is outside the run's folder."),
    (lambda run_dir, run: _e7_page(run).pop("words_sha256"),
     f"This run is not served: its file {E7_WORDS} has no hash recorded in run.json."),
], ids=["outside", "no-hash"])
def test_codex9b_the_same_run_json_on_disk_is_refused_at_load(client, runs_dir, change, detail):
    f = runs_dir / "sample" / "run.json"
    run = json.loads(f.read_text())
    change(runs_dir / "sample", run)
    f.write_text(json.dumps(run, indent=1))
    _refused_fix(client, runs_dir, detail)


def test_codex9b_unchanged_words_still_take_the_fix(client, runs_dir, monkeypatch):
    _after_load(monkeypatch, lambda run_dir, run: None)
    n = len(lines(runs_dir))
    r = post(client, "e7", "fix", fix=E7_FIX)
    assert r.status_code == 200, r.text
    assert len(lines(runs_dir)) == n + 2
