# proof-reader
Page-cited, cross-checked extraction from messy PDFs using a team of Meta models. Meta Global AI Developer Hackathon 2026.

## S1: one filing page, end to end

One real SEC filing page goes all the way through:

1. **Capture** (`capture/capture_page.py`): checks the source PDF's SHA-256, cuts out page 95 of 144 of the
   Ionis 2026 annual report to shareholders (Consolidated Statements of Operations), and records every word on
   the page with its box in PDF points. Output: `fixtures/ions_ars_2026_p95.pdf`, `.words.json`, `MANIFEST.json`.
   The full filing is not kept in this repo.
2. **Read** (`capture/read_table.py`): sends the page's words to Muse Spark (Meta) and asks for the table as JSON,
   with a proposed box for each value. The exact instructions sent, their SHA-256 and the raw answer are saved with
   the reading. If the answer can't be understood it is saved as unreadable; nothing is guessed.
3. **Check** (`checks/`): three code checks run on every value:
   - *Matches source text*: the value appears in the page's own text.
   - *Citation located*: the words inside the proposed box read the same as the value, and the box does not
     cut into any other word (a word counts as inside only when most of it is inside the box).
   - *Right row and column*: the box sits on the line of its row label and under its column header.

   A value is **matched** only when all three checks pass, **failed** if any fails, and otherwise stays **proposed**.
4. **View** (`viewer/build_viewer.py`): one HTML file with the table on the left and the page on the right, every
   proposed box drawn. Click a value to find its box; click a box to find its value.

### Run it

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m capture.capture_page --source /path/to/IONS_ARS_2026-04-23.pdf   # only to re-capture
.venv/bin/python -m capture.read_table --fake      # offline, clearly labelled FAKE reading
.venv/bin/python -m capture.read_table             # live Muse Spark reading (needs MODEL_API_KEY)
.venv/bin/python -m viewer.build_viewer            # writes viewer/out/index.html
.venv/bin/python -m pytest -q
```

The live reader uses `MODEL_API_KEY` from the environment or from `~/.config/proof-reader/env`
(`MODEL_API_KEY="..."`). The key is never printed or saved.

The fake reading is built by code from the words file, with three values changed on purpose so that failed and
proposed values show up in the viewer. It is always marked FAKE and is never shown as a model reading.

## R1: run.json, the contract for the screens

See [`runs/README.md`](runs/README.md): one `run.json` per run (never edited after the run ends), one append-only
`decisions.jsonl`, the schemas, a saved demo run, a sample run, and the state rule in `runs/state.py`.

## Accuracy and the mistake log

`accuracy/` measures how often a reader gets a page right, against an answer key, and keeps a log of every caught
mistake by type, model and who caught it.

- **Answer key** (`accuracy/key.py`): one JSON file per page: the source's sha256, the page number, the words
  file's sha256, `cells` [{row_label, column, value}] and `events` [{ticker, program, event_type, date_text,
  quote}]. Before anything is scored, `check_key` checks the key itself against the page: every value must pass
  `text_layer_match`, every row label and column must be printed on the page, every quote must pass `quote_match`
  and hold its date text and program, and nothing may be listed twice. A key that fails is refused with every
  offending entry listed, so a typo in the key can never count against the reader. A key must say
  `"complete": true` (it lists every value and event the page holds) or `false` (a partial key); a key silent on
  it is refused. A column is the same column however the year is printed ("2025", "FY2025", "December 31, 2025",
  "Dec. 31, 2025"): key and scorer compare columns by `checks.row_column_match.column_key`. A key event may carry
  `aliases` (other names for its program, such as the drug for an indication); the scorer matches any, and a name
  used for two programs is refused.
- **Scorer** (`accuracy/score.py`): `score_table` counts, out of the key's N values: found (right value, saved
  under the right row and column, every check passed), wrong value, wrong place (right value but a box or
  row/column check failed), missed; on a complete key, values the key does not hold are extra (a second copy is
  `DUPLICATE`); on a partial key they are **unscored**: not a mistake, no type, never in the mistake log (the key
  holds 10 of 40 values and the reader read all 40 right: 10/10 found, 30 unscored).
  `score_events` counts, out of the key's M events: found (same ticker, program, event type and day or period,
  every check passed), wrong date, wrong type, not backed by the checks, missed, and invented (another program,
  another company, a duplicate, an unreadable answer; on a partial key a program the key does not hold is
  unscored instead). `score_planted` plants wrong values into copies of real
  cells, runs the real checks and counts how many were caught. Every item not found carries a type code and
  says whether the checks flagged it.
- **Mistake types** (`accuracy/types.py`, fixed list): WRONG_VALUE, WRONG_ROW, WRONG_COLUMN, BOX_DRIFT,
  QUOTE_NOT_ON_PAGE, DATE_NOT_IN_QUOTE, PERIOD_MISREAD, DATE_IN_PAST, WRONG_EVENT_TYPE, WRONG_COMPANY,
  SOURCE_NOT_ALLOWED, DUPLICATE, UNREADABLE_OUTPUT, each with plain words for the screen. A failed check maps to
  one code; when several checks fail on one item, the value check names it first, then row and column, then the
  box. `mistakes[]` in run.json carries an optional `type`; the state rule lists every counted mistake with its
  type and model (`current_state(...)["mistake_list"]`), and `accuracy/mistake_log.py` counts them by type x
  model x caught_by for a run and its decisions.
- **Report** (`accuracy/report.py`, `python -m accuracy.run`): plain text and JSON. Per model: values n/N, events
  m/M, planted wrong values caught k/k, and the mistakes by type; then "Right value, check failed" listed apart (the
  reader had the value or event right and a check stopped it, with the check's own reason text); then every other
  miss with its page and what the key expected. Unscored items are counted, never listed as misses.

```sh
.venv/bin/python -m accuracy.run --key accuracy/keys/ions_ars_2026_p95.key.json \
    --reading fixtures/ions_ars_2026_p95.reading.json --words fixtures/ions_ars_2026_p95.words.json \
    --planted accuracy/keys/ions_ars_2026_p95.planted.json
.venv/bin/python -m accuracy.run --key accuracy/keys/ptgx_8k_2026-08-05_ex991_p3.key.json --run ptgx-r4a
.venv/bin/python -m accuracy.run --key accuracy/keys/ions_ars_2026_p95.key.json --run sample   # + mistake log
```

### Numbers on the unsealed keys (tooling only, not a measured error rate)

Both keys in `accuracy/keys/` were written by Claude from the page words, so they test the tooling, not the
reader. They are not the held-out result.

| Page | Model | Result |
|---|---|---|
| Ionis annual report, page 95 (real reading) | Muse Spark | values 69/69 (0 wrong value, 0 wrong place, 0 missed, 0 extra) |
| Ionis page 95, 12 planted wrong values | Muse Spark reading | caught 12/12 (wrong value 4/4, wrong row or column 4/4, box drift 4/4), each with the planted type |
| Protagonist 8-K exhibit 99.1, page 3 (`runs/ptgx-r4a`, arrives with R4a) | Muse Spark | events 2/2 (PN-8047 Q1 2027; PN-458o second half of 2027) |
| Sample run mistake log | Muse Spark | 3: PERIOD_MISREAD by code, WRONG_COMPANY by the second reader, WRONG_EVENT_TYPE by the person |

### The sealed run (R4b-2)

1. Someone other than Claude picks 5 real public pages (SEC filings or IR decks with a text layer) from the 12
   companies and writes one key per page in the format above, with `"sealed": true` and each page's sha256.
2. The keys and pages live in the record repo's held-out folder, not in this repo. Claude does not open them while
   building the reader.
3. Someone other than Claude runs the reader on the pages, then `python -m accuracy.run --key <key> --run <run>`
   for each page. A key that fails `check_key` is refused and fixed by its writer before anything is scored.
4. The report (every miss listed) goes into the record; the audit header then reads "Accuracy (sealed set):
   <model> n/N values, m/M events; planted errors caught k/k".
