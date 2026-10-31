# Design and walkthrough

## Five-minute review

1. Run `python -m rail_inbox demo`. Compare the explicit oracle with the baseline and durable snapshots in the JSON output.
2. Read `rail_inbox/model.py`: validation, explicit offset conversion, canonical identity and per-equipment observations.
3. Read `rail_inbox/store.py`: `receive` commits raw evidence; `work_one` atomically claims and processes; `snapshot` retains tied candidates.
4. Read `test_real_process_exit_before_commit_is_recoverable`: the child process exits with code 23 after candidate inserts, then a new connection recovers the still-pending receipt.
5. Run `python scripts/record_demo.py --check` to reproduce the committed result. Review the 120-permutation test to see which invariant is independent of arrival order.

## Transaction boundaries

| Boundary | What survives | What the caller may do |
|---|---|---|
| Receipt insertion fails | No committed receipt | Report failure; do not acknowledge |
| Receipt commit succeeds, caller loses response | Stored pending receipt | Retry safely under content-identity assumption |
| Worker fails before commit | Receipt remains pending; partial projection rolls back | Restart worker |
| Worker commit succeeds | Observation, audit and completion agree | Read snapshot or process next receipt |
| Replay fails | Previous derived state restored | Inspect failure; retry after resolving it |

The transaction controls only this database. Sending email, calling another API or publishing messages inside it would not make those effects atomic. A production extension would need an outbox and idempotent downstream consumers.

## Deliberate choices

**SQLite rather than a distributed stack.** It makes the durability boundary inspectable without cloud accounts. Locking is easy to reason about for this bounded single-host workload. A service with sustained ingestion would likely use a database queue or broker, indexed pending work, worker leases, backoff and monitoring. Those additions should follow measured workload and availability requirements.

**Rollback journal rather than WAL.** The local Python runtime bundles SQLite 3.49.1. SQLite documents a rare concurrent-connection [WAL-reset bug](https://sqlite.org/wal.html#walresetbug) affecting that version. This prototype uses rollback journaling with `synchronous=EXTRA`, accepting reader/writer blocking and additional sync work. A future WAL configuration should use a patched SQLite runtime and be tested separately.

**Content identity rather than invented sequence numbers.** There is no assumed event ID or sequence in this adapter. Normalization includes all received header fields except count/list and the source wall-clock representation, replacing the latter with an explicit-offset UTC instant. It then adds the equipment ID. Unknown headers remain significant. Reordered object keys and equipment lists therefore collapse; changed candidate content does not. Hash collisions are assumed computationally negligible, not mathematically impossible.

**Conservative conflict display.** A minute-resolution timestamp cannot establish a true order for two differing same-minute observations. Choosing by receipt time or hash would look deterministic while inventing certainty. The snapshot marks the latest timestamp ambiguous and retains candidates. An unambiguous later observation can supersede it; older conflicting evidence remains in history.

**Reject instead of guessing.** Count mismatches, repeated equipment IDs, duplicate JSON keys, non-finite numbers, malformed dates, missing offsets and reserved normalized fields receive explicit rejection reasons. Unexpected header fields remain visible as candidate content. Only the expected top-level envelope and equipment-entry shape are supported.

**Rebuild instead of mutating evidence.** Replay replaces derived state inside one transaction. Raw payloads, offsets and received times stay fixed in application code. It is not a separate append-only security system: a database owner could modify it. Reprocessing evidence after a code change can change outcomes; the exact-match demo checks determinism for the same version and receipts.

## Likely interview questions

- **What is guaranteed after returning a receipt?** The SQLite commit succeeded under the configured journal/sync settings. Correct storage and filesystem behavior are prerequisites; the whole machine or disk can still fail.
- **Does this provide exactly-once delivery?** No. It records every receipt and makes local candidate insertion idempotent under a stated content-identity assumption. It does not control upstream delivery or outside side effects.
- **What if the latest timestamp is wrong?** A far-future but valid event can dominate the snapshot. A real integration needs provider clock semantics and a quarantine policy; this prototype does not invent a lateness threshold.
- **Why require an offset per receipt?** The adapter must avoid silently assuming a timezone. An onboarding contract needs to define how source wall-clock timestamps and daylight-saving transitions map to instants.
- **How do two workers avoid racing?** Both claim and apply under `BEGIN IMMEDIATE`; SQLite allows one writer. This sacrifices write throughput for a simple correctness boundary.
- **Why retain rejected payloads?** A parser can improve without losing the evidence needed to reproduce a failure. Real data would require access controls, sensitive-data handling, retention and audited remediation.
- **How would this scale?** Measure arrival rate, burst size and recovery-time requirements. Then add bounded batch processing, a pending-work index, an incremental projection, monitoring, backups and a broker/database appropriate to the target availability needs.
- **What would you confirm before BNSF integration?** Approved onboarding, authenticated HTTPS ingress and certificate validation, the actual versioned payload contract, event identity, delivery/retry behavior, timestamp semantics, customer authorization, data handling and operational ownership. No live integration was attempted here.

## Public motivation and references

- [BNSF API Center: Push Notifications](https://www.bnsf.com/ship-with-bnsf/support-services/customer-api/push-notifications/) — public motivation and example shape, reviewed October 31, 2026. Synthetic values replace the published example values. The implementation and test failure modes are independent design choices, not evidence about BNSF's private systems.
- [Python SQLite documentation](https://docs.python.org/3/library/sqlite3.html) — connection and transaction APIs.
- [SQLite WAL](https://sqlite.org/wal.html) and [synchronous pragma](https://sqlite.org/pragma.html#pragma_synchronous) — local storage tradeoffs.
