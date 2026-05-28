from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdtb.config import (
    LiveTradingNotAuthorizedError,
    Settings,
    load_settings,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = REPO_ROOT / "config" / "default.yaml"


def test_default_config_loads_in_paper_mode(monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    settings = load_settings([DEFAULT_YAML])
    assert isinstance(settings, Settings)
    assert settings.trading.mode == "PAPER"
    assert settings.trading.allow_live_orders is False
    assert settings.trading.max_order_value_krw == 30000
    assert settings.strategy.major_supply_contract.min_contract_to_revenue_ratio == 0.08


def test_live_tiny_requires_env_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    live_yaml = tmp_path / "live.yaml"
    live_yaml.write_text(
        (DEFAULT_YAML.read_text())
        .replace("mode: PAPER", "mode: LIVE_TINY")
        .replace("allow_live_orders: false", "allow_live_orders: true"),
        encoding="utf-8",
    )
    with pytest.raises(LiveTradingNotAuthorizedError):
        load_settings([live_yaml])


def test_live_tiny_loads_when_env_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    live_yaml = tmp_path / "live.yaml"
    live_yaml.write_text(
        (DEFAULT_YAML.read_text())
        .replace("mode: PAPER", "mode: LIVE_TINY")
        .replace("allow_live_orders: false", "allow_live_orders: true"),
        encoding="utf-8",
    )
    settings = load_settings([live_yaml])
    assert settings.trading.mode == "LIVE_TINY"
    assert settings.trading.allow_live_orders is True
