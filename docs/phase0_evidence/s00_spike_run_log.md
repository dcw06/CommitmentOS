# Phase 0 spike — sanitized run log, limitations, and closing status

## Timeline (all 2026)

| When | What ran |
|---|---|
| 08-10 evening | Decisions frozen: golden scenario (`golden_scenario_rev_1`), autonomy policy (`autonomy_policy_v1`), scope set (`scope_set_v1`). Project `commitmentos-505114` created. |
| 08-10/11 | Cloud foundation: billing linked (after freeing a quota slot), 10 APIs, Firestore Native `us-west1`, 3 delivery SAs + runtime SA, 3 Cloud Tasks queues, Gmail topic with push-SA grant, 3 secrets vaulted, $100 budget alert. First deploy (revision `…00001`), env corrected to real URLs (`…00002`). |
| 08-12 | §3 OAuth spike: Testing pass (authorize / refresh / revoke / `invalid_grant` / reauthorize), production pass (restricted scope granted unverified), grant-wide-revocation finding, mode selected. |
| 08-12 | §4 Gmail chain live (coalesced deliveries, named task, bounded fetch, cursor discipline); stale-env incident produced real-token negative evidence. |
| 08-12 | §5 Calendar webhook boundary (7-case invalid matrix zero-side-effect, live chain via API poke, replay convergence, channel renewal). |
| 08-12 | §6 provider behaviors (stable IDs, 409-adopt, If-Match, forced 412 shape, ownership guard, preview cleanup) — 10/10 first run. |
| 08-12 | §7 deployed Gemini structured output (all golden expectations matched) + deployed ADK 2.6.3 graph run with durable outcome. |
| 08-12 | §8 full reauthorization cycle live, including code-replay rejection and no-grace-window finding. |
| 08-12 | §9 session/allowlist/demo contracts; OIDC negative matrix with genuinely signed tokens; **OAuth mode selection made final**. |

Deployment record: 10 Cloud Run revisions over the spike; final revision
`commitmentos-00010-wzf`, health live. Full command transcript resides in the
build session log; every evidence value above is reproducible from the
scripts in `scripts/` (`oauth_spike.py`, `gmail_watch_spike.py`,
`calendar_watch_spike.py`, `calendar_identity_spike.py`).

## Known limitations and open items at gate time

1. **Section 4 crash-gap repair row open** — task-creation-failure fault
   injection deferred; the repairing periodic dispatcher is Phase 1 scope
   (blocker log B1).
2. **Owner screenshots pending** — consent-warning appearance per scope class
   (Testing and production) not yet captured to this pack (blocker log B2).
3. **Controlled account is a personal secondary address** — accepted by the
   owner with the recorded caveat; all committed docs use the `controlled-01`
   alias, and the address lives only in the gitignored `.env`.
4. **Spike code is throwaway by design** — `backend/src/commitmentos/spike/`
   routes are replaced by the real command stack in Phase 1; the durable
   document shapes they write were kept close to the target design.
5. **In-memory rate limiter** on the Calendar webhook is per-instance;
   durable per-channel limiting is Phase 5.
6. **Watches expire every 7 days** regardless of OAuth mode; daily renewal
   automation is Phase 2. Manual renewal runbook proven in §8.
7. Local-only quirk: the build sandbox intermittently fails gRPC DNS
   (c-ares); `GRPC_DNS_RESOLVER=native` is the workaround. Never observed in
   Cloud Run.

## Budget and teardown status

- Cloud Run scales to zero; queues, Firestore, Pub/Sub, and Secret Manager
  sit in free tier at spike volumes. Spend drivers were ~10 Cloud Build runs
  and a handful of Gemini calls (each well under $0.001).
- The $100 project-scoped budget (alerts at 50/90/100%) has fired no alert —
  spend is below the 50% threshold.
- Idle-cost teardown list recorded in checklist §2; nothing requires teardown
  while the build continues.
