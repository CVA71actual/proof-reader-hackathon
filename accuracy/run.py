"""Score a run or a reading against a checked answer key and write the report.

    python -m accuracy.run --key accuracy/keys/<page>.key.json --run <run_id> [--planted <file>] [--out <dir>]
    python -m accuracy.run --key <key.json> --reading <reading.json> --words <words.json> [--planted <file>]
    python -m accuracy.run --key <key.json> --run <run_id> [--planted <file>] --export <run_id>

The key is checked first (accuracy.key.check_key); a refused key stops here with every offending entry listed,
and nothing is scored. --run reads runs/<run_id>/run.json (and its decisions.jsonl) and scores the tables and
rows from the key's page. --reading runs the table checks on a saved reading first. --planted plants wrong
values into the reading and counts how many the checks caught (needs --reading, or the reading named in the
planted file). The report is printed as text; --out also writes report.json and report.txt there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from accuracy.key import KeyRefused, check_key, load_key
from accuracy.mistake_log import load_run, mistake_log
from accuracy.report import build_report, render_text
from accuracy.score import model_family, run_window, score_events, score_planted, score_table
from checks.status import check_reading

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"


class Refused(Exception):
    pass


def _page_of_run(run: dict, key: dict) -> tuple[dict, dict]:
    for s in run.get("sources", []):
        if s.get("sha256") != key.get("source_sha256"):
            continue
        for p in s.get("pages", []):
            if p.get("page_number") == key.get("page_number"):
                return s, p
    raise Refused(f"The run holds no page {key.get('page_number')} of the key's source.")


def score_run(key: dict, run_dir: Path, key_path: str | None = None, planted: list[dict] | None = None,
              reading: dict | None = None) -> dict:
    run, decisions = load_run(run_dir)
    source, page = _page_of_run(run, key)
    raw = (run_dir / page["words"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != page.get("words_sha256"):
        raise Refused(f"The run's words file {page['words']} does not match the hash recorded in run.json.")
    words_doc = json.loads(raw)
    problems = check_key(key, words_doc, raw)
    if problems:
        raise KeyRefused(problems)
    tables = [t for t in run.get("tables", []) if t.get("source_id") == source["id"]
              and t.get("page_number") == key["page_number"]]
    rows = [r for r in run.get("rows", []) if r.get("source_id") == source["id"]
            and r.get("page_number") == key["page_number"]]
    table = score_table(tables[0], key) if key.get("cells") and tables else None
    if key.get("cells") and not tables:
        table = score_table({"rows": [], "model": None}, key)
    events = score_events(rows, key, run.get("exclusions"), window=run_window(run)) if key.get("events") else None
    plant = score_planted(reading, words_doc, planted) if planted and reading else None
    return build_report(key, key_path, table=table, events=events, planted=plant,
                        run_log=mistake_log(run, decisions), run_id=run["run"]["id"])


def score_reading(key: dict, reading: dict, words_path: Path, key_path: str | None = None,
                  planted: list[dict] | None = None) -> dict:
    raw = words_path.read_bytes()
    words_doc = json.loads(raw)
    problems = check_key(key, words_doc, raw)
    if problems:
        raise KeyRefused(problems)
    want = key.get("words_sha256")
    if want and reading.get("words_sha256") and reading["words_sha256"] != want:
        raise Refused("The reading was made from another words file than the key's page.")
    model = model_family(reading.get("model"))
    table = score_table(check_reading(reading, words_doc), key, model) if key.get("cells") else None
    plant = score_planted(reading, words_doc, planted) if planted else None
    return build_report(key, key_path, table=table, planted=plant, planted_model=model)


def _load_planted(path: str | None) -> tuple[list[dict] | None, dict | None]:
    if not path:
        return None, None
    doc = json.loads(Path(path).read_text())
    reading = None
    if doc.get("reading"):
        reading = json.loads((REPO / doc["reading"]).read_text())
    return doc["plants"], reading


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m accuracy.run", description=__doc__.splitlines()[0])
    ap.add_argument("--key", required=True)
    ap.add_argument("--run", help="run id under runs/, or a run folder")
    ap.add_argument("--reading")
    ap.add_argument("--words")
    ap.add_argument("--planted")
    ap.add_argument("--out")
    ap.add_argument("--export", help="write accuracy.json into this scored run's folder (a run id under runs/, or a "
                                     "folder), for the screens to read; implies --run")
    a = ap.parse_args(argv)
    if a.export and not a.run and not a.reading:
        a.run = a.export
    if a.export and a.run and a.export != a.run:
        ap.error("--export names the run that was scored: give the same run to --run, or only --export")
    if a.export and a.reading:
        ap.error("--export needs a run to write beside: use --run, not --reading")
    if bool(a.run) == bool(a.reading):
        ap.error("give --run, or --reading with --words")
    if a.reading and not a.words:
        ap.error("--reading needs --words")
    key = load_key(a.key)
    plants, planted_reading = _load_planted(a.planted)
    try:
        run_dir = None
        if a.run:
            run_dir = Path(a.run) if Path(a.run).is_dir() and "/" in a.run else RUNS / a.run
            rep = score_run(key, run_dir, a.key, plants, planted_reading)
        else:
            reading = json.loads(Path(a.reading).read_text())
            rep = score_reading(key, reading, Path(a.words), a.key, plants)
    except KeyRefused as e:
        print("The answer key was refused; nothing was scored. Offending entries:", file=sys.stderr)
        for p in e.problems:
            print(f"  {p['where']}[{p['index']}]: {p['problem']} {json.dumps(p['entry'])}", file=sys.stderr)
        return 2
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 2
    text = render_text(rep)
    print(text, end="")
    if a.export:
        from accuracy import export
        written = export.write(run_dir, rep)
        print(f"Wrote {written}")
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(rep, indent=1) + "\n")
        (out / "report.txt").write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
