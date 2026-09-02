---
name: evo-debug
description: Diagnose the strategy-evolution pipeline — did the weekly Saturday run fire, which phase did it reach, why did it skip or fail, and why has no candidate ever PASSed. Use when evolution seems silent, a Saturday run is missing, no 🧬 Discord message arrived, or the weekly review flags an evolution finding.
---

# Evolution Debugging

Run the mechanical check first, then map symptoms below:

```bash
C:/Python313/python.exe -s scripts/monitor/check_evolution.py
```

## How it works (1 paragraph you must hold in mind)

Evolution is **GUI-bound and per-bot**: on TPE **Saturday** (~04:58) a cheap
fitness check runs at session end, and (≥**05:05**, on the 30s poll) the AI
pipeline runs — but ONLY if the Tk app is open, a **non-regime** bot is
deployed and RUNNING, and `evolution.auto_pipeline` isn't false. Regime
bots are excluded by design (`hasattr(runner,'on_status_poll')` gate,
run_backtest.py ~6635/7135) and produce **zero** artifacts — silence from
a regime bot is not a failure. There is **no headless path** and no
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
| `data\changelog.json` + `Evo` class in `strategies\index.json` | repo | a PASS (none exist to date) |
| `🧬 週末自動演化 … starting` / `演化評分 Fitness: …` / `EVO pipeline error:` | bot `debug_*.log` | start / score / crash |
| 🧬 Discord messages | evolution channel | verdicts + auto-run skips |

**Observability gap:** every mid-pipeline stage message (`EVO 1/3…`, the
verdict block, `no_change` notices) goes to the **Tk chat widget only** —
not the debug log, not `_last_session.json`. Outside the GUI, outcome =
Discord + ai_usage call_sites. Don't conclude "nothing happened" from the
debug log alone.

## Symptom → cause

| Symptom | Likely cause → verify |
|---|---|
| No Saturday `bot_evolution` row at all | GUI closed / no non-regime bot RUNNING at 05:05 TPE Sat (6 of 11 recent Saturdays missed this way) / `auto_pipeline: false`. Check which bots were live: locks + debug logs for that Saturday. |
| Bot totally silent (no artifacts ever) | It's a regime bot — by design. Or the dialog deployed it after Saturday. |
| `bot_evolution` row but no codegen row | Plan said `no_change`, or payload degraded to plan-only (>600k chars), or the bot was fitness-gated (<30 trades). Check debug log `演化評分 Fitness:` line for the gate reason. |
| `EVO pipeline error:` in debug log | Worker crash (API/quota/parse) — full traceback is right there; **no Discord is sent on this path**. |
| Run skipped `訓練窗無新交易` | Watermark cut ≥ current design cut — **by design**, self-heals as trades age past the rolling 14-day holdout. Not a bug. |
| GUI frozen on a Saturday morning | The API-key modal (`_show_api_key_dialog`) fires from the poll thread when the key is missing — blocks the whole app until dismissed. Check `ai.google_api_key`/provider config presence (never the value). |
| Every verdict FAILs, `changelog.json` never appears | Structural: the verdict gate stack (src/evolution/pipeline.py `decide_deep_verdict`) — mutation-expression ≥3 divergent trades, MC CV ≤0.30, OOS ≥ baseline, PF/DD ratios, absolute PF ≥1.0, plus the plan's own criteria. Zero PASS ever as of 2026-09: review the FAIL verdict blocks in Discord history before touching thresholds — and never relax `ABS_PF_FLOOR` casually. |
| Discord silent though the run happened | `_discord` is constructed only at bot deploy; `discord_evolution_channel_id` empty falls back to the main channel; manual (🧬 button) runs suppress skip/error notices by design. |

## Cadence math (this machine)

TPE = local + 15h here. Saturday 05:05 TPE = **Friday 14:05 local**. The
fitness check (~04:58 TPE Sat) = Friday ~13:58 local. `ai_usage.csv`
timestamps are UTC (TPE − 8): a healthy Saturday run shows ~`Fri
21:05 UTC` rows.

## Escalation

A code-level root cause goes through `validate-findings` then `fix-pr`.
Threshold/design changes (verdict gates, cadence) are product decisions —
present the evidence and ask the user; do not auto-tune them.
