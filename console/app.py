"""Ops Console: a local web UI over the ops-bridge-mcp tools.

Every action calls the same MCP tool functions an LLM client would (``src.server``),
so RBAC, SQL guardrails, idempotency, the circuit breaker, the DLQ outbox, and the
audit trail all behave exactly as they do over MCP.

The role switcher is a *demo* control: the console sets the trusted operator context
from the ``X-Console-Role`` header. Bind it to localhost only; it is not an auth layer.

Run:
    python -m console                  # embedded mock ITSM, http://127.0.0.1:8300
    python -m console --itsm-url http://127.0.0.1:8100   # use a separately running mock
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from data.seed_db import DB_PATH, seed_database
from mock_itsm_api.server import app as embedded_itsm_app
from src import server as tools
from src.auth import OperatorRole, operator_context
from src.connectors.action_api import ActionAPIConnector, compute_idempotency_key
from src.telemetry import get_recent_audit_events

STATIC_DIR = Path(__file__).resolve().parent / "static"
EMBEDDED_ITSM_BASE_URL = "http://embedded-itsm.local"
VALID_ROLES = {r.value for r in OperatorRole}

INCIDENT_BOARD_SQL = """
SELECT i.incident_id,
       MIN(i.severity) AS severity,
       MAX(i.plant_code) AS plant_code,
       MAX(i.component_code) AS component_code,
       COUNT(*) AS raw_rows,
       (SELECT s.status FROM incidents s
         WHERE s.incident_id = i.incident_id ORDER BY s.row_id DESC LIMIT 1) AS status,
       (SELECT s.summary FROM incidents s
         WHERE s.incident_id = i.incident_id ORDER BY s.row_id ASC LIMIT 1) AS summary
FROM incidents i
GROUP BY i.incident_id
ORDER BY CASE WHEN MAX(i.status) = 'RESOLVED' THEN 1 ELSE 0 END, severity, i.incident_id DESC
"""


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=4000)
    params: list[Any] | None = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(5, ge=1, le=15)


class WorkOrderRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    priority: Literal["P1", "P2", "P3", "P4"] = "P2"
    work_center: str = Field(..., min_length=1, max_length=80)
    description: str = Field("", max_length=2000)


class EscalationRequest(BaseModel):
    plant_code: str = Field(..., min_length=1, max_length=40)
    line_id: str = Field(..., min_length=1, max_length=80)
    severity: Literal["SEV-1", "SEV-2", "SEV-3"] = "SEV-1"
    reason: str = Field(..., min_length=1, max_length=1000)


class ChaosRequest(BaseModel):
    fail_next_429: int = Field(0, ge=0, le=50)
    fail_next_503: int = Field(0, ge=0, le=50)
    fail_next_500: int = Field(0, ge=0, le=50)
    force_401_once: bool = False
    latency_ms: int = Field(0, ge=0, le=10000)
    reset: bool = False


def _role(x_console_role: str | None) -> str:
    role = (x_console_role or OperatorRole.FIELD_VIEWER.value).strip().lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Unknown role '{role}'.")
    return role


def _operator(role: str) -> Any:
    return operator_context(role, operator_id=f"console:{role}")


def create_app(itsm_url: str | None = None, outbox_db_path: Path | None = None) -> FastAPI:
    """Build the console app.

    With ``itsm_url`` unset the mock ITSM API runs in-process (no second server needed);
    otherwise the console talks to the given ITSM base URL over HTTP.
    """
    embedded = not itsm_url
    base_url = EMBEDDED_ITSM_BASE_URL if embedded else str(itsm_url).rstrip("/")
    transport = httpx.ASGITransport(app=embedded_itsm_app) if embedded else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not DB_PATH.exists():
            seed_database()
        action = ActionAPIConnector(
            base_url=base_url,
            transport=transport,
            outbox_db_path=outbox_db_path,
        )
        tools.configure_connectors(action=action)
        async with httpx.AsyncClient(base_url=base_url, transport=transport, timeout=3.0) as admin:
            app.state.itsm_admin = admin
            yield

    app = FastAPI(title="Ops Bridge Console", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.itsm_mode = "embedded" if embedded else "external"
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # --- Read plane -------------------------------------------------------------------

    @app.get("/api/incidents")
    async def incidents(x_console_role: str | None = Header(default=None)) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.query_telemetry(INCIDENT_BOARD_SQL)

    @app.get("/api/incidents/{incident_id}/triage")
    async def triage(
        incident_id: str, x_console_role: str | None = Header(default=None)
    ) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.diagnose_and_triage_incident(incident_id)

    @app.post("/api/query")
    async def query(
        body: QueryRequest, x_console_role: str | None = Header(default=None)
    ) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.query_telemetry(body.sql, body.params)

    @app.post("/api/search")
    async def search(
        body: SearchRequest, x_console_role: str | None = Header(default=None)
    ) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.search_runbooks(body.query, top_k=body.top_k)

    @app.get("/api/runbooks/{doc_id}/lines")
    async def runbook_lines(doc_id: str, start: int, end: int) -> dict[str, Any]:
        """Return exact runbook lines for a citation (looked up via the indexed catalog only)."""
        catalog = {d["doc_id"]: d for d in tools.kb_connector.list_catalog()}
        doc = catalog.get(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"Runbook '{doc_id}' is not indexed.")
        lines = Path(doc["file_path"]).read_text(encoding="utf-8").splitlines()
        start = max(1, start)
        end = min(len(lines), max(start, end))
        return {
            "doc_id": doc_id,
            "doc_title": doc["doc_title"],
            "start": start,
            "end": end,
            "lines": [{"n": n, "text": lines[n - 1]} for n in range(start, end + 1)],
        }

    # --- Action plane -----------------------------------------------------------------

    @app.post("/api/incidents/{incident_id}/work-order")
    async def work_order(
        incident_id: str,
        body: WorkOrderRequest,
        x_console_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.create_work_order(
                incident_id=incident_id,
                title=body.title,
                priority=body.priority,
                work_center=body.work_center,
                description=body.description,
                idempotency_key=compute_idempotency_key(incident_id, "create_work_order"),
            )

    @app.post("/api/incidents/{incident_id}/escalate")
    async def escalate(
        incident_id: str,
        body: EscalationRequest,
        x_console_role: str | None = Header(default=None),
    ) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.escalate_line_stoppage(
                incident_id=incident_id,
                plant_code=body.plant_code,
                line_id=body.line_id,
                severity=body.severity,
                reason=body.reason,
                idempotency_key=compute_idempotency_key(incident_id, "escalate_line_stoppage"),
            )

    @app.post("/api/outbox/replay")
    async def replay(x_console_role: str | None = Header(default=None)) -> dict[str, Any]:
        role = _role(x_console_role)
        with _operator(role):
            return await tools.replay_outbox_actions()

    # --- System & fault injection -----------------------------------------------------

    @app.get("/api/system")
    async def system() -> dict[str, Any]:
        action = tools.action_connector
        itsm: dict[str, Any]
        try:
            r = await app.state.itsm_admin.get("/health")
            itsm = {"reachable": r.status_code == 200, **(r.json() if r.status_code == 200 else {})}
        except httpx.HTTPError as exc:
            itsm = {"reachable": False, "error": str(exc)}
        return {
            "itsm_mode": app.state.itsm_mode,
            "itsm": itsm,
            "circuit_breaker": action.circuit_breaker.snapshot(),
            "token_refresh_count": action.token_manager.refresh_count,
            "outbox": {
                "counts": action.outbox.status_counts(),
                "recent": action.outbox.list_recent(limit=8),
            },
        }

    @app.get("/api/audit")
    async def audit(limit: int = 60) -> dict[str, Any]:
        events = get_recent_audit_events(limit=max(1, min(limit, 200)))
        return {"events": list(reversed(events))}

    @app.post("/api/chaos")
    async def chaos(body: ChaosRequest) -> dict[str, Any]:
        """Inject faults into the ITSM mock (demo only; a real ITSM has no such endpoint)."""
        try:
            r = await app.state.itsm_admin.post("/admin/chaos", json=body.model_dump())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"ITSM mock unreachable: {exc}") from exc
        return r.json()

    return app


app = create_app(itsm_url=os.environ.get("OPS_CONSOLE_ITSM_URL") or None)
