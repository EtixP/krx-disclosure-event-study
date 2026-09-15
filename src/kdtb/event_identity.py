from __future__ import annotations

import hashlib
import json
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kdtb.schemas.economic_event import EconomicEvent

_RECEIPT_NO = re.compile(r"^[0-9]{14}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTENDED_SNAPSHOT_KEY = "__kdtb_canonical_event_snapshot__"
_EXTENDED_SNAPSHOT_VERSION = "m1.2-non-finite-v1"
_NON_FINITE_KINDS = frozenset({"nan", "negative_infinity", "positive_infinity"})


def _non_finite_values(
    value: object,
    *,
    path: tuple[str | int, ...] = (),
) -> tuple[tuple[tuple[str | int, ...], str], ...]:
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            kind = "nan"
        elif value > 0:
            kind = "positive_infinity"
        else:
            kind = "negative_infinity"
        return ((path, kind),)
    if isinstance(value, dict):
        return tuple(
            item
            for key in sorted(value, key=lambda item: str(item))
            for item in _non_finite_values(
                value[key],
                path=(*path, str(key)),
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            item
            for index, nested in enumerate(value)
            for item in _non_finite_values(nested, path=(*path, index))
        )
    return ()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _restore_non_finite_values(
    event_value: object,
    metadata: object,
) -> None:
    if not isinstance(metadata, list) or not metadata:
        raise ValueError("extended event snapshot requires non-finite metadata")
    for item in metadata:
        if not isinstance(item, dict) or set(item) != {"kind", "path"}:
            raise ValueError("non-finite event metadata is malformed")
        kind = item["kind"]
        path = item["path"]
        if kind not in _NON_FINITE_KINDS:
            raise ValueError("non-finite event kind is invalid")
        if not isinstance(path, list) or not path:
            raise ValueError("non-finite event path is invalid")
        if any(
            isinstance(component, bool)
            or (isinstance(component, int) and component < 0)
            or (isinstance(component, str) and not component)
            or not isinstance(component, (str, int))
            for component in path
        ):
            raise ValueError("non-finite event path is invalid")

        current = event_value
        for component in path[:-1]:
            if isinstance(component, int):
                if not isinstance(current, list) or component >= len(current):
                    raise ValueError("non-finite event path does not exist")
                current = current[component]
            else:
                if not isinstance(current, dict) or component not in current:
                    raise ValueError("non-finite event path does not exist")
                current = current[component]
        final = path[-1]
        if isinstance(final, int):
            if not isinstance(current, list) or final >= len(current):
                raise ValueError("non-finite event path does not exist")
            if current[final] is not None:
                raise ValueError("non-finite event path must replace canonical null")
        else:
            if not isinstance(current, dict) or final not in current:
                raise ValueError("non-finite event path does not exist")
            if current[final] is not None:
                raise ValueError("non-finite event path must replace canonical null")
        current[final] = {
            "nan": float("nan"),
            "negative_infinity": float("-inf"),
            "positive_infinity": float("inf"),
        }[kind]


def _canonical_event_snapshot_unchecked(
    event: EconomicEvent,
) -> tuple[str, str]:
    event_value = event.model_dump(mode="json")
    non_finite_values = _non_finite_values(event.model_dump(mode="python"))
    if non_finite_values:
        snapshot: object = {
            _EXTENDED_SNAPSHOT_KEY: {
                "encoding": _EXTENDED_SNAPSHOT_VERSION,
                "event": event_value,
                "non_finite_values": [
                    {"kind": kind, "path": list(path)}
                    for path, kind in non_finite_values
                ],
            }
        }
    else:
        snapshot = event_value
    payload = _canonical_json(snapshot)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, digest


def _source_values_equal(left: object, right: object) -> bool:
    """Compare source values without bool/int, container, or NaN coercion."""

    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        return math.isnan(left) and math.isnan(right) or left == right
    if isinstance(left, dict):
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(unmatched):
                if _source_values_equal(left_key, right_key):
                    if not _source_values_equal(left_value, right_value):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _source_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def canonical_event_snapshot(event: EconomicEvent) -> tuple[str, str]:
    """Return canonical bytes only when they preserve the complete source event."""

    payload, digest = _canonical_event_snapshot_unchecked(event)
    try:
        restored = event_from_canonical_snapshot(payload)
    except Exception as error:
        raise ValueError(
            "canonical event snapshot cannot losslessly round-trip source values"
        ) from error
    if not _source_values_equal(
        event.model_dump(mode="python"),
        restored.model_dump(mode="python"),
    ):
        raise ValueError(
            "canonical event snapshot cannot losslessly round-trip source values"
        )
    return payload, digest


def event_from_canonical_snapshot(payload: str) -> EconomicEvent:
    """Load and semantically verify exact canonical M1.2 event bytes."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"event snapshot contains non-standard JSON value {value}")

    decoded = json.loads(payload, parse_constant=reject_constant)
    if isinstance(decoded, dict) and set(decoded) == {_EXTENDED_SNAPSHOT_KEY}:
        extended = decoded[_EXTENDED_SNAPSHOT_KEY]
        if not isinstance(extended, dict) or set(extended) != {
            "encoding",
            "event",
            "non_finite_values",
        }:
            raise ValueError("extended event snapshot is malformed")
        if extended["encoding"] != _EXTENDED_SNAPSHOT_VERSION:
            raise ValueError("extended event snapshot encoding is unsupported")
        event_value = extended["event"]
        _restore_non_finite_values(
            event_value,
            extended["non_finite_values"],
        )
    else:
        event_value = decoded

    from kdtb.schemas.economic_event import EconomicEvent

    event = EconomicEvent.model_validate(event_value)
    canonical_payload, _ = _canonical_event_snapshot_unchecked(event)
    if canonical_payload != payload:
        raise ValueError("event snapshot is not canonical")
    return event


def delivery_id_for_snapshot(
    *,
    trigger_receipt_no: str,
    event_sha256: str,
) -> str:
    """Derive M1.2's stable delivery ID from one immutable snapshot."""

    if _RECEIPT_NO.fullmatch(trigger_receipt_no) is None:
        raise ValueError("delivery trigger must be a 14-digit DART receipt number")
    if _SHA256.fullmatch(event_sha256) is None:
        raise ValueError("event snapshot hash must be a lowercase SHA-256")
    value = f"m1.2:{trigger_receipt_no}:{event_sha256}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def delivery_id_for_event(
    *,
    trigger_receipt_no: str,
    event: EconomicEvent,
) -> str:
    """Recompute a delivery ID directly from its trigger and event state."""

    _, event_sha256 = canonical_event_snapshot(event)
    return delivery_id_for_snapshot(
        trigger_receipt_no=trigger_receipt_no,
        event_sha256=event_sha256,
    )
