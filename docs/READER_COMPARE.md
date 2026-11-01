# Reader compare (offline replay)

Offline structural comparison of what a reader returned (`proposals`) versus what downstream code retained (`downstream_rows`), using shared interface v1. This is an archived-replay diagnostic — **not** a live reader test, **not** a judgment of content (no accuracy metric, no pass/fail, no score), and **not** evidence that a revised prompt or pivot works.

Owned deliverable for order `READER-BASELINE-001`: `tools/reader_compare.py`, `tests/test_reader_compare.py`, this doc. The tool reads the offline_replay JSON that `tools/reader_baseline.py` writes; it does not import it.

## Purpose

- Load a shared `offline_replay` JSON document.
- Compare only **explicit** `{proposal_index, row_index}` links.
- Show proposal and linked downstream row side by side.
- Report structural field observations: dropped / changed / unchanged / row_only.
- Leave unmatched proposals and rows unpaired when links are missing, malformed, out of bounds, or conflicting — never guess by array order, ticker, quote, or name.
- Keep `human_judgment` and `human_notes` blank for a person to fill.
- Preserve `raw_response` exactly (not prettified). Unknown recorded cost/time stay null; replay wall time is separate.

## Shared interface v1

```json
{
  "schema_version": 1,
  "mode": "offline_replay",
  "records": [
    {
      "record_id": "...",
      "source": {"artifact_path": null, "sha256": null, "page": null},
      "reader_role": "first|second|unknown",
      "model": null,
      "prompt": null,
      "raw_response": null,
      "parse_status": "ok|empty|malformed|error|unavailable",
      "parse_error": null,
      "proposals": [],
      "downstream_rows": null,
      "links": [],
      "metadata": {}
    }
  ]
}
```

Notes:

- `proposals` / `downstream_rows` preserve original objects, including unknown fields.
- `downstream_rows: null` = snapshot unavailable; `[]` = supplied empty snapshot (kept distinct).
- `links` are only explicit `{proposal_index: int, row_index: int}` pairs.
- Survival fields where supplied (structural, not semantic): `exact_text`, `conditions`, `relevance`, `subject_company`, `relation`, `type_hint`.

## CLI

```text
PYTHONPATH=. python tools/reader_compare.py path/to/records.json \
  --out compare.md [--format md|json]
```

Without `--out` the report goes to stdout. Nothing is written except `--out`. Exit codes: `0` if the envelope is ok (`schema_version`/`mode`/`records` valid); `2` otherwise (including unreadable input).

Only records with `parse_status: ok` are compared. For `unavailable`, `empty`, `malformed` and `error` records, `comparison_status` is `not_compared_<status>`: no proposal is paired, supplied proposals/links are reported as `not_compared`, and downstream rows stay unmatched. Recorded cost/time are read from `metadata.recorded_timing_cost` (null when unknown).

Library entry points: `load_input`, `compare_document`, `compare_record`, `classify_links`, `compare_objects`, `render_markdown`.

## Exact offline command (tests)

From the repo root:

```text
PYTHONPATH=. python -m pytest tests/test_reader_compare.py -q
```


## Limitations

- Archived offline replay only — not evidence a revised prompt or pivot works.
- No accuracy metric, pass/fail, score or similarity matching; field diffs are structural observations only.
- Links must be explicit; no pairing by order, ticker, quote, or name.
- Missing prompt, raw_response, hashes, or links stay explicitly missing; nothing is reconstructed.
- `downstream_rows` null vs `[]` stay distinct.
- `malformed` / `empty` / `error` / `unavailable` parse statuses stay separate from `ok`.
- Human judgment columns are blank; this tool states no verdict on any proposal or row.
- Unknown recorded cost/time stay null; `replay.elapsed_ms` is compare wall time only.
- Synthetic tests only (`metadata.synthetic=true`); no archived downstream snapshot or link map was used.

## Not pivot evidence

A clean CLI exit or a green synthetic test suite only means the compare envelope and structural reporting behaved as coded. It does **not** mean the product pivot, a new prompt, or live reader quality improved.
