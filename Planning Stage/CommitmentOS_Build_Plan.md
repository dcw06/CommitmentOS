# CommitmentOS Build Plan

## All Things Agentic Hackathon

**Working product name:** CommitmentOS  
**Primary track:** The Taskmaster  
**Secondary award target:** Best Architectural Design

## 1. Product Concept

CommitmentOS is an autonomous commitment and reconciliation engine that discovers obligations from connected evidence sources, reserves enough working time in Google Calendar, monitors real-world changes, and continuously repairs the plan until each commitment is completed. Gmail is the initial source; Canvas is a later adapter used to demonstrate richer deadline and completion semantics.

**Core promise:** Turn scattered obligations into continuously managed execution plans.

**Positioning:** Google Calendar tells users when things happen. CommitmentOS helps ensure that what users promised actually gets done.

The product should not compete with existing Google features such as inbox summaries, basic action-item extraction, daily briefs, or one-step Calendar creation. Its differentiation comes from persistent commitment tracking, ownership classification, dependency management, capacity-aware planning, continuous reconciliation, and completion verification across platforms.

## 2. Core User Problem

Important obligations are scattered across email, learning-management systems, documents, and calendars. Existing tools can display deadlines or create tasks, but they do not consistently answer the following questions:

- What exactly did I promise to do?
- Who owns each obligation?
- What am I waiting on from someone else?
- How much time must I reserve before the deadline?
- Is the commitment still achievable after my schedule changes?
- Was the promised outcome actually completed?

CommitmentOS should manage this complete lifecycle rather than stopping after task extraction.

## 3. Primary Autonomous Loop and Reconciliation Model

The central workflow is:

1. Detect a commitment or deadline.
2. Preserve its source evidence and determine ownership.
3. Propose and confirm the effort required.
4. Inspect the user's available Calendar capacity.
5. Calculate whether the commitment is achievable.
6. Schedule appropriate working sessions before the deadline.
7. Observe changes to commitments, sources, and Calendar state.
8. Reconcile actual state against the desired commitment state.
9. Recalculate risk and repair the schedule when necessary.
10. Record why each decision and action occurred.
11. Escalate when completion becomes unlikely.
12. Verify that the promised outcome was completed and close the commitment.

This complete loop should be the center of both the implementation and the hackathon demonstration. The Reconciliation Engine—not Gemini—should be presented as the center of the system. Gemini is one reasoning component used for interpretation; deterministic services retain control of risk, scheduling, state transitions, and action policies.

```text
Observe real-world state
          ↓
Normalize changes
          ↓
Compare actual state with desired commitment state
          ↓
Are all commitments still achievable?
          ↓
      Yes       No
       │         │
   Continue   Recalculate risk
                 ↓
            Repair the plan
                 ↓
          Execute safe actions
                 ↓
             Observe again ↻
```

## 4. Prioritized Product Capabilities

### 4.1 Unified Commitment Inbox — P0

Create a dashboard containing obligations collected from normalized evidence sources. Gmail is the P0 source; Canvas is introduced in P1. Each commitment should include:

- Title and description
- Owner
- Requester or beneficiary
- Source and supporting evidence
- Deadline
- Estimated effort
- Priority and model confidence
- Dependencies
- Scheduled work blocks
- Current risk level
- Completion status

Users should be able to approve, edit, dismiss, pause, or manually add commitments.

### 4.2 Gmail Commitment Detector — P0

Use the Gmail API and Gemini to recognize language such as:

- “I'll send the proposal by Friday.”
- “Could you finish this tomorrow?”
- “I'll review it after Alex sends the figures.”
- “Just following up—have you completed this?”

The model should return structured data instead of only summarizing the message. The system must preserve the Gmail message ID and supporting evidence so that users can verify each inference.

Example output:

```json
{
  "action": "Send revised proposal",
  "owner": "user",
  "deadline": "2026-08-14T17:00:00-07:00",
  "estimated_minutes": 180,
  "dependency": null,
  "confidence": 0.91
}
```

### 4.3 Commitment Ownership Classification — P0

Classify each detected obligation as one of the following:

- **My commitment:** The user promised to do something.
- **Request to me:** Someone asked the user to do something.
- **Commitment to me:** Someone else promised the user something.
- **External dependency:** The user's work is blocked by someone else.
- **Possible commitment:** The language is too ambiguous for automatic action.

This ownership model differentiates CommitmentOS from a general-purpose action-item extractor.

### 4.4 Canvas Assignment Connector — P1

Collect relevant information from Canvas, including:

- Course
- Assignment name and instructions
- Due date
- Submission status
- Deadline changes
- Relevant files or modules, when available

Canvas supplies formal deadlines and submission evidence. CommitmentOS determines when the user should perform the work and whether the commitment has been fulfilled. Canvas is an evidence-source adapter, not part of the product identity. For the hackathon demo, support a controlled test account or personal access token so that institutional OAuth approval is not a critical dependency.

### 4.5 Effort Estimator and Confirmation — P0

Estimate the work required based on the description, deliverable type, referenced materials, and user feedback. Treat the result as an uncertain proposal rather than a fact. Show a range and confidence, and ask the user to confirm or override it before making substantial Calendar changes.

The initial version can use simple effort categories:

- Small: 30 minutes
- Medium: 1–2 hours
- Large: 3–5 hours
- Project: Multiple sessions

An adaptive estimator that learns from estimated versus actual completion time belongs in P2.

### 4.6 Capacity-Aware Calendar Planner — P0

Do not create only a deadline event. Divide the estimated effort into working sessions that fit around existing Calendar commitments.

The planner should consider:

- Existing Calendar events
- Working hours
- Preferred focus periods
- Minimum session length
- Breaks and daily workload limits
- Task priority
- Deadline buffer
- User scheduling preferences

Use deterministic code to enforce time constraints. Gemini can interpret commitments and recommend a strategy, while the scheduling engine guarantees that generated blocks do not violate hard constraints.

### 4.7 Reconciliation Engine and Continuous Replanning — P0

Run a background reconciliation process when:

- A new commitment appears
- A Calendar event is added or moved
- A planned work session is missed
- The effort estimate changes
- A commitment is completed
- In P1, a Canvas deadline changes
- In P1, a dependency becomes late

The engine should compare current source, commitment, and Calendar state against the previously desired state. It should preserve completed work, adjust future sessions, recalculate risk, and explain every change in plain language. All handlers must be idempotent so replaying an event cannot create duplicate commitments or Calendar blocks.

### 4.8 Commitment Risk Engine — Basic P0, Advanced P1

Assign one of the following states:

- **On track:** Sufficient capacity remains.
- **At risk:** Remaining capacity is close to the required effort.
- **Blocked:** An unresolved dependency prevents progress.
- **Critical:** Insufficient working capacity remains before the deadline.
- **Overdue:** The deadline passed without completion.

The P0 calculation should stay deliberately simple:

```text
remaining work = 180 minutes
usable capacity = 240 minutes
→ ON TRACK

remaining work = 180 minutes
usable capacity = 195 minutes
→ AT RISK

remaining work = 180 minutes
usable capacity = 120 minutes
→ CRITICAL
```

A passed deadline produces **Overdue**. Dependency-driven **Blocked** status and richer confidence adjustments belong in P1.

### 4.9 Dependency Tracking — Schema in P0, Behavior in P1

Represent relationships among commitments. For example:

```text
Alex sends figures
        ↓
User reviews figures
        ↓
User submits report
```

The P0 data model must already support dependency relationships to avoid a later redesign. P1 adds dependency-driven risk and an interface showing which commitment is blocked, who owns the blocking deliverable, and when follow-up becomes appropriate.

### 4.10 Approval-Based Follow-Ups — P1

When another person's commitment becomes late, the system should:

1. Detect the delay.
2. Identify the user's affected downstream commitment.
3. Draft a contextual follow-up.
4. Display the supporting source evidence.
5. Ask the user to approve sending it.

CommitmentOS should not automatically contact third parties during the MVP.

### 4.11 Completion Verification — Basic P0, Source-Rich P1/P2

Close commitments using evidence instead of assuming that elapsed Calendar time means completion. Possible evidence includes:

- P0: The user sent the promised Gmail reply or manually confirmed completion.
- P1: Canvas reports that an assignment was submitted.
- P2: A referenced Drive artifact reached the expected state.

### 4.12 Decision and Audit Timeline — Minimal P0, Polished P2

P0 must record enough information to answer, “Why did the agent move this block?” A polished investigation interface can wait until P2. Record important system events, including:

- Commitment detection
- User confirmation
- Scheduling actions
- Calendar conflicts
- Replanning decisions
- Dependency changes
- Follow-up approvals
- Completion evidence
- Failed actions and retries

Each entry should contain a timestamp, reason, source, affected commitment, model confidence when applicable, previous and new risk, requested action, and execution outcome.

## 5. Commitment Data Model

The central product object should be a commitment rather than an email, task, or Calendar event.

```json
{
  "commitment": "Send revised proposal",
  "owner": "me",
  "beneficiary": "Professor Chen",
  "source": {
    "system": "gmail",
    "message_id": "abc123",
    "evidence": "I'll send the revision by Friday"
  },
  "deadline": "2026-08-14T17:00:00-07:00",
  "estimated_minutes": 180,
  "estimate_confidence": 0.55,
  "effort_confirmed": true,
  "blocked_by": [],
  "blocks": [],
  "work_blocks": ["calendar-event-1", "calendar-event-2"],
  "risk": "on_track",
  "status": "in_progress"
}
```

The dependency fields exist in P0 even though dependency reasoning and follow-up behavior are implemented in P1.

## 6. Dashboard Structure

### Today

- Today's planned work sessions
- Newly detected commitments
- Commitments at risk
- Pending approvals

### Commitments

- All active obligations
- Ownership, deadline, and risk
- Source evidence
- Scheduled versus remaining effort

### Calendar Plan

- Planned working sessions
- Scheduling conflicts
- Replanning explanations

### Dependencies

- Items waiting on other people
- Downstream commitments affected
- Recommended follow-ups

### Activity

- Agent decisions
- Tool actions
- Failures and retries
- Approval history

## 7. Safety and Trust Controls

The MVP should include:

- OAuth with minimum necessary permissions
- No external messages without explicit approval
- Confidence thresholds for inferred commitments
- Source evidence for every extraction
- Monitoring pause control
- Account disconnection and data deletion
- Undo for Calendar changes
- Deduplication of messages and assignments
- Idempotent background jobs
- Retry and dead-letter handling
- Secure token storage through Secret Manager

### Suggested Autonomy Policy

| Action | Policy |
|---|---|
| Detect a possible commitment | Automatic |
| Add a commitment to the dashboard | Automatic |
| Schedule high-confidence personal work blocks | Automatic with undo |
| Make extensive Calendar changes | Notify the user |
| Infer an uncertain deadline | Request confirmation |
| Send an external email | Always require approval |
| Delete an event or commitment | Require confirmation |

## 8. Reconciliation-Centered Google Cloud Architecture

```text
       Gmail API       Canvas API       Calendar API
          │                │                 │
          └────────────────┼─────────────────┘
                           ↓
              Cloud Run ingestion adapters
                           ↓
                     Event normalizer
                           ↓
                         Pub/Sub
                           ↓
                Commitment state (Firestore)
                           ↕
             ┌───────────────────────────┐
             │   RECONCILIATION ENGINE   │
             │         Google ADK        │
             └───────────────────────────┘
                 │           │           │
                 ↓           ↓           ↓
              Gemini       Risk      Deterministic
           interpretation  engine      scheduler
                 │           │           │
                 └───────────┼───────────┘
                             ↓
                     Policy-safe actions
                             ↓
                  Google Calendar executor
                             ↓
                       Observe again ↻

                 Web dashboard + timeline

Cloud Tasks     → Reliable retries and delayed checks
Cloud Scheduler → Periodic reconciliation
Cloud Logging   → Operational logs and audit evidence
Secret Manager  → OAuth credentials and tokens
```

This architecture makes the product boundary explicit: Gemini interprets ambiguous human input, but it is not the system. The Reconciliation Engine owns state comparison, calls the risk and scheduling components, applies action policy, records decisions, and observes the result. The architecture satisfies the required Gemini, Google agent framework, and Google Cloud infrastructure components while demonstrating asynchronous execution.

## 9. Locked Implementation Roadmap

### P0 — Prove the Autonomous Closed Loop

- Define the commitment data model.
- Implement Gmail ingestion.
- Add Gemini structured extraction with source evidence.
- Classify commitment ownership.
- Propose an effort range and obtain user confirmation.
- Implement Google Calendar read and write access.
- Build a deterministic capacity and work-block scheduler.
- Implement background processing.
- Implement a basic risk calculation.
- Detect Calendar changes and displaced work.
- Implement reconciliation and automatic schedule repair.
- Record a minimal decision and execution timeline.
- Verify completion through sent Gmail evidence or user confirmation.
- Deploy the complete loop on Google Cloud.

The required P0 sequence is:

```text
Gmail arrives
      ↓
Commitment extracted
      ↓
Evidence + ownership established
      ↓
Effort confirmed
      ↓
Calendar capacity calculated
      ↓
Work scheduled
      ↓
Something changes
      ↓
RECONCILIATION
      ↓
Risk recalculated
      ↓
Schedule automatically repaired
      ↓
Reason recorded
      ↓
Completion detected
      ↓
Commitment closed
```

**Hard development gate:** Do not begin P1 until this complete P0 scenario can run repeatedly without manual intervention other than the intentional effort-confirmation step.

### P1 — Deepen the Commitment Model

- Add the Canvas evidence-source adapter.
- Implement the dependency graph.
- Handle changing source deadlines.
- Represent commitments owned by external people.
- Add dependency-driven blocked and risk states.
- Draft contextual, source-linked follow-ups with approval.
- Verify completion from Canvas submission state.

### P2 — Ecosystem Breadth, Sophisticated UX, and Learning

- Add a polished audit and investigation interface.
- Add Google Drive completion verification.
- Learn adaptive effort estimates from actual outcomes.
- Add Outlook and multiple-account support.
- Add multimodal input or approval.
- Explore shared and team commitments.

### Competition Polish

- Finalize the architecture diagram.
- Capture visible Google Cloud deployment evidence.
- Create a reliable seeded demo scenario.
- Demonstrate failure handling and retries.
- Write reproducible setup instructions.
- Record the four-minute demo video.
- Optionally publish a technical article and social post.

## 10. Recommended Competition Demo Story

1. Show an overloaded Google Calendar.
2. Canvas publishes a four-hour assignment due Friday.
3. Gmail contains: “I'll review it once Alex sends the dataset.”
4. CommitmentOS records the assignment, the user's commitment, and Alex's dependency.
5. The planner reserves three working sessions before the deadline and shows the initial risk as On Track.
6. A new meeting visibly displaces the most important planned session.
7. The background agent detects the conflict, changes the risk to At Risk, and automatically repairs the Calendar.
8. Alex's dataset becomes late, increasing the commitment's risk.
9. The system drafts a source-linked follow-up and requests approval.
10. Canvas reports submission, and CommitmentOS closes the commitment.
11. The activity view explains every decision, including the before-and-after risk state, and displays Google Cloud execution evidence.

The visible Calendar repair is the demo's unforgettable moment. The judge should immediately see the displaced work, the remaining effort, the deadline, and the repaired plan. This scenario demonstrates operational utility, asynchronous execution, persistent state, adaptation, explainability, safety, and production readiness.

## 11. Stretch Features

Only add these after the complete MVP loop works reliably:

- Outlook connector through Microsoft Graph
- Multiple Google-account support
- Adaptive effort estimates based on actual work
- Google Drive document-state verification
- Voice approval
- Screenshot or PDF assignment understanding
- Shared team commitments
- Collaborative dependency graphs
- What-if scheduling
- Weekly commitment-reliability reports
- Gemma-powered local classification
- Multimodal document or voice input

## 12. Features to Defer

Do not spend initial hackathon development time on:

- Generic Gmail summarization
- A general-purpose chatbot
- Basic daily briefs
- Meeting-time suggestions
- Slides or presentation generation
- Social-media monitoring
- Sentiment or dissatisfaction detection
- Automatic external follow-ups
- Every Google Workspace integration
- Enterprise-wide agent registries
- Analytics without a direct user benefit

## 13. Success Definition

The project succeeds when it reliably demonstrates one complete closed loop:

> Detect commitment → establish evidence and ownership → confirm effort → reserve working time → monitor changes → reconcile and repair → explain the decision → verify completion.

A dependable implementation of this sequence is more valuable than a large number of shallow connectors. P0 is considered complete only when the system repeatedly survives the full demo scenario without manual repair or hidden intervention. P1 deepens commitment semantics; P2 broadens the ecosystem and experience. This creates a focused Taskmaster submission with a credible opportunity to compete for Best Architectural Design.
