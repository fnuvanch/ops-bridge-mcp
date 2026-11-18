# SOP-COMP-004: CFRP Wing Spar Autoclave Cure Cycle Thermal Excursion & NDT Quarantine Protocol

**Document ID:** SOP-COMP-004
**Applicability:** Composite Structures Facility - Autoclave Zone 3 (`AUTOCLAVE-Z3`)
**Compliance Authority:** NADCAP AC7118 / Boeing-Airbus Material Spec BMS-8-276
**Revision:** 5.1 (Effective 2026-07-30)

## 1. Scope & Material Sensitivity
Carbon-Fiber Reinforced Polymer (CFRP) primary wing spar layups undergo a controlled 180.0 C (+/- 3.0 C) cure dwell under 6.5 bar nitrogen autoclave pressure. Uncontrolled exothermic runaway or PID steam/electric heater overshoot can degrade resin glass-transition temperature ($T_g$) or cause interlaminar delamination.

## 2. Critical Cure Telemetry Envelopes
Inspect `component_telemetry` for `AUTOCLAVE-Z3`:
- **Target Cure Dwell Temperature (`autoclave_zone3_temp_c`):** 177.0 C to 183.0 C.
- **Minor Excursion (Warning):** 183.1 C to 185.0 C for < 8 minutes. Requires MRB engineering evaluation.
- **Major Thermal Excursion (Quarantine Trigger):** > 185.0 C for any duration, OR > 183.0 C for >= 10 minutes.
- **Vacuum Bag Integrity (`vac_bag_leak_rate_mbar_min`):** Must stay <= 2.0 mbar/min during ramp and dwell. Exceeding 4.0 mbar/min voids structural porosity compliance.

## 3. Immediate Operator Actions on Excursion
1. **Place Part Batch on Automated Quarantine Hold:** Do NOT release the cured CFRP wing spar to CNC trimming or drilling. Tag the SAP batch ID with `STATUS = MRB_QUARANTINE`.
2. **Preserve Thermocouple & Vacuum Trace Logs:** Export the 1-Hz thermocouple channel matrix (TC-01 through TC-24) and verify whether the excursion was a single-channel sensor drift or true zone air-stream overshoot.
3. **Schedule Phased-Array Ultrasonic Testing (PAUT) NDT:** Create a mandatory work order (`create_work_order`) assigned to `WC-NDT-COMPOSITES` for 100% C-scan ultrasonic attenuation mapping and Differential Scanning Calorimetry (DSC) coupon testing on the witness tab.

## 4. Line Stoppage Escalation Rule
If two consecutive cure loads in `AUTOCLAVE-Z3` exceed 185.0 C or a primary flight-critical spar shows delamination > 6 mm diameter in PAUT C-scan, the `incident_commander` must call `escalate_line_stoppage` for facility `PLANT-COMP-US2` / line `AUTOCLAVE-BAY-3` to lock out the heater PID controller until Honeywell/Siemens loop recalibration completes.
