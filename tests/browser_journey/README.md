# Browser journey (real screens, fake models)

An automated acceptance test of Proof Reader's main journey, driven through the real `ui/` screens in headless
Chromium (Playwright for Python) against the real `server/app.py` started on a free port over a temporary runs
folder. The research job runs with the fakes of `tests/research_fakes.py` (scripted researcher, reviewer,
adjudicator, Brave Search, fetcher, renderer) and the offline SEC intake preparer of `tests/test_ticker_intake.py`.
No model, network or paid call is made.

Run it from the repo root:

```bash
pip install -r requirements.txt playwright==1.56.0
python -m pytest tests/browser_journey -p no:cacheprovider -q
```

The browser binary is found from `PROOF_READER_CHROME`, Playwright's own download (`playwright install chromium`),
the chromium folders under `PLAYWRIGHT_BROWSERS_PATH` or `/opt/pw-browsers`, or chromium/google-chrome on PATH.
Without one the journey is skipped with a sentence saying so.

The steps run in order in `test_main_journey.py` and share one server, one browser page and one research run,
started by the New Run screen's real Run button (NR-001: the screen sends `mode: "research"`; the test reads the
request off the browser and never intercepts or rewrites it). A step the product blocks is marked
`xfail(strict=True)` with the observed reason in its marker, so it fails loudly the day the product fixes it (then
drop the marker). Today: none. The four earlier markers are gone: 2b and 4a pass (the run card shows "review:
reviewing"; the judgment pane shows what the reviewer found), and 3a (a row the reviewer assessed `no_issue` in a
review that found something elsewhere must carry the reviewed label) and 7c (the calendar screen's row must show the
current, post-Fix title) are normal regressions for the product fixes in `runs/research_labels.py` and `ui/app.js`
calRow (Codex PR183 items 1 and 2). They fail on a build without those fixes.

What the exports step asserts (7): the summary counts on the PDF and the Word file match the API's counts and the
Export screen; and every event row of each export, with the PDF's calendar table parsed by column (pypdf layout
mode) and the Word table by cell, carries exactly the row's current title (a person's fix applied), its effective
timing, its inclusion label and the person's decision label, with "Kept by you" required on the kept row and
rejected on the automatically included row. The expected values come from the API's state, never from the report's
own view. Two mutation negatives (7a, 7b) feed the real renderers (`report/pdf_report.py`, `report/docx_report.py`)
a deliberately wrong view, "Kept by you" on the automatic row and the stale pre-Fix title, and show the per-row
check fails on each export.

The review-stage step (2b) reads the review-progress element on the PTGX line of the run card ("review:
reviewing", optionally with the reviewer's name) and accepts only a wording that says the review is running now;
"not independently reviewed", "review completed", "review: done" and the research stage word "Finished" are
contrast controls the predicate must reject (`test_2b_contrast_…`). The review row's step (4) asserts the queue
shows the actual reviewer issue ("The reviewer found …") and the pane shows the finding text.

The honesty rules (never "Kept by you" for automatic inclusion, never "verified", an incomplete review never shown
as "independently reviewed") are asserted on every captured screen text and on both exports.

Nothing here edits product code; the fakes are injected only through `create_app`'s own parameters.
