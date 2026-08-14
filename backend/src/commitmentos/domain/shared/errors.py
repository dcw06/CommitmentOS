from __future__ import annotations


class DomainError(Exception):
    ...


class InvalidTransitionError(DomainError):
    ...


class RevisionConflictError(DomainError):
    ...


class StaleProjectionError(DomainError):
    ...


class PolicyViolationError(DomainError):
    ...


class NoFeasiblePlanError(DomainError):
    ...
