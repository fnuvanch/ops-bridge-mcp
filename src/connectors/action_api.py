"""Idempotent, fault-tolerant REST client for mutating ITSM actions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from src.auth import OAuthTokenManager
from src.resilience import (
    REST_API_TIMEOUT_SECONDS,
    CircuitBreaker,
    CircuitBreakerOpenError,
    TransientUpstreamError,
    execute_with_timeout,
    retry_with_backoff,
)

DEFAULT_OUTBOX_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "action_outbox.db"


class ActionAPIError(RuntimeError):
    """Raised when a mutating ITSM API request fails non-retryably or exhausts retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ClientRejection:
    """Carries a 4xx ActionAPIError through the circuit breaker as a non-failure."""

    __slots__ = ("error",)

    def __init__(self, error: ActionAPIError) -> None:
        self.error = error


class ActionOutboxStore:
    """Persistent SQLite Dead-Letter Outbox for mutating ITSM calls when circuit is OPEN."""

    def __init__(self, db_path: Path | str = DEFAULT_OUTBOX_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_actions (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    action_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def enqueue(
        self,
        *,
        action_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist a blocked mutating request into the outbox idempotently."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT outbox_id, status FROM outbox_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                return {
                    "outbox_id": int(existing["outbox_id"]),
                    "idempotency_key": idempotency_key,
                    "status": str(existing["status"]),
                    "already_queued": True,
                }
            cur = conn.execute(
                """
                INSERT INTO outbox_actions (
                    idempotency_key, action_type, payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'PENDING', ?, ?)
                """,
                (idempotency_key, action_type, json.dumps(payload), now, now),
            )
            conn.commit()
            return {
                "outbox_id": int(cur.lastrowid),
                "idempotency_key": idempotency_key,
                "status": "PENDING",
                "already_queued": False,
            }

    def list_pending(self) -> list[dict[str, Any]]:
        """Return all pending outbox items in FIFO order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, idempotency_key, action_type, payload_json, status, attempts, created_at
                FROM outbox_actions
                WHERE status = 'PENDING'
                ORDER BY outbox_id ASC
                """
            ).fetchall()
        return [
            {
                "outbox_id": int(r["outbox_id"]),
                "idempotency_key": r["idempotency_key"],
                "action_type": r["action_type"],
                "payload": json.loads(r["payload_json"]),
                "status": r["status"],
                "attempts": int(r["attempts"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent outbox entries of any status, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, idempotency_key, action_type, payload_json, status,
                       attempts, last_error, created_at, updated_at
                FROM outbox_actions
                ORDER BY outbox_id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            out.append(
                {
                    "outbox_id": int(r["outbox_id"]),
                    "idempotency_key": r["idempotency_key"],
                    "action_type": r["action_type"],
                    "incident_id": payload.get("incident_id"),
                    "status": r["status"],
                    "attempts": int(r["attempts"]),
                    "last_error": r["last_error"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )
        return out

    def status_counts(self) -> dict[str, int]:
        """Return the number of outbox entries per status."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM outbox_actions GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    def mark_replayed(self, idempotency_key: str, result: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox_actions
                SET status = 'REPLAYED',
                    attempts = attempts + 1,
                    last_error = NULL,
                    result_json = ?,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (json.dumps(result), now, idempotency_key),
            )
            conn.commit()

    def mark_attempt_failed(
        self,
        idempotency_key: str,
        error: str,
        *,
        permanent: bool = False,
        max_attempts: int = 5,
    ) -> str:
        """Record a failed replay; dead-letter it if permanent or attempts are exhausted.

        Returns the resulting status ('PENDING' or 'DEAD_LETTER').
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE outbox_actions
                SET attempts = attempts + 1,
                    last_error = ?,
                    updated_at = ?,
                    status = CASE
                        WHEN ? OR attempts + 1 >= ? THEN 'DEAD_LETTER'
                        ELSE status
                    END
                WHERE idempotency_key = ?
                """,
                (error, now, int(permanent), max_attempts, idempotency_key),
            )
            conn.commit()
            row = conn.execute(
                "SELECT status FROM outbox_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return str(row["status"]) if row else "UNKNOWN"


def is_upstream_health_failure(exc: BaseException) -> bool:
    """Return True if an exception indicates ITSM upstream ill-health.

    HTTP 4xx rejections (bad ticket id, schema validation) are caller errors and must
    not trip the shared circuit breaker for every other operator.
    """
    if isinstance(exc, ActionAPIError) and exc.status_code is not None:
        return not (400 <= exc.status_code < 500)
    return True


def compute_idempotency_key(incident_id: str, action_type: str) -> str:
    """Compute deterministic SHA-256 idempotency key from incident_id and action_type."""
    inc = (incident_id or "").strip()
    act = (action_type or "").strip()
    if not inc or not act:
        raise ValueError("Both 'incident_id' and 'action_type' are required to compute idempotency_key.")
    digest = hashlib.sha256(f"{inc}:{act}".encode("utf-8")).hexdigest()
    return digest


def validate_idempotency_key(idempotency_key: str) -> str:
    """Validate that an explicit non-empty deterministic idempotency_key was supplied."""
    if not isinstance(idempotency_key, str) or len(idempotency_key.strip()) < 8:
        raise ValueError(
            "A deterministic 'idempotency_key' (min 8 chars, e.g. sha256(incident_id + ':' + action_type)) "
            "is mandatory for mutating ITSM operations."
        )
    return idempotency_key.strip()


class ActionAPIConnector:
    """Resilient ITSM REST client with OAuth refresh, retry backoff, and circuit breaker."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = REST_API_TIMEOUT_SECONDS,
        circuit_breaker: CircuitBreaker | None = None,
        token_manager: OAuthTokenManager | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 0.05,
        retry_jitter: float = 0.03,
        outbox: ActionOutboxStore | None = None,
        outbox_db_path: Path | str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("ITSM_API_URL", "http://127.0.0.1:8100")).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            name="itsm_api", failure_threshold=3, recovery_timeout=15.0
        )
        self.token_manager = token_manager or OAuthTokenManager(token_url="/auth/token")
        self._transport = transport
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_jitter = retry_jitter
        self.outbox = outbox or ActionOutboxStore(
            db_path=outbox_db_path if outbox_db_path is not None else DEFAULT_OUTBOX_DB_PATH
        )

    def _make_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": httpx.Timeout(self.timeout_seconds),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _send_resilient(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        validate_idempotency_key(idempotency_key)

        async def _guarded_call() -> dict[str, Any]:
            @retry_with_backoff(
                max_retries=self.max_retries,
                base_delay=self.retry_base_delay,
                jitter=self.retry_jitter,
            )
            async def _attempt_once() -> dict[str, Any]:
                async with self._make_client() as client:
                    response = await execute_with_timeout(
                        self.token_manager.request_with_token_refresh(
                            client,
                            method,
                            path,
                            json=json_body,
                            headers={"Idempotency-Key": idempotency_key},
                        ),
                        timeout_seconds=self.timeout_seconds,
                        source_name="itsm_action_api",
                    )

                if response.status_code in (429, 503):
                    raise TransientUpstreamError(
                        f"ITSM API transient error HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )
                if response.status_code >= 500:
                    raise ActionAPIError(
                        f"ITSM API server error HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )
                if response.status_code >= 400:
                    raise ActionAPIError(
                        f"ITSM API rejected request HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )
                return dict(response.json())

            try:
                return await _attempt_once()
            except ActionAPIError as exc:
                if is_upstream_health_failure(exc):
                    raise
                # Caller error (4xx): upstream is healthy, so hand the error back to
                # the caller without counting it against the shared circuit breaker.
                return _ClientRejection(exc)

        outcome = await self.circuit_breaker.call(_guarded_call)
        if isinstance(outcome, _ClientRejection):
            raise outcome.error
        return outcome

    async def create_work_order(
        self,
        *,
        incident_id: str,
        title: str,
        priority: str,
        work_center: str,
        idempotency_key: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create (or idempotently replay) a maintenance work order in ITSM."""
        payload = {
            "incident_id": incident_id,
            "title": title,
            "priority": priority,
            "work_center": work_center,
            "description": description,
            "idempotency_key": idempotency_key,
        }
        return await self._send_resilient(
            "POST",
            "/api/v1/work-orders",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    async def escalate_line_stoppage(
        self,
        *,
        incident_id: str,
        plant_code: str,
        line_id: str,
        severity: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Trigger (or idempotently replay) a P1 FAL line stoppage escalation."""
        payload = {
            "incident_id": incident_id,
            "plant_code": plant_code,
            "line_id": line_id,
            "severity": severity,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        return await self._send_resilient(
            "POST",
            "/api/v1/escalations",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    async def transition_ticket(
        self,
        *,
        ticket_id: str,
        new_status: str,
        idempotency_key: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """Transition an existing work order or escalation ticket status."""
        payload = {
            "new_status": new_status,
            "comment": comment,
            "idempotency_key": idempotency_key,
        }
        return await self._send_resilient(
            "PATCH",
            f"/api/v1/tickets/{ticket_id}/transition",
            json_body=payload,
            idempotency_key=idempotency_key,
        )

    async def replay_outbox(self) -> dict[str, Any]:
        """Replay pending DLQ outbox mutations once the circuit breaker allows probes."""
        pending = self.outbox.list_pending()
        replayed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        deferred: list[int] = []

        for pos, item in enumerate(pending):
            action_type = item["action_type"]
            payload = dict(item["payload"])
            idem_key = item["idempotency_key"]
            try:
                if action_type == "create_work_order":
                    res = await self.create_work_order(**payload)
                elif action_type == "escalate_line_stoppage":
                    res = await self.escalate_line_stoppage(**payload)
                elif action_type == "transition_ticket":
                    res = await self.transition_ticket(**payload)
                else:
                    raise ValueError(f"Unsupported outbox action_type '{action_type}'.")
                self.outbox.mark_replayed(idem_key, res)
                replayed.append(
                    {
                        "outbox_id": item["outbox_id"],
                        "action_type": action_type,
                        "idempotency_key": idem_key,
                        "ticket_id": res.get("ticket_id"),
                    }
                )
            except CircuitBreakerOpenError:
                # Upstream still unhealthy: stop and keep the rest queued without
                # burning their retry budget.
                deferred = [p["outbox_id"] for p in pending[pos:]]
                break
            except Exception as exc:
                # A 4xx rejection or unknown action will never succeed on retry
                permanent = not is_upstream_health_failure(exc) or isinstance(exc, ValueError)
                new_status = self.outbox.mark_attempt_failed(
                    idem_key, str(exc), permanent=permanent
                )
                failed.append(
                    {
                        "outbox_id": item["outbox_id"],
                        "action_type": action_type,
                        "idempotency_key": idem_key,
                        "error": str(exc),
                        "status": new_status,
                    }
                )

        return {
            "pending_found": len(pending),
            "replayed_count": len(replayed),
            "failed_count": len(failed),
            "deferred_count": len(deferred),
            "replayed": replayed,
            "failed": failed,
            "deferred_outbox_ids": deferred,
        }
