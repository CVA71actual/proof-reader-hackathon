"""Slice 1a: exact inputs/artifact/rule bindings and an honest visible-word limit."""

import copy
import json
from pathlib import Path

from runs import claim_assessment
from runs.recheck import row_checks
from runs.state import current_state
from runs.calendar import partition_intervals, timing_state

ROOT = Path(__file__).resolve().parents[1]


def _sample():
    run = json.loads((ROOT / "runs/sample/run.json").read_text())
    row = copy.deepcopy(run["rows"][0])
    source = next(s for s in run["sources"] if s["id"] == row["source_id"])
    return run, row, source


def test_row_checks_bind_exact_candidate_page_and_checker_inputs():
    run, row, source = _sample()
    page = source["pages"][0]
    words = json.loads((ROOT / "runs/sample" / page["words"]).read_text())
    from runs.sources import load_page_evidence
    _, consumed = load_page_evidence(ROOT / "runs/sample", page)
    results = row_checks(row, source, words, consumed_artifact=consumed)
    from runs.build_run import to_contract_check
    quote = to_contract_check(next(c.to_dict() for c in results if c.check == "quote_match"))
    binding = quote["assessment"]
    assert binding["inputs"] == {"quote": row["quote"], "box": row["box"]}
    assert binding["evidence"]["source_id"] == row["source_id"]
    assert binding["evidence"]["page_number"] == row["page_number"]
    assert binding["evidence"]["image_sha256"] == page["image_sha256"]
    assert binding["evidence"]["words_used"] == consumed
    assert binding["evidence"]["regions"] is None
    assert binding["evaluation_mode"] is None
    assert binding["checker_version"] == claim_assessment.checker_version("quote_match")
    assert claim_assessment.check_current(quote, row, source)


def test_date_binding_omits_derived_bounds_but_binds_a_person_selected_date():
    _, row, source = _sample()
    row["date_text"], row["date"], row["date_end"] = "Q1 2027", None, None
    ordinary = claim_assessment.assessment_for("date_match", row, source)
    projected = claim_assessment.assessment_for(
        "date_match", {**row, "date": "2027-01-01", "date_end": "2027-03-31"}, source)
    assert ordinary["input_sha256"] == projected["input_sha256"]
    chosen_feb = claim_assessment.assessment_for(
        "date_match", {**row, "date": "2027-02-01", "date_end": "2027-02-01"}, source, chosen=True)
    chosen_mar = claim_assessment.assessment_for(
        "date_match", {**row, "date": "2027-03-01", "date_end": "2027-03-01"}, source, chosen=True)
    assert chosen_feb["input_sha256"] != chosen_mar["input_sha256"]


def test_candidate_page_or_checker_change_makes_saved_check_stale(monkeypatch):
    _, row, source = _sample()
    page = source["pages"][0]
    consumed = {"file": page["words"], "sha256": page["words_sha256"], "kind": "captured"}
    binding = claim_assessment.assessment_for("quote_match", row, source, consumed_artifact=consumed)
    check = {"name": "quote_match", "assessment": binding}
    changed = {**row, "quote": row["quote"] + " changed"}
    assert not claim_assessment.check_current(check, changed, source)
    page_changed = copy.deepcopy(source)
    page_changed["pages"][0]["image_sha256"] = "0" * 64
    assert not claim_assessment.check_current(check, row, page_changed)
    monkeypatch.setattr(claim_assessment, "checker_version", lambda _: "0" * 64)
    assert not claim_assessment.check_current(check, row, source)


def test_person_selected_date_and_sam_regions_are_bound_inputs():
    _, row, source = _sample()
    page = source["pages"][0]
    artifact = {"file": page["words"], "sha256": page["words_sha256"], "kind": "captured"}
    autonomous = next(c.to_dict() for c in row_checks(row, source, {}, consumed_artifact=artifact)
                      if c.check == "date_match")
    selected = next(c.to_dict() for c in row_checks(row, source, {}, chosen=True, consumed_artifact=artifact)
                    if c.check == "date_match")
    assert autonomous["assessment"]["evaluation_mode"] == {"date_person_selected": False}
    assert selected["assessment"]["evaluation_mode"] == {"date_person_selected": True}
    quote = next(c.to_dict() for c in row_checks(row, source, {}, consumed_artifact=artifact)
                 if c.check == "quote_match")
    changed = copy.deepcopy(source)
    changed["pages"][0]["regions"] = [{"id": "r1", "box": [1, 2, 3, 4]}]
    assert not claim_assessment.check_current({"name": "quote_match", "assessment": quote["assessment"]}, row, changed)
    changed_scan_state = copy.deepcopy(source)
    changed_scan_state["pages"][0]["scanned"] = True
    assert not claim_assessment.check_current(
        {"name": "quote_match", "assessment": quote["assessment"]}, row, changed_scan_state)


def test_check_freshness_matches_the_artifact_actually_consumed_even_if_derived_exists():
    _, row, source = _sample()
    page = source["pages"][0]
    changed_source = copy.deepcopy(source)
    changed_page = changed_source["pages"][0]
    changed_page["derived_words"] = {"kind": "model_transcription", "file": "derived.words.json",
                                     "sha256": "d" * 64}
    captured = {"file": page["words"], "sha256": page["words_sha256"], "kind": "captured"}
    derived = {"file": "derived.words.json", "sha256": "d" * 64, "kind": "derived"}
    check_output = {"result": "pass", "reason": "fixture", "compared": {}}
    captured_check = {"name": "quote_match", **check_output}
    captured_check["assessment"] = claim_assessment.assessment_for(
        "quote_match", row, changed_source, consumed_artifact=captured, output=check_output)
    derived_check = {"name": "quote_match", **check_output}
    derived_check["assessment"] = claim_assessment.assessment_for(
        "quote_match", row, changed_source, consumed_artifact=derived, output=check_output)

    assert claim_assessment.check_current(captured_check, row, changed_source, consumed_artifact=captured)
    assert not claim_assessment.check_current(derived_check, row, changed_source, consumed_artifact=captured)


def test_derived_artifact_is_the_bytes_parsed_and_missing_or_changed_artifact_refuses(tmp_path):
    from runs.sources import SourcesError, load_page_evidence

    _, row, source = _sample()
    page = copy.deepcopy(source["pages"][0])
    captured = (ROOT / "runs/sample" / page["words"]).read_bytes()
    image_path = tmp_path / page["image"]
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes((ROOT / "runs/sample" / page["image"]).read_bytes())
    derived = tmp_path / "transcribed.words.json"
    derived.write_bytes(captured)
    import hashlib
    page["derived_words"] = {"kind": "model_transcription", "file": derived.name,
                             "sha256": hashlib.sha256(captured).hexdigest(),
                             "based_on_image_sha256": page["image_sha256"]}
    page_words = tmp_path / page["words"]
    page_words.parent.mkdir(parents=True, exist_ok=True)
    page_words.write_bytes(captured)
    words, identity = load_page_evidence(tmp_path, page)
    assert words["words"] and identity == {"file": derived.name, "sha256": page["derived_words"]["sha256"],
                                           "kind": "derived"}
    page_words.write_bytes(captured + b" ")
    try:
        load_page_evidence(tmp_path, page)
    except SourcesError:
        pass
    else:
        raise AssertionError("a changed captured artifact must be refused even when derived words are declared")
    page_words.write_bytes(captured)
    derived.unlink()
    try:
        load_page_evidence(tmp_path, page)
    except SourcesError:
        pass
    else:
        raise AssertionError("missing declared derived words must be refused")
    derived.write_bytes(captured + b" ")
    try:
        load_page_evidence(tmp_path, page)
    except SourcesError:
        pass
    else:
        raise AssertionError("changed declared derived words must be refused")


def test_period_display_bounds_are_added_after_freshness_check():
    run, row, source = _sample()
    row = copy.deepcopy(row)
    row["date_text"], row["date"], row["date_end"] = "Q1 2027", None, None
    from runs.sources import load_page_evidence
    page = source["pages"][0]
    _, artifact = load_page_evidence(ROOT / "runs/sample", page)
    checks = row_checks(row, source, json.loads((ROOT / "runs/sample" / page["words"]).read_text()),
                        consumed_artifact=artifact)
    from runs.build_run import to_contract_check
    saved = [to_contract_check(c.to_dict()) for c in checks]
    date_check = next(c for c in saved if c["name"] == "date_match")
    date_check.update(result="pass", compared={"timing": {"precision": "period", "start": "2027-01-01",
                                                               "end": "2027-03-31"}})
    output = {key: date_check[key] for key in ("result", "reason", "compared")}
    date_check["assessment"] = claim_assessment.assessment_for(
        "date_match", row, source, consumed_artifact=artifact, output=output)
    run["rows"] = [{**row, "checks": saved}]
    state = current_state(run, [])["targets"][row["id"]]
    assert state["checks_stale"] is False
    assert (state["values"]["date"], state["values"]["date_end"]) == ("2027-01-01", "2027-03-31")


def test_legacy_green_becomes_unresolved_and_keeps_original_checks_as_history():
    run, row, _ = _sample()
    for check in row["checks"]:
        check.pop("assessment", None)
    run["rows"][0] = row
    state = current_state(run, [])
    target = state["targets"][row["id"]]
    assert target["group"] == "unverified"
    assert target["status"] == "proposed"
    assert target["assessment_status"] == "unresolved"
    assert target["claim_assessments"]["visible_passage"]["result"] == "not_checked"
    assert target["effective_checks"] == []
    assert target["check_history"] == row["checks"]
    assert state["unresolved"] >= 1


def test_unbound_saved_checks_are_history_after_zero_cost_load_recheck():
    from runs.check_load import bind_on_load

    run, row, _ = _sample()
    for check in row["checks"]:
        check.pop("assessment", None)
    old = copy.deepcopy(row["checks"])
    run["rows"][0] = row
    bind_on_load(run, ROOT / "runs/sample")
    refreshed = run["rows"][0]
    assert refreshed["check_history"] == old
    assert all(check.get("assessment") for check in refreshed["checks"])
    target = current_state(run, [])["targets"][row["id"]]
    assert target["checks_stale"] is False
    assert target["effective_checks"] == refreshed["checks"]
    assert target["check_history"] == old


def test_bound_text_layer_and_box_pass_is_current_while_visibility_stays_not_checked():
    run, row, source = _sample()
    page = source["pages"][0]
    words = json.loads((ROOT / "runs/sample" / page["words"]).read_text())
    from runs.build_run import to_contract_check
    from runs.sources import load_page_evidence
    _, consumed = load_page_evidence(ROOT / "runs/sample", page)
    row["checks"] = [to_contract_check(c.to_dict())
                      for c in row_checks(row, source, words, consumed_artifact=consumed)]
    run["rows"] = [row]
    state = current_state(run, [])
    target = state["targets"][row["id"]]
    assert target["checks_stale"] is False
    assert target["effective_checks"]
    assert target["claim_assessments"]["visible_passage"]["result"] == "not_checked"
    assert target["group"] == "kept"
    assert target["on_calendar"] is True


def test_timing_labels_keep_absent_unresolved_and_explicit_dependency_distinct():
    undated = {"id": "u", "date": None, "date_end": None, "date_text": None, "quote": "The trial is ongoing."}
    unresolved = {"id": "r", "date": None, "date_end": None, "date_text": "in the coming months",
                  "quote": "Results are expected in the coming months."}
    contingent = {"id": "c", "date": None, "date_end": None, "date_text": None,
                  "quote": "The filing will occur only if FDA approves the application."}
    assert timing_state(undated) == "undated"
    assert timing_state(unresolved) == "timing_unresolved"
    assert timing_state(contingent) == "undated"  # conditionality is an independent fact
    parts = partition_intervals([{**undated, "timing_state": timing_state(undated)},
                                 {**unresolved, "timing_state": timing_state(unresolved)},
                                 {**contingent, "timing_state": timing_state(contingent), "contingent": True}],
                                "2026-10-01", "2027-04-01")
    assert [r["id"] for r in parts["undated"]] == ["u"]
    assert [r["id"] for r in parts["timing_unresolved"]] == ["r"]
    assert [r["id"] for r in parts["contingent"]] == ["c"]


def test_one_contract_serializer_preserves_near_miss_and_assessment_to_report_view():
    from checks.common import CheckResult
    from report.data import _check_rows
    from runs.build_run import to_contract_check

    evidence = {"source_id": "s1", "page_number": 3, "image_sha256": "1" * 64,
                "words_sha256": "2" * 64, "words_used_sha256": "3" * 64,
                "derived_words": None, "capture_completeness": {"capture_incomplete": True}}
    assessment = {"inputs": {"quote": "9,"}, "evidence": evidence,
                  "checker_version": "4" * 64, "input_sha256": "5" * 64, "assessment_sha256": "6" * 64}
    result = CheckResult("quote_match", "Quote found on page", "fail", "One word differs.",
                         {"value": "9,", "found": "19,"}, near_match="19,",
                         differs=[("9,", "19,")], assessment=assessment).to_dict()
    saved = to_contract_check(result)
    assert saved["near_match"] == "19," and saved["differs"] == [["9,", "19,"]]
    assert saved["assessment"] == assessment
    [reported] = _check_rows([saved])
    assert reported["near_match"] == "19," and reported["differs"] == [["9,", "19,"]]
    assert reported["assessment"] == assessment
