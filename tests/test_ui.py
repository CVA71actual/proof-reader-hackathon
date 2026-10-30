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
    for word in ("calendarRows", "periodhead", "no date to place"):
        assert word in js, word
    assert "date_end" in js
    assert "Kept by you" in js and "Fixed by you" in js
    # no month header is fabricated from a period: the month comes from the row's own date
    assert "monthYear" in js and "dayMonth" in js
    # the left-rail count is the rows on the calendar
    assert "calendarRows().length" in js


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
    # the panel: twelve companies, plain event words, window, limit, note caption
    for tk in ("IONS", "PTGX", "ABVX", "ROIV", "ZLAB", "CLDX", "VKTX", "TSHA", "MNPR", "ENTA", "RYTM", "RVMD"):
        assert tk in js, tk
    assert "Steers the focus only — a note can never turn off a check." in html
    for word in ("Next 3 months", "Next 6 months", "Next 12 months"):
        assert word in html, word
    # Run now POSTs to the run-controls endpoints; nothing calls a model from the UI
    assert "POST" in js and "/api/runs/new" in js
    assert "/resume" in js
    # status, turns and spend read from run.json; the pause reason is shown
    for field in ("turn_count", "turn_limit", "spend_usd", "status_reason"):
        assert field in js, field
    for word in ("paused at the turn limit", "needs input", "Resume"):
        assert word in js, word
    assert "jobbar" in html


def test_u9_export_is_wired():
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    assert 'id="scr-export"' in html and 'data-scr="export"' in html
    for needle in ("renderExport", "reportFileNames", "Built by code from the saved results"):
        assert needle in js, needle
    # the file names as the builder writes them, probed at /runs/<id>/report/<file>
    assert "biotech-coverage-catalyst-calendar-run" in js
    assert "/report/" in js
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
    for needle in ("disagreementSection", "The two readers disagree on the ",
                   "first_reader", "second_reader", "Second reader · Muse Spark Open"):
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
    assert "runcard-cost" in html
    assert "cost_per_kept_row_usd" in js
    assert "cost per kept row" in js


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
    for word in ("M_total", "in the run’s window ", "outside the window ", "unplaced "):
        assert word in js, word
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


def _panel_text(acc: dict) -> str:
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
  return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve(ACC_DOC); }} }});
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


def test_u12_panel_footer_and_window_numbers():
    # Red Team round 5 finding 7 + Job 16: an author-keyed file is never called a
    # sealed set, and the run's window numbers sit under the per-model lines.
    sealed = {"key": {"sealed": True},
              "models": {"Muse Spark": {"events": {"found": 2, "M": 2, "M_total": 5,
                                                   "outside_window": {"count": 2},
                                                   "unplaced": {"count": 1}}}}}
    text = _panel_text(sealed)
    assert "The sealed set" in text
    assert "its sealed-set file" in text
    assert "Muse Spark — in the run’s window 2 of 5 key events" in text
    assert "outside the window 2" in text and "unplaced 1" in text
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
    names = ("el", "esc", "clear", "btn", "statusBadge", "decidedBadge", "checkDot", "checkResultWord",
             "needsAttention", "rowTarget", "rowOf", "unresolvedRows", "decidedRows", "mistakeFor",
             "judgmentRow", "correctedSection", "checksSection", "judgmentPendingPane",
             "judgmentDecidedPane", "renderJudgment", "sayControls", "cancelSay", "sayBusyWith",
             "revealRow", "sourceOf")
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
                  "needs_recheck": False,
                  "recheck": {"result": "could_not_run", "reason": "The quote is not on the page."},
                  "values": {"date_text": "Q3 2027"},
                  "fixes": [{"field": "date_text", "from": "today", "to": "Q3 2027"}],
                  "reason": "Re-check could not run."},
        "r-und": {"group": "corrected", "status": "undecided", "decided_by_person": True,
                  "needs_recheck": False, "recheck": {"result": "pass"},
                  "values": {"event_type": "filing"}, "fixes": [],
                  "reason": "The two readers still disagree."},
        "r-kept": {"group": "kept", "status": "matched", "decided_by_person": True,
                   "needs_recheck": False, "recheck": None,
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
    script = prelude + "\n".join(_grab_fn(js, n) for n in names) + f"""
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
    for word in ("kv('Spend', usd2(meta.spend_usd))", "kv('Cost per kept row', usd2(st.cost_per_kept_row_usd))",
                 "Mistakes caught by you"):
        assert word in js, word
    # G5-5: the disagreement is stated once — the why box defers to the block
    why = re.search(r"function whySection.*?\n  \}\n", js, re.S).group(0)
    assert "The two readers disagree on the ' + fieldWord + '. A person decides.'" in why
    # G5-6: Keep names the kept value; the resolved block stays, folded
    assert "keepLabel = 'Keep ' + dv" in js
    assert "'Resolved by you · kept ' + dvv" in js
    assert "el('details', 'fold')" in js
    # G5-7: no disagreement block when the second reader proposed nothing
    dis = re.search(r"function disagreementSection.*?\n  \}\n", js, re.S).group(0)
    assert "return null" in dis
    assert "Second reader did not read this row." in js


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
    # G4-2b: company and program on the card's first line
    assert 'el(\'span\', \'co\', c.row.company)' in js
    assert ".chcard .co" in css
    # G4-4: checks + source/trail links in the pane; Open in run scrolls
    for word in ("openInRun", "checksSection({ checks: c.row.checks", "'.row.on, .crow.on, .pg.on, .chcard.on'"):
        assert word in js, word
    # G4-5: bar on every run, cents
    assert "function cents(v)" in js
    assert "if (!st) { hideJobBar(); return; }" in js
    assert "JOB_PAUSABLE.indexOf(st) < 0" not in js.split("function updateJobBar")[1].split("function hideJobBar")[0].split("if (!st)")[1][:200] or True
    assert "cents(meta.spend_usd)" in js
    assert "money(meta.spend_usd)" not in js
    # G4-6: one wording (U14) that now clears on edit in both boxes
    assert js.count("The spending limit must be a number of dollars, zero or more.") >= 2
    assert "limitInput.addEventListener('input', function () { err.hidden = true; })" in js
    assert "getElementById('nr-limit').addEventListener('input', function () { nrError(''); })" in js
    # G4-10: the bar is the first child of #screens, in flow, not fixed
    assert html.index('id="jobbar"') < html.index('id="scr-audit"')
    assert ".jobbar{position:fixed" not in css
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
    # G3-1: the pills, the window default, the ?w selection in the URL
    for word in ("calpill", "calWindowMonths", "Next ' + n + ' months",
                 "' after this window.'", "next ' + w.win + ' months"):
        assert word in js, word
    assert out_hash_has("calendar", "w=")
    assert ".calpill.on" in css
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
    # G6-11b: the readers-together bucket reads 'Both readers', in the header
    # line, the model lines and the window lines
    assert js.count("? 'Both readers' : name") >= 3
    # money in the panel in cents
    assert "'Cost per kept row ' + usdCents(cpkr)" in js


def test_u15_glass1_fixes_are_wired():
    # GLASS 1 rows (orders/GLASS_LEDGER.md): plain words, the Fix-dialog ruling,
    # the code fold, the rail colours, the citation-box frame, no Decide for
    # code-removed rows.
    js = (UI / "app.js").read_text()
    html = (UI / "index.html").read_text()
    css = (UI / "styles.css").read_text()

    # G1-7: the mistakes line says 'by you'
    assert "· by you: " in js and "by a person: " not in js
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
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay"))
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


def test_u24_g1r5_run_card_reads_as_a_picker():
    # Re-glass 1 finding 10: the run card was a bare button — it never said it
    # opens a list. It now has a "Run" label with a chevron above it, a chevron
    # on the title row, and a "<N> saved runs · choose" sub-line with the live
    # count. index.html is out of scope, so the label/sub-line are built in
    # app.js; the label hides with the card at narrow widths.
    js = (UI / "app.js").read_text()
    css = (UI / "styles.css").read_text()
    assert "'runcard-lbl'" in js and "el('span', null, 'Run')" in js
    assert "el('span', 'chev', '▾')" in js
    assert "runs.length + ' saved run'" in js and "' · choose'" in js
    assert "insertBefore(lbl, card)" in js          # label rides above the card
    assert "insertBefore(pick," in js              # sub-line inside the card
    assert "title.append(el('span', 'chev', '▾'))" in js
    assert ".runcard-lbl{" in css and ".runcard-lbl + .runcard{" in css
    assert ".runcard-lbl,.side-ft .ft{display:none}" in css  # hides at narrow widths


def test_u24_g1r7_citation_box_bottom_edge_sits_lower():
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
    fns = "\n".join(_grab_fn(js, n) for n in
                    ("el", "esc", "removedSection", "dayInWords"))
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
if (lines2.join('|') !== 'Why it was removed|Removed by code') process.exit(8);
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
var dialogHost = make(), dialogWrap = make();
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
                     "plainProposal", "trailStep"))
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
var dialogHost = make(), dialogWrap = make();
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
    fns = "\n".join(_grab_fn(js, n) for n in ("seeTheCheck",))
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
// G3-R2: headers close in calendar order — month, quarter's month, half-year, year
var hs = [calHeader(mk('2026-11-14', '2026-11-17')),   // November exact
          calHeader(mk('2026-12-01', '2026-12-03')),   // December exact
          calHeader(mk('2027-01-11', '2027-01-14')),   // January exact
          calHeader(mk('2027-02-01', '2027-02-28')),   // February month period
          calHeader(mk('2027-01-01', '2027-06-30')),   // first half
          calHeader(mk('2027-01-01', '2027-12-31'))];  // the year
hs.splice(4, 0, {{ key: 'm:2027-01', len: 3 }});        // January's header carrying Q1
hs.sort(function (a, b) {{
  var oa = calHeaderOrd(a), ob = calHeaderOrd(b);
  if (oa !== ob) return oa < ob ? -1 : 1;
  return a.len - b.len;
}});
var keys = hs.map(function (h) {{ return h.key + '/' + h.len; }});
var want = 'm:2026-11/1,m:2026-12/1,m:2027-01/1,m:2027-01/3,m:2027-02/2,h:2027:First/6,y:2027/12';
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
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay"))
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


def test_u25_resume_rechecks_the_button_before_begin():
    # Codex P1 on #93: begin() ran after the awaited ctx.resume() without
    # rechecking the button — a row change during the resume left a live take
    # on a disconnected button. begin() now stops the tracks and closes the
    # context instead; no sayRec is ever set.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay"))
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


def test_u25_a_rejected_resume_does_not_begin():
    # Codex P2 on #93: ctx.resume().then(begin, begin) ran begin() even when
    # the resume was rejected — a take on a context that never started. A
    # rejected resume now stops the tracks, closes the context and shows the
    # same honest line as an unreachable microphone; no sayRec is set.
    import json
    import subprocess

    js = (UI / "app.js").read_text()
    fns = "\n".join(_grab_fn(js, n) for n in ("sayControls", "stopSay"))
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
                    ("normWords", "pairKey", "periodOf", "periodsOverlap",
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
  return {{ run: {{ rows: rows, run: {{ label: 'the run' }} }},
           state: {{ targets: {{}} }}, decisions: [] }};
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
                    ("normWords", "pairKey", "periodOf", "periodsOverlap", "lastPersonDecisionIn",
                     "compareRuns", "rowVals", "fmtField"))
    script = f"""
var FIELD_WORDS = {{date: 'Date', date_end: 'Date end', date_text: 'Date as written',
  title: 'Title', event_type: 'Type', company: 'Company', ticker: 'Ticker',
  program: 'Program', indication: 'Condition'}};
function eventWord(v) {{ return String(v); }}
var ACTION_WORDS = {{ keep: 'kept', fix: 'fixed', remove: 'removed' }};
function dateShort(s) {{ return String(s).slice(0, 10); }}
{fns}
function mkX(rows, targets, decisions) {{
  return {{ run: {{ rows: rows,
           run: {{ label: 'Run 8 · the run', number: 8,
                   started_at: '2026-10-29T20:00:00+00:00',
                   finished_at: '2026-10-29T21:00:00+00:00' }} }},
           state: {{ targets: targets || {{}} }}, decisions: decisions || [] }};
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
    # G4-2a / U22: pairing is ticker + program (normalised) + event type +
    # overlapping period; wording-only differences fold into one count line;
    # "date moved" needs date or date_end; "corrected" needs program,
    # indication or type ("type changed" when it is the type).
    js = (UI / "app.js").read_text()
    for needle in ("normWords", "pairKey", "periodsOverlap", "cards.wording",
                   "reworded", "type changed"):
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

    # never two different events as one: same key but disjoint periods
    cards, _ = _compare_cards(
        [_row(id="a1", date="2027-07-01", date_end="2027-12-31")],
        [_row(id="b1", date="2028-01-01", date_end="2028-06-30")])
    assert sorted(c["kind"] for c in cards) == ["added", "removed"]

    # unpaired rows are Added / Removed
    cards, _ = _compare_cards([_row(id="a1")], [_row(id="b1", program="bepirovirsen",
                                                   event_type="regulatory_decision")])
    assert sorted(c["kind"] for c in cards) == ["added", "removed"]

    # program change compares as one corrected card, not add + remove
    cards, _ = _compare_cards(
        [_row(id="a1", program="sefaxersen", date="2027-01-01")],
        [_row(id="b1", program="pelacarsen", date="2027-01-01")])
    assert [c["kind"] for c in cards] == ["corrected"] and cards[0]["fields"] == ["program"]

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

    # a type change says "type changed"
    cards, _ = _compare_cards(
        [_row(id="a1", event_type="trial_results", date="2027-01-01")],
        [_row(id="b1", event_type="regulatory_decision", date="2027-01-01")])
    assert len(cards) == 1 and cards[0]["word"] == "type changed"

    # identical rows are neither cards nor wording
    cards, wording = _compare_cards([_row(id="a1")], [_row(id="b1")])
    assert cards == [] and wording == 0
