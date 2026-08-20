# Measured proof index

The interactive sandbox and the seeded dashboard demonstrate the product's
behavior. The claims a UI cannot demonstrate — durability, replay, delivery
guards, and the security matrix — were measured against the deployed Cloud Run
service. This page indexes exactly where each measurement lives. Evidence
files are frozen records: they are never regenerated or edited.

## The numbers, with their scope

Every result names the Cloud Run revision it was measured on — the revision
is the build identifier — and the frozen file recording it (detailed in the
sections below).

| Result | Scope | Revision | Date |
| --- | --- | --- | --- |
| 10/10 consecutive golden runs, 61/61 checkpoints each | Full real Gmail → interpretation → Calendar path, live Gemini every run | `commitmentos-00042-fcj` | 2026-08-15 |
| 10/10 consecutive golden runs, 50/50 checkpoints each | Seeded mode, post-hardening policy v2 | `commitmentos-00045-dwk` | 2026-08-17 |
| 0 duplicate commitments · 0 duplicate Calendar events | Checkpointed inside every golden run above | both campaign revisions | 2026-08-15/17 |
| Conflict-to-repair 7.2–10.2 s (mean 9.1 s) | Live deployed service, real Calendar conflict, warmed 15 s budget | `commitmentos-00042-fcj` | 2026-08-15 |
| Byte-identical replay of every observation and action | Re-delivered inside all 20 golden runs above | both campaign revisions | 2026-08-15/17 |
| 73/73 live security probes | Session/CSRF/OIDC matrices, full `/demo` mutation rejection, sandbox isolation | `commitmentos-00059-t4z` | 2026-08-20 |
| 32/32 extraction eval, 100% every metric | Fixture suite over live Gemini, ~$0.0008/message | pre-deploy eval run | 2026-08-13 |

## Golden campaign: ten consecutive live runs, byte-identical replay

| Claim | Where it is proven |
| --- | --- |
| Ten consecutive golden-path runs on the deployed service, **live Gemini interpretation every run, 61/61 checkpoints each**; conflict-to-repair 7.2–10.2 s (mean 9.1 s); replay of every observation and action leaves durable state byte-identical | Revision `commitmentos-00042-fcj` (2026-08-15). Summary: [`phase5_evidence/phase5_gate_summary.md`](phase5_evidence/phase5_gate_summary.md). Runs: `phase5_evidence/golden_run_01_20260815t063708.json` … `golden_run_10_20260815t074239.json` |
| Ten consecutive seeded runs, **50/50 checkpoints each**, after the post-audit hardening pass (policy v2); frozen 18-step audit order; byte-identical replay ×10 | Revision `commitmentos-00045-dwk` (2026-08-17). Summary: [`post_hardening_evidence/gate_summary.md`](post_hardening_evidence/gate_summary.md). Runs: `post_hardening_evidence/golden_run_01_20260817t111348.json` … `golden_run_10_20260817t125006.json` |

Each run file records every checkpoint with the observed values: Cloud Tasks
deliveries, observation replay digests before/after, outbox action statuses,
stable Calendar event ids, and repair latency. Failed earlier attempts are
preserved beside the passes with their diagnoses — the failures are part of
the record, not cleaned up.

## Live security matrix

| Claim | Where it is proven |
| --- | --- |
| **73/73 probes green** on the currently serving revision: session negative matrix (12), CSRF on every controlled mutation route (15), wrong OIDC audience/identity on internal routes (6), `/demo` full read/mutation matrix (27), sandbox isolation — forged sessions, off-deck input, caller-defined identities, oversized messages, state-disallowed cards, cross-session invisibility (12), rate limit (1) | Revision `commitmentos-00059-t4z` (2026-08-20). [`sandbox_evidence/security_probes_20260820t051512.json`](sandbox_evidence/security_probes_20260820t051512.json), narrated in [`sandbox_evidence/README.md`](sandbox_evidence/README.md) |
| 73/73 probes green on the prior revision | Revision `commitmentos-00058-dtx` (2026-08-20). [`sandbox_evidence/security_probes_20260820t040040.json`](sandbox_evidence/security_probes_20260820t040040.json) |
| 73/73 probes green on the prior sandbox revision | Revision `commitmentos-00055-ks8` (2026-08-18). [`sandbox_evidence/security_probes_20260818t153546.json`](sandbox_evidence/security_probes_20260818t153546.json) |
| 70/70 probes green on the first sandbox revision | Revision `commitmentos-00048-4ft` (2026-08-18). [`sandbox_evidence/security_probes_20260818t030803.json`](sandbox_evidence/security_probes_20260818t030803.json) |
| 61/61 probes green on the post-hardening gate revision | [`post_hardening_evidence/security_probes_20260817t043029.json`](post_hardening_evidence/security_probes_20260817t043029.json) |
| 61/61 probes green on the Phase-6 dashboard revision | [`phase5_evidence/security_probes_20260815t134558.json`](phase5_evidence/security_probes_20260815t134558.json) |

The probe driver is committed at `scripts/run_phase5b_security.py`; every
probe file names the service URL and revision it ran against.

## Extraction quality

| Claim | Where it is proven |
| --- | --- |
| 32/32 eval cases, 100% on every metric (deadline, ownership, evidence anchoring, injection resistance), ~$0.0008/message | [`phase2_evidence/extraction_eval_20260813t040448.json`](phase2_evidence/extraction_eval_20260813t040448.json) (run 1, exposing the delimiter-escape injection gap the hardening closed, is preserved beside it) |

## What each mechanism is, in the source

- Calendar `If-Match` etags, idempotency keys, and revision fencing:
  `backend/src/commitmentos/application/commands/execute_calendar_action.py`
- Named idempotent Cloud Tasks and replay convergence:
  `backend/src/commitmentos/application/services/` dispatchers and the
  replay digests inside every golden-run file
- OIDC enforcement on internal routes: probe groups `oidc` in every security
  probes file
- Deploy release gate (session affinity, instance cap, traffic, live sandbox
  model probe): `scripts/deploy_commitmentos.py`

The interactive sandbox's expandable **session execution evidence** shows the
same fields (revisions, policy reasons, planner version, idempotency keys,
stable event ids, expected/observed etags) projected live from the audit and
outbox documents the real stack writes in your session.
