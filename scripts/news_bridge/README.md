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

## W2 — 跨市場 cross-market monitor (`crossmarket_monitor.py`)

Two symbol tiers, and one quorum gate on top of both:

| Tier | Symbols | May write `signal.json`? |
|------|---------|--------------------------|
| signal | `SOXX` ±2.5%, `TSM` ±3.5%, `QQQ` ±2.0% | yes — downside only, **and only with the vote quorum** |
| alert | `ASML.AS` ±3.0%, `^STOXX50E` ±2.0%, `NQ=F` ±1.5%, `^N225` ±2.0%, `^KS11` ±2.0%, `^HSI` ±1.5%, `^TWII` ±1.5%, `000001.SS` ±1.5% | never — Discord alert only, both directions |

Every symbol in both tiers carries its own (looser) **vote** thresholds and
counts toward the quorum: `risk_off` is written only when ≥ `VOTE_MIN_SYMBOLS`
(2) fresh symbols breach their vote thresholds down and none breaches up.  A
single signal-tier breach with no quorum posts the alert and writes nothing
(dedup key `signal-alert-down:<us_date>`, deliberately separate from
`down:<us_date>` so it can never suppress a later real fire).

Upside never auto-writes anything, in either tier — entering a position
always needs the human tap.

## Regime votes in one paragraph

Vote files live next to `news.regime_vote_path` (settings.yaml), one per
source: `regime_vote.json` → `regime_vote_w2.json` / `_w3` / `_w4`.  At
classification time (~04:58 TPE) the regime state machine reads every
valid, non-expired vote; votes that agree with the raw technical
classification and **meet that direction's quorum** skip one night of
hysteresis confirmation.  Votes are **deleted after every classification
pass**, directions are only `trending-up` / `trending-down` (no neutral —
an uncertain source writes nothing), and `expires_after_session` must
equal the night being classified (`"YYYY-MM-DD|NIGHT"`, open-date
convention, 05:00 boundary).

Two gates sit between a vote file and a strategy swap:

- **Asymmetric quorum** (`regime.vote_quorum_up` = 2,
  `regime.vote_quorum_down` = 1): going long on external evidence is the
  expensive mistake, so it takes two sources; confirming a down read
  early is cheap and protective, so one is enough.  Conflict-cancel in
  the range-bound probe is unchanged and absolute — ANY opposing vote
  kills it, quorum or not.
- **Per-source age gate** (`news.nightly_vote_max_age_h`, default
  `{W2: 4.0}`): the writer stamps `fired_at`, and a vote older than its
  source's limit at read time is consumed with the rest but never shown
  to the classifier.  W2 fires during the US session and the nightly lane
  reads it ~20 h later at the END of the session it named; W2's edge
  lives in the ~4 h after the fire.  Sources not listed have no limit.

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

The futures signal is the **day-over-day CHANGE in 外資 TX net OI (ΔOI)**,
never the level: 外資 carry a permanent structural hedge short (~-80,000
contracts), so the level clears every threshold in the short direction
every single day and can never turn positive.  Yesterday's OI comes from
the `.chips_state.json` sidecar next to the vote file; a baseline older
than 5 calendar days is discarded (a stale Δ silently inflates across
the gap).

1. No OI baseline within 5 calendar days → no vote (ΔOI undefined);
   today's OI is still recorded so the next pass has a baseline.
2. `|ΔOI| < 1,000` contracts → no vote (hysteresis band).
3. ΔOI direction vs equity flow contradiction (equity |flow| > 5 bn NTD
   in the *opposite* direction) → no vote — 外資 adding shorts on a day
   they net-bought equities is a hedge adjustment, not a directional
   view.
4. `|ΔOI| >= 5,000` → vote on the sign of ΔOI.
5. `1,000 <= |ΔOI| < 5,000` → vote only when the 外資 equity flow points
   the *same* way (> 5 bn NTD — active cash-market confirmation).

If TWSE is unreachable the contradiction check can't run and the script
conservatively writes **no vote**.  On every no-vote outcome any stale
`regime_vote_w4.json` is deleted.  Today's OI is recorded in the sidecar
even on no-vote days so tomorrow's ΔOI has its baseline.

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
