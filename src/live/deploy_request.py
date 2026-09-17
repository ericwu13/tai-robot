"""DeployRequest — the resolved inputs of a live-bot deploy.

The GUI's session-picker dialog produced a positional 10-tuple that
``_deploy_live`` unpacked in place. The headless CLI needs to build the
same inputs without a dialog, so the tuple now has a named shape shared
by both callers. ``from_dialog_tuple`` / ``to_dialog_tuple`` keep the
dialog's positional contract intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field


TRADING_MODES = ("paper", "semi_auto", "auto")

# Positional order of the dialog result tuple — do not reorder.
_DIALOG_FIELDS = (
    "bot_name", "resume_session", "trading_mode", "loss_limit",
    "regime_enabled", "regime_long", "regime_short",
    "news_enabled", "news_tier2_enabled", "news_directional",
)


@dataclass
class DeployRequest:
    bot_name: str
    resume_session: dict | None = None
    trading_mode: str = "paper"
    loss_limit: str | int = "1000"
    regime_enabled: bool = False
    regime_long: str = ""
    regime_short: str = ""
    news_enabled: bool = False
    news_tier2_enabled: bool = False
    news_directional: bool = False
    # Not part of the dialog tuple: the CLI records where the request came
    # from so the debug log can tell a headless deploy from a GUI one.
    origin: str = field(default="gui", compare=False)

    @classmethod
    def from_dialog_tuple(cls, result: tuple) -> "DeployRequest":
        if len(result) != len(_DIALOG_FIELDS):
            raise ValueError(
                f"dialog tuple has {len(result)} fields, "
                f"expected {len(_DIALOG_FIELDS)}")
        return cls(**dict(zip(_DIALOG_FIELDS, result)))

    def to_dialog_tuple(self) -> tuple:
        return tuple(getattr(self, name) for name in _DIALOG_FIELDS)

    @property
    def is_resume(self) -> bool:
        return self.resume_session is not None
