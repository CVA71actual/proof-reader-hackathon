"""accuracy.json: a scored run's numbers, saved beside the run so the screens (and the report) can show them.

    python -m accuracy.run --key <key.json> --run <run_id> [--planted <file>] --export <run_id>

writes runs/<run_id>/accuracy.json from a scorer run. It is the accuracy report (accuracy/report.py) in the shape the audit
header and the Accuracy panel read, with what the order adds:

    window         the run's time window {start, months, end} and what it set aside, scored apart from the
                   in-window numbers in models[].events: outside_window and unplaced (each count, proposed_excluded,
                   proposed_not_excluded, never_proposed, items), excluded_by_window (rows the run left out by the
                   window; not held against the model); present when events were scored (its window is null when the run had none)
    sample         true when the run is the made-up sample run: the numbers describe sample data, not real events
    key            {path, sealed, complete, source_sha256, page_number, cells, events}   sealed is what the key says it is:
                   a key in this repo is an author's key and says false; the UI labels the numbers by it
    models         per model: values {found, N, ...}, events {found, M, ...}, planted {caught, total, by_kind},
                   mistakes_by_type
    planted        the planted-error summary {caught, total, by_kind} (top level, where the audit header reads it)
    readers        who read the run: first reader(s), the second reader's status and input, escalation
    agreement      the two readers on the run's event rows: how many were compared, agreed, disagreed, the rate, rows only
                   the second reader found, values the readers read differently, and (with a key) how many rows or values
                   BOTH readers agreed on and the key says were wrong: the case the checks must catch
                   A one-model run (choices.models "one": Muse Spark only) has agreement null, readers.models saying
                   so, and no second_reader count in mistake_log.from_run_by_caught_by.
    mistake_log    counts by type: from this scoring, and from the run's own log (what code, the second reader and the
                   person caught)
    date, product_tip, product_dirty   when it was made, the product commit it was made on, and whether that tree had
                   uncommitted changes
    run_sha256     the sha256 of the run's run.json the numbers were scored from
    decisions_sha256, words, words_sha256   the rest of what was scored: decisions.jsonl's bytes (the person's decisions,
                   which feed only mistake_log.from_run, never the model's scores) and the words file. write() refuses a
                   report whose inputs are not the ones on disk; the server re-reads from_run from the live decisions
    content_sha256 the sha256 of this file's own content (everything but this field)

The server serves it only if both hashes hold (server/app.py): a file edited after it was written, or one that belongs to a
different version of the run, is refused with the reason, like a page file whose hash changed. A hash inside a file shows
accidental damage and a swap of the run; it does not stop someone who rewrites both, and does not claim to.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from runs.one_model import ONE_MODEL_LINE, run_is_one_model

EXPORT_VERSION = 1
FILE_NAME = "accuracy.json"
REPO = Path(__file__).resolve().parent.parent
WRONG_EVENT_BUCKETS = ("wrong_date", "wrong_type", "unsupported", "invented")
WRONG_VALUE_BUCKETS = ("wrong_value", "wrong_place", "extra")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(doc: dict) -> str:
    """The hash of a file's content: everything except content_sha256, as canonical JSON."""
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def git_state(repo: Path = REPO) -> tuple[str | None, bool | None]:
    """(short commit, uncommitted changes?) of the product checkout; (None, None) if git cannot say."""
    try:
        tip = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                                    capture_output=True, text=True, timeout=10, check=True).stdout.strip())
        return tip or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _compared(row: dict) -> bool:
    return row.get("found_by") is None and any(t.get("step") == "Second reading" for t in row.get("trail", []))


def agreement(run: dict, table: dict | None = None, events: dict | None = None) -> dict:
    """The two readers on this run. Agreement is not evidence (the checks decide), so the interesting number is
    `both_wrong`: what both agreed on and the key says is wrong. It is None when there is no key to say so."""
    rows = [r for r in run.get("rows", []) if _compared(r)]
    split = [r for r in rows if r.get("disagreement")]
    agreed_ids = {r["id"] for r in rows if not r.get("disagreement")}
    cells = [c for t in run.get("tables", []) for r in t.get("rows", []) for c in r.get("cells", [])]
    cells_split = [c for c in cells if c.get("disagreement")]
    second = (run.get("run") or {}).get("second_reader") or {}
    read = second.get("status") == "read"
    both_wrong = {"rows": None, "values": None}
    if events is not None and rows:
        both_wrong["rows"] = sum(1 for b in WRONG_EVENT_BUCKETS for it in events.get(b, []) if it.get("id") in agreed_ids)
    if table is not None and read:
        split_ids = {c["id"] for c in cells_split}
        both_wrong["values"] = sum(1 for b in WRONG_VALUE_BUCKETS for it in table.get(b, [])
                                   if it.get("id") and it["id"] not in split_ids)
    return {
        "rows_compared": len(rows), "rows_agreed": len(rows) - len(split), "rows_disagreed": len(split),
        "rate": round((len(rows) - len(split)) / len(rows), 4) if rows else None,
        "second_reader_only_rows": sum(1 for r in run.get("rows", []) if r.get("found_by") == "second_reader"),
        "values_disagreed": len(cells_split), "both_wrong": both_wrong,
        "note": "Agreement is not evidence: the checks decide. both_wrong counts what both readers agreed on and the "
                "key says is wrong.",
    }


def window_block(events: dict | None) -> dict | None:
    if not events:
        return None
    return {"window": events.get("window"), "M_in_window": events["M_in_window"], "M": events["M"],
            "outside_window": events["outside_window"], "unplaced": events["unplaced"],
            "excluded_by_window": events["excluded_by_window"]}


def readers(run: dict, report: dict) -> dict:
    meta = run.get("run") or {}
    first = sorted({n for name, m in report.get("models", {}).items() for n in (m.get("models") or [name])})
    out = {"first": first, "second_reader": meta.get("second_reader"), "escalation": meta.get("escalation")}
    if run_is_one_model(run):       # ONE-MODEL-RUN-001: say so; never a second reader's status
        out.update(second_reader=None, escalation=None, models=ONE_MODEL_LINE)
    return out


def caught_by(run: dict, counts: dict) -> dict:
    """Who caught the run's mistakes. A one-model run had no second reader: its count is left out, not shown as 0."""
    counts = dict(counts)
    if run_is_one_model(run):
        counts.pop("second_reader", None)
    return counts


class StaleReport(Exception):
    """The report was scored from inputs that are no longer the ones on disk."""


def _decisions_bytes(run_dir: Path) -> bytes:
    f = Path(run_dir) / "decisions.jsonl"
    return f.read_bytes() if f.exists() else b""


def _words_sha(run_dir: Path, words: str | None) -> str | None:
    f = Path(run_dir) / words if words else None
    return sha256_bytes(f.read_bytes()) if f and f.is_file() else None


def build_export(report: dict, run: dict, run_bytes: bytes, *, now: datetime | None = None, tip: str | None = None,
                 dirty: bool | None = None) -> dict:
    """The file's content for a scored run. `report`: accuracy.report.build_report's output; `run`: run.json parsed and
    `run_bytes` its bytes (the hash binds the numbers to exactly this run)."""
    if report.get("inputs", {}).get("run_sha256") != sha256_bytes(run_bytes):
        raise StaleReport("The report was scored from a different run.json than the one being exported.")
    now = now or datetime.now(timezone.utc)
    if tip is None and dirty is None:
        tip, dirty = git_state()
    log = report.get("mistake_log") or {}
    scoring = Counter(e["type"] for e in log.get("from_scoring", []))
    from_run = log.get("from_run") or {}
    planted = report.get("planted")
    doc = {
        "export_version": EXPORT_VERSION,
        "run": report.get("run") or (run.get("run") or {}).get("id"),
        "sample": bool((run.get("run") or {}).get("sample")),
        "date": now.date().isoformat(),
        "made_at": now.isoformat(timespec="seconds"),
        "product_tip": tip, "product_dirty": dirty,
        "key": report["key"],
        "readers": readers(run, report),
        "models": report["models"],
        "planted": ({"caught": planted["caught"], "total": planted["total"], "by_kind": planted["by_kind"]}
                    if planted else None),
        # a one-model run has no second reader to agree with: no agreement block, not a block of zeros
        "agreement": (None if run_is_one_model(run) else agreement(run, report.get("table"), report.get("events"))),
        "mistake_log": {"from_scoring": dict(scoring), "from_run": dict(from_run.get("by_type", {})),
                        "from_run_by_caught_by": caught_by(run, from_run.get("by_caught_by", {}))},
        "run_sha256": sha256_bytes(run_bytes),
        **{k: report["inputs"][k] for k in ("decisions_sha256", "words", "words_sha256")},
    }
    if report.get("events"):                # beside the existing fields; absent when no events were scored
        doc["window"] = window_block(report["events"])
    doc["content_sha256"] = content_hash(doc)
    return doc


def write(run_dir: Path, report: dict, **kw) -> Path:
    """Write runs/<id>/accuracy.json for a scored run. Replaces an earlier accuracy.json: the latest scoring stands."""
    run_dir = Path(run_dir)
    run_bytes = (run_dir / "run.json").read_bytes()
    ident = report.get("inputs") or {}
    on_disk = {"run_sha256": sha256_bytes(run_bytes), "decisions_sha256": sha256_bytes(_decisions_bytes(run_dir)),
               "words_sha256": _words_sha(run_dir, ident.get("words"))}
    changed = [k for k, v in on_disk.items() if ident.get(k) != v]
    if changed:
        raise StaleReport(f"Not written: the report was scored from inputs that are not the ones on disk now "
                          f"({', '.join(changed)}). Score the run again.")
    doc = build_export(report, json.loads(run_bytes), run_bytes, **kw)
    out = run_dir / FILE_NAME
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return out


UNBOUND = "Accuracy file predates input binding; re-score"


def _unbound_reason(doc: dict) -> str | None:
    """A plain reason if the saved file lacks any part of its scoring identity (run, words, decisions hashes; version).
    Nothing is filled in from today's files and no score is shown."""
    def is_hash(v):
        return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
    if doc.get("export_version") != EXPORT_VERSION:
        return f"{UNBOUND} (stale or unbound: the file's version is missing or not {EXPORT_VERSION})."
    missing = [k for k in ("run_sha256", "words_sha256", "decisions_sha256") if not is_hash(doc.get(k))]
    if not isinstance(doc.get("words"), str) or not doc.get("words"):
        missing.append("words")
    if missing:
        return f"{UNBOUND} (stale or unbound: missing or invalid {', '.join(missing)})."
    return None


def read_if_intact(run_dir: Path) -> tuple[bytes | None, str | None]:
    """(the file's bytes, None) if accuracy.json exists and both its hashes hold; (None, None) if there is none;
    (None, a plain reason) if it is damaged or belongs to another version of the run."""
    f = Path(run_dir) / FILE_NAME
    if not f.is_file():
        return None, None
    data = f.read_bytes()
    try:
        doc = json.loads(data)
    except json.JSONDecodeError:
        return None, f"This run's {FILE_NAME} is not valid JSON."
    if not isinstance(doc, dict) or doc.get("content_sha256") != content_hash(doc):
        return None, f"This run's {FILE_NAME} does not match the hash recorded in it: it was changed after it was written."
    bound = _unbound_reason(doc)
    if bound:
        return None, bound
    if doc.get("run_sha256") != sha256_bytes((Path(run_dir) / "run.json").read_bytes()):
        return None, (f"This run's {FILE_NAME} was scored from a different version of run.json than the one on disk; "
                      f"it is not served.")
    if doc.get("words_sha256") != _words_sha(run_dir, doc.get("words")):
        return None, f"This run's {FILE_NAME} was scored from a different words file than the one on disk; it is not served."
    if doc.get("decisions_sha256") != sha256_bytes(_decisions_bytes(run_dir)):
        doc = _with_current_decisions(doc, Path(run_dir))        # a person decided since: refresh the decision block
        data = (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    return data, None


def _with_current_decisions(doc: dict, run_dir: Path) -> dict:
    """The saved doc with only its decision-derived block (mistake_log.from_run*) taken from the live decisions. The
    model's scores are untouched: a Keep or Fix never changes them."""
    from accuracy.mistake_log import load_run, mistake_log
    run, decisions = load_run(run_dir)
    live = mistake_log(run, decisions)
    out = {**doc, "mistake_log": {**doc["mistake_log"], "from_run": dict(live["by_type"]),
                                  "from_run_by_caught_by": caught_by(run, live["by_caught_by"])},
           "decisions_sha256": sha256_bytes(_decisions_bytes(run_dir))}
    out["content_sha256"] = content_hash(out)
    return out
