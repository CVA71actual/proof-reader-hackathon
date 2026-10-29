"""Job 7 item 3: a page with no text layer. SAM 3.1 proposes regions, the reader reads each region's image, and the box is
checked against the region instead of against words.

SAM 3.1 itself and the reader are fakes here: no test reaches the network. The live findings are checked through the
saved fixtures: on the real PTGX page SAM 3.1 matched nothing for "table" or "block of text" and answered only for "page".
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events, sam
from capture.capture_source import load_entry
from capture.reader import Reply
from checks.common import COULD_NOT_RUN, FAIL, PASS
from checks.selector_match import region_match
from runs import prices, recheck, scan
from server import jobs
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import FakeReader

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PIN = read_events.PIN
W, H = 612.0, 792.0           # the PTGX page in PDF points; its scan is 2x in pixels
IMG_W, IMG_H = 1224, 1584
CHOICES = {"companies": ["PTGX-SCAN"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}


def answer_line(*boxes, w=IMG_W, h=IMG_H):
    """What SAM 3.1 answers: one comma-separated line of box and mask records."""
    return ",".join(f"<0f>{i}<|box;x1={b[0]};y1={b[1]};x2={b[2]};y2={b[3]};w={w};h={h}|><|mask;x=0;y=0;data=QUJD|>"
                    for i, b in enumerate(boxes))


class FakeSam:
    """Answers each concept from a dict {concept: [pixel boxes]}; records the requests. Never touches the network."""
    model = "sam-3.1"

    def __init__(self, by_concept=None, problem=None, error_on=None):
        self.by_concept, self.problem, self.error_on = by_concept or {}, problem, error_on
        self.calls = []

    def ready(self):
        return self.problem

    def segment(self, image, concept):
        self.calls.append((concept, image))
        if concept == self.error_on:
            return Reply(None, "sam-3.1", error="The model call did not succeed (APIError, status 503).", images=0)
        return Reply(answer_line(*self.by_concept.get(concept, [])), "sam-3.1", images=1)


TABLE_PX, TEXT_PX = (120, 300, 1100, 900), (120, 1000, 1100, 1400)         # two regions, in image pixels
TWO_BOXES = {"table": [TABLE_PX], "block of text": [TEXT_PX]}


def event(program, date_text, date, date_end, quote, box):
    return {"ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.", "program": program, "indication": None,
            "event_type": "milestone", "title": f"{program} Phase 1 initiation", "date_text": date_text, "date": date,
            "date_end": date_end, "quote": quote, "box": box}


def region_reader(per_region):
    """A first reader that answers by the region it is shown: `per_region` = [events for r1, events for r2]."""
    def answer(prompt, n):
        return json.dumps({"events": per_region[n - 1]})
    return FakeReader(answer=answer)


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, first, sam_reader, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first, second_reader=None,
                                   wonder_reader=None, sam_reader=sam_reader, background=False))
    r = client.post("/api/runs/new", json={**CHOICES, **choices})
    assert r.status_code == 202, r.text
    return client, r.json()["id"]


def run_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def job_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "job.json").read_text())


# ---------------------------------------------------------------- what SAM 3.1 answers


def test_the_answer_line_is_parsed_into_boxes_and_the_masks_are_ignored():
    text = answer_line((5, 3, 1219, 1583), (10, 20, 30, 40))
    boxes = sam.parse_boxes(text)
    assert boxes == [{"x1": 5, "y1": 3, "x2": 1219, "y2": 1583, "w": IMG_W, "h": IMG_H},
                     {"x1": 10, "y1": 20, "x2": 30, "y2": 40, "w": IMG_W, "h": IMG_H}]


@pytest.mark.parametrize("text", ["", None, "no boxes here", "<0f>0<|box;x1=a;y1=1|>", answer_line((5, 5, 1, 9))])
def test_an_empty_or_unusable_answer_is_no_match_not_an_error(text):
    assert sam.parse_boxes(text) == []


def test_a_box_in_image_pixels_becomes_a_box_in_page_points_counting_the_last_pixel():
    box = {"x1": 120, "y1": 300, "x2": 1099, "y2": 899, "w": IMG_W, "h": IMG_H}      # x2, y2 inclusive
    assert sam.to_points(box, W, H) == [60.0, 150.0, 550.0, 450.0]


def test_the_sam_reader_is_the_responses_route_to_the_listed_id():
    assert sam.MODEL == "sam-3.1" and sam.CONCEPTS == ("table", "block of text") and sam.FALLBACK == "page"
    import inspect
    assert list(inspect.signature(sam.SamReader.segment).parameters) == ["self", "image", "concept"]


# ---------------------------------------------------------------- regions


def pts(px):
    return sam.to_points({"x1": px[0], "y1": px[1], "x2": px[2], "y2": px[3], "w": IMG_W, "h": IMG_H}, W, H)


def test_tables_come_first_a_text_block_inside_a_table_is_dropped_and_the_rest_read_top_to_bottom():
    table, text, inside = pts(TABLE_PX), pts(TEXT_PX), pts((200, 400, 900, 800))
    regions = scan.merge_regions({"block of text": [text, inside], "table": [table]})
    assert [(r["id"], r["concept"]) for r in regions] == [("r1", "table"), ("r2", "block of text")]
    assert regions[0]["box"] == table and regions[1]["box"] == text


def test_a_near_duplicate_box_is_dropped_and_regions_are_numbered_in_reading_order():
    low, high = pts((100, 1000, 1000, 1200)), pts((100, 100, 1000, 300))
    regions = scan.merge_regions({"block of text": [low, high, [x + 1 for x in high]]})
    assert [r["box"] for r in regions] == [high, low] and [r["id"] for r in regions] == ["r1", "r2"]


def test_the_whole_page_is_a_region_only_when_nothing_finer_was_found():
    page = [2.5, 1.5, 610.0, 792.0]
    assert scan.needs_fallback({"table": [], "block of text": []}) is True
    assert scan.needs_fallback({"table": [pts(TABLE_PX)], "block of text": []}) is False
    assert [r["concept"] for r in scan.merge_regions({"table": [], "block of text": [], "page": [page]})] == ["page"]
    both = scan.merge_regions({"table": [pts(TABLE_PX)], "page": [page]})
    assert [r["concept"] for r in both] == ["table"], "a table was found: the page is not added beside it"


def test_a_crop_is_the_regions_box_padded_and_kept_on_the_page():
    crop = scan.crop_box({"box": [1.0, 2.0, 600.0, 790.0]}, W, H)
    assert crop == [0.0, 0.0, W, H] or crop == [0.0, 0.0, 604.0 if 604.0 <= W else W, H]
    assert scan.crop_box({"box": [100.0, 100.0, 200.0, 200.0]}, W, H) == [96.0, 96.0, 204.0, 204.0]


def test_the_grid_box_over_a_crop_becomes_a_box_on_the_page():
    crop = [96.0, 96.0, 204.0, 196.0]
    out = scan.to_page_points({"box": [0, 0, 1000, 1000]}, crop)["box"]
    assert out == crop
    assert scan.to_page_points({"box": [500, 500, 1000, 1000]}, crop)["box"] == [150.0, 146.0, 204.0, 196.0]
    assert scan.to_page_points({"box": None}, crop)["box"] is None


def test_the_crop_png_is_the_part_of_the_page_image_inside_the_box():
    import pymupdf
    png = scan.scanned_png(load_entry(PIN))
    part = pymupdf.Pixmap(scan.crop_png(png, [100.0, 100.0, 300.0, 200.0], W))
    assert (part.width, part.height) == (400, 200), "2x: points to pixels"


def test_the_scanned_page_has_no_text_layer():
    import pymupdf
    png = scan.scanned_png(load_entry(PIN))
    doc = pymupdf.open(stream=png, filetype="png")
    assert doc[0].get_text().strip() == "" and png[:4] == b"\x89PNG"
    full = pymupdf.Pixmap(png)
    assert (full.width, full.height) == (IMG_W, IMG_H)


# ---------------------------------------------------------------- the check against a region


REGIONS = [{"id": "r1", "concept": "table", "box": [60.0, 150.0, 550.0, 450.0]},
           {"id": "r2", "concept": "block of text", "box": [60.0, 500.0, 550.0, 700.0]}]


def test_a_box_inside_a_region_passes_and_names_it():
    r = region_match([100, 200, 500, 300], REGIONS, W, H)
    assert (r.check, r.label, r.result) == ("selector_match", "Citation located", PASS)
    assert r.reason == "The box lies inside r1, a table region SAM 3.1 found."
    assert region_match([100, 520, 500, 600], REGIONS, W, H).reason.startswith("The box lies inside r2")
    assert region_match([59, 149, 551, 451], REGIONS, W, H).result == PASS, "a little slack at the edge"


def test_a_box_that_is_in_no_region_fails_and_a_box_across_two_regions_fails():
    assert region_match([100, 460, 500, 490], REGIONS, W, H).result == FAIL
    r = region_match([100, 300, 500, 600], REGIONS, W, H)
    assert r.result == FAIL and r.reason == "The box is not inside any region SAM 3.1 found on the page."


@pytest.mark.parametrize("box,result", [(None, COULD_NOT_RUN), ([1, 2, 3], FAIL), ([5, 5, 5, 9], FAIL),
                                        ([-9, -9, -1, -1], FAIL), ([700, 10, 800, 20], FAIL)])
def test_a_missing_or_unusable_box_is_not_a_pass(box, result):
    assert region_match(box, REGIONS, W, H).result == result


def test_with_no_region_the_check_could_not_run():
    assert region_match([100, 200, 500, 300], [], W, H).result == COULD_NOT_RUN
    assert region_match([100, 200, 500, 300], None, W, H).result == COULD_NOT_RUN


def test_a_whole_page_region_says_nothing_about_where_the_quote_is_so_it_never_passes():
    page = [{"id": "r1", "concept": "page", "box": [0.0, 0.0, W, H]}]
    r = region_match([100, 200, 500, 300], page, W, H)
    assert r.result == COULD_NOT_RUN and "only region is the whole page" in r.reason
    assert region_match([700, 10, 800, 20], page, W, H).result == FAIL, "off the page is still wrong"


def test_the_row_checks_use_the_region_on_a_scan_and_the_text_layer_otherwise():
    row = {"id": "e1", "quote": "Phase 1 initiation is anticipated in Q1 2027.", "box": [100, 200, 500, 300],
           "date_text": "Q1 2027", "date": "2027-01-01", "date_end": "2027-03-31", "event_type": "milestone",
           "title": "x", "program": "PN-1", "page_number": 3}
    scanned = {"kind": "filing", "pages": [{"page_number": 3, "scanned": True, "regions": REGIONS}]}
    empty = {"words": [], "page_width": W, "page_height": H}
    got = {c.check: c for c in recheck.row_checks(row, scanned, empty)}
    assert got["quote_match"].result == COULD_NOT_RUN and "no text layer" in got["quote_match"].reason
    assert got["selector_match"].result == PASS and "SAM 3.1 found" in got["selector_match"].reason
    plain = {"kind": "filing", "pages": [{"page_number": 3}]}
    no_text = {c.check: c for c in recheck.row_checks(row, plain, empty)}["selector_match"]
    assert no_text.result == COULD_NOT_RUN and "a picture, not text" in no_text.reason, \
        "no regions and no words: nothing is printed there, so the words are a picture"
    not_captured = {c.check: c for c in recheck.row_checks(row, scanned, None)}
    assert "SAM 3.1" not in not_captured["selector_match"].reason, "a page that was not captured has no regions to use"


# ---------------------------------------------------------------- in a run


def quote_events(box=(100, 100, 900, 300)):
    q1 = "PN-8047 (oral small molecule hepcidin functional mimetic): IND-enabling studies are ongoing, with Phase 1 initiation anticipated in Q1 2027."
    q2 = "PN-458o (dual GLP/GIP agonist peptide): IND-enabling studies are ongoing with Phase 1 initiation anticipated in the second half of 2027."
    return (event("PN-8047", "Q1 2027", "2027-01-01", "2027-03-31", q1, list(box)),
            event("PN-458o", "the second half of 2027", "2027-07-01", "2027-12-31", q2, list(box)))


def test_two_regions_are_read_one_by_one_and_each_row_is_checked_against_its_own_region(runs_dir):
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    fake = FakeSam(TWO_BOXES)
    client, rid = start(runs_dir, first, fake)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert run["run"]["status"] == "done", run["run"]["status_reason"]
    assert [c for c, _ in fake.calls] == ["table", "block of text"], "the page was not asked for: regions were found"
    page = run["sources"][0]["pages"][0]
    assert page["scanned"] is True and [(r["id"], r["concept"]) for r in page["regions"]] == [
        ("r1", "table"), ("r2", "block of text")]
    assert (runs_dir / rid / page["words"]).read_text().count('"words": []') == 1, "no text layer"
    assert len(first.prompts) == 2 and "one region of a scanned page (a table that SAM 3.1 found)" in first.prompts[0]
    assert "a block of text that SAM 3.1 found" in first.prompts[1]
    assert first.images[0] is not None and first.images[0][:4] == b"\x89PNG"
    for row in run["rows"]:
        got = {c["name"]: c["result"] for c in row["checks"]}
        assert got == {"quote_match": "could_not_run", "selector_match": "pass", "date_match": "pass",
                       "source_rule": "pass", "type_match": "pass"}
        assert (row["status"], row["group"]) == ("proposed", "unverified"), "a scan is never matched"
        assert "The page is a scan" in row["reason"]
    assert run["rows"][0]["checks"][1]["reason"].startswith("The box lies inside r1, a table region")
    assert run["rows"][1]["checks"][1]["reason"].startswith("The box lies inside r2, a block of text region")


def test_the_trail_names_the_scanned_page_and_sam_as_a_model_step_with_its_image_cost(runs_dir):
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    _, rid = start(runs_dir, first, FakeSam(TWO_BOXES))
    row = run_of(runs_dir, rid)["rows"][0]
    steps = [(t["step"], t["tag"], t["who"]) for t in row["trail"][:4]]
    assert steps == [("Instructions sent", "Code", "capture.read_events"), ("Scanned page", "Code", "runs.scan"),
                     ("SAM 3.1 found r1", "Model", "SAM 3.1"), ("Proposed", "Model", "Muse Spark")]
    sam_step = row["trail"][2]
    assert sam_step["tokens"] is None and sam_step["cost_usd"] == pytest.approx(2 * 0.0025 / 2)
    assert "a table at" in sam_step["text"] and "2 images processed" in sam_step["text"]
    assert "no text layer" in row["trail"][1]["text"]


def test_each_sam_request_and_each_region_read_is_a_counted_priced_turn(runs_dir):
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    _, rid = start(runs_dir, first, FakeSam(TWO_BOXES))
    m = run_of(runs_dir, rid)["run"]
    assert m["turn_count"] == 4, "two SAM requests and two region reads"
    assert m["spend_usd"] == pytest.approx(2 * prices.PRICES_PER_IMAGE_USD["SAM 3.1"] + 2 * 0.01)


def test_when_sam_matches_neither_concept_the_whole_page_is_asked_for_and_read(runs_dir):
    first = region_reader([list(quote_events())])
    fake = FakeSam({"page": [(4, 0, 1219, 1583)]})
    _, rid = start(runs_dir, first, fake)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert [c for c, _ in fake.calls] == ["table", "block of text", "page"]
    [region] = run["sources"][0]["pages"][0]["regions"]
    assert region["concept"] == "page"
    assert "SAM 3.1 found no table or text block on the scanned page" in first.prompts[0] or \
        "SAM 3.1 found no table or text block on it" in first.prompts[0]
    for row in run["rows"]:
        sel = next(c for c in row["checks"] if c["name"] == "selector_match")
        assert sel["result"] == "could_not_run" and "only region is the whole page" in sel["reason"]
        assert row["status"] == "proposed"
        assert row["trail"][1]["text"].startswith("SAM 3.1 found no table or text block on this page")
    assert run["run"]["turn_count"] == 4


def test_a_second_row_for_a_sentence_read_from_two_overlapping_regions_is_kept_once(runs_dir):
    both = list(quote_events())
    first = region_reader([[both[0]], [both[0], both[1]]])
    _, rid = start(runs_dir, first, FakeSam(TWO_BOXES))
    quotes = [r["quote"] for r in run_of(runs_dir, rid)["rows"]]
    assert len(quotes) == 2 and len(set(quotes)) == 2


def test_a_box_outside_its_region_fails_the_citation_check(runs_dir):
    stray = (10, 10, 60, 40)           # the top-left corner of the crop, far from where the region's text is... in the crop
    events = quote_events(box=(0, 0, 1000, 1000))   # the whole crop: reaches past the region by the crop's padding
    first = region_reader([[events[0]], [events[1]]])
    _, rid = start(runs_dir, first, FakeSam(TWO_BOXES))
    row = run_of(runs_dir, rid)["rows"][0]
    sel = next(c for c in row["checks"] if c["name"] == "selector_match")
    assert sel["result"] == "fail" and sel["reason"] == "The box is not inside any region SAM 3.1 found on the page."
    assert row["status"] == "failed"


def test_no_sam_reader_or_no_key_is_needs_input_and_nothing_is_read(runs_dir):
    first = region_reader([[], []])
    _, rid = start(runs_dir, first, None)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and m["status_reason"] == "No SAM 3.1 reader is available, so a scanned page cannot be read."
    _, rid2 = start(runs_dir, first, FakeSam(problem="No model key was found, so nothing was read."))
    assert run_of(runs_dir, rid2)["run"]["status_reason"] == "No model key was found, so nothing was read."
    assert not first.prompts


def test_a_failed_sam_call_stops_the_run_and_resume_does_not_repeat_the_calls_that_succeeded(runs_dir):
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    fake = FakeSam(TWO_BOXES, error_on="block of text")
    client, rid = start(runs_dir, first, fake)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "failed" and m["status_reason"].startswith("SAM 3.1: The model call did not succeed")
    fake.error_on = None
    client.post(f"/api/runs/{rid}/resume")
    assert run_of(runs_dir, rid)["run"]["status"] == "done"
    assert [c for c, _ in fake.calls] == ["table", "block of text", "block of text"], "'table' was not asked twice"


def test_the_turn_limit_pauses_a_scan_and_resume_finishes_it_without_repeating_any_call(runs_dir, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 3)
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    fake = FakeSam(TWO_BOXES)
    client, rid = start(runs_dir, first, fake)
    assert run_of(runs_dir, rid)["run"]["status"] == "paused_turn_limit"
    assert len(fake.calls) == 2 and len(first.prompts) == 1
    client.post(f"/api/runs/{rid}/resume")
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and len(run["rows"]) == 2
    assert len(fake.calls) == 2 and len(first.prompts) == 2, "the saved answers were used"


def test_the_spending_limit_covers_sam(runs_dir):
    first = region_reader([[quote_events()[0]], [quote_events()[1]]])
    fake = FakeSam(TWO_BOXES)
    _, rid = start(runs_dir, first, fake, spending_limit_usd=0.001)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and m["status_reason"].startswith("Stopped at the spending limit")
    assert len(fake.calls) == 1 and not first.prompts


def test_a_sam_model_with_no_price_stops_a_run_with_a_spending_limit(runs_dir):
    class Unpriced(FakeSam):
        model = "some-unpriced-segmenter"

    first = region_reader([[], []])
    _, rid = start(runs_dir, first, Unpriced(TWO_BOXES), spending_limit_usd=5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and "some-unpriced-segmenter" in m["status_reason"]


def test_a_scanned_page_is_skipped_when_no_event_type_is_chosen(runs_dir):
    first, fake = region_reader([[], []]), FakeSam(TWO_BOXES)
    _, rid = start(runs_dir, first, fake, event_types=[])
    assert not fake.calls and not first.prompts
    assert run_of(runs_dir, rid)["run"]["status_reason"].startswith("Nothing to read")


def test_the_default_server_with_a_fake_reader_has_no_sam(runs_dir):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(), background=False))
    rid = client.post("/api/runs/new", json=CHOICES).json()["id"]
    assert run_of(runs_dir, rid)["run"]["status"] == "needs_input"


def test_the_new_run_choices_accept_the_scanned_page_company():
    assert jobs.validate_choices(CHOICES)["companies"] == ["PTGX-SCAN"]
    assert jobs.price_name("sam-3.1") == "SAM 3.1" and prices.has_price("SAM 3.1") and not prices.has_price("sam")
    assert prices.image_cost_usd("SAM 3.1", 1000) == pytest.approx(2.5)
    assert prices.image_cost_usd("SAM 3.1", None) is None and prices.image_cost_usd("Muse Spark", 3) is None


# ---------------------------------------------------------------- the saved live run


def test_the_saved_live_sam_answers_on_the_scanned_ptgx_page():
    rec = json.loads((FIXTURES / f"{PIN}.sam.json").read_text())
    assert rec["model"] == "sam-3.1" and rec["base_url"] == "https://api.meta.ai/v1"
    assert rec["input"] == "page image rendered with no text layer"
    assert rec["image_sha256"] == scan.sha256(scan.scanned_png(load_entry(PIN)))
    c = rec["concepts"]
    assert c["table"]["boxes"] == [] and c["block of text"]["boxes"] == [], "matched nothing: an object segmenter"
    [page] = c["page"]["boxes"]
    assert (page["w"], page["h"]) == (IMG_W, IMG_H) and page["x2"] > 1200 and page["y2"] > 1500
    assert rec["images_processed"] == 3 and rec["cost_usd"] == pytest.approx(0.0075)


def test_the_committed_scan_run_is_what_the_builder_makes(tmp_path):
    entry = load_entry(PIN)
    run = json.loads((REPO / "runs" / "ptgx-scan" / "run.json").read_text())
    sam_record = json.loads((FIXTURES / f"{PIN}.sam.json").read_text())
    read_record = json.loads((FIXTURES / f"{PIN}.scan_reading.json").read_text())
    fresh = scan.assemble(entry, tmp_path / "ptgx-scan", scan.scanned_png(entry), sam_record, read_record,
                          finished_at=run["run"]["finished_at"])
    assert fresh == run
    RUN_SCHEMA.validate(run)


def test_the_committed_scan_run_says_what_happened_and_never_calls_a_scan_matched():
    run = json.loads((REPO / "runs" / "ptgx-scan" / "run.json").read_text())
    page = run["sources"][0]["pages"][0]
    assert page["scanned"] is True and [r["concept"] for r in page["regions"]] == ["page"]
    assert len(run["rows"]) == 2
    for row in run["rows"]:
        got = {c["name"]: c["result"] for c in row["checks"]}
        assert got["quote_match"] == "could_not_run" and got["selector_match"] == "could_not_run"
        assert row["status"] == "proposed" and row["group"] == "unverified"
        assert [t["step"] for t in row["trail"][:3]] == ["Instructions sent", "Scanned page", "SAM 3.1 found r1"]
    reads = json.loads((FIXTURES / f"{PIN}.scan_reading.json").read_text())["reads"]
    assert len(reads) == 1 and reads[0]["usage"]["prompt_tokens"] > 0 and reads[0]["model"].startswith("muse-spark")
    import os
    text = json.dumps(reads) + (FIXTURES / f"{PIN}.sam.json").read_text()
    assert not os.environ.get("MODEL_API_KEY") or os.environ["MODEL_API_KEY"] not in text
