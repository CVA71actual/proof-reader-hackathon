"""PIN-25: behaviour tests for historical rules (R-ids) that had no pinning test on main 2f031e8.

Each test pins the rule's CURRENT behaviour on main as the rules ledger (RULES_PINNED.md, 2026-10-31) records it.
Tests drive the real functions: Python code directly, ui/app.js functions copied out and run under node (no
source-string greps). No model is called; server tests use the FakeReader and a tmp copy of runs/sample.
Rules in scope that were skipped (already covered on main, being replaced by PR #137, or documentation-only) are
listed in the PR body, not here.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from capture import read_events, read_table, research_instructions, sam
from capture.capture_source import load_entry
from checks import date_fixture_test as fx
from checks.event_checks import COULD_NOT_RUN, add_months_to, date_match, parse_exact_date
from runs import build_entry, escalate, scan
from runs.calendar import run_interval_partition
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import PTGX_ONLY, FakeReader

RUNS = REPO / "runs"
UI = REPO / "ui"
PIN = read_events.PIN
SOURCE = {"id": "s", "kind": "filing", "published_date": "2026-08-05", "pages": [{"page_number": 3}]}
AT = "2026-10-31T00:00:00+00:00"


def _sample():
    run = json.loads((RUNS / "sample" / "run.json").read_text())
    decisions = [json.loads(line) for line in (RUNS / "sample" / "decisions.jsonl").read_text().splitlines()
                 if line.strip()]
    return run, decisions


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    shutil.copytree(RUNS / "sample", d / "sample")
    return d


def _client(runs_dir, reader=None):
    reader = reader or FakeReader()
    app = create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False)
    return TestClient(app), reader


# ---------------------------------------------------------------- node harness for ui/app.js


def _grab_fn(js: str, name: str) -> str:
    at = js.index(f"function {name}(")
    i = js.index("{", at)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[at: j + 1]
    raise AssertionError(f"{name} does not close")


DOM = """
function make(tag) {
  return { tag: tag, className: '', textContent: '', children: [], style: {}, hidden: false, open: false,
    append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    insertBefore: function (n) { this.children.unshift(n); },
    addEventListener: function () {}, querySelector: function () { return null; } };
}
var nodes = {};
global.document = {
  createElement: function (tag) { return make(tag); },
  createTextNode: function (t) { var n = make('#text'); n.textContent = t; return n; },
  getElementById: function (id) { if (!nodes[id]) nodes[id] = make('div'); return nodes[id]; }
};
function texts(node, out) {
  out = out || [];
  if (node && node.textContent) out.push(node.textContent);
  ((node && node.children) || []).forEach(function (c) { texts(c, out); });
  return out;
}
function clear(node) { node.textContent = ''; node.children = []; return node; }
"""


def _node(fn_names, body: str) -> dict:
    js = (UI / "app.js").read_text()
    script = DOM + "\n".join(_grab_fn(js, n) for n in fn_names) + "\n" + body
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr or got.stdout
    return json.loads(got.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- checks / dates


def test_r12_parse_exact_date_returns_none_for_an_impossible_date():
    assert parse_exact_date("March 12, 2027") == date(2027, 3, 12)        # the valid forms still parse
    assert parse_exact_date("February 30, 2027") is None
    assert parse_exact_date("2027-02-30") is None
    assert parse_exact_date("31 April 2027") is None


def test_r23_date_match_with_no_date_text_could_not_run():
    quote = "Topline data are expected in the first quarter of 2027."
    for text in (None, ""):
        r = date_match(text, "2027-01-01", quote)
        assert r.result == COULD_NOT_RUN
        assert r.reason == "The reader gave no date text for this row."


def test_r72_window_end_clamps_to_the_last_day_of_the_month():
    assert add_months_to(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert add_months_to(date(2027, 8, 31), 6) == date(2028, 2, 29)
    run = {"run": {"started_at": "2026-08-31T15:00:00+00:00"}, "rows": []}
    _, anchor, end = run_interval_partition(run, {"targets": {}})
    assert (anchor, end) == ("2026-08-31", "2027-02-28")


def test_r92_date_fixture_test_passes_on_the_committed_fixture_and_fails_a_tampered_copy(tmp_path, monkeypatch,
                                                                                          capsys):
    assert all(ok for ok, _ in fx.run())
    assert fx.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("PASS: ") and "FAIL" not in "\n".join(out[:-1])

    lines = fx.FIXTURE.read_text(encoding="utf-8").splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.strip() and not ln.startswith("#"))
    cols = lines[first].split("\t")
    cols[1] = "period" if cols[1] != "period" else "day"
    lines[first] = "\t".join(cols)
    tampered = tmp_path / "date_phrases.tsv"
    tampered.write_text("\n".join(lines) + "\n", encoding="utf-8")
    real_load = fx.load
    monkeypatch.setattr(fx, "load", lambda path=tampered: real_load(path))
    assert fx.main() == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("FAIL: ")
    assert any(ln.startswith("FAIL  " + repr(cols[0])) for ln in out)


# ---------------------------------------------------------------- prompts


def test_r32_second_reader_scan_and_wonder_prompts_are_not_narrowed_by_type_choices():
    entry = load_entry(PIN)
    words_doc = json.loads((FIXTURES / f"{PIN}.words.json").read_text())
    trig = {"kind": "check", "label": "Date read as written", "why": "The period differs."}
    narrow = ("filing",)
    brief = research_instructions.DEFAULT
    pairs = {
        # the second reader builds its prompts with read_events' builders and their default (all) types
        "second reader, words": (read_events.build_prompt(words_doc, entry, narrow, brief),
                                 read_events.build_prompt(words_doc, entry, note=brief)),
        "second reader, image": (read_events.build_image_prompt(entry, narrow, brief),
                                  read_events.build_image_prompt(entry, note=brief)),
        "scan region": (scan.region_prompt(entry, {"concept": sam.FALLBACK}, narrow, brief),
                        scan.region_prompt(entry, {"concept": sam.FALLBACK}, note=brief)),
        "wonder": (escalate.prompt(entry, trig, (100.0, 200.0), words_doc, narrow),
                   escalate.prompt(entry, trig, (100.0, 200.0), words_doc)),
    }
    for name, (narrowed, broad) in pairs.items():
        assert narrowed == broad, name
        if name == "wonder":
            # ESCALATION-PATCH-001: repair a requested field, not rediscover events.
            assert "Repair only the date fields" in narrowed
            assert "Do not rewrite the event" in narrowed
        else:
            assert brief in narrowed, name
            assert "undated plans and conditional events" in narrowed, name
            assert "source's meaning, timing and qualifications" in narrowed, name
            assert "Routine financial figures alone are not calendar events." in narrowed, name
    assert read_table.build_image_prompt() == read_table.build_image_prompt(None)


# ---------------------------------------------------------------- report


def test_r82_report_value_and_date_display_honour_a_null_in_the_state():
    from report.data import _date_display, _value

    raw = {"date": "2027-03-12", "date_text": "March 12, 2027", "date_end": None}
    cleared = {"date": None, "date_text": None, "date_end": None}       # a fix cleared the day
    for field in ("date", "date_text", "date_end"):
        assert _value(cleared, raw, field) is None
    assert _value({}, raw, "date") == "2027-03-12"                      # absent from the state: the raw row
    assert _date_display(cleared) == "—"
    assert _date_display({"date": "2027-01-01", "date_end": "2027-03-31", "date_text": None}) != "2027-01-01"


def _report_texts(as_of: str):
    from docx import Document
    from pypdf import PdfReader

    from report.data import build_view
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf

    run, decisions = _sample()
    run = copy.deepcopy(run)
    run["run"]["as_of"] = as_of
    from tests.pivot_assertions import fresh_keep
    # Explicit new Keeps in the disposable report fixture, after the as-of change.
    for row in run["rows"]:
        if row["id"] not in {"e5", "e6"}:
            decisions.append(fresh_keep(run, decisions, row["id"]))
    view = build_view(run, decisions, generated_at="2026-10-31T00:00:00Z")
    pdf = "".join("".join((p.extract_text() or "").split()) for p in PdfReader(BytesIO(build_pdf(view))).pages)
    docx = [p.text for p in Document(BytesIO(build_docx(view))).paragraphs]
    return view, pdf, docx


def _evidence_line(docx: list[str], rid: str) -> str:
    hits = [p for p in docx if p.startswith(f"Evidence {rid}:")]
    assert len(hits) == 1, (rid, hits)
    return hits[0]


def test_r112_calendar_evidence_line_carries_source_id_and_source_document():
    view, pdf, docx = _report_texts("2027-01-10")
    assert view["calendar"]
    for row in view["calendar"]:
        line = _evidence_line(docx, row["id"])
        assert f"{row['source_document']} ({row['source_id']}) · page {row['page_number']}" in line
        assert "".join(f"Evidence {row['id']}: {row['source_document']} ({row['source_id']})".split()) in pdf


def test_r122_past_and_later_rows_each_print_an_evidence_line_with_source_page_and_quote():
    view, pdf, docx = _report_texts("2027-01-10")
    assert view["past"] and view["later"]
    for row in view["past"] + view["later"]:
        line = _evidence_line(docx, row["id"])
        assert f"({row['source_id']}) · page {row['page_number']}" in line
        assert row["source_document"] in line
        assert row["quote"] and f"quote: {row['quote']}" in line
        assert "".join(f"Evidence {row['id']}: {row['source_document']} ({row['source_id']})".split()) in pdf


# ---------------------------------------------------------------- escalation


def test_r106_a_disagreement_escalation_records_evidence_and_replaces_nothing():
    words_doc = json.loads((FIXTURES / f"{PIN}.words.json").read_text())
    reading = json.loads((FIXTURES / f"{PIN}.events.json").read_text())
    event = next(e for e in reading["events"] if e.get("date"))
    reading = {"prompt_text": "p", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "fake": False}
    row = build_entry.build_row(1, event, reading, SOURCE, words_doc, AT)
    row["disagreement"] = {"field": "date", "first_reader": row["date"], "second_reader": "1999-01-01"}
    before = copy.deepcopy(row)
    trig = escalate.trigger(row)
    assert trig["kind"] == "disagreement"
    run = {"mistakes": []}

    got = escalate.apply(row, event, reading, SOURCE, words_doc, AT, trig, run, (None, None, 1), "prompt")

    assert got == "evidence"
    for f in escalate.FIELDS:
        assert row[f] == before[f], f
    assert (row["status"], row["group"], row["checks"]) == (before["status"], before["group"], before["checks"])
    assert run["mistakes"] == []
    last = row["trail"][-1]
    assert last["step"] == "Escalation result"
    assert "row stays as it was" in last["text"]
    assert "person must review" in last["text"]
    assert "No mistake is counted" in last["text"]


# ---------------------------------------------------------------- server


class NoCallReader(FakeReader):
    def call(self, prompt, image=None):
        raise AssertionError("a model was called while serving a saved run")


def test_r90_get_replays_decisions_without_a_model_call_and_a_keep_does_not_pass_a_check(runs_dir):
    client, reader = _client(runs_dir, NoCallReader())
    got = client.get("/api/runs/sample")
    assert got.status_code == 200, got.text
    assert reader.prompts == []
    e7 = got.json()["state"]["targets"]["e7"]
    assert (e7["group"], e7["decided_by_person"], e7["last_action"]) == ("kept", True, "keep")
    assert e7["status"] == "failed"
    results = {(c.get("name") or c.get("check") or c.get("id")): c["result"] for c in e7["effective_checks"]}
    assert results["quote_match"] == "fail" and results["selector_match"] == "fail"


def test_r121_a_decision_whose_report_rebuild_fails_marks_the_report_stale_and_unserved(runs_dir, monkeypatch):
    from report import build as report_build

    client, _ = _client(runs_dir)
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["PTGX"]})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    run_dir = runs_dir / rid
    built = report_build.build(rid, run_dir=run_dir, generated_at="2026-10-31T12:00:00Z")
    name = built["pdf"].name
    assert client.get(f"/runs/{rid}/report/{name}").status_code == 200

    def broken(*a, **k):
        raise RuntimeError("builder down")

    monkeypatch.setattr(report_build, "build", broken)
    resp = client.post(f"/api/runs/{rid}/decisions", json={"target": "e1", "action": "keep"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["report_refresh"].startswith("could not rebuild, moved to stale-")
    saved = json.loads((run_dir / "run.json").read_text())
    assert saved["report_stale"] is True and "builder down" in saved["report_stale_reason"]
    assert not (run_dir / "report" / name).exists()
    assert any((d / name).is_file() for d in (run_dir / "report").glob("stale-*"))
    assert client.get(f"/runs/{rid}/report/{name}").status_code == 404
    decisions = (run_dir / "decisions.jsonl").read_text().splitlines()
    assert json.loads(decisions[-1])["target"] == "e1"     # the decision itself was written


# ---------------------------------------------------------------- ui (node)


def test_r71_cal_window_end_clamps_to_month_end():
    body = """
var detail = { run: { calendar_window: { start: '2026-08-31' } } };
var a = calWindow();
detail.run.calendar_window = { start: '2027-08-31' };
var b = calWindow();
detail.run.calendar_window = { start: '2026-10-13' };
var c = calWindow();
detail.run.calendar_window = { unresolved: true, reason: 'no date' };
var d = calWindow();
console.log(JSON.stringify({ a: a, b: b, c: c, d: d }));
"""
    out = _node(["calWindow"], body)
    assert (out["a"]["anchor"], out["a"]["end"]) == ("2026-08-31", "2027-02-28")
    assert out["b"]["end"] == "2028-02-29"
    assert out["c"]["end"] == "2027-04-13"
    assert out["d"]["anchor"] is None and out["d"]["end"] is None and out["d"]["unresolved"] is True


CAL_PRELUDE = """
var route = { rowId: null }, calRouted = null, calSel = null, calSrcSel = null;
function calendarRows() { return []; }          // no kept/corrected rows in any bucket
function calRow(r, dayCell, isPeriod) { var n = make('button'); n.textContent = 'ROW ' + r.row.id + ' | ' + dayCell;
  return n; }
function revealRow() {}
function rowTarget(row) { return detail.state.targets[row.id] || {}; }
function rowOf(id) { return null; }
"""


def _calendar(rows, exclusions, targets):
    detail = {"id": "run", "run": {"run": {"label": "probe"}, "rows": rows, "exclusions": exclusions,
                                   "calendar_window": {"start": "2026-10-13"}, "sources": []},
              "state": {"targets": targets}}
    body = CAL_PRELUDE + f"var detail = {json.dumps(detail)};\n" + """
function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text; return e; }
renderCalendar();
console.log(JSON.stringify({ mid: texts(nodes['cal-mid']), det: texts(nodes['cal-det']) }));
"""
    return _node(["projectionComplete", "partialProjection", "shownField", "shownDateText", "calWindow", "calFoldHit", "renderCalendar"], body)


def test_r74_latest_past_event_is_the_latest_raw_end_or_start_per_ticker():
    rows = [
        {"id": "x1", "ticker": "AAA", "date": "2026-01-01", "date_end": "2026-03-31", "date_text": "Q1 2026"},
        # a later raw start, but a person's fix in the state moved it to December: the state value is not read
        {"id": "x2", "ticker": "AAA", "date": "2026-02-01", "date_end": None, "date_text": "February 1, 2026"},
        {"id": "y1", "ticker": "BBB", "date": "2025-05-01", "date_end": None, "date_text": "May 1, 2025"},
    ]
    exclusions = [{"target": t, "rule": "event_in_past", "by": "code"} for t in ("x1", "x2", "y1")]
    targets = {"x2": {"values": {"date": "2026-12-01"}, "group": "removed"}}
    out = _calendar(rows, exclusions, targets)
    mid = out["mid"]
    assert mid.count("Latest past event · AAA") == 1 and mid.count("Latest past event · BBB") == 1
    shown = [t for t in mid if t.startswith("ROW ")]
    assert "ROW x1 | Q1 2026" in shown and "ROW y1 | May 1, 2025" in shown
    assert not any(t.startswith("ROW x2") for t in shown)


def test_r75_an_otherwise_empty_calendar_still_renders_the_latest_past_event():
    rows = [{"id": "x1", "ticker": "AAA", "date": "2026-01-01", "date_end": None, "date_text": "January 1, 2026"}]
    out = _calendar(rows, [{"target": "x1", "rule": "event_in_past", "by": "code"}], {})
    assert "Latest past event · AAA" in out["mid"]
    assert not any(t.startswith("Nothing on the calendar yet") for t in out["mid"])
    # and with no past exclusion at all the empty sentence is what shows
    empty = _calendar(rows, [], {})
    assert any(t.startswith("Nothing on the calendar yet") for t in empty["mid"])


def test_r95_render_source_groups_pages_by_accession_under_the_first_title():
    acc_a, acc_b = "0001234567-26-000111", "0001234567-26-000222"
    sources = [
        {"id": f"src-aaa-{acc_a}-ex99-1", "title": "AAA 8-K exhibit 99.1 (exhibit 99.1), filed 2026-10-23",
         "published_date": "2026-10-23", "pages": [{"page_number": 1}, {"page_number": 2}]},
        {"id": f"src-aaa-{acc_a}-ex99-2", "title": "AAA 8-K exhibit 99.2 (exhibit 99.2), filed 2026-10-23",
         "published_date": "2026-10-23", "pages": [{"page_number": 1}]},
        {"id": f"src-aaa-{acc_b}-ex99-1", "title": "AAA 10-Q, filed 2026-11-05",
         "published_date": "2026-11-05", "pages": [{"page_number": 1}, {"page_number": 2}]},
    ]
    detail = {"id": "run", "run": {"rows": [], "sources": sources}, "state": {"targets": {},
        "page_outcomes": {"items": [
            {"source_id": sources[0]["id"], "page_number": 1, "read": True},
            {"source_id": sources[0]["id"], "page_number": 2, "unreadable": True},
            {"source_id": sources[1]["id"], "page_number": 1, "skipped": True},
            {"source_id": sources[2]["id"], "page_number": 1},
        ]}}}
    body = f"var detail = {json.dumps(detail)};\n" + """
var MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
var route = { srcId: null, page: null, valueId: null, back: null };
function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text; return e; }
function currentRunId() { return detail.id; }
function navigate() {}
function revealRow() {}
renderSource();
var list = nodes['srcList'].children.map(function (f) { return { summary: f.children[0].textContent,
  pages: f.children.slice(1).map(function (b) { return texts(b).join(' '); }) }; });
console.log(JSON.stringify({ list: list, sub: nodes['src-sub'].textContent }));
"""
    out = _node(["dateShort", "btn", "renderSource"], body)
    assert out["sub"] == "Every page the rows were read from · 5 pages"
    assert out["list"] == [
        {"summary": "AAA 8-K exhibit 99.1 · filed 23 Oct 2026 · 3 pages",
         "pages": ["page 1 read", "page 2 unreadable", "page 1 skipped"]},
        {"summary": "AAA 10-Q · filed 5 Nov 2026 · 2 pages",
         "pages": ["page 1 not read", "page 2 outcome unavailable"]},
    ]


@pytest.mark.xfail(strict=True, reason="REC-31 / R96: the counted second attribution disappears from its filter")
def test_r96_counted_second_mistake_remains_in_its_filter():
    from runs.state import current_state

    run, decisions = _sample()
    first = run["mistakes"][0]
    other = "person" if first["caught_by"] != "person" else "code"
    run["mistakes"].append({**first, "id": "m99", "caught_by": other})
    st = current_state(run, decisions)
    on_target = [m for m in st["mistake_list"] if m["target"] == first["target"]]
    assert [m["caught_by"] for m in on_target][:1] == [first["caught_by"]] and len(on_target) >= 2
    base = current_state(_sample()[0], decisions)
    assert st["mistakes"][other] == base["mistakes"][other] + 1          # the counter counts the second one

    filt = {"code": "MCODE", "second_reader": "MREADER", "person": "MYOU"}
    detail = {"run": run, "state": st}
    body = f"var detail = {json.dumps(detail)};\n" + f"""
var audit = {{ filter: 'ALL' }};
function exclusionFor() {{ return null; }}
function needsAttention() {{ return false; }}
function rowTarget(row) {{ return detail.state.targets[row.id] || {{}}; }}
var got = mistakeFor({json.dumps(first['target'])});
audit.filter = {json.dumps(filt[first['caught_by']])};
var firstIds = visibleAuditRows().map(function (r) {{ return r.id; }});
audit.filter = {json.dumps(filt[other])};
var otherIds = visibleAuditRows().map(function (r) {{ return r.id; }});
console.log(JSON.stringify({{ got: got, firstIds: firstIds, otherIds: otherIds }}));
"""
    out = _node(["mistakeFor", "visibleAuditRows"], body)
    assert out["got"]["caught_by"] == first["caught_by"]
    assert first["target"] in out["firstIds"]
    assert first["target"] in out["otherIds"]            # every counted attribution must remain filterable


@pytest.mark.xfail(strict=True, reason="REC-09 / R74: past context reads superseded raw dates")
def test_r74_current_future_date_does_not_remain_past_context():
    row = {"id": "x1", "ticker": "AAA", "date": "2026-01-01", "date_end": None,
           "date_text": "January 1, 2026"}
    targets = {"x1": {"values": {**row, "date": "2027-01-01", "date_text": "January 1, 2027"},
                      "group": "kept", "on_calendar": True, "decided_by_person": True}}
    out = _calendar([row], [{"target": "x1", "rule": "event_in_past", "by": "code"}], targets)
    assert "Latest past event · AAA" not in out["mid"]
