"""RESEARCHER-FULL-001: how the two small saved-run fixtures under this folder were cut from real saved PTGX runs.

Run once by hand (python -m tests.fixtures.e2e_saved_run.make_fixture SRC_RUN_DIR NAME ROW_ID...); the tests never
run it. A fixture is a strict subset of the saved run.json: the chosen rows unchanged byte for byte in content; the
sources they cite, each keeping only its cited pages; the cited page files (PNG and words) copied unchanged so every
recorded sha256 still holds; run.research without its bulky step log and audit (they are not read downstream). The
original run folders are only read."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROP_RESEARCH = ("steps", "audit", "refusals")


def cut(src_dir: Path, name: str, row_ids: list[str]) -> Path:
    run = json.loads((src_dir / "run.json").read_text())
    rows = [r for r in run["rows"] if r["id"] in row_ids]
    assert [r["id"] for r in rows] == row_ids, "every chosen row must exist, in the saved order"
    cited: dict[str, set] = {}
    for r in rows:
        cited.setdefault(r["source_id"], set()).add(r["page_number"])
        for span in (r.get("research") or {}).get("evidence") or []:
            cited.setdefault(span["source_id"], set()).add(span["page_number"])
    sources = []
    for s in run["sources"]:
        if s["id"] in cited:
            sources.append({**s, "pages": [p for p in s["pages"] if p["page_number"] in cited[s["id"]]]})
    out = HERE / name
    if out.exists():
        shutil.rmtree(out)
    (out / "pages").mkdir(parents=True)
    for s in sources:
        for p in s["pages"]:
            for key in ("image", "words"):
                if p.get(key):
                    shutil.copyfile(src_dir / p[key], out / p[key])
    research = {k: v for k, v in run["run"]["research"].items() if k not in DROP_RESEARCH}
    fixture = {**run, "run": {**run["run"], "research": research}, "sources": sources, "rows": rows}
    (out / "run.json").write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n")
    (out / "decisions.jsonl").write_text("")
    (out / "PROVENANCE.txt").write_text(
        f"Cut by make_fixture.py from {src_dir}/run.json (run {run['run']['id']}, started "
        f"{run['run'].get('started_at')}). Rows: {', '.join(row_ids)}. Sources: "
        f"{', '.join(f'{s} p{sorted(p)}' for s, p in sorted(cited.items()))}. Dropped run.research keys: "
        f"{', '.join(DROP_RESEARCH)}.\n")
    return out


if __name__ == "__main__":
    print(cut(Path(sys.argv[1]), sys.argv[2], sys.argv[3:]))
