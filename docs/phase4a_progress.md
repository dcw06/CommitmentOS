# Phase 4A Progress — Calendar Truth

**Status:** **LIVE EXIT CLOSED** on 2026-08-14.

## What is now authoritative

- Calendar uses the same fenced, bounded generation protocol as Gmail: one
  provider page per named task, deterministic staged items, XOR manifests,
  apply checkpoints, an unpromoted candidate sync token, and one final
  publication transaction.
- `CalendarSnapshotReducer` now normalizes timed and all-day events (including
  DST boundaries), transparency, recurrence identity, ownership provenance,
  cancellations, tombstones, busy intervals, and stable snapshot hashes.
- Applying a Calendar generation activates the cursor publication barrier.
  Planner snapshot reads and Calendar executor preflight are ineligible until
  publication clears it. A publication advances `calendar_state_revision`
  exactly once; replay cannot advance it again.
- HTTP 410 abandons the expired incremental generation, marks the cursor for
  replacement, and enqueues a new full-resync request. Full replacement stages
  missing-event tombstones through the same manifest/apply path and cannot
  publish until tombstoning is complete.
- Snapshot diffs emit typed environmental-disruption, valid/invalid user-move,
  user-deletion, and application-echo observations. User moves run the shared
  hard-constraint evaluator. Completed-outbox echoes match response etag or
  payload hash and are persisted as `ignored`, so they start no repair loop.
- `PortfolioPlanningService` now reads the published snapshot store and carries
  its real Calendar revision/hash in the full expected-revision set. The Phase
  3B provider reread/hash bridge has been removed.

## Ingress and maintenance replacement

- The Phase 0 Calendar fetch bridge and webhook route are no longer mounted.
  The public route verifies the durable channel/resource mapping and the
  SHA-256 channel-token hash in constant time, enforces a durable per-channel
  rate limit, coalesces a sync request, and dispatches the typed source-sync
  task.
- Calendar channel renewal now runs alongside Gmail renewal, preserves
  previous-channel overlap metadata, stores only the token hash, and stops the
  replaced channel after the new registration is durable.

## Local evidence

`backend/tests/integration/test_phase4a_calendar_truth.py` proves:

1. all-day DST normalization and exclusive end-date semantics;
2. two-page token non-promotion and single final publication;
3. snapshot persistence, typed disruption, revision-once replay behavior;
4. manifest-covered full-resync tombstones;
5. 410 abandonment and full-resync handoff;
6. verified/replayed webhook coalescing and invalid-token zero-side-effects;
7. renewal overlap metadata;
8. planner and executor barrier refusal with zero Calendar I/O;
9. completed-outbox echo suppression; and
10. valid/invalid manual-move and user-deletion typing.

The full repository suite is green. D3 rows 1–6 are closed locally. Rows 7–8
are now closed locally by Phase 4B; the controlled-account
two-page/barrier/snapshot planner run remains the live 4A exit.

## Live exit — closed 2026-08-14

The controlled-account driver passed every Phase 4A checkpoint inside the
final **65/65** Phase 4A+4B run on gate revision
`commitmentos-00050-qar`:

1. Eleven uniquely tagged transparent changes with gate page size 10 produced
   exactly two provider pages. Page 1 staged 10 items, retained a continuation
   token, left the candidate token unpromoted, and left the published cursor
   byte-identical.
2. The real Firestore publication barrier became externally observable. The
   deployed executor returned `calendar_truth_ineligible`; the deployed
   planner returned `workflow_exception`; neither published a run or performed
   Calendar I/O.
3. Page 2 published all 11 staged items with equal staged/applied manifests.
   Cursor revision and `calendar_state_revision` each advanced exactly once
   (`62 → 63`), the candidate token promoted, and the barrier cleared.
4. The post-publication planner succeeded exactly once. Planner run
   `30ad195d7b22…` carried Calendar revision `63` and snapshot hash
   `c3e2259cc129…`, byte-identical to a fresh reduction of the durable snapshot
   store.
5. The temporary barrier-observation fixture and transparent Calendar events
   were uniquely tagged; Calendar fixtures were removed during guarded
   cleanup. The default-off barrier probe delay was restored to `0` after the
   proof.

Production now serves `commitmentos-00052-did` with page size `250`, apply
chunk `100`, all three queues `RUNNING`, an eligible Calendar cursor, and no
publication barrier. Full evidence:
`docs/phase4_evidence/phase4ab_gate_run.md`.
