"""PERF (Manus 2026-11-03 08:09 §5, SAFE design): the checks-tree content digest is computed fresh once per stable
operation and dropped after it. No import-lifetime cache, no mtime key, no cached currency answer."""
import contextlib
import shutil
import threading
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from report.data import build_view, load_run_dir
from report.docx_report import build_docx
from report.pdf_report import build_pdf
from runs import claim_assessment as ca
from runs.state import current_state
from server.app import create_app

REPO = Path(__file__).resolve().parents[1]


def _outputs(runs_dir, rid):
    run, decisions = load_run_dir(runs_dir / rid)
    view = build_view(run, decisions, generated_at="2026-11-03T12:00:00Z")
    doc = Document(BytesIO(build_docx(view)))
    return {"state": current_state(run, decisions),
            "api": TestClient(create_app(runs_dir=runs_dir)).get(f"/api/runs/{rid}").json(),
            "pdf": build_pdf(view),
            "docx": [p.text for p in doc.paragraphs] + [c.text for t in doc.tables for r in t.rows for c in r.cells]}


def test_scoped_digest_outputs_are_byte_identical_to_per_call_hashing(tmp_path, monkeypatch):
    for rid in ("sample", "ptgx-r5"):
        shutil.copytree(REPO / "runs" / rid, tmp_path / rid, ignore=shutil.ignore_patterns("report"))
    scoped = {rid: _outputs(tmp_path, rid) for rid in ("sample", "ptgx-r5")}
    # the old behaviour: no scope anywhere, every checker_version call hashes the tree
    monkeypatch.setattr(ca, "checker_digest_scope", contextlib.nullcontext)
    calls = {"n": 0}
    real = ca._checks_tree_digest

    def counted():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(ca, "_checks_tree_digest", counted)
    for rid in ("sample", "ptgx-r5"):
        assert _outputs(tmp_path, rid) == scoped[rid], rid
    assert calls["n"] > 100          # the unscoped path really did hash per call


def _tree(tmp_path):
    shutil.copytree(REPO / "checks", tmp_path / "checks", ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path / "checks" / "common.py"


def test_one_digest_per_operation_and_a_checks_change_invalidates_in_the_next(tmp_path, monkeypatch):
    target = _tree(tmp_path)
    monkeypatch.setattr(ca, "REPO", tmp_path)
    calls = {"n": 0}
    real = ca._checks_tree_digest

    def counted():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(ca, "_checks_tree_digest", counted)
    with ca.checker_digest_scope():
        first = ca.checker_version("quote_match")
        assert ca.checker_version("date_match") == first and calls["n"] == 1   # computed once in the operation
        with ca.checker_digest_scope():                                       # a nested call shares it
            assert ca.checker_version("selector_match") == first and calls["n"] == 1
    target.write_bytes(target.read_bytes() + b"\n# a change to the checks code\n")
    with ca.checker_digest_scope():
        second = ca.checker_version("quote_match")
    assert second != first and calls["n"] == 2                                # a new operation sees the change
    assert ca.checker_version("quote_match") == second                        # outside any scope: fresh too


def test_a_check_bound_before_a_checks_change_is_not_current_in_the_next_operation(tmp_path, monkeypatch):
    from tests.test_pivot_proposal_flow import CASES, make_run
    _, run = make_run(CASES[0])
    row, source = run["rows"][0], run["sources"][0]
    check = next(c for c in row["checks"] if c["name"] == "quote_match")
    target = _tree(tmp_path)
    monkeypatch.setattr(ca, "REPO", tmp_path)
    rebound = ca.bind_contract_checks([check], row, source,
                                      consumed_artifact=check["assessment"]["evidence"]["words_used"],
                                      capability=ca.page_capability(source["pages"][0]))[0]
    with ca.checker_digest_scope():
        assert ca.check_current(rebound, row, source)
    target.write_bytes(target.read_bytes() + b"\n# changed\n")
    with ca.checker_digest_scope():
        assert not ca.check_current(rebound, row, source)


def test_a_context_copied_out_of_an_operation_cannot_reuse_its_digest(tmp_path, monkeypatch):
    target = _tree(tmp_path)
    monkeypatch.setattr(ca, "REPO", tmp_path)
    import contextvars
    with ca.checker_digest_scope():
        first = ca.checker_version("quote_match")
        leaked = contextvars.copy_context()             # e.g. a worker thread started inside a request
    target.write_bytes(target.read_bytes() + b"\n# changed\n")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", leaked.run(ca.checker_version, "quote_match")))
    t.start()
    t.join()
    assert out["v"] != first
