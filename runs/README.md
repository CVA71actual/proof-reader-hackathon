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
  see "Cost on the record"), status and reason, plus `pages_total` / `pages_read` when the run records its selected
  source-page count. Reports do not infer unread pages for older partial runs that lack that count.
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
- `rows[]`: dated company events. Every row and table value has a unique decision target ID; the job allocates row IDs
  from its persisted `next_row_number`, so pruning a row cannot re-use its ID. Duplicate saved targets refuse state,
  decisions and report generation until an explicit mapping repairs the run. Each event has a `status`, a `group`, a
  plain `reason`, its `checks` and a `trail`.
  For a legacy duplicate, `python -m runs.repair_ids <run>` validates but does not write its explicit `id_repairs`
  mapping. Target and exclusion/mistake reference entries name zero-based JSON paths and old/new IDs; each decision
  line targeting an ambiguous ID needs its own mapping. Inspect the dry-run output, then pass `--write` to persist.
  A reference can map only to a repaired target from that same old ID. The applied mapping remains in `run.json`;
  saved reports and accuracy scoring are marked stale. If interrupted between file writes, a pending journal keeps
  the run refused; rerun with `--write` to complete the repair.
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
  `event_in_past` is a legacy exclusion value. A printed past date never automatically excludes a new proposal;
  its date note stays reviewable, and a human-accepted historical event goes into Past.
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
2027). Its relationship to the event remains for human review (below). A period with no year ("Q3") fails. Vaguer
still ("later this year") with no date given: `not_needed`.

#### Printed dates and event meaning

`date_match` compares the complete printed date/period wording and the proposed
ISO bounds. A wrong day, wrong period, absent wording, partial year or missing
reference date cannot pass. A pass does not establish that the date belongs to
the proposed action, that the action is unconditional, or that guidance remains
current. Commas, keyword proximity and sentence order are not semantic evidence.
The model proposal and qualifications stay visible for human review. A passing
mechanical check never accepts the event.

A proposed past date does not automatically exclude a row. After a person Keeps
that reviewed proposal, the same calendar/report partition places it in Past.
The run retains historical context instead of discarding it through a keyword
or clause-based interpretation. Existing persisted exclusions remain history
until an explicit migration or human action changes their effective disposition.

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

`runs/escalate.py` sends a failed **literal date comparison** and the original row identity/quotation to
Muse Wonder (`muse-wonder-1.0`, `capture/wonder.py`). The image is a full-width band around the quote, padded by
60 points. The answer requests only date wording, bounds and supporting region, not a replacement event.
At most **1 row per page** is sent, including across resumes; the existing 25% Wonder share of a limited run's
spending limit and the ordinary paid-call gate still apply.

Only date_text/date/date_end may be patched. The answer must use the original passage and source/page;
containment alone is insufficient. Explicit identity or interpretation changes are retained for a person,
not applied. Omitted interpretation fields were not requested and remain unchanged on the original row.
An incomplete or failed repair is recorded without a repeated attempt for that row.

The same production date comparison must fail on the original values and pass on the candidate, with the other
mechanical checks satisfied. The before/after comparison is stored with the actual answer in escalation_proposal.
Only that demonstrated mechanical correction counts as a mistake caught by code. Title, program, indication,
conditions, relevance, relationship and original quotation are never replaced by the repair. A reader disagreement
remains held; the model answer is evidence only. A repaired row still needs explicit human acceptance, and a
material repair expires an earlier Keep.

The trail keeps separate Code send/instructions, Model answer, Code checks and Code application/refusal steps.
`run.escalation` retains its existing rows_sent/rows_replaced summary (replaced now means a bounded date patch).
The following is a historical full-event escalation example, not the current application contract: `fixtures/ptgx_..._p3.wonder.json` is one live escalation: the PTGX first reading was altered **on purpose** (one
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
- `unresolved`: rows now in `open` or `unverified`, plus rows now `corrected` whose fix is awaiting a re-check,
  whose re-check could not run or that are still `undecided`, the one "Needs attention" number. A later keep or remove takes the row out (its
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

## Research runs (RESEARCHER-FULL-001 S1)

A run whose `choices.mode` is `"research"` is driven by `runs/research.py` instead of the sources index: Muse Spark
1.3 researches each company on the Responses API (`capture/responses_reader.py`) with built-in `web_search` and the
function tools of `runs/research_contract.py` (`fetch_edgar`, `fetch_url`, `fetch_ctgov`, `read_source_pages`,
`propose_events`, `record_coverage`, `finish_draft`). Everything it may cite is captured by code first
(`capture/research_sources.py`): the exact bytes (kept under `research/captures/<source_id>/`), SHA-256, retrieval
time, rendered pages and words, and per page `research_checks` (`capture_incomplete`, `hidden_text`: words under 2pt).
Web HTML is rendered from a sanitized, isolated snapshot (no scripts, no network or file subresources); what was
removed is recorded under `sources[].retrieval.rendered.limitations`. Search results and SEC/registry listings are
leads, never evidence.

Choices: `spending_limit_usd` is the only limit that is on (default $30 for a research run; strict admission: a
call is made only if its worst case fits). `research_limits` holds the optional settings, every one null (off)
unless a person turns it on: `max_spend_per_company_usd`, `max_requests_per_company`, `max_searches_per_company`,
`max_searches_per_request`, `max_fetches_per_company`, `max_pages_per_company`, `max_minutes_per_company`,
`stop_on_empty_rate`; `concurrency` (default 3) only queues companies. Up to 10 companies.

Rows are ordinary event rows built by `runs.build_entry.build_row` (Keep, Remove, Undo, calendar and exports are
unchanged), plus `rows[].research`: label "Agent research — not independently reviewed", `revision`, every evidence
span with its own quote checks, `issues` (typed), `conditions_state` (null = not established, [] = none stated),
`near_duplicates` (linked, never merged), `revisions` (history) and `feedback`. When a non-primary span has the role
`timing`, `date_match` runs against that span and its reason names it. A research row's Keep binds its whole evidence
set (`runs/review.py research_basis`); other rows' bases are unchanged.

`run.research` (the server field the Agent trail reads): `label`; `limits`; `reservation` (the rule and its
assumptions); `companies[ticker]` (status queued | researching | finished | stopped | failed, `status_reason`,
counters `requests`/`searches`/`fetches`/`pages_delivered`/`sources`/`proposals`/`empty_answers`, `spend_usd`,
`limitation` {kind, reason}, `capture_failures`, `finish`); `steps` (the Agent trail: one actor per step, `kind`
request | search | fetch | proposal | check | coverage | finish | limit | repeat | error | prose, with `refs` such as
`call_id`, `source_id`, `row_id`, `queries`, `results`); `manifest` (summary of the append-only
`research/manifest.jsonl` with its checkpoint digests); `refusals`; `coverage` (including searched-not-found records
with retrieval times); `consolidations`; `repeats`; `kill_conditions`; `reservation_exceeded`; `review_reserve`
and `review[ticker]` (the independent review, below); `audit` (the draft audit, `runs/research_audit.py`). Exact requests and responses
are under `research/turns/`. A person stops a research run with `POST /api/runs/{id}/research-stop`. A research run
is not resumed in S1.

Search (BRAVE-PRIMARY-001, CVA 2026-11-02): `brave_search` (Brave Search, `capture/brave_search.py`, key from
BRAVE_API_KEY) is the primary search; Meta's built-in `web_search` is secondary. Brave is priced at $5 per 1,000
requests (`runs/prices.py`, run.prices.per_1k_requests_usd) and admitted through the same strict gate; one attempt,
a 429 is a recorded `search_limitations` entry. Storage policy: Brave titles and snippets reach the model for one turn
only and are never written to run.json, the Agent trail, research/turns/ or the manifest; only the query and result
URLs are kept. Without a key the tool is not offered and the company's `search_limitations` says so.

Independent review (RESEARCHER-FULL-001 S2 wiring, `runs/research.py` `review_company`): after each company's
research ends, for any reason except a person's stop (finished, spend cap, a safety stop, a limit, a technical
failure), `capture/research_reviewer.review_draft` attacks that company's draft. The reviewer is Claude Opus 5.5
(`claude-opus-5-5`, Anthropic, family Claude; MULTI-FAMILY-001, CVA 2026-11-03 "Claude for reviewer for now") through
`capture/research_claude.py` (the anthropic SDK, imported lazily; `client.beta.messages.stream` + `get_final_message`,
adaptive thinking with `block_binding: drop_block`, effort high, max_tokens 16384, tool_choice auto; NO server-side
fallback (Codex ruling, CAP-001: one request is one attempt, reserved exactly), the model that answered recorded in
`review.served_by` and any model without a known price unpriced; malformed tool_use blocks and invalid usage counts are
never a clean or priced result; every assistant turn's content goes back
unchanged; all tool results of a turn in one user message, `is_error` on failures; a `refusal` stop is recorded with its
category). DeepSeek V4 Pro on Baseten stays a configured option (our Baseten key was refused for it, HTTP 403). Muse
Spark Open is a declared fallback used only when Claude is unavailable or refused, and then the record
(`review[t].reviewer_choice`, `families.same_family: true`) and every label say it is the researcher's own family
("reviewed by Muse Spark Open (same model family as the researcher)", never "independently"). Labels name the reviewer
that ran, e.g. "Agent research — independently reviewed by Claude Opus 5.5 (different model family from the researcher)
(no findings)". Its first message
is the rows with their row ids and revisions, the LIST of the company's captured sources (id, title, publisher, kind,
URL, dates, page count, no text) and only the text of the pages the rows cite; it reads any other page with
`read_source_pages` (at most `PAGES_PER_READ` = 5 per call, a recorded request-size default). Requests replay the last
`REPLAY_TURNS` = 3 turns; older turns become a code-written summary (findings, pages read, searches), and page text and
search payloads reach the model once and are then retired to stubs. Scope rule: clean = complete, no findings, no
rejected attempt, no failed retrieval, and every CITED page read; a page never opened is a per-source `not_reviewed`
limitation (recorded and shown, not a blocker for clean). Its search is Brave Search (query and URLs
in the manifest, `stage: independent_review`; the payload is never stored); its fetch is the S1 capture path
(`register_source`, `retrieval.tool` `review_fetch`, a manifest `source` line), so it can cite only captured text. Every
reviewer, adjudicator and Brave request is reserved and settled on the run's one gate (strict, exactly once, unknown
usage unpriced); its counts are `companies[ticker].review_usage`. Findings that contest a row (every kind but
omission) go to `adjudicate` (Muse Wonder), fail-closed; a verdict is a proposed resolution, never a human acceptance.

Review reserve (`review_reserve`): research is admitted under the cap less `REVIEW_RESERVE_FRACTION` (15%, a code
default, recorded and on the trail), the review under the whole cap; no reserve is held when the reviewer cannot run.

`review[ticker]`: `status` complete | incomplete | not_run, `reason`, `clean`, `label`, `research_ended`,
`reviewed_basis` [[row_id, revision]], `disputes`, `review` (the reviewer's whole record: findings, rejected attempts,
limitations, searches, fetches, usage), `adjudication`, `usage`. Rows get `research.review_findings` (each finding
bound to that row: `finding_id`, `kind`, `text`, `by`, `row_id`, `revision_id`, `current`, `evidence`
[{source_id, quote, start, end}], `review_status`, `reviewed_at`, `adjudication` {outcome proposed_resolution |
unresolved | not_sent, reason, reading, resolution, evidence, model, human_accepted false}) and `research.review`
{status, reviewed_revision (null unless a complete review covered this revision), at}. Labels (never "verified";
"independently reviewed" only for a COMPLETE review): "Agent research — independently reviewed (no findings)" for a
complete and clean review; "Agent research — independently reviewed with findings" when a complete review made
current findings; "Agent research — partly reviewed (incomplete), findings so far" when findings exist but the review
did not complete; otherwise "Agent research — not independently reviewed". The run label is the clean one only when
every company's is; the findings one only when every company's review completed.

Row first (A-TO-Z-001 for the reviewer, after the 2026-11-02 live test gathered 37 searches and 40 fetches with no
conclusion): `assess_rows` gives draft rows a verdict (`no_issue`, or `finding` after `report_finding`). A search or
fetch runs only when at least min(all rows, turn x `ASSESS_ROWS_PER_TURN` = 5) rows have a verdict; otherwise it is
refused with a typed message and recorded in `order_refusals` (no request, no charge). One fetched source at a time: a
new fetch is refused until the last fetched source has a finding citing it or `nothing_material`. A fetch of an
already-captured URL (given or fetched) returns the same source_id with no request. Clean also needs every row
assessed; rows without a verdict are listed in `rows_not_assessed`. `rows[].research.review.verdict` carries the row's
verdict. These are recorded design defaults about order, not spend limits.

Adjudication room (CAP-001, live test 21e10cd): every reviewer request and search is admitted only if its own bound
PLUS the worst case of the adjudication requests the findings so far will need fits under the cap; otherwise the review
stops (incomplete, the reason names the amount kept back) and adjudication runs. An adjudication request carries only
the pages that hold the cited quotes (`adjudication_messages`), never whole documents. A second finding on the same
row(s) and kind is merged into the first (its evidence added, `merged` keeps the text), never a new finding.

Retrieval limits in labels (Red Team PR177_21e10cd, OUTCOME-001 / CLAIM-001): a source the reviewer could not open
(or whose captured text could not be given) and a search that did not complete are counted in `review[t].limits`.
Such a review is never clean; every label that claims a review carries them in plain words after a dash, e.g.
"Agent research — independently reviewed with findings — 1 source could not be opened", or for a complete review
with no findings "Agent research — independently reviewed — 1 source could not be opened". Row, company, run, export
and audit labels all carry it.

Reviewer access (live test 5d028a8): the model list does not reflect key permissions, so once per run a one-token
access check (`OneShotChat.probe`, through the gate) asks whether the key may use the reviewer. A 401/403 refusal is
recorded in `run.research.reviewer_access` with the provider's message; the refused model is never retried, and the
declared fallback (Muse Spark Open) reviews every company with the same-family label and `review[t].multi_family.met:
false`. A refusal on a first real call (no access check) is handled the same way.

Labels are derived, never read back (e2e #180, CLAIM-001 / UNKNOWN-001): `runs/research_labels.py
derive_review_labels(run)` sets every row, company and run label from the recorded review (its status, the findings
still current on the rows, the retrieval limits in its limitations, the reviewer that ran). It runs when a review is
saved and on every load (`runs/check_load.bind_on_load`), so the API, the report view, the PDF and the DOCX always show
what the record supports; a run saved by an older build gets the honest label without its file being rewritten.

Finding lifetime (Red Team PR177_21e10cd): a finding stays current until a later COMPLETE review addresses its row and
finds it resolved (no repeat finding of that kind, and the row assessed no_issue on the same revision; then
`resolved: true`); a repeat in a complete review supersedes it (`superseded: true`); a change of the row's revision or
evidence makes it `stale` (IDENTITY-001). Nothing is dropped because a new attempt completed.

Re-review (Manus 18:33): every attempt is numbered (`attempt`; earlier ones under `attempts`). A complete review
replaces the rows' findings. An attempt that did not complete keeps every earlier finding: on an unchanged basis (the
row's revision and whole evidence set, `basis`) it stays `current`; on a changed basis it is `stale: true` with a
`stale_reason`; each carries `latest_attempt` {attempt, status, at}. Row-less omissions are kept the same way in
`unbound_findings`. Steps: kind `review` (start, reserve, adjudication, outcome) and `finding`.

`python -m tools.research_smoke --review RUN_DIR --cap-usd N --i-have-the-operator-go` reviews a COPY of an existing
run's saved draft without researching (the copy's cap is its spend so far plus N).
