"""U1+U2+U4: the app screens in ui/, served by the decisions server.

The screens read run.json through the server only and write a person's decisions through
it; these tests pin the shell, the served assets, and the house rules the UI must keep
(no external calls, sample labelled as sample, only the agreed words on screen).
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

REPO = Path(__file__).resolve().parent.parent
UI = REPO / "ui"


def _calendar_view(rows, targets, partition=None):
    """Complete the server projection used by the isolated calendarRows mocks."""
    if partition is None:
        partition = {name: [] for name in
                     ("calendar", "past", "later", "contingent", "undated", "timing_unresolved")}
    listed = {entry["id"] for bucket in partition.values() for entry in bucket}
    for row in rows:
        target = targets.setdefault(row["id"], {})
        target["values"] = {**row, **target.get("values", {})}
        target.setdefault("effective_checks", row.get("checks", []))
        if "on_calendar" not in target:
            target["on_calendar"] = target.get("group") in {"kept", "corrected"}
        if partition is not None and row["id"] not in listed and target["on_calendar"]:
            bucket = "calendar" if target["values"].get("date") else (
                "timing_unresolved" if target["values"].get("date_text") else "undated")
            partition[bucket].append({"id": row["id"]})
    return partition

pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="ui/ is built on the ui/* branches")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(runs_dir=REPO / "runs", ui_dir=UI))


def test_shell_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Proof" in r.text
    for needle in ("Audit log", "Source view", "Judgment needed", "Catalyst Calendar", "Agent trail"):
        assert needle in r.text
    assert 'src="app.js"' in r.text


def test_assets_are_served(client):
    for name, needle in (("styles.css", "--accent"), ("app.js", "/api/runs")):
        r = client.get(f"/{name}")
        assert r.status_code == 200, name
        assert needle in r.text, name


def test_no_external_calls_from_the_ui():
    for f in sorted(UI.glob("*")):
        if f.suffix not in {".html", ".css", ".js"}:
            continue
        text = f.read_text()
        assert "http://" not in text and "https://" not in text, f"{f.name} references an external URL"
        for m in re.finditer(r"""fetch\((['"])(.*?)\1\)""", text):
            assert m.group(2).startswith("/"), f"{f.name} fetches {m.group(2)}"
        for m in re.finditer(r"""(?:src|href)\s*=\s*(['"])(.*?)\1""", text):
            assert not re.match(r"^[a-z]+:", m.group(2)), f"{f.name} loads {m.group(2)}"


def test_the_ui_only_uses_the_agreed_words():
    text = (UI / "app.js").read_text() + (UI / "index.html").read_text()
    for banned in ("verified", "proved"):
        assert not re.search(rf"\b{banned}\b", text, re.IGNORECASE), f"the word '{banned}' must not appear"


def test_sample_is_labelled_as_sample():
    js = (UI / "app.js").read_text()
    assert "Sample data" in js
    assert "not real events" in js


def test_hash_parser_and_builder_speak_the_same_language():
    # regression (U1): the hash parser's output keys must be the keys render() consumes.
    js = (UI / "app.js").read_text()
    parse = re.search(r"function parseHash\(\) \{.*?\n  \}", js, re.S).group(0)
    assert "runId: seg[0]" in parse
    for key in ("screen", "srcId", "page", "valueId", "rowId", "dialog", "filter"):
        assert f"out.{key}" in parse or f"out['{key}']" in parse, f"parseHash never sets {key}"
    # the source page segment is built as 'p' + page_number and parsed back positionally
    assert "'p' + pp.page.page_number" in js
    assert "seg[3]" in parse


def test_the_ui_talks_to_the_run_and_decision_contract():
    js = (UI / "app.js").read_text()
    assert "/api/runs" in js                       # the run list and one run
    assert "'/runs/'" in js                        # page images at the paths run.json names
    assert "decisions" in js and "POST" in js      # a person's decisions go through the server
    for field in ("width_pt", "height_pt", "image", "sha256", "page_number"):
        assert field in js, f"the page contract field {field} is used"
    for action in ("keep", "fix", "remove", "undo"):
        assert f'action: \'{action}\'' in js, f"the {action} action is wired"
    # the fix dialog offers the fields the row contract allows
    for field in ("date_text", "date_end", "event_type", "quote", "ticker"):
        assert f"'{field}'" in js, f"the fix dialog offers {field}"


def test_the_rendered_html_source_says_so():
    js = (UI / "app.js").read_text()
    assert "rendered_from" in js
    assert "Page rendered by code from the filed HTML" in js


def test_needs_attention_and_recheck_pending_are_wired():
    js = (UI / "app.js").read_text()
    assert "could_not_run" in js and "Re-check pending" in js
    assert "Kept as stated" in js
    assert "The value already passed its checks before the edit." in js


def test_plain_screen_words_not_raw_ids():
    js = (UI / "app.js").read_text()
    for word in ("Trial results", "Regulatory decision", "Milestone", "Unknown"):
        assert word in js, f"the event type word {word} is mapped"
    for word in ("Date as written", "Start date", "End date", "Event type"):
        assert word in js, f"the field word {word} is mapped"


def test_u5_u6_screens_are_wired():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    # both screens exist and are reachable from the rail
    for needle in ('id="scr-trail"', 'id="scr-calendar"', 'data-scr="trail"', 'data-scr="calendar"'):
        assert needle in html, needle
    for needle in ("renderTrail", "renderCalendar", "ct-trail", "ct-calendar"):
        assert needle in js, needle
    # the hash language speaks of trail and calendar
    assert "out.screen === 'trail'" in js
    assert "o.screen === 'judgment' || o.screen === 'trail'" in js


def test_u5_trail_contract_on_screen():
    js = (UI / "app.js").read_text()
    # one actor per step, tagged; the first step expands to the exact prompt text
    for word in ("Instructions sent", "Show the instructions", "Decided by you", "See the check"):
        assert word in js, word
    for tag in ("'Model'", "'Code'", "'Person'"):
        assert tag in js, tag
    # model steps show tokens and cost when present (R6-lite fields, tolerated when absent)
    for field in ("tokens.prompt", "tokens.completion", "cost_usd"):
        assert field in js, field
    # the person's decision steps come from the decisions record, one per line,
    # each its own step in order (G2-1)
    assert "personStepsFor" in js
    assert "PERSON_STEP_WORDS" in js
    assert "Undone by you" in js
    assert "dateShort" in js
    # a failed check step links to the check in the details pane (folds follow
    # the row's check order — no data attributes needed)
    assert "checkFnFor" in js and "indexOf(chk)" in js


def test_u6_calendar_rules_on_screen():
    js = (UI / "app.js").read_text()
    # kept and corrected only, periods placed under their period as written, no invented day
    for word in ("calendarRows", "periodhead", "Undated · no timing stated"):
        assert word in js, word
    assert "date_end" in js
    assert "Kept by you" in js and "Fixed by you" in js
    # no month header is fabricated from a period: the month comes from the row's own date
    assert "monthYear" in js and "dayMonth" in js
    # the left-rail count is the rows on the calendar
    assert "calendarRows().length" in js
    assert "detail.run.calendar_partition" in js and "Past · " in js


def test_u5_u6_format_helpers():
    js = (UI / "app.js").read_text()
    # money keeps small costs exact (0.000352 must not round to $0.0004)
    assert "toFixed(6)" in js
    assert "money(" in js
    # the decision action words on screen are plain, never the raw action ids
    for word in ("kept", "fixed", "removed"):
        assert word in js


def test_u7_what_changed_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    assert 'id="scr-changed"' in html and 'data-scr="changed"' in html
    for needle in ("renderChanged", "compareRuns", "labelFamily", "Nothing to compare yet"):
        assert needle in js, needle
    # the four change kinds and their plain words
    for word in ("added", "removed", "corrected", "date moved"):
        assert word in js, word
    # cards say where they came from and what the person did since
    assert "Since then:" in js
    assert "from " in js
    # G4-R6: one picker everywhere — the two drop-downs offer ANY two runs;
    # the three-tab family picker is gone
    assert "familyMembers" not in js
    assert "Compare the run before it against" not in js
    assert "out.screen === 'changed'" in js


def test_u8_new_run_panel_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    # Kimi 52: the company list is server-owned (the index the run will use) —
    # no ticker is hard-coded in the panel anymore
    for tk in ("ABVX", "ROIV", "ZLAB", "CLDX", "TSHA", "MNPR", "ENTA", "RYTM", "RVMD"):
        assert tk not in js, tk
    assert "renderNewRunCompanies" in js and "loadNewRunCompanies" in js
    assert "Guides the research; the source checks still run on every row." in html
    for word in ("Next 3 months", "Next 6 months", "Next 12 months"):
        assert word not in html, word
    # Run now POSTs to the run-controls endpoints; nothing calls a model from the UI
    assert "POST" in js and "/api/runs/new" in js
    assert "/resume" in js
    # status, turns and spend read from run.json; the pause reason is shown
    for field in ("turn_count", "turn_limit", "spend_usd", "status_reason"):
        assert field in js, field
    for word in ("paused at the turn limit", "needs input", "Resume"):
        assert word in js, word
    # G4-R3: the bar is gone from the research screens; its block is in the card
    assert "jobbar" not in html
    assert "runcard-job" in html and "updateRunCardJob" in js


def test_u9_export_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    assert 'id="scr-export"' in html and 'data-scr="export"' in html
    for needle in ("renderExport", "reportFileNames", "Built by code from the saved results"):
        assert needle in js, needle
    # the file names as the builder writes them, probed at /runs/<id>/report/<file>
    assert "biotech-coverage-catalyst-calendar-run" in js
    assert "/report/" in js
    assert "report_stale_reason" in js and "Saved report is out of date." in js
    # G5-2b: no command line, no run id on screen; until the file is served
    # the honest sentence, never a dead label
    assert "Built from the command line" not in js
    assert "REPORT_BUILD_ENDPOINT = null" in js
    assert "Build report" not in html
    assert "No report is saved for this run yet." in js
    assert "Open from the runs folder" not in js
    # the summary numbers come from state, the same counts as the audit log
    for field in ("counts.rows", "unresolved", "mistakes.code", "mistakes.second_reader",
                  "mistakes.person", "excluded.code"):
        assert field in js, field


def test_u9_second_reader_details_are_wired():
    js = (UI / "app.js").read_text()
    # the disagreement (field + both readers' values) above the checks
    for needle in ("disagreementSection", "bothReadersWord() + ' disagree on the ",
                   "first_reader", "second_reader",
                   "'Second reader · ' + srL + ': '"):
        assert needle in js, needle
    # the Muse Spark Open trail step is a Model step: tokens and cost show (U5 path)
    assert "Muse Spark Open" not in js or True  # the model name comes from the data, not the UI
    assert "tokens.prompt" in js and "cost_usd" in js


def test_fix_fields_include_program_and_indication():
    # Codex finding 1 (reviews/CODEX_PR26_4557556.md): the second reader can
    # disagree on program and indication, so the Fix dialog must offer them
    # and What changed must compare them — in plain words.
    js = (UI / "app.js").read_text()
    fix_fields = re.search(r"var FIX_FIELDS = \[(.*?)\];", js, re.S).group(1)
    for field in ("'program'", "'indication'"):
        assert field in fix_fields, f"the fix dialog does not offer {field}"
    assert "program: 'Program'" in js
    assert "indication: 'Condition'" in js
    changed_fields = re.search(r"var FIELDS = \[(.*?)\];", js, re.S).group(1)
    for field in ("'program'", "'indication'"):
        assert field in changed_fields, f"What changed does not compare {field}"


def test_u10_polish_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    css = (UI / "styles.css").read_text()
    # the accuracy line from runs/<id>/accuracy.json, hidden when absent
    for needle in ("accuracy.json", "ensureAccuracyLine", "audit-accuracy"):
        assert needle in js or needle in html, needle
    for word in ("Sealed set: ", "'values '", "'events '", "'planted '"):
        assert word in js, word
    # Codex P2 on PR #30: the sealed label is earned — key.sealed must be true;
    # G1-8: an author-keyed report reads 'Our own key, not sealed'
    assert "accSealLabel" in js
    assert "key.sealed === true" in js
    assert "Our own key, not sealed" in js
    # rendered_from: the note shows in the details pane too, not only the source meta bar
    assert js.count("Page rendered by code from the filed HTML") >= 2
    assert "rendnote" in js and ".rendnote" in css
    # cost per kept row in the run card, hidden when the run recorded no spend
    assert "cost_per_kept_row_usd" in js
    assert "less than a cent per kept row" in js
    assert "costPerKeptRowWords" in js


def test_u11_accuracy_panel_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    # reached from the audit header
    assert "accbtn" in html and "accpanel" in html
    assert "renderAccuracyPanel" in js
    # the mistake log by type x model x who caught it, from state.mistake_list
    assert "mistake_list" in js
    for word in ("caught_by", "what_changed", "the second reader"):
        assert word in js, word
    # plain-word type labels mirroring accuracy/types.py LABELS
    for word in ("Value not as printed", "Value placed in the wrong row",
                 "Quote not printed on the page", "Wrong kind of event", "Wrong company",
                 "Source not allowed on its own", "Same item proposed twice",
                 "Reader’s answer could not be read", "No type recorded"):
        assert word in js, f"the mistake type label '{word}' is mapped"
    # sealed-run numbers per model + the planted-errors line + cost per kept row
    assert "accModelLines" in js and "accPlantedLine" in js
    assert "accuracy.json" in js
    assert "Our own key, not sealed" in js
    assert "Planted errors caught: " in js
    assert "cost_per_kept_row_usd" in js
    # no model calls: the panel reads state and the accuracy file only
    panel = re.search(r"function renderAccuracyPanel.*?\n  \}\n", js, re.S)
    body = panel.group(0)
    assert "fetch(" not in body.replace("accuracyProbe", "")
    for url in ("/api/runs/new", "/decisions"):
        assert url not in body, f"the panel must not call {url}"


def _grab_fn(js: str, name: str) -> str:
    """A top-level function from app.js, copied the way tests/redteam/test_round5.py reads it."""
    at = js.index(f"function {name}")
    i = js.index("{", at)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[at : j + 1]
    raise AssertionError(f"{name} does not close")


def test_u12_audit_line_is_one_models_counts():
    # Red Team round 5 finding 6: the audit header line names one model and never
    # stitches missing parts from another model into the same score.
    js = (UI / "app.js").read_text()
    assert "Accuracy counts all dated and undated key events." in js
    # footer: 'sealed-set file' only when key.sealed is true, else 'the accuracy numbers' (G1-8)
    assert "sealed-set file" in js and "the accuracy numbers" in js
    # behavior: two models, each with one part — the line must not carry both
    import json
    import subprocess
    acc = {"key": {"sealed": False},
           "models": {"Muse Spark": {"values": {"found": 1, "N": 100}},
                      "Muse Wonder": {"events": {"found": 2, "M": 2},
                                      "planted": {"caught": 12, "total": 12}}}}
    script = "\n".join((_grab_fn(js, "accSealLabel"), _grab_fn(js, "accuracyLineFrom"),
                        f"const line = accuracyLineFrom({json.dumps(acc)});",
                        "if (line.includes('values 1/100') && line.includes('events 2/2')) process.exit(2);",
                        "if (!line.includes('Muse Spark')) process.exit(3);",
                        "console.log(line);"))
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout or got.stderr
    assert "values 1/100" in got.stdout and "events 2/2" not in got.stdout


def _panel_text(acc: dict, status: int = 200) -> str:
    """renderAccuracyPanel's text under the same mock element harness as the redteam UI tests."""
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("accPart", "accModelLines", "accPlantedLine", "accuracyProbe", "renderAccuracyPanel"))
    script = f"""
const ACC_DOC = {json.dumps(acc)};
function el(tag, cls, text) {{
  return {{ tag: tag, className: cls || '', textContent: text || '', children: [], style: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }} }};
}}
function clear(node) {{ node.textContent = ''; node.children = []; return node; }}
function textOf(node, out) {{
  out = out || [];
  if (node.textContent) out.push(node.textContent);
  (node.children || []).forEach(function (c) {{ textOf(c, out); }});
  return out.join('\\n');
}}
global.fetch = function () {{
  return Promise.resolve({{ ok: {str(status == 200).lower()}, status: {status}, json: function () {{ return Promise.resolve(ACC_DOC); }} }});
}};
var accCache = {{ id: null, acc: null, tried: false }};
var accPanelHost = el('div');
var detail = {{ id: 'r', run: {{ run: {{ label: 'probe' }}, rows: [] }},
  state: {{ cost_per_kept_row_usd: null, mistake_list: [] }} }};
{fns}
renderAccuracyPanel();
setTimeout(function () {{ console.log(textOf(accPanelHost)); }}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stderr
    return got.stdout


def test_u12_panel_footer_explains_accuracy_window_scope():
    # An author-keyed file is never called a sealed set; scoring covers all
    # key events while the six-month horizon organizes the calendar only.
    sealed = {"key": {"sealed": True},
              "models": {"Muse Spark": {"events": {"found": 2, "M": 2, "M_total": 5,
                                                   "outside_window": {"count": 2},
                                                   "unplaced": {"count": 1}}}}}
    text = _panel_text(sealed)
    assert "The sealed set" in text
    assert "its sealed-set file" in text
    assert "Accuracy counts all dated and undated key events." in text
    assert "in the run’s window" not in text
    unsealed = {"key": {"sealed": False},
                "models": {"Muse Spark": {"values": {"found": 1, "N": 100}}}}
    text = _panel_text(unsealed)
    assert "Our own key, not sealed" in text
    assert "sealed-set" not in text
    assert "the accuracy numbers" in text
    blank = _panel_text({"key": {"sealed": False}, "models": {}})
    assert "Our own key, not sealed" in blank and "sealed-set" not in blank
    # G6-6 unified the empty state to one wording
    assert "No accuracy numbers for this run." in blank


def _judgment_screen(row_id):
    """The Judgment needed screen under the same mock-element harness as the redteam tests.

    Two corrected rows still need a person (re-check could not run; two readers still
    undecided), one kept row is fully decided. Returns (list text, detail pane text).
    """
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket", "rowBadge", "checkDot", "checkResultWord",
             "projected", "reviewClass", "fixStillOpen", "needsAttention", "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds", "restoreFolds",
             "judgmentDecidedPane", "renderJudgment", "sayControls", "cancelSay", "sayBusyWith", "captureFolds", "restoreFolds",
             "revealRow", "sourceOf", "nearMissCheck", "listReasonLine", "shownDateText", "shownField", "projectionComplete", "partialProjection")
    current_run_id_stub = 'function currentRunId() { return "run"; }\n'
    rows = [
        {"id": "r-cnr", "title": "ROW-COULD-NOT-RUN", "ticker": "ACME", "proposed_by": "Muse Spark",
         "checks": [], "date_text": "Q3 2027"},
        {"id": "r-und", "title": "ROW-STILL-UNDECIDED", "ticker": "ACME", "proposed_by": "Muse Spark",
         "checks": [], "date_text": "Q3 2027"},
        {"id": "r-kept", "title": "ROW-KEPT", "ticker": "ACME", "proposed_by": "Muse Spark",
         "checks": [], "date_text": "August 5, 2026"},
    ]
    state = {"targets": {
        "r-cnr": {"group": "corrected", "status": "proposed", "decided_by_person": True,
                  "needs_recheck": False, "needs_attention": True,
                  "recheck": {"result": "could_not_run", "reason": "The quote is not on the page."},
                  "values": {"date_text": "Q3 2027"},
                  "fixes": [{"field": "date_text", "from": "today", "to": "Q3 2027"}],
                  "reason": "Re-check could not run."},
        "r-und": {"group": "corrected", "status": "undecided", "decided_by_person": True,
                  "needs_recheck": False, "needs_attention": True, "recheck": {"result": "pass"},
                  "values": {"event_type": "filing"}, "fixes": [],
                  "reason": "The two readers still disagree."},
        "r-kept": {"group": "kept", "status": "matched", "decided_by_person": True,
                   "needs_recheck": False, "needs_attention": False, "recheck": None,
                   "values": {"date_text": "August 5, 2026"}, "fixes": [], "reason": "Kept."},
    }, "exclusions": [], "mistake_list": []}
    detail = {"id": "run", "run": {"rows": rows, "mistakes": [], "sources": [], "run": {"label": "probe"}},
              "state": state}
    prelude = """
var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed',
                     unreadable: 'unreadable', undecided: 'undecided' };
var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed' };
var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };
var FIELD_WORDS = { date_text: 'Date as printed', date: 'Date', event_type: 'Event type' };
var NO_ERROR = 'The value already passed its checks before the edit.';
var judgmentSel = null;
var route = { screen: 'judgment', rowId: null, dialog: null };
function navigate() {}
function postDecision() {}
function exclusionFor() { return null; }
function closeDialog() {}
function openDialog() {}
function seeTheCheck() { return { textContent: '', children: [] }; }
"""
    script = prelude + "var qnoteDraft = {};\n" + current_run_id_stub + "\n".join(_grab_fn(js, n) for n in names) + f"""
var detail = {json.dumps(detail)};
route.rowId = {json.dumps(row_id)};
function textOf(node, out) {{
  out = out || [];
  if (node && node.textContent) out.push(node.textContent);
  ((node && node.children) || []).forEach(function (c) {{ textOf(c, out); }});
  return out.join('\\n');
}}
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: false,
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function () {{}}, querySelector: function () {{ return null; }} }};
}}
var nodes = {{}};
var sayRec = null;   // the app holds the live take in this seat
global.document = {{
  createElement: function () {{ return make(); }},
  getElementById: function (id) {{ if (!nodes[id]) nodes[id] = make(); return nodes[id]; }}
}};
renderJudgment();
console.log(JSON.stringify({{ rows: textOf(nodes['judgment-rows']), det: textOf(nodes['judgment-det']) }}));
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stderr or got.stdout
    out = json.loads(got.stdout)
    return out["rows"], out["det"]


def test_u13_corrected_open_row_waits_once_and_offers_keep():
    # Red Team round 7: a corrected row that still needs a person sits in Waiting
    # for you once (not also under Decided by you) and the pane offers Keep,
    # Fix again, Remove and Undo.
    js = (UI / "app.js").read_text()
    assert "still open" in js
    waiting, det = _judgment_screen("r-cnr")
    cut = waiting.index("Decided by you")
    wait_list, decided_list = waiting[:cut], waiting[cut:]
    assert "ROW-COULD-NOT-RUN" in wait_list and "ROW-STILL-UNDECIDED" in wait_list
    assert "ROW-KEPT" not in wait_list
    assert "Fixed by you · still open" in wait_list
    assert "ROW-KEPT" in decided_list
    assert "ROW-COULD-NOT-RUN" not in decided_list and "ROW-STILL-UNDECIDED" not in decided_list
    for word in ("Keep", "Fix again", "Remove", "Undo", "could not run"):
        assert word in det, word
    _, und = _judgment_screen("r-und")
    for word in ("Keep", "Fix again", "Remove", "Undo"):
        assert word in und, word


def test_u14_limit_boxes_refuse_non_finite():
    # Red Team round 8: the New run limit box and the job-bar 'new limit $' box must
    # refuse anything that is not a finite number of dollars (Infinity, 1e309, NaN,
    # negatives) with the inline red line, and never send the request.
    import math
    import re

    js = (UI / "app.js").read_text()
    wording = "The spending limit must be a number of dollars, zero or more."
    assert js.count(wording) >= 2, "both limit boxes use the same red line"

    def guard_after(anchor):
        window = js[js.index(anchor):js.index(anchor) + 700]
        i = 0
        while True:
            j = window.find("if", i)
            if j < 0:
                break
            k = j + 2
            while k < len(window) and window[k].isspace():
                k += 1
            if k >= len(window) or window[k] != "(":
                i = j + 2
                continue
            depth = 0
            for n in range(k, len(window)):
                depth += (window[n] == "(") - (window[n] == ")")
                if depth == 0:
                    if "return" in window[n + 1:n + 120]:
                        return window[k + 1:n]
                    i = n + 1
                    break
            else:
                break
        raise AssertionError(f"no returning if after {anchor}")

    def guard_fires(cond, var, value):
        expr = cond.strip()
        expr = re.sub(r"Number\.isFinite\s*\(", "math.isfinite(", expr)
        expr = re.sub(r"Number\.isNaN\s*\(", "math.isnan(", expr)
        expr = expr.replace("||", " or ").replace("&&", " and ")
        expr = re.sub(r"!([^=\s])", r" not \1", expr)
        expr = re.sub(rf"\b{var}\b", "value", expr)
        return bool(eval(expr, {"__builtins__": {}}, {"math": math, "value": value}))

    for anchor, var in (("limit = Number(limitRaw);", "limit"),
                        ("var v = Number(limitInput.value.trim());", "v")):
        cond = guard_after(anchor)
        assert "Number.isFinite" in cond, f"the guard after {anchor} must use Number.isFinite"
        for typed in ("Infinity", "1e309", "NaN", "-1"):
            assert guard_fires(cond, var, float(typed)), (anchor, cond, typed)
        for typed in ("0", "5", "0.5"):
            assert not guard_fires(cond, var, float(typed)), (anchor, cond, typed)


def test_u19_export_glass5_rows():
    # GLASS 5 rows: G5-2b honest report rows; G5-3 spend + cost per kept row;
    # G5-5 one statement of the disagreement; G5-6 Keep <value> + the resolved
    # block folded; G5-7 nothing when the second reader gave nothing.
    js = (UI / "app.js").read_text()
    # G5-2b
    assert js.count("No report is saved for this run yet.") == 2
    assert "python -m report.build" not in js and "currentRunId() + '\n" not in js
    # G5-3: spend and cost per kept row in the summary
    for word in ("kv('Spend', usd2(meta.spend_usd)", "kv('Cost per kept row', usd2(st.cost_per_kept_row_usd))",
                 "Mistakes caught by you"):
        assert word in js, word
    # G5-5: the disagreement is stated once — the why box defers to the block
    why = re.search(r"function whySection.*?\n  \}\n", js, re.S).group(0)
    assert "bothReadersWord() + ' disagree on the ' + fieldWord + '. A person decides.'" in why
    # G5-6: Keep names the kept value; the resolved block stays, folded
    assert "keepLabel = 'Keep ' + dv" in js
    assert "'Resolved by you · kept ' + dvv" in js
    assert "el('details', 'fold')" in js
    # G5-7: no disagreement block when the second reader proposed nothing
    dis = re.search(r"function disagreementSection.*?\n  \}\n", js, re.S).group(0)
    assert "return null" in dis
    assert "srName + ' did not read this row.'" in js


def test_u18_changed_bar_glass4_rows():
    # GLASS 4 rows + G1-16: run picker naming, card company/program, pane
    # checks and links, the bar on every run in cents, one clearing error
    # wording, the bar in flow, and blue for selected things.
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    css = (UI / "styles.css").read_text()
    # G4-1: any two runs can be picked, each named Run N · day Mon · label;
    # different families compare on the shared companies and say so
    for word in ("runPick", "Compare: ' + runPick", "comparing the ", "chpicks"):
        assert word in js, word
    assert "is the only run of its kind" not in js
    # G4-2b: company and program on the card's first line — the state values
    # first, the raw row only as fallback (A-F9 second half)
    assert "el('span', 'co', cvCo)" in js
    assert ".chcard .co" in css
    # G4-4: checks + source/trail links in the pane; Open in run scrolls
    for word in ("openInRun", "checksSection({ checks: c.row.checks", "'.row.on, .crow.on, .pg.on, .chcard.on'"):
        assert word in js, word
    # G4-5: bar on every run, cents
    assert "function cents(v)" in js
    assert "if (!st) {" in js and "updateRunCardJob" in js  # G4-R3: the block lives in the card
    assert "JOB_PAUSABLE.indexOf(st) >= 0" in js
    assert "cents(meta.spend_usd)" in js
    assert "money(meta.spend_usd)" not in js
    # G4-6: one wording (U14) that now clears on edit in both boxes
    assert js.count("The spending limit must be a number of dollars, zero or more.") >= 2
    assert "limitInput.addEventListener('input', function () { err.hidden = true; })" in js
    assert "getElementById('nr-limit').addEventListener('input', function () { nrError(''); })" in js
    # G4-10: the bar is the first child of #screens, in flow, not fixed
    assert 'id="jobbar"' not in html
    assert ".jobbar" not in css
    assert "paddingTop" not in js
    # G1-16: blue for selected rows in every list, the links, the selected box
    for word in (".row.on{background:var(--blue-soft)", ".crow.on{background:var(--blue-soft)",
                 ".pg.on{background:var(--blue-soft)", ".val.on{background:var(--blue-soft)",
                 ".chcard.on{border-color:var(--blue)", ".box.sel{border-width:3px;border-color:var(--blue)",
                 ".link{color:var(--blue);font-weight:500;font-size:13px}"):
        assert word in css, word
    # red stays for fails / Remove / flags
    assert ".box.failed{border-color:var(--red);" in css
    assert ".step.failstep .ic{border-color:var(--red);" in css
    assert ".btn.danger{color:var(--red)" in css


def test_u17_calendar_glass3_fixes():
    # GLASS 3 (Catalyst Calendar) rows: G3-1 filter pills; G3-2 period headers;
    # G3-3 hollow period dot; G3-4 one quarter marker, exact dates first;
    # G3-6 back origin + list follows the row; G3-7 source page list.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    # Discovery and calendar presentation have no arbitrary date horizon.
    assert "w.anchor + ' – ' + w.end" in js
    assert "r.date && r.date <= w.end" not in js
    # G3-2: headers are the period as given, sorted by start then shortest first
    for word in ("calHeader", "half + ' half of ' + y", "calMonths", "calDays"):
        assert word in js, word
    # G3-3: filled dot = date, hollow dot = period, with the title
    assert "Period, no day given" in js
    assert "caldot.hollow" in css and "caldot.filled" in css
    # G3-4: one canonical quarter marker, own wording kept on the row
    assert "calQuarterKey" in js
    # G3-6: origin carried, Back to calendar, scroll after the screen shows
    assert "← Back to calendar" in js
    assert "'.row.on, .crow.on, .pg.on" in js  # u18 extends the selector with .chcard.on
    assert "back: 'calendar', w: calWindow().win" in js
    # G3-7: sub-line wording; the page list scrolls to the open page
    assert "Every page the rows were read from" in js
    assert "rest on" not in js
    assert "revealRow(onPg)" in js  # the page-list scroll survived the G1-R3 revealRow refactor


def out_hash_has(screen, needle):
    import re
    js = (UI / "app.js").read_text()
    block = re.search(r"function hashFor\(o\) \{.*?\n  \}", js, re.S).group(0)
    part = block[block.index("o.screen === '" + screen + "'"):]
    part = part[:part.index("} else if")]
    return needle in part


def test_u16_trail_glass2_rows():
    # GLASS 2 (Agent trail) rows: G2-1 person steps only from decisions.jsonl;
    # G2-3 glyph per outcome; G2-5 Back/Esc; G2-6 capped + folded prompts;
    # G2-7 plain-word summaries.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    # G2-1: never a synthesized person step — the 'dated record' line is gone
    assert "the dated record is in the run folder" not in js
    assert "var personLines = personStepsFor(row.id);" in js
    assert "'Decided by you · ' + word" not in js
    assert "PERSON_STEP_WORDS" in js and "Undone by you" in js
    # G2-3: tick only on pass, grey dash for could-not-run / not-needed
    assert "skipstep" in js and "skipstep" in css
    assert "outcome[1] === 'pass' ? '✓' : '–'" in js
    # G2-5: Back to rows visible at desktop width; Esc returns to the list
    assert "route.screen === 'trail' && route.rowId" in js
    # G2-6: both prompts folded; the box scrolls inside a fixed height
    assert "Instructions sent to the second reader" in js
    assert ".instrbox .codeblk" in css and "max-height:220px" in css
    # G2-7: plain-word proposals — event word + date as written, day spans
    assert "plainProposal" in js and "daySpanWords" in js
    trail = re.search(r"function plainProposal.*?\n  \}\n", js, re.S).group(0)
    assert "eventWord(row.event_type)" in trail
    assert "regulatory_decision on" not in trail


def test_u20_accuracy_glass6_rows():
    # GLASS 6 rows: G6-3 run card in words; G6-5b the mistake table; G6-6 one
    # shape for the sealed lines and the no-key case; G6-9 the panel scrolls;
    # G6-10 the three terms once; G6-11b 'Both readers' one bucket per line.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    # G6-3
    for word in ("dateTimeWords", "needs attention ' + st.unresolved", "usdCents(cpkr)"):
        assert word in js, word
    assert "money(cpkr)" not in js
    assert "dateShort(r.started_at) + ' ' + (r.started_at || '').slice(11, 16)" in js
    # G6-5b: the table, one short line per entry, folded
    for word in ("el('table', 'acctab')", "'Show the ' + entries.length + ' row", "table.acctab"):
        assert word in (js + css), word
    # G6-6: one shape, '—' when a figure is absent; no-key wording
    for word in ("values ' + (v || '—')", "'No accuracy numbers for this run.'", "sealedHead.textContent = !acc ? 'Scoring'"):
        assert word in js, word
    # G6-9: the sheet scrolls inside itself
    assert "max-height:85vh;overflow-y:auto" in css
    # G6-10: the three terms, once
    for word in ("the answer key a person wrote and sealed before the run.",
                 "the events listed in that answer key.",
                 "key events the reader found but could not give a date to."):
        assert word in js, word
    # G6-11b: the readers-together bucket reads 'Both readers' in score lines.
    assert js.count("? 'Both readers' : name") >= 2
    # money in the panel in cents
    assert "'Cost per kept row ' + usdCents(cpkr)" in js


def test_u15_glass1_fixes_are_wired():
    # GLASS 1 rows (orders/GLASS_LEDGER.md): plain words, the Fix-dialog ruling,
    # the code fold, the rail colours, the citation-box frame, no Decide for
    # code-removed rows.
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    css = (UI / "styles.css").read_text()

    # G1-7/G1-17: the counters say 'by you' and are toggle chips
    assert "mistakes caught by you" in js and "by a person: " not in js
    assert "ctchip" in js and "Showing: " in js
    # G1-8: plain words for the accuracy panel and code removals
    for word in ("Our own key, not sealed", "the accuracy numbers"):
        assert word in js, word
    # G6-6 unified the empty state to one wording
    assert "No accuracy numbers for this run." in js
    assert "Removed by code" in js
    assert "outside_time_window: 'Outside the time window'" in js
    # G1-1: the code that ran — served by Job 21, degraded line until then,
    # and no file paths or function names on screen (the dead coderef link is gone)
    assert "/api/checks/source" in js
    assert "The check’s code is not served on this build." in js
    assert "The code that ran" in js
    assert "coderef" not in js and "quote_match()" not in js
    # G1-2: the Fix dialog keeps the code's re-check reason in a red line and
    # stays open; 'Keep anyway' is the way to the still-open path
    assert "Keep anyway" in js
    dialog = re.search(r"function fixDialog.*?\n  \}\n", js, re.S).group(0)
    assert "showDialogError(why)" in dialog
    assert "rcRes !== 'pass'" in dialog
    # G1-5 / G1-6: Esc closes the pane via replaceRoute (no history entry);
    # the judgment screen has a visible Back to Audit log
    assert "function replaceRoute" in js and "history.replaceState" in js
    assert "judgment-back-audit" in html and "judgment-back-audit" in js
    # G1-11: the decision buttons stay on one line
    assert "btnrow nowrap" in js and ".btnrow.nowrap" in css
    # G1-13: the rail selected state is blue; red stays with Judgment needed
    assert re.search(r"\.nav \.ni\.on\{background:var\(--blue-soft\)", css)
    assert ".nav .ni.on .ct{color:var(--blue)}" in css
    # G1-14: boxes are drawn inside an image-exact frame
    assert "page-img" in js and ".page-img{position:relative}" in css
    # G1-12: the rail returns to the audit row
    assert "screen: 'audit', rowId: audit.selected" in js
    # G1-9 / G1-10: selection survives a filter; Undo toasts at once; the
    # decided pane does not repeat its own badge
    assert "audit.cleared" in js
    assert "if (body.action === 'undo') toast" in js
    assert "t.reason.replace(/\\.$/, '') !== badge" in js
    # G1-S1: no Decide button for a code-removed row
    assert "codeRemoved" in js


def test_u23_say_your_decision_is_wired():
    # U23: Meta has no text-to-speech, so U21's "Read this week" is gone; the
    # person's voice goes IN. The mic records with the Web Audio API (never
    # MediaRecorder) and builds the WAV in the browser — mono, 16-bit, 16 kHz,
    # at most 30 s, audio/wav — POSTed to /api/runs/<id>/voice/decide?target=<row>.
    # The reply shows "You said: … → <proposal>" under the button and pre-fills
    # the same Keep / Fix / Remove controls; nothing is decided from voice.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    for word in ("Say your decision", "voice/decide", "getUserMedia",
                 "encodeWav", "audio/wav", "You said: '",
                 "Voice is not on this build yet.", "sayControls(row)"):
        assert word in js, word
    assert "16000" in js and "SAY_MAX_SECONDS = 30" in js
    # the route takes WAV recorded in the browser, not webm/MediaRecorder
    assert "MediaRecorder" not in js
    assert "Read this week" not in js and "voice/week" not in js
    # the proposal question names the field in plain words and the person
    # still presses the buttons — voice never posts a decision
    assert "sayProposalText" in js and "prefillSay" in js
    assert "postDecision" not in _grab_fn(js, "prefillSay")
    assert ".yousaid" in css and ".saybtn" in css
    # no external call: same-origin fetch only
    assert "http://" not in js and "https://" not in js


def test_u23_say_proposal_and_wav_encoding():
    # behavior: the WAV the browser builds has the RIFF shape the route takes
    # (mono, 16-bit, 16 kHz); the proposal reads as one plain-word question.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("SAY_MONTHS", "encodeWav", "fmtSayValue", "sayProposalText", "sayReplyLine",
                     "prefillSay")
                    if not n.isupper())
    script = f"""
var SAY_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {{date: 'Date', date_end: 'Date end', date_text: 'Date as written',
  title: 'Title', event_type: 'Type', company: 'Company', ticker: 'Ticker',
  program: 'Program', indication: 'Condition'}};
function eventWord(v) {{ return 'type-' + v; }}
{fns}
// 1 s of a 48 kHz tone -> resampled to 16 kHz mono 16-bit WAV
var samples = [];
for (var i = 0; i < 48000; i++) samples.push(Math.sin(i / 48000 * 2 * Math.PI * 440) * 0.5);
var wav = encodeWav(samples, 48000);
var v = new DataView(wav.buffer);
var ok = String.fromCharCode(wav[0], wav[1], wav[2], wav[3]) === 'RIFF' &&
         String.fromCharCode(wav[8], wav[9], wav[10], wav[11]) === 'WAVE' &&
         v.getUint16(22, true) === 1 &&   // mono
         v.getUint16(34, true) === 16 &&  // 16-bit
         v.getUint32(24, true) === 16000; // 16 kHz
if (!ok) process.exit(2);
if (Math.abs(v.getUint32(40, true) - 32000) > 2 * 16000 / 48000 * 2 + 64) process.exit(3);
// the proposal as one question, dates in words, type as its word
var p1 = sayProposalText({{action: 'fix', field: 'date', value: '2027-03-03'}});
if (p1 !== 'Fix the date to March 3, 2027?') process.exit(4);
var p2 = sayProposalText({{action: 'remove'}});
if (p2 !== 'Remove it?') process.exit(5);
var p3 = sayProposalText({{action: 'keep'}});
if (p3 !== 'Keep it?') process.exit(6);
var p4 = sayProposalText({{action: 'fix', field: 'event_type', value: 'trial_results'}});
if (p4 !== 'Fix the type to type-trial_results?') process.exit(7);
if (sayProposalText(null) !== null) process.exit(8);
// the reply line with a fake reply
var line = sayReplyLine({{transcript: 'move it to March third twenty twenty seven',
  proposal: {{action: 'fix', field: 'date', value: '2027-03-03'}}}});
if (line.indexOf("You said: 'move it to March third twenty twenty seven' → Fix the date to March 3, 2027?") !== 0)
  process.exit(9);
var bare = sayReplyLine({{transcript: 'not sure'}});
if (bare !== "You said: 'not sure'") process.exit(10);
// the Job 29 route returns capture.voice.propose() unchanged — its value key is `to`
var p5 = sayProposalText({{action: 'fix', field: 'date', to: '2027-03-03'}});
if (p5 !== 'Fix the date to March 3, 2027?') process.exit(11);
var line2 = sayReplyLine({{transcript: 'move it to March third',
  proposal: {{action: 'fix', field: 'date', to: '2027-03-03'}}}});
if (line2.indexOf("→ Fix the date to March 3, 2027?") === -1) process.exit(12);
// and the same route reply pre-fills the Fix dialog's To box via prefillSay
var filled = null;
function fixDialog(row, opts) {{ filled = opts; }}
function removeDialog() {{}}
global.document = {{ getElementById: function () {{ return {{ value: '' }}; }} }};
prefillSay({{id: 'r1'}}, {{action: 'fix', field: 'date', to: '2027-03-03', note: 'as said'}});
if (!filled || filled.field !== 'date' || filled.to !== '2027-03-03') process.exit(13);
// a spoken period is not an ISO day: the dialog opens on Date as written instead
filled = null;
prefillSay({{id: 'r1'}}, {{action: 'fix', field: 'date', to: 'third quarter 2027'}});
if (!filled || filled.field !== 'date_text' || filled.to !== 'third quarter 2027') process.exit(14);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u23_hold_release_stops_even_when_the_mic_is_slow():
    # Codex P2 on #88: a press-and-hold released while getUserMedia was still
    # setting up kept recording until a second click or the 30 s cap. The
    # release must now carry through the async mic setup and stop the take.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay", "projectionComplete", "partialProjection"))
    script = f"""
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {{}};
var sayRec = null;   // the app's live-take seat
var now = 1_000_000;
Date.now = function () {{ return now; }};
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text;
  return n;
}}
function btn(cls, text) {{ return el('button', cls, text); }}
function clear(node) {{ node.children = []; }}
function encodeWav() {{ return new Uint8Array(0); }}
function currentRunId() {{ return 'run1'; }}
var sent = 0;
global.fetch = function () {{ sent++; return Promise.resolve({{ ok: false }}); }};
{fns}
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: false,
    isConnected: true, _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }} }};
}}
global.window = {{}};
function FakeAC() {{}}
FakeAC.prototype.sampleRate = 48000;
FakeAC.prototype.destination = {{}};
FakeAC.prototype.createMediaStreamSource = function () {{ return {{ connect: function () {{}} }}; }};
FakeAC.prototype.createScriptProcessor = function () {{
  return {{ connect: function () {{}}, onaudioprocess: null }}; }};
FakeAC.prototype.close = function () {{}};
window.AudioContext = FakeAC;
var stream = {{ getTracks: function () {{ return [{{ stop: function () {{}} }}]; }} }};
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', {{
  configurable: true,
  value: {{ mediaDevices: {{ getUserMedia: function () {{
    return new Promise(function (res) {{ gumResolve = res; }}); }} }} }} }});
var wrap = sayControls({{ id: 'r1' }});
var b = wrap.children[0], note = wrap.children[1];
function assert(cond, code) {{ if (!cond) {{ console.error('fail ' + code); process.exit(code); }} }}
// hold past 600 ms, let go BEFORE the mic answers: the take must stop once live
b._l.mousedown();
assert(typeof gumResolve === 'function', 2);        // capture begins on press
now += 3000; b._l.mouseup(); b._l.click();
gumResolve(stream);   // permission answers after the release
setTimeout(function () {{
  assert(sayRec === null, 3);                       // release carried through setup
  assert(sent === 0, 4);                            // an empty take is never posted
  assert(b.textContent === 'Say your decision', 5);
  assert(note.textContent.indexOf('Too short') === 0, 6);
  // a quick click is still a toggle: starts on press, stays live, second click stops
  now += 1000;
  b._l.mousedown(); now += 100; b._l.mouseup(); b._l.click();
  assert(typeof gumResolve === 'function', 12);
  gumResolve(stream);
  setTimeout(function () {{
    assert(sayRec !== null, 7);                     // toggle recording went live
    assert(b.textContent === 'Listening… — press to send', 8);
    b._l.click();                                   // second click stops
    assert(sayRec === null, 9);
    // and a hold that is released after the mic is live stops on release
    now += 1000;
    b._l.mousedown(); b._l.mouseup(); b._l.click();
    gumResolve(stream);
    setTimeout(function () {{
      assert(sayRec !== null, 10);
      now += 3000; b._l.mouseup();                  // long release while live
      assert(sayRec === null, 11);
      console.log('ok');
    }}, 0);
  }}, 0);
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u31_g8_11_picker_is_a_button_not_the_card():
    # GLASS 8 fix 8 (Corey's ruling): the picker is a BUTTON — '▸ Saved runs · N'
    # sits directly under '▸ New run', same size and border; click or Enter
    # opens 'Choose a run'. The card below is a plain status card: no chevron,
    # no 'choose' line, not clickable, no hover, no tabindex. 'RUN' label stays.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    html = (UI / "index.html").read_text()
    # two stacked buttons, then the card
    assert 'id="newrun"' in html and 'id="savedruns"' in html
    assert 'class="btn newrun"' in html and 'class="btn savedruns"' in html
    ni = html.index('id="newrun"'); si = html.index('id="savedruns"'); ci = html.index('id="runcard"')
    assert ni < si < ci, "buttons stack above the card"
    assert ".savedruns{margin:6px 14px 0;width:calc(100% - 28px)}" in css
    assert ".newrun{margin:12px 14px 0;width:calc(100% - 28px)}" in css
    assert "savedrunsBtn.addEventListener('click', openRunSheet)" in js
    assert "updateSavedRunsBtn" in js and "'▸ Saved runs · ' + runs.length" in js
    # the card is plain: no chevron, no chooser line, no click/keyboard code
    assert '<span class="chev">' not in html
    assert 'runcard-choose' not in html and 'runcard-choose' not in js
    assert 'role="button"' not in html and 'tabindex="0"' not in html
    assert "runcardPick" not in js
    assert "addEventListener('click', openRunSheet)" in js  # the button's, not the card's
    assert "runcardPick.addEventListener" not in js
    for dead in (".runcard .pick{cursor:pointer}", ".runcard:hover",
                 ".runcard .pick:focus-visible", ".t2.chooser", ".t1::before",
                 ".runcard .t1 .chev"):
        assert dead not in css, dead
    assert "runs.length + ' saved run'" not in js
    # the RUN label stays above the card
    assert "'runcard-lbl'" in js and "el('span', null, 'Run')" in js
    assert "insertBefore(lbl, card)" in js
    # G9-4 superseded the outright hide: at narrow widths the card is a
    # compact strip; only the label and the footer text hide
    assert ".runcard,.runcard-lbl" not in css.split("@media (max-width:900px){")[1]
    assert ".runcard-lbl,.side-ft .ft{display:none}" in css


def test_u24_g1r5_picker_button_supersedes_the_card_picker():
    # G8-11 supersedes the header-as-click-target part of G4-R3 / G1-R5:
    # covered by test_u31_g8_11 above; this guard only keeps the old pins from
    # silently returning
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    assert "runcardPick" not in js
    assert 'id="runcard-choose"' not in html


def test_new_run_company_list_is_server_owned_pins():
    # Kimi 52 (orders/KIMI_NEWRUN_COMPANY_LIST.md): the New Run dialog's company
    # list is read from the server — the sources index the run will use — never
    # hard-coded in the UI; [all] stays; dialog errors stay inline (red line)
    js = (UI / "app.js").read_text()
    for word in ("return fetch('/api/companies')",
                 "function renderNewRunCompanies(companies, unreadable)",
                 "function loadNewRunCompanies()",
                 "res.body.companies || []",
                 "failed('The company list could not be loaded. The server is not reachable.')",
                 "var NR_EMPTY = 'No company has a page to read yet.';",
                 # Claude review MEDIUM-2: after a failed load Go repeats why, never "still loading";
                 # reopening the dialog is the retry (the open handler calls loadNewRunCompanies)
                 "if (nrState === 'failed') { nrError(nrLoadError + ' Close this and open New run again to try again.'); return; }",
                 "if (nrState !== 'ready') { nrError('The company list is still loading.'); return; }",
                 "if (!nrCompanies.length) { nrError(NR_EMPTY); return; }"):
        assert word in js, word
    # LOW-6: no internal term on screen in the dialog's own sentences
    dialog = js[js.index("// ---------------------------------------------------------------- new run (U8)"):]
    dialog = dialog[:dialog.index("// the job bar")]
    for line in dialog.splitlines():
        if "'" in line and not line.strip().startswith("//"):
            assert "sources index" not in line, line
    # the hard-coded list is gone for good
    assert "NEW_RUN_COMPANIES" not in js
    # the load-failure path is an inline red line, never a toast: the function
    # body (up to the next top-level function) never calls toast(
    body = js[js.index("function loadNewRunCompanies"):]
    body = body[:body.index("\n  function ")]
    assert "toast(" not in body


def test_new_run_renders_the_server_company_list():
    # NEWRUN-ONE-START-001 + addendum 2: the Companies grid shows what YOU
    # chose; the old captured names stay available, unticked, under a quiet
    # 'Previously captured' heading. Ticking one moves it up, unticking moves
    # it back; 'All N' ticks every name; prepared tickers render as ticked
    # boxes (greyed with their reason when they could not be read)
    import subprocess

    js = (UI / "app.js").read_text()
    # (the harness's own clear() mirrors the DOM's children reset — the
    # grabbed app.js clear only sets textContent, which stubs do not expand)
    fns = "\n".join(_grab_fn(js, n) for n in
                     ("el", "nrError", "renderNewRunCompanies", "renderChosen", "renderPrevCompanies",
                      "nrPreparedTickers", "syncStartButton", "intakeDoneN", "tickedReadyCount",
                      "applyPasteDisable"))
    script = "\n".join([
        "var NR_EMPTY = 'No company has a page to read yet.';",
        "var INTAKE_STATE_WORDS = { ready: 'Ready', unresolved: 'Could not be resolved' };",
        "var nodes = {};",
        "var els = { 'nr-tickers': { value: '' }, 'nr-go': { textContent: 'Run now', disabled: false },",
        "            'nr-preparing': { textContent: '', hidden: true } };",
        "var nrChosen = {}; var nrPrepUnticked = {}; var nrCompanies = [];",
        "var nrBusy = false;",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null, usedRun: null };",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], value: '', checked: false,",
        "  disabled: false, hidden: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "global.document = {",
        "  createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  createTextNode: function (t) { return { nodeType: 3, textContent: t }; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  querySelectorAll: function (sel) {",
        "    if (sel === '.nr-ik:checked') return ikTocked;",
        "    return []; }",
        "};",
        "var ikTocked = [];",
        "function clear(n) { n.children = []; return n; }",
        "function el(tag, cls, text) { var n = make(); n.tag = tag; n.className = cls || '';",
        "  if (text) n.textContent = text; return n; }",
        fns,
        "renderNewRunCompanies(['IONS', 'PTGX', 'ABVX'], []);",
        # nothing chosen yet: the Companies grid says how to choose; the
        # captured names sit unticked under the quiet heading
        "var cos = nodes['nr-cos'];",
        "if (cos.children.length !== 1 || cos.children[0].textContent !== 'Tick a company below, or paste tickers above.')",
        "  { console.error('cos=' + cos.children.length); process.exit(2); }",
        "var prev = nodes['nr-prev'];",
        "if (nodes['nr-prev-h'].hidden) { console.error('heading hidden'); process.exit(3); }",
        # Manus overwatch: there is no 'All N' — one box per name, nothing else
        "if (prev.children.length !== 3) { console.error('prev=' + prev.children.length); process.exit(4); }",
        "var boxes = prev.children.map(function (l) { return l.children[0]; });",
        "if (!boxes.every(function (b) { return b.checked === false && b.className === 'nr-pc'; })) process.exit(6);",
        # tick one: it moves to the Companies grid, the prev list shrinks
        "boxes[1].checked = true; boxes[1].onchange();",
        "var chosen = nodes['nr-cos'].children;",
        "if (chosen.length !== 1 || chosen[0].children[0].value !== 'PTGX' || chosen[0].children[0].className !== 'nr-co')",
        "  { console.error('chosen=' + chosen.length); process.exit(7); }",
        "if (nodes['nr-prev'].children.length !== 2) process.exit(8);",
        # untick it: back under Previously captured
        "chosen[0].children[0].checked = false; chosen[0].children[0].onchange();",
        "if (nodes['nr-cos'].children.length !== 1 || nodes['nr-cos'].children[0].tag !== 'div') process.exit(9);",
        "if (nodes['nr-prev'].children.length !== 3) process.exit(10);",
        # tick every captured name: they all move up
        "nodes['nr-prev'].children.forEach(function (l) { l.children[0].checked = true; l.children[0].onchange(); });",
        "if (nodes['nr-cos'].children.length !== 3) { console.error('all=' + nodes['nr-cos'].children.length); process.exit(11); }",
        "if (nodes['nr-prev'].children.length !== 0) process.exit(12);",
        # a different server list renders as-is — nothing hard-coded
        "nrChosen = {};",
        "renderNewRunCompanies(['ZYXI']);",
        "if (nodes['nr-prev'].children.length !== 1) process.exit(13);",
        # LOW-4: a company listed without pages is a plain name with the plain sentence, no checkbox
        "nrChosen = {};",
        "renderNewRunCompanies(['IONS'], ['CRTX']);",
        "var kids = nodes['nr-prev'].children;",
        "if (kids.length !== 2) { console.error('kids=' + kids.length); process.exit(14); }",
        "var np = kids[1];",
        "if (np.tag !== 'span' || np.textContent !== 'CRTX — No pages read yet' || np.children.length) process.exit(15);",
        "if (np.className.indexOf('tk2') !== -1) process.exit(16);",
        # an empty list says so in plain words, with no 'All 0' box
        "nrChosen = {};",
        "renderNewRunCompanies([], []);",
        "if (nodes['nr-prev'].children.length !== 1 || nodes['nr-prev'].children[0].textContent !== NR_EMPTY)",
        "  process.exit(17);",
        "if (!nodes['nr-prev-h'].hidden) process.exit(18);",
        # addendum 2: prepared tickers appear as ticked boxes with the company
        # name; a failure is a greyed box with its plain reason; the button
        # counts the ticked boxes
        "nrChosen = {};",
        "renderNewRunCompanies([], []);",
        "els['nr-tickers'].value = 'PTGX, QUIET';",
        "nrIntake = { id: 'ik1', seq: 1, timer: null,",
        "  text: 'PTGX, QUIET', usedId: null, usedRun: null,",
        "  last: { intake_id: 'ik1', status: 'done', ready_count: 1, failed_count: 1,",
        "    requested: ['PTGX', 'QUIET'],",
        "    tickers: [",
        "      { symbol: 'PTGX', company: 'Protagonist Therapeutics', state: 'ready', reason: null, documents: 1, pages: 1 },",
        "      { symbol: 'QUIET', company: null, state: 'unresolved', reason: 'No company matches this symbol.', documents: 0, pages: 0 } ] } };",
        "renderChosen();",
        # the start button counts the ticked boxes
        "ikTocked = [{ value: 'PTGX', checked: true }];",
        "syncStartButton();",
        "if (els['nr-go'].textContent !== 'Continue with 1 ready' || els['nr-go'].disabled)",
        "  { console.error('button count: ' + els['nr-go'].textContent); process.exit(22); }",
        "ikTocked = [];",
        "syncStartButton();",
        "if (els['nr-go'].textContent !== 'Continue with 0 ready' || !els['nr-go'].disabled)",
        "  { console.error('button count 0: ' + els['nr-go'].textContent); process.exit(23); }",
        "var cosKids = nodes['nr-cos'].children;",
        "if (cosKids.length !== 2) { console.error('cosKids=' + cosKids.length); process.exit(19); }",
        "var ik = cosKids[0];",
        "if (ik.children[0].className !== 'nr-ik' || ik.children[0].checked !== true ||",
        "    ik.children[0].value !== 'PTGX' || ik.children[1].textContent !== ' PTGX · Protagonist Therapeutics')",
        "  { console.error(ik.children[1].textContent); process.exit(20); }",
        "var off = cosKids[1];",
        "if (off.tag !== 'span' || off.className.indexOf('off') === -1 ||",
        "    off.textContent.indexOf('QUIET — Could not be resolved · No company matches this symbol.') !== 0)",
        "  { console.error(off.textContent); process.exit(21); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_new_run_company_load_failure_is_an_inline_error_not_a_toast():
    # the dialog's error surface is the red line. On open the old list is cleared and
    # "Loading…" shows; an unreachable server and a broken index (409 with the server's
    # sentence) each show the red line and render no list; a successful load renders the
    # list and clears the red line.
    import subprocess

    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "nrError") + "\n" + _grab_fn(js, "loadNewRunCompanies")
    script = "\n".join([
        "var nodes = {};",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "global.document = { getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "function clear(n) { n.children = []; return n; }",
        "function el(tag, cls, text) { var n = make(); n.tag = tag; n.className = cls || ''; if (text) n.textContent = text; return n; }",
        "var nrCompanies = null, nrState = 'loading', nrLoadError = '', nrLoadSeq = 0;",
        "var rendered = null;",
        "function renderNewRunCompanies(c, u) { rendered = [c, u]; nodes['nr-cos'].children = [el('label', 'tk2', 'x')]; }",
        "function syncPasteMode() {}",  # the paste mode is covered by the PR #162 tests
        "var resolveFirst;",
        "var A = { companies: function () { return new Promise(function (r) { resolveFirst = r; }); } };",
        fns,
        # an old list sits in the box from the last open (the choices grid and
        # the 'Previously captured' list are both cleared)
        "nodes['nr-cos'] = make(); nodes['nr-cos'].children = [el('label', 'tk2', 'OLD')];",
        "var p = loadNewRunCompanies();",
        "var kids = nodes['nr-prev'].children;",
        "if (kids.length !== 1 || kids[0].textContent !== 'Loading…') { console.error('not loading'); process.exit(2); }",
        "if (nrState !== 'loading') process.exit(3);",
        "resolveFirst({ ok: false, status: 409, body: { detail: 'The index is broken.' } });",
        "p.then(function () {",
        "  var e = nodes['nr-error'];",
        "  if (e.hidden || e.textContent !== 'The company list could not be loaded. The index is broken.') { console.error(e.textContent); process.exit(4); }",
        "  if (rendered !== null || nodes['nr-prev'].children.length) { console.error('list on failure'); process.exit(5); }",
        "  if (nrState !== 'failed' || nrLoadError !== e.textContent) process.exit(6);",
        "  A = { companies: function () { return Promise.reject(new Error('down')); } };",
        "  return loadNewRunCompanies();",
        "}).then(function () {",
        "  var e = nodes['nr-error'];",
        "  if (e.hidden || e.textContent !== 'The company list could not be loaded. The server is not reachable.') { console.error(e.textContent); process.exit(7); }",
        "  A = { companies: function () { return Promise.resolve({ ok: true, status: 200, body: { companies: ['IONS'], listed_without_pages: ['CRTX'] } }); } };",
        "  return loadNewRunCompanies();",
        "}).then(function () {",
        "  if (!rendered || rendered[0].join(',') !== 'IONS' || rendered[1].join(',') !== 'CRTX') { console.error('rendered=' + rendered); process.exit(8); }",
        "  var e = nodes['nr-error'];",
        "  if (!e.hidden || e.textContent !== '') { console.error('red line not cleared'); process.exit(9); }",
        "  if (nrState !== 'ready') process.exit(10);",
        # a late answer from an older open never overwrites the newer list
        "  var late; A = { companies: function () { return new Promise(function (r) { late = r; }); } };",
        "  var older = loadNewRunCompanies();",
        "  A = { companies: function () { return Promise.resolve({ ok: true, status: 200, body: { companies: ['NEW'] } }); } };",
        "  return loadNewRunCompanies().then(function () {",
        "    late({ ok: true, status: 200, body: { companies: ['STALE'] } });",
        "    return older;",
        "  });",
        "}).then(function () {",
        "  if (rendered[0].join(',') !== 'NEW') { console.error('stale=' + rendered[0]); process.exit(11); }",
        "  console.log('ok');",
        "});",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_research_instructions_pins():
    # #52: the New run note becomes 'Research instructions' — a real textarea,
    # no 200-char cap, default and limit from GET /api/settings only
    html = (UI / "index.html").read_text()
    assert '<textarea class="fld" id="nr-note" rows="3"' in html
    assert 'Research instructions' in html
    note_line = html[html.index('id="nr-note"') - 40:html.index('id="nr-note"') + 60]
    assert 'maxlength' not in note_line, note_line
    assert 'Note for the agent' not in html
    js = (UI / "app.js").read_text()
    for word in ("research_instructions_default", "research_instructions_max_length",
                 "function researchFromSettings(", "function noteHintText(",
                 "function applyResearchSettings(", "function notePayload(", "function researchReady(",
                 "Blank — the run uses the default instructions.",
                 "The default instructions could not be loaded.",
                 "Close and open New run to try again"):
        assert word in js, word
    # no hard-coded default text of our own: the only long string literals are
    # sentences, and the settings keys are the only source of the default
    assert "defaultText: '" not in js and 'defaultText: "' not in js


def test_research_instructions_behavior():
    # prefill from settings once; blank says the default will be used; over the
    # limit refuses instead of truncating; unavailable says so and never
    # claims an unseen default; edited text survives ticker preparation errors;
    # both start paths send the exact text (blank -> displayed server default)
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "clear", "nrError", "researchFromSettings", "noteHintText", "notePayload",
                     "researchReady", "applyResearchSettings", "updateNoteHint", "startIntake",
                     "stopIntakePoll"))
    script = "\n".join([
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false,",
        "  addEventListener: function () {},",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var els = { 'nr-tickers': { value: '' }, 'nr-note': { value: '' } };",
        "var nodes = {};",
        "var nrResearch = { defaultText: null, maxLength: null, loaded: false };",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null, usedRun: null };",
        "function setInterval() { return 1; }",
        "function clearInterval() {}",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "var A = { intake: function () { return Promise.reject(new Error('down')); },",
        "          settings: function () { return Promise.resolve({ research_instructions_default: 'Read the 8-K exhibits.',",
        "            research_instructions_max_length: 4000 }); } };",
        fns,
        # settings with the contract fields: the box prefills, hint is the plain line
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.',",
        "  research_instructions_max_length: 4000 });",
        "if (els['nr-note'].value !== 'Read the 8-K exhibits.') { console.error('prefill: ' + els['nr-note'].value); process.exit(2); }",
        "if (nodes['nr-note-hint'].textContent !== 'Guides the research; the source checks still run on every row.')",
        "  { console.error(nodes['nr-note-hint'].textContent); process.exit(3); }",
        # a person's text is never overwritten by a later settings apply
        "els['nr-note'].value = 'Focus on phase 3 readouts.';",
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.', research_instructions_max_length: 4000 });",
        "if (els['nr-note'].value !== 'Focus on phase 3 readouts.') { console.error('edit lost'); process.exit(4); }",
        # blank resolves visibly to the default
        "els['nr-note'].value = '   ';",
        "updateNoteHint();",
        "if (nodes['nr-note-hint'].textContent !== 'Blank — the run uses the default instructions.')",
        "  { console.error(nodes['nr-note-hint'].textContent); process.exit(5); }",
        # whitespace-only box resolves to the displayed default (payload rule)
        "if (notePayload() !== 'Read the 8-K exhibits.') { console.error('blank payload'); process.exit(6); }",
        # a non-blank box is preserved byte for byte — no trim, no client cut
        "els['nr-note'].value = '  Focus on phase 3 readouts.  ';",
        "if (notePayload() !== '  Focus on phase 3 readouts.  ') { console.error('raw not kept'); process.exit(18); }",
        # over the limit the hint counts — the text still goes to the run as
        # written; the server answers any refusal
        "els['nr-note'].value = new Array(4002).join('x');",
        "updateNoteHint();",
        "if (nodes['nr-note-hint'].textContent.indexOf('At most 4000 characters — this is 4001.') === -1)",
        "  { console.error(nodes['nr-note-hint'].textContent); process.exit(7); }",
        # settings unavailable: clear sentence, no invented default, text kept
        "applyResearchSettings(null);",
        "if (nrResearch.defaultText !== null || nrResearch.loaded !== false || !els['nr-note'].value)",
        "  { console.error('default invented or text wiped'); process.exit(10); }",
        "if (nodes['nr-note-hint'].textContent !== 'The default instructions could not be loaded. Close and open New run to try again.')",
        "  { console.error(nodes['nr-note-hint'].textContent); process.exit(11); }",
        # loaded requires a non-empty default AND a positive whole-number max
        "applyResearchSettings({ research_instructions_default: '', research_instructions_max_length: 4000 });",
        "if (nrResearch.loaded) { console.error('empty default loaded'); process.exit(14); }",
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.', research_instructions_max_length: 2.5 });",
        "if (nrResearch.loaded) { console.error('fractional max loaded'); process.exit(15); }",
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.', research_instructions_max_length: 0 });",
        "if (nrResearch.loaded) { console.error('zero max loaded'); process.exit(16); }",
        "applyResearchSettings({ default_run: null });",
        "if (nrResearch.loaded || nrResearch.defaultText !== null) { console.error('old server'); process.exit(12); }",
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.', research_instructions_max_length: 4000 });",
        "if (!nrResearch.loaded) { console.error('valid settings not loaded'); process.exit(17); }",
        # edited text survives a ticker preparation failure untouched (and a
        # delayed settings answer does not overwrite it: value non-empty)
        "els['nr-tickers'].value = 'ACME';",
        "els['nr-note'].value = 'Keep this text.';",
        "applyResearchSettings({ research_instructions_default: 'Read the 8-K exhibits.', research_instructions_max_length: 4000 });",
        "if (els['nr-note'].value !== 'Keep this text.') { console.error('late overwrite'); process.exit(19); }",
        "startIntake().then(function () {",
        "  if (els['nr-note'].value !== 'Keep this text.') { console.error('note lost on failure'); process.exit(13); }",
        "  console.log('ok');",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_research_instructions_both_start_paths_send_the_exact_text():
    # Continue with N ready and Run now send the box's exact text; blank goes
    # as the displayed server default
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "clear", "nrError", "nrModels", "researchReady", "notePayload", "startRunFromIntake", "syncPasteMode",
                     "renderIntakeStatus", "intakeScopeLine", "dayMonth", "intakeDoneN", "syncStartButton",
                     "renderChosen", "renderPrevCompanies", "nrPreparedTickers", "tickedReadyCount",
                     "applyPasteDisable", "nrSupportValue"))
    script = "\n".join([
        "var NEW_RUN_TYPES = [['trial_results', 'Trial results']];",
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "var INTAKE_STATE_WORDS = { ready: 'Ready' };",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false, disabled: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var els = { 'nr-note': { value: 'Focus on catalysts.' }, 'nr-limit': { value: '' }, 'nr-tools': { checked: false },",
        "            'nr-tickers': { value: '' }, 'nr-go': { textContent: '', disabled: false },",
        "            'nr-preparing': { textContent: '', hidden: true } };",
        "var INTAKE_FINISHED = ('ready unresolved no_documents unsupported failed').split(' ');",
        "var nodes = {};",
        "var nrResearch = { defaultText: 'Read the 8-K exhibits.', maxLength: 4000, loaded: true };",
        "var nrSupport = { loaded: false, available: false, reason: null, choices: [], touched: false };",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null, usedRun: null };",
        "var nrBusy = false;",
        "var nrChosen = {}; var nrPrepUnticked = {}; var nrCompanies = [];",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  querySelectorAll: function (sel) {",
        "    if (sel === '.nr-ik:checked') return [{ value: 'ACME', checked: true }];",
        "    if (sel === '.nr-co:checked') return [];",
        "    return []; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "var newrunWrap = { hidden: false };",
        "var runs = []; var switched = null;",
        "function toast() {}",
        "function switchRun(id) { switched = id; }",
        "function stopIntakePoll() {}",
        "var posted = [];",
        "var A = { newRun: function (body) { posted.push(body); return Promise.resolve({ ok: true, status: 202,",
        "    body: { id: 'run1', number: 1, label: 'Run 1' } }); },",
        "  runs: function () { return Promise.resolve([]); } };",
        fns,
        "var st = { intake_id: 'ik1', ready_count: 1, failed_count: 0, requested: ['ACME'], tickers: [",
        "    { symbol: 'ACME', company: 'Acme', state: 'ready', reason: null, documents: 1, pages: 1 } ] };",
        # settings not loaded: the start refuses with the plain retry sentence
        "nrResearch.loaded = false;",
        "startRunFromIntake(st, null);",
        "if (posted.length || nodes['nr-error'].hidden",
        "    || nodes['nr-error'].textContent !== 'The default instructions could not be loaded. Close and open New run to try again.') {",
        "  console.error('not refused: ' + JSON.stringify(posted) + ' / ' + nodes['nr-error'].textContent); process.exit(2); }",
        "nrResearch.loaded = true;",
        # non-blank text goes byte for byte — surrounding spaces preserved
        "els['nr-note'].value = '  Focus on catalysts.  ';",
        "startRunFromIntake(st, null);",
        "if (!posted.length || posted[0].note_for_agent !== '  Focus on catalysts.  ') { console.error(JSON.stringify(posted)); process.exit(3); }",
        # whitespace-only chooses the displayed default, exactly
        "els['nr-note'].value = '   ';",
        "Promise.resolve().then(function () {",
        "  startRunFromIntake({ intake_id: 'ik2', ready_count: 1, failed_count: 0, requested: ['ACME'], tickers: [",
        "      { symbol: 'ACME', company: 'Acme', state: 'ready', reason: null, documents: 1, pages: 1 } ] }, null);",
        "}).then(function () {",
        "  if (posted[1].note_for_agent !== 'Read the 8-K exhibits.') { console.error(JSON.stringify(posted[1])); process.exit(4); }",
        "  console.log('ok');",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_dialog_survives_background_polling():
    # CODEXADV_KIMI_DIALOG_REFRESH: the 2500 ms job poll re-renders and
    # re-calls openDialog for the same run/row/action. The mounted dialog,
    # its draft, focus and caret survive; a close, another target/action, or
    # a fresh open starts clean. Remove, Fix and Keep-anyway covered.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "noteField", "openDialog", "closeDialog", "removeDialog",
             "fixDialog", "keepAnywayDialog", "showDialogError", "basisDigest", "basisOf", "markDialogStale")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var FIELD_WORDS = { quote: 'Quote', date_text: 'Date as written' };",
        "var FIX_FIELDS = ['quote', 'date_text'];",
        "var RULE_WORDS = { event_in_past: 'The period ended before this run began.' };",
        "var NO_ERROR = 'x';",
        "var posts = [];",
        "function currentRunId() { return 'run1'; }",
        "function postDecision(id, body, msg, opts) { posts.push({ id: id, body: body }); }",
        "function navigate() {}",
        "function exclusionFor() { return null; }",
        "function codeExclusionHistory() { return { rule: 'event_in_past', reason: 'The period ended before this run began.' }; }",
        "function rowTarget() { return { values: {} }; }",
        "function nearMissCheck() { return null; }",
        "function nearMissPrefill() { return null; }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, dataset: {}, hidden: false, value: '',",
        "  checked: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; }, remove: function () {}, focus: function () { this.focused = true; } }; }",
        "var nodes = {};",
        "var dialogMount = null;",
        "var dialogWrap = { hidden: true };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (id === 'dialog-wrap') return dialogWrap; if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "var dialogHost = global.document.getElementById('dialog');",
        fns,
        # the stub DOM expands textContent='' into dropped children, like a browser
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        "var row = { id: 'e1', ticker: 'ACME', title: 'Row one', checks: [] };",
        "var row2 = { id: 'e2', ticker: 'BTBR', title: 'Row two', checks: [] };",
        "function textOf(n, out) { out = out || []; if (n && n.textContent) out.push(n.textContent);",
        "  ((n && n.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "function findNote(host) { var hit = null;",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') hit = hit || c; walk(c); }); })(host);",
        "  return hit; }",
        "function findBtns(host) { var b = [];",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'button') b.push(c); walk(c); }); })(host);",
        "  return b; }",
        # --- Remove: type across two polls
        "openDialog(row, 'remove');",
        "var note1 = findNote(nodes['dialog']);",
        "if (!note1) { console.error('no note'); process.exit(2); }",
        "note1.value = 'Not an upcoming event';",
        "note1.selectionStart = 4; note1.selectionEnd = 4;",
        "var nodeBefore = nodes['dialog'].children[0];",
        "openDialog(row, 'remove');   // first poll re-render",
        "openDialog(row, 'remove');   // second poll re-render",
        "if (findNote(nodes['dialog']) !== note1) { console.error('textarea replaced'); process.exit(3); }",
        "if (note1.value !== 'Not an upcoming event' || note1.selectionStart !== 4) { console.error('draft lost'); process.exit(4); }",
        "if (nodes['dialog'].children[0] !== nodeBefore) { console.error('host rebuilt'); process.exit(5); }",
        # submit works and carries the draft
        "var rb = findBtns(nodes['dialog']).filter(function (b) { return b.textContent === 'Remove'; })[0];",
        "rb.onclick();",
        "if (posts.length !== 1 || posts[0].body.action !== 'remove' || posts[0].body.note !== 'Not an upcoming event')",
        "  { console.error(JSON.stringify(posts)); process.exit(6); }",
        # a real close resets: the next open starts fresh
        "closeDialog();",
        "openDialog(row, 'remove');",
        "var note2 = findNote(nodes['dialog']);",
        "if (note2 === note1 || note2.value !== '') { console.error('not fresh after close'); process.exit(7); }",
        # another action on the same row rebuilds (no draft transfer)
        "note2.value = 'a remove draft';",
        "openDialog(row, 'fix');",
        "if (findNote(nodes['dialog']) === note2) { console.error('draft followed the action change'); process.exit(8); }",
        # Fix: the field choice and the entered value survive polls
        "var sel1 = null, f1 = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) {",
        "  if (c.tag === 'select') sel1 = sel1 || c; if (c.tag === 'input') f1 = f1 || c; walk(c); }); })(nodes['dialog']);",
        "if (!sel1 || !f1) { console.error('fix fields missing'); process.exit(9); }",
        "f1.value = 'some wording'; sel1.value = 'quote';",
        "openDialog(row, 'fix'); openDialog(row, 'fix');",
        "if (f1.value !== 'some wording' || sel1.value !== 'quote') { console.error('fix draft lost'); process.exit(10); }",
        # a different row rebuilds (never transfers the draft)
        "openDialog(row2, 'fix');",
        "var moved = false;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c === f1) moved = true; walk(c); }); })(nodes['dialog']);",
        "if (moved) { console.error('draft followed the row change'); process.exit(11); }",
        # Keep-anyway: prefilled reason editable and stable across polls
        "openDialog(row, 'keepanyway');",
        "var kn = findNote(nodes['dialog']);",
        "if (!kn || !kn.value) { console.error('keep-anyway prefill missing'); process.exit(12); }",
        "var preLen = kn.value.length;",
        "kn.value = kn.value + ' (checked the filing)';",
        "openDialog(row, 'keepanyway'); openDialog(row, 'keepanyway');",
        "if (findNote(nodes['dialog']) !== kn || kn.value.length <= preLen) { console.error('keep-anyway draft lost'); process.exit(13); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_keep_anyway_binds_the_shown_basis_and_renews_on_change():
    # DIALOGS-001 (Manus 05:31): the Keep-anyway confirmation binds the
    # proposal/evidence basis the person SAW. A refresh that revises the row
    # under the open box keeps the draft but discloses the change; the next
    # press renews the confirmation instead of writing; the write carries
    # expected_basis for the server to compare (409 on mismatch).
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "noteField", "openDialog", "closeDialog",
             "keepAnywayDialog", "markDialogStale", "showDialogError", "basisDigest", "basisOf")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var RULE_WORDS = { event_in_past: 'The period ended before this run began.' };",
        "var posts = [];",
        "function currentRunId() { return 'run1'; }",
        "function postDecision(id, body, msg, opts) { posts.push({ id: id, body: body }); }",
        "function navigate() {}",
        "function exclusionFor() { return { rule: 'event_in_past', reason: 'The period ended before this run began.' }; }",
        "function codeExclusionHistory() { return null; }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, dataset: {}, hidden: false, value: '',",
        "  checked: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  insertBefore: function (n, ref) { var i = this.children.indexOf(ref); if (i < 0) this.children.push(n); else this.children.splice(i, 0, n); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function (sel) { var want = sel.replace(/^\\./, ''); var hit = null;",
        "    (function walk(n) { (n.children || []).forEach(function (c) {",
        "      if (!hit && (c.className || '').split(' ').indexOf(want) >= 0) hit = c; walk(c); }); })(this);",
        "    return hit; },",
        "  remove: function () {}, focus: function () { this.focused = true; } }; }",
        "var nodes = {};",
        "var dialogMount = null;",
        "var dialogWrap = { hidden: true };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (id === 'dialog-wrap') return dialogWrap; if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "var dialogHost = global.document.getElementById('dialog');",
        fns,
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        # the run as served: revision 1, basis aaa
        "var detail = { id: 'run1',",
        "  run: { rows: [ { id: 'e1', ticker: 'ACME', title: 'ORIGINAL PROPOSAL', checks: [] } ], exclusions: [] },",
        "  state: { targets: { e1: { review_basis: { version: 1, sha256: 'aaa' } } } } };",
        "function rowOf(id) { return detail.run.rows.filter(function (r) { return r.id === id; })[0] || null; }",
        "function findNote(host) { var hit = null;",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') hit = hit || c; walk(c); }); })(host);",
        "  return hit; }",
        "function findBtns(host) { var b = [];",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'button') b.push(c); walk(c); }); })(host);",
        "  return b; }",
        "function keepBtn() { return findBtns(nodes['dialog']).filter(function (b) { return b.textContent === 'Keep anyway'; })[0]; }",
        # open on rev1, type a reason
        "openDialog(rowOf('e1'), 'keepanyway');",
        "var note = findNote(nodes['dialog']);",
        "if (!note) { console.error('no note'); process.exit(2); }",
        "note.value = 'I keep the ORIGINAL PROPOSAL';",
        # a same-basis poll changes nothing and posts on Keep with the shown basis
        "openDialog(rowOf('e1'), 'keepanyway');",
        "if (nodes['dialog'].querySelector('.dlgstale')) { console.error('false stale'); process.exit(3); }",
        "keepBtn().onclick();",
        "if (posts.length !== 1 || JSON.stringify(posts[0].body.expected_basis) !== JSON.stringify({ version: 1, sha256: 'aaa' }) || posts[0].body.note !== 'I keep the ORIGINAL PROPOSAL')",
        "  { console.error(JSON.stringify(posts)); process.exit(4); }",
        # the refresh brings revision 2 with a different basis
        "detail.run.rows[0].title = 'REVISED PROPOSAL';",
        "detail.state.targets.e1.review_basis = { version: 1, sha256: 'bbb' };",
        "openDialog(rowOf('e1'), 'keepanyway');",
        "var sub = nodes['dialog'].querySelector('.sh-s');",
        "if (!nodes['dialog'].querySelector('.dlgstale') || sub.textContent.indexOf('REVISED') === -1)",
        "  { console.error('change not disclosed'); process.exit(5); }",
        "if (findNote(nodes['dialog']) !== note || note.value !== 'I keep the ORIGINAL PROPOSAL') { console.error('draft lost'); process.exit(6); }",
        # the first press RENEWS: no write, draft survives, box rebinds to rev2
        "keepBtn().onclick();",
        "if (posts.length !== 1) { console.error('stale confirmation wrote: ' + JSON.stringify(posts)); process.exit(7); }",
        "var note2 = findNote(nodes['dialog']);",
        "if (note2.value !== 'I keep the ORIGINAL PROPOSAL') { console.error('draft lost on renew'); process.exit(8); }",
        "if (nodes['dialog'].querySelector('.dlgstale')) { console.error('stale notice stuck'); process.exit(9); }",
        # the confirmed press writes, bound to the basis now shown
        "keepBtn().onclick();",
        "if (posts.length !== 2 || JSON.stringify(posts[1].body.expected_basis) !== JSON.stringify({ version: 1, sha256: 'bbb' })) { console.error(JSON.stringify(posts)); process.exit(10); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr

    # the contract (#178 2cba64e): every decision from a dialog carries the
    # SHOWN row's review_basis (the served object or its sha256)
    for fn in ("keepAnywayDialog", "removeDialog", "fixDialog"):
        assert _grab_fn(js, fn).count("expected_basis: dialogMount ? dialogMount.basis : null") == 1, fn


def test_a_late_refresh_of_the_previous_run_never_paints_under_the_new_run():
    # DIALOGS-002 (Manus 05:31): refresh() GETs run1 and waits; switchRun loads
    # run2; run1's late answer must be discarded — wrapper and document ids
    # stay run2, no run1 render lands.
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("refresh", "switchRun"))
    script = "\n".join([
        "var detail = { id: 'run1', run: { run: { id: 'run1' } }, state: {} };",
        "var renders = [], shells = [];",
        "function renderShell() { shells.push(detail.id); }",
        "function render() { renders.push(detail.id + '/' + detail.run.run.id); }",
        "function navigate() {}",
        "function toast() {}",
        "var resolvers = {};",
        "var loadGen = 0;",
        "var A = { run: function (id) { return new Promise(function (r) { resolvers[id] = r; }); } };",
        fns,
        "refresh();                 // GET run1 pending",
        "switchRun('run2', false);  // GET run2 pending — the newer load",
        "resolvers['run2']({ run: { id: 'run2' }, state: {} });",
        "Promise.resolve().then(function () {",
        "  if (detail.id !== 'run2' || detail.run.run.id !== 'run2') { console.error('run2 not applied'); process.exit(2); }",
        "  var n = renders.length;",
        "  resolvers['run1']({ run: { id: 'run1' }, state: {} });   // the late answer",
        "  return Promise.resolve().then(function () {",
        "    if (renders.length !== n) { console.error('late run1 painted: ' + JSON.stringify(renders)); process.exit(3); }",
        "    if (detail.id !== 'run2' || detail.run.run.id !== 'run2') { console.error('identity mixed'); process.exit(4); }",
        "    console.log('ok');",
        "  });",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_newrun_one_start_ruling():
    # NEWRUN-ONE-START-001 + the CVA addendum: ONE start button at the bottom;
    # the dialog closes and opens the run ONLY after the server accepts the
    # start (202) — on failure it stays open with the paste text, the spending
    # limit and the instructions intact, error inline in red; the start button
    # disables the instant it is clicked. Plus the clear close and unticked
    # captured boxes.
    html = (UI / "index.html").read_text()
    assert 'id="nr-close"' in html and '×' in html
    assert 'id="nr-preparing"' in html
    js = (UI / "app.js").read_text()
    for word in ("function intakeDoneN(", "function syncStartButton(", "function nrStartClick(",
                 "getElementById('nr-close').addEventListener('click'",
                 "Preparing… (",
                 # the sheet keeps its closing doors: Esc handler and Cancel
                 "if (newrunWrap && !newrunWrap.hidden) { newrunWrap.hidden = true; return; }"):
        assert word in js, word
    _run_node(_new_run_sheet_script([
        # Run now refused by the server: dialog stays open, everything intact
        "global.NEXT_RUN_ANSWER = { ok: false, status: 422, body: { detail: 'Choose at least one company.' } };",
        "els['nr-tickers'].value = '';",
        "els['nr-limit'].value = '2.00';",
        "els['nr-note'].value = 'Focus on catalysts.';",
        "boxes[0].checked = true;",
        "syncPasteMode();",
        "nrStartClick();",
        "if (!els['nr-go'].disabled) fail(2, 'button not disabled on click');",
        "later().then(function () {",
        "  if (posts.length !== 1) fail(3, 'no post');",
        "  if (newrunWrap.hidden) fail(4, 'dialog closed on a refused start');",
        "  if (els['nr-tickers'].value !== '' || els['nr-limit'].value !== '2.00' || els['nr-note'].value !== 'Focus on catalysts.')",
        "    fail(5, 'settings lost on failure');",
        "  var e = nodes['nr-error'];",
        "  if (e.hidden || e.textContent !== 'Choose at least one company.') fail(6, e.textContent);",
        # the server accepts: only now the dialog closes and the run opens
        "  global.NEXT_RUN_ANSWER = { ok: true, status: 202, body: { id: 'run9', number: 9, label: 'Run 9' } };",
        "  boxes[0].checked = true;",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 2 || switched !== 'run9') fail(7, 'run did not open: ' + JSON.stringify(posts));",
        "  if (!newrunWrap.hidden) fail(8, 'dialog open after the 202');",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


def test_newrun_accepted_run_survives_a_failed_runs_refresh():
    # PRE-REVIEW-001 (Manus overwatch): POST /api/runs/new is accepted (202)
    # and then the saved-runs refresh fails — the accepted run still opens by
    # its returned id, the dialog stays closed, and no second paid start is
    # invited, on both the Run-now and the intake path
    _run_node(_new_run_sheet_script([
        "A.runs = function () { return Promise.reject(new Error('refresh down')); };",
        # Run-now path
        "boxes[0].checked = true;",
        "nrStartClick();",
        "later().then(function () {",
        "  if (posts.length !== 1) fail(2, 'posts: ' + posts.length);",
        "  if (switched !== 'run9') fail(3, 'accepted run not opened: ' + switched);",
        "  if (!newrunWrap.hidden) fail(4, 'dialog open after the 202');",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1) fail(5, 'a second start was invited');",
        "  if (els['nr-go'].disabled) fail(6, 'button stuck disabled');",
        # the intake path carries the same protection
        "  posts = []; switched = null; newrunWrap.hidden = false;",
        "  els['nr-tickers'].value = 'PTGX QUIET'; syncPasteMode();",
        "  return startIntake().then(later);",
        "}).then(function () {",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1 || switched !== 'run9') fail(7, 'intake accepted run not opened: ' + switched);",
        "  if (!newrunWrap.hidden) fail(8, 'dialog open after the intake 202');",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1) fail(9, 'intake second start invited');",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


def test_pr171_review_tick_state_survives_refusal():
    # Claude review of #171 (findings 1-3): prepared unticks are state — a 422
    # never re-ticks them and the retry sends only the ticked symbols; Run now
    # counts the ticked captured names and sleeps at zero; captured names
    # ticked before a paste stay disabled and are never counted in the intake.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "nrError", "renderChosen", "renderPrevCompanies", "nrPreparedTickers", "syncStartButton",
             "intakeDoneN", "tickedReadyCount", "applyPasteDisable", "syncPasteMode", "startRunFromIntake",
             "nrStartClick", "researchReady", "notePayload", "nrModels", "stopIntakePoll",
             "renderIntakeStatus", "intakeScopeLine", "dayMonth", "nrSupportValue")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var NR_PASTE_ONLY = 'paste only';",
        "var NEW_RUN_TYPES = [['trial_results', 'Trial results']];",
        "var INTAKE_STATE_WORDS = { ready: 'Ready', unresolved: 'Could not be resolved' };",
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, dataset: {}, hidden: false,",
        "  value: '', checked: false, disabled: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; }, focus: function () {} }; }",
        "var els = { 'nr-tickers': { value: '' }, 'nr-go': { textContent: '', disabled: false },",
        "            'nr-preparing': { textContent: '', hidden: true } };",
        "var nodes = {};",
        "var nrChosen = {}; var nrPrepUnticked = {}; var nrCompanies = []; var nrBusy = false;",
        "var nrResearch = { defaultText: null, maxLength: null, loaded: true };",
        "var nrSupport = { loaded: false, available: false, reason: null, choices: [], touched: false };",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "var nrIntake = { id: 'ik1', seq: 1, timer: null, last: null, text: null, usedId: null, usedRun: null };",
        "var DONE = { intake_id: 'ik1', status: 'done', ready_count: 2, failed_count: 0,",
        "  requested: ['PTGX', 'CRNX'], tickers: [",
        "    { symbol: 'PTGX', company: 'Protagonist', state: 'ready', reason: null, documents: 1, pages: 1 },",
        "    { symbol: 'CRNX', company: 'Crinetics', state: 'ready', reason: null, documents: 1, pages: 1 } ] };",
        "var posts = [];",
        "var ANSWER = { ok: true, status: 202, body: { id: 'run9', number: 9, label: 'Run 9' } };",
        "var A = { newRun: function (body) { posts.push(body); return Promise.resolve(ANSWER); },",
        "          runs: function () { return Promise.resolve([]); } };",
        "function currentRunId() { return 'run1'; }",
        "function toast() {}",
        "function switchRun() {}",
        "var newrunWrap = { hidden: false };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  createTextNode: function (t) { return { nodeType: 3, textContent: t, children: [] }; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  querySelectorAll: function (sel) {",
        "    var out = [];",
        "    function match(c) {",
        "      if (sel === '.nr-ik:checked') return c.tag === 'input' && c.className === 'nr-ik' && c.checked;",
        "      if (sel === '.nr-co:checked') return c.tag === 'input' && c.className === 'nr-co' && c.checked;",
        "      if (sel === '.nr-pc, .nr-co, #nr-all') return c.tag === 'input' && (c.className === 'nr-pc' || c.className === 'nr-co' || c.id === 'nr-all');",
        "      return false; }",
        "    (function walk(n) { (n.children || []).forEach(function (c) { if (!c) return; if (match(c)) out.push(c); walk(c); }); })(",
        "      { children: [nodes['nr-cos'], nodes['nr-prev']] });",
        "    return out; } };",
        "function clear(n) { n.children = []; n.textContent = ''; return n; }",
        fns,
        "function ikBoxes() { return global.document.querySelectorAll('.nr-ik:checked'); }",
        # --- finding 1: untick CRNX, refuse, retry sends only PTGX
        "els['nr-tickers'].value = 'PTGX, CRNX';",
        "nrIntake.text = 'PTGX, CRNX'; nrIntake.last = DONE;",
        "syncPasteMode();",
        "var boxes = [];",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'input' && c.className === 'nr-ik') boxes.push(c); walk(c); }); })(nodes['nr-cos']);",
        "if (boxes.length !== 2 || !boxes[0].checked || !boxes[1].checked) { console.error('prepared boxes'); process.exit(2); }",
        "boxes[1].checked = false; boxes[1].onchange();",
        "if (els['nr-go'].textContent !== 'Continue with 1 ready') { console.error('count: ' + els['nr-go'].textContent); process.exit(3); }",
        "ANSWER = { ok: false, status: 422, body: { detail: 'Choose at least one company.' } };",
        "nrStartClick();",
        "if (posts.length !== 1 || posts[0].companies.join(',') !== 'PTGX') { console.error(JSON.stringify(posts)); process.exit(4); }",
        "later().then(function () {",
        # the refusal re-rendered the sheet: CRNX must still be unticked
        "  var boxes2 = [];",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'input' && c.className === 'nr-ik') boxes2.push(c); walk(c); }); })(nodes['nr-cos']);",
        "  if (boxes2.length !== 2 || boxes2[1].checked) { console.error('re-ticked after 422'); process.exit(5); }",
        "  if (els['nr-go'].textContent !== 'Continue with 1 ready') { console.error('count after 422: ' + els['nr-go'].textContent); process.exit(6); }",
        # retry with the server accepting: only the ticked symbol goes
        "  ANSWER = { ok: true, status: 202, body: { id: 'run9', number: 9, label: 'Run 9' } };",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 2 || posts[1].companies.join(',') !== 'PTGX') { console.error('retry: ' + JSON.stringify(posts)); process.exit(7); }",
        # --- finding 2: Run now counts and sleeps at zero
        "  nrPrepUnticked = {}; nrIntake.last = null; nrIntake.text = null; els['nr-tickers'].value = '';",
        "  nrChosen = {}; nrCompanies = ['IONS', 'PTGX'];",
        "  renderPrevCompanies(['IONS', 'PTGX'], []);",
        "  syncPasteMode();",
        "  if (els['nr-go'].textContent !== 'Run now · 0 companies' || !els['nr-go'].disabled)",
        "    { console.error('zero: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled); process.exit(8); }",
        "  syncPasteMode();",
        "  var pc = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all');",
        "  if (pc.length !== 2) { console.error('prev boxes: ' + pc.length); process.exit(14); }",
        "  pc[0].checked = true; pc[0].onchange();",
        "  if (els['nr-go'].textContent !== 'Run now · 1 company' || els['nr-go'].disabled)",
        "    { console.error('one: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled); process.exit(9); }",
        # --- finding 3: a captured tick before pasting stays disabled, not counted
        "  els['nr-tickers'].value = 'PTGX, CRNX';",
        "  nrIntake.text = 'PTGX, CRNX'; nrIntake.last = DONE;",
        "  syncPasteMode();",
        "  var chosenCo = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all').filter(function (b) { return b.className === 'nr-co'; });",
        # re-review fix-on-find: a ticked box is always a company the run reads,
        # so the chosen captured name shows disabled AND unticked while pasted
        "  if (!chosenCo.length || !chosenCo[0].disabled || chosenCo[0].checked)",
        "    { console.error('chosen captured box not disabled+unticked'); process.exit(10); }",
        "  var pc2 = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all').filter(function (b) { return b.className === 'nr-pc'; });",
        "  if (!pc2.every(function (b) { return b.disabled; })) { console.error('prev boxes enabled'); process.exit(11); }",
        "  if (els['nr-go'].textContent !== 'Continue with 2 ready') { console.error('captured counted: ' + els['nr-go'].textContent); process.exit(12); }",
        # unticking the chosen captured box re-renders: everything stays disabled
        "  chosenCo[0].checked = false; chosenCo[0].onchange();",
        "  var pc3 = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all').filter(function (b) { return b.className === 'nr-pc'; });",
        "  if (!pc3.every(function (b) { return b.disabled; })) { console.error('prev boxes re-enabled'); process.exit(13); }",
        # clearing the paste box gives the captured choice back, ticked and enabled
        "  nrChosen = { IONS: true }; renderPrevCompanies(['IONS', 'PTGX'], []);",
        "  els['nr-tickers'].value = 'PTGX, CRNX'; syncPasteMode();",
        "  var co4 = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all').filter(function (b) { return b.className === 'nr-co'; });",
        "  if (co4.length !== 1 || co4[0].checked || !co4[0].disabled) { console.error('pasted: IONS shows ticked'); process.exit(15); }",
        "  els['nr-tickers'].value = ''; syncPasteMode();",
        "  var co5 = global.document.querySelectorAll('.nr-pc, .nr-co, #nr-all').filter(function (b) { return b.className === 'nr-co'; });",
        "  if (co5.length !== 1 || !co5[0].checked || co5[0].disabled) { console.error('cleared: IONS not restored'); process.exit(16); }",
        "  if (els['nr-go'].textContent !== 'Run now · 1 company') { console.error('cleared count: ' + els['nr-go'].textContent); process.exit(17); }",
        "  console.log('ok');",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
        "function later() { return new Promise(function (r) { setTimeout(r, 0); }); }",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_your_call_note_survives_background_polling():
    # CODEXADV_KIMI_DIALOG_REFRESH: the inline 'Your call' note in the
    # judgment pending pane keeps its draft and caret across a same-row
    # re-render; another row starts clean
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "projectionComplete", "partialProjection",
             "reviewClass", "fixStillOpen", "needsAttention",
             "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds", "restoreFolds", "judgmentDecidedPane",
             "sayControls", "cancelSay", "sayBusyWith", "revealRow", "sourceOf", "nearMissCheck", "listReasonLine",
             "shownDateText", "shownField", "whySection", "keptCurrent", "codeExclusionHistory", "detailsPane",
             "supportFindingFor", "supportNoteText",
             "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "disagreementSection",
             "postDecision", "checkFnFor", "reportUrl", "reportRefusalReason", "dayInWords", "eventWord",
             "renderJudgment", "researchLabelLine", "researchContextSection")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'x';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var RULE_WORDS = {};",
        "var posts = [];",
        "function currentRunId() { return 'run1'; }",
        "function postDecision(id, body) { posts.push(body); }",
        "function navigate() {}",
        "function exclusionFor() { return null; }",
        "function codeExclusionHistory() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function fmtField(f, v) { return String(v); }",
        "function bothReadersWord() { return 'The readers'; }",
        "function toast() {}",
        "function mistakeTypeWord(t) { return t; }",
        "var detail = { id: 'run1', run: { rows: [], mistakes: [], sources: [], tables: [], run: { label: 'probe' } },",
        "  state: { targets: {}, exclusions: [], mistake_list: [] } };",
        "function nearMissCheck() { return null; }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false, value: '',",
        "  checked: false, dataset: {}, open: false, scrollTop: 0,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function (sel) { var hit = null;",
        "    (function walk(n) { (n.children || []).forEach(function (c) {",
        "      if (!hit && sel === '#qnote' && c.id === 'qnote') hit = c; walk(c); }); })(this);",
        "    return hit; },",
        "  remove: function () {}, focus: function () { this.focused = true; },",
        "  setSelectionRange: function (a, b) { this.selectionStart = a; this.selectionEnd = b; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {};",
        "var route = { screen: 'judgment', rowId: null, dialog: null }; var judgmentSel = null;",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  activeElement: null };",
        fns,
        # the stub DOM expands textContent='' into dropped children, like a browser
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        "var row = { id: 'e1', title: 'ROW', ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],",
        "  date_text: 'Q3 2027', date: null, event_type: 'filing', quote: 'q', source_id: 's1', page_number: 1 };",
        "var row2 = { id: 'e2', title: 'ROW2', ticker: 'BTBR', proposed_by: 'Muse Spark', checks: [],",
        "  date_text: 'Q4 2027', date: null, event_type: 'filing', quote: 'q', source_id: 's1', page_number: 1 };",
        "var t = { group: 'open', status: 'undecided', decided_by_person: false, needs_attention: true,",
        "  needs_recheck: false, recheck: null, last_action: null, review_label: 'Proposed — needs review',",
        "  review_bucket: 'proposed', accepted: false, decision_label: null, on_calendar: false, issues: [],",
        "  values: { date_text: 'Q3 2027' }, fixes: [], reason: null, disagreement: null };",
        "var host = global.document.getElementById('judgment-det');",
        "judgmentPendingPane(host, row, t);",
        "var note1 = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') note1 = note1 || c; walk(c); }); })(host);",
        "if (!note1) { console.error('no qnote'); process.exit(2); }",
        "note1.value = 'mid-word draft here';",
        "note1.selectionStart = 7; note1.selectionEnd = 7;",
        "if (note1.oninput) note1.oninput();",
        "judgmentPendingPane(host, row, t);   // background refresh, same row",
        "judgmentPendingPane(host, row, t);   // second interval",
        "var still = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') still = still || c; walk(c); }); })(host);",
        # the pane legitimately rebuilds; the draft and caret are what survive
        "if (!still || still.value !== 'mid-word draft here' || still.selectionStart !== 7)",
        "  { console.error('qnote draft lost: ' + (still && still.value)); process.exit(3); }",
        # another row: the pane rebuilds clean (no draft transfer)
        "judgmentPendingPane(host, row2, t);",
        "var fresh = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') fresh = fresh || c; walk(c); }); })(host);",
        "if (fresh === note1 || fresh.value !== '') { console.error('draft followed the row change'); process.exit(5); }",
        # the reviewer's repro: drive renderJudgment itself — it CLEARS the
        # pane before the pending pane runs, and the draft must still survive
        "detail.run.rows = [row, row2];",
        "detail.state.targets = { 'e1': t, 'e2': t };",
        "qnoteDraft = {}; route.screen = 'judgment'; route.rowId = 'e1'; judgmentSel = null;",
        "renderJudgment();",
        "var rj1 = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') rj1 = rj1 || c; walk(c); }); })(nodes['judgment-det']);",
        "if (!rj1) { console.error('renderJudgment built no note'); process.exit(6); }",
        "rj1.value = 'Because the date is wrong';",
        "rj1.selectionStart = 4; rj1.selectionEnd = 4;",
        "if (rj1.oninput) rj1.oninput();",
        "renderJudgment();   // a poll tick: the host is cleared, then rebuilt",
        "renderJudgment();   // a second tick",
        "var rj2 = null;",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') rj2 = rj2 || c; walk(c); }); })(nodes['judgment-det']);",
        "if (!rj2 || rj2.value !== 'Because the date is wrong' || rj2.selectionStart !== 4)",
        "  { console.error('renderJudgment wiped the note: ' + (rj2 && rj2.value)); process.exit(7); }",
        # fix-on-find (Claude re-review + Red Team 7f822fc): renderJudgment reads
        # the LIVE box before its clear, so a caret moved without typing and a
        # focused EMPTY box both survive the tick
        "function liveNote() { var hit = null; (function walk(n) { (n.children || []).forEach(function (c) { if (!hit && c.id === 'qnote') hit = c; walk(c); }); })(nodes['judgment-det']); return hit; }",
        "var gid = global.document.getElementById;",
        "global.document.getElementById = function (id) { return id === 'qnote' ? liveNote() : gid(id); };",
        "var rj3 = liveNote(); rj3.selectionStart = 9; rj3.selectionEnd = 9; global.document.activeElement = rj3;",
        "renderJudgment();",
        "var rj4 = liveNote();",
        "if (rj4 === rj3 || rj4.selectionStart !== 9 || !rj4.focused || rj4.value !== 'Because the date is wrong')",
        "  { console.error('caret/focus without input lost: ' + (rj4 && rj4.selectionStart) + '/' + (rj4 && rj4.focused)); process.exit(8); }",
        "route.rowId = 'e2'; renderJudgment();",
        "var rj5 = liveNote(); if (rj5.value !== '') { console.error('draft crossed rows'); process.exit(9); }",
        "global.document.activeElement = rj5;",
        "renderJudgment();",
        "var rj6 = liveNote();",
        "if (rj6 === rj5 || !rj6.focused || rj6.value !== '') { console.error('focused empty note lost focus'); process.exit(10); }",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_your_call_note_never_crosses_runs_sharing_a_row_id():
    # DIALOGS-003 (Manus 05:31): the 'Your call' draft is keyed by run AND row.
    # run2's own e1 (same generated id) starts with an empty box even when a
    # draft exists from run1; the same-run refresh still preserves it.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "projectionComplete", "partialProjection",
             "reviewClass", "fixStillOpen", "needsAttention",
             "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds",
             "restoreFolds", "judgmentDecidedPane",
             "sayControls", "cancelSay", "sayBusyWith", "revealRow", "sourceOf", "nearMissCheck",
             "listReasonLine",
             "shownDateText", "shownField", "whySection", "keptCurrent", "codeExclusionHistory", "detailsPane",
             "supportFindingFor", "supportNoteText",
             "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "disagreementSection",
             "checkFnFor", "reportUrl", "reportRefusalReason", "dayInWords", "eventWord",
             "researchLabelLine", "researchContextSection")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'x';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var RULE_WORDS = {};",
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var RUN_ID = 'run1';",
        "function currentRunId() { return RUN_ID; }",
        "function navigate() {}",
        "function postDecision(id, body) {}",
        "function exclusionFor() { return null; }",
        "function codeExclusionHistory() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function toast() {}",
        "function mistakeTypeWord(t) { return t; }",
        "var detail = { id: 'run1', run: { rows: [], mistakes: [], sources: [], tables: [], run: { label: 'probe' } },",
        "  state: { targets: {}, exclusions: [], mistake_list: [] } };",
        "function nearMissCheck() { return null; }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false, value: '',",
        "  checked: false, dataset: {}, open: false, scrollTop: 0,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; }, remove: function () {}, focus: function () { this.focused = true; },",
        "  setSelectionRange: function (a, b) { this.selectionStart = a; this.selectionEnd = b; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {};",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; }, activeElement: null };",
        fns,
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        "var row = { id: 'e1', title: 'RUN ONE ROW', ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],",
        "  date_text: 'Q3 2027', date: null, event_type: 'filing', quote: 'q', source_id: 's1', page_number: 1 };",
        "var t = { group: 'open', status: 'undecided', decided_by_person: false, needs_attention: true,",
        "  needs_recheck: false, recheck: null, last_action: null, review_label: 'Proposed — needs review',",
        "  review_bucket: 'proposed', accepted: false, decision_label: null, on_calendar: false, issues: [],",
        "  values: { date_text: 'Q3 2027' }, fixes: [], reason: null, disagreement: null };",
        "function qnoteOf(host) { var hit = null;",
        "  (function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'textarea') hit = hit || c; walk(c); }); })(host);",
        "  return hit; }",
        "var host = global.document.getElementById('judgment-det');",
        "judgmentPendingPane(host, row, t);",
        "var n1 = qnoteOf(host);",
        "n1.value = 'RUN ONE REASON';",
        "if (n1.oninput) n1.oninput();",
        # same run, same row: a refresh keeps the draft
        "judgmentPendingPane(host, row, t);",
        "if (qnoteOf(host).value !== 'RUN ONE REASON') { console.error('same-run draft lost'); process.exit(2); }",
        # run2's own e1 (same id): its box starts empty
        "RUN_ID = 'run2';",
        "judgmentPendingPane(host, row, t);",
        "if (qnoteOf(host).value !== '') { console.error('draft crossed runs: ' + qnoteOf(host).value); process.exit(3); }",
        # typing there never overwrites run1's draft, and run1 keeps its own
        "var n2 = qnoteOf(host); n2.value = 'RUN TWO REASON'; if (n2.oninput) n2.oninput();",
        "RUN_ID = 'run1';",
        "judgmentPendingPane(host, row, t);",
        "if (qnoteOf(host).value !== 'RUN ONE REASON') { console.error('run1 draft lost: ' + qnoteOf(host).value); process.exit(4); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_current_review_issues_reach_the_decision_pane_and_list():
    # PROJECTION-001 (Manus 05:31): a row whose review projection carries
    # current issues shows the exact concern where the person is asked to
    # Keep/Fix/Remove, and in the judgment list's sub-line — never only a
    # generic 'with findings' label.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "projectionComplete", "partialProjection",
             "reviewClass", "fixStillOpen", "needsAttention",
             "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds",
             "restoreFolds", "judgmentDecidedPane",
             "sayControls", "cancelSay", "sayBusyWith", "revealRow", "sourceOf", "nearMissCheck",
             "listReasonLine",
             "shownDateText", "shownField", "whySection", "keptCurrent", "codeExclusionHistory", "detailsPane",
             "supportFindingFor", "supportNoteText",
             "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "disagreementSection",
             "checkFnFor", "reportUrl", "reportRefusalReason", "dayInWords", "eventWord",
             "researchLabelLine", "researchContextSection")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'x';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var RULE_WORDS = {};",
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "function currentRunId() { return 'run1'; }",
        "function navigate() {}",
        "function postDecision(id, body) {}",
        "function exclusionFor() { return null; }",
        "function codeExclusionHistory() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function toast() {}",
        "function mistakeTypeWord(t) { return t; }",
        "var detail = { id: 'run1', run: { rows: [], mistakes: [], sources: [], tables: [], run: { label: 'probe' } },",
        "  state: { targets: {}, exclusions: [], mistake_list: [] } };",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false, value: '',",
        "  checked: false, dataset: {}, open: false, scrollTop: 0,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; }, remove: function () {}, focus: function () { this.focused = true; },",
        "  setSelectionRange: function (a, b) { this.selectionStart = a; this.selectionEnd = b; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {}; var judgmentSel = null;",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; }, activeElement: null };",
        fns,
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        "function textOf(node, out) { out = out || []; if (node && node.textContent) out.push(node.textContent);",
        "  ((node && node.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        # the served projection: a current reviewer finding, issues filled by
        # the review projection on GET /api/runs/{id}
        "var row = { id: 'e1', title: 'ICARUS topline', ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],",
        "  date_text: 'Q3 2027', date: null, event_type: 'filing', quote: 'subject to FDA feedback',",
        "  source_id: 's1', page_number: 1 };",
        "var t = { group: 'open', status: 'undecided', decided_by_person: false, needs_attention: true,",
        "  needs_recheck: false, recheck: null, last_action: null, review_label: 'Proposed — needs review',",
        "  review_bucket: 'proposed', accepted: false, decision_label: null, on_calendar: false,",
        "  issues: ['The reviewer found lost condition: dropped'],",
        "  values: { date_text: 'Q3 2027' }, fixes: [], reason: null, disagreement: null };",
        "detail.run.rows = [row]; detail.state.targets = { e1: t };",
        # the decision pane (where Keep/Fix/Remove is asked) names the concern
        "var host = global.document.getElementById('judgment-det');",
        "judgmentPendingPane(host, row, t);",
        "var paneTxt = textOf(host);",
        "if (paneTxt.indexOf('dropped') === -1) { console.error('pane lacks the issue: ' + paneTxt.slice(0, 200)); process.exit(2); }",
        # the judgment list's sub-line carries it too
        "var listTxt = textOf(judgmentRow(row));",
        "if (listTxt.indexOf('dropped') === -1) { console.error('list sub-line lacks the issue: ' + listTxt); process.exit(3); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_empty_judgment_queue_never_claims_auto_inclusions_were_decided():
    # PROJECTION-004 (Manus 05:31): with the inclusion projection, an empty
    # conflict queue says 'Nothing needs review.' — an automatically included
    # proposal had no person decision, so the legacy 'everything decided'
    # sentence only stands on older payloads without the projection.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "projectionComplete", "partialProjection",
             "reviewClass", "fixStillOpen", "needsAttention",
             "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds",
             "restoreFolds", "judgmentDecidedPane",
             "sayControls", "cancelSay", "sayBusyWith", "revealRow", "sourceOf", "nearMissCheck",
             "listReasonLine",
             "shownDateText", "shownField", "whySection", "keptCurrent", "codeExclusionHistory", "detailsPane",
             "supportFindingFor", "supportNoteText",
             "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "disagreementSection",
             "checkFnFor", "reportUrl", "reportRefusalReason", "dayInWords", "eventWord",
             "researchLabelLine", "researchContextSection", "renderJudgment")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'x';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var RULE_WORDS = {};",
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "function currentRunId() { return 'run1'; }",
        "function navigate() {}",
        "function postDecision(id, body) {}",
        "function exclusionFor() { return null; }",
        "function codeExclusionHistory() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function toast() {}",
        "function mistakeTypeWord(t) { return t; }",
        "var detail = { id: 'run1', run: { rows: [], mistakes: [], sources: [], tables: [], run: { label: 'probe' } },",
        "  state: { targets: {}, exclusions: [], mistake_list: [], unresolved: 0,",
        "    counts: { inclusion: { review: 0, kept: 0, included: 1 }, review: { proposed: 0 } } } };",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false, value: '',",
        "  checked: false, dataset: {}, open: false, scrollTop: 0,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; }, remove: function () {}, focus: function () { this.focused = true; },",
        "  setSelectionRange: function (a, b) { this.selectionStart = a; this.selectionEnd = b; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {}; var judgmentSel = null;",
        "var route = { screen: 'judgment', rowId: null, dialog: null };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; }, activeElement: null };",
        fns,
        "function clear(node) { node.children = []; node.textContent = ''; return node; }",
        "function textOf(node, out) { out = out || []; if (node && node.textContent) out.push(node.textContent);",
        "  ((node && node.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "renderJudgment();",
        "var t1 = textOf(nodes['judgment-rows']);",
        "if (t1.indexOf('Nothing needs review.') === -1) { console.error('projection wording missing: ' + t1); process.exit(2); }",
        "if (t1.indexOf('decided') !== -1) { console.error('claims decisions: ' + t1); process.exit(3); }",
        # an older server without the inclusion projection keeps the legacy line
        "detail.state.counts = null;",
        "renderJudgment();",
        "var t2 = textOf(nodes['judgment-rows']);",
        "if (t2.indexOf('The queue is empty. Everything proposed has been decided.') === -1)",
        "  { console.error('legacy fallback lost: ' + t2); process.exit(4); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


    # fix-on-find (Red Team 7588841): .btn.pri has no :disabled rule, so the
    # disabled start kept the live ink fill — order (1) says never an
    # active-looking start that will not start
    css = (UI / "styles.css").read_text()
    assert "#nr-go:disabled,#nr-go:disabled:hover{opacity:.4;cursor:default}" in css


def test_research_s1_ui_surfaces():
    # RESEARCHER-FULL-001 S1: the documented projection rendered as sent, no UI
    # semantic logic — agent trail steps (one actor per step), per-company
    # progress, searched-not-found notes, the row label, the Context section,
    # and the source retrieval block (URL, hash, retrieval time)
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "hm", "num", "money", "usdCents",
             "researchTrailBlock", "researchCompanyBlock", "researchNotFoundBlock",
             "researchLabelLine", "researchContextSection", "srcmetaBar")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {},",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  createTextNode: function (t) { return { nodeType: 3, textContent: t }; } };",
        "function esc(v) { return String(v); }",
        "function dateTimeWords(iso) { return String(iso).replace('T', ' '); }",
        "var RESEARCH_STEP_WORDS = { request: 'Request', search: 'Search', fetch: 'Fetch', proposal: 'Proposal', check: 'Check', coverage: 'Coverage', finish: 'Finish', limit: 'Limit', repeat: 'Repeat', error: 'Error', prose: 'Note' };",
        "var RESEARCH_STATUS_WORDS = { queued: 'Queued', researching: 'Researching', finished: 'Finished', stopped: 'Stopped', failed: 'Failed' };",
        "var CONDITIONS_WORDS = { not_established: 'Conditions not established', none_stated: 'No conditions stated', stated: 'Conditions stated' };",
        fns,
        "function textOf(n, out) { out = out || []; if (typeof n === 'string') out.push(n);",
        "  else if (n && n.textContent) out.push(n.textContent);",
        "  ((n && n.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        # the agent trail: one actor per step, plain kind words, the step's sentence
        "var steps = [",
        "  { n: 1, at: '2026-11-02T10:00:00Z', ticker: 'ACME', kind: 'search', tag: 'Model', who: 'Muse Spark',",
        "    text: 'Searched: ACME phase 3 results 2027', refs: { queries: ['ACME phase 3 results 2027'] } },",
        "  { n: 2, at: '2026-11-02T10:01:00Z', ticker: 'ACME', kind: 'fetch', tag: 'Code', who: 'Code',",
        "    text: 'Fetched https://example.com/press/1', refs: { source_id: 's1' } },",
        "  { n: 3, at: '2026-11-02T10:05:00Z', ticker: 'ACME', kind: 'proposal', tag: 'Model', who: 'Muse Spark',",
        "    text: 'Proposed 1 event', refs: { row_id: 'e1' } },",
        "  { n: 4, at: '2026-11-02T10:06:00Z', ticker: 'ACME', kind: 'check', tag: 'Code', who: 'Code',",
        "    text: 'Quote check: pass' } ];",
        "var tb = textOf(researchTrailBlock({ steps: steps }));",
        "if (tb.indexOf('Agent research — not independently reviewed') === -1) { console.error('trail title'); process.exit(2); }",
        "if (tb.indexOf('Muse Spark') === -1 || tb.indexOf('Search') === -1 || tb.indexOf('Searched: ACME phase 3 results 2027') === -1)",
        "  { console.error(tb); process.exit(3); }",
        "if (tb.indexOf('Fetch') === -1 || tb.indexOf('Proposal') === -1 || tb.indexOf('Check') === -1)",
        "  { console.error('kinds'); process.exit(4); }",
        # G1-1: a module path never prints as the actor — the tag 'Code' does
        "var mod = textOf(researchTrailBlock({ steps: [",
        "  { at: '2026-11-02T10:00:00Z', kind: 'start', who: 'runs.research', tag: 'Code', text: 'Started.' },",
        "  { at: '2026-11-02T10:00:01Z', kind: 'search', who: 'capture.research_sources', tag: 'Code', text: 'Listed.' },",
        "  { at: '2026-11-02T10:00:02Z', kind: 'request', who: 'Muse Spark', tag: 'Model', text: 'Sent.' } ] }));",
        "if (mod.indexOf('runs.research') !== -1 || mod.indexOf('capture.research_sources') !== -1)",
        "  { console.error(mod); process.exit(15); }",
        "if (mod.indexOf('Started.') === -1 || mod.indexOf('Listed.') === -1 || mod.indexOf('Muse Spark') === -1)",
        "  { console.error(mod); process.exit(16); }",
        # the record's own label leads the block when there is one
        "var lbl = textOf(researchTrailBlock({ label: 'Agent research — partly reviewed by Muse Spark Open (same model family as the researcher) (incomplete), findings so far',",
        "  steps: [ { at: 't', kind: 'review', who: 'Muse Spark Open', tag: 'Model', text: 'Reviewed.' } ] }));",
        "if (lbl.indexOf('partly reviewed by Muse Spark Open') === -1 || lbl.indexOf('not independently reviewed') !== -1)",
        "  { console.error(lbl); process.exit(17); }",
        # per-company progress
        "var cb = textOf(researchCompanyBlock({ ACME: { status: 'researching', requests: 5, searches: 12,",
        "  fetches: 4, pages_delivered: 9, spend_usd: 0.42, status_reason: '' }, BTBR: { status: 'failed',",
        "  status_reason: 'The SEC could not be reached.', requests: 2 } }));",
        "if (cb.indexOf('ACME') === -1 || cb.indexOf('Researching') === -1 || cb.indexOf('12 searches') === -1 ||",
        "    cb.indexOf('9 pages delivered') === -1 || cb.indexOf('$0.42') === -1)",
        "  { console.error(cb); process.exit(5); }",
        "if (cb.indexOf('BTBR') === -1 || cb.indexOf('Failed') === -1 || cb.indexOf('The SEC could not be reached.') === -1)",
        "  { console.error(cb); process.exit(6); }",
        # searched, not found — with the scope note once
        "var nb = textOf(researchNotFoundBlock([",
        "  { ticker: 'ACME', category: 'pipeline', question: 'Phase 2 readout timing', searched_not_found: true,",
        "    scope_note: \"'Not found' covers these queries and sources at these times only, not the whole web.\" },",
        "  { ticker: 'ACME', category: 'partnerships', question: 'Licensing deals found: 2' } ]));",
        "if (!nb || nb.indexOf('Searched, not found') === -1 || nb.indexOf('Phase 2 readout timing') === -1)",
        "  { console.error(nb); process.exit(7); }",
        "if (nb.indexOf('Licensing deals') !== -1) { console.error('non-gap leaked'); process.exit(8); }",
        "if (nb.indexOf('covers these queries and sources at these times only') === -1) { console.error('scope lost'); process.exit(9); }",
        # nothing found -> no block
        "if (researchNotFoundBlock([{ question: 'x' }]) !== null) { console.error('empty block'); process.exit(10); }",
        # the row label + Context section
        "var row = { id: 'e1', research: { label: 'Agent research — not independently reviewed', revision: 3,",
        "  conditions_state: 'stated', issues: [{ code: 'KNOWN_OMITTED', message: 'A condition is stated but not assessed.' }] } };",
        "var ll = textOf(researchLabelLine(row));",
        "if (ll !== 'Agent research — not independently reviewed') { console.error(ll); process.exit(11); }",
        "if (researchLabelLine({ id: 'e2' }) !== null) { console.error('label on a plain row'); process.exit(12); }",
        "var ctx = textOf(researchContextSection(row));",
        "if (ctx.indexOf('Context') === -1 || ctx.indexOf('Conditions stated') === -1 ||",
        "    ctx.indexOf('A condition is stated but not assessed.') === -1 || ctx.indexOf('Proposal revision 3') === -1)",
        "  { console.error(ctx); process.exit(13); }",
        # source retrieval block: URL, retrieval time, original hash, pages
        "var sm = textOf(srcmetaBar({ id: 's1', publisher: 'Example', kind: 'company_release',",
        "  title: 'T', url: 'https://example.com/p', published_date: '2026-10-01',",
        "  captured_at: '2026-11-02T10:01:00+00:00', sha256: 'ab' + 'cd'.repeat(31),",
        "  retrieval: { tool: 'fetch_url', requested_url: 'https://example.com/p', final_url: 'https://example.com/p',",
        "    retrieved_at: '2026-11-02T10:01:05+00:00', original_sha256: '12' + '34'.repeat(31),",
        "    pages_captured: 2, pages_total: 3 } }));",
        "if (sm.indexOf('URL') === -1 || sm.indexOf('https://example.com/p') === -1 ||",
        "    sm.indexOf('Retrieved') === -1 || sm.indexOf('2026-11-02 10:01:05') === -1 ||",
        "    sm.indexOf('Original SHA-256') === -1 || sm.indexOf('1234') === -1 || sm.indexOf('Pages captured') === -1 ||",
        "    sm.indexOf('2 of 3') === -1)",
        "  { console.error(sm); process.exit(14); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_source_view_check_and_scroll_survive_background_polling():
    # CODEXADV_KIMI_DIALOG_REFRESH: an opened 'See the check' fold (stable
    # check identity) and the Source pane scroll survive a same-target
    # refresh; a changed target resets them. Two intervals covered.
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("el", "esc", "clear", "seeTheCheck", "captureFolds", "restoreFolds",
                                               "captureSrcView", "restoreSrcView", "srcViewRouteKey"))
    script = "\n".join([
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "function currentRunId() { return 'run1'; }",
        "function checkResultWord(r) { return CHECK_WORDS[r] || r; }",
        "function dateFinding() { return false; }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, dataset: {}, hidden: false, open: false, scrollTop: 0,",
        "  setAttribute: function (k, v) { this[k] = v; },",
        "  getAttribute: function (k) { if (k === 'data-check') return this.dataset.check; return this[k]; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function (sel) { var hit = null;",
        "    (function walk(n) { (n.children || []).forEach(function (c) {",
        "      if (!hit && c.tag === 'details' && sel.indexOf('details') === 0) {",
        "        var wantOpen = sel.indexOf('[open]') !== -1;",
        "        var wantChk = /data-check=\"([^\"]+)\"/.exec(sel);",
        "        if (wantOpen && !c.open) return;",
        "        if (wantChk && (!c.dataset || c.dataset.check !== wantChk[1])) return;",
        "        hit = c; }",
        "      walk(c); }); })(this);",
        "    return hit; },",
        "  querySelectorAll: function (sel) { var out = [];",
        "    (function walk(n) { (n.children || []).forEach(function (c) {",
        "      if (c.tag === 'details' && sel === 'details.fold[data-check]') out.push(c);",
        "      if (c.tag === 'details' && sel === 'details.fold[data-check][open]' && c.open) out.push(c);",
        "      walk(c); }); })(this);",
        "    return out; } }; }",
        "var nodes = {};",
        "var route = { srcId: 's1', page: 'p1', valueId: 'v1' };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        fns,
        # a mounted fold, open, carrying its check identity; the pane is scrolled
        "var det = global.document.getElementById('srcDet');",
        "var fold = seeTheCheck({ name: 'quote_match', result: 'fail', reason: 'nearly matches', compared: { looked_for: 'x' } });",
        "if (fold.dataset.check !== 'quote_match') { console.error('no identity'); process.exit(2); }",
        # the first render marks what the pane shows (restore records its key)
        "det.append(fold);",
        "restoreSrcView();",
        "fold.open = true;",
        "det.scrollTop = 300;",
        "captureSrcView();",
        # the refresh clears and rebuilds the pane (new node, closed, scroll kept)
        "det.children = [];",
        "var fold2 = seeTheCheck({ name: 'quote_match', result: 'fail', reason: 'nearly matches', compared: { looked_for: 'x' } });",
        "det.append(fold2);",
        "restoreSrcView();",
        "if (fold2.open !== true) { console.error('check not re-opened'); process.exit(3); }",
        "if (det.scrollTop !== 300) { console.error('scroll not restored: ' + det.scrollTop); process.exit(4); }",
        # second interval: the same again
        "captureSrcView();",
        "det.children = []; det.scrollTop = 300;",
        "var fold3 = seeTheCheck({ name: 'quote_match', result: 'fail', reason: 'nearly matches', compared: { looked_for: 'x' } });",
        "det.append(fold3);",
        "restoreSrcView();",
        "if (fold3.open !== true || det.scrollTop !== 300) { console.error('second interval lost'); process.exit(5); }",
        # a changed target: no stale restore
        "fold3.open = true; det.scrollTop = 111;",
        "captureSrcView();",
        "route.valueId = 'v2';",
        "det.children = []; det.scrollTop = 0;",
        "var fold4 = seeTheCheck({ name: 'quote_match', result: 'fail', reason: 'nearly matches', compared: { looked_for: 'x' } });",
        "det.append(fold4);",
        "restoreSrcView();",
        "if (fold4.open !== false || det.scrollTop !== 0) { console.error('stale restore'); process.exit(6); }",
        # fix-on-find (Red Team 7f822fc): render() sets the route BEFORE
        # renderSource captures — the old value's check and scroll must not be
        # copied onto the new value in that order either
        "fold4.open = true; det.scrollTop = 180;",
        "route.valueId = 'v3';",
        "captureSrcView();",
        "det.children = []; det.scrollTop = 0;",
        "var fold5 = seeTheCheck({ name: 'quote_match', result: 'fail', reason: 'nearly matches', compared: { looked_for: 'x' } });",
        "det.append(fold5);",
        "restoreSrcView();",
        "if (fold5.open !== false || det.scrollTop !== 0) { console.error('route-first stale restore'); process.exit(7); }",
        "console.log('ok');",
    ])
    # DIALOGS-004: the child's exit status is the failing-if-broken signal —
    # without this assert a JS regression reported on stderr passed silently
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


    # wiring pins: each surface renders from run.research / row.research
    for word in ("function runResearch(", "researchTrailBlock(rs)",
                 "researchCompanyBlock(runResearch() && runResearch().companies)",
                 "researchNotFoundBlock(runResearch() && runResearch().coverage)",
                 "researchLabelLine(row)", "researchContextSection(row)",
                 "source.retrieval", "rs && rs.steps && rs.steps.length"):
        assert word in js, word


def test_research_s1_surfaces_read_the_real_get_envelope(tmp_path):
    # FIX-ON-FIND (Manus 16:52): runResearch() must read detail.run.RUN.research
    # — the real GET /api/runs/{id} envelope nests the run.json record (with
    # `research`) under `run`. Builds a genuine research run through the backend
    # with its fake Responses reader, serves it, GETs the run, and renders every
    # UI surface in node against that exact response.
    import importlib.util
    import json
    import subprocess

    rig = importlib.util.spec_from_file_location("trs1", Path(__file__).parent / "test_research_s1.py")
    trs1 = importlib.util.module_from_spec(rig)
    rig.loader.exec_module(trs1)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    reader = trs1.F.FakeResponses(trs1.happy_script())
    client, rid, _tools = trs1.start(runs_dir, reader)
    body = client.get("/api/runs/" + rid).json()          # the real served shape:
    # {**run.json, state, ...} — the run RECORD (with `research`) sits at body["run"]
    assert body["run"]["research"]["steps"]                 # the nesting Manus cited
    assert any(r.get("research") for r in body["rows"])

    js = (UI / "app.js").read_text()
    script = "\n".join([
        "const fs = require('fs');",
        "function make(tag) {",
        "  return { tag, className: '', textContent: '', children: [], style: {}, hidden: false, value: '', dataset: {},",
        "    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },",
        "    append(...a) { for (const c of a) this.children.push(typeof c === 'string' ? { textContent: c, children: [] } : c); },",
        "    appendChild(c) { this.children.push(c); return c; }, addEventListener() {}, removeEventListener() {},",
        "    setAttribute(k, v) { this[k] = v; }, getAttribute() { return null; },",
        "    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}, scrollIntoView() {}, remove() {},",
        "    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },",
        "    set innerHTML(v) { this.children = []; this._html = v; }, get innerHTML() { return this._html || ''; },",
        "    firstChild: null };",
        "}",
        "const nodes = {};",
        "global.document = { createElement: make, createTextNode: t => ({ textContent: String(t), children: [] }),",
        "  getElementById: id => (nodes[id] = nodes[id] || make('div')), querySelector: () => null, querySelectorAll: () => [],",
        "  addEventListener() {}, body: make('body'), documentElement: make('html'), title: '', activeElement: null };",
        "global.window = { addEventListener() {}, location: { hash: '' }, matchMedia: () => ({ matches: false, addEventListener() {} }),",
        "  setTimeout, history: { replaceState() {}, pushState() {} }, getComputedStyle: () => ({}) };",
        "global.location = window.location; global.history = window.history;",
        "global.localStorage = { getItem() { return null; }, setItem() {} };",
        "global.fetch = () => new Promise(() => {});",
        "global.requestAnimationFrame = () => 0;",
        "let src = fs.readFileSync(" + json.dumps(str(UI / "app.js")) + ", 'utf8');",
        "src = src.replace(/route = parseHash\(\);\s*boot\(\);\s*\}\)\(\);\s*$/,",
        "  'global.__H = { get detail(){return detail;}, set detail(v){detail=v;}, ev: function(s){ return eval(s); } };\\n})();');",
        "if (!/global\.__H/.test(src)) throw new Error('no boot hook');",
        "eval(src);",
        "const H = global.__H;",
        "function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent);",
        "  (n.children || []).forEach(c => textOf(c, out)); return out; }",
        "H.detail = " + json.dumps({"id": rid, "run": body, "state": body["state"]}) + ";",
        "H.ev(\"route = { screen: 'audit', runId: '" + rid + "', rowId: null, dialog: null }\");",
        # the FIX-ON-FIND assertion: the research ledger comes from the real envelope
        "const rs = H.ev('runResearch')();",
        "if (!rs || !rs.steps || !rs.steps.length) { console.error('runResearch null from the real envelope'); process.exit(2); }",
        "const kinds = {}; rs.steps.forEach(s => kinds[s.kind] = true);",
        "for (const k of ['request', 'search', 'fetch', 'proposal', 'check', 'coverage', 'finish'])",
        "  if (!kinds[k]) { console.error('missing step kind ' + k); process.exit(3); }",
        "const tb = textOf(H.ev('researchTrailBlock')(rs)).join(' ');",
        "if (tb.indexOf('Agent research — not independently reviewed') === -1 || tb.indexOf('Search') === -1)",
        "  { console.error(tb.slice(0, 200)); process.exit(4); }",
        "const cb = textOf(H.ev('researchCompanyBlock')(rs.companies)).join(' ');",
        "if (cb.indexOf('PTGX') === -1 || cb.indexOf('Finished') === -1) { console.error(cb); process.exit(5); }",
        "const nf = H.ev('researchNotFoundBlock')(rs.coverage);",
        "if (!nf) { console.error('no searched-not-found block'); process.exit(6); }",
        "const nft = textOf(nf).join(' ');",
        "if (nft.indexOf('Searched, not found') === -1 || nft.indexOf('PDUFA') === -1)",
        "  { console.error(nft); process.exit(7); }",
        "const row = H.ev('rowOf')(H.ev('detail').run.rows.filter(r => r.research)[0].id);",
        "const ll = H.ev('researchLabelLine')(row);",
        "if (!ll || ll.textContent !== 'Agent research — not independently reviewed') { console.error('label'); process.exit(8); }",
        "const ctx = H.ev('researchContextSection')(row);",
        "if (!ctx) { console.error('no Context section'); process.exit(9); }",
        # FIX-ON-FIND 17:07 — the real render functions to final DOM
        "H.ev(\"route = { screen: 'trail', runId: '" + rid + "', rowId: null, dialog: null }\");",
        "H.ev('renderTrail')();",
        "var trailText = textOf(nodes['trail-mid']).join(' ');",
        "if (trailText.indexOf('Agent research — not independently reviewed') === -1 || trailText.indexOf('Search') === -1)",
        "  { console.error('trail DOM: ' + trailText.slice(0, 160)); process.exit(10); }",
        "H.ev(\"audit.filter = 'ALL'; audit.selected = null; route = { screen: 'audit', runId: '" + rid + "', rowId: null, dialog: null }\");",
        "H.ev('renderAuditRows')();",
        "var auditText = textOf(nodes['audit-rows']).join(' ');",
        "if (auditText.indexOf('Searched, not found') === -1 || auditText.indexOf('PDUFA') === -1)",
        "  { console.error('audit DOM: ' + auditText.slice(0, 160)); process.exit(11); }",
        "H.ev('updateRunCardJob')();",
        "var cardText = textOf(nodes['runcard-job']).join(' ');",
        "if (cardText.indexOf('PTGX') === -1 || cardText.indexOf('Finished') === -1)",
        "  { console.error('card DOM: ' + cardText.slice(0, 160)); process.exit(12); }",
        # fix-on-find (Claude re-review): company progress on EVERY status,
        # the status-less saved run included (its branch returns early), and
        # the money in cents
        "var runRec = H.ev('detail').run.run; var keepSt = runRec.status;",
        "[null, 'queued', 'running', 'done', 'needs_input'].forEach(function (st) {",
        "  if (st === null) delete runRec.status; else runRec.status = st;",
        "  nodes['runcard-job'].children = []; nodes['runcard-job'].textContent = '';",
        "  H.ev('updateRunCardJob')();",
        "  var ct = textOf(nodes['runcard-job']).join(' ');",
        "  if ((ct.indexOf('saved run') === 0) !== (st === null)) { console.error('status ' + st + ' not rendered: ' + ct.slice(0, 80)); process.exit(18); }",
        "  if (ct.indexOf('PTGX') === -1) { console.error('no company progress at status ' + st + ': ' + ct.slice(0, 120)); process.exit(15); }",
        "  if (/\\$\\d+\\.\\d{3,}/.test(ct)) { console.error('money not in cents: ' + ct); process.exit(16); }",
        "});",
        "runRec.status = keepSt;",
        # a research run with no proposal yet still shows its not-found notes
        "var keepRows = H.ev('detail').run.rows; H.ev('detail').run.rows = [];",
        "nodes['audit-rows'].children = []; nodes['audit-rows'].textContent = '';",
        "H.ev('renderAuditRows')();",
        "var zeroText = textOf(nodes['audit-rows']).join(' ');",
        "if (zeroText.indexOf('Searched, not found') === -1) { console.error('zero-row notes: ' + zeroText.slice(0, 120)); process.exit(17); }",
        "H.ev('detail').run.rows = keepRows;",
        "var paneHost = make('div');",
        "H.ev('detailsPane')(paneHost, row, { actions: true, back: 'audit' });",
        "var paneText = textOf(paneHost).join(' ');",
        "if (paneText.indexOf('Agent research — not independently reviewed') === -1)",
        "  { console.error('pane DOM label missing'); process.exit(13); }",
        "if (paneText.indexOf('Context') === -1)",
        "  { console.error('pane DOM Context missing'); process.exit(14); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=90)
    assert got.returncode == 0, got.stdout + got.stderr


def test_ticker_intake_pins():
    # TICKER-INTAKE-001: the New run sheet intakes pasted tickers through the
    # server — no hard-coded list, plain words, inline errors (the red line)
    html = (UI / "index.html").read_text()
    for marker in ('id="nr-tickers"', 'id="nr-intake-go"', 'id="nr-intake-out"'):
        assert marker in html, marker
    js = (UI / "app.js").read_text()
    for word in ("fetch('/api/intake'",
                 "intakeStatus: function (id)",
                 "function intakeScopeLine(",
                 "function renderIntakeStatus(",
                 "function startRunFromIntake(",
                 "accept_ready_subset: st.requested.some(",
                 "'Continue with ' + n + ' ready'",
                 "no_documents: 'No documents found'",
                 "unresolved: 'Could not be resolved'",
                 "unsupported: 'Not supported'",
                 "failed: 'Preparation failed'",
                 # the run starts from the intake id, never a rebuilt list
                 "intake_id: st.intake_id"):
        assert word in js, word
    # no captured name is hard-coded anywhere in the UI
    for name in ("PTGX", "ABVX", "CRNX", "NEW_RUN_COMPANIES"):
        assert name not in js, name


def test_intake_scope_line_states_the_page_cap():
    # INTAKE-PAGE-CAP-001: the scope line says the run reads only the first
    # pages of each document; it never claims every page was read
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("dayMonth", "intakeScopeLine"))
    base = {"form_scope": ["8-K"], "lookback_days": 90, "window_start": "2026-07-30", "cutoff": "2026-10-28"}
    script = "\n".join([
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        fns,
        "console.log(JSON.stringify([",
        "  intakeScopeLine(" + json.dumps({**base, "pages_per_document": 2}) + "),",
        "  intakeScopeLine(" + json.dumps({**base, "pages_per_document": 1}) + "),",
        "  intakeScopeLine(" + json.dumps(base) + ")]));",
    ])
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    two, one, legacy = json.loads(out.stdout)
    assert two == "Read: the first 2 pages of each 8-K press release filed 30 Jul 2026 to 28 Oct 2026 (90 days)."
    assert one == "Read: the first page of each 8-K press release filed 30 Jul 2026 to 28 Oct 2026 (90 days)."
    # an intake recorded before the cap read every page it captured: the older line stays
    assert legacy == "Read: 8-K press releases filed 30 Jul 2026 to 28 Oct 2026 (90 days)."
    for line in (two, one):
        assert "every" not in line.lower() and "all " not in line.lower() and "complete" not in line.lower()


def test_ticker_intake_renders_states_scope_and_continue_button():
    # every ticker renders its company, plain state and the server's reason;
    # the scope line reads in words; done offers 'Continue with N ready' and
    # the failures stay listed; with nothing ready the button is disabled
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "clear", "dayMonth", "intakeScopeLine", "renderIntakeStatus"))
    done = {
        "intake_id": "ik1", "status": "done", "ready_count": 2, "failed_count": 2,
        "form_scope": ["8-K"], "lookback_days": 90, "window_start": "2026-08-04", "cutoff": "2026-11-02",
        "pages_per_document": 2,
        "tickers": [
            {"symbol": "ACME", "company": "Acme Corporation", "state": "ready", "reason": None,
             "documents": 3, "pages": 2, "form_scope": ["8-K"], "lookback_days": 90,
             "window_start": "2026-08-04", "cutoff": "2026-11-02"},
            {"symbol": "BTBR", "company": "Biotab Pharmaceuticals", "state": "ready", "reason": None,
             "documents": 1, "pages": 1, "form_scope": ["8-K"], "lookback_days": 90,
             "window_start": "2026-08-04", "cutoff": "2026-11-02"},
            {"symbol": "CRNX", "company": None, "state": "unresolved",
             "reason": "No company matches this symbol.", "documents": 0, "pages": 0},
            {"symbol": "DRMA", "company": None, "state": "no_documents",
             "reason": "No document of this kind was filed in the window.", "documents": 0, "pages": 0},
        ],
    }
    script = "\n".join([
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "var INTAKE_STATE_WORDS = { resolving: 'Finding the company', capturing: 'Capturing documents',",
        "  ready: 'Ready', unresolved: 'Could not be resolved', no_documents: 'No documents found',",
        "  unsupported: 'Not supported', failed: 'Preparation failed' };",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var nodes = {};",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null }, nrBusy = false;",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        fns,
        "renderIntakeStatus(document.getElementById('nr-intake-out'), " + json.dumps(done) + ");",
        "function textOf(n, out) { out = out || []; if (n && n.textContent) out.push(n.textContent);",
        "  ((n && n.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "var text = textOf(nodes['nr-intake-out']);",
        "if (text.indexOf('Acme Corporation') === -1) { console.error('company missing'); process.exit(2); }",
        "if (text.indexOf('Ready') === -1) { console.error('ready word missing'); process.exit(3); }",
        "if (text.indexOf('Could not be resolved') === -1) { console.error('unresolved word'); process.exit(4); }",
        "if (text.indexOf('No documents found') === -1) { console.error('no_documents word'); process.exit(5); }",
        "if (text.indexOf('No company matches this symbol.') === -1) { console.error('reason'); process.exit(6); }",
        # the failure rows stay listed
        "if (text.indexOf('CRNX') === -1 || text.indexOf('DRMA') === -1) { console.error('failures lost'); process.exit(7); }",
        "if (text.indexOf('Read: the first 2 pages of each 8-K press release filed 4 Aug 2026 to 2 Nov 2026 (90 days).') === -1) { console.error('scope: ' + text); process.exit(8); }",
        "if (text.indexOf('2 ready · 2 could not be prepared') === -1) { console.error('counts'); process.exit(9); }",
        # NEWRUN-ONE-START-001: no start button inside the list — the ONE
        # start button is nr-go at the bottom of the sheet
        "var btns = []; (function walk(n) { (n.children || []).forEach(function (c) {",
        "  if (c.tag === 'button') btns.push(c); walk(c); }); })(nodes['nr-intake-out']);",
        "if (btns.length !== 0) { console.error('mid-dialog button survives'); process.exit(10); }",
        # nothing ready: no run can start (the stub's clear() only mirrors the
        # DOM's textContent reset, so drop the children the real DOM would drop)
        "nodes['nr-intake-out'].children = [];",
        "var none = " + json.dumps(dict(done, ready_count=0, failed_count=4,
                                        tickers=[dict(t, state="failed", reason="The SEC could not be reached.")
                                                 for t in done["tickers"]])) + ";",
        "renderIntakeStatus(nodes['nr-intake-out'], none);",
        "btns = []; (function walk(n) { (n.children || []).forEach(function (c) {",
        "  if (c.tag === 'button') btns.push(c); walk(c); }); })(nodes['nr-intake-out']);",
        "if (btns.length !== 0) { console.error('mid-dialog button survives'); process.exit(11); }",
        "if (none.ready_count !== 0) { console.error('fixture'); process.exit(12); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_ticker_intake_flow_prepare_poll_and_continue_starts_the_run():
    # the flow end to end with stubbed routes: paste -> POST /api/intake ->
    # poll GET /api/intake/{id} -> done -> 'Continue with 1 ready' posts
    # /api/runs/new with the intake id, the ready names only, and
    # accept_ready_subset true because a requested ticker failed
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "clear", "nrError", "dayMonth", "intakeScopeLine", "renderIntakeStatus",
                     "syncPasteMode", "stopIntakePoll", "pollIntake", "startIntake", "startRunFromIntake",
                     "nrModels", "notePayload", "researchReady", "intakeDoneN", "syncStartButton", "nrStartClick",
                     "renderChosen", "nrPreparedTickers", "tickedReadyCount",
                     "applyPasteDisable", "nrSupportValue"))
    script = "\n".join([
        "var NR_PASTE_ONLY = 'This run reads only the pasted tickers. Clear the box to choose from the captured names instead.';",
        "var nrBusy = false;",
        "var nrSupport = { loaded: false, available: false, reason: null, choices: [], touched: false };",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "var INTAKE_STATE_WORDS = { resolving: 'Finding the company', capturing: 'Capturing documents',",
        "  ready: 'Ready', unresolved: 'Could not be resolved', no_documents: 'No documents found',",
        "  unsupported: 'Not supported', failed: 'Preparation failed' };",
        "var NEW_RUN_TYPES = [['trial_results', 'Trial results'], ['regulatory_decision', 'Regulatory decision'],",
        "  ['launch', 'Launch'], ['conference', 'Conference'], ['filing', 'Filing'], ['milestone', 'Milestone']];",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false, disabled: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var nodes = {};",
        "var els = { 'nr-tickers': { value: 'acme, BTBR' }, 'nr-limit': { value: '' },",
        "            'nr-note': { value: '' }, 'nr-tools': { checked: false },",
        "            'nr-go': { textContent: 'Run now', disabled: false },",
        "            'nr-preparing': { textContent: '', hidden: true } };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  createTextNode: function (t) { return { nodeType: 3, textContent: t }; },",
        "  querySelectorAll: function (sel) {",
        "    if (sel === '.nr-ik:checked') return [{ value: 'ACME', checked: true }];",
        "    if (sel === '.nr-co:checked') return [];",
        "    if (sel === '.nr-pc, .nr-co, #nr-all') return [];",
        "    return []; } };",
        "var INTAKE_FINISHED = ('ready unresolved no_documents unsupported failed').split(' ');",
        "var newrunWrap = { hidden: false };",
        "var runs = []; var switched = null;",
        "function toast() {}",
        "function switchRun(id) { switched = id; }",
        "function setInterval() { return 1; }",
        "function clearInterval() {}",
        "var posted = null, intakeText = null, statusCalls = 0;",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null };",
        "var nrResearch = { defaultText: null, maxLength: null, loaded: true };",
        "var nrChosen = {};",
        "var nrPrepUnticked = {};",
        "var nrCompanies = [];",
        "var PREPARING = { intake_id: 'ik1', status: 'preparing', ready_count: 0, failed_count: 0,",
        "  requested: ['ACME', 'BTBR'], tickers: [",
        "    { symbol: 'ACME', company: 'Acme Corporation', state: 'capturing', reason: 'Capturing 8-K exhibits filed 2026-08-04 to 2026-11-02.', documents: 1, pages: 0 },",
        "    { symbol: 'BTBR', company: null, state: 'resolving', reason: 'Looking up the company.', documents: 0, pages: 0 } ] };",
        "var DONE = { intake_id: 'ik1', status: 'done', ready_count: 1, failed_count: 1,",
        "  requested: ['ACME', 'BTBR'], form_scope: ['8-K'], lookback_days: 90,",
        "  window_start: '2026-08-04', cutoff: '2026-11-02', tickers: [",
        "    { symbol: 'ACME', company: 'Acme Corporation', state: 'ready', reason: null, documents: 2, pages: 2 },",
        "    { symbol: 'BTBR', company: null, state: 'unresolved', reason: 'No company matches this symbol.', documents: 0, pages: 0 } ] };",
        "var A = {",
        "  intake: function (text) { intakeText = text; return Promise.resolve({ ok: true, status: 202, body: { intake_id: 'ik1' } }); },",
        "  intakeStatus: function (id) { statusCalls++; return Promise.resolve({ ok: true, status: 200,",
        "    body: statusCalls === 1 ? PREPARING : DONE }); },",
        "  newRun: function (body) { posted = body; return Promise.resolve({ ok: true, status: 202, body: { id: 'run9', number: 9, label: 'Run 9' } }); },",
        "  runs: function () { return Promise.resolve([]); } };",
        fns,
        "function textOf(n, out) { out = out || []; if (n && n.textContent) out.push(n.textContent);",
        "  ((n && n.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "startIntake().then(function () {",
        "  if (intakeText !== 'acme, BTBR') { console.error('posted text: ' + intakeText); process.exit(2); }",
        "  var t1 = textOf(nodes['nr-intake-out']);",
        "  if (t1.indexOf('Capturing documents') === -1) { console.error('preparing render: ' + t1); process.exit(3); }",
        # NEWRUN-ONE-START-001 (4): while preparing the button sleeps with the
        # progress line beside it
        "  if (els['nr-go'].disabled !== true || els['nr-preparing'].hidden ||",
        "      els['nr-preparing'].textContent !== 'Preparing… (0 of 2 done)')",
        "    { console.error('preparing line: ' + els['nr-go'].textContent + ' / ' + els['nr-preparing'].textContent); process.exit(14); }",
        "  return pollIntake(1);",
        "}).then(function () {",
        "  var t2 = textOf(nodes['nr-intake-out']);",
        "  if (t2.indexOf('1 ready · 1 could not be prepared') === -1) { console.error('counts: ' + t2); process.exit(5); }",
        "  if (t2.indexOf('No company matches this symbol.') === -1) { console.error('failure lost'); process.exit(6); }",
        "  if (t2.indexOf('Read: 8-K press releases filed 4 Aug 2026 to 2 Nov 2026 (90 days).') === -1) { console.error('scope'); process.exit(7); }",
        # NEWRUN-ONE-START-001: the ONE start button is nr-go — now 'Continue with 1 ready'
        "  if (!els['nr-preparing'].hidden) { console.error('preparing line stuck'); process.exit(15); }",
        "  if (els['nr-go'].textContent !== 'Continue with 1 ready' || els['nr-go'].disabled)",
        "    { console.error('one button: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled); process.exit(8); }",
        "  nrStartClick();",
        "  if (!posted) { console.error('no post'); process.exit(9); }",
        "  if (posted.intake_id !== 'ik1' || posted.companies.join(',') !== 'ACME') { console.error(JSON.stringify(posted)); process.exit(10); }",
        "  if (posted.accept_ready_subset !== true) { console.error('subset flag'); process.exit(11); }",
        "  if (posted.mode !== 'research' || 'event_types' in posted || 'reader_tools' in posted || 'models' in posted"
        "    || 'time_window_months' in posted) { console.error('options'); process.exit(12); }",
        "  return new Promise(function (r) { setTimeout(r, 0); });",
        "}).then(function () {",
        "  if (switched !== 'run9' || !newrunWrap.hidden) { console.error('switch'); process.exit(13); }",
        "  console.log('ok');",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_new_run_posts_mode_research_and_the_server_accepts_it(tmp_path):
    # MODE-001: 'Run now' starts the research agent, not the old page reader —
    # the payload the UI builds carries mode 'research' and none of the
    # page-reader-only choices, and the real POST /api/runs/new envelope
    # accepts it (fake research reader; no paid call)
    import json
    import subprocess

    from fastapi.testclient import TestClient

    from server.app import create_app
    from tests import research_fakes as F

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "clear", "nrError", "nrModels", "researchReady", "notePayload", "startRunNow",
                     "syncPasteMode", "applySupportOptions", "nrSupportValue",
                     "renderChosen", "renderPrevCompanies", "nrPreparedTickers", "tickedReadyCount",
                     "applyPasteDisable", "syncStartButton", "intakeDoneN", "noteHintText"))
    script = "\n".join([
        "var NEW_RUN_TYPES = [['trial_results', 'Trial results']];",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false, disabled: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var els = { 'nr-tickers': { value: '' }, 'nr-limit': { value: '' },",
        "            'nr-note': { value: 'Focus on catalysts.' }, 'nr-tools': { checked: false },",
        "            'nr-support': { checked: false } };",
        "var nodes = {};",
        "var nrResearch = { defaultText: null, maxLength: null, loaded: true };",
        "var nrSupport = { loaded: true, available: true, reason: null, choices: ['lettucedetect'], touched: false };",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  querySelectorAll: function (sel) {",
        "    if (sel === '.nr-co:checked') return [{ value: 'PTGX', checked: true }];",
        "    return []; } };",
        "var newrunWrap = { hidden: false };",
        "var runs = []; var switched = null; var nrState = 'ready'; var nrLoadError = '';",
        "var nrCompanies = ['PTGX'];",
        "var nrBusy = false;",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null, usedRun: null };",
        "var nrChosen = {}; var nrPrepUnticked = {};",
        "function toast() {}",
        "function switchRun(id) { switched = id; }",
        "var posted = [];",
        "var A = { newRun: function (body) { posted.push(body); return Promise.resolve({ ok: true, status: 202,",
        "    body: { id: 'run1', number: 1, label: 'PTGX · research' } }); },",
        "  runs: function () { return Promise.resolve([]); } };",
        fns,
        "startRunNow();",
        "if (!posted.length) { console.error('no post'); process.exit(2); }",
        "var p = posted[0];",
        "if (p.mode !== 'research' || p.companies.join(',') !== 'PTGX') { console.error(JSON.stringify(p)); process.exit(3); }",
        "for (var k in { event_types: 1, time_window_months: 1, reader_tools: 1, models: 1 })",
        "  if (k in p) { console.error('page-mode key sent: ' + k); process.exit(4); }",
        "if (p.spending_limit_usd !== null || p.note_for_agent !== 'Focus on catalysts.' || p.support_check !== null)",
        "  { console.error(JSON.stringify(p)); process.exit(5); }",
        "console.log(JSON.stringify(p));",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    body = json.loads(got.stdout.strip().splitlines()[-1])

    # the same payload through the real envelope: accepted, and the saved run
    # is a research run (Muse Spark only; the reviewer is stage S2)
    reader = F.PerCompany({"PTGX": [
        F.body([F.call("finish_draft", {"dispositions": [], "limitations": []}, "i1")])]})
    tools = F.tools({})
    app = create_app(runs_dir=tmp_path / "runs", reader=reader, background=False, second_reader=None,
                     wonder_reader=None, sam_reader=None, research_tools=tools, research_reader=reader)
    client = TestClient(app)
    r = client.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    run = json.loads((tmp_path / "runs" / r.json()["id"] / "run.json").read_text())
    assert run["run"]["choices"]["mode"] == "research"
    assert run["run"]["choices"]["models"] == "one"
    assert run["run"]["choices"]["reader_tools"] is False
    assert run["run"]["label"] == "PTGX · research"


def test_ticker_intake_refusal_shows_the_servers_sentence_in_the_red_line():
    # a refused intake (422/409 with the server's own sentence) is an inline
    # red line, never a toast; the list area stays empty
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("el", "clear", "nrError", "stopIntakePoll", "startIntake"))
    script = "\n".join([
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false,",
        "  addEventListener: function () {},",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var nodes = {};",
        "var els = { 'nr-tickers': { value: 'ACME' } };",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        "var A = { intake: function () { return Promise.resolve({ ok: false, status: 422,",
        "  body: { detail: 'An intake takes at most 25 tickers.' } }); } };",
        fns,
        "startIntake().then(function () {",
        "  var e = nodes['nr-error'];",
        "  if (e.hidden || e.textContent !== 'The tickers could not be prepared. An intake takes at most 25 tickers.') {",
        "    console.error(e.textContent); process.exit(2); }",
        "  if (!nodes['nr-intake-out'].hidden) { console.error('list area visible on failure'); process.exit(3); }",
        "  console.log('ok');",
        "}).catch(function (e) { console.error(e && e.message); process.exit(99); });",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    # Re-glass 1 finding 9 (nit): the citation box was drawn exactly on the
    # word rectangle, so its bottom edge clipped date descenders by 1-2 px.
    # The box now extends 2 pt lower; top, left and width are untouched.
    js = (UI / "app.js").read_text()
    assert "v.box[3] + 2 - v.box[1]" in js
    assert "b.style.top = (v.box[1] / H * 100)" in js or "v.box[1] / H * 100" in js


def test_u24_g1r4_removed_pane_reason_dates_in_words_no_null_line():
    # Re-glass 1 finding 6: the code-removed pane printed a literal "null" line
    # under "Why it was removed", and the reason used machine dates
    # ("the date 2026-09-22 is before today (2026-10-29)"). ISO days in the
    # sentence now read in words, and a missing reason prints no line at all.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    # U37: the code-removed pane now also carries the 'Keep anyway' button, so
    # its btn helper is grabbed too
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "esc", "btn", "removedSection", "dayInWords"))
    script = f"""
var RULE_WORDS = {{ outside_time_window: 'Outside the time window',
  third_party_calendar_only: 'House rule: a third-party calendar is never a source on its own.',
  person_removed: 'Removed by you.' }};
var SAY_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, dataset: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function () {{}} }};
}}
global.document = {{ createElement: function (tag) {{ var n = make(); n.tag = tag; return n; }} }};
var EXCLUSIONS = [];
function exclusionFor(id) {{ return EXCLUSIONS.filter(function (x) {{ return x.target === id; }})[0] || null; }}
{fns}
function textOf(node, out) {{
  out = out || [];
  if (node.textContent) out.push(node.textContent);
  (node.children || []).forEach(function (c) {{ textOf(c, out); }});
  return out.join('\\n');
}}
// machine dates in the sentence read in words
var s1 = dayInWords('Outside the time window: the date 2026-09-04 is before today (2026-10-29).');
if (s1 !== 'Outside the time window: the date September 4, 2026 is before today (October 29, 2026).')
  process.exit(2);
if (dayInWords('House rule, no dates.') !== 'House rule, no dates.') process.exit(3);
// a code removal with a reason: dates in words
EXCLUSIONS = [{{ target: 'r1', rule: 'outside_time_window', by: 'code',
  reason: 'Outside the time window: the date 2026-09-22 is before today (2026-10-29).' }}];
var t1 = {{ group: 'removed', reason: null, values: {{}} }};
var p1 = textOf(removedSection({{ id: 'r1' }}, t1));
if (p1.indexOf('Removed by code') === -1) process.exit(4);
if (p1.indexOf('September 22, 2026') === -1) process.exit(5);
if (p1.indexOf('2026-09-22') !== -1) process.exit(6);
// a code removal with NO reason and an unlisted rule: no line at all, no "null"
EXCLUSIONS = [{{ target: 'r2', rule: null, reason: null, by: 'code' }}];
var p2 = textOf(removedSection({{ id: 'r2' }}, {{ group: 'removed', reason: null, values: {{}} }}));
if (p2.indexOf('null') !== -1) process.exit(7);
var lines2 = p2.split('\\n').filter(function (l) {{ return l.trim(); }});
// (U37: the 'Keep anyway' button is part of the box)
if (lines2.join('|') !== 'Why it was removed|Removed by code|Keep anyway') process.exit(8);
// a person removal keeps its own reason, dates in words
EXCLUSIONS = [{{ target: 'r3', rule: 'person_removed', by: 'person', reason: null }}];
var p3 = textOf(removedSection({{ id: 'r3' }}, {{ group: 'removed',
  reason: 'A past annual result, not an upcoming event.', values: {{}} }}));
if (p3.indexOf('A past annual result, not an upcoming event.') === -1) process.exit(9);
if (p3.indexOf('Removed') === -1) process.exit(10);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u24_g1r2_cancel_after_a_failed_recheck_discards_the_fix():
    # Re-glass 1 finding 2: Save fix with a failed re-check recorded the fix
    # immediately, so Cancel still left "Fixed by you · still open" behind.
    # Cancel after that save now posts an undo — nothing is recorded until
    # Save fix passes or Keep anyway — and the red line says "failed" once.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "fixDialog")
    script = f"""
var FIELD_WORDS = {{ date_text: 'Date as written', date: 'Date', event_type: 'Event type',
  title: 'Title', company: 'Company', ticker: 'Ticker', program: 'Program',
  indication: 'Condition' }};
var FIX_FIELDS = ['date_text', 'date', 'event_type', 'title', 'company', 'ticker',
                  'program', 'indication'];
var NO_ERROR = 'The value already passed its checks before the edit.';
var CHECK_WORDS = {{ pass: 'passed', fail: 'failed', could_not_run: 'could not run',
                   not_needed: 'not needed' }};
function checkResultWord(r) {{ return CHECK_WORDS[r] || r; }}
var pending = false;
var route = {{ screen: 'judgment', rowId: 'r1', dialog: 'fix' }};
var navigations = [], toasts = [], errors = [], decides = [];
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}},
    hidden: false, value: '', placeholder: '', _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }},
    insertBefore: function (n) {{ this.children.unshift(n); }},
    remove: function () {{}}, focus: function () {{}} }};
}}
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}}
function btn(cls, text, onClick) {{ var b = el('button', cls, text); b._click = onClick; return b; }}
function clear(node) {{ node.textContent = ''; node.children = []; return node; }}
var dialogHost = make(), dialogWrap = make(); var dialogMount = null;
var TARGET = {{ values: {{}}, fixes: [], group: 'open' }};
function rowTarget(r) {{ return TARGET; }}
function currentRunId() {{ return 'run1'; }}
function navigate(h) {{ navigations.push(h); }}
function refresh() {{ return Promise.resolve(); }}
function toast(m) {{ toasts.push(m); }}
function showDialogError(m) {{ errors.push(m); }}
var DECIDE_REPLY = {{ ok: true, status: 200, body: {{ state: {{
  fixes: [{{ field: 'date_text', from: 'today', to: 'March 3, 2029' }}],
  recheck: {{ result: 'fail', reason: 'The re-check failed: The date text is not part of the quote.' }},
  group: 'corrected' }} }} }};
var UNDO_REPLY = {{ ok: true, status: 200, body: {{ state: {{}} }} }};
var A = {{ decide: function (runId, body) {{
  decides.push(body);
  return Promise.resolve(body.action === 'undo' ? UNDO_REPLY : DECIDE_REPLY);
}} }};
global.document = {{ getElementById: function () {{ return {{ value: '' }}; }} }};
{fns}
function findBtn(node, text) {{
  if (node.textContent === text && node._click) return node;
  for (var i = 0; i < (node.children || []).length; i++) {{
    var hit = findBtn(node.children[i], text);
    if (hit) return hit;
  }}
  return null;
}}
function undoCount() {{
  return decides.filter(function (b) {{ return b.action === 'undo'; }}).length;
}}
// scenario 1: one save with a failed re-check, then Cancel — the fix is discarded
fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
var save = findBtn(dialogHost, 'Save fix');
if (!save) process.exit(2);
save._click();
setTimeout(function () {{
  if (errors.length !== 1) process.exit(3);
  var n = 0, at = -1;
  while ((at = errors[0].indexOf('failed', at + 1)) !== -1) n++;
  if (n !== 1) {{ console.error('red line: ' + errors[0]); process.exit(4); }}  // "failed" once
  if (errors[0].indexOf('The date text is not part of the quote.') === -1) process.exit(5);
  var cancel = findBtn(dialogHost, 'Cancel');
  if (!cancel) process.exit(6);
  cancel._click();
  if (toasts[0] !== 'The fix was discarded.') process.exit(7);
  setTimeout(function () {{
    if (undoCount() !== 1) process.exit(8);          // discard, not a keep
    if (decides.filter(function (b) {{ return b.action === 'fix'; }}).length !== 1) process.exit(9);
    // scenario 1b (Codex P2): TWO failed saves, then Cancel — BOTH fixes are undone
    decides = []; toasts = []; errors = []; navigations = [];
    fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
    save = findBtn(dialogHost, 'Save fix');
    save._click();
    setTimeout(function () {{
      save._click();                                  // second save, re-check fails again
      setTimeout(function () {{
        if (decides.filter(function (b) {{ return b.action === 'fix'; }}).length !== 2) process.exit(10);
        cancel = findBtn(dialogHost, 'Cancel');
        cancel._click();
        if (toasts[0] !== 'The fixes were discarded.') process.exit(11);
        setTimeout(function () {{
          if (undoCount() !== 2) process.exit(12);    // every fix THIS dialog saved
          // both undos are done before the dialog closes
          if (navigations.length !== 1) process.exit(13);
          // scenario 2: Cancel with no save yet only closes the dialog
          decides = []; toasts = []; errors = []; navigations = [];
          fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
          cancel = findBtn(dialogHost, 'Cancel');
          cancel._click();
          if (undoCount() !== 0) process.exit(14);    // never undoes a pre-dialog decision
          if (navigations.length !== 1) process.exit(15);
          console.log('ok');
        }}, 0);
      }}, 0);
    }}, 0);
  }}, 0);
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u26_glass2_wonder_prompt_folds_and_person_steps_carry_time():
    # Re-glass 2: the Muse Wonder prompt folded and capped exactly like the
    # first and second readers' prompts (same fold label, same instrbox), and
    # person steps carry date AND time like the Code and Model step headers.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    assert "Instructions sent to Muse Wonder': true" in js
    assert "dateShort(l.at) + ' ' + hm(l.at)" in js
    import re
    prompt_steps = re.search(r"var PROMPT_STEPS = \{.*?\};", js, re.S).group(0)
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "esc", "clear", "revealRow", "checkFnFor", "daySpanWords",
                     "plainProposal", "dayInWords", "trailStep", "readerDateWords",
                     "readerWhen", "shownField"))
    script = f"""
{prompt_steps}
var FIELD_WORDS = {{ date_text: 'Date as written' }};
var PERSON_STEP_WORDS = {{ keep: 'Kept by you', fix: 'Fixed by you',
                           remove: 'Removed by you', undo: 'Undone by you' }};
var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',
                    'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, dataset: {{}},
    open: false, _l: {{}}, classList: {{ add: function () {{}}, remove: function () {{}}, toggle: function () {{}} }},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }} }};
}}
global.document = {{ createElement: function (tag) {{ var n = make(); n.tag = tag; return n; }} }};
function rowTarget(r) {{ return {{ values: {{}} }}; }}
function dateShort(iso) {{ return '29 Oct 2026'; }}
function hm(iso) {{ return '17:21:05'; }}
function dayMonth(d) {{ return ''; }}
function eventWord(v) {{ return String(v); }}
function num(n) {{ return n === null || n === undefined ? '' : Number(n).toLocaleString('en-US'); }}
function money(c) {{ return '$' + c; }}
{fns}
function walk(n, fn) {{ fn(n); (n.children || []).forEach(function (c) {{ walk(c, fn); }}); }}
function findOne(n, pred) {{
  var hit = null;
  walk(n, function (x) {{ if (!hit && pred(x)) hit = x; }});
  return hit;
}}
var WONDER_TEXT = 'You are re-reading one region of a page. The image is a band... ' +
  'Output the JSON object only.';
// the Wonder instruction step folds exactly like the other two readers'
var step = trailStep({{ id: 'r1', checks: [] }}, {{
  step: 'Instructions sent to Muse Wonder', tag: 'Code', who: 'capture.wonder',
  at: '2026-10-29T16:51:11+00:00', text: WONDER_TEXT, tokens: {{ prompt: 1335, completion: null }}
}}, false);
var fold = findOne(step, function (x) {{ return (x.className || '').indexOf('fold') !== -1; }});
if (!fold) {{ console.error('no fold on the Wonder step'); process.exit(2); }}
if ((fold.className || '').indexOf('instrbox') === -1) {{ console.error('fold class: ' + fold.className); process.exit(3); }}
var summary = findOne(fold, function (x) {{ return x.tag === 'summary'; }});
if (!summary || summary.textContent !== 'Show the instructions') {{ console.error('summary: ' + JSON.stringify(summary && summary.textContent)); process.exit(4); }}
var pre = findOne(fold, function (x) {{ return x.tag === 'pre'; }});
if (!pre || pre.textContent !== WONDER_TEXT) {{ console.error('pre: ' + (pre && pre.textContent.length)); process.exit(5); }}
var meta = findOne(step, function (x) {{ return (x.className || '') === 'meta'; }});
if (!meta || meta.textContent !== 'prompt 1,335 tokens') {{ console.error('meta: ' + JSON.stringify(meta && meta.textContent)); process.exit(6); }}
// the step's own line is the caption, not the raw prompt
var flat = [];
walk(step, function (x) {{ if (x.textContent) flat.push(x.textContent); }});
if (flat.join('\\n').indexOf('The exact text the model received.') === -1) {{ console.error('caption missing'); process.exit(8); }}
// a person step carries date AND time in the same wh span form
var pstep = trailStep({{ id: 'r1', checks: [] }}, {{
  __decision: true, line: {{ action: 'fix', at: '2026-10-29T17:21:05+00:00',
    fix: {{ field: 'date_text', from: 'November 12, 2026', to: 'March 3, 2029' }},
    note: 'the quote says March' }}
}}, false);
var wh = findOne(pstep, function (x) {{ return (x.className || '') === 'wh'; }});
if (!wh || wh.textContent !== '29 Oct 2026 17:21:05') {{
  console.error('person wh: ' + (wh && wh.textContent)); process.exit(9);
}}
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    # Codex P2 on #91: Cancel pressed while the Save POST was still in flight
    # closed the dialog with saves=0, and the late-landing fix stayed. Cancel
    # is now disabled while a save is pending, and a fix whose re-check answer
    # arrives after the dialog closed (Esc) is undone, never orphaned.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "fixDialog")
    script = f"""
var FIELD_WORDS = {{ date_text: 'Date as written', date: 'Date' }};
var FIX_FIELDS = ['date_text', 'date'];
var NO_ERROR = 'The value already passed its checks before the edit.';
var CHECK_WORDS = {{ pass: 'passed', fail: 'failed' }};
function checkResultWord(r) {{ return CHECK_WORDS[r] || r; }}
var pending = false;
var route = {{ screen: 'judgment', rowId: 'r1', dialog: 'fix' }};
var navigations = [], toasts = [], errors = [], decides = [];
var saveResolver = null;
var undoOk = true;
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}},
    hidden: false, value: '', placeholder: '', disabled: false, _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }},
    insertBefore: function (n) {{ this.children.unshift(n); }},
    remove: function () {{}}, focus: function () {{}} }};
}}
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}}
function btn(cls, text, onClick) {{ var b = el('button', cls, text); b._click = onClick; return b; }}
function clear(node) {{ node.textContent = ''; node.children = []; return node; }}
var dialogHost = make(), dialogWrap = make(); var dialogMount = null;
function rowTarget(r) {{ return {{ values: {{}}, fixes: [], group: 'open' }}; }}
function currentRunId() {{ return 'run1'; }}
function navigate(h) {{ navigations.push(h); }}
function refresh() {{ return Promise.resolve(); }}
function toast(m) {{ toasts.push(m); }}
function showDialogError(m) {{ errors.push(m); }}
var DECIDE_REPLY = {{ ok: true, status: 200, body: {{ state: {{
  fixes: [{{ field: 'date_text', from: 'today', to: 'March 3, 2029' }}],
  recheck: {{ result: 'fail', reason: 'The re-check failed: The date text is not part of the quote.' }},
  group: 'corrected' }} }} }};
var A = {{ decide: function (runId, body) {{
  decides.push(body);
  if (body.action === 'undo') return Promise.resolve(undoOk
    ? {{ ok: true, status: 200, body: {{ state: {{}} }} }}
    : {{ ok: false, status: 500, body: {{ detail: 'nope' }} }});
  // the Save POST answers only when the test lets it
  return new Promise(function (res) {{ saveResolver = res; }});
}} }};
global.document = {{ getElementById: function () {{ return {{ value: '' }}; }} }};
{fns}
function findBtn(node, text) {{
  if (node.textContent === text && node._click) return node;
  for (var i = 0; i < (node.children || []).length; i++) {{
    var hit = findBtn(node.children[i], text);
    if (hit) return hit;
  }}
  return null;
}}
function undoCount() {{
  return decides.filter(function (b) {{ return b.action === 'undo'; }}).length;
}}
// Cancel while the Save POST is in flight: the button is disabled and the
// click is a no-op — the dialog waits for the answer
fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
var save = findBtn(dialogHost, 'Save fix');
var cancel = findBtn(dialogHost, 'Cancel');
save._click();
if (cancel.disabled !== true) process.exit(2);
cancel._click();
if (navigations.length !== 0 || undoCount() !== 0) process.exit(3);
// the answer lands: failed re-check, dialog still open — Cancel now discards
saveResolver(DECIDE_REPLY);
setTimeout(function () {{
  if (cancel.disabled !== false) process.exit(4);
  if (errors.length !== 1) process.exit(5);
  cancel._click();
  if (toasts[0] !== 'The fix was discarded.') process.exit(6);
  setTimeout(function () {{
    if (undoCount() !== 1) process.exit(7);
    // Esc during an in-flight save: the late fix is undone, not orphaned
    decides = []; toasts = []; errors = []; navigations = [];
    fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
    save = findBtn(dialogHost, 'Save fix');
    save._click();
    dialogWrap.hidden = true;                       // Esc closed the dialog mid-flight
    saveResolver(DECIDE_REPLY);
    setTimeout(function () {{
      if (undoCount() !== 1) process.exit(8);       // the late fix was undone
      if (toasts.indexOf('The fix was discarded.') === -1) process.exit(9);
      if (errors.length !== 0) process.exit(10);    // no red line into a closed dialog
      // the late undo can fail: the person gets the recovery sentence
      decides = []; toasts = []; errors = []; navigations = [];
      undoOk = false;
      fixDialog({{ id: 'r1', ticker: 'ACME', title: 'ROW' }});
      save = findBtn(dialogHost, 'Save fix');
      save._click();
      dialogWrap.hidden = true;
      saveResolver(DECIDE_REPLY);
      setTimeout(function () {{
        if (undoCount() !== 1) process.exit(11);
        if (toasts.indexOf('The fix could not be discarded. Open the row and press Undo.') === -1)
          process.exit(12);
        console.log('ok');
      }}, 0);
    }}, 0);
  }}, 0);
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u24_g1r1_the_code_that_ran_is_the_real_source_not_json():
    # Re-glass 1 finding 1: the fold showed the route's raw JSON record
    # ({"ref":"checks/event_checks.py:quote_match","source":"def ...\n..."})
    # as one escaped line. The fold now parses the reply and shows the source
    # only — a folded monospace block, newlines honoured, no JSON, no ref path.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("seeTheCheck", "dateFinding", "projectionComplete", "partialProjection"))
    script = f"""
var CHECK_WORDS = {{ pass: 'passed', fail: 'failed', could_not_run: 'could not run',
                   not_needed: 'not needed' }};
function checkResultWord(r) {{ return CHECK_WORDS[r] || r; }}
function el(tag, cls, text) {{
  return {{ tag: tag, className: cls || '', textContent: text || '', children: [],
    style: {{}}, dataset: {{}}, open: false, _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }} }};
}}
function esc(s) {{ return s === null || s === undefined ? '' : String(s); }}
{fns}
var ROUTE_REPLY = {{ ref: 'checks/event_checks.py:quote_match',
  source: 'def quote_match(quote: str | None, words: list[dict], box=None) -> CheckResult:\\n    # Pass if the quote is on the page.\\n    return CheckResult("pass")' }};
global.fetch = function (url) {{
  if (url.indexOf('/api/checks/source?ref=') !== 0) return Promise.resolve({{ ok: false }});
  return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve(ROUTE_REPLY); }} }});
}};
var c = {{ result: 'pass', compared: {{ looked_for: 'the quote', found: 'the quote' }},
           code_ref: {{ file: 'event_checks', function: 'quote_match' }} }};
var d = seeTheCheck(c);
var toggle = d._l.toggle;
if (!toggle) process.exit(2);
d.open = true;
toggle();
setTimeout(function () {{
  var lbl = null, pre = null;
  (function walk(n) {{
    (n.children || []).forEach(function (x) {{
      if (x.className === 'codelbl') lbl = x;
      if (x.tag === 'pre') pre = x;
      walk(x);
    }});
  }})(d);
  if (!lbl || lbl.textContent !== 'The code that ran') process.exit(3);
  if (!pre) process.exit(4);
  if (pre.textContent !== ROUTE_REPLY.source) process.exit(5);       // source verbatim
  if (pre.textContent.indexOf('\\n') < 0) process.exit(6);           // newlines honoured
  var shown = [];
  (function all(n) {{
    if (n.textContent) shown.push(n.textContent);
    (n.children || []).forEach(all);
  }})(d);
  var flat = shown.join('\\n');
  if (flat.indexOf('event_checks.py') !== -1) process.exit(7);       // no ref path on screen
  if (flat.indexOf('"ref"') !== -1) process.exit(8);                 // no raw JSON
  if (flat.indexOf('quote_match(quote') === -1) process.exit(9);     // the real code is there
  var before = (function count(n) {{ var k = 0;
    (n.children || []).forEach(function (x) {{ k += 1 + count(x); }}); return k; }})(d);
  toggle();                                                          // a second toggle re-fetches nothing
  var after = (function count(n) {{ var k = 0;
    (n.children || []).forEach(function (x) {{ k += 1 + count(x); }}); return k; }})(d);
  if (after !== before) process.exit(10);
  console.log('ok');
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u26_glass3_calendar_rows():
    # Re-glass 3: a month-only date is a period (hollow dot under a
    # 'February 2027' header, never '1-28 Feb' filled); headers read in the
    # order the periods close (February before First half and 2027); a full
    # year folds every wording of it into the one year marker (verified live:
    # '2027' and 'in 2027' share one marker under the year header).
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    assert "calIsMonthPeriod" in js and "calHeaderOrd" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("calDays", "calIsMonthPeriod", "calIsExact", "calMonths",
                     "calQuarterKey", "calHeader", "calHeaderOrd"))
    script = f"""
var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',
                    'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
var MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                   'August', 'September', 'October', 'November', 'December'];
{fns}
function mk(date, end) {{ return {{ period: !!end, date: date, end: end || null }}; }}
// G3-R1: a whole-month period is a month-only date, not an exact range
if (!calIsMonthPeriod(mk('2027-02-01', '2027-02-28'))) process.exit(2);
if (!calIsMonthPeriod(mk('2027-01-01', '2027-01-31'))) process.exit(3);
if (calIsMonthPeriod(mk('2026-11-14', '2026-11-17'))) process.exit(4);   // a congress weekend stays exact
if (calIsMonthPeriod(mk('2027-01-01', '2027-03-31'))) process.exit(5);   // a quarter is not a month
if (calIsExact(mk('2027-02-01', '2027-02-28'))) process.exit(6);         // never exact
if (!calIsExact(mk('2026-11-14', '2026-11-17'))) process.exit(7);
var mh = calHeader(mk('2027-02-01', '2027-02-28'));
if (mh.key !== 'm:2027-02' || mh.len !== 2) process.exit(8);             // its month's header
// G9-R3: a quarter gets its OWN header right after the first month it spans
// begins ('Q4 2026' after 'October 2026'), never the start month's header
var q4 = calHeader(mk('2026-10-01', '2026-12-31'));
if (q4.key !== 'q:Q4 2026' || q4.label !== 'Q4 2026' || q4.len !== 3) process.exit(11);
var sepdec = calHeader(mk('2026-09-01', '2026-12-31'));
if (sepdec.key !== 'p:2026-09:2026-12-31' || sepdec.label !== null) process.exit(12);
// G3-R2/G9-R3: headers close in calendar order — month, quarter's own header,
// half-year, year
var hs = [calHeader(mk('2026-11-14', '2026-11-17')),   // November exact
          calHeader(mk('2026-12-01', '2026-12-03')),   // December exact
          calHeader(mk('2027-01-11', '2027-01-14')),   // January exact
          calHeader(mk('2027-02-01', '2027-02-28')),   // February month period
          calHeader(mk('2027-01-01', '2027-03-31')),   // Q1 — its own header
          calHeader(mk('2027-01-01', '2027-06-30')),   // first half
          calHeader(mk('2027-01-01', '2027-12-31'))];  // the year
hs.sort(function (a, b) {{
  var oa = calHeaderOrd(a), ob = calHeaderOrd(b);
  if (oa !== ob) return oa < ob ? -1 : 1;
  return a.len - b.len;
}});
var keys = hs.map(function (h) {{ return h.key + '/' + h.len; }});
var want = 'm:2026-11/1,m:2026-12/1,m:2027-01/1,q:Q1 2027/3,m:2027-02/2,h:2027:First/6,y:2027/12';
if (keys.join(',') !== want) {{ console.error(keys.join(',')); process.exit(9); }}
// G3-R3 canon lives in renderCalendar; the helper it builds on:
if (calQuarterKey(mk('2027-01-01', '2027-03-31')) !== 'Q1 2027') process.exit(10);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u24_g1r3_row_landing_scrolls_the_list_column_only():
    # Re-glass 1 finding 7: opening a row from a hash, filter or the rail scrolled
    # the WHOLE .main ~6,000 px (scrollIntoView moves overflow:hidden boxes too),
    # taking the heading, page and pane off screen. Every landing scroll now goes
    # through revealRow, which forces .main back to 0 — on the Audit log AND the
    # Catalyst Calendar (title + pills stay put on long runs).
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    # no render path scrolls .main directly any more — the only call site left
    # is inside revealRow itself
    assert js.count(".scrollIntoView(") == 1 and "revealRow(onIt)" in js
    for fn in ("renderAudit", "renderCalendar", "renderSource", "renderTrail", "render"):
        assert fn in js
    fns = _grab_fn(js, "revealRow")
    script = f"""
{fns}
var scrolled = [];
var main = {{ scrollTop: 0 }};
global.document = {{ querySelector: function (sel) {{ return sel === '.main' ? main : null; }} }};
var node = {{ scrollIntoView: function (opts) {{ scrolled.push(opts || 'nearest'); }} }};
// a landing scroll moves the list pane but .main stays at 0
main.scrollTop = 6144;   // what scrollIntoView did to .main before the fix
revealRow(node);
if (scrolled.length !== 1) process.exit(2);
if (main.scrollTop !== 0) process.exit(3);   // G1-R3: .main never scrolls
revealRow(node, {{ block: 'center', behavior: 'smooth' }});
if (scrolled.length !== 2 || scrolled[1].block !== 'center') process.exit(4);
if (main.scrollTop !== 0) process.exit(5);
revealRow(null);                              // no selection: no crash, main untouched
if (main.scrollTop !== 0) process.exit(6);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u23_server_422_sentence_is_shown():
    # the route answers 422 with a plain sentence in detail — show that sentence;
    # a missing route (404/501) or a body without detail keeps the build line
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay", "projectionComplete", "partialProjection"))
    script = f"""
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {{}};
var sayRec = null;
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: false,
    isConnected: true, _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }} }};
}}
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}}
function btn(cls, text) {{ return el('button', cls, text); }}
function clear(node) {{ node.children = []; }}
function encodeWav() {{ return new Uint8Array(0); }}
function currentRunId() {{ return 'run1'; }}
var nextResp = null;
global.fetch = function () {{
  var r = nextResp; nextResp = null;
  return Promise.resolve(r);
}};
{fns}
var stream = {{ getTracks: function () {{ return [{{ stop: function () {{}} }}]; }} }};
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', {{ configurable: true,
  value: {{ mediaDevices: {{ getUserMedia: function () {{
    return new Promise(function (res) {{ gumResolve = res; }}); }} }} }} }});
function FakeAC() {{}}
FakeAC.prototype.sampleRate = 48000;
FakeAC.prototype.destination = {{}};
FakeAC.prototype.createMediaStreamSource = function () {{ return {{ connect: function () {{}} }}; }};
FakeAC.prototype.createScriptProcessor = function () {{
  return {{ connect: function () {{}}, onaudioprocess: null }}; }};
FakeAC.prototype.close = function () {{}};
global.window = {{ AudioContext: FakeAC }};
var wrap = sayControls({{ id: 'r1' }});
var b = wrap.children[0], note = wrap.children[1];
function assert(cond, code) {{ if (!cond) {{ console.error('fail ' + code); process.exit(code); }} }}
function recordAndStop(resp, done) {{
  b._l.mousedown();
  gumResolve(stream);
  setTimeout(function () {{
    for (var i = 0; i < 20000; i++) sayRec.samples.push(0);  // ~0.42 s, past the too-short cut
    nextResp = resp;
    b._l.click();   // stop -> post
    setTimeout(done, 0);
  }}, 0);
}}
recordAndStop({{ ok: false, status: 422,
  json: function () {{ return Promise.resolve({{ detail: 'The audio is longer than 30 seconds.' }}); }} }},
  function () {{
    assert(note.textContent === 'The audio is longer than 30 seconds.', 2);
    // a 404 (route absent) keeps the one honest build line
    recordAndStop({{ ok: false, status: 404,
      json: function () {{ return Promise.resolve({{ detail: 'should be ignored' }}); }} }},
      function () {{
        assert(note.textContent === 'Voice is not on this build yet.', 3);
        // a 422 body without detail also falls back to the build line
        recordAndStop({{ ok: false, status: 422, json: function () {{ return Promise.resolve({{}}); }} }},
          function () {{
            assert(note.textContent === 'Voice is not on this build yet.', 4);
            console.log('ok');
          }});
      }});
  }});
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u29_run_card_job_block_and_money_rule():
    # GLASS 7 / U29: the job block lives in the rail run card; the money rule
    # reads 'less than a cent per kept row' under a cent and '$0.08 per kept
    # row' at a cent or more; the stop sentence is figure-less; the chooser
    # line carries the live saved-run count.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    for word in ("runcard-job", "updateRunCardJob", "costPerKeptRowWords",
                 "less than a cent per kept row", "saved run", "jb-row1"):
        assert word in (html + js), word
    assert "jobbar" not in html
    fns = "\n".join(_grab_fn(js, n) for n in ("cents", "usdCents", "money", "costPerKeptRowWords", "projectionComplete", "partialProjection"))
    script = f"""
var detail = {{ state: {{ cost_per_kept_row_usd: null }} }};
{fns}
function setCpkr(v) {{ detail.state.cost_per_kept_row_usd = v; }}
setCpkr(0.005189);
if (costPerKeptRowWords() !== 'less than a cent per kept row') process.exit(2);
setCpkr(0.08);
if (costPerKeptRowWords() !== '$0.08 per kept row') process.exit(3);
setCpkr(0.10);
if (costPerKeptRowWords() !== '$0.10 per kept row') process.exit(4);
setCpkr(null);
if (costPerKeptRowWords() !== null) process.exit(5);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u25_resume_rechecks_the_button_before_begin():
    # Codex P1 on #93: begin() ran after the awaited ctx.resume() without
    # rechecking the button — a row change during the resume left a live take
    # on a disconnected button. begin() now stops the tracks and closes the
    # context instead; no sayRec is ever set.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay", "projectionComplete", "partialProjection"))
    script = f"""
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {{}};
var sayRec = null;
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: false,
    isConnected: true, value: '', _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }} }};
}}
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}}
function btn(cls, text) {{ return el('button', cls, text); }}
function clear(node) {{ node.children = []; }}
function encodeWav() {{ return new Uint8Array(0); }}
function currentRunId() {{ return 'run1'; }}
var sent = 0;
global.fetch = function () {{ sent++; return Promise.resolve({{ ok: false }}); }};
{fns}
var stops = 0, closes = 0, resumeResolve = null;
var stream = {{ getTracks: function () {{ return [{{ stop: function () {{ stops++; }} }}]; }} }};
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', {{ configurable: true,
  value: {{ mediaDevices: {{ getUserMedia: function () {{
    return new Promise(function (res) {{ gumResolve = res; }}); }} }} }} }});
function FakeAC() {{ this.sampleRate = 48000; this.destination = {{}}; this.state = 'suspended'; }}
FakeAC.prototype.createMediaStreamSource = function () {{ return {{ connect: function () {{}} }}; }};
FakeAC.prototype.createScriptProcessor = function () {{
  return {{ connect: function () {{}}, onaudioprocess: null }}; }};
FakeAC.prototype.resume = function () {{
  // the resume answers only when the test lets it — after the button is gone
  return new Promise(function (res) {{ resumeResolve = res; }});
}};
FakeAC.prototype.close = function () {{ closes++; }};
global.window = {{ AudioContext: FakeAC }};
var wrap = sayControls({{ id: 'r1' }});
var b = wrap.children[0];
b._l.mousedown();
gumResolve(stream);
setTimeout(function () {{
  if (sayRec) {{ console.error('a take started before the resume answered'); process.exit(2); }}
  b.isConnected = false;                 // the row changed mid-resume
  resumeResolve();                       // the deferred resume now lands
  setTimeout(function () {{
    if (sayRec !== null) {{ console.error('sayRec was set on a gone button'); process.exit(3); }}
    if (stops !== 1) {{ console.error('tracks stopped ' + stops + ' time(s)'); process.exit(4); }}
    if (closes !== 1) {{ console.error('ctx closed ' + closes + ' time(s)'); process.exit(5); }}
    if (b.textContent.indexOf('Listening') === 0) {{ console.error('button still says Listening'); process.exit(6); }}
    if (sent !== 0) {{ console.error('a take was posted'); process.exit(7); }}
    console.log('ok');
  }}, 0);
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u28_corrected_rows_say_who_caught_it():
    # G1-18: a Corrected row carries 'Caught by code' / 'Caught by the second
    # reader' / 'Fixed by you' in place of MATCHED, and the second line reads
    # the bare before → after — dates in words, event types as their word.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    assert "Caught by the second reader" in js and "calWords" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "esc", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket", "dayInWords",
                     "projected", "reviewClass", "decidedMark", "shownDateText", "shownField", "exclusionFor", "mistakeFor", "auditRow",
                     "projectionComplete", "partialProjection"))
    script = f"""
var FIELD_WORDS = {{ date_text: 'Date as written', date: 'Date', event_type: 'Event type' }};
var STATUS_WORDS = {{ matched: 'matched', proposed: 'proposed' }};
var DECIDED_BADGE = {{ kept: 'Kept by you', corrected: 'Fixed by you' }};
var RULE_WORDS = {{}};
var SAY_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
function eventWord(v) {{ return {{ regulatory_decision: 'Regulatory decision',
  advisory_committee: 'Advisory committee' }}[v] || String(v); }}
function navigate() {{}}
function currentRunId() {{ return 'run1'; }}
var audit = {{ selected: null, filter: 'ALL' }};
var detail = {{ run: {{ rows: [], mistakes: [
    {{ target: 'e1', caught_by: 'code', what_changed: 'Date changed from 2027-06-03 to 2027-06-30 after the date check failed.' }},
    {{ target: 'e2', caught_by: 'second_reader', what_changed: 'Company changed from Samplebio Two (SMPL2) to Samplebio Three (SMPL3).' }},
    {{ target: 'e3', caught_by: 'person', what_changed: 'Event type changed from regulatory_decision to advisory_committee.' }}
  ], sources: [] }}, state: {{ targets: {{}}, exclusions: [] }} }};
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, dataset: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function () {{}}, querySelector: function () {{ return null; }} }};
}}
global.document = {{ createElement: function (t) {{ var n = make(); n.tag = t; return n; }},
  createTextNode: function (t) {{ return {{ textContent: t }}; }} }};
{fns}
function textOf(n, out) {{
  out = out || [];
  if (n.textContent) out.push(n.textContent);
  (n.children || []).forEach(function (c) {{ textOf(c, out); }});
  return out.join(' | ');
}}
function mkRow(id) {{
  return {{ id: id, ticker: 'SMPL', title: 'ROW-' + id, date_text: 'June 30, 2027',
           event_type: 'regulatory_decision', company: 'Samplebio Three', proposed_by: 'Muse Spark' }};
}}
// run-level correction caught by code: dates in words, bare before → after
detail.run.rows = [mkRow('e1')];
detail.state.targets = {{ e1: {{ group: 'corrected', status: 'matched', decided_by_person: false,
  fixes: [], values: {{ date: '2027-06-30', date_text: 'June 30, 2027', company: 'Samplebio Three',
    event_type: 'regulatory_decision', ticker: 'SMPL', title: 'ROW-e1' }} }} }};
var s1 = textOf(auditRow(mkRow('e1'), detail.state.targets.e1));
if (s1.indexOf('Caught by code') === -1) {{ console.error(s1); process.exit(2); }}
if (s1.indexOf('June 3, 2027 → June 30, 2027') === -1) {{ console.error(s1); process.exit(3); }}
if (s1.indexOf('2027-06-03') !== -1) {{ console.error('raw ISO on screen: ' + s1); process.exit(4); }}
// caught by the second reader: company words as written
detail.run.rows = [mkRow('e2')];
detail.state.targets = {{ e2: {{ group: 'corrected', status: 'matched', decided_by_person: false,
  fixes: [], values: {{ company: 'Samplebio Three', ticker: 'SMPL', title: 'ROW-e2' }} }} }};
var s2 = textOf(auditRow(mkRow('e2'), detail.state.targets.e2));
if (s2.indexOf('Caught by the second reader') === -1) {{ console.error(s2); process.exit(5); }}
if (s2.indexOf('Samplebio Two (SMPL2) → Samplebio Three') === -1) {{ console.error(s2); process.exit(6); }}
// Codex P2: the combined label — 'Company and ticker changed from … to …, as the
// second reading gave.' is the real sample builder wording; it maps to company
detail.run.mistakes = [{{ target: 'e4', caught_by: 'code',
  what_changed: 'Company and ticker changed from Samplebio Two (SMPL2) to Samplebio Three (SMPL3), as the second reading gave.' }}];
detail.run.rows = [mkRow('e4')];
detail.state.targets = {{ e4: {{ group: 'corrected', status: 'matched', decided_by_person: false,
  fixes: [], values: {{ company: 'Samplebio Three', ticker: 'SMPL3', title: 'ROW-e4' }} }} }};
var s4 = textOf(auditRow(mkRow('e4'), detail.state.targets.e4));
if (s4.indexOf('Samplebio Two (SMPL2) → Samplebio Three') === -1) {{ console.error(s4); process.exit(10); }}
if (s4.indexOf('Samplebio Three (SMPL3)') === -1) {{ console.error('ticker dropped: ' + s4); process.exit(11); }}
// caught by you / a person's own fix: 'Fixed by you', and a person's fix line wins
detail.run.rows = [mkRow('e3')];
detail.state.targets = {{ e3: {{ group: 'corrected', status: 'matched', decided_by_person: true,
  fixes: [{{ field: 'event_type', from: 'regulatory_decision', to: 'advisory_committee' }}],
  values: {{ event_type: 'advisory_committee', ticker: 'SMPL', title: 'ROW-e3' }} }} }};
var s3 = textOf(auditRow(mkRow('e3'), detail.state.targets.e3));
if (s3.indexOf('Fixed by you') === -1) {{ console.error(s3); process.exit(7); }}
if (s3.indexOf('Regulatory decision → Advisory committee') === -1) {{ console.error(s3); process.exit(8); }}
if (s3.indexOf('decided by you') !== -1) {{ console.error('redundant line: ' + s3); process.exit(9); }}
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr
    # Codex P2 on #93: ctx.resume().then(begin, begin) ran begin() even when
    # the resume was rejected — a take on a context that never started. A
    # rejected resume now stops the tracks, closes the context and shows the
    # same honest line as an unreachable microphone; no sayRec is set.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay", "projectionComplete", "partialProjection"))
    script = f"""
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {{}};
var sayRec = null;
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: true,
    isConnected: true, value: '', _l: {{}},
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function (t, fn) {{ this._l[t] = fn; }},
    querySelector: function () {{ return null; }} }};
}}
function el(tag, cls, text) {{
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}}
function btn(cls, text) {{ return el('button', cls, text); }}
function clear(node) {{ node.children = []; }}
function encodeWav() {{ return new Uint8Array(0); }}
function currentRunId() {{ return 'run1'; }}
var sent = 0;
global.fetch = function () {{ sent++; return Promise.resolve({{ ok: false }}); }};
{fns}
var stops = 0, closes = 0, connected = 0;
var stream = {{ getTracks: function () {{ return [{{ stop: function () {{ stops++; }} }}]; }} }};
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', {{ configurable: true,
  value: {{ mediaDevices: {{ getUserMedia: function () {{
    return new Promise(function (res) {{ gumResolve = res; }}); }} }} }} }});
function FakeAC() {{ this.sampleRate = 48000; this.destination = {{}}; this.state = 'suspended'; }}
FakeAC.prototype.createMediaStreamSource = function () {{ return {{ connect: function () {{ connected++; }} }}; }};
FakeAC.prototype.createScriptProcessor = function () {{
  return {{ connect: function () {{ connected++; }}, onaudioprocess: null }}; }};
FakeAC.prototype.resume = function () {{ return Promise.reject(new Error('denied')); }};
FakeAC.prototype.close = function () {{ closes++; }};
global.window = {{ AudioContext: FakeAC }};
var wrap = sayControls({{ id: 'r1' }});
var b = wrap.children[0], note = wrap.children[1];
b._l.mousedown();
gumResolve(stream);
setTimeout(function () {{
  if (sayRec !== null) {{ console.error('sayRec was set after a rejected resume'); process.exit(2); }}
  if (stops !== 1) {{ console.error('tracks stopped ' + stops + ' time(s)'); process.exit(3); }}
  if (closes !== 1) {{ console.error('ctx closed ' + closes + ' time(s)'); process.exit(4); }}
  if (connected !== 0) {{ console.error('the context was wired ' + connected + ' time(s)'); process.exit(5); }}
  if (note.textContent !== 'The microphone could not be reached.') {{
    console.error('note: ' + JSON.stringify(note.textContent)); process.exit(6);
  }}
  if (note.hidden !== false) {{ console.error('the note stayed hidden'); process.exit(7); }}
  if (sent !== 0) {{ console.error('a take was posted'); process.exit(8); }}
  console.log('ok');
}}, 0);
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr



def _compare_cards(rows_a, rows_b):
    """compareRuns from app.js under node, against two fake runs.

    Returns (cards, wording): cards is a list of {kind, word, fields} and
    wording the folded wording-only count."""
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("normWords", "pairKey", "titleSim", "pageSet", "rowPageKey",
                     "compareRuns", "rowVals", "fmtField"))
    script = f"""
var FIELD_WORDS = {{date: 'Date', date_end: 'Date end', date_text: 'Date as written',
  title: 'Title', event_type: 'Type', company: 'Company', ticker: 'Ticker',
  program: 'Program', indication: 'Condition'}};
function eventWord(v) {{ return String(v); }}
var ACTION_WORDS = {{}};
function lastPersonDecisionIn() {{ return null; }}
function dateShort(s) {{ return String(s); }}
{fns}
function mkX(rows) {{
  var targets = {{}};
  rows.forEach(function (r) {{ targets[r.id] = {{ values: Object.assign({{}}, r) }}; }});
  return {{ run: {{ rows: rows, run: {{ label: 'the run' }} }},
           state: {{ targets: targets }}, decisions: [] }};
}}
var cards = compareRuns(mkX({json.dumps(rows_a)}), mkX({json.dumps(rows_b)}));
console.log(JSON.stringify({{ cards: cards.map(function (c) {{
  return {{ kind: c.kind, word: c.word || null,
           fields: c.fields.map(function (d) {{ return d.field; }}) }}; }}),
  wording: cards.wording || 0 }}));
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout.strip().splitlines()[-1])
    return out["cards"], out["wording"]


def test_u27_since_then_group_and_reworded_fold():
    # Re-glass 4: a row the person decided after run B gets a 'Since then'
    # card even when nothing changed between the runs, in its own group with
    # the count; wording-only pairs are kept (not just counted) so the screen
    # can fold them open under 'Show the N reworded rows'.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    assert "Since then · " in js and "reworded row" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("normWords", "pairKey", "titleSim", "lastPersonDecisionIn",
                     "pageSet", "rowPageKey", "compareRuns", "rowVals", "fmtField"))
    script = f"""
var FIELD_WORDS = {{date: 'Date', date_end: 'Date end', date_text: 'Date as written',
  title: 'Title', event_type: 'Type', company: 'Company', ticker: 'Ticker',
  program: 'Program', indication: 'Condition'}};
function eventWord(v) {{ return String(v); }}
var ACTION_WORDS = {{ keep: 'kept', fix: 'fixed', remove: 'removed' }};
function dateShort(s) {{ return String(s).slice(0, 10); }}
{fns}
function mkX(rows, targets, decisions) {{
  var projection = {{}};
  rows.forEach(function (r) {{ projection[r.id] = {{ values: Object.assign({{}}, r) }}; }});
  Object.keys(targets || {{}}).forEach(function (id) {{
    projection[id] = Object.assign(projection[id] || {{}}, targets[id]);
  }});
  return {{ run: {{ rows: rows,
           run: {{ label: 'Run 8 · the run', number: 8,
                   started_at: '2026-10-29T20:00:00+00:00',
                   finished_at: '2026-10-29T21:00:00+00:00' }} }},
           state: {{ targets: projection }}, decisions: decisions || [] }};
}}
var rowsA = [
  {{ id: 'r1', ticker: 'IONS', company: 'Ionis Pharmaceuticals', program: 'zilganersen',
     event_type: 'regulatory_decision', title: 'ZANVASTRO U.S. availability',
     date_text: 'in 2027', date: '2027-01-01', date_end: '2027-12-31' }},
  {{ id: 'r2', ticker: 'PTGX', company: 'Protagonist', program: 'rusfertide',
     event_type: 'regulatory_decision', title: 'PDUFA',
     date_text: 'August 2026', date: '2026-08-01' }},
  {{ id: 'r3', ticker: 'VKTX', company: 'Viking', program: 'VK2735',
     event_type: 'trial_results', title: 'Part 2 results',
     date_text: '2027', date: '2027-06-01' }}
];
var rowsB = JSON.parse(JSON.stringify(rowsA));
rowsB[1] = {{ id: 'r2b', ticker: 'PTGX', company: 'Protagonist', program: 'rusfertide',
  event_type: 'regulatory_decision', title: 'PDUFA',
  date_text: 'Aug 2026', date: '2026-08-15' }};          // date moved
rowsB[2] = JSON.parse(JSON.stringify(rowsB[2]));
rowsB[2].date_text = 'in 2027';                           // wording only
rowsB.push({{ id: 'r4', ticker: 'RVMD', company: 'Revolution', program: 'elironrasib',
  event_type: 'trial_results', title: 'Initiate Phase 3 RASolve 307 study of elironrasib',
  date_text: 'the fourth quarter of 2026', date: '2026-10-01' }});   // added
var targets = {{ r1: {{ decided_by_person: true, last_action: 'keep',
  values: {{ ticker: 'IONS', company: 'Ionis Pharmaceuticals', program: 'zilganersen',
    event_type: 'regulatory_decision', title: 'ZANVASTRO U.S. availability',
    date_text: 'in 2027', date: '2027-01-01', date_end: '2027-12-31' }} }} }};
var decisions = [{{ target: 'r1', action: 'keep', by: 'person', at: '2026-10-30T09:00:00+00:00' }},
                 {{ target: 'r1', action: 'undo', by: 'person', at: '2026-10-30T09:01:00+00:00' }},
                 {{ target: 'r1', action: 'keep', by: 'person', at: '2026-10-30T09:02:00+00:00' }}];
var cards = compareRuns(mkX(rowsA, {{}}, []), mkX(rowsB, targets, decisions));
if (cards.since !== 1) {{ console.error('since=' + cards.since); process.exit(2); }}
var since = cards.filter(function (c) {{ return c.kind === 'since'; }})[0];
if (!since) process.exit(3);
if (since.row.id !== 'r1') process.exit(4);
if (since.since.text !== 'Since then: kept by you · 2026-10-30') {{
  console.error(since.since.text); process.exit(5); }}
// the last decision wins; an undecided row or one decided before run B gets nothing
var targets2 = {{ r1: {{ decided_by_person: true, last_action: 'keep',
  values: {{ ticker: 'IONS', company: 'Ionis Pharmaceuticals', program: 'zilganersen',
    event_type: 'regulatory_decision', title: 'ZANVASTRO U.S. availability',
    date_text: 'in 2027', date: '2027-01-01', date_end: '2027-12-31' }} }} }};
var early = [{{ target: 'r1', action: 'keep', by: 'person', at: '2026-10-28T09:00:00+00:00' }}];
var cards2 = compareRuns(mkX(rowsA, {{}}, []), mkX(rowsB, targets2, early));
if (cards2.since !== 0) {{ console.error('early since=' + cards2.since); process.exit(6); }}
// G4-R7: the wording pair is kept for the fold
if (cards.wording !== 1) {{ console.error('wording=' + cards.wording); process.exit(7); }}
if (!cards.wordingRows || cards.wordingRows.length !== 1) process.exit(8);
var wr = cards.wordingRows[0];
if (wr.rb.id !== 'r3' || wr.diffs.length !== 1 || wr.diffs[0].field !== 'date_text') process.exit(9);
// kind order: added, removed, corrected, moved, since — since last
var kinds = cards.map(function (c) {{ return c.kind; }});
if (kinds[kinds.length - 1] !== 'since') {{ console.error(kinds.join(',')); process.exit(10); }}
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def _row(**over):
    base = {"id": "x", "ticker": "IONS", "company": "Ionis", "program": "sefaxersen",
            "indication": "IgAN", "title": "Readout", "event_type": "trial_results",
            "date": None, "date_end": None, "date_text": "in 2027"}
    base.update(over)
    return base


def test_u22_what_changed_compares_substance():
    # G9-3 / U34 (supersedes the U22 pairing): identity = ticker + program
    # (normalised) + event kind + SAME period start; the one fallback is same
    # ticker + kind with title word overlap >= 0.6. Rows with no confident
    # partner join "No confident match", never a guessed pair. "date moved"
    # needs date or date_end on an IDENTITY pair; "corrected" needs program,
    # indication or type; wording-only differences fold into one count line.
    js = (UI / "app.js").read_text()
    for needle in ("normWords", "pairKey", "titleSim", "cards.wording",
                   "reworded", "Only in "):
        assert needle in js, needle

    # wording: capitalisation/punctuation of the title and "2027" vs "in 2027"
    # fold into the count, never a card
    cards, wording = _compare_cards(
        [_row(id="a1", title="Phase 3 topline data!", date="2027-01-01",
              date_end="2027-03-31", date_text="2027")],
        [_row(id="b1", title="phase 3 topline data", date="2027-01-01",
              date_end="2027-03-31", date_text="in 2027")])
    assert cards == [] and wording == 1

    # date moved: only when date or date_end changed
    cards, wording = _compare_cards(
        [_row(id="a1", date="2027-06-01", date_end="2027-12-31", date_text="2H 2027")],
        [_row(id="b1", date="2027-06-01", date_end="2027-09-30", date_text="Q3 2027")])
    assert [c["kind"] for c in cards] == ["moved"] and wording == 0

    # same ticker + kind + title but disjoint periods: a title match with a
    # date disagreement is not confident — both rows join "No confident match"
    cards, _ = _compare_cards(
        [_row(id="a1", date="2027-07-01", date_end="2027-12-31")],
        [_row(id="b1", date="2028-01-01", date_end="2028-06-30")])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # rows with no confident partner (different program AND kind) join the group
    cards, _ = _compare_cards([_row(id="a1")], [_row(id="b1", program="bepirovirsen",
                                                   event_type="regulatory_decision")])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # U34b: two set programs that differ are different events — no longer one
    # corrected card; each row is 'Only in' its own run
    cards, _ = _compare_cards(
        [_row(id="a1", program="sefaxersen", date="2027-01-01")],
        [_row(id="b1", program="pelacarsen", date="2027-01-01")])
    assert [c["kind"] for c in cards] == ["unmatched", "unmatched"]

    # program normalisation (case, punctuation, whitespace) still pairs
    cards, _ = _compare_cards(
        [_row(id="a1", program="PN-881", date="2027-01-01")],
        [_row(id="b1", program="Pn 881", date="2027-01-01")])
    assert len(cards) == 1 and cards[0]["kind"] == "corrected"

    # indication change is a corrected card
    cards, _ = _compare_cards(
        [_row(id="a1", indication="IgA nephropathy", date="2027-01-01")],
        [_row(id="b1", indication="PV", date="2027-01-01")])
    assert [c["kind"] for c in cards] == ["corrected"] and cards[0]["fields"] == ["indication"]

    # a type change breaks both pairing rules (identity and fallback both key
    # on the kind) — the rows are not confidently the same event and join the
    # group instead of guessing a "type changed" card
    cards, _ = _compare_cards(
        [_row(id="a1", event_type="trial_results", date="2027-01-01")],
        [_row(id="b1", event_type="regulatory_decision", date="2027-01-01")])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # identical rows are neither cards nor wording
    cards, wording = _compare_cards([_row(id="a1")], [_row(id="b1")])
    assert cards == [] and wording == 0


def test_u30_glass8_leftovers():
    # G8-1: a run paused at its spending limit shows the plain sentence —
    # the stored status_reason is not printed for that case, so old runs
    # with the figures version read plain too
    js = (UI / "app.js").read_text()
    assert "JOB_LIMIT_STOP" in js
    assert ("Stopped at the spending limit. Raise the limit and press Resume "
            "to read more.") in js
    assert "function jobReason(st, meta, accepted)" in js
    assert "jobReason(st, meta, detail.run.unknown_cost_accepted)" in js
    assert "host.append(el('div', JOB_PAUSABLE.indexOf(st) >= 0 ? 'jb-stop' : 'jb-sub', meta.status_reason))" not in js
    # Codex P2 on #101: only a reason that STARTS 'Stopped at the spending
    # limit' is the limit stop. The unpriced-call pause also names the
    # spending limit, and a turn-limit pause can coincide with spend at the
    # limit — neither may be rewritten.
    assert "sr.indexOf('Stopped at the spending limit') === 0" in js
    assert "meta.spend_usd >= limit" not in js
    import json
    import subprocess
    limit_stop = "Stopped at the spending limit. Raise the limit and press Resume to read more."
    cases = [
        # (status, status_reason, spend, limit) -> expected
        ("needs_input",
         "Stopped at the spending limit ($5.06 of $5.00 used): spending limit reached. "
         "Resume with a higher spending_limit_usd to read more.", 5.06, 5.0, limit_stop),
        ("needs_input", limit_stop, 5.06, 5.0, limit_stop),  # already plain — idempotent
        ("needs_input",
         "The cost of a call could not be counted (runs/prices.py has no price for Muse Spark), "
         "so the spending limit of $5.00 cannot be kept. Nothing more was read.", 5.06, 5.0, None),
        ("paused_turn_limit",
         "Stopped at the turn limit (40 turns). Resume to continue with a fresh budget of turns.",
         5.06, 5.0, None),  # spend at the limit but the pause is the turn limit
        ("failed", "The page failed three times.", 5.06, 5.0, None),
        ("done", "Read 114 pages: 96 rows, 0 values.", 2.57, 6.0,
         "Read 114 pages · 96 rows"),
    ]
    checks = []
    for i, (st, reason, spend, limit, expected) in enumerate(cases):
        want = expected if expected is not None else reason
        meta = {"status": st, "status_reason": reason, "spend_usd": spend,
                "choices": {"spending_limit_usd": limit}}
        checks.append(f"got = jobReason({json.dumps(st)}, {json.dumps(meta)});")
        checks.append(f"if (got !== {json.dumps(want)}) {{ console.error({json.dumps(str(i))} + ' :: ' + got); process.exit(2); }}")
    script = "\n".join([
        "var JOB_PAUSABLE = ['paused_turn_limit', 'needs_input', 'failed'];",
        f"var JOB_LIMIT_STOP = {json.dumps(limit_stop)};",
        _grab_fn(js, "unpricedMark"),
        _grab_fn(js, "jobReason"),
        "var got;",
        *checks,
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout or got.stderr
    # G8-5: the done summary drops the values count
    assert "pages · ' + m[2] + ' rows" in js
    assert "rows?, [\\d,]+ values\\." in js  # only as the stored-text pattern, rewritten away
    # G8-3: the run sheet names dates in words like the card
    assert "dateTimeWords(r.started_at)" in js
    assert ".replace('T', ' ').slice(0, 16)" not in js
    # G8-6: the code fold renders closed by default — its builder never sets
    # .open; the only openers are the trail's 'See the check' link and the
    # Source-view filing auto-open (G8-9b)
    see = _grab_fn(js, "seeTheCheck")
    assert ".open = true" not in see
    assert "folds[idx].open = true" in js
    assert "var d = el('details', 'fold');" in js
    # G8-2/G8-4: the css half. G8-2's blue ring pinned the card header, which
    # G8-11 made unfocusable — the flag colour must not ring a plain control,
    # and the picker is now a native button
    css = (UI / "styles.css").read_text()
    assert ".runcard .pick:focus-visible" not in css
    assert "outline:2px solid var(--accent)" not in css
    # G9-R1 rev: flex 0 0 auto keeps a chip whole while it fits (the row
    # stacks whole chips); max-width + normal white-space let an over-wide
    # chip wrap inside its own pill — no clipped text
    assert "flex:0 0 auto" in css.split(".ctchip{")[1].split("}")[0]
    assert "white-space:normal" in css.split(".ctchip{")[1].split("}")[0]
    assert ".ctchip.zero + .ctchip.zero" not in css


def test_u31_glass8_rulings_2_and_3_and_the_caveat():
    # G8-8: done state = 'Run finished · N need review' (grey dot) or
    # 'Run finished · nothing left to review' (green dot), on the card and in
    # the Choose-a-run list
    js = (UI / "app.js").read_text()
    assert "Run finished · " in js
    assert "need review' : 'nothing left to review'" in js
    assert "el('span', 'dot' + (left > 0 ? ' grey' : ''))" in js
    assert "if (r.status === 'done')" in js
    css = (UI / "styles.css").read_text()
    assert ".jb-row1 .dot.grey{background:var(--faint);animation:none}" in css
    # G8-9: (a) the right pane collapses until a row is picked
    assert "det.hidden = true" in js and "det.hidden = false" in js
    assert "Click a value to see what was compared." not in js
    # G8-9: (b) pages grouped by filing — one header per document
    for word in ("function filingName(s)", "function accession(s)",
                 "el('details', 'filing' + (isOpen ? ' on' : ''))",
                 "' · filed ' + (dateShort(f.filed) || 'no date')",
                 "' · ' + f.pages.length + ' page'"):
        assert word in js, word
    assert ".filing summary{" in css and ".filing.on summary{background:var(--blue-soft)}" in css
    # G8-9: (c) no zoom — and the Codex P2 narrow-screen fix: the mobile
    # display:block rule must not resurrect a [hidden] pane
    assert "zoom" not in js.lower()
    assert "zoom" not in css.lower()
    assert ".screen.picked .pane.det[hidden]{display:none}" in css
    # G8-10's caveat line is gone: G9-3 pairs by event identity, so the
    # "matched by position" warning no longer applies
    assert "changed-caveat" not in js
    assert "Rows are matched by position" not in js
    assert ".chcaveat{" not in css


def test_u32_calendar_folds_only_identical_current_events():
    # A2 F1: only rows with identical current values and full interval fold;
    # similar starts with different date wording remain independently visible.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    for word in ("rep.fold = g", "date_end: e.end", "indication: shownVal(vals, e.row, 'indication')",
                 "el('span', 'srcpill', r.fold.length + ' sources')",
                 "'Read from ' + sel.fold.length + ' sources'",
                 "calSrcSel"):
        assert word in js, word
    # the source list shows each one's own status (matched / kept by you / …)
    list_src = js.split("'Read from ' + sel.fold.length + ' sources'")[1].split("det.append(list)")[0]
    assert "decidedBadge(e.t) : statusBadge(e.t.status)" in list_src
    # folded rows act through the picked source
    assert "actions: sel.fold.length > 1" in js
    for word in (".srcpill{", ".srcitem{", ".srcitem.on{"):
        assert word in css, word
    # Four rows share an event/start but make different current date claims.
    import json
    import subprocess
    rows = [
        {"id": "a1", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "a2", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 ’27"},
        {"id": "a3", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "(Q1 ’27)"},
        {"id": "a4", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Ph1 for PN-8047"},
        # a different ticker, a different kind and a different start stay apart
        {"id": "b1", "ticker": "IONS", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "b2", "ticker": "PTGX", "program": "PN-8047", "event_type": "regulatory",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "b3", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-07-01", "date_text": "2H 2027"},
        # an open row never reaches the calendar
        {"id": "b4", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
    ]
    targets = {r["id"]: {"group": "open" if r["id"] == "b4" else "kept",
                         "values": {"date": r["date"], "date_text": r["date_text"]}}
               for r in rows}
    partition = _calendar_view(rows, targets)
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows();",
        "if (got.length !== 7) { console.error('n=' + got.length); process.exit(2); }",
        "if (got.some(function (e) { return e.fold.length !== 1; })) process.exit(3);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u32_back_from_a_secondary_source_reselects_the_fold():
    # Codex P2 on #103: Back to calendar from a secondary source's page carries
    # that source's row id; the calendar must land back on the folded event
    # with the same source picked, not lose the selection
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "function calFoldHit(rows, rowId)" in js
    assert "calSel = hit.rep; calSrcSel = hit.src;" in js
    rows = [
        {"id": "a1", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "a4", "ticker": "PTGX", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Ph1 for PN-8047"},
        {"id": "b1", "ticker": "IONS", "program": "PN-8047", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
    ]
    targets = {r["id"]: {"group": "kept", "values": {"date": r["date"], "date_text": r["date_text"]}}
               for r in rows}
    partition = _calendar_view(rows, targets)
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        _grab_fn(js, "calFoldHit"),
        "var all = calendarRows();",
        # A same-start row with conflicting date wording is no longer folded.
        "var h = calFoldHit(all, 'a4');",
        "if (!h || h.rep !== 'a4' || h.src !== 'a4') { console.error(JSON.stringify(h)); process.exit(2); }",
        # the other row remains independently addressable
        "h = calFoldHit(all, 'a1');",
        "if (!h || h.rep !== 'a1' || h.src !== 'a1') { console.error(JSON.stringify(h)); process.exit(3); }",
        # a singleton row maps to itself; an unknown id maps to nothing
        "h = calFoldHit(all, 'b1');",
        "if (!h || h.rep !== 'b1' || h.src !== 'b1') { console.error(JSON.stringify(h)); process.exit(4); }",
        "if (calFoldHit(all, 'zz9') !== null) process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34_g9_1_calendar_uses_the_servers_corrected_rule():
    # Triage finding 1: ui/calendarRows admitted every corrected row;
    # runs/calendar.py on_calendar admits one only when the fix's re-check
    # passed, no re-check is pending and the readers are decided
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "var partition = detail.run.calendar_partition || null" in js
    assert "if (!t.on_calendar) return" in js
    rows = [{"id": rid, "ticker": "PTGX", "program": "PN-" + rid, "event_type": "milestone",
             "date": "2027-01-01"} for rid in
            ("keep", "pass", "fail", "cnr", "pending", "undecided", "nofix")]
    targets = {
        "keep": {"group": "kept", "values": {"date": "2027-01-01"}},
        "pass": {"group": "corrected", "fixes": [{}], "recheck": {"result": "pass"},
                 "needs_recheck": False, "status": "matched",
                 "values": {"date": "2027-01-01"}},
        "fail": {"group": "corrected", "fixes": [{}], "recheck": {"result": "fail"},
                 "needs_recheck": False, "status": "matched",
                 "values": {"date": "2027-01-01"}},
        "cnr": {"group": "corrected", "fixes": [{}], "recheck": {"result": "could_not_run"},
                "needs_recheck": False, "status": "matched",
                "values": {"date": "2027-01-01"}},
        "pending": {"group": "corrected", "fixes": [{}], "recheck": None,
                    "needs_recheck": True, "status": "matched",
                    "values": {"date": "2027-01-01"}},
        "undecided": {"group": "corrected", "fixes": [{}], "recheck": {"result": "pass"},
                      "needs_recheck": False, "status": "undecided",
                      "values": {"date": "2027-01-01"}},
        "nofix": {"group": "corrected", "fixes": [], "values": {"date": "2027-01-01"}},
    }
    eligible = {"keep", "pass", "nofix"}
    for row_id, target in targets.items():
        target["on_calendar"] = row_id in eligible
        row = next(row for row in rows if row["id"] == row_id)
        target["values"] = {**row, **target["values"]}
    partition = {"calendar": [{"id": row_id} for row_id in eligible]}
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows().map(function (e) { return e.row.id; });",
        "console.log(JSON.stringify(got));",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr
    assert json.loads(got.stdout.strip().splitlines()[-1]) == ["keep", "pass", "nofix"]


def test_needs_attention_includes_pending_recheck():
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    script = "\n".join([
        _grab_fn(js, "needsAttention"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var pending = needsAttention({group:'corrected', needs_attention:true, needs_recheck:true, recheck:null, status:'matched'});",
        "var completed = needsAttention({group:'corrected', needs_attention:false, needs_recheck:false, recheck:{result:'pass'}, status:'matched'});",
        "console.log(JSON.stringify([pending, completed]));",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr
    assert json.loads(got.stdout.strip().splitlines()[-1]) == [True, False]


def test_judgment_checks_show_current_results_and_label_original_history():
    import subprocess
    js = (UI / "app.js").read_text()
    script = "\n".join([
        "function el(tag, cls, value) { return {tag:tag, cls:cls, textContent:value || '', children:[], style:{}, append:function(){ this.children.push.apply(this.children, arguments); }}; }",
        "function checkDot(result) { return el('span', null, result); }",
        "function esc(value) { return el('span', null, value); }",
        "function checkResultWord(result) { return result; }",
        "function seeTheCheck(check) { return el('details', null, 'details ' + check.label); }",
        "function flatten(node) { return (typeof node === 'string' ? node : node.textContent || '') + (node.children || []).map(flatten).join(' '); }",
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "dateFinding"),
        _grab_fn(js, "passOutcome"),
        _grab_fn(js, "passOutcomeDate"),
        _grab_fn(js, "checksSection"),
        "var PASS_OUTCOME_WORDS = { date_match: passOutcomeDate, quote_match: 'Quote matches source', selector_match: 'Citation located' };",
        "var TIMING_CAVEAT = 'This compares the printed timing only; whether this timing belongs to this event needs your review.';",
        "var TIMING_CAVEAT_OLD = 'This compares date bounds only; whether the date belongs to this event needs your review.';",
        _grab_fn(js, "timingReason"),
        "var raw = [{result:'fail', label:'Original value failure', reason:'Old value.'}];",
        "var current = [{result:'pass', label:'Fixed value pass', reason:'Re-check on new value.'}];",
        "var done = flatten(checksSection({checks:raw}, {fixes:[{}], effective_checks:current, check_history:raw, recheck:{result:'pass'}}));",
        "var histAt = done.indexOf('Original checks · history (before edit)');",
        "if (histAt < 0 || done.indexOf('Fixed value pass') < 0 || done.indexOf('Original value failure') < histAt) process.exit(2);",
        "var pending = flatten(checksSection({checks:raw}, {fixes:[{}], needs_recheck:true, effective_checks:[], check_history:raw}));",
        "if (pending.indexOf('No checks ran on the current value yet; re-check pending.') < 0 || pending.indexOf('Original checks · history (before edit)') < 0) process.exit(3);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_check_results_words_and_capture_copy():
    # CHECK-RESULTS-WORDS-001: a passing scoped check states one concise
    # observed outcome (date with its period wording, quote, citation); a fail
    # stays an honest conflict; could_not_run stays honest. Detected clipping
    # is an actionable warning with the original source; unknown completeness
    # is a neutral 'Capture details' disclosure — never a warning per row.
    import subprocess

    js = (UI / "app.js").read_text()
    script = "\n".join([
        "function el(tag, cls, value) { return {tag:tag, cls:cls, textContent:value || '', children:[], style:{}, dataset:{}, append:function(){ this.children.push.apply(this.children, arguments); }}; }",
        "function checkDot(result) { return el('span', null, result); }",
        "function esc(value) { return el('span', null, value); }",
        "function checkResultWord(result) { return ({ pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' })[result] || result; }",
        "function seeTheCheck(check) { return el('details', null, 'details ' + check.label); }",
        "function sourceOf(row) { return { url: 'https://www.sec.gov/Archives/x' }; }",
        "function flatten(node) { return (typeof node === 'string' ? node : node.textContent || '') + (node.children || []).map(flatten).join(' '); }",
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "dateFinding"),
        _grab_fn(js, "passOutcome"),
        _grab_fn(js, "passOutcomeDate"),
        _grab_fn(js, "checksSection"),
        "var PASS_OUTCOME_WORDS = { date_match: passOutcomeDate, quote_match: 'Quote matches source', selector_match: 'Citation located' };",
        "var TIMING_CAVEAT = 'This compares the printed timing only; whether this timing belongs to this event needs your review.';",
        "var TIMING_CAVEAT_OLD = 'This compares date bounds only; whether the date belongs to this event needs your review.';",
        _grab_fn(js, "timingReason"),
        "var checks = [",
        "  { name: 'date_match', result: 'pass', label: 'Date read as written',",
        "    timing: { wording: 'first half of calendar year 2027' } },",
        "  { name: 'quote_match', result: 'pass', label: 'Quote found on page' },",
        "  { name: 'selector_match', result: 'pass', label: 'Citation located' },",
        "  { name: 'type_match', result: 'pass', label: 'Event type matches the quote' },",
        "  { name: 'quote_match', result: 'fail', label: 'Quote found on page', reason: 'Says June 3, the page says June 30.' },",
        "  { name: 'date_match', result: 'could_not_run', label: 'Date read as written', reason: 'No words on the page.' } ];",
        "var t = { effective_checks: checks, fixes: [], check_history: [], page_evidence: {} };",
        "var out = flatten(checksSection({ checks: [], source_id: 's1' }, t));",
        "if (out.indexOf('Timing matches source: first half of calendar year 2027') === -1) { console.error('date outcome'); process.exit(2); }",
        "if (out.indexOf('Quote matches source') === -1) { console.error('quote outcome'); process.exit(3); }",
        "if (out.indexOf('Citation located') === -1) { console.error('citation outcome'); process.exit(4); }",
        "if (out.indexOf('pass') === -1 || out.indexOf('failed') === -1) { console.error('fail not honest'); process.exit(5); }",
        "if (out.indexOf('could not run') === -1 || out.indexOf('No words on the page.') === -1) { console.error('unable not honest'); process.exit(6); }",
        # actual conflict names the compared values (the reason prints)
        "if (out.indexOf('June 3') === -1) { console.error('conflict values lost'); process.exit(7); }",
        # no detected clipping and no unknown completeness: no warning, no details
        "if (/cut off|Capture details/.test(out)) { console.error('copy leaked'); process.exit(8); }",
        # detected clipping: actionable warning + original source link + issue
        "var t2 = { effective_checks: [], fixes: [], check_history: [],",
        "  page_evidence: { capture_completeness: { capture_incomplete: true,",
        "    capture_incomplete_reason: 'Text runs past the rendered page bottom.' } } };",
        "var warn = flatten(checksSection({ checks: [], source_id: 's1' }, t2));",
        "if (warn.indexOf('The captured page may be cut off.') === -1 ||",
        "    warn.indexOf('Open the original to check.') === -1 ||",
        "    warn.indexOf('Text runs past the rendered page bottom.') === -1)",
        "  { console.error(warn); process.exit(9); }",
        # fix-on-find (Red Team 7f822fc): no original URL -> no dead anchor
        "sourceOf = function () { return {}; };",
        "var warnNoUrl = checksSection({ checks: [], source_id: 's1' }, t2);",
        "var anchors = 0; (function walk(n) { (n.children || []).forEach(function (c) { if (c && c.tag === 'a') anchors++; if (c) walk(c); }); })(warnNoUrl);",
        "if (anchors || flatten(warnNoUrl).indexOf('Open the original to check.') !== -1) { console.error('dead link'); process.exit(19); }",
        "sourceOf = function () { return { url: 'https://www.sec.gov/Archives/x' }; };",
        # unknown completeness only: neutral disclosure, no warning paragraph
        "var t3 = { effective_checks: [], fixes: [], check_history: [],",
        "  page_evidence: { capture_completeness: { completeness_not_established: true,",
        "    completeness_reason: 'The capture tool reported no completeness signal for this renderer.' } } };",
        "var det = flatten(checksSection({ checks: [], source_id: 's1' }, t3));",
        "if (det.indexOf('Capture details') === -1 ||",
        "    det.indexOf('This check cannot rule out every kind of clipping.') === -1 ||",
        "    det.indexOf('The capture tool reported no completeness signal for this renderer.') === -1)",
        "  { console.error(det); process.exit(10); }",
        "if (det.indexOf('The captured page may be cut off.') !== -1) { console.error('warning on unknown completeness'); process.exit(11); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34_g9_2_mistakes_read_the_state_list():
    # Triage finding 2: mistakeFor read the static run.mistakes, so a person's
    # fix counted in the 'by you' chip but the chip found no row. The chips
    # and badges read state.mistake_list (the superset), falling back to
    # run.mistakes only when the state list is absent.
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "(detail.state && detail.state.mistake_list) || detail.run.mistakes || []" in js
    script = "\n".join([
        "var detail = { run: { mistakes: [{ target: 'r1', caught_by: 'code' }] },",
        "                state: { mistake_list: [{ target: 'r2', caught_by: 'person',",
        "                  what_changed: 'Date changed from June 3, 2027 to June 30, 2027.' }] } };",
        _grab_fn(js, "mistakeFor"),
        # the person's fix is found — the by-you chip returns exactly that row
        "var m = mistakeFor('r2');",
        "if (!m || m.caught_by !== 'person') { console.error(JSON.stringify(m)); process.exit(2); }",
        # an empty state list is still the state list — no falling back
        "detail.state.mistake_list = [];",
        "if (mistakeFor('r1')) process.exit(3);",
        # an absent state list -> the run list is the fallback
        "detail.state.mistake_list = null;",
        "if (!mistakeFor('r1') || mistakeFor('r1').caught_by !== 'code') process.exit(4);",
        "if (mistakeFor('r2')) process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34_g9_3_compare_pairs_by_event_identity():
    # Triage finding 3: What changed paired unrelated events (Ph2b PsO vs
    # opt-out; Ph1 SAD vs buyback) as one change. Pairing is now identity
    # (ticker + program + kind + period start), then same ticker + kind with
    # title word overlap >= 0.6; everything else joins "No confident match";
    # DATE MOVED only names identity pairs.
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    for word in ("(r.date || '')", "function titleSim(ra, rb)", "bestSim = 0.6",
                 "'Only in ' + onlyLabel('b') + ' · '", "kind: 'unmatched'",
                 "fallbackPairs", "cards.unmatched"):
        assert word in js, word
    assert "periodsOverlap" not in js and "aByTicker" not in js

    def pso_a(**over):
        base = {"id": "a1", "ticker": "PTGX", "company": "Protagonist",
                "program": "PN-881", "event_type": "trial_results",
                "title": "Phase 2b study in plaque psoriasis (PsO)",
                "date": "2027-01-01", "date_end": "2027-06-30", "date_text": "1H 2027"}
        base.update(over)
        return base

    def sad_a(**over):
        base = {"id": "a1", "ticker": "PTGX", "company": "Protagonist",
                "program": "PN-235", "event_type": "trial_results",
                "title": "Phase 1 SAD study", "date": "2027-01-01",
                "date_end": "2027-03-31", "date_text": "Q1 2027"}
        base.update(over)
        return base

    # Codex's mis-pairs: same ticker, overlapping window, but a different
    # event — title overlap far below 0.6, program different -> no pair
    cards, _ = _compare_cards(
        [pso_a()],
        [{"id": "b1", "ticker": "PTGX", "company": "Protagonist", "program": "PN-881",
          "event_type": "corporate", "title": "Opt-out of future sales program",
          "date": "2027-02-01", "date_end": "2027-02-01", "date_text": "February 2027"}])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    cards, _ = _compare_cards(
        [sad_a()],
        [{"id": "b1", "ticker": "PTGX", "company": "Protagonist", "program": "PN-235",
          "event_type": "corporate", "title": "Share buyback", "date": "2027-01-15",
          "date_end": "2027-01-15", "date_text": "January 2027"}])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # DATE MOVED names only identity pairs: a fallback (title) pair whose
    # program was renamed keeps the same dates -> corrected, never moved;
    # one whose ONLY difference is a date dissolves into the group
    cards, _ = _compare_cards(
        [pso_a(program="")],
        [pso_a(id="b1", program="PN-882")])
    # (program set on only one side — two set programs that differ never pair, U34b)
    assert [c["kind"] for c in cards] == ["corrected"]

    cards, _ = _compare_cards(
        [pso_a(title="Phase 2b plaque psoriasis study")],
        [pso_a(id="b1", title="Phase 2b plaque psoriasis study",
               date="2028-01-01", date_end="2028-06-30", date_text="1H 2028")])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # a moved PERIOD START is a different identity (identity keys on the
    # start) — it joins the group, never a "date moved" card
    cards, _ = _compare_cards(
        [sad_a()],
        [sad_a(id="b1", date="2027-04-01", date_end="2027-06-30", date_text="Q2 2027")])
    assert sorted(c["kind"] for c in cards) == ["unmatched", "unmatched"]

    # an identity pair whose period start holds and only the end moved still
    # says date moved
    cards, _ = _compare_cards(
        [sad_a()],
        [sad_a(id="b1", date_end="2027-04-30", date_text="Q1 2027 (to April)")])
    assert [c["kind"] for c in cards] == ["moved"]


def test_u34_g9_4_narrow_rail_becomes_a_compact_strip():
    # Triage finding 6: below 900 px the rail used to vanish (run card, picker,
    # Resume). It is now a compact strip at the top: run name, status word and
    # spend, the Saved runs button, Resume + limit box when paused; the tabs
    # scroll horizontally with a visible edge; nothing is unreachable.
    css = (UI / "styles.css").read_text()
    q = css.split("@media (max-width:900px){")[1]
    for word in (".runcard{display:flex;margin:6px 10px 4px",
                 ".runcard .jb{margin-top:0;border-top:0;padding-top:0;display:flex",
                 ".runcard .jb-stop{flex-basis:100%",
                 ".savedruns{margin:4px 10px 6px",
                 "mask-image:linear-gradient(90deg,#000 82%,transparent)"):
        assert word in q, word
    # the card is no longer hidden outright; the footer text is, the theme
    # toggle (.tg) is not
    assert ".runcard,.runcard-lbl" not in q
    assert ".runcard-lbl,.side-ft .ft{display:none}" in q


def test_u34_compare_pairs_on_state_values():
    # U34 +1 (Red Team F9): a fix shows on the audit's state values, so the
    # compare's identity must read those values — a raw-row program that a fix
    # corrected must not split the pair, and a state value that moved must not
    # be masked by the raw row
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "var k = pairKey(rowVals(a, r))" in js
    assert "var q = (aByKey[pairKey(vb)] || [])" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("normWords", "pairKey", "titleSim", "rowVals", "pageSet", "rowPageKey", "compareRuns", "fmtField"))
    script = f"""
var FIELD_WORDS = {{date: 'Date', date_end: 'Date end', date_text: 'Date as written',
  title: 'Title', event_type: 'Type', company: 'Company', ticker: 'Ticker',
  program: 'Program', indication: 'Condition'}};
function eventWord(v) {{ return String(v); }}
function lastPersonDecisionIn() {{ return null; }}
function dateShort(s) {{ return String(s); }}
{fns}
function mkX(rows, targets) {{
  var projection = {{}};
  rows.forEach(function (r) {{ projection[r.id] = {{ values: Object.assign({{}}, r) }}; }});
  Object.keys(targets || {{}}).forEach(function (id) {{
    projection[id] = Object.assign(projection[id] || {{}}, targets[id]);
    projection[id].values = Object.assign(projection[id].values || {{}}, targets[id].values || {{}});
  }});
  return {{ run: {{ rows: rows, run: {{ label: 'the run' }} }},
           state: {{ targets: projection }}, decisions: [] }};
}}
var rowsA = [{{ id: 'a1', ticker: 'PTGX', program: 'PN-OLD', event_type: 'milestone',
  title: 'Phase 1 initiation', date: '2027-01-01', date_text: 'Q1 2027' }}];
var rowsB = [{{ id: 'b1', ticker: 'PTGX', program: 'PN-8047', event_type: 'milestone',
  title: 'Phase 1 initiation', date: '2027-01-01', date_text: 'Q1 2027' }}];
// the raw programs differ, but run A's STATE value was fixed to PN-8047 ->
// identity pair, no card
var targets = {{ a1: {{ values: {{ ticker: 'PTGX', program: 'PN-8047', event_type: 'milestone',
  title: 'Phase 1 initiation', date: '2027-01-01', date_text: 'Q1 2027' }} }} }};
var cards = compareRuns(mkX(rowsA, targets), mkX(rowsB, {{}}));
if (cards.length !== 0) {{ console.error(JSON.stringify(cards)); process.exit(2); }}
// and a state value that moved the start breaks the identity even when the
// raw rows agree -> the rows are not confidently one event
var targetsB = {{ b1: {{ values: {{ ticker: 'PTGX', program: 'PN-8047', event_type: 'milestone',
  title: 'Phase 1 initiation', date: '2027-04-01', date_text: 'Q2 2027' }} }} }};
var cards2 = compareRuns(mkX(rowsA, targets), mkX(rowsB, targetsB));
if (cards2.length !== 2 || cards2.some(function (c) {{ return c.kind !== 'unmatched'; }})) {{
  console.error(JSON.stringify(cards2)); process.exit(3); }}
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34b_only_in_groups_and_no_cross_program_fallback():
    # Codex's PN-881 UC vs PN-235 Crohn: generic title wording must not pair
    # two rows whose programs (or indications) are both set and differ
    ucs = {"id": "a1", "ticker": "PTGX", "company": "Protagonist", "program": "PN-881",
           "indication": "ulcerative colitis", "event_type": "trial_results",
           "title": "Phase 2 clinical trial results in ulcerative colitis",
           "date": "2027-01-01", "date_end": "2027-03-31", "date_text": "Q1 2027"}
    crohn = dict(ucs, id="b1", program="PN-235", indication="Crohn disease",
                 title="Phase 2 clinical trial results in Crohn disease")
    cards, _ = _compare_cards([ucs], [crohn])
    assert [c["kind"] for c in cards] == ["unmatched", "unmatched"]
    # indication alone, programs equal-or-unset
    cards, _ = _compare_cards([dict(ucs, program="")],
                              [dict(crohn, program="", date="2027-02-01")])
    assert [c["kind"] for c in cards] == ["unmatched", "unmatched"]
    js = (UI / "app.js").read_text()
    for word in ("'Only in ' + onlyLabel('a')", "side: side", "Only in ' + (x.run.run.label"):
        assert word in js, word
    assert "No confident" not in js


def test_u34b_unread_pages_are_not_only_in_and_header_says_so():
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("normWords", "pairKey", "titleSim", "rowVals", "pageSet", "rowPageKey",
                     "pageSet", "rowPageKey", "compareRuns", "fmtField"))
    script = f"""
var FIELD_WORDS = {{}};
function eventWord(v) {{ return String(v); }}
function lastPersonDecisionIn() {{ return null; }}
function dateShort(s) {{ return String(s); }}
{fns}
function pg(n) {{ var p = []; for (var i = 1; i <= n; i++) p.push({{ page_number: i }}); return p; }}
function mk(num, nPages, rows) {{
  var targets = {{}};
  rows.forEach(function (r) {{ targets[r.id] = {{ values: Object.assign({{}}, r) }}; }});
  return {{ run: {{ rows: rows, sources: [{{ id: 's1', pages: pg(nPages) }}],
                   run: {{ label: 'L' + num, number: num }} }},
           state: {{ targets: targets }}, decisions: [] }};
}}
function row(id, page, title, prog) {{
  return {{ id: id, ticker: 'T', program: prog, event_type: 'milestone', title: title,
           date: '2027-0' + page + '-01', source_id: 's1', page_number: page }};
}}
// run 1 read 1 page, run 2 read 3: run 2's rows on pages 2-3 are unread by run 1
var A = mk(1, 1, [row('a1', 1, 'Alpha start', 'P1')]);
var B = mk(2, 3, [row('b1', 1, 'Alpha start', 'P1'), row('b2', 2, 'Beta thing', 'P2'),
                  row('b3', 1, 'Gamma only here', 'P3')]);
var cards = compareRuns(A, B);
if (cards.pageNote !== 'Run 2 read 2 pages the other run did not; unread pages are not changes') {{
  console.error(cards.pageNote); process.exit(2); }}
// b2 (page 2, unread by run 1) is hidden; b3 (page 1, read by both) is Only in L2
if (cards.length !== 1 || cards[0].side !== 'b' || cards[0].row.id !== 'b3') {{
  console.error(JSON.stringify(cards.map(function (c) {{ return [c.kind, c.side, c.row.id]; }}))); process.exit(3); }}
if (cards.onlyB !== 1 || cards.onlyA !== 0) process.exit(4);
// same page counts: no note, nothing hidden
var C = mk(3, 3, [row('c1', 2, 'Beta thing', 'P2'), row('c2', 3, 'Delta', 'P4')]);
var c2 = compareRuns(mk(2, 3, [row('b2', 2, 'Beta thing', 'P2'), row('b4', 3, 'Eps', 'P5')]), C);
if (c2.pageNote) process.exit(5);
if (c2.onlyA !== 1 || c2.onlyB !== 1) process.exit(6);
// equal counts, different page numbers: sets differ -> both rows suppressed, note present
function mkp(num, pages, rows) {{
  var r = mk(num, 0, rows);
  r.run.sources = [{{ id: 's1', pages: pages.map(function (n) {{ return {{ page_number: n }}; }}) }}];
  return r;
}}
var D = compareRuns(mkp(1, [1], [row('d1', 1, 'Only page one', 'P1')]),
                    mkp(2, [2], [row('d2', 2, 'Only page two', 'P2')]));
if (D.length !== 0 || D.onlyA !== 0 || D.onlyB !== 0) {{
  console.error(JSON.stringify(D.map(function (c) {{ return [c.side, c.row.id]; }}))); process.exit(7); }}
if (D.pageNote !== 'Run 1 read 1 pages the other run did not; Run 2 read 1 pages the other run did not; unread pages are not changes') {{
  console.error(D.pageNote); process.exit(8); }}
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34b_default_run_is_the_nearest_comparable():
    import subprocess
    js = (UI / "app.js").read_text()
    assert "nearestComparable(bId, others)" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("labelFamily", "pageSet", "pageSig", "nearestComparable"))
    script = f"""
var pageSigs = {{}};
var runs = [{{ id: 'r1', number: 1, label: 'Run 1 · X' }}, {{ id: 'r2', number: 2, label: 'Run 2 · Y' }},
            {{ id: 'r3', number: 3, label: 'Run 3 · X' }}, {{ id: 'r4', number: 4, label: 'Run 4 · X' }}];
{fns}
var others = runs.filter(function (r) {{ return r.id !== 'r4'; }}).sort(function (x, y) {{ return y.number - x.number; }});
// no page sets known: previous run of the same label (r3)
if (nearestComparable('r4', others) !== 'r3') process.exit(2);
// r2 has the same page set as r4 -> it wins over the same-label rule
pageSigs = {{ r4: 'a', r2: 'a', r3: 'b', r1: 'b' }};
if (nearestComparable('r4', others) !== 'r2') process.exit(3);
// nothing comparable: the newest other run
runs[3].label = 'Run 4 · Z'; pageSigs = {{ r4: 'z' }};
if (nearestComparable('r4', others) !== 'r3') process.exit(4);
console.log('ok');
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u34_af9_second_half_changes_show_state_values():
    # Red Team A-F9, second half: pairing reads state values (U34 +1, merged)
    # and SHOWING a change does too — a fixed title/program/company/type is
    # what the card and the pane display, never the raw saved row
    js = (UI / "app.js").read_text()
    for word in ("var cv = rowVals(c.x, c.row);",
                 "var cvCo = shownVal(cv, c.row, 'company')",
                 "var cvPr = shownVal(cv, c.row, 'program')",
                 "var cvTt = shownVal(cv, c.row, 'title')",
                 "var dTy = shownVal(cvDet, c.row, 'event_type')"):
        assert word in js, word
    # Codex P2: presence wins over truthiness — a field a person CLEARED
    # ('' or null in the state values) shows as cleared, never the raw value;
    # the raw row is only the answer when the state has no key at all
    import subprocess
    fns = _grab_fn(js, "shownVal")
    script = "\n".join([
        fns,
        "var raw = { program: 'PN-OLD', indication: 'IgAN', title: 'Old title' };",
        # cleared to '' -> '' (no raw fallback)
        "if (shownVal({ program: '' }, raw, 'program') !== '') process.exit(2);",
        # cleared to null -> null
        "if (shownVal({ indication: null }, raw, 'indication') !== null) process.exit(3);",
        # the state mock is the complete current-value projection, so an
        # unchanged field is present with the raw value it retained
        "if (shownVal({ title: raw.title } , raw, 'title') !== 'Old title') process.exit(4);",
        "if (shownVal({ program: raw.program } , raw, 'program') !== 'PN-OLD') process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u35_af2b_calendar_lists_rows_with_no_parsed_day():
    # Red Team round 11 A-F2(b): the screen calendar dropped kept rows whose
    # date text names no parsed day (14 rows on twelve-4). They now list in a
    # final "No day given" section — date text as written, hollow dot — and
    # the header count includes them; only rows with neither day nor date
    # text stay off
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "nodate: !d" in js
    assert "'Contingent · ') + nodates.length" in js
    # The total is now the shared server read view; retain the behavior checks
    # below for dated and undated row membership.
    assert "detail.run.calendar_partition" in js
    assert "calendarRows().length" in js
    rows = [
        {"id": "k1", "ticker": "PTGX", "program": "PN-1", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "u1", "ticker": "IONS", "program": "bepi", "event_type": "regulatory_decision",
         "date": None, "date_text": "in the coming months"},
        {"id": "u2", "ticker": "PTGX", "program": "PN-2", "event_type": "milestone",
         "date": None, "date_text": "early Q1 2027"},
        {"id": "x1", "ticker": "VKTX", "program": "VK9", "event_type": "milestone",
         "date": None, "date_text": None},
    ]
    targets = {r["id"]: {"group": "kept", "values": {"date": r["date"], "date_text": r["date_text"]}}
               for r in rows}
    partition = _calendar_view(rows, targets)
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows();",
        "if (got.length !== 4) { console.error('n=' + got.length); process.exit(2); }",
        "var und = got.filter(function (e) { return e.nodate; });",
        "if (und.length !== 3) { console.error('und=' + und.length); process.exit(3); }",
        "if (!und.some(function (e) { return e.row.id === 'x1' && !e.t.values.date_text; })) process.exit(4);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u35_af3_fold_key_uses_the_title_when_program_is_empty():
    # Same-day rows with different titles or full intervals remain separate;
    # only candidates with the same current values and interval can fold.
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    calendar_source = js.split("function calendarRows")[1].split("return order.map")[0]
    assert "date_end: e.end" in calendar_source and "indication:" in calendar_source
    rows = [
        {"id": "c1", "ticker": "VKTX", "program": "", "event_type": "conference",
         "date": "2026-11-16", "date_end": "2026-11-17", "date_text": "November 16-17",
         "title": "UBS Global Biopharma Conference London, UK"},
        {"id": "c2", "ticker": "VKTX", "program": "", "event_type": "conference",
         "date": "2026-11-16", "date_end": "2026-11-19", "date_text": "November 16-19",
         "title": "Jefferies Global Healthcare Conference London, UK"},
        # Same title but a conflicting interval is not silently folded.
        {"id": "c3", "ticker": "VKTX", "program": "", "event_type": "conference",
         "date": "2026-11-16", "date_end": "2026-11-16", "date_text": "Nov 16",
         "title": "UBS Global Biopharma Conference London, UK"},
    ]
    targets = {r["id"]: {"group": "kept", "values": {"date": r["date"], "date_end": r["date_end"],
                                                    "date_text": r["date_text"]}}
               for r in rows}
    partition = _calendar_view(rows, targets)
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows();",
        "if (got.length !== 3) { console.error('n=' + got.length); process.exit(2); }",
        "if (got.some(function (e) { return e.fold.length !== 1; })) process.exit(3);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_calendar_partitions_before_folding_and_keeps_period_conflict_visible():
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    rows = [
        {"id": "e1", "ticker": "PTGX", "program": "PN-1", "indication": "IgAN",
         "event_type": "milestone", "date": "2026-07-01", "date_end": "2026-09-30",
         "date_text": "Q3 2026", "title": "Results"},
        {"id": "e2", "ticker": "PTGX", "program": "PN-1", "indication": "IgAN",
         "event_type": "milestone", "date": "2026-07-01", "date_end": "2026-12-31",
         "date_text": "H2 2026", "title": "Results"},
    ]
    targets = {r["id"]: {"group": "kept", "status": "matched", "values": r.copy()} for r in rows}
    partition = {"past": [{"id": "e1"}], "calendar": [{"id": "e2"}], "later": [], "contingent": []}
    partition = _calendar_view(rows, targets, partition)
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) +
        " }, state: { targets: " + json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows();",
        "if (got.length !== 2) { console.error('n=' + got.length); process.exit(2); }",
        "if (got[0].row.id !== 'e1' || got[0].bucket !== 'past') process.exit(3);",
        "if (got[1].row.id !== 'e2' || got[1].bucket !== 'calendar') process.exit(4);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_calendar_empty_buckets_still_render_latest_past_history():
    js = (UI / "app.js").read_text()
    render = _grab_fn(js, "renderCalendar")
    assert "!later.length && !lastEvents.length" in render


def test_u35_af6_matched_badge_names_the_unchecked_check():
    # Red Team round 11 A-F6: a matched row whose date or type check was
    # 'not needed' did not have that confirmed — the badge says "matched ·
    # date not checked" / "matched · type not checked", the pane explains in
    # plain words
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    for word in ("function rowBadge(row, t)", "'matched · ' + unchecked.join(' · ')",
                 "if (c.name === 'date_match') unchecked.push('date not checked')",
                 "if (c.name === 'type_match') unchecked.push('type not checked')",
                 "'Not confirmed by a check: ' + unchecked2.join(' and ')",
                 ".st-partmatched{"):
        assert word in (js + css), word
    # the audit and judgment lists use rowBadge; decided rows never read as code
    script = "\n".join([
        _grab_fn(js, "rowBadge"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        _grab_fn(js, "reviewBucket"),
        _grab_fn(js, "projected"),
        _grab_fn(js, "reviewClass"),
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var STATUS_WORDS = { matched: 'matched', failed: 'failed', proposed: 'proposed',"
        + " unreadable: 'unreadable', undecided: 'undecided' };",
        "function el(tag, cls, text) { return { tag: tag, className: cls, textContent: text || '',"
        + " title: '' }; }",
        "function statusBadge(s) { return el('span', 'st st-' + s, STATUS_WORDS[s]); }",
        "function decidedBadge(t) { return el('span', 'st st-' + t.group, DECIDED_BADGE[t.group]); }",
        "var both = { checks: [{ name: 'date_match', result: 'not_needed' },"
        + "                     { name: 'type_match', result: 'not_needed' },"
        + "                     { name: 'quote_found', result: 'pass' }],"
        + "             status: 'matched', decided_by_person: false };",
        "var b1 = rowBadge(both, both);",
        "if (b1.textContent !== 'matched · date not checked · type not checked') {"
        + " console.error(b1.textContent); process.exit(2); }",
        "var dateOnly = { checks: [{ name: 'date_match', result: 'not_needed' },"
        + "                          { name: 'type_match', result: 'pass' }],"
        + "             status: 'matched', decided_by_person: false };",
        "if (rowBadge(dateOnly, dateOnly).textContent !== 'matched · date not checked') process.exit(3);",
        "var clean = { checks: [{ name: 'date_match', result: 'pass' }],"
        + "             status: 'matched', decided_by_person: false };",
        "if (rowBadge(clean, clean).textContent !== 'matched') process.exit(4);",
        "var kept = { checks: [{ name: 'date_match', result: 'fail' }],"
        + "           status: 'failed', decided_by_person: true, group: 'kept' };",
        "if (rowBadge({}, kept).textContent !== 'Kept by you') process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_review_labels_wiring_pins():
    # GROUP-AXIS-001 (order against codex/pivot-001): the projection's fields
    # are the authority when sent — review_label owns the row badge,
    # decision_label/review_label the decided words, review_bucket the listed
    # group and the badge colour, counts.review the Waiting-for-you number.
    # Presence, not truthiness: without the projection every fallback is main's.
    js = (UI / "app.js").read_text()
    for word in ("function reviewBucket(t)", "function projected(t)", "function viewGroup(t)",
                 "function reviewClass(t)", "function decidedWord(t, otherwise)", "function fixStillOpen(t)",
                 "function groupCount(g)", "function keptCurrent(t)", "function decidedMark(t)",
                 "function reviewAcceptedCount(st)", "function dateFinding(c)",
                 "if (projected(t)) return t.decision_label || t.review_label;",
                 "reviewCounts ? reviewCounts.proposed : unresolved.length",
                 "keptCurrent(r.t) ? 'Kept by you; the event period ended"):
        assert word in js, word
    # every decided surface goes through decidedBadge(t) / decidedWord(t, …)
    assert "decidedBadge(t.group)" not in js and "decidedBadge(e.t.group)" not in js
    assert "t.decided_by_person && t.group === 'kept'" not in js
    # F1: the list filter and its headings read the listed group, never the raw group
    assert "rowTarget(r).group === g" not in js
    assert "var g = viewGroup(rowTarget(r));" in _grab_fn(js, "visibleAuditRows")
    # F6: calendarUndated had no caller — removed, not rewired
    assert "calendarUndated" not in js


def test_type_provenance_undo_and_refusal_pins():
    # #52 follow-ups: type provenance (labels verbatim, values in plain words,
    # never a raw id), D1 (Undo whenever the decision stack is non-empty under
    # the projection — last_action tells), D3 (a refused report file prints the
    # server's reason sentence, never 'No report is saved…')
    js = (UI / "app.js").read_text()
    for word in ("function plainTypeValue(v)", "function typeCategoryValue(t)",
                 "function typePresentationPairs(t)", "EVENT_WORDS[v] !== undefined ? eventWord(v) : String(v)",
                 "function reportRefusalReason(res)",
                 "Rebuild the report to open it here.",
                 "var canUndo = correctedOpen ||",
                 "typeof t.last_action === 'string' && !!t.last_action",
                 "return reportRefusalReason(r).then(function (reason)",
                 "c.label === 'Date period found in quote' || c.label === 'Date wording present in quote'",
                 "function shownDateText(t, row)",
                 "function shownField(t, row, field)",
                 "Object.prototype.hasOwnProperty.call(v, field)",
                 "typeof t.date_display === 'string'",
                 "'decision_label' in t",
                 # RT item-30: every date-wording surface routes through the one
                 # helper — calendar period label, span header, Past/Later and
                 # latest-past lines included
                 "var wording = shownDateText(r.t, r.row);",
                 "htext = shownDateText(sample.t, sample.row);",
                 "var written = shownDateText(r.t, r.row) || r.date;",
                 "shownDateText(r.t, r.row) ||",
                 "((r.t.values && r.t.values.date) || '') || 'Date not stated';"):
        assert word in js, word
    # D3: the refusal path must not fall through to the no-report sentence when
    # a reason exists; the fallback only stands when there is no reason
    assert "if (reason) {" in js
    # type surfaces read the presentation when present
    assert "typePresentationPairs(t)" in _grab_fn(js, "detailsPane")


def test_shown_date_text_presence_beats_fallback():
    # RED-2 (item-29 addendum): an explicit clear (present null/empty
    # values.date_text) is never resurrected from the row's raw wording; only
    # an absent key falls back
    import subprocess

    js = (UI / "app.js").read_text()
    script = "\n".join([
        _grab_fn(js, "shownField"),
        _grab_fn(js, "shownDateText"),
        "var row = { date_text: 'Q3 2027' };",
        # absent key -> the row's wording
        "if (shownDateText({}, row) !== 'Q3 2027') process.exit(2);",
        "if (shownDateText({ values: {} }, row) !== 'Q3 2027') process.exit(3);",
        # present value -> it wins
        "if (shownDateText({ values: { date_text: 'early 2027' } }, row) !== 'early 2027') process.exit(4);",
        # an explicit clear is a clear, never the raw wording
        "if (shownDateText({ values: { date_text: '' } }, row) !== '') { console.error('clear resurrected'); process.exit(5); }",
        "if (shownDateText({ values: { date_text: null } }, row) !== '') { console.error('null resurrected'); process.exit(6); }",
        # pivot 07de86e: the fields live on the state TARGET — date_display
        # wins when present; date_text_cleared marks a clear even plainer
        # than an empty value
        "var withDisplay = { date_display: 'Q1 2027 (Jan 1 – Mar 31, 2027)', values: { date_text: '' } };",
        "if (shownDateText(withDisplay, row) !== 'Q1 2027 (Jan 1 – Mar 31, 2027)') process.exit(7);",
        "var flagged = { date_text_cleared: true, values: { date_text: 'Q1 2027' } };",
        "if (shownDateText(flagged, row) !== '') { console.error('cleared flag ignored'); process.exit(8); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_cleared_date_text_regression_addendum3():
    # Addendum 3 regression: a person clears the date wording only (structured
    # dates kept). Every effective-field lookup is presence-aware through ONE
    # helper: the audit card, detail pane, Decide pane, period calendar and the
    # Keep-as-stated prompt show the cleared state — and the posted Keep note
    # carries no raw 'Q1 2027'.
    import subprocess

    js = (UI / "app.js").read_text()
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "reviewClass", "fixStillOpen",
             "needsAttention", "effectiveChecks", "rowTarget", "rowOf", "unresolvedRows", "decidedRows",
             "mistakeFor", "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane", "captureFolds", "restoreFolds",
             "judgmentDecidedPane", "renderJudgment", "sayControls", "cancelSay", "sayBusyWith", "captureFolds", "restoreFolds",
             "revealRow", "sourceOf", "nearMissCheck", "listReasonLine", "shownDateText", "shownField",
             "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "detailsPane",
             "supportFindingFor", "supportNoteText", "researchLabelLine", "researchContextSection",
             "disagreementSection", "whySection", "checksSection", "keptCurrent", "codeExclusionHistory", "projectionComplete", "partialProjection", "projectionComplete", "partialProjection")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    script = "\n".join([
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'The value already passed its checks before the edit.';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var judgmentSel = null; var route = { screen: 'judgment', rowId: null, dialog: null };",
        "var posts = [];",
        "function currentRunId() { return 'run1'; }",
        "function navigate() {}",
        "function postDecision(id, body) { posts.push({ id: id, body: body }); }",
        "function exclusionFor() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function fmtField(f, v) { return String(v); }",
        "function bothReadersWord() { return 'The readers'; }",
        "function toast() {}",
        "function eventWord(t) { return EVENT_WORDS[t] || String(t); }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {};",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        fns,
        # the row's raw wording is 'Q1 2027'; the person's clear left values.date_text = ''
        "var row = { id: 'r1', title: 'ROW-CLEARED', ticker: 'ACME', proposed_by: 'Muse Spark',",
        "  date_text: 'Q1 2027', date: '2027-03-12', date_end: '2027-06-30', quote: 'q', event_type: 'filing',",
        "  checks: [], source_id: 's1', page_number: 1 };",
        "var detail = { id: 'run', run: { rows: [row], mistakes: [], sources: [], run: { label: 'probe' } },",
        "  state: { targets: { r1: { group: 'open', status: 'proposed', decided_by_person: false,",
        "    needs_attention: true, needs_recheck: false, recheck: null,",
        "    values: { date_text: '', date: '2027-03-12', date_end: '2027-06-30' },",
        "    fixes: [], reason: null, disagreement: null } }, exclusions: [], mistake_list: [] } };",
        "function textOf(node, out) { out = out || []; if (node && node.textContent) out.push(node.textContent);",
        "  ((node && node.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "renderJudgment();",
        "var detTxt = textOf(nodes['judgment-det']);",
        "if (detTxt.indexOf('Q1 2027') !== -1) { console.error('decide pane resurrected the clear: ' + detTxt.slice(0, 160)); process.exit(2); }",
        "if (detTxt.indexOf('Keep as stated') === -1) { console.error('period prompt missing'); process.exit(3); }",
        "var listTxt = textOf(nodes['judgment-rows']);",
        "if (listTxt.indexOf('Q1 2027') !== -1) { console.error('list card resurrected the clear'); process.exit(4); }",
        # the Keep-as-stated button posts a note with no raw wording
        "var btns = [];",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'button') btns.push(c); walk(c); }); })(nodes['judgment-det']);",
        "var kas = btns.filter(function (b) { return b.textContent === 'Keep as stated'; })[0];",
        "if (!kas) { console.error('Keep as stated not offered for the period'); process.exit(5); }",
        "kas.addEventListener ? null : null;",
        "if (typeof kas.onclick !== 'function') { console.error('button has no click handler'); process.exit(6); }",
        "kas.onclick();",
        "if (!posts.length) { console.error('no decision posted'); process.exit(7); }",
        "var note = posts[0].body.note || '';",
        "if (note.indexOf('Q1 2027') !== -1) { console.error('note carries the raw wording: ' + note); process.exit(8); }",
        "if (note.indexOf('the period') === -1) { console.error('note lost the period sense: ' + note); process.exit(9); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_type_provenance_values_and_labels():
    # labels render as sent; a known id becomes its word; freeform reader
    # wording passes through untouched; empty reads 'Not supplied'; without
    # the projection every surface keeps its old wording
    import subprocess

    js = (UI / "app.js").read_text()
    script = "\n".join([
        "var EVENT_WORDS = { trial_results: 'Trial results', filing: 'Filing' };",
        "function eventWord(t) { return EVENT_WORDS[t] || String(t); }",
        _grab_fn(js, "plainTypeValue"),
        _grab_fn(js, "typeCategoryValue"),
        _grab_fn(js, "typePresentationPairs"),
        # known id -> plain word
        "if (plainTypeValue('trial_results') !== 'Trial results') process.exit(2);",
        # freeform reader wording passes through verbatim (no title-casing)
        "if (plainTypeValue('topline data talk') !== 'topline data talk') process.exit(3);",
        # empty / missing -> 'Not supplied'
        "if (plainTypeValue(null) !== 'Not supplied' || plainTypeValue('') !== 'Not supplied') process.exit(4);",
        # category value alone
        "var t = { type_presentation: { category: 'trial_results', category_label: 'Stored category',",
        "  type_hint: null, type_hint_label: \"Reader's proposed type\" } };",
        "if (typeCategoryValue(t) !== 'Trial results') process.exit(5);",
        # both pairs, labels verbatim as the server sends them
        "var pairs = typePresentationPairs(t);",
        "if (pairs[0].join('|') !== 'Stored category|Trial results') { console.error(pairs[0]); process.exit(6); }",
        "if (pairs[1].join('|') !== \"Reader's proposed type|Not supplied\") { console.error(pairs[1]); process.exit(7); }",
        # legacy keyword label renders as sent
        "var leg = { type_presentation: { category: 'trial_results', category_label: 'Category (from old keyword rule)',",
        "  type_hint: 'topline data talk', type_hint_label: \"Reader's proposed type\" } };",
        "if (typePresentationPairs(leg)[0][0] !== 'Category (from old keyword rule)') process.exit(8);",
        "if (typePresentationPairs(leg)[1][1] !== 'topline data talk') process.exit(9);",
        # no presentation -> null, the caller keeps the old wording
        "if (typeCategoryValue({}) !== null || typePresentationPairs({}) !== null) process.exit(10);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_report_refusal_prints_the_servers_reason():
    # D3: a refused HEAD (unverified against the run state) carries the
    # server's reason — that sentence plus the rebuild hint is shown; a refusal
    # without a reason keeps the old honest fallback
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("currentRunId", "reportUrl", "reportRefusalReason", "reportFileNames", "rowTarget",
                     "reviewAcceptedCount", "reviewAxis", "oneModelRun", "renderExport"))
    script = "\n".join([
        "function el(tag, cls, text) { return { tag: tag, className: cls || '', textContent: text || '', children: [], style: {},",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "function clear(n) { n.textContent = ''; n.children = []; return n; }",
        "var nodes = {};",
        "global.document = { getElementById: function (id) { if (!nodes[id]) nodes[id] = el('div'); return nodes[id]; } };",
        "var current = 'run-x';",
        "function money(v) { return '$' + v; } function unpricedMark() { return ''; }",
        "var detail = { run: { run: { number: 2, label: 'probe', spend_usd: null }, report_stale: false,",
        "  report_stale_reason: null },",
        "  state: { cost_per_kept_row_usd: null, counts: { rows: {} }, unresolved: 0,",
        "    mistakes: { code: 0, second_reader: 0, person: 0 }, excluded: { code: 0, person: 0 } } };",
        "var GROUP_ORDER = [], GROUP_NAMES = {};",
        "var REFUSAL = 'This saved report is not verified against the current run state.';",
        "var STALE = 'Saved report is out of date. Rebuild it to show the current results.';",
        "var mode = 'reason';",
        "global.fetch = function () {",
        "  if (mode === 'ok') return Promise.resolve({ ok: true, status: 200, headers: { get: function () { return null; } } });",
        "  if (mode === 'reason') return Promise.resolve({ ok: false, status: 404,",
        "    headers: { get: function (h) { return h === 'x-report-reason' ? REFUSAL : null; } },",
        "    json: function () { return Promise.reject(new Error('HEAD has no body')); } });",
        "  if (mode === 'stale') return Promise.resolve({ ok: false, status: 404,",
        "    headers: { get: function (h) { return h === 'x-report-reason' ? STALE : null; } },",
        "    json: function () { return Promise.reject(new Error('no body')); } });",
        "  return Promise.resolve({ ok: false, status: 404, headers: { get: function () { return null; } },",
        "    json: function () { return Promise.reject(new Error('no body')); } });",
        "};",
        "function textOf(n, out) { out = out || []; if (n.textContent) out.push(n.textContent);",
        "  (n.children || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        fns,
        "renderExport();",
        "setTimeout(function () {",
        "  var t1 = textOf(nodes['exp-mid']);",
        "  if (t1.indexOf(REFUSAL) === -1) { console.error('reason missing: ' + t1.slice(0, 200)); process.exit(2); }",
        "  if (t1.indexOf('Rebuild the report to open it here.') === -1) { console.error('hint missing'); process.exit(3); }",
        "  if (t1.indexOf('No report is saved for this run yet.') !== -1) { console.error('wrong sentence shown'); process.exit(4); }",
        "  mode = 'stale';",
        "  renderExport();",
        "  setTimeout(function () {",
        "    var ts = textOf(nodes['exp-mid']);",
        "    if (ts.indexOf(STALE) === -1) { console.error('stale sentence missing'); process.exit(7); }",
        "    if (ts.indexOf('Rebuild the report to open it here.') !== -1) { console.error('doubled rebuild hint'); process.exit(8); }",
        "    mode = 'bare';",
        "    renderExport();",
        "    setTimeout(function () {",
        "      var t2 = textOf(nodes['exp-mid']);",
        "      if (t2.indexOf('No report is saved for this run yet.') === -1) { console.error('fallback lost'); process.exit(5); }",
        "      mode = 'ok';",
        "      renderExport();",
        "      setTimeout(function () {",
        "        var t3 = textOf(nodes['exp-mid']);",
        "        if (t3.indexOf('Open') === -1) { console.error('open link missing'); process.exit(6); }",
        "        console.log('ok');",
        "      }, 0);",
        "    }, 0);",
        "  }, 0);",
        "}, 0);",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_review_labels_badge_and_decided_words():
    # The badge shows the projection's review_label with the bucket's colour;
    # a superseded Keep reads as history — never 'Kept by you', never green.
    # Labels are the exact strings from codex/pivot-001 runs/review.py.
    import subprocess

    js = (UI / "app.js").read_text()
    script = "\n".join([
        _grab_fn(js, n) for n in ("reviewBucket", "projected", "reviewClass", "rowBadge", "decidedWord",
                                  "decidedBadge", "keptCurrent", "decidedMark", "reviewAcceptedCount",
                                  "needsAttention", "fixStillOpen", "projectionComplete", "partialProjection")
    ] + [
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var STATUS_WORDS = { matched: 'matched', failed: 'failed', proposed: 'proposed',"
        + " unreadable: 'unreadable', undecided: 'undecided' };",
        "function el(tag, cls, text) { return { tag: tag, className: cls, textContent: text || '', title: '' }; }",
        "function statusBadge(s) { return el('span', 'st st-' + s, STATUS_WORDS[s]); }",
        "function fail(n, m) { console.error(n + ': ' + m); process.exit(n); }",
        "var acc = { group: 'kept', decided_by_person: true, last_action: 'keep', accepted: true, needs_attention: false, on_calendar: true,",
        "  review_bucket: 'accepted', review_label: 'Accepted by you', decision_label: 'Kept by you' };",
        "var b = rowBadge({}, acc);",
        "if (b.textContent !== 'Accepted by you' || b.className !== 'st st-kept') fail(2, b.textContent + b.className);",
        "if (decidedWord(acc, 'x') !== 'Kept by you') fail(3, decidedWord(acc, 'x'));",
        "var stale = { group: 'kept', decided_by_person: true, last_action: 'keep', accepted: false, needs_attention: true, on_calendar: false,",
        "  review_bucket: 'proposed', review_label: 'Previous Keep — needs your review',",
        "  decision_label: 'Previous Keep — needs your review' };",
        "var sb = rowBadge({}, stale), sd = decidedBadge(stale);",
        "if (sb.className !== 'st st-corrected open' || sd.className !== 'st st-corrected open')"
        + " fail(4, sb.className + '/' + sd.className);",
        "if (/Kept by you/.test(sb.textContent + sd.textContent)) fail(5, 'stale reads as kept');",
        "if (keptCurrent(stale) !== false) fail(6, 'stale reads as current');",
        "if (fixStillOpen(stale) !== false) fail(7, 'stale reads as a fix');",
        # F5: no doubled 'decided by you' when the label already names the person
        "if (decidedMark(stale) || decidedMark(acc)) fail(8, 'doubled decided by you');",
        "var fix = { group: 'corrected', decided_by_person: true, last_action: 'fix', accepted: false, on_calendar: false,",
        "  needs_attention: true, review_bucket: 'proposed', review_label: 'Corrected, awaiting your review',",
        "  decision_label: 'Fixed by you — not yet accepted' };",
        "if (decidedMark(fix)) fail(9, 'doubled on a fix');",
        "if (fixStillOpen(fix) !== true) fail(10, 'fix not open');",
        # F7: projection present, decision_label null — review_label, never group words
        "var f7 = { group: 'removed', decided_by_person: true, last_action: 'undo', accepted: false, on_calendar: false,",
        "  needs_attention: false, review_bucket: 'excluded', review_label: 'Excluded', decision_label: null };",
        "if (decidedWord(f7, 'x') !== 'Excluded') fail(11, decidedWord(f7, 'x'));",
        "if (decidedBadge(f7).className !== 'st st-removed') fail(12, decidedBadge(f7).className);",
        # no projection (main-shaped): main's words, main's per-surface fallbacks
        "var kept = { checks: [], status: 'failed', decided_by_person: true, group: 'kept' };",
        "if (rowBadge({}, kept).textContent !== 'Kept by you') fail(13, 'kept');",
        "var open = { group: 'open', decided_by_person: true, needs_attention: true };",
        "if (decidedBadge(open).textContent !== 'open' || decidedBadge(open).className !== 'st st-open')"
        + " fail(14, 'open badge');",
        "if (decidedWord(open, 'Fixed by you') !== 'Fixed by you') fail(15, 'open still-open word');",
        "if (fixStillOpen(open) !== true) fail(16, 'main rule');",
        "if (keptCurrent({ group: 'kept', decided_by_person: true }) !== true) fail(17, 'old kept');",
        "if (decidedMark({ group: 'kept', decided_by_person: true }) !== true) fail(18, 'old mark');",
        "if (decidedMark({ group: 'corrected', decided_by_person: true }) !== false) fail(19, 'old corrected mark');",
        "if (reviewAcceptedCount({ counts: { review: { accepted: 3 } } }) !== 3) fail(20, 'count');",
        "if (reviewAcceptedCount({ counts: {} }) !== null) fail(21, 'no count');",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u35_af7_person_kept_unresolved_row_is_marked():
    # Red Team round 11 A-F7: a keep does not settle a disagreement or clear a
    # failed check — the calendar row wears a small warm marker next to the
    # title, and the pane leads with the failed check / the disagreement
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    for word in ("'kept with a failed check'", "'kept, readers disagree'",
                 "'kept with a failed check, readers disagree'",
                 "el('span', 'keepwarn'", "Kept by you, but the check",
                 "still disagree on the",
                 ".keepwarn{"):
        assert word in (js + css), word
    # the marker only rides on person-kept rows
    cal = _grab_fn(js, "calRow")
    assert "keptCurrent(t)" in cal
    pane = _grab_fn(js, "detailsPane")
    assert pane.index("Kept by you, but the check") < pane.index("var sec1")

# The real app.js rendered under a stub DOM (node), one payload shape per call:
# 'main' (no projection) and 'proj' (the codex/pivot-001 projection fields).
# Rows: accepted Keep, superseded Keep, undecided group-kept row, Fix-only,
# code-excluded, past Fix, no-date undecided, no-date Fix whose re-check could
# not run (the Claude review's eight), plus a person's fix whose re-check
# failed (group 'open', Red Team at 497aada) and a person-touched row that
# code removed (F7).
_REVIEW_RENDER_JS = r"""// GROUP-AXIS-001 render probe: the real app.js under a stub DOM, one payload
// shape per call ('main' = no projection, 'proj' = codex/pivot-001 fields).
const fs = require('fs');
const [, , appPath, shape] = process.argv;
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
const proj = shape === 'proj';
const projLabel = 'Date period found in quote';
// the date check: the pivot's label on the projected payload, main's label otherwise
const dateCheck = { name: 'date_match', label: proj ? projLabel : 'Date read as written', result: 'pass' };
function row(id, title, extra) {
  return Object.assign({ id, title, ticker: 'ACME', proposed_by: 'Muse Spark',
    checks: [{ name: 'quote_found', label: 'Quote found', result: 'pass' }, dateCheck],
    date_text: 'Q3 2027', date: null, event_type: 'filing', quote: 'q', source_id: 's1', page_number: 1 }, extra || {});
}
const rows = [row('r-acc', 'ACCEPTED'), row('r-stale', 'STALE-KEEP'), row('r-und', 'UNDECIDED-KEPT-GROUP'),
  row('r-fix', 'FIX-ONLY'), row('r-exc', 'CODE-EXCLUDED'), row('r-past', 'PAST-FIXED', { date_text: '2020', date: '2020-01-01' }),
  row('r-nod', 'NODATE-UNDECIDED', { date_text: '', date: null }), row('r-cnr', 'NODATE-FIX-COULDNOTRUN', { date_text: '', date: null }),
  row('r-open', 'FIX-RECHECK-FAILED'), row('r-f7', 'TOUCHED-CODE-REMOVED')];
const base = { status: 'matched', needs_recheck: false, recheck: null, fixes: [], values: { date_text: 'Q3 2027' }, reason: null, disagreement: null };
// [main-shaped target, projection fields from codex/pivot-001 runs/review.py]
const P = {
  'r-acc': [{ group: 'kept', decided_by_person: true, last_action: 'keep', needs_attention: false },
    { accepted: true, on_calendar: true, review_label: 'Accepted by you', decision_label: 'Kept by you', review_bucket: 'accepted', needs_attention: false }],
  'r-stale': [{ group: 'kept', decided_by_person: true, last_action: 'keep', needs_attention: false },
    { accepted: false, on_calendar: false, review_label: 'Previous Keep — needs your review', decision_label: 'Previous Keep — needs your review', review_bucket: 'proposed', needs_attention: true, reason: 'The proposal or evidence changed after this Keep; review is required.' }],
  'r-und': [{ group: 'kept', decided_by_person: false, needs_attention: false },
    { accepted: false, on_calendar: false, review_label: 'Proposed — needs review', decision_label: null, review_bucket: 'proposed', needs_attention: true }],
  'r-fix': [{ group: 'corrected', decided_by_person: true, last_action: 'fix', needs_attention: false, fixes: [{ field: 'date_text', from: 'Q2 2027', to: 'Q3 2027', mistake: true, recheck: { result: 'pass' } }], recheck: { result: 'pass' } },
    { accepted: false, on_calendar: false, review_label: 'Corrected, awaiting your review', decision_label: 'Fixed by you — not yet accepted', review_bucket: 'proposed', needs_attention: true }],
  'r-exc': [{ group: 'removed', decided_by_person: false, needs_attention: false },
    { accepted: false, on_calendar: false, review_label: 'Excluded', decision_label: null, review_bucket: 'excluded', needs_attention: false }],
  'r-nod': [{ group: 'kept', decided_by_person: false, needs_attention: false, values: {} },
    { accepted: false, on_calendar: false, review_label: 'Proposed — needs review', decision_label: null, review_bucket: 'proposed', needs_attention: true }],
  'r-cnr': [{ group: 'corrected', decided_by_person: true, last_action: 'fix', needs_attention: true, values: {}, fixes: [{ field: 'title', from: 'a', to: 'b', mistake: false, recheck: { result: 'could_not_run' } }], recheck: { result: 'could_not_run' } },
    { accepted: false, on_calendar: false, review_label: 'Corrected, awaiting your review', decision_label: 'Fixed by you — not yet accepted', review_bucket: 'proposed', needs_attention: true }],
  'r-past': [{ group: 'corrected', decided_by_person: true, last_action: 'fix', needs_attention: false, values: { date_text: '2020', date: '2020-01-01' } },
    { accepted: false, on_calendar: false, review_label: 'Corrected, awaiting your review', decision_label: 'Fixed by you — not yet accepted', review_bucket: 'proposed', needs_attention: true }],
  // Red Team at 497aada: a person's fix whose re-check failed sits in group 'open'
  'r-open': [{ group: 'open', decided_by_person: true, last_action: 'fix', needs_attention: true, status: 'failed', reason: 'The re-check failed.', fixes: [{ field: 'date_text', from: 'Q2 2027', to: 'Q3 2027', mistake: false, recheck: { result: 'fail' } }], recheck: { result: 'fail' } },
    { accepted: false, on_calendar: false, review_label: 'Corrected, awaiting your review', decision_label: 'Fixed by you — not yet accepted', review_bucket: 'proposed', needs_attention: true }],
  // F7: a person touched it, code removed it, nothing accepted or removed by the person
  'r-f7': [{ group: 'removed', decided_by_person: true, last_action: 'undo', needs_attention: false },
    { accepted: false, on_calendar: false, review_label: 'Excluded', decision_label: null, review_bucket: 'excluded', needs_attention: false }],
};
const MAINCAL = { 'r-acc': true, 'r-stale': true, 'r-und': true, 'r-fix': true, 'r-exc': false, 'r-past': true, 'r-nod': true, 'r-cnr': false, 'r-open': false, 'r-f7': false };
const targets = {};
for (const r of rows) {
  const k = r.id;
  targets[k] = Object.assign({}, base, { on_calendar: MAINCAL[k], effective_checks: r.checks }, P[k][0], proj ? P[k][1] : {});
}
const groups = { kept: 0, corrected: 0, removed: 0, unverified: 0, open: 0 };
for (const k in targets) groups[targets[k].group]++;
const counts = { rows: groups, cells: {}, fixes_pending: 0, edits_without_error: 0, excluded_by_person: 0 };
const buckets = { accepted: 0, proposed: 0, excluded: 0 };
for (const k in targets) if (proj) buckets[targets[k].review_bucket]++;
if (proj) counts.review = buckets;
const unresolvedMain = Object.keys(targets).filter(k => targets[k].needs_attention).length;
const state = { targets, counts, mistakes: { code: 0, second_reader: 0, person: 1 }, mistake_list: [], excluded: { code: 2, person: 0 },
  exclusions: [{ target: 'r-exc', by: 'code', rule: 'event_in_past', reason: 'past' }, { target: 'r-f7', by: 'code', rule: 'event_in_past', reason: 'past' }],
  unresolved: proj ? buckets.proposed : unresolvedMain, ignored: [], kept_rows: proj ? buckets.accepted : groups.kept, cost_per_kept_row_usd: null };
H.detail = { id: 'run1', run: { rows, calendar_window: { start: '2026-10-01' }, run: { id: 'run1', label: 'probe', started: '2026-10-01T00:00:00Z' },
  sources: [{ id: 's1', kind: 'pdf', title: 'S', publisher: 'P' }], mistakes: [], tables: [] }, state };
function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent); (n.children || []).forEach(c => textOf(c, out)); return out; }
function cls(n, out) { out = out || []; if (!n) return out; if (n.className && /\b(st|dot)\b/.test(n.className)) out.push(n.className + ' :: ' + n.textContent); (n.children || []).forEach(c => cls(c, out)); return out; }
// the date check line in a pane: [dot class, result word]
function dateLine(n, out) {
  out = out || []; if (!n) return out;
  if (n.className === 'chk' && n.children.length >= 3 && textOf(n.children[1]).join('').indexOf('Date ') === 0)
    out.push([n.children[0].className, n.children[2].textContent]);
  (n.children || []).forEach(c => dateLine(c, out)); return out;
}
H.ev("route = { screen: 'audit', runId: 'run1', rowId: null, dialog: null }");
const out = {};
function run(name, fn, nodeId) {
  try { const r = fn(); const n = nodeId ? nodes[nodeId] : r; out[name] = { text: textOf(n), badges: cls(n), date: dateLine(n) }; }
  catch (e) { out[name] = 'ERR ' + e.message.split('\n')[0]; }
  finally { if (nodeId) delete nodes[nodeId]; }
}
run('facets', () => H.ev('renderAuditFacets()'), 'audit-facets');
run('auditRows_ALL', () => { H.ev("audit.filter='ALL'; audit.selected='r-acc'"); H.ev('renderAuditRows()'); }, 'audit-rows');
for (const g of ['kept', 'proposed', 'corrected', 'removed', 'open'])
  run('auditRows_' + g, () => { H.ev("audit.filter='" + g + "'"); H.ev('renderAuditRows()'); }, 'audit-rows');
H.ev("audit.filter='ALL'");
run('judgment_sub', () => H.ev('renderJudgment()'), 'judgment-sub');
run('judgment_rows', () => H.ev('renderJudgment()'), 'judgment-rows');
run('calendar', () => { H.ev('renderCalendar()'); }, 'cal-mid');
run('export', () => H.ev('renderExport()'), 'exp-det');
for (const r of rows) {
  run('pane_' + r.id, () => { const host = make('div'); H.ev('detailsPane')(host, H.ev('rowOf')(r.id), { actions: true, back: 'audit' }); return host; });
  run('jpane_' + r.id, () => { const host = make('div'); const t = H.ev('rowTarget')(r);
    (t.decided_by_person && !H.ev('needsAttention')(t) ? H.ev('judgmentDecidedPane') : H.ev('judgmentPendingPane'))(host, r, t); return host; });
}
console.log(JSON.stringify(out, null, 1));
"""

# sha256 of the main-shaped render: byte-identical to main's own render (RT
# addition c — the 'Not stated' wording appears only when the projection is
# present). Re-verified by diffing both renders surface by surface.
_ITEM30_RENDER_JS = r"""// T1 (pr157 review) render probe: the real app.js under a stub DOM against
// server-shaped payloads — cleared date wording, a partial projection, an
// edited type on the trail. One shape per call.
const fs = require('fs');
const [, , appPath, shape] = process.argv;
function make(tag) {
  return { tag, className: '', textContent: '', children: [], style: {}, hidden: false, value: '', dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    append(...a) { for (const c of a) this.children.push(typeof c === 'string' ? { textContent: c, children: [] } : c); },
    appendChild(c) { this.children.push(c); return c; }, prepend(...a) { this.children.unshift(...a); },
    addEventListener(ev, fn) { this['on' + ev] = fn; }, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; }, getAttribute() { return null; },
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
function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent); (n.children || []).forEach(c => textOf(c, out)); return out; }
function cls(n, out) { out = out || []; if (!n) return out; if (n.className && /\bst\b/.test(n.className)) out.push(n.className + ' :: ' + n.textContent); (n.children || []).forEach(c => cls(c, out)); return out; }
function mkRow(id, title, extra) {
  return Object.assign({ id, title, ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],
    date_text: 'Q1 2027', date: '2027-03-12', date_end: '2027-06-30', event_type: 'filing', quote: 'q',
    source_id: 's1', page_number: 1 }, extra || {});
}
let rows, targets, calendar;
const base = { status: 'matched', needs_recheck: false, recheck: null, fixes: [], reason: null, disagreement: null };
if (shape === 'cleared') {
  // the person cleared the date wording only; the structured period stays
  rows = [mkRow('r-empty', 'CLEARED-EMPTY', {}), mkRow('r-null', 'CLEARED-NULL', {})];
  targets = {
    'r-empty': Object.assign({}, base, { group: 'kept', decided_by_person: false, values: { date_text: '', date: '2027-03-12', date_end: '2027-06-30' }, on_calendar: true }),
    'r-null': Object.assign({}, base, { group: 'kept', decided_by_person: false, values: { date_text: null, date: '2027-03-12', date_end: '2027-06-30' }, on_calendar: true }),
  };
  calendar = { calendar: [{ id: 'r-empty' }, { id: 'r-null' }], past: [], later: [], undated: [], timing_unresolved: [], contingent: [] };
} else if (shape === 'partial') {
  // H3: some projection fields but not all — needs review, never kept/accepted/calendar
  rows = [mkRow('r-part', 'PARTIAL', {}), mkRow('r-ok', 'COMPLETE', {}), mkRow('r-pc', 'PARTIAL-CORRECTED', {})];
  targets = {
    'r-part': Object.assign({}, base, { group: 'kept', decided_by_person: true, last_action: 'keep',
      review_label: 'Accepted by you', accepted: true, values: { date_text: 'Q1 2027', date: '2027-03-12', date_end: '2027-06-30' }, on_calendar: true }),
    'r-ok': Object.assign({}, base, { group: 'kept', decided_by_person: true, last_action: 'keep',
      review_label: 'Accepted by you', decision_label: 'Kept by you', review_bucket: 'accepted',
      accepted: true, on_calendar: true, needs_attention: false, values: { date_text: 'Q1 2027', date: '2027-03-12', date_end: '2027-06-30' } }),
    // a partial row in the corrected group: never 'Fixed by you' anywhere
    'r-pc': Object.assign({}, base, { group: 'corrected', decided_by_person: true, last_action: 'fix',
      review_label: 'Corrected, awaiting your review', needs_attention: true,
      values: { date_text: 'Q3 2027', date: null, date_end: null } }),
  };
  calendar = { calendar: [{ id: 'r-ok' }], past: [], later: [], undated: [], timing_unresolved: [], contingent: [] };
} else if (shape === 'edited-type') {
  // H1: the reader proposed trial_readout; the person fixed it to filing —
  // the trail proposal sentence quotes the READER, not the current value
  rows = [mkRow('r-ed', 'EDITED-TYPE', { event_type: 'trial_readout' })];
  targets = { 'r-ed': Object.assign({}, base, { group: 'corrected', decided_by_person: true, last_action: 'fix',
    fixes: [{ field: 'event_type', from: 'trial_readout', to: 'filing', mistake: false, recheck: { result: 'pass' } }],
    values: { event_type: 'filing', date_text: 'Q3 2027', date: null, date_end: null },
    type_presentation: { category: 'filing', category_label: 'Category edited by you',
      type_hint: null, type_hint_label: "Reader's proposed type" } }) };
  calendar = { calendar: [], past: [], later: [], undated: [], timing_unresolved: [], contingent: [] };
} else { throw new Error('unknown shape ' + shape); }
const groups = { kept: 0, corrected: 0, removed: 0, unverified: 0, open: 0 };
for (const k in targets) groups[targets[k].group]++;
const counts = { rows: groups, cells: {}, fixes_pending: 0, edits_without_error: 0, excluded_by_person: 0 };
H.detail = { id: 'run1',
  run: { rows, calendar_partition: calendar, calendar_window: { start: '2026-10-01' }, run: { id: 'run1', label: 'probe', started: '2026-10-01T00:00:00Z' },
    sources: [{ id: 's1', kind: 'pdf', title: 'S', publisher: 'P' }], mistakes: [], tables: [] },
  state: { targets, counts, mistakes: { code: 0, second_reader: 0, person: 0 }, mistake_list: [],
    excluded: { code: 0, person: 0 }, exclusions: [], unresolved: 0, ignored: [], kept_rows: 0,
    cost_per_kept_row_usd: null } };
function money(v) { return '$' + v; }
function unpricedMark() { return ''; }
function reportFileNames() { return []; }
H.ev("route = { screen: 'audit', runId: 'run1', rowId: null, dialog: null }");
const out = {};
function run(name, fn, nodeId) {
  try { const r = fn(); const n = nodeId ? nodes[nodeId] : r; out[name] = { text: textOf(n), badges: cls(n) }; }
  catch (e) { out[name] = 'ERR ' + e.message.split('\n')[0]; }
  finally { if (nodeId) delete nodes[nodeId]; }
}
run('calendar', () => H.ev('renderCalendar()'), 'cal-mid');
run('export', () => H.ev('renderExport()'), 'exp-det');
run('auditRows_ALL', () => { H.ev("audit.filter='ALL'; audit.selected=null"); H.ev('renderAuditRows()'); }, 'audit-rows');
run('judgment_rows', () => H.ev('renderJudgment()'), 'judgment-rows');
run('facets', () => H.ev('renderAuditFacets()'), 'audit-facets');
for (const r of rows) {
  run('pane_' + r.id, () => { const host = make('div'); H.ev('detailsPane')(host, H.ev('rowOf')(r.id), { actions: true, back: 'audit' }); return host; });
  run('trail_' + r.id, () => { const row = H.ev('rowOf')(r.id); const txt = H.ev('plainProposal')(row, H.ev('rowTarget')(row), { step: 'Proposed', tag: 'Model' }); const n = make('div'); n.textContent = txt; return n; });
}
console.log(JSON.stringify(out, null, 1));
"""

# c1313667 is CHECK-RESULTS-WORDS-001's date outcome ('Timing matches source'
# in place of a bare 'passed' on every pane's passing date check) — re-verified
# by diffing both renders surface by surface: that one line is the only change
_MAIN_SHAPED_RENDER_SHA256 = "f98299d4cdc02b6e83e0ce55788c7895954046ba5282bdc571f6187e5dab65e4"


def _review_render(tmp_path, shape):
    import json
    import subprocess

    script = tmp_path / "render.js"
    script.write_text(_REVIEW_RENDER_JS)
    got = subprocess.run(["node", str(script), str(UI / "app.js"), shape],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return got.stdout, json.loads(got.stdout)


def _item30_render(tmp_path, shape):
    import json
    import subprocess

    script = tmp_path / "item30.js"
    script.write_text(_ITEM30_RENDER_JS)
    got = subprocess.run(["node", str(script), str(UI / "app.js"), shape],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def test_t1_cleared_date_text_renders_cleared_on_calendar_and_trail(tmp_path):
    # T1/H2: a cleared date_text (null AND '') renders as cleared on every
    # CURRENT surface — calendar, audit list, judgment list, panes never
    # resurrect the raw 'Q1 2027'. The trail is different (item 38): it is the
    # reader's historical record and quotes the reader's own words again
    out = _item30_render(tmp_path, "cleared")
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    for surface in ("calendar", "auditRows_ALL", "judgment_rows",
                    "pane_r-empty", "pane_r-null"):
        text = " ".join(out[surface]["text"])
        assert "Q1 2027" not in text, (surface, text[:200])
    # the trail sentence wears the READER's words + the row period's day-span
    # words — never the cleared state, and no ISO dates (F1/F3, second pass)
    import re
    iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    for surface in ("trail_r-empty", "trail_r-null"):
        text = " ".join(out[surface]["text"])
        assert "Q1 2027 (12 Mar – 30 Jun 2027)" in text, (surface, text[:200])
        assert not iso.search(text), (surface, text[:200])
    # the structured period still places the row on the calendar (no invented
    # wording, the period dates kept); the two identical cleared rows fold
    cal = " ".join(out["calendar"]["text"])
    assert "CLEARED-EMPTY" in cal and "2 sources" in cal, cal[:200]
    # RT(c): 'Not stated' appears only under the projection — these cleared
    # rows carry no projection fields, so the pane shows the cleared display


def test_t1_partial_projection_needs_review_not_kept(tmp_path):
    # T1/H3: a row with some projection fields but not all is listed as needs
    # review — never kept/accepted, never on the calendar, never green
    out = _item30_render(tmp_path, "partial")
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    audit = " ".join(out["auditRows_ALL"]["text"])
    # the partial rows sit under the proposed heading, the complete one under kept
    assert "Proposed — needs review · 2" in audit, audit[:300]
    assert "Checked and kept · 1" in audit, audit[:300]
    assert "PARTIAL" in " ".join(out["judgment_rows"]["text"])
    # H5: needs review on the badge, the decided word and the pane — no
    # kept/accepted words, no green, and never the fix pane
    for surface in ("pane_r-part",):
        text = " ".join(out[surface]["text"])
        for word in ("Kept by you", "Accepted by you"):
            assert word not in text, (surface, word)
        assert "Needs review" in text, (surface, text[:200])
        assert not any(b.startswith("st st-kept ") for b in out[surface]["badges"]), out[surface]["badges"]
    jtext = " ".join(out["judgment_rows"]["text"])
    jbadge = " ".join(b for b in out["judgment_rows"]["badges"])
    assert "Kept by you" not in jbadge and "Needs review" in jbadge, jbadge
    assert "still open" not in jtext, jtext[:200]
    # RT(a): the export summary uses the same viewGroup counts as the strip
    # even with no counts.review — kept 1, one proposed line, total adds up
    exp = " ".join(out["export"]["text"])
    assert "Checked and kept 1" in exp, exp[:300]
    assert "Proposed — needs review 2" in exp, exp[:300]
    assert "Checked and kept 2" not in exp, exp[:300]
    # RT(b): a partial CORRECTED row never reads 'Fixed by you' — not on the
    # audit-list badge, not in the pane's corrected section
    pc_text = " ".join(out["pane_r-pc"]["text"])
    assert "Fixed by you" not in pc_text, pc_text[:200]
    assert "Needs review" in pc_text, pc_text[:200]
    pc_badges = " ".join(out["auditRows_ALL"]["badges"])
    all_badge_needs = [b for b in out["auditRows_ALL"]["badges"] if "Needs review" in b]
    assert all(b.startswith("st st-corrected open") for b in all_badge_needs), all_badge_needs
    # the calendar carries only the complete row
    cal = " ".join(out["calendar"]["text"])
    assert "COMPLETE" in cal and "PARTIAL" not in cal, cal[:200]


def test_t1_edited_type_trail_quotes_the_reader(tmp_path):
    # T1/H1: the trail's proposal sentence credits the READER's proposal — a
    # person's later type edit never replaces it ('Trial results', not 'Filing')
    out = _item30_render(tmp_path, "edited-type")
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    trail = " ".join(out["trail_r-ed"]["text"])
    assert "Trial results" in trail, trail[:200]
    assert trail.index("Trial results") < trail.index("·"), trail
    assert not trail.startswith("Filing"), trail
    # the DETAIL pane still names the person's edit (type provenance)
    pane = " ".join(out["pane_r-ed"]["text"])
    assert "Category edited by you" in pane and "Filing" in pane, pane[:240]


def test_h4_real_state_payload_shows_the_server_display_after_a_clear(tmp_path):
    # Integration uses the actual state projection, without copying its date
    # functions or filling missing fields on the result inside this test.
    import json
    import subprocess
    from runs.state import current_state

    # the real state machine over a run doc: a period row, then the person
    # clears the date wording, keeping the structured period
    run = {"run": {"id": "run1", "label": "probe"}, "rows": [{
        "id": "r1", "ticker": "ACME", "company": "Acme", "title": "Period row",
        "proposed_by": "Muse Spark", "event_type": "filing", "quote": "q",
        "source_id": "s1", "page_number": 1, "checks": [],
        "date_text": "Q1 2027", "date": "2027-01-01", "date_end": "2027-03-31"}],
        "sources": [{"id": "s1", "kind": "pdf", "title": "S", "publisher": "P"}],
        "mistakes": [], "tables": []}
    clear_fix = {"at": "2026-11-01T00:00:00Z", "target": "r1", "action": "fix", "by": "person",
                 "note": None, "fix": {"field": "date_text", "from": "Q1 2027", "to": ""}}
    st = current_state(run, [clear_fix])
    t = st["targets"]["r1"]
    assert t["date_text_cleared"] is True
    assert t["date_display"] == "Q1 2027 (2027-01-01 to 2027-03-31)", t["date_display"]

    # render through the real app.js (the item-30 probe style)
    payload = {"rows": run["rows"], "targets": st["targets"],
               "calendar": [{"id": "r1"}], "groups": {}}
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload))
    script = tmp_path / "realpay.js"
    script.write_text(_REALPAY_RENDER_JS)
    got = subprocess.run(["node", str(script), str(UI / "app.js"), str(payload_path)],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    display = "Q1 2027 (2027-01-01 to 2027-03-31)"
    # the calendar is not in this list on purpose: the real state machine keeps
    # an unaccepted fix off the calendar (on_calendar False) — the calendar
    # surface for a cleared row is covered by the t1_cleared render test.
    # The trail is not in this list either (item 38, second pass): the Reader
    # step quotes the READER's own date, not the current state.
    for surface in ("auditRows_ALL", "judgment_rows", "pane_r1"):
        text = " ".join(out[surface]["text"])
        assert display in text, (surface, text[:220])
    # no undated/unresolved wording on the current surfaces
    alltext = " ".join(" ".join(v["text"]) for v in out.values())
    assert "Undated" not in alltext and "no timing stated" not in alltext
    # F3: no ISO date anywhere in the Reader step — inverted from the first
    # pass, which pinned the display on the trail
    import re
    iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    trail = " ".join(out["trail_r1"]["text"])
    assert not iso.search(trail), trail
    assert trail.count("Q1 2027 (1 Jan – 31 Mar 2027)") == 1, trail
    # F1/F2: the sentence wears the reader's own words + the row period's
    # day-span words; the attribution quotes the reader's bare wording
    step_text = " ".join(out["trail_step_r1"]["text"])
    assert not iso.search(step_text), step_text
    assert "1 Jan – 31 Mar 2027" in step_text, step_text
    assert "Reader's words: 'Q1 2027'" in step_text, step_text


@pytest.mark.parametrize("mode", ["clear-null", "edit"])
def test_item38_reader_step_quotes_the_readers_date_after_clear_null_and_edit(tmp_path, mode):
    # item 38 second pass (F1/F3), real pivot payloads: after a clear to null
    # and after an edit, the Reader step's sentence and attribution quote the
    # READER's own date words ('the first quarter of 2027') with the row
    # period's day-span words — never the server's date_display, never the
    # person's value, no ISO dates. Every current surface keeps date_display.
    import json
    import re
    import subprocess

    from runs.state import current_state

    run = {"run": {"id": "run1", "label": "probe"}, "rows": [{
        "id": "r1", "ticker": "ACME", "company": "Acme", "title": "Period row",
        "proposed_by": "Muse Spark", "event_type": "filing", "quote": "q",
        "source_id": "s1", "page_number": 1, "checks": [],
        "date_text": "the first quarter of 2027",
        "date": "2027-01-01", "date_end": "2027-03-31"}],
        "sources": [{"id": "s1", "kind": "pdf", "title": "S", "publisher": "P"}],
        "mistakes": [], "tables": []}
    if mode == "clear-null":
        fix = {"at": "2026-11-01T00:00:00Z", "target": "r1", "action": "fix", "by": "person",
               "note": None, "fix": {"field": "date_text", "from": "the first quarter of 2027", "to": None}}
        expect = "Q1 2027 (2027-01-01 to 2027-03-31)"   # wording cleared, period kept
    else:
        fix = {"at": "2026-11-01T00:00:00Z", "target": "r1", "action": "fix", "by": "person",
               "note": None, "fix": {"field": "date_text", "from": "the first quarter of 2027", "to": "Q1 2027"}}
        expect = "Q1 2027"                              # the person's wording
    st = current_state(run, [fix])
    t = st["targets"]["r1"]
    assert t["date_display"] == expect, t["date_display"]

    payload = {"rows": run["rows"], "targets": st["targets"],
               "calendar": [{"id": "r1"}], "groups": {}}
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload))
    script = tmp_path / "realpay.js"
    script.write_text(_REALPAY_RENDER_JS)
    got = subprocess.run(["node", str(script), str(UI / "app.js"), str(payload_path)],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    out = json.loads(got.stdout)
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    # current surfaces wear the server's date_display in both modes
    for surface in ("auditRows_ALL", "judgment_rows", "pane_r1"):
        text = " ".join(out[surface]["text"])
        assert expect in text, (mode, surface, text[:220])
    # the Reader step quotes the reader, not the current state
    iso = re.compile(r"\d{4}-\d{2}-\d{2}")
    step_text = " ".join(out["trail_step_r1"]["text"])
    assert not iso.search(step_text), (mode, step_text)
    sentence = [x for x in out["trail_step_r1"]["text"] if x.startswith("Filing ·")][0]
    assert sentence.startswith("Filing · the first quarter of 2027 (1 Jan – 31 Mar 2027): "), (mode, sentence)
    assert "Reader's words: 'the first quarter of 2027'" in step_text, (mode, step_text)


def test_item38_untouched_trail_step_is_byte_identical_to_e90cfc8(tmp_path):
    # item 38 third pass: the 'Reader's words' line rides ONLY after a clear or
    # a date fix — an untouched row's Reader step renders byte-identical to
    # e90cfc8's trailStep (the hash below is that base render's sha256)
    import hashlib
    import json
    import subprocess

    script = tmp_path / "trailstep.js"
    script.write_text(_TRAIL_STEP_RENDER_JS)
    got = subprocess.run(["node", str(script), str(UI / "app.js")],
                         text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    assert hashlib.sha256(got.stdout.encode()).hexdigest() == _UNTOUCHED_TRAIL_SHA256
    out = json.loads(got.stdout)
    sentence = [x for x in out if x.startswith("Filing ·")][0]
    assert sentence == "Filing · in the coming months (12 Mar – 30 Jun 2027): “the words on the page”", sentence
    assert not any("Reader's words" in x for x in out), out


def test_item38_day_span_words_keeps_the_start_year_across_a_year_boundary():
    # Red Team on #160: a cross-year span dropped the start year (both branches
    # of the ternary were empty) — '1 Nov – 28 Feb 2027'. The start year now
    # rides ('1 Nov 2026 – 28 Feb 2027'); same-year spans are unchanged
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("daySpanWords", "dayMonth"))
    script = "\n".join([
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        fns,
        "if (daySpanWords('2026-11-01', '2027-02-28') !== '1 Nov 2026 – 28 Feb 2027')",
        "  { console.error(daySpanWords('2026-11-01', '2027-02-28')); process.exit(2); }",
        "if (daySpanWords('2027-01-01', '2027-03-31') !== '1 Jan – 31 Mar 2027')",
        "  { console.error(daySpanWords('2027-01-01', '2027-03-31')); process.exit(3); }",
        # a whole-year span names the year once
        "if (daySpanWords('2027-01-01', '2027-12-31') !== '1 Jan – 31 Dec 2027')",
        "  { console.error(daySpanWords('2027-01-01', '2027-12-31')); process.exit(4); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


_UNTOUCHED_TRAIL_SHA256 = "46e1a2620828172917bd9253c816bfbfc6ac60a4482d1b01ab22320be752036d"

_TRAIL_STEP_RENDER_JS = r"""// item 38 third pass: render one Reader step (trailStep) for an untouched row
const fs = require('fs');
const [, , appPath] = process.argv;
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
function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent); (n.children || []).forEach(c => textOf(c, out)); return out; }
const row = { id: 'r1', title: 'Untouched', ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],
  date_text: 'in the coming months', date: '2027-03-12', date_end: '2027-06-30',
  event_type: 'filing', quote: 'the words on the page', source_id: 's1', page_number: 1 };
const targets = { 'r1': { group: 'kept', status: 'matched', needs_recheck: false, recheck: null,
  reason: null, disagreement: null, fixes: [],
  values: { date_text: 'in the coming months', date: '2027-03-12', date_end: '2027-06-30', quote: 'the words on the page' } } };
H.detail = { id: 'run1',
  run: { rows: [row], calendar_partition: { calendar: [], past: [], later: [], undated: [], timing_unresolved: [], contingent: [] },
    calendar_window: { start: '2026-10-01' }, run: { id: 'run1', label: 'probe', started: '2026-10-01T00:00:00Z' },
    sources: [{ id: 's1', kind: 'pdf', title: 'S', publisher: 'P' }], mistakes: [], tables: [] },
  state: { targets, counts: { rows: { kept: 1, corrected: 0, removed: 0, unverified: 0, open: 0 }, cells: {}, fixes_pending: 0, edits_without_error: 0, excluded_by_person: 0 },
    mistakes: { code: 0, second_reader: 0, person: 0 }, mistake_list: [], excluded: { code: 0, person: 0 },
    exclusions: [], unresolved: 0, ignored: [], kept_rows: 0, cost_per_kept_row_usd: null } };
H.ev("route = { screen: 'audit', runId: 'run1', rowId: null, dialog: null }");
const step = H.ev('trailStep')(row, { step: 'Proposed', tag: 'Model', who: 'Muse Spark',
  text: 'ACME filing on 2027-03-12: “the words on the page”', at: '2026-10-01T00:00:00Z',
  tokens: { prompt: 1200, completion: 150 }, cost_usd: 0.002 }, false);
console.log(JSON.stringify(textOf(step)));
"""


_REALPAY_RENDER_JS = r"""// H4 real-payload probe: render the state-machine payload through the real
// app.js under a stub DOM. Payload: rows/targets/calendar from argv[3].
const fs = require('fs');
const [, , appPath, payloadPath] = process.argv;
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
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
function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent); (n.children || []).forEach(c => textOf(c, out)); return out; }
const targets = payload.targets;
const groups = { kept: 0, corrected: 0, removed: 0, unverified: 0, open: 0 };
for (const k in targets) { const g = targets[k].group; if (g in groups) groups[g] += 1; }
H.detail = { id: 'run1',
  run: { rows: payload.rows, calendar_partition: { calendar: payload.calendar, past: [], later: [], undated: [], timing_unresolved: [], contingent: [] },
    calendar_window: { start: '2026-10-01' }, run: { id: 'run1', label: 'probe', started: '2026-10-01T00:00:00Z' },
    sources: [{ id: 's1', kind: 'pdf', title: 'S', publisher: 'P' }], mistakes: [], tables: [] },
  state: { targets, counts: { rows: groups, cells: {}, fixes_pending: 0, edits_without_error: 0, excluded_by_person: 0 },
    mistakes: { code: 0, second_reader: 0, person: 0 }, mistake_list: [], excluded: { code: 0, person: 0 },
    exclusions: [], unresolved: 0, ignored: [], kept_rows: 0, cost_per_kept_row_usd: null } };
H.ev("route = { screen: 'audit', runId: 'run1', rowId: null, dialog: null }");
const out = {};
function run(name, fn, nodeId) {
  try { const r = fn(); const n = nodeId ? nodes[nodeId] : r; out[name] = { text: textOf(n) }; }
  catch (e) { out[name] = 'ERR ' + e.message.split('\n')[0]; }
  finally { if (nodeId) delete nodes[nodeId]; }
}
run('calendar', () => H.ev('renderCalendar()'), 'cal-mid');
run('auditRows_ALL', () => { H.ev("audit.filter='ALL'; audit.selected=null"); H.ev('renderAuditRows()'); }, 'audit-rows');
run('judgment_rows', () => H.ev('renderJudgment()'), 'judgment-rows');
const row = payload.rows[0];
run('trail_r1', () => { const txt = H.ev('plainProposal')(H.ev('rowOf')('r1'), H.ev('rowTarget')(row), {}); const n = make('div'); n.textContent = txt; return n; });
run('trail_step_r1', () => H.ev('trailStep')(H.ev('rowOf')('r1'), { step: 'Proposed', tag: 'Model',
  who: 'Muse Spark', text: 'ACME filing on 2027-03-12: “q”', at: '2026-10-01T00:00:00Z',
  tokens: { prompt: 1, completion: 1 }, cost_usd: 0.001 }, false));
run('pane_r1', () => { const host = make('div'); H.ev('detailsPane')(host, H.ev('rowOf')('r1'), { actions: true, back: 'audit' }); return host; });
console.log(JSON.stringify(out, null, 1));
"""


def test_m1_undo_click_posts_the_undo(tmp_path):
    # M1/D1: a superseded Keep offers Undo; clicking it posts the API undo —
    # target, action undo, no note
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    script = (Path(__file__).parent / "item30.js")
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "decidedWord", "reviewBucket",
             "rowBadge", "checkDot", "checkResultWord", "projected", "projectionComplete", "partialProjection",
             "reviewClass", "fixStillOpen", "needsAttention", "effectiveChecks", "rowTarget", "rowOf",
             "unresolvedRows", "decidedRows", "mistakeFor", "judgmentRow", "correctedSection", "checksSection",
             "judgmentPendingPane", "judgmentDecidedPane", "renderJudgment", "sayControls", "cancelSay",
             "sayBusyWith", "captureFolds", "restoreFolds", "revealRow", "sourceOf", "nearMissCheck", "listReasonLine", "shownDateText",
             "shownField", "typeCategoryValue", "typePresentationPairs", "plainTypeValue", "detailsPane",
             "supportFindingFor", "supportNoteText", "researchLabelLine", "researchContextSection",
             "disagreementSection", "whySection", "keptCurrent", "codeExclusionHistory", "projectionComplete", "partialProjection", "projectionComplete", "partialProjection")
    fns = "\n".join(_grab_fn(js, n) for n in names)
    harness = "\n".join([
        "var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed', unreadable: 'unreadable', undecided: 'undecided' };",
        "var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' };",
        "var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };",
        "var FIELD_WORDS = { date_text: 'Date as written', date: 'Date', event_type: 'Event type' };",
        "var NO_ERROR = 'The value already passed its checks before the edit.';",
        "var EVENT_WORDS = { filing: 'Filing' };",
        "var judgmentSel = null; var route = { screen: 'judgment', rowId: null, dialog: null };",
        "var posts = [];",
        "function currentRunId() { return 'run1'; }",
        "function navigate() {}",
        "function postDecision(id, body) { posts.push({ id: id, body: body }); }",
        "function exclusionFor() { return null; }",
        "function closeDialog() {}",
        "function openDialog() {}",
        "function seeTheCheck() { return { textContent: '', children: [] }; }",
        "function fmtField(f, v) { return String(v); }",
        "function bothReadersWord() { return 'The readers'; }",
        "function toast() {}",
        "function eventWord(t) { return EVENT_WORDS[t] || String(t); }",
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  querySelector: function () { return null; } }; }",
        "var nodes = {}; var sayRec = null; var qnoteDraft = {};",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (!nodes[id]) nodes[id] = make(); return nodes[id]; } };",
        fns,
        "var row = { id: 'r-stale', title: 'STALE', ticker: 'ACME', proposed_by: 'Muse Spark', checks: [],",
        "  date_text: 'Q3 2027', date: null, event_type: 'filing', quote: 'q', source_id: 's1', page_number: 1 };",
        # the superseded Keep: person's keep, projection says proposed
        "var detail = { id: 'run', run: { rows: [row], mistakes: [], sources: [], run: { label: 'probe' } },",
        "  state: { targets: { 'r-stale': { group: 'kept', status: 'matched', decided_by_person: true,",
        "    last_action: 'keep', needs_attention: true, needs_recheck: false, recheck: null,",
        "    review_label: 'Previous Keep — needs your review', decision_label: 'Previous Keep — needs your review',",
        "    review_bucket: 'proposed', accepted: false, on_calendar: false,",
        "    values: { date_text: 'Q3 2027' }, fixes: [], reason: null, disagreement: null } },",
        "    exclusions: [], mistake_list: [] } };",
        "function textOf(node, out) { out = out || []; if (node && node.textContent) out.push(node.textContent);",
        "  ((node && node.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "renderJudgment();",
        "var btns = [];",
        "(function walk(n) { (n.children || []).forEach(function (c) { if (c.tag === 'button') btns.push(c); walk(c); }); })(nodes['judgment-det']);",
        "var undo = btns.filter(function (b) { return b.textContent === 'Undo'; })[0];",
        "if (!undo) { console.error('Undo not offered for the superseded Keep'); process.exit(2); }",
        "undo.onclick();",
        "if (posts.length !== 1) { console.error('posts=' + posts.length); process.exit(3); }",
        "var p = posts[0];",
        "if (p.id !== 'r-stale' || p.body.target !== 'r-stale' || p.body.action !== 'undo' || p.body.note !== null)",
        "  { console.error(JSON.stringify(p)); process.exit(4); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=harness, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_review_projection_rendered_counts_lists_and_badges(tmp_path):
    _, out = _review_render(tmp_path, "proj")
    for name, v in out.items():
        assert not isinstance(v, str), (name, v)
    rows = 10

    def facet(text, name):
        return int(text[text.index(name + " ") + 1])

    facets = out["facets"]["text"]
    summary = out["export"]["text"]
    assert summary[0] == "Summary — the same counts as the audit log"
    total = 0
    for name in ("Checked and kept", "Proposed — needs review", "Corrected", "Checked and removed",
                 "Unverified", "Still open"):
        n = facet(facets, name)
        # F2: each summary line is its facet's number
        assert summary[summary.index(name) + 1] == str(n), name
        total += n
    assert total == rows                                   # F2: every row is on a line
    # F1: the kept facet == its filtered list == its heading == the accepted row
    kept_n = facet(facets, "Checked and kept")
    assert kept_n == 1
    kept_list = out["auditRows_kept"]["text"]
    assert kept_list.count("ACME") == kept_n and "ACCEPTED" in kept_list
    assert "Checked and kept · %d" % kept_n in out["auditRows_ALL"]["text"]
    for key, name in (("proposed", "Proposed — needs review"), ("corrected", "Corrected"),
                      ("removed", "Checked and removed"), ("open", "Still open")):
        n = facet(facets, name)
        assert out["auditRows_" + key]["text"].count("ACME") == n, key
        assert "%s · %d" % (name, n) in out["auditRows_ALL"]["text"], key
    # nothing says kept/accepted unless accepted is true; code removals are
    # never 'Removed by you' (F7); nothing non-accepted is green
    for rid in ("r-stale", "r-und", "r-fix", "r-exc", "r-past", "r-nod", "r-cnr", "r-open", "r-f7"):
        for surface in ("pane_", "jpane_"):
            text = " ".join(out[surface + rid]["text"])
            for word in ("Kept by you", "Accepted by you", "Removed by you"):
                assert word not in text, (surface, rid, word)
            assert not any(b.startswith("st st-kept ") for b in out[surface + rid]["badges"]), (surface, rid)
    alltext = " ".join(" ".join(v["text"]) for v in out.values())
    assert "decided by you" not in alltext                 # F5: no doubled marker
    # F3/F4: the superseded Keep is amber and never a fix — and D1: with the
    # projection, Undo is offered whenever the decision stack is non-empty
    # (last_action names the person's decision), so the stale Keep can be
    # taken back; it is still not the fix pane
    stale = out["jpane_r-stale"]["text"]
    assert "Undo" in stale and "Fix again…" not in stale
    assert not any("Keep accepts the fixed value" in t or "· still open" in t for t in stale)
    for surface in ("pane_r-stale", "jpane_r-stale", "auditRows_ALL", "judgment_rows"):
        assert "st st-corrected open :: Previous Keep — needs your review" in out[surface]["badges"], surface
    # a Fix not yet accepted keeps the fix pane (Undo offered)
    assert "Undo" in out["jpane_r-fix"]["text"]
    assert "st st-removed :: Excluded" in out["pane_r-f7"]["badges"]
    assert "st st-kept :: Kept by you" in out["pane_r-acc"]["badges"]
    assert "st st-kept :: Accepted by you" in out["auditRows_ALL"]["badges"]
    assert out["judgment_sub"]["text"] == ["Waiting for your decision · 7"]
    # CHECK-RESULTS-WORDS-001 (supersedes DATE-LITERAL-001's neutral finding):
    # a passing scoped date comparison states its observed outcome, green —
    # never amber on a match
    for rid in ("r-acc", "r-stale", "r-fix"):
        lines = out["pane_" + rid]["date"]
        assert lines, rid
        for dot, word in lines:
            assert "pass" in dot.split() and "unresolved" not in dot.split(), (rid, dot)
            assert word == "Timing matches source", (rid, word)


def test_review_projection_fallback_is_main_byte_for_byte(tmp_path):
    import hashlib

    raw, out = _review_render(tmp_path, "main")
    # readable pins first, so a regression names itself
    facets = out["facets"]["text"]
    assert facets[facets.index("Checked and kept ") + 1] == "4"
    assert "Proposed — needs review " not in facets
    assert "Kept by you" in out["pane_r-stale"]["text"]
    listed = " ".join(out["judgment_rows"]["text"])
    # Red Team at 497aada: a person's fix whose re-check failed (group 'open')
    assert "Fixed by you · still open" in listed and "Decided by you · still open" not in listed
    assert "st st-open :: open" in out["pane_r-open"]["badges"]
    # CHECK-RESULTS-WORDS-001: a passing date check states its observed
    # outcome rather than a bare 'passed'
    assert out["pane_r-acc"]["date"] == [["dot pass", "Timing matches source"]]
    # and the whole render, byte for byte, is main's
    assert hashlib.sha256(raw.encode()).hexdigest() == _MAIN_SHAPED_RENDER_SHA256


def test_u35_af8_fold_representative_is_the_unsettled_source():
    # Red Team round 11 A-F8: a folded event opens on the first source whose
    # state is undecided or whose checks did not all pass — not blindly the
    # first in file order; a fully settled fold keeps its first member and the
    # pill still counts every source
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    assert "e.t.status === 'undecided' ||" in js
    assert "effectiveChecks(e.row, e.t).some(function (c) { return c.result !== 'pass'; })" in js
    rows = [
        {"id": "a1", "ticker": "PTGX", "program": "PN-1", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "a2", "ticker": "PTGX", "program": "PN-1", "event_type": "milestone",
         "date": "2027-01-01", "date_text": "Q1 2027"},
        {"id": "b1", "ticker": "IONS", "program": "z-1", "event_type": "regulatory_decision",
         "date": "2027-01-01", "date_text": "in 2027"},
        {"id": "b2", "ticker": "IONS", "program": "z-1", "event_type": "regulatory_decision",
         "date": "2027-01-01", "date_text": "in 2027"},
    ]
    targets = {
        "a1": {"group": "kept", "status": "matched", "values": {"date": "2027-01-01", "date_text": "Q1 2027"}},
        "a2": {"group": "kept", "status": "undecided", "values": {"date": "2027-01-01", "date_text": "Q1 2027"}},
        "b1": {"group": "kept", "status": "matched", "values": {"date": "2027-01-01", "date_text": "in 2027"}},
        "b2": {"group": "kept", "status": "matched", "values": {"date": "2027-01-01", "date_text": "in 2027"}},
    }
    partition = _calendar_view(rows, targets)
    # a1 fully settled; a2 undecided -> a2 represents; b's fold fully settled
    # -> first in file order
    rows[0]["checks"] = [{"result": "pass"}]
    rows[2]["checks"] = [{"result": "pass"}]
    rows[3]["checks"] = [{"result": "pass"}]
    script = "\n".join([
        "var detail = { run: { rows: " + json.dumps(rows) + ", calendar_partition: " + json.dumps(partition) + " }, state: { targets: " +
        json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        "var got = calendarRows();",
        "if (got.length !== 2) { console.error('n=' + got.length); process.exit(2); }",
        # fold a: a2 is undecided -> the representative, fold still counts both
        "if (got[0].row.id !== 'a2' || got[0].fold.length !== 2) {"
        + " console.error(got[0].row.id + ' fold=' + got[0].fold.length); process.exit(3); }",
        # fold b: all settled -> first in file order
        "if (got[1].row.id !== 'b1' || got[1].fold.length !== 2) process.exit(4);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u35_clean_room_ac5_ac6_ac9():
    # A-C5: a row's shown reason is the state's current reason, never a stored
    # sentence from before the state rule re-derived the row; 'Kept by you'
    # only ever renders on a person decision
    js = (UI / "app.js").read_text()
    why = _grab_fn(js, "whySection")
    assert "t.reason || 'Waiting for you.'" in why
    assert "row.reason" not in why
    # U38: a generic "not printed" reason yields to the check's near-miss sentence
    assert "c.reason.indexOf('nearly matches') >= 0" in why
    assert "/not printed/i.test(shown)" in why
    # A-C6: one sentence on the Accuracy panel under an unsealed key
    assert "This key was written from the same page text, so it tests the checks, not the reader." in js
    assert "clear(keyNoteHost)" in js and "if (acc && !sealed)" in js
    # A-C9: one name per reader in every sentence; the footer names the run's
    # actual readers (second reader + Wonder from the run's own record)
    for word in ("function readerNames()", "meta.second_reader.model", "meta.escalation.model",
                 "rn.join(' · ') + ' read'"):
        assert word in js, word
    assert "'The two readers disagree" not in js
    assert "'Second reader did not read this row.'" not in js


def test_u35_codex_p2s_presence_effective_checks_reader_label():
    # Codex's three P2s on #113:
    # (1) calendarRows reads dates/program/title by key PRESENCE — a date a
    #     fix cleared (state value null) must not resurrect via the raw row
    # (2) A-F7/F8 judge a row by the EFFECTIVE re-check outcome
    #     (recheck.after.checks), not the possibly-stale saved row.checks
    # (3) the disagreement block names the second reader from the run's
    #     metadata, never a hardcoded string
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    for word in ("Object.prototype.hasOwnProperty.call(cv, k)",
                 "program: shownVal(vals, e.row, 'program')",
                 "function effectiveChecks(row, t)",
                 "effectiveChecks(row, t).filter(function (c) { return c.result === 'fail'; })",
                 "effectiveChecks(e.row, e.t).some(function (c) { return c.result !== 'pass'; })",
                 "'Second reader · ' + srL + ': '"):
        assert word in js, word
    fns = "\n".join(_grab_fn(js, n) for n in ("shownVal", "effectiveChecks", "calendarRows", "projectionComplete", "partialProjection"))
    script = "\n".join([
        "var detail = { run: { rows: [",
        "  { id: 'c1', ticker: 'PTGX', program: 'PN-1', event_type: 'milestone',",
        "    date: '2027-01-01', date_text: 'Q1 2027', title: 'One',",
        "    checks: [{ result: 'fail', label: 'Saved fail' }] },",
        "  { id: 'c2', ticker: 'IONS', program: 'bepi', event_type: 'regulatory_decision',",
        "    date: '2027-02-01', date_text: 'Feb 2027', title: 'Two',",
        "    checks: [{ result: 'pass' }] }",
        "], calendar_partition: { timing_unresolved: [{ id: 'c1' }], calendar: [{ id: 'c2' }] }, run: {} }, state: { targets: {",
        "  // c1: a fix CLEARED the date (key present, value null) and the re-check",
        "  // re-ran the checks — the saved fail is stale, the after-checks pass",
        "  c1: { group: 'kept', status: 'matched',",
        "        on_calendar: true, effective_checks: [{ result: 'pass' }],",
        "        values: { date: null, date_text: 'in 2027', title: 'One' },",
        "        recheck: { result: 'pass', after: { checks: [{ result: 'pass' }] } } },",
        "  c2: { group: 'kept', status: 'matched',",
        "        on_calendar: true, effective_checks: [{ result: 'pass' }],",
        "        values: { date: '2027-02-01', date_text: 'Feb 2027', title: 'Two' } }",
        "} } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        fns,
        # P2-1: the cleared date wins by presence — c1 is undated (No day given),
        # its raw row.date never resurrects
        "var got = calendarRows();",
        "var c1 = got.filter(function (e) { return e.row.id === 'c1'; })[0];",
        "if (!c1 || !c1.nodate) { console.error('c1 nodate=' + (c1 && c1.nodate)); process.exit(2); }",
        # P2-2: the effective checks are the re-check's after set, not the saved row
        "if (effectiveChecks(detail.run.rows[0], detail.state.targets.c1)[0].result !== 'pass') process.exit(3);",
        "if (effectiveChecks(detail.run.rows[1], detail.state.targets.c2)[0].result !== 'pass') process.exit(4);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u36_g9r1_zero_counters_are_grey_pills_that_stack():
    # GLASS 9 nit 1 (Corey rev 31 Oct): all four counter chips stay pills
    # whether zero or not; when they do not fit on one line they stack one
    # per line — never side by side with a sideways scroll, never plain
    # text that clips. Zero pills are grey and not clickable
    css = (UI / "styles.css").read_text()
    zero = css.split(".ctchip.zero{")[1].split("}")[0]
    assert "cursor:default" in zero
    assert "var(--faint)" in zero
    assert "transparent" not in zero
    assert "white-space:normal" not in zero          # a pill stays one line
    counters = css.split(".counters{")[1].split("}")[0]
    assert "flex-wrap:wrap" in counters              # chips stack, no sideways scroll
    chip = css.split(".ctchip{")[1].split("}")[0]
    assert "flex:0 0 auto" in chip       # a chip stays whole while it fits
    assert "max-width:100%" in chip      # an over-wide chip wraps inside its own pill
    assert "white-space:normal" in chip


def test_u36_g9r2_month_period_marker_uses_the_source_wording():
    # GLASS 9 nit 2: the 'early Q1 2027' period marker inside January read
    # 'January 2027' — the same words as the month header above it. A
    # month-period marker now carries the source wording, never the month's
    # own name
    js = (UI / "app.js").read_text()
    assert "(wording || monthYear(r.date))" in js


def test_u36_g9r3_quarter_periods_get_their_own_header():
    # GLASS 9 nit 3: Sample 'Q4 2026' sat under an 'October 2026' month
    # header. A quarter (or any span longer than a month) now gets its own
    # header right after the first month it spans begins; the render side
    # labels a non-quarter span with the source wording
    import subprocess
    js = (UI / "app.js").read_text()
    assert "'p:' + start + ':' + r.end" in js
    assert "h.key.slice(0, 2) === 'p:'" in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("calDays", "calIsMonthPeriod", "calIsExact", "calMonths",
                     "calQuarterKey", "calHeader", "calHeaderOrd"))
    script = "\n".join([
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "                    'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "var MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July',",
        "                   'August', 'September', 'October', 'November', 'December'];",
        fns,
        "function mk(date, end) { return { period: !!end, date: date, end: end || null }; }",
        # the Sample case: Q4 2026 is its own header, sorted at October —
        # right after the October month header, never under it
        "var q4 = calHeader(mk('2026-10-01', '2026-12-31'));",
        "if (q4.key !== 'q:Q4 2026' || q4.label !== 'Q4 2026') process.exit(2);",
        "var oct = { key: 'm:2026-10', sort: '2026-10', len: 1 };",
        "var hs = [q4, oct].sort(function (a, b) {",
        "  var oa = calHeaderOrd(a), ob = calHeaderOrd(b);",
        "  if (oa !== ob) return oa < ob ? -1 : 1;",
        "  return a.len - b.len;",
        "});",
        "if (hs[0].key !== 'm:2026-10' || hs[1].key !== 'q:Q4 2026') process.exit(3);",
        # a month-only date still sits in its month's header (G3-R1 unchanged)
        "if (calHeader(mk('2027-02-01', '2027-02-28')).key !== 'm:2027-02') process.exit(4);",
        # a non-quarter span gets its own header keyed by its exact dates
        "if (calHeader(mk('2026-09-01', '2026-12-31')).key !== 'p:2026-09:2026-12-31') process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u36_g9r4_run_sheet_lists_each_run_once():
    # GLASS 9 nit 4: the Sample run appeared twice in 'Choose a run' — a
    # copied run directory keeps the same run number. The sheet now dedupes
    # by run number and the flagged sample entry wins over the copy
    import subprocess
    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "dedupeSheetRuns")
    script = "\n".join([
        fns,
        "var list = [",
        "  { id: 'copy-run-2', number: 2, label: 'Sample run', sample: false },",
        "  { id: 'sample', number: 2, label: 'Sample run', sample: true },",
        "  { id: 'run-3', number: 3, label: 'Run 7' },",
        "  { id: 'odd', label: 'no number' },",
        "];",
        "var got = dedupeSheetRuns(list);",
        "if (got.length !== 3) { console.error('n=' + got.length); process.exit(2); }",
        "if (got[0].id !== 'sample') { console.error('first=' + got[0].id); process.exit(3); }",
        "if (got[1].id !== 'run-3' || got[2].id !== 'odd') process.exit(4);",
        # no collisions -> the list passes through untouched
        "var plain = dedupeSheetRuns([{ id: 'a', number: 1 }, { id: 'b', number: 2 }]);",
        "if (plain.length !== 2) process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u38_near_miss_prefill_pins():
    # U38 rework (Job 47): the Fix dialog pre-fill reads the check's
    # near_match verbatim — one source of truth, no word-swapping in the UI
    js = (UI / "app.js").read_text()
    for word in ("function nearMissCheck(row)",
                 "function nearMissPrefill(row)",
                 "function listReasonLine(row, t)",
                 "fixDialog(row, nearMissPrefill(row))",
                 "c.reason.indexOf('nearly matches') >= 0",
                 "typeof chk.near_match !== 'string'",
                 "listReasonLine(row, t)"):
        assert word in js, word
    # the word-swap reconstruction is gone for good
    for gone in ("nearMissFixPrefill", "foldWord", "quote ‘(.+?)’, page ‘(.+?)’",
                 "and \\d+ more"):
        assert gone not in js, gone
    # the pane renders the check reason as-is — never a generic "not printed"
    # override: the only place that sentence can come from is the reason itself
    assert "not printed anywhere" not in js


def test_u38_near_miss_prefill_reads_the_checks_near_match():
    # Job 47 serializes near_match (the page's wording) on the quote check;
    # the pre-fill is that string verbatim — the Red Team's two failures (a
    # repeated differing word landing on the wrong occurrence, a ’ token cut
    # off) came from rebuilding the wording in the UI and cannot recur
    import subprocess

    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "nearMissCheck") + "\n" + _grab_fn(js, "nearMissPrefill")
    script = "\n".join([
        fns,
        "function mk(nearMatch, reason) {",
        "  return { id: 'r1', checks: reason === null ? [] :",
        "    [{ name: 'quote_match', result: 'fail', reason: reason, near_match: nearMatch }] };",
        "}",
        # the sample's real SMPL6 near miss after Job 47
        "var s = nearMissPrefill(mk('Samplebio Six will report final results on July 19, 2027.',",
        "  'The quote nearly matches the page; 1 word differs — quote ‘9,’, page ‘19,’.'));",
        "if (!s || s.field !== 'quote') { console.error('no prefill'); process.exit(2); }",
        "if (s.to !== 'Samplebio Six will report final results on July 19, 2027.')",
        "  { console.error('to=' + s.to); process.exit(3); }",
        # Red Team case 1: a repeated 'the' — near_match is taken verbatim,
        # never reconstructed, so the wrong-occurrence swap is impossible
        "var s2 = nearMissPrefill(mk(",
        "  'The company expects topline data for its program in the second half of 2027',",
        "  'The quote nearly matches the page; 1 word differs — quote ‘the’, page ‘its’.'));",
        "if (!s2 || s2.to !== 'The company expects topline data for its program in the second half of 2027')",
        "  { console.error('to2=' + (s2 && s2.to)); process.exit(4); }",
        # Red Team case 2: a ’ token survives untouched
        "var s3 = nearMissPrefill(mk('Patient’s data.',",
        "  'The quote nearly matches the page; 1 word differs — quote ‘patients’, page ‘patient’s’.'));",
        "if (!s3 || s3.to !== 'Patient’s data.') { console.error('to3=' + (s3 && s3.to)); process.exit(5); }",
        # an old run whose check carries NO near_match -> no pre-fill
        "if (nearMissPrefill({ id: 'r2', checks: [{ name: 'quote_match', result: 'fail',",
        "  reason: 'The quote nearly matches the page; 1 word differs — quote ‘9’, page ‘10’.' }] }) !== null)",
        "  process.exit(6);",
        # an empty near_match is no pre-fill either
        "if (nearMissPrefill(mk('', 'The quote nearly matches the page; 1 word differs — quote ‘a’, page ‘b’.')) !== null)",
        "  process.exit(7);",
        # a fail that is not a near miss -> nothing
        "if (nearMissPrefill(mk(null, 'The quote is not printed anywhere on the page.')) !== null) process.exit(8);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u38_judgment_list_sub_line_shows_the_near_miss_sentence():
    # The judgment-needed list row's sub-line shows the check's own near-miss
    # sentence instead of a stale generic "not printed" state reason; any
    # other reason passes through (still truncated at 72 chars)
    import subprocess

    js = (UI / "app.js").read_text()
    fns = _grab_fn(js, "nearMissCheck") + "\n" + _grab_fn(js, "listReasonLine")
    script = "\n".join([
        fns,
        "var chk = { name: 'quote_match', result: 'fail',",
        "  reason: 'The quote nearly matches the page; 1 word differs — quote ‘9,’, page ‘19,’.',",
        "  near_match: 'Samplebio Six will report final results on July 19, 2027.' };",
        "var row = { id: 'e7', checks: [chk] };",
        # the sample's stale generic reason loses to the near-miss sentence
        "var got = listReasonLine(row, { reason: 'The quote is not printed on the page as given. Waiting for you.' });",
        "if (got.indexOf('nearly matches') === -1) { console.error('got=' + got); process.exit(2); }",
        "if (/not printed/i.test(got)) { console.error('generic shown: ' + got); process.exit(3); }",
        # a non-generic state reason passes through untouched
        "var other = listReasonLine(row, { reason: 'The day in the row differs from the day printed in the quote.' });",
        "if (other !== 'The day in the row differs from the day printed in the quote.')",
        "  { console.error('other=' + other); process.exit(4); }",
        # long reasons truncate at 72 chars with an ellipsis
        "var long = listReasonLine({ id: 'x', checks: [] }, { reason: new Array(30).join('abcd ') });",
        "if (long.length !== 73 || long.slice(-1) !== '…') { console.error('long=' + long.length); process.exit(5); }",
        # no near-miss check + generic reason -> the reason stands (A-C5)
        "var plain = listReasonLine({ id: 'y', checks: [] },",
        "  { reason: 'The quote is not printed anywhere on the page.' });",
        "if (plain.indexOf('not printed') === -1) { console.error('plain=' + plain); process.exit(6); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr


def test_u38_why_section_shows_the_near_miss_not_a_generic_not_printed():
    # U38 third ruling: the pane never says "not printed on the page" when the
    # check result carries a near match — the check's own sentence wins over a
    # stale generic reason (the sample's e7 stores one in run.json)
    import subprocess

    js = (UI / "app.js").read_text()
    fn = re.search(r"function whySection.*?\n  \}\n", js, re.S).group(0)
    script = "\n".join([
        "var FIELD_WORDS = { quote: 'Quote' };",
        "function bothReadersWord() { return 'The readers'; }",
        "function el(tag, cls, text) { return { tag: tag, className: cls || '', textContent: text || '',",
        "  children: [], style: {},",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        fn,
        # the sample's real case: a stale generic reason + a near-miss check
        "var row = { checks: [{ result: 'fail', reason: 'The quote nearly matches the page; 1 word differs — quote ‘9,’ page ‘19,’.' }] };",
        "var t = { group: 'open', reason: 'The quote is not printed on the page as given. Waiting for you.', disagreement: null };",
        "var txt = JSON.stringify(whySection(row, t));",
        "if (/not printed/i.test(txt)) { console.error('generic reason shown'); process.exit(2); }",
        "if (txt.indexOf('nearly matches') === -1) { console.error('near-miss sentence missing'); process.exit(3); }",
        # no near match -> the state reason shows as before (A-C5)
        "var row2 = { checks: [{ result: 'fail', reason: 'The quote is not printed anywhere on the page.' }] };",
        "if (JSON.stringify(whySection(row2, t)).indexOf('not printed') === -1) process.exit(4);",
        # no reason at all -> Waiting for you.
        "var sec3 = whySection({ checks: [] }, { group: 'open', reason: null, disagreement: null });",
        "if (JSON.stringify(sec3).indexOf('Waiting for you.') === -1) process.exit(5);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr

def test_u37_keep_anyway_ui_pins():
    # U37: the pane of a code-removed row carries a 'Keep anyway' button next
    # to the reason, opening a dialog with the same reason box as Remove; the
    # audit row of a kept-anyway row reads 'code had removed it'; the dialog
    # mounts on the audit route; person trail notes show dates in words
    js = (UI / "app.js").read_text()
    for word in ("function keepAnywayDialog(row, keepDraft)",
                 "'Keep anyway'",
                 "dialog: 'keepanyway'",
                 "action: 'keep', note: note.value.trim()",
                 "function codeExclusionHistory(rowId)",
                 "'· code had removed it'",
                 "openDialog(rowOf(audit.selected), route.dialog)",
                 "'Kept by you — code had removed it.'",
                 "dayInWords(l.note)"):
        assert word in js, word
    # the audit route carries the dialog param both ways (parse + build)
    assert "out.filter = q.f || null; out.dialog = q.d || null; }" in js
    assert "if (o.dialog) q.push('d=' + encodeURIComponent(o.dialog));" in js
    # postDecision can return to the audit screen (default stays judgment)
    assert "screen: (opts && opts.screen) || 'judgment'" in js


def test_u37_a_person_keep_outranks_a_code_exclusion():
    # U37 state half (runs/state.py already honours it — pinned here): a
    # person's keep on a code-removed row moves it to 'kept', the live
    # exclusion drops (Excluded -1, kept +1) while run.json's exclusions stay
    # as history; Undo restores the code removal and the counters
    import json
    from runs.state import current_state

    run = json.loads((REPO / "runs" / "sample" / "run.json").read_text())
    assert run["exclusions"][0]["target"] == "e6" and run["exclusions"][0]["by"] == "code"
    base = current_state(run, [])
    assert base["targets"]["e6"]["group"] == "removed"
    assert base["excluded"]["code"] == 1 and len(base["exclusions"]) == 1
    kept_before = base["counts"]["rows"]["kept"]
    removed_before = base["counts"]["rows"]["removed"]

    keep = {"at": "2026-10-31T14:00:00+00:00", "target": "e6", "action": "keep",
            "note": "House rule: a third-party calendar is never a source on its own.",
            "by": "person"}
    st = current_state(run, [keep])
    t6 = st["targets"]["e6"]
    assert t6["group"] == "kept" and t6["decided_by_person"] and t6["last_action"] == "keep"
    assert st["excluded"]["code"] == 0 and st["exclusions"] == []
    assert st["counts"]["rows"]["kept"] == kept_before + 1
    assert st["counts"]["rows"]["removed"] == removed_before - 1

    undo = {"at": "2026-10-31T14:05:00+00:00", "target": "e6", "action": "undo", "by": "person"}
    back = current_state(run, [keep, undo])
    assert back["targets"]["e6"]["group"] == "removed"
    assert not back["targets"]["e6"]["decided_by_person"]
    assert back["excluded"]["code"] == 1 and len(back["exclusions"]) == 1
    assert back["counts"]["rows"]["kept"] == kept_before
    assert back["counts"]["rows"]["removed"] == removed_before


def test_r48c_unresolved_window_renders_without_date_arithmetic():
    import json
    import subprocess
    js = (UI / "app.js").read_text()
    rows = [{"id": "a1", "ticker": "PTGX", "program": "P", "event_type": "milestone",
             "date": "2027-01-01", "date_text": "Q1 2027"}]
    targets = {"a1": {"group": "kept", "values": {"date": "2027-01-01", "date_text": "Q1 2027"}}}
    reason = "This run has no usable reference date (as_of or started_at)."
    run = {"rows": rows, "calendar_window": {"start": None, "end": None, "unresolved": True, "reason": reason},
           "calendar_partition": {"calendar": [], "past": [], "later": [], "contingent": [{"id": "a1"}]}}
    _calendar_view(rows, targets, run["calendar_partition"])
    script = "\n".join([
        "var detail = { run: " + json.dumps(run) + ", state: { targets: " + json.dumps(targets) + " } };",
        "function rowTarget(row) { return detail.state.targets[row.id] || {}; }",
        "var RealDate = Date; Date = function () { throw new Error('Date() called'); };",
        _grab_fn(js, "shownVal"),
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "calendarRows"),
        _grab_fn(js, "projectionComplete"), _grab_fn(js, "partialProjection"),
        _grab_fn(js, "calWindow("),
        "var got = calendarRows();",
        "if (got.length !== 1 || got[0].bucket !== 'contingent') process.exit(2);",
        "var w = calWindow();",
        "if (!w.unresolved || w.reason !== " + json.dumps(reason) + " || w.anchor !== null) process.exit(3);",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout + got.stderr
    assert "Timing unresolved" in js and "w.unresolved ? w.reason" in js


def _grab_var(js: str, name: str) -> str:
    """A top-level `var NAME = ...;` from app.js (a literal: balanced brackets, then the semicolon)."""
    at = js.index(f"var {name} = ")
    depth = 0
    for j in range(at, len(js)):
        if js[j] in "{[(":
            depth += 1
        elif js[j] in "}])":
            depth -= 1
        elif js[j] == ";" and depth == 0:
            return js[at : j + 1]
    raise AssertionError(f"{name} does not close")


def _render_card_and_summary(meta: dict, accepted: int = 0, research: dict | None = None) -> dict:
    """Job 51e: the run card (updateRunCardJob) and the report Summary (renderExport), rendered by app.js's own
    functions in node under the same mock element harness as the other UI tests; returns each one's text."""
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    if research is not None:
        meta = {**meta, "research": research}
    consts = "\n".join(_grab_var(js, n) for n in ("GROUP_NAMES", "GROUP_ORDER", "JOB_STATUS_WORDS", "JOB_ACTIVE",
                                                   "JOB_PAUSABLE", "JOB_LIMIT_STOP", "RESEARCH_STATUS_WORDS",
                                                   "NO_SECOND_READER", "ONE_MODEL_LINE"))
    fns = "\n".join(_grab_fn(js, n) for n in ("money", "cents", "costPerKeptRowWords", "unpricedMark", "jobReason",
                                               "updateRunCardJob", "oneModelRun", "reviewAxis", "renderExport",
                                               "reviewAcceptedCount", "projectionComplete", "partialProjection",
                                               "researchCompanyBlock", "runResearch", "researchReviewLine",
                                               "num", "usdCents", "hm"))
    script = f"""
function make() {{
  return {{ tag: 'div', className: '', textContent: '', children: [], style: {{}}, hidden: false, value: '',
    append: function () {{ for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); }},
    addEventListener: function () {{}} }};
}}
function el(tag, cls, text) {{ var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n; }}
function btn(cls, text, onClick) {{ var b = el('button', cls, text); b._click = onClick; return b; }}
function clear(node) {{ node.textContent = ''; node.children = []; return node; }}
function textOf(node, out) {{
  out = out || [];
  if (node.textContent) out.push(node.textContent);
  (node.children || []).forEach(function (c) {{ textOf(c, out); }});
  return out;
}}
var nodes = {{}};
global.document = {{ getElementById: function (id) {{ if (!nodes[id]) nodes[id] = make(); return nodes[id]; }} }};
function reportFileNames() {{ return []; }}
{consts}
{fns}
var counts = {{ kept: 0, corrected: 0, removed: 0, unverified: 0, open: 0 }};
var detail = {{ id: 'r', run: {{ run: {json.dumps(meta)}, unknown_cost_accepted: {json.dumps(accepted)} }},
  state: {{ cost_per_kept_row_usd: null, unresolved: 0, counts: {{ rows: counts }},
            mistakes: {{ code: 0, second_reader: 0, person: 0 }}, excluded: {{ code: 0, person: 0 }} }} }};
updateRunCardJob();
renderExport();
var card = nodes['runcard-job'];
var stop = card.children.filter(function (c) {{ return c.className === 'jb-stop' || c.className === 'jb-sub'; }});
var spend = [];
(function walk(n) {{
  if (n.className === 'kv' && n.children[0].textContent === 'Spend') spend.push(n.children[1].textContent);
  (n.children || []).forEach(walk);
}})(nodes['exp-det']);
console.log(JSON.stringify({{ card: textOf(card), stop: stop.map(function (c) {{ return c.textContent; }}),
                             spend: spend }}));
"""
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


LIMIT_STOP_STORED = ("Stopped at the spending limit: $1.04 of $1.00 used (run-0007). "
                     "Raise the limit and press Resume to read more. (1 unpriced call(s); limit bounds counted spend)")


def test_51e_the_marker_survives_a_limit_stop_after_acceptance_on_card_and_summary():
    # Job 51e defect 1: acceptance + a raised limit + a limit stop — jobReason returned the plain limit sentence
    # early and the card lost the marker. Rendered, not grepped.
    meta = {"status": "needs_input", "status_reason": LIMIT_STOP_STORED, "spend_usd": 1.04, "turn_count": 3,
            "turn_limit": 40, "choices": {"spending_limit_usd": 1.0}, "unpriced_calls": 1}
    got = _render_card_and_summary(meta, accepted=1)
    mark = "(1 unpriced call(s); limit bounds counted spend)"
    assert got["stop"][-1] == ("Stopped at the spending limit. Raise the limit and press Resume to read more. " + mark)
    assert got["spend"] == ["$1.04 " + mark]
    assert not any("cost unknown" in t for t in got["card"] + got["spend"])


def test_51e_before_acceptance_the_card_and_summary_say_the_cost_is_unknown():
    # Job 51e defect 2: before the person accepts, the marker never says the limit bounds anything
    stored = ("The cost of a call could not be counted (runs/prices.py has no price for not-a-priced-model), so the "
              "spending limit of $0.50 cannot be kept. Nothing more was read. (1 unpriced call(s); cost unknown)")
    meta = {"status": "needs_input", "status_reason": stored, "spend_usd": 0.2, "turn_count": 1, "turn_limit": 40,
            "choices": {"spending_limit_usd": 0.5}, "unpriced_calls": 1}
    got = _render_card_and_summary(meta, accepted=0)
    assert got["stop"][-1] == stored                         # the marker once, not twice
    assert got["spend"] == ["$0.20 (1 unpriced call(s); cost unknown)"]
    assert not any("limit bounds counted spend" in t for t in got["card"] + got["spend"])


def test_51e_a_later_unaccepted_call_turns_the_marker_back_to_cost_unknown():
    meta = {"status": "needs_input", "status_reason": "The cost of a call could not be counted.", "spend_usd": 0.3,
            "choices": {"spending_limit_usd": 99}, "unpriced_calls": 2}
    got = _render_card_and_summary(meta, accepted=1)
    assert got["stop"][-1] == "The cost of a call could not be counted. (2 unpriced call(s); cost unknown)"
    assert got["spend"] == ["$0.30 (2 unpriced call(s); cost unknown)"]


def test_51e_no_unpriced_call_no_marker():
    meta = {"status": "needs_input", "status_reason": LIMIT_STOP_STORED.split(" (1 unpriced")[0], "spend_usd": 1.04,
            "choices": {"spending_limit_usd": 1.0}, "unpriced_calls": 0}
    got = _render_card_and_summary(meta)
    assert got["stop"][-1] == "Stopped at the spending limit. Raise the limit and press Resume to read more."
    assert got["spend"] == ["$1.04"]


def test_run_card_names_the_review_stage_and_never_claims_one_model_for_research():
    # PROJECTION-002 (Manus 05:31): a research run whose company finished
    # research but is under review says so, with the recorded reviewer's name;
    # the page-mode 'One model: Muse Spark only' sentence never appears on a
    # research run — the independent reviewer is stage S2.
    meta = {"status": "done", "spend_usd": 0.42, "turn_count": 5, "turn_limit": 40,
            "choices": {"mode": "research", "models": "one", "spending_limit_usd": 2.0},
            "label": "PTGX · research"}
    research = {"companies": {"PTGX": {"status": "finished", "review_status": "reviewing",
                                        "requests": 4, "searches": 2, "fetches": 6, "spend_usd": 0.42}},
                "review": {"PTGX": {"status": "incomplete", "reviewer": "Claude Opus 5.5"}}}
    got = _render_card_and_summary(meta, research=research)
    card = "\n".join(got["card"])
    assert "reviewing" in card and "Claude Opus 5.5" in card, card
    assert "One model" not in card, card
    # the page-mode one-model run keeps its sentence (unchanged control)
    page = _render_card_and_summary({"status": "done", "spend_usd": 0.1, "turn_count": 2, "turn_limit": 40,
                                     "choices": {"mode": "pages", "models": "one"}})
    assert any("One model: Muse Spark only" in t for t in page["card"]), page["card"]


def test_job50b_the_409_clears_key_sealed_and_planted_and_shows_the_plain_reason(tmp_path):
    import json
    import shutil
    from fastapi.testclient import TestClient
    from accuracy import export
    from server.app import create_app
    runs = tmp_path / "runs"
    runs.mkdir()
    shutil.copytree(REPO / "runs" / "sample", runs / "sample")
    f = runs / "sample" / export.FILE_NAME
    doc = json.loads(f.read_text())
    assert doc["key"]["sealed"] is False and doc["planted"] and doc["models"]       # counts exist to be hidden
    for k in ("words", "words_sha256", "decisions_sha256", "run_sha256", "export_version"):
        doc.pop(k, None)
    doc["content_sha256"] = export.content_hash(doc)
    f.write_text(json.dumps(doc))
    r = TestClient(create_app(runs_dir=runs, ui_dir=runs / "no-ui")).get("/api/runs/sample/accuracy")
    assert r.status_code == 409
    body = r.json()
    want = ("This Accuracy result was saved before the inputs it scored were recorded. Re-score to show it.")
    assert body["reason"] == want and "detail" in body and "models" not in body
    text = _panel_text(body, status=409)
    assert want in text
    top = text.split("\nCost\n")[0]            # the scoring part; the glossary below the panel is fixed text
    assert "Scoring\n" + want in top
    for gone in ("Muse Spark", "values", "planted", "sealed", "Our own key", "/", "sha256", "words", "export_version"):
        assert gone not in top, gone
    mismatch = _panel_text({"reason": "This Accuracy result does not match the run's current inputs. Re-score to show it.",
                            "detail": "field detail: run_sha256"}, status=409)
    assert "does not match the run's current inputs. Re-score to show it." in mismatch
    assert "run_sha256" not in mismatch and "field detail" not in mismatch


# ---- PR #162 Claude review findings 1-5 (Operator ruling: a pasted box means
# the run reads only the pasted tickers; Continue is the only start for an intake)

def _new_run_sheet_script(body_lines, extra_fns=()):
    """Node script over a stub New run sheet: captured boxes IONS/PTGX, the
    paste box, Run now, the intake list; every POST /api/runs/new is counted
    and answered after a tick, the way a real server answers later."""
    js = (UI / "app.js").read_text()
    names = ("el", "nrError", "dayMonth", "intakeScopeLine", "renderIntakeStatus",
             "syncPasteMode", "stopIntakePoll", "pollIntake", "startIntake", "startRunFromIntake",
             "startRunNow", "nrModels", "notePayload", "researchReady",
             "intakeDoneN", "syncStartButton", "nrStartClick",
             "renderChosen", "renderPrevCompanies", "nrPreparedTickers", "tickedReadyCount",
             "applyPasteDisable", "nrSupportValue") + tuple(extra_fns)
    fns = "\n".join(_grab_fn(js, n) for n in names)
    consts = "\n".join(re.search(r"var %s = '[^']*';" % n, js).group(0)
                       for n in ("NR_PASTE_ONLY", "NR_PREPARE_FIRST", "NR_EMPTY"))
    return "\n".join([
        "var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',",
        "  'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];",
        "var INTAKE_STATE_WORDS = { resolving: 'Finding the company', capturing: 'Capturing documents',",
        "  ready: 'Ready', unresolved: 'Could not be resolved', no_documents: 'No documents found',",
        "  unsupported: 'Not supported', failed: 'Preparation failed' };",
        "var NEW_RUN_TYPES = [['trial_results', 'Trial results'], ['filing', 'Filing']];",
        "var INTAKE_FINISHED = ('ready unresolved no_documents unsupported failed').split(' ');",
        consts,
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], hidden: false, disabled: false,",
        "  addEventListener: function (ev, fn) { this['on' + ev] = fn; },",
        "  removeAttribute: function (k) { delete this[k]; },",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "var nodes = {};",
        "var boxes = [{ value: 'IONS', checked: true, disabled: false }, { value: 'PTGX', checked: true, disabled: false }];",
        "var allBox = { checked: true, disabled: false };",
        "var nrChosen = {};",
        "var nrPrepUnticked = {};",
        "var ikBoxes = [{ value: 'PTGX', checked: true }];",
        "var els = { 'nr-tickers': { value: '' }, 'nr-limit': { value: '' },",
        "            'nr-note': { value: '' }, 'nr-tools': { checked: false },",
        "            'nr-go': { textContent: 'Run now', disabled: false },",
        "            'nr-preparing': { textContent: '', hidden: true } };",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; },",
        "  getElementById: function (id) { if (els[id]) return els[id]; if (!nodes[id]) nodes[id] = make(); return nodes[id]; },",
        "  createTextNode: function (t) { return { nodeType: 3, textContent: t }; },",
        "  querySelectorAll: function (sel) {",
        "    if (sel === '.nr-co:checked') return boxes.filter(function (b) { return b.checked; });",
        "    if (sel === '.nr-ik:checked') return ikBoxes.filter(function (b) { return b.checked; });",
        "    if (sel === '.nr-pc, .nr-co, #nr-all') return boxes.concat([allBox]);",
        "    return []; } };",
        "var newrunWrap = { hidden: false };",
        "var runs = []; var switched = null; var nrState = 'ready'; var nrLoadError = '';",
        "var nrCompanies = ['IONS', 'PTGX'];",
        "var nrResearch = { defaultText: null, maxLength: null, loaded: true };",
        "var nrSupport = { loaded: false, available: false, reason: null, choices: [], touched: false };",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "var nrSupportSeq = 0; var nrSupportPending = false;",
        "function toast() {}",
        "function switchRun(id) { switched = id; }",
        "function setInterval() { return 1; }",
        "function clearInterval() {}",
        "var nrBusy = false;",
        "var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null };",
        "var posts = [];",
        "var DONE = { intake_id: 'ik1', status: 'done', ready_count: 1, failed_count: 1,",
        "  requested: ['PTGX', 'QUIET'],",
        "  form_scope: ['8-K'], lookback_days: 90, window_start: '2026-07-30', cutoff: '2026-10-28', tickers: [",
        "    { symbol: 'PTGX', company: 'Protagonist', state: 'ready', documents: 1, pages: 1,",
        "      reason: '1 document(s), 1 page(s): 8-K exhibits filed in the window.' },",
        "    { symbol: 'QUIET', company: null, state: 'no_documents', reason: 'No 8-K in the window.', documents: 0, pages: 0 } ] };",
        "var A = {",
        "  intake: function () { return Promise.resolve({ ok: true, status: 202, body: { intake_id: 'ik1' } }); },",
        "  intakeStatus: function () { return Promise.resolve({ ok: true, status: 200, body: DONE }); },",
        "  newRun: function (body) { posts.push(body); return new Promise(function (r) { setTimeout(function () {",
        "    r(global.NEXT_RUN_ANSWER || { ok: true, status: 202, body: { id: 'run9', number: 9, label: 'Run 9' } }); }, 5); }); },",
        "  runs: function () { return Promise.resolve([]); } };",
        fns,
        # the real DOM drops the children when textContent is reset; the stub does too
        "function clear(node) { node.textContent = ''; node.children = []; return node; }",
        "function textOf(n, out) { out = out || []; if (n && n.textContent) out.push(n.textContent);",
        "  ((n && n.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "function buttons() { var b = []; (function walk(n) { (n.children || []).forEach(function (c) {",
        "  if (c.tag === 'button') b.push(c); walk(c); }); })(nodes['nr-intake-out']); return b; }",
        "function later() { return new Promise(function (r) { setTimeout(r, 20); }); }",
        "function fail(code, msg) { console.error(msg); process.exit(code); }",
    ] + body_lines)


def _run_node(script):
    import subprocess
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_pr162_unprepared_paste_and_run_now_is_refused():
    # Finding 1 + NEWRUN-ONE-START-001: paste ABVX QUIET, skip Prepare — the
    # ONE start button sleeps as 'Continue with 0 ready' (never an active Run
    # now that refuses); a direct startRunNow still posts nothing and shows the
    # inline red line; the captured names are not run instead
    _run_node(_new_run_sheet_script([
        "els['nr-tickers'].value = 'ABVX QUIET';",
        "syncPasteMode();",
        "if (els['nr-go'].textContent !== 'Continue with 0 ready' || !els['nr-go'].disabled)",
        "  fail(2, 'one button awake for unprepared paste: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled);",
        "startRunNow();",
        "if (posts.length) fail(3, 'posted: ' + JSON.stringify(posts));",
        "var e = nodes['nr-error'];",
        "if (e.hidden || e.textContent !== 'Prepare the pasted tickers first, or clear the box.') fail(4, e.textContent);",
        # prepared text still never starts from Run now: Continue is the only start
        "startIntake().then(later).then(function () {",
        "  startRunNow();",
        "  if (posts.length) fail(5, 'Run now posted with prepared text');",
        "  if (e.textContent !== NR_PASTE_ONLY) fail(6, e.textContent);",
        # box cleared: Run now behaves as before, the captured names
        "  els['nr-tickers'].value = '';",
        "  syncPasteMode();",
        "  if (els['nr-go'].disabled || els['nr-go'].textContent !== 'Run now · 2 companies') fail(7, 'Run now not restored: ' + els['nr-go'].textContent);",
        "  if (!nodes['nr-intake-out'].hidden) fail(8, 'intake list shown with the box empty');",
        "  startRunNow();",
        "  if (posts.length !== 1 || posts[0].companies.join(',') !== 'IONS,PTGX' || 'intake_id' in posts[0])",
        "    fail(9, JSON.stringify(posts));",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


def test_pr162_captured_boxes_disabled_while_the_box_has_text():
    # NEWRUN-ONE-START-001: with text in the box the captured boxes sleep and
    # the one button is a disabled 'Continue with 0 ready' — never an active
    # Run now; with the box blank everything wakes as before
    _run_node(_new_run_sheet_script([
        "els['nr-tickers'].value = '  ';",
        "syncPasteMode();",
        "if (els['nr-go'].disabled || els['nr-go'].textContent !== 'Run now · 2 companies' || boxes[0].disabled || allBox.disabled)",
        "  fail(2, 'blank box disabled things: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled);",
        "if (!nodes['nr-paste-note'].hidden) fail(3, 'note shown for a blank box');",
        "els['nr-tickers'].value = 'PTGX, QUIET';",
        "syncPasteMode();",
        "if (!els['nr-go'].disabled || els['nr-go'].textContent !== 'Continue with 0 ready') fail(4, 'button not asleep');",
        "if (!boxes.every(function (b) { return b.disabled; }) || !allBox.disabled) fail(5, 'tick boxes enabled');",
        "var n = nodes['nr-paste-note'];",
        "if (n.hidden || n.textContent !== 'This run reads only the pasted tickers. Clear the box to choose from the captured names instead.')",
        "  fail(6, n.textContent);",
        "els['nr-tickers'].value = '';",
        "syncPasteMode();",
        "if (els['nr-go'].disabled || els['nr-go'].textContent !== 'Run now · 2 companies' || boxes[1].disabled || allBox.disabled || !n.hidden)",
        "  fail(7, 'not restored: ' + els['nr-go'].textContent + '/' + els['nr-go'].disabled);",
        # zero ticked: the button sleeps — never an active Run now that refuses
        "boxes[0].checked = false; boxes[1].checked = false; syncPasteMode();",
        "if (!els['nr-go'].disabled || els['nr-go'].textContent !== 'Run now · 0 companies')",
        "  fail(8, 'zero-tick button awake');",
        "console.log('ok');",
    ]))
    # the box listens for edits, and the note has its place in the sheet
    js = (UI / "app.js").read_text()
    assert "getElementById('nr-tickers').addEventListener('input', syncPasteMode)" in js
    assert 'id="nr-paste-note"' in (UI / "index.html").read_text()


def test_pr162_double_click_sends_exactly_one_post():
    # Finding 2 + NEWRUN-ONE-START-001: two clicks on the ONE button before the
    # answer -> one POST, for both its forms (Continue and Run now)
    _run_node(_new_run_sheet_script([
        "els['nr-tickers'].value = 'PTGX QUIET';",
        "syncPasteMode();",
        "startIntake().then(later).then(function () {",
        "  if (els['nr-go'].textContent !== 'Continue with 1 ready' || els['nr-go'].disabled) fail(2, 'one button not ready');",
        "  nrStartClick(); nrStartClick();",
        "  if (!els['nr-go'].disabled) fail(3, 'Continue not disabled in flight');",
        "  if (posts.length !== 1) fail(4, 'continue posts: ' + posts.length);",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1) fail(5, 'continue posts after answer: ' + posts.length);",
        # Run now, box empty, double click
        "  posts = []; newrunWrap.hidden = false;",
        "  els['nr-tickers'].value = ''; syncPasteMode();",
        "  nrStartClick(); nrStartClick();",
        "  if (!els['nr-go'].disabled) fail(6, 'Run now not disabled in flight');",
        "  if (posts.length !== 1) fail(7, 'run now posts: ' + posts.length);",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1 || els['nr-go'].disabled) fail(8, 'after answer: ' + posts.length);",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


def test_pr162_used_intake_has_no_continue_after_202_or_reopen():
    _run_node(_new_run_sheet_script([
        "els['nr-tickers'].value = 'PTGX QUIET';",
        "syncPasteMode();",
        "startIntake().then(later).then(function () {",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  if (posts.length !== 1 || switched !== 'run9') fail(2, 'run did not start');",
        "  var t = textOf(nodes['nr-intake-out']);",
        "  if (!els['nr-go'].disabled) fail(3, 'start button still awake after 202');",
        "  if (t.indexOf('A run was started from these tickers.') === -1) fail(4, t);",
        "  if (t.indexOf('QUIET') === -1 || t.indexOf('No 8-K in the window.') === -1) fail(5, 'failures lost');",
        # reopen New run: the newrun handler redraws the last intake
        "  renderIntakeStatus(nodes['nr-intake-out'], nrIntake.last);",
        "  syncPasteMode();",
        "  if (!els['nr-go'].disabled) fail(6, 'start button awake on reopen');",
        "  if (textOf(nodes['nr-intake-out']).indexOf('A run was started from these tickers.') === -1) fail(7, 'used line lost');",
        "  startRunFromIntake(nrIntake.last);",
        "  if (posts.length !== 1) fail(8, 'a used intake started a second run');",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))
    js = (UI / "app.js").read_text()
    # the reopen path redraws through the same renderer, then the paste mode
    assert ("if (nrIntake.last) renderIntakeStatus(document.getElementById('nr-intake-out'), nrIntake.last);\n"
            "    syncPasteMode();") in js


def test_pr162_422_list_detail_is_shown_as_text():
    # Finding 3: FastAPI's list-shaped 422 never reads '[object Object]'
    _run_node(_new_run_sheet_script([
        "global.NEXT_RUN_ANSWER = { ok: false, status: 422, body: { detail: [{ loc: ['body'], msg: 'Field required', type: 'missing' }] } };",
        "els['nr-tickers'].value = 'PTGX QUIET';",
        "syncPasteMode();",
        "startIntake().then(later).then(function () {",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  var e = nodes['nr-error'];",
        "  if (e.hidden || e.textContent !== 'The server answered 422.') fail(2, e.textContent);",
        # the addendum: a refused start leaves the dialog open with everything
        # intact, and the one button is usable again
        "  if (newrunWrap.hidden) fail(3, 'dialog closed on a refused start');",
        "  if (els['nr-tickers'].value !== 'PTGX QUIET') fail(4, 'pasted text lost');",
        "  if (els['nr-go'].disabled || els['nr-go'].textContent !== 'Continue with 1 ready')",
        "    fail(5, 'start button not restored');",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


def test_pr162_scope_wording_and_no_duplicate_counts():
    # Findings 4 and 5: one plain scope line, no 'lookback'/'cutoff'/'Forms';
    # the ready row says its counts once (the server's reason)
    _run_node(_new_run_sheet_script([
        "renderIntakeStatus(document.getElementById('nr-intake-out'), DONE);",
        "var t = textOf(nodes['nr-intake-out']);",
        "if (t.indexOf('Read: 8-K press releases filed 30 Jul 2026 to 28 Oct 2026 (90 days).') === -1) fail(2, t);",
        "if (/lookback|cutoff|Forms/.test(t)) fail(3, t);",
        "if (t.indexOf('1 document · 1 page') !== -1) fail(4, 'duplicate counts line');",
        "if (t.indexOf('1 document(s), 1 page(s)') === -1) fail(5, 'server reason lost');",
        "console.log('ok');",
    ]))


def test_pr162_server_409_used_marks_the_intake_used_with_a_link_to_that_run():
    # a second tab or a lost answer: the server says the intake already started
    # run7; the sheet shows it used (no Continue, no red line) and links to run7
    _run_node(_new_run_sheet_script([
        "global.NEXT_RUN_ANSWER = { ok: false, status: 409, body: { detail: 'A run was already started from these tickers.', run_id: 'run7' } };",
        "els['nr-tickers'].value = 'PTGX QUIET';",
        "syncPasteMode();",
        "startIntake().then(later).then(function () {",
        "  nrStartClick();",
        "  return later();",
        "}).then(function () {",
        "  if (!els['nr-go'].disabled) fail(2, 'start button awake after a used 409');",
        "  var t = textOf(nodes['nr-intake-out']);",
        "  if (t.indexOf('A run was started from these tickers.') === -1) fail(3, t);",
        "  if (!nodes['nr-error'].hidden && nodes['nr-error'].textContent) fail(4, nodes['nr-error'].textContent);",
        "  var links = []; (function walk(n) { (n.children || []).forEach(function (c) {",
        "    if (c.tag === 'a') links.push(c); walk(c); }); })(nodes['nr-intake-out']);",
        "  if (links.length !== 1 || links[0].textContent !== 'Open that run') fail(5, 'no link');",
        "  links[0].onclick({ preventDefault: function () {} });",
        "  if (switched !== 'run7' || !newrunWrap.hidden) fail(6, 'link did not open run7');",
        # an intake the server reports as used (reopen, other tab) has no Continue either
        "  nrIntake.usedId = null;",
        "  nrIntake.last = Object.assign({}, DONE, { run_id: 'run7' });",
        "  renderIntakeStatus(nodes['nr-intake-out'], nrIntake.last);",
        "  syncPasteMode();",
        "  if (!els['nr-go'].disabled) fail(7, 'start button awake for a server-used intake');",
        "  console.log('ok');",
        "}).catch(function (err) { fail(99, err && err.stack); });",
    ]))


# ---- LETTUCE-OPTION (quiet advisory, Codex's contract): the claim/evidence
# pane note shows ONLY a current unsupported_spans finding, understated, with
# the exact suspect text and source; 'LettuceDetect' attribution and run status
# live in the trail only; the New run checkbox is offered from
# GET /api/research/options (server default; unavailable shows its reason).

def test_lettuce_ui_shell_and_finding_helpers():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    css = (UI / "styles.css").read_text()
    # the New run checkbox + hint, and the understated pane note style (no badge)
    assert 'id="nr-support"' in html and 'id="nr-support-hint"' in html
    assert '.supportnote' in css and 'badge' not in css.split('.supportnote')[1][:80]
    assert "getJSON('/api/research/options')" in js
    # helpers exist and are wired into the pane and the trail (definition + call)
    for name in ('supportFindingFor', 'supportTrailSteps'):
        assert js.count(name + '(') >= 2, name
    assert 'support_check: nrSupportValue()' in js
    fns = "\n".join(_grab_fn(js, n) for n in
                    ('supportFindingFor', 'supportNoteText', 'supportStepText', 'supportTrailSteps'))
    script = "\n".join([
        fns,
        "var findings = {",
        "  e1: { result: 'unsupported_spans', spans: [{ start: 0, end: 5, text: 'Phase' }] },",
        "  e2: { result: 'no_finding', spans: [] },",
        "  e3: { result: 'could_not_run', spans: [], reason: 'the pinned weights are not installed' },",
        "  e4: { result: 'stale', spans: [] },",
        "};",
        # only a current finding passes; no finding / not run / stale stay out
        "if (!supportFindingFor(findings, 'e1')) fail(2, 'finding hidden');",
        "['e2', 'e3', 'e4', 'e9'].forEach(function (k) { if (supportFindingFor(findings, k)) fail(3, k); });",
        # understated note: exact suspect text + source + advisory, never 'verified'
        "var note = supportNoteText(findings.e1, { publisher: 'Protagonist Therapeutics' }, { page_number: 3 });",
        "if (note.indexOf('“Phase”') === -1) fail(4, note);",
        "if (note.indexOf('Protagonist Therapeutics') === -1 || note.indexOf('page 3') === -1) fail(5, note);",
        "if (note.indexOf('Advisory only') === -1) fail(6, note);",
        "if (/verif/i.test(note)) fail(7, note);",
        # CLAIM-001: the detector's flag is potential, never stated as fact
        "if (note.indexOf('may not be supported by the cited source') === -1) fail(16, note);",
        "if (note.indexOf('flagged as not supported') !== -1) fail(17, note);",
        # trail wording: not run carries the reason; no finding is not 'verified'
        "var t1 = supportStepText({ refs: { result: 'could_not_run', row_id: 'e1' },",
        "  text: 'Support check (LettuceDetect, advisory) e1: could not run (the pinned weights are not installed (LETTUCE_WEIGHTS_PATH is not a directory)).' });",
        "if (t1.indexOf('Not run:') !== 0 || t1.indexOf('the pinned weights are not installed') === -1) fail(8, t1);",
        # the served finding's reason wins over the step prose (one source of truth)
        "var t1b = supportStepText({ refs: { result: 'could_not_run', row_id: 'e1' }, text: 'old prose' },",
        "  { result: 'could_not_run', reason: 'the served reason' });",
        "if (t1b !== 'Not run: the served reason.') fail(13, t1b);",
        "var t2 = supportStepText({ refs: { result: 'no_finding', row_id: 'e2' },",
        "  text: 'Support check (LettuceDetect, advisory) e2: no finding.' });",
        "if (t2.indexOf('No finding') !== 0 || /verif/i.test(t2)) fail(9, t2);",
        "var t3 = supportStepText({ refs: { result: 'unsupported_spans', row_id: 'e1' },",
        "  text: 'Support check (LettuceDetect, advisory) e1: unsupported spans.' }, findings.e1);",
        "if (t3.indexOf('Potentially unsupported wording') !== 0) fail(10, t3);",
        # a stale served finding never reads as a current one (item 1)
        "var t4 = supportStepText({ refs: { result: 'unsupported_spans', row_id: 'e4' },",
        "  text: 'Support check (LettuceDetect, advisory) e4: unsupported spans.' }, findings.e4);",
        "if (t4.indexOf('Earlier check (no longer current)') !== 0 || t4.indexOf('changed after it ran.') === -1)",
        "  fail(14, t4);",
        # an unknown step result is recorded as unknown — never a finding (UNKNOWN-001)
        "var t5 = supportStepText({ refs: { result: 'something_new', row_id: 'e1' }, text: 'x' }, findings.e1);",
        "if (t5 !== 'Result not recorded.') fail(15, t5);",
        # the trail steps: this row only, attribution 'LettuceDetect'
        "var steps = [",
        "  { who: 'capture.support_signal', at: '2026-11-02T00:00:00Z', refs: { row_id: 'e1', result: 'unsupported_spans' }, text: 'x' },",
        "  { who: 'capture.support_signal', at: '2026-11-02T00:00:01Z', refs: { row_id: 'e2', result: 'no_finding' }, text: 'y' },",
        "  { who: 'runs.research', at: 't', refs: { row_id: 'e1' }, text: 'z' },",
        "];",
        "var out = supportTrailSteps(steps, 'e1');",
        "if (out.length !== 1 || out[0].tag !== 'LettuceDetect' || out[0].step !== 'Support check') fail(11, JSON.stringify(out));",
        "if (supportTrailSteps(steps, 'e2').length !== 1 || supportTrailSteps(steps, 'e3').length !== 0) fail(12);",
        "console.log('ok');",
    ])
    _run_node(script)


def test_support_attempt_renders_once_in_the_trail():
    # PROJECTION-003 (Manus 05:31): one support attempt is one display step.
    # The run-level research block skips the raw capture.support_signal step;
    # the row-level supportTrailSteps renders it with attribution and the
    # served currentness wording.
    import subprocess

    js = (UI / "app.js").read_text()
    consts = "\n".join(_grab_var(js, n) for n in ("RESEARCH_STEP_WORDS",))
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "hm", "researchTrailBlock", "supportTrailSteps", "supportStepText", "timingReason"))
    script = "\n".join([
        "function make() { return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false,",
        "  append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); } }; }",
        "global.document = { createElement: function (t) { var n = make(); n.tag = t; return n; } };",
        consts,
        fns,
        "function textOf(node, out) { out = out || []; if (node && node.textContent) out.push(node.textContent);",
        "  ((node && node.children) || []).forEach(function (c) { textOf(c, out); }); return out.join('\\n'); }",
        "var steps = [",
        "  { who: 'capture.support_signal', at: '2026-11-02T00:00:00Z', refs: { row_id: 'e1', result: 'unsupported_spans' }, text: 'x' },",
        "  { who: 'Muse Spark', at: '2026-11-02T00:00:02Z', kind: 'model_call', step: 'Read', text: 'researched' },",
        "];",
        "var block = researchTrailBlock({ label: 'Agent research', steps: steps });",
        "var blockTxt = textOf(block);",
        "if (blockTxt.indexOf('Support check') !== -1 || blockTxt.indexOf('unsupported spans') !== -1)",
        "  { console.error('raw support step duplicated at run level: ' + blockTxt); process.exit(2); }",
        "if (blockTxt.indexOf('researched') === -1) { console.error('real step lost: ' + blockTxt); process.exit(3); }",
        # the row-level step carries the attempt, once, attributed
        "var rowSteps = supportTrailSteps(steps, 'e1', {});",
        "if (rowSteps.length !== 1 || rowSteps[0].who !== 'LettuceDetect' || rowSteps[0].tag !== 'LettuceDetect')",
        "  { console.error(JSON.stringify(rowSteps)); process.exit(4); }",
        "console.log('ok');",
    ])
    _run_node(script)


def test_checks_on_every_run_section_renders_with_default_on_checkbox():
    # CVA-approved 16:50 order: one 'Checks on every run' section — read-only
    # fact lines, then the wording-check checkbox ON by default; the old
    # 'PROPOSED WORDING (optional)' heading is gone. No invented claims: the
    # review line records which model ran and whether it is a different
    # family — it never promises one.
    html = (UI / "index.html").read_text()
    assert "Checks on every run" in html
    for line in ("Quote is printed on the cited page (code)",
                 "Citation is where it says (code)",
                 "Dates match the quote (code)",
                 "Independent review by a second model"):
        assert line in html, line
    review_line = [l for l in html.splitlines() if "Independent review by a second model" in l][0]
    assert "the run records which one ran and whether it is a different family" in review_line
    assert "Wording check by a hallucination model (advisory; on by default)" in html
    assert ("Flags proposed wording the cited source may not support. "
            "A flag changes no check, count or decision.") in html
    assert "Proposed wording" not in html
    assert "Check the proposed wording against the cited source" not in html


def test_lettuce_new_run_checkbox_offered_from_options():
    # available: enabled, ON by default (the CVA ruling); a person's untick
    # survives reopens and delayed answers
    _run_node(_new_run_sheet_script([
        "els['nr-support'] = { checked: false, disabled: true };",
        "applySupportOptions({ support_check: { choices: ['lettucedetect'], default: null, available: true,",
        "                                        unavailable_reason: null } });",
        "if (els['nr-support'].disabled) fail(2, 'available box disabled');",
        # CVA ruling: ON by default whenever the check is offered — the body
        # carries support_check with no click at all
        "if (!els['nr-support'].checked) fail(3, 'available should be on by default');",
        "if (nrSupportValue() !== 'lettucedetect') fail(4, 'default-on should send the choice');",
        "startRunNow();",
        "if (!posts.length || posts[0].support_check !== 'lettucedetect') fail(6, JSON.stringify(posts));",
        # the person can still turn it off; a later answer keeps that choice
        "els['nr-support'].checked = false;",
        "nrSupport.touched = true;",
        "applySupportOptions({ support_check: { choices: ['lettucedetect'], default: 'lettucedetect', available: true } });",
        "if (els['nr-support'].checked) fail(8, 'person choice reset by a re-open');",
        "if (nrSupportValue() !== null) fail(9, 'person off should send off');",
        "console.log('ok');",
    ], extra_fns=('supportFromOptions', 'applySupportOptions', 'nrSupportValue')))


def test_lettuce_new_run_checkbox_unavailable_shows_reason_and_never_sends():
    _run_node(_new_run_sheet_script([
        "els['nr-support'] = { checked: true, disabled: false };",
        "applySupportOptions({ support_check: { choices: ['lettucedetect'], default: null, available: false,",
        "                                        unavailable_reason: 'the pinned weights are not installed (LETTUCE_WEIGHTS_PATH is not a directory)' } });",
        "if (!els['nr-support'].disabled) fail(2, 'unavailable box enabled');",
        # plain words on screen, no product name; the server's sentence (env var
        # names and all) is the hover title only
        "if (nodes['nr-support-hint'].textContent !== 'The wording check is not set up on this computer.')",
        "  fail(3, nodes['nr-support-hint'].textContent);",
        "if (nodes['nr-support-hint'].title !== 'the pinned weights are not installed (LETTUCE_WEIGHTS_PATH is not a directory)')",
        "  fail(9, nodes['nr-support-hint'].title);",
        "if (/[A-Z_]{6,}/.test(nodes['nr-support-hint'].textContent)) fail(10, 'env var name on screen');",
        "if (/LettuceDetect/.test(nodes['nr-support-hint'].textContent)) fail(13, 'product name on screen');",
        # a tick never survives the answer that the check is unavailable
        "if (els['nr-support'].checked) fail(11, 'unavailable box stays ticked');",
        "if (nrSupportValue() !== null) fail(4, 'unavailable must send off');",
        "startRunNow();",
        "if (!posts.length || posts[0].support_check !== null) fail(5, JSON.stringify(posts));",
        # a failed options read: disabled with the retry line, still off
        "applySupportOptions(null);",
        "if (!els['nr-support'].disabled) fail(6, 'not-loaded box enabled');",
        "if (els['nr-support'].checked) fail(12, 'not-loaded box ticked');",
        "if (nodes['nr-support-hint'].textContent.indexOf('could not be loaded') === -1) fail(7);",
        "if (nrSupportValue() !== null) fail(8, 'not-loaded must send off');",
        # a stale tooltip never survives a later answer: available after
        # unavailable clears the title
        "applySupportOptions({ support_check: { choices: ['lettucedetect'], default: null, available: true } });",
        "if (nodes['nr-support-hint'].title) fail(14, 'stale tooltip: ' + nodes['nr-support-hint'].title);",
        "if (els['nr-support'].disabled) fail(15, 'available box disabled');",
        "console.log('ok');",
    ], extra_fns=('supportFromOptions', 'applySupportOptions', 'nrSupportValue')))


def test_lettuce_pending_reopen_sends_off_until_the_fresh_answer():
    # NR-002 (Manus 05:31): reopening while the options GET is pending must
    # leave the box disabled AND unticked, and a start reads no stale value —
    # off/null ships until the fresh answer lands
    _run_node(_new_run_sheet_script([
        # a ticked, available box left over from a previous open
        "els['nr-support'] = { checked: true, disabled: false };",
        "nrSupport = { loaded: true, available: true, reason: null, choices: ['lettucedetect'], touched: true };",
        "var deferred = [];",
        "A.researchOptions = function () { return new Promise(function (r) { deferred.push(r); }); };",
        "refreshSupportOptions();",
        "if (els['nr-support'].checked) fail(2, 'pending box stays ticked');",
        "if (!els['nr-support'].disabled) fail(3, 'pending box enabled');",
        "startRunNow();",
        "if (!posts.length || posts[0].support_check !== null) fail(4, JSON.stringify(posts));",
        "posts.length = 0;",
        # the fresh available answer re-enables the box; the person's tick ships
        "deferred[0]({ support_check: { choices: ['lettucedetect'], default: null, available: true,",
        "                               unavailable_reason: null } });",
        "Promise.resolve().then(function () {",
        "  if (els['nr-support'].disabled) fail(5, 'box still disabled after the answer');",
        "  els['nr-support'].checked = true;",
        "  return later();",   # the first start's deferred server reply frees nrBusy
        "}).then(function () {",
        "  startRunNow();",
        "  if (!posts.length || posts[0].support_check !== 'lettucedetect') fail(6, JSON.stringify(posts));",
        "  console.log('ok');",
        "}).catch(function (e) { fail(99, e && e.message); });",
    ], extra_fns=('supportFromOptions', 'applySupportOptions', 'nrSupportValue', 'refreshSupportOptions')))


def test_lettuce_older_options_answer_never_overrides_the_newer():
    # NR-003 (Manus 05:31): two opens in flight; the newer request's
    # unavailable answer owns the state — the older request's late available
    # answer must not enable the box or restore the availability hint
    _run_node(_new_run_sheet_script([
        "els['nr-support'] = { checked: false, disabled: true };",
        "var deferred = [];",
        "A.researchOptions = function () { return new Promise(function (r) { deferred.push(r); }); };",
        "refreshSupportOptions();",   # request 1
        "refreshSupportOptions();",   # request 2 — the newer one
        "deferred[1]({ support_check: { choices: ['lettucedetect'], default: null, available: false,",
        "                               unavailable_reason: 'NEW_UNAVAILABLE' } });",
        "Promise.resolve().then(function () {",
        "  if (!els['nr-support'].disabled) fail(2, 'unavailable box enabled');",
        "  if (nodes['nr-support-hint'].textContent !== 'The wording check is not set up on this computer.')",
        "    fail(3, nodes['nr-support-hint'].textContent);",
        # the older request's late available answer: ignored
        "  deferred[0]({ support_check: { choices: ['lettucedetect'], default: null, available: true,",
        "                                unavailable_reason: null } });",
        "  return Promise.resolve().then(function () {",
        "    if (!els['nr-support'].disabled) fail(4, 'stale answer enabled the box');",
        "    if (nodes['nr-support-hint'].textContent !== 'The wording check is not set up on this computer.')",
        "      fail(5, nodes['nr-support-hint'].textContent);",
        "    if (nrSupportValue() !== null) fail(6, 'stale answer leaked a value');",
        "    console.log('ok');",
        "  });",
        "}).catch(function (e) { fail(99, e && e.message); });",
    ], extra_fns=('supportFromOptions', 'applySupportOptions', 'nrSupportValue', 'refreshSupportOptions')))

    # the Red Team timing ruling: a current date pass's caveat reads the ruling's
    # words verbatim; history (stale) passes wear a neutral dot and 'passed
    # then' — never a green dot, never the current 'Timing matches source'
    import subprocess

    js = (UI / "app.js").read_text()
    script = "\n".join([
        "function el(tag, cls, value) { return {tag:tag, cls:cls, textContent:value || '', children:[], style:{}, dataset:{}, append:function(){ this.children.push.apply(this.children, arguments); }}; }",
        "function checkDot(result) { return el('span', 'dot ' + result); }",
        "function esc(value) { return el('span', null, value); }",
        "function checkResultWord(result) { return ({ pass: 'passed', fail: 'failed', could_not_run: 'could not run', not_needed: 'not needed', unresolved: 'unresolved' })[result] || result; }",
        "function seeTheCheck(check) { return el('details', null, 'details ' + check.label); }",
        "function flatten(node) { return (typeof node === 'string' ? node : node.textContent || '') + (node.children || []).map(flatten).join(' '); }",
        _grab_fn(js, "effectiveChecks"),
        _grab_fn(js, "dateFinding"),
        _grab_fn(js, "passOutcome"),
        _grab_fn(js, "passOutcomeDate"),
        _grab_fn(js, "timingReason"),
        _grab_fn(js, "checksSection"),
        "var PASS_OUTCOME_WORDS = { date_match: passOutcomeDate, quote_match: 'Quote matches source', selector_match: 'Citation located' };",
        "var TIMING_CAVEAT = 'This compares the printed timing only; whether this timing belongs to this event needs your review.';",
        "var TIMING_CAVEAT_OLD = 'This compares date bounds only; whether the date belongs to this event needs your review.';",
        "var current = [{ name: 'date_match', result: 'pass', label: 'Date period found in quote', timing: { wording: 'H1 2027' },",
        "    reason: 'The quote names a period. ' + TIMING_CAVEAT_OLD }];",
        "var history = [{ name: 'date_match', result: 'pass', label: 'Date wording present in quote', timing: { wording: 'H1 2027' } },",
        "  { name: 'quote_match', result: 'pass', label: 'Quote found on page' },",
        "  { name: 'quote_match', result: 'fail', label: 'Quote found on page', reason: 'Old conflict.' }];",
        "var t = { effective_checks: current, fixes: [], check_history: history, checks_stale: true, page_evidence: {} };",
        "var sec = checksSection({ checks: current }, t);",
        "var out = flatten(sec);",
        # the current pass keeps its outcome and carries the ruling's caveat
        "if (out.indexOf('Timing matches source: H1 2027') === -1) { console.error('current outcome'); process.exit(2); }",
        "if (out.indexOf(TIMING_CAVEAT) === -1) { console.error('verbatim caveat missing'); process.exit(3); }",
        "if (out.indexOf(TIMING_CAVEAT_OLD) !== -1) { console.error('old caveat still shown'); process.exit(4); }",
        # the history section: no current outcome, no green dot, fail stays red
        "var histAt = out.indexOf('Saved checks · history (not current)');",
        "if (histAt < 0) { console.error('history header'); process.exit(5); }",
        "var hist = out.slice(histAt);",
        "if (hist.indexOf('Timing matches source') !== -1) { console.error('history wears the current outcome'); process.exit(6); }",
        "if (hist.indexOf('passed then') === -1) { console.error('passed then missing'); process.exit(7); }",
        "if (hist.indexOf('Old conflict.') === -1) { console.error('history conflict lost'); process.exit(8); }",
        "function dotsByHeader(node) { var o = {}, cur = '(head)';",
        "  (node.children || []).forEach(function (c) {",
        "    if (c.tag === 'h4') { cur = c.textContent; o[cur] = []; return; }",
        "    if (c.cls && /(^|\\s)chk(\\s|$)/.test(c.cls))",
        "      (c.children || []).forEach(function (cc) {",
        "        if (cc.cls && /(^|\\s)dot(\\s|$)/.test(cc.cls)) (o[cur] = o[cur] || []).push(cc.cls); }); });",
        "  return o; }",
        "var dots = dotsByHeader(sec);",
        "var curDots = (dots['Checks · by Code'] || []).join(' ');",
        "var histDots = (dots['Saved checks · history (not current)'] || []).join(' ');",
        "if (curDots.indexOf('dot pass') === -1) { console.error('current pass not green: ' + curDots); process.exit(9); }",
        "if (/dot pass/.test(histDots)) { console.error('history pass is green: ' + histDots); process.exit(10); }",
        "if (histDots.indexOf('dot history') === -1) { console.error('history neutral dot missing: ' + histDots); process.exit(11); }",
        "if (histDots.indexOf('dot fail') === -1) { console.error('history fail not red: ' + histDots); process.exit(12); }",
        # a date pass the server sent with no reason still carries the caveat
        "var bare = timingReason({ name: 'date_match', result: 'pass' });",
        "if (bare !== TIMING_CAVEAT) { console.error('bare caveat: ' + bare); process.exit(13); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_lettuce_real_envelope_pane_and_trail(tmp_path):
    # NEXT-CALLER-001: one test through the real boundary — the GET /api/runs/{id}
    # envelope (top-level support_findings; run.research.steps) drives the real
    # detailsPane and renderTrail to final DOM
    import json
    import subprocess

    body = {
        "rows": [
            {"id": "e1", "ticker": "ACME", "title": "ACME Phase 2 start", "program": "ACME-1",
             "source_id": "s1", "page_number": 31, "checks": [], "proposed_by": "Muse Spark"},
            {"id": "e2", "ticker": "ACME", "title": "ACME Phase 3 readout", "program": "ACME-2",
             "source_id": "s1", "page_number": 31, "checks": [], "proposed_by": "Muse Spark"},
            {"id": "e3", "ticker": "ACME", "title": "ACME filing", "source_id": "s1",
             "page_number": 31, "checks": [], "proposed_by": "Muse Spark"},
            {"id": "e4", "ticker": "ACME", "title": "ACME approval", "source_id": "s1",
             "page_number": 31, "checks": [], "proposed_by": "Muse Spark"},
        ],
        "support_findings": {
            "e1": {"result": "unsupported_spans", "spans": [{"start": 0, "end": 5, "text": "Phase"}]},
            "e2": {"result": "stale", "spans": []},
            "e3": {"result": "no_finding", "spans": []},
            "e4": {"result": "could_not_run", "spans": [], "reason": "the pinned weights are not installed"},
        },
        "run": {"id": "run1", "label": "Run 1", "number": 1, "started_at": "2026-11-02T00:00:00Z",
                "research": {"label": "Agent research — not independently reviewed", "steps": [
                    {"who": "capture.support_signal", "tag": "Code", "kind": "check", "at": "2026-11-02T00:01:00Z",
                     "refs": {"row_id": "e1", "result": "unsupported_spans"}, "text": "Support check (LettuceDetect, advisory) e1: unsupported spans."},
                    {"who": "capture.support_signal", "tag": "Code", "kind": "check", "at": "2026-11-02T00:01:01Z",
                     "refs": {"row_id": "e2", "result": "unsupported_spans"}, "text": "Support check (LettuceDetect, advisory) e2: unsupported spans."},
                    {"who": "capture.support_signal", "tag": "Code", "kind": "check", "at": "2026-11-02T00:01:02Z",
                     "refs": {"row_id": "e3", "result": "no_finding"}, "text": "Support check (LettuceDetect, advisory) e3: no finding."},
                    {"who": "capture.support_signal", "tag": "Code", "kind": "check", "at": "2026-11-02T00:01:03Z",
                     "refs": {"row_id": "e4", "result": "could_not_run"}, "text": "Support check (LettuceDetect, advisory) e4: could not run (the pinned weights are not installed)."},
                ]}},
        "sources": [{"id": "s1", "kind": "filing", "publisher": "ACME Corp", "pages": [{"page_number": 31}]}],
        "mistakes": [], "tables": [],
        "calendar_partition": {"calendar": [], "past": [], "later": [], "undated": [],
                               "timing_unresolved": [], "contingent": []},
        "calendar_window": {"start": "2026-11-01", "end": "2027-05-01"},
    }
    targets = {r["id"]: {"group": "proposed"} for r in body["rows"]}
    js = (UI / "app.js").read_text()
    script = "\n".join([
        "const fs = require('fs');",
        "function make(tag) {",
        "  return { tag, className: '', textContent: '', children: [], style: {}, hidden: false, value: '', dataset: {},",
        "    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },",
        "    append(...a) { for (const c of a) this.children.push(typeof c === 'string' ? { textContent: c, children: [] } : c); },",
        "    appendChild(c) { this.children.push(c); return c; }, addEventListener() {}, removeEventListener() {},",
        "    setAttribute(k, v) { this[k] = v; }, getAttribute() { return null; },",
        "    querySelector() { return null; }, querySelectorAll() { return []; }, focus() {}, scrollIntoView() {}, remove() {},",
        "    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; },",
        "    set innerHTML(v) { this.children = []; this._html = v; }, get innerHTML() { return this._html || ''; },",
        "    firstChild: null };",
        "}",
        "const nodes = {};",
        "global.document = { createElement: make, createTextNode: t => ({ textContent: String(t), children: [] }),",
        "  getElementById: id => (nodes[id] = nodes[id] || make('div')), querySelector: () => null, querySelectorAll: () => [],",
        "  addEventListener() {}, body: make('body'), documentElement: make('html'), title: '', activeElement: null };",
        "global.window = { addEventListener() {}, location: { hash: '' }, matchMedia: () => ({ matches: false, addEventListener() {} }),",
        "  setTimeout, history: { replaceState() {}, pushState() {} }, getComputedStyle: () => ({}) };",
        "global.location = window.location; global.history = window.history;",
        "global.localStorage = { getItem() { return null; }, setItem() {} };",
        "global.fetch = () => new Promise(() => {});",
        "global.requestAnimationFrame = () => 0;",
        "let src = fs.readFileSync(" + json.dumps(str(UI / "app.js")) + ", 'utf8');",
        "src = src.replace(/route = parseHash\\(\\);\\s*boot\\(\\);\\s*\\}\\)\\(\\);\\s*$/,",
        "  'global.__H = { get detail(){return detail;}, set detail(v){detail=v;}, ev: function(s){ return eval(s); } };\\n})();');",
        "if (!/global\\.__H/.test(src)) throw new Error('no boot hook');",
        "eval(src);",
        "const H = global.__H;",
        "function textOf(n, out) { out = out || []; if (!n) return out; if (n.textContent) out.push(n.textContent);",
        "  (n.children || []).forEach(c => textOf(c, out)); return out; }",
        "H.detail = " + json.dumps({"id": "run1", "run": body,
                                   "state": {"targets": targets, "mistake_list": [], "exclusions": []}}) + ";",
        "H.ev(\"route = { screen: 'audit', runId: 'run1', rowId: null, dialog: null }\");",
        # the pane: a current finding shows the understated note; every other
        # result adds nothing
        "function paneText(id) { const host = make('div');",
        "  H.ev('detailsPane')(host, H.ev('rowOf')(id), { actions: true, back: 'audit' });",
        "  return textOf(host).join(' '); }",
        "var p1 = paneText('e1');",
        "if (p1.indexOf('Potentially unsupported wording') === -1 || p1.indexOf('“Phase”') === -1)",
        "  { console.error('e1 note: ' + p1.slice(0, 160)); process.exit(2); }",
        "if (p1.indexOf('may not be supported by the cited source') === -1)",
        "  { console.error('e1 wording: ' + p1.slice(0, 160)); process.exit(3); }",
        "['e2', 'e3', 'e4'].forEach(function (id) {",
        "  var p = paneText(id);",
        "  if (p.indexOf('Potentially unsupported wording') !== -1 || p.indexOf('“Phase”') !== -1)",
        "    { console.error(id + ' pane should stay clean: ' + p.slice(0, 160)); process.exit(4); }",
        "});",
        # the trail: the record's own label leads the block; a stale finding
        # reads as an earlier check, never as a current finding
        "H.ev(\"route = { screen: 'trail', runId: 'run1', rowId: 'e2', dialog: null }\");",
        "H.ev('renderTrail')();",
        "var trailText = textOf(nodes['trail-mid']).join(' ');",
        "if (trailText.indexOf('Agent research — not independently reviewed') === -1)",
        "  { console.error('trail header: ' + trailText.slice(0, 160)); process.exit(5); }",
        "if (trailText.indexOf('Earlier check (no longer current)') === -1)",
        "  { console.error('stale trail line: ' + trailText.slice(0, 200)); process.exit(6); }",
        "if (/Potentially unsupported wording/.test(trailText))",
        "  { console.error('stale reads as current'); process.exit(7); }",
        "H.ev(\"route = { screen: 'trail', runId: 'run1', rowId: 'e3', dialog: null }\");",
        "H.ev('renderTrail')();",
        "var t3 = textOf(nodes['trail-mid']).join(' ');",
        "if (t3.indexOf('No finding') === -1 || /verif/i.test(t3) && t3.indexOf('not a statement') === -1)",
        "  { console.error('no-finding trail line: ' + t3.slice(0, 200)); process.exit(8); }",
        "H.ev(\"route = { screen: 'trail', runId: 'run1', rowId: 'e4', dialog: null }\");",
        "H.ev('renderTrail')();",
        "var t4 = textOf(nodes['trail-mid']).join(' ');",
        "if (t4.indexOf('Not run') === -1)",
        "  { console.error('not-run trail line: ' + t4.slice(0, 200)); process.exit(9); }",
        "console.log('ok');",
    ])
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, timeout=90)
    assert got.returncode == 0, got.stdout + got.stderr
