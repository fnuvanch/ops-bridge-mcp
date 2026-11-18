"""Fault-tolerance, OAuth refresh, idempotency, and graceful degradation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from data.seed_db import RUNBOOKS_DIR, seed_database
from mock_itsm_api.server import STATE as ITSM_STATE
from mock_itsm_api.server import app as itsm_app
from mock_itsm_api.server import reset_itsm_state
from src.auth import OAuthTokenManager, operator_context
from src.connectors.action_api import (
    ActionAPIConnector,
    ActionAPIError,
    compute_idempotency_key,
)
from src.connectors.knowledge_base import KnowledgeBaseConnector
from src.connectors.structured_db import StructuredDBConnector
from src.resilience import CircuitBreaker, CircuitBreakerOpenError, CircuitState, UpstreamTimeoutError
from src.server import (
    configure_connectors,
    create_work_order,
    diagnose_and_triage_incident,
    escalate_line_stoppage,
    mcp,
    replay_outbox_actions,
)
from src.telemetry import clear_audit_events


@pytest.fixture()
def resilient_env(tmp_path: Path):
    """Seed isolated telemetry DB and connect ActionAPIConnector to mock_itsm_api ASGI app."""
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    reset_itsm_state()
    clear_audit_events()

    db_conn = StructuredDBConnector(db_path=db_path, timeout_seconds=2.0)
    kb_conn = KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path, timeout_seconds=2.0)
    token_mgr = OAuthTokenManager(initial_token="expired-bootstrap-token")
    cb = CircuitBreaker(name="itsm_api_test", failure_threshold=3, recovery_timeout=15.0)
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        timeout_seconds=3.0,
        circuit_breaker=cb,
        token_manager=token_mgr,
        transport=httpx.ASGITransport(app=itsm_app),
        max_retries=3,
        retry_base_delay=0.01,
        retry_jitter=0.005,
        outbox_db_path=tmp_path / "action_outbox.db",
    )
    configure_connectors(db=db_conn, kb=kb_conn, action=action_conn)
    return db_conn, kb_conn, action_conn


@pytest.mark.asyncio
async def test_oauth_token_auto_refresh_on_401(resilient_env) -> None:
    _, _, action_conn = resilient_env
    assert action_conn.token_manager.access_token == "expired-bootstrap-token"
    assert action_conn.token_manager.refresh_count == 0

    with operator_context("ops_engineer"):
        res = await create_work_order(
            incident_id="INC-2026-0891",
            title="Replace 80VU HEPA mesh & run Megger test",
            priority="P2",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=compute_idempotency_key("INC-2026-0891", "create_work_order"),
        )
        assert res["status"] == "OK"
        assert action_conn.token_manager.refresh_count == 1
        assert action_conn.token_manager.access_token.startswith("tok_live_")

        # Simulate mid-flight token expiration via chaos toggle
        ITSM_STATE.chaos["force_401_once"] = True
        res2 = await create_work_order(
            incident_id="INC-2026-0912",
            title="PAUT C-scan autoclave batch 449",
            priority="P1",
            work_center="WC-NDT-COMPOSITES",
            idempotency_key=compute_idempotency_key("INC-2026-0912", "create_work_order"),
        )
        assert res2["status"] == "OK"
        assert action_conn.token_manager.refresh_count == 2


@pytest.mark.asyncio
async def test_exponential_backoff_recovers_from_429_and_503(resilient_env) -> None:
    # Seed initial valid token first so we isolate 429/503 retries
    _, _, action_conn = resilient_env
    ITSM_STATE.chaos["fail_next_429"] = 2

    with operator_context("ops_engineer"):
        res = await create_work_order(
            incident_id="INC-2026-0904",
            title="Bleed Green/Yellow PTU hydraulic circuit",
            priority="P2",
            work_center="WC-HYDRAULICS-01",
            idempotency_key=compute_idempotency_key("INC-2026-0904", "create_work_order"),
        )
        assert res["status"] == "OK"
        assert ITSM_STATE.chaos["fail_next_429"] == 0

    ITSM_STATE.chaos["fail_next_503"] = 2
    with operator_context("incident_commander"):
        esc_res = await escalate_line_stoppage(
            incident_id="INC-2026-0912",
            plant_code="ICT-COMP3",
            line_id="AUTOCLAVE-BAY-3",
            severity="SEV-1",
            reason="188.4C cure dwell excursion > 10 minutes",
            idempotency_key=compute_idempotency_key("INC-2026-0912", "escalate_line_stoppage"),
        )
        assert esc_res["status"] == "OK"
        assert ITSM_STATE.chaos["fail_next_503"] == 0


@pytest.mark.asyncio
async def test_deterministic_idempotency_prevents_duplicate_tickets(resilient_env) -> None:
    key1 = compute_idempotency_key("INC-2026-0891", "create_work_order")
    key2 = compute_idempotency_key("INC-2026-0891", "create_work_order")
    assert key1 == key2

    with operator_context("incident_commander"):
        first = await create_work_order(
            incident_id="INC-2026-0891",
            title="80VU blower isolation",
            priority="P1",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=key1,
        )
        second = await create_work_order(
            incident_id="INC-2026-0891",
            title="80VU blower isolation (LLM retry)",
            priority="P1",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=key2,
        )
        assert first["status"] == "OK"
        assert second["status"] == "OK"
        assert first["work_order"]["idempotent_replay"] is False
        assert second["work_order"]["idempotent_replay"] is True
        assert first["work_order"]["ticket_id"] == second["work_order"]["ticket_id"]
        assert len(ITSM_STATE.tickets) == 1

        # Missing/short idempotency key must fail cleanly
        invalid = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="Missing key check",
            idempotency_key="",
        )
        assert invalid["status"] == "ERROR"
        assert invalid["error_code"] == "INVALID_IDEMPOTENCY_KEY"


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_3_failures_and_half_opens_after_15s(tmp_path: Path) -> None:
    fake_now = [100.0]

    def fake_clock() -> float:
        return fake_now[0]

    reset_itsm_state()
    clear_audit_events()

    cb = CircuitBreaker(
        name="itsm_cb_clock_test",
        failure_threshold=3,
        recovery_timeout=15.0,
        time_fn=fake_clock,
    )
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        timeout_seconds=1.0,
        circuit_breaker=cb,
        token_manager=OAuthTokenManager(initial_token="expired-bootstrap-token"),
        transport=httpx.ASGITransport(app=itsm_app),
        max_retries=1,
        retry_base_delay=0.01,
        retry_jitter=0.0,
        outbox_db_path=tmp_path / "action_outbox.db",
    )
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    configure_connectors(
        db=StructuredDBConnector(db_path=db_path),
        kb=KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path),
        action=action_conn,
    )

    # Inject 3 consecutive HTTP 500 failures
    ITSM_STATE.chaos["fail_next_500"] = 3
    with operator_context("ops_engineer"):
        for idx in range(3):
            res = await create_work_order(
                incident_id=f"INC-FAIL-{idx}",
                title="Simulated 500 failure",
                priority="P2",
                work_center="WC-AVIONICS-FAL2",
                idempotency_key=compute_idempotency_key(f"INC-FAIL-{idx}", "create_work_order"),
            )
            assert res["status"] == "ERROR"
            assert res["error_code"] == "ITSM_ACTION_FAILED"

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 3

        # Next call must fail fast without reaching ITSM API
        requests_before = len(ITSM_STATE.request_log)
        fast_fail = await create_work_order(
            incident_id="INC-FAST-FAIL",
            title="Should fail fast",
            priority="P2",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=compute_idempotency_key("INC-FAST-FAIL", "create_work_order"),
        )
        assert fast_fail["status"] == "DEGRADED"
        assert fast_fail["error_code"] == "CIRCUIT_BREAKER_OPEN"
        assert len(ITSM_STATE.request_log) == requests_before

        # Advance virtual clock by 15s -> HALF_OPEN -> probe succeeds -> CLOSED
        fake_now[0] += 15.1
        assert cb.state == CircuitState.HALF_OPEN

        recovered = await create_work_order(
            incident_id="INC-RECOVER-1",
            title="Recovery probe",
            priority="P2",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=compute_idempotency_key("INC-RECOVER-1", "create_work_order"),
        )
        assert recovered["status"] == "OK"
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_latency_timeout(resilient_env) -> None:
    _, _, action_conn = resilient_env
    # Replace connector timeout with 0.05s while mock_itsm_api sleeps 150ms
    action_conn.timeout_seconds = 0.05
    action_conn.max_retries = 0
    ITSM_STATE.chaos["latency_ms"] = 150

    # Prime a valid token before latency injection so timeout occurs on work-order POST
    async with action_conn._make_client() as client:
        await action_conn.token_manager.refresh_token(client)

    with operator_context("ops_engineer"):
        for idx in range(3):
            res = await create_work_order(
                incident_id=f"INC-TIMEOUT-{idx}",
                title="Timeout spike test",
                priority="P2",
                work_center="WC-AVIONICS-FAL2",
                idempotency_key=compute_idempotency_key(f"INC-TIMEOUT-{idx}", "create_work_order"),
            )
            assert res["error_code"] == "ITSM_ACTION_FAILED"

        assert action_conn.circuit_breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_diagnose_and_triage_reconciles_hygiene_and_degrades_gracefully(
    resilient_env,
) -> None:
    db_conn, kb_conn, action_conn = resilient_env

    # 1. Happy path: duplicate INC-2026-0891 rows coalesced + skewed timestamps normalized
    with operator_context("field_viewer"):
        ok_res = await diagnose_and_triage_incident("INC-2026-0891")
        assert ok_res["status"] == "OK"
        assert ok_res["degraded_state"] is False
        assert ok_res["degraded_sources"] == []
        telemetry = ok_res["structured_telemetry"]
        assert telemetry["duplicate_records_detected"] is True
        assert telemetry["raw_record_count"] == 2
        reconciled = telemetry["reconciled_incident"]
        assert reconciled["tail_number"] == "F-WWBA"
        assert reconciled["shift_lead"] == "M. Laurent"
        assert reconciled["root_cause_code"] == "RC-AV-FOD-PLENUM"
        assert reconciled["updated_at_utc"] == "2026-11-17T14:35:00Z"
        assert len(telemetry["component_telemetry"]) == 3
        assert ok_res["runbook_guidance"]["result_count"] >= 1
        assert "SOP-AV-001" in ok_res["runbook_guidance"]["results"][0]["citation"]

        # 2. Degraded path A: structured_db times out -> KB guidance still returned
        class SlowFailingDB(StructuredDBConnector):
            async def get_incident_bundle(self, incident_id: str):
                await asyncio.sleep(0.01)
                raise UpstreamTimeoutError("structured_db timed out after 2.0s")

        configure_connectors(
            db=SlowFailingDB(db_path=db_conn.db_path),
            kb=kb_conn,
            action=action_conn,
        )
        degraded_db_res = await diagnose_and_triage_incident(
            "INC-2026-0891", symptom_query="AV-BLOWER-400HZ ARINC 429 current draw"
        )
        assert degraded_db_res["status"] == "DEGRADED"
        assert degraded_db_res["degraded_state"] is True
        assert degraded_db_res["degraded_sources"] == ["structured_db"]
        assert "structured_db" in degraded_db_res["warning_banner"]
        assert degraded_db_res["structured_telemetry"] is None
        assert degraded_db_res["runbook_guidance"]["result_count"] >= 1

        # 3. Degraded path B: knowledge_base fails -> structured_telemetry still returned
        class FailingKB(KnowledgeBaseConnector):
            async def search(self, query: str, top_k: int = 5):
                raise UpstreamTimeoutError("knowledge_base timed out after 2.0s")

        configure_connectors(
            db=db_conn,
            kb=FailingKB(runbooks_dir=RUNBOOKS_DIR, db_path=db_conn.db_path),
            action=action_conn,
        )
        degraded_kb_res = await diagnose_and_triage_incident("INC-2026-0891")
        assert degraded_kb_res["status"] == "DEGRADED"
        assert degraded_kb_res["degraded_state"] is True
        assert degraded_kb_res["degraded_sources"] == ["knowledge_base"]
        assert "knowledge_base" in degraded_kb_res["warning_banner"]
        assert degraded_kb_res["structured_telemetry"]["found"] is True
        assert degraded_kb_res["runbook_guidance"] is None


@pytest.mark.asyncio
async def test_ticket_transition_and_mcp_resources_and_prompt(resilient_env) -> None:
    _, _, action_conn = resilient_env
    with operator_context("ops_engineer"):
        wo_res = await create_work_order(
            incident_id="INC-2026-0904",
            title="Inspect hydraulic priority valve 1031GM",
            priority="P2",
            work_center="WC-HYDRAULICS-01",
            idempotency_key=compute_idempotency_key("INC-2026-0904", "create_work_order"),
        )
        ticket_id = wo_res["work_order"]["ticket_id"]

        trans_res = await action_conn.transition_ticket(
            ticket_id=ticket_id,
            new_status="IN_PROGRESS",
            idempotency_key=compute_idempotency_key(ticket_id, "transition_in_progress"),
            comment="Technician assigned at MOB-FAL2.",
        )
        assert trans_res["previous_status"] == "OPEN"
        assert trans_res["new_status"] == "IN_PROGRESS"

    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert {
        "query_telemetry",
        "search_runbooks",
        "diagnose_and_triage_incident",
        "create_work_order",
        "escalate_line_stoppage",
    }.issubset(tool_names)

    resources = await mcp.list_resources()
    resource_uris = {str(r.uri) for r in resources}
    assert {
        "ops://telemetry/schema",
        "ops://runbooks/catalog",
        "ops://audit/recent",
    }.issubset(resource_uris)

    schema_res = await mcp.read_resource("ops://telemetry/schema")
    assert "incidents" in str(schema_res)
    catalog_res = await mcp.read_resource("ops://runbooks/catalog")
    assert "SOP-AV-001" in str(catalog_res)
    audit_res = await mcp.read_resource("ops://audit/recent")
    assert "create_work_order" in str(audit_res)

    prompt_res = await mcp.get_prompt("incident_triage_prompt", {"incident_id": "INC-2026-0891"})
    assert "INC-2026-0891" in str(prompt_res)


@pytest.mark.asyncio
async def test_structured_db_real_timeout_interrupts_heavy_query(tmp_path: Path) -> None:
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    slow_db = StructuredDBConnector(db_path=db_path, timeout_seconds=0.05)

    # Recursive CTE generating a massive sequence that exceeds 50ms and gets interrupted
    heavy_sql = (
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 5000000) "
        "SELECT SUM(x) AS total FROM cnt"
    )
    with pytest.raises(UpstreamTimeoutError):
        await slow_db.execute_query(heavy_sql)


@pytest.mark.asyncio
async def test_outbox_queues_when_circuit_open_and_replays_after_recovery(tmp_path: Path) -> None:
    fake_now = [200.0]

    def fake_clock() -> float:
        return fake_now[0]

    reset_itsm_state()
    clear_audit_events()

    cb = CircuitBreaker(
        name="itsm_dlq_test",
        failure_threshold=3,
        recovery_timeout=15.0,
        time_fn=fake_clock,
    )
    outbox_path = tmp_path / "dlq_outbox.db"
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        timeout_seconds=1.0,
        circuit_breaker=cb,
        token_manager=OAuthTokenManager(initial_token="expired-bootstrap-token"),
        transport=httpx.ASGITransport(app=itsm_app),
        max_retries=0,
        outbox_db_path=outbox_path,
    )
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    configure_connectors(
        db=StructuredDBConnector(db_path=db_path),
        kb=KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path),
        action=action_conn,
    )

    ITSM_STATE.chaos["fail_next_500"] = 3
    with operator_context("incident_commander"):
        for idx in range(3):
            await create_work_order(
                incident_id=f"INC-DLQ-TRIP-{idx}",
                title="Trip circuit breaker",
                priority="P2",
                work_center="WC-AVIONICS-FAL2",
                idempotency_key=compute_idempotency_key(f"INC-DLQ-TRIP-{idx}", "create_work_order"),
            )
        assert cb.state == CircuitState.OPEN

        # Queued into SQLite DLQ outbox instead of losing mutating request
        wo_key = compute_idempotency_key("INC-2026-0891", "create_work_order")
        esc_key = compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage")
        q1 = await create_work_order(
            incident_id="INC-2026-0891",
            title="Queued WO during outage",
            priority="P1",
            work_center="WC-AVIONICS-FAL2",
            idempotency_key=wo_key,
        )
        q2 = await escalate_line_stoppage(
            incident_id="INC-2026-0891",
            plant_code="TLS-FAL1",
            line_id="FAL-STN-40",
            severity="SEV-1",
            reason="Queued escalation during outage",
            idempotency_key=esc_key,
        )
        assert q1["outbox_queued"] is True
        assert q2["outbox_queued"] is True
        assert len(action_conn.outbox.list_pending()) == 2

        # Advance past 15s recovery window -> replay_outbox_actions flushes both cleanly
        fake_now[0] += 15.5
        replay = await replay_outbox_actions()
        assert replay["status"] == "OK"
        assert replay["pending_found"] == 2
        assert replay["replayed_count"] == 2
        assert replay["failed_count"] == 0
        assert len(action_conn.outbox.list_pending()) == 0
        assert len(ITSM_STATE.tickets) == 2
