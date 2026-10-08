# M0.8 official-source feasibility artifact

`buyback_source_feasibility_v1.json` is the deterministic result from the
approved frozen canary contract. It contains no authenticated provider payload
or provider-returned field value. Raw responses and envelopes, once an
authorized probe is possible, belong only in the gitignored `local_evidence/`
store with user-only permissions.

The checked-in artifact is intentionally `BLOCKED`: the implementation
environment had none of the six separate KRX service approvals, so it made zero
authenticated calls and reports one `AUTH_REQUIRED` blocker per service. The
independent production decision is `NO_GO_LICENSE` because no retained written
agreement supersedes the public noncommercial terms. The overall ML gate
remains `NO_GO`.

Artifact SHA-256: `f865ea2276cc57521bf9ea2cd8226a2285adf2c24ca30006276de402023f13b8`.

Reproduce the bytes from a minimal, credential-free environment:

```bash
env -i PATH="$PATH" .venv/bin/python -m scripts.assess_official_source_feasibility \
  --output /tmp/m0_8_buyback_source_feasibility_v1.json
```

The command refuses existing destinations and any destination under `data/`,
`sources/` or `artifacts/`. Compare the new file to the committed artifact; do
not overwrite the committed evidence in place.
