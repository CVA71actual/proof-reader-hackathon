"""Offline pipe pressure regressions: no weights or model calls."""
import sys
import time
from capture.support_process import run_batch


def worker(tmp_path, body):
    p = tmp_path / "worker.py"
    p.write_text(body)
    weights = tmp_path / "weights"
    weights.mkdir()
    return dict(worker=p, python=sys.executable, weights=str(weights), pinned_weights_sha256=None)


def test_input_delivery_is_inside_the_deadline(tmp_path):
    args = worker(tmp_path, "import time,sys\ntime.sleep(3)\nsys.stdin.read()\nprint('{}')\n")
    began = time.monotonic()
    results, meta = run_batch([{"id":"e1", "claim":"x", "passage":"a" * 2000000}], timeout_s=0.2, **args)
    assert time.monotonic() - began < 1.5
    assert results["e1"]["result"] == "could_not_run"
    assert "timeout" in meta["reason"]


def test_person_stop_interrupts_input_delivery(tmp_path):
    args = worker(tmp_path, "import time,sys\ntime.sleep(3)\nsys.stdin.read()\nprint('{}')\n")
    began = time.monotonic()
    results, meta = run_batch([{"id":"e1", "claim":"x", "passage":"a" * 2000000}], timeout_s=10,
                              should_stop=lambda: time.monotonic() - began > 0.1, **args)
    assert time.monotonic() - began < 1.5
    assert meta["reason"] == "stopped by a person"


def test_stdout_and_stderr_are_drained_during_execution(tmp_path):
    args = worker(tmp_path, "import json,sys\nsys.stdin.read()\nsys.stderr.write('e' * 2000000)\nprint(json.dumps({'results': {'e1': {'result': 'no_finding', 'spans': []}}, 'padding': 'a' * 2000000}))\n")
    results, meta = run_batch([{"id":"e1", "claim":"x", "passage":"p"}], timeout_s=3, **args)
    assert meta["status"] == "ok"
    assert results["e1"]["result"] == "no_finding"
