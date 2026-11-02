"""One bounded mechanical date repair, never a replacement interpretation.

Muse Wonder proposes date fields against the unchanged original quote and page.
Code compares the same mechanical check before/after. Alternate passages or
interpretations stay evidence for a person, and every repaired row still needs
human acceptance. The runner owns the existing one-attempt and spending gates.
"""

from __future__ import annotations

from copy import deepcopy
import json

from accuracy import types as T
from capture import read_events, read_table
from checks import event_checks
from checks.common import normalize
from checks.selector_match import _quote_run
from runs import build_entry, prices
from runs.proposal import CONTEXT_FIELDS, context_steps, normalized_context
from runs.state import _real_disagreement

WONDER = "Muse Wonder"
WHO = "runs.escalate"
WONDER_WHO = "capture.wonder"
MAX_PER_PAGE = 1     # rows sent to Wonder from one page: each is a counted, priced turn
WONDER_SHARE = 0.25  # Wonder's part of a limited run's spending limit: past it nothing more is sent
SENT_FOR = (event_checks.DATE_LABEL,)   # the failures worth Wonder's price
PAD_PT = 60.0        # how far above and below the row the region reaches, in PDF points
CHARS_PER_TOKEN = 4  # a rough count of prompt tokens from text, for the estimate made before a call
BYTES_PER_IMAGE_TOKEN = 750  # the same for an image, from its PNG size


def estimate_usd(prompt_text: str, image: bytes | None) -> float:
    """What a Wonder call is likely to cost before it is sent: the prompt's size at Muse Wonder's input price.
    Only the input is counted, so it is a floor; the reply's real cost is added once it comes."""
    tokens = len(prompt_text) / CHARS_PER_TOKEN + (len(image) / BYTES_PER_IMAGE_TOKEN if image else 0)
    return tokens * prices.PRICES_PER_1M_USD["Muse Wonder"]["input"] / 1_000_000



FIELDS = ("event_type", "title", "date_text", "date", "date_end", "quote", "program", "indication", "box") + CONTEXT_FIELDS


def candidates(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("found_by") == "coverage" or r.get("status") == "unreadable" or r.get("group") == "removed":
            continue
        status = event_checks.row_status(r.get("checks") or [])
        if status == event_checks.FAILED or r.get("disagreement"):
            out.append(r)
    return out


def budgeted(rows: list[dict]) -> list[dict]:
    """The candidates Wonder is worth sending: a failed literal date check. A box or quote failure is mended by
    code (runs.build_entry.locate_box) or is a paraphrase Wonder cannot cure; a disagreement is settled only by a person,
    so Wonder's reading would be evidence only."""
    return [r for r in candidates(rows) if event_checks.row_status(r.get("checks") or []) == event_checks.FAILED
            and any(c["result"] == "fail" and c["label"] in SENT_FOR for c in r.get("checks", []))]


def trigger(row: dict, labels: tuple[str, ...] | None = None) -> dict:
    """{kind, label, why}: the first failed check (of `labels`, if given), else the readers' disagreement."""
    for c in row.get("checks", []):
        if c.get("result") == "fail" and (labels is None or c.get("label") in labels):
            return {"kind": "check", "label": c.get("label"), "why": c.get("reason")}
    d = row.get("disagreement")
    if not d:
        return {"kind": "none", "label": None, "why": None}
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
           types: tuple[str, ...] = read_events.EVENT_TYPES, note: str | None = None,
           *, row: dict | None = None) -> str:
    target = {key: (row or {}).get(key) for key in
              ("id", "source_id", "page_number", "quote", "date_text", "date", "date_end")}
    return (f"Repair only the date fields of this existing proposal. Do not rewrite the event.\n"
            f"The image is a horizontal band from {y_range[0]:.0f} to {y_range[1]:.0f} points down a page "
            f"{words_doc['page_width']:.0f} points wide and {words_doc['page_height']:.0f} tall; "
            f"it is not the whole page.\n"
            f"The mechanical check was {trig['label']}: {trig['why']}\n"
            "Read the printed date in the ORIGINAL quotation from the image. Copy the id, source_id, "
            "page_number and quote exactly. Keep printed words in order; line breaks may become spaces. "
            "Do not use a neighboring sentence or infer which event a date belongs to. "
            "If the original passage does not supply a usable date, return an empty events list.\n"
            "Return JSON only: {\"events\": [{\"id\": \"...\", \"source_id\": \"...\", "
            "\"page_number\": 1, \"quote\": \"original passage\", \"date_text\": \"printed date wording\", "
            "\"date\": \"YYYY-MM-DD\", \"date_end\": null, \"box\": [x0,y0,x1,y1]}]}. "
            "date is the exact day, or the first day of a period; date_end is its last day (null for an exact day). "
            "For a quarter use its first day through its last day, never an invented day inside it. "
            "If bounds cannot be resolved, use null. Boxes use a 0 to 1000 grid over this band image, "
            "ordered left, top, right, bottom; null if unavailable. The box is a proposal, not a geometry check. "
            "Do not return title, type, program, indication, relevance, conditions, subject or relationship.\n"
            f"Filing reference date: {entry.get('filing_date') or entry.get('published_date')}.\n"
            "Original proposal: " + json.dumps(target, ensure_ascii=False))


def pick(row: dict, events: list[dict]) -> dict | None:
    """Explicit target or exact passage only. Ambiguous answers never pick the first.

    An explicitly targeted different passage reaches apply as retained evidence,
    never as permission to replace the original event quote.
    """
    matches = [e for e in events if
               (e.get("id") == row.get("id") and row.get("id") is not None)
               or ("id" not in e and isinstance(e.get("quote"), str)
                   and e["quote"].split() == (row.get("quote") or "").split())]
    return matches[0] if len(matches) == 1 else None


def to_page_points(event: dict, y_range: tuple[float, float], words_doc: dict) -> dict:
    """The band's 0-1000 grid box -> PDF points on the page (the band is the full width)."""
    box = read_events.grid_to_points(event.get("box"), float(words_doc["page_width"]), y_range[1] - y_range[0])
    if box is not None:
        box = [box[0], round(box[1] + y_range[0], 2), box[2], round(box[3] + y_range[0], 2)]
    return {**event, "box": box}


def _changes(old: dict, new: dict) -> list[str]:
    return [f"{build_entry.plain_field(f)} from {build_entry.plain_value(f, old.get(f))} to "
            f"{build_entry.plain_value(f, new.get(f))}" for f in FIELDS
            if f != "box" and f not in CONTEXT_FIELDS and normalize(None if old.get(f) is None else str(old.get(f)))
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
          run: dict, share: tuple, prompt_text: str, consumed_artifact: dict | None = None) -> str:
    """Apply date fields only after the same current comparison fails then passes.

    Raw model output stays separate from the copied proposal used for checking.
    A new or changed interpretation is never applied or credited as a mistake.
    """
    from runs import recheck
    from runs.build_run import to_contract_check

    record_sent(row, (reading.get("usage") or {}).get("prompt_tokens"), at, trig, prompt_text)
    row["escalation_proposal"] = {"model": WONDER, "at": at, "source_id": source.get("id"),
        "page_number": row.get("page_number"),
        "proposal": deepcopy(event if event is not None else {"events": reading.get("events")}),
        "raw_response": reading.get("raw_response")}
    row["trail"].append({"step": WONDER, "tag": "Model", "who": WONDER, "at": at,
        "text": ("No unambiguous date repair proposal was returned." if event is None else
                 f"Proposed date wording: {event.get('date_text') or 'not supplied'}; "
                 f"date: {event.get('date') or 'not supplied'}; "
                 f"end date: {event.get('date_end') or 'not supplied'}. "
                 f"Quotation: {event.get('quote') or 'not supplied'}"),
        **prices.model_cost_fields(WONDER, (reading.get("usage") or {}).get("prompt_tokens"),
                                  (reading.get("usage") or {}).get("completion_tokens"))})

    def hold(reason, outcome="kept"):
        row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
            "text": reason + " The row stays as it was; a person must review it. No mistake is counted."})
        return outcome

    if event is None:
        return hold("No unambiguous answer for this row.", "unreadable")
    if trig.get("kind") == "disagreement" or _real_disagreement(row.get("disagreement")):
        return hold("A model repair cannot settle the readers' disagreement.", "evidence")
    if trig.get("label") not in SENT_FOR:
        return hold("This is not an applicable mechanical date repair.")
    if source.get("id") != row.get("source_id") or not any(
            p.get("page_number") == row.get("page_number") for p in source.get("pages", [])):
        return hold("The checked source/page is not the original row's source/page.")
    for field in ("id", "source_id", "page_number"):
        if field in event and (type(event[field]) is not type(row.get(field)) or event[field] != row.get(field)):
            return hold(f"The answer changes {field}; it is alternative evidence only.")
    if not isinstance(event.get("quote"), str) or not event["quote"].strip() or (
            event["quote"].split() != (row.get("quote") or "").split()):
        return hold("The answer uses a different passage; a timing sentence cannot replace the event quotation.")

    # Omitted fields were not requested. Explicit changes (including malformed
    # values) are retained, not silently joined onto the original interpretation.
    interpretation = ("event_type", "title", "program", "indication", "ticker", "company") + CONTEXT_FIELDS
    changes = [{"field": f, "before": deepcopy(row.get(f)), "after": deepcopy(event[f])}
               for f in interpretation if f in event
               and normalized_context(event[f]) != normalized_context(row.get(f))]
    row["escalation_context_changes"] = changes
    row["trail"] += context_steps(event, at)
    if changes:
        return hold("The answer changes interpretation (" + "; ".join(
            f"{c['field']}: {c['before']!r} -> {c['after']!r}" for c in changes) + ").")
    if any(f not in event or (event[f] is not None and not isinstance(event[f], str))
           for f in recheck.DATE_FIELDS):
        return hold("The answer did not supply all requested date fields in usable shapes.")
    proposal = deepcopy(row)
    for field in recheck.DATE_FIELDS:
        proposal[field] = event[field]
    changed = _changes(row, proposal)
    if not changed:
        return hold("The answer changes no requested date value.")

    before = [to_contract_check(c.to_dict()) for c in recheck.row_checks(
        row, source, words_doc, consumed_artifact=consumed_artifact)]
    after = [to_contract_check(c.to_dict()) for c in recheck.row_checks(
        proposal, source, words_doc, consumed_artifact=consumed_artifact)]
    old_check = next(c for c in before if c["name"] == "date_match")
    new_check = next(c for c in after if c["name"] == "date_match")
    row["escalation_proposal"]["comparison"] = {"before": old_check, "after": new_check}
    row["trail"] += [{"step": "Checked", "tag": "Code", "who": WHO, "at": at,
                      "text": f"{c['label']}: {c['result']}. {c['reason']}"} for c in after]
    if old_check["result"] != "fail" or new_check["result"] != "pass":
        return hold("The same current date comparison did not change from fail to pass.")
    if event_checks.row_status(after) != event_checks.MATCHED:
        return hold("The proposed repair did not satisfy the remaining mechanical checks.")
    for field in recheck.DATE_FIELDS:
        row[field] = proposal[field]
    row["checks"] = after
    row["status"], row["group"] = "matched", "corrected"
    row["reason"] = f"{WONDER} repaired a printed date comparison. Event meaning still needs your review."
    row["trail"].append({"step": "Escalation result", "tag": "Code", "who": WHO, "at": at,
        "text": "Applied only the date repair (" + "; ".join(changed)
                + "). The original event quotation and interpretation are unchanged. Human acceptance is still required."})
    mistakes = run.setdefault("mistakes", [])
    ids = [int(m["id"][1:]) for m in mistakes if m["id"][1:].isdigit()]
    mistakes.append({"id": f"m{max(ids, default=0) + 1}", "target": row["id"], "caught_by": "code",
        "what_changed": f"{WONDER} repaired the same failed date comparison: " + "; ".join(changed) + ".",
        "type": T.mistake_type(old_check, T.WRONG_VALUE)})
    return "replaced"
