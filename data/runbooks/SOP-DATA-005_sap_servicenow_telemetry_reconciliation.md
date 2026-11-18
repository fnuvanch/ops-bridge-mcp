# SOP-DATA-005: SAP PM / ServiceNow Telemetry Skew & Incident Deduplication Runbook

**Document ID:** SOP-DATA-005
**Applicability:** Enterprise Ops Data Plane (`ops-bridge-mcp`, SAP PM ECC, ServiceNow FSM)
**Compliance Authority:** FDE Operational Data Governance Standard v2.4
**Revision:** 2.4 (Effective 2026-11-01)

## 1. Known Data Hygiene Failure Modes
Field telemetry ingested across multi-site plants (Toulouse `TLS-FAL1`, Mobile `MOB-FAL2`, Wichita `ICT-COMP3`) routinely exhibits three synchronization anomalies in `ops_telemetry.db`:
1. **Duplicate `incident_id` Rows:** Edge gateways retry batch writes when SAP RFC and ServiceNow REST connectors overlap, producing two or more rows for the same `incident_id` (e.g., `INC-2026-0891`). Often one row carries `tail_number` while the second carries `root_cause_code` or updated `status`.
2. **Missing / NULL Operational Metadata:** Offline ruggedized tablets occasionally flush incident stubs with empty strings `""` or `NULL` in `shift_lead`, `tail_number`, or `root_cause_code`.
3. **UTC vs. Local Timestamp Skew:** Legacy SAP plant historians emit naive local timestamps (`2026-11-17 09:15:00`) or offset strings (`2026-11-17 08:22:10-06:00`, `2026-11-17T15:22:10+01:00`) alongside strict UTC ISO-8601 (`2026-11-17T14:22:10Z`).

## 2. Deterministic Reconciliation Rules
When triaging an incident via `diagnose_and_triage_incident` or raw `query_telemetry`:
- **Coalesce Non-Empty Fields:** Merge duplicate `incident_id` records by selecting the latest non-null/non-empty value per column and surfacing a `duplicate_records_detected: true` flag plus the raw row count so engineers see the reconciliation transparently.
- **Normalize Timestamps to UTC:** Parse timezone-aware timestamps to UTC (`Z`). If a timestamp lacks an offset (naive string), apply the plant's IANA timezone mapping (`TLS-*` -> `Europe/Paris`, `MOB-*`/`ICT-*` -> `America/Chicago`) and annotate `timestamp_skew_corrected: true`.

## 3. Downstream ITSM Idempotency & Degraded Mode SOP
- Never retry `POST /api/v1/work-orders` or `POST /api/v1/escalations` without a deterministic `idempotency_key` (`sha256(incident_id + ":" + action_type)`).
- When `mock_itsm_api` experiences HTTP `429` or `503` bursts, `ops-bridge-mcp` executes exponential backoff with jitter. After 3 consecutive failures, the circuit breaker trips `OPEN` for 15 seconds to protect the downstream ITSM queue.
- During a structured DB timeout (> 2.0s) or circuit-breaker trip, engineers should proceed using the non-degraded source(s) returned in `diagnose_and_triage_incident` and log the `degraded_sources` list in the shift handoff report.
