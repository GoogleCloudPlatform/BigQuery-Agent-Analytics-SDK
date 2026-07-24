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
  prefix: "v",
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
    "vsentinelbqaa",
    "v",
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
  prefix: "analytics-v2",
  billingProject: "billing-project-123",
};
const advancedDashboard = new URL(buildDashboardUrl(advanced));
assert.equal(
  advancedDashboard.searchParams.get("ds.ds230.sqlReplace").split(",").at(-1),
  "analytics-v2",
);
assert.equal(
  advancedDashboard.searchParams.get("ds.ds230.billingProjectId"),
  "billing-project-123",
);
assert.equal(
  new URL(
    buildSetupUrl(advanced, "https://example.test/configure"),
  ).searchParams.get("prefix"),
  "analytics-v2",
);

for (const invalid of [
  { ...values, project: "UPPERCASE" },
  { ...values, project: "project;drop" },
  { ...values, dataset: "bad-dataset" },
  { ...values, dataset: "data`set" },
  { ...values, table: "table,other" },
  { ...values, prefix: "v,other" },
  { ...values, billingProject: "UPPERCASE" },
]) {
  assert.throws(() => buildDashboardUrl(invalid));
}

for (const collision of [
  { ...values, project: "vsentinelbqaaproj" },
  { ...values, dataset: "customer_vsentinelbqaa_data" },
  { ...values, table: "agent_vsentinelbqaa_events" },
  { ...values, prefix: "bqaa_fixture_adk_1_27_0_custom" },
  { ...values, billingProject: "vsentinelbqaabilling" },
]) {
  assert.throws(
    () => buildDashboardUrl(collision),
    /reserved dashboard template value/,
  );
}

console.log(
  "web configurator OK: identifiers and sentinels validated; Linking API URL deterministic",
);
