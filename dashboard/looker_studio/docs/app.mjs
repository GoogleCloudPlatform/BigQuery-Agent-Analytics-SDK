import {
  buildDashboardUrl,
  buildSetupUrl,
  validateConfiguration,
} from "./configurator.mjs";
import { REPORT_CONFIG } from "./report-config.mjs";

const form = document.querySelector("#configurator");
const createLink = document.querySelector("#create-dashboard");
const copyButton = document.querySelector("#copy-link");
const checklistButton = document.querySelector("#copy-checklist");
const checklist = document.querySelector("#security-checklist");
const status = document.querySelector("#form-status");
const inputs = {
  project: document.querySelector("#project"),
  dataset: document.querySelector("#dataset"),
  table: document.querySelector("#table"),
  billingProject: document.querySelector("#billing-project"),
};
const errors = {
  project: document.querySelector("#project-error"),
  dataset: document.querySelector("#dataset-error"),
  table: document.querySelector("#table-error"),
  billingProject: document.querySelector("#billing-project-error"),
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

function clearFieldErrors() {
  for (const [name, input] of Object.entries(inputs)) {
    input.removeAttribute("aria-invalid");
    errors[name].textContent = "";
  }
}

function refresh() {
  clearFieldErrors();
  try {
    const values = validateConfiguration(currentValues());
    createLink.href = buildDashboardUrl(values);
    createLink.removeAttribute("aria-disabled");
    copyButton.disabled = false;
    setStatus(
      `Ready for ${values.project}.${values.dataset}.${values.table}.`,
      "ready",
    );
  } catch (error) {
    createLink.removeAttribute("href");
    createLink.setAttribute("aria-disabled", "true");
    copyButton.disabled = true;
    if (error.field && inputs[error.field]) {
      inputs[error.field].setAttribute("aria-invalid", "true");
      errors[error.field].textContent = error.message;
    }
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

checklistButton.addEventListener("click", async () => {
  const text = [...checklist.querySelectorAll("li")]
    .map((item, index) => `${index + 1}. ${item.textContent.trim()}`)
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    setStatus("Security checklist copied.", "ready");
  } catch {
    setStatus("Could not copy the security checklist.", "error");
  }
});

refresh();
