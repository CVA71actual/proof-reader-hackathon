"""Word writer (python-docx). Same section order as the PDF."""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt


def _set_run_font(run, *, size=9, bold=False, italic=False, name="Helvetica"):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = name
    r = run._element
    rpr = r.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)


def _add_footer(section, text: str) -> None:
    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    run = p.add_run(text)
    _set_run_font(run, size=7)


def _heading(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    _set_run_font(run, size=13, bold=True)
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after = Pt(4)


def _line(doc: Document, text: str, *, size=9, bold=False, italic=False) -> None:
    p = doc.add_paragraph()
    run = p.add_run(text)
    _set_run_font(run, size=size, bold=bold, italic=italic)
    p.paragraph_format.space_after = Pt(2)


def _decision(row: dict) -> str:
    """The person's ruling on an open row, verbatim from report/data.py; nothing when there is none."""
    label = row.get("decision_label")
    return f" · {label}" if label and label != row.get("group_label") else ""


def _check_text(c: dict, *, limitation: bool = True) -> str:
    """One check result as text. A passing date_match is a neutral finding (DATE-LITERAL-001): it prints the
    server's label alone, never a pass verdict, because it compares printed date wording only."""
    if c.get("check_name") == "date_match" and c.get("result") == "pass":
        text = c["name"]
    else:
        text = f"{c['name']}: {c['result']}"
    if limitation and c.get("limitation"):
        text += f" ({c['limitation']})"
    return text


def _context(row: dict) -> str:
    """The row's transported context in plain words: conditions, relevance, subject company, relation."""
    # None = unknown (never established); [] = the reader said there are none (runs/proposal.py).
    conditions = row.get("conditions")
    if conditions is None:
        conds = "not stated"
    else:
        conds = "; ".join(conditions) or "none stated"
    return (f"conditions: {conds} · relevance: {row.get('relevance') or 'not stated'} · "
            f"subject company: {row.get('subject_company') or 'not stated'} · "
            f"relation: {row.get('relation') or 'not stated'}")


def build_docx(view: dict) -> bytes:
    doc = Document()
    section = doc.sections[0]
    section.top_margin = Pt(54)
    section.bottom_margin = Pt(54)
    section.left_margin = Pt(54)
    section.right_margin = Pt(54)
    _add_footer(section, view["footer"])

    # Cover
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.add_run(view["title"])
    _set_run_font(run, size=18, bold=True)

    _line(doc, f"Run {view['run_number']}: {view['run_label']}")
    _line(doc, f"Run date: {view['run_date']}")
    if view.get("as_of_note"):
        _line(doc, view["as_of_note"])
    _line(doc, f"Run status: {view['run_status']}")
    if view.get("research_label"):
        _line(doc, view["research_label"], bold=True)
        for line in view.get("research_lines") or []:
            _line(doc, line, size=8)
    if view.get("models_line"):
        _line(doc, view["models_line"])
    if view["page_outcomes_recorded"]:
        pc = view["page_outcome_counts"]
        _line(doc, "Page outcomes: " + " · ".join(f"{key.replace('_', ' ')} {pc[key]}" for key in (
            "captured", "attempted", "read", "unreadable", "skipped", "unread")))
        for company in view["page_outcomes_by_company"]:
            _line(doc, f"{company['ticker']} page outcomes: captured {company['captured']} · attempted "
                  f"{company['attempted']} · read {company['read']} · unreadable {company['unreadable']} · "
                  f"skipped {company['skipped']} · unread {company['unread']}")
    else:
        _line(doc, "Page outcomes: not recorded for this saved run.")
    _line(doc, "Stopping reason: " + view["stopping_reason"])
    if view["sample_stamp"]:
        _line(doc, view["sample_stamp"], italic=True, size=10)
    _line(doc, f"{view['generated_at']}", size=8)

    _heading(doc, "Summary")
    for key, label in view["group_labels"].items():
        _line(doc, f"{label}: {view['counts'][key]}")
    _line(doc, view["counters_line"])
    _line(doc, f"Unresolved (needs attention): {view['unresolved']}")

    _heading(doc, "Calendar")
    _line(doc, f"Window: {view['calendar_window']}", size=8)
    _line(doc, f"{view['group_labels']['accepted']} — ordered by date. Periods are shown as stated.", size=8)
    if not view["calendar"] and not view.get("past") and not view.get("contingent") and not view.get("undated") and not view.get("timing_unresolved") and not view.get("later"):
        if view.get("no_accepted_rows_text"):
            _line(doc, view["no_accepted_rows_text"])
    else:
        table = doc.add_table(rows=1, cols=6)
        hdr = table.rows[0].cells
        for i, name in enumerate(["Date / period", "Ticker", "Program", "Event", "Source", "Status"]):
            hdr[i].text = name
            for p in hdr[i].paragraphs:
                for r in p.runs:
                    _set_run_font(r, size=8, bold=True)
        for row in view["calendar"]:
            cells = table.add_row().cells
            status = row["review_label"]
            if row.get("contingent"):
                status += " · Contingent"
            if row["person_tag"]:
                status = f"{status} · {row['person_tag']}"
            vals = [
                row["date_display"], row["ticker"], row["program"] or "—",
                row["event"], row["source"], status,
            ]
            for i, v in enumerate(vals):
                cells[i].text = v
                for p in cells[i].paragraphs:
                    for r in p.runs:
                        _set_run_font(r, size=8)
    for key, label in (("past", "Past"), ("undated", "Undated · no timing stated"),
                       ("timing_unresolved", "Timing unresolved"), ("contingent", "Contingent"), ("later", "Later")):
        if view.get(key):
            _heading(doc, f"{label} · {len(view[key])}")
            for row in view[key]:
                _line(doc, f"{row['date_display']} · {row['ticker']} · {row['program']} · {row['event']} · "
                      f"{row['id']} · {row['source_document']} ({row['source_id']}) · page {row['page_number']} · "
                      f"indication: {row['indication'] or 'not stated'} · action: {row['action']} · {_context(row)} · {row['reason']}"
                      + (f" · {row['page_evidence_note']}" if row.get("page_evidence_note") else ""), size=8)
    for row in view["calendar"] + view.get("past", []) + view.get("undated", []) + view.get("timing_unresolved", []) + view.get("contingent", []) + view.get("later", []):
        checks = "; ".join(_check_text(c)
                            for c in row["check_limitations"])
        if not checks and row.get("check_status_text"):
            checks = row["check_status_text"]
        history = "; ".join(_check_text(c)
                              for c in row.get("check_history", []))
        history_text = f" · {row.get('check_history_label') or 'Check history'}: {history}" if history else ""
        _line(doc, f"Evidence {row['id']}: {row['source_document']} ({row['source_id']}) · page {row['page_number']} · "
              f"indication {row['indication'] or 'not stated'} · action {row['action']} · "
              f"quote: {row['quote'] or 'not recorded'} · {_context(row)} · "
              f"current-value checks: {checks or 'not recorded'}"
              f"{history_text}" + (f" · {row['page_evidence_note']}" if row.get("page_evidence_note") else ""), size=8)

    _heading(doc, "Corrections")
    if not view["corrections"]:
        _line(doc, "None.")
    else:
        for c in view["corrections"]:
            check = f" · check: {c['check']}" if c.get("check") else ""
            _line(doc, f"{c['id']} · {c.get('ticker', '')} · {c.get('event', '')}: {c['before_after']} "
                  f"(caught by {c['caught_by']}{check}); "
                  f"latest re-check: {c.get('latest_recheck', 'not recorded')}"
                  + (f"; override reason: {c['override_reason']}" if c.get("override_reason") else ""))

    _heading(doc, "Other edits · no mistake demonstrated")
    if not view.get("edits"):
        _line(doc, "None.")
    else:
        for edit in view["edits"]:
            _line(doc, f"{edit['id']} · {edit.get('ticker', '')} · {edit.get('event', '')}: "
                  f"{edit['before_after']} ({edit['edit_status']}) · re-check: {edit['latest_recheck']}"
                  + (f"; override reason: {edit['override_reason']}" if edit.get("override_reason") else ""))

    _heading(doc, "Excluded")
    if not view["exclusions"]:
        _line(doc, "None.")
    else:
        for x in view["exclusions"]:
            _line(doc, f"{x['id']}: {x['rule_words']}")

    _heading(doc, "Still open and unverified")
    if not view["open_rows"]:
        _line(doc, "None.")
    else:
        for r in view["open_rows"]:
            checks = "; ".join(_check_text(c, limitation=False) for c in r.get("check_limitations", []))
            checks = checks or r.get("check_status_text") or "not recorded"
            history = "; ".join(_check_text(c, limitation=False) for c in r.get("check_history", []))
            history_text = f" · {r.get('check_history_label') or 'Check history'}: {history}" if history else ""
            _line(doc, f"{r['group_label']}{_decision(r)} · {r['ticker']} · {r['company']} · {r['program']} · "
                  f"{r['indication'] or 'indication not stated'} · {r['event']} · {r['date_display']} · "
                  f"source {r['source_document']} ({r['source_id']}) page {r['page_number']} · {_context(r)} · "
                  f"quote: {r['quote']}: {r['reason']}"
                  + (f" · {r['page_evidence_note']}" if r.get("page_evidence_note") else "")
                  + f" · timing: {r.get('timing_state') or 'unresolved'}"
                  + (" · Contingent" if r.get("contingent") else "")
                  + f" · current-value checks: {checks}{history_text}"
                  + (f" · latest re-check: {r['latest_recheck']}" if r.get("latest_recheck") else "")
                  + (f" · override reason: {r['override_reason']}" if r.get("override_reason") else ""))

    _heading(doc, "Sources")
    for s in view["sources"]:
        _line(doc, " · ".join([s["publisher"] or s["id"], s["kind"], s["date"] or "—"]))
        if s["url"]:
            _line(doc, s["url"], size=7)
        _line(doc, f"sha256: {s['sha256']}", size=7)
        if s["rendered_note"]:
            _line(
                doc,
                f"{s['rendered_note']} · source HTML sha256: {s['rendered_from_sha256']} · "
                f"renderer: {s['renderer']}",
                size=8,
            )

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
