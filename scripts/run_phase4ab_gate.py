"""Close the controlled-account Phase 4A and 4B live exits.

This driver uses only HTTPS/REST control planes so it can run from a restricted
workstation without relying on Firestore's gRPC transport.  It creates a small
set of uniquely tagged, transparent Calendar fixtures and removes only those
exact IDs during cleanup.

The live sequence is deliberately operational:

* 4A: 11 changes with a gate page size of 10 produce exactly two provider
  pages.  Page 1 is invoked while the source queue is paused, proving cursor
  non-promotion.  Page 2 is invoked while the driver polls the real Firestore
  publication barrier; a pending planner observation and pending Calendar
  action are both delivered during that barrier and must refuse work.
* 4B: one real owned block is moved to a constraint-safe free slot and must be
  adopted with no new outbox mutation.  A separate repair action is held,
  provider truth is changed without snapshot publication, and the deployed
  executor must receive a real HTTP 412, synchronize, reissue with the new
  etag, and then succeed.

Usage:
    .venv/bin/python scripts/run_phase4ab_gate.py run \
        --service-url https://SERVICE.run.app
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.contracts.observations import (  # noqa: E402
    ObservationFactory,
    ObservationType,
)
from commitmentos.domain.planning.calendar_state import CalendarSnapshotReducer  # noqa: E402
from commitmentos.domain.planning.models import TimeInterval  # noqa: E402
from commitmentos.domain.shared.types import CanonicalEncoder  # noqa: E402
from commitmentos.infrastructure.firestore.serializers import (  # noqa: E402
    CalendarEventSnapshotSerializer,
    ObservationSerializer,
)

PROJECT = "commitmentos-505114"
REGION = "us-west1"
SERVICE = "commitmentos"
EVIDENCE_PATH = Path("docs/phase4_evidence/phase4ab_gate_run_final.json")
SEED_EVIDENCE_PATH = Path("docs/phase4_evidence/phase4ab_seed_target.json")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def prefix(value: Any, length: int = 12) -> str:
    return str(value or "")[:length]


def run_gcloud(*args: str) -> str:
    result = subprocess.run(
        ["gcloud", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def decode_value(value: Mapping[str, Any]) -> Any:
    if "nullValue" in value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "timestampValue" in value:
        return datetime.fromisoformat(value["timestampValue"].replace("Z", "+00:00"))
    if "arrayValue" in value:
        return [decode_value(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            key: decode_value(item) for key, item in value["mapValue"].get("fields", {}).items()
        }
    raise ValueError(f"unsupported Firestore value shape: {sorted(value)}")


def encode_value(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, datetime):
        return {"timestampValue": iso(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, Mapping):
        return {
            "mapValue": {"fields": {str(key): encode_value(item) for key, item in value.items()}}
        }
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(item) for item in value]}}
    raise TypeError(f"cannot encode {type(value)!r} for Firestore")


def decode_document(document: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    document_id = str(document["name"]).rsplit("/", 1)[-1]
    return (
        document_id,
        {key: decode_value(value) for key, value in document.get("fields", {}).items()},
    )


class FirestoreRest:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.token = run_gcloud("auth", "print-access-token")
        self.documents = (
            f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
        )

    @property
    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        url = f"{self.documents}/{collection}/{quote(document_id, safe='')}"
        response = await self.client.get(url, headers=self.headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return decode_document(response.json())[1]

    async def query(self, collection: str) -> dict[str, dict[str, Any]]:
        response = await self.client.post(
            f"{self.documents}:runQuery",
            headers=self.headers | {"Content-Type": "application/json"},
            json={"structuredQuery": {"from": [{"collectionId": collection}]}},
        )
        response.raise_for_status()
        rows: dict[str, dict[str, Any]] = {}
        for item in response.json():
            if "document" in item:
                document_id, document = decode_document(item["document"])
                rows[document_id] = document
        return rows

    async def set(self, collection: str, document_id: str, document: Mapping[str, Any]) -> None:
        response = await self.client.patch(
            f"{self.documents}/{collection}/{quote(document_id, safe='')}",
            headers=self.headers | {"Content-Type": "application/json"},
            json={"fields": {key: encode_value(value) for key, value in document.items()}},
        )
        response.raise_for_status()

    async def delete(self, collection: str, document_id: str) -> None:
        response = await self.client.delete(
            f"{self.documents}/{collection}/{quote(document_id, safe='')}",
            headers=self.headers,
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()


class CalendarRest:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.access_token = ""

    @staticmethod
    def _secret_name(reference: str) -> str:
        parts = reference.split("/")
        return parts[parts.index("secrets") + 1]

    async def authorize(self) -> None:
        client_secret_raw = run_gcloud(
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={self._secret_name(self.settings.oauth_client_secret_ref)}",
            f"--project={PROJECT}",
        )
        refresh_token = run_gcloud(
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={self._secret_name(self.settings.controlled_refresh_token_secret_ref)}",
            f"--project={PROJECT}",
        )
        config = json.loads(client_secret_raw)["web"]
        response = await self.client.post(
            config["token_uri"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
            },
        )
        response.raise_for_status()
        self.access_token = response.json()["access_token"]

    @property
    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _event_url(self, event_id: str | None = None) -> str:
        base = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{quote(self.settings.calendar_id, safe='')}/events"
        )
        return f"{base}/{quote(event_id, safe='')}" if event_id else base

    async def get(self, event_id: str) -> dict[str, Any]:
        response = await self.client.get(self._event_url(event_id), headers=self.headers)
        response.raise_for_status()
        return response.json()

    async def insert(self, body: Mapping[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            self._event_url(),
            headers=self.headers | {"Content-Type": "application/json"},
            json=dict(body),
        )
        response.raise_for_status()
        return response.json()

    async def patch(
        self,
        event_id: str,
        body: Mapping[str, Any],
        etag: str | None,
    ) -> dict[str, Any]:
        headers = dict(self.headers) | {"Content-Type": "application/json"}
        if etag:
            headers["If-Match"] = etag
        response = await self.client.patch(
            self._event_url(event_id), headers=headers, json=dict(body)
        )
        response.raise_for_status()
        return response.json()

    async def delete(self, event_id: str, etag: str | None = None) -> None:
        headers = dict(self.headers)
        if etag:
            headers["If-Match"] = etag
        response = await self.client.delete(self._event_url(event_id), headers=headers)
        if response.status_code not in (200, 204, 404, 410):
            response.raise_for_status()

    async def list_day(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        response = await self.client.get(
            self._event_url(),
            headers=self.headers,
            params={
                "timeMin": iso(start),
                "timeMax": iso(end),
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": "2500",
                "orderBy": "startTime",
            },
        )
        response.raise_for_status()
        return list(response.json().get("items", []))


@dataclass
class PendingAction:
    outbox_id: str
    action_key: str
    work_block_id: str
    event_id: str
    expected_etag: str
    desired_start: datetime
    desired_end: datetime


class Gate:
    def __init__(self, service_url: str, timeout: float) -> None:
        self.settings = Settings.load()
        self.service_url = service_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.AsyncClient(timeout=120)
        self.fs = FirestoreRest(self.client)
        self.calendar = CalendarRest(self.client, self.settings)
        self.run_id = f"phase4ab-{utc_now():%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        self.fixture_event_ids: list[str] = []
        self.conflict_event_id: str | None = None
        self.restore_target: tuple[str, str, datetime, datetime] | None = None
        self.initial_queue_states: dict[str, str] = {}
        self.evidence: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": iso(utc_now()),
            "service_url": self.service_url,
            "checks": [],
        }
        self.cloud_env = self._cloud_environment()
        self.task_token = ""

    def _cloud_environment(self) -> dict[str, str]:
        raw = run_gcloud(
            "run",
            "services",
            "describe",
            SERVICE,
            f"--region={REGION}",
            f"--project={PROJECT}",
            "--format=json",
        )
        service = json.loads(raw)
        values = {}
        for item in service["spec"]["template"]["spec"]["containers"][0]["env"]:
            if "value" in item:
                values[item["name"]] = item["value"]
        return values

    def check(self, label: str, condition: bool, detail: Any = "") -> None:
        row = {"label": label, "passed": bool(condition), "detail": str(detail)}
        self.evidence["checks"].append(row)
        print(
            f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""),
            flush=True,
        )
        if not condition:
            raise RuntimeError(label)

    async def wait_for(
        self,
        label: str,
        loader: Callable[[], Any],
        predicate: Callable[[Any], bool],
        timeout: float | None = None,
    ) -> Any:
        deadline = time.monotonic() + (timeout or self.timeout)
        latest = None
        while time.monotonic() < deadline:
            latest = await loader()
            if predicate(latest):
                return latest
            await asyncio.sleep(0.5)
        raise TimeoutError(f"timed out waiting for {label}; latest={latest!r}")

    def queue(self, queue_name: str, operation: str) -> None:
        run_gcloud(
            "tasks",
            "queues",
            operation,
            queue_name,
            f"--location={REGION}",
            f"--project={PROJECT}",
            "--quiet",
        )

    def queue_state(self, queue_name: str) -> str:
        return run_gcloud(
            "tasks",
            "queues",
            "describe",
            queue_name,
            f"--location={REGION}",
            f"--project={PROJECT}",
            "--format=value(state)",
        )

    async def task_post(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        if not self.task_token:
            audience = self.cloud_env["COMMITMENTOS_TASKS_OIDC_AUDIENCE"]
            account = self.cloud_env["COMMITMENTOS_TASKS_SERVICE_ACCOUNT"]
            self.task_token = run_gcloud(
                "auth",
                "print-identity-token",
                f"--impersonate-service-account={account}",
                f"--audiences={audience}",
                "--include-email",
            )
        return await self.client.post(
            f"{self.service_url}{path}",
            headers={"Authorization": f"Bearer {self.task_token}"},
            json=dict(body),
        )

    def source_body(
        self,
        request_id: str,
        generation_id: str,
        page_sequence: int,
    ) -> Mapping[str, Any]:
        return {
            "schema_version": self.cloud_env["COMMITMENTOS_TASK_SCHEMA_VERSION"],
            "sync_request_id": request_id,
            "sync_generation_id": generation_id,
            "page_sequence": page_sequence,
            "source": "calendar",
            "user_id": self.settings.controlled_user_id,
            "trace_id": self.run_id,
        }

    def action_body(self, action: PendingAction) -> Mapping[str, Any]:
        return {
            "schema_version": self.cloud_env["COMMITMENTOS_TASK_SCHEMA_VERSION"],
            "outbox_id": action.outbox_id,
            "action_idempotency_key": action.action_key,
            "trace_id": self.run_id,
        }

    def observation_body(self, observation_id: str, generation: int) -> Mapping[str, Any]:
        return {
            "schema_version": self.cloud_env["COMMITMENTOS_TASK_SCHEMA_VERSION"],
            "observation_id": observation_id,
            "workflow_version": self.cloud_env["COMMITMENTOS_WORKFLOW_VERSION"],
            "dispatch_generation": generation,
            "trace_id": self.run_id,
        }

    async def preflight(self) -> None:
        await self.calendar.authorize()
        health = await self.client.get(f"{self.service_url}/health/live")
        self.check("gate revision health is live", health.status_code == 200, health.text[:120])
        for queue_name in (
            self.settings.source_sync_queue,
            self.settings.reconciliation_queue,
            self.settings.calendar_actions_queue,
        ):
            state = self.queue_state(queue_name)
            self.initial_queue_states[queue_name] = state
            self.check(f"queue {queue_name} starts RUNNING", state == "RUNNING", state)
        cursor = await self.fs.get("sync_cursors", f"calendar:{self.settings.controlled_user_id}")
        self.check("published Calendar cursor exists", cursor is not None)
        assert cursor is not None
        self.check(
            "Calendar truth starts eligible",
            not cursor.get("publish_in_progress_generation_id")
            and not cursor.get("full_resync_required"),
        )

    async def list_targets(self) -> int:
        """Print sanitized provider-owned blocks and gate eligibility."""
        try:
            await self.calendar.authorize()
            candidates: list[tuple[datetime, str, str, str, bool]] = []
            for block_id, block in (await self.fs.query("work_blocks")).items():
                start = block.get("scheduled_start")
                if (
                    not isinstance(start, datetime)
                    or not block.get("calendar_event_id")
                ):
                    continue
                try:
                    event = await self.calendar.get(str(block["calendar_event_id"]))
                except httpx.HTTPStatusError:
                    continue
                private = (event.get("extendedProperties") or {}).get("private") or {}
                if (
                    private.get("managed_by") == "commitmentos"
                    and private.get("work_block_id") == block_id
                ):
                    state = str(block.get("execution_state", "unknown"))
                    eligible = state == "planned" and start > utc_now()
                    candidates.append(
                        (
                            start,
                            prefix(block_id),
                            str(block.get("revision", "?")),
                            state,
                            eligible,
                        )
                    )
            candidates.sort()
            print(
                json.dumps(
                    [
                        {
                            "work_block_prefix": block_id,
                            "scheduled_start": iso(start),
                            "revision": revision,
                            "execution_state": state,
                            "gate_eligible": eligible,
                        }
                        for start, block_id, revision, state, eligible in candidates
                    ],
                    indent=2,
                )
            )
            return 0 if any(item[4] for item in candidates) else 1
        finally:
            await self.client.aclose()

    @staticmethod
    def seed_commitment_id(settings: Settings, run_tag: str) -> str:
        return CanonicalEncoder.hash(
            [
                "commitment:v1",
                settings.controlled_user_id,
                f"thread_seeded_slice_{run_tag}",
                f"message_seeded_slice_{run_tag}:span0",
            ]
        )

    async def seed_target(self, run_tag: str, effort: int, deadline_days: int) -> int:
        session_id: str | None = None
        try:
            commitment_id = self.seed_commitment_id(self.settings, run_tag)
            deadline = (utc_now() + timedelta(days=deadline_days)).replace(
                minute=0, second=0, microsecond=0
            )
            if await self.fs.get("commitments", commitment_id) is None:
                factory = ObservationFactory()
                observation = factory.source_change(
                    observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
                    user_id=self.settings.controlled_user_id,
                    producer_id=(
                        f"{self.settings.controlled_user_id}:message_seeded_slice_{run_tag}"
                    ),
                    producer_version=f"seeded-{run_tag}",
                    source="gmail",
                    external_id=f"message_seeded_slice_{run_tag}",
                    external_version=f"seeded-{run_tag}",
                    payload_hash=f"seeded-{run_tag}",
                    source_reference={
                        "thread_id": f"thread_seeded_slice_{run_tag}",
                        "message_id": f"message_seeded_slice_{run_tag}",
                    },
                    safe_metadata={
                        "seeded_commitment": {
                            "source_thread_id": f"thread_seeded_slice_{run_tag}",
                            "source_span_key": f"message_seeded_slice_{run_tag}:span0",
                            "title": "Phase 4AB live-gate fixture",
                            "description": "Isolated live-gate fixture; remove after evidence",
                            "ownership_type": "my_commitment",
                            "beneficiary": "CommitmentOS gate reviewer",
                            "deadline_value": deadline.isoformat(),
                            "deadline_expression": "five-day live-gate fixture deadline",
                            "deadline_confidence": 1.0,
                            "timezone": self.settings.controlled_timezone,
                            "proposed_effort_minutes": effort,
                            "effort_confidence": 1.0,
                            "semantic_fingerprint": f"my_commitment:phase4ab-gate:{run_tag}",
                            "evidence_excerpt": "Controlled Phase 4AB live-gate fixture",
                        }
                    },
                    observed_at=utc_now(),
                    trace_id=f"trace-phase4ab-seed-{run_tag}",
                )
                document = dict(ObservationSerializer().to_document(observation))
                document["reconciliation_status"] = "retryable_failed"
                await self.fs.set("source_observations", observation.observation_id, document)
                seeded = await self.task_post(
                    "/internal/tasks/reconcile-observation",
                    self.observation_body(observation.observation_id, 0),
                )
                self.check(
                    "seed observation reconciles through deployed workflow",
                    seeded.status_code == 200 and '"status":"completed"' in seeded.text,
                    seeded.text[:180],
                )
            else:
                self.check("seed command resumes its durable commitment", True)

            async def approval(request_type: str) -> tuple[str, dict[str, Any]] | None:
                rows = await self.fs.query("approvals")
                return next(
                    (
                        (approval_id, row)
                        for approval_id, row in rows.items()
                        if row.get("commitment_id") == commitment_id
                        and row.get("request_type") == request_type
                        and row.get("status") == "pending"
                    ),
                    None,
                )

            raw_session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            session_id = hashlib.sha256(raw_session.encode()).hexdigest()
            now = utc_now()
            await self.fs.set(
                "web_sessions",
                session_id,
                {
                    "user_id": self.settings.controlled_user_id,
                    "email": self.settings.controlled_email,
                    "csrf_secret": csrf,
                    "created_at": now,
                    "expires_at": now + timedelta(hours=1),
                    "revoked_at": None,
                },
            )

            async def resolve(
                approval_id: str, row: Mapping[str, Any], confirmed_minutes: int | None = None
            ) -> None:
                body: dict[str, Any] = {
                    "decision": "approve",
                    "expected_revision": row["revision"],
                }
                if confirmed_minutes is not None:
                    body["confirmed_minutes"] = confirmed_minutes
                response = await self.client.post(
                    f"{self.service_url}/api/v1/approvals/{approval_id}/resolve",
                    cookies={"commitmentos_session": raw_session},
                    headers={"X-CSRF-Token": csrf},
                    json=body,
                )
                self.check(
                    f"{row['request_type']} resolves through guarded route",
                    response.status_code == 200
                    and response.json().get("status") == "completed",
                    response.text[:180],
                )

            effort_approval = await self.wait_for(
                "seed effort approval",
                lambda: approval("effort_confirmation"),
                bool,
            )
            assert effort_approval is not None
            await resolve(*effort_approval, confirmed_minutes=effort)
            plan_approval = await self.wait_for(
                "seed initial plan approval",
                lambda: approval("initial_plan_approval"),
                bool,
            )
            assert plan_approval is not None
            await resolve(*plan_approval)

            async def executed_blocks() -> list[tuple[str, dict[str, Any]]]:
                blocks = [
                    (block_id, row)
                    for block_id, row in (await self.fs.query("work_blocks")).items()
                    if row.get("commitment_id") == commitment_id
                    and row.get("execution_state") == "planned"
                ]
                outbox = await self.fs.query("action_outbox")
                if blocks and all(
                    any(
                        action.get("work_block_id") == block_id
                        and action.get("execution_status") == "succeeded"
                        for action in outbox.values()
                    )
                    for block_id, _ in blocks
                ):
                    return blocks
                return []

            blocks = await self.wait_for(
                "seeded target execution",
                executed_blocks,
                bool,
            )
            self.check("seed creates exactly one planned block", len(blocks) == 1, len(blocks))
            block_id, block = blocks[0]
            await self.calendar.authorize()
            event = await self.calendar.get(str(block["calendar_event_id"]))
            private = (event.get("extendedProperties") or {}).get("private") or {}
            self.check(
                "seeded target has a real owned Calendar event",
                private.get("managed_by") == "commitmentos"
                and private.get("work_block_id") == block_id,
            )
            SEED_EVIDENCE_PATH.write_text(
                json.dumps(
                    {
                        "run_tag": run_tag,
                        "commitment_id": prefix(commitment_id),
                        "work_block_id": prefix(block_id),
                        "calendar_event_id": prefix(block["calendar_event_id"]),
                        "scheduled_start": iso(block["scheduled_start"]),
                        "workflow": "seeded_observation_to_deployed_approvals_outbox_executor",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            print(f"seeded gate target: {prefix(block_id)}", flush=True)
            return 0
        finally:
            if session_id:
                await self.fs.delete("web_sessions", session_id)
            await self.client.aclose()

    async def cleanup_seed(self, run_tag: str) -> int:
        try:
            commitment_id = self.seed_commitment_id(self.settings, run_tag)
            await self.calendar.authorize()
            blocks = [
                (block_id, row)
                for block_id, row in (await self.fs.query("work_blocks")).items()
                if row.get("commitment_id") == commitment_id
            ]
            for block_id, block in blocks:
                try:
                    event = await self.calendar.get(str(block["calendar_event_id"]))
                    private = (event.get("extendedProperties") or {}).get("private") or {}
                    if (
                        private.get("managed_by") == "commitmentos"
                        and private.get("work_block_id") == block_id
                    ):
                        await self.calendar.delete(event["id"], event.get("etag"))
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in (404, 410):
                        raise
            for collection, predicate in (
                (
                    "approvals",
                    lambda row: row.get("commitment_id") == commitment_id,
                ),
                ("action_outbox", lambda row: row.get("commitment_id") == commitment_id),
            ):
                for document_id, row in (await self.fs.query(collection)).items():
                    if predicate(row):
                        await self.fs.delete(collection, document_id)
            for block_id, _ in blocks:
                await self.fs.delete("work_blocks", block_id)
            await self.fs.delete("commitments", commitment_id)
            print(
                f"cleaned seeded fixture {run_tag}: {len(blocks)} block(s)",
                flush=True,
            )
            return 0
        finally:
            await self.client.aclose()

    async def inspect_target(self, work_block_prefix: str) -> int:
        try:
            blocks = [
                (block_id, row)
                for block_id, row in (await self.fs.query("work_blocks")).items()
                if block_id.startswith(work_block_prefix)
            ]
            if len(blocks) != 1:
                print(json.dumps({"block_matches": len(blocks)}))
                return 1
            block_id, block = blocks[0]
            approvals = [
                {
                    "id": prefix(approval_id),
                    "request_type": row.get("request_type"),
                    "status": row.get("status"),
                    "decision": row.get("decision"),
                    "created_at": iso(row["created_at"]),
                }
                for approval_id, row in (await self.fs.query("approvals")).items()
                if (row.get("payload") or {}).get("work_block_id") == block_id
            ]
            actions = [
                {
                    "id": prefix(outbox_id),
                    "execution_status": row.get("execution_status"),
                    "dispatch_status": row.get("dispatch_status"),
                    "action_type": (row.get("mutation") or {}).get("action_type"),
                    "expected_etag_hash": hashlib.sha256(
                        str(
                            (row.get("mutation") or {}).get(
                                "expected_observed_event_etag", ""
                            )
                        ).encode()
                    ).hexdigest()[:16],
                }
                for outbox_id, row in (await self.fs.query("action_outbox")).items()
                if row.get("work_block_id") == block_id
            ]
            observations = [
                {
                    "id": prefix(observation_id),
                    "type": row.get("observation_type"),
                    "status": row.get("reconciliation_status"),
                    "observed_start": (row.get("safe_metadata") or {}).get(
                        "observed_start"
                    ),
                }
                for observation_id, row in (
                    await self.fs.query("source_observations")
                ).items()
                if (row.get("safe_metadata") or {}).get("work_block_id") == block_id
            ]
            print(
                json.dumps(
                    {
                        "block": {
                            "id": prefix(block_id),
                            "execution_state": block.get("execution_state"),
                            "user_edit_state": block.get("user_edit_state"),
                            "revision": block.get("revision"),
                            "scheduled_start": iso(block["scheduled_start"]),
                        },
                        "approvals": approvals,
                        "actions": actions,
                        "observations": observations[-12:],
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0
        finally:
            await self.client.aclose()

    async def verify_closed(self, run_tag: str) -> int:
        try:
            await self.preflight()
            self.check(
                "production Calendar page size is restored",
                self.cloud_env.get("COMMITMENTOS_CALENDAR_SYNC_PAGE_SIZE") == "250",
            )
            self.check(
                "production apply chunk is restored",
                self.cloud_env.get("COMMITMENTOS_MAXIMUM_SYNC_APPLY_ITEMS_PER_CHUNK")
                == "100",
            )
            self.check(
                "publication barrier probe delay is disabled",
                self.cloud_env.get(
                    "COMMITMENTOS_SYNC_PUBLICATION_BARRIER_PROBE_DELAY_SECONDS"
                )
                == "0",
            )
            commitment_id = self.seed_commitment_id(self.settings, run_tag)
            self.check(
                "seeded gate commitment is removed",
                await self.fs.get("commitments", commitment_id) is None,
            )
            return 0
        finally:
            await self.client.aclose()

    async def prepare_412_action(self, work_block_prefix: str) -> PendingAction:
        blocks = await self.fs.query("work_blocks")
        matches = [
            (block_id, block)
            for block_id, block in blocks.items()
            if block_id.startswith(work_block_prefix)
        ]
        self.check("forced-412 target prefix resolves once", len(matches) == 1, len(matches))
        block_id, block = matches[0]
        self.check("forced-412 target is planned", block.get("execution_state") == "planned")
        event_id = str(block["calendar_event_id"])
        event = await self.calendar.get(event_id)
        private = (event.get("extendedProperties") or {}).get("private") or {}
        self.check(
            "forced-412 target is owned by CommitmentOS",
            private.get("managed_by") == "commitmentos"
            and private.get("work_block_id") == block_id,
        )
        provider_start = datetime.fromisoformat(
            event["start"]["dateTime"].replace("Z", "+00:00")
        )
        provider_end = datetime.fromisoformat(
            event["end"]["dateTime"].replace("Z", "+00:00")
        )
        start = block["scheduled_start"]
        end = block["scheduled_end"]
        if provider_start != start or provider_end != end:
            # A stopped gate may have published desired state while its held
            # action became stale during fixture cleanup. Restore the exact
            # app-owned desired interval and wait for the normal watch path
            # before arming another run.
            restored = await self.calendar.patch(
                event_id,
                {
                    "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": end.isoformat()},
                },
                event.get("etag"),
            )

            async def load_snapshot() -> dict[str, Any] | None:
                rows = await self.fs.query("calendar_event_snapshots")
                return next(
                    (
                        row
                        for row in rows.values()
                        if row.get("calendar_event_id") == event_id
                    ),
                    None,
                )

            await self.wait_for(
                "stopped-run provider alignment",
                load_snapshot,
                lambda value: bool(
                    value
                    and value.get("observed_event_etag") == restored.get("etag")
                    and value.get("observed_start") == start
                    and value.get("observed_end") == end
                ),
            )
            event = await self.calendar.get(event_id)
            self.restore_target = None
            self.check("provider truth is aligned before the new 412 run", True)
        self.queue(self.settings.calendar_actions_queue, "pause")
        # Queue pause does not cancel a delivery that Cloud Tasks has already
        # dispatched. Drain those deliveries before introducing the controlled
        # invalid move, otherwise an old executor can consume the repair intent
        # before the gate captures it in the pending state.
        await asyncio.sleep(15)
        outbox_before = set((await self.fs.query("action_outbox")))
        approvals_before = set((await self.fs.query("approvals")))
        duration = end - start
        local = ZoneInfo(self.settings.controlled_timezone)
        invalid_start = datetime.combine(
            start.astimezone(local).date(),
            datetime.min.time(),
            tzinfo=local,
        ) + timedelta(hours=3)
        invalid_end = invalid_start + duration
        self.restore_target = (event_id, block_id, start, end)
        invalid_move = await self.calendar.patch(
            event_id,
            {
                "start": {"dateTime": invalid_start.isoformat()},
                "end": {"dateTime": invalid_end.isoformat()},
            },
            event.get("etag"),
        )
        print("waiting for invalid-move repair intent", flush=True)

        async def load_decision() -> tuple[str, dict[str, Any]] | None:
            approvals = await self.fs.query("approvals")
            matches = [
                (approval_id, row)
                for approval_id, row in approvals.items()
                if approval_id not in approvals_before
                and row.get("request_type") == "calendar_invalid_move_decision"
                and row.get("status") == "pending"
                and (row.get("payload") or {}).get("work_block_id") == block_id
            ]
            if not matches:
                return None
            return max(matches, key=lambda item: (item[1].get("created_at"), item[0]))

        decision_id, decision = await self.wait_for(
            "invalid-move decision",
            load_decision,
            bool,
        )
        raw_session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        session_id = hashlib.sha256(raw_session.encode()).hexdigest()
        now = utc_now()
        await self.fs.set(
            "web_sessions",
            session_id,
            {
                "user_id": self.settings.controlled_user_id,
                "email": self.settings.controlled_email,
                "csrf_secret": csrf,
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
                "revoked_at": None,
            },
        )
        try:
            response = await self.client.post(
                f"{self.service_url}/api/v1/approvals/{decision_id}/resolve",
                cookies={"commitmentos_session": raw_session},
                headers={"X-CSRF-Token": csrf},
                json={
                    "decision": "approve",
                    "choice": "restore_approved_slot",
                    "expected_revision": decision["revision"],
                },
            )
            self.check(
                "invalid move is explicitly resolved to restore approved slot",
                response.status_code == 200
                and response.json().get("status") == "completed",
                response.text[:180],
            )
        finally:
            await self.fs.delete("web_sessions", session_id)

        async def load_actions() -> list[tuple[str, dict[str, Any]]]:
            rows = await self.fs.query("action_outbox")
            return [
                (outbox_id, row)
                for outbox_id, row in rows.items()
                if outbox_id not in outbox_before
                and row.get("work_block_id") == block_id
                and (row.get("mutation") or {}).get("action_type") == "patch"
            ]

        rows = await self.wait_for(
            "pending repair action",
            load_actions,
            lambda value: len(value) == 1 and value[0][1].get("execution_status") == "pending",
        )
        outbox_id, row = rows[0]
        mutation = row["mutation"]
        action = PendingAction(
            outbox_id=outbox_id,
            action_key=row["action_idempotency_key"],
            work_block_id=block_id,
            event_id=event_id,
            expected_etag=mutation["expected_observed_event_etag"],
            desired_start=mutation["desired_start"],
            desired_end=mutation["desired_end"],
        )
        self.check("repair action carries snapshot etag", bool(action.expected_etag))
        self.check(
            "repair action carries the invalid-move etag",
            action.expected_etag == invalid_move.get("etag"),
        )
        self.evidence["phase4b_412_target"] = {
            "work_block_id": prefix(block_id),
            "event_id": prefix(event_id),
            "outbox_id": prefix(outbox_id),
            "expected_etag_hash": hashlib.sha256(action.expected_etag.encode()).hexdigest()[:16],
            "repair_trigger": "invalid_user_move_outside_planning_hours",
        }
        return action

    async def create_planner_probe_observation(self) -> tuple[str, int]:
        plans = await self.fs.query("planner_runs")
        published = [
            (planner_id, plan)
            for planner_id, plan in plans.items()
            if plan.get("user_id") == self.settings.controlled_user_id
            and plan.get("status") == "published"
        ]
        self.check("a published planner run exists for barrier probe", bool(published))
        planner_id, _ = max(
            published,
            key=lambda item: (
                item[1].get("calculated_at") or datetime.min.replace(tzinfo=timezone.utc),
                item[0],
            ),
        )
        now = utc_now()
        observation = ObservationFactory().continuation(
            observation_type=ObservationType.PLAN_UNDO_REQUESTED,
            user_id=self.settings.controlled_user_id,
            producer_id=f"phase4ab-barrier-probe:{self.run_id}",
            producer_version="1",
            safe_metadata={
                "planner_run_id": planner_id,
                "requested_by": self.settings.controlled_user_id,
                "mode": "replan_from_current_facts",
                "gate_probe_run": self.run_id,
            },
            observed_at=now,
            trace_id=self.run_id,
        )
        document = dict(ObservationSerializer().to_document(observation))
        # Retryable observations are directly claimable but excluded from the
        # dispatch_pending scan. This keeps the refusal probe durable without
        # creating a Cloud Task that can race the direct barrier delivery.
        document["reconciliation_status"] = "retryable_failed"
        await self.fs.set(
            "source_observations",
            observation.observation_id,
            document,
        )
        self.check(
            "planner barrier probe observation is recorded as retryable",
            document["reconciliation_status"] == "retryable_failed",
        )
        stored = await self.fs.get("source_observations", observation.observation_id)
        self.check(
            "planner observation is durable and undispatched",
            stored is not None
            and stored.get("reconciliation_status") == "retryable_failed",
        )
        return observation.observation_id, observation.dispatch_generation

    async def create_two_page_fixtures(self) -> None:
        local = ZoneInfo(self.settings.controlled_timezone)
        fixture_day = (utc_now().astimezone(local) + timedelta(days=30)).date()
        base = datetime.combine(
            fixture_day,
            datetime.min.time(),
            tzinfo=local,
        ) + timedelta(hours=1)
        for index in range(11):
            start = base + timedelta(minutes=index * 2)
            event = await self.calendar.insert(
                {
                    "summary": f"Phase 4A transparent gate fixture {index + 1:02d}",
                    "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": (start + timedelta(minutes=1)).isoformat()},
                    "transparency": "transparent",
                    "extendedProperties": {"private": {"phase4ab_gate_run": self.run_id}},
                }
            )
            self.fixture_event_ids.append(event["id"])
        self.check(
            "created exactly 11 transparent gate fixtures", len(self.fixture_event_ids) == 11
        )

    async def run_phase4a(
        self,
        pending_action: PendingAction,
        planner_observation_id: str,
        planner_dispatch_generation: int,
    ) -> None:
        request_id = f"calendar:{self.settings.controlled_user_id}"
        await self.create_two_page_fixtures()
        # Calendar changes and watch signals are eventually visible. With the
        # queue held, this delay cannot start a generation; it only ensures the
        # direct bootstrap sees the complete 11-change set.
        await asyncio.sleep(10)

        async def load_request() -> dict[str, Any] | None:
            return await self.fs.get("sync_requests", request_id)

        request = await self.wait_for(
            "coalesced Calendar sync request",
            load_request,
            lambda value: bool(value and value.get("status") == "pending"),
        )
        cursor_id = request_id
        cursor_before = await self.fs.get("sync_cursors", cursor_id)
        assert cursor_before is not None
        bootstrap_generation = str(request["sync_generation_id"])
        page1 = await self.task_post(
            "/internal/tasks/source-sync",
            self.source_body(request_id, bootstrap_generation, 0),
        )
        self.check("page 1 task accepted", page1.status_code == 200, page1.text[:180])
        page1_json = page1.json()
        generation_id = page1_json["identifiers"]["sync_generation_id"]
        generation_page1 = await self.fs.get("sync_generations", generation_id)
        cursor_page1 = await self.fs.get("sync_cursors", cursor_id)
        assert generation_page1 is not None and cursor_page1 is not None
        self.check(
            "generation remains staging after page 1", generation_page1["status"] == "staging"
        )
        self.check("page 1 staged exactly 10 items", generation_page1["staged_item_count"] == 10)
        self.check("generation records one provider page", generation_page1["page_count"] == 1)
        self.check("page 1 has a continuation token", bool(generation_page1.get("next_page_token")))
        self.check(
            "candidate sync token is unpromoted after page 1",
            generation_page1.get("candidate_next_cursor") is None,
        )
        self.check(
            "published cursor is byte-identical after page 1",
            cursor_page1.get("published_cursor") == cursor_before.get("published_cursor")
            and cursor_page1.get("revision") == cursor_before.get("revision")
            and cursor_page1.get("calendar_state_revision")
            == cursor_before.get("calendar_state_revision"),
        )

        planner_runs_before = set(await self.fs.query("planner_runs"))
        page2_task = asyncio.create_task(
            self.task_post(
                "/internal/tasks/source-sync",
                self.source_body(request_id, generation_id, 2),
            )
        )
        barrier_seen = None
        barrier_deadline = time.monotonic() + 60
        while time.monotonic() < barrier_deadline and not page2_task.done():
            cursor = await self.fs.get("sync_cursors", cursor_id)
            if cursor and cursor.get("publish_in_progress_generation_id") == generation_id:
                barrier_seen = cursor
                break
            await asyncio.sleep(0.05)
        self.check("live publication barrier became observable", barrier_seen is not None)

        executor_probe, planner_probe = await asyncio.gather(
            self.task_post(
                "/internal/tasks/execute-calendar-action",
                self.action_body(pending_action),
            ),
            self.task_post(
                "/internal/tasks/reconcile-observation",
                self.observation_body(planner_observation_id, planner_dispatch_generation),
            ),
        )
        self.check(
            "executor refuses while publication barrier is held",
            executor_probe.status_code == 503
            and "calendar_truth_ineligible" in executor_probe.text,
            executor_probe.text[:160],
        )
        self.check(
            "planner refuses while publication barrier is held",
            planner_probe.status_code == 503 and "workflow_exception" in planner_probe.text,
            planner_probe.text[:160],
        )
        self.check(
            "planner refusal publishes no run",
            set(await self.fs.query("planner_runs")) == planner_runs_before,
        )
        action_after_probe = await self.fs.get("action_outbox", pending_action.outbox_id)
        self.check(
            "executor barrier refusal performs zero Calendar action",
            action_after_probe is not None
            and action_after_probe.get("execution_status") == "pending",
        )

        page2 = await page2_task
        self.check("page 2 applies and publishes", page2.status_code == 200, page2.text[:180])
        generation_final = await self.fs.get("sync_generations", generation_id)
        cursor_final = await self.fs.get("sync_cursors", cursor_id)
        assert generation_final is not None and cursor_final is not None
        self.check(
            "generation contains exactly two provider pages", generation_final["page_count"] == 2
        )
        self.check(
            "generation publishes all 11 items",
            generation_final["staged_item_count"] == 11
            and generation_final["applied_item_count"] == 11,
        )
        self.check(
            "staged and applied manifests match",
            generation_final["staged_manifest"] == generation_final["applied_manifest"],
        )
        self.check("generation is published", generation_final["status"] == "published")
        self.check(
            "publication barrier clears",
            cursor_final.get("publish_in_progress_generation_id") is None,
        )
        self.check(
            "candidate token promotes only at final publication",
            cursor_final.get("published_cursor") == generation_final.get("candidate_next_cursor"),
        )
        self.check(
            "cursor revision advances exactly once",
            cursor_final["revision"] == cursor_before["revision"] + 1,
        )
        self.check(
            "Calendar state revision advances exactly once",
            cursor_final["calendar_state_revision"] == cursor_before["calendar_state_revision"] + 1,
        )

        planner_after = await self.task_post(
            "/internal/tasks/reconcile-observation",
            self.observation_body(planner_observation_id, planner_dispatch_generation),
        )
        self.check(
            "planner succeeds after publication",
            planner_after.status_code == 200,
            planner_after.text[:180],
        )
        new_plans = {
            planner_id: row
            for planner_id, row in (await self.fs.query("planner_runs")).items()
            if planner_id not in planner_runs_before and row.get("status") == "published"
        }
        self.check("snapshot-driven planner run is published", len(new_plans) == 1, len(new_plans))
        planner_id, planner = next(iter(new_plans.items()))
        snapshots = await self.fs.query("calendar_event_snapshots")
        serializer = CalendarEventSnapshotSerializer()
        typed = tuple(serializer.from_document(item_id, row) for item_id, row in snapshots.items())
        snapshot_hash = CalendarSnapshotReducer().snapshot_hash(typed)
        self.check(
            "planner snapshot hash matches snapshot store byte-for-byte",
            planner["calendar_snapshot_hash"] == snapshot_hash,
        )
        self.check(
            "planner Calendar revision matches published cursor",
            planner["calendar_state_revision"] == cursor_final["calendar_state_revision"],
        )
        self.evidence["phase4a"] = {
            "generation_id": prefix(generation_id),
            "planner_run_id": prefix(planner_id),
            "page_count": generation_final["page_count"],
            "item_count": generation_final["staged_item_count"],
            "cursor_revision_before": cursor_before["revision"],
            "cursor_revision_after": cursor_final["revision"],
            "calendar_state_revision_before": cursor_before["calendar_state_revision"],
            "calendar_state_revision_after": cursor_final["calendar_state_revision"],
            "snapshot_hash": snapshot_hash,
        }

    async def valid_adoption_slot(
        self,
        block_id: str,
        block: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> TimeInterval:
        local = ZoneInfo(self.settings.controlled_timezone)
        old_start = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
        old_end = datetime.fromisoformat(event["end"]["dateTime"].replace("Z", "+00:00"))
        duration = old_end - old_start
        day = old_start.astimezone(local).date()
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=local)
        rows = await self.calendar.list_day(day_start, day_start + timedelta(days=1))
        occupied: list[TimeInterval] = []
        for row in rows:
            if row.get("id") == event.get("id") or row.get("status") == "cancelled":
                continue
            if row.get("transparency", "opaque") == "transparent":
                continue
            start_doc = row.get("start") or {}
            end_doc = row.get("end") or {}
            if start_doc.get("dateTime") and end_doc.get("dateTime"):
                start = datetime.fromisoformat(start_doc["dateTime"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(end_doc["dateTime"].replace("Z", "+00:00"))
                occupied.append(TimeInterval(start, end))
            elif start_doc.get("date") and end_doc.get("date"):
                start = datetime.fromisoformat(start_doc["date"]).replace(tzinfo=local)
                end = datetime.fromisoformat(end_doc["date"]).replace(tzinfo=local)
                occupied.append(TimeInterval(start, end))
        commitment = await self.fs.get("commitments", str(block["commitment_id"]))
        assert commitment is not None
        deadline = commitment["deadline"]["value"]
        candidate = datetime.combine(day, datetime.min.time(), tzinfo=local) + timedelta(hours=9)
        end_of_day = candidate.replace(hour=17, minute=30)
        while candidate + duration <= end_of_day:
            interval = TimeInterval(candidate, candidate + duration)
            if (
                interval.start.astimezone(timezone.utc) > utc_now() + timedelta(minutes=5)
                and interval.end.astimezone(timezone.utc) <= deadline.astimezone(timezone.utc)
                and interval.start.astimezone(timezone.utc) != old_start.astimezone(timezone.utc)
                and not any(interval.overlaps(value) for value in occupied)
            ):
                return interval
            candidate += timedelta(minutes=15)
        raise RuntimeError(f"no valid adoption slot for {block_id}")

    async def run_adoption(self, work_block_prefix: str) -> None:
        blocks = await self.fs.query("work_blocks")
        matches = [
            (item_id, row)
            for item_id, row in blocks.items()
            if item_id.startswith(work_block_prefix)
        ]
        self.check("adoption target prefix resolves once", len(matches) == 1, len(matches))
        block_id, block = matches[0]
        self.check("adoption target is planned", block.get("execution_state") == "planned")
        event = await self.calendar.get(str(block["calendar_event_id"]))
        private = (event.get("extendedProperties") or {}).get("private") or {}
        self.check(
            "adoption target is app-owned",
            private.get("managed_by") == "commitmentos"
            and private.get("work_block_id") == block_id,
        )
        slot = await self.valid_adoption_slot(block_id, block, event)
        outbox_before = set(await self.fs.query("action_outbox"))
        activity_before = set(await self.fs.query("activity_events"))
        moved = await self.calendar.patch(
            event["id"],
            {
                "start": {"dateTime": slot.start.isoformat()},
                "end": {"dateTime": slot.end.isoformat()},
            },
            event.get("etag"),
        )

        async def load_block() -> dict[str, Any] | None:
            return await self.fs.get("work_blocks", block_id)

        adopted = await self.wait_for(
            "valid user move adoption",
            load_block,
            lambda value: bool(
                value
                and value.get("user_edit_state") == "adopted"
                and value.get("scheduled_start") == slot.start.astimezone(timezone.utc)
                and value.get("scheduled_end") == slot.end.astimezone(timezone.utc)
            ),
        )
        outbox_after = set(await self.fs.query("action_outbox"))
        self.check(
            "valid adoption creates zero Calendar outbox mutations", outbox_after == outbox_before
        )
        activities = await self.fs.query("activity_events")
        adoption_events = [
            row
            for item_id, row in activities.items()
            if item_id not in activity_before
            and row.get("event_type") == "user_move_adopted"
            and (row.get("payload") or {}).get("work_block_id") == block_id
        ]
        self.check(
            "valid adoption records one explanation",
            len(adoption_events) == 1,
            len(adoption_events),
        )
        observations = await self.fs.query("source_observations")
        move_observations = [
            row
            for row in observations.values()
            if row.get("observation_type") == "calendar_user_move_valid"
            and (row.get("source_reference") or {}).get("work_block_id") == block_id
            and row.get("reconciliation_status") == "processed"
        ]
        self.check("real move is typed and processed", bool(move_observations))
        self.evidence["phase4b_adoption"] = {
            "work_block_id": prefix(block_id),
            "event_id": prefix(event["id"]),
            "provider_etag_hash": hashlib.sha256(str(moved.get("etag", "")).encode()).hexdigest()[
                :16
            ],
            "plan_revision": adopted["plan_revision"],
            "outbox_delta": len(outbox_after - outbox_before),
            "explanation_count": len(adoption_events),
        }

    async def run_412(self, action: PendingAction) -> None:
        self.queue(self.settings.source_sync_queue, "pause")
        # Stop new sync deliveries and let any already-dispatched generation
        # publish before changing provider truth behind the stable snapshot.
        await asyncio.sleep(15)

        async def eligible_cursor() -> dict[str, Any] | None:
            return await self.fs.get(
                "sync_cursors", f"calendar:{self.settings.controlled_user_id}"
            )

        await self.wait_for(
            "Calendar truth eligibility before forced 412",
            eligible_cursor,
            lambda value: bool(
                value
                and not value.get("publish_in_progress_generation_id")
                and not value.get("full_resync_required")
            ),
        )
        event_before = await self.calendar.get(action.event_id)
        snapshot_before_rows = await self.fs.query("calendar_event_snapshots")
        snapshot_before = next(
            row
            for row in snapshot_before_rows.values()
            if row.get("calendar_event_id") == action.event_id
        )
        self.check(
            "intent etag equals published snapshot before 412",
            action.expected_etag == snapshot_before.get("observed_event_etag"),
        )
        changed = await self.calendar.patch(
            action.event_id,
            {"description": f"Phase 4B forced-412 marker {self.run_id}"},
            event_before.get("etag"),
        )
        self.check(
            "provider etag changed behind snapshot", changed.get("etag") != action.expected_etag
        )
        observations_before = set(await self.fs.query("source_observations"))
        stale_response = await self.task_post(
            "/internal/tasks/execute-calendar-action",
            self.action_body(action),
        )
        self.check(
            "deployed executor acknowledges real provider 412",
            stale_response.status_code == 200
            and "calendar_precondition_stale" in stale_response.text,
            stale_response.text[:180],
        )
        stale = await self.fs.get("action_outbox", action.outbox_id)
        self.check(
            "old intent is terminally stale_precondition",
            stale is not None and stale.get("execution_status") == "stale_precondition",
        )
        observations_after_stale = await self.fs.query("source_observations")
        old_results = [
            row
            for item_id, row in observations_after_stale.items()
            if item_id not in observations_before
            and row.get("observation_type") == "action_result"
            and (row.get("safe_metadata") or {}).get("outbox_id") == action.outbox_id
        ]
        self.check("412 emits no action_result observation", len(old_results) == 0)
        sync_request = await self.fs.get(
            "sync_requests", f"calendar:{self.settings.controlled_user_id}"
        )
        self.check(
            "412 commits one coalesced Calendar sync request",
            sync_request is not None and sync_request.get("status") == "pending",
        )

        old_outbox_ids = set(await self.fs.query("action_outbox"))
        self.queue(self.settings.source_sync_queue, "resume")

        async def load_resumed() -> list[tuple[str, dict[str, Any]]]:
            rows = await self.fs.query("action_outbox")
            return [
                (item_id, row)
                for item_id, row in rows.items()
                if item_id not in old_outbox_ids
                and row.get("work_block_id") == action.work_block_id
                and (row.get("mutation") or {}).get("action_type") == "patch"
            ]

        resumed_rows = await self.wait_for(
            "new-etag resumed outbox intent",
            load_resumed,
            lambda value: len(value) == 1,
        )
        resumed_id, resumed_row = resumed_rows[0]
        resumed_mutation = resumed_row["mutation"]
        resumed = PendingAction(
            outbox_id=resumed_id,
            action_key=resumed_row["action_idempotency_key"],
            work_block_id=action.work_block_id,
            event_id=action.event_id,
            expected_etag=resumed_mutation["expected_observed_event_etag"],
            desired_start=resumed_mutation["desired_start"],
            desired_end=resumed_mutation["desired_end"],
        )
        self.check(
            "resumed intent preserves desired repair",
            resumed.desired_start == action.desired_start
            and resumed.desired_end == action.desired_end,
        )
        self.check(
            "resumed intent carries the independently synchronized etag",
            resumed.expected_etag == changed.get("etag")
            and resumed.expected_etag != action.expected_etag,
        )
        resumed_response = await self.task_post(
            "/internal/tasks/execute-calendar-action",
            self.action_body(resumed),
        )
        self.check(
            "resumed conditional patch succeeds",
            resumed_response.status_code == 200 and '"status":"completed"' in resumed_response.text,
            resumed_response.text[:180],
        )
        resumed_final = await self.fs.get("action_outbox", resumed.outbox_id)
        self.check(
            "resumed outbox is succeeded",
            resumed_final is not None and resumed_final.get("execution_status") == "succeeded",
        )
        provider_final = await self.calendar.get(action.event_id)
        provider_start = datetime.fromisoformat(
            provider_final["start"]["dateTime"].replace("Z", "+00:00")
        )
        provider_end = datetime.fromisoformat(
            provider_final["end"]["dateTime"].replace("Z", "+00:00")
        )
        self.check(
            "provider lands at the preserved desired interval",
            provider_start == action.desired_start and provider_end == action.desired_end,
        )
        self.restore_target = None
        self.evidence["phase4b_412"] = {
            "stale_outbox_id": prefix(action.outbox_id),
            "resumed_outbox_id": prefix(resumed.outbox_id),
            "old_etag_hash": hashlib.sha256(action.expected_etag.encode()).hexdigest()[:16],
            "new_etag_hash": hashlib.sha256(resumed.expected_etag.encode()).hexdigest()[:16],
            "old_status": stale["execution_status"] if stale else "missing",
            "resumed_status": resumed_final["execution_status"] if resumed_final else "missing",
            "action_result_count_for_stale_intent": len(old_results),
        }

    async def delete_conflict_fixture(self) -> None:
        if not self.conflict_event_id:
            return
        event_id = self.conflict_event_id
        try:
            event = await self.calendar.get(event_id)
            private = (event.get("extendedProperties") or {}).get("private") or {}
            if private.get("phase4ab_gate_run") == self.run_id:
                await self.calendar.delete(event_id, event.get("etag"))
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in (404, 410):
                raise
        self.conflict_event_id = None

    async def restore_invalid_move(self) -> None:
        if not self.restore_target:
            return
        event_id, block_id, start, end = self.restore_target
        event = await self.calendar.get(event_id)
        private = (event.get("extendedProperties") or {}).get("private") or {}
        if (
            private.get("managed_by") == "commitmentos"
            and private.get("work_block_id") == block_id
        ):
            provider_start = datetime.fromisoformat(
                event["start"]["dateTime"].replace("Z", "+00:00")
            )
            provider_end = datetime.fromisoformat(
                event["end"]["dateTime"].replace("Z", "+00:00")
            )
            if provider_start != start or provider_end != end:
                await self.calendar.patch(
                    event_id,
                    {
                        "start": {"dateTime": start.isoformat()},
                        "end": {"dateTime": end.isoformat()},
                    },
                    event.get("etag"),
                )
        self.restore_target = None

    async def cleanup(self) -> None:
        # Restore observation processing first.  Keep Calendar actions paused
        # until the exact gate fixtures are removed, so a failed run cannot
        # release an obsolete repair while its synthetic conflict still exists.
        for queue_name in (
            self.settings.source_sync_queue,
            self.settings.reconciliation_queue,
        ):
            try:
                self.queue(queue_name, "resume")
            except Exception as error:  # noqa: BLE001
                print(f"cleanup warning: could not resume {queue_name}: {error}", flush=True)
        try:
            await self.delete_conflict_fixture()
        except httpx.HTTPStatusError as error:
            print(f"cleanup warning: conflict deletion: {error}", flush=True)
        try:
            await self.restore_invalid_move()
        except httpx.HTTPStatusError as error:
            print(f"cleanup warning: invalid-move restoration: {error}", flush=True)
        for event_id in self.fixture_event_ids:
            try:
                event = await self.calendar.get(event_id)
                private = (event.get("extendedProperties") or {}).get("private") or {}
                if private.get("phase4ab_gate_run") == self.run_id:
                    await self.calendar.delete(event_id, event.get("etag"))
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in (404, 410):
                    print(f"cleanup warning: fixture deletion: {error}", flush=True)
        try:
            await asyncio.sleep(10)
            self.queue(self.settings.calendar_actions_queue, "resume")
        except Exception as error:  # noqa: BLE001
            print(
                "cleanup warning: could not resume "
                f"{self.settings.calendar_actions_queue}: {error}",
                flush=True,
            )

    async def run(self, adoption_prefix: str, stale_prefix: str) -> int:
        failure: str | None = None
        try:
            await self.preflight()
            pending_action = await self.prepare_412_action(stale_prefix)
            self.queue(self.settings.source_sync_queue, "pause")
            self.queue(self.settings.reconciliation_queue, "pause")
            # Pausing does not cancel a delivery already in flight. Let any
            # pre-pause cleanup/conflict task finish before freezing the cursor
            # baseline for the exact two-page proof.
            await asyncio.sleep(15)
            (
                planner_observation_id,
                planner_dispatch_generation,
            ) = await self.create_planner_probe_observation()
            await self.run_phase4a(
                pending_action,
                planner_observation_id,
                planner_dispatch_generation,
            )
            self.queue(self.settings.source_sync_queue, "resume")
            self.queue(self.settings.reconciliation_queue, "resume")
            # The post-publication planner run can supersede the action used
            # for the barrier refusal. Remove that probe conflict, let normal
            # truth processing settle, then arm a fresh intent for HTTP 412.
            await self.delete_conflict_fixture()
            await asyncio.sleep(15)
            pending_action = await self.prepare_412_action(stale_prefix)
            await self.run_412(pending_action)
            await self.run_adoption(adoption_prefix)
        except Exception as error:  # noqa: BLE001
            failure = f"{type(error).__name__}: {error}"
            print(f"GATE FAILED: {failure}", flush=True)
        finally:
            try:
                await self.cleanup()
            finally:
                self.evidence["completed_at"] = iso(utc_now())
                self.evidence["result"] = "passed" if failure is None else "failed"
                if failure:
                    self.evidence["failure"] = failure
                EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
                EVIDENCE_PATH.write_text(
                    json.dumps(self.evidence, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                await self.client.aclose()
        passed = sum(1 for row in self.evidence["checks"] if row["passed"])
        total = len(self.evidence["checks"])
        print(f"checkpoints: {passed}/{total} passed", flush=True)
        return 0 if failure is None else 1


async def async_main(args: argparse.Namespace) -> int:
    gate = Gate(args.service_url, args.timeout)
    if args.command == "list-targets":
        return await gate.list_targets()
    if args.command == "seed-target":
        return await gate.seed_target(args.run_tag, args.effort, args.deadline_days)
    if args.command == "cleanup-seed":
        return await gate.cleanup_seed(args.run_tag)
    if args.command == "inspect-target":
        return await gate.inspect_target(args.work_block)
    if args.command == "verify-closed":
        return await gate.verify_closed(args.run_tag)
    return await gate.run(args.adoption_block, args.stale_block)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--service-url", required=True)
    run_parser.add_argument("--timeout", type=float, default=180)
    run_parser.add_argument("--adoption-block", default="4e03f71438a0")
    run_parser.add_argument("--stale-block", default="99423c96ecdc")
    targets_parser = sub.add_parser("list-targets")
    targets_parser.add_argument("--service-url", required=True)
    targets_parser.add_argument("--timeout", type=float, default=180)
    seed_parser = sub.add_parser("seed-target")
    seed_parser.add_argument("--service-url", required=True)
    seed_parser.add_argument("--timeout", type=float, default=240)
    seed_parser.add_argument("--run-tag", required=True)
    seed_parser.add_argument("--effort", type=int, default=60)
    seed_parser.add_argument("--deadline-days", type=int, default=5)
    cleanup_parser = sub.add_parser("cleanup-seed")
    cleanup_parser.add_argument("--service-url", required=True)
    cleanup_parser.add_argument("--timeout", type=float, default=240)
    cleanup_parser.add_argument("--run-tag", required=True)
    inspect_parser = sub.add_parser("inspect-target")
    inspect_parser.add_argument("--service-url", required=True)
    inspect_parser.add_argument("--timeout", type=float, default=240)
    inspect_parser.add_argument("--work-block", required=True)
    verify_parser = sub.add_parser("verify-closed")
    verify_parser.add_argument("--service-url", required=True)
    verify_parser.add_argument("--timeout", type=float, default=240)
    verify_parser.add_argument("--run-tag", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
