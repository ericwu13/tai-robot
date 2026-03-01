"""Application configuration via nested dataclasses + YAML loader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..utils.errors import ConfigError


@dataclass
class CredentialConfig:
    user_id: str = ""
    authority_flag: int = 2  # 0=prod, 1=test+prod, 2=test only

    def get_password(self) -> str:
        """Read password from env var, then try keyring."""
        pwd = os.environ.get("CAPITAL_API_PASSWORD")
        if pwd:
            return pwd
        try:
            import keyring
            pwd = keyring.get_password("tai-robot", self.user_id)
            if pwd:
                return pwd
        except ImportError:
            pass
        raise ConfigError(
            "Password not found. Set CAPITAL_API_PASSWORD env var "
            "or store in keyring: keyring.set_password('tai-robot', '<user_id>', '<password>')"
        )


@dataclass
class AccountConfig:
    branch: str = ""
    account: str = ""

    @property
    def full_account(self) -> str:
        return f"{self.branch}{self.account}"


@dataclass
class TradingConfig:
    mode: str = "paper"       # paper | semi_auto | full_auto
    symbol: str = "TXFD0"
    market_no: int = 2        # 2=T-session, 7=full-session
    default_qty: int = 1
    order_type: str = "2"     # "2"=auto new/close
    trade_type: int = 0       # 0=ROD, 1=IOC, 2=FOK
    price_flag: int = 0       # 0=market, 1=limit


@dataclass
class RiskConfig:
    max_position: int = 2
    max_daily_loss: float = 20000.0
    max_drawdown_pct: float = 5.0
    order_rate_limit: int = 10  # per minute


@dataclass
class StrategyConfig:
    name: str = "ma_crossover"
    params: dict = field(default_factory=dict)
    bar_interval: int = 60  # seconds


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_dir: str = "logs"
    trade_csv: str = "logs/trades.csv"


@dataclass
class AppConfig:
    credentials: CredentialConfig = field(default_factory=CredentialConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _build_dataclass(cls, data: dict):
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    if data is None:
        return cls()
    filtered = {}
    for f in cls.__dataclass_fields__:
        if f in data:
            field_type = cls.__dataclass_fields__[f].type
            val = data[f]
            # Recurse into nested dataclasses
            if isinstance(val, dict) and hasattr(field_type, "__dataclass_fields__"):
                val = _build_dataclass(field_type, val)
            filtered[f] = val
    return cls(**filtered)


def load_config(path: str | Path = "settings.yaml") -> AppConfig:
    """Load configuration from YAML file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}. Copy settings.example.yaml to settings.yaml")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = AppConfig(
        credentials=_build_dataclass(CredentialConfig, raw.get("credentials")),
        account=_build_dataclass(AccountConfig, raw.get("account")),
        trading=_build_dataclass(TradingConfig, raw.get("trading")),
        risk=_build_dataclass(RiskConfig, raw.get("risk")),
        strategy=_build_dataclass(StrategyConfig, raw.get("strategy")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
    )
    return cfg
