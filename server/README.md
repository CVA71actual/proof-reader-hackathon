# server: the local decisions server

A small FastAPI app the screens call. It serves runs, takes a person's decisions, re-checks a person's fix
with the real check functions, returns one state per row, and starts and resumes runs (R6, below). Only a run's
job calls a model, through one reader (`capture/reader.py`); the decisions, listing and page code never touches a
model, the env file or `MODEL_API_KEY`.

## Run

From the repo root, on this machine only (uvicorn binds to 127.0.0.1 unless told otherwise; do not pass `--host`):

```sh
.venv/bin/python -m uvicorn server.app:app --port 8777
```

Then open http://127.0.0.1:8777/ (the `ui/` folder).

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
undo, same_event, distinct_events — see "Two copies of one event" below), `note` (text or null), `fix` ({field, from, to} on a fix, otherwise null). The server adds `at` (now, UTC)
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

### Two copies of one event (DUPLICATE-RESOLUTION-001)

The rule (full text in `runs/state.py`, above `duplicate_pair`): two event rows are possibly one event only when they
have the same company and the same exact day, no distinctness guard fires (different regulators, regions, trial
phases, indications, or programs with no shared name), and either the titles are equal after normalization, or both
name the same meeting (same kind: conference/symposium, earnings, investor day, shareholder meeting, and the same
organiser/meeting name once generic words are dropped), or both have the same program and the same action
(approval, launch, readout, ...). Same company and day alone never pair ("Wolfe conference" and "UBS conference" on
one day stay two events). In each group the copy on the calendar (the primary) is the earliest current Keep, else
the first row placed automatically, else the first row.

Every event row in `state.targets` (and every `/calendar` entry) carries:

```
"duplicate_of":      null | {"row_id": "e10", "title": "...", "date": "2026-11-12", "accepted": true}
                     # accepted: that copy is on the calendar (kept by you or placed automatically)
"duplicate_state":   null | "unresolved" | "both_kept" | "same_event" | "distinct_events"
"duplicate_actions": [] | ["same_event", "distinct_events"] | ["undo"]   # what the server takes for it now
"keep_refused":      null | "<the 409 sentence below>"                   # a plain Keep would be refused
"supporting_sources": [{"row_id", "source_id", "page_number", "title", "date", "date_text", "quote", "note", "at"}]
                     # on the primary: copies a person marked as the same event (state only; also on /calendar)
"possible_duplicates": ["e28"]                                          # on the primary (state only)
```

`both_kept`: Keeps on both copies already exist (run 9 e10/e28). Nothing is rewritten; the later Keep is held, the
row is in Needs review with the issue "Both copies were kept (this one and e10); resolve: same event or different
events", its held Keep is listed in `review_history`, and the calendar shows only the primary.

A plain Keep while `keep_refused` is set is refused, under the decisions lock (so of two concurrent Keeps on two
copies the second sees the first), nothing written:

```sh
curl -s -X POST .../api/runs/run-9/decisions -d '{"target": "e28", "action": "keep"}'
# 409 {"detail": "Same event as e10, already on the calendar. Choose: it is the same event (add this as a second
#      source of e10) or they are different events.", "reason": "<the same sentence>",
#      "code": "duplicate_unresolved", "target": "e28",
#      "duplicate_of": {"row_id": "e10", "title": "...", "date": "2026-11-12", "accepted": true},
#      "actions": ["same_event", "distinct_events"]}
```

The two resolution actions (body as any decision; `duplicate_of` optional — when sent it must still be the row's
other copy, else 409 and nothing written; `expected_basis` works as for every decision):

```sh
# the same event: e28 becomes a supporting source of e10, not a second calendar entry; its evidence stays on e28
-d '{"target": "e28", "action": "same_event", "duplicate_of": "e10", "note": null}'
# 200 {..., "state": {"duplicate_state": "same_event", "inclusion": "excluded", "semantic_status": "same_event",
#      "on_calendar": false, "review_label": "Same event as e10 — kept as a second source of e10", ...}}
#   refused 409 when e10 is not on the calendar ("Keep e10 first, or keep this row instead.")

# different events: a note is required (422 "Say in a note why these are different events." without one);
# then a plain Keep of e28 is taken and both are on the calendar
-d '{"target": "e28", "action": "distinct_events", "duplicate_of": "e10", "note": "Separate fireside chat"}'
```

The line written records `"duplicate_of": "<row id>"`. Both are undone with `undo` like any decision.
`distinct_events` is an assertion, not acceptance: the row still needs a Keep.

`GET /runs/{id}/pages/{file}`: page images and words files, at the paths `run.json` names.

## Run controls (R6)

`GET /api/companies`: the selectable companies of the sources index a new run will read —
`{"companies": [...], "listed_without_pages": [...]}`. `companies` is exactly what `"companies": ["all"]`
resolves to (one rule, `jobs.selectable_companies`: every company with a page to read; a scan-only
company is not one). `listed_without_pages` names the companies the index lists with no page (never one
that has pages); the dialog shows them as plain names with "No pages read yet", not selectable. A broken
index answers 409 with its plain sentence in `detail`, which the dialog shows in its red line. The New
Run dialog renders this list; no company name is hard-coded in the UI.

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
code, never a mistake) every event row whose period ends before today or starts after today plus the window (an exact
day is both; `python -m runs.rewindow` lifts exclusions the rule no longer makes); a row with no date is not touched.

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

**Source set frozen (Job 51f).** On its first start a run records in `job.json` `source_freeze` the SHA-256 of the
sources index file and of every chosen page's page and words files. The index is the run's own (Job 51g):
`source_freeze.index` records its absolute path and file name with the hash, and a resume (and the job) compares with
that file, never the server's default index, so a run read from `sources_index.fourteen-1013.json` resumes on a server
whose default is another index. If that file is no longer on disk the resume is refused, 409, saying so. A resume whose
index or any file differs is refused, 409, "source set changed; historical replay would not be the same run"; nothing
is re-fetched. A run started before this has no record and is refused until the Operator, after checking the index
hash by hand, runs `python -m server.run_steps record_source_freeze <id> --index FILE --index-sha256 HEX` (or `POST
/api/runs/{id}/source-freeze` `{"index_sha256": HEX}`, which uses the server's index), which records FILE's path and
the current files only if they match.

**An unknown cost needs a person (Job 51g).** Whenever a run holds calls that could not be priced which no person has
accepted (`unpriced_calls` > `unknown_cost_accepted`), a resume is refused, 422 naming `accept_unknown_cost`, until it
carries `"accept_unknown_cost": true`, with or without a spending limit (with one, a higher `spending_limit_usd` too).
The acceptance is a Person step "Unknown cost accepted".

**A stop from outside (Job 51f).** A worker stopped by a signal leaves `run.json` "running". The Operator writes
`runs/<id>/OPERATOR_STOP.json` `{"stopped_at": "2026-10-31T21:31:44Z", "signal": "SIGTERM",
"potentially_billed_calls": 1, "note": null}` (the last two optional; no other field) and runs `python -m
server.run_steps record_operator_stop <id>` (or `POST /api/runs/{id}/operator-stop`, the same function). It refuses a
run that is not "running", is driven by this server, or was written after `stopped_at`, and is never applied twice.
It adds the calls to `unpriced_calls`, writes a Person step "Operator stop recorded", and sets `needs_input`,
"stopped by the Operator; N potentially billed call(s), cost unknown". A resume then needs `accept_unknown_cost: true`
from a person (and a higher limit, if the run has one). It writes `job.json` and then `run.json`; if it is stopped
between the two, running it again finishes `run.json` without counting the calls twice (Job 51g). The command line
for both steps is `server/run_steps.py`; `runs/twelve.py` always creates a new run, and `run_steps` never resumes a run
or accepts an unknown cost.

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

## Research runs (RESEARCHER-FULL-001 S1)

`POST /api/runs/new` with `"mode": "research"` starts a research run (see `runs/README.md`, "Research runs"): up to 10
tickers, `spending_limit_usd` (default $30, the only limit on), optional `research_limits` (all off by default) and the
editable `note_for_agent`. `create_app(research_tools=..., research_reader=...)` injects the fetcher/renderer and the
Responses reader (tests pass fakes; the default is live). `POST /api/runs/{id}/research-stop` asks every company to stop
before its next model call; nothing is published. `POST /api/runs/{id}/research-finish` (body optional,
`{"note": text}`; 202 with `{requested_at, by, note, meaning}`) is Finish now (STOP-RULE-001): every company ends
its research before its next model call as finished, then the review, adjudication and support check run on what
was found and the run publishes; research-stop wins if both are asked. Both answer 409 unless the run is a queued or
running research run. `POST /api/runs/{id}/resume` refuses a research run in S1.
