from __future__ import annotations

import json
import unittest

from rail_inbox.demo import notification
from rail_inbox.model import InvalidEvent, normalize, utc_time


class ModelTests(unittest.TestCase):
    def test_object_and_equipment_order_do_not_change_identity(self) -> None:
        payload = notification(equipment=("DEMO0002", "DEMO0001"))
        original = normalize(json.dumps(payload), "+00:00")
        nested = payload["geofenceNotification"]
        nested["equipmentList"].reverse()
        payload["geofenceNotification"] = dict(reversed(list(nested.items())))
        self.assertEqual(original, normalize(json.dumps(payload), "+00:00"))

    def test_offsets_normalize_same_instant(self) -> None:
        first = normalize(json.dumps(notification("10:00")), "+01:00")
        second = normalize(json.dumps(notification("09:00")), "+00:00")
        self.assertEqual(first, second)

    def test_unknown_header_changes_are_not_silently_discarded(self) -> None:
        payload = notification()
        first = normalize(json.dumps(payload), "+00:00")
        payload["geofenceNotification"]["newProviderField"] = "changed"
        self.assertNotEqual(first.event_hash, normalize(json.dumps(payload), "+00:00").event_hash)

    def test_bad_payloads(self) -> None:
        examples = [
            "{",
            "[]",
            "null",
            '{"geofenceNotification":null}',
            '{"a":1,"a":2}',
            '{"n":NaN}',
            '{"n":Infinity}',
            '{"n":1e999}',
            "[" * 2000,
            "[" * 15 + "0" + "]" * 15,
        ]
        for raw in examples:
            with self.subTest(raw=raw[:30]), self.assertRaises(InvalidEvent):
                normalize(raw, "+00:00")

    def test_invalid_fields(self) -> None:
        for field, value in (
            ("customerId", True),
            ("customerId", 2**63),
            ("customerId", -1),
            ("geofenceName", ""),
            ("trainId", "x\n"),
            ("trainId", "\ud800"),
            ("equipmentCount", 2),
            ("equipmentCount", True),
            ("equipmentList", []),
            ("equipmentList", [{"equipmentId": ""}]),
            ("equipmentList", [{"equipmentId": "X"}, {"equipmentId": "X"}]),
            ("eventDate", "02-29-2025"),
            ("eventTime", "25:00"),
            ("occurredAt", "unexpected"),
            ("equipmentId", "unexpected"),
        ):
            payload = notification()
            payload["geofenceNotification"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(InvalidEvent):
                normalize(json.dumps(payload), "+00:00")

    def test_source_offset_is_explicit_and_bounded(self) -> None:
        for offset in (None, "UTC", "-00:00", "+14:01", "+25:00", "+1:00"):
            with self.subTest(offset=offset), self.assertRaises(InvalidEvent):
                utc_time("10-01-2026", "09:00", offset)
        self.assertEqual(utc_time("10-01-2026", "09:00", "-05:30"), "2026-10-01T14:30:00+00:00")

    def test_calendar_validation_is_strict(self) -> None:
        for date in ("1-01-2026", "02-30-2026", "00-01-2026"):
            with self.subTest(date=date), self.assertRaises(InvalidEvent):
                utc_time(date, "09:00", "+00:00")


if __name__ == "__main__":
    unittest.main()
