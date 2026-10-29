"""Scan the full git history of this repo for secrets and leak shapes before a public flip.

Usage (from the repo root):
    python tools/flip_scan.py
    python tools/flip_scan.py --repo /path/to/proof-reader --json

Walks every commit and every blob. Prints one line per finding:
    <kind>  <commit>  <path>  line <n>

Kinds: email, key_sk, key_api, key_long, pz, users_path, filed_html, full_pdf

Never prints the matched value (public-flip safe). Exit 0 always; the report is the
stdout. No network. No model calls.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ---- patterns (values never echoed) -----------------------------------------

EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
# sk-… (OpenAI-style) live tokens — not the bare prefix in docs.
KEY_SK = re.compile(r"\bsk-(?:live|proj|test)-[A-Za-z0-9_-]{8,}\b|\bsk-[A-Za-z0-9]{20,}\b")
# api_key=VALUE where VALUE looks like a literal secret (not os.environ / get / argv).
KEY_API = re.compile(
    r"""(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|MODEL_API_KEY|BASETEN_API_KEY)\s*[=:]\s*['\"]([^'\"]{8,})['\"]"""
)
# 40+ hex/base64 runs that are NOT ordinary fingerprint / accession / URL path noise.
KEY_LONG = re.compile(r"(?i)(?<![A-Fa-f0-9+/=])(?:[A-Fa-f0-9]{40,}|[A-Za-z0-9+/]{40,}={0,2})(?![A-Fa-f0-9+/=])")
SHA_FIELD = re.compile(r"(?i)\b(sha256|hash|fingerprint|digest|checksum|accession)\b")
ENV_RHS = re.compile(r"""(?i)(os\.environ|getenv|getenv|os\.getenv|environ\[)""")
# Whole word PZ (case-sensitive per the order: "the word PZ")
PZ = re.compile(r"\bPZ\b")
USERS = re.compile(r"(?<![\w/])/Users/[A-Za-z0-9._-]+(?:/[^\s\"'<>]*)?")

# Filed HTML: actual HTML/SGML bodies, not JSON that merely cites an EDGAR URL.
EDGAR_HTML_BODY = re.compile(
    r"(?is)(?:<!DOCTYPE\s+html|<html[\s>]|<TYPE>EX-99|&lt;TYPE&gt;EX-99|SEC-DOCUMENT:)"
)
PDF_MAGIC = b"%PDF-"

# Single-page fixture PDFs that are allowed in the product repo (named in .gitignore exceptions).
ALLOWED_PDF_SUFFIXES = (
    "ions_ars_2026_p95.pdf",
    "ptgx_8k_2026-08-05_ex991_p3.pdf",
)

TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".svg", ".xml", ".csv",
    ".sh", ".env", ".example", ".gitignore", ".dockerignore", ".rst", ".csv",
}
SKIP_DIR_PREFIXES = ("ui/node_modules/", ".git/")


def git(repo: Path, *args: str, raw: bool = False) -> bytes | str:
    out = subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)
    return out if raw else out.decode("utf-8", errors="replace")


def all_blobs(repo: Path) -> list[tuple[str, str, str]]:
    """[(commit, mode_path_or_path, sha), ...] for every tree entry that is a blob, every commit.

    Uses `git rev-list --objects --all` which lists commit + reachable objects once; we then
    attribute each blob to every commit that first introduced it via `git log --all --find-object`
    would be slow, so instead we walk each commit's tree (small history).
    """
    commits = git(repo, "rev-list", "--all").split()
    rows: list[tuple[str, str, str]] = []
    for commit in commits:
        # name-only with object names: <mode> <type> <sha>\t<path>
        listing = git(repo, "ls-tree", "-r", commit)
        for line in listing.splitlines():
            if not line.strip():
                continue
            meta, path = line.split("\t", 1)
            mode, typ, sha = meta.split()
            if typ != "blob":
                continue
            if any(path.startswith(p) for p in SKIP_DIR_PREFIXES):
                continue
            rows.append((commit, path, sha))
    return rows


def is_probably_text(path: str, data: bytes) -> bool:
    if b"\x00" in data[:8000]:
        return False
    suf = Path(path).suffix.lower()
    if suf in TEXT_EXT or suf == "":
        return True
    # sniff
    try:
        data[:4000].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _key_long_is_noise(line: str) -> bool:
    """True for fingerprint / URL / accession lines that are not secret material."""
    if SHA_FIELD.search(line):
        return True
    if "sec.gov/" in line.lower() or "edgar/data/" in line.lower():
        return True
    if re.search(r"(?i)\"[a-z0-9_]*sha256\"\s*:", line):
        return True
    if re.search(r"(?i)(page_sha256|words_sha256|source_sha256|rendered_pdf_sha256|image_sha256)", line):
        return True
    return False


def scan_text(path: str, commit: str, text: str) -> list[dict]:
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        kinds = []
        if EMAIL.search(line):
            kinds.append("email")
        if KEY_SK.search(line):
            kinds.append("key_sk")
        m_api = KEY_API.search(line)
        if m_api and not ENV_RHS.search(line):
            # Skip obvious placeholders.
            val = m_api.group(2)
            if not re.search(r"(?i)^(your|xxx|todo|changeme|placeholder|none|null|test)", val):
                kinds.append("key_api")
        if KEY_LONG.search(line) and not _key_long_is_noise(line):
            kinds.append("key_long")
        if PZ.search(line):
            kinds.append("pz")
        if USERS.search(line):
            kinds.append("users_path")
        for kind in kinds:
            hits.append({"kind": kind, "commit": commit, "path": path, "line": i})
    # Filed HTML: real HTML/SGML bodies (fixtures sample HTML is tiny and OK).
    low = path.lower()
    if low.endswith((".htm", ".html")) and EDGAR_HTML_BODY.search(text) and len(text) > 20_000:
        hits.append({"kind": "filed_html", "commit": commit, "path": path, "line": 0})
    elif EDGAR_HTML_BODY.search(text[:5000]) and len(text) > 50_000 and not low.endswith(".json"):
        hits.append({"kind": "filed_html", "commit": commit, "path": path, "line": 0})
    return hits


def scan_pdf(path: str, commit: str, data: bytes) -> list[dict]:
    if not data.startswith(PDF_MAGIC):
        return []
    name = Path(path).name.lower()
    if any(name.endswith(a) or name == a for a in ALLOWED_PDF_SUFFIXES):
        return []
    # Fixture single pages are small; a "full" rendered exhibit or ARS is much larger.
    # Also flag any PDF outside fixtures/.
    under_fixtures = path.replace("\\", "/").startswith("fixtures/")
    if under_fixtures and len(data) < 200_000:
        return []
    return [{"kind": "full_pdf", "commit": commit, "path": path, "line": 0, "bytes": len(data)}]


def load_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """Batch-load blob contents."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout
    out: dict[str, bytes] = {}
    for sha in shas:
        proc.stdin.write(sha.encode() + b"\n")
    proc.stdin.close()
    stdout = proc.stdout
    for sha in shas:
        header = stdout.readline()
        if not header:
            break
        parts = header.decode().split()
        if len(parts) < 3 or parts[1] == "missing":
            continue
        size = int(parts[2])
        data = stdout.read(size)
        stdout.read(1)  # trailing newline
        out[sha] = data
    proc.wait()
    return out


def run(repo: Path) -> list[dict]:
    rows = all_blobs(repo)
    # Unique shas
    shas = sorted({sha for _, _, sha in rows})
    blobs = load_blobs(repo, shas)
    # Deduplicate findings by (kind, path, line, content-hash-of-line-position) across commits:
    # keep the oldest commit that introduced the hit (last in rev-list is oldest? rev-list is newest-first)
    # We report every (commit, path, line) as ordered — but collapse identical path+line+kind to first (newest) only
    # for readability; also emit a unique-by-path summary. Order asks: print file, commit, line — so one per commit
    # that contains it is correct but noisy. Prefer: one row per (kind, path, line) with the tip-most commit.
    best: dict[tuple, dict] = {}
    commit_order = {c: i for i, c in enumerate(git(repo, "rev-list", "--all").split())}
    for commit, path, sha in rows:
        data = blobs.get(sha)
        if data is None:
            continue
        hits = []
        if data.startswith(PDF_MAGIC) or path.lower().endswith(".pdf"):
            hits.extend(scan_pdf(path, commit, data))
        if is_probably_text(path, data):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            hits.extend(scan_text(path, commit, text))
        for h in hits:
            key = (h["kind"], h["path"], h["line"])
            prev = best.get(key)
            if prev is None or commit_order.get(commit, 10**9) < commit_order.get(prev["commit"], 10**9):
                # keep newest commit that still has it (smaller index in rev-list)
                best[key] = h
    # Sort: kind, path, line
    return sorted(best.values(), key=lambda h: (h["kind"], h["path"], h["line"], h["commit"]))


def format_line(h: dict) -> str:
    line = h["line"]
    line_s = f"line {line}" if line else "file"
    return f"{h['kind']:12}  {h['commit'][:12]}  {h['path']}  {line_s}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text lines")
    args = ap.parse_args(argv)
    hits = run(args.repo.resolve())
    if args.json:
        # Still no values — only kind/commit/path/line
        slim = [{"kind": h["kind"], "commit": h["commit"], "path": h["path"], "line": h["line"]} for h in hits]
        print(json.dumps(slim, indent=2))
    else:
        by = defaultdict(int)
        for h in hits:
            print(format_line(h))
            by[h["kind"]] += 1
        print(file=sys.stderr)
        print(f"# {len(hits)} finding(s) across {len(by)} kind(s): " +
              ", ".join(f"{k}={v}" for k, v in sorted(by.items())), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
