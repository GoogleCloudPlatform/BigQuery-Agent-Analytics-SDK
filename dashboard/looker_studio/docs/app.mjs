import {
  BILLING_PROJECT_MESSAGE,
  DEFAULT_PROFILE,
  PROFILES,
  PROJECT_RE,
  buildDashboardUrl,
  buildSetupUrl,
  resolveProfileConfig,
  validateQualifiedTableId,
} from "./configurator.mjs";

const form = document.querySelector("#configurator");
const createLink = document.querySelector("#create-dashboard");
const copyButton = document.querySelector("#copy-link");
const checklistButton = document.querySelector("#copy-checklist");
const checklist = document.querySelector("#security-checklist");
const status = document.querySelector("#form-status");
const tableIdInput = document.querySelector("#table-id");
const tableIdError = document.querySelector("#table-id-error");
const advancedSettings = document.querySelector("#advanced-settings");
const billingInput = document.querySelector("#billing-project");
const billingError = document.querySelector("#billing-project-error");
// Surface toggle (ADK Agents / BQCA Prompt & Response Logging). Optional:
// every use is guarded so a page or harness without the toggle still runs.
const profileButtons = new Map(
  PROFILES.map((id) => [id, document.querySelector(`#profile-${id}`)]),
);
const query = new URLSearchParams(window.location.search);

function normalizeProfile(value) {
  const id = String(value ?? "").trim().toLowerCase();
  return PROFILES.includes(id) ? id : null;
}

// The surface a page shows without a ?profile= override: the /bqca/ page
// declares it on <html>; its path is the fallback signal.
function pageDefaultProfile() {
  const root = document.documentElement;
  const declared =
    normalizeProfile(root?.getAttribute?.("data-bqca-default-profile")) ??
    normalizeProfile(root?.dataset?.bqcaDefaultProfile);
  if (declared) {
    return declared;
  }
  const path = window.location?.pathname ?? "";
  return /\/bqca(?:\/(?:index\.html)?)?$/.test(path) ? "bqca" : DEFAULT_PROFILE;
}

const defaultProfile = pageDefaultProfile();
let activeProfile = normalizeProfile(query.get("profile")) ?? defaultProfile;
let activeProfileConfig = resolveProfileConfig(activeProfile);

function applyProfile(profile) {
  activeProfile = profile;
  activeProfileConfig = resolveProfileConfig(profile);
  document.documentElement?.setAttribute?.("data-bqaa-profile", profile);
  for (const [id, button] of profileButtons) {
    button?.setAttribute?.("aria-pressed", String(id === profile));
  }
  for (const element of document.querySelectorAll?.("[data-profile-only]") ?? []) {
    element.hidden = element.dataset?.profileOnly !== profile;
  }
  tableIdInput.setAttribute(
    "placeholder",
    `my-project.my_dataset.${activeProfileConfig.defaultTable}`,
  );
}

// Keeps the address bar shareable after a toggle without reloading; the
// toggle state stays authoritative if the History API is unavailable.
function syncProfileQuery(profile) {
  try {
    const url = new URL(window.location.href);
    if (profile === defaultProfile) {
      url.searchParams.delete("profile");
    } else {
      url.searchParams.set("profile", profile);
    }
    window.history?.replaceState?.(window.history.state, "", url.toString());
  } catch {
    // Ignore: an unshareable address bar must never block configuration.
  }
}

// #448 field state. `derived` is the parsed triple behind the last valid
// field value; `lastValidRaw` is that value verbatim, so any mutation away
// from it is detected synchronously. `revealTableErrors` implements the
// validation-timing contract: paste and setup-link prefill validate
// immediately; manual entry first reports errors on blur or an attempted
// action; once touched or invalid, every input revalidates.
let derived = null;
let lastValidRaw = null;
let revealTableErrors = false;
// Bumped on every state mutation and at the start of every async copy, so a
// clipboard completion that is no longer current cannot restore a stale
// status over what the user's later edit cleared (#449 review).
let statusEpoch = 0;

function setStatus(message, kind = "") {
  status.textContent = message;
  status.dataset.kind = kind;
}

function disableActions() {
  createLink.removeAttribute("href");
  createLink.setAttribute("aria-disabled", "true");
  copyButton.disabled = true;
}

function clearTableError() {
  tableIdInput.removeAttribute("aria-invalid");
  tableIdError.textContent = "";
}

function showTableError(message) {
  tableIdInput.setAttribute("aria-invalid", "true");
  tableIdError.textContent = message;
}

function clearBillingError() {
  billingInput.removeAttribute("aria-invalid");
  billingError.textContent = "";
}

function showBillingError(message) {
  billingInput.setAttribute("aria-invalid", "true");
  billingError.textContent = message;
  // The override lives inside the Advanced disclosure: an error written
  // into a closed <details> would leave both actions disabled with no
  // visible explanation, so surface it. Correction never auto-closes.
  advancedSettings.open = true;
}

// The single normalization both validation and URL construction consume: a
// whitespace-only override must behave exactly like blank (billing the
// project segment), never reach validateConfiguration as truthy text.
function billingOverride() {
  return billingInput.value.trim();
}

// A blank override bills the project segment of the fully qualified ID, so
// blank is always valid; anything else must be a project ID.
function billingOverrideIsValid() {
  const value = billingOverride();
  if (!value || PROJECT_RE.test(value)) {
    clearBillingError();
    return true;
  }
  showBillingError(BILLING_PROJECT_MESSAGE);
  return false;
}

function refresh() {
  statusEpoch += 1;
  let parsed;
  try {
    parsed = validateQualifiedTableId(tableIdInput.value, activeProfileConfig);
    clearTableError();
  } catch (error) {
    derived = null;
    lastValidRaw = null;
    disableActions();
    setStatus("");
    if (revealTableErrors) {
      showTableError(error.message);
    } else {
      clearTableError();
    }
    billingOverrideIsValid();
    return;
  }
  derived = parsed;
  lastValidRaw = tableIdInput.value;
  // #448 decision 3: a valid table ID alone is not actionable — the billing
  // override must be blank or valid too, and an invalid override keeps the
  // parsed triple while disabling both actions.
  if (!billingOverrideIsValid()) {
    disableActions();
    setStatus("");
    return;
  }
  try {
    createLink.href = buildDashboardUrl({
      ...derived,
      billingProject: billingOverride(),
      profile: activeProfile,
    });
    createLink.removeAttribute("aria-disabled");
    copyButton.disabled = false;
    const readySuffix =
      activeProfile === "bqca"
        ? " Opens the dedicated 7-page tool-free BQCA Looker Studio template (ds0); you can also export sql/bqca_events_v1.sql.tmpl (--custom-sql-out) or run the Self-Hosted Streamlit BQCA Dashboard (dashboards/streamlit/)."
        : "";
    setStatus(
      `Ready for ${derived.project}.${derived.dataset}.${derived.table}.${readySuffix}`,
      "ready",
    );
  } catch (error) {
    // Unreachable through the field validators; keep the page honest if
    // the template configuration itself is broken.
    derived = null;
    lastValidRaw = null;
    disableActions();
    setStatus(error.message, "error");
  }
}

tableIdInput.addEventListener("input", () => {
  if (tableIdInput.value !== lastValidRaw) {
    // Fail closed on every mutation away from the last valid value: the
    // derived triple, both actions, and any prior Ready/status announcement
    // are revoked immediately; only error *presentation* may wait for the
    // validation trigger below.
    derived = null;
    disableActions();
    setStatus("");
  }
  refresh();
});

tableIdInput.addEventListener("change", () => {
  revealTableErrors = true;
  refresh();
});

tableIdInput.addEventListener("paste", (event) => {
  const text = event.clipboardData.getData("text");
  revealTableErrors = true;
  // The default paste inserts at the selection, so an invalid fragment
  // could merge with a previous Ready value into a DIFFERENT valid ID and
  // silently retarget the dashboard (#449 review). Always take over: the
  // field becomes either the normalized valid ID or the complete raw
  // clipboard text — never a splice — and validates immediately.
  event.preventDefault();
  let parsed = null;
  try {
    parsed = validateQualifiedTableId(text, activeProfileConfig);
  } catch {
    parsed = null;
  }
  tableIdInput.value = parsed
    ? `${parsed.project}.${parsed.dataset}.${parsed.table}`
    : text;
  if (tableIdInput.value !== lastValidRaw) {
    derived = null;
    disableActions();
    setStatus("");
  }
  refresh();
});

billingInput.addEventListener("input", refresh);

const WAITING_MESSAGE =
  "Opening Looker Studio in a new tab. Building your report copy can take " +
  "up to ~10 seconds and may briefly show an error page — don’t close it. " +
  "One exception: “This report isn’t shared with you” will not resolve by " +
  "waiting — see the note under the Create button.";

// The attempted-action path (#448 decision 1): reveal deferred errors,
// revalidate, and open exactly one tab when actionable. Reached by the
// form's submit event and — because this form has two text inputs and no
// native submit control, so browsers never run implicit submission — by an
// explicit Enter bridge on both fields (#449 review, P1).
function attemptCreate() {
  revealTableErrors = true;
  refresh();
  if (createLink.href) {
    // Invalidate any pending clipboard completion: the provisioning
    // warning must not be overwritten by an older copy resolving late
    // (#449 review).
    statusEpoch += 1;
    setStatus(WAITING_MESSAGE, "waiting");
    window.open(createLink.href, "_blank", "noopener,noreferrer");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  attemptCreate();
});

for (const input of [tableIdInput, billingInput]) {
  input.addEventListener("keydown", (event) => {
    // Only a discrete, non-composing Enter is an attempted action: a held
    // key repeat must not open tab after tab, and Enter committing an IME
    // composition (isComposing, or legacy keyCode 229) is text entry, not
    // activation (#449 review).
    if (
      event.key !== "Enter" ||
      event.repeat ||
      event.isComposing ||
      event.keyCode === 229
    ) {
      return;
    }
    event.preventDefault();
    attemptCreate();
  });
}

createLink.addEventListener("click", (event) => {
  if (!createLink.href) {
    event.preventDefault();
    revealTableErrors = true;
    refresh();
    return;
  }
  statusEpoch += 1;
  setStatus(WAITING_MESSAGE, "waiting");
});

copyButton.addEventListener("click", async () => {
  if (!derived) {
    return;
  }
  const epoch = ++statusEpoch;
  try {
    const setupUrl = buildSetupUrl(
      { ...derived, billingProject: billingOverride(), profile: activeProfile },
      window.location.href,
      { pageDefaultProfile: defaultProfile },
    );
    await navigator.clipboard.writeText(setupUrl);
    if (epoch === statusEpoch) {
      setStatus("Setup link copied. It contains identifiers, never credentials.", "ready");
    }
  } catch (error) {
    if (epoch === statusEpoch) {
      setStatus(error.message || "Could not copy the setup link.", "error");
    }
  }
});

checklistButton.addEventListener("click", async () => {
  const epoch = ++statusEpoch;
  const text = [...checklist.querySelectorAll("li")]
    .map((item, index) => `${index + 1}. ${item.textContent.trim()}`)
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    if (epoch === statusEpoch) {
      setStatus("Security checklist copied.", "ready");
    }
  } catch {
    if (epoch === statusEpoch) {
      setStatus("Could not copy the security checklist.", "error");
    }
  }
});

// The surface is applied before any prefill so validation and both links
// target it from the first render. Switching keeps the typed table ID and
// revalidates it; a pristine field stays pristine.
applyProfile(activeProfile);
for (const [id, button] of profileButtons) {
  button?.addEventListener?.("click", () => {
    if (id === activeProfile) {
      return;
    }
    applyProfile(id);
    syncProfileQuery(id);
    refresh();
  });
}

// Setup-link prefill keeps the existing three-parameter contract: all three
// identifier parameters compose the fully qualified ID and validate
// immediately; anything less leaves the field pristine. An optional
// `profile` parameter (read above) selects the surface.
if (query.has("billingProject")) {
  billingInput.value = query.get("billingProject");
}
const prefill = ["project", "dataset", "table"].map((name) => query.get(name));
if (prefill.every((part) => part)) {
  tableIdInput.value = prefill.join(".");
  revealTableErrors = true;
  refresh();
} else {
  // Pristine: empty field, no error, actions disabled, no status.
  disableActions();
}

// Written only at runtime; the browser smoke test asserts its presence in
// the live DOM and its absence from the static HTML, proving this module
// actually executed (the pre-#448 proof — an initial validation error — no
// longer exists in the pristine state).
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
