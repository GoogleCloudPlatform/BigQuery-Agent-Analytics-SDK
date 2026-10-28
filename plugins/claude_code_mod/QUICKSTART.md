# Quickstart

`bigquery-agent-analytics-mod` records your coding-agent sessions in BigQuery—prompts, model answers, tool calls, token counts, and latency—from the background so your terminal never waits on network requests. Pick the setup path that matches how you work:

- [A. Your own sandbox](#a-your-own-sandbox) — try it in your own Google Cloud project in about 5 minutes.
- [B. Your team set it up](#b-your-team-set-it-up) — connect to a shared BigQuery table your administrator already created.
- [For administrators](#for-administrators) — set up a shared table, write-only credentials, and usage views for a team.

For all configuration options, event schemas, and redaction details, see [README.md](README.md) (especially [Privacy and security](README.md#privacy-and-security)).

## Before you start

Run `claude plugin list` (or `/plugin` inside a session) to check your current setup:

- **Turn off the older shell-hook tracer if enabled**: If `bigquery-agent-analytics-tracing` is active for the same table, disable it so every event isn't written twice:
  ```bash
  claude plugin disable bigquery-agent-analytics-tracing@bqaa-tracing
  ```
  Also remove any manual tracing hooks you previously added to `~/.claude/settings.json`.
- **Update an existing marketplace checkout**: If you added the `bqaa-tracing` marketplace earlier, refresh it before installing so you get the current mod:
  ```bash
  claude plugin marketplace update bqaa-tracing
  ```
- **Migrating from an older marketplace?** If your existing plugin ID ends in `@bigquery-agent-analytics-plugin`, follow [MIGRATION.md](MIGRATION.md).

## A. Your own sandbox

### 1. Prerequisites

- Claude Code CLI v2.1.296 or newer (`claude --version`)
- Google Cloud CLI (`gcloud` and `bq`)
- A Google Cloud project where you can create BigQuery datasets

### 2. Sign in to Google Cloud

```bash
gcloud auth login
gcloud auth application-default login
```

### 3. Create the BigQuery dataset and table

```bash
git clone --depth 1 https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
BQAA_PROJECT_ID=YOUR_PROJECT BQAA_DATASET=agent_analytics \
  ./BigQuery-Agent-Analytics-SDK/plugins/claude_code_mod/setup.sh
```

`setup.sh` creates the `agent_analytics` dataset, the `agent_events` table, and two daily usage views in `agent_analytics_reporting`.

### 4. Install the mod

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=YOUR_PROJECT --config dataset=agent_analytics
```

If you already have a `claude` session open, run `/reload-plugins` or start a new session.

### 5. Verify and query your rows

Start `claude`, send a quick test prompt (for example, `"list the files here"`), wait about 5 seconds for the background batch to flush, and run `/bqaa-status`. You should see `BigQuery Agent Analytics mod: enabled, target YOUR_PROJECT.agent_analytics.agent_events (US)` and `requests: 1 ok` (or more).

Then query your logged events in BigQuery:

```bash
bq --project_id=YOUR_PROJECT query --use_legacy_sql=false \
  'SELECT event_type, COUNT(*) AS events
   FROM `YOUR_PROJECT.agent_analytics.agent_events`
   WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
     AND JSON_VALUE(attributes, "$.writer.plugin") = "bigquery-agent-analytics-mod"
   GROUP BY event_type
   ORDER BY events DESC'
```

## B. Your team set it up

### 1. What you need from your admin

Ask your administrator for the GCP `project`, `dataset`, and optional `accessTokenCommand`. If your organization deployed [managed settings](docs/managed-settings.md), the mod is already enabled and configured for you—skip straight to Step 2.

### 2. Sign in to Google Cloud

- **Managed settings or a service-account impersonation `accessTokenCommand`**: run `gcloud auth login`.
- **Direct access with your own Google account (no `accessTokenCommand`)**: run `gcloud auth application-default login`.

### 3. Install the mod

Skip this step if `claude plugin list` already shows `bigquery-agent-analytics-mod` enabled. Otherwise:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=THE_PROJECT --config dataset=THE_DATASET \
  --config "accessTokenCommand=THE_TOKEN_COMMAND"
```

Omit `--config "accessTokenCommand=..."` if your administrator did not provide one.

### 4. Verify

Start `claude`, send a prompt, wait 5 seconds, and run `/bqaa-status`. Confirm it shows `enabled` with your team's target table, no `notice` line, and `requests: 1 ok` (or more). Note that write-only team roles cannot run `SELECT` queries against `agent_events` directly.

## If rows are not written

When rows cannot reach BigQuery, the mod buffers them on disk and shows the cause and fix in `/bqaa-status` and the banner above the prompt.

| Symptom or message | How to fix it |
| --- | --- |
| `no working Google credentials` | Sign in again: run `gcloud auth login` when using managed settings or a custom `accessTokenCommand`, or `gcloud auth application-default login` when using default credentials. |
| `BigQuery denied the identity access to the dataset` (`403`) | Ask your admin for insert-only access (`bigquery.tables.get` and `bigquery.tables.updateData` on `agent_events`), then run `/bqaa-backfill` to replay buffered rows. |
| `BigQuery cannot find the dataset ...` or `BigQuery cannot find the table ...` (`404`) | Verify `project` and `dataset`, or run the `setup.sh` command shown in `/bqaa-status`. Newly created tables can take a couple of minutes to accept streaming inserts; queued rows send automatically once ready. |
| Duplicate events in BigQuery | Both this mod and `bigquery-agent-analytics-tracing` (or manual `~/.claude/settings.json` hooks) are enabled. Disable the older tracer as shown in [Before you start](#before-you-start). |
| `Plugin "bigquery-agent-analytics-mod" not found in marketplace` | Your cached `bqaa-tracing` marketplace predates the mod. Run `claude plugin marketplace update bqaa-tracing` and install again. |

## For administrators

1. **Create the dataset, table, and views**: Run `setup.sh` (see [Step A.3](#3-create-the-bigquery-dataset-and-table)) in a dedicated GCP project where regular users do not hold project-wide BigQuery read roles. This creates `<dataset>.agent_events` and two aggregate views (`daily_usage_by_user`, `daily_usage_by_model`) in `<dataset>_reporting`.
2. **Provision write-only and view-reader access**: Run [`setup_gcp_prereqs.py`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/main/plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py) with `--service-account bqaa-row-writer --writer-role row-writer` to grant developers insert-only impersonation (`bigquery.tables.get` and `bigquery.tables.updateData` only) and grant your reader group access strictly to `<dataset>_reporting`. See [docs/service-account-auth.md](docs/service-account-auth.md) for the full setup and `tests/access.live.sh` verification.
3. **Roll out to developers**: Share the `project`, `dataset`, and impersonation `accessTokenCommand` for [Path B](#b-your-team-set-it-up), or deploy an organization policy with [docs/managed-settings.md](docs/managed-settings.md).
4. **Query usage and traces**: Point stakeholders to `<dataset>_reporting` (`daily_usage_by_user`, `daily_usage_by_model`) for daily adoption and token metrics, and use [`sql/sample_queries.sql`](sql/sample_queries.sql) and [docs/value.md](docs/value.md) for admin-level latency, tool-error, and trace queries.
