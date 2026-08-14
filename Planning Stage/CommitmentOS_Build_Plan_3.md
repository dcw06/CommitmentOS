# CommitmentOS Build Plan 3

## All Things Agentic Hackathon — Implementation-Ready Competition Plan

**Version:** 3.0  
**Plan date:** August 10, 2026  
**Working product name:** CommitmentOS  
**Primary track:** The Taskmaster  
**Secondary award target:** Best Architectural Design  
**Primary persona:** A person managing deadline-driven project work through Gmail and Google Calendar  
**P0 evidence sources:** Gmail and Google Calendar only  
**Pinned model:** Gemini 3.5 Flash (`gemini-3.5-flash`)  
**Agent framework:** Google Agent Development Kit 2.x, Python graph workflow  
**Primary runtime:** Python on Cloud Run  
**Durable application state:** Firestore-owned domain state and event records  
**Execution rule:** Every cloud invocation is bounded; human approval resumes through a new durable observation  

## 1. Executive Decision

Build CommitmentOS as an evidence-backed, capacity-aware commitment controller—not as an inbox summarizer, chatbot, or generic personal assistant.

The complete P0 product is one closed loop:

> Detect a commitment in Gmail → preserve its evidence and ownership → confirm effort → reserve Calendar capacity → observe a conflict → reconcile and minimally repair the plan → explain the action → verify completion.

The Reconciliation Engine is the center of the product. Gemini 3.5 Flash interprets ambiguous human language and produces structured proposals. Deterministic services retain authority over identity resolution, state transitions, progress, risk, scheduling constraints, action policy, idempotency, and Calendar mutations.

Every reconciliation run is a bounded cloud invocation. When user input is required, the run writes an approval or confirmation record and terminates safely. The dashboard response becomes a new immutable observation that starts a fresh run from durable Firestore state. P0 does not depend on an in-memory Cloud Run process remaining alive while a person responds.

P0 does not require Canvas, dependency graphs, external follow-ups, Drive, multimodal input, or a general chat interface. Those features begin only after the Gmail-to-Calendar loop passes the reliability gate in Section 16.

## 2. Product Thesis and Competitive Boundary

### 2.1 Core promise

Turn scattered promises into continuously managed execution plans.

### 2.2 Positioning

Google Calendar shows when events happen. Gmail and Gemini can summarize information and surface action items. CommitmentOS manages the longitudinal state between a promise and its verified completion.

Its differentiation comes from the combination of:

- A durable commitment ledger rather than a transient summary
- Source-linked evidence and explicit ownership
- Confirmed effort and remaining-work state
- Capacity-aware scheduling before a deadline
- Persistent observation of real-world changes
- Minimal-change automatic plan repair
- Separate feasibility and dependency state
- Policy-controlled actions with undo
- Completion evidence instead of assuming elapsed time means success
- A decision timeline explaining every mutation

### 2.3 Product boundary

CommitmentOS will not spend P0 time on:

- Generic Gmail summaries
- A chatbot as the primary interface
- Daily briefs without actions
- Simple “create a Calendar event” behavior
- Meeting-time suggestions
- Content generation unrelated to commitment completion
- Broad Workspace automation
- Autonomous external messaging

### 2.4 P0 user scenario

The initial user is someone who makes deadline-bound promises over email and must fit the work around an already busy Calendar. The same foundation can later serve students through Canvas and teams through shared dependencies, but those extensions do not define the initial product.

## 3. Product Outcomes

CommitmentOS should answer six questions for every active commitment:

1. What outcome was promised?
2. Who owns it and who benefits from it?
3. What source evidence supports the inference?
4. How much work remains before the deadline?
5. Is the commitment still achievable under the current Calendar?
6. What evidence shows that it was completed?

The product succeeds only when it manages all six questions as durable state.

For competition evidence, the dashboard should also expose a small outcome strip derived from audit data:

- Active commitments kept feasible
- Work minutes reserved before deadlines
- Conflicts detected and repaired automatically
- Unaffected Calendar blocks preserved during repair
- Duplicate external actions prevented during replay tests

These are proof-of-operation metrics, not speculative productivity claims.

## 4. Commitment Lifecycle

### 4.1 Lifecycle states

```text
candidate
    ↓
awaiting_confirmation
    ↓
active
    ↓
in_progress
    ↓
completion_candidate
    ↓
completed
```

Side states are `paused`, `dismissed`, and `canceled`. These lifecycle states are separate from risk and blocking state.

### 4.2 Feasibility risk

```text
unknown | on_track | at_risk | critical | overdue
```

### 4.3 Blocking state

```text
clear | waiting | blocked
```

Separating these dimensions prevents loss of information. A later P1 commitment can be both `blocked` by another person and `critical` because insufficient time remains.

### 4.4 Work-block execution state

```text
planned → active → awaiting_check_in → completed
                  └───────────────→ missed
planned ──────────────────────────→ canceled
```

Calendar time passing is not proof that work happened. A block contributes to `verified_completed_minutes` only after explicit user confirmation or qualifying completion evidence. An elapsed unconfirmed block becomes `awaiting_check_in`, and a skipped block becomes `missed`; both trigger reconciliation without silently reducing remaining effort.

### 4.5 Desired versus actual state

For each commitment, the Reconciliation Engine maintains:

- **Desired state:** confirmed deadline, remaining effort, policy, and valid work blocks required before the deadline
- **Actual state:** current Gmail evidence, Calendar events, verified work progress, work-block status, approvals, completion evidence, and synchronization state

Reconciliation compares the two. It acts only when a meaningful difference exists and records the exact before-and-after state.

## 5. Primary Autonomous Loop

```text
Observe Gmail or Calendar change
              ↓
Normalize it into a source observation
              ↓
Interpret ambiguous content with Gemini
              ↓
Validate structured output in deterministic code
              ↓
Resolve identity and persist evidence
              ↓
Compare actual state with desired state
              ↓
Calculate feasibility and blocking state
              ↓
Produce a stable scheduling proposal
              ↓
Apply the autonomy policy
        ┌─────┴─────┐
        ↓           ↓
     Execute     Persist input request
        ↓           ↓
 Record outcome   End bounded run
        ↓
Observe the result again ↻

User response → immutable observation → new bounded run ↗
```

This is an event-driven workflow, not an unbounded LLM loop or a long-lived web request. Every run has an input observation, a bounded set of nodes, deterministic termination, and a durable outcome. Firestore is the source of truth between runs; ADK expresses the control flow inside each run.

## 6. Locked P0 Scope

### 6.1 P0 capabilities

| Capability | P0 acceptance condition |
|---|---|
| Gmail ingestion | New and sent-message changes reach the backend and can recover from missed notifications |
| Structured interpretation | Gemini returns schema-valid commitment proposals with source evidence |
| Ownership | The system distinguishes my commitment, request to me, commitment to me, and ambiguous language |
| Commitment identity | Thread updates modify or supersede the correct commitment without silent duplication |
| Effort confirmation | The user confirms or edits the proposed effort before the first Calendar plan |
| Calendar capacity | Busy time, working hours, minimum block length, and daily limits are respected |
| Deterministic scheduling | Required work is split into valid app-owned blocks before the deadline |
| Verified progress | Elapsed time never reduces remaining effort without explicit progress or completion evidence |
| Continuous reconciliation | A moved or newly added event triggers a new state comparison |
| Stable repair | Only the minimum necessary future CommitmentOS blocks move |
| Risk | Remaining effort and usable capacity produce a reproducible risk result |
| Audit | Every decision and action records its reason, policy, and outcome |
| Completion | Manual confirmation or qualifying sent-email evidence can close a commitment |
| Cloud deployment | The full loop runs on Google Cloud and is visible in the demo |

### 6.2 P0 hard gate

Do not begin P1 until the exact Gmail-only competition scenario runs successfully ten consecutive times with:

- No duplicate commitments
- No duplicate Calendar blocks
- No hard scheduling violations
- No manual repair outside intentional effort and first-plan confirmation
- A complete audit record for every Calendar mutation
- Safe recovery from replayed source events
- Safe continuation after a Cloud Run instance is recycled between input request and user response

### 6.3 P1 — commitment semantics and a second source

Only after the P0 gate:

- Canvas assignment adapter using a controlled test account or personal access token
- Dependency-edge behavior
- External owner state
- Source deadline changes
- Dependency-driven blocked state
- Contextual follow-up drafts with explicit approval
- Canvas submission evidence

### 6.4 P2 — breadth and learning

- Google Drive completion evidence
- Adaptive effort estimation from historical outcomes
- Outlook and multiple-account support
- Shared commitments and team views
- Rich investigation interface
- What-if planning
- Voice or multimodal approvals
- Weekly reliability reports

## 7. Locked Technical Stack

### 7.1 Model

- Gemini 3.5 Flash
- Stable model ID: `gemini-3.5-flash`
- Accessed through the Gemini API for the hackathon implementation
- Structured outputs enabled for commitment extraction
- Low thinking level for routine extraction; increase only for explicitly ambiguous cases if evaluation shows a material benefit
- No direct Calendar or Gmail write tools exposed to the extraction agent

The model version must be centralized in configuration and written into every model-backed audit event.

### 7.2 Agent framework

- Google ADK 2.x for Python
- Graph workflow for explicit nodes, branching, and typed state inside bounded invocations
- Deterministic function nodes for validation, risk, scheduling, and policy
- Gemini-backed agent node only where human-language interpretation is required
- Application-level input requests persisted in Firestore rather than an invocation waiting in memory

ADK session history may be retained for tracing, but it is not the authoritative commitment store. P0 does not require a persistent ADK `SessionService`. If native ADK `RequestInput` is added later, the implementation must first configure a persistent session service, resumability, invocation ID storage, and replay-safe tools.

### 7.3 Application stack

- Python and FastAPI backend on Cloud Run
- React/TypeScript web dashboard, deployed with the simplest reproducible hosting path available
- Firestore for authoritative application state, observations, approvals, audit events, synchronization cursors, and per-user processing leases
- Pub/Sub for Gmail notifications and normalized event delivery
- Cloud Tasks for named idempotent retries, delayed execution, and serialized per-user synchronization
- Cloud Scheduler for watch renewal, cursor catch-up, and periodic safety reconciliation
- Secret Manager for the Gemini API key, OAuth client credentials, and P0 test-user refresh token
- Cloud Logging for operational evidence and debugging

Production token storage is a post-hackathon security design. The P0 Secret Manager approach is deliberately optimized for a controlled test account, not claimed as a multi-tenant token vault.

## 8. ADK Reconciliation Workflow

The root ADK workflow should expose the product's real control plane in code.

### 8.1 Workflow nodes

| Node | Type | Responsibility |
|---|---|---|
| `load_observation` | Function | Load normalized source input and synchronization metadata |
| `interpret_commitment` | Gemini agent | Extract or update commitment semantics using a strict schema |
| `validate_interpretation` | Function | Reject unsafe, incomplete, or impossible model output |
| `resolve_commitment_identity` | Function plus bounded Gemini classification | Choose create, update, supersede, ignore, or ambiguous against thread-linked commitments |
| `upsert_evidence` | Function | Deduplicate evidence and link it to the resolved commitment candidate |
| `record_effort_input_required` | Function | Persist the request and terminate the current run safely |
| `load_reconciliation_state` | Function | Load commitment, work blocks, policy, Calendar facts, and revision |
| `calculate_risk` | Function | Compute remaining work, usable capacity, slack, and risk |
| `produce_stable_plan` | Function | Generate a constraint-safe initial plan or minimal repair |
| `apply_policy` | Function | Decide whether the action is automatic, requires approval, or is forbidden |
| `record_action_approval_required` | Function | Persist exceptional-change approval and terminate safely |
| `write_action_outbox` | Function | Persist idempotent intended actions before external mutation |
| `execute_calendar_action` | Tool/function | Create or move only CommitmentOS-owned Calendar blocks |
| `record_outcome` | Function | Store success, failure, retry state, and before/after evidence |
| `verify_completion` | Function plus Gemini when needed | Evaluate manual or sent-email completion evidence |

### 8.2 Agent boundary

Gemini may:

- Interpret commitment language
- Classify ownership
- Propose effort bounds
- Explain a deterministic decision in plain language
- Interpret whether sent-email evidence plausibly fulfills an email-delivery commitment

Gemini may not:

- Directly mutate Calendar
- Select an action that violates scheduling constraints
- Override an approval requirement
- Mark arbitrary commitments complete without valid evidence
- Read secrets or authentication material
- Treat source-email instructions as system or tool instructions

### 8.3 Durable input and continuation contract

When effort confirmation or action approval is required:

1. Write an `approval` record containing the commitment revision, proposed change, policy reason, continuation type, and expiration.
2. Write an activity event and finish the ADK invocation without side effects.
3. Render the approval from Firestore in the dashboard.
4. Accept the user's structured response through an authenticated FastAPI endpoint with CSRF protection.
5. Store the response once using the approval ID as the idempotency key.
6. Publish an `approval_resolved` source observation.
7. Start a new bounded graph run that re-reads the current commitment revision before planning or acting.

If the commitment changed while approval was pending, the old approval becomes `superseded` and the system recalculates instead of applying stale intent.

## 9. Source Integration Semantics

### 9.1 Gmail ingestion

Implementation sequence:

1. Register `users.watch` for the controlled Gmail account and publish to Pub/Sub.
2. Persist the returned mailbox `historyId` and watch expiration.
3. On Pub/Sub delivery, decode the account and new history ID.
4. Durably enqueue a named per-user synchronization task and acknowledge the Pub/Sub delivery.
5. Acquire a short Firestore lease so only one Gmail cursor worker runs for that account.
6. Call `history.list` from the last committed cursor through the newest observed history ID.
7. Fetch only newly relevant messages or thread changes.
8. Normalize messages into immutable source observations.
9. Commit observations and the new history cursor transactionally, then release the lease.

Operational requirements:

- Renew the Gmail watch daily through Cloud Scheduler.
- Filter normalized changes to relevant Inbox and Sent activity.
- Treat Pub/Sub delivery as at-least-once.
- Do not use Pub/Sub message ordering as a cursor correctness mechanism.
- Use a per-user queue or lease so concurrent notifications cannot race the same history cursor.
- Fall back to periodic `history.list` catch-up when no notification arrives.
- Handle an invalid or expired history cursor with a bounded resynchronization.
- Prevent notifications caused by the application from creating loops.

### 9.2 Calendar observation

Implementation sequence:

1. Establish an Events watch channel for the selected Calendar.
2. Receive the HTTPS notification at a Cloud Run webhook.
3. Validate the channel ID and opaque channel token.
4. Durably enqueue a named Calendar synchronization task and return success.
5. Acquire a per-calendar synchronization lease.
6. Use the persisted Calendar sync token to fetch actual event changes.
7. Normalize the changes into observations.
8. Trigger reconciliation only for affected active commitments or capacity windows.
9. Persist the next sync token after a successful incremental sync and release the lease.

Operational requirements:

- Notifications contain change signals, not event bodies; always fetch the changed resources.
- Replace expiring Calendar channels with new unique channel IDs.
- On HTTP `410`, discard the invalid sync cursor and perform a full bounded resync.
- Use a periodic reconciliation pass as a safety net.
- Store the Calendar event `etag` and use revision guards against concurrent updates.

### 9.3 Calendar ownership

Every CommitmentOS-created event must include private extended properties:

```json
{
  "managed_by": "commitmentos",
  "commitment_id": "commitment_123",
  "work_block_id": "block_456",
  "plan_revision": "7"
}
```

The executor may create, move, or cancel only events containing valid CommitmentOS ownership metadata. It must never mutate unrelated user events.

### 9.4 Calendar create idempotency

For every new work block, derive a stable Calendar event ID from the action idempotency key using a base32hex-safe hash. Supply that ID to Calendar during `events.insert` rather than relying on a server-generated ID.

If execution succeeds but the worker crashes before recording the response, a retry must fetch the deterministic event ID or treat an already-existing response as success. Extended properties remain the ownership and audit mechanism; the deterministic event ID closes the create-before-record crash window.

### 9.5 Commitment identity across messages

Before creating a commitment from a new Gmail observation:

1. Load active, candidate, dismissed, and recently completed commitments linked to the Gmail thread.
2. Build a bounded comparison set containing IDs, ownership, normalized outcome, beneficiary, deadline, and supporting evidence references.
3. Ask the interpretation node for structured semantics and a proposed identity operation.
4. Validate the target exists, belongs to the same user, and is compatible with deterministic ownership and thread rules.
5. Apply `create`, `update_existing`, `supersede`, or `ignore`; route `ambiguous` to confirmation.
6. Record the candidate set, proposed operation, final operation, and reason in the audit timeline.

A dismissed source span must not reappear unchanged after later thread activity. Multiple distinct commitments in one message receive stable source-span keys so replay remains idempotent.

## 10. Domain and Persistence Model

### 10.1 Commitment

```json
{
  "commitment_id": "commitment_123",
  "user_id": "user_1",
  "revision": 7,
  "source_thread_id": "thread_123",
  "semantic_fingerprint": "my_commitment:send-revised-proposal:professor-chen",
  "title": "Send revised proposal",
  "description": "Send the revised proposal to Professor Chen",
  "ownership_type": "my_commitment",
  "owner": { "type": "user", "display_name": "Me" },
  "beneficiary": { "display_name": "Professor Chen" },
  "deadline": {
    "value": "2026-08-14T17:00:00-07:00",
    "timezone": "America/Los_Angeles",
    "confidence": 0.93,
    "evidence_id": "evidence_123"
  },
  "effort": {
    "lower_minutes": 120,
    "likely_minutes": 180,
    "upper_minutes": 240,
    "confidence": 0.58,
    "confirmed_minutes": 180,
    "confirmed_at": "2026-08-10T18:00:00Z"
  },
  "verified_completed_minutes": 0,
  "remaining_minutes": 180,
  "lifecycle_status": "active",
  "risk_level": "on_track",
  "blocking_status": "clear",
  "plan_revision": 3,
  "policy_profile": "default_personal",
  "last_reconciled_at": "2026-08-10T18:01:00Z",
  "created_at": "2026-08-10T17:59:00Z",
  "updated_at": "2026-08-10T18:01:00Z"
}
```

`semantic_fingerprint` is a matching aid, not an autonomous merge authority. Thread-linked existing commitments and evidence are always checked before creation. The resolver emits one of `create`, `update_existing`, `supersede`, `ignore`, or `ambiguous`; ambiguous identity never silently merges records.

### 10.2 Work block

```json
{
  "work_block_id": "block_456",
  "commitment_id": "commitment_123",
  "calendar_id": "primary",
  "calendar_event_id": "6k9m2...base32hex",
  "duration_minutes": 60,
  "execution_state": "planned",
  "scheduled_start": "2026-08-11T09:00:00-07:00",
  "scheduled_end": "2026-08-11T10:00:00-07:00",
  "verified_minutes": 0,
  "completion_evidence_id": null,
  "etag": "calendar-etag",
  "plan_revision": 3
}
```

Only `verified_minutes` contributes to commitment progress. Moving or deleting a Calendar event changes the plan, not the amount of completed work.

### 10.3 Supporting collections

| Collection | Required fields or purpose |
|---|---|
| `source_observations` | Source, external ID, external version, observed time, payload hash, idempotency key |
| `evidence` | Commitment ID, source reference, minimal excerpt, confidence, model version, schema version |
| `work_blocks` | Commitment ID, deterministic Calendar event ID, duration, execution state, verified minutes, evidence, etag, plan revision |
| `dependency_edges` | P1 source commitment, target commitment, type, owner, status |
| `approvals` | Request type, commitment revision, payload, continuation type, policy reason, expiration, decision, actor, timestamps |
| `action_outbox` | Intended mutation, deterministic external ID, idempotency key, status, attempts, before/after, error |
| `activity_events` | Human-readable and machine-readable audit timeline |
| `sync_cursors` | Gmail history ID, Calendar sync token, channel IDs, expirations, last success |
| `processing_leases` | User/source key, lease owner, acquired time, expiration, last completed cursor |

### 10.4 Idempotency keys

Suggested forms:

```text
gmail:{user_id}:{message_id}:{extractor_schema_version}
calendar:{calendar_id}:{event_id}:{etag}
action:{commitment_id}:{plan_revision}:{action_type}:{target_id}
approval:{approval_id}:{decision}
```

Bidirectional dependency arrays must not be stored inside a commitment as the primary representation. P1 dependencies belong in an edge collection to avoid stale denormalized graphs.

## 11. Deterministic Risk Engine

### 11.1 P0 calculation

```text
verified_completed_minutes = sum(work_block.verified_minutes)
remaining_minutes = max(confirmed_effort - verified_completed_minutes, 0)

reserved_capacity = sum(valid future blocks owned by this commitment)
unreserved_capacity = sum(eligible free minutes not allocated to another commitment)
usable_capacity = reserved_capacity + unreserved_capacity

slack_minutes = usable_capacity - remaining_minutes
slack_ratio = slack_minutes / max(remaining_minutes, 30)
```

When Calendar busy intervals are generated, this commitment's own valid future blocks must either be excluded from busy time or added back exactly once as `reserved_capacity`. Blocks owned by other active commitments remain unavailable. Unconfirmed elapsed blocks contribute neither progress nor future capacity.

Initial thresholds:

```text
deadline passed and incomplete  → overdue
effort not confirmed            → unknown
slack_minutes < 0               → critical
0 ≤ slack_ratio < 0.25          → at_risk
slack_ratio ≥ 0.25              → on_track
```

These thresholds must be configuration, not prompt text. The audit event records confirmed effort, verified progress, reserved capacity, unreserved capacity, remaining work, slack, threshold version, and previous/new risk.

### 11.2 Progress evidence

P0 accepts two progress paths:

- **Work-block check-in:** The user records minutes actually completed after a block. The update is bounded by the block duration and stored with actor and timestamp.
- **Commitment completion:** Manual confirmation or qualifying Sent-email evidence closes the commitment and sets remaining effort to zero while retaining the prior effort history.

The system must never infer completed minutes only because a block's end time has passed. If a block is partially completed, the verified portion is retained and the remaining portion is replanned.

### 11.3 P0 limitations

P0 deliberately excludes probabilistic productivity forecasts, adaptive personal models, and dependency-adjusted capacity. The goal is a transparent calculation a judge can reproduce from the screen.

## 12. Deterministic Scheduling and Stable Repair

### 12.1 Hard constraints

- Existing non-CommitmentOS busy events
- Commitment deadline and timezone
- User working hours
- Minimum session length
- Maximum work-block length
- Daily focus-work limit
- No overlap
- No scheduling in the past
- Only app-owned blocks may be mutated

### 12.2 Soft preferences

- Preferred focus periods
- Earlier completion and deadline buffer
- Fewer fragmented sessions
- Balanced daily load
- Avoiding back-to-back work when alternatives exist

### 12.3 Initial planning algorithm

P0 uses a deterministic greedy planner:

1. Retrieve busy intervals for the planning horizon.
2. Treat unrelated events and blocks assigned to other commitments as busy; treat this commitment's valid owned blocks as reserved capacity.
3. Generate candidate free slots at a fixed interval such as 15 minutes.
4. Remove candidates violating hard constraints.
5. Score valid slots using stable documented preferences.
6. Allocate the remaining effort across preserved owned blocks and the best new slots.
7. Produce a plan proposal and risk result.
8. Create Calendar events only after initial confirmation, using deterministic external event IDs.

An optimization solver is not required for P0.

### 12.4 Repair objective

When actual Calendar state changes, apply this priority order:

1. Never violate a hard constraint.
2. Preserve completed and currently active work blocks.
3. Preserve all unaffected future blocks.
4. Move the smallest possible number of affected future blocks.
5. Minimize the total time displacement from the approved plan.
6. Restore deadline buffer where possible.
7. Escalate instead of pretending success when no feasible repair exists.

The demo explanation should say exactly which block moved, why it moved, what remained unchanged, and how risk changed.

## 13. Autonomy, Safety, and Privacy

### 13.1 P0 autonomy policy

| Action | Policy |
|---|---|
| Detect and record a possible commitment | Automatic |
| Show a candidate in the dashboard | Automatic |
| Infer an uncertain deadline or owner | Require confirmation |
| Create the first Calendar plan | Require effort and plan confirmation |
| Repair app-owned blocks within approved preferences | Automatic with notification and undo |
| Make extensive changes or exceed daily limits | Require renewed approval |
| Modify a non-CommitmentOS event | Forbidden |
| Send an external email | Not in P0; always approval-gated in P1 |
| Mark complete from ambiguous evidence | Require confirmation |
| Disconnect an account or delete stored data | Require confirmation |

“Extensive change” must be deterministic configuration, for example moving more than two blocks, shifting total work by more than one day, or scheduling outside preferred hours.

### 13.2 Prompt-injection boundary

Email and Canvas content are untrusted data. P0 must enforce:

- Source text is clearly delimited as data in model requests.
- The extraction agent has no mutation tools.
- Model output must satisfy a strict schema.
- Deterministic code validates dates, ranges, ownership enums, and confidence.
- Model-produced text can never bypass the policy node.
- Secrets, tokens, internal prompts, and unrelated emails are excluded from model context.
- Adversarial email fixtures are included in evaluation.
- Logs redact message bodies, OAuth material, and Gemini API credentials.

### 13.3 Data minimization

- Process the required email body transiently.
- Persist message and thread IDs, a short evidence excerpt, structured fields, and a content hash.
- Avoid storing complete mailbox contents.
- Provide monitoring pause, account disconnect, and data deletion.
- Record access and deletion events in the audit timeline.

### 13.4 OAuth plan

P0 uses a named Gmail/Calendar test account and the narrowest functional scopes:

- Gmail read access for commitment and Sent evidence
- Calendar events read/write access
- Basic identity scopes

Do not request Gmail send or modify scope in P0. Refresh tokens and watches can expire, so the UI must surface `reauth_required` instead of failing silently. Reconnect the recording account shortly before the final demo.

For an External OAuth consent screen in Testing status, assume the refresh token expires after seven days because Gmail and Calendar scopes exceed basic profile access. The implementation runbook must therefore include:

- An end-to-end reauthorization test during the integration spike
- Scheduled developer reconnection during the 21-day build
- Reconnection immediately before golden-run testing and video recording
- No silent fallback to stale cached source data
- A seeded judge mode that demonstrates the interface without asking judges to connect Gmail

The hosted URL may expose seeded or read-only demonstration data. P0 does not promise public multi-user Gmail onboarding or OAuth verification. Never share the controlled Google account's credentials with judges.

### 13.5 Endpoint authentication

- Authenticate the dashboard and approval endpoints; validate OAuth `state` on connection callbacks.
- Configure authenticated Pub/Sub push where possible and validate the expected subscription or service identity.
- Validate Calendar channel ID, opaque channel token, resource ID, and expected calendar mapping before enqueueing work.
- Keep webhook handlers fast: validate, durably enqueue, and return. Business reconciliation belongs in Cloud Tasks workers.

## 14. Dashboard — P0 Only

P0 has three primary views.

### 14.1 Today

- Today's CommitmentOS work blocks
- Newly detected candidates
- At-risk or critical commitments
- Pending confirmation or approval

### 14.2 Commitments

- Lifecycle, ownership, deadline, risk, and remaining effort
- Source evidence and confidence
- Scheduled work blocks
- Work-block check-in and verified minutes
- Initial confirmation and pause/dismiss controls
- Manual completion control

### 14.3 Activity

- Observation received
- Interpretation created or rejected
- Confirmation recorded
- Risk before and after
- Scheduling proposal
- Policy decision
- Calendar execution result
- Retry or failure
- Completion evidence

The initial release does not need separate Calendar Plan or Dependencies pages. Calendar details belong inside a commitment. Dependencies become a P1 view only after the behavior exists.

## 15. Reliability and Recovery

### 15.1 Delivery model

Assume at-least-once delivery. Exactly-once product outcomes are achieved through idempotency keys, deterministic Calendar create IDs, revision checks, source cursors, leases, and owned-event lookup—not by assuming infrastructure or ADK invokes code only once.

### 15.2 Action outbox

Before an external Calendar mutation:

1. Write the intended action, policy result, state revision, idempotency key, and deterministic external event ID.
2. Dispatch a named Cloud Task.
3. Re-read the current commitment revision before execution.
4. Skip stale or already-completed actions.
5. For a create, first check the deterministic external event ID; then insert or adopt the existing owned event.
6. Execute the Calendar mutation with etag or revision guards where supported.
7. Record the external ID, etag, outcome, and new state.
8. Observe the resulting Calendar change without creating a duplicate repair loop.

A periodic dispatcher repairs pending outbox records that were written but not enqueued.

### 15.3 Concurrency

- Reconciliation operates on an expected commitment revision.
- Firestore transactions protect revision updates.
- Gmail synchronization is serialized per user with a lease or queue concurrency of one.
- Approval resolution is compare-and-set: only one decision can win, and stale commitment revisions supersede the request.
- Stale planners must discard their output and recalculate.
- Pub/Sub ordering is not treated as a correctness guarantee.
- The activity record preserves all failed and superseded attempts.

### 15.4 Failure states visible to the user

```text
reauth_required
source_sync_delayed
model_output_rejected
calendar_action_failed
reconciliation_retrying
no_feasible_plan
approval_superseded
work_check_in_required
```

Failures must not be hidden behind an “On Track” state.

## 16. Evaluation and Definition of Done

### 16.1 Extraction evaluation

Create at least 30 labeled Gmail fixtures covering:

- My explicit promise
- Request to me
- Another person's promise
- External dependency language
- No commitment
- Ambiguous deadline
- Multiple commitments in one message
- Thread updates and changed deadlines
- Replies that restate the same commitment
- A new distinct commitment in an existing thread
- Dismissed evidence resurfacing after a later reply
- Prompt-injection attempts

Report:

- Schema-valid output rate
- Ownership accuracy
- Deadline accuracy
- False-positive candidate rate
- Identity-operation accuracy and duplicate-commitment count
- Model latency
- Cost per processed message

Target at least 90% ownership and deadline accuracy on the curated set, while preserving the raw measured result in the submission.

### 16.2 Scheduler tests

Include cases for:

- Timezones and daylight-saving transitions
- Existing all-day and recurring events
- Insufficient capacity
- Minimum block length
- Daily work limits
- Conflict inserted after planning
- Existing owned blocks count once as capacity
- Other commitments' blocks remain unavailable
- Elapsed but unconfirmed blocks do not count as progress
- Partial verified progress replans only the remainder
- Completed block preservation
- Minimal-change repair

Hard-constraint violations must equal zero.

### 16.3 Reconciliation tests

- Replay one Gmail notification repeatedly: one commitment only.
- Deliver overlapping Gmail history notifications concurrently: cursor advances once with no lost observations.
- Replay one Calendar notification repeatedly: one repair only.
- Interrupt Calendar create after the external insert but before outcome storage: deterministic event ID yields one Calendar outcome only.
- Recycle the Cloud Run instance after an input request: the later user response continues safely from Firestore.
- Resolve the same approval twice: one decision wins and one continuation observation exists.
- Invalidate a Calendar sync token: bounded full resync without state corruption.
- Let a Gmail watch expire in a fixture: renewal/catch-up path recovers.
- Run the golden demo scenario ten consecutive times.

### 16.4 Competition acceptance metrics

P0 is complete only when:

- Ten consecutive golden-path runs succeed.
- Conflict-to-repaired-plan latency is under 60 seconds operationally and under 15 seconds in the warmed demo environment.
- Duplicate commitment and work-block counts are zero under replay.
- Every Calendar mutation has an audit event and idempotency key.
- Every automatic repair touches only app-owned future blocks.
- Uncertain commitments never create a first plan without confirmation.
- Invalid model output produces safe rejection rather than partial execution.
- Elapsed time alone never reduces remaining effort.
- Stale approvals are superseded instead of executed.
- Completion always has stored evidence or explicit user confirmation.

## 17. Implementation Roadmap

### Phase 0 — integration risk spike, Days 1–2

- Freeze the golden scenario, autonomy policy, and narrow OAuth scopes.
- Create the cloud project, OAuth client, controlled test account, and minimal Cloud Run service.
- Prove Gmail watch → Pub/Sub → authenticated endpoint → durable Cloud Task.
- Prove Calendar watch → webhook → incremental fetch and channel renewal metadata.
- Prove deterministic Calendar event insert, lookup, update, and cleanup.
- Prove Gemini 3.5 Flash structured output and one deployed ADK 2.x graph run.
- Complete one reauthorization cycle and observe the expected failure state for an invalid refresh token.

**Gate:** All external systems required by the golden path work from deployed Cloud Run code. No product UI is required yet.

### Phase 1 — contracts and seeded vertical slice, Days 3–5

- Define Pydantic schemas, lifecycle transitions, identity operations, work-block states, and Firestore collections.
- Implement immutable observations, activity events, approvals, revisions, and the action outbox.
- Implement a seeded observation that produces one commitment, one approval request, one approved plan, and one audited Calendar block.
- Prove an approval survives an application restart and continues through a new observation.
- Draw the first architecture diagram before implementation diverges.

**Gate:** A seeded end-to-end run reaches real Calendar through a replay-safe outbox and remains recoverable across Cloud Run recycling.

### Phase 2 — Gmail evidence and identity, Days 6–8

- Implement serialized per-user Gmail cursor processing, message fetch, normalization, and minimal evidence storage.
- Add daily watch renewal, catch-up, bounded cursor recovery, and loop prevention.
- Add Gemini structured extraction, deterministic validation, ownership classification, and identity operations.
- Build fixtures for restatements, deadline changes, multiple commitments, dismissals, and prompt injection.
- Add the candidate dashboard and source evidence view.

**Gate:** Real and replayed thread activity produces the correct commitment records with zero unintended duplicates.

### Phase 3 — progress, capacity, and first plan, Days 9–11

- Add effort proposal and durable effort/plan confirmation.
- Implement work-block states, verified-minute check-in, and remaining-effort calculation.
- Read Calendar busy intervals and user preferences.
- Implement corrected reserved-plus-unreserved capacity math, free-slot generation, and stable scoring.
- Create app-owned blocks using deterministic event IDs and private extended properties.
- Add safe undo through a new reconciliation event rather than blind state reversal.

**Gate:** Confirmed remaining effort produces a reproducible, constraint-safe Calendar plan; elapsed time alone cannot alter progress.

### Phase 4 — observation and minimal repair, Days 12–14

- Complete Calendar synchronization, renewal, token recovery, and affected-commitment routing.
- Implement deterministic risk calculation and minimal-change repair.
- Add revision guards, named Cloud Tasks, retry adoption, and notification-loop suppression.
- Add periodic safety reconciliation and visible failure states.
- Tune the warmed demo path to repair within 15 seconds.

**Gate:** A newly inserted meeting automatically causes exactly one minimal repair with a complete before/after explanation.

### Phase 5 — completion and hardening, Days 15–17

- Add manual completion, work-block check-ins, and qualifying Sent-message completion candidates.
- Add prompt-injection fixtures, redaction, authenticated endpoints, monitoring pause, disconnect, and deletion.
- Exercise reauthorization, concurrent Gmail delivery, stale approvals, cursor recovery, and create-before-record crashes.
- Run the golden scenario ten consecutive times.

**Gate:** All Section 16 acceptance metrics pass with measured results preserved.

### Phase 6 — competition delivery, Days 18–21

- Freeze product scope; do not start P1.
- Polish only Today, Commitments, and Activity.
- Finalize the architecture diagram, README, OAuth test limitations, seeded judge mode, and cloud spin-up instructions.
- Capture Cloud Run, Pub/Sub, Firestore, Cloud Tasks, and Logging evidence.
- Record the demo at least 48 hours before submission with one temporarily warm Cloud Run instance.
- Prepare a backup recording, seeded reset procedure, and OAuth reconnection checklist.
- Complete the Devpost write-up, measured outcomes, limitations, and technology list.
- Add optional public content and social promotion only after required submission assets are safe.

P1 work may begin only if Phase 5 passes early and competition materials are already complete.

## 18. Four-Minute Competition Demo

### 0:00–0:20 — problem and promise

Show a busy Calendar and say:

> Commitments are scattered through email, but a deadline alone does not reserve the time needed to deliver. CommitmentOS keeps the promise achievable until it is complete.

### 0:20–0:55 — evidence-backed detection

- Open a seeded Gmail thread: “I'll send the revised proposal by Friday.”
- Show the candidate with ownership, deadline, confidence, and highlighted evidence.
- Emphasize that the system did not merely summarize the inbox.

### 0:55–1:25 — effort and initial plan

- Confirm the proposed three-hour effort.
- Show three valid work blocks appear around existing events.
- Point out that the first plan required confirmation.

### 1:25–1:50 — real-world disruption

- Add or reveal a meeting that displaces one CommitmentOS block.
- Return to the dashboard without pressing a “replan” button.
- In the recording environment, keep one Cloud Run instance warm and target visible reconciliation within 15 seconds.

### 1:50–2:30 — autonomous reconciliation

- Show the background observation arrive.
- Show risk change and the repaired Calendar.
- Highlight that only one affected future block moved and the other blocks remained stable.

### 2:30–3:00 — trust and auditability

- Open the Activity view.
- Show the source event, reserved and unreserved capacity, old/new risk, policy decision, action, and external outcome.
- Mention deterministic event IDs, idempotency, and app-owned event restrictions.

### 3:00–3:20 — completion

- Send the promised email or confirm completion.
- Show completion evidence, verified progress, and closure of the commitment.

### 3:20–3:50 — architecture and Google Cloud

- Show the architecture diagram.
- Identify Gemini 3.5 Flash, bounded ADK workflow runs, Firestore continuation state, deterministic planner, Pub/Sub, Cloud Tasks, Cloud Scheduler, and Cloud Run.
- Briefly show Cloud Run or Cloud Logging execution evidence.

### 3:50–4:00 — closing line

> Calendar manages events. CommitmentOS manages whether your promises remain achievable—and proves when they are done.

Canvas, dependency follow-ups, and P1 features must not appear in the primary demo path. If P1 is stable, show it only as a short closing screenshot or mention it in the roadmap.

## 19. Submission Assets

Required competition assets:

- Hosted project URL if stable
- Concise problem and value proposition
- Feature and technology list
- Public or properly shared repository
- Reproducible local and cloud spin-up instructions
- Architecture diagram
- Approximately four-minute demo video
- Visible proof of Google Cloud deployment
- Findings, limitations, and learnings

Repository documentation should include:

- Exact model and ADK versions
- Required Google APIs and scopes
- Environment-variable and Secret Manager setup
- OAuth test-user setup
- Seven-day testing-token reconnection and `reauth_required` behavior
- Pub/Sub and Calendar webhook configuration
- Gmail per-user serialization and cursor recovery
- Firestore indexes
- Cloud Run deployment commands
- Deterministic Calendar event ID generation and outbox recovery
- Demo-data seeding and reset
- Evaluation commands and measured results
- Known restrictions around OAuth testing and public production release

## 20. Features Explicitly Deferred

Do not add before P0 passes:

- Canvas
- Dependency graph UI or behavior
- Follow-up emails
- Gmail send scope
- Google Drive completion checks
- Outlook
- Multi-account support
- Adaptive effort learning
- Voice approval
- Screenshot or PDF assignment understanding
- Shared team commitments
- What-if scheduling
- Weekly analytics
- Gemma bonus integration
- A broad chatbot
- Native long-lived ADK `RequestInput` sessions or a new SQL session store
- Public multi-user Gmail onboarding and OAuth verification
- Model Armor or enterprise platform components that do not improve the golden path

## 21. Final Success Definition

CommitmentOS succeeds when the following sequence runs repeatedly on real Gmail and Calendar data without hidden intervention:

> Detect → ground in evidence → resolve identity and ownership → confirm effort → reserve capacity → record verified progress → observe disruption → reconcile actual and desired state → minimally repair → explain and audit → verify completion.

The winning product is not the one with the most connectors. It is the one that makes this loop visible, reliable, safe, and unmistakably useful—even when events are replayed, an instance restarts, or a person responds later.

## 22. Primary Technical References

- Hackathon brief supplied with the project: `All Things Agentic Hackathon.docx`
- Gemini 3.5 Flash model: <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash>
- ADK graph workflows: <https://adk.dev/graphs/>
- ADK human input: <https://adk.dev/graphs/human-input/>
- ADK Cloud Run session persistence: <https://adk.dev/deploy/cloud-run/>
- ADK workflow resume behavior: <https://adk.dev/runtime/resume/>
- Gmail push notifications: <https://developers.google.com/workspace/gmail/api/guides/push>
- Calendar push notifications: <https://developers.google.com/workspace/calendar/api/guides/push>
- Calendar incremental synchronization: <https://developers.google.com/workspace/calendar/api/guides/sync>
- Calendar event insertion and client-supplied IDs: <https://developers.google.com/workspace/calendar/api/v3/reference/events/insert>
- Gmail OAuth scopes: <https://developers.google.com/workspace/gmail/api/auth/scopes>
- Google OAuth 2.0 behavior: <https://developers.google.com/identity/protocols/oauth2>
