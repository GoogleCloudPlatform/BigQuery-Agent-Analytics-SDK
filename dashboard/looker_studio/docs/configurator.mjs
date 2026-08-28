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
// Every table submessage starts with this marker; comparing marker starts to
// complete matches rejects workspaces holding a truncated table reference.
// The lookahead accepts a following field delimiter or end of string, so a
// workspace truncated exactly at a dangling `!4m3` still counts as a marker.
const BIGQUERY_WORKSPACE_TABLE_MARKER_RE = /!4m3(?=!|$)/g;
// Dataset views encode a `!3m2!1s<project>!2s<dataset>` resource. When one
// coexists with a table reference the active resource cannot be proved from
// the undocumented `ws` encoding, so such workspaces are rejected outright.
const BIGQUERY_WORKSPACE_DATASET_RE = /!3m2!1s[^!]+!2s[^!]+/;
// Any scheme followed by `//` is URL-shaped, and so are slashless `http:` /
// `https:` spellings, which `new URL()` canonicalizes to the `://` form
// (`https:evil.example` parses as `https://evil.example/`). Routing those
// spellings here keeps them out of the legacy `project:dataset.table` colon
// normalization; no legitimate colon-form paste can start with them because
// `PROJECT_RE` requires at least six characters.
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
});

export const BILLING_PROJECT_MESSAGE = VALIDATION_MESSAGES.billingProject;

// Whole-field errors for the combined table-ID input: input with no truthful
// project/dataset/table segments to blame (#448 decision 2). Segment-level
// errors are built from VALIDATION_MESSAGES with the segment named up front.
const TABLE_ID_MESSAGES = Object.freeze({
  empty:
    "Enter the fully qualified BQAA table ID as project.dataset.table.",
  unparseable:
    "Enter the fully qualified ID as project.dataset.table — exactly three dot-separated segments.",
  link:
    "That link doesn’t clearly name exactly one BigQuery table. Open the table itself in the BigQuery console (close other table tabs) and copy the address-bar URL again — or paste the dotted project.dataset.table ID instead.",
});

const SEGMENT_LABELS = Object.freeze({
  project: "Project",
  dataset: "Dataset",
  table: "Table",
});

export class ConfigurationError extends Error {
  // `segment` distinguishes the two #448 error classes for the combined
  // table-ID field: a segment-level error names the offending
  // project/dataset/table segment; a whole-field error carries null.
  constructor(field, message, segment = null) {
    super(message);
    this.name = "ConfigurationError";
    this.field = field;
    this.segment = segment;
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
      // Attributed to the offending segment so the single-field UI can
      // report it in the segment-level error class (#448 decision 2); the
      // collision logic itself and the Linking API output are unchanged.
      throw new ConfigurationError(
        "tableId",
        `${SEGMENT_LABELS[name]} segment: contains a later reserved dashboard template value.`,
        name,
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
  // SQL-copy punctuation is one enclosing whole-ID backtick pair plus
  // trailing statement/list punctuation. Only that pair is stripped: an
  // embedded backtick stays in its segment and fails segment validation
  // rather than being silently deleted into a different normalized ID
  // (#449 review).
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

// Structural extraction only: returns the project/dataset/table tuple of an
// unambiguous supported-host Console link without judging the identifiers,
// so validateQualifiedTableId can attribute a bad segment to its segment
// (#449 review). The public parser below stays strict.
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
  return { project, dataset, table };
}

export function parseBigQueryConsoleTableUrl(value) {
  const parsed = extractBigQueryConsoleTableReference(value);
  return hasValidTableIdentifiers(parsed) ? parsed : null;
}

export function parseTableReference(value) {
  const normalized = String(value ?? "").trim();
  if (URL_SHAPED_RE.test(normalized)) {
    return parseBigQueryConsoleTableUrl(normalized);
  }
  return splitQualifiedTableId(normalized);
}

export function parseTableReferenceForInput(value) {
  const parsed = parseTableReference(value);
  return hasValidTableIdentifiers(parsed) ? parsed : null;
}

// Validates the combined table-ID field (#448) and returns the parsed
// triple, or throws a ConfigurationError in one of the two error classes:
// whole-field (empty, unparseable, or a link that names no single table —
// `segment: null`) or segment-level (exactly three segments, one violating
// its rule or a sentinel collision — `segment` names the offender). The
// sentinel check runs here so a colliding value never reaches the Ready
// state only to fail at URL construction.
export function validateQualifiedTableId(value, config = REPORT_CONFIG) {
  const raw = String(value ?? "").trim();
  if (!raw) {
    throw new ConfigurationError("tableId", TABLE_ID_MESSAGES.empty);
  }
  // The URL branch uses the structural extractor, not the strict public
  // parser: a supported-host link that unambiguously names one table but
  // carries an invalid identifier has three identifiable segments, so it
  // must reach the segment loop below rather than collapse into the
  // whole-field link error.
  const parsed = URL_SHAPED_RE.test(raw)
    ? extractBigQueryConsoleTableReference(raw)
    : splitQualifiedTableId(raw);
  if (!parsed) {
    throw new ConfigurationError(
      "tableId",
      URL_SHAPED_RE.test(raw)
        ? TABLE_ID_MESSAGES.link
        : TABLE_ID_MESSAGES.unparseable,
    );
  }
  for (const [segment, pattern] of [
    ["project", PROJECT_RE],
    ["dataset", DATASET_RE],
    ["table", TABLE_RE],
  ]) {
    if (!pattern.test(parsed[segment])) {
      throw new ConfigurationError(
        "tableId",
        `${SEGMENT_LABELS[segment]} segment: ${VALIDATION_MESSAGES[segment]}`,
        segment,
      );
    }
  }
  rejectSentinelCollisions(parsed, config);
  return Object.freeze({ ...parsed });
}
