"""Job 7 item 1: escalation to Muse Wonder when a check fails or the two readers disagree.

Every model here is a fake: no test reaches the network. Wonder is never trusted on its own: its answer is a new
proposal that goes through the same five checks, and only a proposal that passes all of them replaces anything.
"""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events
from capture.capture_source import load_entry
from capture.reader import Reply
from capture.wonder import MODEL as WONDER_MODEL
from capture.wonder import WonderReader
from runs import escalate, prices
from runs.state import current_state
from server import jobs
from server.app import create_app
from tests.conftest import FIXTURES, REPO
from tests.test_run_controls import FakeReader
from tests.test_second_reader import FakeSecond

RUN_SCHEMA = Draft202012Validator(json.loads((REPO / "runs" / "schema" / "run.schema.json").read_text()))
PIN = read_events.PIN
CHOICES = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": None,
           "spending_limit_usd": None, "note_for_agent": None}


def right_events():
    return json.loads(FakeReader.default("Protagonist"))["events"]


def wrong_date_events(n=1):
    """The first reader's events with the Q1 2027 event's date moved to a day that is not in Q1's first day."""
    ev = copy.deepcopy(right_events())
    for e in ev:
        if e["date_text"] == "Q1 2027":
            e["date"] = "2027-02-01"
    return ev


class FakeWonder:
    """Records the prompt and image of each call; answers with `answer(prompt, image, n)` (a list of events)."""

    def __init__(self, answer=None, tokens=(4000, 2000), problem=None, model=WONDER_MODEL):
        self.answer, self.tokens, self.problem, self.model = answer, tokens, problem, model
        self.prompts, self.images = [], []

    def ready(self):
        return self.problem

    def call(self, prompt, image=None):
        self.prompts.append(prompt)
        self.images.append(image)
        events = self.answer(prompt, image, len(self.prompts)) if self.answer else []
        return Reply(json.dumps({"events": events}), self.model, prompt_tokens=self.tokens[0],
                     completion_tokens=self.tokens[1])


def band_grid(box, y_range, words_doc):
    """A box in page points -> the 0-1000 grid over the band image Wonder is shown."""
    h = y_range[1] - y_range[0]
    return [box[0] * 1000 / words_doc["page_width"], (box[1] - y_range[0]) * 1000 / h,
            box[2] * 1000 / words_doc["page_width"], (box[3] - y_range[0]) * 1000 / h]


@pytest.fixture
def words_doc():
    return json.loads((FIXTURES / f"{PIN}.words.json").read_text())


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    return d


def start(runs_dir, first=None, second=None, wonder=None, **choices):
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=first or FakeReader(),
                                   second_reader=second, wonder_reader=wonder, background=False))
    r = client.post("/api/runs/new", json={**CHOICES, **choices})
    assert r.status_code == 202, r.text
    return client, r.json()["id"]


def run_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "run.json").read_text())


def job_of(runs_dir, rid):
    return json.loads((runs_dir / rid / "job.json").read_text())


def correct_answer(words_doc):
    """Wonder reads the band right: the real events, boxes given on the band's grid."""
    def answer(prompt, image, n):
        out = []
        for e in right_events():
            out.append(e)
        return out
    return answer


def reread_right(words_doc, runs_dir_getter=None):
    """An answer function for a re-read of the Q1 2027 row: the right event, its box on the band grid."""
    def answer(prompt, image, n):
        ev = next(e for e in right_events() if e["date_text"] == "Q1 2027")
        box = ev["box"]
        y_range = (max(0.0, box[1] - escalate.PAD_PT), min(words_doc["page_height"], box[3] + escalate.PAD_PT))
        return [{**ev, "box": band_grid(box, y_range, words_doc)}]
    return answer


# ---------------------------------------------------------------- the pieces


def test_the_wonder_reader_is_the_meta_model_api_call_to_the_listed_id():
    assert WONDER_MODEL == "muse-wonder-1.0"
    r = WonderReader()
    assert r.model == "muse-wonder-1.0" and r.name == "Muse Wonder"
    import inspect
    assert list(inspect.signature(r.call).parameters) == ["prompt", "image"]


def test_only_rows_with_a_failed_check_or_a_disagreement_are_candidates():
    ok = {"id": "e1", "status": "matched", "group": "kept", "checks": [{"result": "pass"}]}
    failed = {"id": "e2", "status": "failed", "group": "open", "checks": [{"result": "pass"}, {"result": "fail"}]}
    split = {"id": "e3", "status": "undecided", "group": "open", "checks": [{"result": "pass"}],
             "disagreement": {"field": "date", "first_reader": "a", "second_reader": "b"}}
    cov = {"id": "e4", "status": "undecided", "group": "open", "checks": [], "found_by": "coverage"}
    unread = {"id": "e5", "status": "unreadable", "group": "open", "checks": []}
    removed = {"id": "e6", "status": "failed", "group": "removed", "checks": [{"result": "fail"}]}
    unrun = {"id": "e7", "status": "proposed", "group": "unverified", "checks": [{"result": "could_not_run"}]}
    assert [r["id"] for r in escalate.candidates([ok, failed, split, cov, unread, removed, unrun])] == ["e2", "e3"]


def test_the_trigger_names_the_failed_check_or_the_disputed_field():
    failed = {"checks": [{"result": "pass", "label": "A", "reason": "x"},
                         {"result": "fail", "label": "Date read as written", "reason": "The period differs."}]}
    assert escalate.trigger(failed) == {"kind": "check", "label": "Date read as written",
                                        "why": "The period differs."}
    split = {"checks": [], "disagreement": {"field": "event_type", "first_reader": "milestone",
                                            "second_reader": "trial_results"}}
    t = escalate.trigger(split)
    assert t["kind"] == "disagreement" and t["label"] == "the two readers disagree on event_type"
    assert "Muse Spark read milestone and Muse Spark Open read trial_results." == t["why"]


def test_the_region_is_a_full_width_band_around_the_quote(words_doc, tmp_path):
    ev = next(e for e in right_events() if e["date_text"] == "Q1 2027")
    row = {"quote": ev["quote"], "box": None}
    y0, y1 = escalate.band(row, words_doc)
    quote_words = [w for w in words_doc["words"] if w["text"] in ev["quote"].split()]
    assert 0 <= y0 < y1 <= words_doc["page_height"]
    assert y1 - y0 < words_doc["page_height"] / 2, "a band, not the page"
    png = (REPO / "runs" / "ptgx-r4a" / "pages" / "p3.png").read_bytes()
    band = escalate.region_png(png, words_doc, (y0, y1))
    import pymupdf
    page = pymupdf.open(stream=png, filetype="png")[0]
    crop = pymupdf.Pixmap(band)
    assert band[:4] == b"\x89PNG" and crop.width == round(page.rect.width)
    assert crop.height == pytest.approx((y1 - y0) * page.rect.width / words_doc["page_width"], abs=2)


def test_with_no_quote_and_no_box_the_whole_page_is_the_region(words_doc):
    assert escalate.band({"quote": None, "box": None}, words_doc) == (0.0, words_doc["page_height"])
    y0, y1 = escalate.band({"quote": "not on the page", "box": [10, 300, 200, 320]}, words_doc)
    assert (y0, y1) == (300 - escalate.PAD_PT, 320 + escalate.PAD_PT)


def test_a_grid_box_on_the_band_becomes_a_box_on_the_page(words_doc):
    box = [60.0, 300.0, 500.0, 330.0]
    y_range = (240.0, 390.0)
    grid = band_grid(box, y_range, words_doc)
    back = escalate.to_page_points({"box": grid}, y_range, words_doc)["box"]
    assert back == pytest.approx(box, abs=0.05)
    assert escalate.to_page_points({"box": None}, y_range, words_doc)["box"] is None


def test_the_prompt_carries_the_failed_checks_label_and_reason_and_no_word_list(words_doc):
    entry = load_entry(PIN)
    trig = {"kind": "check", "label": "Date read as written", "why": "The period in the row differs."}
    text = escalate.prompt(entry, trig, (240.0, 390.0), words_doc)
    assert "Date read as written" in text and "The period in the row differs." in text
    assert "not the whole page" in text and "240 to 390 points" in text
    assert "\nw0|" not in text and "id|text|x0|y0|x1|y1" not in text


# ---------------------------------------------------------------- in a run


def test_a_row_that_fails_a_check_is_sent_to_wonder_and_takes_a_reread_that_passes_every_check(runs_dir, words_doc):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    wonder = FakeWonder(answer=reread_right(words_doc))
    _, rid = start(runs_dir, first, None, wonder)
    run = run_of(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    row = next(r for r in run["rows"] if r["date_text"] == "Q1 2027")
    assert (row["status"], row["group"]) == ("matched", "corrected")
    assert row["date"] == "2027-01-01" and all(c["result"] in ("pass", "not_needed") for c in row["checks"])
    assert row["reason"] == ("Re-read by Muse Wonder after Date read as written failed; its reading passed every "
                             "check.")
    steps = [(t["step"], t["tag"], t["who"]) for t in row["trail"] if t["step"] != "Checked"][-5:]
    assert steps == [("Sent to Muse Wonder because Date read as written failed", "Code", "runs.escalate"),
                     ("Instructions sent to Muse Wonder", "Code", "capture.wonder"),
                     ("Muse Wonder", "Model", "Muse Wonder"),
                     ("Escalation result", "Code", "runs.escalate")] or steps[-4:] == [
        ("Sent to Muse Wonder because Date read as written failed", "Code", "runs.escalate"),
        ("Instructions sent to Muse Wonder", "Code", "capture.wonder"),
        ("Muse Wonder", "Model", "Muse Wonder"), ("Escalation result", "Code", "runs.escalate")]
    model_step = next(t for t in row["trail"] if t["step"] == "Muse Wonder")
    assert model_step["tokens"]["prompt"] == 4000 and model_step["cost_usd"] > 0
    after_model = [t["step"] for t in row["trail"][row["trail"].index(model_step) + 1:]]
    assert after_model.count("Checked") == 5 and after_model[-1] == "Escalation result"
    assert run["run"]["escalation"] == {"model": "Muse Wonder", "status": "read", "rows_sent": 1, "rows_replaced": 1}
    [m] = run["mistakes"]
    assert (m["target"], m["caught_by"], m["type"]) == (row["id"], "code", "PERIOD_MISREAD")
    assert "date from 2027-02-01 to 2027-01-01" in m["what_changed"]
    other = next(r for r in run["rows"] if r["date_text"] != "Q1 2027")
    assert "Muse Wonder" not in json.dumps(other["trail"]), "the matched row was not sent"


def test_wonder_is_sent_the_rows_region_and_the_checks_reason_not_the_whole_page(runs_dir, words_doc):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    wonder = FakeWonder(answer=reread_right(words_doc))
    _, rid = start(runs_dir, first, None, wonder)
    [prompt], [png] = wonder.prompts, wonder.images
    assert "Date read as written" in prompt and "stopped by a code check" in prompt
    page = (runs_dir / rid / "pages" / "p3.png").read_bytes()
    assert png[:4] == b"\x89PNG" and 0 < len(png) < len(page)


def test_a_reread_that_fails_a_check_changes_nothing(runs_dir, words_doc):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    still_wrong = FakeWonder(answer=lambda prompt, image, n: [{**next(e for e in right_events()
                                                                       if e["date_text"] == "Q1 2027"),
                                                               "date": "2027-02-01", "box": [10, 10, 900, 60]}])
    _, rid = start(runs_dir, first, None, still_wrong)
    run = run_of(runs_dir, rid)
    row = next(r for r in run["rows"] if r["date_text"] == "Q1 2027")
    assert (row["status"], row["group"], row["date"]) == ("failed", "open", "2027-02-01")
    assert run["mistakes"] == []
    result = next(t for t in row["trail"] if t["step"] == "Escalation result")
    assert "did not pass every check" in result["text"] and result["text"].endswith("The row stays as it was.")
    assert run["run"]["escalation"]["rows_sent"] == 1 and run["run"]["escalation"]["rows_replaced"] == 0


def test_an_answer_with_no_event_changes_nothing(runs_dir):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    _, rid = start(runs_dir, first, None, FakeWonder())
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r["date_text"] == "Q1 2027")
    assert row["status"] == "failed"
    assert next(t for t in row["trail"] if t["step"] == "Escalation result")["text"].startswith(
        "Muse Wonder's answer held no event for this row.")


def test_a_row_the_readers_disagree_on_is_sent_but_only_a_person_settles_it(runs_dir, words_doc):
    events = right_events()
    other = copy.deepcopy(events)
    for e in other:
        if e["date_text"] == "Q1 2027":
            e["date"] = "2027-02-01"
    wonder = FakeWonder(answer=reread_right(words_doc))
    _, rid = start(runs_dir, FakeReader(), FakeSecond(answer=lambda p, n: json.dumps({"events": other})), wonder)
    run = run_of(runs_dir, rid)
    row = next(r for r in run["rows"] if r.get("disagreement"))
    assert (row["status"], row["group"]) == ("undecided", "open"), "nothing was replaced"
    assert row["disagreement"]["field"] == "date" and row["date"] == "2027-01-01"
    assert [t["step"] for t in row["trail"] if "Muse Wonder" in t["step"]] == [
        "Sent to Muse Wonder because the two readers disagree on date", "Instructions sent to Muse Wonder",
        "Muse Wonder"]
    result = next(t for t in row["trail"] if t["step"] == "Escalation result")
    assert result["text"].startswith("Muse Wonder read date as 2027-01-01, as Muse Spark did; its reading passed "
                                     "every check. A disagreement is settled only by a person's fix")
    assert run["mistakes"] == [] and current_state(run, [])["unresolved"] == 1


def test_nothing_to_send_means_no_call(runs_dir):
    wonder = FakeWonder()
    _, rid = start(runs_dir, FakeReader(), None, wonder)
    assert not wonder.prompts and "escalation" not in run_of(runs_dir, rid)["run"]
    assert job_of(runs_dir, rid)["escalated"] == {"src-ptgx-8k-2026-08-05-ex991": "nothing to send"}


def test_a_run_with_no_wonder_never_escalates(runs_dir):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    _, rid = start(runs_dir, first, None, None)
    run = run_of(runs_dir, rid)
    assert "escalation" not in run["run"] and run["mistakes"] == []


def test_no_wonder_key_is_not_run_and_the_run_still_finishes(runs_dir):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    _, rid = start(runs_dir, first, None, FakeWonder(problem="No model key was found, so nothing was read."))
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done"
    assert run["run"]["escalation"] == {"model": "Muse Wonder", "status": "not_run", "rows_sent": 0,
                                        "rows_replaced": 0, "reason": "No model key was found, so nothing was read."}


def test_at_most_three_rows_a_page_are_sent(runs_dir):
    many = [copy.deepcopy(e) for e in wrong_date_events() * 3]
    for i, e in enumerate(many):
        e["date"] = "2027-02-01"
        e["program"] = f"PN-{i}"
    wonder = FakeWonder()
    _, rid = start(runs_dir, FakeReader(answer=lambda p, n: json.dumps({"events": many})), None, wonder)
    assert len(wonder.prompts) == escalate.MAX_PER_PAGE == 3
    assert run_of(runs_dir, rid)["run"]["escalation"]["rows_sent"] == 3


def test_each_escalation_is_a_counted_priced_turn_and_resume_does_not_send_a_row_twice(runs_dir, monkeypatch):
    many = [copy.deepcopy(e) for e in wrong_date_events() * 3]
    for i, e in enumerate(many):
        e["date"] = "2027-02-01"
        e["program"] = f"PN-{i}"
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 2)        # the first read, then one escalation
    wonder = FakeWonder(tokens=(1_000_000, 500_000))
    client, rid = start(runs_dir, FakeReader(answer=lambda p, n: json.dumps({"events": many})), None, wonder)
    m = run_of(runs_dir, rid)["run"]
    assert (m["status"], m["turn_count"], len(wonder.prompts)) == ("paused_turn_limit", 2, 1)
    price = prices.PRICES_PER_1M_USD["Muse Wonder"]
    assert m["spend_usd"] == pytest.approx(0.01 + price["input"] + 0.5 * price["output"])
    assert client.post(f"/api/runs/{rid}/resume").status_code == 202
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "done" and m["resumes"] == 1
    assert len(wonder.prompts) == 3, "three rows a page in all, one call each, none repeated across the resume"
    assert len(job_of(runs_dir, rid)["escalated_rows"]) == 3 and m["escalation"]["rows_sent"] == 3


def test_the_spending_limit_covers_a_wonder_call(runs_dir):
    first = FakeReader(cost=0.60, answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    wonder = FakeWonder()
    _, rid = start(runs_dir, first, None, wonder, spending_limit_usd=0.5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and m["status_reason"].startswith("Stopped at the spending limit")
    assert not wonder.prompts


def test_a_wonder_model_with_no_price_stops_a_run_with_a_spending_limit(runs_dir):
    first = FakeReader(cost=0.01, answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    _, rid = start(runs_dir, first, None, FakeWonder(model="some-unpriced-model"), spending_limit_usd=5)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "needs_input" and "some-unpriced-model" in m["status_reason"]


def test_a_failed_wonder_call_stops_the_run_and_can_be_resumed(runs_dir, words_doc):
    class Failing(FakeWonder):
        def call(self, prompt, image=None):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return Reply(None, WONDER_MODEL, error="The model call did not succeed (APIError, status 503).")
            return super().call(prompt, image)

    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    wonder = Failing(answer=reread_right(words_doc))
    client, rid = start(runs_dir, first, None, wonder)
    m = run_of(runs_dir, rid)["run"]
    assert m["status"] == "failed" and m["status_reason"].startswith("Muse Wonder: The model call did not succeed")
    client.post(f"/api/runs/{rid}/resume")
    run = run_of(runs_dir, rid)
    assert run["run"]["status"] == "done" and run["run"]["escalation"]["rows_replaced"] == 1


def test_the_default_server_has_no_wonder_when_a_fake_reader_is_given(runs_dir):
    app = create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(), background=False)
    client = TestClient(app)
    rid = client.post("/api/runs/new", json=CHOICES).json()["id"]
    assert "escalation" not in run_of(runs_dir, rid)["run"], "no model but the fake one could have been called"


def test_the_band_may_hold_other_events_and_the_rows_own_is_the_one_taken(runs_dir, words_doc):
    """The live run: the band around the PN-8047 sentence also held the PN-458o sentence above it, and Wonder returned
    both. The row takes the event whose quote is its own, not the first one."""
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    own = reread_right(words_doc)

    def both(prompt, image, n):
        other = next(e for e in right_events() if e["date_text"] != "Q1 2027")
        return [{**other, "box": [10, 10, 900, 60]}, *own(prompt, image, n)]

    _, rid = start(runs_dir, first, None, FakeWonder(answer=both))
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r["date_text"] == "Q1 2027")
    assert (row["status"], row["date"], row["program"]) == ("matched", "2027-01-01", "PN-8047")


def test_several_events_none_of_them_this_rows_means_no_answer_for_it(runs_dir):
    first = FakeReader(answer=lambda p, n: json.dumps({"events": wrong_date_events()}))
    other = [e for e in right_events() if e["date_text"] != "Q1 2027"]
    elsewhere = [{**other[0]}, {**other[0], "program": "PN-1", "quote": "Something else is expected in Q4 2028."}]
    _, rid = start(runs_dir, first, None, FakeWonder(answer=lambda p, i, n: elsewhere))
    row = next(r for r in run_of(runs_dir, rid)["rows"] if r["date_text"] == "Q1 2027")
    assert row["status"] == "failed"
    assert "held no event for this row" in next(t for t in row["trail"] if t["step"] == "Escalation result")["text"]


def test_one_event_with_no_overlap_is_still_the_answer_when_the_rows_own_quote_was_the_problem():
    row = {"quote": "A quote the reader mangled."}
    only = {"quote": "The real sentence.", "program": "X"}
    assert escalate.pick(row, [only]) is only
    assert escalate.pick(row, [only, {"quote": "Another one."}]) is None
    assert escalate.pick({"quote": "The real sentence."}, [{"quote": "Another one."}, only]) is only


# ---------------------------------------------------------------- the saved live runs


def _no_keys_in(text):
    import os
    for name in ("BASETEN_API_KEY", "OPENROUTER_API_KEY", "MODEL_API_KEY"):
        assert not os.environ.get(name) or os.environ[name] not in text


def test_the_saved_live_wonder_escalation_of_the_ptgx_page():
    rec = json.loads((FIXTURES / f"{PIN}.wonder.json").read_text())
    assert rec["model"] == "muse-wonder-1.0" and rec["base_url"] == "https://api.meta.ai/v1"
    assert rec["demonstration"].startswith("The first reading of the PTGX page was ALTERED ON PURPOSE")
    assert rec["trigger"]["label"] == "Date read as written" and rec["input"] == "image"
    assert read_events.read_table.sha256_text(rec["prompt_text"]) == rec["prompt_sha256"]
    assert "Date read as written" in rec["prompt_text"] and "\nw0|" not in rec["prompt_text"]
    u = rec["usage"]
    assert isinstance(u["prompt_tokens"], int) and isinstance(u["completion_tokens"], int)
    assert rec["cost_usd_at_placeholder_price"] == prices.cost_usd("Muse Wonder", u["prompt_tokens"],
                                                                   u["completion_tokens"])
    own = next(e for e in rec["events"] if e["program"] == "PN-8047")
    assert (own["date"], own["date_end"]) == ("2027-01-01", "2027-03-31"), "Wonder read the printed period"
    # one code check refused the re-read: its box reaches below the quote's last line
    checks = {c["name"]: c for c in rec["proposal_checks"]}
    assert checks["selector_match"]["result"] == "fail" and "below the last line" in checks["selector_match"]["reason"]
    assert all(c["result"] == "pass" for n, c in checks.items() if n != "selector_match")
    assert rec["outcome"] == "kept" and rec["row_after"]["status"] == "failed" and rec["row_after"]["date"] == "2027-02-01"
    _no_keys_in(json.dumps(rec))


def test_the_saved_live_image_second_reading_of_the_ptgx_page():
    rec = json.loads((FIXTURES / f"{PIN}.events.second.json").read_text())
    assert rec["input"] == "image" and rec["model"] == "meta-models/muse-spark-open-1026" and rec["route"] == "baseten"
    assert rec["prompt_text"] == read_events.build_image_prompt(load_entry(PIN))
    assert "\nw0|" not in rec["prompt_text"] and "id|text|x0|y0|x1|y1" not in rec["prompt_text"], "no word list"
    assert [e["program"] for e in rec["events"]] == ["PN-458o", "PN-8047"]
    assert all(isinstance(e["box"], list) and len(e["box"]) == 4 and max(e["box"]) <= 1000 for e in rec["events"]), \
        "boxes on the 0-1000 grid"
    words = json.loads((FIXTURES / f"{PIN}.events.second_words.json").read_text())
    assert rec["usage"]["prompt_tokens"] < words["usage"]["prompt_tokens"], "the image costs fewer prompt tokens"
    _no_keys_in(json.dumps(rec))


def test_the_two_reader_run_records_that_the_second_reader_read_the_image():
    run = json.loads((REPO / "runs" / "ptgx-r5" / "run.json").read_text())
    RUN_SCHEMA.validate(run)
    assert run["run"]["second_reader"]["input"] == "image"
    for row in run["rows"]:
        step = next(t for t in row["trail"] if t["step"] == "Second reader input")
        assert "does not depend on the text layer" in step["text"]
        assert row["status"] == "matched" and "disagreement" not in row
