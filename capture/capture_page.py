"""Capture one filing page: check the source file, cut out the page, record every word.

Usage (from the repo root):
    python -m capture.capture_page [--source PATH]

Writes into fixtures/:
    ions_ars_2026_p95.pdf          the single extracted page
    ions_ars_2026_p95.words.json   every word on the page with its box in PDF points
    MANIFEST.json                  where the page came from and the hashes of what was written

No network is used. The full filing PDF never goes into the repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"

DEFAULT_SOURCE = Path.home() / "proof-reader-docs" / "s1" / "IONS_ARS_2026-04-23.pdf"

FILING = {
    "ticker": "IONS",
    "company": "Ionis Pharmaceuticals, Inc.",
    "form": "ARS",
    "filing_date": "2026-04-23",
    "accession": "0001140361-26-016395",
    "source_url": "https://www.sec.gov/Archives/edgar/data/874015/000114036126016395/ny20063671x3_ars.pdf",
    "source_sha256": "aabcaf5ef7714c9390fc5bd135d9ab0f5e77ffc559241ad4b5427802ac2f09a2",
    "source_page_count": 144,
    "page_number": 95,  # 1-based
    "page_title": "Consolidated Statements of Operations",
}

PAGE_FILE = "ions_ars_2026_p95.pdf"
WORDS_FILE = "ions_ars_2026_p95.words.json"
MANIFEST_FILE = "MANIFEST.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_page(source: Path, page_index: int, out_pdf: Path) -> None:
    src = pymupdf.open(source)
    out = pymupdf.open()
    out.insert_pdf(src, from_page=page_index, to_page=page_index)
    out.set_metadata({})
    out.save(out_pdf, garbage=4, deflate=True, no_new_id=True, reproducible=True)
    out.close()
    src.close()


def page_words(pdf: Path, page_number: int | None = None) -> dict:
    """Every word on the (single) page, in the PDF's own reading order. `page_number`: the page's number in
    the full source (default: the pinned IONS page)."""
    return page_words_bytes(Path(pdf).read_bytes(), page_number)


def page_words_bytes(pdf_bytes: bytes, page_number: int | None = None) -> dict:
    """Extract words from the exact PDF byte snapshot supplied by the caller."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    raw = page.get_text("words", sort=False)
    words = []
    for i, w in enumerate(raw):
        x0, y0, x1, y1, text, block_no, line_no, word_no = w[:8]
        words.append(
            {
                "id": f"w{i}",
                "text": text,
                "x0": round(x0, 2),
                "y0": round(y0, 2),
                "x1": round(x1, 2),
                "y1": round(y1, 2),
                "block": block_no,
                "line": line_no,
                "word": word_no,
            }
        )
    result = {
        "page_number": FILING["page_number"] if page_number is None else page_number,
        "page_width": round(page.rect.width, 2),
        "page_height": round(page.rect.height, 2),
        "units": "PDF points, origin top-left, y grows downward",
        "words": words,
    }
    doc.close()
    return result


def capture(source: Path, fixtures: Path = FIXTURES) -> dict:
    if not source.exists():
        raise SystemExit(f"Source file not found: {source}")
    got = sha256_file(source)
    if got != FILING["source_sha256"]:
        raise SystemExit(
            "The source file is not the pinned filing (its fingerprint differs). "
            f"Expected {FILING['source_sha256']}, got {got}."
        )
    src = pymupdf.open(source)
    if src.page_count != FILING["source_page_count"]:
        raise SystemExit(f"Expected {FILING['source_page_count']} pages, found {src.page_count}.")
    src.close()

    fixtures.mkdir(parents=True, exist_ok=True)
    page_pdf = fixtures / PAGE_FILE
    extract_page(source, FILING["page_number"] - 1, page_pdf)

    words = page_words(page_pdf)
    words_path = fixtures / WORDS_FILE
    words_path.write_text(json.dumps(words, indent=1) + "\n")

    manifest = dict(FILING)
    manifest.update(
        {
            "page_file": PAGE_FILE,
            "page_sha256": sha256_file(page_pdf),
            "words_file": WORDS_FILE,
            "words_sha256": sha256_file(words_path),
            "word_count": len(words["words"]),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "capture_tool": f"pymupdf {pymupdf.VersionBind}",
        }
    )
    old = fixtures / MANIFEST_FILE
    if old.exists() and "entries" in json.loads(old.read_text()):
        manifest["entries"] = json.loads(old.read_text())["entries"]  # pages captured by capture_source
    (fixtures / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="path to the full filing PDF")
    args = ap.parse_args(argv)
    m = capture(args.source)
    print(f"Captured page {m['page_number']} of {m['source_page_count']}: {m['word_count']} words.")
    print(f"Page fingerprint {m['page_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
