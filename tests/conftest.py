import json
import hashlib
import re
import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FIXTURES = REPO / "fixtures"


@pytest.fixture(scope="session")
def manifest():
    return json.loads((FIXTURES / "MANIFEST.json").read_text())


@pytest.fixture(scope="session")
def words_doc(manifest):
    return json.loads((FIXTURES / manifest["words_file"]).read_text())


@pytest.fixture(scope="session")
def words(words_doc):
    return words_doc["words"]


def find_word(words, text):
    return next(w for w in words if w["text"] == text)


def box_of(*ws, pad=0.5):
    return [min(w["x0"] for w in ws) - pad, min(w["y0"] for w in ws) - pad,
            max(w["x1"] for w in ws) + pad, max(w["y1"] for w in ws) + pad]


def without_trails(run: dict) -> dict:
    """Ignore append-only presentation/provenance additions when comparing a rebuilt proposal to its saved source."""
    rows = []
    for row in run["rows"]:
        clean = {k: v for k, v in row.items() if k != "trail"}
        clean.pop("check_history", None)  # generated only by the zero-cost load-time re-check view
        clean["checks"] = [{k: v for k, v in check.items() if k != "assessment"}
                           for check in row.get("checks", [])]
        rows.append(clean)
    return {**run, "rows": rows}


def _r34_bound_baseline(run_dir: Path, run: dict, text: str) -> dict:
    """Replace only the synthetic baseline with a page artifact, then use the production $0 loader.

    The Red Team mutations (one-sided reader value, missing period bounds, and person program fix) remain
    in their original tests. No check result is supplied here: checks are recomputed from the stored words.
    """
    from runs.check_load import load_bound_run

    run_dir.mkdir(parents=True, exist_ok=True)
    page_dir = run_dir / "pages"
    page_dir.mkdir()
    rows = run.get("rows") or []
    pages = []
    words_for_page = {}
    for index, row in enumerate(rows, 1):
        row["source_id"], row["page_number"] = "s1", index
        row["title"] = "Phase 2"
        row["quote"] = text
        words, x = [], 24.0
        for n, token in enumerate(re.findall(r"\S+", text)):
            width = max(5.0, len(token) * 4.5)
            words.append({"id": f"w{n}", "text": token, "x0": x, "y0": 100.0,
                          "x1": x + width, "y1": 112.0})
            x += width + 2.0
        raw = (json.dumps({"page_width": 612.0, "page_height": 792.0, "words": words}) + "\n").encode()
        name = f"pages/p{index}.words.json"
        (run_dir / name).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        pages.append({"page_number": index, "words": name, "words_sha256": digest,
                      "width_pt": 612.0, "height_pt": 792.0})
        row["box"] = [23.0, 99.0, x, 113.0]
    run["sources"] = [{"id": "s1", "kind": "filing", "published_date": "2026-10-30", "pages": pages}]
    (run_dir / "run.json").write_text(json.dumps(run) + "\n")
    return load_bound_run(run_dir)


@pytest.fixture
def check37_evidence_baseline():
    """Explicit setup for the five check-37 cases that need a real bound baseline."""
    from report.data import build_view, load_run_dir

    class Baseline:
        def round8(self, saved, root, rows, generated_at):
            unsupported_root = root / "without_keep"
            unsupported_root.mkdir()
            unsupported_dir = saved(unsupported_root, copy.deepcopy(rows))
            run, _ = load_run_dir(unsupported_dir)
            unsupported = build_view(run, [], generated_at=generated_at)
            target = unsupported["state"]["targets"]["e1"]
            assert not target["on_calendar"] and not unsupported["calendar"]
            assert target["status"] != "matched"
            assert not any(c.get("name") == "date_match" and c.get("result") == "pass"
                           for c in target["effective_checks"])

            keep_root = root / "with_keep"
            keep_root.mkdir()
            keep = {"action": "keep", "target": "e1", "by": "person", "at": "2026-10-29T13:00:00Z",
                    "note": "Retain this proposed period for the rendering fixture; no passing date check is claimed."}
            kept_dir = saved(keep_root, rows, decisions=[keep])
            kept_run, kept_decisions = load_run_dir(kept_dir)
            from tests.pivot_assertions import fresh_keep
            # Add a NEW bound decision; retain the unbound line as history.
            kept_decisions.append(fresh_keep(kept_run, kept_decisions, "e1"))
            (kept_dir / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in kept_decisions))
            kept_view = build_view(kept_run, kept_decisions, generated_at=generated_at)
            kept_target = kept_view["state"]["targets"]["e1"]
            assert kept_target["status"] != "matched"
            assert kept_target["values"]["date_text"] is None
            assert not any(c.get("name") == "date_match" and c.get("result") == "pass"
                           for c in kept_target["effective_checks"])
            return kept_dir

        def r34(self, run_dir, run, text):
            return _r34_bound_baseline(run_dir, run, text)

    return Baseline()


def rebuilt_source_view(run: dict) -> dict:
    """Compare a fresh build to a loaded historical run without rewriting history.

    Current checks (including every result, reason and binding) remain exact.
    Only the historical check/trail/summary presentation and absent legacy
    proposal fields differ legitimately from a build made with today's code.
    """
    view = copy.deepcopy(run)
    for row in view.get("rows", []):
        for field in ("conditions", "relation", "relevance", "subject_company"):
            row.setdefault(field, None)
        row.pop("check_history", None)
        row.pop("trail", None)
        row.pop("reason", None)  # original verdict summary, retained in the saved file
    for table in view.get("tables", []):
        for row in table.get("rows", []):
            for cell in row.get("cells", []):
                cell.pop("check_history", None)
    return view
