import {
  buildDashboardUrl,
  buildSetupUrl,
  validateConfiguration,
} from "./configurator.mjs";
import { REPORT_CONFIG } from "./report-config.mjs";

const form = document.querySelector("#configurator");
const createLink = document.querySelector("#create-dashboard");
const copyButton = document.querySelector("#copy-link");
const status = document.querySelector("#form-status");
const inputs = {
  project: document.querySelector("#project"),
  dataset: document.querySelector("#dataset"),
  table: document.querySelector("#table"),
  prefix: document.querySelector("#prefix"),
  billingProject: document.querySelector("#billing-project"),
};

function currentValues() {
  return Object.fromEntries(
    Object.entries(inputs).map(([name, input]) => [name, input.value]),
  );
}

function setStatus(message, kind = "") {
  status.textContent = message;
  status.dataset.kind = kind;
}

function refresh() {
  try {
    const values = validateConfiguration(currentValues());
    createLink.href = buildDashboardUrl(values);
    createLink.removeAttribute("aria-disabled");
    copyButton.disabled = false;
    setStatus(
      `Ready for ${values.project}.${values.dataset}.${values.table} `
        + `(${values.prefix}_* views).`,
      "ready",
    );
  } catch (error) {
    createLink.removeAttribute("href");
    createLink.setAttribute("aria-disabled", "true");
    copyButton.disabled = true;
    setStatus(error.message, "error");
  }
}

const query = new URLSearchParams(window.location.search);
for (const [name, input] of Object.entries(inputs)) {
  if (query.has(name)) {
    input.value = query.get(name);
  }
}
if (!inputs.table.value) {
  inputs.table.value = REPORT_CONFIG.defaultTable;
}
if (!inputs.prefix.value) {
  inputs.prefix.value = REPORT_CONFIG.defaultViewPrefix;
}

for (const input of Object.values(inputs)) {
  input.addEventListener("input", refresh);
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  refresh();
  if (createLink.href) {
    window.open(createLink.href, "_blank", "noopener,noreferrer");
  }
});

createLink.addEventListener("click", (event) => {
  if (!createLink.href) {
    event.preventDefault();
    refresh();
  }
});

copyButton.addEventListener("click", async () => {
  try {
    const setupUrl = buildSetupUrl(currentValues(), window.location.href);
    await navigator.clipboard.writeText(setupUrl);
    setStatus("Setup link copied. It contains identifiers, never credentials.", "ready");
  } catch (error) {
    setStatus(error.message || "Could not copy the setup link.", "error");
  }
});

refresh();
