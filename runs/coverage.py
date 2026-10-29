"""The open rows for dated sentences the reader skipped (checks/coverage.py finds them).

    possible_event_rows(words_doc, rows, source, ...)   one open row per uncovered sentence

Each is a row a person can see in Judgment needed: title "Possible event not read: <sentence>", status
"undecided", group "open", the sentence as its quote with its box, the first future date or period it names, no
checks (nothing proposed it, so nothing was checked) and `found_by: "coverage"`. The trail says plainly that no
model was asked: code found it. Scoring never counts these rows as proposals (accuracy/score.py).
"""

from __future__ import annotations

from datetime import date

from checks import coverage

SCAN_WHO = "checks.coverage"
TITLE = "Possible event not read: "
TITLE_MAX = 240


def possible_event_rows(words_doc: dict, rows: list[dict], source: dict, *, first_index: int, at: str,
                        today: date, ticker: str, company: str) -> list[dict]:
    """`rows`: the rows the reader proposed from this page (their quotes cover the sentences they came from).
    `today`: a date or period must start after it to count."""
    found = coverage.dated_sentences(words_doc["words"], today)
    quotes = [r.get("quote") for r in rows if r.get("found_by") != "coverage" and r.get("status") != "unreadable"]
    page = source["pages"][0]["page_number"]
    out = []
    for i, s in enumerate(coverage.uncovered(found, quotes)):
        title = TITLE + s["text"]
        out.append({
            "id": f"e{first_index + i}", "ticker": ticker, "company": company, "event_type": "unknown",
            "title": title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…",
            "date_text": s["date_text"], "date": s["date"], "date_end": s["date_end"], "quote": s["text"],
            "program": None, "indication": None, "source_id": source["id"], "page_number": page, "box": s["box"],
            "proposed_by": "Dated-sentence coverage (code)", "found_by": "coverage", "status": "undecided",
            "group": "open",
            "reason": (f"The reader did not turn this sentence into a row. It names {s['date_text']}, a date or "
                       f"period after {today.isoformat()}. Look at it: keep it as an event, fix it, or remove it."),
            "sample": False, "checks": [],
            "trail": [
                {"step": "Instructions sent", "tag": "Code", "who": SCAN_WHO, "at": at,
                 "text": (f"No model was asked. Code cut the page's words into sentences, looked in each for a "
                          f"date or period starting after {today.isoformat()}, and compared each with the quotes of "
                          f"the rows the reader proposed.")},
                {"step": "Found", "tag": "Code", "who": SCAN_WHO, "at": at,
                 "text": f"No proposed row quotes this sentence. It names {s['date_text']}."},
            ],
        })
    return out
