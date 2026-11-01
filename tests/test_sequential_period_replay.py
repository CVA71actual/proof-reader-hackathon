"""Sequential editable period projection, reload and Undo regression coverage."""
import json
import shutil

from fastapi.testclient import TestClient

from server.app import create_app
from tests.conftest import REPO


def test_period_projection_survives_reload_and_undo(tmp_path):
    d = tmp_path / "ptgx-r4a"
    shutil.copytree(REPO / "runs/ptgx-r4a", d)
    (d / "decisions.jsonl").write_text("")
    path = d / "run.json"
    run = json.loads(path.read_text())
    row = run["rows"][1]
    row["date"] = row["date_end"] = None
    row["date_text"] = "Q2 2027"
    for check in row["checks"]:
        check.pop("assessment", None)
    path.write_text(json.dumps(run))
    with TestClient(create_app(tmp_path, background=False)) as client:
        url = f"/api/runs/ptgx-r4a/decisions"
        target = row["id"]

        def state():
            return client.get("/api/runs/ptgx-r4a").json()["state"]["targets"][target]

        first = client.post(url, json={"target": target, "action": "fix", "fix": {
            "field": "date_text", "from": "Q2 2027", "to": "Q1 2027"}})
        assert first.status_code == 200, first.json()
        assert state()["values"]["date"] == "2027-01-01"
        stale = client.post(url, json={"target": target, "action": "fix", "fix": {
            "field": "date", "from": None, "to": "2027-02-01"}})
        assert stale.status_code == 422

        second = client.post(url, json={"target": target, "action": "fix", "fix": {
            "field": "date", "from": "2027-01-01", "to": "2027-02-01"}})
        assert second.status_code == 200, second.json()
        assert state()["values"]["date"] == "2027-02-01"
        assert client.get("/api/runs/ptgx-r4a").status_code == 200  # reload/replay

        undo_second = client.post(url, json={"target": target, "action": "undo"})
        assert undo_second.status_code == 200
        assert state()["values"]["date"] == "2027-01-01"
        undo_first = client.post(url, json={"target": target, "action": "undo"})
        assert undo_first.status_code == 200
        assert state()["values"]["date"] is None
        assert state()["values"]["date_text"] == "Q2 2027"
