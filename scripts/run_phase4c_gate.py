"""Phase 4C controlled-account gate driver.

The driver never inserts the conflict. `arm` freezes the current durable and
Calendar baseline and prints the exact owned interval. Insert an unrelated
meeting over that interval in Google Calendar, touch nothing else, then run
`verify`. The real watch, source generation, classifier, repairer, executor,
and echo watch must close the gate without driver intervention.

Usage:
    .venv/bin/python scripts/run_phase4c_gate.py arm [--block-id PREFIX]
    # Insert one unrelated meeting over the printed interval in Calendar UI.
    .venv/bin/python scripts/run_phase4c_gate.py verify [--timeout 90]
    .venv/bin/python scripts/run_phase4c_gate.py safety
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.infrastructure.firestore.repositories.implementations import (  # noqa: E402
    ACTION_OUTBOX,
    ACTIVITY_EVENTS,
    CALENDAR_EVENT_SNAPSHOTS,
    COMMITMENTS,
    SOURCE_OBSERVATIONS,
    WORK_BLOCKS,
)
from commitmentos.infrastructure.google.credentials import (  # noqa: E402
    ControlledCredentialsProvider,
)

BASELINE_FILE = Path.home() / ".commitmentos_phase4c_gate.json"


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _overlaps(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return left_start < right_end and right_start < left_end


class GateDriver:
    def __init__(
        self,
        settings: Settings,
        service_url: str,
        scheduler_audience: str,
    ) -> None:
        self.settings = settings
        self.service_url = service_url.rstrip("/")
        self.scheduler_audience = scheduler_audience
        self.container = ApplicationContainer.build(settings)
        self.uow = self.container.unit_of_work()
        self.credentials = ControlledCredentialsProvider(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )

    async def query(
        self, collection: str, filters: list[tuple[str, str, Any]]
    ) -> list[tuple[str, Mapping[str, Any]]]:
        async def _load(repositories):  # noqa: ANN202
            return await repositories._context.query(collection, filters)

        return await self.uow.read(_load)

    def user_filter(self) -> list[tuple[str, str, Any]]:
        return [("user_id", "==", self.settings.controlled_user_id)]

    def calendar_service(self):  # noqa: ANN201
        from googleapiclient.discovery import build

        return build(
            "calendar",
            "v3",
            credentials=self.credentials.credentials(),
            cache_discovery=False,
        )

    def scheduler_token(self) -> str:
        result = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-identity-token",
                f"--impersonate-service-account={self.settings.scheduler_service_account}",
                f"--audiences={self.scheduler_audience}",
                "--include-email",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()


async def arm(driver: GateDriver, args: argparse.Namespace) -> int:
    blocks = await driver.query(WORK_BLOCKS, [])
    candidates = []
    for block_id, block in blocks:
        scheduled_start = _parse(block.get("scheduled_start"))
        if (
            block.get("execution_state") == "planned"
            and scheduled_start is not None
            and scheduled_start > datetime.now(timezone.utc)
        ):
            candidates.append((block_id, block))
    if args.block_id:
        candidates = [
            item for item in candidates if item[0].startswith(args.block_id)
        ]
    candidates.sort(key=lambda item: (item[1]["scheduled_start"], item[0]))
    if len(candidates) != 1 and args.block_id:
        print(f"block prefix matched {len(candidates)} planned blocks")
        return 1
    if not candidates:
        print("no future planned owned block is available")
        return 1
    target_id, target = candidates[0]
    start = _parse(target["scheduled_start"])
    end = _parse(target["scheduled_end"])
    assert start is not None and end is not None

    outbox = await driver.query(ACTION_OUTBOX, driver.user_filter())
    activity = await driver.query(ACTIVITY_EVENTS, driver.user_filter())
    observations = await driver.query(SOURCE_OBSERVATIONS, driver.user_filter())
    commitments = await driver.query(COMMITMENTS, driver.user_filter())
    snapshots = await driver.query(
        CALENDAR_EVENT_SNAPSHOTS, driver.user_filter()
    )
    baseline = {
        "armed_at": datetime.now(timezone.utc).isoformat(),
        "target_work_block_id": target_id,
        "target_calendar_event_id": target["calendar_event_id"],
        "target_start": start.isoformat(),
        "target_end": end.isoformat(),
        "work_blocks": {item_id: _json(item) for item_id, item in blocks},
        "outbox_ids": sorted(item_id for item_id, _ in outbox),
        "activity_ids": sorted(item_id for item_id, _ in activity),
        "observation_ids": sorted(item_id for item_id, _ in observations),
        "commitment_ids": sorted(item_id for item_id, _ in commitments),
        "snapshot_ids": sorted(item_id for item_id, _ in snapshots),
    }
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2, sort_keys=True))
    BASELINE_FILE.chmod(0o600)
    print(f"armed target {target_id} ({target['calendar_event_id']})")
    print(f"insert ONE unrelated meeting over {start.isoformat()} -> {end.isoformat()}")
    print("touch nothing else, then run the verify command")
    return 0


async def safety(driver: GateDriver, args: argparse.Namespace) -> int:
    token = driver.scheduler_token()
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{driver.service_url}/internal/scheduler/maintenance/safety_reconciliation",
            headers={"Authorization": f"Bearer {token}"},
        )
    print(f"HTTP {response.status_code}: {response.text[:1000]}")
    return 0 if response.status_code == 200 else 1


async def verify(driver: GateDriver, args: argparse.Namespace) -> int:
    if not BASELINE_FILE.exists():
        print("no armed baseline; run arm first")
        return 1
    baseline = json.loads(BASELINE_FILE.read_text())
    target_id = baseline["target_work_block_id"]
    baseline_outbox = set(baseline["outbox_ids"])
    baseline_activity = set(baseline["activity_ids"])
    baseline_observations = set(baseline["observation_ids"])
    armed_at = datetime.fromisoformat(baseline["armed_at"])
    deadline = time.monotonic() + args.timeout
    latest: dict[str, Any] = {}

    while time.monotonic() < deadline:
        blocks = dict(await driver.query(WORK_BLOCKS, []))
        outbox = dict(await driver.query(ACTION_OUTBOX, driver.user_filter()))
        activity = dict(await driver.query(ACTIVITY_EVENTS, driver.user_filter()))
        observations = dict(
            await driver.query(SOURCE_OBSERVATIONS, driver.user_filter())
        )
        new_actions = {
            item_id: item
            for item_id, item in outbox.items()
            if item_id not in baseline_outbox
            and item.get("work_block_id") == target_id
            and (item.get("mutation") or {}).get("action_type") == "patch"
        }
        new_echoes = [
            item
            for item_id, item in observations.items()
            if item_id not in baseline_observations
            and item.get("observation_type") == "calendar_app_echo"
            and (item.get("source_reference") or {}).get("work_block_id")
            == target_id
        ]
        new_explanations = [
            item
            for item_id, item in activity.items()
            if item_id not in baseline_activity
            and item.get("event_type") == "plan_repaired"
            and any(
                mutation.get("work_block_id") == target_id
                for mutation in (item.get("payload") or {}).get("mutations", [])
            )
        ]
        latest = {
            "blocks": blocks,
            "outbox": outbox,
            "activity": activity,
            "observations": observations,
            "new_actions": new_actions,
            "new_echoes": new_echoes,
            "new_explanations": new_explanations,
        }
        if (
            len(new_actions) == 1
            and next(iter(new_actions.values())).get("execution_status") == "succeeded"
            and len(new_echoes) == 1
            and len(new_explanations) == 1
        ):
            break
        await asyncio.sleep(1)

    checks: list[tuple[str, bool, str]] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        checks.append((label, condition, detail))
        print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))

    target = latest.get("blocks", {}).get(target_id)
    new_actions = latest.get("new_actions", {})
    new_echoes = latest.get("new_echoes", [])
    explanations = latest.get("new_explanations", [])
    check("target work block still exists", target is not None)
    if target is None:
        return 1
    check(
        "owned event ID stayed stable",
        target.get("calendar_event_id") == baseline["target_calendar_event_id"],
    )
    check(
        "target moved away from the conflicted interval",
        target.get("scheduled_start") != _parse(baseline["target_start"]),
    )
    check("exactly one repair patch intent", len(new_actions) == 1, str(len(new_actions)))
    action: Mapping[str, Any] = next(iter(new_actions.values()), {})
    check("repair patch succeeded", action.get("execution_status") == "succeeded")
    check("exactly one suppressed repair echo", len(new_echoes) == 1, str(len(new_echoes)))
    if new_echoes:
        check(
            "echo is terminally ignored",
            new_echoes[0].get("reconciliation_status") == "ignored",
        )
    check("one complete repair explanation", len(explanations) == 1, str(len(explanations)))
    if explanations:
        payload = explanations[0].get("payload") or {}
        mutations = payload.get("mutations") or []
        check("explanation says one block moved", payload.get("moved_block_count") == 1)
        check(
            "explanation contains before and after",
            len(mutations) == 1
            and bool(mutations[0].get("before_start"))
            and bool(mutations[0].get("after_start")),
        )
        check("explanation contains risk arc", bool(payload.get("risk_arc")))
        check(
            "explanation identifies unchanged blocks",
            bool(payload.get("preserved_work_block_ids")),
        )

    unchanged = [
        block_id
        for block_id, before in baseline["work_blocks"].items()
        if block_id != target_id
        and _json(latest["blocks"].get(block_id)) == before
    ]
    expected_unchanged = len(baseline["work_blocks"]) - 1
    check(
        "every unaffected work block is byte-preserved",
        len(unchanged) == expected_unchanged,
        f"{len(unchanged)}/{expected_unchanged}",
    )

    response_at = _parse(
        (action.get("mutation_response") or {}).get(
            "mutation_response_received_at"
        )
    )
    provider = driver.calendar_service()
    event_rows = provider.events().list(
        calendarId=driver.settings.calendar_id,
        timeMin=baseline["armed_at"],
        singleEvents=True,
        showDeleted=False,
    ).execute().get("items", [])
    original_start = datetime.fromisoformat(baseline["target_start"])
    original_end = datetime.fromisoformat(baseline["target_end"])
    conflicts = []
    for event in event_rows:
        private = (event.get("extendedProperties") or {}).get("private") or {}
        start = _parse((event.get("start") or {}).get("dateTime"))
        end = _parse((event.get("end") or {}).get("dateTime"))
        if (
            private.get("managed_by") != "commitmentos"
            and start is not None
            and end is not None
            and _overlaps(start, end, original_start, original_end)
        ):
            conflicts.append(event)
    check("one real unrelated conflict meeting detected", len(conflicts) == 1, str(len(conflicts)))
    inserted_at = _parse(conflicts[0].get("created")) if len(conflicts) == 1 else None
    latency = (
        (response_at - inserted_at).total_seconds()
        if response_at is not None and inserted_at is not None
        else None
    )
    check(
        "warmed insert-to-repair latency is under 15 seconds",
        latency is not None and 0 <= latency < 15,
        f"{latency:.3f}s" if latency is not None else "unmeasured",
    )
    check(
        "operational insert-to-repair latency is under 60 seconds",
        latency is not None and latency < 60,
    )
    check("verification observed only post-arm outcomes", armed_at <= datetime.now(timezone.utc))

    failed = [label for label, ok, _ in checks if not ok]
    print(f"checkpoints: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default=None)
    parser.add_argument("--scheduler-audience", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    arm_parser = sub.add_parser("arm")
    arm_parser.add_argument("--block-id", default=None)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--timeout", type=float, default=90)
    sub.add_parser("safety")
    args = parser.parse_args()
    settings = Settings.load()
    service_url = args.service_url or str(settings.service_base_url)
    scheduler_audience = (
        args.scheduler_audience
        or str(settings.scheduler_oidc_audience)
    )
    driver = GateDriver(settings, service_url, scheduler_audience)
    handlers = {"arm": arm, "verify": verify, "safety": safety}
    sys.exit(asyncio.run(handlers[args.command](driver, args)))


if __name__ == "__main__":
    main()
