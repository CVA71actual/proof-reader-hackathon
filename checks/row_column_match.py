"""Check: does the proposed box sit on the row label's line and under the column's header?

Shown on screen as "Right row and column".

Page geometry only, no model:
- the row: the words left of the table's value columns on the box's text line (a word is on the line when
  its vertical centre is within half a word height of the box's vertical centre) must read as the row label.
  A label printed over two or three lines ends on the value line; the lines above it are joined in when they
  hold label words only.
- the column: the header words for the column (e.g. "2025") are found above the box, on one header line with
  the other column headers. Each header owns a band that reaches halfway to its neighbouring headers (the
  outer bands reach out by the same half gap). The box's horizontal centre must fall in the wanted header's
  band. The neighbouring headers are taken from the page, not from the reader's list of columns: every
  other header word printed on that header line competes for the box, so leaving a column out of the list
  cannot widen another column's band.
- the claim checked is the row the value is saved in. If the reader also labelled the cell itself and that
  label is not the row's label, the check fails: the saved table would show the value under a row it was
  not read for.

pass when both hold, fail when either is found to be wrong, could_not_run when the label or the headers
cannot be located on the page (and nothing was found wrong).
"""

from __future__ import annotations

import re
from statistics import median

from checks.common import COULD_NOT_RUN, FAIL, PASS, CheckResult, normalize
from checks.selector_match import _valid_box

CHECK_ID = "row_column_match"
LABEL = "Right row and column"

# Tokens that sit between a label and its values and are not part of either.
_LEADERS = {"$"}
# A year printed as a column header, e.g. "2025" or "2024(1)".
_YEAR = re.compile(r"^\(?(?:19|20)\d{2}\)?\W*(?:\(\w{1,2}\))?$")
# How many lines above the value line a wrapped label may start.
MAX_LABEL_LINES = 3

# Header words that belong to a year: "December 31, 2025", "Dec. 31, 2025" and "FY2025" all head column "2025".
_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_DATE_YEAR = re.compile(r"^(?:(?:" + "|".join(_MONTHS) + r")\.?\s+\d{1,2},?\s+)?((?:19|20)\d{2})$", re.I)
_FISCAL_YEAR = re.compile(r"^(?:fy|fiscal(?: year)?)\s*((?:19|20)\d{2})$", re.I)
_DATE_PREFIX = re.compile(r"^(?:(?:" + "|".join(_MONTHS) + r")\.?|\d{1,2},?|fy|fiscal|year)$", re.I)


# Dot leaders: fill between a row label and its values (". . . . .", "......", "…", "____", "- - -").
_FILL_WORD = re.compile(r"^[.\u2026_\-\u2013\u2014]+$")
_TRAILING_FILL = re.compile(r"(?:[._\-\u2013\u2014]{2,}|\u2026)[.\u2026_\-\u2013\u2014]*$")


def _cut_leaders(line: list[dict]) -> list[dict]:
    """The label part of a line: the words before the first leader run. A word that ends in leaders
    ("revenue......") keeps what is before them. Anything after a leader run is not label."""
    out = []
    for w in line:
        if _FILL_WORD.match(w["text"]):
            break
        m = _TRAILING_FILL.search(w["text"])
        if m and m.start() > 0:
            out.append({**w, "text": w["text"][: m.start()]})
            break
        out.append(w)
    return out


def _key(text: str | None) -> str:
    """A column's name as compared: a year printed with its date or fiscal prefix is just the year."""
    t = normalize(text)
    m = _DATE_YEAR.match(t) or _FISCAL_YEAR.match(t)
    return m.group(1) if m else t


def _extend(run: list[dict], line: list[dict]) -> list[dict]:
    """A year header with its date words in front ("Dec. 31," before "2025") is one header: take them in."""
    out = list(run)
    if not _YEAR.match(out[0]["text"]):
        return out
    i = line.index(out[0])
    while i > 0 and _DATE_PREFIX.match(line[i - 1]["text"]):
        out.insert(0, line[i - 1])
        i -= 1
    return out


def _cx(w) -> float:
    return (w["x0"] + w["x1"]) / 2


def _cy(w) -> float:
    return (w["y0"] + w["y1"]) / 2


def _height(w) -> float:
    return w["y1"] - w["y0"]


def _same_line(words: list[dict], y: float, tol: float) -> list[dict]:
    return sorted((w for w in words if abs(_cy(w) - y) <= tol), key=lambda w: w["x0"])


def _text(words: list[dict]) -> str:
    return normalize(" ".join(w["text"] for w in words))


def _runs(text: str, words: list[dict], above_y: float, tol: float) -> list[list[dict]]:
    """Every run of words on one line, ending above `above_y`, that reads as `text`."""
    want = _key(text)
    if not want:
        return []
    n = len(want.split(" "))
    found = []
    for w in words:
        if w["y1"] > above_y + tol:
            continue
        line = _same_line(words, _cy(w), tol)
        i = line.index(w)
        run = line[i : i + n]
        if len(run) == n and _key(_text(run)) == want:
            found.append(run)
    return found


def _find_header(text: str, words: list[dict], above_y: float, tol: float,
                 columns: list[str] | None = None, values: list[float] = ()) -> list[dict] | None:
    """The header run for `text` above the box. Several lines may print it (a caption, a repeated year), so the
    line is chosen by the page: the one that holds the most of the listed columns, then the one whose headers sit
    best over the value glyphs on the box's line (`values`: their centres), then the lowest."""
    runs = _runs(text, words, above_y, tol)
    if not runs:
        return None

    def on_line(r):
        line = _same_line(words, _cy(r[0]), tol)
        found = [rr for c in (columns or []) if (rr := _runs(c, line, _cy(r[0]) + tol, tol))]
        return [rr[0] for rr in found] or [r]

    def misalignment(r):
        if not values:
            return 0.0
        centres = [(rr[0]["x0"] + rr[-1]["x1"]) / 2 for rr in on_line(r)]
        return sum(min(abs(c - v) for v in values) for c in centres) / len(centres)

    def coverage(r):
        line = _same_line(words, _cy(r[0]), tol)
        return sum(bool(_runs(c, line, _cy(r[0]) + tol, tol)) for c in columns or [])

    return min(runs, key=lambda r: (-coverage(r), round(misalignment(r), 1), -_cy(r[0])))


def _bands(headers: list[tuple[str, float]], single_half: float = 0.0) -> list[tuple[str, float, float]]:
    """(text, centre) sorted left to right -> (text, left, right); bands meet halfway between centres.
    A lone header has no neighbour to meet: its band reaches `single_half` either side of its centre."""
    headers = sorted(headers, key=lambda h: h[1])
    if len(headers) == 1:
        c = headers[0][1]
        return [(headers[0][0], c - single_half, c + single_half)]
    out = []
    for i, (text, c) in enumerate(headers):
        left = (headers[i - 1][1] + c) / 2 if i > 0 else c - (headers[1][1] - c) / 2
        right = (c + headers[i + 1][1]) / 2 if i + 1 < len(headers) else c + (c - headers[i - 1][1]) / 2
        out.append((text, left, right))
    return out


def row_column_match(
    row_label: str | None,
    column: str | None,
    box,
    words: list[dict],
    columns: list[str] | None = None,
    reader_label: str | None = None,
) -> CheckResult:
    """`row_label`: the label of the row the value is saved in (the row the table shows it under).
    `columns`: the table's column headers as the reader gave them; they say how a header of several words
    reads, and never remove a header printed on the page. If not given, every word on the wanted header's
    line is taken as a header.
    `reader_label`: a label the reader put on the cell itself, if any. It is never the claim checked; if it
    is not the row's label, the check fails."""
    want_row = normalize(row_label)
    want_col = _key(column)
    reader = normalize(reader_label)
    looked_for = f"row {want_row or '?'}, column {want_col or '?'}"

    def result(res: str, reason: str, found_row=None, found_col=None) -> CheckResult:
        found = None
        if found_row is not None or found_col is not None:
            found = f"row {found_row if found_row is not None else '?'}, column {found_col if found_col is not None else '?'}"
        return CheckResult(CHECK_ID, LABEL, res, reason, {
            "value": looked_for, "found": found, "box": box,
            "looked_for_row": want_row, "looked_for_column": want_col,
            "found_row": found_row, "found_column": found_col,
            "reader_label": reader or None,
        })

    if reader and reader != want_row:
        where = f"row '{want_row}'" if want_row else "a row with no label"
        return result(FAIL, f"The reader labelled this cell '{reader}' but it sits in {where}.")

    if box is None:
        return result(COULD_NOT_RUN, "The reader did not propose a place on the page for this value.")
    if not _valid_box(box) or not (box[2] > box[0] and box[3] > box[1]):
        return result(FAIL, "The proposed place on the page is not a usable box.")
    if not words:
        return result(COULD_NOT_RUN, "The page has no text layer to compare against.")
    x0, y0, x1, y1 = (float(v) for v in box)
    bx, by = (x0 + x1) / 2, (y0 + y1) / 2
    tol = median(_height(w) for w in words) / 2
    values = [_cx(w) for w in _same_line(words, by, tol)
              if any(ch.isdigit() for ch in w["text"]) and w["text"] not in _LEADERS]

    # ---- the column
    found_col = None
    col_problem = None  # reason the column could not be located
    left_edge = None    # where the value columns start; label words sit left of it
    columns = [_key(c) for c in columns] if columns else columns
    header = _find_header(want_col, words, y0, tol, columns, values) if want_col else None
    if not want_col:
        col_problem = "The reader gave no column for this value."
    elif header is None:
        col_problem = f"The column header {want_col} was not found above the box."
    else:
        hy = _cy(header[0])
        line = _same_line(words, hy, tol)
        header = _extend(header, line)
        heads = []   # (name, centre, left edge of its words)
        if columns:
            used = {id(w) for w in header}
            for text in columns:
                runs = _runs(text, line, hy + tol, tol)
                if runs:
                    run = _extend(runs[0], line)
                    heads.append((_key(text), (runs[0][0]["x0"] + runs[0][-1]["x1"]) / 2, run[0]["x0"]))
                    used.update(id(w) for w in run)
            if not any(t == want_col for t, _, _ in heads):
                heads.append((want_col, (header[0]["x0"] + header[-1]["x1"]) / 2, header[0]["x0"]))
            # page evidence: every other header word printed on the line competes too. A year anywhere on
            # the line is a header; any other word counts once it is right of the first listed header (a
            # stub heading such as "(in thousands)" at the left is not a value column).
            first_x0 = min(w["x0"] for w in line if id(w) in used)
            for w in line:
                if id(w) in used or w["text"] in _LEADERS:
                    continue
                if _YEAR.match(w["text"]) or w["x0"] >= first_x0:
                    heads.append((normalize(w["text"]), _cx(w), w["x0"]))
        else:
            heads = [(normalize(w["text"]), _cx(w), w["x0"]) for w in line]
            if not any(t == want_col for t, _, _ in heads):
                heads.append((want_col, (header[0]["x0"] + header[-1]["x1"]) / 2, header[0]["x0"]))
        # one header alone: its band reaches twice its own width (or the box's width) either side of its centre
        half = max(2 * (header[-1]["x1"] - header[0]["x0"]), x1 - x0)
        bands = _bands([(t, c) for t, c, _ in heads], half)
        left_edge = min(left for _, _, left in heads)   # where the value columns start; label words sit left of it
        under = next((t for t, lo, hi in bands if lo <= bx < hi), None)
        found_col = under if under is not None else "no column"
        if under != want_col:
            where = f"under {under}" if under is not None else "outside every column"
            return result(FAIL, f"The box sits {where}, not {want_col}.", None, found_col)

    # ---- the row
    if not want_row:
        return result(COULD_NOT_RUN, col_problem or "The reader gave no row label for this value.", None, found_col)

    def label_words(y: float) -> list[dict]:
        if left_edge is None:
            # no header bands: take the words left of the box that read as words, not as values
            return _cut_leaders([w for w in _same_line(words, y, tol)
                                 if w["x1"] <= x0 and (any(ch.isalpha() for ch in w["text"])
                                                       or _FILL_WORD.match(w["text"]))])
        return _cut_leaders([w for w in _same_line(words, y, tol)
                             if w["x1"] <= min(left_edge, x0) and w["text"] not in _LEADERS])

    def line_has_values(y: float) -> bool:
        edge = left_edge if left_edge is not None else x0
        return any(w["x0"] >= edge and w["text"] not in _LEADERS for w in _same_line(words, y, tol))

    on_line = label_words(by)
    found_row = _text(on_line)
    if not on_line:
        return result(COULD_NOT_RUN, "No row label was found on the box's line.", None, found_col)
    if found_row != want_row:
        # a label wrapped over lines above the value line: join those lines when they hold label words only
        joined, y = found_row, by
        step = 2 * tol + 1  # about one line pitch
        for _ in range(MAX_LABEL_LINES - 1):
            above = [w for w in words if w["y1"] <= y - tol and abs(_cy(w) - (y - step)) <= step / 2 + tol]
            if not above:
                break
            y = max(_cy(w) for w in above)
            if line_has_values(y):
                break
            prev = label_words(y)
            if not prev:
                break
            joined = normalize(_text(prev) + " " + joined)
            if joined == want_row:
                found_row = joined
                break
    if found_row != want_row:
        return result(FAIL, f"The row label '{want_row}' is not on this line; found '{found_row}'.",
                      found_row, found_col)
    if col_problem is not None:
        return result(COULD_NOT_RUN, col_problem, found_row, None)
    return result(PASS, f"The box is on the '{want_row}' line and under {want_col}.", found_row, found_col)
