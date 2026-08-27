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
  validateQualifiedTableId,
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
assert.ok(
  pageSource.indexOf("provisions your copy — don’t close it.") <
    pageSource.indexOf("shows an acknowledgement dialog"),
  "step 02 must keep the transient warning ahead of the dialog explanation",
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
// #445: the dynamic status a user actually watches after clicking must not
// train them to wait out the terminal denial either.
assert.match(
  appSource,
  /This report isn’t shared with you.*will not resolve by/s,
  "WAITING_MESSAGE must except the terminal dialog from the wait guidance",
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
    // #448: the collision is attributed to the offending segment so the
    // single-field UI reports it in the segment-level error class; the
    // collision logic and Linking API output are unchanged.
    (error) =>
      error.field === "tableId" &&
      ["project", "dataset"].includes(error.segment) &&
      /reserved dashboard template value/.test(error.message),
  );
}

// #448: the combined-field validator returns the parsed triple and throws
// in exactly two error classes: whole-field (segment: null) for input with
// no truthful segments to blame, and segment-level (segment named) when
// exactly three segments exist and one violates its rule — including
// sentinel collisions, which must fail here and never after Ready.
assert.deepEqual(
  validateQualifiedTableId("my-project.my_dataset.my_table"),
  qualifiedTableId,
);
assert.deepEqual(
  validateQualifiedTableId("`my-project.my_dataset.my_table`;"),
  qualifiedTableId,
  "SQL-copy punctuation is normalized by the combined-field validator",
);
assert.throws(
  () => validateQualifiedTableId(""),
  (error) =>
    error.field === "tableId" &&
    error.segment === null &&
    /project\.dataset\.table/.test(error.message),
  "empty input is a whole-field error",
);
assert.throws(
  () => validateQualifiedTableId("a.b.c.d"),
  (error) =>
    error.field === "tableId" &&
    error.segment === null &&
    /three dot-separated segments/.test(error.message),
  "wrong arity is a whole-field error",
);
assert.throws(
  () => validateQualifiedTableId("BADPROJECT.dataset.table"),
  (error) =>
    error.field === "tableId" &&
    error.segment === "project" &&
    /^Project segment: /.test(error.message),
  "an invalid project is a segment-level error naming the segment",
);
assert.throws(
  () => validateQualifiedTableId("my-project.bad-dataset.table"),
  (error) =>
    error.segment === "dataset" && /^Dataset segment: /.test(error.message),
);
assert.throws(
  () => validateQualifiedTableId("my-project.my_dataset.table$20260807"),
  (error) =>
    error.segment === "table" && /^Table segment: /.test(error.message),
);
assert.throws(
  () => validateQualifiedTableId("xsentinelbqaaevents.my_dataset.my_table"),
  (error) =>
    error.segment === "project" &&
    /reserved dashboard template value/.test(error.message),
  "a sentinel collision fails segment-attributed before Ready",
);
assert.deepEqual(
  validateQualifiedTableId(publicConsoleTableUrl),
  consoleTableId,
  "a supported Console link validates through the combined field",
);
assert.deepEqual(
  validateQualifiedTableId(pantheonTableUrl),
  consoleTableId,
  "both supported Console hosts validate through the combined field",
);
assert.throws(
  () =>
    validateQualifiedTableId(
      "https://console.cloud.google.com/bigquery?ws=" +
        workspaceReference +
        "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
        "!2sbqaa_looker_demo!3sother_table",
    ),
  (error) =>
    error.segment === null &&
    /clearly name exactly one BigQuery table/.test(error.message),
  "an ambiguous Console link is a whole-field error",
);

// #448: a generated setup link round-trips through the single field while
// retaining the existing three-parameter query format.
{
  const roundTripSetup = new URL(
    buildSetupUrl(values, "https://example.test/configure"),
  );
  const prefillId = [
    roundTripSetup.searchParams.get("project"),
    roundTripSetup.searchParams.get("dataset"),
    roundTripSetup.searchParams.get("table"),
  ].join(".");
  const reparsed = validateQualifiedTableId(prefillId);
  assert.equal(
    buildSetupUrl(
      { ...reparsed, billingProject: "" },
      "https://example.test/configure",
    ),
    roundTripSetup.toString(),
    "the setup link survives a field round-trip byte-for-byte",
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
  ["#table-id", new FakeElement("")],
  ["#table-id-error", new FakeElement()],
  ["#billing-project", new FakeElement("")],
  ["#billing-project-error", new FakeElement()],
]);
const fakeDocumentElement = new FakeElement();
globalThis.document = {
  documentElement: fakeDocumentElement,
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
let copiedText = "";
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: {
    clipboard: {
      async writeText(text) {
        copiedText = text;
      },
    },
  },
});

const field = fakeElements.get("#table-id");
const fieldError = fakeElements.get("#table-id-error");
const billing = fakeElements.get("#billing-project");
const billingErrorEl = fakeElements.get("#billing-project-error");
const createLink = fakeElements.get("#create-dashboard");
const copyButton = fakeElements.get("#copy-link");
const formStatus = fakeElements.get("#form-status");

function assertActionsDisabled(context) {
  assert.equal(createLink.href, "", `${context}: create link has no URL`);
  assert.equal(
    createLink.attributes.get("aria-disabled"),
    "true",
    `${context}: create link is aria-disabled`,
  );
  assert.equal(copyButton.disabled, true, `${context}: copy is disabled`);
}

function assertFieldClean(context) {
  assert.equal(
    field.attributes.has("aria-invalid"),
    false,
    `${context}: field is not marked invalid`,
  );
  assert.equal(fieldError.textContent, "", `${context}: no field error`);
}

function typeIntoField(value) {
  field.value = value;
  field.listeners.input({ target: field });
}

function commitField() {
  field.listeners.change({ target: field });
}

function pasteIntoField(text) {
  let prevented = false;
  field.listeners.paste({
    clipboardData: { getData: () => text },
    preventDefault() {
      prevented = true;
    },
  });
  return prevented;
}

await import("../docs/app.mjs");

// #448 pristine first load: empty field, no error, both actions disabled,
// no status — and the runtime app-initialized marker is set.
assert.equal(field.value, "", "pristine: the field starts empty");
assertFieldClean("pristine");
assertActionsDisabled("pristine");
assert.equal(formStatus.textContent, "", "pristine: no status");
assert.equal(
  fakeDocumentElement.attributes.get("data-bqaa-app-initialized"),
  "true",
  "app.mjs writes the runtime app-initialized marker",
);

// Manual typing stays partial: no error while incomplete, but a value that
// becomes valid enables the actions without waiting for blur.
typeIntoField("my-pro");
assertFieldClean("partial typing");
assertActionsDisabled("partial typing");
assert.equal(formStatus.textContent, "", "partial typing: no status");

typeIntoField("my-project.my_dataset.my_table");
assert.match(
  formStatus.textContent,
  /^Ready for my-project\.my_dataset\.my_table\./,
  "a hand-typed valid ID reaches Ready",
);
assert.equal(formStatus.dataset.kind, "ready");
assert.ok(createLink.href, "Ready enables the create link");
assert.equal(copyButton.disabled, false, "Ready enables copy");
assertFieldClean("valid");

// #448 fail-closed mutation: editing the Ready value immediately revokes
// the derived triple, both actions, and the Ready announcement — while the
// error presentation stays deferred until blur (the field is untouched by
// a validation trigger so far in this cycle).
typeIntoField("my-project.my_dataset.");
assertActionsDisabled("mutated away from valid");
assert.equal(
  formStatus.textContent,
  "",
  "mutation clears the prior Ready announcement",
);
assert.equal(formStatus.dataset.kind, "");
assertFieldClean("mutated before blur");

commitField();
assert.equal(
  field.attributes.get("aria-invalid"),
  "true",
  "blur reveals the deferred error",
);
assert.match(
  fieldError.textContent,
  /three dot-separated segments/,
  "an incomplete ID is a whole-field error",
);
assertActionsDisabled("invalid after blur");

// Corrected: once touched/invalid, revalidation happens on every input.
typeIntoField("my-project.my_dataset.my_table");
assert.match(formStatus.textContent, /^Ready for /, "corrected reaches Ready");
assertFieldClean("corrected");
assert.ok(createLink.href, "corrected re-enables the create link");

// Segment-level error class: the offending segment is named inline.
typeIntoField("BADPROJECT.dataset.table");
assert.match(
  fieldError.textContent,
  /^Project segment: /,
  "an invalid project segment is attributed inline",
);
assert.match(fieldError.textContent, /6–30 lowercase letters/);
assertActionsDisabled("segment error");
assert.equal(formStatus.textContent, "", "segment error keeps the status empty");

// Sentinel collisions are segment-attributed and block Ready.
typeIntoField("xsentinelbqaaevents.my_dataset.my_table");
assert.match(
  fieldError.textContent,
  /^Project segment: .*reserved dashboard template value/,
  "a sentinel collision reports as a segment-level error before Ready",
);
assertActionsDisabled("sentinel collision");

// Whole-field error class for a link that names no single table.
typeIntoField(
  "https://console.cloud.google.com/bigquery?ws=" +
    workspaceReference +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843" +
    "!2sbqaa_looker_demo!3sother_table",
);
assert.match(
  fieldError.textContent,
  /clearly name exactly one BigQuery table/,
  "an ambiguous Console link is a whole-field error",
);
assertActionsDisabled("ambiguous link");

// Every supported paste form normalizes into the field and reaches Ready.
for (const [description, pastedId] of [
  ["normal dotted ID", "my-project.my_dataset.my_table"],
  ["backticked ID", "`my-project.my_dataset.my_table`"],
  ["legacy colon ID", "my-project:my_dataset.my_table"],
  ["semicolon-terminated ID", "my-project.my_dataset.my_table;"],
  ["comma-terminated ID", "my-project.my_dataset.my_table,"],
]) {
  typeIntoField("");
  assert.equal(pasteIntoField(pastedId), true, `${description} is handled`);
  assert.equal(
    field.value,
    "my-project.my_dataset.my_table",
    `${description} normalizes into the field`,
  );
  assert.match(
    formStatus.textContent,
    /^Ready for my-project\.my_dataset\.my_table\./,
    `${description} reaches Ready`,
  );
  assert.equal(formStatus.dataset.kind, "ready");
  assertFieldClean(description);
}

// Both supported Console hosts parse through a paste into the single field.
for (const [description, consoleUrl] of [
  ["Pantheon", pantheonTableUrl],
  ["public Console", publicConsoleTableUrl],
  ["clicked-table", clickedTableUrl],
  ["encoded", encodedConsoleTableUrl],
]) {
  typeIntoField("");
  assert.equal(
    pasteIntoField(consoleUrl),
    true,
    `${description} URL paste is handled`,
  );
  assert.equal(
    field.value,
    "haiyuan-anarres-dev-806843.bqaa_looker_demo.agent_events",
    `${description} URL normalizes to the dotted ID`,
  );
  assert.match(formStatus.textContent, /^Ready for /);
}

// A paste that parses but carries an invalid segment reports immediately:
// paste is a validation trigger, no blur needed.
typeIntoField("");
assert.equal(pasteIntoField("BADPROJECT.dataset.table"), true);
assert.equal(field.value, "BADPROJECT.dataset.table");
assert.match(
  fieldError.textContent,
  /^Project segment: /,
  "a pasted invalid segment reports without waiting for blur",
);
assertActionsDisabled("pasted segment error");

// An unparseable paste lands as raw text; its input event validates it
// immediately because paste is a validation trigger.
typeIntoField("");
assert.equal(
  pasteIntoField("a.b.c.d"),
  false,
  "wrong-arity paste retains ordinary paste behavior",
);
typeIntoField("a.b.c.d");
assert.match(
  fieldError.textContent,
  /three dot-separated segments/,
  "the landed unparseable paste reports a whole-field error immediately",
);
assert.equal(field.value, "a.b.c.d", "the raw invalid text is retained");
assertActionsDisabled("unparseable paste");

// A rejected Console link lands as raw text and reports the link error.
for (const rejectedUrl of [
  "https://evil.example/bigquery?ws=" + workspaceReference,
  "https://console.cloud.google.com/bigquery?ws=" +
    "!1m5!1m4!4m3!1shaiyuan-anarres-dev-806843!2sbqaa_looker_demo",
  "https://",
  "https:evil.example",
]) {
  typeIntoField("");
  assert.equal(
    pasteIntoField(rejectedUrl),
    false,
    "a rejected Console URL retains ordinary paste behavior",
  );
  typeIntoField(rejectedUrl);
  assert.match(
    fieldError.textContent,
    /clearly name exactly one BigQuery table/,
    `${rejectedUrl} reports the whole-field link error`,
  );
  assert.equal(field.value, rejectedUrl, "the raw link text is retained");
  assertActionsDisabled("rejected link");
}

// #448 decision 3: actionability requires the billing override too. An
// invalid override keeps the parsed table triple (the field stays clean and
// its value survives) while both actions disable; correcting the override
// restores Ready without touching the table field.
typeIntoField("my-project.my_dataset.my_table");
assert.match(formStatus.textContent, /^Ready for /);
billing.value = "UPPERCASE";
billing.listeners.input({ target: billing });
assertActionsDisabled("invalid billing override");
assert.match(
  billingErrorEl.textContent,
  /6–30 lowercase letters/,
  "the billing field shows its own error",
);
assert.equal(
  billing.attributes.get("aria-invalid"),
  "true",
  "the billing field is marked invalid",
);
assertFieldClean("table field during billing error");
assert.equal(
  field.value,
  "my-project.my_dataset.my_table",
  "the parsed table value is retained during a billing error",
);

billing.value = "billing-project-123";
billing.listeners.input({ target: billing });
assert.match(formStatus.textContent, /^Ready for /, "corrected billing restores Ready");
assert.equal(billingErrorEl.textContent, "", "the billing error clears");
assert.equal(billing.attributes.has("aria-invalid"), false);
assert.match(
  createLink.href,
  /billingProjectId%5D=billing-project-123|billingProjectId=billing-project-123|ds\.ds230\.billingProjectId/,
  "the explicit override reaches the Linking API URL",
);
{
  const linkParams = new URL(createLink.href).searchParams;
  assert.equal(
    linkParams.get("ds.ds230.billingProjectId"),
    "billing-project-123",
  );
}
billing.value = "";
billing.listeners.input({ target: billing });
{
  const linkParams = new URL(createLink.href).searchParams;
  assert.equal(
    linkParams.get("ds.ds230.billingProjectId"),
    "my-project",
    "a blank override bills the project segment of the fully qualified ID",
  );
}

// Copy produces the unchanged three-parameter setup link from the current
// derived triple.
copiedText = "";
await copyButton.listeners.click();
{
  const copied = new URL(copiedText);
  assert.deepEqual(Object.fromEntries(copied.searchParams), {
    project: "my-project",
    dataset: "my_dataset",
    table: "my_table",
  });
}

// #398: clicking the enabled create link sets the provisioning expectation
// without blocking navigation, and the message clears on the next change.
let navigationPrevented = false;
createLink.listeners.click({
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
  formStatus.textContent,
  /may briefly show an error page/,
  "clicking the enabled link warns about the provisioning delay",
);
assert.equal(formStatus.dataset.kind, "waiting");

typeIntoField(field.value);
assert.doesNotMatch(
  formStatus.textContent,
  /error page/,
  "the waiting message clears on the next valid state change",
);

fakeElements.get("#configurator").listeners.submit({ preventDefault() {} });
assert.equal(
  formStatus.dataset.kind,
  "waiting",
  "submitting the form sets the same provisioning expectation",
);

// A disabled create link must not navigate; the attempted action is a
// validation trigger that reveals the field error.
typeIntoField("a.b.c.d");
navigationPrevented = false;
createLink.listeners.click({
  preventDefault() {
    navigationPrevented = true;
  },
});
assert.equal(
  navigationPrevented,
  true,
  "a disabled create link must not navigate",
);
assert.match(
  fieldError.textContent,
  /three dot-separated segments/,
  "the attempted action reveals the field error",
);

// #448: an existing three-parameter setup link prefills the single field
// and validates immediately; the regenerated link is identical.
window.location.search =
  "?project=my-project&dataset=my_dataset&table=my_table";
billing.value = "";
await import("../docs/app.mjs?prefill=three-parameter");
assert.equal(
  field.value,
  "my-project.my_dataset.my_table",
  "a legacy setup link composes the fully qualified ID",
);
assert.match(
  formStatus.textContent,
  /^Ready for my-project\.my_dataset\.my_table\./,
  "a prefilled link validates immediately",
);
copiedText = "";
await copyButton.listeners.click();
assert.equal(
  new URL(copiedText).search,
  "?project=my-project&dataset=my_dataset&table=my_table",
  "the regenerated setup link retains the three-parameter format",
);

console.log(
  "web configurator OK: single-field states, error classes, and Linking API URL deterministic",
);
