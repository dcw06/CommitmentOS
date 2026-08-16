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
- **Escalates instead of pretending**: out-of-policy repairs (e.g. a >24-hour
  shift) become explicit approvals; infeasibility, reauthorization, and every
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
**Secret Manager** holds the Gemini key, OAuth client, and the controlled
account's refresh token.

Every reconciliation run is a **bounded ADK graph invocation**: it loads one
durable observation, interprets (Gemini node) and decides (deterministic
nodes), persists intent or an approval request, and terminates. External
Calendar mutation happens only in a separate replay-safe Cloud Tasks
executor using stable client-supplied event IDs, `If-Match` preconditions,
and revision/epoch guards. Nothing waits in memory; the dashboard, approvals,
and action results continue through new durable observations.

| Route class | Trust contract |
|---|---|
| `/app`, `/api/v1/*` | Google OAuth login (controlled account allowlist) → opaque server-side session cookie; CSRF token on every mutation |
| `/internal/tasks/*`, `/internal/scheduler/*`, `/internal/pubsub/*` | Google-signed OIDC: exact audience + per-group service-account identity |
| Calendar webhook | High-entropy channel token (constant-time hash compare) + channel/resource mapping + durable per-channel rate limit |
| `/demo` | Read-only seeded judge mode; static data, no Firestore/credential path, every mutation method rejected |

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

The hosted service exposes **`/demo`** — the full dashboard rendering seeded
data derived from a fixed demonstration scenario. It is read-only by
construction: the demo read model has no Firestore, credential, or Google
API access path, and every mutation method under `/demo` is rejected before
any handler logic. No login required.

## Repository layout

```
backend/src/commitmentos/   the service: api/ application/ domain/
                            infrastructure/ workflows/ contracts/ bootstrap/
backend/tests/              unit + integration + contract suites over an
                            in-memory Firestore twin (production repo code
                            runs unmodified against it)
tests/fault_injection/      §16.4 fault matrix (worker kill, lease takeover,
                            executor death, projection corruption)
tests/golden_path/          local rehearsal of the golden campaign
frontend/                   React dashboard (Today / Commitments / Activity)
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

# 3. Tests — 224 tests over the in-memory Firestore twin; no cloud access
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
# accounts (runtime, pubsub, tasks, scheduler with run.invoker), and four
# secrets (OAuth client, controlled refresh token, Gemini key, calendar
# channel token) — resource names are referenced from .env.example.

# Composite indexes
# (create the definitions in infra/firestore/indexes.json)

# Deploy — the Dockerfile builds the dashboard (Node stage) and the service
gcloud run deploy commitmentos --source . --region us-west1 \
  --allow-unauthenticated   # IAM-edge public for the Calendar webhook;
                            # application-layer contracts guard every route

# Scheduler jobs (dispatch repair, watch renewal, cursor catch-up, safety)
bash scripts/create_scheduler_jobs.sh

# Register the Gmail + Calendar watches with the controlled credential
python scripts/gmail_watch_spike.py register
python scripts/calendar_watch_spike.py register --service-url <SERVICE_URL>
```

The app does not need to stay publicly live outside judging; it scales to
zero. All measured evidence in `docs/` was produced against the deployed
service.

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
  full `/demo` mutation matrix — all green against the deployed revision.
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

## Known limitations

- Single controlled user and calendar by design for this release; multi-user OAuth
  onboarding, token vaulting, and verification are out of scope.
- Sent-email completion inference is deliberately excluded — completion is
  an explicit user act.
- The unverified-app consent warning is part of the personal-use OAuth mode.
- Canvas, dependencies, follow-up drafts, and other P1+ features are
  documented in the plan but intentionally unbuilt.
