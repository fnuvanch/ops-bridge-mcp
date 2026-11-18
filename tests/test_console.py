"""Ops Console API tests: the console must go through the same RBAC, guardrails,
idempotency, circuit breaker, and outbox as the MCP tools."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from console.app import create_app
from data.seed_db import RUNBOOKS_DIR, seed_database
from mock_itsm_api.server import reset_itsm_state
from src import server as tools
from src.connectors.knowledge_base import KnowledgeBaseConnector
from src.connectors.structured_db import StructuredDBConnector
from src.telemetry import clear_audit_events


@pytest.fixture()
async def console(tmp_path: Path):
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    reset_itsm_state()
    clear_audit_events()
    app = create_app(outbox_db_path=tmp_path / "outbox.db")
    async with app.router.lifespan_context(app):
        # Point read connectors at the isolated DB (lifespan only wires the action plane)
        tools.configure_connectors(
            db=StructuredDBConnector(db_path=db_path),
            kb=KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path),
        )
        tools.action_connector.retry_base_delay = 0.0
        tools.action_connector.retry_jitter = 0.0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://console.test"
        ) as client:
            yield client


def as_role(role: str) -> dict[str, str]:
    return {"X-Console-Role": role}


async def test_index_and_static_assets_served(console: httpx.AsyncClient) -> None:
    index = await console.get("/")
    assert index.status_code == 200
    assert "Ops Bridge" in index.text
    for asset in ("/static/console.css", "/static/console.js"):
        assert (await console.get(asset)).status_code == 200


async def test_incident_board_lists_and_orders_by_severity(console: httpx.AsyncClient) -> None:
    res = (await console.get("/api/incidents", headers=as_role("field_viewer"))).json()
    assert res["status"] == "OK"
    ids = [r["incident_id"] for r in res["rows"]]
    assert "INC-2026-0891" in ids
    merged = {r["incident_id"]: r["raw_rows"] for r in res["rows"]}
    assert merged["INC-2026-0891"] == 2
    assert res["rows"][-1]["status"] == "RESOLVED"


async def test_triage_returns_reconciled_bundle(console: httpx.AsyncClient) -> None:
    res = (await console.get("/api/incidents/INC-2026-0891/triage")).json()
    assert res["status"] == "OK"
    assert res["structured_telemetry"]["reconciled_incident"]["tail_number"] == "F-WWBA"
    assert len(res["structured_telemetry"]["component_telemetry"]) == 3


async def test_unknown_role_header_rejected(console: httpx.AsyncClient) -> None:
    r = await console.get("/api/incidents", headers=as_role("root"))
    assert r.status_code == 400


async def test_work_order_rbac_and_idempotent_replay(console: httpx.AsyncClient) -> None:
    body = {"title": "Isolate blower", "priority": "P1", "work_center": "WC-AVIONICS-FAL2"}
    denied = (await console.post("/api/incidents/INC-2026-0891/work-order", json=body, headers=as_role("field_viewer"))).json()
    assert denied["error_code"] == "RBAC_FORBIDDEN"

    first = (await console.post("/api/incidents/INC-2026-0891/work-order", json=body, headers=as_role("ops_engineer"))).json()
    second = (await console.post("/api/incidents/INC-2026-0891/work-order", json=body, headers=as_role("ops_engineer"))).json()
    assert first["status"] == "OK" and first["work_order"]["idempotent_replay"] is False
    assert second["work_order"]["idempotent_replay"] is True
    assert first["work_order"]["ticket_id"] == second["work_order"]["ticket_id"]


async def test_escalation_requires_incident_commander(console: httpx.AsyncClient) -> None:
    body = {"plant_code": "TLS-FAL1", "line_id": "FAL-STN-40", "severity": "SEV-1", "reason": "Blower trip"}
    denied = (await console.post("/api/incidents/INC-2026-0891/escalate", json=body, headers=as_role("ops_engineer"))).json()
    assert denied["error_code"] == "RBAC_FORBIDDEN"
    ok = (await console.post("/api/incidents/INC-2026-0891/escalate", json=body, headers=as_role("incident_commander"))).json()
    assert ok["status"] == "OK"
    assert ok["escalation"]["ticket_id"].startswith("ESC-FAL-")


async def test_sql_guardrail_applies_through_console(console: httpx.AsyncClient) -> None:
    res = (await console.post("/api/query", json={"sql": "DELETE FROM incidents"})).json()
    assert res["error_code"] == "SQL_GUARDRAIL_VIOLATION"


async def test_runbook_lines_only_for_indexed_docs(console: httpx.AsyncClient) -> None:
    ok = (await console.get("/api/runbooks/SOP-AV-001/lines", params={"start": 8, "end": 10})).json()
    assert ok["lines"][0] == {"n": 8, "text": "## 1. Purpose and Scope"}
    missing = await console.get("/api/runbooks/..%2F..%2Fsrc%2Fauth/lines", params={"start": 1, "end": 5})
    assert missing.status_code == 404


async def test_chaos_trips_circuit_queues_outbox_and_system_reports_it(console: httpx.AsyncClient) -> None:
    await console.post("/api/chaos", json={"fail_next_500": 12})
    for idx in range(3):
        res = (await console.post(
            f"/api/incidents/INC-TRIP-{idx}/work-order",
            json={"title": "trip", "work_center": "WC"},
            headers=as_role("ops_engineer"),
        )).json()
        assert res["error_code"] == "ITSM_ACTION_FAILED"

    queued = (await console.post(
        "/api/incidents/INC-2026-0891/work-order",
        json={"title": "queued", "work_center": "WC"},
        headers=as_role("ops_engineer"),
    )).json()
    assert queued["error_code"] == "CIRCUIT_BREAKER_OPEN"
    assert queued["outbox_queued"] is True

    system = (await console.get("/api/system")).json()
    assert system["circuit_breaker"]["state"] == "OPEN"
    assert system["circuit_breaker"]["retry_after_seconds"] > 0
    assert system["outbox"]["counts"] == {"PENDING": 1}
    assert system["outbox"]["recent"][0]["incident_id"] == "INC-2026-0891"
    assert system["itsm"]["chaos"]["fail_next_500"] > 0

    audit = (await console.get("/api/audit")).json()["events"]
    assert audit[0]["status"] == "CIRCUIT_OPEN_QUEUED"
    assert audit[0]["metadata"]["incident_id"] == "INC-2026-0891"


async def test_chaos_input_is_validated(console: httpx.AsyncClient) -> None:
    r = await console.post("/api/chaos", json={"latency_ms": 999999})
    assert r.status_code == 422
