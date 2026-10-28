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
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from capture.capture_page import extract_page, page_words, sha256_file

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
MANIFEST_FILE = "MANIFEST.json"
DOCS = Path.home() / "proof-reader-docs"

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
