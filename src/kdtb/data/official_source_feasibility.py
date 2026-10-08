"""Fail-closed official-source evidence machinery for the M0.8 canary.

The module deliberately separates authenticated transport from persistence and
presentation.  Provider payload values are usable only in memory or from the
restricted evidence store; public results contain hashes, counts and categorical
codes.  No caller needs a provider credential to replay already captured bytes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote_to_bytes


SCHEMA_VERSION = "m0.8_official_source_evidence_v1"
PARSER_VERSION = "m0.8_krx_json_v1"
SECRET_PARAMETER_NAMES = frozenset(
    {"auth_key", "servicekey", "crtfc_key", "authorization", "api_key"}
)
ALLOWED_RESPONSE_HEADERS = frozenset(
    {"content-type", "date", "etag", "last-modified", "x-request-id"}
)
SUPPORTED_MARKETS = frozenset({"KOSPI", "KOSDAQ"})
SUPPORTED_INSTRUMENTS = frozenset({"ordinary_share", "common_stock"})
CANARY_DEFINITION_SHA256 = (
    "ac94f3b62045823e759389bac641199571c4c034e9a823a5fce2dfb45df2f3f6"
)
CORE_REQUEST_CAP = 1800
OPTIONAL_REQUEST_CAP = 600


class EvidenceError(RuntimeError):
    """A categorical error whose text never contains authenticated values."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code):
            raise ValueError("evidence errors require a categorical code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ServiceContract:
    service_id: str
    provider: str
    endpoint_family: str
    endpoint: str
    method: str
    documentation_url: str
    terms_url: str
    advertised_coverage: str
    authentication: str
    request_limit: str
    license_class: str
    commercial_use: str
    observed_at_utc: str
    approval_environment: str
    credential_environment: str
    core: bool

    @property
    def contract_sha256(self) -> str:
        return canonical_sha256(asdict(self))


KRX_DOCUMENTATION = "https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd"
KRX_TERMS = "https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp"
DATA_GO_STOCK_DOC = "https://www.data.go.kr/data/15094808/openapi.do"
DATA_GO_SECURITY_DOC = "https://www.data.go.kr/data/15094775/openapi.do"
DART_CORP_DOC = (
    "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DE001&apiId=AE00004"
)


def _krx_contract(
    service_id: str, endpoint_family: str, endpoint: str
) -> ServiceContract:
    return ServiceContract(
        service_id=service_id,
        provider="KRX_OPEN_API",
        endpoint_family=endpoint_family,
        endpoint=f"https://data-dbg.krx.co.kr/svc/apis/{endpoint}",
        method="GET",
        documentation_url=KRX_DOCUMENTATION,
        terms_url=KRX_TERMS,
        advertised_coverage="2010 onward",
        authentication="AUTH_KEY header; separate service approval required",
        request_limit="10000 requests per key per day",
        license_class="public noncommercial API terms",
        commercial_use="prohibited absent retained superseding agreement",
        observed_at_utc="2026-10-03T00:00:00Z",
        approval_environment=f"KRX_APPROVED_{service_id.upper()}",
        credential_environment="KRX_OPEN_API_AUTH_KEY",
        core=True,
    )


CORE_KRX_CONTRACTS: tuple[ServiceContract, ...] = (
    _krx_contract("kospi_stock_daily", "stock_daily", "sto/stk_bydd_trd"),
    _krx_contract("kosdaq_stock_daily", "stock_daily", "sto/ksq_bydd_trd"),
    _krx_contract("kospi_security_basic", "security_basic", "sto/stk_isu_base_info"),
    _krx_contract("kosdaq_security_basic", "security_basic", "sto/ksq_isu_base_info"),
    _krx_contract("kospi_index_daily", "index_daily", "idx/kospi_dd_trd"),
    _krx_contract("kosdaq_index_daily", "index_daily", "idx/kosdaq_dd_trd"),
)

OPTIONAL_CONTRACTS: tuple[ServiceContract, ...] = (
    ServiceContract(
        service_id="fsc_stock_price_crosscheck",
        provider="FSC_DATA_GO_KR",
        endpoint_family="stock_daily_crosscheck",
        endpoint=(
            "https://apis.data.go.kr/1160100/service/" "GetStockSecuritiesInfoService"
        ),
        method="GET",
        documentation_url=DATA_GO_STOCK_DOC,
        terms_url=DATA_GO_STOCK_DOC,
        advertised_coverage="official contract; empirical history unverified",
        authentication="transient serviceKey query parameter",
        request_limit="unknown",
        license_class="posted noncommercial/no-modification terms",
        commercial_use="prohibited absent retained superseding agreement",
        observed_at_utc="2026-10-03T00:00:00Z",
        approval_environment="DATA_GO_KR_APPROVED_STOCK_PRICE",
        credential_environment="DATA_GO_KR_SERVICE_KEY",
        core=False,
    ),
    ServiceContract(
        service_id="fsc_listed_security_crosscheck",
        provider="FSC_DATA_GO_KR",
        endpoint_family="listed_security_crosscheck",
        endpoint="https://apis.data.go.kr/1160100/service/GetKrxListedInfoService",
        method="GET",
        documentation_url=DATA_GO_SECURITY_DOC,
        terms_url=DATA_GO_SECURITY_DOC,
        advertised_coverage="official contract; empirical history unverified",
        authentication="transient serviceKey query parameter",
        request_limit="unknown",
        license_class="posted noncommercial/no-modification terms",
        commercial_use="prohibited absent retained superseding agreement",
        observed_at_utc="2026-10-03T00:00:00Z",
        approval_environment="DATA_GO_KR_APPROVED_LISTED_SECURITY",
        credential_environment="DATA_GO_KR_SERVICE_KEY",
        core=False,
    ),
    ServiceContract(
        service_id="open_dart_corporation_bridge",
        provider="OPEN_DART",
        endpoint_family="current_corporation_code",
        endpoint="https://opendart.fss.or.kr/api/corpCode.xml",
        method="GET",
        documentation_url=DART_CORP_DOC,
        terms_url=DART_CORP_DOC,
        advertised_coverage="current mapping with modification date",
        authentication="transient crtfc_key query parameter",
        request_limit="unknown",
        license_class="official API contract; production rights unproved",
        commercial_use="unknown",
        observed_at_utc="2026-10-03T00:00:00Z",
        approval_environment="OPEN_DART_APPROVED_CORP_CODE",
        credential_environment="DART_API_KEY",
        core=False,
    ),
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def service_access(
    contract: ServiceContract, environment: Mapping[str, str] | None = None
) -> dict[str, Any]:
    values = os.environ if environment is None else environment
    credential_present = bool(values.get(contract.credential_environment))
    approved = values.get(contract.approval_environment, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "approved",
    }
    status = "APPROVED" if credential_present and approved else "AUTH_REQUIRED"
    return {
        "service_id": contract.service_id,
        "status": status,
        "credential_present": credential_present,
        "service_approved": approved,
        "blocker_code": (
            None if status == "APPROVED" else f"auth_required:{contract.service_id}"
        ),
    }


def all_core_services_approved(
    environment: Mapping[str, str] | None = None,
) -> bool:
    return all(
        service_access(contract, environment)["status"] == "APPROVED"
        for contract in CORE_KRX_CONTRACTS
    )


def sanitize_request_identity(
    *,
    endpoint: str,
    method: str,
    parameters: Mapping[str, Any],
    known_credentials: Iterable[str] = (),
) -> dict[str, Any]:
    credentials = {value for value in known_credentials if value}
    clean: dict[str, Any] = {}
    for key, value in sorted(parameters.items()):
        if key.casefold() in SECRET_PARAMETER_NAMES:
            continue
        rendered = str(value)
        if any(secret in rendered for secret in credentials):
            continue
        clean[key] = value
    identity = {
        "endpoint": endpoint.split("?", 1)[0],
        "method": method.upper(),
        "parameters": clean,
    }
    if any(
        secret in canonical_bytes(identity).decode("utf-8") for secret in credentials
    ):
        raise EvidenceError("CREDENTIAL_IN_REQUEST_IDENTITY")
    return identity


def _percent_decode(value: bytes) -> bytes:
    try:
        return unquote_to_bytes(value)
    except Exception:
        return value


def _form_decode(value: bytes) -> bytes:
    return _percent_decode(value.replace(b"+", b" "))


def _credential_variants(credentials: Iterable[str]) -> tuple[bytes, ...]:
    variants: set[bytes] = set()
    for item in credentials:
        if not item:
            continue
        raw = item.encode("utf-8")
        variants.add(raw)
        variants.add(_percent_decode(raw))
        variants.add(_form_decode(raw))
    return tuple(sorted(variants))


def _contains_credential(
    body: bytes, headers: Mapping[str, str], credentials: Iterable[str]
) -> bool:
    candidates = _credential_variants(credentials)
    header_bytes = canonical_bytes({str(k): str(v) for k, v in headers.items()})
    decoded_body = _percent_decode(body)
    decoded_headers = _percent_decode(header_bytes)
    form_decoded_body = _form_decode(body)
    form_decoded_headers = _form_decode(header_bytes)
    return any(
        secret in body
        or secret in decoded_body
        or secret in form_decoded_body
        or secret in header_bytes
        or secret in decoded_headers
        or secret in form_decoded_headers
        for secret in candidates
    )


def _validated_request_identity(
    request_identity: Mapping[str, Any], credentials: Iterable[str]
) -> dict[str, Any]:
    if set(request_identity) != {"endpoint", "method", "parameters"}:
        raise EvidenceError("REQUEST_IDENTITY_INVALID")
    endpoint = request_identity.get("endpoint")
    method = request_identity.get("method")
    parameters = request_identity.get("parameters")
    if not isinstance(endpoint, str) or not isinstance(method, str):
        raise EvidenceError("REQUEST_IDENTITY_INVALID")
    if not isinstance(parameters, Mapping):
        raise EvidenceError("REQUEST_IDENTITY_INVALID")
    sanitized = sanitize_request_identity(
        endpoint=endpoint,
        method=method,
        parameters=parameters,
        known_credentials=credentials,
    )
    if sanitized != dict(request_identity) or _contains_credential(
        canonical_bytes(request_identity), {}, credentials
    ):
        raise EvidenceError("CREDENTIAL_IN_REQUEST_IDENTITY")
    return sanitized


def parser_source_sha256() -> str:
    return file_sha256(Path(__file__))


@dataclass(frozen=True)
class ResponseEnvelope:
    schema_version: str
    provider: str
    service_id: str
    contract_sha256: str
    request_identity: Mapping[str, Any]
    request_sha256: str
    retrieved_at_utc: str
    intended_market_date: str
    status_code: int
    content_type: str
    selected_headers: Mapping[str, str]
    raw_byte_count: int
    raw_sha256: str
    parser_version: str
    parser_source_sha256: str
    license_class: str
    redistribution: str
    retention_deadline_utc: str | None
    blob_reference: str
    envelope_integrity_sha256: str

    def integrity_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("envelope_integrity_sha256")
        return payload

    def expected_integrity_sha256(self) -> str:
        return canonical_sha256(self.integrity_payload())

    def public_summary(self) -> dict[str, Any]:
        """Return only values safe for committed artifacts or output channels."""
        return {
            "provider": self.provider,
            "service_id": self.service_id,
            "contract_sha256": self.contract_sha256,
            "request_sha256": self.request_sha256,
            "retrieved_at_utc": self.retrieved_at_utc,
            "intended_market_date": self.intended_market_date,
            "status_code_class": f"{self.status_code // 100}xx",
            "raw_byte_count": self.raw_byte_count,
            "raw_sha256": self.raw_sha256,
            "parser_version": self.parser_version,
            "parser_source_sha256": self.parser_source_sha256,
            "license_class": self.license_class,
            "redistribution": self.redistribution,
            "retention_deadline_utc": self.retention_deadline_utc,
            "blob_reference_sha256": hashlib.sha256(
                self.blob_reference.encode("utf-8")
            ).hexdigest(),
            "envelope_integrity_sha256": self.envelope_integrity_sha256,
        }


@dataclass(frozen=True)
class TrustedEnvelopeIdentity:
    """Envelope identity retained by a conclusion manifest, not the envelope."""

    envelope_name: str
    envelope_file_sha256: str


@dataclass(frozen=True)
class CapturedEvidence:
    envelope: ResponseEnvelope
    trusted_identity: TrustedEnvelopeIdentity

    def conclusion_reference(self) -> dict[str, Any]:
        """Return the trusted identity that a conclusion manifest must retain."""
        return {
            "envelope": self.envelope.public_summary(),
            "trusted_identity": asdict(self.trusted_identity),
        }


class LocalEvidenceStore:
    """Create-only restricted store for authenticated response bytes/envelopes."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        if self.root.is_symlink():
            raise EvidenceError("UNSAFE_EVIDENCE_ROOT")

    def _open_root(self, *, create: bool) -> int:
        """Open the evidence root componentwise without following symlinks."""
        if not self.root.is_absolute() or self.root.anchor != "/":
            raise EvidenceError("UNSAFE_EVIDENCE_ROOT")
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open("/", flags)
        try:
            for component in self.root.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise EvidenceError("EVIDENCE_ROOT_UNAVAILABLE") from None
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            root_stat = os.fstat(descriptor)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise EvidenceError("UNSAFE_EVIDENCE_ROOT")
            os.fchmod(descriptor, 0o700)
            return descriptor
        except EvidenceError:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            raise EvidenceError("UNSAFE_EVIDENCE_ROOT") from None

    def _relative_envelope_name(self, envelope_path: Path) -> str:
        candidate = Path(envelope_path)
        if candidate.parent == Path("."):
            name = candidate.name
        else:
            absolute = Path(os.path.abspath(candidate))
            if absolute.parent != self.root:
                raise EvidenceError("EVIDENCE_PATH_ESCAPE")
            name = absolute.name
        if not re.fullmatch(r"[0-9a-f]{64}\.envelope\.json", name):
            raise EvidenceError("INVALID_ENVELOPE_REFERENCE")
        return name

    def _visible_root_matches(self, root_fd: int) -> bool:
        try:
            visible = os.stat(self.root, follow_symlinks=False)
            opened = os.fstat(root_fd)
        except OSError:
            return False
        return stat.S_ISDIR(visible.st_mode) and (
            visible.st_dev,
            visible.st_ino,
        ) == (opened.st_dev, opened.st_ino)

    @staticmethod
    def _read_regular_file(root_fd: int, name: str, unavailable: str) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, dir_fd=root_fd)
        except OSError:
            raise EvidenceError(unavailable) from None
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise EvidenceError(unavailable)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(descriptor)

    @staticmethod
    def _budget_record(core_used: int, optional_used: int) -> dict[str, Any]:
        payload = {
            "schema_version": "m0.8_probe_budget_ledger_v1",
            "canary_definition_sha256": CANARY_DEFINITION_SHA256,
            "core_cap": CORE_REQUEST_CAP,
            "optional_cap": OPTIONAL_REQUEST_CAP,
            "core_used": core_used,
            "optional_used": optional_used,
        }
        return {**payload, "ledger_sha256": canonical_sha256(payload)}

    @classmethod
    def _validate_budget_record(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise EvidenceError("BUDGET_LEDGER_INVALID")
        integrity = raw.get("ledger_sha256")
        payload = {key: value for key, value in raw.items() if key != "ledger_sha256"}
        expected = cls._budget_record(
            payload.get("core_used", -1), payload.get("optional_used", -1)
        )
        if raw != expected or integrity != canonical_sha256(payload):
            raise EvidenceError("BUDGET_LEDGER_INVALID")
        if not all(
            type(payload[key]) is int and 0 <= payload[key] <= payload[cap]
            for key, cap in (
                ("core_used", "core_cap"),
                ("optional_used", "optional_cap"),
            )
        ):
            raise EvidenceError("BUDGET_LEDGER_INVALID")
        return raw

    def consume_transport_budget(self, contract: ServiceContract) -> int:
        """Claim one call from this process's authority for the frozen probe."""
        return _probe_authority_for_store(self).consume(contract)

    def capture(
        self,
        *,
        contract: ServiceContract,
        request_identity: Mapping[str, Any],
        intended_market_date: str,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes,
        known_credentials: Iterable[str],
        retrieved_at_utc: str | None = None,
        retention_deadline_utc: str | None = None,
    ) -> CapturedEvidence:
        credentials = tuple(known_credentials)
        # These checks intentionally precede directory creation: an echo leaves
        # zero blob, zero envelope and zero conclusion input.
        if _contains_credential(body, headers, credentials):
            raise EvidenceError("CREDENTIAL_ECHO_REJECTED")
        validated_identity = _validated_request_identity(request_identity, credentials)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", intended_market_date):
            raise EvidenceError("INVALID_INTENDED_MARKET_DATE")
        try:
            normalized_status = int(status_code)
        except (TypeError, ValueError):
            raise EvidenceError("PROVIDER_STATUS_INVALID") from None
        root_fd = self._open_root(create=True)
        digest = hashlib.sha256(body).hexdigest()
        request_hash = canonical_sha256(validated_identity)
        capture_id = hashlib.sha256(
            canonical_bytes(
                {
                    "request_sha256": request_hash,
                    "raw_sha256": digest,
                    "nonce": secrets.token_hex(16),
                }
            )
        ).hexdigest()
        blob_name = f"{capture_id}.blob"
        safe_headers = {
            key.casefold(): str(value)
            for key, value in headers.items()
            if key.casefold() in ALLOWED_RESPONSE_HEADERS
        }
        timestamp = retrieved_at_utc or datetime.now(timezone.utc).isoformat()
        envelope = ResponseEnvelope(
            schema_version=SCHEMA_VERSION,
            provider=contract.provider,
            service_id=contract.service_id,
            contract_sha256=contract.contract_sha256,
            request_identity=validated_identity,
            request_sha256=request_hash,
            retrieved_at_utc=timestamp,
            intended_market_date=intended_market_date,
            status_code=normalized_status,
            content_type=safe_headers.get("content-type", "unknown"),
            selected_headers=safe_headers,
            raw_byte_count=len(body),
            raw_sha256=digest,
            parser_version=PARSER_VERSION,
            parser_source_sha256=parser_source_sha256(),
            license_class=contract.license_class,
            redistribution="restricted_local_only",
            retention_deadline_utc=retention_deadline_utc,
            blob_reference=blob_name,
            envelope_integrity_sha256="",
        )
        envelope = replace(
            envelope,
            envelope_integrity_sha256=envelope.expected_integrity_sha256(),
        )
        envelope_name = f"{envelope.envelope_integrity_sha256}.envelope.json"
        encoded = canonical_bytes(asdict(envelope)) + b"\n"
        trusted_identity = TrustedEnvelopeIdentity(
            envelope_name=envelope_name,
            envelope_file_sha256=hashlib.sha256(encoded).hexdigest(),
        )
        created_blob = False
        try:
            blob_fd = os.open(
                blob_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=root_fd,
            )
            created_blob = True
            with os.fdopen(blob_fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            envelope_fd = os.open(
                envelope_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=root_fd,
            )
            with os.fdopen(envelope_fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if not self._visible_root_matches(root_fd):
                raise EvidenceError("EVIDENCE_ROOT_CHANGED")
        except FileExistsError as error:
            if created_blob:
                try:
                    os.unlink(blob_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            raise EvidenceError("CREATE_ONLY_COLLISION") from error
        except Exception:
            if created_blob:
                try:
                    os.unlink(blob_name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
            try:
                os.unlink(envelope_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(root_fd)
        return CapturedEvidence(
            envelope=envelope,
            trusted_identity=trusted_identity,
        )

    def replay(
        self,
        envelope_path: Path,
        *,
        expected_identity: TrustedEnvelopeIdentity,
        contracts: Mapping[str, ServiceContract],
        now_utc: datetime | None = None,
    ) -> tuple[ResponseEnvelope, bytes]:
        envelope_name = self._relative_envelope_name(envelope_path)
        if not isinstance(expected_identity, TrustedEnvelopeIdentity):
            raise EvidenceError("TRUSTED_ENVELOPE_IDENTITY_REQUIRED")
        if (
            not re.fullmatch(
                r"[0-9a-f]{64}\.envelope\.json",
                expected_identity.envelope_name,
            )
            or not re.fullmatch(r"[0-9a-f]{64}", expected_identity.envelope_file_sha256)
            or envelope_name != expected_identity.envelope_name
        ):
            raise EvidenceError("TRUSTED_ENVELOPE_MISMATCH")
        root_fd = self._open_root(create=False)
        try:
            encoded_envelope = self._read_regular_file(
                root_fd, envelope_name, "INVALID_ENVELOPE"
            )
            if (
                hashlib.sha256(encoded_envelope).hexdigest()
                != expected_identity.envelope_file_sha256
            ):
                raise EvidenceError("TRUSTED_ENVELOPE_MISMATCH")
            raw = json.loads(encoded_envelope.decode("utf-8"))
            envelope = ResponseEnvelope(**raw)
        except EvidenceError:
            os.close(root_fd)
            raise
        except Exception:
            os.close(root_fd)
            raise EvidenceError("INVALID_ENVELOPE") from None
        try:
            try:
                expected_integrity = envelope.expected_integrity_sha256()
            except Exception:
                raise EvidenceError("INVALID_ENVELOPE") from None
            if (
                not envelope.envelope_integrity_sha256
                or envelope.envelope_integrity_sha256 != expected_integrity
                or envelope_name
                != f"{envelope.envelope_integrity_sha256}.envelope.json"
            ):
                raise EvidenceError("ENVELOPE_INTEGRITY_MISMATCH")
            contract = contracts.get(envelope.service_id)
            if (
                contract is None
                or contract.contract_sha256 != envelope.contract_sha256
                or envelope.provider != contract.provider
                or envelope.license_class != contract.license_class
                or envelope.redistribution != "restricted_local_only"
            ):
                raise EvidenceError("CONTRACT_METADATA_TAMPERED")
            if envelope.schema_version != SCHEMA_VERSION:
                raise EvidenceError("ENVELOPE_SCHEMA_MISMATCH")
            if canonical_sha256(envelope.request_identity) != envelope.request_sha256:
                raise EvidenceError("REQUEST_IDENTITY_TAMPERED")
            if (
                envelope.parser_version != PARSER_VERSION
                or envelope.parser_source_sha256 != parser_source_sha256()
            ):
                raise EvidenceError("PARSER_SOURCE_TAMPERED")
            if envelope.retention_deadline_utc:
                try:
                    deadline = datetime.fromisoformat(envelope.retention_deadline_utc)
                except (TypeError, ValueError):
                    raise EvidenceError("INVALID_RETENTION_DEADLINE") from None
                current = now_utc or datetime.now(timezone.utc)
                if current >= deadline:
                    raise EvidenceError("EVIDENCE_RETENTION_EXPIRED")
            if not re.fullmatch(r"[0-9a-f]{64}\.blob", envelope.blob_reference):
                raise EvidenceError("INVALID_BLOB_REFERENCE")
            body = self._read_regular_file(
                root_fd, envelope.blob_reference, "RAW_BLOB_UNAVAILABLE"
            )
            if len(body) != envelope.raw_byte_count:
                raise EvidenceError("RAW_BYTE_COUNT_MISMATCH")
            if hashlib.sha256(body).hexdigest() != envelope.raw_sha256:
                raise EvidenceError("RAW_BLOB_TAMPERED")
            if not self._visible_root_matches(root_fd):
                raise EvidenceError("EVIDENCE_ROOT_CHANGED")
            return envelope, body
        finally:
            os.close(root_fd)


_PROBE_AUTHORITY_TOKEN = object()
_PROBE_AUTHORITY_REGISTRY: dict[str, "_ProbeRunAuthority"] = {}
_PROBE_AUTHORITY_REGISTRY_LOCK = threading.Lock()
_PROBE_AUTHORITY_PID = os.getpid()


class _ProbeRunAuthority:
    """Non-resettable send authority for one frozen probe execution.

    The ledger remains useful as an auditable counter, but it is not its own
    authority.  This object holds the original root, lock and ledger file
    descriptors and an expected ledger digest that is never persisted.  The
    process registry keeps this authority alive and unique for the root for the
    rest of this probe execution.
    """

    def __init__(self, store: LocalEvidenceStore, token: object) -> None:
        if token is not _PROBE_AUTHORITY_TOKEN:
            raise EvidenceError("PROBE_AUTHORITY_INVALID")
        self.store = store
        self.root_fd = store._open_root(create=True)
        self.lock_name = f".{CANARY_DEFINITION_SHA256}.budget.lock"
        self.ledger_name = f".{CANARY_DEFINITION_SHA256}.budget.json"
        self._thread_lock = threading.Lock()
        self.lock_fd = -1
        self.ledger_fd = -1
        try:
            self.lock_fd = self._open_named_file(self.lock_name, create=True)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise EvidenceError("PROBE_AUTHORITY_UNAVAILABLE") from None
            if not self._named_fd_matches(self.lock_name, self.lock_fd):
                raise EvidenceError("PROBE_AUTHORITY_UNAVAILABLE")
            self.ledger_fd = self._open_named_file(self.ledger_name, create=True)
            if not self._named_fd_matches(self.ledger_name, self.ledger_fd):
                raise EvidenceError("BUDGET_LEDGER_UNAVAILABLE")
            initial = canonical_bytes(store._budget_record(0, 0)) + b"\n"
            self._write_ledger(initial)
            os.fsync(self.root_fd)
            if not store._visible_root_matches(self.root_fd):
                raise EvidenceError("EVIDENCE_ROOT_CHANGED")
            self._expected_ledger_sha256 = hashlib.sha256(initial).digest()
        except Exception:
            self._close()
            raise

    def _open_named_file(self, name: str, *, create: bool) -> int:
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self.root_fd)
        except OSError:
            raise EvidenceError("BUDGET_LEDGER_UNAVAILABLE") from None
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise EvidenceError("BUDGET_LEDGER_UNAVAILABLE")
        return descriptor

    def _named_fd_matches(self, name: str, descriptor: int) -> bool:
        try:
            visible = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
            opened = os.fstat(descriptor)
        except OSError:
            return False
        return stat.S_ISREG(visible.st_mode) and (
            visible.st_dev,
            visible.st_ino,
        ) == (opened.st_dev, opened.st_ino)

    def _read_ledger(self) -> bytes:
        os.lseek(self.ledger_fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(self.ledger_fd, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _write_ledger(self, payload: bytes) -> None:
        os.lseek(self.ledger_fd, 0, os.SEEK_SET)
        os.ftruncate(self.ledger_fd, 0)
        offset = 0
        while offset < len(payload):
            offset += os.write(self.ledger_fd, payload[offset:])
        os.fsync(self.ledger_fd)

    def _validate_binding(self) -> bytes:
        if not self.store._visible_root_matches(self.root_fd):
            raise EvidenceError("EVIDENCE_ROOT_CHANGED")
        if not self._named_fd_matches(self.lock_name, self.lock_fd):
            raise EvidenceError("PROBE_AUTHORITY_UNAVAILABLE")
        if not self._named_fd_matches(self.ledger_name, self.ledger_fd):
            raise EvidenceError("BUDGET_LEDGER_UNAVAILABLE")
        raw = self._read_ledger()
        if not secrets.compare_digest(
            hashlib.sha256(raw).digest(), self._expected_ledger_sha256
        ):
            raise EvidenceError("BUDGET_LEDGER_INVALID")
        return raw

    def consume(self, contract: ServiceContract) -> int:
        with self._thread_lock:
            raw = self._validate_binding()
            try:
                record = self.store._validate_budget_record(
                    json.loads(raw.decode("utf-8"))
                )
            except EvidenceError:
                raise
            except Exception:
                raise EvidenceError("BUDGET_LEDGER_INVALID") from None
            group = "core" if contract.core else "optional"
            used_key = f"{group}_used"
            cap_key = f"{group}_cap"
            if record[used_key] >= record[cap_key]:
                raise EvidenceError("REQUEST_BUDGET_EXHAUSTED")
            next_record = self.store._budget_record(
                record["core_used"] + int(contract.core),
                record["optional_used"] + int(not contract.core),
            )
            next_bytes = canonical_bytes(next_record) + b"\n"
            self._write_ledger(next_bytes)
            if self._validate_after_write(next_bytes):
                self._expected_ledger_sha256 = hashlib.sha256(next_bytes).digest()
            return next_record[used_key]

    def _validate_after_write(self, expected: bytes) -> bool:
        if not self.store._visible_root_matches(self.root_fd):
            raise EvidenceError("EVIDENCE_ROOT_CHANGED")
        if not self._named_fd_matches(self.lock_name, self.lock_fd):
            raise EvidenceError("PROBE_AUTHORITY_UNAVAILABLE")
        if not self._named_fd_matches(self.ledger_name, self.ledger_fd):
            raise EvidenceError("BUDGET_LEDGER_UNAVAILABLE")
        if not secrets.compare_digest(self._read_ledger(), expected):
            raise EvidenceError("BUDGET_LEDGER_INVALID")
        return True

    def _close(self, *, unlock: bool = True) -> None:
        if unlock and self.lock_fd >= 0:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in (self.ledger_fd, self.lock_fd, self.root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.ledger_fd = self.lock_fd = self.root_fd = -1


def _probe_authority_for_store(store: LocalEvidenceStore) -> _ProbeRunAuthority:
    global _PROBE_AUTHORITY_PID, _PROBE_AUTHORITY_REGISTRY_LOCK
    key = str(store.root)
    current_pid = os.getpid()
    if current_pid != _PROBE_AUTHORITY_PID:
        # This check must precede the inherited lock: a fork may copy that lock
        # while another parent thread owns it. An inherited live authority stays
        # in the same frozen execution lineage even after the parent exits.
        if _PROBE_AUTHORITY_REGISTRY:
            raise EvidenceError("PROBE_AUTHORITY_FORKED")
        _PROBE_AUTHORITY_PID = current_pid
        _PROBE_AUTHORITY_REGISTRY_LOCK = threading.Lock()
    with _PROBE_AUTHORITY_REGISTRY_LOCK:
        authority = _PROBE_AUTHORITY_REGISTRY.get(key)
        if authority is None:
            authority = _ProbeRunAuthority(store, _PROBE_AUTHORITY_TOKEN)
            _PROBE_AUTHORITY_REGISTRY[key] = authority
        return authority


def execute_authenticated_request(
    *,
    contract: ServiceContract,
    parameters: Mapping[str, Any],
    credential: str,
    intended_market_date: str,
    store: LocalEvidenceStore,
    environment: Mapping[str, str],
    transport: Callable[
        [str, str, Mapping[str, Any], Mapping[str, str]],
        tuple[int, Mapping[str, str], bytes],
    ],
) -> CapturedEvidence:
    """Send one request without exposing provider values to output/errors."""
    access = service_access(contract, environment)
    expected_credential = environment.get(contract.credential_environment, "")
    if (
        access["status"] != "APPROVED"
        or not credential
        or credential != expected_credential
        or (contract.core and not all_core_services_approved(environment))
    ):
        raise EvidenceError("AUTH_REQUIRED")
    identity = sanitize_request_identity(
        endpoint=contract.endpoint,
        method=contract.method,
        parameters=parameters,
        known_credentials=(credential,),
    )
    wire_parameters = dict(parameters)
    wire_headers: dict[str, str] = {}
    if contract.provider == "KRX_OPEN_API":
        wire_headers["AUTH_KEY"] = credential
    elif contract.provider == "FSC_DATA_GO_KR":
        wire_parameters["serviceKey"] = credential
    elif contract.provider == "OPEN_DART":
        wire_parameters["crtfc_key"] = credential
    # The process-run frozen-canary authority is claimed immediately before the
    # only transport boundary. Failed attempts count toward the same run.
    store.consume_transport_budget(contract)
    try:
        status, headers, body = transport(
            contract.method, contract.endpoint, wire_parameters, wire_headers
        )
    except Exception:
        # Chaining provider exceptions could leak authenticated values when a
        # caller renders a traceback, even though this message is categorical.
        raise EvidenceError("PROVIDER_TRANSPORT_ERROR") from None
    envelope = store.capture(
        contract=contract,
        request_identity=identity,
        intended_market_date=intended_market_date,
        status_code=status,
        headers=headers,
        body=body,
        known_credentials=(credential,),
    )
    if not 200 <= status < 300:
        raise EvidenceError("PROVIDER_HTTP_ERROR_CAPTURED")
    return envelope


def _number(value: Any, *, positive: bool, code: str) -> float:
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        raise EvidenceError(code) from None
    if not math.isfinite(parsed) or parsed < 0 or (positive and parsed <= 0):
        raise EvidenceError(code)
    return parsed


def parse_krx_json(
    body: bytes, *, expected_collection: str = "OutBlock_1"
) -> list[dict[str, Any]]:
    """Parse a KRX page without including payload fields in any exception."""
    try:
        payload = json.loads(body)
    except Exception:
        raise EvidenceError("PROVIDER_JSON_INVALID") from None
    if not isinstance(payload, dict):
        raise EvidenceError("PROVIDER_SCHEMA_DRIFT")
    error_keys = any(
        key.casefold() in {"err_cd", "error", "message"} for key in payload
    )
    if error_keys and expected_collection not in payload:
        raise EvidenceError("PROVIDER_ERROR_PAYLOAD")
    rows = payload.get(expected_collection)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EvidenceError("PROVIDER_SCHEMA_DRIFT")
    return [dict(row) for row in rows]


@dataclass(frozen=True)
class StockObservation:
    market_date: str
    market: str
    short_code: str
    isin: str
    security_group: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float
    listed_shares: float


def reconcile_stock_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    requested_date: str,
    market: str,
    short_code: str,
    complete_pagination: bool,
) -> StockObservation:
    if not complete_pagination:
        raise EvidenceError("PAGINATION_INCOMPLETE")
    if market not in SUPPORTED_MARKETS:
        raise EvidenceError("MARKET_UNSUPPORTED")
    matches = [row for row in rows if str(row.get("short_code", "")) == short_code]
    if not matches:
        raise EvidenceError("SHORT_CODE_NOT_OBSERVED")
    if len(matches) != 1:
        raise EvidenceError("SHORT_CODE_AMBIGUOUS")
    row = matches[0]
    if row.get("market_date") != requested_date:
        raise EvidenceError("REQUESTED_DATE_MISMATCH")
    if row.get("market") != market:
        raise EvidenceError("MARKET_MISMATCH")
    isin = str(row.get("isin", ""))
    if not re.fullmatch(r"KR[A-Z0-9]{10}", isin):
        raise EvidenceError("ISIN_INVALID")
    group = str(row.get("security_group", ""))
    if group not in SUPPORTED_INSTRUMENTS:
        raise EvidenceError("INSTRUMENT_UNSUPPORTED")
    open_price = _number(row.get("open"), positive=True, code="PRICE_INVALID")
    high = _number(row.get("high"), positive=True, code="PRICE_INVALID")
    low = _number(row.get("low"), positive=True, code="PRICE_INVALID")
    close = _number(row.get("close"), positive=True, code="PRICE_INVALID")
    if low > min(open_price, close) or high < max(open_price, close) or low > high:
        raise EvidenceError("OHLC_ORDER_INVALID")
    return StockObservation(
        market_date=requested_date,
        market=market,
        short_code=short_code,
        isin=isin,
        security_group=group,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=_number(row.get("volume"), positive=False, code="VOLUME_INVALID"),
        value=_number(row.get("value"), positive=False, code="VALUE_INVALID"),
        listed_shares=_number(
            row.get("listed_shares"), positive=True, code="LISTED_SHARES_INVALID"
        ),
    )


def reconcile_benchmark_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    requested_date: str,
    stable_index_code: str,
) -> dict[str, Any]:
    if not stable_index_code:
        raise EvidenceError("BENCHMARK_CODE_REQUIRED")
    matches = [row for row in rows if row.get("index_code") == stable_index_code]
    if not matches:
        raise EvidenceError("BENCHMARK_SESSION_MISSING")
    if len(matches) != 1:
        raise EvidenceError("BENCHMARK_AMBIGUOUS")
    row = matches[0]
    if row.get("market_date") != requested_date:
        raise EvidenceError("REQUESTED_DATE_MISMATCH")
    close = _number(row.get("close"), positive=True, code="BENCHMARK_PRICE_INVALID")
    return {
        "market_date": requested_date,
        "index_code": stable_index_code,
        "close": close,
    }


def classify_identity_bridge(
    *, historical_effective_evidence: bool, current_mapping: bool
) -> str:
    if historical_effective_evidence:
        return "HISTORICAL_BRIDGE_SUPPORTED"
    if current_mapping:
        return "CURRENT_ONLY_NOT_HISTORICAL_PROOF"
    return "NOT_OBSERVED"


def classify_corporate_action(
    *, raw_adjusted_difference: bool, action_evidence: bool
) -> str:
    if raw_adjusted_difference and not action_evidence:
        return "CORPORATE_ACTION_QUARANTINE"
    return "NO_UNEXPLAINED_DIFFERENCE"


RESEARCH_PRECEDENCE = (
    "AUTH_REQUIRED",
    "NO_GO_REPRODUCIBILITY",
    "NO_GO_SOURCE_SCHEMA",
    "PARTIAL_IDENTITY_BRIDGE",
)


def decide_research(blockers: Iterable[str]) -> dict[str, Any]:
    ordered = sorted(set(blockers))
    prefixes = {item.split(":", 1)[0] for item in ordered}
    summary = next(
        (candidate for candidate in RESEARCH_PRECEDENCE if candidate in prefixes),
        "GO_RESEARCH_BACKFILL",
    )
    return {"summary": summary, "blockers": ordered}


def apply_diagnostics(
    primary_blockers: Iterable[str], diagnostic_blockers: Iterable[str]
) -> dict[str, Any]:
    """Diagnostics may add blockers; they can never remove or promote."""
    return decide_research([*primary_blockers, *diagnostic_blockers])


def decide_production(
    *,
    public_terms_prohibit: bool,
    superseding_agreement: bool,
    unclear: bool = False,
) -> str:
    if superseding_agreement:
        return "GO_PRODUCTION_LICENSED"
    if public_terms_prohibit:
        return "NO_GO_LICENSE"
    if unclear:
        return "LICENSE_REVIEW_REQUIRED"
    return "LICENSE_REVIEW_REQUIRED"


def canary_request_budget(counts: Mapping[str, int]) -> dict[str, int]:
    primary = int(counts["base_krx_requests_three_endpoint_families"])
    versions = int(
        counts["second_version_requests_two_market_dates_three_endpoint_families"]
    )
    hard = int(counts["hard_core_krx_request_cap_including_retries"])
    if primary != 1173 or versions != 6 or hard != 1800:
        raise EvidenceError("CANARY_BUDGET_MISMATCH")
    return {
        "primary": primary,
        "second_versions": versions,
        "diagnostic_cap": 360,
        "retry_cap": 261,
        "hard_cap": hard,
        "optional_combined_cap": 600,
    }


class RequestBudget:
    def __init__(self, cap: int) -> None:
        if cap < 0:
            raise ValueError("request cap must be nonnegative")
        self.cap = cap
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.cap:
            raise EvidenceError("REQUEST_BUDGET_EXHAUSTED")
        self.used += 1


@dataclass(frozen=True)
class ProbeRequest:
    service_id: str
    market: str
    market_date: str
    version: str
    parameters: Mapping[str, str]


def build_frozen_core_probe_requests(
    definition: Mapping[str, Any], environment: Mapping[str, str]
) -> list[ProbeRequest]:
    """Build the 1,179 precommitted requests only after all six approvals."""
    if not all_core_services_approved(environment):
        raise EvidenceError("AUTH_REQUIRED")
    budget = RequestBudget(1800)
    by_market_family = {
        ("KOSPI", "stock_daily"): "kospi_stock_daily",
        ("KOSDAQ", "stock_daily"): "kosdaq_stock_daily",
        ("KOSPI", "security_basic"): "kospi_security_basic",
        ("KOSDAQ", "security_basic"): "kosdaq_security_basic",
        ("KOSPI", "index_daily"): "kospi_index_daily",
        ("KOSDAQ", "index_daily"): "kosdaq_index_daily",
    }
    requests: list[ProbeRequest] = []
    for item in definition["market_date_pairs"]:
        for family in ("stock_daily", "security_basic", "index_daily"):
            budget.consume()
            requests.append(
                ProbeRequest(
                    service_id=by_market_family[(item["market"], family)],
                    market=item["market"],
                    market_date=item["date"],
                    version="primary",
                    parameters={"basDd": item["date"].replace("-", "")},
                )
            )
    for item in definition["second_version_market_date_pairs"]:
        for family in ("stock_daily", "security_basic", "index_daily"):
            budget.consume()
            requests.append(
                ProbeRequest(
                    service_id=by_market_family[(item["market"], family)],
                    market=item["market"],
                    market_date=item["date"],
                    version="second_version",
                    parameters={"basDd": item["date"].replace("-", "")},
                )
            )
    if len(requests) != 1179:
        raise EvidenceError("CANARY_REQUEST_CENSUS_MISMATCH")
    return requests


def verify_canary_definition(path: Path, root: Path) -> dict[str, Any]:
    expected_file_hash = (
        "cb42e612ecb226a4b1e664722b86355e7d2e00d66039d4efc7fa48f42a3165d1"
    )
    if file_sha256(path) != expected_file_hash:
        raise EvidenceError("CANARY_FILE_HASH_MISMATCH")
    definition = json.loads(path.read_text(encoding="utf-8"))
    selection_hash = canonical_sha256(definition.get("selection"))
    expected_selection_hash = (
        "aa89e2580c81eb445ff1cbe068cfc6a3216a1c2533f6eb210a28062dbc0e4716"
    )
    if (
        selection_hash != expected_selection_hash
        or definition.get("selection_sha256") != selection_hash
    ):
        raise EvidenceError("CANARY_SELECTION_HASH_MISMATCH")
    canonical_definition = dict(definition)
    claimed_definition_hash = canonical_definition.pop("definition_sha256", None)
    definition_hash = canonical_sha256(canonical_definition)
    expected_definition_hash = (
        "ac94f3b62045823e759389bac641199571c4c034e9a823a5fce2dfb45df2f3f6"
    )
    if (
        definition_hash != expected_definition_hash
        or claimed_definition_hash != definition_hash
    ):
        raise EvidenceError("CANARY_DEFINITION_HASH_MISMATCH")
    counts = definition.get("counts", {})
    if (
        len(definition.get("selection", [])) != 141
        or len(definition.get("market_date_pairs", [])) != 391
        or counts.get("unique_receipts") != 141
        or counts.get("market_date_pairs_including_event_and_recorded_window_dates")
        != 391
    ):
        raise EvidenceError("CANARY_CENSUS_MISMATCH")
    canary_request_budget(counts)
    for pin in definition.get("source_pins", []):
        source = root / pin["path"]
        if (
            not source.is_file()
            or source.stat().st_size != pin["bytes"]
            or file_sha256(source) != pin["sha256"]
        ):
            raise EvidenceError("CANARY_SOURCE_PIN_MISMATCH")
    return definition


def choose_diagnostics(
    mismatches: Sequence[Mapping[str, str]], *, cap_receipts: int = 24
) -> list[Mapping[str, str]]:
    """Choose deterministic diagnostics by code then seeded receipt hash."""
    if cap_receipts < 0 or cap_receipts > 24:
        raise EvidenceError("DIAGNOSTIC_RECEIPT_CAP_INVALID")
    seed = "m0.8-buyback-canary-v1"
    ranked = sorted(
        mismatches,
        key=lambda item: (
            str(item["mismatch_code"]),
            hashlib.sha256(
                f"{seed}|diagnostic|{item['receipt_no']}".encode("utf-8")
            ).hexdigest(),
            canonical_sha256(dict(item)),
        ),
    )
    unique: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for item in ranked:
        receipt = str(item["receipt_no"])
        if receipt in seen:
            continue
        seen.add(receipt)
        unique.append(item)
        if len(unique) == cap_receipts:
            break
    return unique


@dataclass(frozen=True)
class DiagnosticPlan:
    receipts: tuple[Mapping[str, Any], ...]
    market_date_pairs: tuple[tuple[str, str], ...]
    request_count: int


def plan_diagnostics(
    mismatches: Sequence[Mapping[str, Any]],
    *,
    cap_receipts: int = 24,
    cap_market_date_pairs: int = 120,
    cap_calls: int = 360,
) -> DiagnosticPlan:
    """Select a deterministic diagnostic subset under all three hard caps."""
    if cap_market_date_pairs < 0 or cap_market_date_pairs > 120:
        raise EvidenceError("DIAGNOSTIC_PAIR_CAP_INVALID")
    if cap_calls < 0 or cap_calls > 360:
        raise EvidenceError("DIAGNOSTIC_CALL_CAP_INVALID")
    chosen = choose_diagnostics(mismatches, cap_receipts=cap_receipts)
    admitted: list[Mapping[str, Any]] = []
    pairs: set[tuple[str, str]] = set()
    for item in chosen:
        candidate_pairs: set[tuple[str, str]] = set()
        raw_pairs = item.get("market_date_pairs", ())
        if not isinstance(raw_pairs, Sequence) or isinstance(raw_pairs, (str, bytes)):
            raise EvidenceError("DIAGNOSTIC_PAIR_INVALID")
        for pair in raw_pairs:
            if not isinstance(pair, Mapping):
                raise EvidenceError("DIAGNOSTIC_PAIR_INVALID")
            market = str(pair.get("market", ""))
            market_date = str(pair.get("date", ""))
            if market not in SUPPORTED_MARKETS or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", market_date
            ):
                raise EvidenceError("DIAGNOSTIC_PAIR_INVALID")
            candidate_pairs.add((market, market_date))
        combined = pairs | candidate_pairs
        call_count = len(combined) * 3
        if len(combined) > cap_market_date_pairs or call_count > cap_calls:
            continue
        pairs = combined
        admitted.append(item)
    ordered_pairs = tuple(sorted(pairs))
    if len(admitted) > 24 or len(ordered_pairs) > 120 or len(ordered_pairs) * 3 > 360:
        raise EvidenceError("DIAGNOSTIC_CAP_EXCEEDED")
    return DiagnosticPlan(
        receipts=tuple(admitted),
        market_date_pairs=ordered_pairs,
        request_count=len(ordered_pairs) * 3,
    )
