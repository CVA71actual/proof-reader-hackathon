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
    story.append(_p("Companies covered: " + " · ".join(view["companies"]), styles["body"]))
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
    story.append(_p(
        "Kept and corrected rows only, ordered by date. Periods are shown as stated.",
        styles["small"],
    ))
    if not view["calendar"]:
        story.append(_p("No kept or corrected rows.", styles["body"]))
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
            status = row["status"]
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

    # Corrections
    story.append(_p("Corrections", styles["h1"]))
    if not view["corrections"]:
        story.append(_p("None.", styles["body"]))
    else:
        for c in view["corrections"]:
            check = f" · check: {c['check']}" if c.get("check") else ""
            story.append(_p(
                f"{c['id']}: {c['before_after']} (caught by {c['caught_by']}{check})",
                styles["body"],
            ))

    # Excluded
    story.append(_p("Excluded", styles["h1"]))
    if not view["exclusions"]:
        story.append(_p("None.", styles["body"]))
    else:
        for x in view["exclusions"]:
            story.append(_p(f"{x['id']}: {x['rule_words']}", styles["body"]))

    # Still open and unverified
    story.append(_p("Still open and unverified", styles["h1"]))
    if not view["open_rows"]:
        story.append(_p("None.", styles["body"]))
    else:
        for r in view["open_rows"]:
            story.append(_p(
                f"{r['group_label']} · {r['ticker']} · {r['event']}: {r['reason']}",
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
