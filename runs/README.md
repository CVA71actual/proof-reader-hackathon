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
| `runs/<id>/run.json` | once, when the run ends | never |
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

- `run`: id, number, label, sample, start and finish times, the person's choices, spend (`null` = not recorded).
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
  program name as printed ("PN-8047"). "Keep as stated" is a normal
  outcome for period dates; the calendar must place periods.
- `mistakes[]`: genuine errors only: a proposed value that was wrong and got changed, or a row removed because its
  content was wrong. Each says what changed on which row or value, and who caught it: `code`, `second_reader` or
  `person`. An optional `type` names the mistake with one of the fixed codes in `accuracy/types.py`
  (WRONG_VALUE, PERIOD_MISREAD, WRONG_COMPANY, ...); `current_state` lists every counted mistake with its type
  and model under `mistake_list`, and `accuracy/mistake_log.py` counts them by type, model and who caught it.
- `exclusions[]`: rows left out under a rule, not because they were wrong: `id`, `target`, `rule`
  (`third_party_calendar_only`, `event_in_past`, `person_removed`), `reason`, `by` (`code` or `person`). A row
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
December). The date text must be a whole date or period in the quote ("2027" inside "Q3 2027" is not the year
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
   submission, filing, NDA, BLA, MAA; milestone: dosed, enrolled, initiated, initiation, completed, first patient), plus the
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

A row with no event words in the quote (in the sample, e10: "Year Ended December 31, 2025", an earnings row
whose title word "results" is not in the quote) is `could_not_run` for this check.

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
| Citation located | `checks/selector_match.py` `selector_match` |
| Right row and column | `checks/row_column_match.py` `row_column_match` (table values; `compared` reads "row <label>, column <header>") |
| Quote found on page | `checks/event_checks.py` `quote_match` |
| Date read as written | `checks/event_checks.py` `date_match` |
| Source is more than a calendar | `checks/event_checks.py` `source_rule` |

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
- A step never mixes a routing action and a model's interpretation. A Code step's `who` is never a model name,
  and a Model step's `who` is never a check function. The schema holds this: a Model `who` is one of the known
  model names, a Code `who` is a Python name, a Person `who` is "person", and no `who` names two actors. A Person step inside run.json means the person
acted while the run was still going; everything after the run ends goes to decisions.jsonl.

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
  The checks compare quote, date_text, date and date_end (a table value: value). A fix of any other field (title,
  company, ticker, event_type unless it is the disputed field) is not shown right by a re-check: unless a check
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
  checks before the edit." Settling the readers' disagreement by choosing the disputed value (event_type, which no
  check compares) is such an edit: it settles the row but shows no old error, so it is not counted.
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
fake reading offline. `runs/build_entry.py` turns each proposed event into a row: the same four checks the server
re-runs on a fix (`runs/recheck.py` `row_checks`), status from `row_status`, starting group from the status
(matched -> kept, failed -> open, anything else -> unverified). A full stop the reader copied from the end of the
sentence into `date_text` ("Q1 2027.") is dropped by code in its own "Tidied" step. `decisions.jsonl` starts empty.

```sh
.venv/bin/python -m capture.capture_source   # page 3 from ~/proof-reader-docs/r4a/ptgx_ex991.pdf (pinned hash)
.venv/bin/python -m capture.read_events      # live Muse Spark reading (needs MODEL_API_KEY); --fake offline
.venv/bin/python -m runs.build_entry         # runs/ptgx-r4a/
```

## Regenerate

```sh
.venv/bin/python -m runs.build_run       # runs/demo/ from fixtures/ (the real Muse Spark reading)
.venv/bin/python -m runs.build_sample    # runs/sample/
.venv/bin/python -m pytest -q tests/test_runs.py
```

Both builds are repeatable: the sample comes out byte for byte the same; the demo differs only in `finished_at`.
A fake reading (`--reading fixtures/ions_ars_2026_p95.reading.fake.json`) is labelled as fake, never
"Saved demo run".
