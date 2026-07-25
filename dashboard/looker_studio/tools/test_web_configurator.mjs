import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  buildDashboardUrl,
  buildSetupUrl,
  validateConfiguration,
} from "../docs/configurator.mjs";
import { REPORT_CONFIG } from "../docs/report-config.mjs";

const pageSource = readFileSync(
  new URL("../docs/index.html", import.meta.url),
  "utf8",
);
assert.doesNotMatch(pageSource, /github\.com\/caohy1988/);
assert.match(
  pageSource,
  /https:\/\/github\.com\/GoogleCloudPlatform\/BigQuery-Agent-Analytics-SDK/,
);
assert.match(
  pageSource,
  /https:\/\/googlecloudplatform\.github\.io\/BigQuery-Agent-Analytics-SDK\//,
);

const values = {
  project: "customer-project-123",
  dataset: "agent_analytics",
  table: "agent_events",
  billingProject: "customer-project-123",
};
assert.deepEqual(validateConfiguration(values), values);

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

console.log(
  "web configurator OK: identifiers and sentinels validated; Linking API URL deterministic",
);
