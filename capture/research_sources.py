"""The research agent's fetch tools, captured by code (RESEARCHER-FULL-001 S1): fetch_url, fetch_edgar, fetch_ctgov.

Every fetch that returns a document is captured deterministically before the model sees a word of it:
    the exact bytes received   kept in the run folder (research/captures/<source_id>/original.<ext>), with SHA-256,
                               size, content type, requested and final URL, HTTP status and retrieval time
    rendered pages             HTML through the existing renderer (capture.fetch_filings.render_html_to_pdf, headless
                               Chrome; tests inject their own), a PDF as received, ClinicalTrials.gov JSON through a
                               code-written text rendering (render_json_pdf); then every page is cut, its words and
                               boxes extracted (capture.capture_page.page_words_bytes) exactly like the filing pages
    capture checks per page    capture_incomplete (capture.capture_source.assess_capture_completeness) and the hidden
                               or sub-2pt text test (HIDDEN_WORD_HEIGHT_PT, the selector's MIN_WORD_HEIGHT_PT)
There is no page cap on a fetched source. Search results and listings are leads, never evidence: fetch_edgar's
filing list and a ClinicalTrials.gov query return leads only, and a lead becomes evidence only after fetch_url (or
fetch_ctgov by NCT id) captures it.

Safety: http(s) only; no credentials in URLs; hosts that resolve to private, loopback, link-local or reserved addresses
are refused, on the first request and on every redirect; bodies are bounded by MAX_BYTES. Page text is data: nothing in
a fetched page is ever executed or treated as an instruction.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pymupdf

from capture.capture_page import page_words_bytes
from capture.capture_source import assess_capture_completeness, completeness_fields
from checks.selector_match import MIN_WORD_HEIGHT_PT

HIDDEN_WORD_HEIGHT_PT = MIN_WORD_HEIGHT_PT     # 2.0 pt: a word shorter than this is not printed where it seems
MAX_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_S = 180       # LIMITS-002 (CVA 2026-11-02); the SEC 10-Q took 62s in the free live proof
MAX_REDIRECTS = 5
WEB_USER_AGENT = "ProofReader-research/1.0 (+public-source capture; contact in SEC_USER_AGENT)"
CTGOV_STUDY = "https://clinicaltrials.gov/api/v2/studies/{nct}"
CTGOV_SEARCH = "https://clinicaltrials.gov/api/v2/studies?{query}"
SEC_HOSTS = ("sec.gov", "www.sec.gov", "data.sec.gov", "efts.sec.gov")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"


class FetchRefused(ValueError):
    """The URL may not be fetched (scheme, credentials, private address): a typed tool error, not a capture."""


class CaptureFailed(RuntimeError):
    """The bytes were received but could not be captured (rendered, cut into pages, hashed)."""


@dataclass
class Fetched:
    """One HTTP answer as received."""
    requested_url: str
    final_url: str
    status: int | None
    content_type: str | None
    body: bytes
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- URL safety


def _host_is_public(host: str, resolve: Callable[[str], list[str]] | None = None) -> bool:
    try:
        addrs = [host] if _is_ip(host) else (resolve or _resolve)(host)
    except OSError:
        return False
    if not addrs:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return False
    return True


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def check_url(url, resolve: Callable[[str], list[str]] | None = None) -> str:
    """The URL if it may be fetched, else FetchRefused with the plain reason."""
    if not isinstance(url, str) or not url.strip():
        raise FetchRefused("The URL is empty.")
    url = url.strip()
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise FetchRefused("Only http and https URLs are fetched.")
    if p.username or p.password or "@" in (p.netloc or ""):
        raise FetchRefused("A URL carrying credentials is never fetched.")
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        raise FetchRefused("Local and private hosts are never fetched.")
    if not _host_is_public(host, resolve):
        raise FetchRefused("The host does not resolve to a public address, so it is not fetched.")
    return url


def is_sec(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in SEC_HOSTS or host.endswith(".sec.gov")


# ---------------------------------------------------------------- the live fetcher


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolve):
        self.resolve = resolve
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise FetchRefused("Too many redirects.")
        check_url(newurl, self.resolve)     # every hop is checked again
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LiveFetcher:
    """GET with bounded size and time, redirect hops re-checked. SEC hosts get the SEC User-Agent and the 10/s rate
    limit (capture.fetch_filings.RateLimitedFetcher's rule); other hosts a plain research User-Agent."""

    def __init__(self, sec_user_agent: str | None = None, resolve=None):
        self.sec_user_agent = sec_user_agent
        self.resolve = resolve
        self._sec = None

    def _sec_ua(self) -> str:
        if self.sec_user_agent is None:
            from capture.fetch_filings import load_user_agent
            self.sec_user_agent = load_user_agent()
        return self.sec_user_agent

    def get(self, url: str) -> Fetched:
        url = check_url(url, self.resolve)
        if is_sec(url):
            import time
            from capture.fetch_filings import RATE_INTERVAL
            last = getattr(self, "_sec_last", 0.0)
            wait = RATE_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._sec_last = time.monotonic()
            ua = self._sec_ua()
        else:
            ua = WEB_USER_AGENT
        opener = urllib.request.build_opener(_CheckedRedirect(self.resolve))
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
        at = now_utc()
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT_S) as resp:
                body = resp.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise CaptureFailed(f"The document is larger than {MAX_BYTES // (1024 * 1024)} MB; not captured.")
                return Fetched(url, resp.geturl(), resp.status, resp.headers.get("Content-Type"), body, at)
        except urllib.error.HTTPError as e:
            return Fetched(url, e.geturl() or url, e.code, e.headers.get("Content-Type") if e.headers else None, b"", at)
        except urllib.error.URLError as e:
            raise CaptureFailed(f"The URL could not be reached ({type(e.reason).__name__}).") from None
        except (TimeoutError, socket.timeout):
            raise CaptureFailed("The site did not answer in time.") from None


# ---------------------------------------------------------------- rendering


def kind_of(content_type: str | None, body: bytes, url: str) -> str:
    """'pdf', 'html', 'json' or 'text' from the received bytes first, the declared type second."""
    ct = (content_type or "").split(";")[0].strip().lower()
    head = body[:1024].lstrip().lower()
    if body[:5] == b"%PDF-":
        return "pdf"
    if ct == "application/json" or head[:1] in (b"{", b"[") and ct in ("", "text/plain", "application/json"):
        return "json"
    if ct in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype html", b"<html")) or b"<html" in head:
        return "html"
    if ct.startswith("text/"):
        return "text"
    return "unknown"


def render_text_pdf(text: str, title: str) -> bytes:
    """A code-written PDF of plain text (Letter pages, 10pt Helvetica, lines wrapped at WRAP characters), so a JSON
    record or a plain-text document can be cut into pages and cited with word boxes like any filing page.
    Deterministic: same text, same bytes."""
    import textwrap
    width, height, margin, size, leading = 612, 792, 54, 10, 12
    per_page = int((height - 2 * margin) // leading)
    lines = []
    for raw in [title, ""] + text.splitlines():
        lines += textwrap.wrap(raw, WRAP, break_long_words=True, break_on_hyphens=False) or [""]
    doc = pymupdf.open()
    for start in range(0, max(len(lines), 1), per_page):
        page = doc.new_page(width=width, height=height)
        for n, line in enumerate(lines[start:start + per_page]):
            if line:
                page.insert_text((margin, margin + size + n * leading), line, fontsize=size, fontname="helv")
    doc.set_metadata({})
    out = doc.tobytes(garbage=4, deflate=True, no_new_id=True)
    doc.close()
    return out


WRAP = 95


def _flatten(value, path: str = "") -> list[str]:
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out += _flatten(v, f"{path}.{k}" if path else str(k))
        return out
    if isinstance(value, list):
        out = []
        for n, v in enumerate(value):
            out += _flatten(v, f"{path}[{n}]")
        return out
    return [f"{path}: {value}"]


def render_json_pdf(body: bytes, title: str) -> tuple[bytes, str]:
    """A registry record (ClinicalTrials.gov JSON) rendered by code as one 'field.path: value' line per field."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CaptureFailed("The registry answer is not valid JSON.") from None
    return render_text_pdf("\n".join(_flatten(data)), title), "code: capture.research_sources.render_json_pdf (field.path: value lines)"


# ---------------------------------------------------------------- isolated rendering of fetched web HTML
#
# A fetched web page is untrusted. It is never rendered as received: sanitize_html writes a controlled snapshot with
# no active content (script, iframe, frame, object, embed, applet, base, meta refresh, event-handler attributes,
# javascript: URLs) and no network or file subresources (img/srcset/source/video/audio/link/stylesheet @import and
# CSS url() references are removed; inline data: images stay). A Content-Security-Policy meta of default-src 'none'
# heads the snapshot, and Chrome runs with JavaScript off, all hostnames unresolvable and every request sent to a dead
# proxy (loopback included), in an empty temporary folder. Nothing the page names can be loaded while it renders.
# What was removed is recorded as a specific capture limitation (images not captured, stylesheets not applied, ...).
# The SEC exhibit path (capture.fetch_filings.render_html_to_pdf) is unchanged.

CSP = ("default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; script-src 'none'; "
       "frame-src 'none'; object-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'")
_DROP_WITH_CONTENT = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "template", "noembed"}
_DROP_TAG = {"base", "link", "source", "track", "portal"}
_URL_ATTRS = {"src", "srcset", "data", "poster", "background", "formaction", "action", "lowsrc", "dynsrc",
              "xlink:href", "ping", "manifest", "codebase", "archive", "longdesc", "usemap", "icon"}
_CSS_URL = __import__("re").compile(r"url\s*\(\s*(['\"]?)(?!data:)[^)]*\1\s*\)", __import__("re").I)
_CSS_URL_OPEN = __import__("re").compile(r"url\s*\(", __import__("re").I)
_CSS_IMPORT = __import__("re").compile(r"@import[^;]*;?", __import__("re").I)


CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
_CSS_ESCAPE = __import__("re").compile(r"\\(?:[0-9a-fA-F]{1,6}\s?|.)", __import__("re").S)


def _clean_css(css: str, counts: dict) -> str:
    if "\\" in css:          # CSS escapes (u\\72l(...)) are dropped, not decoded: the CSP blocks loads anyway
        counts["css_escapes_removed"] = counts.get("css_escapes_removed", 0) + len(_CSS_ESCAPE.findall(css))
        css = _CSS_ESCAPE.sub("", css)
    n_url = len(_CSS_URL.findall(css))
    n_imp = len(_CSS_IMPORT.findall(css))
    if n_url:
        counts["css_urls_removed"] = counts.get("css_urls_removed", 0) + n_url
    if n_imp:
        counts["css_imports_removed"] = counts.get("css_imports_removed", 0) + n_imp
    css = _CSS_URL.sub("none", _CSS_IMPORT.sub("", css))
    # Red Team b7bbc34: an UNCLOSED url( (url(file:///etc/passwd …) is not matched above. Any url( left that is not an
    # inline data: image drops the whole style text; the code-owned CSP stays as the second barrier.
    for m in _CSS_URL_OPEN.finditer(css):
        if not css[m.end():].lstrip(" \t\n'\"").lower().startswith("data:"):
            counts["css_dropped_unclosed_url"] = counts.get("css_dropped_unclosed_url", 0) + 1
            return ""
    return css


def sanitize_html(html_text: str) -> tuple[str, dict]:
    """(controlled snapshot, counts of what was removed). Text content is kept; nothing active or remote survives."""
    import html as _html
    from html.parser import HTMLParser

    counts: dict = {}
    out: list[str] = []

    def bump(k: str, n: int = 1) -> None:
        counts[k] = counts.get(k, 0) + n

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.skip = 0
            self.in_style = False
            self.hidden_tag, self.hidden_depth = None, 0     # an element the page hides (display:none, hidden)

        def _attrs(self, tag, attrs):
            kept = []
            for k, v in attrs:
                k = k.lower()
                v = v or ""
                if k.startswith("on"):
                    bump("event_handlers_removed")
                    continue
                if k in _URL_ATTRS and not (tag == "img" and k == "src" and v.strip().lower().startswith("data:")):
                    if tag == "img" and k in ("src", "srcset"):
                        continue          # counted once per image below
                    bump("subresource_references_removed")
                    continue
                if k == "href":
                    if tag != "a" or v.strip().lower().startswith(("javascript:", "data:", "vbscript:")):
                        bump("subresource_references_removed")
                        continue
                if k == "style":
                    v = _clean_css(v, counts)
                if k == "http-equiv":
                    bump("meta_http_equiv_removed")
                    return None
                kept.append((k, v))
            return kept

        def handle_starttag(self, tag, attrs, closed=False):
            tag = tag.lower()
            if self.hidden_tag is not None:
                if tag == self.hidden_tag and not closed:
                    self.hidden_depth += 1
                return
            amap = {k.lower(): (v or "") for k, v in attrs}
            style = amap.get("style", "").replace(" ", "").lower()
            if not self.skip and not closed and tag not in _DROP_TAG and tag not in ("br", "hr", "img", "input", "meta") \
                    and ("hidden" in amap or "display:none" in style or "visibility:hidden" in style):
                bump("hidden_elements_removed")       # text the page does not show is never captured as text
                self.hidden_tag, self.hidden_depth = tag, 1
                return
            if self.skip:
                if tag in _DROP_WITH_CONTENT:
                    self.skip += 1
                return
            if tag in _DROP_WITH_CONTENT:
                bump(f"{tag}_removed")
                if not closed:
                    self.skip = 1
                return
            if tag in _DROP_TAG:
                bump("stylesheets_not_applied" if tag == "link" else f"{tag}_removed")
                return
            if tag in ("img", "image", "picture", "video", "audio", "input") and tag != "picture":
                src = dict((k.lower(), v or "") for k, v in attrs).get("src", "")
                if tag == "img" and src.strip().lower().startswith("data:"):
                    pass
                elif tag in ("img", "image", "video", "audio") or (tag == "input" and
                                                                   dict(attrs).get("type", "").lower() == "image"):
                    bump("images_not_captured" if tag in ("img", "image", "input") else "media_not_captured")
                    alt = dict((k.lower(), v or "") for k, v in attrs).get("alt", "").strip()
                    out.append(f"[image not captured{': ' + _html.escape(alt) if alt else ''}]")
                    return
            kept = self._attrs(tag, attrs)
            if kept is None:
                return
            if tag == "style":
                self.in_style = True
            a = "".join(f' {k}="{_html.escape(v, quote=True)}"' for k, v in kept)
            out.append(f"<{tag}{a}{' /' if closed else ''}>")

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs, closed=True)

        def handle_endtag(self, tag):
            tag = tag.lower()
            if self.hidden_tag is not None:
                if tag == self.hidden_tag:
                    self.hidden_depth -= 1
                    if self.hidden_depth == 0:
                        self.hidden_tag = None
                return
            if self.skip:
                if tag in _DROP_WITH_CONTENT:
                    self.skip -= 1
                return
            if tag in _DROP_WITH_CONTENT or tag in _DROP_TAG or tag in ("img", "video", "audio"):
                return
            if tag == "style":
                self.in_style = False
            out.append(f"</{tag}>")

        def handle_data(self, data):
            if self.skip or self.hidden_tag is not None:
                return
            out.append(_clean_css(data, counts) if self.in_style else data)

        def handle_entityref(self, name):
            if not self.skip and self.hidden_tag is None:
                out.append(f"&{name};")

        def handle_charref(self, name):
            if not self.skip and self.hidden_tag is None:
                out.append(f"&#{name};")

        def handle_comment(self, data):
            pass

        def handle_decl(self, decl):
            out.append(f"<!{decl}>")

    parser = P()
    parser.feed(html_text)
    parser.close()
    # The code-owned policy always comes first, before any untrusted byte (an HTML parser hoists a leading meta
    # into the head), whatever the page holds: no head, a body that mentions the policy, anything.
    return CSP_META + "".join(out), counts


_BLOCK = __import__("re").compile(r"(?is)<\s*/?\s*(?:p|div|br|tr|li|h[1-6]|table|section|article|header|footer|"
                                 r"blockquote|pre|ul|ol|dl|dt|dd|hr|title)\b[^>]*>")
_CELL = __import__("re").compile(r"(?is)<\s*/\s*t[dh]\s*>")
_TAG = __import__("re").compile(r"(?s)<[^>]*>")
_STYLE_BLOCK = __import__("re").compile(r"(?is)<style\b.*?</style>")


def readable_text(sanitized: str) -> str:
    """The readable text of a sanitized snapshot: one line per block, table cells separated by ' | ', entities
    decoded, whitespace collapsed. Deterministic. Nothing is executed (scripts were removed by sanitize_html)."""
    import html as _html
    t = _STYLE_BLOCK.sub(" ", sanitized)
    t = _CELL.sub(" | ", t)
    t = _BLOCK.sub("\n", t)
    t = _TAG.sub(" ", t)
    t = _html.unescape(t).replace("\xa0", " ")
    lines = [" ".join(l.split()) for l in t.splitlines()]
    return "\n".join(l for l in lines if l and l.strip(" |"))


def render_limitations(counts: dict) -> list[str]:
    """Plain, specific capture limitations from what sanitize_html removed."""
    words = {"images_not_captured": "image(s) not captured (the rendered page shows a placeholder)",
             "media_not_captured": "audio/video element(s) not captured",
             "stylesheets_not_applied": "external stylesheet(s) not applied (layout may differ from the site)",
             "css_imports_removed": "stylesheet import(s) not applied", "css_urls_removed": "CSS resource URL(s) not loaded",
             "script_removed": "script(s) removed: content the site builds with JavaScript is not captured",
             "iframe_removed": "embedded frame(s) not captured", "frame_removed": "frame(s) not captured",
             "object_removed": "embedded object(s) not captured", "embed_removed": "embedded object(s) not captured",
             "hidden_elements_removed": "element(s) the page hides (display:none, hidden) not captured"}
    return [f"{n} {words[k]}" for k, n in sorted(counts.items()) if k in words and n]


# --blink-settings=scriptEnabled=false is NOT used: it stops headless Chrome from writing any PDF (exit 0, no file),
# which failed every web capture in the first live smoke run. --disable-javascript prints, and scripts are already
# removed by sanitize_html and refused by the code-owned CSP (script-src 'none').
ISOLATION_FLAGS = ("--disable-javascript", "--host-resolver-rules=MAP * ~NOTFOUND",
                   "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   "--disable-extensions", "--no-pings", "--disable-domain-reliability")


def chrome_render(html_path: Path, pdf_path: Path, *, run=None) -> str:
    """Render a fetched web page offline: sanitize it into an empty temporary folder, then print it with headless
    Chrome under ISOLATION_FLAGS. `run`: subprocess.run (tests intercept it). Returns the renderer label; the
    sanitized snapshot's limitations are written beside the PDF as <pdf>.limitations.json."""
    import subprocess
    from capture import fetch_filings as ff

    run = run or subprocess.run
    raw = html_path.read_text(encoding="utf-8", errors="replace")
    snapshot, counts = sanitize_html(raw)
    chrome = ff.find_chrome()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pr-research-render-") as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(snapshot, encoding="utf-8")
        printable = ff._print_html_with_page_size(page, ff.LETTER_WIDTH_IN, ff.LETTER_HEIGHT_IN)
        with tempfile.TemporaryDirectory(prefix="pr-research-chrome-", ignore_cleanup_errors=True) as profile:
            cmd = [str(chrome), "--headless", f"--user-data-dir={profile}", "--no-first-run",
                   "--no-default-browser-check", "--disable-gpu", "--no-pdf-header-footer", *ISOLATION_FLAGS,
                   f"--print-to-pdf={pdf_path}", printable.resolve().as_uri()]
            import time as _time
            started = _time.monotonic()
            try:
                run(cmd, check=True, capture_output=True, text=True, timeout=300)
            except subprocess.CalledProcessError as e:
                raise CaptureFailed(f"Chrome exited with code {e.returncode} after "
                                    f"{_time.monotonic() - started:.1f}s and wrote no usable PDF.") from None
            except subprocess.TimeoutExpired:
                raise CaptureFailed("Chrome did not finish rendering within 300s.") from None
    if not pdf_path.is_file() or pdf_path.stat().st_size < 64:
        raise CaptureFailed(f"Chrome exited normally after {_time.monotonic() - started:.1f}s but wrote no PDF "
                            f"(snapshot {len(snapshot)} characters).")
    (pdf_path.parent / (pdf_path.name + ".limitations.json")).write_text(
        json.dumps({"removed": counts, "limitations": render_limitations(counts)}, sort_keys=True) + "\n")
    return ff.renderer_label(chrome) + " (isolated: sanitized snapshot, no scripts, no network)"


# ---------------------------------------------------------------- per-page capture checks


def page_capture_checks(words_doc: dict) -> dict:
    """The capture checks run on every fetched page. capture_incomplete comes from the completeness record already in
    the words doc; the hidden-text test counts words shorter than HIDDEN_WORD_HEIGHT_PT. Results are 'pass', 'fail'
    or 'not_established' (completeness can be shown to fail, never shown complete)."""
    words = [w for w in words_doc.get("words", []) if not w.get("break")]
    tiny = [w["id"] for w in words if float(w["y1"]) - float(w["y0"]) < HIDDEN_WORD_HEIGHT_PT]
    if words_doc.get("capture_incomplete"):
        incomplete = {"result": "fail", "reason": words_doc.get("capture_incomplete_reason")}
    else:
        incomplete = {"result": "not_established", "reason": words_doc.get("completeness_reason")}
    if not words:
        hidden = {"result": "fail", "reason": "The page has no text words, so no quote on it can be checked.",
                  "hidden_word_ids": [], "words": 0}
    elif len(tiny) == len(words):
        hidden = {"result": "fail", "reason": "Every word on the page is shorter than 2pt: a hidden text layer.",
                  "hidden_word_ids": tiny[:200], "words": len(words)}
    elif tiny:
        hidden = {"result": "fail", "reason": f"{len(tiny)} of {len(words)} words are shorter than 2pt (hidden or "
                                              f"sub-2pt text); a quote on them is not printed where the page shows it.",
                  "hidden_word_ids": tiny[:200], "words": len(words)}
    else:
        hidden = {"result": "pass", "reason": "No word on the page is shorter than 2pt.", "hidden_word_ids": [],
                  "words": len(words)}
    return {"capture_incomplete": incomplete, "hidden_text": hidden,
            "hidden_threshold_pt": HIDDEN_WORD_HEIGHT_PT}


# ---------------------------------------------------------------- capture into the run folder


def cut_pdf(pdf_bytes: bytes, html_text: str | None = None) -> list[tuple[bytes, dict]]:
    """Every page of a PDF as (single-page PDF bytes, words doc with its completeness record). No page cap."""
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise CaptureFailed("The document is not a readable PDF.") from None
    pages = []
    try:
        with tempfile.TemporaryDirectory(prefix="pr-research-") as tmp:
            for i in range(doc.page_count):
                one = pymupdf.open()
                one.insert_pdf(doc, from_page=i, to_page=i)
                one.set_metadata({})
                raw = one.tobytes(garbage=4, deflate=True, no_new_id=True)
                one.close()
                tmp_pdf = Path(tmp) / f"p{i + 1}.pdf"
                tmp_pdf.write_bytes(raw)
                words = page_words_bytes(raw, i + 1)
                words.update(assess_capture_completeness(tmp_pdf, html_text=html_text))
                pages.append((raw, words))
    finally:
        doc.close()
    if not pages:
        raise CaptureFailed("The document has no pages.")
    return pages


def capture_document(fetched: Fetched, run_dir: Path, source_id: str, *,
                     render_html: Callable[[Path, Path], str] = chrome_render, title: str | None = None) -> dict:
    """Capture received bytes into run_dir. Returns {original_file, original_sha256, kind, rendered, pages:
    [(pdf_bytes, words_doc)]}. CaptureFailed when the bytes cannot become pages with words."""
    body = fetched.body
    if fetched.status is not None and not 200 <= fetched.status < 300:
        raise CaptureFailed(f"The site answered HTTP {fetched.status}; nothing to capture.")
    if not body:
        raise CaptureFailed("The site answered with no bytes.")
    kind = kind_of(fetched.content_type, body, fetched.final_url)
    ext = {"pdf": "pdf", "html": "html", "json": "json", "text": "txt"}.get(kind, "bin")
    folder = Path(run_dir) / "research" / "captures" / source_id
    folder.mkdir(parents=True, exist_ok=True)
    original = folder / f"original.{ext}"
    original.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    html_text = None
    failure, pre_cut = None, None
    if kind == "pdf":
        pdf_bytes, rendered = body, {"format": "pdf", "renderer": None}
    elif kind == "html":
        html_text = body.decode("utf-8", errors="replace")
        snapshot, removed = sanitize_html(html_text)
        text = readable_text(snapshot)
        (folder / "text.txt").write_text(text, encoding="utf-8")
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out = folder / "rendered.pdf"
        failure, pre_cut = None, None
        try:
            renderer = render_html(original, out)
            pdf_bytes = out.read_bytes()
            pre_cut = cut_pdf(pdf_bytes, html_text=html_text)    # the rendered pages must cut into pages with words
        except CaptureFailed as e:
            failure = str(e)
        except Exception as e:  # the renderer's own words: a render error carries no secret
            failure = f"{type(e).__name__}: {str(e)[:300]}"
        if failure is None:
            rendered = {"format": "html", "form": "rendered", "renderer": renderer, "text_sha256": text_sha,
                        "text_file": str((folder / "text.txt").relative_to(run_dir))}
            lim = out.parent / (out.name + ".limitations.json")
            if lim.is_file():
                rendered.update(json.loads(lim.read_text()))
            else:  # a renderer that does not sanitize records nothing, so say that the snapshot is not established
                rendered.update({"removed": None, "limitations": ["The renderer did not record what it removed."]})
        else:
            # CVA 2026-11-02: a page the model could read never dead-ends on our render step. Its readable text is
            # captured instead (set by code on plain pages, so quotes and word ids still work); the citation box is
            # not applicable and the render failure is a recorded limitation, never a pass.
            if not text.strip():
                raise CaptureFailed(f"The page could not be rendered ({failure}) and has no readable text.")
            pdf_bytes = render_text_pdf(text, title or fetched.final_url)
            out.write_bytes(pdf_bytes)
            rendered = {"format": "html", "form": "text", "renderer": "code: capture.research_sources.readable_text + "
                                                                       "render_text_pdf (text capture)",
                        "render_failure": failure, "text_sha256": text_sha,
                        "text_file": str((folder / "text.txt").relative_to(run_dir)), "removed": removed,
                        "visibility": "unknown",
                        "limitations": [f"Text capture: the page image was not rendered ({failure}); the citation "
                                        f"box cannot be checked.",
                                        "Visibility unknown: a text capture cannot tell text the site hides with its "
                                        "stylesheets from text it shows."] + render_limitations(removed)}
    elif kind == "json":
        pdf_bytes, renderer = render_json_pdf(body, title or fetched.final_url)
        (folder / "rendered.pdf").write_bytes(pdf_bytes)
        rendered = {"format": "json", "renderer": renderer}
    elif kind == "text":
        pdf_bytes = render_text_pdf(body.decode("utf-8", errors="replace"), title or fetched.final_url)
        (folder / "rendered.pdf").write_bytes(pdf_bytes)
        rendered = {"format": "text", "renderer": "code: capture.research_sources.render_text_pdf"}
    else:
        raise CaptureFailed(f"The content type {fetched.content_type or 'unknown'} cannot be captured as pages.")
    pages = pre_cut if kind == "html" and failure is None else cut_pdf(pdf_bytes, html_text=html_text)
    rendered["pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    return {"original_file": str(original.relative_to(run_dir)), "original_sha256": sha, "original_bytes": len(body),
            "kind": kind, "rendered": rendered, "pages": pages, "html_sha256": sha if kind == "html" else None,
            "completeness": [completeness_fields(w) for _, w in pages]}


# ---------------------------------------------------------------- block and interstitial pages (OUTCOME-001)

# A site that serves its maintenance page, a bot wall or an access-denied page to our fetcher has given us nothing:
# such a page is never a captured source (live brief-v2 run: jnj.com served "Site Maintenance" seven times and the
# model read it as "nothing material").
#
# OUTCOME-001, one principle (Operator ruling on Red Team #175 at 3ff7309): block wording is wording whose subject is
# the site, page, service or system itself ("the site is down", "this page is temporarily unavailable", "site
# maintenance", "we'll be back soon"), or an access refusal / bot wall. Never the bare word "maintenance": "patients
# are undergoing maintenance dosing" is a document. The final rule (Operator, Red Team #175 at 5a48646): a page is a
# block page when that wording is its title or h1, whatever its length; or when any prose sentence carries it and the
# page has fewer than BLOCK_OTHER_SENTENCES other prose sentences (a block notice plus one fact sentence is still a
# block notice; a long document mentioning site maintenance in passing is captured).
# Detection is a heuristic: a refused page is recorded with its reason, and the reviewer remains the backstop.
_re = __import__("re")
_SUBJECT = r"(?:web ?site|site|page|portal|server|service|system)s?"
_SITE_STATE = (rf"\b{_SUBJECT}\s+(?:(?:is|are|has been|have been|was|were|will be|may be|is being)\s+)?"
               r"(?:(?:currently|temporarily|now|briefly|still|being)\s+)*"
               r"(?:down|offline|unavailable|under (?:\w+ )?maintenance|undergoing (?:\w+ ){0,2}maintenance|"
               r"(?:being )?(?:updated|upgraded)|maintenance)\b")
_BOT_WALL = (r"\bwe(?:'|’)?ll be back\b|\b(?:please )?(?:check|come) back (?:later|tomorrow|soon|shortly)\b|\baccess denied\b|\brequest blocked\b|\b(?:access|request|you|your (?:\w+ )?(?:ip|address|access|request))\s+(?:has|have)\s+been blocked\b|"
             r"\bare you a robot\b|\bcaptcha\b|\bunusual traffic\b|\battention required\b|\bjust a moment\b|"
             r"\bchecking your browser\b|\benable javascript and cookies\b|\bplease switch to a different browser\b|"
             r"\b403 forbidden\b|\byou don(?:'|’)?t have permission to access\b|\bbot detection\b|"
             r"\bsecurity check\b|\bver\w{0,3}y (?:that )?you are (?:a )?human\b")
_BLOCK_WORDING = _re.compile(f"{_SITE_STATE}|{_BOT_WALL}")
BLOCK_OTHER_SENTENCES = 5
_HEADLINE = _re.compile(r"(?is)<(title|h1)\b[^>]*>(.*?)</\1\s*>")
_SENTENCE_BREAK = _re.compile(r"(?<=[.!?…])\s+")


def _block_hit(text: str) -> str | None:
    m = _BLOCK_WORDING.search(" ".join(text.lower().split()))
    return m.group(0) if m else None


def _prose_sentences(text: str) -> list[str]:
    """The page's prose: sentences (ending in . ! ? …) on its readable lines; navigation and link lists, which carry
    no sentence punctuation, are not prose."""
    out = []
    for line in text.splitlines():
        out += [p for p in _SENTENCE_BREAK.split(line.strip())        # linear: no backtracking on long lines
                if p.endswith((".", "!", "?", "…")) and len(p.split()) > 1]
    return out


def block_reason(fetched: "Fetched") -> str | None:
    """'site served a block/maintenance page (...)' when the answer is an interstitial rather than the document: block
    wording (see above) in the title or h1; or in any prose sentence of a page with fewer than BLOCK_OTHER_SENTENCES
    other prose sentences."""
    if fetched.status is not None and not 200 <= fetched.status < 300:
        return None        # an error status is reported as itself (403/404/451 could_not_access, others failed)
    if fetched.status is not None and fetched.status not in (200, 203):
        return f"site served a block/maintenance page (unusual status HTTP {fetched.status})"
    if kind_of(fetched.content_type, fetched.body, fetched.final_url) != "html":
        return None
    import html as _html
    sanitized = sanitize_html(fetched.body.decode("utf-8", errors="replace"))[0]
    text = readable_text(sanitized)
    if not text.strip():
        return "site served a block/maintenance page (no readable text)"
    for tag, inner in _HEADLINE.findall(sanitized):
        hit = _block_hit(_html.unescape(_TAG.sub(" ", inner)))
        if hit:
            where = "title" if tag.lower() == "title" else "headline"
            return f"site served a block/maintenance page (“{hit}” in the {where}, {len(text)} characters of text)"
    prose = _prose_sentences(text)
    hits = [h for h in map(_block_hit, prose) if h]
    if hits and len(prose) - len(hits) < BLOCK_OTHER_SENTENCES:
        return (f"site served a block/maintenance page (“{hits[0]}”, with {len(prose) - len(hits)} other prose "
                f"sentence{'s' if len(prose) - len(hits) != 1 else ''}, {len(text)} characters of text)")
    return None


# ---------------------------------------------------------------- SEC listing (leads) and ClinicalTrials.gov


def edgar_listing(submissions: dict, cik: str, forms: list[str], since: str, until: str) -> list[dict]:
    """Filing leads from the SEC submissions JSON: form, filing date, accession and the primary document URL. A lead
    is not evidence: the agent captures a document with fetch_url before it may cite it."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    wanted = {f.upper() for f in forms} if forms else None
    out = []
    for i, form in enumerate(recent.get("form") or []):
        base = str(form).split("/")[0].upper()
        if wanted and form.upper() not in wanted and base not in wanted:
            continue
        filed = (recent.get("filingDate") or [""])[i]
        if not (since <= filed <= until):
            continue
        acc = (recent.get("accessionNumber") or [""])[i]
        doc = (recent.get("primaryDocument") or [""] * (i + 1))[i] if recent.get("primaryDocument") else ""
        desc = (recent.get("primaryDocDescription") or [""] * (i + 1))[i] if recent.get("primaryDocDescription") else ""
        out.append({"form": form, "filing_date": filed, "accession": acc, "description": desc or None,
                    "url": ARCHIVE_DOC.format(cik=int(cik), acc_nodash=acc.replace("-", ""), doc=doc) if doc else None,
                    "index_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"})
    return out


def ctgov_search_url(query: str, page_token: str | None = None) -> str:
    q = {"query.term": query, "pageSize": "20",
         "fields": "NCTId,BriefTitle,OverallStatus,LeadSponsorName,PrimaryCompletionDate,LastUpdatePostDate"}
    if page_token:
        q["pageToken"] = page_token
    return CTGOV_SEARCH.format(query=urllib.parse.urlencode(q))


def ctgov_leads(body: bytes) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CaptureFailed("The registry answer is not valid JSON.") from None
    out = []
    for s in data.get("studies") or []:
        ps = (s or {}).get("protocolSection") or {}
        ident, status = ps.get("identificationModule") or {}, ps.get("statusModule") or {}
        sponsor = ((ps.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("name")
        out.append({"nct_id": ident.get("nctId"), "title": ident.get("briefTitle"),
                    "status": status.get("overallStatus"), "sponsor": sponsor,
                    "primary_completion": (status.get("primaryCompletionDateStruct") or {}).get("date"),
                    "last_update_posted": (status.get("lastUpdatePostDateStruct") or {}).get("date")})
    return out, data.get("nextPageToken")
