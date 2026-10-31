"""Local CLI; no listener, credentials, network calls or operational commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from rail_inbox.demo import run_demo
from rail_inbox.model import MAX_PAYLOAD_BYTES, InvalidEvent
from rail_inbox.store import Inbox


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("inbox.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive", help="durably store one local payload")
    receive.add_argument("file", type=Path)
    receive.add_argument("--source-offset", help="known event UTC offset, e.g. +00:00")
    for name in ("work", "snapshot", "stats", "audit", "replay", "demo"):
        commands.add_parser(name)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            output: object = run_demo()
        else:
            with Inbox(args.db) as inbox:
                if args.command == "receive":
                    with args.file.open("rb") as stream:
                        raw = stream.read(MAX_PAYLOAD_BYTES + 1)
                    if len(raw) > MAX_PAYLOAD_BYTES:
                        raise InvalidEvent(f"payload exceeds {MAX_PAYLOAD_BYTES} byte limit")
                    output = {
                        "committed_receipt": inbox.receive(raw.decode("utf-8"), args.source_offset)
                    }
                elif args.command == "work":
                    output = {"processed_receipts": inbox.drain(), "stats": inbox.stats()}
                elif args.command == "snapshot":
                    output = inbox.snapshot()
                elif args.command == "stats":
                    output = inbox.stats()
                elif args.command == "audit":
                    output = inbox.audit()
                else:
                    inbox.replay()
                    output = {"replayed": True, "stats": inbox.stats()}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except (OSError, UnicodeError, InvalidEvent, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
