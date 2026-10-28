# server: the local decisions server

A small FastAPI app the screens call. It serves runs, takes a person's decisions, re-checks a person's fix
with the real check functions, and returns one state per row. It never calls a model, never loads the env
file and never reads `MODEL_API_KEY`.

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
`current_state(run, decisions)` (targets, counts, mistakes, excluded, exclusions, unresolved, ignored).

```sh
curl -s 127.0.0.1:8777/api/runs/sample | jq '.state.targets.e7.group'
# "kept"
```

`GET /api/runs/{id}/decisions`: the lines of `decisions.jsonl`, oldest first.

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
Source is more than a calendar; table values get Matches source text, Citation located and Right row and column. Code then writes
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

## The file only grows

`decisions.jsonl` is append-only. The server never edits or removes a line. Undo is a new line that takes back
the target's latest decision still in effect; undo of a fix also takes back its re-check. A fix writes two lines
in one append (the person's fix, then code's re-check), so the file never holds a fix the server meant to
re-check without its re-check.
