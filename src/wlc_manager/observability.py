from __future__ import annotations

import json
import logging
import socket
import sys
import time
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from wlc_manager.config import GraylogConfig

_RESERVED_LOG_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}
_SENSITIVE_FRAGMENTS = ("password", "secret", "credential", "token")


class JsonFormatter(logging.Formatter):
    """Stable JSON logs for stdout and container log collection."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that merges stable run context with per-event fields."""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        event_fields = kwargs.get("extra", {})
        kwargs["extra"] = {**self.extra, **event_fields}
        return msg, kwargs


class GelfUdpHandler(logging.Handler):
    """Minimal compressed GELF 1.1 UDP handler for Graylog."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.hostname = socket.gethostname()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "version": "1.1",
                "host": self.hostname,
                "short_message": record.getMessage(),
                "timestamp": record.created,
                "level": _syslog_level(record.levelno),
                "_logger": record.name,
            }
            for key, value in _extra_fields(record).items():
                payload[f"_{key}"] = value
            if record.exc_info:
                payload["full_message"] = logging.Formatter().formatException(record.exc_info)
            encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._socket.sendto(zlib.compress(encoded), (self.host, self.port))
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._socket.close()
        super().close()


def configure_logging(graylog: GraylogConfig, *, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(JsonFormatter())
    root.addHandler(console)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("netmiko").setLevel(logging.WARNING)
    logging.getLogger("paramiko").setLevel(logging.WARNING)

    if graylog.enabled:
        if graylog.host is None:  # Guard for non-Pydantic callers.
            raise ValueError("Graylog host is required when Graylog is enabled")
        root.addHandler(GelfUdpHandler(graylog.host, graylog.port))


@contextmanager
def process_run(
    process_name: str,
    *,
    trigger: str,
    run_id: str | None = None,
    logger: logging.Logger | None = None,
) -> Iterator[ContextLoggerAdapter]:
    actual_run_id = run_id or str(uuid.uuid4())
    base_logger = logger or logging.getLogger("wlc_manager")
    run_logger = ContextLoggerAdapter(
        base_logger,
        {"run_id": actual_run_id, "process_name": process_name, "trigger": trigger},
    )
    started = time.monotonic()
    run_logger.info("process started", extra={"event": "process_started", "status": "running"})
    try:
        yield run_logger
    except Exception:
        run_logger.exception(
            "process failed",
            extra={
                "event": "process_finished",
                "status": "failed",
                "duration_ms": _elapsed_ms(started),
            },
        )
        raise
    else:
        run_logger.info(
            "process completed",
            extra={
                "event": "process_finished",
                "status": "succeeded",
                "duration_ms": _elapsed_ms(started),
            },
        )


@contextmanager
def process_step(logger: ContextLoggerAdapter, step_name: str) -> Iterator[None]:
    started = time.monotonic()
    logger.info(
        "process step started",
        extra={"event": "step_started", "step": step_name, "status": "running"},
    )
    try:
        yield
    except Exception:
        logger.exception(
            "process step failed",
            extra={
                "event": "step_finished",
                "step": step_name,
                "status": "failed",
                "duration_ms": _elapsed_ms(started),
            },
        )
        raise
    else:
        logger.info(
            "process step completed",
            extra={
                "event": "step_finished",
                "step": step_name,
                "status": "succeeded",
                "duration_ms": _elapsed_ms(started),
            },
        )


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_LOG_FIELDS or key.startswith("_"):
            continue
        fields[key] = "[REDACTED]" if _is_sensitive(key) else value
    return fields


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_FRAGMENTS)


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _syslog_level(level: int) -> int:
    if level >= logging.CRITICAL:
        return 2
    if level >= logging.ERROR:
        return 3
    if level >= logging.WARNING:
        return 4
    if level >= logging.INFO:
        return 6
    return 7
