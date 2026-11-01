"""A served report must match the saved run, decisions, and report/state code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = ("report/data.py", "report/pdf_report.py", "report/docx_report.py",
                 "report/build.py", "report/manifest.py", "runs/state.py", "runs/calendar.py",
                 "runs/claim_assessment.py", "checks/event_checks.py", "checks/selector_match.py",
                 "checks/common.py")
MANIFEST = "manifest.json"


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def report_manifest(run: dict, decisions: list[dict]) -> dict:
    code = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in VERSION_FILES}
    content = {k: v for k, v in run.items() if k not in {"report_stale", "report_stale_reason"}}
    return {"run_sha256": _hash(content), "decisions_sha256": _hash(decisions), "code_sha256": _hash(code)}


def write_report_manifest(report_dir: Path, run: dict, decisions: list[dict]) -> None:
    (Path(report_dir) / MANIFEST).write_text(json.dumps(report_manifest(run, decisions), indent=2) + "\n")


def report_is_current(report_dir: Path, run: dict, decisions: list[dict]) -> bool:
    path = Path(report_dir) / MANIFEST
    if not path.is_file():
        return False
    try:
        saved = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return saved == report_manifest(run, decisions)
