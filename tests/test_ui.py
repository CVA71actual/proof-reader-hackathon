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
