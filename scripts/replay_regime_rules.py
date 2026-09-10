"""Replay a bot's regime_history.csv through the regime engine, offline.

Answers "what would the switching brain have done under these rules?"
from the bot's own recorded classifier features — so a rule change
(pause semantics, flip accounting, streak handling) is compared against
what actually happened before it ships.

Usage:
    python scripts/replay_regime_rules.py "data/live/TMF00_mybot/regime_history.csv"
    python scripts/replay_regime_rules.py <csv> --mode legacy
    python scripts/replay_regime_rules.py <csv> --mode both   (default)

    --mode new|legacy|both
    --adx-enter / --adx-exit / --adx-strong / --confirm-sessions
    --max-flips / --flip-window / --pause-sessions
    --vote-quorum-up / --vote-quorum-down / --range-bias-action

"both" also prints the session-by-session diff between the legacy rules
and the current ones, and a parity line saying whether the legacy run
reproduced the recorded effective_regime column (it should — that is the
harness's own self-check).

Read-only: writes nothing, orders nothing.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.regime.replay import (                            # noqa: E402
    HistoryRow, ReplaySession, diff_sessions, parse_votes, replay)
from src.regime.state_machine import RegimeConfig          # noqa: E402

LEGACY_RULES = dict(pause_freezes_exits=True,
                    exits_count_as_flips=True,
                    transitional_resets_streak=True)


def _f(cell: str) -> float:
    try:
        return float(cell)
    except (TypeError, ValueError):
        return 0.0


def load_rows(path: str, session: str = "NIGHT") -> list[HistoryRow]:
    """Assessed rows only — P&L-only updates carry no adx and are skipped."""
    rows: list[HistoryRow] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for rec in csv.DictReader(f):
            if (rec.get("session") or "").strip() != session:
                continue
            if not (rec.get("adx") or "").strip():
                continue
            rows.append(HistoryRow(
                date=(rec.get("date") or "").strip(),
                adx=_f(rec.get("adx")),
                plus_di=_f(rec.get("plus_di")),
                minus_di=_f(rec.get("minus_di")),
                atr_ratio=_f(rec.get("atr_ratio")),
                ema_slope=_f(rec.get("ema_slope")),
                close=_f(rec.get("close")),
                vote_sources=parse_votes(rec.get("votes") or ""),
                recorded_raw=(rec.get("raw_regime") or "").strip(),
                recorded_effective=(rec.get("effective_regime") or "").strip(),
                recorded_decision=(rec.get("decision") or "").strip(),
            ))
    return rows


def build_cfg(args, legacy: bool) -> RegimeConfig:
    kwargs = dict(
        enabled=True,
        long_strategy="LONG", short_strategy="SHORT",
        adx_enter=args.adx_enter, adx_exit=args.adx_exit,
        adx_strong=args.adx_strong, confirm_sessions=args.confirm_sessions,
        max_flips=args.max_flips, flip_window=args.flip_window,
        pause_sessions=args.pause_sessions,
        vote_quorum_up=args.vote_quorum_up,
        vote_quorum_down=args.vote_quorum_down,
        range_bias_action=args.range_bias_action,
    )
    if legacy:
        kwargs.update(LEGACY_RULES)
    return RegimeConfig(**kwargs)


def print_run(title: str, sessions: list[ReplaySession]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'date':<12}{'raw':<16}{'effective':<16}{'pending':<20}"
          f"{'paused':<8}{'flips':<7}{'decision':<20}")
    for s in sessions:
        pending = f"{s.pending_label or '-'}:{s.pending_count}"
        print(f"{s.date:<12}{s.raw:<16}{s.effective:<16}{pending:<20}"
              f"{('YES' if s.paused else ''):<8}{s.flips:<7}{s.decision:<20}")


def print_parity(sessions: list[ReplaySession]) -> None:
    mismatches = [s for s in sessions
                  if s.recorded_effective and s.effective != s.recorded_effective]
    checked = sum(1 for s in sessions if s.recorded_effective)
    if not checked:
        print("\nParity: history has no effective_regime column to check against.")
        return
    if mismatches:
        print(f"\nParity: {len(mismatches)}/{checked} rows differ from the recording:")
        for s in mismatches:
            print(f"  {s.date}: replay={s.effective} recorded={s.recorded_effective}")
    else:
        print(f"\nParity: legacy rules reproduce all {checked} recorded "
              f"effective_regime values.")


def print_diff(legacy: list[ReplaySession], new: list[ReplaySession]) -> None:
    pairs = diff_sessions(legacy, new)
    print(f"\n=== diff: legacy vs current rules ({len(pairs)} sessions differ) ===")
    if not pairs:
        print("(identical)")
        return
    print(f"{'date':<12}{'legacy effective':<20}{'new effective':<20}"
          f"{'legacy decision':<22}{'new decision':<22}")
    for a, b in pairs:
        print(f"{a.date:<12}{a.effective:<20}{b.effective:<20}"
              f"{a.decision:<22}{b.decision:<22}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("history_csv")
    p.add_argument("--mode", choices=("new", "legacy", "both"), default="both")
    p.add_argument("--session", default="NIGHT")
    p.add_argument("--adx-enter", type=float, default=25.0)
    p.add_argument("--adx-exit", type=float, default=20.0)
    p.add_argument("--adx-strong", type=float, default=30.0)
    p.add_argument("--confirm-sessions", type=int, default=2)
    p.add_argument("--max-flips", type=int, default=3)
    p.add_argument("--flip-window", type=int, default=10)
    p.add_argument("--pause-sessions", type=int, default=5)
    p.add_argument("--vote-quorum-up", type=int, default=2)
    p.add_argument("--vote-quorum-down", type=int, default=1)
    p.add_argument("--range-bias-action", default="sit_out")
    args = p.parse_args()

    rows = load_rows(args.history_csv, args.session)
    if not rows:
        print(f"No assessed {args.session} rows in {args.history_csv}")
        return 1
    print(f"{len(rows)} assessed {args.session} sessions "
          f"({rows[0].date} .. {rows[-1].date})")

    legacy = replay(rows, build_cfg(args, legacy=True))
    new = replay(rows, build_cfg(args, legacy=False))

    if args.mode in ("legacy", "both"):
        print_run("legacy rules", legacy)
        print_parity(legacy)
    if args.mode in ("new", "both"):
        print_run("current rules", new)
    if args.mode == "both":
        print_diff(legacy, new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
