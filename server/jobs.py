"""R6 run controls: start a run from the New run panel's choices, stop it at its limits, resume it.

POST /api/runs/new calls create_run (a run folder with run.json status "queued", an empty decisions.jsonl and
job.json) and then run_job, which reads the sources already captured for the chosen companies through the
existing readers (PTGX page 3: events, IONS page 95: table) and builds the rows with the same functions the
builders use (runs.build_entry.build_row, runs.build_run.build_table). The checks are code and run on every row;
nothing the person writes can switch one off.

What the run carries in run.json's "run" object, while it is going:
    status          queued | running | paused_turn_limit | needs_input | done | failed
    status_reason   one plain sentence for the status
    turn_count      model calls used in the current budget; turn_limit the budget (default 40);
                    turns_total every call in the run; resumes how many times it was resumed
    second_reader   optional: {model, status read | not_run | unreadable, reason}: whether the second reader (Muse
                    Spark Open, runs/compare.py) read the run's pages; its read is one more turn per page, counted
                    and priced like the first reader's, and covered by the same limits
    spend_usd       the sum of each call's cost from runs/prices.py (null when the model has no price there),
                    against choices.spending_limit_usd
    trail           a Person step each time a person raises the spending limit on resume
Every model call is one turn. Before each call the job checks, in this order: the reader is ready (a key),
the turn budget, the spending limit. It stops with a plain reason, never silently.

Three refusals: a page whose answer could not be read three times in the run gets one row, group "open",
status "unreadable", with THREE_REFUSALS as its reason, so it goes to a person.

run.json is rewritten only by this job, and only while the run is going; once the status is "done" it is never
written again. job.json holds what the job needs to carry on (unreadable answers per page, a log of stops).
Both files are replaced whole (write a temp file, then rename), so a read never sees half a file. Every rewrite
of run.json also appends one line to job.json's log (status, turn_count, spend_usd, the reason if there is one).

Legacy category/window choices remain readable but do not narrow new research or remove proposals.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import calendar as _calendar
from datetime import date, datetime, timezone
from pathlib import Path

from accuracy import types as T
from capture import read_events, read_table, reader_tools, sam as sam_module
from capture.reader import Reply
from capture.http import MAX_COMPLETION_TOKENS
from capture.second_reader import CAP_REACHED, OPENROUTER_CAP_USD, UNPRICED
from checks.common import normalize
from checks.coverage import sentence_covered, sentences as coverage_sentences
from capture.capture_source import load_entry
from checks import event_checks
from runs import picture, build_entry, build_run, compare, coverage, escalate, prices, scan, sources
from runs.ids import reserve_row_numbers, require_unique_target_ids
from runs.state import run_reference_date

DEFAULT_TURN_LIMIT = 40
MAX_PAGE_FAILURES = 2      # a page that stops the run this many times is skipped, and the run goes on with the rest
UNREADABLE_LIMIT = 3
THREE_REFUSALS = "The reader's answer could not be read three times; sent to a person."
CALLS_EXHAUSTED = "The reader used its {cap} model calls on this page without a readable answer; sent to a person."
RESUMABLE = ("paused_turn_limit", "needs_input", "failed")
TIME_WINDOW_MAX = 120

# Which pages exist for which company is the sources index (runs/sources.py): runs/sources_index.json when it exists, else
# the built-in one (PTGX 8-K exhibit page 3, IONS annual report page 95, and the PTGX page as a scan: no text layer, regions
# from SAM 3.1). "events" pages go to the event reader (both readers), "table" pages to the table reader.
CHOICE_FIELDS = ("companies", "event_types", "time_window_months", "spending_limit_usd", "note_for_agent",
                 "reader_tools")

# One lock for the run.json / job.json files of every run in this process.
RUN_LOCK = threading.RLock()

# The per-run budget gate, shared by the run worker and the voice route. Before either paid call, under RUN_LOCK, the
# call's bounded worst case is added here as a reservation; the call is refused if spend + reservations would pass the
# limit. The reservation is released when the call is counted (or abandoned).
MODEL_CALL_UNBOUNDED = math.inf      # a reader call has no token cap, so its reservation is unbounded in the table
_RESERVED: dict[str, dict[int, float]] = {}
_NEXT_TOKEN = [0]


def reserve_budget(run_id: str, spent: float, limit, worst: float, alone_may_cross: bool = False) -> int | None:
    """Reserve `worst` dollars of the run's limit. Returns a token to release, or None when spend + the reservations
    already held + `worst` would pass the limit. With no limit, nothing is held back (a token is still returned).
    `alone_may_cross`: a reader call keeps the run's long-standing rule when nothing else is in flight (it is made while
    spend is under the limit, and the call that crosses stops the run); once another call holds a reservation, its own
    worst case counts too, so two calls cannot both pass against the same balance."""
    with RUN_LOCK:
        held = _RESERVED.setdefault(run_id, {})
        others = sum(held.values())
        own = 0 if alone_may_cross and not others else worst
        if limit is not None:
            total = round((spent or 0) + others + own, prices.ROUND_TO)
            if (total > limit) if own else ((spent or 0) >= limit):
                return None
        _NEXT_TOKEN[0] += 1
        held[_NEXT_TOKEN[0]] = worst
        return _NEXT_TOKEN[0]


def release_budget(run_id: str, token: int | None) -> None:
    if token is None:
        return
    with RUN_LOCK:
        held = _RESERVED.get(run_id, {})
        held.pop(token, None)
        if not held:
            _RESERVED.pop(run_id, None)


VOICE_BUSY_MESSAGE = "A model call is in progress; try the spoken decision again in a moment."


def reserve_voice(runs_dir: Path, run_id: str, limit, worst: float) -> int | None:
    """The voice route's side of the gate: spend is read from run.json in the same lock that takes the reservation.
    Refused (JobError 409) while a model call is in flight: its cost has no bound, so nothing may be added beside it."""
    with RUN_LOCK:
        if any(v == math.inf for v in _RESERVED.get(run_id, {}).values()):
            raise JobError(409, VOICE_BUSY_MESSAGE)
        spent = json.loads((Path(runs_dir) / run_id / "run.json").read_text())["run"].get("spend_usd") or 0
        return reserve_budget(run_id, spent, limit, worst)


class JobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def window_reason(row: dict, now: date, last: date, months: int) -> str | None:
    """Why the time window leaves this row out (None: it stays). A row is outside only if its period ENDS before
    `now` or STARTS after `last` (now plus the window); an exact day is both its start and its end."""
    try:
        day = date.fromisoformat(row["date"]) if row.get("date") else None
    except ValueError:
        day = None
    if day is None:
        return None
    end = event_checks.period_last_day(row)
    if end is None or end < day:
        end = day
    if event_checks.in_window(day, end, now, last):
        return None
    if end < now:
        if end != day:
            return f"Outside the time window: the period ends {end}, before today ({now})."
        return f"Outside the time window: the date {row['date']} is before today ({now})."
    return (f"Outside the time window: the date {row['date']} is later than {months} "
            f"month{'s' if months != 1 else ''} from today ({last}).")


def past_reason(row: dict, run_date: date) -> str | None:
    """Code exclusion only when the event's full period ended strictly before the run date."""
    # A parsed value is only a proposal until the date check supports it. In
    # particular, a failed or uncheckable date must remain reviewable.
    date_check = next((c for c in row.get("checks", [])
                       if c.get("name") == "date_match" or c.get("id") == "date_match"
                       or c.get("label") == "Date read as written"), None)
    if not date_check or date_check.get("result") != "pass":
        return None
    return past_candidate_reason(row, run_date)


def past_candidate_reason(row: dict, run_date: date) -> str | None:
    """The date-only past rationale, used as a proposal when date_match is not pass."""
    try:
        start = date.fromisoformat(row["date"]) if row.get("date") else None
    except (TypeError, ValueError):
        return None
    if start is None:
        return None
    end = event_checks.period_last_day(row) or start
    if end >= run_date:
        return None
    if end != start:
        return f"Already past: the event period ended {end.isoformat()} before this run began ({run_date.isoformat()})."
    return f"Already past: the event date {start.isoformat()} was before this run began ({run_date.isoformat()})."


def today() -> date:
    return datetime.now(timezone.utc).date()


def add_months(d: date, months: int) -> date:
    return event_checks.add_months_to(d, months)


def price_name(model: str) -> str:
    """The name runs/prices.py knows a model by (a model id it does not know is returned as it is)."""
    model = model.split("/")[-1]       # meta-models/muse-spark-open-1026 and meta/muse-spark-open
    if model.startswith("sam-"):
        return "SAM 3.1"
    if model.startswith("muse-wonder"):
        return "Muse Wonder"
    if model.startswith("muse-spark"):
        return "Muse Spark Open" if "open" in model else "Muse Spark"
    return model


def snapshot(meta: dict, reason: str | None = None) -> dict:
    """One line of job.json's log: every rewrite of run.json leaves one, so no state is lost between snapshots."""
    line = {"at": now_utc(), "status": meta["status"], "turn_count": meta["turn_count"],
            "spend_usd": meta["spend_usd"]}
    if reason:
        line["reason"] = reason
    return line


def money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.01 or x == 0 else f"${x:.4f}"


def _write(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _number(v, *, integer: bool = False) -> bool:
    return isinstance(v, int if integer else (int, float)) and not isinstance(v, bool)


def _finite_limit(v) -> bool:
    """A spending limit: a number of dollars, finite, zero or more."""
    if not _number(v):
        return False
    try:
        return math.isfinite(v) and v >= 0
    except OverflowError:       # an integer too large to be a float (JSON 1 followed by 1000 zeros)
        return False


def validate_choices(body: dict, index: sources.Index | None = None) -> dict:
    """The New run panel's choices, or a ValueError with one plain sentence. `companies` may be ["all"]: every company in
    the sources index (the scanned variant of a page is not one of them), recorded as the list it stands for."""
    index = index or sources.load_index()
    if not isinstance(body, dict):
        raise ValueError("The choices must be a JSON object.")
    extra = set(body) - set(CHOICE_FIELDS)
    if extra:
        raise ValueError(f"Unknown choice: {', '.join(sorted(extra))}.")
    companies, types = body.get("companies"), body.get("event_types", list(read_events.EVENT_TYPES))
    if not isinstance(companies, list) or not companies or not all(isinstance(c, str) for c in companies):
        raise ValueError("Choose at least one company.")
    if companies == ["all"]:
        # every company with a page to read; one the index lists with no page (ABVX in the captured index) is left out
        companies = [t for t in index.tickers if any(p["kind"] != "scan_events" for p in index.pages(t))]
        if not companies:
            raise ValueError("The sources index holds no page to read.")
    empty = [c for c in companies if c in index.empty and c not in index.tickers]
    unknown = [c for c in companies if c not in index.tickers and c not in index.empty]
    if unknown:
        raise ValueError(f"No source has been captured for {', '.join(unknown)} yet. "
                         f"Captured so far: {', '.join(sorted(index.tickers))}.")
    if empty:
        raise ValueError(f"The sources index lists {', '.join(empty)} but holds no page for "
                         f"{'it' if len(empty) == 1 else 'them'} yet, so there is nothing to read.")
    if len(set(companies)) != len(companies):
        raise ValueError("A company is listed twice.")
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise ValueError("event_types must be a list of event types.")
    bad = [t for t in types if t not in read_events.EVENT_TYPES]
    if bad:
        raise ValueError(f"Unknown event type: {', '.join(bad)}. Known: {', '.join(read_events.EVENT_TYPES)}.")
    window, limit, note = (body.get("time_window_months"), body.get("spending_limit_usd"),
                           body.get("note_for_agent"))
    if window is not None and not (_number(window, integer=True) and 1 <= window <= TIME_WINDOW_MAX):
        raise ValueError(f"The time window must be a whole number of months from 1 to {TIME_WINDOW_MAX}, or null.")
    if limit is not None and not _finite_limit(limit):
        raise ValueError("The spending limit must be a finite number of dollars, zero or more, or null.")
    if note is not None and not isinstance(note, str):
        raise ValueError("The note for the agent must be text or null.")
    tools = body.get("reader_tools", False)
    if tools is None:
        tools = False
    if not isinstance(tools, bool):
        raise ValueError("reader_tools must be true or false.")
    return {"companies": list(companies), "event_types": list(read_events.EVENT_TYPES), "time_window_months": 6,
            "spending_limit_usd": limit, "note_for_agent": note.strip() or None if isinstance(note, str) else None,
            "reader_tools": tools}


def label_for(number: int, choices: dict) -> str:
    """The run's label, from its choices: "12 companies · 6 months", "PTGX · 12 months", "IONS" (no window)."""
    companies = choices["companies"]
    who = companies[0] if len(companies) == 1 else f"{len(companies)} companies"
    window = choices.get("time_window_months")
    return f"{who} · {window} month{'s' if window != 1 else ''}" if window else who


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_as_of(value) -> str | None:
    """The run's reference date as ISO text (YYYY-MM-DD), None when not given, or a ValueError."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("as_of must be a date written YYYY-MM-DD, or null.") from None


def create_run(runs_dir: Path, choices: dict, turn_limit: int | None = None, run_id: str | None = None,
               as_of: str | None = None) -> dict:
    """Make runs/<id>/ with run.json (queued), an empty decisions.jsonl and job.json. Returns the run.json dict.
    `turn_limit`: the budget of model calls before the run pauses (default DEFAULT_TURN_LIMIT). `run_id`: the folder's
    name (default run-<number>), for a run that is to be kept under a name, such as twelve-1; ValueError if it is not a
    plain name or the folder is there already. The run's number is the next in the sequence either way."""
    if run_id is not None and (not _RUN_ID.match(run_id) or ".." in run_id):
        raise ValueError(f"The run id {run_id!r} is not a plain name (letters, digits, dot, dash, underscore).")
    runs_dir = Path(runs_dir)
    with RUN_LOCK:
        numbers = []
        for f in runs_dir.glob("*/run.json"):
            try:
                numbers.append(int(json.loads(f.read_text())["run"]["number"]))
            except (ValueError, KeyError, TypeError):
                continue
        number = max(numbers, default=0) + 1
        if run_id is not None:
            if (runs_dir / run_id).exists():
                raise ValueError(f"There is already a folder {run_id} under {runs_dir.name}/; a saved run is never "
                                 f"overwritten.")
        else:
            while (runs_dir / f"run-{number}").exists():
                number += 1
        run_dir = runs_dir / (run_id or f"run-{number}")
        (run_dir / "pages").mkdir(parents=True)
        now = now_utc()
        as_of = validate_as_of(as_of)
        run = {
            "schema_version": build_run.SCHEMA_VERSION,
            "run": {
                "id": run_dir.name, "number": number, "label": label_for(number, choices),
                "sample": False, "started_at": now, "finished_at": None, "choices": choices, "spend_usd": None,
                "prices": prices.snapshot(),
                "status": "queued", "status_reason": "Waiting to start.", "turn_count": 0,
                "turn_limit": DEFAULT_TURN_LIMIT if turn_limit is None else turn_limit, "turns_total": 0, "resumes": 0,
            },
            "sources": [], "tables": [], "rows": [], "mistakes": [], "exclusions": [],
        }
        if as_of:       # the one reference date (runs.state.run_reference_date); absent = the start day
            run["run"]["as_of"] = as_of
        (run_dir / "decisions.jsonl").write_text("")
        _write(run_dir / "job.json", {"attempts": {}, "next_row_number": 1, "unpriced_calls": 0,
                                      "second": {}, "openrouter_spend_usd": 0.0,
                                      "openrouter_unpriced_calls": 0,
                                      "log": [snapshot(run["run"], "Run created.")]})
        _write(run_dir / "run.json", run)
        return run


def limit_unpriced(limit, job: dict) -> bool:
    """True when a run has a spending limit but a call that could not be priced: its spend total is not reliable."""
    unpriced = job.get("unpriced_calls")
    return limit is not None and isinstance(unpriced, (int, float)) and not isinstance(unpriced, bool) and unpriced > 0


def resume_run(runs_dir: Path, run_id: str, spending_limit_usd=None) -> dict:
    """Give a stopped run a fresh budget of turns and queue it again. Raises JobError if it cannot go on.
    `spending_limit_usd`: a new limit, higher than the old one; the only way past a spending-limit stop."""
    run_dir = Path(runs_dir) / run_id
    with RUN_LOCK:
        run = json.loads((run_dir / "run.json").read_text())
        meta = run["run"]
        status = meta.get("status")
        if status is None:
            raise JobError(409, "This run was saved before run controls existed; it cannot be resumed.")
        if status not in RESUMABLE:
            what = {"done": "This run is finished; start a new run instead.",
                    "queued": "This run is waiting to start.", "running": "This run is still going."}
            raise JobError(409, what.get(status, f"A run that is {status} cannot be resumed."))
        job = json.loads((run_dir / "job.json").read_text())
        choices = meta["choices"]
        limit = choices["spending_limit_usd"]
        if spending_limit_usd is not None:
            if not _finite_limit(spending_limit_usd):
                raise JobError(422, "The new spending limit must be a finite number of dollars, zero or more.")
            if limit is None:
                raise JobError(409, "This run has no spending limit to raise.")
            if spending_limit_usd <= limit:
                raise JobError(409, f"The new spending limit must be higher than the old one ({money(limit)}).")
        if limit_unpriced(limit, job):
            raise JobError(409, "A spending limit cannot be kept on this run: the cost of a call could not be "
                                "counted. Start a new run.")
        if limit is not None and spending_limit_usd is None and (meta["spend_usd"] or 0) >= limit:
            raise JobError(409, "The spending limit stops this run. Resume with a higher spending limit to "
                                "read more.")
        if spending_limit_usd is not None:
            text = f"Spending limit raised by you from {money(limit)} to {money(spending_limit_usd)}."
            meta.setdefault("trail", []).append({"step": "Spending limit raised", "tag": "Person", "who": "person",
                                                 "text": text, "at": now_utc()})
            job.setdefault("limit_changes", []).append({"at": now_utc(), "from": limit, "to": spending_limit_usd})
            choices["spending_limit_usd"] = spending_limit_usd
        meta.update(status="queued", turn_count=0, resumes=meta.get("resumes", 0) + 1,
                    status_reason=f"Resuming with a fresh budget of {meta['turn_limit']} turns.")
        job["log"].append(snapshot(meta, meta["status_reason"]))
        _write(run_dir / "job.json", job)
        _write(run_dir / "run.json", run)
        return run


def run_job(runs_dir: Path, run_id: str, reader, second=None, as_of: date | None = None, wonder=None,
            sam=None, index: sources.Index | None = None) -> None:
    """Carry the run on until it is done or a limit stops it. Never raises: an unexpected error ends it as failed.
    `second`: the second reader (capture/second_reader.py), or None for a run with one reader.
    `index`: the sources index (default: runs/sources_index.json, else the built-in one).
    `sam`: SAM 3.1 (capture/sam.py), which finds the regions of a scanned page.
    `wonder`: the escalation model (capture/wonder.py, Muse Wonder), or None for a run that never escalates.
    `as_of`: must equal the run's persisted as_of (else refused); if the run has none it is persisted. Coverage counts
    dates after the run's reference date (runs.state.run_reference_date) only."""
    job = Job(Path(runs_dir) / run_id, reader, second, as_of, wonder, sam, index)
    try:
        job.drive()
    except Exception as e:  # the type only: nothing that could carry request details
        job.stop("failed", f"The job stopped on an unexpected error ({type(e).__name__}). Resume to try again.")
    finally:
        release_budget(job.dir.name, job._held)
        job.remove_unrecorded_pages()


class Job:
    def __init__(self, run_dir: Path, reader, second=None, as_of: date | None = None, wonder=None, sam=None,
                 index: sources.Index | None = None):
        self.dir, self.reader, self.second, self.as_of, self.wonder = run_dir, reader, second, as_of, wonder
        self.sam = sam
        if second is not None and hasattr(second, "before_fallback"):
            second.before_fallback = self._before_fallback
        self.index = index or sources.load_index()
        self._held: int | None = None             # the budget reservation of the call in flight (count_turn releases it)
        self.current_page: str | None = None      # the page being worked on, so a failed stop is charged to it
        self.run = json.loads((run_dir / "run.json").read_text())
        self.job = json.loads((run_dir / "job.json").read_text())
        self.meta = self.run["run"]
        self.choices = self.meta["choices"]
        if as_of is not None:       # one clock: the run's persisted as_of; a different one is refused, a new one persisted
            given, persisted = as_of.isoformat(), self.meta.get("as_of")
            if persisted and persisted != given:
                raise JobError(409, f"as_of {given} conflicts with this run's as_of {persisted}.")
            self.meta["as_of"] = given
        reserve_row_numbers(self.run, self.job, 0)
        if self.meta["status"] == "done":
            raise JobError(409, "This run is finished; it is never written again.")

    # ------------------------------------------------------------ state

    def save(self, reason: str | None = None, refresh: bool = True) -> None:
        with RUN_LOCK:
            require_unique_target_ids(self.run)
            if refresh:      # a voice call may have raised the persisted spend since this worker last wrote
                self.refresh_spend()      # (count_turn passes False: it refreshed in this lock and has unsaved spend)
            self._merge_voice_from_disk()
            self.job["log"].append(snapshot(self.meta, reason))
            _write(self.dir / "job.json", self.job)
            _write(self.dir / "run.json", self.run)

    def _merge_voice_from_disk(self) -> None:
        """job.json's voice_turns and any log entries this worker did not write (voice calls write them) are kept:
        the worker never writes voice_turns, so the persisted list is the truth; missing log entries go in by time."""
        try:
            disk = json.loads((self.dir / "job.json").read_text())
        except (OSError, ValueError):
            return
        if disk.get("voice_turns"):
            self.job["voice_turns"] = disk["voice_turns"]
        log = self.job["log"]
        missing = [e for e in disk.get("log", []) if e not in log]
        if missing:
            self.job["log"] = sorted(log + missing, key=lambda e: e.get("at") or "")      # stable: ties keep order

    def stop(self, status: str, reason: str) -> None:
        if status == "failed" and self.current_page:
            failures = self.job.setdefault("page_failures", {})
            failures[self.current_page] = failures.get(self.current_page, 0) + 1
            self.job.setdefault("page_failure_reasons", {})[self.current_page] = reason
        self.meta["status"], self.meta["status_reason"] = status, reason
        self.save(reason)

    def remove_unrecorded_pages(self) -> None:
        """A page is captured into the run's folder before it is read; if the run stops first (a limit, a failure), its
        files are in pages/ but run.json names nothing of them. Remove them, so pages/ holds exactly the pages run.json
        names (a resume captures the page again, to the same bytes)."""
        pages = self.dir / "pages"
        if not pages.is_dir():
            return
        named = {Path(p[k]).name for s in self.run["sources"] for p in s["pages"] for k in ("image", "words")}
        for f in pages.iterdir():
            if f.is_file() and f.name not in named:
                f.unlink()

    def skip_page(self, page: dict, reason: str) -> None:
        """Leave the rest of the work on one page undone, say why (run.json skipped_pages, job.json), and go on."""
        sid, ticker = page["source_id"], page["record"]["ticker"]
        skipped = self.job.setdefault("skipped", {})
        if sid in skipped:
            return
        skipped[sid] = {"ticker": ticker, "reason": reason}
        self.meta.setdefault("skipped_pages", []).append({"source_id": sid, "ticker": ticker, "reason": reason})
        self.job["log"].append({"at": now_utc(), "note": f"{ticker} page {sid} skipped: {reason}"})
        self.save()

    def refresh_spend(self) -> None:
        """The persisted run.json spend is the truth (a voice call may have raised it since this worker last wrote)."""
        with RUN_LOCK:
            try:
                disk = json.loads((self.dir / "run.json").read_text())["run"].get("spend_usd")
            except (OSError, ValueError, KeyError):
                return
            if disk is not None:
                self.meta["spend_usd"] = disk

    def _before_fallback(self, silent_reply) -> bool:
        """The second reader's Baseten call got no answer and OpenRouter is next. Count the silent turn now (unpriced
        under a limit), then put the fallback call through the budget gate. False: the fallback is not made."""
        with RUN_LOCK:
            self.refresh_spend()
            self._count_turn(silent_reply)
            release_budget(self.dir.name, self._held)
            self._held = None
            self.save(refresh=False)
            limit = self.choices["spending_limit_usd"]
            if limit is None:
                return True
            if not _finite_limit(limit) or (self.meta["spend_usd"] or 0) >= limit:
                return False
            self._held = reserve_budget(self.dir.name, self.meta["spend_usd"], limit, MODEL_CALL_UNBOUNDED,
                                        alone_may_cross=True)
            return self._held is not None

    def count_turn(self, reply) -> None:
        if (reply.extra or {}).get("already_counted"):      # counted before the fallback gate; nothing more to add
            return
        with RUN_LOCK:       # the spend is added and the reservation released as one step: the gate never sees both
            self.refresh_spend()      # read-modify-write: the call's cost goes onto the persisted spend
            self._count_turn(reply)
            release_budget(self.dir.name, self._held)
            self._held = None
            self.save(refresh=False)

    def _count_turn(self, reply) -> None:
        self.meta["turn_count"] += 1
        self.meta["turns_total"] += 1
        if reply.images is not None:      # a model priced per image (SAM 3.1)
            cost = prices.image_cost_usd(price_name(reply.model), reply.images)
        else:
            cost = prices.cost_usd(price_name(reply.model), reply.prompt_tokens, reply.completion_tokens)
        if cost is None and self.choices["spending_limit_usd"] is not None and not reply.error:
            models = self.job.setdefault("unpriced_models", [])      # the model the REPLY names, not the one asked for
            if reply.model not in models:
                models.append(reply.model)
        if cost is None and (reply.extra or {}).get("route") == "openrouter" and not reply.error:
            # a fallback call with no token counts cannot be held under the cap: no more fallback calls
            self.job["openrouter_unpriced_calls"] = self.job.get("openrouter_unpriced_calls", 0) + 1
        if cost is not None:
            self.meta["spend_usd"] = round((self.meta["spend_usd"] or 0) + cost, prices.ROUND_TO)
            if (reply.extra or {}).get("route") == "openrouter":   # the fallback route has its own $20 cap
                self.job["openrouter_spend_usd"] = round(self.job.get("openrouter_spend_usd", 0.0) + cost,
                                                         prices.ROUND_TO)
        elif self.choices["spending_limit_usd"] is not None and (not reply.error or (
                reply.timed_out and (reply.extra or {}).get("may_have_been_billed", True))):
            # a call that got no answer cost an unknown amount, unless every failure was connect-only (nothing was sent)
            self.job["unpriced_calls"] += 1
        if (reply.extra or {}).get("retried_after_timeout") and not reply.timed_out:
            # answered only on a second try that followed a possibly-billed timeout: the first try's cost is unknown, but
            # it is bounded: the same prompt and the longest answer the call may ask for (max_tokens), or the same image cost
            self.job["retried_after_timeout"] = self.job.get("retried_after_timeout", 0) + 1
            if reply.images is not None:
                bound = prices.image_cost_usd(price_name(reply.model), reply.images)
            else:
                bound = prices.cost_usd(price_name(reply.model), reply.prompt_tokens, MAX_COMPLETION_TOKENS)
            if bound is not None:
                self.meta["spend_usd"] = round((self.meta["spend_usd"] or 0) + bound, prices.ROUND_TO)
                if (reply.extra or {}).get("route") == "openrouter":
                    self.job["openrouter_spend_usd"] = round(self.job.get("openrouter_spend_usd", 0.0) + bound,
                                                             prices.ROUND_TO)
                total = round(self.job.get("retry_worst_case_usd", 0.0) + bound, prices.ROUND_TO)
                self.job["retry_worst_case_usd"] = self.meta["retry_worst_case_usd"] = total
                self.job["retries_charged"] = self.job.get("retries_charged", 0) + 1
            elif self.choices["spending_limit_usd"] is not None:      # no bound can be computed: an unpriced call
                if cost is not None:      # (a reply with no cost was already counted above)
                    self.job["unpriced_calls"] += 1
                for key in ("unpriced_models", "retried_models"):
                    models = self.job.setdefault(key, [])
                    if reply.model not in models:
                        models.append(reply.model)

    def stopped_before_a_call(self, reader=None) -> bool:
        """The checks made before every model call. True (and the run is stopped) when it must not go on. When it
        returns False under a spending limit, the call's worst case is reserved on the run's budget gate (shared with
        the voice route) until count_turn releases it."""
        release_budget(self.dir.name, self._held)        # a call that was never counted holds nothing
        self._held = None
        with RUN_LOCK:
            self.refresh_spend()
            return self._stopped_before_a_call(reader)

    def _stopped_before_a_call(self, reader=None) -> bool:
        if self._checks_before_a_call(reader):
            return True
        limit = self.choices["spending_limit_usd"]
        if limit is not None:
            with RUN_LOCK:
                self._held = reserve_budget(self.dir.name, self.meta["spend_usd"], limit, MODEL_CALL_UNBOUNDED,
                                            alone_may_cross=True)
            if self._held is None:
                self.stop("needs_input", self.limit_reason(limit))
                return True
        return False

    def _checks_before_a_call(self, reader=None) -> bool:
        reader = reader or self.reader
        meta, limit = self.meta, self.choices["spending_limit_usd"]
        if meta["turn_count"] >= meta["turn_limit"]:
            self.stop("paused_turn_limit", f"Stopped at the turn limit ({meta['turn_limit']} turns). "
                                           f"Resume to continue with a fresh budget of turns.")
            return True
        if limit is None:
            return False
        if not _finite_limit(limit):
            self.stop("needs_input", "The spending limit stored for this run is not a finite number of dollars, "
                                     "so it cannot be kept. Nothing more was read.")
            return True
        if self.job["unpriced_calls"] or not prices.has_price(price_name(reader.model)):
            self.stop("needs_input", self.unpriced_reason(limit, reader))
            return True
        if (meta["spend_usd"] or 0) >= limit:
            self.stop("needs_input", self.limit_reason(limit))
            return True
        return False

    def limit_reason(self, limit: float) -> str:
        # G4-R1/GLASS 7: the figures ride the turn/spend line; the sentence is
        # plain words, shown once
        return "Stopped at the spending limit. Raise the limit and press Resume to read more."

    def unpriced_reason(self, limit: float, reader=None) -> str:
        retried = self.job.get("retried_models") or []
        models = [m for m in (self.job.get("unpriced_models") or []) if m not in retried or not prices.has_price(price_name(m))]
        parts = []
        if models or not retried:
            names = ", ".join(models or [getattr(reader, "model", "the model")])
            parts.append(f"The cost of a call could not be counted (runs/prices.py has no price for {names}), so the "
                         f"spending limit of {money(limit)} cannot be kept. Nothing more was read.")
        parts += [f"One call to {m} was answered only on a second try; the first try may have been billed, so its "
                  f"cost is unknown." for m in retried]
        return " ".join(parts)

    def stopped_after_the_last_call(self) -> bool:
        """The checks made when no call is left: a run is done only if every call stayed inside its spending limit. The
        call that crosses the limit stops the run even when it was the last one, and a call whose cost could not be
        counted (the reply named a model with no price) cannot be shown to have stayed inside."""
        limit = self.choices["spending_limit_usd"]
        if limit is None:
            return False
        self.refresh_spend()
        if self.job["unpriced_calls"]:
            self.stop("needs_input", self.unpriced_reason(limit))
            return True
        if (self.meta["spend_usd"] or 0) > limit:
            self.stop("needs_input", self.limit_reason(limit))
            return True
        return False

    # ------------------------------------------------------------ the run

    def drive(self) -> None:
        self.meta["status"], self.meta["status_reason"] = "running", "Reading the captured sources."
        selected_pages = [page for ticker in self.choices["companies"] for page in self.index.pages(ticker)]
        self.meta["pages_total"] = len(selected_pages)
        self.meta["pages_read"] = len(self.run.get("sources", []))
        self.save()
        problem = self.reader.ready()
        if problem:
            return self.stop("needs_input", problem)
        done = {s["id"] for s in self.run["sources"]}
        for ticker in self.choices["companies"]:
            pages = self.index.pages(ticker)
            if not pages:
                return self.stop("needs_input", f"No page is indexed for {ticker}, so nothing was read for it.")
            for page in pages:
                sid = page["source_id"]
                self.current_page = sid
                if sid in self.job.get("skipped", {}):
                    continue
                if self.job.get("page_failures", {}).get(sid, 0) >= MAX_PAGE_FAILURES:
                    self.skip_page(page, f"It stopped the run {MAX_PAGE_FAILURES} times, the last time: "
                                         f"{self.job['page_failure_reasons'].get(sid, 'no reason recorded')}")
                    continue
                if sid not in done and not self.read_page(page) and self.meta["status"] != "running":
                    return
                if not self.date_send_back(page) and self.meta["status"] != "running":
                    return
                if not self.second_pass(page) and self.meta["status"] != "running":
                    return
                if not self.date_send_back(page, "second") and self.meta["status"] != "running":
                    return
                if not self.escalation_pass(page) and self.meta["status"] != "running":
                    return
        self.current_page = None
        if self.stopped_after_the_last_call():
            return
        read = len(self.run["sources"])
        skipped = len(self.job.get("skipped", {}))
        self.meta["finished_at"] = now_utc()
        self.meta["status"] = "done"
        self.meta["status_reason"] = (f"Read {read} page{'s' if read != 1 else ''}: {len(self.run['rows'])} rows, "
                                      f"{sum(len(r['cells']) for t in self.run['tables'] for r in t['rows'])} values."
                                      + (f" {skipped} page{'s' if skipped != 1 else ''} skipped (see skipped_pages)."
                                         if skipped else "")
                                      if read else "Nothing to read: no captured page matches the choices.")
        self.save(self.meta["status_reason"])

    def read_page(self, page: dict) -> bool:
        """Read one captured page and add its source and rows (or tables). False when nothing was added."""
        kind = page["kind"]
        if kind == "scan_events":
            return self.read_scan_page(page)
        entry = page["record"]            # the index's record of the page: ticker, company, filing date, hashes, ...
        if kind == "events":
            types = read_events.EVENT_TYPES
            if not types:
                self.job["log"].append({"at": now_utc(), "note": f"{entry['ticker']} page skipped: no event type "
                                                                 f"was chosen."})
                return False
        try:
            source, words_doc = sources.capture_page(entry, self.dir)      # captured on first use, hashes checked
        except sources.SourcesError as e:
            # a page whose files are missing or no longer match the index will not mend on a retry: skip it, go on
            self.skip_page(page, str(e))
            return False
        picture_state = None
        if kind == "events" and picture.is_picture_page(words_doc):
            rebuilt = self.rebuild_picture(source, words_doc)
            if rebuilt is None:
                return False                      # the run stopped (a limit, a failed call); a resume goes on from here
            words_doc, picture_state = rebuilt
        if kind == "events":
            prompt = read_events.build_prompt(words_doc, entry, types, self.choices["note_for_agent"])
            who, parse = "capture.read_events", read_events.parse_events
        else:
            prompt = read_table.build_prompt(words_doc, self.choices["note_for_agent"])
            who, parse = "capture.read_table", read_table.parse_table
        page_facts = {"words_sha256": entry["words_sha256"], "page_sha256": entry["page_sha256"], "pin": entry["id"]}

        attempts = self.job["attempts"].setdefault(source["id"], [])
        reply, parsed, no_answer = None, None, None
        tool_box = None
        if kind == "events" and self.choices.get("reader_tools"):
            if hasattr(self.reader, "chat"):
                tool_box = reader_tools.ToolBox(words_doc["words"], source.get("published_date"))
            else:
                self.job["log"].append({"at": now_utc(), "note": "reader_tools is on but this reader cannot call tools; "
                                                                 "the page was read without them."})
        while True:
            if tool_box is not None:
                if tool_box.model_calls >= reader_tools.MAX_MODEL_CALLS:
                    break                  # the page's model calls are spent: held, not given a fresh set
                reply, prompt = self.converse(prompt, tool_box)
                if reply is None:
                    return False
                if reply.timed_out:
                    no_answer = reply.error
                    break
            else:
                if self.stopped_before_a_call():
                    return False
                reply = self.reader.call(prompt)
                self.count_turn(reply)
                if reply.timed_out:
                    no_answer = reply.error
                    break
                if reply.error:
                    self.stop("failed", f"{reply.error} Resume to try again.")
                    return False
            parsed, problem = parse(reply.raw)
            if parsed is not None:
                break
            attempts.append({"at": now_utc(), "problem": problem})
            self.save()
            if len(attempts) >= UNREADABLE_LIMIT:
                break

        at = now_utc()
        if parsed is None:
            cap = reader_tools.MAX_MODEL_CALLS if len(attempts) < UNREADABLE_LIMIT else None   # why the loop ended
            row_number = reserve_row_numbers(self.run, self.job, 1)
            self.run["rows"].append(self.refused_row(entry, source, prompt, who, attempts, at, row_number,
                                                     calls_cap=None if no_answer else cap, no_answer=no_answer))
        elif kind == "events":
            extra = {**(reply.extra or {}), **page_facts, "usage": {"prompt_tokens": reply.prompt_tokens,
                                                                    "completion_tokens": reply.completion_tokens}}
            reading = read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=parsed,
                                          problem=None, entry=entry, extra=extra)
            usage = {} if reading["fake"] else reading["usage"]
            n = len(parsed)
            shares = list(zip(prices.split_tokens(usage.get("prompt_tokens"), n),
                              prices.split_tokens(usage.get("completion_tokens"), n), [n] * n))
            first = reserve_row_numbers(self.run, self.job, n)
            new_rows = [build_entry.build_row(first + i, ev, reading, source, words_doc, at, share=shares[i],
                                              tool_steps=(tool_box.steps_for(ev.get("quote"), at, build_entry.MUSE_SPARK)
                                                          + tool_box.unrecorded_steps(at, build_entry.MUSE_SPARK)
                                                          if tool_box is not None else None),
                                              tool_type=(tool_box.type_found(ev.get("quote"))
                                                         if tool_box is not None else None))
                        for i, ev in enumerate(parsed)]
            coverage_rows = coverage.possible_event_rows(words_doc, new_rows, source,
                                                         first_index=self.job["next_row_number"], at=at,
                                                         today=self.coverage_date(source), ticker=entry["ticker"],
                                                         company=entry["company"])
            self.job["next_row_number"] += len(coverage_rows)
            new_rows += coverage_rows
            self.add_picture_trail(new_rows, picture_state, at)
            self.apply_window(new_rows, at)
            self.run["rows"] += new_rows
            self.mark_fake(reading)
        else:
            reading = read_table._reading_record(model=reply.model, prompt=prompt, raw=reply.raw, table=parsed,
                                                 problem=None, extra={**(reply.extra or {}), **page_facts})
            self.run["tables"].append(build_run.build_table(
                reading, words_doc, table_id=f"t{len(self.run['tables']) + 1}", source_id=source["id"],
                page_number=source["pages"][0]["page_number"]))
            self.mark_fake(reading)
        self.run["sources"].append(source)
        self.meta["pages_read"] = len(self.run["sources"])
        self.save()
        return True

    def converse(self, prompt: str, box: "reader_tools.ToolBox"):
        """One read of an events page as a short loop (capture/reader_tools.py): the model may call the checks about its
        own proposals, then hands over the final events. Returns (a Reply for the final answer with the loop's summed
        tokens, the text sent first), or (None, prompt) when the run stopped. Every model call is counted as a turn and
        priced when it is made (count_turn); the Reply returned is only for the rows' trail and is not counted again."""
        native = self.job.get("reader_tools_path") != "json_request"
        sent = prompt + reader_tools.note(native)
        messages = [{"role": "user", "content": sent}]
        while True:
            if self.stopped_before_a_call():
                return None, prompt
            last = box.model_calls >= reader_tools.MAX_MODEL_CALLS - 1        # the last call may not ask for tools
            reply = self.reader.chat(messages, reader_tools.TOOLS if native and not last else None,
                                     json_mode=not native)
            self.count_turn(reply)
            box.model_calls += 1
            if reply.timed_out:
                return reply, prompt      # the service did not answer: the page goes to a person (read_page)
            if reply.error:
                if native and (reply.extra or {}).get("tools_rejected"):
                    # api.meta.ai rejected the tools parameter: the same loop on the JSON tool request, from here on
                    native = False
                    self.job["reader_tools_path"] = "json_request"
                    sent = prompt + reader_tools.note(False)
                    messages = [{"role": "user", "content": sent}]
                    continue
                self.stop("failed", f"{reply.error} Resume to try again.")
                return None, prompt
            if isinstance(reply.prompt_tokens, int) and isinstance(reply.completion_tokens, int):
                box.p_tokens += reply.prompt_tokens
                box.c_tokens += reply.completion_tokens
            else:
                box.unrecorded += 1      # a call with no count is noted on its own step; it does not null the priced ones
            self.job.setdefault("reader_tools_path", "native" if native else "json_request")
            if native:
                calls = reader_tools.native_calls(reply.tool_calls)
                if calls and not last:
                    messages.append({"role": "assistant", "content": reply.raw or "", "tool_calls": reply.tool_calls})
                    box.begin_turn()
                    for c in calls:
                        answer, hint = box.call(c["name"], c["arguments"])
                        messages.append({"role": "tool", "tool_call_id": c["id"],
                                         "content": json.dumps({**answer, **({"note": hint} if hint else {})})})
                    continue
            else:
                request = reader_tools.parse_tool_request(reply.raw)
                if request is not None and not last:
                    answers = []
                    box.begin_turn()
                    for c in request:
                        answer, hint = box.call(c["name"], c["arguments"])
                        answers.append({"name": c["name"], **answer, **({"note": hint} if hint else {})})
                    messages.append({"role": "assistant", "content": reply.raw})
                    messages.append({"role": "user", "content": json.dumps({"tool_answers": answers})})
                    continue
            break
        box.note_final(self.final_quotes(reply.raw))
        summary = {**box.summary(), "model_calls": box.model_calls}
        pages = self.job.setdefault("reader_tools_pages", {})
        pages[self.current_page or ""] = summary
        total = {k: sum(p[k] for p in pages.values()) for k in summary}
        self.meta["reader_tools"] = {"path": self.job["reader_tools_path"], **total}      # what the run's tool use came to
        priced = box.model_calls > box.unrecorded
        final = Reply(reply.raw, reply.model, prompt_tokens=box.p_tokens if priced else None,
                      completion_tokens=box.c_tokens if priced else None,
                      extra={**(reply.extra or {}), "reader_tools": {"path": self.job["reader_tools_path"], **summary}})
        return final, sent

    @staticmethod
    def final_quotes(raw) -> list:
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError:
            return []
        evs = data.get("events") if isinstance(data, dict) else None
        return [e.get("quote") for e in evs if isinstance(e, dict)] if isinstance(evs, list) else []

    def apply_window(self, rows: list[dict], at: str) -> None:
        """Mark only events whose full period ended before this run as code exclusions.

        Future proposals remain available; the calendar presents those beyond its six-month window in Later.
        """
        run_date = run_reference_date({"run": self.meta})
        exclusions = self.run.setdefault("exclusions", [])
        for row in rows:
            if row.get("group") == "removed" or not row.get("date"):
                continue
            reason = past_reason(row, run_date)
            if reason is None:
                candidate = past_candidate_reason(row, run_date)
                date_check = next((c for c in row.get("checks", []) if c.get("name") == "date_match"), None)
                if candidate and (not date_check or date_check.get("result") != "pass"):
                    proposal = (f"{candidate} Proposed exclusion not applied because Date read as written is "
                                f"{(date_check or {}).get('result') or 'unavailable'}.")
                    row["past_exclusion_proposal"] = proposal
                    row.setdefault("trail", []).append({"step": "Past exclusion not applied", "tag": "Code",
                        "who": "server.jobs", "at": at, "text": proposal})
                continue
            prior = row.get("reason")
            if prior:
                reason += " " + prior
            row["group"] = "removed"
            row["reason"] = reason
            row.setdefault("trail", []).append({"step": "Left out as already past", "tag": "Code",
                "who": "server.jobs", "at": at, "text": reason})
            exclusions.append({"id": f"x{len(exclusions) + 1}", "target": row["id"],
                               "rule": "event_in_past", "reason": reason, "by": "code"})

    def coverage_date(self, source: dict) -> date:
        """A dated sentence counts when its date is after the run's reference date: the persisted
        runs.state.run_reference_date (as_of, else the start day). One clock; the source is not consulted."""
        ref = run_reference_date(self.run)
        if ref is None:
            raise JobError(409, "This run has no usable reference date (as_of or started_at).")
        return ref

    # ------------------------------------------------------------ the second reader

    def note_second(self, sid: str, status: str, reason: str | None = None, model: str | None = None,
                    got: str | None = None) -> None:
        """Record what happened to the second read of one page, and summarise it for the run."""
        self.job["second"][sid] = {"status": status, "reason": reason, "model": model, "input": got}
        seen = list(self.job["second"].values())
        pick = next((v for v in seen if v["status"] == "read"), seen[-1])
        skipped = [v["reason"] for v in seen if v["status"] != "read" and v["reason"]]
        reason = pick["reason"] if pick["status"] != "read" else (
            "Not every page was read by the second reader: " + " ".join(dict.fromkeys(skipped)) if skipped else None)
        self.meta["second_reader"] = {"model": pick["model"], "status": pick["status"], "reason": reason,
                                      **({"input": pick["input"]} if pick.get("input") else {})}

    def second_problem(self) -> str | None:
        """Why the second reader cannot be used now (None: it can). The $20 fallback cap is applied first."""
        if self.second is None:
            return "No second reader is available."
        if hasattr(self.second, "openrouter_allowed"):      # the cap, before readiness is asked
            unpriced = self.job.get("openrouter_unpriced_calls", 0)
            self.second.openrouter_allowed = (self.job.get("openrouter_spend_usd", 0.0) < OPENROUTER_CAP_USD
                                              and not unpriced)
            self.second.openrouter_blocked = None if self.second.openrouter_allowed else (
                UNPRICED if unpriced else CAP_REACHED)
        return self.second.ready()

    # ------------------------------------------------------------ a picture page: SAM 3.1 regions + the image reader

    def rebuild_picture(self, source: dict, words_doc: dict):
        """A page whose text layer is hidden (runs/picture.py): SAM 3.1 finds regions (one counted, priced turn per
        concept), the image reader transcribes each region's crop (one turn each), and the words file in the run's folder
        is rebuilt from the transcriptions. Returns (new words_doc, state), or (words_doc, None) when it cannot be done
        (no SAM 3.1 or no image reader, no region, nothing transcribed: the page is read from its own words, as before,
        and the log says so), or None when the run stopped. What each call returned is kept in job.json."""
        sid = source["id"]
        page0 = source["pages"][0]
        why = None
        if self.sam is None or self.sam.ready():
            why = self.sam.ready() if self.sam is not None else "No SAM 3.1 reader is available."
        elif self.second_problem():
            why = self.second_problem()
        if why:
            self.job["log"].append({"at": now_utc(), "note": f"{sid} is a picture page; its words were not rebuilt: {why}"})
            return words_doc, None
        w, h = float(page0["width_pt"]), float(page0["height_pt"])
        png = (self.dir / page0["image"]).read_bytes()
        st = self.job.setdefault("picture", {}).setdefault(sid, {"by_concept": {}, "calls": [], "reads": {}})
        if "words_doc" not in st:
            for concept in picture.CONCEPTS + (sam_module.FALLBACK,):
                if concept in st["by_concept"]:
                    continue
                if concept == sam_module.FALLBACK and not picture.needs_fallback(st["by_concept"]):
                    break
                if self.stopped_before_a_call(self.sam):
                    return None
                reply = self.sam.segment(png, concept)
                self.count_turn(reply)
                if reply.error:
                    self.stop("failed", f"SAM 3.1: {reply.error} Resume to try again.")
                    return None
                st["by_concept"][concept] = [sam_module.to_points(b, w, h) for b in sam_module.parse_boxes(reply.raw)]
                st["calls"].append({"concept": concept, "images": reply.images,
                                    "cost_usd": prices.image_cost_usd(sam_module.NAME, reply.images)})
                self.save()
            regions = picture.merge_regions(st["by_concept"], (w, h))
            for region in regions:
                if region["id"] in st["reads"]:
                    continue
                if self.stopped_before_a_call(self.second):
                    return None
                crop = scan.crop_box(region, w, h)
                prompt = picture.transcribe_prompt(region)
                reply = self.second.call(prompt, image=scan.crop_png(png, crop, w))
                self.count_turn(reply)
                if reply.error:
                    self.stop("failed", f"The image reader: {reply.error} Resume to try again.")
                    return None
                st["reads"][region["id"]] = {"region_id": region["id"], "crop": crop, "model": reply.model,
                                             "text": picture.parse_transcription(reply.raw),
                                             "usage": {"prompt_tokens": reply.prompt_tokens,
                                                       "completion_tokens": reply.completion_tokens}}
                self.save()
            st["regions"] = regions
            made = [{"region": r, "text": st["reads"][r["id"]]["text"]} for r in regions]
            built = picture.build_words(w, h, page0["page_number"], [t for t in made if t["text"]])
            st["words_doc"] = built if built["words"] else None
            st["kept_regions"] = [r["region"] for r in made if r["text"]]
            self.save()
        if not st["words_doc"]:
            page0["note"] = picture.ALL_EMPTY_NOTE
            self.job["log"].append({"at": now_utc(), "note": f"{sid} is a picture page; SAM 3.1 and the image reader "
                                                              f"gave no words, so it is read from its own words."})
            return words_doc, st            # the state goes on: every attempted call still gets its trail step
        rebuilt = st["words_doc"]
        words_path = self.dir / page0["words"]
        words_path.write_text(json.dumps(rebuilt, indent=1, ensure_ascii=False) + "\n")
        page0.update({"words_sha256": build_run.file_sha256(words_path), "kind": picture.KIND,
                      "words_from": picture.WORDS_FROM, "note": picture.source_note(st["kept_regions"]),
                      "regions": st["kept_regions"]})
        return rebuilt, st

    def add_picture_trail(self, rows: list[dict], st: dict | None, at: str, costs: bool = True) -> None:
        """Put the picture-page steps first in every row's trail. `costs` False: the page's calls were already counted on
        the rows read first, so these steps carry no cost."""
        if not st or "regions" not in st or not rows:
            return
        regions = st["regions"]
        reads = [st["reads"][r["id"]] for r in regions]          # every attempted call, empty ones too
        made = [{"region": r, "text": st["reads"][r["id"]]["text"]} for r in regions]
        n_words = len((st.get("words_doc") or {}).get("words", []))
        note = picture.source_note([r for r, t in zip(regions, made) if t["text"]]) if n_words else picture.ALL_EMPTY_NOTE
        for i, row in enumerate(rows):
            steps = picture.trail_steps(regions, made, st["calls"], reads, n_words, at, len(rows), i)
            if not costs:
                steps = [{**s_, "tokens": None, "cost_usd": None} if s_["tag"] == "Model" else s_ for s_ in steps]
            row["trail"][1:1] = steps          # the schema keeps "Instructions sent" first
            if row.get("reason"):
                row["reason"] = f"{row['reason']} Source note: {note}."

    def second_pass(self, page: dict) -> bool:
        """The second reader's independent read of a page the first reader has read, compared by code. True when
        the page is finished with (read, skipped or unreadable); False when the run stopped."""
        if page["kind"] == "scan_events":
            return True        # a scanned page is read region by region (read_scan_page); no second read of it yet
        sid = page["source_id"]
        source = next((s for s in self.run["sources"] if s["id"] == sid), None)
        if self.second is None or source is None or sid in self.job["second"]:
            return True
        problem = self.second_problem()
        if problem:
            self.note_second(sid, "not_run", problem)
            self.save(problem)
            return True
        page0 = source["pages"][0]
        words_doc = json.loads((self.dir / page0["words"]).read_text())
        image = (self.dir / page0["image"]).read_bytes()      # the page as the second reader sees it
        note = self.choices["note_for_agent"]
        entry = page["record"]
        if page["kind"] == "events":
            types = read_events.EVENT_TYPES
            attempts = [("image", read_events.build_image_prompt(entry, types, note), image),
                        ("image_and_words", read_events.build_prompt(words_doc, entry, types, note), image)]
            parse = read_events.parse_events
        else:
            attempts = [("image", read_table.build_image_prompt(note), image),
                        ("image_and_words", read_table.build_prompt(words_doc, note), image)]
            parse = read_table.parse_table
        # The second reader reads the page IMAGE, with no word list: its read does not depend on the text layer. If
        # that answer cannot be read, the words are sent too, once, as a fallback; the run records which it got.
        parsed, why, reply, prompt, got = None, None, None, None, None
        tried = self.job.setdefault("second_attempts", {}).setdefault(sid, [])     # paid for, unreadable: a resume skips them
        for got, prompt, png in attempts:
            if got in tried:
                continue
            if self.stopped_before_a_call(self.second):
                return False
            reply = self.second.call(prompt, image=png)
            self.count_turn(reply)
            if reply.timed_out:      # no answer even after the one retry: the first reader's rows stand, nothing is compared
                parsed, why = None, reply.error
                break
            if reply.error:
                self.stop("failed", f"The second reader: {reply.error} Resume to try again.")
                return False
            parsed, why = parse(reply.raw)
            if parsed is not None:
                break
            tried.append(got)
            self.save()
        if reply is None:       # every attempt was made before a resume and none could be read
            self.note_second(sid, "unreadable", "The answers could not be read before the run stopped.", None, got)
            self.save("The second reader's answers could not be read; nothing was compared.")
            return True
        name = price_name(reply.model)
        if parsed is None:
            self.note_second(sid, "unreadable", why, name, got)
            self.save(why + " Nothing was compared." if reply.timed_out
                      else f"The second reader's answer could not be read ({why}); nothing was compared.")
            return True
        at = now_utc()
        extra = {**(reply.extra or {}), "input": got, "words_sha256": entry["words_sha256"],
                 "page_sha256": entry["page_sha256"], "pin": entry["id"],
                 "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
        if page["kind"] == "events" and got == "image":     # boxes came back on the 0-1000 grid over the image
            for ev in parsed:
                ev["box"] = read_events.grid_to_points(ev.get("box"), page0["width_pt"], page0["height_pt"])
        if page["kind"] == "events":
            reading = read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=parsed,
                                          problem=None, entry=entry, extra=extra)
            rows = [r for r in self.run["rows"] if r["source_id"] == sid]
            next_row = reserve_row_numbers(self.run, self.job, 0)
            _, new_rows = compare.apply_events(rows, parsed, reading, source, words_doc, at,
                                               next_index=next_row)
            self.job["next_row_number"] += len(new_rows)
            self.add_picture_trail(new_rows, self.job.get("picture", {}).get(sid), at, costs=False)
            self.apply_window(new_rows, at)
            # a dated sentence the second reader turned into a row no longer needs its "not read" row
            quotes = [r["quote"] for r in new_rows]
            self.run["rows"] = [r for r in self.run["rows"]
                                if not (r.get("found_by") == "coverage" and r["source_id"] == sid
                                        and sentence_covered(r["quote"], quotes))]
            self.run["rows"] += new_rows
        else:
            table = next((t for t in self.run["tables"] if t["source_id"] == sid), None)
            if table is not None:
                compare.apply_table(table, parsed)
        self.note_second(sid, "read", None, name, got)
        self.save()
        return True

    # ------------------------------------------------------------ a scanned page: SAM 3.1 regions

    def read_scan_page(self, page: dict) -> bool:
        """A page with no text layer. SAM 3.1 is asked for each concept ("table", "block of text"), one counted, priced turn
        each (per image); every box it returns becomes a region; each region's crop is read by the first reader, one
        turn each; the rows are built with the region check in place of the text-layer box check (runs/scan.py).
        What each call returned is kept in job.json, so a resume never pays for a call twice."""
        entry = load_entry(read_events.PIN)
        types = read_events.EVENT_TYPES
        if not types:
            self.job["log"].append({"at": now_utc(), "note": f"{entry['ticker']} scanned page skipped: no event type "
                                                              f"was chosen."})
            return False
        if self.sam is None or self.sam.ready():
            reason = ("No SAM 3.1 reader is available, so a scanned page cannot be read." if self.sam is None
                      else self.sam.ready())
            self.stop("needs_input", reason)
            return False
        png = scan.scanned_png(entry)
        words = json.loads((build_run.FIXTURES / entry["words_file"]).read_text())
        w, h = float(words["page_width"]), float(words["page_height"])
        st = self.job.setdefault("scan", {}).setdefault(scan.SOURCE_ID, {"by_concept": {}, "calls": [], "reads": {}})
        for concept in sam_module.CONCEPTS + (sam_module.FALLBACK,):
            if concept in st["by_concept"]:
                continue
            if concept == sam_module.FALLBACK and not scan.needs_fallback(st["by_concept"]):
                break
            if self.stopped_before_a_call(self.sam):
                return False
            reply = self.sam.segment(png, concept)
            self.count_turn(reply)
            if reply.error:
                self.stop("failed", f"SAM 3.1: {reply.error} Resume to try again.")
                return False
            st["by_concept"][concept] = [sam_module.to_points(b, w, h) for b in sam_module.parse_boxes(reply.raw)]
            st["calls"].append({"concept": concept, "images": reply.images,
                                "cost_usd": prices.image_cost_usd(sam_module.NAME, reply.images)})
            self.save()
        regions = scan.merge_regions(st["by_concept"])
        note = self.choices["note_for_agent"]
        for region in regions:
            if region["id"] in st["reads"]:
                continue
            crop = scan.crop_box(region, w, h)
            prompt = scan.region_prompt(entry, region, types, note)
            if self.stopped_before_a_call(self.reader):
                return False
            reply = self.reader.call(prompt, image=scan.crop_png(png, crop, w))
            self.count_turn(reply)
            if reply.error:
                self.stop("failed", f"{reply.error} Resume to try again.")
                return False
            events, _ = read_events.parse_events(reply.raw)
            st["reads"][region["id"]] = {"region_id": region["id"], "crop": crop, "prompt_text": prompt,
                                         "model": reply.model, "events": events,
                                         "usage": {"prompt_tokens": reply.prompt_tokens,
                                                   "completion_tokens": reply.completion_tokens}}
            self.save()
        source = scan.scanned_source(entry, self.dir, png, w, h, regions, stem=page["record"]["file_stem"])
        by_id = {r["id"]: r for r in regions}
        reads = [{**r, "region": by_id[r["region_id"]]} for r in st["reads"].values() if r["region_id"] in by_id]
        first = reserve_row_numbers(self.run, self.job, 0)
        rows = scan.assemble_rows(reads, source, st["calls"], now_utc(), first_index=first)
        self.job["next_row_number"] += len(rows)
        self.apply_window(rows, now_utc())
        self.run["rows"] += rows
        self.run["sources"].append(source)
        self.meta["pages_read"] = len(self.run["sources"])
        self.save()
        return True

    # ------------------------------------------------------------ escalation to Muse Wonder

    def note_escalation(self, status: str, reason: str | None = None) -> None:
        sent = sum(1 for v in self.job.get("escalated_rows", {}).values())
        replaced = sum(1 for v in self.job.get("escalated_rows", {}).values() if v == "replaced")
        self.meta["escalation"] = {"model": "Muse Wonder", "status": status, "rows_sent": sent,
                                   "rows_replaced": replaced, **({"reason": reason} if reason else {})}

    def escalation_pass(self, page: dict) -> bool:
        """Send each row of a page that failed a check, or that the two readers disagree on, to Muse Wonder (at most
        MAX_PER_PAGE rows), one counted and priced turn each. True when the page is finished with; False when the run
        stopped. Wonder's answer is a new proposal that goes through the same checks (runs/escalate.py)."""
        sid = page["source_id"]
        source = next((s for s in self.run["sources"] if s["id"] == sid), None)
        done = self.job.setdefault("escalated", {})
        if page["kind"] == "scan_events":
            return True
        if self.wonder is None or source is None or page["kind"] != "events" or sid in done:
            return True
        sent = self.job.setdefault("escalated_rows", {})
        page_rows = [r for r in self.run["rows"] if r["source_id"] == sid]
        left = escalate.MAX_PER_PAGE - sum(1 for r in page_rows if r["id"] in sent)    # the cap holds across resumes
        rows = [r for r in escalate.budgeted(page_rows) if r["id"] not in sent][: max(left, 0)]
        if not rows:
            done[sid] = "nothing to send"
            return True
        problem = self.wonder.ready()
        if problem:
            done[sid] = "not_run"
            self.note_escalation("not_run", problem)
            self.save(problem)
            return True
        page0 = source["pages"][0]
        words_doc = json.loads((self.dir / page0["words"]).read_text())
        image = (self.dir / page0["image"]).read_bytes()
        entry = page["record"]
        types = read_events.EVENT_TYPES
        for row in rows:
            if self.wonder_budget_spent():
                done[sid] = "budget"
                self.note_escalation("read", f"Muse Wonder's part of the spending limit "
                                             f"({int(escalate.WONDER_SHARE * 100)}%) is used; no more rows are sent.")
                self.save()
                return True
            trig = escalate.trigger(row, escalate.SENT_FOR)
            y_range = escalate.band(row, words_doc)
            text = escalate.prompt(entry, trig, y_range, words_doc, types, self.choices["note_for_agent"])
            region = escalate.region_png(image, words_doc, y_range)
            if self.wonder_would_cross(escalate.estimate_usd(text, region)):
                done[sid] = "budget"
                self.note_escalation("read", f"Muse Wonder's part of the spending limit "
                                             f"({int(escalate.WONDER_SHARE * 100)}%) would be passed by the next call; "
                                             f"it is not sent.")
                self.save()
                return True
            if self.stopped_before_a_call(self.wonder):
                return False
            reply = self.wonder.call(text, image=region)
            retry_before = self.job.get("retry_worst_case_usd", 0.0)
            self.count_turn(reply)
            call_cost = prices.cost_usd(price_name(reply.model), reply.prompt_tokens, reply.completion_tokens)
            lost = self.job.get("retry_worst_case_usd", 0.0) - retry_before      # a lost try counts in Wonder's share too
            self.job["wonder_spend_usd"] = round(self.job.get("wonder_spend_usd", 0.0) + (call_cost or 0.0) + lost,
                                                 prices.ROUND_TO)
            if reply.timed_out:      # no answer: the row stays as it was and the next row goes on
                row["trail"].append({"step": "Escalation result", "tag": "Code", "who": "capture.http",
                                     "text": f"{reply.error} The row stays as it was.", "at": now_utc()})
                self.job["escalated_rows"][row["id"]] = "kept"
                self.save(reply.error)
                continue
            if reply.error:
                self.stop("failed", f"Muse Wonder: {reply.error} Resume to try again.")
                return False
            if call_cost is None and self.choices["spending_limit_usd"] is not None:
                # the cost of this reply cannot be counted, so it cannot be shown to sit inside Wonder's share:
                # the reading is not applied and the row stays as it was
                escalate.record_sent(row, reply.prompt_tokens, now_utc(), trig, text)
                escalate.record_unused_call(row, reply, now_utc(), f"The cost of {escalate.WONDER}'s call could not "
                                            f"be counted, so its reading is not taken.")
                self.job["escalated_rows"][row["id"]] = "kept"
                done[sid] = "budget"
                self.save()
                self.stop("needs_input", self.unpriced_reason(self.choices["spending_limit_usd"], self.wonder))
                return False
            events, _ = read_events.parse_events(reply.raw)
            events = events or []
            extra = {**(reply.extra or {}), "input": "image",
                     "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
            reading = read_events._record(model=reply.model, prompt=text, raw=reply.raw, events=events, problem=None,
                                          entry=entry, extra=extra)
            if self.wonder_budget_passed():     # the call cost more than the share allows: its reading is not taken
                escalate.record_sent(row, reply.prompt_tokens, now_utc(), trig, text)
                escalate.record_unused_call(row, reply, now_utc(), f"{escalate.WONDER}'s call took the run past "
                                            f"{int(escalate.WONDER_SHARE * 100)}% of its spending limit, so its "
                                            f"reading is not taken.")
                self.job["escalated_rows"][row["id"]] = "kept"
                done[sid] = "budget"
                self.note_escalation("read", "Muse Wonder's part of the spending limit is used; no more rows are sent.")
                self.save()
                return True
            chosen = escalate.pick(row, events)       # the band may hold other events: take this row's
            event = escalate.to_page_points(chosen, y_range, words_doc) if chosen else None
            share = (reply.prompt_tokens, reply.completion_tokens, 1)
            outcome = escalate.apply(row, event, reading, source, words_doc, now_utc(), trig, self.run, share, text)
            self.job["escalated_rows"][row["id"]] = outcome
            self.note_escalation("read")
            self.save()
        done[sid] = "done"
        self.note_escalation("read")
        self.save()
        return True

    def wonder_budget_spent(self) -> bool:
        """True when Muse Wonder has used its share of a limited run's spending limit (escalate.WONDER_SHARE)."""
        limit = self.choices["spending_limit_usd"]
        return limit is not None and self.job.get("wonder_spend_usd", 0.0) >= escalate.WONDER_SHARE * limit

    def wonder_budget_passed(self) -> bool:
        """True when Muse Wonder's spend is past its share of the limit (at the share exactly is not past it)."""
        limit = self.choices["spending_limit_usd"]
        return limit is not None and self.job.get("wonder_spend_usd", 0.0) > escalate.WONDER_SHARE * limit

    def wonder_would_cross(self, estimate_usd: float) -> bool:
        """True when a Wonder call of about `estimate_usd` would carry its spend past its share of the limit."""
        limit = self.choices["spending_limit_usd"]
        return limit is not None and self.job.get("wonder_spend_usd", 0.0) + estimate_usd > escalate.WONDER_SHARE * limit

    def date_send_back(self, page: dict, phase: str = "first") -> bool:
        """One send-back per row whose date text is not in its quote: the reader that wrote the row is asked once (one
        counted, priced turn per page and reader, for all its rows) for the sentence that states the date. `phase`
        "first": the first reader's rows, right after it read the page; "second": the second reader's rows, asked of the
        second reader once it has read. The new quote is taken only if it is printed on the page; the checks then decide
        the row (runs/build_entry.build_row). True when the page is finished with; False when the run stopped."""
        sid = page["source_id"]
        key = sid if phase == "first" else f"{sid}#second"
        done = self.job.setdefault("sent_back", {})
        source = next((s for s in self.run["sources"] if s["id"] == sid), None)
        reader = self.reader if phase == "first" else self.second
        if key in done or source is None or page["kind"] != "events" or reader is None:
            return True
        who_wrote = (lambda r: r.get("found_by") is None) if phase == "first" else (
            lambda r: r.get("found_by") == "second_reader")
        rows = [r for r in self.run["rows"] if r["source_id"] == sid and who_wrote(r) and r.get("group") != "removed"
                and r.get("quote") and any(
                    c["name"] == "date_match" and c["result"] == "fail" and c["reason"].startswith("The date text is not")
                    for c in r.get("checks", []))]
        if not rows:
            done[key] = "nothing to send"
            return True
        if phase == "second" and reader.ready():
            done[key] = "not_run"
            return True
        page0 = source["pages"][0]
        words_doc = json.loads((self.dir / page0["words"]).read_text())
        entry = page["record"]
        prompt = read_events.build_send_back_prompt(words_doc, entry, rows)
        if self.stopped_before_a_call(reader):
            return False
        reply = reader.call(prompt)
        self.count_turn(reply)
        if reply.timed_out:      # no answer: the rows stay as they were, for the checks and a person
            done[key] = "no answer"
            self.save(reply.error)
            return True
        if reply.error:
            self.stop("failed", f"{reply.error} Resume to try again.")
            return False
        answers = {a["id"]: a for a in (read_events.parse_send_back(reply.raw) or [])}
        usage = {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}
        reading = read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=[], problem=None,
                                      entry=entry, extra={**(reply.extra or {}), "usage": usage})
        share = (reply.prompt_tokens, reply.completion_tokens, len(rows))
        at = now_utc()
        for row in rows:
            self.apply_send_back(row, answers.get(row["id"]), reading, source, words_doc, share, prompt, at)
        done[key] = "done"
        self.save()
        return True

    @staticmethod
    def is_printed_sentence(quote: str, words_doc: dict) -> bool:
        """True when `quote` equals a sentence of the page after collapsing runs of whitespace, and nothing else:
        no other character may differ."""
        squash = lambda t: " ".join(str(t).split())
        want = squash(quote)
        return any(squash(s["text"]) == want for s in coverage_sentences(words_doc["words"]))

    def apply_send_back(self, row: dict, answer: dict | None, reading: dict, source: dict, words_doc: dict,
                        share: tuple, prompt: str, at: str) -> None:
        who = "server.jobs"
        pt = reading["usage"]["prompt_tokens"]
        row["trail"].append({"step": "Sent back for the date", "tag": "Code", "who": who, "at": at,
                             "text": "The date text is not part of the quote. The same reader was asked once for the "
                                     "sentence printed on the page that states the date."})
        row["trail"].append({"step": "Instructions sent", "tag": "Code", "who": "capture.read_events", "at": at,
                             "text": prompt, "tokens": {"prompt": pt, "completion": None} if isinstance(pt, int) else None})
        quote = (answer or {}).get("quote")
        date_text = (answer or {}).get("date_text")
        if not (isinstance(quote, str) and quote.strip() and isinstance(date_text, str) and date_text.strip()):
            row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                                 "text": "The reader gave no sentence for the date. The row stays as it was."})
            return
        if not self.is_printed_sentence(quote, words_doc):
            row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                                 "text": "The sentence the reader gave is not printed on the page as a sentence, word "
                                         "for word. The row stays as it was."})
            return
        event = {k: row.get(k) for k in ("ticker", "company", "event_type", "title", "date", "date_end", "program",
                                         "indication")}
        event.update(quote=quote, date_text=date_text, box=None)
        proposal = build_entry.build_row(0, event, reading, source, words_doc, at, share=share,
                                         model_name=price_name(reading["model"]))
        model_step = next((t for t in proposal["trail"] if t["tag"] == "Model"), None)
        if model_step:
            row["trail"].append({**model_step, "step": "Sentence for the date"})
        row["trail"] += [t for t in proposal["trail"] if t["step"] in ("Checked", "Box located by code",
                                                                       "Year inferred from the filing date")]
        printed = next(c for c in proposal["checks"] if c["name"] == "quote_match")["result"] == "pass"
        if not printed:
            row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                                 "text": "The sentence the reader gave is not printed on the page. The row stays as "
                                         "it was."})
            return
        old = next(c for c in row["checks"] if c["name"] == "date_match")
        for f in ("quote", "date_text", "date", "date_end", "box", "checks", "status", "group", "reason"):
            row[f] = proposal[f]
        if row.get("found_by") == "second_reader":      # nobody else read it: it stays an open row of the second reader
            failed = any(c["result"] == "fail" for c in row["checks"])
            row["status"] = event_checks.row_status(row["checks"], second_reader_agrees=False)
            row["group"] = "open"
            row["reason"] = (f"{proposal['reason']} " if failed else "") + compare.ONLY_REASON
        row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                             "text": f"The sentence is printed on the page. The row takes it; the checks decide: "
                                     f"{proposal['status']}."})
        if proposal["status"] == "matched":
            ids = [int(m["id"][1:]) for m in self.run["mistakes"] if m["id"][1:].isdigit()]
            self.run["mistakes"].append({
                "id": f"m{max(ids, default=0) + 1}", "target": row["id"], "caught_by": "code",
                "what_changed": f"The date text was not in the quote; the reader, sent back once, gave the sentence "
                                f"that states “{date_text}”.",
                "type": T.mistake_type({**old, "result": "fail"}, T.DATE_NOT_IN_QUOTE)})

    def mark_fake(self, reading: dict) -> None:
        if reading.get("fake"):
            self.meta["label"] = (f"Practice run ({', '.join(self.choices['companies'])}): "
                                  f"fake reading, no model called")

    def refused_row(self, entry: dict, source: dict, prompt: str, who: str, attempts: list, at: str, row_number: int,
                    calls_cap: int | None = None, no_answer: str | None = None) -> dict:
        """An unreadable page: one row, group open, for a person to look at. `attempts`: the answers that were not
        understood. `calls_cap`: set when the page's model calls ran out before three answers were refused (the
        reader_tools cap); the reason and a trail step then say so, and THREE_REFUSALS is kept for three parse failures."""
        page_number = source["pages"][0]["page_number"]
        ticker = entry["ticker"]
        trail = [{"step": "Instructions sent", "tag": "Code", "who": who, "text": prompt, "at": at}]
        trail += [{"step": f"Answer {i} not understood", "tag": "Code", "who": who,
                   "text": a["problem"], "at": a["at"]} for i, a in enumerate(attempts, start=1)]
        reason = THREE_REFUSALS
        if calls_cap is not None:
            reason = CALLS_EXHAUSTED.format(cap=calls_cap)
            trail.append({"step": f"Model-call cap reached ({calls_cap} calls)", "tag": "Code", "who": "server.jobs",
                          "text": f"The page's {calls_cap} model calls were used; {len(attempts)} answer"
                                  f"{'s were' if len(attempts) != 1 else ' was'} not understood.", "at": at})
        title = f"{ticker} page {page_number}: the reader's answer could not be read"
        if no_answer:       # the service did not answer, even after the one retry (capture/http.py)
            reason = no_answer
            title = f"{ticker} page {page_number}: the reader did not answer in time"
            trail.append({"step": "No answer from the model", "tag": "Code", "who": "capture.http", "text": no_answer,
                          "at": at})
        trail.append({"step": "Sent to a person", "tag": "Code", "who": "server.jobs", "text": reason, "at": at})
        return {
            "id": f"e{row_number}", "ticker": ticker, "company": entry["company"],
            "event_type": "unknown", "title": title,
            "date_text": None, "date": None, "quote": None, "source_id": source["id"], "page_number": page_number,
            "box": None, "proposed_by": build_run.MUSE_SPARK, "status": "unreadable", "group": "open",
            "reason": reason, "sample": False, "checks": [], "trail": trail,
        }


def record_voice_unpriced(runs_dir: Path, run_id: str, billed_attempts: int, model: str = "Muse Voice Transcribe") -> None:
    """A voice call got no answer but may have been billed (billed_attempts > 0): its cost is unknown, so the run's
    budget state gets one unpriced call, the same marker an unpriced reader call leaves. Call it only under a limit."""
    if not billed_attempts:
        return
    run_dir = Path(runs_dir) / run_id
    with RUN_LOCK:
        job_file = run_dir / "job.json"
        job = json.loads(job_file.read_text()) if job_file.is_file() else {"log": []}
        job["unpriced_calls"] = job.get("unpriced_calls", 0) + 1
        models = job.setdefault("unpriced_models", [])
        if model not in models:
            models.append(model)
        job.setdefault("log", []).append({"at": now_utc(), "note": "A spoken decision got no answer; it may have been "
                                                                   "billed, so the spend is not known."})
        _write(job_file, job)


def fail_voice(runs_dir: Path, run_id: str, token: int | None, billed_attempts: int, limited: bool) -> None:
    """A voice call failed: release its reservation and, if it may have been billed under a limit, persist the
    unknown-spend marker, in ONE RUN_LOCK acquisition, so the run worker's gate never sees neither."""
    with RUN_LOCK:
        if limited and billed_attempts:
            record_voice_unpriced(runs_dir, run_id, billed_attempts)
        release_budget(run_id, token)


def count_voice_turn(runs_dir: Path, run_id: str, heard: dict, token: int | None = None) -> None:
    """Price one spoken-decision transcription into the run's spend as a turn ("Muse Voice Transcribe", audio seconds).
    The turn counters are not touched (they are the reading budget); the spend and a trail line in job.json are."""
    run_dir = Path(runs_dir) / run_id
    with RUN_LOCK:
        run = json.loads((run_dir / "run.json").read_text())
        # Rule: the route refuses a turn once spend >= limit (409), so a turn that gets here is always priced in full.
        job_file = run_dir / "job.json"
        job = json.loads(job_file.read_text()) if job_file.is_file() else {"log": []}
        meta, cost = run["run"], heard.get("cost_usd")
        release_budget(run_id, token)       # counted now: the reservation gives way to the spend, in one lock
        if cost is not None:
            meta["spend_usd"] = round((meta.get("spend_usd") or 0) + cost, prices.ROUND_TO)
        job.setdefault("voice_turns", []).append({"at": now_utc(), "model": "Muse Voice Transcribe",
                                                  "audio_seconds": round(heard["audio_ms"] / 1000, 3), "cost_usd": cost})
        job.setdefault("log", []).append({"at": now_utc(), "note": "Spoken decision transcribed.",
                                          "spend_usd": meta.get("spend_usd")})
        _write(run_dir / "job.json", job)
        _write(run_dir / "run.json", run)
