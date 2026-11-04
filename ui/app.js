/* Proof Reader — U9 Export + second-reader details, on top of U1–U8.
   Reads runs through the local decisions server only; no external calls.
   One state per row comes from the server: run.json + decisions in order. */
(function () {
  'use strict';

  var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed',
                       unreadable: 'unreadable', undecided: 'undecided' };
  var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run',
                      not_needed: 'not needed', unresolved: 'unresolved' };
  var GROUP_NAMES = { kept: 'Checked and kept', corrected: 'Corrected', removed: 'Checked and removed',
                      unverified: 'Unverified', open: 'Still open' };
  var GROUP_ORDER = ['kept', 'corrected', 'removed', 'unverified', 'open'];
  var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };
  var ACTION_WORDS = { keep: 'kept', fix: 'fixed', remove: 'removed' };
  // GROUP-AXIS-001: the server's review projection owns the review words when it
  // sends them — a superseded (stale) Keep reads as history, never as current
  // acceptance. Presence, not truthiness: an empty label falls back.
  function reviewBucket(t) {
    return t.review_bucket || (t.accepted ? 'accepted' : 'proposed');
  }
  // addendum: nothing on screen says kept for a row the projection does not
  // count as accepted (a superseded Keep is history); a server without the
  // projection keeps the old group rule
  function keptCurrent(t) {
    if (partialProjection(t)) return false;
    if (!t || !t.decided_by_person || t.group !== 'kept') return false;
    return t.accepted === undefined || t.accepted === null || t.accepted === true;
  }
  // the row carries the server's review projection (review_label/bucket)
  // H3: the projection is atomic. The complete field set is review_label,
  // review_bucket, accepted, needs_attention, on_calendar (decision_label is
  // legitimately null for rule/correction rows, so it is not required).
  function projectionComplete(t) {
    // M2: the decision_label KEY must be present — null is a legitimate value
    // for rule/correction rows, a missing key is not a projection
    return !!t && typeof t.review_label === 'string' && typeof t.review_bucket === 'string' &&
           typeof t.accepted === 'boolean' && typeof t.needs_attention === 'boolean' &&
           typeof t.on_calendar === 'boolean' && 'decision_label' in t;
  }
  // some projection fields but not all: never trusted — the row needs review,
  // is never kept/accepted, never on the calendar. The trigger is a pivot-only
  // field: needs_attention/on_calendar exist on main too, so they never mark
  // a row partial on their own.
  function partialProjection(t) {
    if (!t || projectionComplete(t)) return false;
    return typeof t.review_label === 'string' || typeof t.review_bucket === 'string' ||
           typeof t.accepted === 'boolean';
  }
  function projected(t) {
    return projectionComplete(t);
  }
  // the '· decided by you' marker. With the projection the row badge is the
  // review label, and every label that carries a person's decision already
  // names them ('Accepted by you', 'Corrected, awaiting your review', …) — the
  // marker would only repeat it; it shows only for a decision label whose
  // review label does not say so. Without the projection: the old group rule.
  function decidedMark(t) {
    if (partialProjection(t)) return false;
    if (projected(t)) {
      return typeof t.decision_label === 'string' && !!t.decision_label &&
             !/\byou(r)?\b/i.test(t.review_label);
    }
    return !!(t.decided_by_person && t.group !== 'removed' && t.group !== 'corrected');
  }
  // the review axis count when the server sends it (else null: no projection)
  function reviewAcceptedCount(st) {
    var c = st && st.counts && st.counts.review;
    return c && typeof c.accepted === 'number' ? c.accepted : null;
  }
  // GROUP-AXIS-001: the group a row is listed, headed and counted under.
  // With the projection, review_bucket decides: only an accepted row is
  // 'Checked and kept'; an excluded row is 'Checked and removed'; a proposed
  // row the checks had kept (undecided, or a superseded Keep) is listed under
  // the projection's own words 'Proposed — needs review'; any other proposed
  // row keeps its check result (corrected / unverified / still open).
  // Without the projection: the server's group, as before.
  function viewGroup(t) {
    if (partialProjection(t)) return 'proposed';
    if (!t || typeof t.review_bucket !== 'string') return t ? t.group : undefined;
    if (t.review_bucket === 'accepted') return 'kept';
    if (t.review_bucket === 'excluded') return 'removed';
    // CALENDAR-RULE-001: a clean proposal the server placed by itself (inclusion other than review) is listed
    // apart from the rows that need review, never under a review or kept heading
    if (typeof t.inclusion === 'string' && t.inclusion !== 'review' && t.inclusion !== 'kept') return 'auto';
    return t.group === 'kept' || t.group === 'removed' ? 'proposed' : t.group;
  }
  var REVIEW_GROUP_ORDER = ['kept', 'auto', 'proposed', 'corrected', 'removed', 'unverified', 'open'];
  // the review axis turns on when the projection is present at all — the
  // server's counts.review, or any row carrying a pivot-only field (a partial
  // row is uncounted even when the run ships no counts.review)
  function reviewAxis() {
    if (reviewAcceptedCount(detail.state) !== null) return true;
    return (detail.run.rows || []).some(function (r) {
      var t = rowTarget(r);
      return typeof t.review_label === 'string' || typeof t.review_bucket === 'string' ||
             typeof t.accepted === 'boolean';
    });
  }
  function groupOrder() {
    return reviewAxis() ? REVIEW_GROUP_ORDER : GROUP_ORDER;
  }
  function groupName(g) {
    if (g === 'auto') return 'Placed automatically — not reviewed by you';
    return g === 'proposed' ? 'Proposed — needs review' : GROUP_NAMES[g];
  }
  // one count per listed group: with the projection, the rows behind the
  // heading (so facet == list == heading == summary line); else the server's
  function groupCount(g) {
    if (!reviewAxis()) return detail.state.counts.rows[g];
    return rowsByGroup(g).length;
  }
  // a row a person fixed that still needs a person (re-check could not run;
  // with the projection, any Fix not yet accepted). A superseded Keep is not
  // a fix: it needs review like any proposal.
  function fixStillOpen(t) {
    if (!t || !t.decided_by_person || !needsAttention(t)) return false;
    if (partialProjection(t)) return false;   // H5: not a fix — needs review
    return projected(t) ? t.last_action === 'fix' : true;
  }
  // RED-2 / addendum 3: ONE presence-aware rule for every effective-field
  // lookup — a target.values own property wins even when null/empty (an
  // explicit clear is a clear, never resurrected from the row's raw value);
  // only an absent key falls back to the row.
  function shownField(t, row, field) {
    var v = t && t.values;
    if (v && Object.prototype.hasOwnProperty.call(v, field)) {
      return v[field] === null || v[field] === undefined ? '' : v[field];
    }
    return (row && row[field]) || '';
  }
  function shownDateText(t, row) {
    // H4: the server puts the one effective date display on the state target
    // itself (runs/state.py: date_display / date_text_cleared), not inside
    // values — a cleared wording with a kept period shows that period
    if (t && typeof t.date_display === 'string') return t.date_display;
    if (t && t.date_text_cleared === true) return '';
    return shownField(t, row, 'date_text');
  }
  // item 38 (adviser ruling): the trail is the historical record — the reader
  // step quotes the reader's ORIGINAL date words verbatim and attributed. The
  // row is the filed record and is never mutated by a person's later clear or
  // edit, so row.date_text is always the reader's own wording; every
  // current-state surface keeps wearing the server's date_display instead.
  function readerDateWords(row) {
    var w = row && row.date_text;
    return (typeof w === 'string' && w.trim() !== '') ? w : null;
  }
  // #52 type provenance (server: runs/proposal.py type_presentation). Labels
  // render as the server sends them; values read in plain words — a known id
  // becomes its word, freeform reader wording passes through untouched.
  function plainTypeValue(v) {
    if (v === null || v === undefined || v === '') return 'Not supplied';
    return EVENT_WORDS[v] !== undefined ? eventWord(v) : String(v);
  }
  // the category value alone (list rows and the calendar line), or null when
  // the server sends no type_presentation — the caller keeps its old wording
  function typeCategoryValue(t) {
    var p = t && t.type_presentation;
    return p && typeof p === 'object' ? plainTypeValue(p.category) : null;
  }
  // both label:value pairs for the detail pane: [category_label, value],
  // [type_hint_label, value]; null without the projection
  function typePresentationPairs(t) {
    var p = t && t.type_presentation;
    if (!p || typeof p !== 'object') return null;
    return [[p.category_label || 'Category', plainTypeValue(p.category)],
            [p.type_hint_label || "Reader's proposed type", plainTypeValue(p.type_hint)]];
  }
  var FIX_FIELDS = ['date_text', 'date', 'date_end', 'event_type', 'title', 'quote', 'company', 'ticker',
                    'program', 'indication'];
  var RULE_WORDS = {
    third_party_calendar_only: 'House rule: a third-party calendar is never a source on its own.',
    person_removed: 'Removed by you.',
    event_in_past: 'The period ended before this run began.',
    source_rule: 'House rule: the source is not one the run accepts.',
    outside_time_window: 'Outside the time window'
  };
  var NO_ERROR = 'The value already passed its checks before the edit.';
  // plain screen words — no raw ids on screen (UI-LANGUAGE rule)
  var EVENT_WORDS = {
    trial_readout: 'Trial results', trial_results: 'Trial results',
    regulatory_decision: 'Regulatory decision', advisory_committee: 'Advisory committee',
    launch: 'Launch', conference: 'Conference', conference_presentation: 'Conference presentation',
    filing: 'Filing', milestone: 'Milestone', investor_day: 'Investor day', earnings: 'Earnings',
    unknown: 'Unknown'
  };
  var FIELD_WORDS = { date_text: 'Date as written', date: 'Start date', date_end: 'End date',
                      event_type: 'Event type', title: 'Title', quote: 'Quote',
                      company: 'Company', ticker: 'Ticker', program: 'Program',
                      indication: 'Condition' };
  // run controls (U8): the job's plain status words and the panel's fixed choices
  var JOB_STATUS_WORDS = { queued: 'queued', running: 'running',
    paused_turn_limit: 'paused at the turn limit', needs_input: 'needs input',
    done: 'done', failed: 'failed' };
  var JOB_ACTIVE = ['queued', 'running'];
  var JOB_PAUSABLE = ['paused_turn_limit', 'needs_input', 'failed'];
  var NEW_RUN_TYPES = [['trial_results', 'Trial results'], ['regulatory_decision', 'Regulatory decision'],
                       ['launch', 'Launch'], ['conference', 'Conference'], ['filing', 'Filing'],
                       ['milestone', 'Milestone']];
  function eventWord(t) {
    if (!t) return 'Unknown';
    return EVENT_WORDS[t] || t.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  // small formatting helpers (no external calls)
  var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                     'August', 'September', 'October', 'November', 'December'];
  function hm(iso) {   // "06:17:19" from an ISO timestamp
    var m = /T(\d{2}):(\d{2}):(\d{2})/.exec(iso || '');
    return m ? m[1] + ':' + m[2] + ':' + m[3] : '';
  }
  function dayMonth(d) {   // "12 Mar" from YYYY-MM-DD
    var p = (d || '').split('-');
    if (p.length !== 3) return '';
    var mi = parseInt(p[1], 10) - 1;
    return MONTHS_SHORT[mi] ? parseInt(p[2], 10) + ' ' + MONTHS_SHORT[mi] : '';
  }
  function monthYear(d) {   // "July 2027" from YYYY-MM-DD
    var p = (d || '').split('-');
    if (p.length !== 3) return '';
    var mi = parseInt(p[1], 10) - 1;
    return MONTHS_LONG[mi] ? MONTHS_LONG[mi] + ' ' + p[0] : p[0];
  }
  function dateShort(iso) {   // "27 Oct 2026" from an ISO timestamp
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    if (!m) return '';
    var mi = parseInt(m[2], 10) - 1;
    return MONTHS_SHORT[mi] ? parseInt(m[3], 10) + ' ' + MONTHS_SHORT[mi] + ' ' + m[1] : m[1];
  }
  function money(c) {   // 0.000352 -> "$0.000352", 0.0018 -> "$0.0018"
    if (c === null || c === undefined) return '';
    return '$' + Number(c).toFixed(6).replace(/0+$/, '').replace(/\.$/, '.00');
  }
  // G6-3/G6-7: money in cents, one way, everywhere a person reads a total;
  // below one cent the precise figure, so nothing ever reads "$0.00"
  function usdCents(v) {
    if (v === null || v === undefined) return '';
    return v >= 0.01 ? '$' + Number(v).toFixed(2) : money(v);
  }
  function dateTimeWords(iso) {   // "29 Oct 2026 15:47" from an ISO timestamp
    if (!iso) return 'no date';
    return dateShort(iso) + ' ' + iso.slice(11, 16);
  }
  function num(n) { return n === null || n === undefined ? '' : Number(n).toLocaleString('en-US'); }

  // ---------------------------------------------------------------- helpers
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function esc(s) { return s === null || s === undefined ? '' : String(s); }
  function clear(node) { node.textContent = ''; return node; }
  // G1-R3: a row landing scrolls the list column only. .main is overflow:hidden
  // but scrollIntoView still moves its scrollTop, so it is forced back to 0 —
  // the heading, page and pane stay on screen on every navigation
  function revealRow(node, opts) {
    if (!node) return;
    if (node.scrollIntoView) node.scrollIntoView(opts || { block: 'nearest' });
    if (document.querySelector) {
      var main = document.querySelector('.main');
      if (main && main.scrollTop) main.scrollTop = 0;
    }
  }
  function btn(cls, text, onClick) {
    var b = el('button', cls, text);
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  var toastTimer = null;
  function toast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2600);
  }

  function statusBadge(status) {
    return el('span', 'st st-' + status, STATUS_WORDS[status] || status);
  }
  // A-F6: a matched row whose date check or type check was 'not needed' did
  // not have that confirmed — "matched" alone would overstate it
  function rowBadge(row, t) {
    // H3/H5: a partial projection is an amber needs-review, never
    // kept/accepted — its own labels are not trusted either
    if (partialProjection(t)) {
      return el('span', 'st st-corrected open', 'Needs review');
    }
    // the projection's review_label is the row's badge when present: accepted
    // green, excluded red, anything else the amber attention style
    if (projected(t)) return el('span', 'st st-' + reviewClass(t), t.review_label);
    if (t.decided_by_person) return decidedBadge(t);
    if (t.status !== 'matched') return statusBadge(t.status);
    var unchecked = [];
    (row.checks || []).forEach(function (c) {
      if (c.result !== 'not_needed') return;
      if (c.name === 'date_match') unchecked.push('date not checked');
      if (c.name === 'type_match') unchecked.push('type not checked');
    });
    if (!unchecked.length) return statusBadge(t.status);
    var b = el('span', 'st st-partmatched', 'matched · ' + unchecked.join(' · '));
    b.title = 'This check did not apply to the row — the checks below say why.';
    return b;
  }
  // the decided wording: the projection's decision_label wins when sent, so a
  // stale Keep never reads 'Kept by you'
  // F7: with the projection, decision_label or else review_label — never the
  // group words (a code removal is not 'Removed by you')
  // Without the projection each surface keeps main's own fallback for a group
  // with no decided word (e.g. 'open'): the badge the group id, the still-open
  // line 'Fixed by you', the decided pane 'Decided by you'.
  function decidedWord(t, otherwise) {
    if (partialProjection(t)) return 'Needs review';
    if (projected(t)) return t.decision_label || t.review_label;
    return DECIDED_BADGE[t.group] || otherwise;
  }
  // the badge colour by review bucket when sent: accepted green, excluded red,
  // anything else the amber attention style (a superseded Keep is not green)
  function reviewClass(t) {
    var bucket = reviewBucket(t);
    return bucket === 'accepted' ? 'kept' : bucket === 'excluded' ? 'removed' : 'corrected open';
  }
  function decidedBadge(t) {
    // a partial projection never wears the group colour (kept green) — amber
    if (partialProjection(t)) return el('span', 'st st-corrected open', decidedWord(t));
    return el('span', 'st st-' + (projected(t) ? reviewClass(t) : t.group), decidedWord(t, t.group));
  }
  function checkDot(result) { return el('span', 'dot ' + result); }
  // DATE-LITERAL-001: under the server's label 'Date wording present in quote'
  // a passing date check is a finding (the words are in the quote), not a
  // confirmation of the date — neutral amber, never the green pass. Presence
  // of that label decides; the old label keeps the old pass style.
  function dateFinding(c) {
    return !!c && (c.name === 'date_match' || c.id === 'date_match') &&
           c.result === 'pass' &&
           (c.label === 'Date period found in quote' || c.label === 'Date wording present in quote');
  }
  // CHECK-RESULTS-WORDS-001: a passing scoped comparison states one
  // concise observed outcome — never an amber warning. Other passes keep the
  // plain 'passed'.
  function passOutcome(c) {
    if (!c || c.result !== 'pass') return null;
    var o = PASS_OUTCOME_WORDS[c.name];
    if (!o) return null;
    return typeof o === 'function' ? o(c) : o;
  }
  function passOutcomeDate(c) {
    var wording = c.timing && typeof c.timing.wording === 'string' && c.timing.wording;
    return 'Timing matches source' + (wording ? ': ' + wording : '');
  }
  var PASS_OUTCOME_WORDS = {
    date_match: passOutcomeDate,
    quote_match: 'Quote matches source',
    selector_match: 'Citation located'
  };
  // OPERATOR_RULING_TIMING_LABEL: the caveat under a current date pass reads
  // the ruling's words verbatim. The UI owns this sentence (checks/ keeps its
  // own reason text untouched); the code's older wording is rewritten here.
  var TIMING_CAVEAT = 'This compares the printed timing only; whether this timing belongs to this event needs your review.';
  var TIMING_CAVEAT_OLD = 'This compares date bounds only; whether the date belongs to this event needs your review.';
  function timingReason(c) {
    var r = c && c.reason ? String(c.reason) : '';
    if (r.indexOf(TIMING_CAVEAT_OLD) >= 0) return r.replace(TIMING_CAVEAT_OLD, TIMING_CAVEAT);
    if (c && c.name === 'date_match' && c.result === 'pass' && r.indexOf(TIMING_CAVEAT) < 0)
      return r ? r + ' ' + TIMING_CAVEAT : TIMING_CAVEAT;
    return r;
  }
  function checkResultWord(result) { return CHECK_WORDS[result] || result; }

  // ---------------------------------------------------------------- theme
  var themeBtn = document.getElementById('theme');
  function applyTheme(dark) {
    document.documentElement.classList.toggle('dark', dark);
    themeBtn.setAttribute('aria-pressed', String(dark));
    try { localStorage.setItem('pr-theme', dark ? 'dark' : 'light'); } catch (e) {}
  }
  try { applyTheme(localStorage.getItem('pr-theme') === 'dark'); } catch (e) {}
  try {   // '?theme=dark|light' in the URL overrides, so a look can be linked and screenshotted
    var qTheme = new URLSearchParams(location.search).get('theme');
    if (qTheme === 'dark' || qTheme === 'light') applyTheme(qTheme === 'dark');
  } catch (e) {}
  themeBtn.addEventListener('click', function () {
    applyTheme(!document.documentElement.classList.contains('dark'));
  });

  // ---------------------------------------------------------------- api
  function getJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function api() {
    return {
      runs: function () { return getJSON('/api/runs'); },
      companies: function () {
        // the New run dialog shows the server's own sentence when the list cannot be read
        return fetch('/api/companies').then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      settings: function () { return getJSON('/api/settings').then(function (b) { return b || {}; }, function () { return null; }); },
      // LETTUCE-OPTION: the New run wording-check choice, its default and its availability
      researchOptions: function () { return getJSON('/api/research/options').then(function (b) { return b || {}; }, function () { return null; }); },
      intake: function (text) {
        // TICKER-INTAKE-001: pasted tickers the server resolves and captures; no model call
        return fetch('/api/intake', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ tickers: text })
        }).then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      intakeStatus: function (id) {
        return fetch('/api/intake/' + encodeURIComponent(id)).then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      run: function (id) { return getJSON('/api/runs/' + encodeURIComponent(id)); },
      decisions: function (id) { return getJSON('/api/runs/' + encodeURIComponent(id) + '/decisions'); },
      newRun: function (body) {
        return fetch('/api/runs/new', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
        }).then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      resume: function (id, body) {
        return fetch('/api/runs/' + encodeURIComponent(id) + '/resume', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {})
        }).then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      researchFinish: function (id) {
        return fetch('/api/runs/' + encodeURIComponent(id) + '/research-finish', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}'
        }).then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      researchStop: function (id) {
        return fetch('/api/runs/' + encodeURIComponent(id) + '/research-stop', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}'
        }).then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      },
      decide: function (id, body) {
        return fetch('/api/runs/' + encodeURIComponent(id) + '/decisions', {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
        }).then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        });
      }
    };
  }

  // ---------------------------------------------------------------- model
  var A = api();
  var runs = [];            // /api/runs list
  var detail = null;        // {id, run, state}
  var route = null;         // parsed hash
  var audit = { filter: 'ALL', selected: null, prev: null, cleared: false };   // selection memory
  var judgmentSel = null;
  var trailSel = null;
  var calSel = null;
  var calSrcSel = null;     // G8-7: which source of a folded row is picked
  var calRouted = null;     // the rowId the calendar last routed on (a change
                            // re-applies the fold mapping; pane re-renders
                            // must not stomp the picked source)
  var chgSel = null;        // selected card index on What changed
  var changedCountCache = null;
  var changedCache = null;  // {key, a, b, cards}
  var pending = false;      // a decision POST is in flight
  var decisionsCache = { id: null, lines: null };   // decisions.jsonl lines, for the trail's person step
  var accuracyCache = { id: null, line: null, tried: false };   // runs/<id>/accuracy.json, one line for the audit header

  function needsAttention(t) {
    if (partialProjection(t)) return true;
    return !!(t && t.needs_attention);
  }
  function rowTarget(row) { return detail.state.targets[row.id] || {}; }
  // DIALOGS-001: the material identity a decision confirms — the server serves
  // it per target as review_basis {version, sha256}; null on older payloads
  function basisOf(rowId) {
    var st = (typeof detail !== 'undefined' && detail && detail.state) || {};
    var b = (st.targets || {})[rowId] ? st.targets[rowId].review_basis : null;
    return b && typeof b === 'object' ? b : null;
  }
  function basisDigest(rowId) {
    var b = basisOf(rowId);
    return b && typeof b.sha256 === 'string' ? b.sha256 : null;
  }
  function rowOf(id) {
    return detail.run.rows.filter(function (r) { return r.id === id; })[0] || null;
  }
  function rowsByGroup(g) {
    return detail.run.rows.filter(function (r) { return viewGroup(rowTarget(r)) === g; });
  }
  function visibleAuditRows() {
    var f = audit.filter;
    return detail.run.rows.filter(function (r) {
      var g = viewGroup(rowTarget(r));
      if (f === 'ALL') return true;
      if (f === 'ATTN') return needsAttention(rowTarget(r));
      // G1-17: the counter filters — the rows behind each count
      if (f === 'EXC') return !!exclusionFor(r.id);
      if (f === 'MCODE' || f === 'MREADER' || f === 'MYOU') {
        var m = mistakeFor(r.id);
        if (!m) return false;
        return (f === 'MCODE' && m.caught_by === 'code') ||
               (f === 'MREADER' && m.caught_by === 'second_reader') ||
               (f === 'MYOU' && m.caught_by === 'person');
      }
      return g === f;
    });
  }
  function unresolvedRows() {
    return detail.run.rows.filter(function (r) { return needsAttention(rowTarget(r)); });
  }
  function decidedRows() {
    // Red Team round 7: a corrected row that still needs a person (re-check could
    // not run, or the two readers still undecided) is waiting, not decided — it
    // is listed once, under Waiting for you
    return detail.run.rows.filter(function (r) {
      var t = rowTarget(r);
      return t.decided_by_person && !needsAttention(t);
    });
  }
  // glass adviser (3): a Fix is not a Keep. A person-fixed row the projection
  // does not carry as accepted wears a badge that says 'not reviewed by you',
  // so it must not sit under 'Decided by you' either — one projection, one
  // story. It lists under 'Corrected by you, not kept'.
  function fixedNotKept(t) {
    if (!t.decided_by_person || needsAttention(t)) return false;
    if (t.group === 'removed' || t.group === 'kept') return false;
    return typeof t.accepted === 'boolean' ? !t.accepted : t.group === 'corrected';
  }
  // ONE-MODEL-RUN-001: a run started with Muse Spark only (choices.models
  // 'one'); the same sentences as runs/one_model.py
  var ONE_MODEL_LINE = 'One model: Muse Spark only (no second reader, no escalation).';
  var NO_SECOND_READER = 'No second reader in this run';
  function oneModelRun(meta) {
    return !!(meta && meta.choices && meta.choices.models === 'one');
  }
  // A-C9: one name per reader, from the run itself — the first reader from
  // the rows it proposed, the second and Wonder from the run's own record
  function readerNames() {
    var meta = detail.run.run || {};
    var first = null;
    detail.run.rows.forEach(function (r) { if (!first && r.proposed_by) first = r.proposed_by; });
    var names = [first || 'Muse Spark'];
    if (meta.second_reader && meta.second_reader.model) names.push(meta.second_reader.model);
    if (meta.escalation && meta.escalation.model) names.push(meta.escalation.model);
    return names;
  }
  // 'Muse Spark and Muse Spark Open'
  function bothReadersWord() {
    return readerNames().slice(0, 2).join(' and ');
  }

  function sourceOf(row) {
    var id = row.source_id;
    return detail.run.sources.filter(function (s) { return s.id === id; })[0] || null;
  }
  // G9-2: the state list is the superset — it carries run.json's mistakes
  // plus every fix in effect that is a demonstrated mistake. The chips and
  // badges read it so a person's fix counts in 'by you' AND its chip finds
  // the row; the run's static list is only a fallback when state has none.
  function mistakeFor(rowId) {
    var list = (detail.state && detail.state.mistake_list) || detail.run.mistakes || [];
    return list.filter(function (m) { return m.target === rowId; })[0] || null;
  }
  function exclusionFor(rowId) {
    return (detail.state.exclusions || []).filter(function (x) { return x.target === rowId; })[0] || null;
  }
  // U37: run.json's exclusions[] are history — they stay even after a person
  // keeps the row — so the pane and the audit note can still say code had
  // removed it once the live exclusion is gone
  function codeExclusionHistory(rowId) {
    return (detail.run.exclusions || []).filter(function (x) {
      return x.target === rowId && x.by === 'code';
    })[0] || null;
  }

  // DIALOGS-002: one generation counter for "which load owns the screen".
  // refresh and switchRun both capture it when they start and apply their
  // answer only while still the newest — a late GET of the previous run can
  // never paint its document under the new run's identity.
  var loadGen = 0;
  function refresh() {
    var gen = ++loadGen;
    var rid = detail.id;
    decisionsCache = { id: null, lines: null };
    changedCache = null;
    changedCountCache = null;
    accuracyCache = { id: null, line: null, tried: false };
    return A.run(rid).then(function (body) {
      if (gen !== loadGen) return;    // a newer load owns the screen now
      detail = { id: rid, run: body, state: body.state };
      renderShell();
      render();
    });
  }

  // ---------------------------------------------------------------- routing
  // '#/<run>/audit[/<rowId>][?f=ALL|ATTN|kept|...]'
  // '#/<run>/source/<srcId>/p<n>[?v=<id>][&back=audit]'
  // '#/<run>/judgment[/<rowId>][?d=<action>]'
  // '#/<run>/trail[/<rowId>]'
  // '#/<run>/calendar'
  // '#/<run>/changed[?a=<runId>&b=<runId>]'
  // '#/<run>/export'
  function parseHash() {
    var h = location.hash || '';
    if (h === '' ) return null;
    if (h === '#/' || h === '#') return { screen: 'none' };
    var q = {};
    var path = h.replace(/^#\//, '');
    var qi = path.indexOf('?');
    if (qi >= 0) {
      path.slice(qi + 1).split('&').forEach(function (kv) {
        var p = kv.split('=');
        if (p[0]) q[p[0]] = decodeURIComponent(p[1] || '');
      });
      path = path.slice(0, qi);
    }
    var seg = path.split('/').map(decodeURIComponent);
    var out = { runId: seg[0], screen: seg[1] || 'audit' };
    if (out.screen === 'audit') { out.rowId = seg[2] || null; out.filter = q.f || null; out.dialog = q.d || null; }
    if (out.screen === 'source') {
      out.srcId = seg[2] || null; out.page = seg[3] || null;
      out.valueId = q.v || null; out.back = q.back || null;
      out.w = q.w ? Number(q.w) : null;
    }
    if (out.screen === 'judgment') { out.rowId = seg[2] || null; out.dialog = q.d || null; }
    if (out.screen === 'trail') { out.rowId = seg[2] || null; }
    if (out.screen === 'calendar') { out.rowId = seg[2] || null; out.w = q.w ? Number(q.w) : null; }
    if (out.screen === 'changed') { out.a = q.a || null; out.b = q.b || null; }
    return out;
  }

  function hashFor(o) {
    var h = '#/' + encodeURIComponent(o.runId) + '/' + o.screen;
    var q = [];
    if (o.screen === 'audit') {
      if (o.rowId) h += '/' + encodeURIComponent(o.rowId);
      if (o.filter && o.filter !== 'ALL') q.push('f=' + o.filter);
      if (o.dialog) q.push('d=' + encodeURIComponent(o.dialog));
    } else if (o.screen === 'source') {
      h += '/' + encodeURIComponent(o.srcId) + '/' + o.page;
      if (o.valueId) q.push('v=' + encodeURIComponent(o.valueId));
      if (o.back) q.push('back=' + o.back);
      if (o.w) q.push('w=' + o.w);
    } else if (o.screen === 'judgment' || o.screen === 'trail') {
      if (o.rowId) h += '/' + encodeURIComponent(o.rowId);
      if (o.screen === 'judgment' && o.dialog) q.push('d=' + encodeURIComponent(o.dialog));
    } else if (o.screen === 'calendar') {
      if (o.rowId) h += '/' + encodeURIComponent(o.rowId);
      if (o.w) q.push('w=' + o.w);
    } else if (o.screen === 'changed') {
      if (o.a) q.push('a=' + encodeURIComponent(o.a));
      if (o.b) q.push('b=' + encodeURIComponent(o.b));
    }
    return q.length ? h + '?' + q.join('&') : h;
  }

  function navigate(o) {
    var h = hashFor(o);
    if (location.hash !== h) location.hash = h;
    else render();
  }
  // G1-6: close things without a new history entry — browser Back must never
  // re-open what Esc closed
  function replaceRoute(o) {
    var h = hashFor(o);
    history.replaceState(null, '', location.pathname + location.search + h);
    route = parseHash();
    render();
  }
  function currentRunId() { return detail ? detail.id : null; }

  // ---------------------------------------------------------------- render
  function render() {
    route = parseHash();
    if (!route) route = { runId: detail.id, screen: 'audit' };   // fresh visit: no hash yet
    if (!detail) return;
    if (route.runId && route.runId !== detail.id) { switchRun(route.runId, false); return; }

    if (route.screen === 'source') renderSource();
    else if (route.screen === 'judgment') renderJudgment();
    else if (route.screen === 'trail') renderTrail();
    else if (route.screen === 'calendar') renderCalendar();
    else if (route.screen === 'changed') renderChanged();
    else if (route.screen === 'export') renderExport();
    else renderAudit();
    updateRunCardJob();

    document.querySelectorAll('#nav .ni[data-scr]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.scr === (route.screen === 'none' ? 'audit' : route.screen));
    });
    var scr = document.getElementById('scr-' + (route.screen === 'none' ? 'audit' : route.screen));
    var narrow = window.matchMedia('(max-width: 900px)').matches;
    var picked = (route.screen === 'source' && !!route.srcId) ||
                 (route.screen === 'trail' && !!route.rowId);
    document.querySelectorAll('.screen').forEach(function (s) { s.classList.toggle('on', s === scr); });
    if (scr) scr.classList.toggle('picked', narrow && (picked || route.screen === 'calendar'));
    // G4-4: a jump to a row lands with it scrolled into view — screens only
    // become visible here, so scrolls fired inside renderX() were no-ops
    if (scr) {
      var onIt = scr.querySelector('.row.on, .crow.on, .pg.on, .chcard.on');
      if (onIt) revealRow(onIt);
    }
    document.getElementById('src-back').hidden = !(narrow && route.screen === 'source' && !route.back);
    var backBtn = document.getElementById('src-back-audit');
    backBtn.hidden = !(route.screen === 'source' && (route.back === 'audit' || route.back === 'calendar'));
    backBtn.textContent = route.back === 'calendar' ? '← Back to calendar' : '← Back to Audit log';
    // G2-5: Back to rows is visible at desktop width whenever a row is open
    document.getElementById('trail-back').hidden = !(route.screen === 'trail' && route.rowId);
    // G1-5: a row opened from Audit log (Decide) offers a way back
    document.getElementById('judgment-back-audit').hidden =
      !(route.screen === 'judgment' && route.rowId);
  }

  // ---------------------------------------------------------------- shell
  function renderShell() {
    var meta = detail.run.run || {};
    var st = detail.state;
    var card = document.getElementById('runcard');
    card.hidden = false;
    // G8-11: a plain 'Run' label above a plain status card — the picker is
    // the 'Saved runs' button under New run
    var lbl = document.getElementById('runcard-lbl');
    if (!lbl) {
      lbl = el('div', 'runcard-lbl');
      lbl.id = 'runcard-lbl';
      lbl.append(el('span', null, 'Run'));
      card.parentNode.insertBefore(lbl, card);
    }
    document.getElementById('runcard-title').textContent = meta.label || ('Run ' + meta.number);
    // the card header: run, date in words, needs attention
    document.getElementById('runcard-sub').textContent =
      'Run ' + meta.number + ' · ' + dateTimeWords(meta.started_at) +
      ' · needs attention ' + st.unresolved + (detail.run.as_of_note ? ' · ' + detail.run.as_of_note : '');
    // the saved-runs button's count rides the same runs list
    updateSavedRunsBtn();
    // the sample warning stays warm; 'click to switch runs' is gone
    var flag = document.getElementById('runcard-flag');
    flag.hidden = !meta.sample;
    if (meta.sample) flag.textContent = 'Sample data — not real events';
    var rn = readerNames();
    document.getElementById('side-foot').textContent =
      (meta.sample ? 'Sample data — not real events · ' : '') +
      (rn.length === 1 ? rn[0] + ' reads' : rn.join(' · ') + ' read') + ' · Code checks';
    document.title = 'Proof Reader — ' + (meta.label || '');
    updateRunCardJob();

    var nRows = detail.run.rows.length;
    var outcomeView = st.page_outcomes || {};
    var nPages = outcomeView.recorded ? outcomeView.counts.captured : '—';
    document.getElementById('ct-audit').textContent = nRows;
    document.getElementById('ct-source').textContent = nPages;
    document.getElementById('ct-judgment').textContent = st.unresolved;
    document.getElementById('ni-judgment').classList.toggle('alert', st.unresolved > 0);
    document.getElementById('ct-trail').textContent = nRows;
    document.getElementById('ct-calendar').textContent = calendarRows().length;
    document.getElementById('ct-changed').textContent = changedCountCache || '';
    ensureJobPoll();
  }

  // kept and corrected rows with a date — the ones that go on the calendar
  // G8-7: rows that are the same event — same ticker + program + event kind +
  // same period start — fold to ONE calendar row. The folded entry keeps the
  // first source's row and carries `fold`, every source row in it.
  // G9-1: a corrected row reaches the calendar only when the re-check of the
  // fix passed, no re-check is pending and the readers are decided — the same
  // rule as runs/state.py. A fix that failed, could not run
  // or waits on a re-check stays off until the person keeps it.
  function calendarRows() {
    var out = [];
    // Apply the server's shared interval partition before folding. This keeps
    // rows with different placement (or timing) separate on the glass.
    var partition = detail.run.calendar_partition || null;
    var bucketById = {};
    if (partition) {
      ['calendar', 'past', 'later', 'contingent', 'undated', 'timing_unresolved'].forEach(function (bucket) {
        (partition[bucket] || []).forEach(function (entry) { bucketById[entry.id] = bucket; });
      });
    }
    detail.run.rows.forEach(function (r) {
      var t = rowTarget(r);
      if (partialProjection(t)) return;   // H3: never on the calendar
      if (!t.on_calendar) return;
      var bucket = bucketById[r.id];
      if (!bucket) return; // the shared read view is the only placement authority
      var vals = t.values || {};
      var d = vals.date || null;
      var end = vals.date_end || null;
      // the calendar entry wears the target-level effective display
      var dt = typeof t.date_display === 'string' ? t.date_display
        : t.date_text_cleared === true ? '' : (vals.date_text || null);
      var entry = { row: r, t: t, date: d || null, nodate: !d,
                    period: !!end, end: end || null, dateText: dt || null, bucket: bucket };
      out.push(entry);
    });
    var groups = {}, order = [];
    out.forEach(function (e) {
      // Fold only rows with identical current values and full interval, and
      // never fold across server placement, indication, or decision state.
      var vals = e.t.values || {};
      var identity = {
        bucket: e.bucket,
        ticker: shownVal(vals, e.row, 'ticker') || '',
        company: shownVal(vals, e.row, 'company') || '',
        program: shownVal(vals, e.row, 'program') || '',
        indication: shownVal(vals, e.row, 'indication') || '',
        event_type: shownVal(vals, e.row, 'event_type') || '',
        title: shownVal(vals, e.row, 'title') || '',
        date_text: e.dateText,
        date: e.date,
        date_end: e.end,
        quote: shownVal(vals, e.row, 'quote') || ''
      };
      var key = JSON.stringify(identity);
      if (!groups[key]) { groups[key] = []; order.push(groups[key]); }
      groups[key].push(e);
    });
    // A-F8: the representative is the first source whose state is undecided or
    // whose checks did not all pass — the event opens where the question is;
    // only a fully settled fold keeps its first-in-file member. The pill
    // still counts every source.
    return order.map(function (g) {
      var rep = g.filter(function (e) {
        return e.t.status === 'undecided' ||
               effectiveChecks(e.row, e.t).some(function (c) { return c.result !== 'pass'; });
      })[0] || g[0];
      rep.fold = g;
      return rep;
    });
  }
  // G3-2/G3-3: how long a period row runs — day ranges (a congress weekend)
  // count as exact dates, not periods
  function calDays(r) {
    if (!r.period || !r.end) return 0;
    return Math.round((new Date(r.end.slice(0, 10)) - new Date(r.date.slice(0, 10))) / 86400000);
  }
  // G3-R1: a period that is exactly one whole month (the 1st to its last day)
  // is a month-only date — the source named no day — never an exact range
  function calIsMonthPeriod(r) {
    if (!r.period || !r.end) return false;
    var d1 = parseInt(r.date.slice(8, 10), 10), d2 = parseInt(r.end.slice(8, 10), 10);
    if (d1 !== 1 || r.date.slice(0, 7) !== r.end.slice(0, 7)) return false;
    var last = new Date(parseInt(r.end.slice(0, 4), 10),
                        parseInt(r.end.slice(5, 7), 10), 0).getDate();
    return d2 === last;
  }
  function calIsExact(r) { return !r.period || (calDays(r) <= 31 && !calIsMonthPeriod(r)); }
  // whole months a period spans, inclusive: Jan 1 – Mar 31 is 3
  function calMonths(r) {
    if (!r.period || !r.end) return 1;
    return (parseInt(r.end.slice(0, 4), 10) - parseInt(r.date.slice(0, 4), 10)) * 12 +
           (parseInt(r.end.slice(5, 7), 10) - parseInt(r.date.slice(5, 7), 10)) + 1;
  }
  // G3-4: the canonical words for a quarter-length period — the one marker
  // under which every writing of the same quarter sits
  function calQuarterKey(r) {
    var y = r.date.slice(0, 4), m = parseInt(r.date.slice(5, 7), 10);
    return 'Q' + (Math.floor((m - 1) / 3) + 1) + ' ' + y;
  }

  // G3-2/G9-R3: the header a row sits under — never one narrower than its
  // own dates. A month-only date and exact days sit in their month's header;
  // a quarter or any longer span gets its OWN header, sorted right after the
  // first month it spans begins; half-years and years get their named headers
  function calHeader(r) {
    var start = r.date.slice(0, 7);
    if (calIsExact(r)) return { key: 'm:' + start, sort: start, len: 1, label: null };
    // G3-R1: a month-only date sits in its month's header as a period marker
    if (calIsMonthPeriod(r)) return { key: 'm:' + start, sort: start, len: 2, label: null };
    var m = calMonths(r);
    var y = r.date.slice(0, 4);
    if (m === 6) {
      var half = parseInt(r.date.slice(5, 7), 10) <= 6 ? 'First' : 'Second';
      return { key: 'h:' + y + ':' + half, sort: start, len: 6,
               label: half + ' half of ' + y };
    }
    if (m === 12) return { key: 'y:' + y, sort: start, len: 12, label: y };
    // G9-R3: a quarter never sits under its start month's header ('Q4 2026'
    // under 'October 2026') — its own quarter header, after that month's;
    // other spans (e.g. 'by year-end', Sep–Dec) get their own header too,
    // labelled with the source wording
    return { key: m === 3 ? 'q:' + calQuarterKey(r) : 'p:' + start + ':' + r.end,
             sort: start, len: m,
             label: m === 3 ? calQuarterKey(r) : null };
  }
  // G3-R2: headers read in the order the periods close — a month's header
  // comes at its month, a quarter right after the first month it spans, a
  // half-year at its mid/end, a year at December — so February 2027 never
  // lands after 'First half of 2027' or '2027'
  function calHeaderOrd(h) {
    if (h.key.slice(0, 2) === 'm:') return h.key.slice(2);
    var p = h.key.split(':');
    if (p[0] === 'q' || p[0] === 'p') return h.sort;
    if (p[0] === 'h') return p[1] + (p[2] === 'First' ? '-06' : '-12');
    return p[1] + '-12';
  }

  function switchRun(runId, nav) {
    var gen = ++loadGen;
    A.run(runId).then(function (body) {
      if (gen !== loadGen) return;    // DIALOGS-002: a newer load owns the screen
      detail = { id: runId, run: body, state: body.state };
      audit = { filter: 'ALL', selected: null, prev: null, cleared: false };
      judgmentSel = null;
      trailSel = null;
      calSel = null;
      calSrcSel = null;
      calRouted = null;
      chgSel = null;
      changedCache = null;
      accuracyCache = { id: null, line: null, tried: false };
      decisionsCache = { id: null, lines: null };
      renderShell();
      if (nav !== false) navigate({ runId: runId, screen: 'audit' });
      else render();
    }).catch(function () { toast('That run could not be read.'); });
  }

  // the sealed-set accuracy line (U10): reads runs/<id>/accuracy.json, the latest
  // sealed scoring of this run, when the server serves it; hidden when absent
  // the label is earned: only a report whose key is sealed reads 'Sealed set';
  // an author-keyed report is labeled in plain words (G1-8)
  function accSealLabel(acc) {
    return acc && acc.key && acc.key.sealed === true ? 'Sealed set: ' : 'Our own key, not sealed: ';
  }
  // Red Team round 5 (finding 6): a shown rate is one model's counts, never a
  // mix stitched from several models. The header line names the model and shows
  // only the first model that has any counts; every model is listed separately
  // in the Accuracy panel (accModelLines).
  function accuracyLineFrom(acc) {
    if (!acc || typeof acc !== 'object') return null;
    // G6-11b: a bucket that scores the readers together reads 'Both readers'
    // (nested: the redteam harness reads this function alone)
    function bucketName(name) {
      var first = (acc.readers && acc.readers.first) || [];
      var parts = String(name).split(',').map(function (s) { return s.trim(); });
      return parts.length >= 2 && parts.every(function (p) { return first.indexOf(p) >= 0; })
        ? 'Both readers' : name;
    }
    function part(x) {
      if (!x || typeof x !== 'object') return null;
      var f = null, n = null;
      ['found', 'scored', 'caught'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) f = x[k]; });
      ['N', 'M', 'total'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) n = x[k]; });
      return n !== null ? (f !== null ? f : '?') + '/' + n : null;
    }
    function bitsFrom(src) {
      var b = [];
      var v = part(src.values) ||
        (src.table && src.table.counts ? part({ found: src.table.counts.found, N: src.table.N }) : null);
      var ev = part(src.events) ||
        (src.events && src.events.counts ? part({ found: src.events.counts.found, M: src.events.M }) : null);
      var pl = part(src.planted);
      if (v) b.push('values ' + v);
      if (ev) b.push('events ' + ev);
      if (pl) b.push('planted ' + pl);
      return b;
    }
    var names = acc.models && typeof acc.models === 'object' ? Object.keys(acc.models) : [];
    for (var i = 0; i < names.length; i++) {
      var bits = bitsFrom(acc.models[names[i]] || {});
      if (bits.length) return accSealLabel(acc) + bucketName(names[i]) + ': ' + bits.join(' · ');
    }
    // no per-model counts: the flat single-source shape, nothing to mix
    var flat = bitsFrom(acc);
    return flat.length ? accSealLabel(acc) + flat.join(' · ') : null;
  }
  function ensureAccuracyLine() {
    var slot = document.getElementById('audit-accuracy');
    if (!detail) return;
    var id = detail.id;
    if (accuracyCache.id === id && accuracyCache.tried) {
      slot.hidden = !accuracyCache.line;
      if (accuracyCache.line) slot.textContent = accuracyCache.line;
      return;
    }
    accuracyCache = { id: id, line: null, tried: true };
    slot.hidden = true;
    var urls = ['/runs/' + encodeURIComponent(id) + '/accuracy.json',
                '/api/runs/' + encodeURIComponent(id) + '/accuracy'];
    (function probe(i) {
      if (i >= urls.length) return;
      fetch(urls[i]).then(function (r) {
        if (r.status === 409) {      // Job 50b: show the reason, no counts
          return r.json().then(function (b) {
            var why = String((b && b.reason) || 'This Accuracy result does not match the run\'s current inputs. Re-score to show it.');
            accuracyCache = { id: id, line: why, tried: true };
            if (route && route.screen === 'audit') { slot.textContent = why; slot.hidden = false; }
          });
        }
        if (!r.ok) return probe(i + 1);
        return r.json().then(function (acc) {
          var line = accuracyLineFrom(acc);
          accuracyCache = { id: id, line: line, tried: true };
          if (line && route && route.screen === 'audit') {
            slot.textContent = line;
            slot.hidden = false;
          }
        });
      }).catch(function () { probe(i + 1); });
    })(0);
  }

  function renderAudit() {
    if (route.filter) audit.filter = route.filter;
    if (route.rowId && rowOf(route.rowId)) { audit.selected = route.rowId; audit.cleared = false; }
    var st = detail.state;
    var meta = detail.run.run || {};

    var sub = document.getElementById('audit-sub');
    sub.textContent = 'Every proposed row and what happened to it · ' + (meta.label || 'run ' + meta.number);
    var rl = document.getElementById('audit-runlabel');
    rl.hidden = false;
    rl.textContent = meta.label || '';
    rl.className = 'runlabel' + (meta.sample ? ' sample' : '');

    // G1-17: the counters are filters, exactly like Needs attention — click
    // narrows the list, the chip reads 'Showing: …', the filter rides the
    // URL, click again clears; zero counts are not clickable
    var countersEl = clear(document.getElementById('audit-counters'));
    var chipDefs = [
      ['MCODE', 'mistakes caught by code', st.mistakes.code],
      ['MREADER', 'mistakes caught by the second reader', st.mistakes.second_reader],
      ['MYOU', 'mistakes caught by you', st.mistakes.person],
      ['EXC', 'excluded', st.excluded.code + st.excluded.person]
    ];
    chipDefs.forEach(function (d) {
      // ONE-MODEL-RUN-001: no second reader ran, so no 'caught by the second reader: 0'
      if (d[0] === 'MREADER' && oneModelRun(meta)) {
        countersEl.append(el('span', 'ctchip zero', NO_SECOND_READER));
        return;
      }
      if (!d[2]) {
        countersEl.append(el('span', 'ctchip zero', d[1] + ': 0'));
        return;
      }
      var on = audit.filter === d[0];
      var b = btn('ctchip' + (on ? ' on' : ''), '');
      b.textContent = (on ? 'Showing: ' : '') + d[1] + ' · ' + d[2];
      b.addEventListener('click', function () {
        audit.filter = on ? 'ALL' : d[0];
        navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected, filter: audit.filter });
      });
      countersEl.append(b);
    });
    ensureAccuracyLine();

    var ab = document.getElementById('attnbtn');
    ab.textContent = (audit.filter === 'ATTN' ? 'Showing: needs attention · ' : 'Needs attention · ') + st.unresolved;
    ab.classList.toggle('on', audit.filter === 'ATTN');
    ab.onclick = function () {
      if (audit.filter === 'ATTN') {
        audit.filter = 'ALL';
        if (audit.prev && rowOf(audit.prev)) audit.selected = audit.prev;
        audit.prev = null;
      } else {
        audit.prev = audit.selected;
        audit.filter = 'ATTN';
      }
      navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected, filter: audit.filter });
    };

    renderAuditFacets();
    renderAuditRows();
    renderAuditMid();
    renderAuditDet();

    // U37: the Keep-anyway dialog mounts on the audit route too — open it for
    // the selected row, and make sure no stale dialog survives a plain audit view
    if (route.dialog && audit.selected && rowOf(audit.selected)) openDialog(rowOf(audit.selected), route.dialog);
    else closeDialog();
    restoreFolds(document.getElementById('audit-det'),
                 currentRunId() + '/audit/' + (audit.selected || ''));
  }

  function renderAuditFacets() {
    var host = clear(document.getElementById('audit-facets'));
    var st = detail.state;
    var defs = [['ALL', 'All']].concat(groupOrder().map(function (g) { return [g, groupName(g)]; }));
    defs.forEach(function (d) {
      // GROUP-AXIS-001: each facet counts the rows its filter lists — the kept
      // facet only accepted rows (fallback: the server's group counts)
      var n = d[0] === 'ALL' ? detail.run.rows.length : groupCount(d[0]);
      var b = btn('fc' + (audit.filter === d[0] ? ' on' : ''), '');
      b.append(document.createTextNode(d[1] + ' '), el('b', null, String(n)));
      b.addEventListener('click', function () {
        audit.filter = d[0];
        navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected, filter: audit.filter });
      });
      host.append(b);
    });
  }

  function syncAuditSelection(vis) {
    // G1-5: Esc closed the pane on purpose — do not re-select on re-render
    if (audit.cleared) { audit.selected = null; return; }
    // G1-9: a filter never changes the person's selected row; only fill an
    // empty selection (fresh run) or one whose row no longer exists
    if (audit.selected && rowOf(audit.selected)) return;
    audit.selected = vis.length ? vis[0].id : null;
  }

  // ---------------- RESEARCHER-FULL-001 S1: the research projection, rendered
  // as the server sends it — no UI semantic logic (runs/README.md "Research runs")
  // the GET /api/runs/{id} envelope: the run.json record — with `research`,
  // `choices` and `status` — is detail.run.RUN (FIX-ON-FIND, Manus 16:52)
  function runResearch() {
    var r = detail && detail.run && detail.run.run;
    return (r && r.research) || null;
  }
  var RESEARCH_STEP_WORDS = { request: 'Request', search: 'Search', fetch: 'Fetch',
    proposal: 'Proposal', check: 'Check', coverage: 'Coverage', finish: 'Finish',
    limit: 'Limit', repeat: 'Repeat', error: 'Error', prose: 'Note' };
  var RESEARCH_STATUS_WORDS = { queued: 'Queued', researching: 'Researching',
    finished: 'Finished', stopped: 'Stopped', failed: 'Failed' };
  var CONDITIONS_WORDS = { not_established: 'Conditions not established',
    none_stated: 'No conditions stated', stated: 'Conditions stated' };
  // one actor per step, the kind in plain words, the step's own sentence
  // the header is the record's own label — a partly-reviewed run never reads
  // 'not independently reviewed' here while its review steps list below
  function researchTrailBlock(rs) {
    var box = el('div', 'rstrail');
    box.append(el('div', 'rstitle', (rs && rs.label) || 'Agent research — not independently reviewed'));
    ((rs && rs.steps) || []).forEach(function (s2) {
      // PROJECTION-003: a support attempt is shown once, as the row's
      // attributed step (supportTrailSteps, with served currentness) — the
      // raw run-level copy would double it with stale-agnostic wording
      if (s2.who === 'capture.support_signal') return;
      var line = el('div', 'rsstep');
      // G1-1: a module path (runs.research) never prints — the actor is the
      // step's tag ('Code'); model names pass through unchanged
      var who = /^[a-z_]+(\.[a-z_]+)+$/.test(s2.who || '') ? (s2.tag || 'Code') : (s2.who || s2.tag || '');
      line.append(el('span', 'rswho', who));
      line.append(el('span', 'rskind', RESEARCH_STEP_WORDS[s2.kind] || s2.kind || ''));
      line.append(el('span', 'rstext', s2.text || ''));
      if (s2.at) line.append(el('span', 'rsat', hm(s2.at)));
      box.append(line);
    });
    return box;
  }
  // per-company progress for the run card
  function researchCompanyBlock(companies) {
    if (!companies) return null;
    var ticks = Object.keys(companies);
    if (!ticks.length) return null;
    var box = el('div', 'rscomp');
    ticks.forEach(function (t) {
      var c = companies[t] || {};
      var line = el('div', 'rscline');
      line.append(el('b', null, t));
      line.append(el('span', 'rsst', RESEARCH_STATUS_WORDS[c.status] || c.status || ''));
      // PROJECTION-002: finished research is not finished review — the recorded
      // review stage (and who ran it) rides the company line until it is done
      if (c.review_status && c.review_status !== 'done') {
        var rev = ((runResearch() || {}).review || {})[t] || {};
        line.append(el('span', 'rsst', 'review: ' + c.review_status + (rev.reviewer ? ' (' + rev.reviewer + ')' : '')));
      }
      var bits = [];
      if (c.requests !== null && c.requests !== undefined) bits.push(num(c.requests) + ' requests');
      if (c.searches !== null && c.searches !== undefined) bits.push(num(c.searches) + ' searches');
      if (c.fetches !== null && c.fetches !== undefined) bits.push(num(c.fetches) + ' fetches');
      if (c.pages_delivered !== null && c.pages_delivered !== undefined) bits.push(num(c.pages_delivered) + ' pages delivered');
      // G6-3/G6-7: money in cents, one way ($2.65, never $2.651162)
      if (c.spend_usd !== null && c.spend_usd !== undefined) bits.push(usdCents(c.spend_usd));
      if (bits.length) line.append(el('span', 'rscounts', bits.join(' · ')));
      var why = c.status_reason || (c.limitation && c.limitation.reason) || '';
      if (why) line.append(el('span', 'rswhy', why));
      box.append(line);
    });
    return box;
  }
  // PROJECTION-002: the recorded S2 review in one honest line — who reviewed
  // (the recorded reviewer identity, e.g. Claude Opus 5.5 or the declared
  // fallback) and the review stage across companies. Nothing is claimed that
  // the record does not carry.
  function researchReviewLine() {
    var rs = runResearch();
    var review = rs && rs.review;
    if (!review || typeof review !== 'object') return null;
    var ticks = Object.keys(review);
    if (!ticks.length) return null;
    var stages = {}, names = {};
    ticks.forEach(function (t) {
      var e = review[t] || {};
      if (e.status) stages[e.status] = (stages[e.status] || 0) + 1;
      if (e.reviewer) names[e.reviewer] = true;
    });
    var st = Object.keys(stages), who = Object.keys(names);
    if (!st.length && !who.length) return null;
    var bits = [];
    if (who.length) bits.push('review by ' + who.join(', '));
    if (st.length === 1) bits.push('review ' + st[0] + (stages[st[0]] > 1 ? ' (' + stages[st[0]] + ' companies)' : ''));
    else if (st.length) bits.push(st.map(function (s) { return 'review ' + s + ' × ' + stages[s]; }).join(' · '));
    return bits.join(' · ');
  }
  // 'Searched, not found' notes for the Audit log
  function researchNotFoundBlock(coverage) {
    var recs = (coverage || []).filter(function (r) { return r && r.searched_not_found; });
    if (!recs.length) return null;
    var box = el('div', 'rsnf');
    box.append(el('h4', null, 'Searched, not found'));
    recs.forEach(function (r) {
      box.append(el('div', 'rsnfl',
        (r.ticker ? r.ticker + ' · ' : '') + (r.category || '') + ': ' + (r.question || '')));
    });
    var scope = recs[0].scope_note;
    if (scope) box.append(el('div', 'hint', scope));
    return box;
  }
  // the row's research label and its Context section (completed facts)
  function researchLabelLine(row) {
    var rs = row && row.research;
    if (!rs || !rs.label) return null;
    return el('div', 'rslabel', rs.label);
  }
  function researchContextSection(row) {
    var rs = row && row.research;
    if (!rs) return null;
    var facts = [];
    if (rs.conditions_state && CONDITIONS_WORDS[rs.conditions_state]) facts.push(CONDITIONS_WORDS[rs.conditions_state]);
    (rs.issues || []).forEach(function (i2) { if (i2 && i2.message) facts.push(i2.message); });
    if (rs.revision !== null && rs.revision !== undefined) facts.push('Proposal revision ' + rs.revision);
    if (!facts.length) return null;
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Context'));
    var box = el('div', 'ba blue');
    facts.forEach(function (f) { box.append(el('div', null, f)); });
    sec.append(box);
    return sec;
  }

  function renderAuditRows() {
    var host = clear(document.getElementById('audit-rows'));
    var vis = visibleAuditRows();
    syncAuditSelection(vis);
    // fix-on-find (Manus 17:07 sibling risk): the research 'Searched, not
    // found' notes show even before the first proposal
    var nf = researchNotFoundBlock(runResearch() && runResearch().coverage);
    if (!vis.length) {
      host.append(el('div', 'empty', audit.filter === 'ATTN'
        ? 'Nothing needs attention. Every row has a home.' : 'No rows in this group.'));
      if (nf) host.append(nf);
      return;
    }
    var grouped = audit.filter === 'ALL' || audit.filter === 'ATTN' ||
                  audit.filter === 'MCODE' || audit.filter === 'MREADER' ||
                  audit.filter === 'MYOU' || audit.filter === 'EXC';
    if (grouped) {
      // one heading per group: sort by group order, stable within a group
      var order = {}, idx = {};
      groupOrder().forEach(function (g, i) { order[g] = i; });
      detail.run.rows.forEach(function (r, i) { idx[r.id] = i; });
      vis = vis.slice().sort(function (a, b) {
        var d = order[viewGroup(rowTarget(a))] - order[viewGroup(rowTarget(b))];
        return d !== 0 ? d : idx[a.id] - idx[b.id];
      });
    }
    var lastG = null;
    vis.forEach(function (row) {
      var t = rowTarget(row);
      if (grouped && viewGroup(t) !== lastG) {
        lastG = viewGroup(t);
        var n = rowsByGroup(lastG).length;
        host.append(el('div', 'grp', groupName(lastG) + ' · ' + n));
      }
      host.append(auditRow(row, t));
    });
    var on = host.querySelector('.row.on');
    if (on) revealRow(on);
    if (nf) host.append(nf);
  }

  function auditRow(row, t) {
    var b = btn('row' + (row.id === audit.selected ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || '(no title)'));
    l1.append(el('span', 'dt', shownDateText(t, row)));
    var wf = supportTag(row.id);   // CVA: visible without opening the row
    if (wf) l1.append(wf);
    var l2 = el('div', 'l2');
    var x = t.decided_by_person ? null : exclusionFor(row.id);
    if (projected(t)) {
      // GROUP-AXIS-001: the review projection owns the badge when sent
      // ('Excluded', 'Corrected, awaiting your review', …)
      l2.append(rowBadge(row, t));
    } else if (partialProjection(t)) {
      // H3/H5: a partial projection is needs review — never the group's
      // 'Fixed by you' / 'Removed by code' badge
      l2.append(rowBadge(row, t));
    } else if (x && x.by === 'code') {
      // G1-8 / G1-S1: a row removed by code says so — never a matched/proposed badge
      l2.append(el('span', 'st st-removed', 'Removed by code'));
    } else {
      // G1-18: a corrected row says who caught the mistake, in place of MATCHED
      if (t.group === 'corrected') {
        var mm = mistakeFor(row.id);
        l2.append(el('span', 'st st-corrected',
          !mm || mm.caught_by === 'person' ? 'Fixed by you'
          : mm.caught_by === 'code' ? 'Caught by code'
          : 'Caught by the second reader'));
      } else {
        l2.append(rowBadge(row, t));
      }
    }
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    if (x && x.by === 'code') {
      var ruleWords = RULE_WORDS[x.rule] || String(x.rule || '').replace(/_/g, ' ');
      l2.append(el('span', 'fixline', '· ' + ruleWords));
    }
    // U37: a row the person kept over a code removal says so — 'Kept by you ·
    // code had removed it' — in place of the generic 'decided by you'
    var keptAnyway = t.group === 'kept' && t.decided_by_person &&
                     t.last_action === 'keep' && codeExclusionHistory(row.id);
    if (keptAnyway) {
      l2.append(el('span', 'fixline', '· code had removed it'));
    } else if (decidedMark(t)) {
      l2.append(el('span', null, '· decided by you'));
    }
    if (t.group === 'corrected') {
      // G1-18: the before → after — a person's fix carries it; a correction
      // that came with the run keeps the old value in the mistake's record
      var cfx = (t.fixes || []).slice(-1)[0] || null;
      if (!cfx) {
        var cfm = mistakeFor(row.id);
        var cmm = cfm ? /^([A-Za-z ]+?) changed from (.*?) to /.exec(cfm.what_changed || '') : null;
        var cfield = cmm ? ({ 'date': 'date', 'company': 'company', 'company and ticker': 'company',
                              'event type': 'event_type', 'title': 'title', 'program': 'program',
                              'indication': 'indication' })[cmm[1].toLowerCase()] : null;
        if (cfield) {
          var to = t.values && t.values[cfield] !== undefined ? t.values[cfield] : null;
          // the combined label names the ticker too — the after carries it
          if (cmm[1].toLowerCase() === 'company and ticker' && to &&
              t.values && t.values.ticker)
            to = to + ' (' + t.values.ticker + ')';
          cfx = { field: cfield, from: cmm[2], to: to };
        }
      }
      if (cfx) {
        function calWords(v) {
          if (v === null || v === undefined) return '(none)';
          if (cfx.field === 'event_type') return eventWord(v);
          if ((cfx.field === 'date' || cfx.field === 'date_end') && /^\d{4}-\d{2}-\d{2}$/.test(String(v)))
            return dayInWords(v);
          return String(v);
        }
        l2.append(el('span', 'fixline', calWords(cfx.from) + ' → ' + calWords(cfx.to)));
      }
    }
    if ((t.group === 'open' || t.group === 'unverified') && t.reason) {
      l2.append(el('span', 'fixline', t.reason.length > 72 ? t.reason.slice(0, 72) + '…' : t.reason));
    }
    b.append(l1, l2);
    b.addEventListener('click', function () {
      audit.selected = row.id;
      audit.cleared = false;
      navigate({ runId: currentRunId(), screen: 'audit', rowId: row.id, filter: audit.filter });
    });
    return b;
  }

  function pageFor(row) {
    var s = sourceOf(row);
    if (!s) return null;
    var p = (s.pages || []).filter(function (x) { return x.page_number === row.page_number; })[0];
    return p ? { source: s, page: p } : null;
  }

  function renderAuditMid() {
    var host = clear(document.getElementById('audit-mid'));
    var row = rowOf(audit.selected);
    if (!row) {
      host.append(el('div', 'empty', 'Nothing selected.'));
      return;
    }
    var pp = pageFor(row);
    if (!pp) {
      host.append(el('div', 'empty', 'The page this row cites was not captured.'));
      return;
    }
    host.append(srcmetaBar(pp.source));
    var wrap = pageImage(pp, row.id);
    host.append(wrap);
    var go = btn('btn', 'Open in Source view', function () {
      navigate({ runId: currentRunId(), screen: 'source', srcId: pp.source.id,
                 page: 'p' + pp.page.page_number, valueId: row.id, back: 'audit' });
    });
    go.style.margin = '14px 26px';
    go.style.alignSelf = 'flex-start';
    host.append(go);
  }

  function srcmetaBar(source) {
    var meta = el('div', 'srcmeta');
    function kv(k, v, cls) {
      var s = el('span'); s.append(el('b', null, k + ' '), el('span', cls || '', esc(v))); meta.append(s);
    }
    kv('Publisher', source.publisher);
    kv('Kind', source.kind);
    kv('Published', source.published_date);
    kv('Captured', (source.captured_at || '').replace('T', ' ').replace('+00:00', ' UTC'));
    kv('SHA-256', (source.sha256 || '').slice(0, 16) + '…');
    if (source.rendered_from) {
      var s = el('span', 'rend');
      s.append(el('b', null, 'Page rendered by code from the filed HTML '),
        '· SHA-256 ' + source.rendered_from.sha256.slice(0, 16) + '…');
      meta.append(s);
    }
    // RESEARCHER-FULL-001 S1: a source fetched by the agent's tools — where it
    // came from, when, and the hash of the exact bytes received
    var rt = source.retrieval;
    if (rt) {
      kv('URL', rt.final_url || rt.requested_url || '—');
      if (rt.retrieved_at) kv('Retrieved', dateTimeWords(rt.retrieved_at));
      if (rt.original_sha256) kv('Original SHA-256', rt.original_sha256.slice(0, 16) + '…');
      if (rt.pages_captured !== null && rt.pages_captured !== undefined &&
          rt.pages_total !== null && rt.pages_total !== undefined)
        kv('Pages captured', rt.pages_captured + ' of ' + rt.pages_total);
    }
    return meta;
  }

  function pageImage(pp, highlightId) {
    var wrap = el('div', 'page-wrap');
    var visualEvidence = pp.page.visual_evidence || {};
    if (visualEvidence.overlays_supported === false) {
      var why = visualEvidence.origin === 'hidden_text'
        ? 'Hidden text layer: citation overlays are unavailable because its word boxes are nonvisual slivers.'
        : visualEvidence.origin === 'approximate'
          ? 'Approximate image geometry: citation overlays are unavailable because SAM or model transcription did not verify visible words.'
          : 'No visible text geometry is available for citation overlays.';
      wrap.append(el('div', 'hint', why));
      highlightId = null;
    }
    // G1-14: boxes live in an inner container that exactly frames the image —
    // percentages of this container are the page points scaled to the rendered
    // image, with no padding offset
    var inner = el('div', 'page-img');
    var img = el('img');
    img.alt = pp.source.title + ' · page ' + pp.page.page_number;
    img.src = '/runs/' + encodeURIComponent(currentRunId()) + '/' +
      pp.page.image.split('/').map(encodeURIComponent).join('/');
    inner.append(img);
    wrap.append(inner);
    var W = pp.page.width_pt, H = pp.page.height_pt;
    (visualEvidence.overlays_supported === false ? [] : valuesOnPage(pp)).forEach(function (v) {
      if (!Array.isArray(v.box) || v.box.length !== 4 ||
          v.box.some(function (n) { return typeof n !== 'number'; })) return;
      var b = el('div', 'box ' + v.status + (v.id === highlightId ? ' sel' : ''));
      b.title = v.label;
      b.style.left = (v.box[0] / W * 100) + '%';
      b.style.top = (v.box[1] / H * 100) + '%';
      b.style.width = (Math.max(0, v.box[2] - v.box[0]) / W * 100) + '%';
      // G1-R7: the bottom edge sits 2 pt lower so descenders are never clipped
      b.style.height = (Math.max(0, v.box[3] + 2 - v.box[1]) / H * 100) + '%';
      b.addEventListener('click', function () {
        navigate({ runId: currentRunId(), screen: 'source', srcId: pp.source.id,
                   page: 'p' + pp.page.page_number, valueId: v.id, back: route.screen === 'audit' ? 'audit' : null });
      });
      inner.append(b);
    });
    return wrap;
  }

  function valuesOnPage(pp) {
    var key = pp.source.id + ':' + pp.page.page_number;
    var out = [];
    (detail.run.tables || []).forEach(function (t) {
      if (t.source_id !== pp.source.id || t.page_number !== pp.page.page_number) return;
      (t.rows || []).forEach(function (r) {
        (r.cells || []).forEach(function (c) {
          out.push({ id: c.id, label: r.label + ' · ' + c.column, box: c.box, status: c.status });
        });
      });
    });
    detail.run.rows.forEach(function (r) {
      if (r.source_id + ':' + r.page_number === key) {
        out.push({ id: r.id, label: r.title, box: r.box, status: r.status });
      }
    });
    return out;
  }

  // ---------------- LETTUCE-OPTION (quiet advisory) ----------------
  // A finding is optional evidence. The claim/evidence pane adds understated
  // 'Potentially unsupported wording' with the exact suspect text ONLY while
  // the served support_findings record is a current unsupported_spans result;
  // stale, no_finding and could_not_run add nothing there (never a pass).
  function supportFindingFor(findings, rowId) {
    var f = (findings || {})[rowId];
    if (f && f.result === 'unsupported_spans' && f.spans && f.spans.length) return f;
    return null;
  }
  // CVA: a current hallucination finding is visible WITHOUT opening the row —
  // a small neutral tag in the row lists and on the calendar row. Never a
  // status, never in a count, never moves the row; stale, no-finding and
  // could-not-run rows show no tag. The pane note stays as it is.
  function supportTag(rowId) {
    if (!supportFindingFor((detail.run || {}).support_findings, rowId)) return null;
    var tag = el('span', 'wflag', 'Wording flag (advisory)');
    tag.title = 'The hallucination check thinks some wording may not be supported by the cited page. Open the row to see it.';
    return tag;
  }
  function supportNoteText(f, src, row) {
    var words = f.spans.map(function (s) { return '“' + s.text + '”'; });
    return words.join(' · ') + ' — may not be supported by the cited source' +
      (src ? ' (' + src.publisher + ' · page ' + row.page_number + ')' : '') +
      '. Advisory only; it changes no check, count or decision.';
  }
  // Trail attribution: the run-level research steps of the check become one
  // display step each — 'LettuceDetect' and the run status live ONLY here,
  // never as a badge, banner or score. The served finding's own reason is the
  // source of truth for a not-run step; the step prose is the fallback.
  function supportTrailSteps(steps, rowId, findings) {
    return (steps || []).filter(function (s) {
      return s && s.who === 'capture.support_signal' && s.refs && s.refs.row_id === rowId;
    }).map(function (s) {
      return { step: 'Support check', tag: 'LettuceDetect', who: 'LettuceDetect',
               at: s.at, text: supportStepText(s, (findings || {})[rowId]) };
    });
  }
  function supportStepText(s, finding) {
    var result = s.refs && s.refs.result;
    if (result === 'could_not_run') {
      var why = finding && finding.result === 'could_not_run' && finding.reason ? finding.reason : '';
      if (!why) {
        var m = /could not run \((.*)\)\.?$/.exec(s.text || '');
        why = m ? m[1] : '';
      }
      return 'Not run' + (why ? ': ' + why : '') + '.';
    }
    if (result === 'no_finding') return 'No finding — no wording was flagged. This is not a statement that the claim is supported.';
    // a changed claim or source hides old spans immediately: only a CURRENT
    // finding reads as a finding; anything else is earlier-check or unknown
    if (result === 'unsupported_spans') {
      if (finding && finding.result === 'unsupported_spans') return 'Potentially unsupported wording in the proposed title.';
      if (finding) return 'Earlier check (no longer current): the claim or its cited source changed after it ran.';
    }
    return 'Result not recorded.';
  }

  // details pane (shared by audit + judgment)
  function detailsPane(host, row, opts) {
    opts = opts || {};
    clear(host);
    // the research label and Context section lead the pane — after the clear
    var rsLabel = researchLabelLine(row);
    if (rsLabel) host.append(rsLabel);
    var rsCtx = researchContextSection(row);
    if (rsCtx) host.append(rsCtx);
    var t = rowTarget(row);
    var src = sourceOf(row);

    // A-F7: a row a person kept while a check failed or the readers disagree
    // leads with that — before anything else in the pane (a superseded Keep
    // is history, never 'Kept by you')
    if (keptCurrent(t)) {
      var af7fails = effectiveChecks(row, t).filter(function (c) { return c.result === 'fail'; });
      var af7dis = t.disagreement && t.disagreement.second_reader !== null &&
                   t.disagreement.second_reader !== undefined;
      if (af7fails.length || af7dis) {
        var warn = el('div', 'ba amber');
        if (af7fails.length) {
          warn.append(el('div', null, 'Kept by you, but the check “' + af7fails[0].label +
            '” failed' + (af7fails.length > 1 ? ' (+' + (af7fails.length - 1) + ' more)' : '') +
            ' — ' + (af7fails[0].reason || 'see the checks below.')));
        }
        if (af7dis) {
          var fld = FIELD_WORDS[t.disagreement.field] || t.disagreement.field;
          warn.append(el('div', null, 'Kept by you, and ' + bothReadersWord() + ' still disagree on the ' +
            String(fld).toLowerCase() + '.'));
        }
        host.append(warn);
      }
    }

    var sec1 = el('div', 'sec');
    if (t.decided_by_person) sec1.append(decidedBadge(t));
    var ttl = el('div', 'ttl', shownField(t, row, 'title') || '(no title)');
    ttl.style.marginTop = t.decided_by_person ? '8px' : '0';
    sec1.append(ttl);
    host.append(sec1);

    var sec2 = el('div', 'sec');
    function kv(k, v, node) {
      var r = el('div', 'kv');
      r.append(el('span', 'k', k));
      r.append(node || el('span', 'v', v));
      sec2.append(r);
    }
    // the pane wears the same current values as the calendar row (Codex item 1)
    kv('Company', (shownField(t, row, 'ticker') || '') + ' · ' + (shownField(t, row, 'company') || ''));
    var paneProgram = shownField(t, row, 'program');
    if (paneProgram) kv('Program', paneProgram);
    // #52 type provenance: the server's label:value pairs when it sends them
    var typePairs = typePresentationPairs(t);
    if (typePairs) {
      // glass adviser (4): plain words in the pane — the legacy rule's
      // internal label reads 'Category', and an empty value drops its line
      // instead of printing 'Not supplied'
      typePairs.forEach(function (pr) {
        if (pr[1] === 'Not supplied') return;
        kv(pr[0] === 'Category (from old keyword rule)' ? 'Category' : pr[0], pr[1]);
      });
    } else {
      kv('Type', eventWord(shownField(t, row, 'event_type')));
    }
    kv('Event date', '', el('span', 'v mono', shownDateText(t, row) ||
      (projected(t) || partialProjection(t) ? 'Not stated' : '')));
    var timingWords = { undated: 'Undated · no timing stated', timing_unresolved: 'Timing unresolved', dated: 'Timing stated' };
    kv('Timing', (timingWords[t.timing_state] || 'Timing unresolved') +
      (t.contingent ? ' · Contingent (explicit dependency)' : ''));
    kv('Proposed by', 'Model · ' + (row.proposed_by || ''));
    if (src) {
      var a = el('a', 'link', src.publisher);
      a.href = '#';
      a.addEventListener('click', function (ev) {
        ev.preventDefault();
        var pp = pageFor(row);
        // G3-6: the origin screen (and its window selection) rides along
        if (pp) navigate({ runId: currentRunId(), screen: 'source', srcId: src.id,
                           page: 'p' + pp.page.page_number, valueId: row.id,
                           back: opts.back || 'audit', w: opts.w || null });
      });
      kv('Source ', '', a);
      if (src.rendered_from) {
        sec2.append(el('div', 'rendnote',
          'Page rendered by code from the filed HTML · SHA-256 ' +
          String(src.rendered_from.sha256 || '').slice(0, 16) + '…'));
      }
    }
    host.append(sec2);

    if (t.values && t.values.quote) {
      var sq = el('div', 'sec');
      sq.append(el('h4', null, 'The quote'));
      var qb = el('div', 'qbox');
      if (src) qb.append(el('div', 'qm', 'from ' + src.publisher + ' · page ' + row.page_number));
      qb.append('“' + t.values.quote + '”');
      sq.append(qb);
      host.append(sq);
    }

    // LETTUCE-OPTION: understated advisory note beside the claim/evidence —
    // a current finding only; stale, no finding and not-run stay out of the pane
    var sf = supportFindingFor(((detail.run || {})).support_findings, row.id);
    if (sf) {
      var sn = el('div', 'supportnote');
      sn.append(el('div', 'k', 'Potentially unsupported wording'));
      sn.append(el('div', null, supportNoteText(sf, src, row)));
      host.append(sn);
    }

    // PROJECTION-001: the review projection's current issues are the reason
    // this row asks for a person — say them where the decision happens, not
    // only in the research trail
    if (t.issues && t.issues.length) {
      var isec = el('div', 'sec');
      isec.append(el('h4', null, 'Needs review because'));
      var ibox = el('div', 'ba');
      t.issues.forEach(function (w) { ibox.append(el('div', null, dayInWords(w))); });
      isec.append(ibox);
      host.append(isec);
    }

    if (t.group === 'corrected' && !partialProjection(t)) host.append(correctedSection(row, t));
    if (t.group === 'removed') host.append(removedSection(row, t));
    if (t.group === 'open' || t.group === 'unverified') host.append(whySection(row, t));
    if (t.disagreement) host.append(disagreementSection(row, t));

    // A-F6: when a check did not apply, the pane says in one plain sentence
    // what the badge carries — the check lines below give the why
    if (t.status === 'matched' && !t.decided_by_person) {
      var unchecked2 = [];
      (row.checks || []).forEach(function (c) {
        if (c.result !== 'not_needed') return;
        if (c.name === 'date_match') unchecked2.push('the event date');
        if (c.name === 'type_match') unchecked2.push('the event type');
      });
      if (unchecked2.length) {
        var nn = el('div', 'hint');
        nn.textContent = 'Not confirmed by a check: ' + unchecked2.join(' and ') +
          '. The check lines below say why.';
        host.append(nn);
      }
    }

    host.append(checksSection(row, t));

    // G5-7: when the second reader proposed nothing, say so once, under the checks
    if (t.disagreement && (t.disagreement.second_reader === null || t.disagreement.second_reader === undefined)) {
      var srhint = el('div', 'hint');
      srhint.style.marginTop = '6px';
      var srName = ((detail.run.run || {}).second_reader || {}).model || 'Muse Spark Open';
      srhint.textContent = srName + ' did not read this row.';
      host.append(srhint);
    }

    if (opts.actions) {
      var sa = el('div', 'sec');
      var br = el('div', 'btnrow');
      br.append(btn('btn', 'Open source page', function () {
        var pp = pageFor(row);
        if (pp) navigate({ runId: currentRunId(), screen: 'source', srcId: src.id,
                           page: 'p' + pp.page.page_number, valueId: row.id,
                           back: opts.back || 'audit', w: opts.w || null });
      }));
      br.append(btn('btn', 'Open agent trail', function () {
        navigate({ runId: currentRunId(), screen: 'trail', rowId: row.id });
      }));
      // G1-S1: a row removed by code is the code's call — no Decide button
      var xc = exclusionFor(row.id);
      var codeRemoved = xc && xc.by === 'code';
      if (!codeRemoved) {
        var dec = btn('btn pri', t.decided_by_person ? 'Change it' : 'Decide', function () {
        judgmentSel = row.id;
        navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
      });
        br.append(dec);
      }
      sa.append(br);
      host.append(sa);
    }
  }

  function correctedSection(row, t) {
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Corrected'));
    var box = el('div', 'ba');
    var fx = (t.fixes || []).slice(-1)[0];
    var m = mistakeFor(row.id);
    if (fx) {
      var line = el('div');
      line.append(esc(FIELD_WORDS[fx.field] || fx.field) + ': ');
      var s = el('s'); s.style.color = 'var(--sub)'; s.textContent = fx.from === null || fx.from === undefined ? '(none)' : fx.from;
      line.append(s, ' ', el('span', 'arr', '→'), ' ', el('b', null, fx.to === null || fx.to === undefined ? '(none)' : fx.to));
      box.append(line);
    }
    var who = el('div');
    who.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
    if (m) {
      who.textContent = (m.caught_by === 'second_reader' ? 'Caught by the second reader. ' : 'Caught by code. ') + m.what_changed;
    } else if (fx && fx.mistake) {
      who.textContent = 'Caught by you. The fix passed its re-check and the old value failed it.';
    } else if (fx && fx.edit_without_error) {
      who.textContent = 'Recorded as an edit — ' + NO_ERROR + ' The mistake counter did not change.';
    } else if (fx && fx.before_undemonstrated) {
      who.textContent = fx.not_counted_reason || '';
    } else if (fx && fx.not_counted_reason) {
      who.textContent = fx.not_counted_reason;
    } else if (t.decided_by_person && fx) {
      who.textContent = 'Fixed by you.';
    }
    box.append(who);
    if (t.needs_recheck) {
      box.append(el('div', 'pendline', 'Re-check pending.'));
    } else if (t.recheck) {
      var rc = el('div', 'recline');
      rc.append('Re-checked after the fix: ');
      rc.append(el('b', null, checkResultWord(t.recheck.result)));
      if (t.recheck.reason) rc.append(' — ' + t.recheck.reason);
      box.append(rc);
    }
    sec.append(box);
    return sec;
  }

  function removedSection(row, t) {
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Why it was removed'));
    var x = exclusionFor(row.id);
    var box = el('div', 'ba red');
    var reasonLine = el('div');
    reasonLine.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
    if (x && x.by === 'code') {
      // G1-8 / G1-S1: say who removed it and why, in plain words
      box.append(el('b', null, 'Removed by code'));
      // G1-R4: dates in the sentence read in words, and a missing reason never
      // prints a line (no literal "null" under the box)
      var why = x.reason || RULE_WORDS[x.rule] || '';
      if (why) {
        reasonLine.textContent = dayInWords(why);
        box.append(reasonLine);
      }
      // U37: the code's call can be overruled — a person keeps the row anyway
      // (a recorded keep decision; the exclusion stays in run.json as history)
      var kbr = el('div', 'btnrow');
      kbr.style.marginTop = '10px';
      kbr.append(btn('btn pri', 'Keep anyway', function () {
        navigate({ runId: currentRunId(), screen: 'audit', rowId: row.id, dialog: 'keepanyway' });
      }));
      box.append(kbr);
      sec.append(box);
      return sec;
    }
    var rule = x ? (RULE_WORDS[x.rule] || String(x.rule || '').replace(/_/g, ' ')) : 'Removed.';
    box.append(el('b', null, rule));
    var reason2 = (x && x.reason) || t.reason || '';
    if (reason2) {
      reasonLine.textContent = dayInWords(reason2);
      box.append(reasonLine);
    }
    sec.append(box);
    return sec;
  }

  // G1-R4: an ISO day inside a reason sentence reads in words
  function dayInWords(s) {
    return String(s).replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, function (_, y, mo, d) {
      return SAY_MONTHS[parseInt(mo, 10) - 1] + ' ' + parseInt(d, 10) + ', ' + y;
    });
  }

  function whySection(row, t) {
    var sec = el('div', 'sec');
    sec.append(el('h4', null, t.group === 'open' ? 'Why it needs you' : 'Why it is unverified'));
    var box = el('div', 'ba' + (t.group === 'open' ? ' blue' : ''));
    // G5-5: the disagreement is stated once on the screen — in the block below
    // the checks' pane, not again here
    if (t.disagreement && t.disagreement.second_reader) {
      var fieldWord = (FIELD_WORDS[t.disagreement.field] || t.disagreement.field).toLowerCase();
      box.textContent = bothReadersWord() + ' disagree on the ' + fieldWord + '. A person decides.';
    } else {
      // A-C5: the shown reason is the state's current reason — never a reason
      // stored on the raw row from before the state rule re-derived it
      var shown = t.reason || 'Waiting for you.';
      // U38: when a failed check carries a near-miss sentence, a generic
      // "not printed on the page" reason must never be shown instead
      var near = (row.checks || []).filter(function (c) {
        return c.result === 'fail' && c.reason && c.reason.indexOf('nearly matches') >= 0;
      })[0];
      if (near && /not printed/i.test(shown)) shown = near.reason;
      box.textContent = shown;
    }
    sec.append(box);
    return sec;
  }

  // the second reader (R5): the field the two readers disagree on, both
  // values, above the checks
  function disagreementSection(row, t) {
    var d = t.disagreement;
    // G5-7: a second reader that proposed nothing is not a disagreement — no block
    if (!d || d.second_reader === null || d.second_reader === undefined) return null;
    var sec = el('div', 'sec');
    var fieldWord = FIELD_WORDS[d.field] || d.field;
    sec.append(el('h4', null, bothReadersWord() + ' disagree on the ' + fieldWord.toLowerCase()));
    var box = el('div', 'ba blue');
    function val(v) { return d.field === 'event_type' ? eventWord(v) : fmtField(d.field, v); }
    var l1 = el('div');
    l1.append(el('b', null, 'First reader · ' + (row.proposed_by || 'Muse Spark') + ': '), val(d.first_reader));
    var l2 = el('div');
    l2.style.marginTop = '4px';
    var srL = ((detail.run.run || {}).second_reader || {}).model || 'Muse Spark Open';
    l2.append(el('b', null, 'Second reader · ' + srL + ': '), val(d.second_reader));
    box.append(l1, l2);
    sec.append(box);
    return sec;
  }

  function checksSection(row, t) {
    var sec = el('div', 'sec');
    var hasFix = (t.fixes || []).length > 0;
    var current = effectiveChecks(row, t);
    var history = t.check_history || [];
    var visible = (t.claim_assessments || {}).visible_passage;
    var evidence = t.page_evidence || {};
    var capture = evidence.capture_completeness || {};
    if (capture.capture_incomplete) {
      // detected clipping stays an actionable warning, with the original source
      var warn = el('div', 'capwarn');
      warn.append('The captured page may be cut off. ');
      var osrc = row && row.source_id ? sourceOf(row) : null;
      // fix-on-find (Red Team 7f822fc): the link only when there is an
      // original to open — never an anchor without an href
      if (osrc && osrc.url) {
        var oa = el('a', 'link', 'Open the original to check.');
        oa.href = osrc.url; oa.target = '_blank'; oa.rel = 'noopener';
        warn.append(oa);
      }
      if (capture.capture_incomplete_reason)
        warn.append(el('span', 'capwhy', ' ' + capture.capture_incomplete_reason));
      sec.append(warn);
    } else if (capture.completeness_not_established) {
      // unknown completeness is neutral detail, never a warning on every row
      var cdet = el('details', 'fold capdet');
      cdet.dataset.check = 'capture';
      cdet.append(el('summary', null, 'Capture details'));
      cdet.append(el('div', null, 'This check cannot rule out every kind of clipping.'));
      if (capture.completeness_reason) cdet.append(el('div', 'hint', capture.completeness_reason));
      sec.append(cdet);
    }
    if (evidence.derived_words) {
      sec.append(el('div', 'hint', 'Words came from a model transcription of the captured page image; their geometry is approximate.'));
    }

    function appendChecks(label, checks, emptyText, isHistory) {
      sec.append(el('h4', null, label));
      if (!checks.length) {
        sec.append(el('div', null, emptyText));
        return;
      }
      checks.forEach(function (c) {
      var line = el('div', 'chk');
      var finding = dateFinding(c);
      // history lines never wear the current outcome or a green dot — a pass
      // that was true then reads neutral ('passed before your edit'), a
      // conflict stays red
      var outcome = isHistory ? null : passOutcome(c);
      line.append(outcome ? checkDot('pass')
        : isHistory && c.result === 'pass' ? el('span', 'dot history')
        : finding ? el('span', 'dot finding unresolved')
        : checkDot(c.result));
      var cn = el('span', 'cn');
      cn.append(esc(c.label), el('span', 'wtag', 'Code'));
      line.append(cn);
      var cr = el('span', 'cr', outcome ? outcome
        : isHistory && c.result === 'pass' ? 'passed before your edit'
        : finding ? '' : checkResultWord(c.result));
      line.append(cr);
      sec.append(line);
      var reasonText = timingReason(c);
      if (reasonText) {
        var r = el('div');
        r.style.cssText = 'font-size:12px;color:var(--sub);padding:0 0 2px 17px';
        r.textContent = reasonText;
        sec.append(r);
      }
      sec.append(seeTheCheck(c));
      });
    }

    appendChecks(hasFix ? 'Checks on current value · by Code' : 'Checks · by Code', current,
      t.assessment_status === 'unresolved' ? (t.checks_stale
        ? 'Saved checks are not current for this candidate or page.'
        : 'No check establishes that the claimed words are visible on the rendered page.')
        : t.needs_recheck ? 'No checks ran on the current value yet; re-check pending.'
        : (t.recheck && (t.recheck.result === 'could_not_run' || t.recheck.result === 'unresolved')
          ? (t.recheck.result === 'unresolved' ? 'The re-check is unresolved; no checks are recorded for the current value.'
            : 'The re-check could not run; no checks are recorded for the current value.')
          : 'No checks ran for this row.'));
    if (visible) {
      var vline = el('div', 'chk');
      vline.append(checkDot(visible.result));
      vline.append(el('span', 'cn', visible.label), el('span', 'cr', 'not checked'));
      sec.append(vline, el('div', 'hint', visible.reason));
    }
    // glass adviser (4): the 'before edit' history belongs to actual edits —
    // a never-edited row never shows an 'ORIGINAL CHECKS · HISTORY' block
    if (history.length && (hasFix || t.checks_stale)) {
      appendChecks(t.checks_stale ? 'Saved checks · history (not current)' : 'Original checks · history (before edit)', history,
        'No original checks were recorded.', true);
    }
    return sec;
  }

  function seeTheCheck(c) {
    // G8-6: the fold renders closed by default on every screen; the only
    // opener is the person (or the trail's 'See the check' link) — the code
    // block is never shown uninvited
    var d = el('details', 'fold');
    // CODEXADV_KIMI_DIALOG_REFRESH: the fold carries its check's name so a
    // same-target background refresh can re-open the same check
    if (c.name) d.dataset.check = c.name;
    d.append(el('summary', null, 'See the check'));
    var cmp = c.compared || {};
    var box = el('div', 'cmp');
    box.style.marginTop = '6px';
    box.append(el('span', 'lf', 'looked for '), esc(cmp.looked_for));
    if ('found' in cmp) {
      box.append(el('span', 'lf', ' · found '),
        esc(cmp.found === null || cmp.found === undefined || cmp.found === '' ? 'nothing' : cmp.found));
    }
    if (c.near_match) box.append(el('span', 'lf', ' · page wording '), esc(c.near_match));
    d.append(box);
    var res = el('div', 'recline');
    if (dateFinding(c)) {
      var tr = timingReason(c);
      if (tr) res.append(tr);
    } else {
      res.append('Result: ', el('b', null, checkResultWord(c.result)));
      if (c.reason) res.append(' — ' + c.reason);
    }
    d.append(res);
    // G1-1: the code that ran, folded — no file paths or function names on screen.
    // Job 21 serves it at /api/checks/source; until then (or when the run names no
    // code reference) the fold says the code is not served on this build, never a
    // dead link.
    var src = el('div', 'codesrc-host');
    d.append(src);
    d.addEventListener('toggle', function () {
      if (!d.open || src.dataset.probed) return;
      src.dataset.probed = '1';
      var notServed = function () {
        src.textContent = 'The check’s code is not served on this build.';
      };
      if (!(c.code_ref && c.code_ref.file)) { notServed(); return; }
      fetch('/api/checks/source?ref=' + encodeURIComponent(c.code_ref.file + ':' + (c.code_ref.function || '')))
        .then(function (r) {
          if (!r.ok) { notServed(); return; }
          // G1-R1: the route answers JSON {ref, source} — the fold shows the
          // source only, as real code, never the raw record or the ref path
          return r.json().then(function (rec) {
            var text = rec && rec.source;
            if (!text || !String(text).trim()) { notServed(); return; }
            src.textContent = '';
            src.append(el('div', 'codelbl', 'The code that ran'));
            var pre = el('pre', 'codesrc');
            pre.textContent = String(text);
            src.append(pre);
          });
        }).catch(notServed);
    });
    return d;
  }

  function renderAuditDet() {
    // the poll rebuilds this pane: opened folds and its scroll survive a
    // same-row refresh
    captureFolds(document.getElementById('audit-det'));
    var host = clear(document.getElementById('audit-det'));
    var row = rowOf(audit.selected);
    if (!row) {
      host.append(el('div', 'empty', 'Nothing selected.'));
      return;
    }
    detailsPane(host, row, { actions: true, back: 'audit' });
  }

  // ---------------------------------------------------------------- source (U1)
  // CODEXADV_KIMI_DIALOG_REFRESH: across a same-target background refresh,
  // opened folds and the pane scroll survive in every pane the poll rebuilds;
  // a changed target (or a deliberate navigation) resets them. A fold's
  // identity is its position among the pane's folds plus its check name — a
  // name alone re-opens the wrong fold when a check appears in two sections.
  function srcViewRouteKey() {
    return currentRunId() + '/' + route.srcId + '/' + route.page + '/' + (route.valueId || '');
  }
  // fix-on-find (Red Team 7f822fc): the keep is keyed by what the pane SHOWED
  // (hostEl.__foldKey, set at restore), not by the route at capture time —
  // render() updates the route before the pane is rebuilt, so a route key
  // there always equals the new key and copied the old folds onto a new row
  // or value. The keep lives on its own host, so panes never clobber each other.
  function captureFolds(hostEl) {
    var opens = [];
    if (hostEl.querySelectorAll) {
      var folds = hostEl.querySelectorAll('details.fold[data-check][open]');
      for (var i = 0; i < folds.length; i++) {
        // the index among ALL the pane's folds, with the name as a guard
        var all = hostEl.querySelectorAll('details.fold[data-check]');
        for (var j = 0; j < all.length; j++) if (all[j] === folds[i]) { opens.push(j + ':' + folds[i].getAttribute('data-check')); break; }
      }
    }
    hostEl.__foldKeep = { key: hostEl.__foldKey, opens: opens, scroll: hostEl.scrollTop };
  }
  function restoreFolds(hostEl, key) {
    var keep = hostEl.__foldKeep;
    hostEl.__foldKeep = null;
    hostEl.__foldKey = key;
    if (!keep || keep.key !== key || !hostEl.querySelectorAll) return;
    var all = hostEl.querySelectorAll('details.fold[data-check]');
    keep.opens.forEach(function (k) {
      var parts = k.split(':');
      var d = all[parseInt(parts[0], 10)];
      if (d && d.getAttribute('data-check') === parts.slice(1).join(':')) d.open = true;
    });
    hostEl.scrollTop = keep.scroll;
  }
  function captureSrcView() {
    captureFolds(document.getElementById('srcDet'));
  }
  function restoreSrcView() {
    restoreFolds(document.getElementById('srcDet'), srcViewRouteKey());
  }
  function renderSource() {
    captureSrcView();
    var host = clear(document.getElementById('srcList'));
    var mid = clear(document.getElementById('srcwrap'));
    var det = clear(document.getElementById('srcDet'));
    // G8-9a: the right pane is collapsed until a value is picked; only then
    // does it open (see below where it is unhidden)
    det.hidden = true;
    // G3-7: the sub-line is one plain sentence
    document.getElementById('src-sub').textContent = 'Every page the rows were read from';

    var pages = [];
    var pageOutcomeView = ((detail.state || {}).page_outcomes || {});
    var pageOutcomeByKey = {};
    (pageOutcomeView.items || []).forEach(function (outcome) {
      pageOutcomeByKey[outcome.source_id + ':' + outcome.page_number] = outcome;
    });
    detail.run.sources.forEach(function (s) {
      (s.pages || []).forEach(function (p) { pages.push({ source: s, page: p }); });
    });
    document.getElementById('src-sub').textContent =
      'Every page the rows were read from · ' + pages.length + ' pages';
    clear(host);

    var selected = null;
    pages.forEach(function (pp) {
      if (pp.source.id === route.srcId && 'p' + pp.page.page_number === route.page) selected = pp;
    });

    // G8-9b: the page list groups pages under their filing — one header per
    // document ('IONS 8-K exhibit 99.1 · filed 23 Oct 2026 · 3 pages'), pages
    // as 'page 1 … page N'. A filing is one accession number; the open page's
    // filing is unfolded and highlighted, the others folded.
    function filingName(s) {
      // 'IONS 8-K exhibit 99.1 (exhibit 99.1), filed 2026-10-23' -> 'IONS 8-K exhibit 99.1'
      var t = (s.title || s.id || '').replace(/, filed \d{4}-\d{2}-\d{2}$/, '');
      var m = /^(.*) \(([^()]*)\)$/.exec(t);
      if (m && m[1].slice(-m[2].length) === m[2]) t = m[1];
      return t;
    }
    function accession(s) {
      var m = /\/data\/\d+\/(\d+)\//.exec(s.url || '') ||
              /^src-.*?-(\d{10}-\d{2}-\d{6})-/.exec(s.id || '');
      return m ? m[1] : s.id;
    }
    var filings = [];
    pages.forEach(function (pp) {
      var key = accession(pp.source);
      var f = filings.filter(function (x) { return x.key === key; })[0];
      if (!f) {
        f = { key: key, name: filingName(pp.source), filed: pp.source.published_date, pages: [] };
        filings.push(f);
      }
      f.pages.push(pp);
    });
    filings.forEach(function (f) {
      var isOpen = selected && f.pages.indexOf(selected) >= 0;
      var fd = el('details', 'filing' + (isOpen ? ' on' : ''));
      if (isOpen) fd.open = true;
      fd.append(el('summary', null, f.name + ' · filed ' + (dateShort(f.filed) || 'no date') +
        ' · ' + f.pages.length + ' page' + (f.pages.length === 1 ? '' : 's')));
      f.pages.forEach(function (pp) {
        var key = 'p' + pp.page.page_number;
        var b = btn('pg' + (selected && pp === selected ? ' on' : ''), '');
        b.append(el('span', 'c', 'page ' + pp.page.page_number));
        var outcome = pageOutcomeByKey[pp.source.id + ':' + pp.page.page_number];
        var outcomeLabel = !outcome ? 'outcome unavailable' : outcome.read ? 'read'
          : outcome.skipped ? 'skipped' : outcome.unreadable ? 'unreadable' : 'not read';
        b.append(el('span', 'c', outcomeLabel));
        b.addEventListener('click', function () {
          navigate({ runId: currentRunId(), screen: 'source', srcId: pp.source.id, page: key,
                     valueId: null, back: route.back });
        });
        fd.append(b);
      });
      host.append(fd);
    });

    if (!selected) {
      mid.append(el('div', 'empty', 'Choose a page on the left.'));
      restoreSrcView();
      return;
    }

    // G3-7: the page list follows the open page — highlighted and in view
    var onPg = host.querySelector('.pg.on');
    if (onPg) revealRow(onPg);

    mid.append(srcmetaBar(selected.source));
    var wrap = pageImage(selected, route.valueId);
    mid.append(wrap);
    var selBox = wrap.querySelector('.box.sel');
    if (selBox) revealRow(selBox, { behavior: 'smooth', block: 'center', inline: 'center' });

    var v = valuesOnPage(selected).filter(function (x) { return x.id === route.valueId; })[0];
    if (!v) { restoreSrcView(); return; }
    // G8-9a: a value is picked — the pane opens
    det.hidden = false;
    var row = rowOf(v.id);
    if (row) detailsPane(det, row, { back: route.back === 'audit' || route.back === 'calendar' ? route.back : null,
                                     w: route.w || null });
    else {
      // a table value: show its checks
      var cell = null, trow = null, table = null;
      (detail.run.tables || []).forEach(function (tb) {
        (tb.rows || []).forEach(function (r) {
          (r.cells || []).forEach(function (c) { if (c.id === v.id) { cell = c; trow = r; table = tb; } });
        });
      });
      if (cell) {
        var sec = el('div', 'sec');
        sec.append(el('h4', null, 'Table value'), el('div', 'ttl', trow.label + ' · ' + cell.column));
        det.append(sec);
        det.append(checksSection({ checks: cell.checks }, {}));
      }
    }
    restoreSrcView();
  }

  // ---------------------------------------------------------------- accuracy panel (U11)
  // Reached from the audit header: the mistake log by type x model x who
  // caught it, the sealed-set numbers per model, cost per kept row, and the
  // planted-errors line. Everything from state + runs/<id>/accuracy.json;
  // no model calls.
  var MISTAKE_TYPE_WORDS = {
    WRONG_VALUE: 'Value not as printed',
    WRONG_ROW: 'Value placed in the wrong row',
    WRONG_COLUMN: 'Value placed in the wrong column',
    BOX_DRIFT: 'Box does not sit on the value',
    QUOTE_NOT_ON_PAGE: 'Quote not printed on the page',
    DATE_NOT_IN_QUOTE: 'Date not part of the quote',
    PERIOD_MISREAD: 'Date or period read differently from the page',
    DATE_IN_PAST: 'Event date already past',
    WRONG_EVENT_TYPE: 'Wrong kind of event',
    WRONG_COMPANY: 'Wrong company',
    SOURCE_NOT_ALLOWED: 'Source not allowed on its own',
    DUPLICATE: 'Same item proposed twice',
    UNREADABLE_OUTPUT: 'Reader’s answer could not be read',
    MISSED_DATED_SENTENCE: 'A dated sentence the reader did not turn into a row'
  };
  var CAUGHT_BY_WORDS = { code: 'code', second_reader: 'the second reader', person: 'a person' };
  function mistakeTypeWord(t) { return MISTAKE_TYPE_WORDS[t] || 'No type recorded'; }

  // plain words for ids inside server sentences (what_changed lines); only
  // snake_case ids — a bare 'date' or 'title' in prose must not be replaced
  var ID_WORDS = {};
  Object.keys(EVENT_WORDS).forEach(function (k) { ID_WORDS[k] = EVENT_WORDS[k]; });
  Object.keys(FIELD_WORDS).forEach(function (k) { if (k.indexOf('_') >= 0) ID_WORDS[k] = FIELD_WORDS[k]; });
  function plainWords(text) {
    var out = String(text || '');
    Object.keys(ID_WORDS).sort(function (a, b) { return b.length - a.length; }).forEach(function (k) {
      out = out.replace(new RegExp('\\b' + k + '\\b', 'g'), ID_WORDS[k]);
    });
    return out;
  }

  var accCache = { id: null, acc: null, tried: false };
  function accuracyProbe(id) {
    if (accCache.id === id && accCache.tried) return Promise.resolve(accCache.acc);
    accCache = { id: id, acc: null, tried: true };
    var urls = ['/runs/' + encodeURIComponent(id) + '/accuracy.json',
                '/api/runs/' + encodeURIComponent(id) + '/accuracy'];
    return new Promise(function (resolve) {
      (function probe(i) {
        if (i >= urls.length) return resolve(null);
        fetch(urls[i]).then(function (r) {
          if (r.status === 409) {      // Job 50b: stale or unbound: the reason, never counts
            return r.json().then(function (b) {
              var refused = { refused: String((b && b.reason) || 'This Accuracy result does not match the run\'s current inputs. Re-score to show it.') };
              accCache = { id: id, acc: refused, tried: true };
              resolve(refused);
            }).catch(function () { probe(i + 1); });
          }
          if (!r.ok) return probe(i + 1);
          r.json().then(function (acc) {
            accCache = { id: id, acc: acc, tried: true };
            resolve(acc);
          }).catch(function () { probe(i + 1); });
        }).catch(function () { probe(i + 1); });
      })(0);
    });
  }
  function accPart(x) {
    if (!x || typeof x !== 'object') return null;
    var f = null, n = null;
    ['found', 'scored', 'caught'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) f = x[k]; });
    ['N', 'M', 'total'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) n = x[k]; });
    return n !== null ? (f !== null ? f : '?') + '/' + n : null;
  }
  // one line per model, one shape: 'Muse Spark — values 69/69 · events — · planted 12/12'
  // (G6-6: a figure the file does not hold prints '—', it is not dropped;
  // a model with nothing at all is skipped; G6-11b: the readers-together
  // bucket reads 'Both readers' — nested, the redteam harness reads this
  // function alone)
  function accModelLines(acc) {
    if (!acc) return [];
    function bucketName(name) {
      var first = (acc.readers && acc.readers.first) || [];
      var parts = String(name).split(',').map(function (s) { return s.trim(); });
      return parts.length >= 2 && parts.every(function (p) { return first.indexOf(p) >= 0; })
        ? 'Both readers' : name;
    }
    var lines = [];
    var models = acc.models;
    if (models && typeof models === 'object') {
      Object.keys(models).forEach(function (name) {
        var m = models[name] || {};
        var v = accPart(m.values), ev = accPart(m.events), pl = accPart(m.planted);
        if (!v && !ev && !pl) return;
        lines.push(bucketName(name) + ' — values ' + (v || '—') +
                   ' · events ' + (ev || '—') + ' · planted ' + (pl || '—'));
      });
    }
    if (!lines.length) {
      var b2 = [];
      var v2 = accPart(acc.values) ||
        (acc.table && acc.table.counts ? accPart({ found: acc.table.counts.found, N: acc.table.N }) : null);
      var e2 = accPart(acc.events) ||
        (acc.events && acc.events.counts ? accPart({ found: acc.events.counts.found, M: acc.events.M }) : null);
      var p2 = accPart(acc.planted);
      if (v2) b2.push('values ' + v2);
      if (e2) b2.push('events ' + e2);
      if (p2) b2.push('planted ' + p2);
      if (b2.length) lines.push(b2.join(' · '));
    }
    return lines;
  }
  function accPlantedLine(acc) {
    var pl = accPart(acc && acc.planted);
    return pl ? 'Planted errors caught: ' + pl : null;
  }

  var accPanelWrap = document.getElementById('accpanel-wrap');
  var accPanelHost = document.getElementById('accpanel');
  document.getElementById('accbtn').addEventListener('click', function () {
    renderAccuracyPanel();
    accPanelWrap.hidden = false;
  });
  accPanelWrap.addEventListener('click', function (e) { if (e.target === accPanelWrap) accPanelWrap.hidden = true; });

  function renderAccuracyPanel() {
    clear(accPanelHost);
    var meta = detail.run.run || {};
    var st = detail.state;
    accPanelHost.append(el('div', 'sh-t', 'Accuracy'));
    accPanelHost.append(el('div', 'sh-s', (meta.label || 'This run') + (meta.sample ? ' · sample data — not real events' : '')));

    var sealedHead = el('h4', null, 'The accuracy numbers');
    accPanelHost.append(sealedHead);
    var keyNoteHost = el('div');
    accPanelHost.append(keyNoteHost);
    var sealedHost = el('div', null, 'Reading the saved scoring…');
    accPanelHost.append(sealedHost);
    var plantedHost = el('div');
    accPanelHost.append(plantedHost);
    // G1-8: an author key is not called a sealed set; "the accuracy numbers" is the plain word
    var foot = el('div', 'hint', 'Every number here comes from the saved run and the accuracy numbers — no model is called for this panel.');
    foot.style.marginTop = '12px';
    accuracyProbe(detail.id).then(function (acc) {
      if (acc && acc.refused) {
        sealedHead.textContent = 'Scoring';
        clear(keyNoteHost); clear(sealedHost); clear(plantedHost);
        sealedHost.append(el('div', 'hint', acc.refused));
        return;
      }
      var sealed = acc && acc.key && acc.key.sealed === true;
      // G6-6: no key at all — no key heading either, just the plain Scoring
      // heading and one line; G1-8: the author-keyed heading in plain words
      sealedHead.textContent = !acc ? 'Scoring' : (sealed ? 'The sealed set' : 'Our own key, not sealed');
      foot.textContent = 'Every number here comes from the saved run and ' +
        (sealed ? 'its sealed-set file' : 'the accuracy numbers') + ' — no model is called for this panel.';
      // A-C6: under an unsealed key, one sentence says what such a key proves
      clear(keyNoteHost);
      if (acc && !sealed) {
        keyNoteHost.append(el('div', 'hint',
          'This key was written from the same page text, so it tests the checks, not the reader.'));
      }
      clear(sealedHost);
      var lines = accModelLines(acc);
      if (!lines.length) {
        sealedHost.append(el('div', 'hint', 'No accuracy numbers for this run.'));
      } else {
        lines.forEach(function (l) {
          var d = el('div', 'accline');
          d.append(el('span', 'what', l));
          sealedHost.append(d);
        });
      }
      sealedHost.append(el('div', 'hint', 'Accuracy counts all dated and undated key events. The six-month window organizes the calendar only.'));
      var planted = accPlantedLine(acc);
      clear(plantedHost);
      if (planted) plantedHost.append(el('div', 'hint', planted));
    });

    var cpkr = st.cost_per_kept_row_usd;
    var costLine = el('div', 'hint');
    costLine.textContent = cpkr !== null && cpkr !== undefined
      ? 'Cost per kept row ' + usdCents(cpkr) + ' — spend divided by the rows now kept or corrected.'
      : 'Cost per kept row: not recorded for this run.';
    accPanelHost.append(el('h4', null, 'Cost'));
    accPanelHost.append(costLine);

    // G6-5b: the mistake log as a table — type in plain words × model × who
    // caught it, one count per cell; each entry is one short line, folded
    // under its type
    accPanelHost.append(el('h4', null, 'The mistake log — by type · model · who caught it'));
    var log = st.mistake_list || [];
    if (!log.length) {
      accPanelHost.append(el('div', 'hint', 'No counted mistakes in this run.'));
    } else {
      var models = [];
      log.forEach(function (m) {
        var n = m.model || 'a model';
        if (models.indexOf(n) < 0) models.push(n);
      });
      var CATCHERS = [['code', 'caught by code'], ['second_reader', 'by the second reader'], ['person', 'by you']];
      function caughtWord(cb) { return cb === 'person' ? 'you' : (CAUGHT_BY_WORDS[cb] || cb); }
      var table = el('table', 'acctab');
      var thr = el('tr');
      thr.append(el('th', null, 'Mistake'));
      models.forEach(function (n) { thr.append(el('th', null, n)); });
      CATCHERS.forEach(function (c) { thr.append(el('th', null, c[1])); });
      thr.append(el('th', null, ''));
      table.append(thr);
      var byType = {};
      log.forEach(function (m) { (byType[m.type || 'none'] = byType[m.type || 'none'] || []).push(m); });
      Object.keys(byType).forEach(function (t) {
        var entries = byType[t];
        var tr = el('tr');
        tr.append(el('td', null, mistakeTypeWord(t)));
        models.forEach(function (n) {
          tr.append(el('td', null, String(entries.filter(function (m) { return (m.model || 'a model') === n; }).length || '')));
        });
        CATCHERS.forEach(function (c) {
          tr.append(el('td', null, String(entries.filter(function (m) { return m.caught_by === c[0]; }).length || '')));
        });
        tr.append(el('td', null, ''));   // placeholder — the fold spans all columns below
        table.append(tr);
        // G6-R1: the opened fold flows full width, not squeezed into a count
        // column — it sits in its own row spanning every column
        var foldTr = el('tr');
        var foldFull = el('td');
        foldFull.colSpan = 1 + models.length + CATCHERS.length + 1;
        var fold = el('details', 'fold');
        fold.append(el('summary', null, 'Show the ' + entries.length + ' row' + (entries.length === 1 ? '' : 's')));
        entries.forEach(function (m) {
          var line = el('div', 'accline');
          var what = plainWords(m.what_changed || '');
          if (what.length > 96) what = what.slice(0, 96) + '…';
          line.append(el('span', 'who', (m.model || 'a model') + ' — caught by ' + caughtWord(m.caught_by)));
          line.append(el('span', 'what', what));
          fold.append(line);
        });
        foldFull.append(fold);
        foldTr.append(foldFull);
        table.append(foldTr);
      });
      accPanelHost.append(table);
    }

    // G6-10: the three terms, explained once
    var glos = el('div', null);
    glos.style.marginTop = '14px';
    glos.append(el('h4', null, 'The words this panel uses'));
    [['Sealed set', 'the answer key a person wrote and sealed before the run.'],
     ['Key events', 'the events listed in that answer key.'],
     ['Unplaced', 'key events the reader found but could not give a date to.']].forEach(function (g) {
      var d = el('div', 'hint');
      d.append(el('b', null, g[0] + ' — '), g[1]);
      glos.append(d);
    });
    accPanelHost.append(glos);
    // footer goes last, under the mistake log
    accPanelHost.append(foot);
  }

  // ---------------------------------------------------------------- trail (U5)
  // One step per line, one actor each. Model steps name the model and carry
  // tokens/cost when present; the first step expands to the exact prompt text;
  // a person's decision is the last step, dated from decisions.jsonl.
  function renderTrail() {
    document.getElementById('trail-sub').textContent =
      'Step by step, by row · ' + detail.run.rows.length + ' rows';
    if (route.rowId && rowOf(route.rowId)) trailSel = route.rowId;

    var host = clear(document.getElementById('trail-rows'));
    detail.run.rows.forEach(function (r) { host.append(trailRowBtn(r)); });
    var on = host.querySelector('.row.on');
    if (on) revealRow(on);

    var mid = clear(document.getElementById('trail-mid'));
    var det = clear(document.getElementById('trail-det'));
    var rs = runResearch();
    mid.append(trailInstructionsBlock());
    var row = rowOf(trailSel) || detail.run.rows[0] || null;
    if (!row) {
      mid.append(el('div', 'empty', 'No rows in this run.'));
      det.append(el('div', 'empty', 'Nothing selected.'));
      return;
    }
    trailSel = row.id;

    // decision dates come from the decisions list; fetch it once per run
    if (decisionsCache.id !== detail.id) {
      var runId = detail.id;
      decisionsCache = { id: runId, lines: null };
      A.decisions(runId).then(function (lines) {
        decisionsCache = { id: runId, lines: lines };
        render();
      }).catch(function () { decisionsCache = { id: runId, lines: [] }; });
    }

    // Run 9: one folded claim section per row. Research steps belong to the row
    // named by refs.row_id; only searches, limits and other unbound steps stay
    // in the final run-wide fold. Every fold starts closed.
    detail.run.rows.forEach(function (r) { mid.append(trailClaimSection(r, rs)); });
    if (rs && rs.steps && rs.steps.length) {
      var wide = el('details', 'trailclaim runwide');
      wide.append(el('summary', 'trailclaimsum', 'Run-wide steps'));
      var wideBody = el('div', 'trailwrap trailclaimbody');
      rs = { label: rs.label, steps: researchStepsWithoutRow(rs) };
      wideBody.append(researchTrailBlock(rs));
      if (!rs.steps.length) wideBody.append(el('div', 'empty', 'No run-wide steps.'));
      wide.append(wideBody);
      mid.append(wide);
    }

    detailsPane(det, row, { back: null });
  }

  function trailInstructionsBlock() {
    var meta = detail.run.run || {};
    var note = meta.choices && meta.choices.note_for_agent;
    var wrap = el('div', 'trailwrap trailinstructions');
    wrap.append(el('div', 'rstitle', 'Instructions used for this run'));
    wrap.append(el('pre', 'codeblk', typeof note === 'string' && note !== ''
      ? note : 'No note for the agent was sent.'));
    return wrap;
  }

  function researchStepsForRow(rs, rowId) {
    return ((rs && rs.steps) || []).filter(function (s) {
      return s && s.refs && s.refs.row_id === rowId;
    });
  }

  function researchStepsWithoutRow(rs) {
    return ((rs && rs.steps) || []).filter(function (s) {
      return !(s && s.refs && s.refs.row_id);
    });
  }

  function trailClaimSection(row, rs) {
    var t = rowTarget(row);
    var fold = el('details', 'trailclaim');
    var summary = el('summary', 'trailclaimsum');
    summary.append(el('span', 'claimtitle', shownField(t, row, 'title') || '(no title)'),
                   el('span', 'claimdate', shownDateText(t, row)),
                   t.decided_by_person ? decidedBadge(t) : statusBadge(t.status));
    fold.append(summary);

    var wrap = el('div', 'trailwrap trailclaimbody');
    var meta = detail.run.run || {};
    wrap.append(el('div', 'tm', (row.ticker || '') + ' · proposed by ' + (row.proposed_by || 'Model') +
      ' · ' + (meta.label || 'run ' + meta.number) + (meta.sample ? ' · sample data — not real events' : '')));
    var researchSteps = researchStepsForRow(rs, row.id);
    if (researchSteps.length) {
      wrap.append(researchTrailBlock({ label: 'Research steps', steps: researchSteps }));
    }

    var steps = (row.trail || []).slice();
    // LETTUCE-OPTION: the wording-check steps of this row ride after the row's
    // own steps — attribution and run status stay in the trail only
    var supportSteps = supportTrailSteps((((detail.run.run || {}).research || {}).steps), row.id,
                                         ((detail.run || {})).support_findings);
    // G2-1: Person steps come only from decisions.jsonl lines — one step per
    // line, in order, with the person's own note; a matched or undecided row
    // nobody decided ends at the last code step
    var personLines = personStepsFor(row.id);
    var lines = steps.concat(supportSteps)
                     .concat(personLines.map(function (l) { return { __decision: true, line: l, target: t }; }));
    lines.forEach(function (s, i) {
      wrap.append(trailStep(row, s, i < lines.length - 1));
    });
    if (!researchSteps.length && !lines.length) wrap.append(el('div', 'empty', 'No steps recorded for this row.'));
    fold.append(wrap);
    return fold;
  }

  function trailRowBtn(row) {
    var t = rowTarget(row);
    var b = btn('row' + (row.id === trailSel ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || ''));
    l1.append(el('span', 'dt', shownDateText(t, row)));
    var l2 = el('div', 'l2');
    l2.append(t.decided_by_person ? decidedBadge(t) : statusBadge(t.status));
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    b.append(l1, l2);
    b.addEventListener('click', function () {
      trailSel = row.id;
      navigate({ runId: currentRunId(), screen: 'trail', rowId: row.id });
    });
    return b;
  }

  // G2-1: each decisions.jsonl line of the row is its own Person step, in order
  var PERSON_STEP_WORDS = { keep: 'Kept by you', fix: 'Fixed by you',
                            remove: 'Removed by you', undo: 'Undone by you' };
  function personStepsFor(rowId) {
    return (decisionsCache.lines || []).filter(function (l) {
      return l.target === rowId && l.by === 'person';
    });
  }
  function lastPersonDecisionIn(lines, rowId) {
    var last = null;
    (lines || []).forEach(function (l) {
      if (l.target !== rowId) return;
      if (l.action === 'undo') last = null;
      else if (l.action === 'keep' || l.action === 'fix' || l.action === 'remove') last = l;
    });
    return last;
  }
  function lastPersonDecision(rowId) {
    return lastPersonDecisionIn(decisionsCache.lines, rowId);
  }

  function checkFnFor(row, who) {
    var hit = null;
    (row.checks || []).forEach(function (c) {
      if (c.code_ref && c.code_ref.function === who) hit = c;
    });
    return hit;
  }

  // G2-7: the model's proposal summary in plain words — 'Milestone · Q1 ’27
  // (1 Jan – 31 Mar 2027): “…”', never 'milestone on 2027-01-01 to …'
  function daySpanWords(a, b) {
    var ya = (a || '').slice(0, 4), yb = (b || '').slice(0, 4);
    if (!dayMonth(a) || !dayMonth(b)) return '';
    // '1 Jan – 31 Mar 2027' (the start's year omitted when it matches the
    // end's); a cross-year span keeps the start year: '1 Nov 2026 – 28 Feb 2027'
    return (ya === yb ? dayMonth(a) + ' – ' : dayMonth(a) + ' ' + ya + ' – ') +
           dayMonth(b) + ' ' + yb;
  }
  // F1 (item 38, second pass): the Reader step's sentence is the reader's
  // historical record — the READER's own date: the row's words (the row is
  // filed and never mutated by decisions) with the row period's day-span
  // words, never the current state, and no ISO dates
  function readerWhen(row) {
    var dt = readerDateWords(row) || '';
    var d1 = row.date, d2 = row.date_end;
    var span = (d1 && d2 && daySpanWords(d1, d2)) || '';
    if (dt && span) return dt + ' (' + span + ')';
    if (dt) return dt;
    if (span) return span;
    if (d1 && dayMonth(d1)) return dayMonth(d1) + ' ' + String(d1).slice(0, 4);
    return 'no date on the page';
  }
  function plainProposal(row, t, s) {
    // after a clear or an edit the sentence still wears the reader's own date
    // — no date_display, no person wording, no ISO dates on the trail
    var when = readerWhen(row);
    // the quote is the reader's own too — a person's later quote edit is
    // never credited to the reader (third pass)
    var quote = (typeof row.quote === 'string' && row.quote.trim() !== '') ? row.quote : '';
    // H1: the proposal sentence quotes what the READER proposed — a person's
    // later type edit is never credited to the model (runs/proposal.py:108)
    var said = eventWord(row.event_type) + ' · ' + when;
    return quote ? said + ': “' + quote + '”' : said;
  }

  // G2-R2 / G2-6: every reader's prompt folds the same way — the first, the
  // second reader's, and Muse Wonder's re-read prompt
  var PROMPT_STEPS = { 'Instructions sent': true,
                       'Instructions sent to the second reader': true,
                       'Instructions sent to Muse Wonder': true };
  function trailStep(row, s, more) {
    var t = rowTarget(row);
    var step = el('div', 'step');
    var rail = el('div', 'rail');
    var body = el('div', 'bd');
    step.append(rail, body);

    if (s.__decision) {
      // G2-1: a person step is one decisions.jsonl line — its own words, its own
      // note; nothing is synthesized for rows nobody decided
      var l = s.line;
      var word = PERSON_STEP_WORDS[l.action] || 'Decided by you';
      rail.append(el('span', 'ic', '●'));
      if (more) rail.append(el('span', 'ln'));
      step.classList.add('personstep');
      var hd = el('div', 'hd');
      hd.append(el('span', 'who', word));
      hd.append(el('span', 'st2', 'Person'));
      // G2-R3: person steps carry date AND time, in the step header like the
      // Code and Model steps (a decision can come days after the run)
      hd.append(el('span', 'wh', dateShort(l.at) + ' ' + hm(l.at)));
      body.append(hd);
      var fx = l.fix;
      if (fx && l.action === 'fix') {
        body.append(el('div', 'tx', (FIELD_WORDS[fx.field] || fx.field) + ': ' +
          (fx.from === null || fx.from === undefined ? '(none)' : fx.from) + ' → ' +
          (fx.to === null || fx.to === undefined ? '(none)' : fx.to)));
      }
      if (l.note) body.append(el('div', 'tx', 'Note: “' + dayInWords(l.note) + '”'));
      return step;
    }

    var isFail = s.tag === 'Code' && /: fail\./.test(s.text || '');
    // G2-3: one glyph per outcome — tick only on pass, a grey dash when the
    // check could not run or was not needed, a red cross on fail
    var outcome = /: (pass|fail|could not run|not needed)\./.exec(s.text || '');
    var glyph = '·';
    if (s.tag === 'Model') glyph = '◎';
    else if (s.tag === 'Person') glyph = '●';
    else if (isFail) glyph = '✗';
    else if (s.step === 'Checked' && outcome) glyph = outcome[1] === 'pass' ? '✓' : '–';
    else if (s.step === 'Checked') glyph = '✓';
    else if (s.step === 'Sent back') glyph = '→';
    else if (PROMPT_STEPS[s.step]) glyph = '»';
    rail.append(el('span', 'ic', glyph));
    if (more) rail.append(el('span', 'ln'));
    if (isFail) step.classList.add('failstep');
    if (s.tag === 'Person') step.classList.add('personstep');
    if (s.step === 'Checked' && outcome && outcome[1] !== 'pass' && !isFail) step.classList.add('skipstep');

    var hd = el('div', 'hd');
    var whoName = s.tag === 'Model' ? (s.who || 'Model')
      : s.tag === 'Person' ? 'a person'
      : s.step;                       // Code steps read by their step name; the actor is the tag
    hd.append(el('span', 'who', whoName));
    hd.append(el('span', 'st2', s.tag === 'Model' ? 'Model · ' + (s.who || '') : s.tag));
    hd.append(el('span', 'wh', hm(s.at)));
    body.append(hd);

    if (PROMPT_STEPS[s.step]) {
      // G2-R2 / G2-6: the prompt folds exactly like the other readers' — same
      // fold label, same capped box with its own scroll
      body.append(el('div', 'tx', 'The exact text the model received.'));
      var fold = el('details', 'fold instrbox');
      fold.append(el('summary', null, 'Show the instructions'));
      var pre = el('pre', 'codeblk', s.text || '');
      fold.append(pre);
      body.append(fold);
      if (s.tokens && s.tokens.prompt !== null && s.tokens.prompt !== undefined) {
        body.append(el('div', 'meta', 'prompt ' + num(s.tokens.prompt) + ' tokens'));
      }
    } else {
      // G2-7: model summaries in plain words — the event type word and the date
      // as written, never snake_case ids or ISO dates. The reader's proposal
      // sentence appears on the proposal step and again on the second reading.
      var isProposal = s.tag === 'Model' && !PROMPT_STEPS[s.step] &&
                       /\bon\b/.test(s.text || '');
      var text = isProposal ? plainProposal(row, t, s) : (s.text || '');
      body.append(el('div', 'tx', text));
      if (isProposal) {
        // item 38 (third pass): the attribution rides ONLY when the wording
        // was cleared or fixed — never on an untouched row, whose trail is
        // byte-identical to the pre-item-38 render
        var rw = readerDateWords(row);
        var touched = t && (t.date_text_cleared === true ||
                            (t.fixes || []).some(function (f) { return f && f.field === 'date_text'; }));
        if (rw !== null && touched)
          body.append(el('div', 'meta', "Reader's words: '" + rw + "'"));
      }
      if (s.tag === 'Model' && s.tokens) {
        var parts = [];
        if (s.tokens.prompt !== null && s.tokens.prompt !== undefined) parts.push(num(s.tokens.prompt) + ' tokens in');
        if (s.tokens.completion !== null && s.tokens.completion !== undefined) parts.push(num(s.tokens.completion) + ' out');
        if (s.cost_usd !== null && s.cost_usd !== undefined) parts.push(money(s.cost_usd));
        if (parts.length) body.append(el('div', 'meta', parts.join(' · ')));
      } else if (s.tag === 'Model' && s.cost_usd !== null && s.cost_usd !== undefined) {
        body.append(el('div', 'meta', money(s.cost_usd) + ' · tokens not recorded'));
      }
    }

    // a failed check's step links to "See the check" in the details pane
    if (isFail) {
      var chk = checkFnFor(row, s.who);
      if (chk) {
        var link = el('button', 'seecheck', 'See the check');
        link.addEventListener('click', function () {
          // the folds in the details pane follow the row's check order
          var idx = (row.checks || []).indexOf(chk);
          var folds = document.querySelectorAll('#trail-det details.fold');
          if (idx >= 0 && folds[idx]) {
            folds[idx].open = true;
            revealRow(folds[idx], { block: 'center', behavior: 'smooth' });
          }
        });
        body.querySelector('.hd').append(link);
      }
    }
    return step;
  }

  // ---------------------------------------------------------------- calendar (U6)
  // U23: Meta's model list has no text-to-speech, so the U21 read-aloud
  // control is gone; voice is the person speaking, on Judgment needed.

  // Kept and corrected rows only, never removed / unverified / open. Exact days
  // sort within their month; a period row sits under its period as written,
  // with no invented day.
  // G3-1: the "Next 3 / 6 / 12 months" filter lives in the header as three
  // pills; the default is the run's own window. The selection rides the URL
  // (?w=) so browser Back restores it.
  function calWindowMonths() {
    var m = (detail.run.run || {}).choices && (detail.run.run || {}).choices.time_window_months;
    return (m === 3 || m === 6 || m === 12) ? m : 6;
  }
  function addMonths(ymd, n) {
    var y = parseInt(ymd.slice(0, 10).slice(0, 4), 10), mo = parseInt(ymd.slice(5, 7), 10) - 1 + n;
    y += Math.floor(mo / 12); mo = ((mo % 12) + 12) % 12;
    return y + '-' + String(mo + 1).padStart(2, '0') + '-' + ymd.slice(8, 10);
  }
  function calWindow() {
    var win = 6;
    var cw = detail.run.calendar_window || {};
    // no usable reference date: no window, and no date arithmetic on a non-date
    if (cw.unresolved || !cw.start) {
      return { win: win, anchor: null, end: null, unresolved: true,
               reason: cw.reason || 'This run has no usable reference date (as_of or started_at).' };
    }
    var anchor = cw.start;
    var y = Number(anchor.slice(0, 4)), m = Number(anchor.slice(5, 7)) - 1 + win;
    y += Math.floor(m / 12); m = ((m % 12) + 12) % 12;
    var lastDay = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
    var day = Math.min(Number(anchor.slice(8, 10)), lastDay);
    var end = new Date(Date.UTC(y, m, day)).toISOString().slice(0, 10);
    return { win: win, anchor: anchor, end: end };
  }

  // G8-7: a source row id — e.g. Back to calendar from that source's page —
  // maps to its folded event's representative row id and the source within it
  function calFoldHit(rows, rowId) {
    var hit = null;
    rows.forEach(function (r) {
      r.fold.forEach(function (e) { if (e.row.id === rowId) hit = { rep: r.row.id, src: e.row.id }; });
    });
    return hit;
  }

  function renderCalendar() {
    var meta = detail.run.run || {};
    var all = calendarRows();
    if (route.rowId && rowOf(route.rowId) && route.rowId !== calRouted) {
      calRouted = route.rowId;
      // landing on a secondary source of a fold reselects the folded event
      // with that source picked
      var hit = calFoldHit(all, route.rowId);
      if (hit) { calSel = hit.rep; calSrcSel = hit.src; }
      else calSel = route.rowId;
    }
    var w = calWindow();
    // The server supplies the same lower/upper interval partition consumed by
    // report/data.py, so an overlapping period and a human-kept old row land
    // in the same section in both outputs.
    var rows = all.filter(function (r) { return r.bucket === 'calendar'; });
    var past = all.filter(function (r) { return r.bucket === 'past'; });
    var later = all.filter(function (r) { return r.bucket === 'later'; });
    var nodates = all.filter(function (r) { return r.bucket === 'contingent'; });
    var undated = all.filter(function (r) { return r.bucket === 'undated'; });
    var timingUnresolved = all.filter(function (r) { return r.bucket === 'timing_unresolved'; });
    var pastIds = {};
    (detail.run.exclusions || []).forEach(function (x) { if (x.rule === 'event_in_past') pastIds[x.target] = true; });
    var lastEvents = (detail.run.rows || []).filter(function (r) { return pastIds[r.id]; })
      .map(function (r) { return { row: r, t: rowTarget(r), date: r.date || null,
        nodate: !r.date, period: !!r.date_end, end: r.date_end || null, fold: [] }; });
    var lastByTicker = {};
    lastEvents.forEach(function (r) {
      if (!lastByTicker[r.row.ticker] || (r.end || r.date) > (lastByTicker[r.row.ticker].end || lastByTicker[r.row.ticker].date))
        lastByTicker[r.row.ticker] = r;
    });
    lastEvents = Object.keys(lastByTicker).map(function (k) { return lastByTicker[k]; });

    var host = clear(document.getElementById('cal-mid'));
    var det = clear(document.getElementById('cal-det'));

    var head = el('div', 'ph');
    head.style.background = 'var(--panel2)';
    head.append(el('h2', null, 'Catalyst Calendar'));
    var subrow = el('div', 'cal-subrow');
    // A-F2(b): the count includes the rows with no parsed day
    var total = rows.length + past.length + nodates.length + undated.length + timingUnresolved.length + later.length;
    // CALENDAR-RULE-001: the calendar holds kept rows and rows placed automatically; the headline names neither
    // as the other, and splits them from the server's counts.inclusion when it sends them
    var inc = detail.state && detail.state.counts && detail.state.counts.inclusion;
    // (a server without counts.inclusion keeps the old kept-only calendar and its old words)
    if (inc) {
      // every part of the headline counts the rows actually shown in that part: N is the calendar itself (the
      // window), split into kept by you + included automatically; the sections below it are named on their own
      var keptN = rows.filter(function (r) { return r.t.inclusion === 'kept'; }).length;
      var autoN = rows.filter(function (r) { return r.t.inclusion === 'included'; }).length;
      var parts = [rows.length + ' row' + (rows.length === 1 ? '' : 's'), 'kept by you ' + keptN,
                   'included automatically ' + autoN];
      if (past.length) parts.push('context (past) ' + past.length);
      if (later.length) parts.push('later ' + later.length);
      var undatedN = nodates.length + undated.length + timingUnresolved.length;
      if (undatedN) parts.push('undated ' + undatedN);
      subrow.append(el('div', 'sub', 'Events on the calendar, by date · ' + parts.join(' · ') + ' · ' +
        (w.unresolved ? w.reason : w.anchor + ' – ' + w.end)));
    } else {
      subrow.append(el('div', 'sub', 'Kept events, by date · ' + total + ' row' +
        (total === 1 ? '' : 's') + ' · ' + (w.unresolved ? w.reason : w.anchor + ' – ' + w.end)));
    }
    head.append(subrow);
    var rl = el('div', 'runlabel' + (meta.sample ? ' sample' : ''), meta.label || '');
    head.append(rl);
    host.append(head);

    if (!rows.length && !past.length && !nodates.length && !undated.length && !timingUnresolved.length && !later.length && !lastEvents.length) {
      host.append(el('div', 'empty', inc
        ? 'Nothing on the calendar yet. Rows with no issues are placed here automatically, and rows you keep appear here too.'
        : 'Nothing kept yet — the calendar fills as rows are kept or corrected.'));
      det.append(el('div', 'empty', 'Pick an event to see its details.'));
      return;
    }

    var headers = [];
    function header(r) {
      var def = calHeader(r);
      for (var i = 0; i < headers.length; i++) if (headers[i].key === def.key) return headers[i];
      var h = { key: def.key, sort: def.sort, len: def.len, label: def.label, exact: [], periods: [] };
      headers.push(h);
      return h;
    }
    rows.forEach(function (r) {
      var h = header(r);
      if (calIsExact(r)) h.exact.push(r);
      else {
        // G3-R1/G3-R4: one marker per canonical period — every writing of a
        // quarter shares 'Q1 2027', a full year shares its year label — each
        // row keeps its own wording
        // G9-R2: a period marker inside a month carries the source wording
        // ('early Q1 2027'), never the month's own name repeated from the
        // header above it
        var wording = shownDateText(r.t, r.row);
        var canon = calIsMonthPeriod(r) ? (wording || monthYear(r.date))
          : calMonths(r) <= 3 ? calQuarterKey(r)
          : calMonths(r) === 12 ? r.date.slice(0, 4)
          : wording;
        var bucket = null;
        h.periods.forEach(function (p) { if (p.label === canon) bucket = p; });
        if (!bucket) { bucket = { label: canon, rows: [] }; h.periods.push(bucket); }
        bucket.rows.push(r);
      }
    });
    headers.sort(function (a, b) {
      var oa = calHeaderOrd(a), ob = calHeaderOrd(b);
      if (oa !== ob) return oa < ob ? -1 : 1;
      return a.len - b.len;
    });

    var wrap = el('div', 'calwrap');
    headers.forEach(function (h) {
      var sample = h.exact[0] || (h.periods[0] && h.periods[0].rows[0]);
      // G9-R3: a span header that is not a quarter ('by year-end', Sep–Dec)
      // reads the source wording of its first row, never its start month's name
      var htext = h.label;
      if (!htext && sample && sample.period && !calIsExact(sample) &&
          h.key.slice(0, 2) === 'p:')
        htext = shownDateText(sample.t, sample.row);
      wrap.append(el('div', 'mo', htext || monthYear(sample ? sample.date : h.sort + '-01')));
      // G3-4: exact dates first by day, then periods shortest to longest
      h.exact.sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
      // G3-4: periods shortest to longest inside the header
      h.periods.sort(function (a, b) {
        var la = calMonths(a.rows[0]), lb = calMonths(b.rows[0]);
        if (la !== lb) return la - lb;
        return a.rows[0].date < b.rows[0].date ? -1 : 1;
      });
      h.exact.forEach(function (r) { wrap.append(calRow(r, calDayCell(r), false)); });
      h.periods.forEach(function (p) {
        // G3-3: the group marker is a hollow dot — a period, no day given
        var ph = el('div', 'periodhead');
        var phDot = el('span', 'pdot');
        phDot.title = 'Period, no day given';
        ph.append(phDot, p.label || 'Period as written');
        wrap.append(ph);
        p.rows.forEach(function (r) { wrap.append(calRow(r, '', true)); });
      });
    });
    // Wording distinguishes missing timing, unresolved timing, and explicit dependencies.
    if (undated.length) {
      wrap.append(el('div', 'mo', 'Undated · no timing stated · ' + undated.length));
      undated.forEach(function (r) { wrap.append(calRow(r, '', true)); });
    }
    if (timingUnresolved.length) {
      wrap.append(el('div', 'mo', 'Timing unresolved · ' + timingUnresolved.length));
      timingUnresolved.forEach(function (r) { wrap.append(calRow(r, '', true)); });
    }
    if (nodates.length) {
      wrap.append(el('div', 'mo', (w.unresolved ? 'Timing unresolved · ' : 'Contingent · ') + nodates.length));
      nodates.forEach(function (r) { wrap.append(calRow(r, '', true)); });
    }
    if (past.length) {
      wrap.append(el('div', 'mo', 'Past · ' + past.length));
      past.forEach(function (r) {
        var written = shownDateText(r.t, r.row) || r.date;
        var reason = r.t.reason || r.row.reason || (keptCurrent(r.t) ? 'Kept by you; the event period ended before this run began.' : '');
        if (reason) r.row.reason = reason;
        wrap.append(calRow(r, written, !!r.period));
      });
    }
    if (later.length) {
      wrap.append(el('div', 'mo', 'Later · ' + later.length));
      // glass adviser (5): the Later section reads in date order — a period
      // sorts by its start, then the exact-day rows of the same date
      later.slice().sort(function (a, b) {
        if (a.date !== b.date) return a.date < b.date ? -1 : 1;
        return (b.period ? 1 : 0) - (a.period ? 1 : 0);
      }).forEach(function (r) {
        var written = shownDateText(r.t, r.row) || r.date;
        wrap.append(calRow(r, written, !!r.period));
      });
    }
    if (lastEvents.length) {
      lastEvents.forEach(function (r) {
        wrap.append(el('div', 'mo', 'Latest past event · ' + (r.row.ticker || 'ticker not stated')));
        var written = shownDateText(r.t, r.row) ||
          ((r.t.values && r.t.values.date) || '') || 'Date not stated';   // never the raw row date
        wrap.append(calRow(r, written, !!r.period));
      });
    }
    host.append(wrap);

    var on = host.querySelector('.crow.on');
    if (on) revealRow(on);

    // A-F2(b): the undated rows are pickable too
    var sel = all.concat(lastEvents).filter(function (r) { return r.row.id === calSel; })[0] || null;
    if (!sel) {
      det.append(el('div', 'empty', 'Pick an event to see its details.'));
      return;
    }
    // G8-7: a folded row's pane lists every source — quote, page, own status —
    // and the person picks which source the actions act on. The list rides
    // ABOVE the picked source's details (detailsPane clears the pane first).
    var pick = sel;
    var list = null;
    if (sel.fold.length > 1) {
      if (!sel.fold.some(function (e) { return e.row.id === calSrcSel; })) calSrcSel = sel.row.id;
      pick = sel.fold.filter(function (e) { return e.row.id === calSrcSel; })[0];
      list = el('div', 'sec');
      list.append(el('h4', null, 'Read from ' + sel.fold.length + ' sources'));
      sel.fold.forEach(function (e) {
        var src = sourceOf(e.row);
        var it = btn('srcitem' + (e.row.id === calSrcSel ? ' on' : ''));
        if (src) it.append(el('div', 'src1', src.publisher + ' · page ' + e.row.page_number));
        var q = (e.t.values && e.t.values.quote) || e.row.quote;
        if (q) it.append(el('div', 'srcq', '“' + q + '”'));
        var st = el('div', 'src1');
        st.append(e.t.decided_by_person ? decidedBadge(e.t) : statusBadge(e.t.status));
        it.append(st);
        it.addEventListener('click', function () { calSrcSel = e.row.id; renderCalendar(); });
        list.append(it);
      });
    } else calSrcSel = null;
    // G3-6: opened from the calendar, the pane links back to the calendar
    detailsPane(det, pick.row, { back: 'calendar', w: calWindow().win,
                                 actions: sel.fold.length > 1 });
    if (list) det.insertBefore(list, det.firstChild);
    var sa = el('div', 'sec');
    var br = el('div', 'btnrow');
    br.append(btn('btn', 'Open in Audit log', function () {
      navigate({ runId: currentRunId(), screen: 'audit', rowId: pick.row.id });
    }));
    // the folded pane's detailsPane already carries Open source page, Open
    // agent trail and Decide — the plain single-source pane keeps its own
    if (sel.fold.length === 1) {
      br.append(btn('btn', 'Open agent trail', function () {
        navigate({ runId: currentRunId(), screen: 'trail', rowId: pick.row.id });
      }));
    }
    // glass adviser (1): the calendar pane decides too — Keep / Fix / Remove
    // (+ Undo) with the same dialogs and expected_basis as the audit log,
    // without leaving the calendar
    var pt = rowTarget(pick.row);
    var calPending = needsAttention(pt) ||
      (projected(pt) && !pt.accepted && pt.review_bucket === 'proposed' &&
       typeof pt.inclusion === 'string');
    if (calPending) {
      br.append(btn('btn pri', 'Keep', function () {
        postDecision(pick.row.id, { target: pick.row.id, action: 'keep', note: null },
          'Kept by you.', { screen: 'calendar', w: calWindow().win });
      }));
      br.append(btn('btn', 'Fix…', function () {
        navigate({ runId: currentRunId(), screen: 'calendar', rowId: pick.row.id,
                   dialog: 'fix', w: calWindow().win });
      }));
      br.append(btn('btn danger', 'Remove…', function () {
        navigate({ runId: currentRunId(), screen: 'calendar', rowId: pick.row.id,
                   dialog: 'remove', w: calWindow().win });
      }));
    }
    if (projected(pt) && typeof pt.last_action === 'string' && !!pt.last_action) {
      br.append(btn('btn', 'Undo', function () {
        postDecision(pick.row.id, { target: pick.row.id, action: 'undo', note: null },
          'Undone.', { screen: 'calendar', w: calWindow().win });
      }));
    }
    sa.append(br);
    det.append(sa);
    if (route.dialog) openDialog(pick.row, route.dialog);
    else closeDialog();
  }

  // G3-2: the day cell — a single day, or a day range kept as a range
  function calDayCell(r) {
    if (!r.period || !r.end) return dayMonth(r.date);
    var y1 = r.date.slice(0, 4), m1 = r.date.slice(5, 7), d1 = parseInt(r.date.slice(8, 10), 10);
    var y2 = r.end.slice(0, 4), m2 = r.end.slice(5, 7), d2 = parseInt(r.end.slice(8, 10), 10);
    var mon1 = MONTHS_SHORT[parseInt(m1, 10) - 1] || m1;
    var mon2 = MONTHS_SHORT[parseInt(m2, 10) - 1] || m2;
    if (y1 === y2 && m1 === m2) return d1 + '–' + d2 + ' ' + mon1;
    return d1 + ' ' + mon1 + (y1 === y2 ? '' : ' ' + y1) + ' – ' + d2 + ' ' + mon2 +
           (y1 === y2 ? '' : ' ' + y2);
  }

  function calRow(r, dayCell, isPeriod) {
    var row = r.row, t = r.t;
    var b = btn('crow' + (row.id === calSel ? ' on' : ''));
    // G3-3: filled dot = an exact day or day range; hollow dot = a period
    var cd = el('span', 'cd');
    var dot = el('span', 'caldot' + (isPeriod ? ' hollow' : ' filled'));
    if (isPeriod) dot.title = 'Period, no day given';
    cd.append(dot);
    if (dayCell) cd.append(document.createTextNode(dayCell));
    b.append(cd);
    var c2 = el('span', 'ct2');
    // Codex NOT ACCEPT (7bcb6ea item 1): the calendar wears the CURRENT values,
    // not the reader's original row — shownField is presence-aware, so a fix
    // shows and a deliberately cleared field shows as cleared
    c2.append(el('span', 'tk', shownField(t, row, 'ticker') || '—'));
    var calProgram = shownField(t, row, 'program');
    if (calProgram) c2.append(el('span', 'pg2', calProgram));
    c2.append(el('span', 'tt', shownField(t, row, 'title') || '(no title)'));
    var wf = supportTag(row.id);   // CVA: visible without opening the row
    if (wf) c2.append(wf);
    if (t.contingent) c2.append(el('span', 'keepwarn', 'Contingent · explicit dependency'));
    // A-F7: a row a person kept while a check failed or the readers disagree
    // is not settled — a small warm marker says so next to the title
    if (keptCurrent(t)) {
      if (t.claim_assessments && t.claim_assessments.visible_passage &&
          t.claim_assessments.visible_passage.result === 'not_checked') {
        c2.append(el('span', 'keepwarn', 'kept by you · visible words not checked'));
      }
      var af7fail = effectiveChecks(row, t).some(function (c) { return c.result === 'fail'; });
      var af7dis = t.disagreement && t.disagreement.second_reader !== null &&
                   t.disagreement.second_reader !== undefined;
      if (af7fail || af7dis) {
        c2.append(el('span', 'keepwarn',
          af7fail && af7dis ? 'kept with a failed check, readers disagree'
          : af7fail ? 'kept with a failed check' : 'kept, readers disagree'));
      }
    }
    // G8-7: a folded row says how many sources it was read from
    if (r.fold.length > 1) c2.append(el('span', 'srcpill', r.fold.length + ' sources'));
    if (isPeriod) {
      // G3-4: the row keeps its own source wording as the date text
      var wording = shownDateText(t, row);
      if (wording) c2.append(el('span', 'dt', wording));
    }
    b.append(c2);
    b.append(el('span', 'ty', typeCategoryValue(t) || eventWord(shownField(t, row, 'event_type'))));
    if (t.decided_by_person) b.append(decidedBadge(t));
    b.addEventListener('click', function () {
      calSel = row.id;
      navigate({ runId: currentRunId(), screen: 'calendar', rowId: row.id, w: calWindow().win });
    });
    return b;
  }

  // ---------------------------------------------------------------- judgment (U4)
  function renderJudgment() {
    var unresolved = unresolvedRows();
    var decided = decidedRows();
    // GROUP-AXIS-001: the review projection's count is the authority when the
    // server sends it; the client-side list is the fallback on older servers
    var reviewCounts = detail.state.counts && detail.state.counts.review;
    // CALENDAR-RULE-001: the server's queue is the rows with issues (state.unresolved, counts.inclusion.review);
    // a clean proposal placed automatically is not waiting for a decision
    var inclusionCounts = detail.state.counts && detail.state.counts.inclusion;
    document.getElementById('judgment-sub').textContent =
      'Waiting for your decision · ' + (inclusionCounts ? inclusionCounts.review
        : reviewCounts ? reviewCounts.proposed : unresolved.length);

    var host = clear(document.getElementById('judgment-rows'));
    var fixedNK = decided.filter(function (r) { return fixedNotKept(rowTarget(r)); });
    var plainDecided = decided.filter(function (r) { return !fixedNotKept(rowTarget(r)); });
    if (!unresolved.length && !decided.length) {
      // PROJECTION-004: with the inclusion projection, a clean proposal is
      // placed automatically — that is not a person decision, so the empty
      // queue never claims everything was decided
      host.append(el('div', 'empty', inclusionCounts ? 'Nothing needs review.'
        : 'The queue is empty. Everything proposed has been decided.'));
    } else {
      if (unresolved.length) {
        host.append(el('div', 'grp', 'Waiting for you · ' + unresolved.length));
        unresolved.forEach(function (r) { host.append(judgmentRow(r)); });
      }
      if (fixedNK.length) {
        // glass adviser (3): a Fix is not a Keep — the group says what the badge says
        host.append(el('div', 'grp', 'Corrected by you, not kept · ' + fixedNK.length));
        fixedNK.forEach(function (r) { host.append(judgmentRow(r)); });
      }
      if (plainDecided.length) {
        host.append(el('div', 'grp', 'Decided by you · ' + plainDecided.length));
        plainDecided.forEach(function (r) { host.append(judgmentRow(r)); });
      }
    }
    var on = host.querySelector('.row.on');
    if (on) revealRow(on);

    // capture before the host clear: the poll rebuilds this pane and opened
    // folds + scroll survive only a same-row refresh
    captureFolds(document.getElementById('judgment-det'));
    // fix-on-find (Red Team / Claude re-review): snapshot the live 'Your call'
    // note BEFORE the clear — the clear blurs it, and a caret moved without
    // typing (click, arrows) never fires input
    var liveNote = document.getElementById('qnote');
    // only a box with something to keep (text, or the caret in it) records;
    // an empty unfocused box on another row never wipes this row's draft
    if (liveNote && liveNote.__row && (liveNote.value || document.activeElement === liveNote)) {
      if (!qnoteDraft) qnoteDraft = {};
      qnoteDraft[currentRunId() + '/' + liveNote.__row] =
        { runId: currentRunId(), rowId: liveNote.__row, value: liveNote.value,
          start: liveNote.selectionStart, end: liveNote.selectionEnd,
          focused: document.activeElement === liveNote };
    }
    var mid = clear(document.getElementById('judgment-mid'));
    var det = clear(document.getElementById('judgment-det'));
    var row = rowOf(judgmentSel) || unresolved[0] || null;
    if (route.rowId && rowOf(route.rowId)) row = rowOf(route.rowId);
    if (!row) {
      mid.append(el('div', 'empty', 'Pick a row on the left.'));
      det.append(el('div', 'empty', 'Nothing waiting.'));
      closeDialog();
      return;
    }
    judgmentSel = row.id;
    var t = rowTarget(row);
    // glass adviser (2): with a row open, the middle column shows its source
    // and quote — never a bare 'Pick a row' beside an open decision
    var jsrc = sourceOf(row);
    var jmid = el('div', 'jmidcard');
    jmid.append(el('div', 'ttl', row.title || '(no title)'));
    if (jsrc) jmid.append(el('div', 'hint', jsrc.publisher + ' · page ' + row.page_number));
    var jquote = (t.values && t.values.quote) || row.quote;
    if (jquote) jmid.append(el('div', 'srcq', '“' + jquote + '”'));
    mid.append(jmid);
    // a corrected row that still needs a person gets the pending pane (Keep is
    // the answer the server accepts), not the decided one (Undo only)
    // CALENDAR-RULE-001: a row placed automatically has no decision yet — it gets the same Keep / Fix / Remove
    // as a row waiting for review, so Remove (and then Undo) works on every row
    // (selected by the acceptance/inclusion projection, not by "never decided": a re-checked Fix on a placed row
    // still leaves Keep / Fix again / Remove, with Undo beside them)
    if (needsAttention(t) || (projected(t) && !t.accepted && t.review_bucket === 'proposed' &&
        typeof t.inclusion === 'string')) judgmentPendingPane(det, row, t);
    else judgmentDecidedPane(det, row, t);

    if (route.dialog) openDialog(row, route.dialog);
    else closeDialog();
    restoreFolds(document.getElementById('judgment-det'),
                 currentRunId() + '/judgment/' + (route.rowId || judgmentSel || ''));
  }

  function judgmentRow(row) {
    var t = rowTarget(row);
    var b = btn('row' + (row.id === judgmentSel ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || ''));
    l1.append(el('span', 'dt', shownDateText(t, row)));
    var wf = supportTag(row.id);   // CVA: visible without opening the row
    if (wf) l1.append(wf);
    var l2 = el('div', 'l2');
    if (fixStillOpen(t)) {
      l2.append(el('span', 'st st-corrected open', decidedWord(t, 'Fixed by you') + ' · still open'));
    } else {
      l2.append(rowBadge(row, t));
    }
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    if ((t.reason || (t.issues && t.issues.length)) && (!t.decided_by_person || needsAttention(t))) {
      l2.append(el('span', 'fixline', listReasonLine(row, t)));
    }
    b.append(l1, l2);
    b.addEventListener('click', function () {
      judgmentSel = row.id;
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
    });
    return b;
  }

  function noteField(id, placeholder) {
    var t = el('textarea', 'note');
    t.id = id;
    t.placeholder = placeholder || 'Why you decided this… (optional)';
    return t;
  }

  // ---------------------------------------------------------------- say your decision (U23)
  // Meta's model list has no text-to-speech; the voice model transcribes the
  // person. The mic records in the browser with the Web Audio API, downsampled
  // to a mono 16-bit 16 kHz WAV (the route takes WAV only, at most 30 s), and
  // POSTs to /api/runs/<id>/voice/decide?target=<row>. The reply's proposal
  // pre-fills the same Keep / Fix / Remove buttons — nothing is decided until
  // the person presses one.
  var SAY_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                    'August', 'September', 'October', 'November', 'December'];
  var SAY_MAX_SECONDS = 30;

  // float samples -> 16-bit PCM mono WAV at 16 kHz, capped at 30 s
  function encodeWav(samples, rate) {
    var TARGET = 16000;
    var ratio = rate / TARGET;
    var outLen = Math.max(1, Math.min(Math.floor(samples.length / ratio), TARGET * SAY_MAX_SECONDS));
    var buf = new ArrayBuffer(44 + outLen * 2);
    var v = new DataView(buf);
    function wstr(o, t) { for (var i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); }
    wstr(0, 'RIFF'); v.setUint32(4, 36 + outLen * 2, true); wstr(8, 'WAVE'); wstr(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, TARGET, true); v.setUint32(28, TARGET * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    wstr(36, 'data'); v.setUint32(40, outLen * 2, true);
    var o = 44;
    for (var i = 0; i < outLen; i++) {
      var base = Math.floor(i * ratio), cnt = 0, s = 0;
      var end = Math.min(base + Math.max(1, Math.round(ratio)), samples.length);
      for (var j = base; j < end; j++) { s += samples[j]; cnt++; }
      s = cnt ? s / cnt : 0;
      s = Math.max(-1, Math.min(1, s));
      v.setInt16(o, s < 0 ? Math.round(s * 32768) : Math.round(s * 32767), true);
      o += 2;
    }
    return new Uint8Array(buf);
  }

  // a proposed value in plain words: dates in words, types as their word
  function fmtSayValue(field, value) {
    if (value === null || value === undefined || value === '') return '(none)';
    if (field === 'event_type') return eventWord(value);
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value));
    if (m && (field === 'date' || field === 'date_end')) {
      return SAY_MONTHS[parseInt(m[2], 10) - 1] + ' ' + parseInt(m[3], 10) + ', ' + m[1];
    }
    return String(value);
  }

  // the proposal as one question: "Fix the date to March 3, 2027?"
  function sayProposalText(p) {
    if (!p || !p.action) return null;
    if (p.action === 'fix') {
      // the route returns capture.voice.propose() unchanged: its value key is `to`
      var to = p.value !== undefined ? p.value : p.to;
      // a spoken period opens the dialog on Date as written — the question
      // names THAT field, never the ISO-only date box
      var field = p.field;
      if (field === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(String(to))) field = 'date_text';
      return 'Fix the ' + String(FIELD_WORDS[field] || field || 'row').toLowerCase() +
             ' to ' + fmtSayValue(field, to) + '?';
    }
    if (p.action === 'remove') return 'Remove it?';
    return 'Keep it?';
  }

  function sayReplyLine(reply) {
    var t = sayProposalText(reply && reply.proposal);
    return "You said: '" + ((reply && reply.transcript) || '') + "'" + (t ? ' → ' + t : '');
  }

  var sayRec = null;
  // a re-render for another row drops a live take without posting it
  function sayBusyWith(row) { return sayRec && sayRec.row !== row; }
  function cancelSay() {
    var rec = sayRec;
    if (!rec) return;
    sayRec = null;
    clearTimeout(rec.timer);
    rec.node.onaudioprocess = null;
    rec.stream.getTracks().forEach(function (t) { t.stop(); });
    try { rec.node.disconnect(); rec.ctx.close(); } catch (e) { /* already shut */ }
    if (rec.button.isConnected) rec.button.textContent = 'Say your decision';
  }
  function stopSay() {
    var rec = sayRec;
    if (!rec) return;
    sayRec = null;
    clearTimeout(rec.timer);
    rec.node.onaudioprocess = null;
    rec.stream.getTracks().forEach(function (t) { t.stop(); });
    try { rec.node.disconnect(); rec.ctx.close(); } catch (e) { /* already shut */ }
    rec.button.textContent = 'Say your decision';
    if (rec.samples.length / rec.ctx.sampleRate < 0.4) {
      rec.note.textContent = 'Too short — hold the button, say your decision, let go.';
      rec.note.hidden = false;
      return;
    }
    rec.button.textContent = 'Sending…';
    var wav = encodeWav(rec.samples, rec.ctx.sampleRate);
    fetch('/api/runs/' + encodeURIComponent(currentRunId()) +
          '/voice/decide?target=' + encodeURIComponent(rec.row.id), {
      method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav
    }).then(function (r) {
      rec.button.textContent = 'Say your decision';
      if (!r.ok) {
        // the route answers 422 with a plain sentence in detail; a missing route
        // (404/501) or an unreadable body keeps the one honest build line
        if (r.status === 404 || r.status === 501) {
          rec.note.textContent = 'Voice is not on this build yet.';
          rec.note.hidden = false;
          return null;
        }
        return r.json().then(function (err) {
          rec.note.textContent = (err && err.detail) || 'Voice is not on this build yet.';
          rec.note.hidden = false;
          return null;
        }).catch(function () {
          rec.note.textContent = 'Voice is not on this build yet.';
          rec.note.hidden = false;
          return null;
        });
      }
      return r.json().then(function (reply) {
        var p = reply && reply.proposal;
        var line = sayReplyLine(reply);
        clear(rec.said);
        rec.said.append(el('div', null, line));
        if (p && p.note) rec.said.append(el('div', 'hint', p.note));
        rec.said.hidden = false;
        prefillSay(rec.row, p);
      });
    }).catch(function () {
      rec.button.textContent = 'Say your decision';
      rec.note.textContent = 'Voice is not on this build yet.';
      rec.note.hidden = false;
    });
  }

  // the proposal fills the SAME controls — nothing is posted from voice
  function prefillSay(row, p) {
    if (!p) return;
    var q = document.getElementById('qnote');
    // the spoken note never replaces a reason the person already typed — the
    // words already show in the question and the To box (round 10)
    if (p.note && q && !q.value) q.value = p.note;
    if (p.action === 'fix') {
      // a spoken period is not an ISO day: the date box would red-line a value
      // the person just said — offer Date as written instead
      var to = p.value !== undefined ? p.value : p.to;
      var field = p.field;
      if (field === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(String(to))) field = 'date_text';
      fixDialog(row, { field: field, to: to });
    } else if (p.action === 'remove') {
      removeDialog(row, p.note || null);
    } else {
      var kb = document.getElementById('keepbtn');
      if (kb) {
        kb.classList.add('pulse');
        setTimeout(function () { kb.classList.remove('pulse'); }, 2400);
      }
    }
  }

  function sayControls(row) {
    var wrap = el('div', 'saywrap');
    var note = el('div', 'hint saynote');
    note.hidden = true;
    var said = el('div', 'yousaid');
    said.hidden = true;
    var b = btn('btn saybtn', 'Say your decision');
    b.id = 'saybtn';
    var holdAt = 0, heldStop = false, releaseStopsLive = false, starting = false;
    // press and hold (capture starts on press), or click to start / click to stop
    function startCapture() {
      starting = true;
      note.hidden = true;
      said.hidden = true;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        starting = false;
        note.textContent = 'Voice is not on this build yet.';
        note.hidden = false;
        return;
      }
      // a mic that never answers reads as unreachable, not as a silent wait
      var gum = navigator.mediaDevices.getUserMedia({ audio: true });
      Promise.race([gum, new Promise(function (_, rej) {
        setTimeout(function () { rej(new Error('mic timeout')); }, 10000);
      })]).then(function (stream) {
        starting = false;
        if (!b.isConnected) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        var AC = window.AudioContext || window.webkitAudioContext;
        var ctx = new AC();
        if (!ctx.createScriptProcessor) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          releaseStopsLive = false;
          note.textContent = 'Voice is not on this build yet.';
          note.hidden = false;
          return;
        }
        var begin = function () {
          // the row may have changed while the context was resuming — never
          // start a take on a button that is gone (Codex P1 on #93)
          if (!b.isConnected) {
            stream.getTracks().forEach(function (t) { t.stop(); });
            try { ctx.close(); } catch (e) { /* already shut */ }
            return;
          }
          var src = ctx.createMediaStreamSource(stream);
          var node = ctx.createScriptProcessor(4096, 1, 1);
          var rec = { ctx: ctx, stream: stream, node: node, samples: [], button: b,
                      note: note, said: said, row: row,
                      timer: setTimeout(stopSay, SAY_MAX_SECONDS * 1000) };
          node.onaudioprocess = function (e) {
            var ch = e.inputBuffer.getChannelData(0);
            for (var i = 0; i < ch.length; i++) rec.samples.push(ch[i]);
            if (rec.samples.length / ctx.sampleRate >= SAY_MAX_SECONDS) stopSay();
          };
          src.connect(node);
          node.connect(ctx.destination);
          sayRec = rec;
          b.textContent = 'Listening… — press to send';
          // the person let go while the mic was still setting up: stop now
          if (releaseStopsLive) { releaseStopsLive = false; stopSay(); }
        };
        // the context is built after the press, outside the gesture — some
        // browsers start it suspended, which would record a silent take
        if (ctx.resume) {
          ctx.resume().then(begin, function () {
            // the context never started — no take can run on it
            stream.getTracks().forEach(function (t) { t.stop(); });
            try { ctx.close(); } catch (e) { /* already shut */ }
            note.textContent = 'The microphone could not be reached.';
            note.hidden = false;
          });
        } else begin();
      }).catch(function () {
        starting = false;
        releaseStopsLive = false;
        // only a truly lost race stops the microphone — a late answer after
        // the 10 s timeout must not keep the mic running
        gum.then(function (s) {
          s.getTracks().forEach(function (t) { t.stop(); });
        }, function () { /* permission denied — nothing to stop */ });
        note.textContent = 'The microphone could not be reached.';
        note.hidden = false;
      });
    }
    b.addEventListener('mousedown', function () {
      holdAt = Date.now();
      if (!sayRec && !starting) startCapture();
    });
    b.addEventListener('mouseup', function () {
      if (Date.now() - holdAt <= 600) return;
      if (sayRec) { heldStop = true; stopSay(); }
      // let go while the mic was still setting up: the take stops once it is live
      else if (starting) releaseStopsLive = true;
    });
    b.addEventListener('click', function () {
      if (heldStop) { heldStop = false; return; }
      if (sayRec) { stopSay(); return; }
      // the press that began this setup already handled it (mouse) — Enter has no mousedown
      if (starting) return;
      startCapture();
    });
    // CVA: voice pre-fills a proposal; it never saves the decision by itself
    var explain = el('div', 'hint',
      'Voice pre-fills a decision; nothing is saved until you press a decision button.');
    wrap.append(b, note, said, explain);
    return wrap;
  }

  // CODEXADV_KIMI_DIALOG_REFRESH: an in-progress 'Your call' note lives in a
  // per-run+row draft map, updated on input — renderJudgment CLEARS the pane
  // before this function runs, so a host lookup there always finds nothing.
  // A draft follows only its OWN run and row, never another target (DIALOGS-003:
  // two runs can generate the same row id; each keeps its own words).
  var qnoteDraft = {};
  // CVA: one note feeds every decision. With a reason already typed, Remove
  // asks only for confirmation; an empty box keeps the existing reason dialog.
  function judgmentRemove(row, note) {
    var reason = (note || '').trim();
    if (!reason) {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id, dialog: 'remove' });
      return;
    }
    if (!window.confirm('Remove this row? Your note will be saved with the decision.')) return;
    postDecision(row.id, { target: row.id, action: 'remove', note: reason },
      'Removed by you — moved to checked and removed.');
  }

  function judgmentPendingPane(host, row, t) {
    var keepNote = qnoteDraft ? qnoteDraft[currentRunId() + '/' + row.id] : null;
    clear(host);
    if (sayBusyWith(row)) cancelSay();
    // a row the person corrected earlier but that still needs a person: show the
    // fix and its re-check outcome, then ask for the call again (Red Team round 7)
    var correctedOpen = fixStillOpen(t);
    if (correctedOpen) {
      var hd = el('div', 'sec');
      hd.append(el('div', 'ttl', row.title || '(no title)'));
      var banner = el('div', 'ba');
      banner.append(el('b', null, decidedWord(t, 'Fixed by you') + ' · still open'));
      if (t.reason) {
        var br1 = el('div');
        br1.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
        br1.textContent = t.reason;
        banner.append(br1);
      }
      hd.append(banner);
      host.append(hd);
      host.append(correctedSection(row, t));
      if (t.disagreement) host.append(disagreementSection(row, t));
      host.append(checksSection(row, t));
    } else {
      detailsPane(host, row, { back: null });
    }
    // decision area replaces the action row: append at the end
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Your note (saved with your decision)'));
    var note = el('textarea', 'note');
    note.id = 'qnote';
    note.__row = row.id;   // the row this box belongs to
    sec.append(el('div', null, ''), note);
    note.placeholder = 'Why you decided this… (optional for Keep, required for Remove)';
    // the same run's same-row draft comes back — text, or a focused empty box;
    // another run's row with this id starts with its own empty box (DIALOGS-003)
    var restoreNote = !!(keepNote && keepNote.runId === currentRunId() && keepNote.rowId === row.id &&
                         (keepNote.value || keepNote.focused));
    if (restoreNote) note.value = keepNote.value || '';
    note.addEventListener('input', function () {
      if (!qnoteDraft) qnoteDraft = {};
      qnoteDraft[currentRunId() + '/' + row.id] =
        { runId: currentRunId(), rowId: row.id, value: note.value,
          start: note.selectionStart, end: note.selectionEnd,
          focused: document.activeElement === note };
    });
    // no focus/blur bookkeeping here: the pane clear itself blurs the box and
    // would wipe the focus renderJudgment just recorded (fix-on-find)
    // G1-11: the decision buttons stay on one line — Remove… is never orphaned
    var br = el('div', 'btnrow nowrap');
    br.style.marginTop = '10px';
    var v = t.values || {};
    var isPeriod = !!v.date_end || !!(v.date_text && !v.date);
    // G5-6: with a disagreement open, Keep says which value it keeps
    var keepLabel = 'Keep';
    if (t.disagreement && t.disagreement.second_reader !== null && t.disagreement.second_reader !== undefined) {
      var dv = t.disagreement.field === 'event_type'
        ? eventWord(t.disagreement.first_reader)
        : String(t.disagreement.first_reader);
      if (dv.length > 24) dv = dv.slice(0, 24) + '…';
      keepLabel = 'Keep ' + dv;
    }
    var keepB = btn('btn pri', keepLabel, function () {
      postDecision(row.id, { target: row.id, action: 'keep', note: note.value || null },
        'Kept by you.');
    });
    keepB.id = 'keepbtn';
    br.append(keepB);
    if (isPeriod) {
      br.append(btn('btn', 'Keep as stated', function () {
        var dt = shownDateText(t, row) || 'the period';
        postDecision(row.id, { target: row.id, action: 'keep',
          note: note.value || ('Kept as stated: ' + dt + '.') },
          'Kept as stated — the period stays, no day invented.');
      }));
    }
    br.append(btn('btn', correctedOpen ? 'Fix again…' : 'Fix…', function () {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id, dialog: 'fix' });
    }));
    br.append(btn('btn danger', 'Remove…', function () {
      judgmentRemove(row, note.value);
    }));
    // D1: under the projection the server tells us the decision stack through
    // last_action (null when the stack is empty) — Undo is offered whenever a
    // decision exists, not only beside an open fix: a superseded Keep can be
    // taken back like any other decision
    var canUndo = correctedOpen ||
      (projected(t) && typeof t.last_action === 'string' && !!t.last_action);
    if (canUndo) {
      br.append(btn('btn', 'Undo', function () {
        postDecision(row.id, { target: row.id, action: 'undo', note: null }, 'Undone.');
      }));
    }
    sec.append(br);
    if (isPeriod) {
      var keptWording = shownDateText(t, row);
      sec.append(el('div', 'hint', keptWording
        ? 'Keep as stated keeps “' + keptWording + '” as a period, with no invented day.'
        : 'Keep as stated leaves the wording cleared, with no invented day.'));
    }
    if (correctedOpen) {
      sec.append(el('div', 'hint',
        'Keep accepts the fixed value as it stands; Fix again changes it; Undo takes your fix back.'));
    }
    // U23: the person can say the decision instead of typing it
    sec.append(sayControls(row));
    host.append(sec);
    // focus and caret only take once the box is in the page
    if (restoreNote) {
      if (keepNote.focused && note.focus) note.focus();
      if (note.setSelectionRange && keepNote.start !== null && keepNote.start !== undefined) {
        try { note.setSelectionRange(keepNote.start, keepNote.end); } catch (e) { /* detached */ }
      }
    }
  }

  function judgmentDecidedPane(host, row, t) {
    clear(host);
    // G5-R1: the pane keeps the row's header after a decision — title,
    // company, date and quote above the decision state
    var hd = el('div', 'sec');
    hd.append(el('div', 'ttl', row.title || '(no title)'));
    var kv = el('div', 'sec');
    function kvr(k, v) { var r = el('div', 'kv'); r.append(el('span', 'k', k), el('span', 'v', v)); kv.append(r); }
    kvr('Company', (row.ticker || '') + ' · ' + (row.company || ''));
    if (row.program) kvr('Program', row.program);
    kvr('Event date', shownDateText(t, row) || (projected(t) || partialProjection(t) ? 'Not stated' : ''));
    hd.append(kv);
    var qt = shownField(t, row, 'quote');
    if (qt) {
      var sq = el('div', 'sec');
      sq.append(el('h4', null, 'The quote'));
      var qb = el('div', 'qbox');
      var srcq = sourceOf(row);
      if (srcq) qb.append(el('div', 'qm', 'from ' + srcq.publisher + ' · page ' + row.page_number));
      qb.append('“' + qt + '”');
      sq.append(qb);
      hd.append(sq);
    }
    host.append(hd);
    var sec = el('div', 'sec');
    // green only for an accepted row when the projection is sent; an
    // excluded row reads red (fallback: green, as before)
    var box = el('div', projected(t) && reviewBucket(t) !== 'accepted'
      ? (reviewBucket(t) === 'excluded' ? 'ba red' : 'ba') : 'ba green');
    var badge = decidedWord(t, 'Decided by you');
    box.append(el('b', null, badge));
    // G1-10: one line — a reason that repeats the badge adds nothing
    if (t.reason && t.reason.replace(/\.$/, '') !== badge) {
      var r = el('div');
      r.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
      r.textContent = t.reason;
      box.append(r);
    }
    sec.append(box);
    host.append(sec);
    // G5-6: the resolved disagreement stays on the record, folded — the row's
    // pane keeps what the second reader read
    var dg = t.disagreement;
    if (dg && dg.second_reader !== null && dg.second_reader !== undefined) {
      var dsec = el('div', 'sec');
      var fold = el('details', 'fold');
      var dvv = dg.field === 'event_type' ? eventWord(dg.first_reader) : String(dg.first_reader);
      if (dvv.length > 24) dvv = dvv.slice(0, 24) + '…';
      fold.append(el('summary', null, 'Resolved by you · kept ' + dvv));
      var dbox = el('div', 'ba blue');
      dbox.style.marginTop = '8px';
      function dval(v) { return dg.field === 'event_type' ? eventWord(v) : fmtField(dg.field, v); }
      var dl1 = el('div');
      dl1.append(el('b', null, 'First reader · ' + (row.proposed_by || 'Muse Spark') + ': '), dval(dg.first_reader));
      var dl2 = el('div');
      dl2.style.marginTop = '4px';
      var srL2 = ((detail.run.run || {}).second_reader || {}).model || 'Muse Spark Open';
      dl2.append(el('b', null, 'Second reader · ' + srL2 + ': '), dval(dg.second_reader));
      dbox.append(dl1, dl2);
      fold.append(dbox);
      dsec.append(fold);
      host.append(dsec);
    }
    if (t.group === 'corrected') host.append(correctedSection(row, t));
    if (t.group === 'removed') host.append(removedSection(row, t));
    host.append(checksSection(row, t));
    var sa = el('div', 'sec');
    var br = el('div', 'btnrow');
    br.append(btn('btn', 'Undo', function () {
      postDecision(row.id, { target: row.id, action: 'undo', note: null }, 'Undone.');
    }));
    sa.append(br);
    host.append(sa);
  }

  // ---------------------------------------------------------------- decisions
  function postDecision(rowId, body, successMsg, opts) {
    if (pending) return;
    pending = true;
    // G1-10: an Undo must not wait ~1 s in silence — say it at once
    if (body.action === 'undo') toast(successMsg || 'Undone.');
    A.decide(currentRunId(), body).then(function (res) {
      pending = false;
      if (!res.ok) {
        var msg = (res.body && res.body.detail) || ('HTTP ' + res.status);
        if (route.dialog) showDialogError(msg);
        else toast(msg);
        // DIALOGS-001: a refused write on changed material (409) refreshes so
        // the mounted dialog re-binds to the current proposal and asks again
        if (res.status === 409) refresh();
        return;
      }
      // drop the dialog from the hash BEFORE re-rendering, or it would re-open on the old state
      var nav = { runId: currentRunId(), screen: (opts && opts.screen) || 'judgment', rowId: rowId };
      if (opts && opts.w) nav.w = opts.w;
      navigate(nav);
      return refresh().then(function () {
        if (body.action !== 'undo') toast(successMsg || 'Decided.');
      });
    }).catch(function () {
      pending = false;
      toast('The server is not reachable.');
    });
  }

  // ---------------------------------------------------------------- dialogs
  var dialogWrap = document.getElementById('dialog-wrap');
  var dialogHost = document.getElementById('dialog');
  // CODEXADV_KIMI_DIALOG_REFRESH: the 2500 ms job poll re-renders and re-calls
  // openDialog for the same run/row/action. The mounted dialog, its draft, the
  // selected field, focus and caret survive that. A real close (Esc, Cancel,
  // backdrop), a successful save, or another target/action resets it.
  var dialogMount = null;
  function closeDialog() {
    dialogMount = null;
    dialogWrap.hidden = true;
    clear(dialogHost);
  }
  function showDialogError(msg) {
    var old = dialogHost.querySelector('.errline');
    if (old) old.remove();
    var pend = dialogHost.querySelector('.pendline');
    if (pend) pend.remove();
    var e = el('div', 'errline', msg);
    var br = dialogHost.querySelector('.btnrow');
    if (br) dialogHost.insertBefore(e, br);
    else dialogHost.append(e);
  }

  // glass adviser: a dialog cancelled from the calendar returns there (the
  // window preserved); from any other screen the Cancel targets are unchanged
  function dialogHome(row, fallback) {
    if (typeof route !== 'undefined' && route && route.screen === 'calendar') {
      return { runId: currentRunId(), screen: 'calendar', rowId: row.id, w: route.w || null };
    }
    return fallback;
  }

  function openDialog(row, action) {
    // background refresh for the dialog already open on this run/row/action:
    // keep the mounted dialog untouched — recreating it wiped the in-progress
    // reason, the selected field and the caret mid-typing
    if (action && dialogMount && dialogMount.key === currentRunId() + '/' + row.id + '/' + action &&
        !dialogWrap.hidden) {
      // DIALOGS-001: a refresh may change the material behind the same id
      // (a revised proposal). The draft survives, but the confirmation must
      // not outlive the proposal the person actually saw.
      if (action === 'keepanyway' && basisDigest(row.id) !== dialogMount.basisKey) markDialogStale(row);
      return;
    }
    dialogMount = action ? { key: currentRunId() + '/' + row.id + '/' + action,
                             basis: basisOf(row.id), basisKey: basisDigest(row.id),
                             stale: false } : null;
    // U38: on a row whose quote check failed with a near match, the Fix
    // dialog opens on the quote field pre-filled with the page's wording —
    // one Save fix click accepts it (the person can still edit)
    if (action === 'fix') fixDialog(row, nearMissPrefill(row));
    else if (action === 'remove') removeDialog(row);
    else if (action === 'keepanyway') keepAnywayDialog(row);
    else closeDialog();
  }
  // DIALOGS-001: the proposal on screen no longer matches the one the box was
  // opened on — disclose the current wording and require a fresh confirmation;
  // the reason draft is untouched
  function markDialogStale(row) {
    if (!dialogMount || dialogMount.stale) return;
    dialogMount.stale = true;
    var fresh = rowOf(row.id) || row;
    var sub = dialogHost.querySelector('.sh-s');
    if (sub) sub.textContent = fresh.ticker + ' · ' + fresh.title;
    if (!dialogHost.querySelector('.dlgstale')) {
      var notice = el('div', 'ba dlgstale');
      notice.textContent = 'The proposal changed while this box was open. The current wording is ' +
        'shown above — press Keep anyway again to confirm it.';
      var br0 = dialogHost.querySelector('.btnrow');
      if (br0) dialogHost.insertBefore(notice, br0);
      else dialogHost.append(notice);
    }
  }

  // U38 rework (Job 47): one source of truth — the check itself carries
  // near_match, the page's wording the check found. The UI never rebuilds
  // the wording from the sentence (a repeated word or a ’ token broke that).
  function nearMissCheck(row) {
    return (row.checks || []).filter(function (c) {
      return c.name === 'quote_match' && c.result === 'fail' && c.reason &&
             c.reason.indexOf('nearly matches') >= 0;
    })[0] || null;
  }
  // the Fix-dialog pre-fill: the check's near_match, verbatim; old runs whose
  // checks carry no near_match get no pre-fill (null)
  function nearMissPrefill(row) {
    var chk = nearMissCheck(row);
    if (!chk || typeof chk.near_match !== 'string' || !chk.near_match) return null;
    return { field: 'quote', to: chk.near_match };
  }
  // the sub-line under a judgment-list row: the current projected issue says
  // why review is needed; the state's mechanical reason is the fallback.
  // A generic "not printed" reason loses to the check's own near-miss sentence
  function listReasonLine(row, t) {
    var shown = (t.issues && t.issues.length ? t.issues[0] : null) || t.reason || '';
    var nm = nearMissCheck(row);
    if (nm && /not printed/i.test(shown)) shown = nm.reason;
    return shown.length > 72 ? shown.slice(0, 72) + '…' : shown;
  }

  // U37: keep a code-removed row anyway — the same reason box as Remove (the
  // code's reason is pre-filled, the person can edit it), saving records a
  // person keep that outranks the code exclusion.
  // DIALOGS-001: the decision carries the expected_basis the person SAW (the
  // server's review-basis digest at mount); a material change under the open
  // box renews the confirmation instead of writing. `keepDraft` is a draft to
  // restore across that renew (the person's words survive).
  function keepAnywayDialog(row, keepDraft) {
    var x = exclusionFor(row.id) || codeExclusionHistory(row.id);
    if (!x) { closeDialog(); return; }
    clear(dialogHost);
    dialogHost.append(el('div', 'sh-t', 'Keep this row anyway'));
    dialogHost.append(el('div', 'sh-s', row.ticker + ' · ' + row.title));
    dialogHost.append(el('h4', null, 'Why you keep it (required — it is the recorded reason)'));
    var note = noteField('dnote', 'e.g. It is an upcoming event the rule misjudged.');
    note.value = keepDraft !== undefined ? keepDraft : (x.reason || RULE_WORDS[x.rule] || '');
    dialogHost.append(note);
    var br = el('div', 'btnrow');
    br.style.marginTop = '14px';
    br.append(btn('btn pri', 'Keep anyway', function () {
      if (!note.value.trim()) { showDialogError('A reason is required — it is the recorded reason.'); return; }
      // the proposal changed under the open box: no write — put the current
      // wording on screen, keep the draft, and ask for a fresh confirmation
      if (dialogMount && dialogMount.stale) {
        var fresh = rowOf(row.id) || row;
        dialogMount = { key: currentRunId() + '/' + fresh.id + '/keepanyway',
                        basis: basisOf(fresh.id), basisKey: basisDigest(fresh.id), stale: false };
        keepAnywayDialog(fresh, note.value);
        return;
      }
      postDecision(row.id, { target: row.id, action: 'keep', note: note.value.trim(),
                             expected_basis: dialogMount ? dialogMount.basis : null },
        'Kept by you — code had removed it.', { screen: 'audit' });
    }), btn('btn', 'Cancel', function () {
      navigate(dialogHome(row, { runId: currentRunId(), screen: 'audit', rowId: row.id }));
    }));
    dialogHost.append(br);
    dialogWrap.hidden = false;
    note.focus();
  }

  function removeDialog(row, prefillNote) {
    clear(dialogHost);
    dialogHost.append(el('div', 'sh-t', 'Remove this row'));
    dialogHost.append(el('div', 'sh-s', row.ticker + ' · ' + row.title));
    dialogHost.append(el('h4', null, 'Why it is removed (required — it is the recorded reason)'));
    var note = noteField('dnote', 'e.g. Not an upcoming event.');
    if (prefillNote) note.value = prefillNote;
    dialogHost.append(note);
    var br = el('div', 'btnrow');
    br.style.marginTop = '14px';
    br.append(btn('btn danger', 'Remove', function () {
      if (!note.value.trim()) { showDialogError('A reason is required — it is the recorded reason.'); return; }
      postDecision(row.id, { target: row.id, action: 'remove', note: note.value.trim(),
                             expected_basis: dialogMount ? dialogMount.basis : null },
        'Removed by you — moved to checked and removed.');
    }), btn('btn', 'Cancel', function () {
      navigate(dialogHome(row, { runId: currentRunId(), screen: 'judgment', rowId: row.id }));
    }));
    dialogHost.append(br);
    dialogWrap.hidden = false;
    note.focus();
  }

  function fixDialog(row, prefill) {
    var t = rowTarget(row);
    clear(dialogHost);
    dialogHost.append(el('div', 'sh-t', 'Fix the row'));
    dialogHost.append(el('div', 'sh-s', row.ticker + ' · ' + row.title));
    dialogHost.append(el('h4', null, 'Field'));
    var sel = el('select', 'fld');
    FIX_FIELDS.forEach(function (f) {
      var o = el('option', null, FIELD_WORDS[f] || f);
      o.value = f;
      sel.append(o);
    });
    // U23: a voice proposal pre-fills the field it names
    if (prefill && prefill.field && FIX_FIELDS.indexOf(prefill.field) >= 0) sel.value = prefill.field;
    dialogHost.append(sel);
    dialogHost.append(el('h4', null, 'From (the current value)'));
    var from = el('input', 'fld');
    from.readOnly = true;
    dialogHost.append(from);
    dialogHost.append(el('h4', null, 'To'));
    var to = el('input', 'fld');
    dialogHost.append(to);
    function cur() { return (t.values && t.values[sel.value] !== undefined) ? t.values[sel.value] : null; }
    function placeholderFor(field) {
      if (field === 'date' || field === 'date_end') return 'YYYY-MM-DD';
      if (field === 'date_text') return 'The date or period as printed, e.g. Q3 2027';
      return 'The corrected ' + (FIELD_WORDS[field] || field).toLowerCase();
    }
    function show() {
      from.value = cur() === null || cur() === undefined ? '(none)' : String(cur());
      to.placeholder = placeholderFor(sel.value);
    }
    sel.addEventListener('change', show);
    show();
    if (prefill && prefill.to !== undefined && prefill.to !== null) to.value = String(prefill.to);
    function validateFix() {
      if (!to.value.trim() && cur() !== null) return 'A new value is required.';
      if ((sel.value === 'date' || sel.value === 'date_end') && !/^\d{4}-\d{2}-\d{2}$/.test(to.value.trim())) {
        return (FIELD_WORDS[sel.value] || sel.value) + ' must be written as YYYY-MM-DD.';
      }
      return null;
    }
    var br = el('div', 'btnrow');
    br.style.marginTop = '14px';
    // G1-2 (ruling): when the re-check cannot confirm the fix, the dialog shows
    // the code's reason as a red line and stays open; "Keep anyway" accepts the
    // standing fix and the row takes the "Fixed by you · still open" path
    var keepAnyway = null;
    // G1-R2 / Codex P2: each failed-re-check save records a fix server-side —
    // Cancel must discard every fix THIS dialog saved (never one that predated
    // it), so the saves are counted and undone one by one
    var saves = 0;
    // Codex P2: while a save POST is in flight the fix is not recorded yet —
    // Cancel must wait for the answer (disabled), and a fix that lands after
    // the dialog was closed (Esc) is undone, never orphaned
    var savePending = false;
    var cancelBtn = btn('btn', 'Cancel', function () {
      if (savePending) return;
      // G1-R2 / Codex P2: Cancel discards every fix this dialog saved, undoing
      // them newest-first; nothing is recorded until Save fix passes or Keep
      // anyway. With no save yet, Cancel only closes the dialog.
      if (saves > 0) {
        var n = saves;
        toast(n === 1 ? 'The fix was discarded.' : 'The fixes were discarded.');
        var undoOne = function (left) {
          if (left <= 0) {
            navigate(dialogHome(row, { runId: currentRunId(), screen: 'judgment', rowId: row.id }));
            return refresh();
          }
          return A.decide(currentRunId(), { target: row.id, action: 'undo', note: null })
            .then(function (res) {
              if (!res.ok) {
                toast((res.body && res.body.detail) || ('HTTP ' + res.status));
                return null;
              }
              return undoOne(left - 1);
            });
        };
        undoOne(n);
      } else {
        navigate(dialogHome(row, { runId: currentRunId(), screen: 'judgment', rowId: row.id }));
      }
    });
    br.append(btn('btn pri', 'Save fix', function () {
      var problem = validateFix();
      if (problem) { showDialogError(problem); return; }
      var note = (document.getElementById('qnote') || {}).value || null;
      var body = { target: row.id, action: 'fix', note: note || null,
                   fix: { field: sel.value, from: cur(), to: to.value },
                   expected_basis: dialogMount ? dialogMount.basis : null };
      var old = dialogHost.querySelector('.errline');
      if (old) old.remove();
      var pendEl = el('div', 'pendline', 'Re-check pending…');
      var br2 = dialogHost.querySelector('.btnrow');
      dialogHost.insertBefore(pendEl, br2);
      if (pending) return;
      pending = true;
      savePending = true;
      cancelBtn.disabled = true;
      A.decide(currentRunId(), body).then(function (res) {
        pending = false;
        savePending = false;
        cancelBtn.disabled = false;
        pendEl.remove();
        if (!res.ok) {
          showDialogError((res.body && res.body.detail) || ('HTTP ' + res.status));
          // UI-BASIS-OWN-SAVE / DIALOGS-001: a 409 means the row's material
          // changed outside this dialog — the red line discloses it, then the
          // refresh adopts the current basis so the person's NEXT save is
          // judged against what is on screen; never an auto-submit
          if (res.status === 409) {
            refresh().then(function () {
              if (dialogMount && dialogMount.key === currentRunId() + '/' + row.id + '/fix') {
                dialogMount.basis = basisOf(row.id);
              }
            });
          }
          return;
        }
        var st = res.body.state;
        // UI-BASIS-OWN-SAVE: this dialog's OWN accepted save changed the
        // proposal, so the basis it mounted is stale — adopt the returned
        // current review_basis before the next edit, or every further save
        // 409s forever against a basis the box outgrew
        if (dialogMount && dialogMount.key === currentRunId() + '/' + row.id + '/fix' &&
            res.body.state && res.body.state.review_basis) {
          dialogMount.basis = res.body.state.review_basis;
        }
        saves += 1;
        var fx = (st.fixes || []).slice(-1)[0];
        var rcRes = st.recheck && st.recheck.result;
        if (rcRes && rcRes !== 'pass') {
          if (dialogWrap.hidden) {
            // the dialog was closed (Esc) while the save was in flight — the
            // fix landed with nobody to show the red line to: undo it
            A.decide(currentRunId(), { target: row.id, action: 'undo', note: null })
              .then(function (u) {
                if (u.ok) toast('The fix was discarded.');
                else toast('The fix could not be discarded. Open the row and press Undo.');
              }).catch(function () {
                toast('The fix could not be discarded. Open the row and press Undo.');
              });
            return;
          }
          // the re-check did not confirm the fix: red line inside the dialog,
          // dialog stays open — the person fixes again, cancels, or keeps anyway.
          // The reason may repeat the outcome ("The re-check failed: …") — the
          // word "failed" appears once (G1-R2)
          var reason = st.recheck.reason ? st.recheck.reason.replace(/^The re-check failed: /, '') : '';
          var why = 'Re-checked after the fix: ' + checkResultWord(rcRes) +
                    (reason ? ' — ' + reason : '');
          showDialogError(why);
          if (!keepAnyway) {
            keepAnyway = btn('btn', 'Keep anyway', function () {
              navigate(dialogHome(row, { runId: currentRunId(), screen: 'judgment', rowId: row.id }));
              return refresh().then(function () {
                toast(st.group === 'corrected'
                  ? 'The fix stands — Fixed by you · still open.'
                  : 'The fix stands — the row is still open.');
              });
            });
            br.append(keepAnyway);
          }
          return;
        }
        var what = 'Re-checked after the fix: ' + checkResultWord(st.recheck && st.recheck.result);
        if (fx && fx.edit_without_error) what += ' — ' + NO_ERROR;
        else if (fx && fx.mistake) what += ' — counted as a mistake caught by you.';
        else if (fx && fx.not_counted_reason && !fx.mistake) what += ' — ' + fx.not_counted_reason;
        // the dialog closes with the hash; the re-check result shows on the row's pane
        navigate(dialogHome(row, { runId: currentRunId(), screen: 'judgment', rowId: row.id }));
        return refresh().then(function () { toast(what); });
      }).catch(function () {
        pending = false;
        savePending = false;
        cancelBtn.disabled = false;
        pendEl.remove();
        showDialogError('The server is not reachable.');
      });
    }));
    br.append(cancelBtn);
    dialogHost.append(br);
    dialogWrap.hidden = false;
    to.focus();
  }

  // ---------------------------------------------------------------- what changed (U7)
  // Compare two runs of the same label family: rows added, removed, corrected
  // and date moves between the two run files. A card says which run it came
  // from and, below, the person's later decision on that row, dated.
  function labelFamily(label) {
    var l = label || '';
    return l.replace(/^\s*Run\s+\d+\s*(?:·\s*)?/, '') || l;
  }
  function runShort(r) {
    // G6-3: dates in words everywhere a run is named
    return (r.label || r.id) + ' · ' + dateShort(r.started_at) + ' ' + (r.started_at || '').slice(11, 16);
  }

  // G4-1: every run is named the same way everywhere on this screen:
  // 'Run N · day Mon · label'
  function runPick(r) {
    var s = r.started_at || '';
    var mon = MONTHS_SHORT[parseInt(s.slice(5, 7), 10) - 1] || '';
    var day = s.slice(8, 10);
    return 'Run ' + (r.number || '?') + ' · ' + (day ? day + ' ' + mon : 'no date') +
           ' · ' + (r.label || r.id);
  }

  // U34b: the run What changed opens against — the nearest comparable run:
  // the closest earlier run that read the same page set, else the previous run
  // of the same label, else the newest other run. Page sets load in the
  // background (pageSigs); until then the label rule answers.
  var pageSigs = {}, pageSigsLoading = false;
  function pageSig(x) {
    var keys = Object.keys(pageSet(x).set).sort();
    return keys.length ? keys.join(',') : null;
  }
  function nearestComparable(bId, others) {
    var bM = runs.filter(function (r) { return r.id === bId; })[0] || {};
    var bn = bM.number || 0;
    function nearest(list) {
      var older = list.filter(function (r) { return (r.number || 0) < bn; });
      return (older[0] || list[0] || {}).id;
    }
    var sameSet = pageSigs[bId] ? others.filter(function (r) { return pageSigs[r.id] === pageSigs[bId]; }) : [];
    if (sameSet.length) return nearest(sameSet);
    var fam = labelFamily(bM.label);
    var sameLabel = others.filter(function (r) { return labelFamily(r.label) === fam; });
    if (sameLabel.length) return nearest(sameLabel);
    return (others[0] || {}).id;
  }
  function loadPageSigs(ids) {
    var need = ids.filter(function (id) { return !(id in pageSigs); });
    if (!need.length || pageSigsLoading) return;
    pageSigsLoading = true;
    Promise.all(need.map(function (id) {
      return A.run(id).then(function (d) { pageSigs[id] = pageSig({ run: d }); },
                            function () { pageSigs[id] = null; });
    })).then(function () {
      pageSigsLoading = false;
      if (route && route.screen === 'changed' && !route.a) renderChanged();
    });
  }

  function renderChanged() {
    var meta = detail.run.run || {};
    var sub = document.getElementById('changed-sub');
    var pairHost = clear(document.getElementById('changed-pairs'));
    var host = clear(document.getElementById('changed-rows'));
    var mid = clear(document.getElementById('changed-mid'));
    var det = clear(document.getElementById('changed-det'));

    // G4-R6: one picker everywhere — the two drop-downs offer ANY two saved
    // runs on every run; the three-tab row is gone
    var curId = currentRunId();
    var pickFrom = runs.slice();
    if (runs.length < 2) {
      sub.textContent = 'This is the only run saved here.';
      changedCountCache = 0;
      document.getElementById('ct-changed').textContent = '0';
      host.append(el('div', 'empty', 'Nothing to compare yet — run the same companies again and the two runs can be compared.'));
      mid.append(el('div', 'empty', 'Nothing to compare.'));
      det.append(el('div', 'empty', 'Pick a change to see it.'));
      return;
    }

    function valid(id) { return id && pickFrom.some(function (m) { return m.id === id; }); }
    var bId, aId;
    var others = runs.filter(function (r) { return r.id !== curId; })
      .sort(function (x, y) { return (y.number || 0) - (x.number || 0); });
    bId = valid(route.b) ? route.b : curId;
    if (!valid(route.a)) loadPageSigs(others.map(function (r) { return r.id; }).concat([bId]));
    aId = valid(route.a) ? route.a : nearestComparable(bId, others);
    var aM = runs.filter(function (r) { return r.id === aId; })[0];
    var bM = runs.filter(function (r) { return r.id === bId; })[0];
    // G4-2a: the count beside the cards is substance only, with the wording
    // fold on its own line
    var suffix = '';
    if (changedCache && changedCache.key === aId + '>' + bId) {
      suffix = changedCache.note + ' · ' + changedCache.cards.length + ' change' +
               (changedCache.cards.length === 1 ? '' : 's') +
               (changedCache.wording ? ' · ' + changedCache.wording + ' row' +
                (changedCache.wording === 1 ? '' : 's') + ' reworded' : '') +
               (changedCache.since ? ' · ' + changedCache.since + ' decided by you since' : '');
    }
    if (changedCache && changedCache.key === aId + '>' + bId && changedCache.cards.pageNote) {
      suffix += ' · ' + changedCache.cards.pageNote;
    }
    sub.textContent = 'Compare: ' + runPick(aM) + ' → ' + runPick(bM) + suffix;

    // G4-R6: the two drop-downs, on every run
    var selA = el('select', 'fld runpick');
    var selB = el('select', 'fld runpick');
    pickFrom.forEach(function (m) {
      var oa = el('option', null, runPick(m)); oa.value = m.id;
      var ob = el('option', null, runPick(m)); ob.value = m.id;
      selA.append(oa); selB.append(ob);
    });
    selA.value = aId; selB.value = bId;
    function repick() { navigate({ runId: currentRunId(), screen: 'changed', a: selA.value, b: selB.value }); }
    selA.addEventListener('change', repick);
    selB.addEventListener('change', repick);
    var lbl = el('div', 'chpicks');
    lbl.append(el('span', 'chpicklbl', 'Compare'), selA, el('span', 'chpicklbl', 'against'), selB);
    pairHost.append(lbl);

    var key = aId + '>' + bId;
    if (changedCache && changedCache.key === key) return renderChangedCards(host, mid, det);
    changedCache = null;
    host.append(el('div', 'empty', 'Reading the two runs…'));
    Promise.all([A.run(aId), A.run(bId), A.decisions(aId), A.decisions(bId)]).then(function (all) {
      var ra = all[0], rb = all[1];
      var note = '';
      // G4-1: runs of different families compare on the companies they share
      if (labelFamily(ra.run.label) !== labelFamily(rb.run.label)) {
        var ca = (ra.run.choices && ra.run.choices.companies) || [];
        var cb = (rb.run.choices && rb.run.choices.companies) || [];
        var shared = ca.filter(function (c) { return cb.indexOf(c) >= 0; });
        if (shared.length) {
          ra = { rows: ra.rows.filter(function (r) { return shared.indexOf(r.ticker) >= 0; }),
                 run: ra.run, sources: ra.sources, mistakes: ra.mistakes, tables: ra.tables,
                 state: ra.state };
          rb = { rows: rb.rows.filter(function (r) { return shared.indexOf(r.ticker) >= 0; }),
                 run: rb.run, sources: rb.sources, mistakes: rb.mistakes, tables: rb.tables,
                 state: rb.state };
          note = ' · comparing the ' + shared.length + ' compan' + (shared.length === 1 ? 'y' : 'ies') +
                 ' both runs read';
        }
      }
      var cards = compareRuns({ run: ra, state: ra.state, decisions: all[2] },
                              { run: rb, state: rb.state, decisions: all[3] });
      changedCache = { key: key, a: { run: ra, state: ra.state, decisions: all[2] },
                       b: { run: rb, state: rb.state, decisions: all[3] }, note: note,
                       wording: cards.wording || 0, wordingRows: cards.wordingRows || [],
                       since: cards.since || 0, unmatched: cards.unmatched || 0, cards: cards };
      changedCountCache = changedCache.cards.length;
      document.getElementById('ct-changed').textContent = String(changedCountCache);
      if (route && route.screen === 'changed') renderChanged();
    }).catch(function () {
      host.append(el('div', 'empty', 'The two runs could not be read.'));
    });
  }

  function rowVals(x, row) {
    var t = x.state.targets[row.id] || {};
    return t.values || {};
  }
  // Codex P2 (#112): the state value wins on KEY PRESENCE — a value a person
  // cleared ('' or null) must show as cleared, never fall back to the raw row;
  // the raw row is only the answer when the state has no value for the field
  function shownVal(cv, raw, k) {
    if (cv && Object.prototype.hasOwnProperty.call(cv, k)) return cv[k];
    return undefined;
  }
  // Codex P2 (#113): a corrected row's effective checks are the re-check's
  // 'after' set — the saved row.checks can be stale (a saved run is never
  // rebuilt); only when no re-check ran do the saved checks speak
  function effectiveChecks(row, t) {
    return (t && t.effective_checks) || [];
  }
  function fmtField(field, value) {
    if (value === null || value === undefined || value === '') return '(none)';
    return field === 'event_type' ? eventWord(value) : String(value);
  }

  // G9-3: rows pair by event identity — same ticker + program (normalised:
  // case, punctuation, whitespace) + event kind + SAME PERIOD START; then the
  // one fallback: same ticker + same kind with title word overlap >= 0.6.
  // Never by position, never by loose field scores. A row with no confident
  // partner is listed 'Only in' its own run, never a guessed Added/Removed pair.
  function normWords(s) {
    return String(s == null ? '' : s).toLowerCase()
      .replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
  }
  function pairKey(r) {
    return (r.ticker || '') + '|' + normWords(r.program) + '|' +
           (r.event_type || '') + '|' + (r.date || '');
  }
  // word overlap of two titles: shared words over the longer title, 0..1
  function titleSim(ra, rb) {
    var wa = normWords(ra.title).split(' ').filter(function (w) { return w; });
    var wb = normWords(rb.title).split(' ').filter(function (w) { return w; });
    if (!wa.length || !wb.length) return 0;
    var inB = {};
    wb.forEach(function (w) { inB[w] = true; });
    var hit = 0;
    wa.forEach(function (w) { if (inB[w]) hit++; });
    return hit / Math.max(wa.length, wb.length);
  }

  // U34b: the pages a run read, as 'source:page' keys
  function pageSet(x) {
    var set = {}, n = 0;
    ((x.run.sources) || []).forEach(function (src) {
      (src.pages || []).forEach(function (p) { set[src.id + ':' + p.page_number] = true; n++; });
    });
    return { set: set, n: n };
  }
  function rowPageKey(r) { return r.source_id + ':' + r.page_number; }

  function compareRuns(a, b) {
    // U34b: when the runs read different page counts, a row from a page the
    // other run did not read is not 'Only in' — an unread page is not a change
    var pgA = pageSet(a), pgB = pageSet(b);
    function extraKeys(x, y) { return Object.keys(x.set).filter(function (k) { return !y.set[k]; }).length; }
    var extraA = extraKeys(pgA, pgB), extraB = extraKeys(pgB, pgA);
    // page SETS, not counts: equal counts of different pages still differ
    var pagesDiffer = pgA.n > 0 && pgB.n > 0 && (extraA > 0 || extraB > 0);
    function unreadBy(pg, r) { return pagesDiffer && r.source_id != null && !pg.set[rowPageKey(r)]; }
    // U34 +1 (Red Team F9): pairing reads the STATE values — a fix already
    // shows on the audit values and must drive the identity here too, never
    // the raw saved row
    var aByKey = {};
    a.run.rows.forEach(function (r) {
      var k = pairKey(rowVals(a, r)); (aByKey[k] = aByKey[k] || []).push(r);
    });
    // identity pass
    var used = {}, pairs = [], fallbackPairs = [], onlyB = [];
    b.run.rows.forEach(function (rb) {
      var vb = rowVals(b, rb);
      var q = (aByKey[pairKey(vb)] || []).filter(function (ra) { return !used[ra.id]; });
      if (q.length) { used[q[0].id] = true; pairs.push([q[0], rb]); return; }
      onlyB.push(rb);
    });
    // fallback pass: same ticker + same kind + title overlap >= 0.6
    var restB = onlyB.filter(function (rb) {
      var vb = rowVals(b, rb);
      var best = null, bestSim = 0.6;
      a.run.rows.forEach(function (ra) {
        if (used[ra.id]) return;
        var va = rowVals(a, ra);
        if ((va.ticker || '') !== (vb.ticker || '')) return;
        if ((va.event_type || '') !== (vb.event_type || '')) return;
        // U34b: two rows whose program (or indication) are both set and differ
        // are different events — shared title boilerplate never pairs them
        if (va.program && vb.program && normWords(va.program) !== normWords(vb.program)) return;
        if (va.indication && vb.indication && normWords(va.indication) !== normWords(vb.indication)) return;
        var s = titleSim(va, vb);
        if (s >= bestSim) { bestSim = s; best = ra; }
      });
      if (!best) return true;
      used[best.id] = true;
      fallbackPairs.push([best, rb]);
      return false;
    });
    var unmatched = [];
    var stillA = [];
    a.run.rows.forEach(function (r) { if (!used[r.id]) stillA.push(r); });

    var cards = [];
    var wording = 0;
    var wordingRows = [];          // G4-R7: the pairs that differ in wording only
    var quietB = {};               // G4-R5: paired rows that produced no card
    function since(x, row) {
      var t = x.state.targets[row.id] || {};
      if (!t.decided_by_person) return null;
      var d = lastPersonDecisionIn(x.decisions || [], row.id);
      var word = ACTION_WORDS[d ? d.action : t.last_action] || 'decided';
      return { text: 'Since then: ' + word + ' by you' + (d ? ' · ' + dateShort(d.at) : ''),
                   run: x };
    }
    var FIELDS = ['date', 'date_end', 'date_text', 'title', 'event_type', 'company', 'ticker',
                  'program', 'indication'];
    // G9-3: rows with no confident partner sit in their own group, both runs'
    // rows listed — never guessed into a pair
    // U34b: unmatched rows list per run ('Only in <run>'); no claim of a change
    function unmatchedCard(row, x, side) {
      return { kind: 'unmatched', side: side, row: row, x: x, fields: [],
               line: 'Only in ' + (x.run.run.label || (side === 'a' ? 'the older run' : 'the newer run')) + '.',
               since: since(x, row) };
    }
    restB.forEach(function (rb) { if (!unreadBy(pgA, rb)) unmatched.push(unmatchedCard(rb, b, 'b')); });
    stillA.forEach(function (ra) { if (!unreadBy(pgB, ra)) unmatched.push(unmatchedCard(ra, a, 'a')); });
    pairs.forEach(function (pr) {
      var ra = pr[0], rb = pr[1];
      var diffs = [];
      FIELDS.forEach(function (f) {
        var va = rowVals(a, ra)[f], vb = rowVals(b, rb)[f];
        if (String(va === null || va === undefined ? '' : va) !== String(vb === null || vb === undefined ? '' : vb)) {
          diffs.push({ field: f, from: va, to: vb });
        }
      });
      if (!diffs.length) { quietB[rb.id] = true; return; }
      // G4-2a: substance only. "Date moved" when date or date_end changed;
      // "corrected" when program, indication or type changed ("type changed"
      // when it is the type); a difference only in the wording of the title or
      // the date text folds into one count line, never a card — and the rows
      // are listed under "Show the N reworded rows" (G4-R7)
      var moved = diffs.some(function (d) { return d.field === 'date' || d.field === 'date_end'; });
      var corrected = diffs.some(function (d) {
        return d.field === 'program' || d.field === 'indication' || d.field === 'event_type';
      });
      if (!moved && !corrected) { wording++; quietB[rb.id] = true; wordingRows.push({ ra: ra, rb: rb, diffs: diffs }); return; }
      var lines = diffs.map(function (d) {
        return (FIELD_WORDS[d.field] || d.field) + ': ' + fmtField(d.field, d.from) + ' → ' + fmtField(d.field, d.to);
      });
      var typeOnly = !moved && corrected &&
        diffs.some(function (d) { return d.field === 'event_type'; }) &&
        !diffs.some(function (d) { return d.field === 'program' || d.field === 'indication'; });
      cards.push({ kind: moved ? 'moved' : 'corrected', row: rb, x: b, fields: diffs,
                   word: typeOnly ? 'type changed' : null,
                   line: lines.join(' · '), since: since(b, rb) });
    });
    // G9-3: fallback pairs matched on title alone — a date difference there is
    // never a "date moved" (only identity pairs move dates); when dates are
    // all that differs the pair is not confident and both rows join the group
    fallbackPairs.forEach(function (pr) {
      var ra = pr[0], rb = pr[1];
      var diffs = [];
      FIELDS.forEach(function (f) {
        var va = rowVals(a, ra)[f], vb = rowVals(b, rb)[f];
        if (String(va === null || va === undefined ? '' : va) !== String(vb === null || vb === undefined ? '' : vb)) {
          diffs.push({ field: f, from: va, to: vb });
        }
      });
      if (!diffs.length) { quietB[rb.id] = true; return; }
      var substantive = diffs.some(function (d) {
        return d.field === 'program' || d.field === 'indication' || d.field === 'event_type';
      });
      if (!substantive) {
        unmatched.push(unmatchedCard(ra, a, 'a'));
        unmatched.push(unmatchedCard(rb, b, 'b'));
        return;
      }
      var lines = diffs.map(function (d) {
        return (FIELD_WORDS[d.field] || d.field) + ': ' + fmtField(d.field, d.from) + ' → ' + fmtField(d.field, d.to);
      });
      cards.push({ kind: 'corrected', row: rb, x: b, fields: diffs,
                   line: lines.join(' · '), since: since(b, rb) });
    });
    // G4-R5: a row the person decided after run B used to vanish from this
    // screen when nothing changed between the runs — it now gets its own
    // 'Since then' card in its own group
    var bEnd = (b.run.run.finished_at || b.run.run.started_at || '');
    var sinceOnly = [];
    if (bEnd) b.run.rows.forEach(function (rb) {
      if (!quietB[rb.id]) return;
      var t = b.state.targets[rb.id] || {};
      if (!t.decided_by_person) return;
      var d = lastPersonDecisionIn(b.decisions || [], rb.id);
      if (!d || !d.at || d.at <= bEnd) return;
      var word = ACTION_WORDS[d.action] || 'decided';
      sinceOnly.push({ kind: 'since', row: rb, x: b, fields: [],
        line: 'No change between the runs.',
        since: { text: 'Since then: ' + word + ' by you · ' + dateShort(d.at), run: b } });
    });
    cards = cards.concat(unmatched).concat(sinceOnly);
    cards.unmatched = unmatched.length;
    cards.since = sinceOnly.length;
    var order = { corrected: 0, moved: 1, unmatched: 2, since: 3 };
    cards.sort(function (x, y) {
      if (x.kind === 'unmatched' && y.kind === 'unmatched') return (x.side === 'b' ? 0 : 1) - (y.side === 'b' ? 0 : 1);
      return order[x.kind] - order[y.kind];
    });
    cards.onlyB = unmatched.filter(function (c) { return c.side === 'b'; }).length;
    cards.onlyA = unmatched.filter(function (c) { return c.side === 'a'; }).length;
    if (pagesDiffer) {
      var parts = [];
      if (extraA > 0) parts.push('Run ' + (a.run.run.number || '?') + ' read ' + extraA + ' pages the other run did not');
      if (extraB > 0) parts.push('Run ' + (b.run.run.number || '?') + ' read ' + extraB + ' pages the other run did not');
      cards.pageNote = parts.join('; ') + '; unread pages are not changes';
    }
    cards.wording = wording;
    cards.wordingRows = wordingRows;
    return cards;
  }

  var CHG_WORDS = { corrected: 'corrected', moved: 'date moved',
                    unmatched: 'only in one run', since: 'since then' };
  var CHG_CLASS = { corrected: 'st-corrected', moved: 'st-undecided',
                    unmatched: 'st-undecided', since: 'st-kept' };

  function chRewordedFold() {
    // G4-R7: the wording-only rows are openable, not just counted
    var rows = changedCache.wordingRows || [];
    if (!rows.length) return null;
    var fold = el('details', 'fold chwordfold');
    fold.append(el('summary', null, 'Show the ' + rows.length + ' reworded row' +
                                  (rows.length === 1 ? '' : 's')));
    rows.forEach(function (w) {
      var line = el('div', 'chword');
      var bits = w.diffs.map(function (d) {
        return '“' + fmtField(d.field, d.from) + '” → “' + fmtField(d.field, d.to) + '”';
      });
      line.append(el('span', 'tk', w.rb.ticker || '—'));
      line.append(el('span', 'wt', (w.rb.title || '(no title)') + ' — ' + bits.join(' · ')));
      fold.append(line);
    });
    return fold;
  }

  function renderChangedCards(host, mid, det) {
    var cards = changedCache.cards;
    if (!cards.length) {
      var same = 'The two runs propose the same rows — nothing changed between them.';
      if (changedCache.wording) {
        same = 'No real changes — ' + changedCache.wording + ' row' +
               (changedCache.wording === 1 ? '' : 's') + ' reworded between the runs (wording only).';
      }
      if (changedCache.cards.pageNote) host.append(el('div', 'chpagenote', changedCache.cards.pageNote));
      host.append(el('div', 'empty', same));
      var wf = chRewordedFold();
      if (wf) host.append(wf);
      mid.append(el('div', 'empty', 'Nothing changed.'));
      det.append(el('div', 'empty', 'Pick a change to see it.'));
      return;
    }
    if (chgSel === null || !cards[chgSel]) chgSel = 0;
    var sinceGrp = false, unGrp = false, unGrpA = false;
    function onlyLabel(side) {
      var x = side === 'a' ? changedCache.a : changedCache.b;
      return x.run.run.label || (side === 'a' ? 'the older run' : 'the newer run');
    }
    if (changedCache.cards.pageNote) host.append(el('div', 'chpagenote', changedCache.cards.pageNote));
    cards.forEach(function (c, i) {
      // G9-3: the rows with no confident partner sit under their own group line
      if (c.kind === 'unmatched' && c.side === 'b' && !unGrp) {
        unGrp = true;
        host.append(el('div', 'grp', 'Only in ' + onlyLabel('b') + ' · ' + changedCache.cards.onlyB));
      }
      if (c.kind === 'unmatched' && c.side === 'a' && !unGrpA) {
        unGrpA = true;
        host.append(el('div', 'grp', 'Only in ' + onlyLabel('a') + ' · ' + changedCache.cards.onlyA));
      }
      // G4-R5: the since-then cards open with their own group line + count
      if (c.kind === 'since' && !sinceGrp) {
        sinceGrp = true;
        host.append(el('div', 'grp', 'Since then · ' + changedCache.since + ' decided by you'));
      }
      var b = btn('chcard' + (i === chgSel ? ' on' : ''));
      var top = el('div', 'top');
      top.append(el('span', 'chtag ' + CHG_CLASS[c.kind], c.word || CHG_WORDS[c.kind]));
      // the card shows the STATE values — a fix already made is the value
      // that compares, never the raw saved row (Red Team A-F9)
      var cv = rowVals(c.x, c.row);
      var cvTicker = shownVal(cv, c.row, 'ticker');
      top.append(el('span', 'tk mono', (cvTicker === '' || cvTicker === null || cvTicker === undefined) ? '—' : cvTicker));
      // G4-2b: company and program ride with the title on the card
      var cvCo = shownVal(cv, c.row, 'company');
      if (cvCo) top.append(el('span', 'co', cvCo));
      var cvPr = shownVal(cv, c.row, 'program');
      if (cvPr) top.append(el('span', 'pg2', cvPr));
      var cvTt = shownVal(cv, c.row, 'title');
      top.append(el('span', null, cvTt || '(no title)'));
      b.append(top);
      b.append(el('div', 'why', c.line));
      if (c.since) b.append(el('div', 'since', c.since.text));
      b.append(el('div', 'when', 'from ' + runShort({ label: c.x.run.run.label, started_at: c.x.run.run.started_at })));
      b.addEventListener('click', function () {
        chgSel = i;
        renderChanged();
      });
      host.append(b);
    });
    var wf2 = chRewordedFold();
    if (wf2) host.append(wf2);

    var c = cards[chgSel];
    mid.append(el('div', 'empty', 'Pick a change on the left.'));
    clear(det);
    var sec = el('div', 'sec');
    sec.append(el('span', 'chtag ' + CHG_CLASS[c.kind], c.word || CHG_WORDS[c.kind]));
    var cvHead = rowVals(c.x, c.row);
    var ttl = el('div', 'ttl', shownVal(cvHead, c.row, 'title') || '(no title)');
    ttl.style.marginTop = '8px';
    sec.append(ttl);
    det.append(sec);
    var sec2 = el('div', 'sec');
    function kv(k, v) { var r = el('div', 'kv'); r.append(el('span', 'k', k), el('span', 'v', v)); sec2.append(r); }
    var cvDet = rowVals(c.x, c.row);
    var dTicker = shownVal(cvDet, c.row, 'ticker');
    var dCo = shownVal(cvDet, c.row, 'company');
    kv('Company', ((dTicker === null || dTicker === undefined) ? '' : dTicker) + ' · ' +
       ((dCo === null || dCo === undefined) ? '' : dCo));
    var dTy = shownVal(cvDet, c.row, 'event_type');
    kv('Type', dTy ? eventWord(dTy) : '(none)');
    c.fields.forEach(function (d) {
      kv(FIELD_WORDS[d.field] || d.field, fmtField(d.field, d.from) + ' → ' + fmtField(d.field, d.to));
    });
    if (!c.fields.length) kv('Date as written', (rowVals(c.x, c.row).date_text || '(none)'));
    kv('Came from', c.x.run.run.label || '');
    det.append(sec2);
    if (c.since) {
      var sec3 = el('div', 'sec');
      sec3.append(el('div', 'since', c.since.text));
      det.append(sec3);
    }
    // G4-4: the pane carries the row's checks and the source / trail links,
    // exactly like the Audit log pane
    det.append(checksSection({ checks: c.row.checks || [] }, rowVals(c.x, c.row) || {}));
    var sec4 = el('div', 'sec');
    var br = el('div', 'btnrow');
    var rid = c.x.run.run.id;
    br.append(btn('btn', 'Open source page', function () {
      openInRun(rid, { runId: rid, screen: 'source', srcId: c.row.source_id,
                       page: 'p' + c.row.page_number, valueId: c.row.id, back: 'audit' });
    }));
    br.append(btn('btn', 'Open agent trail', function () {
      openInRun(rid, { runId: rid, screen: 'trail', rowId: c.row.id });
    }));
    br.append(btn('btn pri', 'Open in ' + (c.x.run.run.label || 'that run'), function () {
      openInRun(rid, { runId: rid, screen: 'audit', rowId: c.row.id });
    }));
    sec4.append(br);
    det.append(sec4);
  }

  // G4-4: jump to another run, then land on the row — highlighted and scrolled
  // into view by the post-render scroll
  function openInRun(rid, nav) {
    if (detail && detail.id === rid) navigate(nav);
    else { switchRun(rid); setTimeout(function () { navigate(nav); }, 400); }
  }

  // ---------------------------------------------------------------- export (U9)
  // The saved report files for the run, the build note, and the report's
  // summary numbers from state (the same counts as the audit log).
  var REPORT_BUILD_ENDPOINT = null;   // no build endpoint on main yet; command line it is
  function reportFileNames(meta) {
    var base = 'biotech-coverage-catalyst-calendar-run' + (meta.number || 0);
    return [{ file: base + '.pdf', kind: 'PDF' }, { file: base + '.docx', kind: 'Word' }];
  }
  function reportUrl(name) {
    return '/runs/' + encodeURIComponent(currentRunId()) + '/report/' +
      name.split('/').map(encodeURIComponent).join('/');
  }
  // D3 / L1: the server's reason sentence on a refused report file travels in
  // the HEAD response's X-Report-Reason header (a HEAD body never reaches the
  // browser — header only, no body parsing).
  function reportRefusalReason(res) {
    var fromHeader = res.headers &&
      (res.headers.get('x-report-reason') || res.headers.get('x-refuse-reason'));
    return Promise.resolve(fromHeader ? String(fromHeader) : null);
  }

  function renderExport() {
    var meta = detail.run.run || {};
    var st = detail.state;
    var mid = clear(document.getElementById('exp-mid'));
    var det = clear(document.getElementById('exp-det'));

    var head = el('div', 'ph');
    head.style.background = 'var(--panel2)';
    head.append(el('h2', null, 'Export'));
    head.append(el('div', 'sub', 'The saved report and the run’s summary'));
    head.append(el('div', 'runlabel' + (meta.sample ? ' sample' : ''), meta.label || ''));
    mid.append(head);

    var wrap = el('div', 'calwrap');
    wrap.append(el('div', 'sec2-t', 'Saved report files'));
    var reportsStale = !!detail.run.report_stale;
    if (reportsStale) {
      wrap.append(el('div', 'hint', detail.run.report_stale_reason ||
        'This run changed after the saved report. The report is unavailable until it is rebuilt.'));
    }
    var listHost = el('div');
    reportFileNames(meta).forEach(function (f) {
      var row = el('div', 'exrow');
      row.append(el('span', 'exic', '▤'));
      var midCol = el('div');
      midCol.style.flex = '1';
      midCol.style.minWidth = '0';
      midCol.append(el('div', null, 'Biotech Coverage Catalyst Calendar — run ' + (meta.number || '?') +
        ' (' + f.kind + ')'));
      var nameLine = el('div', 'exmeta mono', f.file);
      midCol.append(nameLine);
      var right = el('span');
      right.style.marginLeft = 'auto';
      right.append(el('span', 'exmeta', 'checking…'));
      row.append(midCol, right);
      listHost.append(row);
      // served by the server at /runs/<id>/report/<file> when that route exists
      fetch(reportUrl(f.file), { method: 'HEAD' }).then(function (r) {
        clear(right);
        if (r.ok) {
          var a = el('a', 'link', 'Open');
          a.href = reportUrl(f.file);
          right.append(a);
          return null;
        }
        // D3: a refused file (unverified against the run state, out of date)
        // carries the server's own reason — that sentence is the honest line,
        // never 'No report is saved for this run yet'
        return reportRefusalReason(r).then(function (reason) {
          if (reason) {
            // L2: when the top-of-screen hint already carries this exact
            // sentence, say it once — the file row points at it
            var topReason = reportsStale && (detail.run.report_stale_reason || '');
            if (topReason && reason === topReason) {
              right.append(el('span', 'exmeta', 'Unavailable until rebuilt.'));
            } else {
              var box = el('div', 'exmeta');
              box.style.textAlign = 'right';
              box.append(el('div', null, reason));
              if (!/rebuild/i.test(reason)) box.append(el('div', null, 'Rebuild the report to open it here.'));
              right.append(box);
            }
          } else {
            // G5-2b: no dead label — the honest sentence until the file is served
            right.append(el('span', 'exmeta', reportsStale ? 'Saved report is out of date.' : 'No report is saved for this run yet.'));
          }
        });
      }).catch(function () {
        clear(right);
        right.append(el('span', 'exmeta', reportsStale ? 'Saved report is out of date.' : 'No report is saved for this run yet.'));
      });
    });
    wrap.append(listHost);
    // G5-2b: never a command line, never a run id — the footer sentence below
    // is the whole build note
    mid.append(wrap);
    var foot = el('div', 'expfoot', 'Built by code from the saved results');
    mid.append(foot);

    // the report's summary numbers: the same counts as the audit log
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Summary — the same counts as the audit log'));
    function kv(k, v) { var r = el('div', 'kv'); r.append(el('span', 'k', k), el('span', 'v', v)); sec.append(r); }
    // G5-3: the run's own spend and cost per kept row, in the summary
    function usd2(v) {
      return v >= 0.01 ? '$' + Number(v).toFixed(2) : money(v);
    }
    if (meta.spend_usd !== null && meta.spend_usd !== undefined) {
      var spendMark = unpricedMark(meta, detail.run.unknown_cost_accepted);
      kv('Spend', usd2(meta.spend_usd) + (spendMark ? ' (' + spendMark + ')' : ''));
    }
    if (st.cost_per_kept_row_usd !== null && st.cost_per_kept_row_usd !== undefined) {
      kv('Cost per kept row', usd2(st.cost_per_kept_row_usd));
    }
    // the same groups and counts as the audit log's facets — with the
    // projection the lines add up to every row (proposed rows on their own line)
    if (!reviewAxis()) {
      GROUP_ORDER.forEach(function (g) {
        kv(GROUP_NAMES[g], String(st.counts.rows[g]));
      });
    } else {
      // the same viewGroup counts as the facet strip — partial rows uncounted
      groupOrder().forEach(function (g) {
        kv(groupName(g), String(groupCount(g)));
      });
    }
    kv('Needs attention', String(st.unresolved));
    kv('Mistakes caught by code', String(st.mistakes.code));
    if (oneModelRun(meta)) kv('Second reader', NO_SECOND_READER);
    else kv('Mistakes caught by the second reader', String(st.mistakes.second_reader));
    kv('Mistakes caught by you', String(st.mistakes.person));
    kv('Excluded by code', String(st.excluded.code));
    kv('Excluded by you', String(st.excluded.person));
    det.append(sec);
    det.append(el('div', 'sec', ''));
  }

  // ---------------------------------------------------------------- new run (U8)
  var newrunWrap = document.getElementById('newrun-wrap');
  // Kimi 52: the selectable companies come from the server — the same rule a run's
  // [all] uses — never a hard-coded list. nrState: 'loading' until the fetch lands,
  // then 'ready' (nrCompanies set) or 'failed' (nrLoadError holds the plain sentence).
  var nrCompanies = null;
  var nrState = 'loading';
  var nrLoadError = '';
  var nrLoadSeq = 0;
  var NR_EMPTY = 'No company has a page to read yet.';
  // Addendum 2 (CVA): the Companies grid shows what YOU chose — prepared
  // tickers as ticked boxes (greyed with their reason when they could not be
  // read) and captured names you ticked. The old captured names stay available
  // unticked under a quiet 'Previously captured' heading. Nothing pre-ticked.
  var nrChosen = {};    // captured symbols the person ticked
  // prepared tickers the person unticked, keyed by symbol within the current
  // intake — a refused start or a keystroke must never re-tick them (the run
  // would silently include a company the person excluded)
  var nrPrepUnticked = {};
  // the prepared tickers for the box's current text (done intake only)
  function nrPreparedTickers() {
    var text = document.getElementById('nr-tickers').value.trim();
    var st = nrIntake.last;
    if (!text || !st || text !== nrIntake.text || st.status !== 'done') return [];
    return st.tickers || [];
  }
  function renderChosen() {
    var cos = clear(document.getElementById('nr-cos'));
    var any = false;
    nrPreparedTickers().forEach(function (tk) {
      any = true;
      if (tk.state === 'ready') {
        var l = el('label', 'tk2');
        var i = el('input');
        i.type = 'checkbox'; i.className = 'nr-ik'; i.value = tk.symbol;
        i.checked = !nrPrepUnticked[tk.symbol];
        l.append(i, document.createTextNode(' ' + tk.symbol + (tk.company ? ' · ' + tk.company : '')));
        i.addEventListener('change', function () {
          // the untick is state, not just DOM — it survives re-renders
          if (i.checked) delete nrPrepUnticked[tk.symbol];
          else nrPrepUnticked[tk.symbol] = true;
          nrError(''); syncStartButton();
        });
        cos.append(l);
      } else {
        // a ticker that could not be read: a greyed box with its plain reason
        cos.append(el('span', 'tk2 off', tk.symbol + ' — ' + (INTAKE_STATE_WORDS[tk.state] || tk.state) +
                      (tk.reason ? ' · ' + tk.reason : '')));
      }
    });
    // while the paste box holds text the run reads only the pasted tickers, so
    // a captured name you ticked shows unticked (and disabled): a ticked box is
    // always a company the run reads. The choice is kept in nrChosen and comes
    // back ticked when the box is cleared.
    var pastedNow = !!document.getElementById('nr-tickers').value.trim();
    (nrCompanies || []).forEach(function (t) {
      if (!nrChosen[t]) return;
      any = true;
      var l = el('label', 'tk2');
      var i = el('input');
      i.type = 'checkbox'; i.className = 'nr-co'; i.value = t; i.checked = !pastedNow;
      l.append(i, document.createTextNode(' ' + t));
      i.addEventListener('change', function () {
        if (i.checked) return;
        delete nrChosen[t];
        renderChosen();
        renderPrevCompanies(nrCompanies, nrPrevUnreadable);
      });
      cos.append(l);
    });
    if (!any) cos.append(el('div', 'hint', 'Tick a company below, or paste tickers above.'));
    applyPasteDisable();
    syncStartButton();
  }
  var nrPrevUnreadable = [];
  // the quiet 'Previously captured' list: available names, nothing pre-ticked
  function renderPrevCompanies(companies, unreadable) {
    nrPrevUnreadable = unreadable || [];
    nrCompanies = companies;
    var host = clear(document.getElementById('nr-prev'));
    document.getElementById('nr-prev-h').hidden = !companies.length;
    if (!companies.length) {
      host.append(el('div', 'hint', NR_EMPTY));
      renderChosen();
      return;
    }
    companies.forEach(function (t) {
      if (nrChosen[t]) return;   // chosen names live in the Companies grid
      var l = el('label', 'tk2');
      var i = el('input');
      i.type = 'checkbox'; i.className = 'nr-pc'; i.value = t; i.checked = false;
      l.append(i, document.createTextNode(' ' + t));
      i.addEventListener('change', function () {
        if (i.checked) { nrChosen[t] = true; nrError(''); }
        else delete nrChosen[t];
        renderChosen();
        renderPrevCompanies(companies, unreadable);
      });
      host.append(l);
    });
    // companies the server names but holds no page for: plain names, nothing to tick
    (unreadable || []).forEach(function (t) {
      host.append(el('span', 'hint nr-nopage', t + ' — No pages read yet'));
    });
    renderChosen();
  }
  function renderNewRunCompanies(companies, unreadable) {
    renderPrevCompanies(companies, unreadable);
  }
  function loadNewRunCompanies() {
    var seq = ++nrLoadSeq;
    nrCompanies = null; nrState = 'loading'; nrLoadError = '';
    // the old list never stays on screen while the new one loads
    clear(document.getElementById('nr-prev')).append(el('div', 'hint', 'Loading…'));
    function failed(msg) {
      if (seq !== nrLoadSeq) return;
      nrState = 'failed'; nrLoadError = msg;
      clear(document.getElementById('nr-prev'));
      // inline red line, no toast (the order's rule for dialog errors)
      nrError(msg);
    }
    return A.companies().then(function (res) {
      if (seq !== nrLoadSeq) return;
      if (!res.ok) {
        // the server answered: show its own sentence (a broken index is a 409 with one)
        failed('The company list could not be loaded. ' +
               ((res.body && typeof res.body.detail === 'string' && res.body.detail) || ('The server answered ' + res.status + '.')));
        return;
      }
      nrCompanies = res.body.companies || [];
      nrState = 'ready';
      nrError('');
      renderNewRunCompanies(nrCompanies, res.body.listed_without_pages || []);
      syncPasteMode();
    }).catch(function () {
      failed('The company list could not be loaded. The server is not reachable.');
    });
  }
  function nrError(msg) {
    var e = document.getElementById('nr-error');
    e.textContent = msg;
    e.hidden = !msg;
  }
  // ---------------- research instructions (#52): the box's default and limit
  // come from GET /api/settings (research_instructions_default /
  // research_instructions_max_length) — never a hard-coded default. Loaded
  // means a non-empty default AND a positive whole-number limit; anything
  // else refuses the start rather than run on an unseen default. Blank
  // starts the run with the displayed default; the hint says so in the open.
  var nrResearch = { defaultText: null, maxLength: null, loaded: false };
  function researchFromSettings(body) {
    var d = body && typeof body.research_instructions_default === 'string'
      ? body.research_instructions_default : null;
    var m = body && body.research_instructions_max_length;
    var maxOk = typeof m === 'number' && isFinite(m) && Math.floor(m) === m && m > 0;
    var loaded = !!d && maxOk;
    return { defaultText: loaded ? d : null, maxLength: maxOk ? m : null, loaded: loaded };
  }
  // the start buttons refuse until the settings are loaded — never an
  // unseen server default
  function researchReady() {
    if (nrResearch.loaded) return true;
    nrError('The default instructions could not be loaded. Close and open New run to try again.');
    return false;
  }
  // what a run receives: the box's text exactly as written, byte for byte;
  // whitespace-only chooses the displayed default
  function notePayload() {
    var raw = document.getElementById('nr-note').value;
    return raw.trim() === '' ? nrResearch.defaultText : raw;
  }
  // the hint under the box: the plain explanation, or how blank/over-long resolves
  function noteHintText(value, rs) {
    var text = value || '';
    if (!rs.loaded) return 'The default instructions could not be loaded. Close and open New run to try again.';
    if (rs.maxLength && text.length > rs.maxLength)
      return 'At most ' + rs.maxLength + ' characters — this is ' + text.length + '. Nothing is cut short.';
    if (!text.trim() && rs.defaultText) return 'Blank — the run uses the default instructions.';
    return 'Guides the research; the source checks still run on every row.';
  }
  function applyResearchSettings(body) {
    nrResearch = researchFromSettings(body);
    var el = document.getElementById('nr-note');
    // prefill from the server's default once; a person's edits never
    // overwritten — a delayed answer finds the box as the person left it
    if (nrResearch.loaded && nrResearch.defaultText && !el.value) el.value = nrResearch.defaultText;
    updateNoteHint();
  }
  function updateNoteHint() {
    document.getElementById('nr-note-hint').textContent =
      noteHintText(document.getElementById('nr-note').value, nrResearch);
  }
  document.getElementById('nr-note').addEventListener('input', function () { nrError(''); updateNoteHint(); });
  // ---------------- LETTUCE-OPTION (CVA: no toggle): the hallucination check on
  // the wording is not a choice. It is SENT ON EVERY START when the server
  // (/api/research/options) says it is available; otherwise nothing is sent.
  // The fifth line under "Checks on every run" says which, in plain words.
  var nrSupport = { loaded: false, available: false, reason: null, choices: [] };
  // NR-002: while a fresh options request is in flight a start sends nothing —
  // a stale availability answer never ships.
  // NR-003: only the newest request may apply (the company-list loader's rule).
  var nrSupportSeq = 0;
  var nrSupportPending = false;
  var SUPPORT_LINE_ON = '✓ Hallucination check on the wording (advisory — a flag changes no check, count or decision)';
  var SUPPORT_LINE_OFF = '– Hallucination check: not set up on this computer';
  var SUPPORT_LINE_UNKNOWN = '– Hallucination check: availability could not be loaded. Close and open New run to try again.';
  function supportFromOptions(body) {
    var o = body && body.support_check;
    var choices = o && Array.isArray(o.choices) ? o.choices : null;
    if (!choices || !choices.length) return { loaded: false, available: false, reason: null, choices: [] };
    return { loaded: true, available: o.available === true,
             reason: o.unavailable_reason || null, choices: choices };
  }
  function applySupportOptions(body) {
    nrSupportPending = false;    // the accepted answer ends the pending window
    nrSupport = supportFromOptions(body);
    var line = document.getElementById('nr-support-line');
    // a stale tooltip never survives a later answer — the title is per-answer;
    // the server's reason (env var names and all) stays on hover, off the screen
    line.removeAttribute('title');
    line.textContent = !nrSupport.loaded ? SUPPORT_LINE_UNKNOWN
      : nrSupport.available ? SUPPORT_LINE_ON : SUPPORT_LINE_OFF;
    if (!nrSupport.loaded || !nrSupport.available) {
      if (nrSupport.reason) line.title = nrSupport.reason;
    }
    line.hidden = false;
  }
  // what a start sends: the server's choice word whenever the check is
  // available — the person has no toggle. Unavailable, not loaded, or a refresh
  // still in flight: nothing is sent (the server's own off).
  function nrSupportValue() {
    if (nrSupportPending) return null;
    if (nrSupport.loaded && nrSupport.available &&
        nrSupport.choices.indexOf('lettucedetect') >= 0) return 'lettucedetect';
    return null;
  }
  // the sheet's open handler: refresh the wording-check availability every open,
  // the way the company list does — seq-guarded, and pending (nothing sent)
  // until the fresh answer applies
  function refreshSupportOptions() {
    nrSupportPending = true;
    var seq = ++nrSupportSeq;
    return A.researchOptions().then(function (o) {
      if (seq !== nrSupportSeq) return;    // an older answer never overrides
      applySupportOptions(o);
    });
  }
  // ---------------- ticker intake (TICKER-INTAKE-001): paste any list; the
  // server resolves each symbol against SEC records and captures its documents.
  // The run starts from the intake with the ready names only — the failures
  // stay listed beside it. Every state word is plain; the reason is the
  // server's own sentence.
  var INTAKE_STATE_WORDS = {
    resolving: 'Finding the company', capturing: 'Capturing documents',
    ready: 'Ready', unresolved: 'Could not be resolved', no_documents: 'No documents found',
    unsupported: 'Not supported', failed: 'Preparation failed'
  };
  // text: the pasted text the intake was prepared from; usedId: the intake a
  // run has already started from (it offers no second Continue)
  var nrIntake = { id: null, seq: 0, timer: null, last: null, text: null, usedId: null, usedRun: null };
  // one start in flight at a time, from either button (a double click sends one POST)
  var nrBusy = false;
  var NR_PASTE_ONLY = 'This run reads only the pasted tickers. Clear the box to choose from the captured names instead.';
  var NR_PREPARE_FIRST = 'Prepare the pasted tickers first, or clear the box.';
  // NEWRUN-ONE-START-001: ONE start button at the bottom. With the paste box
  // empty it is 'Run now' (the captured names ticked above). With text it is
  // 'Continue with N ready' and only wakes when the intake for that text is
  // done — it is never active-looking while it would refuse. While preparing,
  // 'Preparing… (n of N done)' rides beside it.
  var INTAKE_FINISHED = ('ready unresolved no_documents unsupported failed').split(' ');
  // the box's situation: null (box empty -> 'Run now'), 'none' (text pasted
  // but no intake prepared for it), 'preparing', or the finished record
  function intakeDoneN() {
    var text = document.getElementById('nr-tickers').value.trim();
    if (!text) return null;
    var st = nrIntake.last;
    if (!st || text !== nrIntake.text) return 'none';
    return st.status === 'done' ? st : 'preparing';
  }
  function syncStartButton() {
    var btn = document.getElementById('nr-go');
    var prep = document.getElementById('nr-preparing');
    var st = intakeDoneN();
    if (st === null) {
      // the start counts the ticked captured names and never offers an active
      // button that would refuse at zero
      var n = document.querySelectorAll('.nr-co:checked').length;
      btn.textContent = 'Run now · ' + n + ' compan' + (n === 1 ? 'y' : 'ies');
      btn.disabled = !!nrBusy || !n;
      prep.hidden = true;
    } else if (st === 'none') {
      // pasted but not prepared (or the text changed): nothing to continue —
      // the button sleeps until Prepare finishes, it never reads 'Run now'
      btn.textContent = 'Continue with 0 ready';
      btn.disabled = true;
      prep.hidden = true;
    } else if (st === 'preparing') {
      var fin = (nrIntake.last.tickers || []).filter(function (t) {
        return INTAKE_FINISHED.indexOf(t.state) >= 0;
      }).length;
      btn.textContent = 'Continue with ' + nrIntake.last.ready_count + ' ready';
      btn.disabled = true;
      prep.hidden = false;
      prep.textContent = 'Preparing… (' + fin + ' of ' + (nrIntake.last.requested || []).length + ' done)';
    } else {
      // addendum 2: the button counts the boxes you ticked
      var n = tickedReadyCount(st);
      btn.textContent = 'Continue with ' + n + ' ready';
      btn.disabled = !!st.run_id || st.intake_id === nrIntake.usedId || !n || !!nrBusy;
      prep.hidden = true;
    }
  }
  // the prepared ready tickers whose box is ticked
  function tickedReadyCount(st) {
    var ticked = {};
    document.querySelectorAll('.nr-ik:checked').forEach(function (c) { ticked[c.value] = true; });
    return (st.tickers || []).filter(function (t) { return t.state === 'ready' && ticked[t.symbol]; }).length;
  }
  // Operator ruling: when the paste box has any text, the run reads only the
  // pasted tickers — the captured-name boxes are disabled and one plain line
  // says so; the start button becomes 'Continue with N ready'. With the box
  // empty everything is as before. The intake list shows only while the box
  // holds the text it was prepared from.
  // while the paste box holds text the run reads only the pasted tickers —
  // every captured box (chosen or previously captured) stays disabled
  function applyPasteDisable() {
    var pasted = !!document.getElementById('nr-tickers').value.trim();
    document.querySelectorAll('.nr-pc, .nr-co, #nr-all').forEach(function (c) { c.disabled = pasted; });
  }
  function syncPasteMode() {
    var text = document.getElementById('nr-tickers').value.trim();
    var pasted = !!text;
    renderChosen();
    syncStartButton();
    var note = document.getElementById('nr-paste-note');
    note.textContent = pasted ? NR_PASTE_ONLY : '';
    note.hidden = !pasted;
    var out = document.getElementById('nr-intake-out');
    if (!pasted || text !== nrIntake.text) out.hidden = true;
    else if (out.children && out.children.length) out.hidden = false;
  }
  function stopIntakePoll() {
    if (nrIntake.timer) clearInterval(nrIntake.timer);
    nrIntake.timer = null;
  }
  // the starting material captured for every ticker, in plain words, once per intake
  // "Captured starting material: the first 2 pages of each 8-K press release filed 30 Jul 2026 to 28 Oct 2026 (90 days)."
  // Research mode runs the agent loop after preparation: the intake is the captured
  // starting material, not what the run "reads" — the agent goes on from there.
  // INTAKE-PAGE-CAP-001: preparation captures only the first pages of each document;
  // an intake without the cap recorded keeps the older line
  function intakeScopeLine(st) {
    if (!st || typeof st.cutoff !== 'string' || typeof st.window_start !== 'string') return '';
    var from = dayMonth(st.window_start), to = dayMonth(st.cutoff);
    if (!from || !to) return '';
    var forms = (st.form_scope || []).join(', ') || '8-K';
    var days = (typeof st.lookback_days === 'number') ? ' (' + st.lookback_days + ' days)' : '';
    var cap = st.pages_per_document;
    var what = (typeof cap === 'number' && cap > 0)
      ? 'the first ' + (cap === 1 ? 'page' : cap + ' pages') + ' of each ' + forms + ' press release'
      : forms + ' press releases';
    return 'Captured starting material: ' + what + ' filed ' + from + ' ' + st.window_start.slice(0, 4) +
           ' to ' + to + ' ' + st.cutoff.slice(0, 4) + days + '.';
  }
  function renderIntakeStatus(host, st) {
    clear(host);
    host.hidden = false;
    (st.tickers || []).forEach(function (tk) {
      var row = el('div', 'ik-row');
      var head = el('div', 'ik-head');
      head.append(el('b', null, tk.symbol || ''));
      if (tk.company) head.append(el('span', 'ik-co', tk.company));
      head.append(el('span', 'ik-st ik-' + tk.state, INTAKE_STATE_WORDS[tk.state] || tk.state));
      row.append(head);
      // the server's own plain sentence: what is happening, or why it failed
      if (tk.reason) row.append(el('div', 'ik-why', tk.reason));
      host.append(row);
    });
    var scope = intakeScopeLine(st);
    if (scope) host.append(el('div', 'ik-scope', scope));
    if (st.status === 'done') {
      var foot = el('div', 'ik-foot');
      foot.append(el('span', 'ik-counts', st.ready_count + ' ready' +
        (st.failed_count ? ' · ' + st.failed_count + ' could not be prepared' : '')));
      var isUsed = !!st.run_id || (!!st.intake_id && st.intake_id === nrIntake.usedId);
      var usedRun = (typeof st.run_id === 'string' && st.run_id) || (isUsed && nrIntake.usedRun) || '';
      if (isUsed) {
        // a run already started from this intake (here, in another tab, or a
        // lost answer): no second start from it, and a link to that run
        host.append(foot);
        var used = el('div', 'ik-scope', 'A run was started from these tickers.');
        if (usedRun) {
          var open = el('a', 'link', 'Open that run');
          open.href = '#';
          open.addEventListener('click', function (e) {
            if (e && e.preventDefault) e.preventDefault();
            newrunWrap.hidden = true;
            switchRun(usedRun);
          });
          used.append(document.createTextNode(' '), open);
        }
        host.append(used);
      } else {
        // NEWRUN-ONE-START-001: no second start button here — the ONE start
        // button at the bottom becomes 'Continue with N ready'
        host.append(foot);
      }
    }
    return host;
  }
  function pollIntake(seq) {
    return A.intakeStatus(nrIntake.id).then(function (res) {
      if (seq !== nrIntake.seq) return;
      if (!res.ok) {
        stopIntakePoll();
        nrError('The intake status could not be read. ' +
                ((res.body && typeof res.body.detail === 'string' && res.body.detail) ||
                 ('The server answered ' + res.status + '.')));
        return;
      }
      nrIntake.last = res.body;
      renderIntakeStatus(document.getElementById('nr-intake-out'), res.body);
      syncPasteMode();
      if (res.body.status === 'done') stopIntakePoll();
    }).catch(function () { /* the next poll retries; the last render stays */ });
  }
  function startIntake() {
    var raw = document.getElementById('nr-tickers').value.trim();
    nrError('');
    if (!raw) { nrError('Paste at least one ticker first.'); return; }
    stopIntakePoll();
    var seq = ++nrIntake.seq;
    nrIntake.last = null;
    nrIntake.text = raw;
    nrPrepUnticked = {};
    var out = document.getElementById('nr-intake-out');
    clear(out).append(el('div', 'hint', 'Preparing — finding the companies and capturing documents…'));
    out.hidden = false;
    return A.intake(raw).then(function (res) {
      if (seq !== nrIntake.seq) return;
      if (!res.ok) {
        out.hidden = true;
        nrError('The tickers could not be prepared. ' +
                ((res.body && typeof res.body.detail === 'string' && res.body.detail) ||
                 ('The server answered ' + res.status + '.')));
        return;
      }
      nrIntake.id = res.body.intake_id;
      nrIntake.timer = setInterval(function () { pollIntake(seq); }, 1500);
      return pollIntake(seq);
    }).catch(function () {
      out.hidden = true;
      nrError('The server is not reachable.');
    });
  }
  // ONE-MODEL-RUN-001: 'one' = Muse Spark only, 'all' = checked by a second
  // reader (the default); both Run now and Continue send it
  function nrModels() {
    var one = document.getElementById('nr-models-one');
    return one && one.checked ? 'one' : 'all';
  }
  // Continue with N ready: the run reads only this intake's captured pages —
  // the ready names; accept_ready_subset when a requested name is left out
  function startRunFromIntake(st, btn) {
    if (nrBusy) return;
    if ((st.intake_id && st.intake_id === nrIntake.usedId) || st.run_id) return;
    if (!researchReady()) return;
    var ticked = {};
    document.querySelectorAll('.nr-ik:checked').forEach(function (c) { ticked[c.value] = true; });
    var ready = (st.tickers || []).filter(function (t) { return t.state === 'ready' && ticked[t.symbol]; })
                                   .map(function (t) { return t.symbol; });
    if (!ready.length) { nrError('Tick at least one prepared ticker.'); return; }
    var limit = null;
    var limitRaw = document.getElementById('nr-limit').value.trim();
    if (limitRaw !== '') {
      limit = Number(limitRaw);
      // Red Team round 8: 'Infinity' and '1e309' are not numbers of dollars
      if (!Number.isFinite(limit) || limit < 0) {
        nrError('The spending limit must be a number of dollars, zero or more.');
        return;
      }
    }
    nrError('');
    nrBusy = true;
    if (btn) btn.disabled = true;
    syncPasteMode();
    function done() {
      nrBusy = false;
      if (btn) btn.disabled = !st.ready_count;
      syncPasteMode();
    }
    // research is the only mode the screen starts: the page reader's choices
    // (event types, window, reader tools, second-reader models) stay home —
    // the server rejects them in research mode
    return A.newRun({ intake_id: st.intake_id, companies: ready,
               accept_ready_subset: st.requested.some(function (s) { return ready.indexOf(s) < 0; }),
               mode: 'research',
               spending_limit_usd: limit,
               note_for_agent: notePayload(),
               support_check: nrSupportValue() }).then(function (res) {
      if (!res.ok && res.status === 409 && res.body && typeof res.body.run_id === 'string') {
        // the server already bound a run to this intake: it is used, link to that run
        nrBusy = false;
        nrIntake.usedId = st.intake_id;
        nrIntake.usedRun = res.body.run_id;
        renderIntakeStatus(document.getElementById('nr-intake-out'), st);
        syncPasteMode();
        return;
      }
      if (!res.ok) {
        done();
        nrError((res.body && typeof res.body.detail === 'string' && res.body.detail) ||
                ('The server answered ' + res.status + '.'));
        return;
      }
      stopIntakePoll();
      // the intake is used: redraw without Continue, the failures stay listed
      nrIntake.usedId = st.intake_id;
      nrIntake.usedRun = res.body.id;
      nrBusy = false;
      renderIntakeStatus(document.getElementById('nr-intake-out'), st);
      syncPasteMode();
      newrunWrap.hidden = true;
      var id = res.body.id;
      toast((res.body.label || 'Run ' + res.body.number) + ' — started.');
      // PRE-REVIEW-001: open the accepted run by its returned id — a failed
      // saved-runs refresh afterwards is refresh housekeeping, never a
      // failed start, and never re-opens the door to a second paid run
      switchRun(id);
      return A.runs().then(function (list) {
        runs = list;
      }).catch(function () { /* the list refresh failed; the run stays open */ });
    }, function () { done(); nrError('The server is not reachable.'); });
  }
  document.getElementById('nr-intake-go').addEventListener('click', startIntake);
  document.getElementById('nr-tickers').addEventListener('input', syncPasteMode);
  document.getElementById('newrun').addEventListener('click', function () {
    nrError('');
    newrunWrap.hidden = false;
    loadNewRunCompanies();
    // reopening keeps the last intake result (the failures stay listed)
    if (nrIntake.last) renderIntakeStatus(document.getElementById('nr-intake-out'), nrIntake.last);
    syncPasteMode();
    // retry the research settings when they never loaded — that is the retry
    // the hint promises; a loaded default is never overwritten
    if (!nrResearch.loaded) A.settings().then(function (s) { applyResearchSettings(s); });
    else updateNoteHint();
    // the wording-check availability: refresh each open, like the company list —
    // pending (nothing sent on a start) until the fresh options answer
    refreshSupportOptions();
  });
  document.getElementById('nr-cancel').addEventListener('click', function () { newrunWrap.hidden = true; });
  // G4-6: one wording in both limit boxes, and the red line clears as soon as
  // the box is edited
  document.getElementById('nr-limit').addEventListener('input', function () { nrError(''); });
  newrunWrap.addEventListener('click', function (e) { if (e.target === newrunWrap) newrunWrap.hidden = true; });
  // Run now: the captured names ticked above; refused while the paste box has text
  function startRunNow() {
    if (nrBusy) return;
    if (!researchReady()) return;
    var pastedText = document.getElementById('nr-tickers').value.trim();
    if (pastedText) {
      nrError(pastedText === nrIntake.text && nrIntake.last ? NR_PASTE_ONLY : NR_PREPARE_FIRST);
      return;
    }
    // the list comes from the server: nothing selectable until it lands
    if (nrState === 'failed') { nrError(nrLoadError + ' Close this and open New run again to try again.'); return; }
    if (nrState !== 'ready') { nrError('The company list is still loading.'); return; }
    if (!nrCompanies.length) { nrError(NR_EMPTY); return; }
    var companies = [];
    document.querySelectorAll('.nr-co:checked').forEach(function (c) { companies.push(c.value); });
    if (!companies.length) { nrError('Choose at least one company.'); return; }
    var limitRaw = document.getElementById('nr-limit').value.trim();
    var limit = null;
    if (limitRaw !== '') {
      limit = Number(limitRaw);
      // Red Team round 8: 'Infinity' and '1e309' are not numbers of dollars — refuse
      // anything that is not finite (Number.isFinite), never send the request
      if (!Number.isFinite(limit) || limit < 0) { nrError('The spending limit must be a number of dollars, zero or more.'); return; }
    }
    var note = notePayload();
    nrError('');
    nrBusy = true;
    syncPasteMode();
    function done() { nrBusy = false; syncPasteMode(); }
    // research is the only mode the screen starts; the page reader's choices
    // (event types, window, reader tools, second-reader models) stay home
    return A.newRun({ companies: companies, mode: 'research',
               spending_limit_usd: limit, note_for_agent: note,
               support_check: nrSupportValue() }).then(function (res) {
      done();
      if (!res.ok) { nrError((res.body && res.body.detail) || ('HTTP ' + res.status)); return; }
      newrunWrap.hidden = true;
      var id = res.body.id;
      toast((res.body.label || 'Run ' + res.body.number) + ' — started.');
      // PRE-REVIEW-001: the accepted run opens by its returned id even when
      // the saved-runs refresh fails afterwards — that is never a failed start
      switchRun(id);
      return A.runs().then(function (list) {
        runs = list;
      }).catch(function () { /* the list refresh failed; the run stays open */ });
    }, function () { done(); nrError('The server is not reachable.'); });
  }
  // NEWRUN-ONE-START-001: the one start button — 'Continue with N ready' once
  // the pasted intake is done, 'Run now' with the box empty
  function nrStartClick() {
    var st = intakeDoneN();
    if (st && st !== 'preparing') startRunFromIntake(st, document.getElementById('nr-go'));
    else startRunNow();
  }
  document.getElementById('nr-go').addEventListener('click', nrStartClick);
  // the clear close (Esc and Cancel keep working)
  document.getElementById('nr-close').addEventListener('click', function () { newrunWrap.hidden = true; });

  // the job bar: status, turns and spend from run.json while a run goes, and the
  // stop reason with a Resume when it pauses
  var jobPoll = { id: null, timer: null };
  function stopJobPoll() {
    if (jobPoll.timer) clearInterval(jobPoll.timer);
    jobPoll = { id: null, timer: null };
  }
  function startJobPoll(id) {
    if (jobPoll.id === id) return;
    stopJobPoll();
    jobPoll.id = id;
    jobPoll.timer = setInterval(function () {
      A.run(id).then(function (body) {
        var st = (body.run || {}).status;
        if (detail && detail.id === id) {
          detail = { id: id, run: body, state: body.state };
          decisionsCache = { id: null, lines: null };
          renderShell();
          render();
        }
        if (JOB_ACTIVE.indexOf(st) < 0) {
          stopJobPoll();
          A.runs().then(function (list) { runs = list; updateSavedRunsBtn(); });
        }
      }).catch(function () { stopJobPoll(); });
    }, 2500);
  }
  function ensureJobPoll() {
    if (!detail) return;
    var st = (detail.run.run || {}).status;
    if (st && JOB_ACTIVE.indexOf(st) >= 0) startJobPoll(detail.id);
    else if (jobPoll.id && (!detail || detail.id !== jobPoll.id ||
             JOB_ACTIVE.indexOf((detail.run.run || {}).status) < 0)) stopJobPoll();
  }
  // G4-5: the job bar reads money in cents, one way ($5.06, never $5.055042)
  function cents(v) { return '$' + Number(v).toFixed(2); }

  // Run 9: an active research run can finish searching into review, or stop
  // without publishing. Both actions are explicit and confirmed; ended runs
  // render neither control.
  function researchRunControls(meta, live) {
    var researchMode = (meta.choices || {}).mode === 'research' || !!meta.research;
    if (!live || !researchMode) return null;
    var err = el('span', 'errline', '');
    err.hidden = true;
    function act(kind) {
      var finishing = kind === 'finish';
      var question = finishing
        ? 'Stop searching and review what was found? The review and checks still run, then the run is published.'
        : 'Stop now? Nothing is reviewed or published.';
      if (!window.confirm(question)) return;
      var request = finishing ? A.researchFinish(currentRunId()) : A.researchStop(currentRunId());
      request.then(function (res) {
        if (!res.ok) { err.textContent = (res.body && res.body.detail) || ('HTTP ' + res.status); err.hidden = false; return; }
        err.hidden = true;
        toast(finishing ? 'Finishing run.' : 'Run stopped.');
        startJobPoll(currentRunId());
        return refresh();
      }).catch(function () { err.textContent = 'The server is not reachable.'; err.hidden = false; });
    }
    var ctl = el('div', 'jb-ctl research-controls');
    ctl.append(btn('btn', 'Finish now', function () { act('finish'); }),
               btn('btn', 'Stop and discard', function () { act('stop'); }), err);
    return ctl;
  }

  // G4-R3: the job block lives in the rail run card. Live = a pulsing dot +
  // 'running'; paused = 'needs input' + the stop sentence in the warm box +
  // limit box and Resume; done = 'done'; no recorded status = 'saved run' + cost.
  function updateRunCardJob() {
    var host = document.getElementById('runcard-job');
    if (!host) return;
    clear(host);
    if (!detail) { host.hidden = true; return; }
    var meta = detail.run.run || {};
    var st = meta.status;
    if (!st) {
      // G4-R2: the block shows on every run — with no status it is a quiet
      // 'saved run' plus the cost when known, no dot
      var bits = ['saved run'];
      var cw = costPerKeptRowWords();
      if (cw) bits.push(cw);
      host.append(el('div', 'jb-sub', bits.join(' · ')));
      // fix-on-find: the status-less path returns early, so it carries the
      // per-company research progress itself (7c0c3b4 dropped it here)
      var rsc0 = researchCompanyBlock(runResearch() && runResearch().companies);
      if (rsc0) host.append(rsc0);
      host.hidden = false;
      return;
    }
    var live = JOB_ACTIVE.indexOf(st) >= 0;
    var row1 = el('div', 'jb-row1' + (live ? ' live' : ''));
    if (live) row1.append(el('span', 'dot'));
    // G8-8: a finished run says what is left to review, with a grey dot while
    // anything waits and a green dot at zero
    if (st === 'done') {
      var left = (detail.state && detail.state.unresolved) || 0;
      row1.append(el('span', 'dot' + (left > 0 ? ' grey' : '')));
      // PROJECTION-004: a research run is not independently reviewed by finishing —
      // at zero unresolved it says nothing is waiting, never that nothing needs review
      var researchMode = (meta.choices || {}).mode === 'research' || !!meta.research;
      row1.append(el('span', null, 'Run finished · ' +
        (left > 0 ? left + ' need review'
                  : (researchMode ? 'no rows waiting on you' : 'nothing left to review'))));
    } else {
      row1.append(el('span', null, JOB_STATUS_WORDS[st] || st));
    }
    host.append(row1);
    // PROJECTION-002: the one-reader sentence is a page-mode sentence — a
    // research run's independent reviewer is stage S2, named from the record
    if (oneModelRun(meta) && meta.choices.mode !== 'research') {
      host.append(el('div', 'jb-sub', ONE_MODEL_LINE));
    }
    var rsReview = researchReviewLine();
    if (rsReview) host.append(el('div', 'jb-sub', rsReview));
    var bits = [];
    if (meta.turn_count !== null && meta.turn_count !== undefined) {
      bits.push('turn ' + meta.turn_count + ' of ' + (meta.turn_limit || '?'));
    }
    if (meta.spend_usd !== null && meta.spend_usd !== undefined) {
      bits.push(meta.choices && meta.choices.spending_limit_usd !== null && meta.choices.spending_limit_usd !== undefined
        ? cents(meta.spend_usd) + ' of ' + cents(meta.choices.spending_limit_usd)
        : cents(meta.spend_usd) + ' used');
    }
    if (bits.length) host.append(el('div', 'jb-sub', bits.join(' · ')));
    var outcomeView = (detail.state || {}).page_outcomes || {};
    if (outcomeView.recorded) {
      var oc = outcomeView.counts;
      host.append(el('div', 'jb-sub', 'Pages · captured ' + oc.captured + ' · attempted ' + oc.attempted +
        ' · read ' + oc.read + ' · unreadable ' + oc.unreadable + ' · skipped ' + oc.skipped + ' · unread ' + oc.unread));
      if (outcomeView.by_company.length) {
        var by = el('details', 'jb-sub');
        by.append(el('summary', null, 'Page outcomes by company'));
        outcomeView.by_company.forEach(function (c) {
          by.append(el('div', null, c.ticker + ' · captured ' + c.captured + ' · attempted ' + c.attempted +
            ' · read ' + c.read + ' · unreadable ' + c.unreadable + ' · skipped ' + c.skipped + ' · unread ' + c.unread));
        });
        host.append(by);
      }
    } else {
      host.append(el('div', 'jb-sub', 'Page outcomes not recorded for this saved run.'));
    }
    // the stop sentence wears the warm box only when it needs you; a done
    // run's 'Read N pages' summary is plain text
    if (meta.status_reason) {
      host.append(el('div', JOB_PAUSABLE.indexOf(st) >= 0 ? 'jb-stop' : 'jb-sub', jobReason(st, meta, detail.run.unknown_cost_accepted)));
    }
    var researchCtl = researchRunControls(meta, live);
    if (researchCtl) host.append(researchCtl);
    if (JOB_PAUSABLE.indexOf(st) >= 0) {
      var limitInput = null;
      if (st === 'needs_input') {
        limitInput = el('input', 'fld');
        limitInput.placeholder = 'new limit $';
        limitInput.inputMode = 'decimal';
      }
      var err = el('span', 'errline', '');
      err.hidden = true;
      // G4-6: the error line clears as soon as the box is edited
      if (limitInput) limitInput.addEventListener('input', function () { err.hidden = true; });
      var ctl = el('div', 'jb-ctl');
      if (limitInput) ctl.append(limitInput);
      ctl.append(btn('btn', 'Resume', function () {
        var body = {};
        if (limitInput && limitInput.value.trim() !== '') {
          var v = Number(limitInput.value.trim());
          // Red Team round 8: refuse anything that is not finite, with the same red line
          if (!Number.isFinite(v) || v < 0) { err.textContent = 'The spending limit must be a number of dollars, zero or more.'; err.hidden = false; return; }
          body.spending_limit_usd = v;
        }
        A.resume(currentRunId(), body).then(function (res) {
          if (!res.ok) { err.textContent = (res.body && res.body.detail) || ('HTTP ' + res.status); err.hidden = false; return; }
          err.hidden = true;
          toast('Resumed.');
          startJobPoll(currentRunId());
          return refresh();
        }).catch(function () { err.textContent = 'The server is not reachable.'; err.hidden = false; });
      }), err);
      host.append(ctl);
    }
    // per-company research progress rides every state — queued, running, done
    var rsc = researchCompanyBlock(runResearch() && runResearch().companies);
    if (rsc) host.append(rsc);
    host.hidden = false;
  }
  // the money rule: under a cent reads in words, a cent or more in cents
  function costPerKeptRowWords() {
    var cpkr = detail.state.cost_per_kept_row_usd;
    if (cpkr === null || cpkr === undefined) return null;
    return cpkr < 0.01 ? 'less than a cent per kept row' : usdCents(cpkr) + ' per kept row';
  }

  // G8-1: run.json keeps whatever sentence the run stopped with, so old runs
  // still carry the figures-and-raw-id version. Only that one stored sentence —
  // a reason STARTING 'Stopped at the spending limit' — is normalized to the
  // plain words. The unpriced-call pause also names the spending limit and a
  // turn-limit pause can coincide with spend at the limit; neither is a limit
  // stop and both pass through untouched.
  var JOB_LIMIT_STOP = 'Stopped at the spending limit. Raise the limit and press Resume to read more.';
  // Job 51e: the unpriced-call marker, the same two strings as server/jobs.py
  // unpriced_marker — 'cost unknown' until the person accepts every unpriced
  // call, then 'limit bounds counted spend'. '' when no call is unpriced.
  // `accepted`: GET /api/runs/{id}'s unknown_cost_accepted (from job.json).
  function unpricedMark(meta, accepted) {
    var n = meta.unpriced_calls;
    if (typeof n !== 'number' || !(n > 0)) return '';
    var acc = typeof accepted === 'number' && accepted > 0 ? accepted : 0;
    return n + ' unpriced call(s); ' + (acc >= n ? 'limit bounds counted spend' : 'cost unknown');
  }
  function jobReason(st, meta, accepted) {
    var sr = meta.status_reason;
    if (JOB_PAUSABLE.indexOf(st) >= 0 &&
        typeof sr === 'string' && sr.indexOf('Stopped at the spending limit') === 0) {
      sr = JOB_LIMIT_STOP;      // Job 51e: no early return — the unpriced marker still follows
    } else {
      // G8-5: 'Read 114 pages: 96 rows, 0 values.' — the values count is a code
      // word to a first-time reader; the plain summary is 'Read N pages · M rows'
      var m = /^Read ([\d,]+) pages?: ([\d,]+) rows?, [\d,]+ values\.(.*)$/.exec(sr || '');
      if (m) sr = 'Read ' + m[1] + ' pages · ' + m[2] + ' rows' + m[3];
    }
    var mark = unpricedMark(meta, accepted);
    if (mark && typeof sr === 'string' && sr.indexOf(mark) < 0) sr += ' (' + mark + ')';
    return sr;
  }

  // ---------------------------------------------------------------- run sheet
  var runSheetWrap = document.getElementById('runsheet-wrap');
  // G9-R4: one entry per run in 'Choose a run' — a copied run directory
  // keeps the same run number, so entries dedupe by number; a flagged sample
  // entry wins over the copy (the Sample run listed once, not again as a copy)
  function dedupeSheetRuns(list) {
    var byNum = {};
    list.forEach(function (r) {
      if (r.number == null) return;
      var prev = byNum[r.number];
      if (!prev || (r.sample && !prev.sample)) byNum[r.number] = r;
    });
    return list.filter(function (r) {
      return r.number == null || byNum[r.number] === r;
    });
  }
  function openRunSheet() {
    var host = clear(document.getElementById('runsheet-list'));
    dedupeSheetRuns(runs).forEach(function (r) {
      var b = btn('runopt' + (detail && r.id === detail.id ? ' on' : ''));
      b.append(el('div', 'n', (r.label || r.id) + (r.sample ? ' · sample' : '')));
      // G8-8: a finished run carries the same 'Run finished · …' words as the
      // card; other statuses keep 'needs attention N · status'
      var tail = 'needs attention ' + r.unresolved +
        (r.status ? ' · ' + (JOB_STATUS_WORDS[r.status] || r.status) : '');
      if (r.status === 'done') {
        // PROJECTION-004: same research wording as the card — no 'reviewed' claim
        tail = 'Run finished · ' + (r.unresolved > 0
          ? r.unresolved + ' need review' : (r.research ? 'no rows waiting on you' : 'nothing left to review'));
      }
      b.append(el('div', 'm', 'Run ' + r.number + ' · ' + dateTimeWords(r.started_at) + ' · ' + tail +
        (r.as_of_note ? ' · ' + r.as_of_note : '')));
      b.addEventListener('click', function () {
        runSheetWrap.hidden = true;
        if (!detail || r.id !== detail.id) switchRun(r.id);
      });
      host.append(b);
    });
    runSheetWrap.hidden = false;
  }
  // G8-11: the picker is a button, not the card — '▸ Saved runs · N' sits under
  // '▸ New run' (same size and border); a native button, so Enter and Space
  // open the sheet without extra code. The card below is a plain status card.
  var savedrunsBtn = document.getElementById('savedruns');
  savedrunsBtn.addEventListener('click', openRunSheet);
  function updateSavedRunsBtn() {
    savedrunsBtn.textContent = '▸ Saved runs · ' + runs.length;
  }
  document.getElementById('runsheet-close').addEventListener('click', function () { runSheetWrap.hidden = true; });
  runSheetWrap.addEventListener('click', function (e) { if (e.target === runSheetWrap) runSheetWrap.hidden = true; });
  dialogWrap.addEventListener('click', function (e) {
    if (e.target !== dialogWrap || !route || !route.dialog) return;
    navigate({ runId: currentRunId(), screen: 'judgment', rowId: route.rowId });
  });

  // ---------------------------------------------------------------- nav + keys
  document.querySelectorAll('#nav .ni[data-scr]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.scr === 'source') {
        var first = null;
        detail.run.sources.forEach(function (s) {
          if (!first && s.pages && s.pages.length) first = { src: s.id, page: 'p' + s.pages[0].page_number };
        });
        if (first) navigate({ runId: currentRunId(), screen: 'source', srcId: first.src, page: first.page });
      } else if (b.dataset.scr === 'audit') {
        // G1-12: returning to the Audit log keeps the selected row in the URL,
        // the list and the pane — never a stale pane with nothing selected
        navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected,
                   filter: audit.filter !== 'ALL' ? audit.filter : null });
      } else {
        navigate({ runId: currentRunId(), screen: b.dataset.scr });
      }
    });
  });
  document.querySelectorAll('#nav .ni[data-soon]').forEach(function (b) {
    b.addEventListener('click', function () { toast('Every screen of this build is here.'); });
  });
  document.getElementById('src-back').addEventListener('click', function () {
    var first = null;
    detail.run.sources.forEach(function (s) {
      if (!first && s.pages && s.pages.length) first = { src: s.id, page: 'p' + s.pages[0].page_number };
    });
    if (first) navigate({ runId: currentRunId(), screen: 'source', srcId: first.src, page: first.page });
    else navigate({ runId: currentRunId(), screen: 'audit' });
  });
  document.getElementById('src-back-audit').addEventListener('click', function () {
    // G3-6: Back goes where the row was opened from, with the row still selected
    if (route.back === 'calendar') {
      navigate({ runId: currentRunId(), screen: 'calendar', rowId: route.valueId, w: route.w });
    } else {
      navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected });
    }
  });
  document.getElementById('judgment-back-audit').addEventListener('click', function () {
    navigate({ runId: currentRunId(), screen: 'audit', rowId: route.rowId });
  });
  document.getElementById('trail-back').addEventListener('click', function () {
    navigate({ runId: currentRunId(), screen: 'trail' });
  });
  window.addEventListener('hashchange', function () { render(); });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (accPanelWrap && !accPanelWrap.hidden) { accPanelWrap.hidden = true; return; }
    if (newrunWrap && !newrunWrap.hidden) { newrunWrap.hidden = true; return; }
    if (runSheetWrap && !runSheetWrap.hidden) { runSheetWrap.hidden = true; return; }
    if (dialogWrap && !dialogWrap.hidden && route && route.dialog) {
      // G1-6: replace, not push — Back must not re-open the dialog; U37: the
      // dialog can mount on the audit screen too, not only on judgment
      replaceRoute({ runId: currentRunId(), screen: route.screen === 'judgment' ? 'judgment' : 'audit',
                     rowId: route.rowId });
      return;
    }
    // G1-5: Esc closes the details pane (no history entry)
    if (route && route.screen === 'audit' && audit.selected) {
      audit.selected = null;
      audit.cleared = true;
      replaceRoute({ runId: currentRunId(), screen: 'audit',
                     filter: audit.filter !== 'ALL' ? audit.filter : null });
      return;
    }
    if (route && route.screen === 'judgment' && route.rowId) {
      judgmentSel = null;
      replaceRoute({ runId: currentRunId(), screen: 'judgment' });
      return;
    }
    // G2-5: Esc returns from a row's trail to the list (a pushed entry, so
    // browser Back lands back on the row)
    if (route && route.screen === 'trail' && route.rowId) {
      trailSel = null;
      navigate({ runId: currentRunId(), screen: 'trail' });
      return;
    }
    if (route && route.screen === 'source' && route.valueId) {
      replaceRoute({ runId: currentRunId(), screen: 'source', srcId: route.srcId, page: route.page,
                     back: route.back });
    }
  });

  // ---------------------------------------------------------------- boot
  function boot() {
    Promise.all([A.runs(), A.settings()]).then(function (both) {
      var list = both[0], dflt = both[1] && both[1].default_run;
      applyResearchSettings(both[1]);
      runs = list;
      // an explicit #/<id> route resolves even when the runs list is empty
      // (allowlist or fresh folder) — the empty message is only for no route
      var start = route && route.runId;
      if (!start && !runs.length) {
        document.getElementById('audit-rows').append(el('div', 'empty', 'No runs found under runs/.'));
        return;
      }
      if (!start && dflt && runs.some(function (r) { return r.id === dflt; })) start = dflt;
      if (!start) start = (runs.filter(function (r) { return r.sample; })[0] || runs[0]).id;
      return A.run(start).then(function (body) {
        detail = { id: start, run: body, state: body.state };
        renderShell();
        render();
      });
    }).catch(function (err) {
      document.getElementById('audit-rows').append(el('div', 'empty',
        'The decisions server is not reachable. Start it with: uvicorn server.app:app --port 8777 (' +
        err.message + ')'));
    });
  }
  route = parseHash();
  boot();
})();
