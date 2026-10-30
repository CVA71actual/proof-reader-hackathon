"""A picture page: a slide page whose text layer is hidden, so SAM 3.1 and an image reader rebuild its words.

Some filed slide decks (the PTGX exhibit 99.2) carry their words as a hidden text layer squashed into a strip well under
2 pt tall. The page shows the slide as a picture; the quote and citation checks, which compare a quote with the words,
cannot pass on it. The path here:
  1. is_picture_page(): the page's words are a hidden strip (most are shorter than HIDDEN_PT) or there are fewer than
     MIN_REAL_WORDS real words on it.
  2. SAM 3.1 (capture/sam.py) is asked for each concept in CONCEPTS ("text block", "title", "table", "bullet list"), one
     request each; merge_regions() keeps the boxes, largest first, dropping one that lies mostly inside a kept one.
     If SAM matches nothing, the whole page is the one region (sam.FALLBACK).
  3. Each region is cropped from the page image (the same crop as a scanned page) and sent to the image reader (Muse
     Spark Open, capture/second_reader.py) with the instruction to transcribe the region's text exactly.
  4. build_words() puts each transcription's words inside the region's box (lines spread evenly down the box, words
     spread along a line by their length) and writes a words file marked words_from "sam+image reader", kind "picture".
  5. The event reader then reads the page with those words and every check runs as usual. Each row's trail starts with
     "SAM 3.1 found N regions" (Model), "Transcribed region k" (Model), "Words rebuilt from the picture" (Code), and
     the page's source says the words came from the picture, not from the page's own text layer.
The words are an image reader's transcription placed by geometry, not printed text with true positions: that is why the
words file and the source say so. Everything here is pure (no model call); server/jobs.py makes the calls.
"""

from __future__ import annotations

import json

from capture import sam
from runs import prices, scan

WHO = "runs.picture"
CONCEPTS = ("text block", "title", "table", "bullet list")
HIDDEN_PT = 2.0            # a word shorter than this is not printed where the page shows it (a hidden text layer)
HIDDEN_SHARE = 0.8         # this share of the words shorter than HIDDEN_PT: the layer is a hidden strip
MIN_REAL_WORDS = 10        # fewer real (tall enough) words than this: the page is a picture
INSIDE = 0.8               # a region with this share of its area inside a kept, larger one is dropped
WORDS_FROM = "sam+image reader"
KIND = "picture"
SOURCE_NOTE = "words read from the picture by two Meta models; not the page's own text layer"
WHOLE_PAGE = "whole page"   # the concept of the one region code makes when SAM 3.1 found none
NO_REGION_NOTE = ("words read from the picture by an image reader (SAM 3.1 found no region on it, so the whole page was "
                  "read as one region); not the page's own text layer")
READER_NAME = "Muse Spark Open"
EMPTY_READ_REASON = "the image reader returned no text for this region"
ALL_EMPTY_NOTE = "words are the page's own text layer; SAM 3.1 and the image reader gave no text from the picture"


# ---------------------------------------------------------------- detecting a picture page


def real_words(words: list[dict]) -> list[dict]:
    return [w for w in words if w["y1"] - w["y0"] >= HIDDEN_PT]


def is_picture_page(words_doc: dict, min_real: int = MIN_REAL_WORDS) -> bool:
    """True when fewer than `min_real` of the page's words are tall enough to be printed (a hidden strip does not
    matter when the page also has that many real words)."""
    words = words_doc.get("words") or []
    real = len(real_words(words))
    return real < min_real      # judged on the count of real-height words, never on the share


# ---------------------------------------------------------------- regions


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def merge_regions(by_concept: dict[str, list[list[float]]], page_size: tuple[float, float] | None = None) -> list[dict]:
    """[{id, concept, box}] in PDF points from the boxes found per concept. The largest come first; a box with INSIDE of
    its area inside one already kept (a title inside the text block that holds it) is dropped, so no text is read twice.
    The whole-page answer (sam.FALLBACK) is used only when nothing finer was found. Reading order: top to bottom, then
    left to right; ids r1, r2, ... If SAM found nothing at all and `page_size` (width, height in points) is given, code makes
    the one region: the whole page (concept WHOLE_PAGE), and the trail and the source note say SAM found none."""
    kept: list[tuple[str, list[float]]] = []
    boxes = [(c, b) for c, bs in by_concept.items() if c != sam.FALLBACK for b in bs]
    for concept, box in sorted(boxes, key=lambda cb: -_area(cb[1])):
        if _area(box) <= 0 or any(scan._inter(box, k) / _area(box) >= INSIDE for _, k in kept):
            continue
        kept.append((concept, box))
    if not kept:
        kept = [(sam.FALLBACK, b) for b in by_concept.get(sam.FALLBACK, []) if _area(b) > 0][:1]
    if not kept and page_size:
        kept = [(WHOLE_PAGE, [0.0, 0.0, float(page_size[0]), float(page_size[1])])]
    kept.sort(key=lambda ck: (ck[1][1], ck[1][0]))
    return [{"id": f"r{i}", "concept": c, "box": b} for i, (c, b) in enumerate(kept, start=1)]


def needs_fallback(by_concept: dict[str, list[list[float]]]) -> bool:
    """True when no concept matched anything: the whole page is then asked for."""
    return not any(by_concept.get(c) for c in CONCEPTS)


def transcribe_prompt(region: dict) -> str:
    what = "the whole page" if region["concept"] in (sam.FALLBACK, WHOLE_PAGE) else f"one region of a slide (a {region['concept']})"
    return (f"The image is {what}. Transcribe all the text in the image exactly as printed: the same words, numbers, "
            f"punctuation and symbols, in reading order, one line of the image per line of your answer. Do not "
            f"summarize, correct, translate or add anything. Answer with a JSON object {{\"text\": \"...\"}} whose "
            f"text has the lines separated by \\n.")


def parse_transcription(raw: str | None) -> str | None:
    """The transcribed text from the reader's answer: the "text" of a JSON object, or the raw answer if it is not JSON.
    None when there is nothing."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        doc = json.loads(text)
        if isinstance(doc, dict):
            doc = doc.get("text")
        text = doc if isinstance(doc, str) else None
    except ValueError:
        pass
    return text if text and text.strip() else None


# ---------------------------------------------------------------- the words file


def region_words(text: str, box: list[float], block: int, first_id: int) -> list[dict]:
    """The words of one transcription placed inside `box`: each line of the text gets an equal band of the box's height;
    the words of a line share the box's width in proportion to their length. One word per whitespace-separated token."""
    x0, y0, x1, y1 = box
    lines = [ln.split() for ln in text.splitlines() if ln.split()]
    out: list[dict] = []
    band = (y1 - y0) / max(len(lines), 1)
    for li, tokens in enumerate(lines):
        total = sum(len(t) for t in tokens) + max(len(tokens) - 1, 0)       # a one-letter gap between words
        step = (x1 - x0) / total
        x = x0
        for wi, tok in enumerate(tokens):
            width = len(tok) * step
            out.append({"id": f"w{first_id + len(out)}", "text": tok, "x0": round(x, 2), "y0": round(y0 + li * band, 2),
                        "x1": round(x + width, 2), "y1": round(y0 + (li + 1) * band, 2), "block": block, "line": li,
                        "word": wi})
            x += width + step
    return out


def region_break(n: int, x: float, y: float, block: int) -> dict:
    """A zero-size, empty word between two regions' words: a quote is a run of consecutive words and never crosses it."""
    return {"id": f"w{n}", "text": "", "x0": x, "y0": y, "x1": x, "y1": y, "block": block, "line": 0, "word": 0,
            "break": True}


def build_words(page_width: float, page_height: float, page_number: int, transcriptions: list[dict]) -> dict:
    """The words file of a picture page. `transcriptions`: [{region: {id, concept, box}, text}] in reading order. The file
    has the shape of any words file, plus words_from, kind and the regions, so every reader of it can tell."""
    words: list[dict] = []
    regions = []
    for i, t in enumerate(transcriptions):
        got = region_words(t["text"] or "", t["region"]["box"], i, len(words) + (1 if words else 0))
        if words and got:
            x0, y0 = t["region"]["box"][:2]
            words.append(region_break(len(words), x0, y0, i))      # quote_match does not run across this
        words += got
        regions.append({**t["region"], "words": len(got)})
    return {"page_number": page_number, "page_width": page_width, "page_height": page_height, "units": "pt",
            "words_from": WORDS_FROM, "kind": KIND, "regions": regions, "words": words}


# ---------------------------------------------------------------- the trail


def trail_steps(regions: list[dict], transcriptions: list[dict], sam_calls: list[dict], reads: list[dict], n_words: int,
                at: str, n_rows: int, row_index: int = 0) -> list[dict]:
    """The steps every row of the page begins with, in the order they happened. Costs are split over the `n_rows` rows
    of the page (this is row number `row_index` of them), so the run's spend counts each call once."""
    n = max(n_rows, 1)
    images = sum(c["images"] or 0 for c in sam_calls)
    sam_known = all(c.get("cost_usd") is not None for c in sam_calls)
    sam_cost = sum(c["cost_usd"] for c in sam_calls) if sam_known else None
    found = [r for r in regions if r["concept"] != WHOLE_PAGE]
    listed = ("Regions: " + "; ".join(f"{r['id']} a {r['concept']} at {r['box']}" for r in found) if found else
              "No region matched \"text block\", \"title\", \"table\", \"bullet list\" or \"page\", so code made "
              f"one region of the whole page {regions[0]['box']}")
    steps = [{"step": f"{sam.NAME} found {len(found)} region{'s' if len(found) != 1 else ''}", "tag": "Model",
              "who": sam.NAME, "at": at,
              "text": (listed
                       + f" (PDF points). The page's text layer is a hidden strip, so it was not used. SAM 3.1 finds "
                         f"where things are; it reads nothing. {images} image{'s' if images != 1 else ''} processed "
                         f"({len(sam_calls)} concept requests)."),
              "tokens": None, "cost_usd": round(sam_cost / n, prices.ROUND_TO) if sam_known else None}]
    for i, (region, read) in enumerate(zip(regions, reads), start=1):
        usage = read.get("usage") or {}
        text = (transcriptions[i - 1]["text"] or "") if i <= len(transcriptions) else ""
        said = (f"Region {region['id']} ({region['concept']}), read from its image: “{text}”" if text else
                f"Region {region['id']} ({region['concept']}): {EMPTY_READ_REASON}.")
        steps.append({"step": f"Transcribed region {i}", "tag": "Model", "who": READER_NAME, "at": at, "text": said,
                      **prices.model_cost_fields(READER_NAME, *(_share(usage.get(k), n, row_index) for k in
                                                                ("prompt_tokens", "completion_tokens")))})
    kept = [r for r, t in zip(regions, transcriptions) if t["text"]]
    if not kept:
        steps.append({"step": "Words kept from the page's own text layer", "tag": "Code", "who": WHO, "at": at,
                      "text": f"The picture gave no text: {ALL_EMPTY_NOTE}."})
        return steps
    steps.append({"step": "Words rebuilt from the picture", "tag": "Code", "who": WHO, "at": at,
                  "text": f"{n_words} words, one per token of the transcriptions, placed inside their regions' boxes "
                          f"(marked words_from “{WORDS_FROM}”). {source_note(kept)}."})
    return steps


def source_note(regions: list[dict]) -> str:
    return NO_REGION_NOTE if any(r["concept"] == WHOLE_PAGE for r in regions) else SOURCE_NOTE


def _share(total, parts: int, index: int):
    return prices.split_tokens(total, parts)[index]
