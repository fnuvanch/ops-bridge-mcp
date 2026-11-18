"""Structured JSON audit telemetry for ops-bridge-mcp.

All structured log output is emitted exclusively to stderr and appended to a
local JSONL audit trail (data/audit.jsonl) so stdio MCP protocol frames on
stdout are never corrupted.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os

DEFAULT_AUDIT_PATH = Path(
    os.environ.get("OPS_AUDIT_PATH")
    or Path(__file__).resolve().parent.parent / "data" / "audit.jsonl"
)

_AUDIT_LOCK = threading.Lock()
_AUDIT_BUFFER: deque[dict[str, Any]] = deque(maxlen=500)


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON strings."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "audit_payload", None)
        if isinstance(payload, dict):
            return json.dumps(payload, default=str, sort_keys=False)
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            default=str,
        )


def _build_stderr_logger() -> logging.Logger:
    logger = logging.getLogger("ops_bridge.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    return logger


_AUDIT_LOGGER = _build_stderr_logger()


def log_audit_event(
    *,
    tool_name: str,
    operator_role: str,
    rbac_decision: str,
    latency_ms: float,
    status: str,
    degraded_state: bool = False,
    degraded_sources: list[str] | None = None,
    operator_id: str = "fde-operator",
    trace_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    audit_file: Path | None = None,
) -> dict[str, Any]:
    """Emit a structured JSON audit record to stderr, memory buffer, and JSONL file."""
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "trace_id": trace_id or uuid.uuid4().hex[:16],
        "operator_id": operator_id,
        "operator_role": operator_role,
        "tool_name": tool_name,
        "rbac_decision": rbac_decision,
        "status": status,
        "latency_ms": round(float(latency_ms), 3),
        "degraded_state": bool(degraded_state),
        "degraded_sources": list(degraded_sources or []),
        "metadata": metadata or {},
    }

    env_path = os.environ.get("OPS_AUDIT_PATH")
    target_file = audit_file or (Path(env_path) if env_path else DEFAULT_AUDIT_PATH)
    with _AUDIT_LOCK:
        _AUDIT_BUFFER.append(event)
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            with target_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError:
            # Never fail an operational tool call because local disk logging errored
            pass

    _AUDIT_LOGGER.info("audit_event", extra={"audit_payload": event})
    return event


def get_recent_audit_events(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent audit events (newest last)."""
    with _AUDIT_LOCK:
        if limit <= 0:
            return []
        return list(_AUDIT_BUFFER)[-limit:]


def clear_audit_events() -> None:
    """Clear the in-memory audit buffer (primarily for isolated test runs)."""
    with _AUDIT_LOCK:
        _AUDIT_BUFFER.clear()
