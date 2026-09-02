"""Generate or verify deterministic research-state baseline artifacts.

Examples:
    python -m scripts.verify_research_state
    python -m scripts.verify_research_state --generate --output-dir /tmp/m0_1_replay

The one-time input capture requires the ignored local research database:
    python -m scripts.verify_research_state --refresh-buyback-times-from-db --generate \
        --output-dir /tmp/m0_1_db_replay
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from kdtb.research.baseline import (
    BUYBACK_TIMES_INPUT,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SNAPSHOT_NAME,
    capture_buyback_filing_times,
    generate_snapshot,
    verify_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="regenerate JSON artifacts from the recorded inputs before verification",
    )
    parser.add_argument(
        "--refresh-buyback-times-from-db",
        action="store_true",
        help="refresh the pinned buyback filing-time input from the local SQLite DB",
    )
    parser.add_argument("--db", default="data/kdtb.db", help="local SQLite source DB")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="snapshot directory, relative to repository root unless absolute",
    )
    parser.add_argument("--snapshot-name", default=DEFAULT_SNAPSHOT_NAME)
    parser.add_argument(
        "--filing-times-input",
        default=None,
        help=(
            "pinned buyback filing-time CSV to copy into a new snapshot directory; "
            "relative paths are resolved from repository root"
        ),
    )
    parser.add_argument(
        "--check-inputs",
        action="store_true",
        help="also require current external data and generator hashes to match the manifest",
    )
    return parser.parse_args()


def _resolve_from_root(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def require_nonhistorical_output(output_dir: Path, *, mutating: bool) -> None:
    """Prevent later milestones from rewriting the verified M0.1 evidence."""
    immutable = (PROJECT_ROOT / DEFAULT_OUTPUT_DIR).resolve()
    if mutating and output_dir.resolve() == immutable:
        raise ValueError(
            "the verified M0.1 snapshot is immutable; choose a different "
            "--output-dir for regeneration"
        )


def main() -> int:
    args = parse_args()
    output_dir = _resolve_from_root(args.output_dir)
    require_nonhistorical_output(
        output_dir,
        mutating=args.generate or args.refresh_buyback_times_from_db,
    )

    captured = None
    if args.refresh_buyback_times_from_db:
        captured = capture_buyback_filing_times(
            db_path=_resolve_from_root(args.db),
            event_csv=PROJECT_ROOT / "data/event_study_buyback.csv",
            output_path=output_dir / BUYBACK_TIMES_INPUT,
        )

    if args.generate:
        result = generate_snapshot(
            project_root=PROJECT_ROOT,
            output_dir=output_dir,
            snapshot_name=args.snapshot_name,
            filing_times_source=(
                _resolve_from_root(args.filing_times_input)
                if args.filing_times_input
                else None
            ),
        )
        if args.check_inputs:
            result = verify_snapshot(
                output_dir=output_dir,
                project_root=PROJECT_ROOT,
                check_external_inputs=True,
            )
    else:
        result = verify_snapshot(
            output_dir=output_dir,
            project_root=PROJECT_ROOT,
            check_external_inputs=args.check_inputs,
        )

    if captured is not None:
        result["buyback_filing_times_captured"] = captured
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
