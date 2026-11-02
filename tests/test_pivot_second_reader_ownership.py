"""A reused reader must not route one job's failed attempt into another job."""
import json

from capture.reader import Reply
from capture.second_reader import SecondReader
from server import jobs
from tests.test_second_reader import CHOICES
from tests.test_run_controls import FakeReader


class UnpricedReader(SecondReader):
    def _key(self, name):
        return "offline-test-key"

    def _ask(self, route, prompt, image=None):
        assert route == "baseten", "unknown cost must prevent the fallback"
        return Reply(None, "unpriced-test-model", error="HTTP 400",
                     extra={"route": route, "http_status": 400, "may_have_been_billed": True})


def test_shared_second_reader_keeps_gate_and_costs_with_its_job(tmp_path):
    shared = UnpricedReader()
    work = []
    for name in ("job-a", "job-b"):
        jobs.create_run(tmp_path, CHOICES, run_id=name)
        work.append(jobs.Job(tmp_path / name, FakeReader(), shared))
    a, b = work
    assert shared.before_fallback is None
    assert a.second is not b.second and a.second is not shared
    assert a.second.before_fallback.__self__ is a
    assert b.second.before_fallback.__self__ is b
    a.second.call("No network: synthetic unpriced failure")
    saved_a = json.loads((a.dir / "job.json").read_text())
    saved_b = json.loads((b.dir / "job.json").read_text())
    assert saved_a["unpriced_calls"] == 1
    assert saved_b.get("unpriced_calls", 0) == 0
    assert a.meta["turns_total"] == 1 and b.meta["turns_total"] == 0
    assert a.meta["status"] == "needs_input"
    a.second.openrouter_allowed = False
    a.second.openrouter_blocked = "job A exhausted"
    assert b.second.openrouter_allowed and shared.openrouter_allowed
    assert b.second.openrouter_blocked is None and shared.openrouter_blocked is None
