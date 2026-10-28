"""The local decisions server: the screens read runs and write a person's decisions through it.

Run (from the repo root, localhost only):
    ~/proof-reader-docs/.venv/bin/python -m uvicorn server.app:app --port 8777

It calls a model only through a run's job (server/jobs.py, started by POST /api/runs/new or a resume), and only
through the reader it is given (capture/reader.py LiveReader); the decisions, listing and paging code never
touches a model, the env file or a key. Run it as one process (uvicorn's default, a
single worker): one lock in this process makes every decision one step (read the log, check the decision
against the state as it is now, run the re-check, append), and reads wait for a write in progress. Everything it says about a row comes
from runs/state.py current_state(run, decisions); everything it writes is one appended line in
runs/<id>/decisions.jsonl (a fix writes two: the person's fix, then code's re-check of it, which runs the
checks on the value before the fix and after it).

Before a run is used for anything (listed, shown, decided on, or a page file served), every page image and
words file it names is hashed and compared with the image_sha256 / words_sha256 recorded in run.json. If one
file is missing or differs, the whole run is refused with 409 and a sentence naming the file; no file of that
run is served, and no decision is written to it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator

from capture.reader import LiveReader
from runs.calendar import calendar_entries
from runs.recheck import recheck_cell, recheck_row
from runs.state import PERSON_ACTIONS, current_state
from server import jobs

REPO = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO / "runs"
UI_DIR = REPO / "ui"
DECISION_SCHEMA = RUNS_DIR / "schema" / "decision.schema.json"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BODY_FIELDS = {"target", "action", "note", "fix", "by"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# One lock for the whole process: a POST holds it from reading decisions.jsonl to appending its lines, and a
# read of decisions.jsonl takes it too, so no request sees a half-written batch or decides on a stale state.
DECISIONS_LOCK = threading.Lock()


class RunStore:
    """Reads runs/<id>/run.json and decisions.jsonl; appends decision lines. Nothing is ever rewritten.
    Callers hold DECISIONS_LOCK around decisions() and append()."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def run_dir(self, run_id: str) -> Path:
        if not _SAFE_ID.match(run_id or "") or ".." in run_id:
            raise HTTPException(404, "No run has that id.")
        d = self.runs_dir / run_id
        if not (d / "run.json").is_file():
            raise HTTPException(404, "No run has that id.")
        return d

    def ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.parent.name for p in self.runs_dir.glob("*/run.json") if _SAFE_ID.match(p.parent.name))

    def run(self, run_id: str) -> dict:
        """run.json, after every page file it names has been checked against its recorded hash."""
        d = self.run_dir(run_id)
        run = json.loads((d / "run.json").read_text())
        verify_page_files(d, run)
        return run

    def page_files(self, run: dict) -> dict[str, str]:
        """The page files run.json names (relative path -> recorded sha256), the only ones ever served."""
        return {page[key]: page.get(f"{key}_sha256") for src in run.get("sources", [])
                for page in src.get("pages", []) for key in ("image", "words") if page.get(key)}

    def decisions(self, run_id: str) -> list:
        f = self.run_dir(run_id) / "decisions.jsonl"
        if not f.is_file():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]

    def append(self, run_id: str, lines: list[dict]) -> None:
        f = self.run_dir(run_id) / "decisions.jsonl"
        text = "".join(json.dumps(d) + "\n" for d in lines)
        with f.open("a") as out:
            out.write(text)

    def words_doc(self, run_id: str, source: dict | None, page_number) -> dict | None:
        """Parse the exact words bytes verified here; None only when the cited page was not captured."""
        if not source:
            return None
        page = next((p for p in source.get("pages", []) if p.get("page_number") == page_number), None)
        if not page or not page.get("words"):
            return None
        run_dir = self.run_dir(run_id)
        name = page["words"]
        f = (run_dir / name).resolve()
        refuse = f"This run is not served: its file {name}"
        if run_dir.resolve() not in f.parents:
            raise HTTPException(409, f"{refuse} is outside the run's folder.")
        expected = page.get("words_sha256")
        if not expected:
            raise HTTPException(409, f"{refuse} has no hash recorded in run.json.")
        try:
            data = f.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            raise HTTPException(409, f"{refuse} is missing.") from None
        if hashlib.sha256(data).hexdigest() != expected:
            raise HTTPException(409, f"{refuse} does not match the hash recorded in run.json.")
        return json.loads(data.decode("utf-8"))


def _sha256(f: Path) -> str:
    h = hashlib.sha256()
    with f.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_page_files(run_dir: Path, run: dict) -> None:
    """Refuse the run (409) if any page image or words file it names is missing or differs from its hash."""
    root = run_dir.resolve()
    for src in run.get("sources", []):
        for page in src.get("pages", []):
            for key, hash_key in (("image", "image_sha256"), ("words", "words_sha256")):
                name = page.get(key)
                if not name:
                    continue
                f = (run_dir / name).resolve()
                refuse = f"This run is not served: its file {name}"
                if root not in f.parents:
                    raise HTTPException(409, f"{refuse} is outside the run's folder.")
                if not page.get(hash_key):
                    raise HTTPException(409, f"{refuse} has no hash recorded in run.json.")
                if not f.is_file():
                    raise HTTPException(409, f"{refuse} is missing.")
                if _sha256(f) != page[hash_key]:
                    raise HTTPException(409, f"{refuse} does not match the hash recorded in run.json.")


def _find_target(run: dict, target: str) -> tuple[str, dict, dict | None, object] | None:
    """(kind, item, source, page_number) for a row id or a table value id."""
    sources = {s["id"]: s for s in run.get("sources", [])}
    for row in run.get("rows", []):
        if row["id"] == target:
            return "row", row, sources.get(row.get("source_id")), row.get("page_number")
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            for cell in trow.get("cells", []):
                if cell["id"] == target:
                    return "cell", cell, sources.get(table.get("source_id")), table.get("page_number")
    return None


def _cell_context(run: dict, target: str) -> tuple[str | None, list[str] | None]:
    """(row label, table columns) for a table value id: what the row-and-column check compares against."""
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            if any(cell["id"] == target for cell in trow.get("cells", [])):
                return trow.get("label"), list(table.get("columns", [])) or None
    return None, None


def _summary(state: dict) -> dict:
    return {k: state[k] for k in ("counts", "mistakes", "excluded", "exclusions", "unresolved")}


def create_app(runs_dir: Path = RUNS_DIR, ui_dir: Path = UI_DIR, reader=None, background: bool = True) -> FastAPI:
    """`reader`: what a run's job calls (default: Muse Spark, needs a key). `background`: False runs a job to its
    stop inside the request, which is how the tests use it."""
    store = RunStore(runs_dir)
    reader = reader or LiveReader()

    def start_job(run_id: str) -> None:
        if background:
            threading.Thread(target=jobs.run_job, args=(store.runs_dir, run_id, reader), daemon=True).start()
        else:
            jobs.run_job(store.runs_dir, run_id, reader)

    validator = Draft202012Validator(json.loads(DECISION_SCHEMA.read_text()))
    app = FastAPI(title="Proof Reader decisions server", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        out = []
        for run_id in store.ids():
            try:
                run = store.run(run_id)
            except HTTPException as refused:
                out.append({"id": run_id, "refused": refused.detail})
                continue
            meta = run.get("run", {})
            with DECISIONS_LOCK:
                decisions = store.decisions(run_id)
            state = current_state(run, decisions)
            out.append({"id": run_id, "number": meta.get("number"), "label": meta.get("label"),
                        "sample": meta.get("sample"), "started_at": meta.get("started_at"),
                        "finished_at": meta.get("finished_at"), "status": meta.get("status"),
                        "counts": state["counts"],
                        "unresolved": state["unresolved"]})
        return out

    @app.post("/api/runs/new", status_code=202)
    def new_run(body: dict = Body(...)) -> dict:
        try:
            choices = jobs.validate_choices(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        run = jobs.create_run(store.runs_dir, choices)
        start_job(run["run"]["id"])
        return jobs_view(store, run["run"]["id"])

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: dict | None = Body(None)) -> dict:
        store.run_dir(run_id)
        body = body or {}
        if set(body) - {"spending_limit_usd"}:
            raise HTTPException(422, "A resume takes only spending_limit_usd.")
        try:
            jobs.resume_run(store.runs_dir, run_id, body.get("spending_limit_usd"))
        except jobs.JobError as e:
            raise HTTPException(e.status, e.message) from None
        start_job(run_id)
        return jobs_view(store, run_id)

    @app.get("/api/runs/{run_id}/calendar")
    def get_calendar(run_id: str) -> dict:
        run = store.run(run_id)
        with DECISIONS_LOCK:
            decisions = store.decisions(run_id)
        return {"run_id": run_id, "entries": calendar_entries(run, current_state(run, decisions))}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = store.run(run_id)
        with DECISIONS_LOCK:
            decisions = store.decisions(run_id)
        return {**run, "state": current_state(run, decisions)}

    @app.get("/api/runs/{run_id}/decisions")
    def get_decisions(run_id: str) -> list:
        store.run_dir(run_id)
        with DECISIONS_LOCK:
            return store.decisions(run_id)

    @app.post("/api/runs/{run_id}/decisions")
    def post_decision(run_id: str, body: dict = Body(...)) -> dict:
        run = store.run(run_id)
        extra = set(body) - BODY_FIELDS
        if extra:
            raise HTTPException(422, f"Unknown field in the decision: {', '.join(sorted(extra))}.")
        if body.get("by", "person") != "person":
            raise HTTPException(422, "Only a person's decisions are taken here.")
        if body.get("action") not in PERSON_ACTIONS:
            if body.get("action") == "recheck":
                raise HTTPException(422, "Only code writes a re-check.")
            raise HTTPException(422, "The action must be keep, fix, remove or undo.")
        line = {"at": now_utc(), "target": body.get("target"), "action": body["action"],
                "note": body.get("note"), "fix": body.get("fix"), "by": "person"}
        errors = sorted(validator.iter_errors(line), key=lambda e: list(e.path))
        if errors:
            raise HTTPException(422, _schema_reason(errors[0], line))

        with DECISIONS_LOCK:
            return _decide(store, run_id, run, line)

    @app.get("/runs/{run_id}/pages/{name}")
    def page_file(run_id: str, name: str) -> Response:
        run = store.run(run_id)          # refuses the whole run if any page file differs from its hash
        d = store.run_dir(run_id) / "pages"
        want = store.page_files(run).get(f"pages/{name}")
        if not _SAFE_ID.match(name) or ".." in name or want is None or not (d / name).is_file():
            raise HTTPException(404, "No such page file.")
        data = (d / name).read_bytes()   # the bytes sent are the bytes checked, never a later copy
        if hashlib.sha256(data).hexdigest() != want:
            raise HTTPException(409, f"This run is not served: its file pages/{name} does not match the hash "
                                     f"recorded in run.json.")
        media = "image/png" if name.endswith(".png") else "application/json" if name.endswith(".json") else None
        return Response(data, media_type=media or "application/octet-stream")

    if Path(ui_dir).is_dir():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app


def jobs_view(store: RunStore, run_id: str) -> dict:
    """The run object (status, turns, spend, choices) as the job has left it."""
    return json.loads((store.run_dir(run_id) / "run.json").read_text())["run"]


def _decide(store: RunStore, run_id: str, run: dict, line: dict) -> dict:
    """Check the decision against the current state, re-check a fix, append. The caller holds DECISIONS_LOCK."""
    decisions = store.decisions(run_id)
    state = current_state(run, decisions + [line])
    refused = [x for x in state["ignored"] if x["index"] == len(decisions)]
    if refused:
        raise HTTPException(422, refused[0]["reason"])

    written = [line]
    if line["action"] == "fix":
        target = state["targets"][line["target"]]
        kind, item, source, page_number = _find_target(run, line["target"])
        words_doc = store.words_doc(run_id, source, page_number)
        fixed = {**item, **target["values"]}
        fx = target["fixes"][-1]          # this fix; the state rule says whether it settles the disagreement
        old = {**fixed, fx["field"]: fx["from"]}   # the item as it was just before this fix
        if kind == "row":
            rc = recheck_row(now_utc(), fixed, source or {"kind": "other"}, words_doc, fx["settles"],
                             field=fx["field"], before=old)
        else:
            row_label, columns = _cell_context(run, line["target"])
            rc = recheck_cell(now_utc(), fixed, words_doc, fx["settles"], field=fx["field"],
                              page_number=page_number, before=old, row_label=row_label, columns=columns)
        written.append(rc)
        state = current_state(run, decisions + written)
        # The state rule must take the re-check; if it would not, write nothing.
        if any(x["index"] >= len(decisions) for x in state["ignored"]):
            raise HTTPException(500, "The re-check could not be attached to the fix; nothing was written.")
    store.append(run_id, written)
    return {"target": line["target"], "state": state["targets"][line["target"]], "written": written,
            **_summary(state)}


def _schema_reason(err, line: dict) -> str:
    """One plain sentence for the first schema error."""
    where = ".".join(str(p) for p in err.path)
    if where == "target" or (not where and "'target'" in err.message):
        return "A decision needs the id of a row or table value."
    if where.startswith("fix") or line["action"] == "fix":
        if line["action"] == "fix":
            return "A fix needs a field, a from value and a to value (text or null)."
        return f"Only a fix carries a fix; a {line['action']} does not."
    if where == "note":
        return "A note must be text or null."
    return "The decision is not in the expected shape."


app = create_app()
