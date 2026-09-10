"""P5 — join W2 fires to what the bot actually did with them.

A W2 fire has two very different afterlives and they must not be scored
as one number:

- The **intra-session** afterlife: the 1/2/4/8-hour P&L columns of the
  alerts sheet, which measure the fire where its edge lives.
- The **nightly-lane** afterlife: the fire writes a vote, the vote is
  read at the ~04:58 classification of the night it names, and whatever
  leg that classification picks only trades from the NEXT session on.

This module does the second join, and only the second — the sheet
already carries the first.  Given the fires, the bot's
``regime_history.csv`` rows and its 1-min bars, it answers, per fire:
which night the vote targeted, whether a classification row for that
night exists, what it decided, which session the resulting leg first
traded, what that session made, and which way the market actually went
over the window the leg was live.

Everything here is pure — no filesystem, no clock, no argparse.  The
reading and writing lives in ``scripts/score_fires.py``, so the join
logic can be tested against a handful of synthetic rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# The session-identity boundary used everywhere in this codebase: a
# NIGHT session is named by its OPEN date, and the night that opened on
# D runs until 05:00 on D+1.  A fire at 02:30 therefore belongs to
# yesterday's night, not today's.
NIGHT_BOUNDARY_HOUR = 5

DAY_OPEN = (8, 45)      # TAIFEX day session opens 08:45 TPE
DAY_CLOSE = (13, 45)    # ...and closes 13:45
NIGHT_OPEN = (15, 0)    # night session opens 15:00 ...
NIGHT_CLOSE = (5, 0)    # ...and closes 05:00 the next calendar day

PNL_COLUMNS = ("1h淨損益", "2h淨損益", "4h淨損益", "8h淨損益", "收盤淨損益")

OUT_HEADER = (
    "時間TPE", "tier", "方向",
    *PNL_COLUMNS,
    "night_key", "consumed_row", "decision", "strategy_active", "votes",
    "leg_date", "leg_day_pnl", "leg_night_pnl", "leg_window_move",
)

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


@dataclass
class Fire:
    """One row of the W2 alerts sheet."""
    time_tpe: str
    tier: str = ""
    direction: str = ""
    pnl: dict = field(default_factory=dict)   # column name -> raw cell


@dataclass
class HistoryRow:
    """One ``regime_history.csv`` row, reduced to the columns we join on."""
    date: str
    session: str              # "DAY" | "NIGHT"
    decision: str = ""
    strategy_active: str = ""
    votes: str = ""
    pnl: str = ""
    raw_regime: str = ""      # "" on result-only rows (no classification)


@dataclass
class ScoredFire:
    fire: Fire
    night_key: str = ""
    consumed_row: str = ""
    decision: str = ""
    strategy_active: str = ""
    votes: str = ""
    leg_date: str = ""
    leg_day_pnl: str = ""
    leg_night_pnl: str = ""
    leg_window_move: str = ""

    def to_row(self) -> list:
        return [
            self.fire.time_tpe, self.fire.tier, self.fire.direction,
            *(self.fire.pnl.get(c, "") for c in PNL_COLUMNS),
            self.night_key, self.consumed_row, self.decision,
            self.strategy_active, self.votes, self.leg_date,
            self.leg_day_pnl, self.leg_night_pnl, self.leg_window_move,
        ]


def parse_tpe_time(value: str) -> datetime | None:
    """Parse a sheet timestamp.  Returns None on anything unrecognised."""
    text = str(value or "").strip()
    if not text:
        return None
    # Drop a trailing timezone offset if the sheet kept one — every value
    # in this file is TPE by construction.
    for sep in ("+", "Z"):
        if sep in text[10:]:
            text = text[:10] + text[10:].split(sep)[0]
            text = text.strip()
            break
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def night_key_for(when: datetime) -> str:
    """The ``"YYYY-MM-DD|NIGHT"`` session a fire at *when* targets.

    Hour >= 5 → tonight (the night opening today); earlier → the night
    that opened yesterday and has not closed yet.
    """
    date = when.date() if when.hour >= NIGHT_BOUNDARY_HOUR else (
        when - timedelta(days=1)).date()
    return f"{date.isoformat()}|NIGHT"


def night_key_date(night_key: str) -> str:
    return night_key.split("|")[0]


def _is_up(direction: str) -> bool | None:
    """True for a bullish fire, False for bearish, None if unreadable."""
    text = str(direction or "").strip().lower()
    if not text:
        return None
    if any(t in text for t in ("up", "bull", "long", "多", "漲", "↑")):
        return True
    if any(t in text for t in ("down", "bear", "short", "空", "跌", "↓")):
        return False
    return None


def find_classification(history: list, date: str) -> HistoryRow | None:
    """The NIGHT classification row for *date*, if the bot wrote one.

    Result-only rows (P&L recorded but ``raw_regime`` blank — the bot was
    not classifying that night) do not count as a consumed vote.  The
    LAST matching row wins: ``record_session_result`` updates in place,
    but a pre-idempotent history can hold duplicates.
    """
    match = None
    for row in history:
        if row.session == "NIGHT" and row.date == date and row.raw_regime:
            match = row
    return match


def next_night_after(history: list, date: str) -> str:
    """Date of the first NIGHT row strictly after *date*.

    This is where a holiday gap takes care of itself: the bot writes no
    rows on a closed day, so the "next" night is simply the next one
    that exists.  Rows are sorted here rather than assumed sorted — a
    history that was appended to across restarts is only mostly ordered.
    """
    later = sorted({r.date for r in history
                    if r.session == "NIGHT" and r.date > date})
    return later[0] if later else ""


def session_pnl(history: list, date: str, session: str) -> str:
    """The recorded P&L cell for (date, session) — "" when absent."""
    value = ""
    for row in history:
        if row.date == date and row.session == session and row.pnl != "":
            value = row.pnl
    return value


def _at(date_str: str, hm: tuple) -> datetime | None:
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return base.replace(hour=hm[0], minute=hm[1])


def day_open_price(bars: list, date: str) -> float | None:
    """Open of the first day-session bar on *date* (08:45 onwards).

    Not "the bar stamped exactly 08:45": a bot that started mid-session,
    or a session whose first minute had no trade, still has a first bar,
    and taking it is more useful than reporting a hole.  Bars carry
    OPEN-time stamps (the codebase-wide convention).
    """
    start, end = _at(date, DAY_OPEN), _at(date, DAY_CLOSE)
    if start is None:
        return None
    window = [b for b in bars if start <= b[0] < end]
    return float(window[0][1]) if window else None


def night_close_price(bars: list, date: str) -> float | None:
    """Close of the last bar of the night that OPENED on *date*.

    The window straddles midnight (15:00 → 05:00 next day), which is
    exactly why the night is identified by its open date everywhere.
    """
    start = _at(date, NIGHT_OPEN)
    if start is None:
        return None
    end = _at(date, NIGHT_CLOSE)
    end = (end + timedelta(days=1)) if end else None
    window = [b for b in bars if start <= b[0] < end]
    return float(window[-1][4]) if window else None


def leg_window_move(bars: list, leg_date: str, direction: str) -> str:
    """Signed points the market moved over the leg's first full day.

    From the day-session open on *leg_date* to the close of the night
    that opens the same evening — the window the swapped leg was first
    live for.  Signed so POSITIVE always means "the fire was right":
    an up fire keeps the raw move, a down fire flips it.  "" when either
    end is missing (a holiday, a bot that was down, a gap in the CSVs)
    or the fire's direction is unreadable.
    """
    up = _is_up(direction)
    if up is None or not leg_date:
        return ""
    start = day_open_price(bars, leg_date)
    end = night_close_price(bars, leg_date)
    if start is None or end is None:
        return ""
    move = end - start
    return f"{move if up else -move:+.0f}"


def score_fires(fires: list, history: list, bars: list) -> list:
    """Join every fire to its night, its classification and its leg.

    *fires* are ``Fire`` rows, *history* ``HistoryRow`` rows and *bars*
    ``(datetime, open, high, low, close, volume)`` tuples sorted by
    datetime.  A fire whose timestamp will not parse still comes back —
    with an empty ``night_key`` — so nothing silently disappears from
    the output.
    """
    bars = sorted(bars, key=lambda b: b[0])
    out = []
    for fire in fires:
        scored = ScoredFire(fire=fire)
        when = parse_tpe_time(fire.time_tpe)
        if when is None:
            out.append(scored)
            continue

        scored.night_key = night_key_for(when)
        date = night_key_date(scored.night_key)

        classification = find_classification(history, date)
        if classification is not None:
            scored.consumed_row = classification.date
            scored.decision = classification.decision
            scored.strategy_active = classification.strategy_active
            scored.votes = classification.votes

        scored.leg_date = next_night_after(history, date)
        if scored.leg_date:
            scored.leg_day_pnl = session_pnl(history, scored.leg_date, "DAY")
            scored.leg_night_pnl = session_pnl(history, scored.leg_date, "NIGHT")
            scored.leg_window_move = leg_window_move(
                bars, scored.leg_date, fire.direction)
        out.append(scored)
    return out
