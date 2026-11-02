"""TICKER-INTAKE-001: any-ticker New Run intake (server/intake.py). Offline: every SEC request is answered by a fake
fetcher from saved samples, pages are rendered by a stub, and the reader is a fake that records every call."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from capture import fetch_filings as ff
from runs import sources
from server import intake, jobs
from server.app import create_app
from tests.test_fetch_filings import SAMPLE_HEADERS, SAMPLE_HTML, SAMPLE_INDEX, SAMPLE_SUB, _stub_render
from tests.test_run_controls import FakeReader, PTGX_ONLY, runs_dir  # noqa: F401

TODAY = date(2026, 10, 28)
ACC = "0001104659-26-091099"
CRNX_CIK = "1658247"


def _sub(cik, name, forms, dates, prefix="0009999999-26-0000"):
    accs = [f"{prefix}{i:02d}" for i in range(len(forms))]
    return {"cik": cik, "name": name, "filings": {"recent": {
        "accessionNumber": accs, "filingDate": dates, "form": forms, "primaryDocument": ["x.htm"] * len(forms)}}}


def _archive(cik, acc):
    return ff.ARCHIVES.format(cik=int(cik), acc_nodash=acc.replace("-", ""))


class FakeFetcher:
    """Answers URLs from a dict; anything else is an HTTP 404 FetchError. Records every URL asked for."""

    def __init__(self, pages: dict[str, bytes]):
        self.pages, self.urls = pages, []

    def get(self, url):
        self.urls.append(url)
        if url not in self.pages:
            raise ff.FetchError(f"GET {url} failed: HTTP 404.")
        return self.pages[url]

    def get_json(self, url):
        return json.loads(self.get(url).decode())


def sec_samples() -> dict[str, bytes]:
    """PTGX (built-in map, one exhibit captured and one missing), CRNX (only in the fresh SEC list, one exhibit),
    FPI (6-K filer), QUIET (no 8-K in the window), NOEX (an 8-K without EX-99), DOWN (filing list unreachable),
    BROKE (every exhibit fails), TWIN (two companies)."""
    out: dict[str, bytes] = {}

    def put(url, doc):
        out[url] = doc if isinstance(doc, bytes) else json.dumps(doc).encode()

    tickers = {str(i): row for i, row in enumerate([
        {"cik_str": int(CRNX_CIK), "ticker": "CRNX", "title": "Crinetics Pharmaceuticals, Inc."},
        {"cik_str": 1000001, "ticker": "FPI", "title": "Foreign Issuer S.A."},
        {"cik_str": 1000002, "ticker": "QUIET", "title": "Quiet Co."},
        {"cik_str": 1000003, "ticker": "DOWN", "title": "Down Co."},
        {"cik_str": 1000004, "ticker": "BROKE", "title": "Broke Co."},
        {"cik_str": 1000005, "ticker": "TWIN", "title": "Twin A"},
        {"cik_str": 1000006, "ticker": "TWIN", "title": "Twin B"},
        {"cik_str": 1000007, "ticker": "NOEX", "title": "No Exhibit Co."},
    ])}
    put(ff.COMPANY_TICKERS_URL, tickers)
    # PTGX: the saved sample (8-K 2026-08-05): sample_ex99-1.htm is served, the deck exhibit is not (one failure)
    put(ff.SUBMISSIONS_URL.format(cik="1377121"), SAMPLE_SUB)
    base = _archive("1377121", ACC)
    put(base + "index.json", SAMPLE_INDEX)
    put(base + f"{ACC}-index-headers.html", SAMPLE_HEADERS.encode())
    put(base + "sample_ex99-1.htm", SAMPLE_HTML)
    # CRNX: one 8-K with one exhibit
    acc = "0001658247-26-000010"
    put(ff.SUBMISSIONS_URL.format(cik=CRNX_CIK),
        _sub(CRNX_CIK, "Crinetics Pharmaceuticals, Inc.", ["8-K"], ["2026-09-15"], prefix="0001658247-26-0000")
        | {"filings": {"recent": {"accessionNumber": [acc], "filingDate": ["2026-09-15"], "form": ["8-K"],
                                  "primaryDocument": ["crnx8k.htm"]}}})
    cbase = _archive(CRNX_CIK, acc)
    put(cbase + "index.json", {"directory": {"item": [{"name": "crnx_ex99-1.htm"}]}})
    put(cbase + f"{acc}-index-headers.html", b"")
    put(cbase + "crnx_ex99-1.htm", b"<html><body><p>Crinetics announces a Phase 3 readout in 2027.</p></body></html>")
    put(ff.SUBMISSIONS_URL.format(cik="1000001"), _sub("1000001", "Foreign Issuer S.A.", ["6-K", "6-K"],
                                                       ["2026-09-01", "2026-10-01"]))
    put(ff.SUBMISSIONS_URL.format(cik="1000002"), _sub("1000002", "Quiet Co.", ["8-K", "10-Q"],
                                                       ["2025-01-01", "2026-10-01"]))
    # NOEX: an 8-K in the window whose filing holds no EX-99
    nacc = "0001000007-26-000001"
    put(ff.SUBMISSIONS_URL.format(cik="1000007"), {"cik": "1000007", "name": "No Exhibit Co.", "filings": {"recent": {
        "accessionNumber": [nacc], "filingDate": ["2026-10-01"], "form": ["8-K"], "primaryDocument": ["n.htm"]}}})
    put(_archive("1000007", nacc) + "index.json", {"directory": {"item": [{"name": "n.htm"}]}})
    # BROKE: an 8-K whose only exhibit cannot be fetched
    bacc = "0001000004-26-000001"
    put(ff.SUBMISSIONS_URL.format(cik="1000004"), {"cik": "1000004", "name": "Broke Co.", "filings": {"recent": {
        "accessionNumber": [bacc], "filingDate": ["2026-10-01"], "form": ["8-K"], "primaryDocument": ["b.htm"]}}})
    put(_archive("1000004", bacc) + "index.json", {"directory": {"item": [{"name": "broke_ex99-1.htm"}]}})
    return out


@pytest.fixture
def prep(tmp_path):
    cache = tmp_path / "company_tickers.json"
    # a stale local cache that does not list CRNX (as on this machine): absence is not invalidity
    cache.write_text(json.dumps({"0": {"cik_str": 1000002, "ticker": "QUIET", "title": "Quiet Co."}}))
    fetcher = FakeFetcher(sec_samples())
    return intake.Preparer(fetcher=fetcher, render=_stub_render, filings_root=tmp_path / "intake-docs",
                           today=lambda: TODAY, tickers_cache=cache, background=False)


def client_for(runs_dir, prep, reader=None, **kw):
    reader = reader or FakeReader()
    app = create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False,
                     intake_preparer=prep, **kw)
    return TestClient(app), reader


def post_intake(client, text):
    r = client.post("/api/intake", json={"tickers": text})
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"intake_id"}
    g = client.get(f"/api/intake/{r.json()['intake_id']}")
    assert g.status_code == 200
    return g.json()


def by_symbol(rec):
    return {t["symbol"]: t for t in rec["tickers"]}


# ---------------------------------------------------------------- the pasted text


def test_normalise_mixed_case_duplicates_and_newlines():
    assert intake.normalise("ptgx, PTGX\nIons;  crnx\n\n\tPtgx") == ["PTGX", "IONS", "CRNX"]
    assert intake.normalise("brk/b ok") == ["BRK/B", "OK"]          # a bad token is kept, to be reported
    for bad in ("", "  \n ,", None, 5):
        with pytest.raises(intake.IntakeError):
            intake.normalise(bad)
    with pytest.raises(intake.IntakeError, match="at most"):
        intake.normalise(" ".join(f"T{i}" for i in range(intake.MAX_TICKERS + 1)))


def test_intake_body_and_ids_are_checked(runs_dir, prep):
    client, _ = client_for(runs_dir, prep)
    assert client.post("/api/intake", json={"tickers": "PTGX", "x": 1}).status_code == 422
    assert client.post("/api/intake", json={"tickers": ""}).status_code == 422
    assert client.get("/api/intake/in-000000000000").status_code == 404
    assert client.get("/api/intake/..%2Fsample").status_code == 404


# ---------------------------------------------------------------- per-ticker states


def test_mixed_batch_states_identity_and_counts(runs_dir, prep):
    client, reader = client_for(runs_dir, prep)
    rec = post_intake(client, "ptgx\ncrnx, PTGX zzzz brk/b FPI QUIET NOEX DOWN BROKE twin")
    assert rec["status"] == "done"
    assert rec["requested"] == ["PTGX", "CRNX", "ZZZZ", "BRK/B", "FPI", "QUIET", "NOEX", "DOWN", "BROKE", "TWIN"]
    t = by_symbol(rec)
    assert {s: x["state"] for s, x in t.items()} == {
        "PTGX": "ready", "CRNX": "ready", "ZZZZ": "unresolved", "BRK/B": "unsupported", "FPI": "unsupported",
        "QUIET": "no_documents", "NOEX": "no_documents", "DOWN": "failed", "BROKE": "failed", "TWIN": "unresolved"}
    assert rec["ready_count"] == 2 and rec["failed_count"] == 8
    # resolved identity: the built-in map for PTGX, the refreshed SEC list for CRNX (absent from the stale cache)
    assert (t["PTGX"]["cik"], t["PTGX"]["company"]) == ("1377121", "Protagonist Therapeutics, Inc.")
    assert (t["CRNX"]["cik"], t["CRNX"]["company"]) == (CRNX_CIK, "Crinetics Pharmaceuticals, Inc.")
    assert prep.fetcher().urls.count(ff.COMPANY_TICKERS_URL) == 1          # refreshed once, not per symbol
    assert "CRNX" in json.dumps(json.loads(prep.tickers_cache.read_text()))  # the cache now knows it
    # never a guessed company
    for s in ("ZZZZ", "TWIN"):
        assert t[s]["cik"] is None and t[s]["company"] is None
    assert "more than one company" in t["TWIN"]["reason"]
    assert "no company was guessed" in t["ZZZZ"]["reason"]
    assert "6-K" in t["FPI"]["reason"] and "8-K" in t["NOEX"]["reason"] and "No 8-K filed" in t["QUIET"]["reason"]
    assert "filing list" in t["DOWN"]["reason"] and "failed to capture" in t["BROKE"]["reason"]
    # documents are counted; one PTGX exhibit could not be fetched and is listed, not hidden
    assert t["PTGX"]["documents"] == 1 and t["PTGX"]["pages"] >= 1 and len(t["PTGX"]["failed_documents"]) == 1
    assert t["CRNX"]["documents"] == 1 and t["QUIET"]["documents"] == 0
    for x in t.values():
        assert x["form_scope"] == ["8-K"] and x["lookback_days"] == 90
        assert x["cutoff"] == "2026-10-28" and x["window_start"] == "2026-07-30"
        assert x["state"] in intake.STATES and x["reason"]
        assert "catalyst" not in x["reason"].lower()                       # never "no catalysts"
    assert reader.prompts == []                                            # no model call in an intake


def test_ready_index_holds_only_ready_pages_and_is_read_only(runs_dir, prep):
    client, _ = client_for(runs_dir, prep)
    rec = post_intake(client, "PTGX ZZZZ CRNX")
    path = runs_dir / "intake" / rec["intake_id"] / "sources_index.json"
    assert not os.stat(path).st_mode & stat.S_IWUSR
    index = sources.load_index(path)
    assert sorted(index.tickers) == ["CRNX", "PTGX"]
    for t in index.tickers:
        for p in index.pages(t):
            assert Path(p["record"]["page_path"]).is_file()
            assert str(prep.filings_root / rec["intake_id"]) in p["record"]["page_path"]
    # the ready symbols pass the existing run validation against this index
    choices = jobs.validate_choices({**PTGX_ONLY, "companies": ["PTGX", "CRNX"]}, index)
    assert choices["companies"] == ["PTGX", "CRNX"]


# ---------------------------------------------------------------- from intake to run


def test_run_from_intake_needs_explicit_subset_and_snapshots_its_manifest(runs_dir, prep):
    client, reader = client_for(runs_dir, prep)
    rec = post_intake(client, "PTGX CRNX ZZZZ")
    assert reader.prompts == []
    body = {**PTGX_ONLY, "companies": ["PTGX", "CRNX"], "intake_id": rec["intake_id"]}
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 422 and "ZZZZ" in r.json()["detail"] and "accept_ready_subset" in r.json()["detail"]
    r = client.post("/api/runs/new", json={**body, "companies": ["PTGX", "ZZZZ"], "accept_ready_subset": True})
    assert r.status_code == 422 and "ZZZZ" in r.json()["detail"]        # a failed symbol is never run
    assert reader.prompts == [] and not list(runs_dir.glob("run-*"))

    r = client.post("/api/runs/new", json={**body, "accept_ready_subset": True})
    assert r.status_code == 202, r.text
    assert reader.prompts                                                  # model calls only after the explicit start
    rid = r.json()["id"]
    run_dir = runs_dir / rid
    copy = run_dir / "sources_index.json"
    src = runs_dir / "intake" / rec["intake_id"] / "sources_index.json"
    assert copy.read_bytes() == src.read_bytes() and not os.stat(copy).st_mode & stat.S_IWUSR
    job = json.loads((run_dir / "job.json").read_text())
    freeze = job["source_freeze"]
    assert Path(freeze["index"]["path"]) == copy.resolve()               # the run reads its own copy
    assert {p["ticker"] for p in freeze["pages"]} == {"PTGX", "CRNX"}
    meta = json.loads((run_dir / "intake.json").read_text())
    assert meta["requested"] == ["PTGX", "CRNX", "ZZZZ"] and meta["left_out"] == ["ZZZZ"]
    assert meta["cutoff"] == "2026-10-28" and meta["form_scope"] == ["8-K"]

    # a later intake of the same name writes its own files; the run's manifest and pages do not change
    before = copy.read_bytes()
    sums = {p["source_id"]: p["page_sha256"] for p in freeze["pages"]}
    rec2 = post_intake(client, "ptgx")
    assert rec2["intake_id"] != rec["intake_id"]
    assert copy.read_bytes() == before
    assert jobs.source_set_changes(freeze, sources.load_index(copy), ["PTGX", "CRNX"]) == []
    assert sums == {p["source_id"]: p["page_sha256"] for p in freeze["pages"]}


def test_run_from_intake_never_reads_unrelated_cached_pages(runs_dir, prep):
    # the server's own index has PTGX/IONS pages (the built-in pin); an intake run must not read them
    client, _ = client_for(runs_dir, prep, index=sources.builtin_index())
    rec = post_intake(client, "CRNX")
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["IONS"], "intake_id": rec["intake_id"],
                                           "accept_ready_subset": True})
    assert r.status_code == 422 and "IONS" in r.json()["detail"]
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["all"], "intake_id": rec["intake_id"]})
    assert r.status_code == 202, r.text
    job = json.loads((runs_dir / r.json()["id"] / "job.json").read_text())
    assert {p["ticker"] for p in job["source_freeze"]["pages"]} == {"CRNX"}
    run = json.loads((runs_dir / r.json()["id"] / "run.json").read_text())
    assert run["run"]["choices"]["companies"] == ["CRNX"]


def test_run_refused_while_preparing_and_when_nothing_ready(runs_dir, prep):
    prep.background = True
    client, reader = client_for(runs_dir, prep)
    with intake.PREP_LOCK:                                  # another intake is capturing: this one waits its turn
        r = client.post("/api/intake", json={"tickers": "PTGX"})
        assert r.status_code == 202
        iid = r.json()["intake_id"]
        g = client.get(f"/api/intake/{iid}").json()
        assert g["status"] == "preparing" and g["tickers"][0]["state"] == "resolving"
        assert g["ready_count"] == 0 and g["failed_count"] == 0
        r = client.post("/api/runs/new", json={**PTGX_ONLY, "intake_id": iid})
        assert r.status_code == 409 and "still preparing" in r.json()["detail"]
    for _ in range(200):
        if client.get(f"/api/intake/{iid}").json()["status"] == "done":
            break
        time.sleep(0.02)
    assert client.get(f"/api/intake/{iid}").json()["ready_count"] == 1
    prep.background = False
    rec = post_intake(client, "ZZZZ QUIET")
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["all"], "intake_id": rec["intake_id"]})
    assert r.status_code == 409 and "No ticker in this intake is ready" in r.json()["detail"]
    assert "ZZZZ" in r.json()["detail"] and "QUIET" in r.json()["detail"]
    assert reader.prompts == []


def test_accept_ready_subset_needs_an_intake(runs_dir, prep):
    client, _ = client_for(runs_dir, prep)
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "accept_ready_subset": True})
    assert r.status_code == 422


def test_unexpected_error_in_one_ticker_does_not_stop_the_batch(runs_dir, prep, monkeypatch):
    real = ff.fetch_ticker

    def boom(ticker, **kw):
        if ticker == "CRNX":
            raise KeyError("x")
        return real(ticker, **kw)

    monkeypatch.setattr(ff, "fetch_ticker", boom)
    client, _ = client_for(runs_dir, prep)
    t = by_symbol(post_intake(client, "CRNX PTGX"))
    assert t["CRNX"]["state"] == "failed" and "KeyError" in t["CRNX"]["reason"]
    assert t["PTGX"]["state"] == "ready"


def test_one_intake_admits_one_run_even_for_concurrent_posts(runs_dir, prep, monkeypatch):
    # PR #162 Operator ruling: two identical POST /api/runs/new with the same intake_id (a lost answer, a double
    # click, a second tab) admit exactly one job; the replay gets 409 with the plain sentence and the first run's id,
    # and the reader is called for one run only
    client, reader = client_for(runs_dir, prep)
    rec = post_intake(client, "PTGX")
    real_create = jobs.create_run

    def slow_create(*a, **kw):                     # hold the admission window open so both requests are inside it
        time.sleep(0.3)
        return real_create(*a, **kw)

    monkeypatch.setattr(jobs, "create_run", slow_create)
    body = {**PTGX_ONLY, "companies": ["PTGX"], "intake_id": rec["intake_id"]}
    answers = []
    gate = threading.Barrier(2)

    def post():
        c = TestClient(client.app)
        gate.wait()
        r = c.post("/api/runs/new", json=body)
        answers.append((r.status_code, r.json()))

    threads = [threading.Thread(target=post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    codes = sorted(code for code, _ in answers)
    assert codes == [202, 409], answers
    run_id = next(b["id"] for code, b in answers if code == 202)
    refused = next(b for code, b in answers if code == 409)
    assert refused == {"detail": "A run was already started from these tickers.", "run_id": run_id}
    assert [p.name for p in runs_dir.glob("run-*")] == [run_id]
    assert client.get(f"/api/intake/{rec['intake_id']}").json()["run_id"] == run_id
    one_run_calls = len(reader.prompts)
    assert one_run_calls

    # a later replay is refused the same way, with no reader call
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 409 and r.json()["run_id"] == run_id
    assert len(reader.prompts) == one_run_calls

    # the concurrent pair made exactly one run's reader calls: a single run from a fresh intake makes as many
    monkeypatch.setattr(jobs, "create_run", real_create)
    rec2 = post_intake(client, "PTGX")
    r = client.post("/api/runs/new", json={**body, "intake_id": rec2["intake_id"]})
    assert r.status_code == 202, r.text
    assert len(reader.prompts) == 2 * one_run_calls


# ---------------------------------------------------------------- INTAKE-PAGE-CAP-001: the first pages of each document

MULTI_CIK = "1000008"


def _paged_render(html_path: Path, pdf_path: Path, chrome: Path | None = None) -> str:
    """A stub render with as many pages as the HTML's PAGES=<n> marker says (one page without a marker)."""
    import re

    import pymupdf
    text = html_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"PAGES=(\d+)", text)
    doc = pymupdf.open()
    for k in range(int(m.group(1)) if m else 1):
        doc.new_page(width=612, height=792).insert_text((72, 72), f"page {k + 1} of {html_path.name}", fontsize=11)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(pdf_path)
    doc.close()
    return "stub-renderer headless print-to-pdf"


@pytest.fixture
def multi_prep(tmp_path):
    """MULTI: one 8-K with two exhibits, a 5-page deck and a 1-page release; CRNX: one 3-page exhibit."""
    samples = sec_samples()
    tickers = json.loads(samples[ff.COMPANY_TICKERS_URL])
    tickers["99"] = {"cik_str": int(MULTI_CIK), "ticker": "MULTI", "title": "Multi Co."}
    samples[ff.COMPANY_TICKERS_URL] = json.dumps(tickers).encode()
    acc = "0001000008-26-000001"
    samples[ff.SUBMISSIONS_URL.format(cik=MULTI_CIK)] = json.dumps({"cik": MULTI_CIK, "name": "Multi Co.", "filings": {
        "recent": {"accessionNumber": [acc], "filingDate": ["2026-10-01"], "form": ["8-K"],
                   "primaryDocument": ["m.htm"]}}}).encode()
    base = _archive(MULTI_CIK, acc)
    samples[base + "index.json"] = json.dumps(
        {"directory": {"item": [{"name": "multi_ex99-1.htm"}, {"name": "multi_ex99-2.htm"}]}}).encode()
    samples[base + f"{acc}-index-headers.html"] = b""
    samples[base + "multi_ex99-1.htm"] = b"<html><body><p>Multi deck PAGES=5</p></body></html>"
    samples[base + "multi_ex99-2.htm"] = b"<html><body><p>Multi release, one page.</p></body></html>"
    cbase = _archive(CRNX_CIK, "0001658247-26-000010")
    samples[cbase + "crnx_ex99-1.htm"] = b"<html><body><p>Crinetics readout PAGES=3</p></body></html>"
    cache = tmp_path / "company_tickers.json"
    cache.write_text(json.dumps({}))
    return intake.Preparer(fetcher=FakeFetcher(samples), render=_paged_render, filings_root=tmp_path / "intake-docs",
                           today=lambda: TODAY, tickers_cache=cache, background=False)


def test_cap_pages_is_per_document_and_keeps_short_documents_whole():
    def pg(acc, ex, n):
        return {"accession": acc, "exhibit": ex, "page_number": n}
    pages = [pg("A", "99.1", n) for n in (3, 1, 2, 4)] + [pg("A", "99.2", 1)] + [pg("B", "99.1", n) for n in (1, 2)]
    kept = intake.cap_pages(pages)
    assert [(p["accession"], p["exhibit"], p["page_number"]) for p in kept] == [
        ("A", "99.1", 1), ("A", "99.1", 2), ("A", "99.2", 1), ("B", "99.1", 1), ("B", "99.1", 2)]
    assert intake.PAGES_PER_DOCUMENT == 2
    assert intake.cap_pages([pg("C", "99.1", 1)]) == [pg("C", "99.1", 1)]


def test_intake_index_holds_the_first_pages_of_each_document_with_honest_counts(runs_dir, multi_prep):
    client, reader = client_for(runs_dir, multi_prep)
    rec = post_intake(client, "MULTI CRNX")
    t = by_symbol(rec)
    assert t["MULTI"]["state"] == "ready" and t["CRNX"]["state"] == "ready"
    # per document, not per ticker: MULTI keeps 2 of the deck's 5 and the whole 1-page release (3, not 2)
    assert (t["MULTI"]["documents"], t["MULTI"]["pages_captured"], t["MULTI"]["pages_in_run"]) == (2, 6, 3)
    assert (t["CRNX"]["documents"], t["CRNX"]["pages_captured"], t["CRNX"]["pages_in_run"]) == (1, 3, 2)
    assert t["MULTI"]["pages"] == 6 and t["MULTI"]["pages_per_document"] == 2
    assert t["MULTI"]["reason"] == ("2 documents, 6 pages captured; the run reads the first 2 pages of each "
                                    "(3 pages).")
    assert t["CRNX"]["reason"] == "1 document, 3 pages captured; the run reads the first 2 pages of each (2 pages)."
    assert (rec["pages_per_document"], rec["pages_captured"], rec["pages_in_run"]) == (2, 9, 5)
    for x in t.values():
        assert "every page" not in x["reason"] and "all pages" not in x["reason"]
    index = sources.load_index(runs_dir / "intake" / rec["intake_id"] / "sources_index.json")
    got = sorted((p["record"]["exhibit"], p["record"]["page_number"]) for p in index.pages("MULTI"))
    assert got == [("99.1", 1), ("99.1", 2), ("99.2", 1)]
    assert sorted(p["record"]["page_number"] for p in index.pages("CRNX")) == [1, 2]
    # pages over the cap are still captured on disk, only left out of the index
    on_disk = list((multi_prep.filings_root / rec["intake_id"]).rglob("*_p*.pdf"))
    assert len(on_disk) == 9 and any(f.name.endswith("_p5.pdf") for f in on_disk)
    assert reader.prompts == []


def test_run_snapshot_holds_only_the_capped_pages(runs_dir, multi_prep):
    client, _ = client_for(runs_dir, multi_prep)
    rec = post_intake(client, "MULTI CRNX")
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["MULTI", "CRNX"], "intake_id": rec["intake_id"]})
    assert r.status_code == 202, r.text
    run_dir = runs_dir / r.json()["id"]
    freeze = json.loads((run_dir / "job.json").read_text())["source_freeze"]
    assert len(freeze["pages"]) == 5
    snap = sources.load_index(run_dir / "sources_index.json")
    assert sum(len(snap.pages(t)) for t in snap.tickers) == 5
    for t in snap.tickers:
        assert all(p["record"]["page_number"] <= 2 for p in snap.pages(t))
    meta = json.loads((run_dir / "intake.json").read_text())
    assert meta["pages_per_document"] == 2
    assert {x["symbol"]: x["pages_in_run"] for x in meta["tickers"]} == {"MULTI": 3, "CRNX": 2}
