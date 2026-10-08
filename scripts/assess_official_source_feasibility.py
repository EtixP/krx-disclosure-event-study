"""Generate the M0.8 official-source feasibility decision artifact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from kdtb.data.official_source_feasibility import (
    CORE_KRX_CONTRACTS,
    OPTIONAL_CONTRACTS,
    canary_request_budget,
    decide_production,
    decide_research,
    file_sha256,
    service_access,
    verify_canary_definition,
)
from scripts.audit_historical_data_readiness import write_new_json


ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "docs/history/M0.8-canary-v1.json"
SCHEMA_VERSION = "m0.8_buyback_source_feasibility_v1"
GENERATOR_SOURCES = (
    "scripts/assess_official_source_feasibility.py",
    "src/kdtb/data/official_source_feasibility.py",
    "scripts/audit_historical_data_readiness.py",
)


def _contract_public(contract: Any) -> dict[str, Any]:
    value = {
        "service_id": contract.service_id,
        "provider": contract.provider,
        "endpoint_family": contract.endpoint_family,
        "endpoint": contract.endpoint,
        "method": contract.method,
        "documentation_url": contract.documentation_url,
        "terms_url": contract.terms_url,
        "advertised_coverage": contract.advertised_coverage,
        "authentication": contract.authentication,
        "request_limit": contract.request_limit,
        "license_class": contract.license_class,
        "commercial_use": contract.commercial_use,
        "observed_at_utc": contract.observed_at_utc,
        "contract_sha256": contract.contract_sha256,
        "documentation_evidence": {
            "status": "pending_local_byte_capture",
            "authenticated_provider_value": False,
        },
    }
    return value


def build_artifact(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = {} if environment is None else environment
    canary = verify_canary_definition(CANARY, ROOT)
    core_access = [service_access(contract, values) for contract in CORE_KRX_CONTRACTS]
    optional_access = [
        service_access(contract, values) for contract in OPTIONAL_CONTRACTS
    ]
    auth_blockers = [
        f"AUTH_REQUIRED:{item['service_id']}"
        for item in core_access
        if item["status"] != "APPROVED"
    ]
    # This implementation artifact records no authenticated observation. Even
    # if ambient credentials exist, a separate explicitly authorized probe must
    # capture/replay all six services before empirical conclusions are possible.
    if not auth_blockers:
        auth_blockers = ["NO_GO_REPRODUCIBILITY:authenticated_canary_not_captured"]
    research = decide_research(auth_blockers)
    milestone_status = "BLOCKED"
    generation_command = [
        ".venv/bin/python",
        "-m",
        "scripts.assess_official_source_feasibility",
        "--output",
        "/tmp/m0_8_buyback_source_feasibility_v1.json",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "buyback official-source and production-license feasibility",
        "generation": {
            "command": generation_command,
            "network_used": False,
            "database_used": False,
            "authenticated_calls": 0,
            "restricted_provider_values_in_artifact": 0,
            "python_executable": generation_command[0],
        },
        "generator_sources": [
            {
                "path": path,
                "bytes": (ROOT / path).stat().st_size,
                "sha256": file_sha256(ROOT / path),
            }
            for path in GENERATOR_SOURCES
        ],
        "canary": {
            "path": str(CANARY.relative_to(ROOT)),
            "file_sha256": file_sha256(CANARY),
            "selection_sha256": canary["selection_sha256"],
            "definition_sha256": canary["definition_sha256"],
            "census": canary["counts"],
            "source_pins": canary["source_pins"],
            "request_budget": canary_request_budget(canary["counts"]),
            "authenticated_observations": 0,
            "unprobed_receipts": 141,
        },
        "full_cohort_projection": {
            "rows": 4_846,
            "stock_codes": 1_020,
            "event_dates": 1_163,
            "window_dates": 1_222,
            "exact_market_date_pairs": 2_421,
            "exact_three_family_requests": 7_263,
            "conservative_market_date_pairs": 2_444,
            "conservative_three_family_requests": 7_332,
            "classification": "projection_not_observation",
        },
        "source_contracts": [
            _contract_public(contract)
            for contract in (*CORE_KRX_CONTRACTS, *OPTIONAL_CONTRACTS)
        ],
        "access": {
            "core_krx": core_access,
            "optional": optional_access,
            "all_six_core_approved": all(
                item["status"] == "APPROVED" for item in core_access
            ),
        },
        "evidence": {
            "local_response_envelopes": 0,
            "local_raw_blobs": 0,
            "offline_replays": 0,
            "categorical_mismatch_counts": {},
            "identity_bridge": "NOT_OBSERVED",
            "raw_outcome_schema": "NOT_OBSERVED",
            "revision_comparison": "NOT_OBSERVED",
            "documentation_bytes": "PENDING_LOCAL_CAPTURE",
        },
        "decisions": {
            "research": research,
            "production": {
                "summary": decide_production(
                    public_terms_prohibit=True, superseding_agreement=False
                ),
                "blockers": [
                    "public_noncommercial_terms",
                    "no_retained_superseding_production_agreement",
                ],
            },
            "overall_ml_gate": {
                "summary": "NO_GO",
                "remaining_m0_7_blockers_outside_scope": [
                    "missing_point_in_time_disclosure_evidence",
                    "cross_category_copy_conflict",
                ],
            },
        },
        "milestone": {
            "status": milestone_status,
            "reason": (
                "all six KRX service approvals and an authenticated "
                "offline-replayable canary are required"
            ),
            "verified": False,
            "next_bounded_action": (
                "obtain all six KRX service approvals, then run the frozen "
                "canary under the 1800-call cap"
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON path outside data, sources and artifacts",
    )
    args = parser.parse_args()
    try:
        payload = build_artifact(environment=os.environ)
        digest = write_new_json(args.output, payload)
    except Exception:
        # The CLI never prints provider content, credentials or nested exception
        # text. Only an implementation-controlled category reaches stderr.
        print("M0_8_GENERATION_FAILED", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "artifact_sha256": digest,
                "milestone_status": payload["milestone"]["status"],
                "production_decision": payload["decisions"]["production"]["summary"],
                "research_decision": payload["decisions"]["research"]["summary"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
