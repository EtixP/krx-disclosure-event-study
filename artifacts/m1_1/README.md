# M1.1 normalization-prevalence artifact

`normalization_prevalence.json` measures how often the pinned local DART corpus
contains provider-defined update prefixes, cancellation markers, and same-day
same-title collisions. It deliberately does **not** assign canonical lineage
from title similarity. Canonical lineage uses DART viewer `family`, `att`, and
`ref` receipt relationships; `att` connects an attachment correction to the
parent revision family returned on the same page.

Regenerate against the same database bytes with:

```bash
PYTHONPATH=src python -m scripts.audit_event_normalization \
  --db data/kdtb.db \
  --out artifacts/m1_1/normalization_prevalence.json
```

Pinned input database SHA-256:
`265d888ac2ba4e205c95a1611e0ba260dbea5e5a5237761995bbd9444fef5137`.

Committed artifact SHA-256:
`33ff25172f692b4b91dacc88f8776d8997444a8aea3561780c3f4a5de1551684`.

The source database is not committed. A different database snapshot is
expected to produce a different artifact and must be described as a data-version
change, not silently substituted for this one.
