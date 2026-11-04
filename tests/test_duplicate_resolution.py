"""DUPLICATE-RESOLUTION-001 (CVA 2026-11-03, run 9 e10/e28: never silently two accepted copies of one event).

The rule lives in runs/state.py (_resolve_duplicates / duplicate_pair); the server's decision path refuses a plain
Keep of the second copy while the first is on the calendar and takes same_event / distinct_events. Synthetic data,
plus a read-only replay of the saved run 9 when it is on this machine (skipped elsewhere).
"""
import copy
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runs.calendar import run_interval_partition
from runs.state import current_state, duplicate_pair
from server.app import create_app
from tests.test_duplicate_rows import TITLE, combine, keep, truist
from tests.test_pivot_proposal_flow import CASES, make_run

REFUSED = ("Same event as e1, already on the calendar. Choose: it is the same event (add this as a second source "
           "of e1) or they are different events.")


def _serve(tmp_path, run):
    """Write `run` (rows all citing the one synthetic page) as runs/synthetic and serve it."""
    make_run(CASES[0], tmp_path / "synthetic")          # writes the synthetic words file the rows' checks bind to
    words = json.loads((tmp_path / "synthetic" / "synthetic.words.json").read_text())
    quote = run["rows"][0]["quote"]
    words["words"] = []
    for i, token in enumerate(quote.split()):
        x, y = 20 + (i % 6) * 95, 30 + (i // 6) * 16
        words["words"].append({"id": f"w{i}", "text": token, "x0": x, "y0": y, "x1": x + min(90, len(token) * 5),
                               "y1": y + 12, "block": 0, "line": i // 6, "word": i % 6})
    assert hashlib.sha256(json.dumps(words).encode()).hexdigest() == run["sources"][0]["pages"][0]["words_sha256"]
    (tmp_path / "synthetic" / "synthetic.words.json").write_text(json.dumps(words))
    (tmp_path / "synthetic" / "run.json").write_text(json.dumps(run))
    return TestClient(create_app(runs_dir=tmp_path)), "/api/runs/synthetic", tmp_path / "synthetic"


def _targets(client, root):
    return client.get(root).json()["state"]["targets"]


def _calendar_ids(client, root):
    return [e["id"] for e in client.get(root + "/calendar").json()["entries"]]


def _log(directory):
    path = directory / "decisions.jsonl"
    return path.read_text() if path.exists() else ""


# ---- the rule -------------------------------------------------------------------------------------------------

def _v(title, day="2026-11-12", **kw):
    return {"company": "Protagonist Therapeutics, Inc.", "date": day, "date_end": None, "title": title,
            "program": None, "indication": None, **kw}


def test_rule_pairs_same_named_meeting_across_wording():
    t = "Truist Securities 2026 Biopharma Symposium participation"
    assert duplicate_pair(_v(t), _v(t))
    assert duplicate_pair(_v(t), _v("Truist Biopharma Symposium fireside chat"))
    assert duplicate_pair(_v(t), _v("Protagonist to present at the Truist Securities Symposium"))
    assert duplicate_pair(_v("Rusfertide FDA approval decision (PDUFA)", program="rusfertide"),
                          _v("FDA approval of MIMRYLO (rusfertide)", program="MIMRYLO (rusfertide)"))


def test_rule_never_pairs_on_company_and_day_alone():
    wolfe, ubs = _v("Wolfe Research Healthcare Conference participation"), _v("UBS Global Biopharma Conference")
    assert not duplicate_pair(wolfe, ubs)
    assert not duplicate_pair(_v("Truist Securities Biopharma Symposium"), _v("Q3 2026 earnings call"))
    assert not duplicate_pair(_v("FDA approval of ICOTYDE", program="icotrokinra"),
                              _v("European Commission approval of ICOTYDE", program="icotrokinra"))
    assert not duplicate_pair(_v("PN-881 Phase 1 initiation", program="PN-881"),
                              _v("PN-881 Phase 2 initiation", program="PN-881"))
    assert not duplicate_pair(_v("Rusfertide launch", program="rusfertide"),
                              _v("Rusfertide approval", program="rusfertide"))
    t = "Truist Securities Biopharma Symposium"
    assert not duplicate_pair(_v(t), _v(t, day="2026-11-13"))                       # another day
    assert not duplicate_pair(_v(t), _v(t, company="Cedar Bio"))                     # another company
    assert not duplicate_pair(_v(t, date_end="2026-12-31"), _v(t, date_end="2026-12-31"))   # periods out of scope
    assert duplicate_pair(_v(t, date_end="2026-11-12"), _v(t))                       # a one-day period is a day


def test_same_date_distinct_events_not_paired_in_state(tmp_path):
    wolfe = truist(title="Wolfe Research Healthcare Conference participation")
    ubs = truist(title="UBS Global Biopharma Conference participation")
    earnings = truist(title="Third quarter 2026 earnings call")
    state = current_state(combine(wolfe, ubs, earnings), [])
    for t in ("e1", "e2", "e3"):
        s = state["targets"][t]
        assert s["inclusion"] == "included" and s["duplicate_of"] is None and s["duplicate_state"] is None


# ---- the decision path ----------------------------------------------------------------------------------------

def test_keep_first_truist_then_keep_second_is_409(tmp_path):
    client, root, directory = _serve(tmp_path, combine(truist(), truist()))
    t = _targets(client, root)
    assert t["e2"]["duplicate_of"] == {"row_id": "e1", "title": TITLE, "date": "2026-11-12", "accepted": True}
    assert t["e2"]["duplicate_actions"] == ["same_event", "distinct_events"] and t["e2"]["keep_refused"] == REFUSED
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    before = _log(directory)
    r = client.post(root + "/decisions", json={"target": "e2", "action": "keep", "note": "keep it"})
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == body["reason"] == REFUSED
    assert body["code"] == "duplicate_unresolved" and body["duplicate_of"]["row_id"] == "e1"
    assert body["actions"] == ["same_event", "distinct_events"]
    assert _log(directory) == before
    # also refused while e1 is only placed automatically (not kept): auto-included counts as on the calendar
    client2, root2, _ = _serve(tmp_path / "auto", combine(truist(), truist()))
    assert client2.post(root2 + "/decisions", json={"target": "e2", "action": "keep"}).status_code == 409


def test_same_event_one_calendar_entry_both_sources_visible(tmp_path):
    client, root, directory = _serve(tmp_path, combine(truist(), truist("November 12, 2026")))
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    r = client.post(root + "/decisions", json={"target": "e2", "action": "same_event", "duplicate_of": "e1",
                                               "note": "same symposium, second capture"})
    assert r.status_code == 200, r.text
    line = json.loads(_log(directory).splitlines()[-1])
    assert line["action"] == "same_event" and line["duplicate_of"] == "e1" and line["by"] == "person"
    t = _targets(client, root)
    assert t["e2"]["duplicate_state"] == "same_event" and not t["e2"]["on_calendar"]
    assert t["e2"]["inclusion"] == "excluded" and t["e2"]["semantic_status"] == "same_event"
    assert t["e2"]["values"]["quote"] and t["e2"]["effective_checks"] is not None     # its evidence stays
    assert t["e1"]["accepted"]
    assert [s["row_id"] for s in t["e1"]["supporting_sources"]] == ["e2"]
    assert t["e1"]["supporting_sources"][0]["date_text"] == "November 12, 2026"
    entries = client.get(root + "/calendar").json()["entries"]
    assert [e["id"] for e in entries] == ["e1"]
    assert [s["row_id"] for s in entries[0]["supporting_sources"]] == ["e2"]
    # undo brings the choice back
    assert client.post(root + "/decisions", json={"target": "e2", "action": "undo"}).status_code == 200
    assert _targets(client, root)["e2"]["duplicate_state"] == "unresolved"


def test_distinct_events_needs_a_note_then_both_kept(tmp_path):
    client, root, directory = _serve(tmp_path, combine(truist(), truist()))
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    bad = client.post(root + "/decisions", json={"target": "e2", "action": "distinct_events", "note": "  "})
    assert bad.status_code == 422 and "note" in bad.json()["detail"]
    ok = client.post(root + "/decisions", json={"target": "e2", "action": "distinct_events",
                                                "note": "Separate 1x1 day announced separately"})
    assert ok.status_code == 200, ok.text
    t = _targets(client, root)
    assert t["e2"]["duplicate_state"] == "distinct_events" and t["e2"]["keep_refused"] is None
    assert client.post(root + "/decisions", json={"target": "e2", "action": "keep"}).status_code == 200
    t = _targets(client, root)
    assert t["e1"]["inclusion"] == t["e2"]["inclusion"] == "kept"
    assert sorted(_calendar_ids(client, root)) == ["e1", "e2"]


def test_resolution_refused_without_a_pair_or_with_a_stale_partner(tmp_path):
    client, root, _ = _serve(tmp_path, combine(truist(), truist(title="UBS Global Biopharma Conference")))
    r = client.post(root + "/decisions", json={"target": "e2", "action": "same_event", "duplicate_of": "e1"})
    assert r.status_code == 409
    client, root, _ = _serve(tmp_path / "b", combine(truist(), truist(), truist()))
    r = client.post(root + "/decisions", json={"target": "e3", "action": "same_event", "duplicate_of": "e2"})
    assert r.status_code == 409 and "now e1" in r.json()["detail"]


def test_concurrent_keeps_exactly_one_succeeds(tmp_path):
    # Both copies need review (neither is placed automatically), so either Keep alone would be taken.
    a, b = truist(), truist()
    for run in (a, b):
        run["rows"][0]["research"] = {"review_findings": [{"kind": "x", "text": "check the date", "row_id": "e1",
                                                           "current": True, "stale": False}]}
    client, root, directory = _serve(tmp_path, combine(a, b))
    t = _targets(client, root)
    assert t["e1"]["inclusion"] == t["e2"]["inclusion"] == "review"
    barrier, results = threading.Barrier(2), {}

    def post(target):
        barrier.wait()
        results[target] = client.post(root + "/decisions", json={"target": target, "action": "keep"}).status_code
    threads = [threading.Thread(target=post, args=(x,)) for x in ("e1", "e2")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results.values()) == [200, 409], results
    keeps = [json.loads(x) for x in _log(directory).splitlines()]
    assert len(keeps) == 1 and keeps[0]["action"] == "keep"
    assert len(_calendar_ids(client, root)) == 1


def test_both_kept_projected_unresolved_without_rewriting(tmp_path):
    run = combine(truist(), truist())
    s0 = current_state(run, [])
    log = [keep(s0, "e1"), keep(s0, "e2")]
    log[1]["at"] = "2026-11-02T00:00:01+00:00"
    frozen = copy.deepcopy(log)
    state = current_state(run, log)
    assert log == frozen
    first, second = state["targets"]["e1"], state["targets"]["e2"]
    assert first["inclusion"] == "kept"
    assert second["inclusion"] == "review" and second["duplicate_state"] == "both_kept"
    assert "Both copies were kept (this one and e1); resolve: same event or different events" in second["issues"]
    assert second["review_label"] == "Previous Keep — needs your review"
    assert second["review_history"][-1]["action"] == "keep"
    partition, _, _ = run_interval_partition(run, state)
    assert [e["id"] for e in partition["calendar"]] == ["e1"]
    assert state["counts"]["inclusion"]["kept"] == 1 and state["kept_rows"] == 1


# ---- run 9 replay (read-only; the saved run is copied, its pages hard-linked) --------------------------------------

RUN9 = Path.home() / "proof-reader-candidate/runs/run-9"


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


@pytest.mark.skipif(not (RUN9 / "run.json").is_file(), reason="saved run 9 is not on this machine")
def test_run9_replay_real_decisions(tmp_path):
    original_hash = hashlib.sha256((RUN9 / "decisions.jsonl").read_bytes()).hexdigest()
    run_hash = hashlib.sha256((RUN9 / "run.json").read_bytes()).hexdigest()
    copy_dir = tmp_path / "run-9"
    copy_dir.mkdir()
    for item in RUN9.iterdir():
        if item.name == "pages":
            # the server refuses links that leave the run folder; hard links (or copies) stay inside it, and
            # nothing writes page files
            shutil.copytree(item, copy_dir / "pages", copy_function=_link_or_copy)
        elif item.is_dir():
            shutil.copytree(item, copy_dir / item.name)
        else:
            shutil.copy2(item, copy_dir / item.name)
    saved_lines = (copy_dir / "decisions.jsonl").read_text()
    client = TestClient(create_app(runs_dir=tmp_path))
    root = "/api/runs/run-9"
    t = client.get(root).json()["state"]["targets"]
    assert t["e10"]["inclusion"] == "kept"
    assert t["e28"]["duplicate_state"] == "both_kept" and t["e28"]["inclusion"] == "review"
    assert t["e28"]["duplicate_of"]["row_id"] == "e10" and t["e28"]["duplicate_of"]["accepted"]
    assert any(i.startswith("Both copies were kept (this one and e10)") for i in t["e28"]["issues"])
    assert t["e25"]["inclusion"] == "excluded" and t["e25"]["last_action"] == "remove"     # THRIVE stays removed
    for other in ("e9", "e11", "e12"):        # Wolfe, Jefferies, UBS: different conferences, not paired
        assert t[other]["duplicate_of"] is None
    entries = client.get(root + "/calendar").json()["entries"]
    truist_ids = [e["id"] for e in entries if "Truist" in (e["title"] or "")]
    assert truist_ids == ["e10"]
    assert (copy_dir / "decisions.jsonl").read_text() == saved_lines            # nothing written by reading
    # resolve on the copy: same event -> one Truist entry, both sources visible, history appended only
    r = client.post(root + "/decisions", json={"target": "e28", "action": "same_event", "duplicate_of": "e10"})
    assert r.status_code == 200, r.text
    assert (copy_dir / "decisions.jsonl").read_text().startswith(saved_lines)
    entries = client.get(root + "/calendar").json()["entries"]
    truist = [e for e in entries if "Truist" in (e["title"] or "")]
    assert [e["id"] for e in truist] == ["e10"]
    assert [s["row_id"] for s in truist[0]["supporting_sources"]] == ["e28"]
    assert hashlib.sha256((RUN9 / "decisions.jsonl").read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256((RUN9 / "run.json").read_bytes()).hexdigest() == run_hash
