from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class TradingConfig(BaseModel):
    """Position/exposure caps. Used by the risk engine when it scores a
    candidate signal; this project has no execution path, so they bound the
    hypothetical trade the backtest prices, not a real order."""

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

    Later paths override earlier ones (shallow keys deep-merged).
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

    return Settings(**merged)
