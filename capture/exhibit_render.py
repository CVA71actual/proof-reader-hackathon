"""Isolated rendering of a captured SEC exhibit (Codex decision 5a; ISOLATE-001, EVIDENCE-001).

server/intake.py renders every captured exhibit with render_captured_exhibit. The exhibit HTML is untrusted, so it is
never opened as received: it goes through the same sanitizer, Content-Security-Policy and isolated headless Chrome
launch as a fetched web page (capture.research_sources.sanitize_html and isolated_print). The one difference is the
exhibit's own images, which a slide deck exhibit is made of:

    an <img> reference is resolved against the exhibit's capture folder and the folder's asset ledger only
    (capture.fetch_filings.ASSET_LEDGER: the sidecars this capture fetched, by name and SHA-256). It is shown only when
    it names, in the same folder, an asset the ledger lists as captured for this exhibit, the file is a regular file
    (or a symlink that stays in the folder), its bytes still have the recorded SHA-256, and they decode as a PNG, JPEG,
    GIF or WebP raster. Those bytes are inlined as a data: URI in the snapshot; the browser itself never reads a file.
    Every other reference is omitted with the usual placeholder and a recorded reason: a network or file: URL, a path
    outside the folder or in a subfolder, a symlink that escapes the folder, a file the capture did not write, a
    changed file, an SVG, HTML or other non-raster file, an inline data: image that is not a raster.

The paper is sized to the inlined images (capture.fetch_filings.paper_size_for_pixels, the existing wide-slide rule),
so a wide slide is not cut at Letter width. The fetcher's original HTML and assets are never modified: the snapshot is
written only in a temporary folder. Beside the PDF, <pdf>.render.json records each inlined asset (name, SHA-256, bytes,
media type, pixel size), each omitted reference with its reason, what the sanitizer removed, and the limitations. A
sanitized rendering never certifies that the capture is complete or that it matches the original visual layout.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

import pymupdf

from capture import fetch_filings as ff
from capture import research_sources as rs

MAX_ASSET_BYTES = 25 * 1024 * 1024
RENDER_RECORD_SUFFIX = ".render.json"
RENDERER = "capture.exhibit_render.render_captured_exhibit"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_DATA = re.compile(r"(?is)^data:([^,]*?),(.*)$")
_CHARSET = re.compile(rb"""(?i)<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""")
LIMITATION = ("Sanitized rendering: scripts, external stylesheets, CSS images and network or file resources are not "
              "loaded; this rendering does not certify that the capture is complete or matches the original layout.")


def raster_type(data: bytes) -> str | None:
    """The raster media type the bytes begin with (PNG, JPEG, GIF, WebP), or None (SVG, HTML and anything else)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _pixels(data: bytes) -> tuple[int, int] | None:
    try:
        pix = pymupdf.Pixmap(data)
    except Exception:
        return None
    try:
        return int(pix.width), int(pix.height)
    finally:
        pix = None


def decode_html(raw: bytes) -> str:
    """The exhibit's text: its declared charset when it names one Python knows, else UTF-8, else Windows-1252 (EDGAR
    exhibits are often single-byte). The snapshot is always written and declared as UTF-8."""
    m = _CHARSET.search(raw[:4096])
    if m:
        try:
            return raw.decode(m.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


class AssetResolver:
    """sanitize_html's image_resolver for one exhibit: resolves <img src> against capture_dir and the exhibit's
    captured asset list only. Collects what it inlined (consumed) and what it refused (omitted, with a reason)."""

    def __init__(self, capture_dir: Path, html_name: str, ledger: dict | None):
        self.capture_dir = Path(capture_dir).resolve()
        ledger = ledger or {"assets": {}, "exhibits": {}}
        entry = ledger["exhibits"].get(html_name)
        self.has_list = isinstance(entry, dict)
        captured = entry.get("captured", []) if self.has_list else []
        assets = ledger["assets"]
        self.allowed = {n: assets[n] for n in captured if isinstance(n, str) and isinstance(assets.get(n), dict)}
        self.consumed: list[dict] = []
        self.omitted: list[dict] = []
        self._cache: dict[str, str] = {}

    def _omit(self, src: str, reason: str) -> None:
        self.omitted.append({"ref": src[:200], "reason": reason})
        return None

    def _inline(self, data: bytes) -> tuple[str, str, tuple[int, int]] | str:
        """(media type, data: URI, pixels) for validated raster bytes, or the reason they are refused."""
        if len(data) > MAX_ASSET_BYTES:
            return f"larger than {MAX_ASSET_BYTES} bytes"
        mtype = raster_type(data)
        if mtype is None:
            return "not a PNG, JPEG, GIF or WebP raster (SVG, HTML and other files are never inlined)"
        px = _pixels(data)
        if px is None:
            return f"not a decodable {mtype} image"
        return mtype, f"data:{mtype};base64,{base64.b64encode(data).decode('ascii')}", px

    def __call__(self, src: str) -> str | None:
        src = (src or "").strip()
        if not src:
            return self._omit(src, "no image source")
        if src in self._cache:
            for c in self.consumed:
                if c["ref"] == src[:200]:
                    c["uses"] += 1
            return self._cache[src]
        m = _DATA.match(src)
        if m:
            meta, payload = m.group(1), m.group(2)
            if not meta.lower().rstrip().endswith(";base64"):
                return self._omit(src, "inline data: image that is not base64 raster data")
            try:
                data = base64.b64decode(re.sub(r"\s+", "", unquote(payload)), validate=True)
            except (binascii.Error, ValueError):
                return self._omit(src, "inline data: image that does not decode")
            got = self._inline(data)
            if isinstance(got, str):
                return self._omit(src, f"inline data: image {got}")
            mtype, uri, px = got
            self.consumed.append({"ref": src[:200], "source": "inline in the exhibit HTML",
                                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                                  "media_type": mtype, "pixels": list(px), "uses": 1})
            self._cache[src] = uri
            return uri
        if src.startswith(("//", "\\\\")) or _SCHEME.match(src):
            return self._omit(src, "network or file reference: never loaded")
        ref = unquote(src.split("#", 1)[0].split("?", 1)[0]).strip()
        if not ref or "\\" in ref or "\x00" in ref:
            return self._omit(src, "not a plain file name in the capture folder")
        norm = posixpath.normpath(ref)
        if ref.startswith("/") or norm == ".." or norm.startswith("../"):
            return self._omit(src, "path outside the capture folder")
        if "/" in norm or norm in (".", ff.ASSET_LEDGER) or norm.startswith("."):
            return self._omit(src, "not a captured asset of this exhibit (only files beside it are captured)")
        if not self.has_list:
            return self._omit(src, "the capture folder has no asset list for this exhibit")
        known = self.allowed.get(norm)
        if known is None:
            return self._omit(src, "not a captured asset of this exhibit")
        path = self.capture_dir / norm
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return self._omit(src, "the captured asset is no longer in the capture folder")
        if not real.is_relative_to(self.capture_dir):
            return self._omit(src, "symlink that escapes the capture folder")
        if not real.is_file():
            return self._omit(src, "not a regular file")
        try:
            with open(real, "rb") as fh:
                data = fh.read(MAX_ASSET_BYTES + 1)
        except OSError:
            return self._omit(src, "the captured asset could not be read")
        sha = hashlib.sha256(data).hexdigest()
        if sha != known.get("sha256"):
            return self._omit(src, "the file no longer has the SHA-256 recorded when it was captured")
        got = self._inline(data)
        if isinstance(got, str):
            return self._omit(src, got)
        mtype, uri, px = got
        self.consumed.append({"ref": src[:200], "name": norm, "sha256": sha, "bytes": len(data), "media_type": mtype,
                              "pixels": list(px), "source_url": known.get("source_url"), "uses": 1})
        self._cache[src] = uri
        return uri


def render_captured_exhibit(html_path: Path, pdf_path: Path, chrome: Path | None = None, *, run=None) -> str:
    """Render one captured SEC exhibit to pdf_path in isolation (module doc). Signature of the intake's render hook:
    (html_path, pdf_path, chrome). `run`: subprocess.run (tests intercept it). Returns the renderer label; writes
    <pdf>.render.json beside the PDF. Raises capture.fetch_filings.FetchError when Chrome writes no usable PDF."""
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    raw = html_path.read_bytes()
    resolver = AssetResolver(html_path.parent, html_path.name, ff.read_asset_ledger(html_path.parent))
    snapshot, counts = rs.sanitize_html(decode_html(raw), image_resolver=resolver)
    width_in, height_in = ff.paper_size_for_pixels(tuple(c["pixels"]) for c in resolver.consumed)
    try:
        used = rs.isolated_print(snapshot, pdf_path, width_in=width_in, height_in=height_in, chrome=chrome, run=run)
    except rs.CaptureFailed as e:
        raise ff.FetchError(f"Chrome failed to render {html_path.name}: {e}") from None
    record = {
        "renderer": RENDERER,
        "html_file": html_path.name,
        "html_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        "paper_in": [round(width_in, 4), round(height_in, 4)],
        "asset_list": ff.ASSET_LEDGER if resolver.has_list else None,
        "assets_consumed": resolver.consumed,
        "assets_omitted": resolver.omitted,
        "removed": counts,
        "limitations": rs.render_limitations(counts) + [LIMITATION],
    }
    (pdf_path.parent / (pdf_path.name + RENDER_RECORD_SUFFIX)).write_text(
        json.dumps(record, indent=1, sort_keys=True) + "\n")
    return ff.renderer_label(used) + rs.ISOLATED_LABEL + "; captured raster images inlined"
