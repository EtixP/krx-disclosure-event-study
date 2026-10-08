"""Execute preserved pre-M0.6 sources in an isolated temporary tree.

Changed source bytes come from the archive; unchanged sources retain their
current bytes and are checked by the replayed artifacts' source manifests.
Only public CSV/JSON data and frozen artifacts are exposed. No DB is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from kdtb.research.baseline import require_new_research_output, write_json

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "artifacts/m0_6/legacy_sources"

# Code is executed in a separate process with the temporary tree as CWD and
# explicit PYTHONPATH; no current learner module is imported into that process.
REPLAY = """
import json
from pathlib import Path
from kdtb.research.baseline import build_learner_buyback_summary, write_json, sha256_file
from scripts.compare_benchmark_adjustment import build_comparison
from scripts.analyze_research_inference import build_report
from scripts.compare_cost_revision import build_comparison as cost_comparison
root = Path.cwd()
outputs = [
    ("m0_1_learner", "artifacts/baselines/pre_revision/learner_buyback_summary.json", build_learner_buyback_summary(root, "pre_revision")),
    ("m0_3", "artifacts/m0_3/benchmark_adjustment_comparison.json", build_comparison()),
    ("m0_5", "artifacts/m0_5/research_inference.json", build_report()),
]
result = {}
for name, frozen, payload in outputs:
    target = root / (name + ".json")
    write_json(target, payload)
    if target.read_bytes() != (root / frozen).read_bytes():
        raise AssertionError(f"historical exact replay differs: {name}")
    result[name] = {"byte_identical": True, "sha256": sha256_file(target)}
# M0.2 predates benchmark CSV enrichment: compare its scientific payload, not
# newer input/source hashes. This limitation predates M0.6.
cost = cost_comparison(before_dir=root / "artifacts/baselines/pre_revision")
write_json(root / "m0_2_current.json", cost)
cost = json.loads((root / "m0_2_current.json").read_text())
frozen_cost = json.loads((root / "artifacts/m0_2/historical_cost_comparison.json").read_text())
for key in ("categories", "buyback_learner", "buyback_intraday", "tax_schedule"):
    if cost[key] != frozen_cost[key]:
        raise AssertionError(f"M0.2 scientific replay differs: {key}")
result["m0_2"] = {"scientific_payload_identical": True, "byte_identical": False,
                   "reason": "pre-existing input enrichment/source drift since M0.2"}
print(json.dumps(result, sort_keys=True))
"""


def replay():
    manifest = json.loads((ARCHIVE.parent / "legacy_source_manifest.json").read_text())
    with tempfile.TemporaryDirectory(prefix="krx-m0-6-historical-") as temp:
        tree = Path(temp)
        for directory in ("src", "scripts"):
            shutil.copytree(
                ROOT / directory,
                tree / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        for record in manifest["sources"]:
            source = ARCHIVE / record["path"]
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != record["sha256"]:
                raise ValueError(f"archived source hash mismatch: {record['path']}")
            shutil.copyfile(source, tree / record["path"])
        for name in ("pyproject.toml", "requirements.txt"):
            shutil.copyfile(ROOT / name, tree / name)
        (tree / "data").mkdir()
        for path in (ROOT / "data").iterdir():
            if path.suffix in {".csv", ".json"}:
                (tree / "data" / path.name).symlink_to(path)
        (tree / "artifacts").mkdir()
        for name in ("baselines", "m0_2", "m0_3", "m0_4", "m0_5"):
            (tree / "artifacts" / name).symlink_to(ROOT / "artifacts" / name)
        env = {**os.environ, "PYTHONPATH": f"{tree / 'src'}:{tree}"}
        run = subprocess.run(
            [sys.executable, "-c", REPLAY],
            cwd=tree,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        return {
            "schema_version": "m0.6_preserved_source_replay_v1",
            "archive_manifest_sha256": hashlib.sha256(
                (ARCHIVE.parent / "legacy_source_manifest.json").read_bytes()
            ).hexdigest(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "db_exposed": False,
            "results": json.loads(run.stdout),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = require_new_research_output(args.output, project_root=ROOT)
    result = replay()
    write_json(output, result, overwrite=False)
    print(json.dumps(result["results"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
