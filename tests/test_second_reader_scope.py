"""Default provider-routing state belongs to one job, never to the server."""

from fastapi.testclient import TestClient
import pytest

from runs import sources
from server import app as app_module


@pytest.mark.parametrize("background", [False, True])
def test_default_second_reader_is_new_for_each_started_job(tmp_path, monkeypatch, background):
    readers = []
    # This test exercises the production factory but performs no model calls.
    monkeypatch.setattr(app_module, "LiveReader", lambda: object())

    def capture_job(*args):
        readers.append(args[3])

    monkeypatch.setattr(app_module.jobs, "run_job", capture_job)
    class ImmediateThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    application = app_module.create_app(runs_dir=tmp_path, background=background,
                                        index=sources.builtin_index())
    with TestClient(application) as client:
        for _ in range(2):
            response = client.post("/api/runs/new", json={"companies": ["PTGX"]})
            assert response.status_code == 202, response.text
    assert len(readers) == 2
    assert readers[0] is not readers[1]
    readers[0].openrouter_allowed = False
    readers[0].openrouter_blocked = "First run only"
    readers[0].before_fallback = lambda reply: False
    assert readers[1].openrouter_allowed
    assert readers[1].openrouter_blocked is None
    assert readers[1].before_fallback is None


def test_explicitly_injected_reader_is_preserved(tmp_path, monkeypatch):
    supplied, first = object(), object()
    seen = []
    monkeypatch.setattr(app_module.jobs, "run_job", lambda *args: seen.append(args[3]))
    application = app_module.create_app(runs_dir=tmp_path, background=False,
                                        reader=first, second_reader=supplied,
                                        index=sources.builtin_index())
    with TestClient(application) as client:
        response = client.post("/api/runs/new", json={"companies": ["PTGX"]})
        assert response.status_code == 202, response.text
    assert seen == [supplied]
