from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

TradingMode = Literal["BACKTEST", "PAPER", "LIVE_TINY", "LIVE"]


class TradingConfig(BaseModel):
    mode: TradingMode = "PAPER"
    allow_live_orders: bool = False
    max_order_value_krw: int = Field(gt=0)
    max_daily_loss_krw: int = Field(gt=0)
    max_open_positions: int = Field(ge=1)
    max_trades_per_day: int = Field(ge=1)
    force_exit_before_close: bool = True


class StrategyParams(BaseModel):
    enabled: bool = True
    min_contract_to_revenue_ratio: float = Field(ge=0)
    min_llm_confidence: float = Field(ge=0, le=1)
    max_price_move_after_disclosure_pct: float = Field(ge=0)
    stop_loss_pct: float = Field(gt=0)
    take_profit_pct: float = Field(gt=0)
    # Loop-7 additions, both walk-forward validated on 24 months of data.
    kospi_only: bool = False
    excluded_counterparty_types: list[str] = Field(default_factory=list)


class StrategyConfig(BaseModel):
    major_supply_contract: StrategyParams


class MarketConfig(BaseModel):
    max_spread_pct: float = Field(ge=0)
    min_avg_daily_trading_value_krw: int = Field(ge=0)
    max_disclosure_age_minutes: int = Field(ge=0)


class LLMConfig(BaseModel):
    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = Field(gt=0)


class StorageConfig(BaseModel):
    sqlite_path: str = "data/kdtb.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    json_format: bool = False


class Settings(BaseModel):
    trading: TradingConfig
    strategy: StrategyConfig
    market: MarketConfig
    llm: LLMConfig
    storage: StorageConfig = StorageConfig()
    logging: LoggingConfig = LoggingConfig()


class LiveTradingNotAuthorizedError(RuntimeError):
    """Raised when config requests live trading but env hasn't explicitly enabled it."""


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(
    config_paths: list[Path] | None = None,
    env_path: Path | None = None,
) -> Settings:
    """Load YAML config(s) and .env, then validate.

    Later paths override earlier ones (shallow keys deep-merged). Live trading
    requires both `trading.allow_live_orders: true` in YAML AND
    `ENABLE_LIVE_TRADING=true` in the environment — otherwise raises.
    """
    if config_paths is None:
        config_paths = [Path("config/default.yaml")]
    if env_path is not None and env_path.exists():
        load_dotenv(env_path)
    elif Path(".env").exists():
        load_dotenv(".env")

    merged: dict = {}
    for path in config_paths:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, data)

    settings = Settings(**merged)
    _enforce_live_trading_guard(settings)
    return settings


def _enforce_live_trading_guard(settings: Settings) -> None:
    wants_live = settings.trading.mode in ("LIVE_TINY", "LIVE") or settings.trading.allow_live_orders
    env_enabled = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    if wants_live and not env_enabled:
        raise LiveTradingNotAuthorizedError(
            "Config requests live trading but ENABLE_LIVE_TRADING is not 'true' in env. "
            "Refusing to load."
        )
