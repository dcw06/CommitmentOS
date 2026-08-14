# Phase 4 Split — Calendar Truth (4A), Repair Decisions (4B), Always-On Safety (4C)

**Authority:** plan §17 Phase 4 (Days 12–14), §9.5 (Calendar change
classification), §12.4 (repair objective), §11.1 (risk arc), §13.1
(autonomy policy + thresholds), §15 (reliability); architecture §11.5
(generation protocol), §12 (outbox machines); checklist Part II D3 (all
eight rows) plus the two Phase 1 D1 rows left open by design
(synchronized-source observation form; resume-from-synchronized-truth
after 412). Phase 3 closed live 2026-08-13 (Day 4).

Phase 4 splits on the same seam Phase 3 used — facts first, decisions
second, operations last. 4A makes Calendar state an authoritative,
versioned, classified input with no repair decisions; 4B is every decision
made from that truth; 4C is the machinery that keeps both running
unattended plus the official gate demo. 4B consumes 4A's snapshots
unchanged; 4C's gate needs both. The official Phase 4 gate closes at the
end of 4C.

## Chunk 4A — Calendar truth: bounded generations, snapshots, barrier

Everything that must be *true, versioned, and classified* before any repair
is worth deciding. Scaffolding already in place: the
`CalendarEventSnapshot`/`CalendarStateSnapshot` model is fully typed and
`CalendarSnapshotReducer` is the stub to fill
(`domain/planning/calendar_state.py`); `calendar_event_snapshots` is a
declared collection.

1. **Bounded Calendar synchronization generations** — port the proven
   Phase 2 Gmail protocol to Calendar in `SynchronizeSource`: fenced
   per-user source lease, one provider page per named task, deterministic
   generation items, page/apply checkpoints with commutative XOR manifests,
   candidate **sync token** held unpromoted until the single final
   publication transaction (D3 row 1). This removes the Phase 0 spike
   bridge for spike-shaped Calendar task bodies — the last spike-era code
   on a production path.
2. **Snapshot reduction and persistence** — implement
   `CalendarSnapshotReducer.reduce_change` / `busy_intervals` /
   `snapshot_hash`: staged items become event snapshots with etags,
   ownership properties, transparency/all-day semantics, tombstones, and
   provenance; `calendar_state_revision` increments **exactly once** per
   publication (D3 row 6).
3. **Publication barrier** — while a generation is applying, planner
   publication and Calendar-executor preflight are ineligible; the barrier
   clears at publication (D3 rows 2–4).
4. **410 full-replacement recovery** — invalid sync token marks
   `full_resync_required`; a full replacement generation tombstones
   everything not re-observed (D3 row 5). Calendar channel renewal joins
   `RunMaintenance` beside the Gmail watch renewal.
5. **§9.5 classification at snapshot-diff time** — each publication diffs
   previous vs new snapshots and materializes typed observations routed to
   affected commitments: environmental disruption (unrelated overlap),
   valid/invalid user move of an owned block, user deletion,
   application-generated echo. Echoes are matched to their completed outbox
   action and suppressed — no duplicate repair loop starts.
6. **Busy-truth switch** — `PortfolioPlanningService` reads busy intervals
   and the snapshot hash from the published snapshot store instead of the
   3B live reread/hash guard; planner runs carry the snapshot's
   `calendar_state_revision` (the 3B deviation this was always scheduled to
   replace).

**4A exit:** D3 rows 1–6 locally; live: a controlled-account two-page
Calendar generation with the published token unchanged until publication, a
barrier window proven to hold planner publication and executor preflight,
and one snapshot-driven planner run whose busy inputs match the snapshot
store byte for byte.

**Live update (2026-08-14):** 4A is closed live. A controlled two-page
generation proved page-1 token/cursor non-promotion, real planner/executor
barrier refusal, one final publication, and byte-identical planner
revision/hash against the durable snapshot store. See
`docs/phase4a_progress.md` and `docs/phase4_evidence/phase4ab_gate_run.md`.

## Chunk 4B — Repair decisions: minimal change, adoption, deletion, executor integration

Everything that decides what to do about classified change.
`StablePlanRepairer` (`domain/planning/repair.py`) is the stub to fill.

1. **Minimal-change repair core** — implement the §12.4 priority order over
   the 3B planner input: never violate a hard constraint; preserve
   completed/active blocks; adopt valid user moves first; preserve all
   unaffected future blocks across every commitment; move the fewest
   affected blocks; minimize total displacement; restore feasibility;
   escalate rather than pretend. `displacement_cost` and
   `preserves_unaffected_blocks` become the test oracle.
2. **Conflict evaluation and risk arc** — the repair run records the §11.1
   before/after: allocation deficit at detection, shortfall restored to
   zero, previous/new risk — the stored events the dashboard's outcome
   sentence and the demo explanation derive from.
3. **§9.5 decision handlers** — valid manual move: adopt as desired state,
   bump plan revision, reconcile the rest of the portfolio around it.
   Invalid move: preserve observed state, request a choice. User deletion:
   mark `user_deleted`, raise one structured decision (reschedule the
   unfinished minutes / record completed minutes / pause the commitment),
   never silently recreate.
4. **Policy enforcement** — in-policy repair executes automatically with
   notification and undo; any `policy_thresholds_v1` breach (>2 blocks,
   >24 h shift, outside preferred periods, daily-limit exceedance) converts
   the repair into an `action_approval` with durable intent and zero
   Calendar mutation. Control-epoch and automatic-action-mode checks
   enforced in the dispatcher and again immediately before Calendar I/O.
5. **Executor on snapshot truth** — `expected_observed_etag` sourced from
   the snapshot store (not a provider read), sent as `If-Match` on every
   patch and cancel (D3 row 7); a forced 412 runs the full stale-precondition
   path end to end: no overwrite, no blind retry, one durable sync request,
   no `action_result`, and reconciliation resumes only after the snapshot
   store re-synchronizes (D3 row 8 — this also closes the two Phase 1 D1
   rows left open by design). Retry adoption on insert races.
6. **ADK graph** — the repair path runs through the existing
   feasibility/plan/policy stages with a classification stage ahead of them.

**4B exit:** §16.2 repair matrix rows locally (one conflict → exactly one
moved block, unaffected blocks byte-identical, adoption, deletion decision,
threshold escalation, 412 path); live: one real user move adopted with
explanation, one forced 412 through the full path on the deployed executor.

**Live update (2026-08-14):** 4B is closed live. A real owned move was adopted
with one explanation and zero Calendar outbox delta; a real provider 412
proved stale terminal intent, no `action_result`, one synchronized-truth
handoff, a new-etag intent, and successful conditional repair. D3 rows 7–8
and the deferred D1 synchronized-resume rows are now closed live. See
`docs/phase4b_progress.md` and `docs/phase4_evidence/phase4ab_gate_run.md`.

## Chunk 4C — Always-on safety, latency, and the Phase 4 gate

Everything that runs unattended, plus the demo-grade proof.

1. **Periodic safety reconciliation** — a scheduler job that walks live
   state: work-block lifecycle transitions at their scheduled times
   (`planned → active → awaiting_check_in`, raising
   `work_check_in_required` input requests — subsuming the Phase 3 gate's
   annotated elapse-scan stand-in), drift between desired and snapshot
   state, stuck generations/leases, overdue-deadline risk refresh, stale
   planner-run cleanup.
2. **Visible failure states** — `reauth_required`, `full_resync_required`,
   held-by-control actions, stale runs, and pending decisions surfaced
   through the status/dashboard reads so silent failure is impossible.
3. **Loop suppression under echo** — the repair's own patch produces a
   watch signal; prove the echo matches the completed action and no second
   repair starts (the §9.5 fifth case, end to end under the real watch).
4. **Latency tuning** — warmed demo path (one warm instance) from conflict
   insert to repaired Calendar under 15 seconds; operational path under 60;
   measure and record both in evidence.
5. **Live Phase 4 gate (plan §17):** insert a real unrelated meeting over
   an owned block in the Calendar UI, touch nothing else — watch →
   generation → snapshot publication → classification → minimal repair
   moves exactly one block via `If-Match` (stable event ID kept) → activity
   shows the complete before/after explanation and risk arc → the second
   commitment's blocks and every unrelated event are untouched → replay
   the whole chain with zero duplicates.

**4C exit = the Phase 4 gate:** "A newly inserted meeting automatically
causes exactly one minimal repair with a complete before/after explanation."

**Local update (2026-08-14):** 4C is implemented locally: periodic safety,
visible dashboard failures, real-webhook echo suppression, latency evidence,
the one-minute scheduler job, reversible warmed-demo setting, and the live
gate driver are present. The 143-test repository suite is green. Deployment
and the controlled-account gate remain open; see `docs/phase4c_progress.md`.

**PHASE 4 GATE CLOSED (live) 2026-08-14:** the controlled-account
meeting-over-block run passed 17/17 verify checkpoints on revision
`commitmentos-00031-rsz` (8.293 s warmed insert-to-repair). Run 1 exposed a
live-only policy bug — an unrelated overdue commitment made every plan
portfolio-infeasible and escalated the in-policy repair — fixed with
`_repair_blocking_infeasibility` plus a regression test (148 green). Full
record: `docs/phase4_evidence/phase4_gate_run.md`;
gate-closure section in `docs/phase4c_progress.md`.

## Sequencing and estimate

4A ≈ one working session (the generation protocol is a port of proven
Phase 2 machinery onto an already-typed snapshot model); 4B ≈ one to two
sessions (the §12.4 core plus the repair test matrix is the deep half);
4C ≈ one session (ops wiring, tuning, gate run). All inside the plan's
Days 12–14 window, currently seven days ahead of schedule.

Note for the gate demo data: the Phase 3 busy fixtures and the five
app-owned events from the Phase 3 gate remain on the controlled calendar;
`daf9a729…`'s deadline has passed, so 4C's safety scan should surface it as
`overdue` — expected, and itself a visible-failure-state proof.
