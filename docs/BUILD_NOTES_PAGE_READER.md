# Proof Reader: build notes from the earlier page-reading version

These notes describe the page-reading version built earlier in the hackathon (one model reading captured filing pages, before the research agent). Its figures were recorded during development and were not independently re-verified for this submission.

Proof Reader is a biotech catalyst calendar built from SEC filings: Meta models propose the events, code checks every row against the page, and a person decides. It is for anyone who needs the dates that move a drug company (trial results, filings, conferences) with each one tied to a quote on a saved page, so nothing has to be taken on trust. The thesis in one line: a model checking its own work did not help; a second model plus code did; a person still decides.

Meta Global AI Developer Hackathon 2026.

## Run it without a key

No key, no network, no money. Looking at the saved runs calls no model.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # about 15 seconds
.venv/bin/python -m runs.demo
```

`runs.demo` lists the saved runs, prints one run's numbers, then serves the app on this machine only. Ctrl-C stops it.
With no flags it prints and opens the run **`ptgx-r5`**. The app opens on that run's Audit log at
<http://127.0.0.1:8777/#/ptgx-r5/audit>. The same address with another run name opens that run:
`http://127.0.0.1:8777/#/<run>/audit`. `--run twelve-4` prints and opens that run instead. `--port 8800` uses another
port. `--no-serve` prints and stops. The "Saved runs" button in the app switches runs.

A six-line walkthrough:

1. `ptgx-r5` (one real page, two readers). The terminal says: 1 page read, 2 rows kept, 0 need attention, cost $0.0319,
   $0.0159 per kept row. It also says the two readers agreed on 2 of 2 rows and "Unsealed (author key): events 2/2".
2. In the app, open **Audit log** and click PN-8047 (Q1 2027). Read its five checks and the quote on the page image.
3. Click **Open agent trail** to see the exact instructions sent to the model, its answer and each code check.
4. Open **Catalyst Calendar**: the same two rows, PN-8047 and PN-458o (second half of 2027).
5. Switch to the demo run, `twelve-4` (4 companies, 114 pages). Counts: kept by code 29, removed by rules 43,
   unverified 6, still open 18; 24 rows need attention (demo run, 29 Oct). Its Calendar shows the kept rows with a day, and the rest under "No day given".
6. Open **Export** for any run to get its PDF and Word reports.

Saved runs are never rebuilt: the screens read `runs/<id>/run.json`, `decisions.jsonl` and
`accuracy.json` as saved (which of them are hash-checked is listed under "Where each number comes from"). `sample` is made-up data and says so on every screen. `twelve-4` has no `accuracy.json`, so its
accuracy line says no scoring is saved.

## What you are looking at

One line per screen in the app:

- **Audit log**: every row a model proposed, with its five code checks and the quote on the page image.
- **Judgment needed**: the rows a check failed or could not settle, where a person presses Keep, fix or remove.
- **Catalyst Calendar**: the rows that were kept, by date, with who kept each one (code or a person).
- **Source view**: the saved page itself with each proposed box drawn on it.
- **Agent trail**: the exact instructions sent to a model, its answer and each code check, step by step.
- **What changed**: what differs between two runs, row by row.
- **Export**: the PDF and Word reports for a run.

## What the labels mean

**Matched** means no check failed and none could not run. A check that does not apply to a row (for example a
date check on a row with no date) shows as a dash, and the badge says "date not checked" or "type not checked" so
you can see what was not looked at. **Kept by code** (shown as "Checked and kept") means the checks and the rules kept the row; no person looked.
**Kept by you** means a person pressed Keep, and the decision is written to `decisions.jsonl`. A Keep overrules a
failed check: the row stays on the calendar, and the calendar shows "Kept by you" beside the failed check; it never turns it into a tick.
Matched never means "true in the world": it means the quote is on the saved page, the date is in the quote, and the
other checks passed.

## The numbers

Every figure below says which count it is. There are two kinds. **Check status** is the code's own mark on each row
(matched, failed, proposed, undecided), before the rules. **Kept after rules** is what is left after the window rule
and the other rules take rows out; this is what the screens and the calendar show.

### The demo run

For the demo run `twelve-4` (4 companies, 114 pages, 96 rows) (demo run, 29 Oct):

| Count | Number |
|---|---|
| By check status: matched / failed / undecided / proposed | 41 / 24 / 18 / 13 |
| Kept after rules, by code | 29 |
| Removed by rules (for example a date already past) | 43 |
| Unverified / still open | 6 / 18 |
| Needs attention | 24 |
| A person's decisions | 0 |
| Cost | $2.57 |
| **Cost per kept row (one figure)** | **$0.0886, about $0.09** (= $2.57 over 29 kept rows) |

These are the demo run's figures on 29 Oct; the run is replaced by a new one before submission and the table is refreshed from it.

Matched by check status (41) is larger than kept after rules (29) because some matched rows are removed by the
window rule: their date is outside the six months. Earlier text in this repo gave other twelve-4 figures; they used a
different count and are replaced by the table above. Other runs: `ptgx-r5`: 2 rows kept, $0.0319, $0.0159 per kept row.

### The sealed run: measured result

**Sealed run 5.** 15 live pages of run twelve-3; 53 upcoming events keyed; the keys are sealed in the record
repo (SEAL 2967944a…b35c779e).

- Inside the run's 6-month window there were 19 key events. Between them the two readers proposed all 19 (0 missed); not each reader alone. Code confirmed 6 ("found").
  10 were proposed but held for a person ("unsupported": a check failed). 2 had the wrong date, 1 the wrong type, and 0 were missed.
- Outside the window there were 22: 18 proposed and left out by the window rule, 1 proposed and kept, 3 never proposed.
- Unplaced (vague dates such as "mid-2027"): 12.
- Invented in-window rows: 17.
- A single reader alone: 2 of 6 in-window.

The run folder and the scoring record are outside this repo.

### Meta results, honestly

- **Picture pages**: On the 13 slide-style pages in the demo set, pages the first reader failed went from 20 to 4 and matched rows from 9 to 16 when the pages were read through the image reader (Muse Spark Open on the page image) (demo run, 29 Oct). Recorded outside this repo, not in a run folder here.
- **Reader tools** (the first reader calls the checks while it reads): matched fell from 15 to 7. It did not
  help, so it ships **off**.
- **SAM 3.1** found no text regions on the real documents it was tried on (it answered only for the whole page). Not shipped.
- **Muse Wonder** re-read 2 rows in the demo run and replaced none (demo run, 29 Oct).
- A model checking its own work did not help. A second model plus code checks did. A person still decides.

## Run it with a key

1. Put your keys in `~/.config/proof-reader/env`, one per line, as `MODEL_API_KEY="..."`. The code never prints or saves them.
2. One command, with a spending limit:

   ```sh
   .venv/bin/python -m runs.twelve --companies all --window 6 --limit 5.00
   ```

   (The app's New run panel does the same thing.) Add `--reader-tools` to let the first reader call the checks while
   it reads. It is off by default.
3. What it spends: real money. The run stops at the limit you set. The demo run, `twelve-4`, read 114 pages of 4
   companies and cost $2.57 (demo run, 29 Oct). Prices are in `runs/prices.py`.

## Where each number comes from, and what is not in this repo

Offline, you can re-derive everything saved here from files in this checkout:

- `runs/<id>/run.json`: the rows, check status, sources, the cost, and the exclusions. Written when the run ends and
  not edited after.
- `runs/<id>/accuracy.json`: the scoring against an answer key, with `run_sha256` (the exact `run.json` it was scored
  from) and `content_sha256` (its own content). Only `ptgx-r5` and `sample` have one.
- `runs/<id>/decisions.jsonl`: every person's Keep, fix, remove or undo. Append-only: lines are added, never edited.
  `twelve-4` has none, which is why every kept row there is "kept by code".
- The agent trail, each row's `trail` in `run.json`: the exact instructions sent to a model (with their SHA-256),
  its answer, and each code check.

What is hash-checked, and what is not:

- `run.json` records the SHA-256 of each page file and of the words file; these are checked before a run is served.
  `run.json` itself is not self-hashed.
- `accuracy.json` carries `run_sha256` (the `run.json` it was scored from) and `content_sha256` (itself); both are
  checked before it is served.
- `decisions.jsonl` is append-only and carries no hash.

A hash shows damage or a swap; it does not stop someone who rewrites a file and its hash together.

**Not in this repo:** `twelve-2` and `twelve-3` (the full live runs; twelve-3 is 113 MB) and the sealed answer
keys with their results (sealed run 5). The sealed keys are kept out on purpose: the person who builds the reader must
not see them, so they are not a fair test if published. The 15 to 7, 20 to 4 and 9 to 16 figures above also come from the
project records, not from a run folder here. Those numbers you can read about but not re-derive offline.

## Technical notes

### S1: one filing page, end to end

One real SEC filing page (page 95 of 144 of the Ionis 2026 annual report) goes through four steps:

1. **Capture** (`capture/capture_page.py`): checks the source PDF's SHA-256, cuts out the page and records every word with its box in PDF points. Output: `fixtures/ions_ars_2026_p95.pdf`, `.words.json`, `MANIFEST.json`. The full filing is not kept in this repo.
2. **Read** (`capture/read_table.py`): sends the page's words to Muse Spark and asks for the table as JSON with a proposed box per value. The instructions sent, their SHA-256 and the raw answer are saved; an answer that can't be understood is saved as unreadable, never guessed.
3. **Check** (`checks/`): *matches source text*, *citation located* (the box reads the same as the value and cuts into no other word) and *right row and column*. All three pass: **matched**; any fails: **failed**; otherwise **proposed**.
4. **View** (`viewer/build_viewer.py`): one HTML file with the table beside the page, every proposed box drawn.

By hand:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m capture.capture_page --source /path/to/IONS_ARS_2026-04-23.pdf   # only to re-capture
.venv/bin/python -m capture.read_table --fake      # offline, clearly labelled FAKE reading
.venv/bin/python -m capture.read_table             # live Muse Spark reading (needs MODEL_API_KEY)
.venv/bin/python -m viewer.build_viewer            # writes viewer/out/index.html
.venv/bin/python -m pytest -q
```

The live reader uses `MODEL_API_KEY` from the environment or from `~/.config/proof-reader/env`; the key is never printed or saved. The fake reading changes three values on purpose so failed and proposed values show up; it is always marked FAKE. `viewer.build_viewer` shows the real reading (`fixtures/ions_ars_2026_p95.reading.json`) whenever it exists; for the fake one pass `--reading fixtures/ions_ars_2026_p95.reading.fake.json`. Re-capture needs the full filing, whose SHA-256 is checked against `fixtures/MANIFEST.json`.

### R1: run.json, the contract for the screens

See [`runs/README.md`](runs/README.md): one `run.json` per run (not edited after the run ends), one append-only `decisions.jsonl`, the schemas, a saved demo run, a sample run, and the state rule in `runs/state.py`.

### Accuracy tooling and the mistake log

`accuracy/` measures how often a reader gets a page right against an answer key, and logs every caught mistake by type, model and who caught it.

- **Answer key** (`accuracy/key.py`): one JSON file per page (source sha256, page, words sha256, `cells`, `events`, and `"complete": true|false`). `check_key` checks the key against the page first, and a key that fails is refused with every offending entry listed, so a typo in the key never counts against the reader. Columns compare by `checks.row_column_match.column_key`.
- **Scorer** (`accuracy/score.py`): `score_table` counts found, wrong value, wrong place, missed, and extra (on a partial key, values it does not hold are *unscored*, never a mistake). `score_events` counts found, wrong date, wrong type, not backed by the checks, missed and invented. `score_planted` plants wrong values into copies of real cells and counts how many the real checks catch. Every item not found carries a type code.
- **Mistake types** (`accuracy/types.py`, fixed list of 14, WRONG_VALUE to MISSED_DATED_SENTENCE, each with plain words for the screen). `accuracy/mistake_log.py` counts them by type x model x caught_by.
- **Export** (`accuracy/export.py`): `python -m accuracy.run --key <key> --run <id> [--planted <file>] --export <id>` writes `runs/<id>/accuracy.json` with `run_sha256` and `content_sha256`, `sealed` and `complete`, the commit it was made on and whether the tree was dirty. The server serves it only if the hashes hold (409 if changed or scored from another version of the run, 404 if there is no scoring). A hash shows damage or a swap; it does not stop someone who rewrites both. See the module for every field.
- **Report** (`accuracy/report.py`): plain text and JSON, per model: values n/N, events m/M, planted caught k/k, mistakes by type, then "Right value, check failed" apart from every other miss. Unscored items are counted, never listed as misses.

```sh
.venv/bin/python -m accuracy.run --key accuracy/keys/ions_ars_2026_p95.key.json \
    --reading fixtures/ions_ars_2026_p95.reading.json --words fixtures/ions_ars_2026_p95.words.json \
    --planted accuracy/keys/ions_ars_2026_p95.planted.json
.venv/bin/python -m accuracy.run --key accuracy/keys/ptgx_8k_2026-08-05_ex991_p3.key.json --run ptgx-r4a
.venv/bin/python -m accuracy.run --key accuracy/keys/ions_ars_2026_p95.key.json --run sample   # + mistake log
```

#### Numbers on the unsealed keys (tooling only, not a measured error rate)

Both keys in `accuracy/keys/` were written by Claude from the page words, so they test the tooling, not the
reader. They are not the held-out result.

| Page | Model | Result |
|---|---|---|
| Ionis annual report, page 95 (real reading) | Muse Spark | values 69/69 (0 wrong value, 0 wrong place, 0 missed, 0 extra) |
| Ionis page 95, 12 planted wrong values | Muse Spark reading | caught 12/12 (wrong value 4/4, wrong row or column 4/4, box drift 4/4), each with the planted type |
| Protagonist 8-K exhibit 99.1, page 3 (`runs/ptgx-r4a`) | Muse Spark | events 2/2 (PN-8047 Q1 2027; PN-458o second half of 2027) |
| Sample run mistake log | Muse Spark | 3: PERIOD_MISREAD by code, WRONG_COMPANY by the second reader, WRONG_EVENT_TYPE by the person |

#### How the sealed run was done

1. Someone other than Claude picks 5 real public pages (SEC filings or IR decks with a text layer) from the 12
   companies and writes one key per page in the format above, with `"sealed": true` and each page's sha256.
2. The keys and pages are kept outside this repo. Claude does not open them while
   building the reader.
3. Someone other than Claude runs the reader on the pages, then `python -m accuracy.run --key <key> --run <run>`
   for each page. A key that fails `check_key` is refused and fixed by its writer before anything is scored.
4. The report (every miss listed) goes into the record; the audit header then reads "Accuracy (sealed set):
   <model> n/N values, m/M events; planted errors caught k/k".

#### Earlier sealed runs on one page (runs 1-4)


- Run 1: Muse Spark alone, 10/40 values, 0/4 events.
- Run 2: the same readings after the check fixes, 40/40.
- Run 3: fresh reading, 40/40; planted errors 48/48; 0/4 events.
- Run 4: both readers 40/40 each, 3/4 events each, 93% agreement, no shared wrong table value; both readers shared one wrong event type (CDX-622).

The sealed run caught our own check bugs before the model's.

#### Live runs (all 11 indexed companies, 313 pages)


- twelve-2: 305 rows, $7.68, cost per kept row $0.15.
- twelve-3: 295 rows, $7.79, cost per kept row $0.13 (old count; not comparable to the $0.0886 above).
- Date-check failures went from 67 to 27 between the two runs. These are two live runs and the models are not
  deterministic, so this is a comparison, not a measured effect.
- Committed demo run twelve-4 (4 companies, 114 pages, $2.57, $0.0886 per kept row on 29 kept rows) (demo run, 29 Oct).
  Check status: matched 41, failed 24 over 114 pages; after rules, 29 kept. Against twelve-1, which covers 105 of
  twelve-4's 114 pages: on the 105 pages both runs read, check-status matched went 20 -> 38 and failed 61 -> 23.

Prices are the ones in `runs/prices.py` (`PRICE_STATUS` says where each comes from). Runs made before the price table was confirmed (Job 19; twelve-2, twelve-3 and twelve-4 among them) tallied Muse Spark Open at the placeholder ($0.20 / $0.80) and carry no price table; their recorded spend is kept. Runs made under this code carry the table in run.json (run.prices).

### A page where the second model changed the answer

Historical behavior from 29 October: the example below predates the bounded-repair contract. Current code does
not accept type, title, program or indication changes because keyword checks pass. Escalation can repair a
mechanically demonstrated date error on the original passage; interpretations remain proposals for human review.

Run twelve-3, row `e3`: the Ionis (IONS) 8-K exhibit 99.1 page, page id
`src-ions-8k-0001140361-26-037371-ex991-p1`, sha256 `5b558015e2f28510c98fd9c16457cf31f23ac0c4869a35d52fd6d2658ad6e569`.
Muse Spark proposed the event as a conference; the check "Event type matches the quote" failed ("The quote's words

check. The trail step reads:

> Escalation result: Muse Wonder's reading passed every check. The row takes it (event_type from conference to trial_results; title from Interim analysis data presentation to Interim analysis data presentation at upcoming medical congress; program from sefaxersen to IMagINATION; indication from primary IgA nephropathy to IgAN).

Before and after: event_type conference → trial_results; title "Interim analysis data presentation" → "Interim analysis
data presentation at upcoming medical congress"; program sefaxersen → IMagINATION; indication "primary IgA nephropathy" →
IgAN. The page and the quote are the same; the checks, not the second model, decided.

The twelve-3 run folder is held outside the repo for size (113 MB). Its `run.json` has sha256
`e6703d1a41a934d428e3c485e9ea336fe667f888847836e6347659d65504cc4c`. The committed demo run twelve-4 sent 2 rows to
Wonder and replaced 0 (demo run, 29 Oct).
