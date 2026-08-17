# Post-audit hardening progress

Date: 2026-08-17  
Status: source-complete; local verification green; live gate pending

This record describes changes made after the immutable Phase 0–6 evidence
campaign. It does not alter or retroactively extend any JSON evidence under
`docs/phase*_evidence/`.

## Architecture correction

- Removed the unused reconciliation node tree, unused typed state/router, the
  duplicate policy/evidence models, unused Pub/Sub and system adapters, and
  placeholder watch/demo scripts.
- Renamed the real controller to `DurableReconciliationWorkflow`.
- Reduced the production ADK graph to its truthful boundary:
  `execute_durable_reconciliation -> finalize_reconciliation_run`. The
  controller remains the single transaction-aware implementation of
  interpretation, identity, planning, policy, and outbox work.
- Implemented production configuration guards and removed unused health and
  container placeholder methods.

## Closed backend gaps

- Gmail HTTP-404 cursor loss now marks the old generation abandoned, creates a
  bounded Inbox/Sent replacement generation, pages through it, and publishes a
  fresh profile history ID only after all items apply.
- Calendar webhook verification now rejects inactive, missing-expiry, and
  expired current/overlap channels before rate-limit writes or enqueue.
- `explain_decision` now uses the versioned `explanation_v1` prompt and strict
  JSON schema. Explanations are non-authoritative and fall back to a bounded
  deterministic statement if the model is unavailable.
- Controlled, CSRF-protected reopen and priority commands are reachable from
  API and dashboard. Both use expected revisions, append audit events, and
  emit durable observations for portfolio replanning.
- Date-only deadlines normalize to the configured 17:30 working-day end in
  the controlled IANA timezone under `deadline_normalization_v1`.
- Capacity shortfall and effort confirmation no longer masquerade as a person
  blocker. The existing P0 dependency field remains clear until a real
  dependency model exists.
- Exact visible states now include `model_output_rejected`,
  `reconciliation_retrying`, and `portfolio_capacity_conflict`. Calendar
  actions become terminal `calendar_action_failed` after five provider
  attempts instead of remaining pending forever.
- Gmail Pub/Sub change signals have a durable fixed-window fetch limit in
  addition to OIDC verification.
- Login start, accepted/rejected callback, session access, and logout produce
  minimized `access_recorded` audit events with no email address in payload.
- Frozen `autonomy_policy_v2` / `policy_thresholds_v2` adds an explicit
  `forbidden` disposition for non-owned targets and changes the
  extensive-change threshold from one block shifted over 24 hours to aggregate
  displacement over 24 hours.
- Outbox documents persist backward-compatible `before_state`; commitment
  documents persist backward-compatible `explicit_priority` and
  `last_reconciled_at`.
- Portfolio calculation reserves the before-side of pending/held/in-flight
  mutations until Calendar truth verifies that the capacity was released.

## Local verification

```text
.venv/bin/pytest
254 passed

.venv/bin/ruff check .
All checks passed!

cd frontend && npm run build
production build completed successfully (28 modules)
```

## Dashboard and documentation closure

- Today now renders the plan-required five-stat outcome strip from backend
  planner/audit read models in both live and demo modes, plus a direct newly
  detected candidate section.
- Commitment list/detail responses expose only current-provenance risk,
  remaining effort, allocation, projected finish, shortfall, shared buffer,
  and stable portfolio position. Detail no longer substitutes `unknown`/null
  when a current projection exists.
- CSRF- and revision-guarded pause/resume/dismiss controls emit durable
  observations. Pause/dismiss supersede pending confirmations, release future
  blocks through conditional cancel outbox intents, and remove the commitment
  from active portfolio demand; resume/reopen return through stable plan diff
  and policy.
- A global live control strip exposes monitoring/action modes and held/in-flight
  counts on Today, Commitments, detail, and Activity. Automatic-action resume
  requires explicit browser confirmation and records aggregate revalidation
  results after held intent is checked.
- Activity renders actor, risk arcs, stable portfolio ordering, allocations,
  control counts, resume results, and an expandable lossless audit payload.
- README operational runbooks now document Gmail serialization and automatic
  cursor recovery, bounded source generations and write budgets, publication
  barriers/final cursor promotion, authoritative versus projection state,
  static and live seed/reset behavior, extraction-eval commands, and the
  Testing-mode seven-day OAuth fallback.

## Required owner-run live gate

Before submission claims or deployment evidence are updated:

1. Verify the local and Cloud Run configuration pins
   `COMMITMENTOS_POLICY_VERSION=autonomy_policy_v2`, then deploy a new revision
   using the owner-run command in `AGENTS.md`.
2. Renew the Calendar watch once so the channel document contains status
   and overlap-expiration metadata. **Use the maintenance route
   (`gcloud scheduler jobs run commitmentos-renew-watches`) — NOT
   `scripts/calendar_watch_spike.py`, whose register/renew paths still write
   status-less channel documents that the new webhook verifier rejects.**
   Until a new-code renewal runs, every Calendar push 403s (Gmail is
   unaffected); the daily schedule alone leaves up to ~24 h of dead Calendar
   ingress after deploy.
3. Run the full security probes, including expired/inactive channel and Gmail
   rate-limit cases.
4. Run the ten-run golden campaign and replay checks.
5. Preserve results in a new evidence directory/revision; never overwrite the
   Phase 5 evidence.
