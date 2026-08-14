from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class ControlMode(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class SystemControls:
    user_id: str
    observation_mode: ControlMode
    automatic_action_mode: ControlMode
    control_epoch: int
    actor: str
    reason: str
    updated_at: datetime

    def set_observation_mode(
        self,
        mode: ControlMode,
        actor: str,
        reason: str,
        at: datetime,
    ) -> SystemControls:
        # Every control change increments the monotonic epoch, including a
        # same-mode confirmation; an action from an older epoch cannot mutate
        # Calendar without fresh reconciliation (plan §13.1.1).
        return replace(
            self,
            observation_mode=mode,
            control_epoch=self.control_epoch + 1,
            actor=actor,
            reason=reason,
            updated_at=at,
        )

    def set_automatic_action_mode(
        self,
        mode: ControlMode,
        actor: str,
        reason: str,
        at: datetime,
    ) -> SystemControls:
        return replace(
            self,
            automatic_action_mode=mode,
            control_epoch=self.control_epoch + 1,
            actor=actor,
            reason=reason,
            updated_at=at,
        )

    def allows_observation_processing(self) -> bool:
        return self.observation_mode == ControlMode.ENABLED

    def allows_automatic_actions(self) -> bool:
        return self.automatic_action_mode == ControlMode.ENABLED


def initial_system_controls(user_id: str, at: datetime) -> SystemControls:
    return SystemControls(
        user_id=user_id,
        observation_mode=ControlMode.ENABLED,
        automatic_action_mode=ControlMode.ENABLED,
        control_epoch=1,
        actor="system_bootstrap",
        reason="initial controls",
        updated_at=at,
    )
