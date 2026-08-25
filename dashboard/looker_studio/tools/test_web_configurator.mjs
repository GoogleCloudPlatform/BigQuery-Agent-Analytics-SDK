import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import {
  buildDashboardUrl,
  buildSetupUrl,
  parseBigQueryConsoleTableUrl,
  parseQualifiedTableIdForInput,
  parseTableReference,
  parseTableReferenceForInput,
  splitQualifiedTableId,
  validateConfiguration,
} from "../docs/configurator.mjs";
import { REPORT_CONFIG } from "../docs/report-config.mjs";

const pageSource = readFileSync(
  new URL("../docs/index.html", import.meta.url),
  "utf8",
);

const docsDir = new URL("../docs/", import.meta.url);

for (const file of readdirSync(docsDir)) {
  if (!file.endsWith(".mjs")) {
    continue;
  }

  const source = readFileSync(
    new URL(file, docsDir),
    "utf8",
  );

  for (const match of source.matchAll(/from\s+["']([^"']+)["']/g)) {
    assert.match(
      match[1],
      /^\.\//,
      `${file} imports "${match[1]}", expected a browser-safe relative import.`,
    );
  }
}

assert.doesNotMatch(pageSource, /github\.com\/caohy1988/);
assert.match(
  pageSource,
  /https:\/\/github\.com\/GoogleCloudPlatform\/BigQuery-Agent-Analytics-SDK/,
);
assert.match(
  pageSource,
  /https:\/\/googlecloudplatform\.github\.io\/BigQuery-Agent-Analytics-SDK\//,
);
assert.match(
  pageSource,
  /Designed for desktop screens at least 1280 px wide/,
);
assert.match(pageSource, /allow up to 90 seconds/);

// Trust-cluster contract (#398/#399/#400): the wait expectation is set
// before the click, the acknowledgement dialog is explained with a link to
// the exact SQL, and the palette carries no legacy teal.
assert.match(pageSource, /aria-describedby="create-wait-note"/);
assert.match(pageSource, /id="create-wait-note"/);
assert.match(pageSource, /lookerstudio\.google\.com/);
assert.match(pageSource, /sql\/events_v1\.template\.sql/);
assert.match(pageSource, /class="notice notice-warning"/);
assert.doesNotMatch(pageSource, /#096b5a/);

// #398 requires the transient-error warning both at the button AND in
// step 02, and the stated duration must be the same everywhere it appears.
const dontClose = pageSource.match(/don’t close it/g) ?? [];
assert.ok(
  dontClose.length >= 2,
  "the do-not-close warning must appear at the button and in step 02",
);
assert.match(
  pageSource,
  /provisions your copy — don’t close it\./,
  "step 02 must repeat the transient-error warning before the dialog",
);
assert.match(
  pageSource,
  /Looker Studio then\s+shows an acknowledgement dialog/,
  "step 02 must still explain the acknowledgement dialog",
);

// #445: the terminal "This report isn’t shared with you" denial must be
// distinguished from the transient flicker everywhere the flicker is
// described, and explained once with a report-it link — never folded into
// the wait-it-out guidance.
assert.match(pageSource, /id="report-not-shared"/);
const terminalDialogRefs =
  pageSource.match(/href="#report-not-shared"/g) ?? [];
assert.ok(
  terminalDialogRefs.length >= 2,
  "both wait-it-out notes must point at the terminal-dialog explainer",
);
const dialogQuotes =
  pageSource.match(/This report isn’t shared with you/g) ?? [];
assert.ok(
  dialogQuotes.length >= 3,
  "the dialog must be quoted at the button, in step 02, and in the explainer",
);
assert.match(
  pageSource,
  /issues\/445/,
  "the explainer must link the tracking issue for reporting regressions",
);

const appSource = readFileSync(
  new URL("../docs/app.mjs", import.meta.url),
  "utf8",
);
const waitingDuration = appSource.match(/up to (~\d+ seconds)/)?.[1];
const noteDuration = pageSource.match(/up to\s+(~\d+ seconds)/s)?.[1];
assert.ok(waitingDuration, "WAITING_MESSAGE must state a duration");
assert.equal(
  noteDuration,
  waitingDuration,
  "the static wait note and WAITING_MESSAGE must state the same duration",
);

const stylesSource = readFileSync(
  new URL("../docs/styles.css", import.meta.url),
  "utf8",
);
assert.match(stylesSource, /--action: #1967d2/);
assert.match(stylesSource, /--focus: #1a73e8/);
assert.match(stylesSource, /\.notice-warning/);
assert.match(stylesSource, /@media \(prefers-color-scheme: dark\)/);
assert.doesNotMatch(stylesSource, /#096b5a|#7dd3c7|#a6e4dc/);

const values = {
  project: "customer-project-123",
  dataset: "agent_analytics",
  table: "agent_events",
  billingProject: "customer-project-123",
};
assert.deepEqual(validateConfiguration(values), values);

const hyphenatedTable = {
  ...values,
  table: "events_agent_cur-phenix",
};
assert.deepEqual(validateConfiguration(hyphenatedTable), hyphenatedTable);

const qualifiedTableId = {
  project: "my-project",
  dataset: "my_dataset",
  table: "my_table",
};

assert.deepEqual(
  splitQualifiedTableId("my-project.my_dataset.my_table"),
  qualifiedTableId,
);

assert.deepEqual(
  splitQualifiedTableId("`my-project.my_dataset.my_table`"),
  qualifiedTableId,
);

assert.deepEqual(
  splitQualifiedTableId("my-project:my_dataset.my_table"),
  qualifiedTableId,
);

assert.deepEqual(
  splitQualifiedTableId("my-project.my_dataset.my_table;"),
  qualifiedTableId,
);

assert.deepEqual(
  splitQualifiedTableId("my-project.my_dataset.my_table,"),
  qualifiedTableId,
);

assert.equal(
  splitQualifiedTableId("a.b.c.d"),
  null,
);

assert.equal(
  splitQualifiedTableId("my-project.my_dataset."),
  null,
);

assert.equal(
  parseQualifiedTableIdForInput("BADPROJECT.dataset.table"),
  null,
);

assert.equal(
  parseQualifiedTableIdForInput("my-project.my_dataset.table$20260807"),
  null,
);

assert.equal(
  parseQualifiedTableIdForInput("my-project.my_dataset.table@1234"),
  null,
);

const consoleTableId = {
  project: "haiyuan-anarres-dev-806843",
  dataset: "bqaa_looker_demo",
  table: "agent_events",
};
const workspaceReference =
  "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
  "!2sbqaa_looker_demo!3sagent_events";
const pantheonTableUrl =
  "https://pantheon.corp.google.com/bigquery?ws=" + workspaceReference;
const publicConsoleTableUrl =
  "https://console.cloud.google.com/bigquery?project=another-project&ws=" +
  workspaceReference;
const encodedConsoleTableUrl =
  "https://console.cloud.google.com/bigquery?ws=" +
  "%211m5%211m4%214m3%211shaiyuan-anarres-dev-806843" +
  "%212sbqaa_looker_demo%213sagent_events";
// The live Console appends UI-state fields such as `!23sRESOURCE_LIST`
// (clicked table) or `!23sWS_URL_PARAM` (link navigation), which also bump
// the enclosing group counts from `!1m5!1m4` to `!1m6!1m5`. Captured from a
// real session on 2026-08-12.
const clickedTableUrl =
  "https://console.cloud.google.com/bigquery?project=another-project&ws=" +
  "!1m6!1m5!4m3!1shaiyuan-anarres-dev-806843" +
  "!2sbqaa_looker_demo!3sagent_events!23sRESOURCE_LIST";
const linkNavigationTableUrl =
  "https://console.cloud.google.com/bigquery?ws=" +
  "!1m6!1m5!4m3!1shaiyuan-anarres-dev-806843" +
  "!2sbqaa_looker_demo!3sagent_events!23sWS_URL_PARAM";
// One table open in two workspace tabs still names a single table.
const repeatedReferenceTableUrl =
  "https://console.cloud.google.com/bigquery?ws=" +
  workspaceReference +
  workspaceReference;
// Browsers accept the slashless `https:` spelling and canonicalize it to
// the `://` form, so an allowlisted link keeps working without slashes.
const slashlessConsoleTableUrl =
  "https:console.cloud.google.com/bigquery?ws=" + workspaceReference;
// A table tab alongside the Console's left-panel group (`!16m3`, not a
// table or dataset resource). Captured from a real session on 2026-08-12.
const leftPanelTableUrl =
  "https://console.cloud.google.com/bigquery?project=another-project&ws=" +
  "!1m12!1m5!4m3!1shaiyuan-anarres-dev-806843" +
  "!2sbqaa_looker_demo!3sagent_events!23sWS_URL_PARAM" +
  "!1m5!16m3!1m1!1shaiyuan-anarres-dev-806843!3e2!23sLEFT_PANEL";

for (const url of [
  pantheonTableUrl,
  publicConsoleTableUrl,
  encodedConsoleTableUrl,
  clickedTableUrl,
  linkNavigationTableUrl,
  repeatedReferenceTableUrl,
  leftPanelTableUrl,
  slashlessConsoleTableUrl,
]) {
  assert.deepEqual(
    parseBigQueryConsoleTableUrl(url),
    consoleTableId,
    `${url} resolves to the copied BigQuery table`,
  );
  assert.deepEqual(
    parseTableReference(url),
    consoleTableId,
    `${url} is accepted by the unified paste parser`,
  );
  assert.deepEqual(
    parseTableReferenceForInput(url),
    consoleTableId,
    `${url} is accepted by the committed-input parser`,
  );
}

for (const rejectedUrl of [
  "http://console.cloud.google.com/bigquery?ws=" + workspaceReference,
  "https://evil.example/bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com.evil.example/bigquery?ws=" +
    workspaceReference,
  "https://user@console.cloud.google.com/bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com:8443/bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com/bigquery/?ws=" + workspaceReference,
  "https://console.cloud.google.com/not-bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com/bigquery",
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference + "&ws=" + workspaceReference,
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843!2sbqaa_looker_demo",
  // Two different tables in one workspace are ambiguous.
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
    "!2sbqaa_looker_demo!3sother_table",
  // A dataset view (`!3m2` marker) names no table.
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!3m2!1shaiyuan-anarres-dev-806843" +
    "!2sbqaa_looker_demo!23sRESOURCE_LIST",
  // A dataset view alongside a table reference leaves the active resource
  // unprovable, so the table must not be autofilled.
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!3m2!1shaiyuan-anarres-dev-806843" +
    "!2sother_dataset!23sRESOURCE_LIST",
  // A truncated second table marker signals malformed workspace state.
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843!2sbqaa_looker_demo",
  // So does a workspace truncated exactly at a dangling terminal marker.
  "https://console.cloud.google.com/bigquery?ws=" + workspaceReference + "!4m3",
  // URL-shaped input whose URL constructor throws.
  "https://",
  // Slashless HTTP(S) spellings canonicalize to real URLs and must not fall
  // through to the legacy colon-form normalization.
  "https:evil.example",
  "HTTPS:evil.example",
  "http:evil.example",
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1sBADPROJECT!2sbqaa_looker_demo!3sagent_events",
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
    "!2sbad-dataset!3sagent_events",
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
    "!2sbqaa_looker_demo!3stable$20260812",
]) {
  assert.equal(
    parseBigQueryConsoleTableUrl(rejectedUrl),
    null,
    `${rejectedUrl} is rejected without extracting identifiers`,
  );
  assert.equal(
    parseTableReference(rejectedUrl),
    null,
    `${rejectedUrl} cannot fall through to qualified-ID parsing`,
  );
}

const dashboard = new URL(buildDashboardUrl(values));
assert.equal(dashboard.origin, "https://lookerstudio.google.com");
assert.equal(dashboard.pathname, "/reporting/create");
assert.equal(dashboard.searchParams.get("c.reportId"), REPORT_CONFIG.reportId);
assert.equal(dashboard.searchParams.get("c.mode"), "view");
assert.equal(
  dashboard.searchParams.get("ds.ds230.sqlReplace"),
  [
    "test-project-0728-467323",
    "customer-project-123",
    "bqaa_fixture_adk_1_27_0",
    "agent_analytics",
    "sentinelbqaaevents",
    "agent_events",
  ].join(","),
);
assert.equal(
  dashboard.searchParams.get("ds.ds230.billingProjectId"),
  "customer-project-123",
);
assert.equal(dashboard.searchParams.get("ds.ds230.refreshFields"), "false");
assert.match(
  dashboard.searchParams.get("r.reportName"),
  /agent_analytics\.agent_events$/,
);

const hyphenatedDashboard = new URL(buildDashboardUrl(hyphenatedTable));
assert.equal(
  hyphenatedDashboard.searchParams.get("ds.ds230.sqlReplace"),
  [
    "test-project-0728-467323",
    "customer-project-123",
    "bqaa_fixture_adk_1_27_0",
    "agent_analytics",
    "sentinelbqaaevents",
    "events_agent_cur-phenix",
  ].join(","),
);

const setup = new URL(
  buildSetupUrl(values, "https://example.test/configure?stale=yes#old"),
);
assert.deepEqual(
  Object.fromEntries(setup.searchParams),
  {
    project: values.project,
    dataset: values.dataset,
    table: values.table,
  },
);
assert.equal(setup.hash, "");

const advanced = {
  ...values,
  billingProject: "billing-project-123",
};
const advancedDashboard = new URL(buildDashboardUrl(advanced));
assert.equal(
  advancedDashboard.searchParams.get("ds.ds230.billingProjectId"),
  "billing-project-123",
);

for (const invalid of [
  { ...values, project: "UPPERCASE" },
  { ...values, project: "project;drop" },
  { ...values, dataset: "bad-dataset" },
  { ...values, dataset: "data`set" },
  { ...values, table: "table,other" },
  { ...values, billingProject: "UPPERCASE" },
]) {
  assert.throws(
    () => buildDashboardUrl(invalid),
    (error) => typeof error.field === "string" && error.message.length > 20,
  );
}

assert.throws(
  () => buildDashboardUrl({ ...values, project: "short" }),
  (error) =>
    error.field === "project" &&
    /6–30 lowercase letters, digits, or hyphens/.test(error.message),
);
assert.throws(
  () => buildDashboardUrl({ ...values, dataset: "bad-dataset" }),
  (error) =>
    error.field === "dataset" &&
    /letter or underscore/.test(error.message),
);

for (const collision of [
  { ...values, project: "xsentinelbqaaevents" },
  { ...values, dataset: "customer_sentinelbqaaevents_data" },
]) {
  assert.throws(
    () => buildDashboardUrl(collision),
    /reserved dashboard template value/,
  );
}

for (const safeCollision of [
  { ...values, project: "test-project-0728-467323" },
  { ...values, dataset: "bqaa_fixture_adk_1_27_0" },
  { ...values, table: "custom_sentinelbqaaevents_table" },
  { ...values, billingProject: "test-project-0728-467323" },
]) {
  assert.doesNotThrow(() => buildDashboardUrl(safeCollision));
}

class FakeElement {
  constructor(value = "") {
    this.value = value;
    this.textContent = "";
    this.dataset = {};
    this.disabled = false;
    this.href = "";
    this.listeners = {};
    this.attributes = new Map();
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "href") this.href = "";
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  querySelectorAll() {
    return [];
  }
}

const fakeElements = new Map([
  ["#configurator", new FakeElement()],
  ["#create-dashboard", new FakeElement()],
  ["#copy-link", new FakeElement()],
  ["#copy-checklist", new FakeElement()],
  ["#security-checklist", new FakeElement()],
  ["#form-status", new FakeElement()],
  ["#project", new FakeElement(values.project)],
  ["#dataset", new FakeElement(values.dataset)],
  ["#table", new FakeElement(values.table)],
  ["#billing-project", new FakeElement("")],
  ["#project-error", new FakeElement()],
  ["#dataset-error", new FakeElement()],
  ["#table-error", new FakeElement()],
  ["#billing-project-error", new FakeElement()],
]);
globalThis.document = {
  querySelector(selector) {
    return fakeElements.get(selector);
  },
};
globalThis.window = {
  location: {
    href: "https://example.test/configure",
    search: "",
  },
  open() {},
};
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: {
    clipboard: {
      async writeText() {},
    },
  },
});

await import("../docs/app.mjs");

function resetTableInputs() {
  fakeElements.get("#project").value = values.project;
  fakeElements.get("#dataset").value = values.dataset;
  fakeElements.get("#table").value = values.table;

  fakeElements.get("#form-status").textContent = "";
  fakeElements.get("#form-status").dataset.kind = "";

  fakeElements.get("#project-error").textContent = "";
  fakeElements.get("#dataset-error").textContent = "";
  fakeElements.get("#table-error").textContent = "";
}

function pasteIntoProject(name, text) {
  let prevented = false;
  fakeElements.get(`#${name}`).listeners.paste({
    clipboardData: { getData: () => text },
    preventDefault() {
      prevented = true;
    },
  });
  return prevented;
}

for (const [description, pastedId] of [
  ["normal dotted ID", "my-project.my_dataset.my_table"],
  ["backticked ID", "`my-project.my_dataset.my_table`"],
  ["legacy colon ID", "my-project:my_dataset.my_table"],
  ["semicolon-terminated ID", "my-project.my_dataset.my_table;"],
  ["comma-terminated ID", "my-project.my_dataset.my_table,"],
]) {
  resetTableInputs();
  assert.equal(pasteIntoProject("project", pastedId), true, `${description} is handled`);
  assert.deepEqual(
    {
      project: fakeElements.get("#project").value,
      dataset: fakeElements.get("#dataset").value,
      table: fakeElements.get("#table").value,
    },
    qualifiedTableId,
    `${description} fills all identifier fields`,
  );
  assert.equal(
    fakeElements.get("#form-status").textContent,
    'Split "my-project.my_dataset.my_table" into the three fields.',
    `${description} reports the split`,
  );
  assert.equal(fakeElements.get("#form-status").dataset.kind, "ready");
}

resetTableInputs();
fakeElements.get("#dataset").value = "my-project.my_dataset.my_table";
fakeElements.get("#dataset").listeners.change({
  target: fakeElements.get("#dataset"),
});
assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  qualifiedTableId,
  "the change fallback also splits a qualified ID",
);
assert.equal(
  fakeElements.get("#form-status").textContent,
  'Split "my-project.my_dataset.my_table" into the three fields.',
);

resetTableInputs();

assert.equal(
  pasteIntoProject("project", "BADPROJECT.dataset.table"),
  true,
  "a qualified identifier with an invalid project is still distributed",
);

assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  {
    project: "BADPROJECT",
    dataset: "dataset",
    table: "table",
  },
  "the qualified identifier is distributed before validation",
);

assert.match(
  fakeElements.get("#project-error").textContent,
  /Use 6–30 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit/,
  "the invalid project component is reported inline",
);

assert.equal(
  fakeElements.get("#form-status").textContent,
  "",
  "an invalid split does not replace the inline field error with a success status",
);

assert.equal(
  fakeElements.get("#form-status").dataset.kind,
  "",
  "an invalid split does not leave the form in a ready state",
);

resetTableInputs();
assert.equal(
  pasteIntoProject("project", "customers"),
  false,
  "an unqualified ID retains normal paste behavior",
);
assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  {
    project: values.project,
    dataset: values.dataset,
    table: values.table,
  },
  "an unqualified paste does not distribute values to the other fields",
);

// Simulate the browser applying the unhandled paste and dispatching its input event.
fakeElements.get("#project").value = "customers";
fakeElements.get("#project").listeners.input({
  target: fakeElements.get("#project"),
});
assert.equal(fakeElements.get("#project").value, "customers");
assert.equal(fakeElements.get("#dataset").value, values.dataset);
assert.equal(fakeElements.get("#table").value, values.table);

fakeElements.get("#table").value = "table,other";
fakeElements.get("#table").listeners.input({
  target: fakeElements.get("#table"),
});
assert.match(
  fakeElements.get("#table-error").textContent,
  /letters, digits/,
);
assert.equal(
  fakeElements.get("#form-status").textContent,
  "",
  "field validation must not repeat the inline error in the form status",
);

resetTableInputs();

assert.equal(
  pasteIntoProject("project", "a.b.c.d"),
  false,
  "extra-dot identifiers should not be parsed",
);

assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  {
    project: values.project,
    dataset: values.dataset,
    table: values.table,
  },
);

fakeElements.get("#project").value = "a.b.c.d";
fakeElements.get("#project").listeners.input({
  target: fakeElements.get("#project"),
});

assert.match(
  fakeElements.get("#project-error").textContent,
  /6–30 lowercase letters/,
);

resetTableInputs();

assert.equal(
  pasteIntoProject("project", "my-project.my_dataset.table$20260807"),
  true,
);

assert.equal(
  fakeElements.get("#project").value,
  "my-project",
);

assert.equal(
  fakeElements.get("#dataset").value,
  "my_dataset",
);

assert.equal(
  fakeElements.get("#table").value,
  "table$20260807",
);

assert.match(
  fakeElements.get("#table-error").textContent,
  /letters, digits/,
);

assert.equal(
  fakeElements.get("#form-status").textContent,
  "",
  "invalid table should keep the form status empty",
);

resetTableInputs();

assert.equal(
  pasteIntoProject("project", "my-project.my_dataset.table@1234"),
  true,
);

assert.equal(
  fakeElements.get("#table").value,
  "table@1234",
);

assert.match(
  fakeElements.get("#table-error").textContent,
  /letters, digits/,
);

assert.equal(
  fakeElements.get("#form-status").textContent,
  "",
);

resetTableInputs();

assert.equal(
  pasteIntoProject("project", "my-project.my_dataset."),
  false,
  "identifiers with an empty part should not be parsed",
);

assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  {
    project: values.project,
    dataset: values.dataset,
    table: values.table,
  },
);

fakeElements.get("#project").value = "my-project.my_dataset.";
fakeElements.get("#project").listeners.input({
  target: fakeElements.get("#project"),
});

assert.match(
  fakeElements.get("#project-error").textContent,
  /6–30 lowercase letters/,
);

resetTableInputs();

const typedQualifiedId = "my-project.my_dataset.my_table";

for (let i = 1; i <= typedQualifiedId.length; i += 1) {
  const prefix = typedQualifiedId.slice(0, i);

  fakeElements.get("#project").value = prefix;
  fakeElements.get("#project").listeners.input({
    target: fakeElements.get("#project"),
  });

  assert.equal(
    fakeElements.get("#project").value,
    prefix,
    `typing "${prefix}" must not distribute the qualified ID`,
  );
  assert.equal(
    fakeElements.get("#dataset").value,
    values.dataset,
    `typing "${prefix}" must not change the dataset`,
  );
  assert.equal(
    fakeElements.get("#table").value,
    values.table,
    `typing "${prefix}" must not change the table`,
  );
}

fakeElements.get("#project").listeners.change({
  target: fakeElements.get("#project"),
});

assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  qualifiedTableId,
  "a committed qualified ID is distributed on change",
);

for (const field of ["project", "dataset", "table"]) {
  resetTableInputs();

  assert.equal(
    pasteIntoProject(
      field,
      "my-project.my_dataset.my_table",
    ),
    true,
    `qualified ID pasted into ${field} is handled`,
  );

  assert.deepEqual(
    {
      project: fakeElements.get("#project").value,
      dataset: fakeElements.get("#dataset").value,
      table: fakeElements.get("#table").value,
    },
    qualifiedTableId,
    `qualified ID pasted into ${field} fills all identifier fields`,
  );
}

for (const [description, consoleUrl] of [
  ["Pantheon", pantheonTableUrl],
  ["public Console", publicConsoleTableUrl],
  ["clicked-table", clickedTableUrl],
]) {
  for (const field of ["project", "dataset", "table"]) {
    resetTableInputs();

    assert.equal(
      pasteIntoProject(field, consoleUrl),
      true,
      `${description} URL pasted into ${field} is handled`,
    );
    assert.deepEqual(
      {
        project: fakeElements.get("#project").value,
        dataset: fakeElements.get("#dataset").value,
        table: fakeElements.get("#table").value,
      },
      consoleTableId,
      `${description} URL pasted into ${field} fills all identifier fields`,
    );
    assert.equal(
      fakeElements.get("#form-status").textContent,
      'Split "haiyuan-anarres-dev-806843.bqaa_looker_demo.agent_events" into the three fields.',
      `${description} URL reports the extracted table`,
    );
    assert.equal(fakeElements.get("#form-status").dataset.kind, "ready");
  }
}

resetTableInputs();
fakeElements.get("#dataset").value = encodedConsoleTableUrl;
fakeElements.get("#dataset").listeners.change({
  target: fakeElements.get("#dataset"),
});
assert.deepEqual(
  {
    project: fakeElements.get("#project").value,
    dataset: fakeElements.get("#dataset").value,
    table: fakeElements.get("#table").value,
  },
  consoleTableId,
  "the committed-input fallback recognizes an encoded Console URL",
);

for (const rejectedUrl of [
  "https://evil.example/bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1sBADPROJECT!2sbqaa_looker_demo!3sagent_events",
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843!2sbqaa_looker_demo",
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!3m2!1shaiyuan-anarres-dev-806843" +
    "!2sother_dataset!23sRESOURCE_LIST",
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843!2sbqaa_looker_demo",
  "https://console.cloud.google.com/bigquery?ws=" + workspaceReference + "!4m3",
  "https://",
  "https:evil.example",
]) {
  resetTableInputs();
  assert.equal(
    pasteIntoProject("dataset", rejectedUrl),
    false,
    "a rejected Console URL retains ordinary paste behavior",
  );
  assert.equal(fakeElements.get("#project").value, values.project);
  assert.equal(fakeElements.get("#dataset").value, values.dataset);
  assert.equal(fakeElements.get("#table").value, values.table);

  // Simulate the browser applying the unintercepted paste to its target.
  fakeElements.get("#dataset").value = rejectedUrl;
  fakeElements.get("#dataset").listeners.input({
    target: fakeElements.get("#dataset"),
  });
  assert.equal(fakeElements.get("#project").value, values.project);
  assert.equal(fakeElements.get("#table").value, values.table);
}

// #398: clicking the enabled create link sets the provisioning expectation
// without blocking navigation, and the message clears on the next change.
resetTableInputs();
fakeElements.get("#project").listeners.input({
  target: fakeElements.get("#project"),
});
assert.match(fakeElements.get("#form-status").textContent, /^Ready for /);

let navigationPrevented = false;
fakeElements.get("#create-dashboard").listeners.click({
  preventDefault() {
    navigationPrevented = true;
  },
});
assert.equal(
  navigationPrevented,
  false,
  "an enabled create link must still navigate",
);
assert.match(
  fakeElements.get("#form-status").textContent,
  /may briefly show an error page/,
  "clicking the enabled link warns about the provisioning delay",
);
assert.equal(fakeElements.get("#form-status").dataset.kind, "waiting");

fakeElements.get("#project").listeners.input({
  target: fakeElements.get("#project"),
});
assert.doesNotMatch(
  fakeElements.get("#form-status").textContent,
  /error page/,
  "the waiting message clears on the next valid state change",
);

fakeElements.get("#configurator").listeners.submit({ preventDefault() {} });
assert.equal(
  fakeElements.get("#form-status").dataset.kind,
  "waiting",
  "submitting the form sets the same provisioning expectation",
);

fakeElements.get("#create-dashboard").href = "";
navigationPrevented = false;
fakeElements.get("#create-dashboard").listeners.click({
  preventDefault() {
    navigationPrevented = true;
  },
});
assert.equal(
  navigationPrevented,
  true,
  "a disabled create link must not navigate",
);

console.log(
  "web configurator OK: identifiers and sentinels validated; Linking API URL deterministic",
);
