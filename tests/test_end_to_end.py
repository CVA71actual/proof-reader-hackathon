import json
import re

import pytest
from conftest import FIXTURES, REPO

from capture import read_table
from checks.status import FAILED, MATCHED, PROPOSED, UNREADABLE, check_reading
from viewer import build_viewer


@pytest.fixture(scope="module")
def fake_reading(words_doc):
    return read_table.read_fake(words_doc)


def test_fake_reading_is_labelled(fake_reading):
    assert fake_reading["model"] == "fake"
    assert fake_reading["fake"] is True
    assert "FAKE" in fake_reading["fake_note"]
    assert fake_reading["readable"] is True
    assert fake_reading["prompt_sha256"] == read_table.sha256_text(fake_reading["prompt_text"])


def test_fake_reading_is_deterministic(words_doc, fake_reading):
    assert read_table.read_fake(words_doc)["table"] == fake_reading["table"]


def test_end_to_end_on_fake_reading(words_doc, fake_reading):
    checked = check_reading(fake_reading, words_doc, visual_evidence=None, derived_words_present=False)
    cells = [c for r in checked["rows"] for c in r["cells"]]
    assert len(checked["rows"]) == 23 and len(cells) == 69
    assert checked["counts"] == {MATCHED: 66, FAILED: 2, PROPOSED: 1}
    by_key = {(r["label"], c["column"]): c for r in checked["rows"] for c in r["cells"]}
    assert by_key[("Total revenue", "2025")]["status"] == FAILED
    assert by_key[("Royalty revenue", "2024")]["status"] == FAILED
    assert by_key[("Net loss", "2023")]["status"] == PROPOSED
    assert by_key[("Loss from operations", "2025")]["status"] == MATCHED
    assert by_key[("Product sales, net", "2024")]["value"] == "-"
    # Every cell that was not changed on purpose is matched.
    for c in cells:
        assert (c["status"] == MATCHED) == ("fake_note" not in c)
        assert [k["check"] for k in c["checks"]] == ["text_layer_match", "selector_match", "row_column_match"]
        assert all(k["kind"] == "Code" for k in c["checks"])


def test_prompt_lists_every_word(words_doc):
    prompt = read_table.build_prompt(words_doc)
    for w in words_doc["words"]:
        assert f"\n{w['id']}|{w['text']}|" in prompt


@pytest.mark.parametrize("raw", [None, "", "sorry, I cannot", "[1, 2]", '{"rows": "none"}', '{"rows": [{"label": "x"}]}'])
def test_unparsable_answer_is_unreadable(raw, words_doc):
    table, problem = read_table.parse_table(raw)
    assert table is None and problem
    reading = {"readable": False, "table": None, "raw_response": raw}
    assert check_reading(reading, words_doc, visual_evidence=None, derived_words_present=False)["status"] == UNREADABLE


def test_fenced_json_answer_is_parsed():
    table, problem = read_table.parse_table('```json\n{"title": "T", "columns": ["2025"], "rows": []}\n```')
    assert problem is None and table["title"] == "T"


def test_missing_key_gives_plain_message(monkeypatch, tmp_path, words_doc):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    with pytest.raises(SystemExit) as e:
        read_table.read_live(words_doc)
    assert "MODEL_API_KEY" in str(e.value)


def test_env_file_loader(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_TEST_VALUE", raising=False)
    f = tmp_path / "env"
    f.write_text('# comment\nPR_TEST_VALUE="abc def"\n')
    read_table.load_env_file(f)
    import os
    assert os.environ["PR_TEST_VALUE"] == "abc def"


def test_viewer_builds(tmp_path, fake_reading):
    out = build_viewer.build(fake_reading, tmp_path / "index.html")
    html = out.read_text()
    assert "data:image/png;base64," in html
    assert "FAKE" in html and "Second reader: Day 3" in html
    assert "Matches source text" in html and "Citation located" in html and "Right row and column" in html
    assert "0001140361-26-016395" in html


def test_no_forbidden_words_in_product_code():
    banned = re.compile(r"verif|prove|proved|correct", re.I)
    for folder in ("capture", "checks", "viewer"):
        for p in (REPO / folder).glob("*.py"):
            assert not banned.search(p.read_text()), p


REAL = FIXTURES / "ions_ars_2026_p95.reading.json"


@pytest.mark.skipif(not REAL.exists(), reason="no model reading recorded yet")
def test_checks_run_over_model_reading(words_doc):
    reading = json.loads(REAL.read_text())
    assert reading["model"] != "fake" and reading["fake"] is False
    assert reading["prompt_sha256"] == read_table.sha256_text(reading["prompt_text"])
    checked = check_reading(reading, words_doc, visual_evidence=None, derived_words_present=False)
    if not reading["readable"]:
        assert checked["status"] == UNREADABLE
        return
    for row in checked["rows"]:
        for c in row["cells"]:
            results = {k["check"]: k["result"] for k in c["checks"]}
            if c["status"] == MATCHED:
                assert set(results.values()) == {"pass"}
