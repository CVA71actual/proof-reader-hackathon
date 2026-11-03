# report — PDF and Word from saved results (M1)

```sh
python -m report.build <run_id>
python -m report.build <run_id> --out DIR   # tests only
```

Writes `biotech-coverage-catalyst-calendar-run<N>.pdf` and `.docx` from that run's
`run.json` + `decisions.jsonl` + `runs.state.current_state` only. No model calls.
No network. Default output: `runs/<id>/report/`. A later build never overwrites a
saved report there (past runs keep their file). `--out` always writes, for tests.

Libraries: reportlab, python-docx (pinned in `requirements.txt`).

Order (EXPORT-LAYOUT-001, CVA 2026-11-03), the same in both files: title and one run line (run number, companies,
run date), the run's review label; **Calendar** (page 1) with the Summary counts under it; Needs your review;
Undated, Later, Context (past); then **Appendix: research log and sources** (the run record and research log that
used to open the report, evidence and checks, corrections, edits, exclusions, sources). The footer of a research run
names the models that run used, from its record (`Research: … · Review: … [· Adjudication: …] · Built by code from
the saved results`); other runs keep `Built with Meta Muse models · built by code from the saved results`.
