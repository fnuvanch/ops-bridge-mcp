# SOP-AV-001: A320 Avionics Bay Cooling Blower & ARINC 429 Bus Fault Triage

**Document ID:** SOP-AV-001
**Applicability:** Airbus A320 Family (MSN 4100-9800) - Avionics Compartment 80VU / 82VU
**Compliance Authority:** EASA Part-145 / FAA AC 43.13-1B
**Revision:** 4.2 (Effective 2026-09-01)

## 1. Purpose and Scope
This Standard Operating Procedure defines the fault isolation, telemetry verification, and airworthiness clearance steps for Avionics Bay Cooling Blower (`AV-BLOWER-400HZ`) over-current trips, thermal cutout events, and downstream ARINC 429 data bus CRC errors on A320 Final Assembly Line (FAL) and Line Maintenance stations.

## 2. Telemetry Thresholds & Fault Signatures
Before opening the 80VU avionics access panel, verify live sensor telemetry in the operational telemetry store:
- **Nominal 400Hz Blower Current (`blower_current_amps`):** 6.2 A to 9.8 A steady-state at 115 VAC / 400 Hz.
- **Warning Threshold:** > 10.5 A sustained for >= 15 seconds.
- **Critical Trip Threshold (`AV-BLOWER-400HZ`):** >= 11.5 A or winding temperature > 95.0 C triggers thermal cutout relay `12HQ`.
- **Avionics Rack Inlet Temp (`bay_inlet_temp_c`):** Must remain <= 38.0 C during ground power operations. Above 42.0 C, ADIRS-1/2 and ELAC-1/2 experience thermal throttling and ARINC 429 parity/CRC frame drops.
- **ARINC 429 CRC Error Rate (`arinc429_crc_err_rate`):** Nominal 0 errors/min; > 5 errors/min indicates bus impedance fault or ground-loop noise from degraded blower stator insulation.

## 3. Step-by-Step Fault Isolation Procedure
1. **Safety Lockout / Tagout (LOTO):** Confirm aircraft external GPU power is verified and circuit breakers `11HQ` (Blower Normal) and `12HQ` (Blower Override) are collared and tagged OPEN prior to physical inspection.
2. **Intake Filter & Plenum Inspection:** Inspect the avionics bay extract skin valve and HEPA intake mesh for FOD (composite dust, sealant trimmings, or safety wire debris). Blocked airflow commonly drives `AV-BLOWER-400HZ` current draw into the 11.8-13.2 A stall band.
3. **Motor Winding Megger & Phase Balance Test:** Disconnect cannon plug `82VU-P4` and measure phase-to-ground insulation resistance at 500 VDC. Minimum acceptable resistance is 20 MOhm. Check Phase A/B/C resistance balance within +/- 3%.
4. **ARINC 429 Shield Continuity Verification:** If `arinc429_crc_err_rate` exceeds 5 errors/min alongside blower fault, inspect the twisted-shielded pair ground termination at terminal block `TB-429-A`. Shield-to-airframe resistance must be < 2.5 mOhm.

## 4. Work Order & Escalation Criteria
- **P2 Work Order (`create_work_order`):** Required when current draw is 10.5-11.4 A or filter replacement resolves the temperature delta (`bay_inlet_temp_c` returns below 36.0 C). Use work center `WC-AVIONICS-FAL2`.
- **P1 Line Stoppage Escalation (`escalate_line_stoppage`):** Mandatory if winding insulation < 20 MOhm, burnt stator odor is present, or `arinc429_crc_err_rate` persists > 10 errors/min after blower isolation. Do not apply external GPU power until Incident Commander clears the hold.

## 5. Return-to-Service Sign-Off
Log the SAP PM work order reference, measured post-repair `blower_current_amps`, and 15-minute thermal soak reading in the ServiceNow maintenance log. Quality Inspector (QI) dual-signature is mandatory before closing the 80VU bay door.
