"""Escalation to Muse Wonder: when a row fails a check or the two readers disagree, one row's page region goes to a
stronger model for a re-read, and whatever it answers goes through the same checks.

    candidates(rows)                  the event rows to send: a failed check, or a disagreement between the readers
    trigger(row)                      why: the first failed check's label and reason, or the disputed field
    region_png(row, page_png, words_doc, page_pt)   (PNG bytes of the band of the page around the row, its y range)
    prompt(entry, trigger, band, page_pt, types, note)   what Wonder is asked
    apply(row, ...)                   record the escalation on the row; replace its values only if the re-read passes
                                      every check

Muse Wonder is never trusted on its own. Its answer is a new proposal, built with the same builder as every other
row, so the same five checks run on it (quote on the page, box on the quote, date as printed, the source, the kind
of event). If it passes every check and the row had failed one, the row takes its values (group `corrected`, status
`matched`) and the failure is recorded as a mistake the CODE caught, repaired by the escalation. If it does not pass,
the row stays as it was. If the row was sent because the readers disagree, nothing is replaced: only a person's fix
settles a disagreement, and Wonder's reading is added to the trail as evidence.

The trail, one actor per step: Code "Sent to Muse Wonder because <check> failed" (runs.escalate), Code "Instructions
sent to Muse Wonder" (capture.wonder), Model "Muse Wonder" (its tokens and cost), Code "Checked" for each check on its
proposal, Code "Escalation result" (runs.escalate). A row that is sent and not replaced carries the same steps; its
result says "the row stays as it was" and why.
"""

from __future__ import annotations

from accuracy import types as T
from capture import read_events, read_table
from checks.common import normalize
from checks.selector_match import _quote_run
from runs import build_entry, prices

WONDER = "Muse Wonder"
WHO = "runs.escalate"
WONDER_WHO = "capture.wonder"
MAX_PER_PAGE = 1     # rows sent to Wonder from one page: each is a counted, priced turn
WONDER_SHARE = 0.25  # Wonder's part of a limited run's spending limit: past it nothing more is sent
SENT_FOR = ("Date read as written", "Event type matches the quote")   # the failures worth Wonder's price
PAD_PT = 60.0        # how far above and below the row the region reaches, in PDF points
CHARS_PER_TOKEN = 4  # a rough count of prompt tokens from text, for the estimate made before a call
BYTES_PER_IMAGE_TOKEN = 750  # the same for an image, from its PNG size


def estimate_usd(prompt_text: str, image: bytes | None) -> float:
    """What a Wonder call is likely to cost before it is sent: the prompt's size at Muse Wonder's input price.
    Only the input is counted, so it is a floor; the reply's real cost is added once it comes."""
    tokens = len(prompt_text) / CHARS_PER_TOKEN + (len(image) / BYTES_PER_IMAGE_TOKEN if image else 0)
    return tokens * prices.PRICES_PER_1M_USD["Muse Wonder"]["input"] / 1_000_000


FIELDS = ("event_type", "title", "date_text", "date", "date_end", "quote", "program", "indication", "box")


def candidates(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("found_by") == "coverage" or r.get("status") == "unreadable" or r.get("group") == "removed":
            continue
        if any(c["result"] == "fail" for c in r.get("checks", [])) or r.get("disagreement"):
            out.append(r)
    return out


def budgeted(rows: list[dict]) -> list[dict]:
    """The candidates Wonder is worth sending: a failed date or event type check. A box or quote failure is mended by
    code (runs.build_entry.locate_box) or is a paraphrase Wonder cannot cure; a disagreement is settled only by a person,
    so Wonder's reading would be evidence only."""
    return [r for r in candidates(rows) if any(c["result"] == "fail" and c["label"] in SENT_FOR
                                               for c in r.get("checks", []))]


def trigger(row: dict, labels: tuple[str, ...] | None = None) -> dict:
    """{kind, label, why}: the first failed check (of `labels`, if given), else the readers' disagreement."""
    for c in row.get("checks", []):
        if c["result"] == "fail" and (labels is None or c["label"] in labels):
            return {"kind": "check", "label": c["label"], "why": c["reason"]}
    d = row["disagreement"]
    f = d["field"]
    return {"kind": "disagreement", "label": f"the two readers disagree on {build_entry.plain_field(f)}",
            "why": (f"Muse Spark read {build_entry.plain_value(f, d['first_reader']) if d['first_reader'] is not None else 'nothing'} and "
                    f"Muse Spark Open read {build_entry.plain_value(f, d['second_reader']) if d['second_reader'] is not None else 'nothing'}.")}


def band(row: dict, words_doc: dict) -> tuple[float, float]:
    """The y range, in PDF points, of the region sent: the quote's own lines if it is printed, else the row's box,
    padded; the whole page when the row has neither."""
    h = float(words_doc["page_height"])
    quote = normalize(row.get("quote"))
    run = _quote_run(quote, words_doc["words"], (0, 0, 1, 1)) if quote else None
    if run:
        y0, y1 = min(w["y0"] for w in run), max(w["y1"] for w in run)
    elif isinstance(row.get("box"), list) and len(row["box"]) == 4:
        y0, y1 = float(row["box"][1]), float(row["box"][3])
    else:
        return 0.0, h
    return max(0.0, y0 - PAD_PT), min(h, y1 + PAD_PT)


def region_png(page_png: bytes, words_doc: dict, y_range: tuple[float, float]) -> bytes:
    """The full-width band of the page image between two y values in PDF points, as PNG bytes."""
    import pymupdf

    doc = pymupdf.open(stream=page_png, filetype="png")
    try:
        page = doc[0]
        units_per_pt = page.rect.width / float(words_doc["page_width"])    # a PNG page is in its own dpi's units
        px_per_unit = pymupdf.Pixmap(page_png).width / page.rect.width     # keep the image's real resolution
        clip = pymupdf.Rect(0, y_range[0] * units_per_pt, page.rect.width, y_range[1] * units_per_pt)
        return page.get_pixmap(matrix=pymupdf.Matrix(px_per_unit, px_per_unit), clip=clip).tobytes("png")
    finally:
        doc.close()


def prompt(entry: dict, trig: dict, y_range: tuple[float, float], words_doc: dict,
           types: tuple[str, ...] = read_events.EVENT_TYPES, note: str | None = None) -> str:
    head = (f"You are re-reading one region of a page. The image is a horizontal band of the page, from {y_range[0]:.0f} "
            f"to {y_range[1]:.0f} points down a page {words_doc['page_width']:.0f} points wide and "
            f"{words_doc['page_height']:.0f} tall; it is not the whole page.\n"
            f"An earlier reading of an event printed in this band was stopped by a code check: "
            f"{trig['label']}. {trig['why']}\n"
            f"Read the band again, carefully, from the image alone. Boxes are on a 0 to 1000 grid over this band image.\n\n")
    return head + read_events.build_image_prompt(entry, types, note)


def pick(row: dict, events: list[dict]) -> dict | None:
    """The event in Wonder's answer that is this row's: the one whose quote overlaps the row's quote (the band may hold
    other events too). With one event and no overlap (the row's own quote may be what was wrong) that one; with
    several and none overlapping, none."""
    from runs.compare import _same_quote

    same = [e for e in events if _same_quote(row.get("quote"), e.get("quote"))]
    if same:
        return same[0]
    return events[0] if len(events) == 1 else None


def to_page_points(event: dict, y_range: tuple[float, float], words_doc: dict) -> dict:
    """The band's 0-1000 grid box -> PDF points on the page (the band is the full width)."""
    box = read_events.grid_to_points(event.get("box"), float(words_doc["page_width"]), y_range[1] - y_range[0])
    if box is not None:
        box = [box[0], round(box[1] + y_range[0], 2), box[2], round(box[3] + y_range[0], 2)]
    return {**event, "box": box}


def _changes(old: dict, new: dict) -> list[str]:
    return [f"{build_entry.plain_field(f)} from {build_entry.plain_value(f, old.get(f))} to "
            f"{build_entry.plain_value(f, new.get(f))}" for f in FIELDS
            if f != "box" and normalize(None if old.get(f) is None else str(old.get(f)))
            != normalize(None if new.get(f) is None else str(new.get(f)))]


def record_sent(row: dict, reading_tokens: int | None, at: str, trig: dict, prompt_text: str) -> None:
    """The two Code steps before Wonder answers: that the row was sent and why, and the exact instructions."""
    row["trail"] += [
        {"step": f"Sent to {WONDER}", "tag": "Code", "who": WHO, "at": at,
         "text": (f"{trig['label'][:1].upper()}{trig['label'][1:]}"
                  + (" failed" if trig["kind"] == "check" else "") + f". {trig['why']}")},
        {"step": f"Instructions sent to {WONDER}", "tag": "Code", "who": WONDER_WHO, "at": at, "text": prompt_text,
         "tokens": {"prompt": reading_tokens, "completion": None} if isinstance(reading_tokens, int) else None},
    ]


def record_unused_call(row: dict, reply, at: str, why: str) -> None:
    """A Wonder call was made but its reading is not taken (its cost passed the run's limit, or could not be counted):
    the Model step carries the tokens and cost, then the result says the row stays as it was and why."""
    row["trail"] += [
        {"step": WONDER, "tag": "Model", "who": WONDER, "at": at,
         "text": f"{WONDER} answered. Its reading was not used.",
         **prices.model_cost_fields(WONDER, reply.prompt_tokens, reply.completion_tokens)},
        {"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
         "text": f"{why} The row stays as it was."},
    ]


def apply(row: dict, event: dict | None, reading: dict, source: dict, words_doc: dict, at: str, trig: dict,
          run: dict, share: tuple, prompt_text: str) -> str:
    """Record the escalation on `row` (and its replacement and mistake on `run`). Returns what happened:
    "replaced", "kept" (the re-read did not pass every check), "evidence" (a disagreement: nothing replaced) or
    "unreadable" (no event in the answer)."""
    pt = (reading.get("usage") or {}).get("prompt_tokens")
    record_sent(row, pt, at, trig, prompt_text)
    if event is None:
        row["trail"].append({"step": WONDER, "tag": "Model", "who": WONDER, "at": at,
                             "text": "Answered with no event for this row.",
                             **prices.model_cost_fields(WONDER, reading.get("usage", {}).get("prompt_tokens"),
                                                        reading.get("usage", {}).get("completion_tokens"))})
        row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
                             "text": f"{WONDER}'s answer held no event for this row. The row stays as it was."})
        return "unreadable"
    proposal = build_entry.build_row(0, event, reading, source, words_doc, at, share=share, model_name=WONDER,
                                     instructions_who=WONDER_WHO)
    model_step = next((t for t in proposal["trail"] if t["tag"] == "Model"),
                      next(t for t in proposal["trail"] if t["step"] == "Proposed"))
    row["trail"].append({**model_step, "step": WONDER})
    row["trail"] += [t for t in proposal["trail"] if t["step"] == "Checked"]
    passed = proposal["status"] == "matched"
    if trig["kind"] == "disagreement":
        d = row["disagreement"]
        value = proposal.get(d["field"])
        agrees = [who for who, v in (("Muse Spark", d["first_reader"]), ("Muse Spark Open", d["second_reader"]))
                  if v is not None and normalize(str(v)).lower() == normalize(str(value)).lower()]
        row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
                             "text": (f"{WONDER} read {build_entry.plain_field(d['field'])} as "
                                      f"{build_entry.plain_value(d['field'], value) if value is not None else 'nothing'}"
                                      + (f", as {' and '.join(agrees)} did" if agrees else ", as neither reader did")
                                      + f"; its reading {'passed' if passed else 'did not pass'} every check. A "
                                        f"disagreement is settled only by a person's fix, so nothing was replaced.")})
        return "evidence"
    if not passed:
        failed = next((c for c in proposal["checks"] if c["result"] == "fail"), None)
        why = (f"{failed['label']} failed: {failed['reason']}" if failed
               else "a check could not run on it")
        row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
                             "text": f"{WONDER}'s reading did not pass every check ({why}). The row stays as it was."})
        return "kept"
    changed = _changes(row, proposal)
    old_check = next(c for c in row["checks"] if c["result"] == "fail")
    for f in FIELDS:
        row[f] = proposal[f]
    row["checks"], row["status"], row["group"] = proposal["checks"], "matched", "corrected"
    row["reason"] = (f"Re-read by {WONDER} after {trig['label']} failed; its reading passed every check.")
    row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
                         "text": f"{WONDER}'s reading passed every check. The row takes it"
                                 + (f" ({'; '.join(changed)})" if changed else "") + "."})
    ids = [int(m["id"][1:]) for m in run["mistakes"] if m["id"][1:].isdigit()]
    run["mistakes"].append({"id": f"m{max(ids, default=0) + 1}", "target": row["id"], "caught_by": "code",
                            "what_changed": (f"{WONDER} re-read the row after {trig['label']} failed: "
                                             + ("; ".join(changed) if changed else "the box") + "."),
                            **({"type": T.check_type(old_check)} if T.check_type(old_check) else {})})
    return "replaced"
