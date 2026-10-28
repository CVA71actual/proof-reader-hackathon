/* Proof Reader — U1: app shell + Source view.
   Reads runs through the local decisions server only; no external calls. */
(function () {
  'use strict';

  var RUN_WORDS = { matched: 'matched', proposed: 'proposed', failed: 'failed',
                    unreadable: 'unreadable', undecided: 'undecided' };
  var CHECK_WORDS = { pass: 'passed', fail: 'failed', could_not_run: 'could not run',
                      not_needed: 'not needed' };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  var toastTimer = null;
  function toast(msg) {
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 2400);
  }

  function esc(s) { return s === null || s === undefined ? '' : String(s); }

  // ---------------------------------------------------------------- theme
  var themeBtn = document.getElementById('theme');
  function applyTheme(dark) {
    document.documentElement.classList.toggle('dark', dark);
    themeBtn.setAttribute('aria-pressed', String(dark));
    try { localStorage.setItem('pr-theme', dark ? 'dark' : 'light'); } catch (e) {}
  }
  try { applyTheme(localStorage.getItem('pr-theme') === 'dark'); } catch (e) {}
  themeBtn.addEventListener('click', function () {
    applyTheme(!document.documentElement.classList.contains('dark'));
  });

  // ---------------------------------------------------------------- state
  var model = null;   // {runId, run, sources, pages, valuesByPage}
  var sel = { pageKey: null, valueId: null };

  function pageKey(sourceId, pageNumber) { return sourceId + ':' + pageNumber; }

  function buildModel(runId, run) {
    var sources = run.sources || [];
    var pages = [];
    var valuesByPage = {};
    sources.forEach(function (s) {
      (s.pages || []).forEach(function (p) {
        pages.push({ source: s, page: p, key: pageKey(s.id, p.page_number) });
        valuesByPage[pageKey(s.id, p.page_number)] = [];
      });
    });
    function addValue(sourceId, pageNumber, v) {
      var k = pageKey(sourceId, pageNumber);
      if (valuesByPage[k]) valuesByPage[k].push(v);
    }
    (run.tables || []).forEach(function (t) {
      (t.rows || []).forEach(function (row) {
        (row.cells || []).forEach(function (c) {
          addValue(t.source_id, t.page_number, {
            id: c.id, kind: 'cell', group: t.title || 'Table',
            label: row.label, sub: c.column, value: c.value,
            box: c.box, status: c.status, checks: c.checks || [],
            model: t.model && t.model.name
          });
        });
      });
    });
    (run.rows || []).forEach(function (r) {
      addValue(r.source_id, r.page_number, {
        id: r.id, kind: 'row', group: 'Proposed events',
        label: r.title, sub: (r.ticker || '') + (r.date_text ? ' · ' + r.date_text : ''),
        value: r.quote, box: r.box, status: r.status, checks: r.checks || [],
        model: r.proposed_by
      });
    });
    return { runId: runId, run: run, sources: sources, pages: pages, valuesByPage: valuesByPage };
  }

  // ---------------------------------------------------------------- routing
  // '#/source/<source id>/p<page number>?v=<value id>' selects a page and value.
  // '#/' means "no page chosen" (the mobile back button). '' means "not routed yet":
  // the first page is chosen as the default.
  function hashFor(key, valueId) {
    var p = model.pages.filter(function (x) { return x.key === key; })[0];
    if (!p) return '#/';
    return '#/source/' + encodeURIComponent(p.source.id) + '/p' + p.page.page_number +
      (valueId ? '?v=' + encodeURIComponent(valueId) : '');
  }

  function readHash() {
    var h = location.hash || '';
    if (h === '') return null;
    if (h === '#/' || h === '#') return { pageKey: null, valueId: null };
    var m = /^#\/source\/([^/]+)\/p(\d+)(?:\?v=(.+))?$/.exec(h);
    if (!m || !model) return { pageKey: null, valueId: null };
    var key = pageKey(decodeURIComponent(m[1]), Number(m[2]));
    if (!model.pages.some(function (x) { return x.key === key; })) return { pageKey: null, valueId: null };
    return { pageKey: key, valueId: m[3] ? decodeURIComponent(m[3]) : null };
  }

  function navigate(key, valueId) {
    var h = hashFor(key, valueId);
    if (location.hash !== h) location.hash = h;   // hashchange renders
    else render();
  }

  // ---------------------------------------------------------------- render
  var listHost = document.getElementById('srcList');
  var midHost = document.getElementById('srcwrap');
  var detHost = document.getElementById('srcDet');

  function render() {
    var fromHash = readHash();
    if (fromHash) sel = fromHash;
    else if (model.pages.length) sel = { pageKey: model.pages[0].key, valueId: null };
    if (sel.pageKey && sel.valueId) {
      var onPage = model.valuesByPage[sel.pageKey] || [];
      if (!onPage.some(function (v) { return v.id === sel.valueId; })) sel.valueId = null;
    }
    renderList();
    renderPage();
    renderDetail();
    var scr = document.getElementById('scr-source');
    var narrow = window.matchMedia('(max-width: 900px)').matches;
    scr.classList.toggle('picked', narrow && !!sel.pageKey);
    document.getElementById('src-back').hidden = !(narrow && sel.pageKey);
  }

  function renderList() {
    listHost.textContent = '';
    document.getElementById('src-sub').textContent =
      'The pages the rows rest on · ' + model.pages.length + ' pages';
    model.pages.forEach(function (p) {
      var vals = model.valuesByPage[p.key] || [];
      var srcBtn = el('button', 'pg' + (p.key === sel.pageKey ? ' on' : ''));
      srcBtn.append(el('span', 'n', p.source.title),
        el('span', 'c', 'page ' + p.page.page_number + (vals.length ? ' · ' + vals.length + ' values' : '')));
      srcBtn.addEventListener('click', function () { navigate(p.key, null); });
      listHost.append(srcBtn);
      if (p.key === sel.pageKey && vals.length) {
        var groups = [];
        vals.forEach(function (v) {
          var g = groups.filter(function (x) { return x.name === v.group; })[0];
          if (!g) { g = { name: v.group, items: [] }; groups.push(g); }
          g.items.push(v);
        });
        groups.forEach(function (g) {
          listHost.append(el('div', 'grp', g.name));
          g.items.forEach(function (v) { listHost.append(valueRow(v)); });
        });
      }
    });
  }

  function valueRow(v) {
    var b = el('button', 'val' + (v.id === sel.valueId ? ' on' : ''));
    b.title = v.label + ' · ' + v.sub + (v.box ? '' : ' — no box on the page for this value');
    b.append(el('span', 'l', v.label + ' · ' + v.sub),
      el('span', 'v', v.value === null || v.value === undefined ? '(none)' : String(v.value)),
      el('span', 'st st-' + v.status, RUN_WORDS[v.status] || v.status));
    b.addEventListener('click', function () { navigate(sel.pageKey, v.id); });
    return b;
  }

  function renderPage() {
    midHost.textContent = '';
    if (!sel.pageKey) {
      midHost.append(el('div', 'empty', 'Choose a page on the left.'));
      return;
    }
    var p = model.pages.filter(function (x) { return x.key === sel.pageKey; })[0];
    if (!p) return;
    var meta = el('div', 'srcmeta');
    function kv(k, v) {
      var s = el('span'); s.append(el('b', null, k + ' '), esc(v)); meta.append(s);
    }
    kv('Publisher', p.source.publisher);
    kv('Kind', p.source.kind);
    kv('Published', p.source.published_date);
    kv('Captured', (p.source.captured_at || '').replace('T', ' ').replace('+00:00', ' UTC'));
    kv('SHA-256', (p.source.sha256 || '').slice(0, 16) + '…');
    midHost.append(meta);

    var wrap = el('div', 'page-wrap');
    var img = el('img');
    img.alt = p.source.title + ' · page ' + p.page.page_number;
    img.src = '/runs/' + encodeURIComponent(model.runId) + '/' +
      p.page.image.split('/').map(encodeURIComponent).join('/');
    wrap.append(img);

    var W = p.page.width_pt, H = p.page.height_pt;
    (model.valuesByPage[sel.pageKey] || []).forEach(function (v) {
      if (!Array.isArray(v.box) || v.box.length !== 4 ||
          v.box.some(function (n) { return typeof n !== 'number'; })) return;
      var b = el('div', 'box ' + v.status + (v.id === sel.valueId ? ' sel' : ''));
      b.title = v.label + ' · ' + v.sub + ': ' + v.value;
      b.style.left = (v.box[0] / W * 100) + '%';
      b.style.top = (v.box[1] / H * 100) + '%';
      b.style.width = (Math.max(0, v.box[2] - v.box[0]) / W * 100) + '%';
      b.style.height = (Math.max(0, v.box[3] - v.box[1]) / H * 100) + '%';
      b.addEventListener('click', function () { navigate(sel.pageKey, v.id); });
      wrap.append(b);
    });
    midHost.append(wrap);

    var selBox = wrap.querySelector('.box.sel');
    if (selBox) selBox.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
  }

  function renderDetail() {
    detHost.textContent = '';
    if (!sel.valueId) {
      detHost.append(el('div', 'empty', 'Click a value to see what was compared.'));
      return;
    }
    var vals = model.valuesByPage[sel.pageKey] || [];
    var v = vals.filter(function (x) { return x.id === sel.valueId; })[0];
    if (!v) {
      detHost.append(el('div', 'empty', 'Click a value to see what was compared.'));
      return;
    }
    var listItem = listHost.querySelector('[data-value-id]') &&
      listHost.querySelector('.val.on');
    if (listItem) listItem.scrollIntoView({ behavior: 'smooth', block: 'center' });

    var sec = el('div', 'sec');
    sec.append(el('h4', null, v.kind === 'cell' ? 'Table value' : 'Proposed event'),
      el('div', 'ttl', v.label));
    [['Where', v.sub],
     ['Status', RUN_WORDS[v.status] || v.status],
     ['Read by', v.model ? 'Model · ' + v.model : 'Model']].forEach(function (pair) {
      var row = el('div', 'kv');
      row.append(el('span', 'k', pair[0]));
      if (pair[0] === 'Status') row.append(el('span', 'v st st-' + v.status, pair[1]));
      else row.append(el('span', 'v', pair[1]));
      sec.append(row);
    });
    if (v.kind === 'row' && v.value) sec.append(el('div', 'qbox', v.value));
    detHost.append(sec);

    var csec = el('div', 'sec');
    csec.append(el('h4', null, 'Checks · by Code'));
    v.checks.forEach(function (c) {
      var row = el('div', 'chk');
      row.append(el('span', 'dot ' + c.result),
        el('span', 'cn', c.label + ' — ' + (CHECK_WORDS[c.result] || c.result)));
      csec.append(row);
      if (c.reason) {
        var reason = el('div');
        reason.style.cssText = 'font-size:12px;color:var(--sub);padding:0 0 2px 17px';
        reason.textContent = c.reason;
        csec.append(reason);
      }
      var cmp = c.compared || {};
      if ('looked_for' in cmp || 'found' in cmp) {
        var box = el('div', 'cmp');
        box.append(el('span', 'lf', 'looked for '), esc(cmp.looked_for),
          el('span', 'lf', ' · found '),
          esc(cmp.found === null || cmp.found === undefined || cmp.found === '' ? 'nothing' : cmp.found));
        csec.append(box);
      }
      if (c.code_ref && c.code_ref.file) {
        csec.append(el('div', 'coderef', c.code_ref.file +
          (c.code_ref.function ? ' · ' + c.code_ref.function + '()' : '')));
      }
    });
    if (!v.checks.length) csec.append(el('div', null, 'No checks ran for this value.'));
    detHost.append(csec);
    if (!v.box) detHost.append(el('div', 'sec', 'No box on the page for this value.'));
  }

  // ---------------------------------------------------------------- shell
  function renderShell() {
    var run = model.run.run || {};
    document.getElementById('runcard').hidden = false;
    document.getElementById('runcard-title').textContent = run.label || ('Run ' + run.number);
    document.getElementById('runcard-sub').textContent =
      'Run ' + run.number + ' · ' + (run.started_at || '').replace('T', ' ').slice(0, 16) +
      ' · ' + (run.choices && run.choices.companies ? run.choices.companies.length + ' companies' : '');
    var flag = document.getElementById('runcard-flag');
    flag.hidden = !run.sample;
    if (run.sample) flag.textContent = 'Sample data — not real events';
    document.getElementById('side-foot').textContent =
      (run.sample ? 'Sample data — not real events · ' : '') + 'Muse Spark reads · Code checks';
    document.title = 'Proof Reader — ' + (run.label || 'Source view');
  }

  // ---------------------------------------------------------------- boot
  function pickRun(runs) {
    if (!runs.length) return null;
    return runs.filter(function (r) { return r.sample; })[0] || runs[0];
  }

  function boot() {
    fetch('/api/runs').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (runs) {
      var chosen = pickRun(runs);
      if (!chosen) {
        listHost.append(el('div', 'empty', 'No runs found under runs/.'));
        return;
      }
      return fetch('/api/runs/' + encodeURIComponent(chosen.id)).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (run) {
        model = buildModel(chosen.id, run);
        renderShell();
        render();
      });
    }).catch(function (err) {
      listHost.append(el('div', 'empty',
        'The decisions server is not reachable. Start it with: ' +
        'uvicorn server.app:app --port 8777 (' + err.message + ')'));
    });
  }

  window.addEventListener('hashchange', render);
  document.getElementById('src-back').addEventListener('click', function () {
    if (location.hash === '#/') render();
    else location.hash = '#/';
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && sel.valueId) navigate(sel.pageKey, null);
  });
  document.querySelectorAll('[data-soon]').forEach(function (b) {
    b.addEventListener('click', function () { toast('That screen lands in a later slice (U2–U9).'); });
  });

  boot();
})();
