"""The answer key for one page, and the code that checks the key before anything is scored against it.

Format (JSON, one file per page):

    {
      "key_version": 1,
      "sealed": false,                     true for the held-out set; this repo only holds unsealed keys
      "source_sha256": "<64 hex>",         the document the page was cut from
      "page_number": 95,
      "words_sha256": "<64 hex>",          optional: the page's words file, as captured
      "written_by": "...", "note": "...",  optional, plain words
      "cells":  [{"row_label": "...", "column": "...", "value": "..."}],
      "events": [{"ticker": "...", "program": "...", "event_type": "...", "date_text": "...", "quote": "..."}]
    }

check_key(key, words_doc) refuses a key that cannot be trusted, with every offending entry listed: every value
must pass text_layer_match on the page, every row label and column header must be printed on it, every quote
must pass quote_match and hold its date text, and no entry may appear twice. A key that is refused is never
scored against, so a typo in the key cannot count against the reader.

check_key returns the list of problems (empty when the key is good); raise_if_refused raises when that list is not empty.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from checks.common import PASS, normalize
from checks.event_checks import _norm_quote, quote_match
from checks.text_layer_match import text_layer_match

KEY_VERSION = 1
EVENT_TYPES = ("trial_results", "regulatory_decision", "launch", "conference", "filing", "milestone")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class KeyRefused(ValueError):
    """The key failed check_key. `problems` lists each offending entry."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__(f"The answer key was refused: {len(problems)} entr{'y' if len(problems) == 1 else 'ies'} "
                         "could not be found on the page.")


def load_key(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def cell_id(c: dict) -> tuple[str, str]:
    return normalize(c.get("row_label")), normalize(c.get("column"))


def event_id(e: dict) -> tuple[str, str, str]:
    return (normalize(e.get("ticker")).upper(), normalize(e.get("program")).lower(),
            normalize(e.get("event_type")).lower())


def _printed(text: str | None, words: list[dict]) -> bool:
    """Printed on the page as consecutive words (any length)."""
    return quote_match(text, words).result == PASS


def check_key(key: dict, words_doc: dict, words_bytes: bytes | None = None) -> list[dict]:
    """Return the list of problems ([] when the key can be used). See raise_if_refused."""
    problems: list[dict] = []
    words = (words_doc or {}).get("words") or []

    def bad(where: str, index: int | None, entry, why: str) -> None:
        problems.append({"where": where, "index": index, "entry": entry, "problem": why})

    if not isinstance(key, dict):
        return [{"where": "key", "index": None, "entry": None, "problem": "The key is not a JSON object."}]
    if key.get("key_version") != KEY_VERSION:
        bad("key", None, key.get("key_version"), f"The key needs key_version {KEY_VERSION}.")
    if not _HEX64.match(str(key.get("source_sha256") or "")):
        bad("key", None, key.get("source_sha256"), "The key needs the source's sha256.")
    if not isinstance(key.get("page_number"), int) or key["page_number"] < 1:
        bad("key", None, key.get("page_number"), "The key needs a page number.")
    if words_bytes is not None and key.get("words_sha256"):
        got = hashlib.sha256(words_bytes).hexdigest()
        if got != key["words_sha256"]:
            bad("key", None, got, "The page's words file is not the one the key was written from.")
    if not words:
        bad("key", None, None, "The page has no text layer to check the key against.")
        return problems

    cells, events = key.get("cells") or [], key.get("events") or []
    if not cells and not events:
        bad("key", None, None, "The key holds no values and no events.")
    seen_cells: set = set()
    for i, c in enumerate(cells):
        if not isinstance(c, dict) or not all(isinstance(c.get(f), str) and c.get(f) for f in
                                              ("row_label", "column", "value")):
            bad("cells", i, c, "A key value needs a row label, a column and a value.")
            continue
        if cell_id(c) in seen_cells:
            bad("cells", i, c, "The key lists this row and column twice.")
        seen_cells.add(cell_id(c))
        if text_layer_match(c["value"], words).result != PASS:
            bad("cells", i, c, f"The value {c['value']} is not printed on the page.")
        if not _printed(c["row_label"], words):
            bad("cells", i, c, f"The row label '{c['row_label']}' is not printed on the page.")
        if not _printed(c["column"], words):
            bad("cells", i, c, f"The column {c['column']} is not printed on the page.")
    seen_events: set = set()
    for i, e in enumerate(events):
        if not isinstance(e, dict) or not all(isinstance(e.get(f), str) and e.get(f) for f in
                                              ("ticker", "program", "event_type", "date_text", "quote")):
            bad("events", i, e, "A key event needs a ticker, a program, an event type, a date text and a quote.")
            continue
        if e["event_type"] not in EVENT_TYPES:
            bad("events", i, e, f"The event type {e['event_type']} is not one of the fixed event types.")
        if event_id(e) in seen_events:
            bad("events", i, e, "The key lists this ticker, program and event type twice.")
        seen_events.add(event_id(e))
        if quote_match(e["quote"], words).result != PASS:
            bad("events", i, e, "The quote is not printed on the page, word for word.")
        if normalize(e["date_text"]).lower() not in _norm_quote(e["quote"]).lower():
            bad("events", i, e, f"The date text '{e['date_text']}' is not part of the quote.")
        if normalize(e["program"]).lower() not in _norm_quote(e["quote"]).lower():
            bad("events", i, e, f"The program '{e['program']}' is not named in the quote.")
    return problems


def raise_if_refused(key: dict, words_doc: dict, words_bytes: bytes | None = None) -> dict:
    problems = check_key(key, words_doc, words_bytes)
    if problems:
        raise KeyRefused(problems)
    return key
