import json

import pytest
from fastapi import HTTPException

from runs.build_run import write_run
from runs.ids import duplicate_target_ids, reserve_row_numbers
from runs import repair_ids
from runs.repair_ids import repair
from runs.state import current_state
from server.app import RunStore


def test_row_number_reservations_survive_pruning_and_stale_counters():
    run = {"rows": [{"id": "e1"}, {"id": "e5"}, {"id": "e6"}], "tables": []}
    job = {"next_row_number": 7}

    assert reserve_row_numbers(run, job, 2) == 7
    assert job["next_row_number"] == 9

    run["rows"].pop()  # a later stage may remove a coverage row after assigning its target id
    assert reserve_row_numbers(run, job, 1) == 9

    # Old job files may not carry the counter; the largest surviving id still wins.
    assert reserve_row_numbers(run, {}, 1) == 6


def test_row_number_reservation_refuses_duplicate_targets():
    run = {"rows": [{"id": "e6"}, {"id": "e6"}], "tables": []}
    assert duplicate_target_ids(run) == ["e6"]
    with pytest.raises(ValueError, match="duplicate target IDs: e6"):
        reserve_row_numbers(run, {}, 1)


def test_state_and_server_store_refuse_ambiguous_duplicate_targets(tmp_path):
    run = {"rows": [{"id": "e6"}, {"id": "e6"}], "tables": [], "sources": []}
    with pytest.raises(ValueError, match="duplicate target IDs: e6"):
        current_state(run, [])

    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps(run))
    with pytest.raises(HTTPException) as exc:
        RunStore(tmp_path).run("run-11")
    assert exc.value.status_code == 409
    assert "e6" in exc.value.detail


def test_write_run_refuses_duplicate_ids_before_persisting(tmp_path):
    run = {"run": {}, "rows": [{"id": "e6"}, {"id": "e6"}], "tables": []}
    with pytest.raises(ValueError, match="duplicate target IDs: e6"):
        write_run(run, tmp_path)
    assert not (tmp_path / "run.json").exists()


def test_explicit_id_repair_dry_run_then_rewrites_rows_and_decisions(tmp_path):
    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    run = {
        "rows": [{"id": "e6", "source_id": "s1"}, {"id": "e6", "source_id": "s2"}],
        "tables": [],
        "mistakes": [{"target": "e6", "caught_by": "person"}],
        "exclusions": [{"target": "e6", "rule": "person_removed"}],
        "id_repairs": {
            "targets": [
                {"path": ["rows", 0], "from": "e6", "to": "e6"},
                {"path": ["rows", 1], "from": "e6", "to": "e13"},
            ],
            "references": [
                {"path": ["mistakes", 0, "target"], "from": "e6", "to": "e13"},
                {"path": ["exclusions", 0, "target"], "from": "e6", "to": "e13"},
            ],
            "decisions": [
                {"line": 0, "from": "e6", "to": "e13"},
            ],
        },
    }
    run_path = run_dir / "run.json"
    decisions_path = run_dir / "decisions.jsonl"
    (run_dir / "accuracy.json").write_text("stale score bytes")
    run_path.write_text(json.dumps(run))
    decisions_path.write_text(json.dumps({"target": "e6", "action": "keep"}) + "\n")

    before_run, before_decisions = run_path.read_text(), decisions_path.read_text()
    assert "would repair 2 targets and 1 decisions" in repair(run_dir)
    assert run_path.read_text() == before_run
    assert decisions_path.read_text() == before_decisions

    assert "repaired 2 targets and 1 decisions" in repair(run_dir, write=True)
    saved = json.loads(run_path.read_text())
    assert [row["id"] for row in saved["rows"]] == ["e6", "e13"]
    assert saved["id_repairs"]["applied"] is True
    assert saved["mistakes"][0]["target"] == "e13"
    assert saved["exclusions"][0]["target"] == "e13"
    assert saved["report_stale"] is True
    assert saved["accuracy_stale"] is True
    assert json.loads(decisions_path.read_text())["target"] == "e13"
    assert duplicate_target_ids(saved) == []


def test_explicit_id_repair_refuses_unmapped_ambiguous_decision(tmp_path):
    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "rows": [{"id": "e6"}, {"id": "e6"}], "tables": [],
        "id_repairs": {"targets": [
            {"path": ["rows", 0], "from": "e6", "to": "e6"},
            {"path": ["rows", 1], "from": "e6", "to": "e13"},
        ], "decisions": []},
    }))
    (run_dir / "decisions.jsonl").write_text(json.dumps({"target": "e6", "action": "keep"}) + "\n")
    with pytest.raises(ValueError, match="Every decision on an ambiguous ID"):
        repair(run_dir, write=True)
    assert duplicate_target_ids(json.loads((run_dir / "run.json").read_text())) == ["e6"]


def test_explicit_id_repair_requires_mappings_for_exclusion_and_mistake_references(tmp_path):
    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "rows": [{"id": "e6"}, {"id": "e6"}], "tables": [],
        "mistakes": [{"target": "e6"}], "exclusions": [{"target": "e6"}],
        "id_repairs": {"targets": [
            {"path": ["rows", 0], "from": "e6", "to": "e6"},
            {"path": ["rows", 1], "from": "e6", "to": "e13"},
        ], "decisions": [], "references": []},
    }))
    (run_dir / "decisions.jsonl").write_text("")
    with pytest.raises(ValueError, match="ambiguous exclusion/mistake reference"):
        repair(run_dir, write=True)
    saved = json.loads((run_dir / "run.json").read_text())
    assert saved["mistakes"][0]["target"] == saved["exclusions"][0]["target"] == "e6"
    assert not (run_dir / "id_repairs.pending.json").exists()


def test_explicit_id_repair_rejects_decision_mapping_to_unrelated_existing_target(tmp_path):
    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "rows": [{"id": "e6"}, {"id": "e6"}, {"id": "e8"}], "tables": [],
        "id_repairs": {"targets": [
            {"path": ["rows", 0], "from": "e6", "to": "e6"},
            {"path": ["rows", 1], "from": "e6", "to": "e13"},
        ], "decisions": [{"line": 0, "from": "e6", "to": "e8"}]},
    }))
    (run_dir / "decisions.jsonl").write_text(json.dumps({"target": "e6", "action": "keep"}) + "\n")
    with pytest.raises(ValueError, match="does not map an ambiguous ID"):
        repair(run_dir, write=True)
    assert not (run_dir / "id_repairs.pending.json").exists()


def test_interrupted_id_repair_stays_unserved_and_can_be_completed(tmp_path, monkeypatch):
    run_dir = tmp_path / "run-11"
    run_dir.mkdir()
    run = {"run": {"id": "run-11"}, "rows": [{"id": "e6"}, {"id": "e6"}], "tables": [],
           "id_repairs": {"targets": [
               {"path": ["rows", 0], "from": "e6", "to": "e6"},
               {"path": ["rows", 1], "from": "e6", "to": "e13"},
           ], "decisions": [{"line": 0, "from": "e6", "to": "e13"}]}}
    (run_dir / "run.json").write_text(json.dumps(run))
    (run_dir / "decisions.jsonl").write_text(json.dumps({"target": "e6", "action": "keep"}) + "\n")
    real_replace = repair_ids._replace_atomically
    failed = False

    def fail_run_replacement(path, text):
        nonlocal failed
        if path.name == "run.json" and not failed:
            failed = True
            raise OSError("simulated stop between file replacements")
        real_replace(path, text)

    monkeypatch.setattr(repair_ids, "_replace_atomically", fail_run_replacement)
    with pytest.raises(OSError, match="simulated stop"):
        repair(run_dir, write=True)
    assert (run_dir / "id_repairs.pending.json").exists()
    with pytest.raises(HTTPException) as refused:
        RunStore(tmp_path).run("run-11")
    assert refused.value.status_code == 409 and "interrupted ID repair" in refused.value.detail

    monkeypatch.setattr(repair_ids, "_replace_atomically", real_replace)
    assert "completed interrupted ID repair" in repair(run_dir, write=True)
    assert not (run_dir / "id_repairs.pending.json").exists()
    assert [row["id"] for row in json.loads((run_dir / "run.json").read_text())["rows"]] == ["e6", "e13"]
    assert json.loads((run_dir / "decisions.jsonl").read_text())["target"] == "e13"
