# SOP-MFG-002: Final Assembly Line (FAL) Andon Stoppage & Escalation Protocol

**Document ID:** SOP-MFG-002
**Applicability:** Toulouse FAL-1/FAL-2, Mobile FAL-US1, Hamburg FAL-DE3
**Compliance Authority:** AS9100D Clause 8.5.1 / Corporate Manufacturing Governance
**Revision:** 3.8 (Effective 2026-10-12)

## 1. Purpose and Governance Mandate
This protocol governs emergency Andon line stoppages across Final Assembly Line (FAL) stations. Uncontrolled line stoppages incur > \$45,000/hour in downstream takt-time disruption and require strict Role-Based Access Control (RBAC) authorization and deterministic audit traceability.

## 2. Severity Classification Matrix
- **SEV-3 (Advisory / Local Buffer):** Minor tooling drift or single fastener non-conformance. Handled locally by station lead; no ITSM escalation required.
- **SEV-2 (Work Order Required):** Sub-assembly test failure, missing kitted LRU, or calibration expiry on critical torque controllers. `ops_engineer` or `incident_commander` must issue a `create_work_order` action within 30 minutes.
- **SEV-1 (Critical Line Stoppage):** Safety hazard, structural non-conformance, uncontained hydraulic/thermal excursion, or aircraft airworthiness flight-control bus anomaly halting takt movement. **Only an `incident_commander` is authorized to execute `escalate_line_stoppage`.**

## 3. Mandatory Pre-Escalation Verification
Before triggering `escalate_line_stoppage`, the operator or AI copilot MUST:
1. Run `diagnose_and_triage_incident` to correlate structured SAP/ServiceNow records and live component sensor telemetry with the relevant engineering SOP.
2. Confirm whether duplicate incident records exist across plant/ERP sync streams (see `SOP-DATA-005`) and use the canonical `incident_id`.
3. Compute and pass a deterministic `idempotency_key` (SHA-256 of `incident_id + ":" + action_type`) so network retries or circuit-breaker recoveries never spawn duplicate P1 war-room bridges.

## 4. P1 Escalation SLA & Containment Timeline
- **T+0 min:** Station Andon switches to RED; `escalate_line_stoppage` creates P1 ITSM escalation (`ESC-FAL-*`) and pages Duty Incident Commander, Manufacturing Engineering, and Quality Assurance.
- **T+15 min:** Containment triage bridge convenes. If structured telemetry DB is temporarily degraded during an ERP outage, operators must record the `degraded_sources` banner in the shift turnover log and rely on local SOP runbook citations.
- **T+60 min:** Root Cause Corrective Action (RCCA) disposition must be assigned: Rework-in-Station, Material Review Board (MRB) Hold, or Station Bypass.

## 5. Resumption & Unblocking Criteria
Line movement cannot resume until the P1 escalation ticket transitions to `RESOLVED` or `CONTAINED` with verified QI sign-off and all anomalous telemetry sensors return inside nominal operating limits for 3 consecutive polling intervals.
