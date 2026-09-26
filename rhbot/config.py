"""Settings. Hard caps live here. YAML may only tighten them."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from rhbot.errors import ConfigError
from rhbot.money import D

ALLOWED_SYMBOLS = ("BTC-USD", "ETH-USD")

# Spot only. These are not settings. No config key or environment variable turns them off.
PRODUCT = "spot"
LEVERAGE = Decimal("1")
ALLOW_MARGIN = False
ALLOW_SHORT = False

# Config may set a limit equal to these, or stricter. It may not go past them.
# "Stricter" means a smaller risk budget, a higher cost, a larger minimum order,
# or a longer minimum hold.
#
# freeze_drawdown_pct and kill_drawdown_pct are paper-only. A live phase must
# not inherit these looser drawdown limits. Config may only lower them.
HARD_CAPS = {
    "max_position_pct": Decimal("0.50"),
    "max_total_exposure_pct": Decimal("1"),
    "max_daily_loss_pct": Decimal("0.04"),
    # Paper only: freeze new buys at 10% off that book's mark-to-bid peak.
    "freeze_drawdown_pct": Decimal("0.10"),
    # Paper only: hard kill and flatten that book at 40% off its peak.
    "kill_drawdown_pct": Decimal("0.40"),
    "max_trades_per_day": 2,
    "max_quote_age_seconds": 30,
    "max_daily_turnover_pct": Decimal("1"),
    "max_spread_per_side": Decimal("0.02"),
    "min_order_notional": Decimal("10"),
    "min_cost_per_side": Decimal("0.01"),
    "min_hold_days": 7,
}

# Values that try to turn trading on. Anything else in these variables is ignored.
_LIVE_ENV = {
    "RHBOT_LIVE": {"1", "true", "yes", "on", "live"},
    "RHBOT_MODE": {"live"},
    "LIVE_TRADING": {"1", "true", "yes", "on", "live"},
    "ENABLE_LIVE_TRADING": {"1", "true", "yes", "on", "live"},
}


def reject_live_env() -> None:
    """No environment variable can select a live broker."""
    for key, blocked in _LIVE_ENV.items():
        raw = os.environ.get(key, "").strip().lower()
        if raw in blocked:
            raise ConfigError(f"{key} cannot enable live trading; this process is paper-only")


class Settings(BaseModel):
    """Paper bot settings. ``mode`` cannot be anything but paper."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "paper"
    state_dir: Path = Path("state")
    starting_cash: Decimal = Decimal("1000")
    symbols: tuple[str, ...] = ALLOWED_SYMBOLS
    cost_per_side: Decimal = Decimal("0.01")
    max_position_pct: Decimal = Decimal("0.50")
    max_total_exposure_pct: Decimal = Decimal("1")
    max_daily_loss_pct: Decimal = Decimal("0.04")
    # Paper only. Per-book mark-to-bid peak. Config may only tighten these.
    # They must not be copied into a live phase.
    freeze_drawdown_pct: Decimal = Decimal("0.10")
    kill_drawdown_pct: Decimal = Decimal("0.40")
    max_trades_per_day: int = 2
    max_quote_age_seconds: int = 30
    max_daily_turnover_pct: Decimal = Decimal("1")
    max_spread_per_side: Decimal = Decimal("0.02")
    min_order_notional: Decimal = Decimal("10")
    # Chosen before any backtest. The hold is the risk floor. See ARCHITECTURE.md.
    sma_window: int = 200
    trend_band: Decimal = Decimal("0.02")
    min_hold_days: int = 7
    trend_target_weight: Decimal = Decimal("0.50")
    # $1,000 / 52 weeks, rounded down to the cent. One coin per period.
    dca_notional: Decimal = Decimal("19.23")
    loop_seconds: int = 60
    market_data: str = "public"
    public_provider: str = "coinbase"

    @field_validator(
        "starting_cash",
        "cost_per_side",
        "max_position_pct",
        "max_total_exposure_pct",
        "max_daily_loss_pct",
        "freeze_drawdown_pct",
        "kill_drawdown_pct",
        "max_daily_turnover_pct",
        "max_spread_per_side",
        "min_order_notional",
        "trend_band",
        "trend_target_weight",
        "dca_notional",
        mode="before",
    )
    @classmethod
    def _decimals(cls, value: object) -> Decimal:
        try:
            return D(value)
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("state_dir", mode="before")
    @classmethod
    def _path(cls, value: object) -> Path:
        return Path(str(value))

    @field_validator("symbols", mode="before")
    @classmethod
    def _symbols(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise ValueError("symbols must be a list")
        return tuple(str(item) for item in value)

    @field_validator("mode")
    @classmethod
    def _paper_only(cls, value: str) -> str:
        if value != "paper":
            raise ValueError("mode must be paper; live trading is not implemented")
        return value

    @field_validator("market_data")
    @classmethod
    def _market_data(cls, value: str) -> str:
        if value not in ("public", "robinhood"):
            raise ValueError("market_data must be public or robinhood")
        return value

    @field_validator("public_provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        if value not in ("coinbase", "kraken"):
            raise ValueError("public_provider must be coinbase or kraken")
        return value

    @model_validator(mode="after")
    def _tighten_only(self) -> Settings:
        problems: list[str] = []
        decimal_caps = (
            "max_position_pct",
            "max_total_exposure_pct",
            "max_daily_loss_pct",
            "freeze_drawdown_pct",
            "kill_drawdown_pct",
            "max_daily_turnover_pct",
            "max_spread_per_side",
        )
        for name in decimal_caps:
            got = getattr(self, name)
            cap = HARD_CAPS[name]
            if got <= 0:
                problems.append(f"{name} must be positive")
            elif got > cap:
                problems.append(
                    f"{name}={got} loosens the hard cap {cap}; config may only tighten"
                )
        if not 1 <= self.max_quote_age_seconds <= int(HARD_CAPS["max_quote_age_seconds"]):
            problems.append(
                "max_quote_age_seconds must be 1..30; config may only tighten the hard cap"
            )
        if not 0 <= self.max_trades_per_day <= int(HARD_CAPS["max_trades_per_day"]):
            problems.append(
                "max_trades_per_day must be 0..2; config may only tighten the hard cap"
            )
        if self.min_order_notional < HARD_CAPS["min_order_notional"]:
            problems.append("min_order_notional cannot be below 10")
        if self.freeze_drawdown_pct >= self.kill_drawdown_pct:
            problems.append("freeze_drawdown_pct must stay below kill_drawdown_pct")
        if not self.symbols:
            problems.append("symbols cannot be empty")
        unknown = [s for s in self.symbols if s not in ALLOWED_SYMBOLS]
        if unknown:
            problems.append(f"symbols not on the allowlist: {unknown}")
        if len(set(self.symbols)) != len(self.symbols):
            problems.append("symbols contains a duplicate")
        if self.starting_cash <= 0:
            problems.append("starting_cash must be positive")
        if not HARD_CAPS["min_cost_per_side"] <= self.cost_per_side <= Decimal("0.05"):
            problems.append("cost_per_side must be at least 0.01 and at most 0.05")
        if not 2 <= self.sma_window <= 400:
            problems.append("sma_window must be between 2 and 400")
        if not Decimal("0") <= self.trend_band <= Decimal("0.20"):
            problems.append("trend_band must be between 0 and 0.20")
        if not int(HARD_CAPS["min_hold_days"]) <= self.min_hold_days <= 90:
            problems.append("min_hold_days must be at least 7 and at most 90")
        if self.trend_target_weight <= 0 or self.trend_target_weight > self.max_position_pct:
            problems.append("trend_target_weight must be positive and within max_position_pct")
        if self.dca_notional < self.min_order_notional:
            problems.append("dca_notional is below min_order_notional")
        if self.dca_notional > self.starting_cash:
            problems.append("dca_notional exceeds starting cash")
        if not 10 <= self.loop_seconds <= 3600:
            problems.append("loop_seconds must be between 10 and 3600")
        if problems:
            raise ValueError("; ".join(problems))
        return self


def resolve_config_path(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("RHBOT_CONFIG")
    if env:
        path = Path(env)
        if not path.exists():
            raise ConfigError(f"RHBOT_CONFIG file not found: {path}")
        return path
    default = Path("config.yaml")
    if default.exists():
        return default
    return None


def load_settings(config: str | None = None, state_dir: str | None = None) -> Settings:
    reject_live_env()
    path = resolve_config_path(config)
    data: dict = {}
    if path is not None:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be a mapping")
        data = loaded
    if state_dir:
        data["state_dir"] = state_dir
    elif os.environ.get("RHBOT_STATE_DIR") and "state_dir" not in data:
        data["state_dir"] = os.environ["RHBOT_STATE_DIR"]
    try:
        return Settings(**data)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
