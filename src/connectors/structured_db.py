"""Read-only parameterized SQLite connector with strict SQL guardrails and timeout."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from src.resilience import DB_TIMEOUT_SECONDS, UpstreamTimeoutError

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ops_telemetry.db"
MAX_ROW_LIMIT = 100

# REPLACE is only forbidden as a statement keyword; the scalar replace(...) function is allowed.
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|REPLACE(?!\s*\()|CREATE|TRUNCATE|PRAGMA|VACUUM|REINDEX|GRANT|REVOKE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)
# Trailing "LIMIT n", "LIMIT n OFFSET m" or SQLite's "LIMIT m, n" (offset, count)
_LIMIT_TAIL_RE = re.compile(
    r"\bLIMIT\s+(\d+)(?:\s+OFFSET\s+(\d+)|\s*,\s*(\d+))?\s*$",
    re.IGNORECASE,
)
_ANY_LIMIT_TAIL_RE = re.compile(r"\bLIMIT\b[^()]*$", re.IGNORECASE)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

PLANT_TIMEZONES: dict[str, str] = {
    "TLS-FAL1": "Europe/Paris",
    "TLS-FAL2": "Europe/Paris",
    "HAM-FAL3": "Europe/Berlin",
    "MOB-FAL2": "America/Chicago",
    "ICT-COMP3": "America/Chicago",
}


class SQLSafetyError(ValueError):
    """Raised when a SQL query violates read-only or row-limit compliance guardrails."""


def _mask_string_literals(sql: str) -> str:
    """Replace single-quoted literals with '' so keyword/comment checks ignore user data."""
    return _STRING_LITERAL_RE.sub("''", sql)


def validate_and_enforce_sql(sql: str) -> str:
    """Validate that SQL is read-only and enforce LIMIT <= 100."""
    return _validate_and_rewrite(sql, MAX_ROW_LIMIT)[0]


def _validate_and_rewrite(sql: str, cap: int) -> tuple[str, bool]:
    """Validate SQL and rewrite its row limit to ``cap``.

    Returns ``(rewritten_sql, capped)`` where ``capped`` is True when the server-side
    MAX_ROW_LIMIT was imposed (no LIMIT given, or a LIMIT larger than allowed).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SQLSafetyError("SQL query must be a non-empty string.")

    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    # Structural checks run against a copy with string literals masked, so a value like
    # LIKE '%update%' or '--' inside quotes does not trip the guardrails.
    masked = _mask_string_literals(cleaned)
    if masked.count("'") % 2 != 0:
        raise SQLSafetyError("Unbalanced string literal quotes in SQL query.")

    # Reject SQL comments to avoid obfuscated payloads
    if "--" in masked or "/*" in masked or "*/" in masked:
        raise SQLSafetyError("SQL comments (-- or /* */) are prohibited in telemetry queries.")

    # Allow an optional single trailing semicolon; forbid stacked statements
    if ";" in masked:
        raise SQLSafetyError("Multiple SQL statements separated by ';' are prohibited.")

    forbidden_match = _FORBIDDEN_SQL_RE.search(masked)
    if forbidden_match:
        keyword = forbidden_match.group(1).upper()
        raise SQLSafetyError(
            f"Read-only guardrail violation: SQL keyword '{keyword}' is prohibited."
        )

    if not re.match(r"^(SELECT|WITH)\b", masked, re.IGNORECASE):
        raise SQLSafetyError("Only SELECT or WITH (CTE) read-only queries are permitted.")

    limit_match = _LIMIT_TAIL_RE.search(masked)
    if limit_match:
        if limit_match.group(3) is not None:
            # SQLite "LIMIT offset, count" form
            offset, requested = int(limit_match.group(1)), int(limit_match.group(3))
        else:
            requested = int(limit_match.group(1))
            offset = int(limit_match.group(2)) if limit_match.group(2) else None
        if requested > MAX_ROW_LIMIT:
            offset_part = f" OFFSET {offset}" if offset is not None else ""
            return cleaned[: limit_match.start()] + f"LIMIT {cap}{offset_part}", True
        return cleaned, False
    if _ANY_LIMIT_TAIL_RE.search(masked):
        # Non-literal LIMIT (e.g. "LIMIT ?" bound parameter): cap via an outer query
        return f"SELECT * FROM ({cleaned}) LIMIT {cap}", True
    return f"{cleaned} LIMIT {cap}", True


def normalize_timestamp_to_utc(ts: str | None, plant_code: str | None = None) -> tuple[str | None, bool]:
    """Normalize UTC, offset, and naive plant-local timestamps to ISO-8601 UTC ('Z')."""
    if ts is None:
        return None, False
    raw = str(ts).strip()
    if not raw:
        return None, False

    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), False
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        # Malformed upstream timestamp: surface it untouched rather than failing triage
        return None, False

    if dt.tzinfo is not None:
        utc_iso = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return utc_iso, True

    tz_name = PLANT_TIMEZONES.get((plant_code or "").upper(), "UTC")
    localized = dt.replace(tzinfo=ZoneInfo(tz_name))
    utc_iso = localized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return utc_iso, True


class StructuredDBConnector:
    """Read-only SQLite connector with parameterized queries, timeout, and hygiene reconciliation."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        timeout_seconds: float = DB_TIMEOUT_SECONDS,
    ) -> None:
        self.db_path = Path(db_path)
        self.timeout_seconds = float(timeout_seconds)

    def _open_readonly(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Operational telemetry DB not found at {self.db_path}. Run 'python data/seed_db.py'."
            )
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    async def execute_query(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and execute a parameterized read-only SQL query within timeout."""
        safe_sql, capped = _validate_and_rewrite(sql, MAX_ROW_LIMIT)
        # When the server cap applies, fetch one sentinel row so truncation is exact
        exec_sql = _validate_and_rewrite(sql, MAX_ROW_LIMIT + 1)[0] if capped else safe_sql
        bound_params = tuple(params or ())

        active_conn: dict[str, sqlite3.Connection | None] = {"conn": None}

        def _run_sync() -> dict[str, Any]:
            conn = self._open_readonly()
            active_conn["conn"] = conn
            try:
                cursor = conn.execute(exec_sql, bound_params)
                # Defense in depth: hard-cap rows at fetch time regardless of SQL text
                fetched = cursor.fetchmany(MAX_ROW_LIMIT + 1)
                rows = [dict(row) for row in fetched[:MAX_ROW_LIMIT]]
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                return {
                    "executed_sql": safe_sql,
                    "columns": columns,
                    "row_count": len(rows),
                    "truncated_to_limit": len(fetched) > MAX_ROW_LIMIT,
                    "rows": rows,
                }
            finally:
                conn.close()
                active_conn["conn"] = None

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_run_sync),
                timeout=self.timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            conn = active_conn.get("conn")
            if conn is not None:
                try:
                    conn.interrupt()
                except Exception:
                    pass
            raise UpstreamTimeoutError(
                f"structured_db query timed out after {self.timeout_seconds:.1f}s"
            ) from exc

    async def get_schema_summary(self) -> dict[str, Any]:
        """Inspect user table/view columns in read-only mode (FTS5 shadow tables excluded)."""
        tables_res = await self.execute_query(
            "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
        )
        rows = tables_res["rows"]
        virtual_tables = {
            r["name"]
            for r in rows
            if (r.get("sql") or "").upper().startswith("CREATE VIRTUAL TABLE")
        }
        shadow_suffixes = ("_content", "_data", "_idx", "_docsize", "_config")
        shadow_tables = {f"{vt}{suf}" for vt in virtual_tables for suf in shadow_suffixes}

        def _inspect_safe() -> dict[str, list[dict[str, Any]]]:
            conn = self._open_readonly()
            try:
                out: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    tbl = row["name"]
                    if tbl.startswith("sqlite_") or tbl in shadow_tables:
                        continue
                    cur = conn.execute(f'SELECT * FROM "{tbl}" LIMIT 0')
                    cols = [d[0] for d in cur.description] if cur.description else []
                    out[tbl] = [{"name": col} for col in cols]
                return out
            finally:
                conn.close()

        schema = await asyncio.wait_for(
            asyncio.to_thread(_inspect_safe),
            timeout=self.timeout_seconds,
        )
        return {"database": str(self.db_path), "tables": schema}

    async def get_incident_bundle(self, incident_id: str) -> dict[str, Any]:
        """Fetch and reconcile incident, linked work orders, and component sensor telemetry."""
        inc_res = await self.execute_query(
            """
            SELECT row_id, incident_id, tail_number, work_center, component_code,
                   severity, status, summary, reported_at, updated_at,
                   plant_code, shift_lead, root_cause_code
            FROM incidents
            WHERE incident_id = ?
            ORDER BY row_id ASC
            """,
            [incident_id],
        )
        raw_rows = inc_res["rows"]
        if not raw_rows:
            return {
                "found": False,
                "incident_id": incident_id,
                "reconciled_incident": None,
                "duplicate_records_detected": False,
                "raw_record_count": 0,
                "hygiene_notes": ["No matching incident_id found in structured telemetry DB."],
                "work_orders": [],
                "component_telemetry": [],
            }

        reconciled, hygiene_notes = _reconcile_incident_rows(raw_rows)

        wo_res = await self.execute_query(
            """
            SELECT work_order_id, incident_id, sap_order_number, assigned_team,
                   priority, state, created_at, parts_required
            FROM work_orders
            WHERE incident_id = ?
            ORDER BY created_at DESC
            """,
            [incident_id],
        )
        plant_code = reconciled.get("plant_code")
        work_orders = []
        for wo in wo_res["rows"]:
            wo_copy = dict(wo)
            norm_created, skewed = normalize_timestamp_to_utc(wo_copy.get("created_at"), plant_code)
            wo_copy["created_at_utc"] = norm_created
            if skewed:
                wo_copy["timestamp_skew_corrected"] = True
            work_orders.append(wo_copy)

        tail = reconciled.get("tail_number")
        comp = reconciled.get("component_code")
        telemetry_rows: list[dict[str, Any]] = []
        if tail and comp:
            tel_res = await self.execute_query(
                """
                SELECT telemetry_id, tail_number, component_code, sensor_metric,
                       metric_value, unit, threshold_max, anomaly_flag, recorded_at, facility
                FROM component_telemetry
                WHERE tail_number = ? AND component_code = ?
                ORDER BY anomaly_flag DESC, recorded_at DESC
                """,
                [tail, comp],
            )
            telemetry_rows = tel_res["rows"]
        elif comp:
            tel_res = await self.execute_query(
                """
                SELECT telemetry_id, tail_number, component_code, sensor_metric,
                       metric_value, unit, threshold_max, anomaly_flag, recorded_at, facility
                FROM component_telemetry
                WHERE component_code = ?
                ORDER BY anomaly_flag DESC, recorded_at DESC
                """,
                [comp],
            )
            telemetry_rows = tel_res["rows"]

        normalized_telemetry = []
        for t in telemetry_rows:
            t_copy = dict(t)
            norm_ts, skewed = normalize_timestamp_to_utc(
                t_copy.get("recorded_at"), t_copy.get("facility") or plant_code
            )
            t_copy["recorded_at_utc"] = norm_ts
            t_copy["timestamp_skew_corrected"] = skewed
            normalized_telemetry.append(t_copy)

        return {
            "found": True,
            "incident_id": incident_id,
            "duplicate_records_detected": len(raw_rows) > 1,
            "raw_record_count": len(raw_rows),
            "reconciled_incident": reconciled,
            "hygiene_notes": hygiene_notes,
            "work_orders": work_orders,
            "component_telemetry": normalized_telemetry,
        }


def _reconcile_incident_rows(raw_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Coalesce duplicate incident rows and normalize timestamps to UTC."""
    hygiene_notes: list[str] = []
    if len(raw_rows) > 1:
        hygiene_notes.append(
            f"Detected {len(raw_rows)} duplicate rows for incident_id={raw_rows[0]['incident_id']}; "
            "coalesced non-empty fields across SAP/ServiceNow streams."
        )

    # Order rows oldest -> freshest (by normalized updated_at, falling back to reported_at)
    # so the most recently updated sync stream wins field conflicts, not insertion order.
    def _freshness(row: dict[str, Any]) -> str:
        plant = row.get("plant_code")
        upd, _ = normalize_timestamp_to_utc(row.get("updated_at"), plant)
        rep, _ = normalize_timestamp_to_utc(row.get("reported_at"), plant)
        return upd or rep or ""

    raw_rows = sorted(raw_rows, key=_freshness)

    merged: dict[str, Any] = {}
    missing_fields: set[str] = set()
    fields = [
        "incident_id",
        "tail_number",
        "work_center",
        "component_code",
        "severity",
        "status",
        "summary",
        "reported_at",
        "updated_at",
        "plant_code",
        "shift_lead",
        "root_cause_code",
    ]
    for field in fields:
        val = None
        had_empty = False
        for row in raw_rows:
            candidate = row.get(field)
            if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
                had_empty = True
            else:
                val = candidate
        merged[field] = val
        if had_empty and val is None:
            missing_fields.add(field)
        elif had_empty and val is not None and len(raw_rows) > 1:
            hygiene_notes.append(f"Recovered missing '{field}' from duplicate sync row.")

    if missing_fields:
        hygiene_notes.append(
            f"Still missing/null fields after merge: {', '.join(sorted(missing_fields))}."
        )

    plant_code = merged.get("plant_code")
    # First report time is the earliest across duplicate streams
    reported = [
        normalize_timestamp_to_utc(r.get("reported_at"), r.get("plant_code") or plant_code)
        for r in raw_rows
    ]
    reported_valid = [(ts, sk) for ts, sk in reported if ts]
    rep_utc, rep_skewed = min(reported_valid) if reported_valid else (None, False)
    upd_utc, upd_skewed = normalize_timestamp_to_utc(merged.get("updated_at"), plant_code)
    merged["reported_at_utc"] = rep_utc
    merged["updated_at_utc"] = upd_utc
    merged["timestamp_skew_corrected"] = bool(rep_skewed or upd_skewed)
    if merged["timestamp_skew_corrected"]:
        hygiene_notes.append(
            f"Normalized skewed/local timestamps to UTC using facility timezone ({plant_code})."
        )

    return merged, hygiene_notes
