"""Synthetic adapter tests for tools/reader_compare.py (READER-BASELINE-001).

All records are labeled metadata.synthetic=true. Designated read-only sources
(runs/ and proof-reader-docs) lack original raw reader replies for this adapter;
these fixtures invent nothing as saved calls.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_compare():
    try:
        from tools import reader_compare as mod  # type: ignore

        return mod
    except Exception:
        path = REPO / "tools" / "reader_compare.py"
        spec = importlib.util.spec_from_file_location("reader_compare", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


rc = _load_compare()


def _base_record(**overrides):
    rec = {
        "record_id": "synthetic-default",
        "source": {"artifact_path": None, "sha256": None, "page": None},
        "reader_role": "first",
        "model": None,
        "prompt": None,
        "raw_response": None,
        "parse_status": "ok",
        "parse_error": None,
        "proposals": [],
        "downstream_rows": None,
        "links": [],
        "metadata": {"synthetic": True},
    }
    rec.update(overrides)
    meta = dict(rec.get("metadata") or {})
    meta["synthetic"] = True
    rec["metadata"] = meta
    return rec


def _envelope(records):
    return {"schema_version": 1, "mode": "offline_replay", "records": records}


def _obs_by_field(pair):
    return {o["field"]: o for o in pair["field_observations"]}


def test_dropped_conditions():
    """Proposal has conditions; linked downstream row drops the field."""
    prop = {
        "exact_text": "Phase 3 readout expected mid-2026, subject to enrollment",
        "conditions": ["subject to enrollment"],
        "relevance": "catalyst",
        "subject_company": "ACME",
        "relation": "self",
        "type_hint": "clinical_readout",
    }
    row = {
        "exact_text": "Phase 3 readout expected mid-2026, subject to enrollment",
        "relevance": "catalyst",
        "subject_company": "ACME",
        "relation": "self",
        "type_hint": "clinical_readout",
    }
    doc = _envelope([
        _base_record(
            record_id="synthetic-dropped-conditions",
            proposals=[prop],
            downstream_rows=[row],
            links=[{"proposal_index": 0, "row_index": 0}],
        )
    ])
    report = rc.compare_document(doc)
    assert report["ok"] is True
    rec = report["records"][0]
    assert rec["metadata"]["synthetic"] is True
    assert len(rec["pairs"]) == 1
    obs = _obs_by_field(rec["pairs"][0])
    assert obs["conditions"]["observation"] == "dropped"
    assert obs["conditions"]["survival_field"] is True
    assert obs["conditions"]["proposal_value"] == ["subject to enrollment"]
    assert obs["conditions"]["in_row"] is False
    md = rc.render_markdown(report)
    assert "dropped" in md
    assert "Limitations" in md


def test_type_overwrite():
    """type_hint changes between proposal and linked row (structural changed)."""
    prop = {
        "exact_text": "NDA filing planned for 2H26",
        "conditions": [],
        "relevance": "regulatory",
        "subject_company": "BETA",
        "relation": "self",
        "type_hint": "nda_filing",
    }
    row = {
        **prop,
        "type_hint": "pdufa",
    }
    doc = _envelope([
        _base_record(
            record_id="synthetic-type-overwrite",
            proposals=[prop],
            downstream_rows=[row],
            links=[{"proposal_index": 0, "row_index": 0}],
        )
    ])
    report = rc.compare_document(doc)
    rec = report["records"][0]
    obs = _obs_by_field(rec["pairs"][0])
    assert obs["type_hint"]["observation"] == "changed"
    assert obs["type_hint"]["proposal_value"] == "nda_filing"
    assert obs["type_hint"]["row_value"] == "pdufa"
    assert obs["type_hint"]["survival_field"] is True
    assert rec["counts"]["changed_field_observations"] >= 1


def test_unchanged_easy_earnings():
    """Easy earnings-style survival fields unchanged across the link."""
    prop = {
        "exact_text": "Company expects to report Q4 results in February",
        "conditions": None,
        "relevance": "earnings",
        "subject_company": "GAMMA Inc",
        "relation": "self",
        "type_hint": "earnings",
        "extra_unknown": "keep-me",
    }
    row = dict(prop)
    doc = _envelope([
        _base_record(
            record_id="synthetic-unchanged-easy-earnings",
            proposals=[prop],
            downstream_rows=[row],
            links=[{"proposal_index": 0, "row_index": 0}],
        )
    ])
    report = rc.compare_document(doc)
    rec = report["records"][0]
    pair = rec["pairs"][0]
    obs = _obs_by_field(pair)
    for field in rc.SURVIVAL_FIELDS:
        assert obs[field]["observation"] == "unchanged"
        assert obs[field]["survival_field"] is True
    assert obs["extra_unknown"]["observation"] == "unchanged"
    assert pair["human_judgment"] == ""
    assert pair["human_notes"] == ""
    assert rec["counts"]["unchanged_survival_field_observations"] == len(rc.SURVIVAL_FIELDS)


def test_unpaired_reordered_programs():
    """Reordered programs without explicit links stay unmatched — never pair by order."""
    proposals = [
        {"exact_text": "Program Alpha Phase 2", "subject_company": "DELTA", "type_hint": "clinical"},
        {"exact_text": "Program Beta Phase 3", "subject_company": "DELTA", "type_hint": "clinical"},
    ]
    # Same objects reversed — would match if anyone inferred by order/name.
    rows = [proposals[1], proposals[0]]
    doc = _envelope([
        _base_record(
            record_id="synthetic-unpaired-reordered-programs",
            proposals=proposals,
            downstream_rows=rows,
            links=[],  # explicit: no links
        )
    ])
    report = rc.compare_document(doc)
    rec = report["records"][0]
    assert rec["pairs"] == []
    assert len(rec["unmatched_proposals"]) == 2
    assert len(rec["unmatched_rows"]) == 2
    assert rec["counts"]["linked_pairs"] == 0
    for u in rec["unmatched_proposals"]:
        assert u["reason"] == "no_valid_explicit_link"
        assert u["human_judgment"] == ""
        assert u["human_notes"] == ""


def test_invalid_links():
    """Malformed, out-of-bounds, and conflicting links are reported; nothing guessed."""
    proposals = [
        {"exact_text": "A", "type_hint": "x"},
        {"exact_text": "B", "type_hint": "y"},
    ]
    rows = [
        {"exact_text": "A", "type_hint": "x"},
        {"exact_text": "B", "type_hint": "y"},
    ]
    links = [
        "not-an-object",
        {"proposal_index": "0", "row_index": 0},
        {"proposal_index": 99, "row_index": 0},
        {"proposal_index": 0, "row_index": 99},
        {"proposal_index": 0, "row_index": 0},
        {"proposal_index": 0, "row_index": 1},  # conflicts with previous on proposal 0
        {"proposal_index": 1, "row_index": 0},  # also conflicts on row 0 with accepted candidate
    ]
    doc = _envelope([
        _base_record(
            record_id="synthetic-invalid-links",
            proposals=proposals,
            downstream_rows=rows,
            links=links,
        )
    ])
    report = rc.compare_document(doc)
    rec = report["records"][0]
    kinds = {i["kind"] for i in rec["link_issues"]}
    assert "malformed_link" in kinds
    assert "out_of_bounds" in kinds
    assert "conflicting_link" in kinds
    # Conflicting candidates must not be applied as pairs; leave unpaired rather than guess.
    assert rec["pairs"] == []
    assert len(rec["unmatched_proposals"]) == 2
    assert len(rec["unmatched_rows"]) == 2


def test_missing_snapshot_vs_empty_snapshot():
    """null = unavailable; [] = empty snapshot — keep distinct."""
    prop = {"exact_text": "orphan proposal", "type_hint": "misc"}

    missing = rc.compare_record(
        _base_record(
            record_id="synthetic-missing-snapshot",
            proposals=[prop],
            downstream_rows=None,
            links=[{"proposal_index": 0, "row_index": 0}],
        )
    )
    empty = rc.compare_record(
        _base_record(
            record_id="synthetic-empty-snapshot",
            proposals=[prop],
            downstream_rows=[],
            links=[{"proposal_index": 0, "row_index": 0}],
        )
    )

    assert missing["downstream_rows_status"] == "unavailable"
    assert empty["downstream_rows_status"] == "empty"
    assert missing["counts"]["downstream_rows"] is None
    assert empty["counts"]["downstream_rows"] == 0
    assert missing["unmatched_rows"] == []
    assert empty["unmatched_rows"] == []
    assert missing["pairs"] == []
    assert empty["pairs"] == []
    assert any(i["kind"] == "row_snapshot_unavailable" for i in missing["link_issues"])
    assert any(i["kind"] == "out_of_bounds" for i in empty["link_issues"])
    assert len(missing["unmatched_proposals"]) == 1
    assert len(empty["unmatched_proposals"]) == 1


def test_raw_parse_failure():
    """malformed/error parse_status preserves raw_response exactly; not prettified."""
    messy = '{"events":[{"exact_text":"broken"\n  ,,, not json'
    doc = _envelope([
        _base_record(
            record_id="synthetic-raw-parse-failure",
            parse_status="malformed",
            parse_error="trailing commas / incomplete object",
            raw_response=messy,
            proposals=[],
            downstream_rows=[],
            links=[],
            metadata={"synthetic": True, "recorded_timing_cost": {"elapsed_seconds": None, "cost_usd": None}},
        )
    ])
    report = rc.compare_document(doc, replay_elapsed_ms=12)
    rec = report["records"][0]
    assert rec["parse_status"] == "malformed"
    assert rec["raw_response"] == messy
    assert rec["raw_response"] != json.dumps(json.loads('{"ok":true}'))  # sanity: exact bytes/string
    assert rec["recorded_metadata"]["cost_usd"] is None
    assert rec["recorded_metadata"]["elapsed_seconds"] is None
    assert rec["comparison_status"] == "not_compared_malformed"
    assert report["replay"]["elapsed_ms"] == 12
    md = rc.render_markdown(report)
    assert messy in md
    assert "human_judgment" in md

    # error status likewise preserves raw
    err_rec = rc.compare_record(
        _base_record(
            record_id="synthetic-raw-parse-error",
            parse_status="error",
            parse_error="timeout",
            raw_response=messy,
        )
    )
    assert err_rec["parse_status"] == "error"
    assert err_rec["raw_response"] == messy


def test_envelope_exit_semantics(tmp_path):
    """CLI returns 0 when envelope ok, 2 when not."""
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    good.write_text(json.dumps(_envelope([])), encoding="utf-8")
    bad.write_text(json.dumps({"schema_version": 99, "mode": "nope", "records": []}), encoding="utf-8")
    assert rc.main([str(good), "--out", str(tmp_path / "g.md")]) == 0
    assert rc.main([str(bad), "--out", str(tmp_path / "b.md")]) == 2
    assert rc.main([str(tmp_path / "missing.json")]) == 2


def test_classify_links_unit():
    pairs, issues = rc.classify_links(
        [{"proposal_index": 0, "row_index": 0}],
        n_proposals=1,
        rows=[{"x": 1}],
    )
    assert pairs == [{"proposal_index": 0, "row_index": 0}]
    assert issues == []


def _unavailable_with_stuff():
    return _base_record(
        record_id="synthetic-unavailable",
        parse_status="unavailable",
        parse_error="no raw_response saved",
        raw_response=None,
        # hand-built inconsistent input: proposals/links present although the raw reply is missing
        proposals=[{"exact_text": "x"}],
        downstream_rows=[{"exact_text": "x"}],
        links=[{"proposal_index": 0, "row_index": 0}],
        metadata={"synthetic": True, "saved_parse": {"saved_events": [{"exact_text": "x"}]}},
    )


def test_missing_raw_is_unavailable_never_compared():
    rec = rc.compare_record(_unavailable_with_stuff())
    assert rec["parse_status"] == "unavailable"
    assert rec["comparison_status"] == "not_compared_unavailable"
    assert rec["raw_response"] is None
    assert rec["pairs"] == []
    assert rec["unmatched_proposals"] == []
    assert rec["counts"]["proposals"] == 0
    assert [i["kind"] for i in rec["link_issues"]] == ["not_compared"]
    # downstream rows stay visible as unmatched, never paired to anything
    assert len(rec["unmatched_rows"]) == 1
    md = rc.render_markdown(rc.compare_document(_envelope([_unavailable_with_stuff()])))
    assert "not_compared_unavailable" in md


def test_only_explicit_links_compared():
    props = [{"exact_text": "a"}, {"exact_text": "b"}]
    rows = [{"exact_text": "a"}, {"exact_text": "b"}]  # identical content, same order
    rec = rc.compare_record(_base_record(proposals=props, downstream_rows=rows, links=[]))
    assert rec["pairs"] == []
    assert len(rec["unmatched_proposals"]) == 2 and len(rec["unmatched_rows"]) == 2
    rec = rc.compare_record(_base_record(
        proposals=props, downstream_rows=rows, links=[{"proposal_index": 1, "row_index": 0}]))
    assert [(p["proposal_index"], p["row_index"]) for p in rec["pairs"]] == [(1, 0)]
    assert [u["proposal_index"] for u in rec["unmatched_proposals"]] == [0]
    assert [u["row_index"] for u in rec["unmatched_rows"]] == [1]


def test_output_written_only_to_out(tmp_path, monkeypatch, capsys):
    src = tmp_path / "in" / "records.json"
    src.parent.mkdir()
    src.write_text(json.dumps(_envelope([_base_record()])), encoding="utf-8")
    out = tmp_path / "out" / "report.json"
    out.parent.mkdir()
    before = {p for p in tmp_path.rglob("*")}
    monkeypatch.chdir(tmp_path)
    assert rc.main([str(src), "--out", str(out), "--format", "json"]) == 0
    assert capsys.readouterr().out == ""
    assert {p for p in tmp_path.rglob("*")} - before == {out}
    assert json.loads(out.read_text())["ok"] is True
    # no --out: stdout only, no files
    before = {p for p in tmp_path.rglob("*")}
    assert rc.main([str(src)]) == 0
    assert {p for p in tmp_path.rglob("*")} == before
    assert capsys.readouterr().out.startswith("# Reader compare")


def test_no_network(tmp_path, monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    src = tmp_path / "r.json"
    src.write_text(json.dumps(_envelope([_base_record(
        proposals=[{"exact_text": "a"}], downstream_rows=[{"exact_text": "a"}],
        links=[{"proposal_index": 0, "row_index": 0}])])), encoding="utf-8")
    assert rc.main([str(src), "--out", str(tmp_path / "o.md")]) == 0


def test_no_verdict_language_in_output():
    report = rc.compare_document(_envelope([_base_record(
        proposals=[{"exact_text": "a"}], downstream_rows=[{"exact_text": "b"}],
        links=[{"proposal_index": 0, "row_index": 0}])]))
    text = (json.dumps(report) + rc.render_markdown(report)).lower()
    for word in ("pass", "fail", "similarity", "accuracy_score", "f1", "precision", "recall"):
        assert word not in text.replace("not a verdict", ""), word
    assert not any(k in report["summary_counts"] for k in ("score", "accuracy", "pass", "fail"))
