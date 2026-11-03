"""R1: the run.json contract, the saved demo run, the sample run, and the state rule."""

import copy
from dataclasses import replace
import importlib
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from checks import event_checks
from runs import build_run, build_sample, recheck as recheck_module, sources
from runs.claim_assessment import bind_checks
from runs.state import GROUPS, current_state

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
SCHEMA = json.loads((RUNS / "schema" / "run.schema.json").read_text())
DECISION_SCHEMA = json.loads((RUNS / "schema" / "decision.schema.json").read_text())

RESULTS = {"pass", "fail", "could_not_run", "not_needed"}
RAN = {"pass", "fail"}


def load(name):
    from runs.check_load import load_bound_run
    return load_bound_run(RUNS / name)


@pytest.fixture(scope="module")
def demo():
    return load("demo")


@pytest.fixture(scope="module")
def sample():
    return load("sample")


@pytest.fixture(scope="module")
def sample_decisions(sample):
    lines = (RUNS / "sample" / "decisions.jsonl").read_text().splitlines()
    decisions = [json.loads(x) for x in lines if x.strip()]
    from runs.check_load import bind_decisions_on_load
    return bind_decisions_on_load(sample, decisions, RUNS / "sample")


def all_checks(run):
    for t in run["tables"]:
        for r in t["rows"]:
            for c in r["cells"]:
                for ch in c["checks"]:
                    yield c, ch
    for row in run["rows"]:
        for ch in row["checks"]:
            yield row, ch


def items(run):
    for t in run["tables"]:
        for r in t["rows"]:
            yield from r["cells"]
    yield from run["rows"]


# ---------------------------------------------------------------- schema


def test_schemas_are_valid_2020_12():
    Draft202012Validator.check_schema(SCHEMA)
    Draft202012Validator.check_schema(DECISION_SCHEMA)


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_runs_validate(name):
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(load(name)), key=str)
    assert not errors, [e.message for e in errors[:5]]


def test_sample_decisions_validate(sample_decisions):
    v = Draft202012Validator(DECISION_SCHEMA)
    for d in sample_decisions:
        assert not list(v.iter_errors(d)), d


def test_schema_rejects_bad_result_and_bad_first_step(sample):
    bad = copy.deepcopy(sample)
    bad["rows"][0]["checks"][0]["result"] = "ok"
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))
    bad = copy.deepcopy(sample)
    bad["rows"][0]["trail"][0]["step"] = "Proposed"
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))


def test_decision_schema_needs_fix_only_on_fix():
    v = Draft202012Validator(DECISION_SCHEMA)
    base = {"at": "2026-10-27T16:00:00+00:00", "target": "e1", "note": None, "by": "person"}
    assert not list(v.iter_errors({**base, "action": "keep", "fix": None}))
    assert list(v.iter_errors({**base, "action": "fix", "fix": None}))
    assert list(v.iter_errors({**base, "action": "keep", "fix": {"field": "date", "from": "a", "to": "b"}}))


# ---------------------------------------------------------------- checks and statuses


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_every_check_result_is_allowed(name):
    for _, ch in all_checks(load(name)):
        assert ch["result"] in RESULTS
        assert ch["kind"] == "Code"


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_matched_only_when_every_check_that_ran_passed(name):
    for it in items(load(name)):
        results = [c["result"] for c in it["checks"]]
        if it["status"] == "matched":
            assert all(r in ("pass", "not_needed") for r in results), it["id"]
            assert "pass" in results, it["id"]
        if "fail" in results:
            assert it["status"] == "failed", it["id"]


def test_demo_cells_match_the_s1_status_rule(demo):
    for c in items(demo):
        results = [ch["result"] for ch in c["checks"]]
        want = "failed" if "fail" in results else "matched" if all(r == "pass" for r in results) else "proposed"
        assert c["status"] == want


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_code_refs_point_at_real_functions(name):
    for _, ch in all_checks(load(name)):
        ref = ch["code_ref"]
        path = REPO / ref["file"]
        assert path.is_file(), ref
        module = importlib.import_module(ref["file"][:-3].replace("/", "."))
        assert callable(getattr(module, ref["function"], None)), ref
        assert ref["function"] == ch["name"]


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_files_named_in_the_run_exist(name):
    run = load(name)
    for s in run["sources"]:
        for p in s["pages"]:
            assert (RUNS / name / p["image"]).is_file()
            words = json.loads((RUNS / name / p["words"]).read_text())
            assert words["page_width"] == p["width_pt"] and words["page_height"] == p["height_pt"]
    ids = {s["id"] for s in run["sources"]}
    for x in run["tables"] + run["rows"]:
        assert x["source_id"] in ids


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_ids_are_unique_and_mistakes_point_at_them(name):
    run = load(name)
    ids = [it["id"] for it in items(run)]
    assert len(ids) == len(set(ids))
    for m in run["mistakes"] + run["exclusions"]:
        assert m["target"] in ids
    assert len({x["id"] for x in run["exclusions"]}) == len(run["exclusions"])


@pytest.mark.parametrize("name", ["demo", "sample"])
def test_table_instructions_are_the_exact_prompt(name):
    from capture.read_table import READING_PATH, sha256_text

    reading = json.loads(READING_PATH.read_text())
    for t in load(name)["tables"]:
        assert t["instructions"]["prompt_text"] == reading["prompt_text"]
        assert sha256_text(t["instructions"]["prompt_text"]) == t["instructions"]["prompt_sha256"]
        assert t["model"] == {"name": "Muse Spark (muse-spark-1.3)", "tag": "Model"}


def test_every_trail_starts_with_the_exact_instructions(sample):
    for row in sample["rows"]:
        sent, first = row["trail"][:2]
        assert sent["step"] == "Sent the page to Muse Spark" and sent["tag"] == "Code"
        assert first["step"] == "Instructions sent"
        assert first["text"] == build_sample.SAMPLE_EVENT_INSTRUCTIONS


CHECK_FUNCTIONS = {"text_layer_match", "selector_match", "row_column_match", "quote_match", "date_match", "source_rule"}


def test_every_trail_step_has_one_actor(sample):
    for row in sample["rows"]:
        for step in row["trail"]:
            if step["tag"] == "Code":
                assert step["who"] not in build_sample.MODEL_NAMES, (row["id"], step)
                assert "Muse Spark" not in step["who"], (row["id"], step)
            elif step["tag"] == "Model":
                assert step["who"] in build_sample.MODEL_NAMES, (row["id"], step)
                assert step["who"] not in CHECK_FUNCTIONS, (row["id"], step)
            else:
                assert step["who"] == "person", (row["id"], step)


def test_every_runs_page_files_match_their_recorded_hashes():
    """The page image and words copy in each run folder are bound to the sha256 in run.json."""
    import hashlib
    run_dirs = sorted(p.parent for p in RUNS.glob("*/run.json"))
    assert run_dirs
    for run_dir in run_dirs:
        run = json.loads((run_dir / "run.json").read_text())
        for source in run["sources"]:
            for page in source["pages"]:
                for key in ("image", "words"):
                    data = (run_dir / page[key]).read_bytes()
                    assert hashlib.sha256(data).hexdigest() == page[f"{key}_sha256"], (run_dir.name, page[key])
                    changed = bytearray(data)
                    changed[-1] ^= 0x01
                    assert hashlib.sha256(changed).hexdigest() != page[f"{key}_sha256"]


def test_schema_model_names_cover_every_model_the_builders_name():
    names = set(SCHEMA["$defs"]["model_name"]["enum"])
    assert set(build_sample.MODEL_NAMES) <= names and build_run.MUSE_SPARK in names


def test_rereadings_are_a_model_step_then_a_code_step(sample):
    rows = {r["id"]: r for r in sample["rows"]}
    for rid, model in (("e3", "Muse Spark"), ("e4", "Muse Spark Open")):
        trail = rows[rid]["trail"]
        i = next(n for n, s in enumerate(trail) if s["step"] == "Fixed")
        assert trail[i]["tag"] == "Code"
        assert trail[i - 1]["tag"] == "Model" and trail[i - 1]["who"] == model
        assert trail[i - 1]["text"].startswith("Read ")
    e3 = rows["e3"]["trail"]
    i = next(n for n, s in enumerate(e3) if s["step"] == "Sent back")
    assert e3[i]["tag"] == "Code" and "Date read as written failed" in e3[i]["text"]
    assert e3[i - 1]["who"] == "date_match" and "fail" in e3[i - 1]["text"]


# ---------------------------------------------------------------- the two runs


def test_demo_is_the_real_reading(demo):
    assert demo["run"]["label"] == "Saved demo run"
    assert demo["run"]["sample"] is False
    assert demo["rows"] == [] and demo["mistakes"] == [] and demo["exclusions"] == []
    cells = list(items(demo))
    assert len(cells) == 69
    assert all(c["status"] == "matched" for c in cells)


def test_demo_current_checks_match_rebuild_without_rewriting_history(demo, tmp_path):
    from tests.conftest import rebuilt_source_view
    saved = RUNS / "demo" / "run.json"
    before = saved.read_bytes()
    reading = json.loads(build_run.read_table.READING_PATH.read_text())
    fresh = build_run.build_demo_run(reading, tmp_path / "demo", finished_at=demo["run"]["finished_at"])
    assert rebuilt_source_view(fresh) == rebuilt_source_view(demo)
    assert saved.read_bytes() == before
    assert all(c["check_history"] for row in demo["tables"][0]["rows"] for c in row["cells"])


def test_fake_reading_is_never_labelled_saved_demo(tmp_path):
    from capture import read_table

    fake = read_table.read_fake(read_table.load_words())
    run = build_run.build_demo_run(fake, tmp_path / "practice")
    assert run["run"]["label"] != build_run.REAL_LABEL
    assert "fake" in run["run"]["label"].lower()
    assert "fake" in run["tables"][0]["model"]["name"]
    assert {c["status"] for c in items(run)} == {"matched", "failed", "proposed"}


def test_sample_current_checks_and_rechecks_match_rebuild_history_is_unchanged(sample, tmp_path):
    from tests.conftest import rebuilt_source_view
    from runs.check_load import bind_decisions_on_load
    folder = RUNS / "sample"
    before = {name: (folder / name).read_bytes() for name in ("run.json", "decisions.jsonl")}
    fresh, decisions = build_sample.build_sample_run(tmp_path / "sample")
    assert rebuilt_source_view(fresh) == rebuilt_source_view(sample)
    committed = [json.loads(line) for line in before["decisions.jsonl"].splitlines()]
    # Historical human lines are identical. Only code rechecks are recomputed,
    # in memory, by the same production loader used for the fresh build.
    assert [d for d in committed if d["by"] == "person"] == [d for d in decisions if d["by"] == "person"]
    assert all("review_basis" not in d for d in committed if d["action"] == "keep")
    assert bind_decisions_on_load(sample, committed, folder) == bind_decisions_on_load(fresh, decisions, tmp_path / "sample")
    for name, content in before.items():
        assert (folder / name).read_bytes() == content
    # Model steps in this generated sample have not been rewritten by the loader.
    for old, new in zip(sample["rows"], fresh["rows"]):
        assert [t for t in old["trail"] if t["tag"] == "Model"] == [t for t in new["trail"] if t["tag"] == "Model"]


def test_sample_unreadable_row_without_box_uses_the_no_place_reason(sample):
    row = next(row for row in sample["rows"] if row["id"] == "e9")
    selector = next(check for check in row["checks"] if check["name"] == "selector_match")
    assert row["box"] is None
    assert selector["result"] == "could_not_run"
    assert selector["reason"] == "The reader did not propose a place on the page for this value."


def test_sample_is_labelled_sample_everywhere(sample):
    assert "Sample" in sample["run"]["label"]
    assert sample["run"]["sample"] is True
    assert sample["rows"] and all(r["sample"] is True for r in sample["rows"])
    for s in sample["sources"]:
        if s["id"] != build_run.IONS_SOURCE_ID:
            assert s["title"].startswith("Sample source")
            assert s["publisher"].startswith("Sample source")
            assert s["url"] is None


def test_sample_exercises_every_group_status_result_tag_and_catcher(sample):
    rows = sample["rows"]
    assert len(rows) >= 8
    assert {r["group"] for r in rows} == set(GROUPS)
    assert {r["status"] for r in rows} == {"matched", "proposed", "failed", "unreadable", "undecided"}
    assert {ch["result"] for r in rows for ch in r["checks"]} == RESULTS
    assert {t["tag"] for r in rows for t in r["trail"]} == {"Model", "Code", "Person"}
    assert {m["caught_by"] for m in sample["mistakes"]} == {"code", "second_reader", "person"}


def test_sample_house_rule_removal(sample):
    kinds = {s["id"]: s["kind"] for s in sample["sources"]}
    removed = [r for r in sample["rows"] if r["group"] == "removed"]
    assert any(
        kinds[r["source_id"]] == "third_party_calendar"
        and "third-party calendar is never a source on its own" in r["reason"]
        and any(c["name"] == "source_rule" and c["result"] == "fail" for c in r["checks"])
        for r in removed
    )


def test_house_rule_removal_is_an_exclusion_not_a_mistake(sample):
    assert [(x["target"], x["rule"], x["by"]) for x in sample["exclusions"]] == \
        [("e6", "third_party_calendar_only", "code")]
    assert "e6" not in {m["target"] for m in sample["mistakes"]}
    rows = {r["id"]: r for r in sample["rows"]}
    for x in sample["exclusions"]:
        assert rows[x["target"]]["group"] == "removed"
        assert x["reason"] == rows[x["target"]]["reason"]
    assert not {m["target"] for m in sample["mistakes"]} & {x["target"] for x in sample["exclusions"]}


def test_schema_rejects_an_unknown_exclusion_rule(sample):
    bad = copy.deepcopy(sample)
    bad["exclusions"][0]["rule"] = "looked_odd"
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))
    bad = copy.deepcopy(sample)
    del bad["exclusions"]
    assert list(Draft202012Validator(SCHEMA).iter_errors(bad))


def test_sample_has_a_period_row(sample):
    row = next(r for r in sample["rows"] if r["date_text"] == "Q3 2027")
    assert (row["date"], row["date_end"]) == ("2027-07-01", "2027-09-30")
    assert row["status"] == "matched"
    assert next(c for c in row["checks"] if c["name"] == "date_match")["result"] == "pass"
    for r in sample["rows"]:
        if r.get("date_end"):
            assert r["date"] and r["date"] <= r["date_end"]


def test_sample_counts(sample):
    groups = {g: sum(r["group"] == g for r in sample["rows"]) for g in GROUPS}
    assert groups == {"kept": 3, "corrected": 3, "removed": 1, "unverified": 2, "open": 2}


# ---------------------------------------------------------------- words on screen

BANNED = [re.compile(r"verif", re.I), re.compile(r"prove", re.I), re.compile(r"\bcorrect\b", re.I)]


def screen_text(run):
    """Every label, reason and trail line the screens show. Instructions and quotes are excluded
    (they are copied from elsewhere), as are machine ids such as the group id "unverified"."""
    yield run["run"]["label"]
    for _, ch in all_checks(run):
        yield ch["label"]
        yield ch["reason"]
    for t in run["tables"]:
        yield t["model"]["name"]
    for r in run["rows"]:
        yield r["reason"]
        for step in r["trail"]:
            yield step["step"]
            if not step["step"].startswith("Instructions sent"):     # the exact instructions sent are not screen words
                yield step["text"]
    for m in run["mistakes"]:
        yield m["what_changed"]
    for x in run["exclusions"]:
        yield x["reason"]
    for s in run["sources"]:
        yield s["title"]


@pytest.mark.parametrize("name", ["demo", "sample", "ptgx-r4a"])
def test_no_banned_words_on_screen(name):
    for text in screen_text(load(name)):
        for pat in BANNED:
            assert not pat.search(text), (pat.pattern, text)


def test_group_label_corrected_is_allowed_but_correct_is_not():
    assert not BANNED[2].search("corrected")
    assert BANNED[2].search("Correct")


def test_state_reasons_have_no_banned_words(sample):
    ds = [
        {"at": "t", "target": "e1", "action": "keep", "note": None, "fix": None, "by": "person"},
        {"at": "t", "target": "e2", "action": "remove", "note": None, "fix": None, "by": "person"},
        {"at": "t", "target": "e3", "action": "fix", "note": None,
         "fix": {"field": "title", "from": "FDA target action date", "to": "FDA action date"}, "by": "person"},
    ]
    st = current_state(sample, ds)
    for t in ("e1", "e2", "e3"):
        for pat in BANNED:
            assert not pat.search(st["targets"][t]["reason"])


# ---------------------------------------------------------------- state rule


def dec(target, action, note=None, fix=None, at="2026-10-27T16:00:00+00:00"):
    return {"at": at, "target": target, "action": action, "note": note, "fix": fix, "by": "person"}


def assert_review_rows(st, excluded):
    """GROUP-AXIS-001: verify each sample proposal, not just a replacement total. CALENDAR-RULE-001: the rows that
    need attention are exactly the undecided ones with issues; the clean ones are placed automatically; none is
    accepted."""
    expected = {"e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8", "e9", "e10", "e11"} - set(excluded)
    rows = {key: target for key, target in st["targets"].items() if target["kind"] == "row"}
    with_issues = {key for key in expected if rows[key]["issues"]}
    assert {key for key, target in rows.items() if target["needs_attention"]} == with_issues
    assert st["unresolved"] == len(with_issues)
    for key in expected:
        assert not rows[key]["accepted"]
        assert rows[key]["on_calendar"] == (not rows[key]["issues"])


def test_no_decisions_preserves_mechanics_but_requires_review_for_each_proposal(sample):
    st = current_state(sample, [])
    for r in sample["rows"]:
        assert st["targets"][r["id"]]["group"] == r["group"]
        assert st["targets"][r["id"]]["needs_recheck"] is False
    assert st["counts"]["rows"] == {"kept": 3, "corrected": 3, "removed": 1, "unverified": 2, "open": 2}
    assert st["counts"]["cells"] == {"kept": 69, "corrected": 0, "removed": 0, "unverified": 0, "open": 0}
    assert st["mistakes"] == {"code": 1, "second_reader": 1, "person": 1}
    assert st["excluded"] == {"code": 1, "person": 0}
    assert st["counts"]["excluded_by_person"] == 0
    assert [(x["target"], x["rule"], x["by"]) for x in st["exclusions"]] == [("e6", "third_party_calendar_only", "code")]
    # GROUP-AXIS-001: the old four mechanically unsettled rows remain recorded;
    # every nonexcluded proposal now needs semantic review, even if checks pass.
    recorded = json.loads((RUNS / "sample" / "run.json").read_text())
    assert sum(r["group"] in {"open", "unverified"} for r in recorded["rows"]) == 4
    # CALENDAR-RULE-001: only the rows with issues need attention; the clean ones are placed automatically.
    expected_attention = {"e1": False, "e2": False, "e3": False, "e4": True, "e5": False,
                          "e6": False, "e7": True, "e8": False, "e9": True, "e10": False, "e11": False}
    for rid, attention in expected_attention.items():
        target = st["targets"][rid]
        assert target["needs_attention"] == attention, (rid, target["issues"])
        assert not target["accepted"]
        assert target["on_calendar"] == (rid != "e6" and not attention)
    assert st["unresolved"] == sum(expected_attention.values())
    assert all(t["decided_by_person"] is False for t in st["targets"].values())
    assert st["ignored"] == []


def test_keep(sample):
    st = current_state(sample, [dec("e8", "keep")])
    assert st["targets"]["e8"]["group"] == "kept"
    assert st["counts"]["rows"]["kept"] == 4 and st["counts"]["rows"]["open"] == 1
    assert st["mistakes"]["person"] == 1


def test_fix_records_before_and_after_and_needs_recheck(sample):
    st = current_state(sample, [dec("e7", "fix", fix={"field": "date", "from": "2027-07-09", "to": "2027-07-19"})])
    t = st["targets"]["e7"]
    assert t["group"] == "corrected"
    assert t["values"]["date"] == "2027-07-19"
    assert t["before"] == {"date": "2027-07-09"}
    assert t["fixes"][0]["from"] == "2027-07-09" and t["fixes"][0]["to"] == "2027-07-19"
    assert t["needs_recheck"] is True and t["recheck"] is None
    assert st["mistakes"]["person"] == 1  # not counted until a re-check passes
    assert st["counts"]["fixes_pending"] == 1


def test_fix_on_a_table_value(sample):
    st = current_state(sample, [dec("t1.0.0", "fix", fix={"field": "value", "from": "115,315", "to": "115,316"})])
    assert st["targets"]["t1.0.0"]["group"] == "corrected"
    assert st["targets"]["t1.0.0"]["needs_recheck"] is True
    assert st["counts"]["cells"]["corrected"] == 1 and st["counts"]["cells"]["kept"] == 68


def test_remove_uses_the_note_as_reason(sample):
    st = current_state(sample, [dec("e10", "remove", note="Not an event.")])
    assert st["targets"]["e10"]["group"] == "removed"
    assert st["targets"]["e10"]["reason"] == "Not an event."
    assert st["mistakes"]["person"] == 1
    assert st["counts"]["excluded_by_person"] == 1 and st["excluded"] == {"code": 1, "person": 1}
    assert {"target": "e10", "rule": "person_removed", "reason": "Not an event.", "by": "person"} in st["exclusions"]
    assert_review_rows(st, {"e6", "e10"})


@pytest.mark.parametrize("target, status", [("e7", "failed"), ("e8", "undecided"), ("e10", "proposed")])
def test_keep_is_a_choice_not_a_check_result(sample, target, status):
    st = current_state(sample, [dec(target, "keep", note="Keep it.")])
    t = st["targets"][target]
    assert t["group"] == "kept"
    assert t["status"] == status
    assert t["decided_by_person"] is True and t["last_action"] == "keep"
    row = next(r for r in sample["rows"] if r["id"] == target)
    assert row["status"] == status  # the run itself is not touched


def test_only_fixes_in_effect_are_person_mistakes_and_undo_lowers_them(sample):
    fix = dec("e7", "fix", fix={"field": "date", "from": "2027-07-09", "to": "2027-07-19"})
    assert current_state(sample, [fix, recheck("e7", "pass")])["mistakes"]["person"] == 2
    assert current_state(sample, [fix, recheck("e7", "pass"), dec("e7", "undo")])["mistakes"]["person"] == 1
    assert current_state(sample, [dec("e7", "keep")])["mistakes"]["person"] == 1
    same = dec("e7", "fix", fix={"field": "date", "from": "2027-07-09", "to": "2027-07-09"})
    st = current_state(sample, [same])
    assert st["mistakes"]["person"] == 1 and len(st["ignored"]) == 1


def recheck(target, result, at="2026-10-27T16:05:00+00:00", by="code", before="fail", updates=None):
    """A state-test re-check whose before/after check outputs are bound to their exact candidate and page."""
    run = load("sample")
    row = copy.deepcopy(next(r for r in run["rows"] if r["id"] == target))
    source = next(s for s in run["sources"] if s["id"] == row["source_id"])
    page = next(p for p in source["pages"] if p["page_number"] == row["page_number"])
    words, artifact = sources.load_page_evidence(RUNS / "sample", page)
    after = copy.deepcopy(row)
    if target == "e7":
        after[E7_FIX["field"]] = E7_FIX["to"]
    elif target == "e8":
        after.update(event_type="regulatory_submission")
    after.update(updates or {})

    field = "date" if target == "e7" else "event_type"

    def bound(candidate, overall, chosen=False):
        checks = recheck_module.row_checks(candidate, source, words, chosen=chosen,
                                           consumed_artifact=artifact)
        applicable = [c for c in checks if c.result != "not_needed"]
        if overall == "pass":
            outcomes = {c.check: "pass" for c in applicable}
        else:
            outcomes = {c.check: "pass" for c in applicable}
            if applicable:
                outcomes[applicable[0].check] = "fail" if overall == "fail" else "could_not_run"
        checks = [replace(c, result=outcomes.get(c.check, c.result),
                          reason="Synthetic, current check result for the state rule test.") for c in checks]
        return [x.to_dict() for x in bind_checks(checks, candidate, source, consumed_artifact=artifact)]

    old_checks = bound(row, before)
    after_checks = bound(after, result, chosen=field == "date")
    checks = after_checks
    return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": by,
            "recheck": {"result": result, "reason": f"The checks were run again and the result is {result}.",
                        "checks": checks,
                        "before": {"result": before, "checks": old_checks},
                        "after": {"result": result, "checks": after_checks}}}


E7_FIX = {"field": "date", "from": "2027-07-09", "to": "2027-07-19"}


def test_a_fix_waiting_for_its_recheck_is_pending_not_a_mistake(sample):
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX)])
    t = st["targets"]["e7"]
    assert t["needs_recheck"] is True and t["recheck"] is None and t["fixes"][0]["recheck"] is None
    assert t["group"] == "corrected"
    assert st["mistakes"]["person"] == 1 and st["counts"]["fixes_pending"] == 1


def test_a_fix_whose_recheck_passed_counts_as_a_person_mistake(sample):
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass")])
    t = st["targets"]["e7"]
    assert t["needs_recheck"] is False and t["recheck"]["result"] == "pass"
    assert {c["check"] for c in t["recheck"]["checks"]} == {
        "quote_match", "selector_match", "date_match", "source_rule", "type_match"}
    assert all(c.get("assessment") for c in t["recheck"]["checks"])
    assert t["group"] == "corrected" and t["status"] == "matched"
    assert st["mistakes"]["person"] == 2 and st["counts"]["fixes_pending"] == 0
    assert st["ignored"] == []


def test_a_fix_whose_recheck_failed_is_not_counted_and_goes_back_to_open(sample):
    st = current_state(sample, [dec("e8", "fix", fix={"field": "event_type", "from": "regulatory_decision",
                                                          "to": "regulatory_submission"}),
                                recheck("e8", "fail")])
    t = st["targets"]["e8"]
    assert t["group"] == "open" and t["status"] == "failed"
    assert t["values"]["event_type"] == "regulatory_submission"
    assert t["needs_recheck"] is False and t["recheck"]["result"] == "fail"
    assert st["mistakes"]["person"] == 1 and st["counts"]["fixes_pending"] == 0
    assert_review_rows(st, {"e6"})
    row = next(r for r in sample["rows"] if r["id"] == "e8")
    assert row["status"] == "undecided"  # the run itself is not touched


def test_a_fix_whose_recheck_could_not_run_is_pending_and_needs_attention(sample):
    base = current_state(sample, [])
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "could_not_run")])
    t = st["targets"]["e7"]
    assert (t["group"], t["status"]) == ("corrected", "proposed") and t["decided_by_person"]
    assert t["recheck"]["result"] == "could_not_run" and t["needs_recheck"] is False
    assert st["mistakes"] == base["mistakes"] and st["counts"]["fixes_pending"] == 1
    assert st["unresolved"] == base["unresolved"]          # e7 was open; it still needs attention
    assert st["ignored"] == []
    undone = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "could_not_run"), dec("e7", "undo")])
    assert undone["counts"] == base["counts"] and undone["unresolved"] == base["unresolved"]


def test_undo_of_a_fix_takes_its_recheck_back_too(sample):
    for result in ("pass", "fail", "could_not_run"):
        st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", result), dec("e7", "undo")])
        t = st["targets"]["e7"]
        assert t["recheck"] is None and t["needs_recheck"] is False and t["fixes"] == []
        assert (t["group"], t["status"], t["values"]["date"]) == ("open", "failed", "2027-07-09")
        assert st["mistakes"]["person"] == 1 and st["counts"]["fixes_pending"] == 0
        assert st["ignored"] == []


def test_a_recheck_applies_to_the_latest_fix_only(sample):
    fix2 = {"field": "quote", "from": "Samplebio Six will report final results on July 9, 2027.",
            "to": "Samplebio Six will report final results on July 19, 2027."}
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass"),
                                dec("e7", "fix", fix=fix2)])
    t = st["targets"]["e7"]
    assert t["needs_recheck"] is True and t["recheck"] is None
    assert st["mistakes"]["person"] == 2 and st["counts"]["fixes_pending"] == 1
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass"),
                                dec("e7", "fix", fix=fix2), recheck("e7", "pass", at="2026-10-27T16:06:00+00:00",
                                                                      updates={"quote": fix2["to"]})])
    assert st["mistakes"]["person"] == 3 and st["counts"]["fixes_pending"] == 0


def test_rechecks_that_cannot_apply_are_ignored(sample):
    ds = [
        recheck("e7", "pass"),                                   # no fix waiting
        recheck("e7", "maybe"),                                  # bad result
        {**recheck("e7", "pass"), "by": "person"},               # a person cannot write a re-check
        dec("e7", "fix", fix=E7_FIX),
        recheck("e7", "pass"),
        recheck("e7", "fail"),                                   # that fix already has its re-check
    ]
    st = current_state(sample, ds)
    assert [i["index"] for i in st["ignored"]] == [0, 1, 2, 5]
    assert all(i["reason"] for i in st["ignored"])
    assert st["targets"]["e7"]["recheck"]["result"] == "pass"


def test_recheck_with_duplicate_event_check_is_refused(sample):
    line = recheck("e7", "pass")
    complete = line["recheck"]["after"]["checks"]
    incomplete = complete[:-1] + [complete[0]]
    line["recheck"]["checks"] = incomplete
    line["recheck"]["after"]["checks"] = incomplete

    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), line])

    assert st["targets"]["e7"]["needs_recheck"] is True
    assert st["targets"]["e7"]["recheck"] is None
    assert st["ignored"][-1]["reason"] == (
        "The saved re-check is unbound or stale; it remains history."
    )


def test_date_migration_skips_only_human_decisions_still_in_effect(sample):
    from runs.recheck_dates import person_decisions_in_effect

    assert person_decisions_in_effect(sample, [dec("e7", "keep")]) == {"e7"}
    assert person_decisions_in_effect(sample, [dec("e7", "keep"), dec("e7", "undo")]) == set()


def test_decision_schema_allows_a_code_recheck_only(sample):
    v = Draft202012Validator(DECISION_SCHEMA)
    assert not list(v.iter_errors(recheck("e7", "pass")))
    assert list(v.iter_errors({**recheck("e7", "pass"), "by": "person"}))
    assert list(v.iter_errors({**recheck("e7", "maybe")}))
    assert list(v.iter_errors({**dec("e7", "keep"), "by": "code"}))
    no_rc = recheck("e7", "pass")
    del no_rc["recheck"]
    assert list(v.iter_errors(no_rc))


def test_undo_of_a_remove_lowers_excluded_by_person(sample):
    st = current_state(sample, [dec("e8", "remove", note="no"), dec("e8", "undo")])
    assert st["counts"]["excluded_by_person"] == 0 and st["excluded"]["person"] == 0
    assert st["mistakes"]["person"] == 1


def test_a_person_can_take_a_row_out_of_a_code_exclusion(sample):
    st = current_state(sample, [dec("e6", "keep", note="Backed by the company's own slide.")])
    assert st["targets"]["e6"]["group"] == "kept" and st["targets"]["e6"]["status"] == "failed"
    assert st["excluded"] == {"code": 0, "person": 0}
    st = current_state(sample, [dec("e6", "remove", note="Calendar only.")])
    assert st["excluded"] == {"code": 0, "person": 1}


def test_unresolved_counts_every_unaccepted_proposal_including_legacy_keeps(sample):
    st = current_state(sample, [dec("t1.0.0", "fix", fix={"field": "value", "from": "115,315", "to": "115,316"})])
    assert_review_rows(st, {"e6"})
    st = current_state(sample, [dec("e7", "keep"), dec("e9", "remove")])
    # This old unbound Keep remains history; it does not accept e7.
    assert st["targets"]["e7"]["review_history"]
    assert_review_rows(st, {"e6", "e9"})


def test_undo_restores_the_state_before_the_last_decision(sample):
    ds = [dec("e8", "keep"), dec("e8", "remove", note="no"), dec("e8", "undo")]
    t = current_state(sample, ds)["targets"]["e8"]
    assert t["group"] == "kept"
    assert current_state(sample, ds)["mistakes"]["person"] == 1


def test_undo_twice_goes_back_to_the_run(sample):
    ds = [dec("e8", "keep"), dec("e8", "remove", note="no"), dec("e8", "undo"), dec("e8", "undo")]
    st = current_state(sample, ds)
    assert st["targets"]["e8"]["group"] == "open"
    assert st["targets"]["e8"]["reason"] == (
        "The two readers named different event types. Waiting for you. "
        "No issues were found, so it was placed automatically; it was not reviewed by you.")
    assert not st["targets"]["e8"]["accepted"] and st["targets"]["e8"]["inclusion"] != "kept"
    assert st["ignored"] == []


def test_undo_of_a_fix_restores_the_value(sample):
    ds = [dec("e7", "fix", fix={"field": "date", "from": "2027-07-09", "to": "2027-07-19"}), dec("e7", "undo")]
    t = current_state(sample, ds)["targets"]["e7"]
    assert t["values"]["date"] == "2027-07-09" and t["group"] == "open" and t["needs_recheck"] is False


def test_undo_only_touches_its_own_target(sample):
    st = current_state(sample, [dec("e8", "keep"), dec("e10", "remove"), dec("e8", "undo")])
    assert st["targets"]["e8"]["group"] == "open"
    assert st["targets"]["e10"]["group"] == "removed"


def test_ignored_decisions_change_nothing_and_say_why(sample):
    ds = [
        dec("nope", "keep"),
        dec("e1", "undo"),
        dec("e1", "shout"),
        {**dec("e1", "keep"), "by": "model"},
        dec("e7", "fix", fix={"field": "date", "from": "2000-01-01", "to": "2027-07-19"}),
        dec("e7", "fix", fix={"field": "group", "from": "open", "to": "kept"}),
        dec("e7", "fix", fix=None),
        "not a dict",
    ]
    st = current_state(sample, ds)
    assert [i["index"] for i in st["ignored"]] == list(range(len(ds)))
    assert all(i["reason"] for i in st["ignored"])
    assert "nope" in st["ignored"][0]["reason"]
    assert st["counts"] == current_state(sample, [])["counts"]
    assert st["mistakes"] == current_state(sample, [])["mistakes"]


def test_sample_decisions_file(sample, sample_decisions):
    st = current_state(sample, sample_decisions)
    assert st["ignored"] == []
    assert st["targets"]["e8"]["group"] == "kept"
    assert st["targets"]["e7"]["group"] == "kept"
    assert st["targets"]["e7"]["status"] == "failed" and st["targets"]["e7"]["decided_by_person"] is True
    assert st["targets"]["e8"]["status"] == "undecided" and st["targets"]["e8"]["decided_by_person"] is True
    assert st["targets"]["e7"]["values"]["date"] == "2027-07-09"
    assert st["targets"]["e7"]["recheck"] is None and st["counts"]["fixes_pending"] == 0
    rc = [d for d in sample_decisions if d["action"] == "recheck"]
    assert [(d["target"], d["by"], d["recheck"]["result"]) for d in rc] == [("e7", "code", "fail")]
    before_undo = current_state(sample, sample_decisions[:3])["targets"]["e7"]
    assert (before_undo["group"], before_undo["status"]) == ("open", "failed")
    assert st["targets"]["e10"]["group"] == "removed"
    assert st["mistakes"] == {"code": 1, "second_reader": 1, "person": 1}
    assert st["excluded"] == {"code": 1, "person": 1}
    assert st["counts"]["excluded_by_person"] == 1
    # Historical decisions and recorded checks above are unchanged; unbound Keeps are not current acceptance.
    assert_review_rows(st, {"e6", "e10"})


def test_state_does_not_change_its_inputs(sample, sample_decisions):
    before = copy.deepcopy((sample, sample_decisions))
    current_state(sample, sample_decisions)
    assert (sample, sample_decisions) == before


# ---------------------------------------------------------------- event checks


# date_match needs the row's event to find the clause that dates it (Codex PR9 review, Fable's ruling 2026-10-28).
ROW_EVENT = {"event_type": "trial_readout", "title": "Phase 3 topline results"}


def _dm(*args, **kwargs):
    return event_checks.date_match(*args, **{**ROW_EVENT, **kwargs})


def test_date_match_cases():
    q = "Results are expected on March 12, 2027."
    assert _dm("March 12, 2027", "2027-03-12", q).result == "pass"
    assert _dm("March 12, 2027", "2027-03-21", q).result == "fail"
    assert _dm("March 13, 2027", "2027-03-13", q).result == "fail"
    assert _dm(None, None, q).result == "could_not_run"
    assert _dm("later this year", None, "data later this year").result == "not_needed"
    assert _dm("later this year", "2026-12-31", "data later this year").result == "fail"
    assert _dm("March 12, 2027", None, q).result == "fail"
    assert _dm("March 12, 2027", "2027-03-12", q, date_end="2027-03-31").result == "fail"


@pytest.mark.parametrize("text, start, end", [
    ("Q3 2027", "2027-07-01", "2027-09-30"),
    ("3Q 2027", "2027-07-01", "2027-09-30"),
    ("third quarter of 2027", "2027-07-01", "2027-09-30"),
    ("Q4 2027", "2027-10-01", "2027-12-31"),
    ("2H 2027", "2027-07-01", "2027-12-31"),
    ("H1 2027", "2027-01-01", "2027-06-30"),
    ("second half of 2027", "2027-07-01", "2027-12-31"),
    ("February 2028", "2028-02-01", "2028-02-29"),
    ("2027", "2027-01-01", "2027-12-31"),
])
def test_named_periods_have_fixed_first_and_last_days(text, start, end):
    s, e = event_checks.parse_period(text)
    assert (s.isoformat(), e.isoformat()) == (start, end)
    quote = f"Data are expected in {text}."
    assert _dm(text, start, quote, date_end=end).result == "pass"
    assert _dm(text, None, quote).result == "pass"
    assert _dm(text, start, quote, date_end="2030-01-01").result == "fail"
    assert _dm(text, start, quote).result == "fail"
    assert _dm(text, start, "Data are expected soon.", date_end=end).result == "fail"


def test_vague_periods_check_only_the_words():
    q = "The company expects data in mid-2027."
    assert event_checks.parse_period("mid-2027") == (None, None)
    assert _dm("mid-2027", None, q).result == "pass"
    assert _dm("mid-2027", "2027-05-01", q, date_end="2027-08-31").result == "pass"
    assert _dm("mid-2027", "2027-09-01", q, date_end="2027-08-31").result == "fail"
    assert _dm("mid-2027", None, "The company expects data soon.").result == "fail"
    assert event_checks.vague_period_year("mid-2027") == 2027
    assert event_checks.vague_period_year("later this year") is None
    assert event_checks.parse_period("later this year") is None
    assert event_checks.parse_period("March 12, 2027") is None


@pytest.mark.parametrize("text, start, end", [
    ("early 2027", "2027-01-01", "2027-04-30"),
    ("mid-2027", "2027-05-01", "2027-08-31"),
    ("late 2027", "2027-09-01", "2027-12-31"),
    ("end of 2027", "2027-09-01", "2027-12-31"),
])
def test_vague_periods_need_dates_inside_their_window(text, start, end):
    """early = January to April, mid = May to August, late / end of = September to December.
    Changed in R3b (Red Team round 1): whole-year dates no longer pass for a vague period."""
    q = f"The company expects data in {text}."
    assert _dm(text, start, q, date_end=end).result == "pass"
    assert _dm(text, start, q, date_end=start).result == "pass"
    for s_, e_ in [("2027-01-01", "2027-12-31"), ("2026-12-01", "2027-03-31"), ("2027-10-01", "2028-01-31"),
                   ("2028-01-01", "2028-06-30"), ("2026-01-01", "2026-06-30")]:
        if (s_, e_) == (start, end):
            continue
        if start <= s_ <= e_ <= end:
            continue
        r = _dm(text, s_, q, date_end=e_)
        assert r.result == "fail", (s_, e_)
        assert "2027" in r.reason
    assert _dm(text, None, q).result == "pass"


def test_vague_period_with_no_year_keeps_the_ordered_dates_rule(monkeypatch):
    monkeypatch.setattr(event_checks, "parse_period", lambda t: (None, None))
    q = "The company expects data around then."
    assert event_checks.vague_period_year("around then") is None
    assert _dm("around then", "2026-05-01", q, date_end="2029-08-31").result == "pass"
    assert _dm("around then", "2029-09-01", q, date_end="2026-08-31").result == "fail"


def test_source_rule_cases():
    assert event_checks.source_rule("filing").result == "pass"
    assert event_checks.source_rule("third_party_calendar").result == "fail"
    assert event_checks.source_rule("third_party_calendar", ["third_party_calendar"]).result == "fail"
    assert event_checks.source_rule("third_party_calendar", ["company_release"]).result == "pass"


def test_quote_match_cases(words):
    assert event_checks.quote_match("Year Ended December 31, 2025", words).result == "pass"
    assert event_checks.quote_match("Year Ended December 31, 2026", words).result == "fail"
    assert event_checks.quote_match("anything", []).result == "could_not_run"
    assert event_checks.quote_match(None, words).result == "could_not_run"


def test_row_status_rules():
    complete = [{"name": name, "result": "pass"} for name in event_checks.REQUIRED_EVENT_CHECKS]
    complete[-1]["result"] = "not_needed"
    assert event_checks.row_status(complete) == "matched"
    assert event_checks.row_status(complete[:-1]) == event_checks.PROPOSED
    assert event_checks.row_status(complete[:-1] + [complete[0]]) == event_checks.PROPOSED
    assert event_checks.row_status([{"name": "source_rule", "result": "pass"}]) == event_checks.PROPOSED
    malformed = [dict(item) for item in complete]
    malformed[0].pop("result")
    assert event_checks.row_status(malformed) == event_checks.PROPOSED
    malformed = [dict(item) for item in complete]
    malformed[0]["name"] = []
    assert event_checks.row_status(malformed) == event_checks.PROPOSED
    partial_failure = complete[:-1] + [{"name": complete[0]["name"], "result": "fail"}]
    assert event_checks.row_status(partial_failure) == event_checks.FAILED
    complete[-1]["result"] = "could_not_run"
    assert event_checks.row_status(complete) == "proposed"
    complete[-1]["result"] = "fail"
    assert event_checks.row_status(complete, second_reader_agrees=False) == "failed"
    complete[-1]["result"] = "pass"
    assert event_checks.row_status(complete, second_reader_agrees=False) == "undecided"
    assert event_checks.row_status(complete, readable=False) == "unreadable"


def test_event_check_completeness_rejects_unhashable_result():
    checks = [{"name": name, "result": "pass"} for name in event_checks.REQUIRED_EVENT_CHECKS]
    checks[0]["result"] = []
    assert event_checks.row_status(checks) == event_checks.PROPOSED


def test_row_status_refuses_each_incomplete_required_event_check():
    complete = [{"name": name, "result": "pass"} for name in event_checks.REQUIRED_EVENT_CHECKS]
    assert event_checks.row_status(complete) == event_checks.MATCHED
    for missing in event_checks.REQUIRED_EVENT_CHECKS:
        incomplete = [check for check in complete if check["name"] != missing]
        assert event_checks.row_status(incomplete) == event_checks.PROPOSED, missing


def test_a_positive_failure_stays_a_failure_in_an_incomplete_event_set():
    from runs.escalate import budgeted, candidates
    from runs.state import derive_status

    incomplete = [{"name": event_checks.TYPE_ID, "label": event_checks.TYPE_LABEL, "result": "fail",
                   "reason": "Only one check was saved."}]
    row = {"id": "e1", "status": event_checks.MATCHED, "group": "kept", "checks": incomplete}
    assert derive_status(row)[:2] == (event_checks.FAILED, "open")
    assert [item["id"] for item in candidates([row])] == ["e1"]
    assert budgeted([row]) == []  # retired semantic type failures never trigger a paid date repair

    complete = [{"name": name, "label": label, "result": "pass", "reason": "fixture"}
                for name, label in ((event_checks.QUOTE_ID, event_checks.QUOTE_LABEL),
                                    (event_checks.SELECTOR_ID, "Citation located"),
                                    (event_checks.DATE_ID, event_checks.DATE_LABEL),
                                    (event_checks.SOURCE_ID, event_checks.SOURCE_LABEL),
                                    (event_checks.TYPE_ID, event_checks.TYPE_LABEL))]
    complete[-1]["result"] = "fail"
    row["checks"] = complete
    row["status"], row["group"] = event_checks.FAILED, "open"
    assert [item["id"] for item in candidates([row])] == ["e1"]
    assert budgeted([row]) == []  # retired semantic type failures never trigger a paid date repair

    # Preserve the active positive control: a literal date failure is still repairable.
    row["checks"] = [{"name": event_checks.DATE_ID, "label": event_checks.DATE_LABEL,
                      "result": "fail", "reason": "Printed day differs."}]
    assert derive_status(row)[:2] == (event_checks.FAILED, "open")
    assert [item["id"] for item in budgeted([row])] == ["e1"]


def test_a_pass_on_a_field_no_check_compares_is_not_a_person_mistake(sample):
    # Codex PR4 #1: a title is compared by no check, so even a passing re-check does not show a mistake.
    fix2 = {"field": "title", "from": "Final results", "to": "Final trial results"}
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass"),
                                dec("e7", "fix", fix=fix2), recheck("e7", "pass", at="2026-10-27T16:06:00+00:00",
                                                                      updates={"title": fix2["to"]})])
    assert st["mistakes"]["person"] == 2 and st["counts"]["fixes_pending"] == 1
    assert [f["checked"] for f in st["targets"]["e7"]["fixes"]] == [True, False]


# ---------------------------------------------------------------- before and after: a demonstrated mistake


def test_a_fix_counts_only_when_the_old_value_failed(sample):
    base = current_state(sample, [])
    wrong = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass", before="fail")])
    assert wrong["mistakes"]["person"] == base["mistakes"]["person"] + 1
    assert wrong["counts"]["edits_without_error"] == 0
    same = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass", before="pass")])
    assert same["mistakes"] == base["mistakes"] and same["counts"]["edits_without_error"] == 1
    t = same["targets"]["e7"]
    assert t["group"] == "corrected" and t["status"] == "matched"
    assert t["fixes"][-1]["not_counted_reason"] == "The value already passed its checks before the edit."


def test_before_could_not_run_and_after_pass_is_not_counted_and_flagged(sample):
    base = current_state(sample, [])
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass", before="could_not_run")])
    assert st["mistakes"] == base["mistakes"]
    f = st["targets"]["e7"]["fixes"][-1]
    assert f["mistake"] is False and f["before_undemonstrated"] is True
    assert f["not_counted_reason"] == "The old value could not be assessed; no mistake was demonstrated."
    assert st["targets"]["e7"]["reason"].endswith(
        f["not_counted_reason"] + " No issues were found, so it was placed automatically; it was not reviewed by you.")
    # CALENDAR-RULE-001: a fix whose re-check passed leaves no issue; it is placed automatically, never accepted.
    assert not st["targets"]["e7"]["needs_attention"] and not st["targets"]["e7"]["accepted"]
    assert st["targets"]["e7"]["decision_label"] == "Fixed by you — not yet accepted"
    assert f["recheck"]["before"]["result"] == "could_not_run"
    assert f["recheck"]["after"]["result"] == "pass"
    assert st["counts"]["edits_without_error"] == 0
    undone = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass", before="could_not_run"),
                                    dec("e7", "undo")])
    assert undone["mistakes"] == base["mistakes"] and undone["counts"] == base["counts"]


def test_a_recheck_without_a_before_run_is_not_counted(sample):
    base = current_state(sample, [])
    rc = recheck("e7", "pass")
    del rc["recheck"]["before"], rc["recheck"]["after"]
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), rc])
    assert st["mistakes"] == base["mistakes"] and st["counts"]["edits_without_error"] == 0
    assert st["targets"]["e7"]["status"] == "matched"


def test_a_failing_recheck_is_never_counted_whatever_before_said(sample):
    base = current_state(sample, [])
    for before in ("pass", "fail", "could_not_run"):
        st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "fail", before=before)])
        assert st["mistakes"] == base["mistakes"] and st["counts"]["edits_without_error"] == 0


def test_undo_of_an_edit_without_error_lowers_edits_without_error(sample):
    ds = [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass", before="pass")]
    assert current_state(sample, ds)["counts"]["edits_without_error"] == 1
    assert current_state(sample, ds + [dec("e7", "undo")])["counts"]["edits_without_error"] == 0


def test_decision_schema_requires_before_and_after_on_a_recheck():
    v = Draft202012Validator(DECISION_SCHEMA)
    for key in ("before", "after"):
        rc = recheck("e7", "pass")
        del rc["recheck"][key]
        assert list(v.iter_errors(rc)), key
    rc = recheck("e7", "pass")
    rc["recheck"]["before"]["result"] = "maybe"
    assert list(v.iter_errors(rc))


# ---------------------------------------------------------------- Codex PR9 passages under DATE-LITERAL-001
# Original adversarial sentences remain; code checks wording/bounds, human review owns meaning.

Q3_27, Q4_27 = ("2027-07-01", "2027-09-30"), ("2027-10-01", "2027-12-31")
BOUNDS = [pytest.param(True, id="with-bounds"), pytest.param(False, id="no-bounds")]


def _period(text, bounds, quote, with_bounds, **kw):
    start, end = bounds if with_bounds else (None, None)
    result = _dm(text, start, quote, date_end=end, **kw)
    # DATE-LITERAL-001: a literal pass must never accept any of these adversarial interpretations.
    from tests.test_pivot_proposal_flow import make_run
    from runs.calendar import calendar_entries
    _, run = make_run(("adversarial-date", quote, "proposed study timing", text, start, []),
                      event_updates={"date_end": end})
    state = current_state(run, [])
    from tests.pivot_assertions import assert_never_accepted
    target = assert_never_accepted(run, state)
    assert target["values"]["quote"] == quote
    assert next(c for c in run["rows"][0]["checks"] if c["name"] == "date_match")["result"] == result.result
    return result


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_negated_printed_date_matches_words_but_stays_held(with_bounds):
    r = _period("Q3 2027", Q3_27, "Q3 2027 is no longer the expected readout period.", with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_superseded_and_new_date_words_both_stay_held(with_bounds):
    q = "Q3 2027 was the previous target; results are now expected in Q4 2027."
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_background_year_and_event_period_are_literal_only(with_bounds):
    q = "In 2026 the company announced that results are expected in Q3 2027."
    assert _period("Q3 2027", Q3_27, q, with_bounds).result == "pass"
    r = _period("2026", ("2026-01-01", "2026-12-31"), q, with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_comparison_and_event_period_both_need_review(with_bounds):
    q = "Data in Q3 2027, compared with Q3 2026."
    r = _period("Q3 2026", ("2026-07-01", "2026-09-30"), q, with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason
    assert _period("Q3 2027", Q3_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_two_event_clauses_match_words_without_choosing_event_date(with_bounds):
    q = "Topline data are expected in Q3 2027; results will be presented in Q4 2027."
    for text, bounds in (("Q3 2027", Q3_27), ("Q4 2027", Q4_27)):
        r = _period(text, bounds, q, with_bounds)
        assert r.result == "pass" and "date bounds only" in r.reason, r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_date_words_without_event_words_remain_a_proposal(with_bounds):
    r = _period("Q3 2027", Q3_27, "The company expects Q3 2027.", with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason


def test_codex9_exact_day_still_checks_bounds_and_comparison_stays_held():
    q = "Topline data on March 12, 2027 follow the Q4 2026 results."
    assert _dm("March 12, 2027", "2027-03-12", q).result == "pass"
    assert _dm("March 12, 2027", "2027-03-13", q).result == "fail"
    for with_bounds in (True, False):
        r = _period("Q4 2026", ("2026-10-01", "2026-12-31"), q, with_bounds)
        assert r.result == "pass" and "date bounds only" in r.reason, r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_negation_and_delay_remain_human_readings_of_literal_dates(with_bounds):
    neg = _period("Q3 2027", Q3_27, "Results are not expected in Q3 2027.", with_bounds)
    assert neg.result == "pass" and "date bounds only" in neg.reason
    assert _period("Q3 2027", Q3_27, "Results are expected in Q3 2027.", with_bounds).result == "pass"
    assert _period("Q4 2027", Q4_27, "Results are now expected in Q4 2027.", with_bounds).result == "pass"
    moved = "Results moved from Q3 2027 to Q4 2027."
    assert _period("Q4 2027", Q4_27, moved, with_bounds).result == "could_not_run"
    assert _period("Q3 2027", Q3_27, moved, with_bounds).result == "could_not_run"
    assert _period("Q3 2027", Q3_27, "The Q3 2027 readout has been delayed.", with_bounds).result == "pass"


def test_codex9_mention_order_never_grants_semantic_acceptance():
    # The same two dates in either order give the same answer.
    a = "Data in Q3 2027, compared with Q3 2026."
    b = "Compared with Q3 2026, data in Q3 2027."
    for q in (a, b):
        assert _period("Q3 2027", Q3_27, q, True).result == "pass", q
        assert _period("Q3 2026", ("2026-07-01", "2026-09-30"), q, True).result == "pass", q


def test_literal_date_check_does_not_derive_event_words_from_type_title_or_program():
    # DATE-LITERAL-001 supersedes the old semantic-anchor rule.
    base = event_checks.date_match("Q1 2027", "2027-01-01", "Timing: Q1 2027.", "2027-03-31")
    changed = event_checks.date_match("Q1 2027", "2027-01-01", "Timing: Q1 2027.", "2027-03-31",
                                      event_type="regulatory_decision", title="Investor day", program="samplimab")
    assert changed == base and base.result == "pass"


# ---------------------------------------------------------------- Codex PR9 fix 0fa435e passages: no semantic interpretation


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_denied_move_range_endpoints_stay_unresolved(with_bounds):
    q = "Results will not move from Q3 2027 to Q4 2027."
    for text, bounds in (("Q4 2027", Q4_27), ("Q3 2027", Q3_27)):
        r = _period(text, bounds, q, with_bounds)
        assert r.result == "could_not_run", (text, r.reason)


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_superseded_period_and_new_period_stay_proposed(with_bounds):
    q = "Q3 2027 was the previous target; results are now expected in Q4 2027."
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_now_q4_not_q3_requires_human_interpretation(with_bounds):
    # DATE-LITERAL-001: the quote retains its negation; no comma split certifies either claim.
    q = "Results are now expected in Q4 2027, not Q3 2027."
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "pass" and "date bounds only" in r.reason, r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_affirmed_move_does_not_pass_a_range_endpoint(with_bounds):
    moved = "Results moved from Q3 2027 to Q4 2027."
    assert _period("Q4 2027", Q4_27, moved, with_bounds).result == "could_not_run"
    assert _period("Q3 2027", Q3_27, moved, with_bounds).result == "could_not_run"
