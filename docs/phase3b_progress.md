# Phase 3B Progress — Deterministic Portfolio Planning and Publication

**Status:** **LIVE PHASE 3 GATE CLOSED 2026-08-13** (local exit closed the
same day). All eight controlled-account live-closure steps below are
recorded in `docs/phase3_evidence/phase3_gate_run.md`: revisions
`commitmentos-00020-lgf`/`00021-4l5`, planner runs `42babb59…` (single
commitment) and `58fd5731…` (both commitments, five constraint-safe
intervals, no shared minute allocated twice), five real Calendar events with
stable derived IDs, guarded-route effort/plan/check-in mutations, replayed
observation and action tasks converging `no_op` with a byte-identical state
digest, and a clean log scan. One live-only finding was fixed during the
gate (effort-approval reissue on commitment revision — golden audit step 3;
regression-tested, deployed in `00021-4l5`).

**Authority:** build plan §11.1, §12, §13.1, §16.2, and §17 Phase 3; local
implementation evidence is `backend/tests/integration/test_phase3b_portfolio_planner.py`.

The local gate now produces one deterministic, constraint-safe allocation
across every active commitment. It does not allocate the same Calendar minute
twice. The Phase 1 consecutive-day 09:00 seed has been removed from the
continuation path: effort confirmation now loads live busy intervals and
authoritative preferences, calculates a portfolio plan, publishes the planner
run and projections, and asks for approval of that exact versioned result.

## Implemented

### Deterministic allocation and scoring

- `PortfolioPlanner` preserves valid future owned blocks first and subtracts
  them once from the same normalized capacity pool as Calendar busy time.
- Commitment deficits use the stable order deadline → explicit priority →
  creation time → commitment ID.
- Candidate intervals are 15-minute-grid intervals from Chunk 3A. Every
  selected interval immediately becomes occupied for the rest of the
  portfolio.
- Static scoring is versioned (`stable-slot-score-v1`) and deterministic:
  preferred focus placement, deadline buffer, and longer valid blocks. The
  portfolio layer applies balanced daily load and back-to-back avoidance
  before the stable UTC start/end tie-break.
- Work-block IDs are deterministic and never recycle a known ID. Replaying
  identical authoritative facts yields an identical planner-run ID and plan.

### §11.1 risk truth

For each commitment the stored run records confirmed effort, verified
progress inputs, preserved minutes, new allocation, shortfall, remaining
unused capacity before the deadline, slack ratio, threshold version, and
previous/new risk. Thresholds are exactly:

- completed → no active risk (`unknown` read value);
- incomplete with passed deadline → `overdue`;
- unconfirmed effort → `unknown`;
- any shortfall → `critical`;
- zero shortfall and slack ratio below 0.25 → `at_risk`;
- slack ratio at least 0.25 → `on_track`.

### Versioned publication and execution safety

- `planner_runs` serialization and Firestore persistence are implemented.
- Each run includes planner, constraint, score, and risk-threshold versions;
  its horizon, Calendar snapshot hash/revision, complete commitment and
  work-block revision set, preference revision, ordering, allocations, risk
  audit, and publication/stale status.
- Before publication, Calendar busy inputs are reread and compared by stable
  hash. The Firestore transaction then verifies the full authoritative set,
  including detecting a newly active commitment or newly added/removed work
  block. A mismatch publishes no projection or outbox intent, records a stale
  run, and recalculates from current facts.
- Projection publication does not advance fact revisions. Each projection
  carries the commitment revision, complete work-block revision hash,
  planner-run ID, calculator version, and computation time.
- Approved publication diffs current persisted blocks against the desired
  plan. Creates derive a Calendar ID once from immutable work-block identity;
  moves and cancels reuse the persisted Calendar event ID. The outbox carries
  the post-plan projection hash, commitment/plan revisions, control epoch, and
  conditional event etag where required.
- The Calendar executor verifies both projection identity and its work-block
  revision provenance before crossing the external-I/O boundary.

Live Calendar is necessarily guarded by a reread/hash comparison rather than
an atomic Firestore snapshot. Phase 4 replaces this 3B boundary with the
consistent staged Calendar snapshot publication described in the build plan.

### Workflow and undo

- The ADK wrapper exposes bounded
  `calculate_portfolio_feasibility → produce_stable_plan → apply_policy`
  stages while the transaction-aware deterministic route remains authoritative.
- First-plan policy remains `first_plan_requires_approval`.
- `POST /api/v1/plans/{planner_run_id}/undo` is protected by the controlled
  session and CSRF guard. It writes an idempotent `plan_undo_requested`
  observation and audit event, then reconciliation recalculates from current
  facts. The command never edits commitments, work blocks, outbox records, or
  Calendar state directly.

## Local evidence

The full local suite passes:

- **113 tests passed**;
- Ruff clean across backend source, tests, and scripts;
- targeted mypy clean across the 18 changed 3B/runtime source files;
- Firestore index JSON parses successfully.

The 3B suite covers:

- two commitments competing for one pool receive distinct minutes;
- deterministic replay and all four ordering keys;
- insufficient capacity and exact risk thresholds;
- minimum block size, daily focus limits, and zero hard-constraint violations;
- an owned block represented in Calendar and preserved state counts once and
  remains unavailable to another commitment;
- persisted Calendar event identity wins across a moved plan revision;
- planner-run serialization round-trip;
- two active commitments plus unrelated busy time publish one guarded plan,
  projections, work blocks, and outbox set;
- a new active commitment invalidates the captured full revision set;
- guarded undo creates reconciliation input with zero direct reversal.

The remaining warnings are dependency deprecations from Starlette/httpx and
Google ADK/GenAI; none are test failures.

## Controlled-account live closure

**Completed 2026-08-13 — every step below is recorded with sanitized
identifiers in `docs/phase3_evidence/phase3_gate_run.md`.**

1. Deploy the revision containing both 3A and 3B and publish the 3A work-block
   index.
2. Confirm effort on real commitment `daf9a729…` through the guarded approval
   route.
3. Ensure at least one other controlled-user commitment is active with
   confirmed effort.
4. Observe one published `planner_runs` document containing the complete
   expected revision set and both commitments in the stable order.
5. Verify every proposed interval is future, inside configured working hours,
   before its deadline, outside actual Calendar busy time, and distinct from
   every other allocated interval.
6. Approve the exact planner run; verify the outbox creates the corresponding
   Calendar events and that each event ID matches its persisted work block.
7. Replay reconciliation and the Calendar-action tasks; verify the plan is
   reproduced and no duplicate commitment, work block, event, or allocated
   minute appears.
8. Record sanitized planner-run, projection, outbox, Calendar, and audit
   evidence. Only then change this document and `docs/phase3_plan.md` to
   **LIVE PHASE 3 GATE CLOSED**.

No deployment, real approval, or external Calendar mutation was performed as
part of the local implementation pass.
