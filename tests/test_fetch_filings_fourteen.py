"""Offline tests for the 14-name capture: ticker lookup, --until, exhibit inclusion. No network, no Chrome."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from capture import fetch_filings as ff
from capture import index_filings as ix
from tests.test_fetch_filings import _stub_render

TICKERS_DOC = {
    "0": {"cik_str": 1125345, "ticker": "ACAD", "title": "ACADIA PHARMACEUTICALS INC"},
    "1": {"cik_str": 1639691, "ticker": "TRAX", "title": "Trax Example Inc."},
}


def _subs(rows):
    return {"cik": "0001125345", "name": "Acadia Pharmaceuticals Inc.", "filings": {"recent": {
        "form": [r[0] for r in rows], "filingDate": [r[1] for r in rows],
        "accessionNumber": [r[2] for r in rows], "primaryDocument": [r[3] for r in rows],
    }}}


ROWS = [
    ("10-K", "2026-02-20", "0000000001-26-000001", "acad-10k.htm"),
    ("8-K", "2026-08-05", "0000000001-26-000002", "acad-8k.htm"),
    ("10-Q", "2026-10-20", "0000000001-26-000003", "acad-10q.htm"),
    ("424B5", "2026-03-01", "0000000001-26-000004", "acad-424b5.htm"),
    ("4", "2026-03-02", "0000000001-26-000005", "form4.xml"),
    ("8-K", "2025-01-02", "0000000001-25-000009", "old.htm"),
]


def test_resolve_cik_builtin_first_then_sec_file(tmp_path):
    assert ff.resolve_cik("ptgx", tickers_doc=TICKERS_DOC) == ff.CIKS["PTGX"]
    assert ff.resolve_cik("acad", tickers_doc=TICKERS_DOC) == ("1125345", "ACADIA PHARMACEUTICALS INC")


def test_resolve_cik_uses_local_cache_without_fetching(tmp_path):
    cache = tmp_path / "company_tickers.json"
    cache.write_text(json.dumps(TICKERS_DOC))
    assert ff.resolve_cik("TRAX", fetcher=None, cache_path=cache)[0] == "1639691"


def test_resolve_cik_downloads_once_and_caches(tmp_path):
    class Fake:
        calls = 0
        def get_json(self, url):
            Fake.calls += 1
            assert url == ff.COMPANY_TICKERS_URL
            return TICKERS_DOC
    cache = tmp_path / "sub" / "company_tickers.json"
    assert ff.resolve_cik("ACAD", Fake(), cache_path=cache)[0] == "1125345"
    assert ff.resolve_cik("TRAX", Fake(), cache_path=cache)[0] == "1639691"
    assert Fake.calls == 1 and cache.is_file()


def test_resolve_cik_unknown_raises():
    with pytest.raises(ff.FetchError, match="Unknown ticker ZZZZ"):
        ff.resolve_cik("ZZZZ", tickers_doc=TICKERS_DOC)


def test_parse_tickers_accepts_names_outside_the_twelve():
    assert ff.parse_tickers("ACAD, agio") == ["ACAD", "AGIO"]
    with pytest.raises(SystemExit):
        ff.parse_tickers("bad ticker!")


def test_until_keeps_only_filings_on_or_before_the_date():
    subs = _subs(ROWS)
    got = ff.select_filings(subs, 365, today=date(2026, 10, 31), until=date(2026, 10, 13),
                            forms=("8-K", "10-K", "10-Q", "424B"))
    assert [f["accession"] for f in got] == [
        "0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000004"]
    now = ff.select_filings(subs, 365, today=date(2026, 10, 31), forms=("8-K", "10-K", "10-Q", "424B"))
    assert "0000000001-26-000003" in [f["accession"] for f in now]
    on_day = ff.select_filings(subs, 365, today=date(2026, 10, 31), until=date(2026, 10, 20), forms=("10-Q",))
    assert len(on_day) == 1  # filing_date == until is included


def test_form_family():
    assert [ff.form_family(f) for f in ("10-K/A", "424B5", "S-1/A", "8-K", "4")] == ["10-K", "424B", "S-1", "8-K", None]


def test_default_forms_stay_8k_only():
    got = ff.recent_8k_filings(_subs(ROWS), 365, today=date(2026, 10, 31))
    assert {f["form"] for f in got} == {"8-K"}


def _run(tmp_path, until=None, forms=("8-K", "10-K", "10-Q", "424B")):
    root = tmp_path / "filings"
    cik = "1125345"
    index_by, headers, html = {}, {}, {}
    for form, _d, acc, primary in ROWS:
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
        index_by[acc] = {"directory": {"item": []}}
        headers[acc] = ""
        html[base + primary] = f"<html><body>{form} main body {acc}</body></html>".encode()
        if form in ("8-K", "10-Q"):
            ex = "acad_ex99-1.htm"
            index_by[acc] = {"directory": {"item": [{"name": ex}]}}
            html[base + ex] = f"<html><body>Press release {acc}</body></html>".encode()
    pages = ff.fetch_ticker(
        "ACAD", days=365, fetcher=ff.RateLimitedFetcher("t t@example.com", interval=0), filings_root=root,
        today=date(2026, 10, 31), until=until, forms=forms, submissions=_subs(ROWS),
        index_by_accession=index_by, headers_by_accession=headers, html_by_url=html,
        render=_stub_render, tickers_doc=TICKERS_DOC)
    return root, pages


def test_exhibits_are_captured_with_the_filing_and_main_doc_only_for_non_8k(tmp_path):
    root, pages = _run(tmp_path)
    seen = {(p["form"], p["exhibit"]) for p in pages}
    assert ("8-K", "99.1") in seen and ("10-Q", "99.1") in seen
    assert ("10-K", "main") in seen and ("10-Q", "main") in seen and ("424B5", "main") in seen
    assert ("8-K", "main") not in seen  # 8-K: exhibits only, as for the twelve
    assert all(p["ticker"] == "ACAD" and p["cik"] == "1125345" for p in pages)
    assert not any(str(root) in json.dumps(p) for p in pages)  # no absolute paths in the entries
    ex = next(p for p in pages if p["form"] == "8-K")
    assert ex["id"].startswith("acad-8k-0000000001-26-000002-ex991-p")
    assert (root / "ACAD" / "0000000001-26-000002" / "acad_ex99-1_p1.words.json").is_file()


def test_until_applies_to_the_fetch(tmp_path):
    _root, pages = _run(tmp_path, until=date(2026, 10, 13))
    assert {p["accession"] for p in pages} == {
        "0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000004"}


def test_index_tool_reads_what_fetch_wrote(tmp_path):
    root, pages = _run(tmp_path)
    got = ix.index_ticker("ACAD", root)
    assert len(got) == len(pages)
    assert {(p["form"], p["exhibit"], p["filing_date"]) for p in got} >= {
        ("10-K", "main", "2026-02-20"), ("8-K", "99.1", "2026-08-05"), ("10-Q", "99.1", "2026-10-20")}
    assert all(p["cik"] == "1125345" for p in got)


def test_one_bad_document_is_recorded_not_fatal(tmp_path):
    def render(html, pdf, chrome=None):
        if "10k" in html.name:
            raise ff.FetchError("boom")
        return _stub_render(html, pdf, chrome)
    failures: list[str] = []
    root = tmp_path / "f"
    acc = "0000000001-26-000001"
    base = f"https://www.sec.gov/Archives/edgar/data/1125345/{acc.replace('-', '')}/"
    pages = ff.fetch_ticker(
        "ACAD", days=365, fetcher=ff.RateLimitedFetcher("t t@example.com", interval=0), filings_root=root,
        today=date(2026, 10, 31), forms=("10-K",), submissions=_subs(ROWS[:1]),
        index_by_accession={acc: {}}, headers_by_accession={acc: ""},
        html_by_url={base + "acad-10k.htm": b"<html>x</html>"}, render=render, tickers_doc=TICKERS_DOC,
        failures=failures)
    assert pages == [] and len(failures) == 1 and "boom" in failures[0]


def _pg(form, exhibit, filed, acc="a1"):
    return {"form": form, "exhibit": exhibit, "filing_date": filed, "accession": acc}


def test_workout_scope_is_8k_exhibits_in_window_no_bodies():
    until = date(2026, 10, 13)
    pages = [
        _pg("8-K", "99.1", "2026-10-13"),        # on the cutoff: in
        _pg("8-K/A", "99.2", "2026-04-16"),      # exactly 180 days before: in
        _pg("8-K", "99.1", "2026-04-15"),        # 181 days: out
        _pg("8-K", "99.1", "2026-10-14"),        # after the cutoff: out
        _pg("10-Q", "main", "2026-10-01"),       # body: out
        _pg("10-K", "99.1", "2026-09-01"),       # non-8-K exhibit: out
    ]
    got = ix.workout_pages(pages, until)
    assert [(p["form"], p["filing_date"]) for p in got] == [("8-K", "2026-10-13"), ("8-K/A", "2026-04-16")]


def test_scope_table_counts_filings_exhibits_pages():
    full = {"ACAD": [_pg("8-K", "99.1", "2026-09-01", "a1"), _pg("8-K", "99.1", "2026-09-01", "a1"),
                     _pg("10-K", "main", "2026-02-01", "a2")]}
    rows = ix.scope_table(full, {"ACAD": full["ACAD"][:2]})
    assert rows[1].split() == ["ACAD", "2", "2", "2", "3"]
    assert rows[-1].split() == ["TOTAL", "2", "2", "2", "3"]


def test_until_is_applied_to_the_merged_union(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"index_version": 1, "companies": {
        "OLD": {"pages": [_pg("8-K", "99.1", "2026-10-13", "o1"), _pg("8-K", "99.1", "2026-10-20", "o2"),
                          {"form": "8-K", "exhibit": "99.1", "accession": "o3"}]}}}))
    out = tmp_path / "out.json"
    root = tmp_path / "empty"
    root.mkdir()
    ix.main(["--tickers", "ACAD", "--out", str(out), "--merge", str(base),
             "--filings-root", str(root), "--until", "2026-10-13"])
    pages = json.loads(out.read_text())["companies"]["OLD"]["pages"]
    assert [p["accession"] for p in pages] == ["o1"]


def test_merge_without_until_is_unchanged(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"index_version": 1, "companies": {
        "OLD": {"pages": [_pg("8-K", "99.1", "2026-10-20", "o2")]}}}))
    out = tmp_path / "out.json"
    root = tmp_path / "empty"
    root.mkdir()
    ix.main(["--tickers", "ACAD", "--out", str(out), "--merge", str(base), "--filings-root", str(root)])
    assert len(json.loads(out.read_text())["companies"]["OLD"]["pages"]) == 1
