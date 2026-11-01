# PIVOT-001 expected-case fixtures

Expected cases for the PIVOT-001 pivot examples (orders/DEVIN_JOB_3_PIVOT_FIXTURES.md;
repairs per reviews/MANUS_OBSERVER_RECONCILIATION_2026-11-01_1051PT.md and the PR #143 review).
Convention — one and only one: every `evidence_spans[]` gives `kind`, `quote` and `word_range`
(zero-based inclusive indexes into the page's `words[].text`) such that the words at that
range, joined with single spaces, equal `quote` exactly; synthetic-labelled cases have no
words file and `word_range` is null with `quote` verbatim inside `passage`.
`synthetic_sample_source` flags the runs/sample entries honestly. `expected_proposals` is
always a list (empty when no event is expected); each proposal carries its own
`expected_review_outcome`. `subject` splits `program` from `company`, with `company_origin`
marking whether the company is printed or comes from the run assignment. `conditions_stated`
holds printed words only; interpretations live in `conditions_proposed_inferred`.
Consumer: tests/test_pivot_fixtures.py asserts the schema and every span verbatim-at-range.
Each case's `passage_range` is the word range of `passage` itself; every `evidence_spans[].word_range`
must lie inside it — a span pointing at identical words elsewhere on the page fails the consumer.
`conditions_stated` must appear verbatim inside the passage text, not merely on the page.
Each proposal also declares `item_range` — the word range of its own item inside the passage — and carries
its `evidence_spans` and `conditions_stated` within it, so a span or condition moved between items fails.

Expected printed timing and a named program must also occur in that proposal's item;
valid spans cannot excuse an expected answer that names the other item's date/program.
Semantic outcomes are proposed/needs attention, never matched (CONTRACT-001 section 2b).
Inferred-condition glosses are optional interpretations, not required model answers or
evidence of support. These seven cases are shared development controls, not a complete
acceptance suite or a held-out accuracy set. Case 01 is not an earnings-positive control.
