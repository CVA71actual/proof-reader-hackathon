"""PIN-25: historical rules on the review screen (ui/app.js), pinned by running the real functions under node.

Each test is named after its rule id in the 123-rule catalogue. The functions are copied out of ui/app.js by exact
name and run under node with a small stand-in for the page's elements; nothing here greps app.js
for a string. Server-side state comes from runs.state.current_state on a tmp copy of runs/sample. No model calls.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from runs.state import current_state

REPO = Path(__file__).resolve().parent.parent
JS = (REPO / "ui" / "app.js").read_text()

# A stand-in for the page: el() nodes that hold children, getElementById, and a text dump of a node tree.
DOM = r"""
function el(tag, cls, text) {
  return { tag: tag, className: cls || '', textContent: text || '', children: [], style: {}, title: '',
    hidden: false, open: false, href: '',
    append: function () {
      for (var i = 0; i < arguments.length; i++) {
        var c = arguments[i];
        this.children.push(typeof c === 'string' ? { textContent: c, children: [] } : c);
      }
    },
    querySelector: function () { return null; },
    addEventListener: function () {} };
}
function clear(node) { node.textContent = ''; node.children = []; return node; }
function textOf(node) {
  var out = [];
  (function walk(n) { if (n.textContent) out.push(n.textContent); (n.children || []).forEach(walk); })(node);
  return out.join('\n');
}
var NODES = {};
var document = { getElementById: function (id) { return NODES[id] || (NODES[id] = el('div')); } };
function revealRow() {}
"""


def _node(*parts: str) -> str:
    got = subprocess.run(["node", "-"], input="\n".join(parts), text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    return got.stdout


def _grab(name: str) -> str:
    """The function `name` (exact name, not a prefix) from app.js, braces balanced."""
    at = JS.index(f"function {name}(")
    depth, i = 0, JS.index("{", at)
    for j in range(i, len(JS)):
        depth += {"{": 1, "}": -1}.get(JS[j], 0)
        if depth == 0:
            return JS[at:j + 1]
    raise AssertionError(f"{name} does not close")


def _fns(*names: str) -> str:
    return "\n".join(_grab(n) for n in names)


@pytest.fixture
def sample(tmp_path):
    d = tmp_path / "sample"
    shutil.copytree(REPO / "runs" / "sample", d)
    run = json.loads((d / "run.json").read_text())
    decisions = [json.loads(line) for line in (d / "decisions.jsonl").read_text().splitlines() if line.strip()]
    return run, decisions


# ---------------------------------------------------------------- calendar


def _calendar_script(rows, targets, partition=None, exclusions=None, window=None, tail=""):
    return [
        DOM,
        "var detail = { run: { rows: %s, calendar_partition: %s, exclusions: %s, calendar_window: %s, run: {} },"
        " state: { targets: %s } };" % (json.dumps(rows), json.dumps(partition), json.dumps(exclusions or []),
                                       json.dumps(window or {"start": "2026-10-01"}), json.dumps(targets)),
        "var route = { rowId: null }; var calSel = null, calSrcSel = null, calRouted = null;",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        "function rowOf(id) { return detail.run.rows.filter(function (r) { return r.id === id; })[0] || null; }",
        "function calRow(r, written) { return el('div', 'crow', 'ROW ' + r.row.id + ' ' + written); }",
        _fns("shownVal", "effectiveChecks", "projectionComplete", "partialProjection", "calendarRows", "calWindow", "calFoldHit", "renderCalendar"),
        tail,
    ]


def _cal_row(rid, **kw):
    row = {"id": rid, "ticker": "AAA", "company": "Aaa", "program": "PN-1", "indication": "IgAN",
           "event_type": "milestone", "title": "Results", "date": "2026-12-01", "date_end": None,
           "date_text": "December 1, 2026", "quote": "Results on December 1, 2026.",
           "checks": [{"name": "quote_match", "result": "pass"}]}
    row.update(kw)
    return row


def test_r70_calendar_folds_only_rows_identical_in_every_current_value_and_keeps_undated_rows():
    # R70: state placement beats raw dates before folding; dated-but-removed is dropped, an in-window row explicitly
    # placed later stays there, and an explicitly placed undated row is listed. Folding still carries every source.
    rows = [_cal_row("a1"), _cal_row("a2"), _cal_row("a3"),
            _cal_row("b1", date_end="2026-12-31"),           # differs only in date_end
            _cal_row("c1", indication="Other"),              # differs only in indication
            _cal_row("u1", date=None, date_text=None)]       # no day at all
    targets = {r["id"]: {"group": "kept", "status": "matched", "on_calendar": r["id"] != "a1",
                         "values": {k: r.get(k) for k in ("ticker", "company", "program", "indication",
                                                           "event_type", "title", "date_text", "date",
                                                           "date_end", "quote")}}
               for r in rows}
    partition = {"calendar": [{"id": rid} for rid in ("a1", "a2", "a3", "c1")],
                 "later": [{"id": "b1"}],
                 "contingent": [{"id": "u1"}]}
    out = _node(*_calendar_script(rows, targets, partition=partition, tail=(
        "var got = calendarRows();"
        "console.log(JSON.stringify(got.map(function (e) {"
        "  return [e.row.id, e.fold.map(function (f) { return f.row.id; }), e.bucket]; })));")))
    got = json.loads(out.strip().splitlines()[-1])
    assert got == [["a2", ["a2", "a3"], "calendar"], ["b1", ["b1"], "later"],
                   ["c1", ["c1"], "calendar"], ["u1", ["u1"], "contingent"]]


# ---------------------------------------------------------------- export


def _export_text(fetch_js: str, stale: bool) -> str:
    return _node(
        DOM,
        "var detail = { run: { run: { number: 2, label: 'probe', spend_usd: null }, report_stale: %s,"
        " report_stale_reason: null }, state: { cost_per_kept_row_usd: null, counts: { rows: {} }, unresolved: 0,"
        " mistakes: { code: 0, second_reader: 0, person: 0 }, excluded: { code: 0, person: 0 } } };"
        % ("true" if stale else "false"),
        "var GROUP_ORDER = [], GROUP_NAMES = {};",
        "function money(v) { return '$' + v; } function unpricedMark() { return ''; }",
        "function reportFileNames() { return [{ file: 'r.pdf', kind: 'PDF' }, { file: 'r.docx', kind: 'Word' }]; }",
        "function reportUrl(n) { return '/runs/x/report/' + n; }",
        "global.fetch = " + fetch_js + ";",
        _fns("projectionComplete", "partialProjection", "shownDateText", "shownField",
             "reviewAcceptedCount", "reportRefusalReason", "reviewAxis", "oneModelRun", "renderExport"),
        "renderExport();",
        "setTimeout(function () { console.log(textOf(NODES['exp-mid'])); }, 0);",
    )


@pytest.mark.parametrize("fetch_js", [
    "function () { return Promise.resolve({ ok: false, status: 404 }); }",
    "function () { return Promise.resolve({ ok: false, status: 409 }); }",
    "function () { return Promise.reject(new Error('offline')); }",
], ids=["404", "409", "network-error"])
def test_r86_any_failed_report_head_shows_the_same_absence_sentence(fetch_js):
    # R86: renderExport runs a HEAD per saved report file; every failure (404, 409, a network error) gives the one
    # sentence "No report is saved for this run yet." — and a stale run says it is out of date instead.
    out = _export_text(fetch_js, stale=False)
    assert out.count("No report is saved for this run yet.") == 2 and "Open" not in out.split("\n")
    stale = _export_text(fetch_js, stale=True)
    assert stale.count("Saved report is out of date.") == 2
    assert "No report is saved for this run yet." not in stale
    ok = _export_text("function () { return Promise.resolve({ ok: true, status: 200 }); }", stale=False)
    assert "Open" in ok.split("\n") and "No report is saved" not in ok


# ---------------------------------------------------------------- source pages


# ---------------------------------------------------------------- audit chips


# ---------------------------------------------------------------- needs attention


def test_r117_ui_needs_attention_count_equals_the_servers_unresolved_count(sample):
    # R117 (record: the stale predicate is fixed; needsAttention tests needs_recheck): over the sample's state, and
    # over the same state with a fix waiting for its re-check, the rows the screen lists as needing attention are as
    # many as the server's `unresolved`.
    run, decisions = sample
    # FIX-THEN-KEEP-001: first accept e1's current revision. A pending edit then
    # invalidates that acceptance and adds one review item; legacy Keeps stay history.
    decisions = decisions + [{"at": "2026-10-31T09:59:00Z", "target": "e1", "action": "keep",
                              "by": "person", "fix": None, "note": None,
                              "review_basis": current_state(run, decisions)["targets"]["e1"]["review_basis"]}]
    assert current_state(run, decisions)["targets"]["e1"]["accepted"]
    pending = decisions + [{"at": "2026-10-31T10:00:00Z", "target": "e1", "action": "fix", "by": "person",
                            "fix": {"field": "date", "from": "2027-03-12", "to": "2027-03-13"}}]
    for decs in (decisions, pending):
        st = current_state(run, decs)
        rows = [t for rid, t in st["targets"].items() if t["kind"] == "row"]
        out = _node(_fns("projectionComplete", "partialProjection", "needsAttention"),
                    "var ts = %s;" % json.dumps(rows),
                    "console.log(ts.filter(needsAttention).length);")
        assert int(out.strip().splitlines()[-1]) == st["unresolved"]
    assert current_state(run, pending)["unresolved"] == current_state(run, decisions)["unresolved"] + 1
