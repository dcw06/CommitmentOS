# Phase 3A Progress — Progress Truth and Planning Inputs

**Started/completed locally:** 2026-08-13  
**Authority:** `docs/phase3_plan.md` Chunk 3A; build plan §4.4–4.5, §11.2,
§12.1, §16.2; architecture §10.  
**Status:** **LIVE CLOSED 2026-08-13.** The production implementation is
wired; the suite is green (115 tests after the gate-day reissue fix), Ruff
clean, targeted mypy clean. The live steps are recorded in
`docs/phase3_evidence/phase3_gate_run.md`: `work_blocks` index READY,
revision `commitmentos-00020-lgf`/`00021-4l5` deployed, the production
horizon read normalized a timed hold, three expanded recurring instances,
and an exclusive-end all-day hold while excluding a transparent event (no
bodies persisted or logged), and the guarded check-in route recorded one
bounded check-in with an identical redelivery converging `no_op`
(one evidence record, one revision advance).

## Implemented

### Verified progress and the completion invariant

- `RecordWorkCheckIn` commits one work-block revision, immutable
  `work_check_in` evidence record, actor/timestamps, deterministic activity,
  and `work_check_in_recorded` observation in one transaction. Dispatch occurs
  after commit and remains repairable through the existing observation
  dispatcher.
- `POST /api/v1/work-blocks/{work_block_id}/check-in` is protected by the
  controlled session and session-bound CSRF dependencies before body
  validation. The request carries a bounded client idempotency key and
  expected work-block revision.
- Retries with the same key and payload converge without a second revision,
  evidence record, observation, or task. Reusing a key for different facts is
  rejected. Verified minutes are bounded by block duration.
- A partial check-in retains exactly the reported verified minutes; a missed
  or elapsed block contributes nothing else. `ProgressCalculator` derives
  active remaining work as `max(confirmed effort - verified minutes, 0)`.
- Explicitly completed commitments have zero schedulable demand while their
  stored verified-minute total remains unchanged. Completion therefore stays
  terminal without fabricating work.

### Phase 2 identity-confirmation continuation

- Identity approvals now retain the complete safe structured proposal and its
  source-observation reference, rather than only the dashboard summary.
- Approval applies the stored proposal through a new bounded reconciliation
  continuation. Ambiguous ownership requires a confirmed ownership value;
  actionable commitments enter the normal effort-confirmation path.
- Rejection writes a deterministic `source_span_dismissal` fact. The identity
  resolver consults those facts before confidence/ambiguity routing, so an
  unchanged rejected span cannot reopen another confirmation after later
  thread activity.

### Calendar and authoritative user inputs

- `GoogleCalendarReader.list_busy_intervals` performs a bounded, paginated
  `events.list` horizon read with `singleEvents=true`, expanding recurring
  instances. Canceled and transparent events do not consume capacity.
- Timed events remain timezone-aware; all-day dates use the controlled user's
  IANA timezone and Google's exclusive end-date semantics. Results are clipped
  to the planning horizon and carry stable read identities and app-ownership
  classification. No Calendar body is persisted by this read path.
- `PlanningInputReader` commits the frozen `golden_scenario_rev_1` defaults as
  authoritative user facts on first use: 09:00–17:30 local working hours,
  30-minute minimum, 60-minute maximum, 180-minute daily focus limit, and the
  working window as the P0 preferred focus window. Later reads reuse the
  committed document.
- User-horizon work-block reads are implemented; the required
  `(commitment_id, scheduled_start)` composite index is checked in.

### Pure planning primitives — facts only

- `IntervalSet`: timezone-aware half-open normalization, union, subtraction,
  intersection, containment, and total elapsed minutes. DST duration uses UTC
  instants, not naive wall-clock subtraction.
- `ConstraintEvaluator`: past-time, deadline, working-hours, minimum/maximum
  block, overlap, and daily-focus-limit violations with stable codes.
- `CandidateSlotGenerator`: deterministic future-only alternatives on a
  15-minute UTC instant grid, clipped by free intervals and deadline, with
  block durations between the configured minimum and maximum. Every candidate
  has a neutral score of zero: 3A makes no preference or allocation decision.

At this 3A checkpoint, `scoring.py`, `portfolio.py`, `risk.py`, `diff.py`,
planner-run publication, projections, and outbox planning were deliberately
left to Chunk 3B. They were subsequently implemented on 2026-08-13; see
`docs/phase3b_progress.md`. Minimal-change repair remains the Phase 4 gate.

## Test evidence

`backend/tests/integration/test_phase3a_progress_inputs.py` and the extended
Phase 2/route contract suites prove:

- elapsed but unconfirmed work contributes zero minutes;
- partial verification changes only the remaining amount;
- manual completion closes demand without inventing verified minutes;
- atomic, bounded, idempotent check-ins and conflicting-key rejection;
- session and CSRF checks run before request-body validation;
- identity approval creates the stored proposal and rejection suppresses the
  source span across later thread activity;
- DST-safe elapsed duration and interval subtraction;
- deterministic, grid-aligned, future-only candidates before the deadline;
- hard-constraint and daily-limit classification;
- authoritative defaults commit exactly once;
- paginated recurring-event expansion, all-day blocking, and transparent-event
  exclusion in the production Calendar adapter.

Local verification:

```text
100 passed, 20 warnings
ruff: All checks passed
targeted mypy: Success, no issues found in 17 source files
```

The warnings are existing Starlette/httpx and Google ADK/GenAI deprecations;
no new test warning is introduced by 3A.

## Live closure steps

1. Deploy the new service revision and the `work_blocks` composite index.
2. Run one controlled-account horizon read containing a normal timed event, an
   all-day event, and a recurring instance; verify the normalized intervals
   and that no event body is written to Firestore or logs.
3. Exercise the guarded check-in route on a real app-owned block, redeliver the
   identical request, and confirm one revision/evidence/observation only.
4. Record the revision, sanitized trace, and index READY state here. Chunk 3A
   can then be marked live-closed; Chunk 3B remains the Phase 3 gate.
