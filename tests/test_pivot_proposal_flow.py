"""Proposal transport controls, not model-quality or semantic-check clearance.

All readings and page words here are synthetic. The next pivot slice replaces
the old semantic checks; these tests pin lossless transport through the existing
human-review state and report-data paths in the meantime.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from capture.read_events import parse_events
from report.data import build_view
from runs.build_entry import build_row
from runs.calendar import calendar_entries
from runs.compare import _uncheckable_row
from runs.proposal import CONTEXT_FIELDS, context_fields
from runs.state import current_state


AT = "2026-11-01T18:00:00Z"
CASES = [
    ("earnings", "Cedar Bio will release financial results on November 12, 2026.",
     "earnings release", "November 12, 2026", "2026-11-12", []),
    ("qualified", "CB-100 results are expected in Q1 2027 if validation succeeds.",
     "conditional clinical readout", "Q1 2027", "2027-01-01", ["if validation succeeds"]),
    ("connected", "Cedar Bio plans to initiate the CB-100 study. This initiation is expected in Q1 2027.",
     "study initiation", "Q1 2027", "2027-01-01", []),
    ("unlinked", "Cedar Bio plans to initiate the CB-100 study. Its unrelated office lease ends in Q1 2027.",
     "study initiation with timing not established", "", None, []),
    ("filing", "Cedar Bio expects to submit the CB-100 application in Q1 2027; approval has not been granted.",
     "planned submission, not approval", "Q1 2027", "2027-01-01", ["approval has not been granted"]),
]


def make_run(case, directory=None, *, event_updates=None, reading_fake=True):
    name, quote, hint, date_text, day, conditions = case
    proposal = {
        "ticker": "CEDR", "company": "Cedar Bio", "program": "CB-100", "indication": None,
        "title": hint, "type_hint": hint, "quote": quote, "date_text": date_text,
        "date": day, "date_end": "2027-03-31" if date_text == "Q1 2027" else None,
        "conditions": conditions, "relevance": f"Synthetic {name} proposal; meaning needs review.",
        "subject_company": "Cedar Bio", "relation": "Assigned company; no other relationship proposed.",
        "box": None, "word_ids": [],
    }
    if event_updates:
        proposal.update(event_updates)
    events, problem = parse_events(json.dumps({"events": [proposal]}))
    assert problem is None
    words = {"page_number": 1, "page_width": 612, "page_height": 792, "words": []}
    for i, token in enumerate(quote.split()):
        x, y = 20 + (i % 6) * 95, 30 + (i // 6) * 16
        words["words"].append({"id": f"w{i}", "text": token, "x0": x, "y0": y,
                               "x1": x + min(90, len(token) * 5), "y1": y + 12,
                               "block": 0, "line": i // 6, "word": i % 6})
    digest = hashlib.sha256(json.dumps(words).encode()).hexdigest()
    source = {"id": "synthetic-source", "kind": "company_release", "published_date": "2026-11-01",
              "pages": [{"page_number": 1, "words": "synthetic.words.json", "words_sha256": digest,
                         "visual_evidence": {"origin": "text_layer", "overlays_supported": True,
                                             "reason": "Synthetic test fixture, not real page evidence."}}]}
    row = build_row(1, events[0], {"fake": reading_fake, "prompt_text": "Synthetic, no model called."},
                    source, words, AT,
                    consumed_artifact={"file": "synthetic.words.json", "sha256": digest, "kind": "captured"})
    run = {"run": {"id": "synthetic", "number": 1, "sample": True, "started_at": AT,
                   "as_of": "2026-11-01", "finished_at": AT, "spend_usd": None, "choices": {}},
           "sources": [source], "rows": [row], "tables": [], "mistakes": [], "exclusions": []}
    if directory is not None:
        directory.mkdir(parents=True)
        (directory / "synthetic.words.json").write_text(json.dumps(words))
        (directory / "run.json").write_text(json.dumps(run))
    return proposal, run


def decision(action):
    return {"at": AT, "action": action, "by": "person", "target": "e1", "fix": None, "note": None}


def kept_decision(run, previous=()):
    # New Keeps carry the server-computed basis of the effective proposal.
    # Legacy unbound Keeps are tested separately as history, never restamped.
    basis = current_state(run, list(previous))["targets"]["e1"]["review_basis"]
    return {**decision("keep"), "review_basis": basis}


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_proposal_survives_saved_state_keep_undo_and_report_data(case):
    proposal, run = make_run(case)
    # The storage round trip is intentional: references to a model-response
    # object cannot substitute for actually saving each field.
    run = json.loads(json.dumps(run))
    checks = copy.deepcopy(run["rows"][0]["checks"])
    before = current_state(run, [])
    for field in CONTEXT_FIELDS:
        assert run["rows"][0][field] == proposal[field]
        assert before["targets"]["e1"]["values"][field] == proposal[field]
    initial_report = build_view(run, [], generated_at=AT)
    initial_rows = sum((initial_report[key] for key in ("calendar", "later", "past", "contingent", "undated", "timing_unresolved", "open_rows")), [])
    assert len(initial_rows) == 1
    for field in CONTEXT_FIELDS:
        assert initial_rows[0][field] == proposal[field]

    keep = kept_decision(run)
    kept = current_state(run, [keep])
    entry = calendar_entries(run, kept)[0]
    report = build_view(run, [keep], generated_at=AT)
    report_rows = sum((report[key] for key in ("calendar", "later", "past", "contingent", "undated", "timing_unresolved")), [])
    assert len(report_rows) == 1
    for field in CONTEXT_FIELDS:
        assert entry[field] == proposal[field] == report_rows[0][field]
    assert entry["decided_by_person"]
    assert run["rows"][0]["checks"] == checks
    assert not any(kept["mistakes"].values())

    undone = current_state(run, [keep, decision("undo")])
    assert calendar_entries(run, undone) == calendar_entries(run, before)
    assert undone["targets"]["e1"]["needs_attention"] == before["targets"]["e1"]["needs_attention"]
    assert json.loads(json.dumps(undone))["targets"]["e1"]["values"]["conditions"] == proposal["conditions"]
    # Required pivot gate, intentionally still red until the shared checker
    # handoff: transporting a proposal is not semantic support for it.
    assert before["targets"]["e1"]["needs_attention"]
    assert not calendar_entries(run, before)


def test_unknown_page_second_reader_keeps_qualifications_without_checks():
    proposal, _ = make_run(CASES[1])
    row = _uncheckable_row(1, proposal, None, {"prompt_text": "Synthetic"}, (None, None, 1), AT)
    assert row["checks"] == []
    for field in CONTEXT_FIELDS:
        assert row[field] == proposal[field]


def test_context_does_not_invent_hint_or_alias_mutable_conditions():
    assert context_fields({"event_type": "launch"})["type_hint"] is None
    original = {"conditions": ["if validation succeeds"]}
    copied = context_fields(original)
    copied["conditions"].clear()
    assert original["conditions"] == ["if validation succeeds"]


def test_absent_and_null_conditions_are_unknown_but_empty_list_is_preserved():
    assert context_fields({})["conditions"] is None
    assert context_fields({"conditions": None})["conditions"] is None
    assert context_fields({"conditions": []})["conditions"] == []


@pytest.mark.parametrize("field", CONTEXT_FIELDS)
@pytest.mark.parametrize("invalid", [3, {"x": 1}, [1], "   "])
def test_malformed_proposal_values_are_unknown_on_live_and_unknown_page_paths(field, invalid):
    from runs.compare import _model_step
    proposal, run = make_run(CASES[1], event_updates={field: invalid}, reading_fake=False)
    row = run["rows"][0]
    assert row[field] is None
    unknown = _uncheckable_row(1, proposal, None, {"prompt_text": "Synthetic"}, (None, None, 1), AT)
    assert unknown[field] is None
    for candidate in (row, unknown):
        steps = [t for t in candidate["trail"] if t["step"] == "Proposal shape"]
        assert len(steps) == 1 and steps[0]["tag"] == "Code"
        assert field in steps[0]["text"]
    schema = json.loads((Path(__file__).parents[1] / "runs/schema/run.schema.json").read_text())
    errors = list(Draft202012Validator({"$ref": "#/$defs/row", "$defs": schema["$defs"]}).iter_errors(row))
    assert not errors, [(list(e.path), e.message) for e in errors]
    step = _model_step(proposal, (None, None, 1), AT)
    if field == "type_hint":
        assert "type hint none" in step["text"]


def test_partly_invalid_conditions_are_wholly_unknown_with_a_named_trail():
    from runs.proposal import context_steps
    proposal, run = make_run(CASES[1], event_updates={
        "conditions": ["if validation succeeds", ""], "relevance": 3}, reading_fake=False)
    row = run["rows"][0]
    assert row["conditions"] is None and row["relevance"] is None
    steps = context_steps(proposal, AT)
    assert len(steps) == 1 and steps[0] in row["trail"]
    assert "conditions" in steps[0]["text"] and "relevance" in steps[0]["text"]
    assert not run["mistakes"]
    assert context_steps({}, AT) == []
    assert context_steps({"conditions": None}, AT) == []
    assert context_steps({"conditions": []}, AT) == []


def test_second_reader_trail_respects_explicit_unknown_but_preserves_legacy_raw_hint():
    from runs.compare import _model_step
    # Raw reader event_type is an old input spelling; an explicit unknown
    # type_hint is not permission to borrow a controlled classification.
    assert "type hint launch" in _model_step({"event_type": "launch"}, (None, None, 1), AT)["text"]
    assert "type hint none" in _model_step({"event_type": "launch", "type_hint": None},
                                          (None, None, 1), AT)["text"]


@pytest.mark.parametrize("replacement_context", ["absent", "malformed", "complete"])
def test_bounded_repair_preserves_omitted_context_but_holds_malformed_context(tmp_path, replacement_context):
    from runs import escalate
    proposal, run = make_run(CASES[1], tmp_path / "source", event_updates={"date": "2027-02-01"}, reading_fake=False)
    row = run["rows"][0]
    assert row["status"] == "failed"
    before = copy.deepcopy(row)
    answer = {**proposal, "date": "2027-01-01"}
    if replacement_context == "absent":
        for field in CONTEXT_FIELDS:
            answer.pop(field)
    elif replacement_context == "malformed":
        answer["conditions"] = "if validation succeeds"
    words = json.loads((tmp_path / "source/synthetic.words.json").read_text())
    outcome = escalate.apply(row, answer, {"fake": False, "prompt_text": "Synthetic"},
                             run["sources"][0], words, AT, escalate.trigger(row), run,
                             (None, None, 1), "Synthetic")
    # ESCALATION-PATCH-001: omitted interpretation was not requested. Only
    # date fields may change; explicit malformed interpretation remains held.
    if replacement_context in ("complete", "absent"):
        assert outcome == "replaced"
        assert row["date"] == "2027-01-01" and row["conditions"] == proposal["conditions"]
    else:
        assert outcome == "kept"
        for field in escalate.FIELDS + ("status", "group", "checks"):
            assert row[field] == before[field], field
        assert "changes interpretation" in row["trail"][-1]["text"]
        assert run["mistakes"] == []
        assert not calendar_entries(run, current_state(run, []))
        if replacement_context == "malformed":
            assert any(t["step"] == "Proposal shape" and "conditions" in t["text"]
                       for t in row["trail"])


def test_conditions_edit_shape_is_enforced_in_schema_and_state():
    schema = json.loads((Path(__file__).parents[1] / "runs/schema/decision.schema.json").read_text())
    validator = Draft202012Validator(schema)
    _, run = make_run(CASES[1])
    line = {**decision("fix"), "fix": {"field": "conditions", "from": ["if validation succeeds"], "to": []}}
    assert not list(validator.iter_errors(line))
    state = current_state(run, [line])
    assert state["targets"]["e1"]["values"]["conditions"] == []
    assert not any(state["mistakes"].values())
    for bad in ("unconditional", [3], {"condition": "none"}):
        changed = {**line, "fix": {**line["fix"], "to": bad}}
        assert list(validator.iter_errors(changed))
        assert current_state(run, [changed])["ignored"]
    changed = {**line, "fix": {"field": "title", "from": "old", "to": []}}
    assert list(validator.iter_errors(changed))


def test_api_conditions_edit_rechecks_without_counting_a_semantic_mistake(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app

    proposal, run = make_run(CASES[1], tmp_path / "synthetic")
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        root = "/api/runs/synthetic"
        initial = client.get(root)
        assert initial.status_code == 200, initial.text
        assert initial.json()["state"]["targets"]["e1"]["values"]["conditions"] == proposal["conditions"]
        keep = {"target": "e1", "action": "keep"}
        assert client.post(root + "/decisions", json=keep).status_code == 200
        fixed = client.post(root + "/decisions", json={"target": "e1", "action": "fix", "fix": {
            "field": "conditions", "from": proposal["conditions"], "to": []}})
        assert fixed.status_code == 200, fixed.text
        state = fixed.json()["state"]
        assert state["values"]["conditions"] == []
        assert state["recheck"]["result"] == "could_not_run"
        assert not state["on_calendar"]
        assert not any(client.get(root).json()["state"]["mistakes"].values())
        # No heuristic treats the human's removal of a qualification as a
        # successful semantic correction. A further keep is a separate choice.
        assert client.post(root + "/decisions", json=keep).status_code == 200
        entry = client.get(root + "/calendar").json()["entries"][0]
        assert entry["conditions"] == [] and entry["decided_by_person"]
        undo = {"target": "e1", "action": "undo"}
        assert client.post(root + "/decisions", json=undo).status_code == 200
        assert client.get(root + "/calendar").json()["entries"] == []
        assert client.post(root + "/decisions", json=undo).status_code == 200
        assert client.get(root + "/calendar").json()["entries"][0]["conditions"] == proposal["conditions"]
    # Reload through a new server instance, not a retained client/state object.
    with TestClient(create_app(runs_dir=tmp_path)) as client:
        assert client.get(root + "/calendar").json()["entries"][0]["conditions"] == proposal["conditions"]
    assert json.loads((tmp_path / "synthetic/run.json").read_text())["rows"] == run["rows"]
