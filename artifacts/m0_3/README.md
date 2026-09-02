# M0.3 benchmark-adjustment artifact

`benchmark_adjustment_comparison.json` is the deterministic comparison between
the verified M0.2 raw-return methodology and M0.3 broad-market abnormal
returns. It covers all seven headline categories, the buyback learner, and the
buyback time-aware entry analysis.

Regenerate it from committed inputs with:

```bash
python -m scripts.compare_benchmark_adjustment
```

The artifact hashes its event-study inputs, benchmark cache/metadata, pinned
filing-time input, M0.2 comparison, and every direct generator source. Its
regression test also reconciles arithmetic and proves the current raw side is
unchanged from M0.2.
