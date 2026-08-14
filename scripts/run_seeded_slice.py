"""Phase 1 gate driver — the seeded vertical slice against the deployed service.

Drives plan §17 Phase 1's gate end to end:

    seeded observation -> reconciliation (deployed) -> effort approval ->
    initial-plan approval -> work blocks + outbox -> authenticated executor
    (deployed, real Calendar) -> action_result -> follow-up reconciliation

The driver reuses the production composition root: observations, approvals,
and dispatch all go through the real Firestore unit of work and real named
Cloud Tasks that deliver to the deployed Cloud Run handlers with OIDC. Every
bounded step therefore runs on a fresh instance-equivalent (scale-to-zero),
which is the recoverable-across-recycling property in live form.

Each invocation uses a unique run tag inside the fixture identifiers, so
deterministic observation/commitment IDs stay replay-safe per run and reruns
start clean.

Usage:
    .venv/bin/python scripts/run_seeded_slice.py run [--cleanup] [--timeout 240]
    .venv/bin/python scripts/run_seeded_slice.py cleanup --run-tag <tag>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from commitmentos.application.dto import AuthenticatedActor, CommandStatus  # noqa: E402
from commitmentos.bootstrap.container import ApplicationContainer  # noqa: E402
from commitmentos.bootstrap.settings import Settings  # noqa: E402
from commitmentos.contracts.observations import ObservationType  # noqa: E402
from commitmentos.domain.actions.models import (  # noqa: E402
    CalendarActionType,
    CalendarMutation,
)
from commitmentos.infrastructure.google.calendar_reader import GoogleCalendarReader  # noqa: E402
from commitmentos.infrastructure.google.calendar_writer import GoogleCalendarWriter  # noqa: E402
from commitmentos.infrastructure.google.credentials import (  # noqa: E402
    ControlledCredentialsProvider,
)
from commitmentos.workflows.reconciliation.phase1_workflow import (  # noqa: E402
    derive_calendar_event_id,
)

_checkpoints: list[tuple[str, bool, str]] = []


def checkpoint(label: str, ok: bool, detail: str = "") -> None:
    _checkpoints.append((label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


class SeededSliceDriver:
    def __init__(self, settings: Settings, run_tag: str) -> None:
        self.settings = settings
        self.run_tag = run_tag
        self.container = ApplicationContainer.build(settings)
        self.uow = self.container.unit_of_work()
        self.controlled = self.container.controlled_commands()
        credentials = ControlledCredentialsProvider(
            settings.oauth_client_secret_ref,
            settings.controlled_refresh_token_secret_ref,
        )
        self.calendar_reader = GoogleCalendarReader(credentials)
        self.calendar_writer = GoogleCalendarWriter(credentials)
        self.commitment_id: str | None = None

    # ------------------------------------------------------------------

    def _actor(self) -> AuthenticatedActor:
        return AuthenticatedActor(
            user_id=self.settings.controlled_user_id,
            email=self.settings.controlled_email,
            session_id=f"seeded-slice-driver-{self.run_tag}",
            authenticated_at=datetime.now(timezone.utc),
        )

    def _seed_payload(self, deadline_days: int, effort_minutes: int) -> dict:
        deadline = (datetime.now(timezone.utc) + timedelta(days=deadline_days)).replace(
            minute=0, second=0, microsecond=0
        )
        return {
            "source_thread_id": f"thread_seeded_slice_{self.run_tag}",
            "source_span_key": f"message_seeded_slice_{self.run_tag}:span0",
            "title": "Send revised proposal to Professor Chen (seeded slice)",
            "description": "Phase 1 seeded vertical-slice fixture",
            "ownership_type": "my_commitment",
            "beneficiary": "Professor Chen",
            "deadline_value": deadline.isoformat(),
            "deadline_expression": "seeded fixture deadline",
            "deadline_confidence": 0.93,
            "timezone": self.settings.controlled_timezone,
            "proposed_effort_minutes": effort_minutes,
            "effort_confidence": 0.58,
            "semantic_fingerprint": f"my_commitment:seeded-slice:{self.run_tag}",
            "evidence_excerpt": "Yes—I'll have it back before the review. (seeded)",
        }

    async def seed(self, deadline_days: int, effort_minutes: int) -> str:
        from commitmentos.domain.shared.types import CanonicalEncoder

        # The workflow derives the commitment ID from the seeded thread/span;
        # computing it here up front keys every later poll to THIS run.
        self.commitment_id = CanonicalEncoder.hash(
            [
                "commitment:v1",
                self.settings.controlled_user_id,
                f"thread_seeded_slice_{self.run_tag}",
                f"message_seeded_slice_{self.run_tag}:span0",
            ]
        )
        factory = self.container.observation_factory
        observation = factory.source_change(
            observation_type=ObservationType.GMAIL_MESSAGE_CHANGED,
            user_id=self.settings.controlled_user_id,
            producer_id=(
                f"{self.settings.controlled_user_id}:message_seeded_slice_{self.run_tag}"
            ),
            producer_version=f"seeded-{self.run_tag}",
            source="gmail",
            external_id=f"message_seeded_slice_{self.run_tag}",
            external_version=f"seeded-{self.run_tag}",
            payload_hash=f"seeded-{self.run_tag}",
            source_reference={
                "thread_id": f"thread_seeded_slice_{self.run_tag}",
                "message_id": f"message_seeded_slice_{self.run_tag}",
            },
            safe_metadata={
                "seeded_commitment": self._seed_payload(deadline_days, effort_minutes)
            },
            observed_at=datetime.now(timezone.utc),
            trace_id=f"trace-seeded-slice-{self.run_tag}",
        )

        async def _create(repositories):
            created = await repositories.observations.create(observation)
            return created

        created = await self.uow.run(_create)
        checkpoint("observation committed before dispatch", created, observation.observation_id[:12])
        result = await self.container.observation_dispatcher.dispatch(observation.observation_id)
        checkpoint(
            "named reconciliation task created after commit",
            result is not None,
            result.task_name if result else "",
        )
        return observation.observation_id

    # ------------------------------------------------------------------

    async def _pending_approval(self, request_type: str) -> dict | None:
        async def _load(repositories):
            return list(
                await repositories.approvals.list_pending(self.settings.controlled_user_id)
            )

        for approval in await self.uow.read(_load):
            if (
                approval.get("request_type") == request_type
                and approval.get("commitment_id") == self.commitment_id
            ):
                return approval
        return None

    async def wait_for_approval(self, request_type: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            approval = await self._pending_approval(request_type)
            if approval is not None:
                checkpoint(f"{request_type} approval pending", True, approval["approval_id"][:12])
                return approval
            await asyncio.sleep(3)
        checkpoint(f"{request_type} approval pending", False, f"timeout {timeout}s")
        raise TimeoutError(request_type)

    async def resolve(self, approval: dict, **extra) -> None:
        result = await self.controlled.resolve_approval.execute(
            self._actor(),
            approval["approval_id"],
            {"decision": "approve", **extra},
            approval["revision"],
            trace_id=f"trace-seeded-slice-{self.run_tag}",
        )
        checkpoint(
            f"{approval.get('request_type')} resolved once",
            result.status == CommandStatus.COMPLETED,
            str(result.error_code or ""),
        )

    # ------------------------------------------------------------------

    async def _slice_state(self) -> dict:
        commitment_id = self.commitment_id

        async def _load(repositories):
            commitment = await repositories.commitments.get(commitment_id)
            blocks = list(await repositories.work_blocks.list_for_commitment(commitment_id))
            return commitment, blocks

        commitment, blocks = await self.uow.read(_load)
        return {"commitment": commitment, "blocks": blocks}

    async def wait_for_execution(self, timeout: float) -> list:
        """Wait until every work block has one succeeded outbox action."""
        assert self.commitment_id is not None
        deadline = time.monotonic() + timeout
        blocks: list = []
        while time.monotonic() < deadline:
            state = await self._slice_state()
            blocks = state["blocks"]
            if blocks:
                statuses = await self._statuses_by_block()
                if all("succeeded" in statuses.get(block.work_block_id, []) for block in blocks):
                    checkpoint(
                        "every work block executed via deployed executor",
                        True,
                        f"{len(blocks)} blocks",
                    )
                    return blocks
            await asyncio.sleep(3)
        checkpoint("every work block executed via deployed executor", False, "timeout")
        raise TimeoutError("execution")

    async def _outbox_rows(self) -> list[tuple[str, dict]]:
        """All outbox rows for this run's commitment (equality-only query;
        resume revalidation may replace originals with epoch-suffixed intents,
        so deterministic-ID lookups alone would miss the executing records)."""
        from commitmentos.infrastructure.firestore.repositories.implementations import (
            ACTION_OUTBOX,
        )

        commitment_id = self.commitment_id

        async def _load(repositories):
            return await repositories._context.query(
                ACTION_OUTBOX, [("commitment_id", "==", commitment_id)]
            )

        return await self.uow.read(_load)

    async def _statuses_by_block(self) -> dict[str, list[str]]:
        statuses: dict[str, list[str]] = {}
        for _, row in await self._outbox_rows():
            statuses.setdefault(row["work_block_id"], []).append(row["execution_status"])
        return statuses

    async def verify_calendar(self, blocks: list) -> None:
        for block in blocks:
            expected_id = derive_calendar_event_id(self.settings.calendar_id, block.work_block_id)
            checkpoint(
                "work block carries the derived stable event ID",
                block.calendar_event_id == expected_id,
                block.work_block_id[:12],
            )
            event = await self.calendar_reader.get_event(
                self.settings.calendar_id, block.calendar_event_id
            )
            if event is None:
                checkpoint("calendar event exists on the controlled calendar", False, expected_id)
                continue
            properties = event.payload.get("extendedProperties", {}).get("private", {})
            checkpoint(
                "calendar event exists with CommitmentOS ownership properties",
                properties.get("managed_by") == "commitmentos"
                and properties.get("work_block_id") == block.work_block_id,
                event.event_id[:16],
            )

    async def verify_action_results(self, blocks: list, timeout: float) -> None:
        """action_result observation IDs are deterministic (producer = outbox,
        version = terminal status), so verification needs no Firestore index:
        get each expected document and wait for terminal reconciliation."""
        from commitmentos.contracts.observations import (
            ObservationIdFactory,
            ReconciliationStatus,
        )

        succeeded_outbox_ids = [
            outbox_id
            for outbox_id, row in await self._outbox_rows()
            if row["execution_status"] == "succeeded"
        ]
        id_factory = ObservationIdFactory()
        expected_ids = [
            id_factory.create(ObservationType.ACTION_RESULT, outbox_id, "succeeded")
            for outbox_id in succeeded_outbox_ids
        ]

        async def _load(repositories):
            statuses = []
            for observation_id in expected_ids:
                observation = await repositories.observations.get(observation_id)
                statuses.append(
                    observation.reconciliation_status if observation is not None else None
                )
            return statuses

        deadline = time.monotonic() + timeout
        statuses: list = []
        while time.monotonic() < deadline:
            statuses = await self.uow.read(_load)
            if all(status == ReconciliationStatus.PROCESSED for status in statuses):
                checkpoint(
                    "action_result observations reconciled to processed",
                    True,
                    f"{len(statuses)} observations",
                )
                return
            await asyncio.sleep(3)
        checkpoint(
            "action_result observations reconciled to processed",
            False,
            f"statuses: {[getattr(s, 'value', None) for s in statuses]}",
        )

    # ------------------------------------------------------------------

    async def change_automatic_actions(self, target_mode: str) -> None:
        from commitmentos.application.dto import ControlChangeRequest

        async def _epoch(repositories):
            controls = await repositories.system_controls.get(self.settings.controlled_user_id)
            return controls.control_epoch

        epoch = await self.uow.read(_epoch)
        result = await self.controlled.change_system_control.execute(
            self._actor(),
            ControlChangeRequest(
                control_name="automatic_actions",
                target_mode=target_mode,
                reason=f"seeded-slice pause proof ({self.run_tag})",
                expected_control_epoch=epoch,
            ),
            trace_id=f"trace-seeded-slice-{self.run_tag}",
        )
        checkpoint(
            f"automatic actions -> {target_mode}",
            result.status == CommandStatus.COMPLETED,
            f"epoch {result.revision}",
        )

    async def assert_held_without_mutation(self, timeout: float) -> None:
        """While paused: every outbox action must reach held_by_control and
        no slice Calendar event may exist."""
        deadline = time.monotonic() + min(timeout, 90)
        held = False
        while time.monotonic() < deadline:
            blocks = (await self._slice_state())["blocks"]
            if blocks:
                statuses = await self._statuses_by_block()
                if all(
                    statuses.get(block.work_block_id) == ["held_by_control"] for block in blocks
                ):
                    held = True
                    break
            await asyncio.sleep(3)
        checkpoint("paused: outbox actions held_by_control", held, "")
        blocks = (await self._slice_state())["blocks"]
        mutated = []
        for block in blocks:
            event = await self.calendar_reader.get_event(
                self.settings.calendar_id, block.calendar_event_id
            )
            if event is not None:
                mutated.append(block.calendar_event_id)
        checkpoint(
            "paused: zero Calendar mutations",
            not mutated,
            f"{len(mutated)} unexpected events" if mutated else "",
        )

    async def cleanup(self) -> None:
        """Cancel slice-created Calendar events through the conditional-write
        path (If-Match), then remove the slice's Firestore records. Activity
        events and reconciliation runs are retained as audit history."""
        if self.commitment_id is None:
            print("cleanup: no commitment recorded for this run tag; nothing to do")
            return
        state = await self._slice_state()
        for block in state["blocks"]:
            event = await self.calendar_reader.get_event(
                self.settings.calendar_id, block.calendar_event_id
            )
            if event is None:
                continue
            outcome = await self.calendar_writer.cancel_owned(
                CalendarMutation(
                    action_type=CalendarActionType.CANCEL,
                    calendar_id=self.settings.calendar_id,
                    calendar_event_id=block.calendar_event_id,
                    work_block_id=block.work_block_id,
                    desired_start=None,
                    desired_end=None,
                    expected_observed_event_etag=event.etag,
                    private_properties={},
                )
            )
            print(f"cleanup: cancel {block.calendar_event_id[:16]} -> {outcome.outcome_type.value}")

        commitment_id = self.commitment_id

        async def _purge(repositories):
            from commitmentos.infrastructure.firestore.repositories.implementations import (
                ACTION_OUTBOX,
                APPROVALS,
                COMMITMENTS,
                WORK_BLOCKS,
            )

            context = repositories._context
            blocks = list(await repositories.work_blocks.list_for_commitment(commitment_id))
            for approval in await repositories.approvals.list_pending(
                self.settings.controlled_user_id
            ):
                if approval.get("commitment_id") == commitment_id:
                    context.stage_delete(APPROVALS, approval["approval_id"])
            for block in blocks:
                context.stage_delete(WORK_BLOCKS, block.work_block_id)
            context.stage_delete(COMMITMENTS, commitment_id)
            outbox_rows = await context.query(
                ACTION_OUTBOX, [("commitment_id", "==", commitment_id)]
            )
            for outbox_id, _ in outbox_rows:
                context.stage_delete(ACTION_OUTBOX, outbox_id)
            return len(blocks) + len(outbox_rows) + 1

        purged = await self.uow.run(_purge)
        print(f"cleanup: purged {purged} slice documents (audit history retained)")


async def run(args: argparse.Namespace) -> int:
    settings = Settings.load()
    run_tag = args.run_tag or datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    print(f"seeded-slice run tag: {run_tag}")
    print(f"target service:       {settings.service_base_url}")
    driver = SeededSliceDriver(settings, run_tag)

    if args.command == "cleanup":
        # Recover commitment identity from the tag the same way seeding does.
        from commitmentos.domain.shared.types import CanonicalEncoder

        driver.commitment_id = CanonicalEncoder.hash(
            [
                "commitment:v1",
                settings.controlled_user_id,
                f"thread_seeded_slice_{run_tag}",
                f"message_seeded_slice_{run_tag}:span0",
            ]
        )
        await driver.cleanup()
        return 0

    await driver.seed(args.deadline_days, args.effort)
    effort_approval = await driver.wait_for_approval("effort_confirmation", args.timeout)
    await driver.resolve(effort_approval, confirmed_minutes=args.effort)
    plan_approval = await driver.wait_for_approval("initial_plan_approval", args.timeout)
    if args.pause_proof:
        # §17 Phase 1 pause proof, live: pause BEFORE the plan approval so the
        # outbox intent is written while automatic actions are paused, prove
        # the actions hold with zero Calendar mutations, then resume through
        # revalidation and only then expect execution.
        await driver.change_automatic_actions("paused")
        await driver.resolve(plan_approval)
        await driver.assert_held_without_mutation(args.timeout)
        await driver.change_automatic_actions("enabled")
    else:
        await driver.resolve(plan_approval)
    blocks = await driver.wait_for_execution(args.timeout)
    await driver.verify_calendar(blocks)
    await driver.verify_action_results(blocks, args.timeout)

    failures = [label for label, ok, _ in _checkpoints if not ok]
    print()
    print(f"checkpoints: {len(_checkpoints) - len(failures)}/{len(_checkpoints)} passed")
    if args.cleanup:
        await driver.cleanup()
    else:
        print(f"events retained; clean up later with: run_seeded_slice.py cleanup --run-tag {run_tag}")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "cleanup"], nargs="?", default="run")
    parser.add_argument("--deadline-days", type=int, default=4)
    parser.add_argument("--effort", type=int, default=180)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--cleanup", action="store_true", help="clean up after a passing run")
    parser.add_argument(
        "--pause-proof",
        action="store_true",
        help="prove the automatic-action pause holds queued intent before executing",
    )
    parser.add_argument("--run-tag", default=None)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
