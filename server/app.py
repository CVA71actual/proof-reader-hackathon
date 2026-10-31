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

import asyncio
import hashlib
import importlib
import inspect
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator

from accuracy import export as accuracy_export
from capture.reader import LiveReader
from capture import voice
from capture.sam import SamReader
from capture.second_reader import SecondReader
from capture.wonder import WonderReader
from runs import prices
from runs.calendar import calendar_entries, run_interval_partition
from runs.ids import require_unique_target_ids
from runs.recheck import recheck_cell, recheck_row
from report.data import report_filenames
from runs.state import PERSON_ACTIONS, as_of_note, current_state
from server import jobs

REPO = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO / "runs"
UI_DIR = REPO / "ui"
DECISION_SCHEMA = RUNS_DIR / "schema" / "decision.schema.json"

_CODE_REF = re.compile(r"^checks/([A-Za-z0-9_]+)\.py:([A-Za-z_][A-Za-z0-9_]*)$")
NO_SUCH_CHECK = "No check code has that reference; only functions under checks/ can be shown."

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
        if (d / "id_repairs.pending.json").exists():
            raise HTTPException(409, "This run has an interrupted ID repair and is not served until repair completes.")
        run = json.loads((d / "run.json").read_text())
        try:
            require_unique_target_ids(run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
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


def _multipart_audio(content_type: str, body: bytes) -> bytes:
    """The bytes of the `audio` part of a multipart/form-data body (b"" when there is none)."""
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    for part in msg.iter_parts() if msg.is_multipart() else []:
        if part.get_param("name", header="content-disposition") == "audio":
            return part.get_payload(decode=True) or b""
    return b""


def _cell_context(run: dict, target: str) -> tuple[str | None, list[str] | None]:
    """(row label, table columns) for a table value id: what the row-and-column check compares against."""
    for table in run.get("tables", []):
        for trow in table.get("rows", []):
            if any(cell["id"] == target for cell in trow.get("cells", [])):
                return trow.get("label"), list(table.get("columns", [])) or None
    return None, None


def _summary(state: dict) -> dict:
    return {k: state[k] for k in ("counts", "mistakes", "excluded", "exclusions", "unresolved")}


def create_app(runs_dir: Path = RUNS_DIR, ui_dir: Path = UI_DIR, reader=None, background: bool = True,
               second_reader="default", coverage_as_of=None, wonder_reader="default",
               sam_reader="default", index=None, transcriber=None, default_run=None) -> FastAPI:
    """`reader`: what a run's job calls (default: Muse Spark, needs a key). `second_reader`: the independent second
    read (default: Muse Spark Open when no `reader` was given, none when one was, so a test with a fake reader never
    reaches the network; None: one reader). `background`: False runs a job to its stop inside the request, which is
    how the tests use it. `coverage_as_of`: a date; the dated-sentence coverage counts dates after it (default: each
    source's published date). `wonder_reader`: the escalation model (default: Muse Wonder when no `reader` was given,
    none when one was; None: no escalation). `sam_reader`: SAM 3.1 for scanned pages (same default rule). `index`: the sources index
    (runs/sources.py; default: runs/sources_index.json, else the built-in one), which says what pages each company has."""
    transcriber = transcriber or voice.transcribe
    voice_locks: dict[str, asyncio.Lock] = {}   # one lock per run_id: voice calls on a run go one at a time
    store = RunStore(runs_dir)
    if second_reader == "default":
        second_reader = SecondReader() if reader is None else None
    if wonder_reader == "default":
        wonder_reader = WonderReader() if reader is None else None
    if sam_reader == "default":
        sam_reader = SamReader() if reader is None else None
    reader = reader or LiveReader()

    def start_job(run_id: str) -> None:
        if background:
            threading.Thread(target=jobs.run_job, args=(store.runs_dir, run_id, reader, second_reader, coverage_as_of, wonder_reader,
                                   sam_reader, index),
                             daemon=True).start()
        else:
            jobs.run_job(store.runs_dir, run_id, reader, second_reader, coverage_as_of, wonder_reader, sam_reader,
                         index)

    validator = Draft202012Validator(json.loads(DECISION_SCHEMA.read_text()))
    app = FastAPI(title="Proof Reader decisions server", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/settings")
    def get_settings() -> dict:
        """The run the app opens on when the address names none (`python -m runs.demo --run X`); null: the UI's own pick."""
        return {"default_run": default_run if default_run in store.ids() else None}

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
            try:
                state = current_state(run, decisions)
            except ValueError as refused:
                out.append({"id": run_id, "refused": str(refused)})
                continue
            out.append({"id": run_id, "number": meta.get("number"), "label": meta.get("label"),
                        "sample": meta.get("sample"), "started_at": meta.get("started_at"),
                        "finished_at": meta.get("finished_at"), "status": meta.get("status"),
                        "as_of_note": as_of_note(run), "counts": state["counts"],
                        "unresolved": state["unresolved"]})
        return out

    @app.post("/api/runs/new", status_code=202)
    def new_run(body: dict = Body(...)) -> dict:
        try:
            body = dict(body)
            as_of = jobs.validate_as_of(body.pop("as_of", None))
            choices = jobs.validate_choices(body, index)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        run = jobs.create_run(store.runs_dir, choices, as_of=as_of)
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

    @app.get("/api/checks/source")
    def check_source(ref: str = "") -> dict:
        """The source text of one check function, for refs like checks/event_checks.py:date_match. Only functions
        defined in a module under checks/ are shown; any other ref is a plain 404."""
        m = _CODE_REF.match(ref or "")
        if not m or not (REPO / "checks" / f"{m[1]}.py").is_file():
            raise HTTPException(404, NO_SUCH_CHECK)
        module = importlib.import_module(f"checks.{m[1]}")
        fn = getattr(module, m[2], None)
        if not inspect.isfunction(fn) or fn.__module__ != module.__name__:
            raise HTTPException(404, NO_SUCH_CHECK)
        return {"ref": ref, "source": inspect.getsource(fn)}

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
        state = current_state(run, decisions)
        partition, window_start, window_end = run_interval_partition(run, state)
        return {**run, "state": state, "calendar_partition": partition,
                "calendar_window": {"start": window_start, "end": window_end},
                "as_of_note": as_of_note(run)}

    @app.get("/api/runs/{run_id}/decisions")
    def get_decisions(run_id: str) -> list:
        store.run(run_id)          # the log is part of the run: refused with the run if a page file differs
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
            # Every page file run.json names is checked again here, at decision time, not only the words file
            # the re-check reads: a file changed since the run was loaded refuses the decision before any write.
            verify_page_files(store.run_dir(run_id), run)
            return _decide(store, run_id, run, line)

    @app.post("/api/runs/{run_id}/voice/decide")
    async def voice_decide(run_id: str, request: Request, target: str = "") -> dict:
        """A spoken decision for one row: the WAV is transcribed and the words become a proposal by rules. Nothing is
        written to decisions.jsonl; the call is priced into the run's spend as one turn."""
        store.run(run_id)
        body = await request.body()
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            body = _multipart_audio(request.headers["content-type"], body)
        async with voice_locks.setdefault(run_id, asyncio.Lock()):
            # One call at a time per run, from reading spend to counting the turn: concurrent calls see each other's spend.
            run = store.run(run_id)
            if _find_target(run, target) is None:
                raise HTTPException(404, "No row or value in this run has that id.")
            limit, spent = run["run"]["choices"].get("spending_limit_usd"), run["run"].get("spend_usd") or 0
            if limit is not None and spent >= limit:
                raise HTTPException(409, "The run is at its spending limit; raise it to keep using voice.")
            job_path = store.run_dir(run_id) / "job.json"
            if job_path.exists() and jobs.limit_unpriced(limit, json.loads(job_path.read_text())):
                raise HTTPException(409, "The run has a model call that could not be priced, so its spend is not "
                                         "known; voice is paused until the run is resumed.")
            try:
                allowed = voice.allowed_ms(voice.check_wav(body))
                # Reserve the most this recording can be billed (the same bound the reply is held to) on the run's
                # budget gate, shared with the run worker: spend + every held reservation must stay inside the limit.
                # two transcriptions worst case: a read timeout may follow a billed request, then the retry is billed too
                worst = 2 * (prices.audio_cost_usd(voice.NAME, allowed) or 0) if limit is not None else 0
                token = jobs.reserve_voice(store.runs_dir, run_id, limit, worst)
                if token is None:
                    raise HTTPException(409, "The run is at its spending limit; raise it to keep using voice.")
            except voice.VoiceError as e:
                raise HTTPException(422, str(e)) from None
            except jobs.JobError as e:
                raise HTTPException(e.status, e.message) from None
            try:
                heard = transcriber(body)
                voice.check_audio_ms(heard.get("audio_ms"), allowed)
            except voice.VoiceError as e:
                jobs.fail_voice(store.runs_dir, run_id, token, getattr(e, "billed_attempts", 0), limit is not None)
                raise HTTPException(422, str(e)) from None
            except BaseException:
                jobs.release_budget(run_id, token)
                raise
            jobs.count_voice_turn(store.runs_dir, run_id, heard, token)
        return {"transcript": heard["transcript"], "proposal": voice.propose(heard["transcript"]),
                "cost_usd": heard["cost_usd"], "audio_ms": heard["audio_ms"]}

    @app.get("/runs/{run_id}/accuracy.json")
    @app.get("/api/runs/{run_id}/accuracy")
    def accuracy_file(run_id: str) -> Response:
        """The run's saved scoring (accuracy/export.py). Served only if the run's page files match their hashes and the
        file's own hashes hold: it is not altered since it was written, and it was scored from this run.json."""
        run = store.run(run_id)
        if run.get("accuracy_stale"):
            raise HTTPException(409, run.get("accuracy_stale_reason") or
                                "This run's accuracy scoring is stale and must be regenerated.")
        data, problem = accuracy_export.read_if_intact(store.run_dir(run_id))
        if problem:
            raise HTTPException(409, problem)
        if data is None:
            raise HTTPException(404, "No accuracy scoring is saved for this run.")
        return Response(data, media_type="application/json")

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

    REPORT_TYPES = {".pdf": "application/pdf",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

    @app.api_route("/runs/{run_id}/report/{name}", methods=["GET", "HEAD"])
    def report_file(run_id: str, name: str) -> Response:
        """The run's saved PDF or Word report, from <run folder>/report/. Only the two file names report/ builds for
        this run are served; any other name, or a report not built yet, is a plain 404. Nothing there is listed."""
        run = store.run(run_id)
        number = run.get("run", {}).get("number")
        if not isinstance(number, int) or name not in report_filenames(number):
            raise HTTPException(404, "No such report file.")
        if run.get("report_stale"):
            raise HTTPException(404, run.get("report_stale_reason") or
                                "This run's report is out of date after a state change and has not been rebuilt.")
        f = store.run_dir(run_id) / "report" / name
        if not f.is_file():
            raise HTTPException(404, "This run has no saved report file with that name.")
        return Response(f.read_bytes(), media_type=REPORT_TYPES[Path(name).suffix])

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
    report_refresh = "no saved reports"
    report_dir = store.run_dir(run_id) / "report"
    has_reports = report_dir.is_dir() and any(p.is_file() and p.suffix in {".pdf", ".docx"}
                                               for p in report_dir.iterdir())
    if has_reports:
        run_path = store.run_dir(run_id) / "run.json"
        latest = json.loads(run_path.read_text())
        latest["report_stale"] = True
        latest["report_stale_reason"] = "A person changed this run's state; its saved report is being rebuilt."
        run_path.write_text(json.dumps(latest, indent=1, ensure_ascii=False) + "\n")
    store.append(run_id, written)
    if has_reports:
        from runs.recheck_dates import keep_reports_honest
        report_refresh = keep_reports_honest(store.run_dir(run_id), line["at"])
    return {"target": line["target"], "state": state["targets"][line["target"]], "written": written,
            "report_refresh": report_refresh, **_summary(state)}


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
