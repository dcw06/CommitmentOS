from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from logging import Filter, LogRecord
from typing import Any, Mapping

DEFAULT_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-goog-channel-token",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "session",
    "csrf",
    "api_key",
    "password",
    "prompt",
    "body",
    "email_body",
}

_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
_TOKEN_QUERY_PATTERN = re.compile(r"(token|key|secret|code)=([^&\s]+)", re.IGNORECASE)

_log_context: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "commitmentos_log_context", default=None
)


@dataclass(frozen=True, slots=True)
class SafeLogContext:
    trace_id: str | None = None
    user_id_hash: str | None = None
    observation_id: str | None = None
    reconciliation_run_id: str | None = None
    planner_run_id: str | None = None
    outbox_id: str | None = None
    attempt: int | None = None


class SensitiveDataRedactor(Filter):
    """Removes credentials, cookies, channel tokens, and content from log records.

    Closes Phase 0 blocker B3: the log pipeline redacts by key and by pattern
    before any handler formats the record.
    """

    def __init__(self, sensitive_keys: set[str] | None = None) -> None:
        super().__init__()
        self._sensitive_keys = {key.lower() for key in (sensitive_keys or DEFAULT_SENSITIVE_KEYS)}

    def filter(self, record: LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact_text(record.msg)
        if isinstance(record.args, Mapping):
            record.args = dict(self.redact_mapping(record.args))
        elif isinstance(record.args, tuple):
            record.args = tuple(
                self.redact_text(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        extra = getattr(record, "safe_payload", None)
        if isinstance(extra, Mapping):
            record.safe_payload = self.redact_mapping(extra)
        return True

    def redact_mapping(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in self._sensitive_keys:
                redacted[key] = "[REDACTED]"
            elif isinstance(item, Mapping):
                redacted[key] = self.redact_mapping(item)
            elif isinstance(item, str):
                redacted[key] = self.redact_text(item)
            else:
                redacted[key] = item
        return redacted

    def redact_text(self, value: str) -> str:
        value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
        return _TOKEN_QUERY_PATTERN.sub(r"\1=[REDACTED]", value)


class _StructuredFormatter(logging.Formatter):
    def __init__(self, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "event_name": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
        }
        context = _log_context.get()
        if context:
            entry.update({key: value for key, value in context.items() if value is not None})
        safe_payload = getattr(record, "safe_payload", None)
        if isinstance(safe_payload, Mapping):
            entry["payload"] = dict(safe_payload)
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


class LoggingConfigurator:
    def __init__(self, service_name: str, environment: str) -> None:
        self._service_name = service_name
        self._environment = environment
        self._redactor = SensitiveDataRedactor()

    def configure(self) -> None:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_StructuredFormatter(self._service_name, self._environment))
        handler.addFilter(self._redactor)
        root.handlers = [handler]
        for noisy in ("uvicorn.access",):
            logging.getLogger(noisy).addFilter(self._redactor)

    def bind_context(self, context: SafeLogContext) -> None:
        _log_context.set(
            {
                "trace_id": context.trace_id,
                "user_id_hash": context.user_id_hash,
                "observation_id": context.observation_id,
                "reconciliation_run_id": context.reconciliation_run_id,
                "planner_run_id": context.planner_run_id,
                "outbox_id": context.outbox_id,
                "attempt": context.attempt,
            }
        )

    def clear_context(self) -> None:
        _log_context.set(None)
