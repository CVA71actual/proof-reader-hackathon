"""Capture one page of a pinned source: check the source file against the pin, cut out the page, record every word.

Usage (from the repo root):
    python -m capture.capture_source [--pin ptgx_8k_2026-08-05_ex991_p3] [--source PATH]

This is capture_page generalised to any pinned source, including one EDGAR holds as HTML only. Such a source is
rendered to PDF by code (the pin names the renderer and both fingerprints: the filed HTML and the rendered PDF);
the page in fixtures/ is cut from that rendered PDF, and every screen says "page rendered by code from the filed
HTML".

Writes into fixtures/:
    <pin>.pdf          the single extracted page
    <pin>.words.json   every word on the page with its box in PDF points
    MANIFEST.json      under "entries": {<pin>: ...}: where the page came from and the hashes of what was written

No network is used. Neither the filed HTML nor the full rendered PDF goes into the repo.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from capture.capture_page import extract_page, page_words, sha256_file

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
MANIFEST_FILE = "MANIFEST.json"
DOCS = Path.home() / "proof-reader-docs"

# A drawn/image/text box that extends past the page (beyond this slack in PDF points) is clipped.
CLIP_SLACK_PT = 1.0
# Default get_text("words") clips to the page; off-page text is invisible to that call. Use an unbounded
# clip so geometry reflects what the PDF actually holds (MuPDF still has the glyphs).
_UNBOUNDED = pymupdf.Rect(-1e5, -1e5, 1e5, 1e5)
# CSS that can discard layout before print — surviving PDF geometry cannot show retention.
_HTML_CLIP_CSS = re.compile(
    r"overflow\s*:\s*(hidden|clip)\b|clip-path\s*:|clip\s*:\s*rect\s*\(",
    re.IGNORECASE,
)
# Fields that form the one capture-completeness record (written once, copied forward — never reconstructed).
COMPLETENESS_KEYS = (
    "capture_incomplete",
    "capture_incomplete_reason",
    "certified_complete",
    "completeness_not_established",
    "completeness_reason",
)


def completeness_fields(src: dict) -> dict:
    """Copy the completeness record from words or a page entry (authoritative fields only)."""
    return {k: src[k] for k in COMPLETENESS_KEYS if k in src}


def page_content_bbox(page: pymupdf.Page) -> pymupdf.Rect | None:
    """Union of text (including off-page glyphs), image, and path bounds, or None when empty."""
    boxes: list[pymupdf.Rect] = []
    for w in page.get_text("words", clip=_UNBOUNDED) or []:
        boxes.append(pymupdf.Rect(w[:4]))
    for info in page.get_image_info() or []:
        bbox = info.get("bbox")
        if bbox:
            boxes.append(pymupdf.Rect(bbox))
    for d in page.get_drawings() or []:
        r = d.get("rect")
        if r:
            boxes.append(pymupdf.Rect(r))
    if not boxes:
        return None
    out = pymupdf.Rect(boxes[0])
    for b in boxes[1:]:
        out |= b
    return out


def page_clip_reason(pdf: Path, slack_pt: float = CLIP_SLACK_PT) -> str | None:
    """If any page's content box extends past the rendered page box, return a plain reason; else None.

    Uses unbounded text extraction so text positioned outside the page is not mistaken for absence.
    """
    doc = pymupdf.open(pdf)
    try:
        for i in range(doc.page_count):
            page = doc[i]
            content = page_content_bbox(page)
            if content is None:
                continue
            pr = page.rect
            overflow_right = content.x1 - pr.x1
            overflow_left = pr.x0 - content.x0
            overflow_bottom = content.y1 - pr.y1
            overflow_top = pr.y0 - content.y0
            if max(overflow_right, overflow_left, overflow_bottom, overflow_top) <= slack_pt:
                continue
            parts = []
            if overflow_right > slack_pt:
                parts.append(f"right by {overflow_right:.1f}pt")
            if overflow_left > slack_pt:
                parts.append(f"left by {overflow_left:.1f}pt")
            if overflow_bottom > slack_pt:
                parts.append(f"bottom by {overflow_bottom:.1f}pt")
            if overflow_top > slack_pt:
                parts.append(f"top by {overflow_top:.1f}pt")
            where = f"page {i + 1} of {pdf.name}" if doc.page_count > 1 else pdf.name
            return (f"Content bounding box extends past the rendered page box on {where} "
                    f"({', '.join(parts)}; content "
                    f"[{content.x0:.1f},{content.y0:.1f},{content.x1:.1f},{content.y1:.1f}] vs page "
                    f"[{pr.x0:.1f},{pr.y0:.1f},{pr.x1:.1f},{pr.y1:.1f}]).")
    finally:
        doc.close()
    return None


def html_has_clipping_css(html_text: str | None) -> bool:
    """True when the source HTML can discard laid-out content before print (overflow/clip)."""
    return bool(html_text and _HTML_CLIP_CSS.search(html_text))


def assess_capture_completeness(
    page_pdf: Path,
    *,
    html_text: str | None = None,
    slack_pt: float = CLIP_SLACK_PT,
) -> dict:
    """One completeness record for a captured page.

    Overflow measured at the page boundary -> capture_incomplete. Anything else -> completeness_not_established.
    Completeness is never inferred from absence: a CSS clipping blacklist (inline, linked or otherwise) finding
    nothing establishes nothing, and discarded content cannot be recovered from the surviving PDF. Nothing here sets
    certified_complete=True; there is no positive completeness signal available at this layer.
    """
    clip_reason = page_clip_reason(page_pdf, slack_pt=slack_pt)
    if clip_reason:
        return {
            "capture_incomplete": True,
            "certified_complete": False,
            "capture_incomplete_reason": clip_reason,
        }
    if html_has_clipping_css(html_text):
        reason = (
            "Source HTML uses CSS clipping (overflow:hidden/clip or clip-path). Content discarded "
            "before PDF generation cannot be recovered from surviving PDF objects. No page-boundary "
            "overflow was detected; that does not establish a complete capture."
        )
    else:
        reason = (
            "No page-boundary overflow was detected, which does not establish a complete capture "
            "(clipping can come from inline or linked CSS or elsewhere and is not reliably detectable). "
            "Completeness is not established."
        )
    return {
        "completeness_not_established": True,
        "certified_complete": False,
        "completeness_reason": reason,
    }


def apply_capture_completeness(
    words: dict,
    page_pdf: Path | None = None,
    *,
    html_text: str | None = None,
    slack_pt: float = CLIP_SLACK_PT,
    assessment: dict | None = None,
) -> dict:
    """Write the one completeness record into the words doc.

    Pass `assessment` to reuse an already-computed record (pinned capture computes it once).
    """
    fields = assessment if assessment is not None else assess_capture_completeness(
        page_pdf, html_text=html_text, slack_pt=slack_pt)
    words = dict(words)
    for k in COMPLETENESS_KEYS:
        words.pop(k, None)
    words.update(fields)
    return words

# One entry per pinned page. The pin record is orders/R4A_PIN_PTGX_2026-10-28.md in the ops record.
PINS = {
    "ptgx_8k_2026-08-05_ex991_p3": {
        "ticker": "PTGX",
        "company": "Protagonist Therapeutics, Inc.",
        "cik": "1377121",
        "form": "8-K",
        "exhibit": "99.1",
        "exhibit_title": "Second quarter 2026 financial results press release",
        "filing_date": "2026-08-05",
        "accession": "0001104659-26-091099",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1377121/000110465926091099/tm2622272d1_ex99-1.htm",
        "source_format": "html",
        "source_sha256": "ecfd2d79a6e812707d22a9f9a5b86ab1ff7dcc4e8bd9aa2d4c0c7636a7280d57",
        "rendered_pdf_sha256": "4ba68e37da0c15348d3eb9c3164a8f477ca81fa42d570fd648961e69cb5a1c50",
        "renderer": "Google Chrome 156.0.8078.48 headless print-to-pdf",
        "rendered_page_count": 6,
        "page_number": 3,  # 1-based, in the rendered PDF
        "default_source": DOCS / "r4a" / "ptgx_ex991.pdf",
    },
}


def capture(pin_id: str, source: Path | None = None, fixtures: Path = FIXTURES) -> dict:
    pin = PINS[pin_id]
    source = Path(source or pin["default_source"])
    if not source.exists():
        raise SystemExit(f"Source file not found: {source}")
    want = pin.get("rendered_pdf_sha256") or pin["source_sha256"]
    got = sha256_file(source)
    if got != want:
        raise SystemExit(
            "The source file is not the pinned one (its fingerprint differs). "
            f"Expected {want}, got {got}."
        )
    src = pymupdf.open(source)
    count = src.page_count
    src.close()
    expected_pages = pin.get("rendered_page_count") or pin.get("source_page_count")
    if count != expected_pages:
        raise SystemExit(f"Expected {expected_pages} pages, found {count}.")

    fixtures.mkdir(parents=True, exist_ok=True)
    page_file, words_file = f"{pin_id}.pdf", f"{pin_id}.words.json"
    page_pdf = fixtures / page_file
    extract_page(source, pin["page_number"] - 1, page_pdf)
    completeness = assess_capture_completeness(page_pdf)
    # Pinned fixture words stay byte-stable (their hash is pinned); the one assessment, computed above,
    # is carried on the manifest entry instead.
    words = page_words(page_pdf, pin["page_number"])
    words_path = fixtures / words_file
    words_path.write_text(json.dumps(words, indent=1) + "\n")

    entry = {k: v for k, v in pin.items() if k != "default_source"}
    entry.update({
        "page_file": page_file,
        "page_sha256": sha256_file(page_pdf),
        "words_file": words_file,
        "words_sha256": sha256_file(words_path),
        "word_count": len(words["words"]),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capture_tool": f"pymupdf {pymupdf.VersionBind}",
    })
    entry.update(completeness)
    manifest_path = fixtures / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.setdefault("entries", {})[pin_id] = entry
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return entry


def load_entry(pin_id: str, fixtures: Path = FIXTURES) -> dict:
    """The manifest entry of a captured pinned page."""
    return json.loads((fixtures / MANIFEST_FILE).read_text())["entries"][pin_id]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pin", default="ptgx_8k_2026-08-05_ex991_p3", choices=sorted(PINS))
    ap.add_argument("--source", type=Path, default=None, help="path to the full (rendered) source PDF")
    args = ap.parse_args(argv)
    e = capture(args.pin, args.source)
    print(f"Captured page {e['page_number']} of {e['rendered_page_count']}: {e['word_count']} words.")
    print(f"Page fingerprint {e['page_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
