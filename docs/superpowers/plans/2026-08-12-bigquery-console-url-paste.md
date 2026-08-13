# BigQuery Console URL Paste Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept strict BigQuery table URLs from Pantheon and public Google Cloud Console in the existing Looker Studio configurator paste flow.

**Architecture:** Add a pure allowlisted URL parser beside the existing qualified-ID parser, then route the existing paste and committed-input handlers through one table-reference boundary. Reuse all existing field assignment and validation behavior; add no network calls or dependencies.

**Tech Stack:** Browser-native JavaScript modules, `URL`, `URLSearchParams`, Node `assert`, static HTML.

## Global Constraints

- Accept only HTTPS URLs on `pantheon.corp.google.com` or `console.cloud.google.com` with exact path `/bigquery`.
- Require exactly one `ws` parameter and exactly one complete `!1m5!1m4!4m3!1sPROJECT!2sDATASET!3sTABLE` reference.
- Apply `PROJECT_RE`, `DATASET_RE`, and `TABLE_RE` to URL-derived components before recognizing the URL.
- Rejected URLs must not fill any other identifier field.
- Preserve all merged #405 qualified-ID, validation, and fallback behavior.
- Add no fetch, navigation, external dependency, or BigQuery Console URL decoding beyond the approved workspace pattern.

---

### Task 1: Pure BigQuery Console URL parsing

**Files:**
- Modify: `dashboard/looker_studio/tools/test_web_configurator.mjs`
- Modify: `dashboard/looker_studio/docs/configurator.mjs`

**Interfaces:**
- Consumes: `PROJECT_RE`, `DATASET_RE`, `TABLE_RE`, and `splitQualifiedTableId(value)`.
- Produces: `parseBigQueryConsoleTableUrl(value)`, `parseTableReference(value)`, and `parseTableReferenceForInput(value)`, each returning `{ project, dataset, table } | null`.

- [ ] **Step 1: Write failing parser tests**

Add literal assertions for the reported Pantheon URL, the equivalent public
Console URL, encoded `ws` markers, and rejection of HTTP, foreign host,
userinfo, non-default port, wrong path, missing/duplicate `ws`, incomplete or
duplicate marker sequences, and invalid components.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
node dashboard/looker_studio/tools/test_web_configurator.mjs
```

Expected: failure because `parseBigQueryConsoleTableUrl` and the unified
table-reference functions are not exported.

- [ ] **Step 3: Implement the minimal pure parser**

Use `new URL()` in a `try` block, exact protocol/host/path checks,
`searchParams.getAll("ws")`, one global match of the approved workspace
pattern, and the three existing identifier regexes. Route URL-shaped input
only through this parser; route non-URL input through
`splitQualifiedTableId()`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
node dashboard/looker_studio/tools/test_web_configurator.mjs
```

Expected: all existing and new parser assertions pass.

### Task 2: Browser paste and committed-input integration

**Files:**
- Modify: `dashboard/looker_studio/tools/test_web_configurator.mjs`
- Modify: `dashboard/looker_studio/docs/app.mjs`

**Interfaces:**
- Consumes: `parseTableReference(value)` and `parseTableReferenceForInput(value)` from Task 1.
- Produces: existing UI behavior extended to recognized Console URLs, with no new DOM API.

- [ ] **Step 1: Write failing DOM behavior tests**

For both allowed hosts, paste a literal table URL into each of `project`,
`dataset`, and `table`; assert all three fields and the ready status. Add a
committed-input case and rejected-URL cases asserting that no other field is
changed and the paste is not intercepted.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
node dashboard/looker_studio/tools/test_web_configurator.mjs
```

Expected: URL paste is not intercepted and the fields are not distributed.

- [ ] **Step 3: Route handlers through the unified parser**

Replace the paste handler's `splitQualifiedTableId(text)` call with
`parseTableReference(text)`. Replace the change handler's
`parseQualifiedTableIdForInput(value)` call with
`parseTableReferenceForInput(value)`. Keep `afterQualifiedTableId()` as the
single mutation and refresh path.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
node dashboard/looker_studio/tools/test_web_configurator.mjs
```

Expected: URL and existing qualified-ID UI cases pass.

### Task 3: User guidance and final verification

**Files:**
- Modify: `dashboard/looker_studio/docs/index.html`
- Modify: `tests/test_looker_studio_dashboard.py`

**Interfaces:**
- Consumes: the supported paste contract from Tasks 1 and 2.
- Produces: visible configurator guidance pinned by the existing dashboard test suite.

- [ ] **Step 1: Write the failing guidance assertion**

Add a behavioral source assertion that the project-field hint tells users they
may paste a fully qualified table ID or BigQuery Console table link into any
identifier field.

- [ ] **Step 2: Run the dashboard test and verify RED**

Run:

```bash
pytest -q tests/test_looker_studio_dashboard.py
```

Expected: failure because the guidance is absent.

- [ ] **Step 3: Add concise guidance**

Update `project-hint` in `docs/index.html` with one sentence describing both
accepted paste forms and the any-field behavior, without changing the three
fields as the source of truth.

- [ ] **Step 4: Run complete verification**

Run:

```bash
node dashboard/looker_studio/tools/test_web_configurator.mjs
pytest -q tests/test_looker_studio_dashboard.py
bash autoformat.sh
git diff --check
```

Expected: every command exits 0 with no failing test or formatting error.

- [ ] **Step 5: Commit and publish**

Stage only the design, plan, parser, browser integration, guidance, and tests;
commit as `feat: accept BigQuery Console table links`, push
`agent/403-console-url-paste` to the upstream repository, and open a draft PR
against `GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK:main` linking #403.
