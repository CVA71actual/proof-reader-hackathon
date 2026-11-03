"""The research loop (RESEARCHER-FULL-001 S1): Muse Spark researches each company on the Responses API; code captures
and checks every claim, and the rows enter the existing run, state and decision log as proposals.

A research run is an ordinary run (server/jobs.create_run) whose choices carry mode "research". run_job hands it to
ResearchJob, a Job whose drive() runs one loop per company instead of reading the sources index. Everything else is the
existing run: run.json/job.json written whole under RUN_LOCK, rows built by runs.build_entry.build_row (so Keep,
Remove, Undo, issues, calendar and exports work unchanged), spend counted on the one per-run gate.

Limits (CVA ruling 2026-11-02: "I don't want any limits on anything without my approval"):
    The only limit that is ON is the run's spending cap (choices.spending_limit_usd; a research run defaults to
    DEFAULT_SPEND_CAP_USD = $30), kept through the existing gate (server/jobs.reserve_budget).
    Every other limit is an optional setting in choices.research_limits, OFF (null) unless a person turns it on:
    max_spend_per_company_usd, max_requests_per_company, max_searches_per_company, max_searches_per_request,
    max_fetches_per_company, max_pages_per_company, max_minutes_per_company, stop_on_empty_rate.
    concurrency (default 3) is a rate-limit safety setting only: every company is queued and runs; none is dropped.
    There is no page cap on a fetched source.
With the optional limits off, a company's loop ends only when: the model says it is done (finish_draft, or a final
answer in prose after one reminder to use the tools), the spend cap is reached (a recorded limitation), a person
stops the run (request_stop), or a technical failure (an error answer, a service that did not answer, an unexpected
error). Two recorded safety stops were APPROVED by CVA (LIMITS-002, 2026-11-02): two empty replies in a row stop that
company; a prose-only reply after one reminder counts as done. A third rule (stop after the same action with the same
result three times) is a PROPOSAL only and is not applied: repeats are recorded, nothing more. A limit or a stop never publishes anything: rows stay proposals, the run is not
"done", and the draft audit says the draft is partial. One company's failure is recorded and the others go on.

An identical action repeated with an identical result (same tool, same arguments, same answer) is recorded under
run.research.repeats and on the Agent trail; it does not stop the loop.

Each call's bounded worst case is reserved on the gate before it is made: input tokens bounded by the request's bytes
(plus the server-side history for previous_response_id) plus an assumed search-context allowance per search, output by
max_output_tokens, searches by max_searches_per_request when set, else an assumed allowance. The assumption is a
reservation only: the cost counted is the reply's own usage and search count at runs/prices.py prices; a call whose
input exceeded its reservation is recorded (reservation_exceeded). A call that cannot be priced (no usage, an unpriced
model, an uncountable search) is an unpriced call: the existing gate then stops paid work until a person accepts it. A
failed request that may have been billed is charged its reserved worst case.

What the run carries (run.json "run.research"; see runs/README.md): label, limits, companies (per-company progress,
status, limitation), steps (the Agent trail: request, search, fetch, proposal, check, coverage, finish, limit,
repeat, error), manifest (the run-owned source manifest summary), refusals, coverage, consolidations, repeats,
kill_conditions and the draft audit. Exact requests and responses are kept under research/turns/.
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

from capture import research_sources as rs
from capture import responses_reader as rr
from capture.capture_source import completeness_fields
from checks.common import normalize
from runs import build_entry, build_run, prices, research_contract as rc, sources
from runs.ids import reserve_row_numbers
from runs.state import run_reference_date

LABEL = rc.LABEL
# RESEARCHER-FULL-001 S2 wiring: the three labels a research result can carry. Never "verified".
LABEL_REVIEWED_CLEAN = "Agent research — independently reviewed (no findings)"
LABEL_REVIEWED_FINDINGS = "Agent research — independently reviewed with findings"
LABEL_PARTIAL_FINDINGS = "Agent research — partly reviewed (incomplete), findings so far"
LABELS = (LABEL, LABEL_REVIEWED_CLEAN, LABEL_REVIEWED_FINDINGS, LABEL_PARTIAL_FINDINGS)
# The review reserve: a code default (not a hidden limit; recorded in run.research.review_reserve and on the Agent
# trail). Research is admitted only under (1 - fraction) of the run's spending cap, so the independent review always
# has room to run; the review itself is admitted under the whole cap. No reserve is held when the reviewer cannot run.
REVIEW_RESERVE_FRACTION = 0.15
REVIEW_STAGE = "independent_review"
# MULTI-FAMILY-001 (register): the producer's provider and model family, recorded beside the reviewer's
RESEARCHER_PROVIDER = "meta"
RESEARCHER_FAMILY = "Muse Spark (Meta)"
DEFAULT_SPEND_CAP_USD = 30.0
DEFAULT_CONCURRENCY = 3
RESEARCH_TURN_LIMIT = 1_000_000     # a research run is not paused by the page-mode turn budget; turns are counted only
LIMIT_KEYS = ("max_spend_per_company_usd", "max_requests_per_company", "max_searches_per_company",
              "max_searches_per_request", "max_fetches_per_company", "max_pages_per_company",
              "max_minutes_per_company", "stop_on_empty_rate", "concurrency")
# Reservation assumptions (not limits): tokens the provider may add per search and per request beyond the bytes sent.
ASSUMED_SEARCHES_PER_REQUEST = 5
# 20,000 (was 8,000): the first live smoke run had a reply of 88,011 input tokens against a 57,279 reservation, so
# 8,000 per search understated the context web_search adds. Not a limit: it makes the reserved worst case honest.
SEARCH_CONTEXT_TOKENS = 20000
REQUEST_OVERHEAD_TOKENS = 4096
EDGAR_PAGE = 40
# A-TO-Z-001 (CVA standing design rule, 2026-11-02): one whole unit before the next. Find, capture, read, propose (or
# record nothing_material), code-check; then the next source. Lead searches (web_search, an SEC listing, a registry
# query) may be batched, but captured sources never pile up unprocessed: while a captured source is unsettled (not
# cited by a proposal and not marked nothing_material), capturing another is refused with a typed issue and recorded
# as a coverage gap. A design default, not a spend limit.
UNSETTLED_SOURCES_MAX = 1
# Bounded replay (live run 3: input regrew to 297,910 bytes by turn 45 through encrypted reasoning, proposal arguments,
# search results and stubs). The request replays only the last REPLAY_TURNS turns; reasoning items only from the
# latest turn (the one whose calls are being answered); search result lists only from the latest turn. Older turns
# are dropped as whole call/output pairs (the API's pairing stays valid) and replaced by one code-written summary of
# the research so far, built from the run's own ledger. Nothing is lost: every step, source and proposal is recorded.
REPLAY_TURNS = 3
LINE_WORDS_MAX = 40
STOP_FILE = "STOP_REQUESTED.json"
TICKER = re.compile(r"^[A-Z0-9.-]{1,10}$")
NCT = re.compile(r"^NCT[0-9]{8}$")
MAX_COMPANIES = 10

NUDGE_TOOLS = ("Use the tools: capture sources with the fetch tools, propose events with propose_events, record what "
               "you checked with record_coverage, and call finish_draft when you are done.")
NUDGE_EMPTY = ("Your last answer had no tool call and no text. Continue the research with a tool call, or call "
               "finish_draft if you are done.")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- choices


def validate_limits(raw) -> dict:
    """choices.research_limits: every key present, null = off. Unknown keys and wrong types are refused."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("research_limits must be an object.")
    extra = set(raw) - set(LIMIT_KEYS)
    if extra:
        raise ValueError(f"Unknown research limit: {', '.join(sorted(extra))}.")
    out = {}
    for k in LIMIT_KEYS:
        v = raw.get(k)
        if k == "concurrency":
            v = DEFAULT_CONCURRENCY if v is None else v
            if not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_COMPANIES):
                raise ValueError(f"concurrency must be a whole number from 1 to {MAX_COMPANIES}.")
        elif k == "stop_on_empty_rate":
            v = bool(v) if v is not None else False
            if not isinstance(raw.get(k, False), (bool, type(None))):
                raise ValueError("stop_on_empty_rate must be true, false or null.")
        elif v is not None:
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and v == v and v != float("inf"))
            if k != "max_spend_per_company_usd" and k != "max_minutes_per_company":
                ok = ok and isinstance(v, int)
            if not ok:
                raise ValueError(f"{k} must be a positive number, or null (off).")
        out[k] = v
    return out


def validate_research_companies(companies) -> list[str]:
    if not isinstance(companies, list) or not companies or not all(isinstance(c, str) for c in companies):
        raise ValueError("Choose at least one company.")
    out = [c.strip().upper() for c in companies]
    bad = [c for c in out if not TICKER.match(c)]
    if bad:
        raise ValueError(f"Not a ticker: {', '.join(bad)}.")
    if len(set(out)) != len(out):
        raise ValueError("A company is listed twice.")
    if len(out) > MAX_COMPANIES:
        raise ValueError(f"A research run takes at most {MAX_COMPANIES} companies.")
    return out


def request_stop(runs_dir: Path, run_id: str, note: str | None = None) -> dict:
    """A person stops a research run: every company stops before its next call; nothing is published."""
    path = Path(runs_dir) / run_id / "research" / STOP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"requested_at": now_utc(), "by": "person", "note": note}
    path.write_text(json.dumps(rec) + "\n")
    return rec


# ---------------------------------------------------------------- tools injected into the job


class ResearchTools:
    """What the loop reaches outside the process with: a fetcher (get(url) -> research_sources.Fetched), the HTML
    renderer and the ticker -> (cik, company) resolver. Tests pass fakes; the defaults are live."""

    def __init__(self, fetcher=None, render_html=None, resolve=None, brave=None, reviewer=None, adjudicator=None):
        from capture import research_reviewer as rv
        from capture.brave_search import LiveBrave
        self.fetcher = fetcher or rs.LiveFetcher()
        self.render_html = render_html or rs.chrome_render
        self.resolve = resolve or self._resolve
        self.brave = brave if brave is not None else LiveBrave()      # BRAVE-PRIMARY-001: the primary search
        # S2: the independent reviewer (Muse Spark Open) and the adjudicator (Muse Wonder); tests pass fakes
        self.reviewer = reviewer if reviewer is not None else rv.reviewer_model()
        self.adjudicator = adjudicator if adjudicator is not None else rv.adjudicator_model()

    def _resolve(self, ticker: str) -> tuple[str, str]:
        from capture.fetch_filings import resolve_cik

        class _Json:
            def __init__(self, f):
                self.f = f

            def get_json(self, url):
                got = self.f.get(url)
                return json.loads(got.body.decode("utf-8"))
        return resolve_cik(ticker, fetcher=_Json(self.fetcher))


# ---------------------------------------------------------------- the run's source manifest


class Manifest:
    """research/manifest.jsonl: append-only. Each line {seq, at, kind, ...}; kinds: source (a captured document and
    its hashes), access (an identical refetch), listing (an SEC filing list), leads (search or registry leads, not
    evidence), capture_failed, checkpoint. A checkpoint line freezes the SHA-256 of every byte before it and the
    generation number; the audit recomputes the chain."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "research" / "manifest.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

    def lines(self) -> list[dict]:
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def append(self, kind: str, **fields) -> dict:
        seq = len(self.lines()) + 1
        rec = {"seq": seq, "at": now_utc(), "kind": kind, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        return rec

    def checkpoint(self, reason: str) -> dict:
        before = self.path.read_bytes()
        gen = sum(1 for l in self.lines() if l["kind"] == "checkpoint") + 1
        sources_ = sorted({l["source_id"] for l in self.lines() if l["kind"] == "source"})
        return self.append("checkpoint", generation=gen, reason=reason,
                           digest=hashlib.sha256(before).hexdigest(), bytes_before=len(before), sources=sources_)

    def summary(self) -> dict:
        lines = self.lines()
        cps = [l for l in lines if l["kind"] == "checkpoint"]
        return {"file": "research/manifest.jsonl", "entries": len(lines),
                "sources": sum(1 for l in lines if l["kind"] == "source"),
                "generation": cps[-1]["generation"] if cps else 0,
                "checkpoint_digest": cps[-1]["digest"] if cps else None}


def verify_manifest(run_dir: Path) -> list[str]:
    """Problems with the manifest chain: every checkpoint digest must equal the SHA-256 of the bytes before it."""
    path = Path(run_dir) / "research" / "manifest.jsonl"
    if not path.exists():
        return ["The run has no source manifest."]
    raw = path.read_bytes()
    problems, offset = [], 0
    for line in raw.splitlines(keepends=True):
        rec = json.loads(line)
        if rec.get("kind") == "checkpoint" and rec.get("digest") != hashlib.sha256(raw[:offset]).hexdigest():
            problems.append(f"Manifest checkpoint {rec.get('generation')} does not match the entries before it.")
        offset += len(line)
    return problems


# ---------------------------------------------------------------- the model's view of a captured source


def page_lines(words_doc: dict) -> list[str]:
    """The page as lines of text, each with the word ids it spans: 'w12-w20: text'."""
    lines, cur, key = [], [], None
    for w in words_doc.get("words", []):
        k = (w.get("block"), w.get("line"))
        if cur and (k != key or len(cur) >= LINE_WORDS_MAX):
            lines.append(cur)
            cur = []
        cur.append(w)
        key = k
    if cur:
        lines.append(cur)
    return [f"{l[0]['id']}-{l[-1]['id']}: " + " ".join(w["text"] for w in l) for l in lines]


# ---------------------------------------------------------------- the job


def _jobs():
    from server import jobs      # imported late: server.jobs imports this module
    return jobs


class ReviewSearchFailed(Exception):
    """The reviewer's Brave search answered with an error (recorded; the review records it as a limitation)."""


class Stop(Exception):
    """A company's loop ends here. `kind`: finished | limit | spend_cap | person | failed | unpriced."""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


def input_token_bound(body: dict, history_bytes: int = 0) -> tuple[int, int]:
    """(input-token bound, search bound) reserved for one request: its bytes (plus the stored history with
    previous_response_id), the assumed search context per search, and a fixed overhead."""
    searches = 0
    if any(t.get("type") == "web_search" for t in body.get("tools", [])):
        searches = body.get("max_tool_calls") or ASSUMED_SEARCHES_PER_REQUEST
    size = rr.request_size(body) + (history_bytes if body.get("previous_response_id") else 0)
    return size + searches * SEARCH_CONTEXT_TOKENS + REQUEST_OVERHEAD_TOKENS, searches


def make_job_class():
    jobs = _jobs()

    class ResearchJob(jobs.Job):
        def __init__(self, run_dir: Path, reader, tools: ResearchTools | None = None, as_of: date | None = None):
            super().__init__(run_dir, reader, None, as_of, None, None, None)
            self.second = self.wonder = self.sam = None
            self.tools = tools or ResearchTools()
            self.limits = validate_limits(self.choices.get("research_limits"))
            self.manifest = Manifest(run_dir)
            self.state: dict[str, dict] = {}        # per company, in memory: sources, keys, call ids, history
            self.cap_reached: str | None = None     # set once the run's spend cap stops every company
            self.first_attempts: list[bool] = []    # empty? for the first five generation attempts of the run

        # ------------------------------------------------------------ ledger

        @property
        def research(self) -> dict:
            return self.meta["research"]

        def init_ledger(self) -> None:
            with jobs.RUN_LOCK:
                if isinstance(self.meta.get("prices"), dict):
                    self.meta["prices"]["per_1k_searches_usd"] = prices.search_snapshot()
                    self.meta["prices"]["per_1k_requests_usd"] = prices.request_snapshot()
                r = self.meta.setdefault("research", {})
                r.setdefault("label", LABEL)
                r["limits"] = dict(self.limits)
                r.setdefault("companies", {})
                for t in self.choices["companies"]:
                    r["companies"].setdefault(t, {
                        "status": "queued", "status_reason": "Waiting for a research slot.", "cik": None,
                        "company": None, "requests": 0, "searches": 0, "fetches": 0, "pages_delivered": 0,
                        "sources": 0, "proposals": 0, "spend_usd": 0.0, "unpriced_calls": 0,
                        "empty_answers": 0, "started_at": None, "finished_at": None, "limitation": None,
                        "capture_failures": [], "finish": None})
                for k in ("steps", "refusals", "coverage", "consolidations", "repeats", "kill_conditions"):
                    r.setdefault(k, [])
                if not isinstance(r.get("review"), dict) or "stage" in r["review"]:
                    r["review"] = {}      # per company: run.research.review[ticker] (an S1 placeholder is replaced)
                r["review_reserve"] = self.review_reserve()
                r["manifest"] = self.manifest.summary()
                r.setdefault("audit", None)
                r["reservation"] = {
                    "rule": "strict: a call is made only if spend + reservations in flight + its worst case fit "
                            "under the spending cap",
                    "assumptions": {"searches_per_request": ASSUMED_SEARCHES_PER_REQUEST,
                                    "search_context_tokens_per_search": SEARCH_CONTEXT_TOKENS,
                                    "request_overhead_tokens": REQUEST_OVERHEAD_TOKENS,
                                    "input_tokens_bound": "request bytes (plus stored history bytes with "
                                                          "previous_response_id)",
                                    "max_output_tokens": rr.MAX_OUTPUT_TOKENS},
                    "note": "These are planning assumptions for the reservation, not measured provider facts: the "
                            "cost counted is each reply's own usage and search count. A reply whose input exceeded "
                            "its reservation is listed under reservation_exceeded, and spend can then pass the cap "
                            "by that excess."}

        def company(self, ticker: str) -> dict:
            return self.research["companies"][ticker]

        def step(self, ticker: str | None, kind: str, tag: str, who: str, text: str, **refs) -> dict:
            with jobs.RUN_LOCK:
                s = {"n": len(self.research["steps"]) + 1, "at": now_utc(), "ticker": ticker, "kind": kind,
                     "tag": tag, "who": who, "text": text, **({"refs": refs} if refs else {})}
                self.research["steps"].append(s)
                return s

        def save_ledger(self) -> None:
            with jobs.RUN_LOCK:
                self.research["manifest"] = self.manifest.summary()
                self.save()

        # ------------------------------------------------------------ the gate (the run's one spend cap)

        def stop_requested(self) -> bool:
            return (self.dir / "research" / STOP_FILE).exists()

        def worst_case(self, body: dict, ticker: str) -> tuple[float | None, int, int]:
            """(worst-case dollars, input-token bound, search bound) of one request."""
            input_bound, searches = input_token_bound(body, self.state[ticker]["history_bytes"])
            worst = prices.responses_worst_case_usd(jobs.price_name(body["model"]), input_bound,
                                                    body["max_output_tokens"], searches)
            return worst, input_bound, searches

        def reserve(self, ticker: str, body: dict) -> tuple[int | None, float, int]:
            """Wait for room on the gate, or raise Stop. Returns (token, worst, input bound)."""
            worst, input_bound, _ = self.worst_case(body, ticker)
            c = self.company(ticker)
            while True:
                if self.stop_requested():
                    raise Stop("person", "Stopped by a person before the next call.")
                if self.cap_reached:
                    raise Stop("spend_cap", self.cap_reached)
                with jobs.RUN_LOCK:
                    self.refresh_spend()
                    limit = self.research_limit()
                    if jobs.unknown_unaccepted(self.job):
                        raise Stop("unpriced", "A call could not be priced, so the spending cap cannot be kept; a "
                                               "person must accept the unknown cost. Nothing more was researched.")
                    if worst is None or not prices.has_price(jobs.price_name(body["model"])):
                        raise Stop("unpriced", f"runs/prices.py has no price for {body['model']}, so the spending "
                                               f"cap cannot be kept. Nothing was researched.")
                    cap = self.limits["max_spend_per_company_usd"]
                    if cap is not None and c["spend_usd"] >= cap:
                        raise Stop("limit", f"Per-company spend limit reached ({jobs.money(c['spend_usd'])} of "
                                            f"{jobs.money(cap)}), a setting a person turned on.")
                    # Strict admission (Manus 15:04): the call is made only if spend + every reservation in
                    # flight + its own worst case fits under the cap. Page mode keeps its own rule.
                    token = jobs.reserve_budget(self.dir.name, self.meta["spend_usd"], limit, worst,
                                                alone_may_cross=False)
                    if token is not None:
                        return token, worst, input_bound
                    if not jobs._RESERVED.get(self.dir.name):
                        reason = self.cap_reason("call", worst)
                        self.cap_reached = reason
                        raise Stop("spend_cap", reason)
                time.sleep(0.05)      # another company's call is in flight: queue behind it

        def count(self, ticker: str, reply: rr.ResponsesReply, token, worst: float, input_bound: int) -> float | None:
            """Count one call on the run and on its company; release its reservation."""
            try:
                return self._count(ticker, reply, token, worst, input_bound)
            finally:          # settle exactly once on every path: the reservation is always released
                jobs.release_budget(self.dir.name, token)

        def _count(self, ticker, reply, token, worst, input_bound):
            c = self.company(ticker)
            name = jobs.price_name(reply.model)
            with jobs.RUN_LOCK:
                self.refresh_spend()
                self.meta["turn_count"] += 1
                self.meta["turns_total"] += 1
                c["requests"] += 1
                basis = "usage"
                if reply.error:
                    cost = worst if reply.may_have_been_billed else 0.0
                    basis = "worst_case_bound" if reply.may_have_been_billed else "not_billed"
                    if reply.may_have_been_billed:
                        self.job.setdefault("research_worst_case_charged", []).append(
                            {"at": now_utc(), "ticker": ticker, "usd": worst})
                else:
                    cost = prices.responses_cost_usd(name, reply.input_tokens, reply.output_tokens, reply.searches)
                if cost is None:
                    self.job["unpriced_calls"] = self.job.get("unpriced_calls", 0) + 1
                    models = self.job.setdefault("unpriced_models", [])
                    if reply.model not in models:
                        models.append(reply.model)
                    c["unpriced_calls"] += 1
                else:
                    self.meta["spend_usd"] = round((self.meta["spend_usd"] or 0) + cost, prices.ROUND_TO)
                    c["spend_usd"] = round(c["spend_usd"] + cost, prices.ROUND_TO)
                if isinstance(reply.searches, int):
                    c["searches"] += reply.searches
                over_tokens = isinstance(reply.input_tokens, int) and reply.input_tokens > input_bound
                over_dollars = cost is not None and worst is not None and cost > worst
                if over_tokens or over_dollars:      # dollars compared too: searches alone can pass the reservation
                    self.research.setdefault("reservation_exceeded", []).append(
                        {"at": now_utc(), "ticker": ticker, "input_tokens": reply.input_tokens,
                         "reserved_input_tokens": input_bound, "searches": reply.searches,
                         "cost_usd": cost, "reserved_usd": worst})
                jobs.release_budget(self.dir.name, token)
                self.meta["unpriced_calls"] = jobs.unpriced_count(self.job)
                self.save(refresh=False)
            return cost if basis != "not_billed" else 0.0

        # ------------------------------------------------------------ the review reserve (S2 wiring)

        def reviewer_problem(self) -> str | None:
            """Why the independent reviewer cannot run in this process, or None."""
            reviewer = getattr(self.tools, "reviewer", None)
            if reviewer is None:
                return "No independent reviewer is configured."
            try:
                return reviewer.ready()
            except Exception as e:
                return f"The reviewer could not be prepared ({type(e).__name__})."

        def review_reserve(self) -> dict:
            """run.research.review_reserve: the share of the cap research leaves for the review (recorded, shown)."""
            if not hasattr(self, "reserve_held"):
                self.reserve_held = self.reviewer_problem() is None
            cap = self.choices.get("spending_limit_usd")
            if cap is None:
                return {"held": False, "fraction": None, "usd": None, "cap_usd": None, "research_limit_usd": None,
                        "reason": "The run has no spending cap, so no review reserve is held."}
            if not self.reserve_held:
                return {"held": False, "fraction": 0.0, "usd": 0.0, "cap_usd": cap, "research_limit_usd": cap,
                        "reason": f"No review reserve is held: the independent reviewer cannot run "
                                  f"({self.reviewer_problem()}). Research may use the whole cap."}
            usd = round(cap * REVIEW_RESERVE_FRACTION, prices.ROUND_TO)
            return {"held": True, "fraction": REVIEW_RESERVE_FRACTION, "usd": usd, "cap_usd": cap,
                    "research_limit_usd": round(cap - usd, prices.ROUND_TO),
                    "reason": f"A code default, not a hidden limit: research is admitted only under "
                              f"{jobs.money(cap - usd)} of the {jobs.money(cap)} cap, so {jobs.money(usd)} "
                              f"({round(REVIEW_RESERVE_FRACTION * 100)}%) stays for the independent review of every "
                              f"company. The review is admitted under the whole cap."}

        def research_limit(self):
            """The cap research calls are admitted under: the run's cap less the review reserve (when held)."""
            cap = self.choices.get("spending_limit_usd")
            if cap is None or not getattr(self, "reserve_held", False):
                return cap
            return round(cap * (1 - REVIEW_RESERVE_FRACTION), prices.ROUND_TO)

        def cap_reason(self, what: str, worst: float) -> str:
            limit, spent = self.choices["spending_limit_usd"], self.meta["spend_usd"] or 0
            if getattr(self, "reserve_held", False) and limit is not None:
                share = self.research_limit()
                return (f"Spending cap reached: the research budget is reached: the next {what}'s bound "
                        f"{jobs.money(worst)} would exceed research's {jobs.money(share)} share of the "
                        f"{jobs.money(limit)} cap ({jobs.money(spent)} spent); {jobs.money(limit - share)} is held "
                        f"for the independent review. Raise the cap to research more.")
            if what == "call":
                return (f"Spending cap reached: budget reached: the next call's bound {jobs.money(worst)} would exceed "
                        f"the {jobs.money(limit)} cap ({jobs.money(spent)} spent). Raise the cap to research more.")
            return (f"Spending cap reached: budget reached: the next request's bound {jobs.money(worst)} would exceed "
                    f"the {jobs.money(limit)} cap. Raise the cap to research more.")

        # ------------------------------------------------------------ the independent review (S2 wiring)

        def review_admit(self, worst: float):
            """(token, None) or (None, reason): strict admission of one reviewer or adjudicator request under the
            WHOLE cap (the research share being used up does not refuse it: that is what the reserve is for)."""
            while True:
                if self.stop_requested():
                    return None, "A person stopped the run."
                with jobs.RUN_LOCK:
                    self.refresh_spend()
                    if jobs.unknown_unaccepted(self.job):
                        return None, ("A call could not be priced, so the spending cap cannot be kept; a person must "
                                      "accept the unknown cost.")
                    limit = self.choices["spending_limit_usd"]
                    token = jobs.reserve_budget(self.dir.name, self.meta["spend_usd"], limit, worst,
                                                alone_may_cross=False)
                    if token is not None:
                        return token, None
                    if not jobs._RESERVED.get(self.dir.name):
                        return None, (f"Spending cap reached: the next request's bound {jobs.money(worst)} would "
                                      f"exceed the {jobs.money(limit)} cap "
                                      f"({jobs.money(self.meta['spend_usd'] or 0)} spent).")
                time.sleep(0.05)      # another call is in flight: queue behind it

        def review_usage(self, ticker: str) -> dict:
            return self.company(ticker).setdefault("review_usage", {
                "model_requests": 0, "search_requests": 0, "fetches": 0, "spend_usd": 0.0, "unpriced_calls": 0,
                "worst_case_charged_usd": 0.0})

        def review_gate(self, ticker: str):
            """The spend_gate capture/research_reviewer.py takes, on the run's one gate: reserve the request's bounded
            worst case before it is made; settle exactly once after, at its own usage (unknown usage is unpriced, never
            zero; a possibly-billed failure is charged its worst case)."""
            from capture.brave_search import NAME as BRAVE

            def reserve(req: dict):
                kind = req.get("kind")
                if kind == "search":
                    brave = self.tools.brave
                    problem = "Brave Search is not configured." if brave is None else brave.ready()
                    if problem:
                        return {"admitted": False, "reason": f"{problem} The reviewer's independent search (Brave "
                                                             f"Search) is unavailable."}
                    name, bound = BRAVE, None
                    worst = prices.request_cost_usd(BRAVE, req.get("searches") or 1)
                else:
                    name = jobs.price_name(str(req.get("model") or ""))
                    bound = int(req.get("request_bytes") or 0) + REQUEST_OVERHEAD_TOKENS
                    out = req.get("max_output_tokens")
                    worst = prices.cost_usd(name, bound, out) if isinstance(out, int) else None
                if worst is None:
                    return {"admitted": False, "reason": f"runs/prices.py has no price for {name}, so the spending cap "
                                                         f"cannot be kept."}
                token, why = self.review_admit(worst)
                if token is None:
                    return {"admitted": False, "reason": why}
                return {"admitted": True, "token": token, "worst": worst, "name": name, "kind": kind,
                        "input_bound": bound, "stage": req.get("stage")}

            def settle(req: dict) -> None:
                t = req.get("ticket") or {}
                usage = req.get("usage") or {}
                try:
                    with jobs.RUN_LOCK:
                        self.refresh_spend()
                        err, billed = req.get("error"), req.get("may_have_been_billed")
                        if err:
                            cost = t["worst"] if billed else 0.0
                        elif t.get("kind") == "search":
                            cost = prices.request_cost_usd(t["name"], usage.get("searches"))
                        else:
                            cost = prices.cost_usd(t["name"], usage.get("prompt_tokens"), usage.get("completion_tokens"))
                        u = self.review_usage(ticker)
                        u["search_requests" if t.get("kind") == "search" else "model_requests"] += 1
                        if err and billed:
                            u["worst_case_charged_usd"] = round(u["worst_case_charged_usd"] + t["worst"],
                                                                prices.ROUND_TO)
                            self.job.setdefault("research_worst_case_charged", []).append(
                                {"at": now_utc(), "ticker": ticker, "usd": t["worst"], "stage": t.get("stage")})
                        if cost is None:
                            self.job["unpriced_calls"] = self.job.get("unpriced_calls", 0) + 1
                            models = self.job.setdefault("unpriced_models", [])
                            if t.get("name") not in models:
                                models.append(t.get("name"))
                            u["unpriced_calls"] += 1
                        else:
                            self.meta["spend_usd"] = round((self.meta["spend_usd"] or 0) + cost, prices.ROUND_TO)
                            u["spend_usd"] = round(u["spend_usd"] + cost, prices.ROUND_TO)
                        p = usage.get("prompt_tokens")
                        if (isinstance(p, int) and t.get("input_bound") is not None and p > t["input_bound"]) or \
                                (cost is not None and cost > t["worst"]):
                            self.research.setdefault("reservation_exceeded", []).append(
                                {"at": now_utc(), "ticker": ticker, "stage": t.get("stage"), "input_tokens": p,
                                 "reserved_input_tokens": t.get("input_bound"), "cost_usd": cost,
                                 "reserved_usd": t["worst"]})
                        self.meta["unpriced_calls"] = jobs.unpriced_count(self.job)
                        self.save(refresh=False)
                finally:      # exactly once on every path: the reservation is always released
                    jobs.release_budget(self.dir.name, t.get("token"))

            def gate(req: dict):
                if req.get("phase") == "reserve":
                    return reserve(req)
                if req.get("phase") == "settle":
                    return settle(req)
                raise ValueError("Unknown spend gate phase.")
            return gate

        def review_search(self, ticker: str):
            """The reviewer's independent search: Brave Search (BRAVE-PRIMARY-001). Leads only; the payload goes to the
            reviewer for its turn in memory and is never stored (the manifest keeps the query and URLs)."""
            from capture.brave_search import NAME

            def search(query: str):
                got = self.tools.brave.search(query, 10)
                with jobs.RUN_LOCK:
                    self.manifest.append("leads", ticker=ticker, tool="brave_search", engine=NAME, stage=REVIEW_STAGE,
                                         query=query, urls=got.urls, error=got.error, http_status=got.http_status)
                if got.error:
                    self.step(ticker, "search", "Code", "capture.brave_search",
                              f"Reviewer search with Brave Search failed for “{query}”: {got.error} Not retried; "
                              f"recorded as a review limitation.", stage=REVIEW_STAGE, engine=NAME, queries=[query],
                              results=[])
                    raise ReviewSearchFailed(got.error)
                self.step(ticker, "search", "Code", "capture.brave_search",
                          f"Reviewer searched with Brave Search: {query}. {len(got.urls)} result"
                          f"{'s' if len(got.urls) != 1 else ''} (leads, not evidence).",
                          stage=REVIEW_STAGE, engine=NAME, queries=[query], results=got.urls[:20])
                return {"results": got.results, "searches": 1, "status": "completed"}
            return search

        def review_fetch(self, ticker: str, fetched: list):
            """The reviewer's fetch: the S1 capture path (check_url, the fetcher, register_source: pages, hashes and a
            manifest line), so it can cite only a captured source."""
            def fetch(url: str) -> dict:
                st = self.ensure_state(ticker)
                st["review_fetch_n"] = st.get("review_fetch_n", 0) + 1
                cid = f"review-{ticker.lower()}-f{st['review_fetch_n']}"
                try:
                    rs.check_url(url, getattr(self.tools.fetcher, "resolve", None))
                    got = self.tools.fetcher.get(url)
                except rs.FetchRefused as e:
                    self.step(ticker, "fetch", "Code", "capture.research_sources",
                              f"Refused to fetch {url} for the reviewer: {e}", call_id=cid, stage=REVIEW_STAGE)
                    raise
                except rs.CaptureFailed as e:
                    self._capture_failed(ticker, cid, url, str(e), "review_fetch")
                    raise
                try:
                    source = self.register_source(ticker, cid, got, "review_fetch")
                except rs.CaptureFailed as e:
                    self._capture_failed(ticker, cid, url, str(e), "review_fetch", got)
                    raise
                with jobs.RUN_LOCK:
                    self.review_usage(ticker)["fetches"] += 1
                    if source["retrieval"].get("call_id") == cid:
                        source["retrieval"]["stage"] = REVIEW_STAGE
                    self.save_ledger()
                view = self.review_source_view(source)
                fetched.append(view)
                return view
            return fetch

        def review_source_view(self, source: dict) -> dict:
            """A captured source as the reviewer gets it: its identity and its captured text, page by page."""
            pages = []
            for p in source["pages"]:
                words, _ = sources.load_page_evidence(self.dir, p)
                pages.append({"page_number": p["page_number"],
                              "text": " ".join(w["text"] for w in words.get("words", []))})
            return {"source_id": source["id"], "url": source["url"], "title": source.get("title"),
                    "publisher": source.get("publisher"), "kind": source.get("kind"),
                    "published": source.get("published_date"), "retrieved_at": source.get("captured_at"),
                    "pages": pages, "text": "\n".join(pg["text"] for pg in pages)}

        @staticmethod
        def review_proposal(row: dict) -> dict:
            """A draft row as the reviewer gets it, bound by row id and revision (no researcher reasoning)."""
            r = row["research"]
            return {"row_id": row["id"], "revision_id": r["revision"], "title": row.get("title"),
                    "program": row.get("program"), "indication": row.get("indication"),
                    "date_text": row.get("date_text"), "date": row.get("date"), "date_end": row.get("date_end"),
                    "conditions": row.get("conditions"), "conditions_state": r.get("conditions_state"),
                    "quote": row.get("quote"), "status": row.get("status"), "action": r.get("action"),
                    "placement_proposal": r.get("placement_proposal"),
                    "evidence": [{"source_id": s["source_id"], "page_number": s["page_number"], "quote": s["quote"],
                                  "roles": s.get("roles")} for s in r.get("evidence") or []],
                    "code_checks": [{"name": c.get("name"), "result": c.get("result")} for c in row.get("checks") or []],
                    "issues": [i.get("code") for i in r.get("issues") or []]}

        def ensure_state(self, ticker: str) -> dict:
            """The company's in-memory state; rebuilt from run.json when research never set it up (the identity could
            not be resolved) or when an existing run is reviewed without researching."""
            st = self.state.get(ticker)
            if st is not None:
                return st
            c = self.company(ticker)
            srcs = {s["id"]: s for s in self.run["sources"] if (s.get("retrieval") or {}).get("ticker") == ticker}
            st = self.state[ticker] = {
                "cik": c.get("cik"), "company": c.get("company") or ticker, "sources": srcs, "keys": {}, "calls": {},
                "actions": {}, "leads": {}, "history_bytes": 0, "finish_attempts": 0, "delivered": {}, "settled": {},
                "compacted": set(), "turn_starts": [], "brave_calls": {}, "brave_items": [],
                "fetch_by_url": {(s["url"], s["retrieval"].get("original_sha256")): s for s in srcs.values()}}
            return st

        @staticmethod
        def review_disputes(ticker: str, record: dict, rows_by_id: dict) -> list[dict]:
            """A finding that contests a row's reading (every kind except omission, which names a missing event) is a
            dispute between the researcher's row and the reviewer's finding: Muse Wonder gets both readings and the
            captured evidence of both."""
            from capture import research_reviewer as rv
            out = []
            for f in record.get("findings") or []:
                if f["kind"] == "omission" or not f["row_revision_ids"]:
                    continue
                readings, evidence = [], [{"source_id": e["source_id"], "quote": e["quote"]} for e in f["evidence"]]
                for ref in f["row_revision_ids"]:
                    row = rows_by_id.get(ref["row_id"])
                    if row is None:
                        continue
                    readings.append(f"{row['id']} revision {ref['revision_id']}: {row.get('title')}; timing "
                                    f"{row.get('date_text') or 'undated'}; conditions "
                                    f"{json.dumps(row.get('conditions'))}; quote: {row.get('quote')}")
                    evidence += [{"source_id": s["source_id"], "quote": s["quote"]}
                                 for s in row["research"].get("evidence") or []]
                out.append({"dispute_id": f"{ticker}-{f['finding_id']}", "finding_id": f["finding_id"],
                            "row_revision_ids": f["row_revision_ids"], "component": f["kind"],
                            "readings": [{"by": "Muse Spark (the researcher's draft)", "text": " | ".join(readings)},
                                         {"by": f"{rv.REVIEWER_NAME} (the independent reviewer)", "text": f["text"]}],
                            "evidence": evidence})
            return out

        @staticmethod
        def company_review_label(status: str | None, clean: bool, has_findings: bool) -> str:
            """'independently reviewed ...' only for a COMPLETE review; findings from a review that did not finish
            read 'partly reviewed (incomplete), findings so far'; nothing else claims a review."""
            if status == "complete":
                if clean:
                    return LABEL_REVIEWED_CLEAN
                if has_findings:
                    return LABEL_REVIEWED_FINDINGS
                return LABEL
            return LABEL_PARTIAL_FINDINGS if has_findings else LABEL

        def run_review_label(self) -> str:
            """The run's label: reviewed (no findings) only when every company's review is complete and clean; with
            findings only when every company's review is complete; partly reviewed when findings exist but some review
            did not complete; otherwise not independently reviewed."""
            reviews = self.research.get("review") or {}
            entries = [reviews.get(t) for t in self.research["companies"]]
            if not entries or any(not isinstance(e, dict) for e in entries):
                return LABEL_PARTIAL_FINDINGS if any(isinstance(e, dict) and e.get("label") in
                                                     (LABEL_REVIEWED_FINDINGS, LABEL_PARTIAL_FINDINGS)
                                                     for e in entries) else LABEL
            if all(e.get("label") == LABEL_REVIEWED_CLEAN for e in entries):
                return LABEL_REVIEWED_CLEAN
            if all(e.get("status") == "complete" for e in entries) and \
                    any(e.get("label") == LABEL_REVIEWED_FINDINGS for e in entries):
                return LABEL_REVIEWED_FINDINGS
            if any(e.get("label") in (LABEL_REVIEWED_FINDINGS, LABEL_PARTIAL_FINDINGS) for e in entries):
                return LABEL_PARTIAL_FINDINGS
            return LABEL

        def review_company(self, ticker: str, ended_kind: str, ended_reason: str) -> dict:
            """After a company's research ends (for any reason but a person's stop), the independent reviewer attacks
            its draft; disputed findings go to Muse Wonder (fail-closed); the record, the findings on their rows, the
            labels and the trail are saved. Returns run.research.review[ticker]."""
            from capture import research_reviewer as rv
            started = now_utc()
            ended = {"kind": ended_kind, "reason": ended_reason}
            rows = [r for r in self.run["rows"] if (r.get("research") or {}).get("ticker") == ticker]
            rows_by_id = {r["id"]: r for r in rows}
            if self.stop_requested():
                record = {"stage": REVIEW_STAGE, "status": "not_run", "clean": False, "findings": [], "rejected": [],
                          "limitations": [], "reason": "A person stopped the run, so the independent review was not "
                                                       "run; nothing was reviewed."}
                adj = {"stage": "adjudication", "status": "not_run", "reason": "The review did not run.",
                       "outcomes": [], "skipped": [], "usage": None}
                return self.save_review(ticker, rows, record, adj, ended, started, [])
            st = self.ensure_state(ticker)
            given, not_given = [], []
            for s in st["sources"].values():
                try:
                    given.append(self.review_source_view(s))
                except Exception as e:
                    not_given.append({"source_id": s["id"], "reason": f"Its captured text could not be loaded "
                                                                      f"({type(e).__name__}), so it was not given."})
            as_of = run_reference_date(self.run) or date.today()
            company = {"ticker": ticker, "name": st["company"], "cik": st["cik"], "as_of": as_of.isoformat(),
                       "calendar_window": [as_of.isoformat(), jobs.add_months(as_of, 6).isoformat()]}
            self.step(ticker, "review", "Code", "runs.research",
                      f"Independent review of {ticker} started after its research ended ({ended_kind}): "
                      f"{len(rows)} proposal{'s' if len(rows) != 1 else ''} and {len(given)} captured source"
                      f"{'s' if len(given) != 1 else ''} sent to {rv.REVIEWER_NAME}; its own search is Brave Search.",
                      stage=REVIEW_STAGE)
            fetched: list[dict] = []
            try:
                record = rv.review_draft(company=company, assignment=self.choices.get("note_for_agent") or "",
                                         proposals=[self.review_proposal(r) for r in rows], sources=given,
                                         search=self.review_search(ticker), fetch_capture=self.review_fetch(ticker, fetched),
                                         spend_gate=self.review_gate(ticker), model=self.tools.reviewer)
            except Exception as e:      # the type only: nothing that could carry request details
                record = {"stage": REVIEW_STAGE, "status": "incomplete", "clean": False, "findings": [],
                          "rejected": [], "limitations": [],
                          "reason": f"The review stopped on an unexpected error ({type(e).__name__})."}
            if not_given:
                record["sources_not_given"] = not_given
                record["clean"] = False
            disputes = self.review_disputes(ticker, record, rows_by_id)
            if disputes:
                self.step(ticker, "review", "Code", "runs.research",
                          f"{len(disputes)} finding{'s' if len(disputes) != 1 else ''} dispute{'' if len(disputes) != 1 else 's'} "
                          f"a row's reading: sent to {rv.ADJUDICATOR_NAME}, one request each, fail-closed.",
                          stage="adjudication")
                try:
                    adj = rv.adjudicate(disputes=disputes, sources=given + fetched, spend_gate=self.review_gate(ticker),
                                        model=self.tools.adjudicator)
                except Exception as e:
                    adj = {"stage": "adjudication", "status": "incomplete",
                           "reason": f"Adjudication stopped on an unexpected error ({type(e).__name__}); every dispute "
                                     f"stays unresolved.",
                           "outcomes": [{"dispute_id": d["dispute_id"], "row_revision_ids": d["row_revision_ids"],
                                         "component": d["component"], "outcome": rv.UNRESOLVED,
                                         "reason": "Not adjudicated.", "reading": None, "resolution": None,
                                         "evidence": [], "model": None, "human_accepted": False} for d in disputes],
                           "skipped": [], "usage": None}
            else:
                adj = {"stage": "adjudication", "status": "not_needed",
                       "reason": "No finding disputes a row's reading, so nothing was sent to Muse Wonder.",
                       "outcomes": [], "skipped": [], "usage": None}
            return self.save_review(ticker, rows, record, adj, ended, started, disputes)

        @staticmethod
        def finding_basis(row: dict) -> str:
            """The basis a finding was made against: the row's revision and its whole evidence set."""
            r = row["research"]
            spans = [[e.get("source_id"), e.get("page_number"), e.get("quote")] for e in r.get("evidence") or []]
            return hashlib.sha256(json.dumps([r.get("revision"), spans], sort_keys=True).encode()).hexdigest()

        def save_review(self, ticker, rows, record, adj, ended, started, disputes) -> dict:
            """Persist one review attempt. A COMPLETE review replaces the rows' findings. An attempt that did not
            complete (incomplete, not run) keeps every earlier finding: one whose row revision and evidence are
            unchanged stays current; one whose basis changed is marked stale. The attempt's own findings are added."""
            from capture import research_reviewer as rv
            at = now_utc()
            rows_by_id = {r["id"]: r for r in rows}
            outcomes = {o.get("dispute_id"): o for o in adj.get("outcomes") or []}
            reviewed = {(str(x["row_id"]), str(x["revision_id"]))
                        for x in ((record.get("reviewed") or {}).get("rows") or [])}
            status = record.get("status")
            prior = (self.research.get("review") or {}).get(ticker)
            prior = prior if isinstance(prior, dict) and "stage" in prior else None
            attempt = (prior.get("attempt") or 1) + 1 if prior else 1
            with jobs.RUN_LOCK:
                for row in rows:
                    old = row["research"].get("review_findings") or []
                    kept = []
                    if status != "complete":
                        basis = self.finding_basis(row)
                        for f in old:
                            f = dict(f)
                            if f.get("basis") == basis and str(f.get("revision_id")) == str(row["research"]["revision"]):
                                f["current"] = True
                            else:
                                f["current"], f["stale"] = False, True
                                f["stale_reason"] = ("The row's revision or evidence changed after this finding was "
                                                     "made.")
                            f["latest_attempt"] = {"attempt": attempt, "status": status, "at": at}
                            kept.append(f)
                    row["research"]["review_findings"] = kept
                for f in record.get("findings") or []:
                    o = outcomes.get(f"{ticker}-{f['finding_id']}")
                    if o is not None:
                        adjudication = {k: o.get(k) for k in ("outcome", "reason", "reading", "resolution", "evidence",
                                                               "model")}
                        adjudication["human_accepted"] = False
                    else:
                        adjudication = {"outcome": "not_sent", "human_accepted": False,
                                        "reason": ("An omission names a missing event; it does not dispute a row's "
                                                   "reading." if f["kind"] == "omission" else "Not sent.")}
                    for ref in f["row_revision_ids"]:
                        row = rows_by_id.get(ref["row_id"])
                        if row is None:
                            continue
                        row["research"]["review_findings"].append({
                            "finding_id": f["finding_id"], "attempt": attempt, "kind": f["kind"], "text": f["text"],
                            "by": f.get("by"), "row_id": row["id"], "revision_id": ref["revision_id"],
                            "current": str(ref["revision_id"]) == str(row["research"]["revision"]),
                            "basis": self.finding_basis(row),
                            "evidence": f["evidence"], "review_status": status,
                            "reviewed_at": at, "adjudication": adjudication})
                company_has = False
                verdicts = {(str(a["row_id"]), str(a["revision_id"])): a for a in record.get("assessments") or []}
                for row in rows:
                    rev = row["research"]["revision"]
                    # reviewed_revision: the revision a COMPLETED review covered; null when the review did not finish
                    reviewed_this = status == "complete" and (row["id"], str(rev)) in reviewed
                    prev = row["research"].get("review") or {}
                    a = verdicts.get((row["id"], str(rev)))
                    row["research"]["review"] = {
                        "status": status, "attempt": attempt, "at": at,
                        "verdict": a["verdict"] if a else None,
                        "reviewed_revision": rev if reviewed_this else (
                            prev.get("reviewed_revision") if status != "complete" and
                            prev.get("reviewed_revision") == rev else None)}
                    current = [f for f in row["research"]["review_findings"] if f.get("current")]
                    company_has = company_has or bool(current)
                    # CLAIM-001 (Red Team PR177_8cd269a): "independently reviewed" only when the LATEST attempt is
                    # complete; findings carried through an attempt that did not complete read "partly reviewed"
                    if status == "complete" and current:
                        row["research"]["label"] = LABEL_REVIEWED_FINDINGS
                    elif current:
                        row["research"]["label"] = LABEL_PARTIAL_FINDINGS
                    elif status == "complete" and record.get("clean") and reviewed_this:
                        row["research"]["label"] = LABEL_REVIEWED_CLEAN
                    else:
                        row["research"]["label"] = LABEL
                omissions = [f for f in record.get("findings") or [] if not f["row_revision_ids"]]
                carried_omissions = [] if status == "complete" or not prior else [
                    o for o in prior.get("unbound_findings") or []]
                unbound = carried_omissions + [{**f, "attempt": attempt, "review_status": status} for f in omissions]
                label = self.company_review_label(status, bool(record.get("clean")), company_has or bool(unbound))
                attempts = (prior.get("attempts") or []) + [{k: prior.get(k) for k in (
                    "attempt", "status", "reason", "label", "started_at", "finished_at")}] if prior else []
                entry = {"stage": REVIEW_STAGE, "ticker": ticker, "attempt": attempt, "status": status,
                         "reason": record.get("reason"), "clean": bool(record.get("clean")), "label": label,
                         "reviewer": rv.REVIEWER_NAME, "adjudicator": rv.ADJUDICATOR_NAME, "search": "Brave Search",
                         "families": {
                             "researcher": {"model": getattr(self.reader, "model", None),
                                            "provider": RESEARCHER_PROVIDER, "family": RESEARCHER_FAMILY},
                             "reviewer": {"model": record.get("model") or getattr(self.tools.reviewer, "model", None),
                                          "provider": rv.REVIEWER_PROVIDER, "family": rv.REVIEWER_FAMILY},
                             "same_family": RESEARCHER_FAMILY == rv.REVIEWER_FAMILY},
                         "research_ended": ended, "started_at": started, "finished_at": at,
                         "reviewed_basis": sorted([list(k) for k in reviewed]),
                         "disputes": [{"dispute_id": d["dispute_id"], "finding_id": d["finding_id"],
                                       "row_revision_ids": d["row_revision_ids"], "component": d["component"]}
                                      for d in disputes],
                         "unbound_findings": unbound,
                         "review": record, "adjudication": adj, "usage": dict(self.review_usage(ticker)),
                         "attempts": attempts}
                self.research.setdefault("review", {})[ticker] = entry
                self.save_ledger()
            for f in record.get("findings") or []:
                o = outcomes.get(f"{ticker}-{f['finding_id']}")
                rows_txt = ", ".join(f"{x['row_id']} rev {x['revision_id']}" for x in f["row_revision_ids"]) or "no row"
                self.step(ticker, "finding", "Model", rv.REVIEWER_NAME,
                          f"Finding {f['finding_id']} ({f['kind'].replace('_', ' ')}) on {rows_txt}: {f['text'][:600]}"
                          + (f" — {rv.ADJUDICATOR_NAME}: {o['outcome'].replace('_', ' ')}"
                             + (f" ({o['reason'][:200]})" if o.get("reason") else "") + "; never a human acceptance."
                             if o else ""),
                          stage=REVIEW_STAGE, finding_id=f["finding_id"], finding_kind=f["kind"],
                          rows=[x["row_id"] for x in f["row_revision_ids"]])
            lims = record.get("limitations") or []
            self.step(ticker, "review", "Code", "runs.research",
                      f"Independent review of {ticker}: {record.get('status')}. {record.get('reason')}"
                      + (f" {len(lims)} review limitation{'s' if len(lims) != 1 else ''} recorded." if lims else "")
                      + (f" {len(record.get('rejected') or [])} finding attempt(s) rejected by code."
                         if record.get("rejected") else "")
                      + (f" Rows assessed: {len(record.get('assessments') or [])} of {len(rows)}."
                         + (f" {len(record.get('order_refusals') or [])} search/fetch request(s) refused until rows "
                            f"were assessed or a fetched source was settled (A-TO-Z-001)."
                            if record.get("order_refusals") else "") if "assessments" in record else "")
                      + f" Label: {label}.", stage=REVIEW_STAGE, status=record.get("status"), label=label)
            return entry

        def finish_review_labels(self) -> None:
            with jobs.RUN_LOCK:
                self.research["label"] = self.run_review_label()

        def review_only(self, tickers: list[str] | None = None, note: str | None = None) -> None:
            """Review an EXISTING run's saved draft without researching again (tools/research_smoke.py --review)."""
            self.init_ledger()
            todo = tickers or list(self.research["companies"])
            with jobs.RUN_LOCK:
                self.research["review_only"] = {"at": now_utc(), "companies": todo, "note": note}
            for t in todo:
                c = self.company(t)
                self.review_company(t, "review_only", f"Reviewing the saved draft; research ended earlier: "
                                                      f"{c.get('status')} — {c.get('status_reason')}")
            self.finish_review_labels()
            from runs import research_audit
            with jobs.RUN_LOCK:
                self.manifest.checkpoint("review only end")
                self.research["audit"] = research_audit.draft_audit(self.dir, self.run, [])
                self.save_ledger()


        # ------------------------------------------------------------ drive

        def drive(self) -> None:
            self.init_ledger()
            self.meta["status"], self.meta["status_reason"] = "running", "Researching."
            self.meta["turn_limit"] = RESEARCH_TURN_LIMIT
            self.save_ledger()
            self.step(None, "review", "Code", "runs.research", "Review reserve: " + self.research["review_reserve"]["reason"],
                      stage=REVIEW_STAGE, **{k: v for k, v in self.research["review_reserve"].items() if k != "reason"})
            problem = self.reader.ready()
            if problem:
                return self.stop("needs_input", problem)
            tickers = list(self.choices["companies"])
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.limits["concurrency"]) as pool:
                list(pool.map(self.run_company, tickers))      # every company is queued; none is dropped
            self.finish_review_labels()
            with jobs.RUN_LOCK:
                self.manifest.checkpoint("run end")
            from runs import research_audit
            with jobs.RUN_LOCK:
                self.research["audit"] = research_audit.draft_audit(self.dir, self.run, [])
            companies = self.research["companies"]
            unfinished = {t: c for t, c in companies.items() if c["status"] != "finished"}
            rows = sum(1 for r in self.run["rows"] if r.get("research"))
            if not unfinished and not jobs.unknown_unaccepted(self.job):
                self.meta["finished_at"] = now_utc()
                self.meta["status"] = "done"
                self.meta["status_reason"] = (f"Researched {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}: "
                                              f"{rows} proposal{'s' if rows != 1 else ''}, "
                                              f"{len(self.run['sources'])} sources captured. "
                                              f"{self.research['label']}.")
                self.save_ledger()
                return
            parts = [f"{t}: {c['status_reason']}" for t, c in unfinished.items()]
            status = "needs_input"
            self.research["manifest"] = self.manifest.summary()
            self.stop(status, f"Partial research, not published ({len(companies) - len(unfinished)} of "
                              f"{len(companies)} companies finished). " + " ".join(parts))

        def run_company(self, ticker: str) -> None:
            c = self.company(ticker)
            with jobs.RUN_LOCK:
                c.update(status="researching", status_reason="Researching.", started_at=now_utc())
                self.save_ledger()
            self.step(ticker, "start", "Code", "runs.research", f"Started researching {ticker}.")
            try:
                self.research_company(ticker)
                kind, reason = "finished", c.get("status_reason") or "The model finished."
            except Stop as s:
                kind, reason = s.kind, s.reason
            except Exception as e:  # the type only: nothing that could carry request details
                kind, reason = "failed", f"Technical failure: an unexpected error ({type(e).__name__})."
            with jobs.RUN_LOCK:
                c["status"] = {"finished": "finished", "failed": "failed"}.get(kind, "stopped")
                c["status_reason"] = reason
                c["finished_at"] = now_utc()
                if kind != "finished":
                    c["limitation"] = {"kind": kind, "reason": reason, "at": now_utc(),
                                       "not_checked": "Research after this point was not done for this company."}
                self.manifest.checkpoint(f"{ticker} {c['status']}")
                self.save_ledger()
            self.step(ticker, "limit" if kind not in ("finished",) else "finish", "Code", "runs.research",
                      f"{ticker}: {reason}")
            try:      # S2: the independent review runs after research ends, for any reason but a person's stop
                self.review_company(ticker, kind, reason)
            except Exception as e:  # the type only
                self.step(ticker, "review", "Code", "runs.research",
                          f"The independent review of {ticker} could not be recorded ({type(e).__name__}).",
                          stage=REVIEW_STAGE)

        # ------------------------------------------------------------ one company's loop

        def research_company(self, ticker: str) -> None:
            c = self.company(ticker)
            try:
                cik, company = self.tools.resolve(ticker)
            except Exception as e:
                raise Stop("failed", f"Technical failure: the company's SEC identity could not be resolved "
                                     f"({type(e).__name__}).")
            as_of = run_reference_date(self.run) or date.today()
            window_end = _jobs().add_months(as_of, 6)
            with jobs.RUN_LOCK:
                c.update(cik=str(int(cik)), company=company)
            st = self.state[ticker] = {"cik": str(int(cik)), "company": company, "sources": {}, "keys": {},
                                       "calls": {}, "actions": {}, "leads": {}, "history_bytes": 0,
                                       "fetch_by_url": {}, "finish_attempts": 0, "as_of": as_of,
                                       "delivered": {}, "settled": {}, "compacted": set(), "turn_starts": [],
                                       "brave_calls": {}, "brave_items": [],
                                       "window_end": window_end, "started": time.monotonic()}
            assignment = self.choices.get("note_for_agent") or ""
            card = {"ticker": ticker, "company": company, "cik": st["cik"], "as_of": as_of.isoformat(),
                    "calendar_window": [as_of.isoformat(), window_end.isoformat()],
                    "search_tools": ("brave_search (primary), web_search (secondary)" if
                                     (self.tools.brave is not None and self.tools.brave.ready() is None)
                                     else "brave_search is unavailable in this run; use web_search"),
                    "note": "Research events upcoming in the window; include relevant recent history as context."}
            first = [{"role": "user", "content": "Research assignment (from the person, exactly as written):\n"
                                                 + assignment},
                     {"role": "user", "content": "Company card:\n" + json.dumps(card)}]
            continuation = self.choices.get("research_continuation") or rr.CONTINUE_REPLAY
            history, pending, prev_id = list(first), list(first), None
            prose_nudged, empty_streak, timeout_streak = False, 0, 0
            brave_problem = self.tools.brave.ready() if self.tools.brave is not None else "Brave Search is not configured."
            st["brave"] = brave_problem is None
            if brave_problem:
                with jobs.RUN_LOCK:
                    self.company(ticker)["search_limitations"] = [{"engine": "Brave Search", "at": now_utc(),
                                                                   "reason": brave_problem}]
                self.step(ticker, "limit", "Code", "runs.research",
                          f"{brave_problem} Research continues on Meta's web_search.", engine="Brave Search")
            functions = rc.function_tools(brave=st["brave"])
            while True:
                self.retire_brave(ticker, c["requests"])       # every path: Brave payloads are delivered only once
                self.check_company_limits(ticker)
                searches_left = self.searches_left(ticker)
                items = (pending if (continuation == rr.CONTINUE_PREVIOUS and prev_id)
                         else self.replay_input(ticker, history, len(first)))
                body = rr.build_request(instructions=rc.CONTROLLER, input_items=items, functions=functions,
                                        searches_left=searches_left, continuation=continuation,
                                        previous_response_id=prev_id)
                if self.limits["max_searches_per_request"] is None and "max_tool_calls" in body and \
                        searches_left >= 10 ** 9:
                    body.pop("max_tool_calls")      # no search limit is on: none is sent
                token, worst, input_bound = self.reserve(ticker, body)
                self.step(ticker, "request", "Code", "runs.research",
                          f"Sent research turn {c['requests'] + 1} for {ticker} to Muse Spark "
                          f"({len(items)} input items, {rr.request_size(body)} bytes).")
                try:
                    reply = self.reader.respond(body)
                except BaseException:
                    jobs.release_budget(self.dir.name, token)     # never strand a reservation on the gate
                    raise
                cost = self.count(ticker, reply, token, worst, input_bound)
                self.save_turn(ticker, c["requests"], body, reply, cost)
                self.step(ticker, "usage", "Code", "runs.research",
                          f"Turn {c['requests']}: {rr.request_size(body)} bytes sent; input tokens "
                          f"{reply.input_tokens}, output tokens {reply.output_tokens} (reasoning "
                          f"{reply.reasoning_tokens}), searches {reply.searches}, cost "
                          f"{'unknown' if cost is None else jobs.money(cost)}.",
                          turn=c["requests"], input_bytes=rr.request_size(body), input_tokens=reply.input_tokens,
                          output_tokens=reply.output_tokens, reasoning_tokens=reply.reasoning_tokens,
                          searches=reply.searches, cost_usd=cost)
                if reply.timed_out:
                    # LIMITS-003 (CVA): a timeout no longer fails the company. The call is counted as possibly billed
                    # at its reserved worst case (count above); the next turn is a fresh, admitted request with the
                    # same replayed history (no resend of the same request). Two timeouts in a row stop the company
                    # as a recorded technical failure, like the approved two-empty-replies safety stop.
                    timeout_streak += 1
                    self.step(ticker, "timeout", "Code", "runs.research",
                              f"{reply.error} Counted as possibly billed at its reserved worst case "
                              f"({jobs.money(worst)}); the next turn is a new request "
                              f"({timeout_streak} in a row).", worst_case_usd=worst)
                    if timeout_streak >= 2:
                        raise Stop("failed", "Technical failure: the model did not answer twice in a row "
                                             f"(read timeout {rr.RESEARCH_READ_TIMEOUT_S}s each).")
                    continue
                timeout_streak = 0
                if reply.error:
                    self.step(ticker, "error", "Code", "runs.research", f"{reply.error}"
                              + (" It may have been billed; its reserved worst case was charged."
                                 if reply.may_have_been_billed else ""))
                    raise Stop("failed", f"Technical failure: {reply.error} It was not retried.")
                if reply.status == "incomplete":
                    # Manus 15:55 and 18:12: a length/incomplete termination is classified FIRST, before the empty
                    # branch, replay, search recording and tool dispatch. NONE of its tool calls run, no finish_draft
                    # is accepted, none of its text, reasoning or arguments is replayed; it is kept only in the turn
                    # log for audit and was settled above. Reasoning-only length cuts land here too, never in the
                    # empty-answer branch. The company stops incomplete; nothing is retried.
                    why = reply.incomplete_reason or "no reason given"
                    self.note_attempt(True)
                    held = reply.function_calls()
                    with jobs.RUN_LOCK:
                        for call in held:
                            self.research["refusals"].append({
                                "at": now_utc(), "ticker": ticker, "call_id": call.get("call_id"),
                                "tool": call.get("name"), "issues": [rc.issue(
                                    "incomplete_answer", f"{call.get('name')} came in an answer the provider marked "
                                                         f"incomplete ({why}); not executed.")]})
                    self.step(ticker, "error", "Model", "Muse Spark",
                              f"The answer was cut off (status incomplete: {why}); none of its "
                              f"{len(held)} tool call(s) ran and nothing from it was accepted or replayed. "
                              f"Kept in the turn log for audit.")
                    self.save_ledger()
                    raise Stop("incomplete", f"Incomplete: the model's answer was cut off (status incomplete: {why}); "
                                             f"the proposals recorded so far are kept, the company is not finished. "
                                             f"It was not retried.")
                self.note_attempt(reply.empty)
                for s in reply.web_searches():
                    self.record_search(ticker, s)
                st["history_bytes"] += rr.request_size({"o": reply.output})
                if reply.empty:
                    empty_streak += 1
                    with jobs.RUN_LOCK:
                        c["empty_answers"] += 1
                    why = ("it stopped at its output budget while reasoning (finish_reason length)"
                           if reply.length_cut else "it had no tool call and no text")
                    self.step(ticker, "error", "Model", "Muse Spark", f"Empty answer: {why}.")
                    if empty_streak >= 2:
                        raise Stop("failed", "Technical failure: two empty answers in a row "
                                             f"(the last: {why}).")
                    nudge = {"role": "user", "content": NUDGE_EMPTY}
                    st["turn_starts"].append(len(history))
                    history += list(reply.output) + [nudge]
                    pending, prev_id = [nudge], reply.response_id
                    continue
                empty_streak = 0
                calls = reply.function_calls()
                st["turn_starts"].append(len(history))
                history += list(reply.output)
                if not calls:
                    text = reply.text()
                    if text:
                        self.step(ticker, "prose", "Model", "Muse Spark",
                                  "Answered in prose without a tool call (kept as uncommitted research): "
                                  + text[:600], citations=reply.citations()[:20])
                    if prose_nudged:
                        with jobs.RUN_LOCK:
                            c["status_reason"] = ("The model ended with a final answer and no finish_draft; its "
                                                  "proposals are recorded, dispositions are missing.")
                            c["finish"] = {"by": "prose", "at": now_utc(), "text": text[:2000]}
                        return
                    prose_nudged = True
                    nudge = {"role": "user", "content": NUDGE_TOOLS}     # LIMITS-002: prose after this = done
                    history.append(nudge)
                    pending, prev_id = [nudge], reply.response_id
                    continue
                outputs, finished = [], False
                partial_search = reply.unfinished_searches()
                for call in calls:
                    if partial_search and call.get("name") == "finish_draft":
                        # Manus 17:07: a finish in a reply whose own search did not complete is not accepted
                        result = rc.envelope(call.get("call_id"), "error", None, [rc.issue(
                            "unfinished_search", "finish_draft was not accepted: a web search in the same answer did "
                                                 "not complete. Continue, then finish.")])
                        with jobs.RUN_LOCK:
                            self.research["refusals"].append({"at": now_utc(), "ticker": ticker,
                                                              "call_id": call.get("call_id"), "tool": "finish_draft",
                                                              "issues": result["issues"]})
                        outputs.append({"type": "function_call_output", "call_id": call["call_id"],
                                        "output": json.dumps(result, ensure_ascii=False)})
                        continue
                    result, done = self.dispatch(ticker, call)
                    self.note_repeat(ticker, call, result)
                    item = {"type": "function_call_output", "call_id": call["call_id"],
                            "output": json.dumps(result, ensure_ascii=False)}
                    data = result.get("data") if isinstance(result.get("data"), dict) else {}
                    if call.get("name") == "brave_search" and data.get("engine") == "Brave Search":
                        st["brave_items"].append((item, c["requests"]))
                    if data.get("source_id") and data.get("text"):      # page text delivered: compactable later
                        st["delivered"].setdefault(data["source_id"], []).append(
                            (item, [t["page_number"] for t in data["text"]], c["requests"]))
                    outputs.append(item)
                    finished = finished or done
                self.compact(ticker, c["requests"])     # retires only text the model has already been sent
                history += outputs
                pending, prev_id = outputs, reply.response_id
                self.save_ledger()
                if finished:
                    return

        def searches_left(self, ticker: str) -> int:
            c = self.company(ticker)
            per_company, per_request = self.limits["max_searches_per_company"], self.limits["max_searches_per_request"]
            left = 10 ** 9
            if per_company is not None:
                left = max(0, per_company - c["searches"])
            if per_request is not None:
                left = min(left, per_request)
            return left

        def check_company_limits(self, ticker: str) -> None:
            c, L, st = self.company(ticker), self.limits, self.state[ticker]
            if L["max_requests_per_company"] is not None and c["requests"] >= L["max_requests_per_company"]:
                raise Stop("limit", f"Per-company request limit reached ({c['requests']} requests), a setting a "
                                    f"person turned on.")
            if L["max_minutes_per_company"] is not None and \
                    (time.monotonic() - st["started"]) / 60 >= L["max_minutes_per_company"]:
                raise Stop("limit", f"Per-company time limit reached ({L['max_minutes_per_company']} minutes), a "
                                    f"setting a person turned on.")

        def note_attempt(self, empty: bool) -> None:
            with jobs.RUN_LOCK:
                if len(self.first_attempts) >= 5:
                    return
                self.first_attempts.append(empty)
                if len(self.first_attempts) == 5 and sum(self.first_attempts) > 1:
                    rec = {"code": "empty_answer_rate", "at": now_utc(),
                           "detail": f"{sum(self.first_attempts)} of the first 5 research answers were empty "
                                     f"(Addendum 2 kill condition: more than 1 in 5)."}
                    self.research["kill_conditions"].append(rec)
                    self.step(None, "limit", "Code", "runs.research", "Kill condition met: " + rec["detail"])
                    if self.limits["stop_on_empty_rate"]:
                        self.cap_reached = ("Stopped: " + rec["detail"] + " (stop_on_empty_rate is on).")

        def note_repeat(self, ticker: str, call: dict, result: dict) -> None:
            st = self.state[ticker]
            try:
                args = json.dumps(json.loads(call.get("arguments") or "null"), sort_keys=True)
            except (TypeError, json.JSONDecodeError):
                args = str(call.get("arguments"))
            out = {k: v for k, v in result.items() if k != "call_id"}
            key = hashlib.sha256(f"{call.get('name')}|{args}".encode()).hexdigest()
            res = hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()
            seen = st["actions"].setdefault(key, [])
            if res in seen:
                with jobs.RUN_LOCK:
                    rec = {"at": now_utc(), "ticker": ticker, "tool": call.get("name"), "call_id": call.get("call_id"),
                           "times": seen.count(res) + 1}
                    self.research["repeats"].append(rec)
                self.step(ticker, "repeat", "Code", "runs.research",
                          f"Repeated action: {call.get('name')} with the same arguments returned the same result "
                          f"({rec['times']} times). Recorded; the loop goes on.", call_id=call.get("call_id"))
            seen.append(res)

        def redact_brave(self, ticker: str, body: dict) -> dict:
            """BRAVE-PRIMARY-001 storage policy: a turn log keeps a Brave answer as its query and URLs only."""
            ids = self.state.get(ticker, {}).get("brave_calls") or {}
            if not ids:
                return body
            out = copy.deepcopy(body)
            for item in out.get("input", []):
                if item.get("type") == "function_call_output" and item.get("call_id") in ids:
                    item["output"] = json.dumps(ids[item["call_id"]], ensure_ascii=False)
            return out

        def save_turn(self, ticker: str, n: int, body: dict, reply: rr.ResponsesReply, cost) -> None:
            folder = self.dir / "research" / "turns"
            folder.mkdir(parents=True, exist_ok=True)
            rec = {"ticker": ticker, "n": n, "at": now_utc(), "request": self.redact_brave(ticker, body),
                   "response": {"id": reply.response_id, "model": reply.model, "status": reply.status,
                                "incomplete_reason": reply.incomplete_reason, "output": reply.output,
                                "usage": {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                                          "reasoning_tokens": reply.reasoning_tokens},
                                "searches": reply.searches, "error": reply.error, "http_status": reply.http_status,
                                "may_have_been_billed": reply.may_have_been_billed},
                   "cost_usd": cost}
            (folder / f"{ticker}-{n:03d}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n")

        def record_search(self, ticker: str, s: dict) -> None:
            urls = [r["url"] for r in s["results"] if r.get("url")]
            status = s.get("status")
            with jobs.RUN_LOCK:
                self.manifest.append("leads", ticker=ticker, tool="web_search", item_id=s.get("id"),
                                     status=status, queries=s["queries"], results=s["results"][:50])
            self.step(ticker, "search", "Model", "Muse Spark",
                      f"Searched the web: {'; '.join(s['queries']) or '(query not reported)'}. "
                      f"{len(urls)} result{'s' if len(urls) != 1 else ''} (leads, not evidence)."
                      + ("" if status == "completed" else f" The search did not complete (status {status!r}); its "
                                                          f"results are partial."),
                      queries=s["queries"], results=urls[:20], status=status)

        # ------------------------------------------------------------ the function tools

        def dispatch(self, ticker: str, call: dict) -> tuple[dict, bool]:
            cid = call.get("call_id") or f"call-{len(self.state[ticker]['calls']) + 1}"
            name = call.get("name")
            self.state[ticker]["calls"][cid] = {"name": name, "at": now_utc()}
            args, problems = rc.parse_arguments(name, call.get("arguments"))
            if problems:
                with jobs.RUN_LOCK:
                    self.research["refusals"].append({"at": now_utc(), "ticker": ticker, "call_id": cid,
                                                      "tool": name, "issues": problems})
                self.step(ticker, "check", "Code", "runs.research_contract",
                          f"Refused {name}: " + "; ".join(p["message"] for p in problems), call_id=cid)
                return rc.envelope(cid, "error", None, problems), False
            handler = {"brave_search": self.t_brave, "fetch_url": self.t_fetch_url, "fetch_edgar": self.t_fetch_edgar,
                       "fetch_ctgov": self.t_fetch_ctgov, "read_source_pages": self.t_read_pages,
                       "propose_events": self.t_propose, "record_coverage": self.t_coverage,
                       "nothing_material": self.t_nothing_material, "revise_event": self.t_revise,
                       "finish_draft": self.t_finish}[name]
            return handler(ticker, cid, args)

        def admit_paid(self, ticker: str, worst: float) -> int | None:
            """The same strict gate for a non-model paid request (Brave): spend + reservations + worst under the cap,
            or Stop. Queues behind calls in flight."""
            while True:
                if self.stop_requested():
                    raise Stop("person", "Stopped by a person before the next paid request.")
                if self.cap_reached:
                    raise Stop("spend_cap", self.cap_reached)
                with jobs.RUN_LOCK:
                    self.refresh_spend()
                    if jobs.unknown_unaccepted(self.job):
                        raise Stop("unpriced", "A call could not be priced, so the spending cap cannot be kept.")
                    limit = self.research_limit()
                    token = jobs.reserve_budget(self.dir.name, self.meta["spend_usd"], limit, worst,
                                                alone_may_cross=False)
                    if token is not None:
                        return token
                    if not jobs._RESERVED.get(self.dir.name):
                        reason = self.cap_reason("request", worst)
                        self.cap_reached = reason
                        raise Stop("spend_cap", reason)
                time.sleep(0.05)

        def settle_paid(self, ticker: str, token, cost: float, what: str) -> None:
            try:
                with jobs.RUN_LOCK:
                    self.refresh_spend()
                    self.meta["spend_usd"] = round((self.meta["spend_usd"] or 0) + cost, prices.ROUND_TO)
                    c = self.company(ticker)
                    c["spend_usd"] = round(c["spend_usd"] + cost, prices.ROUND_TO)
                    c[what] = c.get(what, 0) + 1
                    self.save(refresh=False)
            finally:
                jobs.release_budget(self.dir.name, token)

        def t_brave(self, ticker, cid, args):
            """BRAVE-PRIMARY-001: the primary search. Leads only; paid per request through the same strict gate; one
            attempt (no retry); a 429 is a recorded limitation. Storage: query + URLs only (capture/brave_search.py)."""
            from capture.brave_search import NAME
            st = self.state[ticker]
            query = args["query"]
            price = prices.request_cost_usd(NAME, 1)
            token = self.admit_paid(ticker, price)
            got = None
            try:
                got = self.tools.brave.search(query, args.get("count") or 10)
            finally:
                billed = got is None or got.error is None or got.may_have_been_billed
                self.settle_paid(ticker, token, price if billed else 0.0, "brave_requests")
            kept = {"call_id": cid, "status": "ok" if not got.error else "error",
                    "data": {"engine": NAME, "query": query, "urls": got.urls},
                    "note": "Brave result titles and snippets are not stored (Brave terms); only the query and URLs."}
            st["brave_calls"][cid] = kept
            with jobs.RUN_LOCK:
                self.manifest.append("leads", ticker=ticker, tool="brave_search", engine=NAME, call_id=cid,
                                     query=query, urls=got.urls, error=got.error, http_status=got.http_status)
            if got.error:
                if got.rate_limited:
                    with jobs.RUN_LOCK:
                        self.company(ticker).setdefault("search_limitations", []).append(
                            {"engine": NAME, "at": now_utc(), "reason": got.error, "query": query})
                self.step(ticker, "search", "Code", "capture.brave_search",
                          f"Brave Search failed for “{query}”: {got.error} Not retried; recorded."
                          + (" Use web_search meanwhile." if got.rate_limited else ""),
                          engine=NAME, queries=[query], results=[], call_id=cid)
                return rc.envelope(cid, "error", {"engine": NAME, "query": query}, [rc.issue(
                    "rate_limited" if got.rate_limited else "search_failed",
                    f"{got.error} Use web_search for this query.")]), False
            self.step(ticker, "search", "Code", "capture.brave_search",
                      f"Searched with Brave Search: {query}. {len(got.urls)} result"
                      f"{'s' if len(got.urls) != 1 else ''} (leads, not evidence).",
                      engine=NAME, queries=[query], results=got.urls[:20], call_id=cid)
            # the titles and snippets go to the model for this turn only (in memory); see redact_brave/compact
            return rc.envelope(cid, "ok", {"engine": NAME, "query": query, "results": got.results,
                                           "note": "Leads only: capture a source with fetch_url before citing it."}
                               ), False

        def _fetch_allowed(self, ticker: str) -> str | None:
            cap = self.limits["max_fetches_per_company"]
            if cap is not None and self.company(ticker)["fetches"] >= cap:
                return f"Per-company fetch limit reached ({cap}), a setting a person turned on."
            return None

        def replay_input(self, ticker: str, history: list, first_n: int) -> list:
            """The bounded input of the next request (REPLAY_TURNS). Never mutates the history."""
            st = self.state[ticker]
            starts = st["turn_starts"]
            if len(starts) <= REPLAY_TURNS:
                keep_from, dropped = (starts[0] if starts else len(history)), 0
            else:
                keep_from, dropped = starts[-REPLAY_TURNS], len(starts) - REPLAY_TURNS
            last_start = starts[-1] if starts else len(history)
            out = list(history[:first_n])
            if dropped:
                out.append({"role": "user", "content": self.summary_text(ticker, dropped)})
            for i in range(keep_from, len(history)):
                item = history[i]
                latest = i >= last_start
                if item.get("type") == "reasoning" and not latest:
                    continue                                   # reasoning replays only with its own turn's calls
                if item.get("type") == "web_search_call" and not latest and "results" in item:
                    item = {k: v for k, v in item.items() if k != "results"}    # leads are in the manifest
                out.append(item)
            return out

        def summary_text(self, ticker: str, dropped: int) -> str:
            """A code-written summary of the research so far, from the run's ledger (no model text)."""
            st = self.state[ticker]
            lines = [f"Research so far (code summary; {dropped} earlier turns are not replayed, every step is "
                     f"recorded):"]
            for sid, src in st["sources"].items():
                by = st["settled"].get(sid)
                lines.append(f"- source {sid}: {src['title'][:100]} ({len(src['pages'])} pages) — "
                             + (f"settled by {', '.join(by)}" if by else "NOT SETTLED: propose from it or "
                                                                          "nothing_material"))
            for r in self.run["rows"]:
                if (r.get("research") or {}).get("ticker") == ticker:
                    codes = ",".join(i["code"] for i in r["research"]["issues"]) or "no issue"
                    lines.append(f"- proposal {r['id']} rev {r['research']['revision']}: {r['title'][:80]} "
                                 f"[{r['date_text'] or 'undated'}] {r['status']}; {codes}")
            for c in self.research["coverage"]:
                if c["ticker"] == ticker and not c.get("a_to_z_gap"):
                    lines.append(f"- coverage {c['category']} {c['status']}: {c['question'][:90]}")
            fetched = {s["url"] for s in st["sources"].values()} | {s["retrieval"]["requested_url"]
                                                                    for s in st["sources"].values()}
            leads = [(u, l.get("title")) for u, l in st["leads"].items() if u not in fetched][:30]
            if leads:
                lines.append("- SEC filings listed and not yet captured (leads): "
                             + "; ".join(f"{t} {u}" for u, t in leads))
            failures = self.company(ticker)["capture_failures"]
            if failures:
                lines.append("- could not capture: " + "; ".join(f"{f['url']} ({f.get('http_status') or f['reason'][:40]})"
                                                                 for f in failures[-10:]))
            return "\n".join(lines)

        def unsettled(self, ticker: str) -> list[str]:
            st = self.state[ticker]
            return [sid for sid in st["sources"] if sid not in st["settled"]]

        def a_to_z_refusal(self, ticker: str, cid: str, url: str) -> dict | None:
            """A-TO-Z-001: refuse a new capture while a captured source is unsettled; record the gap."""
            open_ = self.unsettled(ticker)
            if len(open_) < UNSETTLED_SOURCES_MAX:
                return None
            names = ", ".join(open_)
            with jobs.RUN_LOCK:
                self.research["coverage"].append({
                    "at": now_utc(), "ticker": ticker, "call_id": cid, "category": "other",
                    "question": f"Capture {url}", "status": "partial", "programs": [], "finding_kind": "code",
                    "finding": f"A-TO-Z-001: not captured, because {names} was captured and not yet settled "
                               f"(proposed from, or marked nothing material).", "tool_calls": [],
                    "sources_opened": [], "unverified_references": [], "a_to_z_gap": True,
                    "scope_note": "Gathering without proposing is recorded, never a silent loop."})
            self.step(ticker, "check", "Code", "runs.research",
                      f"A-TO-Z-001: refused to capture {url} while {names} is unsettled.", call_id=cid)
            return rc.envelope(cid, "error", {"unsettled_sources": open_}, [rc.issue(
                "settle_first", f"A-TO-Z-001: first finish {names}: propose_events citing it, or nothing_material "
                                f"with the reason. Then capture the next source. (Searches and listings are not "
                                f"limited.)", None, open_)])

        def settle(self, ticker: str, sid: str, by: str) -> None:
            st = self.state[ticker]
            st["settled"].setdefault(sid, [])
            if by not in st["settled"][sid]:
                st["settled"][sid].append(by)

        def retire_brave(self, ticker: str, requests_made: int) -> None:
            """BRAVE-PRIMARY-001: a Brave answer's titles and snippets are sent to the model exactly once. Called before
            every request on every path (tool, prose, empty answer, timeout) and from compact: an answer registered at
            turn T was delivered by request T+1 and is retired to its query and URLs before any later request."""
            st = self.state[ticker]
            for item, turn in st["brave_items"]:
                if turn < requests_made and id(item) not in st["compacted"]:
                    item["output"] = json.dumps(st["brave_calls"][item["call_id"]], ensure_ascii=False)
                    st["compacted"].add(id(item))

        def compact(self, ticker: str, current_turn: int) -> None:
            """Replace the delivered page text of every settled source in the replayed history with a short code-
            written stub (the source, the pages read, what settled it). Each function_call keeps its output item, so
            the call/output pairing the API requires stays valid; the evidence stays in the captured files and can be
            read again with read_source_pages. (With previous_response_id the provider keeps its own history.)"""
            st = self.state[ticker]
            self.retire_brave(ticker, current_turn)
            for sid, items in st["delivered"].items():
                if sid not in st["settled"]:
                    continue
                src = st["sources"][sid]
                for item, pages, turn in items:
                    if id(item) in st["compacted"] or turn >= current_turn:
                        continue      # Manus 17:07: a fresh read is consumed in the next request before it retires
                    stub = rc.envelope(item["call_id"], "ok", {
                        "source_id": sid, "title": src["title"], "url": src["url"], "pages_total": len(src["pages"]),
                        "pages_read": pages, "settled_by": list(st["settled"][sid]), "compacted": True,
                        "note": "Page text removed from the conversation after this source was settled (A-TO-Z-001). "
                                "It is still captured; call read_source_pages to read it again."})
                    item["output"] = json.dumps(stub, ensure_ascii=False)
                    st["compacted"].add(id(item))

        def t_fetch_url(self, ticker, cid, args, tool="fetch_url", title=None):
            limited = self._fetch_allowed(ticker)
            if limited:
                return rc.envelope(cid, "error", None, [rc.issue("limit", limited)]), False
            url = args["url"]
            refused = self.a_to_z_refusal(ticker, cid, url)
            if refused is not None:
                return refused, False
            try:
                rs.check_url(url, getattr(self.tools.fetcher, "resolve", None))
                fetched = self.tools.fetcher.get(url)
            except rs.FetchRefused as e:
                self.step(ticker, "fetch", "Code", "capture.research_sources", f"Refused to fetch {url}: {e}",
                          call_id=cid)
                return rc.envelope(cid, "error", None, [rc.issue("fetch_refused", str(e))]), False
            except rs.CaptureFailed as e:
                return self._capture_failed(ticker, cid, url, str(e), tool), False
            with jobs.RUN_LOCK:
                self.company(ticker)["fetches"] += 1
            try:
                source = self.register_source(ticker, cid, fetched, tool, title)
            except rs.CaptureFailed as e:
                return self._capture_failed(ticker, cid, url, str(e), tool, fetched), False
            return rc.envelope(cid, "ok", self.source_view(ticker, source, [1])), False

        def _capture_failed(self, ticker, cid, url, reason, tool, fetched=None):
            status = getattr(fetched, "status", None)
            refused = status in (403, 404, 451)
            rec = {"at": now_utc(), "url": url, "tool": tool, "reason": reason, "http_status": status,
                   "retrieved_at": getattr(fetched, "retrieved_at", None),
                   "outcome": "could_not_access" if refused else "capture_failed"}
            with jobs.RUN_LOCK:
                self.company(ticker)["capture_failures"].append(rec)
                self.manifest.append("capture_failed", ticker=ticker, call_id=cid, **rec)
            if refused:
                self.step(ticker, "fetch", "Code", "capture.research_sources",
                          f"Could not access {url}: HTTP {status} at {rec['retrieved_at']}. Recorded; research goes on.",
                          call_id=cid)
                return rc.envelope(cid, "error", {"url": url, "http_status": status,
                                                  "retrieved_at": rec["retrieved_at"]}, [rc.issue(
                    "could_not_access", f"The site answered HTTP {status}, so nothing from it was captured. Look for "
                                        f"the same document at its primary origin: the company's SEC filing (an 8-K "
                                        f"EX-99 press release via fetch_edgar), the company's investor-relations page, "
                                        f"or the issuer's wire release. Do not try to get around the refusal.")])
            self.step(ticker, "fetch", "Code", "capture.research_sources",
                      f"Capture failed for {url}: {reason} Nothing from it can be cited; recorded as a limitation.",
                      call_id=cid)
            return rc.envelope(cid, "error", {"url": url}, [rc.issue(
                "capture_failed", f"{reason} The document is not captured, so it cannot support a claim. Try an "
                                  f"accessible primary alternative.")])

        def register_source(self, ticker: str, cid: str, fetched: rs.Fetched, tool: str, title=None) -> dict:
            """Capture received bytes as a run source (pages/ files + run.json source + manifest line). An identical
            refetch (same final URL, same bytes) reuses the source and adds an access line."""
            st = self.state[ticker]
            sha = hashlib.sha256(fetched.body).hexdigest()
            prior = st["fetch_by_url"].get((fetched.final_url, sha))
            if prior:
                with jobs.RUN_LOCK:
                    self.manifest.append("access", ticker=ticker, call_id=cid, source_id=prior["id"],
                                         original_sha256=sha, retrieved_at=fetched.retrieved_at)
                self.step(ticker, "fetch", "Code", "capture.research_sources",
                          f"Fetched {fetched.final_url} again: the same bytes, so the same source {prior['id']}.",
                          call_id=cid, source_id=prior["id"])
                return prior
            with jobs.RUN_LOCK:
                n = self.job.get("research_next_source", 1)
                self.job["research_next_source"] = n + 1
            sid = f"src-r{n}-{ticker.lower()}"
            captured = rs.capture_document(fetched, self.dir, sid, render_html=self.tools.render_html, title=title)
            lead = (st["leads"].get(fetched.requested_url) or st["leads"].get(fetched.final_url)
                    or self.sec_filing_lead(ticker, cid, fetched.final_url) or {})
            pages = []
            for raw, words in captured["pages"]:
                n_page = words["page_number"]
                stem = f"{sid}-p{n_page}"
                image_rel, words_rel = f"pages/{stem}.png", f"pages/{stem}.words.json"
                (self.dir / "research" / "captures" / sid / "pages").mkdir(parents=True, exist_ok=True)
                (self.dir / "research" / "captures" / sid / "pages" / f"p{n_page}.pdf").write_bytes(raw)
                build_run.render_page_bytes(raw, self.dir / image_rel)
                (self.dir / words_rel).write_text(json.dumps(words, indent=1) + "\n")
                page = {"page_number": n_page, "image": image_rel, "width_pt": words["page_width"],
                        "height_pt": words["page_height"], "words": words_rel,
                        "image_sha256": build_run.file_sha256(self.dir / image_rel),
                        "words_sha256": build_run.file_sha256(self.dir / words_rel)}
                comp = completeness_fields(words)
                if comp:
                    page["capture_completeness"] = comp
                if captured["rendered"].get("form") == "text":
                    page["text_capture"] = {"reason": captured["rendered"]["render_failure"],
                                            "text_sha256": captured["rendered"]["text_sha256"],
                                            "visibility": "unknown"}
                page["visual_evidence"] = sources.visual_evidence(page, words)
                page["research_checks"] = rs.page_capture_checks(words)
                pages.append(page)
            host = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", fetched.final_url) or [None, "web"])[1])
            sec = rs.is_sec(fetched.final_url)
            doc_title = title or lead.get("title") or _html_title(fetched.body, captured["kind"]) or fetched.final_url
            source = {
                "id": sid, "publisher": f"{st['company']} (filed with the SEC)" if sec else host,
                "kind": "filing" if sec else "other", "title": doc_title[:300], "url": fetched.final_url,
                "published_date": lead.get("filing_date"), "captured_at": fetched.retrieved_at,
                "sha256": captured["rendered"]["pdf_sha256"], "pages": pages,
                "retrieval": {"tool": tool, "requested_url": fetched.requested_url, "final_url": fetched.final_url,
                              "retrieved_at": fetched.retrieved_at, "http_status": fetched.status,
                              "content_type": fetched.content_type, "original_sha256": sha,
                              "original_bytes": captured["original_bytes"], "original_file": captured["original_file"],
                              "rendered": captured["rendered"], "manifest_seq": 1, "ticker": ticker,
                              "call_id": cid, "pages_total": len(pages), "pages_captured": len(pages),
                              "page_cap_reason": None},
            }
            if captured["kind"] == "html":
                source["rendered_from"] = {"format": "html", "sha256": sha,
                                           "renderer": captured["rendered"]["renderer"] or "unknown renderer"}
            with jobs.RUN_LOCK:
                line = self.manifest.append("source", ticker=ticker, call_id=cid, source_id=sid,
                                            original_sha256=sha, rendered_pdf_sha256=source["sha256"],
                                            form=captured["rendered"].get("form"),
                                            text_sha256=captured["rendered"].get("text_sha256"),
                                            render_failure=captured["rendered"].get("render_failure"),
                                            url=fetched.final_url, retrieved_at=fetched.retrieved_at,
                                            pages=[{"page_number": p["page_number"], "image_sha256": p["image_sha256"],
                                                    "words_sha256": p["words_sha256"]} for p in pages])
                source["retrieval"]["manifest_seq"] = line["seq"]
                self.run["sources"].append(source)
                st["sources"][sid] = source
                st["fetch_by_url"][(fetched.final_url, sha)] = source
                c = self.company(ticker)
                c["sources"] += 1
                self.save_ledger()
            flags = [f"page {p['page_number']}: {k} {p['research_checks'][k]['result']}" for p in pages
                     for k in ("capture_incomplete", "hidden_text") if p["research_checks"][k]["result"] == "fail"]
            self.step(ticker, "fetch", "Code", "capture.research_sources",
                      f"Captured {fetched.final_url} as {sid}: {len(pages)} page{'s' if len(pages) != 1 else ''}, "
                      f"SHA-256 {sha[:12]}…, retrieved {fetched.retrieved_at}."
                      + (" Capture checks: " + "; ".join(flags) + "." if flags else " Capture checks run on every page.")
                      + (f" Text capture: the page image was not rendered ({captured['rendered']['render_failure']}); "
                         f"the model reads the captured text, and the citation box cannot be checked."
                         if captured["rendered"].get("form") == "text" else ""),
                      call_id=cid, source_id=sid)
            return source

        def sec_filing_lead(self, ticker: str, cid: str, url: str) -> dict | None:
            """An SEC archive document captured by its URL (an exhibit, say) takes its filing date from the filer's
            submissions list, matched by accession: without it 'by year-end' or a yearless day has no year to count
            from (live run 3). One SEC metadata request per filer, cached; recorded in the manifest."""
            m = re.search(r"/Archives/edgar/data/(\d+)/(\d{18})/", url or "")
            if not m:
                return None
            cik, acc = str(int(m[1])), m[2]
            st = self.state[ticker]
            dates = st.setdefault("filing_dates", {})
            if cik not in dates:
                dates[cik] = {}
                listing_url = rs.SUBMISSIONS_URL.format(cik=cik)
                try:
                    got = self.tools.fetcher.get(listing_url)
                    recent = (json.loads(got.body.decode("utf-8")).get("filings") or {}).get("recent") or {}
                    for a, d, f in zip(recent.get("accessionNumber") or [], recent.get("filingDate") or [],
                                       recent.get("form") or []):
                        dates[cik][a.replace("-", "")] = (d, f)
                    with jobs.RUN_LOCK:
                        self.manifest.append("listing", ticker=ticker, call_id=cid, url=listing_url,
                                             sha256=hashlib.sha256(got.body).hexdigest(),
                                             retrieved_at=got.retrieved_at, filings=len(dates[cik]),
                                             purpose="filing date of a captured SEC document")
                except Exception:      # the filing date stays unknown (null), never guessed
                    return None
            hit = dates[cik].get(acc)
            if not hit:
                return None
            return {"filing_date": hit[0], "title": f"{ticker if cik == st['cik'] else 'CIK ' + cik} {hit[1]} document, "
                                                    f"filed {hit[0]} ({url.rsplit('/', 1)[-1]})"}

        def source_view(self, ticker: str, source: dict, pages: list[int]) -> dict:
            inventory = [{"page_number": p["page_number"],
                          "capture_incomplete": p["research_checks"]["capture_incomplete"]["result"],
                          "hidden_text": p["research_checks"]["hidden_text"]["result"]} for p in source["pages"]]
            shown = []
            for n in pages:
                page = next((p for p in source["pages"] if p["page_number"] == n), None)
                if page is None:
                    continue
                words, _ = sources.load_page_evidence(self.dir, page)
                shown.append({"page_number": n, "lines": page_lines(words)})
                with jobs.RUN_LOCK:
                    self.company(ticker)["pages_delivered"] += 1
            return {"source_id": source["id"], "title": source["title"], "url": source["url"],
                    "published_date": source["published_date"], "kind": source["kind"],
                    "retrieved_at": source["captured_at"], "pages_total": len(source["pages"]),
                    "pages": inventory, "text": shown,
                    "capture_form": (source.get("retrieval") or {}).get("rendered", {}).get("form"),
                    "capture_limitations": (source.get("retrieval") or {}).get("rendered", {}).get("limitations") or [],
                    "next": ("A-TO-Z-001: propose_events citing this source, or nothing_material with the reason, "
                             "before capturing another source." if source["id"] not in self.state[ticker]["settled"]
                             else "This source is settled.")}

        def t_read_pages(self, ticker, cid, args):
            src = self.state[ticker]["sources"].get(args["source_id"])
            if src is None:
                return rc.envelope(cid, "error", None, [rc.issue("missing_source", f"Source {args['source_id']} is "
                                                         f"not captured for {ticker}.")]), False
            cap = self.limits["max_pages_per_company"]
            want = [n for n in args["pages"] if any(p["page_number"] == n for p in src["pages"])]
            missing = [n for n in args["pages"] if n not in want]
            issues = [rc.issue("missing_source", f"Source {src['id']} has no page {n}.") for n in missing]
            if cap is not None:
                room = max(0, cap - self.company(ticker)["pages_delivered"])
                if room < len(want):
                    issues.append(rc.issue("limit", f"Per-company page limit ({cap}), a setting a person turned "
                                                    f"on: pages {want[room:]} not delivered."))
                    want = want[:room]
            view = self.source_view(ticker, src, want)
            self.step(ticker, "fetch", "Code", "runs.research",
                      f"Delivered pages {', '.join(map(str, want)) or 'none'} of {src['id']} to the model.",
                      call_id=cid, source_id=src["id"])
            return rc.envelope(cid, "partial" if issues else "ok", view, issues), False

        def t_fetch_edgar(self, ticker, cid, args):
            st = self.state[ticker]
            if int(args["cik"]) != int(st["cik"]):
                return rc.envelope(cid, "error", None, [rc.issue(
                    "identity_mismatch", f"CIK {args['cik']} is not {ticker}'s ({st['cik']}). A partner's filing can "
                                         f"be captured by its sec.gov URL with fetch_url.")]), False
            try:
                since, until = date.fromisoformat(args["since"]), date.fromisoformat(args["until"])
            except ValueError:
                return rc.envelope(cid, "error", None, [rc.issue("malformed_argument",
                                                                 "since and until must be ISO dates.")]), False
            until = min(until, st["as_of"])
            if since > until:
                return rc.envelope(cid, "error", None, [rc.issue("malformed_argument",
                                                                 "since is after until (until is clipped to the "
                                                                 "run's reference date).")]), False
            url = rs.SUBMISSIONS_URL.format(cik=st["cik"])
            try:
                fetched = self.tools.fetcher.get(url)
                if fetched.status is not None and not 200 <= fetched.status < 300:
                    raise rs.CaptureFailed(f"SEC answered HTTP {fetched.status}.")
                listing = rs.edgar_listing(json.loads(fetched.body.decode("utf-8")), st["cik"], args["forms"],
                                           since.isoformat(), until.isoformat())
            except (rs.CaptureFailed, ValueError, UnicodeDecodeError) as e:
                return self._capture_failed(ticker, cid, url, str(e) or type(e).__name__, "fetch_edgar"), False
            with jobs.RUN_LOCK:
                self.company(ticker)["fetches"] += 1
                self.manifest.append("listing", ticker=ticker, call_id=cid, url=url,
                                     sha256=hashlib.sha256(fetched.body).hexdigest(),
                                     retrieved_at=fetched.retrieved_at, filings=len(listing))
            for f in listing:
                if f["url"]:
                    st["leads"][f["url"]] = {"filing_date": f["filing_date"],
                                             "title": f"{ticker} {f['form']}" + (f" ({f['description']})"
                                                                                 if f["description"] else "")
                                                      + f", filed {f['filing_date']}"}
            start = int(args["cursor"] or 0) if str(args["cursor"] or "0").isdigit() else 0
            page = listing[start:start + EDGAR_PAGE]
            nxt = str(start + EDGAR_PAGE) if start + EDGAR_PAGE < len(listing) else None
            self.step(ticker, "fetch", "Code", "capture.research_sources",
                      f"Listed {len(listing)} SEC filings for CIK {st['cik']} from {since} to {until} (leads; "
                      f"retrieved {fetched.retrieved_at}).", call_id=cid)
            return rc.envelope(cid, "ok", {"filings": page, "next_cursor": nxt, "retrieved_at": fetched.retrieved_at,
                                           "note": "Leads only: capture a document with fetch_url to cite it."}), False

        def t_fetch_ctgov(self, ticker, cid, args):
            nct, query = (args["nct_id"] or "").strip() or None, (args["query"] or "").strip() or None
            if (nct is None) == (query is None):
                return rc.envelope(cid, "error", None, [rc.issue("malformed_argument",
                                                                 "Give exactly one of nct_id and query.")]), False
            if nct is not None:
                nct = nct.upper()
                if not NCT.match(nct):
                    return rc.envelope(cid, "error", None, [rc.issue("malformed_argument",
                                                                     "nct_id must look like NCT01234567.")]), False
                return self.t_fetch_url(ticker, cid, {"url": rs.CTGOV_STUDY.format(nct=nct), "purpose": "trial"},
                                        tool="fetch_ctgov", title=f"ClinicalTrials.gov {nct} (registry record)")
            url = rs.ctgov_search_url(query, args["cursor"])
            try:
                fetched = self.tools.fetcher.get(url)
                if fetched.status is not None and not 200 <= fetched.status < 300:
                    raise rs.CaptureFailed(f"ClinicalTrials.gov answered HTTP {fetched.status}.")
                leads, token = rs.ctgov_leads(fetched.body)
            except rs.CaptureFailed as e:
                return self._capture_failed(ticker, cid, url, str(e), "fetch_ctgov"), False
            with jobs.RUN_LOCK:
                self.company(ticker)["fetches"] += 1
                self.manifest.append("leads", ticker=ticker, tool="fetch_ctgov", call_id=cid, query=query,
                                     sha256=hashlib.sha256(fetched.body).hexdigest(),
                                     retrieved_at=fetched.retrieved_at, results=leads[:50])
            self.step(ticker, "search", "Code", "capture.research_sources",
                      f"Searched ClinicalTrials.gov for “{query}”: {len(leads)} studies (leads, not evidence).",
                      call_id=cid)
            return rc.envelope(cid, "ok", {"studies": leads, "next_cursor": token,
                                           "retrieved_at": fetched.retrieved_at,
                                           "note": "Leads only: capture a study with nct_id to cite it."}), False

        # ------------------------------------------------------------ proposals: the checks inside the tool

        def t_propose(self, ticker, cid, args):
            results = [self.intake(ticker, cid, n, ev) for n, ev in enumerate(args["events"])]
            status = "ok" if all(r["status"] in ("checked", "duplicate") for r in results) else "partial"
            self.save_ledger()
            return rc.envelope(cid, status, {"events": results}), False

        def t_revise(self, ticker, cid, args):
            result = self.intake(ticker, cid, 0, args["event"], (args["id"], args["expected_revision"]))
            self.save_ledger()
            return rc.envelope(cid, "ok" if result["status"] == "checked" else "partial", {"events": [result]}), False

        def intake(self, ticker: str, cid: str, n: int, ev: dict, revision: tuple | None = None) -> dict:
            st = self.state[ticker]
            at = now_utc()
            if ev["ticker"].strip().upper() != ticker:
                problems = [rc.issue("identity_mismatch", f"This research is for {ticker}; the event names "
                                                          f"{ev['ticker']}.", f"events[{n}]")]
            else:
                problems = rc.event_runtime_issues(ev, n, st["sources"])
            existing = None
            if not problems and revision is not None:
                rid, rev = revision
                existing = next((r for r in self.run["rows"] if r["id"] == rid and r.get("research")
                                 and r["ticker"].upper() == ticker), None)
                if existing is None:
                    problems = [rc.issue("stale_revision", f"revise_event id: there is no proposal {rid} for {ticker}.",
                                         rid)]
                elif existing["research"]["revision"] != rev:
                    problems = [rc.issue("stale_revision", f"revise_event expected_revision: {rid} is at revision "
                                                           f"{existing['research']['revision']}, not {rev}.", rid)]
                elif not existing["research"]["feedback"]["repair_open"]:
                    problems = [rc.issue("repair_not_offered", f"{rid} already had its one repair; it stays "
                                                               f"recorded for a person.", rid)]
            if problems:
                with jobs.RUN_LOCK:
                    self.research["refusals"].append({"at": at, "ticker": ticker, "call_id": cid, "index": n,
                                                      "tool": "propose_events", "issues": problems,
                                                      "event": copy.deepcopy(ev)})
                self.step(ticker, "check", "Code", "runs.research_contract",
                          f"Refused proposal {n + 1} (“{ev.get('title', '')[:80]}”): "
                          + "; ".join(p["message"] for p in problems), call_id=cid)
                return {"index": n, "status": "refused", "id": None, "issues": problems}
            key = rc.evidence_key(ev)
            if existing is None and key in st["keys"]:
                rid = st["keys"][key]
                self.step(ticker, "check", "Code", "runs.research_contract",
                          f"Proposal {n + 1} repeats {rid} exactly (same event and evidence): the same row.",
                          call_id=cid, row_id=rid)
                return {"index": n, "status": "duplicate", "id": rid, "issues": []}
            row = self.build_row(ticker, cid, ev, existing, at)
            issues = row["research"]["issues"]
            for span in ev["evidence"]:
                self.settle(ticker, span["source_id"], row["id"])
            with jobs.RUN_LOCK:
                st["keys"][key] = row["id"]
                self.link_near_duplicates(ticker, row)
                if existing is None:
                    self.run["rows"].append(row)
                    self.company(ticker)["proposals"] += 1
                else:
                    self.run["rows"][self.run["rows"].index(existing)] = row
            checks = ", ".join(f"{c['label']} {c['result'].replace('_', ' ')}" for c in row["checks"])
            self.step(ticker, "proposal", "Model", "Muse Spark",
                      f"Proposed {row['id']} (revision {row['research']['revision']}): {row['title']} "
                      f"[{row['date_text'] or 'no date'}], citing {len(ev['evidence'])} span(s).",
                      call_id=cid, row_id=row["id"])
            self.step(ticker, "check", "Code", "runs.research_contract",
                      f"Checked {row['id']}: {checks}."
                      + (" Issues: " + "; ".join(i["code"] for i in issues) + "." if issues else " No issue."),
                      call_id=cid, row_id=row["id"], issues=[i["code"] for i in issues])
            fb = row["research"]["feedback"]
            out = {"index": n, "status": "checked", "id": row["id"], "revision": row["research"]["revision"],
                   "row_status": row["status"],
                   "checks": [{"name": c["name"], "result": c["result"], "reason": c["reason"]} for c in row["checks"]],
                   "spans": [{k: s[k] for k in ("source_id", "page_number", "quote_match", "printed_quote_match")}
                             for s in row["research"]["evidence"]],
                   "issues": issues}
            if issues and fb["repair_open"]:
                out["status"] = "needs_repair"
                out["repair"] = (f"Typed feedback (once): call revise_event with id {row['id']}, expected_revision "
                                 f"{row['research']['revision']} and the whole revised event; or leave it for a person.")
            elif issues:
                out["note"] = "Recorded with its issues for a person; no further repair is offered."
            if row["research"]["near_duplicates"]:
                out["linked_near_duplicates"] = row["research"]["near_duplicates"]
            return out

        def build_row(self, ticker: str, cid: str, ev: dict, existing: dict | None, at: str) -> dict:
            st = self.state[ticker]
            spans, words_by_span = [], []
            for span in ev["evidence"]:
                src = st["sources"][span["source_id"]]
                page = next(p for p in src["pages"] if p["page_number"] == span["page_number"])
                words, consumed = sources.load_page_evidence(self.dir, page)
                words_by_span.append(words)
                spans.append(rc.span_check(span, words, page))
            k = ev["primary_span_index"]
            primary = ev["evidence"][k]
            source = st["sources"][primary["source_id"]]
            page = next(p for p in source["pages"] if p["page_number"] == primary["page_number"])
            _, consumed = sources.load_page_evidence(self.dir, page)
            one_page = {**source, "pages": [page]}
            event = {"ticker": ticker, "company": st["company"], "title": ev["title"], "date_text": ev["date_text"],
                     "date": ev["date"], "date_end": ev["date_end"], "quote": primary["quote"],
                     "program": ev["program"], "indication": ev["indication"], "type_hint": ev["type_hint"],
                     "relevance": ev["relevance"], "conditions": copy.deepcopy(ev["conditions"]),
                     "subject_company": ev["subject_company"], "relation": ev["relation"],
                     "word_ids": primary.get("word_ids"), "box": None}
            reading = {"prompt_text": f"{rc.CONTROLLER}\n\nResearch assignment:\n{self.choices.get('note_for_agent')}",
                       "usage": {}, "fake": False, "input": "words"}
            agent_step = [{"step": "Researched", "tag": "Code", "who": "runs.research", "at": at,
                           "text": f"Proposed by the research agent (propose_events, call {cid}), citing "
                                   f"{len(ev['evidence'])} captured span(s); code ran the checks inside the tool. "
                                   f"{LABEL}."}]
            if existing is None:
                number = reserve_row_numbers(self.run, self.job, 1)
            else:
                number = int(existing["id"].lstrip("e"))
            row = build_entry.build_row(number, event, reading, one_page, words_by_span[k], at,
                                        consumed_artifact=consumed, tool_steps=agent_step)
            timing_k = self.apply_timing_span(row, ev, k, one_page, words_by_span, consumed, at)
            self.apply_window([row], at, source=source)
            revision = 1 if existing is None else existing["research"]["revision"] + 1
            research = {"label": LABEL, "revision": revision, "call_id": cid, "ticker": ticker,
                        "primary_span_index": k, "evidence": spans, "action": ev["action"],
                        "modality": ev["modality"], "relevance_kind": ev["relevance_kind"],
                        "placement_proposal": ev["placement_proposal"], "prior_row_ids": list(ev["prior_row_ids"]),
                        "conditions_state": ("not_established" if ev["conditions"] is None else
                                             "none_stated" if ev["conditions"] == [] else "stated"),
                        "near_duplicates": [], "revisions": [], "evidence_key": rc.evidence_key(ev)}
            timing_ctx = row.get("research") or {}
            row["research"] = research
            research["timing_span_index"] = timing_k
            if timing_ctx.get("timing") is not None:
                research["timing"] = timing_ctx["timing"]      # the date check is bound to it (claim_assessment)
                research["timing_release_day"] = timing_ctx["timing_release_day"]
            research["issues"] = rc.research_issues(row, spans, words_by_span, k, timing_k)
            if existing is None:
                research["feedback"] = {"repair_open": bool(research["issues"]),
                                        "given_at": at if research["issues"] else None}
            else:
                old = {kk: copy.deepcopy(v) for kk, v in existing.items() if kk not in ("trail", "research")}
                research["revisions"] = existing["research"]["revisions"] + [{
                    "revision": existing["research"]["revision"], "row": old,
                    "issues": existing["research"]["issues"], "evidence": existing["research"]["evidence"]}]
                research["feedback"] = {"repair_open": False, "given_at": existing["research"]["feedback"]["given_at"],
                                        "repaired_at": at}
                row["trail"] = existing["trail"] + [s for s in row["trail"] if s["step"] in ("Checked", "Researched")]
                row["trail"].append({"step": "Revised", "tag": "Code", "who": "runs.research", "at": at,
                                     "text": f"Revision {revision} replaced revision {revision - 1} after the typed "
                                             f"feedback; the earlier revision is kept in its history."})
            return row

        def apply_timing_span(self, row, ev, k, one_page, words_by_span, consumed, at) -> int | None:
            """Addendum 3 point 1 (Manus 15:03): timing need not sit in the event quote. When a span other than the
            primary one has the role "timing", date_match runs against THAT span's quote (on its own page, with that
            page's dateline for an explicit "today"); the primary span keeps quote_match and the citation box. The
            check's reason names the span it ran on. Returns the timing span's index, or None (primary)."""
            from checks import event_checks
            from runs import recheck
            timing = next((i for i, sp in enumerate(ev["evidence"]) if i != k and "timing" in sp["roles"]), None)
            if timing is None or "timing" in ev["evidence"][k]["roles"]:
                return None
            span = ev["evidence"][timing]
            tsrc = self.state[row["ticker"].upper()]["sources"][span["source_id"]]
            words = words_by_span[timing]["words"]
            # date_match on the timing span runs inside recheck.row_checks, shared with every later re-check
            from runs.claim_assessment import timing_snapshot
            tpage = next(p for p in tsrc["pages"] if p["page_number"] == span["page_number"])
            _, t_consumed = sources.load_page_evidence(self.dir, tpage)
            today = event_checks.release_day(words, tsrc.get("published_date"))
            row["research"] = {"timing": timing_snapshot(tsrc, tpage, span["quote"], t_consumed),
                               "timing_release_day": today[0].isoformat() if today else None}
            results = recheck.row_checks(row, one_page, words_by_span[k], consumed_artifact=consumed)
            bound = next(r for r in results if r.check == event_checks.DATE_ID)
            status = event_checks.row_status(results)
            row.update({"status": status, "group": build_entry.START_GROUP.get(status, "unverified"),
                        "reason": build_entry.row_reason(status, results, row),
                        "checks": [build_run.to_contract_check(r.to_dict()) for r in results]})
            for step in row["trail"]:
                if step["step"] == "Checked" and step["who"] == event_checks.DATE_ID:
                    step["text"] = f"{bound.label}: {bound.result.replace('_', ' ')}. {bound.reason}"
                    step["at"] = at
            return timing

        def link_near_duplicates(self, ticker: str, row: dict) -> None:
            for other in self.run["rows"]:
                if not other.get("research") or other["id"] == row["id"]:
                    continue
                if rc.near_duplicate(row, other):
                    if other["id"] not in row["research"]["near_duplicates"]:
                        row["research"]["near_duplicates"].append(other["id"])
                    if row["id"] not in other["research"]["near_duplicates"]:
                        other["research"]["near_duplicates"].append(row["id"])
                    pair = sorted([row["id"], other["id"]])
                    if not any(c["rows"] == pair for c in self.research["consolidations"]):
                        self.research["consolidations"].append({
                            "rows": pair, "ticker": ticker, "status": "proposed", "at": now_utc(),
                            "reason": "Same program and overlapping timing from different sources; both kept and "
                                      "linked, never merged automatically."})

        # ------------------------------------------------------------ coverage and finish

        def t_coverage(self, ticker, cid, args):
            st = self.state[ticker]
            unknown_calls = [c for c in args["tool_call_ids"] if c not in st["calls"]]
            unknown_sources = [s for s in args["source_ids"] if s not in st["sources"]]
            issues = [rc.issue("unknown_reference", f"Tool call {c} is not one of this company's calls.") for c in
                      unknown_calls] + [rc.issue("unknown_reference", f"Source {s} is not captured for {ticker}.")
                                        for s in unknown_sources]
            calls = {c: st["calls"][c] for c in args["tool_call_ids"] if c in st["calls"]}
            rec = {"at": now_utc(), "ticker": ticker, "call_id": cid, "category": args["category"],
                   "question": args["question"], "status": args["status"], "programs": args["programs"],
                   "finding": args["finding"], "finding_kind": "agent interpretation",
                   "tool_calls": [{"call_id": c, "tool": v["name"], "at": v["at"]} for c, v in calls.items()],
                   "sources_opened": [{"source_id": s, "url": st["sources"][s]["url"],
                                       "retrieved_at": st["sources"][s]["captured_at"]}
                                      for s in args["source_ids"] if s in st["sources"]],
                   "unverified_references": unknown_calls + unknown_sources,
                   "scope_note": "'Not found' covers these queries and sources at these times only, not the whole "
                                 "web."}
            if args["status"] == "checked_none":
                rec["searched_not_found"] = True
                rec["retrieval_times"] = sorted({v["at"] for v in calls.values()}
                                                | {x["retrieved_at"] for x in rec["sources_opened"]})
            with jobs.RUN_LOCK:
                self.research["coverage"].append(rec)
            self.step(ticker, "coverage", "Model", "Muse Spark",
                      f"Coverage ({args['category']}, {args['status'].replace('_', ' ')}): {args['question']}"
                      + (f" — {args['finding']}" if args["finding"] else ""), call_id=cid)
            return rc.envelope(cid, "partial" if issues else "ok", {"recorded": True}, issues), False

        def t_nothing_material(self, ticker, cid, args):
            st = self.state[ticker]
            src = st["sources"].get(args["source_id"])
            if src is None:
                return rc.envelope(cid, "error", None, [rc.issue(
                    "missing_source", f"Source {args['source_id']} is not captured for {ticker}.")]), False
            self.settle(ticker, src["id"], "nothing material")
            with jobs.RUN_LOCK:
                self.research["coverage"].append({
                    "at": now_utc(), "ticker": ticker, "call_id": cid, "category": "other",
                    "question": f"Anything material in {src['title']}?", "status": "checked_none", "programs": [],
                    "finding": args["reason"], "finding_kind": "agent interpretation", "nothing_material": True,
                    "tool_calls": [], "sources_opened": [{"source_id": src["id"], "url": src["url"],
                                                          "retrieved_at": src["captured_at"]}],
                    "unverified_references": [], "searched_not_found": True,
                    "retrieval_times": [src["captured_at"]],
                    "scope_note": "The agent's reading of this captured source only."})
            self.step(ticker, "coverage", "Model", "Muse Spark",
                      f"Nothing material in {src['id']}: {args['reason']}", call_id=cid, source_id=src["id"])
            return rc.envelope(cid, "ok", {"settled": src["id"]}), False

        def t_finish(self, ticker, cid, args):
            st = self.state[ticker]
            mine = [r["id"] for r in self.run["rows"] if (r.get("research") or {}).get("ticker") == ticker]
            given = [d["row_id"] for d in args["dispositions"]]
            unknown = [r for r in given if r not in mine]
            missing = [r for r in mine if r not in given]
            dup_bad = [d["row_id"] for d in args["dispositions"]
                       if d["disposition"] == "duplicate" and d["related_row_id"] not in mine]
            issues = ([rc.issue("unknown_reference", f"{r} is not a proposal of {ticker}.", r) for r in unknown]
                      + [rc.issue("missing_disposition", f"{r} has no disposition.", r) for r in missing]
                      + [rc.issue("malformed_argument", f"{r} is marked duplicate of a row that is not one of this "
                                                        f"company's proposals.", r) for r in dup_bad])
            open_ = self.unsettled(ticker)
            # A-TO-Z-001 (register): a draft cannot end finished with an unconcluded unit
            issues += [rc.issue("settle_first", f"A-TO-Z-001: {sid} was captured and not settled: propose_events "
                                                f"citing it, or nothing_material with the reason, before finishing.",
                                sid) for sid in open_]
            st["finish_attempts"] += 1
            if issues and st["finish_attempts"] == 1:
                self.step(ticker, "check", "Code", "runs.research_contract",
                          "finish_draft sent back once: " + "; ".join(i["message"] for i in issues), call_id=cid)
                return rc.envelope(cid, "partial", {"finished": False,
                                                    "repair": "Send finish_draft again with every proposal."},
                                   issues), False
            with jobs.RUN_LOCK:
                c = self.company(ticker)
                c["finish"] = {"by": "finish_draft", "at": now_utc(), "call_id": cid,
                               "dispositions": args["dispositions"], "limitations": args["limitations"],
                               "missing_dispositions": missing, "issues": issues,
                               "unsettled_sources": open_}
            if open_:
                self.step(ticker, "check", "Code", "runs.research_contract",
                          f"finish_draft not accepted as finished: {', '.join(open_)} still unsettled (A-TO-Z-001).",
                          call_id=cid)
                raise Stop("incomplete", f"Incomplete: the model finished with {', '.join(open_)} captured and not "
                                         f"settled (A-TO-Z-001), so the draft is not complete.")
                c["status_reason"] = (f"The model finished: {len(mine)} proposal{'s' if len(mine) != 1 else ''}, "
                                      f"{len(args['limitations'])} limitation{'s' if len(args['limitations']) != 1 else ''} "
                                      f"recorded." + (f" {len(missing)} without a disposition." if missing else ""))
            self.step(ticker, "finish", "Model", "Muse Spark", c["status_reason"], call_id=cid)
            return rc.envelope(cid, "ok" if not issues else "partial", {"finished": True}, issues), True

    return ResearchJob


def _html_title(body: bytes, kind: str) -> str | None:
    if kind != "html":
        return None
    m = re.search(rb"<title[^>]*>(.*?)</title>", body[:200000], re.I | re.S)
    if not m:
        return None
    import html
    t = " ".join(html.unescape(m.group(1).decode("utf-8", errors="replace")).split())
    return t or None


def run_research(runs_dir: Path, run_id: str, reader, tools: ResearchTools | None = None,
                 as_of: date | None = None) -> None:
    """Drive a research run to its end. Never raises: an unexpected error ends it as failed."""
    jobs = _jobs()
    if not hasattr(reader, "respond"):
        reader = rr.LiveResponsesReader()
    job = make_job_class()(Path(runs_dir) / run_id, reader, tools, as_of)
    with jobs.RUN_LOCK:
        jobs.ACTIVE_RUNS.add(job.dir.name)
    try:
        job.drive()
    except Exception as e:
        job.stop("failed", f"The research job stopped on an unexpected error ({type(e).__name__}).")
    finally:
        with jobs.RUN_LOCK:
            jobs.ACTIVE_RUNS.discard(job.dir.name)
            jobs._RESERVED.pop(job.dir.name, None)       # nothing of this run is in flight once its job has ended
        job.remove_unrecorded_pages()
