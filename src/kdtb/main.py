from __future__ import annotations

import logging
from pathlib import Path

from kdtb.config import load_settings
from kdtb.logging_setup import setup_logging
from kdtb.storage.db import init_db


def main() -> None:
    settings = load_settings([Path("config/default.yaml")])
    setup_logging(settings.logging.level, settings.logging.json_format)
    log = logging.getLogger("kdtb")
    log.info("loaded config: mode=%s", settings.trading.mode)

    conn = init_db(settings.storage.sqlite_path)
    log.info("initialized sqlite at %s", settings.storage.sqlite_path)
    conn.close()

    log.info("Milestone 1 skeleton ready. No ingestion/strategy/broker wired yet.")


if __name__ == "__main__":
    main()
