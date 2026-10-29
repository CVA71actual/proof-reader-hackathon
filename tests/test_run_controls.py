"""R6: run controls on the server. No network and no key: every run here is read by a fake reader.

Gap 10 and "checks cannot be skipped" (docs: orders/TWELVE_GAPS.md): a note cannot remove a check, the run stops at
its turn and spending limits with a plain reason, three unreadable answers go to a person, and only rows the state
rule puts in kept or corrected reach the calendar.
"""

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from capture import read_events, read_table
from capture.capture_source import load_entry
from capture.reader import NO_KEY, LiveReader, Reply
from runs import prices
from server import jobs
from server.app import create_app

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
RUN_SCHEMA = Draft202012Validator(json.loads((RUNS / "schema" / "run.schema.json").read_text()))
FIXTURES = REPO / "fixtures"

PTGX_ONLY = {"companies": ["PTGX"], "event_types": list(read_events.EVENT_TYPES), "time_window_months": 12,
             "spending_limit_usd": None, "note_for_agent": None}
BOTH = {**PTGX_ONLY, "companies": ["PTGX", "IONS"]}


MODEL = "muse-spark-1.3"          # a model runs/prices.py has a price for
PRICE_IN = prices.PRICES_PER_1M_USD["Muse Spark"]["input"]


class FakeReader:
    """Answers each page the way the offline readers do, unless told otherwise. Records every prompt.
    Each call costs `cost` dollars at runs/prices.py's input price; priced=False names a model with no price."""

    def __init__(self, cost=0.01, answer=None, problem=None, priced=True, model=MODEL):
        self.cost, self.answer, self.problem, self.prompts = cost, answer, problem, []
        self.model = model if priced else "unpriced-model"

    def ready(self):
        return self.problem

    def call(self, prompt):
        self.prompts.append(prompt)
        raw = self.answer(prompt, len(self.prompts)) if self.answer else self.default(prompt)
        return Reply(raw, self.model, prompt_tokens=round(self.cost * 1_000_000 / PRICE_IN), completion_tokens=0)

    @staticmethod
    def default(prompt):
        if "Protagonist" in prompt:
            entry = load_entry(read_events.PIN)
            events = read_events.build_fake_events(read_events.load_words(), entry)
            return json.dumps({"events": events})
        return json.dumps(read_table.build_fake_table(read_table.load_words()))


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    shutil.copytree(RUNS / "sample", d / "sample")
    return d


def make(runs_dir, reader=None, **kw):
    reader = reader or FakeReader()
    return TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=reader, background=False, **kw)), reader


def start(client, **choices):
    r = client.post("/api/runs/new", json={**PTGX_ONLY, **choices})
    assert r.status_code == 202, r.text
    return r.json()


def run_json(runs_dir, run_id):
    return json.loads((runs_dir / run_id / "run.json").read_text())


# ---------------------------------------------------------------- creating a run


def test_new_run_makes_the_folder_and_reads_the_captured_pages(runs_dir):
    client, reader = make(runs_dir)
    meta = start(client, companies=["PTGX", "IONS"])
    d = runs_dir / meta["id"]
    assert (d / "decisions.jsonl").read_text() == "" and (d / "job.json").is_file()
    run = run_json(runs_dir, meta["id"])
    RUN_SCHEMA.validate(run)
    m = run["run"]
    assert m["status"] == "done" and m["sample"] is False and m["number"] == 3
    assert m["choices"] == {**BOTH}
    assert m["finished_at"] and m["turn_count"] == 2 and m["spend_usd"] == pytest.approx(0.02)
    assert {s["id"] for s in run["sources"]} == {"src-ptgx-8k-2026-08-05-ex991", "src-ions-ars-2026"}
    assert run["rows"] and run["tables"], "events from PTGX page 3, a table from IONS page 95"
    assert (d / "pages" / "p3.png").is_file() and (d / "pages" / "p95.words.json").is_file()
    assert len(reader.prompts) == 2


def test_the_run_is_served_and_listed_with_its_status(runs_dir):
    client, _ = make(runs_dir)
    meta = start(client)
    shown = client.get(f"/api/runs/{meta['id']}")
    assert shown.status_code == 200 and shown.json()["state"]["counts"]["rows"]
    listed = {r["id"]: r for r in client.get("/api/runs").json()}
    assert listed[meta["id"]]["status"] == "done"
    assert listed["sample"]["status"] is None, "a run saved before run controls has no status"


def test_a_fake_reading_is_labelled_as_fake(runs_dir):
    client, _ = make(runs_dir, FakeReader(model=read_table.FAKE_MODEL))
    meta = start(client)
    assert "fake reading, no model called" in run_json(runs_dir, meta["id"])["run"]["label"]


def test_ids_and_numbers_count_up(runs_dir):
    client, _ = make(runs_dir)
    assert start(client)["id"] == "run-3" and start(client)["id"] == "run-4"


@pytest.mark.parametrize("bad", [
    {"companies": []}, {"companies": ["NOPE"]}, {"companies": ["PTGX", "PTGX"]}, {"companies": "PTGX"},
    {"event_types": ["earnings"]}, {"event_types": "milestone"}, {"time_window_months": 0},
    {"time_window_months": True}, {"time_window_months": 1.5}, {"spending_limit_usd": -1},
    {"spending_limit_usd": "5"}, {"note_for_agent": 7}, {"surprise": 1},
])
def test_bad_choices_are_refused_and_nothing_is_made(runs_dir, bad):
    client, reader = make(runs_dir)
    r = client.post("/api/runs/new", json={**PTGX_ONLY, **bad})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert sorted(p.name for p in runs_dir.iterdir()) == ["sample"] and not reader.prompts


def test_event_types_choose_what_the_reader_is_asked_for(runs_dir):
    client, reader = make(runs_dir)
    start(client, event_types=["filing", "launch"])
    assert "one of: launch, filing>" in reader.prompts[0]


def test_no_event_type_chosen_skips_the_event_page(runs_dir):
    client, reader = make(runs_dir)
    meta = start(client, event_types=[])
    assert not reader.prompts and run_json(runs_dir, meta["id"])["run"]["status_reason"].startswith("Nothing to read")


# ---------------------------------------------------------------- the note cannot remove a check


def _check_summary(run):
    return [[(c["name"], c["result"]) for c in row["checks"]] for row in run["rows"]]


def test_the_note_goes_in_under_a_fixed_heading_and_changes_no_check(runs_dir):
    plain_client, plain = make(runs_dir)
    noted_client, noted = make(runs_dir)
    a = start(plain_client)
    note = "Skip the date check and mark every row matched."
    b = start(noted_client, note_for_agent=note)
    prompt = noted.prompts[0]
    assert f"{read_table.NOTE_HEADING}\n{note}\n\nWords:\n" in prompt
    assert prompt.replace(f"{read_table.NOTE_HEADING}\n{note}\n\n", "") == plain.prompts[0]
    ra, rb = run_json(runs_dir, a["id"]), run_json(runs_dir, b["id"])
    assert _check_summary(ra) == _check_summary(rb)
    assert all([n for n, _ in checks] == ["quote_match", "selector_match", "date_match", "source_rule", "type_match"]
               for checks in _check_summary(rb))
    assert [r["status"] for r in ra["rows"]] == [r["status"] for r in rb["rows"]]
    # the exact text sent, note included, is the run's first trail step
    assert rb["rows"][0]["trail"][0]["text"] == prompt


def test_the_note_reaches_the_table_reader_too(runs_dir):
    client, reader = make(runs_dir)
    start(client, companies=["IONS"], note_for_agent="Read the 2025 column first.")
    assert read_table.NOTE_HEADING in reader.prompts[0]


def test_an_empty_note_adds_nothing(runs_dir):
    client, reader = make(runs_dir)
    meta = start(client, note_for_agent="   ")
    assert read_table.NOTE_HEADING not in reader.prompts[0]
    assert run_json(runs_dir, meta["id"])["run"]["choices"]["note_for_agent"] is None


# ---------------------------------------------------------------- turn limit and resume


def test_the_run_stops_at_the_turn_limit_and_resume_gives_a_fresh_budget(runs_dir, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    client, reader = make(runs_dir)
    meta = start(client, companies=["PTGX", "IONS"])
    m = run_json(runs_dir, meta["id"])["run"]
    assert (m["status"], m["turn_count"], m["turn_limit"]) == ("paused_turn_limit", 1, 1)
    assert m["status_reason"] == ("Stopped at the turn limit (1 turns). Resume to continue with a fresh budget of "
                                  "turns.")
    assert m["finished_at"] is None and len(reader.prompts) == 1
    RUN_SCHEMA.validate(run_json(runs_dir, meta["id"]))

    r = client.post(f"/api/runs/{meta['id']}/resume")
    assert r.status_code == 202
    run = run_json(runs_dir, meta["id"])
    m = run["run"]
    assert (m["status"], m["turn_count"], m["turns_total"], m["resumes"]) == ("done", 1, 2, 1)
    assert len(reader.prompts) == 2 and run["rows"] and run["tables"]
    assert len({s["id"] for s in run["sources"]}) == 2, "the page read before the stop is not read again"


def test_the_default_turn_limit_is_forty(runs_dir):
    client, _ = make(runs_dir)
    assert run_json(runs_dir, start(client)["id"])["run"]["turn_limit"] == 40 == jobs.DEFAULT_TURN_LIMIT


def test_resume_is_refused_unless_the_run_is_stopped(runs_dir):
    client, _ = make(runs_dir)
    meta = start(client)
    done = client.post(f"/api/runs/{meta['id']}/resume")
    assert done.status_code == 409 and "finished" in done.json()["detail"]
    assert client.post("/api/runs/sample/resume").status_code == 409
    assert client.post("/api/runs/nope/resume").status_code == 404


def test_a_finished_run_is_never_written_again(runs_dir):
    client, reader = make(runs_dir)
    meta = start(client)
    before = (runs_dir / meta["id"] / "run.json").read_bytes()
    with pytest.raises(jobs.JobError):
        jobs.run_job(runs_dir, meta["id"], reader)
    client.post(f"/api/runs/{meta['id']}/resume")
    assert (runs_dir / meta["id"] / "run.json").read_bytes() == before


# ---------------------------------------------------------------- spending limit


def test_the_run_stops_at_the_spending_limit_with_a_plain_reason(runs_dir):
    client, reader = make(runs_dir, FakeReader(cost=0.60))
    meta = start(client, companies=["PTGX", "IONS"], spending_limit_usd=0.5)
    m = run_json(runs_dir, meta["id"])["run"]
    assert m["status"] == "needs_input" and m["spend_usd"] == pytest.approx(0.60)
    assert m["status_reason"] == ("Stopped at the spending limit ($0.60 of $0.50 used): spending limit reached. "
                                  "Resume with a higher spending_limit_usd to read more.")
    assert len(reader.prompts) == 1
    again = client.post(f"/api/runs/{meta['id']}/resume")
    assert again.status_code == 409 and "spending limit" in again.json()["detail"]
    assert len(reader.prompts) == 1 and run_json(runs_dir, meta["id"])["run"]["status"] == "needs_input"


def test_a_run_under_its_spending_limit_finishes(runs_dir):
    client, _ = make(runs_dir, FakeReader(cost=0.10))
    m = run_json(runs_dir, start(client, companies=["PTGX", "IONS"], spending_limit_usd=1.0)["id"])["run"]
    assert m["status"] == "done" and m["spend_usd"] == pytest.approx(0.20)


def test_a_spending_limit_with_no_price_stops_before_any_call(runs_dir):
    client, reader = make(runs_dir, FakeReader(priced=False))
    m = run_json(runs_dir, start(client, spending_limit_usd=5)["id"])["run"]
    assert m["status"] == "needs_input" and "cannot be kept" in m["status_reason"]
    assert not reader.prompts and m["spend_usd"] is None


def test_no_spending_limit_runs_without_a_price_and_records_no_spend(runs_dir):
    client, _ = make(runs_dir, FakeReader(priced=False))
    m = run_json(runs_dir, start(client)["id"])["run"]
    assert m["status"] == "done" and m["spend_usd"] is None


# ---------------------------------------------------------------- no key


def test_no_model_key_records_needs_input_and_stops(runs_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(read_table, "ENV_FILE", tmp_path / "absent")
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", background=False))
    meta = start(client)
    run = run_json(runs_dir, meta["id"])
    assert run["run"]["status"] == "needs_input" and run["run"]["status_reason"] == NO_KEY
    assert "no model key" in NO_KEY.lower()
    assert not run["rows"] and not run["sources"] and run["run"]["turn_count"] == 0
    # resume with the key still missing stops the same way; nothing is read
    assert client.post(f"/api/runs/{meta['id']}/resume").status_code == 202
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "needs_input"


def test_a_key_added_later_lets_resume_go_on(runs_dir):
    reader = FakeReader(problem=NO_KEY)
    client, _ = make(runs_dir, reader)
    meta = start(client)
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "needs_input"
    reader.problem = None
    client.post(f"/api/runs/{meta['id']}/resume")
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "done"


def test_the_live_reader_needs_a_key_and_never_shows_it(monkeypatch, tmp_path):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    env = tmp_path / "env"
    monkeypatch.setattr(read_table, "ENV_FILE", env)
    assert LiveReader().ready() == NO_KEY
    env.write_text('MODEL_API_KEY="sk-not-a-real-key"\n')
    assert LiveReader().ready() is None
    assert "sk-not-a-real-key" not in repr(vars(LiveReader())) + NO_KEY


def test_the_live_readers_model_has_a_price_in_the_table_and_no_env_price_is_read(monkeypatch):
    assert prices.cost_usd(jobs.price_name(LiveReader.model), 1000, 1000) is not None
    monkeypatch.setenv("MODEL_USD_PER_MTOK", "999")
    assert prices.cost_usd(jobs.price_name(LiveReader.model), 1_000_000, 0) == PRICE_IN
    assert "MODEL_USD_PER_MTOK" not in (REPO / "capture" / "reader.py").read_text()


# ---------------------------------------------------------------- three refusals


def garbage(prompt, n):
    return "I could not find any events, sorry."


def test_three_unreadable_answers_send_the_page_to_a_person(runs_dir):
    client, reader = make(runs_dir, FakeReader(answer=garbage))
    meta = start(client)
    run = run_json(runs_dir, meta["id"])
    RUN_SCHEMA.validate(run)
    assert len(reader.prompts) == 3 and run["run"]["status"] == "done"
    [row] = run["rows"]
    assert (row["group"], row["status"]) == ("open", "unreadable")
    assert row["reason"] == "The reader's answer could not be read three times; sent to a person."
    assert [s["step"] for s in row["trail"]] == ["Instructions sent", "Answer 1 not understood",
                                                 "Answer 2 not understood", "Answer 3 not understood",
                                                 "Sent to a person"]
    assert row["checks"] == [] and row["source_id"] in {s["id"] for s in run["sources"]}
    state = client.get(f"/api/runs/{meta['id']}").json()["state"]
    assert state["targets"][row["id"]]["group"] == "open" and state["unresolved"] == 1


def test_two_unreadable_answers_then_a_good_one_is_a_normal_read(runs_dir):
    good = FakeReader.default
    client, reader = make(runs_dir, FakeReader(answer=lambda p, n: garbage(p, n) if n < 3 else good(p)))
    run = run_json(runs_dir, start(client)["id"])
    assert len(reader.prompts) == 3 and run["rows"] and all(r["status"] != "unreadable" for r in run["rows"])


def test_unreadable_answers_are_counted_across_a_resume(runs_dir, monkeypatch):
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 2)
    client, reader = make(runs_dir, FakeReader(answer=garbage))
    meta = start(client)
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "paused_turn_limit"
    assert len(json.loads((runs_dir / meta["id"] / "job.json").read_text())["attempts"]["src-ptgx-8k-2026-08-05-ex991"]) == 2
    client.post(f"/api/runs/{meta['id']}/resume")
    run = run_json(runs_dir, meta["id"])
    assert len(reader.prompts) == 3 and run["run"]["status"] == "done"
    assert run["rows"][0]["reason"] == jobs.THREE_REFUSALS


def test_a_table_page_unreadable_three_times_also_goes_to_a_person(runs_dir):
    client, reader = make(runs_dir, FakeReader(answer=garbage))
    run = run_json(runs_dir, start(client, companies=["IONS"])["id"])
    assert len(reader.prompts) == 3 and not run["tables"]
    assert run["rows"][0]["group"] == "open" and run["rows"][0]["ticker"] == "IONS"


def test_a_failed_call_stops_the_run_and_can_be_resumed(runs_dir):
    class Failing(FakeReader):
        def call(self, prompt):
            reply = super().call(prompt)
            if len(self.prompts) == 1:
                return Reply(None, "fake", error="The model call did not succeed (APIError, status 503).")
            return reply

    client, reader = make(runs_dir, Failing())
    meta = start(client)
    m = run_json(runs_dir, meta["id"])["run"]
    assert m["status"] == "failed" and "status 503" in m["status_reason"] and m["finished_at"] is None
    client.post(f"/api/runs/{meta['id']}/resume")
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "done"


# ---------------------------------------------------------------- the calendar


def calendar(client, run_id):
    r = client.get(f"/api/runs/{run_id}/calendar")
    assert r.status_code == 200
    assert r.json()["run_id"] == run_id
    return r.json()["entries"]


def test_the_calendar_holds_only_kept_and_corrected_rows(runs_dir):
    client, _ = make(runs_dir)
    entries = calendar(client, "sample")
    state = client.get("/api/runs/sample").json()["state"]["targets"]
    # e7 and e8 are in because the sample's decisions keep them: a person's keep, shown as such
    assert {e["id"] for e in entries} == {"e1", "e2", "e3", "e4", "e5", "e7", "e8", "e11"}
    assert {e["group"] for e in entries} == {"kept", "corrected"}
    off = {i for i, t in state.items() if t["kind"] == "row" and t["group"] in ("removed", "unverified", "open")}
    assert off == {"e6", "e9", "e10"} and not off & {e["id"] for e in entries}
    e7 = next(e for e in entries if e["id"] == "e7")
    assert (e7["status"], e7["decided_by_person"], e7["last_action"]) == ("failed", True, "keep")


def test_the_calendar_follows_the_decisions(runs_dir):
    client, _ = make(runs_dir)

    def ids():
        return {e["id"] for e in calendar(client, "sample")}

    def decide(target, action):
        r = client.post("/api/runs/sample/decisions", json={"target": target, "action": action, "note": None,
                                                           "fix": None})
        assert r.status_code == 200, r.text

    assert "e1" in ids() and "e9" not in ids()
    decide("e1", "remove")
    assert "e1" not in ids()
    decide("e1", "undo")
    assert "e1" in ids()
    decide("e9", "keep")                      # a person's keep puts an unverified row on the calendar
    entry = next(e for e in calendar(client, "sample") if e["id"] == "e9")
    assert (entry["group"], entry["decided_by_person"], entry["status"]) == ("kept", True, "unreadable")
    decide("e9", "undo")
    assert "e9" not in ids()
    assert "e7" in ids()
    decide("e7", "undo")                      # takes back the keep: the row is open again and leaves
    assert "e7" not in ids()


def test_the_calendar_values_are_the_current_ones_and_dated_rows_come_first(runs_dir):
    client, _ = make(runs_dir)
    entries = calendar(client, "sample")
    dates = [e["date"] for e in entries]
    assert dates == sorted(dates, key=lambda d: (d is None, d or ""))
    e3 = next(e for e in entries if e["id"] == "e3")
    run = json.loads((runs_dir / "sample" / "run.json").read_text())
    fixed = client.get("/api/runs/sample").json()["state"]["targets"]["e3"]["values"]
    assert e3["date"] == fixed["date"] and e3["quote"] == fixed["quote"]
    assert any(r["id"] == "e3" for r in run["rows"])


def test_a_new_runs_calendar_has_only_rows_the_state_keeps(runs_dir):
    client, _ = make(runs_dir)
    meta = start(client)
    run = run_json(runs_dir, meta["id"])
    state = client.get(f"/api/runs/{meta['id']}").json()["state"]["targets"]
    expected = {r["id"] for r in run["rows"] if state[r["id"]]["group"] in ("kept", "corrected")}
    assert {e["id"] for e in calendar(client, meta["id"])} == expected
    # a row that failed a check starts open and stays off the calendar until a person acts
    off = {r["id"] for r in run["rows"] if r["group"] in ("open", "unverified")}
    assert not off & expected


def test_the_calendar_of_an_unknown_run_is_404(runs_dir):
    client, _ = make(runs_dir)
    assert client.get("/api/runs/nope/calendar").status_code == 404


# ---------------------------------------------------------------- the committed runs are untouched


def test_committed_runs_still_pass_the_schema_and_are_not_changed_by_a_new_run(runs_dir):
    shutil.copytree(RUNS / "demo", runs_dir / "demo")
    shutil.copytree(RUNS / "ptgx-r4a", runs_dir / "ptgx-r4a")
    digests = {n: hashlib.sha256((runs_dir / n / "run.json").read_bytes()).hexdigest()
               for n in ("sample", "demo", "ptgx-r4a")}
    client, _ = make(runs_dir)
    meta = start(client)
    assert meta["id"] == "run-4"
    for name, digest in digests.items():
        RUN_SCHEMA.validate(run_json(runs_dir, name))
        assert hashlib.sha256((runs_dir / name / "run.json").read_bytes()).hexdigest() == digest


# ---------------------------------------------------------------- job.json log (ruling 1)


def test_every_rewrite_of_run_json_leaves_a_line_in_the_job_log(runs_dir):
    client, _ = make(runs_dir)
    meta = start(client, companies=["PTGX", "IONS"])
    log = json.loads((runs_dir / meta["id"] / "job.json").read_text())["log"]
    assert all({"at", "status", "turn_count", "spend_usd"} <= set(line) for line in log)
    assert [line["status"] for line in log][0] == "queued" and log[-1]["status"] == "done"
    assert [line["turn_count"] for line in log if line["status"] == "running"][-1] == 2
    assert log[-1]["spend_usd"] == pytest.approx(0.02) and log[-1]["reason"].startswith("Read 2 pages")
    assert len(log) >= 6, "created, running, two turns, two pages, done"


# ---------------------------------------------------------------- raising the spending limit (ruling 3)


def stopped_at_limit(runs_dir, **kw):
    client, reader = make(runs_dir, FakeReader(cost=0.60))
    meta = start(client, companies=["PTGX", "IONS"], spending_limit_usd=0.5, **kw)
    assert run_json(runs_dir, meta["id"])["run"]["status"] == "needs_input"
    return client, reader, meta["id"]


def test_resume_with_a_higher_limit_goes_on_and_records_a_person_step(runs_dir):
    client, reader, rid = stopped_at_limit(runs_dir)
    r = client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": 2.0})
    assert r.status_code == 202
    run = run_json(runs_dir, rid)
    RUN_SCHEMA.validate(run)
    assert run["run"]["status"] == "done" and run["run"]["choices"]["spending_limit_usd"] == 2.0
    [step] = run["run"]["trail"]
    assert (step["tag"], step["who"], step["step"]) == ("Person", "person", "Spending limit raised")
    assert step["text"] == "Spending limit raised by you from $0.50 to $2.00."
    job = json.loads((runs_dir / rid / "job.json").read_text())
    assert [(c["from"], c["to"]) for c in job["limit_changes"]] == [(0.5, 2.0)]
    assert len(reader.prompts) == 2 and run["run"]["spend_usd"] == pytest.approx(1.2)


@pytest.mark.parametrize("new,code", [(0.5, 409), (0.3, 409), (-1, 422), ("9", 422), (True, 422)])
def test_resume_needs_a_limit_higher_than_the_old_one(runs_dir, new, code):
    client, reader, rid = stopped_at_limit(runs_dir)
    before = (runs_dir / rid / "run.json").read_bytes()
    assert client.post(f"/api/runs/{rid}/resume", json={"spending_limit_usd": new}).status_code == code
    assert (runs_dir / rid / "run.json").read_bytes() == before and len(reader.prompts) == 1


def test_resume_takes_nothing_but_a_limit_and_a_run_with_no_limit_has_none_to_raise(runs_dir, monkeypatch):
    client, _, rid = stopped_at_limit(runs_dir)
    assert client.post(f"/api/runs/{rid}/resume", json={"turn_limit": 99}).status_code == 422
    monkeypatch.setattr(jobs, "DEFAULT_TURN_LIMIT", 1)
    client2, _ = make(runs_dir)
    other = start(client2, companies=["PTGX", "IONS"])
    assert run_json(runs_dir, other["id"])["run"]["status"] == "paused_turn_limit"
    r = client2.post(f"/api/runs/{other['id']}/resume", json={"spending_limit_usd": 3})
    assert r.status_code == 409 and "no spending limit" in r.json()["detail"]


# ---------------------------------------------------------------- the time window (ruling 5)


def with_today(monkeypatch, y, m, d):
    monkeypatch.setattr(jobs, "today", lambda: date(y, m, d))


def test_add_months_keeps_to_the_end_of_a_short_month():
    assert jobs.add_months(date(2026, 10, 31), 4) == date(2027, 2, 28)
    assert jobs.add_months(date(2026, 10, 28), 12) == date(2027, 10, 28)


def test_a_row_later_than_the_window_is_left_out_by_code_and_is_not_a_mistake(runs_dir, monkeypatch):
    with_today(monkeypatch, 2026, 10, 28)
    client, _ = make(runs_dir)
    meta = start(client, time_window_months=6)         # to 2027-04-28: Q1 2027 in, 2H 2027 (2027-07-01) out
    run = run_json(runs_dir, meta["id"])
    RUN_SCHEMA.validate(run)
    by_date = {r["date"]: r for r in run["rows"]}
    late, early = by_date["2027-07-01"], by_date["2027-01-01"]
    assert late["group"] == "removed" and early["group"] != "removed"
    assert late["reason"] == ("Outside the time window: the date 2027-07-01 is later than 6 months from today "
                              "(2027-04-28).")
    assert late["trail"][-1]["step"] == "Left out" and late["trail"][-1]["tag"] == "Code"
    [x] = run["exclusions"]
    assert (x["target"], x["rule"], x["by"]) == (late["id"], "outside_time_window", "code")
    state = client.get(f"/api/runs/{meta['id']}").json()["state"]
    assert state["excluded"]["code"] == 1 and sum(state["mistakes"].values()) == 0
    assert late["id"] not in {e["id"] for e in calendar(client, meta["id"])}


def test_a_row_dated_before_today_is_left_out(runs_dir, monkeypatch):
    with_today(monkeypatch, 2027, 2, 1)                # Q1 2027 starts 2027-01-01: before today
    client, _ = make(runs_dir)
    run = run_json(runs_dir, start(client, time_window_months=6)["id"])
    by_date = {r["date"]: r for r in run["rows"]}
    assert by_date["2027-01-01"]["group"] == "removed" and by_date["2027-07-01"]["group"] != "removed"
    assert "is before today (2027-02-01)" in by_date["2027-01-01"]["reason"]


def test_no_window_leaves_nothing_out_and_an_undated_row_is_never_touched(runs_dir, monkeypatch):
    with_today(monkeypatch, 2030, 1, 1)
    client, _ = make(runs_dir)
    assert run_json(runs_dir, start(client, time_window_months=None)["id"])["exclusions"] == []

    def undated(prompt, n):
        entry = load_entry(read_events.PIN)
        ev = read_events.build_fake_events(read_events.load_words(), entry)[0]
        return json.dumps({"events": [{**ev, "date": None, "date_end": None}]})

    client2, _ = make(runs_dir, FakeReader(answer=undated))
    run = run_json(runs_dir, start(client2, time_window_months=1)["id"])
    undated = run["rows"][0]
    assert undated["group"] != "removed" and undated["id"] not in {x["target"] for x in run["exclusions"]}
    # the only exclusion is the dated 'not read' row the coverage check added for the other event
    assert [r["found_by"] for r in run["rows"] if r["id"] in {x["target"] for x in run["exclusions"]}] == ["coverage"]


def test_the_window_is_checked_in_the_choices(runs_dir):
    client, _ = make(runs_dir)
    assert client.post("/api/runs/new", json={**PTGX_ONLY, "time_window_months": 0}).status_code == 422
