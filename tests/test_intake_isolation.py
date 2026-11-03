"""Codex decision 5a (ISOLATE-001, EVIDENCE-001, NEXT-CALLER-001): the intake's default renderer is the isolated one.

The real Preparer default renders a captured slide-deck exhibit, and a research capture renders a fetched page, both
through headless Chrome with the sanitizer, CSP and isolated launch: the slide that is only a captured image stays
visible, the wide slide is not clipped, and no script, file traversal, uncaptured image or network resource loads.
Local fixtures only: a fake SEC fetcher, a loopback server that records every request, and generated PNGs. No model
call. Tests that need a browser are skipped when Chrome is not installed; the resolver tests run everywhere."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pymupdf
import pytest

from capture import exhibit_render as er
from capture import fetch_filings as ff
from capture import research_sources as rs
from server import intake
from tests.test_ticker_intake import CRNX_CIK, TODAY, FakeFetcher, _archive, sec_samples

ACC = "0001658247-26-000010"
HTML_NAME = "crnx_ex99-1.htm"
SLIDE = "crnx_ex99-1img01.png"        # a slide that is only an image
WIDE = "crnx_ex99-1img02.png"         # a slide wider than Letter
EVIL_SVG = "crnx_ex99-1img03.svg"     # captured, but SVG: never inlined
RED, BLUE, GREEN, MAGENTA, YELLOW = (255, 0, 0), (0, 0, 255), (0, 200, 0), (255, 0, 255), (255, 255, 0)


def _chrome_or_none():
    try:
        return ff.find_chrome()
    except ff.FetchError:
        return None


needs_chrome = pytest.mark.skipif(_chrome_or_none() is None, reason="Google Chrome is not installed")


def png(w: int, h: int, rgb) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, rgb)
    return pix.tobytes("png")


class Loopback:
    """A local HTTP server that records every request; any request at all is a failure of isolation."""

    def __init__(self):
        hits = self.hits = []
        body = png(40, 40, YELLOW)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}/{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def loopback():
    lb = Loopback()
    yield lb
    lb.close()


def deck_html(lb: Loopback, secret: Path) -> bytes:
    return f"""<html><head><meta charset="utf-8"><title>Deck</title>
<script>document.title='SCRIPT RAN'; fetch('{lb.url("script")}');
document.addEventListener('DOMContentLoaded', () => document.body.insertAdjacentText('afterbegin', 'SCRIPT RAN'));</script>
<link rel="stylesheet" href="{lb.url("style.css")}">
<style>body {{ background-image: url({lb.url("bg.png")}); }}</style>
</head><body onload="fetch('{lb.url("onload")}')">
<p>Crinetics corporate deck, exhibit text.</p>
<div style="page-break-after:always"><img src="{SLIDE}" alt=""></div>
<div style="page-break-after:always"><img src="{WIDE}"></div>
<img src="../secret.png"><img src="stray.png"><img src="{EVIL_SVG}">
<img src="{lb.url("net.png")}"><img src="{secret.resolve().as_uri()}"><img src="//127.0.0.1:{lb.port}/proto.png">
<iframe src="{lb.url("frame")}"></iframe><object data="{lb.url("obj")}"></object>
</body></html>""".encode()


def colour_pixels(pdf: Path) -> dict:
    """Pixels per colour over every page of the PDF (rendered at 24 dpi)."""
    want = {"red": RED, "blue": BLUE, "green": GREEN, "magenta": MAGENTA, "yellow": YELLOW}
    out = dict.fromkeys(want, 0)
    with pymupdf.open(pdf) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=24)
            s, n = pix.samples, pix.n
            for i in range(0, len(s), n):
                px = (s[i], s[i + 1], s[i + 2])
                for k, rgb in want.items():
                    if all(abs(a - b) < 40 for a, b in zip(px, rgb)):
                        out[k] += 1
    return out


def pdf_text(pdf: Path) -> str:
    with pymupdf.open(pdf) as doc:
        return "\n".join(p.get_text() for p in doc)


# ---------------------------------------------------------------- the real Preparer default, end to end


@needs_chrome
def test_the_intake_default_renderer_keeps_captured_slides_and_loads_nothing_else(tmp_path, loopback):
    filings_root = tmp_path / "intake-docs"
    secret = filings_root / "secret.png"
    slide, wide, svg = png(1200, 675, RED), png(2400, 600, BLUE), (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400">'
        b'<rect width="400" height="400" fill="rgb(0,200,0)"/></svg>')
    html = deck_html(loopback, secret)
    samples = sec_samples()
    cbase = _archive(CRNX_CIK, ACC)
    samples[cbase + "index.json"] = json.dumps({"directory": {"item": [
        {"name": HTML_NAME}, {"name": SLIDE}, {"name": WIDE}, {"name": EVIL_SVG}]}}).encode()
    samples[cbase + HTML_NAME] = html
    samples[cbase + SLIDE] = slide
    samples[cbase + WIDE] = wide
    samples[cbase + EVIL_SVG] = svg
    cache = tmp_path / "company_tickers.json"
    cache.write_text("{}")
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(png(500, 500, GREEN))            # named by a file: URL in the exhibit
    planted = {}

    class PlantingFetcher(FakeFetcher):
        """When the exhibit is fetched its capture folder exists: put a file just outside it (../secret.png) and an
        uncaptured file inside it (stray.png), so the render meets both."""

        def get(self, url):
            if url.endswith(HTML_NAME) and not planted:
                cap = next(filings_root.rglob(ACC))
                (cap.parent / "secret.png").write_bytes(png(500, 500, GREEN))
                (cap / "stray.png").write_bytes(png(500, 500, MAGENTA))
                planted["dir"] = cap
            return super().get(url)

    fetcher = PlantingFetcher(samples)
    prep = intake.Preparer(fetcher=fetcher, filings_root=filings_root, today=lambda: TODAY, tickers_cache=cache,
                           background=False)
    assert prep.render is er.render_captured_exhibit                    # the real default, not a test stub
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    rec = intake.start(runs_dir, "CRNX", prep)
    t = rec["tickers"][0]
    assert t["state"] == "ready", t
    cap = planted["dir"]
    pdf = cap / "crnx_ex99-1.pdf"
    colours = colour_pixels(pdf)
    assert colours["red"] > 200, colours            # the image-only slide is visible
    assert colours["blue"] > 200, colours           # the wide slide is printed
    assert colours["green"] == 0 and colours["magenta"] == 0 and colours["yellow"] == 0, colours
    assert loopback.hits == []                      # no script, stylesheet, frame or image reached the network
    text = pdf_text(pdf)
    assert "Crinetics corporate deck" in text and "SCRIPT RAN" not in text

    # the wide slide is not clipped: the page is as wide as the slide and the whole image lies on the page
    with pymupdf.open(pdf) as doc:
        boxes = [(p.rect, pymupdf.Rect(i["bbox"])) for p in doc for i in p.get_image_info()]
    wide_boxes = [(r, b) for r, b in boxes if b.width > 1500]
    assert wide_boxes, boxes
    page_rect, box = wide_boxes[0]
    assert box.width == pytest.approx(2400 * 0.75, rel=0.02)
    assert box.x0 >= page_rect.x0 - 1 and box.x1 <= page_rect.x1 + 1 and box.y1 <= page_rect.y1 + 1

    # what was inlined and what was refused is recorded, by name and hash
    record = json.loads((cap / "crnx_ex99-1.pdf.render.json").read_text())
    consumed = {c["name"]: c["sha256"] for c in record["assets_consumed"]}
    assert consumed == {SLIDE: hashlib.sha256(slide).hexdigest(), WIDE: hashlib.sha256(wide).hexdigest()}
    reasons = {o["ref"]: o["reason"] for o in record["assets_omitted"]}
    assert "outside the capture folder" in reasons["../secret.png"]
    assert reasons["stray.png"] == "not a captured asset of this exhibit"
    assert "SVG" in reasons[EVIL_SVG]
    assert "network or file reference" in reasons[loopback.url("net.png")]
    assert "network or file reference" in reasons[secret.resolve().as_uri()]
    assert "network or file reference" in reasons[f"//127.0.0.1:{loopback.port}/proto.png"]
    assert record["paper_in"][0] >= 2400 / 96
    assert record["removed"]["script_removed"] == 1 and record["removed"]["iframe_removed"] == 1
    assert record["html_sha256"] == hashlib.sha256(html).hexdigest()

    # the fetcher's original HTML and assets are kept unchanged as evidence; nothing was written beside them to render
    assert (cap / HTML_NAME).read_bytes() == html
    assert (cap / SLIDE).read_bytes() == slide and (cap / WIDE).read_bytes() == wide and (cap / EVIL_SVG).read_bytes() == svg
    assert not [p.name for p in cap.iterdir() if p.name.startswith(".print_")]
    ledger = ff.read_asset_ledger(cap)
    assert ledger["exhibits"][HTML_NAME]["captured"] == sorted([SLIDE, WIDE, EVIL_SVG])
    assert {m["name"] for m in ledger["exhibits"][HTML_NAME]["missing"]} >= {"secret.png", "stray.png"}
    assert not [u for u in fetcher.urls if "127.0.0.1" in u]

    # the run's index carries the render record of every page of the exhibit
    index = json.loads((runs_dir / "intake" / rec["intake_id"] / intake.INDEX_FILE).read_text())
    entries = index["companies"]["CRNX"]["pages"]
    assert entries
    assert all(e["render_record"] == str((cap / "crnx_ex99-1.pdf.render.json").resolve()) for e in entries)
    assert all(e["render_record_sha256"] for e in entries)
    assert all(e["renderer"].endswith("captured raster images inlined") for e in entries)


# ---------------------------------------------------------------- a research capture through the same launch


@needs_chrome
def test_a_research_capture_shows_an_inline_slide_and_loads_nothing_named(tmp_path, loopback):
    outside = tmp_path / "outside.png"
    outside.write_bytes(png(400, 400, GREEN))
    (tmp_path / "run").mkdir()
    slide = base64.b64encode(png(900, 500, RED)).decode()
    page = f"""<html><head><script>fetch('{loopback.url("s")}');document.title='SCRIPT RAN'</script>
<link rel=stylesheet href="{loopback.url("c.css")}"><style>p{{background:url({loopback.url("bg")})}}</style></head>
<body onload="fetch('{loopback.url("o")}')"><p>Topline readout slide.</p>
<img src="data:image/png;base64,{slide}">
<img src="{loopback.url("x.png")}"><img src="../outside.png"><img src="{outside.as_uri()}">
<div style="background:url({outside.as_uri()}">unclosed</div><iframe src="{loopback.url("f")}"></iframe>
</body></html>""".encode()
    fetched = rs.Fetched("https://example.org/deck", "https://example.org/deck", 200, "text/html", page,
                         "2026-11-02T18:00:00+00:00")
    got = rs.capture_document(fetched, tmp_path / "run", "src-r1-deck")      # the default chrome_render
    assert got["rendered"]["form"] == "rendered", got["rendered"].get("render_failure")
    pdf = tmp_path / "run" / "research" / "captures" / "src-r1-deck" / "rendered.pdf"
    colours = colour_pixels(pdf)
    assert colours["red"] > 100 and colours["green"] == 0 and colours["yellow"] == 0, colours
    assert loopback.hits == []
    assert "SCRIPT RAN" not in pdf_text(pdf)
    assert got["rendered"]["removed"]["images_not_captured"] == 3


# ---------------------------------------------------------------- the resolver alone (no browser)


def _capture_folder(tmp_path, files: dict[str, bytes], captured=None, html=b"<p>x</p>") -> Path:
    cap = tmp_path / "PTGX" / ACC
    cap.mkdir(parents=True)
    (cap / HTML_NAME).write_bytes(html)
    for n, b in files.items():
        (cap / n).write_bytes(b)
    names = list(files) if captured is None else captured
    ff.write_asset_ledger(cap, {"assets": {n: {"sha256": hashlib.sha256(files[n]).hexdigest(), "bytes": len(files[n]),
                                               "source_url": f"https://www.sec.gov/x/{n}"} for n in names if n in files},
                                "exhibits": {HTML_NAME: {"captured": names, "missing": []}}})
    return cap


def test_the_resolver_inlines_only_captured_rasters_inside_the_folder(tmp_path):
    good = png(30, 20, RED)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png(30, 20, GREEN))
    cap = _capture_folder(tmp_path, {"a.png": good, "fake.png": b"<html><script>x</script></html>",
                                     "s.png": b'<svg xmlns="http://www.w3.org/2000/svg"/>', "changed.png": good,
                                     "notlisted.png": good}, captured=["a.png", "fake.png", "s.png", "changed.png"])
    (cap / "changed.png").write_bytes(png(30, 20, BLUE))                 # altered after capture
    os.symlink(outside, cap / "esc.png")                               # a symlink escaping the folder
    ledger = ff.read_asset_ledger(cap)
    ledger["assets"]["esc.png"] = {"sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}
    ledger["exhibits"][HTML_NAME]["captured"].append("esc.png")
    ff.write_asset_ledger(cap, ledger)
    (cap / "sub").mkdir()
    (cap / "sub" / "a.png").write_bytes(good)

    r = er.AssetResolver(cap, HTML_NAME, ff.read_asset_ledger(cap))
    assert r("a.png").startswith("data:image/png;base64,")
    assert r("./a.png?v=1").startswith("data:image/png;base64,")
    svg_data = "data:image/svg+xml;base64," + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
    for src in ("fake.png", "s.png", "changed.png", "notlisted.png", "esc.png", "sub/a.png", "../PTGX/" + ACC + "/a.png",
                "/etc/hosts", "%2e%2e/outside.png", "..\\outside.png", "file:///etc/hosts", "http://127.0.0.1/x.png",
                "https://www.sec.gov/a.png", "//evil/a.png", "javascript:alert(1)", svg_data, "data:image/png,abc",
                "captured_assets.json", ""):
        assert r(src) is None, src
    reasons = {o["ref"]: o["reason"] for o in r.omitted}
    assert "SVG" in reasons["fake.png"] and "SVG" in reasons["s.png"]
    assert "SHA-256" in reasons["changed.png"]
    assert reasons["notlisted.png"] == "not a captured asset of this exhibit"
    assert reasons["esc.png"] == "symlink that escapes the capture folder"
    assert "outside" in reasons["../PTGX/" + ACC + "/a.png"] and "outside" in reasons["%2e%2e/outside.png"]
    assert [c["name"] for c in r.consumed] == ["a.png", "a.png"]
    assert r.consumed[0]["sha256"] == hashlib.sha256(good).hexdigest() and r.consumed[0]["pixels"] == [30, 20]

    no_list = er.AssetResolver(cap, "other.htm", ff.read_asset_ledger(cap))
    assert no_list("a.png") is None and "no asset list" in no_list.omitted[0]["reason"]


def test_the_default_renderer_prints_a_sanitized_snapshot_from_an_empty_folder(tmp_path, monkeypatch):
    """Without a browser: the command line carries every isolation flag, the printed file sits alone in a temporary
    folder, and it holds the captured slide as inline bytes and nothing the exhibit named."""
    slide = png(2000, 500, RED)
    html = (f'<html><head><script>fetch("http://127.0.0.1:9/x")</script></head><body>'
            f'<img src="{SLIDE}"><img src="../secret.png"><img src="http://127.0.0.1:9/n.png"></body></html>').encode()
    cap = _capture_folder(tmp_path, {SLIDE: slide}, html=html)
    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setenv("CHROME_BIN", str(fake_chrome))
    monkeypatch.setattr(ff, "renderer_label", lambda chrome: "Fake Chrome headless print-to-pdf")
    seen = {}

    def intercept(cmd, **kw):
        seen["cmd"] = cmd
        printable = Path(cmd[-1].replace("file://", ""))
        seen["html"] = printable.read_text()
        seen["folder"] = sorted(p.name for p in printable.parent.iterdir())
        out = next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1]
        Path(out).write_bytes(rs.render_text_pdf("slide", "T"))

    label = er.render_captured_exhibit(cap / HTML_NAME, cap / "crnx_ex99-1.pdf", run=intercept)
    assert label.endswith("(isolated: sanitized snapshot, no scripts, no network); captured raster images inlined")
    assert set(rs.ISOLATION_FLAGS) <= set(seen["cmd"])
    assert seen["folder"] == [".print_page.html", "page.html"]
    h = seen["html"]
    assert base64.b64encode(slide).decode() in h and "Content-Security-Policy" in h and "default-src 'none'" in h
    assert "<script" not in h and "127.0.0.1" not in h and "secret" not in h and SLIDE not in h
    w = float(h.split("@page{size:", 1)[1].split("in", 1)[0])
    assert w == pytest.approx(2000 / 96 + 2 * ff.PRINT_MARGIN_IN, abs=0.01)       # wide-slide paper retained
    assert (cap / HTML_NAME).read_bytes() == html


def test_a_capture_without_a_fetcher_lists_no_assets(tmp_path):
    cap = tmp_path / "X" / ACC
    pages = ff.capture_exhibit(ticker="X", company="X", cik="1", form="8-K", filing_date="2026-10-01", accession=ACC,
                               html_name=HTML_NAME, html_bytes=b"<p>x</p>", dest_dir=cap, base_url="https://x/",
                               index_names=[], fetcher=None, chrome=None,
                               render=lambda h, p, c: (p.write_bytes(rs.render_text_pdf("x", "x")), "stub")[1],
                               filings_root=tmp_path)
    assert pages and "render_record" not in pages[0]
    assert ff.read_asset_ledger(cap)["exhibits"][HTML_NAME]["captured"] == []


# ---------------------------------------------------------------- Red Team PR181 at 149f182 (ISOLATE-001)

SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="http://127.0.0.1:9/svg"/></svg>'
HTML_BYTES = b"<html><script>fetch('http://127.0.0.1:9/html')</script></html>"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _rt1_pages() -> list[str]:
    """Red Team #1: SVG or HTML bytes labelled data:image/png, in every CSS spelling and in an img src."""
    out = []
    for bad in (SVG_BYTES, HTML_BYTES):
        uri = f"data:image/png;base64,{_b64(bad)}"
        out += [f'<div style="background:url({uri})">x</div>',
                f"<style>p{{background-image:url('{uri}')}}</style><p>x</p>",
                f'<div style="background-image:image-set(&quot;{uri}&quot; 1x)">x</div>',
                f"<style>p{{background:-webkit-image-set(url({uri}) 1x)}}</style><p>x</p>",
                f'<img src="{uri}">', f'<img srcset="{uri} 1x" src="{uri}">',
                f'<div style="background:url(data:image/svg+xml;base64,{_b64(SVG_BYTES)})">x</div>']
    return out


def _rt2_pages(host="127.0.0.1:9") -> list[str]:
    """Red Team #2: URLs written as plain strings inside image-set() and other CSS functions."""
    refs = (f"https://{host}/a.png", "file:///etc/passwd", f"//{host}/p.png", f"http://{host}/b.png")
    out = []
    for r in refs:
        for fn in ("image-set", "-webkit-image-set", "cross-fade", "src", "image"):
            out.append(f"<style>p{{background-image:{fn}(\"{r}\" 1x)}}</style><p>x</p>")
            out.append(f"<div style=\"background-image:{fn}('{r}' 1x)\">x</div>")
        out.append(f"<div style=\"background-image:image-set('ok.png' 1x, '{r}' 2x)\">x</div>")
    return out


@pytest.mark.parametrize("page", _rt1_pages())
def test_rt1_no_non_raster_bytes_survive_as_a_data_uri_anywhere(page):
    for resolver in (None, er.AssetResolver(Path("/nonexistent"), "x.htm", None)):
        snap, counts = rs.sanitize_html(page, image_resolver=resolver)
        body = snap[len(rs.CSP_META):]
        assert _b64(SVG_BYTES) not in body and _b64(HTML_BYTES) not in body, (page, body)
        assert "data:" not in body.lower(), (page, body)
        assert (counts.get("css_dropped_data_uri") or counts.get("images_not_captured")
                or counts.get("css_urls_removed")), counts


@pytest.mark.parametrize("page", _rt2_pages())
def test_rt2_no_url_string_survives_in_a_css_function(page):
    snap, counts = rs.sanitize_html(page)
    body = snap[len(rs.CSP_META):].lower()
    for bad in ("https:", "http:", "file:", "//127.0.0.1", "etc/passwd", "image-set", "cross-fade"):
        assert bad not in body, (page, body)
    assert counts.get("css_dropped_url_string") == 1, counts


def test_rt_a_real_raster_data_uri_in_css_is_kept():
    real = _b64(png(8, 8, RED))
    snap, counts = rs.sanitize_html(f'<div style="background:url(data:image/png;base64,{real})">x</div>'
                                    f"<style>p{{background:image-set(\"data:image/png;base64,{real}\" 1x)}}</style>")
    assert snap.count(real) == 2 and not counts.get("css_dropped_data_uri") and not counts.get("css_dropped_url_string")


@needs_chrome
def test_rt_the_default_renderer_loads_nothing_named_by_css_strings_or_labelled_data(tmp_path, loopback):
    """Both Red Team cases through the intake's default renderer and real Chrome: nothing reaches the loopback
    server, and the slide still shows."""
    slide = png(900, 500, RED)
    host = f"127.0.0.1:{loopback.port}"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" height="300" '
           f'fill="rgb(0,200,0)"/><image href="http://{host}/svg-inner"/></svg>').encode()
    pages = "".join(p for p in _rt2_pages(host))
    html = (f'<html><body><img src="{SLIDE}">'
            f'<div style="width:300px;height:300px;background:url(data:image/png;base64,{_b64(svg)})">g</div>'
            f'<div style="width:300px;height:300px;background-image:image-set(&quot;http://{host}/set&quot; 1x)">'
            f's</div>{pages}</body></html>').encode()
    cap = _capture_folder(tmp_path, {SLIDE: slide}, html=html)
    er.render_captured_exhibit(cap / HTML_NAME, cap / "out.pdf")
    colours = colour_pixels(cap / "out.pdf")
    assert loopback.hits == [] and colours["red"] > 100 and colours["green"] == 0 and colours["yellow"] == 0
    rec = json.loads((cap / "out.pdf.render.json").read_text())
    assert rec["removed"]["css_dropped_data_uri"] >= 1 and rec["removed"]["css_dropped_url_string"] >= 1
    assert any("not a decodable raster" in l for l in rec["limitations"])


# ---------------------------------------------------------------- Red Team PR181 at ae6e9f1: the decode check alone decides

def _spellings(uri: str) -> list[str]:
    return [f'url("{uri}")', f"url('{uri}')", f"url({uri})", f"url( \"{uri}\" )",
            f'image-set("{uri}" 1x)', f"-webkit-image-set('{uri}' 1x)", f'image-set(url("{uri}") 1x)',
            f"image-set(url({uri}) 1x)"]


@pytest.mark.parametrize("i", range(8))
@pytest.mark.parametrize("where", ["attr", "block"])
def test_rt3_a_real_png_survives_in_every_css_spelling_and_a_labelled_svg_never_does(i, where):
    real = f"data:image/png;base64,{_b64(png(6, 6, RED))}"
    fake = f"data:image/png;base64,{_b64(SVG_BYTES)}"
    for resolver in (None, er.AssetResolver(Path("/nonexistent"), "x.htm", None)):
        for uri, keep in ((real, True), (fake, False)):
            css = f"background-image:{_spellings(uri)[i]}"
            page = (f"<div style='{css}'>x</div>" if "'" not in css else f'<div style="{css.replace(chr(34), "&quot;")}">x</div>') \
                if where == "attr" else f"<style>p{{{css}}}</style><p>x</p>"
            snap, counts = rs.sanitize_html(page, image_resolver=resolver)
            body = snap[len(rs.CSP_META):]
            if keep:
                assert uri in body, (page, body, counts)
                assert not counts.get("css_urls_removed") and not counts.get("css_dropped_data_uri"), counts
            else:
                assert _b64(SVG_BYTES) not in body and "data:" not in body, (page, body)


# ---------------------------------------------------------------- Manus 05:27: the render record reaches the run source

def _intake_run(tmp_path, monkeypatch, tamper=None):
    """Producer (real capture_exhibit + the Preparer's default renderer, Chrome intercepted) -> intake index -> a run
    started from it through the API (fake reader) -> run.json sources. `tamper(cap)` runs between intake and run."""
    from fastapi.testclient import TestClient
    from server.app import create_app
    from tests.test_run_controls import PTGX_ONLY, FakeReader

    fake_chrome = tmp_path / "chrome"
    fake_chrome.write_text("")
    monkeypatch.setenv("CHROME_BIN", str(fake_chrome))
    monkeypatch.setattr(ff, "renderer_label", lambda chrome: "Fake Chrome headless print-to-pdf")
    real_print = rs.isolated_print

    def intercept(cmd, **kw):          # never executes a browser
        out = next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1]
        Path(out).write_bytes(rs.render_text_pdf("Crinetics announces a Phase 3 readout in 2027.", "T"))
    monkeypatch.setattr(rs, "isolated_print", lambda *a, **kw: real_print(*a, **{**kw, "run": intercept}))

    slide = png(300, 200, RED)
    samples = sec_samples()
    cbase = _archive(CRNX_CIK, ACC)
    samples[cbase + "index.json"] = json.dumps({"directory": {"item": [{"name": HTML_NAME}, {"name": SLIDE}]}}).encode()
    samples[cbase + HTML_NAME] = (f'<html><body><p>Crinetics announces a Phase 3 readout in 2027.</p>'
                                  f'<img src="{SLIDE}"><img src="uncaptured.png"></body></html>').encode()
    samples[cbase + SLIDE] = slide
    cache = tmp_path / "company_tickers.json"
    cache.write_text("{}")
    prep = intake.Preparer(fetcher=FakeFetcher(samples), filings_root=tmp_path / "intake-docs", today=lambda: TODAY,
                           tickers_cache=cache, background=False)
    assert prep.render is er.render_captured_exhibit
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    client = TestClient(create_app(runs_dir=runs_dir, ui_dir=runs_dir / "no-ui", reader=FakeReader(),
                                   background=False, intake_preparer=prep))
    r = client.post("/api/intake", json={"tickers": "CRNX"})
    assert r.status_code == 202, r.text
    iid = r.json()["intake_id"]
    cap = next((tmp_path / "intake-docs").rglob(ACC))
    if tamper:
        tamper(cap)
    r = client.post("/api/runs/new", json={**PTGX_ONLY, "companies": ["CRNX"], "intake_id": iid})
    assert r.status_code == 202, r.text
    run = json.loads((runs_dir / r.json()["id"] / "run.json").read_text())
    srcs = [s for s in run["sources"] if s.get("rendered_from")]
    assert srcs, run["sources"]
    return run, srcs, slide


def _report_texts(run):
    import io
    from docx import Document
    from pypdf import PdfReader
    from report.data import build_view
    from report.docx_report import build_docx
    from report.pdf_report import build_pdf
    view = build_view(run, [], generated_at="2026-11-03T12:00:00+00:00")
    pdf = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(build_pdf(view))).pages)
    docx = "\n".join(p.text for p in Document(io.BytesIO(build_docx(view))).paragraphs)
    return view, " ".join(pdf.split()), docx


def _schema_ok(run):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[1] / "runs" / "schema" / "run.schema.json").read_text())
    errors = sorted(Draft202012Validator(schema).iter_errors(run), key=str)
    assert not errors, [e.message for e in errors[:3]]


def test_an_uncaptured_image_omission_reaches_the_run_source_and_the_report(tmp_path, monkeypatch):
    run, srcs, slide = _intake_run(tmp_path, monkeypatch)
    _schema_ok(run)
    for src in srcs:
        rf = src["rendered_from"]
        rec = rf["render_record"]
        assert rec["status"] == "verified" and rec["file"] == "crnx_ex99-1.pdf.render.json" and len(rec["sha256"]) == 64
        assert rec["assets_consumed"] == [{"name": SLIDE, "sha256": hashlib.sha256(slide).hexdigest(),
                                           "bytes": len(slide), "media_type": "image/png"}]
        assert rec["assets_omitted"] == [{"ref": "uncaptured.png", "reason": "not a captured asset of this exhibit"}]
        assert "1 image(s) not captured (the rendered page shows a placeholder)" in rf["limitations"]
        assert ("Image not shown on the rendered page: uncaptured.png (not a captured asset of this exhibit)."
                in rf["limitations"])
    view, pdf, docx = _report_texts(run)
    want = "Render limitation: Image not shown on the rendered page: uncaptured.png (not a captured asset of this exhibit)."
    assert want in docx and want in pdf
    assert any(want.split(": ", 1)[1] in r["render_limitations"] for r in view["sources"])
    js = (Path(__file__).resolve().parents[1] / "ui" / "app.js").read_text()
    assert js.count("rendered_from.limitations || []") == 2       # the reader shows them on both source panels


def test_a_changed_render_record_becomes_an_explicit_limitation_never_silent(tmp_path, monkeypatch):
    def tamper(cap):
        p = cap / "crnx_ex99-1.pdf.render.json"
        doc = json.loads(p.read_text())
        doc["assets_omitted"] = []                     # someone hides the omission after capture
        doc["limitations"] = []
        p.write_text(json.dumps(doc))
    run, srcs, _ = _intake_run(tmp_path, monkeypatch, tamper=tamper)
    _schema_ok(run)
    for src in srcs:
        rf = src["rendered_from"]
        assert rf["render_record"]["status"] == "changed"
        assert rf["render_record"]["assets_omitted"] == [] and rf["render_record"]["assets_consumed"] == []
        assert len(rf["limitations"]) == 1 and rf["limitations"][0].startswith("Render record changed since capture")
        assert "not known" in rf["limitations"][0]
    _, pdf, docx = _report_texts(run)
    assert "Render limitation: Render record changed since capture" in docx
    assert "Render limitation: Render record changed since capture" in pdf


def test_a_missing_render_record_becomes_an_explicit_limitation(tmp_path, monkeypatch):
    run, srcs, _ = _intake_run(tmp_path, monkeypatch,
                               tamper=lambda cap: (cap / "crnx_ex99-1.pdf.render.json").unlink())
    _schema_ok(run)
    for src in srcs:
        assert src["rendered_from"]["render_record"]["status"] == "unavailable"
        assert src["rendered_from"]["limitations"][0].startswith("Render record unavailable")
