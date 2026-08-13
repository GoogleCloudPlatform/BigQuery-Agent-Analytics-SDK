import { REPORT_CONFIG } from "./report-config.mjs";

export const PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
export const DATASET_RE = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
export const TABLE_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$/;

const BIGQUERY_CONSOLE_HOSTS = new Set([
  "console.cloud.google.com",
  "pantheon.corp.google.com",
]);
// The `!4m3!1s<project>!2s<dataset>!3s<table>` submessage is the stable core
// of a Console table reference. The group counts that precede it (for example
// `!1m5!1m4` or `!1m6!1m5`) vary with UI-state fields the Console appends,
// such as `!23sRESOURCE_LIST`, so they must not be part of the contract.
const BIGQUERY_WORKSPACE_TABLE_RE = /!4m3!1s([^!]+)!2s([^!]+)!3s([^!]+)/g;
const ABSOLUTE_URL_RE = /^[a-z][a-z0-9+.-]*:\/\//i;

const VALIDATION_MESSAGES = Object.freeze({
  project:
    "Use 6–30 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit.",
  dataset:
    "Start with a letter or underscore, then use only letters, digits, or underscores.",
  table:
    "Start with a letter, digit, or underscore, then use only letters, digits, underscores, or hyphens.",
  billingProject:
    "Use 6–30 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit.",
});

export class ConfigurationError extends Error {
  constructor(field, message) {
    super(message);
    this.name = "ConfigurationError";
    this.field = field;
  }
}

function requireValue(field, value, pattern) {
  const normalized = String(value ?? "").trim();
  if (!pattern.test(normalized)) {
    throw new ConfigurationError(field, VALIDATION_MESSAGES[field]);
  }
  return normalized;
}

function rejectSentinelCollisions(values, config) {
  const order = ["project", "dataset", "table"];
  const sentinels = order.map((name) => config.sentinels?.[name]);
  if (
    sentinels.some((sentinel) => typeof sentinel !== "string" || !sentinel) ||
    new Set(sentinels).size !== sentinels.length
  ) {
    throw new Error("The dashboard template has invalid sentinel bindings.");
  }
  for (const [index, name] of order.entries()) {
    const value = values[name];
    if (sentinels.slice(index + 1).some((sentinel) => value.includes(sentinel))) {
      throw new Error(
        `The ${name} contains a later reserved dashboard template value.`,
      );
    }
  }
}

export function validateConfiguration(input, config = REPORT_CONFIG) {
  const project = requireValue("project", input.project, PROJECT_RE);
  const values = {
    project,
    dataset: requireValue("dataset", input.dataset, DATASET_RE),
    table: requireValue("table", input.table, TABLE_RE),
    billingProject: requireValue(
      "billingProject",
      input.billingProject || project,
      PROJECT_RE,
    ),
  };
  rejectSentinelCollisions(values, config);
  return Object.freeze(values);
}

export function buildDashboardUrl(input, config = REPORT_CONFIG) {
  const values = validateConfiguration(input, config);
  const alias = config.dataSourceAlias;
  const replacements = [
    config.sentinels.project,
    values.project,
    config.sentinels.dataset,
    values.dataset,
    config.sentinels.table,
    values.table,
  ];
  const params = new URLSearchParams({
    "c.reportId": config.reportId,
    "c.mode": "view",
    "r.reportName": `BigQuery Agent Analytics — ${values.dataset}.${values.table}`,
    [`ds.${alias}.datasourceName`]:
      `BQAA — ${values.project}.${values.dataset}.${values.table}`,
    [`ds.${alias}.billingProjectId`]: values.billingProject,
    [`ds.${alias}.sqlReplace`]: replacements.join(","),
    [`ds.${alias}.refreshFields`]: "false",
  });
  return `https://lookerstudio.google.com/reporting/create?${params.toString()}`;
}

export function buildSetupUrl(input, pageUrl) {
  const values = validateConfiguration(input);
  const url = new URL(pageUrl);
  const params = {
    project: values.project,
    dataset: values.dataset,
    table: values.table,
  };
  if (values.billingProject !== values.project) {
    params.billingProject = values.billingProject;
  }
  url.search = new URLSearchParams(params).toString();
  url.hash = "";
  return url.toString();
}

export function splitQualifiedTableId(value) {
  const normalized = String(value ?? "")
  .trim()
  .replace(/`/g, "")
  .replace(/^([^.:]+):/, "$1.")
  .replace(/[;,]+$/, "");

  const parts = normalized.split(".");
  if (parts.length !== 3 || parts.some((part) => part.length === 0)) {
    return null;
  }

  const [project, dataset, table] = parts;
  return { project, dataset, table };
}

export function parseQualifiedTableIdForInput(value) {
  const parsed = splitQualifiedTableId(value);

  return hasValidTableIdentifiers(parsed) ? parsed : null;
}

function hasValidTableIdentifiers(parsed) {
  if (!parsed) {
    return false;
  }

  return (
    PROJECT_RE.test(parsed.project) &&
    DATASET_RE.test(parsed.dataset) &&
    TABLE_RE.test(parsed.table)
  );
}

export function parseBigQueryConsoleTableUrl(value) {
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

  const matches = [
    ...workspaceValues[0].matchAll(BIGQUERY_WORKSPACE_TABLE_RE),
  ];
  if (matches.length === 0) {
    return null;
  }

  // A workspace URL can reference the same table more than once (for example
  // one entry per open tab). That is still unambiguous, so collapse the
  // matches and only reject when they name different tables.
  const distinctReferences = new Set(
    matches.map(([, project, dataset, table]) =>
      [project, dataset, table].join("!"),
    ),
  );
  if (distinctReferences.size !== 1) {
    return null;
  }

  const [, project, dataset, table] = matches[0];
  const parsed = { project, dataset, table };
  return hasValidTableIdentifiers(parsed) ? parsed : null;
}

export function parseTableReference(value) {
  const normalized = String(value ?? "").trim();
  if (ABSOLUTE_URL_RE.test(normalized)) {
    return parseBigQueryConsoleTableUrl(normalized);
  }
  return splitQualifiedTableId(normalized);
}

export function parseTableReferenceForInput(value) {
  const parsed = parseTableReference(value);
  return hasValidTableIdentifiers(parsed) ? parsed : null;
}
