# D7/P3 validation-bar backfill

The script turns a broker K-line export into the bar history evolution
validation needs, from mid-April 2026 (warmup before the first trade on
2026-05-07) through the last completed session. It does not change the
multifold pipeline. It only prepares a separate data tree.

```bash
python scripts/backfill_bars.py --input broker_1m.csv --symbol TX00
python scripts/backfill_bars.py --input broker_1m.csv --symbol TX00 --write
```

## Safety

- Dry-run is the default. No file is created unless `--write` is passed.
- `--write` creates files only under `data/backfill/`. A target that
  lexically escapes that directory, or a symlink that resolves outside it,
  is refused. `data/live`, other `data/` children, `settings.yaml`,
  `taifex_closures.yaml`, state, votes, and lock files are never written.
- Symbols come from `SYMBOL_CONFIG` (TX00 / MTX00 / TMF00 and their
  prefixes). Bot names are rejected. The script reads no credentials.
- Exchange closures are marked and skipped. Missing bars are a failed
  validation report. The script does not forward-fill or synthesize bars.
- A failed validation writes nothing, including on `--write`.
- `--write` refuses to replace a file that already exists unless `--force`
  is also passed. `--force` does not allow a path outside `data/backfill/`,
  and a dry-run ignores it.

## Source

1. Pass a CSV from the broker's own historical K-line feed (`--input`).
   That is the same family of bars live warmup uses. This checkout does
   not open an SKCOM session.
2. `--cross-check` takes a TAIFEX official daily CSV and flags any
   day-session close that differs by more than 0.1%. Those rows are not
   copied into the output.
3. There is no third-party vendor source.

## Resolution and layout

Native bars are 1-minute. 15m / 1H / H4 are derived with
`aggregate_bars`, the same function live uses. `bars_1D.csv` is not
that aggregate. It is one bar per trading day in the broker daily
K-line layout (`kline_config.CACHE_SUFFIXES` `(4, 1)` → `_1D.csv`,
the same files as `data/TXF1_1D.csv` / `data/TMF1_1D.csv`):

- Header `datetime,open,high,low,close,volume` (no `res` column).
- Timestamp is 04:00 UTC on the trading day, written as
  America/New_York wall time: `YYYY/MM/DD 00:00` on that date during
  US daylight time, and `YYYY/MM/DD 23:00` on the previous date during
  US standard time.
- The bar holds the night session that opens on that trading day
  (15:00 TWT through 05:00 the next morning) plus the day session of
  the next trading day. Friday night is in Friday's bar together with
  Monday's day session. Sunday night is not in any daily bar. A
  trading day outside the requested span is omitted.

If the broker history is only 15-minute for the older span, pass
`--resolution 15m` (or let auto-detect choose it). Every intraday output
row is marked `res=15m`. The daily file has no `res` column. There is no
synthetic 1-minute file. Intrabar-sensitive checks must refuse that span.

```
data/backfill/closures.csv
data/backfill/TXF1/bars_1m.csv      # omitted when res=15m
data/backfill/TXF1/bars_15m.csv
data/backfill/TXF1/bars_1H.csv
data/backfill/TXF1/bars_H4.csv
data/backfill/TXF1/bars_1D.csv
data/backfill/TXF1/report.json
```

`data/` is gitignored. Swapping this tree into the live cache is a
separate deploy decision.

## Closures

`closures.csv` columns: `date,session,reason,source` with
`session=day|night` and `reason=holiday|typhoon|halt`.

A session is closed when any of these say so:

- the weekend and Taiwan public-holiday calendar in `src.market_data.holidays`
- a date in the operator file `taifex_closures.yaml` (a weekday halt;
  the file stays gitignored and is never required)
- the announcement list in `src/evolution/backfill.py`: 2026-09-26
  (typhoon), 2026-09-28, 2026-10-10, and 2026-10-11

The session key is the session **open** date. A night bar after midnight
belongs to the previous date. A holiday closes that date's day session
and the night session that **opens** on that date. The night that opens
on the previous weekday and ends on the holiday morning is still treated
as expected-open unless an announcement marks it. That treatment is
pending confirmation of the TAIFEX rule and is intentionally unchanged.

## Folds

The report's `fold_count` is how many 14-day windows fit between the
first and last kept bar, walked the same way as `analyze_folds` and
capped at its current limit of 10. A span from 2026-05-07 through
2026-10-23 yields at least 8 when trades occur through the span. Pass
`--require-folds 8` to fail a shorter pull. The default does not, so a
one-session check can still be written.
