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
