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

## Source

1. Pass a CSV from the broker's own historical K-line feed (`--input`).
   That is the same family of bars live warmup uses. This checkout does
   not open an SKCOM session.
2. `--cross-check` takes a TAIFEX official daily CSV and flags any
   day-session close that differs by more than 0.1%. Those rows are not
   copied into the output.
3. There is no third-party vendor source.

## Resolution and layout

Native bars are 1-minute. 15m / 1H / H4 / 1D are derived with
`aggregate_bars`, the same function live uses. The 1D file is one bar
per session (day and night), not the TAIFEX official daily print.

If the broker history is only 15-minute for the older span, pass
`--resolution 15m` (or let auto-detect choose it). Every output row is
marked `res=15m`. There is no synthetic 1-minute file. Intrabar-sensitive
checks must refuse that span.

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
and the night session that **opens** on that date. The previous date's
night, which ends on the holiday morning, is still expected unless an
announcement marks it.

## Folds

The report's `fold_count` is how many 14-day windows fit between the
first and last kept bar, walked the same way as `analyze_folds` and
capped at its current limit of 10. A span from 2026-05-07 through
2026-10-23 yields at least 8 when trades occur through the span. Pass
`--require-folds 8` to fail a shorter pull. The default does not, so a
one-session check can still be written.
