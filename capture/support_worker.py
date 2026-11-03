"""The LettuceDetect worker: run by a SEPARATE interpreter (LETTUCE_PYTHON, with torch/transformers/lettucedetect), never
imported by the product process. One process per company: it loads the pinned detector ONCE, checks the weights digest
against the pin, runs every item, prints one JSON object and exits. No network, no downloads, no retries.

    stdin:  {"weights_path": str, "pinned_weights_sha256": str, "items": [{"id": str, "claim": str, "passage": str}]}
    stdout: {"weights_sha256": str|null, "load_seconds": float|null, "results": {id: support_signal result},
             "error": str|null}

capture/support_process.py starts it, enforces the timeout, and turns every failure into could_not_run.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # this repo (the interpreter runs with -I)
    from capture import support_signal as ss

    req = json.loads(sys.stdin.read() or "{}")
    items = req.get("items") or []
    out = {"weights_sha256": None, "load_seconds": None, "results": {}, "error": None}
    began = time.perf_counter()
    try:
        det = ss._LocalDetector(str(req.get("weights_path") or ""))
    except Exception as e:  # the detector could not start: every item could not run
        out["error"] = f"detector could not start: {type(e).__name__}: {e}"
        print(json.dumps(out))
        return 0
    out["load_seconds"] = round(time.perf_counter() - began, 3)
    out["weights_sha256"] = det.weights_sha256
    pinned = req.get("pinned_weights_sha256")
    if pinned and det.weights_sha256 != pinned:
        out["error"] = f"weights digest {det.weights_sha256} does not match the pinned {pinned}"
        print(json.dumps(out))
        return 0
    for item in items:
        out["results"][item["id"]] = ss.support_signal({"claim": item["claim"]}, item["passage"], detector=det)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
