from __future__ import annotations

import itertools
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rail_inbox.demo import notification, run_demo
from rail_inbox.model import MAX_PAYLOAD_BYTES, InvalidEvent
from rail_inbox.store import Inbox


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="inbox-test-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "inbox.db"

    def test_receive_is_durable_across_reopen_before_processing(self) -> None:
        with Inbox(self.database) as inbox:
            receipt = inbox.receive(json.dumps(notification()), "+00:00")
            self.assertEqual(inbox.stats()["pending"], 1)
            self.assertEqual(inbox.snapshot(), [])
        with Inbox(self.database) as reopened:
            self.assertEqual(reopened.drain(), 1)
            self.assertEqual(reopened.audit()[0]["receipt_id"], receipt)
            self.assertEqual(reopened.snapshot()[0]["status"], "current")

    def test_file_database_uses_rollback_journal_and_extra_sync(self) -> None:
        with Inbox(self.database) as inbox:
            self.assertEqual(
                inbox.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete"
            )
            self.assertEqual(inbox.connection.execute("PRAGMA synchronous").fetchone()[0], 3)

    def test_duplicate_receipts_are_audited_but_effect_is_not_repeated(self) -> None:
        with Inbox(self.database) as inbox:
            for _ in range(3):
                inbox.receive(json.dumps(notification()), "+00:00")
            self.assertEqual(inbox.drain(), 3)
            self.assertEqual(inbox.stats()["duplicate"], 2)
            self.assertEqual(inbox.stats()["observations"], 1)
            self.assertEqual(len(inbox.audit()), 3)

    def test_missing_offset_is_retained_and_rejected_without_guessing(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification()), None)
            inbox.drain()
            self.assertEqual(inbox.stats()["rejected"], 1)
            self.assertIn("explicit source offset", inbox.audit()[0]["detail"])
            self.assertEqual(inbox.snapshot(), [])

    def test_poison_receipt_does_not_block_next_valid_receipt(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.receive("{", "+00:00")
            inbox.receive(json.dumps(notification()), "+00:00")
            self.assertEqual(inbox.drain(), 2)
            self.assertEqual(inbox.stats()["rejected"], 1)
            self.assertEqual(len(inbox.snapshot()), 1)

    def test_all_120_arrival_orders_agree_with_event_time_oracle(self) -> None:
        events = [
            notification("09:00"),
            notification("11:00"),
            notification("10:00"),
            notification("11:00"),
            notification("11:00", message="conflict"),
        ]
        expected = None
        for permutation in itertools.permutations(events):
            with Inbox(":memory:") as inbox:
                for payload in permutation:
                    inbox.receive(json.dumps(payload), "+00:00")
                inbox.drain()
                snapshot = inbox.snapshot()
                self.assertEqual(snapshot[0]["occurred_at"], "2026-10-01T11:00:00+00:00")
                self.assertEqual(snapshot[0]["status"], "ambiguous")
                self.assertEqual(inbox.stats()["duplicate"], 1)
                if expected is None:
                    expected = snapshot
                self.assertEqual(snapshot, expected)

    def test_newer_unambiguous_event_supersedes_older_ambiguity(self) -> None:
        with Inbox(self.database) as inbox:
            for payload in (
                notification(),
                notification(message="conflict"),
                notification("11:00"),
            ):
                inbox.receive(json.dumps(payload), "+00:00")
            inbox.drain()
            self.assertEqual(inbox.snapshot()[0]["status"], "current")
            self.assertEqual(inbox.stats()["observations"], 3)

    def test_customer_boundary_keeps_same_equipment_separate(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification(customer=900001)), "+00:00")
            inbox.receive(json.dumps(notification(customer=900002)), "+00:00")
            inbox.drain()
            self.assertEqual(len(inbox.snapshot()), 2)
            self.assertTrue(all(row["status"] == "current" for row in inbox.snapshot()))

    def test_overlapping_equipment_lists_do_not_create_false_conflict(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification(equipment=("A", "B"))), "+00:00")
            inbox.receive(json.dumps(notification(equipment=("B", "C"))), "+00:00")
            inbox.drain()
            self.assertEqual(inbox.stats()["observations"], 3)
            self.assertEqual(inbox.stats()["ambiguous_assets"], 0)

    def test_injected_failure_rolls_back_projection_completion_and_audit(self) -> None:
        def fail() -> None:
            raise RuntimeError("injected crash boundary")

        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification()), "+00:00")
            with self.assertRaises(RuntimeError):
                inbox.work_one(after_projection=fail)
            self.assertEqual(inbox.stats()["pending"], 1)
            self.assertEqual(inbox.stats()["observations"], 0)
            self.assertEqual(inbox.audit(), [])
            self.assertEqual(inbox.drain(), 1)

    def test_real_process_exit_before_commit_is_recoverable(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification()), "+00:00")
        code = (
            "import os, sys; from rail_inbox.store import Inbox; "
            "i=Inbox(sys.argv[1]); i.work_one(after_projection=lambda: os._exit(23))"
        )
        result = subprocess.run([sys.executable, "-c", code, str(self.database)], check=False)
        self.assertEqual(result.returncode, 23)
        with Inbox(self.database) as reopened:
            self.assertEqual(reopened.stats()["pending"], 1)
            self.assertEqual(reopened.stats()["observations"], 0)
            self.assertEqual(reopened.drain(), 1)
            self.assertEqual(reopened.stats()["observations"], 1)

    def test_two_workers_do_not_apply_one_receipt_twice(self) -> None:
        with Inbox(self.database) as inbox:
            for index in range(40):
                inbox.receive(json.dumps(notification(equipment=(f"DEMO{index:04}",))), "+00:00")

        def worker() -> int:
            with Inbox(self.database) as inbox:
                return inbox.drain()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(lambda _: worker(), range(2))), 40)
        with Inbox(self.database) as inbox:
            self.assertEqual(inbox.stats()["observations"], 40)
            self.assertEqual(len(inbox.audit()), 40)

    def test_replay_is_atomic_and_does_not_rewrite_receipt_evidence(self) -> None:
        def fail() -> None:
            raise RuntimeError("replay interrupted")

        with Inbox(self.database) as inbox:
            inbox.receive(json.dumps(notification()), "+00:00")
            inbox.receive("{", None)
            inbox.drain()
            snapshot, audit, stats = inbox.snapshot(), inbox.audit(), inbox.stats()
            receipts = inbox.connection.execute(
                "SELECT raw_payload, source_offset, received_at FROM receipts ORDER BY id"
            ).fetchall()
            with self.assertRaises(RuntimeError):
                inbox.replay(after_projection=fail)
            self.assertEqual(
                (inbox.snapshot(), inbox.audit(), inbox.stats()), (snapshot, audit, stats)
            )
            inbox.replay()
            self.assertEqual(
                (inbox.snapshot(), inbox.audit(), inbox.stats()), (snapshot, audit, stats)
            )
            self.assertEqual(
                receipts,
                inbox.connection.execute(
                    "SELECT raw_payload, source_offset, received_at FROM receipts ORDER BY id"
                ).fetchall(),
            )

    def test_oversized_input_is_not_acknowledged(self) -> None:
        with Inbox(self.database) as inbox:
            with self.assertRaises(InvalidEvent):
                inbox.receive("x" * (MAX_PAYLOAD_BYTES + 1), "+00:00")
            self.assertEqual(inbox.stats()["receipts"], 0)

    def test_cli_bad_input_and_end_to_end(self) -> None:
        payload = Path(self.directory.name) / "payload.json"
        payload.write_text(json.dumps(notification()), encoding="utf-8")
        base = [sys.executable, "-m", "rail_inbox", "--db", str(self.database)]
        for command in (
            ["receive", str(payload), "--source-offset=+00:00"],
            ["work"],
            ["snapshot"],
            ["stats"],
            ["audit"],
            ["replay"],
        ):
            result = subprocess.run(base + command, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
        result = subprocess.run(
            base + ["receive", "missing.json"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 2)

    def test_demo_has_checked_baseline_and_replay_result(self) -> None:
        result = run_demo()
        self.assertEqual(result["baseline"]["incorrect_asset_projections"], 3)
        self.assertEqual(result["durable_inbox"]["incorrect_asset_projections"], 0)
        self.assertEqual(result["durable_inbox"]["duplicate"], 1)
        self.assertEqual(result["durable_inbox"]["rejected"], 1)
        self.assertEqual(result["durable_inbox"]["persisted_pending_before_worker_restart"], 1)
        self.assertEqual(result["durable_inbox"]["ambiguous_assets"], 1)
        self.assertTrue(result["durable_inbox"]["replay_matches_snapshot_and_audit"])

    def test_database_write_failure_returns_no_receipt(self) -> None:
        with Inbox(self.database) as inbox:
            inbox.connection.execute(
                "CREATE TRIGGER fail_insert BEFORE INSERT ON receipts "
                "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                inbox.receive(json.dumps(notification()), "+00:00")
            self.assertEqual(inbox.stats()["receipts"], 0)


if __name__ == "__main__":
    unittest.main()
