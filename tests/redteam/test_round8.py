"""Red Team round 8. A failure is a finding: each assertion is the result the code should have given.

Base: product main 9c22ac2. No model call, no network, no key. The readers are fakes.
Scope: the New run panel and the resume flow (server/jobs.py limits) and the export
(report/ PDF and Word from saved state).
"""

import json
import math
import re
import shutil
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

from report.build import build
from report.data import build_view, load_run_dir
from server import jobs
from tests.test_run_controls import PTGX_ONLY, RUNS, FakeReader, make, run_json

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "ui" / "app.js"
FIXED = "2026-10-29T18:00:00Z"


@pytest.fixture
def runs_dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    shutil.copytree(RUNS / "sample", d / "sample")
    return d


# The sentence the job records when a send-back supplies the date. It contains the
# letters "date", which is also the name of a different field.
SEND_BACK = ("The date text was not in the quote; the reader, sent back once, gave the sentence "
             "that states “Q1 2027”.")


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    for section in doc.sections:
        for p in section.footer.paragraphs:
            parts.append(p.text)
    return "\n".join(parts)


def _raw_new_run(limit_token: str) -> bytes:
    """A New run body whose spending limit is the JSON number `limit_token` (for example 1e309)."""
    body = json.dumps({**PTGX_ONLY, "spending_limit_usd": 1})
    return body.replace('"spending_limit_usd": 1', f'"spending_limit_usd": {limit_token}').encode()


def _row(**kw) -> dict:
    base = {
        "id": "e1", "ticker": "PTGX", "company": "Protagonist Therapeutics, Inc.",
        "event_type": "milestone", "title": "Phase 3 start", "date_text": "Q1 2027",
        "date": "2027-01-01", "date_end": "2027-03-31", "quote": "Phase 3 start in Q1 2027",
        "program": "PN-8047", "indication": None, "group": "kept", "status": "matched",
        "source_id": "s1", "page_number": 3, "reason": "The checks passed.", "sample": False,
        "checks": [],
    }
    base.update(kw)
    return base


def _saved(tmp_path: Path, rows: list[dict], decisions: list[dict] | None = None,
           mistakes: list[dict] | None = None) -> Path:
    run_dir = tmp_path / "run-8"
    run_dir.mkdir()
    doc = {
        "run": {
            "id": "run-8", "number": 8, "label": "PTGX", "sample": False,
            "started_at": "2026-10-29T12:00:00+00:00", "finished_at": "2026-10-29T12:05:00+00:00",
            "choices": {"companies": ["PTGX"], "event_types": ["milestone"], "time_window_months": 12,
                        "spending_limit_usd": 2, "note_for_agent": None},
            "spend_usd": 0.01, "status": "done", "status_reason": "Read 1 page.",
            "turn_count": 1, "turn_limit": 40, "turns_total": 1, "resumes": 0,
        },
        "sources": [{"id": "s1", "publisher": "SEC", "kind": "8-K", "published_date": "2026-08-05",
                     "url": "", "sha256": "abc"}],
        "tables": [], "rows": rows, "mistakes": mistakes or [], "exclusions": [],
    }
    (run_dir / "run.json").write_text(json.dumps(doc))
    lines = [json.dumps(d) for d in (decisions or [])]
    (run_dir / "decisions.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    return run_dir


def _guard_after(src: str, anchor: str) -> str:
    """The `if` condition that returns, in the New run or Resume handler, after `anchor`."""
    window = src[src.index(anchor):src.index(anchor) + 700]
    i = 0
    while True:
        j = window.find("if", i)
        if j < 0:
            break
        k = j + 2
        while k < len(window) and window[k].isspace():
            k += 1
        if k >= len(window) or window[k] != "(":
            i = j + 2
            continue
        depth = 0
        for n in range(k, len(window)):
            depth += (window[n] == "(") - (window[n] == ")")
            if depth == 0:
                cond = window[k + 1:n]
                if "return" in window[n + 1:n + 120]:
                    return cond
                i = n + 1
                break
        else:
            break
    raise AssertionError(f"no returning if after {anchor}")


def _guard_fires(cond: str, var: str, value: float) -> bool:
    """True when the handler's own condition refuses `value`. The condition is read from ui/app.js."""
    expr = cond.strip()
    expr = re.sub(r"Number\.isFinite\s*\(", "math.isfinite(", expr)
    expr = re.sub(r"Number\.isNaN\s*\(", "math.isnan(", expr)
    expr = re.sub(r"(?<![\w.])isFinite\s*\(", "math.isfinite(", expr)
    expr = re.sub(r"(?<![\w.])isNaN\s*\(", "math.isnan(", expr)
    expr = expr.replace("||", " or ").replace("&&", " and ")
    expr = re.sub(r"!([^=\s])", r" not \1", expr)
    expr = re.sub(rf"\b{var}\b", "value", expr)
    return bool(eval(expr, {"__builtins__": {}}, {"math": math, "value": value}))


# ---------------------------------------------------------------- spending limit


def test_a_non_finite_spending_limit_is_refused():
    """Infinity is not a number of dollars. json accepts 1e309 as Infinity, and Infinity >= 0 is true,
    so the gate lets it through. A limit that is not finite must be refused with the other bad limits."""
    with pytest.raises(ValueError):
        jobs.validate_choices({**PTGX_ONLY, "spending_limit_usd": float("inf")})


def test_the_new_run_endpoint_refuses_a_non_finite_limit(runs_dir):
    """POST /api/runs/new with spending_limit_usd 1e309. The body is the JSON number, which parses as
    Infinity. The run must be refused and no page is read."""
    client, reader = make(runs_dir)
    got = client.post("/api/runs/new", content=_raw_new_run("1e309"),
                      headers={"content-type": "application/json"})
    assert got.status_code == 422 and isinstance(got.json()["detail"], str)
    assert reader.prompts == []
    assert sorted(p.name for p in runs_dir.iterdir()) == ["sample"]


def test_a_stored_non_finite_limit_stops_before_any_call(tmp_path):
    """A run.json that already holds Infinity never trips spend >= limit, so the run reads the page
    and finishes. The limit cannot be kept: stop before the call, the way a limit with no price does."""
    runs = tmp_path / "runs"
    runs.mkdir()
    choices = jobs.validate_choices(PTGX_ONLY)
    choices["spending_limit_usd"] = 1
    run = jobs.create_run(runs, choices)
    rid = run["run"]["id"]
    path = runs / rid / "run.json"
    doc = json.loads(path.read_text())
    doc["run"]["choices"]["spending_limit_usd"] = float("inf")
    path.write_text(json.dumps(doc))
    reader = FakeReader(cost=1.0)
    jobs.run_job(runs, rid, reader)
    meta = json.loads(path.read_text())["run"]
    assert meta["status"] == "needs_input"
    assert reader.prompts == []
    assert "finite" in meta["status_reason"].lower()


def test_resume_does_not_take_a_non_finite_limit(runs_dir):
    """A stopped run is resumed by raising the limit. 1e309 is Infinity, which is higher than every
    finite limit, so the resume stores it and the run goes on with a limit that can never be reached."""
    client, reader = make(runs_dir, FakeReader(cost=0.60))
    meta = client.post("/api/runs/new", json={**PTGX_ONLY, "spending_limit_usd": 0.5})
    assert meta.status_code == 202, meta.text
    rid = meta.json()["id"]
    saved = run_json(runs_dir, rid)["run"]
    assert saved["status"] == "needs_input"
    before, prompts = saved["choices"]["spending_limit_usd"], len(reader.prompts)
    got = client.post(f"/api/runs/{rid}/resume", content=b'{"spending_limit_usd":1e309}',
                      headers={"content-type": "application/json"})
    assert got.status_code == 422 and isinstance(got.json()["detail"], str)
    after = run_json(runs_dir, rid)["run"]
    assert after["choices"]["spending_limit_usd"] == before
    assert after["status"] == "needs_input"
    assert len(reader.prompts) == prompts


def test_the_limit_box_refuses_infinity():
    """The New run box and the Resume box do Number(text) and reject only NaN and negatives.
    Number('Infinity') and Number('1e309') are Infinity. JSON.stringify turns Infinity into null,
    and the server then stores no spending limit, so the run reads every page."""
    src = APP_JS.read_text()
    cases = (("limit = Number(limitRaw);", "limit"), ("var v = Number(limitInput.value.trim());", "v"))
    for anchor, var in cases:
        cond = _guard_after(src, anchor)
        for typed in ("Infinity", "1e309"):
            assert _guard_fires(cond, var, float(typed)), (anchor, cond, typed)


# ---------------------------------------------------------------- turn budget


def test_a_turn_budget_of_zero_makes_no_call(tmp_path):
    """create_run stores `turn_limit or 40`, so a budget of 0 is stored as 40 and the run calls the model.
    python -m runs.twelve --turns 0 uses this argument. Zero calls means a pause before the first call."""
    runs = tmp_path / "runs"
    runs.mkdir()
    run = jobs.create_run(runs, jobs.validate_choices(PTGX_ONLY), turn_limit=0)
    rid = run["run"]["id"]
    reader = FakeReader()
    jobs.run_job(runs, rid, reader)
    meta = json.loads((runs / rid / "run.json").read_text())["run"]
    assert meta["turn_limit"] == 0
    assert reader.prompts == []
    assert meta["status"] == "paused_turn_limit"


# ---------------------------------------------------------------- the export


def test_a_period_is_not_printed_as_its_first_day(tmp_path):
    """A row with a period end and no date text is a period. The export must show the end as well as
    the start. Printing the start alone makes the quarter look like a single day."""
    run_dir = _saved(tmp_path, [_row(date_text=None, title="Window")])
    view = build_view(*load_run_dir(run_dir), generated_at=FIXED)
    shown = view["calendar"][0]["date_display"]
    assert "2027-01-01" in shown and "2027-03-31" in shown and shown != "2027-01-01"
    built = build("run-8", run_dir=run_dir, out=tmp_path / "out", generated_at=FIXED)
    for text in (_pdf_text(built["pdf"]), _docx_text(built["docx"])):
        assert "2027-03-31" in text
        assert "Window" in text


def test_a_person_fix_of_the_date_stays_in_the_report(tmp_path):
    """The corrections list drops a person's fix when the field name occurs inside an earlier sentence.
    The send-back sentence contains 'date'. The person's later change of the date field is a different
    edit, and the export has to show it: the calendar then carries that date."""
    decisions = [
        {"action": "fix", "target": "e1", "by": "person", "at": "2026-10-29T13:00:00Z",
         "fix": {"field": "date", "from": "2027-01-01", "to": "2027-04-02"}},
        {"action": "recheck", "target": "e1", "by": "code", "at": "2026-10-29T13:00:01Z",
         "recheck": {"result": "pass", "checks": [], "before": {"result": "fail", "checks": []},
                     "after": {"result": "pass"}}},
    ]
    mistakes = [{"id": "m1", "target": "e1", "caught_by": "code", "what_changed": SEND_BACK}]
    run_dir = _saved(tmp_path, [_row()], decisions, mistakes)
    view = build_view(*load_run_dir(run_dir), generated_at=FIXED)
    assert view["state"]["ignored"] == []
    assert view["state"]["targets"]["e1"]["values"]["date"] == "2027-04-02"
    texts = [c["what"] for c in view["corrections"]]
    assert any("2027-04-02" in t for t in texts), texts
    assert any("Q1 2027" in t for t in texts), texts
    built = build("run-8", run_dir=run_dir, out=tmp_path / "out", generated_at=FIXED)
    for text in (_pdf_text(built["pdf"]), _docx_text(built["docx"])):
        assert "2027-04-02" in text
        assert "Q1 2027" in text
