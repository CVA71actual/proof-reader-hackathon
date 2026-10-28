"""Read the table on the captured page with Muse Spark (Meta), or build a labelled fake reading.

Usage (from the repo root):
    python -m capture.read_table            live reading, needs MODEL_API_KEY
    python -m capture.read_table --fake     offline fake reading built from the words file

The live reading is saved to fixtures/ions_ars_2026_p95.reading.json.
The fake reading is saved to fixtures/ions_ars_2026_p95.reading.fake.json (not committed) and is
always marked model "fake". It is never presented as a model reading.

The API key is read from the environment (MODEL_API_KEY), or from ~/.config/proof-reader/env
(KEY="value" lines). The key is never printed, logged or saved.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
WORDS_PATH = FIXTURES / "ions_ars_2026_p95.words.json"
MANIFEST_PATH = FIXTURES / "MANIFEST.json"
READING_PATH = FIXTURES / "ions_ars_2026_p95.reading.json"
FAKE_READING_PATH = FIXTURES / "ions_ars_2026_p95.reading.fake.json"

ENV_FILE = Path.home() / ".config" / "proof-reader" / "env"
BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-spark-1.3"
FAKE_MODEL = "fake"

INSTRUCTIONS = """\
You are reading one page of a company's SEC filing. The page holds a financial table.
Below is every word on the page, one per line, as: id|text|x0|y0|x1|y1
Coordinates are PDF points. The origin is the top-left corner of the page and y grows downward.
The page is {width} points wide and {height} points tall.

Read the table and answer with JSON only, in exactly this shape:
{{
  "title": "<table title as printed>",
  "columns": ["<column header>", ...],
  "rows": [
    {{
      "label": "<row label as printed>",
      "cells": [
        {{"column": "<one of columns>", "value": "<value as printed>", "box": [x0, y0, x1, y1]}}
      ]
    }}
  ]
}}

Rules:
- Use only the words given below. Do not add, round or recompute anything.
- "value" is the text exactly as printed: keep commas, decimals, parentheses for negatives, and "-" for a dash.
  Leave out a separate "$" sign.
- "box" is your proposal for where the value is printed: the smallest box, in PDF points, that encloses all
  the words of the value (including a closing parenthesis). If you cannot place it, use null.
- Include every row that has values. Skip section headings that have no values.
- Output the JSON object only, with no other text.

Words:
"""


# ---------------------------------------------------------------- environment


def load_env_file(path: Path | None = None) -> None:
    """Load KEY="value" lines into os.environ without overriding what is already set."""
    path = ENV_FILE if path is None else path  # read at call time so tests can point it elsewhere
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------- prompt


def load_words(path: Path = WORDS_PATH) -> dict:
    return json.loads(path.read_text())


def build_prompt(words_doc: dict) -> str:
    head = INSTRUCTIONS.format(width=_num(words_doc["page_width"]), height=_num(words_doc["page_height"]))
    lines = [
        f"{w['id']}|{w['text']}|{_num(w['x0'])}|{_num(w['y0'])}|{_num(w['x1'])}|{_num(w['y1'])}"
        for w in words_doc["words"]
    ]
    return head + "\n".join(lines) + "\n"


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


# ---------------------------------------------------------------- parsing


def parse_table(raw: str | None) -> tuple[dict | None, str | None]:
    """Return (table, problem). Never guesses: if the shape is wrong, table is None."""
    if not raw or not raw.strip():
        return None, "The model returned an empty answer."
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None, "The answer is not JSON."
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None, "The answer is not JSON."
    if not isinstance(data, dict):
        return None, "The answer is not a JSON object."
    rows = data.get("rows")
    if not isinstance(rows, list):
        return None, "The answer has no list of rows."
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cells"), list):
            return None, "A row is missing its list of cells."
        for cell in row["cells"]:
            if not isinstance(cell, dict):
                return None, "A cell is not a JSON object."
    columns = data.get("columns")
    table = {
        "title": data.get("title") if isinstance(data.get("title"), str) else "",
        "columns": [str(c) for c in columns] if isinstance(columns, list) else [],
        "rows": rows,
    }
    return table, None


# ---------------------------------------------------------------- live reading


def read_live(words_doc: dict) -> dict:
    load_env_file()
    key = os.environ.get("MODEL_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "No model key found. Set MODEL_API_KEY in the environment or in "
            f"{ENV_FILE} as MODEL_API_KEY=\"...\", then run again. (Use --fake to work offline.)"
        )
    from openai import OpenAI  # imported here so --fake and tests need no network client

    client = OpenAI(base_url=BASE_URL, api_key=key)
    del key
    prompt = build_prompt(words_doc)
    messages = [{"role": "user", "content": prompt}]

    response_format_used = "json_object"
    try:
        resp = _call(client, messages, json_mode=True)
    except Exception as e:  # some endpoints reject response_format; retry once without it
        if _status(e) in (400, 422):
            response_format_used = "none"
            try:
                resp = _call(client, messages, json_mode=False)
            except Exception as e2:
                raise SystemExit(_plain_error(e2)) from None
        else:
            raise SystemExit(_plain_error(e)) from None

    raw = resp.choices[0].message.content if resp.choices else None
    table, problem = parse_table(raw)
    return _reading_record(
        model=getattr(resp, "model", None) or MODEL,
        prompt=prompt,
        raw=raw,
        table=table,
        problem=problem,
        extra={"base_url": BASE_URL, "requested_model": MODEL, "response_format": response_format_used},
    )


def _call(client, messages, json_mode: bool):
    kwargs = {"model": MODEL, "messages": messages, "temperature": 0}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs)


def _status(e: Exception):
    return getattr(e, "status_code", None)


def _plain_error(e: Exception) -> str:
    # Only the error type and status code: never echo request details that could carry the key.
    code = _status(e)
    return f"The model call did not succeed ({type(e).__name__}{f', status {code}' if code else ''})."


def _reading_record(*, model, prompt, raw, table, problem, extra=None) -> dict:
    rec = {
        "model": model,
        "fake": model == FAKE_MODEL,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "page_sha256": _manifest().get("page_sha256"),
        "words_sha256": _file_sha(WORDS_PATH),
        "prompt_sha256": sha256_text(prompt),
        "prompt_text": prompt,
        "raw_response": raw,
        "readable": table is not None,
        "parse_problem": problem,
        "table": table,
    }
    if extra:
        rec.update(extra)
    return rec


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}


# ---------------------------------------------------------------- fake reading

# The fake reading is built from the words file by plain layout rules. Three cells are changed on
# purpose so that every status shows up in the viewer; each change is listed in "fake_changes".
FAKE_CHANGES = [
    {"row": "Total revenue", "column": "2025", "change": "value", "new_value": "943,717",
     "note": "one digit changed on purpose"},
    {"row": "Royalty revenue", "column": "2024", "change": "box_to_neighbour",
     "note": "box moved onto the cell to its left on purpose"},
    {"row": "Net loss", "column": "2023", "change": "no_box",
     "note": "box removed on purpose"},
]

_YEAR = re.compile(r"^\d{4}$")


def _lines(words: list[dict], tol: float = 1.5) -> list[list[dict]]:
    """Group words into visual lines by their top edge."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["y0"], w["x0"])):
        if out and abs(out[-1][0]["y0"] - w["y0"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in out]


def build_fake_table(words_doc: dict) -> dict:
    lines = _lines(words_doc["words"])
    header = next(line for line in lines if sum(bool(_YEAR.match(w["text"])) for w in line) >= 2)
    cols = [w for w in header if _YEAR.match(w["text"])]
    value_x = min(c["x0"] for c in cols) - 25  # values start a little left of the first header
    title_line = next((line for line in lines if any(w["text"] == "CONSOLIDATED" for w in line)), [])
    title = " ".join(w["text"] for w in title_line)

    rows = []
    for line in lines:
        if line is header or line[0]["y0"] <= header[0]["y0"]:
            continue
        label_words = [w for w in line if w["x1"] < value_x]
        tokens = [w for w in line if w["x0"] >= value_x and w["text"] != "$"]
        if not label_words or not tokens:
            continue
        groups: list[list[dict]] = []
        for t in tokens:
            if groups and t["text"] == ")" and t["x0"] - groups[-1][-1]["x1"] < 3:
                groups[-1].append(t)
            else:
                groups.append([t])
        cells = []
        for g in groups:
            right = g[0]["x1"]
            col = min(cols, key=lambda c: abs(c["x1"] - right))
            cells.append(
                {
                    "column": col["text"],
                    "value": " ".join(w["text"] for w in g),
                    "box": [min(w["x0"] for w in g), min(w["y0"] for w in g),
                            max(w["x1"] for w in g), max(w["y1"] for w in g)],
                }
            )
        rows.append({"label": " ".join(w["text"] for w in label_words), "cells": cells})

    table = {"title": title, "columns": [c["text"] for c in cols], "rows": rows}
    return apply_fake_changes(table)


def apply_fake_changes(table: dict) -> dict:
    table = copy.deepcopy(table)
    for ch in FAKE_CHANGES:
        row = next(r for r in table["rows"] if r["label"] == ch["row"])
        idx = next(i for i, c in enumerate(row["cells"]) if c["column"] == ch["column"])
        cell = row["cells"][idx]
        if ch["change"] == "value":
            cell["value"] = ch["new_value"]
        elif ch["change"] == "box_to_neighbour":
            cell["box"] = list(row["cells"][idx - 1]["box"])
        elif ch["change"] == "no_box":
            cell["box"] = None
        cell["fake_note"] = ch["note"]
    return table


def read_fake(words_doc: dict) -> dict:
    table = build_fake_table(words_doc)
    rec = _reading_record(
        model=FAKE_MODEL,
        prompt=build_prompt(words_doc),
        raw=json.dumps(table),
        table=table,
        problem=None,
        extra={
            "fake_note": "FAKE reading built by code from the words file. No model was called.",
            "fake_changes": FAKE_CHANGES,
        },
    )
    return rec


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="build a labelled fake reading offline")
    ap.add_argument("--out", type=Path, default=None, help="where to save the reading")
    args = ap.parse_args(argv)

    words_doc = load_words()
    if args.fake:
        reading = read_fake(words_doc)
        out = args.out or FAKE_READING_PATH
    else:
        reading = read_live(words_doc)
        out = args.out or READING_PATH
    out.write_text(json.dumps(reading, indent=1) + "\n")
    if reading["readable"]:
        n = sum(len(r["cells"]) for r in reading["table"]["rows"])
        print(f"Saved reading by {reading['model']}: {n} cells -> {out}")
    else:
        print(f"Saved an unreadable answer from {reading['model']} ({reading['parse_problem']}) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
