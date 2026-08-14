"""Phase 3 gate driver — 3A/3B controlled-account live closure.

Drives the eight live-closure steps recorded in `docs/phase3b_progress.md`
(plus the 3A steps in `docs/phase3a_progress.md`) against the deployed
service, in separately runnable subcommands:

    busy-setup        create unrelated busy events on the controlled calendar
                      (timed opaque, recurring opaque, all-day opaque, one
                      transparent event that must NOT count as busy)
    busy-read         production GoogleCalendarReader horizon read (3A proof)
    mint-session      create a controlled web session directly in Firestore so
                      the guarded HTTPS routes can be exercised end to end
    status            sanitized dump of commitments/approvals/planner runs
    resolve           POST /api/v1/approvals/{id}/resolve via session + CSRF
    verify-run        constraint verification of a published planner_runs doc
    verify-calendar   poll outbox execution; verify real events + stable IDs
    elapse-block      planned -> awaiting_check_in via the production domain
                      transition (driver stands in for the Phase 4 elapse
                      scan, which does not exist yet by design)
    check-in          POST /api/v1/work-blocks/{id}/check-in, then redeliver
                      the identical request and prove convergence
    state-hash        canonical digest of durable state for replay comparison
    replay-observations   redeliver reconcile-observation tasks (OIDC) -> no_op
    replay-actions    redeliver execute-calendar-action tasks -> no duplicates
    cleanup-session   revoke the minted web session

The driver reuses the production composition root (real Firestore unit of
work, production repositories and serializers). Mutations that the product
guards behind the controlled session travel through the deployed HTTPS routes
with a real session cookie and CSRF header; the driver writes Firestore
directly only where it stands in for the user's browser session issuance or
for later-phase machinery, and says so explicitly.

Usage:
    .venv/bin/python scripts/run_phase3_gate.py <subcommand> [options]
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

from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.domain.planning.intervals import TimeInterval  # noqa: E402
from commitmentos.infrastructure.firestore.repositories.implementations import (  # noqa: E402
    ACTION_OUTBOX,
    APPROVALS,
    COMMITMENTS,
    EVIDENCE,
    PLANNER_RUNS,
    SOURCE_OBSERVATIONS,
    WORK_BLOCKS,
)
from commitmentos.infrastructure.google.calendar_reader import GoogleCalendarReader  # noqa: E402
from commitmentos.infrastructure.google.credentials import (  # noqa: E402
    ControlledCredentialsProvider,
)

SESSION_FILE = Path.home() / ".commitmentos_phase3_session.json"

# Frozen golden defaults committed by PlanningInputReader (3A).
WORK_START = (9, 0)
WORK_END = (17, 30)
MIN_BLOCK_MINUTES = 30
MAX_BLOCK_MINUTES = 60
DAILY_FOCUS_LIMIT = 180

_checkpoints: list[tuple[str, bool, str]] = []


def checkpoint(label: str, ok: bool, detail: str = "") -> None:
    _checkpoints.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def finish() -> int:
    failures = [label for label, ok, _ in _checkpoints if not ok]
    if _checkpoints:
        print(f"\ncheckpoints: {len(_checkpoints) - len(failures)}/{len(_checkpoints)} passed")
    return 1 if failures else 0


class GateDriver:
    def __init__(self, settings: Settings, service_url: str) -> None:
        self.settings = settings
        self.service_url = service_url.rstrip("/")
        self.container = ApplicationContainer.build(settings)
        self.uow = self.container.unit_of_work()
        self.credentials = ControlledCredentialsProvider(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )
        self.calendar_reader = GoogleCalendarReader(self.credentials)

    # -- raw Firestore reads (equality-only queries; no composite indexes) --

    async def query(self, collection: str, filters: list[tuple[str, str, Any]]) -> list:
        async def _load(repositories):
            return await repositories._context.query(collection, filters)

        return await self.uow.read(_load)

    async def get_raw(self, collection: str, document_id: str) -> Mapping[str, Any] | None:
        async def _load(repositories):
            return await repositories._context.get(collection, document_id)

        return await self.uow.read(_load)

    def user_filter(self) -> list[tuple[str, str, Any]]:
        return [("user_id", "==", self.settings.controlled_user_id)]

    # -- session-backed HTTPS calls ----------------------------------------

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

    # -- OIDC task redelivery ----------------------------------------------

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


# ---------------------------------------------------------------------------


def _calendar_service(driver: GateDriver):
    from googleapiclient.discovery import build

    return build(
        "calendar", "v3", credentials=driver.credentials.credentials(), cache_discovery=False
    )


def _local_tz(driver: GateDriver) -> ZoneInfo:
    return ZoneInfo(driver.settings.controlled_timezone)


async def cmd_busy_setup(driver: GateDriver, args: argparse.Namespace) -> int:
    """Create unrelated (non-CommitmentOS) events shaping real busy time."""
    tz = _local_tz(driver)
    now_local = datetime.now(tz)
    day = now_local.date()
    timed_start = datetime(day.year, day.month, day.day, 13, 0, tzinfo=tz)
    if timed_start < now_local + timedelta(minutes=45):
        timed_start = datetime(day.year, day.month, day.day, 16, 0, tzinfo=tz)
    if timed_start < now_local + timedelta(minutes=45):
        next_day = day + timedelta(days=1)
        timed_start = datetime(next_day.year, next_day.month, next_day.day, 13, 0, tzinfo=tz)
    transparent_start = timed_start + timedelta(hours=1)
    recur_day = day + timedelta(days=1)
    recur_start = datetime(recur_day.year, recur_day.month, recur_day.day, 11, 0, tzinfo=tz)
    all_day = day + timedelta(days=3)

    service = _calendar_service(driver)
    calendar_id = driver.settings.calendar_id

    def _insert(body: dict) -> str:
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
        return created["id"]

    def _rfc(moment: datetime) -> dict:
        return {"dateTime": moment.isoformat(), "timeZone": driver.settings.controlled_timezone}

    events = {
        "timed_opaque": {
            "summary": "Phase3 gate — unrelated meeting (busy)",
            "start": _rfc(timed_start),
            "end": _rfc(timed_start + timedelta(hours=1)),
        },
        "recurring_opaque": {
            "summary": "Phase3 gate — recurring hold (busy)",
            "start": _rfc(recur_start),
            "end": _rfc(recur_start + timedelta(minutes=30)),
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"],
        },
        "all_day_opaque": {
            "summary": "Phase3 gate — all-day hold (busy)",
            "start": {"date": all_day.isoformat()},
            "end": {"date": (all_day + timedelta(days=1)).isoformat()},
            "transparency": "opaque",
        },
        "transparent_timed": {
            "summary": "Phase3 gate — transparent hold (must NOT count)",
            "start": _rfc(transparent_start),
            "end": _rfc(transparent_start + timedelta(hours=1)),
            "transparency": "transparent",
        },
    }
    created = {}
    for label, body in events.items():
        created[label] = _insert(body)
        print(f"created {label}: {created[label]}")
    print(json.dumps({"busy_fixture_event_ids": created}, indent=2))
    return 0


async def cmd_busy_read(driver: GateDriver, args: argparse.Namespace) -> int:
    """3A live proof: production adapter horizon read over real busy time."""
    now = datetime.now(timezone.utc)
    horizon = TimeInterval(now, now + timedelta(days=args.days))
    intervals = await driver.calendar_reader.list_busy_intervals(
        driver.settings.calendar_id, horizon, driver.settings.controlled_timezone
    )
    tz = _local_tz(driver)
    zone = driver.settings.controlled_timezone
    print(f"busy intervals over {args.days}d horizon (times in {zone}):")
    for busy in intervals:
        start = busy.interval.start.astimezone(tz)
        end = busy.interval.end.astimezone(tz)
        minutes = int((busy.interval.end - busy.interval.start).total_seconds() // 60)
        print(
            f"  {start:%a %m-%d %H:%M} -> {end:%a %m-%d %H:%M}  ({minutes}m)"
            f"  owned={busy.is_app_owned}  event={busy.calendar_event_id[:14]}…"
        )
    checkpoint("busy read returned normalized intervals", len(intervals) > 0, str(len(intervals)))
    return finish()


async def cmd_mint_session(driver: GateDriver, args: argparse.Namespace) -> int:
    """Stand-in for the browser login: issue a controlled-user session doc.

    The raw token never leaves the local session file; Firestore stores only
    its SHA-256, exactly as the production login flow does.
    """
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session = {
        "session_id_hash": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": driver.settings.controlled_user_id,
        "email": driver.settings.controlled_email,
        "csrf_secret": csrf,
        "created_at": now,
        "expires_at": now + timedelta(hours=6),
        "revoked_at": None,
    }

    async def _create(repositories):
        await repositories.web_sessions.create(session)
        return True

    await driver.uow.run(_create)
    SESSION_FILE.write_text(json.dumps({"session_token": token, "csrf_secret": csrf}))
    SESSION_FILE.chmod(0o600)
    print(
        f"session minted (hash {session['session_id_hash'][:12]}…), expires {session['expires_at']:%H:%M}Z"
    )
    return 0


async def cmd_cleanup_session(driver: GateDriver, args: argparse.Namespace) -> int:
    if not SESSION_FILE.exists():
        print("no session file")
        return 0
    token, _ = driver.load_session()
    session_hash = hashlib.sha256(token.encode()).hexdigest()

    async def _revoke(repositories):
        await repositories.web_sessions.revoke(session_hash, datetime.now(timezone.utc))
        return True

    await driver.uow.run(_revoke)
    SESSION_FILE.unlink()
    print(f"session {session_hash[:12]}… revoked")
    return 0


async def cmd_status(driver: GateDriver, args: argparse.Namespace) -> int:
    commitments = await driver.query(COMMITMENTS, driver.user_filter())
    print("commitments:")
    for doc_id, doc in commitments:
        print(
            f"  {doc_id[:12]}…  rev={doc.get('revision')}  {doc.get('lifecycle_status')}"
            f"  deadline={doc.get('deadline', {}).get('value')}"
            f"  effort={(doc.get('effort') or {}).get('confirmed_minutes')}"
            f"  title={str(doc.get('title'))[:50]!r}"
        )
    approvals = await driver.query(APPROVALS, driver.user_filter())
    print("approvals:")
    for doc_id, doc in approvals:
        print(
            f"  {doc_id[:12]}…  {doc.get('request_type')}  status={doc.get('status')}"
            f"  rev={doc.get('revision')}  commitment={str(doc.get('commitment_id'))[:12]}…"
        )
    runs = await driver.query(PLANNER_RUNS, driver.user_filter())
    print("planner runs:")
    for doc_id, doc in runs:
        print(f"  {doc_id[:16]}…  status={doc.get('status')}  created={doc.get('created_at')}")
    blocks = await driver.query(WORK_BLOCKS, [])
    print("work blocks (all):")
    for doc_id, doc in blocks:
        print(
            f"  {doc_id[:12]}…  {doc.get('execution_state')}  rev={doc.get('revision')}"
            f"  {doc.get('scheduled_start')} -> {doc.get('scheduled_end')}"
            f"  commitment={str(doc.get('commitment_id'))[:12]}…"
        )
    return 0


async def cmd_resolve(driver: GateDriver, args: argparse.Namespace) -> int:
    """Resolve an approval through the guarded HTTPS route."""
    approval = await driver.get_raw(APPROVALS, args.approval_id)
    if approval is None:
        matches = [
            (doc_id, doc)
            for doc_id, doc in await driver.query(APPROVALS, driver.user_filter())
            if doc_id.startswith(args.approval_id)
        ]
        if len(matches) != 1:
            print(f"approval {args.approval_id!r} not found ({len(matches)} prefix matches)")
            return 1
        args.approval_id, approval = matches[0]
    body: dict[str, Any] = {
        "decision": args.decision,
        "expected_revision": approval["revision"],
    }
    if args.minutes is not None:
        body["confirmed_minutes"] = args.minutes
    response = await driver.api_post(f"/api/v1/approvals/{args.approval_id}/resolve", body)
    print(f"HTTP {response.status_code}: {response.text[:400]}")
    checkpoint(
        f"{approval.get('request_type')} resolved via guarded route",
        response.status_code == 200 and response.json().get("status") == "completed",
        args.approval_id[:12],
    )
    return finish()


def _extract_desired_blocks(doc: Any, path: str = "") -> list[dict]:
    """Recursively find allocation entries carrying commitment + start/end."""
    found: list[dict] = []
    if isinstance(doc, Mapping):
        keys = set(doc.keys())
        if {"commitment_id"} <= keys and any("start" in key for key in keys):
            found.append(dict(doc))
        else:
            for value in doc.values():
                found.extend(_extract_desired_blocks(value, path))
    elif isinstance(doc, list):
        for value in doc:
            found.extend(_extract_desired_blocks(value, path))
    return found


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


async def cmd_verify_run(driver: GateDriver, args: argparse.Namespace) -> int:
    """Step 4+5: verify the published planner run and its proposed intervals."""
    runs = await driver.query(PLANNER_RUNS, driver.user_filter())
    if args.planner_run_id:
        selected = [(i, d) for i, d in runs if i.startswith(args.planner_run_id)]
    else:
        selected = sorted(runs, key=lambda pair: str(pair[1].get("created_at")))[-1:]
    if len(selected) != 1:
        print(f"planner run selection ambiguous ({len(selected)})")
        return 1
    run_id, doc = selected[0]
    print(f"planner run {run_id}")
    print(f"  top-level keys: {sorted(doc.keys())}")

    raw = json.dumps(doc, default=str)
    commitments = await driver.query(COMMITMENTS, driver.user_filter())
    active = [
        (i, d)
        for i, d in commitments
        if d.get("lifecycle_status") in ("active", "awaiting_confirmation", "planning")
        and (d.get("effort") or {}).get("confirmed_minutes")
    ]
    for commitment_id, commitment in active:
        checkpoint(
            f"run references commitment {commitment_id[:12]}…",
            commitment_id in raw,
            commitment.get("lifecycle_status", ""),
        )
    checkpoint("run records a status", "status" in doc, str(doc.get("status")))

    desired = _extract_desired_blocks(doc)
    intervals: list[tuple[str, datetime, datetime]] = []
    for entry in desired:
        start = _parse_dt(
            entry.get("scheduled_start") or entry.get("start") or entry.get("start_at")
        )
        end = _parse_dt(entry.get("scheduled_end") or entry.get("end") or entry.get("end_at"))
        if start and end:
            intervals.append((str(entry.get("commitment_id")), start, end))
    print(f"  desired intervals found: {len(intervals)}")

    tz = _local_tz(driver)
    now = datetime.now(timezone.utc)
    horizon = TimeInterval(now, now + timedelta(days=10))
    busy = await driver.calendar_reader.list_busy_intervals(
        driver.settings.calendar_id, horizon, driver.settings.controlled_timezone
    )
    owned_event_ids = {
        doc.get("calendar_event_id") for _, doc in await driver.query(WORK_BLOCKS, [])
    }
    unrelated_busy = [
        b for b in busy if b.calendar_event_id not in owned_event_ids and not b.is_app_owned
    ]

    deadline_by_commitment = {
        i: _parse_dt(d.get("deadline", {}).get("value")) for i, d in commitments
    }
    all_ok = bool(intervals)
    per_day: dict[tuple[str, Any], int] = {}
    for commitment_id, start, end in sorted(intervals, key=lambda item: item[1]):
        problems = []
        local_start, local_end = start.astimezone(tz), end.astimezone(tz)
        minutes = int((end - start).total_seconds() // 60)
        if args.plan_time is None and start <= now - timedelta(minutes=1):
            problems.append("in the past")
        if (local_start.hour, local_start.minute) < WORK_START or (
            local_end.hour,
            local_end.minute,
        ) > WORK_END:
            problems.append("outside working hours")
        if not MIN_BLOCK_MINUTES <= minutes <= MAX_BLOCK_MINUTES:
            problems.append(f"bad duration {minutes}")
        deadline = deadline_by_commitment.get(commitment_id)
        if deadline and end > deadline:
            problems.append("after deadline")
        for other_commitment, other_start, other_end in intervals:
            if (start, end) != (other_start, other_end) and (
                start < other_end and other_start < end
            ):
                problems.append(f"overlaps allocation of {other_commitment[:8]}")
        for b in unrelated_busy:
            if start < b.interval.end and b.interval.start < end:
                problems.append("overlaps unrelated busy time")
        day_key = (commitment_id[:1] and "all", local_start.date())
        per_day[day_key] = per_day.get(day_key, 0) + minutes
        label = f"{local_start:%a %m-%d %H:%M}->{local_end:%H:%M} {commitment_id[:12]}…"
        checkpoint(f"interval valid: {label}", not problems, "; ".join(problems))
        all_ok = all_ok and not problems
    for (_, day), total in sorted(per_day.items()):
        checkpoint(f"daily focus total {day} within limit", total <= DAILY_FOCUS_LIMIT, f"{total}m")
    checkpoint("plan proposes at least one interval", bool(intervals), str(len(intervals)))
    return finish()


async def cmd_verify_calendar(driver: GateDriver, args: argparse.Namespace) -> int:
    """Step 6: after approval — outbox executed, real events, stable IDs."""
    from commitmentos.workflows.reconciliation.phase1_workflow import derive_calendar_event_id

    deadline = time.monotonic() + args.timeout
    blocks: list[tuple[str, Mapping[str, Any]]] = []
    while time.monotonic() < deadline:
        blocks = [
            (i, d)
            for i, d in await driver.query(WORK_BLOCKS, [])
            if str(d.get("commitment_id", "")).startswith(args.commitment_id)
        ]
        if blocks:
            outbox = await driver.query(ACTION_OUTBOX, [])
            by_block: dict[str, list[str]] = {}
            for _, row in outbox:
                by_block.setdefault(str(row.get("work_block_id")), []).append(
                    str(row.get("execution_status"))
                )
            if all("succeeded" in by_block.get(i, []) for i, _ in blocks):
                break
        await asyncio.sleep(3)
    checkpoint("work blocks exist for commitment", bool(blocks), f"{len(blocks)} blocks")
    for block_id, doc in blocks:
        expected = derive_calendar_event_id(driver.settings.calendar_id, block_id)
        checkpoint(
            f"stable derived event ID for {block_id[:12]}…",
            doc.get("calendar_event_id") == expected,
            "",
        )
        event = await driver.calendar_reader.get_event(
            driver.settings.calendar_id, str(doc.get("calendar_event_id"))
        )
        properties = event.payload.get("extendedProperties", {}).get("private", {}) if event else {}
        checkpoint(
            f"live event carries ownership properties for {block_id[:12]}…",
            event is not None
            and properties.get("managed_by") == "commitmentos"
            and properties.get("work_block_id") == block_id,
            str(doc.get("calendar_event_id"))[:16],
        )
    return finish()


async def cmd_elapse_block(driver: GateDriver, args: argparse.Namespace) -> int:
    """Driver stands in for the Phase 4 elapse scan: planned -> awaiting_check_in.

    Uses the production domain transition + repository save under the real
    unit of work; recorded as an annotated stand-in in the gate evidence.
    """

    async def _elapse(repositories):
        blocks = [
            (i, d)
            for i, d in await repositories._context.query(WORK_BLOCKS, [])
            if i.startswith(args.block_id)
        ]
        if len(blocks) != 1:
            raise SystemExit(f"block {args.block_id!r}: {len(blocks)} matches")
        block_id, _ = blocks[0]
        block = await repositories.work_blocks.get(block_id)
        updated = block.request_check_in(datetime.now(timezone.utc))
        await repositories.work_blocks.save(updated, block.revision)
        return block_id, updated.execution_state.value, updated.revision

    block_id, state, revision = await driver.uow.run(_elapse)
    print(f"block {block_id[:12]}… -> {state} (rev {revision})")
    return 0


async def cmd_check_in(driver: GateDriver, args: argparse.Namespace) -> int:
    """3A live: guarded check-in + identical redelivery convergence."""
    blocks = [(i, d) for i, d in await driver.query(WORK_BLOCKS, []) if i.startswith(args.block_id)]
    if len(blocks) != 1:
        print(f"block {args.block_id!r}: {len(blocks)} matches")
        return 1
    block_id, block = blocks[0]
    body = {
        "idempotency_key": args.key,
        "completed": bool(args.completed),
        "verified_minutes": args.minutes,
        "checked_in_at": datetime.now(timezone.utc).isoformat(),
        "expected_revision": block["revision"],
    }
    first = await driver.api_post(f"/api/v1/work-blocks/{block_id}/check-in", body)
    print(f"first delivery  HTTP {first.status_code}: {first.text[:300]}")
    checkpoint(
        "check-in recorded via guarded route",
        first.status_code == 200 and first.json().get("status") == "completed",
        "",
    )
    second = await driver.api_post(f"/api/v1/work-blocks/{block_id}/check-in", body)
    print(f"second delivery HTTP {second.status_code}: {second.text[:300]}")
    checkpoint(
        "identical redelivery converges without a second record",
        second.status_code == 200 and second.json().get("status") == "no_op",
        second.json().get("error_code", "") if second.status_code == 200 else "",
    )
    evidence = [
        (i, d)
        for i, d in await driver.query(EVIDENCE, [("work_block_id", "==", block_id)])
        if d.get("client_idempotency_key") == args.key
    ]
    checkpoint("exactly one evidence record for the idempotency key", len(evidence) == 1, "")
    refreshed = await driver.get_raw(WORK_BLOCKS, block_id)
    checkpoint(
        "exactly one revision advance",
        refreshed is not None and refreshed.get("revision") == block["revision"] + 1,
        f"rev {block['revision']} -> {refreshed.get('revision') if refreshed else '?'}",
    )
    return finish()


async def _state_digest(driver: GateDriver) -> tuple[str, dict[str, int]]:
    view: dict[str, Any] = {}
    counts: dict[str, int] = {}
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
        APPROVALS: lambda d: {
            "revision": d.get("revision"),
            "status": d.get("status"),
            "type": d.get("request_type"),
        },
        PLANNER_RUNS: lambda d: {"status": d.get("status")},
        EVIDENCE: lambda d: {"type": d.get("evidence_type")},
        SOURCE_OBSERVATIONS: lambda d: {"status": d.get("reconciliation_status")},
        ACTION_OUTBOX: lambda d: {
            "dispatch": d.get("dispatch_status"),
            "execution": d.get("execution_status"),
        },
    }
    for collection, select in selectors.items():
        rows = await driver.query(collection, [])
        view[collection] = {doc_id: select(doc) for doc_id, doc in sorted(rows)}
        counts[collection] = len(rows)
    blocks = await driver.query(WORK_BLOCKS, [])
    events = {}
    for _, doc in sorted(blocks):
        event_id = str(doc.get("calendar_event_id"))
        event = await driver.calendar_reader.get_event(driver.settings.calendar_id, event_id)
        events[event_id] = getattr(event, "etag", None)
    view["calendar_event_etags"] = events
    digest = hashlib.sha256(json.dumps(view, sort_keys=True, default=str).encode()).hexdigest()
    return digest, counts


async def cmd_state_hash(driver: GateDriver, args: argparse.Namespace) -> int:
    digest, counts = await _state_digest(driver)
    print(f"state digest: {digest}")
    print(json.dumps(counts, indent=2))
    return 0


async def cmd_replay_observations(driver: GateDriver, args: argparse.Namespace) -> int:
    """Step 7: redeliver reconcile-observation tasks -> no_op, state unchanged."""
    rows = await driver.query(SOURCE_OBSERVATIONS, driver.user_filter())
    processed = [
        (i, d)
        for i, d in rows
        if d.get("reconciliation_status") == "processed"
        and (not args.since or str(d.get("observed_at", ""))[:19] >= args.since)
    ]
    processed = sorted(processed, key=lambda pair: str(pair[1].get("observed_at")))
    if args.limit:
        processed = processed[-args.limit :]
    before, _ = await _state_digest(driver)
    token = driver.tasks_identity_token()
    for observation_id, doc in processed:
        payload = {
            "schema_version": driver.settings.task_schema_version,
            "observation_id": observation_id,
            "workflow_version": doc.get("workflow_version", driver.settings.workflow_version),
            "dispatch_generation": doc.get("dispatch_generation", 1),
            "trace_id": f"phase3-gate-replay-{observation_id[:8]}",
        }
        response = await driver.task_post("/internal/tasks/reconcile-observation", payload, token)
        summary = response.json() if response.status_code == 200 else response.text[:120]
        checkpoint(
            f"replayed observation {observation_id[:12]}…",
            response.status_code == 200,
            str(summary)[:80],
        )
    after, _ = await _state_digest(driver)
    checkpoint("durable state digest identical after observation replay", before == after, "")
    return finish()


async def cmd_replay_actions(driver: GateDriver, args: argparse.Namespace) -> int:
    """Step 7: redeliver execute-calendar-action tasks -> zero duplicates."""
    rows = await driver.query(ACTION_OUTBOX, [])
    succeeded = [(i, d) for i, d in rows if d.get("execution_status") == "succeeded"]
    if args.commitment_id:
        succeeded = [
            (i, d)
            for i, d in succeeded
            if str(d.get("commitment_id", "")).startswith(args.commitment_id)
        ]
    before, _ = await _state_digest(driver)
    token = driver.tasks_identity_token()
    for outbox_id, doc in succeeded:
        payload = {
            "schema_version": driver.settings.task_schema_version,
            "outbox_id": outbox_id,
            "action_idempotency_key": doc.get("action_idempotency_key", outbox_id),
            "trace_id": f"phase3-gate-replay-{outbox_id[:8]}",
        }
        response = await driver.task_post("/internal/tasks/execute-calendar-action", payload, token)
        summary = response.json() if response.status_code == 200 else response.text[:120]
        checkpoint(
            f"replayed action {outbox_id[:12]}…",
            response.status_code == 200,
            str(summary)[:80],
        )
    after, _ = await _state_digest(driver)
    checkpoint("durable state digest identical after action replay", before == after, "")
    return finish()


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("busy-setup")
    p = sub.add_parser("busy-read")
    p.add_argument("--days", type=int, default=6)
    sub.add_parser("mint-session")
    sub.add_parser("cleanup-session")
    sub.add_parser("status")
    p = sub.add_parser("resolve")
    p.add_argument("--approval-id", required=True)
    p.add_argument("--decision", default="approve")
    p.add_argument("--minutes", type=int, default=None)
    p = sub.add_parser("verify-run")
    p.add_argument("--planner-run-id", default=None)
    p.add_argument("--plan-time", default=None)
    p = sub.add_parser("verify-calendar")
    p.add_argument("--commitment-id", required=True)
    p.add_argument("--timeout", type=float, default=240.0)
    p = sub.add_parser("elapse-block")
    p.add_argument("--block-id", required=True)
    p = sub.add_parser("check-in")
    p.add_argument("--block-id", required=True)
    p.add_argument("--minutes", type=int, required=True)
    p.add_argument("--completed", action="store_true")
    p.add_argument("--key", default="phase3-gate-checkin-001")
    sub.add_parser("state-hash")
    p = sub.add_parser("replay-observations")
    p.add_argument("--since", default=None)
    p.add_argument("--limit", type=int, default=8)
    p = sub.add_parser("replay-actions")
    p.add_argument("--commitment-id", default=None)
    args = parser.parse_args()

    settings = Settings.load()
    service_url = args.service_url or str(settings.service_base_url)
    driver = GateDriver(settings, service_url)
    handlers = {
        "busy-setup": cmd_busy_setup,
        "busy-read": cmd_busy_read,
        "mint-session": cmd_mint_session,
        "cleanup-session": cmd_cleanup_session,
        "status": cmd_status,
        "resolve": cmd_resolve,
        "verify-run": cmd_verify_run,
        "verify-calendar": cmd_verify_calendar,
        "elapse-block": cmd_elapse_block,
        "check-in": cmd_check_in,
        "state-hash": cmd_state_hash,
        "replay-observations": cmd_replay_observations,
        "replay-actions": cmd_replay_actions,
    }
    sys.exit(asyncio.run(handlers[args.command](driver, args)))


if __name__ == "__main__":
    main()
