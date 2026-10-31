"""Single-host durable inbox with transactional projections and replay."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rail_inbox.model import MAX_PAYLOAD_BYTES, InvalidEvent, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_payload TEXT NOT NULL,
    source_offset TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processed','duplicate','rejected')),
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    event_hash TEXT PRIMARY KEY,
    first_receipt INTEGER NOT NULL REFERENCES receipts(id)
);
CREATE TABLE IF NOT EXISTS observations (
    fingerprint TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    equipment_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    first_receipt INTEGER NOT NULL REFERENCES receipts(id)
);
CREATE INDEX IF NOT EXISTS asset_time
    ON observations(customer_id, equipment_id, occurred_at DESC);
CREATE TABLE IF NOT EXISTS audit (
    receipt_id INTEGER PRIMARY KEY REFERENCES receipts(id),
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Inbox:
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        mode = self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode not in {"delete", "memory"}:
            self.connection.close()
            raise sqlite3.OperationalError("rollback journal mode could not be enabled")
        self.connection.execute("PRAGMA synchronous=EXTRA")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Inbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def receive(self, raw: str, source_offset: str | None) -> int:
        """Return a receipt only after durable commit; never process inline."""
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise InvalidEvent(f"payload exceeds {MAX_PAYLOAD_BYTES} byte limit")
        with self._transaction():
            cursor = self.connection.execute(
                "INSERT INTO receipts(raw_payload, source_offset, received_at) VALUES (?, ?, ?)",
                (raw, source_offset, datetime.now(UTC).isoformat()),
            )
            receipt_id = cursor.lastrowid
        assert receipt_id is not None
        return receipt_id

    def _apply(self, receipt: sqlite3.Row, after_projection: Callable[[], None] | None) -> str:
        receipt_id = receipt["id"]
        try:
            event = normalize(receipt["raw_payload"], receipt["source_offset"])
        except InvalidEvent as exc:
            outcome, detail = "rejected", str(exc)
        else:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?)", (event.event_hash, receipt_id)
            )
            outcome = "processed" if cursor.rowcount == 1 else "duplicate"
            detail = event.event_hash
            if outcome == "processed":
                for observation in event.observations:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            observation.fingerprint,
                            observation.customer_id,
                            observation.equipment_id,
                            observation.occurred_at,
                            observation.details_json,
                            receipt_id,
                        ),
                    )
        if after_projection is not None:
            after_projection()
        self.connection.execute(
            "UPDATE receipts SET status=?, detail=? WHERE id=?", (outcome, detail, receipt_id)
        )
        self.connection.execute("INSERT INTO audit VALUES (?, ?, ?)", (receipt_id, outcome, detail))
        return outcome

    def work_one(self, after_projection: Callable[[], None] | None = None) -> str | None:
        """Serialize claim, projection, audit and completion in one transaction."""
        with self._transaction():
            receipt = self.connection.execute(
                "SELECT * FROM receipts WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if receipt is None:
                return None
            return self._apply(receipt, after_projection)

    def drain(self) -> int:
        count = 0
        while self.work_one() is not None:
            count += 1
        return count

    def snapshot(self) -> list[dict[str, Any]]:
        # Resolve from all candidates at the greatest event time. Never use receipt
        # order or an arbitrary hash to choose between same-minute candidates.
        rows = self.connection.execute(
            """SELECT o.* FROM observations AS o
               JOIN (SELECT customer_id, equipment_id, MAX(occurred_at) AS latest
                     FROM observations GROUP BY customer_id, equipment_id) AS m
               ON o.customer_id=m.customer_id AND o.equipment_id=m.equipment_id
                  AND o.occurred_at=m.latest
               ORDER BY o.customer_id, o.equipment_id, o.fingerprint"""
        ).fetchall()
        assets: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            key = row["customer_id"], row["equipment_id"]
            if key not in assets:
                assets[key] = {
                    "customer_id": key[0],
                    "equipment_id": key[1],
                    "occurred_at": row["occurred_at"],
                    "status": "current",
                    "candidates": [],
                }
            assets[key]["candidates"].append(row["details_json"])
        for asset in assets.values():
            if len(asset["candidates"]) > 1:
                asset["status"] = "ambiguous"
        return list(assets.values())

    def stats(self) -> dict[str, int]:
        result = {key: 0 for key in ("pending", "processed", "duplicate", "rejected")}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM receipts GROUP BY status"
        ):
            result[row["status"]] = row["n"]
        result["receipts"] = sum(result.values())
        result["observations"] = self.connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0]
        result["ambiguous_assets"] = sum(item["status"] == "ambiguous" for item in self.snapshot())
        return result

    def audit(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection.execute("SELECT * FROM audit ORDER BY receipt_id")
        ]

    def replay(self, after_projection: Callable[[], None] | None = None) -> None:
        """Atomically rebuild derived state; receipt bodies and offsets stay unchanged."""
        with self._transaction():
            self.connection.execute("DELETE FROM audit")
            self.connection.execute("DELETE FROM observations")
            self.connection.execute("DELETE FROM events")
            self.connection.execute("UPDATE receipts SET status='pending', detail=''")
            for receipt in self.connection.execute("SELECT * FROM receipts ORDER BY id").fetchall():
                self._apply(receipt, after_projection)
