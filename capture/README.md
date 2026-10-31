# capture/

## 14-name SEC capture (Job 46)

EDGAR only, no key, no model calls. Filings are stored outside the repo under `~/proof-reader-docs/filings/`; only the
code and the two index files are committed.

    python -m capture.fetch_filings --tickers ACAD,AGIO,... --days 365 --until 2026-10-13 \
        --forms 8-K,10-K,10-Q,S-1,424B --out ~/proof-reader-docs/filings/fourteen-1013 --index /tmp/a.json
    python -m capture.fetch_filings --tickers ACAD,AGIO,... --days 365 \
        --forms 8-K,10-K,10-Q,S-1,424B --out ~/proof-reader-docs/filings/fourteen-now --index /tmp/b.json
    python -m capture.index_filings --tickers ACAD,AGIO,... --filings-root <the --out dir> --out runs/sources_index.<set>.json

- Tickers outside the built-in twelve are looked up in SEC `company_tickers.json` (cached at `~/proof-reader-docs/company_tickers.json`).
- `--until YYYY-MM-DD` keeps filings with `filing_date <= until`; the `--days` window counts back from `until` (or from today).
- `--forms` defaults to `8-K` (the twelve pipeline, unchanged). 8-Ks give their EX-99.* exhibits only; 10-K, 10-Q, S-1 and
  424B* also give the primary document (exhibit `main`) plus any EX-99.* exhibits.
- Each accession folder gets a `filing.json` (form, date, document list); each ticker folder a `submissions.json`.
  `index_filings` reads these, so form, date and exhibit labels are exact.
- A document that fails to fetch or render is printed and listed at the end; the rest carry on.

### Workout vs full indexes (Job 46b)

`index_filings --until D --out X.json --full-out X.full.json` writes two indexes and prints a per-ticker table
(filings, exhibits, workout pages, full pages). The workout index (`--out`) holds 8-K / 8-K/A exhibit pages (Ex. 99.x)
filed within `--exhibit-days` (default 180) before `D`, and no 10-K/10-Q/S-1/424B body pages. The `.full.json` has everything,
so reading the bodies is a choice, not a loss. Without `--until`/`--full-out` the tool behaves as before.
Committed: `runs/sources_index.fourteen-1013{,.full}.json` (cutoff 13 Oct 2026) and `runs/sources_index.fourteen-now{,.full}.json` (cutoff 31 Oct 2026).
