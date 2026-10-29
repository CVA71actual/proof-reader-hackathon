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

time_window_months: a row whose date (a period's first day) is before today or later than today plus the window is
left out under the rule outside_time_window (group removed, an exclusion by code, never a mistake). A row with no
date is not touched.
"""

from __future__ import annotations

import json
import os
import threading
import calendar as _calendar
from datetime import date, datetime, timezone
from pathlib import Path

from capture import read_events, read_table, sam as sam_module
from capture.second_reader import CAP_REACHED, OPENROUTER_CAP_USD, UNPRICED
from checks.coverage import sentence_covered
from capture.capture_source import load_entry
from runs import build_entry, build_run, compare, coverage, escalate, prices, scan, sources

DEFAULT_TURN_LIMIT = 40
UNREADABLE_LIMIT = 3
THREE_REFUSALS = "The reader's answer could not be read three times; sent to a person."
RESUMABLE = ("paused_turn_limit", "needs_input", "failed")
TIME_WINDOW_MAX = 120

# Which pages exist for which company is the sources index (runs/sources.py): runs/sources_index.json when it exists, else
# the built-in one (PTGX 8-K exhibit page 3, IONS annual report page 95, and the PTGX page as a scan: no text layer, regions
# from SAM 3.1). "events" pages go to the event reader (both readers), "table" pages to the table reader.
CHOICE_FIELDS = ("companies", "event_types", "time_window_months", "spending_limit_usd", "note_for_agent")

# One lock for the run.json / job.json files of every run in this process.
RUN_LOCK = threading.RLock()


class JobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(timezone.utc).date()


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, _calendar.monthrange(year, month)[1]))


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


def validate_choices(body: dict, index: sources.Index | None = None) -> dict:
    """The New run panel's choices, or a ValueError with one plain sentence. `companies` may be ["all"]: every company in
    the sources index (the scanned variant of a page is not one of them), recorded as the list it stands for."""
    index = index or sources.load_index()
    if not isinstance(body, dict):
        raise ValueError("The choices must be a JSON object.")
    extra = set(body) - set(CHOICE_FIELDS)
    if extra:
        raise ValueError(f"Unknown choice: {', '.join(sorted(extra))}.")
    companies, types = body.get("companies"), body.get("event_types")
    if not isinstance(companies, list) or not companies or not all(isinstance(c, str) for c in companies):
        raise ValueError("Choose at least one company.")
    if companies == ["all"]:
        companies = [t for t in index.tickers if any(p["kind"] != "scan_events" for p in index.pages(t))]
    unknown = [c for c in companies if c not in index.tickers]
    if unknown:
        raise ValueError(f"No source has been captured for {', '.join(unknown)} yet. "
                         f"Captured so far: {', '.join(sorted(index.tickers))}.")
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
    if limit is not None and not (_number(limit) and limit >= 0):
        raise ValueError("The spending limit must be a number of dollars, zero or more, or null.")
    if note is not None and not isinstance(note, str):
        raise ValueError("The note for the agent must be text or null.")
    return {"companies": list(companies), "event_types": list(types), "time_window_months": window,
            "spending_limit_usd": limit, "note_for_agent": note.strip() or None if isinstance(note, str) else None}


def label_for(number: int, choices: dict) -> str:
    """The run's label, from its choices: "12 companies · 6 months", "PTGX · 12 months", "IONS" (no window)."""
    companies = choices["companies"]
    who = companies[0] if len(companies) == 1 else f"{len(companies)} companies"
    window = choices.get("time_window_months")
    return f"{who} · {window} month{'s' if window != 1 else ''}" if window else who


def create_run(runs_dir: Path, choices: dict, turn_limit: int | None = None) -> dict:
    """Make runs/<id>/ with run.json (queued), an empty decisions.jsonl and job.json. Returns the run.json dict.
    `turn_limit`: the budget of model calls before the run pauses (default DEFAULT_TURN_LIMIT)."""
    runs_dir = Path(runs_dir)
    with RUN_LOCK:
        numbers = []
        for f in runs_dir.glob("*/run.json"):
            try:
                numbers.append(int(json.loads(f.read_text())["run"]["number"]))
            except (ValueError, KeyError, TypeError):
                continue
        number = max(numbers, default=0) + 1
        while (runs_dir / f"run-{number}").exists():
            number += 1
        run_dir = runs_dir / f"run-{number}"
        (run_dir / "pages").mkdir(parents=True)
        now = now_utc()
        run = {
            "schema_version": build_run.SCHEMA_VERSION,
            "run": {
                "id": run_dir.name, "number": number, "label": label_for(number, choices),
                "sample": False, "started_at": now, "finished_at": None, "choices": choices, "spend_usd": None,
                "status": "queued", "status_reason": "Waiting to start.", "turn_count": 0,
                "turn_limit": turn_limit or DEFAULT_TURN_LIMIT, "turns_total": 0, "resumes": 0,
            },
            "sources": [], "tables": [], "rows": [], "mistakes": [], "exclusions": [],
        }
        (run_dir / "decisions.jsonl").write_text("")
        _write(run_dir / "job.json", {"attempts": {}, "unpriced_calls": 0, "second": {}, "openrouter_spend_usd": 0.0,
                                      "openrouter_unpriced_calls": 0,
                                      "log": [snapshot(run["run"], "Run created.")]})
        _write(run_dir / "run.json", run)
        return run


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
            if not (_number(spending_limit_usd) and spending_limit_usd >= 0):
                raise JobError(422, "The new spending limit must be a number of dollars, zero or more.")
            if limit is None:
                raise JobError(409, "This run has no spending limit to raise.")
            if spending_limit_usd <= limit:
                raise JobError(409, f"The new spending limit must be higher than the old one ({money(limit)}).")
        if limit is not None and job.get("unpriced_calls"):
            raise JobError(409, "A spending limit cannot be kept on this run: the cost of a call could not be "
                                "counted. Start a new run.")
        if limit is not None and spending_limit_usd is None and (meta["spend_usd"] or 0) >= limit:
            raise JobError(409, "The spending limit stops this run. Resume with a higher spending_limit_usd to "
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
    `as_of`: the day a date must be after to count as upcoming for the dated-sentence coverage; by default the
    source's published date, so an old filing's already-past dates raise no open rows."""
    job = Job(Path(runs_dir) / run_id, reader, second, as_of, wonder, sam, index)
    try:
        job.drive()
    except Exception as e:  # the type only: nothing that could carry request details
        job.stop("failed", f"The job stopped on an unexpected error ({type(e).__name__}). Resume to try again.")


class Job:
    def __init__(self, run_dir: Path, reader, second=None, as_of: date | None = None, wonder=None, sam=None,
                 index: sources.Index | None = None):
        self.dir, self.reader, self.second, self.as_of, self.wonder = run_dir, reader, second, as_of, wonder
        self.sam = sam
        self.index = index or sources.load_index()
        self.run = json.loads((run_dir / "run.json").read_text())
        self.job = json.loads((run_dir / "job.json").read_text())
        self.meta = self.run["run"]
        self.choices = self.meta["choices"]
        if self.meta["status"] == "done":
            raise JobError(409, "This run is finished; it is never written again.")

    # ------------------------------------------------------------ state

    def save(self, reason: str | None = None) -> None:
        with RUN_LOCK:
            self.job["log"].append(snapshot(self.meta, reason))
            _write(self.dir / "job.json", self.job)
            _write(self.dir / "run.json", self.run)

    def stop(self, status: str, reason: str) -> None:
        self.meta["status"], self.meta["status_reason"] = status, reason
        self.save(reason)

    def count_turn(self, reply) -> None:
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
        elif self.choices["spending_limit_usd"] is not None and not reply.error:
            self.job["unpriced_calls"] += 1
        self.save()

    def stopped_before_a_call(self, reader=None) -> bool:
        """The checks made before every model call. True (and the run is stopped) when it must not go on."""
        reader = reader or self.reader
        meta, limit = self.meta, self.choices["spending_limit_usd"]
        if meta["turn_count"] >= meta["turn_limit"]:
            self.stop("paused_turn_limit", f"Stopped at the turn limit ({meta['turn_limit']} turns). "
                                           f"Resume to continue with a fresh budget of turns.")
            return True
        if limit is None:
            return False
        if self.job["unpriced_calls"] or not prices.has_price(price_name(reader.model)):
            self.stop("needs_input", self.unpriced_reason(limit, reader))
            return True
        if (meta["spend_usd"] or 0) >= limit:
            self.stop("needs_input", self.limit_reason(limit))
            return True
        return False

    def limit_reason(self, limit: float) -> str:
        return (f"Stopped at the spending limit ({money(self.meta['spend_usd'] or 0)} of {money(limit)} used): "
                f"spending limit reached. Resume with a higher spending_limit_usd to read more.")

    def unpriced_reason(self, limit: float, reader=None) -> str:
        models = ", ".join(self.job.get("unpriced_models") or [getattr(reader, "model", "the model")])
        return (f"The cost of a call could not be counted (runs/prices.py has no price for {models}), so the spending "
                f"limit of {money(limit)} cannot be kept. Nothing more was read.")

    def stopped_after_the_last_call(self) -> bool:
        """The checks made when no call is left: a run is done only if every call stayed inside its spending limit. The
        call that crosses the limit stops the run even when it was the last one, and a call whose cost could not be
        counted (the reply named a model with no price) cannot be shown to have stayed inside."""
        limit = self.choices["spending_limit_usd"]
        if limit is None:
            return False
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
                if page["source_id"] not in done and not self.read_page(page) and self.meta["status"] != "running":
                    return
                if not self.second_pass(page) and self.meta["status"] != "running":
                    return
                if not self.escalation_pass(page) and self.meta["status"] != "running":
                    return
        if self.stopped_after_the_last_call():
            return
        read = len(self.run["sources"])
        self.meta["finished_at"] = now_utc()
        self.meta["status"] = "done"
        self.meta["status_reason"] = (f"Read {read} page{'s' if read != 1 else ''}: {len(self.run['rows'])} rows, "
                                      f"{sum(len(r['cells']) for t in self.run['tables'] for r in t['rows'])} values."
                                      if read else "Nothing to read: no captured page matches the choices.")
        self.save(self.meta["status_reason"])

    def read_page(self, page: dict) -> bool:
        """Read one captured page and add its source and rows (or tables). False when nothing was added."""
        kind = page["kind"]
        if kind == "scan_events":
            return self.read_scan_page(page)
        entry = page["record"]            # the index's record of the page: ticker, company, filing date, hashes, ...
        if kind == "events":
            types = tuple(t for t in read_events.EVENT_TYPES if t in self.choices["event_types"])
            if not types:
                self.job["log"].append({"at": now_utc(), "note": f"{entry['ticker']} page skipped: no event type "
                                                                 f"was chosen."})
                return False
        try:
            source, words_doc = sources.capture_page(entry, self.dir)      # captured on first use, hashes checked
        except sources.SourcesError as e:
            self.stop("failed", f"{e} Resume to try again.")
            return False
        if kind == "events":
            prompt = read_events.build_prompt(words_doc, entry, types, self.choices["note_for_agent"])
            who, parse = "capture.read_events", read_events.parse_events
        else:
            prompt = read_table.build_prompt(words_doc, self.choices["note_for_agent"])
            who, parse = "capture.read_table", read_table.parse_table
        page_facts = {"words_sha256": entry["words_sha256"], "page_sha256": entry["page_sha256"], "pin": entry["id"]}

        attempts = self.job["attempts"].setdefault(source["id"], [])
        reply, parsed = None, None
        while True:
            if self.stopped_before_a_call():
                return False
            reply = self.reader.call(prompt)
            self.count_turn(reply)
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
            self.run["rows"].append(self.refused_row(entry, source, prompt, who, attempts, at))
        elif kind == "events":
            extra = {**(reply.extra or {}), **page_facts, "usage": {"prompt_tokens": reply.prompt_tokens,
                                                                    "completion_tokens": reply.completion_tokens}}
            reading = read_events._record(model=reply.model, prompt=prompt, raw=reply.raw, events=parsed,
                                          problem=None, entry=entry, extra=extra)
            usage = {} if reading["fake"] else reading["usage"]
            n = len(parsed)
            shares = list(zip(prices.split_tokens(usage.get("prompt_tokens"), n),
                              prices.split_tokens(usage.get("completion_tokens"), n), [n] * n))
            first = len(self.run["rows"]) + 1
            new_rows = [build_entry.build_row(first + i, ev, reading, source, words_doc, at, share=shares[i])
                        for i, ev in enumerate(parsed)]
            new_rows += coverage.possible_event_rows(words_doc, new_rows, source, first_index=first + len(new_rows),
                                                     at=at, today=self.coverage_date(source), ticker=entry["ticker"],
                                                     company=entry["company"])
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
        self.save()
        return True

    def apply_window(self, rows: list[dict], at: str) -> None:
        """Leave out, under outside_time_window, a row dated before today or later than today plus the window."""
        months = self.choices["time_window_months"]
        if months is None:
            return
        now = today()
        last = add_months(now, months)
        for row in rows:
            try:
                day = date.fromisoformat(row["date"]) if row.get("date") else None
            except ValueError:
                day = None
            if day is None or now <= day <= last:
                continue
            when = (f"is before today ({now})" if day < now else
                    f"is later than {months} month{'s' if months != 1 else ''} from today ({last})")
            reason = f"Outside the time window: the date {row['date']} {when}."
            row["group"], row["reason"] = "removed", reason
            row["trail"].append({"step": "Left out", "tag": "Code", "who": "server.jobs", "text": reason, "at": at})
            self.run["exclusions"].append({"id": f"x{len(self.run['exclusions']) + 1}", "target": row["id"],
                                           "rule": "outside_time_window", "reason": reason, "by": "code"})

    def coverage_date(self, source: dict) -> date:
        """A dated sentence counts when its date is after this day: `as_of` if the run was given one, else the day the
        source was published (a filing's own date), never the day the run was made."""
        return self.as_of or date.fromisoformat(source["published_date"])

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

    def second_pass(self, page: dict) -> bool:
        """The second reader's independent read of a page the first reader has read, compared by code. True when
        the page is finished with (read, skipped or unreadable); False when the run stopped."""
        if page["kind"] == "scan_events":
            return True        # a scanned page is read region by region (read_scan_page); no second read of it yet
        sid = page["source_id"]
        source = next((s for s in self.run["sources"] if s["id"] == sid), None)
        if self.second is None or source is None or sid in self.job["second"]:
            return True
        if hasattr(self.second, "openrouter_allowed"):      # the cap, before readiness is asked
            unpriced = self.job.get("openrouter_unpriced_calls", 0)
            self.second.openrouter_allowed = (self.job.get("openrouter_spend_usd", 0.0) < OPENROUTER_CAP_USD
                                              and not unpriced)
            self.second.openrouter_blocked = None if self.second.openrouter_allowed else (
                UNPRICED if unpriced else CAP_REACHED)
        problem = self.second.ready()
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
            types = tuple(t for t in read_events.EVENT_TYPES if t in self.choices["event_types"])
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
        for got, prompt, png in attempts:
            if self.stopped_before_a_call(self.second):
                return False
            reply = self.second.call(prompt, image=png)
            self.count_turn(reply)
            if reply.error:
                self.stop("failed", f"The second reader: {reply.error} Resume to try again.")
                return False
            parsed, why = parse(reply.raw)
            if parsed is not None:
                break
        name = price_name(reply.model)
        if parsed is None:
            self.note_second(sid, "unreadable", why, name, got)
            self.save(f"The second reader's answer could not be read ({why}); nothing was compared.")
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
            ids = [int(r["id"][1:]) for r in self.run["rows"] if r["id"][1:].isdigit()]
            _, new_rows = compare.apply_events(rows, parsed, reading, source, words_doc, at,
                                               next_index=max(ids, default=0) + 1)
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
        types = tuple(t for t in read_events.EVENT_TYPES if t in self.choices["event_types"])
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
        source = scan.scanned_source(entry, self.dir, png, w, h, regions)
        by_id = {r["id"]: r for r in regions}
        reads = [{**r, "region": by_id[r["region_id"]]} for r in st["reads"].values() if r["region_id"] in by_id]
        ids = [int(r["id"][1:]) for r in self.run["rows"] if r["id"][1:].isdigit()]
        rows = scan.assemble_rows(reads, source, st["calls"], now_utc(), first_index=max(ids, default=0) + 1)
        self.apply_window(rows, now_utc())
        self.run["rows"] += rows
        self.run["sources"].append(source)
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
        rows = [r for r in escalate.candidates(page_rows) if r["id"] not in sent][: max(left, 0)]
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
        types = tuple(t for t in read_events.EVENT_TYPES if t in self.choices["event_types"])
        for row in rows:
            trig = escalate.trigger(row)
            y_range = escalate.band(row, words_doc)
            text = escalate.prompt(entry, trig, y_range, words_doc, types, self.choices["note_for_agent"])
            if self.stopped_before_a_call(self.wonder):
                return False
            reply = self.wonder.call(text, image=escalate.region_png(image, words_doc, y_range))
            self.count_turn(reply)
            if reply.error:
                self.stop("failed", f"Muse Wonder: {reply.error} Resume to try again.")
                return False
            events, _ = read_events.parse_events(reply.raw)
            events = events or []
            extra = {**(reply.extra or {}), "input": "image",
                     "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}}
            reading = read_events._record(model=reply.model, prompt=text, raw=reply.raw, events=events, problem=None,
                                          entry=entry, extra=extra)
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

    def mark_fake(self, reading: dict) -> None:
        if reading.get("fake"):
            self.meta["label"] = (f"Practice run ({', '.join(self.choices['companies'])}): "
                                  f"fake reading, no model called")

    def refused_row(self, entry: dict, source: dict, prompt: str, who: str, attempts: list, at: str) -> dict:
        """The third unreadable answer for a page: one row, group open, for a person to look at."""
        page_number = source["pages"][0]["page_number"]
        ticker = entry["ticker"]
        trail = [{"step": "Instructions sent", "tag": "Code", "who": who, "text": prompt, "at": at}]
        trail += [{"step": f"Answer {i} not understood", "tag": "Code", "who": who,
                   "text": a["problem"], "at": a["at"]} for i, a in enumerate(attempts, start=1)]
        trail.append({"step": "Sent to a person", "tag": "Code", "who": "server.jobs",
                      "text": THREE_REFUSALS, "at": at})
        return {
            "id": f"e{len(self.run['rows']) + 1}", "ticker": ticker, "company": entry["company"],
            "event_type": "unknown", "title": f"{ticker} page {page_number}: the reader's answer could not be read",
            "date_text": None, "date": None, "quote": None, "source_id": source["id"], "page_number": page_number,
            "box": None, "proposed_by": build_run.MUSE_SPARK, "status": "unreadable", "group": "open",
            "reason": THREE_REFUSALS, "sample": False, "checks": [], "trail": trail,
        }
