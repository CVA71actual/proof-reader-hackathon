"""R1: the run.json contract, the saved demo run, the sample run, and the state rule."""

import copy
import importlib
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from checks import event_checks
from runs import build_run, build_sample
from runs.state import GROUPS, current_state

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
SCHEMA = json.loads((RUNS / "schema" / "run.schema.json").read_text())
DECISION_SCHEMA = json.loads((RUNS / "schema" / "decision.schema.json").read_text())

RESULTS = {"pass", "fail", "could_not_run", "not_needed"}
RAN = {"pass", "fail"}


def load(name):
    return json.loads((RUNS / name / "run.json").read_text())


@pytest.fixture(scope="module")
def demo():
    return load("demo")


@pytest.fixture(scope="module")
def sample():
    return load("sample")


@pytest.fixture(scope="module")
def sample_decisions():
    lines = (RUNS / "sample" / "decisions.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines if x.strip()]


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


@pytest.mark.parametrize("name", ["demo", "sample"])
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


@pytest.mark.parametrize("name", ["demo", "sample"])
def test_every_check_result_is_allowed(name):
    for _, ch in all_checks(load(name)):
        assert ch["result"] in RESULTS
        assert ch["kind"] == "Code"


@pytest.mark.parametrize("name", ["demo", "sample"])
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


@pytest.mark.parametrize("name", ["demo", "sample"])
def test_code_refs_point_at_real_functions(name):
    for _, ch in all_checks(load(name)):
        ref = ch["code_ref"]
        path = REPO / ref["file"]
        assert path.is_file(), ref
        module = importlib.import_module(ref["file"][:-3].replace("/", "."))
        assert callable(getattr(module, ref["function"], None)), ref
        assert ref["function"] == ch["name"]


@pytest.mark.parametrize("name", ["demo", "sample"])
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


@pytest.mark.parametrize("name", ["demo", "sample"])
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
        first = row["trail"][0]
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


def test_demo_build_is_current(demo, tmp_path):
    reading = json.loads(build_run.read_table.READING_PATH.read_text())
    fresh = build_run.build_demo_run(reading, tmp_path / "demo", finished_at=demo["run"]["finished_at"])
    assert fresh == demo


def test_fake_reading_is_never_labelled_saved_demo(tmp_path):
    from capture import read_table

    fake = read_table.read_fake(read_table.load_words())
    run = build_run.build_demo_run(fake, tmp_path / "practice")
    assert run["run"]["label"] != build_run.REAL_LABEL
    assert "fake" in run["run"]["label"].lower()
    assert "fake" in run["tables"][0]["model"]["name"]
    assert {c["status"] for c in items(run)} == {"matched", "failed", "proposed"}


def test_sample_build_is_current(sample, tmp_path):
    fresh, decisions = build_sample.build_sample_run(tmp_path / "sample")
    assert fresh == sample
    committed = (RUNS / "sample" / "decisions.jsonl").read_text()
    assert committed == "".join(json.dumps(d) + "\n" for d in decisions)


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
        for step in r["trail"][1:]:
            yield step["step"]
            yield step["text"]
        yield r["trail"][0]["step"]
    for m in run["mistakes"]:
        yield m["what_changed"]
    for x in run["exclusions"]:
        yield x["reason"]
    for s in run["sources"]:
        yield s["title"]


@pytest.mark.parametrize("name", ["demo", "sample"])
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


def test_no_decisions_leaves_the_run_as_it_was(sample):
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
    assert st["unresolved"] == 4
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
    assert st["unresolved"] == 3


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


def recheck(target, result, at="2026-10-27T16:05:00+00:00", by="code", before="fail"):
    """A code re-check line. `before`: the result of the same checks on the value before the fix."""
    checks = [{"name": "date_match", "result": result}]
    return {"at": at, "target": target, "action": "recheck", "note": None, "fix": None, "by": by,
            "recheck": {"result": result, "reason": f"The checks were run again and the result is {result}.",
                        "checks": checks,
                        "before": {"result": before, "checks": [{"name": "date_match", "result": before}]},
                        "after": {"result": result, "checks": checks}}}


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
    assert t["recheck"]["checks"] == [{"name": "date_match", "result": "pass"}]
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
    assert st["unresolved"] == 4
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
                                dec("e7", "fix", fix=fix2), recheck("e7", "pass", at="2026-10-27T16:06:00+00:00")])
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


def test_unresolved_counts_open_and_unverified_rows_only(sample):
    st = current_state(sample, [dec("t1.0.0", "fix", fix={"field": "value", "from": "115,315", "to": "115,316"})])
    assert st["unresolved"] == 4
    st = current_state(sample, [dec("e7", "keep"), dec("e9", "remove")])
    assert st["unresolved"] == 2


def test_undo_restores_the_state_before_the_last_decision(sample):
    ds = [dec("e8", "keep"), dec("e8", "remove", note="no"), dec("e8", "undo")]
    t = current_state(sample, ds)["targets"]["e8"]
    assert t["group"] == "kept"
    assert current_state(sample, ds)["mistakes"]["person"] == 1


def test_undo_twice_goes_back_to_the_run(sample):
    ds = [dec("e8", "keep"), dec("e8", "remove", note="no"), dec("e8", "undo"), dec("e8", "undo")]
    st = current_state(sample, ds)
    assert st["targets"]["e8"]["group"] == "open"
    assert st["targets"]["e8"]["reason"] == "The two readers named different event types. Waiting for you."
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
    assert st["unresolved"] == 1


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
    P = {"result": "pass"}
    assert event_checks.row_status([P, {"result": "not_needed"}]) == "matched"
    assert event_checks.row_status([{"result": "not_needed"}]) == "proposed"
    assert event_checks.row_status([P, {"result": "could_not_run"}]) == "proposed"
    assert event_checks.row_status([P, {"result": "fail"}], second_reader_agrees=False) == "failed"
    assert event_checks.row_status([P], second_reader_agrees=False) == "undecided"
    assert event_checks.row_status([P], readable=False) == "unreadable"


def test_a_pass_on_a_field_no_check_compares_is_not_a_person_mistake(sample):
    # Codex PR4 #1: a title is compared by no check, so even a passing re-check does not show a mistake.
    fix2 = {"field": "title", "from": "Final results", "to": "Final trial results"}
    st = current_state(sample, [dec("e7", "fix", fix=E7_FIX), recheck("e7", "pass"),
                                dec("e7", "fix", fix=fix2), recheck("e7", "pass", at="2026-10-27T16:06:00+00:00")])
    assert st["mistakes"]["person"] == 2 and st["counts"]["fixes_pending"] == 0
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
    assert st["targets"]["e7"]["reason"].endswith(f["not_counted_reason"])
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


# ---------------------------------------------------------------- Codex PR9 review: which date belongs to the event

Q3_27, Q4_27 = ("2027-07-01", "2027-09-30"), ("2027-10-01", "2027-12-31")
BOUNDS = [pytest.param(True, id="with-bounds"), pytest.param(False, id="no-bounds")]


def _period(text, bounds, quote, with_bounds, **kw):
    start, end = bounds if with_bounds else (None, None)
    return _dm(text, start, quote, date_end=end, **kw)


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_negation_after_the_date_fails(with_bounds):
    r = _period("Q3 2027", Q3_27, "Q3 2027 is no longer the expected readout period.", with_bounds)
    assert r.result == "fail", r.reason
    assert "Q3 2027" in r.reason


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_a_superseded_period_fails_and_names_the_new_one(with_bounds):
    q = "Q3 2027 was the previous target; results are now expected in Q4 2027."
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "fail", r.reason
    assert r.reason == "The quote says Q3 2027 is no longer the expected period; it now names Q4 2027."
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_a_background_year_does_not_block_the_event_period(with_bounds):
    q = "In 2026 the company announced that results are expected in Q3 2027."
    assert _period("Q3 2027", Q3_27, q, with_bounds).result == "pass"
    r = _period("2026", ("2026-01-01", "2026-12-31"), q, with_bounds)
    assert r.result == "fail", r.reason
    assert r.reason == "The quote attaches Q3 2027 to the results; 2026 is not the date the quote gives for the event."


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_a_comparison_period_fails_and_the_event_period_passes(with_bounds):
    q = "Data in Q3 2027, compared with Q3 2026."
    r = _period("Q3 2026", ("2026-07-01", "2026-09-30"), q, with_bounds)
    assert r.result == "fail", r.reason
    assert r.reason == "The quote attaches Q3 2027 to the data; Q3 2026 is a comparison."
    assert _period("Q3 2027", Q3_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_two_event_clauses_with_different_dates_could_not_run(with_bounds):
    q = "Topline data are expected in Q3 2027; results will be presented in Q4 2027."
    for text, bounds in (("Q3 2027", Q3_27), ("Q4 2027", Q4_27)):
        r = _period(text, bounds, q, with_bounds)
        assert r.result == "could_not_run", r.reason
        assert r.reason == "Could not tell which date in the quote belongs to the event."


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_no_event_words_could_not_run(with_bounds):
    r = _period("Q3 2027", Q3_27, "The company expects Q3 2027.", with_bounds)
    assert r.result == "could_not_run", r.reason


def test_codex9_exact_day_against_a_later_comparison():
    q = "Topline data on March 12, 2027 follow the Q4 2026 results."
    assert _dm("March 12, 2027", "2027-03-12", q).result == "pass"
    assert _dm("March 12, 2027", "2027-03-13", q).result == "fail"
    for with_bounds in (True, False):
        r = _period("Q4 2026", ("2026-10-01", "2026-12-31"), q, with_bounds)
        assert r.result == "fail", r.reason
        assert r.reason == "The quote attaches March 12, 2027 to the data; Q4 2026 is a comparison."


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9_prefix_negation_and_plain_cases_go_through_the_clause_rule(with_bounds):
    neg = _period("Q3 2027", Q3_27, "Results are not expected in Q3 2027.", with_bounds)
    assert neg.result == "fail" and neg.reason == "The quote says the event is not in Q3 2027."
    assert _period("Q3 2027", Q3_27, "Results are expected in Q3 2027.", with_bounds).result == "pass"
    assert _period("Q4 2027", Q4_27, "Results are now expected in Q4 2027.", with_bounds).result == "pass"
    moved = "Results moved from Q3 2027 to Q4 2027."
    assert _period("Q4 2027", Q4_27, moved, with_bounds).result == "pass"
    assert _period("Q3 2027", Q3_27, moved, with_bounds).result == "fail"
    assert _period("Q3 2027", Q3_27, "The Q3 2027 readout has been delayed.", with_bounds).result == "fail"


def test_codex9_mention_order_is_not_evidence():
    # The same two dates in either order give the same answer.
    a = "Data in Q3 2027, compared with Q3 2026."
    b = "Compared with Q3 2026, data in Q3 2027."
    for q in (a, b):
        assert _dm("Q3 2027", *Q3_27[:1], q, date_end=Q3_27[1]).result == "pass", q
        assert _dm("Q3 2026", "2026-07-01", q, date_end="2026-09-30").result == "fail", q


def test_codex9_event_words_come_from_the_type_title_and_program():
    words = event_checks.event_anchors("regulatory_decision", "FDA target action date", "samplimab")
    assert {"pdufa", "action date", "samplimab"} <= set(words)
    assert event_checks.event_anchors("investor_day", "Investor day") == ["investor"]


# ---------------------------------------------------------------- Codex PR9 fix 0fa435e: a denied move dates nothing


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_a_denied_move_could_not_run_for_either_date(with_bounds):
    q = "Results will not move from Q3 2027 to Q4 2027."
    for text, bounds in (("Q4 2027", Q4_27), ("Q3 2027", Q3_27)):
        r = _period(text, bounds, q, with_bounds)
        assert r.result == "could_not_run", (text, r.reason)
        assert r.reason == "Could not tell which date in the quote belongs to the event."


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_a_superseded_period_still_fails_and_the_new_one_still_passes(with_bounds):
    q = "Q3 2027 was the previous target; results are now expected in Q4 2027."
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "fail", r.reason
    assert r.reason == "The quote says Q3 2027 is no longer the expected period; it now names Q4 2027."
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_now_q4_not_q3_passes_q4_and_fails_q3(with_bounds):
    # The comma puts "not Q3 2027" in its own clause, so the denied-move rule does not apply:
    # Q4 is the date given for the results and Q3 is the period the quote rejects.
    q = "Results are now expected in Q4 2027, not Q3 2027."
    assert _period("Q4 2027", Q4_27, q, with_bounds).result == "pass"
    r = _period("Q3 2027", Q3_27, q, with_bounds)
    assert r.result == "fail", r.reason
    assert r.reason == "The quote says Q3 2027 is no longer the expected period; it now names Q4 2027."


@pytest.mark.parametrize("with_bounds", BOUNDS)
def test_codex9b_an_affirmed_move_still_passes_the_new_date(with_bounds):
    moved = "Results moved from Q3 2027 to Q4 2027."
    assert _period("Q4 2027", Q4_27, moved, with_bounds).result == "pass"
    assert _period("Q3 2027", Q3_27, moved, with_bounds).result == "fail"
