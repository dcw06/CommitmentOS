# CommitmentOS — Agent Onboarding Brief

CommitmentOS is an evidence-backed commitment manager. It finds your commitments
in Gmail threads using Gemini 3.5 Flash, reserves time for them on your Google
Calendar, automatically fixes your plan if your calendar changes, and only marks
work as done when you confirm it and log real minutes. Built solo in two weeks
for Google's **All Things Agentic Hackathon** (Taskmaster track; aiming for Best
Architectural Design as well).

This file is your starting point if you're a coding agent or a new contributor.
Please read it before you start making changes.

## Authoritative documents

* `Plan_Final/CommitmentOS_Build_Plan_Final.md` — this is the main build plan
  (v5.1). When you see `§` references in code or docs, they point to sections in
  this document.
* `docs/Phase_0_Integration_Risk_Spike_Checklist.md` — the checklist and
  decision log for each phase.
* `docs/phase*_progress.md` and `docs/phase*_evidence/` — these track the
  progress and evidence for each phase. Any intentional changes from the plan
  are recorded here, so check them before "fixing" something that looks like a
  mismatch between the plan and the code.
* `README.md` — covers the architecture, trust contracts, how to start and
  deploy, the security model, and measured results.
* `docs/submission_assets/` — includes the Devpost story, dashboard screenshots,
  and social media drafts.

## Current state (2026-08-31)

All build phases (0–6) are complete with live evidence. The system is deployed
on Cloud Run (`us-west1`, project `commitmentos-505114`), serving the React
dashboard at `/app` (with OAuth for the controlled user), a read-only judge mode
at `/demo` (no login, no edits), and an interactive judge sandbox at `/sandbox`.

The sandbox (`backend/src/commitmentos/sandbox/`) runs the real command stack,
but uses an in-memory twin so each visitor gets their own isolated world. If
you're editing the sandbox, keep these rules in mind: the twin in
`sandbox/twin.py` is shared with the backend test suite (it's re-exported in
`backend/tests/fakes.py`), so any changes affect the tests too. The sandbox is
the only place you can make changes without authentication, so its isolation is
strictly enforced by `backend/tests/contract/test_sandbox_contracts.py` — don't
loosen it. Recorded interpretations in `sandbox/scenario.py` include evidence
quotes, which must always be exact matches from their message bodies. In free
play, you're limited to the two simulated identities and the caps set in
`sandbox/session.py`, and never use a recorded interpretation as a fallback. The
only external connection is the sandbox-specific Gemini interpreter built in
`bootstrap/gemini_boundary.py`; the sandbox always uses its own key from a
separate quota project, and it must never receive the production interpreter or
a container-bound client.

Measured evidence:

* Ten golden-path runs in a row were completed on the deployed service, with
  live Gemini interpretation for each run. All 61 checkpoints were hit, repairs
  from conflict took between 7.2 and 10.2 seconds (average 9.1), and every
  observation and action was replayed exactly (`docs/phase5_evidence/`).
* After the post-audit hardening pass, ten more consecutive runs passed, 50/50
  checkpoints each (`docs/post_hardening_evidence/`).
* All live security probes passed — 61/61 in the campaign era, growing to 73/73
  on the final frozen revision (see the post-audit section below).
* Extraction evaluation: 32 out of 32 cases passed, with perfect scores across
  all metrics, at a cost of about $0.0008 per message (see
  `docs/phase2_evidence/`).

The submission assets — demo video script, Devpost story, screenshots, and
social drafts — are complete under `docs/submission_assets/`, and
`docs/proof_index.md` ties every measured claim to the revision that produced
it.

## Verification commands

```bash
.venv/bin/pytest                # full suite — 328 tests, must stay green
.venv/bin/ruff check .          # must stay clean (repo enforces check, not format)
cd frontend && npm run build    # tsc + vite; bundle served from frontend/dist
```

mypy may report known errors from untyped `googleapiclient`/ADK imports — don't
worry about these unless you've changed the flagged files.

## Hard rules

1. Never include personal identifiers in committed files. The controlled Google
   account is always called `controlled-01` in every document, and its real
   address only appears in the gitignored `.env` file. Remember, this repo is
   shared with hackathon judges.
2. Frozen artifacts include: `golden_scenario_rev_1`, `autonomy_policy_v1`,
   `autonomy_policy_v2`, `scope_set_v1`, and the evidence JSON files under
   `docs/`. These files are permanent records — never regenerate, edit, or
   "clean up" past evidence. If you change a scenario or policy, update the
   revision and add a checklist decision-log entry.
3. Don't risk the verified state. The current deployed revision passed all
   checks, so any backend changes this close to submission require re-running
   the full test suite and making an intentional, documented decision — not a
   quick refactor.
4. Deploys are only done by the owner: use
   `.venv/bin/python scripts/deploy_commitmentos.py --deploy` (agents shouldn't
   deploy or change traffic settings). This release process enforces the Cloud
   Run contract for the sandbox.
5. The `.env` file is the source of truth for local config, and has been
   accidentally corrupted by IDE buffer restores before — so always check its
   contents before any deploy-related work.

## Architecture in one paragraph

The whole system runs as one FastAPI service on Cloud Run, with Firestore as
the single source of truth. Gemini interprets ambiguous email language during a
controlled ADK workflow, producing structured proposals anchored by evidence.
All the important steps — like resolving identity, planning your schedule,
applying policies, and making Calendar changes — are handled by deterministic
code. Gmail updates come in via Pub/Sub push, Calendar updates arrive through a
secure webhook, and everything is managed through three Cloud Tasks queues with
unique, idempotent tasks. Calendar writes are handled by a transactional outbox
and a separate handler that double-checks revisions, execution epochs, and
If-Match etags just before writing. If a 412 error comes up, the system marks
the intent as stale and triggers a resync. Cloud Scheduler handles watch
renewals, cursor catch-ups, repairing dispatch, and runs a safety
reconciliation every minute. Every decision is recorded in an audit timeline.

## Post-audit source state (2026-08-20 — code frozen)

The deployed revision `commitmentos-00059-t4z` (100% traffic, deployed via the
release gate `scripts/deploy_commitmentos.py`) carries every committed pass:
the post-audit hardening (`docs/post_audit_hardening_progress.md`,
gate-verified on `commitmentos-00045-dwk` — `docs/post_hardening_evidence/`),
the sandbox judge-path hardening (`docs/sandbox_hardening_progress.md`), and
the judge-feedback rounds (`docs/judge_feedback_progress.md`). The frozen
revision is live-verified: security probes 73/73
(`docs/sandbox_evidence/security_probes_20260820t051512.json`) and the full
authored story driven through the API with live Gemini. `docs/proof_index.md`
maps every measured claim to its revision and frozen evidence file. Deploys
remain owner-run through the release gate; do not bypass its post-deploy
checks.

Remaining intentional scope gap:

* Person/dependency behavior is still a top priority (P1). The
  `blocking_status` field is left clear so it's not used for effort
  confirmation or capacity shortfall — those each have their own fields and
  risk indicators.
