/* Proof Reader — U9 Export + second-reader details, on top of U1–U8.
   Reads runs through the local decisions server only; no external calls.
   One state per row comes from the server: run.json + decisions in order. */
(function () {
  'use strict';

  var STATUS_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed',
                       unreadable: 'unreadable', undecided: 'undecided' };
  var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run',
                      not_needed: 'not needed' };
  var GROUP_NAMES = { kept: 'Checked and kept', corrected: 'Corrected', removed: 'Checked and removed',
                      unverified: 'Unverified', open: 'Still open' };
  var GROUP_ORDER = ['kept', 'corrected', 'removed', 'unverified', 'open'];
  var DECIDED_BADGE = { kept: 'Kept by you', corrected: 'Fixed by you', removed: 'Removed by you' };
  var ACTION_WORDS = { keep: 'kept', fix: 'fixed', remove: 'removed' };
  var FIX_FIELDS = ['date_text', 'date', 'date_end', 'event_type', 'title', 'quote', 'company', 'ticker',
                    'program', 'indication'];
  var RULE_WORDS = {
    third_party_calendar_only: 'House rule: a third-party calendar is never a source on its own.',
    person_removed: 'Removed by you.',
    past_event: 'The event had already happened when the page was read.',
    source_rule: 'House rule: the source is not one the run accepts.'
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
  var NEW_RUN_COMPANIES = ['IONS', 'PTGX', 'ABVX', 'ROIV', 'ZLAB', 'CLDX',
                           'VKTX', 'TSHA', 'MNPR', 'ENTA', 'RYTM', 'RVMD'];
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
  function decidedBadge(group) {
    return el('span', 'st st-' + group, DECIDED_BADGE[group] || group);
  }
  function checkDot(result) { return el('span', 'dot ' + result); }
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
  var audit = { filter: 'ALL', selected: null, prev: null };   // selection memory
  var judgmentSel = null;
  var trailSel = null;
  var calSel = null;
  var chgSel = null;        // selected card index on What changed
  var changedCountCache = null;
  var changedCache = null;  // {key, a, b, cards}
  var pending = false;      // a decision POST is in flight
  var decisionsCache = { id: null, lines: null };   // decisions.jsonl lines, for the trail's person step
  var accuracyCache = { id: null, line: null, tried: false };   // runs/<id>/accuracy.json, one line for the audit header

  function needsAttention(t) {
    if (t.group === 'open' || t.group === 'unverified') return true;
    if (t.group !== 'corrected') return false;
    return (t.recheck && t.recheck.result === 'could_not_run') || t.status === 'undecided';
  }
  function rowTarget(row) { return detail.state.targets[row.id] || {}; }
  function rowOf(id) {
    return detail.run.rows.filter(function (r) { return r.id === id; })[0] || null;
  }
  function rowsByGroup(g) {
    return detail.run.rows.filter(function (r) { return rowTarget(r).group === g; });
  }
  function visibleAuditRows() {
    var f = audit.filter;
    return detail.run.rows.filter(function (r) {
      var g = rowTarget(r).group;
      if (f === 'ALL') return true;
      if (f === 'ATTN') return needsAttention(rowTarget(r));
      return g === f;
    });
  }
  function unresolvedRows() {
    return detail.run.rows.filter(function (r) { return needsAttention(rowTarget(r)); });
  }
  function decidedRows() {
    return detail.run.rows.filter(function (r) { return rowTarget(r).decided_by_person; });
  }
  function sourceOf(row) {
    var id = row.source_id;
    return detail.run.sources.filter(function (s) { return s.id === id; })[0] || null;
  }
  function mistakeFor(rowId) {
    return (detail.run.mistakes || []).filter(function (m) { return m.target === rowId; })[0] || null;
  }
  function exclusionFor(rowId) {
    return (detail.state.exclusions || []).filter(function (x) { return x.target === rowId; })[0] || null;
  }

  function refresh() {
    decisionsCache = { id: null, lines: null };
    changedCache = null;
    changedCountCache = null;
    accuracyCache = { id: null, line: null, tried: false };
    return A.run(detail.id).then(function (body) {
      detail = { id: detail.id, run: body, state: body.state };
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
    if (out.screen === 'audit') { out.rowId = seg[2] || null; out.filter = q.f || null; }
    if (out.screen === 'source') {
      out.srcId = seg[2] || null; out.page = seg[3] || null;
      out.valueId = q.v || null; out.back = q.back || null;
    }
    if (out.screen === 'judgment') { out.rowId = seg[2] || null; out.dialog = q.d || null; }
    if (out.screen === 'trail') { out.rowId = seg[2] || null; }
    if (out.screen === 'changed') { out.a = q.a || null; out.b = q.b || null; }
    return out;
  }

  function hashFor(o) {
    var h = '#/' + encodeURIComponent(o.runId) + '/' + o.screen;
    var q = [];
    if (o.screen === 'audit') {
      if (o.rowId) h += '/' + encodeURIComponent(o.rowId);
      if (o.filter && o.filter !== 'ALL') q.push('f=' + o.filter);
    } else if (o.screen === 'source') {
      h += '/' + encodeURIComponent(o.srcId) + '/' + o.page;
      if (o.valueId) q.push('v=' + encodeURIComponent(o.valueId));
      if (o.back) q.push('back=' + o.back);
    } else if (o.screen === 'judgment' || o.screen === 'trail') {
      if (o.rowId) h += '/' + encodeURIComponent(o.rowId);
      if (o.screen === 'judgment' && o.dialog) q.push('d=' + encodeURIComponent(o.dialog));
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
    updateJobBar();

    document.querySelectorAll('#nav .ni[data-scr]').forEach(function (b) {
      b.classList.toggle('on', b.dataset.scr === (route.screen === 'none' ? 'audit' : route.screen));
    });
    var scr = document.getElementById('scr-' + (route.screen === 'none' ? 'audit' : route.screen));
    var narrow = window.matchMedia('(max-width: 900px)').matches;
    var picked = (route.screen === 'source' && !!route.srcId) ||
                 (route.screen === 'trail' && !!route.rowId);
    document.querySelectorAll('.screen').forEach(function (s) { s.classList.toggle('on', s === scr); });
    if (scr) scr.classList.toggle('picked', narrow && (picked || route.screen === 'calendar'));
    document.getElementById('src-back').hidden = !(narrow && route.screen === 'source' && !route.back);
    document.getElementById('src-back-audit').hidden = !(route.screen === 'source' && route.back === 'audit');
    document.getElementById('trail-back').hidden = !(narrow && route.screen === 'trail' && route.rowId);
  }

  // ---------------------------------------------------------------- shell
  function renderShell() {
    var meta = detail.run.run || {};
    document.getElementById('runcard').hidden = false;
    document.getElementById('runcard-title').textContent = meta.label || ('Run ' + meta.number);
    document.getElementById('runcard-sub').textContent =
      'Run ' + meta.number + ' · ' + (meta.started_at || '').replace('T', ' ').slice(0, 16);
    var flag = document.getElementById('runcard-flag');
    flag.hidden = !meta.sample;
    if (meta.sample) flag.textContent = 'Sample data — not real events · click to switch runs';
    else flag.textContent = 'Click to switch runs';
    var cost = document.getElementById('runcard-cost');
    var cpkr = detail.state.cost_per_kept_row_usd;
    if (cpkr !== null && cpkr !== undefined) {
      cost.textContent = 'cost per kept row ' + money(cpkr);
      cost.hidden = false;
    } else cost.hidden = true;
    document.getElementById('side-foot').textContent =
      (meta.sample ? 'Sample data — not real events · ' : '') + 'Muse Spark reads · Code checks';
    document.title = 'Proof Reader — ' + (meta.label || '');

    var st = detail.state;
    var nRows = detail.run.rows.length;
    var nPages = detail.run.sources.reduce(function (n, s) { return n + (s.pages || []).length; }, 0);
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
  function calendarRows() {
    var out = [];
    detail.run.rows.forEach(function (r) {
      var t = rowTarget(r);
      if (t.group !== 'kept' && t.group !== 'corrected') return;
      var d = (t.values && t.values.date) || r.date;
      if (d) out.push({ row: r, t: t, date: d, period: !!(t.values && t.values.date_end) });
    });
    return out;
  }
  function calendarUndated() {
    var n = 0;
    detail.run.rows.forEach(function (r) {
      var t = rowTarget(r);
      if (t.group !== 'kept' && t.group !== 'corrected') return;
      var d = (t.values && t.values.date) || r.date;
      if (!d) n++;
    });
    return n;
  }

  function switchRun(runId, nav) {
    A.run(runId).then(function (body) {
      detail = { id: runId, run: body, state: body.state };
      audit = { filter: 'ALL', selected: null, prev: null };
      judgmentSel = null;
      trailSel = null;
      calSel = null;
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
  function accuracyLineFrom(acc) {
    if (!acc || typeof acc !== 'object') return null;
    function part(x) {
      if (!x || typeof x !== 'object') return null;
      var f = null, n = null;
      ['found', 'scored', 'caught'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) f = x[k]; });
      ['N', 'M', 'total'].forEach(function (k) { if (x[k] !== null && x[k] !== undefined) n = x[k]; });
      return n !== null ? (f !== null ? f : '?') + '/' + n : null;
    }
    var v = part(acc.values) ||
      (acc.table && acc.table.counts ? part({ found: acc.table.counts.found, N: acc.table.N }) : null);
    var ev = part(acc.events) ||
      (acc.events && acc.events.counts ? part({ found: acc.events.counts.found, M: acc.events.M }) : null);
    var pl = part(acc.planted);
    var bits = [];
    if (v) bits.push('values ' + v);
    if (ev) bits.push('events ' + ev);
    if (pl) bits.push('planted ' + pl);
    return bits.length ? 'Sealed set: ' + bits.join(' · ') : null;
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

  // ---------------------------------------------------------------- audit
  function renderAudit() {
    if (route.filter) audit.filter = route.filter;
    if (route.rowId && rowOf(route.rowId)) audit.selected = route.rowId;
    var st = detail.state;
    var meta = detail.run.run || {};

    var sub = document.getElementById('audit-sub');
    sub.textContent = 'Every proposed row and what happened to it · ' + (meta.label || 'run ' + meta.number);
    var rl = document.getElementById('audit-runlabel');
    rl.hidden = false;
    rl.textContent = meta.label || '';
    rl.className = 'runlabel' + (meta.sample ? ' sample' : '');

    document.getElementById('audit-counters').textContent =
      'Mistakes caught by code: ' + st.mistakes.code +
      ' · by the second reader: ' + st.mistakes.second_reader +
      ' · by a person: ' + st.mistakes.person +
      ' · Excluded: ' + (st.excluded.code + st.excluded.person);
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
  }

  function renderAuditFacets() {
    var host = clear(document.getElementById('audit-facets'));
    var st = detail.state;
    var defs = [['ALL', 'All']].concat(GROUP_ORDER.map(function (g) { return [g, GROUP_NAMES[g]]; }));
    defs.forEach(function (d) {
      var n = d[0] === 'ALL' ? detail.run.rows.length : st.counts.rows[d[0]];
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
    if (!vis.length) { audit.selected = null; return; }
    if (!audit.selected || !vis.some(function (r) { return r.id === audit.selected; })) {
      audit.selected = vis[0].id;
    }
  }

  function renderAuditRows() {
    var host = clear(document.getElementById('audit-rows'));
    var vis = visibleAuditRows();
    syncAuditSelection(vis);
    if (!vis.length) {
      host.append(el('div', 'empty', audit.filter === 'ATTN'
        ? 'Nothing needs attention. Every row has a home.' : 'No rows in this group.'));
      return;
    }
    var grouped = audit.filter === 'ALL' || audit.filter === 'ATTN';
    if (grouped) {
      // one heading per group: sort by group order, stable within a group
      var order = {}, idx = {};
      GROUP_ORDER.forEach(function (g, i) { order[g] = i; });
      detail.run.rows.forEach(function (r, i) { idx[r.id] = i; });
      vis = vis.slice().sort(function (a, b) {
        var d = order[rowTarget(a).group] - order[rowTarget(b).group];
        return d !== 0 ? d : idx[a.id] - idx[b.id];
      });
    }
    var lastG = null;
    vis.forEach(function (row) {
      var t = rowTarget(row);
      if (grouped && t.group !== lastG) {
        lastG = t.group;
        var n = rowsByGroup(lastG).length;
        host.append(el('div', 'grp', GROUP_NAMES[lastG] + ' · ' + n));
      }
      host.append(auditRow(row, t));
    });
    var on = host.querySelector('.row.on');
    if (on) on.scrollIntoView({ block: 'nearest' });
  }

  function auditRow(row, t) {
    var b = btn('row' + (row.id === audit.selected ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || '(no title)'));
    l1.append(el('span', 'dt', t.values && t.values.date_text || row.date_text || ''));
    var l2 = el('div', 'l2');
    l2.append(t.decided_by_person ? decidedBadge(t.group) : statusBadge(t.status));
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    if (t.decided_by_person && t.group !== 'removed') {
      l2.append(el('span', null, '· decided by you'));
    }
    var fx = (t.fixes || []).slice(-1)[0];
    if (t.group === 'corrected' && fx) {
      l2.append(el('span', 'fixline', (FIELD_WORDS[fx.field] || fx.field) + ': ' + (fx.from === null || fx.from === undefined ? '(none)' : fx.from) +
        ' → ' + (fx.to === null || fx.to === undefined ? '(none)' : fx.to)));
    }
    if ((t.group === 'open' || t.group === 'unverified') && t.reason) {
      l2.append(el('span', 'fixline', t.reason.length > 72 ? t.reason.slice(0, 72) + '…' : t.reason));
    }
    b.append(l1, l2);
    b.addEventListener('click', function () {
      audit.selected = row.id;
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
    return meta;
  }

  function pageImage(pp, highlightId) {
    var wrap = el('div', 'page-wrap');
    var img = el('img');
    img.alt = pp.source.title + ' · page ' + pp.page.page_number;
    img.src = '/runs/' + encodeURIComponent(currentRunId()) + '/' +
      pp.page.image.split('/').map(encodeURIComponent).join('/');
    wrap.append(img);
    var W = pp.page.width_pt, H = pp.page.height_pt;
    valuesOnPage(pp).forEach(function (v) {
      if (!Array.isArray(v.box) || v.box.length !== 4 ||
          v.box.some(function (n) { return typeof n !== 'number'; })) return;
      var b = el('div', 'box ' + v.status + (v.id === highlightId ? ' sel' : ''));
      b.title = v.label;
      b.style.left = (v.box[0] / W * 100) + '%';
      b.style.top = (v.box[1] / H * 100) + '%';
      b.style.width = (Math.max(0, v.box[2] - v.box[0]) / W * 100) + '%';
      b.style.height = (Math.max(0, v.box[3] - v.box[1]) / H * 100) + '%';
      b.addEventListener('click', function () {
        navigate({ runId: currentRunId(), screen: 'source', srcId: pp.source.id,
                   page: 'p' + pp.page.page_number, valueId: v.id, back: route.screen === 'audit' ? 'audit' : null });
      });
      wrap.append(b);
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

  // details pane (shared by audit + judgment)
  function detailsPane(host, row, opts) {
    opts = opts || {};
    clear(host);
    var t = rowTarget(row);
    var src = sourceOf(row);

    var sec1 = el('div', 'sec');
    if (t.decided_by_person) sec1.append(decidedBadge(t.group));
    var ttl = el('div', 'ttl', row.title || '(no title)');
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
    kv('Company', (row.ticker || '') + ' · ' + (row.company || ''));
    if (row.program) kv('Program', row.program);
    kv('Type', eventWord(row.event_type));
    kv('Event date', '', el('span', 'v mono', t.values && t.values.date_text || row.date_text || ''));
    kv('Proposed by', 'Model · ' + (row.proposed_by || ''));
    if (src) {
      var a = el('a', 'link', src.publisher);
      a.href = '#';
      a.addEventListener('click', function (ev) {
        ev.preventDefault();
        var pp = pageFor(row);
        if (pp) navigate({ runId: currentRunId(), screen: 'source', srcId: src.id,
                           page: 'p' + pp.page.page_number, valueId: row.id, back: opts.back || 'audit' });
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

    if (t.group === 'corrected') host.append(correctedSection(row, t));
    if (t.group === 'removed') host.append(removedSection(row, t));
    if (t.group === 'open' || t.group === 'unverified') host.append(whySection(row, t));
    if (t.disagreement) host.append(disagreementSection(row, t));

    host.append(checksSection(row, t));

    if (opts.actions) {
      var sa = el('div', 'sec');
      var br = el('div', 'btnrow');
      br.append(btn('btn', 'Open source page', function () {
        var pp = pageFor(row);
        if (pp) navigate({ runId: currentRunId(), screen: 'source', srcId: src.id,
                           page: 'p' + pp.page.page_number, valueId: row.id, back: 'audit' });
      }));
      br.append(btn('btn', 'Open agent trail', function () {
        navigate({ runId: currentRunId(), screen: 'trail', rowId: row.id });
      }));
      var dec = btn('btn pri', t.decided_by_person ? 'Change it' : 'Decide', function () {
        judgmentSel = row.id;
        navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
      });
      br.append(dec);
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
    var rule = x ? (RULE_WORDS[x.rule] || x.rule.replace(/_/g, ' ')) : 'Removed.';
    box.append(el('b', null, rule.charAt(0).toUpperCase() === rule.charAt(0) && x && x.by === 'code' ? rule : rule));
    var reason = el('div');
    reason.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
    reason.textContent = (x && x.reason) || t.reason || '';
    if (reason.textContent) box.append(reason);
    sec.append(box);
    return sec;
  }

  function whySection(row, t) {
    var sec = el('div', 'sec');
    sec.append(el('h4', null, t.group === 'open' ? 'Why it needs you' : 'Why it is unverified'));
    var box = el('div', 'ba' + (t.group === 'open' ? ' blue' : ''));
    box.textContent = t.reason || row.reason || 'Waiting for you.';
    sec.append(box);
    return sec;
  }

  // the second reader (R5): the field the two readers disagree on, both
  // values, above the checks
  function disagreementSection(row, t) {
    var d = t.disagreement;
    var sec = el('div', 'sec');
    var fieldWord = FIELD_WORDS[d.field] || d.field;
    sec.append(el('h4', null, 'The two readers disagree on the ' + fieldWord.toLowerCase()));
    var box = el('div', 'ba blue');
    function val(v) { return d.field === 'event_type' ? eventWord(v) : fmtField(d.field, v); }
    var l1 = el('div');
    l1.append(el('b', null, 'First reader · ' + (row.proposed_by || 'Muse Spark') + ': '), val(d.first_reader));
    var l2 = el('div');
    l2.style.marginTop = '4px';
    l2.append(el('b', null, 'Second reader · Muse Spark Open: '), val(d.second_reader));
    box.append(l1, l2);
    sec.append(box);
    return sec;
  }

  function checksSection(row, t) {
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Checks · by Code'));
    var pendingNote = t.decided_by_person && t.needs_recheck;
    if (pendingNote) {
      var pn = el('div', 'hint');
      pn.style.color = 'var(--amber)';
      pn.textContent = 'These results apply to the old value until the re-check finishes.';
      sec.append(pn);
    }
    (row.checks || []).forEach(function (c) {
      var line = el('div', 'chk');
      line.append(checkDot(c.result));
      var cn = el('span', 'cn');
      cn.append(esc(c.label), el('span', 'wtag', 'Code'));
      line.append(cn);
      var cr = el('span', 'cr', checkResultWord(c.result));
      line.append(cr);
      sec.append(line);
      if (c.reason) {
        var r = el('div');
        r.style.cssText = 'font-size:12px;color:var(--sub);padding:0 0 2px 17px';
        r.textContent = c.reason;
        sec.append(r);
      }
      sec.append(seeTheCheck(c));
    });
    if (!(row.checks || []).length) sec.append(el('div', null, 'No checks ran for this row.'));
    return sec;
  }

  function seeTheCheck(c) {
    var d = el('details', 'fold');
    if (c.code_ref && c.code_ref.function) d.dataset.fn = c.code_ref.function;
    d.append(el('summary', null, 'See the check'));
    var cmp = c.compared || {};
    var box = el('div', 'cmp');
    box.style.marginTop = '6px';
    box.append(el('span', 'lf', 'looked for '), esc(cmp.looked_for));
    if ('found' in cmp) {
      box.append(el('span', 'lf', ' · found '),
        esc(cmp.found === null || cmp.found === undefined || cmp.found === '' ? 'nothing' : cmp.found));
    }
    d.append(box);
    var res = el('div', 'recline');
    res.append('Result: ', el('b', null, checkResultWord(c.result)));
    if (c.reason) res.append(' — ' + c.reason);
    d.append(res);
    if (c.code_ref && c.code_ref.file) {
      var ref = el('a', 'coderef');
      ref.textContent = c.code_ref.file + (c.code_ref.function ? ' · ' + c.code_ref.function + '()' : '');
      ref.href = '#';
      ref.title = 'The check, in the product repo: ' + c.code_ref.file;
      ref.addEventListener('click', function (ev) { ev.preventDefault(); });
      d.append(ref);
    }
    return d;
  }

  function renderAuditDet() {
    var host = clear(document.getElementById('audit-det'));
    var row = rowOf(audit.selected);
    if (!row) {
      host.append(el('div', 'empty', 'Nothing selected.'));
      return;
    }
    detailsPane(host, row, { actions: true, back: 'audit' });
  }

  // ---------------------------------------------------------------- source (U1)
  function renderSource() {
    var host = clear(document.getElementById('srcList'));
    var mid = clear(document.getElementById('srcwrap'));
    var det = clear(document.getElementById('srcDet'));
    document.getElementById('src-sub').textContent = 'The pages the rows rest on';

    var pages = [];
    detail.run.sources.forEach(function (s) {
      (s.pages || []).forEach(function (p) { pages.push({ source: s, page: p }); });
    });
    document.getElementById('src-sub').textContent =
      'The pages the rows rest on · ' + pages.length + ' pages';
    clear(host);

    var selected = null;
    pages.forEach(function (pp) {
      if (pp.source.id === route.srcId && 'p' + pp.page.page_number === route.page) selected = pp;
    });
    pages.forEach(function (pp) {
      var key = 'p' + pp.page.page_number;
      var b = btn('pg' + (selected && pp === selected ? ' on' : ''), '');
      b.append(el('span', 'n', pp.source.title), el('span', 'c', 'page ' + pp.page.page_number));
      b.addEventListener('click', function () {
        navigate({ runId: currentRunId(), screen: 'source', srcId: pp.source.id, page: key,
                   valueId: null, back: route.back });
      });
      host.append(b);
    });

    if (!selected) {
      if (route.srcId || route.page) mid.append(el('div', 'empty', 'Choose a page on the left.'));
      else mid.append(el('div', 'empty', 'Choose a page on the left.'));
      det.append(el('div', 'empty', 'Click a value to see what was compared.'));
      return;
    }

    mid.append(srcmetaBar(selected.source));
    var wrap = pageImage(selected, route.valueId);
    mid.append(wrap);
    var selBox = wrap.querySelector('.box.sel');
    if (selBox) selBox.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });

    var v = valuesOnPage(selected).filter(function (x) { return x.id === route.valueId; })[0];
    if (!v) {
      det.append(el('div', 'empty', 'Click a value to see what was compared.'));
      return;
    }
    var row = rowOf(v.id);
    if (row) detailsPane(det, row, { back: route.back === 'audit' ? 'audit' : null });
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
  }

  // ---------------------------------------------------------------- trail (U5)
  // One step per line, one actor each. Model steps name the model and carry
  // tokens/cost when present; the first step expands to the exact prompt text;
  // a person's decision is the last step, dated from decisions.jsonl.
  function renderTrail() {
    document.getElementById('trail-sub').textContent =
      'Step by step, for one row · ' + detail.run.rows.length + ' rows';
    if (route.rowId && rowOf(route.rowId)) trailSel = route.rowId;

    var host = clear(document.getElementById('trail-rows'));
    detail.run.rows.forEach(function (r) { host.append(trailRowBtn(r)); });
    var on = host.querySelector('.row.on');
    if (on) on.scrollIntoView({ block: 'nearest' });

    var mid = clear(document.getElementById('trail-mid'));
    var det = clear(document.getElementById('trail-det'));
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

    var meta = detail.run.run || {};
    var head = el('div', 'trailhead');
    head.append(el('div', 'tt', row.title || '(no title)'));
    head.append(el('div', 'tm', (row.ticker || '') + ' · proposed by ' + (row.proposed_by || 'Model') +
      ' · ' + (meta.label || 'run ' + meta.number) + (meta.sample ? ' · sample data — not real events' : '')));
    var wrap = el('div', 'trailwrap');
    wrap.append(head);

    var steps = (row.trail || []).slice();
    var t = rowTarget(row);
    var decision = t.decided_by_person ? lastPersonDecision(row.id) : null;
    var lines = steps.concat([{ __decision: true, line: decision, target: t }]);
    lines.forEach(function (s, i) {
      wrap.append(trailStep(row, s, i < lines.length - 1));
    });
    mid.append(wrap);

    detailsPane(det, row, { back: null });
  }

  function trailRowBtn(row) {
    var t = rowTarget(row);
    var b = btn('row' + (row.id === trailSel ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || ''));
    l1.append(el('span', 'dt', (t.values && t.values.date_text) || row.date_text || ''));
    var l2 = el('div', 'l2');
    l2.append(t.decided_by_person ? decidedBadge(t.group) : statusBadge(t.status));
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    b.append(l1, l2);
    b.addEventListener('click', function () {
      trailSel = row.id;
      navigate({ runId: currentRunId(), screen: 'trail', rowId: row.id });
    });
    return b;
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

  function trailStep(row, s, more) {
    var t = rowTarget(row);
    var step = el('div', 'step');
    var rail = el('div', 'rail');
    var body = el('div', 'bd');
    step.append(rail, body);

    if (s.__decision) {
      // the person's decision: last step, tagged Person
      var action = s.line ? s.line.action : (t.last_action || 'keep');
      var word = ACTION_WORDS[action] || action;
      rail.append(el('span', 'ic', '●'));
      if (more) rail.append(el('span', 'ln'));
      step.classList.add('personstep');
      var hd = el('div', 'hd');
      hd.append(el('span', 'who', 'Decided by you · ' + word));
      hd.append(el('span', 'st2', 'Person'));
      hd.append(el('span', 'wh', s.line ? dateShort(s.line.at) : ''));
      body.append(hd);
      var fx = s.line && s.line.fix;
      if (fx) {
        body.append(el('div', 'tx', (FIELD_WORDS[fx.field] || fx.field) + ': ' +
          (fx.from === null || fx.from === undefined ? '(none)' : fx.from) + ' → ' +
          (fx.to === null || fx.to === undefined ? '(none)' : fx.to)));
      }
      var note = (s.line && s.line.note) || t.reason || '';
      if (note) body.append(el('div', 'tx', 'Note: “' + note + '”'));
      if (!s.line) body.append(el('div', 'tx', 'Decided after this run was saved — the dated record is in the run folder.'));
      return step;
    }

    var isFail = s.tag === 'Code' && /: fail\./.test(s.text || '');
    var glyph = '·';
    if (s.tag === 'Model') glyph = '◎';
    else if (s.tag === 'Person') glyph = '●';
    else if (isFail) glyph = '✗';
    else if (s.step === 'Checked') glyph = '✓';
    else if (s.step === 'Sent back') glyph = '→';
    else if (s.step === 'Instructions sent') glyph = '»';
    rail.append(el('span', 'ic', glyph));
    if (more) rail.append(el('span', 'ln'));
    if (isFail) step.classList.add('failstep');
    if (s.tag === 'Person') step.classList.add('personstep');

    var hd = el('div', 'hd');
    var whoName = s.tag === 'Model' ? (s.who || 'Model')
      : s.tag === 'Person' ? 'a person'
      : s.step;                       // Code steps read by their step name; the actor is the tag
    hd.append(el('span', 'who', whoName));
    hd.append(el('span', 'st2', s.tag === 'Model' ? 'Model · ' + (s.who || '') : s.tag));
    hd.append(el('span', 'wh', hm(s.at)));
    body.append(hd);

    if (s.step === 'Instructions sent') {
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
      body.append(el('div', 'tx', s.text || ''));
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
          var folds = document.querySelectorAll('#trail-det details.fold');
          for (var i = 0; i < folds.length; i++) {
            if (folds[i].dataset.fn === s.who) {
              folds[i].open = true;
              folds[i].scrollIntoView({ block: 'center', behavior: 'smooth' });
              break;
            }
          }
        });
        body.querySelector('.hd').append(link);
      }
    }
    return step;
  }

  // ---------------------------------------------------------------- calendar (U6)
  // Kept and corrected rows only, never removed / unverified / open. Exact days
  // sort within their month; a period row sits under its period as written,
  // with no invented day.
  function renderCalendar() {
    var meta = detail.run.run || {};
    var rows = calendarRows();

    var host = clear(document.getElementById('cal-mid'));
    var det = clear(document.getElementById('cal-det'));

    var head = el('div', 'ph');
    head.style.background = 'var(--panel2)';
    head.append(el('h2', null, 'Catalyst Calendar'));
    head.append(el('div', 'sub', 'Kept events, by date · ' + rows.length + ' row' +
      (rows.length === 1 ? '' : 's')));
    var rl = el('div', 'runlabel' + (meta.sample ? ' sample' : ''), meta.label || '');
    head.append(rl);
    host.append(head);

    if (!rows.length) {
      host.append(el('div', 'empty', 'Nothing kept yet — the calendar fills as rows are kept or corrected.'));
      det.append(el('div', 'empty', 'Pick an event to see its details.'));
      return;
    }

    var exact = rows.filter(function (r) { return !r.period; })
      .sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
    var periods = rows.filter(function (r) { return r.period; })
      .sort(function (a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });

    var months = [];
    function month(key) {
      for (var i = 0; i < months.length; i++) if (months[i].key === key) return months[i];
      var m = { key: key, exact: [], periods: [] };
      months.push(m);
      return m;
    }
    exact.forEach(function (r) { month(r.date.slice(0, 7)).exact.push(r); });
    periods.forEach(function (r) {
      var m = month(r.date.slice(0, 7));
      var label = (r.t.values && r.t.values.date_text) || r.row.date_text || '';
      var bucket = null;
      m.periods.forEach(function (p) { if (p.label === label) bucket = p; });
      if (!bucket) { bucket = { label: label, rows: [] }; m.periods.push(bucket); }
      bucket.rows.push(r);
    });
    months.sort(function (a, b) { return a.key < b.key ? -1 : 1; });

    var wrap = el('div', 'calwrap');
    months.forEach(function (m) {
      var sample = m.exact[0] || (m.periods[0] && m.periods[0].rows[0]);
      wrap.append(el('div', 'mo', monthYear(sample ? sample.date : m.key + '-01')));
      m.exact.forEach(function (r) { wrap.append(calRow(r, dayMonth(r.date))); });
      m.periods.forEach(function (p) {
        wrap.append(el('div', 'periodhead', p.label || 'Period as written'));
        p.rows.forEach(function (r) { wrap.append(calRow(r, '')); });
      });
    });
    var undated = calendarUndated();
    if (undated) {
      wrap.append(el('div', 'calfnote', undated + ' kept row' + (undated === 1 ? ' has' : 's have') +
        ' no date to place — not on the calendar.'));
    }
    host.append(wrap);

    var sel = rows.filter(function (r) { return r.row.id === calSel; })[0] || null;
    if (!sel) {
      det.append(el('div', 'empty', 'Pick an event to see its details.'));
      return;
    }
    detailsPane(det, sel.row, { back: null });
    var sa = el('div', 'sec');
    var br = el('div', 'btnrow');
    br.append(btn('btn', 'Open in Audit log', function () {
      navigate({ runId: currentRunId(), screen: 'audit', rowId: sel.row.id });
    }));
    br.append(btn('btn', 'Open agent trail', function () {
      navigate({ runId: currentRunId(), screen: 'trail', rowId: sel.row.id });
    }));
    sa.append(br);
    det.append(sa);
  }

  function calRow(r, dayCell) {
    var row = r.row, t = r.t;
    var b = btn('crow' + (row.id === calSel ? ' on' : ''));
    b.append(el('span', 'cd', dayCell));
    var c2 = el('span', 'ct2');
    c2.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) c2.append(el('span', 'pg2', row.program));
    c2.append(el('span', 'tt', row.title || '(no title)'));
    b.append(c2);
    b.append(el('span', 'ty', eventWord(row.event_type)));
    if (t.decided_by_person) b.append(decidedBadge(t.group));
    b.addEventListener('click', function () {
      calSel = row.id;
      renderCalendar();
    });
    return b;
  }

  // ---------------------------------------------------------------- judgment (U4)
  function renderJudgment() {
    var unresolved = unresolvedRows();
    var decided = decidedRows();
    document.getElementById('judgment-sub').textContent =
      'Waiting for your decision · ' + unresolved.length;

    var host = clear(document.getElementById('judgment-rows'));
    if (!unresolved.length && !decided.length) {
      host.append(el('div', 'empty', 'The queue is empty. Everything proposed has been decided.'));
    } else {
      if (unresolved.length) {
        host.append(el('div', 'grp', 'Waiting for you · ' + unresolved.length));
        unresolved.forEach(function (r) { host.append(judgmentRow(r)); });
      }
      if (decided.length) {
        host.append(el('div', 'grp', 'Decided by you · ' + decided.length));
        decided.forEach(function (r) { host.append(judgmentRow(r)); });
      }
    }
    var on = host.querySelector('.row.on');
    if (on) on.scrollIntoView({ block: 'nearest' });

    var mid = clear(document.getElementById('judgment-mid'));
    mid.append(el('div', 'empty', 'Pick a row on the left.'));
    var det = clear(document.getElementById('judgment-det'));
    var row = rowOf(judgmentSel) || unresolved[0] || null;
    if (route.rowId && rowOf(route.rowId)) row = rowOf(route.rowId);
    if (!row) {
      det.append(el('div', 'empty', 'Nothing waiting.'));
      closeDialog();
      return;
    }
    judgmentSel = row.id;
    var t = rowTarget(row);
    if (t.decided_by_person) judgmentDecidedPane(det, row, t);
    else judgmentPendingPane(det, row, t);

    if (route.dialog) openDialog(row, route.dialog);
    else closeDialog();
  }

  function judgmentRow(row) {
    var t = rowTarget(row);
    var b = btn('row' + (row.id === judgmentSel ? ' on' : ''));
    var l1 = el('div', 'l1');
    l1.append(el('span', 'tk', row.ticker || '—'));
    if (row.program) l1.append(el('span', 'pg2', row.program));
    l1.append(el('span', 'tt', row.title || ''));
    l1.append(el('span', 'dt', (t.values && t.values.date_text) || row.date_text || ''));
    var l2 = el('div', 'l2');
    l2.append(t.decided_by_person ? decidedBadge(t.group) : statusBadge(t.status));
    l2.append(el('span', 'mdl', 'by ' + (row.proposed_by || 'Model')));
    if (t.reason && !t.decided_by_person) {
      l2.append(el('span', 'fixline', t.reason.length > 72 ? t.reason.slice(0, 72) + '…' : t.reason));
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

  function judgmentPendingPane(host, row, t) {
    clear(host);
    detailsPane(host, row, { back: null });
    // decision area replaces the action row: append at the end
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Your call'));
    var note = noteField('qnote');
    sec.append(el('div', null, ''), note);
    note.placeholder = 'Why you decided this… (optional for Keep, required for Remove)';
    var br = el('div', 'btnrow');
    br.style.marginTop = '10px';
    var v = t.values || {};
    var isPeriod = !!v.date_end || !!(v.date_text && !v.date);
    br.append(btn('btn pri', 'Keep', function () {
      postDecision(row.id, { target: row.id, action: 'keep', note: note.value || null },
        'Kept by you.');
    }));
    if (isPeriod) {
      br.append(btn('btn', 'Keep as stated', function () {
        var dt = (t.values && t.values.date_text) || row.date_text || 'the period';
        postDecision(row.id, { target: row.id, action: 'keep', note: 'Kept as stated: ' + dt + '.' },
          'Kept as stated — the period stays, no day invented.');
      }));
    }
    br.append(btn('btn', 'Fix…', function () {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id, dialog: 'fix' });
    }));
    br.append(btn('btn danger', 'Remove…', function () {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id, dialog: 'remove' });
    }));
    sec.append(br);
    if (isPeriod) {
      sec.append(el('div', 'hint', '“Keep as stated” keeps “' +
        ((t.values && t.values.date_text) || row.date_text) + '” as a period — the calendar shows no invented day.'));
    }
    host.append(sec);
  }

  function judgmentDecidedPane(host, row, t) {
    clear(host);
    var sec = el('div', 'sec');
    var box = el('div', 'ba green');
    box.append(el('b', null, (DECIDED_BADGE[t.group] || 'Decided by you')));
    if (t.reason) {
      var r = el('div');
      r.style.cssText = 'margin-top:6px;color:var(--sub);font-size:12.5px';
      r.textContent = t.reason;
      box.append(r);
    }
    sec.append(box);
    host.append(sec);
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
  function postDecision(rowId, body, successMsg) {
    if (pending) return;
    pending = true;
    A.decide(currentRunId(), body).then(function (res) {
      pending = false;
      if (!res.ok) {
        var msg = (res.body && res.body.detail) || ('HTTP ' + res.status);
        if (route.dialog) showDialogError(msg);
        else toast(msg);
        return;
      }
      // drop the dialog from the hash BEFORE re-rendering, or it would re-open on the old state
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: rowId });
      return refresh().then(function () {
        toast(successMsg || 'Decided.');
      });
    }).catch(function () {
      pending = false;
      toast('The server is not reachable.');
    });
  }

  // ---------------------------------------------------------------- dialogs
  var dialogWrap = document.getElementById('dialog-wrap');
  var dialogHost = document.getElementById('dialog');
  function closeDialog() {
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

  function openDialog(row, action) {
    if (action === 'fix') fixDialog(row);
    else if (action === 'remove') removeDialog(row);
    else closeDialog();
  }

  function removeDialog(row) {
    clear(dialogHost);
    dialogHost.append(el('div', 'sh-t', 'Remove this row'));
    dialogHost.append(el('div', 'sh-s', row.ticker + ' · ' + row.title));
    dialogHost.append(el('h4', null, 'Why it is removed (required — it is the recorded reason)'));
    var note = noteField('dnote', 'e.g. Not an upcoming event.');
    dialogHost.append(note);
    var br = el('div', 'btnrow');
    br.style.marginTop = '14px';
    br.append(btn('btn danger', 'Remove', function () {
      if (!note.value.trim()) { showDialogError('A reason is required — it is the recorded reason.'); return; }
      postDecision(row.id, { target: row.id, action: 'remove', note: note.value.trim() },
        'Removed by you — moved to checked and removed.');
    }), btn('btn', 'Cancel', function () {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
    }));
    dialogHost.append(br);
    dialogWrap.hidden = false;
    note.focus();
  }

  function fixDialog(row) {
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
    function validateFix() {
      if (!to.value.trim() && cur() !== null) return 'A new value is required.';
      if ((sel.value === 'date' || sel.value === 'date_end') && !/^\d{4}-\d{2}-\d{2}$/.test(to.value.trim())) {
        return (FIELD_WORDS[sel.value] || sel.value) + ' must be written as YYYY-MM-DD.';
      }
      return null;
    }
    var br = el('div', 'btnrow');
    br.style.marginTop = '14px';
    br.append(btn('btn pri', 'Save fix', function () {
      var problem = validateFix();
      if (problem) { showDialogError(problem); return; }
      var note = (document.getElementById('qnote') || {}).value || null;
      var body = { target: row.id, action: 'fix', note: note || null,
                   fix: { field: sel.value, from: cur(), to: to.value } };
      var old = dialogHost.querySelector('.errline');
      if (old) old.remove();
      var pendEl = el('div', 'pendline', 'Re-check pending…');
      var br2 = dialogHost.querySelector('.btnrow');
      dialogHost.insertBefore(pendEl, br2);
      if (pending) return;
      pending = true;
      A.decide(currentRunId(), body).then(function (res) {
        pending = false;
        pendEl.remove();
        if (!res.ok) {
          showDialogError((res.body && res.body.detail) || ('HTTP ' + res.status));
          return;
        }
        var st = res.body.state;
        var fx = (st.fixes || []).slice(-1)[0];
        var what = 'Re-checked after the fix: ' + checkResultWord(st.recheck && st.recheck.result);
        if (fx && fx.edit_without_error) what += ' — ' + NO_ERROR;
        else if (fx && fx.mistake) what += ' — counted as a mistake caught by you.';
        else if (fx && fx.not_counted_reason && !fx.mistake) what += ' — ' + fx.not_counted_reason;
        // the dialog closes with the hash; the re-check result shows on the row's pane
        navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
        return refresh().then(function () { toast(what); });
      }).catch(function () {
        pending = false;
        pendEl.remove();
        showDialogError('The server is not reachable.');
      });
    }), btn('btn', 'Cancel', function () {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: row.id });
    }));
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
  function familyMembers(fam) {
    return runs.filter(function (r) { return labelFamily(r.label) === fam; })
      .sort(function (x, y) { return (y.number || 0) - (x.number || 0); });
  }
  function runShort(r) {
    return (r.label || r.id) + ' · ' + (r.started_at || '').replace('T', ' ').slice(0, 10);
  }

  function renderChanged() {
    var meta = detail.run.run || {};
    var fam = labelFamily(meta.label);
    var members = familyMembers(fam);
    var sub = document.getElementById('changed-sub');
    var pairHost = clear(document.getElementById('changed-pairs'));
    var host = clear(document.getElementById('changed-rows'));
    var mid = clear(document.getElementById('changed-mid'));
    var det = clear(document.getElementById('changed-det'));

    if (members.length < 2) {
      sub.textContent = (meta.label || 'This run') + ' is the only run of its kind saved here.';
      changedCountCache = 0;
      document.getElementById('ct-changed').textContent = '0';
      host.append(el('div', 'empty', 'Nothing to compare yet — run the same companies again and the two runs can be compared.'));
      mid.append(el('div', 'empty', 'Nothing to compare.'));
      det.append(el('div', 'empty', 'Pick a change to see it.'));
      return;
    }

    var bId = route.b && members.some(function (m) { return m.id === route.b; }) ? route.b : members[0].id;
    var aId = route.a && members.some(function (m) { return m.id === route.a; }) ? route.a : members[1].id;
    var aM = runs.filter(function (r) { return r.id === aId; })[0];
    var bM = runs.filter(function (r) { return r.id === bId; })[0];
    sub.textContent = (aM.label || aId) + ' → ' + (bM.label || bId);

    members.forEach(function (m) {
      var f = btn('fc' + (m.id === bId ? ' on' : ''), m.label || m.id);
      f.title = 'Compare the run before it against ' + (m.label || m.id);
      f.addEventListener('click', function () {
        var idx = members.indexOf(m);
        var older = members[idx + 1] || members.filter(function (x) { return x.id !== m.id; })[0];
        if (!older) return;
        navigate({ runId: currentRunId(), screen: 'changed', a: older.id, b: m.id });
      });
      pairHost.append(f);
    });

    var key = aId + '>' + bId;
    if (changedCache && changedCache.key === key) return renderChangedCards(host, mid, det);
    host.append(el('div', 'empty', 'Reading the two runs…'));
    Promise.all([A.run(aId), A.run(bId), A.decisions(aId), A.decisions(bId)]).then(function (all) {
      changedCache = { key: key, a: { run: all[0], state: all[0].state, decisions: all[2] },
                       b: { run: all[1], state: all[1].state, decisions: all[3] },
                       cards: compareRuns({ run: all[0], state: all[0].state, decisions: all[2] },
                                            { run: all[1], state: all[1].state, decisions: all[3] }) };
      changedCountCache = changedCache.cards.length;
      document.getElementById('ct-changed').textContent = String(changedCountCache);
      if (route && route.screen === 'changed') renderChanged();
    }).catch(function () {
      host.append(el('div', 'empty', 'The two runs could not be read.'));
    });
  }

  function rowVals(x, row) {
    var t = x.state.targets[row.id] || {};
    return t.values || row;
  }
  function rowKey(row) {
    return (row.ticker || '') + '|' + (row.event_type || '') + '|' + (row.program || '').toLowerCase();
  }
  function fmtField(field, value) {
    if (value === null || value === undefined || value === '') return '(none)';
    return field === 'event_type' ? eventWord(value) : String(value);
  }

  function compareRuns(a, b) {
    // match rows across the two files in three passes: exact (title, program,
    // indication), then title (so a program or indication change compares as a
    // corrected field instead of add + remove), then ticker · type · program
    // (so a corrected title still compares). Whatever stays unmatched is
    // added (B) or removed (A).
    function kTitle(r) { return (r.ticker || '') + '|' + (r.event_type || '') + '|' + (r.title || '').toLowerCase(); }
    function kExact(r) { return kTitle(r) + '|' + (r.program || '') + '|' + (r.indication || ''); }
    function bucketize(rows, keyFn) {
      var m = {};
      rows.forEach(function (r) { (m[keyFn(r)] = m[keyFn(r)] || []).push(r); });
      return m;
    }
    var aExact = bucketize(a.run.rows, kExact);
    var pairs = [], onlyB = [];
    b.run.rows.forEach(function (rb) {
      var q = aExact[kExact(rb)];
      if (q && q.length) pairs.push([q.shift(), rb]);
      else onlyB.push(rb);
    });
    var aLeft = [];
    Object.keys(aExact).forEach(function (k) { aLeft = aLeft.concat(aExact[k]); });
    var aTitle = bucketize(aLeft, kTitle);
    var stillB = [];
    onlyB.forEach(function (rb) {
      var q = aTitle[kTitle(rb)];
      if (q && q.length) {
        // same title twice (two programs): keep the pairing with the same program
        var idx = 0, best = -1;
        q.forEach(function (ra, i) {
          var s = 0;
          if (String(ra.program || '') === String(rb.program || '')) s += 2;
          if (String(ra.indication || '') === String(rb.indication || '')) s += 1;
          if (s > best) { best = s; idx = i; }
        });
        pairs.push([q.splice(idx, 1)[0], rb]);
      } else stillB.push(rb);
    });
    var aLeft2 = [];
    Object.keys(aTitle).forEach(function (k) { aLeft2 = aLeft2.concat(aTitle[k]); });
    var aLoose = bucketize(aLeft2, rowKey);
    var lastB = [];
    stillB.forEach(function (rb) {
      var q = aLoose[rowKey(rb)];
      if (q && q.length) pairs.push([q.shift(), rb]);
      else lastB.push(rb);
    });
    var stillA = [];
    Object.keys(aLoose).forEach(function (k) { stillA = stillA.concat(aLoose[k]); });

    var cards = [];
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
    lastB.forEach(function (rb) {
      cards.push({ kind: 'added', row: rb, x: b, fields: [],
                   line: 'New in ' + (b.run.run.label || 'the newer run') + '.', since: since(b, rb) });
    });
    stillA.forEach(function (ra) {
      cards.push({ kind: 'removed', row: ra, x: a, fields: [],
                   line: 'In ' + (a.run.run.label || 'the older run') + ', not in ' + (b.run.run.label || 'the newer run') + '.',
                   since: since(a, ra) });
    });
    pairs.forEach(function (pr) {
      var ra = pr[0], rb = pr[1];
      var diffs = [];
      FIELDS.forEach(function (f) {
        var va = rowVals(a, ra)[f], vb = rowVals(b, rb)[f];
        if (String(va === null || va === undefined ? '' : va) !== String(vb === null || vb === undefined ? '' : vb)) {
          diffs.push({ field: f, from: va, to: vb });
        }
      });
      if (!diffs.length) return;
      var datey = diffs.some(function (d) { return d.field === 'date' || d.field === 'date_end' || d.field === 'date_text'; });
      var lines = diffs.map(function (d) {
        return (FIELD_WORDS[d.field] || d.field) + ': ' + fmtField(d.field, d.from) + ' → ' + fmtField(d.field, d.to);
      });
      cards.push({ kind: datey ? 'moved' : 'corrected', row: rb, x: b, fields: diffs,
                   line: lines.join(' · '), since: since(b, rb) });
    });
    var order = { added: 0, removed: 1, corrected: 2, moved: 3 };
    cards.sort(function (x, y) { return order[x.kind] - order[y.kind]; });
    return cards;
  }

  var CHG_WORDS = { added: 'added', removed: 'removed', corrected: 'corrected', moved: 'date moved' };
  var CHG_CLASS = { added: 'st-kept', removed: 'st-removed', corrected: 'st-corrected', moved: 'st-undecided' };

  function renderChangedCards(host, mid, det) {
    var cards = changedCache.cards;
    if (!cards.length) {
      host.append(el('div', 'empty', 'The two runs propose the same rows — nothing changed between them.'));
      mid.append(el('div', 'empty', 'Nothing changed.'));
      det.append(el('div', 'empty', 'Pick a change to see it.'));
      return;
    }
    if (chgSel === null || !cards[chgSel]) chgSel = 0;
    cards.forEach(function (c, i) {
      var b = btn('chcard' + (i === chgSel ? ' on' : ''));
      var top = el('div', 'top');
      top.append(el('span', 'chtag ' + CHG_CLASS[c.kind], CHG_WORDS[c.kind]));
      top.append(el('span', 'tk mono', c.row.ticker || '—'));
      top.append(el('span', null, c.row.title || '(no title)'));
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

    var c = cards[chgSel];
    mid.append(el('div', 'empty', 'Pick a change on the left.'));
    clear(det);
    var sec = el('div', 'sec');
    sec.append(el('span', 'chtag ' + CHG_CLASS[c.kind], CHG_WORDS[c.kind]));
    var ttl = el('div', 'ttl', c.row.title || '(no title)');
    ttl.style.marginTop = '8px';
    sec.append(ttl);
    det.append(sec);
    var sec2 = el('div', 'sec');
    function kv(k, v) { var r = el('div', 'kv'); r.append(el('span', 'k', k), el('span', 'v', v)); sec2.append(r); }
    kv('Company', (c.row.ticker || '') + ' · ' + (c.row.company || ''));
    kv('Type', eventWord(c.row.event_type));
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
    var sec4 = el('div', 'sec');
    var br = el('div', 'btnrow');
    br.append(btn('btn', 'Open in ' + (c.x.run.run.label || 'that run'), function () {
      var rid = c.x.run.run.id;
      if (detail && detail.id === rid) {
        navigate({ runId: rid, screen: 'audit', rowId: c.row.id });
      } else {
        switchRun(rid);
        setTimeout(function () { navigate({ runId: rid, screen: 'audit', rowId: c.row.id }); }, 400);
      }
    }));
    sec4.append(br);
    det.append(sec4);
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
        } else {
          right.append(el('span', 'exmeta', 'Open from the runs folder'));
        }
      }).catch(function () {
        clear(right);
        right.append(el('span', 'exmeta', 'Open from the runs folder'));
      });
    });
    wrap.append(listHost);

    var buildLine = REPORT_BUILD_ENDPOINT
      ? null
      : el('div', 'calfnote', 'Built from the command line: python -m report.build ' + currentRunId());
    if (buildLine) wrap.append(buildLine);
    mid.append(wrap);
    var foot = el('div', 'expfoot', 'Built by code from the saved results');
    mid.append(foot);

    // the report's summary numbers: the same counts as the audit log
    var sec = el('div', 'sec');
    sec.append(el('h4', null, 'Summary — the same counts as the audit log'));
    function kv(k, v) { var r = el('div', 'kv'); r.append(el('span', 'k', k), el('span', 'v', v)); sec.append(r); }
    GROUP_ORDER.forEach(function (g) {
      kv(GROUP_NAMES[g], String(st.counts.rows[g]));
    });
    kv('Needs attention', String(st.unresolved));
    kv('Mistakes caught by code', String(st.mistakes.code));
    kv('Mistakes caught by the second reader', String(st.mistakes.second_reader));
    kv('Mistakes caught by a person', String(st.mistakes.person));
    kv('Excluded by code', String(st.excluded.code));
    kv('Excluded by you', String(st.excluded.person));
    det.append(sec);
    det.append(el('div', 'sec', ''));
  }

  // ---------------------------------------------------------------- new run (U8)
  var newrunWrap = document.getElementById('newrun-wrap');
  var nrBuilt = false;
  function buildNewRun() {
    if (nrBuilt) return;
    nrBuilt = true;
    var cos = clear(document.getElementById('nr-cos'));
    var all = el('label', 'tk2');
    var allInput = el('input');
    allInput.type = 'checkbox'; allInput.id = 'nr-all'; allInput.checked = true;
    all.append(allInput, document.createTextNode(' All ' + NEW_RUN_COMPANIES.length));
    cos.append(all);
    NEW_RUN_COMPANIES.forEach(function (t) {
      var l = el('label', 'tk2');
      var i = el('input');
      i.type = 'checkbox'; i.className = 'nr-co'; i.value = t; i.checked = true;
      l.append(i, document.createTextNode(' ' + t));
      cos.append(l);
    });
    allInput.addEventListener('change', function () {
      document.querySelectorAll('.nr-co').forEach(function (c) { c.checked = allInput.checked; });
    });
    var types = clear(document.getElementById('nr-types'));
    NEW_RUN_TYPES.forEach(function (t) {
      var l = el('label', 'tk2');
      var i = el('input');
      i.type = 'checkbox'; i.className = 'nr-type'; i.value = t[0]; i.checked = true;
      l.append(i, document.createTextNode(' ' + t[1]));
      types.append(l);
    });
    document.getElementById('nr-win').addEventListener('click', function (e) {
      var b = e.target.closest('[data-win]');
      if (!b) return;
      document.querySelectorAll('#nr-win [data-win]').forEach(function (x) { x.classList.toggle('on', x === b); });
    });
  }
  function nrError(msg) {
    var e = document.getElementById('nr-error');
    e.textContent = msg;
    e.hidden = !msg;
  }
  document.getElementById('newrun').addEventListener('click', function () {
    buildNewRun();
    nrError('');
    newrunWrap.hidden = false;
  });
  document.getElementById('nr-cancel').addEventListener('click', function () { newrunWrap.hidden = true; });
  newrunWrap.addEventListener('click', function (e) { if (e.target === newrunWrap) newrunWrap.hidden = true; });
  document.getElementById('nr-go').addEventListener('click', function () {
    var companies = [];
    document.querySelectorAll('.nr-co:checked').forEach(function (c) { companies.push(c.value); });
    if (!companies.length) { nrError('Choose at least one company.'); return; }
    var types = [];
    document.querySelectorAll('.nr-type:checked').forEach(function (c) { types.push(c.value); });
    var winBtn = document.querySelector('#nr-win [data-win].on');
    var limitRaw = document.getElementById('nr-limit').value.trim();
    var limit = null;
    if (limitRaw !== '') {
      limit = Number(limitRaw);
      if (isNaN(limit) || limit < 0) { nrError('The spending limit must be a number of dollars, zero or more.'); return; }
    }
    var note = document.getElementById('nr-note').value.trim() || null;
    nrError('');
    A.newRun({ companies: companies, event_types: types,
               time_window_months: winBtn ? Number(winBtn.dataset.win) : null,
               spending_limit_usd: limit, note_for_agent: note }).then(function (res) {
      if (!res.ok) { nrError((res.body && res.body.detail) || ('HTTP ' + res.status)); return; }
      newrunWrap.hidden = true;
      var id = res.body.id;
      toast((res.body.label || 'Run ' + res.body.number) + ' — started.');
      return A.runs().then(function (list) {
        runs = list;
        switchRun(id);
      });
    }).catch(function () { nrError('The server is not reachable.'); });
  });

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
          A.runs().then(function (list) { runs = list; });
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
  function updateJobBar() {
    var bar = document.getElementById('jobbar');
    if (!detail) { hideJobBar(); return; }
    var meta = detail.run.run || {};
    var st = meta.status;
    if (!st || (JOB_ACTIVE.indexOf(st) < 0 && JOB_PAUSABLE.indexOf(st) < 0)) { hideJobBar(); return; }
    clear(bar);
    var live = JOB_ACTIVE.indexOf(st) >= 0;
    bar.append(el('span', live ? 'jb-live' : 'jb-status',
      (meta.label || 'Run ' + meta.number) + ' · ' + (JOB_STATUS_WORDS[st] || st)));
    var bits = [];
    if (meta.turn_count !== null && meta.turn_count !== undefined) {
      bits.push('turn ' + meta.turn_count + ' of ' + (meta.turn_limit || '?'));
    }
    if (meta.spend_usd !== null && meta.spend_usd !== undefined) {
      bits.push(meta.choices && meta.choices.spending_limit_usd !== null && meta.choices.spending_limit_usd !== undefined
        ? money(meta.spend_usd) + ' of ' + money(meta.choices.spending_limit_usd) + ' used'
        : money(meta.spend_usd) + ' used');
    }
    if (bits.length) bar.append(el('span', 'jb-sub', bits.join(' · ')));
    if (meta.status_reason) bar.append(el('span', 'jb-sub', meta.status_reason));
    if (JOB_PAUSABLE.indexOf(st) >= 0) {
      var limitInput = null;
      if (st === 'needs_input') {
        limitInput = el('input', 'fld');
        limitInput.placeholder = 'new limit $';
        limitInput.inputMode = 'decimal';
        bar.append(limitInput);
      }
      var err = el('span', 'errline', '');
      err.hidden = true;
      bar.append(btn('btn', 'Resume', function () {
        var body = {};
        if (limitInput && limitInput.value.trim() !== '') {
          var v = Number(limitInput.value.trim());
          if (isNaN(v) || v < 0) { err.textContent = 'The limit must be a number of dollars.'; err.hidden = false; return; }
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
    }
    bar.hidden = false;
    document.getElementById('screens').style.paddingTop = '40px';
  }
  function hideJobBar() {
    document.getElementById('jobbar').hidden = true;
    document.getElementById('screens').style.paddingTop = '';
  }

  // ---------------------------------------------------------------- run sheet
  var runSheetWrap = document.getElementById('runsheet-wrap');
  document.getElementById('runcard').addEventListener('click', function () {
    var host = clear(document.getElementById('runsheet-list'));
    runs.forEach(function (r) {
      var b = btn('runopt' + (detail && r.id === detail.id ? ' on' : ''));
      b.append(el('div', 'n', (r.label || r.id) + (r.sample ? ' · sample' : '')));
      b.append(el('div', 'm', 'Run ' + r.number + ' · ' + (r.started_at || '').replace('T', ' ').slice(0, 16) +
        ' · needs attention ' + r.unresolved +
        (r.status ? ' · ' + (JOB_STATUS_WORDS[r.status] || r.status) : '')));
      b.addEventListener('click', function () {
        runSheetWrap.hidden = true;
        if (!detail || r.id !== detail.id) switchRun(r.id);
      });
      host.append(b);
    });
    runSheetWrap.hidden = false;
  });
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
    navigate({ runId: currentRunId(), screen: 'audit', rowId: audit.selected });
  });
  document.getElementById('trail-back').addEventListener('click', function () {
    navigate({ runId: currentRunId(), screen: 'trail' });
  });
  window.addEventListener('hashchange', function () { render(); });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (newrunWrap && !newrunWrap.hidden) { newrunWrap.hidden = true; return; }
    if (runSheetWrap && !runSheetWrap.hidden) { runSheetWrap.hidden = true; return; }
    if (dialogWrap && !dialogWrap.hidden && route && route.dialog) {
      navigate({ runId: currentRunId(), screen: 'judgment', rowId: route.rowId });
      return;
    }
    if (route && route.screen === 'source' && route.valueId) {
      navigate({ runId: currentRunId(), screen: 'source', srcId: route.srcId, page: route.page,
                 back: route.back });
    }
  });

  // ---------------------------------------------------------------- boot
  function boot() {
    A.runs().then(function (list) {
      runs = list;
      if (!runs.length) {
        document.getElementById('audit-rows').append(el('div', 'empty', 'No runs found under runs/.'));
        return;
      }
      var start = route && route.runId ? route.runId : (runs.filter(function (r) { return r.sample; })[0] || runs[0]).id;
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
