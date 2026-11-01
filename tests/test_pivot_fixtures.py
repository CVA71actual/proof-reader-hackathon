"""PIVOT-001 fixture consumer: uniform schema + every span verbatim and inside its own item range."""
import json
from pathlib import Path

import pytest

CASES = sorted(Path(__file__).parent.glob("fixtures/pivot_cases/*.json"))
REPO = Path(__file__).parent.parent

MANDATORY_CASE_KEYS = {"id", "pivot_example", "source", "passage", "passage_range",
                       "expected_proposals", "unsupported_pairings"}
CASE_KEYS = MANDATORY_CASE_KEYS | {"evidence_spans", "expected_review_outcome", "review_reason", "note"}
SOURCE_KEYS = {"run", "source_id", "synthetic_sample_source", "words_file", "page_number"}
PROPOSAL_KEYS = {"action", "type_words", "subject", "timing_as_printed", "conditions_stated",
                 "conditions_proposed_inferred", "expected_review_outcome", "review_reason",
                 "item_range", "evidence_spans"}
SUBJECT_KEYS = {"program", "company", "company_origin"}
SPAN_KEYS = {"kind", "quote", "word_range"}


def _page_text(case):
    d = json.loads((REPO / case["source"]["run"] / case["source"]["words_file"]).read_text())
    return [w["text"] for w in d["words"]]


@pytest.mark.parametrize("case_file", CASES, ids=[p.name for p in CASES])
def test_fixture_schema_is_uniform(case_file):
    case = json.loads(case_file.read_text())
    assert set(case) <= CASE_KEYS and MANDATORY_CASE_KEYS <= set(case), f"{case_file.name}: key drift"
    assert set(case["source"]) == SOURCE_KEYS
    for prop in case["expected_proposals"]:
        assert set(prop) == PROPOSAL_KEYS, f"{case_file.name}: proposal key drift"
        assert set(prop["subject"]) == SUBJECT_KEYS
        assert prop["expected_review_outcome"] in {"proposed", "needs attention"}
        assert prop["review_reason"]
        assert isinstance(prop["evidence_spans"], list)
        for span in prop["evidence_spans"]:
            assert set(span) == SPAN_KEYS
    if not case["expected_proposals"]:          # no-proposal cases carry the outcome at case level
        assert case.get("expected_review_outcome") == "no proposal" and case.get("review_reason")
    for span in case.get("evidence_spans", []):
        assert set(span) == SPAN_KEYS


@pytest.mark.parametrize("case_file", CASES, ids=[p.name for p in CASES])
def test_spans_are_verbatim_and_inside_their_own_item(case_file):
    case = json.loads(case_file.read_text())
    if case["source"]["words_file"]:
        toks = _page_text(case)
        a0, a1 = case["passage_range"]
        assert " ".join(toks[a0:a1 + 1]) == case["passage"], "passage_range is not the passage"
        for span in case.get("evidence_spans", []):
            b0, b1 = span["word_range"]
            assert a0 <= b0 and b1 <= a1 and " ".join(toks[b0:b1 + 1]) == span["quote"]
        for prop in case["expected_proposals"]:
            c0, c1 = prop["item_range"]
            assert a0 <= c0 and c1 <= a1, f"{case_file.name}: item_range escapes the passage"
            item_text = " ".join(toks[c0:c1 + 1])
            _expected_identifiers_are_printed(prop, item_text)
            for span in prop["evidence_spans"]:
                b0, b1 = span["word_range"]
                assert c0 <= b0 and b1 <= c1, \
                    f"{case_file.name}: span {span['kind']} escapes its proposal's item range"
                assert " ".join(toks[b0:b1 + 1]) == span["quote"], \
                    f"{case_file.name}: span {span['word_range']} does not print {span['quote'][:60]!r}"
            for cond in prop["conditions_stated"]:
                assert cond in item_text, \
                    f"{case_file.name}: stated condition not in the proposal's own item: {cond!r}"
    else:                                        # synthetic-labelled case: everything lives in the passage
        assert case["passage_range"] is None
        for prop in case["expected_proposals"]:
            assert prop["item_range"] is None
            _expected_identifiers_are_printed(prop, case["passage"])
            for span in prop["evidence_spans"]:
                assert span["word_range"] is None and span["quote"] in case["passage"]
            for cond in prop["conditions_stated"]:
                assert cond in case["passage"]


def _expected_identifiers_are_printed(prop, item_text):
    # Fixture integrity, not a production entailment check: expected printed
    # timing and program must refer to this item's words, not its neighbour.
    assert prop["timing_as_printed"] and prop["timing_as_printed"] in item_text
    program = prop["subject"]["program"]
    assert program is None or program in item_text


@pytest.mark.parametrize("field,value", [
    ("timing_as_printed", "Q1 2027"),
    ("expected_review_outcome", "matched"),
    ("program", "PN-8047"),
])
def test_cross_item_expected_answers_and_false_clearance_are_rejected(tmp_path, field, value):
    case = json.loads((Path(__file__).parent / "fixtures/pivot_cases/06_cross_program_timing_isolation.json").read_text())
    prop = case["expected_proposals"][0]
    if field == "program":
        prop["subject"][field] = value
    else:
        prop[field] = value
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(case))
    with pytest.raises(AssertionError):
        test_fixture_schema_is_uniform(changed)
        test_spans_are_verbatim_and_inside_their_own_item(changed)
