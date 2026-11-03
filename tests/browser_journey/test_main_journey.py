"""The main journey of Proof Reader, driven through the real screens in headless Chromium against the real server.

What runs: server/app.py create_app under uvicorn on a free port over a temporary runs folder; ui/ served by that
server; the research job with FAKE models only (tests/research_fakes.py: a scripted Responses API researcher, a
scripted Claude Opus 5.5 reviewer, a scripted Muse Wonder adjudicator, a fake Brave Search, a fake fetcher, an
offline renderer) and the offline SEC intake preparer of tests/test_ticker_intake.py. No model, network or paid
call is made anywhere.

The journey (CVA, 2026-11-03), in order; a step the product blocks is marked xfail(strict=True) with the reason:
  1. New Run: enter PTGX, prepare it, start a research run through the real Run button (NR-001: the screen sends
     mode "research"; the request is read off the browser, never intercepted or rewritten).
  2. Watch the run card until done, including the review stage (2b, xfail: no stage label for the review).
  3. Open a calendar row: the quote, the check reasons and the review label are visible.
  4. One clean automatic placement ("Included — not reviewed by you") and one row in review with its reason.
  5. Remove then Undo on the included row; Fix (Keep anyway on the undemonstrated re-check) then Keep on the review row.
  6. Reload: the decisions persist.
  7. Download the PDF and Word exports: the summary counts match the screen, and every event row of each export
     (the PDF's table parsed by column, the Word table by cell) carries exactly the current title, the effective
     timing, the inclusion label and the person's decision label, never "Kept by you" on an automatic row. Two
     mutation negatives (7a, 7b) feed the real renderers a wrong view and show the per-row check fails on it.
     7c (xfail): the calendar screen's row still shows the pre-Fix title.
Honesty rules are asserted on every screen text captured and on both exports: never "Kept by you" for automatic
inclusion, never "verified", and an incomplete review is never shown as "independently reviewed".

Run:  python -m pytest tests/browser_journey -p no:cacheprovider -q
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from server import intake
from server.app import create_app
from tests import research_fakes as F
from tests.browser_journey.conftest import Server
from tests.test_fetch_filings import _stub_render
from tests.test_research_s1 import PR1, QUOTE, _row_ids
from tests.test_ticker_intake import TODAY, FakeFetcher as SecFetcher, sec_samples

AS_OF = "2026-11-02"
INCLUDED = "Included — not reviewed by you"
NEEDS_REVIEW = "Proposed — needs review"
KEPT = "Kept by you"
EXPAND_QUOTE = "Protagonist expects EXPAND topline data in December 2026, subject to FDA feedback."
CONDITION = "subject to FDA feedback"
FINDING_TEXT = "The EXPAND timing is subject to FDA feedback; the draft drops the condition."
PAGE = F.html("SAN DIEGO, Nov. 2, 2026 -- Protagonist Therapeutics announced today its pipeline update.",
              QUOTE, EXPAND_QUOTE)
WAIT_MS = 30_000


# ------------------------------------------------------------------ the scripted models

def research_script(gate: threading.Event, events: list[dict]) -> list:
    """One company's research: a search, a fetch of PR1, the proposals, one coverage note, finish. The first
    answer waits on `gate`, so the screen can be watched while the company is 'Researching'."""
    first = F.body([F.reasoning(1), F.search("Protagonist catalysts 2027", [PR1]),
                    F.call("fetch_url", {"url": PR1, "purpose": "company release"}, "c1")], rid="r1")
    return [
        lambda req: (gate.wait(60), first)[1],
        F.body([F.call("propose_events", {"events": events}, "c2")], rid="r2"),
        F.body([F.call("record_coverage", {"ticker": "PTGX", "category": "regulators",
                                           "question": "Any PDUFA date for rusfertide?", "status": "checked_none",
                                           "tool_call_ids": ["c1"], "source_ids": ["src-r1-ptgx"],
                                           "programs": ["rusfertide"], "finding": "No PDUFA date stated."}, "c3")],
               rid="r3"),
        lambda req: F.body([F.call("finish_draft", {"dispositions": [
            {"row_id": r, "disposition": "proposal", "related_row_id": None, "reason": "supported"}
            for r in _row_ids(req)], "limitations": []}, "c4")], rid="r4"),
    ]


def events_two() -> list[dict]:
    clean = F.event("ICARUS Phase 3 topline data", QUOTE, date_text="Q2 2027", date="2027-04-01",
                    date_end="2027-06-30", program="ICARUS", conditions=[])
    # the draft drops the stated condition: the independent reviewer's finding puts this row in review
    flagged = F.event("EXPAND topline data", EXPAND_QUOTE, date_text="December 2026", date="2026-12-01",
                      date_end="2026-12-31", program="EXPAND", conditions=[])
    return [clean, flagged]


def review_script(gate: threading.Event) -> list:
    """The reviewer: a lost-condition finding on the EXPAND row, no issue on the ICARUS row, then finish. The
    first reply waits on `gate`, so the screen can be watched while the company is reviewing."""
    def assess(messages):
        gate.wait(60)
        draft = json.loads(messages[1]["content"])["draft_proposals"]
        flagged = [p for p in draft if "EXPAND" in json.dumps(p)]
        assert len(flagged) == 1, draft
        finding = ("report_finding", {
            "kind": "lost_condition",
            "row_revision_ids": [{"row_id": flagged[0]["row_id"], "revision_id": flagged[0]["revision_id"]}],
            "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}], "text": FINDING_TEXT})
        verdicts = ("assess_rows", {"assessments": [
            {"row_id": p["row_id"], "revision_id": p["revision_id"],
             "verdict": "finding" if p is flagged[0] else "no_issue"} for p in draft]})
        return F.chat_tools(finding, verdicts)
    return [assess, F.chat_tools(("finish_review", {"summary": "One finding: a dropped condition."}))]


def truncated_review_script() -> list:
    """A review cut off by the model's length limit: recorded as incomplete, never as reviewed."""
    return [F.chat_tools(("finish_review", {"summary": "done"}), finish="length")]


def adjudication_script() -> list:
    return [F.verdict({"verdict": "resolved", "reading": 1, "resolution": "Keep the condition.",
                       "evidence": [{"source_id": "src-r1-ptgx", "quote": CONDITION}],
                       "reason": "The release states the condition."})]


# ------------------------------------------------------------------ the journey (shared state, in order)

class Journey:
    def __init__(self, runs_dir: Path, browser):
        self.runs_dir = runs_dir
        self.gates = {"research": threading.Event(), "review": threading.Event()}
        self.reader = F.FakeResponses(research_script(self.gates["research"], events_two()))
        self.tools = F.tools({PR1: (200, "text/html; charset=utf-8", PAGE)})
        self.tools.reviewer = F.FakeChat(review_script(self.gates["review"]))
        self.tools.adjudicator = F.FakeChat(adjudication_script(), model=F.ADJUDICATOR_MODEL)
        self.tools.brave = F.FakeBrave(answers={})
        cache = runs_dir.parent / "company_tickers.json"
        cache.write_text(json.dumps({"0": {"cik_str": 1377121, "ticker": "PTGX",
                                           "title": "Protagonist Therapeutics, Inc."}}))
        prep = intake.Preparer(fetcher=SecFetcher(sec_samples()), render=_stub_render,
                               filings_root=runs_dir.parent / "intake-docs", today=lambda: TODAY,
                               tickers_cache=cache, background=False)
        app = create_app(runs_dir=runs_dir, reader=self.reader, background=True, second_reader=None,
                         wonder_reader=None, sam_reader=None, research_tools=self.tools,
                         research_reader=self.reader, intake_preparer=prep)
        self.server = Server(app).start()
        self.base = self.server.base
        self.http = httpx.Client(base_url=self.base, timeout=30)
        self.context = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
        self.page = self.context.new_page()
        self.console = []
        self.page.on("console", lambda m: self.console.append(f"{m.type}: {m.text}"))
        self.page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        self.ui_body = None        # the POST /api/runs/new body the real Run button sent
        self.ui_status = None      # the server's status for it
        self.ui_answer = None      # the server's JSON answer to it
        self.ui_run_id = None      # the run it created, when the server accepted it as a research run
        self.run_id = None
        self.rows = {}             # 'clean' / 'flagged' -> row id
        self.clean_label = None
        self.screens = []          # (name, text) of every screen text captured, for the honesty rules
        self.report_view = None    # the view report.build rendered the exports from (step 7)
        self.export_files = None   # the PDF and Word bytes the Export screen served (step 7)

    # ---- helpers
    def api(self, path: str) -> dict:
        r = self.http.get(path)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def run(self) -> dict:
        return self.api(f"/api/runs/{self.run_id}")

    def goto(self, hash_route: str, run_id: str | None = None) -> None:
        """A fresh load of the app on one screen (a hash change alone never bootstraps the app's run)."""
        self.page.goto("about:blank")
        self.page.goto(f"{self.base}/#/{run_id or self.run_id}/{hash_route}", wait_until="load")

    def text(self, selector: str = "#app") -> str:
        return self.page.inner_text(selector)

    def capture(self, name: str, selector: str = "#app") -> str:
        text = self.text(selector)
        self.screens.append((name, text))
        return text

    def wait_text(self, selector: str, needle: str, timeout: int = WAIT_MS) -> str:
        deadline = time.monotonic() + timeout / 1000
        last = ""
        while time.monotonic() < deadline:
            try:
                last = self.page.inner_text(selector)
            except Exception:
                last = ""
            if has(last, needle):
                return last
            self.page.wait_for_timeout(200)
        raise AssertionError(f"'{needle}' never appeared in {selector}. Last text:\n{last[:1500]}")

    def wait_api(self, predicate, what: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        body = None
        while time.monotonic() < deadline:
            body = self.run()
            if predicate(body):
                return body
            time.sleep(0.2)
        raise AssertionError(f"{what} never happened. Last run status: {json.dumps(body['run'].get('status'))} "
                             f"{json.dumps((body['run'].get('research') or {}).get('companies'))[:600]}")

    def click_button(self, label: str, within: str = "#app") -> None:
        self.page.locator(f"{within} button", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$")).first.click()

    def close(self) -> None:
        self.context.close()
        self.http.close()
        self.server.stop()


@pytest.fixture(scope="module")
def j(runs_dir, browser):
    journey = Journey(runs_dir, browser)
    yield journey
    journey.close()


def has(text: str, needle: str) -> bool:
    """Case-insensitive containment: the screens uppercase badges and headings with CSS, which innerText keeps."""
    return needle.lower() in text.lower()


# ------------------------------------------------------------------ the honesty rules

def assert_honest(text: str, *, where: str, kept_allowed: bool = False, review_complete: bool = True) -> None:
    """The three rules, on one screen or export text (case-insensitive: CSS uppercases badges)."""
    low = text.lower()
    plain = low.replace("unverified", "")
    assert not re.search(r"\bverified\b", plain), f"{where}: says 'verified':\n{text[:800]}"
    if not kept_allowed:
        # the calendar headline's count 'kept by you 0' and the export's 'Kept by you: 0' name the count, not a row
        labels = re.sub(r"kept by you[:]? ?\d", "", low)
        assert "kept by you" not in labels, f"{where}: shows '{KEPT}' with no current Keep by a person:\n{text[:800]}"
    if not review_complete:
        stripped = low.replace("not independently reviewed", "")
        assert "independently reviewed" not in stripped, \
            f"{where}: an incomplete review is shown as independently reviewed:\n{text[:800]}"


# ================================================================== 1. New Run through the real UI

def ui_research_body(j: Journey) -> dict:
    """Click the New Run screen's real Run button and record, passively, the POST /api/runs/new it sent and the
    server's answer. Nothing is intercepted, rewritten or refused: the body is read off the request Chromium made."""
    page = j.page
    page.goto(f"{j.base}/", wait_until="load")
    page.click("#newrun")
    page.wait_for_selector("#newrun-wrap:not([hidden])")
    page.fill("#nr-tickers", "PTGX")
    page.click("#nr-intake-go")
    # the intake finishes inside the request (background=False); the button wakes as 'Continue with 1 ready'
    j.wait_text("#nr-go", "Continue with 1 ready")
    # NR-001: a research run is Muse Spark only; the page-mode model choice is hidden on the New Run screen
    assert not page.is_visible("#nr-models-one"), "the page-mode model choice is shown on a research New Run"
    with page.expect_response("**/api/runs/new", timeout=15_000) as info:
        page.click("#nr-go")
    response = info.value
    j.ui_body = json.loads(response.request.post_data or "{}")
    j.ui_status = response.status
    j.ui_answer = response.json()
    if response.status == 202:
        page.wait_for_selector("#newrun-wrap", state="hidden")
        j.ui_run_id = j.ui_answer["id"]
    else:
        page.click("#nr-cancel")
    return j.ui_body


def test_1_new_run_starts_a_research_run_through_the_real_run_button(j):
    body = ui_research_body(j)
    assert body.get("companies") == ["PTGX"], body
    assert body.get("mode") == "research", f"The New Run screen sent no mode: research. It sent: {body}"
    assert j.ui_status == 202, f"POST /api/runs/new {body} -> {j.ui_status} {j.ui_answer}"
    assert j.ui_run_id, j.ui_answer
    run = j.api(f"/api/runs/{j.ui_run_id}")["run"]
    assert run["choices"]["mode"] == "research" and run["choices"]["companies"] == ["PTGX"], run["choices"]
    # the screen moved to the run it started
    assert j.page.evaluate("location.hash").startswith(f"#/{j.ui_run_id}"), j.page.evaluate("location.hash")


# ================================================================== 2. the run card until done, with the review

def test_2_the_journey_follows_the_run_the_real_button_started(j):
    if j.ui_body is None:
        ui_research_body(j)
    assert j.ui_run_id, (f"The real Run button started no research run (POST /api/runs/new -> {j.ui_status} "
                         f"{j.ui_answer}); the journey has nothing to follow.")
    j.run_id = j.ui_run_id
    run = j.run()["run"]
    assert run["choices"]["mode"] == "research" and run["choices"]["companies"] == ["PTGX"]
    assert run["status"] in ("queued", "running"), run


def test_2a_the_run_card_shows_the_company_researching(j):
    j.goto("audit")
    text = j.wait_text("#runcard", "Researching")
    j.screens.append(("run card · researching", text))
    assert "running" in text or "queued" in text, text
    assert "PTGX" in text
    assert_honest(text, where="run card while researching")


REVIEW_IN_PROGRESS = re.compile(r"^(reviewing|review in progress|under review|independent review running|"
                                r"independent review in progress)$", re.IGNORECASE)


def review_in_progress_label(stage: str) -> bool:
    """True only for a current-stage label that says the independent review is running now. The whole label is
    matched: 'Not independently reviewed' (a row's research label) and 'Review completed' are not in progress."""
    return bool(REVIEW_IN_PROGRESS.match(" ".join(stage.split())))


def test_2b_contrast_the_stage_predicate_rejects_finished_and_unreviewed_wordings():
    assert review_in_progress_label("Reviewing") and review_in_progress_label("REVIEWING")
    for control in ("not independently reviewed", "Not independently reviewed", "review completed",
                    "Review completed", "Agent research — not independently reviewed", "reviewed", "Researching"):
        assert not review_in_progress_label(control), control


def ptgx_stage_label(j: Journey) -> str:
    """The current-stage label of PTGX's own line on the run card (ui/app.js researchCompanyBlock: one .rscline per
    company, the ticker in <b>, the stage word in .rsst)."""
    line = j.page.locator("#runcard .rscline", has=j.page.locator("b", has_text=re.compile(r"^PTGX$"))).first
    assert line.count() == 1, f"no run-card line for PTGX:\n{j.text('#runcard')}"
    return line.locator(".rsst").first.inner_text()


@pytest.mark.xfail(strict=True, reason="The run card never shows the review stage: ui/app.js researchCompanyBlock "
                                       "renders companies[ticker].status through RESEARCH_STATUS_WORDS (Queued / "
                                       "Researching / Finished / Stopped / Failed) and never review_status "
                                       "('reviewing' / 'done'). Observed: while the API says review_status: "
                                       "reviewing, the PTGX line's stage label reads 'Finished' (its research "
                                       "stage) and the card's run line reads 'Researching.'; nothing on the card "
                                       "says the independent review is running.")
def test_2b_the_run_card_shows_the_review_stage(j):
    j.gates["research"].set()
    j.wait_api(lambda b: (b["run"].get("research") or {}).get("companies", {}).get("PTGX", {})
               .get("review_status") == "reviewing", "the independent review of PTGX starting")
    # the job poll redraws the card every 2.5 s
    j.page.wait_for_timeout(3000)
    text = j.capture("run card · reviewing", "#runcard")
    assert_honest(text, where="run card while reviewing")
    company = j.run()["run"]["research"]["companies"]["PTGX"]
    assert company["review_status"] == "reviewing", company
    stage = ptgx_stage_label(j)
    assert review_in_progress_label(stage), \
        f"API review_status is 'reviewing' but the PTGX line's stage label reads {stage!r}:\n{text}"


def test_2c_the_run_card_reaches_done_after_the_review(j):
    j.gates["research"].set()
    j.gates["review"].set()
    body = j.wait_api(lambda b: b["run"].get("status") not in ("queued", "running"), "the run ending")
    assert body["run"]["status"] == "done", body["run"].get("status_reason")
    text = j.wait_text("#runcard", "Run finished")
    j.screens.append(("run card · done", text))
    assert "PTGX" in text and "Finished" in text, text
    review = body["run"]["research"]["review"]["PTGX"]
    assert review["status"] == "complete" and review["clean"] is False, review
    rows = {r["title"]: r for r in body["rows"] if r.get("research")}
    j.rows = {"clean": rows["ICARUS Phase 3 topline data"]["id"], "flagged": rows["EXPAND topline data"]["id"]}
    targets = body["state"]["targets"]
    assert targets[j.rows["clean"]]["inclusion"] == "included", targets[j.rows["clean"]]
    assert targets[j.rows["flagged"]]["inclusion"] == "review", targets[j.rows["flagged"]]
    assert "1 need review" in text, text
    assert_honest(text, where="run card when done")


# ================================================================== 3 + 4. the calendar row and the review row

def test_3_a_calendar_row_shows_quote_checks_and_the_review_label(j):
    j.goto("calendar")
    j.wait_text("#cal-mid", "Catalyst Calendar")
    head = j.text("#cal-mid")
    assert "1 row" in head and "kept by you 0" in head and "included automatically 1" in head, head
    j.page.locator("#cal-mid .crow", has_text="ICARUS").first.click()
    pane = j.wait_text("#cal-det", "The quote")
    j.screens.append(("calendar · ICARUS row", j.text("#scr-calendar")))
    assert QUOTE in pane, pane
    assert has(pane, "Checks · by Code"), pane
    # the check lines: each check's name, its outcome and the reason behind it
    for name, reason in (("Quote found on page", "printed on the page, word for word"),
                         ("Citation located", "inside the box"),
                         ("Date period found in quote", "This compares the printed timing only"),
                         ("Event meaning needs review", "Code does not check event meaning")):
        assert has(pane, name) and has(pane, reason), (name, reason, pane)
    clean_row = next(r for r in j.run()["rows"] if r["id"] == j.rows["clean"])
    j.clean_label = clean_row["research"]["label"]
    assert has(pane, j.clean_label), f"The row's review label is not in the pane:\n{pane}"
    assert_honest(pane, where="calendar row pane")
    # the clean row was placed by the rule, not kept: the inclusion label, visible on the Audit log row
    j.goto("audit")
    audit = j.wait_text("#audit-rows", INCLUDED)
    j.screens.append(("audit log", j.text("#scr-audit")))
    assert_honest(audit, where="audit log")


@pytest.mark.xfail(strict=True, reason="runs/research_labels.py derive_review_labels labels a row 'clean' only when "
                                       "the company's whole review is clean (record.clean). In a completed review "
                                       "that found something on another row, a row the reviewer assessed no_issue "
                                       "on its current revision is labelled 'Agent research — not independently "
                                       "reviewed', which contradicts its own review record (status complete, "
                                       "verdict no_issue). Owners: runs/research_labels.py.")
def test_3a_a_reviewed_clean_row_carries_the_reviewed_label(j):
    from runs import research
    clean_row = next(r for r in j.run()["rows"] if r["id"] == j.rows["clean"])
    review = clean_row["research"]["review"]
    assert review["status"] == "complete" and review["verdict"] == "no_issue"
    assert review["reviewed_revision"] == clean_row["research"]["revision"]
    assert clean_row["research"]["label"] == research.LABEL_REVIEWED_CLEAN, clean_row["research"]["label"]


def test_4_the_review_row_shows_its_reason(j):
    j.goto("judgment")
    text = j.wait_text("#judgment-rows", "Waiting for you · 1")
    assert "EXPAND" in text, text
    j.page.locator("#judgment-rows .row", has_text="EXPAND").first.click()
    pane = j.wait_text("#judgment-det", "Your call")
    j.screens.append(("judgment · EXPAND row", j.text("#scr-judgment")))
    listing = j.text("#judgment-rows")
    assert has(listing, NEEDS_REVIEW), listing
    flagged = next(r for r in j.run()["rows"] if r["id"] == j.rows["flagged"])
    assert "with findings" in flagged["research"]["label"]
    assert has(pane, flagged["research"]["label"]), pane
    target = j.run()["state"]["targets"][j.rows["flagged"]]
    assert target["needs_attention"] and target["issues"], target
    assert "a person must decide" in target["reason"]
    # the state's reason line rides under the row in the queue (its first 72 characters)
    assert has(listing, target["reason"][:40]), (target["reason"], listing)
    assert has(pane, "Keep") and has(pane, "Fix…") and has(pane, "Remove…"), pane
    assert_honest(pane, where="judgment pane of the review row")
    assert_honest(listing, where="judgment queue")


@pytest.mark.xfail(strict=True, reason="The independent reviewer's finding text (state.targets[row].issues, "
                                       "'The reviewer found …') is not rendered anywhere in ui/app.js: the "
                                       "judgment pane shows the research label 'with findings' and the generic "
                                       "reason, never what the reviewer found.")
def test_4a_the_review_row_shows_what_the_reviewer_found(j):
    j.goto(f"judgment/{j.rows['flagged']}")
    pane = j.wait_text("#judgment-det", "Your call")
    issues = j.run()["state"]["targets"][j.rows["flagged"]]["issues"]
    assert any("reviewer found" in i for i in issues), issues
    assert FINDING_TEXT in pane or any(i in pane for i in issues), f"No reviewer finding in the pane:\n{pane}"


# ================================================================== 5. Remove/Undo and Fix/Keep

def decisions(j: Journey) -> list[dict]:
    return j.api(f"/api/runs/{j.run_id}/decisions")


def test_5_remove_then_undo_on_the_included_row(j):
    j.goto(f"judgment/{j.rows['clean']}")
    pane = j.wait_text("#judgment-det", "Your call")
    assert "ICARUS" in pane, pane
    j.click_button("Remove…", "#judgment-det")
    j.page.wait_for_selector("#dialog-wrap:not([hidden])")
    j.page.fill("#dnote", "Journey check: removing the automatic placement.")
    j.click_button("Remove", "#dialog")
    pane = j.wait_text("#judgment-det", "Removed by you")
    j.screens.append(("judgment · ICARUS removed", j.text("#scr-judgment")))
    target = j.run()["state"]["targets"][j.rows["clean"]]
    assert target["inclusion"] == "excluded" and target["decision_label"] == "Removed by you", target
    assert has(pane, "Removed by you") and has(pane, "Undo"), pane
    assert_honest(pane, where="judgment pane after Remove")
    j.click_button("Undo", "#judgment-det")
    j.wait_text("#judgment-det", "Your call")
    j.wait_api(lambda b: b["state"]["targets"][j.rows["clean"]]["inclusion"] == "included", "the Undo landing")
    target = j.run()["state"]["targets"][j.rows["clean"]]
    assert target["review_label"] == INCLUDED and target["decision_label"] is None, target
    log = decisions(j)
    assert [d["action"] for d in log if d["target"] == j.rows["clean"]] == ["remove", "undo"], log
    j.goto("calendar")
    head = j.wait_text("#cal-mid", "included automatically 1")
    assert "kept by you 0" in head, head
    assert_honest(j.capture("calendar after Undo", "#scr-calendar"), where="calendar after Undo")


def test_5a_fix_then_keep_on_the_review_row(j):
    j.goto(f"judgment/{j.rows['flagged']}")
    j.wait_text("#judgment-det", "Your call")
    j.click_button("Fix…", "#judgment-det")
    j.page.wait_for_selector("#dialog-wrap:not([hidden])")
    j.page.select_option("#dialog select", "title")
    j.page.fill("#dialog input:not([readonly])", "EXPAND Phase 3 topline data (subject to FDA feedback)")
    j.click_button("Save fix", "#dialog")
    # No check compares a title, so the re-check "could not run": the dialog stays open with the code's reason and
    # offers Keep anyway (ui/app.js fixDialog, ruling G1-2). The fix is recorded; the row stays open for a person.
    dialog = j.wait_text("#dialog", "Re-checked after the fix")
    j.screens.append(("fix dialog · re-check", dialog))
    assert has(dialog, "could not run") and has(dialog, "No check compares title"), dialog
    j.click_button("Keep anyway", "#dialog")
    j.page.wait_for_selector("#dialog-wrap", state="hidden", timeout=WAIT_MS)
    pane = j.wait_text("#judgment-det", "still open")
    j.screens.append(("judgment · EXPAND fixed", j.text("#scr-judgment")))
    target = j.run()["state"]["targets"][j.rows["flagged"]]
    assert target["last_action"] == "fix" and target["accepted"] is False, target
    assert target["recheck"]["result"] == "could_not_run", target["recheck"]
    assert target["values"]["title"] == "EXPAND Phase 3 topline data (subject to FDA feedback)", target["values"]
    assert has(pane, "Fixed by you") and has(pane, "could not run") and not has(pane, KEPT), pane
    assert_honest(pane, where="judgment pane after Fix")
    j.page.click("#keepbtn")
    pane = j.wait_text("#judgment-det", KEPT)
    j.screens.append(("judgment · EXPAND kept", j.text("#scr-judgment")))
    target = j.run()["state"]["targets"][j.rows["flagged"]]
    assert target["accepted"] is True and target["inclusion"] == "kept", target
    assert target["decision_label"] == KEPT
    assert_honest(pane, where="judgment pane after Keep", kept_allowed=True)
    log = [d["action"] for d in decisions(j) if d["target"] == j.rows["flagged"]]
    assert log[-3:] == ["fix", "recheck", "keep"] or log[-2:] == ["fix", "keep"], log


# ================================================================== 6. reload

def test_6_the_decisions_persist_after_a_reload(j):
    j.page.reload(wait_until="load")
    j.goto("calendar")
    head = j.wait_text("#cal-mid", "2 rows")
    assert "kept by you 1" in head and "included automatically 1" in head, head
    rows = j.text("#cal-mid")
    assert "ICARUS" in rows and "EXPAND" in rows, rows
    # the kept row wears Kept by you; the included row does not
    kept_row = j.page.locator("#cal-mid .crow", has_text="EXPAND").first.inner_text()
    clean_row = j.page.locator("#cal-mid .crow", has_text="ICARUS").first.inner_text()
    assert has(kept_row, KEPT) and not has(clean_row, KEPT), (kept_row, clean_row)
    j.goto(f"judgment/{j.rows['flagged']}")
    pane = j.wait_text("#judgment-det", KEPT)
    assert has(j.text("#judgment-rows"), "Decided by you · 1"), j.text("#judgment-rows")
    j.goto(f"judgment/{j.rows['clean']}")
    pane = j.wait_text("#judgment-det", "Your call")
    assert not has(pane, KEPT) and not has(pane, "Removed by you"), pane
    assert_honest(j.capture("after reload · calendar + judgment", "#app"), where="after reload", kept_allowed=True)
    targets = j.run()["state"]["targets"]
    assert targets[j.rows["flagged"]]["inclusion"] == "kept" and targets[j.rows["clean"]]["inclusion"] == "included"


# ================================================================== 7. the exports

CALENDAR_COLUMNS = ["Date / period", "Ticker", "Program", "Event", "Source", "Status"]


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def pdf_calendar_rows(data: bytes, tickers: set[str]) -> list[dict]:
    """The calendar table of the PDF, one dict per event row (column name -> cell text). pypdf's layout mode keeps
    the table's grid: the header line gives each column's character offset, every row is one block of lines
    between blank lines, a cell is its column's slice of the block's lines joined. The table ends at the first
    block whose Ticker cell is not one of the run's tickers (the evidence lines that follow it)."""
    from pypdf import PdfReader
    pages = [p.extract_text(extraction_mode="layout") or "" for p in PdfReader(io.BytesIO(data)).pages]
    lines = "\n".join(pages).splitlines()
    header = next((i for i, l in enumerate(lines)
                   if all(c in l for c in CALENDAR_COLUMNS) and l.find("Date / period") < l.find("Status")), None)
    assert header is not None, "no calendar table header in the PDF"
    starts = [lines[header].index(c) for c in CALENDAR_COLUMNS]
    spans = list(zip(starts, starts[1:] + [10_000]))
    rows, block = [], []

    def cells(block: list[str]) -> dict:
        return {name: " ".join(" ".join(l[a:b].split()) for l in block).strip()
                for name, (a, b) in zip(CALENDAR_COLUMNS, spans)}

    for line in lines[header + 1:] + [""]:
        if line.strip():
            block.append(line)
            continue
        if block:
            row = cells(block)
            if row["Ticker"] not in tickers:
                break
            rows.append(row)
            block = []
    assert rows, f"no event rows parsed from the PDF calendar table after:\n{lines[header]}"
    return rows


def docx_text(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    for section in doc.sections:
        parts += [p.text for p in section.footer.paragraphs]
    return "\n".join(parts)


def docx_calendar_rows(data: bytes) -> list[dict]:
    """The calendar table of the Word file, one dict per event row (column name -> cell text)."""
    from docx import Document
    doc = Document(io.BytesIO(data))
    table = next((t for t in doc.tables if [c.text for c in t.rows[0].cells] == CALENDAR_COLUMNS), None)
    assert table is not None, f"no calendar table in the Word file: {[[c.text for c in t.rows[0].cells] for t in doc.tables]}"
    return [dict(zip(CALENDAR_COLUMNS, [c.text for c in r.cells])) for r in table.rows[1:]]


def expected_calendar_rows(j: Journey) -> dict[str, dict]:
    """What each event row must read on the exports, from the API's current state (never from the report's own
    view): the current title (a person's fix applied), the effective timing, the row's inclusion label and the
    person's decision label when there is one."""
    body = j.run()
    rows = {r["id"]: r for r in body["rows"]}
    out = {}
    for key in ("clean", "flagged"):
        rid = j.rows[key]
        st, row = body["state"]["targets"][rid], rows[rid]
        title = st["values"].get("title") or row["title"]
        status = st["review_label"]
        if st.get("contingent"):
            status += " · Contingent"
        if st.get("decision_label"):
            status += f" · {st['decision_label']}"
        out[title] = {"Date / period": st["date_display"], "Ticker": row["ticker"], "Program": row["program"],
                      "Event": title, "Status": status, "inclusion": st["inclusion"],
                      "automatic": st["inclusion"] == "included"}
    return out


def assert_export_rows(rows: list[dict], expected: dict[str, dict], *, where: str) -> None:
    """Every expected event is one row of the export, with exactly its current title, its effective timing and its
    exact status (inclusion label · person's decision label); no other rows; and never 'Kept by you' on a row
    the rule placed automatically."""
    by_event = {r["Event"]: r for r in rows}
    assert len(by_event) == len(rows), f"{where}: duplicate event titles in the table: {[r['Event'] for r in rows]}"
    assert set(by_event) == set(expected), \
        f"{where}: the table's events {sorted(by_event)} are not the run's current titles {sorted(expected)}"
    for title, want in expected.items():
        got = by_event[title]
        for col in ("Date / period", "Ticker", "Program", "Status"):
            assert got[col] == want[col], f"{where}: row {title!r} {col}: {got[col]!r} != {want[col]!r}"
        if want["automatic"]:
            assert not has(got["Status"], KEPT), f"{where}: automatic row {title!r} wears {KEPT!r}: {got['Status']!r}"
            assert has(got["Status"], INCLUDED), f"{where}: automatic row {title!r} lost its inclusion label: {got['Status']!r}"
        else:
            assert has(got["Status"], KEPT), f"{where}: the kept row {title!r} has no {KEPT!r}: {got['Status']!r}"


def export_rows(kind: str, data: bytes, tickers: set[str]) -> list[dict]:
    return pdf_calendar_rows(data, tickers) if kind == "pdf" else docx_calendar_rows(data)


def test_7_the_pdf_and_word_exports_match_the_screen(j):
    # The server serves a saved report only; nothing on the screens builds one (report/README.md: the build is the
    # command `python -m report.build <run>`). The journey runs that same builder, after the decisions.
    from report.build import build as build_report
    out = build_report(j.run_id, run_dir=j.runs_dir / j.run_id)
    j.report_view = out["view"]
    j.goto("export")
    # each file row answers its own HEAD request: wait until neither still says 'checking…'
    j.wait_text("#exp-mid", "Open")
    deadline = time.monotonic() + WAIT_MS / 1000
    while has(j.text("#exp-mid"), "checking…") and time.monotonic() < deadline:
        j.page.wait_for_timeout(100)
    text = j.text("#exp-mid")
    links = j.page.locator("#exp-mid a.link")
    hrefs = [links.nth(i).get_attribute("href") for i in range(links.count())]
    assert len(hrefs) == 2 and any(h.endswith(".pdf") for h in hrefs) and any(h.endswith(".docx") for h in hrefs), text
    assert "out of date" not in text and "No report is saved" not in text, text
    summary = j.text("#exp-det") + "\n" + j.text("#exp-mid")
    j.screens.append(("export", summary))
    files = {}
    for href in hrefs:
        r = j.http.get(href)
        assert r.status_code == 200, (href, r.status_code, r.text[:200])
        files["pdf" if href.endswith(".pdf") else "docx"] = r.content
    assert files["pdf"][:4] == b"%PDF" and files["docx"][:2] == b"PK"
    j.export_files = files
    got = {"pdf": pdf_text(files["pdf"]), "docx": docx_text(files["docx"])}
    counts = j.run()["state"]["counts"]["inclusion"]
    assert counts["kept"] == 1 and counts["included"] == 1 and counts["review"] == 0, counts
    expected = expected_calendar_rows(j)
    fixed_title = "EXPAND Phase 3 topline data (subject to FDA feedback)"
    assert fixed_title in expected and expected[fixed_title]["Status"] == f"Accepted by you · Contingent · {KEPT}", expected
    assert expected["ICARUS Phase 3 topline data"]["Status"] == INCLUDED, expected
    assert expected[fixed_title]["Date / period"] == "December 2026", expected
    assert expected["ICARUS Phase 3 topline data"]["Date / period"] == "Q2 2027", expected
    # the calendar screen shows the same timings and labels (the current title: test_7c)
    j.goto("calendar")
    head = j.wait_text("#cal-mid", "2 rows")
    assert "kept by you 1" in head and "included automatically 1" in head
    for title, want in expected.items():
        row = j.page.locator("#cal-mid .crow", has_text=want["Program"]).first.inner_text()
        assert has(row, want["Date / period"]), (title, row)
        assert has(row, KEPT) == (not want["automatic"]), (title, row)
    # the summary counts, on both exports
    for kind, body in got.items():
        flat = " ".join(body.split())
        assert f"Kept by you: {counts['kept']}" in flat, (kind, flat[:1500])
        assert f"Included automatically — not reviewed by you: {counts['included']}" in flat, (kind, flat[:1500])
        assert f"Needs your review: {counts['review']}" in flat, (kind, flat[:1500])
        assert "Unresolved (needs attention): 0" in flat, kind
        plain = flat.replace("unverified", "").replace("Unverified", "")
        assert not re.search(r"\bverified\b", plain, re.IGNORECASE), kind
        # the research label on the exports is the run's recorded label (reviewed, with findings), never 'verified'
        label = j.run()["run"]["research"]["label"]
        assert "with findings" in label and (label in flat or label.split(" (")[0] in flat), (kind, label, flat[:1200])
    # every event row of each export: the current title, the effective timing, the exact status and attribution
    for kind, data in files.items():
        rows = export_rows(kind, data, {"PTGX"})
        assert len(rows) == 2, (kind, rows)
        assert_export_rows(rows, expected, where=f"{kind} calendar table")
    # the screen's own numbers agree with the exports
    assert re.search(r"needs attention\s*\n0\b", summary, re.IGNORECASE), summary
    assert re.search(r"placed automatically — not reviewed by you\s*\n1\b", summary, re.IGNORECASE), summary
    assert re.search(r"checked and kept\s*\n1\b", summary, re.IGNORECASE), summary


@pytest.mark.xfail(strict=True, reason="The calendar screen's row shows the row's original title, not the "
                                       "person's fix: ui/app.js calRow renders row.title, never "
                                       "state.targets[row].values.title, so after the Fix the EXPAND row still "
                                       "reads 'EXPAND topline data' while the exports (report/data.py _event_label) "
                                       "read 'EXPAND Phase 3 topline data (subject to FDA feedback)'.")
def test_7c_the_calendar_screen_row_shows_the_current_title(j):
    expected = expected_calendar_rows(j)
    fixed_title = "EXPAND Phase 3 topline data (subject to FDA feedback)"
    assert fixed_title in expected, expected
    j.goto("calendar")
    j.wait_text("#cal-mid", "2 rows")
    row = j.page.locator("#cal-mid .crow", has_text="EXPAND").first.inner_text()
    assert fixed_title in row, f"The calendar row does not show the current title {fixed_title!r}:\n{row}"


def rendered_rows(view: dict, kind: str) -> list[dict]:
    """The real report renderers (report/pdf_report.py, report/docx_report.py) on a view, parsed per event row."""
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf
    data = build_pdf(view) if kind == "pdf" else build_docx(view)
    return export_rows(kind, data, {"PTGX"})


def mutated_view(j: Journey, mutate) -> dict:
    import copy
    view = copy.deepcopy(j.report_view)
    mutate(view)
    return view


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_7a_mutation_the_exports_check_fails_on_kept_by_you_on_the_automatic_row(j, kind):
    """Negative control: the real renderers fed a view whose automatically included row says 'Kept by you' must
    fail the per-row check, on each export. The unmutated view passes the same check first."""
    expected = expected_calendar_rows(j)
    assert_export_rows(rendered_rows(j.report_view, kind), expected, where=f"{kind} unmutated")

    def kept_on_automatic(view):
        row = next(r for r in view["calendar"] if "ICARUS" in r["event"])
        assert row["review_label"] == INCLUDED and not row["person_tag"], row
        row["person_tag"] = KEPT
    rows = rendered_rows(mutated_view(j, kept_on_automatic), kind)
    with pytest.raises(AssertionError, match="ICARUS"):
        assert_export_rows(rows, expected, where=f"{kind} mutated")


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_7b_mutation_the_exports_check_fails_on_the_stale_pre_fix_title(j, kind):
    """Negative control: the real renderers fed a view that still carries the EXPAND row's pre-Fix title must fail
    the per-row check, on each export."""
    expected = expected_calendar_rows(j)

    def stale_title(view):
        row = next(r for r in view["calendar"] if "EXPAND" in r["event"])
        assert row["event"] == "EXPAND Phase 3 topline data (subject to FDA feedback)", row
        row["event"] = "EXPAND topline data"
    rows = rendered_rows(mutated_view(j, stale_title), kind)
    assert any(r["Event"] == "EXPAND topline data" for r in rows), rows
    with pytest.raises(AssertionError, match="current titles"):
        assert_export_rows(rows, expected, where=f"{kind} mutated")


# ================================================================== honesty: an incomplete review

def test_8_an_incomplete_review_is_never_shown_as_independently_reviewed(j):
    # a second research run on the same server: the reviewer's reply is cut off, so the review is incomplete
    gate = threading.Event()
    gate.set()
    j.reader.script.extend(research_script(gate, [events_two()[0]]))
    j.tools.reviewer.script.extend(truncated_review_script())
    # the body the real Run button sent in step 1, without its intake binding (one run per intake), as_of pinned
    body = {k: v for k, v in j.ui_body.items() if k not in ("intake_id", "accept_ready_subset")}
    body["as_of"] = AS_OF
    assert body.get("mode") == "research", body
    r = j.http.post("/api/runs/new", json=body)
    assert r.status_code == 202, r.text
    second = r.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = j.api(f"/api/runs/{second}")
        if run["run"]["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert run["run"]["status"] == "done", run["run"]
    rec = run["run"]["research"]["review"]["PTGX"]
    assert rec["status"] == "incomplete", rec
    (row,) = [x for x in run["rows"] if x.get("research")]
    assert row["research"]["label"] == "Agent research — not independently reviewed"
    j.goto("calendar", second)
    j.wait_text("#cal-mid", "Catalyst Calendar")
    j.page.locator("#cal-mid .crow", has_text="ICARUS").first.click()
    pane = j.wait_text("#cal-det", "The quote")
    card = j.text("#runcard")
    for where, text in (("calendar pane", pane), ("run card", card)):
        assert_honest(text, where=f"incomplete review · {where}", review_complete=False)
    assert has(pane, "not independently reviewed"), pane
    j.goto("trail", second)
    trail = j.wait_text("#scr-trail", "Agent trail")
    assert_honest(trail, where="incomplete review · agent trail", review_complete=False)


# ================================================================== every captured screen, once more

def test_9_every_captured_screen_kept_the_honesty_rules(j):
    assert j.screens, "no screen text was captured"
    for name, text in j.screens:
        kept_allowed = any(k in name for k in ("kept", "after reload", "export"))
        assert_honest(text, where=name, kept_allowed=kept_allowed)
    errors = [c for c in j.console if c.startswith("pageerror")]
    assert not errors, errors
