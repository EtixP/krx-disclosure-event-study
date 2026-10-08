# M0.6 evidence

- `learner_chronology_v1.json`: strict daily buyback comparison, actual source and
  input hashes, admission exclusions, maturity-stage diagnostics and decisions.
- `legacy_source_manifest.json` and `legacy_sources/`: ten byte-exact pre-M0.6
  generator-source snapshots. These are historical reference code, not default
  learner entry points. Do not format or update the archived files.
- `preserved_source_replay_v1.json`: evidence from executing those old sources in
  a temporary source tree with no database exposed. M0.1 learner/M0.3/M0.5
  reproduce exactly; M0.2 scientific payload reproduces after canonical
  serialization, with its pre-existing input/source provenance drift retained.

Current compatibility builders record their actual current source hashes;
archived source hashes are never substituted for newly executed code.
See `docs/history/M0.6.md` for methodology, commands, scientific changes and
remaining limits. All new evidence is exploratory and uses reconstructed daily
observations, not proven original-vintage availability or executable fills.

After verifier-returned repairs, M0.2/M0.3/M0.5 no-argument commands verify
frozen hashes without generating or writing. Explicit current-source replays
and the preserved-source runner require new paths outside repository
`data/`, `sources/`, and `artifacts/` and publish without replacing an existing
file. The historical intraday default reads only the pinned buyback filing-time
CSV, never the current database. Scientific M0.6 results are unchanged.
