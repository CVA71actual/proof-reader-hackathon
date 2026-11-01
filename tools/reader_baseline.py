"""Offline reader-baseline import: what a reader returned vs. what downstream code kept.

Imports SAVED reader artifacts (the reading-record shape written by capture/read_events.py and
capture/second_reader.py) into the shared "offline_replay" JSON interface v1, with optional downstream row
snapshots and an explicit link map. Archived replay only: no model call, no credentials, no network, no writes
outside --out. It is not a test of any prompt and not evidence about accuracy.

    python tools/reader_baseline.py --reader first=fixtures/X.events.json --reader second=fixtures/X.events.second.json
    python tools/reader_baseline.py --reader PATH [--downstream KEY=ROWS.json] [--links LINKS.json] [--out OUT.json]

KEY is the artifact file name (e.g. X.events.json) or its record_id. Exit codes: 0 written, 2 usage or unsupported
input (nothing written).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from capture.read_events import parse_events   # the real parser; its contract is the only one supported here
from capture.read_table import sha256_text

SCHEMA_VERSION = 1
ROLES = ("first", "second", "unknown")
TOOL = "tools/reader_baseline.py"
# keys lifted into the record proper; everything else of the artifact is kept verbatim under metadata.recorded
_LIFTED = ("model", "prompt_text", "raw_response", "events")
_HOME = re.compile(r"/(?:Users|home)/[^/\s\"']+")


class InputError(Exception):
    """Unsupported or unusable input; the CLI reports it and writes nothing."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def portable_path(path: str | Path) -> str:
    """Relative paths stay as given (posix form); absolute paths are reduced to the file name, never a home path."""
    p = Path(path)
    return p.name if p.is_absolute() or ".." in p.parts else p.as_posix()


def _load_json(path: Path, what: str):
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise InputError(f"{what} {portable_path(path)} cannot be read ({type(e).__name__}).")
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InputError(f"{what} {portable_path(path)} is not JSON.")


def supported_reading(data) -> str | None:
    """None when data is a reading record of the event-reader contract, else one plain sentence why not."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if "table" in data:
        return "a table reading (capture/read_table.py); only event readings are supported"
    # The record shape written by capture/read_events.py _record (also used by capture/second_reader.py) always
    # carries the `raw_response` and `events` keys, even for a failed call (both null). An object with neither key
    # is not such a record, whatever else it holds (e.g. a bare {"error": ...}).
    if "raw_response" not in data and "events" not in data:
        return ("no raw_response or events key; not an event reading record of the capture/read_events.py shape")
    if "events" in data and data["events"] is not None and not isinstance(data["events"], list):
        return "saved events is neither a list nor null; not an event reading record"
    return None


def _portability_warnings(obj, found: list[str], where: str = "metadata.recorded") -> list[str]:
    if isinstance(obj, str):
        if _HOME.search(obj):
            found.append(where)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _portability_warnings(v, found, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _portability_warnings(v, found, f"{where}[{i}]")
    return found


def classify(artifact: dict) -> tuple[str, str | None, list]:
    """(parse_status, parse_error, proposals) from what the reader actually returned, through the real parser.
    Proposals come from re-parsing raw_response, never from the artifact's saved `events` (those are compared
    separately, in metadata)."""
    error = artifact.get("error") or (artifact.get("parse_problem") if artifact.get("call_failed") else None)
    raw = artifact.get("raw_response")
    if artifact.get("call_failed") or artifact.get("error"):
        return "error", error or "The recorded call failed (no message saved).", []
    if "raw_response" not in artifact:
        return "unavailable", "The artifact has no raw_response; nothing returned by the reader was saved.", []
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "empty", "The model returned an empty answer.", []
    if not isinstance(raw, str):
        return "malformed", "raw_response is not text.", []
    events, problem = parse_events(raw)
    if events is None:
        return "malformed", problem, []
    return "ok", None, events


def build_record(path: Path, role_arg: str | None, measure: bool = False) -> dict:
    raw_bytes, artifact = _load_json(path, "Reader artifact")
    why = supported_reading(artifact)
    if why:
        raise InputError(f"Unsupported reader artifact {portable_path(path)}: {why}.")
    t0 = time.perf_counter()
    status, perr, proposals = classify(artifact)
    elapsed = round(time.perf_counter() - t0, 6) if measure else None
    digest = sha256_bytes(raw_bytes)

    role = role_arg
    inferred = False
    if role is None:
        # only an explicit marker the reader's own artifact saved; file names and models are not evidence
        role, inferred = ("second", True) if artifact.get("second_reader") is True else ("unknown", False)

    prompt = artifact.get("prompt_text")
    recorded_prompt_hash = artifact.get("prompt_sha256")
    saved_events = artifact.get("events")
    recorded = {k: v for k, v in artifact.items() if k not in _LIFTED}
    hist = artifact.get("page_sha256")
    usage = artifact.get("usage") if isinstance(artifact.get("usage"), dict) else None
    meta = {
        "import": {
            "tool": TOOL,
            "artifact_sha256": digest,            # hash of the file supplied now, at import
            "role_source": "explicit" if role_arg else ("artifact_marker" if inferred else "not_stated"),
        },
        "provenance": {
            # the historical hashes are only what the artifact itself recorded; never rebuilt from today's files
            "historical_source_sha256": hist if isinstance(hist, str) else None,
            "historical_source_sha256_status": "recorded_in_artifact" if isinstance(hist, str) else "missing",
            "recorded_prompt_sha256": recorded_prompt_hash if isinstance(recorded_prompt_hash, str) else None,
            "prompt_available": isinstance(prompt, str),
            "prompt_sha256_check": (
                "not_checkable" if not (isinstance(prompt, str) and isinstance(recorded_prompt_hash, str))
                else ("match" if sha256_text(prompt) == recorded_prompt_hash else "mismatch")),
            "links_supplied": False,
        },
        "recorded": recorded,                     # old metadata, verbatim: created_at, usage, route, ...
        "recorded_timing_cost": {                 # null = nothing recorded; no figure is invented
            "elapsed_seconds": artifact.get("elapsed_seconds"),
            "cost_usd": artifact.get("cost_usd"),
            "usage": usage,
        },
        "replay": {"elapsed_seconds": elapsed},   # this import's own parse time, only with --measure-elapsed
        "saved_parse": {                          # what the artifact's own downstream-side parse retained
            "saved_events_present": isinstance(saved_events, list),
            "saved_events_count": len(saved_events) if isinstance(saved_events, list) else None,
            "saved_readable": artifact.get("readable"),
            "saved_events_equal_reparse": (saved_events == proposals) if isinstance(saved_events, list)
            and status == "ok" else None,
        },
    }
    if "raw_response" not in artifact and isinstance(saved_events, list):
        # Missing raw evidence stays missing: `proposals` (what the reader returned) stays []. The saved events are
        # what downstream-side code retained, so they are carried only here, labelled as such.
        meta["saved_parse"]["saved_events"] = saved_events
        meta["saved_parse"]["saved_events_are"] = ("what the artifact's downstream-side parse retained; the reader's "
                                                   "raw reply was not saved, so these are not reader proposals")
    warns = _portability_warnings(recorded, [])
    if warns:
        meta["portability_warnings"] = warns
    # synthetic must say synthetic: an explicit synthetic marker, or a fake-model reading (read_events writes fake)
    markers = [k for k in ("synthetic", "fake") if artifact.get(k) is True]
    if markers:
        meta["synthetic"] = True
        meta["synthetic_markers"] = markers
    return {
        "record_id": f"{path.name}@{digest[:12]}",
        "source": {"artifact_path": portable_path(path), "sha256": digest, "page": None},
        "reader_role": role,
        "model": artifact.get("model") if isinstance(artifact.get("model"), str) else None,
        "prompt": prompt if isinstance(prompt, str) else None,
        "raw_response": artifact.get("raw_response") if isinstance(artifact.get("raw_response"), str) else None,
        "parse_status": status,
        "parse_error": perr,
        "proposals": proposals,
        "downstream_rows": None,
        "links": [],
        "metadata": meta,
    }


def load_rows(path: Path):
    """Rows of a downstream snapshot: a JSON list, or an object with a `rows` list. [] stays a supplied empty."""
    raw, data = _load_json(path, "Downstream snapshot")
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        data = data["rows"]
    if not isinstance(data, list):
        raise InputError(f"Downstream snapshot {portable_path(path)} is neither a list nor an object with `rows`.")
    return data, sha256_bytes(raw)


def _is_index(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def apply_links(record: dict, entries: list) -> None:
    """Validate explicit {proposal_index,row_index} pairs. Valid, unambiguous pairs go in record['links'];
    everything else is reported in metadata.link_issues and stays unpaired. Nothing is inferred."""
    issues, good = [], []
    rows = record["downstream_rows"]
    n_prop = len(record["proposals"])
    for i, e in enumerate(entries):
        if (not isinstance(e, dict) or not _is_index(e.get("proposal_index")) or not _is_index(e.get("row_index"))):
            issues.append({"kind": "malformed", "link": e, "entry": i})
        elif rows is None:
            issues.append({"kind": "downstream_unavailable", "link": e, "entry": i})
        elif not (0 <= e["proposal_index"] < n_prop) or not (0 <= e["row_index"] < len(rows)):
            issues.append({"kind": "out_of_bounds", "link": e, "entry": i,
                           "proposal_count": n_prop, "row_count": len(rows)})
        else:
            good.append((e["proposal_index"], e["row_index"], i))
    pairs = {}
    for p, r, i in good:
        pairs.setdefault((p, r), []).append(i)
    by_p, by_r = {}, {}
    for p, r in pairs:
        by_p.setdefault(p, set()).add(r)
        by_r.setdefault(r, set()).add(p)
    links = []
    for (p, r), idxs in pairs.items():
        if len(by_p[p]) > 1 or len(by_r[r]) > 1:
            issues.append({"kind": "conflicting", "link": {"proposal_index": p, "row_index": r}, "entries": idxs,
                           "detail": "proposal maps to several rows" if len(by_p[p]) > 1
                           else "row is claimed by several proposals"})
        else:
            if len(idxs) > 1:
                issues.append({"kind": "duplicate", "link": {"proposal_index": p, "row_index": r}, "entries": idxs})
            links.append({"proposal_index": p, "row_index": r})
    record["links"] = sorted(links, key=lambda x: (x["proposal_index"], x["row_index"]))
    record["metadata"]["provenance"]["links_supplied"] = True
    if issues:
        record["metadata"]["link_issues"] = issues


def structure(record: dict) -> dict:
    """Structural observations only. A count difference is not a mistake caught and not a quality measure."""
    rows, props = record["downstream_rows"], record["proposals"]
    lp = {x["proposal_index"] for x in record["links"]}
    lr = {x["row_index"] for x in record["links"]}
    return {
        "proposal_count": len(props),
        "downstream_row_count": None if rows is None else len(rows),
        "linked_pairs": len(record["links"]),
        "unlinked_proposals": len(props) - len(lp),
        "unlinked_rows": None if rows is None else len(rows) - len(lr),
        "count_difference": None if rows is None else len(props) - len(rows),
        "note": "structural observation only; not a verdict, accuracy figure or evidence a reader was right or wrong",
    }


def _check_links_file(data) -> list:
    if isinstance(data, dict) and isinstance(data.get("links"), list):
        data = data["links"]
    if not isinstance(data, list):
        raise InputError("Link map must be a list (or an object with `links`) of "
                         "{record, proposal_index, row_index}.")
    return data


def build(readers: list[tuple[str | None, Path]], downstream: dict[str, Path] | None = None,
          links_path: Path | None = None, measure: bool = False) -> dict:
    records = [build_record(p, role, measure) for role, p in readers]
    ids = [r["record_id"] for r in records]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise InputError(f"Duplicate record_id (the same artifact supplied twice): {', '.join(dup)}.")

    def find(key: str) -> dict:
        hit = [r for r in records if key in (r["record_id"], Path(r["source"]["artifact_path"]).name)]
        if not hit:
            raise InputError(f"No supplied reader artifact matches key {key!r}.")
        if len(hit) > 1:
            raise InputError(f"Key {key!r} matches several reader artifacts; use the full record_id.")
        return hit[0]

    for key, path in (downstream or {}).items():
        rec = find(key)
        if rec["downstream_rows"] is not None:
            raise InputError(f"Two downstream snapshots were supplied for {key!r}.")
        rows, digest = load_rows(path)
        rec["downstream_rows"] = rows
        rec["metadata"]["downstream_snapshot"] = {"artifact_path": portable_path(path), "sha256": digest}
    if links_path is not None:
        _, data = _load_json(links_path, "Link map")
        grouped: dict[str, list] = {}
        for i, e in enumerate(_check_links_file(data)):
            key = e.get("record") if isinstance(e, dict) else None
            if not isinstance(key, str):
                raise InputError(f"Link map entry {i} names no `record`; it cannot be attached to a reader.")
            grouped.setdefault(find(key)["record_id"], []).append(
                {k: v for k, v in e.items() if k != "record"})
        for rid, entries in grouped.items():
            apply_links(find(rid), entries)
        for r in records:
            r["metadata"]["link_map"] = {"artifact_path": portable_path(links_path)}
    for r in records:
        r["metadata"]["structure"] = structure(r)
    return {"schema_version": SCHEMA_VERSION, "mode": "offline_replay", "records": records}


def _parse_reader(spec: str) -> tuple[str | None, Path]:
    head, sep, rest = spec.partition("=")
    if sep and head in ROLES:
        return head, Path(rest)
    return None, Path(spec)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reader", action="append", required=True, metavar="[ROLE=]PATH",
                    help="saved reader artifact; ROLE is first|second|unknown (default: artifact marker, else unknown)")
    ap.add_argument("--downstream", action="append", default=[], metavar="KEY=PATH",
                    help="downstream rows snapshot (JSON list or {rows:[...]}) for one reader artifact")
    ap.add_argument("--links", type=Path, help="explicit link map JSON: [{record,proposal_index,row_index},...]")
    ap.add_argument("--out", type=Path, help="write here (default: stdout)")
    ap.add_argument("--measure-elapsed", action="store_true", help="record this replay's own parse time")
    args = ap.parse_args(argv)
    try:
        ds = {}
        for spec in args.downstream:
            key, sep, path = spec.partition("=")
            if not sep or not key:
                raise InputError(f"--downstream {spec!r} is not KEY=PATH.")
            if key in ds:
                raise InputError(f"Two downstream snapshots were supplied for {key!r}.")
            ds[key] = Path(path)
        doc = build([_parse_reader(s) for s in args.reader], ds, args.links, args.measure_elapsed)
    except InputError as e:
        print(f"reader_baseline: {e}", file=sys.stderr)
        return 2
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for r in doc["records"]:
        s = r["metadata"]["structure"]
        print(f"{r['record_id']}: {r['reader_role']} {r['parse_status']} proposals={s['proposal_count']} "
              f"rows={s['downstream_row_count']} links={s['linked_pairs']} "
              f"link_issues={len(r['metadata'].get('link_issues', []))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
