"""TICKER-INTAKE-001: prepare any pasted tickers for a New Run, before the existing POST /api/runs/new.

POST /api/intake {"tickers": "<raw text>"} -> 202 {"intake_id"}; GET /api/intake/{id} -> the record below.
The preparation is one background thread per intake (the same mechanism run jobs use: threading + jobs._write, whole
file replaced by rename); only one intake captures at a time, through one shared SEC fetcher, so SEC throttling
(capture.fetch_filings.RATE_INTERVAL) holds across intakes. It reuses capture/fetch_filings.py as it is: the ticker rule
(TICKER_RE), the SEC identity lookup (resolve_cik over company_tickers.json), the capture (fetch_ticker) and the index
shape (build_index). It never calls a model and never reads a key.

Record (runs/intake/<id>/intake.json):
    status          preparing | done
    requested       the symbols after normalising (upper case) and removing repeats, in the order pasted
    tickers         one per requested symbol: symbol, cik, company, state, reason, documents, pages, form_scope,
                    lookback_days, window_start, cutoff, failed_documents, pages_per_document, pages_captured,
                    pages_in_run (pages = pages_captured: every page captured; pages_in_run: the capped pages a run
                    made from the intake reads)
    pages_per_document, pages_captured, pages_in_run   the same, over the whole intake
    ready_count     tickers whose state is "ready"; failed_count every other finished state
state, per ticker:
    resolving       looking the symbol up in SEC identity data
    capturing       fetching and rendering its documents
    ready           at least one document captured; the run may read it
    unresolved      SEC identity data has no company for the symbol (or more than one): never guessed
    no_documents    the company is known but filed no document of the scope in the window
    unsupported     the symbol cannot be looked up, or the company files only forms this capture does not read (6-K)
    failed          the SEC could not be reached or every document failed to capture
None of these is "no catalysts": that is a finding of a run, and only a run that read pages can make it.

Scope (recorded on every ticker so a calendar comparison is meaningful): 8-K EX-99 HTML exhibits filed in the
LOOKBACK_DAYS days up to the cutoff date. That is recent press releases and decks, not complete company research.

Page cap (INTAKE-PAGE-CAP-001): every page of every document is captured, but the ready-pages index keeps only the
first PAGES_PER_DOCUMENT pages of each document (by page_number, per accession and exhibit), where the catalysts are;
the rest are mostly financial tables. A page over the cap is never in a run's index, so no run reads it or shows it
as read, and the counts (pages_captured, pages_in_run) and the reason say so.

When the preparation is done it writes runs/intake/<id>/sources_index.json (read-only) holding the ready tickers'
pages only, with absolute file paths into the intake's own filings folder (~/proof-reader-docs/intake/<id>/filings),
which no other capture writes. A run made from the intake copies that file into its own folder and reads only it
(snapshot_into_run): the job freezes that copy (Job 51f), so a running run's sources never change and no unrelated
cached page is read. A run that leaves out a requested symbol needs accept_ready_subset true (check_subset).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from capture import fetch_filings as ff
from runs import sources
from server import jobs

MAX_TICKERS = 25
MAX_RAW_CHARS = 2000
LOOKBACK_DAYS = 90
FORM_SCOPE = ("8-K",)
STATES = ("resolving", "capturing", "ready", "unresolved", "no_documents", "unsupported", "failed")
FINISHED = ("ready", "unresolved", "no_documents", "unsupported", "failed")
INTAKE_DIR = "intake"
INTAKE_FILE = "intake.json"
PAGES_PER_DOCUMENT = 2
INDEX_FILE = "sources_index.json"
_ID = re.compile(r"^in-[0-9a-f]{12}$")
_SPLIT = re.compile(r"[\s,;]+")

# one intake captures at a time: the shared fetcher keeps the SEC rate across intakes and is not thread-safe
PREP_LOCK = threading.Lock()
_WRITE_LOCK = threading.RLock()
# one run per intake: the check and the bind happen under this lock (a lost response, a double click or a second tab
# never admits a second paid run from the same intake)
_RUN_LOCK = threading.Lock()
USED_TEXT = "A run was already started from these tickers."


class IntakeError(Exception):
    def __init__(self, status: int, detail: str, run_id: str | None = None):
        super().__init__(detail)
        self.status, self.detail, self.run_id = status, detail, run_id


# ---------------------------------------------------------------- the pasted text


def normalise(raw) -> list[str]:
    """The pasted text as symbols: split on spaces, new lines, commas and semicolons, upper case, repeats removed,
    in the order pasted. Raises IntakeError 422 for no text, too much text, or more than MAX_TICKERS symbols. A token
    that breaks the ticker rule is kept (it is reported as unsupported, never dropped)."""
    if not isinstance(raw, str):
        raise IntakeError(422, "tickers must be text: symbols separated by spaces, commas or new lines.")
    if len(raw) > MAX_RAW_CHARS:
        raise IntakeError(422, f"That is more than {MAX_RAW_CHARS} characters; paste at most {MAX_TICKERS} tickers.")
    out: list[str] = []
    for part in _SPLIT.split(raw.strip()):
        t = part.strip().upper()
        if t and t not in out:
            out.append(t)
    if not out:
        raise IntakeError(422, "Paste at least one ticker.")
    if len(out) > MAX_TICKERS:
        raise IntakeError(422, f"{len(out)} tickers were pasted; one intake takes at most {MAX_TICKERS}.")
    return out


# ---------------------------------------------------------------- the preparer


class Preparer:
    """What an intake uses to reach the SEC. Tests give a fake fetcher, a stub renderer, a temp filings root and a
    fixed day; the server's default is the live SEC fetcher (SEC_USER_AGENT), headless Chrome and today."""

    def __init__(self, fetcher=None, render: Callable | None = None, filings_root: Path | None = None,
                 today: Callable[[], date] | None = None, tickers_cache: Path | None = None,
                 background: bool = True):
        self._fetcher = fetcher
        self.render = render or ff.render_html_to_pdf
        self.filings_root = Path(filings_root) if filings_root else ff.DOCS / "intake"
        self.today = today or date.today
        self.tickers_cache = Path(tickers_cache) if tickers_cache else ff.COMPANY_TICKERS_CACHE
        self.background = background

    def fetcher(self):
        if self._fetcher is None:
            self._fetcher = ff.RateLimitedFetcher(ff.load_user_agent())
        return self._fetcher


def _ticker_rows(doc) -> list[dict]:
    rows = doc.values() if isinstance(doc, dict) else doc
    return [r for r in rows if isinstance(r, dict)]


class _Identity:
    """SEC identity data for one intake: the local company_tickers.json cache, refreshed from the SEC at most once
    when a symbol is not in it (absence from a stale cache is not invalidity: CRNX)."""

    def __init__(self, prep: Preparer):
        self.prep, self.doc, self.refreshed = prep, None, False

    def _load(self):
        if self.doc is None:
            p = self.prep.tickers_cache
            if p.is_file():
                try:
                    self.doc = json.loads(p.read_text())
                except json.JSONDecodeError:
                    self.doc = None
            if self.doc is None:
                self._refresh()

    def _refresh(self):
        self.doc = self.prep.fetcher().get_json(ff.COMPANY_TICKERS_URL)
        self.refreshed = True
        p = self.prep.tickers_cache
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc))
        os.replace(tmp, p)

    def matches(self, symbol: str) -> list[dict]:
        return [r for r in _ticker_rows(self.doc) if str(r.get("ticker", "")).upper() == symbol]

    def resolve(self, symbol: str) -> tuple[str, str] | tuple[None, str]:
        """(cik, company), or (None, plain reason) when SEC identity data has no single company for the symbol.
        FetchError when the SEC cannot be reached."""
        if symbol in ff.CIKS:
            return ff.resolve_cik(symbol)
        self._load()
        found = self.matches(symbol)
        if not found and not self.refreshed:
            self._refresh()
            found = self.matches(symbol)
        ciks = sorted({str(int(r["cik_str"])) for r in found if str(r.get("cik_str", "")).strip().isdigit()})
        if not ciks:
            return None, (f"The SEC's current company list has no company with the ticker {symbol}. It may be "
                          f"misspelled, delisted, or listed only outside the US; no company was guessed.")
        if len(ciks) > 1:
            return None, (f"The SEC's company list gives {symbol} to more than one company (CIK {', '.join(ciks)}); "
                          f"no company was guessed.")
        return ff.resolve_cik(symbol, tickers_doc=self.doc)


# ---------------------------------------------------------------- the record


def intake_root(runs_dir: Path) -> Path:
    return Path(runs_dir) / INTAKE_DIR


def intake_dir(runs_dir: Path, intake_id: str) -> Path:
    if not isinstance(intake_id, str) or not _ID.match(intake_id):
        raise IntakeError(404, "No intake has that id.")
    d = intake_root(runs_dir) / intake_id
    if not (d / INTAKE_FILE).is_file():
        raise IntakeError(404, "No intake has that id.")
    return d


def read(runs_dir: Path, intake_id: str) -> dict:
    d = intake_dir(runs_dir, intake_id)
    with _WRITE_LOCK:
        return json.loads((d / INTAKE_FILE).read_text())


def _counts(rec: dict) -> None:
    states = [t["state"] for t in rec["tickers"]]
    rec["ready_count"] = states.count("ready")
    rec["failed_count"] = sum(1 for s in states if s in FINISHED and s != "ready")
    rec["pages_per_document"] = PAGES_PER_DOCUMENT
    rec["pages_captured"] = sum(t.get("pages_captured", 0) for t in rec["tickers"])
    rec["pages_in_run"] = sum(t.get("pages_in_run", 0) for t in rec["tickers"])


def cap_pages(pages: list[dict], per_document: int = PAGES_PER_DOCUMENT) -> list[dict]:
    """The first per_document pages of each document (accession, exhibit) by page_number, in their captured order.
    A document with fewer pages is kept whole."""
    by_doc: dict[tuple, list[int]] = {}
    for p in pages:
        by_doc.setdefault((p["accession"], p["exhibit"]), []).append(p["page_number"])
    keep = {k: set(sorted(v)[:per_document]) for k, v in by_doc.items()}
    return [p for p in pages if p["page_number"] in keep[(p["accession"], p["exhibit"])]]


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _save(d: Path, rec: dict) -> None:
    with _WRITE_LOCK:
        _counts(rec)
        jobs._write(d / INTAKE_FILE, rec)


def _ticker(symbol: str, window_start: date, cutoff: date) -> dict:
    return {"symbol": symbol, "cik": None, "company": None, "state": "resolving", "reason": "Looking up the company.",
            "documents": 0, "pages": 0, "pages_per_document": PAGES_PER_DOCUMENT,
            "pages_captured": 0, "pages_in_run": 0, "form_scope": list(FORM_SCOPE), "lookback_days": LOOKBACK_DAYS,
            "window_start": window_start.isoformat(), "cutoff": cutoff.isoformat(), "failed_documents": []}


def start(runs_dir: Path, raw, prep: Preparer) -> dict:
    """Make runs/intake/<id>/intake.json (status preparing) and start its preparation. Returns the record."""
    symbols = normalise(raw)
    cutoff = prep.today()
    window_start = cutoff - timedelta(days=LOOKBACK_DAYS)
    root = intake_root(runs_dir)
    root.mkdir(parents=True, exist_ok=True)
    while True:
        intake_id = f"in-{secrets.token_hex(6)}"
        d = root / intake_id
        try:
            d.mkdir()
            break
        except FileExistsError:
            continue
    rec = {"intake_id": intake_id, "status": "preparing", "started_at": jobs.now_utc(), "finished_at": None,
           "requested": symbols, "tickers": [_ticker(s, window_start, cutoff) for s in symbols],
           "form_scope": list(FORM_SCOPE), "lookback_days": LOOKBACK_DAYS,
           "window_start": window_start.isoformat(), "cutoff": cutoff.isoformat(), "index_sha256": None}
    _save(d, rec)
    if prep.background:
        threading.Thread(target=prepare, args=(d, prep), daemon=True).start()
    else:
        prepare(d, prep)
    return read(runs_dir, intake_id)


def _set(d: Path, rec: dict, i: int, **fields) -> None:
    rec["tickers"][i].update(fields)
    _save(d, rec)


def _no_pages_reason(sub_path: Path, window_start: date, cutoff: date) -> tuple[str, str]:
    """(state, reason) for a known company with no captured page: unsupported when it files only 6-K reports (a
    foreign issuer) in the window, else no_documents saying what the window held."""
    try:
        sub = json.loads(sub_path.read_text())
    except (OSError, json.JSONDecodeError):
        sub = {}
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    in_window = [f for f, fd in zip(forms, dates)
                 if window_start.isoformat() <= str(fd) <= cutoff.isoformat()]
    span = f"between {window_start.isoformat()} and {cutoff.isoformat()}"
    eight_k = [f for f in in_window if ff.form_family(f) == "8-K"]
    if eight_k:
        return "no_documents", (f"{len(eight_k)} 8-K filing(s) {span}, none with an HTML press-release exhibit "
                                f"(EX-99) to read.")
    if any(str(f).upper().startswith("6-K") for f in in_window) or (
            not in_window and any(str(f).upper().startswith(("6-K", "20-F", "40-F")) for f in forms)):
        return "unsupported", ("This company files as a foreign issuer (6-K reports, not 8-K); this capture reads "
                               "8-K press-release exhibits only.")
    return "no_documents", f"No 8-K filed {span}."


def prepare(d: Path, prep: Preparer) -> None:
    """Resolve and capture every requested symbol, one at a time; never raises. Writes the ready pages' index and
    marks the intake done."""
    with PREP_LOCK:
        rec = json.loads((d / INTAKE_FILE).read_text())
        window_start, cutoff = date.fromisoformat(rec["window_start"]), date.fromisoformat(rec["cutoff"])
        filings_root = prep.filings_root / d.name / "filings"
        identity = _Identity(prep)
        pages_by_ticker: dict[str, list[dict]] = {}
        for i, t in enumerate(rec["tickers"]):
            symbol = t["symbol"]
            try:
                if not ff.TICKER_RE.match(symbol):
                    _set(d, rec, i, state="unsupported",
                         reason=f"{symbol} is not a ticker symbol the SEC lookup can take: use letters, digits, "
                                f"'.' or '-', 1 to 10 characters.")
                    continue
                try:
                    cik, company = identity.resolve(symbol)
                except ff.FetchError as e:
                    _set(d, rec, i, state="failed", reason=f"Could not reach the SEC's company list: {e}")
                    continue
                if cik is None:
                    _set(d, rec, i, state="unresolved", reason=company)
                    continue
                _set(d, rec, i, cik=cik, company=company, state="capturing",
                     reason=f"Capturing 8-K exhibits filed {rec['window_start']} to {rec['cutoff']}.")
                failures: list[str] = []
                try:
                    pages = ff.fetch_ticker(symbol, days=LOOKBACK_DAYS, fetcher=prep.fetcher(),
                                            filings_root=filings_root, today=cutoff, until=cutoff,
                                            forms=FORM_SCOPE, render=prep.render, tickers_doc=identity.doc,
                                            failures=failures)
                except ff.FetchError as e:
                    _set(d, rec, i, state="failed", reason=f"Could not read the SEC filing list: {e}")
                    continue
                sub_path = filings_root / symbol / "submissions.json"
                try:
                    company = json.loads(sub_path.read_text()).get("name") or company
                except (OSError, json.JSONDecodeError):
                    pass
                docs = len({(p["accession"], p["exhibit"]) for p in pages})
                if pages:
                    in_run = cap_pages(pages)
                    pages_by_ticker[symbol] = in_run
                    reason = (f"{_n(docs, 'document')}, {_n(len(pages), 'page')} captured; the run reads the first "
                              f"{PAGES_PER_DOCUMENT} pages of each ({_n(len(in_run), 'page')}).")
                    if failures:
                        reason += f" {len(failures)} more document(s) could not be captured."
                    _set(d, rec, i, company=company, state="ready", reason=reason, documents=docs,
                         pages=len(pages), pages_captured=len(pages), pages_in_run=len(in_run),
                         failed_documents=failures)
                elif failures:
                    _set(d, rec, i, company=company, state="failed", failed_documents=failures,
                         reason=f"Every document found ({len(failures)}) failed to capture; first: {failures[0]}")
                else:
                    state, reason = _no_pages_reason(sub_path, window_start, cutoff)
                    _set(d, rec, i, company=company, state=state, reason=reason)
            except Exception as e:  # the type only: nothing that could carry request details
                _set(d, rec, i, state="failed", reason=f"The preparation stopped on an unexpected error "
                                                       f"({type(e).__name__}).")
        if pages_by_ticker:
            doc = ff.build_index(pages_by_ticker)
            doc["note"] = (f"Written by server/intake.py for intake {d.name}: the ready tickers' pages only, the "
                           f"first {PAGES_PER_DOCUMENT} pages of each document; files under {filings_root}.")
            for body in doc["companies"].values():
                for p in body["pages"]:
                    tick = filings_root / p["ticker"]
                    p["page_file"] = str((tick / p["page_file"]).resolve())
                    p["words_file"] = str((tick / p["words_file"]).resolve())
            path = d / INDEX_FILE
            path.write_text(json.dumps(doc, indent=1) + "\n")
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            rec["index_sha256"] = jobs._file_sha(path)
        rec["status"] = "done"
        rec["finished_at"] = jobs.now_utc()
        _save(d, rec)


# ---------------------------------------------------------------- from intake to run


def view(rec: dict) -> dict:
    """What GET /api/intake/{id} returns."""
    keys = ("intake_id", "status", "started_at", "finished_at", "requested", "tickers", "ready_count", "failed_count",
            "form_scope", "lookback_days", "window_start", "cutoff", "run_id", "pages_per_document", "pages_captured",
            "pages_in_run")
    return {k: rec.get(k) for k in keys}


def ready_index(runs_dir: Path, intake_id) -> tuple[dict, sources.Index]:
    """The finished intake and its ready pages' index, or IntakeError (404 no intake, 409 still preparing or nothing
    ready, or its index changed since it was written)."""
    rec = read(runs_dir, intake_id)
    if rec.get("run_id"):
        raise IntakeError(409, USED_TEXT, run_id=rec["run_id"])
    if rec["status"] != "done":
        raise IntakeError(409, "This intake is still preparing; wait until every ticker is ready or has failed.")
    if not rec.get("ready_count"):
        raise IntakeError(409, "No ticker in this intake is ready, so there is nothing for a run to read. "
                               + _left_out_text(rec, set()))
    path = intake_dir(runs_dir, intake_id) / INDEX_FILE
    if jobs._file_sha(path) != rec.get("index_sha256"):
        raise IntakeError(409, "This intake's captured-pages file changed after it was prepared; start a new intake.")
    try:
        return rec, sources.load_index(path)
    except sources.SourcesError as e:
        raise IntakeError(409, str(e)) from None


def _left_out_text(rec: dict, chosen: set[str]) -> str:
    left = [t for t in rec["tickers"] if t["symbol"] not in chosen]
    return "Not in the run: " + "; ".join(f"{t['symbol']} ({t['state'].replace('_', ' ')}: {t['reason']})"
                                          for t in left) + "." if left else ""


def check_subset(rec: dict, companies: list[str], accept_ready_subset) -> list[str]:
    """The requested symbols the run leaves out. Refuses (IntakeError 422) when any is left out and the person did not
    send accept_ready_subset true: a requested name is never dropped silently."""
    if not isinstance(accept_ready_subset, bool):
        raise IntakeError(422, "accept_ready_subset must be true or false.")
    left = [s for s in rec["requested"] if s not in companies]
    if left and not accept_ready_subset:
        raise IntakeError(422, f"{len(left)} of {len(rec['requested'])} requested ticker(s) would not be in this run. "
                               f"{_left_out_text(rec, set(companies))} To run the ready ones only, send "
                               f"accept_ready_subset true.")
    return left


def bind_run(runs_dir: Path, intake_id: str, create_run: Callable[[], str]) -> str:
    """Create the intake's one run and record its id on the intake, atomically: under _RUN_LOCK the intake is read,
    refused (IntakeError 409, USED_TEXT, with the run id) when a run was already started from it, else create_run()
    makes the run and its id is written to the intake before the lock is released."""
    with _RUN_LOCK:
        d = intake_dir(runs_dir, intake_id)
        rec = read(runs_dir, intake_id)
        if rec.get("run_id"):
            raise IntakeError(409, USED_TEXT, run_id=rec["run_id"])
        run_id = create_run()
        with _WRITE_LOCK:
            rec = read(runs_dir, intake_id)
            rec["run_id"] = run_id
            _save(d, rec)
        return run_id


def snapshot_into_run(run_dir: Path, runs_dir: Path, rec: dict, companies: list[str], left_out: list[str]) -> sources.Index:
    """Copy the intake's ready-pages index into the run's own folder (read-only) with the intake record beside it, and
    return the copy as the run's index. The job freezes this copy; nothing else is read."""
    src = intake_dir(runs_dir, rec["intake_id"]) / INDEX_FILE
    dst = Path(run_dir) / INDEX_FILE
    shutil.copyfile(src, dst)
    os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    jobs._write(Path(run_dir) / "intake.json", {
        "intake_id": rec["intake_id"], "requested": rec["requested"], "companies": companies, "left_out": left_out,
        "tickers": rec["tickers"], "form_scope": rec["form_scope"], "lookback_days": rec["lookback_days"],
        "pages_per_document": rec.get("pages_per_document"),
        "window_start": rec["window_start"], "cutoff": rec["cutoff"], "index_sha256": jobs._file_sha(dst)})
    return sources.load_index(dst)
