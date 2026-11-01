#!/usr/bin/env python3
"""Offline structural compare: reader proposals vs downstream retained rows.

Consumes shared offline_replay JSON interface v1. Does not call models, does not
guess proposal↔row links, and passes no judgment on content. Archived replay
only — not evidence that a revised prompt or pivot works.

Usage (from repo root):
    PYTHONPATH=. python tools/reader_compare.py path/to/records.json --out compare.md [--format md|json]

With no --out the report goes to stdout. Nothing is ever written anywhere but --out.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MODE = "offline_replay"
PARSE_STATUSES = frozenset({"ok", "empty", "malformed", "error", "unavailable"})
READER_ROLES = frozenset({"first", "second", "unknown"})
# Fields the contract says must survive where supplied (structural, not semantic).
SURVIVAL_FIELDS = (
    "exact_text",
    "conditions",
    "relevance",
    "subject_company",
    "relation",
    "type_hint",
)
LIMITATIONS = [
    "Archived offline replay only: not a live reader test and not evidence a revised prompt or pivot works.",
    "Links must be explicit {proposal_index, row_index}; no pairing by array order, ticker, quote, or name.",
    "Field differences are structural observations only — not a verdict on either side and not a quality measure.",
    "Missing prompt, raw_response, hashes, or links stay explicitly missing; nothing is reconstructed.",
    "downstream_rows null means unavailable; [] means a supplied empty snapshot — these stay distinct.",
    "malformed, empty, error, and unavailable parse_status values stay separate from ok.",
    "Human judgment columns are blank for a person to fill; this tool states no verdict on any proposal or row.",
    "Unknown recorded cost/time stays null; replay_elapsed_ms is wall time of this compare only.",
    "Only parse_status ok records are compared; for any other status the reader returned nothing usable (or it was not saved), so no proposal is paired and any downstream rows stay unmatched.",
]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def _values_equal(a: Any, b: Any) -> bool:
    return _jsonable(a) == _jsonable(b)


def _present(obj: dict, key: str) -> bool:
    return key in obj


def downstream_status(rows: Any) -> str:
    if rows is None:
        return "unavailable"
    if isinstance(rows, list) and len(rows) == 0:
        return "empty"
    if isinstance(rows, list):
        return "present"
    return "invalid"


def validate_envelope(doc: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["input is not a JSON object"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if doc.get("mode") != MODE:
        errors.append(f"mode must be {MODE!r}")
    if not isinstance(doc.get("records"), list):
        errors.append("records must be a list")
    return errors


def _normalize_source(source: Any) -> dict:
    if not isinstance(source, dict):
        return {"artifact_path": None, "sha256": None, "page": None}
    return {
        "artifact_path": source.get("artifact_path", None),
        "sha256": source.get("sha256", None),
        "page": source.get("page", None),
    }


def classify_links(
    links: Any,
    n_proposals: int,
    rows: list | None,
) -> tuple[list[dict], list[dict]]:
    """Return (accepted_pairs, link_issues). Conflicting/invalid links are reported and not applied."""
    issues: list[dict] = []
    if links is None:
        links = []
    if not isinstance(links, list):
        return [], [{"kind": "malformed_links_container", "detail": "links is not a list", "link": links}]

    candidates: list[dict] = []
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            issues.append({"kind": "malformed_link", "detail": "link is not an object", "link_index": i, "link": link})
            continue
        pi, ri = link.get("proposal_index"), link.get("row_index")
        if not _is_int(pi) or not _is_int(ri):
            issues.append({
                "kind": "malformed_link",
                "detail": "proposal_index and row_index must be integers",
                "link_index": i,
                "link": link,
            })
            continue
        if pi < 0 or pi >= n_proposals:
            issues.append({
                "kind": "out_of_bounds",
                "detail": f"proposal_index {pi} outside 0..{max(n_proposals - 1, 0)}",
                "link_index": i,
                "link": {"proposal_index": pi, "row_index": ri},
            })
            continue
        if rows is None:
            issues.append({
                "kind": "row_snapshot_unavailable",
                "detail": "downstream_rows is null; row links cannot be applied",
                "link_index": i,
                "link": {"proposal_index": pi, "row_index": ri},
            })
            continue
        if ri < 0 or ri >= len(rows):
            issues.append({
                "kind": "out_of_bounds",
                "detail": f"row_index {ri} outside 0..{max(len(rows) - 1, 0)}",
                "link_index": i,
                "link": {"proposal_index": pi, "row_index": ri},
            })
            continue
        candidates.append({"proposal_index": pi, "row_index": ri, "link_index": i})

    by_prop: dict[int, list[dict]] = {}
    by_row: dict[int, list[dict]] = {}
    for c in candidates:
        by_prop.setdefault(c["proposal_index"], []).append(c)
        by_row.setdefault(c["row_index"], []).append(c)

    conflicted: set[int] = set()
    for pi, group in by_prop.items():
        if len(group) > 1:
            issues.append({
                "kind": "conflicting_link",
                "detail": f"proposal_index {pi} linked more than once",
                "links": [{"proposal_index": g["proposal_index"], "row_index": g["row_index"],
                           "link_index": g["link_index"]} for g in group],
            })
            conflicted.update(g["link_index"] for g in group)
    for ri, group in by_row.items():
        if len(group) > 1:
            issues.append({
                "kind": "conflicting_link",
                "detail": f"row_index {ri} linked more than once",
                "links": [{"proposal_index": g["proposal_index"], "row_index": g["row_index"],
                           "link_index": g["link_index"]} for g in group],
            })
            conflicted.update(g["link_index"] for g in group)

    accepted = [c for c in candidates if c["link_index"] not in conflicted]
    pairs = [{"proposal_index": c["proposal_index"], "row_index": c["row_index"]} for c in accepted]
    return pairs, issues


def compare_objects(proposal: dict, row: dict) -> list[dict]:
    """Structural field observations between one proposal and one linked downstream row."""
    keys = sorted(set(proposal) | set(row), key=lambda k: (k not in SURVIVAL_FIELDS, k))
    observations: list[dict] = []
    for key in keys:
        in_p, in_r = _present(proposal, key), _present(row, key)
        pv = proposal.get(key) if in_p else None
        rv = row.get(key) if in_r else None
        if in_p and not in_r:
            obs = "dropped"
        elif in_r and not in_p:
            obs = "row_only"
        elif _values_equal(pv, rv):
            obs = "unchanged"
        else:
            obs = "changed"
        observations.append({
            "field": key,
            "observation": obs,
            "proposal_value": pv if in_p else None,
            "row_value": rv if in_r else None,
            "in_proposal": in_p,
            "in_row": in_r,
            "survival_field": key in SURVIVAL_FIELDS,
        })
    return observations


def compare_record(rec: dict) -> dict:
    record_id = rec.get("record_id")
    source = _normalize_source(rec.get("source"))
    role = rec.get("reader_role") or "unknown"
    if role not in READER_ROLES:
        role = "unknown"
    parse_status = rec.get("parse_status")
    if parse_status not in PARSE_STATUSES:
        parse_status_note = f"unrecognized parse_status {parse_status!r}; treated as unknown"
        parse_status_norm = "unknown"
    else:
        parse_status_note = None
        parse_status_norm = parse_status

    proposals = rec.get("proposals")
    if proposals is None:
        proposals = []
    if not isinstance(proposals, list):
        proposals = []
        proposal_container_issue = "proposals is not a list; treated as empty for pairing"
    else:
        proposal_container_issue = None

    rows = rec.get("downstream_rows")
    rows_status = downstream_status(rows)
    rows_list: list | None
    if rows_status == "present":
        rows_list = list(rows)
    elif rows_status == "empty":
        rows_list = []
    else:
        rows_list = None

    comparable = parse_status == "ok"
    if comparable:
        pairs, link_issues = classify_links(rec.get("links"), len(proposals), rows_list)
    else:
        # missing/failed/empty/malformed reader output is never compared; nothing is paired
        pairs, link_issues = [], []
        if proposals or rec.get("links"):
            link_issues = [{
                "kind": "not_compared",
                "detail": f"parse_status is {parse_status_norm!r}; supplied proposals/links are not used",
            }]
        proposals = []
    if proposal_container_issue:
        link_issues = [{"kind": "malformed_proposals", "detail": proposal_container_issue}, *link_issues]
    if parse_status_note:
        link_issues = [{"kind": "unknown_parse_status", "detail": parse_status_note}, *link_issues]
    if rows_status == "invalid":
        link_issues = [{
            "kind": "invalid_downstream_rows",
            "detail": "downstream_rows must be null or a list",
        }, *link_issues]

    linked_p = {p["proposal_index"] for p in pairs}
    linked_r = {p["row_index"] for p in pairs}

    pair_reports = []
    for p in pairs:
        raw_prop = proposals[p["proposal_index"]]
        raw_row = rows_list[p["row_index"]]
        prop_obj = raw_prop if isinstance(raw_prop, dict) else {}
        row_obj = raw_row if isinstance(raw_row, dict) else {}
        observations = compare_objects(prop_obj, row_obj)
        if not isinstance(raw_prop, dict):
            observations.insert(0, {
                "field": "_proposal",
                "observation": "malformed",
                "proposal_value": raw_prop,
                "row_value": None,
                "in_proposal": True,
                "in_row": False,
                "survival_field": False,
            })
        if not isinstance(raw_row, dict):
            observations.insert(0, {
                "field": "_downstream_row",
                "observation": "malformed",
                "proposal_value": None,
                "row_value": raw_row,
                "in_proposal": False,
                "in_row": True,
                "survival_field": False,
            })
        pair_reports.append({
            "proposal_index": p["proposal_index"],
            "row_index": p["row_index"],
            "proposal": deepcopy(raw_prop),
            "downstream_row": deepcopy(raw_row),
            "field_observations": observations,
            "human_judgment": "",
            "human_notes": "",
        })

    unmatched_proposals = []
    for i, prop in enumerate(proposals):
        if i in linked_p:
            continue
        unmatched_proposals.append({
            "proposal_index": i,
            "proposal": deepcopy(prop),
            "reason": "no_valid_explicit_link",
            "human_judgment": "",
            "human_notes": "",
        })

    unmatched_rows = []
    if rows_list is not None:
        for i, row in enumerate(rows_list):
            if i in linked_r:
                continue
            unmatched_rows.append({
                "row_index": i,
                "downstream_row": deepcopy(row),
                "reason": "no_valid_explicit_link",
                "human_judgment": "",
                "human_notes": "",
            })

    dropped = changed = unchanged_survival = 0
    for pr in pair_reports:
        for obs in pr["field_observations"]:
            if obs["observation"] == "dropped":
                dropped += 1
            elif obs["observation"] == "changed":
                changed += 1
            elif obs["observation"] == "unchanged" and obs.get("survival_field"):
                unchanged_survival += 1

    meta_in = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
    recorded = {
        # reader_baseline.py writes recorded_timing_cost; unknown stays null
        "cost_usd": (meta_in.get("recorded_timing_cost") or {}).get("cost_usd") if isinstance(meta_in.get("recorded_timing_cost"), dict) else None,
        "elapsed_seconds": (meta_in.get("recorded_timing_cost") or {}).get("elapsed_seconds") if isinstance(meta_in.get("recorded_timing_cost"), dict) else None,
    }

    return {
        "record_id": record_id,
        "source": source,
        "reader_role": role,
        "model": rec.get("model", None),
        "prompt": rec.get("prompt", None),
        "raw_response": rec.get("raw_response", None),
        "parse_status": parse_status if parse_status_norm != "unknown" else parse_status,
        "parse_status_bucket": parse_status_norm,
        "comparison_status": "compared" if comparable else f"not_compared_{parse_status_norm}",
        "parse_error": rec.get("parse_error", None),
        "downstream_rows_status": rows_status if rows_status != "invalid" else "unavailable",
        "link_issues": link_issues,
        "pairs": pair_reports,
        "unmatched_proposals": unmatched_proposals,
        "unmatched_rows": unmatched_rows,
        "counts": {
            "proposals": len(proposals),
            "downstream_rows": None if rows_list is None else len(rows_list),
            "linked_pairs": len(pair_reports),
            "unmatched_proposals": len(unmatched_proposals),
            "unmatched_rows": len(unmatched_rows),
            "link_issues": len(link_issues),
            "dropped_field_observations": dropped,
            "changed_field_observations": changed,
            "unchanged_survival_field_observations": unchanged_survival,
        },
        "recorded_metadata": recorded,
        "metadata": deepcopy(meta_in),
    }


def compare_document(doc: dict, *, replay_elapsed_ms: int | None = None) -> dict:
    errors = validate_envelope(doc)
    if errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": "reader_compare",
            "mode": MODE,
            "ok": False,
            "errors": errors,
            "limitations": list(LIMITATIONS),
            "replay": {"elapsed_ms": replay_elapsed_ms, "recorded_cost": None},
            "records": [],
        }
    records = []
    for r in doc["records"]:
        if isinstance(r, dict):
            records.append(compare_record(r))
        else:
            records.append(compare_record({
                "record_id": None,
                "proposals": [],
                "downstream_rows": None,
                "links": [],
                "parse_status": "error",
                "parse_error": "record is not an object",
            }))
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "reader_compare",
        "mode": MODE,
        "ok": True,
        "errors": [],
        "limitations": list(LIMITATIONS),
        "replay": {"elapsed_ms": replay_elapsed_ms, "recorded_cost": None},
        "records": records,
        "summary_counts": {
            "records": len(records),
            "linked_pairs": sum(r["counts"]["linked_pairs"] for r in records),
            "unmatched_proposals": sum(r["counts"]["unmatched_proposals"] for r in records),
            "unmatched_rows": sum(r["counts"]["unmatched_rows"] for r in records),
            "link_issues": sum(r["counts"]["link_issues"] for r in records),
            "dropped_field_observations": sum(r["counts"]["dropped_field_observations"] for r in records),
            "changed_field_observations": sum(r["counts"]["changed_field_observations"] for r in records),
        },
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Reader compare (offline replay)")
    lines.append("")
    lines.append(
        "Structural comparison of supplied reader proposals against explicitly linked "
        "downstream rows. No verdict on content. Not evidence a pivot works."
    )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for item in report.get("limitations") or LIMITATIONS:
        lines.append(f"- {item}")
    lines.append("")
    replay = report.get("replay") or {}
    lines.append("## Replay clock")
    lines.append("")
    lines.append(f"- replay_elapsed_ms: {replay.get('elapsed_ms')}")
    lines.append(f"- recorded_cost (unknown stays null): {replay.get('recorded_cost')}")
    lines.append("")
    if not report.get("ok"):
        lines.append("## Envelope errors")
        lines.append("")
        for e in report.get("errors") or []:
            lines.append(f"- {e}")
        lines.append("")
        return "\n".join(lines) + "\n"

    summary = report.get("summary_counts") or {}
    lines.append("## Summary counts (structural)")
    lines.append("")
    for k in (
        "records",
        "linked_pairs",
        "unmatched_proposals",
        "unmatched_rows",
        "link_issues",
        "dropped_field_observations",
        "changed_field_observations",
    ):
        lines.append(f"- {k}: {summary.get(k)}")
    lines.append("")

    for rec in report.get("records") or []:
        lines.append(f"## Record `{rec.get('record_id')}`")
        lines.append("")
        src = rec.get("source") or {}
        lines.append("### Identities")
        lines.append("")
        lines.append(f"- reader_role: {rec.get('reader_role')}")
        lines.append(f"- model: {rec.get('model')}")
        lines.append(f"- source.artifact_path: {src.get('artifact_path')}")
        lines.append(f"- source.sha256: {src.get('sha256')}")
        lines.append(f"- source.page: {src.get('page')}")
        lines.append(f"- parse_status: {rec.get('parse_status')}")
        lines.append(f"- parse_error: {rec.get('parse_error')}")
        lines.append(f"- downstream_rows_status: {rec.get('downstream_rows_status')}")
        recorded = rec.get("recorded_metadata") or {}
        lines.append(f"- comparison_status: {rec.get('comparison_status')}")
        lines.append(f"- recorded cost_usd: {recorded.get('cost_usd')}")
        lines.append(f"- recorded elapsed_seconds: {recorded.get('elapsed_seconds')}")
        lines.append("")
        lines.append("Human judgment columns (`human_judgment`, `human_notes`) are left blank for a person to fill.")
        lines.append("")
        lines.append("### Raw response (exact)")
        lines.append("")
        raw = rec.get("raw_response")
        if raw is None:
            lines.append("_missing_")
        else:
            lines.append("```")
            lines.append(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
            lines.append("```")
        lines.append("")
        lines.append("### Prompt")
        lines.append("")
        prompt = rec.get("prompt")
        if prompt is None:
            lines.append("_missing_")
        else:
            lines.append("```")
            lines.append(prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False))
            lines.append("```")
        lines.append("")

        if rec.get("link_issues"):
            lines.append("### Link issues (reported, not guessed)")
            lines.append("")
            for issue in rec["link_issues"]:
                lines.append(f"- **{issue.get('kind')}**: {issue.get('detail')}")
            lines.append("")

        lines.append("### Linked pairs (proposal || downstream row)")
        lines.append("")
        if not rec.get("pairs"):
            lines.append("_none_")
            lines.append("")
        for pair in rec.get("pairs") or []:
            lines.append(f"#### Pair proposal[{pair['proposal_index']}] <-> row[{pair['row_index']}]")
            lines.append("")
            lines.append("| side | JSON |")
            lines.append("| --- | --- |")
            lines.append(f"| proposal | `{json.dumps(pair.get('proposal'), ensure_ascii=False)}` |")
            lines.append(
                f"| downstream_row | `{json.dumps(pair.get('downstream_row'), ensure_ascii=False)}` |"
            )
            lines.append("")
            lines.append("| field | observation | proposal_value | row_value | survival_field |")
            lines.append("| --- | --- | --- | --- | --- |")
            for obs in pair.get("field_observations") or []:
                lines.append(
                    f"| {obs['field']} | {obs['observation']} | "
                    f"`{json.dumps(obs.get('proposal_value'), ensure_ascii=False)}` | "
                    f"`{json.dumps(obs.get('row_value'), ensure_ascii=False)}` | "
                    f"{obs.get('survival_field')} |"
                )
            lines.append("")
            lines.append(
                f"- human_judgment: {pair.get('human_judgment')!r} | "
                f"human_notes: {pair.get('human_notes')!r}"
            )
            lines.append("")

        lines.append("### Unmatched proposals")
        lines.append("")
        if not rec.get("unmatched_proposals"):
            lines.append("_none_")
        else:
            for u in rec["unmatched_proposals"]:
                lines.append(
                    f"- proposal[{u['proposal_index']}] reason={u['reason']}: "
                    f"`{json.dumps(u.get('proposal'), ensure_ascii=False)}` "
                    f"| human_judgment={u.get('human_judgment')!r} | "
                    f"human_notes={u.get('human_notes')!r}"
                )
        lines.append("")
        lines.append("### Unmatched downstream rows")
        lines.append("")
        if rec.get("downstream_rows_status") == "unavailable":
            lines.append("_snapshot unavailable (null) — no row list to leave unmatched_")
        elif not rec.get("unmatched_rows"):
            lines.append("_none_")
        else:
            for u in rec["unmatched_rows"]:
                lines.append(
                    f"- row[{u['row_index']}] reason={u['reason']}: "
                    f"`{json.dumps(u.get('downstream_row'), ensure_ascii=False)}` "
                    f"| human_judgment={u.get('human_judgment')!r} | "
                    f"human_notes={u.get('human_notes')!r}"
                )
        lines.append("")
        counts = rec.get("counts") or {}
        lines.append("### Structural counts")
        lines.append("")
        for k, v in counts.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines) + "\n"


def load_input(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline reader proposal vs downstream row compare.")
    parser.add_argument("input", type=Path, help="Shared offline_replay JSON document")
    parser.add_argument("--out", type=Path, default=None, help="Write the report here (default: stdout)")
    parser.add_argument("--format", choices=("md", "json"), default="md", help="Report format (default: md)")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        doc = load_input(args.input)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"reader_compare: cannot read input: {type(e).__name__}", file=sys.stderr)
        return 2
    report = compare_document(doc, replay_elapsed_ms=None)
    report["replay"]["elapsed_ms"] = int(round((time.perf_counter() - started) * 1000))

    if args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_markdown(report)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
