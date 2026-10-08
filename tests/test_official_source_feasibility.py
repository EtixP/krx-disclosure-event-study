from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import traceback

import pytest

from kdtb.data import official_source_feasibility as source
from scripts import assess_official_source_feasibility as assess
from scripts.audit_historical_data_readiness import write_new_json


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts/m0_8/buyback_source_feasibility_v1.json"


def _contract() -> source.ServiceContract:
    return source.CORE_KRX_CONTRACTS[0]


def _identity(secret: str = "secret-value") -> dict:
    return source.sanitize_request_identity(
        endpoint="https://provider.example/api?AUTH_KEY=ignored",
        method="get",
        parameters={"basDd": "20240102", "AUTH_KEY": secret},
        known_credentials=(secret,),
    )


def _approved_environment(secret: str) -> dict[str, str]:
    environment = {"KRX_OPEN_API_AUTH_KEY": secret}
    environment.update(
        {item.approval_environment: "true" for item in source.CORE_KRX_CONTRACTS}
    )
    return environment


def _reseal(raw: dict) -> dict:
    candidate = source.ResponseEnvelope(**{**raw, "envelope_integrity_sha256": ""})
    raw["envelope_integrity_sha256"] = candidate.expected_integrity_sha256()
    return raw


def _replace_with_resealed(path: Path, raw: dict) -> Path:
    resealed = _reseal(raw)
    replacement = path.with_name(
        f"{resealed['envelope_integrity_sha256']}.envelope.json"
    )
    path.unlink()
    replacement.write_text(json.dumps(resealed))
    return replacement


def _capture(tmp_path: Path, body: bytes = b'{"OutBlock_1":[]}'):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    captured = store.capture(
        contract=_contract(),
        request_identity=_identity(),
        intended_market_date="2024-01-02",
        status_code=200,
        headers={"Content-Type": "application/json", "X-Secret": "not-retained"},
        body=body,
        known_credentials=("secret-value",),
        retrieved_at_utc="2026-10-03T00:00:00+00:00",
    )
    path = next((tmp_path / "evidence").glob("*.envelope.json"))
    return store, captured, path


def _trusted_for(path: Path) -> source.TrustedEnvelopeIdentity:
    return source.TrustedEnvelopeIdentity(
        envelope_name=path.name,
        envelope_file_sha256=source.file_sha256(path),
    )


def _stock_row(**updates):
    row = {
        "market_date": "2024-01-02",
        "market": "KOSPI",
        "short_code": "005930",
        "isin": "KR7005930003",
        "security_group": "ordinary_share",
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "1000",
        "value": "100000",
        "listed_shares": "1000000",
    }
    row.update(updates)
    return row


def test_frozen_canary_hashes_census_source_pins_and_budget_recompute():
    definition = source.verify_canary_definition(
        ROOT / "docs/history/M0.8-canary-v1.json", ROOT
    )
    assert (
        source.canonical_sha256(definition["selection"])
        == definition["selection_sha256"]
    )
    canonical = dict(definition)
    claimed = canonical.pop("definition_sha256")
    assert source.canonical_sha256(canonical) == claimed
    assert len({item["receipt_no"] for item in definition["selection"]}) == 141
    assert len(definition["market_date_pairs"]) == 391
    assert source.canary_request_budget(definition["counts"])["hard_cap"] == 1800


def test_registry_has_exactly_six_separately_approved_core_services():
    assert len(source.CORE_KRX_CONTRACTS) == 6
    assert len({item.service_id for item in source.CORE_KRX_CONTRACTS}) == 6
    assert len({item.approval_environment for item in source.CORE_KRX_CONTRACTS}) == 6
    assert {item.credential_environment for item in source.CORE_KRX_CONTRACTS} == {
        "KRX_OPEN_API_AUTH_KEY"
    }
    assert {item.endpoint_family for item in source.CORE_KRX_CONTRACTS} == {
        "stock_daily",
        "security_basic",
        "index_daily",
    }


def test_each_missing_service_is_a_separate_auth_blocker():
    statuses = [source.service_access(item, {}) for item in source.CORE_KRX_CONTRACTS]
    assert [item["blocker_code"] for item in statuses] == [
        f"auth_required:{item.service_id}" for item in source.CORE_KRX_CONTRACTS
    ]
    environment = {"KRX_OPEN_API_AUTH_KEY": "never-display"}
    environment.update(
        {item.approval_environment: "true" for item in source.CORE_KRX_CONTRACTS[:-1]}
    )
    assert not source.all_core_services_approved(environment)


def test_frozen_probe_refuses_all_requests_until_every_service_is_approved():
    definition = source.verify_canary_definition(
        ROOT / "docs/history/M0.8-canary-v1.json", ROOT
    )
    partial = {"KRX_OPEN_API_AUTH_KEY": "secret"}
    partial.update(
        {item.approval_environment: "true" for item in source.CORE_KRX_CONTRACTS[:-1]}
    )
    with pytest.raises(source.EvidenceError, match="^AUTH_REQUIRED$"):
        source.build_frozen_core_probe_requests(definition, partial)
    approved = {"KRX_OPEN_API_AUTH_KEY": "secret"}
    approved.update(
        {item.approval_environment: "true" for item in source.CORE_KRX_CONTRACTS}
    )
    requests = source.build_frozen_core_probe_requests(definition, approved)
    assert len(requests) == 1179
    assert sum(item.version == "second_version" for item in requests) == 6


def test_sanitized_request_identity_omits_secret_name_value_and_query():
    secret = "highly-sensitive-key"
    identity = source.sanitize_request_identity(
        endpoint=f"https://example.test/path?AUTH_KEY={secret}",
        method="get",
        parameters={"AUTH_KEY": secret, "serviceKey": secret, "date": "20240102"},
        known_credentials=(secret,),
    )
    rendered = json.dumps(identity)
    assert secret not in rendered
    assert "AUTH_KEY" not in rendered
    assert identity["parameters"] == {"date": "20240102"}


@pytest.mark.parametrize(
    "location",
    [
        "body",
        "encoded_body",
        "lowercase_encoded_body",
        "mixed_encoded_body",
        "mixed_encoded_header",
        "header",
    ],
)
def test_credential_echo_leaves_zero_blob_envelope_or_conclusion_input(
    tmp_path, location
):
    secret = "echo / credential+"
    body = b"normal"
    headers = {"Content-Type": "application/json"}
    if location == "body":
        body = f"provider echoed {secret}".encode()
    elif location == "encoded_body":
        body = b"provider echoed echo+%2F+credential%2B"
    elif location == "lowercase_encoded_body":
        body = b"provider echoed echo+%2f+credential%2b"
    elif location == "mixed_encoded_body":
        body = b"provider echoed echo+%2f+credential%2B"
    elif location == "mixed_encoded_header":
        headers["X-Request-Id"] = "echo+%2f+credential%2B"
    else:
        headers["X-Request"] = secret
    store = source.LocalEvidenceStore(tmp_path / "never-created")
    with pytest.raises(source.EvidenceError, match="^CREDENTIAL_ECHO_REJECTED$"):
        store.capture(
            contract=_contract(),
            request_identity=_identity(secret),
            intended_market_date="2024-01-02",
            status_code=200,
            headers=headers,
            body=body,
            known_credentials=(secret,),
        )
    assert not store.root.exists()


@pytest.mark.parametrize(
    "parameter_name,parameter_value",
    [
        ("AuTh_KeY", "identity / credential+"),
        ("safe", "identity+%2F+credential%2B"),
        ("safe", "identity+%2f+credential%2b"),
        ("safe", "identity+%2f+credential%2B"),
    ],
)
def test_capture_revalidates_identity_and_persists_nothing_on_secret(
    tmp_path, parameter_name, parameter_value
):
    secret = "identity / credential+"
    identity = _identity(secret)
    identity["parameters"][parameter_name] = parameter_value
    store = source.LocalEvidenceStore(tmp_path / "never-created")
    with pytest.raises(source.EvidenceError, match="^CREDENTIAL_IN_REQUEST_IDENTITY$"):
        store.capture(
            contract=_contract(),
            request_identity=identity,
            intended_market_date="2024-01-02",
            status_code=200,
            headers={},
            body=b"safe",
            known_credentials=(secret,),
        )
    assert not store.root.exists()


@pytest.mark.parametrize("location", ["body", "allowed_header", "request_identity"])
def test_mixed_hex_percent_encoding_of_slash_plus_is_rejected(tmp_path, location):
    secret = "/+"
    body = b"safe"
    headers = {"Content-Type": "application/json"}
    identity = _identity(secret)
    if location == "body":
        body = b"%2f%2B"
    elif location == "allowed_header":
        headers["X-Request-Id"] = "%2f%2B"
    else:
        identity["parameters"]["safe"] = "%2f%2B"
    store = source.LocalEvidenceStore(tmp_path / "never-created")
    expected = (
        "CREDENTIAL_IN_REQUEST_IDENTITY"
        if location == "request_identity"
        else "CREDENTIAL_ECHO_REJECTED"
    )
    with pytest.raises(source.EvidenceError, match=f"^{expected}$"):
        store.capture(
            contract=_contract(),
            request_identity=identity,
            intended_market_date="2024-01-02",
            status_code=200,
            headers=headers,
            body=body,
            known_credentials=(secret,),
        )
    assert not store.root.exists()


def test_store_is_user_only_create_only_and_retains_multiple_versions(
    tmp_path, monkeypatch
):
    nonces = iter(["01" * 16, "02" * 16, "01" * 16])
    monkeypatch.setattr(source.secrets, "token_hex", lambda _count: next(nonces))
    store, first, _ = _capture(tmp_path, b"version-one")
    second = store.capture(
        contract=_contract(),
        request_identity=_identity(),
        intended_market_date="2024-01-02",
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=b"version-two",
        known_credentials=("secret-value",),
        retrieved_at_utc="2026-10-04T00:00:00+00:00",
    )
    assert first.envelope.raw_sha256 != second.envelope.raw_sha256
    assert len(list(store.root.glob("*.blob"))) == 2
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in store.root.iterdir())
    with pytest.raises(source.EvidenceError, match="^CREATE_ONLY_COLLISION$"):
        store.capture(
            contract=_contract(),
            request_identity=_identity(),
            intended_market_date="2024-01-02",
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=b"version-one",
            known_credentials=("secret-value",),
        )
    assert len(list(store.root.glob("*.blob"))) == 2


def test_offline_replay_checks_bytes_contract_parser_and_expiry(tmp_path, monkeypatch):
    store, captured, path = _capture(tmp_path)
    envelope = captured.envelope
    contracts = {_contract().service_id: _contract()}
    replayed, body = store.replay(
        path,
        expected_identity=captured.trusted_identity,
        contracts=contracts,
    )
    assert replayed.raw_sha256 == envelope.raw_sha256
    assert body == b'{"OutBlock_1":[]}'

    blob = store.root / envelope.blob_reference
    blob.write_bytes(b"tampered")
    with pytest.raises(source.EvidenceError, match="^RAW_BYTE_COUNT_MISMATCH$"):
        store.replay(
            path,
            expected_identity=captured.trusted_identity,
            contracts=contracts,
        )


def test_contract_and_parser_tampering_fail_replay(tmp_path):
    store, _, path = _capture(tmp_path)
    original = json.loads(path.read_text())
    raw = dict(original)
    raw["contract_sha256"] = "0" * 64
    path = _replace_with_resealed(path, raw)
    with pytest.raises(source.EvidenceError, match="^CONTRACT_METADATA_TAMPERED$"):
        store.replay(
            path,
            expected_identity=_trusted_for(path),
            contracts={_contract().service_id: _contract()},
        )
    raw = dict(original)
    raw["parser_source_sha256"] = "1" * 64
    path = _replace_with_resealed(path, raw)
    with pytest.raises(source.EvidenceError, match="^PARSER_SOURCE_TAMPERED$"):
        store.replay(
            path,
            expected_identity=_trusted_for(path),
            contracts={_contract().service_id: _contract()},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("retention_deadline_utc", None),
        ("license_class", "commercial-anything-goes"),
        ("redistribution", "public"),
        ("blob_reference", "../outside.blob"),
    ],
)
def test_replay_critical_envelope_fields_are_bound_to_original_file(
    tmp_path, field, value
):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    captured = store.capture(
        contract=_contract(),
        request_identity=_identity(),
        intended_market_date="2024-01-02",
        status_code=200,
        headers={},
        body=b"evidence",
        known_credentials=("secret-value",),
        retention_deadline_utc="2027-10-04T00:00:00+00:00",
    )
    path = next(store.root.glob("*.envelope.json"))
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(source.EvidenceError, match="^TRUSTED_ENVELOPE_MISMATCH$"):
        store.replay(
            path,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
        )


def test_resealed_parent_blob_reference_and_external_hash_cannot_escape(tmp_path):
    store, captured, path = _capture(tmp_path)
    outside = tmp_path / "outside.blob"
    outside.write_bytes(b"external provider bytes")
    raw = json.loads(path.read_text())
    raw["blob_reference"] = "../outside.blob"
    raw["raw_byte_count"] = outside.stat().st_size
    raw["raw_sha256"] = source.file_sha256(outside)
    path = _replace_with_resealed(path, raw)
    with pytest.raises(source.EvidenceError, match="^TRUSTED_ENVELOPE_MISMATCH$"):
        store.replay(
            path,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
        )


def test_resealed_renamed_retention_removal_fails_trusted_identity(tmp_path):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    captured = store.capture(
        contract=_contract(),
        request_identity=_identity(),
        intended_market_date="2024-01-02",
        status_code=200,
        headers={},
        body=b"retained evidence",
        known_credentials=("secret-value",),
        retention_deadline_utc="2026-10-03T00:00:00+00:00",
    )
    path = next(store.root.glob("*.envelope.json"))
    raw = json.loads(path.read_text())
    raw["retention_deadline_utc"] = None
    path = _replace_with_resealed(path, raw)
    with pytest.raises(source.EvidenceError, match="^TRUSTED_ENVELOPE_MISMATCH$"):
        store.replay(
            path,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
            now_utc=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )


def test_blob_and_envelope_symlinks_and_root_replacement_cannot_escape(tmp_path):
    store, captured, envelope_path = _capture(tmp_path)
    envelope = captured.envelope
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / envelope.blob_reference).write_bytes(b'{"OutBlock_1":[]}')
    (store.root / envelope.blob_reference).unlink()
    (store.root / envelope.blob_reference).symlink_to(outside / envelope.blob_reference)
    with pytest.raises(source.EvidenceError, match="^RAW_BLOB_UNAVAILABLE$"):
        store.replay(
            envelope_path,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
        )

    displaced = tmp_path / "displaced"
    store.root.rename(displaced)
    store.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(source.EvidenceError, match="^UNSAFE_EVIDENCE_ROOT$"):
        store.replay(
            store.root / envelope_path.name,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
        )


def test_symlink_evidence_root_is_rejected_without_writing_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "evidence"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(source.EvidenceError, match="^UNSAFE_EVIDENCE_ROOT$"):
        source.LocalEvidenceStore(link)
    assert not list(target.iterdir())


def test_root_replacement_during_capture_rolls_back_without_escape(
    tmp_path, monkeypatch
):
    root = tmp_path / "evidence"
    displaced = tmp_path / "displaced"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = source.LocalEvidenceStore(root)
    real_open = source.os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and str(path).endswith(".blob") and kwargs.get("dir_fd"):
            root.rename(displaced)
            root.symlink_to(outside, target_is_directory=True)
            replaced = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(source.os, "open", replacing_open)
    with pytest.raises(source.EvidenceError, match="^EVIDENCE_ROOT_CHANGED$"):
        store.capture(
            contract=_contract(),
            request_identity=_identity(),
            intended_market_date="2024-01-02",
            status_code=200,
            headers={},
            body=b"never escape",
            known_credentials=("secret-value",),
        )
    assert replaced
    assert not list(outside.iterdir())
    assert not list(displaced.iterdir())


def test_expired_evidence_is_disabled(tmp_path):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    captured = store.capture(
        contract=_contract(),
        request_identity=_identity(),
        intended_market_date="2024-01-02",
        status_code=200,
        headers={},
        body=b"{}",
        known_credentials=("secret-value",),
        retention_deadline_utc="2026-10-04T00:00:00+00:00",
    )
    path = next(store.root.glob("*.envelope.json"))
    with pytest.raises(source.EvidenceError, match="^EVIDENCE_RETENTION_EXPIRED$"):
        store.replay(
            path,
            expected_identity=captured.trusted_identity,
            contracts={_contract().service_id: _contract()},
            now_utc=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )


def test_transport_uses_secret_only_on_wire_and_never_in_errors_or_output(
    tmp_path, capsys, caplog
):
    secret = "wire-only-secret"
    observed = {}

    def transport(method, endpoint, parameters, headers):
        observed.update(headers)
        return (
            503,
            {"Content-Type": "application/json"},
            b'{"message":"private provider value"}',
        )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(
            source.EvidenceError, match="^PROVIDER_HTTP_ERROR_CAPTURED$"
        ) as raised,
    ):
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={"basDd": "20240102"},
            credential=secret,
            intended_market_date="2024-01-02",
            store=source.LocalEvidenceStore(tmp_path / "evidence"),
            environment=_approved_environment(secret),
            transport=transport,
        )
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text + str(raised.value)
    assert observed["AUTH_KEY"] == secret
    assert secret not in output
    assert "private provider value" not in output


def test_transport_exception_never_exposes_nested_provider_value(tmp_path):
    def transport(*_args):
        raise RuntimeError("provider-value-that-must-not-surface")

    with pytest.raises(source.EvidenceError) as raised:
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=source.LocalEvidenceStore(tmp_path / "evidence"),
            environment=_approved_environment("secret"),
            transport=transport,
        )
    assert str(raised.value) == "PROVIDER_TRANSPORT_ERROR"
    assert raised.value.__cause__ is None


def test_send_boundary_requires_all_approvals_and_consumes_budget(tmp_path):
    calls = []

    def transport(*args):
        calls.append(args)
        return 200, {}, b"{}"

    partial = _approved_environment("secret")
    partial.pop(source.CORE_KRX_CONTRACTS[-1].approval_environment)
    with pytest.raises(source.EvidenceError, match="^AUTH_REQUIRED$"):
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=source.LocalEvidenceStore(tmp_path / "evidence"),
            environment=partial,
            transport=transport,
        )
    assert calls == []
    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("fresh_store_each_call", [False, True])
def test_probe_run_core_budget_cannot_be_reset_by_callers(
    tmp_path, fresh_store_each_call
):
    root = tmp_path / "evidence"
    store = source.LocalEvidenceStore(root)
    calls = 0

    def failing_transport(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("counted provider attempt")

    final_error = None
    for _ in range(1801):
        try:
            source.execute_authenticated_request(
                contract=_contract(),
                parameters={},
                credential="secret",
                intended_market_date="2024-01-02",
                store=(
                    source.LocalEvidenceStore(root) if fresh_store_each_call else store
                ),
                environment=_approved_environment("secret"),
                transport=failing_transport,
            )
        except source.EvidenceError as error:
            final_error = error
    assert calls == 1800
    assert final_error is not None
    assert final_error.code == "REQUEST_BUDGET_EXHAUSTED"
    ledger_path = next(root.glob("*.budget.json"))
    ledger = json.loads(ledger_path.read_text())
    assert ledger["canary_definition_sha256"] == source.CANARY_DEFINITION_SHA256
    assert ledger["core_cap"] == 1800
    assert ledger["core_used"] == 1800


def test_resealed_budget_reset_fails_closed_with_fresh_store(tmp_path):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    store.consume_transport_budget(_contract())
    ledger_path = next(store.root.glob("*.budget.json"))
    ledger = json.loads(ledger_path.read_text())
    ledger["core_used"] = 0
    ledger = store._budget_record(ledger["core_used"], ledger["optional_used"])
    ledger_path.write_text(json.dumps(ledger))
    calls = []
    with pytest.raises(source.EvidenceError, match="^BUDGET_LEDGER_INVALID$"):
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=source.LocalEvidenceStore(store.root),
            environment=_approved_environment("secret"),
            transport=lambda *_args: calls.append(True),
        )
    assert calls == []


@pytest.mark.parametrize("fresh_store", [False, True])
def test_deleted_budget_ledger_fails_before_transport(tmp_path, fresh_store):
    store = source.LocalEvidenceStore(tmp_path / "evidence")
    store.consume_transport_budget(_contract())
    next(store.root.glob("*.budget.json")).unlink()
    calls = []
    with pytest.raises(source.EvidenceError, match="^BUDGET_LEDGER_UNAVAILABLE$"):
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=(source.LocalEvidenceStore(store.root) if fresh_store else store),
            environment=_approved_environment("secret"),
            transport=lambda *_args: calls.append(True),
        )
    assert calls == []


@pytest.mark.parametrize("fresh_store", [False, True])
def test_replaced_evidence_root_cannot_reset_probe_authority(tmp_path, fresh_store):
    root = tmp_path / "evidence"
    displaced = tmp_path / "displaced"
    store = source.LocalEvidenceStore(root)
    store.consume_transport_budget(_contract())
    root.rename(displaced)
    root.mkdir()
    calls = []
    with pytest.raises(source.EvidenceError, match="^EVIDENCE_ROOT_CHANGED$"):
        source.execute_authenticated_request(
            contract=_contract(),
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=(source.LocalEvidenceStore(root) if fresh_store else store),
            environment=_approved_environment("secret"),
            transport=lambda *_args: calls.append(True),
        )
    assert calls == []


def test_fresh_stores_share_the_optional_probe_counter(tmp_path):
    root = tmp_path / "evidence"
    contract = source.OPTIONAL_CONTRACTS[0]
    secret = "optional-secret"
    environment = {
        contract.credential_environment: secret,
        contract.approval_environment: "true",
    }
    calls = 0

    def failing_transport(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("counted optional provider attempt")

    final_error = None
    for _ in range(601):
        try:
            source.execute_authenticated_request(
                contract=contract,
                parameters={},
                credential=secret,
                intended_market_date="2024-01-02",
                store=source.LocalEvidenceStore(root),
                environment=environment,
                transport=failing_transport,
            )
        except source.EvidenceError as error:
            final_error = error
    assert calls == 600
    assert final_error is not None
    assert final_error.code == "REQUEST_BUDGET_EXHAUSTED"
    ledger = json.loads(next(root.glob("*.budget.json")).read_text())
    assert ledger["core_used"] == 0
    assert ledger["optional_used"] == 600


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_fork_lineage_cannot_reset_exhausted_authority_after_parent_exits(tmp_path):
    root = tmp_path / "evidence"
    program = r"""
import json
import os
from pathlib import Path
import sys

from kdtb.data import official_source_feasibility as source

root = Path(sys.argv[1])
contract = source.CORE_KRX_CONTRACTS[0]
environment = {"KRX_OPEN_API_AUTH_KEY": "secret"}
environment.update(
    {item.approval_environment: "true" for item in source.CORE_KRX_CONTRACTS}
)
store = source.LocalEvidenceStore(root)
parent_calls = 0

def failing_transport(*_args):
    global parent_calls
    parent_calls += 1
    raise RuntimeError("counted parent attempt")

for _ in range(1800):
    try:
        source.execute_authenticated_request(
            contract=contract,
            parameters={},
            credential="secret",
            intended_market_date="2024-01-02",
            store=store,
            environment=environment,
            transport=failing_transport,
        )
    except source.EvidenceError as error:
        if error.code != "PROVIDER_TRANSPORT_ERROR":
            raise

death_read, death_write = os.pipe()
lineage_child = os.fork()
if lineage_child:
    os.close(death_read)
    os._exit(0)

os.close(death_write)
while os.read(death_read, 1):
    pass
os.close(death_read)
child_calls = 0

def child_transport(*_args):
    global child_calls
    child_calls += 1
    return 200, {}, b"{}"

code = None
try:
    source.execute_authenticated_request(
        contract=contract,
        parameters={},
        credential="secret",
        intended_market_date="2024-01-02",
        store=source.LocalEvidenceStore(root),
        environment=environment,
        transport=child_transport,
    )
except source.EvidenceError as error:
    code = error.code
ledger = json.loads(next(root.glob("*.budget.json")).read_text())
print(
    json.dumps(
        {
            "child_calls": child_calls,
            "code": code,
            "core_used": ledger["core_used"],
            "parent_calls": parent_calls,
        },
        sort_keys=True,
    ),
    flush=True,
)
os._exit(0)
"""
    completed = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-c", program, str(root)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "child_calls": 0,
        "code": "PROBE_AUTHORITY_FORKED",
        "core_used": 1800,
        "parent_calls": 1800,
    }


def test_numeric_and_parser_failures_do_not_leak_values_in_tracebacks():
    secret_value = "provider-secret-number"
    try:
        source.reconcile_stock_rows(
            [_stock_row(close=secret_value)],
            requested_date="2024-01-02",
            market="KOSPI",
            short_code="005930",
            complete_pagination=True,
        )
    except source.EvidenceError:
        rendered = traceback.format_exc()
    else:
        raise AssertionError("invalid provider number was accepted")
    assert secret_value not in rendered

    provider_json = b'{"value": invalid-provider-json}'
    try:
        source.parse_krx_json(provider_json)
    except source.EvidenceError:
        rendered = traceback.format_exc()
    else:
        raise AssertionError("invalid provider JSON was accepted")
    assert "invalid-provider-json" not in rendered


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not-json", "PROVIDER_JSON_INVALID"),
        (b'{"message":"private"}', "PROVIDER_ERROR_PAYLOAD"),
        (b'{"OutBlock_1":{}}', "PROVIDER_SCHEMA_DRIFT"),
    ],
)
def test_parser_errors_are_categorical_and_do_not_surface_values(body, code):
    with pytest.raises(source.EvidenceError, match=f"^{code}$"):
        source.parse_krx_json(body)


@pytest.mark.parametrize(
    "rows,complete,code",
    [
        ([_stock_row()], False, "PAGINATION_INCOMPLETE"),
        ([], True, "SHORT_CODE_NOT_OBSERVED"),
        ([_stock_row(), _stock_row()], True, "SHORT_CODE_AMBIGUOUS"),
        ([_stock_row(market_date="2024-01-03")], True, "REQUESTED_DATE_MISMATCH"),
        ([_stock_row(market="KOSDAQ")], True, "MARKET_MISMATCH"),
        ([_stock_row(isin="bad")], True, "ISIN_INVALID"),
        ([_stock_row(security_group="etf")], True, "INSTRUMENT_UNSUPPORTED"),
        ([_stock_row(close="nan")], True, "PRICE_INVALID"),
        ([_stock_row(high="99")], True, "OHLC_ORDER_INVALID"),
        ([_stock_row(volume="-1")], True, "VOLUME_INVALID"),
    ],
)
def test_stock_reconciliation_quarantines_invalid_evidence(rows, complete, code):
    with pytest.raises(source.EvidenceError, match=f"^{code}$"):
        source.reconcile_stock_rows(
            rows,
            requested_date="2024-01-02",
            market="KOSPI",
            short_code="005930",
            complete_pagination=complete,
        )


def test_valid_stock_and_stable_code_benchmark_reconcile():
    observation = source.reconcile_stock_rows(
        [_stock_row()],
        requested_date="2024-01-02",
        market="KOSPI",
        short_code="005930",
        complete_pagination=True,
    )
    assert observation.close == 105
    benchmark = source.reconcile_benchmark_rows(
        [{"market_date": "2024-01-02", "index_code": "1001", "close": "2600"}],
        requested_date="2024-01-02",
        stable_index_code="1001",
    )
    assert benchmark["close"] == 2600
    with pytest.raises(source.EvidenceError, match="^BENCHMARK_CODE_REQUIRED$"):
        source.reconcile_benchmark_rows(
            [], requested_date="2024-01-02", stable_index_code=""
        )


def test_current_dart_mapping_is_rejected_as_historical_proof_and_actions_quarantine():
    assert (
        source.classify_identity_bridge(
            historical_effective_evidence=False, current_mapping=True
        )
        == "CURRENT_ONLY_NOT_HISTORICAL_PROOF"
    )
    assert (
        source.classify_corporate_action(
            raw_adjusted_difference=True, action_evidence=False
        )
        == "CORPORATE_ACTION_QUARANTINE"
    )


def test_decision_precedence_sorted_blockers_and_diagnostics_never_promote():
    blockers = [
        "PARTIAL_IDENTITY_BRIDGE:current_only",
        "NO_GO_SOURCE_SCHEMA:missing_field",
        "AUTH_REQUIRED:kospi_stock_daily",
        "NO_GO_REPRODUCIBILITY:hash",
    ]
    for ordering in (blockers, list(reversed(blockers))):
        decision = source.decide_research(ordering)
        assert decision["summary"] == "AUTH_REQUIRED"
        assert decision["blockers"] == sorted(blockers)
    primary = source.apply_diagnostics([], ["NO_GO_SOURCE_SCHEMA:diagnostic"])
    assert primary["summary"] == "NO_GO_SOURCE_SCHEMA"
    assert (
        source.decide_production(
            public_terms_prohibit=True, superseding_agreement=False
        )
        == "NO_GO_LICENSE"
    )
    assert (
        source.decide_production(public_terms_prohibit=True, superseding_agreement=True)
        == "GO_PRODUCTION_LICENSED"
    )


def test_diagnostic_selection_is_deterministic_bounded_and_order_independent():
    mismatches = [
        {"receipt_no": f"{index:014d}", "mismatch_code": "B" if index % 2 else "A"}
        for index in range(40)
    ]
    first = source.choose_diagnostics(mismatches)
    second = source.choose_diagnostics(list(reversed(mismatches)))
    assert first == second
    assert len(first) == 24
    assert all(item["mismatch_code"] == "A" for item in first[:20])

    duplicate = [
        {"receipt_no": "1" * 14, "mismatch_code": "Z"},
        {"receipt_no": "1" * 14, "mismatch_code": "A"},
    ]
    assert source.choose_diagnostics(duplicate) == source.choose_diagnostics(
        list(reversed(duplicate))
    )
    assert source.choose_diagnostics(duplicate)[0]["mismatch_code"] == "A"


def test_diagnostic_plan_enforces_receipt_pair_and_call_caps():
    mismatches = []
    for receipt in range(30):
        pairs = [
            {
                "market": "KOSPI" if pair % 2 else "KOSDAQ",
                "date": f"2024-{1 + pair // 28:02d}-{1 + pair % 28:02d}",
            }
            for pair in range(receipt * 6, receipt * 6 + 6)
        ]
        mismatches.append(
            {
                "receipt_no": f"{receipt:014d}",
                "mismatch_code": "SCHEMA",
                "market_date_pairs": pairs,
            }
        )
    plan = source.plan_diagnostics(mismatches)
    assert len(plan.receipts) <= 24
    assert len(plan.market_date_pairs) <= 120
    assert plan.request_count <= 360
    assert plan.request_count == len(plan.market_date_pairs) * 3
    with pytest.raises(source.EvidenceError, match="^DIAGNOSTIC_PAIR_CAP_INVALID$"):
        source.plan_diagnostics(mismatches, cap_market_date_pairs=121)
    with pytest.raises(source.EvidenceError, match="^DIAGNOSTIC_CALL_CAP_INVALID$"):
        source.plan_diagnostics(mismatches, cap_calls=361)


def test_request_budget_never_sends_past_cap():
    budget = source.RequestBudget(2)
    budget.consume()
    budget.consume()
    with pytest.raises(source.EvidenceError, match="^REQUEST_BUDGET_EXHAUSTED$"):
        budget.consume()
    assert budget.used == 2


def test_blocked_artifact_is_byte_reproducible_and_has_no_provider_values(tmp_path):
    payload = assess.build_artifact(environment={})
    assert payload["milestone"]["status"] == "BLOCKED"
    assert payload["decisions"]["research"]["summary"] == "AUTH_REQUIRED"
    assert len(payload["decisions"]["research"]["blockers"]) == 6
    assert payload["decisions"]["production"]["summary"] == "NO_GO_LICENSE"
    assert payload["decisions"]["overall_ml_gate"]["summary"] == "NO_GO"
    assert payload["evidence"]["local_raw_blobs"] == 0
    assert b"highly-sensitive-key" not in ARTIFACT.read_bytes()
    assert json.dumps(payload, sort_keys=True).encode() != b""


def test_blocked_integration_recomputes_census_access_and_both_decisions_offline(
    monkeypatch,
):
    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("offline decision attempted network access")

    monkeypatch.setattr("socket.socket", network_forbidden)
    canary = source.verify_canary_definition(
        ROOT / "docs/history/M0.8-canary-v1.json", ROOT
    )
    payload = assess.build_artifact(environment={})
    assert canary["counts"] == payload["canary"]["census"]
    assert payload["canary"]["unprobed_receipts"] == 141
    assert payload["canary"]["authenticated_observations"] == 0
    assert payload["decisions"]["research"] == source.decide_research(
        [f"AUTH_REQUIRED:{item.service_id}" for item in source.CORE_KRX_CONTRACTS]
    )
    assert payload["decisions"]["production"]["summary"] == "NO_GO_LICENSE"
    assert payload["milestone"]["verified"] is False


def test_m0_8_uses_verified_create_only_publication_guard(tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("winner")
    with pytest.raises(ValueError, match="choose a new output"):
        write_new_json(existing, {"never": "overwrite"})
    with pytest.raises(ValueError, match="choose a new output"):
        write_new_json(
            ROOT / "artifacts/m0_8/never-write-here.json", {"never": "publish"}
        )
    assert existing.read_text() == "winner"


def test_recorded_exact_command_reproduces_artifact(tmp_path):
    recorded = json.loads(ARTIFACT.read_text())
    command = list(recorded["generation"]["command"])
    output = Path(command[-1])
    output.unlink(missing_ok=True)
    environment = {"PATH": os.environ["PATH"]}
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        assert output.read_bytes() == ARTIFACT.read_bytes()
        visible = completed.stdout + completed.stderr
        assert "BLOCKED" in visible
        assert "NO_GO_LICENSE" in visible
    finally:
        output.unlink(missing_ok=True)


def test_cli_output_failure_is_categorical(monkeypatch, capsys, tmp_path):
    def fail(_environment):
        raise RuntimeError("secret provider value")

    monkeypatch.setattr(assess, "build_artifact", fail)
    monkeypatch.setattr(
        "sys.argv", ["assess", "--output", str(tmp_path / "output.json")]
    )
    assert assess.main() == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "M0_8_GENERATION_FAILED"
    assert "secret provider value" not in captured.err
