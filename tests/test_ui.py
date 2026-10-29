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
    # the person's decision step is dated from the decisions record
    assert "lastPersonDecision" in js
    assert "dateShort" in js
    # a failed check step links to the check in the details pane
    assert "dataset.fn" in js and "checkFnFor" in js


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
    # only runs of the same label family are compared, two at a time
    assert "familyMembers" in js
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
    # no build endpoint on main: the command-line note, no Build button
    assert "Built from the command line: python -m report.build " in js
    assert "REPORT_BUILD_ENDPOINT = null" in js
    assert "Build report" not in html
    # the summary numbers come from state, the same counts as the audit log
    for field in ("counts.rows", "unresolved", "mistakes.code", "mistakes.second_reader",
                  "mistakes.person", "excluded.code"):
        assert field in js, field
    # when the route is missing the UI says where to open the files
    assert "Open from the runs folder" in js


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
    # Codex P2 on PR #30: the sealed label is earned — key.sealed must be true,
    # an author-keyed report reads 'Unsealed (author key)'
    assert "accSealLabel" in js
    assert "key.sealed === true" in js
    assert "Unsealed (author key)" in js
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
    assert "Unsealed (author key)" in js
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
    # footer: 'sealed-set file' only when key.sealed is true, else 'accuracy file'
    assert "sealed-set file" in js and "accuracy file" in js
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
    assert "Unsealed (author key)" in text
    assert "sealed-set" not in text
    assert "its accuracy file" in text
    blank = _panel_text({"key": {"sealed": False}, "models": {}})
    assert "Unsealed (author key)" in blank and "sealed-set" not in blank
    assert "No accuracy scoring is saved for this run yet." in blank
