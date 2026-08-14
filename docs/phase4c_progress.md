# Phase 4C Progress — Always-On Safety and Gate Operations

**Status:** **PHASE 4 GATE CLOSED (live) 2026-08-14.** The controlled-account
meeting-over-block run passed 17/17 verify checkpoints on revision
`commitmentos-00031-rsz` with 8.293 s warmed insert-to-repair latency; full
record in `docs/phase4_evidence/phase4_gate_run.md`. See "Gate closure
record" below.

## Gate closure record (2026-08-14)

Plan §17 Phase 4 gate — "A newly inserted meeting automatically causes
exactly one minimal repair with a complete before/after explanation" —
**passed live**:

- **Run 1 exposed a live-only policy bug** (the gate doing its job): the
  pipeline ran end to end in ~2 s, but the in-policy one-block repair was
  escalated to `action_approval` with `policy_reason = repair_infeasible`
  because unrelated overdue commitment `daf9a729…` makes every portfolio
  plan infeasible (`feasible = all(shortfall == 0)` portfolio-wide). One
  overdue commitment permanently disabled automatic repair — and the
  approval-resume path re-checked the same flag, so even approval could not
  execute it.
- **Fix (regression-tested, 148 green):**
  `_repair_blocking_infeasibility` now escalates only when the repair
  itself failed — unplaced affected blocks, an immutable-block conflict, or
  shortfall on a commitment whose deadline is still in the future. Overdue
  shortfall stays visible as `overdue` risk and portfolio
  `no_feasible_plan` but no longer blocks unrelated repair; the `_repair`
  audit row now records `immutable_conflict` explicitly
  (`test_unrelated_overdue_commitment_does_not_block_automatic_repair`).
- **Run 2 (fixed revision): 17/17.** Conflict inserted 06:40:43Z → patch
  landed via `If-Match` 06:40:51Z (**8.293 s**, budget 15 s) → echo
  terminally ignored 06:40:58Z. One block moved 60 minutes with its stable
  event ID; 4/4 unaffected blocks byte-preserved; complete
  before/after/risk-arc explanation (`4d64796b…` critical→on_track,
  `daf9a729…` honestly unchanged at 120-minute overdue shortfall); the
  conflict meeting and every unrelated event untouched.
- **Log scan clean:** 60 entries, only 200/204, zero sensitive-data hits.
- **Deviation recorded:** conflict insertion/removal performed through the
  Calendar API with the controlled credential at the owner's request
  (guarded companion scripts); the demo video will use the Calendar UI.
- Pre-fix artifact: pending `action_approval` `19b1acfb…` retained as audit
  history (expires 2026-08-21). Warmth restored to scale-to-zero after the
  run.

## Periodic safety reconciliation

- `RunMaintenance.safety_reconciliation` now scans active commitments under a
  bounded budget and emits deterministic `periodic_safety` observations. The
  scheduler never fabricates progress or applies planning decisions.
- Fenced reconciliation performs real work-block transitions at scheduled
  boundaries: `planned → active → awaiting_check_in`. The last state is the
  durable check-in request; it records `work_check_in_required` while leaving
  `verified_minutes` unchanged until the guarded check-in route records user
  evidence. Active commitments advance to `in_progress` without implying
  completion.
- The same pass refreshes projections with mismatched provenance, surfaces
  passed deadlines as `overdue`, marks abandoned calculated planner runs
  `stale`, and re-dispatches generations with no checkpoint activity for five
  minutes.
- Desired/snapshot drift is detected only outside the Calendar publication
  barrier. It creates a coalesced Calendar sync request and a visible safety
  fact; §9.5 classification remains owned by authoritative Calendar snapshot
  publication, so the safety job cannot misclassify a user edit or silently
  mutate Calendar.
- `scripts/create_scheduler_jobs.sh` now installs the fourth job every minute.
  This machinery subsumes the Phase 3 gate's historical `elapse-block`
  stand-in.

## Visible failure truth

The controlled-session dashboard now exposes:

- `GET /api/v1/dashboard/today`
- `GET /api/v1/dashboard/activity`
- `GET /api/v1/dashboard/system-status`

The Today response carries the same visible failure set as system status, so
an `on_track` projection cannot hide reauthorization, full-resync, delayed or
in-progress source sync, paused controls, held/in-flight/failed/stale Calendar
actions, pending user block decisions, stale projections, snapshot drift,
unverified elapsed blocks, infeasible plans, or overdue commitments. The
known `daf9a729…` Phase 3 commitment is expected to enter the `overdue` state
on the first deployed safety pass; that is evidence, not a gate regression.

## Explanation, echo, and latency

- Every automatic repair now writes one `plan_repaired` activity containing
  the typed cause, exact before/after mutation documents, unchanged block IDs,
  displacement, full §11.1 risk arc, outbox IDs, undo availability, and
  detection-to-decision latency.
- Calendar terminal activity links back to the disruption observation and
  records detection-to-land latency plus the warmed 15-second budget result.
- The real-watch integration path is proven locally: authenticated webhook →
  Calendar generation/publication → disruption → one minimal patch → second
  authenticated webhook → typed ignored app echo. The second watch produces
  zero outbox records and zero Calendar mutations.
- `scripts/configure_demo_warmth.sh warm|normal` makes the one-warm-instance
  demo setting explicit and reversible. Scale-to-zero remains the normal
  state.

## Local evidence

`backend/tests/integration/test_phase4c_always_on_safety.py` proves:

1. bounded lifecycle transitions and replay convergence with zero invented
   minutes;
2. overdue projection refresh and visibility in both dashboard reads;
3. stale planner cleanup;
4. snapshot drift detection plus coalesced resynchronization;
5. visible `reauth_required` and `full_resync_required`; and
6. one repair under the real webhook path, one suppressed echo, one complete
   explanation, and a warmed latency result below 15 seconds.

Repository verification: Ruff clean, targeted mypy clean, shell scripts parse,
and **147 tests passing**.

## Live deployment evidence — 2026-08-14

- Cloud Run revision `commitmentos-00036-puj` serves 100% of production
  traffic and `/health/live` returns `200`/`live`.
- All 15 required Firestore composite indexes are `READY`.
- `commitmentos-safety-reconciliation` is enabled every minute with the
  production Scheduler service account, URI, and audience. Normal runs return
  `200`.
- The first live run exposed and closed four Phase 0/Calendar-bootstrap
  compatibility gaps with regression tests: a timestamp-less legacy Calendar
  cursor is treated as stale; a watch handshake bootstraps an unpublished
  snapshot; a legacy token without a published generation forces full resync;
  and synchronization reads omit `orderBy` so Google returns the final
  `nextSyncToken`.
- The renewed real Calendar watch produced a verified `204` handshake. Its
  fenced full-resync generation and continuation both returned `200`, then 12
  released reconciliation requests returned `200`. Source-sync and
  reconciliation queues converged empty, with no final-revision `5xx` after
  publication.
- Masked durable verification recorded Calendar cursor revision `1`, Calendar
  state revision `1`, a non-empty published generation, no publication
  barrier, and no full-resync flag. Two elapsed blocks are now
  `awaiting_check_in` with `verified_minutes=0`.
- Commitment `daf9a729…` is durably `in_progress` and `overdue`, with 120
  remaining minutes against its passed `2026-08-13T23:00:00Z` deadline.
- No manual conflict meeting was inserted and demo warmth was not enabled, so
  the official centerpiece remains an explicit owner-observed action.

## Live exit — completed 2026-08-14

All four runbook steps were executed (warm → arm → conflict insertion →
verify 17/17 → evidence + log scan → scale-to-zero restored); the recorded
deviation is that conflict insertion used the Calendar API companion script
rather than the Calendar UI, at the owner's request. The official Phase 4
gate is **closed**: "A newly inserted meeting automatically causes exactly
one minimal repair with a complete before/after explanation" — proven live
with the full record in `docs/phase4_evidence/phase4_gate_run.md`.
