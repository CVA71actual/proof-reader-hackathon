"""RESEARCHER-FULL-001: offline end-to-end test on SAVED real PTGX research runs (finish plan 2026-11-02 section 3).

No model, network or paid call. Two small fixtures cut from real saved runs by tests/fixtures/e2e_saved_run/
make_fixture.py (rows, cited sources and page files unchanged; see each PROVENANCE.txt):
  - reviewed/  review-smoke-21e10cd run-1: research plus a real Muse Spark Open review (incomplete, spend cap) with
               current findings on e1 and e2, and two sources the reviewer could not open;
  - brief/     smoke-brief run-1: research brief v2, not reviewed; e1 is "early Q1 2027".

The test drives the real path only: saved run folder -> server RunStore (page hashes verified) -> runs.state.
current_state -> review projection and calendar partition -> report.data.build_view -> the saved PDF and DOCX
(report.build, served back by GET /runs/{id}/report/{name}) -> the UI envelope GET /api/runs/{id}, and Remove then
Undo through POST /api/runs/{id}/decisions.

Register rows: NEXT-CALLER-001 (every downstream reader agrees), IDENTITY-001 (a finding binds to its row revision),
CLAIM-001 (no label claims more than happened), UNKNOWN-001 (an unopened source is named, never silent), DATE-001
(a stated period is placed by its period)."""
from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from report.build import build
from report.data import CALENDAR_HEADING, INCLUSION_LABELS, report_filenames
from runs.review import INCLUDED_LABEL
from server.app import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "e2e_saved_run"
RUN_ID = "run-1"
AT = "2026-11-03T04:00:00+00:00"
KEPT = "Kept by you"

# What the FULL saved runs derive for these rows (checked by hand against the whole run.json before cutting): the
# fixture must reproduce it, so a cut that changed the outcome is caught here.
EXPECTED = {
    "reviewed": {"e1": "review", "e2": "review", "e3": "included", "e4": "later", "e19": "included"},
    "brief": {"e1": "included", "e3": "included", "e4": "context", "e5": "undated"},
}
# inclusion -> calendar partition buckets that hold it (review and excluded rows are not in the partition)
PARTITION_OF = {"included": ("calendar",), "kept": ("calendar",), "context": ("past",), "later": ("later",),
                "undated": ("undated", "contingent", "timing_unresolved")}


def flat(text: str) -> str:
    return " ".join(text.split())


def pdf_text(data: bytes) -> str:
    return flat("\n".join(p.extract_text() or "" for p in PdfReader(BytesIO(data)).pages))


def docx_text(data: bytes) -> str:
    doc = Document(BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return flat("\n".join(parts))


class Served:
    """One fixture copied to a temp runs folder and served by the real app; the report saved and served back."""

    def __init__(self, name: str, tmp_path: Path):
        self.name = name
        self.runs_dir = tmp_path / "runs"
        self.run_dir = self.runs_dir / RUN_ID
        shutil.copytree(FIXTURES / name, self.run_dir, ignore=shutil.ignore_patterns("PROVENANCE.txt"))
        self.client = TestClient(create_app(runs_dir=self.runs_dir, ui_dir=self.runs_dir / "no-ui", reader=None,
                                            background=False, second_reader=None, wonder_reader=None,
                                            sam_reader=None))
        self.refresh()

    def refresh(self) -> None:
        r = self.client.get(f"/api/runs/{RUN_ID}")
        assert r.status_code == 200, r.text
        self.env = r.json()
        self.state = self.env["state"]
        self.rows = {t: s for t, s in self.state["targets"].items() if s["kind"] == "row"}
        report_dir = self.run_dir / "report"
        if report_dir.exists():
            shutil.rmtree(report_dir)
        out = build(RUN_ID, run_dir=self.run_dir, generated_at=AT)
        self.view = out["view"]
        pdf_name, docx_name = report_filenames(self.env["run"]["number"])
        served = {}
        for n in (pdf_name, docx_name):
            resp = self.client.get(f"/runs/{RUN_ID}/report/{n}")
            assert resp.status_code == 200, (n, resp.text)
            served[n] = resp.content
        assert served[pdf_name] == Path(out["pdf"]).read_bytes()
        assert served[docx_name] == Path(out["docx"]).read_bytes()
        self.pdf = pdf_text(served[pdf_name])
        self.docx = docx_text(served[docx_name])
        cal = self.client.get(f"/api/runs/{RUN_ID}/calendar")
        assert cal.status_code == 200, cal.text
        self.calendar_entries = cal.json()["entries"]

    def raw_row(self, rid: str) -> dict:
        return next(r for r in self.env["rows"] if r["id"] == rid)

    def decide(self, target: str, action: str) -> dict:
        r = self.client.post(f"/api/runs/{RUN_ID}/decisions",
                             json={"target": target, "action": action, "note": None, "fix": None})
        assert r.status_code == 200, r.text
        self.refresh()
        return r.json()


@pytest.fixture(params=sorted(EXPECTED))
def served(request, tmp_path):
    return Served(request.param, tmp_path)


@pytest.fixture
def reviewed(tmp_path):
    return Served("reviewed", tmp_path)


@pytest.fixture
def brief(tmp_path):
    return Served("brief", tmp_path)


def current_producer_findings(row: dict) -> list[dict]:
    """Findings in the real S2 producer shape (runs/research.py save_review) that are current for THIS row revision."""
    research = row.get("research") or {}
    return [f for f in research.get("review_findings") or []
            if isinstance(f, dict) and f.get("current") is True and f.get("text")
            and str(f.get("row_id")) == str(row["id"]) and str(f.get("revision_id")) == str(research.get("revision"))]


# ---------------------------------------------------------------------------------------------------------------------

def test_the_fixture_reproduces_the_full_saved_run(served):
    """The cut changes nothing: every fixture row derives the same inclusion the whole saved run gave it."""
    assert {t: s["inclusion"] for t, s in served.rows.items()} == EXPECTED[served.name]


def test_every_included_row_is_issue_free_with_current_checks_and_no_current_finding(served):
    """NEXT-CALLER-001 / IDENTITY-001: automatic inclusion only for a clean row: no issue, current checks, no finding."""
    included = [t for t, s in served.rows.items() if s["inclusion"] == "included"]
    assert included
    for t in included:
        s = served.rows[t]
        assert s["issues"] == [], (t, s["issues"])
        checks = s.get("effective_checks") or []
        assert checks, f"{t}: included with no current checks"
        assert not s.get("checks_stale") and not s.get("needs_recheck"), t
        assert all(c.get("result") != "fail" for c in checks), (t, checks)
        assert current_producer_findings(served.raw_row(t)) == [], t
        assert s["on_calendar"] and not s["needs_attention"] and not s["accepted"], t


def test_every_row_with_a_current_reviewer_finding_goes_to_review_and_the_text_reaches_state_pdf_and_docx(reviewed):
    """IDENTITY-001 / NEXT-CALLER-001: a current finding (text, row_id, revision_id, current) sends the row to review,
    off the calendar, and its words reach the state issues, the PDF and the Word file."""
    with_findings = {r["id"]: current_producer_findings(r) for r in reviewed.env["rows"]}
    with_findings = {k: v for k, v in with_findings.items() if v}
    assert sorted(with_findings) == ["e1", "e2"]          # the saved review's current findings on these rows
    partition_ids = {e["id"] for bucket in reviewed.env["calendar_partition"].values() for e in bucket}
    open_ids = {r["id"] for r in reviewed.view["open_rows"]}
    for rid, findings in with_findings.items():
        s = reviewed.rows[rid]
        assert s["inclusion"] == "review" and s["needs_attention"] and not s["on_calendar"], rid
        assert rid not in partition_ids and rid in open_ids, rid
        for f in findings:
            words = flat(f["text"])
            assert any(words in flat(i) for i in s["issues"]), (rid, f["finding_id"])
            assert words in reviewed.pdf, (rid, f["finding_id"], "PDF")
            assert words in reviewed.docx, (rid, f["finding_id"], "DOCX")


@pytest.mark.parametrize("name,rid,stated", [("brief", "e1", "early Q1 2027"),
                                             ("reviewed", "e1", "early first quarter 2027")])
def test_an_early_q1_2027_period_row_is_in_the_window_not_undated(tmp_path, name, rid, stated):
    """DATE-001: "early Q1 2027" is a stated period inside the run's window (2026-11-03 to 2027-05-03), never undated."""
    s = Served(name, tmp_path)
    row = s.raw_row(rid)
    assert row["date_text"] == stated
    t = s.rows[rid]
    assert t["placement"] == "calendar", t["placement"]
    assert t["timing_state"] not in ("undated", "timing_unresolved"), t["timing_state"]
    assert "undated" not in (t["date_display"] or "").lower(), t["date_display"]
    assert t["inclusion"] != "undated"
    if t["inclusion"] == "included":          # clean: on a date of the calendar, in the API, the export and the files
        assert rid in [e["id"] for e in s.env["calendar_partition"]["calendar"]]
        assert rid not in [e["id"] for e in s.env["calendar_partition"]["undated"]]
        assert rid in [r["id"] for r in s.view["calendar"]]
        assert flat(t["date_display"]) in s.pdf and flat(t["date_display"]) in s.docx


def _without_headings(text: str) -> str:
    """The files' fixed headings and summary line name 'Kept by you' as a category, not as a row's label."""
    text = text.replace(flat(CALENDAR_HEADING), "")
    for key, label in INCLUSION_LABELS:
        if key == "kept":
            text = text.replace(f"{label}:", "")
    return text


def test_no_label_says_kept_by_you_for_automatic_inclusion(served):
    """CLAIM-001: nothing was kept by a person in these runs, so no row label, API field or report line says so."""
    assert served.state["counts"]["inclusion"]["kept"] == 0
    for t, s in served.rows.items():
        for field in ("review_label", "decision_label"):
            assert KEPT not in (s.get(field) or ""), (t, field, s.get(field))
            assert "Accepted by you" not in (s.get(field) or ""), (t, field)
        if s["inclusion"] == "included":
            assert s["review_label"] == INCLUDED_LABEL and s["decision_label"] is None, t
    for row in served.view["calendar"] + served.view["open_rows"]:
        assert KEPT not in (row.get("review_label") or "") and KEPT not in (row.get("decision_label") or "")
        assert KEPT not in (row.get("person_tag") or "")
    for name, text in (("PDF", served.pdf), ("DOCX", served.docx)):
        assert KEPT not in _without_headings(text), name
        assert flat(INCLUDED_LABEL) in text, name


def _unopened(env: dict) -> int:
    reviews = ((env["run"].get("research") or {}).get("review") or {})
    n = 0
    for entry in reviews.values():
        record = (entry or {}).get("review") or {}
        n += sum(1 for x in record.get("limitations") or [] if x.get("kind") == "fetch")
        n += len(record.get("sources_not_given") or [])
    return n


def test_the_review_label_names_every_source_the_reviewer_could_not_open(reviewed):
    """UNKNOWN-001: a source the reviewer could not open is named in every label that claims a review."""
    n = _unopened(reviewed.env)
    assert n == 2
    want = f"{n} source{'s' if n != 1 else ''} could not be opened"
    labels = {"run (API)": (reviewed.env["run"].get("research") or {}).get("label") or "",
              "report view": reviewed.view.get("research_label") or ""}
    for r in reviewed.env["rows"]:
        label = (r.get("research") or {}).get("label") or ""
        if "review" in label:
            labels[f"row {r['id']} (API)"] = label
    for name, label in labels.items():
        assert want in label, (name, label)
    assert want in reviewed.pdf and want in reviewed.docx


def _check_counts_agree(s: Served) -> None:
    """NEXT-CALLER-001: one count per inclusion, the same in the API state, its calendar partition, the calendar
    route, the export view, the PDF and the Word file."""
    inc = s.state["counts"]["inclusion"]
    by_rows = {}
    for t, st in s.rows.items():
        by_rows[st["inclusion"]] = by_rows.get(st["inclusion"], 0) + 1
    assert {k: v for k, v in inc.items() if v} == by_rows
    part = s.env["calendar_partition"]
    for inclusion, buckets in PARTITION_OF.items():
        in_part = [e["id"] for b in buckets for e in part.get(b, []) if e.get("inclusion", inclusion) == inclusion]
        assert len(in_part) == inc[inclusion], (inclusion, in_part, inc[inclusion])
    assert sum(len(v) for v in part.values()) == sum(inc[k] for k in PARTITION_OF)
    assert len(s.calendar_entries) == sum(inc[k] for k in PARTITION_OF)
    v = s.view
    assert v["counts"]["included"] == inc["included"] and v["counts"]["review"] == inc["review"]
    assert len(v["calendar"]) == inc["included"] + inc["kept"]
    assert len(v["past"]) == inc["context"] and len(v["later"]) == inc["later"]
    assert len(v["undated"]) + len(v["contingent"]) + len(v["timing_unresolved"]) == inc["undated"]
    assert len(v["open_rows"]) == inc["review"] == s.state["unresolved"]
    assert len(v["exclusions"]) >= inc["excluded"]
    for name, text in (("PDF", s.pdf), ("DOCX", s.docx)):
        for key, label in v["group_labels"].items():
            assert f"{flat(label)}: {inc[key]}" in text, (name, key, label, inc[key])
        assert f"Unresolved (needs attention): {inc['review']}" in text, name


def test_api_pdf_docx_and_calendar_counts_agree(served):
    _check_counts_agree(served)


def test_remove_then_undo_round_trips_on_an_included_row(reviewed):
    """Remove takes an automatically included row off the calendar everywhere; Undo puts it back exactly as before."""
    rid = "e19"
    before = reviewed.rows[rid]
    assert before["inclusion"] == "included"
    before_counts = dict(reviewed.state["counts"]["inclusion"])
    before_calendar = [e["id"] for e in reviewed.env["calendar_partition"]["calendar"]]
    before_pdf_cal = [r["id"] for r in reviewed.view["calendar"]]
    assert rid in before_calendar

    reviewed.decide(rid, "remove")
    removed = reviewed.rows[rid]
    assert removed["inclusion"] == "excluded" and not removed["on_calendar"]
    assert removed["review_label"] == "Removed by you" and KEPT not in (removed["decision_label"] or "")
    assert rid not in [e["id"] for b in reviewed.env["calendar_partition"].values() for e in b]
    assert rid not in [r["id"] for r in reviewed.view["calendar"]]
    assert reviewed.state["counts"]["inclusion"]["excluded"] == 1
    assert reviewed.state["counts"]["inclusion"]["included"] == before_counts["included"] - 1
    _check_counts_agree(reviewed)

    reviewed.decide(rid, "undo")
    after = reviewed.rows[rid]
    for key in ("inclusion", "review_label", "decision_label", "issues", "placement", "date_display", "on_calendar",
                "accepted", "values", "review_basis"):
        assert after.get(key) == before.get(key), key
    assert dict(reviewed.state["counts"]["inclusion"]) == before_counts
    assert [e["id"] for e in reviewed.env["calendar_partition"]["calendar"]] == before_calendar
    assert [r["id"] for r in reviewed.view["calendar"]] == before_pdf_cal
    _check_counts_agree(reviewed)
    log = [json.loads(l) for l in (reviewed.run_dir / "decisions.jsonl").read_text().splitlines() if l.strip()]
    assert [d["action"] for d in log] == ["remove", "undo"]          # history kept, nothing rewritten
