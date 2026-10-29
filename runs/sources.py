"""The sources index: which captured pages exist for which company, and capturing one into a run.

A run reads "every indexed page" of each company it is asked for. The index is `runs/sources_index.json` (written by
capture/fetch_filings.py: recent 8-K exhibits for the twelve tickers, each rendered to a PDF page exactly like the PTGX
pin, hashed, and stored under ~/proof-reader-docs/filings/<TICKER>/). Until that file exists the built-in index is used:
the two pages that were hard-coded before (the PTGX 8-K exhibit page 3, the IONS annual report page 95) plus the scanned
PTGX page, so the server behaves as it did.

Index file, one of:
    {"index_version": 1, "companies": {"PTGX": {"pages": [<page>, ...]}, ...}}
    {"pages": [<page>, ...]}                  a flat list; the ticker is in each page
    [<page>, ...]
A page has the fields of an entry in fixtures/MANIFEST.json (the PTGX pin): ticker, company, form, exhibit,
exhibit_title, filing_date, accession, source_url, source_format, source_sha256, rendered_pdf_sha256, renderer,
page_number, page_file, page_sha256, words_file, words_sha256, captured_at. Plus, all optional:
    id            unique name of the page (default: ticker, accession or date, exhibit or form, page number)
    kind          "events" (default: a release, a deck, an 8-K exhibit: read for upcoming events) or "table"
                  (a financial table: read by the table reader)
    source_id     the id of the source in run.json (default: "src-" + id)
    file_stem     the stem of the files written into the run's pages/ folder (default: the id). Two pages may not
                  share a stem (compared without regard to case): the index is refused, because the second capture
                  would overwrite the first and break its hash.
page_file and words_file may be absolute, or relative to the index file's folder, to ~/proof-reader-docs/filings, or to
~/proof-reader-docs/filings/<TICKER>.

capture_page(record, run_dir) is "capture on first use": the page file and the words file are checked against the hashes
in the record (a changed byte refuses the page, with the reason), the page is rendered to an image in the run's folder,
the words are copied beside it, and the source is described with their hashes. Nothing else of the filing is read.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from capture.capture_source import DOCS, FIXTURES, load_entry
from runs import build_entry, build_run

INDEX_PATH = build_run.RUNS / "sources_index.json"
FILINGS = DOCS / "filings"
KINDS = ("events", "table", "scan_events")
REQUIRED = ("ticker", "company", "filing_date", "page_number", "page_file", "words_file", "page_sha256", "words_sha256")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SourcesError(ValueError):
    """The index (or a page it names) cannot be used; the message says which entry and why, in plain words."""


@dataclass
class Index:
    by_ticker: dict[str, list[dict]] = field(default_factory=dict)
    path: Path | None = None
    empty: list[str] = field(default_factory=list)      # companies the index lists but holds no page for (ABVX)

    @property
    def tickers(self) -> list[str]:
        return list(self.by_ticker)

    def pages(self, ticker: str) -> list[dict]:
        """The page descriptors of a company, in index order: {"kind", "source_id", "record"}."""
        return [{"kind": r["kind"], "source_id": r["source_id"], "record": r} for r in self.by_ticker.get(ticker, [])]


# ---------------------------------------------------------------- reading the index


def _resolve(name: str, ticker: str, base: Path | None) -> Path:
    p = Path(name)
    if p.is_absolute():
        return p
    for root in (base, FILINGS / ticker, FILINGS, FIXTURES):
        if root is not None and (root / p).is_file():
            return root / p
    return (base or FIXTURES) / p


def _normalize(raw: dict, base: Path | None, where: str) -> dict:
    missing = [k for k in REQUIRED if raw.get(k) in (None, "")]
    if missing:
        raise SourcesError(f"{where}: the page needs {', '.join(missing)}.")
    r = dict(raw)
    r["ticker"] = str(r["ticker"]).strip().upper()
    if not isinstance(r["page_number"], int) or r["page_number"] < 1:
        raise SourcesError(f"{where}: the page number must be a whole number from 1.")
    r["kind"] = r.get("kind") or "events"
    if r["kind"] not in KINDS:
        raise SourcesError(f"{where}: the kind must be one of {', '.join(KINDS)}, not {r['kind']}.")
    stem = r.get("id") or "-".join(str(x) for x in (r["ticker"].lower(), r.get("accession") or r["filing_date"],
                                                  r.get("exhibit") or r.get("form") or "doc",
                                                  f"p{r['page_number']}"))
    r["id"] = _SAFE.sub("-", stem).strip("-")
    r["source_id"] = r.get("source_id") or f"src-{r['id']}"
    r["file_stem"] = _SAFE.sub("-", r.get("file_stem") or r["id"]).strip("-")
    r["page_path"] = str(_resolve(r["page_file"], r["ticker"], base))
    r["words_path"] = str(_resolve(r["words_file"], r["ticker"], base))
    return r


def _group(doc) -> list[dict]:
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        raise SourcesError("The sources index is not a list of pages or an object holding them.")
    if isinstance(doc.get("companies"), dict):
        out = []
        for ticker, body in doc["companies"].items():
            pages = body.get("pages") if isinstance(body, dict) else body
            for p in pages or []:
                out.append({"ticker": ticker, **{k: v for k, v in p.items() if k != "ticker"}} if isinstance(p, dict)
                           else p)
        return out
    if isinstance(doc.get("pages"), list):
        return doc["pages"]
    raise SourcesError("The sources index holds no companies and no pages.")


def parse_index(doc, base: Path | None = None, path: Path | None = None) -> Index:
    index = Index(path=path)
    if isinstance(doc, dict) and isinstance(doc.get("companies"), dict):
        for t, body in doc["companies"].items():
            if not ((body.get("pages") if isinstance(body, dict) else body) or []):
                index.empty.append(str(t).strip().upper())
    ids: set[str] = set()
    stems: dict[str, str] = {}       # lower case: a case-insensitive file system would fold two stems into one file
    for i, raw in enumerate(_group(doc)):
        if not isinstance(raw, dict):
            raise SourcesError(f"Page {i + 1} of the index is not an object.")
        rec = _normalize(raw, base, f"Page {i + 1} of the index ({raw.get('ticker', '?')})")
        for what in ("id", "source_id"):
            if rec[what] in ids:
                raise SourcesError(f"Page {i + 1} of the index ({rec['ticker']}): the {what} {rec[what]} is used twice.")
            ids.add(rec[what])
        stem = rec["file_stem"].lower()
        if stem in stems:
            raise SourcesError(f"Page {i + 1} of the index ({rec['ticker']}): the file stem {rec['file_stem']} is already "
                               f"used by {stems[stem]}, so their captured files would overwrite each other.")
        stems[stem] = f"page {rec['id']}"
        index.by_ticker.setdefault(rec["ticker"], []).append(rec)
    return index


def builtin_index() -> Index:
    """The pages that were hard-coded: PTGX 8-K exhibit page 3 (events), IONS annual report page 95 (a table), and the
    PTGX page again as a scan (no text layer, regions from SAM 3.1)."""
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text())
    ptgx = load_entry(build_entry.PIN)
    ions = {k: v for k, v in manifest.items() if k != "entries"}
    index = Index()
    for ticker, raw in (
        ("PTGX", {**ptgx, "id": "ptgx-p3", "kind": "events", "source_id": build_entry.SOURCE_ID, "file_stem": "p3"}),
        ("IONS", {**ions, "id": "ions-p95", "kind": "table", "source_id": build_run.IONS_SOURCE_ID,
                  "file_stem": "p95"}),
        ("PTGX-SCAN", {**ptgx, "ticker": "PTGX-SCAN", "id": "ptgx-p3-scanned", "kind": "scan_events",
                       "source_id": "src-ptgx-8k-2026-08-05-ex991-scanned", "file_stem": "p3-scanned"}),
    ):
        index.by_ticker[ticker] = [_normalize(raw, FIXTURES, f"The built-in page for {ticker}")]
    return index


def load_index(path: Path | None = None) -> Index:
    """runs/sources_index.json if it exists (or `path`), else the built-in index."""
    p = Path(path) if path is not None else INDEX_PATH
    if not p.is_file():
        if path is not None:
            raise SourcesError(f"There is no sources index at {p}.")
        return builtin_index()
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError:
        raise SourcesError(f"The sources index {p.name} is not valid JSON.") from None
    return parse_index(doc, p.parent, p)


# ---------------------------------------------------------------- capture on first use


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_page(record: dict, run_dir: Path) -> tuple[dict, dict]:
    """Capture one indexed page into a run's folder and describe it: returns (source, words_doc). Refuses, with a plain
    reason, a page or words file that is missing or no longer matches the hash the index recorded."""
    page_pdf, words_src = Path(record["page_path"]), Path(record["words_path"])
    for what, f, want in (("page", page_pdf, record["page_sha256"]), ("words", words_src, record["words_sha256"])):
        if not f.is_file():
            raise SourcesError(f"The {what} file for {record['ticker']} page {record['page_number']} is missing "
                               f"({f.name}); the index names it.")
        if _sha(f) != want:
            raise SourcesError(f"The {what} file for {record['ticker']} page {record['page_number']} ({f.name}) does not "
                               f"match the hash the index recorded; refusing to read it.")
    stem = record["file_stem"]
    image_rel, words_rel = f"pages/{stem}.png", f"pages/{stem}.words.json"
    (run_dir / "pages").mkdir(parents=True, exist_ok=True)
    build_run.render_page(page_pdf, run_dir / image_rel)
    shutil.copyfile(words_src, run_dir / words_rel)
    words_doc = json.loads(words_src.read_text())
    if record.get("exhibit"):
        title = (f"{record['ticker']} {record.get('form') or '8-K'} exhibit {record['exhibit']} "
                 f"({str(record.get('exhibit_title') or 'exhibit').lower()}), filed {record['filing_date']}")
    else:
        title = (f"{record['ticker']} annual report to shareholders ({record.get('form') or 'report'}), filed "
                 f"{record['filing_date']}")
    source = {
        "id": record["source_id"],
        "publisher": f"{record['company']} (filed with the SEC)",
        "kind": "filing",
        "title": title,
        "url": record.get("source_url"),
        "published_date": record["filing_date"],
        "captured_at": record.get("captured_at") or record["filing_date"] + "T00:00:00+00:00",
        "sha256": record.get("rendered_pdf_sha256") or record.get("source_sha256") or record["page_sha256"],
        "pages": [{
            "page_number": record["page_number"], "image": image_rel,
            "width_pt": words_doc["page_width"], "height_pt": words_doc["page_height"], "words": words_rel,
            "image_sha256": build_run.file_sha256(run_dir / image_rel),
            "words_sha256": build_run.file_sha256(run_dir / words_rel),
        }],
    }
    if record.get("source_format") == "html" and record.get("source_sha256"):
        source["rendered_from"] = {"format": "html", "sha256": record["source_sha256"],
                                   "renderer": record.get("renderer") or "unknown renderer"}
    return source, words_doc
