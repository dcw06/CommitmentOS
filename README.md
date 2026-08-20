# CommitmentOS

**An evidence-backed, capacity-aware commitment controller.** CommitmentOS
is not an assistant that plans once — it is a controller that keeps a
promise feasible as reality changes, until completion is verified:

> Detect a commitment in Gmail → preserve its evidence and ownership →
> confirm effort → reserve Calendar capacity → observe a conflict →
> reconcile and minimally repair the plan → explain the action → verify
> completion.

Gemini interprets ambiguous human language into structured, evidence-anchored
proposals. Deterministic code owns everything consequential: identity
resolution, state transitions, portfolio capacity, scheduling constraints,
policy, idempotency, and every Calendar mutation.

Built for the **All Things Agentic Hackathon** (Taskmaster track).

## What it does

- **Watches Gmail** for commitments you make ("I'll have it back before our
  Friday 4 p.m. review"), resolving ownership, deadline, and thread identity
  across restatements and deadline changes — one commitment, never
  duplicates, with exact evidence excerpts (never stored message bodies).
- **Reserves real Calendar capacity** with a deterministic portfolio planner:
  shared free time is allocated once across all active commitments, inside
  working hours, minimum/maximum block lengths, and daily focus limits.
  The first plan always requires your approval.
- **Repairs autonomously** when reality changes: a meeting dropped onto a
  work block triggers watch → sync → classification → minimal repair — the
  fewest blocks move the smallest distance, unaffected blocks stay
  byte-identical, and the repair lands in **~9 seconds** (measured across
  ten consecutive live campaign runs; budget 15 s warmed / 60 s operational).
- **Escalates instead of pretending**: out-of-policy repairs (e.g. more than
  24 hours of total displacement) become explicit approvals; infeasibility, reauthorization, and every
  failure state are visible in the dashboard — never hidden behind an
  "on track" badge.
- **Tracks honest progress**: elapsed time never counts as work. Only
  explicit check-ins add verified minutes, and completion is an explicit
  terminal act that never fabricates minutes to match the estimate.
- **Explains everything**: a full decision timeline records every
  observation, interpretation, policy decision, outbox write, executor
  result, and control change.

## Architecture

![Architecture](Plan_Final/CommitmentOS_P0_Architecture.png)

One Python/FastAPI service on **Cloud Run** (public IAM edge because Google
Calendar must reach an HTTPS webhook; every route enforces its own trust
contract). **Firestore** owns all durable state. **Pub/Sub** carries Gmail
watch notifications only. Three **Cloud Tasks** queues carry source
synchronization, reconciliation, and Calendar-action execution as named,
idempotent tasks. **Cloud Scheduler** drives watch renewal, cursor catch-up,
dispatch repair, and the once-a-minute safety reconciliation.
**Secret Manager** holds separate production and public-sandbox Gemini keys,
the OAuth client, and the controlled account's refresh token. The sandbox key
is issued from a sandbox-only Gemini quota project so unauthenticated traffic
cannot consume the controlled-data extraction quota.

Every reconciliation run is a **bounded ADK graph invocation** with two honest
stages: execute the durable reconciliation controller once, then finalize a
safe run summary. The controller contains the explicit interpretation,
identity, evidence, portfolio, policy, and outbox stages; the ADK graph does
not pretend those in-process stages are separate graph nodes. External
Calendar mutation happens only in a separate replay-safe Cloud Tasks
executor using stable client-supplied event IDs, `If-Match` preconditions,
and revision/epoch guards. Nothing waits in memory; the dashboard, approvals,
and action results continue through new durable observations.

| Route class | Trust contract |
|---|---|
| `/app`, `/api/v1/*` | Google OAuth login (controlled account allowlist) → opaque server-side session cookie; CSRF token on every mutation |
| `/internal/tasks/*`, `/internal/scheduler/*`, `/internal/pubsub/*` | Google-signed OIDC: exact audience + per-group service-account identity; Gmail change signals are durably rate-limited |
| Calendar webhook | High-entropy channel token (constant-time hash compare) + channel/resource mapping + active overlap status + stored expiry + durable per-channel rate limit |
| `/demo` | Read-only seeded judge mode; static data, no Firestore/credential path, every mutation method rejected |
| `/sandbox` | Interactive judge sandbox; unauthenticated by design. Its state and every mutation port are in-memory twins; its sole external edge is a no-tools Gemini adapter with a distinct sandbox key/client/quota pool. No Firestore client, controlled-user credential/document, or production interpreter is reachable. Session addressed by explicit header (no ambient authority); mutually exclusive guided/free-play lanes; concurrent worlds, creation rate, idle/absolute lifetime, message length, per-session use, rolling traffic/model calls, concurrency, deadlines, and guided cache all capped |

### Stack

| Component | Choice |
|---|---|
| Model | Gemini `gemini-3.5-flash` via the Gemini API (structured outputs, evidence-quote anchoring, `thinking_level: low`) |
| Agent framework | Google ADK 2.6.3 (Python graph `Workflow`, bounded invocations) |
| Runtime | Python 3.14 + FastAPI on Cloud Run |
| State | Firestore (Native mode) — transactions, composite indexes, fenced leases |
| Transport | Pub/Sub (Gmail push only) + Cloud Tasks ×3 + Cloud Scheduler ×4 |
| Frontend | React 19 + TypeScript + Vite, compiled to static assets served by the same service |

## Judge mode

Two zero-login surfaces, neither of which touches live data. They
deliberately tell **different stories**: `/demo` mirrors the measured golden
scenario the live evidence was earned on, while `/sandbox` runs a separately
authored thread (plus free play) — so the proofs reinforce each other without
the agent being hard-coded to a single conversation.

**`/sandbox` — drive it yourself.** Choose one of two isolated lanes per reset:
the authored end-to-end story, or a free-play email thread with a visible
subject. In free play, choose Jordan/You and write your own message (including
several consecutive messages from the same person); once the resulting
commitment is planned, the same conflict, time-advance, and check-in controls
remain available for that judge-authored work. In the guided lane, send the
request, accept it, move the deadline, drop a meeting on
top of a reserved block, let time pass, log your verified minutes — and watch
the agent extract, converge, plan, repair, and refuse to inflate progress in
response. Everything on the right-hand side is produced by the production
stack: the same interpreter implementation/prompt contract, identity resolver,
portfolio planner, policy engine, executor, and audit timeline that serve the
controlled user. The public model instance and key are deliberately separate.
What is simulated is the world around it — Firestore is a dict, Gmail is a scripted
mailbox, Calendar is an in-memory store with Google's etag and
cancelled-event semantics, Cloud Tasks is a list, and the clock is
advanceable. Each visitor gets a private world; sessions cannot see each
other, state reads do not extend their life, and worlds expire after 45 idle
minutes or two hours absolutely. Expiry has its own scheduled deletion, so
private text is released even if no later request arrives to trigger cleanup.

Interpretation is the one deliberately live edge. For a guided card, the first
call for a complete thread/candidate context is real Gemini output and can be
reused from the bounded process cache. If the model is unavailable, or a
schema-valid variation violates that card's authored ownership, identity,
outcome, or deadline contract, the guided story falls back to its recorded
interpretation and labels the path. Judge-authored messages are different:
they never enter the shared cache, they run live, and an outage/rejection is
shown as `custom-unavailable`; a canned interpretation is never substituted.
Custom source and structured results live only in that process-local world
until reset/expiry. Custom input is limited to 1,000 characters, 8 messages per
session, and a rolling 12 requests/minute per instance. Session creation, all
model methods together, model concurrency, and per-session model use have
independent bounds. The documented two-instance deployment ceiling plus the
sandbox-only quota project bound aggregate spend. Sandbox calls have a
20-second transport/application deadline and no SDK retry; only an HTTP-400
unsupported-thinking response gets one compatibility retry without thinking.
The extraction model has no tools, and the production deterministic
validator is unchanged on both paths — evidence quotes must be exact
substrings of the source message. This public edge is a separate interpreter
and lazy client backed by `COMMITMENTOS_SANDBOX_GEMINI_API_KEY_SECRET_REF`;
production refuses to boot if that reference is absent or equals the
controlled-data Gemini reference. The referenced key must be issued by a
sandbox-only quota project.

**`/demo` — read the dashboard.** The full dashboard rendering seeded data
derived from a fixed demonstration scenario. It is read-only by
construction: the demo read model has no Firestore, credential, or Google
API access path, and every mutation method under `/demo` is rejected before
any handler logic. No login required.

## Repository layout

```
backend/src/commitmentos/   the service: api/ application/ domain/
                            infrastructure/ workflows/ contracts/ bootstrap/
             .../sandbox/    the interactive judge sandbox: the in-memory twin
                            (twin.py), one isolated world composing the real
                            stack over it (world.py), guided card deck and its
                            recorded interpretations (scenario.py), bounded
                            judge-authored messages (session.py/interpreter.py)
backend/tests/              unit + integration + contract suites over an
                            in-memory Firestore twin (production repo code
                            runs unmodified against it)
tests/fault_injection/      §16.4 fault matrix (worker kill, lease takeover,
                            executor death, projection corruption)
tests/golden_path/          local rehearsal of the golden campaign
frontend/                   React dashboard (Today / Commitments / Activity)
                            plus the sandbox view; one bundle serves /app,
                            /demo, and /sandbox
scripts/                    gate drivers, golden campaign, security probes,
                            eval runner, ops helpers
docs/                       phase progress, evidence packs, measured results
infra/firestore/            composite index definitions
Plan_Final/                 the authoritative build plan + architecture
```

## Local spin-up

Prerequisites: Python 3.14, [uv](https://docs.astral.sh/uv/), Node 22+.

```bash
# 1. Backend dependencies
uv sync

# 2. Configuration
cp .env.example .env        # then fill in your project's values
                            # (every variable maps 1:1 to bootstrap/settings.py)

# 3. Tests — 304 tests over the in-memory Firestore twin; no cloud access
uv run pytest

# 4. Frontend
cd frontend && npm ci && npm run build && cd ..

# 5. Run the service (serves API + compiled dashboard).
#    Boot reads the OAuth client secret from Secret Manager (a deliberate
#    fail-fast), so a local run needs Application Default Credentials for
#    the project. On macOS, gRPC's default DNS resolver can fail to reach
#    Google endpoints — use the native resolver:
GRPC_DNS_RESOLVER=native uv run uvicorn --app-dir backend/src \
  commitmentos.main:create_app --factory --port 8080
```

The test suite runs entirely against the in-memory Firestore twin — no
cloud access, no credentials. Running the service itself (including the
seeded `/demo` views) requires the project's Secret Manager to be
reachable; the hosted deployment is the zero-setup way to view `/demo`.

Troubleshooting (macOS): some `uv` builds write the venv's `.pth` files
with the macOS hidden file flag set, and Python 3.13+ silently skips
hidden `.pth` files (`python -v` shows "Skipping hidden .pth file"),
breaking `import commitmentos` outside the uvicorn command above. The
`--app-dir backend/src` flag makes the server immune; for other tools run
`chflags nohidden .venv/lib/python*/site-packages/*.pth` after a sync.

## Cloud deployment

```bash
# One-time project setup (region us-west1 in this deployment)
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com firestore.googleapis.com \
  cloudtasks.googleapis.com pubsub.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com \
  gmail.googleapis.com calendar-json.googleapis.com

# Firestore (Native), three Cloud Tasks queues, the Gmail watch topic (with
# gmail-api-push@system.gserviceaccount.com granted Publisher), four service
# accounts (runtime, pubsub, tasks, scheduler with run.invoker), and five
# secrets (OAuth client, controlled refresh token, controlled-data Gemini key,
# sandbox-only Gemini key, calendar channel token) — resource names are
# referenced from .env.example. The sandbox key must come from a separate
# Gemini quota project; only its Secret Manager resource belongs here.

# Composite indexes
# (create the definitions in infra/firestore/indexes.json)

# Set COMMITMENTOS_SANDBOX_GEMINI_API_KEY_SECRET_REF in the local .env, then
# use the owner-only release gate. It builds the dashboard/service, deploys
# with affinity and maxScale=2, explicitly routes 100% to latest, and fails if
# Cloud Run or the served custom-message API/UI differs from that contract.
# The deploy path also sends one sandbox-only model probe to prove the new
# secret is accessible and its key works; it never touches controlled data.
.venv/bin/python scripts/deploy_commitmentos.py --deploy

# Read-only repeat of the post-deploy checks.
.venv/bin/python scripts/deploy_commitmentos.py

# Scheduler jobs (dispatch repair, watch renewal, cursor catch-up, safety)
bash scripts/create_scheduler_jobs.sh

# Register the Gmail + Calendar watches with the controlled credential
python scripts/gmail_watch_spike.py register
python scripts/calendar_watch_spike.py register --service-url <SERVICE_URL>
```

Session affinity keeps a process-local sandbox world on the instance that
created it; the browser still recovers cleanly after recycle or idle expiry.
The two-instance ceiling bounds aggregate public free-play traffic. Cloud Run
remains IAM-edge public for the Calendar webhook, while application-layer
contracts guard every route. Deploys and traffic changes are owner-run.

The app does not need to stay publicly live outside judging; it scales to
zero. All measured evidence in `docs/` was produced against the deployed
service. [`docs/proof_index.md`](docs/proof_index.md) indexes every claim —
which revision it was measured on and which frozen evidence file records it.

### Source serialization, cursor recovery, and publication

Gmail Pub/Sub notifications are change signals, not message payloads. The
receiver durably coalesces concurrent delivery to the greatest observed
history ID. Actual source work is serialized by a fenced
`source-sync:gmail:<user>` lease, so only one Gmail generation for the
controlled user can advance the cursor at a time; duplicate named tasks
resume or converge on the same generation.

An incremental `history.list` 404 means the published Gmail cursor is no
longer usable. The failed generation is abandoned, the cursor is marked for
full resynchronization, and a named full-resync task is queued automatically.
That generation scans Inbox and Sent only. Before its first mailbox page it
captures the profile history ID as the next incremental baseline, so mail
arriving during a multi-page scan is replayed after publication instead of
falling into a scan/cursor race. Calendar invalid sync tokens follow the same
automatic full-generation path over the configured time horizon, including
tombstones for previously published events that disappeared.

Both sources use bounded staging generations:

- One provider page is fetched per named Cloud Task. Normalized generation
  items, page checkpoints, and commutative manifests make retries independent
  of worker lifetime and chunk boundaries.
- Each transaction is capped by configuration at 400 writes and 8 MiB; apply
  chunks are additionally capped at 100 items (the defaults in
  `.env.example`). Transaction overhead is reserved before item writes.
- Observations remain `staged`, and Calendar snapshot data remains behind a
  publication barrier, while a generation is incomplete. Reconciliation and
  planning therefore cannot consume a half-applied source view.
- A single fenced compare-and-set publication transaction verifies staged and
  applied manifests, promotes the candidate cursor, advances the cursor
  revision, and clears the barrier. Only then are observations released in
  bounded batches. The previously published cursor remains authoritative
  after any pre-publication crash.

### Authoritative facts versus projections

Commitments, confirmed effort/deadlines, evidence, work-block execution state,
verified minutes, Calendar snapshots, approvals, controls, and outbox records
are authoritative revisioned facts. Portfolio allocation, remaining effort,
risk, projected finish, shortfall, and shared buffer are replaceable read
projections. Every projection carries its source commitment revision,
work-block revision hash, planner-run ID, and calculator version; the dashboard
exposes allocation fields only while that provenance is current. A stale or
missing projection is shown as unknown, never promoted into fact.

Before external I/O the outbox executor independently reloads authoritative
revisions, the execution-control epoch, ownership, and observed Calendar etag.
Stable event IDs make create replay-safe; patch/cancel use `If-Match`, and an
HTTP 412 stales the intent and requests a fresh source sync. Action results
return as new observations rather than being inferred from queued intent.

### Seeded demo, live seed, reset, and evaluation

`/demo` needs no database seed or reset command. It reads packaged scenario
documents from `backend/src/commitmentos/demo_data/` through a read-only model
with no Firestore or Google credential adapter, so every request starts from
the same data and mutations are impossible.

`/sandbox` needs no seed command either: each session builds its own world in
memory and drops it on idle expiry or instance recycle. "Start over" calls the
bounded reset endpoint, releasing the old world before opening its replacement.
Cloud Run session affinity is required by the deployment command because these
demonstration worlds are intentionally process-local. The sandbox's behavior is
pinned by `backend/tests/integration/test_sandbox_flow.py` (the story must
extract, converge onto one commitment, plan, repair a conflict, and verify
honest minutes) and `backend/tests/contract/test_sandbox_contracts.py`
(isolation, sender/text validation, free-play and card caps, serialization,
automatic expiry, reset, and route precedence).

For an end-to-end seed against the deployed service and real Calendar, use the
audited live driver. Its generated run tag scopes cleanup:

```bash
.venv/bin/python scripts/run_seeded_slice.py run --cleanup
.venv/bin/python scripts/run_seeded_slice.py run --run-tag <tag>
.venv/bin/python scripts/run_seeded_slice.py cleanup --run-tag <tag>
```

For a full controlled-account reset, preview the exact owned events and domain
document counts, then copy the printed revision-bound confirmation phrase:

```bash
.venv/bin/python scripts/reset_controlled_account.py preview
.venv/bin/python scripts/reset_controlled_account.py run \
  --confirm "cleanup <user_id> events=N documents=M"
```

The extraction evaluation calls the pinned production Gemini model, prompt,
wire schema, and deterministic validator. It writes a new result under
`docs/phase2_evidence/`; do not overwrite the recorded evidence pack:

```bash
.venv/bin/python scripts/run_extraction_eval.py
.venv/bin/python scripts/run_extraction_eval.py --limit 5
.venv/bin/python scripts/run_extraction_eval.py --category prompt_injection
```

### Post-audit hardening status

The current source includes a post-campaign hardening pass recorded in
`docs/post_audit_hardening_progress.md`. It removes misleading unused
scaffolding and closes cursor recovery, channel validation, explanation,
reopen/priority, policy, audit, outbox, and failure-state gaps. The immutable
evidence packs still describe the previously deployed revision; this newer
source must pass a fresh owner-run live/security campaign before its results
replace or extend those records.

## OAuth: publishing mode, scopes, limitations

- **Mode:** External / In production / **unverified personal use** — chosen
  after an up-front integration spike proved authorization, refresh, watch renewal,
  revocation, and allowlisting end to end. Refresh tokens do not carry the
  Testing-mode 7-day expiry; the unverified-app warning is acknowledged for
  the single controlled account. This is explicitly **not** a claim of
  public verification or multi-user readiness.
- **Scopes (exactly four):** `openid`, `userinfo.email`,
  `calendar.events` (sensitive), `gmail.readonly` (restricted). No send, no
  modify, no Drive.
- **Access:** the application allowlists a single controlled account; any
  other Google identity completes sign-in and receives `account not allowed`
  with no session. Revocation is grant-wide; the UI surfaces
  `reauth_required` rather than silently using stale data.
- **Testing-mode fallback:** if the consent screen must return to External /
  Testing, treat the restricted-scope refresh token as expiring after seven
  days. Reconnect at least every seven days, immediately before the golden
  campaign, and again before recording:

  ```bash
  .venv/bin/python scripts/oauth_spike.py authorize --mode testing
  .venv/bin/python scripts/oauth_spike.py refresh
  .venv/bin/python scripts/gmail_watch_spike.py register
  .venv/bin/python scripts/calendar_watch_spike.py register --service-url <SERVICE_URL>
  ```

  The authorize command stores the new refresh token in Secret Manager and
  never prints it. In the selected In-production personal-use mode there is
  no seven-day cadence; reconnect is event-driven whenever
  `reauth_required` appears, then renew both watches with the same commands.
- Production multi-tenant token storage is out of scope by design; the
  controlled account's refresh token lives in Secret Manager.

## Evidence and measured results

- **End-to-end campaign:** ten consecutive passing runs of the
  full scenario against the deployed service — live Gemini interpretation
  each run, 61/61 checkpoints, conflict-to-repair 7.2–10.2 s (mean 9.1 s),
  honest verified minutes, byte-identical replay of every observation and
  action. Per-run JSON + summary: `docs/phase5_evidence/`.
- **Extraction evaluation (§16.1):** 32 labeled fixtures across 12
  categories including prompt injection — 100% schema validity, ownership,
  deadline accuracy, and injection containment at ~$0.0008/message:
  `docs/phase2_evidence/`.
- **Live security probes:** session negative matrix, CSRF on every mutation
  route, wrong OIDC audience/identity on all three internal groups, the
  full `/demo` mutation matrix, and the `/sandbox` isolation matrix (no
  session, forged session, off-deck input, cross-session visibility, and
  controlled-identifier leakage) — all green against the deployed revision.
- **Fault injection (§16.4):** worker kill + fenced-lease takeover, executor
  death before/after the Calendar response, create-before-record crash
  convergence, projection corruption blocking stale execution.
- Phase-by-phase gate records with sanitized live evidence:
  `docs/phase*_evidence/`, `docs/Phase_0_Integration_Risk_Spike_Checklist.md`.

## Security and privacy

- Email bodies are processed transiently and never persisted or logged;
  the ledger stores IDs, minimal excerpts, structured fields, and hashes.
- Source text enters the model as delimited untrusted data (with delimiter
  neutralization); the extraction agent has no mutation tools; model output
  must satisfy a strict schema, and evidence quotes must be exact substrings
  of the source — the anti-injection anchor.
- Structured logs pass a redactor (no tokens, cookies, bodies, or addresses);
  verified against live Cloud Logging at every phase gate.
- The executor mutates only events carrying CommitmentOS ownership
  properties, and patch/cancel always sends `If-Match`; a 412 can only mark
  intent stale and trigger resynchronization.
- The interactive sandbox is unauthenticated, so its safety is structural
  rather than procedural: all state and mutation ports are in-memory twins,
  and its session id travels in an explicit header rather than a cookie
  (nothing the browser sends automatically carries authority there). Its one
  external capability is a no-tools model adapter with a separate interpreter,
  client, secret, and quota project; the production container graph and every
  controlled-user credential/document remain unreachable. Free-play text can
  only enter as Jordan or You; length, per-session volume, rolling traffic, and
  the in-memory cache are capped. Arbitrary text never receives a recorded card
  fallback, and all model output still crosses strict schema, evidence-anchor,
  identity, policy, and ownership boundaries before it can mutate even the
  simulated world.

## Known limitations

- Single controlled user and calendar by design for this release; multi-user OAuth
  onboarding, token vaulting, and verification are out of scope.
- Sent-email completion inference is deliberately excluded — completion is
  an explicit user act.
- The unverified-app consent warning is part of the personal-use OAuth mode.
- Canvas, dependencies, follow-up drafts, and other P1+ features are
  documented in the plan but intentionally unbuilt.
