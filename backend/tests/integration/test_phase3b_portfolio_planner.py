"""Phase 3B — deterministic portfolio planning, publication, and safe undo."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from conftest import CALENDAR_ID, CONTROLLED_USER, Phase1App

from commitmentos.application.commands.request_plan_undo import PlanUndoRequest
from commitmentos.application.dto import CommandStatus
from commitmentos.domain.commitments.models import (
    Commitment,
    Deadline,
    Effort,
    LifecycleStatus,
    OwnershipType,
    RiskLevel,
)
from commitmentos.domain.planning.candidate_slots import CandidateSlotGenerator
from commitmentos.domain.planning.constraints import ConstraintEvaluator
from commitmentos.domain.planning.diff import PlanDiffer
from commitmentos.domain.planning.models import (
    CalendarBusyInterval,
    CommitmentDemand,
    PlannerInput,
    PlannerRunStatus,
    PreservedWorkBlock,
    TimeInterval,
    UserPlanningPreferences,
)
from commitmentos.domain.planning.portfolio import PortfolioPlanner
from commitmentos.domain.planning.risk import RiskCalculator, RiskInput
from commitmentos.domain.planning.scoring import SlotScorer
from commitmentos.infrastructure.firestore.serializers import SerializerRegistry

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)


def _preferences(
    *,
    minimum: int = 30,
    maximum: int = 60,
    daily_limit: int = 180,
) -> UserPlanningPreferences:
    return UserPlanningPreferences(
        timezone="UTC",
        working_day_start=time(9),
        working_day_end=time(17, 30),
        minimum_block_minutes=minimum,
        maximum_block_minutes=maximum,
        daily_focus_limit_minutes=daily_limit,
        preferred_focus_windows=((time(9), time(17, 30)),),
    )


def _demand(
    commitment_id: str,
    *,
    deadline: datetime,
    remaining: int = 60,
    priority: int = 0,
    created_offset: int = 0,
) -> CommitmentDemand:
    return CommitmentDemand(
        commitment_id=commitment_id,
        commitment_revision=1,
        plan_revision=0,
        deadline=deadline,
        remaining_minutes=remaining,
        explicit_priority=priority,
        created_at=NOW + timedelta(minutes=created_offset),
        confirmed_effort_minutes=remaining,
    )


def _input(
    demands: tuple[CommitmentDemand, ...],
    *,
    end: datetime,
    preferences: UserPlanningPreferences | None = None,
) -> PlannerInput:
    return PlannerInput(
        user_id="user-portfolio",
        planning_horizon=TimeInterval(NOW, end),
        preferences=preferences or _preferences(),
        demands=demands,
        busy_intervals=(),
        preserved_blocks=(),
        expected_revisions={
            f"commitment:{item.commitment_id}": item.commitment_revision
            for item in demands
        },
        calendar_state_revision=1,
        calendar_snapshot_hash="calendar-hash",
        planner_version="portfolio-greedy-v1",
    )


def _planner() -> PortfolioPlanner:
    constraints = ConstraintEvaluator()
    return PortfolioPlanner(
        CandidateSlotGenerator(15),
        constraints,
        SlotScorer("stable-slot-score-v1"),
        RiskCalculator("portfolio-risk-v1"),
    )


def _commitment(
    commitment_id: str,
    *,
    deadline: datetime,
    effort: int,
    created_at: datetime,
) -> Commitment:
    return Commitment(
        commitment_id=commitment_id,
        user_id=CONTROLLED_USER,
        revision=1,
        source_thread_id=f"thread-{commitment_id}",
        semantic_fingerprint=f"fingerprint-{commitment_id}",
        title=f"Commitment {commitment_id}",
        description="",
        ownership_type=OwnershipType.MY_COMMITMENT,
        owner={"type": "user", "priority": "0"},
        beneficiary={"display_name": "Reviewer"},
        deadline=Deadline(
            value=deadline,
            timezone="America/Los_Angeles",
            confidence=1.0,
            evidence_id=f"evidence-{commitment_id}",
            source_expression="fixture deadline",
            rule_version="test",
        ),
        effort=Effort(effort, 1.0, effort, created_at),
        lifecycle_status=LifecycleStatus.ACTIVE,
        completion_evidence_id=None,
        completed_at=None,
        plan_revision=0,
        projection=None,
        policy_profile="default_personal",
        created_at=created_at,
        updated_at=created_at,
    )


async def _pending(app: Phase1App, request_type: str):
    async def _load(repositories):
        return [
            item
            for item in await repositories.approvals.list_pending(CONTROLLED_USER)
            if item["request_type"] == request_type
        ]

    return (await app.uow.read(_load))[0]


class TestPurePortfolioPlanner:
    def test_two_commitments_never_receive_the_same_minute_and_replay_is_identical(
        self,
    ) -> None:
        demands = (
            _demand("later", deadline=NOW + timedelta(hours=8), created_offset=1),
            _demand("earlier", deadline=NOW + timedelta(hours=6)),
        )
        planner_input = _input(demands, end=NOW + timedelta(hours=8))
        first = _planner().plan(planner_input)
        replay = _planner().plan(planner_input)

        assert first == replay
        assert first.commitment_order == ("earlier", "later")
        assert first.feasible
        assert first.work_blocks
        assert _planner().validate_single_allocation(first)
        earlier = [
            block.interval
            for block in first.work_blocks
            if block.commitment_id == "earlier"
        ]
        later = [
            block.interval
            for block in first.work_blocks
            if block.commitment_id == "later"
        ]
        assert earlier and later
        assert all(not left.overlaps(right) for left in earlier for right in later)

    def test_stable_tie_breaks_use_priority_then_creation_then_id(self) -> None:
        deadline = NOW + timedelta(hours=8)
        planner_input = _input(
            (
                _demand("z-id", deadline=deadline, priority=1, created_offset=1),
                _demand("a-id", deadline=deadline, priority=1, created_offset=1),
                _demand("older", deadline=deadline, priority=1),
                _demand("priority", deadline=deadline, priority=0, created_offset=5),
            ),
            end=deadline,
        )
        assert _planner().order_commitments(planner_input) == (
            "priority",
            "older",
            "a-id",
            "z-id",
        )

    def test_insufficient_capacity_is_single_allocated_and_critical(self) -> None:
        deadline = NOW + timedelta(hours=1)
        plan = _planner().plan(
            _input(
                (
                    _demand("first", deadline=deadline, remaining=60),
                    _demand("second", deadline=deadline, remaining=60),
                ),
                end=deadline,
            )
        )
        allocations = {item.commitment_id: item for item in plan.allocations}
        assert not plan.feasible
        assert allocations["first"].allocated_work_minutes == 60
        assert allocations["second"].allocated_work_minutes == 0
        assert allocations["second"].allocation_deficit == 60
        assert allocations["second"].shortfall_minutes == 60
        assert allocations["second"].risk_level == RiskLevel.CRITICAL
        assert sum(item.allocated_work_minutes for item in plan.allocations) == 60

    def test_daily_limit_and_minimum_length_have_zero_violations(self) -> None:
        preferences = _preferences(minimum=30, maximum=60, daily_limit=60)
        demand = _demand(
            "bounded",
            deadline=NOW + timedelta(days=2),
            remaining=120,
        )
        planner_input = _input(
            (demand,),
            end=NOW + timedelta(days=2),
            preferences=preferences,
        )
        plan = _planner().plan(planner_input)
        constraints = ConstraintEvaluator()
        accepted: list[TimeInterval] = []
        for block in plan.work_blocks:
            assert block.duration_minutes >= preferences.minimum_block_minutes
            assert not constraints.evaluate(
                block.interval,
                demand,
                preferences,
                accepted,
                NOW,
                accepted,
            )
            accepted.append(block.interval)
        assert len({block.interval.start.date() for block in plan.work_blocks}) == 2

    def test_existing_owned_block_counts_once_and_is_unavailable_to_others(self) -> None:
        interval = TimeInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        demands = (
            _demand("preserved-owner", deadline=NOW + timedelta(hours=5)),
            _demand("competitor", deadline=NOW + timedelta(hours=5)),
        )
        planner_input = replace(
            _input(demands, end=NOW + timedelta(hours=5)),
            busy_intervals=(
                CalendarBusyInterval(
                    snapshot_id="snapshot-owned",
                    calendar_event_id="event-owned",
                    interval=interval,
                    is_app_owned=True,
                    work_block_id="owned-block",
                    source_revision=1,
                ),
            ),
            preserved_blocks=(
                PreservedWorkBlock(
                    work_block_id="owned-block",
                    commitment_id="preserved-owner",
                    interval=interval,
                    duration_minutes=60,
                    plan_revision=1,
                    calendar_event_id="event-owned",
                ),
            ),
            known_work_block_ids=("owned-block",),
        )
        plan = _planner().plan(planner_input)
        allocations = {item.commitment_id: item for item in plan.allocations}
        assert allocations["preserved-owner"].preserved_reserved_minutes == 60
        assert allocations["preserved-owner"].newly_allocated_minutes == 0
        competitor = [
            block.interval
            for block in plan.work_blocks
            if block.commitment_id == "competitor"
        ]
        assert competitor
        assert all(not item.overlaps(interval) for item in competitor)


class TestRiskAndStableIdentity:
    def test_section_11_1_thresholds(self) -> None:
        calculator = RiskCalculator("portfolio-risk-v1")
        base = RiskInput(
            lifecycle_status=LifecycleStatus.ACTIVE,
            deadline_passed=False,
            effort_confirmed=True,
            remaining_minutes=120,
            allocated_work_minutes=120,
            shortfall_minutes=0,
            portfolio_slack_minutes=30,
        )
        assert calculator.slack_ratio(120, 30) == 0.25
        assert calculator.calculate(base) == RiskLevel.ON_TRACK
        assert calculator.calculate(
            replace(base, portfolio_slack_minutes=29)
        ) == RiskLevel.AT_RISK
        assert calculator.calculate(
            replace(base, shortfall_minutes=1)
        ) == RiskLevel.CRITICAL
        assert calculator.calculate(
            replace(base, deadline_passed=True)
        ) == RiskLevel.OVERDUE
        assert calculator.calculate(
            replace(base, effort_confirmed=False)
        ) == RiskLevel.UNKNOWN

    def test_plan_diff_reuses_persisted_calendar_event_id_across_revision(self) -> None:
        demand = _demand("c1", deadline=NOW + timedelta(hours=8))
        original = _planner().plan(_input((demand,), end=NOW + timedelta(hours=8)))
        first_block = replace(
            original.work_blocks[0],
            calendar_event_id="persisted-event-id",
        )
        previous = replace(original, work_blocks=(first_block,))
        moved_block = replace(
            first_block,
            interval=TimeInterval(
                first_block.interval.start + timedelta(hours=2),
                first_block.interval.end + timedelta(hours=2),
            ),
            calendar_event_id="incorrect-newly-derived-id",
        )
        desired = replace(
            original,
            planner_run_id="next-run",
            work_blocks=(moved_block,),
        )
        diff = PlanDiffer().diff(previous, desired)
        assert len(diff.mutations) == 1
        assert diff.mutations[0].calendar_event_id == "persisted-event-id"

    def test_planner_run_serializer_round_trip(self) -> None:
        plan = _planner().plan(
            _input(
                (_demand("c1", deadline=NOW + timedelta(hours=8)),),
                end=NOW + timedelta(hours=8),
            )
        )
        serializer = SerializerRegistry().portfolio_plans
        document = serializer.to_document(plan)
        assert serializer.from_document(plan.planner_run_id, document) == plan


class TestPortfolioPublicationGate:
    async def test_two_active_commitments_publish_one_constraint_safe_plan(
        self,
        app: Phase1App,
    ) -> None:
        second = _commitment(
            "commitment-second-active",
            deadline=app.clock.now() + timedelta(days=4),
            effort=60,
            created_at=app.clock.now() - timedelta(days=1),
        )

        async def _save(repositories) -> None:
            await repositories.commitments.save(second, None)

        await app.uow.run(_save)
        # A real busy event consumes the first hour after the scenario clock.
        busy_start = app.clock.now()
        app.calendar.events[(CALENDAR_ID, "unrelated-meeting")] = {
            "status": "confirmed",
            "start": busy_start,
            "end": busy_start + timedelta(hours=1),
            "etag": app.calendar.next_etag(),
            "private_properties": {},
        }
        await app.synchronize_calendar_truth()

        await app.seed_golden_observation()
        await app.run_reconciliation_tasks()
        effort = await _pending(app, "effort_confirmation")
        result = await app.resolve_approval.execute(
            app.actor(),
            effort["approval_id"],
            {"decision": "approve", "confirmed_minutes": 180},
            effort["revision"],
            "trace-phase3b-effort",
        )
        assert result.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()

        approval = await _pending(app, "initial_plan_approval")
        planner_run_id = approval["payload"]["planner_run_id"]
        stored_run = app.store["planner_runs"][planner_run_id]
        assert stored_run["status"] == PlannerRunStatus.PUBLISHED.value
        assert tuple(stored_run["commitment_order"]) == (
            second.commitment_id,
            next(
                key
                for key in app.store["commitments"]
                if key != second.commitment_id
            ),
        )
        proposed = approval["payload"]["proposed_blocks"]
        assert {item["commitment_id"] for item in proposed} == {
            second.commitment_id,
            approval["commitment_id"],
        }
        intervals = [
            TimeInterval(
                datetime.fromisoformat(item["start"]),
                datetime.fromisoformat(item["end"]),
            )
            for item in proposed
        ]
        assert all(
            not left.overlaps(right)
            for index, left in enumerate(intervals)
            for right in intervals[index + 1 :]
        )
        busy_interval = TimeInterval(busy_start, busy_start + timedelta(hours=1))
        assert all(not interval.overlaps(busy_interval) for interval in intervals)

        approved = await app.resolve_approval.execute(
            app.actor(),
            approval["approval_id"],
            {"decision": "approve"},
            approval["revision"],
            "trace-phase3b-plan",
        )
        assert approved.status == CommandStatus.COMPLETED
        await app.run_reconciliation_tasks()

        blocks = list(app.store["work_blocks"].values())
        assert len(blocks) == len(proposed)
        assert len(app.store["action_outbox"]) == len(blocks)
        assert all(row["expected_projection_hash"] for row in app.store["action_outbox"].values())
        assert all(
            row["projection"]["planner_run_id"] == planner_run_id
            for row in app.store["commitments"].values()
        )

    async def test_full_revision_set_detects_new_active_commitment(
        self,
        app: Phase1App,
    ) -> None:
        first = _commitment(
            "commitment-before-calculation",
            deadline=app.clock.now() + timedelta(days=2),
            effort=60,
            created_at=app.clock.now(),
        )

        async def _save_first(repositories) -> None:
            await repositories.commitments.save(first, None)

        await app.uow.run(_save_first)
        plan = await app.portfolio_planning.calculate(CONTROLLED_USER)
        added = _commitment(
            "commitment-after-calculation",
            deadline=app.clock.now() + timedelta(days=3),
            effort=60,
            created_at=app.clock.now(),
        )

        async def _add(repositories) -> None:
            await repositories.commitments.save(added, None)

        await app.uow.run(_add)

        async def _check(repositories) -> bool:
            return await app.portfolio_planning.expected_revisions_match(
                repositories,
                plan,
            )

        assert not await app.uow.read(_check)
        assert app.store.get("planner_runs", {}) == {}

    async def test_undo_emits_reconciliation_and_never_blindly_reverses_state(
        self,
        app: Phase1App,
    ) -> None:
        commitment = _commitment(
            "commitment-undo",
            deadline=app.clock.now() + timedelta(days=2),
            effort=60,
            created_at=app.clock.now(),
        )

        async def _save(repositories) -> None:
            await repositories.commitments.save(commitment, None)

        await app.uow.run(_save)
        plan = await app.portfolio_planning.calculate(CONTROLLED_USER)

        async def _store_plan(repositories) -> None:
            await repositories.planner_runs.create(
                replace(
                    plan,
                    status=PlannerRunStatus.PUBLISHED,
                    published_at=app.clock.now(),
                )
            )

        await app.uow.run(_store_plan)
        state_before = {
            "commitments": dict(app.store["commitments"]),
            "work_blocks": dict(app.store.get("work_blocks", {})),
            "outbox": dict(app.store.get("action_outbox", {})),
        }
        result = await app.request_plan_undo.execute(
            app.actor(),
            PlanUndoRequest(plan.planner_run_id, "undo-once"),
            "trace-undo",
        )
        assert result.status == CommandStatus.COMPLETED
        assert app.store["commitments"] == state_before["commitments"]
        assert app.store.get("work_blocks", {}) == state_before["work_blocks"]
        assert app.store.get("action_outbox", {}) == state_before["outbox"]

        await app.run_reconciliation_tasks()
        assert app.store.get("work_blocks", {}) == state_before["work_blocks"]
        assert app.store.get("action_outbox", {}) == state_before["outbox"]
        undo_events = [
            row
            for row in app.store["activity_events"].values()
            if row["event_type"] == "plan_undo_requested"
        ]
        assert len(undo_events) == 2
        assert any(
            row["payload"].get("mode") == "replan_from_current_facts"
            and row["payload"].get("calendar_mutations_written") == 0
            for row in undo_events
        )
