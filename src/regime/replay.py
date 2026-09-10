"""Pure re-run of the regime engine over recorded history rows.

Feeds ``regime_history.csv`` NIGHT rows back through
:class:`~src.regime.state_machine.RegimeStateMachine` and
:class:`~src.regime.selector.StrategySelector` under an arbitrary
:class:`~src.regime.state_machine.RegimeConfig`, so a rule change can be
compared against what the live bot actually did before it ships.

No I/O here — ``scripts/replay_regime_rules.py`` owns the CSV reading and
the printing.

Fidelity note: the history CSV stores ``ema_slope`` but not ``ema_50``,
so the classifier's ``direction`` ("bullish" if last_close >= ema_50)
cannot be recovered exactly. It is approximated from the slope sign. That
only reaches the selector's range-bound BIAS branch, which is inert under
the default ``range_bias_action: sit_out`` (both directions sit out).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.daily_report.regime_classifier import RegimeResult
from src.regime.selector import Recommendation, StrategySelector
from src.regime.state_machine import RegimeConfig, RegimeState, RegimeStateMachine


@dataclass
class HistoryRow:
    """One assessed NIGHT row from ``regime_history.csv``."""
    date: str
    adx: float
    plus_di: float
    minus_di: float
    atr_ratio: float
    ema_slope: float
    close: float = 0.0
    # "W2:trending-down+W3:trending-up" split into its parts
    vote_sources: list[str] = field(default_factory=list)
    # What the live bot recorded for this row (parity oracle; "" when the
    # caller does not care).
    recorded_raw: str = ""
    recorded_effective: str = ""
    recorded_decision: str = ""

    @property
    def vote_directions(self) -> list[str]:
        out = []
        for src in self.vote_sources:
            _, _, direction = src.partition(":")
            if direction:
                out.append(direction)
        return out


@dataclass
class ReplaySession:
    """The engine's own view of one replayed session."""
    date: str
    raw: str
    effective: str
    pending_label: str
    pending_count: int
    paused: bool
    flips: int              # flips inside the sliding window after this step
    decision: str           # Recommendation.action
    strategy: str
    reason: str
    recorded_effective: str = ""
    recorded_decision: str = ""


def parse_votes(cell: str) -> list[str]:
    """``"W2:trending-down+W3:trending-up"`` -> the two ``SRC:dir`` parts."""
    return [p for p in (cell or "").split("+") if p.strip()]


def _result_from_row(row: HistoryRow) -> RegimeResult:
    """Rebuild the RegimeResult the state machine would have seen.

    Only ``adx_value`` / ``plus_di_value`` / ``minus_di_value`` /
    ``atr_ratio`` drive the machine; ``ema_slope`` and ``direction`` drive
    the selector's range-bound branch. The rest are descriptive fields
    neither one reads, so they are left at their recorded/neutral values.
    """
    return RegimeResult(
        label=row.recorded_raw,
        trend_strength="",
        volatility="",
        direction="bullish" if row.ema_slope >= 0 else "bearish",
        adx_value=row.adx,
        plus_di_value=row.plus_di,
        minus_di_value=row.minus_di,
        atr_value=0.0,
        atr_ratio=row.atr_ratio,
        ema_50=0.0,
        last_close=row.close,
        ema_slope=row.ema_slope,
    )


def replay(rows: list[HistoryRow], cfg: RegimeConfig,
           state: RegimeState | None = None) -> list[ReplaySession]:
    """Re-run every row through the engine under *cfg*.

    Starts from a fresh :class:`RegimeState` unless one is supplied.
    """
    machine = RegimeStateMachine()
    selector = StrategySelector()
    s = state if state is not None else RegimeState()
    out: list[ReplaySession] = []
    for row in rows:
        s = machine.step(
            s, _result_from_row(row), cfg, row.date,
            vote_directions=row.vote_directions,
            vote_sources=row.vote_sources,
        )
        rec: Recommendation = selector.select(s, cfg)
        # flip_sessions is pruned when a flip is APPENDED, so between
        # flips it can hold entries that have already aged out. Report
        # the in-window count — what max_flips is actually compared to.
        in_window = [c for c in s.flip_sessions
                     if s.session_count - c < cfg.flip_window]
        out.append(ReplaySession(
            date=row.date,
            raw=s.raw_regime,
            effective=s.effective_regime,
            pending_label=s.pending_label,
            pending_count=s.pending_count,
            paused=bool(s.last_features.get("_paused")),
            flips=len(in_window),
            decision=rec.action,
            strategy=rec.strategy_name,
            reason=rec.reason,
            recorded_effective=row.recorded_effective,
            recorded_decision=row.recorded_decision,
        ))
    return out


def diff_sessions(left: list[ReplaySession],
                  right: list[ReplaySession]) -> list[tuple[ReplaySession, ReplaySession]]:
    """Pairs where the two runs disagree on effective regime or decision."""
    return [
        (a, b) for a, b in zip(left, right)
        if a.effective != b.effective or a.decision != b.decision
    ]
