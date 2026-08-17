# Post-hardening live gate summary

Date: 2026-08-17 · Revision under test: `commitmentos-00045-dwk`
(post-audit hardening commit `e57949b`, `autonomy_policy_v2`, production guards)

## Verdict

The post-hardening live gate is CLOSED:

- **Security probes: 61/61 all green** (`security_probes_20260817t043029.json`)
  on the deployed revision — session negatives, CSRF, OIDC audience/identity,
  full `/demo` mutation matrix, seeded reads. Webhook rate-limit exceedance
  remains the documented owner-run step.
- **Golden campaign: ten consecutive passing runs, 50/50 checkpoints each**
  (`golden_run_01_20260817t111348.json` … `golden_run_10_20260817t125006.json`),
  seeded mode, live service, one warm instance (`--min-instances 1`, as the
  plan §18 prescribes for the demo environment).
- Conflict-to-repair latency across the ten runs: **min 15.3 s / mean 29.8 s /
  max 60.0 s**, all inside the 60-second operational budget (§16.5). The 15 s
  warmed-demo figure applies to back-to-back warmed paths; run 10 recorded
  15.3 s.
- Byte-identical replay of every observation and action passed in all ten runs.
- The frozen 18-step audit-order contract held in all ten runs.

## Mode note

The campaign ran in **seeded mode** (`--seeded --deadline-days 3`): the golden
thread's real-world deadline (Mon 2026-08-17 16:00 PT, anchored in the frozen
thread) no longer left enough schedulable capacity, so thread mode would test
calendar arithmetic, not the product. The live-Gemini interpretation leg is
separately evidenced on this same revision by the thread-mode run
`golden_run_01_20260817t050214.json`, in which every interpretation checkpoint
passed (single commitment from three observations, `my_commitment` ownership,
confident future deadline) and the run failed only on the aged deadline's
honest allocation shortfall — itself correct §12.4 escalation behavior.

## Failure diagnosis trail (all runs preserved)

Earlier same-day attempts failed for environmental reasons, each diagnosed and
preserved rather than rerun silently:

1. `t043451` — monitoring had been left paused by dashboard testing on 08-15;
   observations correctly held. Resumed via dashboard (epoch 5).
2. `t050214` — thread-mode deadline aged out (see Mode note): 120/180 minutes
   allocatable, honest shortfall, correct repair escalation.
3. `t052556`, `t054859`, `t062302` — driver waits raced Google's throttled
   cancel-echo delivery and the platform's own repair cadences; the product
   self-healed each time after the driver aborted. Driver hardened (below).
4. `t073801` — 49/50: byte-identical replay failed once while Cloud Tasks
   stragglers from the night's earlier failed campaigns were still converging
   (delayed retries landed inside the digest window). On a drained system the
   checkpoint passed in all subsequent runs.
5. `t101958` — 49/50: one conflict-to-repair outlier (80.2 s) on a cold
   scale-to-zero path; functionally perfect. Warm instance eliminates it.

## Driver hardenings (test harness only; no product changes)

- `wait_terminal_observation` budget 240 → 420 s (must outlive one
  `dispatch_pending` sweep when a first delivery draws a retryable 503).
- Transport rescue now redelivers observations with `processing_attempt >= 1`
  when no live lease holds them (replay contract makes duplicates converge).
- "golden plan committed" wait 120 → 420 s (late throttled cancel-echo syncs
  bump `calendar_state_revision` and stale planner attempts until safety
  reconciliation replans).
- Campaign pauses 45 → 120 s between runs; full echo drain before run 1.

## Findings for the record

- **First-plan retry gap:** a first plan whose commit is staled repeatedly by
  concurrent calendar revisions has no dedicated retry trigger until the next
  observation arrives. Benign in production (observations keep arriving);
  surfaced only under campaign-reset echo storms.
- Google suppresses/throttles watch notifications after change bursts
  (10-second spacing, multi-minute tails); any test harness that resets and
  recreates events must drain before asserting.

The Phase 5 evidence under `docs/phase5_evidence/` (2026-08-15, revision
`commitmentos-00043-xw8`, thread mode, 61/61 × 10) is unchanged and remains
the pre-hardening record.
