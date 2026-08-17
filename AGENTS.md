# CommitmentOS — Agent Onboarding Brief

CommitmentOS is an evidence-backed commitment controller: it detects commitments
in Gmail threads with Gemini 3.5 Flash, reserves Google Calendar capacity for
them, autonomously repairs the plan when the calendar changes, and closes
commitments only on explicit user confirmation with honest verified minutes.
Solo six-day build for Google's **All Things Agentic Hackathon** (Taskmaster
track; secondary target: Best Architectural Design).

This file orients any coding agent or new contributor. Read it before editing.

## Authoritative documents

- `Plan_Final/CommitmentOS_Build_Plan_Final.md` — the build plan (v5.1).
  `§` references in code docstrings and docs point at this document's sections.
- `docs/Phase_0_Integration_Risk_Spike_Checklist.md` — gate checklist and
  decision log for every phase.
- `docs/phase*_progress.md` and `docs/phase*_evidence/` — per-phase closure
  records and measured live evidence. **Deliberate deviations from the plan are
  recorded there — check before "fixing" an apparent plan/code mismatch.**
- `README.md` — architecture, trust contracts, spin-up, deployment, security
  model, measured results.
- `docs/submission_assets/` — Devpost story, dashboard screenshots, social
  drafts.

## Current state (2026-08-17)

All build phases (0–6) are closed with live evidence. Deployed on Cloud Run
(`us-west1`, project `commitmentos-505114`), serving the React dashboard at
`/app` (controlled-user OAuth session) and a read-only seeded judge mode at
`/demo` (no login, no mutation capability).

Measured evidence, preserved under `docs/phase5_evidence/`:

- Ten consecutive golden-path runs against the deployed service, live Gemini
  interpretation each run, 61/61 checkpoints, conflict-to-repair 7.2–10.2 s
  (mean 9.1 s), byte-identical replay of every observation and action.
- 61/61 live security probes all green on the final revision.
- Extraction eval: 32/32 cases, 100% every metric, ~$0.0008/message
  (`docs/phase2_evidence/`).

Remaining for submission: the ~4-minute demo video (script: plan §18), pushing
local commits, and the Devpost form. Optional bonus: publishing the LinkedIn
post and a build write-up.

## Verification commands

```bash
.venv/bin/pytest                # full suite — 224 tests, must stay green
.venv/bin/ruff check .          # must stay clean (repo enforces check, not format)
cd frontend && npm run build    # tsc + vite; bundle served from frontend/dist
```

mypy carries known pre-existing errors from untyped `googleapiclient`/ADK
imports; do not chase them unless you touched the flagged files.

## Hard rules

1. **No personal identifiers in committed files, ever.** The controlled Google
   account is aliased `controlled-01` in every committed document; its real
   address lives only in the gitignored `.env`. This repo is shared with
   hackathon judges.
2. **Frozen artifacts:** `golden_scenario_rev_1`, `autonomy_policy_v1`,
   `scope_set_v1`, and the recorded evidence JSON under `docs/`. Evidence files
   are records — never regenerate, edit, or "clean up" past evidence. Scenario
   or policy changes require a revision bump plus a checklist decision-log
   entry.
3. **Don't jeopardize the verified state.** The deployed revision passed the
   full campaign; behavioral backend changes this close to submission need a
   rerun of the suite and a deliberate decision, not a drive-by refactor.
4. **Deploys are owner-run:** `gcloud run deploy commitmentos --source .
   --region us-west1` (agents should not deploy or shift traffic).
5. `.env` is authoritative local config and has been corrupted by IDE buffer
   restores before — verify its contents before any deploy-related work.

## Architecture in one paragraph

One FastAPI service on Cloud Run; Firestore is the sole source of durable
truth. Gemini interprets ambiguous email language inside a bounded ADK
workflow run and produces evidence-anchored structured proposals; everything
consequential (identity resolution, portfolio planning, policy, Calendar
mutation) is deterministic code. Gmail changes arrive via Pub/Sub push,
Calendar changes via an authenticated public webhook; all work rides three
Cloud Tasks queues with named idempotent tasks. Calendar writes go through a
transactional outbox executed by a separate handler that revalidates
revisions, execution-control epochs, and observed etags (`If-Match`)
immediately before I/O; HTTP 412 stales the intent and triggers
resynchronization. Cloud Scheduler drives watch renewal, cursor catch-up,
outbox/observation dispatch repair, and a once-a-minute safety reconciliation.
Every decision lands on an audit timeline.

## Known gaps (comprehensive plan-vs-code audit, 2026-08-17)

Recorded so nobody rediscovers them as surprises; none block submission:

- `backend/src/commitmentos/workflows/reconciliation/nodes/` and a few other
  files are unwired `...` stubs mirroring plan §8.1 node names; the real
  workflow lives in `phase1_workflow.py` behind a coarse ADK graph (documented
  deviation).
- Dashboard omits several plan-§14 surfaces: five-stat outcome strip wording,
  a "newly detected candidates" section, portfolio allocation/shortfall/
  projected-finish display, per-commitment pause/dismiss controls.
- Gmail invalid-cursor recovery is operator-assisted (Calendar's is
  automatic); the Calendar webhook does not check stored channel expiration;
  `explain_decision` (plan §8.2 plain-language explanations) was never
  implemented; `Commitment.reopen()` has no route; explicit user priority in
  planner ordering is never written by anything.
- README lacks plan-§19 sections on Gmail serialization/cursor recovery,
  staging-generation write budgets, demo seed/reset commands, and the eval
  command line.
