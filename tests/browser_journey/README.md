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

The steps run in order in `test_main_journey.py` and share one server, one browser page and one research run. A step
the product blocks is marked `xfail(strict=True)` with the reason in its marker, so it fails loudly the day the
product fixes it (then drop the marker). The honesty rules (never "Kept by you" for automatic inclusion, never
"verified", an incomplete review never shown as "independently reviewed") are asserted on every captured screen
text and on both exports.

Nothing here edits product code; the fakes are injected only through `create_app`'s own parameters.
