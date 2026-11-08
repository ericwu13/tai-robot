# Real-order signature and hunt procedure

## Where the dashboard decides real vs paper
`scripts/dashboard/real_pnl.py::is_real_order(trade)` reads each trade in
`<bot_dir>/session.json → broker.trades`:
real ⇔ `source == "real"` OR `real_entry_price > 0` OR `real_exit_price > 0`.

Two ways this goes wrong:
1. **Paper shown as real.** In June/July (before #92) paper trades got a `real_exit_price` planted
   from `GetFulfillReport(...,4)`, the account's day average across ALL bots (44,794 = the #92
   incident value). Example: AdvancedMomentumFilter LONG 2026-07-23 09:01, source=paper,
   real_exit_price=44794, and the bot never sent an order.
2. **Real shown as paper, or with no source.** `SimulatedBroker` downgrades `source` "real" → "paper"
   when the real ENTRY fill never confirmed (`real_entry_price == 0`), even when the order reached
   the account. Trades from before `source` existed (May/June) have `source == ""`.

## The signature (falsifiable; mode-independent)
Evidence is only what the bot's own log wrote when it called `SendFutureOrderCLR`
(`run_backtest.py::_send_real_order`). These lines are written only on that path:

```
[ts] REAL ORDER: <BUY|SELL> <sym> acct=*** sim_price=<P>
[ts] REAL ORDER PARAMS: ... newclose=<0 new | 1 close | 2 auto>
[ts] REAL ORDER RESULT: code=0 message=<13-digit seq>
[ts] REAL ORDER OK: <13-digit seq>
```

For each trade T in session.json:
- **REAL leg:** a `REAL ORDER:` line with side = the leg's side (entry: LONG→BUY, SHORT→SELL;
  exit: the opposite side), sent within [-120 s, +300 s] of the leg's `entry_dt`/`exit_dt`,
  and followed by `REAL ORDER OK: <seq>`. Each order is matched to at most one leg.
  Corroboration (not required): `REAL FILL (<leg>): seq=<same seq> price=…`, the
  `REAL_ORDER_SENT`/`REAL_*_CONFIRMED` rows in decisions.csv, and `sim_price` equal to the leg's
  sim price.
- **REAL** = both legs REAL. **REAL_ENTRY_ONLY / REAL_EXIT_ONLY** = one leg REAL (I review these
  by hand).
- **PAPER** = no matched OK order on either leg, AND the bot's log has lines in the clock hour of
  both legs (coverage), so the absence of an order is real evidence. Attempted orders that came
  back FAILED, BLOCKED or ERROR count as not sent.
- **UNKNOWN** = no matched order, and a leg's hour has no log coverage. These are left unchanged.
- **ORPHAN order** = an OK real order that matches no trade (a double send, or a trade missing
  from session.json). Reported only.

**What does NOT prove anything:** `trading_mode`, the `--mode auto` deploy line, or the
"Deploying ... mode=auto" log line. Proof: 2.11BbandSmaShortV3 was `mode=auto` at
2026-10-12 23:01, but the order was "skipped (timeout 10s)" and no `REAL ORDER` line was written,
so that trade is paper. Price fields are not proof either (case 1 above).

### Validation (2.11BbandSmaShortV3, v21916 dir)
- Known REAL: SHORT 2026-10-16 01:01:10. `REAL ORDER: SELL … sim_price=47174` 01:01:11, then
  `OK: 2315618285807`, then `REAL FILL (entry) price=47152 accepted=True`. Exit: BUY 04:01:06,
  `OK: 2315618298422`, `REAL FILL (exit) price=47365`. session: source=real, 47152/47365. ✔ REAL
- Known PAPER: SHORT 2026-10-12 23:01:00. ENTRY_FILL logged 23:01:01, "Order skipped
  (timeout 10s)" 23:01:14, no REAL ORDER line in that hour (logs cover it). session: source=paper,
  no real prices. ✔ PAPER
- Whole bot: 64 trades → 47 REAL, 17 PAPER, 0 UNKNOWN. 95 OK orders = 47×2 + 1 orphan
  (2026-08-18 09:46:59, a second SELL, newclose=auto, filled 45603, accepted=False).
  2 PAPER trades currently show as real (#4 2026-06-17, #19 2026-07-16, planted realX).

## Hunt procedure (fixed steps; one run per bot dir; READ-ONLY)
Tools: `scripts/real_order_audit/` — `extract.ps1` (remote read-only scan), `fetch.sh` (needs `REAL_AUDIT_SSH`), `match.py`, `patch.py`.
1. `fetch.sh <bot_dir> <out.txt>` runs `extract.ps1` on Steven over SSH. It opens the files with
   ReadWrite sharing, so the running bot is never blocked. It emits:
   E = evidence lines (REAL ORDER / REAL FILL / ENTRY_FILL / TRADE_CLOSE / skipped / deploy) from
   every `debug_*.log` and `cli_stdout.log*`; H = every clock hour with any log line;
   S = session.json; D = decisions.csv REAL_/ORDER rows.
2. `python -I match.py <out.txt> --json <out.json>` gives a verdict per trade, plus orphans.
3. Review by hand: every REAL_ENTRY_ONLY / REAL_EXIT_ONLY / UNKNOWN row and every orphan. Grep
   `<out.txt>` for ±10 min around the leg. Look for FORCE CLOSE RETRY, a FAILED/BLOCKED result, or
   a mode switch. Write a one-line explanation.
4. Report: counts per verdict, then every trade whose verdict ≠ what the dashboard shows now
   (MISMATCH), with seqs as evidence, then orphans. No writes.
Scope: the v21916 bot dirs (the dashboard's `--base-dir`). The older installs (v21910, v2199) hold
the same debug-log files, with identical REAL ORDER OK counts per file, so they are a subset and
are not scanned again.

## Data fix (owner approves each run)
Store = `<bot_dir>/session.json` (the only store the dashboard reads). A running bot rewrites
this file from memory, so each bot must be STOPPED while its file is edited.
Window: the weekend, market closed until Mon 08:45 TPE. Per bot that has ≥1 mismatch:
1. Stop it (STOP file → `_stop_live`, the deploy skill; never kill by image name).
2. Back up `session.json` → `session.json.bak_realaudit_<ts>`.
3. `patch.py <downloaded session.json> <match.json> <patched.json> <audit.json>` edits only these fields:
   - REAL (and REAL_ENTRY_ONLY after review): `source="real"`.
   - PAPER: `source="paper"`; on the trades with planted prices, set `real_entry_price=0`,
     `real_exit_price=0`, `real_*_dt=""`, `real_exit_estimated=false`. The originals stay in the
     backup and in `real_order_audit.json`, written beside the file.
   - UNKNOWN: no change.
4. Resume with the same command line it was deployed with (`--resume`). The REAL-mode
   BollingerBreakoutATRShortOnly gets the deploy skill's real-order consent gate, last.
5. Check `/api/fleet` and `/api/bot/<id>`: the `real` counts and `real_status` should match the
   audit.
## Going forward
Builds with this change stamp the `SendFutureOrderCLR` seq onto the trade when the order is sent
(`Trade.entry_order_seq` / `exit_order_seq`, via `SimulatedBroker.note_real_order_sent`). The proof
then no longer depends on a fill being confirmed. `real_pnl.is_real_order` decides those rows by
the seq, and a stray price is no longer proof. Legacy rows, which lack the keys, keep the price
rule, so this audit is still how to clear their planted prices.

Caveat: `REAL ORDER OK` means the broker ACCEPTED the order. An IOC market order that was
accepted but never filled would still count as REAL here. The dashboard then shows it as
`missing` (no fill price), not as paper.

## First run (2026-11-08, Steven v21916, 12 bots)
| bot | trades | REAL | PAPER | edits |
|---|---|---|---|---|
| 2.10.0DynamicExitPullbackV3 | 179 | 33 | 146 | 27 source→real, 10 planted prices cleared, 5 source ""→paper |
| 2.11BbandSmaShortV3 | 64 | 47 | 17 | 5 source→real, 2 planted prices cleared |
| AdvancedMomentumFilter | 2 | 0 | 2 | 1 planted price cleared |
| DynamicExitPullbackStrategyV2 | 306 | 50 | 256 | 50 planted prices cleared, 44 source ""→paper |
| DynamicLongV3xBBShortV3 | 81 | 0 | 81 | 4 planted prices cleared |
| TxfMultiTimeframeTrendStrategy | 89 | 3 | 86 | 4 planted prices cleared |
| BollingerBreakoutATR / ShortOnly / DEPV2Evo1 | 60 / 27 / 54 | 2 / 6 / 7 | rest | none |
| LongOnly / DEPV3Evo1 / 08-04-rss | 16 / 13 / 43 | 0 | all | none |

There were 0 UNKNOWN rows. Every planted price is on a paper trade dated before 2026-07-26. In June
and July, decisions.csv shows `REAL_EXIT_CONFIRMED price=…` rows with no order behind them, for
example right after `REAL_ORDER_SKIPPED … no real entry was confirmed`. Their values repeat across
unrelated trades (41319, 44794, 45514), and some are field fragments (133, 200, 460).
One orphan: 2.11BbandSmaShortV3 2026-08-18 09:46:59 SELL seq 2315609126386 (newclose=auto, filled
45603), sent 59 s after that trade's entry order. It is not tied to any trade.
