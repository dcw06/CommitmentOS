"""Phase 5B golden-run campaign driver — the §6.2 hard gate.

Runs the golden scenario end to end against the DEPLOYED service: seed the
portfolio context, drive the golden commitment through detection, effort
confirmation, plan approval, real Calendar events, a verified check-in, a
live conflict repair, elapse, explicit completion, full replay convergence,
and the frozen audit-order contract — then writes measured §16.5 results to
`docs/phase5_evidence/`. `campaign` repeats the run with the audited
cleanup reset between runs; a failed run stops the campaign (ten must be
consecutive).

Modes:
  --thread   (gate mode, default when a thread baseline exists) re-observes
             the real golden thread in the controlled mailbox, so every run
             exercises live Gemini interpretation. A static thread renders
             in full, so the M1→M2→M3 staged evolution appears as one
             interpretation plus convergent replays; the staged evolution
             itself was proven live at the Phase 2 gate (recorded
             deviation, mirrored in docs/phase5_evidence/README.md).
  --seeded   date-shifted seeded observation (no model call); used by the
             local rehearsal and as a degraded fallback only.

Recorded stand-ins (same conventions as the Phase 3/4 gates): the minted
web session stands in for browser login; block elapse uses the production
domain transition driven by the script (the scenario clock spans days the
campaign compresses); the conflict is inserted through the Calendar API
per the owner-approved Phase 4 convention (the demo video uses the UI).

Usage:
    .venv/bin/python scripts/run_golden_path.py mint-session
    .venv/bin/python scripts/run_golden_path.py find-thread --subject "Proposal Revision"
    .venv/bin/python scripts/run_golden_path.py busy-setup
    .venv/bin/python scripts/run_golden_path.py preflight
    .venv/bin/python scripts/run_golden_path.py run --number 1
    .venv/bin/python scripts/run_golden_path.py campaign --count 10
    .venv/bin/python scripts/run_golden_path.py reset
    .venv/bin/python scripts/run_golden_path.py busy-cleanup
    .venv/bin/python scripts/run_golden_path.py cleanup-session
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.application.commands.cleanup_controlled_account import (  # noqa: E402
    CleanupControlledAccount,
)
from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.contracts.observations import ObservationType  # noqa: E402
from commitmentos.domain.shared.types import CanonicalEncoder  # noqa: E402
from commitmentos.infrastructure.firestore.cleanup import (  # noqa: E402
    FirestoreCleanupDocumentStore,
)
from commitmentos.infrastructure.firestore.repositories.implementations import (  # noqa: E402
    ACTION_OUTBOX,
    ACTIVITY_EVENTS,
    APPROVALS,
    COMMITMENTS,
    EVIDENCE,
    PLANNER_RUNS,
    SOURCE_OBSERVATIONS,
    SYNC_CURSORS,
    SYNC_GENERATIONS,
    SYNC_REQUESTS,
    WORK_BLOCKS,
)
from commitmentos.infrastructure.google.calendar_reader import GoogleCalendarReader  # noqa: E402
from commitmentos.infrastructure.google.credentials import (  # noqa: E402
    ControlledCredentialsProvider,
)

SESSION_FILE = Path.home() / ".commitmentos_phase5b_session.json"
BASELINE_FILE = Path.home() / ".commitmentos_golden_campaign.json"
EVIDENCE_DIR = Path(__file__).resolve().parents[1] / "docs" / "phase5_evidence"

# Weekday-neutral variant of the checklist §1 canonical note: the live
# campaign thread date-shifts the scenario's weekday words to stay coherent
# from the actual send day (recorded deviation in docs/phase5_evidence/).
CANONICAL_COMPLETION_NOTE = (
    "Sent the revised proposal to Professor Chen ahead of the review."
)
CONFLICT_SUMMARY = "Golden campaign — department meeting (conflict)"
WARMED_LATENCY_BUDGET_SECONDS = 15.0
OPERATIONAL_LATENCY_BUDGET_SECONDS = 60.0

_checkpoints: list[tuple[str, bool, str]] = []


def checkpoint(label: str, ok: bool, detail: str = "") -> None:
    _checkpoints.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def reset_checkpoints() -> None:
    _checkpoints.clear()


def failures() -> list[str]:
    return [label for label, ok, _ in _checkpoints if not ok]


class GoldenPathRunner:
    """One golden run against the deployed service, plus campaign plumbing."""

    def __init__(self, settings: Settings, service_url: str, run_tag: str) -> None:
        self.settings = settings
        self.service_url = service_url.rstrip("/")
        self.run_tag = run_tag
        self.container = ApplicationContainer.build(settings)
        self.uow = self.container.unit_of_work()
        self.credentials = ControlledCredentialsProvider(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )
        self.calendar_reader = GoogleCalendarReader(self.credentials)
        self.golden_commitment_id: str | None = None
        self.second_commitment_id: str | None = None
        self.run_started_at = datetime.now(timezone.utc)

    # -- shared plumbing ----------------------------------------------------

    def local_tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.controlled_timezone)

    async def query(self, collection: str, filters: list[tuple[str, str, Any]]) -> list:
        async def _load(repositories):  # noqa: ANN001, ANN202
            return await repositories._context.query(collection, filters)  # noqa: SLF001

        return await self.uow.read(_load)

    async def get_raw(self, collection: str, document_id: str) -> Mapping[str, Any] | None:
        async def _load(repositories):  # noqa: ANN001, ANN202
            return await repositories._context.get(collection, document_id)  # noqa: SLF001

        return await self.uow.read(_load)

    def user_filter(self) -> list[tuple[str, str, Any]]:
        return [("user_id", "==", self.settings.controlled_user_id)]

    def load_session(self) -> tuple[str, str]:
        data = json.loads(SESSION_FILE.read_text())
        return data["session_token"], data["csrf_secret"]

    async def api_post(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        token, csrf = self.load_session()
        async with httpx.AsyncClient(timeout=90) as client:
            return await client.post(
                f"{self.service_url}{path}",
                json=dict(body),
                cookies={"commitmentos_session": token},
                headers={"X-CSRF-Token": csrf},
            )

    def tasks_identity_token(self) -> str:
        result = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-identity-token",
                f"--impersonate-service-account={self.settings.tasks_service_account}",
                f"--audiences={self.service_url}/internal/tasks",
                "--include-email",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    async def task_post(self, path: str, body: Mapping[str, Any], token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=90) as client:
            return await client.post(
                f"{self.service_url}{path}",
                json=dict(body),
                headers={"Authorization": f"Bearer {token}"},
            )

    def calendar_service(self):  # noqa: ANN201 - third-party client
        from googleapiclient.discovery import build

        return build(
            "calendar",
            "v3",
            credentials=self.credentials.credentials(),
            cache_discovery=False,
        )

    def gmail_service(self):  # noqa: ANN201 - third-party client
        from googleapiclient.discovery import build

        return build(
            "gmail",
            "v1",
            credentials=self.credentials.credentials(),
            cache_discovery=False,
        )

    def baseline(self) -> dict:
        if BASELINE_FILE.exists():
            return json.loads(BASELINE_FILE.read_text())
        return {}

    def save_baseline(self, data: dict) -> None:
        BASELINE_FILE.write_text(json.dumps(data, indent=2, default=str))
        BASELINE_FILE.chmod(0o600)

    # -- observation seeding -------------------------------------------------

    async def _commit_and_dispatch(self, observation) -> str:  # noqa: ANN001
        async def _create(repositories):  # noqa: ANN001, ANN202
            return await repositories.observations.create(observation)

        created = await self.uow.run(_create)
        if not created:
            # Re-run over an identical content-stable observation after an
            # incomplete reset: re-dispatch so processing still happens.
            print(f"observation {observation.observation_id[:12]}… already exists")
        await self.container.observation_dispatcher.dispatch(observation.observation_id)
        return observation.observation_id

    async def seed_thread_observation(self, message_id: str, thread_id: str) -> str:
        factory = self.container.observation_factory
        observation = factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=self.settings.controlled_user_id,
            producer_id=f"{self.settings.controlled_user_id}:{message_id}",
            producer_version=f"golden-campaign-{self.run_tag}",
            source="gmail",
            external_id=message_id,
            external_version=f"golden-campaign-{self.run_tag}",
            payload_hash=f"golden-campaign-{self.run_tag}",
            source_reference={"thread_id": thread_id, "message_id": message_id},
            safe_metadata={},
            observed_at=datetime.now(timezone.utc),
            trace_id=f"trace-golden-{self.run_tag}",
        )
        return await self._commit_and_dispatch(observation)

    async def seed_seeded_observation(self, payload: Mapping[str, Any], label: str) -> str:
        factory = self.container.observation_factory
        observation = factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=self.settings.controlled_user_id,
            producer_id=f"{self.settings.controlled_user_id}:{label}_{self.run_tag}",
            producer_version=f"seeded-{self.run_tag}",
            source="gmail",
            external_id=f"{label}_{self.run_tag}",
            external_version=f"seeded-{self.run_tag}",
            payload_hash=f"seeded-{self.run_tag}",
            source_reference={
                "thread_id": payload["source_thread_id"],
                "message_id": f"{label}_{self.run_tag}",
            },
            safe_metadata={"seeded_commitment": dict(payload)},
            observed_at=datetime.now(timezone.utc),
            trace_id=f"trace-golden-{self.run_tag}",
        )
        return await self._commit_and_dispatch(observation)

    def second_commitment_payload(self, deadline_days: int) -> dict:
        tz = self.local_tz()
        deadline = (datetime.now(tz) + timedelta(days=deadline_days)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        thread = f"thread_golden_data_summary_{self.run_tag}"
        span = f"message_golden_data_summary_{self.run_tag}:span0"
        self.second_commitment_id = CanonicalEncoder.hash(
            ["commitment:v1", self.settings.controlled_user_id, thread, span]
        )
        return {
            "source_thread_id": thread,
            "source_span_key": span,
            "title": "Prepare Q3 data summary for Sam",
            "description": "Golden-scenario portfolio context (seeded)",
            "ownership_type": "my_commitment",
            "beneficiary": "Sam",
            "deadline_value": deadline.isoformat(),
            "deadline_expression": "ready for Monday's sync (seeded)",
            "deadline_confidence": 0.9,
            "timezone": self.settings.controlled_timezone,
            "proposed_effort_minutes": 120,
            "effort_confidence": 0.7,
            "semantic_fingerprint": "my_commitment:q3-data-summary:sam",
            "evidence_excerpt": "Can you have the Q3 data summary ready for Monday's sync?",
        }

    def golden_seeded_payload(self, deadline_days: int) -> dict:
        tz = self.local_tz()
        deadline = (datetime.now(tz) + timedelta(days=deadline_days)).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        thread = f"thread_golden_proposal_{self.run_tag}"
        span = f"message_golden_proposal_{self.run_tag}:span0"
        self.golden_commitment_id = CanonicalEncoder.hash(
            ["commitment:v1", self.settings.controlled_user_id, thread, span]
        )
        return {
            "source_thread_id": thread,
            "source_span_key": span,
            "title": "Send revised proposal to Professor Chen",
            "description": "Golden scenario (seeded fallback mode)",
            "ownership_type": "my_commitment",
            "beneficiary": "Professor Chen",
            "deadline_value": deadline.isoformat(),
            "deadline_expression": "bring the review forward to Thursday at 4 p.m.",
            "deadline_confidence": 0.93,
            "timezone": self.settings.controlled_timezone,
            "proposed_effort_minutes": 180,
            "effort_confidence": 0.58,
            "semantic_fingerprint": "my_commitment:send-revised-proposal:professor-chen",
            "evidence_excerpt": "Yes—I'll have it back before our Friday 4 p.m. review.",
        }

    # -- waits ---------------------------------------------------------------

    async def wait_for(self, label: str, probe, timeout: float = 90.0, interval: float = 3.0):  # noqa: ANN001
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        last_rescue = started
        while time.monotonic() < deadline:
            value = await probe()
            if value:
                checkpoint(label, True, "")
                return value
            now = time.monotonic()
            if now - started >= 15.0 and now - last_rescue >= 20.0:
                last_rescue = now
                await self.rescue_stuck_transport()
            await asyncio.sleep(interval)
        checkpoint(label, False, f"timeout {timeout}s")
        raise TimeoutError(label)

    # -- transport rescue ----------------------------------------------------

    def _cached_tasks_token(self) -> str:
        cached = getattr(self, "_tasks_token_cache", None)
        if cached and time.monotonic() - cached[1] < 2400:
            return cached[0]
        token = self.tasks_identity_token()
        self._tasks_token_cache = (token, time.monotonic())
        return token

    async def rescue_stuck_transport(self, min_age_seconds: float = 15.0) -> None:
        """Deliver committed-but-undelivered work directly to the OIDC handlers.

        Back-to-back campaign runs reuse deterministic domain IDs (same thread
        → same commitment → same approval → same continuation observation), so
        a generation-0 task name can fall inside Cloud Tasks' 24-hour name
        retention from the previous run, and the enqueue silently converges on
        the consumed name (observed live 2026-08-15: continuation observation
        stuck `queued`/attempts=0 across five dispatch_pending ticks). Task
        names are a transport deduplication aid only (plan §7.3); the
        observation CAS and the executor's guarded claim are the product
        idempotency boundaries, so direct delivery through the replay-contract
        path changes no product semantics.
        """
        now = datetime.now(timezone.utc)

        def _stale(doc: Mapping[str, Any], key: str) -> bool:
            at = doc.get(key)
            if at is None:
                return True
            return (now - self._dt(at)).total_seconds() >= min_age_seconds

        try:
            token = ""
            for observation_id, doc in await self.query(
                SOURCE_OBSERVATIONS, self.user_filter()
            ):
                lease_expiry = doc.get("processing_lease_expires_at")
                lease_active = bool(
                    doc.get("processing_lease_owner")
                    and lease_expiry
                    and lease_expiry.timestamp() > time.time()
                )
                # attempts>=1 included: a first delivery that draws a retryable
                # 503 (reset cancel-echo sync barrier) backs off past every
                # driver wait budget; redelivery is replay-safe (no_op on
                # convergence), so rescue anything idle without a live lease.
                if (
                    doc.get("reconciliation_status") in ("pending", "queued")
                    and not lease_active
                    and _stale(doc, "observed_at")
                ):
                    token = token or self._cached_tasks_token()
                    await self.task_post(
                        "/internal/tasks/reconcile-observation",
                        {
                            "schema_version": self.settings.task_schema_version,
                            "observation_id": observation_id,
                            "workflow_version": doc.get(
                                "workflow_version", self.settings.workflow_version
                            ),
                            "dispatch_generation": doc.get("dispatch_generation", 0),
                            "trace_id": f"golden-rescue-{observation_id[:8]}",
                        },
                        token,
                    )
                    print(
                        f"      transport rescue: observation {observation_id[:12]}… "
                        "delivered directly (task name consumed by a prior run)"
                    )
            for outbox_id, doc in await self.query(ACTION_OUTBOX, []):
                if (
                    doc.get("execution_status") == "pending"
                    and int(doc.get("attempts") or 0) == 0
                    and _stale(doc, "created_at")
                ):
                    token = token or self._cached_tasks_token()
                    await self.task_post(
                        "/internal/tasks/execute-calendar-action",
                        {
                            "schema_version": self.settings.task_schema_version,
                            "outbox_id": outbox_id,
                            "action_idempotency_key": doc.get(
                                "action_idempotency_key", outbox_id
                            ),
                            "trace_id": f"golden-rescue-{outbox_id[:8]}",
                        },
                        token,
                    )
                    print(
                        f"      transport rescue: action {outbox_id[:12]}… "
                        "delivered directly (task name consumed by a prior run)"
                    )
        except Exception as error:  # noqa: BLE001
            print(f"transport rescue: {error}")

    # 420s: a first delivery that draws a retryable 503 (e.g. the reset's
    # cancel-echo sync still holds the calendar publication barrier) backs off
    # past the old 240s budget; rescue skips attempts>=1, so the repair path is
    # the 5-minute dispatch_pending sweep. The budget must outlive one sweep.
    async def wait_terminal_observation(self, observation_id: str, timeout: float = 420.0):
        async def _probe():  # noqa: ANN202
            doc = await self.get_raw(SOURCE_OBSERVATIONS, observation_id)
            if doc and doc.get("reconciliation_status") in ("processed", "ignored"):
                return doc
            return None

        return await self.wait_for(
            f"observation {observation_id[:12]}… terminal", _probe, timeout
        )

    async def wait_pending_approval(
        self, request_type: str, commitment_id: str, timeout: float = 90.0
    ) -> Mapping[str, Any]:
        async def _probe():  # noqa: ANN202
            for doc_id, doc in await self.query(APPROVALS, self.user_filter()):
                if (
                    doc.get("request_type") == request_type
                    and doc.get("status") == "pending"
                    and doc.get("commitment_id") == commitment_id
                ):
                    return {"approval_id": doc_id, **doc}
            return None

        return await self.wait_for(f"{request_type} pending", _probe, timeout)

    async def resolve_approval(
        self, approval: Mapping[str, Any], **extra: Any
    ) -> None:
        body = {"decision": "approve", "expected_revision": approval["revision"], **extra}
        response = await self.api_post(
            f"/api/v1/approvals/{approval['approval_id']}/resolve", body
        )
        ok = response.status_code == 200 and response.json().get("status") == "completed"
        checkpoint(
            f"{approval.get('request_type')} resolved via guarded route",
            ok,
            f"HTTP {response.status_code}",
        )
        if not ok:
            raise RuntimeError(f"approval resolution failed: {response.text[:300]}")

    # -- verification helpers ------------------------------------------------

    async def blocks_for(self, commitment_id: str) -> list[tuple[str, Mapping[str, Any]]]:
        return sorted(
            await self.query(WORK_BLOCKS, [("commitment_id", "==", commitment_id)]),
            key=lambda pair: str(pair[1].get("scheduled_start")),
        )

    @staticmethod
    def _dt(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    async def verify_blocks_envelope(
        self,
        commitment_id: str,
        expected_total_minutes: int,
        deadline: datetime,
        other_blocks: list[tuple[str, Mapping[str, Any]]],
    ) -> list[tuple[str, Mapping[str, Any]]]:
        blocks = await self.blocks_for(commitment_id)
        live = [
            (i, d)
            for i, d in blocks
            if d.get("execution_state") in ("planned", "active", "awaiting_check_in")
        ]
        total = sum(int(d.get("duration_minutes", 0)) for _, d in live)
        checkpoint(
            "allocated minutes equal confirmed effort",
            total == expected_total_minutes,
            f"{total}/{expected_total_minutes}",
        )
        tz = self.local_tz()
        now = datetime.now(timezone.utc)
        intervals: list[tuple[datetime, datetime, str]] = []
        for block_id, doc in live:
            start = self._dt(doc["scheduled_start"])
            end = self._dt(doc["scheduled_end"])
            local_start, local_end = start.astimezone(tz), end.astimezone(tz)
            in_hours = (
                (local_start.hour, local_start.minute) >= (9, 0)
                and (local_end.hour, local_end.minute) <= (17, 30)
            )
            checkpoint(
                f"block {block_id[:10]}… inside envelope",
                start > now - timedelta(minutes=1) and end <= deadline and in_hours,
                f"{local_start:%a %H:%M}-{local_end:%H:%M}",
            )
            intervals.append((start, end, block_id))
        all_intervals = intervals + [
            (self._dt(d["scheduled_start"]), self._dt(d["scheduled_end"]), i)
            for i, d in other_blocks
            if d.get("execution_state") in ("planned", "active", "awaiting_check_in")
        ]
        overlaps = [
            (a[2], b[2])
            for index, a in enumerate(all_intervals)
            for b in all_intervals[index + 1 :]
            if a[0] < b[1] and b[0] < a[1]
        ]
        checkpoint("no interval allocated twice across the portfolio", not overlaps, str(overlaps))
        return live

    async def verify_calendar_events(
        self, blocks: list[tuple[str, Mapping[str, Any]]]
    ) -> None:
        # Poll rather than single-shot: create actions whose burned task
        # names need the transport rescue land a few seconds after the plan
        # commits, and wait_for's rescue hook is what delivers them.
        for block_id, doc in blocks:
            event_id = str(doc.get("calendar_event_id"))

            async def _probe(target_id: str = event_id):  # noqa: ANN202
                event = await self.calendar_reader.get_event(
                    self.settings.calendar_id, target_id
                )
                if event is not None and getattr(event, "status", "") != "cancelled":
                    return event
                return None

            await self.wait_for(
                f"calendar event exists for block {block_id[:10]}…", _probe, 120.0
            )

    async def state_digest(self) -> str:
        selectors = {
            COMMITMENTS: lambda d: {
                "revision": d.get("revision"),
                "lifecycle_status": d.get("lifecycle_status"),
                "effort": (d.get("effort") or {}).get("confirmed_minutes"),
            },
            WORK_BLOCKS: lambda d: {
                "revision": d.get("revision"),
                "state": d.get("execution_state"),
                "start": str(d.get("scheduled_start")),
                "end": str(d.get("scheduled_end")),
                "event": d.get("calendar_event_id"),
                "verified": d.get("verified_minutes"),
            },
            APPROVALS: lambda d: {"revision": d.get("revision"), "status": d.get("status")},
            PLANNER_RUNS: lambda d: {"status": d.get("status")},
            EVIDENCE: lambda d: {"type": d.get("evidence_type")},
            SOURCE_OBSERVATIONS: lambda d: {"status": d.get("reconciliation_status")},
            ACTION_OUTBOX: lambda d: {
                "dispatch": d.get("dispatch_status"),
                "execution": d.get("execution_status"),
            },
        }

        async def _load(repositories):  # noqa: ANN001, ANN202
            # One Firestore transaction gives the digest a serializable read
            # point across every collection. Separate read-only queries can
            # otherwise straddle a workflow commit and manufacture a state
            # that never existed (the 2026-08-17 live replay false failure).
            return {
                collection: await repositories._context.query(collection, [])  # noqa: SLF001
                for collection in selectors
            }

        rows_by_collection = await self.uow.run(_load)
        view: dict[str, Any] = {}
        for collection, select in selectors.items():
            rows = rows_by_collection[collection]
            view[collection] = {doc_id: select(doc) for doc_id, doc in sorted(rows)}
        return hashlib.sha256(
            json.dumps(view, sort_keys=True, default=str).encode()
        ).hexdigest()

    async def _replay_pipeline_busy(self) -> tuple[str, ...]:
        """Return durable work that can still change replay-digested state."""

        async def _load(repositories):  # noqa: ANN001, ANN202
            context = repositories._context  # noqa: SLF001
            return {
                SOURCE_OBSERVATIONS: await context.query(SOURCE_OBSERVATIONS, []),
                ACTION_OUTBOX: await context.query(ACTION_OUTBOX, []),
                SYNC_REQUESTS: await context.query(SYNC_REQUESTS, []),
                SYNC_GENERATIONS: await context.query(SYNC_GENERATIONS, []),
                SYNC_CURSORS: await context.query(SYNC_CURSORS, []),
            }

        rows = await self.uow.run(_load)
        user_id = self.settings.controlled_user_id
        busy: list[str] = []
        for document_id, document in rows[SOURCE_OBSERVATIONS]:
            if document.get("user_id") == user_id and document.get(
                "reconciliation_status"
            ) not in ("processed", "ignored", "rejected"):
                busy.append(
                    f"observation:{document_id[:12]}:{document.get('reconciliation_status')}"
                )
        for document_id, document in rows[ACTION_OUTBOX]:
            if document.get("user_id") == user_id and document.get(
                "execution_status"
            ) not in (
                "succeeded",
                "terminal_failed",
                "stale",
                "stale_precondition",
                "superseded",
            ):
                busy.append(
                    f"action:{document_id[:12]}:{document.get('execution_status')}"
                )
        for document_id, document in rows[SYNC_REQUESTS]:
            if (
                document.get("user_id") == user_id
                and document.get("status") == "pending"
            ):
                busy.append(f"sync-request:{document_id[:12]}:pending")
        for document_id, document in rows[SYNC_GENERATIONS]:
            if document.get("user_id") == user_id and document.get("status") in (
                "staging",
                "applying",
                "ready_to_publish",
            ):
                busy.append(
                    f"sync-generation:{document_id[:12]}:{document.get('status')}"
                )
        for document_id, document in rows[SYNC_CURSORS]:
            if (
                document.get("user_id") == user_id
                and document.get("publish_in_progress_generation_id") is not None
            ):
                busy.append(f"sync-cursor:{document_id[:12]}:publication-barrier")
        return tuple(sorted(busy))

    async def wait_replay_quiescent(
        self,
        timeout: float = 420.0,
        poll_interval: float = 1.0,
        stable_samples: int = 2,
    ) -> str:
        """Return a digest only after durable producers and state are quiet.

        The digest remains strict: it still covers every observation and
        action. This barrier merely prevents an already-running source sync or
        reconciliation from being misattributed to the redeliveries under
        test. Two equal serializable snapshots also close the small handoff
        race between observing an empty pipeline and taking the baseline.
        """
        deadline = time.monotonic() + timeout
        previous_digest: str | None = None
        stable_count = 0
        last_rescue = time.monotonic()
        while time.monotonic() < deadline:
            busy = await self._replay_pipeline_busy()
            digest = await self.state_digest()
            if not busy and digest == previous_digest:
                stable_count += 1
            elif not busy:
                stable_count = 1
            else:
                stable_count = 0
            previous_digest = digest
            if stable_count >= stable_samples:
                return digest
            now = time.monotonic()
            if now - last_rescue >= 20.0:
                last_rescue = now
                await self.rescue_stuck_transport()
            await asyncio.sleep(poll_interval)
        busy = await self._replay_pipeline_busy()
        detail = ", ".join(busy[:5]) or "state did not stabilize"
        raise TimeoutError(f"replay quiescence barrier: {detail}")

    # -- scenario legs -------------------------------------------------------

    async def elapse_block(self, block_id: str) -> None:
        """Scenario-clock stand-in (Phase 3 convention): the campaign
        compresses days, so the driver performs the same durable transition
        and audit record the 4C safety scan applies at scheduled time."""
        from commitmentos.domain.audit.models import (
            ActivityEventFactory,
            ActivityEventType,
        )

        now = datetime.now(timezone.utc)

        async def _elapse(repositories):  # noqa: ANN001, ANN202
            block = await repositories.work_blocks.get(block_id)
            updated = block.request_check_in(now)
            await repositories.work_blocks.save(updated, block.revision)
            await repositories.activity.append(
                ActivityEventFactory().create(
                    user_id=self.settings.controlled_user_id,
                    event_type=ActivityEventType.WORK_CHECK_IN_REQUIRED,
                    trace_id=f"trace-golden-{self.run_tag}",
                    actor="golden_campaign_driver",
                    summary=(
                        "Elapsed work block needs a verified-minute check-in "
                        "(campaign scenario-clock stand-in)"
                    ),
                    payload={
                        "failure_state": "work_check_in_required",
                        "work_block_id": updated.work_block_id,
                        "commitment_id": updated.commitment_id,
                        "previous_execution_state": block.execution_state.value,
                        "execution_state": updated.execution_state.value,
                        "verified_minutes": updated.verified_minutes,
                        "scenario_clock_stand_in": True,
                    },
                    created_at=now,
                )
            )
            return updated.revision

        revision = await self.uow.run(_elapse)
        checkpoint(
            f"block {block_id[:10]}… elapsed to awaiting_check_in (driver stand-in)",
            True,
            f"rev {revision}",
        )

    async def check_in(self, block_id: str, minutes: int, key: str) -> None:
        doc = await self.get_raw(WORK_BLOCKS, block_id)
        body = {
            "idempotency_key": key,
            "completed": True,
            "verified_minutes": minutes,
            "checked_in_at": datetime.now(timezone.utc).isoformat(),
            "expected_revision": doc["revision"],
        }
        first = await self.api_post(f"/api/v1/work-blocks/{block_id}/check-in", body)
        checkpoint(
            f"check-in {minutes}m recorded via guarded route",
            first.status_code == 200 and first.json().get("status") == "completed",
            f"HTTP {first.status_code}",
        )
        second = await self.api_post(f"/api/v1/work-blocks/{block_id}/check-in", body)
        checkpoint(
            "identical check-in redelivery converges",
            second.status_code == 200 and second.json().get("status") == "no_op",
            second.json().get("error_code", "") if second.status_code == 200 else "",
        )

    async def insert_conflict(self, target_block: Mapping[str, Any]) -> tuple[str, float]:
        service = self.calendar_service()
        start = self._dt(target_block["scheduled_start"])
        end = self._dt(target_block["scheduled_end"])
        body = {
            "summary": CONFLICT_SUMMARY,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": self.settings.controlled_timezone,
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": self.settings.controlled_timezone,
            },
        }
        inserted_at = time.monotonic()
        created = (
            service.events()
            .insert(calendarId=self.settings.calendar_id, body=body)
            .execute()
        )
        # Record the ID so the between-runs reset can remove the meeting;
        # the run itself must leave it untouched (verified below).
        baseline = self.baseline()
        baseline.setdefault("conflict_event_ids", []).append(created["id"])
        self.save_baseline(baseline)
        checkpoint("conflict meeting inserted (recorded API convention)", True, created["id"][:14])
        return created["id"], inserted_at

    async def wait_for_repair(
        self,
        target_block_id: str,
        original_start: str,
        inserted_at: float,
        timeout: float = 120.0,
    ) -> float:
        async def _probe():  # noqa: ANN202
            doc = await self.get_raw(WORK_BLOCKS, target_block_id)
            if doc and str(doc.get("scheduled_start")) != original_start:
                return doc
            return None

        await self.wait_for("conflict repaired: target block moved", _probe, timeout, 1.0)
        return time.monotonic() - inserted_at

    async def remove_conflict(self, event_id: str) -> None:
        service = self.calendar_service()
        try:
            service.events().delete(
                calendarId=self.settings.calendar_id, eventId=event_id
            ).execute()
        except Exception as error:  # noqa: BLE001
            print(f"conflict removal: {error}")

    async def complete_commitment(self, commitment_id: str) -> None:
        doc = await self.get_raw(COMMITMENTS, commitment_id)
        body = {
            "idempotency_key": f"golden-complete-{self.run_tag}",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "expected_revision": doc["revision"],
            "note": CANONICAL_COMPLETION_NOTE,
        }
        first = await self.api_post(f"/api/v1/commitments/{commitment_id}/complete", body)
        checkpoint(
            "completion recorded via guarded route",
            first.status_code == 200 and first.json().get("status") == "completed",
            f"HTTP {first.status_code}",
        )
        second = await self.api_post(f"/api/v1/commitments/{commitment_id}/complete", body)
        checkpoint(
            "identical completion redelivery converges",
            second.status_code == 200 and second.json().get("status") == "no_op",
            second.json().get("error_code", "") if second.status_code == 200 else "",
        )

    # -- audit contract ------------------------------------------------------

    AUDIT_ORDER = (
        ("interpretation_created", "golden commitment detected"),
        ("confirmation_recorded", "effort confirmation requested"),
        ("plan_proposed", "portfolio plan proposed"),
        ("outbox_written", "create actions written"),
        ("calendar_action_result", "calendar events created"),
        ("work_check_in_recorded", "first verified check-in"),
        ("plan_repaired", "conflict repaired minimally"),
        ("work_check_in_recorded", "moved-block verified check-in"),
        ("work_check_in_required", "final block awaiting check-in"),
        ("completion_recorded", "explicit completion"),
    )

    async def verify_audit_order(self, since: datetime) -> None:
        rows = await self.query(ACTIVITY_EVENTS, self.user_filter())
        events = sorted(
            (
                doc
                for _, doc in rows
                if self._dt(doc.get("created_at", since)) >= since
            ),
            key=lambda doc: str(doc.get("created_at")),
        )
        sequence = [str(doc.get("event_type")) for doc in events]
        cursor = 0
        missing: list[str] = []
        for event_type, label in self.AUDIT_ORDER:
            try:
                cursor = sequence.index(event_type, cursor) + 1
            except ValueError:
                missing.append(label)
        checkpoint(
            "frozen audit order holds (18-step contract, campaign form)",
            not missing,
            "; ".join(missing) or f"{len(sequence)} events",
        )

    # -- replays -------------------------------------------------------------

    async def replay_everything(self) -> tuple[bool, str, str]:
        before = await self.wait_replay_quiescent()
        token = self.tasks_identity_token()
        observations = [
            (i, d)
            for i, d in await self.query(SOURCE_OBSERVATIONS, self.user_filter())
            if d.get("reconciliation_status") in ("processed", "ignored")
            and self._dt(d.get("observed_at", self.run_started_at)) >= self.run_started_at
        ]
        for observation_id, doc in observations:
            await self.task_post(
                "/internal/tasks/reconcile-observation",
                {
                    "schema_version": self.settings.task_schema_version,
                    "observation_id": observation_id,
                    "workflow_version": doc.get(
                        "workflow_version", self.settings.workflow_version
                    ),
                    "dispatch_generation": doc.get("dispatch_generation", 1),
                    "trace_id": f"golden-replay-{observation_id[:8]}",
                },
                token,
            )
        actions = [
            (i, d)
            for i, d in await self.query(ACTION_OUTBOX, [])
            if d.get("execution_status") == "succeeded"
        ]
        for outbox_id, doc in actions:
            await self.task_post(
                "/internal/tasks/execute-calendar-action",
                {
                    "schema_version": self.settings.task_schema_version,
                    "outbox_id": outbox_id,
                    "action_idempotency_key": doc.get("action_idempotency_key", outbox_id),
                    "trace_id": f"golden-replay-{outbox_id[:8]}",
                },
                token,
            )
        after = await self.wait_replay_quiescent()
        checkpoint(
            "replay of every observation and action leaves state byte-identical",
            before == after,
            f"{len(observations)} obs + {len(actions)} actions",
        )
        return before == after, before, after

    # -- watch-channel discipline --------------------------------------------

    async def _latest_calendar_generation_at(self) -> datetime | None:
        generations = [
            doc
            for _, doc in await self.query("sync_generations", [])
            if doc.get("source") == "calendar" and doc.get("created_at") is not None
        ]
        if not generations:
            return None
        return max(self._dt(doc["created_at"]) for doc in generations)

    async def wait_calendar_settled(
        self, quiet_seconds: float = 75.0, timeout: float = 420.0
    ) -> None:
        """Let the watch channel's burst window drain before the conflict leg.

        The scenario clock separates planning from the conflict by a day; the
        campaign compresses that, but Google throttles channel notifications
        after a burst of changes (observed live 2026-08-15: the creates'
        echoes delivered at 10-second spacing, then the channel swallowed the
        conflict insert's notification entirely). Quiet = the newest calendar
        sync generation is older than `quiet_seconds`.
        """
        deadline = time.monotonic() + timeout
        while True:
            newest = await self._latest_calendar_generation_at()
            age = (
                (datetime.now(timezone.utc) - newest).total_seconds()
                if newest is not None
                else quiet_seconds
            )
            if age >= quiet_seconds:
                checkpoint(
                    "calendar channel settled before the conflict leg",
                    True,
                    f"{age:.0f}s since last sync generation",
                )
                return
            if time.monotonic() >= deadline:
                checkpoint(
                    "calendar channel settled before the conflict leg",
                    False,
                    "settle timeout",
                )
                raise TimeoutError("calendar settle")
            await asyncio.sleep(5.0)

    async def ensure_push_live(self, timeout_per_attempt: float = 45.0) -> None:
        """Prove the watch channel is delivering before the timed conflict leg.

        Inserts a transparent far-future probe event (never enters capacity
        math or the planning horizon) and waits for the resulting sync
        generation. The between-runs reset removes recorded probe events.
        """
        service = self.calendar_service()
        for attempt in range(1, 4):
            baseline_at = await self._latest_calendar_generation_at()
            start = datetime.now(timezone.utc) + timedelta(days=30)
            body = {
                "summary": "Golden campaign — channel liveness probe",
                "transparency": "transparent",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(minutes=15)).isoformat()},
            }
            created = (
                service.events()
                .insert(calendarId=self.settings.calendar_id, body=body)
                .execute()
            )
            baseline = self.baseline()
            baseline.setdefault("probe_event_ids", []).append(created["id"])
            self.save_baseline(baseline)
            probe_started = time.monotonic()
            while time.monotonic() - probe_started < timeout_per_attempt:
                newest = await self._latest_calendar_generation_at()
                if newest is not None and (baseline_at is None or newest > baseline_at):
                    checkpoint(
                        "watch channel proven live before conflict insert",
                        True,
                        f"attempt {attempt}, {time.monotonic() - probe_started:.1f}s",
                    )
                    return
                await asyncio.sleep(2.0)
        checkpoint(
            "watch channel proven live before conflict insert", False, "3 attempts"
        )
        raise TimeoutError("watch channel not delivering")

    # -- the run -------------------------------------------------------------

    async def run(self, mode: str, deadline_days: int) -> dict:
        self.run_started_at = datetime.now(timezone.utc)
        metrics: dict[str, Any] = {
            "run_tag": self.run_tag,
            "mode": mode,
            "started_at": self.run_started_at.isoformat(),
        }

        # 0. Clean start: no live commitments for the user.
        active = [
            (i, d)
            for i, d in await self.query(COMMITMENTS, self.user_filter())
            if d.get("lifecycle_status")
            in ("candidate", "awaiting_confirmation", "active", "in_progress")
        ]
        checkpoint("run starts from a clean portfolio", not active, str(len(active)))
        if active:
            raise RuntimeError("reset required before run")

        # 1. Portfolio context: the second commitment with confirmed effort
        #    and an approved plan (its blocks must be preserved untouched).
        observation_id = await self.seed_seeded_observation(
            self.second_commitment_payload(deadline_days + 3), "message_golden_data_summary"
        )
        await self.wait_terminal_observation(observation_id)
        effort = await self.wait_pending_approval(
            "effort_confirmation", self.second_commitment_id
        )
        await self.resolve_approval(effort, confirmed_minutes=120)
        plan = await self.wait_pending_approval(
            "initial_plan_approval", self.second_commitment_id
        )
        await self.resolve_approval(plan)

        async def _second_blocks_live():  # noqa: ANN202
            blocks = await self.blocks_for(self.second_commitment_id)
            live = [b for b in blocks if b[1].get("execution_state") == "planned"]
            return live if live else None

        second_blocks = await self.wait_for(
            "second commitment planned with real events", _second_blocks_live, 120.0
        )
        await self.verify_calendar_events(second_blocks)
        second_snapshot_before = json.dumps(
            [(i, str(d.get("scheduled_start")), d.get("calendar_event_id")) for i, d in second_blocks],
            sort_keys=True,
        )

        # 2. The golden commitment.
        if mode == "thread":
            baseline = self.baseline()
            thread = baseline.get("golden_thread")
            if not thread:
                raise RuntimeError("no thread baseline; run find-thread first")
            for message_id in thread["message_ids"]:
                observation_id = await self.seed_thread_observation(
                    message_id, thread["thread_id"]
                )
                # Generous window: a transient Gemini 5xx puts the named task
                # into exponential backoff; the retry must land inside the wait.
                await self.wait_terminal_observation(observation_id, 300.0)

            async def _golden():  # noqa: ANN202
                rows = [
                    (i, d)
                    for i, d in await self.query(
                        COMMITMENTS,
                        [("source_thread_id", "==", thread["thread_id"])],
                    )
                    if d.get("lifecycle_status") != "dismissed"
                ]
                return rows if rows else None

            rows = await self.wait_for("golden commitment detected", _golden, 60.0)
            checkpoint(
                "exactly one commitment from three thread observations",
                len(rows) == 1,
                str(len(rows)),
            )
            if len(rows) != 1:
                raise RuntimeError("duplicate commitments")
            self.golden_commitment_id, golden_doc = rows[0]
            checkpoint(
                "ownership resolved as my_commitment",
                golden_doc.get("ownership_type") == "my_commitment",
                str(golden_doc.get("ownership_type")),
            )
            deadline = self._dt((golden_doc.get("deadline") or {}).get("value"))
            checkpoint(
                "interpreted deadline is future and confident",
                deadline > datetime.now(timezone.utc)
                and float((golden_doc.get("deadline") or {}).get("confidence", 0)) >= 0.8,
                deadline.isoformat(),
            )
        else:
            observation_id = await self.seed_seeded_observation(
                self.golden_seeded_payload(deadline_days), "message_golden_proposal"
            )
            await self.wait_terminal_observation(observation_id)
            golden_doc = await self.get_raw(COMMITMENTS, self.golden_commitment_id)
            checkpoint("golden commitment seeded", golden_doc is not None, "")
            deadline = self._dt((golden_doc.get("deadline") or {}).get("value"))
        metrics["golden_commitment_id"] = self.golden_commitment_id
        metrics["deadline"] = deadline.isoformat()

        # 3. Effort confirmation at exactly 180 (checklist §1 edit rule).
        effort = await self.wait_pending_approval(
            "effort_confirmation", self.golden_commitment_id, 120.0
        )
        await self.resolve_approval(effort, confirmed_minutes=180)

        # 4. Initial plan: verify the constraint envelope, then approve.
        plan = await self.wait_pending_approval(
            "initial_plan_approval", self.golden_commitment_id, 120.0
        )
        await self.resolve_approval(plan)

        async def _golden_blocks():  # noqa: ANN202
            blocks = await self.blocks_for(self.golden_commitment_id)
            live = [b for b in blocks if b[1].get("execution_state") == "planned"]
            return live if live else None

        # 420s: late throttled cancel-echo syncs can bump calendar_state_revision
        # mid-replan, staling planner attempts until the 60s safety
        # reconciliation replans from fresh facts; the budget must cover that.
        await self.wait_for("golden plan committed with work blocks", _golden_blocks, 420.0)
        golden_blocks = await self.verify_blocks_envelope(
            self.golden_commitment_id, 180, deadline, second_blocks
        )
        await self.verify_calendar_events(golden_blocks)
        metrics["golden_block_count"] = len(golden_blocks)
        derived_ok = all(
            doc.get("calendar_event_id")
            for _, doc in golden_blocks
        )
        checkpoint("every block carries its stable derived event ID", derived_ok, "")

        async def _actions_processed():  # noqa: ANN202
            rows = [
                (i, d)
                for i, d in await self.query(ACTION_OUTBOX, [])
                if d.get("commitment_id") == self.golden_commitment_id
            ]
            succeeded = [r for r in rows if r[1].get("execution_status") == "succeeded"]
            return succeeded if len(succeeded) >= len(golden_blocks) else None

        await self.wait_for("all create actions succeeded", _actions_processed, 120.0)
        await self.wait_calendar_settled()

        # 5. First verified check-in (60 minutes).
        first_block_id = golden_blocks[0][0]
        await self.elapse_block(first_block_id)
        await self.check_in(first_block_id, 60, f"golden-checkin-1-{self.run_tag}")

        # 6. Conflict over the next planned block → automatic minimal repair.
        await self.ensure_push_live()
        remaining = [
            (i, d) for i, d in await self.blocks_for(self.golden_commitment_id)
            if d.get("execution_state") == "planned"
        ]
        target_id, target_doc = remaining[0]
        original_start = str(target_doc.get("scheduled_start"))
        unaffected_before = {
            i: (str(d.get("scheduled_start")), d.get("calendar_event_id"))
            for i, d in await self.blocks_for(self.golden_commitment_id)
            if i != target_id
        }
        conflict_event_id, inserted_at = await self.insert_conflict(target_doc)
        latency = await self.wait_for_repair(target_id, original_start, inserted_at)
        metrics["conflict_to_repair_seconds"] = round(latency, 3)
        checkpoint(
            "conflict-to-repair latency inside operational budget",
            latency < OPERATIONAL_LATENCY_BUDGET_SECONDS,
            f"{latency:.3f}s (warmed budget {WARMED_LATENCY_BUDGET_SECONDS:.0f}s)",
        )
        moved = await self.get_raw(WORK_BLOCKS, target_id)
        checkpoint(
            "moved block kept its stable event ID",
            moved.get("calendar_event_id") == target_doc.get("calendar_event_id"),
            "",
        )
        unaffected_after = {
            i: (str(d.get("scheduled_start")), d.get("calendar_event_id"))
            for i, d in await self.blocks_for(self.golden_commitment_id)
            if i != target_id
        }
        checkpoint(
            "every unaffected golden block preserved byte-identically",
            unaffected_before == unaffected_after,
            "",
        )
        second_after = json.dumps(
            [
                (i, str(d.get("scheduled_start")), d.get("calendar_event_id"))
                for i, d in await self.blocks_for(self.second_commitment_id)
                if d.get("execution_state") == "planned"
            ],
            sort_keys=True,
        )
        checkpoint(
            "second commitment's blocks untouched by the repair",
            second_after == second_snapshot_before,
            "",
        )
        conflict_event = await self.calendar_reader.get_event(
            self.settings.calendar_id, conflict_event_id
        )
        checkpoint(
            "conflict meeting itself untouched",
            conflict_event is not None and getattr(conflict_event, "status", "") != "cancelled",
            "",
        )

        # 7. Check in the moved block (total 120 verified).
        await self.elapse_block(target_id)
        await self.check_in(target_id, 60, f"golden-checkin-2-{self.run_tag}")

        # 8. Final block elapses; the user completes instead of checking in.
        final_planned = [
            (i, d)
            for i, d in await self.blocks_for(self.golden_commitment_id)
            if d.get("execution_state") == "planned"
        ]
        for block_id, _ in final_planned:
            await self.elapse_block(block_id)

        # 9. Explicit completion with the canonical note (§4.5).
        await self.complete_commitment(self.golden_commitment_id)
        completed = await self.get_raw(COMMITMENTS, self.golden_commitment_id)
        checkpoint(
            "commitment terminal with completion evidence",
            completed.get("lifecycle_status") == "completed"
            and bool(completed.get("completion_evidence_id")),
            "",
        )
        verified_total = sum(
            int(d.get("verified_minutes", 0))
            for _, d in await self.blocks_for(self.golden_commitment_id)
        )
        checkpoint(
            "verified minutes remain 120 — honest closure below the estimate",
            verified_total == 120,
            str(verified_total),
        )
        metrics["verified_minutes_at_completion"] = verified_total

        async def _check_ins_closed():  # noqa: ANN202
            rows = await self.blocks_for(self.golden_commitment_id)
            open_requests = [
                i
                for i, d in rows
                if d.get("execution_state") in ("awaiting_check_in", "active")
            ]
            return True if not open_requests else None

        await self.wait_for("pending check-in requests closed by completion", _check_ins_closed, 90.0)

        # 10. Frozen audit order + full replay convergence.
        await self.verify_audit_order(self.run_started_at)
        replay_ok, digest_before, digest_after = await self.replay_everything()
        metrics["replay_digest"] = digest_after[:16]

        still_completed = await self.get_raw(COMMITMENTS, self.golden_commitment_id)
        checkpoint(
            "commitment stays closed after replay",
            still_completed.get("lifecycle_status") == "completed",
            "",
        )

        # Truncated: provider-issued ID; committed evidence carries no full
        # provider identifiers (docs/phase5_evidence/README.md).
        metrics["conflict_event_id"] = conflict_event_id[:12] + "…"
        metrics["completed_at"] = datetime.now(timezone.utc).isoformat()
        metrics["checkpoints_passed"] = len(_checkpoints) - len(failures())
        metrics["checkpoints_total"] = len(_checkpoints)
        metrics["passed"] = not failures()
        metrics["checkpoints"] = [
            {"label": label, "ok": ok, "detail": detail}
            for label, ok, detail in _checkpoints
        ]
        return metrics

    # -- reset ---------------------------------------------------------------

    async def reset(self) -> bool:
        """Between-runs reset: the audited 5A cleanup command plus removal of
        the driver-inserted conflict meeting and channel-liveness probes."""
        baseline = self.baseline()
        removed = False
        for key in ("conflict_event_ids", "probe_event_ids"):
            for event_id in baseline.get(key, []):
                await self.remove_conflict(event_id)
            if baseline.pop(key, None) is not None:
                removed = True
        if removed:
            self.save_baseline(baseline)
        command = CleanupControlledAccount(
            self.uow,
            self.container.calendar_writer(),
            FirestoreCleanupDocumentStore(self.container.firestore_client()),
            self.container.clock,
        )
        preview = await command.preview(self.settings.controlled_user_id)
        result = await command.execute(
            self.settings.controlled_user_id,
            preview,
            command.confirmation_phrase(preview),
            trace_id=f"golden-campaign-reset-{self.run_tag}",
        )
        print(
            json.dumps(
                {
                    "executed": result.executed,
                    "abort_reason": result.abort_reason,
                    "events_canceled": result.events_canceled,
                    "events_skipped": result.events_skipped_stale_or_unsynchronized,
                    "documents_purged": dict(result.documents_purged),
                },
                indent=2,
            )
        )
        return result.executed


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _runner(args: argparse.Namespace) -> GoldenPathRunner:
    settings = Settings.load()
    service_url = (args.service_url or str(settings.service_base_url)).rstrip("/")
    if service_url != str(settings.service_base_url).rstrip("/"):
        # The local .env is localhost-flavored (Phase 1 gate convention).
        # The container's task dispatcher must target the service under test:
        # Cloud Tasks rejects OIDC-authorized targets that are not https.
        settings = settings.model_copy(
            update={
                "service_base_url": service_url,
                "pubsub_oidc_audience": f"{service_url}/internal/pubsub",
                "tasks_oidc_audience": f"{service_url}/internal/tasks",
                "scheduler_oidc_audience": f"{service_url}/internal/scheduler",
            }
        )
    run_tag = getattr(args, "tag", None) or datetime.now(timezone.utc).strftime(
        "%Y%m%dt%H%M%S"
    )
    return GoldenPathRunner(settings, service_url, run_tag)


async def cmd_mint_session(args: argparse.Namespace) -> int:
    runner = _runner(args)
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session = {
        "session_id_hash": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": runner.settings.controlled_user_id,
        "email": runner.settings.controlled_email,
        "csrf_secret": csrf,
        "created_at": now,
        "expires_at": now + timedelta(hours=12),
        "revoked_at": None,
    }

    async def _create(repositories):  # noqa: ANN001, ANN202
        await repositories.web_sessions.create(session)

    await runner.uow.run(_create)
    SESSION_FILE.write_text(json.dumps({"session_token": token, "csrf_secret": csrf}))
    SESSION_FILE.chmod(0o600)
    print(f"session minted (hash {session['session_id_hash'][:12]}…)")
    return 0


async def cmd_cleanup_session(args: argparse.Namespace) -> int:
    runner = _runner(args)
    if not SESSION_FILE.exists():
        print("no session file")
        return 0
    token, _ = runner.load_session()
    session_hash = hashlib.sha256(token.encode()).hexdigest()

    async def _revoke(repositories):  # noqa: ANN001, ANN202
        await repositories.web_sessions.revoke(session_hash, datetime.now(timezone.utc))

    await runner.uow.run(_revoke)
    SESSION_FILE.unlink()
    print(f"session {session_hash[:12]}… revoked")
    return 0


async def cmd_find_thread(args: argparse.Namespace) -> int:
    """Locate the golden thread in the real mailbox; store its identifiers
    in the protected local baseline (never committed)."""
    runner = _runner(args)
    service = runner.gmail_service()
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=f'subject:"{args.subject}"', maxResults=20)
        .execute()
    )
    messages = listing.get("messages", [])
    if not messages:
        print(f"no messages match subject {args.subject!r}")
        return 1
    thread_ids = {m["threadId"] for m in messages}
    if len(thread_ids) > 1 and not args.thread_id:
        print(f"{len(thread_ids)} threads match; pass --thread-id to disambiguate")
        return 1
    thread_id = args.thread_id or next(iter(thread_ids))
    thread = service.users().threads().get(userId="me", id=thread_id, format="minimal").execute()
    ordered = sorted(
        thread.get("messages", []), key=lambda m: int(m.get("internalDate", 0))
    )
    baseline = runner.baseline()
    baseline["golden_thread"] = {
        "thread_id": thread_id,
        "message_ids": [m["id"] for m in ordered],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    runner.save_baseline(baseline)
    print(
        f"thread {thread_id[:10]}… with {len(ordered)} message(s) recorded to "
        f"{BASELINE_FILE} (protected; identifiers never enter committed docs)"
    )
    return 0


async def cmd_busy_setup(args: argparse.Namespace) -> int:
    """Insert the scenario's unrelated busy meetings (once per campaign)."""
    runner = _runner(args)
    tz = runner.local_tz()
    service = runner.calendar_service()
    today = datetime.now(tz).date()
    fixtures = []
    for day_offset, start_hm, minutes in ((1, (10, 30), 90), (2, (10, 0), 60), (3, (10, 30), 60)):
        day = today + timedelta(days=day_offset)
        start = datetime(day.year, day.month, day.day, *start_hm, tzinfo=tz)
        body = {
            "summary": f"Golden campaign — unrelated meeting {day_offset} (busy)",
            "start": {"dateTime": start.isoformat(), "timeZone": runner.settings.controlled_timezone},
            "end": {
                "dateTime": (start + timedelta(minutes=minutes)).isoformat(),
                "timeZone": runner.settings.controlled_timezone,
            },
        }
        created = service.events().insert(
            calendarId=runner.settings.calendar_id, body=body
        ).execute()
        fixtures.append(created["id"])
        print(f"busy fixture created: {created['id'][:14]}… ({day} {start_hm})")
    baseline = runner.baseline()
    baseline["busy_fixture_event_ids"] = fixtures
    runner.save_baseline(baseline)
    return 0


async def cmd_busy_cleanup(args: argparse.Namespace) -> int:
    runner = _runner(args)
    baseline = runner.baseline()
    service = runner.calendar_service()
    for event_id in baseline.get("busy_fixture_event_ids", []):
        try:
            service.events().delete(
                calendarId=runner.settings.calendar_id, eventId=event_id
            ).execute()
            print(f"deleted busy fixture {event_id[:14]}…")
        except Exception as error:  # noqa: BLE001
            print(f"busy fixture {event_id[:14]}…: {error}")
    baseline.pop("busy_fixture_event_ids", None)
    runner.save_baseline(baseline)
    return 0


async def cmd_preflight(args: argparse.Namespace) -> int:
    """Campaign preconditions against the deployed service."""
    runner = _runner(args)
    reset_checkpoints()
    async with httpx.AsyncClient(timeout=30) as client:
        health = await client.get(f"{runner.service_url}/health/live")
        checkpoint("service healthy", health.status_code == 200, str(health.status_code))
        completion_probe = await client.post(
            f"{runner.service_url}/api/v1/commitments/preflight/complete", json={}
        )
        checkpoint(
            "5A completion route deployed (auth precedes validation)",
            completion_probe.status_code == 401,
            f"HTTP {completion_probe.status_code}",
        )
        login_probe = await client.get(
            f"{runner.service_url}/auth/login",
            params={"return_to": "https://evil.example"},
            follow_redirects=False,
        )
        checkpoint(
            "5A AuthRouter deployed (redirect allowlist live)",
            login_probe.status_code == 400,
            f"HTTP {login_probe.status_code}",
        )
        demo_probe = await client.get(f"{runner.service_url}/demo/api/commitments")
        checkpoint(
            "5A demo surface deployed", demo_probe.status_code == 200, str(demo_probe.status_code)
        )
    cursor = await runner.get_raw(
        "sync_cursors", f"calendar:{runner.settings.controlled_user_id}"
    )
    checkpoint(
        "calendar cursor eligible (no barrier, no full resync)",
        cursor is not None
        and not cursor.get("publish_in_progress_generation_id")
        and not cursor.get("full_resync_required"),
        "",
    )
    channels = await runner.query("calendar_channels", [])
    fresh = [
        doc
        for _, doc in channels
        if runner._dt(doc.get("expiration", datetime.now(timezone.utc)))
        > datetime.now(timezone.utc) + timedelta(days=2)
    ]
    checkpoint("calendar watch valid for 2+ days", bool(fresh), f"{len(channels)} channel(s)")
    overdue = [
        (i, d)
        for i, d in await runner.query(COMMITMENTS, runner.user_filter())
        if d.get("lifecycle_status") in ("active", "in_progress")
    ]
    checkpoint(
        "no leftover live commitments (daf9a729 completed or cleaned)",
        not overdue,
        str(len(overdue)),
    )
    checkpoint("controlled session minted", SESSION_FILE.exists(), "")
    thread_ready = bool(runner.baseline().get("golden_thread")) or args.seeded
    checkpoint("golden thread baseline recorded (or --seeded)", thread_ready, "")
    return 1 if failures() else 0


async def cmd_run(args: argparse.Namespace) -> int:
    runner = _runner(args)
    reset_checkpoints()
    mode = "seeded" if args.seeded else "thread"
    try:
        metrics = await runner.run(mode, args.deadline_days)
    except Exception as error:  # noqa: BLE001
        checkpoint("run aborted", False, f"{type(error).__name__}: {error}")
        metrics = {
            "run_tag": runner.run_tag,
            "mode": mode,
            "passed": False,
            "aborted": f"{type(error).__name__}: {error}",
            "checkpoints": [
                {"label": label, "ok": ok, "detail": detail}
                for label, ok, detail in _checkpoints
            ],
        }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    number = f"{args.number:02d}" if args.number else runner.run_tag
    evidence_path = EVIDENCE_DIR / f"golden_run_{number}_{runner.run_tag}.json"
    evidence_path.write_text(json.dumps(metrics, indent=2, default=str))
    passed = bool(metrics.get("passed"))
    print(f"\nrun {number}: {'PASS' if passed else 'FAIL'} — evidence {evidence_path.name}")
    return 0 if passed else 1


async def cmd_reset(args: argparse.Namespace) -> int:
    runner = _runner(args)
    # The previous run's tail (replayed actions → echo syncs → observations)
    # can land between the cleanup's preview and execute, tripping its
    # drift-guard. That guard is doing its job — settle and re-preview.
    for attempt in range(1, 4):
        executed = await runner.reset()
        if executed:
            return 0
        print(f"reset attempt {attempt} aborted on drift; settling before retry")
        await runner.wait_calendar_settled(quiet_seconds=60.0, timeout=300.0)
    return 1


async def cmd_campaign(args: argparse.Namespace) -> int:
    """Ten consecutive runs; a failure stops the campaign (§6.2)."""
    for number in range(args.start, args.start + args.count):
        print(f"\n================ GOLDEN RUN {number} ================")
        reset_args = argparse.Namespace(service_url=args.service_url, tag=None)
        if await cmd_reset(reset_args) != 0:
            print(f"\ncampaign stopped: reset before run {number} did not execute")
            return 1
        run_args = argparse.Namespace(
            service_url=args.service_url,
            tag=None,
            number=number,
            seeded=args.seeded,
            deadline_days=args.deadline_days,
        )
        outcome = await cmd_run(run_args)
        if outcome != 0:
            print(
                f"\ncampaign stopped at run {number}: fix, regression-test, redeploy, "
                "and restart the count — ten must be consecutive."
            )
            return 1
        await asyncio.sleep(args.pause_seconds)
    print(f"\ncampaign complete: {args.count} consecutive passing runs")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mint-session")
    sub.add_parser("cleanup-session")
    p = sub.add_parser("find-thread")
    p.add_argument("--subject", required=True)
    p.add_argument("--thread-id", default=None)
    sub.add_parser("busy-setup")
    sub.add_parser("busy-cleanup")
    p = sub.add_parser("preflight")
    p.add_argument("--seeded", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--number", type=int, default=0)
    p.add_argument("--seeded", action="store_true")
    p.add_argument("--deadline-days", type=int, default=3)
    p.add_argument("--tag", default=None)
    sub.add_parser("reset")
    p = sub.add_parser("campaign")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--seeded", action="store_true")
    p.add_argument("--deadline-days", type=int, default=3)
    p.add_argument("--pause-seconds", type=float, default=45.0)
    arguments = parser.parse_args()

    handlers = {
        "mint-session": cmd_mint_session,
        "cleanup-session": cmd_cleanup_session,
        "find-thread": cmd_find_thread,
        "busy-setup": cmd_busy_setup,
        "busy-cleanup": cmd_busy_cleanup,
        "preflight": cmd_preflight,
        "run": cmd_run,
        "reset": cmd_reset,
        "campaign": cmd_campaign,
    }
    return asyncio.run(handlers[arguments.command](arguments))


if __name__ == "__main__":
    raise SystemExit(main())
