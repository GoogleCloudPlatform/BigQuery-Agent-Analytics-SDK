# Quickstart

The mod records each coding-agent session as rows in a BigQuery table:
your prompts, the model's visible answers, tool calls with their arguments
and results, token counts and timings (the README's
[Privacy and security](README.md#privacy-and-security) says what is kept
and what is removed). Pick your path:

- [A. Your own sandbox](#a-your-own-sandbox): your own Google Cloud
  project, about ten minutes.
- [B. Your team set it up](#b-your-team-set-it-up): an administrator gave
  you a project, a dataset and a token command, or turned the mod on for
  you.
- [For administrators](#for-administrators): the table, the writers and the
  readers, set up once.

The [README](README.md) is the reference: every option, how it works, and
the limits.

**Checked with** host CLI 2.1.304 on macOS and Linux, in the terminal. Not
checked: Windows, the desktop app and the editor extensions.

## Before you start

Run `claude plugin list` (or `/plugin` in a session) for what is installed,
and `claude plugin marketplace list` for where each marketplace was added
from (`Source: GitHub` or `Source: Folder`). Look for:

- **`bigquery-agent-analytics-mod`, already enabled**: your organization
  turned it on for you, or you installed it earlier. Skip the install step,
  but still sign in, as in [B.2](#2-sign-in-to-google-cloud-1) (with managed
  settings, that is `gcloud auth login`), then go to its check. One
  installed from the marketplace the mod came from
  (`@bigquery-agent-analytics-plugin`) moves to this one as
  [MIGRATION.md](MIGRATION.md) shows.
- **`bigquery-agent-analytics-tracing`, enabled**: the settings-hook tracer.
  Turn it off for the table the mod writes to, or every event is written
  twice: `claude plugin disable bigquery-agent-analytics-tracing@bqaa-tracing`
  (`@bigquery-agent-analytics-plugin` where it came from the earlier
  marketplace). The same goes for the earlier settings-hook recipe (hooks
  pasted into your `~/.claude/settings.json` that run the tracer's
  scripts): this mod replaces it, so take those hooks out.
- **The marketplace `bqaa-tracing` already added** (for the settings-hook
  tracer, say): the install commands below use the copy it fetched then, and
  do not refresh it. Added before the mod was in it, it stops the install
  with `Plugin "bigquery-agent-analytics-mod" not found in marketplace`;
  added later, it installs that older copy. Update it first, and then skip
  the `marketplace add` below:

  ```bash
  claude plugin marketplace update bqaa-tracing
  ```

  If the mod is already installed from it, `claude plugin update` keeps the
  copy you have while the version number is the same ("already at the
  latest version"). After the update above, uninstall it and install it
  again, with the same `--config` options:

  ```bash
  claude plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing
  claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
    --config project=THE_PROJECT --config dataset=THE_DATASET
  ```

## A. Your own sandbox

### 1. What you need

- The host CLI (`claude --version`; checked with 2.1.304).
- The Google Cloud CLI (`gcloud`, which includes `bq`).
- A Google Cloud project where you may create BigQuery datasets.

### 2. Sign in to Google Cloud

```bash
gcloud auth login                         # for gcloud and bq: setup.sh and the query below
gcloud auth application-default login     # for the mod's default token command
```

### 3. Make the dataset

```bash
git clone --depth 1 https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
BQAA_PROJECT_ID=YOUR_PROJECT BQAA_DATASET=agent_analytics \
  ./BigQuery-Agent-Analytics-SDK/plugins/claude_code_mod/setup.sh
```

It makes the dataset, the events table and two usage views. BigQuery can
take a few minutes to accept rows into a table made moments ago: the mod's
rows wait, and are sent on their own then.

### 4. Install the mod

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=YOUR_PROJECT --config dataset=agent_analytics
```

It may say other options are not set yet: they all have defaults. If a
session is already open, run `/reload-plugins` there, or start a new one.

### 5. Check that it works

Start a session (`claude`), and keep it open for ten seconds or more:

- A card above the prompt says the mod is installed and names your table,
  and a few seconds later that the first rows landed in BigQuery.
- `/bqaa-status` starts with
  `BigQuery Agent Analytics mod: enabled, target YOUR_PROJECT.agent_analytics.agent_events (US)`,
  shows no `notice` line, and shows `requests: 1 ok` or more (the mod sends
  every 5 seconds).

That is the check. A session that ends within a few seconds leaves its rows
on disk, in a spill file, and the next session sends them 15 seconds in.

### 6. See your rows

Type one prompt, for example "list the files here". Then, in a shell (your
own project, so you may read the table):

```bash
bq --project_id=YOUR_PROJECT query --use_legacy_sql=false \
  'SELECT event_type, COUNT(*) AS n
   FROM `YOUR_PROJECT.agent_analytics.agent_events`
   WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
     AND JSON_VALUE(attributes, "$.writer.plugin") = "bigquery-agent-analytics-mod"
   GROUP BY event_type ORDER BY n DESC'
```

You see `USER_MESSAGE_RECEIVED` for the prompt, `INVOCATION_STARTING` and
`INVOCATION_COMPLETED` around the turn, `LLM_REQUEST` and `LLM_RESPONSE`
for the model's requests, `AGENT_RESPONSE` for its answer, `TOOL_STARTING`
and `TOOL_COMPLETED` if a tool ran, and `STATE_DELTA` for the session's
start. The session's end is written to disk when you leave, and sent by
your next session.

## B. Your team set it up

### 1. What you need

- The host CLI (`claude --version`; checked with 2.1.304).
- The Google Cloud CLI.
- From your administrator: the project and dataset, and the token command
  if they gave you one. With managed settings, they gave you nothing to
  type: the mod is already on, with a token command that impersonates the
  writer service account, so you sign in with `gcloud auth login` (step 2).

### 2. Sign in to Google Cloud

Once, as yourself:

- With managed settings, or a token command that impersonates a writer
  service account (`gcloud auth print-access-token --impersonate-service-account=...`,
  or a command your administrator named that runs it): `gcloud auth login`.
- Only with no token command at all, and your own access to the table:
  `gcloud auth application-default login`.

### 3. Install the mod

Skip this if `claude plugin list` already shows the mod enabled. Otherwise:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=THE_PROJECT --config dataset=THE_DATASET \
  --config "accessTokenCommand=THE TOKEN COMMAND"
```

Leave out the last line if you were given no token command.

### 4. Check that it works

As in [A.5](#5-check-that-it-works): the card says the first rows landed,
and `/bqaa-status` names your administrator's table, with no `notice` line
and `requests: 1 ok` or more.

You cannot read the rows yourself: a writer may insert rows and read none.
Your administrator, and the readers of the usage views, can.

## If rows are not written

The status line under the prompt, the band above it and `/bqaa-status` say
why, and how to fix it. Rows wait on your disk meanwhile, and are sent once
it is fixed.

| It says | What to do |
| --- | --- |
| no working Google credentials | Sign in again (step 2 of your path): `gcloud auth login` when your organization manages the mod or gave you a token command (the notice may name it as `bqaa-token` or `env`), and `gcloud auth application-default login` only without one. Or fix what the notice says about the token command. A grant made moments ago can take a minute to apply |
| BigQuery denied the identity access to the dataset | Ask your administrator for the row-writer grant on the table (`bigquery.tables.get` and `bigquery.tables.updateData`), never a role that reads rows such as `roles/bigquery.dataEditor`, then run `/bqaa-backfill` to send the rows that were refused |
| BigQuery cannot find the dataset (or the table) | Check the names with your administrator, or make it: the notice prints the whole `setup.sh` command, ready to run. A dataset made moments ago can take a few minutes to accept rows; they are sent on their own then |
| Every event appears twice in the table | The settings-hook tracer is on beside the mod: see [Before you start](#before-you-start) |
| At install: `Plugin "bigquery-agent-analytics-mod" not found in marketplace` | The marketplace `bqaa-tracing` was added before the mod was in it, and keeps that copy: `claude plugin marketplace update bqaa-tracing`, then install again (see [Before you start](#before-you-start)) |
| At install: "its source doesn't match its extraKnownMarketplaces entry" | `bqaa-tracing` was added from another source than the command names (`claude plugin marketplace list` says which): update that marketplace and install `bigquery-agent-analytics-mod@bqaa-tracing` from it, with no `--marketplace` |

## For administrators

1. **What you need:** the Google Cloud CLI, signed in with
   `gcloud auth login` (for `gcloud`, `bq` and the scripts) and
   `gcloud auth application-default login` (the bootstrap's steps that
   change the datasets' access); and Python 3 with the BigQuery client, in
   a virtual environment:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install google-cloud-bigquery
   .venv/bin/python -c "import google.cloud.bigquery"
   ```

   If pip tries to build `cryptography` from source and fails (it did with
   an Intel Python on an Apple-silicon Mac), take a built one:
   `.venv/bin/pip install --only-binary=cryptography google-cloud-bigquery`.

   The roles: project Owner, or Service Usage Admin, Service Account Admin,
   Role Administrator, Project IAM Admin, BigQuery Data Owner and BigQuery
   Job User.
2. **Make the dataset, table and views** with the mod's `setup.sh`, as in
   [A.3](#3-make-the-dataset). Keep it in a project where only the
   administrators hold project roles: a project Owner, or a BigQuery data
   role on the project, reads every row.
3. **Set up the writers and the readers, as two groups.** `setup.sh` prints
   the command when it is done. The bootstrap is still in the mod's source
   repository: from a checkout of it
   (`git clone https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill`):

   ```bash
   .venv/bin/python bigquery_agent_analytics_skill/plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py \
     --project YOUR_PROJECT --dataset agent_analytics --no-create-dataset --no-create-table \
     --service-account bqaa-row-writer --writer-role row-writer \
     --token-creator group:WRITERS --reader group:READERS
   ```

   It prints its plan; run it again with `--execute` (it checks the
   BigQuery client before it changes anything). It may stop on grants it
   could not rule out: Cloud Identity answers group membership only for
   Google Workspace Enterprise and Cloud Identity Premium accounts, so
   elsewhere every person's grant that reads the events data is listed,
   the project's Owners included. Check the list, then run again with
   `--accept-unverified`; a grant known to reach a reader, a token creator
   or everyone still stops it
   ([service-account auth](docs/service-account-auth.md) says what each
   flag keeps). The writer service
   account may only insert rows. The writers' group may impersonate it. The
   readers' group may query the two usage views, which it authorizes on the
   events dataset, with Job User on the project, and no access to the table.
   [Service-account auth](docs/service-account-auth.md) explains each step,
   CI (no key), offboarding, and what `user_id` can and cannot prove.
4. **Check the access** with the mod's `tests/access.live.sh`, as the
   writer and as a reader with no other access (the same guide says how).
5. **Roll it out:**
   - give people path B: the project, the dataset and the token command
     `gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@YOUR_PROJECT.iam.gserviceaccount.com`.
     Each signs in once with `gcloud auth login`;
   - or turn the mod on for everyone with
     [managed settings](docs/managed-settings.md): the destination, the token
     command and every option are pinned, and the settings-hook tracer is
     turned off. Each person still signs in once, with `gcloud auth login`
     (not `application-default`: the token command impersonates the writer).
6. **Point the readers at the results:** the two views in
   `<dataset>_reporting`, `daily_usage_by_user` and `daily_usage_by_model`.
   Readers are refused the table by design. Do not grant them the events
   dataset or table to make other queries run: that reads everyone's
   prompts and tool output.

   For those who may read the raw rows (the administrators), there are the
   queries in [`sql/sample_queries.sql`](sql/sample_queries.sql), and the
   analysis skill in the mod's source repository
   ([`bigquery-agent-analytics-skill`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/tree/main/bigquery-agent-analytics-skill)),
   for questions about error rates, latency, token use and tool failures.

Moving from the marketplace the mod came from
(`haiyuan-eng-google/bigquery_agent_analytics_skill`, ids ending in
`@bigquery-agent-analytics-plugin`), or from the settings-hook tracer: see
[MIGRATION.md](MIGRATION.md).
