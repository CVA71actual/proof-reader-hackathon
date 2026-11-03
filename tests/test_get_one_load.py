"""PERF (approved 2026-11-03): GET /api/runs/{id} and GET /api/runs/{id}/calendar load and verify the run once and
reuse it for the decision replay. Output unchanged; a file tampered with after a GET is caught on the next request;
POST keeps its own decision-time verification."""
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from runs import sources
from server import app as server_app
from server.app import create_app

REPO = Path(__file__).resolve().parents[1]


def _client(tmp_path):
    shutil.copytree(REPO / "runs" / "sample", tmp_path / "sample", ignore=shutil.ignore_patterns("report"))
    return TestClient(create_app(runs_dir=tmp_path)), tmp_path / "sample"


def _counting(monkeypatch):
    calls = {"verify": 0}
    real = sources.verify_page_files

    def counted(*a, **k):
        calls["verify"] += 1
        return real(*a, **k)
    monkeypatch.setattr(sources, "verify_page_files", counted)
    return calls


def test_a_get_loads_and_verifies_once_with_identical_output(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    reused = {p: client.get(p).json() for p in ("/api/runs/sample", "/api/runs/sample/calendar")}
    # the previous behaviour: the decision replay loads the run again
    real = server_app.RunStore.decisions

    def reload(self, run_id, *, refresh_rechecks=True, run=None):
        return real(self, run_id, refresh_rechecks=refresh_rechecks)
    monkeypatch.setattr(server_app.RunStore, "decisions", reload)
    assert {p: client.get(p).json() for p in reused} == reused
    monkeypatch.setattr(server_app.RunStore, "decisions", real)
    calls = _counting(monkeypatch)
    for path in reused:
        calls["verify"] = 0
        assert client.get(path).status_code == 200
        assert calls["verify"] == 1, path


def test_a_file_tampered_after_a_get_is_caught_on_the_next_request(tmp_path):
    client, directory = _client(tmp_path)
    assert client.get("/api/runs/sample").status_code == 200
    run = json.loads((directory / "run.json").read_text())
    words = directory / run["sources"][0]["pages"][0]["words"]
    words.write_bytes(words.read_bytes() + b" ")
    assert client.get("/api/runs/sample").status_code == 409
    assert client.get("/api/runs/sample/calendar").status_code == 409


def test_post_still_verifies_at_decision_time(tmp_path, monkeypatch):
    client, _ = _client(tmp_path)
    calls = _counting(monkeypatch)
    r = client.post("/api/runs/sample/decisions", json={"target": "e1", "action": "keep"})
    assert r.status_code == 200, r.text
    assert calls["verify"] >= 2       # the load, and again inside the decisions lock before writing
