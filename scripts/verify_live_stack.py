"""Live end-to-end MCP stdio + Mock ITSM HTTP chaos verification script.

Exercises:
  1. Real MCP stdio client connection to src/server.py
  2. Incident triage & historian rollup query (INC-2026-0891)
  3. RBAC enforcement (field_viewer deny vs. incident_commander allow) & idempotent replay
  4. Live HTTP /admin/chaos drills against mock_itsm_api (429 backoff, 401 refresh, 500x3 CB open + DLQ outbox, 15s half-open replay)
  5. Audit trail inspection via ops://audit/recent
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def idem_key(incident_id: str, action_type: str) -> str:
    return hashlib.sha256(f"{incident_id}:{action_type}".encode("utf-8")).hexdigest()


def parse_tool_json(call_result: Any) -> dict[str, Any]:
    return json.loads(call_result.content[0].text)


async def wait_for_itsm_health(base_url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(base_url=base_url, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get("/health")
                if r.status_code == 200:
                    return r.json()
            except Exception:
                await asyncio.sleep(0.2)
    raise RuntimeError("mock_itsm_api failed to become healthy within timeout")


async def run_verification() -> None:
    itsm_url = "http://127.0.0.1:8100"
    outbox_db = Path(tempfile.gettempdir()) / f"ops_bridge_verify_outbox_{os.getpid()}.db"
    if outbox_db.exists():
        outbox_db.unlink()

    # Start live mock_itsm_api subprocess
    itsm_proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "mock_itsm_api" / "server.py")],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        health = await wait_for_itsm_health(itsm_url)
        print(f"[ITSM] Live health check OK: {health}")

        server_env = dict(os.environ)
        server_env["ITSM_API_URL"] = itsm_url
        # The server's trusted role is the ceiling; per-call operator_role may only narrow it.
        server_env["X_OPERATOR_ROLE"] = "incident_commander"
        # Ensure clean outbox path isn't colliding; default data/action_outbox.db is fine after cleanup
        default_outbox = PROJECT_ROOT / "data" / "action_outbox.db"
        if default_outbox.exists():
            default_outbox.unlink()

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "src" / "server.py")],
            cwd=str(PROJECT_ROOT),
            env=server_env,
        )

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                print(f"[MCP] Connected over stdio; registered tools: {[t.name for t in tools_resp.tools]}")

                # --- STEP 1: End-to-End Triage & RBAC Walkthrough ---
                triage = parse_tool_json(
                    await session.call_tool(
                        "diagnose_and_triage_incident",
                        {"incident_id": "INC-2026-0891"},
                    )
                )
                assert triage["status"] == "OK"
                assert triage["structured_telemetry"]["duplicate_records_detected"] is True
                assert triage["structured_telemetry"]["reconciled_incident"]["tail_number"] == "F-WWBA"
                assert "SOP-AV-001" in triage["runbook_guidance"]["results"][0]["citation"]
                print(
                    "[STEP 1.1] Triage INC-2026-0891 OK -> "
                    f"citation={triage['runbook_guidance']['results'][0]['citation']}"
                )

                historian = parse_tool_json(
                    await session.call_tool(
                        "query_telemetry",
                        {"sql": "SELECT * FROM v_anomalous_historian_telemetry"},
                    )
                )
                assert historian["status"] == "OK"
                assert historian["row_count"] >= 3
                print(f"[STEP 1.2] Historian view query OK -> rows={historian['row_count']}")

                denied = parse_tool_json(
                    await session.call_tool(
                        "escalate_line_stoppage",
                        {
                            "incident_id": "INC-2026-0891",
                            "plant_code": "TLS-FAL1",
                            "line_id": "FAL-STN-40",
                            "severity": "SEV-1",
                            "reason": "Unauthorized escalation probe",
                            "idempotency_key": idem_key("INC-2026-0891", "escalate_line_stoppage"),
                            "operator_role": "field_viewer",
                        },
                    )
                )
                assert denied["error_code"] == "RBAC_FORBIDDEN"
                print("[STEP 1.3] RBAC denial verified for field_viewer -> RBAC_FORBIDDEN")

                denied_ops = parse_tool_json(
                    await session.call_tool(
                        "escalate_line_stoppage",
                        {
                            "incident_id": "INC-2026-0891",
                            "plant_code": "TLS-FAL1",
                            "line_id": "FAL-STN-40",
                            "severity": "SEV-1",
                            "reason": "ops_engineer cannot halt the line",
                            "idempotency_key": idem_key("INC-2026-0891", "escalate_line_stoppage"),
                            "operator_role": "ops_engineer",
                        },
                    )
                )
                assert denied_ops["error_code"] == "RBAC_FORBIDDEN"
                print("[STEP 1.3b] RBAC denial verified for ops_engineer -> RBAC_FORBIDDEN")

                esc_key = idem_key("INC-2026-0891", "escalate_line_stoppage")
                esc1 = parse_tool_json(
                    await session.call_tool(
                        "escalate_line_stoppage",
                        {
                            "incident_id": "INC-2026-0891",
                            "plant_code": "TLS-FAL1",
                            "line_id": "FAL-STN-40",
                            "severity": "SEV-1",
                            "reason": "80VU blower thermal trip & ARINC 429 CRC faults",
                            "idempotency_key": esc_key,
                            "operator_role": "incident_commander",
                        },
                    )
                )
                esc2 = parse_tool_json(
                    await session.call_tool(
                        "escalate_line_stoppage",
                        {
                            "incident_id": "INC-2026-0891",
                            "plant_code": "TLS-FAL1",
                            "line_id": "FAL-STN-40",
                            "severity": "SEV-1",
                            "reason": "Retry duplicate call",
                            "idempotency_key": esc_key,
                            "operator_role": "incident_commander",
                        },
                    )
                )
                assert esc1["status"] == "OK" and esc1["escalation"]["idempotent_replay"] is False
                assert esc2["status"] == "OK" and esc2["escalation"]["idempotent_replay"] is True
                assert esc1["escalation"]["ticket_id"] == esc2["escalation"]["ticket_id"]
                print(
                    "[STEP 1.4] Authorized escalation + idempotent replay OK -> "
                    f"ticket={esc1['escalation']['ticket_id']}"
                )

                # --- STEP 2: Live HTTP Chaos Drills ---
                async with httpx.AsyncClient(base_url=itsm_url, timeout=2.0) as http:
                    # 2a. 429 rate-limit burst recovery
                    await http.post("/admin/chaos", json={"fail_next_429": 2})
                    wo_429 = parse_tool_json(
                        await session.call_tool(
                            "create_work_order",
                            {
                                "incident_id": "INC-2026-0904",
                                "title": "Bleed Green/Yellow PTU",
                                "priority": "P2",
                                "work_center": "WC-HYDRAULICS-01",
                                "idempotency_key": idem_key("INC-2026-0904", "create_work_order"),
                                "operator_role": "ops_engineer",
                            },
                        )
                    )
                    assert wo_429["status"] == "OK"
                    print(f"[STEP 2.1] HTTP 429 backoff drill OK -> {wo_429['work_order']['ticket_id']}")

                    # 2b. 401 forced token expiration & transparent refresh
                    await http.post("/admin/chaos", json={"force_401_once": True})
                    wo_401 = parse_tool_json(
                        await session.call_tool(
                            "create_work_order",
                            {
                                "incident_id": "INC-2026-0912",
                                "title": "PAUT C-scan autoclave spar batch 449",
                                "priority": "P1",
                                "work_center": "WC-NDT-COMPOSITES",
                                "idempotency_key": idem_key("INC-2026-0912", "create_work_order"),
                                "operator_role": "ops_engineer",
                            },
                        )
                    )
                    assert wo_401["status"] == "OK"
                    print(f"[STEP 2.2] HTTP 401 token auto-refresh drill OK -> {wo_401['work_order']['ticket_id']}")

                    # 2c. 3x HTTP 500 -> trip circuit breaker -> 4th call queues into SQLite DLQ outbox
                    await http.post("/admin/chaos", json={"fail_next_500": 3})
                    for i in range(3):
                        fail_res = parse_tool_json(
                            await session.call_tool(
                                "create_work_order",
                                {
                                    "incident_id": f"INC-CB-{i}",
                                    "title": "CB failure probe",
                                    "priority": "P2",
                                    "work_center": "WC-AVIONICS-FAL2",
                                    "idempotency_key": idem_key(f"INC-CB-{i}", "create_work_order"),
                                    "operator_role": "ops_engineer",
                                },
                            )
                        )
                        assert fail_res["error_code"] == "ITSM_ACTION_FAILED"

                    queued_res = parse_tool_json(
                        await session.call_tool(
                            "create_work_order",
                            {
                                "incident_id": "INC-DLQ-999",
                                "title": "Queued during circuit-open outage",
                                "priority": "P1",
                                "work_center": "WC-AVIONICS-FAL2",
                                "idempotency_key": idem_key("INC-DLQ-999", "create_work_order"),
                                "operator_role": "ops_engineer",
                            },
                        )
                    )
                    assert queued_res["status"] == "DEGRADED"
                    assert queued_res["error_code"] == "CIRCUIT_BREAKER_OPEN"
                    assert queued_res["outbox_queued"] is True
                    print(
                        "[STEP 2.3] 3x HTTP 500 tripped CB OPEN -> 4th request queued in DLQ outbox "
                        f"(outbox_id={queued_res['outbox_id']})"
                    )

                    # Reset chaos and wait for 15s circuit breaker recovery window -> replay outbox
                    await http.post("/admin/chaos", json={"reset": True})
                    print("[STEP 2.4] Waiting 15.2s for CircuitBreaker HALF_OPEN recovery window...")
                    await asyncio.sleep(15.2)

                    replay_res = parse_tool_json(
                        await session.call_tool(
                            "replay_outbox_actions",
                            {"operator_role": "ops_engineer"},
                        )
                    )
                    assert replay_res["status"] == "OK"
                    assert replay_res["replayed_count"] == 1
                    print(
                        "[STEP 2.5] Outbox replay after HALF_OPEN recovery OK -> "
                        f"{replay_res['replayed'][0]['ticket_id']}"
                    )

                # --- Audit Resource Inspection ---
                audit_resource = await session.read_resource("ops://audit/recent")
                audit_json = json.loads(audit_resource.contents[0].text)
                assert len(audit_json["events"]) >= 8
                print(
                    f"[STEP 1.5] ops://audit/recent read OK -> {len(audit_json['events'])} audit events"
                )
    finally:
        itsm_proc.terminate()
        itsm_proc.wait(timeout=5)
        if outbox_db.exists():
            outbox_db.unlink()
        default_outbox = PROJECT_ROOT / "data" / "action_outbox.db"
        if default_outbox.exists():
            default_outbox.unlink()


if __name__ == "__main__":
    asyncio.run(run_verification())
