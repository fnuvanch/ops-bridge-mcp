# SOP-HYD-003: A320 Green/Yellow Hydraulic PTU Pressure Oscillation & Thermal Relief Diagnostics

**Document ID:** SOP-HYD-003
**Applicability:** Airbus A320 Hydraulic Systems - Power Transfer Unit (`HYD-PTU-GRN`)
**Compliance Authority:** AMM Task 29-23-00-810-001 / Part-145
**Revision:** 2.5 (Effective 2026-08-19)

## 1. System Overview & Normal Parameters
The bidirectional Power Transfer Unit (PTU) transfers hydraulic power between the Green and Yellow systems when differential pressure exceeds 500 PSI without exchanging fluid. During ground maintenance and FAL functional testing, improper reservoir pre-charge or priority valve stiction can induce rapid pressure spikes and fluid overheating.

## 2. Diagnostic Sensor Thresholds
Evaluate `component_telemetry` for component `HYD-PTU-GRN`:
- **Nominal System Pressure (`hyd_pressure_psi`):** 2,950 to 3,050 PSI (3,000 PSI +/- 50 PSI).
- **Over-Pressure Oscillation Threshold:** Peak > 3,150 PSI or cyclic hunting > +/- 180 PSI at > 2 Hz.
- **Hydraulic Fluid Return Temperature (`hyd_fluid_temp_c`):** Nominal 35 C to 75 C. Alert at >= 88.0 C; critical overheat trip at >= 95.0 C.
- **Case Drain Flow (`ptu_case_drain_lpm`):** Nominal < 1.2 L/min. Values > 2.4 L/min indicate internal barrel/piston shoe wear.

## 3. Troubleshooting & Containment Steps
1. **Verify Reservoir Air Pre-Charge:** Check Green and Yellow hydraulic reservoir air pressure (nominal 50 PSI regulated from pneumatic bleed/ground cart). Low bootstrap pressure causes PTU cavitation and hammering.
2. **Inhibit PTU & Bleed Air:** Set PTU cockpit push-button (`29-23`) to OFF. Cycle Yellow electric pump (`HYD-ELEC-YEL`) for 3 minutes and check for entrained air in the return sight glass.
3. **Inspect Priority Valve (`1031GM`) & Relief Valve (`1042GM`):** If `hyd_pressure_psi` spikes above 3,150 PSI during Yellow pump start, suspect main relief valve chatter or contaminated Skydrol LD-4 orifice screen.
4. **Fluid Contamination Sample:** Draw 100 mL sample from Green return manifold for NAS 1638 particulate count. Class 7 or worse mandates flush and filter element replacement (`FIN 1021GM`).

## 4. Escalation & Work Order Routing
- Issue a `create_work_order` to `WC-HYDRAULICS-01` for filter/fluid bleed if `hyd_pressure_psi` stabilizes between 2,950 and 3,050 PSI after bleed.
- Escalate as P1 (`escalate_line_stoppage`) if metallic debris is detected on the magnetic chip detector or `hyd_fluid_temp_c` exceeds 95.0 C, risking pump seal degradation across wing actuators.
