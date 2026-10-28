# runs: the contract between the screens and the code

The screens read two files per run. Nothing else.

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
  has an image, its size in PDF points, and a words file (every word with its box).
- `tables[]`: a table read from one page. Each value (`cell`) has an id, a `box` `[x0, y0, x1, y1]` in PDF points
  (origin top-left, y grows downward; `null` if the reader proposed no place), a `status` and its `checks`.
  `instructions` holds the exact text sent to the model and its SHA-256.
- `rows[]`: dated company events. Each has a `status`, a `group`, a plain `reason`, its `checks` and a `trail`.
  `date_text` is the date as printed. `date` is the exact day, or the first day of the period when `date_end` is set.
  `date_end` (optional, ISO or null) is the last day of the period when the source names one ("Q3 2027",
  "2H 2027", "mid-2027"): "Q3 2027" is `date` 2027-07-01, `date_end` 2027-09-30. "Keep as stated" is a normal
  outcome for period dates; the calendar must place periods.
- `mistakes[]`: genuine errors only: a proposed value that was wrong and got changed, or a row removed because its
  content was wrong. Each says what changed on which row or value, and who caught it: `code`, `second_reader` or
  `person`.
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
- `undecided` (rows only): the checks did not fail, but a second reader disagreed.
- `unreadable` (rows only): the reader's answer could not be understood.

"Date read as written" on a period: there is no exact day to compare, so it passes when the period words are
printed in the quote and `date`/`date_end`, if given, are the first and last day of that period (a vague period
such as "mid-2027", "early 2027", "late 2027" or "end of 2027" has no first or last day: `date`/`date_end` need
only be in order and, when the words name a year, both inside that year). Vaguer still ("later this year") with no date given: `not_needed`.

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
  and a Model step's `who` is never a check function. A Person step inside run.json means the person
acted while the run was still going; everything after the run ends goes to decisions.jsonl.

## Decisions and the state rule

One JSON object per line: `at`, `target` (a row id or a table value id), `action` (keep, fix, remove, undo,
recheck), `note`, `fix` ({field, from, to} on a fix, otherwise null), `by` ("person"; "code" on a recheck only).
A recheck line also carries `recheck` {result `pass` or `fail`, reason, checks (the check results)}.

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
- remove: the row goes to `removed`, with the person's note as the reason.
- undo: takes back that row's latest person decision still in effect. Undo twice takes back two. Undo of a fix also
  takes back its re-check.
- A decision that cannot apply changes nothing and is listed in `ignored` with a reason.

It also returns:

- `counts`: per group (rows and table values apart), plus `excluded_by_person` and `fixes_pending` (fixes still
  in effect that have no re-check yet).
- `mistakes` {code, second_reader, person}: who caught each value that was wrong and got changed. The person
  counter is the person mistakes in run.json plus every fix still in effect whose re-check passed: an edit counts
  only once it is a shown correction. A fix waiting for its re-check is in `fixes_pending`, not here; a fix whose
  re-check failed is not counted. A fix always changes the value (one with from == to is ignored). A person's
  remove is not a mistake.
- `excluded` {code, person} and `exclusions` (target, rule, reason, by): run.json exclusions whose row is still
  removed and untouched by the person, plus every row whose latest decision in effect is the person's remove
  (rule `person_removed`). A person who keeps a row the code left out takes it out of the exclusions.
- `unresolved`: rows still in `open` or `unverified`, the one "Needs attention" number.

Undo takes back a fix or a remove, so it lowers whichever counter that decision raised.

Table values start in a group from their status: matched -> kept, failed -> open, anything else -> unverified.

## Regenerate

```sh
.venv/bin/python -m runs.build_run       # runs/demo/ from fixtures/ (the real Muse Spark reading)
.venv/bin/python -m runs.build_sample    # runs/sample/
.venv/bin/python -m pytest -q tests/test_runs.py
```

Both builds are repeatable: the sample comes out byte for byte the same; the demo differs only in `finished_at`.
A fake reading (`--reading fixtures/ions_ars_2026_p95.reading.fake.json`) is labelled as fake, never
"Saved demo run".
