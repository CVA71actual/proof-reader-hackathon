"""A page with no text layer (a scan): SAM 3.1 finds regions, the reader reads each region's image, and the box is checked
against the region instead of against words.

A scanned page has nothing for the text-layer checks to compare with, so the path is:
  1. SAM 3.1 (capture/sam.py) is asked for each concept ("table", "block of text"), one request each; every box it
     returns is a region (merge_regions: tables first, a text block that sits inside a table is dropped, near
     duplicates are dropped, reading order top to bottom).
  2. Each region is cropped from the page image and sent to the reader with the event instructions; the reader's boxes
     come back on a 0-1000 grid over that crop and become PDF points on the page (to_page_points).
  3. Every proposal is a row built by the same builder as any other, with the same five checks. The text-layer ones say
     what they can: Quote found on page could not run (the page has no text layer); Citation located is the region check
     (checks/selector_match.py region_match): the box must lie inside a region SAM 3.1 found. So a row on a scan is at
     best `proposed`, never `matched`: nothing printed was compared with it. That is the honest status.
  4. The trail says it is a scanned page: Code "Scanned page", Model "SAM 3.1" (the image count and cost), then the
     reader's own steps.

assemble() is pure (no model call): it builds the run from the saved SAM answer and the saved region readings, so the
committed run can be rebuilt. The job (server/jobs.py) makes the calls, counts each one as a turn, and passes the same
pieces to assemble_rows().
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from capture import read_events, sam
from capture.capture_source import FIXTURES, load_entry
from checks.coverage import sentence_covered
from runs import build_entry, build_run, prices, page_outcomes

SOURCE_ID = "src-ptgx-8k-2026-08-05-ex991-scanned"
WHO = "runs.scan"
PAD_PT = 4.0          # a region's crop reaches this far past SAM's box, so a word cut by the box is not lost
INSIDE = 0.8          # a text block with at least this share of its area inside a table region is dropped
DUPLICATE_IOU = 0.8
PAGE_NOTE = ("SAM 3.1 found no table or text block on this page (it is an object segmenter, not a layout model): its only "
             "region is the whole page, so the box check could not run and the reader read the whole page image.")
SCAN_NOTE = ("The page has no text layer (a scan), so there are no words to check a quote or a box against. SAM 3.1 found "
             "the region; the reader read the region's image; the box was checked against the region.")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scanned_png(entry: dict) -> tuple[bytes, dict]:
    """Render the verified pinned PDF bytes and return the identity of the exact PNG sent to SAM/readers."""
    from runs.sources import builtin_index, pinned_source_snapshot

    record = builtin_index().pages("PTGX-SCAN")[0]["record"]
    pdf_bytes, _, _, _ = pinned_source_snapshot(record)
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "scan.png"
        build_run.render_page_bytes(pdf_bytes, png)
        raw = png.read_bytes()
        return raw, {"sha256": sha256(raw), "kind": "image"}


# ---------------------------------------------------------------- regions


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b) -> float:
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def needs_fallback(by_concept: dict[str, list[list[float]]]) -> bool:
    """True when no concept has matched anything yet: the whole page is then asked for (sam.FALLBACK)."""
    return not any(by_concept.get(c) for c in sam.CONCEPTS)


def merge_regions(by_concept: dict[str, list[list[float]]]) -> list[dict]:
    """[{id, concept, box}] in PDF points from the boxes SAM 3.1 found for each concept (`by_concept`: concept -> boxes).
    Tables come first; a text block mostly inside a table, or almost the same box as one already kept, is dropped;
    the rest are ordered top to bottom, then left to right, and numbered r1, r2, ..."""
    kept: list[tuple[str, list[float]]] = []
    for concept in sorted(by_concept, key=lambda c: (c != "table", c == sam.FALLBACK, c)):
        if concept == sam.FALLBACK and kept:
            continue          # the whole page is a region only when SAM found nothing finer
        for box in by_concept[concept]:
            if any(_inter(box, k) / _area(box) >= INSIDE for c, k in kept if c == "table" and concept != "table"):
                continue
            if any(_inter(box, k) / (_area(box) + _area(k) - _inter(box, k)) >= DUPLICATE_IOU for _, k in kept):
                continue
            kept.append((concept, box))
    kept.sort(key=lambda ck: (ck[1][1], ck[1][0]))
    return [{"id": f"r{i}", "concept": c, "box": b} for i, (c, b) in enumerate(kept, start=1)]


def crop_box(region: dict, page_w: float, page_h: float) -> list[float]:
    x0, y0, x1, y1 = region["box"]
    return [max(0.0, x0 - PAD_PT), max(0.0, y0 - PAD_PT), min(page_w, x1 + PAD_PT), min(page_h, y1 + PAD_PT)]


def crop_png(png: bytes, box_pt: list[float], page_w: float) -> bytes:
    """The part of the page image inside a box in PDF points, as PNG bytes."""
    import pymupdf

    doc = pymupdf.open(stream=png, filetype="png")
    try:
        page = doc[0]
        units_per_pt = page.rect.width / page_w                # a PNG page is measured in its own dpi's units
        px_per_unit = pymupdf.Pixmap(png).width / page.rect.width
        clip = pymupdf.Rect(*(v * units_per_pt for v in box_pt))
        return page.get_pixmap(matrix=pymupdf.Matrix(px_per_unit, px_per_unit), clip=clip).tobytes("png")
    finally:
        doc.close()


def region_prompt(entry: dict, region: dict, types=read_events.EVENT_TYPES, note: str | None = None) -> str:
    if region["concept"] == sam.FALLBACK:
        head = ("The image is a scanned page (SAM 3.1 found no table or text block on it, only the page). Boxes are on a "
                "0 to 1000 grid over this image.\n\n")
    else:
        head = (f"The image is one region of a scanned page (a {region['concept']} that SAM 3.1 found); it is not the "
                f"whole page. Boxes are on a 0 to 1000 grid over this region image.\n\n")
    return head + read_events.build_image_prompt(entry, tuple(types), note)


def to_page_points(event: dict, crop: list[float]) -> dict:
    """A box on the 0-1000 grid over the crop -> PDF points on the page."""
    box = read_events.grid_to_points(event.get("box"), crop[2] - crop[0], crop[3] - crop[1])
    if box is not None:
        box = [round(box[0] + crop[0], 2), round(box[1] + crop[1], 2), round(box[2] + crop[0], 2),
               round(box[3] + crop[1], 2)]
    return {**event, "box": box}


# ---------------------------------------------------------------- the source and the rows


def mark_scanned_source(source: dict, regions: list[dict]) -> dict:
    """Attach the SAM interpretation to the already verified, captured source page."""
    page = source["pages"][0]
    page.update(scanned=True, regions=regions,
                visual_evidence={"origin": "approximate", "overlays_supported": False,
                                 "reason": "SAM regions locate approximate areas; they do not verify visible words or a citation box."})
    source["title"] += ", as a scanned page (the image only)"
    return source


def assemble_rows(reads: list[dict], source: dict, sam_calls: list[dict], at: str, run_dir: Path,
                  first_index: int = 1) -> list[dict]:
    """The rows from the region readings. `reads`: [{region, crop, prompt_text, raw, model, usage, events}] with
    `events` the parsed answer (None if it could not be read). `sam_calls`: [{concept, images, cost_usd}] for the trail."""
    page = source["pages"][0]
    from runs.sources import load_page_evidence
    words_doc, consumed_artifact = load_page_evidence(run_dir, page)
    sam_cost = sum(c["cost_usd"] or 0 for c in sam_calls)
    images = sum(c["images"] or 0 for c in sam_calls)
    rows: list[dict] = []
    pending: list[tuple[dict, dict]] = []
    for read in reads:
        events = read.get("events") or []
        n = len(events)
        shares = list(zip(prices.split_tokens((read.get("usage") or {}).get("prompt_tokens"), n),
                          prices.split_tokens((read.get("usage") or {}).get("completion_tokens"), n), [n] * n))
        reading = {"fake": False, "prompt_text": read["prompt_text"], "usage": read.get("usage") or {},
                   "model": read.get("model")}
        for ev, share in zip(events, shares):
            ev = to_page_points(ev, read["crop"])
            if any(sentence_covered(ev.get("quote") or "", [r["quote"]]) or sentence_covered(r["quote"] or "",
                                                                                              [ev.get("quote")])
                   for r in rows if r["quote"] and ev.get("quote")):
                continue          # the same sentence read again from an overlapping region
            row = build_entry.build_row(first_index + len(rows), ev, reading, source, words_doc, at, share=share,
                                        consumed_artifact=consumed_artifact)
            rows.append(row)
            pending.append((row, read["region"]))
    for row, region in pending:
        per_row = round(sam_cost / len(rows), prices.ROUND_TO) if rows else 0
        steps = [
            {"step": "Scanned page", "tag": "Code", "who": WHO, "at": at,
             "text": PAGE_NOTE if region["concept"] == sam.FALLBACK else SCAN_NOTE},
            {"step": f"{sam.NAME} found {region['id']}", "tag": "Model", "who": sam.NAME, "at": at,
             "text": (f"Region {region['id']}: {'the whole page' if region['concept'] == sam.FALLBACK else 'a ' + region['concept']}"
                      f" at {region['box']} (PDF points). SAM 3.1 finds where things are; it reads nothing. {images} "
                      f"image{'s' if images != 1 else ''} processed for this page ({len(sam_calls)} concept "
                      f"requests); this row's share of the cost is recorded."),
             "tokens": None, "cost_usd": per_row},
        ]
        at_instructions = next(i for i, t in enumerate(row["trail"]) if t["step"] == "Instructions sent")
        row["trail"][at_instructions + 1:at_instructions + 1] = steps
        row["reason"] = (f"{row['reason']} The page is a scan: there is no text layer, so nothing printed was compared "
                         f"with this row.") if row["status"] != "failed" else row["reason"]
    return rows


# ---------------------------------------------------------------- the pure build from saved answers


def assemble(entry: dict, run_dir: Path, sam_record: dict, read_record: dict, *, finished_at: str | None = None,
             number: int = 5) -> dict:
    """The run for the scanned PTGX page, from the saved SAM answer and the saved region readings. No model is called."""
    page_w = float(read_record["page_width"])
    page_h = float(read_record["page_height"])
    by_concept = {c: [sam.to_points(b, page_w, page_h) for b in v["boxes"]] for c, v in sam_record["concepts"].items()}
    regions = merge_regions(by_concept)
    from runs.sources import builtin_index, capture_page_evidence, load_page_image_evidence
    record = builtin_index().pages("PTGX-SCAN")[0]["record"]
    source, _words, _consumed = capture_page_evidence(record, run_dir)
    mark_scanned_source(source, regions)
    _png, image_identity = load_page_image_evidence(run_dir, source["pages"][0])
    if read_record.get("image_sha256") != image_identity["sha256"]:
        raise ValueError("The saved scan reading was made from a different captured page image.")
    by_id = {r["id"]: r for r in regions}
    reads = [{**r, "region": by_id[r["region_id"]]} for r in read_record["reads"]]
    calls = [{"concept": c, "images": v["images"], "cost_usd": prices.image_cost_usd(sam.NAME, v["images"])}
             for c, v in sam_record["concepts"].items()]
    at = read_record["created_at"]
    rows = assemble_rows(reads, source, calls, at, run_dir)
    from runs.build_run import SCHEMA_VERSION, now_utc
    outcome = page_outcomes.interpretation(reads, bool(calls or reads))
    outcome.update(source_id=source["id"], ticker=entry["ticker"], company=entry["company"],
                   page_number=entry["page_number"])

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {"id": run_dir.name, "number": number, "label": "PTGX · scanned page (SAM 3.1 regions)", "sample": False,
                "started_at": at, "finished_at": finished_at or now_utc(),
                "choices": {"companies": [entry["ticker"]], "event_types": list(read_events.EVENT_TYPES),
                            "time_window_months": None, "spending_limit_usd": None, "note_for_agent": None},
                "spend_usd": prices.run_spend_usd(rows), "page_outcomes": [outcome],
                "pages_read": int(outcome["read"]), "pages_unread": int(outcome["unreadable"])},
        "sources": [source], "tables": [], "rows": rows, "mistakes": [], "exclusions": [],
    }


# ---------------------------------------------------------------- recording and building from the command line

READ_PATH = FIXTURES / f"{read_events.PIN}.scan_reading.json"
RUN_ID = "ptgx-scan"


def _regions_from_saved(entry: dict, sam_record: dict) -> tuple[list[dict], float, float]:
    from runs.sources import builtin_index, pinned_source_snapshot

    record = builtin_index().pages("PTGX-SCAN")[0]["record"]
    words = pinned_source_snapshot(record)[2]
    w, h = float(words["page_width"]), float(words["page_height"])
    return merge_regions({c: [sam.to_points(b, w, h) for b in v["boxes"]] for c, v in sam_record["concepts"].items()}), w, h


def main(argv: list[str] | None = None) -> int:
    """python -m runs.scan --live     read each region SAM 3.1 found (fixtures/..sam.json) with the reader, live; saves
                                      fixtures/..scan_reading.json
       python -m runs.scan            build runs/ptgx-scan/ from the two saved files (no model call)"""
    import argparse
    import json
    from datetime import datetime, timezone

    from capture.reader import LiveReader
    from runs.build_run import write_run

    ap = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=build_run.RUNS / RUN_ID)
    args = ap.parse_args(argv)
    entry = load_entry(read_events.PIN)
    sam_record = json.loads(sam.SAM_PATH.read_text())
    png, image_identity = scanned_png(entry)
    if args.live:
        reader = LiveReader()
        problem = reader.ready()
        if problem:
            print(problem)
            return 2
        regions, w, h = _regions_from_saved(entry, sam_record)
        reads = []
        for region in regions:
            crop = crop_box(region, w, h)
            prompt = region_prompt(entry, region)
            reply = reader.call(prompt, image=crop_png(png, crop, w))
            if reply.error:
                print(f"{region['id']}: the reader's call did not succeed ({reply.error}).")
                return 1
            events, problem = read_events.parse_events(reply.raw)
            reads.append({"region_id": region["id"], "region": region, "crop": crop, "prompt_text": prompt,
                          "raw_response": reply.raw, "model": reply.model, "events": events, "parse_problem": problem,
                          "usage": {"prompt_tokens": reply.prompt_tokens, "completion_tokens": reply.completion_tokens}})
            print(f"{region['id']} ({region['concept']}): {None if events is None else len(events)} events, tokens "
                  f"{reply.prompt_tokens} in / {reply.completion_tokens} out")
        READ_PATH.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                         "page_width": w, "page_height": h, "image_sha256": image_identity["sha256"],
                                         "reads": reads}, indent=1, ensure_ascii=False) + "\n")
        print(f"saved -> {READ_PATH}")
        return 0
    read_record = json.loads(READ_PATH.read_text())
    run = assemble(entry, args.out_dir, sam_record, read_record)
    write_run(run, args.out_dir)
    decisions = args.out_dir / "decisions.jsonl"
    if not decisions.exists():
        decisions.write_text("")
    for r in run["rows"]:
        print(f"{r['id']} {r['title']} [{r['date_text']}] {r['status']} / {r['group']}")
    print(f"Wrote {args.out_dir / 'run.json'}: {len(run['rows'])} rows, spend ${run['run']['spend_usd']}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
