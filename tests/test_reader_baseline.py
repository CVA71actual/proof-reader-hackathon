"""Offline reader-baseline import. Synthetic artifacts are marked synthetic; the one real archived example is a
read-only existing fixture. Nothing here calls a model or a network."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import reader_baseline as rb

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "fixtures"
FIRST = FIX / "ptgx_8k_2026-08-05_ex991_p3.events.json"
SECOND = FIX / "ptgx_8k_2026-08-05_ex991_p3.events.second.json"


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


def synth(raw, **extra):
    d = {"synthetic": True, "model": "synthetic-model", "prompt_text": "SYNTHETIC PROMPT", "raw_response": raw}
    d.update(extra)
    return d


def one(tmp_path, art, role=None, name="a.json"):
    return rb.build_record(write(tmp_path, name, art), role)


# ---- real archived example -------------------------------------------------------------------------------

def test_real_archive_two_readers_distinct_and_raw_exact():
    doc = rb.build([(None, FIRST), (None, SECOND)])
    a, b = doc["records"]
    assert doc["schema_version"] == 1 and doc["mode"] == "offline_replay"
    assert a["record_id"] != b["record_id"]
    assert (a["reader_role"], b["reader_role"]) == ("unknown", "second")   # the first artifact states no role
    assert a["metadata"]["import"]["role_source"] == "not_stated"
    assert a["model"] != b["model"]
    for rec, path in ((a, FIRST), (b, SECOND)):
        saved = json.loads(path.read_text())
        assert rec["raw_response"] == saved["raw_response"]               # byte-exact, not prettified
        assert rec["prompt"] == saved["prompt_text"]
        assert rec["parse_status"] == "ok" and rec["proposals"] == saved["events"]
        assert rec["metadata"]["saved_parse"]["saved_events_equal_reparse"] is True
        assert rec["metadata"]["provenance"]["prompt_sha256_check"] == "match"
        assert rec["metadata"]["provenance"]["historical_source_sha256"] == saved["page_sha256"]
        assert rec["metadata"]["import"]["artifact_sha256"] == rec["source"]["sha256"] != saved["page_sha256"]
    assert a["raw_response"] != b["raw_response"]
    assert all(r["downstream_rows"] is None and r["links"] == [] for r in doc["records"])


def test_explicit_role_wins_and_is_labelled():
    doc = rb.build([("first", FIRST)])
    assert doc["records"][0]["reader_role"] == "first"
    assert doc["records"][0]["metadata"]["import"]["role_source"] == "explicit"


def test_recorded_cost_time_not_invented_and_replay_time_separate():
    r = rb.build([("first", FIRST)])["records"][0]
    assert r["metadata"]["recorded_timing_cost"]["elapsed_seconds"] is None
    assert r["metadata"]["recorded_timing_cost"]["cost_usd"] is None
    assert r["metadata"]["recorded_timing_cost"]["usage"]["prompt_tokens"] == 10586
    assert r["metadata"]["replay"]["elapsed_seconds"] is None
    m = rb.build([("first", FIRST)], measure=True)["records"][0]
    assert isinstance(m["metadata"]["replay"]["elapsed_seconds"], float)


# ---- parse outcomes --------------------------------------------------------------------------------------

def test_malformed_reply_keeps_raw_and_error(tmp_path):
    raw = "Sure! here are the events: [oops"
    r = one(tmp_path, synth(raw))
    assert r["parse_status"] == "malformed" and r["proposals"] == []
    assert r["raw_response"] == raw and "not JSON" in r["parse_error"]


def test_valid_empty_reply_is_ok_not_empty(tmp_path):
    r = one(tmp_path, synth('{"events": []}'))
    assert r["parse_status"] == "ok" and r["proposals"] == [] and r["parse_error"] is None


def test_blank_reply_is_empty_and_distinct_from_valid_empty(tmp_path):
    for i, raw in enumerate(["", "  \n", None]):
        r = one(tmp_path, synth(raw), name=f"b{i}.json")
        assert r["parse_status"] == "empty" and r["proposals"] == []
    assert one(tmp_path, synth("  \n"), name="c.json")["raw_response"] == "  \n"


def test_wrong_shapes_are_malformed_not_repaired(tmp_path):
    for i, raw in enumerate(['{"events": {"a": 1}}', '{"events": [1]}', "[]", '{"other": []}']):
        assert one(tmp_path, synth(raw), name=f"w{i}.json")["parse_status"] == "malformed"


def test_recorded_call_failure_is_error(tmp_path):
    r = one(tmp_path, {"synthetic": True, "raw_response": None, "call_failed": True,
                       "parse_problem": "The service did not answer."})
    assert r["parse_status"] == "error" and r["parse_error"] == "The service did not answer."
    assert r["proposals"] == [] and r["raw_response"] is None


def test_raw_preserved_exactly_including_whitespace_and_fence(tmp_path):
    raw = '```json\r\n{"events":[{"quote":"x"}]}\r\n```\n\n'
    r = one(tmp_path, synth(raw))
    assert r["raw_response"] == raw
    assert r["parse_status"] == "ok" and r["proposals"] == [{"quote": "x"}]


def test_proposals_come_from_raw_and_disagreement_with_saved_events_is_flagged(tmp_path):
    r = one(tmp_path, synth('{"events":[{"quote":"a"},{"quote":"b"}]}', events=[{"quote": "a"}]))
    assert len(r["proposals"]) == 2
    assert r["metadata"]["saved_parse"]["saved_events_equal_reparse"] is False
    assert r["metadata"]["saved_parse"]["saved_events_count"] == 1


# ---- proposal content -----------------------------------------------------------------------------------

def test_conditional_proposal_and_unknown_fields_survive_exactly(tmp_path):
    ev = {"quote": "Phase 3 start expected 2027, contingent on financing.", "conditions": ["financing"],
          "relevance": "primary", "subject_company": "SyntheticCo", "relation": "own", "type_hint": "milestone",
          "novel_field": {"nested": [1, None, "x"]}, "date_text": None}
    raw = json.dumps({"events": [ev], "extra_top": 1})
    r = one(tmp_path, synth(raw))
    assert r["proposals"] == [ev] and list(r["proposals"][0]) == list(ev)
    assert r["metadata"]["synthetic"] is True


def test_missing_provenance_stays_missing(tmp_path):
    r = one(tmp_path, {"raw_response": '{"events": []}'})
    assert r["model"] is None and r["prompt"] is None
    p = r["metadata"]["provenance"]
    assert p["historical_source_sha256"] is None and p["historical_source_sha256_status"] == "missing"
    assert p["recorded_prompt_sha256"] is None and p["prompt_available"] is False
    assert p["prompt_sha256_check"] == "not_checkable"
    assert r["source"]["page"] is None
    assert r["metadata"]["import"]["artifact_sha256"]          # present import hash is a different thing


def test_prompt_hash_mismatch_reported_not_fixed(tmp_path):
    r = one(tmp_path, synth('{"events": []}', prompt_sha256="0" * 64))
    assert r["metadata"]["provenance"]["prompt_sha256_check"] == "mismatch"
    assert r["prompt"] == "SYNTHETIC PROMPT"


def test_no_raw_response_with_saved_events_is_unavailable_and_proposals_stay_empty(tmp_path):
    r = one(tmp_path, {"synthetic": True, "events": [{"quote": "q"}]})
    assert r["parse_status"] == "unavailable" and r["raw_response"] is None
    assert r["proposals"] == []                                   # missing raw evidence stays missing
    sp = r["metadata"]["saved_parse"]
    assert sp["saved_events"] == [{"quote": "q"}] and "downstream-side" in sp["saved_events_are"]
    assert sp["saved_events_equal_reparse"] is None
    assert "proposals_are_saved_events_only" not in sp


def test_failed_call_without_raw_keeps_saved_events_only_as_downstream_parse(tmp_path):
    saved = [{"quote": "downstream-kept"}]
    r = one(tmp_path, {"call_failed": True, "error": "reader failed", "events": saved})
    assert r["parse_status"] == "error" and r["proposals"] == []
    assert r["raw_response"] is None
    assert r["metadata"]["saved_parse"]["saved_events"] == saved
    assert "downstream-side" in r["metadata"]["saved_parse"]["saved_events_are"]
    assert "downstream-kept" not in json.dumps(r["proposals"])


def test_downstream_kept_events_never_become_reader_proposals(tmp_path):
    r = one(tmp_path, {"events": [{"quote": "downstream-kept"}], "prompt_text": "p"})
    assert r["parse_status"] == "unavailable" and r["raw_response"] is None
    assert r["proposals"] == []
    assert r["metadata"]["saved_parse"]["saved_events"] == [{"quote": "downstream-kept"}]
    doc = rb.build([(None, tmp_path / "a.json")])
    assert doc["records"][0]["metadata"]["structure"]["proposal_count"] == 0
    assert "downstream-kept" not in json.dumps(doc["records"][0]["proposals"])


def test_fake_model_reading_is_marked_synthetic(tmp_path):
    r = one(tmp_path, {"raw_response": '{"events":[{"quote":"a"}]}', "fake": True, "model": "fake"})
    assert r["parse_status"] == "ok"
    assert r["metadata"]["synthetic"] is True and r["metadata"]["synthetic_markers"] == ["fake"]
    real = one(tmp_path, {"raw_response": '{"events": []}', "fake": False}, name="b.json")
    assert "synthetic" not in real["metadata"]


def test_real_archive_is_not_marked_synthetic():
    for rec in rb.build([(None, FIRST), (None, SECOND)])["records"]:
        assert "synthetic" not in rec["metadata"]


# ---- unsupported formats ---------------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", ["ions_ars_2026_p95.reading.json",
                                     "ptgx_8k_2026-08-05_ex991_p3.scan_reading.json",
                                     "ptgx_8k_2026-08-05_ex991_p3.sam.json"])
def test_other_reader_formats_are_refused(fixture):
    with pytest.raises(rb.InputError, match="nsupported"):
        rb.build([(None, FIX / fixture)])


@pytest.mark.parametrize("art", [{"error": "boom"}, {"call_failed": True, "error": "boom"},
                                 {"model": "m", "prompt_text": "p"}, {"events": {"a": 1}}])
def test_objects_without_the_record_shape_are_refused(tmp_path, art):
    p = write(tmp_path, "e.json", art)
    with pytest.raises(rb.InputError, match="nsupported reader artifact e.json: .*not an event reading record"):
        rb.build([(None, p)])


def test_non_json_and_non_object_refused(tmp_path):
    with pytest.raises(rb.InputError, match="not JSON"):
        rb.build([(None, write(tmp_path, "x.json", "{nope"))])
    with pytest.raises(rb.InputError, match="nsupported"):
        rb.build([(None, write(tmp_path, "y.json", "[1]"))])


# ---- downstream and links --------------------------------------------------------------------------------

def three(tmp_path):
    art = synth(json.dumps({"events": [{"quote": "a"}, {"quote": "a"}, {"quote": "c"}]}))
    return write(tmp_path, "r.json", art)


def test_downstream_empty_is_distinct_from_unavailable(tmp_path):
    rp = three(tmp_path)
    none = rb.build([("first", rp)])["records"][0]
    empty = rb.build([("first", rp)], {"r.json": write(tmp_path, "d.json", [])})["records"][0]
    assert none["downstream_rows"] is None
    assert empty["downstream_rows"] == []
    s = empty["metadata"]["structure"]
    assert (s["proposal_count"], s["downstream_row_count"], s["unlinked_proposals"]) == (3, 0, 3)
    assert none["metadata"]["structure"]["count_difference"] is None
    assert "not a verdict" in s["note"]
    assert empty["metadata"]["downstream_snapshot"]["sha256"]


def test_rows_object_form_and_unknown_row_fields_kept(tmp_path):
    rows = [{"ticker": "X", "mystery": {"k": 1}}]
    rec = rb.build([("first", three(tmp_path))], {"r.json": write(tmp_path, "d.json", {"rows": rows})})["records"][0]
    assert rec["downstream_rows"] == rows
    with pytest.raises(rb.InputError):
        rb.build([("first", three(tmp_path))], {"r.json": write(tmp_path, "e.json", {"nope": 1})})


def test_only_explicit_links_pair_and_nothing_is_inferred(tmp_path):
    rp = three(tmp_path)    # proposals 0 and 1 have identical quotes: no inference from text or order
    rows = write(tmp_path, "d.json", [{"quote": "a"}, {"quote": "a"}, {"quote": "c"}])
    lk = write(tmp_path, "l.json", [{"record": "r.json", "proposal_index": 2, "row_index": 0}])
    rec = rb.build([("first", rp)], {"r.json": rows}, lk)["records"][0]
    assert rec["links"] == [{"proposal_index": 2, "row_index": 0}]
    s = rec["metadata"]["structure"]
    assert (s["unlinked_proposals"], s["unlinked_rows"], s["linked_pairs"]) == (2, 2, 1)
    assert "link_issues" not in rec["metadata"]


def test_no_link_map_means_no_links_even_with_equal_counts(tmp_path):
    rows = write(tmp_path, "d.json", [{}, {}, {}])
    rec = rb.build([("first", three(tmp_path))], {"r.json": rows})["records"][0]
    assert rec["links"] == [] and rec["metadata"]["provenance"]["links_supplied"] is False


def test_bad_links_are_reported_not_guessed(tmp_path):
    rows = write(tmp_path, "d.json", [{}, {}, {}])
    L = lambda p, r: {"record": "r.json", "proposal_index": p, "row_index": r}
    entries = [L(0, 0),                       # good
               L(5, 1), L(1, 9), L(-1, 0),    # out of bounds
               {"record": "r.json", "proposal_index": "1", "row_index": 1},   # malformed (string)
               {"record": "r.json", "proposal_index": True, "row_index": 1},  # malformed (bool)
               {"record": "r.json", "proposal_index": 1},                     # malformed (missing)
               L(2, 1), L(2, 2),             # one proposal -> two rows: conflict
               L(1, 1)]                      # row 1 claimed by proposals 1 and 2: conflict
    lk = write(tmp_path, "l.json", {"links": entries})
    rec = rb.build([("first", three(tmp_path))], {"r.json": rows}, lk)["records"][0]
    kinds = [i["kind"] for i in rec["metadata"]["link_issues"]]
    assert kinds.count("out_of_bounds") == 3 and kinds.count("malformed") == 3
    assert kinds.count("conflicting") == 3
    assert rec["links"] == [{"proposal_index": 0, "row_index": 0}]       # only the unambiguous pair


def test_duplicate_identical_link_collapses_with_note(tmp_path):
    rows = write(tmp_path, "d.json", [{}])
    lk = write(tmp_path, "l.json", [{"record": "r.json", "proposal_index": 0, "row_index": 0}] * 2)
    rec = rb.build([("first", three(tmp_path))], {"r.json": rows}, lk)["records"][0]
    assert rec["links"] == [{"proposal_index": 0, "row_index": 0}]
    assert [i["kind"] for i in rec["metadata"]["link_issues"]] == ["duplicate"]


def test_links_without_downstream_are_unavailable_not_paired(tmp_path):
    lk = write(tmp_path, "l.json", [{"record": "r.json", "proposal_index": 0, "row_index": 0}])
    rec = rb.build([("first", three(tmp_path))], None, lk)["records"][0]
    assert rec["links"] == [] and rec["metadata"]["link_issues"][0]["kind"] == "downstream_unavailable"


def test_link_to_unknown_record_is_an_input_error(tmp_path):
    lk = write(tmp_path, "l.json", [{"record": "nope.json", "proposal_index": 0, "row_index": 0}])
    with pytest.raises(rb.InputError, match="No supplied reader"):
        rb.build([("first", three(tmp_path))], None, lk)


def test_same_artifact_twice_is_refused(tmp_path):
    rp = three(tmp_path)
    with pytest.raises(rb.InputError, match="Duplicate record_id"):
        rb.build([("first", rp), ("second", rp)])


# ---- portability / CLI -----------------------------------------------------------------------------------

def test_absolute_paths_reduced_and_home_paths_flagged(tmp_path):
    rp = write(tmp_path, "h.json", synth('{"events": []}', note="saved at /Users/someone/x/y.json"))
    r = rb.build_record(rp, "first")
    assert r["source"]["artifact_path"] == "h.json"
    assert r["metadata"]["portability_warnings"] == ["metadata.recorded.note"]
    assert "/Users/" not in json.dumps(r["source"]) and str(tmp_path) not in json.dumps(r["source"])
    assert rb.portable_path("../../secret/x.json") == "x.json"


def test_cli_end_to_end_and_unsupported_exit(tmp_path):
    out = tmp_path / "o.json"
    cmd = [sys.executable, "-I", str(REPO / "tools" / "reader_baseline.py")]
    ok = subprocess.run(cmd + ["--reader", f"first={FIRST}", "--reader", str(SECOND), "--out", str(out)],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    doc = json.loads(out.read_text())
    assert [r["reader_role"] for r in doc["records"]] == ["first", "second"]
    assert list(doc) == ["schema_version", "mode", "records"]
    assert str(REPO) not in out.read_text().split('"raw_response"')[0]
    bad = subprocess.run(cmd + ["--reader", str(FIX / "ions_ars_2026_p95.reading.json"), "--out", str(tmp_path / "n.json")],
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "nsupported" in bad.stderr and not (tmp_path / "n.json").exists()


def _snapshot(root: Path) -> dict:
    return {str(f.relative_to(root)): (f.stat().st_size, f.stat().st_mtime_ns)
            for f in root.rglob("*") if f.is_file() and "__pycache__" not in f.parts}


def test_successful_run_writes_only_out(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    a = write(work, FIRST.name, FIRST.read_text())
    b = write(work, SECOND.name, SECOND.read_text())
    rows = write(work, "rows.json", [{"quote": "x"}])
    lk = write(work, "links.json", [{"record": FIRST.name, "proposal_index": 0, "row_index": 0}])
    monkeypatch.chdir(work)
    out = work / "out" / "o.json"
    out.parent.mkdir()
    before_tmp, before_fix = _snapshot(tmp_path), _snapshot(FIX)
    rc = rb.main(["--reader", f"first={a}", "--reader", str(b), "--downstream", f"{FIRST.name}={rows}",
                  "--links", str(lk), "--out", str(out)])
    assert rc == 0
    after_tmp = _snapshot(tmp_path)
    assert set(after_tmp) - set(before_tmp) == {str(out.relative_to(tmp_path))}
    assert {k: v for k, v in after_tmp.items() if k in before_tmp} == before_tmp     # inputs untouched
    assert _snapshot(FIX) == before_fix
    assert capsys.readouterr().out == ""                                            # nothing on stdout either
    assert json.loads(out.read_text())["records"][0]["links"] == [{"proposal_index": 0, "row_index": 0}]


def test_no_network_and_no_credentials_needed(tmp_path, monkeypatch):
    import http.client
    import os
    import socket
    import urllib.request

    def blocked(*a, **k):
        raise AssertionError("network use attempted")

    for mod, name in ((socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                      (urllib.request, "urlopen"), (http.client.HTTPConnection, "connect"),
                      (http.client.HTTPSConnection, "connect")):
        monkeypatch.setattr(mod, name, blocked)
    try:
        import requests
        monkeypatch.setattr(requests.Session, "request", blocked)
    except ImportError:
        pass
    try:
        import httpx
        monkeypatch.setattr(httpx.Client, "send", blocked)
    except ImportError:
        pass
    monkeypatch.setattr(os, "environ", {})       # no credentials or any other environment
    out = tmp_path / "o.json"
    assert rb.main(["--reader", f"first={FIRST}", "--reader", str(SECOND), "--out", str(out)]) == 0
    assert [r["parse_status"] for r in json.loads(out.read_text())["records"]] == ["ok", "ok"]
