# Phase 3 Split — Progress Truth (3A) then Portfolio Planning (3B)

**Authority:** plan §17 Phase 3 (Days 9–11), §11 (portfolio risk engine),
§12 (deterministic scheduling), §10.2 (work block), §16.2 (scheduler tests);
architecture §10 (planning package). Phase 2 closed 2026-08-13 (Day 4).

**Chunk 3A status:** **LIVE CLOSED 2026-08-13** — implementation, the §16.2
progress/input matrix, and the controlled-account live steps (production
horizon read, guarded check-in with redelivery convergence, `work_blocks`
index READY) are all recorded. Evidence: `docs/phase3a_progress.md` and
`docs/phase3_evidence/phase3_gate_run.md`.

**Chunk 3B status:** **LIVE PHASE 3 GATE CLOSED 2026-08-13** — the
deterministic portfolio planner, §11.1 risk engine, stable plan diff,
revision-set guarded planner-run/projection publication, real initial-plan
continuation, and safe undo observation are implemented and proven live on
revisions `commitmentos-00020-lgf`/`00021-4l5`: two active commitments, one
published portfolio run with five constraint-safe intervals and no shared
minute allocated twice, five real Calendar events with stable derived IDs,
and replayed tasks converging `no_op` with a byte-identical state digest.
The suite is green at 115 tests after the gate-day effort-reissue fix.
Full record: `docs/phase3b_progress.md` and
`docs/phase3_evidence/phase3_gate_run.md`.

Phase 3 splits on the facts/decisions seam: chunk 3A establishes every input
the planner consumes (verified progress, confirmed effort, busy time,
preferences, candidate slots) with no planning decisions; chunk 3B is the
deterministic planner and everything it publishes. 3A is independently
testable and shippable; 3B consumes 3A's primitives unchanged. The official
Phase 3 gate closes at the end of 3B.

## Chunk 3A — Progress truth and planning inputs

Everything that must be *true and bounded* before any plan is worth computing.

1. **Verified-minute check-in** — implement `RecordWorkCheckIn` (client
   idempotency key, minutes bounded by block duration, actor + timestamp,
   `work_check_in_recorded` observation) and its session+CSRF mutation route.
   Work-block execution-state transitions per the Phase 1 domain model.
2. **Completion invariant + remaining effort** — `domain/progress/service.py`:
   `remaining = max(confirmed_effort − Σ verified_minutes, 0)`; elapsed time
   alone can never alter progress (§11.2); partial verification retains the
   verified portion and replans only the remainder.
3. **Identity-confirmation continuation** (deferred from Phase 2) — approving
   an `identity_confirmation` approval applies the stored proposal (create
   the commitment from its payload); rejection dismisses the span durably.
4. **Calendar busy reads** — `CalendarReader` horizon read of busy intervals
   (transparency-aware, all-day and recurring expansion). Live API read in
   Phase 3; the consistent snapshot store replaces it in Phase 4.
5. **User preferences** — authoritative user facts (working hours, minimum
   session, maximum block, daily focus limit, timezone) with committed
   defaults for the controlled user.
6. **Planning primitives (pure, no I/O)** — `intervals.py` (timezone-aware
   half-open interval algebra), `constraints.py` (§12.1 hard-constraint
   checks), `candidate_slots.py` (15-minute fixed-grid shared slot pool from
   working hours minus busy minus preserved blocks).

**3A exit test evidence (§16.2 rows):** check-in bounded/idempotent/stamped;
elapsed-but-unconfirmed blocks contribute nothing; slot generation
deterministic across timezones and DST transitions; all-day and recurring
busy events excluded; no slot in the past or outside working hours.

## Chunk 3B — Deterministic portfolio planner and publication

Everything that decides, versions, and publishes.

1. **Allocation core** — `scoring.py` (stable documented soft preferences),
   `portfolio.py` (§12.3: preserve valid approved blocks first, sort deficits
   by deadline → priority → creation time → commitment ID, allocate each
   candidate slot at most once across the whole portfolio).
2. **Risk engine** — `risk.py` (§11.1 formulas: allocation deficit,
   shortfall, portfolio slack ratio, threshold table → risk levels; audit
   payload with ordering, inputs, and previous/new risk).
3. **Plan diff + stable identity** — `diff.py` (desired blocks → intended
   mutations); later plan revisions reuse the persisted
   `calendar_event_id` (patch/cancel, never re-derive) — proven by test.
4. **Workflow integration** — the real planner replaces the naive 09:00
   seeded planner in the initial-plan proposal/commit path (Phase 1
   deviation resolved; `first_plan_requires_approval` unchanged); ADK graph
   gains distinct `calculate_portfolio_feasibility` / `produce_stable_plan` /
   `apply_policy` stages (Phase 2 granularity note resolved).
5. **Planner runs + projections** — implement `planner_runs` persistence and
   projection publication guarded by the complete expected-revision set,
   input revision hashes, and planner/constraint/score/threshold versions;
   any changed fact ⇒ no writes, run marked stale, recalculation from
   current facts (§11.1).
6. **Safe undo** — an undo request emits a new reconciliation observation
   that replans from current facts; never blind state reversal.

**3B exit = the Phase 3 gate:** two active commitments produce a
reproducible, constraint-safe portfolio plan with no shared minute allocated
twice; elapsed time alone cannot alter progress. Remaining §16.2 rows:
insufficient capacity, competing commitments receive distinct allocations,
deterministic replay produces the identical plan, existing owned blocks
count once as capacity, minimum block length, daily limits, hard-constraint
violations equal zero. Live closure slice: confirm effort on the real
commitment `daf9a729…` → real planner proposes blocks around actual busy
time → approve → real Calendar events with derived stable IDs.

## Sequencing and estimate

3A ≈ one working session (it reuses the Phase 1 command/route machinery);
3B ≈ one to two sessions (the planner core plus the §16.2 matrix). Both
inside the plan's Days 9–11 window, currently ~5 days ahead of schedule.
