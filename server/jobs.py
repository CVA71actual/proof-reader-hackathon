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

The source set is frozen when a run starts (Job 51f). The first time the job drives a run it records in job.json
`source_freeze`: the SHA-256 of the sources index file it reads (for the built-in index, of its records as canonical
JSON) and, for every page of the chosen companies, the SHA-256 of the page file and of the words file as they are on
disk. The index is the run's own (Job 51g): `source_freeze.index` records its absolute path, file name and hash, and
a resume (resume_run, and the job itself before it reads) reads that file, never the server's default index; if the
file is gone the resume is refused with that reason. It refuses when the index hash, the set of pages, or any file's
bytes differ: "source set changed; historical replay would not be the same run". Nothing is re-fetched and no page
is added on a resume. A run started before Job 51f has no `source_freeze`; its resume is refused until a person
records one with `record_source_freeze` (python -m server.run_steps, or POST /api/runs/{id}/source-freeze), which
takes the index file the run was read from and the hash the Operator checked by hand, and records the index's path
and the current files only if they match it.

Any call that could not be priced and that a person has not accepted (unpriced_calls > unknown_cost_accepted) stops a
resume until a person sends accept_unknown_cost true (Job 51g: also on a run with no spending limit); the acceptance is
the Person step "Unknown cost accepted".

A run left "running" by a worker that was stopped from outside (a SIGTERM) is put right by `record_operator_stop`
(python -m server.run_steps, or POST /api/runs/{id}/operator-stop; the same function). It reads
runs/<id>/OPERATOR_STOP.json, a JSON object with exactly these fields:
    stopped_at                 required: when the worker was stopped, ISO 8601 with a time zone ("2026-10-31T21:31:44Z")
    signal                     required: the signal it was stopped with ("SIGTERM"), SIG followed by capitals/digits
    potentially_billed_calls   optional, default 1: the calls that were in flight and may have been billed, a whole
                               number from 0 (0 only when the provider's own record shows nothing was billed)
    note                       optional: one line of text, or null
Any other field, a wrong type, or a stop time in the future is refused with the reason. It refuses a run that is not
"running", one that is driven by this process, one whose job.json was written after stopped_at (the worker may still
be going), and one already recorded (it is never applied twice). It adds potentially_billed_calls to job.json
`unpriced_calls`, writes a Person step "Operator stop recorded" naming the stop and the unknown cost, and sets the
status to needs_input, "stopped by the Operator; N potentially billed call(s), cost unknown". It never resumes the
run and never accepts the cost: a resume then goes through the unknown-cost gate (accept_unknown_cost true from a
person, and a higher limit if the run has one); `unknown_cost_accepted` is left as it was. It writes job.json (the
counters and `operator_stop`) and then run.json; a crash between the two leaves job.json with `operator_stop` and
run.json "running", and running it again completes the run.json write without touching a counter (Job 51g). It is
refused as applied twice only when both are written.
"""

from __future__ import annotations

import hashlib
import json
import copy
import hashlib
import math
import os
import re
import threading
import calendar as _calendar
from datetime import date, datetime, timezone
from pathlib import Path

from accuracy import types as T
from capture import read_events, read_table, reader_tools, sam as sam_module
from capture import research_instructions
from capture.reader import Reply
from capture.http import MAX_COMPLETION_TOKENS
from capture.second_reader import CAP_REACHED, OPENROUTER_CAP_USD, UNPRICED
from checks.common import normalize
from checks.coverage import sentence_covered, sentences as coverage_sentences
from capture.capture_source import load_entry
from checks import event_checks
from runs import picture, build_entry, build_run, compare, coverage, escalate, prices, scan, sources
from runs.ids import reserve_row_numbers, require_unique_target_ids
from runs import page_outcomes
from runs.one_model import MODELS_CHOICES, NO_SECOND_READER, ONE_MODEL_LINE, one_model  # noqa: F401
from runs.state import _real_disagreement, as_of_note, derive_status, run_reference_date


def build_table_for_page(reading: dict, words_doc: dict, source: dict, consumed_artifact: dict,
                         table_id: str) -> dict:
    """Build a table using the visual capability recorded for its captured page."""
    page = source["pages"][0]
    return build_run.build_table(
        reading, words_doc, table_id=table_id, source_id=source["id"],
        page_number=page["page_number"], source=source, consumed_artifact=consumed_artifact,
        visual_evidence=page.get("visual_evidence"),
        derived_words_present=bool(page.get("derived_words")))

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
                 "reader_tools", "models", "mode", "research_limits", "support_check", "turn_limit_per_company")
MODES = ("pages", "research")


# One lock for the run.json / job.json files of every run in this process.
RUN_LOCK = threading.RLock()

# The per-run budget gate, shared by the run worker and the voice route. Before either paid call, under RUN_LOCK, the
# call's bounded worst case is added here as a reservation; the call is refused if spend + reservations would pass the
# limit. The reservation is released when the call is counted (or abandoned).
MODEL_CALL_UNBOUNDED = math.inf      # a reader call has no token cap, so its reservation is unbounded in the table
_RESERVED: dict[str, dict[int, float]] = {}
_NEXT_TOKEN = [0]


_OWNERS: dict[int, object] = {}      # INTEGRATION-002: who holds each reservation token (None: the caller holds it)


def reserve_budget(run_id: str, spent: float, limit, worst: float, alone_may_cross: bool = False,
                   owner=None) -> int | None:
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
        if owner is not None:
            _OWNERS[_NEXT_TOKEN[0]] = owner
        return _NEXT_TOKEN[0]


def release_budget(run_id: str, token: int | None) -> None:
    if token is None:
        return
    with RUN_LOCK:
        held = _RESERVED.get(run_id, {})
        held.pop(token, None)
        _OWNERS.pop(token, None)
        if not held:
            _RESERVED.pop(run_id, None)


def release_owned(run_id: str, owner) -> int:
    """INTEGRATION-002: release only the reservations `owner` holds on this run (a finished job's cleanup), never a
    sibling's outstanding ticket such as a voice call in flight. Returns how many were released."""
    with RUN_LOCK:
        mine = [t for t in _RESERVED.get(run_id, {}) if _OWNERS.get(t) is owner]
    for t in mine:
        release_budget(run_id, t)
    return len(mine)


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
    """`field`: the request field the refusal is about, when the request lacks a value only the caller can supply
    (the server answers such a refusal 422, not 409). None for a refusal about the run's state."""
    def __init__(self, status: int, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.message, self.field = status, message, field


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


def past_reason(row: dict, run_date: date, source: dict | None = None) -> str | None:
    """Compatibility entry point: a printed date cannot justify excluding an event.

    PIVOT leaves every interpretation for review. After explicit acceptance,
    the shared calendar partition places past dates in Past, without dropping
    the source or pretending that date comparison established the event.
    """
    return None


def past_candidate_reason(row: dict, run_date: date) -> str | None:
    """A date-only note, never proof that the proposed event is historical."""
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
        return f"Proposed period ended {end.isoformat()} before this run began ({run_date.isoformat()})."
    return f"Proposed date {start.isoformat()} was before this run began ({run_date.isoformat()})."


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


def selectable_companies(index: sources.Index) -> tuple[list[str], list[str]]:
    """The one rule for what ["all"] stands for, shared by validate_choices and GET /api/companies.

    selectable: every company with a page to read, in index order (a company whose only page is the
    scanned variant is not one of them). listed_without_pages: the companies the index names but holds
    no page for, never a company that has pages."""
    selectable = [t for t in index.tickers if any(p["kind"] != "scan_events" for p in index.pages(t))]
    listed_without_pages = [c for c in index.empty if c not in index.tickers]
    return selectable, listed_without_pages


def validate_choices(body: dict, index: sources.Index | None = None) -> dict:
    """The New run panel's choices, or a ValueError with one plain sentence. `companies` may be ["all"]: every company in
    the sources index (the scanned variant of a page is not one of them), recorded as the list it stands for."""
    if not isinstance(body, dict):
        raise ValueError("The choices must be a JSON object.")
    extra = set(body) - set(CHOICE_FIELDS)
    if extra:
        raise ValueError(f"Unknown choice: {', '.join(sorted(extra))}.")
    mode = body.get("mode") or "pages"
    if mode not in MODES:
        raise ValueError('mode must be "pages" (read the captured pages) or "research" (the research agent).')
    if mode == "research":
        return validate_research_choices(body)
    if body.get("research_limits") is not None:
        raise ValueError("research_limits go with mode research.")
    if body.get("support_check") is not None:
        raise ValueError("support_check goes with mode research.")
    if body.get("turn_limit_per_company") is not None:
        raise ValueError("turn_limit_per_company goes with mode research.")
    index = index or sources.load_index()
    companies, types = body.get("companies"), body.get("event_types", list(read_events.EVENT_TYPES))
    if not isinstance(companies, list) or not companies or not all(isinstance(c, str) for c in companies):
        raise ValueError("Choose at least one company.")
    if companies == ["all"]:
        # every company with a page to read; one the index lists with no page (ABVX in the captured index) is left out
        companies = selectable_companies(index)[0]
        if not companies:
            raise ValueError("The captured-pages file holds no page to read.")
    empty = [c for c in companies if c in index.empty and c not in index.tickers]
    unknown = [c for c in companies if c not in index.tickers and c not in index.empty]
    if unknown:
        raise ValueError(f"No source has been captured for {', '.join(unknown)} yet. "
                         f"Captured so far: {', '.join(sorted(index.tickers))}.")
    if empty:
        raise ValueError(f"The captured-pages file lists {', '.join(empty)} but holds no page for "
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
    note = research_instructions.validate(note)
    tools = body.get("reader_tools", False)
    if tools is None:
        tools = False
    if not isinstance(tools, bool):
        raise ValueError("reader_tools must be true or false.")
    models = body.get("models", "all")
    if models is None:
        models = "all"
    if models not in MODELS_CHOICES:
        raise ValueError('models must be "one" (Muse Spark only) or "all" (Muse Spark, checked by a second reader).')
    return {"companies": list(companies), "event_types": list(read_events.EVENT_TYPES), "time_window_months": 6,
            "spending_limit_usd": limit, "note_for_agent": note,
            "reader_tools": tools, "models": models}


def validate_research_choices(body: dict) -> dict:
    """RESEARCHER-FULL-001: the research agent's choices. Companies are tickers (up to 10; nothing need be captured
    yet). The spending cap is the one limit that is on; it defaults to $30 (CVA, 2026-11-02). Every other limit in
    research_limits is off (null) unless a person turns it on (runs/research.py LIMIT_KEYS)."""
    from runs import research
    companies = research.validate_research_companies(body.get("companies"))
    limit = body.get("spending_limit_usd")
    limit = research.DEFAULT_SPEND_CAP_USD if limit is None else limit
    if not _finite_limit(limit):
        raise ValueError("The spending limit must be a finite number of dollars, zero or more.")
    note = research_instructions.validate(body.get("note_for_agent"), default=research_instructions.RESEARCH_DEFAULT)
    if body.get("reader_tools") not in (None, False):
        raise ValueError("reader_tools is a page-mode choice; a research run always uses its tools.")
    if body.get("models") not in (None, "one"):
        raise ValueError('A research run uses Muse Spark only (models "one"); the independent reviewer is stage S2.')
    return {"companies": companies, "event_types": list(read_events.EVENT_TYPES), "time_window_months": 6,
            "spending_limit_usd": limit, "note_for_agent": note, "reader_tools": False, "models": "one",
            "mode": "research",
            "research_limits": research.validate_turn_limit(body.get(research.TURN_LIMIT_FIELD),
                                                            research.validate_limits(body.get("research_limits"))),
            # LETTUCE-OPTION: present only when a person turns the support check on, so a run with it off keeps
            # exactly today's choices (and run.json bytes)
            **research.validate_support_check(body.get("support_check"))}


def label_for(number: int, choices: dict) -> str:
    """The run's label, from its choices: "12 companies · 6 months", "PTGX · 12 months", "IONS" (no window)."""
    companies = choices["companies"]
    who = companies[0] if len(companies) == 1 else f"{len(companies)} companies"
    if choices.get("mode") == "research":
        return f"{who} · research"
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
        if one_model(choices):
            run["run"]["trail"] = [{"step": "Models chosen", "tag": "Person", "who": "person", "at": now,
                                    "text": ONE_MODEL_LINE}]
        if as_of:       # the one reference date (runs.state.run_reference_date); absent = the start day
            run["run"]["as_of"] = as_of
        (run_dir / "decisions.jsonl").write_text("")
        _write(run_dir / "job.json", {"attempts": {}, "next_row_number": 1, "unpriced_calls": 0,
                                      "second": {}, "openrouter_spend_usd": 0.0,
                                      "openrouter_unpriced_calls": 0,
                                      "log": [snapshot(run["run"], "Run created.")]})
        _write(run_dir / "run.json", run)
        if choices.get("mode") == "research":
            # frozen at admission: the exact effective request per stage (runs/research_request.py)
            from runs import research_request
            research_request.freeze(run_dir, run)
        return run


def unpriced_count(job: dict) -> int:
    n = job.get("unpriced_calls")
    return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 else 0


def accepted_count(job: dict) -> int:
    """How many unpriced calls the person accepted on a resume (`unknown_cost_accepted`); 0 when none. job.json keeps
    it; GET /api/runs/{id} carries it to the UI beside the run (run.json's schema is not widened for it)."""
    n = job.get("unknown_cost_accepted")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def unpriced_marker(n: int, accepted: int = 0) -> str:
    """The one marker every consumer prints (run card status_reason, report Summary) while a call is unpriced.
    Before the person accepts every unpriced call the cost is unknown; once accepted, the limit bounds counted
    spend only. ui/app.js `unpricedMark` prints the same two strings."""
    if accepted >= n:
        return f"{n} unpriced call(s); limit bounds counted spend"
    return f"{n} unpriced call(s); cost unknown"


def unknown_unaccepted(job: dict) -> bool:
    """True when the run holds a call that could not be priced which the person has not accepted (Job 51g: with or
    without a spending limit, a resume then needs accept_unknown_cost true)."""
    return unpriced_count(job) > accepted_count(job)


def limit_unpriced(limit, job: dict) -> bool:
    """True when a run has a spending limit and a call that could not be priced which the person has not accepted:
    its spend total is not reliable. `unknown_cost_accepted` is how many unpriced calls the person accepted."""
    return limit is not None and unknown_unaccepted(job)


# ---------------------------------------------------------------- Job 51f: the source set is frozen at the start

SOURCE_SET_CHANGED = "source set changed; historical replay would not be the same run"
NO_SOURCE_FREEZE = ("This run started before its source set was recorded (Job 51f), so a resume cannot show it reads "
                    "the same sources. Record it with record_source_freeze after checking the captured-pages file hash by "
                    "hand, or start a new run.")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# the runs this process is driving now (run_job); a run listed here is not stale, whatever its run.json says
ACTIVE_RUNS: set[str] = set()


def _file_sha(path) -> str | None:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def index_fingerprint(index: sources.Index) -> dict:
    """The sources index a run reads: {"path", "sha256"}. A file index is hashed by its bytes; the built-in one (no
    file) by its records written as canonical JSON."""
    if index.path is not None and Path(index.path).is_file():
        p = Path(index.path).resolve()      # absolute, so a resume finds the run's own index from any working directory
        return {"path": str(p), "name": p.name, "sha256": _file_sha(p)}
    canon = json.dumps(index.by_ticker, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"path": None, "name": None, "sha256": hashlib.sha256(canon.encode()).hexdigest()}


def frozen_index(frozen: dict, given: sources.Index | None = None) -> sources.Index:
    """Job 51g: the sources index a run was frozen with, which is the run's own, never the server's default. A run
    frozen on an index file reads that file (path recorded in source_freeze); `given` is not used for it. A run frozen
    on an index with no file (the built-in one, or one held in memory) reads `given`, else the built-in index; its
    canonical hash is compared all the same. JobError 409 with a plain reason when the recorded file is not on disk."""
    path = (frozen.get("index") or {}).get("path")
    if not path:
        return given if given is not None else sources.builtin_index()
    p = Path(path)
    if not p.is_file():
        raise JobError(409, f"The captured-pages file this run was frozen with, {p.name}, is not on disk at {p}, so a "
                            f"resume cannot show it reads the same sources. Put that file back, or start a new run.")
    try:
        return sources.load_index(p)
    except sources.SourcesError as e:
        raise JobError(409, f"{SOURCE_SET_CHANGED}: {e}") from None


def source_set(index: sources.Index, companies: list[str]) -> dict:
    """What a run reads, as hashes: the index and, for each page of the chosen companies, its two files' bytes."""
    pages = []
    for ticker in companies:
        for page in index.pages(ticker):
            r = page["record"]
            pages.append({"source_id": page["source_id"], "ticker": r["ticker"],
                          "page_file": r["page_path"], "page_sha256": _file_sha(r["page_path"]),
                          "words_file": r["words_path"], "words_sha256": _file_sha(r["words_path"])})
    return {"index": index_fingerprint(index), "pages": pages}


def source_set_changes(frozen: dict, index: sources.Index, companies: list[str]) -> list[str]:
    """How the current source set differs from the frozen one, one plain phrase each; [] when it is the same."""
    now = source_set(index, companies)
    out = []
    was, is_ = frozen.get("index") or {}, now["index"]
    if was.get("sha256") != is_["sha256"]:
        name = Path(is_["path"]).name if is_["path"] else "the built-in captured-pages file"
        out.append(f"the captured-pages file ({name}) hashes to {is_['sha256']}, recorded {was.get('sha256')}")
    old = {p["source_id"]: p for p in frozen.get("pages") or []}
    new = {p["source_id"]: p for p in now["pages"]}
    added, gone = [s for s in new if s not in old], [s for s in old if s not in new]
    if added:
        out.append(f"pages not in the recorded set: {', '.join(added)}")
    if gone:
        out.append(f"recorded pages no longer listed: {', '.join(gone)}")
    for sid in (s for s in old if s in new):
        for what in ("page", "words"):
            if old[sid][f"{what}_sha256"] != new[sid][f"{what}_sha256"]:
                out.append(f"the {what} file of {sid} has changed bytes")
    return out


def source_set_refusal(changes: list[str]) -> str:
    return (f"{SOURCE_SET_CHANGED}: {'; '.join(changes)}. Nothing was re-fetched; start a new run to read the "
            f"current sources.")


def _check_source_freeze(job: dict, index: sources.Index | None, companies: list[str]) -> None:
    """Raise JobError 409 unless the run has a recorded source set and the run's own frozen index (frozen_index) and
    its files are still that set."""
    frozen = job.get("source_freeze")
    if not frozen:
        raise JobError(409, NO_SOURCE_FREEZE)
    index = frozen_index(frozen, index)
    changes = source_set_changes(frozen, index, companies)
    if changes:
        raise JobError(409, source_set_refusal(changes))


def record_source_freeze(runs_dir: Path, run_id: str, index: sources.Index, index_sha256) -> dict:
    """For a run started before Job 51f: record its source set now, from the files on disk, once a person has checked
    the sources index hash by hand and gives it here (`index_sha256`). Refused if the run already has one, is done or
    is driven by this process, the index is the built-in one, the index file does not hash to `index_sha256`, a file
    does not match the hash the index lists for it, or a page the run has read is not in the index or not the same
    words. Writes job.json `source_freeze` and a Person step "Source set recorded"; the status is left as it is."""
    if not isinstance(index_sha256, str) or not _HEX64.match(index_sha256):
        raise JobError(422, "index_sha256 must be the 64 lower-case hex digits of the hash you checked by hand.",
                       field="index_sha256")
    run_dir = Path(runs_dir) / run_id
    with RUN_LOCK:
        run = json.loads((run_dir / "run.json").read_text())
        meta = run["run"]
        job = json.loads((run_dir / "job.json").read_text())
        if job.get("source_freeze"):
            raise JobError(409, f"This run's source set was recorded at {job['source_freeze'].get('recorded_at')}; "
                                f"it is never recorded again.")
        if meta.get("status") in (None, "done"):
            raise JobError(409, "A finished run (or one saved before run controls) is never resumed; nothing to "
                                "record.")
        if run_id in ACTIVE_RUNS:
            raise JobError(409, "This run is being driven by this server now; its source set was recorded when it "
                                "started.")
        if index.path is None:
            raise JobError(409, "The built-in captured-pages file has no file to check by hand; record a run's source set "
                                "only from a captured-pages file.")
        found = index_fingerprint(index)
        if found["sha256"] != index_sha256:
            raise JobError(409, f"The captured-pages file {Path(index.path).name} hashes to {found['sha256']}, not the "
                                f"{index_sha256} you checked; nothing was recorded.")
        companies = meta["choices"]["companies"]
        frozen = source_set(index, companies)
        records = {p["source_id"]: p["record"] for t in companies for p in index.pages(t)}
        problems = []
        for p in frozen["pages"]:
            r = records[p["source_id"]]
            for what in ("page", "words"):
                if p[f"{what}_sha256"] != r[f"{what}_sha256"]:
                    problems.append(f"the {what} file of {p['source_id']} "
                                    f"{'is missing' if p[what + '_sha256'] is None else 'does not match the captured-pages file'}")
        listed = {p["source_id"]: p for p in frozen["pages"]}
        for s in run.get("sources", []):
            if s["id"] not in listed:
                problems.append(f"the run has read {s['id']}, which this captured-pages file does not list")
            elif any(pg.get("words_sha256") not in (None, listed[s["id"]]["words_sha256"])
                     for pg in s.get("pages", [])):
                problems.append(f"the words the run copied for {s['id']} differ from the words file now")
        if problems:
            raise JobError(409, f"{SOURCE_SET_CHANGED}: {'; '.join(problems)}. Nothing was recorded.")
        at = now_utc()
        job["source_freeze"] = {"recorded_at": at, "by": "record_source_freeze",
                                "checked_index_sha256": index_sha256, **frozen}
        text = (f"Source set recorded after the captured-pages file hash was checked by hand: "
                f"{frozen['index']['name']} ({frozen['index']['path']}) sha256 {index_sha256}; this run's resumes read "
                f"that captured-pages file, not the server's. {len(frozen['pages'])} page(s) of "
                f"{len(companies)} companies, each file's hash recorded. A resume is refused if any of them changes.")
        meta.setdefault("trail", []).append({"step": "Source set recorded", "tag": "Person", "who": "person",
                                             "at": at, "text": text})
        job["log"].append({"at": at, "note": text})
        _write(run_dir / "job.json", job)
        _write(run_dir / "run.json", run)
        return run


# ---------------------------------------------------------------- Job 51f: a stop from outside, recorded

OPERATOR_STOP_FILE = "OPERATOR_STOP.json"
OPERATOR_STOP_FIELDS = ("stopped_at", "signal", "potentially_billed_calls", "note")
_SIGNAL = re.compile(r"^SIG[A-Z0-9]+$")


def operator_stop_reason(n: int) -> str:
    if n == 0:      # the provider's record showed nothing in flight was billed: nothing is unknown
        return "stopped by the Operator; no potentially billed call"
    return f"stopped by the Operator; {n} potentially billed call(s), cost unknown"


def _parse_time(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def read_operator_stop(path: Path) -> dict:
    """OPERATOR_STOP.json, checked (see the module notes for its fields); JobError with a plain reason otherwise."""
    name = OPERATOR_STOP_FILE
    if not path.is_file():
        raise JobError(409, f"There is no {name} in this run's folder; the Operator writes it first.")
    try:
        doc = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        raise JobError(422, f"{name} is not valid JSON.") from None
    if not isinstance(doc, dict):
        raise JobError(422, f"{name} must be a JSON object.")
    extra = sorted(set(doc) - set(OPERATOR_STOP_FIELDS))
    if extra:
        raise JobError(422, f"{name} has a field it does not take: {', '.join(extra)}. "
                            f"It takes {', '.join(OPERATOR_STOP_FIELDS)}.")
    stopped = _parse_time(doc.get("stopped_at"))
    if stopped is None:
        raise JobError(422, f"{name}: stopped_at must be a time written ISO 8601 with a time zone, "
                            f"such as 2026-10-31T21:31:44Z.")
    if stopped > datetime.now(timezone.utc):
        raise JobError(422, f"{name}: stopped_at is in the future.")
    if not isinstance(doc.get("signal"), str) or not _SIGNAL.match(doc["signal"]):
        raise JobError(422, f"{name}: signal must be the name of a signal, such as SIGTERM.")
    n = doc.get("potentially_billed_calls", 1)
    if not _number(n, integer=True) or n < 0:
        raise JobError(422, f"{name}: potentially_billed_calls must be a whole number from 0 (default 1).")
    note = doc.get("note")
    if note is not None and (not isinstance(note, str) or "\n" in note):
        raise JobError(422, f"{name}: note must be one line of text, or null.")
    return {"stopped_at": doc["stopped_at"], "signal": doc["signal"], "potentially_billed_calls": n,
            "note": (note.strip() or None) if isinstance(note, str) else None, "_stopped": stopped}


def record_operator_stop(runs_dir: Path, run_id: str) -> dict:
    """Put right a run left "running" by a worker stopped from outside, from runs/<id>/OPERATOR_STOP.json. The one code
    path for the command line (server.run_steps) and the API. Returns run.json. See the module notes."""
    run_dir = Path(runs_dir) / run_id
    path = run_dir / OPERATOR_STOP_FILE
    with RUN_LOCK:
        run = json.loads((run_dir / "run.json").read_text())
        meta = run["run"]
        job = json.loads((run_dir / "job.json").read_text())
        done = job.get("operator_stop")
        if done:
            if _operator_stop_half_applied(done, meta) and run_id not in ACTIVE_RUNS:
                return _finish_operator_stop(run_dir, run, job)
            raise JobError(409, f"The Operator's stop of this run was recorded at "
                                f"{done.get('recorded_at')}; it is never applied twice.")
        status = meta.get("status")
        if status != "running":
            raise JobError(409, f"Only a run left \"running\" by a stopped worker can be recorded as stopped; this "
                                f"run is {status or 'saved before run controls'}.")
        if run_id in ACTIVE_RUNS:
            raise JobError(409, "This run is still going in this server; it is not a stale \"running\".")
        stop = read_operator_stop(path)
        written = [t for t in (_parse_time(e.get("at")) for e in job.get("log", []) if isinstance(e, dict)) if t]
        last = max(written, default=None)
        if last is not None and last > stop["_stopped"]:
            raise JobError(409, f"job.json was written at {last.isoformat()}, after the stop at {stop['stopped_at']}: "
                                f"the worker may still be going. Nothing was recorded.")
        at = now_utc()
        job["unpriced_calls"] = unpriced_count(job) + stop["potentially_billed_calls"]
        job["operator_stop"] = {"recorded_at": at, "file_sha256": _file_sha(path),
                                **{k: stop[k] for k in OPERATOR_STOP_FIELDS}}
        job["log"].append(snapshot({**meta, "status": "needs_input"},
                                   operator_stop_reason(stop["potentially_billed_calls"])))
        _write(run_dir / "job.json", job)       # the counters: written once, here
        return _finish_operator_stop(run_dir, run, job)


def _operator_stop_half_applied(done: dict, meta: dict) -> bool:
    """job.json carries the Operator's stop but run.json was never rewritten (a crash between the two writes): run.json
    is still "running" and has no "Operator stop recorded" step."""
    return meta.get("status") == "running" and not any(
        t.get("step") == "Operator stop recorded" for t in meta.get("trail", []) if isinstance(t, dict))


def _finish_operator_stop(run_dir: Path, run: dict, job: dict) -> dict:
    """The run.json half of record_operator_stop, from job.json's operator_stop alone: never touches a counter, so it
    may be done again after a crash (Job 51g)."""
    meta, stop = run["run"], job["operator_stop"]
    n = stop["potentially_billed_calls"]
    meta["unpriced_calls"] = unpriced_count(job)
    unknown = (f"{n} call(s) in flight may have been billed; their cost is unknown and is not in the spend, so a "
               f"resume needs a person's acceptance of the unknown cost (and a higher limit, if the run has one)."
               if n else "The provider's record shows no call in flight was billed.")
    text = (f"Stopped by the Operator at {stop['stopped_at']} ({stop['signal']}) while the run was going. {unknown} "
            f"Recorded from {OPERATOR_STOP_FILE}." + (f" Note: {stop['note']}" if stop.get("note") else ""))
    meta.setdefault("trail", []).append({"step": "Operator stop recorded", "tag": "Person", "who": "person",
                                         "at": stop["recorded_at"], "text": text})
    meta["status"], meta["status_reason"] = "needs_input", operator_stop_reason(n)
    _write(run_dir / "run.json", run)
    return run


def resume_run(runs_dir: Path, run_id: str, spending_limit_usd=None, accept_unknown_cost=False,
               index: sources.Index | None = None) -> dict:
    """Give a stopped run a fresh budget of turns and queue it again. Raises JobError if it cannot go on.
    `spending_limit_usd`: a new limit, higher than the old one; the only way past a spending-limit stop.
    `accept_unknown_cost`: the person accepts that calls which could not be priced are not in the spend; without it a
    run holding such a call is refused, with or without a spending limit (Job 51g; an unknown cannot be counted at a
    worst case by code).
    `index`: used only for a run frozen on an index with no file (frozen_index). A run frozen on an index file is
    compared with that file, its own, never the server's default (Job 51g); it must still be the run's frozen source
    set (Job 51f), else 409."""
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
        if choices.get("mode") == "research":
            raise JobError(409, "A research run is not resumed in S1: its captured sources and proposals stay as "
                                "they are. Start a new research run (with a higher spending cap if the cap stopped it).")
        _check_source_freeze(job, index, choices["companies"])
        limit = choices["spending_limit_usd"]
        if spending_limit_usd is not None:
            if not _finite_limit(spending_limit_usd):
                raise JobError(422, "The new spending limit must be a finite number of dollars, zero or more.")
            if limit is None:
                raise JobError(409, "This run has no spending limit to raise.")
            if spending_limit_usd <= limit:
                raise JobError(409, f"The new spending limit must be higher than the old one ({money(limit)}).")
        accepting = False
        if limit_unpriced(limit, job):
            if spending_limit_usd is None or accept_unknown_cost is not True:
                raise JobError(409, "A spending limit cannot be kept on this run: the cost of a call could not be "
                                    "counted. To go on, resume with a higher spending_limit_usd and "
                                    "accept_unknown_cost true (the limit then bounds counted spend only), or start "
                                    "a new run.", field="accept_unknown_cost")
            accepting = True
        elif unknown_unaccepted(job):       # Job 51g: no limit, and still an unknown nobody has accepted
            if accept_unknown_cost is not True:
                raise JobError(409, f"{unpriced_count(job)} call(s) on this run could not be priced; their cost is "
                                    f"unknown and not in the spend. To go on, resume with accept_unknown_cost true, "
                                    f"or start a new run.", field="accept_unknown_cost")
            accepting = True
        if limit is not None and spending_limit_usd is None and (meta["spend_usd"] or 0) >= limit:
            raise JobError(409, "The spending limit stops this run. Resume with a higher spending limit to "
                                "read more.")
        if accepting:
            n = unpriced_count(job)
            bound = (f"the limit of {money(spending_limit_usd)} bounds counted spend only" if limit is not None else
                     "the run has no spending limit; its spend counts priced calls only")
            meta.setdefault("trail", []).append({
                "step": "Unknown cost accepted", "tag": "Person", "who": "person", "at": now_utc(),
                "text": f"Unknown cost accepted: {n} call(s) could not be priced; {bound}"})
            job["unknown_cost_accepted"] = n      # unpriced_calls itself is never zeroed
        if spending_limit_usd is not None:
            text = f"Spending limit raised by you from {money(limit)} to {money(spending_limit_usd)}."
            meta.setdefault("trail", []).append({"step": "Spending limit raised", "tag": "Person", "who": "person",
                                                 "text": text, "at": now_utc()})
            job.setdefault("limit_changes", []).append({"at": now_utc(), "from": limit, "to": spending_limit_usd})
            choices["spending_limit_usd"] = spending_limit_usd
        meta["unpriced_calls"] = unpriced_count(job)
        meta.update(status="queued", turn_count=0, resumes=meta.get("resumes", 0) + 1,
                    status_reason=f"Resuming with a fresh budget of {meta['turn_limit']} turns.")
        job["log"].append(snapshot(meta, meta["status_reason"]))
        _write(run_dir / "job.json", job)
        _write(run_dir / "run.json", run)
        return run


def run_job(runs_dir: Path, run_id: str, reader, second=None, as_of: date | None = None, wonder=None,
            sam=None, index: sources.Index | None = None, research_tools=None, research_reader=None) -> None:
    """Carry the run on until it is done or a limit stops it. Never raises: an unexpected error ends it as failed.
    `second`: the second reader (capture/second_reader.py), or None for a run with one reader.
    `index`: the sources index (default: runs/sources_index.json, else the built-in one).
    `sam`: SAM 3.1 (capture/sam.py), which finds the regions of a scanned page.
    `wonder`: the escalation model (capture/wonder.py, Muse Wonder), or None for a run that never escalates.
    `as_of`: must equal the run's persisted as_of (else refused); if the run has none it is persisted. Coverage counts
    dates after the run's reference date (runs.state.run_reference_date) only."""
    try:
        mode = json.loads((Path(runs_dir) / run_id / "run.json").read_text())["run"]["choices"].get("mode")
    except (OSError, ValueError, KeyError, TypeError):
        mode = None
    if mode == "research":      # RESEARCHER-FULL-001: one loop per company on the Responses API (runs/research.py)
        from runs import research
        return research.run_research(runs_dir, run_id, research_reader or reader, research_tools, as_of)
    job = Job(Path(runs_dir) / run_id, reader, second, as_of, wonder, sam, index)
    with RUN_LOCK:
        ACTIVE_RUNS.add(job.dir.name)
    try:
        job.drive()
    except Exception as e:  # the type only: nothing that could carry request details
        job.stop("failed", f"The job stopped on an unexpected error ({type(e).__name__}). Resume to try again.")
    finally:
        with RUN_LOCK:
            ACTIVE_RUNS.discard(job.dir.name)
        release_budget(job.dir.name, job._held)
        job.remove_unrecorded_pages()


class Job:
    def __init__(self, run_dir: Path, reader, second=None, as_of: date | None = None, wonder=None, sam=None,
                 index: sources.Index | None = None):
        # The injected reader may be reused by concurrent jobs. Its gate and
        # fallback allowance belong to this job, never to the shared template.
        self.dir, self.reader, self.second, self.as_of, self.wonder = run_dir, reader, copy.copy(second), as_of, wonder
        self.sam = sam
        if self.second is not None and hasattr(self.second, "before_fallback"):
            self.second.before_fallback = self._before_fallback
        self.given_index = index                  # Job 51g: a frozen run reads its own index, not this one
        self.index = index or sources.load_index()
        self._held: int | None = None             # the budget reservation of the call in flight (count_turn releases it)
        self.current_page: str | None = None      # the page being worked on, so a failed stop is charged to it
        self.run = json.loads((run_dir / "run.json").read_text())
        self.job = json.loads((run_dir / "job.json").read_text())
        self.meta = self.run["run"]
        self.choices = self.meta["choices"]
        if one_model(self.choices):
            self.second = self.wonder = self.sam = None
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
            self.meta["unpriced_calls"] = unpriced_count(self.job)
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
        self._reconcile_voice_unpriced(disk)
        log = self.job["log"]
        missing = [e for e in disk.get("log", []) if e not in log]
        if missing:
            self.job["log"] = sorted(log + missing, key=lambda e: e.get("at") or "")      # stable: ties keep order

    def _reconcile_voice_unpriced(self, disk: dict) -> None:
        """INTEGRATION-001 (Manus 05:31): an unknown cost the voice route recorded while this worker held its own copy
        of job.json is added to this worker's ledger, once; the worker's later save never resets it."""
        disk_voice = disk.get("voice_unpriced_calls") or 0
        mine = self.job.get("voice_unpriced_calls") or 0
        if isinstance(disk_voice, int) and disk_voice > mine:
            self.job["unpriced_calls"] = unpriced_count(self.job) + (disk_voice - mine)
            self.job["voice_unpriced_calls"] = disk_voice
            models = self.job.setdefault("unpriced_models", [])
            for m in disk.get("unpriced_models") or []:
                if m not in models:
                    models.append(m)
            self.meta["unpriced_calls"] = unpriced_count(self.job)

    def stop(self, status: str, reason: str) -> None:
        if status == "failed" and self.current_page:
            failures = self.job.setdefault("page_failures", {})
            failures[self.current_page] = failures.get(self.current_page, 0) + 1
            self.job.setdefault("page_failure_reasons", {})[self.current_page] = reason
        n = unpriced_count(self.job)
        mark = unpriced_marker(n, accepted_count(self.job))
        if n and mark not in reason:
            reason = f"{reason} ({mark})"
        self.meta["status"], self.meta["status_reason"] = status, reason
        self.save(reason)

    def remove_unrecorded_pages(self) -> None:
        """A page is captured into the run's folder before it is read; if the run stops first (a limit, a failure), its
        files are in pages/ but run.json names nothing of them. Remove them, so pages/ holds exactly the pages run.json
        names (a resume captures the page again, to the same bytes)."""
        pages = self.dir / "pages"
        if not pages.is_dir():
            return
        named = {Path(p[k]).name for s in self.run["sources"] for p in s["pages"]
                 for k in ("image", "words") if p.get(k)}
        named.update(Path(p["derived_words"]["file"]).name for s in self.run["sources"] for p in s["pages"]
                     if (p.get("derived_words") or {}).get("file"))
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
        page_outcomes.update(self.meta, sid, skipped=True, reason=reason)
        self.meta.setdefault("skipped_pages", []).append({"source_id": sid, "ticker": ticker, "reason": reason})
        self.job["log"].append({"at": now_utc(), "note": f"{ticker} page {sid} skipped: {reason}"})
        self.save()

    def refresh_spend(self) -> None:
        """The persisted run.json spend is the truth (a voice call may have raised it since this worker last wrote)."""
        with RUN_LOCK:
            if getattr(self, "job", None) is not None:
                try:  # INTEGRATION-001: the unknown-cost ledger is refreshed with the spend, before every admission
                    self._reconcile_voice_unpriced(json.loads((self.dir / "job.json").read_text()))
                except (OSError, ValueError):
                    pass
            try:
                disk = json.loads((self.dir / "run.json").read_text())["run"].get("spend_usd")
            except (OSError, ValueError, KeyError):
                return
            if disk is not None:
                self.meta["spend_usd"] = disk

    def _before_fallback(self, silent_reply) -> bool:
        """Count the failed primary attempt, then ask the ordinary gate before OpenRouter.
        A possibly billed attempt stays unknown with or without a limit. False means no fallback."""
        with RUN_LOCK:
            self.refresh_spend()
            self._count_turn(silent_reply)
            release_budget(self.dir.name, self._held)
            self._held = None
            self.save(refresh=False)
            # the SAME gate every other paid attempt passes: unknown cost counted, limit, reservation
            return not self._stopped_before_a_call(self.second or self.reader)

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
        classification = (reply.extra or {}).get("billing_classification_reason")
        if classification:
            extra = reply.extra or {}
            self.job["log"].append({"at": now_utc(), "note": classification, "model": price_name(reply.model),
                                    **{k: extra[k] for k in ("route", "http_status") if k in extra}})
        if reply.images is not None:      # a model priced per image (SAM 3.1)
            cost = prices.image_cost_usd(price_name(reply.model), reply.images)
        else:
            cost = prices.cost_usd(price_name(reply.model), reply.prompt_tokens, reply.completion_tokens)
        if cost is None and not reply.error:      # Job 51k: limit or not
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
        counted = False
        if cost is None and ((reply.error and (reply.extra or {}).get("may_have_been_billed", reply.timed_out))
                             or not reply.error):
            # A failed attempt explicitly marked possibly billed has unknown cost;
            # an explicit known-unbilled classification (e.g. connect-only) does not.
            # Job 51h: a possibly-billed failure is counted whether or not the run has a spending limit;
            # Job 51k: so is a successful reply whose cost cannot be priced (no usage, or a returned model with no price)
            self.job["unpriced_calls"] += 1
            counted = True
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
            else:      # no bound can be computed: an unpriced call, limit or not (Job 51j)
                if not counted:      # (counted once: a reply with no cost may already be counted above)
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
            if unknown_unaccepted(self.job):      # Job 51h: no limit, an unknown cost nobody accepted: pause
                self.stop("needs_input", self.unknown_cost_reason())
                return True
            return False
        if not _finite_limit(limit):
            self.stop("needs_input", "The spending limit stored for this run is not a finite number of dollars, "
                                     "so it cannot be kept. Nothing more was read.")
            return True
        if limit_unpriced(limit, self.job) or not prices.has_price(price_name(reader.model)):
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

    def unknown_cost_reason(self) -> str:
        """Job 51h: the pause on a run with no spending limit when a possibly-billed call could not be priced."""
        # Job 51j: plain words on screen; the field name stays in the API error detail only
        return ("A call may have been billed but could not be priced. A person must accept the unknown cost before "
                "this run can resume.")

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
            if unknown_unaccepted(self.job):      # Job 51h: the last call's cost is unknown; a person accepts it first
                self.stop("needs_input", self.unknown_cost_reason())
                return True
            return False
        self.refresh_spend()
        if limit_unpriced(limit, self.job):
            self.stop("needs_input", self.unpriced_reason(limit))
            return True
        if (self.meta["spend_usd"] or 0) > limit:
            self.stop("needs_input", self.limit_reason(limit))
            return True
        return False

    # ------------------------------------------------------------ the run

    def source_set_holds(self) -> bool:
        """Job 51f. On the run's first start, record its source set (job.json source_freeze); afterwards, the index and
        files of the run's own frozen index (frozen_index; Job 51g: never the index this job was given, unless the run
        was frozen on one with no file) must be that set. False (and the run is stopped, needs_input) when they are
        not, when that index file is gone, or when a run that has already gone has no recorded set."""
        companies = self.choices["companies"]
        frozen = self.job.get("source_freeze")
        if not frozen:
            if self.meta.get("resumes") or self.meta.get("turns_total"):
                self.stop("needs_input", NO_SOURCE_FREEZE)
                return False
            self.job["source_freeze"] = {"recorded_at": now_utc(), "by": "run start",
                                         **source_set(self.index, companies)}
            return True
        try:
            self.index = frozen_index(frozen, self.given_index)
        except JobError as e:
            self.stop("needs_input", e.message)
            return False
        changes = source_set_changes(frozen, self.index, companies)
        if changes:
            self.stop("needs_input", source_set_refusal(changes))
            return False
        return True

    def drive(self) -> None:
        if not self.source_set_holds():
            return
        self.meta["status"], self.meta["status_reason"] = "running", "Reading the captured sources."
        selected_pages = [page for ticker in self.choices["companies"] for page in self.index.pages(ticker)]
        page_outcomes.initialize(self.meta, selected_pages, self.run, self.job)
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
        read = self.meta["pages_read"]
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
        sid = page["source_id"]
        kind = page["kind"]
        if kind == "scan_events":
            return self.read_scan_page(page)
        entry = page["record"]            # the index's record of the page: ticker, company, filing date, hashes, ...
        if kind == "events":
            types = read_events.EVENT_TYPES
            if not types:
                page_outcomes.update(self.meta, sid, skipped=True,
                                     reason="No event type was chosen for the page.")
                self.job["log"].append({"at": now_utc(), "note": f"{entry['ticker']} page skipped: no event type "
                                                                 f"was chosen."})
                return False
        try:
            source, words_doc, consumed_artifact = sources.capture_page_evidence(entry, self.dir)
        except sources.SourcesError as e:
            # a page whose files are missing or no longer match the index will not mend on a retry: skip it, go on
            self.skip_page(page, str(e))
            return False
        page_outcomes.update(self.meta, sid, captured=True)
        picture_state = None
        if kind == "events" and picture.is_picture_page(words_doc):
            rebuilt = self.rebuild_picture(source, words_doc, consumed_artifact)
            if rebuilt is None:
                return False                      # the run stopped (a limit, a failed call); a resume goes on from here
            words_doc, picture_state, consumed_artifact = rebuilt
        if kind == "events":
            prompt = read_events.build_prompt(words_doc, entry, types, self.choices["note_for_agent"],
                                              as_of=(run_reference_date(self.run).isoformat()
                                                     if run_reference_date(self.run) else None))
            who, parse = "capture.read_events", read_events.parse_events
        else:
            note = self.choices["note_for_agent"]
            prompt = read_table.build_prompt(words_doc, None if research_instructions.is_default(note) else note)
            who, parse = "capture.read_table", read_table.parse_table
        page_facts = {"words_sha256": consumed_artifact["sha256"], "words_used": consumed_artifact,
                      "page_sha256": entry["page_sha256"], "pin": entry["id"]}

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
                reply, prompt = self.converse(prompt, tool_box, sid)
                if reply is None:
                    return False
                if reply.timed_out:
                    no_answer = reply.error
                    break
            else:
                if self.stopped_before_a_call():
                    return False
                page_outcomes.update(self.meta, sid, attempted=True)
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
            page_outcomes.update(self.meta, sid, unreadable=True,
                                 reason=no_answer or (attempts[-1].get("problem") if attempts else "No readable answer."))
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
                                                         if tool_box is not None else None),
                                              consumed_artifact=consumed_artifact)
                        for i, ev in enumerate(parsed)]
            coverage_rows = coverage.possible_event_rows(words_doc, new_rows, source,
                                                         first_index=self.job["next_row_number"], at=at,
                                                         today=self.coverage_date(source), ticker=entry["ticker"],
                                                         company=entry["company"])
            self.job["next_row_number"] += len(coverage_rows)
            new_rows += coverage_rows
            self.add_picture_trail(new_rows, picture_state, at)
            self.apply_window(new_rows, at, source=source)
            self.run["rows"] += new_rows
            self.mark_fake(reading)
        else:
            reading = read_table._reading_record(model=reply.model, prompt=prompt, raw=reply.raw, table=parsed,
                                                 problem=None, extra={**(reply.extra or {}), **page_facts})
            self.run["tables"].append(build_table_for_page(
                reading, words_doc, source, consumed_artifact, table_id=f"t{len(self.run['tables']) + 1}"))
            self.mark_fake(reading)
        self.run["sources"].append(source)
        if parsed is not None:
            page_outcomes.update(self.meta, sid, read=True)
        self.save()
        return True

    def converse(self, prompt: str, box: "reader_tools.ToolBox", source_id: str | None = None):
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
            if source_id:
                page_outcomes.update(self.meta, source_id, attempted=True)
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

    def apply_window(self, rows: list[dict], at: str, *, source: dict | None = None) -> None:
        """Keep dated history as proposals; an accepted past event is placed in Past."""
        run_date = run_reference_date({"run": self.meta})
        for row in rows:
            if row.get("group") == "removed" or not row.get("date"):
                continue
            if candidate := past_candidate_reason(row, run_date):
                proposal = candidate + " The date-to-event relationship needs your review; the proposal was not excluded."
                if row.get("past_exclusion_proposal") != proposal:
                    row["past_exclusion_proposal"] = proposal
                    row.setdefault("trail", []).append({"step": "Past date kept for review", "tag": "Code",
                        "who": "server.jobs", "at": at, "text": proposal})

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

    def rebuild_picture(self, source: dict, words_doc: dict, consumed_artifact: dict):
        """A page whose text layer is hidden (runs/picture.py): SAM 3.1 finds regions (one counted, priced turn per
        concept), the image reader transcribes each region's crop (one turn each), and a typed derived words artifact
        is written beside (never over) the captured words. Returns (new words_doc, state), or (words_doc, None) when it cannot be done
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
            return words_doc, None, consumed_artifact
        w, h = float(page0["width_pt"]), float(page0["height_pt"])
        png, image_identity = sources.load_page_image_evidence(self.dir, page0)
        st = self.job.setdefault("picture", {}).setdefault(sid, {"by_concept": {}, "calls": [], "reads": {}})
        st["image_sha256"] = image_identity["sha256"]
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
                    if (reply.extra or {}).get("fallback_skipped"):
                        # The admission gate already paused the run. A refusal
                        # is not a page failure and must not lead to skip_page.
                        self.save()
                        return None
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
            return words_doc, st, consumed_artifact   # the state goes on: every attempted call still gets its trail step
        rebuilt = st["words_doc"]
        derived_rel = str(Path(page0["words"]).with_suffix(".transcription.json"))
        derived_path = self.dir / derived_rel
        derived_bytes = (json.dumps(rebuilt, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        derived_path.write_bytes(derived_bytes)
        page0.update({"kind": picture.KIND, "words_from": picture.WORDS_FROM,
                      "note": picture.source_note(st["kept_regions"]), "regions": st["kept_regions"],
                      "derived_words": {"kind": "model_transcription", "file": derived_rel,
                                        "sha256": hashlib.sha256(derived_bytes).hexdigest(),
                                        "based_on_image_sha256": image_identity["sha256"],
                                        "geometry": "approximate",
                                        "models": sorted({x["model"] for x in st["reads"].values()})}})
        page0["visual_evidence"] = sources.visual_evidence(page0, rebuilt)
        return rebuilt, st, {"file": derived_rel, "sha256": hashlib.sha256(derived_bytes).hexdigest(),
                             "kind": "derived"}

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
        words_doc, consumed_artifact = sources.load_page_evidence(self.dir, page0)
        image, image_identity = sources.load_page_image_evidence(self.dir, page0)  # exact image sent to reader
        note = self.choices["note_for_agent"]
        ref_date = run_reference_date(self.run)
        entry = page["record"]
        if page["kind"] == "events":
            types = read_events.EVENT_TYPES
            attempts = [("image", read_events.build_image_prompt(entry, types, note,
                                                                  as_of=ref_date.isoformat() if ref_date else None), image),
                        ("image_and_words", read_events.build_prompt(words_doc, entry, types, note,
                                                                       as_of=ref_date.isoformat() if ref_date else None), image)]
            parse = read_events.parse_events
        else:
            table_note = None if research_instructions.is_default(note) else note
            attempts = [("image", read_table.build_image_prompt(table_note), image),
                        ("image_and_words", read_table.build_prompt(words_doc, table_note), image)]
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
                if (reply.extra or {}).get("fallback_skipped"):
                    # The admission gate has already paused the job. Keep its
                    # reason and the first reading, not a generic failed state.
                    self.note_second(sid, "unreadable", reply.error, price_name(reply.model), got)
                    self.save()
                    return False
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
        extra = {**(reply.extra or {}), "input": got,
                 "words_sha256": consumed_artifact["sha256"] if got == "image_and_words" else None,
                 "words_used": consumed_artifact if got == "image_and_words" else None,
                 "image_sha256": image_identity["sha256"], "page_sha256": entry["page_sha256"], "pin": entry["id"],
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
                                               next_index=next_row, consumed_artifact=consumed_artifact)
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
            page_outcomes.update(self.meta, page["source_id"], skipped=True,
                                 reason="No event type was chosen for the scanned page.")
            self.job["log"].append({"at": now_utc(), "note": f"{entry['ticker']} scanned page skipped: no event type "
                                                              f"was chosen."})
            return False
        if self.sam is None or self.sam.ready():
            reason = ("No SAM 3.1 reader is available, so a scanned page cannot be read." if self.sam is None
                      else self.sam.ready())
            self.stop("needs_input", reason)
            return False
        captured_source, _captured_words, _captured_identity = sources.capture_page_evidence(page["record"], self.dir)
        png, image_identity = sources.load_page_image_evidence(self.dir, captured_source["pages"][0])
        words = {"words": [], "page_width": captured_source["pages"][0]["width_pt"],
                 "page_height": captured_source["pages"][0]["height_pt"]}
        w, h = float(words["page_width"]), float(words["page_height"])
        st = self.job.setdefault("scan", {}).setdefault(scan.SOURCE_ID, {"by_concept": {}, "calls": [], "reads": {}})
        st["image_sha256"] = image_identity["sha256"]
        for concept in sam_module.CONCEPTS + (sam_module.FALLBACK,):
            if concept in st["by_concept"]:
                continue
            if concept == sam_module.FALLBACK and not scan.needs_fallback(st["by_concept"]):
                break
            if self.stopped_before_a_call(self.sam):
                return False
            reply = self.sam.segment(png, concept)
            page_outcomes.update(self.meta, page["source_id"], attempted=True)
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
        ref_date = run_reference_date(self.run)
        for region in regions:
            if region["id"] in st["reads"]:
                continue
            crop = scan.crop_box(region, w, h)
            date_context = (f"Run reference date: {ref_date.isoformat()}. Use this date to distinguish upcoming from "
                            f"historical developments.\n" if ref_date else "")
            prompt = date_context + scan.region_prompt(entry, region, types, note)
            if self.stopped_before_a_call(self.reader):
                return False
            reply = self.reader.call(prompt, image=scan.crop_png(png, crop, w))
            page_outcomes.update(self.meta, page["source_id"], attempted=True)
            self.count_turn(reply)
            if reply.error:
                self.stop("failed", f"{reply.error} Resume to try again.")
                return False
            events, problem = read_events.parse_events(reply.raw)
            st["reads"][region["id"]] = {"region_id": region["id"], "crop": crop, "prompt_text": prompt,
                                         "model": reply.model, "events": events, "parse_ok": events is not None,
                                         "parse_problem": problem,
                                         "usage": {"prompt_tokens": reply.prompt_tokens,
                                                   "completion_tokens": reply.completion_tokens}}
            self.save()
        source = scan.mark_scanned_source(captured_source, regions)
        by_id = {r["id"]: r for r in regions}
        reads = [{**r, "region": by_id[r["region_id"]]} for r in st["reads"].values() if r["region_id"] in by_id]
        first = reserve_row_numbers(self.run, self.job, 0)
        rows = scan.assemble_rows(reads, source, st["calls"], now_utc(), self.dir, first_index=first)
        self.job["next_row_number"] += len(rows)
        self.apply_window(rows, now_utc())
        self.run["rows"] += rows
        self.run["sources"].append(source)
        page_outcomes.update(self.meta, page["source_id"],
                             **page_outcomes.interpretation(reads, bool(st["calls"] or reads)))
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
        words_doc, consumed_artifact = sources.load_page_evidence(self.dir, page0)
        image, image_identity = sources.load_page_image_evidence(self.dir, page0)
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
            text = escalate.prompt(entry, trig, y_range, words_doc, types, self.choices["note_for_agent"], row=row)
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
            extra = {**(reply.extra or {}), "input": "image", "image_sha256": image_identity["sha256"],
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
            outcome = escalate.apply(row, event, reading, source, words_doc, now_utc(), trig, self.run, share, text,
                                     consumed_artifact=consumed_artifact)
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
        second reader once it has read. A repair may correct timing on the unchanged event passage. A different
        timing sentence is retained as a proposal for a person; it never replaces the event quotation.
        True when the page is finished with; False when the run stopped."""
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
        words_doc, consumed_artifact = sources.load_page_evidence(self.dir, page0)
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
                                      entry=entry, extra={**(reply.extra or {}), "words_sha256": consumed_artifact["sha256"],
                                                          "words_used": consumed_artifact,
                                                          "usage": usage})
        share = (reply.prompt_tokens, reply.completion_tokens, len(rows))
        at = now_utc()
        for row in rows:
            self.apply_send_back(row, answers.get(row["id"]), reading, source, words_doc, share, prompt, at,
                                 consumed_artifact)
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
                        share: tuple, prompt: str, at: str, consumed_artifact: dict | None = None) -> None:
        who = "server.jobs"
        pt = reading["usage"]["prompt_tokens"]
        row["trail"].append({"step": "Sent back for the date", "tag": "Code", "who": who, "at": at,
                             "text": "The date text is not part of the quote. The same reader was asked once for the "
                                     "sentence printed on the page that states the date."})
        row["trail"].append({"step": "Instructions sent", "tag": "Code", "who": "capture.read_events", "at": at,
                             "text": prompt, "tokens": {"prompt": pt, "completion": None} if isinstance(pt, int) else None})
        quote = (answer or {}).get("quote")
        date_text = (answer or {}).get("date_text")
        if answer is not None:
            row["timing_proposal"] = {"model": price_name(reading["model"]), "at": at,
                                      "source_id": source.get("id"), "page_number": words_doc.get("page_number"),
                                      "proposal": copy.deepcopy(answer)}
        if source.get("id") != row.get("source_id") or words_doc.get("page_number") != row.get("page_number"):
            row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                                 "text": "The timing answer belongs to a different source or page. "
                                         "The event evidence stays unchanged; a person must review the relationship."})
            return
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
        # This is a code-built timing proposal, not a raw legacy reader event.
        # Its copied controlled category must not become a reader type hint.
        event.update(quote=quote, date_text=date_text, box=None, type_hint=None)
        proposal = build_entry.build_row(0, event, reading, source, words_doc, at, share=share,
                                         model_name=price_name(reading["model"]),
                                         consumed_artifact=consumed_artifact)
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
        if " ".join(quote.split()) != " ".join((row.get("quote") or "").split()):
            row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                                 "text": "The timing sentence is printed on this page, but it differs from the "
                                         "event quotation. Both are retained; code does not establish their relationship. "
                                         "The event quotation and qualifications stay unchanged. No mistake is counted. "
                                         "Proposed timing sentence: " + quote})
            return
        old = next(c for c in row["checks"] if c["name"] == "date_match")
        held = _real_disagreement(row.get("disagreement"))
        keep = ("status", "group", "reason") if held else ()
        for f in ("quote", "date_text", "date", "date_end", "box", "checks", "status", "group", "reason"):
            if f not in keep:
                row[f] = proposal[f]
        if held:       # an existing two-sided disagreement is preserved: only a person's fix settles it
            row["status"], row["group"], _ = derive_status(row)
        if row.get("found_by") == "second_reader":      # nobody else read it: it stays an open row of the second reader
            row["status"] = event_checks.row_status(row["checks"], second_reader_agrees=False)
            failed = row["status"] == event_checks.FAILED
            row["group"] = "open"
            row["reason"] = (f"{proposal['reason']} " if failed else "") + compare.ONLY_REASON
        row["trail"].append({"step": "Send-back result", "tag": "Code", "who": who, "at": at,
                             "text": f"The sentence is printed on the page. The row takes it; the checks decide: "
                                     f"{proposal['status']}."})
        repaired_date = next(c for c in proposal["checks"] if c["name"] == "date_match")
        if (proposal["status"] == "matched" and old["result"] == "fail"
                and repaired_date["result"] == "pass"):
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
        # INTEGRATION-001: a separate, only-growing count of unknown costs recorded by the voice route, which a run
        # worker reconciles into its own ledger (never overwrites) on every refresh and save
        job["voice_unpriced_calls"] = job.get("voice_unpriced_calls", 0) + 1
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
