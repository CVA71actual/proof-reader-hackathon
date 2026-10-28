# proof-reader
Page-cited, cross-checked extraction from messy PDFs using a team of Meta models. Meta Global AI Developer Hackathon 2026.

## S1: one filing page, end to end

One real SEC filing page goes all the way through:

1. **Capture** (`capture/capture_page.py`): checks the source PDF's SHA-256, cuts out page 95 of 144 of the
   Ionis 2026 annual report to shareholders (Consolidated Statements of Operations), and records every word on
   the page with its box in PDF points. Output: `fixtures/ions_ars_2026_p95.pdf`, `.words.json`, `MANIFEST.json`.
   The full filing is not kept in this repo.
2. **Read** (`capture/read_table.py`): sends the page's words to Muse Spark (Meta) and asks for the table as JSON,
   with a proposed box for each value. The exact instructions sent, their SHA-256 and the raw answer are saved with
   the reading. If the answer can't be understood it is saved as unreadable; nothing is guessed.
3. **Check** (`checks/`): two code checks run on every value:
   - *Matches source text*: the value appears in the page's own text.
   - *Citation located*: the words inside the proposed box read the same as the value.

   A value is **matched** only when both checks pass, **failed** if either fails, and otherwise stays **proposed**.
4. **View** (`viewer/build_viewer.py`): one HTML file with the table on the left and the page on the right, every
   proposed box drawn. Click a value to find its box; click a box to find its value.

### Run it

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m capture.capture_page --source /path/to/IONS_ARS_2026-04-23.pdf   # only to re-capture
.venv/bin/python -m capture.read_table --fake      # offline, clearly labelled FAKE reading
.venv/bin/python -m capture.read_table             # live Muse Spark reading (needs MODEL_API_KEY)
.venv/bin/python -m viewer.build_viewer            # writes viewer/out/index.html
.venv/bin/python -m pytest -q
```

The live reader uses `MODEL_API_KEY` from the environment or from `~/.config/proof-reader/env`
(`MODEL_API_KEY="..."`). The key is never printed or saved.

The fake reading is built by code from the words file, with three values changed on purpose so that failed and
proposed values show up in the viewer. It is always marked FAKE and is never shown as a model reading.

## R1: run.json, the contract for the screens

See [`runs/README.md`](runs/README.md): one `run.json` per run (never edited after the run ends), one append-only
`decisions.jsonl`, the schemas, a saved demo run, a sample run, and the state rule in `runs/state.py`.
