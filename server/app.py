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

Before a run is used for anything (listed, shown, decided on, or a page file served), its declared image, captured
words and derived-words artifacts pass the shared verified-artifact loader. If one is missing or differs from its
recorded hash, the whole run is refused with 409 and a sentence naming the file; no file of that run is served,
and no decision is written to it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator

from accuracy import export as accuracy_export
from capture.reader import LiveReader
from capture import voice
from capture.sam import SamReader
from capture.second_reader import SecondReader
from capture.wonder import WonderReader
from runs import prices, sources
from runs.calendar import calendar_entries, run_interval_partition
from runs.ids import require_unique_target_ids
from runs.recheck import recheck_cell, recheck_row
from report.data import report_filenames
from runs.state import PERSON_ACTIONS, UNRESOLVED_REASON, as_of_note, current_state
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


ACCURACY_UNBOUND_REASON = ("This Accuracy result was saved before the inputs it scored were recorded. "
                           "Re-score to show it.")
ACCURACY_MISMATCH_REASON = "This Accuracy result does not match the run's current inputs. Re-score to show it."


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
        from runs.check_load import bind_on_load
        try:
            bind_on_load(run, d, refuse_invalid_artifact=True)
        except sources.SourcesError as exc:
            raise HTTPException(409, str(exc)) from exc
        return run

    def page_files(self, run: dict) -> dict[str, str]:
        """The page files run.json names (relative path -> recorded sha256), the only ones ever served."""
        return {page[key]: page.get(f"{key}_sha256") for src in run.get("sources", [])
                for page in src.get("pages", []) for key in ("image", "words") if page.get(key)}

    def decisions(self, run_id: str, *, refresh_rechecks: bool = True) -> list:
        f = self.run_dir(run_id) / "decisions.jsonl"
        if not f.is_file():
            return []
        decisions = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        if not refresh_rechecks:
            return decisions
        from runs.check_load import bind_decisions_on_load
        return bind_decisions_on_load(self.run(run_id), decisions, self.run_dir(run_id))

    def append(self, run_id: str, lines: list[dict]) -> None:
        f = self.run_dir(run_id) / "decisions.jsonl"
        text = "".join(json.dumps(d) + "\n" for d in lines)
        with f.open("a") as out:
            out.write(text)

    def words_doc(self, run_id: str, source: dict | None, page_number) -> dict | None:
        """Use the shared verified-artifact boundary; None only when the cited page was not captured."""
        if not source:
            return None
        page = next((p for p in source.get("pages", []) if p.get("page_number") == page_number), None)
        if not page or not page.get("words"):
            return None
        try:
            words, _identity = sources.load_page_evidence(self.run_dir(run_id), page)
            return words
        except sources.SourcesError as exc:
            raise HTTPException(409, str(exc)) from exc


def verify_page_files(run_dir: Path, run: dict) -> None:
    """Refuse the run (409) if any page image or words file it names is missing or differs from its hash."""
    for src in run.get("sources", []):
        for page in src.get("pages", []):
            try:
                if page.get("words") and not page.get("words_sha256"):
                    raise HTTPException(409, f"This run is not served: its file {page['words']} has no hash recorded in run.json.")
                if page.get("image") and not page.get("image_sha256"):
                    raise HTTPException(409, f"This run is not served: its file {page['image']} has no hash recorded in run.json.")
                if page.get("words") or page.get("derived_words"):
                    sources.load_page_evidence(run_dir, page)
                elif page.get("image"):
                    sources.load_page_image_evidence(run_dir, page)
            except HTTPException:
                raise
            except sources.SourcesError as exc:
                raise HTTPException(409, str(exc)) from exc


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
        if set(body) - {"spending_limit_usd", "accept_unknown_cost"}:
            raise HTTPException(422, "A resume takes only spending_limit_usd and accept_unknown_cost.")
        if not isinstance(body.get("accept_unknown_cost", False), bool):
            raise HTTPException(422, "accept_unknown_cost must be true or false.")
        try:
            # Job 51g: the run is compared with its OWN frozen index (job.json source_freeze), never this server's
            # default; `index` is used only for a run frozen on an index with no file
            jobs.resume_run(store.runs_dir, run_id, body.get("spending_limit_usd"),
                            body.get("accept_unknown_cost", False), index)
        except jobs.JobError as e:
            if e.field is not None:
                raise HTTPException(422, e.message) from None      # a field only the caller can supply is missing
            raise HTTPException(e.status, e.message) from None
        start_job(run_id)
        return jobs_view(store, run_id)

    @app.post("/api/runs/{run_id}/operator-stop")
    def operator_stop(run_id: str) -> dict:
        """Job 51f: a run left "running" by a worker stopped from outside, put right from runs/<id>/OPERATOR_STOP.json
        (jobs.record_operator_stop, the same step as python -m server.run_steps). It never resumes the run."""
        store.run_dir(run_id)
        try:
            jobs.record_operator_stop(store.runs_dir, run_id)
        except jobs.JobError as e:
            raise HTTPException(e.status, e.message) from None
        return jobs_view(store, run_id)

    @app.post("/api/runs/{run_id}/source-freeze")
    def source_freeze(run_id: str, body: dict = Body(...)) -> dict:
        """Job 51f: record the source set of a run started before it was recorded, from this server's sources index,
        once a person has checked the index hash by hand (`index_sha256`)."""
        store.run_dir(run_id)
        if set(body) != {"index_sha256"}:
            raise HTTPException(422, "A source freeze takes only index_sha256.")
        try:
            jobs.record_source_freeze(store.runs_dir, run_id, index or sources.load_index(), body["index_sha256"])
        except (jobs.JobError, sources.SourcesError) as e:
            raise HTTPException(getattr(e, "status", 409), getattr(e, "message", str(e))) from None
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
        job_path = store.run_dir(run_id) / "job.json"
        # Job 51e: how many unpriced calls the person accepted, so the UI's marker reads 'cost unknown' until then
        accepted = jobs.accepted_count(json.loads(job_path.read_text())) if job_path.exists() else 0
        return {**run, "state": state, "unknown_cost_accepted": accepted, "calendar_partition": partition,
                "calendar_window": ({"start": window_start, "end": window_end} if window_start else
                                    {"start": None, "end": None, "unresolved": True,
                                     "reason": UNRESOLVED_REASON}),
                "as_of_note": as_of_note(run)}

    @app.get("/api/runs/{run_id}/decisions")
    def get_decisions(run_id: str) -> list:
        store.run(run_id)          # the log is part of the run: refused with the run if a page file differs
        with DECISIONS_LOCK:
            return store.decisions(run_id, refresh_rechecks=False)

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

        def refuse(detail: str, unbound: bool = False) -> JSONResponse:
            # `reason` is the sentence for the screen; `detail` carries the field-level cause for logs only
            return JSONResponse({"reason": ACCURACY_UNBOUND_REASON if unbound else ACCURACY_MISMATCH_REASON,
                                 "detail": detail}, status_code=409)
        if run.get("accuracy_stale"):
            return refuse(run.get("accuracy_stale_reason") or
                          "This run's accuracy scoring is stale and must be regenerated.")
        data, problem = accuracy_export.read_if_intact(store.run_dir(run_id))
        if problem:
            return refuse(problem, unbound=problem.startswith(accuracy_export.UNBOUND))
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
        try:
            page = next(p for src in run.get("sources", []) for p in src.get("pages", [])
                        if p.get("image") == f"pages/{name}" or p.get("words") == f"pages/{name}")
            data = (sources.load_page_image_evidence(store.run_dir(run_id), page)[0]
                    if page.get("image") == f"pages/{name}"
                    else sources.load_page_artifact_bytes(store.run_dir(run_id), f"pages/{name}", want))
        except (sources.SourcesError, StopIteration) as exc:
            raise HTTPException(409 if isinstance(exc, sources.SourcesError) else 404,
                                str(exc) if isinstance(exc, sources.SourcesError) else "No such page file.") from exc
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
        from report.manifest import report_is_current
        with DECISIONS_LOCK:
            decisions = store.decisions(run_id)
        if not report_is_current(store.run_dir(run_id) / "report", run, decisions):
            raise HTTPException(404, "This saved report is not verified against the current run state.")
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
        page = next((p for p in (source or {}).get("pages", [])
                     if p.get("page_number") == page_number), None)
        try:
            words_doc, consumed_artifact = (sources.load_page_evidence(store.run_dir(run_id), page)
                                            if page else (None, None))
        except sources.SourcesError as exc:
            raise HTTPException(409, str(exc)) from exc
        # Re-check the actual candidate plus active human edits. `values` is the editable
        # view and may contain derived period display bounds that were not candidate inputs.
        fixed = {**item, **{edit["field"]: edit["to"] for edit in target["fixes"]}}
        fx = target["fixes"][-1]          # this fix; the state rule says whether it settles the disagreement
        old = {**fixed, fx["field"]: fx["from"]}   # the item as it was just before this fix
        if kind == "row":
            rc = recheck_row(now_utc(), fixed, source or {"kind": "other"}, words_doc, fx["settles"],
                             field=fx["field"], before=old, consumed_artifact=consumed_artifact)
        else:
            row_label, columns = _cell_context(run, line["target"])
            rc = recheck_cell(now_utc(), fixed, words_doc, fx["settles"], field=fx["field"],
                              page_number=page_number, before=old, row_label=row_label, columns=columns,
                              visual_evidence=(page or {}).get("visual_evidence"),
                              derived_words_present=bool((page or {}).get("derived_words")))
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
