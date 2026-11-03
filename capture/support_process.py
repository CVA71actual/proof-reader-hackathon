"""The out-of-process, bounded boundary to the LettuceDetect worker (capture/support_worker.py).

The product process never imports lettucedetect. run_batch starts ONE worker per company with LETTUCE_PYTHON (-I),
sends every item on stdin, waits at most `timeout_s`, and checks `should_stop` (a person's Stop) while it waits. On a
timeout or a stop the worker is killed. Nothing is retried. Every failure (no interpreter, no weights, timeout, stop,
non-zero exit, malformed JSON, a weights digest that is not the pinned one) gives each item could_not_run with the
plain reason; it is never a pass and never a finding.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

PYTHON_ENV = "LETTUCE_PYTHON"
WEIGHTS_ENV = "LETTUCE_WEIGHTS_PATH"
WORKER = Path(__file__).with_name("support_worker.py")
TIMEOUT_S = 180               # load (~17 s measured) + ~0.07 s a claim; a company's batch past this is could_not_run
# The digest of the pinned weight files (KRLabsOrg/lettucedect-v2-mmbert-base @ 8831ce06…), measured on the installed
# snapshot 2026-11-02; any other digest is could_not_run.
PINNED_WEIGHTS_SHA256 = "16f28e8afc4941932dd16b1fb8dd284fcef180b711e633a0c094fcab5220f1ee"
POLL_S = 0.2


def _fail(items: list[dict], reason: str, **meta) -> tuple[dict, dict]:
    return ({i["id"]: {"result": "could_not_run", "spans": [], "detail": reason} for i in items},
            {"status": "could_not_run", "reason": reason, **meta})


def run_batch(items: list[dict], *, python: str | None = None, weights: str | None = None,
              timeout_s: float = TIMEOUT_S, worker: Path | str = WORKER, should_stop=None,
              pinned_weights_sha256: str | None = PINNED_WEIGHTS_SHA256) -> tuple[dict, dict]:
    """({item id: support_signal result}, run meta {status, reason, seconds, load_seconds, weights_sha256})."""
    if not items:
        return {}, {"status": "ok", "reason": None, "seconds": 0.0}
    python = python or os.environ.get(PYTHON_ENV, "")
    weights = weights or os.environ.get(WEIGHTS_ENV, "")
    if not python or not Path(python).is_file():
        return _fail(items, f"the LettuceDetect interpreter is not installed ({PYTHON_ENV} is not a file)")
    if not weights or not Path(weights).is_dir():
        return _fail(items, f"the pinned weights are not installed ({WEIGHTS_ENV} is not a directory)")
    request = json.dumps({"weights_path": weights, "pinned_weights_sha256": pinned_weights_sha256,
                          "items": items})
    began = time.monotonic()
    try:
        proc = subprocess.Popen([python, "-I", str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    except OSError as e:
        return _fail(items, f"the worker could not start ({type(e).__name__})")
    try:
        proc.stdin.write(request)
        proc.stdin.close()
    except OSError:
        pass
    while proc.poll() is None:
        elapsed = time.monotonic() - began
        if should_stop is not None and should_stop():
            proc.kill()
            proc.wait()
            return _fail(items, "stopped by a person", seconds=round(elapsed, 3))
        if elapsed > timeout_s:
            proc.kill()
            proc.wait()
            return _fail(items, f"timeout: the worker did not finish within {timeout_s:g}s", seconds=round(elapsed, 3))
        time.sleep(POLL_S)
    seconds = round(time.monotonic() - began, 3)
    stdout = proc.stdout.read()
    if proc.returncode != 0:
        return _fail(items, f"the worker exited with code {proc.returncode}", seconds=seconds)
    try:
        data = json.loads(stdout)
        results = data["results"]
        assert isinstance(results, dict)
    except Exception:
        return _fail(items, "the worker answered with malformed JSON", seconds=seconds)
    if data.get("error"):
        return _fail(items, data["error"], seconds=seconds, weights_sha256=data.get("weights_sha256"))
    if pinned_weights_sha256 and data.get("weights_sha256") != pinned_weights_sha256:
        return _fail(items, "the worker's weights digest is not the pinned one", seconds=seconds,
                     weights_sha256=data.get("weights_sha256"))
    out = {}
    for i in items:
        r = results.get(i["id"])
        out[i["id"]] = r if isinstance(r, dict) and r.get("result") in ("no_finding", "unsupported_spans",
                                                                         "could_not_run") else \
            {"result": "could_not_run", "spans": [], "detail": "the worker returned no result for this claim"}
    return out, {"status": "ok", "reason": None, "seconds": seconds, "load_seconds": data.get("load_seconds"),
                 "weights_sha256": data.get("weights_sha256")}
