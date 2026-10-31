# runs: the contract between the screens and the code

The screens read two files per run. Nothing else.

## How the UI gets data

Through the local decisions server (`server/`, see `server/README.md`): `GET /api/runs` lists the runs,
`GET /api/runs/<id>` returns `run.json` plus its `state` (the state rule below, with `decisions.jsonl` applied),
and `POST /api/runs/<id>/decisions` writes a person's keep, fix, remove or undo. Page images and words files are
served at `/runs/<id>/pages/<file>`, the same relative paths `run.json` names. The screens never write the files
themselves; the server is the only writer, and it only appends.

For offline UI work the committed files still stand on their own: `runs/sample/run.json` and
`runs/sample/decisions.jsonl` can be loaded as static files, and `runs/state.py` is the rule to mirror.

| File | Written | Changed later? |
|---|---|---|
| `runs/<id>/run.json` | once, when the run ends (a run started from the New run panel is rewritten by its job while it is going, and never once its status is `done`; see `server/README.md`) | never after the run ends |
| `runs/<id>/decisions.jsonl` | one line each time a person keeps, fixes, removes or undoes, and each time code re-checks a fix | lines are only added, never edited |

Schemas (JSON Schema 2020-12): `schema/run.schema.json` and `schema/decision.schema.json`.
Every path inside `run.json` (page images, words files) is relative to the run's folder.

## Two runs to build against

- **`runs/demo/`**: label "Saved demo run". Real data only: one source (Ionis annual report, page 95), one table
  read by Muse Spark (69 values), every value checked by code. No event rows yet.
- **`runs/sample/`**: label "Sample run", `run.sample = true`. The same real table, plus 11 made-up event rows,
  each with `"sample": true`, that show every group, status, check result and trail tag. The made-up sources are
  tiny generated pages titled "Sample source" about companies that do not exist ("Samplebio One", ticker
  "SMPL1"...). One sample row (e10) quotes the real Ionis page. `decisions.jsonl` holds five example decisions,
  including a keep on a row whose checks failed (e7) and one whose two readers disagreed (e8).
  Always show that sample rows are sample.

## What is in run.json

- `run`: id, number, label, sample, start and finish times, the person's choices, spend (`null` = not recorded;
  see "Cost on the record").
- `sources[]`: where things came from. `kind` is filing, company_release, third_party_calendar or other. Each page
  has an image, its size in PDF points, and a words file (every word with its box), each with its sha256
  (`image_sha256`, `words_sha256`) so a changed byte in the run folder is noticed. The tests check the committed
  runs against these hashes, and the server checks them every time it loads a run (listing, showing, deciding,
  serving a page file): if any page file is missing or differs by one byte, the whole run is refused with 409
  and a sentence naming the file ("This run is not served: its file pages/p95.png does not match the hash
  recorded in run.json."), `GET /api/runs` lists it only as `{"id", "refused"}`, no file of it is served and
  no decision is written to it. A page file is served only if run.json names it, and the bytes sent are the
  bytes that were hashed. A decision re-check also reads its words file once, verifies those bytes against
  `words_sha256`, and parses that same byte snapshot. A missing or changed file at consumption is refused
  before any decision is appended, even if the earlier run-load verification passed.
  A source EDGAR holds only as HTML carries `rendered_from` {format "html", sha256 of the filed HTML, renderer}:
  its pages were rendered to PDF by code, `sha256` is then the rendered PDF's, and every screen says "page rendered
  by code from the filed HTML".
- `tables[]`: a table read from one page. Each value (`cell`) has an id, a `box` `[x0, y0, x1, y1]` in PDF points
  (origin top-left, y grows downward; `null` if the reader proposed no place), a `status` and its `checks`.
  `instructions` holds the exact text sent to the model and its SHA-256.
- `rows[]`: dated company events. Each has a `status`, a `group`, a plain `reason`, its `checks` and a `trail`.
  `date_text` is the date as printed. `date` is the exact day, or the first day of the period when `date_end` is set.
  `date_end` (optional, ISO or null) is the last day of the period when the source names one ("Q3 2027",
  "2H 2027", "mid-2027"): "Q3 2027" is `date` 2027-07-01, `date_end` 2027-09-30. `program` (optional) is the drug or
  program name as printed ("PN-8047"); `indication` (optional) is the disease or condition it is for, as printed. The
  scorer and the key may alias either. `found_by` (optional) is "coverage" on a row code found (below). "Keep as stated" is a normal
  outcome for period dates; the calendar must place periods.
- `mistakes[]`: genuine errors only: a proposed value that was wrong and got changed, or a row removed because its
  content was wrong. Each says what changed on which row or value, and who caught it: `code`, `second_reader` or
  `person`. An optional `type` names the mistake with one of the fixed codes in `accuracy/types.py`
  (WRONG_VALUE, PERIOD_MISREAD, WRONG_COMPANY, ...); `current_state` lists every counted mistake with its type
  and model under `mistake_list`, and `accuracy/mistake_log.py` counts them by type, model and who caught it.
- `exclusions[]`: rows left out under a rule, not because they were wrong: `id`, `target`, `rule`
  (`third_party_calendar_only`, `event_in_past`, `person_removed`, `outside_time_window`: a run started with a time window: a row whose period ends before today or starts after today plus the window; an exact day is both its start and its end. Reason, for a period: "the period ends 2026-06-30, before today (2026-10-29)"), `reason`, `by` (`code` or `person`). A row
  removed by the house rule is an exclusion, never a mistake.
  A person may keep a row the house rule left out (decided by CVA, 2026-10-27). The row then shows
  "Kept by you" and still shows that its only source is a third-party calendar; the audit log never hides that.

To draw a box on the page image: `scale = image_width_px / width_pt`, then multiply all four numbers by `scale`.

### Statuses (what the checks say)

- `matched`: no check failed, none was left unrun, at least one passed, and no second reader disagreed.
- `failed`: at least one check failed.
- `proposed`: a check could not run, so the reader's answer stands as a proposal.
- `undecided` (rows only): the checks did not fail, but a second reader disagreed. The row's optional
  `disagreement` {field, first_reader, second_reader} says on what (in the sample, e8's event_type).
- `unreadable` (rows only): the reader's answer could not be understood.

"Date read as written" on a period: there is no exact day to compare, so it passes when the period words are
printed in the quote and `date`/`date_end`, if given, are the first and last day of that period (a vague period
such as "mid-2027", "early 2027", "late 2027" or "end of 2027" has no first or last day: `date`/`date_end` must
fall inside its rough window, early = January to April, mid = May to August, late / end of = September to
December). Part of a quarter is one month: "early Q1 2027" is January 2027 (2027-01-01 to 2027-01-31),
"mid-Q2 2027" is May 2027, "late Q4 2027" is December 2027 (early = the quarter's first month, mid = its middle
month, late = its last); like any month it has a first and last day, so `date`/`date_end`, if given, must be
exactly those, and a row that claims the whole quarter ("Q1 2027") on a quote that says "early Q1 2027" fails. The
date text must be a whole date or period in the quote ("2027" inside "Q3 2027" is not the year
2027), and it must be the date the quote gives for the event (below). A period with no year ("Q3") fails. Vaguer
still ("later this year") with no date given: `not_needed`.

#### Which date belongs to the event

Applies to every exact day and named period in `date_text` (`checks/event_checks.py` `_supporting_clause`). The
order in which dates appear in the quote is never evidence.

1. Four digits joined to a code by a hyphen ("PN-8047") are a program name, never a year.
   The quote is split into clauses at sentence ends, at `;` `:` `,`, and before "that", "which", "and", "but",
   "while". A boundary never falls inside a date ("March 12, 2027" stays whole). A clause that begins "follow",
   "compared with/to", "versus", "vs", "than" or "relative to" is a comparison clause and never names the event.
2. Event words: the fixed words for the row's `event_type` (trial results: results, data, readout, topline,
   report; regulatory decision: PDUFA, decision, approval, CRL, action date; launch: launch, commercial
   availability; conference or presentation: present, presentation, conference, late-breaker; filing: submit,
   submission, filing, NDA, BLA, MAA; milestone: dosed, enrolled, initiated, initiation, completed, first patient;
   earnings: results, earnings, revenue, net loss, quarter ended, year ended; other: none), plus the
   longest word of 4 letters or more in the row's title, plus the drug or program name if the row has one. Case
   does not matter. The event clause is a clause that holds a date and an event word and is not a comparison.
3. Decision:
   - No event clause, or more than one with different dates: `could_not_run`, "Could not tell which date in the
     quote belongs to the event." The row stays unresolved and goes to a person.
   - One event clause, and it does not hold `date_text`: `fail`. "The quote attaches Q3 2027 to the data; Q3 2026
     is a comparison." (or "...; 2026 is not the date the quote gives for the event."). If the clause holding
     `date_text` negates or replaces it: "The quote says Q3 2027 is no longer the expected period; it now names
     Q4 2027."
   - One event clause holds multiple different dates and negation: `could_not_run` before interpreting
     "from" / "to" as a transition. For example, "Results will not move from Q3 2027 to Q4 2027" does not
     establish a new Q4 date or disprove the Q3 date. The check leaves both claims unresolved.
   - Otherwise, one event clause, and it holds `date_text`: `fail` if the clause negates it (not, no longer, never, cannot,
     n't, previous / previously, prior, superseded, anywhere in the clause), if it puts `date_text` after "from",
     "instead of" or "rather than", or if it introduces a second, different date as the new one (now / to /
     until / into in the three words before it, or "instead" just after it). "Now expected", "moved to",
     "delayed" and "pushed" move the event to the date that follows them, so they fail `date_text` only when it
     comes before them ("The Q3 2027 readout has been delayed"), not when it follows ("Results are now expected
     in Q4 2027" passes). If the clause holds a second date and neither, or both, are introduced as the new one:
     `could_not_run`. Otherwise the clause supports `date_text`, and the day or period comparison above applies.
4. Worked cases (tests `test_codex9_*` in `tests/test_runs.py`, each with and without `date`/`date_end`):
   "Q3 2027 is no longer the expected readout period." Q3 2027: fail. "Q3 2027 was the previous target; results
   are now expected in Q4 2027." Q3 2027: fail naming Q4 2027; Q4 2027: pass. "In 2026 the company announced that
   results are expected in Q3 2027." Q3 2027: pass; 2026: **fail** (the event clause attaches Q3 2027; chosen
   over could_not_run because the quote does say which date the results have). "Data in Q3 2027, compared with
   Q3 2026." Q3 2026: fail (comparison); Q3 2027: pass. "Topline data are expected in Q3 2027; results will be
   presented in Q4 2027." either date: could_not_run. "Topline data on March 12, 2027 follow the Q4 2026
   results." March 12, 2027: pass; Q4 2026: **fail** (the "follow" clause is a comparison; the event clause
   attaches March 12, 2027).

A row with no event words in the quote is `could_not_run` for this check. (In the sample, e10 is an earnings
row quoting "Year Ended December 31, 2025": "year ended" is an earnings word, so its date check runs and passes;
e10 stays `proposed` because it has no box, so Citation located could not run.)

A tick on screen means `pass` and nothing else. `could_not_run` and `not_needed` are not ticks.

### Groups (where a row sits)

`kept`, `corrected`, `removed`, `unverified`, `open`. On screen they read exactly as in the approved mockup:
"Checked and kept", "Corrected", "Checked and removed", "Unverified", "Still open". "Unverified" is the one
approved use of that word (it says a row did NOT pass its checks). Otherwise keep off screen: "verified",
"proved", and "correct" on its own ("corrected" as a group name is fine).

### Checks

Every check has `name`, `label` (screen words), `kind` "Code", `result` (pass, fail, could_not_run,
not_needed), a one-line `reason`, `compared` {looked_for, found} and `code_ref` {file, function}: the real
function in this repo that ran it. Today's checks:

| Label | Function |
|---|---|
| Matches source text | `checks/text_layer_match.py` `text_layer_match` |
| Citation located | `checks/selector_match.py` `selector_match` (table values: strict; event quotes: the quote rule below) |
| Right row and column | `checks/row_column_match.py` `row_column_match` (table values; `compared` reads "row <label>, column <header>") |
| Quote found on page | `checks/event_checks.py` `quote_match` |
| Date read as written | `checks/event_checks.py` `date_match` |
| Source is more than a calendar | `checks/event_checks.py` `source_rule` |
| Event type matches the quote | `checks/event_checks.py` `type_match` |

**Citation located for a quote box** (event rows, `selector_match(..., quote_lines=True)`). A quote is a sentence, and a
sentence rarely starts at the margin or ends at the other one, so the box passes when every word of the quote lies
inside it and it does not reach above the quote's first line or below its last line; other words on those same
lines (the end of the sentence before, the start of the next) are allowed. Fails say which: "The box does not hold
every word of the quote (missing ...)", "The box reaches above the first line of the quote.", "... below the last
line of the quote." A quote printed more than once is checked against the run the box covers; a quote not printed
at all falls back to the strict check. Table values keep the strict rule.

**Event type matches the quote** (event rows). The row's `event_type` must be a kind of event its own quote's words
fit: the fixed words per kind in `event_checks.py` `_ANCHORS` (trial results: results, data, readout, topline,
report; regulatory decision: PDUFA, decision, approval, CRL, action date; launch; conference; filing; milestone:
dosed, enrolled, initiated, initiation, completed, first patient), matched as whole words, case ignored. `pass`: the
quote holds a word for the row's kind ("The quote's words (initiation) fit milestone."). `fail`: it holds none for
that kind but does for another ("The quote's words (results, topline) point to trial results, not milestone.").
`not_needed`: the quote holds no fixed word for any kind of event, or the type has no fixed words (earnings, other,
unknown): there is nothing to compare, and the row is not held back for it. `could_not_run`: the row has no quote.
When the quote holds words of both milestone and trial results (one sentence often does: "Phase 1 initiation ...
data"), it does not say which the event is, and the check returns `not_needed` ("... nothing decisive to compare
the type with"); when it holds only one family's words, the check decides. A person's fix of the event type is
re-checked by it like a fix of the date.

### The second reader

`capture/second_reader.py` is an independent read of the same page by Muse Spark Open: through Baseten, model
string exactly `meta-models/muse-spark-open-1026`, base URL https://inference.baseten.co/v1; the OpenRouter fallback
(`meta/muse-spark-open`) is used only if `OPENROUTER_API_KEY` is present and the run's OpenRouter spend (kept in
its `job.json`) is under $20. It is given the same kind of prompt, built by the same builder from the page's words
and the run's choices; it is never given the first reader's answer (nothing in its interface takes one). Its tokens
and cost are its own (`runs/prices.py`, "Muse Spark Open", confirmed price, Baseten).

`runs/compare.py` compares the two readings by code, event by event. Two are the same event when one quote is a
**contiguous run of words** of the other (case, spacing and the punctuation at a word's edge ignored) and any program or
indication name they both give is shared. A handful of the same words in another order, or a bag of words that merely
overlaps, never pairs two passages: the second passage would be consumed and agreement written for text the first reader
did not read. Compared:
`date`, `date_end`, `event_type`, `program`, `indication` (case and spacing ignored; nothing against a value is a
difference). The quote is the match key: quotes that run to different lengths are not a disagreement.

- Any difference: the row becomes `undecided` (`failed` if a check had failed) in group `open`, with `disagreement`
  {`field`, `first_reader`, `second_reader`} for the first differing field in the order above (`also` lists the
  others), so it is in Judgment needed. Only a fix of `field` settles it. A person's fix that puts in what the second
  reader read, on any differing field, and is a demonstrated mistake (the re-check passes where the old value failed),
  is counted under `mistakes.second_reader`, not `person`.
- No difference: nothing changes. Agreement is not evidence: only the checks say a row matched, and the trail says so.
- A row the second reader read no matching event for: nothing changes (the trail says so).
- An event only the second reader read: a new open row, `found_by` "second_reader", `proposed_by` "Muse Spark Open",
  its own checks run, status `undecided` (`failed` if a check failed), reason "Proposed by the second reader only: the
  first reader did not read it...". An open "Possible event not read" row for the same sentence is dropped.
- Table values: the same row label and column (`column_key`) with a different value makes the cell `undecided`
  (`failed` if a check had failed) with `disagreement` {field "value", ...}; it sits in the "unverified" group until a
  person fixes the value. The second reader may give a cell more than one value (two rows with one label, a repeated
  column): every value is kept, and one that differs from the first reader's is a disagreement even when another of its
  values agrees; the first differing value is the one recorded.
- An event only the second reader gave, on a page that was not captured (no source page or no words), is kept as an open
  second-reader row with no check run on it and the reason said ("The page was not captured...").

Trail, one actor per step: "Instructions sent to the second reader" (Code, `capture.second_reader`, the exact prompt),
"Second reading" (Model, Muse Spark Open, with its share of the call's tokens and cost), "Compared" (Code,
`runs.compare`). `run.second_reader` {model, status read / not_run / unreadable, reason} says whether the second reader
read the run at all. The saved live reading of the PTGX page is `fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second.json`
(10,791 prompt and 5,902 completion tokens; the first reader's 10,586 and 3,518) and `runs/ptgx-r5/` is the run built
from both readings (`python -m runs.build_entry --second ...`): the readers agreed on both events, so nothing changed
and both rows stay `matched` by their checks.

```sh
.venv/bin/python -m capture.second_reader      # one live second read of the PTGX page (needs a key)
.venv/bin/python -m runs.build_entry --second fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second.json
```

### The sources index and the twelve-company run

`runs/sources.py`. Which pages exist for which company is **the sources index**: `runs/sources_index.json` (written by
`capture/fetch_filings.py`: recent 8-K exhibits for the twelve tickers, each rendered to a PDF page exactly like the PTGX
pin, hashed, stored under `~/proof-reader-docs/filings/<TICKER>/`). When that file is absent the built-in index is used (the
PTGX 8-K exhibit page 3, the IONS annual report page 95, and the PTGX page as a scan), so nothing changes without it.
`runs/sources_index.sample.json` is a two-page SAMPLE with the format. A page record has the fields of a `fixtures/MANIFEST.json`
entry (ticker, company, form, exhibit, exhibit_title, filing_date, accession, source_url, source_format, source_sha256,
rendered_pdf_sha256, renderer, page_number, page_file, page_sha256, words_file, words_sha256, captured_at) and, optionally,
`id`, `kind` (`events`, the default, or `table`), `source_id` and `file_stem`. Two pages may not share a `file_stem` (compared without regard to case, after the characters the loader replaces are
replaced): the index is refused at load, because the second capture would overwrite the first page's files and break its hash. The
built-in scanned PTGX page has its own stem (`p3-scanned`). A run reads **every indexed page** of each company
it is asked for. Each page is **captured on first use** into the run's folder (`pages/<id>.png`, `pages/<id>.words.json`):
the page and words files are checked against the index's hashes (a changed byte stops the run with the reason, `failed`,
resumable), the page is rendered to an image, and the source is described with the hashes. Event pages go through both readers
(and Wonder for a row that fails a check), table pages through the table reader; coverage, the time window and the turn and
spending limits are as for any run. `companies: ["all"]` means every company in the index.

A run's label is made from its choices ("12 companies · 6 months", "PTGX · 12 months", "IONS"); its number is the next in the
sequence. `python -m runs.twelve --companies all --window 6 --limit 5.00` creates and runs such a run from the command line
by the same code as `POST /api/runs/new`, resumes it through turn-limit pauses, builds its PDF and Word report when it is
done, and prints the audit counts (rows and values by group, needs attention, mistakes caught by code / second reader /
person, exclusions), the calendar count, the cost and the cost per kept row.

### The recorded twelve-company run

`runs/twelve-1/` is the one live run on the captured index (`runs/sources_index.twelve.json`, written by
`capture/fetch_filings.py`), made once with

    python -m runs.twelve --id twelve-1 --companies all --window 6 --limit 5.00

(both readers and Muse Wonder on; `--id` keeps it under that name; its number comes from the sequence). Its layout:
`run.json` (a real run: `sample` false, status `done` or the stop it ended on), `decisions.jsonl` (a person's decisions, empty
until someone decides), `job.json` (what the job kept: the calls it made and a log line for every rewrite of `run.json`),
`pages/` (one `<id>.png` and `<id>.words.json` per page read, hashed in `run.json`), `report/` (one PDF and one Word file,
built by code when the run is done) and `accuracy.json` only if a key exists for its pages (none of the twelve companies' pages is
keyed in this repo, so normally there is none). `pages/` holds exactly the pages `run.json` names (the job removes the files of a
page it captured and then stopped before reading). `tests/test_twelve_live.py` checks the layout, checks the real index against the
job, and pins the numbers of the recorded run; its groups are skipped, with the reason, when their files are absent.

**How `runs/twelve-1/` ended** (live, 2026-10-29 04:46 to 05:47 UTC, `--window 6 --limit 5.00`, both readers and Wonder on): it
read 105 of the index's 313 pages (IONS 19, PTGX 55, VKTX 28, RVMD 3 of 12; ABVX has no indexed page) and stopped at the spending
limit ($5.06 of $5.00: the call that crosses the limit stops the run), so its status is `needs_input` and **no report was built**
(a report is built when a run is done). No page broke it, none was skipped, the fallback route was not used. Six turn-limit pauses
were resumed by the command. 95 rows: 14 kept, 3 corrected, 28 removed by the time window, 2 unverified, 48 open; cost $5.055, cost per
kept row $0.2974. The numbers are the real ones; they are not good ones (most rows fail a check), and the failures are read in INBOX_7.

### The second reader reads the image

The second reader is given the **page image** (the run's own `pages/pN.png`, the bytes `run.json` hashed) and a prompt
with **no word list**, so its read does not depend on the text layer; its boxes come back on a 0-1000 grid over the
image and code turns them into PDF points. If its image-only answer cannot be read, the words are sent too, once,
as a fallback (a counted turn). `run.second_reader.input` (`image`, `image_and_words` or `words`) says which it got,
and each compared row's trail says it in a "Second reader input" Code step before the second reader's own
"Instructions sent". The first reader still reads the words. (`python -m capture.second_reader` records a live read;
`--words` the earlier form: `fixtures/ptgx_..._p3.events.second.json` is the image read, 3,246 prompt and 3,772
completion tokens, and `.events.second_words.json` the words read, 10,791 and 5,902.)

### A scanned page: SAM 3.1 regions

`runs/scan.py`, `capture/sam.py`. A page with no text layer has no words for the text-layer checks, so: **SAM 3.1** (the
Meta Model API's Responses route, model `sam-3.1`, streaming; one request per concept, a short noun phrase) is asked for
"table" and "block of text"; each box it returns is a region (a text block inside a table is dropped, near duplicates are
dropped, reading order); each region's crop is read by the first reader with the event instructions, its boxes on a 0-1000
grid over the crop turned into PDF points; every proposal is a row with the same five checks. Quote found on page **could not
run** (no text layer) and Citation located is the **region check** (`region_match`): the box must lie inside a region SAM 3.1
found. So a row on a scan is at best `proposed`, never `matched`: nothing printed was compared with it. The trail says so:
Code "Scanned page", Model "SAM 3.1" (each request is one counted turn, priced per image processed from `runs/prices.py`,
$2.50 per 1,000 images, confirmed), then the reader's own steps. The source page carries `scanned: true` and its `regions`.
In a run, the company `PTGX-SCAN` is the PTGX page rendered to an image alone; each call's answer is kept in `job.json`, so a resume
never pays for a call twice. A scanned page gets no second read yet and no escalation.

**What SAM 3.1 did on the real page (recorded in `fixtures/ptgx_..._p3.sam.json`):** it matched nothing for "table" or "block of
text", and on the IONS financial-statement page it also matched nothing for "table", "grid", "row", "paragraph" or "line of
text"; it answered only for "page" and "document" (one box, the whole page). It is an object segmenter, not a document layout
model. So when neither concept matches, the run asks for "page" and the one region is the whole page; a whole-page region says
nothing about where a quote is, so **Citation located could not run** ("SAM 3.1 found no table or text block on this page; its
only region is the whole page...") and the row stays `proposed`. `runs/ptgx-scan/` is that run: the reader read the scan from
its image alone (3,149 prompt and 1,851 completion tokens) and found the same two events; three SAM requests, $0.0075.
The path is built for a segmenter that does find regions; this one, on documents, does not.

**Not built: a Muse Glimmer first pass.** There is no host for Glimmer this week (it is not on the Meta Model API), so the
cheap-first-pass item was dropped (the Operator's ruling, 2026-10-29). The cost per kept row is therefore unchanged by it.

### Escalation to Muse Wonder

`runs/escalate.py`. When a row fails a check, or the two readers disagree on it, that one row's **page region** (a
full-width band of the page image around its quote, 60 points above and below) and the **failing check's label and
reason** go to Muse Wonder (`muse-wonder-1.0` on the Meta Model API, `capture/wonder.py`, key `MODEL_API_KEY`) for a
re-read. Only for a failed **date** or **event type** check (a box or quote failure is mended by code or is a
paraphrase Wonder cannot cure; a disagreement is settled only by a person, so Wonder's reading would be evidence
only), at most **1 row a page**, and Wonder's spend stops at **25% of a limited run's spending limit**
(`escalate.WONDER_SHARE`; the job log notes it). Each call is one counted, priced turn. Its answer is a new proposal, built with the same
builder as every row, so the same five checks run on it:

- A row sent for a failed check, whose re-read passes **every** check, takes the re-read's values (group `corrected`,
  status `matched`), and the failure is added to `mistakes[]` as caught by `code` (the check) with its type.
- A re-read that does not pass every check changes nothing: the row stays as it was and its trail says why.
- (A row the readers disagree on is no longer sent. Older runs may hold Wonder's reading of one as evidence: "as Muse
  Spark did" / "as neither reader did"; such a row is never replaced.)

Trail, one actor per step: Code "Sent to Muse Wonder because <check> failed" (`runs.escalate`), Code "Instructions
sent to Muse Wonder" (`capture.wonder`), Model "Muse Wonder" (tokens and cost), Code "Checked" for each of the five checks on its
proposal, Code "Escalation result". `run.escalation` {model, status read / not_run, rows_sent, rows_replaced} summarises
it. `fixtures/ptgx_..._p3.wonder.json` is one live escalation: the PTGX first reading was altered **on purpose** (one
date moved) so a row fails; Wonder, shown that row's band and the failed check, read the right event and period
(2027-01-01 to 2027-03-31), and the row stayed as it was because Wonder's box reached below the quote's last line and
Citation located failed (976 prompt and 2,631 completion tokens).

### The fix wave (Job 12)

Found by reading the rows of `runs/twelve-1`:

- **Box located by code.** When the quote is printed on the page (as `quote_match` reads it, typographic marks folded) and
  the reader's box does not pass the citation check, the box is the words found there (`build_entry.locate_box`) and
  the trail has a Code step "Box located by code"; the reader's box was a hint. A box that already passes is left as the
  reader drew it. Both readers' rows go through the same builder. A quote that is not printed (a paraphrase) gets no box.
- **One date send-back.** For the rows of a page whose date text is not in their quote, the same reader is asked once
  (one counted turn for the page) for the sentence that states the date. It is taken only if it is printed on the page;
  the checks then decide the row. Trail: "Sent back for the date", "Sentence for the date" (Model), "Send-back result".
- **Relative periods from the filing date.** "by year-end", "this year", "later this year", "this quarter", "next
  quarter" take their year from the source's filing date (`event_checks.relative_period`): year-end is September to
  December, as `vague_period_window`. A row with no dates gets the period (trail "Year inferred from the filing date",
  Code); `date_match` passes when the row's dates lie inside it.

- **Two-digit years (Codex P2).** A two-digit year written with an apostrophe (`Q1 ’27`, `Aug ’26`, `1H ’27`) is read as
  20YY (`event_checks.expand_short_year`), in the quote and in the date text alike. A judgment call: without the
  apostrophe `Aug 26` is a day and stays unread; a slide's `’27` is never read as 1927 or 2127.

### Job 13: year-less dates, and what is around a date

- **No year printed** ("Friday, Sept. 4 at 10:00 a.m. ET", "December 12"): the year is the first such day on or after
  the source's filing date (`event_checks.yearless_day`). The row's trail has the Code step "Year inferred from the
  filing date", a matched row's reason ends "Year inferred from the filing date.", and `date_match` compares the full
  date (the row must give that day and no period).
- **Around a date**: a leading weekday ("Thursday, "), "today,", a trailing time of day ("at 10:00 a.m. ET", "7:30 pm -
  8:30 pm ET"), brackets and footnote marks ("(3Q’26)", "Feb ’27*") and "calendar year" are not part of the date
  (`clean_date_text`); the date text must still be printed in the quote as written. A same-month range ("November 14 -
  17, 2026") is two days: the row's date and date_end must be them.
- **Send-back for the second reader's rows**: the same once-a-row rule, asked of the second reader, after it has read. The
  row stays the second reader's open row.
- Of the 60 twelve-2 rows that failed 'no day and no period', only 2 had no year printed; most were forms the parser did
  not read. Now 22 pass, 19 could not run (the quote does not show which of its dates is the event's) and 19 still fail:
  'today' (10: the date of the release is not the date of the filing), '1Q27', 'by the end of September 2026', 'Sept/Oct
  2026', 'through at least December 31, 2027', and footnote digits ('(20261)').

### Dated sentences the reader did not turn into rows

`checks/coverage.py` cuts the page's words into sentences (a leading bullet is dropped; abbreviations such as "Inc."
do not end one; more than 80 words with no sentence end is a table or list, not a sentence) and looks in each for a
date or period that starts after the **source's published date** (a filing's own date: an old filing's already-past
dates raise nothing, whatever day the run is made; a run may be given an explicit date instead). The date parsers are
the date check's own: exact days, quarters, halves, months, "mid-2027", "in 2028". A sentence that no proposed row
quotes (a quote that holds the whole sentence, or that holds 80% of its tokens counting repeats: a word that occurs
three times in the sentence needs three in the quote; a quote that is only a piece of the sentence does not cover it)
becomes one open row (`runs/coverage.py`): title "Possible
event not read: <sentence>", `status` undecided, `group` open, the sentence as its `quote` with its box, the date
or period it names, no checks, `found_by` "coverage", and a trail that says no model was asked and code found it.
Fields a coverage row carries (Job 42), for the screen to read:

- `quote` and the title hold the sentence **cleaned**: leading or trailing page numbers, footnote marks (digits glued
  after a word, daggers) and ® ™ © and stray symbols are taken off, spaces collapsed. `raw_sentence` keeps it exactly as
  the page prints it. ("® 23 IMCIVREE HO Country-level Launches Expected in 2027 1." becomes "IMCIVREE HO Country-level
  Launches Expected in 2027".) The sentence's own full stop stays unless it belonged to a stripped page number, so the
  quote still matches the page.
- `guess`: `{date_text, date, date_end, program, event_type}`, every field a guess code made, null when it cannot see
  one. The date or period is the first one the sentence names, read by the date check's parsers ("2027" is the year
  period 2027-01-01 to 2027-12-31; "mid-2027" the window May to August). `program` is a program the run already has a
  row for if the sentence names it, else the first all-caps or brand-cased word before the verb. `event_type` is one
  of the app's types (`trial_results`, `regulatory_decision`, `launch`, `conference`, `filing`, `milestone`) chosen
  from the same word table the type check uses (initiation and dosing give `milestone`).
- `kind`: "missed", the marker for a possible event neither reader turned into a row; `found_by` stays "coverage".
- `status_text`: what the pane says: "Both readers skipped this sentence. It names <period>. Is it an event?". Dates
  in `reason` and the trail are in words ("after 4 Aug 2026"), never ISO.

A person keeps it as an event, fixes it, or removes it; until then it is under Needs attention and never on the
calendar. The scorer never counts these rows as proposals; a key event the reader missed that one of them surfaced
is typed `MISSED_DATED_SENTENCE` and counted as caught by code.

### Trail

One step per thing that happened: `step`, `tag` (Model, Code or Person), `who`, `text`, `at`. The first step is
always "Instructions sent" with the exact text sent to the model.

Every step has exactly one actor:

- Routing is Code ("Sent back to Muse Spark because Date read as written failed"); `who` is the code that did it.
- A reading or a re-reading is Model; `who` is the model name ("Muse Spark", "Muse Spark Open") and `text` is what
  it read.
- Applying a reading to the row is Code, in its own step after the Model step ("Date changed from 2027-06-03 to
  2027-06-30, as the new reading gave").
- A check is Code; `who` is the check function.
- A Model step carries `tokens` {prompt, completion} and `cost_usd`; the first step, "Instructions sent", carries
  the prompt's token count in `tokens` (completion null). See "Cost on the record".
- A step never mixes a routing action and a model's interpretation. A Code step's `who` is never a model name,
  and a Model step's `who` is never a check function. The schema holds this: a Model `who` is one of the known
  model names, a Code `who` is a Python name, a Person `who` is "person", and no `who` names two actors. A Person step inside run.json means the person
acted while the run was still going; everything after the run ends goes to decisions.jsonl.

### Cost on the record

Every Model step in a row's trail carries the tokens of its call (`tokens` {prompt, completion}) and `cost_usd`,
priced from the fixed table in `runs/prices.py` (dollars per 1M input and output tokens). The confirmed prices
(dev.meta.ai, 2026-10-29) are Muse Spark $1.25 in / $4.25 out (cached input $0.15), Muse Wonder $4.00 / $20.00 (cached $0.20) and
SAM 3.1 $2.50 per 1,000 images; Muse Spark Open (Baseten, 2026-10-29) is $1.25 / $4.25 (cached $0.15)
(`PRICE_STATUS` says where each comes from).

Runs made before the price table was confirmed (Job 19; twelve-2, twelve-3 and twelve-4 among them) tallied Muse Spark Open at the placeholder ($0.20 / $0.80) and carry no price table; their recorded spend is kept. Runs made under this code carry the table in run.json (run.prices).
 Cached tokens are not recorded in the saved readings, so every input token is priced at the
input price. The builders take the tokens from the saved
reading's `usage`; when one call proposed several rows, its tokens are split evenly across their Model steps
(remainder to the first, `tokens.call_rows` says how many rows shared it), so the call is counted once. The first
step, "Instructions sent", carries the prompt's whole token count. `run.spend_usd` is the sum of the Model step
costs, and `current_state` returns `cost_per_kept_row_usd`: spend divided by the rows now in "kept" or
"corrected", `null` when no row is kept. Nothing is estimated: a reading with no recorded usage (the demo's table
reading, any fake reading) gives `tokens` and `cost_usd` null and `spend_usd` null. The sample run's token counts
are fixed sample numbers (`runs/build_sample.py` `SAMPLE_TOKENS`) and are shown as sample, like the rest of it.

## Decisions and the state rule

One JSON object per line: `at`, `target` (a row id or a table value id), `action` (keep, fix, remove, undo,
recheck), `note`, `fix` ({field, from, to} on a fix, otherwise null), `by` ("person"; "code" on a recheck only).
A recheck line also carries `recheck` {result `pass`, `fail` or `could_not_run`, reason, checks (the check results),
`before` {result, checks}, `after` {result, checks}}. `before` is the same checks run on the item as it was just
before the fix (the old value); `after` is the run on the fixed item and equals the line's own result and checks.

`runs/state.py` `current_state(run, decisions)` is the reference rule (pure, no files):

- keep: the row goes to `kept`. Its `status` (what the checks said) does not change, and the state is marked
  `decided_by_person: true` with `last_action`, so the screen says "Kept by you" and still shows the checks.
  Every decision works this way: a person's choice is never shown as a check result.
- fix: the row goes to `corrected`, the field changes from `from` to `to` (the `from` must be the current value),
  and the row is marked `needs_recheck: true` with `recheck: null`: its checks were for the old value.
- recheck (written by code, never by a person): the checks were run again on the fixed value. It belongs to the
  target's latest fix still in effect that has no re-check yet (otherwise it is ignored). `pass`: the row stays
  `corrected` with status `matched`. `fail`: the row goes back to `open` with status `failed`. Either way
  `needs_recheck` becomes false and `recheck` holds the result and the check results.
  `could_not_run` (no check failed, but at least one had nothing to compare, for example no box): this is not a
  failure. The row stays `corrected` with status `proposed`, still decided by the person; the fix stays in
  `fixes_pending` (not a mistake) and the row counts in `unresolved`, so it shows under Needs attention. The reason
  says which check: "The re-check could not run: Citation located had nothing to compare."
  Only a fix of the row's `disagreement.field` settles the readers' disagreement; when its re-check passes the row
  becomes `matched` and the reason starts with "Settles the readers' disagreement by the person's fix. " A fix of
  any other field leaves the row `undecided` (group `corrected` if its re-check passed) and under Needs attention.
  The checks compare quote, date_text, date, date_end and event_type (a table value: value). A fix of any other
  field (title, company, ticker) is not shown right by a re-check: unless a check
  fails, the re-check is `could_not_run` with "No check compares <field>; the fix is recorded but not
  demonstrated.", the status stays what the checks said, and the fix stays pending. If the cited page was not
  captured, the page checks could not run ("Page N was not captured."); no other page is used.
- remove: the row goes to `removed`, with the person's note as the reason.
- undo: takes back that row's latest person decision still in effect. Undo twice takes back two. Undo of a fix also
  takes back its re-check.
- A decision that cannot apply changes nothing and is listed in `ignored` with a reason.

It also returns:

- `counts`: per group (rows and table values apart), plus `excluded_by_person`, `fixes_pending` (fixes still
  in effect that have no re-check yet, or whose re-check could not run, on rows not now removed) and
  `edits_without_error` (fixes still in effect whose value passed its checks both before and after the edit).
- `mistakes` {code, second_reader, person}: who caught each value that was wrong and got changed. The person
  counter is the person mistakes in run.json plus every fix still in effect that is a demonstrated mistake.
  A fix is a demonstrated mistake only when both hold: the re-check of the new value passes (`after` is `pass`),
  and the same checks on the old value fail (`before` is `fail`): the old value was wrong. The fix's field must be
  one a check compares, or the disputed field it settled.
  If `before` is `could_not_run` and `after` is `pass`, the fix is recorded but does not count as a mistake.
  It is marked `before_undemonstrated: true`, with the reason "The old value could not be assessed; no mistake
  was demonstrated." Its before and after evidence remains in the trail.
  If `before` is `pass` and `after` is `pass`, the fix is an edit without an error, for example 115,315 changed
  to $115,315 on a value that already matched: the edit is recorded and the row stays `corrected` / `matched`,
  but it is not counted; it adds 1 to `counts.edits_without_error` with the reason "The value already passed its
  checks before the edit." Settling the readers' disagreement by choosing the disputed value (event_type) is such an edit when
  the quote fits both types: it settles the row but shows no old error, so it is not counted.
  A re-check line with no `before` run is not counted. A fix waiting for its re-check is in `fixes_pending`, not
  here; a fix whose re-check failed is not counted, whatever `before` said. A fix always changes the value (one
  with from == to is ignored). A person's remove is not a mistake. Undo of a fix takes back exactly what that fix
  added: a mistake, an edit without an error, or nothing.
- `excluded` {code, person} and `exclusions` (target, rule, reason, by): run.json exclusions whose row is still
  removed and untouched by the person, plus every row whose latest decision in effect is the person's remove
  (rule `person_removed`). A person who keeps a row the code left out takes it out of the exclusions.
- `unresolved`: rows now in `open` or `unverified`, plus rows now `corrected` whose fix's re-check could not run or
  that are still `undecided`, the one "Needs attention" number. A later keep or remove takes the row out (its
  unrun check stays visible in `recheck`); undo of that keep or remove puts it back.
- `cost_per_kept_row_usd`: `run.spend_usd` divided by the rows now in `kept` or `corrected`; `null` when the spend
  was not recorded or no row is kept.

Undo takes back a fix or a remove, so it lowers whichever counter that decision raised.

Table values start in a group from their status: matched -> kept, failed -> open, anything else -> unverified.

## The first real entry

**`runs/ptgx-r4a/`**: label "PTGX · one real entry", `sample` false. One real source: Protagonist Therapeutics'
8-K filed 2026-08-05, exhibit 99.1 (the Q2 2026 results release), page 3 of 6. EDGAR holds the exhibit as HTML
only, so it was rendered to PDF by code (Google Chrome 156.0.8078.48 headless print-to-pdf); the pin record
holds both fingerprints, `capture/capture_source.py` refuses any other file, and only page 3 is in the repo
(`fixtures/ptgx_8k_2026-08-05_ex991_p3.*`, entry under `entries` in `fixtures/MANIFEST.json`).

`capture/read_events.py` sends the page words to Muse Spark (strict JSON, the fixed event types trial_results,
regulatory_decision, launch, conference, filing, milestone) and saves the exact prompt, its SHA-256, the raw
answer and the parsed events to `fixtures/ptgx_8k_2026-08-05_ex991_p3.events.json`; `--fake` builds a labelled
fake reading offline. `runs/build_entry.py` turns each proposed event into a row: the same five checks the server
re-runs on a fix (`runs/recheck.py` `row_checks`), status from `row_status`, starting group from the status
(matched -> kept, failed -> open, anything else -> unverified). A full stop the reader copied from the end of the
sentence into `date_text` ("Q1 2027.") is dropped by code in its own "Tidied" step. `decisions.jsonl` starts empty.

```sh
.venv/bin/python -m capture.capture_source   # page 3 from ~/proof-reader-docs/r4a/ptgx_ex991.pdf (pinned hash)
.venv/bin/python -m capture.read_events      # live Muse Spark reading (needs MODEL_API_KEY); --fake offline
.venv/bin/python -m runs.build_entry         # runs/ptgx-r4a/
```

## Table pages and event types

A page that is a table (today IONS page 95) is read by the table reader whenever its company is chosen in a new
run; the event types chosen on the New run panel apply to event pages only.

## Regenerate

```sh
.venv/bin/python -m runs.build_run       # runs/demo/ from fixtures/ (the real Muse Spark reading)
.venv/bin/python -m runs.build_sample    # runs/sample/
.venv/bin/python -m pytest -q tests/test_runs.py
```

Both builds are repeatable: the sample comes out byte for byte the same; the demo differs only in `finished_at`.
A fake reading (`--reading fixtures/ions_ars_2026_p95.reading.fake.json`) is labelled as fake, never
"Saved demo run".
