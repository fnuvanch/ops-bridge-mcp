"use strict";

/* Ops Bridge Console. Vanilla JS, no external assets (works air-gapped). */

const ROLES = {
  field_viewer: "Field viewer",
  ops_engineer: "Ops engineer",
  incident_commander: "Incident commander",
};

const TOOL_NAMES = {
  query_telemetry: "Query telemetry",
  search_runbooks: "Search procedures",
  diagnose_and_triage_incident: "Triage incident",
  create_work_order: "Create work order",
  escalate_line_stoppage: "Halt line and escalate",
  replay_outbox_actions: "Replay outbox",
  oauth_token_refresh: "Refresh ITSM token",
};

const STATUS_TEXT = {
  OK: "OK",
  FORBIDDEN: "Blocked",
  DEGRADED: "Partial",
  UNAVAILABLE: "Unavailable",
  CIRCUIT_OPEN_QUEUED: "Queued",
  UPSTREAM_ERROR: "ITSM error",
  VALIDATION_ERROR: "Invalid",
  SQL_GUARDRAIL_REJECTED: "Rejected",
  TIMEOUT: "Timed out",
  ERROR: "Error",
  TOKEN_REFRESHED: "Refreshed",
  AUTH_REFRESH_FAILED: "Refresh failed",
};

const METRIC_NAMES = {
  blower_current_amps: "Blower current",
  bay_inlet_temp_c: "Bay inlet temperature",
  arinc429_crc_err_rate: "ARINC 429 CRC errors",
  hyd_pressure_psi: "Hydraulic pressure",
  hyd_fluid_temp_c: "Hydraulic fluid temperature",
  ptu_case_drain_lpm: "PTU case drain flow",
  autoclave_zone3_temp_c: "Autoclave zone 3 temperature",
  vac_bag_leak_rate_mbar_min: "Vacuum bag leak rate",
};

const QUERY_PRESETS = [
  ["Anomalous historian windows", "SELECT * FROM v_anomalous_historian_telemetry"],
  [
    "Duplicate incident rows",
    "SELECT incident_id, COUNT(*) AS source_rows, GROUP_CONCAT(status) AS statuses\nFROM incidents\nGROUP BY incident_id\nHAVING COUNT(*) > 1",
  ],
  [
    "Open work orders",
    "SELECT work_order_id, incident_id, assigned_team, priority, state\nFROM work_orders\nWHERE state <> 'CLOSED'",
  ],
  ["Try a write (it's blocked)", "DELETE FROM incidents WHERE severity = 'SEV-3'"],
];

const CHAOS_TEXT = {
  fail_next_429: (n) => `The next ${n} ITSM calls return 429 Too Many Requests. Retries back off and recover.`,
  fail_next_503: (n) => `The next ${n} ITSM calls return 503. Retries back off and recover.`,
  fail_next_500: (n) => `ITSM fails the next ${n} calls. Three failures open the circuit.`,
  force_401_once: () => "The access token expires on the next call. The client refreshes it and retries once.",
  reset: () => "Faults cleared and the mock ITSM reset. The circuit closes after its next successful call.",
};

const state = {
  role: loadRole(),
  incidentId: null,
  seenAudit: new Set(),
  auditPrimed: false,
  inflight: { system: false, audit: false },
};

/* ---------- small helpers ---------- */

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, val] of Object.entries(attrs || {})) {
    if (val === null || val === undefined || val === false) continue;
    if (key === "class") node.className = val;
    else if (key === "text") node.textContent = val;
    else if (key.startsWith("on") && typeof val === "function") node.addEventListener(key.slice(2), val);
    else if (key === "style" && typeof val === "object") Object.assign(node.style, val);
    else node.setAttribute(key, val === true ? "" : String(val));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const $ = (sel) => document.querySelector(sel);

function loadRole() {
  try {
    const saved = localStorage.getItem("opsConsoleRole");
    if (saved && ROLES[saved]) return saved;
  } catch (_) { /* storage unavailable */ }
  return "ops_engineer";
}

function saveRole(role) {
  try { localStorage.setItem("opsConsoleRole", role); } catch (_) { /* ignore */ }
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Console-Role": state.role },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const detail = data && data.detail;
    let msg = `HTTP ${res.status}`;
    if (typeof detail === "string") msg = detail;
    else if (Array.isArray(detail)) msg = detail.map((d) => `${(d.loc || []).slice(-1)[0] || "input"}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}

function fmtNum(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return Math.abs(v) >= 100 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : v.toLocaleString("en-US", { maximumFractionDigits: 1 });
}

function fmtUtc(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]}, ${hh}:${mm} UTC`;
}

function fmtClock(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function humanMetric(key) {
  if (METRIC_NAMES[key]) return METRIC_NAMES[key];
  const s = String(key || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function roleList(roles) {
  return (roles || []).map((r) => ROLES[r] || r).join(" or ");
}

function setResult(node, tone, text) {
  node.dataset.tone = tone;
  node.textContent = text;
}

/* ---------- role switcher ---------- */

function renderRoles() {
  document.querySelectorAll(".role button").forEach((btn) => {
    const on = btn.dataset.role === state.role;
    btn.setAttribute("aria-checked", String(on));
    btn.tabIndex = on ? 0 : -1;
  });
}

function initRoles() {
  const buttons = [...document.querySelectorAll(".role button")];
  buttons.forEach((btn, i) => {
    btn.addEventListener("click", () => {
      state.role = btn.dataset.role;
      saveRole(state.role);
      renderRoles();
      document.querySelectorAll(".action-result").forEach((n) => { n.textContent = ""; });
    });
    btn.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = buttons[(i + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length];
      next.focus();
      next.click();
    });
  });
  renderRoles();
}

/* ---------- tabs ---------- */

function selectTab(id) {
  document.querySelectorAll('[role="tab"]').forEach((tab) => {
    const on = tab.id === id;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !on;
  });
}

function initTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => selectTab(tab.id));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      next.focus();
      selectTab(next.id);
    });
  });
}

/* ---------- incident board ---------- */

async function loadIncidents() {
  const list = $("#incident-list");
  try {
    const res = await api("/api/incidents");
    if (res.status !== "OK") {
      list.replaceChildren(h("li", { class: "empty", text: res.message || "Couldn't load incidents." }));
      return;
    }
    list.replaceChildren(
      ...res.rows.map((row) =>
        h(
          "li",
          {},
          h(
            "button",
            {
              type: "button",
              class: "incident",
              "data-id": row.incident_id,
              "data-sev": row.severity,
              "data-resolved": row.status === "RESOLVED" ? "true" : "false",
              "aria-label": `${row.incident_id}, ${row.severity}, ${row.component_code || "component not recorded"}, ${(row.status || "").replace(/_/g, " ").toLowerCase()}`,
              onclick: () => openIncident(row.incident_id),
            },
            h("span", { class: "incident-top" },
              h("span", { class: "incident-id", text: row.incident_id }),
              h("span", { class: "incident-sev", text: row.severity })),
            h("span", { class: "incident-comp", text: row.component_code || "Component not recorded" }),
            h("span", { class: "incident-meta" },
              h("span", { text: row.plant_code }),
              h("span", { text: (row.status || "").replace(/_/g, " ").toLowerCase() }),
              row.raw_rows > 1 ? h("span", { class: "merged", text: `${row.raw_rows} source rows` }) : null),
          ),
        ),
      ),
    );
    const preferred = res.rows.find((r) => r.incident_id === "INC-2026-0891") || res.rows[0];
    if (preferred && !state.incidentId) openIncident(preferred.incident_id);
  } catch (err) {
    list.replaceChildren(h("li", { class: "empty", text: `Couldn't load incidents: ${err.message}` }));
  }
}

function markCurrentIncident() {
  document.querySelectorAll(".incident").forEach((btn) => {
    if (btn.dataset.id === state.incidentId) btn.setAttribute("aria-current", "true");
    else btn.removeAttribute("aria-current");
  });
}

async function openIncident(id) {
  state.incidentId = id;
  markCurrentIncident();
  selectTab("tab-triage");
  const panel = $("#panel-triage");
  panel.replaceChildren(h("p", { class: "empty", text: `Pulling telemetry and procedures for ${id}…` }));
  try {
    const res = await api(`/api/incidents/${encodeURIComponent(id)}/triage`);
    if (state.incidentId !== id) return;
    renderTriage(panel, res);
  } catch (err) {
    panel.replaceChildren(h("p", { class: "banner banner-warning", text: `Triage failed: ${err.message}` }));
  }
  refreshAudit();
}

/* ---------- triage view ---------- */

function renderTriage(panel, res) {
  if (res.error_code === "RBAC_FORBIDDEN") {
    panel.replaceChildren(h("p", { class: "banner banner-warning", text: res.message }));
    return;
  }
  const st = res.structured_telemetry;
  const rec = st && st.reconciled_incident;
  const parts = [];

  if (res.degraded_state) {
    parts.push(
      h("p", {
        class: res.status === "UNAVAILABLE" ? "banner banner-warning" : "banner",
        text: `Partial result: ${res.degraded_sources.join(" and ")} didn't answer, so this view shows what the other sources returned.`,
      }),
    );
  }

  if (!rec) {
    parts.push(h("p", { class: "empty", text: st && !st.found ? `No telemetry records for ${res.incident_id}.` : "Telemetry is unavailable for this incident." }));
  } else {
    parts.push(incidentHeader(rec, st));
    if (st.component_telemetry.length) parts.push(readoutSection(rec, st.component_telemetry));
    parts.push(reconciliationSection(st));
    if (st.work_orders.length) parts.push(workOrdersSection(st.work_orders));
  }

  const guidance = res.runbook_guidance;
  if (guidance && guidance.results && guidance.results.length) {
    parts.push(
      h("section", {},
        h("h2", { class: "section-title", text: "Procedures that apply" }),
        h("p", { class: "section-sub", text: "Ranked SOP sections. Open a citation to read the exact lines it refers to." }),
        guideList(guidance.results)),
    );
  }

  if (rec) parts.push(actionsSection(res.incident_id, rec));
  panel.replaceChildren(...parts);
}

function incidentHeader(rec, st) {
  const fact = (label, value) =>
    h("div", {}, h("dt", { text: label }), h("dd", { class: value ? null : "missing", text: value || "Not recorded" }));
  return h(
    "header",
    { class: "inc-head" },
    h("div", { class: "inc-title" },
      h("h1", { text: rec.incident_id }),
      h("span", { class: "sev-tag", "data-sev": rec.severity, text: rec.severity }),
      h("span", { class: "status-tag", text: (rec.status || "").replace(/_/g, " ").toLowerCase() })),
    h("p", { class: "inc-summary", text: rec.summary }),
    h("dl", { class: "facts" },
      fact("Aircraft or batch", rec.tail_number),
      fact("Component", rec.component_code),
      fact("Plant", rec.plant_code),
      fact("Work center", rec.work_center),
      fact("Shift lead", rec.shift_lead),
      fact("Root cause", rec.root_cause_code),
      fact("First reported", fmtUtc(rec.reported_at_utc)),
      fact("Last update", fmtUtc(rec.updated_at_utc))),
  );
}

function readoutSection(rec, telemetry) {
  const over = telemetry.filter((t) => t.anomaly_flag).length;
  return h(
    "section",
    {},
    h("h2", { class: "section-title", text: "Sensor readout" }),
    h("p", {
      class: "section-sub",
      text: over
        ? `${over} of ${telemetry.length} readings on ${rec.tail_number || rec.component_code} are past their engineering limit.`
        : `All ${telemetry.length} readings are within their engineering limits.`,
    }),
    h("div", { class: "readout" }, telemetry.map(gauge)),
  );
}

function gauge(t) {
  const value = Number(t.metric_value);
  const limit = Number(t.threshold_max);
  // Fixed range anchored on the engineering limit (limit sits at 2/3 of the bar), widened
  // only if the reading runs past it, so how far a value overshoots stays visible.
  const scaleMax = Math.max(limit * 1.5, value * 1.08) || 1;
  const pct = (n) => Math.max(0, Math.min(100, (n / scaleMax) * 100));
  const limitPct = pct(limit);
  const anomaly = Boolean(t.anomaly_flag);
  const delta = value - limit;
  const unit = t.unit || "";

  return h(
    "div",
    { class: "gauge", "data-anomaly": String(anomaly) },
    h("div", {},
      h("div", { class: "gauge-name", text: humanMetric(t.sensor_metric) }),
      h("div", { class: "gauge-when" },
        fmtUtc(t.recorded_at_utc) || "Time not recorded",
        t.timestamp_skew_corrected ? h("span", { class: "skew", text: `, converted from ${t.facility} plant time` }) : null)),
    h("div", { class: "gauge-track", role: "img", "aria-label": `${fmtNum(value)} ${unit} against a limit of ${fmtNum(limit)} ${unit}` },
      h("div", { class: "gauge-over", style: { width: `${100 - limitPct}%` } }),
      h("div", { class: "gauge-fill", style: { width: `${pct(value)}%` } }),
      h("div", { class: "gauge-limit", style: { left: `calc(${limitPct}% - 1px)` } }),
      h("div", { class: "gauge-limit-label", style: { left: `${limitPct}%` }, text: `limit ${fmtNum(limit)}` })),
    h("div", { class: "gauge-value" },
      h("span", { class: "gauge-num", text: fmtNum(value) }),
      h("span", { class: "gauge-unit", text: unit }),
      anomaly && delta > 0 ? h("span", { class: "gauge-delta", text: `${fmtNum(delta)} over` }) : null),
  );
}

function reconciliationSection(st) {
  const sub =
    st.raw_record_count > 1
      ? `${st.raw_record_count} source rows from SAP PM and ServiceNow were merged into the record above.`
      : "One source row. Timestamps were still normalized to UTC.";
  return h(
    "section",
    {},
    h("h2", { class: "section-title", text: "How the records were reconciled" }),
    h("p", { class: "section-sub", text: sub }),
    st.hygiene_notes.length
      ? h("ul", { class: "notes" }, st.hygiene_notes.map((n) => h("li", { text: n })))
      : h("p", { class: "hint", text: "Nothing needed fixing." }),
  );
}

function workOrdersSection(orders) {
  return h(
    "section",
    {},
    h("h2", { class: "section-title", text: "Work orders already in SAP" }),
    h("div", { class: "table-wrap" },
      h("table", {},
        h("thead", {}, h("tr", {}, ["Order", "Team", "Priority", "State", "Created", "Parts"].map((c) => h("th", { text: c })))),
        h("tbody", {},
          orders.map((o) =>
            h("tr", {},
              h("td", { text: o.work_order_id }),
              h("td", { text: o.assigned_team }),
              h("td", { text: o.priority }),
              h("td", { text: (o.state || "").replace(/_/g, " ").toLowerCase() }),
              h("td", { text: fmtUtc(o.created_at_utc) || "" }),
              h("td", { text: o.parts_required || "" })))))),
  );
}

/* ---------- procedure guidance (shared by triage and search) ---------- */

function cleanSnippet(text) {
  return String(text || "")
    .split("\n")
    .filter((line) => !line.trim().startsWith("#"))
    .join("\n")
    .replace(/\*\*/g, "")
    .trim()
    .slice(0, 360);
}

function guideList(results) {
  return h("ol", { class: "guides" }, results.map(guideItem));
}

function guideItem(hit) {
  const linesBox = h("div", { hidden: true });
  const toggle = h("button", {
    type: "button",
    class: "guide-cite",
    "aria-expanded": "false",
    text: `Show lines ${hit.line_start}–${hit.line_end} of ${hit.doc_id}`,
  });
  toggle.addEventListener("click", async () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    if (open) {
      linesBox.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = `Show lines ${hit.line_start}–${hit.line_end} of ${hit.doc_id}`;
      return;
    }
    if (!linesBox.childElementCount) {
      try {
        const res = await api(`/api/runbooks/${encodeURIComponent(hit.doc_id)}/lines?start=${hit.line_start}&end=${hit.line_end}`);
        linesBox.append(
          h("ol", { class: "source-lines", "aria-label": `${hit.doc_id} lines ${res.start} to ${res.end}` },
            res.lines.map((l) => h("li", {}, h("span", { class: "ln", text: l.n }), h("span", { class: "tx", text: l.text })))),
        );
      } catch (err) {
        linesBox.append(h("p", { class: "action-result", "data-tone": "warning", text: `Couldn't load source lines: ${err.message}` }));
      }
    }
    linesBox.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = "Hide source lines";
  });

  return h(
    "li",
    { class: "guide" },
    h("div", { class: "guide-head" },
      h("div", {},
        h("div", { class: "guide-section", text: hit.section_heading }),
        h("div", { class: "guide-doc", text: hit.doc_title })),
      h("span", { class: "guide-score", text: `Relevance ${hit.score.toFixed(2)}` })),
    h("p", { class: "guide-snippet", text: cleanSnippet(hit.snippet) }),
    toggle,
    linesBox,
  );
}

/* ---------- actions ---------- */

function actionsSection(incidentId, rec) {
  const sevToPriority = { "SEV-1": "P1", "SEV-2": "P2", "SEV-3": "P3" };

  const woTitle = h("input", { value: `Isolate ${rec.component_code || "fault"} on ${rec.tail_number || rec.work_center || incidentId}`, required: true });
  const woPriority = h("select", {}, ["P1", "P2", "P3", "P4"].map((p) => h("option", { value: p, selected: p === (sevToPriority[rec.severity] || "P2"), text: p })));
  const woCenter = h("input", { value: rec.work_center || "", required: true });
  const woResult = h("p", { class: "action-result", "aria-live": "polite" });
  const woButton = h("button", { type: "submit", class: "btn btn-act", text: "Create work order" });

  const woForm = h(
    "form",
    { class: "action" },
    h("div", {},
      h("h3", { text: "Open a work order" }),
      h("p", { class: "action-who", text: "Needs ops engineer or incident commander. Repeat clicks reuse one idempotency key, so ITSM never gets a duplicate." })),
    h("label", { class: "field" }, h("span", { text: "Title" }), woTitle),
    h("div", { class: "field-pair" },
      h("label", { class: "field" }, h("span", { text: "Priority" }), woPriority),
      h("label", { class: "field" }, h("span", { text: "Work center" }), woCenter)),
    h("div", { class: "row" }, woButton),
    woResult,
  );
  woForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    woButton.disabled = true;
    setResult(woResult, "caution", "Sending to ITSM…");
    try {
      const res = await api(`/api/incidents/${encodeURIComponent(incidentId)}/work-order`, {
        method: "POST",
        body: { title: woTitle.value, priority: woPriority.value, work_center: woCenter.value, description: rec.summary || "" },
      });
      const out = describeAction(res, "work_order");
      setResult(woResult, out.tone, out.text);
    } catch (err) {
      setResult(woResult, "warning", `Request failed: ${err.message}`);
    } finally {
      woButton.disabled = false;
      refreshSystem();
      refreshAudit();
    }
  });

  const escLine = h("input", { value: rec.work_center ? `${rec.plant_code}/${rec.work_center}` : rec.plant_code || "", required: true });
  const escSeverity = h("select", {}, ["SEV-1", "SEV-2", "SEV-3"].map((s) => h("option", { value: s, selected: s === rec.severity, text: s })));
  const escReason = h("input", { value: rec.summary || "", required: true });
  const escResult = h("p", { class: "action-result", "aria-live": "polite" });
  const escButton = h("button", { type: "submit", class: "btn btn-danger", text: "Halt line and escalate" });

  const escForm = h(
    "form",
    { class: "action action-danger" },
    h("div", {},
      h("h3", { text: "Stop the line" }),
      h("p", { class: "action-who", text: "Needs incident commander. A stoppage costs about $45k an hour of takt time." })),
    h("div", { class: "field-pair" },
      h("label", { class: "field" }, h("span", { text: "Line" }), escLine),
      h("label", { class: "field" }, h("span", { text: "Severity" }), escSeverity)),
    h("label", { class: "field" }, h("span", { text: "Reason" }), escReason),
    h("div", { class: "row" }, escButton),
    escResult,
  );
  escForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    escButton.disabled = true;
    setResult(escResult, "caution", "Sending to ITSM…");
    try {
      const res = await api(`/api/incidents/${encodeURIComponent(incidentId)}/escalate`, {
        method: "POST",
        body: { plant_code: rec.plant_code, line_id: escLine.value, severity: escSeverity.value, reason: escReason.value },
      });
      const out = describeAction(res, "escalation");
      setResult(escResult, out.tone, out.text);
    } catch (err) {
      setResult(escResult, "warning", `Request failed: ${err.message}`);
    } finally {
      escButton.disabled = false;
      refreshSystem();
      refreshAudit();
    }
  });

  return h(
    "section",
    {},
    h("h2", { class: "section-title", text: "Act on this incident" }),
    h("p", { class: "section-sub", text: "These write to the ITSM system. What you're allowed to do depends on the role you act as." }),
    h("div", { class: "actions" }, woForm, escForm),
  );
}

function describeAction(res, kind) {
  const verb = kind === "work_order" ? "create work orders" : "stop the line";
  if (res.error_code === "RBAC_FORBIDDEN") {
    return { tone: "warning", text: `Blocked: ${ROLES[res.operator_role] || res.operator_role} can't ${verb}. Needs ${roleList(res.required_roles)}.` };
  }
  if (res.error_code === "CIRCUIT_BREAKER_OPEN") {
    return {
      tone: "caution",
      text: `ITSM keeps failing, so the circuit is open. Saved to the outbox as #${res.outbox_id}. Replay it once the circuit closes.`,
    };
  }
  if (res.status !== "OK") {
    return { tone: "warning", text: `ITSM call failed: ${res.message || res.error_code}` };
  }
  const ticket = kind === "work_order" ? res.work_order : res.escalation;
  if (ticket.idempotent_replay) {
    return { tone: "ok", text: `${ticket.ticket_id} already exists for this incident, so ITSM returned it instead of creating a duplicate.` };
  }
  if (kind === "work_order") return { tone: "ok", text: `Created ${ticket.ticket_id}.` };
  return { tone: "ok", text: `Line stopped. Escalation ${ticket.ticket_id} is open; war room at ${ticket.war_room_bridge}.` };
}

/* ---------- query data ---------- */

function initQuery() {
  const input = $("#sql-input");
  $("#query-presets").replaceChildren(
    ...QUERY_PRESETS.map(([label, sql]) =>
      h("button", { type: "button", class: "preset", text: label, onclick: () => { input.value = sql; runQuery(); } }),
    ),
  );
  $("#run-query").addEventListener("click", runQuery);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runQuery(); }
  });
}

async function runQuery() {
  const out = $("#query-result");
  const btn = $("#run-query");
  btn.disabled = true;
  out.replaceChildren(h("p", { class: "hint", text: "Running…" }));
  try {
    const res = await api("/api/query", { method: "POST", body: { sql: $("#sql-input").value } });
    if (res.status !== "OK") {
      const lead = res.error_code === "SQL_GUARDRAIL_VIOLATION" ? "Rejected" : "Query failed";
      out.replaceChildren(h("p", { class: "action-result", "data-tone": "warning", text: `${lead}: ${res.message}` }));
      return;
    }
    const cols = res.columns;
    out.replaceChildren(
      h("p", { class: "result-meta" },
        `${res.row_count} ${res.row_count === 1 ? "row" : "rows"}${res.truncated_to_limit ? ", cut off at the 100-row limit" : ""}. Ran `,
        h("code", { text: res.executed_sql })),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, cols.map((c) => h("th", { text: c })))),
          h("tbody", {},
            res.rows.map((row) =>
              h("tr", {}, cols.map((c) => (row[c] === null ? h("td", { class: "null", text: "null" }) : h("td", { text: row[c] })))))))),
    );
  } catch (err) {
    out.replaceChildren(h("p", { class: "action-result", "data-tone": "warning", text: `Request failed: ${err.message}` }));
  } finally {
    btn.disabled = false;
    refreshAudit();
  }
}

/* ---------- search procedures ---------- */

function initSearch() {
  $("#search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const out = $("#search-result");
    out.replaceChildren(h("p", { class: "hint", text: "Searching…" }));
    try {
      const res = await api("/api/search", { method: "POST", body: { query: $("#search-input").value, top_k: 5 } });
      if (res.status !== "OK") {
        out.replaceChildren(h("p", { class: "action-result", "data-tone": "warning", text: res.message }));
      } else if (!res.results.length) {
        out.replaceChildren(h("p", { class: "hint", text: "No sections match. Try a component code such as HYD-PTU-GRN or a symptom like cure overshoot." }));
      } else {
        out.replaceChildren(guideList(res.results));
      }
    } catch (err) {
      out.replaceChildren(h("p", { class: "action-result", "data-tone": "warning", text: `Request failed: ${err.message}` }));
    }
    refreshAudit();
  });
}

/* ---------- fault injection + outbox ---------- */

function initFaults() {
  document.querySelectorAll("[data-chaos]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const body = JSON.parse(btn.dataset.chaos);
      const status = $("#fault-status");
      try {
        await api("/api/chaos", { method: "POST", body });
        const [key, val] = Object.entries(body)[0];
        status.textContent = CHAOS_TEXT[key] ? CHAOS_TEXT[key](val) : "Fault armed.";
      } catch (err) {
        status.textContent = `Fault not armed: ${err.message}`;
      }
      refreshSystem();
    });
  });

  $("#replay-outbox").addEventListener("click", async () => {
    const out = $("#replay-result");
    const btn = $("#replay-outbox");
    btn.disabled = true;
    try {
      const res = await api("/api/outbox/replay", { method: "POST" });
      if (res.error_code === "RBAC_FORBIDDEN") {
        setResult(out, "warning", `Blocked: ${ROLES[res.operator_role] || res.operator_role} can't replay the outbox. Needs ${roleList(res.required_roles)}.`);
      } else if (!res.pending_found) {
        setResult(out, "ok", "Nothing to replay.");
      } else {
        const bits = [`Replayed ${res.replayed_count}`];
        if (res.failed_count) bits.push(`${res.failed_count} failed`);
        if (res.deferred_count) bits.push(`${res.deferred_count} still waiting because the circuit is open`);
        setResult(out, res.status === "OK" ? "ok" : "caution", `${bits.join(", ")}.`);
      }
    } catch (err) {
      setResult(out, "warning", `Request failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      refreshSystem();
      refreshAudit();
    }
  });
}

/* ---------- system lamps ---------- */

function setLamp(id, tone, text) {
  const lamp = document.getElementById(id);
  if (tone) lamp.dataset.state = tone;
  else delete lamp.dataset.state;
  lamp.querySelector(".lamp-val").textContent = text;
}

async function refreshSystem() {
  if (state.inflight.system) return;
  state.inflight.system = true;
  try {
    const sys = await api("/api/system");

    const chaos = (sys.itsm && sys.itsm.chaos) || {};
    const armed = chaos.fail_next_429 || chaos.fail_next_503 || chaos.fail_next_500 || chaos.force_401_once || chaos.latency_ms;
    if (!sys.itsm.reachable) setLamp("lamp-itsm", "warning", "Unreachable");
    else if (armed) setLamp("lamp-itsm", "caution", "Faults armed");
    else setLamp("lamp-itsm", "ok", "Connected");

    const cb = sys.circuit_breaker;
    if (cb.state === "OPEN") setLamp("lamp-circuit", "warning", `Open, retry in ${Math.ceil(cb.retry_after_seconds)} s`);
    else if (cb.state === "HALF_OPEN") setLamp("lamp-circuit", "caution", "Half-open, next call tests ITSM");
    else if (cb.failure_count > 0) setLamp("lamp-circuit", "caution", `Closed, ${cb.failure_count} of ${cb.failure_threshold} failures`);
    else setLamp("lamp-circuit", "ok", "Closed");

    const counts = sys.outbox.counts || {};
    const pending = counts.PENDING || 0;
    const dead = counts.DEAD_LETTER || 0;
    if (dead) setLamp("lamp-outbox", "warning", `${dead} failed`);
    else if (pending) setLamp("lamp-outbox", "caution", `${pending} queued`);
    else setLamp("lamp-outbox", "ok", "Empty");

    setLamp("lamp-token", null, String(sys.token_refresh_count));
    renderOutbox(sys.outbox.recent);
  } catch (err) {
    setLamp("lamp-itsm", "warning", "Console offline");
  } finally {
    state.inflight.system = false;
  }
}

function renderOutbox(items) {
  const list = $("#outbox-list");
  if (!items.length) {
    list.replaceChildren(h("li", { class: "outbox-empty", text: "Nothing queued." }));
    return;
  }
  const label = { PENDING: "Queued", REPLAYED: "Sent", DEAD_LETTER: "Failed" };
  list.replaceChildren(
    ...items.map((it) =>
      h("li", {},
        h("span", { text: `${TOOL_NAMES[it.action_type] || it.action_type} for ${it.incident_id || "unknown incident"}` }),
        h("span", { class: "ob-status", "data-s": it.status, text: label[it.status] || it.status }),
        h("span", {
          class: "ob-sub",
          text: `#${it.outbox_id}, ${it.attempts} ${it.attempts === 1 ? "attempt" : "attempts"}${it.last_error && it.status !== "REPLAYED" ? `, last error: ${it.last_error.slice(0, 80)}` : ""}`,
        })),
    ),
  );
}

/* ---------- activity log ---------- */

function auditTone(e) {
  if (e.rbac_decision === "DENY") return "warning";
  if (e.rbac_decision === "SYSTEM") return e.status === "TOKEN_REFRESHED" ? "system" : "warning";
  if (e.status === "OK") return "ok";
  if (["DEGRADED", "CIRCUIT_OPEN_QUEUED", "TIMEOUT"].includes(e.status)) return "caution";
  return "warning";
}

function auditDetail(e) {
  const m = e.metadata || {};
  const who = e.rbac_decision === "SYSTEM" ? "System" : ROLES[e.operator_role] || e.operator_role;
  const bits = [who];
  if (m.incident_id) bits.push(m.incident_id);
  if (m.ticket_id) bits.push(m.idempotent_replay ? `${m.ticket_id} (returned existing)` : m.ticket_id);
  if (typeof m.row_count === "number") bits.push(`${m.row_count} rows`);
  if (typeof m.result_count === "number") bits.push(`${m.result_count} sections`);
  if (m.outbox_id) bits.push(`outbox #${m.outbox_id}`);
  if (typeof m.replayed_count === "number") bits.push(`${m.replayed_count} replayed`);
  bits.push(`${Math.round(e.latency_ms)} ms`);
  return bits.join(", ");
}

async function refreshAudit() {
  if (state.inflight.audit) return;
  state.inflight.audit = true;
  try {
    const res = await api("/api/audit?limit=60");
    const list = $("#audit-list");
    const items = res.events.map((e) => {
      const key = `${e.trace_id}|${e.tool_name}|${e.timestamp}`;
      const fresh = state.auditPrimed && !state.seenAudit.has(key);
      state.seenAudit.add(key);
      return h(
        "li",
        { "data-tone": auditTone(e), class: fresh ? "fresh" : null },
        h("span", { class: "audit-time", text: fmtClock(e.timestamp) }),
        h("span", { class: "audit-what", text: TOOL_NAMES[e.tool_name] || e.tool_name }),
        h("span", { class: "audit-status", text: STATUS_TEXT[e.status] || e.status }),
        h("span", { class: "audit-who", text: auditDetail(e) }),
      );
    });
    list.replaceChildren(...(items.length ? items : [h("li", { class: "outbox-empty", text: "No activity yet." })]));
    state.auditPrimed = true;
  } catch (_) {
    /* lamps already report the console as offline */
  } finally {
    state.inflight.audit = false;
  }
}

/* ---------- boot ---------- */

function boot() {
  initRoles();
  initTabs();
  initQuery();
  initSearch();
  initFaults();
  loadIncidents();
  refreshSystem();
  refreshAudit();
  setInterval(() => { if (!document.hidden) refreshSystem(); }, 1000);
  setInterval(() => { if (!document.hidden) refreshAudit(); }, 2000);
}

boot();
