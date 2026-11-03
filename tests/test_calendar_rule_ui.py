"""CALENDAR-RULE-001 UI (Manus 19:32 items 4 and 5): the complete ui/app.js under the existing stub DOM, fed the real
server envelope (GET /api/runs/<id>) built from real producers (make_run, recheck_row, decisions API). No browser."""
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from tests.test_pivot_proposal_flow import CASES, make_run
from tests.test_ui import UI

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

_HARNESS = r"""
const fs = require('fs');
const [, , appPath, payloadPath, mode] = process.argv;
function make(tag) {
  return { tag, className: '', textContent: '', children: [], style: {}, hidden: false, value: '', dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    append(...a) { for (const c of a) this.children.push(typeof c === 'string' ? { textContent: c, children: [] } : c); },
    appendChild(c) { this.children.push(c); return c; }, prepend(...a) { this.children.unshift(...a); },
    addEventListener() {}, removeEventListener() {}, setAttribute(k, v) { this[k] = v; }, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}, scrollIntoView() {}, remove() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },
    set innerHTML(v) { this.children = []; this._html = v; }, get innerHTML() { return this._html || ''; },
    firstChild: null };
}
const nodes = {};
global.document = { createElement: make, createTextNode: t => ({ textContent: String(t), children: [] }),
  getElementById: id => (nodes[id] = nodes[id] || make('div')), querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, body: make('body'), documentElement: make('html'), title: '' };
global.window = { addEventListener() {}, location: { hash: '' }, matchMedia: () => ({ matches: false, addEventListener() {} }),
  setTimeout, history: { replaceState() {}, pushState() {} }, getComputedStyle: () => ({}) };
global.location = window.location; global.history = window.history;
global.localStorage = { getItem() { return null; }, setItem() {} };
global.fetch = () => new Promise(() => {});
global.requestAnimationFrame = () => 0;
let src = fs.readFileSync(appPath, 'utf8');
src = src.replace(/route = parseHash\(\);\s*boot\(\);\s*\}\)\(\);\s*$/,
  'global.__H = { get detail(){return detail;}, set detail(v){detail=v;}, ev: function(s){ return eval(s); } };\n})();');
if (!/global\.__H/.test(src)) throw new Error('no boot hook');
eval(src);
const H = global.__H;
H.detail = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
function walk(n, f) { f(n); (n.children || []).forEach(c => walk(c, f)); }
if (mode === 'judgment') {
  H.ev("route = {screen:'judgment', runId:'synthetic', rowId:'e1', dialog:null}; judgmentSel='e1'; renderJudgment();");
  const buttons = [];
  walk(nodes['judgment-det'], n => { if (n.tag === 'button') buttons.push(n.textContent); });
  console.log(JSON.stringify({ buttons }));
} else {
  H.ev("route = {screen:'calendar', runId:'synthetic', rowId:null}; renderCalendar();");
  const text = [];
  walk(nodes['cal-mid'], n => { if (n.textContent) text.push(n.textContent); });
  console.log(JSON.stringify({ text }));
}
"""


def _render(tmp_path, detail, mode):
    script, payload = tmp_path / "h.js", tmp_path / "payload.json"
    script.write_text(_HARNESS)
    payload.write_text(json.dumps(detail))
    got = subprocess.run(["node", str(script), str(UI / "app.js"), str(payload), mode],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def _server(tmp_path, case=CASES[0], quote=None):
    make_run(case, tmp_path / "synthetic")
    client = TestClient(create_app(runs_dir=tmp_path))
    return client, "/api/runs/synthetic"


def _detail(client, root):
    body = client.get(root).json()
    return {"id": "synthetic", "run": body, "state": body["state"]}


def test_a_rechecked_fix_on_an_included_row_keeps_keep_fix_remove_and_undo(tmp_path):
    client, root = _server(tmp_path)
    row = client.get(root).json()["rows"][0]
    # A real Fix through the decisions API; the server writes the real re-check (recheck_row), which passes.
    new_quote = " ".join(row["quote"].split()[2:])
    r = client.post(root + "/decisions", json={"target": "e1", "action": "fix",
                                               "fix": {"field": "quote", "from": row["quote"], "to": new_quote}})
    assert r.status_code == 200, r.text
    detail = _detail(client, root)
    t = detail["state"]["targets"]["e1"]
    assert t["recheck"]["result"] == "pass" and t["last_action"] == "fix"
    assert t["inclusion"] == "included" and not t["accepted"] and not t["needs_attention"]
    buttons = _render(tmp_path, detail, "judgment")["buttons"]
    assert "Keep" in buttons and "Remove…" in buttons and "Undo" in buttons, buttons
    assert any(b in buttons for b in ("Fix…", "Fix again…")), buttons
    # negative control: once kept, the decided pane offers Undo and no new Keep
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    kept = _render(tmp_path, _detail(client, root), "judgment")["buttons"]
    assert "Undo" in kept and "Keep" not in kept and "Remove…" not in kept, kept
    # an untouched included row (no decision) also gets Keep / Fix / Remove
    client2, root2 = _server(tmp_path / "fresh")
    fresh = _render(tmp_path, _detail(client2, root2), "judgment")["buttons"]
    assert "Keep" in fresh and "Remove…" in fresh, fresh


def test_calendar_headline_never_calls_automatic_inclusion_kept(tmp_path):
    client, root = _server(tmp_path)
    detail = _detail(client, root)
    assert detail["state"]["kept_rows"] == 0 and detail["state"]["counts"]["inclusion"]["included"] == 1
    text = " ".join(_render(tmp_path, detail, "calendar")["text"])
    assert "Kept events" not in text and "Nothing kept yet" not in text
    assert "Events on the calendar, by date · 1 row · kept by you 0 · included automatically 1" in text
    # after a Keep the split moves to kept by you
    assert client.post(root + "/decisions", json={"target": "e1", "action": "keep"}).status_code == 200
    text = " ".join(_render(tmp_path, _detail(client, root), "calendar")["text"])
    assert "kept by you 1 · included automatically 0" in text
    # negative control: a removed row leaves an empty calendar, whose sentence names both ways in
    assert client.post(root + "/decisions", json={"target": "e1", "action": "undo"}).status_code == 200
    assert client.post(root + "/decisions", json={"target": "e1", "action": "remove",
                                                  "note": "Not ours."}).status_code == 200
    text = " ".join(_render(tmp_path, _detail(client, root), "calendar")["text"])
    assert "Nothing on the calendar yet. Rows with no issues are placed here automatically" in text


def test_calendar_headline_parts_count_exactly_the_rows_shown(tmp_path):
    """Kimi 813ce87: "5 rows · kept by you 0 · included automatically 2" counted Context/Later/Undated rows in N.
    N is now the calendar window alone and equals kept + included automatically; each other section is named."""
    import shutil as _sh
    from tests.test_runs import RUNS
    _sh.copytree(RUNS / "sample", tmp_path / "sample")
    client = TestClient(create_app(runs_dir=tmp_path))
    body = client.get("/api/runs/sample").json()
    part, targets = body["calendar_partition"], body["state"]["targets"]
    detail = {"id": "sample", "run": body, "state": body["state"]}
    text = " ".join(_render(tmp_path, detail, "calendar")["text"])
    head = next(t for t in _render(tmp_path, detail, "calendar")["text"] if t.startswith("Events on the calendar"))
    cal = part["calendar"]
    kept = sum(targets[e["id"]]["inclusion"] == "kept" for e in cal)
    auto = sum(targets[e["id"]]["inclusion"] == "included" for e in cal)
    assert kept + auto == len(cal)
    expected = [f"{len(cal)} row{'s' if len(cal) != 1 else ''}", f"kept by you {kept}", f"included automatically {auto}"]
    if part["past"]:
        expected.append(f"context (past) {len(part['past'])}")
    if part["later"]:
        expected.append(f"later {len(part['later'])}")
    undated = len(part["contingent"]) + len(part["undated"]) + len(part["timing_unresolved"])
    if undated:
        expected.append(f"undated {undated}")
    assert head.startswith("Events on the calendar, by date · " + " · ".join(expected) + " · "), head
    # the sample has rows outside the window, so the old single total would have overstated N
    assert part["later"] or part["past"] or undated
    assert "Kept events" not in text
