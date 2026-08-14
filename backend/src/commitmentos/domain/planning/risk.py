from __future__ import annotations

from dataclasses import dataclass

from commitmentos.domain.commitments.models import LifecycleStatus, RiskLevel


@dataclass(frozen=True, slots=True)
class RiskInput:
    lifecycle_status: LifecycleStatus
    deadline_passed: bool
    effort_confirmed: bool
    remaining_minutes: int
    allocated_work_minutes: int
    shortfall_minutes: int
    portfolio_slack_minutes: int


class RiskCalculator:
    def __init__(self, calculator_version: str) -> None:
        if not calculator_version:
            raise ValueError("calculator version is required")
        self._calculator_version = calculator_version

    @property
    def calculator_version(self) -> str:
        return self._calculator_version

    def calculate(self, risk_input: RiskInput) -> RiskLevel:
        if risk_input.lifecycle_status == LifecycleStatus.COMPLETED:
            return RiskLevel.UNKNOWN
        if risk_input.deadline_passed:
            return RiskLevel.OVERDUE
        if not risk_input.effort_confirmed:
            return RiskLevel.UNKNOWN
        if risk_input.shortfall_minutes > 0:
            return RiskLevel.CRITICAL
        if self.slack_ratio(
            risk_input.remaining_minutes,
            risk_input.portfolio_slack_minutes,
        ) < 0.25:
            return RiskLevel.AT_RISK
        return RiskLevel.ON_TRACK

    def slack_ratio(self, remaining_minutes: int, portfolio_slack_minutes: int) -> float:
        if remaining_minutes < 0 or portfolio_slack_minutes < 0:
            raise ValueError("risk minutes cannot be negative")
        return portfolio_slack_minutes / max(remaining_minutes, 30)
