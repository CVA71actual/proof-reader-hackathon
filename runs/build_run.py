"""Build a run.json from the S1 fixtures: one source (IONS ARS, page 95) and one table read by Muse Spark.

Usage (from the repo root):
    python -m runs.build_run [--reading PATH] [--out-dir DIR]

Writes into runs/demo/ by default:
    run.json               the contract file the screens read
    pages/p95.png          the page image (rendered with pymupdf)
    pages/p95.words.json   every word on the page with its box in PDF points

The run is labelled "Saved demo run" only when the reading is a real model reading. A fake reading
gets a label that says so. The existing code checks are run on every value; their results are copied
as they are (pass, fail, could_not_run) and a value is "matched" only when every required check passed.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from capture import read_table
from checks import event_checks, row_column_match, selector_match, text_layer_match
from checks.status import UNREADABLE, check_reading
from runs import prices

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
RUNS = REPO / "runs"
SCHEMA_DIR = RUNS / "schema"
DEMO_DIR = RUNS / "demo"

SCHEMA_VERSION = "1"
RENDER_ZOOM = 2.0
REAL_LABEL = "Saved demo run"
FAKE_LABEL = "Practice run: fake reading, no model called"

IONS_SOURCE_ID = "src-ions-ars-2026"
MUSE_SPARK = "Muse Spark"

# Every check id that can appear in a run, mapped to the function that produces it.
CHECK_FUNCTIONS = {
    text_layer_match.CHECK_ID: text_layer_match.text_layer_match,
    selector_match.CHECK_ID: selector_match.selector_match,
    row_column_match.CHECK_ID: row_column_match.row_column_match,
    event_checks.QUOTE_ID: event_checks.quote_match,
    event_checks.DATE_ID: event_checks.date_match,
    event_checks.SOURCE_ID: event_checks.source_rule,
    event_checks.TYPE_ID: event_checks.type_match,
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def code_ref(check_id: str) -> dict:
    """The repo file and function that ran a check, taken from the function object itself."""
    fn = CHECK_FUNCTIONS[check_id]
    path = Path(inspect.getsourcefile(fn)).resolve().relative_to(REPO)
    return {"file": path.as_posix(), "function": fn.__name__}


def to_contract_check(c: dict) -> dict:
    """One CheckResult.to_dict() -> the run.json check object. Nothing is re-judged here."""
    raw = c.get("compared") or {}
    compared = {"looked_for": raw.get("value"), "found": raw.get("found")}
    if "box" in raw:
        compared["box"] = list(raw["box"]) if raw["box"] is not None else None
    if "word_ids" in raw:
        compared["word_ids"] = list(raw["word_ids"])
    return {
        "name": c["check"],
        "label": c["label"],
        "kind": c.get("kind", "Code"),
        "result": c["result"],
        "reason": c["reason"],
        "compared": compared,
        "code_ref": code_ref(c["check"]),
    }


def model_display_name(model_id: str) -> str:
    if model_id == read_table.FAKE_MODEL:
        return "fake (no model called)"
    if model_id.startswith("muse-spark"):
        return f"{MUSE_SPARK} ({model_id})"
    return model_id


def build_table(reading: dict, words_doc: dict, *, table_id: str, source_id: str, page_number: int) -> dict:
    prompt = reading.get("prompt_text") or ""
    sha = reading.get("prompt_sha256") or ""
    if read_table.sha256_text(prompt) != sha:
        raise ValueError("The saved instructions do not match their SHA-256; refusing to build the run.")
    checked = check_reading(reading, words_doc)
    table = reading.get("table") if isinstance(reading.get("table"), dict) else {}
    rows = []
    for ri, row in enumerate(checked["rows"]):
        cells = []
        for ci, cell in enumerate(row["cells"]):
            box = cell.get("box")
            cells.append({
                "id": f"{table_id}.{ri}.{ci}",
                "column": str(cell.get("column", "")),
                "value": cell.get("value"),
                "box": list(box) if isinstance(box, (list, tuple)) else None,
                "status": cell["status"],
                "checks": [to_contract_check(c) for c in cell["checks"]],
            })
        rows.append({"label": row["label"], "cells": cells})
    unreadable = checked["status"] == UNREADABLE
    return {
        "id": table_id,
        "source_id": source_id,
        "page_number": page_number,
        "title": table.get("title", "") if not unreadable else "",
        "columns": list(table.get("columns", [])) if not unreadable else [],
        "model": {"name": model_display_name(reading.get("model", "")), "tag": "Model"},
        "instructions": {"prompt_sha256": sha, "prompt_text": prompt},
        "status": "unreadable" if unreadable else "checked",
        "problem": reading.get("parse_problem") if unreadable else None,
        "rows": rows,
    }


def render_page(pdf: Path, out_png: Path, zoom: float = RENDER_ZOOM) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    pix.save(out_png)
    doc.close()


def file_sha256(path: Path) -> str:
    """The sha256 a run records for each page file it copies, so a changed byte is noticed."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ions_source(manifest: dict, run_dir: Path) -> tuple[dict, dict]:
    """Render page 95 into the run folder and copy its words beside it. Returns (source, words_doc)."""
    page_no = manifest["page_number"]
    image_rel = f"pages/p{page_no}.png"
    words_rel = f"pages/p{page_no}.words.json"
    render_page(FIXTURES / manifest["page_file"], run_dir / image_rel)
    words_src = FIXTURES / manifest["words_file"]
    if hashlib.sha256(words_src.read_bytes()).hexdigest() != manifest["words_sha256"]:
        raise ValueError("The words file does not match the manifest; refusing to build the run.")
    shutil.copyfile(words_src, run_dir / words_rel)
    words_doc = json.loads(words_src.read_text())
    source = {
        "id": IONS_SOURCE_ID,
        "publisher": f"{manifest['company']} (filed with the SEC)",
        "kind": "filing",
        "title": f"{manifest['ticker']} annual report to shareholders ({manifest['form']}), filed {manifest['filing_date']}",
        "url": manifest["source_url"],
        "published_date": manifest["filing_date"],
        "captured_at": manifest["captured_at"],
        "sha256": manifest["source_sha256"],
        "pages": [{
            "page_number": page_no,
            "image": image_rel,
            "width_pt": words_doc["page_width"],
            "height_pt": words_doc["page_height"],
            "words": words_rel,
            "image_sha256": file_sha256(run_dir / image_rel),
            "words_sha256": file_sha256(run_dir / words_rel),
        }],
    }
    return source, words_doc


def build_demo_run(reading: dict, run_dir: Path, *, finished_at: str | None = None) -> dict:
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text())
    run_dir.mkdir(parents=True, exist_ok=True)
    source, words_doc = ions_source(manifest, run_dir)
    table = build_table(reading, words_doc, table_id="t1", source_id=source["id"],
                        page_number=manifest["page_number"])
    fake = bool(reading.get("fake")) or reading.get("model") == read_table.FAKE_MODEL
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_dir.name,
            "number": 1,
            "label": FAKE_LABEL if fake else REAL_LABEL,
            "sample": False,
            "started_at": reading.get("created_at") or manifest["captured_at"],
            "finished_at": finished_at or now_utc(),
            "choices": {
                "companies": [manifest["ticker"]],
                "event_types": [],
                "time_window_months": None,
                "spending_limit_usd": None,
                "note_for_agent": None,
            },
            "spend_usd": None,  # the S1 reading did not record its cost
        },
        "sources": [source],
        "tables": [table],
        "rows": [],
        "mistakes": [],
        "exclusions": [],
    }


def write_run(run: dict, run_dir: Path) -> Path:
    from runs.ids import require_unique_target_ids

    require_unique_target_ids(run)
    out = run_dir / "run.json"
    if "prices" not in run["run"]:  # a run made now carries the price table in force (Job 19)
        run["run"]["prices"] = prices.snapshot()
    out.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reading", type=Path, default=read_table.READING_PATH)
    ap.add_argument("--out-dir", type=Path, default=DEMO_DIR)
    args = ap.parse_args(argv)
    reading = json.loads(args.reading.read_text())
    run = build_demo_run(reading, args.out_dir)
    out = write_run(run, args.out_dir)
    cells = [c for r in run["tables"][0]["rows"] for c in r["cells"]]
    print(f"Wrote {out} ({run['run']['label']}): {len(cells)} values, "
          f"{sum(c['status'] == 'matched' for c in cells)} matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
