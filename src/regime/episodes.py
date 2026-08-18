"""Episode grouping for the regime tab's switching log.

Pure data-shaping — no Tkinter. Turns regime_history.csv rows into
"episodes": consecutive runs of the same active leg (long / short /
idle), each carrying its sessions and cumulative P&L. The interesting
unit for review is the decision ("held SHORT from X to Y — net P&L"),
not the raw row dump.

Robust against the file's real-world quirks:
- duplicate (date, session) rows from pre-idempotent-record versions
  are merged (last row wins, original position kept)
- pre-v3 rows are one cell short (no votes column) — header-name
  lookup with length guards
- rows may be classification-only (no P&L yet), result-only (DAY), or
  both
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

_ARROW = {"trending-up": "↑", "trending-down": "↓"}


@dataclass
class SessionRow:
    date: str
    slot: str                  # "DAY" | "NIGHT"
    raw_regime: str = ""
    effective_regime: str = ""
    adx: str = ""
    decision: str = ""
    votes: str = ""            # raw cell, e.g. "W3:trending-up*"
    pnl: float | None = None
    trades: int | None = None
    strategy_active: str = ""

    @property
    def key(self) -> str:
        return f"{self.date}|{self.slot}"


@dataclass
class Episode:
    leg: str                       # "long" | "short" | "idle" | "unknown"
    strategy: str                  # active strategy display name ("" for idle)
    sessions: list = field(default_factory=list)   # chronological SessionRows
    open: bool = False             # still the current episode

    @property
    def start(self) -> str:
        return self.sessions[0].date if self.sessions else ""

    @property
    def end(self) -> str:
        return self.sessions[-1].date if self.sessions else ""

    @property
    def pnl(self) -> float:
        return sum(s.pnl for s in self.sessions if s.pnl is not None)

    @property
    def trades(self) -> int:
        return sum(s.trades for s in self.sessions if s.trades is not None)


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_history(rows: list) -> list:
    """CSV rows (header first) → chronological, deduped SessionRows.

    Duplicate (date, session) rows are merged with LAST-row-wins at the
    FIRST row's position — pre-fix files could hold both a bare result
    row and a classification row for the same session.
    """
    if len(rows) < 2:
        return []
    header, data = rows[0], rows[1:]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    idx = {name: col(name) for name in (
        "date", "session", "raw_regime", "effective_regime", "adx",
        "decision", "votes", "pnl", "trades", "strategy_active")}

    def cell(row, name):
        i = idx[name]
        return row[i] if 0 <= i < len(row) else ""

    merged: dict[str, SessionRow] = {}
    for row in data:
        date, slot = cell(row, "date"), cell(row, "session")
        if not date or not slot:
            continue
        sess = SessionRow(
            date=date, slot=slot,
            raw_regime=cell(row, "raw_regime"),
            effective_regime=cell(row, "effective_regime"),
            adx=cell(row, "adx"),
            decision=cell(row, "decision"),
            votes=cell(row, "votes"),
            pnl=_to_float(cell(row, "pnl")),
            trades=(int(t) if (t := cell(row, "trades")).isdigit() else None),
            strategy_active=cell(row, "strategy_active"),
        )
        existing = merged.get(sess.key)
        if existing is not None:
            # Same session recorded twice: prefer non-empty fields from
            # the LATER row, keep the earlier row's position.
            for name in ("raw_regime", "effective_regime", "adx",
                         "decision", "votes", "strategy_active"):
                value = getattr(sess, name)
                if value:
                    setattr(existing, name, value)
            if sess.pnl is not None:
                existing.pnl = sess.pnl
            if sess.trades is not None:
                existing.trades = sess.trades
        else:
            merged[sess.key] = sess
    return list(merged.values())


def _leg_key(strategy_active: str) -> str:
    s = (strategy_active or "").strip()
    return "" if s.lower() in ("", "idle", "—", "-") else s


def group_episodes(
    sessions: list,
    leg_of: Callable[[str], str] | None = None,
) -> list:
    """Chronological SessionRows → chronological Episodes.

    *leg_of* maps a strategy_active display name to "long"/"short"
    (anything else → "unknown"); None means every non-idle strategy is
    "unknown". The last episode is marked ``open``.

    A DAY row with no strategy recorded (result written before the
    active strategy landed in the CSV) inherits the running episode
    rather than opening an idle one — only NIGHT rows can flip the
    episode to idle, since idle is a classification outcome.
    """
    episodes: list[Episode] = []
    for sess in sessions:
        strategy = _leg_key(sess.strategy_active)
        if strategy:
            leg = leg_of(strategy) if leg_of else "unknown"
            if leg not in ("long", "short"):
                leg = "unknown"
        else:
            leg = "idle"

        current = episodes[-1] if episodes else None
        if (current is not None and sess.strategy_active == ""
                and sess.slot == "DAY"):
            current.sessions.append(sess)
            continue
        if current is not None and current.leg == leg and current.strategy == strategy:
            current.sessions.append(sess)
        else:
            episodes.append(Episode(leg=leg, strategy=strategy, sessions=[sess]))
    if episodes:
        episodes[-1].open = True
    return episodes


def format_votes_cell(votes_cell: str) -> str:
    """"W3:trending-up+W2:trending-up*" → "W3↑+W2↑ 加速"."""
    raw = (votes_cell or "").strip()
    if not raw:
        return ""
    accelerated = raw.endswith("*")
    if accelerated:
        raw = raw[:-1]
    toks = []
    for item in raw.split("+"):
        src, _, direction = item.partition(":")
        toks.append(f"{src}{_ARROW.get(direction, '')}" if src else "")
    out = "+".join(t for t in toks if t)
    if accelerated and out:
        out += " 加速"
    return out


def trend_text(sess: SessionRow) -> str:
    """Bilingual-free trend cell: raw regime, with the unconfirmed state
    spelled out instead of the unreadable "raw→unknown" arrow."""
    raw, eff = sess.raw_regime, sess.effective_regime
    if not raw and not eff:
        return ""
    if raw and eff and raw != eff:
        if eff == "unknown":
            return f"{raw} (未確認 unconfirmed)"
        return f"{raw} (有效 {eff})"
    return raw or eff
