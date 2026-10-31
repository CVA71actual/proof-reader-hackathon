"""Repair duplicate decision target IDs from an explicit, auditable run.json mapping.

Mapping format (zero-based list indexes):
  "id_repairs": {
    "targets": [{"path": ["rows", 0], "from": "e6", "to": "e13"}, ...],
    "references": [{"path": ["mistakes", 0, "target"], "from": "e6", "to": "e13"}, ...],
    "decisions": [{"line": 0, "from": "e6", "to": "e13"}, ...]
  }

Every duplicate target and every exclusion/mistake reference must be mapped. Decisions
referring to an ambiguous old ID must each have an explicit line mapping to one of the
targets mapped from that same old ID. The command never infers a mapping. A durable
journal keeps the run unserved until both files are rewritten; rerun with --write to finish.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from runs.ids import duplicate_target_ids


def _at_path(document, path):
    value = document
    for part in path:
        if isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        elif isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        else:
            raise ValueError(f"Invalid id_repairs path: {path!r}")
    return value


def _set_at_path(document, path, replacement) -> None:
    if not path:
        raise ValueError("A repair reference path cannot be empty")
    parent = _at_path(document, path[:-1])
    leaf = path[-1]
    if isinstance(parent, list) and isinstance(leaf, int) and 0 <= leaf < len(parent):
        parent[leaf] = replacement
    elif isinstance(parent, dict) and isinstance(leaf, str) and leaf in parent:
        parent[leaf] = replacement
    else:
        raise ValueError(f"Invalid id_repairs path: {path!r}")


def repair(run_dir: Path, *, write: bool = False) -> str:
    run_dir = Path(run_dir)
    run_path, decisions_path = run_dir / "run.json", run_dir / "decisions.jsonl"
    pending_path = run_dir / "id_repairs.pending.json"
    if pending_path.exists():
        if not write:
            return f"{run_dir.name}: an interrupted ID repair is pending; pass --write to complete it"
        _complete_pending(run_dir, pending_path)
        return f"{run_dir.name}: completed interrupted ID repair"
    run = json.loads(run_path.read_text())
    duplicate_ids = set(duplicate_target_ids(run))
    if not duplicate_ids:
        return f"{run_dir.name}: no duplicate target IDs; no changes needed"
    spec = run.get("id_repairs")
    if not isinstance(spec, dict) or not isinstance(spec.get("targets"), list) or not isinstance(spec.get("decisions"), list):
        raise ValueError("run.json must contain id_repairs.targets and id_repairs.decisions explicit mappings")
    if not isinstance(spec.get("references", []), list):
        raise ValueError("id_repairs.references must be a list")

    target_entries = spec["targets"]
    seen_paths, seen_new = set(), set()
    target_paths = set()
    for section, entries in (("rows", run.get("rows", [])), ("tables", run.get("tables", []))):
        if section == "rows":
            target_paths.update(("rows", i) for i, row in enumerate(entries)
                                if isinstance(row, dict) and row.get("id") in duplicate_ids)
        else:
            for ti, table in enumerate(entries):
                for ri, row in enumerate(table.get("rows", [])):
                    for ci, cell in enumerate(row.get("cells", [])):
                        if cell.get("id") in duplicate_ids:
                            target_paths.add(("tables", ti, "rows", ri, "cells", ci))
    targets_by_old: dict[str, set[str]] = {}
    for entry in target_entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "from", "to"}:
            raise ValueError("Each target mapping must contain only path, from, and to")
        path = entry["path"]
        if not isinstance(path, list) or not path or not isinstance(entry["from"], str) or not isinstance(entry["to"], str):
            raise ValueError("Target mapping path/from/to has the wrong type")
        key = tuple(path)
        if key in seen_paths:
            raise ValueError(f"Duplicate target mapping path: {path!r}")
        seen_paths.add(key)
        if key not in target_paths:
            raise ValueError(f"Mapping does not identify a duplicate target: {path!r}")
        obj = _at_path(run, path)
        if not isinstance(obj, dict) or obj.get("id") != entry["from"] or entry["from"] not in duplicate_ids:
            raise ValueError(f"Mapping source does not match target at {path!r}")
        if not entry["to"] or entry["to"] in seen_new:
            raise ValueError(f"New target ID must be nonempty and unique: {entry['to']!r}")
        seen_new.add(entry["to"])
        targets_by_old.setdefault(entry["from"], set()).add(entry["to"])
    if seen_paths != target_paths:
        missing = sorted(target_paths - seen_paths, key=repr)
        raise ValueError(f"Explicit mapping required for every duplicate target; missing {missing!r}")

    original_targets = {str(obj.get("id")) for section in (run.get("rows", []),)
                        for obj in section if isinstance(obj, dict)}
    for table in run.get("tables", []):
        original_targets.update(str(cell.get("id")) for row in table.get("rows", [])
                                for cell in row.get("cells", []) if isinstance(cell, dict))
    new_targets = (original_targets - duplicate_ids) | seen_new
    if len(new_targets) != len(original_targets - duplicate_ids) + len(seen_new):
        raise ValueError("A new ID conflicts with an existing target ID")

    required_references = set()
    for section in ("exclusions", "mistakes"):
        for index, item in enumerate(run.get(section, [])):
            if isinstance(item, dict) and item.get("target") in duplicate_ids:
                required_references.add((section, index, "target"))
    reference_entries = spec.get("references", [])
    seen_references = set()
    for entry in reference_entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "from", "to"}:
            raise ValueError("Each reference mapping must contain only path, from, and to")
        path = entry["path"]
        if not isinstance(path, list) or not path or not isinstance(entry["from"], str) or not isinstance(entry["to"], str):
            raise ValueError("Reference mapping path/from/to has the wrong type")
        key = tuple(path)
        if key in seen_references or key not in required_references:
            raise ValueError(f"Reference mapping does not identify one required ambiguous reference: {path!r}")
        seen_references.add(key)
        if _at_path(run, path) != entry["from"] or entry["from"] not in duplicate_ids:
            raise ValueError(f"Reference mapping source does not match at {path!r}")
        if entry["to"] not in targets_by_old.get(entry["from"], set()):
            raise ValueError(f"Reference at {path!r} must identify a repaired target from the same old ID")
    if seen_references != required_references:
        missing = sorted(required_references - seen_references, key=repr)
        raise ValueError(f"Explicit mapping required for every ambiguous exclusion/mistake reference; missing {missing!r}")

    decisions = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()] \
        if decisions_path.exists() else []
    decision_entries = {}
    for entry in spec["decisions"]:
        if not isinstance(entry, dict) or set(entry) != {"line", "from", "to"}:
            raise ValueError("Each decision mapping must contain only line, from, and to")
        line = entry["line"]
        if not isinstance(line, int) or isinstance(line, bool) or line < 0 or line >= len(decisions):
            raise ValueError(f"Decision mapping line is out of range: {line!r}")
        if line in decision_entries or decisions[line].get("target") != entry["from"]:
            raise ValueError(f"Decision mapping source does not match line {line}")
        if entry["from"] not in duplicate_ids or entry["to"] not in targets_by_old.get(entry["from"], set()):
            raise ValueError(f"Decision mapping line {line} does not map an ambiguous ID to a repaired target")
        decision_entries[line] = entry
    required_decisions = {i for i, decision in enumerate(decisions) if decision.get("target") in duplicate_ids}
    if set(decision_entries) != required_decisions:
        raise ValueError(f"Every decision on an ambiguous ID needs an explicit line mapping; missing lines {sorted(required_decisions - set(decision_entries))}")

    for entry in target_entries:
        _at_path(run, entry["path"])["id"] = entry["to"]
    for entry in reference_entries:
        _set_at_path(run, entry["path"], entry["to"])
    for line, entry in decision_entries.items():
        decisions[line]["target"] = entry["to"]
    if duplicate_target_ids(run):
        raise ValueError("Repair mapping leaves duplicate target IDs")
    run["report_stale"] = True
    run["report_stale_reason"] = "This run's target IDs were repaired; rebuild saved reports from the current state."
    if (run_dir / "accuracy.json").is_file():
        run["accuracy_stale"] = True
        run["accuracy_stale_reason"] = "This run's target IDs were repaired; rescore before serving accuracy results."
    run["id_repairs"] = {"applied": True, "targets": target_entries,
                         "references": reference_entries, "decisions": spec["decisions"]}
    rendered_run = json.dumps(run, ensure_ascii=False, indent=2) + "\n"
    rendered_decisions = "".join(json.dumps(decision, ensure_ascii=False) + "\n" for decision in decisions)
    if not write:
        return (f"{run_dir.name}: would repair {len(target_entries)} targets and "
                f"{len(decision_entries)} decisions; pass --write to save")

    _write_pending(pending_path, {
        "run_json": rendered_run,
        "decisions_jsonl": rendered_decisions if decisions_path.exists() else None,
    })
    _complete_pending(run_dir, pending_path)
    return f"{run_dir.name}: repaired {len(target_entries)} targets and {len(decision_entries)} decisions"


def _write_pending(path: Path, payload: dict) -> None:
    _replace_atomically(path, json.dumps(payload, ensure_ascii=False))


def _complete_pending(run_dir: Path, pending_path: Path) -> None:
    payload = json.loads(pending_path.read_text())
    if not isinstance(payload.get("run_json"), str) or payload.get("decisions_jsonl") is not None and not isinstance(payload["decisions_jsonl"], str):
        raise ValueError("Interrupted ID repair journal is invalid; run remains refused")
    _replace_atomically(run_dir / "decisions.jsonl", payload["decisions_jsonl"]) if payload["decisions_jsonl"] is not None else None
    _replace_atomically(run_dir / "run.json", payload["run_json"])
    pending_path.unlink()


def _replace_atomically(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="run directory, or run ID under runs/")
    parser.add_argument("--write", action="store_true", help="persist the validated repair; default is dry-run")
    args = parser.parse_args(argv)
    path = Path(args.run)
    if not path.is_dir():
        path = Path(__file__).resolve().parent / path
    print(repair(path, write=args.write))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
