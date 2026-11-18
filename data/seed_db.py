"""Seed script for ops-bridge-mcp operational telemetry and FTS5 runbook index.

Generates realistic messy enterprise data modeling SAP PM / ServiceNow
manufacturing incidents, work orders, aircraft component sensor telemetry,
and indexes Markdown SOP runbooks into SQLite FTS5.

Usage:
    python data/seed_db.py
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlite_utils

DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "ops_telemetry.db"
RUNBOOKS_DIR = DATA_DIR / "runbooks"


@dataclass(frozen=True)
class RunbookChunk:
    """Represents a single Markdown section extracted from an SOP runbook."""

    chunk_id: str
    doc_id: str
    doc_title: str
    section_heading: str
    line_start: int
    line_end: int
    citation: str
    content: str
    file_path: str


def parse_runbook_sections(md_path: Path) -> list[RunbookChunk]:
    """Parse a Markdown runbook into section-level chunks with exact 1-based line ranges."""
    raw_lines = md_path.read_text(encoding="utf-8").splitlines()
    if not raw_lines:
        return []

    doc_title = md_path.stem
    doc_id = md_path.stem.split("_")[0]
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            doc_title = stripped[2:].strip()
            if ":" in doc_title:
                doc_id = doc_title.split(":", 1)[0].strip()
            break

    heading_indices: list[tuple[int, str]] = []
    for idx, line in enumerate(raw_lines):
        stripped = line.strip()
        if re.match(r"^#{2,3}\s+", stripped):
            heading_text = re.sub(r"^#{2,3}\s+", "", stripped).strip()
            heading_indices.append((idx, heading_text))

    chunks: list[RunbookChunk] = []
    if not heading_indices:
        content = "\n".join(raw_lines).strip()
        citation = f'[{doc_id} § "Overview" (L1-L{len(raw_lines)})]'
        chunks.append(
            RunbookChunk(
                chunk_id=f"{doc_id}::0",
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading="Overview",
                line_start=1,
                line_end=len(raw_lines),
                citation=citation,
                content=content,
                file_path=str(md_path),
            )
        )
        return chunks

    # Include document header preamble as Overview if substantial
    first_heading_idx = heading_indices[0][0]
    preamble_lines = [ln for ln in raw_lines[:first_heading_idx] if ln.strip()]
    if len(preamble_lines) > 1:
        content = "\n".join(raw_lines[:first_heading_idx]).strip()
        line_start = 1
        line_end = first_heading_idx
        chunks.append(
            RunbookChunk(
                chunk_id=f"{doc_id}::overview",
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading="Overview & Metadata",
                line_start=line_start,
                line_end=line_end,
                citation=f'[{doc_id} § "Overview & Metadata" (L{line_start}-L{line_end})]',
                content=content,
                file_path=str(md_path),
            )
        )

    for i, (start_idx, heading) in enumerate(heading_indices):
        end_idx = heading_indices[i + 1][0] - 1 if i + 1 < len(heading_indices) else len(raw_lines) - 1
        section_text = "\n".join(raw_lines[start_idx : end_idx + 1]).strip()
        line_start = start_idx + 1
        line_end = end_idx + 1
        citation = f'[{doc_id} § "{heading}" (L{line_start}-L{line_end})]'
        chunks.append(
            RunbookChunk(
                chunk_id=f"{doc_id}::{i + 1}",
                doc_id=doc_id,
                doc_title=doc_title,
                section_heading=heading,
                line_start=line_start,
                line_end=line_end,
                citation=citation,
                content=section_text,
                file_path=str(md_path),
            )
        )

    return chunks


def load_all_runbook_chunks(runbooks_dir: Path = RUNBOOKS_DIR) -> list[RunbookChunk]:
    """Load and parse all Markdown SOP runbooks in sorted filename order."""
    chunks: list[RunbookChunk] = []
    if not runbooks_dir.exists():
        return chunks
    for md_path in sorted(runbooks_dir.glob("*.md")):
        chunks.extend(parse_runbook_sections(md_path))
    return chunks


def seed_database(db_path: Path = DB_PATH, runbooks_dir: Path = RUNBOOKS_DIR) -> dict[str, int]:
    """Create and seed the SQLite operational telemetry DB and FTS5 runbook index."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    db = sqlite_utils.Database(db_path)

    # 1. Structured Operational Tables
    db.execute(
        """
        CREATE TABLE incidents (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            tail_number TEXT,
            work_center TEXT,
            component_code TEXT,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            reported_at TEXT NOT NULL,
            updated_at TEXT,
            plant_code TEXT NOT NULL,
            shift_lead TEXT,
            root_cause_code TEXT
        )
        """
    )
    db.execute("CREATE INDEX idx_incidents_incident_id ON incidents(incident_id)")
    db.execute("CREATE INDEX idx_incidents_component ON incidents(component_code)")

    db.execute(
        """
        CREATE TABLE work_orders (
            work_order_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL,
            sap_order_number TEXT,
            assigned_team TEXT NOT NULL,
            priority TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            parts_required TEXT
        )
        """
    )
    db.execute("CREATE INDEX idx_work_orders_incident_id ON work_orders(incident_id)")

    db.execute(
        """
        CREATE TABLE component_telemetry (
            telemetry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tail_number TEXT,
            component_code TEXT NOT NULL,
            sensor_metric TEXT NOT NULL,
            metric_value REAL NOT NULL,
            unit TEXT NOT NULL,
            threshold_max REAL NOT NULL,
            anomaly_flag INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            facility TEXT NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX idx_telemetry_tail_comp ON component_telemetry(tail_number, component_code)")

    # Intentionally messy data:
    # - Duplicate incident_id rows (INC-2026-0891, INC-2026-0904) from SAP/ServiceNow overlap
    # - Missing/empty/NULL fields (shift_lead="", root_cause_code=None, tail_number=None)
    # - Timestamp skew: UTC ('Z'), offset ('-06:00', '+01:00'), and naive local timestamps
    incidents_rows = [
        # Duplicate pair 1: A320 Avionics Bay Blower fault (SAP + ServiceNow duplicate ingest)
        {
            "incident_id": "INC-2026-0891",
            "tail_number": "F-WWBA",
            "work_center": "WC-AVIONICS-FAL2",
            "component_code": "AV-BLOWER-400HZ",
            "severity": "SEV-1",
            "status": "OPEN",
            "summary": "80VU avionics bay cooling blower tripped thermal relay 12HQ; ARINC 429 CRC errors spiking.",
            "reported_at": "2026-11-17T14:22:10Z",
            "updated_at": None,
            "plant_code": "TLS-FAL1",
            "shift_lead": "",
            "root_cause_code": None,
        },
        {
            "incident_id": "INC-2026-0891",
            "tail_number": None,
            "work_center": "WC-AVIONICS-FAL2",
            "component_code": "AV-BLOWER-400HZ",
            "severity": "SEV-1",
            "status": "TRIAGING",
            "summary": "ServiceNow sync mirror: 80VU cooling blower current draw 12.4A on GPU ground run.",
            "reported_at": "2026-11-17T15:22:10+01:00",
            "updated_at": "2026-11-17 15:35:00",  # Naive Toulouse local time (+01:00)
            "plant_code": "TLS-FAL1",
            "shift_lead": "M. Laurent",
            "root_cause_code": "RC-AV-FOD-PLENUM",
        },
        # Duplicate pair 2: Hydraulic PTU oscillation in Mobile FAL
        {
            "incident_id": "INC-2026-0904",
            "tail_number": "N518US",
            "work_center": "WC-HYDRAULICS-01",
            "component_code": "HYD-PTU-GRN",
            "severity": "SEV-2",
            "status": "OPEN",
            "summary": "Green/Yellow PTU pressure hunting 2810-3240 PSI during Yellow electric pump check.",
            "reported_at": "2026-11-17 08:10:00-06:00",
            "updated_at": "2026-11-17T14:18:45Z",
            "plant_code": "MOB-FAL2",
            "shift_lead": "R. Vance",
            "root_cause_code": None,
        },
        {
            "incident_id": "INC-2026-0904",
            "tail_number": "N518US",
            "work_center": "",
            "component_code": "HYD-PTU-GRN",
            "severity": "SEV-2",
            "status": "IN_PROGRESS",
            "summary": "SAP PM duplicate record: PTU thermal sensor approaching 90C relief threshold.",
            "reported_at": "2026-11-17 08:10:00",  # Naive Central time
            "updated_at": "2026-11-17 08:25:12-06:00",
            "plant_code": "MOB-FAL2",
            "shift_lead": "",
            "root_cause_code": "RC-HYD-AIR-ENTRAINED",
        },
        # Autoclave thermal excursion (Wichita composites)
        {
            "incident_id": "INC-2026-0912",
            "tail_number": "SPAR-BATCH-449",
            "work_center": "WC-NDT-COMPOSITES",
            "component_code": "AUTOCLAVE-Z3",
            "severity": "SEV-1",
            "status": "QUARANTINE_HOLD",
            "summary": "CFRP wing spar cure dwell overshoot to 188.4C for 11 minutes in Zone 3.",
            "reported_at": "2026-11-17 06:45:00",  # Naive US Central time
            "updated_at": "2026-11-17T12:55:00Z",
            "plant_code": "ICT-COMP3",
            "shift_lead": "K. Chen",
            "root_cause_code": "RC-PID-VALVE-OVERSHOOT",
        },
        # Minor tooling incident with missing tail_number and root_cause_code
        {
            "incident_id": "INC-2026-0915",
            "tail_number": None,
            "work_center": "WC-WINGBOX-JOIN",
            "component_code": "TORQ-CTRL-09",
            "severity": "SEV-3",
            "status": "OPEN",
            "summary": "DC nutrunner controller calibration drift flagged during rib-to-spar fastening.",
            "reported_at": "2026-11-16T22:40:00Z",
            "updated_at": None,
            "plant_code": "TLS-FAL1",
            "shift_lead": None,
            "root_cause_code": "",
        },
        # Resolved historical avionics fault for baseline comparison
        {
            "incident_id": "INC-2026-0840",
            "tail_number": "F-WWBC",
            "work_center": "WC-AVIONICS-FAL2",
            "component_code": "AV-BLOWER-400HZ",
            "severity": "SEV-2",
            "status": "RESOLVED",
            "summary": "82VU extract filter clogged with composite trimming dust; cleaned and verified 8.1A.",
            "reported_at": "2026-11-15T10:05:00Z",
            "updated_at": "2026-11-15T13:40:00Z",
            "plant_code": "TLS-FAL1",
            "shift_lead": "M. Laurent",
            "root_cause_code": "RC-AV-FILTER-FOD",
        },
        # Offline gateway sync failure incident
        {
            "incident_id": "INC-2026-0920",
            "tail_number": "N522US",
            "work_center": "WC-FINAL-TEST",
            "component_code": "EDGE-SYNC-GW1",
            "severity": "SEV-2",
            "status": "OPEN",
            "summary": "Ruggedized tablet batch sync queued duplicate SAP RFC frames during Wi-Fi roaming drop.",
            "reported_at": "2026-11-17T11:02:19-06:00",
            "updated_at": "2026-11-17T17:04:01Z",
            "plant_code": "MOB-FAL2",
            "shift_lead": "D. Patel",
            "root_cause_code": None,
        },
    ]
    db["incidents"].insert_all(incidents_rows)

    work_orders_rows = [
        {
            "work_order_id": "WO-SAP-44019",
            "incident_id": "INC-2026-0891",
            "sap_order_number": "400089122",
            "assigned_team": "Avionics Line Maintenance - Station 40",
            "priority": "P1",
            "state": "IN_PROGRESS",
            "created_at": "2026-11-17T14:30:00Z",
            "parts_required": "PN 80VU-HEPA-04, Megger Test Rig",
        },
        {
            "work_order_id": "WO-SAP-44023",
            "incident_id": "INC-2026-0904",
            "sap_order_number": "400089155",
            "assigned_team": "Hydraulic Systems Team B",
            "priority": "P2",
            "state": "OPEN",
            "created_at": "2026-11-17 08:20:00-06:00",
            "parts_required": "FIN 1021GM Filter Element, Skydrol LD-4 (2L)",
        },
        {
            "work_order_id": "WO-SAP-44031",
            "incident_id": "INC-2026-0912",
            "sap_order_number": "400089190",
            "assigned_team": "Composites NDT & MRB Lab",
            "priority": "P1",
            "state": "HOLD_VERIFICATION",
            "created_at": "2026-11-17T13:05:00Z",
            "parts_required": "PAUT 5MHz Phased Array Probe, DSC Witness Coupon",
        },
        {
            "work_order_id": "WO-SAP-43910",
            "incident_id": "INC-2026-0840",
            "sap_order_number": "400088702",
            "assigned_team": "Avionics Line Maintenance - Station 40",
            "priority": "P2",
            "state": "CLOSED",
            "created_at": "2026-11-15T10:15:00Z",
            "parts_required": "PN 80VU-HEPA-04",
        },
    ]
    db["work_orders"].insert_all(work_orders_rows)

    telemetry_rows = [
        # F-WWBA Avionics anomalies (INC-2026-0891)
        {
            "tail_number": "F-WWBA",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "blower_current_amps",
            "metric_value": 12.4,
            "unit": "A",
            "threshold_max": 11.5,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17T14:21:55Z",
            "facility": "TLS-FAL1",
        },
        {
            "tail_number": "F-WWBA",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "bay_inlet_temp_c",
            "metric_value": 44.8,
            "unit": "C",
            "threshold_max": 38.0,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17 15:21:58+01:00",
            "facility": "TLS-FAL1",
        },
        {
            "tail_number": "F-WWBA",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "arinc429_crc_err_rate",
            "metric_value": 14.0,
            "unit": "err/min",
            "threshold_max": 5.0,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17 15:22:05",  # Naive local TLS time
            "facility": "TLS-FAL1",
        },
        # N518US Hydraulic PTU anomalies (INC-2026-0904)
        {
            "tail_number": "N518US",
            "component_code": "HYD-PTU-GRN",
            "sensor_metric": "hyd_pressure_psi",
            "metric_value": 3240.0,
            "unit": "PSI",
            "threshold_max": 3150.0,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17 08:09:42-06:00",
            "facility": "MOB-FAL2",
        },
        {
            "tail_number": "N518US",
            "component_code": "HYD-PTU-GRN",
            "sensor_metric": "hyd_fluid_temp_c",
            "metric_value": 89.6,
            "unit": "C",
            "threshold_max": 88.0,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17T14:11:05Z",
            "facility": "MOB-FAL2",
        },
        {
            "tail_number": "N518US",
            "component_code": "HYD-PTU-GRN",
            "sensor_metric": "ptu_case_drain_lpm",
            "metric_value": 0.9,
            "unit": "L/min",
            "threshold_max": 2.4,
            "anomaly_flag": 0,
            "recorded_at": "2026-11-17T14:11:05Z",
            "facility": "MOB-FAL2",
        },
        # Autoclave Z3 thermal excursion (INC-2026-0912)
        {
            "tail_number": "SPAR-BATCH-449",
            "component_code": "AUTOCLAVE-Z3",
            "sensor_metric": "autoclave_zone3_temp_c",
            "metric_value": 188.4,
            "unit": "C",
            "threshold_max": 185.0,
            "anomaly_flag": 1,
            "recorded_at": "2026-11-17 06:43:10",
            "facility": "ICT-COMP3",
        },
        {
            "tail_number": "SPAR-BATCH-449",
            "component_code": "AUTOCLAVE-Z3",
            "sensor_metric": "vac_bag_leak_rate_mbar_min",
            "metric_value": 1.4,
            "unit": "mbar/min",
            "threshold_max": 2.0,
            "anomaly_flag": 0,
            "recorded_at": "2026-11-17T12:43:10Z",
            "facility": "ICT-COMP3",
        },
        # Baseline nominal sensor (F-WWBC)
        {
            "tail_number": "F-WWBC",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "blower_current_amps",
            "metric_value": 8.1,
            "unit": "A",
            "threshold_max": 11.5,
            "anomaly_flag": 0,
            "recorded_at": "2026-11-15T13:35:00Z",
            "facility": "TLS-FAL1",
        },
    ]
    db["component_telemetry"].insert_all(telemetry_rows)

    # Plant Historian 1-minute window rollups (simulating partitioned Parquet/PI extracts)
    db.execute(
        """
        CREATE TABLE historian_rollups (
            rollup_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tail_number TEXT NOT NULL,
            component_code TEXT NOT NULL,
            sensor_metric TEXT NOT NULL,
            window_start_utc TEXT NOT NULL,
            window_end_utc TEXT NOT NULL,
            min_val REAL NOT NULL,
            max_val REAL NOT NULL,
            avg_val REAL NOT NULL,
            p95_val REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            threshold_max REAL NOT NULL,
            anomaly_flag INTEGER NOT NULL,
            parquet_partition TEXT NOT NULL
        )
        """
    )
    historian_rows = [
        {
            "tail_number": "F-WWBA",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "blower_current_amps",
            "window_start_utc": "2026-11-17T14:21:00Z",
            "window_end_utc": "2026-11-17T14:22:00Z",
            "min_val": 9.7,
            "max_val": 12.6,
            "avg_val": 11.4,
            "p95_val": 12.4,
            "sample_count": 60,
            "threshold_max": 11.5,
            "anomaly_flag": 1,
            "parquet_partition": "facility=TLS-FAL1/dt=2026-11-17/part-0001.parquet",
        },
        {
            "tail_number": "F-WWBA",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "bay_inlet_temp_c",
            "window_start_utc": "2026-11-17T14:21:00Z",
            "window_end_utc": "2026-11-17T14:22:00Z",
            "min_val": 41.2,
            "max_val": 45.1,
            "avg_val": 43.8,
            "p95_val": 44.8,
            "sample_count": 60,
            "threshold_max": 38.0,
            "anomaly_flag": 1,
            "parquet_partition": "facility=TLS-FAL1/dt=2026-11-17/part-0001.parquet",
        },
        {
            "tail_number": "N518US",
            "component_code": "HYD-PTU-GRN",
            "sensor_metric": "hyd_pressure_psi",
            "window_start_utc": "2026-11-17T14:09:00Z",
            "window_end_utc": "2026-11-17T14:10:00Z",
            "min_val": 2810.0,
            "max_val": 3240.0,
            "avg_val": 3045.5,
            "p95_val": 3210.0,
            "sample_count": 120,
            "threshold_max": 3150.0,
            "anomaly_flag": 1,
            "parquet_partition": "facility=MOB-FAL2/dt=2026-11-17/part-0004.parquet",
        },
        {
            "tail_number": "SPAR-BATCH-449",
            "component_code": "AUTOCLAVE-Z3",
            "sensor_metric": "autoclave_zone3_temp_c",
            "window_start_utc": "2026-11-17T12:43:00Z",
            "window_end_utc": "2026-11-17T12:44:00Z",
            "min_val": 182.9,
            "max_val": 188.4,
            "avg_val": 186.7,
            "p95_val": 188.1,
            "sample_count": 60,
            "threshold_max": 185.0,
            "anomaly_flag": 1,
            "parquet_partition": "facility=ICT-COMP3/dt=2026-11-17/part-0002.parquet",
        },
        {
            "tail_number": "F-WWBC",
            "component_code": "AV-BLOWER-400HZ",
            "sensor_metric": "blower_current_amps",
            "window_start_utc": "2026-11-15T13:35:00Z",
            "window_end_utc": "2026-11-15T13:36:00Z",
            "min_val": 7.8,
            "max_val": 8.4,
            "avg_val": 8.1,
            "p95_val": 8.3,
            "sample_count": 60,
            "threshold_max": 11.5,
            "anomaly_flag": 0,
            "parquet_partition": "facility=TLS-FAL1/dt=2026-11-15/part-0001.parquet",
        },
    ]
    db["historian_rollups"].insert_all(historian_rows)
    db.execute(
        """
        CREATE VIEW v_anomalous_historian_telemetry AS
        SELECT tail_number, component_code, sensor_metric,
               window_start_utc, window_end_utc,
               min_val, max_val, avg_val, p95_val,
               threshold_max, sample_count, parquet_partition
        FROM historian_rollups
        WHERE anomaly_flag = 1
        ORDER BY window_start_utc DESC
        """
    )

    # 2. Unstructured Knowledge Base SQLite FTS5 Virtual Table
    chunks = load_all_runbook_chunks(runbooks_dir)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE runbooks_fts USING fts5(
                chunk_id UNINDEXED,
                doc_id,
                doc_title,
                section_heading,
                line_start UNINDEXED,
                line_end UNINDEXED,
                citation UNINDEXED,
                content,
                tokenize='porter unicode61'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO runbooks_fts (
                chunk_id, doc_id, doc_title, section_heading,
                line_start, line_end, citation, content
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c.chunk_id,
                    c.doc_id,
                    c.doc_title,
                    c.section_heading,
                    c.line_start,
                    c.line_end,
                    c.citation,
                    c.content,
                )
                for c in chunks
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "incidents": len(incidents_rows),
        "work_orders": len(work_orders_rows),
        "component_telemetry": len(telemetry_rows),
        "historian_rollups": len(historian_rows),
        "runbook_chunks": len(chunks),
    }


if __name__ == "__main__":
    summary = seed_database()
    print(f"Seeded {DB_PATH}: {summary}")
