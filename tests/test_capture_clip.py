"""Astra R2-05 / Job 1b: wide exhibits unclipped; completeness not certified from silence; flags carried."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from capture import fetch_filings as ff
from capture import index_filings as idx
from capture.capture_source import (
    apply_capture_completeness,
    assess_capture_completeness,
    completeness_fields,
    page_clip_reason,
)


def _wide_png(path: Path, width: int = 1600, height: int = 400) -> None:
    """A synthetic wide RGB PNG (wider than Letter at 96dpi) with a red right-edge stripe."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter None
        for x in range(width):
            raw.extend((220, 40, 40) if x >= width - 80 else (40, 80, 180))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def test_wide_synthetic_table_renders_unclipped(tmp_path):
    """A wide image exhibit renders on a paper wider than Letter; content stays inside the page box."""
    try:
        chrome = ff.find_chrome()
    except ff.FetchError:
        pytest.skip("Chrome not installed")

    img = tmp_path / "wide_slide.png"
    _wide_png(img, width=1600, height=400)
    html = tmp_path / "wide_ex99-1.htm"
    html.write_text(
        "<html><head><title>wide</title></head><body>"
        f'<p><img src="{img.name}" alt="timeline 2028 Crohn endpoint"></p>'
        "<table style='width:1400px;border-collapse:collapse'>"
        "<tr><td>2025</td><td>2026</td><td>2027</td><td>2028 Crohn endpoint</td></tr>"
        "</table>"
        "</body></html>",
        encoding="utf-8",
    )
    w_in, _h_in = ff.paper_size_for_html(html)
    assert w_in > ff.LETTER_WIDTH_IN

    pdf = tmp_path / "wide_ex99-1.pdf"
    ff.render_html_to_pdf(html, pdf, chrome)
    doc = pymupdf.open(pdf)
    assert doc.page_count >= 1
    assert doc[0].rect.width > 620
    doc.close()

    assert page_clip_reason(pdf) is None
    pages = ff.cut_pages(pdf, tmp_path, "wide_ex99-1", html_text=html.read_text(encoding="utf-8"))
    assert pages[0].get("capture_incomplete") is not True
    # No overflow found is not completeness: never certified from absence.
    assert pages[0].get("certified_complete") is False
    assert pages[0].get("completeness_not_established") is True
    words = json.loads((tmp_path / pages[0]["words_file"]).read_text())
    assert words.get("capture_incomplete") is not True
    assert words.get("certified_complete") is False
    assert words.get("completeness_not_established") is True


def test_forced_clip_is_flagged_never_silent(tmp_path):
    """A page whose content box extends past the page is capture_incomplete; never certified complete."""
    pdf = tmp_path / "clipped.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.draw_rect(pymupdf.Rect(10, 10, 280, 80), color=(1, 0, 0), fill=(1, 0, 0))
    page.insert_text((210, 40), "CLIPPED-RIGHT-EDGE", fontsize=12)
    doc.save(pdf)
    doc.close()

    reason = page_clip_reason(pdf)
    assert reason is not None
    assert "right" in reason.lower()

    words = apply_capture_completeness(
        {"page_number": 1, "page_width": 200, "page_height": 200, "units": "pt", "words": []},
        pdf,
    )
    assert words["capture_incomplete"] is True
    assert words["certified_complete"] is False
    assert "right" in words["capture_incomplete_reason"].lower()


def test_text_positioned_outside_page_is_incomplete(tmp_path):
    """Off-page text (invisible to default clipped get_text) still makes capture_incomplete."""
    pdf = tmp_path / "text-outside.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((20, 40), "VISIBLE", fontsize=12)
    page.insert_text((210, 40), "LOST-ENDPOINT", fontsize=12)
    doc.save(pdf)
    doc.close()

    # Default extraction hides the off-page word; unbounded geometry must still catch it.
    doc = pymupdf.open(pdf)
    assert [w[4] for w in doc[0].get_text("words")] == ["VISIBLE"]
    doc.close()

    reason = page_clip_reason(pdf)
    assert reason is not None and "right" in reason.lower()
    fields = assess_capture_completeness(pdf)
    assert fields["capture_incomplete"] is True
    assert fields["certified_complete"] is False


def test_overflow_hidden_html_is_not_certified_complete(tmp_path):
    """CSS overflow:hidden can drop content before print — no page overflow ≠ complete."""
    try:
        chrome = ff.find_chrome()
    except ff.FetchError:
        pytest.skip("Chrome not installed")

    html = tmp_path / "clip_ex99-1.htm"
    html.write_text(
        "<html><head><title>clipped</title></head><body>"
        '<div style="width:200px;overflow:hidden;border:1px solid #000">'
        '<span style="display:inline-block;width:500px;padding-left:500px">'
        "HIDDEN-ENDPOINT-2028</span></div>"
        "<p>visible label</p>"
        "</body></html>",
        encoding="utf-8",
    )
    pdf = tmp_path / "clip_ex99-1.pdf"
    ff.render_html_to_pdf(html, pdf, chrome)
    # Geometry alone may report no overflow (content never reached the PDF).
    fields = assess_capture_completeness(pdf, html_text=html.read_text(encoding="utf-8"))
    assert fields.get("certified_complete") is not True
    assert fields.get("completeness_not_established") is True or fields.get("capture_incomplete") is True
    if fields.get("completeness_not_established"):
        assert "overflow" in fields["completeness_reason"].lower()


def test_capture_incomplete_survives_exhibit_record_and_index(tmp_path):
    """The words-doc completeness record is copied onto the exhibit entry and the index entry."""
    filings_root = tmp_path / "filings"
    ticker, acc = "PTGX", "0001104659-26-091099"
    dest = filings_root / ticker / acc
    dest.mkdir(parents=True)

    # Synthetic renderer: one overflowing page so completeness is incomplete.
    def _overflow_render(html_path: Path, pdf_path: Path, chrome=None) -> str:
        doc = pymupdf.open()
        page = doc.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(10, 10, 280, 80), color=(1, 0, 0), fill=(1, 0, 0))
        page.insert_text((210, 40), "CLIPPED", fontsize=12)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(pdf_path)
        doc.close()
        return "stub-renderer headless print-to-pdf"

    html_name = "sample_ex99-1.htm"
    html_bytes = b"<html><body>no overflow css here</body></html>"
    pages = ff.capture_exhibit(
        ticker=ticker,
        company="Protagonist Therapeutics, Inc.",
        cik="1377121",
        form="8-K",
        filing_date="2026-08-05",
        accession=acc,
        html_name=html_name,
        html_bytes=html_bytes,
        dest_dir=dest,
        base_url="https://example.test/",
        index_names=[html_name],
        fetcher=None,
        chrome=None,
        render=_overflow_render,
        filings_root=filings_root,
    )
    assert len(pages) == 1
    entry = pages[0]
    assert entry["capture_incomplete"] is True
    assert entry["certified_complete"] is False
    assert "capture_incomplete_reason" in entry
    words = json.loads((dest / Path(entry["words_file"]).name).read_text())
    assert completeness_fields(words) == completeness_fields(entry)

    # Submissions cache so index_ticker can resolve cik/company/date.
    (filings_root / ticker / "submissions.json").write_text(json.dumps({
        "cik": "1377121",
        "name": "Protagonist Therapeutics, Inc.",
        "filings": {"recent": {
            "accessionNumber": [acc], "filingDate": ["2026-08-05"], "form": ["8-K"],
        }},
    }))
    indexed = idx.index_ticker(ticker, filings_root=filings_root)
    assert len(indexed) == 1
    assert indexed[0]["capture_incomplete"] is True
    assert indexed[0]["certified_complete"] is False
    assert indexed[0]["capture_incomplete_reason"] == entry["capture_incomplete_reason"]


def _clean_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_text((20, 40), "INSIDE", fontsize=12)
    doc.save(path)
    doc.close()


@pytest.mark.parametrize("html", [
    "<div style='width:10px;overflow-x:hidden'>wide wide wide</div>",
    "<div style='height:10px;overflow-y:clip'>tall tall tall</div>",
    "<link rel='stylesheet' href='clip.css'><div class='c'>linked</div>",
    "<p>no clipping css at all</p>",
    None,
])
def test_no_overflow_found_is_never_certified_complete(tmp_path, html):
    pdf = tmp_path / "clean.pdf"
    _clean_pdf(pdf)
    fields = assess_capture_completeness(pdf, html_text=html)
    assert fields["completeness_not_established"] is True
    assert fields["certified_complete"] is False
    assert "capture_incomplete" not in fields
    assert fields["completeness_reason"]


def test_measured_overflow_is_incomplete_even_without_css(tmp_path):
    pdf = tmp_path / "over.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((210, 40), "OFF", fontsize=12)
    doc.save(pdf)
    doc.close()
    fields = assess_capture_completeness(pdf, html_text="<p>plain</p>")
    assert fields["capture_incomplete"] is True
    assert fields["certified_complete"] is False


def test_not_established_carries_words_to_exhibit_to_index(tmp_path):
    filings_root = tmp_path / "filings"
    ticker, acc = "PTGX", "0001104659-26-091099"
    dest = filings_root / ticker / acc
    dest.mkdir(parents=True)

    def _clean_render(html_path, pdf_path, chrome=None):
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        _clean_pdf(pdf_path)
        return "stub-renderer headless print-to-pdf"

    html_name = "sample_ex99-1.htm"
    pages = ff.capture_exhibit(
        ticker=ticker, company="Protagonist Therapeutics, Inc.", cik="1377121", form="8-K",
        filing_date="2026-08-05", accession=acc, html_name=html_name,
        html_bytes=b"<html><head><link rel='stylesheet' href='c.css'></head><body>x</body></html>",
        dest_dir=dest, base_url="https://example.test/", index_names=[html_name],
        fetcher=None, chrome=None, render=_clean_render, filings_root=filings_root,
    )
    entry = pages[0]
    assert entry["completeness_not_established"] is True
    assert entry["certified_complete"] is False
    words = json.loads((dest / Path(entry["words_file"]).name).read_text())
    assert completeness_fields(words) == completeness_fields(entry)

    (filings_root / ticker / "submissions.json").write_text(json.dumps({
        "cik": "1377121", "name": "Protagonist Therapeutics, Inc.",
        "filings": {"recent": {"accessionNumber": [acc], "filingDate": ["2026-08-05"], "form": ["8-K"]}},
    }))
    indexed = idx.index_ticker(ticker, filings_root=filings_root)
    assert completeness_fields(indexed[0]) == completeness_fields(entry)
    assert indexed[0]["certified_complete"] is False
