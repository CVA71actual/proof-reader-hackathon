"""Offline test for capture.index_filings: one fake filing in a temp dir."""

from __future__ import annotations

import json
import os

import pytest

import pymupdf

from capture import index_filings as ix
from capture.capture_page import extract_page, page_words, sha256_file
from runs import sources

ACC = "0001234567-26-000001"


def _make(root, ticker="FAKE", with_subs=False):
    d = root / ticker / ACC
    d.mkdir(parents=True)
    (d / "ex.htm").write_text(
        "<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT>\n<html><body><p>Fake Biotech, Inc.</p><p>Hello</p></body></html>")
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    doc.save(d / "ex.pdf")
    doc.close()
    for i in range(2):
        extract_page(d / "ex.pdf", i, d / f"ex_p{i + 1}.pdf")
        (d / f"ex_p{i + 1}.words.json").write_text(json.dumps(page_words(d / f"ex_p{i + 1}.pdf", i + 1)))
    if with_subs:
        (root / ticker / "submissions.json").write_text(json.dumps({
            "cik": "42", "name": "FAKE BIOTECH INC",
            "filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-09-01"], "accessionNumber": [ACC]}}}))
    return d


def test_index_hashes_files_and_falls_back(tmp_path):
    d = _make(tmp_path)
    pages = ix.index_ticker("FAKE", tmp_path)
    assert [p["page_number"] for p in pages] == [1, 2]
    p = pages[0]
    assert p["company"] == "Fake Biotech, Inc."          # from the exhibit header line
    assert p["filing_date_source"] == "mtime"
    assert p["exhibit"] == "99.1" and p["kind"] == "events"
    assert p["page_sha256"] == sha256_file(d / "ex_p1.pdf")
    assert p["words_sha256"] == sha256_file(d / "ex_p1.words.json")
    assert p["page_file"] == f"{ACC}/ex_p1.pdf"
    assert not str(p["local_path"]).startswith("/")


def test_submissions_json_gives_name_and_date(tmp_path):
    _make(tmp_path, with_subs=True)
    p = ix.index_ticker("FAKE", tmp_path)[0]
    assert p["company"] == "FAKE BIOTECH INC" and p["filing_date"] == "2026-09-01"
    assert "filing_date_source" not in p and p["cik"] == "42"


def test_unknown_ticker_empty_and_cli_merge(tmp_path, capsys):
    _make(tmp_path)
    base = tmp_path / "twelve.json"
    base.write_text(json.dumps({"index_version": 1, "companies": {"OLD": {"pages": [{"x": 1}]}}}))
    out = tmp_path / "out.json"
    rc = ix.main(["--tickers", "FAKE,NONE", "--out", str(out), "--merge", str(base), "--filings-root", str(tmp_path)])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert set(doc["companies"]) == {"OLD", "FAKE", "NONE"}
    assert len(doc["companies"]["FAKE"]["pages"]) == 2
    text = capsys.readouterr().out
    assert "FAKE: 2 page(s)" in text and "NONE: 0 page(s)" in text


def test_bad_ticker_refused(tmp_path):
    for bad in ("../x", "A/B", "TOOLONGTICKER1", "a!"):
        with pytest.raises(SystemExit):
            ix.parse_tickers(bad)
    assert ix.index_ticker("../x", tmp_path) == []
    assert ix.parse_tickers("brk.b, ABC-1") == ["BRK.B", "ABC-1"]


def test_symlinked_accession_dir_outside_skipped(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    _make(outside, "FAKE")
    (root / "FAKE").mkdir()
    os.symlink(outside / "FAKE" / ACC, root / "FAKE" / ACC)
    assert ix.index_ticker("FAKE", root) == []
    assert "outside the filings root" in capsys.readouterr().err


def test_symlinked_ticker_dir_outside_skipped(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    _make(outside, "FAKE")
    os.symlink(outside / "FAKE", root / "FAKE")
    assert ix.index_ticker("FAKE", root) == []


def test_symlinked_file_outside_skipped(tmp_path, capsys):
    root = tmp_path / "root"
    d = _make(root)
    secret = tmp_path / "secret.words.json"
    secret.write_text(json.dumps({"words": []}))
    (d / "ex_p1.words.json").unlink()
    os.symlink(secret, d / "ex_p1.words.json")
    pages = ix.index_ticker("FAKE", root)
    assert [p["page_number"] for p in pages] == [2]
    assert "outside the filings root" in capsys.readouterr().err


@pytest.mark.parametrize("body", [{"words": None}, {"words": "abc"}, {"words": {"a": 1}}, {"other": []}, [1, 2], None])
def test_malformed_words_skipped(tmp_path, capsys, body):
    d = _make(tmp_path)
    (d / "ex_p1.words.json").write_text(json.dumps(body))
    pages = ix.index_ticker("FAKE", tmp_path)
    assert [p["page_number"] for p in pages] == [2]
    assert "malformed" in capsys.readouterr().err
