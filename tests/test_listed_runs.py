"""runs/LISTED.json: the Saved runs list shows only the listed runs (plus runs New run made in this server's
lifetime); an unlisted run is not deleted and stays served by id. No model or network call."""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app

RUNS = Path(__file__).resolve().parent.parent / "runs"


def _runs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    shutil.copytree(RUNS / "sample", d / "sample")
    shutil.copytree(RUNS / "demo", d / "demo")
    return d


def _ids(client: TestClient) -> list[str]:
    response = client.get("/api/runs")
    response.raise_for_status()
    return [r["id"] for r in response.json()]


def test_an_unlisted_run_is_absent_from_the_list_but_still_served_by_id(tmp_path):
    d = _runs_dir(tmp_path)
    (d / "LISTED.json").write_text(json.dumps({"listed": ["sample"]}))
    client = TestClient(create_app(runs_dir=d, reader=lambda *a, **k: None))
    assert _ids(client) == ["sample"]
    demo = client.get("/api/runs/demo")
    assert demo.status_code == 200 and demo.json()["run"]["id"] == "demo"
    assert client.get("/api/runs/demo/decisions").status_code == 200


def test_a_missing_listed_file_lists_every_run(tmp_path):
    d = _runs_dir(tmp_path)
    assert not (d / "LISTED.json").exists()
    client = TestClient(create_app(runs_dir=d, reader=lambda *a, **k: None))
    assert _ids(client) == ["demo", "sample"]


def test_a_listed_id_with_no_run_folder_is_ignored(tmp_path):
    d = _runs_dir(tmp_path)
    (d / "LISTED.json").write_text(json.dumps({"listed": ["sample", "not-a-run"]}))
    client = TestClient(create_app(runs_dir=d, reader=lambda *a, **k: None))
    assert _ids(client) == ["sample"]


def test_the_committed_list_is_sample_only_and_the_hidden_runs_still_open():
    listed = json.loads((RUNS / "LISTED.json").read_text())["listed"]
    assert listed == ["sample"]
    client = TestClient(create_app(runs_dir=RUNS, reader=lambda *a, **k: None))
    assert _ids(client) == ["sample"]
    for hidden in ("demo", "ptgx-r5", "twelve-4"):
        assert client.get(f"/api/runs/{hidden}").status_code in (200, 409), hidden   # served, not "No run has that id"


def test_a_run_new_run_made_in_this_server_s_lifetime_is_listed(tmp_path, monkeypatch):
    d = _runs_dir(tmp_path)
    (d / "LISTED.json").write_text(json.dumps({"listed": ["sample"]}))
    from server import jobs
    made = []

    def fake_create_run(runs_dir, choices, as_of=None):
        run_id = "live-1"
        shutil.copytree(RUNS / "demo", Path(runs_dir) / run_id)
        rec = json.loads((Path(runs_dir) / run_id / "run.json").read_text())
        rec["run"]["id"] = run_id
        (Path(runs_dir) / run_id / "run.json").write_text(json.dumps(rec))
        made.append(run_id)
        return {"run": {"id": run_id}}

    monkeypatch.setattr(jobs, "create_run", fake_create_run)
    monkeypatch.setattr(jobs, "run_job", lambda *a, **k: None)
    app = create_app(runs_dir=d, reader=lambda *a, **k: None, background=False)
    client = TestClient(app)
    assert _ids(client) == ["sample"]
    body = {"companies": ["IONS"], "time_window_months": None, "spending_limit_usd": None, "note_for_agent": None}
    response = client.post("/api/runs/new", json=body)
    assert response.status_code == 202, response.text
    assert made == ["live-1"]
    assert _ids(client) == ["live-1", "sample"]
    # a fresh server (a restart) goes back to the committed list: the run is still served, only not listed
    again = TestClient(create_app(runs_dir=d, reader=lambda *a, **k: None))
    assert _ids(again) == ["sample"]
    assert again.get("/api/runs/live-1").status_code == 200
