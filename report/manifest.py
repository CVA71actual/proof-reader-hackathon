"""A served report must match the saved run, decisions, and report/state code,
and its PDF/DOCX bytes must be the exact bytes this manifest names.

The report is one bundle: if either artifact is missing, replaced, or has no
recorded digest (a legacy manifest), the whole bundle is not current. Reads
never rebuild; they only refuse.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = ("report/data.py", "report/pdf_report.py", "report/docx_report.py",
                 "report/build.py", "report/manifest.py", "runs/state.py", "runs/calendar.py",
                 "runs/claim_assessment.py", "runs/review.py", "runs/proposal.py",
                 "runs/check_load.py", "runs/sources.py", "checks/event_checks.py", "checks/selector_match.py",
                 "checks/common.py")
MANIFEST = "manifest.json"


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def report_manifest(run: dict, decisions: list[dict]) -> dict:
    code = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in VERSION_FILES}
    content = {k: v for k, v in run.items() if k not in {"report_stale", "report_stale_reason"}}
    return {"run_sha256": _hash(content), "decisions_sha256": _hash(decisions), "code_sha256": _hash(code)}


def _artifact_names(run: dict) -> tuple[str, ...]:
    from report.data import report_filenames
    number = (run.get("run") or {}).get("number")
    return tuple(report_filenames(number)) if isinstance(number, int) else ()


def _read_artifact(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write to a sibling temp file, then rename over the target."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_report_manifest(report_dir: Path, run: dict, decisions: list[dict]) -> None:
    """Call after the PDF/DOCX exist: binds their bytes, then publishes the manifest last."""
    report_dir = Path(report_dir)
    artifacts = {}
    for name in _artifact_names(run):
        data = _read_artifact(report_dir / name)
        if data is not None:
            artifacts[name] = hashlib.sha256(data).hexdigest()
    saved = {**report_manifest(run, decisions), "artifacts": artifacts}
    atomic_write_bytes(report_dir / MANIFEST, (json.dumps(saved, indent=2) + "\n").encode())


def verified_report_bytes(report_dir: Path, run: dict, decisions: list[dict], name: str) -> bytes | None:
    """The bytes of `name` if and only if the whole saved bundle is current.

    Each artifact is read once and those exact bytes are hashed; the bytes
    returned for `name` are the ones that were verified. Missing manifest,
    changed inputs, a missing/legacy digest or a mismatch on either artifact
    all mean not current (None)."""
    report_dir = Path(report_dir)
    try:
        saved = json.loads((report_dir / MANIFEST).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(saved, dict):
        return None
    inputs = {k: v for k, v in saved.items() if k != "artifacts"}
    if inputs != report_manifest(run, decisions):
        return None
    names = _artifact_names(run)
    digests = saved.get("artifacts")
    if not names or name not in names or not isinstance(digests, dict) or set(digests) != set(names):
        return None
    served = None
    for artifact in names:
        data = _read_artifact(report_dir / artifact)
        if data is None or hashlib.sha256(data).hexdigest() != digests[artifact]:
            return None
        if artifact == name:
            served = data
    return served


def report_is_current(report_dir: Path, run: dict, decisions: list[dict]) -> bool:
    names = _artifact_names(run)
    return bool(names) and verified_report_bytes(report_dir, run, decisions, names[0]) is not None
