"""Record or verify the deterministic fixture and baseline comparison."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rail_inbox.demo import fixture, notification, run_demo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    values = {
        ROOT / "fixtures" / "geofence.json": notification(),
        ROOT / "fixtures" / "scenario.json": fixture(),
        ROOT / "results" / "demo.json": run_demo(),
    }
    for path, value in values.items():
        text = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if args.check:
            if path.read_text(encoding="utf-8") != text:
                print(f"stale output: {path.relative_to(ROOT)}", file=sys.stderr)
                return 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    print("Fixture and demo output match." if args.check else "Recorded fixture and demo output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
