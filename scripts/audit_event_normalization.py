"""Generate the deterministic M1.1 event-normalization prevalence audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from kdtb.events.audit import audit_normalization_prevalence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATOR_SOURCES = (
    "scripts/audit_event_normalization.py",
    "src/kdtb/events/audit.py",
    "src/kdtb/events/normalizer.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_report(database_path: Path) -> dict:
    input_sha256 = _sha256(database_path)
    uri = f"file:{database_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        prevalence = audit_normalization_prevalence(conn)
    if _sha256(database_path) != input_sha256:
        raise ValueError("DART database changed while the prevalence audit was running")
    return {
        "schema_version": 1,
        "milestone": "M1.1",
        "purpose": "measure normalization need before storage or lineage overengineering",
        "input": {
            "path": database_path.as_posix(),
            "sha256": input_sha256,
        },
        "generator_sources": [
            {"path": path, "sha256": _sha256(PROJECT_ROOT / path)}
            for path in GENERATOR_SOURCES
        ],
        "method": {
            "authoritative_lineage": (
                "DART viewer family/att/ref receipt relationships"
            ),
            "non_authoritative_diagnostics": (
                "same-title and same-date counts measure prevalence only"
            ),
        },
        **prevalence,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/kdtb.db"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/m1_1/normalization_prevalence.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
