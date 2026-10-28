"""Build the PDF and Word report for one saved run.

Usage:
    python -m report.build <run_id> [--out DIR]

Default output: runs/<run_id>/report/
A later call never rebuilds an earlier run's report in that folder.
--out is for tests only (always writes).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from report.data import build_view, load_run_dir, report_filenames
from report.docx_report import build_docx
from report.pdf_report import build_pdf

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"


class ReportExistsError(FileExistsError):
    """The run already has a saved report; past runs are never rebuilt."""


def resolve_run_dir(run_id: str, *, run_dir: Path | None = None) -> Path:
    if run_dir is not None:
        path = Path(run_dir)
    else:
        path = RUNS / run_id
    if not (path / "run.json").is_file():
        raise FileNotFoundError(f"No run.json at {path}")
    return path


def output_paths(run_number: int, out_dir: Path) -> tuple[Path, Path]:
    pdf_name, docx_name = report_filenames(run_number)
    return out_dir / pdf_name, out_dir / docx_name


def build(
    run_id: str,
    *,
    out: Path | None = None,
    run_dir: Path | None = None,
    generated_at: str | None = None,
    force: bool = False,
) -> dict:
    """Write PDF and DOCX. Returns paths and the view used.

    When out is None, writes under runs/<id>/report/ and refuses if those files
    already exist (past runs never rebuilt), unless force is true.
    When out is set (tests), always writes into that directory.
    """
    src = resolve_run_dir(run_id, run_dir=run_dir)
    run, decisions = load_run_dir(src)
    number = run["run"]["number"]
    out_dir = Path(out) if out is not None else (src / "report")
    pdf_path, docx_path = output_paths(number, out_dir)

    if out is None and not force and (pdf_path.exists() or docx_path.exists()):
        raise ReportExistsError(
            f"Report already saved for run {run_id} under {out_dir}; "
            "past runs are never rebuilt. Use --out for tests."
        )

    when = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    view = build_view(run, decisions, generated_at=when)
    pdf_bytes = build_pdf(view)
    docx_bytes = build_docx(view)

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf_bytes)
    docx_path.write_bytes(docx_bytes)
    return {
        "run_id": run_id,
        "out_dir": out_dir,
        "pdf": pdf_path,
        "docx": docx_path,
        "view": view,
        "generated_at": when,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the code-built PDF + Word report for one run.")
    parser.add_argument("run_id", help="Run folder name under runs/ (e.g. sample)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write here instead of runs/<id>/report/ (tests only; always overwrites)",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Explicit run folder (default: runs/<run_id>)",
    )
    args = parser.parse_args(argv)
    try:
        result = build(args.run_id, out=args.out, run_dir=args.run_dir)
    except ReportExistsError as e:
        print(str(e), file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Wrote {result['pdf']}")
    print(f"Wrote {result['docx']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
