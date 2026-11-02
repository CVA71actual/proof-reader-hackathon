"""Job 49: one owner for row status (runs.state); a repair tool only replaces a check result."""
import hashlib
import json
from pathlib import Path

from runs import recheck_dates as rd
from runs.state import current_state
from checks.restriction import page_capability
from runs.claim_assessment import bind_contract_checks

DATE = {"name": "date_match", "label": "Date read as written", "kind": "Code", "result": "pass", "reason": "x",
        "compared": {}}


def _check(name, result="pass"):
    return {"name": name, "label": name, "kind": "Code", "result": result, "reason": "r", "compared": {}}


def _row(status, group, checks, disagreement=None):
    row = {"id": "e1", "source_id": "s1", "page_number": 1, "event_type": "trial_results", "title": "topline",
           "program": "Drug A", "quote": "topline results in August 2026.", "date_text": "August 2026",
           "date": "2026-08-01", "date_end": "2026-08-31", "status": status, "group": group, "reason": "",
           "trail": [], "checks": checks}
    if disagreement:
        row["disagreement"] = disagreement
    return row


def _run(row):
    source = {"id": "s1", "kind": "filing", "published_date": "2026-08-01", "pages": [{
        "page_number": 1, "image": "pages/p1.png", "image_sha256": "b" * 64,
        "words": "pages/p1.words.json", "words_sha256": "a" * 64,
        "visual_evidence": {"origin": "text_layer", "overlays_supported": True, "reason": None}}]}
    row["checks"] = bind_contract_checks(row["checks"], row, source,
                                          consumed_artifact={"file": "pages/p1.words.json", "sha256": "a" * 64,
                                                             "kind": "captured"},
        capability=page_capability(source["pages"][0]))
    return {"run": {"started_at": "2026-08-10T10:00:00+00:00", "choices": {}},
            "sources": [source],
            "rows": [row], "exclusions": [], "tables": [], "mistakes": []}


def test_date_repair_changes_the_check_but_does_not_write_row_status(tmp_path):
    row = _row("failed", "open", _five("fail"))
    run = _run(row)
    page = run["sources"][0]["pages"][0]
    page.pop("image")
    page.pop("image_sha256")
    words = []
    x = 20.0
    for i, token in enumerate(row["quote"].split()):
        width = max(5.0, len(token) * 5.0)
        words.append({"id": f"w{i}", "text": token, "x0": x, "y0": 100.0,
                      "x1": x + width, "y1": 112.0})
        x += width + 2.0
    raw = (json.dumps({"page_width": 612.0, "page_height": 792.0, "words": words}) + "\n").encode()
    path = tmp_path / page["words"]
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    page["words_sha256"] = hashlib.sha256(raw).hexdigest()
    source = run["sources"][0]
    row["checks"] = bind_contract_checks(
        row["checks"], row, source,
        consumed_artifact={"file": page["words"], "sha256": page["words_sha256"], "kind": "captured"},
        capability=page_capability(source["pages"][0]))
    before = (row["status"], row["group"])

    result = rd.recheck_dates(run, tmp_path, set(), "t")

    assert result["mutated"] == ["e1"]
    assert next(c for c in row["checks"] if c["name"] == "date_match")["result"] == "pass"
    assert (row["status"], row["group"]) == before  # the repair records a check; state derives status


TWO_SIDED = {"field": "program", "first_reader": "Drug A", "second_reader": "Drug B"}


def test_a_date_check_change_does_not_resolve_a_two_sided_program_disagreement():
    run = _run(_row("undecided", "open", _five(), TWO_SIDED))
    run["rows"][0]["checks"][2] = {**DATE, "reason": "rechecked", "result": "pass"}
    run["rows"][0]["checks"] = bind_contract_checks(
        run["rows"][0]["checks"], run["rows"][0], run["sources"][0],
        consumed_artifact={"file": "pages/p1.words.json", "sha256": "a" * 64, "kind": "captured"},
        capability=page_capability(run["sources"][0]["pages"][0]))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_legacy_recheck_without_results_is_ignored_by_pure_state_callers():
    run = _run(_row("matched", "kept", _five()))
    fix = {"action": "fix", "target": "e1", "by": "person",
           "fix": {"field": "date_text", "from": "August 2026", "to": "September 2026"}, "at": "t"}
    legacy = {"action": "recheck", "target": "e1", "by": "code",
              "recheck": {"result": "pass", "reason": None, "checks": []}, "at": "t"}
    state = current_state(run, [fix, legacy])
    assert len(state["ignored"]) == 1
    assert state["ignored"][0]["reason"] == "The saved re-check has no check results; it remains history."
    assert state["targets"]["e1"]["needs_recheck"]


def test_a_one_sided_disagreement_with_a_failed_check_does_not_become_matched():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", [_check("quote_match", "fail"), DATE], one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_a_one_sided_disagreement_with_a_missing_check_does_not_become_matched():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", [], one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "undecided" and t["group"] == "open"


def test_a_one_sided_disagreement_with_passing_checks_still_needs_acceptance():
    one_sided = {"field": "program", "first_reader": "Drug A", "second_reader": None}
    run = _run(_row("undecided", "open", _five(), one_sided))
    t = current_state(run, [])["targets"]["e1"]
    assert t["status"] == "matched" and t["group"] == "kept"
    # GROUP-AXIS-001: preserve mechanical assertions, require human acceptance.
    assert t["needs_attention"] and not t["accepted"] and not t["on_calendar"]


# ---- Job 49b: one shared derivation (runs.state.derive_status) from the CURRENT effective checks
FIVE = ("quote_match", "selector_match", "date_match", "source_rule", "type_match")


def _five(date_result="pass"):
    return [_check(n, date_result if n == "date_match" else "pass") for n in FIVE]


def _rechecked(run, new_result):
    run["rows"][0]["checks"][2] = {**DATE, "result": new_result}
    run["rows"][0]["checks"] = bind_contract_checks(
        run["rows"][0]["checks"], run["rows"][0], run["sources"][0],
        consumed_artifact={"file": "pages/p1.words.json", "sha256": "a" * 64, "kind": "captured"},
        capability=page_capability(run["sources"][0]["pages"][0]))
    return current_state(run, [])


def test_saved_pass_rechecked_fail_is_not_matched_and_not_on_the_calendar():
    run = _run(_row("matched", "kept", _five()))
    st = _rechecked(run, "fail")
    t = st["targets"]["e1"]
    assert (t["status"], t["group"]) == ("failed", "open")
    assert st["counts"]["rows"]["kept"] == 0
    from runs.calendar import on_calendar
    assert not on_calendar(t)


def test_saved_fail_rechecked_pass_is_mechanically_matched_but_not_accepted():
    run = _run(_row("failed", "open", _five("fail")))
    t = _rechecked(run, "pass")["targets"]["e1"]
    assert (t["status"], t["group"]) == ("matched", "kept")
    # GROUP-AXIS-001: rechecking a value never accepts the interpretation.
    assert t["needs_attention"] and not t["accepted"] and not t["on_calendar"]


def test_saved_fail_rechecked_pass_with_a_disagreement_stays_for_a_person():
    run = _run(_row("failed", "open", _five("fail"), TWO_SIDED))
    t = _rechecked(run, "pass")["targets"]["e1"]
    assert (t["status"], t["group"]) == ("undecided", "open")


def test_second_reader_only_row_still_needs_review_when_checks_pass():
    row = _row("undecided", "open", _five())
    row["found_by"] = "second_reader"
    t = current_state(_run(row), [])["targets"]["e1"]
    assert (t["status"], t["group"]) == ("undecided", "open")


def test_a_human_keep_overrides_the_derived_status():
    run = _run(_row("matched", "kept", _five("fail")))
    keep = {"action": "keep", "target": "e1", "by": "person", "at": "t"}
    t = current_state(run, [keep])["targets"]["e1"]
    assert t["group"] == "kept"


def test_a_code_exclusion_is_untouched_by_the_derivation():
    run = _run(_row("failed", "removed", _five("pass")))
    t = current_state(run, [])["targets"]["e1"]
    assert (t["status"], t["group"]) == ("failed", "removed")
