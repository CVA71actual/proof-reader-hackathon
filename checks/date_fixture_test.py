"""The date-phrase fixture, runnable on its own: `python -m checks.date_fixture_test`.

Prints one line per phrase in checks/fixtures/date_phrases.tsv (PASS/FAIL), then the window cases and the growth
guard, and exits 1 on any FAIL. $0, no model calls. tests/test_r40_date_phrases.py runs the same checks under pytest.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from checks.event_checks import in_window, parse_date_phrase

FIXTURE = Path(__file__).parent / "fixtures" / "date_phrases.tsv"
FILING_DATE = date(2026, 10, 21)               # every line is read with this filing date
TODAY, WINDOW_END = date(2026, 10, 29), date(2027, 4, 29)
# The window cases of the 31 Oct handoff: phrase -> is it in the window (today 2026-10-29, window end 2027-04-29).
WINDOW_CASES = {"second half of calendar year 2026": True, "Q4 2026": True, "by year end": True, "August 2026": False,
                "2028": False, "first half of calendar year 2027": True, "at week 105": True}
# Phrases seeded from the handoff. The fixture only grows: removing one of these is a failure.
SEEDED = (
    "March 12, 2027|12 March 2027|Wednesday, October 21, 2026|2027-03-12|Nov. 14, 2026|Saturday, November 14,2026|"
    "Friday, Sept. 4 at 10:00 a.m. ET|on or about October 27, 2026|by February 1, 2027|November 10 - 12, 2026|"
    "November 16–19, 2026|January 11 – 14, 2027|November 14 – 17, 2026 - Washington, D.C.|August 2026|Aug ’26|Feb ’27*|"
    "in October 2026|by the end of September 2026|the end of September 2026|Q1 2027|Q1 ’27|(Q1 ’27)|1Q27|4Q 2026|"
    "first quarter of 2027|the fourth quarter of 2026|during the fourth quarter of 2026|early Q1 2027|early Q1 '27|"
    "late Q3 2026|Q3 '26|1H 2027|1H ’27|H2 2026|2H'26|2H ’27|first half of 2027|in the first half of 2027|"
    "the second half of 2026|second half of calendar year 2026|the second half of calendar year 2026|"
    "first half of calendar year 2027|second-half 2026|2027|in 2027|calendar year 2027|calendar year 2028|CY2027|FY2027|"
    "fiscal 2027|YE 2026|YE’26|year-end 2026|by year-end|by year end|year - end|end of 2026|HO2027|today|Today|"
    "today at 4:30 p.m. Eastern Time|later this year|later this quarter|this year|in the coming weeks|"
    "in the coming months|upcoming|mid-2027|Mid ’27|early 2027|within 12 months|beginning in 2027|remainder of 2027|"
    "at week 105|following completion of the study|upon FDA approval|(blank)").split("|")


def _day(s: str):
    return date.fromisoformat(s) if s else None


def load(path: Path = FIXTURE) -> list[tuple[str, str, date | None, date | None, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t") + [""] * 5
        rows.append((cols[0], cols[1], _day(cols[2]), _day(cols[3]), cols[4]))
    return rows


def text_of(phrase: str) -> str:
    return "" if phrase == "(blank)" else phrase


def run() -> list[tuple[bool, str]]:
    out = []
    for phrase, kind, start, end, _ in load():
        got = parse_date_phrase(text_of(phrase), FILING_DATE)
        ok = got == (kind, start, end)
        out.append((ok, f"{'PASS' if ok else 'FAIL'}  {phrase!r}  -> {got[0]} {got[1]} {got[2]}"
                        + ("" if ok else f"   expected {kind} {start} {end}")))
    for phrase, want in WINDOW_CASES.items():
        _, start, end = parse_date_phrase(phrase, FILING_DATE)
        got = in_window(start, end, TODAY, WINDOW_END)
        out.append((got == want, f"{'PASS' if got == want else 'FAIL'}  window {phrase!r}: {'in' if got else 'out'}"
                                 f" (expected {'in' if want else 'out'})"))
    have = {r[0] for r in load()}
    for phrase in SEEDED:
        if phrase not in have:
            out.append((False, f"FAIL  seeded line removed: {phrase!r}"))
    return out


def main() -> int:
    results = run()
    for _, line in results:
        print(line)
    bad = sum(1 for ok, _ in results if not ok)
    print(f"{'PASS' if not bad else 'FAIL'}: {len(results) - bad} of {len(results)} lines")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
