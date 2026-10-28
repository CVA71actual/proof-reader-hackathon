"""Build a self-contained HTML page: the read table on the left, the filing page on the right.

Usage (from the repo root):
    python -m viewer.build_viewer [--reading PATH] [--out PATH]

By default it uses the model reading (fixtures/ions_ars_2026_p95.reading.json) if one exists,
otherwise the labelled fake reading (built on the fly if needed). Output: viewer/out/index.html.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import pymupdf

from capture import read_table
from checks.status import check_reading

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
OUT_DIR = Path(__file__).resolve().parent / "out"
RENDER_ZOOM = 2.0


def render_page_png(pdf: Path, zoom: float = RENDER_ZOOM) -> bytes:
    doc = pymupdf.open(pdf)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    data = pix.tobytes("png")
    doc.close()
    return data


def choose_reading(path: Path | None) -> dict:
    if path is not None:
        return json.loads(path.read_text())
    if read_table.READING_PATH.exists():
        return json.loads(read_table.READING_PATH.read_text())
    if read_table.FAKE_READING_PATH.exists():
        return json.loads(read_table.FAKE_READING_PATH.read_text())
    return read_table.read_fake(read_table.load_words())


def build(reading: dict, out: Path) -> Path:
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text())
    words_doc = json.loads((FIXTURES / manifest["words_file"]).read_text())
    checked = check_reading(reading, words_doc)
    png = render_page_png(FIXTURES / manifest["page_file"])

    data = {
        "manifest": manifest,
        "page": {"width": words_doc["page_width"], "height": words_doc["page_height"]},
        "reading": {
            "model": reading.get("model"),
            "fake": bool(reading.get("fake")),
            "fake_note": reading.get("fake_note"),
            "created_at": reading.get("created_at"),
            "prompt_sha256": reading.get("prompt_sha256"),
            "readable": bool(reading.get("readable")),
            "parse_problem": reading.get("parse_problem"),
            "title": (reading.get("table") or {}).get("title", ""),
            "columns": (reading.get("table") or {}).get("columns", []),
        },
        "checked": checked,
    }
    payload = json.dumps(data).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", payload).replace(
        "__IMG__", "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proof Reader</title>
<style>
  :root {
    --bg: #f6f6f4; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6b66; --line: #e2e1dc;
    --pass: #1f7a3a; --fail: #c62828; --grey: #9a9a94; --accent: #2b59c3;
    --matched-bg: #e8f4ec; --failed-bg: #fdecec; --proposed-bg: #f1f1ee;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  header { padding: 12px 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
  header h1 { margin: 0 0 4px; font-size: 16px; }
  header .meta { color: var(--muted); font-size: 12px; word-break: break-all; }
  header a { color: var(--accent); }
  .fake-banner { background: #b71c1c; color: #fff; font-weight: 700; padding: 10px 16px; letter-spacing: .02em; }
  .tags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .tag { font-size: 12px; padding: 2px 8px; border-radius: 10px; border: 1px solid var(--line); background: var(--bg); }
  .tag.model { border-color: var(--accent); color: var(--accent); }
  .tag.fake { background: #b71c1c; color: #fff; border-color: #b71c1c; font-weight: 700; }
  .tag.later { color: var(--muted); border-style: dashed; }
  main { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; padding: 16px;
         height: calc(100vh - 120px); }
  .pane { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; overflow: auto; min-height: 0; }
  .summary { padding: 10px 12px; border-bottom: 1px solid var(--line); font-size: 13px; position: sticky; top: 0;
             background: var(--panel); z-index: 2; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
  th { font-size: 12px; color: var(--muted); font-weight: 600; background: var(--panel); }
  td.label { width: 34%; }
  td.cell { cursor: pointer; }
  td.cell:hover { outline: 1px solid var(--accent); }
  td.cell.sel { outline: 2px solid var(--accent); }
  td.cell.matched { background: var(--matched-bg); }
  td.cell.failed { background: var(--failed-bg); }
  td.cell.proposed { background: var(--proposed-bg); }
  .val { font-variant-numeric: tabular-nums; font-weight: 600; }
  .status { font-size: 11px; text-transform: lowercase; margin-left: 4px; }
  .status.matched { color: var(--pass); } .status.failed { color: var(--fail); } .status.proposed { color: var(--muted); }
  .marks { font-size: 11px; margin-top: 2px; display: flex; flex-direction: column; gap: 1px; }
  .mark.pass { color: var(--pass); } .mark.fail { color: var(--fail); } .mark.could_not_run { color: var(--grey); }
  .detail { padding: 10px 12px; border-top: 1px solid var(--line); font-size: 13px; }
  .detail h3 { margin: 0 0 6px; font-size: 13px; }
  .detail .row { margin: 4px 0; }
  .detail code { background: var(--bg); padding: 0 4px; border-radius: 3px; }
  .note { color: #b71c1c; font-weight: 600; }
  .page-wrap { position: relative; margin: 12px; }
  .page-wrap img { width: 100%; display: block; border: 1px solid var(--line); }
  .box { position: absolute; border: 1.5px solid; cursor: pointer; }
  .box.matched { border-color: var(--pass); background: transparent; }
  .box.failed { border-color: var(--fail); background: rgba(198,40,40,.06); }
  .box.proposed { border-color: var(--grey); background: transparent; }
  .box.sel { border-width: 3px; border-color: var(--accent); background: rgba(43,89,195,.10); z-index: 3; }
  .unreadable { padding: 16px; color: var(--fail); }
  @media (max-width: 800px) { main { grid-template-columns: 1fr; height: auto; } .pane { max-height: 80vh; } }
</style>
</head>
<body>
<div id="fake-banner" class="fake-banner" hidden></div>
<header>
  <h1 id="title"></h1>
  <div class="meta" id="meta"></div>
  <div class="tags" id="tags"></div>
</header>
<main>
  <section class="pane" id="left">
    <div class="summary" id="summary"></div>
    <div id="table-host"></div>
    <div class="detail" id="detail">Click a value to see what was compared.</div>
  </section>
  <section class="pane" id="right">
    <div class="page-wrap" id="page-wrap"><img id="page-img" src="__IMG__" alt="Filing page"></div>
  </section>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById('data').textContent);
  const M = D.manifest, R = D.reading, C = D.checked;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text; return e; };
  const SYMBOL = { pass: '✓', fail: '✗', could_not_run: '–' };
  const WORD = { pass: 'passed', fail: 'failed', could_not_run: 'could not run' };

  document.getElementById('title').textContent =
    M.company + ' · ' + M.form + ' filed ' + M.filing_date + ' · page ' + M.page_number + ' of ' +
    M.source_page_count + ' · ' + (R.title || M.page_title);
  const meta = document.getElementById('meta');
  meta.append('Accession ' + M.accession + ' · ');
  const a = el('a', null, 'source on sec.gov'); a.href = M.source_url; a.target = '_blank'; a.rel = 'noopener';
  meta.append(a, ' · source file fingerprint (SHA-256) ' + M.source_sha256);

  const tags = document.getElementById('tags');
  if (R.fake) {
    tags.append(el('span', 'tag fake', 'FAKE reading · no model was called'));
    const b = document.getElementById('fake-banner');
    b.hidden = false;
    b.textContent = 'FAKE READING. These values were built by code for testing, not read by Muse Spark. ' +
      'Some cells were changed on purpose to show how problems look.';
  } else {
    tags.append(el('span', 'tag model', 'Reading: Model · Muse Spark (' + R.model + ')'));
  }
  tags.append(el('span', 'tag', 'Checks: Code'));
  tags.append(el('span', 'tag later', 'Second reader: Day 3'));

  const summary = document.getElementById('summary');
  const host = document.getElementById('table-host');
  const wrap = document.getElementById('page-wrap');

  if (C.status === 'unreadable') {
    summary.textContent = 'The reading could not be understood.';
    host.append(el('div', 'unreadable', 'The answer from the reader was not a table we could use (' +
      (R.parse_problem || 'unknown problem') + '). The raw answer is saved with the reading. Nothing was guessed.'));
    return;
  }

  const n = C.counts;
  summary.textContent = (n.matched + n.failed + n.proposed) + ' values read · ' + n.matched +
    ' matched · ' + n.failed + ' failed · ' + n.proposed + ' proposed only. ' +
    '"Matched" means both code checks passed.';

  const columns = R.columns.slice();
  C.rows.forEach(r => r.cells.forEach(c => { if (!columns.includes(c.column)) columns.push(c.column); }));

  const tbl = el('table'); const thead = el('thead'); const hr = el('tr');
  hr.append(el('th', null, ''));
  columns.forEach(col => hr.append(el('th', null, col)));
  thead.append(hr); tbl.append(thead);
  const tbody = el('tbody');
  const cellsById = {}, boxesById = {};
  let nextId = 0;

  const W = D.page.width, H = D.page.height;
  C.rows.forEach(row => {
    const tr = el('tr');
    tr.append(el('td', 'label', row.label));
    columns.forEach(col => {
      const matches = row.cells.filter(c => c.column === col);
      const td = el('td');
      matches.forEach(c => {
        const id = 'c' + (nextId++);
        td.className = 'cell ' + c.status; td.dataset.id = id;
        const v = el('div'); v.append(el('span', 'val', c.value === null || c.value === undefined ? '(none)' : String(c.value)));
        v.append(el('span', 'status ' + c.status, c.status));
        td.append(v);
        const marks = el('div', 'marks');
        c.checks.forEach(k => marks.append(el('span', 'mark ' + k.result, SYMBOL[k.result] + ' ' + k.label)));
        td.append(marks);
        td.title = c.checks.map(k => k.label + ': ' + WORD[k.result] + '. ' + k.reason).join('\n');
        cellsById[id] = { td, cell: c, row };
        td.addEventListener('click', () => select(id, 'cell'));
        if (Array.isArray(c.box) && c.box.length === 4 && c.box.every(x => typeof x === 'number')) {
          const [x0, y0, x1, y1] = c.box;
          const b = el('div', 'box ' + c.status);
          b.style.left = (x0 / W * 100) + '%'; b.style.top = (y0 / H * 100) + '%';
          b.style.width = (Math.max(0, x1 - x0) / W * 100) + '%'; b.style.height = (Math.max(0, y1 - y0) / H * 100) + '%';
          b.title = row.label + ' · ' + col + ': ' + c.value;
          b.addEventListener('click', () => select(id, 'box'));
          wrap.append(b); boxesById[id] = b;
        }
      });
      tr.append(td);
    });
    tbody.append(tr);
  });
  tbl.append(tbody); host.append(tbl);

  function select(id, from) {
    document.querySelectorAll('.sel').forEach(e => e.classList.remove('sel'));
    const item = cellsById[id]; if (!item) return;
    item.td.classList.add('sel');
    const box = boxesById[id];
    if (box) box.classList.add('sel');
    if (from === 'cell' && box) box.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
    if (from === 'box') item.td.scrollIntoView({ behavior: 'smooth', block: 'center' });
    showDetail(item, !!box);
  }

  function showDetail(item, hasBox) {
    const d = document.getElementById('detail'); d.textContent = '';
    const c = item.cell;
    d.append(el('h3', null, item.row.label + ' · ' + c.column + ': ' + c.value + ' (' + c.status + ')'));
    d.append(el('div', 'row', 'Value read by: ' + (R.fake ? 'FAKE reading (code, for testing)' : 'Model · Muse Spark')));
    if (c.fake_note) d.append(el('div', 'row note', 'Fake reading: ' + c.fake_note + '.'));
    c.checks.forEach(k => {
      const r = el('div', 'row');
      r.append(el('span', 'mark ' + k.result, SYMBOL[k.result] + ' '), el('strong', null, k.label),
        ' (' + k.kind + ') ' + WORD[k.result] + '. ' + k.reason);
      const cmp = k.compared || {};
      if ('found' in cmp) {
        const s = el('div'); s.append('Looked for ', el('code', null, cmp.value), ', found ',
          el('code', null, cmp.found === null ? 'nothing' : (cmp.found === '' ? 'no text' : cmp.found)));
        r.append(s);
      }
      d.append(r);
    });
    if (!hasBox) d.append(el('div', 'row', 'No box on the page for this value.'));
  }
})();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reading", type=Path, default=None, help="reading JSON to show")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "index.html")
    args = ap.parse_args(argv)
    reading = choose_reading(args.reading)
    out = build(reading, args.out)
    label = "FAKE reading" if reading.get("fake") else f"reading by {reading.get('model')}"
    print(f"Viewer built with {label}: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
