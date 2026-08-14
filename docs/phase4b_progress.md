# Phase 4B Progress — Repair Decisions

**Status:** **LIVE EXIT CLOSED** on 2026-08-14.

## Stable repair objective

- `StablePlanRepairer` implements the §12.4 ordering as a pure operation.
  Completed/active blocks are immutable, valid manual moves are fixed before
  optimization, and every unaffected future block remains semantically
  unchanged across the whole portfolio.
- A bounded exact assignment search accepts only hard-constraint-safe full
  assignments, then minimizes moved-block count, then total start-time
  displacement. Stable timestamps and IDs are tie-breakers only. If no full
  assignment exists, the result is explicitly infeasible and escalates.
- Repair audit rows include the objective version, affected/immutable/adopted
  IDs, moved count, displacement, unplaced IDs, and the unaffected-preservation
  oracle.

## Risk, decisions, and policy

- Every repair planner run persists the §11.1 risk arc per commitment:
  detected allocation deficit, shortfall before/after repair, and risk
  before/after repair. `RISK_CHANGED` activities copy this stored arc for the
  dashboard and demo explanation.
- Valid Calendar moves are adopted as authoritative work-block facts and bump
  plan revision without writing a Calendar action. Invalid moves preserve the
  approved interval and create one structured choice. User deletions mark
  `user_deleted` and create one choice among rescheduling unfinished work,
  recording completion through the check-in invariant, or pausing; no outbox
  record exists before that choice.
- `policy_thresholds_v1` is frozen in a pure evaluator. More than two moved or
  canceled blocks, a shift over 24 hours, placement outside preferred focus
  periods, or daily-focus exceedance creates an `action_approval` containing
  the full plan diff and risk arc, with zero Calendar mutation. In-policy
  repairs publish automatically with an activity notification and undo
  provenance.

## Snapshot etags and stale-precondition continuation

- Repair patch/cancel intents obtain `expected_observed_event_etag` only from
  the published snapshot store. Executor preflight now rejects a missing
  snapshot as well as an etag mismatch; provider reads cannot silently replace
  intent provenance.
- A provider 412 terminally marks the old intent, writes one coalescible sync
  request, and immediately attempts named source-sync dispatch. It emits no
  `action_result` and never retries with a fetched etag.
- Once that generation publishes, the typed Calendar observation compares
  desired facts with the new snapshot. If the old intent is still required,
  reconciliation writes a new plan revision and outbox intent carrying the new
  snapshot etag. This is resume-from-synchronized-truth, not retry.
- Tombstone echo suppression was tightened: only a succeeded cancel can
  suppress an owned tombstone. An old insert/patch etag can never hide a user
  deletion.

## Local evidence

`backend/tests/unit/test_phase4b_repair.py` and
`backend/tests/integration/test_phase4b_repair_decisions.py` prove:

1. one conflict moves exactly one stable block by minimum displacement;
2. unaffected blocks are preserved and the stored risk arc closes;
3. three moved blocks cross `policy_thresholds_v1` into `action_approval`
   with no outbox mutation;
4. a valid manual move is adopted with no Calendar write;
5. invalid moves and user deletions each create one structured decision and
   no silent action;
6. repair patch intent carries the snapshot etag; and
7. forced 412 performs stale → dispatched sync → snapshot publication → typed
   reconciliation → new-etag resume → successful patch.

Checklist D3 rows 7–8 and the two deferred D1 rows are closed locally and
live.

## Live exit — closed 2026-08-14

The final controlled-account run passed **65/65** combined checkpoints on
`commitmentos-00050-qar`:

1. A real owned work block was moved to a different constraint-safe slot. The
   published observation was typed and processed as a valid user move,
   `user_move_adopted` supplied exactly one explanation, plan revision
   advanced, and the Calendar outbox delta was exactly zero.
2. A separately armed restore intent carried only the published snapshot etag.
   Provider metadata then changed behind the paused source cursor, and the
   deployed executor received a real Google HTTP 412. Intent
   `9ecb37e3d549…` became terminal `stale_precondition`, emitted no
   `action_result`, and committed one coalesced Calendar sync request.
3. After independent source synchronization, intent `de7166055ad7…` preserved
   the desired interval, carried the new synchronized etag (old/new hashes
   `46ad8ed8d9f4c0a1` / `71bd78b5a1776175`), executed successfully, and landed
   provider truth at the approved interval.

The live gate exposed and closed two real decision-path defects:

- the guarded approval API schema/router did not forward the domain-required
  Calendar decision `choice`;
- `restore_approved_slot` fell through generic replanning and wrote no patch.

The fix forwards `choice`, then converts an explicit restore decision into one
fenced conditional PATCH while advancing revisions and rebuilding projection
provenance. Route and workflow regressions cover both defects. The isolated
seeded fixture was created through deployed observation → approvals → outbox →
executor, then its owned event and fixture documents were removed by exact run
tag. Full evidence: `docs/phase4_evidence/phase4ab_gate_run.md`.
