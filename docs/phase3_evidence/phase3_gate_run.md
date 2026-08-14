# Phase 3 Gate — Controlled-Account Live Closure Record

**Date:** 2026-08-13 (Day 4; Phase 3 scheduled Days 9–11)
**Driver:** `scripts/run_phase3_gate.py` (per-step subcommands) plus
`scripts/run_seeded_slice.py` for the second commitment.
**Revisions:** `commitmentos-00020-lgf` (3A+3B deploy),
`commitmentos-00021-4l5` (effort-reissue fix, below).
**Index:** `work_blocks (commitment_id ASC, scheduled_start ASC)` READY in
Firestore before the run (all nine composite indexes READY).

All identifiers below are sanitized prefixes; no message bodies, addresses,
tokens, or event summaries from the controlled account appear in this record.

## Route trust contracts (revision 00020, re-probed on 00021)

`/health/live` 200; unauthenticated POSTs to
`/internal/tasks/reconcile-observation`, `/api/v1/approvals/{id}/resolve`,
`/api/v1/work-blocks/{id}/check-in`, `/api/v1/plans/{id}/undo`, and an
unauthenticated `GET /api/v1/commitments` all rejected **401 before body
validation** (the §16.3 ordering contract holds on the two new 3A/3B routes).

## 3A — busy fixtures and the production horizon read

Four unrelated (non-CommitmentOS) events created on the controlled calendar:
a timed opaque hold (Thu 13:00–14:00 PT), a daily recurring opaque hold
(11:00–11:30 PT × 3 from Fri), an opaque all-day hold (Sun), and a
**transparent** timed hold that must not count. Event IDs:
`gk6at9jhu2…`, `bu8eok1qip…`, `i9rj9v6g8a…`, `7h3ubs4e38…`.

`GoogleCalendarReader.list_busy_intervals` (production adapter, real API)
returned exactly five normalized busy intervals: the timed hold, three
expanded recurring instances, and the all-day hold as
Sun 00:00 → Mon 00:00 local (exclusive end-date semantics). The transparent
event was correctly absent. No event body is persisted or logged by this
read path; the deployed twin of this read is proven by the planner runs
below (their `calendar_snapshot_hash` and busy-avoiding placements).

## Live finding and fix: effort-approval reissue gap (golden audit step 3)

Resolving the pending `effort_confirmation` (`e81e325e…`, created at
commitment revision 1) against real commitment `daf9a729…` (revision 2 after
the M3 deadline change) correctly returned `approval_superseded` — the
stale-approval guard live — but nothing reissued the request: the reissue
required by golden audit step 3 ("superseded and reissued against the new
commitment revision") only existed for ownership upgrades. The commitment
was stuck awaiting confirmation with no pending approval and no code path to
create one; the competition demo's M3 sequence would have hit the same wall.

**Fix (revision 00021):** `_ensure_effort_confirmation_requested` in
`phase1_workflow.py` — a `my_commitment` in `awaiting_confirmation` with
unconfirmed effort must hold exactly one pending effort approval at the
current commitment revision; stale pending requests are superseded with an
audit event and the current-revision request is (re)issued. Runs on every
update-path exit including pure restatements. Two regression tests pin the
M3 supersede-and-reissue contract and the stuck-state repair (115 tests
green, ruff clean).

**Live repair through real thread activity:** a restatement reply sent from
the controlled account in the real thread ran the full autonomous pipeline
(watch → Pub/Sub → bounded generation → Gemini → fingerprint convergence →
no-change restatement path) and the invariant helper reissued approval
`15f40ba0…` (pending, commitment revision 2) within ~15 s of delivery. No
manual data repair was performed.

## 3B — effort, portfolio planning, approval, real events

- **Effort confirmed via guarded route** (session cookie + session-bound
  CSRF): approval `15f40ba0…` → 200 `completed`, continuation observation
  dispatched; commitment `daf9a729…` → revision 3, confirmed effort 180.
- **Planner run 1** `42babb59…` (published, deployed planner): three
  60-minute intervals Thu 09:00, 10:15, 11:30 PT — future, inside working
  hours, before the 16:00 PT deadline, clear of the 13:00 busy hold,
  back-to-back-avoidance gaps visible, exactly 180 min against the 180-min
  daily focus limit. Run document carries `allocations`,
  `calendar_snapshot_hash`, `calendar_state_revision`, `commitment_order`,
  `expected_revisions`, `feasible`, `risk_audit`, `unallocated_intervals`,
  and constraint/planner/score/threshold versions. 7/7 checks.
- **Plan approved via guarded route** (`3446b508…` → 200 `completed`) →
  outbox → deployed executor → **three real Calendar events**, each with the
  stable derived event ID recorded on its work block and CommitmentOS
  ownership properties (`3540e341…`→`u0ikspkf…`, `45d937ff…`→`c1jfkqgo…`,
  `b1518be4…`→`hvus9e8a…`). 7/7 checks.
- **Second commitment** (`4d64796b…`, seeded through the production
  observation contract at the deployed service; run tag `20260813t143211`):
  effort 120 confirmed, initial plan approved, two real events with stable
  IDs, action results processed — 12/12 checkpoints.
- **Portfolio planner run 2** `58fd5731…` (published): **both commitments**
  referenced with the full expected-revision set; five intervals total —
  daf9a729's three blocks preserved at their exact times (counted once as
  capacity), the second commitment allocated Fri 09:00 and Sat 09:00 PT
  (balanced daily load), avoiding the recurring holds and the Sunday
  all-day block; **no shared minute allocated twice**; daily totals
  180/60/60 within the limit. 12/12 checks.

## 3A — guarded check-in and convergence

Block `3540e341…` transitioned `planned → awaiting_check_in` by the driver
standing in for the Phase 4 elapse scan (production domain transition +
repository save under the real unit of work; the scan itself is Phase 4
scope by design at the time of this historical run; Phase 4C now subsumes
this stand-in with periodic, deterministic safety observations). Then via the guarded route: check-in of 60 verified
minutes → 200 `completed` (evidence `1ce72787…`, block revision 2→3);
**identical redelivery** → 200 `no_op` / `check_in_already_recorded`;
exactly one evidence record for the idempotency key; exactly one revision
advance. 4/4 checks.

## Replay proofs

State digest = SHA-256 over commitments, work blocks, approvals, planner
runs, evidence, observations, outbox (revision/status projections) **plus
live Calendar event etags**.

- Eight most recent processed observations redelivered to
  `/internal/tasks/reconcile-observation` with an impersonated tasks-SA
  OIDC token → `200 no_op` × 8; digest identical before and after. 9/9.
- All five succeeded calendar actions redelivered to
  `/internal/tasks/execute-calendar-action` → `200 no_op` × 5; digest
  identical — zero duplicate events, zero external mutations. 6/6.

## Log scan

Cloud Logging over the gate window (192 entries, 12:00Z onward): zero
account addresses, bearer tokens, refresh tokens, cookies, session values,
or message-body content (including the restatement email's body).

## Annotated stand-ins (recorded, not gaps in the proof)

1. **Session issuance**: the controlled web session was created directly in
   Firestore (SHA-256-hashed token, same shape the login flow writes) in
   place of the browser OAuth login; every guarded mutation then traveled
   through the deployed HTTPS routes with the real cookie + CSRF checks.
   The session was revoked after the run.
2. **Elapse transition**: `planned → awaiting_check_in` applied by the
   driver through the production domain model. This was the explicit Phase 3
   stand-in; Phase 4C's periodic safety reconciliation now owns the transition
   and raises the durable `work_check_in_required` state.
3. **Second commitment provenance**: seeded through the production
   observation contract (per the golden scenario's seeded portfolio
   context), not a second live Gmail thread; every downstream step ran on
   the deployed service.

## Verdict

All eight live-closure steps in `docs/phase3b_progress.md` and all four 3A
steps in `docs/phase3a_progress.md` are recorded. Plan §17 Phase 3 gate —
"Two active commitments produce a reproducible, constraint-safe portfolio
plan with no shared minute allocated twice; elapsed time alone cannot alter
progress" — **passed live**. The elapsed-time invariant is pinned by the
§16.2 suite and held live: the unconfirmed blocks contributed nothing;
only the explicit 60-minute check-in changed verified progress.
