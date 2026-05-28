from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", as_json: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    if as_json:
        fmt = '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    else:
        fmt = "%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
