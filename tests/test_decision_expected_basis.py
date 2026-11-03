"""DIALOGS-001 server half (Manus 2026-11-03 05:31): a decision may carry the review_basis the person was shown. If
the target changed since (another revision of the proposal or its evidence), the server refuses with 409 and writes
nothing. Callers that send no expected_basis work as before. Synthetic data only."""
import json

from fastapi.testclient import TestClient

from server.app import STALE_DECISION_REASON, create_app
from tests.test_pivot_proposal_flow import CASES, make_run


def _setup(tmp_path):
    make_run(CASES[0], tmp_path / "synthetic")
    client = TestClient(create_app(runs_dir=tmp_path))
    return client, "/api/runs/synthetic", tmp_path / "synthetic"


def _basis(client, root):
    return client.get(root).json()["state"]["targets"]["e1"]["review_basis"]


def _revise(directory):
    """The proposal changes under an open dialog: a material source field (part of the Keep basis) is revised."""
    path = directory / "run.json"
    run = json.loads(path.read_text())
    run["sources"][0]["published_date"] = "2026-10-31"
    path.write_text(json.dumps(run))


def _log(directory):
    path = directory / "decisions.jsonl"
    return path.read_text() if path.exists() else ""


def test_a_keep_bound_to_a_shown_basis_that_changed_is_refused_and_writes_nothing(tmp_path):
    client, root, directory = _setup(tmp_path)
    shown = _basis(client, root)                                   # what the dialog showed (rev1)
    _revise(directory)
    current = _basis(client, root)
    assert current != shown
    before = _log(directory)
    r = client.post(root + "/decisions", json={"target": "e1", "action": "keep", "note": "kept from the dialog",
                                               "expected_basis": shown})
    assert r.status_code == 409 and r.json()["detail"] == STALE_DECISION_REASON
    assert _log(directory) == before
    assert not client.get(root).json()["state"]["targets"]["e1"]["accepted"]
    # the sha256 alone is accepted too, with the same answer
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep",
                                                  "expected_basis": shown["sha256"]}).status_code == 409
    # Remove from a stale dialog is refused the same way
    assert client.post(root + "/decisions", json={"target": "e1", "action": "remove", "note": "x",
                                                  "expected_basis": shown}).status_code == 409
    assert _log(directory) == before


def test_a_keep_bound_to_the_current_basis_is_written_and_binds_it(tmp_path):
    client, root, directory = _setup(tmp_path)
    _revise(directory)
    current = _basis(client, root)
    r = client.post(root + "/decisions", json={"target": "e1", "action": "keep", "expected_basis": current})
    assert r.status_code == 200, r.text
    line = json.loads(_log(directory).splitlines()[-1])
    assert line["review_basis"] == current and "expected_basis" not in line
    assert client.get(root).json()["state"]["targets"]["e1"]["accepted"]


def test_callers_without_expected_basis_work_as_before_and_a_bad_one_is_422(tmp_path):
    client, root, directory = _setup(tmp_path)
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    assert client.post(root + "/decisions", json={"target": "e1", "action": "undo"}).status_code == 200
    bad = client.post(root + "/decisions", json={"target": "e1", "action": "keep", "expected_basis": 7})
    assert bad.status_code == 422
