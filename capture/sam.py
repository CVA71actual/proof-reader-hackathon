"""SAM 3.1: region proposals for a page that has no text layer.

The Meta Model API's Responses route (https://api.meta.ai/v1, model `sam-3.1`), streaming, one user message with two
parts: an `input_text` that is a CONCEPT (a short noun phrase naming one thing: "table", "block of text") and an
`input_image` (a base64 data URL). It answers with one line of comma-separated records
    <0f>N<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|><|mask;x=0;y=0;data=...|>
where w and h are the image's pixel size and x2, y2 are inclusive; an empty answer means no match, not an error. One
request per concept. Masks are ignored: boxes are enough. The call is priced per image processed (runs/prices.py).

SAM 3.1 is an object segmenter, not a document layout model: on the PTGX text page and the IONS financial-statement
page it matched nothing for "table", "block of text", "paragraph", "line of text", "grid", "row" and others, and answered
only for "page" and "document" (one box, the whole page). So when "table" and "block of text" both match nothing,
the run asks for "page" and the one region is the whole page (see runs/scan.py).

SAM finds where things are; it reads nothing. A region it returns is cropped and sent to the reader, and the reader's
box is then checked against the region (checks/selector_match.py region_match). The key is MODEL_API_KEY from the env
loader, never printed.

    python -m capture.sam      one live segmentation of the pinned PTGX page, rendered with no text layer, for
                               "table" and "block of text"; saves fixtures/ptgx_8k_2026-08-05_ex991_p3.sam.json
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

from capture import read_events, read_table
from capture.capture_source import FIXTURES, load_entry
from capture.reader import NO_KEY, Reply

NAME = "SAM 3.1"
MODEL = "sam-3.1"
CONCEPTS = ("table", "block of text")
FALLBACK = "page"     # asked only when neither concept matched: the answer is the whole page, one region
SAM_PATH = FIXTURES / f"{read_events.PIN}.sam.json"

_BOX = re.compile(r"<\|box;x1=(-?\d+);y1=(-?\d+);x2=(-?\d+);y2=(-?\d+);w=(\d+);h=(\d+)\|>")


def parse_boxes(text: str | None) -> list[dict]:
    """The boxes in a SAM answer, in the order given: [{x1, y1, x2, y2, w, h}] in image pixels (x2, y2 inclusive).
    An empty or box-less answer is no match."""
    out = []
    for m in _BOX.finditer(text or ""):
        x1, y1, x2, y2, w, h = (int(v) for v in m.groups())
        if w > 0 and h > 0 and x2 >= x1 and y2 >= y1:
            out.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "h": h})
    return out


def to_points(box: dict, page_width_pt: float, page_height_pt: float) -> list[float]:
    """A SAM box in image pixels -> [x0, y0, x1, y1] in PDF points (the inclusive far edge counts its last pixel)."""
    sx, sy = page_width_pt / box["w"], page_height_pt / box["h"]
    return [round(box["x1"] * sx, 2), round(box["y1"] * sy, 2),
            round((box["x2"] + 1) * sx, 2), round((box["y2"] + 1) * sy, 2)]


class SamReader:
    name = NAME
    model = MODEL

    def _key(self) -> str:
        read_table.load_env_file()
        return os.environ.get("MODEL_API_KEY", "").strip()

    def ready(self) -> str | None:
        return None if self._key() else NO_KEY

    def segment(self, image: bytes, concept: str) -> Reply:
        """One request for one concept. The Reply's raw is the answer line; `images` is the images processed."""
        from openai import OpenAI  # imported here so tests and offline use need no network client

        uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
        client = OpenAI(base_url=read_table.BASE_URL, api_key=self._key())
        try:
            stream = client.responses.create(model=MODEL, stream=True, input=[{"role": "user", "content": [
                {"type": "input_text", "text": concept}, {"type": "input_image", "image_url": uri}]}])
            parts, images = [], 1
            for event in stream:
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    parts.append(getattr(event, "delta", "") or "")
                elif kind == "response.completed":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    images = getattr(usage, "images_processed", None) or images
        except Exception as e:
            return Reply(None, MODEL, error=read_table._plain_error(e), images=0)
        return Reply("".join(parts), MODEL, images=images,
                     extra={"base_url": read_table.BASE_URL, "requested_model": MODEL, "concept": concept})


def main(argv: list[str] | None = None) -> int:
    from runs import scan

    sam = SamReader()
    problem = sam.ready()
    if problem:
        print(problem)
        return 2
    entry = load_entry(read_events.PIN)
    png = scan.scanned_png(entry)
    found = {}
    for concept in CONCEPTS + (FALLBACK,):
        if concept == FALLBACK and any(v["boxes"] for v in found.values()):
            break
        reply = sam.segment(png, concept)
        if reply.error:
            print(f"SAM 3.1 call for {concept!r} did not succeed ({reply.error}).")
            return 1
        found[concept] = {"boxes": parse_boxes(reply.raw), "images": reply.images,
                          "answer_chars": len(reply.raw or ""), "starts_with": (reply.raw or "")[:60]}
        print(f"{concept!r}: {len(found[concept]['boxes'])} boxes, {reply.images} image processed")
    from runs import prices
    rec = {"model": MODEL, "base_url": read_table.BASE_URL, "input": "page image rendered with no text layer",
           "image_sha256": scan.sha256(png), "concepts": found,
           "images_processed": sum(c["images"] for c in found.values()),
           "cost_usd_at_placeholder_price": prices.image_cost_usd(NAME, sum(c["images"] for c in found.values()))}
    SAM_PATH.write_text(json.dumps(rec, indent=1) + "\n")
    print(f"saved -> {SAM_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
