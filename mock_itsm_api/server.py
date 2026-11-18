"""FastAPI Mock ITSM / Jira Server simulating rate limits, auth expiry, and latency spikes.

Run standalone:
    python mock_itsm_api/server.py
or:
    uvicorn mock_itsm_api.server:app --host 127.0.0.1 --port 8100
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(
    title="Mock Enterprise ITSM / Jira API",
    version="1.0.0",
    description="Simulated ServiceNow/Jira action plane with chaos fault injection.",
)


class TokenRequest(BaseModel):
    grant_type: str = "client_credentials"
    client_id: str
    client_secret: str


class ChaosConfig(BaseModel):
    fail_next_429: int = 0
    fail_next_503: int = 0
    fail_next_500: int = 0
    force_401_once: bool = False
    expire_all_tokens: bool = False
    latency_ms: int = 0
    reset: bool = False


class WorkOrderCreateRequest(BaseModel):
    incident_id: str
    title: str
    priority: str = "P2"
    work_center: str
    description: str = ""
    idempotency_key: str | None = None


class EscalationCreateRequest(BaseModel):
    incident_id: str
    plant_code: str
    line_id: str
    severity: str = "SEV-1"
    reason: str
    idempotency_key: str | None = None


class TicketTransitionRequest(BaseModel):
    new_status: str = Field(..., pattern="^(OPEN|IN_PROGRESS|ESCALATED|CONTAINED|RESOLVED)$")
    comment: str = ""
    idempotency_key: str | None = None


class _ITSMState:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.valid_tokens: set[str] = set()
        self.tickets: dict[str, dict[str, Any]] = {}
        self.idempotency_store: dict[str, dict[str, Any]] = {}
        self.ticket_counter: int = 1000
        self.request_log: list[dict[str, Any]] = []
        self.chaos: dict[str, Any] = {
            "fail_next_429": 0,
            "fail_next_503": 0,
            "fail_next_500": 0,
            "force_401_once": False,
            "expire_all_tokens": False,
            "latency_ms": 0,
        }


STATE = _ITSMState()


def reset_itsm_state() -> None:
    """Reset in-memory tickets, tokens, and chaos configuration."""
    STATE.reset()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _verify_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer authorization token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if STATE.chaos["force_401_once"]:
        STATE.chaos["force_401_once"] = False
        STATE.valid_tokens.discard(token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token expired (simulated rotation).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token not in STATE.valid_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token invalid or expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def _apply_chaos_filters(request: Request) -> None:
    latency_ms = int(STATE.chaos.get("latency_ms", 0) or 0)
    header_latency = request.headers.get("X-Mock-Latency-Ms")
    if header_latency is not None:
        latency_ms = max(latency_ms, int(header_latency))
    if latency_ms > 0:
        await asyncio.sleep(latency_ms / 1000.0)

    forced_fault = request.headers.get("X-Mock-Fault")
    if forced_fault == "429" or STATE.chaos["fail_next_429"] > 0:
        if STATE.chaos["fail_next_429"] > 0:
            STATE.chaos["fail_next_429"] -= 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded on downstream ServiceNow/Jira gateway.",
            headers={"Retry-After": "1"},
        )

    if forced_fault == "503" or STATE.chaos["fail_next_503"] > 0:
        if STATE.chaos["fail_next_503"] > 0:
            STATE.chaos["fail_next_503"] -= 1
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstream ITSM queue temporarily unavailable.",
        )

    if forced_fault == "500" or STATE.chaos["fail_next_500"] > 0:
        if STATE.chaos["fail_next_500"] > 0:
            STATE.chaos["fail_next_500"] -= 1
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal ITSM database deadlock error.",
        )


def _extract_idempotency_key(header_key: str | None, body_key: str | None) -> str:
    key = (header_key or body_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header or idempotency_key body field is required.",
        )
    return key


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "mock-itsm-api",
        "ticket_count": len(STATE.tickets),
        "active_tokens": len(STATE.valid_tokens),
        "chaos": dict(STATE.chaos),
    }


@app.post("/auth/token")
async def issue_token(payload: TokenRequest) -> dict[str, Any]:
    if not payload.client_id or not payload.client_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client credentials.",
        )
    new_token = f"tok_live_{secrets.token_hex(12)}"
    STATE.valid_tokens.add(new_token)
    STATE.chaos["expire_all_tokens"] = False
    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": 3600,
        "issued_at": _utc_now(),
    }


@app.post("/admin/chaos")
async def configure_chaos(config: ChaosConfig) -> dict[str, Any]:
    if config.reset:
        reset_itsm_state()
        return {"status": "reset", "chaos": dict(STATE.chaos)}

    STATE.chaos["fail_next_429"] = config.fail_next_429
    STATE.chaos["fail_next_503"] = config.fail_next_503
    STATE.chaos["fail_next_500"] = config.fail_next_500
    STATE.chaos["force_401_once"] = config.force_401_once
    STATE.chaos["latency_ms"] = config.latency_ms
    if config.expire_all_tokens:
        STATE.valid_tokens.clear()
        STATE.chaos["expire_all_tokens"] = True
    return {"status": "configured", "chaos": dict(STATE.chaos)}


@app.post("/api/v1/work-orders")
async def create_work_order(
    payload: WorkOrderCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    _verify_bearer(authorization)
    await _apply_chaos_filters(request)
    idem_key = _extract_idempotency_key(idempotency_key_header, payload.idempotency_key)

    STATE.request_log.append(
        {"endpoint": "/api/v1/work-orders", "idempotency_key": idem_key, "at": _utc_now()}
    )

    store_key = f"work_orders:{idem_key}"
    if store_key in STATE.idempotency_store:
        cached = dict(STATE.idempotency_store[store_key])
        cached["idempotent_replay"] = True
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    STATE.ticket_counter += 1
    ticket_id = f"WO-ITSM-{STATE.ticket_counter}"
    record = {
        "ticket_id": ticket_id,
        "ticket_type": "WORK_ORDER",
        "incident_id": payload.incident_id,
        "title": payload.title,
        "priority": payload.priority,
        "work_center": payload.work_center,
        "description": payload.description,
        "status": "OPEN",
        "idempotency_key": idem_key,
        "idempotent_replay": False,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    STATE.tickets[ticket_id] = record
    STATE.idempotency_store[store_key] = dict(record)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=record)


@app.post("/api/v1/escalations")
async def create_escalation(
    payload: EscalationCreateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    _verify_bearer(authorization)
    await _apply_chaos_filters(request)
    idem_key = _extract_idempotency_key(idempotency_key_header, payload.idempotency_key)

    STATE.request_log.append(
        {"endpoint": "/api/v1/escalations", "idempotency_key": idem_key, "at": _utc_now()}
    )

    store_key = f"escalations:{idem_key}"
    if store_key in STATE.idempotency_store:
        cached = dict(STATE.idempotency_store[store_key])
        cached["idempotent_replay"] = True
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    STATE.ticket_counter += 1
    ticket_id = f"ESC-FAL-{STATE.ticket_counter}"
    record = {
        "ticket_id": ticket_id,
        "ticket_type": "LINE_STOPPAGE_ESCALATION",
        "incident_id": payload.incident_id,
        "plant_code": payload.plant_code,
        "line_id": payload.line_id,
        "severity": payload.severity,
        "reason": payload.reason,
        "status": "ESCALATED",
        "war_room_bridge": f"bridge-{ticket_id.lower()}@ops.internal",
        "idempotency_key": idem_key,
        "idempotent_replay": False,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    STATE.tickets[ticket_id] = record
    STATE.idempotency_store[store_key] = dict(record)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=record)


@app.patch("/api/v1/tickets/{ticket_id}/transition")
async def transition_ticket(
    ticket_id: str,
    payload: TicketTransitionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    _verify_bearer(authorization)
    await _apply_chaos_filters(request)
    idem_key = _extract_idempotency_key(idempotency_key_header, payload.idempotency_key)

    store_key = f"transition:{ticket_id}:{idem_key}"
    if store_key in STATE.idempotency_store:
        cached = dict(STATE.idempotency_store[store_key])
        cached["idempotent_replay"] = True
        return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    if ticket_id not in STATE.tickets:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket '{ticket_id}' not found.",
        )

    ticket = STATE.tickets[ticket_id]
    previous_status = ticket["status"]
    ticket["status"] = payload.new_status
    ticket["updated_at"] = _utc_now()
    if payload.comment:
        ticket["last_comment"] = payload.comment

    result = {
        "ticket_id": ticket_id,
        "previous_status": previous_status,
        "new_status": payload.new_status,
        "comment": payload.comment,
        "idempotency_key": idem_key,
        "idempotent_replay": False,
        "updated_at": ticket["updated_at"],
    }
    STATE.idempotency_store[store_key] = dict(result)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
