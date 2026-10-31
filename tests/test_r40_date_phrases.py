"""Job 40: the date-phrase catalogue, the one window rule, and the GLASS 9 #8 fix."""
from datetime import date
from pathlib import Path

import pytest

from checks import date_fixture_test as fx
from checks.event_checks import date_match, date_phrase_flag, in_window, parse_date_phrase
from server.jobs import window_reason

ROWS = fx.load()


@pytest.mark.parametrize("phrase,kind,start,end", [r[:4] for r in ROWS], ids=[r[0] or "blank" for r in ROWS])
def test_every_fixture_line_parses_to_its_kind_and_dates(phrase, kind, start, end):
    assert parse_date_phrase(fx.text_of(phrase), fx.FILING_DATE) == (kind, start, end)


def test_the_fixture_only_grows():
    have = {r[0] for r in ROWS}
    assert [p for p in fx.SEEDED if p not in have] == []
    assert len(have) == len(ROWS), "a phrase is listed twice"
    assert all(r[1] in ("day", "range", "month", "quarter", "half", "year", "relative", "none") for r in ROWS)


@pytest.mark.parametrize("phrase,want", list(fx.WINDOW_CASES.items()))
def test_the_seven_window_cases(phrase, want):
    _, start, end = parse_date_phrase(phrase, fx.FILING_DATE)
    assert in_window(start, end, fx.TODAY, fx.WINDOW_END) is want


def test_the_window_rule_edges():
    d = date
    assert in_window(None, None, d(2026, 10, 29), d(2027, 4, 29))
    assert in_window(d(2026, 7, 1), d(2026, 10, 29), d(2026, 10, 29), d(2027, 4, 29))        # ends today: in
    assert not in_window(d(2026, 7, 1), d(2026, 10, 28), d(2026, 10, 29), d(2027, 4, 29))
    assert in_window(d(2027, 4, 29), None, d(2026, 10, 29), d(2027, 4, 29))                  # starts on the last day: in
    assert not in_window(d(2027, 4, 30), None, d(2026, 10, 29), d(2027, 4, 29))


def test_window_reason_uses_the_one_rule():
    row = {"date_text": "second half of calendar year 2026", "date": "2026-07-01", "date_end": None}
    assert window_reason(row, fx.TODAY, fx.WINDOW_END, 6) is None
    assert window_reason({"date_text": "August 2026", "date": "2026-08-01", "date_end": None},
                         fx.TODAY, fx.WINDOW_END, 6).startswith("Outside the time window: the period ends 2026-08-31")


def test_ocr_noise_is_flagged_and_never_a_year():
    assert parse_date_phrase("HO2027") == ("none", None, None)
    assert date_phrase_flag("HO2027")
    assert date_phrase_flag("FY2027") is None and date_phrase_flag("CY2027") is None


QUOTE = "The Company expects topline results from the Phase 3 trial in the first half of calendar year 2027."


def _check(dt, quote=QUOTE, iso="2027-01-01", end="2027-06-30"):
    return date_match(dt, iso, quote, end, event_type="trial_results", title="Phase 3 topline results",
                      filed="2026-10-21")


@pytest.mark.parametrize("dt", ["first half of calendar year 2027", "first half of 2027", "first-half of calendar year 2027"])
def test_glass9_8_calendar_year_wording_passes(dt):
    assert _check(dt).result == "pass"


def test_second_half_hyphen_and_a_wrong_row_date_still_fails():
    q = "Data are expected in the second half of calendar year 2026."
    assert _check("second-half 2026", q, "2026-07-01", "2026-12-31").result == "pass"
    assert _check("second half of calendar year 2026", q, "2026-01-01", "2026-06-30").result == "fail"


def test_a_date_text_that_parses_to_none_could_not_run():
    q = "Dosing completes at week 105 of the study."
    assert _check("at week 105", q, "2027-01-01", "2027-06-30").result == "could_not_run"


# ---------------------------------------------------------------- runs.recheck_dates

def _run_with(row):
    return {"run": {"started_at": "2026-10-29T10:00:00+00:00", "choices": {"time_window_months": 6}},
            "sources": [{"id": "s1", "published_date": "2026-10-21", "pages": []}],
            "rows": [row], "exclusions": []}


def _row(rid="e1", **kw):
    base = {"id": rid, "source_id": "s1", "page_number": 1, "event_type": "trial_results", "title": "topline",
            "program": None, "quote": "topline results in August 2026.", "date_text": "August 2026",
            "date": "2026-08-01", "date_end": "2026-08-31", "status": "matched", "group": "kept", "reason": "",
            "trail": [], "checks": [{"name": "date_match", "label": "Date read as written", "kind": "Code",
                                     "result": "pass", "reason": "x", "compared": {}}]}
    return {**base, **kw}


def test_recheck_dates_excludes_a_past_period_and_leaves_a_decided_row_alone():
    from runs import recheck_dates as rd
    from pathlib import Path
    run = _run_with(_row())
    rd.recheck_dates(run, Path("."), set(), "t")
    assert rd.exclude_again(run, set(), "t")["out"] == ["e1"]
    assert run["rows"][0]["group"] == "removed" and run["exclusions"][0]["rule"] == "event_in_past"
    run = _run_with(_row())
    assert rd.exclude_again(run, {"e1"}, "t")["out"] == []
    assert rd.recheck_dates(run, Path("."), {"e1"}, "t")["person"] == 1 and run["rows"][0]["group"] == "kept"


def test_recheck_dates_keeps_a_period_that_contains_today():
    from runs import recheck_dates as rd
    run = _run_with(_row(date_text="second half of calendar year 2026", date="2026-07-01", date_end="2026-12-31",
                         quote="topline results in the second half of calendar year 2026."))
    assert rd.exclude_again(run, set(), "t")["out"] == []


# ---------------------------------------------------------------- Job 40b (Codex P2s on #121 and #118)

def test_within_weeks_and_years_are_relative_windows():
    assert parse_date_phrase("within 12 weeks", fx.FILING_DATE) == ("relative", date(2026, 10, 21), date(2027, 1, 13))
    assert parse_date_phrase("within 2 years", fx.FILING_DATE) == ("relative", date(2026, 10, 21), date(2028, 10, 21))
    assert parse_date_phrase("within 12 months", fx.FILING_DATE)[0] == "relative"


def test_flagged_ocr_noise_could_not_run_never_fails():
    r = date_match("HO2027", "2027-01-01", "HO2027")
    assert r.result == "could_not_run"
    q = "Topline data are expected in HO2027 for the Phase 3 trial."
    assert date_match("HO2027", "2027-01-01", q, event_type="trial_results", title="Phase 3").result == "could_not_run"


def _write_run(d, row):
    import json
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(_run_with(row)))


def test_write_saves_a_same_result_new_reason_change(tmp_path):
    import json
    from runs import recheck_dates as rd
    row = _row(date_text="August 2026", quote="topline results in August 2026.", date="2026-08-01",
               date_end="2026-08-31")
    row["checks"][0]["reason"] = "an older reason"
    run = _run_with(row)
    run["run"]["choices"] = {}                       # no window: only the date check can change
    d = tmp_path / "r1"
    d.mkdir()
    (d / "run.json").write_text(json.dumps(run))
    assert rd.recheck_dates(json.loads((d / "run.json").read_text()), d, set(), "t")["changed"] == []
    assert rd.main(["r1", "--runs-dir", str(tmp_path), "--write"]) == 0
    saved = json.loads((d / "run.json").read_text())["rows"][0]
    assert saved["checks"][0]["result"] == "pass" and saved["checks"][0]["reason"] != "an older reason"
    assert saved["trail"][-1]["step"] == "Date re-checked"


def _sample_copy(tmp_path, tamper=True, reports=True):
    import json
    import shutil
    from report.data import report_filenames
    runs = tmp_path / "runs"
    shutil.copytree(Path(__file__).resolve().parent.parent / "runs" / "sample", runs / "sample")
    d = runs / "sample"
    shutil.rmtree(d / "report", ignore_errors=True)
    run = json.loads((d / "run.json").read_text())
    run["run"].setdefault("choices", {}).pop("time_window_months", None)
    if tamper:
        for row in run["rows"]:
            for c in row.get("checks") or []:
                if c.get("name") == "date_match":
                    c["reason"] = "an older reason"
    (d / "run.json").write_text(json.dumps(run))
    pdf, docx = report_filenames(run["run"]["number"])
    if reports:
        (d / "report").mkdir()
        (d / "report" / pdf).write_bytes(b"old pdf")
        (d / "report" / docx).write_bytes(b"old docx")
    return runs, d, pdf, docx


def test_write_rebuilds_an_existing_report_from_the_new_state(tmp_path):
    import json
    from runs import recheck_dates as rd
    runs, d, pdf, docx = _sample_copy(tmp_path)
    assert rd.main(["sample", "--runs-dir", str(runs), "--write"]) == 0
    assert (d / "report" / pdf).read_bytes().startswith(b"%PDF") and (d / "report" / docx).read_bytes()[:2] == b"PK"
    assert "report_stale" not in json.loads((d / "run.json").read_text())
    assert not list((d / "report").glob("stale-*"))


def test_write_marks_a_report_stale_when_it_cannot_be_rebuilt(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    import report.build
    from runs import recheck_dates as rd
    from server.app import create_app
    runs, d, pdf, docx = _sample_copy(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("no renderer")
    monkeypatch.setattr(report.build, "build", boom)
    assert rd.main(["sample", "--runs-dir", str(runs), "--write"]) == 0
    saved = json.loads((d / "run.json").read_text())
    assert saved["report_stale"] is True and "no renderer" in saved["report_stale_reason"]
    moved = list((d / "report").glob("stale-*"))
    assert len(moved) == 1 and (moved[0] / pdf).read_bytes() == b"old pdf"
    assert not (d / "report" / pdf).exists()
    c = TestClient(create_app(runs_dir=runs, ui_dir=tmp_path / "none", reader=object()))
    assert c.get(f"/runs/sample/report/{pdf}").status_code == 404
    assert c.get(f"/runs/sample/report/{docx}").status_code == 404


def test_write_with_nothing_changed_leaves_reports_untouched(tmp_path):
    from runs import recheck_dates as rd
    runs, d, pdf, docx = _sample_copy(tmp_path, tamper=False)
    assert rd.main(["sample", "--runs-dir", str(runs), "--write"]) == 0      # settle any change first
    (d / "report").mkdir(exist_ok=True)
    (d / "report" / pdf).write_bytes(b"kept pdf")
    (d / "report" / docx).write_bytes(b"kept docx")
    run_bytes = (d / "run.json").read_bytes()
    assert rd.main(["sample", "--runs-dir", str(runs), "--write"]) == 0
    assert (d / "report" / pdf).read_bytes() == b"kept pdf" and (d / "report" / docx).read_bytes() == b"kept docx"
    assert (d / "run.json").read_bytes() == run_bytes


# ---------------------------------------------------------------- Job 45b (put-back reason is a string, never null)

def test_put_back_row_with_no_source_note_gets_empty_reason():
    from runs import recheck_dates as rd
    row = _row(group="removed", reason="Outside the 6-month window.", date_text="December 2026",
               date="2026-12-01", date_end="2026-12-31")
    run = _run_with(row)
    run["exclusions"] = [{"id": "x1", "target": "e1", "rule": rd.RULE, "reason": "old", "by": "code"}]
    assert rd.rewindow(run, set(), "t")["back"] == ["e1"]
    assert run["rows"][0]["reason"] == ""


def test_put_back_row_keeps_its_source_note():
    from runs import recheck_dates as rd
    row = _row(group="removed", reason="Outside the window. Source note: kept as read.", date_text="December 2026",
               date="2026-12-01", date_end="2026-12-31")
    run = _run_with(row)
    run["exclusions"] = [{"id": "x1", "target": "e1", "rule": rd.RULE, "reason": "old", "by": "code"}]
    rd.rewindow(run, set(), "t")
    assert run["rows"][0]["reason"] == "Source note: kept as read."


def test_run_schema_rejects_a_null_row_reason():
    import json
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parent.parent / "runs" / "schema" / "run.schema.json").read_text())
    row_schema = schema["$defs"]["row"]["properties"]["reason"]
    v = Draft202012Validator(row_schema)
    assert v.is_valid("") and not v.is_valid(None)
