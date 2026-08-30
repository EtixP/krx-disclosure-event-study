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

    conn = init_db(settings.storage.sqlite_path)
    log.info("initialized sqlite at %s", settings.storage.sqlite_path)
    conn.close()

    log.info("config loaded and database initialized")


if __name__ == "__main__":
    main()
