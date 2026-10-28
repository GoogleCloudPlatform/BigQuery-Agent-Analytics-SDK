# Quickstart

`bigquery-agent-analytics-mod` records your coding-agent sessions in BigQuery—prompts, model answers, tool calls, token counts, and latency—in the background so your terminal never waits on uploads. Pick the setup path that matches how you work:

- [A. Your own sandbox](#a-your-own-sandbox) — try it in your own Google Cloud project in about 5 minutes.
- [B. Your team set it up](#b-your-team-set-it-up) — connect to a shared BigQuery table your administrator already created.
- [For administrators](#for-administrators) — set up a shared table, write-only credentials, and usage views for a team.

For all configuration options, event schemas, and redaction details, see [README.md](README.md) (especially [Privacy and security](README.md#privacy-and-security)).

## Before you start

If you have never installed a BigQuery Agent Analytics plugin before, skip straight to [A. Your own sandbox](#a-your-own-sandbox) or [B. Your team set it up](#b-your-team-set-it-up). Otherwise, run `claude plugin list` (or `/plugin` inside a session) to check what you already have:

- **Turn off `bigquery-agent-analytics-tracing` if enabled**: If the older settings-hook tracer (`bigquery-agent-analytics-tracing`) is active for the same table, disable it using the plugin ID shown by `claude plugin list` so every event isn't written twice:
  ```bash
  claude plugin disable bigquery-agent-analytics-tracing@bqaa-tracing
  # or, for earlier installs:
  claude plugin disable bigquery-agent-analytics-tracing@bigquery-agent-analytics-plugin
  ```
  Also remove any manual tracing hooks you previously added to `~/.claude/settings.json`.
- **Reusing a table from `bigquery-agent-analytics-tracing`?** That table has no `event_id` column. Inserts still succeed, but BigQuery drops the ID, so retries and backfills can't be deduplicated and deduplicated queries fail. Run `setup.sh` once against that dataset (it adds the column), or run:
  ```sql
  ALTER TABLE `YOUR_PROJECT.YOUR_DATASET.agent_events` ADD COLUMN IF NOT EXISTS event_id STRING;
  ```
- **Already using this marketplace?** Run `claude plugin marketplace update bqaa-tracing`, then `claude plugin update bigquery-agent-analytics-mod@bqaa-tracing`. If it says the mod is already at the latest version (`0.1.0`) while you need a newer build, uninstall it (`claude plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing`) and install it again with the same `--config` values. Then run `/reload-plugins` or start a new session.
- **Migrating from an older marketplace?** If your existing plugin ID ends in `@bigquery-agent-analytics-plugin`, follow [MIGRATION.md](MIGRATION.md).

## A. Your own sandbox

### 1. Prerequisites

- Claude Code CLI v2.1.296 or newer (`claude --version`) in a macOS or Linux terminal (Windows, the desktop app, and editor extensions are not tested)
- Google Cloud CLI (`gcloud` and `bq`)
- A Google Cloud project where you can create BigQuery datasets

### 2. Sign in to Google Cloud

```bash
gcloud auth login                         # for gcloud and bq CLI commands
gcloud auth application-default login     # for the mod's default token command
```

### 3. Create the BigQuery dataset and table

Replace `YOUR_PROJECT` with your real Google Cloud project ID:

```bash
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
git -C BigQuery-Agent-Analytics-SDK sparse-checkout set plugins/claude_code_mod
BQAA_PROJECT_ID=YOUR_PROJECT BQAA_DATASET=agent_analytics \
  ./BigQuery-Agent-Analytics-SDK/plugins/claude_code_mod/setup.sh
```

`setup.sh` creates the `agent_analytics` dataset, the `agent_events` table, and two daily usage views in `agent_analytics_reporting` (in a personal sandbox, you can ignore its closing team-access hint). A newly created table can take a few minutes to accept streaming inserts—rows wait in the meantime and are sent automatically.

### 4. Install the mod

Replace `YOUR_PROJECT` with your Google Cloud project ID:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=YOUR_PROJECT --config dataset=agent_analytics
```

The installer may print `11 userConfig options not yet set — ...`; that's fine because every other option has a default. If you already have a `claude` session open, run `/reload-plugins` or start a new session.

### 5. Verify and query your rows

Start a session and send a short prompt (for example, `"list the files here"`). In the first session after installing a new plugin version, a welcome card names your table and shows **✓ First rows landed in BigQuery** after a successful upload. The card may be absent in later sessions. In any session, wait a few seconds and run `/bqaa-status`: confirm the correct table (`BigQuery Agent Analytics mod: enabled, target YOUR_PROJECT.agent_analytics.agent_events (US)`), no `notice` line, and at least `1 ok` request. If the table was just created and you see `BigQuery cannot find the table`, wait a few minutes; queued rows retry automatically.

Then query your logged events in BigQuery (replace `YOUR_PROJECT` in both places):

```bash
bq --project_id=YOUR_PROJECT query --use_legacy_sql=false \
  'SELECT event_type, COUNT(*) AS events
   FROM `YOUR_PROJECT.agent_analytics.agent_events`
   WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
     AND JSON_VALUE(attributes, "$.writer.plugin") = "bigquery-agent-analytics-mod"
   GROUP BY event_type
   ORDER BY events DESC'
```

After one prompt, you should see `STATE_DELTA` (session start), `USER_MESSAGE_RECEIVED`, `INVOCATION_STARTING`, `LLM_REQUEST`, `LLM_RESPONSE`, `AGENT_RESPONSE`, and `INVOCATION_COMPLETED` (plus `TOOL_STARTING` and `TOOL_COMPLETED` if a tool ran).

## B. Your team set it up

### 1. Prerequisites and details from your admin

- **On your machine**: Claude Code CLI v2.1.296 or newer (`claude --version`, macOS or Linux terminal) and Google Cloud CLI (`gcloud`).
- **From your administrator**: the GCP `project`, `dataset`, and optional `accessTokenCommand` (typically `gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@THE_PROJECT.iam.gserviceaccount.com`). If your organization deployed [managed settings](docs/managed-settings.md), the mod is already enabled and configured for you—skip straight to Step 2.

### 2. Sign in to Google Cloud

- **Managed settings or a service-account impersonation `accessTokenCommand`**: run `gcloud auth login`.
- **Direct access with your own Google account (no `accessTokenCommand`)**: run `gcloud auth application-default login`.

### 3. Install the mod

Skip this step if `claude plugin list` already shows `bigquery-agent-analytics-mod` enabled. Otherwise, replace `THE_PROJECT`, `THE_DATASET`, and the service-account email with the values from your admin:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=THE_PROJECT --config dataset=THE_DATASET \
  --config "accessTokenCommand=gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@THE_PROJECT.iam.gserviceaccount.com"
```

Omit `--config "accessTokenCommand=..."` if your administrator did not give you one. Any `userConfig options not yet set` note from the installer is normal.

### 4. Verify

Start a session and send a prompt. If this is the first session for a newly installed plugin version, a welcome card may confirm **✓ First rows landed in BigQuery**; later sessions need not show it. In any session, run `/bqaa-status` and confirm your team's table, no `notice` line, and at least `1 ok` request. If your admin created the table in the last few minutes and you see `BigQuery cannot find the table`, wait a few minutes; queued rows retry automatically. You won't be able to query the raw `agent_events` table yourself—that's by design, since it holds everyone's prompts and tool output.

## If rows are not written

When rows cannot reach BigQuery, the mod buffers them on disk and shows the cause and fix in `/bqaa-status` and the banner above the prompt.

| Symptom or message | How to fix it |
| --- | --- |
| `no working Google credentials` | Do what the notice's "To fix" part says. Usually that's `gcloud auth application-default login` (default setup) or `gcloud auth login` (a team setup that impersonates a writer). If it names another token command, fix that command. If you set `BQAA_ACCESS_TOKEN`, set a fresh one or unset it. Rows wait in the meantime, and your next prompt retries right away. |
| `BigQuery denied the identity access to the dataset` (`403`) | Ask your admin for insert-only access (`bigquery.tables.get` and `bigquery.tables.updateData` on `agent_events`), then run `/bqaa-backfill` to replay buffered rows. |
| `BigQuery cannot find the dataset ...` or `BigQuery cannot find the table ...` (`404`) | Verify `project` and `dataset`, or run the `setup.sh` command shown in `/bqaa-status`. Newly created tables can take a few minutes to accept streaming inserts; queued rows send automatically once ready. |
| Duplicate events in BigQuery | Check whether both this mod and `bigquery-agent-analytics-tracing` (or manual hooks in your settings file) are enabled; if so, disable the tracer as shown in [Before you start](#before-you-start). With only the mod enabled, retries or backfills can still duplicate rows because `insertAll` deduplication is best-effort; deduplicate on `event_id` using the query in [What it writes](README.md#what-it-writes). |
| `Plugin "bigquery-agent-analytics-mod" not found in marketplace` | Your cached `bqaa-tracing` marketplace predates the mod. Run `claude plugin marketplace update bqaa-tracing` and install again. |

## For administrators

1. **Create the dataset, table, and views**: Run `setup.sh` (see [Step A.3](#3-create-the-bigquery-dataset-and-table)) in a dedicated GCP project where regular users do not hold project-wide BigQuery read roles. This creates `<dataset>.agent_events` and two aggregate views (`daily_usage_by_user`, `daily_usage_by_model`) in `<dataset>_reporting`.
2. **Create the writer service account and grant your two groups**: The bootstrap script lives in a separate repository and runs in a Python virtual environment with `google-cloud-bigquery`:
   ```bash
   git clone --depth 1 https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill
   python3 -m venv .venv && .venv/bin/pip install google-cloud-bigquery
   .venv/bin/python bigquery_agent_analytics_skill/plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py \
     --project YOUR_PROJECT --dataset agent_analytics --no-create-dataset --no-create-table \
     --service-account bqaa-row-writer --writer-role row-writer \
     --token-creator group:WRITERS@example.com --reader group:READERS@example.com
   ```
   Review the plan it prints, then run the same command again with `--execute` (if it stops on grants it cannot verify via Cloud Identity, audit them and add `--accept-unverified`). Verify isolation with `tests/access.live.sh`—see [docs/service-account-auth.md#set-it-up](docs/service-account-auth.md#set-it-up).
3. **Roll out to developers**: Share `project`, `dataset`, and `accessTokenCommand` (`gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@YOUR_PROJECT.iam.gserviceaccount.com`) for [Path B](#b-your-team-set-it-up), or deploy an organization policy with [docs/managed-settings.md](docs/managed-settings.md).
4. **Query usage and traces**: Point stakeholders to `<dataset>_reporting` (`daily_usage_by_user`, `daily_usage_by_model`) for daily adoption and token counts, and use [`sql/sample_queries.sql`](sql/sample_queries.sql) (filling in `{PROJECT}`, `{DATASET}`, and `{TABLE}`) and [docs/value.md](docs/value.md) for admin-level latency, tool-error, and trace queries.
