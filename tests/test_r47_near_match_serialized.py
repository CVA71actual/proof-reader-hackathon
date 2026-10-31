"""Job 47: near_match and differs ride along on the quote check in run.json. No model call, no network, no key."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from checks.event_checks import quote_match
from runs.build_run import to_contract_check
from server.app import create_app

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text())
CHECK = Draft202012Validator({"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": "#/$defs/check"})

PAGE = "The company expects topline data for zelicapavir in the second half of 2027 and the 2027 data."
QUOTE = "The company expects topline data for zelicapavir in the second half of 2027"


def _words(text):
    return [{"id": f"w{i}", "text": t} for i, t in enumerate(text.split())]


def test_near_miss_serializes_both_fields():
    r = quote_match(QUOTE.replace("zelicapavir", "zelicacapavir"), _words(PAGE))
    c = to_contract_check(r.to_dict())
    assert c["near_match"] == QUOTE
    assert c["differs"] == [["zelicacapavir", "zelicapavir"]]
    assert not list(CHECK.iter_errors(c))
    assert json.loads(json.dumps(c)) == c


def test_curly_apostrophe_word_kept_whole():
    page = "Its ’s results come on July 19, 2027."
    r = quote_match("Its results come on July 9, 2027.", _words(page))
    if r.near_match is not None:
        c = to_contract_check(r.to_dict())
        assert c["near_match"] == r.near_match and not list(CHECK.iter_errors(c))


def test_exact_match_has_neither_field():
    c = to_contract_check(quote_match(QUOTE, _words(PAGE)).to_dict())
    assert "near_match" not in c and "differs" not in c
    assert not list(CHECK.iter_errors(c))


def test_schema_accepts_both_and_neither_and_rejects_bad_pairs():
    base = to_contract_check(quote_match(QUOTE, _words(PAGE)).to_dict())
    assert not list(CHECK.iter_errors({**base, "near_match": "x", "differs": [["a", "b"]]}))
    assert not list(CHECK.iter_errors({**base, "near_match": None, "differs": []}))
    assert list(CHECK.iter_errors({**base, "differs": [["a"]]}))


def test_sample_e7_has_them_and_the_server_sends_them(tmp_path):
    import shutil
    shutil.copytree(REPO / "runs" / "sample", tmp_path / "sample")
    client = TestClient(create_app(runs_dir=tmp_path, ui_dir=tmp_path / "no-ui"))
    rows = {r["id"]: r for r in client.get("/api/runs/sample").json()["rows"]}
    q = next(c for c in rows["e7"]["checks"] if c["name"] == "quote_match")
    assert q["near_match"] == "Samplebio Six will report final results on July 19, 2027."
    assert q["differs"] == [["9,", "19,"]]
    for rid, row in rows.items():
        if rid != "e7":
            assert all("near_match" not in c for c in row["checks"])
