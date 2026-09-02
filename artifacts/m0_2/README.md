# M0.2 historical-cost comparison

`historical_cost_comparison.json` compares the immutable M0.1 flat-cost
baseline with current date/market-aware research outputs.

Verify the frozen artifact checksum from the repository root:

```bash
python -m scripts.compare_cost_revision
```

The artifact records hashes for every event-study input and generator source it
used at M0.2 verification. `tests/test_cost_revision_artifact.py` pins the full
artifact SHA-256 and reconciles its before/after arithmetic. Later source or
input changes must not update those historical hashes.

To recompute the comparison against the current tree for diagnosis, write it to
a separate path:

```bash
python -m scripts.compare_cost_revision --output /tmp/m0_2_replay.json
```

The script refuses to overwrite the verified artifact.

The M0.1 pre-revision artifacts remain under `artifacts/baselines/pre_revision`.
Their `--check-inputs` verification is expected to report drift after M0.2; do
not rewrite that historical manifest to make it match current sources.
