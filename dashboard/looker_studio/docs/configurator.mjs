import { REPORT_CONFIG } from "./report-config.mjs";

export const PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
export const BIGQUERY_ID_RE = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
export const PREFIX_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,127}$/;

function requireValue(label, value, pattern) {
  const normalized = String(value ?? "").trim();
  if (!pattern.test(normalized)) {
    throw new Error(`Enter a valid BigQuery ${label}.`);
  }
  return normalized;
}

function rejectSentinelCollisions(values, config) {
  const sentinels = Object.values(config.sentinels ?? {});
  if (
    sentinels.length === 0 ||
    sentinels.some((sentinel) => typeof sentinel !== "string" || !sentinel)
  ) {
    throw new Error("The dashboard template has invalid sentinel bindings.");
  }
  for (const value of Object.values(values)) {
    if (sentinels.some((sentinel) => value.includes(sentinel))) {
      throw new Error(
        "An identifier contains a reserved dashboard template value.",
      );
    }
  }
}

export function validateConfiguration(input, config = REPORT_CONFIG) {
  const project = requireValue("project ID", input.project, PROJECT_RE);
  const values = {
    project,
    dataset: requireValue("dataset ID", input.dataset, BIGQUERY_ID_RE),
    table: requireValue("table ID", input.table, BIGQUERY_ID_RE),
    prefix: requireValue(
      "generated-view prefix",
      input.prefix || config.defaultViewPrefix,
      PREFIX_RE,
    ),
    billingProject: requireValue(
      "billing project ID",
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
    config.sentinels.viewPrefix,
    values.prefix,
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
  if (values.prefix !== REPORT_CONFIG.defaultViewPrefix) {
    params.prefix = values.prefix;
  }
  if (values.billingProject !== values.project) {
    params.billingProject = values.billingProject;
  }
  url.search = new URLSearchParams(params).toString();
  url.hash = "";
  return url.toString();
}
