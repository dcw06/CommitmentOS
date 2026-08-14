# Phase 4A + 4B Dedicated Live Exit — 2026-08-14

**Result:** PASS — **65/65** guarded checkpoints.

**Gate revision:** `commitmentos-00050-qar` (`phase4ab-live-gate4-20260814`).

**Normal production revision after proof:** `commitmentos-00052-did`
(`phase4ab-live-closed-20260814`), 100% traffic.

The machine-readable, sanitized record is
`docs/phase4_evidence/phase4ab_gate_run_final.json`. Identifier and etag
values below are prefixes or hashes; no provider token, credential, source
body, or controlled-account address is recorded.

## Controlled fixture and safety envelope

- One isolated 60-minute commitment was created through the deployed seeded
  observation → effort approval → initial-plan approval → outbox → executor
  workflow. Its block prefix was `2b4568cae24f…`.
- Calendar writes were limited to that verified app-owned event and 11
  uniquely tagged, transparent pagination fixtures. Every ownership check
  required `managed_by=commitmentos` plus the exact work-block ID.
- The source-sync, reconciliation, and Calendar-action queues were paused only
  for bounded proof windows. Each pause included an in-flight-delivery drain;
  guarded cleanup always restored the queues.
- After evidence capture, the 11 transparent fixtures were deleted, the
  isolated owned event was conditionally deleted, and its commitment,
  approvals, block, and outbox records were removed by exact run tag. Audit
  observations and Cloud logs remain.

## Phase 4A — authoritative Calendar truth

| Check | Live result |
|---|---|
| Provider pagination | 11 changes with page size 10 produced exactly 2 pages |
| Page 1 | 10 items staged; generation remained `staging`; continuation present |
| Cursor safety | candidate unpromoted; published cursor and both revisions byte-identical |
| Applying barrier | externally observed in the real Firestore cursor |
| Executor during barrier | `503 calendar_truth_ineligible`; zero Calendar action |
| Planner during barrier | `503 workflow_exception`; zero planner run |
| Final publication | 11 staged/applied items; equal manifests; barrier cleared |
| Revision | cursor `62 → 63`; Calendar state `62 → 63`, exactly once |
| Planner after publication | exactly one published run, `30ad195d7b22…` |
| Snapshot equality | planner hash `c3e2259cc129…` matched a fresh durable-store reduction byte-for-byte |

The barrier used a default-off five-second probe delay immediately after the
real durable barrier transaction. This made external refusal requests
deterministic without fabricating state. Production was restored to delay `0`.

## Phase 4B — decision, 412, synchronized resume, adoption

### Explicit restore and real 412

1. The owned event was moved outside planning hours. Published Calendar truth
   classified it as an invalid user move and created one structured decision.
2. The guarded HTTPS approval route resolved `restore_approved_slot`. The
   resulting PATCH intent carried the exact invalid-move snapshot etag.
3. After a source-queue drain and eligible-cursor check, provider metadata was
   changed while synchronization was held. Snapshot truth retained the old
   etag.
4. The deployed executor sent the old etag as `If-Match` and received a real
   Google HTTP 412. Outbox `9ecb37e3d549…` became
   `stale_precondition`; no `action_result` was emitted; one coalesced Calendar
   sync request was committed.
5. Independent source synchronization published the provider change. A new
   intent `de7166055ad7…` preserved the desired interval and carried the new
   synchronized etag. Old/new etag hashes were
   `46ad8ed8d9f4c0a1` / `71bd78b5a1776175`.
6. The resumed conditional PATCH succeeded and provider truth landed at the
   preserved approved interval.

### Valid move adoption

The same isolated owned event was then moved to a different constraint-safe
slot. The typed observation processed successfully, one `user_move_adopted`
explanation was recorded, plan revision advanced to 12, and the Calendar
outbox delta was exactly **0**.

## Gate findings closed during the run

1. **Cloud Tasks pause semantics:** pausing a queue does not cancel a delivery
   already in flight. The driver now drains after every pause and never assumes
   state from the control-plane acknowledgement alone.
2. **Narrow applying window:** the real barrier could publish before an
   external probe arrived. A bounded default-off probe delay now makes this
   operational proof deterministic; normal value is zero.
3. **Guarded decision API defect:** `ApprovalResolutionRequest` and its router
   did not expose/forward the domain-required Calendar `choice`. The route now
   forwards it, with contract regression coverage.
4. **Missing restore action:** `restore_approved_slot` fell through generic
   replanning and emitted no patch. It now performs one fenced explicit
   restore: clears invalid-edit state, advances authoritative revisions,
   rebuilds projection provenance, writes a snapshot-etag conditional PATCH,
   and dispatches it after commit. Integration regression coverage proves the
   desired interval and authoritative etag.

## Post-gate production and verification

- Revision `commitmentos-00052-did` serves 100% traffic and returns
  `live`/`phase4ab-live-closed-20260814`.
- Normal settings: Calendar page size `250`, apply chunk `100`, barrier probe
  delay `0`.
- All three Cloud Tasks queues are `RUNNING`; the published Calendar cursor is
  eligible with no publication barrier and no full-resync flag.
- The exact seeded commitment is absent after cleanup.
- Ruff: clean.
- Pytest: **151 passed**.
- Changed-file mypy with skipped third-party imports: clean. The repository's
  strict transitive mypy invocation still reports its pre-existing Google
  stub/baseline errors.
- The successful gate window contains the deliberately induced barrier 503s
  and their task retries. After restoration, the next safety reconciliation on
  `commitmentos-00052-did` returned 200 at `09:29:02Z`, with no continuing
  error in the post-gate scan.

