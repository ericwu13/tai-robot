# News Framework — n8n Setup Guide (Phase 3)

The bot side (Phases 1–2, branch `feat/news-framework`) is complete: the regime-switching
runner polls two JSON files every 30 s and reacts. This guide specifies the n8n workflows
that write those files, the exact file contracts, and the go-live checklist.

**Design principle:** n8n never places orders and never talks to the bot process. It writes
files and posts to Discord. The bot treats every file as untrusted input — schema-validated,
staleness-checked, deduplicated. A broken n8n can annoy the bot, never break it.

```
n8n                                        bot (RegimeSwitchingRunner, 30 s poll)
├─ W1 Calendar sweep (weekly+daily) ─────→ events.json      → calendar sit-out gate
├─ W2 Cross-market monitor (night, 2–5m) ─→ signal.json      → Tier 1 risk_off (auto)
├─ W3 RSS + Gemini scorer (15–30 m) ─────→ Discord + CSV log (advisory / evidence only)
└─ W4 Human confirm (Discord tap link) ──→ signal.json      → Tier 2 deploy_short/long
```

---

## 1. File contracts (FROZEN — the bot enforces these)

### 1.1 `events.json` — scheduled-event calendar

```json
{
  "version": 1,
  "updated_at": "2026-08-04T10:00:00+08:00",
  "events": [
    {"date": "2026-08-13", "name": "US CPI", "severity": "high", "sessions": ["DAY", "NIGHT"]},
    {"date": "2026-08-27", "name": "NVDA earnings (US after-close)", "severity": "high", "sessions": ["NIGHT"]}
  ]
}
```

Rules the bot enforces:
- `date` is the **session OPEN date** (Taipei). The night session 15:00→05:00 keeps its
  open date on both sides of midnight — an event during the small hours of the 14th that
  belongs to the night that opened on the 13th must be dated `2026-08-13` with `NIGHT`.
- `updated_at` must be timezone-stamped and refreshed on **every** write, even when the
  event list is unchanged. A calendar ≥ 14 days old is stale → the bot stops gating AND
  releases any standing event gate (fail-open).
- `severity` ∈ high | medium | low; the bot gates at `news.calendar_min_severity`
  (default `high`). Malformed entries are skipped individually; the rest survive.
- Effect of a matching event: **entries suppressed for that session** (sit-out). If a
  position is already open when the event would gate, the gate defers until the trade
  exits by its own management — calendar gates never touch open positions.

What belongs in it: FOMC, US CPI, US mega-cap earnings (**MSFT, NVDA**, TSMC ADR),
Taiwan CPI/GDP, triple witching. Lesson from 2026-07-30: MSFT earnings pivoted the whole
TAIEX reversal — mega-cap earnings are calendar events, not surprises.

### 1.2 `signal.json` — circuit-breaker signal

```json
{
  "version": 1,
  "signal_id": "d6f8c2aa-1b1e-4b7e-9a44-0c2f6f3f0e11",
  "issued_at": "2026-08-04T22:05:00+08:00",
  "action": "risk_off",
  "severity": "high",
  "source": "crossmarket-sox",
  "reason": "SOX -3.2% intraday, TSM ADR -4.1%"
}
```

Rules the bot enforces:
- `action` ∈ `risk_off` (flatten + suppress entries) | `clear` (release signal suppression)
  | `deploy_short` / `deploy_long` (forced-entry event strategy — only honored when
  `news.tier2_enabled: true`, otherwise announced on Discord and consumed silently).
- **New decision = new `signal_id`** (generate a UUID per firing). The bot's ledger
  dedups by id forever; rewriting the file with the same id is a permanent no-op.
  The bot never deletes the signal file — just leave it; overwrite it for the next signal.
- `issued_at` MUST carry a timezone offset (naive → rejected). Freshness window:
  strictly younger than `news.max_signal_age_sec` (default 900 s), at most 120 s in the
  future. Bot polls every 30 s, so any fresh signal is seen within one cycle.
- Every valid signal is consumed **exactly once**, including ones that do nothing
  (tier 2 off, replay in progress, action failed). Nothing is retried.
- **Tier 2 while holding a position:** the flatten precedes the swap, and the swap is
  vetoed while the exit fill is unconfirmed (semi_auto/auto) — the deploy is then dropped
  with a Discord error, not retried. Preferred pattern: send `risk_off` first, then
  `deploy_*` as a **separate signal** (new id) once flat. In paper mode this is a
  non-issue (simulated fills are immediate).

### 1.3 Ledger (`news_ledger.json`)

Bot-owned. n8n must never write or delete it. Defaults into the bot directory when
`news.ledger_path` is empty.

---

## 2. Workflow W1 — Calendar sweep

**Trigger:** Schedule, daily 10:00 TPE (daily so `updated_at` never goes stale even
without changes; the event list itself moves weekly).

**Nodes:**
1. **HTTP Request** — economic calendar source. Options: Finnhub `/calendar/economic`
   (free key), or an RSS/page of your choice. For earnings dates: Finnhub
   `/calendar/earnings?symbol=MSFT` etc., or maintain the mega-cap list by hand quarterly
   (they're only ~8 dates/quarter).
2. **Gemini (gemini-2.5-flash)** — only if a source needs extraction from prose. Prompt it
   to emit strictly the `events` array schema; set the node to JSON output.
3. **Code node** — build the full document: filter to the next 21 days, map each event to
   Taipei session-open dates (a US after-close event = Taiwan `NIGHT` of the same Taipei
   date; a US day-session event ≈ Taiwan `NIGHT` too since it lands 21:30–05:00 TPE),
   stamp `updated_at` with the current time **with offset**, `version: 1`.
4. **Write File** — atomic-ish: write `events.json.tmp` then rename/overwrite
   `events.json` (n8n "Read/Write Files from Disk" node; the bot tolerates a rare torn
   read — it just fails open until the next 5-min re-parse).

**Discord (optional):** post the coming week's gated sessions every Monday.

---

## 3. Workflow W2 — Cross-market monitor (the primary fast trigger)

**Trigger:** Schedule, every 2 min, **only between 15:00 and 05:00 TPE** (n8n cron:
two ranges, `15:00–23:59` and `00:00–05:00`). Outside the night session there is nothing
to monitor — the Taiwan day session doesn't overlap US trading.

**Nodes:**
1. **HTTP Request ×3 (parallel)** — quotes for: `SOXX` (SOX proxy ETF), `TSM` (TSMC ADR),
   `QQQ` (Nasdaq proxy). Finnhub `/quote` (free tier: real-time US, 60 calls/min — 3 calls
   per 2 min is nothing). Verify freshness: each response carries a timestamp; discard
   quotes older than 10 min (pre-market/closed).
2. **Code node — threshold logic:**
   - Intraday move = `(current − previousClose) / previousClose`.
   - Fire `risk_off` when: SOXX ≤ −2.5% **or** TSM ≤ −3.5% **or** QQQ ≤ −2.0%.
   - Fire long-side alert when the mirror thresholds hit on the upside.
   - **Firing dedup (n8n side):** keep `lastFiredBucket` in workflow static data — fire at
     most once per direction per US session; re-arm only after the metric retreats halfway
     to zero. (The bot's ledger also dedups, but don't rely on it for rate-limiting.)
3. **On fire — downside:** Code node builds `signal.json` (fresh UUID, `issued_at` now
   with offset, `action: "risk_off"`, reason with the numbers) → Write File → **Discord**
   alert with the numbers + a W4 confirmation link for `deploy_short`.
4. **On fire — upside:** no auto-signal (there's no long position to protect — upside
   shocks are opportunity, not risk). Discord alert + W4 confirmation link for
   `deploy_long` only.

This asymmetry is deliberate: downside auto-protects (bounded cost if wrong), both
directions require the human tap to *enter* anything.

---

## 4. Workflow W3 — RSS + Gemini scorer (advisory + evidence)

**Trigger:** Schedule, every 20 min.

**Nodes:**
1. **RSS Read ×N** — Anue 鉅亨 headlines, Yahoo Finance TW, MoneyDJ, plus one US macro
   feed. Dedup against seen GUIDs (workflow static data).
2. **Gemini (gemini-2.5-flash)** — batch the new headlines into ONE call. Structured
   output: `[{headline, sentiment: -1..1, category: macro|semis|geopolitical|taiwan|other,
   severity: high|medium|low, rationale_one_line}]`.
3. **Append to CSV** — `news_signal_log.csv`: timestamp (TPE, with offset), feed, headline,
   score fields. **This log is the only valid evaluation data for the news layer** — LLM
   retro-scoring of past headlines is contaminated by training-data memorization
   (lookahead bias), so the forward log is the evidence that earns or denies future
   automation. Never regenerate it retroactively.
4. **Discord** — only `severity: high` items, next to the regime notifications. Everything
   else stays in the CSV.

W3 writes **no** signal files. If months of CSV prove the scores lead sessions reliably,
promoting W3 to a signal writer is a deliberate future decision — with the same schema.

---

## 5. Workflow W4 — Human confirmation (the tap)

**Trigger:** n8n **Webhook node** (GET). The URL is a secret; add a static token query
param and check it in the first node; reject otherwise. W2/W3 embed this URL (with
`?token=...&action=deploy_short` etc.) as a link in their Discord alerts.

**Nodes:**
1. **IF** — token valid, action ∈ {deploy_short, deploy_long, risk_off, clear}.
2. **Code node** — build `signal.json` with a **fresh UUID and fresh `issued_at`**
   (never reuse the alert's timestamp — the tap may come 10 minutes later and the bot
   rejects stale signals; the tap time IS the decision time).
3. **Write File** → **Discord** confirmation ("✅ deploy_short signal written — bot will
   act within 30 s").

Phone flow: Discord alert → tap link → browser opens the webhook → done. ~5 seconds.

---

## 6. Bot-side settings

```yaml
# settings.yaml (never committed)
news:
  enabled: true
  signal_path: "C:/n8n-bridge/signal.json"
  events_path: "C:/n8n-bridge/events.json"
  ledger_path: ""                # empty = bot dir default
  max_signal_age_sec: 900
  tier2_enabled: false           # keep false until the paper record justifies it
  calendar_min_severity: "high"
```

News gating runs **only on regime-switching deploys** (bot type 2). Plain LiveRunner
bots ignore all of it.

---

## 7. Go-live checklist (paper first, always)

1. **Hand test before n8n exists:** write a `signal.json` by hand (fresh UUID, current
   `issued_at`+08:00, `action: "risk_off"`) with a paper regime bot holding a position.
   Within 30 s expect: Discord notification, `FORCE_CLOSE` row tagged `news_risk_off` in
   `decisions.csv`, entries suppressed, ledger file created. Write `clear` (new UUID) →
   suppression released. Re-write the same `risk_off` id → nothing happens (dedup).
2. **Calendar test:** hand-write `events.json` with today's session, `severity: high` →
   sit-out stamped (visible in regime status + Discord). Backdate `updated_at` 15 days →
   gate releases with the stale warning.
3. **Restart test:** trigger suppression, restart the bot, confirm suppression and
   consumed ids survive (session restore).
4. Build W1–W4, repeat 1–2 through n8n end-to-end.
5. Run the news-enabled **paper** bot alongside the unchanged baseline regime bot
   (A/B, reports are namespaced per bot). Tier 2 stays off until Tier 1 + the W3 log
   look sane for ≥2 weeks; real-money mode only after the paper record clears the same
   bar as any evolved strategy (≥14 days, ≥30 trades, positive with margin — remember
   paper fills flatter shock-entry prices, so demand margin, not just green).
