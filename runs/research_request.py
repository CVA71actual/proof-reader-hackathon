"""The research run's request record (CVA 2026-11-03, after run 9): what each model stage of a research run is sent,
frozen at admission and versioned.

Why: run 9's New Run box was presented as "exactly what is sent" while the researcher also received a code-owned
controller with substantive timing rules and a company card. Now the person's brief is the whole research policy and
is sent FIRST (runs/research_contract.researcher_instructions); code text is only tool mechanics and output format.
This module makes the complete effective request inspectable before a run starts (effective_request, served by
GET/POST /api/research/effective-request) and freezes it when the run is admitted (freeze, called by
server/jobs.create_run), so the trail shows the values that run used, never today's defaults.

The record is research/request_record.json in the run folder:
    {"schema": "proof-reader/research-request-record", "schema_version": 1, "run_id",
     "versions": [{"version", "created_at", "reason", "changed": [part, ...], "digest", "stages": [...]}],
     "company_starts": [{"ticker", "version", "at", "company_card", "tools_sent"}],
     "uses": [{"stage", "ticker", "version", "at", "turn"?, "file"?, "model"?}]}
Versions are append-only and never edited: version 1 is written at admission; when the effective request at a later
point (a company's research start, a review start) differs from the latest version, a new version is appended with
the parts that changed, and every later call names that version. Every saved research turn
(research/turns/<ticker>-<n>.json) carries "request_version".

What is filled in at a company's research start (and recorded under company_starts, never in a version): the SEC
identity code resolves (company, cik) and which search tools are available (Brave needs a key). A run saved before this
record existed has no record: request_record_view says "unavailable" with the reason, and only shows, clearly labelled,
what its saved turn files hold; nothing is reconstructed as if it were the original.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA = "proof-reader/research-request-record"
SCHEMA_VERSION = 1
RECORD_FILE = "research/request_record.json"
CARD_FRAMING = "Company card:\n"
WINDOW_MONTHS = 6
RESOLVED_AT_START = ("company", "cik", "search_tools")
SECRET_ENVS = ("MODEL_API_KEY", "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY", "BASETEN_API_KEY", "BASETEN_DEEPSEEK_API_KEY",
               "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
NO_RECORD = ("This run was admitted before request records existed, so the exact instructions, run context and tool "
             "definitions were not frozen at admission. Nothing here is reconstructed from today's defaults.")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_months(d: date, months: int) -> date:
    from checks import event_checks
    return event_checks.add_months_to(d, months)


def reference_date_of(run: dict) -> date:
    """The research loop's reference date: runs.state.run_reference_date, else today (as research_company does)."""
    from runs.state import run_reference_date
    return run_reference_date(run) or date.today()


def window_of(as_of: date) -> dict:
    return {"start": as_of.isoformat(), "end": _add_months(as_of, WINDOW_MONTHS).isoformat(),
            "months": WINDOW_MONTHS}


def company_card(ticker: str, as_of: date, company: str | None = None, cik: str | None = None,
                 brave: bool | None = None) -> dict:
    """The company card the researcher receives: facts only (identity, reference date, window, search tools), no
    research policy. With company/cik/brave None it is the admission template; research_company fills them in."""
    window = window_of(as_of)
    if brave is None:
        tools = None
    else:
        tools = ("brave_search (primary), web_search (secondary)" if brave
                 else "brave_search is unavailable in this run; use web_search")
    return {"ticker": ticker, "company": company, "cik": cik, "as_of": as_of.isoformat(),
            "calendar_window": [window["start"], window["end"]], "search_tools": tools}


def card_message(card: dict) -> dict:
    return {"role": "user", "content": CARD_FRAMING + json.dumps(card)}


def _function_tool_view(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        f = t.get("function") if isinstance(t.get("function"), dict) else t
        out.append({"name": f["name"], "description": f.get("description"), "parameters": copy.deepcopy(f["parameters"])})
    return out


def _reviewer_chain() -> list[dict]:
    from capture import research_reviewer as rv
    from capture import second_reader
    # CVA 2026-11-03 late: Meta's open-weight Muse Spark Open reviews first; DeepSeek V4 Pro only if it is unavailable
    return [{"name": rv.FALLBACK_REVIEWER_NAME, "model": second_reader.BASETEN_MODEL,
             "family": rv.FALLBACK_REVIEWER_FAMILY, "order": 1},
            {"name": rv.DEEPSEEK_NAME, "model": rv.DEEPSEEK_MODEL_ID, "family": rv.DEEPSEEK_FAMILY, "order": 2,
             "role": "used only if Muse Spark Open is unavailable"}]


def effective_request(choices: dict, as_of: date) -> dict:
    """The complete effective request of a research run, per stage, from its validated choices and reference date.
    No model is called and nothing is resolved over the network. Deterministic: the preview and the admission record
    are this same function of the same inputs."""
    from capture import research_instructions as ri
    from capture import research_reviewer as rv
    from capture import responses_reader as rr
    from capture import support_signal as ss
    from capture import wonder
    from runs import research_contract as rc
    from runs import research as research_mod
    brief = ri.research_assignment(choices.get("note_for_agent"))
    tickers = list(choices.get("companies") or [])
    limits = choices.get("research_limits") or research_mod.validate_limits(None)
    research = {
        "stage": "research", "model": rr.NAME, "model_id": rr.MODEL,
        "sent_as": ("instructions: the brief first, then the technical contract; first input item: the company card. "
                    "Later input items are the model's own turns, tool results and the code messages below."),
        "editable": {"brief": brief, "brief_is_default": brief == ri.RESEARCH_DEFAULT},
        "enforced_by_code": {
            "contract_text": rc.CONTROLLER,
            "instructions_text": rc.researcher_instructions(brief),
            "company_card_framing": CARD_FRAMING,
            "tools": ([{"name": "web_search", "type": "web_search (built in)",
                        "description": "The provider's built-in web search; offered while searches are left.",
                        "parameters": {"search_context_size": "medium"}}]
                      + _function_tool_view(rc.function_tools(brave=True))),
            "tool_availability": ("brave_search is sent only when Brave Search is available in the run (the "
                                  "company card says which); every other tool is always sent."),
            "request_settings": {"max_output_tokens": rr.MAX_OUTPUT_TOKENS, "reasoning": dict(rr.REASONING),
                                 "tool_choice": "auto", "parallel_tool_calls": False},
            "code_messages": {"no_tool_call": research_mod.NUDGE_TOOLS, "cut_off": research_mod.NUDGE_RUNAWAY,
                              "empty_answer": research_mod.NUDGE_EMPTY,
                              "replay_summary": ("When older turns are dropped from the replay, code inserts one "
                                                 "summary it writes from the run's own ledger (sources, proposals, "
                                                 "coverage); it is saved in each turn file.")},
        },
        "run_context": {"reference_date": as_of.isoformat(), "window": window_of(as_of),
                        "company_cards": [company_card(t, as_of) for t in tickers],
                        "turn_limit_per_company": limits.get("turn_ceiling_per_company"),
                        "stop_rules": {k: limits.get(k) for k in research_mod.STOP_RULE_DEFAULTS},
                        "resolved_at_research_start": list(RESOLVED_AT_START)},
    }
    review = {
        "stage": "review", "model": " then ".join(m["name"] for m in _reviewer_chain()[:2]),
        "models": _reviewer_chain(),
        "sent_as": ("instructions: the reviewer instructions below; first message: the company, the brief (as the "
                    "draft's assignment), the draft proposals, the list of captured sources and the cited pages."),
        "editable": {"brief": brief},
        "enforced_by_code": {"instructions_text": rv.REVIEWER_INSTRUCTIONS,
                             "tools": _function_tool_view(rv.REVIEWER_TOOLS)},
        "run_context": {"reference_date": as_of.isoformat(), "window": window_of(as_of)},
    }
    adjudication = {
        "stage": "adjudication", "model": rv.ADJUDICATOR_NAME, "model_id": wonder.MODEL,
        "runs_when": "only for a review finding that disputes a row's reading; one request per dispute",
        "editable": None,
        "enforced_by_code": {"instructions_text": rv.ADJUDICATOR_INSTRUCTIONS, "tools": []},
    }
    on = choices.get("support_check") == "lettucedetect"
    support = {
        "stage": "support_check", "engine": "LettuceDetect", "model": ss.MODEL, "model_revision": ss.MODEL_REVISION,
        "enabled": on, "status_if_off": None if on else "not run: the support check is off for this run",
        "editable": None,
        "enforced_by_code": {"question": ss.QUESTION, "max_tokens": ss.MAX_TOKENS,
                             "input": "each proposal's title (the claim) and its cited passage (every evidence span's "
                                      "quote, in order)"},
        "note": "A local detector, not a language model with instructions. Advisory only: no finding is not "
                "'supported', and could not run is a recorded limitation.",
    }
    return {"stages": [research, review, adjudication, support]}


def digest(stages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(stages, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def changed_parts(old: list[dict], new: list[dict]) -> list[str]:
    """Which parts differ between two versions' stages, as 'stage.part' paths."""
    out = []
    by = {s["stage"]: s for s in old}
    for s in new:
        o = by.get(s["stage"])
        if o is None:
            out.append(s["stage"])
            continue
        for k in sorted(set(s) | set(o)):
            if isinstance(s.get(k), dict) and isinstance(o.get(k), dict):
                out += [f"{s['stage']}.{k}.{kk}" for kk in sorted(set(s[k]) | set(o[k])) if s[k].get(kk) != o[k].get(kk)]
            elif s.get(k) != o.get(k):
                out.append(f"{s['stage']}.{k}")
    return out


def record_path(run_dir: Path) -> Path:
    return Path(run_dir) / RECORD_FILE


def load(run_dir: Path) -> dict | None:
    p = record_path(run_dir)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _write(run_dir: Path, record: dict) -> None:
    p = record_path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _version(n: int, reason: str, stages: list[dict], changed: list[str]) -> dict:
    return {"version": n, "created_at": now_utc(), "reason": reason, "changed": changed, "digest": digest(stages),
            "stages": stages}


def freeze(run_dir: Path, run: dict, reason: str = "admission") -> dict:
    """Version 1, written when the run is admitted. Refuses to overwrite an existing record."""
    if record_path(run_dir).exists():
        raise ValueError("This run already has a request record; versions are only appended.")
    meta = run["run"]
    stages = effective_request(meta["choices"], reference_date_of(run))["stages"]
    record = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "run_id": meta["id"],
              "versions": [_version(1, reason, stages, [])], "company_starts": [], "uses": []}
    _write(run_dir, record)
    return record


def ensure_current(run_dir: Path, run: dict, at_what: str) -> int:
    """The version in force now: the latest, or a new one appended when today's effective request for this run (its
    saved choices and reference date, the code's current contract and tools) differs. Call under RUN_LOCK."""
    record = load(run_dir)
    if record is None:
        freeze(run_dir, run, reason=f"recorded at {at_what}: this run had no admission record")
        return 1
    stages = effective_request(run["run"]["choices"], reference_date_of(run))["stages"]
    latest = record["versions"][-1]
    if digest(stages) == latest["digest"]:
        return latest["version"]
    n = latest["version"] + 1
    changed = changed_parts(latest["stages"], stages)
    record["versions"].append(_version(n, f"the effective request at {at_what} differs from version "
                                          f"{latest['version']}", stages, changed))
    _write(run_dir, record)
    return n


def record_company_start(run_dir: Path, ticker: str, version: int, card: dict, tools_sent: list[str]) -> None:
    record = load(run_dir)
    record["company_starts"].append({"ticker": ticker, "version": version, "at": now_utc(),
                                     "company_card": copy.deepcopy(card), "tools_sent": list(tools_sent)})
    _write(run_dir, record)


def record_use(run_dir: Path, stage: str, ticker: str | None, version: int, **refs) -> None:
    record = load(run_dir)
    if record is None:
        return
    record["uses"].append({"stage": stage, "ticker": ticker, "version": version, "at": now_utc(), **refs})
    _write(run_dir, record)


# ---------------------------------------------------------------- serving it


def redact(obj):
    """Secrets only: the value of any configured key that appears in a string becomes [redacted]."""
    secrets = [v for v in (os.environ.get(k) for k in SECRET_ENVS) if v and len(v) >= 8]
    if not secrets:
        return obj

    def walk(x):
        if isinstance(x, str):
            for s in secrets:
                x = x.replace(s, "[redacted]")
            return x
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x
    return walk(obj)


def preview(choices: dict, as_of: date) -> dict:
    out = effective_request(choices, as_of)
    out["note"] = ("Preview: no model is called. Company identity (company, cik) and Brave availability are filled in "
                   "by code when each company's research starts and recorded with the run.")
    return redact(out)


def _saved_turn_evidence(run_dir: Path) -> dict | None:
    """What a legacy run's saved turn files hold (first and last turn per company), labelled as such."""
    folder = Path(run_dir) / "research" / "turns"
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        return None
    by: dict[str, list[Path]] = {}
    for f in files:
        by.setdefault(f.stem.rsplit("-", 1)[0], []).append(f)
    companies = []
    for ticker, fs in sorted(by.items()):
        first = json.loads(fs[0].read_text(encoding="utf-8")).get("request") or {}
        last = json.loads(fs[-1].read_text(encoding="utf-8")).get("request") or {}
        items = [i.get("content") for i in first.get("input") or [] if isinstance(i, dict) and i.get("role") == "user"
                 and isinstance(i.get("content"), str)][:2]
        companies.append({
            "ticker": ticker, "first_turn_file": f"research/turns/{fs[0].name}",
            "last_turn_file": f"research/turns/{fs[-1].name}", "turn_files": len(fs),
            "instructions_text": first.get("instructions"),
            "first_input_messages": items,
            "tools": [t.get("name") or t.get("type") for t in first.get("tools") or []],
            "instructions_same_in_last_turn": last.get("instructions") == first.get("instructions"),
        })
    return {"label": "From saved request files (research/turns), not an admission record. Only the first and last "
                     "saved turn of each company were compared.",
            "companies": companies}


def request_record_view(run_dir: Path, run: dict) -> dict:
    """GET /api/runs/{id}/request-record: the saved record (each version's digest re-checked), or unavailable."""
    run_id = run["run"]["id"]
    if (run["run"].get("choices") or {}).get("mode") != "research":
        return {"run_id": run_id, "status": "not_applicable", "reason": "This is not a research run."}
    record = load(run_dir)
    if record is None:
        return redact({"run_id": run_id, "status": "unavailable", "reason": NO_RECORD,
                       "from_saved_request_files": _saved_turn_evidence(run_dir)})
    versions = []
    for v in record["versions"]:
        intact = digest(v["stages"]) == v["digest"]
        versions.append({**v, "intact": intact,
                         "used_by": [u for u in record["uses"] if u["version"] == v["version"]]})
    return redact({"run_id": run_id, "status": "recorded", "current_version": record["versions"][-1]["version"],
                   "versions": versions, "company_starts": record["company_starts"], "uses": record["uses"]})
