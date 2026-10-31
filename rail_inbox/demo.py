"""Reproducible fault scenario; all identifiers and operating details are synthetic."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rail_inbox.model import InvalidEvent, normalize
from rail_inbox.store import Inbox


def notification(
    clock: str = "09:00",
    equipment: tuple[str, ...] = ("DEMO0001",),
    fence: str = "Synthetic East Boundary",
    message: str = "Synthetic notification",
    customer: int = 900001,
) -> dict[str, Any]:
    return {
        "geofenceNotification": {
            "customerId": customer,
            "sfdcId": "synthetic-record-001",
            "geofenceName": fence,
            "geofenceExpirationDate": "2030-12-31",
            "geofenceCustomerMessage": message,
            "eventDate": "10-01-2026",
            "eventTime": clock,
            "trainId": "SYNTHETIC-TRAIN-01",
            "customer633": "DEMO-CUSTOMER",
            "equipmentCount": len(equipment),
            "equipmentList": [{"equipmentId": item} for item in equipment],
            "loadEmpty": "EMPTY",
            "commodity": "SYNTHETIC",
            "origin333": "DEMO-ORIGIN",
            "destination333": "DEMO-DESTINATION",
        }
    }


def fixture() -> list[dict[str, Any]]:
    initial = notification()
    invalid = notification(equipment=("DEMO0004",))
    invalid["geofenceNotification"]["equipmentCount"] = 2
    duplicate = notification()
    duplicate["geofenceNotification"] = dict(
        reversed(list(duplicate["geofenceNotification"].items()))
    )
    return [
        {"label": "initial", "worker_available": True, "payload": initial},
        {
            "label": "during-worker-outage",
            "worker_available": False,
            "payload": notification("11:00", ("DEMO0001", "DEMO0003")),
        },
        {"label": "late", "worker_available": True, "payload": notification("10:00")},
        {"label": "redelivery", "worker_available": True, "payload": duplicate},
        {
            "label": "first-same-minute-candidate",
            "worker_available": True,
            "payload": notification("10:00", ("DEMO0002",)),
        },
        {
            "label": "conflicting-same-minute-candidate",
            "worker_available": True,
            "payload": notification("10:00", ("DEMO0002",), message="Different synthetic message"),
        },
        {"label": "invalid-count", "worker_available": True, "payload": invalid},
    ]


def run_demo() -> dict[str, Any]:
    baseline: dict[str, dict[str, Any]] = {}
    dropped = 0
    pending_before_restart = 0
    with TemporaryDirectory(prefix="rail-inbox-") as directory:
        database = Path(directory) / "demo.db"
        for item in fixture():
            raw = json.dumps(item["payload"])
            with Inbox(database) as inbox:
                inbox.receive(raw, "+00:00")
                if not item["worker_available"]:
                    dropped += 1
                    pending_before_restart = inbox.stats()["pending"]
                    continue
                inbox.drain()
            # Explicit teaching baseline: no durable queue, no deduplication,
            # and arrival order overwrites each asset's last displayed event.
            try:
                event = normalize(raw, "+00:00")
            except InvalidEvent:
                continue
            for observation in event.observations:
                baseline[observation.equipment_id] = {
                    "occurred_at": observation.occurred_at,
                    "status": "current",
                }
        with Inbox(database) as recovered:
            recovered.drain()
            before = recovered.snapshot()
            audit_before = recovered.audit()
            recovered.replay()
            replay_matches = before == recovered.snapshot() and audit_before == recovered.audit()
            expected = {
                "DEMO0001": {"occurred_at": "2026-10-01T11:00:00+00:00", "status": "current"},
                "DEMO0002": {"occurred_at": "2026-10-01T10:00:00+00:00", "status": "ambiguous"},
                "DEMO0003": {"occurred_at": "2026-10-01T11:00:00+00:00", "status": "current"},
            }
            actual = {
                item["equipment_id"]: {
                    "occurred_at": item["occurred_at"],
                    "status": item["status"],
                }
                for item in before
            }
            return {
                "scenario": "seven synthetic deliveries, three equipment IDs, one worker outage",
                "source_offset": "+00:00 (fixture assumption, not a BNSF timezone claim)",
                "baseline": {
                    "description": "in-memory, arrival-order last-write-wins consumer",
                    "dropped_deliveries": dropped,
                    "incorrect_asset_projections": sum(
                        baseline.get(key) != value for key, value in expected.items()
                    ),
                    "snapshot": baseline,
                },
                "durable_inbox": {
                    "persisted_pending_before_worker_restart": pending_before_restart,
                    **recovered.stats(),
                    "incorrect_asset_projections": sum(
                        actual.get(key) != value for key, value in expected.items()
                    ),
                    "replay_matches_snapshot_and_audit": replay_matches,
                    "snapshot": actual,
                    "audit": audit_before,
                },
                "oracle": expected,
            }
