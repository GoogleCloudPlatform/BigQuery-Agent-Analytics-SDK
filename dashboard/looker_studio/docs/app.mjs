import {
  buildDashboardUrl,
  buildSetupUrl,
  validateConfiguration,
  parseTableReference,
  parseTableReferenceForInput,
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
const tableInputs = [
  inputs.project,
  inputs.dataset,
  inputs.table,
];
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
    const hasFieldError = Boolean(error.field && inputs[error.field]);
    if (hasFieldError) {
      inputs[error.field].setAttribute("aria-invalid", "true");
      errors[error.field].textContent = error.message;
      setStatus("");
    } else {
      setStatus(error.message, "error");
    }
  }
}

function handleQualifiedTableId(parsed) {
  inputs.project.value = parsed.project;
  inputs.dataset.value = parsed.dataset;
  inputs.table.value = parsed.table;
}

function afterQualifiedTableId(parsed) {
  handleQualifiedTableId(parsed);
  refresh();
  if (createLink.href) {
    setStatus(
      `Split "${parsed.project}.${parsed.dataset}.${parsed.table}" into the three fields.`,
      "ready",
    );
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
for (const input of tableInputs) {
  input.addEventListener("input", refresh);
  input.addEventListener("change", (event) => {
    const parsed = parseTableReferenceForInput(event.target.value);

    if (parsed) {
      afterQualifiedTableId(parsed);
    } else {
      refresh();
    }
  });
}

for (const input of tableInputs) {
  input.addEventListener("paste", (event) => {
    const text = event.clipboardData.getData("text");
    const parsed = parseTableReference(text);

    if (parsed) {
      event.preventDefault();
      afterQualifiedTableId(parsed);
    }
  });
}

inputs.billingProject.addEventListener("input", refresh);

const WAITING_MESSAGE =
  "Opening Looker Studio in a new tab. Building your report copy can take " +
  "up to ~10 seconds and may briefly show an error page — don’t close it. " +
  "One exception: “This report isn’t shared with you” will not resolve by " +
  "waiting — see the note under the Create button.";

form.addEventListener("submit", (event) => {
  event.preventDefault();
  refresh();
  if (createLink.href) {
    setStatus(WAITING_MESSAGE, "waiting");
    window.open(createLink.href, "_blank", "noopener,noreferrer");
  }
});

createLink.addEventListener("click", (event) => {
  if (!createLink.href) {
    event.preventDefault();
    refresh();
    return;
  }
  setStatus(WAITING_MESSAGE, "waiting");
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
