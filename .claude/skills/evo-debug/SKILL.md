---
name: evo-debug
description: Diagnose the strategy-evolution pipeline — did the weekly Saturday run fire, which phase did it reach, why did it skip or fail, and why candidates rarely PASS (first PASS 2026-10-10, knob mode). Use when evolution seems silent, a Saturday run is missing, no 🧬 Discord message arrived, or the weekly review flags an evolution finding.
---

# Evolution Debugging

> **Where the data is (since 2026-10-24/25):** the live fleet (12 headless
> `tai_backtest.exe` bots), the bot dashboard and the W1–W4 bridge tasks
> run on **Steven**, not this PC. This PC's `data\live` holds only
> retired/test bots and its n8n is stopped. Read live state from the
> dashboard API (`http://<Steven tailnet IP>:8765/api/fleet`,
> `/api/bot?id=<SYMBOL>_<bot>`; address in `hosts.local.yaml`) or run the
> checker on Steven via `tailscale-ssh`. A run of a checker on this PC
> describes this PC only — never publish it as fleet health. Host map →
> `start-here`. Run checkers from an up-to-date checkout
> (`git rev-list --count HEAD..origin/master` = 0); the main tree may be
> weeks behind.

Run the mechanical check first, then map symptoms below:

```bash
C:/Python313/python.exe -s scripts/monitor/check_evolution.py
```

## How it works (1 paragraph you must hold in mind)

Evolution is **per-bot**: on TPE **Saturday** (~04:58) a cheap
fitness check runs at session end, and (≥**05:05**, on the 30s poll) the AI
pipeline runs — but ONLY if a **non-regime** bot is deployed and RUNNING
in that process (GUI or headless CLI/EXE — `HeadlessBotApp` inherits
`_check_weekly_evolution`; on Steven every non-regime bot runs its own slot), and `evolution.auto_pipeline` isn't false. Regime
bots are excluded by design (`hasattr(runner,'on_status_poll')` gate,
run_backtest.py ~6635/7135) and produce **zero** artifacts — silence from
a regime bot is not a failure. There is no
watermark-based timer; `evolution_watermark.json` only records the design
cut (`trade_count`) shown to the AI. The weekly latch (`_weekly_evo_done`)
is in-memory — restarting the GUI on a Saturday re-arms it.

## The evidence map (outside the GUI, only these exist)

| Evidence | Where | Meaning |
|---|---|---|
| `bot_evolution` row | `data\ai_usage.csv` (**UTC** timestamps; TPE = +8) | plan phase reached that day |
| `evolution_codegen_1`/`_2` rows | same | codegen reached / retried |
| `evolution_watermark.json` `at` | bot dir (TPE stamp) | pipeline attempt finished (even failed) |
| `evolution.json` | bot dir | fitness baseline; rewritten only on >0.05 improvement — frozen is normal |
| `data\changelog.json` entry (date is machine-local) + `data\evolution_pool.db` row | install root of the bot's host | a PASS (`validated`, not deployed — the owner promotes; first PASS 2026-10-10, knob mode). `strategies\index.json` is NOT touched on PASS |
| `🧬 週末自動演化 … starting` / `演化評分 Fitness: …` / `EVO pipeline error:` | bot `debug_*.log` | start / score / crash |
| 🧬 Discord messages | evolution channel | verdicts + auto-run skips |

**Observability gap:** every mid-pipeline stage message (`EVO 1/3…`, the
verdict block, `no_change` notices) goes to the **Tk chat widget only** —
not the debug log, not `_last_session.json`. Outside the GUI, outcome =
Discord + ai_usage call_sites. Don't conclude "nothing happened" from the
debug log alone.

## Classifying a run from the debug log alone (the `msg_len` signature)

The bot's `debug_*.log` records every Discord send as `Discord send
attempt: channel=…, msg_len=N, bot=…`. `notify_evolution` prepends a
header `**[YYYY-MM-DD HH:MM:SS]** \`<bot>\` \`<symbol>\`` + newline, so
the length = template + header (header ≈ 25 + 3 + len(bot name) + 8 + 1).
Find the `🧬 週末自動演化 … starting` line, then the FIRST send after it:

| Δt after start | msg_len | Meaning (run_backtest.py) |
|---|---|---|
| ≤ 2 s | ≈ 200–210 + name | holdout skip「訓練窗無新交易」(~:4748) — BY DESIGN, watermark unchanged |
| ≤ 2 s | ≈ 300 + name | young-session skip「資料太新」(~:4779) — BY DESIGN |
| 30–90 s (after the K-line fetch) | 71 + header ≈ 110–130 | plan said `no_change` (~:5095) — one `bot_evolution` row in ai_usage, no codegen row |
| 60–120 s | ≈ 1000–1500 | **verdict block** (`evolution_verdict`, discord_notify.py ~:361) — PASS or FAIL; text only in Discord |
| any | starts with `🧬 EVO FAIL` / `EVO ERROR` — check the log for `EVO codegen attempt … failed` / `EVO pipeline error` | failure paths (Discord-notified since #108) |

Before the plan call the live slot fetches native `kline_minute` bars via
the Capital API (`請求K線 [n/33] … min=15` lines, run_backtest.py
~:5338-5372); no fetch = the skip branch returned first. The verdict text
is NOT persisted locally (chat auto-save only at window close; the bot
token cannot read Discord history — HTTP 403). The same signature applies
to other users' bundles attached to GitHub issues.

## Efficacy, not just liveness (validated 2026-09-12, weekly-review addendum)

- Attempt history = `ai_usage.csv` per Saturday: plan row without a
  codegen row = `no_change`. On this machine candidates were generated on
  only 4 Saturdays (06-20, 06-27, 07-04, 08-01), all FAIL.
- **Why plans say no_change**: the prompt rule "fewer than 30 trades →
  continue collecting, no change" (~:4832) is read by the model against a
  trade list that contains ONLY trades new since the last watermark
  (`sent_lines = trade_lines[omitted:cut_idx]` ~:4778; 3 trades on
  09-12, 1 trade in issue #94). The watermark advances right after the
  worker thread starts (~:4894), whatever the plan says. Fixed in v2.19.0 (#118/#120).
- **Gate defect**: `max_drawdown_pct` is 0.00 when the window's equity
  never goes positive (metrics.py sentinel, initial_balance=0), so the
  baseline-relative dd gates (pipeline.py ~:260, ~:528-534) reject every
  candidate in a losing-baseline fortnight. Fixed in v2.19.0 (#118/#120).
- Gates ARE reachable: replaying the exact pipeline on 0422's real data,
  2/10 single-parameter neighbours PASS; binding gates are PF-vs-baseline
  (strict ≥) and the design-window MaxDD ratio. Deterministic (seed 42).
- Latent: candidate class name not enforced (a PASS could overwrite the
  live strategy's stored source — fixed in v2.19.0 (#118/#120)); Monte Carlo is a no-op for
  every AI strategy (`**kwargs` signatures → no numeric defaults).
- Short bots (1–7 trades / 2 wk) cannot evolve: 5-trade floor + the <30
  rule. By design; owner decision.
- The empirical harness lives in the session scratchpad
  (`empirical/evo_gates.py`): loads a bot's 1-min CSVs, aggregates to
  native bars, runs `run_deep_validation` + `decide_deep_verdict` for a
  perturbation family. Rebuild it when a deep audit is triggered.

## Symptom → cause

| Symptom | Likely cause → verify |
|---|---|
| No Saturday `bot_evolution` row at all | GUI closed / no non-regime bot RUNNING at 05:05 TPE Sat (6 of 11 recent Saturdays missed this way) / `auto_pipeline: false`. Check which bots were live: locks + debug logs for that Saturday. |
| Bot totally silent (no artifacts ever) | It's a regime bot — by design. Or the dialog deployed it after Saturday. |
| `bot_evolution` row but no codegen row | Either knob mode (watermark `at` inside the slot, `directives.knobs` set, multifold + month gates in `evolution_history.jsonl` `gates`) — never infer "stopped at plan" from ai_usage alone (#161) — or the plan said `no_change` — almost always because the model applied the "<30 trades" rule to the watermark-windowed new-trade list (see Efficacy below; fixed in v2.19.0 (#118/#120)), or payload degraded to plan-only (>600k chars). Check debug log `演化評分 Fitness:` line and the `msg_len` ≈ 110–130 send. |
| `EVO pipeline error:` in debug log | Worker crash (API/quota/parse) — full traceback is right there; **no Discord is sent on this path**. |
| Run skipped `訓練窗無新交易` | Watermark cut ≥ current design cut — **by design**, self-heals as trades age past the rolling 14-day holdout. Not a bug. |
| GUI frozen on a Saturday morning | The API-key modal (`_show_api_key_dialog`) fires from the poll thread when the key is missing — blocks the whole app until dismissed. Check `ai.google_api_key`/provider config presence (never the value). |
| Every verdict FAILs, `changelog.json` never appears | Structural: the verdict gate stack (src/evolution/pipeline.py `decide_deep_verdict`) — mutation-expression ≥3 divergent trades, MC CV ≤0.30, OOS ≥ baseline, PF/DD ratios, absolute PF ≥1.0, plus the plan's own criteria. PASS is rare (first one 2026-10-10): review the FAIL verdict blocks in Discord history before touching thresholds — and never relax `ABS_PF_FLOOR` casually. |
| Discord silent though the run happened | `_discord` is constructed only at bot deploy; `discord_evolution_channel_id` empty falls back to the main channel; manual (🧬 button) runs suppress skip/error notices by design. |

> Line numbers (`~:NNNN`) in this skill predate ~2026-10 and have shifted by
> 1,600–3,000 lines — grep the quoted literal (e.g. `訓練窗無新交易`,
> `演化評分`) instead. Per-attempt outcome is now in each bot dir's
> `evolution_history.jsonl` (`final_status`, `gates.*.reasons`); the
> `msg_len` table is a legacy fallback.

## Cadence math (this machine)

The slot is Sat 05:05 TPE on the host that runs the bot (Steven for the
fleet). clocks → `start-here` §4. `ai_usage.csv`
timestamps are UTC (TPE − 8): a healthy Saturday run shows ~`Fri
21:05 UTC` rows.

## Escalation

A code-level root cause goes through `validate-findings` then `fix-pr`.
Threshold/design changes (verdict gates, cadence) are product decisions —
present the evidence and ask the user; do not auto-tune them.
