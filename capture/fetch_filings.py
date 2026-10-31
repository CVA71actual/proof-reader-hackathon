"""Fetch recent 8-K EX-99.* exhibits for the twelve demo tickers, render each to PDF, index the pages.

Usage (from the repo root):
    python -m capture.fetch_filings --tickers PTGX --days 90
    python -m capture.fetch_filings --tickers all --days 90

For each ticker: list 8-K filings from the EDGAR submissions JSON in the last N days, fetch each filing's
EX-99.* HTML exhibits (User-Agent from SEC_USER_AGENT), render each to a Letter PDF with headless Chrome
exactly as the PTGX pin (--headless --disable-gpu --no-pdf-header-footer --print-to-pdf), hash the HTML
and the PDF, cut every page and its words, and store under ~/proof-reader-docs/filings/<TICKER>/<accession>/
(never in the repo). Writes runs/sources_index.twelve.json for Job 9 (MANIFEST-shaped page entries, kind=events).

At most 10 requests per second. No model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import pymupdf

from capture.capture_page import extract_page, page_words, sha256_file
from capture.capture_source import DOCS

REPO = Path(__file__).resolve().parent.parent
FILINGS = DOCS / "filings"
INDEX_PATH = REPO / "runs" / "sources_index.twelve.json"
ENV_PATHS = (
    Path.home() / ".config" / "proof-reader" / "env",
    REPO / "env",
    REPO / ".env",
)

TICKERS = (
    "IONS", "PTGX", "VKTX", "RVMD", "ZLAB", "CLDX",
    "RYTM", "ENTA", "MNPR", "TSHA", "ABVX", "ROIV",
)

# Public CIK map for the twelve (from SEC company_tickers.json). Used so offline tests need no network.
CIKS: dict[str, tuple[str, str]] = {
    "IONS": ("874015", "Ionis Pharmaceuticals, Inc."),
    "PTGX": ("1377121", "Protagonist Therapeutics, Inc."),
    "VKTX": ("1607678", "Viking Therapeutics, Inc."),
    "RVMD": ("1628171", "Revolution Medicines, Inc."),
    "ZLAB": ("1704292", "Zai Lab Ltd."),
    "CLDX": ("744218", "Celldex Therapeutics, Inc."),
    "RYTM": ("1649904", "Rhythm Pharmaceuticals, Inc."),
    "ENTA": ("1177648", "Enanta Pharmaceuticals, Inc."),
    "MNPR": ("1645469", "Monopar Therapeutics Inc."),
    "TSHA": ("1806310", "Taysha Gene Therapies, Inc."),
    "ABVX": ("1956827", "Abivax S.A."),
    "ROIV": ("1635088", "Roivant Sciences Ltd."),
}

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_TICKERS_CACHE = DOCS / "company_tickers.json"
TICKER_RE = re.compile(r"^[A-Z0-9.-]{1,10}$")
# Forms the wider capture can take (--forms). Default stays 8-K only (the twelve pipeline).
FORM_FAMILIES = ("8-K", "10-K", "10-Q", "S-1", "424B")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
EX99_HTML = re.compile(r"(?i)(?:^|[^a-z0-9])ex[-_]?99(?:[-._]\d+)+\.html?$")
EXHIBIT_NUM = re.compile(r"(?i)ex[-_]?99[-._]?(\d+(?:\.\d+)*)")
# SEC index-headers.html escapes tags as &lt;TYPE&gt;…; the raw .txt uses <TYPE>.
EX99_TYPE_FILE = re.compile(
    r"(?is)(?:<TYPE>|&lt;TYPE&gt;)(EX-99(?:\.\d+)*)\s+"
    r"(?:<SEQUENCE>|&lt;SEQUENCE&gt;)\d+\s+"
    r"(?:<FILENAME>|&lt;FILENAME&gt;)([^\s<&]+)"
    r"(?:\s+(?:<DESCRIPTION>|&lt;DESCRIPTION&gt;)([^\n<&]*))?"
)
IMG_REF = re.compile(
    r"""(?is)(?:src|href)\s*=\s*["']([^"']+\.(?:png|jpe?g|gif|svg|webp|css))["']"""
)
RATE_INTERVAL = 0.1  # 10 requests / second
CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
)


class FetchError(RuntimeError):
    """A filing could not be listed, fetched, or rendered; the message says which and why."""


# ---------------------------------------------------------------- env / HTTP


def load_user_agent(env_paths: tuple[Path, ...] = ENV_PATHS) -> str:
    """SEC_USER_AGENT from the process env, else from ~/.config/proof-reader/env (or a local env file)."""
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    if ua:
        return ua
    for path in env_paths:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "SEC_USER_AGENT":
                ua = val.strip().strip('"').strip("'")
                if ua:
                    return ua
    raise FetchError(
        "SEC_USER_AGENT is not set. Put it in the environment or in ~/.config/proof-reader/env."
    )


class RateLimitedFetcher:
    """GET bytes from a URL with the SEC User-Agent, at most 10 requests per second."""

    def __init__(self, user_agent: str, interval: float = RATE_INTERVAL):
        self.user_agent = user_agent
        self.interval = interval
        self._last = 0.0

    def get(self, url: str) -> bytes:
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                    import gzip
                    try:
                        data = gzip.decompress(data)
                    except OSError:
                        pass
        except urllib.error.HTTPError as e:
            raise FetchError(f"GET {url} failed: HTTP {e.code}.") from e
        except urllib.error.URLError as e:
            raise FetchError(f"GET {url} failed: {e.reason}.") from e
        finally:
            self._last = time.monotonic()
        return data

    def get_json(self, url: str):
        return json.loads(self.get(url).decode("utf-8"))


# ---------------------------------------------------------------- listing exhibits


def form_family(form: str) -> str | None:
    """'10-K/A' -> '10-K'; '424B5' -> '424B'; 'S-1/A' -> 'S-1'; anything else None."""
    base = form.split("/")[0].strip().upper()
    if base.startswith("424B"):
        return "424B"
    return base if base in FORM_FAMILIES else None


def select_filings(
    submissions: dict,
    days: int,
    today: date | None = None,
    until: date | None = None,
    forms: tuple[str, ...] = ("8-K",),
) -> list[dict]:
    """Filings of the wanted form families filed in [reference - days, until]; reference is `until` if given, else today."""
    today = today or date.today()
    ref = until or today
    cutoff = ref - timedelta(days=days)
    recent = submissions.get("filings", {}).get("recent") or {}
    out = []
    for i, form in enumerate(recent.get("form") or []):
        if form_family(form) not in forms:
            continue
        filing_date = recent["filingDate"][i]
        fd = date.fromisoformat(filing_date)
        if fd < cutoff or (until is not None and fd > until):
            continue
        out.append({
            "form": form,
            "filing_date": filing_date,
            "accession": recent["accessionNumber"][i],
            "primary_document": recent.get("primaryDocument", [""])[i] if recent.get("primaryDocument") else "",
        })
    return out


def recent_8k_filings(submissions: dict, days: int, today: date | None = None, until: date | None = None) -> list[dict]:
    """8-K / 8-K/A filings in the submissions JSON whose filing date is within the last `days` days."""
    return select_filings(submissions, days, today=today, until=until, forms=("8-K",))


def resolve_cik(
    ticker: str,
    fetcher: "RateLimitedFetcher | None" = None,
    cache_path: Path = COMPANY_TICKERS_CACHE,
    tickers_doc: dict | None = None,
) -> tuple[str, str]:
    """(cik, company) for a ticker: the built-in map first, else SEC company_tickers.json (cached locally, no key)."""
    ticker = ticker.upper()
    if ticker in CIKS:
        return CIKS[ticker]
    if tickers_doc is None:
        if cache_path.is_file():
            tickers_doc = json.loads(cache_path.read_text())
        elif fetcher is not None:
            tickers_doc = fetcher.get_json(COMPANY_TICKERS_URL)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(tickers_doc))
        else:
            raise FetchError(f"Unknown ticker {ticker}: not in the built-in map and no company_tickers.json available.")
    rows = tickers_doc.values() if isinstance(tickers_doc, dict) else tickers_doc
    for row in rows:
        if str(row.get("ticker", "")).upper() == ticker:
            return str(int(row["cik_str"])), row.get("title") or ticker
    raise FetchError(f"Unknown ticker {ticker}: not in the built-in map or SEC company_tickers.json.")


def is_ex99_html(name: str) -> bool:
    """True for an EX-99.* HTML exhibit file name (not an image or other sidecar)."""
    base = name.rsplit("/", 1)[-1]
    if not EX99_HTML.search(base):
        return False
    stem = base.rsplit(".", 1)[0].lower()
    if "img" in stem or stem.endswith(("-css", "_css")):
        return False
    return True


def exhibit_number(name_or_type: str) -> str:
    """'99.1' from EX-99.1, tm2622272d1_ex99-1.htm, ex-99.1.htm, or EX99_2.html."""
    raw = name_or_type.rsplit("/", 1)[-1].strip()
    # TYPE strings look like EX-99.1 (no file extension); filenames go through EXHIBIT_NUM.
    if re.fullmatch(r"(?i)EX-99(?:\.\d+)*", raw):
        rest = raw.split("-", 1)[-1]  # '99.1' or '99'
        return rest
    m = EXHIBIT_NUM.search(raw)
    if not m:
        return "99"
    num = m.group(1).replace("-", ".").replace("_", ".")
    return f"99.{num}" if not num.startswith("99") else num


def list_ex99_html(index_doc: dict) -> list[str]:
    """HTML EX-99.* file names guessed from a filing's EDGAR index.json (filename heuristic)."""
    items = (index_doc.get("directory") or {}).get("item") or []
    names = []
    for it in items:
        name = it.get("name") if isinstance(it, dict) else None
        if name and is_ex99_html(name):
            names.append(name)
    return names


def ex99_html_from_headers(headers_text: str) -> list[dict]:
    """EX-99.* HTML exhibits from an accession *-index-headers.html (or .txt) body.

    Filenames often omit 'ex99' (e.g. corporatedeck_vfinal4.htm); the TYPE line is authoritative.
    Returns [{"type": "EX-99.1", "exhibit": "99.1", "filename": "...", "title": "..."}, ...].
    """
    out = []
    seen = set()
    for m in EX99_TYPE_FILE.finditer(headers_text):
        typ, filename, title = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        low = filename.lower()
        if not low.endswith((".htm", ".html")):
            continue
        if filename in seen:
            continue
        seen.add(filename)
        out.append({
            "type": typ.upper(),
            "exhibit": exhibit_number(typ),
            "filename": filename,
            "title": title or None,
        })
    return out


# ---------------------------------------------------------------- Chrome render


def find_chrome(explicit: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise FetchError(f"Chrome binary not found at {explicit}.")
        return explicit
    env = (os.environ.get("CHROME_BIN") or "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
    for c in CHROME_CANDIDATES:
        if c.is_file():
            return c
    raise FetchError(
        "Google Chrome was not found. Set CHROME_BIN or install Chrome "
        "(the PTGX pin uses Google Chrome headless print-to-pdf)."
    )


def chrome_version(chrome: Path) -> str:
    try:
        out = subprocess.check_output([str(chrome), "--version"], text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as e:
        raise FetchError(f"Could not read Chrome version from {chrome}: {e}.") from e
    return out.strip() or "Google Chrome"


def renderer_label(chrome: Path) -> str:
    # Match the PTGX pin wording: "Google Chrome 156.0.8078.48 headless print-to-pdf"
    ver = chrome_version(chrome)
    # "Google Chrome 156.0.8078.48" or "Chromium 120.0.0.0"
    return f"{ver} headless print-to-pdf"


def render_html_to_pdf(html_path: Path, pdf_path: Path, chrome: Path | None = None) -> str:
    """Render a local HTML file to a Letter PDF with headless Chrome (PTGX pin flags). Returns the renderer label."""
    chrome = find_chrome(chrome)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()
    url = html_path.resolve().as_uri()
    cmd = [
        str(chrome),
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        url,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        raise FetchError(f"Chrome failed to render {html_path.name}: {err or e}.") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"Chrome timed out rendering {html_path.name}.") from e
    if not pdf_path.is_file() or pdf_path.stat().st_size < 64:
        raise FetchError(f"Chrome wrote no PDF for {html_path.name}.")
    return renderer_label(chrome)


# ---------------------------------------------------------------- capture one exhibit


def _sidecar_names(html_name: str, index_names: list[str]) -> list[str]:
    """Other files in the filing that share the exhibit stem (images, css) so file:// rendering works."""
    stem = html_name.rsplit(".", 1)[0].lower()
    out = []
    for n in index_names:
        low = n.lower()
        if low == html_name.lower():
            continue
        if low.startswith(stem) or stem in low.rsplit(".", 1)[0]:
            # e.g. tm2622272d1_ex99-1img01.jpg
            if low.startswith(stem) or re.match(re.escape(stem) + r"img", low):
                out.append(n)
    return out


def _local_refs(html_bytes: bytes) -> list[str]:
    text = html_bytes.decode("utf-8", errors="replace")
    refs = []
    for m in IMG_REF.finditer(text):
        ref = m.group(1).strip()
        if ref.startswith(("http://", "https://", "data:", "#")):
            continue
        refs.append(ref.split("?", 1)[0])
    return refs


def save_exhibit_assets(
    fetcher: RateLimitedFetcher,
    base_url: str,
    html_name: str,
    html_bytes: bytes,
    dest_dir: Path,
    index_names: list[str],
) -> None:
    """Write the HTML and any local image/css sidecars next to it under dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / html_name).write_bytes(html_bytes)
    wanted = set(_sidecar_names(html_name, index_names))
    wanted.update(Path(r).name for r in _local_refs(html_bytes))
    for name in sorted(wanted):
        if name == html_name or "/" in name or "\\" in name:
            # only same-directory relative names
            if "/" in name or "\\" in name:
                name = Path(name).name
        target = dest_dir / name
        if target.exists():
            continue
        try:
            target.write_bytes(fetcher.get(urljoin(base_url, name)))
        except FetchError:
            # Missing sidecar: still render; content pages usually survive without logos.
            continue


def cut_pages(full_pdf: Path, dest_dir: Path, stem: str) -> list[dict]:
    """Cut each page of the rendered PDF into a single-page PDF + words.json. Returns page records (no hashes yet)."""
    doc = pymupdf.open(full_pdf)
    count = doc.page_count
    doc.close()
    pages = []
    for i in range(count):
        page_no = i + 1
        page_file = f"{stem}_p{page_no}.pdf"
        words_file = f"{stem}_p{page_no}.words.json"
        page_pdf = dest_dir / page_file
        extract_page(full_pdf, i, page_pdf)
        words = page_words(page_pdf, page_no)
        words_path = dest_dir / words_file
        words_path.write_text(json.dumps(words, indent=1) + "\n")
        pages.append({
            "page_number": page_no,
            "page_file": page_file,
            "words_file": words_file,
            "page_sha256": sha256_file(page_pdf),
            "words_sha256": sha256_file(words_path),
            "word_count": len(words["words"]),
        })
    return pages


def capture_exhibit(
    *,
    ticker: str,
    company: str,
    cik: str,
    form: str,
    filing_date: str,
    accession: str,
    html_name: str,
    html_bytes: bytes,
    dest_dir: Path,
    base_url: str,
    index_names: list[str],
    fetcher: RateLimitedFetcher | None,
    chrome: Path | None,
    render: Callable[[Path, Path, Path | None], str] = render_html_to_pdf,
    exhibit: str | None = None,
    exhibit_title: str | None = None,
    filings_root: Path = FILINGS,
) -> list[dict]:
    """Store one exhibit under dest_dir, render to PDF, cut pages; return MANIFEST-shaped page entries."""
    if fetcher is not None:
        save_exhibit_assets(fetcher, base_url, html_name, html_bytes, dest_dir, index_names)
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / html_name).write_bytes(html_bytes)

    source_sha = hashlib.sha256(html_bytes).hexdigest()
    stem = Path(html_name).stem
    full_pdf = dest_dir / f"{stem}.pdf"
    renderer = render(dest_dir / html_name, full_pdf, chrome)
    rendered_sha = sha256_file(full_pdf)
    page_bits = cut_pages(full_pdf, dest_dir, stem)
    exhibit = exhibit or exhibit_number(html_name)
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_url = urljoin(base_url, html_name)
    # page_file / words_file: relative to filings/<TICKER>/ (sources._resolve).
    # local_path: relative to the filings root (never an absolute /Users/… path).
    rel_prefix = f"{accession}/"
    try:
        local_rel = (dest_dir / html_name).resolve().relative_to(filings_root.resolve()).as_posix()
    except ValueError:
        local_rel = f"{ticker}/{accession}/{html_name}"
    out = []
    for p in page_bits:
        ex_slug = exhibit.replace(".", "")
        form_slug = re.sub(r"[^a-z0-9]", "", form.lower())
        page_id = f"{ticker.lower()}-{form_slug}-{accession}-ex{ex_slug}-p{p['page_number']}"
        entry = {
            "ticker": ticker,
            "company": company,
            "cik": cik,
            "form": form,
            "exhibit": exhibit,
            "filing_date": filing_date,
            "accession": accession,
            "source_url": source_url,
            "source_format": "html",
            "source_sha256": source_sha,
            "rendered_pdf_sha256": rendered_sha,
            "renderer": renderer,
            "rendered_page_count": len(page_bits),
            "page_number": p["page_number"],
            "page_file": rel_prefix + p["page_file"],
            "page_sha256": p["page_sha256"],
            "words_file": rel_prefix + p["words_file"],
            "words_sha256": p["words_sha256"],
            "word_count": p["word_count"],
            "captured_at": captured_at,
            "capture_tool": f"pymupdf {pymupdf.VersionBind}",
            "id": page_id,
            "kind": "events",
            # Order fields (also useful for humans reading the index):
            "html_sha256": source_sha,
            "pdf_sha256": rendered_sha,
            "local_path": local_rel,
            "page_count": len(page_bits),
            "rendered_from": {"format": "html", "renderer": renderer, "sha256": source_sha},
        }
        if exhibit_title:
            entry["exhibit_title"] = exhibit_title
        out.append(entry)
    return out


# ---------------------------------------------------------------- per-ticker / main


def fetch_ticker(
    ticker: str,
    *,
    days: int,
    fetcher: RateLimitedFetcher,
    filings_root: Path = FILINGS,
    chrome: Path | None = None,
    today: date | None = None,
    until: date | None = None,
    forms: tuple[str, ...] = ("8-K",),
    submissions: dict | None = None,
    index_by_accession: dict[str, dict] | None = None,
    headers_by_accession: dict[str, str] | None = None,
    html_by_url: dict[str, bytes] | None = None,
    render: Callable[[Path, Path, Path | None], str] = render_html_to_pdf,
    tickers_doc: dict | None = None,
    failures: list[str] | None = None,
) -> list[dict]:
    """Fetch and capture every wanted filing's EX-99.* HTML exhibits for one ticker. Returns page entries.

    For 10-K/10-Q/S-1/424B the filing's primary document is captured too (exhibit "main"); 8-Ks give exhibits only.
    A document that fails to fetch or render is recorded in `failures` (and printed) and the rest carry on.
    """
    ticker = ticker.upper()
    cik, company = resolve_cik(ticker, fetcher, tickers_doc=tickers_doc)
    if submissions is None:
        submissions = fetcher.get_json(SUBMISSIONS_URL.format(cik=cik))
        # Prefer the live company name when present.
        company = submissions.get("name") or company
    # Cached beside the filings so capture/index_filings.py can read cik, company, form and date from it.
    (filings_root / ticker).mkdir(parents=True, exist_ok=True)
    (filings_root / ticker / "submissions.json").write_text(json.dumps(submissions))
    filings = select_filings(submissions, days, today=today, until=until, forms=forms)
    pages: list[dict] = []
    for filing in filings:
        acc = filing["accession"]
        acc_nodash = acc.replace("-", "")
        base_url = ARCHIVES.format(cik=int(cik), acc_nodash=acc_nodash)
        if index_by_accession is not None:
            index_doc = index_by_accession[acc]
        else:
            index_doc = fetcher.get_json(urljoin(base_url, "index.json"))
        all_names = [
            (it.get("name") if isinstance(it, dict) else None)
            for it in ((index_doc.get("directory") or {}).get("item") or [])
        ]
        all_names = [n for n in all_names if n]
        # Prefer TYPE→FILENAME from index-headers (filenames often omit "ex99").
        if headers_by_accession is not None:
            headers_text = headers_by_accession.get(acc, "")
        else:
            headers_url = urljoin(base_url, f"{acc}-index-headers.html")
            try:
                headers_text = fetcher.get(headers_url).decode("utf-8", errors="replace")
            except FetchError:
                headers_text = ""
        typed = ex99_html_from_headers(headers_text) if headers_text else []
        if typed:
            exhibits = typed
        else:
            exhibits = [
                {"filename": n, "exhibit": exhibit_number(n), "title": None, "type": f"EX-{exhibit_number(n)}"}
                for n in list_ex99_html(index_doc)
            ]
        primary = filing.get("primary_document") or ""
        if form_family(filing["form"]) != "8-K" and primary.lower().endswith((".htm", ".html")) and "/" not in primary:
            if all(e["filename"] != primary for e in exhibits):
                exhibits = [{"filename": primary, "exhibit": "main", "title": filing["form"], "type": filing["form"]}] + exhibits
        dest = filings_root / ticker / acc
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "filing.json").write_text(json.dumps({
            "form": filing["form"], "filing_date": filing["filing_date"], "accession": acc,
            "primary_document": primary,
            "documents": {e["filename"]: {"exhibit": e["exhibit"], "title": e.get("title")} for e in exhibits},
        }, indent=1) + "\n")
        for ex in exhibits:
            html_name = ex["filename"]
            url = urljoin(base_url, html_name)
            try:
                if html_by_url is not None:
                    html_bytes = html_by_url[url]
                    use_fetcher = None
                else:
                    html_bytes = fetcher.get(url)
                    use_fetcher = fetcher
                pages.extend(capture_exhibit(
                    ticker=ticker,
                    company=company,
                    cik=str(int(cik)),
                    form=filing["form"],
                    filing_date=filing["filing_date"],
                    accession=acc,
                    html_name=html_name,
                    html_bytes=html_bytes,
                    dest_dir=dest,
                    base_url=base_url,
                    index_names=all_names,
                    fetcher=use_fetcher,
                    chrome=chrome,
                    render=render,
                    exhibit=ex.get("exhibit"),
                    exhibit_title=ex.get("title"),
                    filings_root=filings_root,
                ))
            except FetchError as e:
                msg = f"{ticker} {filing['form']} {acc} {html_name}: {e}"
                print(f"  FAILED {msg}", file=sys.stderr)
                if failures is not None:
                    failures.append(msg)
    return pages


def build_index(pages_by_ticker: dict[str, list[dict]]) -> dict:
    companies = {}
    for ticker in TICKERS:
        if ticker not in pages_by_ticker:
            continue
        companies[ticker] = {"pages": pages_by_ticker[ticker]}
    # Also keep any unexpected tickers at the end.
    for ticker, pages in pages_by_ticker.items():
        if ticker not in companies:
            companies[ticker] = {"pages": pages}
    return {
        "index_version": 1,
        "note": (
            "Written by capture/fetch_filings.py. Page PDFs and words live under "
            "~/proof-reader-docs/filings/<TICKER>/<accession>/; never in the repo."
        ),
        "companies": companies,
    }


def write_index(doc: dict, path: Path = INDEX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n")


def parse_tickers(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.lower() == "all":
        return list(TICKERS)
    out = []
    for part in raw.replace(",", " ").split():
        t = part.strip().upper()
        if not TICKER_RE.match(t):
            raise SystemExit(f"Bad ticker {part!r}: use A-Z, 0-9, '.' or '-' (1-10 characters), or 'all'.")
        if t not in out:
            out.append(t)
    if not out:
        raise SystemExit("Pass --tickers all or a list of tickers.")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", required=True, help="'all' (the twelve) or a comma/space list; others resolve via SEC company_tickers.json")
    ap.add_argument("--until", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                    help="only filings with filing_date <= this date; the --days window counts back from it")
    ap.add_argument("--out", type=Path, default=None, metavar="DIR", help="filings store; same as --filings-root")
    ap.add_argument("--forms", default="8-K", help=f"comma list from {', '.join(FORM_FAMILIES)} (424B = any 424B*); default 8-K")
    ap.add_argument("--days", type=int, default=90, help="how many days of 8-K filings to take (default 90)")
    ap.add_argument("--filings-root", type=Path, default=FILINGS, help="local store (default ~/proof-reader-docs/filings)")
    ap.add_argument("--index", type=Path, default=INDEX_PATH, help="where to write runs/sources_index.twelve.json")
    ap.add_argument("--chrome", type=Path, default=None, help="Chrome binary (default: auto-detect)")
    args = ap.parse_args(argv)
    if args.out is not None:
        args.filings_root = args.out
    forms = tuple(f.strip().upper() for f in args.forms.split(",") if f.strip())
    bad = [f for f in forms if f not in FORM_FAMILIES]
    if bad or not forms:
        raise SystemExit(f"--forms: unknown {bad or forms}; choose from {', '.join(FORM_FAMILIES)}.")

    tickers = parse_tickers(args.tickers)
    ua = load_user_agent()
    fetcher = RateLimitedFetcher(ua)
    chrome = find_chrome(args.chrome)
    print(f"Chrome: {renderer_label(chrome)}")
    print(f"Filings root: {args.filings_root}")
    print(f"Tickers ({len(tickers)}), last {args.days} days{f' to {args.until}' if args.until else ''}, forms {','.join(forms)}.")

    by_ticker: dict[str, list[dict]] = {}
    failures: list[str] = []
    for t in tickers:
        print(f"\n=== {t} ===")
        pages = fetch_ticker(
            t, days=args.days, fetcher=fetcher, filings_root=args.filings_root, chrome=chrome,
            until=args.until, forms=forms, failures=failures,
        )
        by_ticker[t] = pages
        exhibits = {(p["accession"], p["exhibit"]) for p in pages}
        print(f"{t}: {len(exhibits)} exhibit(s), {len(pages)} page(s).")

    # If writing the full index, replace it; if a subset run, merge into any existing index.
    if args.index.is_file() and set(tickers) != set(TICKERS):
        existing = json.loads(args.index.read_text())
        companies = dict(existing.get("companies") or {})
        for t, pages in by_ticker.items():
            companies[t] = {"pages": pages}
        doc = {
            "index_version": existing.get("index_version", 1),
            "note": existing.get("note") or build_index(by_ticker)["note"],
            "companies": {k: companies[k] for k in sorted(companies)},
        }
    else:
        doc = build_index(by_ticker)
    write_index(doc, args.index)
    total_pages = sum(len(v) for v in by_ticker.values())
    print(f"\nWrote {args.index} ({total_pages} page entries).")
    if failures:
        print(f"{len(failures)} document(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
