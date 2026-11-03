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
from capture import research_instructions
from capture.wonder import WonderReader
from runs import prices, sources
from runs.calendar import calendar_entries, run_interval_partition
from runs.ids import require_unique_target_ids
from runs.recheck import recheck_cell, recheck_row
from report.data import report_filenames
from runs.state import PERSON_ACTIONS, UNRESOLVED_REASON, as_of_note, current_state
from server import intake as intake_mod, jobs

REPO = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO / "runs"
UI_DIR = REPO / "ui"
DECISION_SCHEMA = RUNS_DIR / "schema" / "decision.schema.json"

_CODE_REF = re.compile(r"^checks/([A-Za-z0-9_]+)\.py:([A-Za-z_][A-Za-z0-9_]*)$")
NO_SUCH_CHECK = "No check code has that reference; only functions under checks/ can be shown."

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BODY_FIELDS = {"target", "action", "note", "fix", "by", "expected_basis"}
# DIALOGS-001: a decision made in a dialog may say which proposal/evidence it was shown (the target's review_basis,
# or just its sha256). When the target has changed since, nothing is written.
STALE_DECISION_REASON = ("This row changed since you opened it: its proposal or evidence is different now. Nothing was "
                         "saved. Look at it again, then decide.")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# One lock for the whole process: a POST holds it from reading decisions.jsonl to appending its lines, and a
# read of decisions.jsonl takes it too, so no request sees a half-written batch or decides on a stale state.
DECISIONS_LOCK = threading.Lock()


ACCURACY_UNBOUND_REASON = ("This Accuracy result was saved before the inputs it scored were recorded. "
                           "Re-score to show it.")
ACCURACY_MISMATCH_REASON = "This Accuracy result does not match the run's current inputs. Re-score to show it."
REPORT_STALE_REASON = "Saved report is out of date. Rebuild it to show the current results."


def report_status(report_dir: Path, run: dict, decisions: list[dict]) -> dict:
    """Read-only presentation derived from the same binding that gates report serving."""
    from report.manifest import report_is_current
    number = run.get("run", {}).get("number")
    saved = isinstance(number, int) and any((report_dir / name).is_file()
                                            for name in report_filenames(number))
    # A verified current bundle answers for itself; the run's report_stale flag
    # only means "a rebuild failed" and matters while no valid bundle exists.
    stale = bool((saved or run.get("report_stale")) and not report_is_current(report_dir, run, decisions))
    return {"report_stale": stale, "report_stale_reason": REPORT_STALE_REASON if stale else None}


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

    def listed_ids(self) -> list[str] | None:
        """The ids runs/LISTED.json says the Saved runs list shows ({"listed": [...]}); None when the file is
        missing (then every run is listed). A run that is not listed stays openable by its id and its API."""
        f = self.runs_dir / "LISTED.json"
        if not f.is_file():
            return None
        try:
            listed = json.loads(f.read_text()).get("listed")
        except (ValueError, AttributeError):
            raise HTTPException(500, "runs/LISTED.json is not an object with a \"listed\" list.") from None
        if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
            raise HTTPException(500, "runs/LISTED.json is not an object with a \"listed\" list.")
        return listed

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

    def list_stamp(self, run_id: str) -> str:
        """Verify saved evidence, then fingerprint the run-list inputs without re-running checks."""
        d = self.run_dir(run_id)
        if (d / "id_repairs.pending.json").exists():
            raise HTTPException(409, "This run has an interrupted ID repair and is not served until repair completes.")
        run_path = d / "run.json"
        run_bytes = run_path.read_bytes()
        run = json.loads(run_bytes)
        try:
            require_unique_target_ids(run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        # A cache hit still validates the exact saved bytes, but avoids parsing each page.
        # A miss goes through run(), which performs the full verified-artifact load.
        for source in run.get("sources", []):
            for page in source.get("pages", []):
                try:
                    if page.get("image"):
                        sources.load_page_image_evidence(d, page)
                    if page.get("words"):
                        sources.load_page_artifact_bytes(d, page["words"], page.get("words_sha256"))
                    if page.get("derived_words"):
                        derived = page["derived_words"]
                        # Same refusal as sources.load_page_evidence: refuse this run only.
                        if not isinstance(derived, dict) or not derived.get("file") or not derived.get("sha256"):
                            raise sources.SourcesError("The page's derived words artifact has no file or hash recorded.")
                        sources.load_page_artifact_bytes(d, derived["file"], derived["sha256"])
                except sources.SourcesError as exc:
                    raise HTTPException(409, str(exc)) from exc
        decisions_path = d / "decisions.jsonl"
        with DECISIONS_LOCK:
            decisions_bytes = decisions_path.read_bytes() if decisions_path.is_file() else b""
        from runs.check_load import list_input_stamp
        return list_input_stamp(run_bytes, run, decisions_bytes)

    def decisions(self, run_id: str, *, refresh_rechecks: bool = True, run: dict | None = None) -> list:
        f = self.run_dir(run_id) / "decisions.jsonl"
        if not f.is_file():
            return []
        decisions = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        if not refresh_rechecks:
            return decisions
        from runs.check_load import bind_decisions_on_load
        return bind_decisions_on_load(run if run is not None else self.run(run_id), decisions,
                                      self.run_dir(run_id))

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
    """Translate the shared saved-page boundary into the API's refusal response."""
    try:
        sources.verify_page_files(run_dir, run)
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
               sam_reader="default", index=None, transcriber=None, default_run=None,
               intake_preparer=None, research_tools=None, research_reader=None) -> FastAPI:
    """`reader`: what a run's job calls (default: Muse Spark, needs a key). `second_reader`: the independent second
    read (default: Muse Spark Open when no `reader` was given, none when one was, so a test with a fake reader never
    reaches the network; None: one reader). `background`: False runs a job to its stop inside the request, which is
    how the tests use it. `coverage_as_of`: a date; the dated-sentence coverage counts dates after it (default: each
    source's published date). `wonder_reader`: the escalation model (default: Muse Wonder when no `reader` was given,
    none when one was; None: no escalation). `sam_reader`: SAM 3.1 for scanned pages (same default rule). `index`: the sources index
    (runs/sources.py; default: runs/sources_index.json, else the built-in one), which says what pages each company has.
    `intake_preparer`: what POST /api/intake uses to reach the SEC (server/intake.py Preparer; default: the live SEC
    fetcher and headless Chrome, in the background)."""
    intake_preparer = intake_preparer or intake_mod.Preparer()
    transcriber = transcriber or voice.transcribe
    voice_locks: dict[str, asyncio.Lock] = {}   # one lock per run_id: voice calls on a run go one at a time
    store = RunStore(runs_dir)
    list_cache: dict[str, tuple[str, dict]] = {}
    created_here: set[str] = set()   # runs New run made in this server's lifetime: listed whatever LISTED.json says
    # A production second reader carries mutable routing flags and the current
    # job's fallback callback. Never share it across independently started jobs.
    default_second_for_job = second_reader == "default" and reader is None
    if second_reader == "default":
        second_reader = None
    if wonder_reader == "default":
        wonder_reader = WonderReader() if reader is None else None
    if sam_reader == "default":
        sam_reader = SamReader() if reader is None else None
    reader = reader or LiveReader()

    # RESEARCHER-FULL-001: what a research run reaches the web and the Responses API with (default: live)
    research_kwargs = {k: v for k, v in (("research_tools", research_tools), ("research_reader", research_reader))
                       if v is not None}

    def start_job(run_id: str, run_index=None) -> None:
        job_second = SecondReader() if default_second_for_job else second_reader
        job_wonder, job_sam = wonder_reader, sam_reader
        try:
            run_choices = json.loads((store.runs_dir / run_id / "run.json").read_text())["run"]["choices"]
        except (OSError, ValueError, KeyError, TypeError):
            run_choices = None
        if jobs.one_model(run_choices):     # ONE-MODEL-RUN-001: Muse Spark is the only model called, start and resume
            job_second = job_wonder = job_sam = None
        job_index = run_index if run_index is not None else index
        if background:
            threading.Thread(target=jobs.run_job, args=(store.runs_dir, run_id, reader, job_second, coverage_as_of, job_wonder,
                                   job_sam, job_index), daemon=True,
                             **({"kwargs": research_kwargs} if research_kwargs else {})).start()
        else:
            jobs.run_job(store.runs_dir, run_id, reader, job_second, coverage_as_of, job_wonder, job_sam,
                         job_index, **research_kwargs)

    validator = Draft202012Validator(json.loads(DECISION_SCHEMA.read_text()))
    app = FastAPI(title="Proof Reader decisions server", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/settings")
    def get_settings() -> dict:
        """The run the app opens on when the address names none (`python -m runs.demo --run X`); null: the UI's own pick."""
        return {"default_run": default_run if default_run in store.ids() else None,
                "research_instructions_default": research_instructions.DEFAULT,
                "research_instructions_max_length": research_instructions.MAX_LENGTH}

    @app.get("/api/research/options")
    def research_options() -> dict:
        """LETTUCE-OPTION: the optional research settings a New Run screen can offer (support_check, off by default)."""
        from runs import research
        return research.support_check_options()

    @app.get("/api/companies")
    def get_companies() -> dict:
        """The selectable companies of the sources index a new run will read. The New
        Run dialog renders this list, so no company name is hard-coded in the UI."""
        try:
            idx = index or sources.load_index()
        except sources.SourcesError as exc:
            raise HTTPException(409, str(exc)) from exc
        selectable, listed_without_pages = jobs.selectable_companies(idx)   # the same rule as ["all"]
        return {"companies": sorted(selectable), "listed_without_pages": sorted(listed_without_pages)}

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        """Every run, or only those runs/LISTED.json names plus the ones New run made since this server started."""
        out = []
        listed = store.listed_ids()
        for run_id in store.ids():
            if listed is not None and run_id not in listed and run_id not in created_here:
                continue
            try:
                stamp = store.list_stamp(run_id)
                cached = list_cache.get(run_id)
                if cached and cached[0] == stamp:
                    out.append(dict(cached[1]))
                    continue
                run = store.run(run_id)
            except HTTPException as refused:
                out.append({"id": run_id, "refused": refused.detail})
                continue
            meta = run.get("run", {})
            with DECISIONS_LOCK:
                decisions = store.decisions(run_id, run=run)
            try:
                state = current_state(run, decisions)
            except ValueError as refused:
                out.append({"id": run_id, "refused": str(refused)})
                continue
            item = {"id": run_id, "number": meta.get("number"), "label": meta.get("label"),
                    "sample": meta.get("sample"), "started_at": meta.get("started_at"),
                    "finished_at": meta.get("finished_at"), "status": meta.get("status"),
                    "as_of_note": as_of_note(run), "counts": state["counts"],
                    "unresolved": state["unresolved"]}
            list_cache[run_id] = (stamp, item)
            out.append(item)
        for stale_id in set(list_cache) - set(store.ids()):
            list_cache.pop(stale_id, None)
        return out

    @app.post("/api/intake", status_code=202)
    def new_intake(body: dict = Body(...)) -> dict:
        """TICKER-INTAKE-001: resolve and capture pasted tickers (server/intake.py). No model call."""
        if not isinstance(body, dict) or set(body) != {"tickers"}:
            raise HTTPException(422, 'An intake takes only "tickers": the pasted text.')
        try:
            rec = intake_mod.start(store.runs_dir, body["tickers"], intake_preparer)
        except intake_mod.IntakeError as e:
            raise HTTPException(e.status, e.detail) from None
        return {"intake_id": rec["intake_id"]}

    @app.get("/api/intake/{intake_id}")
    def get_intake(intake_id: str) -> dict:
        try:
            return intake_mod.view(intake_mod.read(store.runs_dir, intake_id))
        except intake_mod.IntakeError as e:
            raise HTTPException(e.status, e.detail) from None

    @app.post("/api/runs/new", status_code=202)
    def new_run(body: dict = Body(...)) -> dict:
        """With "intake_id", the run reads only that finished intake's ready pages (a copy in the run's own folder);
        a run that leaves out a requested ticker also needs "accept_ready_subset": true."""
        try:
            body = dict(body)
            as_of = jobs.validate_as_of(body.pop("as_of", None))
            intake_id = body.pop("intake_id", None)
            accept_subset = body.pop("accept_ready_subset", None)
            rec = left_out = None
            if intake_id is None:
                if accept_subset is not None:
                    raise ValueError("accept_ready_subset goes with an intake_id.")
                choices = jobs.validate_choices(body, index)
            else:
                rec, intake_index = intake_mod.ready_index(store.runs_dir, intake_id)
                choices = jobs.validate_choices(body, intake_index)
                left_out = intake_mod.check_subset(rec, choices["companies"],
                                                   False if accept_subset is None else accept_subset)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except intake_mod.IntakeError as e:
            return intake_refusal(e)
        run_index = None
        if rec is None:
            run_id = jobs.create_run(store.runs_dir, choices, as_of=as_of)["run"]["id"]
        else:
            # one run per intake: a replay (lost response, double click, second tab) gets 409 and the first run's id
            try:
                run_id = intake_mod.bind_run(store.runs_dir, intake_id, lambda: jobs.create_run(
                    store.runs_dir, choices, as_of=as_of)["run"]["id"])
            except intake_mod.IntakeError as e:
                return intake_refusal(e)
            run_index = intake_mod.snapshot_into_run(store.runs_dir / run_id, store.runs_dir, rec,
                                                     choices["companies"], left_out)
        created_here.add(run_id)
        start_job(run_id, run_index)
        return jobs_view(store, run_id)

    def intake_refusal(e: "intake_mod.IntakeError"):
        if e.run_id is None:
            raise HTTPException(e.status, e.detail) from None
        return JSONResponse({"detail": e.detail, "run_id": e.run_id}, status_code=e.status)

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

    @app.post("/api/runs/{run_id}/research-stop", status_code=202)
    def research_stop(run_id: str) -> dict:
        """A person stops a research run: every company stops before its next model call; nothing is published."""
        run = json.loads((store.run_dir(run_id) / "run.json").read_text())
        if run["run"].get("choices", {}).get("mode") != "research":
            raise HTTPException(409, "This is not a research run.")
        if run["run"].get("status") not in ("queued", "running"):
            raise HTTPException(409, "This research run is not going.")
        from runs import research
        return research.request_stop(store.runs_dir, run_id)

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
        support = {}
        if (run["run"].get("choices") or {}).get("support_check"):
            from runs.research import support_view       # LETTUCE-OPTION: a finding is shown only while current
            # IDENTITY-001: bound to the effective proposal (a person's current fixes included), as the checks are
            support = {"support_findings": {
                r["id"]: support_view(run, r, (state["targets"].get(r["id"]) or {}).get("values"))
                for r in run.get("rows", []) if (r.get("research") or {}).get("support_check")}}
        return {**run, **report_status(store.run_dir(run_id) / "report", run, decisions), **support,
                "state": state, "unknown_cost_accepted": accepted, "calendar_partition": partition,
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
            return _decide(store, run_id, run, line, expected_basis=body.get("expected_basis"))

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
        with DECISIONS_LOCK:
            decisions = store.decisions(run_id)
        report_dir = store.run_dir(run_id) / "report"
        # One verification: the exact bytes read and hashed here are the bytes served.
        # Same rule as report_status: any saved artifact makes an unbound bundle stale.
        from report.manifest import verified_report_bytes
        saved = any((report_dir / n).is_file() for n in report_filenames(number))
        data = verified_report_bytes(report_dir, run, decisions, name)
        if data is None and (saved or run.get("report_stale")):
            raise HTTPException(404, REPORT_STALE_REASON, headers={"X-Report-Reason": REPORT_STALE_REASON})
        if data is None:
            raise HTTPException(404, "This run has no saved report file with that name.")
        return Response(data, media_type=REPORT_TYPES[Path(name).suffix])

    if Path(ui_dir).is_dir():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app


def jobs_view(store: RunStore, run_id: str) -> dict:
    """The run object (status, turns, spend, choices) as the job has left it."""
    return json.loads((store.run_dir(run_id) / "run.json").read_text())["run"]


def _expected_sha(expected) -> str | None:
    if isinstance(expected, str):
        return expected
    if isinstance(expected, dict) and isinstance(expected.get("sha256"), str):
        return expected["sha256"]
    raise HTTPException(422, "expected_basis must be the row's review_basis or its sha256.")


def _decide(store: RunStore, run_id: str, run: dict, line: dict, expected_basis=None) -> dict:
    """Check the decision against the current state, re-check a fix, append. The caller holds DECISIONS_LOCK.

    DIALOGS-001: with expected_basis (what the person was shown), the decision is refused with 409, and nothing is
    written, unless it equals the target's current review_basis. Without it, callers work as before."""
    decisions = store.decisions(run_id)
    if expected_basis is not None:
        want = _expected_sha(expected_basis)
        shown_target = current_state(run, decisions)["targets"].get(line.get("target"))
        if shown_target is None:
            raise HTTPException(422, "No row or table value has that id.")
        if (shown_target.get("review_basis") or {}).get("sha256") != want:
            raise HTTPException(409, STALE_DECISION_REASON)
    if line["action"] == "keep":
        before = current_state(run, decisions)
        target = before["targets"].get(line.get("target"))
        if target and target["kind"] == "row":
            # This value is always server-computed; never trust a supplied hash
            # or stamp legacy Keeps during load. Preserve the exact effective
            # proposal/evidence accepted at this decision's write boundary.
            line = {**line, "review_basis": target["review_basis"]}
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
