"""Custom exception hierarchy for tai-robot."""


class TaiRobotError(Exception):
    """Base exception for all tai-robot errors."""


class ConfigError(TaiRobotError):
    """Configuration loading or validation error."""


class ConnectionError_(TaiRobotError):
    """API connection failure."""


class LoginError(ConnectionError_):
    """Login to Capital API failed."""


class QuoteError(TaiRobotError):
    """Quote subscription or data error."""


class OrderError(TaiRobotError):
    """Order submission or management error."""


class RiskLimitError(TaiRobotError):
    """Pre-trade risk check rejected the order."""

    def __init__(self, rule_name: str, message: str):
        self.rule_name = rule_name
        super().__init__(f"[{rule_name}] {message}")


class StrategyError(TaiRobotError):
    """Strategy loading or execution error."""
