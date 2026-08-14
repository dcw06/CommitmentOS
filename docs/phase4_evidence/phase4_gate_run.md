# Phase 4 Gate Run — 2026-08-14

**Gate (plan §17 Phase 4):** "A newly inserted meeting automatically causes
exactly one minimal repair with a complete before/after explanation."
**Result: PASSED — 17/17 verify checkpoints, warmed insert-to-repair latency
8.293 s (budget 15 s).**

Driver: `scripts/run_phase4c_gate.py` (`arm` → conflict insertion → `verify`).
Conflict insertion/removal used the companion scripts
`scripts/insert_phase4c_conflict.py` / `remove_phase4c_conflict.py` — the
Calendar API with the controlled credential, at the owner's request, instead
of the runbook's Calendar-UI click. The inserted event body is a plain timed
meeting with no CommitmentOS properties, so the observed pipeline is
identical; the demo video will still perform the UI insertion live.
Sanitization: no account addresses, mailbox content, or credentials appear
below; identifiers are truncated Firestore/Calendar IDs.

## Run 1 — FAILED 5/12, and the failure was the point

- Revision under test: `commitmentos-00036-puj` (pre-fix). Armed target:
  block `99423c96…` (event `aqrvcfu1m2r7…`), Fri 2026-08-14 16:00–17:00 UTC.
  Conflict `624qeing…` inserted 04:14:56Z.
- The mechanical pipeline was flawless: watch webhook `204` at 04:14:57 →
  source-sync `200` (incremental generation `3bb27c6a…`, 1 staged/1 applied,
  published) → typed `calendar_environmental_disruption` observation →
  reconciliation `200` at 04:14:59.
- **No repair executed.** Planner run `f7731a73…` computed the correct
  minimal repair (move one block, 60 min) but recorded `feasible = false`
  because unrelated commitment `daf9a729…` is overdue (deadline passed
  2026-08-13 23:00Z) with 120 remaining minutes that no plan can ever
  allocate. The workflow's escalation predicate was portfolio-wide
  (`not repaired.feasible`), so the in-policy repair was converted into
  `action_approval` `19b1acfb…` with `policy_reason = repair_infeasible`.
- Verify: 5/12 (target unmoved; zero patch intents, echoes, explanations;
  all baseline preservation checks passed — the system mutated nothing).

### Finding (live-only, now regression-tested)

One overdue commitment permanently disabled automatic repair for the entire
portfolio: overdue shortfall is structurally unrepairable (no slot can exist
before a past deadline), so every subsequent plan is portfolio-infeasible and
every unrelated repair escalated. Worse, approving the escalated approval
would re-escalate — `_resume_approved_calendar_repair` set `policy_override`
but the old predicate re-checked `not repaired.feasible` unconditionally,
creating an approval loop with no path to execution. The local 4B/4C fixtures
never combined an overdue commitment with a disruption, so this interaction
only appeared live. (Deployment evidence had predicted `daf9a729…` would go
overdue and called it "evidence, not a gate regression" — half right: the
overdue state itself was fine; its policy interaction was the bug.)

### Fix (deployed as `commitmentos-00031-rsz`)

- `workflows/reconciliation/phase1_workflow.py`: new
  `_repair_blocking_infeasibility(repaired)` — escalation now requires the
  repair itself to have failed: unplaced affected blocks, an
  immutable-block conflict, or shortfall left on a commitment whose deadline
  is still in the future. Overdue shortfall no longer blocks unrelated
  repair; it stays visible through `overdue` risk and the portfolio-level
  `no_feasible_plan` state (`PortfolioPlan.feasible` keeps its meaning).
- `domain/planning/repair.py`: `_repair` audit row now records
  `immutable_conflict` explicitly (previously only folded into `feasible`,
  indistinguishable from portfolio shortfall).
- Regression test
  `test_unrelated_overdue_commitment_does_not_block_automatic_repair`
  reproduces the live scenario: overdue commitment + disruption → one
  automatic patch, no approval, portfolio `feasible` honestly `false`.
- Suite: 148 passed; Ruff clean; targeted mypy clean.

Pre-fix artifact retained: approval `19b1acfb…` remains `pending` as audit
history (expires 2026-08-21). Under the fixed code, resolving it would
recalculate from current facts rather than loop.

## Interlude — clean baseline restore

Stale conflict `624qeing…` deleted 06:35Z via the guarded removal script
(refuses app-owned events and unexpected summaries). The deletion's own
watch cycle ran on the fixed revision: webhook `204` 06:36:17 → sync →
reconciliation → repair evaluation `9a6be09b…` with `moved: 0` and zero
mutations — first live exercise of the fixed predicate.

## Run 2 — PASSED 17/17

Revision `commitmentos-00031-rsz` (fix; 100% traffic), warm (min-instances 1).
Re-armed on the same target block; fresh baseline frozen.

Timeline (UTC):

| Time | Event |
|---|---|
| 06:40:43 | Conflict `ksf9h4qm…` ("Department budget review", 16:00–17:00, no ownership properties) created |
| 06:40:xx | Watch webhook `204` → source-sync `200` → generation published → disruption classified |
| 06:40:49 | Repair planner run `f298522f…` published (`calendar_state_revision` 4) |
| 06:40:51 | Patch `dbb525f9…` landed on Calendar via `If-Match` etag `"357326311…"`, control epoch 3 — **8.293 s** after provider-recorded conflict creation |
| 06:40:58 | Echo watch observation `f16ec4f9…` matched to the completed action, terminally `ignored` |

Verify checkpoints (all PASS):

```
target work block still exists
owned event ID stayed stable
target moved away from the conflicted interval
exactly one repair patch intent — 1
repair patch succeeded
exactly one suppressed repair echo — 1
echo is terminally ignored
one complete repair explanation — 1
explanation says one block moved
explanation contains before and after
explanation contains risk arc
explanation identifies unchanged blocks
every unaffected work block is byte-preserved — 4/4
one real unrelated conflict meeting detected — 1
warmed insert-to-repair latency is under 15 seconds — 8.293s
operational insert-to-repair latency is under 60 seconds
verification observed only post-arm outcomes
```

Durable outcome details:

- Target block `99423c96…`: 16:00–17:00 → **17:00–18:00 UTC**, displacement
  60 min, `plan_revision` 2, block revision 2, **calendar event ID unchanged**
  (`aqrvcfu1m2r7…`).
- `plan_repaired` explanation `006bdf5a…`: `moved_block_count` 1,
  `total_displacement_minutes` 60, before/after mutation document, preserved
  block IDs, full §11.1 risk arc:
  - `4d64796b…` (affected): shortfall 60 → 0, risk critical → on_track.
  - `daf9a729…` (unrelated, overdue): shortfall 120 → 120, risk unchanged —
    untouched and honestly reported.
- Planner run `f298522f…` `_repair` audit: moved 1, displacement 60,
  `unplaced` empty, `immutable_conflict` false, unaffected-preservation
  oracle true; portfolio `feasible: false` retained as visible truth while
  the repair executed automatically.
- The conflict meeting itself and every unrelated event were untouched; the
  4 other owned blocks are byte-identical to the armed baseline.

## Log scan (06:34–06:50Z window)

60 Cloud Logging entries: HTTP statuses `{200: 27, 204: 3}` — zero 4xx/5xx,
zero unexplained retries. Pattern scan for bearer tokens, `Authorization`
headers, refresh tokens, account addresses, and event summary content:
**zero hits**.

## Operational notes

- Warmth was enabled 04:12Z (`configure_demo_warmth.sh warm`) and restored
  to scale-to-zero 06:49Z (`normal`).
- Traffic had been pinned by name to `commitmentos-00036-puj` since the
  previous session's tagged deploys, which is why the fix deploy needed an
  explicit `update-traffic`. Recommendation recorded: return the service to
  `--to-latest` routing so future deploys take traffic automatically.
- The conflict meeting `ksf9h4qm…` and the moved block remain on the
  controlled calendar as demo state.
