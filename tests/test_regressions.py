"""Regression tests for bugs found during code review (privilege escalation, SQL guardrail
false positives, LIMIT handling, circuit-breaker poisoning, outbox dead-lettering)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import jwt
import pytest

from data.seed_db import RUNBOOKS_DIR, seed_database
from mock_itsm_api.server import STATE as ITSM_STATE
from mock_itsm_api.server import app as itsm_app
from mock_itsm_api.server import reset_itsm_state
from src.auth import OAuthTokenManager, operator_context, resolve_operator_role
from src.connectors.action_api import (
    ActionAPIConnector,
    ActionAPIError,
    compute_idempotency_key,
    is_upstream_health_failure,
)
from src.connectors.knowledge_base import KnowledgeBaseConnector
from src.connectors.structured_db import (
    SQLSafetyError,
    StructuredDBConnector,
    normalize_timestamp_to_utc,
    validate_and_enforce_sql,
)
from src.resilience import CircuitBreaker, CircuitState
from src.server import (
    configure_connectors,
    escalate_line_stoppage,
    query_telemetry,
    replay_outbox_actions,
)


@pytest.fixture()
def env(tmp_path: Path):
    db_path = tmp_path / "ops_telemetry.db"
    seed_database(db_path=db_path, runbooks_dir=RUNBOOKS_DIR)
    reset_itsm_state()
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        transport=httpx.ASGITransport(app=itsm_app),
        token_manager=OAuthTokenManager(initial_token="expired-bootstrap-token"),
        retry_base_delay=0.0,
        retry_jitter=0.0,
        outbox_db_path=tmp_path / "outbox.db",
    )
    configure_connectors(
        db=StructuredDBConnector(db_path=db_path),
        kb=KnowledgeBaseConnector(runbooks_dir=RUNBOOKS_DIR, db_path=db_path),
        action=action_conn,
    )
    return action_conn


# --- RBAC: untrusted tool argument must not escalate privileges -----------------------


@pytest.mark.asyncio
async def test_operator_role_argument_cannot_escalate_above_env_role(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_OPERATOR_ROLE", "field_viewer")
    res = await escalate_line_stoppage(
        incident_id="INC-2026-0891",
        plant_code="TLS-FAL1",
        line_id="FAL-STN-40",
        severity="SEV-1",
        reason="LLM-supplied privilege escalation attempt",
        idempotency_key=compute_idempotency_key("INC-2026-0891", "escalate_line_stoppage"),
        operator_role="incident_commander",
    )
    assert res["error_code"] == "RBAC_FORBIDDEN"
    assert res["operator_role"] == "field_viewer"
    assert len(ITSM_STATE.tickets) == 0


def test_operator_role_argument_can_narrow_privileges() -> None:
    with operator_context("incident_commander"):
        assert resolve_operator_role(explicit_role="field_viewer") == "field_viewer"
        assert resolve_operator_role(explicit_role="ops_engineer") == "ops_engineer"
        assert resolve_operator_role() == "incident_commander"
    with operator_context("ops_engineer"):
        assert resolve_operator_role(explicit_role="incident_commander") == "ops_engineer"


def test_jwt_signed_with_old_hardcoded_default_secret_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": "attacker",
            "role": "incident_commander",
            "iss": "ops-bridge-idp",
            "exp": int(time.time()) + 60,
        },
        "ops-bridge-dev-hs256-secret-32bytes!",
        algorithm="HS256",
    )
    assert resolve_operator_role(headers={"X-Operator-Token": forged}) == "invalid_jwt_signature"


def test_jwt_fails_closed_when_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_JWT_SECRET", raising=False)
    token = jwt.encode(
        {"sub": "x", "role": "incident_commander", "iss": "ops-bridge-idp", "exp": int(time.time()) + 60},
        "anything",
        algorithm="HS256",
    )
    assert resolve_operator_role(headers={"X-Operator-Token": token}) == "invalid_jwt_signature"


# --- SQL guardrails: no false positives on literals / functions, robust LIMIT handling ---


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT replace(summary, 'A', 'a') AS s FROM incidents",
        "SELECT incident_id FROM incidents WHERE summary LIKE '%update%'",
        "SELECT incident_id FROM incidents WHERE summary = 'drop -- not a comment; really'",
        "SELECT incident_id, created_at FROM work_orders",
    ],
)
def test_sql_guardrails_allow_safe_queries_with_keywords_in_data(sql: str) -> None:
    validate_and_enforce_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DELETE FROM incidents",
        "REPLACE INTO incidents (incident_id) VALUES ('x')",
        "SELECT 'unterminated FROM incidents",
        "WITH x AS (SELECT 1) DELETE FROM incidents",
    ],
)
def test_sql_guardrails_still_block_unsafe_queries(sql: str) -> None:
    with pytest.raises(SQLSafetyError):
        validate_and_enforce_sql(sql)


def test_sql_limit_offset_comma_form_is_capped() -> None:
    assert validate_and_enforce_sql("SELECT * FROM incidents LIMIT 5, 500").endswith(
        "LIMIT 100 OFFSET 5"
    )
    assert validate_and_enforce_sql("SELECT * FROM incidents LIMIT 5, 10").endswith("LIMIT 5, 10")
    assert validate_and_enforce_sql("SELECT * FROM incidents LIMIT 500 OFFSET 2").endswith(
        "LIMIT 100 OFFSET 2"
    )


@pytest.mark.asyncio
async def test_query_with_parameterized_limit_and_offset_comma_executes(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("X_OPERATOR_ROLE", "field_viewer")
    res = await query_telemetry("SELECT incident_id FROM incidents LIMIT ?", [2])
    assert res["status"] == "OK", res
    assert res["row_count"] == 2

    res2 = await query_telemetry("SELECT incident_id FROM incidents ORDER BY row_id LIMIT 1, 2")
    assert res2["status"] == "OK", res2
    assert res2["row_count"] == 2

    res3 = await query_telemetry("SELECT replace(summary, 'A', 'a') AS s FROM incidents LIMIT 1")
    assert res3["status"] == "OK", res3


@pytest.mark.asyncio
async def test_truncation_flag_not_set_for_small_result(env, monkeypatch) -> None:
    monkeypatch.setenv("X_OPERATOR_ROLE", "field_viewer")
    res = await query_telemetry(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 100) SELECT x FROM n LIMIT 100"
    )
    assert res["row_count"] == 100
    assert res["truncated_to_limit"] is False

    capped = await query_telemetry(
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 500) SELECT x FROM n"
    )
    assert capped["row_count"] == 100
    assert capped["truncated_to_limit"] is True


def test_malformed_timestamp_does_not_raise() -> None:
    assert normalize_timestamp_to_utc("17/11/2026 08:10", "TLS-FAL1") == (None, False)


# --- Circuit breaker: caller 4xx errors must not trip the shared breaker ---------------


def test_upstream_health_predicate() -> None:
    assert is_upstream_health_failure(ActionAPIError("bad", status_code=404)) is False
    assert is_upstream_health_failure(ActionAPIError("bad", status_code=422)) is False
    assert is_upstream_health_failure(ActionAPIError("boom", status_code=500)) is True
    assert is_upstream_health_failure(TimeoutError("slow")) is True


@pytest.mark.asyncio
async def test_client_4xx_errors_do_not_open_circuit(env) -> None:
    for i in range(5):
        with pytest.raises(ActionAPIError) as exc_info:
            await env.transition_ticket(
                ticket_id="DOES-NOT-EXIST",
                new_status="RESOLVED",
                idempotency_key=f"bogus-transition-key-{i}",
            )
        assert exc_info.value.status_code == 404
    assert env.circuit_breaker.state == CircuitState.CLOSED


# --- Outbox: replay while circuit still open defers instead of burning attempts --------


@pytest.mark.asyncio
async def test_replay_defers_when_circuit_still_open(tmp_path: Path) -> None:
    fake_now = [500.0]
    reset_itsm_state()
    cb = CircuitBreaker(name="defer_test", failure_threshold=1, recovery_timeout=15.0, time_fn=lambda: fake_now[0])
    action_conn = ActionAPIConnector(
        base_url="http://mock-itsm.test",
        transport=httpx.ASGITransport(app=itsm_app),
        circuit_breaker=cb,
        token_manager=OAuthTokenManager(initial_token="expired-bootstrap-token"),
        max_retries=0,
        outbox_db_path=tmp_path / "outbox.db",
    )
    configure_connectors(action=action_conn)
    action_conn.outbox.enqueue(
        action_type="create_work_order",
        payload={
            "incident_id": "INC-2026-0891",
            "title": "queued",
            "priority": "P2",
            "work_center": "WC-AVIONICS-FAL2",
            "description": "",
            "idempotency_key": compute_idempotency_key("INC-2026-0891", "create_work_order"),
        },
        idempotency_key=compute_idempotency_key("INC-2026-0891", "create_work_order"),
    )
    # Trip the breaker
    ITSM_STATE.chaos["fail_next_500"] = 1
    with pytest.raises(ActionAPIError):
        await action_conn.create_work_order(
            incident_id="INC-TRIP",
            title="trip",
            priority="P2",
            work_center="WC",
            idempotency_key=compute_idempotency_key("INC-TRIP", "create_work_order"),
        )
    assert cb.state == CircuitState.OPEN

    with operator_context("ops_engineer"):
        res = await replay_outbox_actions()
    assert res["status"] == "DEGRADED"
    assert res["deferred_count"] == 1
    assert res["failed_count"] == 0
    pending = action_conn.outbox.list_pending()
    assert len(pending) == 1 and pending[0]["attempts"] == 0


def test_outbox_dead_letters_permanent_failures(tmp_path: Path) -> None:
    from src.connectors.action_api import ActionOutboxStore

    store = ActionOutboxStore(db_path=tmp_path / "o.db")
    store.enqueue(action_type="create_work_order", payload={}, idempotency_key="key-perm-0001")
    assert store.mark_attempt_failed("key-perm-0001", "HTTP 422", permanent=True) == "DEAD_LETTER"
    assert store.list_pending() == []

    store.enqueue(action_type="create_work_order", payload={}, idempotency_key="key-retry-0001")
    statuses = [store.mark_attempt_failed("key-retry-0001", "HTTP 500", max_attempts=3) for _ in range(3)]
    assert statuses == ["PENDING", "PENDING", "DEAD_LETTER"]


# --- Mock ITSM: idempotency keys are scoped per endpoint -------------------------------


@pytest.mark.asyncio
async def test_mock_idempotency_scoped_per_endpoint(env) -> None:
    shared_key = "shared-key-across-endpoints"
    wo = await env.create_work_order(
        incident_id="INC-1", title="t", priority="P2", work_center="WC", idempotency_key=shared_key
    )
    esc = await env.escalate_line_stoppage(
        incident_id="INC-1", plant_code="TLS-FAL1", line_id="L1", severity="SEV-1", reason="r",
        idempotency_key=shared_key,
    )
    assert wo["ticket_type"] == "WORK_ORDER"
    assert esc["ticket_type"] == "LINE_STOPPAGE_ESCALATION"
    assert esc["idempotent_replay"] is False
