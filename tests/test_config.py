from __future__ import annotations

from pathlib import Path

from kdtb.config import Settings, load_settings


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = REPO_ROOT / "config" / "default.yaml"


def test_default_config_loads():
    settings = load_settings([DEFAULT_YAML])
    assert isinstance(settings, Settings)
    assert settings.trading.max_order_value_krw == 30000
    assert settings.strategy.major_supply_contract.min_contract_to_revenue_ratio == 0.08


def test_later_config_overrides_earlier(tmp_path):
    override = tmp_path / "override.yaml"
    override.write_text("trading:\n  max_order_value_krw: 50000\n", encoding="utf-8")
    settings = load_settings([DEFAULT_YAML, override])
    # overridden key wins, sibling keys in the same block survive the deep-merge
    assert settings.trading.max_order_value_krw == 50000
    assert settings.trading.max_trades_per_day == 3
