"""FastMCP / MCPServer entrypoint for ops-bridge-mcp.

Unifies three enterprise data planes behind a single RBAC-governed,
resilient Model Context Protocol interface:
  1. Structured Operational Telemetry (read-only SQLite)
  2. Unstructured Engineering Knowledge Base (hybrid FTS5 + BM25 + local vector)
  3. Mutating ITSM Action API (idempotent REST client with OAuth refresh & circuit breaker)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

# Ensure repository root is importable when launched via `mcp dev src/server.py`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - fallback for mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]

from src.auth import check_rbac_permission, resolve_operator_id
from src.connectors.action_api import (
    ActionAPIConnector,
    ActionAPIError,
    compute_idempotency_key,
    validate_idempotency_key,
)
from src.connectors.knowledge_base import KnowledgeBaseConnector
from src.connectors.structured_db import SQLSafetyError, StructuredDBConnector
from src.resilience import CircuitBreakerOpenError, UpstreamTimeoutError
from src.telemetry import get_recent_audit_events, log_audit_event

WorkOrderPriority = Literal["P1", "P2", "P3", "P4"]
IncidentSeverity = Literal["SEV-1", "SEV-2", "SEV-3"]

mcp = FastMCP(
    "ops-bridge-mcp",
    instructions=(
        "High-compliance manufacturing and aviation operations MCP bridge. "
        "Enforces RBAC roles (field_viewer, ops_engineer, incident_commander), "
        "read-only SQL guardrails, hybrid SOP runbook citations, and deterministic "
        "idempotency keys for downstream ITSM mutations."
    ),
)
server = mcp
app = mcp

db_connector = StructuredDBConnector()
kb_connector = KnowledgeBaseConnector()
action_connector = ActionAPIConnector()


def configure_connectors(
    *,
    db: StructuredDBConnector | None = None,
    kb: KnowledgeBaseConnector | None = None,
    action: ActionAPIConnector | None = None,
) -> None:
    """Override module-level connectors (useful for testing or custom runtime wiring)."""
    global db_connector, kb_connector, action_connector
    if db is not None:
        db_connector = db
    if kb is not None:
        kb_connector = kb
    if action is not None:
        action_connector = action



def _already_delivered_response(
    *,
    tool_name: str,
    result_field: str,
    outbox_entry: dict[str, Any],
    incident_id: str,
    role: str,
    operator_id: str,
    trace_id: str,
    started: float,
) -> dict[str, Any]:
    """Answer a circuit-open retry whose idempotency key the outbox already delivered.

    ITSM already holds this ticket, so return it as an idempotent replay rather than
    telling the operator it was queued.
    """
    ticket = {**outbox_entry.get("result", {}), "idempotent_replay": True}
    log_audit_event(
        tool_name=tool_name,
        operator_role=role,
        operator_id=operator_id,
        rbac_decision="ALLOW",
        latency_ms=(time.perf_counter() - started) * 1000.0,
        status="OK",
        trace_id=trace_id,
        metadata={
            "incident_id": incident_id,
            "ticket_id": ticket.get("ticket_id"),
            "idempotent_replay": True,
            "served_from": "outbox",
        },
    )
    return {"status": "OK", "trace_id": trace_id, "operator_role": role, result_field: ticket}


@mcp.tool()
async def query_telemetry(
    sql: str,
    params: list[Any] | None = None,
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Execute a parameterized read-only SQL query against operational telemetry.

    Rejects mutating SQL keywords (INSERT, UPDATE, DELETE, DROP, ALTER, ATTACH),
    enforces LIMIT <= 100, and applies a 2.0s execution timeout.
    """
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "query_telemetry", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    try:
        result = await db_connector.execute_query(sql, params)
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="query_telemetry",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="OK",
            trace_id=trace_id,
            metadata={"row_count": result.get("row_count", 0)},
        )
        return {
            "status": "OK",
            "trace_id": trace_id,
            "operator_role": role,
            **result,
        }
    except SQLSafetyError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="query_telemetry",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="SQL_GUARDRAIL_REJECTED",
            trace_id=trace_id,
            metadata={"error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "SQL_GUARDRAIL_VIOLATION",
            "trace_id": trace_id,
            "operator_role": role,
            "message": str(exc),
        }
    except (UpstreamTimeoutError, FileNotFoundError, Exception) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        degraded = isinstance(exc, UpstreamTimeoutError)
        log_audit_event(
            tool_name="query_telemetry",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="TIMEOUT" if degraded else "ERROR",
            degraded_state=degraded,
            degraded_sources=["structured_db"] if degraded else [],
            trace_id=trace_id,
            metadata={"error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "DB_TIMEOUT" if degraded else "DB_QUERY_ERROR",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["structured_db"] if degraded else [],
            "message": str(exc),
        }


@mcp.tool()
async def search_runbooks(
    query: str,
    top_k: int = 5,
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Search engineering SOPs and maintenance runbooks via hybrid FTS5 + BM25 + local vectors."""
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "search_runbooks", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    try:
        result = await kb_connector.search(query=query, top_k=top_k)
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="search_runbooks",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="OK",
            trace_id=trace_id,
            metadata={"result_count": result.get("result_count", 0)},
        )
        return {
            "status": "OK",
            "trace_id": trace_id,
            "operator_role": role,
            **result,
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="search_runbooks",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="ERROR",
            degraded_state=True,
            degraded_sources=["knowledge_base"],
            trace_id=trace_id,
            metadata={"error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "KB_SEARCH_ERROR",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["knowledge_base"],
            "message": str(exc),
        }


@mcp.tool()
async def diagnose_and_triage_incident(
    incident_id: str,
    symptom_query: str | None = None,
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Composite triage tool querying structured DB and SOP runbooks in parallel.

    Uses asyncio.gather(..., return_exceptions=True). If one data plane fails or
    times out, returns partial telemetry/guidance with an explicit degraded_sources
    list and warning banner rather than failing the entire tool call.
    """
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "diagnose_and_triage_incident", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    effective_query = (
        symptom_query.strip()
        if symptom_query and symptom_query.strip()
        else f"{incident_id} fault triage isolation escalation SOP"
    )

    db_res, kb_res = await asyncio.gather(
        db_connector.get_incident_bundle(incident_id),
        kb_connector.search(effective_query, top_k=4),
        return_exceptions=True,
    )

    degraded_sources: list[str] = []
    degradation_reasons: dict[str, str] = {}

    structured_payload: dict[str, Any] | None = None
    if isinstance(db_res, Exception):
        degraded_sources.append("structured_db")
        degradation_reasons["structured_db"] = str(db_res)
    else:
        structured_payload = db_res

    kb_payload: dict[str, Any] | None = None
    if isinstance(kb_res, Exception):
        degraded_sources.append("knowledge_base")
        degradation_reasons["knowledge_base"] = str(kb_res)
    else:
        kb_payload = kb_res

    # If no symptom_query was provided and both sources succeeded, enrich KB query
    # using the reconciled component code and summary from the DB record.
    if (
        not symptom_query
        and structured_payload
        and structured_payload.get("found")
        and kb_payload is not None
    ):
        rec = structured_payload.get("reconciled_incident") or {}
        enriched_query = " ".join(
            part
            for part in (
                rec.get("component_code"),
                rec.get("work_center"),
                rec.get("summary"),
            )
            if part
        ).strip()
        if enriched_query:
            try:
                kb_payload = await kb_connector.search(enriched_query, top_k=4)
            except Exception as exc:
                degraded_sources.append("knowledge_base")
                degradation_reasons["knowledge_base"] = str(exc)
                kb_payload = None

    degraded_state = len(degraded_sources) > 0
    if len(degraded_sources) == 2:
        overall_status = "UNAVAILABLE"
    elif degraded_state:
        overall_status = "DEGRADED"
    else:
        overall_status = "OK"

    warning_banner = (
        f"WARNING: Partial triage response. Degraded sources: {', '.join(degraded_sources)}. "
        f"Details: {json.dumps(degradation_reasons)}"
        if degraded_state
        else None
    )

    latency_ms = (time.perf_counter() - started) * 1000.0
    log_audit_event(
        tool_name="diagnose_and_triage_incident",
        operator_role=role,
        operator_id=operator_id,
        rbac_decision="ALLOW",
        latency_ms=latency_ms,
        status=overall_status,
        degraded_state=degraded_state,
        degraded_sources=degraded_sources,
        trace_id=trace_id,
        metadata={"incident_id": incident_id, "reasons": degradation_reasons},
    )

    return {
        "status": overall_status,
        "trace_id": trace_id,
        "operator_role": role,
        "incident_id": incident_id,
        "degraded_state": degraded_state,
        "degraded_sources": degraded_sources,
        "warning_banner": warning_banner,
        "degradation_reasons": degradation_reasons,
        "structured_telemetry": structured_payload,
        "runbook_guidance": kb_payload,
        "recommended_idempotency_keys": {
            "create_work_order": compute_idempotency_key(incident_id, "create_work_order"),
            "escalate_line_stoppage": compute_idempotency_key(
                incident_id, "escalate_line_stoppage"
            ),
        },
    }


@mcp.tool()
async def create_work_order(
    incident_id: str,
    title: str,
    priority: WorkOrderPriority,
    work_center: str,
    idempotency_key: str,
    description: str = "",
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Create a maintenance work order in the downstream ITSM system (ops_engineer+)."""
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "create_work_order", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    try:
        validated_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="create_work_order",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="VALIDATION_ERROR",
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "INVALID_IDEMPOTENCY_KEY",
            "trace_id": trace_id,
            "operator_role": role,
            "message": str(exc),
        }

    try:
        ticket = await action_connector.create_work_order(
            incident_id=incident_id,
            title=title,
            priority=priority,
            work_center=work_center,
            idempotency_key=validated_key,
            description=description,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="create_work_order",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="OK",
            trace_id=trace_id,
            metadata={
                "incident_id": incident_id,
                "ticket_id": ticket.get("ticket_id"),
                "idempotent_replay": ticket.get("idempotent_replay", False),
            },
        )
        return {
            "status": "OK",
            "trace_id": trace_id,
            "operator_role": role,
            "work_order": ticket,
        }
    except CircuitBreakerOpenError as exc:
        outbox_entry = action_connector.outbox.enqueue(
            action_type="create_work_order",
            payload={
                "incident_id": incident_id,
                "title": title,
                "priority": priority,
                "work_center": work_center,
                "description": description,
                "idempotency_key": validated_key,
            },
            idempotency_key=validated_key,
        )
        if outbox_entry["status"] == "REPLAYED":
            return _already_delivered_response(
                tool_name="create_work_order",
                result_field="work_order",
                outbox_entry=outbox_entry,
                incident_id=incident_id,
                role=role,
                operator_id=operator_id,
                trace_id=trace_id,
                started=started,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="create_work_order",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="CIRCUIT_OPEN_QUEUED",
            degraded_state=True,
            degraded_sources=["itsm_action_api"],
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc), "outbox_id": outbox_entry["outbox_id"]},
        )
        return {
            "status": "DEGRADED",
            "error_code": "CIRCUIT_BREAKER_OPEN",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["itsm_action_api"],
            "retry_after_seconds": exc.retry_after_seconds,
            "outbox_queued": True,
            "outbox_id": outbox_entry["outbox_id"],
            "message": str(exc),
        }
    except (ActionAPIError, UpstreamTimeoutError, Exception) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="create_work_order",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="UPSTREAM_ERROR",
            degraded_state=True,
            degraded_sources=["itsm_action_api"],
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "ITSM_ACTION_FAILED",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["itsm_action_api"],
            "message": str(exc),
        }


@mcp.tool()
async def escalate_line_stoppage(
    incident_id: str,
    plant_code: str,
    line_id: str,
    severity: IncidentSeverity,
    reason: str,
    idempotency_key: str,
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Escalate a P1 FAL line stoppage in ITSM (incident_commander ONLY)."""
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "escalate_line_stoppage", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    try:
        validated_key = validate_idempotency_key(idempotency_key)
    except ValueError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="escalate_line_stoppage",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="VALIDATION_ERROR",
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "INVALID_IDEMPOTENCY_KEY",
            "trace_id": trace_id,
            "operator_role": role,
            "message": str(exc),
        }

    try:
        escalation = await action_connector.escalate_line_stoppage(
            incident_id=incident_id,
            plant_code=plant_code,
            line_id=line_id,
            severity=severity,
            reason=reason,
            idempotency_key=validated_key,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="escalate_line_stoppage",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="OK",
            trace_id=trace_id,
            metadata={
                "incident_id": incident_id,
                "ticket_id": escalation.get("ticket_id"),
                "plant_code": plant_code,
                "line_id": line_id,
                "idempotent_replay": escalation.get("idempotent_replay", False),
            },
        )
        return {
            "status": "OK",
            "trace_id": trace_id,
            "operator_role": role,
            "escalation": escalation,
        }
    except CircuitBreakerOpenError as exc:
        outbox_entry = action_connector.outbox.enqueue(
            action_type="escalate_line_stoppage",
            payload={
                "incident_id": incident_id,
                "plant_code": plant_code,
                "line_id": line_id,
                "severity": severity,
                "reason": reason,
                "idempotency_key": validated_key,
            },
            idempotency_key=validated_key,
        )
        if outbox_entry["status"] == "REPLAYED":
            return _already_delivered_response(
                tool_name="escalate_line_stoppage",
                result_field="escalation",
                outbox_entry=outbox_entry,
                incident_id=incident_id,
                role=role,
                operator_id=operator_id,
                trace_id=trace_id,
                started=started,
            )
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="escalate_line_stoppage",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="CIRCUIT_OPEN_QUEUED",
            degraded_state=True,
            degraded_sources=["itsm_action_api"],
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc), "outbox_id": outbox_entry["outbox_id"]},
        )
        return {
            "status": "DEGRADED",
            "error_code": "CIRCUIT_BREAKER_OPEN",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["itsm_action_api"],
            "retry_after_seconds": exc.retry_after_seconds,
            "outbox_queued": True,
            "outbox_id": outbox_entry["outbox_id"],
            "message": str(exc),
        }
    except (ActionAPIError, UpstreamTimeoutError, Exception) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        log_audit_event(
            tool_name="escalate_line_stoppage",
            operator_role=role,
            operator_id=operator_id,
            rbac_decision="ALLOW",
            latency_ms=latency_ms,
            status="UPSTREAM_ERROR",
            degraded_state=True,
            degraded_sources=["itsm_action_api"],
            trace_id=trace_id,
            metadata={"incident_id": incident_id, "error": str(exc)},
        )
        return {
            "status": "ERROR",
            "error_code": "ITSM_ACTION_FAILED",
            "trace_id": trace_id,
            "operator_role": role,
            "degraded_sources": ["itsm_action_api"],
            "message": str(exc),
        }


@mcp.tool()
async def replay_outbox_actions(
    operator_role: str | None = None,
) -> dict[str, Any]:
    """Replay queued ITSM mutations from the SQLite DLQ outbox after circuit recovery."""
    trace_id = uuid.uuid4().hex[:16]
    started = time.perf_counter()
    allowed, role, rbac_error = check_rbac_permission(
        "replay_outbox_actions", explicit_role=operator_role, trace_id=trace_id
    )
    if not allowed and rbac_error is not None:
        return rbac_error

    operator_id = resolve_operator_id()
    replay_summary = await action_connector.replay_outbox()
    latency_ms = (time.perf_counter() - started) * 1000.0
    unhealthy = replay_summary["failed_count"] > 0 or replay_summary.get("deferred_count", 0) > 0
    status_str = "DEGRADED" if unhealthy else "OK"
    log_audit_event(
        tool_name="replay_outbox_actions",
        operator_role=role,
        operator_id=operator_id,
        rbac_decision="ALLOW",
        latency_ms=latency_ms,
        status=status_str,
        degraded_state=unhealthy,
        degraded_sources=["itsm_action_api"] if unhealthy else [],
        trace_id=trace_id,
        metadata={
            "replayed_count": replay_summary["replayed_count"],
            "failed_count": replay_summary["failed_count"],
            "deferred_count": replay_summary.get("deferred_count", 0),
        },
    )
    return {
        "status": status_str,
        "trace_id": trace_id,
        "operator_role": role,
        **replay_summary,
    }


@mcp.resource("ops://telemetry/schema", mime_type="application/json")
async def get_telemetry_schema() -> str:
    """Return the read-only operational telemetry SQLite schema."""
    try:
        schema = await db_connector.get_schema_summary()
        return json.dumps(schema, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, indent=2)


@mcp.resource("ops://runbooks/catalog", mime_type="application/json")
async def get_runbooks_catalog() -> str:
    """Return indexed engineering SOP runbooks and section line ranges."""
    return json.dumps({"runbooks": kb_connector.list_catalog()}, indent=2)


@mcp.resource("ops://audit/recent", mime_type="application/json")
async def get_recent_audit_trail() -> str:
    """Return the most recent structured JSON audit log events."""
    return json.dumps({"events": get_recent_audit_events(limit=50)}, indent=2)


@mcp.prompt()
def incident_triage_prompt(incident_id: str) -> str:
    """Generate a high-compliance aviation/manufacturing triage workflow prompt."""
    wo_key = compute_idempotency_key(incident_id, "create_work_order")
    esc_key = compute_idempotency_key(incident_id, "escalate_line_stoppage")
    return (
        f"You are assisting a Forward Deployed Engineer triaging operational incident {incident_id}.\n"
        f"1. Call `diagnose_and_triage_incident(incident_id='{incident_id}')` first to reconcile duplicate "
        f"SAP/ServiceNow rows, normalize plant timestamps to UTC, inspect component sensor anomalies, and "
        f"retrieve exact SOP citations.\n"
        f"2. If `degraded_sources` is non-empty, prominently report the `warning_banner` and proceed using "
        f"the available data plane.\n"
        f"3. Cite exact SOP section numbers and line ranges (e.g., `[SOP-AV-001 § ... (L...-L...)]`).\n"
        f"4. Before taking mutating action, use deterministic idempotency keys:\n"
        f"   - Work order: `{wo_key}` (requires `ops_engineer` or `incident_commander`)\n"
        f"   - Line stoppage escalation: `{esc_key}` (requires `incident_commander`)\n"
    )


if __name__ == "__main__":
    mcp.run()
