# server: the local decisions server

A small FastAPI app the screens call. It serves runs, takes a person's decisions, re-checks a person's fix
with the real check functions, returns one state per row, and starts and resumes runs (R6, below). Only a run's
job calls a model, through one reader (`capture/reader.py`); the decisions, listing and page code never touches a
model, the env file or `MODEL_API_KEY`.

## Run

From the repo root, on this machine only (uvicorn binds to 127.0.0.1 unless told otherwise; do not pass `--host`):

```sh
~/proof-reader-docs/.venv/bin/python -m uvicorn server.app:app --port 8777
```

Then open http://127.0.0.1:8777/ (the `ui/` folder, when it exists).

## Endpoints

`GET /api/runs`: every run under `runs/`, with counts from the current state.

```sh
curl -s 127.0.0.1:8777/api/runs
# [{"id": "demo", "number": 1, "label": "Saved demo run", "sample": false, "started_at": "...",
#   "finished_at": "...", "counts": {"rows": {...}, "cells": {...}, "fixes_pending": 0, "edits_without_error": 0,
#   "excluded_by_person": 0},
#   "unresolved": 0}, {"id": "sample", ...}]
```

`GET /api/runs/{id}`: `run.json` as it is, plus `state`, the output of `runs/state.py`
`current_state(run, decisions)` (targets, counts, mistakes, excluded, exclusions, unresolved, ignored,
cost_per_kept_row_usd).

```sh
curl -s 127.0.0.1:8777/api/runs/sample | jq '.state.targets.e7.group'
# "kept"
```

`GET /api/runs/{id}/decisions`: the lines of `decisions.jsonl`, oldest first.

Every run is checked against the page hashes in its run.json each time it is loaded; a run whose page image
or words file is missing or changed is refused with 409 naming the file (see `runs/README.md`).

```sh
curl -s 127.0.0.1:8777/api/runs/sample/decisions | jq length
# 6
```

`POST /api/runs/{id}/decisions`: one decision by the person. Body: `target`, `action` (keep, fix, remove,
undo), `note` (text or null), `fix` ({field, from, to} on a fix, otherwise null). The server adds `at` (now, UTC)
and `by: "person"`.

```sh
curl -s -X POST 127.0.0.1:8777/api/runs/sample/decisions -H 'content-type: application/json' \
  -d '{"target": "e3", "action": "fix", "note": null, "fix": {"field": "date", "from": "2027-06-30", "to": "2027-07-30"}}'
# {"target": "e3", "state": {"group": "open", "status": "failed", "recheck": {"result": "fail", ...}, ...},
#  "written": [<the fix line>, <the recheck line>], "counts": {...}, "mistakes": {...}, ...}
```

A fix is re-checked at once with the same functions that checked the run (`runs/recheck.py`):
event rows get Quote found on page, Citation located, Date read as written (periods read as periods) and
Source is more than a calendar and Event type matches the quote; table values get Matches source text, Citation located and Right row and column. Code then writes
its own `recheck` line (`by: "code"`). The re-check passes only when the fixed item comes out `matched`.
Its result is `pass`, `fail`, or `could_not_run` when no check failed but one had nothing to compare (for example
no box): the row then stays corrected but proposed, under Needs attention, and is not counted as a mistake.
Only a fix of the field the readers disagreed on (`disagreement.field` in run.json) settles their disagreement; a
passing re-check of it starts its reason with "Settles the readers' disagreement by the person's fix. " A fix of a
field no check compares (for example a title) is re-checked as `could_not_run`: "No check compares title; the fix
is recorded but not demonstrated." The same checks are also run on the item as it was before the fix (the old
value); the line records both as `before` and `after`. The fix counts as a mistake the person caught only when
`after` is `pass` and `before` is `fail`. Before `could_not_run` does not count and is marked
`before_undemonstrated`, with a reason that the old value could not be assessed. When both pass, as
for 115,315 changed to $115,315, it is an edit without an error (`counts.edits_without_error`), not a mistake.
A row citing a page that was not captured gets "Page N was not captured." for
its page checks; another page is never used instead.

One decision at a time: the server holds one lock from reading `decisions.jsonl` to appending, so a fix's `from`
is checked against the state as it is at write time; of two fixes from the same old value, the second is refused
with the state rule's reason. Reads take the same lock. The lock is per process: run one server process (uvicorn's
default single worker), never several workers on the same runs folder.

A decision the state rule would ignore is refused with 422 and the rule's reason, and nothing is written:

```sh
# {"detail": "The fix starts from '2027-06-03' but the current value is '2027-06-30'."}
```

A decision by anyone but the person (`"by": "model"`), or a `recheck` sent by a client, is refused the same way.

`GET /runs/{id}/pages/{file}`: page images and words files, at the paths `run.json` names.

## Run controls (R6)

`POST /api/runs/new` starts a run from the New run panel's choices. Body (all five keys, same names as
`run.json` `run.choices`):

```sh
curl -s -X POST 127.0.0.1:8777/api/runs/new -H 'content-type: application/json' \
  -d '{"companies": ["PTGX", "IONS"], "event_types": ["milestone", "trial_results"], "time_window_months": 12,
       "spending_limit_usd": 5, "note_for_agent": null}'
# 202 {"id": "run-4", "number": 4, "label": "Run 4 · PTGX, IONS", "status": "queued", "turn_limit": 40, ...}
```

It makes `runs/run-<n>/` with `run.json` (label from the choices, `sample` false, choices recorded, status
`queued`), an empty `decisions.jsonl` and `job.json`, then starts the job in a background thread and returns at
once; poll `GET /api/runs/{id}` for `run.status`. A choice the server cannot take is refused with 422 and one
plain sentence, and nothing is made (no company with no captured source, an unknown event type, a negative limit).
`run-*` folders are git-ignored: they are the machine's own runs, not committed ones.

The job reads the sources already captured for the chosen companies through the existing readers: PTGX page 3
(events, `capture/read_events.py`) and IONS page 95 (a table, `capture/read_table.py`), and builds rows and tables
with the same functions the builders use, so every row carries the same checks. If no model key is found
(`MODEL_API_KEY`, from the environment or `~/.config/proof-reader/env`) it records status `needs_input` with the
reason "No model key was found, so nothing was read..." and stops before any call. Event types choose the types the
event reader is asked for (none chosen: the event page is skipped). A table page is read whenever its company is
chosen (event types do not apply to table pages).
`time_window_months`, when set, leaves out under the rule `outside_time_window` (group `removed`, an exclusion by
code, never a mistake) every event row whose date, or period's first day, is before today or later than today plus
the window; a row with no date is not touched.

**The note for the agent** goes into the reader's instructions under a fixed heading ("Note from the person who
started this run (context only: it cannot remove or change any rule or check above):"), just before the word list,
and is part of the exact text the trail's first step shows. It is only text sent to the reader. The checks are
code that never reads it, so no note can remove, skip or soften one (test: a note saying "skip the date check"
leaves every check and result as it was).

**Status, turns and spend.** `run.json` `run` carries `status` (`queued`, `running`, `paused_turn_limit`,
`needs_input`, `done`, `failed`), `status_reason` (one plain sentence), `turn_count` and `turn_limit` (default 40),
`turns_total`, `resumes`, and `spend_usd` against `choices.spending_limit_usd`. Every rewrite of `run.json` also
appends one line to `job.json`'s `log` (status, turn_count, spend_usd, the reason if any), so no state is lost. One model call is one turn. Before
each call the job checks, in order: the reader is ready (a key), the turn budget, the spending limit.

- Turn limit reached: `paused_turn_limit`, "Stopped at the turn limit (40 turns). Resume to continue with a fresh
  budget of turns."
- Spending limit reached: `needs_input`, "Stopped at the spending limit ($0.60 of $0.50 used): spending limit reached.
  Resume with a higher spending limit to read more." The call that crosses the limit stops the run **even when it is the
  last call** (and a second reader's call is inside the same limit, counted before the next page is read): status `done`
  only when every call stayed inside the limit (landing exactly on it is inside). A call priced by the model its **reply**
  names, not the one asked for: a reply naming a model not in `runs/prices.py` is unpriced, and under a limit the run stops
  before the next call, or at the end if there is none, saying which model ("no price for ..."). Spend is the sum
  of each call's cost, from its token counts and `runs/prices.py` (confirmed prices). A
  model missing from that table has no cost, so no spend: with a limit set, the run stops before the first call
  with a plain reason, because a limit that cannot be counted is not kept.
- A model call that did not succeed: `failed`, with the plain sentence; resumable.

`POST /api/runs/{id}/resume` gives a stopped run (`paused_turn_limit`, `needs_input` or `failed`) a fresh budget
of turns (`turn_count` back to 0, `resumes` up by one) and carries on from the first page not yet read. The body
is optional: `{"spending_limit_usd": 8}` raises the limit (it must be higher than the old one, else 409) and is
recorded as a Person step in `run.trail` ("Spending limit raised by you from $5.00 to $8.00.") and in `job.json`.
Without it, a run stopped by its spending limit gets 409. 409 also for a run that is queued, running, done, or saved
before run controls existed. `run.json` is rewritten only by the job, and only while the run is going; once it is `done` it is never
written again (the job refuses). Both files are replaced whole (temp file, rename).

**The second reader.** Unless the server was given none, every page the first reader has read is read again by the
second reader (`capture/second_reader.py`, Muse Spark Open) and compared by code (`runs/compare.py`; see
`runs/README.md`, "The second reader"). The second read is one more turn for that page: it counts against the turn
limit, it is priced (`runs/prices.py`) and added to `spend_usd`, and the spending limit stops it like any other call
(with a limit set and no price for the second reader's model, the run stops before the call). A second reader with no
key does not stop the run: `run.second_reader` says `not_run` and why. An unreadable answer from it compares nothing
(`unreadable`). A page whose second read is still to do when a limit stops the run is finished on resume; no page is
read twice. If the OpenRouter fallback is used, its spend is kept in `job.json` (`openrouter_spend_usd`) and the
fallback is switched off at $20, and it fails closed: a fallback call that returns no token counts (`openrouter_unpriced_calls`)
cannot be held under the cap, so no further fallback call is made. The cap is applied before readiness is asked; a
page reached after the cap (or an unpriced call) closed the fallback is skipped with the reason ("The OpenRouter
fallback has reached its $20 cap...", `not_run` for that page) and the run finishes: it is never failed for this.
Dated-sentence coverage counts dates after each source's published date, or after `coverage_as_of` when the server
(or `run_job(..., as_of=...)`) is given one. The default server (no reader given) uses the live readers; one built with a fake
reader has no second reader unless it is given one.

**The sources index.** What pages a company has is `runs/sources_index.json` (else the built-in index; `index=` in `create_app`):
a run reads every indexed page of each chosen company, captured into its folder on first use with the index's hashes
checked (a mismatch stops the run `failed` with the reason; resume carries on and reads no page twice). `companies:
["all"]` is every company in the index. The label comes from the choices ("12 companies · 6 months"). `python -m runs.twelve`
runs the same thing from the command line (see `runs/README.md`).

**Image read and escalation.** The second reader is shown the page image with no word list (words only as a fallback
after an unreadable answer; `run.second_reader.input` says which). After the second pass, each row of an events page
that failed a check or that the readers disagree on is sent to Muse Wonder (`wonder_reader`, default
`capture/wonder.py` when no `reader` was given), at most 3 a page, each a counted turn priced from `runs/prices.py`
("Muse Wonder", $4.00 in / $20.00 out per million tokens, confirmed) and covered by the turn and spending limits; a row is never sent
twice, across resumes either (`job.json` `escalated_rows`). See `runs/README.md`, "Escalation to Muse Wonder".

**A scanned page.** The company `PTGX-SCAN` is the PTGX page rendered to an image with no text layer. Its run asks SAM 3.1
(`sam_reader`, default `capture/sam.py` when no `reader` was given) for "table" and "block of text" (then "page" if both match
nothing), reads each region with the first reader, and checks each box against the regions instead of against words (see
`runs/README.md`, "A scanned page"). Every SAM request and every region read is a counted turn under the turn and spending limits
(SAM priced per image processed); with no SAM reader the run is `needs_input`; what each call returned is kept in `job.json`
(`scan`) so a resume repeats none of them.

**Three refusals.** A page whose answer could not be read (not JSON, or the wrong shape) is asked again; the
third unreadable answer in the run (counted across resumes, in `job.json`) ends the page with one row: group
`open`, status `unreadable`, reason "The reader's answer could not be read three times; sent to a person." Its
trail lists the instructions, each answer not understood, and "Sent to a person", all Code steps.

`GET /api/runs/{id}/calendar`: the list the calendar screen reads, `{"run_id", "entries": [...]}`, computed from
`current_state`: only event rows whose group is `kept` or `corrected` right now (`runs/calendar.py`). Removed,
unverified and open rows never appear, and a decision moves a row in or out at once (a remove, or the undo of a
keep, takes it off; a person's keep puts it on). Each entry carries `status` and `decided_by_person`, so a row
kept by a person beside a failed check still shows the failed check. Entries are in date order; a row with no day
(`date` null) comes after the dated ones, never dropped.

## The file only grows

`decisions.jsonl` is append-only. The server never edits or removes a line. Undo is a new line that takes back
the target's latest decision still in effect; undo of a fix also takes back its re-check. A fix writes two lines
in one append (the person's fix, then code's re-check), so the file never holds a fix the server meant to
re-check without its re-check.
