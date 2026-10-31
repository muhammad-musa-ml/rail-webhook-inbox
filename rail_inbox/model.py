"""A deliberately conservative adapter for the public geofence example shape."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

MAX_PAYLOAD_BYTES = 65_536
MAX_EQUIPMENT = 300


class InvalidEvent(ValueError):
    """Input cannot be interpreted without guessing."""


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidEvent(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise InvalidEvent(f"non-finite JSON number: {value}")


def parse_json(raw: str) -> Any:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 12:
                raise InvalidEvent("JSON nesting exceeds 12 levels")
            if isinstance(item, float) and not math.isfinite(item):
                raise InvalidEvent("non-finite JSON number")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, RecursionError) as exc:
        raise InvalidEvent(f"invalid JSON: {exc}") from exc


def _text(value: object, field: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidEvent(f"{field} must be a non-empty string of at most {limit} characters")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise InvalidEvent(f"{field} contains unsupported characters")
    return value


def utc_time(date: object, clock: object, source_offset: str | None) -> str:
    if source_offset is None or not re.fullmatch(r"[+-](?:0\d|1[0-4]):[0-5]\d", source_offset):
        raise InvalidEvent("explicit source offset required, for example +00:00")
    hours, minutes = map(int, source_offset[1:].split(":"))
    if (hours == 14 and minutes != 0) or source_offset == "-00:00":
        raise InvalidEvent("source offset must be known and within +/-14:00")
    if not isinstance(date, str) or not re.fullmatch(r"\d{2}-\d{2}-\d{4}", date):
        raise InvalidEvent("eventDate must be MM-DD-YYYY")
    if not isinstance(clock, str) or not re.fullmatch(r"\d{2}:\d{2}", clock):
        raise InvalidEvent("eventTime must be HH:MM")
    try:
        local = datetime.strptime(f"{date} {clock}", "%m-%d-%Y %H:%M")
        offset = timedelta(hours=hours, minutes=minutes)
        if source_offset[0] == "-":
            offset = -offset
        return local.replace(tzinfo=timezone(offset)).astimezone(UTC).isoformat()
    except (ValueError, OverflowError) as exc:
        raise InvalidEvent("invalid calendar date, clock time or UTC conversion") from exc


@dataclass(frozen=True)
class Observation:
    customer_id: int
    equipment_id: str
    occurred_at: str
    fingerprint: str
    details_json: str


@dataclass(frozen=True)
class Event:
    event_hash: str
    observations: tuple[Observation, ...]


def normalize(raw: str, source_offset: str | None) -> Event:
    payload = parse_json(raw)
    if not isinstance(payload, dict) or set(payload) != {"geofenceNotification"}:
        raise InvalidEvent("expected one geofenceNotification object")
    notification = payload["geofenceNotification"]
    if not isinstance(notification, dict):
        raise InvalidEvent("geofenceNotification must be an object")
    if {"equipmentId", "occurredAt"} & notification.keys():
        raise InvalidEvent("provider header contains a reserved normalized field")
    customer = notification.get("customerId")
    if type(customer) is not int or not 1 <= customer <= 2**63 - 1:
        raise InvalidEvent("customerId must be a positive signed 64-bit integer")
    _text(notification.get("geofenceName"), "geofenceName")
    _text(notification.get("trainId"), "trainId")
    equipment = notification.get("equipmentList")
    if not isinstance(equipment, list) or not 1 <= len(equipment) <= MAX_EQUIPMENT:
        raise InvalidEvent(f"equipmentList must contain 1..{MAX_EQUIPMENT} entries")
    equipment_ids: list[str] = []
    for item in equipment:
        if not isinstance(item, dict) or set(item) != {"equipmentId"}:
            raise InvalidEvent("each equipment entry must contain only equipmentId")
        equipment_ids.append(_text(item["equipmentId"], "equipmentId", limit=40))
    if len(set(equipment_ids)) != len(equipment_ids):
        raise InvalidEvent("equipmentList contains repeated equipmentId")
    count = notification.get("equipmentCount")
    if type(count) is not int or count != len(equipment_ids):
        raise InvalidEvent("equipmentCount does not match equipmentList")
    occurred_at = utc_time(
        notification.get("eventDate"), notification.get("eventTime"), source_offset
    )
    # Preserve every header field, including unknown ones. A changed header is a
    # distinct candidate; silently dropping it could hide a meaningful difference.
    header = {
        key: value
        for key, value in notification.items()
        if key not in {"equipmentList", "equipmentCount", "eventDate", "eventTime"}
    }
    header["occurredAt"] = occurred_at
    observations: list[Observation] = []
    for equipment_id in sorted(equipment_ids):
        details = {**header, "equipmentId": equipment_id}
        observations.append(
            Observation(customer, equipment_id, occurred_at, digest(details), canonical(details))
        )
    return Event(digest([item.fingerprint for item in observations]), tuple(observations))
