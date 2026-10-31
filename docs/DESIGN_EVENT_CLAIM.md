# Event claim check — design proposal

## Promise and verdict contract

A green row must establish one proposition: **this program and indication have this action, with this modality, at this time, on the visible evidence, as of the evidence available.** All six claims below must support the **same event**; six facts borrowed from different events cannot combine into green.

Each claim records `pass`, `fail`, `could_not_run`, or `not_checked`, bound to exact candidate fields, source/page, consumed page hash, checker version, and supporting evidence span/geometry. Applicability is explicit: an inapplicable claim says why and cannot silently pass. Input, artifact/hash, or checker-version changes invalidate the old verdict; a new assessment is appended. Aggregation is fixed: any fail means fail; any missing, stale, or unrun required claim means unresolved, never green. A human Fix changes the candidate and requires reassessment; Keep records acceptance beside the checks. Neither rewrites historical evidence.

| Claim | Evidence required | If unavailable |
|---|---|---|
| Visible passage | The claimed words are legible in rendered page ink at the cited region. Text-layer match is a separate labelled result. | Visible words not checked |
| Subject | Program and indication match this proposition; aliases are explicit. | Subject unresolved |
| Action/modality | This event's action and planned/expected/conditional/completed/withdrawn form are supported independently of the assigning classifier. | Action unresolved |
| Timing | Date/period attaches to this action clause; bullets, sentence stops, lists and “respectively” are resolved. | Timing unresolved |
| Relative anchor | The statement's own document/release date or another evidenced anchor resolves its relative words. | Anchor unresolved |
| Later evidence | Chronology identifies a change, delay, completion, replacement, cancellation, or withdrawal of this same event. | Currentness not checked |

**No shortcuts:** ink in a box does not prove the claimed words are visible; a filing date is not the statement's anchor by default; words in one clause are not necessarily one event; a newer document naming the same program/action does not itself prove supersession.

## Shared state and Tuesday slices

`runs/state.py` owns effective checks, history, stale/unknown state, grouping, calendar placement, and attention. API, UI, calendar, PDF, and Word consume it; none reconstructs grouping or placement. `checks/` owns bounded claim checks; `runs/build_entry.py` preserves proposals and attaches only supported checks; `runs/compare.py` compares established same-event identities (agreement is not evidence); `runs/coverage.py` surfaces possible omissions, not completeness.

1. **Slice 1 — shared state, provenance, migration, page-read outcomes (R118).** Bind each check to inputs/hash/version; record explicit company and per-page read outcomes. Visible check reads words against rasterized ink at their boxes; text-layer match stays separately labelled. Failure tests: invisible text says “100” while the rendered page says “900” → not pass; missing/changed page → unresolved; captured but unreadable page → not read. A visible matching passage is the positive case. Old green rows become not checked; reassessment appends results from stored artifacts only, with no model calls.
2. **Slice 2 — subject/action/date relationships.** Bind program, indication, modality, clause, date and anchor to one proposition. Failure tests: Drug A's result row cannot borrow Drug B's Q1 date; cancelled/negated ranges fail; “respectively” attaches each date correctly; relative wording without an evidenced statement anchor stays unresolved. Same-event, same-clause, anchored wording is the positive case. Out of reach by Tuesday: cross-document event identity and complete withdrawal discovery.
3. **Slice 3 — later evidence within the run.** Compare only an established same-event identity and show chronology. A newer same-program/action document alone does not supersede; explicit replacement/withdrawal is the failure-path test, and unrelated events remain separate. Complete external/currentness search is out of reach by Tuesday.

## Saved-run transition and wording

When this lands, preserve original rows, checks, page/source hashes, decisions, and timestamps; mark old verdicts not checked; append offline reassessments without restoring old greens; invalidate saved exports and rebuild from current state. Kept rows and unresolved proposals remain accessible with their decisions. Stored words/boxes may support text-layer or geometry reassessment, and stored images may support the visible check; absent/changed artifacts, unsupported anchors, and claims requiring undiscovered later sources stay unresolved.

For timing labels, use the three states Corey decided: **Undated · no timing stated** (no date wording), **Timing unresolved** (wording exists but cannot be resolved), and **Contingent** only when an explicit dependency is supported; an event may be both dated and contingent. Absence of timing does not assert a condition.

This page is design only. Corey has decided the timing wording above. Design acceptance waits for the code adviser’s answer; the Slice 1 order follows that answer. No new build work is authorized while those steps are pending.
