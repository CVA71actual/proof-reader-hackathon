"""Build the first real entry: runs/ptgx-r4a/run.json, from a real filing page through Muse Spark and the code checks.

Usage (from the repo root):
    python -m runs.build_entry [--reading PATH] [--out-dir DIR]

Inputs (all in fixtures/, made by capture.capture_source and capture.read_events):
    ptgx_8k_2026-08-05_ex991_p3.pdf / .words.json   page 3 of the PTGX 8-K exhibit 99.1, rendered by code
                                                      from the filed HTML, and every word on it
    ptgx_8k_2026-08-05_ex991_p3.events.json          Muse Spark's proposed events (prompt, raw answer, parsed)

Writes into runs/ptgx-r4a/:
    run.json               label "PTGX · one real entry", sample false
    pages/p3.png           the page image
    pages/p3.words.json    every word on the page with its box in PDF points
    decisions.jsonl        empty: a person's keeps, fixes, removes and undos go here, through the server

Every event row is Muse Spark's proposal with the real code checks run on it (runs.recheck.row_checks, the same
function the server uses to re-check a fix): quote_match, selector_match on the proposed box, date_match with the
clause rule and the row's event words, and source_rule (an 8-K exhibit is a filing, an allowed source). Status
comes from checks.event_checks.row_status; the starting group from the status (matched -> kept, failed -> open,
anything else -> unverified), the same rule table values start from. A fake reading is labelled as fake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from capture import read_events
from capture.capture_source import FIXTURES, load_entry
from capture.read_table import FAKE_MODEL, sha256_text
from checks import event_checks
from checks.common import CheckResult, normalize
from checks.selector_match import MIN_WORD_HEIGHT_PT, _quote_run, selector_match
from runs import coverage, prices, recheck
from runs.build_run import MUSE_SPARK, RUNS, SCHEMA_VERSION, file_sha256, now_utc, render_page, to_contract_check
from runs.build_run import write_run

PIN = read_events.PIN
RUN_ID = "ptgx-r4a"
RUN_DIR = RUNS / RUN_ID
RUN_NUMBER = 3
REAL_LABEL = "PTGX · one real entry"
TWO_READERS_LABEL = "PTGX · two readers"
FAKE_LABEL = "Practice run (PTGX): fake reading, no model called"
SOURCE_ID = "src-ptgx-8k-2026-08-05-ex991"
BUILDER = "runs.build_entry"     # the code that applies and tidies; a Code step's who, never a model
READER = "capture.read_events"   # the code that sent the instructions

START_GROUP = {event_checks.MATCHED: "kept", event_checks.FAILED: "open"}
_SENTENCE_END = ".,;:"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ptgx_source(entry: dict, run_dir: Path) -> tuple[dict, dict]:
    """Render page 3 into the run folder and copy its words beside it. Returns (source, words_doc)."""
    page_pdf, words_src = FIXTURES / entry["page_file"], FIXTURES / entry["words_file"]
    if _sha(page_pdf) != entry["page_sha256"]:
        raise ValueError("The page file does not match the manifest; refusing to build the run.")
    if _sha(words_src) != entry["words_sha256"]:
        raise ValueError("The words file does not match the manifest; refusing to build the run.")
    page_no = entry["page_number"]
    image_rel, words_rel = f"pages/p{page_no}.png", f"pages/p{page_no}.words.json"
    render_page(page_pdf, run_dir / image_rel)
    shutil.copyfile(words_src, run_dir / words_rel)
    words_doc = json.loads(words_src.read_text())
    source = {
        "id": SOURCE_ID,
        "publisher": f"{entry['company']} (filed with the SEC)",
        "kind": "filing",
        "title": (f"{entry['ticker']} 8-K exhibit {entry['exhibit']} ({entry['exhibit_title'].lower()}), "
                  f"filed {entry['filing_date']}"),
        "url": entry["source_url"],
        "published_date": entry["filing_date"],
        "captured_at": entry["captured_at"],
        "sha256": entry["rendered_pdf_sha256"],
        "rendered_from": {"format": entry["source_format"], "sha256": entry["source_sha256"],
                          "renderer": entry["renderer"]},
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


def _str(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _box_from_ids(ids, words: list[dict]) -> list[float] | None:
    by_id = {w["id"]: w for w in words}
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in by_id for i in ids):
        return None
    ws = [by_id[i] for i in ids]
    return [min(w["x0"] for w in ws), min(w["y0"] for w in ws), max(w["x1"] for w in ws), max(w["y1"] for w in ws)]


def tidy_date_text(date_text: str | None, quote: str | None) -> str | None:
    """A full stop or comma the reader copied from the end of the sentence is not part of the date:
    'Q1 2027.' -> 'Q1 2027', when the shorter text is still printed in the quote. Nothing else changes."""
    if not date_text:
        return date_text
    s = date_text.rstrip(_SENTENCE_END).rstrip()
    return s if s and s != date_text and quote and s in quote else date_text


def locate_box(quote: str | None, hint, words: list[dict]) -> list[float] | None:
    """The box around the page words that read as the quote, when the quote is printed there (the run nearest the
    reader's box if it is printed more than once). None when it is not printed, or the model's box already passes the
    citation check: a box that is right is left as the reader drew it."""
    want = normalize(quote)
    if not want or not words:
        return None
    rect = tuple(hint) if isinstance(hint, list) and len(hint) == 4 else (0.0, 0.0, 1.0, 1.0)
    page = (max(w["x1"] for w in words), max(w["y1"] for w in words))
    if hint and selector_match(quote, hint, words, page[0], page[1], quote_lines=True).result == "pass":
        return None
    run = _quote_run(want, words, rect)
    if not run or any(w["y1"] - w["y0"] < MIN_WORD_HEIGHT_PT for w in run):
        # Words shorter than a line of print are a hidden text layer, not where the page shows the quote: their
        # bounds would replace the reader's box with a sliver at the wrong place (a slide page saved as a picture).
        return None
    return [min(w["x0"] for w in run), min(w["y0"] for w in run), max(w["x1"] for w in run),
            max(w["y1"] for w in run)]


def _same_box(a, b, tol: float = 0.5) -> bool:
    return bool(b) and len(b) == 4 and all(abs(x - y) <= tol for x, y in zip(a, b))


FIELD_WORDS = {"event_type": "event type", "date_text": "date text", "date_end": "end date", "word_ids": "word list"}


def plain_field(field: str) -> str:
    """A row field's name in words for a trail text: event_type -> 'event type'."""
    return FIELD_WORDS.get(field, str(field).replace("_", " "))


def plain_type(event_type) -> str:
    """An event type in words: regulatory_decision -> 'regulatory decision'."""
    return str(event_type).replace("_", " ") if event_type else "unknown"


def plain_value(field: str, value):
    """A field's value for a trail text: event types as words, anything else as it is."""
    return plain_type(value) if field == "event_type" and isinstance(value, str) else value


def when(row: dict) -> str:
    """The date for a trail text. A period is shown as the source wrote it ('Q1 2027'), never as the two dates the
    code worked out from it."""
    if row.get("date_end") and row.get("date_text"):
        return row["date_text"]
    if row["date"] and row.get("date_end"):
        return f"{row['date']} to {row['date_end']}"
    return row["date"] or row["date_text"] or "no date"


def sent_text(model_name: str, reading: dict) -> str:
    """The step name for what the code sent the model: the page, or for a reader that got only the image (or the
    image and the words) say so."""
    got = reading.get("input")
    if got == "image":
        return f"Sent the page image to {model_name}"
    if got == "image_and_words":
        return f"Sent the page image and its words to {model_name}"
    return f"Sent the page to {model_name}"


def row_reason(status: str, results: list, row: dict) -> str:
    if status == event_checks.MATCHED:
        if row.get("date_end"):
            return (f"Every check passed. The page names a period ({row['date_text']}), not a day, so the row "
                    f"keeps the period as stated.")
        return "Every check passed."
    if status == event_checks.FAILED:
        r = next(r for r in results if r.result == "fail")
        return f"{r.label} failed: {r.reason} Waiting for you."
    if status == event_checks.UNREADABLE:
        return "The reader's answer for this row could not be understood."
    r = next((r for r in results if r.result == "could_not_run"), None)
    if r is not None:
        return f"{r.label} could not run: {r.reason}"
    return "The reader's row stands as a proposal."


def build_row(i: int, event: dict, reading: dict, source: dict, words_doc: dict, at: str,
              share: tuple[int | None, int | None, int] = (None, None, 1), model_name: str = MUSE_SPARK,
              instructions_who: str = READER, second_reader: bool = False, tool_steps: list[dict] | None = None,
              tool_type: str | None = None) -> dict:
    """`share` is this row's part of the one call's tokens: (prompt, completion, rows the call proposed).
    `model_name`: the model that proposed it (the second reader's rows are built by the same function)."""
    words = words_doc["words"]
    fake = bool(reading.get("fake"))
    quote = _str(event.get("quote"))
    printed = _str(event.get("date_text"))
    box = event.get("box")
    box_ok = isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
    from_ids = None if box_ok else _box_from_ids(event.get("word_ids"), words)
    row = {
        "id": f"e{i}",
        "ticker": _str(event.get("ticker")) or "",
        "company": _str(event.get("company")) or "",
        "event_type": event_checks.event_type_for(quote),
        "type_hint": _str(event.get("type_hint") or event.get("event_type")),
        "title": _str(event.get("title")) or "",
        "date_text": tidy_date_text(printed, quote),
        "date": _str(event.get("date")),
        "date_end": _str(event.get("date_end")),
        "quote": quote,
        "program": _str(event.get("program")),
        "indication": _str(event.get("indication")),
        "source_id": source["id"],
        "page_number": source["pages"][0]["page_number"],
        "box": [float(v) for v in box] if box_ok else from_ids,
        "proposed_by": model_name,
    }
    reader = "fake reading (no model called)" if fake else model_name
    prompt_tokens = (reading.get("usage") or {}).get("prompt_tokens")
    trail = []
    if not fake:    # the code that routes: one actor per step, so the page going out is its own Code step
        trail.append({"step": sent_text(model_name, reading), "tag": "Code", "who": instructions_who, "at": at,
                      "text": f"Code chose what {model_name} would read and sent it, with the instructions below."})
    trail.append({"step": "Instructions sent to the second reader" if second_reader else "Instructions sent",
                  "tag": "Code", "who": instructions_who, "text": reading["prompt_text"], "at": at,
                  "tokens": ({"prompt": prompt_tokens, "completion": None} if isinstance(prompt_tokens, int) and not fake
                             else None)})
    if fake:
        trail.append({"step": "Proposed", "tag": "Code", "who": "capture.read_events",
                      "text": f"FAKE: built by code from the words, no model called. {row['ticker']} "
                              f"{plain_type(row['event_type'])} on {when(row)}: “{quote}”", "at": at})
    else:
        trail.append({"step": "Second reading" if second_reader else "Proposed", "tag": "Model", "who": model_name,
                      "text": f"{row['ticker']} {row['program'] or ''} type hint {(row['type_hint'] or 'none').replace('_', ' ')} on "
                              f"{when({**row, 'date_text': printed})}, printed as “{printed}”: “{quote}”"
                              .replace("  ", " "), "at": at,
                      **prices.model_cost_fields(model_name, share[0], share[1], call_rows=share[2])})
        trail.append({"step": "Took the reading into the row", "tag": "Code", "who": BUILDER, "at": at,
                      "text": "Copied the reader's proposal and box into the row. Nothing is checked yet."})
    trail.append({"step": "Assigned event type", "tag": "Code", "who": BUILDER, "at": at,
                  "text": f"Assigned {plain_type(row['event_type'])} from the quoted words; retained the reader's type hint separately."})
    trail += tool_steps or []     # the reader's own calls of the checks while it read (capture/reader_tools.py)
    if row["date_text"] != printed:
        trail.append({"step": "Tidied", "tag": "Code", "who": BUILDER,
                      "text": f"Date text “{printed}” kept as “{row['date_text']}”: the "
                              f"“{printed[len(row['date_text']):].strip()}” ends the sentence, not the date.",
                      "at": at})
    if from_ids is not None:
        trail.append({"step": "Placed", "tag": "Code", "who": BUILDER,
                      "text": f"The {reader} gave no box, so the box was drawn around the words it named "
                              f"({len(event['word_ids'])} words).", "at": at})
    inferred = event_checks.relative_period(row["date_text"], source.get("published_date"))
    if inferred is not None and not row["date"] and not row["date_end"]:
        row["date"], row["date_end"] = inferred[0].isoformat(), inferred[1].isoformat()
        trail.append({"step": "Year inferred from the filing date", "tag": "Code", "who": BUILDER,
                      "text": f"“{row['date_text']}” names no year. Counted from the filing date "
                              f"({source['published_date']}) it is {row['date']} to {row['date_end']}.", "at": at})
    day = event_checks.yearless_day(row["date_text"], source.get("published_date"))
    if day is not None:
        row["date"] = row["date"] or day.isoformat()
        trail.append({"step": "Year inferred from the filing date", "tag": "Code", "who": BUILDER,
                      "text": f"“{row['date_text']}” names no year. Counted from the filing date "
                              f"({source['published_date']}) it is {day.isoformat()}.", "at": at})
    if event_checks.is_today_word(row["date_text"]) and not row["date"] and not row["date_end"]:
        found = event_checks.release_day(words, source.get("published_date"))
        if found is not None:
            row["date"] = found[0].isoformat()
            trail.append({"step": f"Date taken from the {found[1]}", "tag": "Code", "who": BUILDER,
                          "text": f"“{row['date_text']}” means the day of the release. Taken from the {found[1]}: "
                                  f"{row['date']}.", "at": at})
    located = locate_box(row.get("quote"), row["box"], words)
    if located is not None and not _same_box(located, row["box"]):
        was = f"the {reader}'s box {[round(v) for v in row['box']]}" if row["box"] else f"no box from the {reader}"
        row["box"] = located
        trail.append({"step": "Box located by code", "tag": "Code", "who": BUILDER,
                      "text": f"The quote is printed on the page, so the box is the words found there; "
                              f"{was} was only a hint.", "at": at})
    if why := event_checks.event_type_reason(row["quote"]):    # carried on the row only when something is missing
        row["type_reason"] = why
    results = recheck.row_checks(row, source, words_doc)
    if tool_type and tool_type != row["event_type"]:
        # The reader asked the type tool and handed over another type. The row keeps the hand-over; the tool's
        # answer is not stored as the type and never replaces the code's own check, but the disagreement is held.
        results = [CheckResult(r.check, r.label, event_checks.FAIL,
                                            f"The reader's own check found the type {tool_type} for these words and "
                                            f"it handed over {row['event_type']}: " + r.reason, {**r.compared, "found": tool_type})
                   if r.check == event_checks.TYPE_ID and r.result != event_checks.FAIL else r for r in results]
    for r in results:
        trail.append({"step": "Checked", "tag": "Code", "who": r.check,
                      "text": f"{r.label}: {r.result.replace('_', ' ')}. {r.reason}", "at": at})
    status = event_checks.row_status(results)
    inferred = any(t["step"] == "Year inferred from the filing date" for t in trail)
    row.update({
        "status": status,
        "group": START_GROUP.get(status, "unverified"),
        "reason": row_reason(status, results, row) + (" Year inferred from the filing date." if inferred
                                                      and status == event_checks.MATCHED else ""),
        "sample": False,
        "checks": [to_contract_check(r.to_dict()) for r in results],
        "trail": trail,
    })
    return row


def build_entry_run(reading: dict, run_dir: Path, *, finished_at: str | None = None,
                    second_reading: dict | None = None, as_of: date | None = None) -> dict:
    """`second_reading`: the second reader's saved reading of the same page (capture/second_reader.py); when it is
    given and readable, runs/compare.py records where the two readers differ and adds what only the second found."""
    entry = load_entry(PIN)
    if sha256_text(reading.get("prompt_text") or "") != reading.get("prompt_sha256"):
        raise ValueError("The saved instructions do not match their SHA-256; refusing to build the run.")
    if reading.get("words_sha256") != entry["words_sha256"]:
        raise ValueError("The reading was made from a different words file; refusing to build the run.")
    if reading.get("call_failed") or not reading.get("readable"):
        raise SystemExit(f"The reading has no events ({reading.get('parse_problem')}); no run is built from it.")
    run_dir.mkdir(parents=True, exist_ok=True)
    source, words_doc = ptgx_source(entry, run_dir)
    at = reading["created_at"]
    fake = bool(reading.get("fake")) or reading.get("model") == FAKE_MODEL
    usage = {} if fake else (reading.get("usage") or {})
    n = len(reading["events"])
    shares = list(zip(prices.split_tokens(usage.get("prompt_tokens"), n),
                      prices.split_tokens(usage.get("completion_tokens"), n), [n] * n))
    rows = [build_row(i, ev, reading, source, words_doc, at, share=shares[i - 1])
            for i, ev in enumerate(reading["events"], start=1)]
    second = None
    if second_reading is not None and second_reading.get("readable") and not second_reading.get("call_failed"):
        from runs import compare   # imported here: compare builds rows with build_row above
        if sha256_text(second_reading.get("prompt_text") or "") != second_reading.get("prompt_sha256"):
            raise ValueError("The saved second-reader instructions do not match their SHA-256; refusing to build.")
        if second_reading.get("words_sha256") != entry["words_sha256"]:
            raise ValueError("The second reading was made from a different words file; refusing to build the run.")
        events = second_reading["events"]
        if second_reading.get("input") == "image":     # boxes were read off the image on the 0-1000 grid
            events = [{**e, "box": read_events.grid_to_points(e.get("box"), words_doc["page_width"],
                                                              words_doc["page_height"])} for e in events]
        _, only_second = compare.apply_events(rows, events, second_reading, source, words_doc,
                                              second_reading["created_at"], next_index=len(rows) + 1)
        rows += only_second
        second = {"model": compare.SECOND, "status": "read", "reason": None,
                  "input": second_reading.get("input") or "words"}
    # a dated sentence no reader turned into a row becomes an open row, so a person sees what was skipped
    rows += coverage.possible_event_rows(words_doc, rows, source, first_index=len(rows) + 1, at=at,
                                         today=as_of or date.fromisoformat(entry["filing_date"]), ticker=entry["ticker"],
                                         company=entry["company"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": run_dir.name,
            "number": RUN_NUMBER if second is None else RUN_NUMBER + 1,
            "label": FAKE_LABEL if fake else (REAL_LABEL if second is None else TWO_READERS_LABEL),
            "sample": False,
            "started_at": at,
            "finished_at": finished_at or now_utc(),
            "choices": {
                "companies": [entry["ticker"]],
                "event_types": list(read_events.EVENT_TYPES),
                "time_window_months": None,
                "spending_limit_usd": None,
                "note_for_agent": None,
            },
            "spend_usd": prices.run_spend_usd(rows),  # from the readings' tokens and runs/prices.py
            **({"second_reader": second} if second else {}),
        },
        "sources": [source],
        "tables": [],
        "rows": rows,
        "mistakes": [],
        "exclusions": [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reading", type=Path, default=read_events.READING_PATH)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--second", type=Path, default=None,
                    help="the second reader's saved reading (capture.second_reader); builds runs/ptgx-r5")
    args = ap.parse_args(argv)
    args.out_dir = args.out_dir or (RUNS / "ptgx-r5" if args.second else RUN_DIR)
    reading = json.loads(args.reading.read_text())
    second = json.loads(args.second.read_text()) if args.second else None
    run = build_entry_run(reading, args.out_dir, second_reading=second)
    out = write_run(run, args.out_dir)
    decisions = args.out_dir / "decisions.jsonl"
    if not decisions.exists():
        decisions.write_text("")
    for r in run["rows"]:
        print(f"{r['id']} {r['title']} [{r['date_text']}] {r['status']} / {r['group']}")
    print(f"Wrote {out} ({run['run']['label']}): {len(run['rows'])} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
