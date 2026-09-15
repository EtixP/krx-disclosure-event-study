# M0.5 research-inference artifact

`research_inference.json` adds deterministic issuer-clustered percentile
bootstrap intervals to the seven realistic T+1-to-T+5 category means. It also
reports fixed 1% and 5% top-tail, bottom-tail, and symmetric-trim stress tests
for the historical buyback timing result.

The bootstrap resamples whole `stock_code` histories and carries all events for
each sampled issuer into a draw. Its estimand remains the event-weighted mean
used by the headline research, but repeated filings are not resampled as if
they were independent issuers. The 95% intervals are pointwise descriptive
intervals, not multiplicity-adjusted confirmatory tests.

Regenerate from committed event-study data and the pinned buyback filing-time
slice with:

```bash
python -m scripts.analyze_research_inference
```

The report hashes all inputs, direct and material transitive generator sources,
and the verified M0.1–M0.4 artifacts. It uses 10,000 resamples with
`random_state=0` and canonical JSON serialization.

Every current historical category, timing, blacklist, and subgroup claim is
labeled `exploratory`. The empty `pre_specified` and `confirmatory` lists are
intentional: fixing a reanalysis procedure does not retroactively preregister a
hypothesis on repeatedly inspected data.
