"""Index filings already on disk for extra tickers (no network, no rendering).

Usage (from the repo root):
    python -m capture.index_filings --tickers KRYS,BBIO --out runs/sources_index.eval2.json
    python -m capture.index_filings --tickers KRYS,BBIO --out runs/sources_index.json \
        --merge runs/sources_index.twelve.json

Walks ~/proof-reader-docs/filings/<TICKER>/<accession>/ (the shape capture/fetch_filings.py writes: the exhibit .htm,
<stem>.pdf, <stem>_pN.pdf and <stem>_pN.words.json) and writes MANIFEST-shaped page entries (kind=events) by hashing
the files that are there. Nothing is fetched and nothing is rendered.

Where each field comes from:
  company      a cached SEC submissions JSON in the ticker folder (or the CIKS table), else the exhibit's own header
               line (first line naming an Inc./Corp./Ltd./...), else the ticker
  filing_date  <accession>/filing.json or a submissions JSON listing the accession, else the exhibit file's mtime
               date, marked "filing_date_source": "mtime"
  exhibit      the <TYPE>EX-99.n line inside the filed document, else the file name, else 99.1 -> "99"
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pymupdf

from capture.capture_page import sha256_file
from capture.capture_source import completeness_fields
from capture.fetch_filings import (
    ARCHIVES, CIKS, FILINGS, build_index, exhibit_number, write_index,
)

REPO = Path(__file__).resolve().parent.parent
TICKER = re.compile(r"^[A-Z0-9.-]{1,10}$")
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
TYPE_LINE = re.compile(r"(?im)^\s*<TYPE>\s*(EX-99(?:\.\d+)*)\s*$")
NAME_LINE = re.compile(
    r"(?i)^[A-Z][\w&.,'\- ]{2,80}?\b(?:Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|Limited|plc|N\.V\.|S\.A\.|"
    r"Therapeutics|Pharmaceuticals|Biosciences|Biopharma)\b[.,]?$"
)
_TAGS = re.compile(r"(?s)<(script|style)\b.*?</\1>|<[^>]+>")


def _rel_base(filings_root: Path) -> Path:
    """The ONE path convention: index paths are relative to ~/proof-reader-docs/filings (FILINGS), so a set under
    FILINGS/<subdir>/ gets "<subdir>/<TICKER>/<accession>/...". A root outside FILINGS (tests) is its own base."""
    real, base = filings_root.resolve(), FILINGS.resolve()
    return base if real == base or base in real.parents else real


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _contained(path: Path, root: Path, what: str = "") -> Path | None:
    """Resolved `path` if it lies inside resolved `root` (symlinks followed), else None with a printed reason."""
    try:
        real = path.resolve()
        real.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        print(f"  skip {what or path.name}: resolves outside the filings root", file=sys.stderr)
        return None
    return real


def _submissions(ticker_dir: Path, root: Path | None = None) -> dict | None:
    """A cached SEC submissions JSON in the ticker folder, if any."""
    for p in sorted(ticker_dir.glob("*.json")):
        if _contained(p, root or ticker_dir, p.name) is None:
            continue
        doc = _read_json(p)
        if isinstance(doc, dict) and isinstance(doc.get("filings"), dict):
            return doc
    return None


def _submission_row(subs: dict | None, accession: str) -> dict:
    recent = ((subs or {}).get("filings") or {}).get("recent") or {}
    accs = recent.get("accessionNumber") or []
    if accession not in accs:
        return {}
    i = accs.index(accession)
    row = {}
    if i < len(recent.get("filingDate") or []):
        row["filing_date"] = recent["filingDate"][i]
    if i < len(recent.get("form") or []):
        row["form"] = recent["form"][i]
    return row


def _header_company(html_text: str) -> str | None:
    text = htmllib.unescape(_TAGS.sub("\n", html_text))
    for line in text.splitlines()[:400]:
        line = " ".join(line.split())
        if line and NAME_LINE.match(line):
            return line.rstrip(",")
    return None


def _exhibits(acc_dir: Path, root: Path) -> list[Path]:
    """Exhibit .htm files that have a rendered <stem>.pdf beside them (both inside the filings root)."""
    return [h for h in sorted(acc_dir.glob("*.htm")) + sorted(acc_dir.glob("*.html"))
            if h.with_suffix(".pdf").is_file()
            and _contained(h, root, h.name) and _contained(h.with_suffix(".pdf"), root, h.with_suffix(".pdf").name)]


def index_exhibit(ticker: str, company: str, cik: str | None, form: str, filing_date: str, date_source: str | None,
                  acc_dir: Path, html_path: Path, filings_root: Path, doc_meta: dict | None = None) -> list[dict]:
    accession = acc_dir.name
    stem = html_path.stem
    full_pdf = html_path.with_suffix(".pdf")
    html_text = html_path.read_text(errors="replace")
    m = TYPE_LINE.search(html_text[:2000])
    exhibit = (doc_meta or {}).get("exhibit") or exhibit_number(m.group(1) if m else html_path.name)
    exhibit_title = (doc_meta or {}).get("title") or f"EXHIBIT {exhibit}"
    nums = sorted(
        int(g.group(1)) for p in acc_dir.glob(f"{stem}_p*.pdf") if (g := re.fullmatch(re.escape(stem) + r"_p(\d+)\.pdf", p.name))
    )
    source_sha = sha256_file(html_path)
    pdf_sha = sha256_file(full_pdf)
    renderer = "unknown (indexed from files on disk)"
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_url = ARCHIVES.format(cik=int(cik), acc_nodash=accession.replace("-", "")) + html_path.name if cik else None
    rel_base = _rel_base(filings_root)
    local_rel = html_path.resolve().relative_to(rel_base).as_posix()
    pages = []
    for n in nums:
        page_pdf = acc_dir / f"{stem}_p{n}.pdf"
        words_path = acc_dir / f"{stem}_p{n}.words.json"
        if not words_path.is_file():
            print(f"  skip {page_pdf.name}: no words file", file=sys.stderr)
            continue
        if not (_contained(page_pdf, filings_root) and _contained(words_path, filings_root)):
            continue
        words = _read_json(words_path)
        if not isinstance(words, dict) or not isinstance(words.get("words"), list):
            print(f"  skip {page_pdf.name}: words file unreadable or malformed", file=sys.stderr)
            continue
        wc = len(words["words"])
        entry = {
            "ticker": ticker, "company": company, "form": form, "exhibit": exhibit,
            "exhibit_title": exhibit_title,
            "filing_date": filing_date, "accession": accession,
            "source_url": source_url, "source_format": "html",
            "source_sha256": source_sha, "rendered_pdf_sha256": pdf_sha, "renderer": renderer,
            "rendered_page_count": len(nums), "page_number": n,
            "page_file": page_pdf.resolve().relative_to(rel_base).as_posix(), "page_sha256": sha256_file(page_pdf),
            "words_file": words_path.resolve().relative_to(rel_base).as_posix(), "words_sha256": sha256_file(words_path),
            "word_count": wc, "captured_at": captured_at, "capture_tool": f"pymupdf {pymupdf.VersionBind}",
            "id": f"{ticker.lower()}-{re.sub(r'[^a-z0-9]', '', form.lower())}-{accession}-ex{exhibit.replace('.', '')}-p{n}",
            "kind": "events",
            "html_sha256": source_sha, "pdf_sha256": pdf_sha, "local_path": local_rel, "page_count": len(nums),
            "rendered_from": {"format": "html", "renderer": renderer, "sha256": source_sha},
        }
        if cik:
            entry["cik"] = str(int(cik))
        if date_source:
            entry["filing_date_source"] = date_source
        # Carry completeness from the words doc (one authoritative record). Legacy words without a
        # completeness field are not certified complete — silence is not establishment.
        carried = completeness_fields(words)
        if carried:
            entry.update(carried)
        else:
            entry["completeness_not_established"] = True
            entry["certified_complete"] = False
            entry["completeness_reason"] = (
                "Completeness was not recorded in the words doc at capture; it is not established at index time."
            )
        pages.append(entry)
    return pages


def index_ticker(ticker: str, filings_root: Path = FILINGS) -> list[dict]:
    ticker = ticker.upper()
    if not TICKER.match(ticker):
        print(f"  skip ticker {ticker!r}: tickers may only use A-Z, 0-9, '.' and '-' (1-10 characters)", file=sys.stderr)
        return []
    tdir = filings_root / ticker
    if not tdir.is_dir() or not _contained(tdir, filings_root, ticker):
        return []
    subs = _submissions(tdir, filings_root)
    cik = str(int(subs["cik"])) if subs and str(subs.get("cik", "")).strip().isdigit() else (
        CIKS[ticker][0] if ticker in CIKS else None)
    sub_company = (subs or {}).get("name") or (CIKS[ticker][1] if ticker in CIKS else None)
    pages: list[dict] = []
    for acc_dir in sorted(p for p in tdir.iterdir() if p.is_dir() and ACCESSION.match(p.name)):
        if not _contained(acc_dir, filings_root, f"{ticker}/{acc_dir.name}"):
            continue
        fj = acc_dir / "filing.json"
        meta = _read_json(fj) if fj.exists() and _contained(fj, filings_root, "filing.json") else None
        meta = meta if isinstance(meta, dict) else {}
        row = _submission_row(subs, acc_dir.name)
        for html_path in _exhibits(acc_dir, filings_root):
            filing_date = meta.get("filing_date") or row.get("filing_date")
            date_source = None
            if not filing_date:
                filing_date = datetime.fromtimestamp(html_path.stat().st_mtime, timezone.utc).date().isoformat()
                date_source = "mtime"
            company = sub_company or _header_company(html_path.read_text(errors="replace")) or ticker
            form = meta.get("form") or row.get("form") or "8-K"
            docs = meta.get("documents") if isinstance(meta.get("documents"), dict) else {}
            pages.extend(index_exhibit(ticker, company, cik, form, filing_date, date_source,
                                       acc_dir, html_path, filings_root, docs.get(html_path.name)))
    return pages


def workout_pages(pages: list[dict], until: date, days: int = 180) -> list[dict]:
    """Workout scope: 8-K / 8-K/A exhibit pages (not 10-K/10-Q/S-1/424B bodies) filed in [until - days, until]."""
    lo = (until - timedelta(days=days)).isoformat()
    hi = until.isoformat()
    return [p for p in pages
            if p.get("form", "").upper().split("/")[0] == "8-K" and p.get("exhibit") != "main"
            and lo <= p.get("filing_date", "") <= hi]


def scope_table(full: dict[str, list[dict]], workout: dict[str, list[dict]]) -> list[str]:
    """Per ticker: filings, exhibits, pages in the workout index and in the full index."""
    def counts(pages):
        return len({p["accession"] for p in pages}), len({(p["accession"], p["exhibit"]) for p in pages}), len(pages)
    rows = [f"{'ticker':<7}{'filings':>8}{'exhibits':>9}{'wk pages':>9}{'full pages':>11}"]
    tot = [0, 0, 0, 0]
    for t, pages in full.items():
        f, e, n = counts(pages)
        w = len(workout.get(t, []))
        rows.append(f"{t:<7}{f:>8}{e:>9}{w:>9}{n:>11}")
        tot = [tot[0] + f, tot[1] + e, tot[2] + w, tot[3] + n]
    rows.append(f"{'TOTAL':<7}{tot[0]:>8}{tot[1]:>9}{tot[2]:>9}{tot[3]:>11}")
    return rows


def merge_index(base: dict, extra: dict) -> dict:
    """Union of two index docs; a ticker in `extra` replaces the same ticker in `base`."""
    companies = dict(base.get("companies") or {})
    companies.update(extra.get("companies") or {})
    return {**{k: v for k, v in base.items() if k != "companies"}, "companies": companies}


def cap_until(doc: dict, until: date) -> tuple[dict, int]:
    """Drop every page whose filing_date is missing or after `until`; returns (doc, dropped count)."""
    hi = until.isoformat()
    dropped = 0
    companies = {}
    for t, c in (doc.get("companies") or {}).items():
        pages = c.get("pages", [])
        kept = [p for p in pages if p.get("filing_date") and str(p["filing_date"]) <= hi]
        dropped += len(pages) - len(kept)
        companies[t] = {**c, "pages": kept}
    return {**doc, "companies": companies}, dropped


def parse_tickers(raw: str) -> list[str]:
    out = []
    for part in raw.replace(",", " ").split():
        t = part.strip().upper()
        if not TICKER.match(t):
            raise SystemExit(f"Bad ticker {part!r}: tickers may only use A-Z, 0-9, '.' and '-' (1-10 characters).")
        if t not in out:
            out.append(t)
    if not out:
        raise SystemExit("Pass --tickers KRYS,BBIO,...")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--merge", type=Path, default=None, help="index to union with (e.g. runs/sources_index.twelve.json)")
    ap.add_argument("--filings-root", type=Path, default=FILINGS)
    ap.add_argument("--until", type=date.fromisoformat, default=None,
                    help="YYYY-MM-DD cutoff for the workout scope (default today); with --full-out, --out gets the workout scope. "
                         "Also applied to the final --out index AFTER --merge: pages in the merged-in index filed after this date are dropped")
    ap.add_argument("--exhibit-days", type=int, default=180, help="workout window: 8-K exhibits filed within this many days before --until")
    ap.add_argument("--full-out", type=Path, default=None, help="also write the unscoped index here")
    args = ap.parse_args(argv)
    scoped = args.full_out is not None or args.until is not None

    by_ticker = {}
    for t in parse_tickers(args.tickers):
        by_ticker[t] = index_ticker(t, args.filings_root)
    full_by_ticker = by_ticker
    if scoped:
        until = args.until or date.today()
        by_ticker = {t: workout_pages(p, until, args.exhibit_days) for t, p in full_by_ticker.items()}
        if args.full_out:
            full_doc = build_index(full_by_ticker)
            full_doc["note"] = "Full index (all forms, 10-K/10-Q/S-1/424B bodies included); see capture/README.md."
            write_index(full_doc, args.full_out)
            print(f"Wrote {args.full_out}")
    doc = build_index(by_ticker)
    doc["note"] = ("Written by capture/index_filings.py from files on disk under "
                   "~/proof-reader-docs/filings/[<subdir>/]<TICKER>/<accession>/; never in the repo. page_file, words_file and "
                   "local_path are relative to ~/proof-reader-docs/filings (including any <subdir>/), not to the ticker dir.")
    if args.merge:
        base = _read_json(args.merge)
        if not isinstance(base, dict) or "companies" not in base:
            raise SystemExit(f"--merge {args.merge}: not a readable index with a 'companies' map.")
        doc = merge_index(base, doc)
        if args.until is not None:
            doc, dropped = cap_until(doc, args.until)
            if dropped:
                print(f"--until {args.until}: dropped {dropped} merged page(s) filed after the cutoff")
    write_index(doc, args.out)
    print(f"Wrote {args.out}")
    if scoped:
        print("\n".join(scope_table(full_by_ticker, by_ticker)))
    else:
        for t in by_ticker:
            print(f"{t}: {len(by_ticker[t])} page(s)")
    if args.merge:
        total = sum(len(c.get("pages", [])) for c in doc["companies"].values())
        print(f"Total: {len(doc['companies'])} companies, {total} page(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
