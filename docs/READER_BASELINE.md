# Reader baseline: offline replay

`tools/reader_baseline.py` separates **what a reader actually returned** (its raw reply) from **what downstream
code retained** (rows), using only saved artifacts. It makes no model call, needs no credentials, touches no network
and writes nothing but `--out`.

This is **archived replay**. It is not a test of a revised prompt, not evidence that any new approach works, and it
yields no accuracy metric or semantic clearance. Count differences are structural observations, never "mistakes
caught".

## Command

```
python tools/reader_baseline.py \
  --reader first=fixtures/ptgx_8k_2026-08-05_ex991_p3.events.json \
  --reader fixtures/ptgx_8k_2026-08-05_ex991_p3.events.second.json \
  --out reader_baseline.json
```

(Run from the repo root with the project virtualenv's Python. `-I` is fine; the script locates the repo itself.)

Options:

- `--reader [ROLE=]PATH` (repeatable). ROLE is `first|second|unknown`. Without ROLE the role is `second` only if the
  artifact itself saved `"second_reader": true`, otherwise `unknown`. File names and model ids are not evidence of role.
  `metadata.import.role_source` says which.
- `--downstream KEY=PATH`. A downstream rows snapshot for one reader: a JSON list, or `{"rows": [...]}`. KEY is the
  artifact's file name or its `record_id`. No `--downstream` means `downstream_rows: null` (unavailable); a supplied
  `[]` stays `[]` (an explicit empty snapshot).
- `--links PATH`. Explicit pairs: `[{"record": KEY, "proposal_index": 0, "row_index": 3}, ...]` (or `{"links": [...]}`).
- `--out PATH` (default stdout), `--measure-elapsed` (records this import's own parse time).

Exit 0 on success, 2 on unsupported/unusable input (nothing written).

## Supported input

Event reading records as saved by `capture/read_events.py` and `capture/second_reader.py` (keys `raw_response`,
`prompt_text`, `events`, ...). The raw reply is re-parsed with the real `capture.read_events.parse_events`; proposals
come from that re-parse, and the artifact's saved `events` are only compared (`metadata.saved_parse`).

Refused with a plain message: table readings (`capture/read_table.py`, e.g. `ions_ars_2026_p95.reading.json`), and
any object without the record shape, i.e. with neither a `raw_response` nor an `events` key (region scans, SAM
output, a bare `{"error": ...}`), or with saved `events` that is neither a list nor null. Nothing is guessed for them.

## Output (shared interface v1, unchanged)

`{"schema_version":1,"mode":"offline_replay","records":[...]}`. Per record: `record_id` (`<file name>@<first 12 of the
import sha256>`), `source` (portable path, import sha256, `page` null), `reader_role`, `model`, `prompt`,
`raw_response` (byte-exact), `parse_status`, `parse_error`, `proposals`, `downstream_rows`, `links`, `metadata`.

- `parse_status`: `ok` (including a valid `{"events": []}`), `empty` (blank/absent reply), `malformed` (not the
  event contract; the parser's reason in `parse_error`), `error` (the saved call failed), `unavailable` (no
  `raw_response` saved). Missing raw evidence stays missing: `proposals` is `[]`. Any saved `events` are what
  downstream-side code retained, not what the reader returned, so they appear only under
  `metadata.saved_parse.saved_events` (labelled by `saved_events_are`), never in `proposals`.
- Synthetic says synthetic: an artifact with `"synthetic": true`, or a fake-model reading (`"fake": true`, written by
  `capture/read_events.py`), gets `metadata.synthetic: true` and `metadata.synthetic_markers` naming which.
- Proposals and rows keep every original field and key order, including unknown ones.
- `links` holds only supplied, in-bounds, unambiguous pairs. Malformed, out-of-bounds, conflicting (one proposal to
  several rows, or one row from several proposals), duplicate, or links with no downstream snapshot are listed in
  `metadata.link_issues` and stay unpaired. Nothing is paired by order, ticker, quote or name.
- Provenance: `source.sha256` and `metadata.import.artifact_sha256` are the hash of the file supplied **now**.
  `metadata.provenance.historical_source_sha256` is present only if the artifact recorded `page_sha256`; otherwise
  null with status `missing`. Prompt/page/links are never rebuilt from today's files. A recorded prompt hash is
  checked against the saved prompt text (`match|mismatch|not_checkable`) and a mismatch is reported, not fixed.
- Old metadata (`created_at`, route, usage, ...) is kept verbatim in `metadata.recorded` and
  `recorded_timing_cost`; unknown cost/time are null. `metadata.replay.elapsed_seconds` is this import's time only.
- Paths are portable (absolute paths reduce to the file name). Home-directory-looking paths found inside recorded
  metadata are listed in `metadata.portability_warnings`; raw replies are never altered.

## Later bounded live experiment (not run here)

To compare readers on a fresh page later, use the existing approved production readers (`capture/reader.py`
LiveReader and `capture/second_reader.py`) through their existing run controls and spend gate, with the saved
reading records they already write; import those with this tool. This job adds no client, executor or gate, and
runs no live experiment. A live run with a revised prompt would be a different, separately authorized test.

## Limits

Only the event-reader contract is parsed. The repository's archived readings available here are the PTGX page
first/second readings (one page); no archived downstream snapshot or link map exists, so downstream and link
behaviour is covered by synthetic tests only (marked synthetic).
