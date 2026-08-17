# News bridge scripts

Standalone (stdlib-first) bridge scripts that feed the bot's news framework.
Each is fired by an n8n workflow (`n8n/*.json`, import into the local n8n
instance) but runs fine from a plain shell too.  Full framework design:
`docs/news_framework_n8n_setup.md`.

| # | Script | Cadence | Output |
|---|--------|---------|--------|
| W1 | `calendar_bridge.py` | daily | economic-calendar risk windows |
| W2 | `crossmarket_monitor.py` | every 2 min (08:00–05:00 TPE) | `signal.json` circuit breaker + `regime_vote_w2.json` |
| W3 | `rss_scorer.py` | every 30 min | `regime_vote_w3.json` (RSS + Gemini sentiment) |
| W4 | `chips_monitor.py` | daily 16:15 TPE | `regime_vote_w4.json` (institutional money flow) |
| W5 | `n8n/W5_manual_tap.json` (webhook, no script) | on tap | `signal.json` via `crossmarket_monitor.py --force-fire` |

(W5 is the human-confirmation tap: a Discord link opens the n8n webhook,
which fires a *signal*, not a vote.  It was historically numbered "W4" —
if your n8n instance still shows a workflow named "W4 Manual tap", rename
or delete it there; the W4 slot belongs to the chips monitor.)

## Regime votes in one paragraph

Vote files live next to `news.regime_vote_path` (settings.yaml), one per
source: `regime_vote.json` → `regime_vote_w2.json` / `_w3` / `_w4`.  At
classification time (~04:58 TPE) the regime state machine reads every
valid, non-expired vote; a vote that agrees with the raw technical
classification skips one night of hysteresis confirmation.  Votes are
**deleted after every classification pass**, directions are only
`trending-up` / `trending-down` (no neutral — an uncertain source writes
nothing), and `expires_after_session` must equal the night being
classified (`"YYYY-MM-DD|NIGHT"`, open-date convention, 05:00 boundary).

## W4 — 籌碼 chips monitor (`chips_monitor.py`)

Turns the daily TAIFEX 三大法人 report into a regime vote.  Based on the
NTNU study (外資 is the only institution with 5/5 significant predictive
metrics) and a TEJ backtest over 2016-01 – 2025-10.

### Data sources

- **TAIFEX OpenAPI** `MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate`
  — filtered strictly to `商品名稱 == "臺股期貨"` (excludes 小型臺股期貨
  and options) and `身份別` starting with `外資`.  The net-OI field
  (`多空未平倉口數淨額`) covers **all delivery months combined** — the
  dataset has no near/far-month split.  Data posts ~15:30 TPE; on
  weekends/holidays the API keeps serving the last trading day, so the
  row date is checked against the requested trade date and a mismatch is
  a no-op.
- **TWSE BFI82U** (三大法人買賣金額統計表) — 外資 equity net buy/sell in
  NTD (買賣差額, rows starting with 外資 summed).  Used only as a
  contradiction check.  Note: this deviates from the original spec's T86
  endpoint on purpose — T86 is a **per-stock** report with no
  market-level 外資 row; BFI82U is the aggregate NTD table.

### Signal logic (`compute_chips_direction`)

1. `|外資 TX net OI| < 1,000` contracts → no vote (hysteresis band).
2. Futures direction vs equity flow contradiction (equity |flow| >
   5 bn NTD in the *opposite* direction) → no vote — 外資 buying stocks
   while short futures is hedging, not a directional view.
3. `|net OI| >= 5,000` → vote in the OI direction regardless of slope.
4. `1,000 <= |net OI| < 5,000` → vote only if the day-over-day OI slope
   agrees (position being built, not unwound).  Yesterday's OI comes
   from the `.chips_state.json` sidecar next to the vote file; on the
   first run (no history) a weak signal writes no vote.

If TWSE is unreachable the contradiction check can't run and the script
conservatively writes **no vote**.  On every no-vote outcome any stale
`regime_vote_w4.json` is deleted.  Today's OI is recorded in the sidecar
even on no-vote days so tomorrow's slope check works.

### Running

```bash
# normal daily run (what the n8n Execute Command node does)
python scripts/news_bridge/chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json

# inspect the decision without writing/deleting anything
python scripts/news_bridge/chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --dry-run

# re-run a specific trade date
python scripts/news_bridge/chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --date 20260814

# resolve the base path from settings.yaml instead
python scripts/news_bridge/chips_monitor.py --settings settings.yaml
```

An empty/unset `regime_vote_path` (votes disabled) exits cleanly with no
work.  Liveness: one line per run is appended to `chips_monitor.log`
next to the vote file (same self-trimming pattern as W2/W3).

### n8n setup

Import `n8n/W4_chips_vote.json`: cron 16:15 TPE weekdays → two HTTP
probe nodes (TAIFEX + TWSE, continue-on-fail — they exist so raw API
responses are visible in the execution log) → Execute Command running
`chips_monitor.py`.  The script does its own fetching and error
handling, so probe failures never block the run.
