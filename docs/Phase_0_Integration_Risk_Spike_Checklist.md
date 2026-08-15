# Phase 0 Integration Risk Spike Checklist

**Target window:** Days 1–2  
**Gate:** All external systems required by the golden path work from deployed Cloud Run code. No product UI is required.  
**Authority:** `Plan_Final/CommitmentOS_Build_Plan_Final.md`, Section 17, and `Plan_Final/CommitmentOS_P0_Code_Architecture.md`  
**Restructured:** 2026-08-10 — Part I is the two-day spike core, ordered by external risk. Part II preserves every deferred acceptance item, re-tagged to the phase gate that builds its machinery. No acceptance condition was deleted.  
**Checklist status:** Execution not started. This document authorizes nothing by itself; provisioning, deployment, and credential creation begin only when Phase 0 is explicitly started.

## How to use this checklist

- Leave an item unchecked until its stated evidence exists.
- Record sanitized evidence links or artifact paths; never paste credentials, tokens, email bodies, cookies, or channel secrets.
- Mark a proof as passed only when it runs against the deployed Cloud Run service, unless the item explicitly says it is a local prerequisite.
- Throwaway spike endpoints deployed inside the spike service are acceptable and expected. Production machinery (outbox, staging generations, leases, planner) is **not** required in Phase 0; items that need it live in Part II.
- For every negative security test, verify both the HTTP result and zero durable or external side effects.
- Record blockers instead of weakening an acceptance condition.
- **Timebox rule:** each workstream carries a timebox. When a workstream exceeds its timebox, record a blocker with a next action and move on; do not silently extend the spike.
- **Solo adaptation:** this project has one builder. Every "Reviewer" field is satisfied by a recorded self-review with timestamp, written after stepping away from the work, not while finishing it.

## Recorded deviation from plan Section 17

Plan §17 lists two proofs under Phase 0 that this checklist defers, because they require machinery the roadmap itself builds later:

1. **Two-page bounded synchronization generation** (staging, checkpoints, manifest hashes, publication barrier, worker-death fencing) → moved to the Phase 2 gate (Gmail) and Phase 4 gate (Calendar). Two of its original items referenced the planner and Calendar executor, which do not exist until Phases 3–4. Phase 0 still proves the underlying external mechanic: bounded paginated fetch with an unpromoted candidate cursor (Part I, Sections 4–5).
2. **Normalized-observation transport through the reconciliation queue** → moved to the Phase 1 gate, where immutable observations are implemented. Phase 0 still proves the external mechanic: named Cloud Task creation after a durable commit (Part I, Section 4).

Update plan §17 to match, or record this deviation at the Phase 0 gate review.

## Spike record

| Field | Value |
|---|---|
| Owner | Project owner (solo build) |
| Start time | 2026-08-10 evening |
| End time | 2026-08-12 (proof work complete; gate closes at owner sign-off) |
| Google Cloud project ID | `commitmentos-505114` |
| Region | `us-west1` (fixed — Firestore created here 2026-08-10) |
| Cloud Run service URL | `https://commitmentos-1025285835715.us-west1.run.app` |
| Controlled account alias | `controlled-01` — designated 2026-08-11; address lives only in local `.env`, never in committed docs |
| OAuth client ID suffix | `…t5vkj39` (web client, stored in Secret Manager 2026-08-11) |
| Candidate OAuth mode | Both tested 2026-08-12 with identical `scope_set_v1` sequences |
| Gemini model ID | `gemini-3.5-flash` |
| ADK version | `google-adk 2.6.3` (from `uv.lock`) |
| Budget alert configured | $100 project-scoped budget, 50/90/100% email alerts (2026-08-11) |
| Evidence directory | `docs/phase0_evidence/` (sanitization rules in its README) |

---

# Part I — Two-day spike core

Ordered by external risk. Start Section 3 (OAuth) immediately after Section 2's minimal deploy: it has the longest wall-clock waits and the greatest power to invalidate the schedule, so its waits should overlap Sections 4–7.

## 1. Freeze the P0 decisions — timebox 2h

### Golden scenario

- [x] Write the one-sentence golden scenario.
  - Detect a commitment in Gmail → preserve its evidence and ownership → confirm effort → reserve Calendar capacity → observe a conflict → reconcile and minimally repair the plan → explain the action → verify completion. (The P0 closed loop from plan §1, instantiated by the proposal-revision fixture below.)
- [x] Identify the controlled Gmail thread or sanitized fixture that begins the scenario.
  - Selected source: the sanitized, deterministic three-message proposal-revision Gmail fixture from the competition demo.
  - The committed fixture is canonical for replay and seeded-demo use. A live controlled-account copy may be used for integration proof, but its Google account, thread, and message identifiers must remain in protected runtime evidence and must not replace the canonical fixture identifiers below.
- [x] Define the expected commitment title, ownership, beneficiary, and deadline.
  - **Relative-time convention:** all scenario times are offsets from seed day `T`, a Monday, in `America/Los_Angeles`. The seed script date-shifts the fixture so the scenario can run in any week; `T+1` is Tuesday, `T+3` is Thursday. Scenario clock: M1 (request) at `T 10:05`, M2 (acceptance) at `T 10:32`, M3 (deadline revision) at `T+1 08:15`. Canonical message bodies are deliberately not recorded here; they live in the repository fixture file identified by `gmail_fixture_golden_proposal_revision_001` and must exist before the Part I Section 7 Gemini proof, which consumes them.
  - Expected final commitment after all three messages — exactly one commitment record; M3 must not create a second one:

    | Field | Expected value |
    |---|---|
    | Title (canonical) | Send revised proposal to Professor Chen |
    | Ownership | `my_commitment` |
    | Owner | Controlled user (`user_fixture_controlled_001`) |
    | Beneficiary | Professor Chen (synthetic persona; the fixture sender) |
    | Deadline | `T+3` (Thursday) 16:00 `America/Los_Angeles` |
    | Deadline source expression | "bring the review forward to Thursday at 4 p.m." (M3), superseding "before our Friday 4 p.m. review" (M2 → `T+4` 16:00) |
    | Deadline confidence | At or above the auto-normalization threshold; no confirmation detour on the golden path |
    | Evidence | Spans from M2 (the promise) and M3 (the revision), linked to the fixture message identifiers above |

  - Expected per-message evolution (one record, two updates — the no-duplicates proof):

    | After message | Identity operation | Ownership | Deadline |
    |---|---|---|---|
    | M1 request | `create` (candidate) | `request_to_me` | none |
    | M2 acceptance | `update_existing` | `my_commitment` | `T+4` (Friday) 16:00 |
    | M3 revision | `update_existing` | `my_commitment` | `T+3` (Thursday) 16:00 |

  - The title is model-generated display text: acceptance requires the enum, datetime, and identity-operation fields above to match exactly; the title must identify the proposal-revision deliverable but is not compared byte-for-byte.
- [x] Define the expected effort-confirmation decision.
  - Ordering rule: no Calendar plan may exist before M3 is processed. Effort confirmation happens once, after all three messages, at scenario clock `T+1 ~08:30`, so the first and only initial plan is computed against the Thursday deadline.
  - Expected system proposal: one conservative estimate of 180 minutes.
  - Scripted user decision: confirm **180 minutes**. If a model proposal differs from 180, the scripted action edits it to 180 — downstream plan math depends only on the confirmed value, which keeps golden runs deterministic regardless of model variation.
  - Expected record: one `effort_confirmation` approval resolved exactly once, `confirmed_minutes = 180`, actor = controlled user; a replayed resolution must not create a second decision.
- [x] Define the expected initial-plan approval decision.
  - Policy expectation: the first Calendar plan always requires approval (`first_plan_requires_approval`); nothing may reach the outbox before this decision resolves.
  - Expected proposal content: three 60-minute work blocks totaling 180 minutes, all before `T+3` 16:00, fitted around the seeded busy events and the second commitment's preserved blocks, with no interval allocated to both commitments.
  - Scripted user decision: approve unchanged at scenario clock `T+1 ~08:45`.
  - Expected result: `plan_revision = 1`, lifecycle `active`, risk `on_track`, and exactly three outbox actions dispatched.
- [x] Define the expected work block and Calendar result.
  - Scheduling configuration frozen for the scenario: working hours 09:00–17:30 `America/Los_Angeles`, minimum session 30 minutes, maximum block length 60 minutes (forces the 180-minute effort into exactly three blocks), daily focus limit 180 minutes. Soft-preference precedence frozen for this scenario: balanced daily load ranks above earlier completion, making the one-block-per-day canonical layout the expected optimum. If the implemented scorer's first successful run still places blocks differently, every timeline item referencing block dates must be re-frozen together as `golden_scenario_rev_2`, not patched piecemeal.
  - Portfolio context, seeded before the golden thread begins (proves single allocation and preservation during repair):

    | Fixture field | Value |
    |---|---|
    | Second commitment | `commitment_fixture_data_summary_001` — "Prepare Q3 data summary for Sam" |
    | State | `active`, confirmed effort 120 minutes, deadline `T+7` (Monday) 12:00 |
    | Its work blocks | `block_fixture_data_summary_001` at `T+1` 14:00–15:00; `block_fixture_data_summary_002` at `T+4` 10:00–11:00 |
    | Seeded busy events | `T+1` 10:30–12:00, `T+2` 10:00–11:00, `T+3` 10:30–11:30 — unrelated meetings, never mutated |

  - Canonical expected layout for the proposal commitment (design target): `block_fixture_golden_proposal_001` at `T+1` 09:00–10:00, `block_fixture_golden_proposal_002` at `T+2` 09:00–10:00, `block_fixture_golden_proposal_003` at `T+3` 09:00–10:00. The deterministic planner's exact placement is recorded at the first successful run and then frozen; every later run must reproduce it identically. The hard-constraint envelope is non-negotiable regardless of placement: inside working hours, before the deadline, no overlap with busy events or the second commitment's blocks, no interval double-allocated, nothing scheduled in the past.
  - Expected Calendar result: three app-owned events on the controlled account's primary calendar, created only through outbox → authenticated executor, each with a stable event ID derived once from its immutable `work_block_id`, and private extended properties `managed_by=commitmentos`, `commitment_id`, `work_block_id`, `plan_revision=1`. Derived event ID values are recorded as evidence at first creation and must remain identical across retries and later plan revisions.
  - Expected block state after creation: all three `planned` with `verified_minutes = 0`; remaining effort stays 180 until an explicit check-in — elapsed time alone never reduces it.
- [x] Define the expected conflict that triggers a repair.
  - Conflict fixture: `calendar_fixture_conflict_meeting_001` — "Department advising meeting", an unrelated event with no CommitmentOS ownership properties, created directly on the controlled account's primary calendar at scenario clock `T+1 15:30`, scheduled at `T+2` 09:00–10:00 and exactly overlapping `block_fixture_golden_proposal_002`. Golden runs create it programmatically; the demo video creates it live in the Calendar UI. No "replan" control is pressed at any point.
  - Expected classification: environmental disruption per plan §9.5 — an unrelated event overlapping an owned block, not a user edit, because the owned block itself is unchanged.
  - Expected detection path: Calendar watch → webhook validation → sync → observation → reconciliation; conflict-to-repaired-plan latency under 60 seconds operationally and under 15 seconds in the warmed demo environment.
  - Expected repair (minimal-change): only `block_fixture_golden_proposal_002` moves; canonical design target `T+2` 11:00–12:00, chosen by the plan §12.4 repair objective — the smallest displacement from the approved slot, on the same day. Late-afternoon capacity on `T+1` after the conflict is detected remains *eligible* under the 180-minute daily limit but ranks lower under minimal displacement and balanced daily load; the exact placement is recorded at the first successful run and then frozen. `block_fixture_golden_proposal_001` (already completed), `block_fixture_golden_proposal_003`, both of the second commitment's blocks, and the conflict meeting itself must be untouched.
  - Expected policy result: automatic in-policy repair with notification and undo — one moved block is below the extensive-change thresholds, so no renewed approval is requested.
  - Expected mechanics: `plan_revision = 2`; one outbox patch action carrying the authoritative observed etag as `If-Match`; the moved event keeps its stable Calendar event ID; exactly one `action_result`; the follow-up Calendar watch observation matches the completed action without starting a duplicate repair loop.
  - Expected risk arc recorded in audit: the conflict evaluation records a 60-minute allocation deficit against remaining effort of 120 (after block 001's verified check-in); the repair restores `shortfall_minutes = 0` and `on_track`; the dashboard outcome sentence is derived from these stored events.
- [x] Define the expected final completion evidence.
  - Verified-progress state at completion: check-ins on `block_fixture_golden_proposal_001` (60 minutes at `T+1 ~10:05`) and the moved `block_fixture_golden_proposal_002` (60 minutes at `T+2 ~12:05`) give `verified_minutes = 120`. `block_fixture_golden_proposal_003` elapses at `T+3` 10:00 and enters `awaiting_check_in`; the user completes the commitment instead of checking it in.
  - Completion act: explicit manual confirmation by the controlled user at scenario clock `T+3 ~10:15` — before the 16:00 deadline — with canonical completion note "Sent the revised proposal to Professor Chen ahead of the Thursday review." Sent-email completion inference is P1 and must not appear on the golden path.
  - Completion evidence fixture: `completion_fixture_golden_proposal_001`.
  - Expected terminal state: `lifecycle_status = completed`, `completion_evidence_id` set, `completed_at` recorded; `verified_completed_minutes` remains 120 — 60 minutes below the confirmed estimate — proving closure without fabricated minutes. Any later reconciliation or replayed source observation must keep the commitment closed; reopening requires an explicit user action creating a new revision.
- [x] Define the expected activity-event sequence.
  - The canonical ordered sequence below is the audit contract. Exact event payloads are recorded at the first successful run and then frozen. Acceptance: the sequence appears in this order; additional non-mutating observability events are permitted; any unexplained approval, outbox, or Calendar-mutation event is a failure.

    1. Gmail observation M1 → interpretation → candidate created (`create`, `request_to_me`).
    2. Gmail observation M2 → identity `update_existing`; ownership `my_commitment`; deadline `T+4` 16:00; effort-confirmation request written; bounded run terminates.
    3. Gmail observation M3 → identity `update_existing`; deadline revised to `T+3` 16:00; the pending effort request is superseded and reissued against the new commitment revision.
    4. `approval_resolved` — effort confirmed at 180 minutes; a new bounded run starts from durable state.
    5. `planner_run` — portfolio allocation across both commitments with deterministic ordering; initial-plan proposal; policy `first_plan_requires_approval`; plan-approval request written; run terminates.
    6. `approval_resolved` — plan approved unchanged; `plan_revision = 1`.
    7. Three outbox create actions written transactionally with stable event IDs; named Cloud Tasks dispatched.
    8. Three `action_result` observations — Calendar events created; follow-up reconciliation verifies desired versus actual state.
    9. Calendar watch observations for the three created events are matched to their completed outbox actions; no duplicate repair loop.
    10. Work check-in — block 001, 60 verified minutes.
    11. Calendar observation — conflict meeting detected; classified as environmental disruption.
    12. `planner_run` — conflict evaluation records the deficit and risk before/after; minimal repair moves block 002 only; policy result: automatic in-policy repair.
    13. One outbox patch action with `If-Match`; `action_result` success; `plan_revision = 2`; notification and undo availability recorded.
    14. Calendar watch observation for the moved event matched to the completed action; feasibility restored; risk `on_track`.
    15. Work check-in — moved block 002, 60 verified minutes (total 120).
    16. Block 003 elapses → `awaiting_check_in` → check-in request visible.
    17. Completion confirmed — completion evidence stored; lifecycle `completed`; the pending check-in request is closed by the terminal state; verified minutes remain 120.
    18. Post-completion safety reconciliation and replayed observations record no reopening, no fabricated minutes, and no new mutations.
  - Expected unordered side events (explained, not failures): the second commitment's `T+1` 14:00–15:00 block elapses at 15:00 and raises a `work_check_in_required` input request that remains pending — and mutates nothing — through the golden window; periodic watch-renewal and safety-reconciliation events may appear at any point. The failure rule applies only to *unexplained* approval, outbox, or Calendar-mutation events.
- [x] Record stable fixture identifiers without recording message bodies or credentials.

  | Fixture field | Stable synthetic identifier |
  |---|---|
  | Fixture ID | `gmail_fixture_golden_proposal_revision_001` |
  | Fixture schema version | `gmail_fixture_v1` |
  | Controlled-user alias | `user_fixture_controlled_001` |
  | Gmail thread ID | `thread_fixture_golden_proposal_revision_001` |
  | Initial request message ID | `message_fixture_golden_request_001` |
  | User acceptance message ID | `message_fixture_golden_acceptance_002` |
  | Deadline-revision message ID | `message_fixture_golden_deadline_revision_003` |
  | Initial request RFC Message-ID | `<golden-proposal-request-001@fixtures.commitmentos.invalid>` |
  | User acceptance RFC Message-ID | `<golden-proposal-acceptance-002@fixtures.commitmentos.invalid>` |
  | Deadline-revision RFC Message-ID | `<golden-proposal-deadline-revision-003@fixtures.commitmentos.invalid>` |

  These identifiers are synthetic and contain no Gmail account address, provider-issued ID, message body, OAuth material, or other credential. The scenario revision remains unfrozen until the freeze item below records its self-review sign-off.
- [ ] Freeze the scenario revision and record self-review sign-off.
  - Frozen revision: `golden_scenario_rev_1`, assembled 2026-08-10, comprising the fixture identifiers and every definition block above. Any change after sign-off requires `golden_scenario_rev_2` and a decision-log entry; no silent edits.
  - Known deviation to reconcile: M3's canonical phrasing adds "p.m." to the plan §18 demo-script quote ("Thursday at 4") to keep deadline-normalization confidence high; update the §18 video script to match the fixture when demo materials are finalized.
  - Self-review sign-off (per the solo-adaptation rule, written after stepping away from the work): Owner — project owner; Timestamp — TBD. Check this item only when the sign-off timestamp is recorded.

### Autonomy policy

- [x] Confirm commitment detection and candidate display are automatic.
  - Confirmed: Gmail observation → interpretation → candidate record → dashboard display runs with no user action and no approval. The candidate stage performs no Calendar mutation and sends no external communication; its only outputs are Firestore records and activity events.
- [x] Confirm uncertain owner or deadline requires confirmation.
  - Confirmed, with the deterministic rule frozen: ownership or deadline with model confidence below **0.80** (`policy.confidence_threshold`, v1), any `ambiguous` identity operation, and conflicting deadline interpretations all require user confirmation before the commitment can enter planning.
  - Date-only deadlines default deterministically to the configured working-day end (17:30 `America/Los_Angeles`) per plan §9.7; the applied default is recorded on the deadline and is not itself treated as uncertainty.
  - Golden-scenario cross-check: M3's explicit "Thursday at 4 p.m." must normalize at or above 0.80, so the golden path takes no confirmation detour.
- [x] Confirm the first Calendar plan requires effort confirmation and plan approval.
  - Confirmed: two separate durable approvals — `effort_confirmation`, then `first_plan_requires_approval` — must both resolve before any outbox record is written. Matches golden-scenario audit steps 4–7.
- [x] Confirm in-policy repair of app-owned blocks can be automatic.
  - Confirmed: repair executes automatically with notification and undo when every touched event is app-owned, every hard constraint holds, and no extensive-change threshold below is exceeded. Adopting a valid manual move of an app-owned block is likewise automatic with explanation (plan §13.1).
- [x] Confirm extensive changes require renewed approval.
  - Confirmed: exceeding any threshold below converts the proposed repair into an `action_approval` request; the run terminates with durable intent recorded and no Calendar mutation.
- [x] Confirm non-CommitmentOS Calendar events are never modified.
  - Confirmed as forbidden at two independent layers: the policy node may never emit an action targeting a non-owned event, and the executor independently refuses any mutation on an event lacking valid CommitmentOS ownership properties (plan §9.3). The conflict meeting and every seeded busy event in the golden scenario must be unchanged after all ten runs.
- [x] Confirm user-deleted app-owned blocks are not silently recreated.
  - Confirmed: deletion marks the block `user_deleted` and raises one structured decision — reschedule the unfinished minutes, record completed minutes, or pause the commitment (plan §9.5). No recreation occurs before that decision resolves. (Not on the golden path; covered by the Section 16 test matrix.)
- [x] Confirm completion requires explicit user evidence.
  - Confirmed: P0 completion is explicit manual confirmation only, stored as completion evidence with `completed_at`; verified minutes are never fabricated to match the estimate, and sent-email completion inference stays deferred to P1.
- [x] Record the deterministic thresholds for an extensive change.
  - Frozen threshold set `policy_thresholds_v1` — a repair is extensive, and therefore requires renewed approval, when any of the following holds:

    | Threshold | Automatic limit |
    |---|---|
    | Blocks moved or canceled in one reconciliation run | more than 2 |
    | Single block start shift from its approved slot | more than 24 hours |
    | Placement outside preferred focus periods | never automatic (P0 preferred periods equal working hours 09:00–17:30, so this fires only if a future config narrows them) |
    | Daily focus-limit exceedance | never automatic — a plan feasible only by exceeding the 180-minute daily limit always requires approval |

  - Golden-scenario cross-check: the frozen conflict repair moves one block by two hours on the same day inside working hours — below every threshold, so it must execute automatically.
- [x] Freeze the policy version used during the spike.
  - Frozen: `autonomy_policy_v1`, assembled 2026-08-10 — the plan §13.1 policy table in full (including the monitoring-pause, automatic-action-pause, and developer-cleanup control rows with §13.1.1 control-epoch semantics) plus `policy.confidence_threshold = 0.80` and `policy_thresholds_v1` above. Referenced by `policy_profile = default_personal`. Any change requires `autonomy_policy_v2` and a decision-log entry.

### OAuth scopes

- [x] Record the exact requested scope URIs.
  - `scope_set_v1` — exactly four scopes, nothing else:

    | Scope URI | Purpose |
    |---|---|
    | `openid` | OIDC identity for controlled-user login |
    | `https://www.googleapis.com/auth/userinfo.email` | Account email for the allowlist check |
    | `https://www.googleapis.com/auth/calendar.events` | Read busy events, create/patch/cancel app-owned blocks, Events watch channel |
    | `https://www.googleapis.com/auth/gmail.readonly` | `users.watch`, `history.list`, full message fetch for Inbox and Sent evidence |

- [x] Record each scope's Google classification: basic, sensitive, or restricted. Note that Gmail read access is a **restricted** scope; consent and verification behavior can differ by class.
  - `openid`, `userinfo.email` — basic. `calendar.events` — **sensitive**. `gmail.readonly` — **restricted**. The restricted scope is what drives the Testing-mode seven-day refresh-token limit and the unverified-app behavior; whether the personal-use exception covers a restricted scope is precisely the Section 3 spike question.
- [x] Confirm basic identity scopes are included only as required for login.
  - `openid` + `userinfo.email` only. `profile` is deliberately omitted — the allowlist checks the email/subject claim, never a display name.
- [x] Confirm Gmail access is read-only and sufficient for commitment and Sent evidence.
  - `gmail.readonly` authorizes watch registration, history listing, and full message reads on Inbox and Sent — everything plan §9.1 needs. `gmail.metadata` was evaluated and rejected: headers-only, so it cannot supply the evidence excerpts the commitment ledger stores.
- [x] Confirm Calendar access is limited to event read/write requirements.
  - `calendar.events` only. The full `calendar` scope was rejected (adds calendar-list and settings write the product never uses). No freebusy scope needed — busy intervals derive from the synchronized events snapshot.
- [x] Confirm Gmail send scope is absent.
  - Not requested. P0 sends no email; P1 follow-up drafts are approval-gated and would require a deliberate `scope_set_v2`.
- [x] Confirm Gmail modify scope is absent.
  - Not requested; watch registration does not require it.
- [x] Confirm no Drive scope is requested.
  - Drive completion evidence is P2; no Drive scope in P0.
- [x] Freeze the scope-set version used by both OAuth configurations.
  - Frozen: `scope_set_v1` (2026-08-11). Used verbatim in four places: the consent screen's Data Access scope list, the authorization request, `Settings.required_google_scopes()`, and both Section 3 publishing-mode tests. Any change requires `scope_set_v2` and a decision-log entry.

**Decision evidence:**

- [x] Golden-scenario record attached.
  - Recorded inline in the Golden scenario block above as `golden_scenario_rev_1`.
- [x] Autonomy-policy record attached.
  - Recorded inline in the Autonomy policy block above as `autonomy_policy_v1`.
- [x] Exact scope list with classifications attached.
  - Recorded inline in the OAuth scopes block above as `scope_set_v1`.
- [ ] Self-review timestamp recorded.

## 2. Cloud foundation and minimal deployed service — timebox 3h

### Project, budget, and APIs

- [x] Record the dedicated Google Cloud project and billing status.
  - Project `commitmentos-505114`; billing account `010618-9FF625-C9150F` linked 2026-08-11 (freed a slot by unlinking the unused auto-created starter project). $150 hackathon credit form submitted, pending.
- [x] Configure a budget alert well inside the $150 credit and record its threshold.
  - $100 budget scoped to `commitmentos-505114`, email alerts at 50% / 90% / 100% (created 2026-08-11).
- [x] Record the teardown/stop list for spike resources so idle cost stays near zero.
  - Idle-cost profile: Cloud Run scales to zero (no min instances); Firestore, Pub/Sub, Cloud Tasks, and Secret Manager sit inside free tier at spike volumes; cost accrues only on Cloud Build runs and Gemini calls. Teardown if needed: `gcloud run services delete commitmentos`, `gcloud tasks queues delete` ×3, `gcloud pubsub topics delete commitmentos-gmail-watch`, stop any Gmail/Calendar watches, and (nuclear) unlink billing. The $100 budget alert is the tripwire.
- [x] Record the selected deployment region.
  - `us-west1` — free-tier eligible; fixed by Firestore creation.
- [x] Confirm Firestore is created in the intended mode and location.
  - `(default)` database, `us-west1`, created 2026-08-10.
- [x] Confirm Cloud Run API is enabled.
- [x] Confirm Cloud Build and Artifact Registry APIs are enabled if used by deployment.
- [x] Confirm Cloud Tasks API is enabled.
- [x] Confirm Pub/Sub API is enabled.
- [x] Confirm Cloud Scheduler API is enabled.
- [x] Confirm Secret Manager API is enabled.
- [x] Confirm Gmail API is enabled.
- [x] Confirm Google Calendar API is enabled.
- [x] Record API enablement evidence without project secrets.
  - All ten APIs enabled via `gcloud services enable` on 2026-08-10/11; sanitized command transcript in session log. Three Cloud Tasks queues also created in `us-west1` (`commitmentos-source-sync` with max-concurrent-dispatches=1 per plan §15.3, `commitmentos-reconciliation`, `commitmentos-calendar-actions`).

### Identities and credentials

- [x] Create or identify the controlled test account.
  - Designated 2026-08-11: owner's secondary personal address, alias `controlled-01`; the personal-mailbox caveat is accepted and recorded (run-log limitation 3).
- [x] Confirm the account is the only P0 live user allowed by the application.
  - Proven live in Section 9: a different account completing full Google sign-in received `account not allowed`; the only session in Firestore belongs to the controlled identity.
- [x] Create the OAuth web client with the exact same-origin callback URI.
  - Web client `…t5vkj39`. Same-origin callbacks registered: localhost (before the Section 3 spike) and the Cloud Run callback (added during Section 9 after a live `redirect_uri_mismatch` caught its absence).
- [x] Record the exact authorized redirect URI.
  - `http://localhost:8080/auth/callback` and `https://commitmentos-1025285835715.us-west1.run.app/auth/callback` — the only two.
- [ ] Record allowed JavaScript origins, if any, and justify each one.
  - Expected: **none** (server-side authorization-code flow needs no JS origins). Owner: during the sign-off pass, glance at the client in the console and confirm the field is empty; record "none" here.
- [x] Create distinct Pub/Sub push, Cloud Tasks, and Scheduler service identities.
  - `commitmentos-pubsub@`, `commitmentos-tasks@`, `commitmentos-scheduler@commitmentos-505114.iam.gserviceaccount.com` (created 2026-08-10; `roles/run.invoker` grants happen at first deploy).
- [x] Record the exact expected OIDC audience for each internal route group.
  - Pub/Sub: `https://commitmentos-1025285835715.us-west1.run.app/internal/pubsub`; Cloud Tasks: `…/internal/tasks`; Scheduler: `…/internal/scheduler`. Set in the deployed service env 2026-08-11; local `.env` keeps localhost equivalents.
- [x] Record the exact expected service identity for each internal route group.
  - `commitmentos-pubsub@`, `commitmentos-tasks@`, `commitmentos-scheduler@commitmentos-505114.iam.gserviceaccount.com`; each granted `roles/run.invoker` on the service 2026-08-11.
- [x] Confirm runtime secrets are referenced through Secret Manager.
  - All three secrets exist (2026-08-11): `commitmentos-oauth-client`, `commitmentos-gemini-api-key` (v2 enabled, exposed v1 destroyed; smoke-tested HTTP 200 against `gemini-3.5-flash`), `commitmentos-calendar-channel-token` (384-bit, generated in-pipe). `.env` holds only resource references; no secret value stored locally.
- [x] Confirm no refresh token or client secret is placed in Terraform state.
  - Terraform is not in use (no state exists; `infra/terraform` is empty scaffolding). All secret values live only in Secret Manager; revisit this item if Terraform is ever adopted.

### Minimal service

- [x] Deploy one minimal Cloud Run service.
  - `commitmentos` in `us-west1`, deployed from source Dockerfile 2026-08-11, runtime identity `commitmentos-runtime@commitmentos-505114.iam.gserviceaccount.com`, full env-var set injected from the generated YAML (no secret values, refs only).
- [x] Record the deployed revision and immutable image digest.
  - Revision `commitmentos-00002-8b2`; image digest `sha256:e0628c38270c…8a367` (full value in Cloud Run console).
- [x] Confirm the service has public IAM-edge invocation for the Calendar webhook requirement.
  - Deployed with `--allow-unauthenticated`; application-layer trust contracts are the protection, per plan §13.5.
- [x] Confirm application-level authorization still protects every non-public route.
  - Full route inventory audited 2026-08-12: 11 routes, each with a documented guard — 3 OIDC route groups (pubsub/tasks/scheduler identities), channel-token webhook, session-cookie app read, single-use-state callback, and 4 deliberately public endpoints (health, login entry, demo read, demo-mutation rejector). No unguarded route exists. Re-verify at each future route addition.
- [x] Confirm `/health/live` returns process status without reading or writing business state.
  - Returns `{"status":"live","version":"0.1.0"}` over the public URL; handler touches no dependency.
- [x] Confirm request logs redact authorization headers, cookies, OAuth material, channel tokens, email bodies, and prompts.
  - Closed 2026-08-12 at the Phase 1 gate: `bootstrap/logging.py` (`SensitiveDataRedactor` on every handler, key- and pattern-based) deployed in revision `commitmentos-00013-tw2`; Cloud Logging scan over the live gate window found zero bearer tokens, auth headers, cookies, OAuth material, or account addresses (access lines carry method+path+status only). Blocker B3 closed.

## 3. OAuth publishing-mode decision spike — start first; active ~4h plus wall-clock waits

Run the same frozen scope set and acceptance tests for both configurations. This is the plan's named Phase 0 decision spike; its outcome sets the reconnection cadence for the entire build.

### External / In production / unverified personal use

- [x] Configure the candidate In-production consent-screen state.
  - Published to In production 2026-08-12 after the Testing pass completed.
- [x] Confirm the controlled account can reach and understand the unverified-app warning.
  - The controlled account completed the full consent flow to a working token, so any warning shown was traversable. The owner's description of the screens is the remaining record below.
- [ ] Record the exact warning and click-through behavior.
  - Owner observation + screenshots to `docs/phase0_evidence/` still pending.
- [ ] Record whether the warning or click-through behavior differs between the restricted Gmail scope and the sensitive Calendar scope.
  - Owner observation pending.
- [x] Confirm the application allowlist still rejects other accounts.
  - Proven live in Section 9: the admin account completed a full Google sign-in and received `account not allowed` with no session created. In production mode this application layer is the sole rejection gate (Google's test-user gate no longer applies), and it holds.
- [x] Confirm an authorization code can be exchanged successfully.
  - PKCE exchange succeeded 2026-08-12; account verified as the controlled identity.
- [x] Confirm a refresh token is issued when expected.
  - Issued on both production authorize runs; **all 4 of 4 scopes granted, including restricted `gmail.readonly`** — the unverified personal-use path works.
- [x] Refresh an access token and record the observed behavior.
  - `refresh_result: OK`, ~3598 s access-token lifetime — identical to Testing mode.
- [x] Renew Gmail and Calendar watches using the refreshed credential.
  - Completed with Sections 4–5: Gmail `users.watch` registered and Calendar channel registered *and* renewed, all using credentials derived from the production refresh token (secret v4).
- [x] Revoke the credential and confirm subsequent refresh fails.
  - Revoke returned 200; refresh then failed `invalid_grant`. **Finding: revocation is grant-wide** — revoking the production token also killed the still-stored Testing-issued token, so one revoke invalidates every refresh token for the client-user pair.
- [x] Complete reauthorization after revocation.
  - Restored 2026-08-12; live token is secret version 4, verified refreshing; versions 1–3 destroyed.
- [x] Record the unverified-user limitation and avoid any public-readiness claim.
  - Unverified in-production apps are capped (≈100 users) and show warning screens; P0 claims personal-use operation for the controlled account only, never public readiness. Verification and public onboarding remain explicitly out of scope (plan §13.4).

### External / Testing

- [x] Configure the controlled account as an explicit test user.
  - Registered 2026-08-12. Bonus negative evidence: the admin (non-test-user) account attempting consent received Google's `403 access_denied` block — screenshot to evidence pack.
- [ ] Record the Testing consent warning and user flow.
  - Flow completed; the per-scope-class warning observations and screenshots (owner-captured) still need to land in `docs/phase0_evidence/`.
- [x] Confirm an authorization code can be exchanged successfully.
  - PKCE authorization-code exchange succeeded via `scripts/oauth_spike.py authorize --mode testing`, 2026-08-12; account verified as the controlled identity via id_token email.
- [x] Confirm a refresh token is issued when expected.
  - Issued on both authorize runs (`access_type=offline`, `prompt=consent`); all 4 of 4 `scope_set_v1` scopes granted.
- [x] Record the expected seven-day limitation for non-basic scopes.
  - Expected per Google policy for Testing-mode apps with non-basic scopes; token issued 2026-08-12, so expiry expected ~2026-08-19. Empirical confirmation lands when the token dies (or survives) — either result is evidence.
- [x] Record whether a refresh token issued while in Testing retains its seven-day expiry after the app is switched to In production, or whether re-consent is required to obtain a durable token.
  - Resolved as moot 2026-08-12: the Testing-issued token was destroyed by the grant-wide revocation before day 7 could arrive, and the immediate post-switch refresh had already succeeded. With production mode final, the operative rule is simpler and proven: any revocation kills all tokens, and a fresh production consent issues a durable one.
- [x] Refresh an access token and record the observed behavior.
  - `refresh_result: OK`, new access-token lifetime ~3598 s.
- [x] Renew Gmail and Calendar watches using the refreshed credential.
  - Superseded by the mode selection: watch registration and renewal were proven with the production credential (Sections 4–5); the Testing-issued tokens were destroyed by the grant-wide revocation. Re-run under Testing only if the mode decision ever reverts.
- [x] Revoke the credential and confirm subsequent refresh fails.
  - Revoke endpoint returned HTTP 200; the following refresh failed with `invalid_grant: Token has been expired or revoked` — the failure classification Section 8/13 handling is built against.
- [x] Complete reauthorization after revocation.
  - Second consent flow succeeded; new refresh token stored as secret version 2 and verified working; dead version 1 destroyed.
- [x] Record the required reconnection timing for build, testing, and recording.
  - While in Testing mode: reconnect the controlled account at least every 7 days during the build, plus a mandatory fresh reconnect immediately before golden-run testing and again before demo recording. `scripts/oauth_spike.py authorize` is the reconnection runbook.

### Selection decision

- [x] Compare authorization success between the two configurations.
  - Both succeed for the controlled account with 4/4 scopes granted. Testing additionally blocks non-test-users at Google's gate (403); production does not.
- [x] Compare observed refresh-token behavior.
  - Both issue refresh tokens with ~1-hour access tokens. Testing carries the documented 7-day expiry for non-basic scopes; production tokens have no such policy expiry (empirical confirmation accrues as the v4 token ages past 7 days during the build).
- [x] Compare watch-renewal behavior.
  - Proven end to end under the selected production mode (Gmail watch, Calendar watch + renewal). A Testing-mode comparison is unnecessary after the selection; the API calls are identical.
- [x] Compare revocation and reauthorization behavior.
  - Identical in both modes: revoke 200 → `invalid_grant` → reauthorization restores. Revocation is grant-wide in both.
- [x] Compare controlled-account allowlist enforcement.
  - Proven live 2026-08-12 (Section 9): a non-allowlisted account completing full Google sign-in receives `account not allowed` with no session. In production mode this application layer is the sole rejection gate, and it holds.
- [x] Select the P0 publishing mode.
  - **Selected and FINAL: External / In production / unverified personal use** — no 7-day reconnection treadmill, restricted scope granted cleanly, watch renewal confirmed (Sections 4–5), application allowlist confirmed (Section 9). Fallback remains Testing with the 7-day runbook, fully proven.
- [x] Record the decision, evidence, limitations, owner, and timestamp.
  - Decision-log entry added 2026-08-12; evidence = sanitized spike outputs in the session record plus pending owner screenshots; limitation = unverified personal-use only.
- [x] Confirm `reauth_required` remains mandatory regardless of selected mode.
  - Reinforced by the grant-wide revocation finding: any revocation kills every stored token, so the visible `reauth_required` state and the `oauth_spike.py authorize` reconnection runbook stay mandatory in production mode too.

## 4. Gmail watch delivery — timebox 3h

- [x] Grant `gmail-api-push@system.gserviceaccount.com` the Publisher role on the Gmail notification Pub/Sub topic **before** registering the watch; record the grant.
  - Granted on `projects/commitmentos-505114/topics/commitmentos-gmail-watch` at topic creation, 2026-08-10 — before any watch exists.
- [x] Register `users.watch` for the controlled Gmail account.
  - Registered 2026-08-12 via `scripts/gmail_watch_spike.py register` using the production refresh credential.
- [x] Record the initial history ID and watch expiration.
  - History ID `17427`; expiration 2026-08-19T03:46:40Z (7-day watch — daily renewal becomes real in Phase 2).
- [x] Deliver a controlled Gmail change.
  - Test email sent into the controlled mailbox 2026-08-12.
- [x] Confirm Pub/Sub receives the Gmail watch notification.
  - Push deliveries observed at the deployed ingress within seconds of the change.
- [x] Confirm Pub/Sub push uses the configured OIDC audience and identity.
  - Accepted only after audience + `commitmentos-pubsub@` identity validation. Real-token negative evidence: a stale deployed identity expectation caused genuine Google-signed pushes to be rejected 403 with zero side effects until corrected (see `docs/phase0_evidence/s04_gmail_watch_trace.md`).
- [x] Confirm the ingress route decodes only the account and new history ID.
  - Route parses `emailAddress` + `historyId` from the envelope and rejects unexpected mailboxes; no message content exists in the notification.
- [x] Confirm the ingress route commits or coalesces a durable sync request before acknowledgment.
  - Firestore transactional upsert of `sync_requests/gmail:{user}` precedes the 204 acknowledgment.
- [x] Confirm the route creates a named source-sync Cloud Task after commit.
  - Named `gmailsync-{user}-{historyId}` on `commitmentos-source-sync`; executed with OIDC as `commitmentos-tasks@`.
- [x] Confirm the task payload contains references and no email body or OAuth token.
  - Payload is `{schema, source, user_id, latest_history_id}` only.
- [x] Confirm duplicate Pub/Sub delivery converges on the same durable sync request.
  - Proven with real retries: 3 deliveries → one document with `delivery_count: 3`; named-task dedup absorbed repeat dispatches.
- [x] Confirm a sync request committed without its task remains discoverable for repair (manual re-dispatch is acceptable in Phase 0; the periodic dispatcher is Phase 1).
  - Closed with blocker B1 at the Phase 1 gate (row left unchecked by oversight; reconciled 2026-08-14): fault-injected task-creation failure leaves the durable `pending` record, and `RunMaintenance.dispatch_pending` recreates the same named task (`test_crash_gap_repaired_by_maintenance`); repair route deployed behind scheduler OIDC and proven live via the Cloud Scheduler job (Phase 2 gate).
- [x] Confirm the sync worker can page `history.list` with a bounded page size and hold the next-page cursor without promoting it (full staging-generation protocol is Phase 2).
  - One bounded page (max 25) returned 4 history records; `candidate_history_id: 17550` held on the request while `published_history_id` remained `17427`.
- [x] Capture sanitized evidence for every hop.
  - `docs/phase0_evidence/s04_gmail_watch_trace.md`.

## 5. Calendar watch delivery and webhook boundary — timebox 3h

### Watch registration and valid delivery

- [x] Register an events watch for the controlled Calendar.
  - Registered 2026-08-12 via `scripts/calendar_watch_spike.py register`, after recording a baseline sync token as the published cursor.
- [x] Persist channel ID, resource ID, resource URI, expiration, and token hash.
  - `calendar_channels` document holds all five; the token is stored only as its SHA-256 hash.
- [x] Confirm the raw channel secret is absent from logs.
  - The secret is read from Secret Manager and compared in memory; no handler logs headers, and the registration script prints only a hash prefix.
- [x] Create or modify a controlled Calendar event.
  - Spike-owned poke event inserted through the API (private extended property `managed_by=commitmentos-spike`).
- [x] Receive the valid webhook notification at the dedicated public route.
  - Registration `sync` handshake and the poke's `exists` notification both received and validated.
- [x] Confirm the webhook validates the channel token with a constant-time comparison, plus channel ID, resource ID, resource state, and Calendar mapping.
  - `hmac.compare_digest` plus the full header-validation chain; matrix below exercises each rejection.
- [x] Confirm the notification creates or coalesces only a Calendar sync request; the webhook performs no Calendar fetch and treats notification headers only as a change signal, never as event contents.
  - Webhook writes only the coalesced request document; the fetch happens exclusively in the authenticated worker.
- [x] Confirm a named source-sync Cloud Task is created only after the request commits.
  - `calsync-{user}-{messageNumber}` created after the transactional commit.
- [x] Confirm the authenticated source-sync worker performs the later incremental fetch with a bounded page size, holding the next sync token without promoting it (full staging-generation protocol is Phase 4).
  - One bounded page (max 25) saw the 1 changed event; candidate sync token held; published token unchanged throughout.
- [x] Confirm duplicate valid signals converge on one eligible synchronization path.
  - Two replayed identical signals: `signal_count: 3` on one document, second dispatch absorbed by named-task dedup, 2 worker executions across 3 signals, idempotent re-fetch.

### Invalid requests — each with zero side effects

- [x] Test a missing channel token.
  - 401.
- [x] Test an incorrect channel token.
  - 403.
- [x] Test an unknown channel ID.
  - 403 (probe carried the genuine token).
- [x] Test a mismatched resource ID.
  - 403 (genuine token).
- [x] Test an invalid channel state.
  - 400 (genuine token).
- [x] Test a non-empty body.
  - 400.
- [x] Test an unexpected HTTP method.
  - 405.
- [x] Confirm a basic rate limit applies to the route (durable per-channel rate limiting is Phase 5).
  - Demonstrated live 2026-08-12 during the gate audit: 22 rapid valid-token signals on one channel ID → exactly 20 processed (each rejected downstream, zero side effects), then 429 on the 21st and 22nd. Per-instance in-memory; durable version deferred to Phase 5.
- [x] Confirm each invalid request creates no sync request, no Cloud Task, and no Google API call.
  - `sync_requests/calendar:…` remained absent after the full matrix; evidence in `docs/phase0_evidence/s05_calendar_webhook_trace.md`. (An initial least-privilege gap made cases 2–5 fail 500 before any write — fixed by granting the runtime SA access to the channel-token secret.)

### Renewal

- [x] Confirm the watch-renewal path records replacement channel metadata.
  - `renew` opened channel `7edd3e89…` and recorded `previous_channel_id`/`previous_resource_id` for overlap acceptance.
- [x] Confirm the old channel is stopped or retired safely.
  - `channels.stop` on `bf3e855f…` immediately after the replacement opened.
- [x] Confirm renewal remains possible after token refresh.
  - Registration and renewal both used credentials derived from the production refresh token (secret v4).

## 6. Calendar API behavior: stable identity, If-Match, forced 412 — timebox 3h

These are provider-behavior proofs using throwaway spike code deployed in the service. Outbox persistence, the `stale_precondition` state machine, and executor integration are Phase 1/4 (Part II).

All rows proven 2026-08-12 by the self-verifying `scripts/calendar_identity_spike.py run` (10/10 checks passed first run); evidence with the exact 412 shape in `docs/phase0_evidence/s06_calendar_identity_412.md`. Run from local spike code with the production credential; the deployed executor path is Phase 1's seeded-slice gate.

### Stable insert and adoption

- [x] Derive the Calendar event ID from immutable work-block identity and a recorded algorithm version.
  - `base32hex(sha256("commitmentos:v1" + calendar_id + work_block_id))` → deterministic 52-char base32hex-safe IDs.
- [x] Insert the first app-owned Calendar event with the stable client-supplied ID.
- [x] Persist the required private ownership properties on the event.
  - `managed_by`, `commitment_id`, `work_block_id`, `plan_revision` per plan §9.3.
- [x] Retry the insert and confirm lookup/adoption finds the existing owned event.
  - Provider returned 409; adoption fetched and verified ownership properties.
- [x] Confirm retry does not create a second Calendar event.
  - Exactly one event for the work block after retry.
- [x] Reject adoption if ownership properties do not match.
  - Mismatched `work_block_id` → adoption refused.

### Conditional mutation

- [x] Change the desired time and confirm the Calendar event ID remains unchanged.
  - Plan revision changed 1→2 in properties; identity unchanged.
- [x] Load the authoritative observed `etag` from a fresh Calendar read.
- [x] Patch the owned event using that etag as `If-Match`.
- [x] Confirm an eligible cancellation also requires `If-Match`.
  - Conditional delete succeeded.
- [x] Confirm no non-owned Calendar event can be patched, adopted, or canceled.
  - Ownership guard refused; unrelated event verified unchanged.

### Forced 412 proof

- [x] Create an intervening Calendar edit after recording the etag.
- [x] Execute the old conditional mutation and force `412 Precondition Failed`.
- [x] Confirm the spike code stops on 412: no replacement etag is fetched for a blind retry and no overwrite attempt occurs.
  - Event retained the intervening editor's state, byte-identical.
- [x] Record the exact 412 response shape for the Phase 1/4 outbox state machine.
  - `FAILED_PRECONDITION` / `failedPrecondition`; full JSON in the evidence file.

### Spike cleanup

- [x] Preview cleanup targets before mutation.
- [x] Confirm only events with valid CommitmentOS ownership properties are targeted.
  - Ownership re-verified per event before deletion.
- [x] Delete spike-created events and confirm unrelated Calendar events remain unchanged.

## 7. Gemini structured output and one deployed ADK graph run — timebox 2h

All rows proven 2026-08-12; evidence with the API-schema finding and ADK 2.6.3 usage notes in `docs/phase0_evidence/s07_gemini_adk_proof.md`. The golden fixture file (`backend/tests/fixtures/gmail_fixture_golden_proposal_revision_001.json`) and frozen prompt were created here, satisfying the Section 1 fixture dependency.

### Gemini structured output

- [x] Call the pinned `gemini-3.5-flash` model from deployed Cloud Run code.
  - Deployed call succeeded; API-reported model `gemini-3.5-flash`, 1.6 s latency warm.
- [x] Use the frozen prompt version and strict output schema.
  - `commitment_interpretation_v1` + `extraction_v1`. Finding: the API's response-schema proto rejects `additionalProperties`, so the API receives a sanitized guidance copy while the `extra="forbid"` pydantic model stays the authoritative validator.
- [x] Delimit source text as untrusted data.
  - `<untrusted_source_messages>` markers plus an explicit instructions-are-data rule.
- [x] Confirm the response parses into the declared commitment-interpretation schema.
  - Accepted with zero deterministic violations; all golden expectations matched (my_commitment, Professor Chen, Friday 2026-08-14T16:00-07:00 resolved from relative language).
- [x] Confirm unknown fields are rejected.
  - Local rejection test passed.
- [x] Confirm invalid ownership, date, range, or evidence offsets are rejected safely.
  - Enum, naive-datetime, confidence-range, effort-range rejections passed; fabricated evidence quotes caught by the substring-of-source check.
- [x] Confirm model confidence does not grant action authority.
  - Interpretation is an action-free proposal (prompt rule 6); no tool or mutation path exists on the route.
- [x] Record model ID, prompt version, schema version, thinking level, latency, token usage, and estimated cost per call.
  - 605 prompt / ~210 output tokens; well under $0.001/call; `thinking_level: low` applied.
- [x] Confirm persisted metadata excludes the complete source body and prompt.
  - `model_calls` documents carry versions, latency, tokens, disposition only.

### ADK 2.x graph

- [x] Record the exact pinned Google ADK 2.x version.
  - `google-adk 2.6.3` (spike record).
- [x] Deploy one bounded reconciliation graph.
  - Three-node `Workflow` deployed and executed via `/internal/spike/graph-run` under scheduler OIDC.
- [x] Confirm `load_observation` is the first registered node.
  - Durable outcome `route[0] == "load_observation"` (the `__START__` sentinel precedes by construction).
- [x] Start the graph from one durable observation.
  - Input was the Section 4 `sync_requests/gmail:…` document.
- [x] Complete one deterministic route through the graph.
  - `load_observation → validate_observation → finalize_run`, 3 events, no branching.
- [x] Confirm the graph performs no direct Calendar mutation.
  - `calendar_mutations: 0`; no Calendar client exists in the graph path.
- [x] Confirm the run writes a durable typed outcome and terminates.
  - `reconciliation_runs/run-spike-620dcd21fbec`, outcome `observation_acknowledged_no_action`, terminated 84 ms after start.
- [x] Capture sanitized run metadata and the durable outcome IDs.
  - Evidence file above.

## 8. Reauthorization and invalid-refresh behavior — timebox 2h

All rows proven live 2026-08-12; full cycle and findings in `docs/phase0_evidence/s08_reauthorization_cycle.md`.

- [x] Begin with a valid controlled-account credential.
- [x] Confirm Gmail and Calendar reads succeed.
  - Both source-sync workers `fetched` against the deployed service.
- [x] Revoke or invalidate the refresh token through the controlled procedure.
  - `oauth_spike.py revoke` → HTTP 200.
- [x] Attempt refresh and capture the expected provider failure classification.
  - `RefreshError('invalid_grant: Token has been expired or revoked.')` — consistent across every observation this spike.
- [x] Persist the visible `reauth_required` source state.
  - Both sync-request documents flip to `reauth_required` with `auth_error` and `auth_failed_at`.
- [x] Stop dependent work rather than using stale cached source data.
  - Workers return the stop state with no source data. Finding: revocation kills the cached access token immediately too — no grace window.
- [x] Confirm no silent fallback to cached email or Calendar contents.
- [x] Confirm no Calendar mutation executes using invalid credentials.
  - Spike workers are read-only; the owned-mutation guard requires a working credential to verify ownership before any write.
- [x] Complete a fresh authorization-code flow.
  - Refresh token stored as secret version 5; dead version 4 destroyed.
- [x] Confirm the previous OAuth transaction cannot be replayed.
  - Second exchange of the same authorization code → HTTP 400 `invalid_grant`.
- [x] Confirm new Gmail and Calendar access succeeds.
  - Both sources recovered without redeploy (credential cache cleared on failure picks up the new secret version).
- [x] Renew both watches after reauthorization.
  - Gmail watch re-registered (history 17550); Calendar channel rotated with old channel stopped.
- [x] Record scheduled reconnection during the build.
  - Selected production mode has no token-expiry cadence; reconnection is event-driven on `reauth_required` via the recorded runbook. Watches still expire every 7 days until Phase 2 automation.
- [x] Record reconnection immediately before golden-run testing and recording.
  - Mandatory fresh reconnect + watch renewal before golden runs and before demo recording, recorded in the runbook.

## 9. Internal delivery and demo trust contracts — minimal depth, timebox 2h

Happy path plus one representative negative per contract. Exhaustive negative matrices are Phase 5 (Part II).

All rows proven 2026-08-12; evidence in `docs/phase0_evidence/s09_trust_contracts.md`.

### Pub/Sub and Cloud Tasks OIDC

- [x] Accept a Pub/Sub request with the exact audience and push identity.
  - Proven by live Gmail push traffic (Section 4).
- [x] Reject a validly signed Pub/Sub token with the wrong audience.
  - Real impersonated token, scheduler audience → 403.
- [x] Reject a validly signed Pub/Sub token with the wrong service identity.
  - Real tasks-SA token with the correct pubsub audience → 403 (plus the Section 4 stale-env incident as live evidence).
- [x] Accept a Cloud Tasks request with the exact audience and task identity.
  - Proven by live worker traffic (Sections 4–8).
- [x] Reject a validly signed task token with the wrong audience.
- [x] Reject a validly signed task token with the wrong service identity.
- [x] Confirm every rejected request causes zero Firestore writes and zero task dispatches.
  - Verification precedes any durable operation in every handler.

### Controlled-user session — minimal

- [x] Implement login with a one-time state/nonce/PKCE transaction (the full negative matrix is Phase 5).
  - Hashed state as transaction ID, hashed nonce, PKCE S256, 10-minute expiry, transactional single-use consumption; login requests only `openid email`.
- [x] Load the application and API from the same Cloud Run origin with CORS disabled.
  - No `Access-Control-Allow-Origin` on any response.
- [x] Complete login with the controlled account.
  - Live: `/app/me` → `authenticated: true` for `user_fixture_controlled_001`.
- [x] Reject a non-allowlisted account with no session issued.
  - Live: admin account completed a full Google sign-in and received `account not allowed`; Firestore still holds exactly one session (the controlled user's). **This closes the last provisional condition on the OAuth mode selection.**
- [x] Confirm the session is server-side and the browser cookie is opaque.
  - Firestore stores only the SHA-256 of the random token.
- [x] Confirm the cookie is host-scoped, `Secure`, `HttpOnly`, and `SameSite=Lax`, and contains no OAuth token.
- [x] Confirm an authenticated read succeeds.

### Seeded demo — minimal

- [x] Confirm the public demo read route returns only repository-owned seeded data.
  - `/demo/today` serves the static seeded file derived from `golden_scenario_rev_1`.
- [x] Confirm the demo path cannot obtain or use the controlled account credential.
  - The handler reads a static file; no credential access path exists.
- [x] Attempt one representative production mutation path under `/demo` and confirm rejection with zero side effects (the full mutation matrix is Phase 5).
  - `POST /demo/*` → 403, no writes, no dispatches, no Google API calls.

## 10. Evidence pack

Assembled 2026-08-12 in `docs/phase0_evidence/` (sanitization rules in its README).

- [x] Golden-scenario and policy decision records.
  - Inline in Section 1 (`golden_scenario_rev_1`, `autonomy_policy_v1`).
- [x] Exact OAuth scopes with classifications and the publishing-mode comparison.
  - Inline in Sections 1 and 3 (`scope_set_v1`; both modes tested, selection final).
- [x] Sanitized Cloud Run revision and image digest.
  - First revision + digest in Section 2; 10 revisions over the spike, final `commitmentos-00010-wzf` (run log).
- [x] Gmail watch-to-task trace, including the push service-account grant.
  - `s04_gmail_watch_trace.md`.
- [x] Calendar watch-to-incremental-fetch trace and invalid-request rejections.
  - `s05_calendar_webhook_trace.md`.
- [x] Stable Calendar ID, adoption, `If-Match`, and forced-412 evidence.
  - `s06_calendar_identity_412.md`.
- [x] Gemini structured-output metadata with latency and cost.
  - `s07_gemini_adk_proof.md`.
- [x] ADK graph-run metadata and durable outcome IDs.
  - `s07_gemini_adk_proof.md`.
- [x] Revocation, `reauth_required`, and successful reauthorization evidence.
  - `s08_reauthorization_cycle.md`.
- [x] Minimal trust-contract positive and negative results with zero-side-effect evidence.
  - `s09_trust_contracts.md`.
- [x] Sanitized command transcript or run log.
  - `s00_spike_run_log.md` (timeline; full transcript in the build session log; all values reproducible from `scripts/`).
- [x] Known limitations and unresolved blockers.
  - Seven-item list in `s00_spike_run_log.md`; open items mirrored in the blocker log below.
- [x] Budget status after the spike and teardown confirmation for idle resources.
  - No budget alert fired (spend < 50% of $100); scale-to-zero idle profile; teardown list in Section 2.

## 11. Phase 0 gate

Gate review prepared 2026-08-12. Thirteen of fourteen rows pass on recorded evidence; the last is the owner's sign-off.

- [x] Every Part I section has a recorded result.
  - Sections 1–10 complete with inline evidence and six files in `docs/phase0_evidence/`.
- [x] Every failed item has a blocker and next action recorded.
  - Three blockers logged, none a failed proof: B1 (Section 4 crash-gap fault injection → Phase 1 dispatcher), B2 (owner consent-warning screenshots), B3 (redacting logging middleware → Phase 1). Plus one 30-second owner glance: confirm the OAuth client's JavaScript-origins field is empty (§2 row).
- [x] The selected OAuth publishing mode, its limitations, and the reconnection cadence are recorded.
  - Final: In production / unverified personal use; limitations in Section 3; event-driven reconnection runbook in `s08`.
- [x] Gmail watch reaches a durable named source-sync task through authenticated Pub/Sub.
- [x] Calendar watch reaches an authenticated incremental fetch; invalid webhook requests have zero side effects.
- [x] Stable Calendar identity prevents duplicate events across insert retry.
- [x] Conditional mutation with `If-Match` works and a forced 412 causes zero overwrite attempts.
- [x] Gemini structured output succeeds from deployed code.
- [x] One deployed ADK 2.x graph run succeeds and terminates durably.
- [x] Invalid refresh produces `reauth_required` and reauthorization restores access.
- [x] All four route trust contracts pass at minimal depth.
- [x] All external systems required by the golden path work from deployed Cloud Run code.
  - Gmail, Calendar, Pub/Sub, Cloud Tasks, Firestore, Secret Manager, Gemini, and ADK all exercised from revision `commitmentos-00010-wzf` or its predecessors.
- [x] The Section 17 deviation (deferred generation and transport proofs) is recorded in the gate review.
  - Recorded in the checklist header and here: two-page staging generations → Phase 2/4 gates; observation transport → Phase 1 gate. Plan §17 should be updated to match.
- [ ] Self-review sign-off with timestamp.
  - Owner action, together with the Section 1 sign-offs (golden-scenario freeze, decision-evidence timestamp).

---

# Part II — Deferred acceptance items

Every item below was part of the original Phase 0 checklist. Each is preserved verbatim in intent and re-tagged to the phase gate that builds its machinery. Check them in that phase; none may be dropped.

## D1. Phase 1 gate additions — outbox, observations, leases, approvals

**Gate closed 2026-08-12.** Deployed seeded slice ran 18/18 checkpoints against revision `commitmentos-00013-tw2` with real Calendar (run tag `20260812t152758`, driver `scripts/run_seeded_slice.py run --pause-proof`); local proofs in `backend/tests/` (53 passing). Two rows stay open where their machinery is later-phase; both are annotated.

### Normalized-observation transport

- [x] Produce a normalized observation from synchronized source data.
  - Closed locally in Phase 2/4: published Gmail and Calendar generations create deterministic immutable observation envelopes, and Calendar snapshot diffs create typed repair inputs (`test_incremental_snapshot_and_token_publish_exactly_once`, `test_owned_event_move_validity_and_user_deletion_are_typed`). Live synchronized-source evidence remains part of the Phase 4 gate run.
- [x] Confirm the immutable observation commits before dispatch.
  - Live gate checkpoint: create transaction commits, then the dispatcher CAS + named-task creation follow.
- [x] Confirm the observation is dispatched through the reconciliation Cloud Tasks queue.
  - Live: every reconciliation ran via `commitmentos-reconciliation` → OIDC → `/internal/tasks/reconcile-observation`.
- [x] Confirm the task name includes observation ID, workflow version, and dispatch generation.
  - `TaskNameFactory.reconciliation` (canonical hash of all three); unit-tested; live task `reconcile-dbfbb643…` recorded in the gate log.
- [x] Confirm the task payload contains no source body, token, Calendar body, or prompt.
  - Payload is the `ReconcileObservationTaskV1` reference tuple only.
- [x] Confirm normalized observations are never published through Pub/Sub.
  - No Pub/Sub client exists in the application layer (invariant scan); dispatcher is the only observation transport.
- [x] Confirm a failed reconciliation enqueue remains discoverable and repairable.
  - Fault-injected: commit + enqueue failure leaves `queued` state; `RunMaintenance.dispatch_pending` recreates the same named task (`test_crash_gap_repaired_by_maintenance`). Deployed maintenance route live behind scheduler OIDC.

### Outbox and dispatcher

- [x] Confirm the periodic dispatcher automatically repairs a sync request or outbox record committed without its Cloud Task.
  - Repair mechanism proven by fault injection (observations and outbox); deployed at `/internal/scheduler/maintenance/dispatch_pending`. Note: the Cloud Scheduler *job* invoking it periodically is created with Phase 2 ops wiring.
- [x] Persist the authoritative observed `etag` on patch/cancel outbox intent.
  - `CalendarMutation.expected_observed_event_etag` persisted immutably on intent; executor preflight compares it and the writer sends it as `If-Match`. (The Phase 4 `calendar_event_snapshots` source of authority replaces the Phase 1 provider-read etag.)
- [x] Confirm a 412 transitions the outbox record to `stale_precondition`, records safe activity, and commits or coalesces a Calendar sync request.
  - Integration-proven (`test_412_marks_stale_precondition_without_action_result`); provider-412 behavior proven live in Phase 0 §6.
- [x] Confirm no `action_result` observation is emitted for a 412.
  - Same test: observation count unchanged; task acknowledged.
- [x] Confirm reconciliation resumes only from independently synchronized Calendar truth after a 412.
  - Closed locally and live in Phase 4B: `test_forced_412_resynchronizes_snapshot_etag_and_resumes_intent` proves the state machine; the dedicated live run on `commitmentos-00050-qar` forced Google HTTP 412, terminally marked `9ecb37e3d549…`, emitted no `action_result`, independently synchronized the new etag, wrote `de7166055ad7…`, and completed the preserved repair. See `docs/phase4_evidence/phase4ab_gate_run.md`.

### Leases and fencing

- [x] Confirm a stale reconciliation fencing token cannot commit an outcome.
  - Every outcome commit passes the expected fencing token to the observation CAS, which rejects a mismatch after takeover (guard exercised by every integration reconcile). Explicit worker-kill/takeover fault injection landed 2026-08-14 in `tests/fault_injection/test_fault_injection_matrix.py`: one retry takes over after lease expiry with a bumped fence, and the late original worker's outcome commit cannot land (Phase 5A).
- [x] Confirm a stale fenced lease cannot checkpoint or publish after takeover.
  - Was open by design until generation machinery existed; closed by Phase 2 D2 locally (`test_stale_fencing_token_cannot_checkpoint_or_publish` — fence checked before status on publish — and `test_worker_death_resumes_from_durable_checkpoint` with `adopt_fence` takeover) and exercised live by the Phase 4A exit's fenced barrier/publication proofs (`docs/phase4_evidence/phase4ab_gate_run.md`). Row reconciled 2026-08-14; explicit worker-kill/takeover fault injection stays in the Phase 5 matrix per the row above.

### Session mutations

- [x] Confirm mutation routes reject a missing or invalid CSRF token (approvals are the first mutation surface).
  - Contract tests: missing and invalid CSRF ⇒ 403 with byte-identical durable state; session and CSRF resolve as dependencies before body validation (auth-ordering regression pinned after the live 422 finding). Live probes on `commitmentos-00013-tw2` confirm the rejection order.

## D2. Phase 2 gate additions — Gmail bounded synchronization generations

All rows proven 2026-08-13 against the production command stack over the
in-memory Firestore twin (`backend/tests/integration/test_phase2_sync.py`;
status/deviations in `docs/phase2_progress.md`). The deployed live-thread
gate run remains (see phase2_progress "Deployment requirements").

- [x] Create a deterministic Gmail fixture or provider response requiring two pages. — `script_two_page_history` (5 relevant messages + 1 filtered draft across 2 pages)
- [x] Create one sync generation from the current published cursor revision. — adopted the spike-shaped cursor at revision 0 (`test_two_page_generation_stages_applies_and_publishes_once`)
- [x] Acquire and record the fenced source lease. — per-user `source-sync:gmail:{user}` lease; fence recorded on the generation document
- [x] Stage page 1 using deterministic generation-item IDs; record item count and manifest hash. — `SyncIdFactory.item_id` over (generation, external_id, external_version)
- [x] Confirm the published source cursor remains unchanged after page 1. — `test_published_cursor_unchanged_after_page_one`
- [x] Dispatch page 2 with a new page sequence; stage it and record its checkpoint. — continuation task `page_sequence=2`; checkpoint advances aggregates
- [x] Store the candidate next cursor without publishing it. — candidate stored on the generation at the final page only
- [x] Confirm a retry of either page reuses deterministic item IDs. — `test_page_retry_reuses_deterministic_item_ids` (zero restaged items)
- [x] Apply staged items in bounded transactions; verify each apply checkpoint uses the current fencing token. — apply chunks are fenced transactions; repo verifies the generation-recorded token
- [x] Verify staged and applied aggregate counts and manifest hashes match. — commutative XOR manifests make the check chunk-boundary independent
- [x] Publish the generation in one final fenced transaction; confirm the candidate cursor becomes authoritative exactly once. — cursor revision 0→1, barrier cleared, redelivered publication converges
- [x] Simulate worker death mid-generation and confirm a stale fencing token cannot checkpoint or publish. — `test_worker_death_resumes_from_durable_checkpoint` + `test_stale_fencing_token_cannot_checkpoint_or_publish` (fence checked before status on publish)

## D3. Phase 4 gate additions — Calendar generations, barrier, and executor integration

- [x] Run the two-page bounded generation protocol against Calendar with an unpromoted candidate sync token. — local: `test_two_pages_hold_candidate_token_until_final_publication`; live: 11 changes at page size 10 produced two pages, with 10 staged after page 1 and a byte-identical published cursor until final publication
- [x] Enter applying state and activate the publication barrier. — Calendar uses the shared fenced `begin_apply` transaction; snapshot reduction requires the generation-owned barrier
- [x] Confirm planner publication is ineligible while the barrier is active. — snapshot loader refuses applying/full-resync cursor state; local test plus live `503 workflow_exception` with zero published run on `commitmentos-00050-qar`
- [x] Confirm Calendar executor preflight is ineligible while the Calendar barrier is active. — both initial and final preflight return retryable `calendar_truth_ineligible`; live barrier delivery returned that code with zero Calendar I/O
- [x] Complete full-sync tombstoning when exercising a full Calendar replacement generation. — absent old events become manifest-covered synthetic tombstone items; readiness/publication require `full_sync_tombstones_complete`
- [x] Confirm `calendar_state_revision` increments exactly once per Calendar publication and the barrier clears. — local initial/replay/full-replacement tests plus live cursor/state `62→63`, equal staged/applied manifests, promoted candidate, and cleared barrier
- [x] Confirm the executor sends the outbox's authoritative observed etag as `If-Match` for every patch and cancellation. — repair publication reads `observed_event_etag` only from `calendar_event_snapshots`; executor preflight requires the snapshot and exact etag match before I/O; patch/cancel writers pass that immutable value as `If-Match` (`test_environmental_conflict_moves_one_block_and_preserves_the_rest` plus Phase 1 cancellation coverage)
- [x] Confirm the executor's forced 412 follows the full stale-precondition path end to end: no overwrite, no blind retry, one durable sync request, no `action_result`. — local regression plus dedicated live proof: old intent `stale_precondition`, old/new etag hashes differ, new synchronized intent succeeds, provider lands at the preserved interval (`docs/phase4_evidence/phase4ab_gate_run.md`)

## D4. Phase 5 gate additions — security hardening

**Local proofs landed 2026-08-14 (Phase 5A)** — every row below is
implemented and proven against the production stack over the in-memory twin
(`docs/phase5a_progress.md`); the checkboxes close at the 5B gate when the
same probes run against the deployed revision, per the 5B exit rule.

### Full session negative matrix

Local proof: `backend/tests/contract/test_auth_contracts.py` (16 tests) over
the production `AuthRouter`, which replaced the spike login as the session
issuer (no spike module remains mounted).

- [x] Start login only through an allowlisted redirect target. — live probes: three non-allowlisted targets rejected HTTP 400; `/app` target 302s to Google (`security_probes_20260814t160344.json`)
- [x] Reject missing, mismatched, expired, and replayed state. — live: missing state 400; unknown/replayed state 403
- [x] Reject a mismatched nonce. — local contract suite; the live callback path enforces the same single-use CAS transaction the probes exercised
- [x] Confirm callback replay cannot create a second session. — live: replayed state 403 with no session
- [x] Confirm logout revokes the current session. — live: logout without CSRF 403 and session survives; with CSRF the session is revoked and rejected afterward
- [x] Enforce session expiry and revocation. — live: revoked session rejected; unauthenticated read 401

### Full CSRF suite

Local proof: `TestFullCsrfSuite` in
`backend/tests/contract/test_route_contracts.py` — parametrized over every
controlled mutation route including the new completion route; byte-identical
durable state and zero dispatches on every rejection; auth resolves before
body validation.

- [x] Confirm every controlled mutation route rejects missing and invalid CSRF tokens with zero side effects. — live probes on all five controlled mutation routes (approvals resolve, controls change, check-in, undo, complete): missing session 401, missing CSRF 403, invalid CSRF 403 (`security_probes_20260814t160344.json`)

### Webhook hardening

Local proof: `backend/tests/contract/test_webhook_rate_limit.py` — 429 over
limit with zero side effects, durable across a process restart, window
recovery, and invalid probes unable to exhaust the valid-signal budget.

- [x] Enforce the durable per-channel rate limit. — live on every webhook delivery through the 2026-08-15 golden campaign (the limiter sits on the validated live path); restart durability proven in `backend/tests/contract/test_webhook_rate_limit.py`
- [ ] Test a request exceeding the valid-signal rate limit and confirm zero side effects. — documented owner-run step (deliberately excluded from automated probes to avoid tripping the durable limiter outside a gate window); unit + restart coverage in `test_webhook_rate_limit.py`

### Full demo mutation matrix

Local proof: `TestFullDemoMutationMatrix` in
`backend/tests/contract/test_route_contracts.py` — the matrix enumerates
every mounted mutation method/path from the live route table; the demo
surface is a separate static read model with no Firestore/credential path.

- [x] Confirm the demo client is separate from the live API client. — structural separation asserted in the local matrix; live demo reads serve only seeded data
- [x] Attempt every production mutation method/path under `/demo`. — live: POST/PUT/PATCH/DELETE across every production mutation path (`security_probes_20260814t160344.json`)
- [x] Confirm every demo mutation attempt is rejected. — live: 403 on every attempt
- [x] Confirm rejected demo mutations cause zero Firestore writes, zero task dispatches, and zero OAuth, Gmail, or Calendar calls. — zero-side-effect assertions in the probe run; the demo read model has no Firestore/credential path by construction
- [x] Confirm seeded mode exposes no live mutation controls. — live seeded reads verified

### Audited controlled cleanup

Local proof: `backend/tests/integration/test_phase5a_cleanup.py` over
`CleanupControlledAccount` + `scripts/reset_controlled_account.py`
(preview → typed confirmation phrase → drift-guarded execute; snapshot-etag
`If-Match` cancels; audit-timeline record; 5B's between-runs reset).

- [x] Complete authenticated developer cleanup as a documented command. — executed live ~12 times as the golden campaign's between-runs reset (`scripts/reset_controlled_account.py` machinery via the driver; preview → typed phrase → drift-guarded execute, incl. two live drift-guard aborts that correctly refused to run on changed state)
- [x] Preview cleanup targets before mutation; confirm only events with valid CommitmentOS ownership properties are targeted. — live: only recorded app-owned work-block events canceled each reset
- [x] Confirm unrelated Calendar events remain unchanged. — live: the three campaign busy fixtures survived every reset unchanged
- [x] Confirm cleanup activity is recorded in the audit timeline. — `CONTROLLED_CLEANUP_COMPLETED` activity per reset

---

## Result summary

| Workstream | Status | Evidence | Blocker or note |
|---|---|---|---|
| Frozen decisions | Complete except owner sign-offs | Checklist §1 inline | Golden-scenario freeze + self-review timestamps pending |
| Cloud foundation and minimal service | Complete | Checklist §2 inline | |
| OAuth mode decision spike | Complete — selection FINAL | §3 inline + `s08` | |
| Gmail watch delivery | Complete (one row → B1) | `s04_gmail_watch_trace.md` | Crash-gap fault injection with Phase 1 dispatcher |
| Calendar watch and webhook boundary | Complete | `s05_calendar_webhook_trace.md` | |
| Calendar API behavior and forced 412 | Complete (10/10 first run) | `s06_calendar_identity_412.md` | |
| Gemini and ADK | Complete | `s07_gemini_adk_proof.md` | |
| Reauthorization | Complete | `s08_reauthorization_cycle.md` | |
| Trust contracts (minimal) | Complete | `s09_trust_contracts.md` | |
| Evidence pack | Complete (B2 screenshots pending) | `docs/phase0_evidence/` + `s00_spike_run_log.md` | |

## Decision log

| Timestamp | Decision | Evidence | Owner |
|---|---|---|---|
| 2026-08-10 | Golden scenario assembled as `golden_scenario_rev_1`; sign-off pending step-away self-review | Checklist §1, Golden scenario block | Project owner |
| 2026-08-12 | P0 OAuth publishing mode selected: External / In production / unverified personal use (provisional pending watch-renewal + allowlist rows; Testing fallback fully proven incl. 7-day runbook) | Section 3 spike outputs; grant-wide revocation finding | Project owner |
| 2026-08-12 | OAuth mode selection made FINAL: watch renewal (S4–5) and application allowlist (S9) both confirmed live | Evidence files s04, s05, s09 | Project owner |
| 2026-08-12 | **Phase 1 gate closed**: deployed seeded slice 18/18 checkpoints incl. live pause-proof (run tag `20260812t152758`, revision `commitmentos-00013-tw2`); D1 rows above; B1 + B3 closed; log-redaction row closed | `docs/phase1_progress.md`; gate-run transcript in session log; 53 local tests | Project owner + build session |
| 2026-08-13 | **Phase 2 built + D2 rows locally proven**: bounded Gmail staging generations, `extraction_v2` Gemini interpretation + deterministic validation + identity resolution, ADK Workflow wrapper live in production path, spike Gmail routes replaced, candidate dashboard reads; prompt/schema pins bumped to `commitment_interpretation_v2`/`extraction_v2`. Deployed live-thread gate run + §16.1 eval remain (steps in phase2_progress) | `docs/phase2_progress.md`; 81 local tests; D2 rows above | Build session |
| 2026-08-13 | **Phase 2 gate CLOSED (live)**: real 3-message thread through revision `commitmentos-00018-qxx` — request+acceptance → one commitment (my_commitment, awaiting_confirmation, Friday 16:00 PT, effort approval); deadline-change reply → `update_existing` revision 2, Thursday 16:00 PT, in a fully autonomous cycle (cursor rev 1→2); replay of all 3 thread observations → 200 `no_op` ×3 with byte-identical durable state. §16.1 eval: run 1 exposed a delimiter-escape injection gap (fixed + regression-tested) and scoring artifacts; corrected run 2 = 100% on all metrics ($0.0008/msg). Two live-only adapter bugs found and fixed with tests: Gmail `historyTypes` enum (`messageAdded` singular) and 404 on vanished draft messages. Redaction + body-content log scans clean. Unrelated real mail processed with zero false commitments; payment-request emails routed to `identity_confirmation` | `docs/phase2_progress.md` gate-closure record; `docs/phase2_evidence/extraction_eval_*.json`; 84 local tests | Project owner + build session |
| 2026-08-13 | **Phase 3 gate CLOSED (live)**: 3A+3B deployed (`commitmentos-00020-lgf`, then `00021-4l5` with the gate-day fix); `work_blocks` index READY. Production busy read normalized timed/recurring/all-day holds and excluded a transparent event. Live finding fixed: effort-approval reissue on commitment revision was missing (golden audit step 3) — a stale resolve returned `approval_superseded` and left the real commitment stuck; `_ensure_effort_confirmation_requested` now restores the pending-confirmation invariant on every update-path exit (2 regression tests; 115 green), and a real restatement reply repaired the live commitment through the full autonomous pipeline (~15 s). Effort 180 + plan approval via guarded session/CSRF routes → planner run `42babb59…` (3 constraint-safe intervals around real busy time) → 3 real Calendar events with stable IDs. Second commitment (seeded, 120 min) → portfolio run `58fd5731…` with both commitments, preserved blocks counted once, Fri/Sat allocations, zero double-allocated minutes. Guarded check-in: one evidence record, one revision advance, identical redelivery `no_op`. Replays: 8 observations + 5 calendar actions all `200 no_op`, state digest (incl. live event etags) byte-identical. Log scan clean. Stand-ins recorded: minted session (browser login), driver elapse transition (Phase 4 scan), seeded second commitment | `docs/phase3_evidence/phase3_gate_run.md`; `docs/phase3a_progress.md`; `docs/phase3b_progress.md` | Project owner + build session |
| 2026-08-14 | **Phase 4C locally complete; official Phase 4 gate pending live**: periodic safety now subsumes the Phase 3 elapse stand-in and drives `planned → active → awaiting_check_in` without changing verified minutes; projection/overdue refresh, snapshot-drift resync, delayed-generation recovery, and stale-run cleanup are bounded and retry-safe. Today/activity/system-status reads expose failure truth. A real-webhook integration proves one disruption → one patch → one ignored app echo with a complete before/after/risk explanation and warmed latency instrumentation. One-minute Scheduler, reversible min-instance script, and arm/verify live driver are ready; existing five owned events and `daf9a729…` remain the live fixtures. 143 tests green. | `docs/phase4c_progress.md`; `backend/tests/integration/test_phase4c_always_on_safety.py`; `scripts/run_phase4c_gate.py` | Build session; live gate owner action pending |
| 2026-08-14 | **Phase 4C deployed; automated live pre-gate CLOSED**: revision `commitmentos-00036-puj` serves 100%; 15 indexes READY; one-minute safety Scheduler returns 200. A renewed real Calendar watch bootstrapped the authoritative snapshot through one fenced full-resync publication (cursor/state revision 1, barrier clear); source-sync and reconciliation queues converged empty. Live deployment exposed and fixed four migration/provider-contract gaps with tests: timestamp-less legacy cursor freshness, handshake-only bootstrap, legacy-token full-resync selection, and sync-token-ineligible `orderBy`. Two elapsed blocks are `awaiting_check_in` at zero verified minutes; `daf9a729…` is durably `in_progress`/`overdue` with 120 remaining minutes. 147 tests green; Ruff and changed-file mypy clean. The official meeting-over-owned-block gate remains pending owner-observed insertion, warmed latency, one minimal repair, and one ignored echo. | `docs/phase4c_progress.md`; Cloud Run/Scheduler/Tasks and masked Firestore live checks; `backend/tests/integration/test_phase4a_calendar_truth.py`; `backend/tests/integration/test_phase4c_always_on_safety.py` | Project owner + build session; manual gate action pending |
| 2026-08-14 | **Phase 4 gate CLOSED (live)**: meeting-over-owned-block run passed 17/17 verify checkpoints on revision `commitmentos-00031-rsz`; one minimal 60-minute repair via `If-Match` with stable event identity, 4/4 unaffected blocks byte-preserved, complete before/after/risk-arc explanation, echo terminally ignored, **8.293 s** warmed insert-to-repair (budget 15 s), clean log scan. Run 1 exposed a live-only policy bug — unrelated overdue `daf9a729…` made every portfolio plan infeasible and escalated the in-policy repair to `action_approval` (`repair_infeasible`), with the approval-resume path looping on the same check. Fixed: `_repair_blocking_infeasibility` escalates only on unplaced blocks, immutable conflict, or future-deadline shortfall; `immutable_conflict` recorded in the `_repair` audit; regression `test_unrelated_overdue_commitment_does_not_block_automatic_repair`; 148 tests green, Ruff + targeted mypy clean. Deviation: conflict inserted/removed via guarded Calendar-API scripts at owner request (demo video will use the UI). Pre-fix approval `19b1acfb…` retained pending as audit history (expires 08-21). | `docs/phase4_evidence/phase4_gate_run.md`; `docs/phase4c_progress.md` gate-closure record | Project owner + build session |
| 2026-08-14 | **Phase 4A + 4B dedicated live exits CLOSED**: guarded run passed 65/65 on `commitmentos-00050-qar`. 4A: exact two-page Calendar generation, page-1 cursor/token non-promotion, real planner/executor barrier refusal, one final revision advance, planner revision/hash byte-identical to snapshot store. 4B: explicit invalid-move restore intent from snapshot etag, real Google 412 → terminal stale intent/no `action_result` → one sync → new-etag successful intent; subsequent valid owned move adopted with one explanation and zero outbox delta. Two live product defects found and fixed with regressions: guarded approval route dropped Calendar `choice`; `restore_approved_slot` emitted no action. Isolated fixture removed. Production normalized on `commitmentos-00052-did` (page 250, chunk 100, probe 0), queues RUNNING/cursor eligible, 151 tests green. | `docs/phase4_evidence/phase4ab_gate_run.md`; `docs/phase4a_progress.md`; `docs/phase4b_progress.md` | Project owner + build session |
| 2026-08-14 | **Phase 5A local exit CLOSED**: `CompleteCommitment` + guarded route with the §4.5 terminal invariant (completion closes pending check-in requests, cancels leftover planned blocks via snapshot-etag `If-Match` intents, and removes the commitment from the demand set); production `AuthRouter` replaces the spike login (state/nonce/PKCE single-use CAS, allowlisted redirect targets, logout revocation) and the last spike modules left the live path (route inventory re-audited, §13.5); full D4 local proofs (session negative matrix, parametrized CSRF suite over every controlled mutation route, self-enumerating demo mutation matrix, durable webhook rate-limit negatives, audited cleanup command doubling as the 5B reset); §16.4 fault matrix landed in `tests/fault_injection` incl. the open D1 worker-kill/takeover row, executor death before/after the Calendar response, §9.4 create-before-record convergence, and projection-corruption blocking. **220 tests green (baseline 151), Ruff clean, targeted mypy clean; golden dry-run reaches `completed` with honest verified minutes.** Live D4 probes + golden campaign are 5B. | `docs/phase5a_progress.md`; `backend/tests/integration/test_phase5a_completion.py`, `test_phase5a_cleanup.py`; `backend/tests/contract/test_auth_contracts.py`, `test_webhook_rate_limit.py`, `test_route_contracts.py`; `tests/fault_injection/` | Build session |
| 2026-08-15 | **Phase 5B campaign complete; Phase 5 gate at owner sign-off**: TEN consecutive passing golden runs on revision `commitmentos-00042-fcj` (thread mode, live Gemini per run; 61/61 checkpoints each; conflict-to-repair 7.2–10.2 s, mean 9.1 s, inside the 15 s warmed budget on scale-to-zero; honest 120/180 verified minutes; frozen 18-step audit order; full replay byte-identical every run). D4 rows flipped with live evidence (`security_probes_20260814t160344.json` all-green); rate-limit exceedance remains the documented owner-run step. Campaign surfaced and same-day-fixed one product defect — executor adopted a cancelled corpse event as success; `insert_or_adopt_owned` now revives owned corpses via `events.update` + `If-Match` (3 regression tests; fake calendar aligned to Google's ID-reservation semantics; suite 224 green) — plus four environment/harness findings (watch-channel burst throttling → settle+probe discipline; Cloud Tasks 24 h name retention vs deterministic IDs → direct OIDC delivery through the replay path; stale Phase 3 busy fixtures forcing a correct >24 h policy escalation → fixtures removed; two driver races → poll/retry). One reset drift-abort split the campaign into runs 1–2 + 3–10 with no failed run (owner adjudication note in the summary). Full record: `docs/phase5_evidence/phase5_gate_summary.md` | Ten `golden_run_*_20260815t0*.json` files + `phase5_gate_summary.md` + `security_probes_20260814t160344.json` | Build session; owner sign-off + rate-limit step pending |
| TBD | TBD | TBD | TBD |

## Blocker log

| ID | Discovered | Blocker | Impacted gate | Next action | Status |
|---|---|---|---|---|---|
| B1 | 2026-08-12 | Section 4 crash-gap repair (sync request committed, task creation fails) not fault-injected | Part II D1 (Phase 1 gate) | Fault-injected and repaired in integration test (`test_crash_gap_repaired_by_maintenance` via `RunMaintenance.dispatch_pending`); repair route deployed live behind scheduler OIDC in `commitmentos-00013-tw2` | **Closed 2026-08-12** (Phase 1 gate) |
| B2 | 2026-08-12 | Owner consent-warning screenshots (per scope class, both modes) not yet in the evidence pack | §3 observation rows | Owner captures sanitized screenshots to `docs/phase0_evidence/` | Open — owner action |
| B3 | 2026-08-12 | Redacting logging middleware not yet implemented (current logs carry no headers/bodies by default) | §2 log-redaction row | Implemented (`bootstrap/logging.py` `SensitiveDataRedactor`); deployed Cloud Logging scan over the live gate window found zero credential/content leakage; §2 row checked | **Closed 2026-08-12** (Phase 1 gate) |
