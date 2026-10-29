"""Job 27: SAM 3.1 regions + an image reader rebuild the words of a picture page. SAM and both readers are fakes."""

import json
import re

import pytest
from jsonschema import Draft202012Validator

from capture import read_events, sam
from capture.reader import Reply
from capture.capture_source import load_entry
from runs import picture, scan, sources
from server import jobs
from tests.conftest import FIXTURES, REPO
from tests.test_scan import FakeSam, answer_line
from tests.test_second_reader import FakeSecond

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PIN = read_events.PIN
TEXT = "Phase 2b initiation in psoriasis: Q1 '27\nOral Ph1: 1H '27"


def word(i, h, y=399.3, x=31.0):
    return {"id": f"w{i}", "text": f"t{i}", "x0": x + i, "y0": y, "x1": x + i + 2, "y1": y + h, "block": 0, "line": 0,
            "word": i}


# ---------------------------------------------------------------- detection


def test_a_hidden_strip_is_a_picture_page():
    doc = {"page_width": 612, "page_height": 792, "words": [word(i, 0.56) for i in range(200)]}
    assert picture.is_picture_page(doc)


def test_a_page_with_few_real_words_is_a_picture_page():
    doc = {"words": [word(i, 8) for i in range(picture.MIN_REAL_WORDS - 1)]}
    assert picture.is_picture_page(doc)
    assert picture.is_picture_page({"words": []})


def test_a_page_of_printed_text_is_not():
    doc = {"words": [word(i, 8) for i in range(picture.MIN_REAL_WORDS)]}
    assert not picture.is_picture_page(doc)
    assert not picture.is_picture_page(json.loads((FIXTURES / load_entry(PIN)["words_file"]).read_text()))


# ---------------------------------------------------------------- regions and crops


def test_regions_are_largest_first_nested_ones_dropped_and_in_reading_order():
    big, title, low = [30, 100, 580, 400], [40, 110, 300, 140], [30, 500, 580, 700]
    regions = picture.merge_regions({"text block": [low, big], "title": [title], "table": [], "bullet list": []})
    assert [r["box"] for r in regions] == [big, low] and [r["id"] for r in regions] == ["r1", "r2"]
    assert regions[0]["concept"] == "text block"


def test_the_whole_page_is_the_region_only_when_nothing_finer_matched():
    assert picture.needs_fallback({c: [] for c in picture.CONCEPTS})
    only_page = picture.merge_regions({"title": [], "page": [[0, 0, 612, 792]]})
    assert only_page == [{"id": "r1", "concept": "page", "box": [0, 0, 612, 792]}]
    assert picture.merge_regions({"title": [[10, 10, 100, 40]], "page": [[0, 0, 612, 792]]})[0]["concept"] == "title"


def test_a_crop_is_the_region_plus_padding_inside_the_page():
    region = {"id": "r1", "concept": "title", "box": [2, 100, 300, 140]}
    assert scan.crop_box(region, 612, 792) == [0.0, 96.0, 304.0, 144.0]
    png = scan.scanned_png(load_entry(PIN))                       # the page at 2 px per point
    import pymupdf
    crop = pymupdf.Pixmap(scan.crop_png(png, [0.0, 96.0, 304.0, 144.0], 612.0))
    assert (crop.width, crop.height) == (608, 96)


# ---------------------------------------------------------------- the words file


def test_words_file_shape_one_word_per_token_inside_the_region():
    region = {"id": "r1", "concept": "text block", "box": [40.0, 100.0, 340.0, 160.0]}
    doc = picture.build_words(612.0, 792.0, 7, [{"region": region, "text": TEXT}])
    assert doc["words_from"] == "sam+image reader" and doc["kind"] == "picture"
    assert doc["page_number"] == 7 and doc["page_width"] == 612.0 and doc["regions"][0]["words"] == len(doc["words"])
    assert [w["text"] for w in doc["words"]] == TEXT.split()
    assert [w["id"] for w in doc["words"]] == [f"w{i}" for i in range(len(doc["words"]))]
    for w in doc["words"]:
        assert set(w) == {"id", "text", "x0", "y0", "x1", "y1", "block", "line", "word"}
        assert 40.0 <= w["x0"] < w["x1"] <= 340.01 and 100.0 <= w["y0"] < w["y1"] <= 160.0
        assert w["y1"] - w["y0"] >= picture.HIDDEN_PT, "a rebuilt word is tall enough to be printed text"
    first, second = [w for w in doc["words"] if w["line"] == 0], [w for w in doc["words"] if w["line"] == 1]
    assert max(w["y1"] for w in first) <= min(w["y0"] for w in second) + 0.01, "two lines, two bands"
    assert all(a["x1"] <= b["x0"] for a, b in zip(first, first[1:])), "words go left to right without overlap"
    assert not picture.is_picture_page(doc), "a rebuilt words file is not itself a picture page"


def test_a_transcription_is_read_from_json_or_plain_text():
    assert picture.parse_transcription('{"text": "a b\\nc"}') == "a b\nc"
    assert picture.parse_transcription("a b\nc") == "a b\nc"
    assert picture.parse_transcription("") is None and picture.parse_transcription('{"text": ""}') is None


# ---------------------------------------------------------------- the job


PX = 2                         # SAM answers in image pixels; the page image is 2 px per point
BOXES = {"text block": [(80, 200, 1100, 500)], "title": [(80, 100, 600, 160)]}


class Transcriber(FakeSecond):
    """The image reader: a transcription for the region prompt, nothing found for a whole-page read."""

    def __init__(self, text=TEXT, empty=(), **kw):
        """`empty`: the 1-based numbers of the region reads that come back with no text."""
        def answer(prompt, n):
            if "Transcribe all the text" in prompt:
                return json.dumps({"text": "" if n in empty else text})
            return json.dumps({"events": []})
        super().__init__(answer=answer, **kw)


class Spark:
    """The event reader: proposes the two sentences it is shown in the word list."""
    model = "muse-spark-1.3"
    problem = None

    def __init__(self):
        self.prompts = []

    def ready(self):
        return self.problem

    def call(self, prompt, image=None):
        self.prompts.append(prompt)
        quote = TEXT.splitlines()[0]
        ev = {"ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.", "program": None, "event_type": "milestone",
              "title": "Phase 2b initiation", "date_text": "Q1 '27", "date": None, "date_end": None, "quote": quote,
              "word_ids": [], "box": None}
        return Reply(json.dumps({"events": [ev]}), self.model, prompt_tokens=1000, completion_tokens=100)


@pytest.fixture
def picture_index(tmp_path):
    """One PTGX page whose words file is a hidden strip (what the ex992 slides carry)."""
    entry = load_entry(PIN)
    words = {"page_number": 3, "page_width": 612.0, "page_height": 792.0, "units": "pt",
             "words": [{"id": f"w{i}", "text": "x", "x0": 31.75 + i, "y0": 399.3, "x1": 32.5 + i, "y1": 399.86,
                        "block": 1, "line": 0, "word": i} for i in range(150)]}
    wf = tmp_path / "strip.words.json"
    wf.write_text(json.dumps(words))
    rec = {**{k: v for k, v in entry.items() if k != "entries"}, "page_file": str(FIXTURES / entry["page_file"]),
           "words_file": str(wf), "words_sha256": sources._sha(wf), "id": "slide-p3", "ticker": "SLD"}
    idx = tmp_path / "idx.json"
    idx.write_text(json.dumps({"index_version": 1, "companies": {"SLD": {"pages": [rec]}}}))
    return sources.load_index(idx)


def run(tmp_path, index, sam_reader, second, first=None):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    choices = jobs.validate_choices({"companies": ["SLD"], "event_types": list(read_events.EVENT_TYPES),
                                     "time_window_months": None, "spending_limit_usd": None, "note_for_agent": None},
                                    index)
    jobs.create_run(runs_dir, choices)
    first = first or Spark()
    jobs.run_job(runs_dir, "run-1", first, second, None, None, sam_reader, index)
    return json.loads((runs_dir / "run-1" / "run.json").read_text()), runs_dir / "run-1", first


def test_a_picture_page_is_rebuilt_read_and_checked(tmp_path, picture_index):
    fake_sam, second = FakeSam(BOXES), Transcriber()
    run_json, run_dir, first = run(tmp_path, picture_index, fake_sam, second)
    assert run_json["run"]["status"] == "done"
    RUN_SCHEMA.validate(run_json)
    assert [c for c, _ in fake_sam.calls] == list(picture.CONCEPTS), "one request per concept, no whole-page fallback"
    page = run_json["sources"][0]["pages"][0]
    assert page["kind"] == "picture" and page["words_from"] == "sam+image reader"
    assert page["note"] == "words read from the picture by two Meta models; not the page's own text layer"
    words = json.loads((run_dir / page["words"]).read_text())
    assert words["words_from"] == "sam+image reader" and len([w for w in words["words"] if not w.get("break")]) == len(TEXT.split()) * 2
    assert page["words_sha256"] == __import__("runs.build_run", fromlist=["x"]).file_sha256(run_dir / page["words"])
    assert "t1" not in first.prompts[0] and "Phase" in first.prompts[0], "the reader is shown the rebuilt words"
    row = run_json["rows"][0]
    checks = {c["name"]: c["result"] for c in row["checks"]}
    assert checks["quote_match"] == "pass", row["checks"][0]["reason"]
    assert [s["step"] for s in row["trail"][:5]] == [
        "Sent the page to Muse Spark", "SAM 3.1 found 2 regions", "Transcribed region 1", "Transcribed region 2",
        "Words rebuilt from the picture"]
    sam_step, t1, built = row["trail"][1], row["trail"][2], row["trail"][4]
    assert (sam_step["tag"], t1["tag"], built["tag"]) == ("Model", "Model", "Code")
    assert sam_step["who"] == "SAM 3.1" and t1["who"] == "Muse Spark Open"
    assert "Source note: words read from the picture by two Meta models; not the page's own text layer" in row["reason"]


def test_the_trail_costs_add_up_and_the_run_counts_each_call_once(tmp_path, picture_index):
    run_json, _, _ = run(tmp_path, picture_index, FakeSam(BOXES), Transcriber())
    # 4 SAM images + 2 transcriptions + 1 event read, plus the second reader's whole-page read
    assert run_json["run"]["turns_total"] == 4 + 2 + 1 + 1
    assert run_json["run"]["spend_usd"] > 0


def test_no_region_found_reads_the_whole_page(tmp_path, picture_index):
    fake_sam = FakeSam({"page": [(0, 0, 1224, 1584)]})
    run_json, _, _ = run(tmp_path, picture_index, fake_sam, Transcriber())
    assert [c for c, _ in fake_sam.calls] == list(picture.CONCEPTS) + ["page"]
    assert run_json["rows"][0]["trail"][1]["step"] == "SAM 3.1 found 1 region"
    assert run_json["sources"][0]["pages"][0]["regions"][0]["concept"] == "page"


def test_when_sam_finds_nothing_code_makes_the_whole_page_one_region_and_the_note_says_so(tmp_path, picture_index):
    fake_sam = FakeSam({})
    run_json, run_dir, _ = run(tmp_path, picture_index, fake_sam, Transcriber())
    RUN_SCHEMA.validate(run_json)
    assert [c for c, _ in fake_sam.calls] == list(picture.CONCEPTS) + ["page"]
    page = run_json["sources"][0]["pages"][0]
    assert page["regions"] == [{"id": "r1", "concept": "whole page", "box": [0.0, 0.0, 612.0, 792.0]}]
    assert "SAM 3.1 found no region" in page["note"] and "two Meta models" not in page["note"]
    row = run_json["rows"][0]
    assert row["trail"][1]["step"] == "SAM 3.1 found 0 regions" and "code made one region" in row["trail"][1]["text"]
    assert "SAM 3.1 found no region" in row["reason"]
    assert json.loads((run_dir / page["words"]).read_text())["words_from"] == "sam+image reader"


def test_without_sam_the_page_is_read_from_its_own_words_and_the_log_says_so(tmp_path, picture_index):
    run_json, run_dir, _ = run(tmp_path, picture_index, None, Transcriber())
    assert "kind" not in run_json["sources"][0]["pages"][0]
    log = json.loads((run_dir / "job.json").read_text())["log"]
    assert any("picture page" in e.get("note", "") and "not rebuilt" in e["note"] for e in log)


def test_a_page_of_printed_text_never_calls_sam(tmp_path):
    from tests.test_twelve import index_doc
    p = tmp_path / "i.json"
    p.write_text(json.dumps(index_doc()))
    index = sources.load_index(p)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    choices = jobs.validate_choices({"companies": ["BBB"], "event_types": list(read_events.EVENT_TYPES),
                                     "time_window_months": None, "spending_limit_usd": None, "note_for_agent": None},
                                    index)
    jobs.create_run(runs_dir, choices)
    from tests.test_run_controls import FakeReader
    fake_sam = FakeSam(BOXES)
    jobs.run_job(runs_dir, "run-1", FakeReader(), None, None, None, fake_sam, index)
    assert fake_sam.calls == []


# ---------------------------------------------------------------- an empty transcription still has its step


def model_steps(run_json, name):
    return [s for r in run_json["rows"] for s in r["trail"] if s["tag"] == "Model" and s["who"] == name]


def spend_in_trails(run_json):
    return sum(s.get("cost_usd") or 0 for r in run_json["rows"] for s in r["trail"] if s["tag"] == "Model")


def test_one_empty_region_among_successes_keeps_its_step_and_cost(tmp_path, picture_index):
    run_json, _, _ = run(tmp_path, picture_index, FakeSam(BOXES), Transcriber(empty=(1,)))
    RUN_SCHEMA.validate(run_json)
    row = run_json["rows"][0]
    steps = {s["step"]: s for s in row["trail"]}
    empty, full = steps["Transcribed region 1"], steps["Transcribed region 2"]
    assert picture.EMPTY_READ_REASON in empty["text"] and "“" not in empty["text"]
    assert empty["tokens"] and empty["cost_usd"] > 0, "the call was priced, so the step carries it"
    assert full["cost_usd"] == empty["cost_usd"] and "Phase" in full["text"]
    assert "Words rebuilt from the picture" in steps and "Words kept from the page's own text layer" not in steps
    assert run_json["sources"][0]["pages"][0]["kind"] == "picture"


def test_all_regions_empty_the_trail_carries_the_attempts_and_the_page_note_says_so(tmp_path, picture_index):
    run_json, run_dir, _ = run(tmp_path, picture_index, FakeSam(BOXES), Transcriber(empty=(1, 2)))
    RUN_SCHEMA.validate(run_json)
    page = run_json["sources"][0]["pages"][0]
    assert "kind" not in page and page["note"] == picture.ALL_EMPTY_NOTE
    assert run_json["rows"], "the page was still read, from its own words"
    for row in run_json["rows"]:
        names = [s["step"] for s in row["trail"]]
        assert names[1:5] == ["SAM 3.1 found 2 regions", "Transcribed region 1", "Transcribed region 2",
                              "Words kept from the page's own text layer"]
        assert all(picture.EMPTY_READ_REASON in s["text"] for s in row["trail"][2:4])
        assert "the picture gave no text" in row["trail"][4]["text"].lower()
        assert "Words rebuilt from the picture" not in names
        assert picture.ALL_EMPTY_NOTE in row["reason"] if row.get("reason") else True


def test_trail_costs_reconcile_with_the_run_spend_when_a_region_is_empty(tmp_path, picture_index):
    from runs import prices
    # the one call with no row to carry it: the second reader's whole-page read, which proposed nothing
    whole_page_read = prices.cost_usd(picture.READER_NAME, 10000, 5000)
    for n, empty in enumerate(((), (1,), (1, 2))):
        (tmp_path / f"e{n}").mkdir()
        run_json, _, _ = run(tmp_path / f"e{n}", picture_index, FakeSam(BOXES), Transcriber(empty=empty))
        for row in run_json["rows"]:
            assert len([s for s in row["trail"] if s["step"].startswith("Transcribed region")]) == 2, empty
        assert run_json["run"]["turns_total"] == 4 + 2 + 1 + 1, "an empty region is still a counted turn"
        assert spend_in_trails(run_json) + whole_page_read == pytest.approx(run_json["run"]["spend_usd"], abs=1e-5)
