"""One Saturday's retire review, shared by every exit that reached a verdict.

The weekly pipeline used to run ``assess_retirement`` only after a full
candidate validation. A pre-registered knob grid that selects nothing
returned before that point, so a decaying incumbent whose whole knob
neighbourhood had stopped beating it was never flagged "not improvable":
the week was not recorded and the retire check never ran.

``run_retire_review`` is that block, moved out of the Tk worker so both
the validation exit and the grid no-pick exit call the same code:

* reads prior relative-gate weeks from the changelog,
* records this week (relative gate only) — a changelog marker line,
* reads applied structural-lane graduations,
* runs ``assess_retirement`` on the incumbent's trades and writes the
  recommend-only text to the log, the changelog and the report list,
* runs the structural incubation follow-up when a lane ledger exists.

It is recommend-only, exactly like the code it replaces. It never
disables a bot, writes settings, or changes a pool status. It never
raises: a failure is logged and an empty report comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .paper_gate import (
    RetireConfig,
    WeekRecord,
    assess_retirement,
    emit_retire_recommendation,
    relative_eval_summary,
    weeks_from_changelog,
)


@dataclass
class RetireReview:
    """What one review produced. ``evaluated_at`` is the history ``ts``."""
    evaluated_at: str
    report_lines: list = field(default_factory=list)
    recommend: bool = False


def _say(log, message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def run_retire_review(*, base_trades, strategy_name: str, version: str,
                      gate_mode: str, verdict_passed: bool,
                      retire_config: RetireConfig | None,
                      structural_ledger: str = "",
                      run_followup: bool = False,
                      log=None, now: datetime | None = None) -> RetireReview:
    """Record this Saturday and run the retire check. Never raises.

    ``gate_mode`` is the resolved promotion-gate mode. Only ``relative``
    records a week: skips and absolute (real-money) runs neither count
    nor reset the not-improvable streak (#188). ``structural_ledger`` is
    the resolved lane ledger path; ``run_followup`` says whether the
    structural incubation follow-up runs (the caller's rule).
    """
    moment = now or datetime.now()
    review = RetireReview(evaluated_at=moment.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        append_changelog = None
        prior_weeks: list[WeekRecord] = []
        try:
            from src.daily_report.changelog import (
                append_changelog as _append_changelog,
                load_changelog,
            )
            append_changelog = _append_changelog
            prior_weeks = weeks_from_changelog(load_changelog(), strategy_name)
        except Exception as hist_err:
            _say(log, "EVO retire history unread: "
                      f"[{type(hist_err).__name__}] {hist_err}")
        weeks = list(prior_weeks)
        if gate_mode == "relative":
            weeks.append(WeekRecord(
                at=review.evaluated_at, evaluated=True,
                passed=bool(verdict_passed)))
            if append_changelog is not None:
                try:
                    append_changelog(
                        strategy_name=strategy_name,
                        version_before=version,
                        version_after=version,
                        change_summary=relative_eval_summary(
                            bool(verdict_passed), review.evaluated_at),
                        initiated_by="ai",
                    )
                except Exception as rec_err:
                    _say(log, "EVO relative-gate record failed: "
                              f"[{type(rec_err).__name__}] {rec_err}")
        grad_stamps: list[str] = []
        try:
            from .structural_lane import applied_graduation_stamps, load_ledger
            grad_stamps = applied_graduation_stamps(load_ledger(structural_ledger))
        except Exception as stamp_err:
            _say(log, "EVO graduation stamps unread: "
                      f"[{type(stamp_err).__name__}] {stamp_err}")
            grad_stamps = []
        rec = assess_retirement(
            list(base_trades or []),
            weeks=weeks,
            structural_graduations=grad_stamps,
            now=moment,
            config=retire_config,
        )
        review.recommend = bool(rec.recommend)
        emit_retire_recommendation(
            rec,
            log=log,
            changelog=(append_changelog if rec.recommend else None),
            report=review.report_lines,
            strategy_name=strategy_name,
            version=version,
        )
        if run_followup:
            from .structural_lane import structural_saturday_followup
            structural_saturday_followup(
                structural_ledger,
                observations=(),
                log=log,
                changelog=append_changelog,
                report=review.report_lines,
                strategy_name=strategy_name,
                version=version,
            )
    except Exception as retire_err:
        _say(log, "EVO retire recommendation failed: "
                  f"[{type(retire_err).__name__}] {retire_err}")
    return review
