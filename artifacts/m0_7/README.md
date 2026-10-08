# M0.7 historical data-readiness audit

`historical_data_readiness_v1.json` is the deterministic, machine-readable
decision artifact for M0.7. It audits the seven frozen event-study category
CSVs against only hash-pinned local evidence. It does not read SQLite, contact a
provider, repair source rows, select canonical overlap copies, or train a model.

The artifact records 28,979 category-row copies and 28,737 unique receipts. It
retains both category copies for every cross-category receipt, including the
four receipts whose frozen fields and admission outcomes conflict. Its gate is
`NO_GO` because the pinned inputs do not supply all-row point-in-time security
identity, exact disclosure-version evidence, or versioned raw daily bars, and
because cross-category copy conflicts remain unresolved.

The historical intraday limitation is an informational prospective-collection
status. It is not substituted for the daily-label evidence gate.

Generate an identical copy only at a fresh path outside `data/`, `sources/` and
`artifacts/`:

```bash
.venv/bin/python -m scripts.audit_historical_data_readiness \
  --output /tmp/m0_7_historical_data_readiness_v1.json
```

The command rejects changed pinned inputs, existing destinations, protected
paths, symlink aliases into protected paths, and replacement races. It opens
every output-directory component from the filesystem root without following
symlinks, retains those directory handles through publication, and rolls back
the new link if the visible component chain changes.
