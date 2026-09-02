# M0.4 price-adjustment audit artifact

`price_adjustment_audit.json` records the explicit M0.4 research policy and
the audit of the bonus-issue and rights-offering datasets. It compares their
current headline outputs to the verified M0.3 artifact and summarizes the
captured fresh-provider cases in `provider_observations.json`.

`dart_right_drop_calendar.json` pins the 499 rights-drop disclosures selected
from the ignored local DART database. This raw-source calendar is deliberately
separate from the title-filtered event-study category files: their union has
441 rights-drop records and finds 70 crossed return windows, while the pinned
DART slice finds 73. The artifact reports both scopes and the three additional
raw-source crossings.

Regenerate the deterministic audit from pinned inputs with:

```bash
python -m scripts.audit_price_adjustments
```

Refresh the raw-source calendar from the same local DART snapshot, then rebuild
the audit, with:

```bash
python -m scripts.audit_price_adjustments \
  --capture-right-drops-from-db data/kdtb.db
```

The provider observation file is dated raw evidence, not a promise that a
future vendor refetch will return the same absolute prices. Its purpose is to
preserve the adjustment vintage that exposed the fivefold `300120` revision,
including the rights-offering window missed by the original per-category
calendar.
