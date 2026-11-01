"""From Red Team round 2 (Grok Red Team, tests/redteam/test_round2.py in proof-reader-redteam): the eleven attacks,
copied here unchanged so they run in CI; only the repo path differs. A failure is a finding: the assertion is the
result the check should have given.

Scope: the decisions server and the state rule (concurrency, undo chains, before/after re-check,
a wrong value then the right one) and row_column_match geometry. Temp copies of runs/sample only.
"""

import json
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from checks.common import FAIL, PASS
from checks.row_column_match import row_column_match
from checks.status import FAILED, MATCHED, check_cell
from server.app import create_app

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "runs"
SAMPLE = RUNS / "sample"
WORDS = json.loads((REPO / "fixtures" / "ions_ars_2026_p95.words.json").read_text())["words"]
COLUMNS = ["2025", "2024", "2023"]


def _digest(folder: Path) -> str:
    import hashlib
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


def _lines(runs_dir):
    return (runs_dir / "sample" / "decisions.jsonl").read_text().splitlines()


def _post(client, target, action, fix=None):
    return client.post("/api/runs/sample/decisions",
                       json={"target": target, "action": action, "note": None, "fix": fix})


def _fix(client, target, field, frm, to):
    return _post(client, target, "fix", {"field": field, "from": frm, "to": to})


def _state(client):
    return client.get("/api/runs/sample").json()["state"]


def _word(text, x, y, w=40, h=11, i="w"):
    return {"id": i, "text": text, "x0": x, "y0": y, "x1": x + w, "y1": y + h}


def _box(word, pad=0.5):
    return [word["x0"] - pad, word["y0"] - pad, word["x1"] + pad, word["y1"] + pad]


def _looked(checks, name):
    return next(c for c in checks if c["name"] == name)["compared"]["looked_for"]


# ---------------------------------------------------------------- decisions server: page hash


@pytest.mark.parametrize("kind", ["changed", "missing"])
def test_the_decision_log_is_refused_when_a_page_file_is_changed(client, runs_dir, kind):
    """A page file that is missing or no longer matches its hash refuses the run, including its decision log."""
    if kind == "changed":
        name = "pages/p95.png"
        f = runs_dir / "sample" / name
        data = bytearray(f.read_bytes())
        data[len(data) // 2] ^= 1
        f.write_bytes(bytes(data))
        detail = f"This run is not served: its file {name} does not match the hash recorded in run.json."
    else:
        name = "pages/smp-filing-b-p1.words.json"
        (runs_dir / "sample" / name).unlink()
        detail = f"This run is not served: its file {name} is missing."
    shown = client.get("/api/runs/sample")
    log = client.get("/api/runs/sample/decisions")
    assert shown.status_code == 409 and shown.json()["detail"] == detail
    assert log.status_code == 409 and log.json()["detail"] == detail


# ---------------------------------------------------------------- state rule: undo chain, before/after, concurrency


def test_a_wrong_value_then_the_right_one_undoes_back_to_the_printed_value(client, runs_dir):
    """115,315 is matched. A value that is not on the page stays in effect and is not a catch.
    Putting the printed value back records before=fail on the wrong value and after=pass on the
    printed one. Two undos restore the printed value and the starting counters. A third is refused.
    """
    s0 = _state(client)
    n0 = len(_lines(runs_dir))
    wrong = _fix(client, "t1.0.0", "value", "115,315", "999,999")
    assert wrong.status_code == 200, wrong.text
    body = wrong.json()
    rc = body["written"][1]["recheck"]
    assert body["state"]["values"]["value"] == "999,999"
    assert (body["state"]["group"], body["state"]["status"]) == ("open", "failed")
    assert (rc["before"]["result"], rc["after"]["result"]) == ("pass", "fail")
    assert _looked(rc["before"]["checks"], "text_layer_match") == "115,315"
    assert _looked(rc["after"]["checks"], "text_layer_match") == "999,999"
    assert body["mistakes"] == s0["mistakes"]
    assert body["counts"]["edits_without_error"] == s0["counts"]["edits_without_error"]
    refused = _fix(client, "t1.0.0", "value", "115,315", "115,316")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "The fix starts from '115,315' but the current value is '999,999'."
    assert len(_lines(runs_dir)) == n0 + 2

    back = _fix(client, "t1.0.0", "value", "999,999", "115,315")
    assert back.status_code == 200, back.text
    repaired = back.json()
    rc = repaired["written"][1]["recheck"]
    assert repaired["state"]["values"]["value"] == "115,315"
    assert (repaired["state"]["group"], repaired["state"]["status"]) == ("corrected", "matched")
    assert (rc["before"]["result"], rc["after"]["result"]) == ("fail", "pass")
    assert _looked(rc["before"]["checks"], "text_layer_match") == "999,999"
    assert _looked(rc["after"]["checks"], "text_layer_match") == "115,315"

    undo_repair = _post(client, "t1.0.0", "undo")
    assert undo_repair.status_code == 200, undo_repair.text
    assert undo_repair.json()["mistakes"] == s0["mistakes"]
    assert undo_repair.json()["state"]["values"]["value"] == "999,999"
    assert undo_repair.json()["state"]["status"] == "failed"
    undo_wrong = _post(client, "t1.0.0", "undo")
    assert undo_wrong.status_code == 200, undo_wrong.text
    assert undo_wrong.json()["mistakes"] == s0["mistakes"]
    assert undo_wrong.json()["counts"] == s0["counts"]
    assert undo_wrong.json()["state"]["values"]["value"] == "115,315"
    assert undo_wrong.json()["state"]["group"] == "kept"
    n = len(_lines(runs_dir))
    third = _post(client, "t1.0.0", "undo")
    assert third.status_code == 422
    assert third.json()["detail"] == "There is nothing to undo for this target."
    assert len(_lines(runs_dir)) == n
    replay = _state(client)["targets"]["t1.0.0"]
    assert replay["values"]["value"] == "115,315" and replay["group"] == "kept"


def test_two_simultaneous_undos_only_one_is_taken(client, runs_dir, monkeypatch):
    import server.app as server_app
    _fix(client, "t1.0.0", "value", "115,315", "999,999")
    n = len(_lines(runs_dir))
    real = server_app.current_state

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(server_app, "current_state", slow)
    start = threading.Barrier(2)
    results = {}

    def go(i):
        start.wait()
        results[i] = _post(client, "t1.0.0", "undo")

    threads = [threading.Thread(target=go, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status_code for r in results.values()) == [200, 422]
    refused = next(r for r in results.values() if r.status_code == 422)
    assert refused.json()["detail"] == "There is nothing to undo for this target."
    assert [json.loads(x)["action"] for x in _lines(runs_dir)[n:]] == ["undo"]
    st = _state(client)
    assert st["ignored"] == []
    assert st["targets"]["t1.0.0"]["values"]["value"] == "115,315"
    assert st["targets"]["t1.0.0"]["group"] == "kept"


def test_a_fix_and_an_undo_at_once_stay_one_valid_order(client, runs_dir, monkeypatch):
    """Either the undo lands first and the fix is refused, or the fix and its re-check land and the undo
    takes them back. The log never holds that fix without its re-check, and a replay matches the GET."""
    import server.app as server_app
    _fix(client, "t1.0.0", "value", "115,315", "999,999")
    n = len(_lines(runs_dir))
    real = server_app.current_state

    def slow(*args, **kwargs):
        time.sleep(0.15)
        return real(*args, **kwargs)

    monkeypatch.setattr(server_app, "current_state", slow)
    start = threading.Barrier(2)
    results = {}

    def go(kind):
        start.wait()
        if kind == "undo":
            results[kind] = _post(client, "t1.0.0", "undo")
        else:
            results[kind] = _fix(client, "t1.0.0", "value", "999,999", "115,315")

    threads = [threading.Thread(target=go, args=(k,)) for k in ("undo", "fix")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    actions = [(json.loads(x)["action"], json.loads(x)["by"]) for x in _lines(runs_dir)[n:]]
    codes = {k: r.status_code for k, r in results.items()}
    committed = _state(client)["targets"]["t1.0.0"]["values"]["value"]
    undo_first = actions == [("undo", "person")] and codes == {"undo": 200, "fix": 422} and committed == "115,315"
    fix_first = (actions == [("fix", "person"), ("recheck", "code"), ("undo", "person")]
                 and codes == {"undo": 200, "fix": 200} and committed == "999,999")
    assert undo_first or fix_first, (actions, codes, committed)
    assert _state(client)["ignored"] == []
    if fix_first:
        assert results["fix"].json()["state"]["values"]["value"] == "115,315"
        assert results["undo"].json()["state"]["values"]["value"] == "999,999"


def test_a_read_during_a_recheck_sees_the_fix_with_its_recheck(client, runs_dir, monkeypatch):
    import server.app as server_app
    n = len(_lines(runs_dir))
    real = server_app.recheck_cell
    gate = threading.Event()

    def slow(*args, **kwargs):
        gate.set()
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(server_app, "recheck_cell", slow)
    holder = {}

    def do_fix():
        holder["r"] = _fix(client, "t1.0.0", "value", "115,315", "999,999")

    thread = threading.Thread(target=do_fix)
    thread.start()
    assert gate.wait(2)
    mid = client.get("/api/runs/sample/decisions")
    thread.join()
    assert holder["r"].status_code == 200, holder["r"].text
    assert mid.status_code == 200
    new = mid.json()[n:]
    assert [(x["action"], x["by"]) for x in new] == [("fix", "person"), ("recheck", "code")]
    assert new[1]["recheck"]["result"] == "fail"


# ---------------------------------------------------------------- row_column_match geometry


def test_a_single_column_does_not_treat_the_whole_page_as_its_band():
    """One year column, plus a units stub that is not itself a column. The value sits under the year.
    A second box on the same line sits far from that year. The first passes; the second fails."""
    stub = _word("(in thousands)", 40, 100, 80, i="stub")
    header = _word("2025", 400, 100, 30, i="h")
    label = _word("Royalty", 40, 180, 50, i="lab")
    value = _word("115,315", 400, 180, 36, i="v")
    far = _word("9,999", 900, 180, 40, i="far")
    words = [stub, header, label, value, far]
    on_value = row_column_match("Royalty", "2025", _box(value), words, ["2025"])
    off_to_the_side = row_column_match("Royalty", "2025", _box(far), words, ["2025"])
    assert [on_value.result, off_to_the_side.result] == [PASS, FAIL], [
        (on_value.result, on_value.reason), (off_to_the_side.result, off_to_the_side.reason)]


def test_a_wide_gap_between_headers_does_not_hide_the_row_label():
    """The label ends at x=100. The 2025 header is centred at 215 and the 2024 header at 715.
    The value sits under 2025, and the label is entirely to the left of the value."""
    header_2025 = _word("2025", 200, 100, 30, i="h1")
    header_2024 = _word("2024", 700, 100, 30, i="h2")
    label = _word("Royalty", 40, 180, 60, i="lab")
    value = _word("115,315", 200, 180, 36, i="v")
    words = [header_2025, header_2024, label, value]
    r = row_column_match("Royalty", "2025", _box(value), words, ["2025", "2024"])
    assert r.result == PASS, (r.result, r.reason)


def _caption_page():
    """A header line aligned over the two values, and a lower line that also names both years."""
    return [
        _word("2025", 190, 80, 30, i="h1"),
        _word("2024", 270, 80, 30, i="h2"),
        _word("2025", 185, 140, 30, i="c1"),
        _word("2024", 385, 140, 30, i="c2"),
        _word("Royalty", 10, 200, 60, i="lab"),
        _word("115,315", 185, 200, 36, i="v1"),
        _word("257,307", 262, 200, 36, i="v2"),
    ]


def test_a_nearer_caption_does_not_match_the_other_column():
    """257,307 is the glyph under the aligned 2024 header. Stored as column 2025, the cell is failed."""
    words = _caption_page()
    value = next(w for w in words if w["id"] == "v2")
    doc = {"words": words, "page_width": 612.0, "page_height": 792.0}
    cell = {"id": "c", "column": "2025", "value": "257,307", "box": _box(value)}
    checked = check_cell(cell, doc, "Royalty", ["2025", "2024"], visual_evidence=None, derived_words_present=False)
    row = next(c for c in checked["checks"] if c["check"] == "row_column_match")
    assert (checked["status"], row["result"]) == (FAILED, FAIL), (checked["status"], row["result"], row["reason"])


def test_a_nearer_caption_on_the_real_page_does_not_move_the_2024_cell():
    """The real Royalty revenue 2024 glyph, with three year words inserted on a line between the real
    header and that glyph. The cell is still the 2024 cell."""
    royalty = next(w for w in WORDS if w["text"] == "257,307")
    inserted = WORDS + [
        _word("2025", 200, 128, 30, i="c1"),
        _word("2024", 900, 128, 30, i="c2"),
        _word("2023", 1100, 128, 30, i="c3"),
    ]
    doc = {"words": inserted, "page_width": 612.0, "page_height": 792.0}
    cell = {"id": "t", "column": "2024", "value": "257,307",
            "box": [royalty["x0"], royalty["y0"], royalty["x1"], royalty["y1"]]}
    checked = check_cell(cell, doc, "Royalty revenue", COLUMNS, visual_evidence=None, derived_words_present=False)
    row = next(c for c in checked["checks"] if c["check"] == "row_column_match")
    assert (checked["status"], row["result"], row["reason"]) == (
        MATCHED, PASS, "The box is on the 'Royalty revenue' line and under 2024."), (
        checked["status"], row["result"], row["reason"])


def test_an_extra_column_name_and_a_nearer_year_keep_the_real_header():
    """The saved columns plus one name that is not on the header line, and one '2024' word nearer the
    cells than the real header. The 2025 glyph asked as 2024 fails as under 2025. The 2024 glyph passes."""
    sales = next(w for w in WORDS if w["text"] == "115,315")
    royalty = next(w for w in WORDS if w["text"] == "257,307")
    words = WORDS + [_word("2024", 300, 128, 30, i="nearer")]
    columns = COLUMNS + ["Total"]
    wrong_column = row_column_match(
        "Product sales, net", "2024",
        [sales["x0"], sales["y0"], sales["x1"], sales["y1"]], words, columns)
    right_column = row_column_match(
        "Royalty revenue", "2024",
        [royalty["x0"], royalty["y0"], royalty["x1"], royalty["y1"]], words, columns)
    assert [(wrong_column.result, wrong_column.reason), (right_column.result, right_column.reason)] == [
        (FAIL, "The box sits under 2025, not 2024."),
        (PASS, "The box is on the 'Royalty revenue' line and under 2024."),
    ]
