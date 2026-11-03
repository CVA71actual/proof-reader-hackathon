"""Offline benchmark scorer for Proof Reader research runs.

    python -m tools.bench_score <run_dir> <bench_file> [more bench files] [--json out.json]

Scores one saved run folder (run.json + decisions.jsonl) against a per-ticker benchmark file of expected catalysts
(tests/fixtures/bench/ptgx.yaml). No model, network or paid call: the run is loaded through the same code the UI
and the exports use (runs.check_load -> runs.state.current_state -> runs.calendar.run_interval_partition), so the
scorer sees exactly the rows, values and placements a person sees. The run folder is only read, never written.

What a result means. A benchmark item is matched to run rows by KEYWORD/ALIAS OVERLAP on the row's title + summary
(relevance) + program + action, and the match is then compared on TIMING (period overlap) and on PLACEMENT CLASS.
This is a keyword+timing match, not a judgement that the row is correct: two rows can share every keyword and say
different things. Every match prints the words it matched and its timing comparison so a person can see why.

Benchmark file (YAML or JSON):
    ticker: PTGX
    items:
      - id: pn881-ph2b
        description: PN-881 Phase 2b psoriasis initiation
        keywords: [[PN-881, PN881], [Phase 2b, Phase 2], psoriasis]   # alias groups; a group hits on any alias
        min_groups: 2                  # groups that must hit (default: every group)
        timing: Q1 2027                # ISO date, 'Qn YYYY', 'Hn YYYY'/'nH YYYY', 'Month YYYY', 'Month D, YYYY',
                                       # 'YYYY', 'undated', or {start, end, text}; '~' prefix allows 14 days' slack
        slack_days: 0                  # optional extra tolerance either side of the expected timing
        placement: upcoming            # upcoming | context | later | undated

Categories (per ticker and in total):
    found        keyword match, timing compatible, placement class as expected
    misplaced    keyword match, but the timing does not overlap or the placement class differs (a row the calendar
                 does not place, because it needs a person's review or was excluded, is misplaced too)
    missed       no run row matches the item's keywords
    duplicates   two or more run rows match the same benchmark item, or rows whose normalized program + action +
                 period coincide
    unmatched    run rows that match no benchmark item (extra or unknown)
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from runs.calendar import run_interval_partition          # noqa: E402
from runs.check_load import bind_decisions_on_load, load_bound_run   # noqa: E402
from runs.state import current_state                      # noqa: E402

DISCLAIMER = ("Keyword+timing match only. A 'found' row shares the benchmark item's keywords and overlaps its expected "
              "timing; it is not a judgement that the row's meaning is correct.")
TEXT_FIELDS = ("title", "summary", "program", "action")
BENCH_PLACEMENTS = ("upcoming", "context", "later", "undated")
_DEFAULT_APPROX_SLACK_DAYS = 14
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9

# How a row's state becomes one placement class, the words of the screens (runs.review.review_projection).
_INCLUSION_CLASS = {"included": "upcoming", "context": "context", "later": "later", "undated": "undated",
                    "review": "review", "excluded": "excluded", "unplaced": "unplaced"}
_BUCKET_CLASS = {"calendar": "upcoming", "past": "context", "later": "later", "contingent": "undated",
                 "undated": "undated", "timing_unresolved": "undated"}


# ----------------------------------------------------------------------------------------------- normalization

def normalize(text: str | None) -> str:
    """Lowercase; 'J&J' -> 'j and j'; hyphens inside a token are dropped (PN-881 -> pn881); everything else that is
    not a letter or digit becomes a space. The same function normalizes keywords and row text, so a phrase
    match is an exact match of whole normalized words."""
    s = (text or "").lower().replace("&", " and ")
    s = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def phrase_in(phrase: str, text: str) -> bool:
    p, t = normalize(phrase), normalize(text)
    return bool(p) and f" {p} " in f" {t} "


# ----------------------------------------------------------------------------------------------------- timing

def _last_day(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_timing(spec, default_slack: int = 0) -> dict:
    """{"kind": "dated"|"undated", "start": date|None, "end": date|None, "text": str, "slack_days": int}."""
    slack = default_slack
    if isinstance(spec, dict):
        text = spec.get("text") or spec.get("date") or f"{spec.get('start')} to {spec.get('end')}"
        slack = int(spec.get("slack_days", slack))
        if spec.get("date"):
            d = date.fromisoformat(str(spec["date"]))
            return {"kind": "dated", "start": d, "end": d, "text": str(text), "slack_days": slack}
        if spec.get("start") or spec.get("end"):
            start = date.fromisoformat(str(spec.get("start") or spec["end"]))
            end = date.fromisoformat(str(spec.get("end") or spec["start"]))
            return {"kind": "dated", "start": start, "end": end, "text": str(text), "slack_days": slack}
        spec = spec.get("period") or "undated"
    raw = str(spec or "undated").strip()
    text = raw
    if raw.startswith("~"):
        raw = raw[1:].strip()
        slack = max(slack, _DEFAULT_APPROX_SLACK_DAYS)
    low = raw.lower()
    if low in ("", "undated", "none", "tbd"):
        return {"kind": "undated", "start": None, "end": None, "text": text, "slack_days": slack}
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        d = date.fromisoformat(low)
        return {"kind": "dated", "start": d, "end": d, "text": text, "slack_days": slack}
    m = re.fullmatch(r"(?:q([1-4])\s*(\d{4})|(\d{4})\s*q([1-4]))", low)
    if m:
        q, y = int(m.group(1) or m.group(4)), int(m.group(2) or m.group(3))
        start_m = 3 * (q - 1) + 1
        return {"kind": "dated", "start": date(y, start_m, 1), "end": _last_day(y, start_m + 2), "text": text,
                "slack_days": slack}
    m = re.fullmatch(r"(?:([12])h\s*(\d{4})|h([12])\s*(\d{4}))", low)
    if m:
        h, y = int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))
        return {"kind": "dated", "start": date(y, 1 if h == 1 else 7, 1), "end": _last_day(y, 6 if h == 1 else 12),
                "text": text, "slack_days": slack}
    m = re.fullmatch(r"(\d{4})", low)
    if m:
        y = int(m.group(1))
        return {"kind": "dated", "start": date(y, 1, 1), "end": date(y, 12, 31), "text": text, "slack_days": slack}
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", low)
    if m and m.group(1) in _MONTHS:
        d = date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        return {"kind": "dated", "start": d, "end": d, "text": text, "slack_days": slack}
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", low)
    if m and m.group(1) in _MONTHS:
        y, mo = int(m.group(2)), _MONTHS[m.group(1)]
        return {"kind": "dated", "start": date(y, mo, 1), "end": _last_day(y, mo), "text": text, "slack_days": slack}
    raise ValueError(f"benchmark timing not understood: {spec!r} (use an ISO date, 'Q1 2027', '1H 2027', "
                     f"'December 2026', 'December 8, 2026', '2027', 'undated', or {{start, end, text}})")


def row_timing(values: dict, display: str | None) -> dict:
    """The row's effective period from the state's current values (a person's fix and the loader's derived period
    bounds applied): [date, date_end or date]; undated when the state holds no date."""
    start = values.get("date")
    if not start:
        return {"kind": "undated", "start": None, "end": None, "text": display or values.get("date_text") or "undated"}
    s = date.fromisoformat(str(start)[:10])
    e = date.fromisoformat(str(values.get("date_end") or start)[:10])
    if e < s:
        e = s
    return {"kind": "dated", "start": s, "end": e, "text": display or values.get("date_text") or f"{s} to {e}"}


def compare_timing(expected: dict, actual: dict) -> dict:
    """{"compatible": bool, "explanation": str}: period overlap with the item's slack; an undated expectation
    constrains nothing; a dated expectation needs a dated row."""
    if expected["kind"] == "undated":
        return {"compatible": True, "explanation": f"expected undated; row {actual['text']!r}: not constrained"}
    exp = f"expected {expected['start']}..{expected['end']} ({expected['text']})"
    slack = int(expected.get("slack_days") or 0)
    if actual["kind"] == "undated":
        return {"compatible": False, "explanation": f"{exp}; row has no date ({actual['text']!r}): no overlap"}
    pad = timedelta(days=slack)
    overlap = actual["start"] - pad <= expected["end"] and actual["end"] + pad >= expected["start"]
    slack_note = f", slack {slack}d" if slack else ""
    return {"compatible": overlap,
            "explanation": f"{exp}; row {actual['start']}..{actual['end']} ({actual['text']}): "
                           f"{'overlap' if overlap else 'no overlap'}{slack_note}"}


# ------------------------------------------------------------------------------------------------ the run

def load_run(run_dir: Path) -> tuple[dict, dict, dict, tuple[str | None, str | None]]:
    """The run as the UI and exports see it. Reads only; the loaders bind checks in memory."""
    run_dir = Path(run_dir)
    run = load_bound_run(run_dir)
    f = run_dir / "decisions.jsonl"
    decisions = [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.is_file() else []
    decisions = bind_decisions_on_load(run, decisions, run_dir)
    state = current_state(run, decisions)
    partition, start, end = run_interval_partition(run, state)
    return run, state, partition, (start, end)


def placement_class(target: dict, bucket: str | None) -> str:
    """One word for where the screens put the row: kept rows by the partition's bucket, clean rows by their
    inclusion; review / excluded / unplaced rows are not on the calendar."""
    inclusion = target.get("inclusion")
    if inclusion == "kept":
        return _BUCKET_CLASS.get(bucket or target.get("placement") or "", "unplaced")
    return _INCLUSION_CLASS.get(inclusion or "", "unplaced")


def run_rows(run: dict, state: dict, partition: dict) -> list[dict]:
    bucket_of = {e["id"]: b for b, entries in partition.items() for e in entries}
    rows_by_id = {r.get("id"): r for r in run.get("rows", [])}
    out = []
    for rid, t in state["targets"].items():
        if t.get("kind") != "row":
            continue
        raw = rows_by_id.get(rid) or {}
        v = t.get("values") or {}
        research = raw.get("research") if isinstance(raw.get("research"), dict) else {}
        fields = {"title": v.get("title") or raw.get("title"),
                  "summary": raw.get("summary") or v.get("relevance") or raw.get("relevance"),
                  "program": v.get("program") or raw.get("program"),
                  "action": research.get("action")}
        text = " | ".join(str(fields[k]) for k in TEXT_FIELDS if fields.get(k))
        row = {"id": rid, "ticker": (v.get("ticker") or raw.get("ticker") or "").upper(),
               **fields, "text": text, "date": v.get("date"), "date_end": v.get("date_end"),
               "date_display": t.get("date_display"), "inclusion": t.get("inclusion"),
               "bucket": bucket_of.get(rid), "placement_proposal": research.get("placement_proposal"),
               "decision_label": t.get("decision_label"), "review_label": t.get("review_label")}
        row["placement_class"] = placement_class(t, row["bucket"])
        row["timing"] = row_timing(v, t.get("date_display"))
        out.append(row)
    out.sort(key=lambda r: (r["ticker"], _row_order(r["id"])))
    return out


def _row_order(rid: str):
    m = re.fullmatch(r"([a-z]*)(\d+)", str(rid))
    return (m.group(1), int(m.group(2))) if m else (str(rid), 0)


# --------------------------------------------------------------------------------------------- the benchmark

def load_bench(path: Path) -> dict:
    path = Path(path)
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        doc = yaml.safe_load(text)
    else:
        doc = json.loads(text)
    if not isinstance(doc, dict) or not doc.get("ticker") or not isinstance(doc.get("items"), list):
        raise ValueError(f"{path}: a benchmark file is a mapping with 'ticker' and a list 'items'")
    items = []
    seen = set()
    for raw in doc["items"]:
        item = dict(raw)
        if not item.get("id") or item["id"] in seen:
            raise ValueError(f"{path}: every item needs a unique id (problem near {raw!r})")
        seen.add(item["id"])
        groups = [[g] if not isinstance(g, list) else list(g) for g in (item.get("keywords") or [])]
        groups = [[str(a) if isinstance(a, (int, float)) and not isinstance(a, bool) else a for a in g] for g in groups]
        if not groups or any(not g or any(not isinstance(a, str) or not a.strip() for a in g) for g in groups):
            raise ValueError(f"{path}: item {item['id']} needs keywords (strings or lists of alias strings)")
        item["keywords"] = groups
        item["min_groups"] = int(item.get("min_groups") or len(groups))
        if not 1 <= item["min_groups"] <= len(groups):
            raise ValueError(f"{path}: item {item['id']}: min_groups must be between 1 and {len(groups)}")
        item["timing_parsed"] = parse_timing(item.get("timing", "undated"), int(item.get("slack_days") or 0))
        item["placement"] = str(item.get("placement") or ("undated" if item["timing_parsed"]["kind"] == "undated"
                                                              else "upcoming")).lower()
        if item["placement"] not in BENCH_PLACEMENTS:
            raise ValueError(f"{path}: item {item['id']}: placement must be one of {BENCH_PLACEMENTS}")
        items.append(item)
    return {"ticker": str(doc["ticker"]).upper(), "label": doc.get("label"), "file": str(path), "items": items}


# ------------------------------------------------------------------------------------------------ matching

def keyword_hits(item: dict, row: dict) -> tuple[list[str], list[str]]:
    """(matched aliases, one per hit group; the names of groups that missed)."""
    matched, missed = [], []
    for group in item["keywords"]:
        hit = next((alias for alias in group if phrase_in(alias, row["text"])), None)
        (matched.append(hit) if hit else missed.append(group[0]))
    return matched, missed


def score_item(item: dict, rows: list[dict]) -> dict:
    candidates = []
    for row in rows:
        matched, missed = keyword_hits(item, row)
        if len(matched) < item["min_groups"]:
            continue
        timing = compare_timing(item["timing_parsed"], row["timing"])
        placement_ok = row["placement_class"] == item["placement"]
        candidates.append({
            "row_id": row["id"], "title": row["title"], "program": row["program"], "action": row["action"],
            "matched_words": matched, "unmatched_groups": missed, "groups_hit": f"{len(matched)}/{len(item['keywords'])}",
            "searched": row["text"], "timing_compatible": timing["compatible"], "timing": timing["explanation"],
            "row_placement": row["placement_class"], "expected_placement": item["placement"],
            "placement_ok": placement_ok, "date_display": row["date_display"],
        })
    candidates.sort(key=lambda c: (-len(c["matched_words"]), not c["timing_compatible"], not c["placement_ok"],
                                   _row_order(c["row_id"])))
    result = {"id": item["id"], "description": item.get("description"), "expected_timing": item["timing_parsed"]["text"],
              "expected_placement": item["placement"], "candidates": candidates}
    if not candidates:
        result.update({"category": "missed", "best": None, "why": "no run row matches the item's keywords"})
        return result
    best = candidates[0]
    problems = []
    if not best["timing_compatible"]:
        problems.append("timing does not overlap")
    if not best["placement_ok"]:
        problems.append(f"placed as {best['row_placement']}, expected {best['expected_placement']}")
    result.update({"category": "found" if not problems else "misplaced", "best": best,
                   "why": "keyword+timing match; placement as expected" if not problems else "; ".join(problems)})
    return result


def period_key(row: dict) -> str:
    t = row["timing"]
    return "undated" if t["kind"] == "undated" else f"{t['start']}..{t['end']}"


def duplicates(item_results: list[dict], rows: list[dict]) -> list[dict]:
    out = []
    for r in item_results:
        ids = [c["row_id"] for c in r["candidates"]]
        if len(ids) >= 2:
            out.append({"rule": "same benchmark item", "item": r["id"], "rows": ids,
                        "detail": {c["row_id"]: c["matched_words"] for c in r["candidates"]}})
    groups: dict[tuple, list[str]] = {}
    for row in rows:
        program, action = normalize(row.get("program")), normalize(row.get("action"))
        if not program or not action:
            continue        # with no program or no action the key says too little to call two rows the same
        groups.setdefault((program, action, period_key(row)), []).append(row["id"])
    for (program, action, period), ids in sorted(groups.items()):
        if len(ids) >= 2:
            out.append({"rule": "same program+action+period", "item": None, "rows": ids,
                        "detail": {"program": program, "action": action, "period": period}})
    return out


def score_ticker(bench: dict, rows: list[dict]) -> dict:
    ticker = bench["ticker"]
    mine = [r for r in rows if r["ticker"] == ticker]
    item_results = [score_item(item, mine) for item in bench["items"]]
    matched_ids = {c["row_id"] for r in item_results for c in r["candidates"]}
    unmatched = [{"row_id": r["id"], "title": r["title"], "program": r["program"], "action": r["action"],
                  "timing": r["timing"]["text"], "placement": r["placement_class"], "searched": r["text"]}
                 for r in mine if r["id"] not in matched_ids]
    dups = duplicates(item_results, mine)
    counts = {"benchmark_items": len(item_results), "run_rows": len(mine),
              "found": sum(r["category"] == "found" for r in item_results),
              "misplaced": sum(r["category"] == "misplaced" for r in item_results),
              "missed": sum(r["category"] == "missed" for r in item_results),
              "duplicate_groups": len(dups), "unmatched_rows": len(unmatched)}
    return {"ticker": ticker, "bench_file": bench["file"], "counts": counts, "items": item_results,
            "duplicates": dups, "unmatched": unmatched}


def score(run_dir: Path, bench_files: list[Path]) -> dict:
    run, state, partition, window = load_run(run_dir)
    rows = run_rows(run, state, partition)
    benches = [load_bench(p) for p in bench_files]
    tickers = [score_ticker(b, rows) for b in benches]
    known = {t["ticker"] for t in tickers}
    other = sorted({r["ticker"] for r in rows if r["ticker"] not in known})
    totals = {k: sum(t["counts"][k] for t in tickers) for k in tickers[0]["counts"]} if tickers else {}
    return {"disclaimer": DISCLAIMER,
            "run": {"dir": str(run_dir), "id": (run.get("run") or {}).get("id"), "label": (run.get("run") or {}).get("label"),
                    "window": {"start": window[0], "end": window[1]}, "event_rows": len(rows)},
            "tickers": tickers, "totals": totals,
            "rows_without_benchmark": [{"ticker": t, "rows": [r["id"] for r in rows if r["ticker"] == t]} for t in other]}


# -------------------------------------------------------------------------------------------------- output

def _table(headers: list[str], lines: list[list[str]], widths: dict | None = None) -> list[str]:
    widths = dict(widths or {})
    cols = len(headers)
    w = [max([len(headers[i])] + [len(str(l[i])) for l in lines]) for i in range(cols)]
    w = [min(w[i], widths.get(headers[i], 60)) for i in range(cols)]
    cut = lambda s, n: (s if len(s) <= n else s[: n - 1] + "…")
    fmt = lambda cells: "  ".join(cut(str(c), w[i]).ljust(w[i]) for i, c in enumerate(cells)).rstrip()
    return [fmt(headers), "  ".join("-" * x for x in w)] + [fmt(l) for l in lines]


def render(result: dict) -> str:
    out = [f"Proof Reader benchmark score — run {result['run']['id']} ({result['run']['dir']})",
           f"Calendar window {result['run']['window']['start']} to {result['run']['window']['end']}; "
           f"{result['run']['event_rows']} event row(s) as the UI shows them.",
           DISCLAIMER, ""]
    for t in result["tickers"]:
        out.append(f"== {t['ticker']}  ({t['bench_file']})")
        lines = []
        for r in t["items"]:
            b = r["best"]
            lines.append([r["id"], r["category"].upper(), b["row_id"] if b else "—",
                          ", ".join(b["matched_words"]) if b else "—",
                          b["timing"] if b else f"expected {r['expected_timing']}",
                          f"{b['row_placement']} / {b['expected_placement']}" if b else f"— / {r['expected_placement']}",
                          r["why"]])
        out += _table(["item", "result", "row", "matched words", "timing comparison", "placed / expected", "why"],
                      lines, {"timing comparison": 96, "why": 60})
        out.append("")
        if t["duplicates"]:
            out.append("Possible duplicates:")
            for d in t["duplicates"]:
                what = f"item {d['item']}" if d["item"] else f"program+action+period {d['detail']}"
                out.append(f"  {', '.join(d['rows'])}: {d['rule']} ({what})")
            out.append("")
        if t["unmatched"]:
            out.append("Unmatched run rows (extra or unknown to the benchmark):")
            for u in t["unmatched"]:
                out.append(f"  {u['row_id']}  {u['title']!r}  program={u['program']!r}  action={u['action']!r}  "
                           f"timing={u['timing']!r}  placed={u['placement']}")
            out.append("")
        c = t["counts"]
        out.append(f"{t['ticker']}: found {c['found']}, misplaced {c['misplaced']}, missed {c['missed']} of "
                   f"{c['benchmark_items']} benchmark items; possible duplicate groups {c['duplicate_groups']}; "
                   f"unmatched run rows {c['unmatched_rows']} of {c['run_rows']}.")
        out.append("")
    if result["rows_without_benchmark"]:
        out.append("Rows for tickers with no benchmark file: " +
                   "; ".join(f"{x['ticker']} {x['rows']}" for x in result["rows_without_benchmark"]))
        out.append("")
    c = result["totals"]
    if c:
        out.append(f"TOTAL: found {c['found']}, misplaced {c['misplaced']}, missed {c['missed']} of "
                   f"{c['benchmark_items']} benchmark items; possible duplicate groups {c['duplicate_groups']}; "
                   f"unmatched run rows {c['unmatched_rows']} of {c['run_rows']}.")
    return "\n".join(out)


def _json_ready(obj):
    if isinstance(obj, dict):
        return {k: _json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_ready(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def folder_digest(run_dir: Path) -> dict[str, str]:
    """sha256 of every file under the run folder, by relative path (used by the tests to show nothing changed)."""
    run_dir = Path(run_dir)
    return {str(p.relative_to(run_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(run_dir.rglob("*")) if p.is_file()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.bench_score", description=__doc__.split("\n\n")[1])
    ap.add_argument("run_dir", type=Path, help="a saved run folder: run.json + decisions.jsonl (+ pages/)")
    ap.add_argument("bench_file", type=Path, nargs="+", help="benchmark file(s), one per ticker (.yaml/.yml/.json)")
    ap.add_argument("--json", type=Path, default=None, help="also write the full result as JSON to this file")
    args = ap.parse_args(argv)
    result = _json_ready(score(args.run_dir, args.bench_file))
    print(render(result))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=1) + "\n")
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
