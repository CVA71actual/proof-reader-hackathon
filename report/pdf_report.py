"""PDF writer (reportlab). Deterministic aside from the cover generation timestamp."""

from __future__ import annotations

from io import BytesIO

import reportlab.rl_config
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    KeepTogether,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

# Byte-stable PDFs when content matches (cover timestamp is the only intended diff).
# Uncompressed page streams keep the cover timestamp as plain bytes so tests can mask it.
reportlab.rl_config.invariant = 1
reportlab.rl_config.pageCompression = 0
reportlab.rl_config.trustedHosts = []


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "RTitle", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=18, leading=22, spaceAfter=10, alignment=TA_LEFT,
        ),
        "h1": ParagraphStyle(
            "RH1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=13, leading=16, spaceBefore=14, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "RBody", parent=base["BodyText"], fontName="Helvetica",
            fontSize=9, leading=12, spaceAfter=4,
        ),
        "small": ParagraphStyle(
            "RSmall", parent=base["BodyText"], fontName="Helvetica",
            fontSize=8, leading=10, spaceAfter=2, textColor=colors.HexColor("#333333"),
        ),
        "mono": ParagraphStyle(
            "RMono", parent=base["BodyText"], fontName="Courier",
            fontSize=7, leading=9, spaceAfter=2,
        ),
        "stamp": ParagraphStyle(
            "RStamp", parent=base["BodyText"], fontName="Helvetica-Oblique",
            fontSize=10, leading=12, spaceBefore=8, spaceAfter=8,
        ),
    }


def _footer_canvas(view):
    footer = view["footer"]

    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#444444"))
        canvas.drawString(0.75 * inch, 0.45 * inch, footer)
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"page {doc.page}")
        canvas.restoreState()

    return _draw


def _status(row: dict) -> str:
    """CALENDAR-RULE-001: a placed row's server label, then the person's tag when there is one (never "Kept by you"
    for automatic inclusion: the tag is the decision label from report/data.py)."""
    label = row.get("review_label") or ""
    tag = row.get("person_tag")
    return f"{label} · {tag}" if tag and tag != label else label


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


def _p(text: str, style) -> Paragraph:
    safe = (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return Paragraph(safe.replace("\n", "<br/>"), style)


def build_pdf(view: dict) -> bytes:
    styles = _styles()
    buf = BytesIO()
    doc = BaseDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title=view["title"],
        author="built by code from the saved results",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer_canvas(view))])

    story = []
    # Cover
    story.append(_p(view["title"], styles["title"]))
    story.append(_p(f"Run {view['run_number']}: {view['run_label']}", styles["body"]))
    story.append(_p(f"Run date: {view['run_date']}", styles["body"]))
    if view.get("as_of_note"):
        story.append(_p(view["as_of_note"], styles["body"]))
    story.append(_p(f"Run status: {view['run_status']}", styles["body"]))
    if view.get("research_label"):
        story.append(_p(view["research_label"], styles["stamp"]))
        for line in view.get("research_lines") or []:
            story.append(_p(line, styles["small"]))
    if view.get("models_line"):
        story.append(_p(view["models_line"], styles["body"]))
    if view["page_outcomes_recorded"]:
        pc = view["page_outcome_counts"]
        story.append(_p("Page outcomes: " + " · ".join(f"{label} {pc[key]}" for key, label in (
            ("captured", "captured"), ("attempted", "attempted"), ("read", "read"),
            ("unreadable", "unreadable"), ("skipped", "skipped"), ("unread", "unread"))), styles["body"]))
        for company in view["page_outcomes_by_company"]:
            story.append(_p(f"{company['ticker']} page outcomes: captured {company['captured']} · attempted "
                            f"{company['attempted']} · read {company['read']} · unreadable {company['unreadable']} · "
                            f"skipped {company['skipped']} · unread {company['unread']}", styles["small"]))
    else:
        story.append(_p("Page outcomes: not recorded for this saved run.", styles["body"]))
    story.append(_p("Stopping reason: " + view["stopping_reason"], styles["body"]))
    if view["sample_stamp"]:
        story.append(_p(view["sample_stamp"], styles["stamp"]))
    story.append(_p(f"{view['generated_at']}", styles["small"]))
    story.append(Spacer(1, 8))

    # Summary
    story.append(_p("Summary", styles["h1"]))
    for key, label in view["group_labels"].items():
        story.append(_p(f"{label}: {view['counts'][key]}", styles["body"]))
    story.append(_p(view["counters_line"], styles["body"]))
    story.append(_p(f"Unresolved (needs attention): {view['unresolved']}", styles["body"]))

    # Calendar
    story.append(_p("Calendar", styles["h1"]))
    story.append(_p(f"Window: {view['calendar_window']}", styles["small"]))
    story.append(_p(view["calendar_heading"], styles["small"]))
    if not view["calendar"] and not view.get("past") and not view.get("contingent") and not view.get("undated") and not view.get("timing_unresolved") and not view.get("later"):
        if view.get("no_accepted_rows_text"):
            story.append(_p(view["no_accepted_rows_text"], styles["body"]))
    else:
        data = [[
            _p("Date / period", styles["small"]),
            _p("Ticker", styles["small"]),
            _p("Program", styles["small"]),
            _p("Event", styles["small"]),
            _p("Source", styles["small"]),
            _p("Status", styles["small"]),
        ]]
        for row in view["calendar"]:
            status = row["review_label"]
            if row.get("contingent"):
                status += " · Contingent"
            if row["person_tag"]:
                status = f"{status} · {row['person_tag']}"
            data.append([
                _p(row["date_display"], styles["small"]),
                _p(row["ticker"], styles["small"]),
                _p(row["program"] or "—", styles["small"]),
                _p(row["event"], styles["small"]),
                _p(row["source"], styles["small"]),
                _p(status, styles["small"]),
            ])
        table = Table(data, colWidths=[1.05 * inch, 0.55 * inch, 0.8 * inch, 1.5 * inch, 2.0 * inch, 1.1 * inch])
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
    for key, label in view["placement_sections"]:
        if view.get(key):
            story.append(_p(f"{label} · {len(view[key])}", styles["h1"]))
            for row in view[key]:
                story.append(_p(f"{_status(row)} · {row['date_display']} · {row['ticker']} · {row['program']} · {row['event']} · "
                        f"{row['id']} · {row['source_document']} ({row['source_id']}) · page {row['page_number']} · "
                                f"indication: {row['indication'] or 'not stated'} · action: {row['action']} · "
                                f"{_context(row)} · {row['reason']}" + (f" · {row['page_evidence_note']}" if row.get("page_evidence_note") else ""),
                                styles["body"]))
    for row in view["calendar"] + view.get("past", []) + view.get("undated", []) + view.get("timing_unresolved", []) + view.get("contingent", []) + view.get("later", []):
        checks = "; ".join(_check_text(c)
                            for c in row["check_limitations"])
        if not checks and row.get("check_status_text"):
            checks = row["check_status_text"]
        history = "; ".join(_check_text(c)
                              for c in row.get("check_history", []))
        history_text = f" · {row.get('check_history_label') or 'Check history'}: {history}" if history else ""
        story.append(_p(f"Evidence {row['id']}: {row['source_document']} ({row['source_id']}) · page {row['page_number']} · "
                        f"indication {row['indication'] or 'not stated'} · action {row['action']} · "
                        f"quote: {row['quote'] or 'not recorded'} · {_context(row)} · "
                        f"current-value checks: {checks or 'not recorded'}"
                        f"{history_text}" + (f" · {row['page_evidence_note']}" if row.get("page_evidence_note") else ""),
                        styles["small"]))

    # Corrections
    story.append(_p("Corrections", styles["h1"]))
    if not view["corrections"]:
        story.append(_p("None.", styles["body"]))
    else:
        for c in view["corrections"]:
            check = f" · check: {c['check']}" if c.get("check") else ""
            story.append(_p(
                f"{c['id']} · {c.get('ticker', '')} · {c.get('event', '')}: {c['before_after']} "
                f"(caught by {c['caught_by']}{check}); "
                f"latest re-check: {c.get('latest_recheck', 'not recorded')}"
                + (f"; override reason: {c['override_reason']}" if c.get("override_reason") else ""),
                styles["body"],
            ))

    story.append(_p("Other edits · no mistake demonstrated", styles["h1"]))
    if not view.get("edits"):
        story.append(_p("None.", styles["body"]))
    else:
        for edit in view["edits"]:
            story.append(_p(f"{edit['id']} · {edit.get('ticker', '')} · {edit.get('event', '')}: "
                            f"{edit['before_after']} ({edit['edit_status']}) · re-check: {edit['latest_recheck']}"
                            + (f"; override reason: {edit['override_reason']}" if edit.get("override_reason") else ""),
                            styles["body"]))

    # Excluded
    story.append(_p("Excluded", styles["h1"]))
    if not view["exclusions"]:
        story.append(_p("None.", styles["body"]))
    else:
        for x in view["exclusions"]:
            story.append(_p(f"{x['id']}: {x['rule_words']}", styles["body"]))

    # Still open and unverified
    story.append(_p(view["needs_review_heading"], styles["h1"]))
    if not view["open_rows"]:
        story.append(_p("None.", styles["body"]))
    else:
        for r in view["open_rows"]:
            checks = "; ".join(_check_text(c, limitation=False) for c in r.get("check_limitations", []))
            checks = checks or r.get("check_status_text") or "not recorded"
            history = "; ".join(_check_text(c, limitation=False) for c in r.get("check_history", []))
            history_text = f" · {r.get('check_history_label') or 'Check history'}: {history}" if history else ""
            story.append(_p(
                f"{r['group_label']}{_decision(r)} · {r['ticker']} · {r['company']} · {r['program']} · "
                f"{r['indication'] or 'indication not stated'} · {r['event']} · {r['date_display']} · "
                f"source {r['source_document']} ({r['source_id']}) page {r['page_number']} · {_context(r)} · "
                f"quote: {r['quote']}: {r['reason']}"
                + (f" · Issues: {'; '.join(r['issues'])}" if r.get("issues") else "")
                + (f" · {r['page_evidence_note']}" if r.get("page_evidence_note") else "")
                + f" · timing: {r.get('timing_state') or 'unresolved'}"
                + (" · Contingent" if r.get("contingent") else "")
                + f" · current-value checks: {checks}{history_text}"
                + (f" · latest re-check: {r['latest_recheck']}" if r.get("latest_recheck") else "")
                + (f" · override reason: {r['override_reason']}" if r.get("override_reason") else ""),
                styles["body"],
            ))

    # Sources
    story.append(_p("Sources", styles["h1"]))
    for s in view["sources"]:
        block = []
        bits = [
            s["publisher"] or s["id"],
            s["kind"],
            s["date"] or "—",
        ]
        block.append(_p(" · ".join(bits), styles["body"]))
        if s["url"]:
            block.append(_p(s["url"], styles["mono"]))
        block.append(_p(f"sha256: {s['sha256']}", styles["mono"]))
        if s["rendered_note"]:
            block.append(_p(
                f"{s['rendered_note']} · source HTML sha256: {s['rendered_from_sha256']} · "
                f"renderer: {s['renderer']}",
                styles["small"],
            ))
        story.append(KeepTogether(block))

    doc.build(story)
    return buf.getvalue()
