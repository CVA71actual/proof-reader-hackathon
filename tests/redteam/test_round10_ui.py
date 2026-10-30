"""Red Team round 10, mic half. A failure is a finding: each assertion is the result the screen should have given.

Product main 44f25f874f2505f191cceb373a8411b0ea87cfb6. The mic is on Judgment needed (ui/app.js).
Tests only. Node runs the functions the way tests/test_ui.py does. No model call, no network, no key.
"""

import re
import subprocess

from conftest import REPO

JS = (REPO / "ui" / "app.js").read_text()


def _grab_fn(name: str) -> str:
    at = JS.index(f"function {name}")
    i = JS.index("{", at)
    depth = 0
    for j in range(i, len(JS)):
        if JS[j] == "{":
            depth += 1
        elif JS[j] == "}":
            depth -= 1
            if depth == 0:
                return JS[at : j + 1]
    raise AssertionError(f"{name} does not close")


def _field_words() -> str:
    got = re.search(r"var FIELD_WORDS = \{.*?\};", JS, re.S)
    assert got, "FIELD_WORDS missing"
    return got.group(0)


def _node(script: str) -> None:
    got = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=20)
    assert got.returncode == 0, got.stdout + got.stderr


_DOM = r"""
function make() {
  return { tag: 'div', className: '', textContent: '', children: [], style: {}, hidden: false,
    isConnected: true, value: '', _l: {},
    append: function () { for (var i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    addEventListener: function (t, fn) { this._l[t] = fn; },
    querySelector: function () { return null; } };
}
function el(tag, cls, text) {
  var n = make(); n.tag = tag; if (cls) n.className = cls; if (text) n.textContent = text; return n;
}
function btn(cls, text) { return el('button', cls, text); }
function clear(node) { node.children = []; }
"""


def test_a_live_take_does_not_stop_the_microphone():
    """The press asks for the microphone. The tracks must still be live when the take starts.
    A late answer (the 10 s timeout, or the button leaving the page) is what stops an unused stream."""
    fns = "\n".join(_grab_fn(n) for n in ("sayControls", "stopSay"))
    _node(_DOM + """
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {};
var sayRec = null;
function encodeWav() { return new Uint8Array(0); }
function currentRunId() { return 'run1'; }
""" + fns + """
var stops = 0;
var stream = { getTracks: function () { return [{ stop: function () { stops++; } }]; } };
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: {
  getUserMedia: function () { return new Promise(function (res) { gumResolve = res; }); } } } });
function FakeAC() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; }
FakeAC.prototype.createMediaStreamSource = function () { return { connect: function () {} }; };
FakeAC.prototype.createScriptProcessor = function () { return { connect: function () {}, onaudioprocess: null }; };
FakeAC.prototype.close = function () {};
global.window = { AudioContext: FakeAC };
var wrap = sayControls({ id: 'r1' });
wrap.children[0]._l.mousedown();
gumResolve(stream);
setTimeout(function () {
  if (!sayRec) { console.error('the take did not start'); process.exit(3); }
  if (stops !== 0) {
    console.error('the microphone was stopped ' + stops + ' time(s) before the take ended');
    process.exit(2);
  }
  process.exit(0);
}, 0);
""")


def test_a_suspended_audio_context_is_resumed_so_the_take_is_recorded():
    """The context is built after the microphone answers, which is outside the press.
    A context created there starts suspended and records nothing until it is resumed.
    One second of audio must be posted."""
    fns = "\n".join(_grab_fn(n) for n in ("sayControls", "stopSay"))
    _node(_DOM + """
var SAY_MAX_SECONDS = 30;
var FIELD_WORDS = {};
var sayRec = null;
var gesture = 0;
function encodeWav() { return new Uint8Array(8); }
function currentRunId() { return 'run1'; }
var sent = 0;
global.fetch = function () { sent++; return Promise.resolve({ ok: true, json: function () {
  return Promise.resolve({ transcript: 'keep it', proposal: { action: 'keep' } }); } }); };
""" + fns + """
var stream = { getTracks: function () { return [{ stop: function () {} }]; } };
var gumResolve = null;
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: {
  getUserMedia: function () { return new Promise(function (res) { gumResolve = res; }); } } } });
function FakeAC() {
  this.sampleRate = 48000; this.destination = {};
  this.state = gesture > 0 ? 'running' : 'suspended';
}
FakeAC.prototype.createMediaStreamSource = function () { return { connect: function () {} }; };
FakeAC.prototype.createScriptProcessor = function () { return { connect: function () {}, onaudioprocess: null }; };
FakeAC.prototype.close = function () {};
FakeAC.prototype.resume = function () { this.state = 'running'; return Promise.resolve(); };
global.window = { AudioContext: FakeAC };
var wrap = sayControls({ id: 'r1' });
var b = wrap.children[0];
var orig = b._l.mousedown;
b._l.mousedown = function () { gesture++; try { return orig.apply(this, arguments); } finally { gesture--; } };
b._l.mousedown();
gumResolve(stream);
setTimeout(function () {
  if (!sayRec) { console.error('the take did not start'); process.exit(3); }
  var state = sayRec.ctx.state;
  if (state === 'running' && sayRec.node.onaudioprocess) {
    var ch = new Float32Array(sayRec.ctx.sampleRate);
    sayRec.node.onaudioprocess({ inputBuffer: { getChannelData: function () { return ch; } } });
  }
  b._l.click();
  setTimeout(function () {
    if (sent !== 1) {
      console.error('a one-second take was not posted (context ' + state + ', sent ' + sent + ')');
      process.exit(2);
    }
    process.exit(0);
  }, 0);
}, 0);
""")


def test_a_spoken_period_is_not_called_a_start_date():
    """A period such as "third quarter 2027" is not an ISO day. The fix dialog opens on Date as written.
    The question under the mic names that same field."""
    fns = "\n".join(_grab_fn(n) for n in ("fmtSayValue", "sayProposalText"))
    _node(_field_words() + """
var SAY_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];
function eventWord(v) { return v; }
""" + fns + """
var periods = ['third quarter 2027', 'q2 2027'];
for (var i = 0; i < periods.length; i++) {
  var to = periods[i];
  var q = sayProposalText({ action: 'fix', field: 'date', to: to });
  var want = 'Fix the date as written to ' + to + '?';
  if (q !== want) { console.error(JSON.stringify(q) + ' !== ' + JSON.stringify(want)); process.exit(2); }
}
process.exit(0);
""")


def test_a_date_fix_does_not_plant_its_words_as_the_reason():
    """The reason box is the note a Save sends. On a date fix the proposal's note is the words of the date,
    which the question and the To box already show. It must not replace a reason the person typed."""
    fns = _grab_fn("prefillSay")
    _node("""
""" + fns + """
var box = { value: 'duplicate of the June row' };
var filled = null, removed = null;
function fixDialog(row, opts) { filled = opts; }
function removeDialog(row, note) { removed = note; }
global.document = { getElementById: function (id) {
  if (id === 'qnote') return box;
  return { classList: { add: function () {}, remove: function () {} } };
} };
prefillSay({ id: 'r1' }, { action: 'remove', note: 'it is a duplicate' });
if (removed !== 'it is a duplicate') {
  console.error('a remove note did not reach the dialog: ' + JSON.stringify(removed));
  process.exit(3);
}
box.value = 'duplicate of the June row';
filled = null;
prefillSay({ id: 'r1' }, { action: 'fix', field: 'date', to: '2027-03-03',
  note: 'March third twenty twenty seven' });
if (!filled || filled.field !== 'date' || filled.to !== '2027-03-03') {
  console.error('the date did not reach the dialog: ' + JSON.stringify(filled));
  process.exit(4);
}
if (box.value !== 'duplicate of the June row') {
  console.error('the reason box became ' + JSON.stringify(box.value));
  process.exit(2);
}
process.exit(0);
""")
