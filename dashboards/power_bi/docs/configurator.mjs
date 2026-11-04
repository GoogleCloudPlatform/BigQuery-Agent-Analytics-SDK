// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

export const PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
export const DATASET_RE = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
export const TABLE_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$/;

export const DEFAULT_DEMO_TABLE = Object.freeze({
  project: "test-project-0728-467323",
  dataset: "bqaa_fixture_adk_1_27_0",
  table: "agent_events",
  billingProject: "test-project-0728-467323",
  windowEndDate: "auto",
  lookbackDays: 90,
  useStorageApi: true,
});

export const PREVIEW_PAGES = Object.freeze([
  {
    id: "token-consumption",
    title: "1. Token Consumption",
    caption:
      "Total Tokens scorecard, Token Consumption Over Time, Top 5 Users by Token Consumption, and Token Usage by Agent.",
    screenshot: "../screenshots/01-token-consumption.png",
  },
  {
    id: "agent-and-sessions",
    title: "2. Agent & Sessions",
    caption:
      "Total Traces, Trace Volume Over Time, Top 5 Users by Traces, Traces by Agent, Total Sessions, Session Volume Over Time, and Top 5 Agents by Sessions.",
    screenshot: "../screenshots/02-agent-and-sessions.png",
  },
  {
    id: "tool-usage",
    title: "3. Tool Usage",
    caption:
      "Tool Calls Over Time stacked by tool name, Tool Invocations (Top 10), and Tool Completions by Agent (Top 10).",
    screenshot: "../screenshots/03-tool-usage.png",
  },
  {
    id: "llm-interactions",
    title: "4. LLM Interactions",
    caption:
      "Total LLM Calls scorecard, LLM Call Volume Over Time, and Top 5 Agents by LLM Calls.",
    screenshot: "../screenshots/04-llm-interactions.png",
  },
  {
    id: "user-analytics",
    title: "5. User Analytics",
    caption:
      "Total Users scorecard, User Growth Over Time, Top 5 Users by Sessions, Top 5 Users by Events, and navigation to Latency.",
    screenshot: "../screenshots/05-user-analytics.png",
  },
  {
    id: "latency",
    title: "6. Latency",
    caption:
      "Average Tool & LLM Latency (ms), Tool & LLM Latency Over Time, and P50 / P75 / P90 / P99 percentile scorecards.",
    screenshot: "../screenshots/06-latency.png",
  },
  {
    id: "errors",
    title: "7. Errors",
    caption:
      "Tool Errors scorecard, Tool Errors Over Time, Top 5 Agents by Tool Errors, and Top 5 Tools by Errors.",
    screenshot: "../screenshots/07-errors.png",
  },
  {
    id: "trace-inspector",
    title: "8. Trace Inspector",
    caption:
      "Event-level inspection table (timestamp, event_type, agent, user_id, trace_id, span_id, status) sorted newest first.",
    screenshot: "../screenshots/08-trace-inspector.png",
  },
]);

const BIGQUERY_CONSOLE_HOSTS = new Set([
  "console.cloud.google.com",
  "pantheon.corp.google.com",
]);
const BIGQUERY_WORKSPACE_TABLE_RE = /!4m3!1s([^!]+)!2s([^!]+)!3s([^!]+)/g;
const BIGQUERY_WORKSPACE_TABLE_MARKER_RE = /!4m3(?=!|$)/g;
const BIGQUERY_WORKSPACE_DATASET_RE = /!3m2!1s[^!]+!2s[^!]+/;
const URL_SHAPED_RE = /^(?:[a-z][a-z0-9+.-]*:\/\/|https?:)/i;

const VALIDATION_MESSAGES = Object.freeze({
  project:
    "Use 6–30 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit.",
  dataset:
    "Start with a letter or underscore, then use only letters, digits, or underscores.",
  table:
    "Start with a letter, digit, or underscore, then use only letters, digits, underscores, or hyphens.",
  billingProject:
    "Use 6–30 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit.",
  windowEndDate:
    "Enter 'auto' or a valid calendar date in YYYY-MM-DD format.",
  lookbackDays:
    "Enter a whole number of days between 1 and 366.",
});

const SEGMENT_LABELS = Object.freeze({
  project: "Project",
  dataset: "Dataset",
  table: "Table",
});

export class ConfigurationError extends Error {
  constructor(field, message, segment = null) {
    super(message);
    this.name = "ConfigurationError";
    this.field = field;
    this.segment = segment;
  }
}

export function splitQualifiedTableId(value) {
  const normalized = String(value ?? "")
    .trim()
    .replace(/[;,]+$/, "")
    .replace(/^`(.*)`$/s, "$1")
    .replace(/^([^.:]+):/, "$1.");

  const parts = normalized.split(".");
  if (parts.length !== 3 || parts.some((part) => part.length === 0)) {
    return null;
  }
  const [project, dataset, table] = parts;
  return { project, dataset, table };
}

function extractBigQueryConsoleTableReference(value) {
  let url;
  try {
    url = new URL(String(value ?? "").trim());
  } catch {
    return null;
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.port ||
    !BIGQUERY_CONSOLE_HOSTS.has(url.hostname) ||
    url.pathname !== "/bigquery"
  ) {
    return null;
  }
  const workspaceValues = url.searchParams.getAll("ws");
  if (workspaceValues.length !== 1) {
    return null;
  }
  const workspace = workspaceValues[0];
  const matches = [...workspace.matchAll(BIGQUERY_WORKSPACE_TABLE_RE)];
  if (matches.length === 0) {
    return null;
  }
  const markerStarts = workspace.match(BIGQUERY_WORKSPACE_TABLE_MARKER_RE);
  if ((markerStarts?.length ?? 0) !== matches.length) {
    return null;
  }
  if (BIGQUERY_WORKSPACE_DATASET_RE.test(workspace)) {
    return null;
  }
  const tables = new Map();
  for (const [, project, dataset, table] of matches) {
    tables.set(`${project}.${dataset}.${table}`, { project, dataset, table });
  }
  if (tables.size !== 1) {
    return null;
  }
  return tables.values().next().value;
}

function validateParsedTableSegments(parsed) {
  const rules = [
    ["project", PROJECT_RE],
    ["dataset", DATASET_RE],
    ["table", TABLE_RE],
  ];
  for (const [segment, pattern] of rules) {
    if (!pattern.test(parsed[segment])) {
      throw new ConfigurationError(
        "tableId",
        `${SEGMENT_LABELS[segment]} segment: ${VALIDATION_MESSAGES[segment]}`,
        segment,
      );
    }
  }
  return Object.freeze({
    project: parsed.project,
    dataset: parsed.dataset,
    table: parsed.table,
  });
}

export function validateQualifiedTableId(value) {
  const trimmed = String(value ?? "").trim();
  if (!trimmed) {
    throw new ConfigurationError(
      "tableId",
      "Enter the fully qualified BQAA table ID as project.dataset.table.",
    );
  }
  if (URL_SHAPED_RE.test(trimmed)) {
    const parsed = extractBigQueryConsoleTableReference(trimmed);
    if (!parsed) {
      throw new ConfigurationError(
        "tableId",
        "That link does not clearly name a single BigQuery table. Paste project.dataset.table instead.",
      );
    }
    return validateParsedTableSegments(parsed);
  }
  const parsed = splitQualifiedTableId(trimmed);
  if (!parsed) {
    throw new ConfigurationError(
      "tableId",
      "Enter the fully qualified ID as project.dataset.table — exactly three dot-separated segments.",
    );
  }
  return validateParsedTableSegments(parsed);
}

export function validateWindowEndDate(value) {
  const raw = String(value ?? "auto").trim();
  if (!raw || raw.toLowerCase() === "auto") {
    return "auto";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    throw new ConfigurationError(
      "windowEndDate",
      VALIDATION_MESSAGES.windowEndDate,
    );
  }
  const [y, m, d] = raw.split("-").map((part) => Number(part));
  const dt = new Date(Date.UTC(y, m - 1, d));
  if (
    dt.getUTCFullYear() !== y ||
    dt.getUTCMonth() + 1 !== m ||
    dt.getUTCDate() !== d
  ) {
    throw new ConfigurationError(
      "windowEndDate",
      VALIDATION_MESSAGES.windowEndDate,
    );
  }
  return raw;
}

export function validateLookbackDays(value) {
  if (value === undefined || value === null || String(value).trim() === "") {
    return 90;
  }
  const num = Number(String(value).trim());
  if (!Number.isInteger(num) || num < 1 || num > 366) {
    throw new ConfigurationError(
      "lookbackDays",
      VALIDATION_MESSAGES.lookbackDays,
    );
  }
  return num;
}

export function validatePowerBIConfig(input) {
  let project = String(input?.project ?? "").trim();
  let dataset = String(input?.dataset ?? "").trim();
  let table = String(input?.table ?? "").trim();

  if (input?.tableId !== undefined && (!project || !dataset || !table)) {
    const parsed = validateQualifiedTableId(input.tableId);
    project = parsed.project;
    dataset = parsed.dataset;
    table = parsed.table;
  } else {
    validateParsedTableSegments({ project, dataset, table });
  }

  const billingRaw = String(input?.billingProject ?? "").trim() || project;
  if (!PROJECT_RE.test(billingRaw)) {
    throw new ConfigurationError(
      "billingProject",
      VALIDATION_MESSAGES.billingProject,
    );
  }

  const windowEndDate = validateWindowEndDate(input?.windowEndDate ?? "auto");
  const lookbackDays = validateLookbackDays(input?.lookbackDays ?? 90);
  const useStorageApi =
    input?.useStorageApi === undefined
      ? true
      : Boolean(
          input.useStorageApi === true ||
            String(input.useStorageApi).toLowerCase() === "true",
        );

  return Object.freeze({
    project,
    dataset,
    table,
    billingProject: billingRaw,
    windowEndDate,
    lookbackDays,
    useStorageApi,
  });
}

export function rewriteExpressionsTmdl(exprText, config) {
  const valid = validatePowerBIConfig(config);
  const replacements = [
    ["BQAA_ProjectId", `"${valid.project}"`],
    ["BQAA_DatasetId", `"${valid.dataset}"`],
    ["BQAA_TableId", `"${valid.table}"`],
    ["BQAA_BillingProjectId", `"${valid.billingProject}"`],
    ["BQAA_WindowEndDate", `"${valid.windowEndDate}"`],
    ["BQAA_LookbackDays", String(valid.lookbackDays)],
    ["BQAA_UseStorageApi", valid.useStorageApi ? "true" : "false"],
  ];

  let updated = String(exprText);
  for (const [paramName, literalVal] of replacements) {
    // Count every declaration first: a missing or duplicated one means the
    // bundled source is malformed, and rewriting only the first match would
    // ship a project whose second declaration keeps a stale default.
    const re = new RegExp(
      `^(expression ${paramName} = ).*?( meta \\[IsParameterQuery=true, .*\\])$`,
      "gm",
    );
    const count = Array.from(updated.matchAll(re)).length;
    if (count !== 1) {
      throw new Error(
        `Expected exactly 1 declaration for ${paramName} in expressions.tmdl, found ${count}`,
      );
    }
    updated = updated.replace(
      re,
      (_match, head, tail) => `${head}${literalVal}${tail}`,
    );
  }
  return updated;
}

export function buildCustomPbipBundle(bundle, config) {
  if (!bundle || typeof bundle !== "object") {
    throw new Error("PBIP bundle is not loaded.");
  }
  const valid = validatePowerBIConfig(config);
  const exprKey =
    "BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl";
  if (!(exprKey in bundle)) {
    throw new Error(`Missing ${exprKey} in PBIP bundle.`);
  }
  const custom = { ...bundle };
  custom[exprKey] = rewriteExpressionsTmdl(bundle[exprKey], valid);
  return custom;
}

// IEEE 802.3 CRC-32 lookup table.
const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let c = i;
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[i] = c >>> 0;
  }
  return table;
})();

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    crc = CRC32_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

// Pure-JS PKZIP archive builder (STORE method 0, deterministic 2026-01-01 timestamp).
export function buildPkZip(fileMap, rootPrefix = "BQAA_Agent_Analytics") {
  const encoder = new TextEncoder();
  const keys = Object.keys(fileMap).sort();
  const localParts = [];
  const centralParts = [];
  let offset = 0;

  // DOS date for 2026-01-01 00:00:00: ((2026 - 1980) << 9) | (1 << 5) | 1 = 0x5c21
  const dosTime = 0x0000;
  const dosDate = 0x5c21;
  const utf8Flag = 0x0800;

  for (const relPath of keys) {
    const cleanRel = relPath.replace(/^\/+/, "");
    const fullName = rootPrefix ? `${rootPrefix}/${cleanRel}` : cleanRel;
    const nameBytes = encoder.encode(fullName);
    const normalizedText = String(fileMap[relPath])
      .replace(/\r\n/g, "\n")
      .replace(/\r/g, "\n");
    const dataBytes = encoder.encode(normalizedText);
    const crc = crc32(dataBytes);
    const size = dataBytes.length;

    const localHeader = new Uint8Array(30 + nameBytes.length);
    const lv = new DataView(localHeader.buffer);
    lv.setUint32(0, 0x04034b50, true); // Local file header signature
    lv.setUint16(4, 20, true); // Version needed to extract (2.0)
    lv.setUint16(6, utf8Flag, true); // General purpose bit flag (UTF-8)
    lv.setUint16(8, 0, true); // Compression method (0 = STORE)
    lv.setUint16(10, dosTime, true);
    lv.setUint16(12, dosDate, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, size, true);
    lv.setUint32(22, size, true);
    lv.setUint16(26, nameBytes.length, true);
    lv.setUint16(28, 0, true);
    localHeader.set(nameBytes, 30);

    localParts.push(localHeader, dataBytes);

    const centralHeader = new Uint8Array(46 + nameBytes.length);
    const cv = new DataView(centralHeader.buffer);
    cv.setUint32(0, 0x02014b50, true); // Central directory file header signature
    cv.setUint16(4, 20, true); // Version made by
    cv.setUint16(6, 20, true); // Version needed to extract
    cv.setUint16(8, utf8Flag, true);
    cv.setUint16(10, 0, true); // STORE
    cv.setUint16(12, dosTime, true);
    cv.setUint16(14, dosDate, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, size, true);
    cv.setUint32(24, size, true);
    cv.setUint16(28, nameBytes.length, true);
    cv.setUint16(30, 0, true);
    cv.setUint16(32, 0, true);
    cv.setUint16(34, 0, true);
    cv.setUint16(36, 0, true);
    cv.setUint32(38, (0o644 << 16) >>> 0, true);
    cv.setUint32(42, offset, true);
    centralHeader.set(nameBytes, 46);

    centralParts.push(centralHeader);
    offset += localHeader.length + dataBytes.length;
  }

  let centralSize = 0;
  for (const part of centralParts) {
    centralSize += part.length;
  }

  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true); // End of central directory signature
  ev.setUint16(4, 0, true);
  ev.setUint16(6, 0, true);
  ev.setUint16(8, keys.length, true);
  ev.setUint16(10, keys.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);
  ev.setUint16(20, 0, true);

  const totalSize = offset + centralSize + eocd.length;
  const out = new Uint8Array(totalSize);
  let pos = 0;
  for (const part of localParts) {
    out.set(part, pos);
    pos += part.length;
  }
  for (const part of centralParts) {
    out.set(part, pos);
    pos += part.length;
  }
  out.set(eocd, pos);
  return out;
}

export function buildSetupUrl(input, pageUrl) {
  const valid = validatePowerBIConfig(input);
  const url = new URL(pageUrl);
  const params = new URLSearchParams({
    project: valid.project,
    dataset: valid.dataset,
    table: valid.table,
  });
  if (valid.billingProject !== valid.project) {
    params.set("billingProject", valid.billingProject);
  }
  if (valid.windowEndDate !== "auto") {
    params.set("windowEndDate", valid.windowEndDate);
  }
  if (valid.lookbackDays !== 90) {
    params.set("lookbackDays", String(valid.lookbackDays));
  }
  if (!valid.useStorageApi) {
    params.set("useStorageApi", "false");
  }
  // The configuration travels in the URL fragment: browsers never send the
  // fragment to the server, so a shared setup link keeps the project, dataset,
  // and table IDs on the client.  The query string is cleared for new links.
  url.search = "";
  url.hash = params.toString();
  return url.toString();
}

export function buildCliZipCommand(input) {
  const valid = validatePowerBIConfig(input);
  const args = [
    "python dashboards/power_bi/tools/generate_pbip.py",
    `--output-zip BQAA_Agent_Analytics_${valid.project}_${valid.dataset}_${valid.table}.zip`,
    `--project ${valid.project}`,
    `--dataset ${valid.dataset}`,
    `--table ${valid.table}`,
  ];
  if (valid.billingProject !== valid.project) {
    args.push(`--billing-project ${valid.billingProject}`);
  }
  if (valid.windowEndDate !== "auto") {
    args.push(`--window-end ${valid.windowEndDate}`);
  }
  if (valid.lookbackDays !== 90) {
    args.push(`--lookback-days ${valid.lookbackDays}`);
  }
  if (!valid.useStorageApi) {
    args.push("--use-storage-api false");
  }
  return args.join(" ");
}

export function buildFabricDeployCommand(input) {
  validatePowerBIConfig(input);
  return [
    "python dashboards/power_bi/tools/deploy_fabric.py",
    "--workspace-id <WORKSPACE_GUID>",
    "--token-file /path/to/fabric.token",
    "--project-dir ./BQAA_Agent_Analytics",
    '--display-name "BQAA Agent Analytics"',
  ].join(" ");
}

export function initPowerBIConfigurator(doc = globalThis.document) {
  if (!doc) {
    return;
  }
  const form = doc.getElementById("configurator");
  if (!form) {
    return;
  }
  const tableInput = doc.getElementById("table-id");
  const billingInput = doc.getElementById("billing-project");
  const windowEndInput = doc.getElementById("window-end-date");
  const lookbackInput = doc.getElementById("lookback-days");
  const storageApiInput = doc.getElementById("use-storage-api");
  const demoPresetBtn = doc.getElementById("use-demo-fixture");
  const downloadBtn = doc.getElementById("download-pbip");
  const copyLinkBtn = doc.getElementById("copy-link");
  const statusEl = doc.getElementById("form-status");
  const cliZipCode = doc.getElementById("cli-zip-cmd");
  const fabricDeployCode = doc.getElementById("fabric-deploy-cmd");

  const fieldErrors = {
    tableId: doc.getElementById("table-id-error"),
    billingProject: doc.getElementById("billing-project-error"),
    windowEndDate: doc.getElementById("window-end-date-error"),
    lookbackDays: doc.getElementById("lookback-days-error"),
  };
  const fieldInputs = {
    tableId: tableInput,
    billingProject: billingInput,
    windowEndDate: windowEndInput,
    lookbackDays: lookbackInput,
  };

  function clearErrors() {
    for (const [key, errEl] of Object.entries(fieldErrors)) {
      if (errEl) {
        errEl.textContent = "";
      }
      if (fieldInputs[key]) {
        fieldInputs[key].removeAttribute("aria-invalid");
      }
    }
  }

  function currentRawInput() {
    return {
      tableId: tableInput?.value ?? "",
      billingProject: billingInput?.value ?? "",
      windowEndDate: windowEndInput?.value || "auto",
      lookbackDays: lookbackInput?.value || 90,
      useStorageApi: storageApiInput ? storageApiInput.checked : true,
    };
  }

  function refreshState() {
    clearErrors();
    const raw = currentRawInput();
    if (!String(raw.tableId).trim()) {
      if (downloadBtn) {
        downloadBtn.disabled = true;
      }
      if (statusEl) {
        statusEl.dataset.kind = "";
        statusEl.textContent =
          "Enter your project.dataset.table ID or click 'Use public demo fixture' above.";
      }
      return null;
    }
    try {
      const valid = validatePowerBIConfig(raw);
      if (downloadBtn) {
        downloadBtn.disabled = false;
      }
      if (statusEl) {
        statusEl.dataset.kind = "ready";
        statusEl.textContent = `Ready to package BQAA_Agent_Analytics_${valid.project}_${valid.dataset}_${valid.table}.zip (118 files).`;
      }
      if (cliZipCode) {
        cliZipCode.textContent = buildCliZipCommand(valid);
      }
      if (fabricDeployCode) {
        fabricDeployCode.textContent = buildFabricDeployCommand(valid);
      }
      return valid;
    } catch (err) {
      if (downloadBtn) {
        downloadBtn.disabled = true;
      }
      const field = err instanceof ConfigurationError ? err.field : "tableId";
      if (fieldErrors[field]) {
        fieldErrors[field].textContent = err.message;
      }
      if (fieldInputs[field]) {
        fieldInputs[field].setAttribute("aria-invalid", "true");
      }
      if (statusEl) {
        statusEl.dataset.kind = "error";
        statusEl.textContent = err.message;
      }
      return null;
    }
  }

  // Hydrate from the URL fragment (a setup link) if present.  Only the
  // fragment is read: the query string is sent to the host, so setup links
  // never carry configuration there.
  try {
    const params = new URLSearchParams(
      String(globalThis.location?.hash ?? "").replace(/^#/, ""),
    );
    const p = params.get("project");
    const d = params.get("dataset");
    const t = params.get("table");
    if (p && d && t && tableInput) {
      tableInput.value = `${p}.${d}.${t}`;
    } else if (params.get("tableId") && tableInput) {
      tableInput.value = params.get("tableId");
    }
    if (params.get("billingProject") && billingInput) {
      billingInput.value = params.get("billingProject");
    }
    if (params.get("windowEndDate") && windowEndInput) {
      windowEndInput.value = params.get("windowEndDate");
    }
    if (params.get("lookbackDays") && lookbackInput) {
      lookbackInput.value = params.get("lookbackDays");
    }
    if (params.get("useStorageApi") === "false" && storageApiInput) {
      storageApiInput.checked = false;
    }
  } catch {
    // Ignore a malformed fragment.
  }

  for (const el of [
    tableInput,
    billingInput,
    windowEndInput,
    lookbackInput,
    storageApiInput,
  ]) {
    if (el) {
      el.addEventListener("input", refreshState);
      el.addEventListener("change", refreshState);
    }
  }

  if (demoPresetBtn && tableInput) {
    demoPresetBtn.addEventListener("click", () => {
      tableInput.value = `${DEFAULT_DEMO_TABLE.project}.${DEFAULT_DEMO_TABLE.dataset}.${DEFAULT_DEMO_TABLE.table}`;
      if (billingInput) {
        billingInput.value = "";
      }
      if (windowEndInput) {
        windowEndInput.value = "auto";
      }
      if (lookbackInput) {
        lookbackInput.value = "90";
      }
      if (storageApiInput) {
        storageApiInput.checked = true;
      }
      refreshState();
    });
  }

  if (downloadBtn) {
    downloadBtn.addEventListener("click", () => {
      const valid = refreshState();
      if (!valid) {
        return;
      }
      try {
        const bundle = globalThis.window?.BQAA_PBIP_BUNDLE;
        const customFiles = buildCustomPbipBundle(bundle, valid);
        const zipBytes = buildPkZip(customFiles, "BQAA_Agent_Analytics");
        const blob = new Blob([zipBytes], { type: "application/zip" });
        const blobUrl = URL.createObjectURL(blob);
        const a = doc.createElement("a");
        a.href = blobUrl;
        a.download = `BQAA_Agent_Analytics_${valid.project}_${valid.dataset}_${valid.table}.zip`;
        doc.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 2000);
        if (statusEl) {
          statusEl.dataset.kind = "ready";
          statusEl.textContent = `Downloaded ${a.download} (${Object.keys(customFiles).length} files).`;
        }
      } catch (err) {
        if (statusEl) {
          statusEl.dataset.kind = "error";
          statusEl.textContent = err.message;
        }
      }
    });
  }

  if (copyLinkBtn) {
    copyLinkBtn.addEventListener("click", async () => {
      const valid = refreshState();
      if (!valid) {
        return;
      }
      const setupUrl = buildSetupUrl(valid, globalThis.location.href);
      if (globalThis.navigator?.clipboard?.writeText) {
        await globalThis.navigator.clipboard.writeText(setupUrl);
        if (statusEl) {
          statusEl.dataset.kind = "ready";
          statusEl.textContent =
            "Copied shareable Power BI setup link to clipboard. Its settings sit after the # in the address, which browsers never send to a server.";
        }
      }
    });
  }

  // Interactive 8-page preview tabs.
  const tabContainer = doc.getElementById("preview-page-tabs");
  const previewImg = doc.getElementById("preview-screenshot");
  const previewCaption = doc.getElementById("preview-caption");
  if (tabContainer && previewImg && previewCaption) {
    PREVIEW_PAGES.forEach((page, idx) => {
      const btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "page-tab";
      btn.role = "tab";
      btn.setAttribute("aria-selected", idx === 0 ? "true" : "false");
      btn.dataset.pageId = page.id;
      btn.textContent = page.title;
      btn.addEventListener("click", () => {
        for (const child of tabContainer.querySelectorAll(".page-tab")) {
          child.setAttribute("aria-selected", "false");
        }
        btn.setAttribute("aria-selected", "true");
        previewImg.src = page.screenshot;
        previewImg.alt = `${page.title} — Power BI dashboard preview`;
        previewCaption.textContent = page.caption;
      });
      tabContainer.appendChild(btn);
    });
  }

  refreshState();
}

if (typeof globalThis.window !== "undefined") {
  globalThis.window.BQAAPowerBIConfigurator = Object.freeze({
    PROJECT_RE,
    DATASET_RE,
    TABLE_RE,
    DEFAULT_DEMO_TABLE,
    PREVIEW_PAGES,
    ConfigurationError,
    splitQualifiedTableId,
    validateQualifiedTableId,
    validateWindowEndDate,
    validateLookbackDays,
    validatePowerBIConfig,
    rewriteExpressionsTmdl,
    buildCustomPbipBundle,
    crc32,
    buildPkZip,
    buildSetupUrl,
    buildCliZipCommand,
    buildFabricDeployCommand,
    initPowerBIConfigurator,
  });
  if (globalThis.document?.readyState === "loading") {
    globalThis.document.addEventListener("DOMContentLoaded", () =>
      initPowerBIConfigurator(),
    );
  } else {
    initPowerBIConfigurator();
  }
}
