"""Red Team round 5. A failure is a finding: each assertion is the result the code should have given.

Base: product main 4f04012. No model call, no network, no key. The readers are fakes.
Scope: the Job 15 fixes, and the lines ui/app.js draws from accuracy.json.
A hyphen or a slash still counts as the edge of an event word. The type check still counts a digit
or an underscore as that edge. A box hint lands on the copy whose bounding box covers the hint,
not on the copy whose words are next to it. A Wonder reply whose tokens are not integers costs
nothing, so the reading replaces the row. A send-back with a space added inside parentheses is
taken. The audit line stitches two models into one score. An unsealed file is still called a sealed set.
"""

import json
import subprocess
from pathlib import Path

from capture import read_table
from capture.reader import Reply
from checks.event_checks import date_match, type_match
from checks.selector_match import selector_match
from runs import build_entry
from tests.test_escalation import FakeWonder, job_of, reread_right, run_of, start, wrong_date_events
from tests.test_r12_fixwave import _q1_quote, _reader, _start
from tests.test_run_controls import FakeReader

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "ui" / "app.js"
MARKED_Q1 = "Q1 \u201927"  # the year-less date the first reader writes; the send-back must not replace it


def _no_keys(monkeypatch, tmp_path):
    """Point the key file away and drop key names before any reader can load them. Values are never read."""
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    for name in ("MODEL_API_KEY", "BASETEN_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _words(text, x, y, prefix):
    out = []
    for i, token in enumerate(text.split()):
        out.append({"id": f"{prefix}{i}", "text": token, "x0": x, "y0": y, "x1": x + 36.0, "y1": y + 10.0})
        x += 40.0
    return out


def _grab(name: str) -> str:
    """The function as it is written in ui/app.js. The test does not keep a copy of the logic."""
    js = APP_JS.read_text()
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


def _node(script: str) -> str:
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True, check=True)
    return got.stdout


# ---------------------------------------------------------------- whole words


def test_date_wording_does_not_classify_hyphen_or_slash_words():
    """DATE-LITERAL-001: a matching date cannot classify data-room or data/set."""
    from tests.pivot_assertions import assert_proposal_held
    room = date_match("Q4 2027", "2027-10-01", "The data-room opens in Q4 2027.", "2027-12-31",
                      event_type="trial_results")
    slash = date_match("Q4 2027", "2027-10-01", "The data/set arrives in Q4 2027.", "2027-12-31",
                       event_type="trial_results")
    assert (room.result, slash.result) == ("pass", "pass")

    for quote in ("The data-room opens in Q4 2027.", "The data/set arrives in Q4 2027."):
        assert_proposal_held(quote, "trial_results", date_text="Q4 2027",
                             start="2027-10-01", end="2027-12-31")


def test_the_type_check_uses_the_same_whole_word_boundary():
    """The date check refuses data2, data_code and data-room. The type check still reads each of them as the
    word 'data' and passes. Gap 8: a fixed word is a whole word in every check that uses it."""
    quotes = (
        "The company expects data2 in Q4 2027.",
        "The company expects data_code in Q4 2027.",
        "The data-room opens in Q4 2027.",
    )
    assert [type_match("trial_results", q).result for q in quotes] == ["not_needed", "not_needed", "not_needed"]


# ---------------------------------------------------------------- the box


def test_a_box_hint_locates_the_copy_whose_words_are_nearest():
    """Two copies of one sentence. One is a single line. The other is the same words at the top of the page
    and the bottom, so the box around it covers the middle, where none of its words are. The hint sits on the
    single line and overlaps neither copy. The tall box wins, because the gap is measured to that box and not
    to the words. Gap 1: the located box is the copy whose words are nearest the hint."""
    quote = "Phase 3 starts in Q3 2027"
    tight = _words(quote, 20.0, 400.0, "t")
    scattered = _words("Phase 3 starts", 20.0, 20.0, "a") + _words("in Q3 2027", 20.0, 700.0, "b")
    words = tight + scattered
    hint = [20.0, 378.0, 250.0, 396.0]
    page = (max(w["x1"] for w in words), max(w["y1"] for w in words))
    assert selector_match(quote, hint, words, page[0], page[1], quote_lines=True).result != "pass"
    nearer = [min(w["x0"] for w in tight), min(w["y0"] for w in tight),
              max(w["x1"] for w in tight), max(w["y1"] for w in tight)]
    assert build_entry.locate_box(quote, hint, words) == nearer


# ---------------------------------------------------------------- Wonder, send-back


class _CountedWonder(FakeWonder):
    """Same as FakeWonder, but the token counts are passed through unchanged (they need not be ints)."""

    def call(self, prompt, image=None):
        self.prompts.append(prompt)
        self.images.append(image)
        events = self.answer(prompt, image, len(self.prompts)) if self.answer else []
        return Reply(json.dumps({"events": events}), self.model,
                     prompt_tokens=self.tokens[0], completion_tokens=self.tokens[1])


def test_a_wonder_reply_whose_tokens_are_not_integers_does_not_replace_the_row(tmp_path, words_doc, monkeypatch):
    """127,500 prompt tokens at Muse Wonder's input price is $0.51, a cent past a quarter of a $2 limit.
    Those counts as integers are refused and the row stays. The same counts as floats are not integers, so
    the cost is nothing, the share is not past, and the reading replaces the row. Gap 10: a fix whose cost
    cannot be shown to sit inside the share is not a corrected row."""
    _no_keys(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    first = FakeReader(cost=0.01, answer=lambda prompt, n: json.dumps({"events": wrong_date_events()}))
    wonder = _CountedWonder(tokens=(127_500.0, 0.0), answer=reread_right(words_doc))
    _, rid = start(runs, first, None, wonder, spending_limit_usd=2)
    run, job = run_of(runs, rid), job_of(runs, rid)
    row = next(r for r in run["rows"] if r.get("date_text") == "Q1 2027")
    assert len(wonder.prompts) == 1
    assert row["status"] == "failed" and row["group"] != "corrected"
    assert "replaced" not in job.get("escalated_rows", {}).values()
    assert not [m for m in run["mistakes"] if m["target"] == row["id"] and m["caught_by"] == "code"]


def test_a_send_back_cannot_add_a_space_inside_the_printed_parentheses(tmp_path, monkeypatch):
    """The answer is the printed sentence plus a space after the opening parenthesis. The compare-form drops a
    space just inside parentheses, so the sentence is taken, the quote on the row is the altered one, and the
    row becomes matched. Gap 1 and gap 10: the quote on the row is the printed sentence, and a send-back that
    is not that sentence does not clear the date."""
    _no_keys(monkeypatch, tmp_path)
    calls = []
    printed = _q1_quote()
    altered = printed.replace("(oral", "( oral", 1)
    run, _ = _start(tmp_path, _reader(lambda i: {"id": i, "quote": altered, "date_text": "Q1 2027"}, calls))
    assert calls == ["read", "send-back"]
    row = next(r for r in run["rows"] if "PN-8047" in (r.get("quote") or ""))
    assert (row["quote"], row["date_text"], row["status"]) == (printed, MARKED_Q1, "failed")


# ---------------------------------------------------------------- accuracy.json as the screen reads it


def test_the_audit_line_does_not_mix_two_models_into_one_score():
    """The first model has only values. The second has only events and planted. The audit line adds them into
    one unlabeled score. Gap 2: a shown rate is one model's counts, not a mix of two models."""
    acc = {
        "key": {"sealed": False},
        "models": {
            "Muse Spark": {"values": {"found": 1, "N": 100}},
            "Muse Wonder": {"events": {"found": 2, "M": 2}, "planted": {"caught": 12, "total": 12}},
        },
    }
    script = "\n".join((
        _grab("accSealLabel"),
        _grab("accuracyLineFrom"),
        f"const line = accuracyLineFrom({json.dumps(acc)});",
        "if (line && line.includes('values 1/100') && line.includes('events 2/2')) process.exit(2);",
        "console.log(line === null || line === undefined ? '' : line);",
    ))
    got = subprocess.run(["node", "-"], input=script, text=True, capture_output=True)
    assert got.returncode == 0, got.stdout or got.stderr


def test_an_unsealed_file_is_not_called_a_sealed_set():
    """The heading says the key is unsealed. The empty state still says no sealed-set scoring is saved, and the
    footer still says every number comes from the sealed-set file. Gap 2: an author key is not a sealed set,
    and the panel does not call it one."""
    filled = {
        "key": {"sealed": False},
        "models": {"Muse Spark": {"values": {"found": 1, "N": 100}, "events": {"found": 1, "M": 1}}},
        "planted": {"caught": 1, "total": 2},
    }
    blank = {"key": {"sealed": False}, "models": {}}
    fns = "\n".join(_grab(name) for name in (
        "accPart", "accModelLines", "accPlantedLine", "accuracyProbe", "renderAccuracyPanel"))
    script = f"""
const docs = {json.dumps({"filled": filled, "blank": blank})};
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
var ACC_DOC = null;
global.fetch = function () {{
  return Promise.resolve({{ ok: true, json: function () {{ return Promise.resolve(ACC_DOC); }} }});
}};
var accCache = {{ id: null, acc: null, tried: false }};
var accPanelHost = el('div');
var detail = {{ id: 'r', run: {{ run: {{ label: 'probe' }}, rows: [] }},
  state: {{ cost_per_kept_row_usd: null, mistake_list: [] }} }};
{fns}
function renderOne(id, acc) {{
  ACC_DOC = acc;
  detail = {{ id: id, run: {{ run: {{ label: 'probe' }}, rows: [] }},
    state: {{ cost_per_kept_row_usd: null, mistake_list: [] }} }};
  accPanelHost = el('div');
  accCache = {{ id: null, acc: null, tried: false }};
  renderAccuracyPanel();
  return new Promise(function (resolve) {{ setTimeout(function () {{ resolve(textOf(accPanelHost)); }}, 0); }});
}}
renderOne('filled', docs.filled).then(function (text) {{
  console.log('FILLED');
  console.log(text);
  return renderOne('blank', docs.blank);
}}).then(function (text) {{
  console.log('BLANK');
  console.log(text);
}}).catch(function (err) {{ console.error(err); process.exit(1); }});
"""
    out = _node(script)
    filled_text, blank_text = out.split("BLANK", 1)
    assert "Muse Spark — values 1/100" in filled_text
    # G1-8 (orders/GLASS_LEDGER.md) renamed the heading to the plain words
    # 'Our own key, not sealed' — same intent: an author key is not called a sealed set
    assert "Our own key, not sealed" in filled_text and "sealed-set" not in filled_text
    assert "Our own key, not sealed" in blank_text and "sealed-set" not in blank_text
