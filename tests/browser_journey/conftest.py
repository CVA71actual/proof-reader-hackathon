"""Fixtures for the real-browser acceptance journey (tests/browser_journey/).

The real server (server/app.py create_app) is started with uvicorn on a free localhost port, over a temporary runs
folder, with FAKE models only: the scripted Responses API researcher, the scripted chat reviewer and adjudicator,
a fake Brave Search, a fake fetcher and an offline renderer from tests/research_fakes.py, and the offline SEC
intake preparer from tests/test_ticker_intake.py. No model, no network, no key, no paid call.

The browser is headless Chromium through Playwright for Python. The browser binary is found in this order: the
PROOF_READER_CHROME environment variable, Playwright's own download, the chromium folders under
PLAYWRIGHT_BROWSERS_PATH or /opt/pw-browsers, then chromium/google-chrome on PATH. Without one the journey is
skipped with a sentence saying so (never silently passed).

Nothing here edits product code: the fixtures only inject fakes through create_app's own parameters.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chrome_candidates() -> list[str]:
    out = []
    env = os.environ.get("PROOF_READER_CHROME")
    if env:
        out.append(env)
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers",
             str(Path.home() / ".cache" / "ms-playwright")]
    for root in [r for r in roots if r]:
        out += sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")), reverse=True)
        out += sorted(glob.glob(os.path.join(root, "chromium_headless_shell-*", "chrome-linux*", "headless_shell")),
                      reverse=True)
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            out.append(found)
    return [c for c in out if os.path.isfile(c) and os.access(c, os.X_OK)]


class Server:
    """server/app.py's app under uvicorn in a thread, on a free port."""

    def __init__(self, app):
        import uvicorn
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="proof-reader-server")

    def start(self) -> "Server":
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("The decisions server did not start.")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture(scope="module")
def runs_dir():
    d = Path(tempfile.mkdtemp(prefix="proof-reader-journey-")) / "runs"
    d.mkdir()
    yield d
    shutil.rmtree(d.parent, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment without the package
        pytest.skip("Playwright for Python is not installed (pip install playwright).")
    pw = sync_playwright().start()
    launched = None
    errors = []
    for path in [None] + chrome_candidates():
        try:
            launched = pw.chromium.launch(headless=True, **({"executable_path": path} if path else {}),
                                          args=["--no-sandbox"])
            break
        except Exception as e:  # the next candidate may work
            errors.append(f"{path or 'playwright default'}: {str(e).splitlines()[0]}")
    if launched is None:
        pw.stop()
        pytest.skip("No headless Chromium could be launched. Tried: " + "; ".join(errors))
    yield launched
    launched.close()
    pw.stop()
