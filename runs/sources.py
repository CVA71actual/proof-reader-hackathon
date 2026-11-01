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
~/proof-reader-docs/filings/<TICKER>. Optional local_path is the filed HTML relative to ~/proof-reader-docs/filings
(never an absolute home path); the loader resolves it to local_file.

capture_page(record, run_dir) is "capture on first use": the page file and the words file are checked against the hashes
in the record (a changed byte refuses the page, with the reason), the page is rendered to an image in the run's folder,
the words are copied beside it, and the source is described with their hashes. Nothing else of the filing is read.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from checks.selector_match import MIN_WORD_HEIGHT_PT
from capture.capture_source import COMPLETENESS_KEYS, DOCS, FIXTURES, load_entry
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
    # local_path in the index is relative to ~/proof-reader-docs/filings (never /Users/…);
    # resolve it the same way as page_file for callers that need the HTML on disk.
    if r.get("local_path"):
        r["local_file"] = str(_resolve(str(r["local_path"]), r["ticker"], base))
    return r


def _group(doc) -> list[dict]:
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        raise SourcesError("The captured-pages file is not a list of pages or an object holding them.")
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
    raise SourcesError("The captured-pages file holds no companies and no pages.")


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
            raise SourcesError(f"Page {i + 1} of the captured-pages file is not an object.")
        rec = _normalize(raw, base, f"Page {i + 1} of the captured-pages file ({raw.get('ticker', '?')})")
        for what in ("id", "source_id"):
            if rec[what] in ids:
                raise SourcesError(f"Page {i + 1} of the captured-pages file ({rec['ticker']}): the {what} {rec[what]} is used twice.")
            ids.add(rec[what])
        stem = rec["file_stem"].lower()
        if stem in stems:
            raise SourcesError(f"Page {i + 1} of the captured-pages file ({rec['ticker']}): the file stem {rec['file_stem']} is already "
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
            raise SourcesError(f"There is no captured-pages file at {p}.")
        return builtin_index()
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError:
        raise SourcesError(f"The captured-pages file {p.name} is not valid JSON.") from None
    return parse_index(doc, p.parent, p)


# ---------------------------------------------------------------- capture on first use


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_page_artifact_bytes(run_dir: Path, name: str, expected_sha256: str | None) -> bytes:
    """Read, contain and hash one saved page artifact in a single byte snapshot."""
    root = Path(run_dir).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise SourcesError(f"This run is not served: its file {name} is outside the run's folder.")
    if not expected_sha256:
        raise SourcesError(f"This run is not served: its file {name} has no hash recorded in run.json.")
    if not path.is_file():
        raise SourcesError(f"This run is not served: its file {name} is missing.")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise SourcesError(f"This run is not served: its file {name} does not match the hash recorded in run.json.")
    return raw


def load_page_image_evidence(run_dir: Path, page: dict) -> tuple[bytes, dict]:
    if not page.get("image"):
        raise SourcesError("The page has no declared image artifact.")
    raw = load_page_artifact_bytes(run_dir, page["image"], page.get("image_sha256"))
    root = Path(run_dir).resolve()
    path = (root / page["image"]).resolve()
    return raw, {"file": str(path.relative_to(root)), "sha256": hashlib.sha256(raw).hexdigest(), "kind": "image"}


def capture_page(record: dict, run_dir: Path) -> tuple[dict, dict, dict]:
    """Compatibility name for the verified boundary: return source, parsed words and consumed identity."""
    return capture_page_evidence(record, run_dir)


def pinned_source_snapshot(record: dict) -> tuple[bytes, bytes, dict, dict]:
    """Return the pinned page bytes, parsed words and identity from the same verified source snapshot."""
    page_pdf, words_src = Path(record["page_path"]), Path(record["words_path"])
    snapshots = {}
    for what, f, want in (("page", page_pdf, record["page_sha256"]), ("words", words_src, record["words_sha256"])):
        if not f.is_file():
            raise SourcesError(f"The {what} file for {record['ticker']} page {record['page_number']} is missing "
                               f"({f.name}); the captured-pages file names it.")
        raw = f.read_bytes()
        if hashlib.sha256(raw).hexdigest() != want:
            raise SourcesError(f"The {what} file for {record['ticker']} page {record['page_number']} ({f.name}) does not "
                               f"match the hash the captured-pages file recorded; refusing to read it.")
        snapshots[what] = raw
    try:
        words_doc = json.loads(snapshots["words"])
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourcesError(f"The words file for {record['ticker']} page {record['page_number']} is not valid JSON; "
                           "refusing to read it.") from None
    identity = {"file": record["words_file"], "sha256": hashlib.sha256(snapshots["words"]).hexdigest(),
                "kind": "captured"}
    return snapshots["page"], snapshots["words"], words_doc, identity


def pinned_words(ticker: str) -> dict:
    """Read the built-in source words through the same manifest-hash boundary as run capture."""
    record = builtin_index().pages(ticker)[0]["record"]
    return pinned_source_snapshot(record)[2]


def capture_page_evidence(record: dict, run_dir: Path) -> tuple[dict, dict, dict]:
    """Capture and return the exact consumed words identity with the parsed snapshot."""
    pdf_bytes, words_bytes, words_doc, source_identity = pinned_source_snapshot(record)
    stem = record["file_stem"]
    image_rel, words_rel = f"pages/{stem}.png", f"pages/{stem}.words.json"
    (run_dir / "pages").mkdir(parents=True, exist_ok=True)
    build_run.render_page_bytes(pdf_bytes, run_dir / image_rel)
    (run_dir / words_rel).write_bytes(words_bytes)
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
    completeness = {key: record[key] for key in COMPLETENESS_KEYS if key in record}
    if completeness:
        source["pages"][0]["capture_completeness"] = completeness
    source["pages"][0]["visual_evidence"] = visual_evidence(source["pages"][0], words_doc)
    if record.get("source_format") == "html" and record.get("source_sha256"):
        source["rendered_from"] = {"format": "html", "sha256": record["source_sha256"],
                                   "renderer": record.get("renderer") or "unknown renderer"}
    return source, words_doc, {**source_identity, "file": words_rel}


def load_page_words(run_dir: Path, page: dict) -> tuple[dict, dict]:
    """Compatibility name for the verified boundary: return parsed words and consumed identity."""
    return load_page_evidence(run_dir, page)


def load_page_evidence(run_dir: Path, page: dict) -> tuple[dict, dict]:
    """Return parsed words and the identity of those exact bytes; verify captured and derived artifacts here."""
    if page.get("image"):
        load_page_image_evidence(run_dir, page)
    derived = page.get("derived_words")
    if not page.get("words"):
        raise SourcesError("The page has no declared words artifact.")
    if not page.get("words_sha256"):
        raise SourcesError(f"This run is not served: its file {page['words']} has no hash recorded in run.json.")
    root = Path(run_dir).resolve()
    captured_raw = load_page_artifact_bytes(run_dir, page["words"], page["words_sha256"])
    try:
        json.loads(captured_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourcesError(f"The page's declared words artifact {page['words']} is not valid JSON.") from None
    path_key, hash_key = ((derived.get("file"), derived.get("sha256")) if derived else
                          (page["words"], page["words_sha256"]))
    if derived and (not path_key or not hash_key):
        raise SourcesError("The page's derived words artifact has no file or hash recorded.")
    raw = captured_raw if not derived else load_page_artifact_bytes(run_dir, path_key, hash_key)
    path = (root / path_key).resolve()
    digest = hashlib.sha256(raw).hexdigest()
    if derived and derived.get("based_on_image_sha256") != page.get("image_sha256"):
        raise SourcesError("The derived transcription is not bound to this captured page image.")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourcesError("The page's declared words artifact is not valid JSON.") from None
    page["visual_evidence"] = visual_evidence(page, parsed)
    if page.get("scanned"):
        # A scanned-page run consumes the rendered image only. Keep the
        # verified captured words file intact, but never feed its hidden text
        # layer into checks or use it as a citation source.
        return {"words": [], "page_width": page["width_pt"], "page_height": page["height_pt"]}, \
            {"file": str((root / page["image"]).resolve().relative_to(root)),
             "sha256": page["image_sha256"], "kind": "image"}
    return parsed, {"file": str(path.relative_to(root)), "sha256": digest,
                    "kind": "derived" if derived else "captured"}


def visual_evidence(page: dict, words_doc: dict) -> dict:
    """Classify whether stored geometry can support ordinary visual citation overlays."""
    # The words artifact is authoritative too: picture transcriptions can have
    # native-height boxes, but those positions were spread across SAM regions.
    from runs import picture
    picture_artifact = (words_doc.get("kind") == picture.KIND or
                        words_doc.get("words_from") == picture.WORDS_FROM)
    if (picture_artifact or page.get("derived_words") or page.get("scanned") or
            (page.get("regions") and not words_doc.get("words"))):
        return {"origin": "approximate", "overlays_supported": False,
                "reason": "The citation geometry comes from image transcription or SAM regions, not visible text words."}
    words = [w for w in words_doc.get("words", []) if not w.get("break")]
    if words and all(float(w.get("y1", 0)) - float(w.get("y0", 0)) < MIN_WORD_HEIGHT_PT for w in words):
        return {"origin": "hidden_text", "overlays_supported": False,
                "reason": "The page's text layer has only nonvisual sliver geometry."}
    if not words:
        return {"origin": "no_text_layer", "overlays_supported": False,
                "reason": "The page has no visible text-word geometry."}
    return {"origin": "text_layer", "overlays_supported": True, "reason": None}
