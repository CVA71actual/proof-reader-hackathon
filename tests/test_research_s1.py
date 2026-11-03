"""RESEARCHER-FULL-001 S1: the research loop on the Responses API, with fake Responses and fake fetches only.

Covers: the Responses call path (replay before tool results, previous_response_id, tool_choice auto, search pricing,
no retry), the capture tools (bytes, SHA-256, retrieval time, pages and words, capture_incomplete and hidden-text
checks, the append-only manifest with checkpoints), the checks inside propose_events (typed feedback once, revisions,
multi-span, word ids as strings, null vs [] conditions, the dateline rule, exact repeats and linked near-duplicates),
rows in the existing state/decision log/calendar/exports with the label, the draft audit, the Agent trail, and the
kill conditions (unpriced call, empty-answer rate, capture failure, limit hit)."""

import io
import json
import zipfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import research_sources as rs
from capture import responses_reader as rr
from runs import prices, research, research_audit, research_contract as rc
from server import jobs
from server.app import create_app
from tests import research_fakes as F
from tests.conftest import FIXTURES, REPO

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PR1 = "https://ir.example.com/news/pr1"
PR2 = "https://www.partner.example.org/release"
QUOTE = "Topline data from the Phase 3 ICARUS trial are expected in Q2 2027."
PAGE1 = F.html("SAN DIEGO, Nov. 2, 2026 -- Protagonist Therapeutics announced today its pipeline update.",
               QUOTE, "The company also plans an NDA submission for rusfertide by year-end 2027, subject to FDA "
                      "feedback.")


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, reader, pages=None, *, companies=("PTGX",), limit=None, limits=None, resolve=None, **choices):
    tools = F.tools(pages if pages is not None else {PR1: (200, "text/html; charset=utf-8", PAGE1)})
    if resolve:
        tools.resolve = resolve
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    body = {"mode": "research", "companies": list(companies), "as_of": "2026-11-02", **choices}
    if limit is not None:
        body["spending_limit_usd"] = limit
    if limits is not None:
        body["research_limits"] = limits
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    return client, r.json()["id"], tools


def run_json(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def happy_script(extra_events=None):
    ev = F.event("ICARUS Phase 3 topline data", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                 program="ICARUS", conditions=[])
    return [
        F.body([F.reasoning(1), F.search("Protagonist catalysts 2027", [PR1]),
                F.call("fetch_url", {"url": PR1, "purpose": "company release"}, "c1")], rid="r1"),
        F.body([F.call("propose_events", {"events": [ev] + (extra_events or [])}, "c2")], rid="r2"),
        F.body([F.call("record_coverage", {"ticker": "PTGX", "category": "regulators",
                                           "question": "Any PDUFA date for rusfertide?", "status": "checked_none",
                                           "tool_call_ids": ["c1"], "source_ids": ["src-r1-ptgx"],
                                           "programs": ["rusfertide"], "finding": "No PDUFA date stated."}, "c3")],
               rid="r3"),
        lambda req: F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": r, "disposition": "proposal", "related_row_id": None, "reason": "supported"}
            for r in _row_ids(req)], "limitations": [
            {"question": "Partner releases", "tool_call_ids": [], "source_ids": [], "reason": "not opened"}]}, "c4")],
            rid="r4"),
    ]


def _row_ids(req):
    """The row ids the propose_events answers in this request's history assigned."""
    ids = []
    for item in req["input"]:
        if item.get("type") == "function_call_output":
            data = json.loads(item["output"]).get("data") or {}
            ids += [e["id"] for e in data.get("events", []) if e.get("id")]
    return sorted(set(ids))


# ---------------------------------------------------------------- the end-to-end fake run


def test_fake_run_from_captured_web_source_to_proposal_checks_state_and_export(runs_dir, tmp_path):
    reader = F.FakeResponses(happy_script())
    client, rid, tools = start(runs_dir, reader)
    run = run_json(runs_dir, rid)
    meta = run["run"]
    assert meta["status"] == "done", meta["status_reason"]
    assert meta["choices"]["mode"] == "research" and meta["choices"]["spending_limit_usd"] == 30.0
    assert meta["prices"]["per_1k_searches_usd"] == {"Muse Spark": 2.50}
    assert all(v is None for k, v in meta["choices"]["research_limits"].items()
               if k not in ("concurrency", "stop_on_empty_rate"))
    RUN_SCHEMA.validate(run)
    # the captured source: bytes, hash, retrieval time, pages and words, capture checks on every page
    src = run["sources"][0]
    ret = src["retrieval"]
    assert src["id"] == "src-r1-ptgx" and src["url"] == PR1 and ret["tool"] == "fetch_url"
    import hashlib
    assert hashlib.sha256((runs_dir / rid / ret["original_file"]).read_bytes()).hexdigest() == ret["original_sha256"]
    assert ret["retrieved_at"] == "2026-11-02T18:00:00+00:00" and src["rendered_from"]["format"] == "html"
    for p in src["pages"]:
        assert set(p["research_checks"]) >= {"capture_incomplete", "hidden_text"}
        assert (runs_dir / rid / p["words"]).is_file() and (runs_dir / rid / p["image"]).is_file()
    # the proposal and its checks
    rows = [r for r in run["rows"] if r.get("research")]
    assert len(rows) == 1
    row = rows[0]
    assert row["research"]["label"] == "Agent research — not independently reviewed"
    assert row["quote"] == QUOTE and row["status"] == "matched", row["checks"]
    assert {c["name"]: c["result"] for c in row["checks"]}["quote_match"] == "pass"
    assert row["conditions"] == [] and row["research"]["conditions_state"] == "none_stated"
    assert row["trail"][0]["step"].startswith("Sent the page")
    # the existing state, decision log and calendar: Keep puts it on the calendar, Remove/Undo work
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "keep", "note": None,
                                                           "fix": None}).status_code == 200
    cal = client.get(f"/api/runs/{rid}/calendar").json()
    assert row["id"] in json.dumps(cal)
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "remove",
                                                           "note": "test", "fix": None}).status_code == 200
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": row["id"], "action": "undo", "note": None,
                                                           "fix": None}).status_code == 200
    state = client.get(f"/api/runs/{rid}").json()["state"]["targets"][row["id"]]
    assert state["accepted"] is True
    # exports: PDF and DOCX carry the label and the row
    from report.build import build as build_report
    out = build_report(rid, out=tmp_path / "rep", run_dir=runs_dir / rid)
    pdf_text = "".join(p.get_text() for p in pymupdf.open(out["pdf"]))
    assert "Agent research — not independently reviewed" in " ".join(pdf_text.split())
    assert "ICARUS" in pdf_text
    docx_xml = zipfile.ZipFile(out["docx"]).read("word/document.xml").decode()
    assert "Agent research — not independently reviewed" in docx_xml and "searched, not found" in docx_xml
    # the audit: sourced, captured, window on the period end, scope per row, exports agree
    audit = meta["research"]["audit"]
    assert audit["every_row_sourced"] and audit["every_source_captured"] and audit["exports"]["agree"]
    assert audit["rows"][0]["window"]["result"] == "in_window"
    assert audit["searched_not_found"][0]["retrieval_times"]
    assert audit["published"] is False
    text = research_audit.format_audit(research_audit.draft_audit(runs_dir / rid, run_json(runs_dir, rid),
                                                                  _decisions(runs_dir, rid)))
    assert "check quote_match: pass" in text and "not checked:" in text and "searched, not found" in text


def _decisions(runs_dir, rid):
    return [json.loads(l) for l in (runs_dir / rid / "decisions.jsonl").read_text().splitlines() if l.strip()]


def test_agent_trail_steps_search_fetch_proposal_check(runs_dir):
    reader = F.FakeResponses(happy_script())
    _, rid, _ = start(runs_dir, reader)
    steps = run_json(runs_dir, rid)["run"]["research"]["steps"]
    kinds = [s["kind"] for s in steps]
    for k in ("request", "search", "fetch", "proposal", "check", "coverage", "finish"):
        assert k in kinds, k
    search = next(s for s in steps if s["kind"] == "search")
    assert "leads, not evidence" in search["text"] and search["refs"]["results"] == [PR1]
    assert next(s for s in steps if s["kind"] == "proposal")["tag"] == "Model"
    assert next(s for s in steps if s["kind"] == "check")["tag"] == "Code"


def test_requests_replay_full_output_before_tool_results_with_auto_tool_choice(runs_dir):
    reader = F.FakeResponses(happy_script())
    start(runs_dir, reader)
    first, second = reader.requests[0], reader.requests[1]
    assert first["tool_choice"] == "auto" and first["parallel_tool_calls"] is False
    assert first["store"] is False and "previous_response_id" not in first
    assert first["tools"][0] == {"type": "web_search", "search_context_size": "medium"}
    assert "max_tool_calls" not in first          # no search limit is on unless a person sets one
    assert {t["name"] for t in first["tools"][1:]} == set(rc.ARGUMENTS) - {"brave_search"}   # no key in tests
    assert all("function" not in t for t in first["tools"][1:])       # flat Responses form
    # the instructions are the compact controller; the assignment is its own input item, exactly
    assert first["instructions"] == rc.CONTROLLER and len(rc.CONTROLLER) < 3000       # compact, not the 3,996 brief
    assert first["input"][0]["content"].endswith(run_choices_note())
    kinds = [i.get("type") for i in second["input"][2:]]
    assert kinds == ["reasoning", "web_search_call", "function_call", "function_call_output"]
    assert second["input"][4]["call_id"] == second["input"][5]["call_id"] == "c1"


def run_choices_note():
    from capture import research_instructions
    return research_instructions.DEFAULT


def test_previous_response_id_sends_only_new_items(runs_dir):
    # continuation is a code setting; exercise it directly on a job
    reader = F.FakeResponses(happy_script() + [F.body([F.call("finish_draft", {"dispositions": [
        {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"}], "limitations": []},
        "c5")])])           # with previous_response_id the first finish lacks e1 and is sent back once
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}),
                          as_of="2026-11-02")
    choices_path = runs_dir / run["run"]["id"] / "run.json"
    doc = json.loads(choices_path.read_text())
    doc["run"]["choices"]["research_continuation"] = rr.CONTINUE_PREVIOUS
    choices_path.write_text(json.dumps(doc))
    research.run_research(runs_dir, run["run"]["id"], reader, F.tools({PR1: (200, "text/html", PAGE1)}))
    second = reader.requests[1]
    assert second["previous_response_id"] == "r1" and second["store"] is True
    assert [i["type"] for i in second["input"]] == ["function_call_output"]


def test_search_price_and_responses_cost():
    assert prices.PRICES_PER_1K_SEARCHES_USD["Muse Spark"] == 2.50
    assert prices.search_cost_usd("Muse Spark", 4) == 0.01
    assert prices.responses_cost_usd("Muse Spark", 1_000_000, 0, 1000) == round(1.25 + 2.50, 6)
    assert prices.search_cost_usd("Muse Spark", None) is None        # unknown search count is never priced as zero
    assert prices.responses_cost_usd("Muse Wonder", 10, 10, 1) is None  # no search price -> unpriced
    assert "per_1k_searches_usd" not in prices.snapshot()     # page-mode snapshots unchanged
    assert prices.search_snapshot() == {"Muse Spark": 2.50}


def test_token_counts_must_be_whole_non_negative_numbers():
    for bad in (-1, True, False, 1.5, None, "10"):
        assert prices.cost_usd("Muse Spark", bad, 10) is None
        assert prices.cost_usd("Muse Spark", 10, bad) is None
        assert prices.search_cost_usd("Muse Spark", bad) is None
        assert prices.image_cost_usd("SAM 3.1", bad) is None
    assert prices.model_cost_fields("Muse Spark", -5, 10) == {"tokens": None, "cost_usd": None}


def test_run_spend_counts_tokens_and_searches(runs_dir):
    reader = F.FakeResponses(happy_script())
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    per_call = prices.cost_usd("Muse Spark", 1000, 200)
    assert meta["spend_usd"] == pytest.approx(4 * per_call + 0.0025, abs=1e-6)
    c = meta["research"]["companies"]["PTGX"]
    assert c["searches"] == 1 and c["requests"] == 4 and c["status"] == "finished"


# ---------------------------------------------------------------- the Responses reader boundary (Manus 14:45)


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def _live(monkeypatch, status, data):
    import httpx
    sent = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            sent.append(url)
            return _Resp(status, data)
    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-marker")
    return rr.LiveResponsesReader(), sent


BODY = {"model": rr.MODEL, "input": [], "tools": [], "max_output_tokens": 10}


def test_http_200_with_a_json_list_is_a_typed_possibly_billed_error(monkeypatch):
    reader, sent = _live(monkeypatch, 200, [])
    reply = reader.respond(BODY)
    assert reply.error and reply.may_have_been_billed and reply.searches is None and len(sent) == 1


@pytest.mark.parametrize("output", ["missing", "not-output-items", [1, 2]])
def test_missing_or_wrong_shaped_output_keeps_search_usage_unknown(monkeypatch, output):
    data = {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10}}
    if output != "missing":
        data["output"] = output
    reader, _ = _live(monkeypatch, 200, data)
    reply = reader.respond(BODY)
    assert reply.error and reply.may_have_been_billed
    assert reply.searches is None                       # never 0
    assert reply.input_tokens == 10 and reply.output_tokens == 10
    assert prices.responses_cost_usd("Muse Spark", reply.input_tokens, reply.output_tokens, reply.searches) is None


def test_valid_function_call_without_prose_is_not_empty_and_an_empty_list_is_allowed(monkeypatch):
    reader, _ = _live(monkeypatch, 200, F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")]))
    reply = reader.respond(BODY)
    assert not reply.error and not reply.empty and reply.searches == 0
    reader, _ = _live(monkeypatch, 200, F.body([]))
    reply = reader.respond(BODY)
    assert not reply.error and reply.empty and reply.searches == 0


def test_400_and_422_are_never_retried(monkeypatch):
    for status in (400, 422):
        reader, sent = _live(monkeypatch, status, {"error": "bad"})
        reply = reader.respond(BODY)
        assert len(sent) == 1 and reply.http_status == status and reply.may_have_been_billed
        assert "bad" not in reply.error           # never the provider's body


def test_a_400_stops_only_that_company_and_the_others_go_on(runs_dir):
    bad = rr.ResponsesReply(model=rr.MODEL, http_status=400, error="The model call did not succeed (HTTP 400).",
                            may_have_been_billed=True)
    reader = F.PerCompany({"PTGX": [bad], "IONS": [
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "i1")])]})

    def resolve(t):
        return {"PTGX": ("1377121", "Protagonist"), "IONS": ("874015", "Ionis")}[t]
    _, rid, _ = start(runs_dir, reader, companies=("PTGX", "IONS"), resolve=resolve)
    meta = run_json(runs_dir, rid)["run"]
    cs = meta["research"]["companies"]
    assert cs["PTGX"]["status"] == "failed" and "not retried" in cs["PTGX"]["status_reason"]
    assert cs["IONS"]["status"] == "finished"
    assert len(reader.readers["PTGX"].requests) == 1
    assert meta["status"] == "needs_input" and "Partial research, not published" in meta["status_reason"]
    assert meta["spend_usd"] > 0        # the failed, possibly billed request is charged its reserved worst case


# ---------------------------------------------------------------- capture tools


def test_word_ids_are_strings_and_a_real_captured_page_validates():
    words = json.loads((FIXTURES / "ptgx_8k_2026-08-05_ex991_p3.words.json").read_text())["words"]
    ids = [w["id"] for w in words[:30]]
    span = {"source_id": "s", "page_number": 1, "quote": "x", "word_ids": ids, "roles": ["timing"]}
    Draft202012Validator(rc.SPAN).validate(span)
    with pytest.raises(Exception):
        Draft202012Validator(rc.SPAN).validate({**span, "word_ids": [1, 2]})


def test_url_safety_refuses_local_private_and_credentialed_urls():
    for url in ("http://localhost:8777/api", "http://127.0.0.1:8777/x", "ftp://example.com/a",
                "https://user:pw@example.com/", "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd"):
        with pytest.raises(rs.FetchRefused):
            rs.check_url(url, F.public)
    with pytest.raises(rs.FetchRefused):
        rs.check_url("https://loopback.test/", F.public)      # a name that resolves to loopback
    assert rs.check_url("https://example.com/x", F.public)


def test_web_html_is_rendered_from_an_isolated_snapshot(tmp_path, monkeypatch):
    """Manus 14:45 HIGH: a page with a script fetching 127.0.0.1, a loopback image and a relative image. Nothing the
    page names reaches the renderer, Chrome runs with scripts off and no network, and the limitation is recorded."""
    from capture import fetch_filings as ff
    page = (b"<html><head><title>T</title><script>fetch('http://127.0.0.1:8777/api/runs')</script>"
            b"<link rel=stylesheet href='site.css'></head><body onload='go()'>"
            b"<p>Topline data in Q2 2027.</p><img src='http://127.0.0.1:8777/x.png' alt='chart'>"
            b"<img src='figures/fig1.png'><iframe src='http://127.0.0.1:8777/'></iframe></body></html>")
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setenv("CHROME_BIN", str(fake_chrome))
    monkeypatch.setattr(ff, "renderer_label", lambda chrome: "Fake Chrome headless print-to-pdf")
    seen = {}

    def intercept(cmd, **kw):          # never executes a browser
        seen["cmd"] = cmd
        printable = Path(cmd[-1].replace("file://", ""))
        seen["html"] = printable.read_text()
        seen["folder"] = sorted(p.name for p in printable.parent.iterdir())
        out = next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1]
        Path(out).write_bytes(rs.render_text_pdf("Topline data in Q2 2027.", "T"))
    fetcher = F.FakeFetcher({"https://example.org/public": (200, "text/html", page)})
    fetched = fetcher.get("https://example.org/public")
    got = rs.capture_document(fetched, tmp_path / "run", "src-r1-x",
                              render_html=lambda h, p: rs.chrome_render(h, p, run=intercept))
    assert fetcher.asked == ["https://example.org/public"]        # no subresource was fetched
    html = seen["html"]
    assert "<script" not in html and "127.0.0.1" not in html and "fig1.png" not in html and "site.css" not in html
    assert "onload" not in html and "<iframe" not in html
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert set(rs.ISOLATION_FLAGS) <= set(seen["cmd"])
    assert "--disable-javascript" in seen["cmd"]
    assert seen["folder"] == [".print_page.html", "page.html"]   # an empty folder: relative refs resolve to nothing
    lim = got["rendered"]["limitations"]
    assert any("image(s) not captured" in l for l in lim) and any("script(s) removed" in l for l in lim)
    assert got["rendered"]["removed"]["images_not_captured"] == 2


def test_sec_exhibit_renderer_is_unchanged():
    import inspect
    from capture import fetch_filings as ff
    src = inspect.getsource(ff.render_html_to_pdf)
    assert "ISOLATION" not in src and "sanitize" not in src


def test_capture_failure_is_recorded_and_the_loop_goes_on(runs_dir):
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": "https://gone.example.com/x", "purpose": "lead"}, "c1")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": [
            {"question": "gone.example.com release", "tool_call_ids": ["c1"], "source_ids": [],
             "reason": "404"}]}, "c2")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages={})
    run = run_json(runs_dir, rid)
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["capture_failures"][0]["http_status"] == 404
    assert "HTTP 404" in c["capture_failures"][0]["reason"]
    out = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out["status"] == "error" and out["issues"][0]["code"] == "could_not_access"
    assert run["sources"] == []
    lines = (runs_dir / rid / "research" / "manifest.jsonl").read_text().splitlines()
    assert any(json.loads(l)["kind"] == "capture_failed" for l in lines)
    assert run["run"]["research"]["audit"]["capture_failures"]["PTGX"]


def test_hidden_and_incomplete_pages_are_flagged_and_a_hidden_quote_is_an_issue(runs_dir):
    url = "https://ir.example.com/deck.pdf"
    pdf = F.pdf_with(["Pipeline overview for 2027."], tiny=["Approval expected March 3, 2027."],
                     off_page=["clipped words"])
    ev = F.event("Approval", "Approval expected March 3, 2027.", date_text="March 3, 2027", date="2027-03-03")
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": url, "purpose": "deck"}, "c1")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
        F.body([F.message("Done.")]),
        F.body([F.message("Still done.")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages={url: (200, "application/pdf", pdf)})
    run = run_json(runs_dir, rid)
    page = run["sources"][0]["pages"][0]
    assert page["research_checks"]["hidden_text"]["result"] == "fail"
    assert page["research_checks"]["capture_incomplete"]["result"] == "fail"
    row = next(r for r in run["rows"] if r.get("research"))
    codes = {i["code"] for i in row["research"]["issues"]}
    assert {"quote_on_hidden_text", "capture_incomplete"} <= codes
    # a final prose answer after one reminder ends the company: the model said it is done
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["finish"]["by"] == "prose"


def test_ctgov_record_is_captured_and_a_query_returns_leads_only(runs_dir):
    study = json.dumps({"protocolSection": {"identificationModule": {"nctId": "NCT01234567",
                                                                     "briefTitle": "ICARUS"},
                                            "statusModule": {"overallStatus": "RECRUITING",
                                                             "primaryCompletionDateStruct": {"date": "2027-03"}}}})
    search = json.dumps({"studies": [json.loads(study)]})
    pages = {rs.CTGOV_STUDY.format(nct="NCT01234567"): (200, "application/json", study.encode()),
             rs.ctgov_search_url("rusfertide"): (200, "application/json", search.encode())}
    reader = F.FakeResponses([
        F.body([F.call("fetch_ctgov", {"nct_id": None, "query": "rusfertide", "cursor": None}, "c1")]),
        F.body([F.call("fetch_ctgov", {"nct_id": "NCT01234567", "query": None, "cursor": None}, "c2")]),
        F.body([F.call("fetch_ctgov", {"nct_id": "NCT01234567", "query": "x", "cursor": None}, "c3")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c4")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages=pages)
    run = run_json(runs_dir, rid)
    out1 = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out1["data"]["studies"][0]["nct_id"] == "NCT01234567" and "Leads only" in out1["data"]["note"]
    assert len(run["sources"]) == 1 and run["sources"][0]["retrieval"]["tool"] == "fetch_ctgov"
    assert run["sources"][0]["retrieval"]["rendered"]["format"] == "json"
    out2 = json.loads(reader.requests[2]["input"][-1]["output"])
    assert any("protocolSection.statusModule.overallStatus: RECRUITING" in l for l in out2["data"]["text"][0]["lines"])
    out3 = json.loads(reader.requests[3]["input"][-1]["output"])
    assert out3["status"] == "error"


def test_fetch_edgar_lists_leads_for_the_company_only_and_clips_to_as_of(runs_dir):
    subs = {"filings": {"recent": {"form": ["8-K", "10-Q", "8-K"], "filingDate": ["2026-10-01", "2026-08-01",
                                                                                   "2026-12-01"],
                                   "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003"],
                                   "primaryDocument": ["a.htm", "b.htm", "c.htm"]}}}
    pages = {rs.SUBMISSIONS_URL.format(cik="1377121"): (200, "application/json", json.dumps(subs).encode())}
    reader = F.FakeResponses([
        F.body([F.call("fetch_edgar", {"cik": "1377121", "forms": ["8-K"], "since": "2026-01-01",
                                       "until": "2027-01-01", "cursor": None}, "c1")]),
        F.body([F.call("fetch_edgar", {"cik": "999", "forms": [], "since": "2026-01-01", "until": "2026-02-01",
                                       "cursor": None}, "c2")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages=pages)
    out = json.loads(reader.requests[1]["input"][-1]["output"])
    assert [f["filing_date"] for f in out["data"]["filings"]] == ["2026-10-01"]      # Dec 1 is after as_of
    assert out["data"]["filings"][0]["url"].endswith("/000126000001/a.htm")
    assert json.loads(reader.requests[2]["input"][-1]["output"])["issues"][0]["code"] == "identity_mismatch"
    assert run_json(runs_dir, rid)["sources"] == []          # a listing is leads, never a source


def test_manifest_is_append_only_with_verifiable_checkpoints(runs_dir):
    reader = F.FakeResponses(happy_script())
    _, rid, _ = start(runs_dir, reader)
    path = runs_dir / rid / "research" / "manifest.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert [l["seq"] for l in lines] == list(range(1, len(lines) + 1))
    assert {"leads", "source", "checkpoint"} <= {l["kind"] for l in lines}
    assert research.verify_manifest(runs_dir / rid) == []
    raw = path.read_text().replace('"kind": "source"', '"kind": "sourcex"', 1)
    path.write_text(raw)
    assert research.verify_manifest(runs_dir / rid)          # an edited entry breaks the next checkpoint


# ---------------------------------------------------------------- proposals: checks inside the tool


def _propose_run(runs_dir, events_batches, pages=None, finish=True):
    script = [F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")])]
    script += [F.body([F.call("revise_event", b, f"p{i}") if isinstance(b, dict) else
                       F.call("propose_events", {"events": b}, f"p{i}")]) for i, b in enumerate(events_batches)]
    if finish:
        script.append(lambda req: F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": r, "disposition": "proposal", "related_row_id": None, "reason": "x"} for r in _row_ids(req)],
            "limitations": []}, "fin")]))
    reader = F.FakeResponses(script)
    _, rid, _ = start(runs_dir, reader, pages=pages)
    return reader, rid, run_json(runs_dir, rid)


def _answer(reader, k):
    return json.loads(reader.requests[k]["input"][-1]["output"])


def test_a_failed_check_goes_back_once_as_typed_feedback_and_one_revision_is_accepted(runs_dir):
    bad = F.event("ICARUS readout", "Topline data are expected in Q3 2027.", date_text="Q3 2027",
                  date="2027-07-01", date_end="2027-09-30")
    fixed = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                    rid="e1", rev=1)
    again = {**fixed, "expected_revision": 2}
    reader, rid, run = _propose_run(runs_dir, [[bad], fixed, again])
    first = _answer(reader, 2)["data"]["events"][0]
    assert first["status"] == "needs_repair" and first["issues"][0]["code"] == "quote_not_found"
    assert "once" in first["repair"]
    second = _answer(reader, 3)["data"]["events"][0]
    assert second["status"] == "checked" and second["revision"] == 2 and not second["issues"]
    third = _answer(reader, 4)["data"]["events"][0]
    assert third["status"] == "refused" and third["issues"][0]["code"] == "repair_not_offered"
    row = next(r for r in run["rows"] if r["id"] == "e1")
    assert row["research"]["revision"] == 2 and row["research"]["revisions"][0]["revision"] == 1
    assert row["research"]["revisions"][0]["issues"][0]["code"] == "quote_not_found"
    assert any(s["step"] == "Revised" for s in row["trail"])
    assert run["run"]["research"]["refusals"]          # the refused third attempt is recorded, not dropped


def test_an_unrepaired_failure_stays_a_proposal_for_a_person(runs_dir):
    bad = F.event("ICARUS readout", "Topline data are expected in Q3 2027.", date_text="Q3 2027",
                  date="2027-07-01", date_end="2027-09-30")
    _, rid, run = _propose_run(runs_dir, [[bad]])
    row = next(r for r in run["rows"] if r.get("research"))
    assert row["status"] == "failed" and row["research"]["feedback"]["repair_open"] is True
    st = __import__("runs.state", fromlist=["current_state"]).current_state(run, [])
    assert st["targets"][row["id"]]["needs_attention"] is True


def test_uncaptured_or_malformed_citations_make_no_row_and_are_recorded(runs_dir):
    uncaptured = F.event("X", QUOTE, source="src-r9-ptgx")
    other = F.event("X", QUOTE, ticker="IONS")
    reader, rid, run = _propose_run(runs_dir, [[uncaptured, other]])
    out = _answer(reader, 2)["data"]["events"]
    assert [e["status"] for e in out] == ["refused", "refused"]
    assert out[0]["issues"][0]["code"] == "missing_source" and out[1]["issues"][0]["code"] == "identity_mismatch"
    assert not [r for r in run["rows"] if r.get("research")]
    assert len(run["run"]["research"]["refusals"]) == 2
    bad_json = F.FakeResponses([F.body([{"type": "function_call", "call_id": "z", "name": "propose_events",
                                         "arguments": "{not json"}]),
                                F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")])])
    start(runs_dir, bad_json)
    assert json.loads(bad_json.requests[1]["input"][-1]["output"])["issues"][0]["code"] == "malformed_argument"


def test_multi_span_evidence_checks_every_span(runs_dir):
    spans = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": QUOTE, "word_ids": None,
              "roles": ["action", "timing"]},
             {"source_id": "src-r1-ptgx", "page_number": 1, "quote": "subject to FDA feedback.",
              "word_ids": None, "roles": ["condition"]},
             {"source_id": "src-r1-ptgx", "page_number": 1, "quote": "not on the page at all", "word_ids": ["w99999"],
              "roles": ["context"]}]
    ev = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                 spans=spans, conditions=["subject to FDA feedback"])
    _, _, run = _propose_run(runs_dir, [[ev]])
    row = next(r for r in run["rows"] if r.get("research"))
    ev_checks = row["research"]["evidence"]
    assert [s["quote_match"] for s in ev_checks][:2] == ["pass", "pass"] and ev_checks[2]["quote_match"] != "pass"
    codes = [i["code"] for i in row["research"]["issues"]]
    assert "quote_not_found" in codes and "locator_mismatch" in codes
    assert row["conditions"] == ["subject to FDA feedback"] and row["research"]["conditions_state"] == "stated"


def test_unknown_conditions_stay_null_and_none_stays_empty(runs_dir):
    unknown = F.event("A", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30", conditions=None)
    none = F.event("B", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30", conditions=[])
    _, _, run = _propose_run(runs_dir, [[unknown, none]])
    rows = {r["title"]: r for r in run["rows"] if r.get("research")}
    assert rows["A"]["conditions"] is None and rows["A"]["research"]["conditions_state"] == "not_established"
    assert rows["B"]["conditions"] == [] and rows["B"]["research"]["conditions_state"] == "none_stated"
    RUN_SCHEMA.validate(run)


def test_the_dateline_is_not_the_event_date_unless_anchored(runs_dir):
    dateline = F.event("Pipeline update", "SAN DIEGO, Nov. 2, 2026 -- Protagonist Therapeutics announced",
                       date_text="Nov. 2, 2026", date="2026-11-02")
    anchored = F.event("Update announced", "SAN DIEGO, Nov. 2, 2026 -- Protagonist Therapeutics announced today",
                       date_text="Nov. 2, 2026", date="2026-11-02")
    _, _, run = _propose_run(runs_dir, [[dateline, anchored]])
    rows = {r["title"]: r for r in run["rows"] if r.get("research")}
    assert "dateline_as_event_date" in {i["code"] for i in rows["Pipeline update"]["research"]["issues"]}
    assert "dateline_as_event_date" not in {i["code"] for i in rows["Update announced"]["research"]["issues"]}


def test_exact_repeat_reuses_the_row_and_near_duplicates_are_linked_not_merged(runs_dir):
    pages = {PR1: (200, "text/html", PAGE1),
             PR2: (200, "text/html", F.html("Partner note: ICARUS topline data expected mid-2027."))}
    a = F.event("ICARUS topline", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                program="ICARUS")
    b = F.event("ICARUS topline (partner)", "ICARUS topline data expected mid-2027.", source="src-r2-ptgx",
                date_text="mid-2027", date="2027-05-01", date_end="2027-08-31", program="ICARUS")
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
        F.body([F.call("propose_events", {"events": [a, a]}, "c2")]),
        F.body([F.call("fetch_url", {"url": PR2, "purpose": "x"}, "c1")]),
        F.body([F.call("propose_events", {"events": [b]}, "c3")]),
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "again"}, "c4")]),
        F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"},
            {"row_id": "e2", "disposition": "duplicate", "related_row_id": "e1", "reason": "same readout"}],
            "limitations": []}, "c5")]),
    ])
    _, rid, _ = start(runs_dir, reader, pages=pages)
    run = run_json(runs_dir, rid)
    out = _answer(reader, 2)["data"]["events"]
    assert out[1]["status"] == "duplicate" and out[1]["id"] == out[0]["id"]
    rows = {r["id"]: r for r in run["rows"] if r.get("research")}
    assert set(rows) == {"e1", "e2"}                      # both kept
    assert rows["e1"]["research"]["near_duplicates"] == ["e2"] and rows["e2"]["research"]["near_duplicates"] == ["e1"]
    assert run["run"]["research"]["consolidations"][0]["status"] == "proposed"
    # an identical refetch is the same source with a new access line
    assert len(run["sources"]) == 2
    kinds = [json.loads(l)["kind"] for l in (runs_dir / rid / "research" / "manifest.jsonl").read_text().splitlines()]
    assert "access" in kinds


def test_window_test_uses_the_period_end():
    from datetime import date
    as_of, end = date(2026, 11, 2), date(2027, 5, 2)
    assert research_audit.window_test({"date": "2026-10-01", "date_end": "2026-12-31"}, as_of, end)["result"] == "in_window"
    assert research_audit.window_test({"date": "2026-07-01", "date_end": "2026-09-30"}, as_of, end)["result"] == "outside_window"
    assert research_audit.window_test({"date": "2027-05-02"}, as_of, end)["result"] == "in_window"
    assert research_audit.window_test({"date": "2027-05-03"}, as_of, end)["result"] == "outside_window"
    assert research_audit.window_test({"date": None}, as_of, end)["result"] == "undated"


def test_repeated_identical_action_is_recorded_and_the_loop_goes_on(runs_dir):
    read = {"source_id": "src-r1-ptgx", "pages": [1], "purpose": "re-read"}
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
        F.body([F.call("read_source_pages", read, "c1")]),
        F.body([F.call("read_source_pages", read, "c2")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")]),
    ])
    _, rid, _ = start(runs_dir, reader)
    r = run_json(runs_dir, rid)["run"]["research"]
    assert r["repeats"] and r["repeats"][0]["tool"] == "read_source_pages"
    assert r["companies"]["PTGX"]["status"] == "finished"
    assert any(s["kind"] == "repeat" for s in r["steps"])


# ---------------------------------------------------------------- kill conditions and limits


def test_kill_condition_unpriced_call_stops_paid_work(runs_dir):
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")], u={})])
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    assert meta["unpriced_calls"] == 1 and len(reader.requests) == 1
    c = meta["research"]["companies"]["PTGX"]
    assert c["status"] == "stopped" and c["limitation"]["kind"] == "unpriced"
    assert meta["status"] == "needs_input" and "not published" in meta["status_reason"]


def test_kill_condition_unpriced_model_is_refused_before_any_call(runs_dir):
    reader = F.FakeResponses([F.body([F.message("x")], model="mystery-model"),
                              F.body([F.message("x")], model="mystery-model")])
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    assert meta["unpriced_calls"] == 1 and len(reader.requests) == 1     # the reply named a model with no price
    assert meta["research"]["companies"]["PTGX"]["limitation"]["kind"] == "unpriced"


def test_kill_condition_empty_answer_rate_is_recorded_and_surfaced(runs_dir):
    empty = F.body([F.reasoning()], status="incomplete", incomplete="max_output_tokens")
    script = [empty, F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")]), empty,
              F.body([F.call("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [1], "purpose": "x"}, "c2")]),
              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])]
    reader = F.FakeResponses(script)
    _, rid, _ = start(runs_dir, reader)
    r = run_json(runs_dir, rid)["run"]["research"]
    assert r["kill_conditions"][0]["code"] == "empty_answer_rate"
    assert r["companies"]["PTGX"]["status"] == "finished"           # recorded, not a stop (setting off)
    assert r["companies"]["PTGX"]["empty_answers"] == 2
    assert any("output budget" in s["text"] for s in r["steps"] if s["kind"] == "error")
    # the nudge after an empty answer replays the output then asks for a tool call
    assert reader.requests[1]["input"][-1]["content"] == research.NUDGE_EMPTY


def test_kill_condition_empty_rate_stops_when_a_person_turned_it_on(runs_dir):
    empty = F.body([], status="incomplete", incomplete="max_output_tokens")
    fetch = F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")])
    read = F.body([F.call("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [1], "purpose": "x"}, "c2")])
    reader = F.FakeResponses([empty, fetch, empty, read, read])
    _, rid, _ = start(runs_dir, reader, limits={"stop_on_empty_rate": True})
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "stopped" and "empty" in c["limitation"]["reason"]


def test_two_empty_answers_in_a_row_are_a_technical_failure(runs_dir):
    empty = F.body([], status="incomplete", incomplete="max_output_tokens")
    reader = F.FakeResponses([empty, empty])
    _, rid, _ = start(runs_dir, reader)
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "failed" and "two empty answers" in c["status_reason"]


def test_kill_condition_spend_cap_hit_stops_with_a_recorded_limitation_and_never_publishes(runs_dir):
    big = F.usage(i=400_000, o=8000)          # more than the reservation assumed: recorded, and the cap stops the next
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")], u=big),
                              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c2")])])
    _, rid, _ = start(runs_dir, reader, limit=0.3)
    meta = run_json(runs_dir, rid)["run"]
    assert len(reader.requests) == 1                      # the call that crossed the cap was the last one
    c = meta["research"]["companies"]["PTGX"]
    assert c["status"] == "stopped" and c["limitation"]["kind"] == "spend_cap"
    assert "Spending cap reached" in c["limitation"]["reason"]
    assert meta["research"]["reservation_exceeded"][0]["input_tokens"] == 400_000
    assert meta["status"] == "needs_input" and meta["research"]["audit"]["published"] is False
    assert meta["research"]["audit"]["complete"] is False


def test_optional_limits_are_off_by_default_and_work_when_turned_on(runs_dir):
    assert research.validate_limits(None) == {**{k: None for k in research.LIMIT_KEYS},
                                              "concurrency": 3, "stop_on_empty_rate": False}
    with pytest.raises(ValueError):
        research.validate_limits({"max_pages_per_company": -1})
    with pytest.raises(ValueError):
        research.validate_limits({"surprise": 1})
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")])])
    _, rid, _ = start(runs_dir, reader, limits={"max_requests_per_company": 1, "max_searches_per_request": 2})
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert c["limitation"]["kind"] == "limit" and "turned on" in c["limitation"]["reason"]
    assert reader.requests[0]["max_tool_calls"] == 2


def test_concurrency_queues_every_company_and_drops_none(runs_dir):
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    reader = F.PerCompany({t: [F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, t)])]
                           for t in tickers})
    _, rid, _ = start(runs_dir, reader, companies=tickers, limits={"concurrency": 1},
                      resolve=lambda t: (str(100 + tickers.index(t)), t))
    cs = run_json(runs_dir, rid)["run"]["research"]["companies"]
    assert all(cs[t]["status"] == "finished" for t in tickers)
    assert run_json(runs_dir, rid)["run"]["status"] == "done"


def test_ten_companies_at_most_and_tickers_validated():
    with pytest.raises(ValueError):
        jobs.validate_choices({"mode": "research", "companies": [f"T{i}" for i in range(11)]})
    with pytest.raises(ValueError):
        jobs.validate_choices({"mode": "research", "companies": ["not a ticker"]})
    with pytest.raises(ValueError):
        jobs.validate_choices({"mode": "research", "companies": ["PTGX"], "models": "all"})
    ch = jobs.validate_choices({"mode": "research", "companies": ["ptgx"]})
    assert ch["companies"] == ["PTGX"] and ch["models"] == "one" and ch["spending_limit_usd"] == 30.0


def test_a_person_stop_ends_research_before_the_next_call(runs_dir):
    holder = {}

    def stop_then_answer(req):
        research.request_stop(holder["dir"], holder["rid"])
        return F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")])
    reader = F.FakeResponses([stop_then_answer])
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}),
                          as_of="2026-11-02")
    holder.update(dir=runs_dir, rid=run["run"]["id"])
    research.run_research(runs_dir, run["run"]["id"], reader, F.tools({PR1: (200, "text/html", PAGE1)}))
    c = run_json(runs_dir, run["run"]["id"])["run"]["research"]["companies"]["PTGX"]
    assert c["limitation"]["kind"] == "person" and len(reader.requests) == 1


def test_a_research_run_is_not_resumed_in_s1(runs_dir):
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")], u={})])
    _, rid, _ = start(runs_dir, reader)
    with pytest.raises(jobs.JobError) as e:
        jobs.resume_run(runs_dir, rid, accept_unknown_cost=True)
    assert "not resumed in S1" in e.value.message


def test_missing_key_stops_before_any_call(runs_dir):
    reader = F.FakeResponses([], key=False)
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    assert meta["status"] == "needs_input" and reader.requests == []


def test_page_mode_is_unchanged_by_the_research_choices():
    with pytest.raises(ValueError):
        jobs.validate_choices({"companies": ["PTGX"], "research_limits": {}})


# ---------------------------------------------------------------- Manus 15:01: exact-repeat identity, Keep binding


@pytest.mark.parametrize("change", [{"conditions": ["subject to FDA feedback"]}, {"subject_company": "Takeda"},
                                    {"relation": "partner"}, {"program": "rusfertide"}, {"modality": "oral"},
                                    {"indication": "PV"}, {"action": "NDA"}])
def test_a_materially_different_proposal_is_never_an_exact_repeat(runs_dir, change):
    a = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30",
                conditions=None)
    b = {**a, **change}
    reader, rid, run = _propose_run(runs_dir, [[a, b]])
    out = _answer(reader, 2)["data"]["events"]
    assert [e["status"] for e in out] == ["checked", "checked"] and out[0]["id"] != out[1]["id"]
    assert len([r for r in run["rows"] if r.get("research")]) == 2


def test_changed_roles_or_primary_span_is_a_new_proposal_and_a_true_repeat_is_not(runs_dir):
    a = F.event("ICARUS readout", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30")
    roles = {**a, "evidence": [{**a["evidence"][0], "roles": ["timing"]}]}
    _, _, run = _propose_run(runs_dir, [[a, roles, dict(a)]])
    assert len([r for r in run["rows"] if r.get("research")]) == 2


def _kept_research_run(runs_dir):
    spans = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": QUOTE, "word_ids": None,
              "roles": ["action", "timing"]},
             {"source_id": "src-r2-ptgx", "page_number": 1, "quote": "ICARUS topline data expected mid-2027.",
              "word_ids": None, "roles": ["context"]}]
    ev = F.event("ICARUS", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30", spans=spans)
    first = F.event("ICARUS (company)", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30")
    pages = {PR1: (200, "text/html", PAGE1),
             PR2: (200, "text/html", F.html("Partner note: ICARUS topline data expected mid-2027."))}
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
        F.body([F.call("propose_events", {"events": [first]}, "c1a")]),      # A-TO-Z-001: settle PR1 first
        F.body([F.call("fetch_url", {"url": PR2, "purpose": "x"}, "c1")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
        F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": r, "disposition": "proposal", "related_row_id": None, "reason": "x"} for r in ("e1", "e2")],
            "limitations": []}, "c3")])])
    client, rid, _ = start(runs_dir, reader, pages=pages)
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e2", "action": "keep", "note": None,
                                                           "fix": None}).status_code == 200
    return run_json(runs_dir, rid), _decisions(runs_dir, rid)


@pytest.mark.parametrize("mutate", ["secondary_words", "secondary_document", "secondary_quote", "secondary_role",
                                    "revision"])
def test_a_keep_expires_when_secondary_evidence_or_the_revision_changes(runs_dir, mutate):
    from runs.state import current_state
    run, decisions = _kept_research_run(runs_dir)
    assert current_state(run, decisions)["targets"]["e2"]["accepted"] is True
    row = next(r for r in run["rows"] if r["id"] == "e2")
    second = next(s for s in run["sources"] if s["id"] == "src-r2-ptgx")
    if mutate == "secondary_words":
        second["pages"][0]["words_sha256"] = "0" * 64
    elif mutate == "secondary_document":
        second["retrieval"]["original_sha256"] = "1" * 64
    elif mutate == "secondary_quote":
        row["research"]["evidence"][1]["quote"] = "ICARUS topline data"
    elif mutate == "secondary_role":
        row["research"]["evidence"][1]["roles"] = ["timing"]
    else:
        row["research"]["revision"] = 2
    st = current_state(run, decisions)["targets"]["e2"]
    assert st["accepted"] is False and st["review_history"]


def test_non_research_rows_keep_a_byte_identical_review_basis():
    import subprocess
    import types
    old_src = subprocess.check_output(["git", "-C", str(REPO), "show", "origin/main:runs/review.py"], text=True)
    old = types.ModuleType("old_review")
    exec(compile(old_src, "old_review.py", "exec"), old.__dict__)
    from runs import review
    from runs.state import current_state
    for name in ("sample", "demo", "ptgx-r4a", "ptgx-r5"):
        run = json.loads((REPO / "runs" / name / "run.json").read_text())
        st = current_state(run, [])
        for r in run["rows"]:
            values = st["targets"][r["id"]]["values"]
            assert review.review_basis(run, r, values) == old.review_basis(run, r, values)


# ---------------------------------------------------------------- Manus 15:03 timing span, 15:04 strict cap and CSP

TIMING_PAGE = F.html("Protagonist plans to report topline data from ICARUS.",
                     "These topline results are expected in Q1 2027.")
TP = "https://ir.example.com/timing"


def _timing_run(runs_dir, date_text, date, date_end):
    spans = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": "plans to report topline data from ICARUS.",
              "word_ids": None, "roles": ["action", "program"]},
             {"source_id": "src-r1-ptgx", "page_number": 1, "quote": "These topline results are expected in Q1 2027.",
              "word_ids": None, "roles": ["timing"]}]
    ev = F.event("ICARUS topline", spans[0]["quote"], date_text=date_text, date=date, date_end=date_end, spans=spans)
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": TP, "purpose": "x"}, "c0")]),
                              F.body([F.call("propose_events", {"events": [ev]}, "c1")]),
                              F.body([F.call("finish_draft", {"dispositions": [
                                  {"row_id": "e1", "disposition": "proposal", "related_row_id": None,
                                   "reason": "x"}], "limitations": []}, "c2")])])
    _, rid, _ = start(runs_dir, reader, pages={TP: (200, "text/html", TIMING_PAGE)})
    return run_json(runs_dir, rid)


def test_timing_in_a_separate_span_is_checked_against_that_span(runs_dir):
    from runs.state import current_state
    run = _timing_run(runs_dir, "Q1 2027", "2027-01-01", "2027-03-31")
    row = next(r for r in run["rows"] if r["id"] == "e1")
    checks = {c["name"]: c for c in row["checks"]}
    assert checks["date_match"]["result"] == "pass", checks["date_match"]
    assert checks["date_match"]["reason"].startswith("Date compared against timing span src-r1-ptgx p1")
    assert checks["quote_match"]["result"] == "pass"            # the primary span keeps its quote check
    assert row["status"] == "matched" and row["research"]["timing_span_index"] == 1
    st = current_state(run, [])["targets"]["e1"]
    assert st["checks_stale"] is False                           # the bound check stays current on load


def test_a_timing_span_with_a_different_period_fails(runs_dir):
    run = _timing_run(runs_dir, "Q3 2027", "2027-07-01", "2027-09-30")
    row = next(r for r in run["rows"] if r["id"] == "e1")
    assert {c["name"]: c["result"] for c in row["checks"]}["date_match"] == "fail"
    assert "timing_mismatch" in {i["code"] for i in row["research"]["issues"]}


def test_strict_admission_refuses_a_call_whose_bound_would_cross_the_cap(runs_dir):
    assert jobs.reserve_budget("strict-test", 29.99, 30.0, 0.25, alone_may_cross=False) is None
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"]}),
                          as_of="2026-11-02")
    path = runs_dir / run["run"]["id"] / "run.json"
    doc = json.loads(path.read_text())
    doc["run"]["spend_usd"] = 29.99
    path.write_text(json.dumps(doc))
    reader = F.FakeResponses([])
    research.run_research(runs_dir, run["run"]["id"], reader, F.tools({}))
    meta = run_json(runs_dir, run["run"]["id"])["run"]
    assert reader.requests == []
    reason = meta["research"]["companies"]["PTGX"]["limitation"]["reason"]
    assert "would exceed the $30.00 cap" in reason
    assert meta["research"]["reservation"]["assumptions"]["searches_per_request"] == 5
    assert meta["research"]["reservation"]["assumptions"]["search_context_tokens_per_search"] == 20000


def test_csp_comes_first_without_a_head_and_escaped_css_urls_do_not_survive():
    page = ('<p>Our Content-Security-Policy is strict.</p>'
            '<div style="background:u\\72l(file:///etc/passwd)">x</div>'
            '<style>p{background:u\\72l(http://127.0.0.1:8777/a)}</style>')
    snap, counts = rs.sanitize_html(page)
    assert snap.startswith(rs.CSP_META)
    assert "url(" not in snap.lower() and "\\72" not in snap
    assert counts["css_escapes_removed"] == 2
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in rs.ISOLATION_FLAGS


# ---------------------------------------------------------------- Manus 15:27: the timing check binds its own span

TP2 = "https://ir.example.com/timing-note"


def _two_source_timing_run(runs_dir, program=None, client_out=None):
    spans = [{"source_id": "src-r1-ptgx", "page_number": 1, "quote": "plans to report topline data from ICARUS.",
              "word_ids": None, "roles": ["action", "program"]},
             {"source_id": "src-r2-ptgx", "page_number": 1, "quote": "These topline results are expected in Q1 2027.",
              "word_ids": None, "roles": ["timing"]}]
    ev = F.event("ICARUS topline", spans[0]["quote"], date_text="Q1 2027", date="2027-01-01", date_end="2027-03-31",
                 spans=spans, program=program)
    pages = {TP: (200, "text/html", F.html("Protagonist plans to report topline data from ICARUS.")),
             TP2: (200, "text/html", F.html("These topline results are expected in Q1 2027."))}
    plan = F.event("ICARUS topline (timing not stated here)", spans[0]["quote"], program=program)
    plan["evidence"] = [dict(spans[0])]
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": TP, "purpose": "x"}, "c0")]),
                              F.body([F.call("propose_events", {"events": [plan]}, "c0b")]),   # A-TO-Z-001
                              F.body([F.call("fetch_url", {"url": TP2, "purpose": "x"}, "c1")]),
                              F.body([F.call("propose_events", {"events": [ev]}, "c2")]),
                              F.body([F.call("finish_draft", {"dispositions": [
                                  {"row_id": r, "disposition": "proposal", "related_row_id": None,
                                   "reason": "x"} for r in ("e1", "e2")], "limitations": []}, "c3")])])
    client, rid, _ = start(runs_dir, reader, pages=pages)
    if client_out is not None:
        client_out.update(client=client, rid=rid)
    return run_json(runs_dir, rid)


@pytest.mark.parametrize("mutate", ["timing_words_hash", "timing_document_hash", "timing_published_date",
                                    "timing_wording"])
def test_the_date_check_goes_stale_when_the_timing_evidence_changes(runs_dir, mutate):
    from runs.state import current_state
    run = _two_source_timing_run(runs_dir)
    row = next(r for r in run["rows"] if r["id"] == "e2")
    date_check = {c["name"]: c for c in row["checks"]}["date_match"]
    assert date_check["result"] == "pass" and row["research"]["timing"]["source_id"] == "src-r2-ptgx"
    assert date_check["assessment"]["inputs"]["timing_span"]["source_id"] == "src-r2-ptgx"
    assert current_state(run, [])["targets"]["e2"]["checks_stale"] is False
    second = next(s for s in run["sources"] if s["id"] == "src-r2-ptgx")
    if mutate == "timing_words_hash":
        second["pages"][0]["words_sha256"] = "0" * 64
    elif mutate == "timing_document_hash":
        second["sha256"] = "1" * 64
    elif mutate == "timing_published_date":
        second["published_date"] = "2026-01-01"
    else:
        row["research"]["timing"]["quote"] = "These topline results are expected in Q3 2027."
    st = current_state(run, [])["targets"]["e2"]
    assert st["checks_stale"] is True and st["effective_checks"] == []


# ---------------------------------------------------------------- Manus 15:54: a person's fix re-checks the timing span


def _fix(client, rid, field, frm, to):
    r = client.post(f"/api/runs/{rid}/decisions", json={"target": "e2", "action": "fix", "note": None,
                                                        "fix": {"field": field, "from": frm, "to": to}})
    assert r.status_code == 200, r.text
    return client.get(f"/api/runs/{rid}").json()["state"]["targets"]["e2"]


def test_a_program_fix_on_a_timing_span_row_keeps_the_date_check_on_the_timing_span(runs_dir):
    box = {}
    _two_source_timing_run(runs_dir, program="ICARUS", client_out=box)
    st = _fix(box["client"], box["rid"], "program", "ICARUS", "ICARUS Phase 3")
    date = {c["name"]: c for c in st["effective_checks"]}["date_match"]
    assert date["result"] == "pass", date["reason"]
    assert date["reason"].startswith("Date compared against timing span src-r2-ptgx p1")
    assert st["checks_stale"] is False
    line = [json.loads(l) for l in (runs_dir / box["rid"] / "decisions.jsonl").read_text().splitlines()][-1]
    assert line["action"] == "recheck" and line["recheck"]["result"] != "fail"


def test_a_date_fix_is_rechecked_against_the_timing_span(runs_dir):
    box = {}
    _two_source_timing_run(runs_dir, program="ICARUS", client_out=box)
    st = _fix(box["client"], box["rid"], "date_text", "Q1 2027", "Q3 2027")
    date = {c["name"]: c for c in st["effective_checks"]}.get("date_match")
    line = [json.loads(l) for l in (runs_dir / box["rid"] / "decisions.jsonl").read_text().splitlines()][-1]
    checks = {c["name"]: c for c in line["recheck"]["after"]["checks"]}
    assert checks["date_match"]["result"] == "fail"
    assert checks["date_match"]["reason"].startswith("Date compared against timing span src-r2-ptgx p1")
    assert line["recheck"]["result"] == "fail"
    before = {c["name"]: c for c in line["recheck"]["before"]["checks"]}
    assert before["date_match"]["result"] == "pass"       # the old value checked against the same timing span


# ---------------------------------------------------------------- live smoke 16:00: render fix, text capture, 403, incomplete

TENQ_URL = "https://www.sec.gov/Archives/edgar/data/1377121/000110465926091123/ptgx-20260630x10q.htm"
TENQ = (REPO / "tests" / "fixtures" / "ptgx-20260630x10q.htm.gz")
TENQ_QUOTE = ("We expect research and development expenses to increase significantly in the second half of 2026 "
              "compared to the first half of 2026.")


def _tenq_bytes():
    import gzip
    return gzip.decompress(TENQ.read_bytes())


def test_isolation_flags_never_include_the_flag_that_blocks_pdf_output():
    assert not any(f.startswith("--blink-settings") for f in rs.ISOLATION_FLAGS)
    assert "--disable-javascript" in rs.ISOLATION_FLAGS


def test_the_real_reason_of_a_render_failure_is_recorded(tmp_path):
    def no_pdf(html, pdf):
        raise rs.CaptureFailed("Chrome exited normally after 0.6s but wrote no PDF (snapshot 10 characters).")
    fetched = rs.Fetched("https://example.org/a", "https://example.org/a", 200, "text/html",
                         F.html("Topline data in Q2 2027."), "2026-11-02T18:00:00+00:00")
    got = rs.capture_document(fetched, tmp_path, "src-r1-x", render_html=no_pdf)
    assert got["rendered"]["form"] == "text"
    assert got["rendered"]["render_failure"].startswith("Chrome exited normally after 0.6s but wrote no PDF")
    assert "(CaptureFailed)" not in json.dumps(got["rendered"])


def test_the_ptgx_10q_falls_back_to_a_text_capture_and_research_continues(runs_dir):
    def broken(html, pdf):
        raise rs.CaptureFailed("Chrome exited normally after 0.6s but wrote no PDF (snapshot 1703711 characters).")
    import tempfile
    from checks import event_checks
    with tempfile.TemporaryDirectory() as tmp:       # the same deterministic text capture: which page holds the quote
        probe = rs.capture_document(rs.Fetched(TENQ_URL, TENQ_URL, 200, "text/html", _tenq_bytes()), Path(tmp),
                                    "probe", render_html=broken)
    page_no = next(w["page_number"] for _, w in probe["pages"]
                   if event_checks.quote_match(TENQ_QUOTE, w["words"]).result == "pass")
    ev = F.event("R&D expense increase H2 2026", TENQ_QUOTE, source="src-r1-ptgx", page=page_no,
                 date_text="second half of 2026", date="2026-07-01", date_end="2026-12-31", type_hint="financial")
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": TENQ_URL, "purpose": "10-Q"}, "c0")]),
                              F.body([F.call("propose_events", {"events": [ev]}, "c1")]),
                              F.body([F.call("finish_draft", {"dispositions": [
                                  {"row_id": "e1", "disposition": "context", "related_row_id": None,
                                   "reason": "x"}], "limitations": []}, "c2")])])
    tools = F.tools({TENQ_URL: (200, "text/html", _tenq_bytes())})
    tools.render_html = broken
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    rid = TestClient(app).post("/api/runs/new", json={"mode": "research", "companies": ["PTGX"],
                                                      "as_of": "2026-11-02"}).json()["id"]
    run = run_json(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    src = run["sources"][0]
    assert src["kind"] == "filing" and src["retrieval"]["rendered"]["form"] == "text"
    assert "wrote no PDF" in src["retrieval"]["rendered"]["render_failure"]
    assert len(src["pages"]) > 20 and all(p["text_capture"]["reason"] for p in src["pages"])
    assert src["retrieval"]["rendered"]["removed"]["hidden_elements_removed"] > 0     # the iXBRL header is not text
    first = json.loads(reader.requests[1]["input"][-1]["output"])["data"]
    assert any("QUARTERLY REPORT" in l for l in first["text"][0]["lines"])           # the model reads the text
    row = next(r for r in run["rows"] if r["id"] == "e1")
    checks = {c["name"]: c for c in row["checks"]}
    assert checks["quote_match"]["result"] == "pass" and "captured text" in checks["quote_match"]["reason"]
    assert "is not established (text capture)" in checks["quote_match"]["reason"]
    assert checks["selector_match"]["result"] == "could_not_run"
    assert checks["selector_match"]["reason"].startswith("not_applicable: text capture (page image not rendered). "
                                                         "Render failure: Chrome exited normally")
    assert checks["date_match"]["result"] == "pass" and checks["source_rule"]["result"] == "pass"
    assert row["status"] != "matched"                       # a box that cannot be checked is never a pass
    from runs.state import current_state
    assert current_state(run, [])["targets"]["e1"]["checks_stale"] is False
    line = next(json.loads(l) for l in (runs_dir / rid / "research" / "manifest.jsonl").read_text().splitlines()
                if json.loads(l)["kind"] == "source")
    assert line["form"] == "text" and line["text_sha256"] and "wrote no PDF" in line["render_failure"]


@pytest.mark.skipif(not any(Path(p).is_file() for p in (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)), reason="Google Chrome is not installed")
def test_the_ptgx_10q_renders_offline_with_real_chrome_to_pages_with_words(tmp_path):
    fetched = rs.Fetched(TENQ_URL, TENQ_URL, 200, "text/html", _tenq_bytes(), "2026-11-02T18:00:00+00:00")
    got = rs.capture_document(fetched, tmp_path, "src-r1-ptgx")
    assert got["rendered"]["form"] == "rendered", got["rendered"].get("render_failure")
    assert len(got["pages"]) > 20 and sum(len(w["words"]) for _, w in got["pages"]) > 10000


def test_a_403_is_could_not_access_and_the_primary_source_path_goes_on(runs_dir):
    blocked = "https://www.morningstar.com/news/ptgx-approval"
    primary = "https://www.sec.gov/Archives/edgar/data/1377121/000110465926080001/ex99-1.htm"
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": blocked, "purpose": "news"}, "c0")]),
        F.body([F.call("fetch_url", {"url": primary, "purpose": "the 8-K EX-99 release"}, "c1")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c2")])])
    _, rid, tools = start(runs_dir, reader, pages={blocked: (403, "text/html", b""),
                                                   primary: (200, "text/html", PAGE1)})
    out = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out["issues"][0]["code"] == "could_not_access" and "primary origin" in out["issues"][0]["message"]
    assert out["data"]["http_status"] == 403 and out["data"]["retrieved_at"]
    run = run_json(runs_dir, rid)
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["capture_failures"][0]["outcome"] == "could_not_access" and c["capture_failures"][0]["url"] == blocked
    assert c["status"] == "finished" and run["sources"][0]["url"] == primary and run["sources"][0]["kind"] == "filing"
    assert "never work around a refusal" in rc.CONTROLLER
    # one honest request each, no retry or disguise (plus the SEC submissions list for the document's filing date)
    assert tools.fetcher.asked == [blocked, primary, rs.SUBMISSIONS_URL.format(cik="1377121")]


def test_an_incomplete_answer_with_finish_draft_never_finalizes_the_company(runs_dir):
    ev = F.event("ICARUS", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30")
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c1"),
                F.call("finish_draft", {"dispositions": [{"row_id": "e1", "disposition": "proposal",
                                                          "related_row_id": None, "reason": "x"}],
                                        "limitations": []}, "c2")],
               status="incomplete", incomplete="max_output_tokens")])
    _, rid, _ = start(runs_dir, reader)
    run = run_json(runs_dir, rid)
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "stopped" and c["limitation"]["kind"] == "incomplete"
    assert "max_output_tokens" in c["limitation"]["reason"] and c["finish"] is None
    assert [r["id"] for r in run["rows"] if r.get("research")] == ["e1"]       # the usable proposal is kept
    assert any(x["tool"] == "finish_draft" and x["issues"][0]["code"] == "incomplete_answer"
               for x in run["run"]["research"]["refusals"])
    assert len(reader.requests) == 2 and run["run"]["status"] == "needs_input"


def test_the_reservation_covers_the_live_reply_that_exceeded_it():
    """Live smoke 2026-11-02: a reply used 88,011 input tokens; the old bound (8,000 per search) reserved 57,279. The
    same request is now reserved above what it used. max_tool_calls stays unsent (no search limit is on)."""
    old_bound = 57_279
    request_bytes = old_bound - 5 * 8000 - research.REQUEST_OVERHEAD_TOKENS      # 13,183 bytes
    body = rr.build_request(instructions="", input_items=[], functions=[], searches_left=10 ** 9)
    body.pop("max_tool_calls")
    body["input"] = [{"role": "user", "content": "x" * (request_bytes - rr.request_size(body) - 30)}]
    assert abs(rr.request_size(body) - request_bytes) < 40
    bound, searches = research.input_token_bound(body)
    assert searches == research.ASSUMED_SEARCHES_PER_REQUEST and bound > 88_011
    assert research.SEARCH_CONTEXT_TOKENS == 20000


# ---------------------------------------------------------------- A-TO-Z-001: one source at a time, compaction, usage


def _big_page(n):
    words = " ".join(f"filler{n}word{i}" for i in range(1500))
    return F.html(f"Source {n}: Topline data for program P{n} are expected in Q2 2027.", words)


def test_a_to_z_refuses_a_second_capture_until_the_first_source_is_settled(runs_dir):
    a, b = "https://ir.example.com/a", "https://ir.example.com/b"
    subs = {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": []}}}
    ev = F.event("P2 topline", "Source 2: Topline data for program P2 are expected in Q2 2027.",
                 source="src-r2-ptgx", date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30")
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": a, "purpose": "x"}, "c0")]),
        F.body([F.call("fetch_url", {"url": b, "purpose": "x"}, "c1")]),           # refused: a is unsettled
        F.body([F.call("fetch_edgar", {"cik": "1377121", "forms": [], "since": "2026-01-01",
                                       "until": "2026-11-01", "cursor": None}, "c2")]),   # leads are not limited
        F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "a staffing note"}, "c3")]),
        F.body([F.call("fetch_url", {"url": b, "purpose": "x"}, "c4")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c5")]),
        F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"}],
            "limitations": []}, "c6")])])
    _, rid, tools = start(runs_dir, reader, pages={
        a: (200, "text/html", F.html("Source 1 staffing note.")), b: (200, "text/html", _big_page(2)),
        rs.SUBMISSIONS_URL.format(cik="1377121"): (200, "application/json", json.dumps(subs).encode())})
    refused = json.loads(reader.requests[2]["input"][-1]["output"])
    assert refused["issues"][0]["code"] == "settle_first" and refused["data"]["unsettled_sources"] == ["src-r1-ptgx"]
    assert tools.fetcher.asked.count(b) == 1                     # the refused capture never reached the site
    assert json.loads(reader.requests[3]["input"][-1]["output"])["status"] == "ok"   # the listing ran
    run = run_json(runs_dir, rid)
    cov = run["run"]["research"]["coverage"]
    assert any(c.get("a_to_z_gap") for c in cov) and any(c.get("nothing_material") for c in cov)
    c = run["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["finish"]["unsettled_sources"] == []
    assert "A-TO-Z-001" in rc.CONTROLLER and "nothing_material" in rc.ARGUMENTS


def _cycle_script(k):
    script = []
    for n in range(1, k + 1):
        url = f"https://ir.example.com/s{n}"
        quote = f"Source {n}: Topline data for program P{n} are expected in Q2 2027."
        script.append(F.body([F.call("fetch_url", {"url": url, "purpose": "x"}, f"f{n}")]))
        script.append(F.body([F.call("propose_events", {"events": [F.event(
            f"P{n} topline", quote, source=f"src-r{n}-ptgx", date_text="Q2 2027", date="2027-04-01",
            date_end="2027-06-30", program=f"P{n}")]}, f"p{n}")]))
    script.append(lambda req: F.body([F.call("finish_draft", {"dispositions": [
        {"row_id": f"e{n}", "disposition": "proposal", "related_row_id": None, "reason": "x"}
        for n in range(1, k + 1)], "limitations": []}, "fin")]))
    pages = {f"https://ir.example.com/s{n}": (200, "text/html", _big_page(n)) for n in range(1, k + 1)}
    return script, pages


def test_compaction_stubs_settled_page_text_and_keeps_every_call_paired(runs_dir):
    script, pages = _cycle_script(3)
    reader = F.FakeResponses(script)
    start(runs_dir, reader, pages=pages)
    last = reader.requests[-1]["input"]
    calls = [i["call_id"] for i in last if i.get("type") == "function_call"]
    outs = [i["call_id"] for i in last if i.get("type") == "function_call_output"]
    assert calls == outs and len(calls) == research.REPLAY_TURNS   # every replayed call keeps its output, in order
    summary = next(i for i in last if i.get("role") == "user" and i["content"].startswith("Research so far"))
    assert "source src-r1-ptgx" in summary["content"] and "proposal e1" in summary["content"]
    for i, item in enumerate(last):
        if item.get("type") == "function_call_output":
            assert last[i - 1].get("type") == "function_call" and last[i - 1]["call_id"] == item["call_id"]
    fetch_outs = [json.loads(i["output"]) for i in last if i.get("type") == "function_call_output"
                  and i["call_id"].startswith("f")]
    assert all(o["data"]["compacted"] and o["data"]["pages_read"] == [1] and o["data"]["settled_by"]
               for o in fetch_outs)
    assert "filler1word" not in json.dumps(last)                  # the settled page text is gone from the replay


def test_bytes_per_turn_stay_bounded_after_compaction(runs_dir):
    script, pages = _cycle_script(6)
    reader = F.FakeResponses(script)
    _, rid, _ = start(runs_dir, reader, pages=pages)
    sizes = [rr.request_size(r) for r in reader.requests]
    page_text = len(json.dumps(json.loads(reader.requests[1]["input"][-1]["output"])))
    # the turn right after each fetch carries one page of text; after compaction later turns grow only by stubs
    after_fetch = sizes[1::2]
    assert max(after_fetch) - min(after_fetch) < 6 * 3000 < page_text * 6
    assert sizes[-1] < sizes[1] + 6 * 3000
    usage = [s for s in run_json(runs_dir, rid)["run"]["research"]["steps"] if s["kind"] == "usage"]
    assert len(usage) == len(reader.requests)
    assert [u["refs"]["input_bytes"] for u in usage] == sizes and usage[0]["refs"]["input_tokens"] == 1000



# ---------------------------------------------------------------- live run 3: ids are code's bookkeeping, not the model's


def test_a_new_proposal_carries_no_id_fields_and_is_accepted(runs_dir):
    ev = F.event("ICARUS", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30")
    assert "id" not in ev and "expected_revision" not in ev
    assert "id" not in rc.EVENT["properties"] and "expected_revision" not in rc.EVENT["properties"]
    reader, _, run = _propose_run(runs_dir, [[ev]])
    assert _answer(reader, 2)["data"]["events"][0]["id"] == "e1"


def test_ids_on_a_new_proposal_are_refused_naming_the_exact_fields(runs_dir):
    ev = {**F.event("ICARUS", QUOTE, date_text="Q2 2027", date="2027-04-01", date_end="2027-06-30"),
          "id": None, "expected_revision": None}
    reader, _, run = _propose_run(runs_dir, [[ev]])
    msg = _answer(reader, 2)["issues"][0]["message"]
    assert "'expected_revision', 'id'" in msg and "events/0" in msg and "revise_event" in msg


def test_a_revision_without_its_ids_is_refused_with_a_precise_message():
    args, problems = rc.parse_arguments("revise_event", json.dumps({"event": F.event("x", QUOTE)}))
    assert args is None
    msgs = [p["message"] for p in problems]
    assert any("required field 'id' is missing (the row id code assigned" in m for m in msgs)
    assert any("required field 'expected_revision' is missing" in m for m in msgs)


def test_limits_002_values():
    assert rr.MAX_OUTPUT_TOKENS == 32768 and rs.FETCH_TIMEOUT_S == 180
    assert rs.MAX_BYTES == 25 * 1024 * 1024 and rs.MAX_REDIRECTS == 5 and research.MAX_COMPANIES == 10



def test_an_sec_document_captured_by_url_takes_its_filing_date_so_year_end_has_a_year(runs_dir):
    """Live run 3, e6: 'by year-end' on an EX-99 fetched by URL failed because its published date was unknown."""
    url = "https://www.sec.gov/Archives/edgar/data/1377121/000110465926091099/tm2622272d1_ex99-2.htm"
    subs = {"filings": {"recent": {"form": ["8-K"], "filingDate": ["2026-09-08"],
                                   "accessionNumber": ["0001104659-26-091099"], "primaryDocument": ["x.htm"]}}}
    quote = "Potential share buyback program announcement by year-end."
    ev = F.event("Buyback announcement", quote, date_text="by year-end", type_hint="corporate")
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": url, "purpose": "deck"}, "c0")]),
                              F.body([F.call("propose_events", {"events": [ev]}, "c1")]),
                              F.body([F.call("finish_draft", {"dispositions": [
                                  {"row_id": "e1", "disposition": "proposal", "related_row_id": None,
                                   "reason": "x"}], "limitations": []}, "c2")])])
    _, rid, _ = start(runs_dir, reader, pages={url: (200, "text/html", F.html(quote)),
                                               rs.SUBMISSIONS_URL.format(cik="1377121"):
                                                   (200, "application/json", json.dumps(subs).encode())})
    run = run_json(runs_dir, rid)
    assert run["sources"][0]["published_date"] == "2026-09-08" and "filed 2026-09-08" in run["sources"][0]["title"]
    row = next(r for r in run["rows"] if r["id"] == "e1")
    assert {c["name"]: c["result"] for c in row["checks"]}["date_match"] == "pass"


def test_guidance_says_code_derives_period_bounds_and_quotes_are_whole():
    assert "set date and date_end to null: code derives the bounds" in rc.CONTROLLER
    assert "Quote whole sentences exactly as printed, including the date words" in rc.CONTROLLER
    from checks import event_checks as e
    q = "into a comprehensive Phase 2 psoriasis program, with initiation expected in early Q1 2027."
    assert e.date_match("early Q1 2027", None, q, date_end=None).result == "pass"       # null bounds: derived
    assert e.date_match("early Q1 2027", "2027-01-01", q, date_end="2027-03-31").result == "fail"   # check unchanged



def test_a_40_turn_run_keeps_input_bytes_under_a_ceiling(runs_dir):
    """Live run 3: input regrew to 297,910 bytes by turn 45. With bounded replay, 40 turns of fetch/propose with
    encrypted reasoning, search results and large proposals stay under a flat ceiling."""
    k = 20
    script = []
    for n in range(1, k + 1):
        url = f"https://ir.example.com/s{n}"
        quote = f"Source {n}: Topline data for program P{n} are expected in Q2 2027."
        reasoning = {"type": "reasoning", "id": f"rs{n}", "summary": [], "encrypted_content": "E" * 4000}
        script.append(F.body([reasoning, F.search(f"query {n}", [f"https://lead{n}-{j}.example.com" for j in range(10)],
                                                  sid=f"ws{n}"),
                              F.call("fetch_url", {"url": url, "purpose": "x"}, f"f{n}")]))
        ev = F.event(f"P{n} topline " + "detail " * 60, quote, source=f"src-r{n}-ptgx", date_text="Q2 2027",
                     program=f"P{n}")
        script.append(F.body([dict(reasoning, id=f"rp{n}"), F.call("propose_events", {"events": [ev]}, f"p{n}")]))
    script.append(lambda req: F.body([F.call("finish_draft", {"dispositions": [
        {"row_id": f"e{n}", "disposition": "proposal", "related_row_id": None, "reason": "x"}
        for n in range(1, k + 1)], "limitations": []}, "fin")]))
    pages = {f"https://ir.example.com/s{n}": (200, "text/html", _big_page(n)) for n in range(1, k + 1)}
    reader = F.FakeResponses(script)
    _, rid, _ = start(runs_dir, reader, pages=pages)
    assert run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]["status"] == "finished"
    sizes = [rr.request_size(r) for r in reader.requests]
    assert len(sizes) == 2 * k + 1
    ceiling = 80_000
    assert max(sizes) < ceiling, max(sizes)
    late, early = sizes[-10:], sizes[6:16]
    assert max(late) < 1.6 * max(early)              # near flat: the summary grows by a line per source/proposal
    for req in reader.requests[1:]:                  # the API pairing holds in every request
        calls = [i["call_id"] for i in req["input"] if i.get("type") == "function_call"]
        outs = [i["call_id"] for i in req["input"] if i.get("type") == "function_call_output"]
        assert calls == outs


# ---------------------------------------------------------------- LIMITS-003 (CVA): 600 s research read timeout


def _timeout_reply():
    return rr.ResponsesReply(model=rr.MODEL, timed_out=True, error="The Muse Spark service did not answer in time.",
                             may_have_been_billed=True)


def test_research_calls_wait_ten_minutes_and_shared_timeouts_are_unchanged():
    from capture import http
    t = rr.research_timeout()
    assert rr.RESEARCH_READ_TIMEOUT_S == 600 and t.read == 600 and t.connect == 20
    assert http.READ_TIMEOUT_S == 120 and http.CONNECT_TIMEOUT_S == 20


def test_a_timeout_is_charged_its_worst_case_and_the_next_turn_goes_on(runs_dir):
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
                              _timeout_reply(),
                              F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "x"}, "c1")]),
                              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c2")])])
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    c = meta["research"]["companies"]["PTGX"]
    assert c["status"] == "finished" and c["requests"] == 4
    step = next(s for s in meta["research"]["steps"] if s["kind"] == "timeout")
    worst = step["refs"]["worst_case_usd"]
    assert worst > 0 and meta["spend_usd"] >= worst
    assert reader.requests[2]["input"] == reader.requests[1]["input"]      # same replay, a new request


def test_two_timeouts_in_a_row_stop_the_company_as_a_technical_failure(runs_dir):
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
                              _timeout_reply(), _timeout_reply()])
    _, rid, _ = start(runs_dir, reader)
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert c["status"] == "failed" and "did not answer twice in a row" in c["status_reason"]


# ---------------------------------------------------------------- Manus 17:07 item 1: delivery before retirement


def test_a_reread_of_a_settled_source_reaches_the_model_before_it_is_compacted(runs_dir):
    ev = F.event("ICARUS", QUOTE, date_text="Q2 2027")
    reader = F.FakeResponses([
        F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")]),
        F.body([F.call("propose_events", {"events": [ev]}, "c1")]),             # settles src-r1-ptgx
        F.body([F.call("read_source_pages", {"source_id": "src-r1-ptgx", "pages": [1], "purpose": "reread"},
                       "c2")]),
        F.body([F.call("record_coverage", {"ticker": "PTGX", "category": "pipeline", "question": "q",
                                           "status": "checked_found", "tool_call_ids": ["c2"],
                                           "source_ids": ["src-r1-ptgx"], "programs": [], "finding": "f"}, "c3")]),
        F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": "e1", "disposition": "proposal", "related_row_id": None, "reason": "x"}],
            "limitations": []}, "c4")])])
    start(runs_dir, reader)
    nxt = reader.requests[3]["input"]                      # the request right after the reread
    out = next(json.loads(i["output"]) for i in nxt if i.get("call_id") == "c2" and "output" in i)
    assert out["data"]["text"] and any("ICARUS" in l for l in out["data"]["text"][0]["lines"])
    assert not out["data"].get("compacted")
    later = reader.requests[4]["input"]                     # one turn later it is retired to a stub
    out2 = next(json.loads(i["output"]) for i in later if i.get("call_id") == "c2" and "output" in i)
    assert out2["data"]["compacted"] and out2["data"]["pages_read"] == [1]


# ---------------------------------------------------------------- Manus 17:07 item 2: unfinished responses


@pytest.mark.parametrize("status", [None, "in_progress", "queued", "cancelled", "failed"])
def test_an_unfinished_response_is_a_typed_failure_not_an_answer(status):
    b = F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")])
    if status is None:
        b.pop("status")
    else:
        b["status"] = status
    reply = rr.parse_response(b)
    assert reply.error and reply.may_have_been_billed and reply.searches is None and not reply.function_calls()


def test_a_finish_beside_an_unfinished_search_is_not_accepted_and_the_status_is_recorded(runs_dir):
    still = dict(F.search("ptgx pdufa", [PR1]), status="searching")
    reader = F.FakeResponses([
        F.body([still, F.call("finish_draft", {"dispositions": [], "limitations": []}, "f1")]),
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "f2")])])
    _, rid, _ = start(runs_dir, reader)
    r = run_json(runs_dir, rid)["run"]["research"]
    assert any(x["issues"][0]["code"] == "unfinished_search" for x in r["refusals"])
    assert r["companies"]["PTGX"]["finish"]["call_id"] == "f2"           # finished only on the later, whole answer
    search = next(s for s in r["steps"] if s["kind"] == "search")
    assert search["refs"]["status"] == "searching" and "did not complete" in search["text"]


# ---------------------------------------------------------------- Manus 17:07 item 5: settlement totality


def test_an_absurd_usage_count_is_unpriced_never_an_overflow():
    assert prices.cost_usd("Muse Spark", 10 ** 400, 10) is None
    assert prices.responses_cost_usd("Muse Spark", 10, 10, 10 ** 400) is None


def test_absurd_usage_releases_the_reservation_and_stops_paid_work(runs_dir):
    reader = F.FakeResponses([F.body([F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c0")],
                                     u=F.usage(i=10 ** 400))])
    _, rid, _ = start(runs_dir, reader)
    meta = run_json(runs_dir, rid)["run"]
    assert meta["unpriced_calls"] == 1 and not jobs._RESERVED.get(rid)
    assert meta["research"]["companies"]["PTGX"]["limitation"]["kind"] == "unpriced"


def test_a_search_only_overrun_is_recorded_in_dollars(runs_dir):
    many = [F.search(f"q{i}", [PR1], sid=f"ws{i}") for i in range(200)]     # far more searches than the 5 assumed
    reader = F.FakeResponses([F.body(many + [F.call("finish_draft", {"dispositions": [], "limitations": []}, "f")],
                                     u=F.usage(i=10, o=10))])
    _, rid, _ = start(runs_dir, reader)
    over = run_json(runs_dir, rid)["run"]["research"]["reservation_exceeded"]
    assert over and over[0]["searches"] == 200 and over[0]["cost_usd"] > over[0]["reserved_usd"]


# ---------------------------------------------------------------- Manus 17:07 item 3: one material identity


@pytest.mark.parametrize("mutate", ["secondary_derived_words", "secondary_published_date"])
def test_keep_checks_and_report_expire_together_when_secondary_timing_support_changes(runs_dir, mutate):
    from runs.state import current_state
    from report.data import build_view
    box = {}
    _two_source_timing_run(runs_dir, program="ICARUS", client_out=box)
    client, rid = box["client"], box["rid"]
    assert client.post(f"/api/runs/{rid}/decisions", json={"target": "e2", "action": "keep", "note": None,
                                                           "fix": None}).status_code == 200
    run, decisions = run_json(runs_dir, rid), _decisions(runs_dir, rid)
    st = current_state(run, decisions)["targets"]["e2"]
    assert st["accepted"] and not st["checks_stale"]
    second = next(s for s in run["sources"] if s["id"] == "src-r2-ptgx")
    if mutate == "secondary_derived_words":
        second["pages"][0]["derived_words"] = {"kind": "model_transcription", "file": "pages/x.json", "sha256": "2" * 64,
                                               "based_on_image_sha256": second["pages"][0]["image_sha256"],
                                               "geometry": "approximate", "models": ["m"]}
    else:
        second["published_date"] = "2025-01-01"
    st = current_state(run, decisions)["targets"]["e2"]
    assert st["checks_stale"] is True and st["accepted"] is False and st["review_history"]
    view = build_view(run, decisions, generated_at="2026-11-02T00:00:00+00:00")
    assert "e2" not in {r["id"] for key in ("calendar", "past", "later", "contingent", "undated") for r in view[key]}
    undone = decisions + [{"target": "e2", "action": "undo", "by": "person", "at": "2026-11-02T00:00:01+00:00",
                           "note": None, "fix": None}]
    st = current_state(run, undone)["targets"]["e2"]
    assert st["accepted"] is False and st["semantic_status"] == "unresolved"


def test_class_hidden_text_in_a_text_capture_is_recorded_as_visibility_unknown(tmp_path):
    page = (b"<html><head><style>.gone{display:none}</style></head><body><p>Shown text in Q2 2027.</p>"
            b"<p class='gone'>Hidden claim: approval in Q1 2027.</p></body></html>")

    def broken(html, pdf):
        raise rs.CaptureFailed("Chrome exited with code 1 after 0.2s and wrote no usable PDF.")
    got = rs.capture_document(rs.Fetched("https://example.org/h", "https://example.org/h", 200, "text/html", page),
                              tmp_path, "src-r1-x", render_html=broken)
    assert got["rendered"]["form"] == "text" and got["rendered"]["visibility"] == "unknown"
    assert any(l.startswith("Visibility unknown") for l in got["rendered"]["limitations"])



# ---------------------------------------------------------------- BRAVE-PRIMARY-001 (CVA): Brave is the primary search


def _brave_run(runs_dir, brave, extra=None, limit=None):
    script = [F.body([F.call("brave_search", {"query": "Protagonist PDUFA 2027", "count": 5}, "b1")]),
              F.body([F.call("fetch_url", {"url": PR1, "purpose": "Brave lead"}, "c1")]),
              F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "x"}, "c2")]),
              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])]
    reader = F.FakeResponses(extra or script)
    tools = F.tools({PR1: (200, "text/html", PAGE1)})
    tools.brave = brave
    app = create_app(runs_dir=runs_dir, reader=reader, background=False, second_reader=None, wonder_reader=None,
                     sam_reader=None, research_tools=tools, research_reader=reader)
    body = {"mode": "research", "companies": ["PTGX"], "as_of": "2026-11-02"}
    if limit is not None:
        body["spending_limit_usd"] = limit
    rid = TestClient(app).post("/api/runs/new", json=body).json()["id"]
    return reader, rid


def test_brave_is_offered_first_used_and_its_leads_reach_the_model(runs_dir):
    brave = F.FakeBrave({"Protagonist PDUFA 2027": [PR1, PR2]})
    reader, rid = _brave_run(runs_dir, brave)
    first = reader.requests[0]
    names = [t.get("name") for t in first["tools"]]
    assert "brave_search" in names and "brave_search (primary)" in json.dumps(first["input"])
    assert "use brave_search first for discovery" in rc.CONTROLLER
    assert brave.queries == ["Protagonist PDUFA 2027"]
    out = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out["data"]["results"][0]["title"] == f"TITLE-{PR1}"          # the model sees titles for its turn
    run = run_json(runs_dir, rid)
    step = next(s for s in run["run"]["research"]["steps"] if s["kind"] == "search")
    assert step["refs"]["engine"] == "Brave Search" and step["refs"]["results"] == [PR1, PR2]
    assert run["run"]["research"]["companies"]["PTGX"]["status"] == "finished"


def test_brave_titles_and_snippets_are_never_stored(runs_dir):
    brave = F.FakeBrave({"Protagonist PDUFA 2027": [PR1, PR2]})
    reader, rid = _brave_run(runs_dir, brave)
    stored = []
    for f in (runs_dir / rid).rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl"):
            stored.append(f.read_text(errors="replace"))
    blob = "\n".join(stored)
    assert "TITLE-" not in blob and "SNIPPET-" not in blob
    assert PR1 in blob and "Protagonist PDUFA 2027" in blob                 # query and URLs are kept
    later = json.dumps(reader.requests[2]["input"])                          # retired after its turn
    assert "SNIPPET-" not in later and PR1 in later


def test_brave_is_priced_per_request_in_the_same_ledger(runs_dir):
    assert prices.PRICES_PER_1K_REQUESTS_USD["Brave Search"] == 5.00
    assert prices.request_cost_usd("Brave Search", 1) == 0.005
    reader, rid = _brave_run(runs_dir, F.FakeBrave({"Protagonist PDUFA 2027": [PR1]}))
    meta = run_json(runs_dir, rid)["run"]
    model = 4 * prices.cost_usd("Muse Spark", 1000, 200)
    assert meta["spend_usd"] == pytest.approx(model + 0.005, abs=1e-6)
    assert meta["research"]["companies"]["PTGX"]["brave_requests"] == 1
    assert meta["prices"]["per_1k_requests_usd"] == {"Brave Search": 5.0}


def test_a_brave_429_is_a_recorded_limitation_without_retry(runs_dir):
    brave = F.FakeBrave(rate_limited=True)
    script = [F.body([F.call("brave_search", {"query": "q", "count": None}, "b1")]),
              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])]
    reader, rid = _brave_run(runs_dir, brave, extra=script)
    assert brave.queries == ["q"]                                           # one attempt
    out = json.loads(reader.requests[1]["input"][-1]["output"])
    assert out["issues"][0]["code"] == "rate_limited" and "web_search" in out["issues"][0]["message"]
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert c["search_limitations"][0]["reason"].endswith("(HTTP 429).") and c["status"] == "finished"


def test_without_a_brave_key_the_tool_is_unavailable_recorded_and_research_goes_on(runs_dir):
    script = [F.body([F.search("ptgx", [PR1]), F.call("fetch_url", {"url": PR1, "purpose": "x"}, "c1")]),
              F.body([F.call("nothing_material", {"source_id": "src-r1-ptgx", "reason": "x"}, "c2")]),
              F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "c3")])]
    reader, rid = _brave_run(runs_dir, F.FakeBrave(key=False), extra=script)
    assert "brave_search" not in [t.get("name") for t in reader.requests[0]["tools"]]
    assert "brave_search is unavailable" in json.dumps(reader.requests[0]["input"])
    c = run_json(runs_dir, rid)["run"]["research"]["companies"]["PTGX"]
    assert "no BRAVE_API_KEY" in c["search_limitations"][0]["reason"] and c["status"] == "finished"


def test_a_brave_request_is_admitted_through_the_strict_cap(runs_dir):
    run = jobs.create_run(runs_dir, jobs.validate_choices({"mode": "research", "companies": ["PTGX"],
                                                           "spending_limit_usd": 1.0}), as_of="2026-11-02")
    path = runs_dir / run["run"]["id"] / "run.json"
    doc = json.loads(path.read_text())
    doc["run"]["spend_usd"] = 0.998
    path.write_text(json.dumps(doc))
    assert jobs.reserve_budget("brave-cap", 0.998, 1.0, 0.005, alone_may_cross=False) is None
