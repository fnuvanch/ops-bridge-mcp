"""RBAC governance and SQL read-only guardrail tests for ops-bridge-mcp."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from data.seed_db import RUNBOOKS_DIR, seed_database
from mock_itsm_api.server import STATE as ITSM_STATE
from mock_itsm_api.server import app as itsm_app
from mock_itsm_api.server import reset_itsm_state
from src.auth import issue_operator_jwt, operator_context
from src.connectors.action_api import ActionAPIConnector, compute_idempotency_key
from src.connectors.knowledge_base import KnowledgeBaseConnector
from src.connectors.structured_db import (
    SQLSafetyError,
    StructuredDBConnector,
    validate_and_enforce_sql,
)
from src.server import (
    configure_connectors,
    create_work_order,
    diagnose_and_triage_incident,
    escalate_line_stoppage,
    mcp,
    query_telemetry,
    replay_outbox_actions,
    search_runbooks,
)
from src.telemetry import clear_audit_events, get_recent_audit_events


@pytest.fixture()
def isolated_env(tmp_path: Path):
    """Seed an isolated SQLite DB and bind connectors to ASGI mock_itsm_api."""
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    reset_itsm_state()
    clear_audit_events()

    db_conn = StructuredDBConnector(db_path=db_path, timeout_seconds=2.0)
    kb_conn = KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path, timeout_seconds=2.0)
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        transport=httpx.ASGITransport(app=itsm_app),
        retry_base_delay=0.01,
        retry_jitter=0.005,
        outbox_db_path=tmp_path / "action_outbox.db",
    )
    configure_connectors(db=db_conn, kb=kb_conn, action=action_conn)
    return db_conn, kb_conn, action_conn


@pytest.mark.asyncio
async def test_field_viewer_allowed_read_tools_and_blocked_from_mutations(isolated_env) -> None:
    with operator_context("field_viewer", operator_id="analyst-01"):
        q_res = await query_telemetry("SELECT incident_id, severity FROM incidents")
        assert q_res["status"] == "OK"
        assert q_res["row_count"] > 0

        kb_res = await search_runbooks("A320 avionics bay cooling blower current draw")
        assert kb_res["status"] == "OK"
        assert kb_res["result_count"] >= 1
        assert "SOP-AV-001" in kb_res["results"][0]["citation"]

        triage_res = await diagnose_and_triage_incident("INC-2026-0891")
        assert triage_res["status"] == "OK"
        assert triage_res["degraded_state"] is False

        # Mutating tools must be strictly blocked with structured RBAC error
        wo_key = compute_idempotency_key("INC-2026-0891", "create_work_order")
        wo_res = await create_work_order(
            incident_id="INC-2026-0891",
            title="Inspect 80VU cooling blower",
            priority="P2",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=wo_key,
        )
        assert wo_res["status"] == "ERROR"
        assert wo_res["error_code"] == "RBAC_FORBIDDEN"
        assert wo_res["operator_role"] == "field_viewer"
        assert "create_work_order" in wo_res["message"]

        esc_key = compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage")
        esc_res = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="Blower trip",
            idempotency_key=esc_key,
        )
        assert esc_res["status"] == "ERROR"
        assert esc_res["error_code"] == "RBAC_FORBIDDEN"
        assert len(ITSM_STATE.tickets) == 0

    events = get_recent_audit_events(limit=10)
    deny_events = [e for e in events if e["rbac_decision"] == "DENY"]
    assert len(deny_events) == 2
    assert {e["tool_name"] for e in deny_events} == {
        "create_work_order",
        "escalate_line_stoppage",
    }
    assert all(e["status"] == "FORBIDDEN" for e in deny_events)


@pytest.mark.asyncio
async def test_ops_engineer_can_create_work_order_but_cannot_escalate(isolated_env) -> None:
    with operator_context(
        "ops_engineer",
        operator_id="eng-42",
        headers={"X-Operator-Role": "ops_engineer"},
    ):
        wo_key = compute_idempotency_key("INC-2026-0904", "create_work_order")
        wo_res = await create_work_order(
            incident_id="INC-2026-0904",
            title="Bleed Green/Yellow PTU & inspect priority valve",
            priority="P2",
            work_center="WC-HYDRAULICS-01",
            idempotency_key=wo_key,
        )
        assert wo_res["status"] == "OK"
        assert wo_res["work_order"]["ticket_id"].startswith("WO-ITSM-")
        assert len(ITSM_STATE.tickets) == 1

        esc_key = compute_idempotency_key("INC-2026-0904", "escalate_line_stoppage")
        esc_res = await escalate_line_stoppage(
            incident_id="INC-2026-0904",
            plant_code="MOB-FAL2",
            line_id="FAL-HYD-2",
            severity="SEV-1",
            reason="Attempted unauthorized stoppage",
            idempotency_key=esc_key,
        )
        assert esc_res["status"] == "ERROR"
        assert esc_res["error_code"] == "RBAC_FORBIDDEN"
        assert "incident_commander" in esc_res["required_roles"]
        assert len(ITSM_STATE.tickets) == 1


@pytest.mark.asyncio
async def test_incident_commander_can_escalate_line_stoppage(isolated_env) -> None:
    with operator_context("incident_commander", operator_id="ic-07"):
        esc_key = compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage")
        esc_res = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="ARINC 429 CRC faults > 10/min with 12HQ thermal trip",
            idempotency_key=esc_key,
        )
        assert esc_res["status"] == "OK"
        assert esc_res["escalation"]["ticket_id"].startswith("ESC-FAL-")
        assert esc_res["escalation"]["status"] == "ESCALATED"


@pytest.mark.asyncio
async def test_role_resolution_via_env_and_unknown_role_rejection(
    isolated_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_OPERATOR_ROLE", "field_viewer")
    wo_res = await create_work_order(
        incident_id="INC-2026-0891",
        title="Unauthorized via env",
        priority="P2",
        work_center="WC-AVIONICS-FAL2",
        idempotency_key=compute_idempotency_key("INC-2026-0891", "create_work_order"),
    )
    assert wo_res["error_code"] == "RBAC_FORBIDDEN"

    # Unknown role must fail closed
    unknown_res = await query_telemetry("SELECT 1", operator_role="rogue_admin")
    assert unknown_res["status"] == "ERROR"
    assert unknown_res["error_code"] == "RBAC_FORBIDDEN"


@pytest.mark.asyncio
async def test_mcp_protocol_call_tool_rbac_integration(isolated_env) -> None:
    """Verify FastMCP/MCPServer tool dispatch enforces RBAC cleanly over protocol call."""
    with operator_context("field_viewer"):
        result = await mcp.call_tool(
            "create_work_order",
            {
                "incident_id": "INC-2026-0891",
                "title": "Blocked via MCP protocol",
                "priority": "P2",
                "work_center": "WC-AVIONICS-FAL2",
                "idempotency_key": compute_idempotency_key("INC-2026-0891", "create_work_order"),
            },
        )
        text_payload = result.content[0].text
        parsed = json.loads(text_payload)
        assert parsed["error_code"] == "RBAC_FORBIDDEN"


@pytest.mark.parametrize(
    "bad_sql",
    [
        "INSERT INTO incidents (incident_id, severity, status, summary, reported_at, plant_code) VALUES ('X','SEV-1','OPEN','bad','now','TLS')",
        "UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = 'INC-2026-0891'",
        "DELETE FROM incidents",
        "DROP TABLE work_orders",
        "ALTER TABLE incidents ADD COLUMN hacked TEXT",
        "ATTACH DATABASE '/tmp/pwn.db' AS pwn",
        "SELECT * FROM incidents; DROP TABLE incidents",
        "SELECT * FROM incidents -- hidden comment",
    ],
)
@pytest.mark.asyncio
async def test_sql_guardrails_reject_mutating_and_unsafe_queries(
    isolated_env, bad_sql: str
) -> None:
    with pytest.raises(SQLSafetyError):
        validate_and_enforce_sql(bad_sql)

    with operator_context("incident_commander"):
        res = await query_telemetry(bad_sql)
        assert res["status"] == "ERROR"
        assert res["error_code"] == "SQL_GUARDRAIL_VIOLATION"


def test_sql_guardrails_enforce_limit_100() -> None:
    assert validate_and_enforce_sql("SELECT * FROM incidents").endswith("LIMIT 100")
    assert validate_and_enforce_sql("SELECT * FROM incidents LIMIT 500").endswith("LIMIT 100")
    assert validate_and_enforce_sql("SELECT * FROM incidents LIMIT 15").endswith("LIMIT 15")


@pytest.mark.asyncio
async def test_cryptographic_jwt_rbac_enforcement_and_tamper_rejection(isolated_env) -> None:
    viewer_jwt = issue_operator_jwt("field_viewer", operator_id="jwt-analyst-9")
    ic_jwt = issue_operator_jwt("incident_commander", operator_id="jwt-ic-1")
    expired_jwt = issue_operator_jwt("incident_commander", operator_id="jwt-ic-1", expires_in_seconds=-60)
    tampered_jwt = ic_jwt[:-4] + "XXXX"

    # A signed field_viewer JWT cannot be escalated by passing explicit operator_role="incident_commander"
    with operator_context("field_viewer", jwt_token=viewer_jwt):
        denied = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="Attempted privilege escalation over JWT",
            idempotency_key=compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage"),
            operator_role="incident_commander",
        )
        assert denied["error_code"] == "RBAC_FORBIDDEN"
        assert denied["operator_role"] == "field_viewer"

    # Tampered & expired JWT tokens fail closed
    for bad_token in (expired_jwt, tampered_jwt):
        with operator_context("incident_commander", headers={"X-Operator-Token": bad_token}):
            res = await query_telemetry("SELECT incident_id FROM incidents")
            assert res["error_code"] == "RBAC_FORBIDDEN"
            assert res["operator_role"] == "invalid_jwt_signature"

    # Valid signed incident_commander JWT succeeds and logs sub as operator_id
    with operator_context("field_viewer", headers={"X-Operator-Token": ic_jwt}):
        ok = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="Signed IC JWT clearance",
            idempotency_key=compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage"),
        )
        assert ok["status"] == "OK"

    events = get_recent_audit_events(limit=5)
    assert events[-1]["operator_id"] == "jwt-ic-1"
    assert events[-1]["rbac_decision"] == "ALLOW"

    # field_viewer is also blocked from replay_outbox_actions
    with operator_context("field_viewer"):
        replay_denied = await replay_outbox_actions()
        assert replay_denied["error_code"] == "RBAC_FORBIDDEN"


@pytest.mark.asyncio
async def test_historian_rollups_and_anomalous_view_query(isolated_env) -> None:
    with operator_context("field_viewer"):
        res = await query_telemetry("SELECT * FROM v_anomalous_historian_telemetry")
        assert res["status"] == "OK"
        assert res["row_count"] == 4
        assert all("parquet_partition" in row for row in res["rows"])
