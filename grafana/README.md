# Grafana Dashboard for BigQuery Agent Analytics

Visualize BQAA telemetry straight from BigQuery in Grafana.
Works on the free tier of Grafana Cloud. It runs alongside the
[`dashboard_v2/`](../dashboard_v2) React app rather than replacing it — both
read the same data.

```
AI Agent app ──SDK──▶ BigQuery agent_events ──ViewManager──▶ typed views (adk_*)
                              │                                    │
                              ├──────────▶ dashboard_v2 (React)    │
                              └──────────▶ Grafana ◀───────────────┘
```

| File                      | Purpose                                                                            |
| ------------------------- | ---------------------------------------------------------------------------------- |
| `bqaa-dashboard.json`     | The dashboard. Import this into Grafana.                                           |
| `bqaa-public-demo.json`   | Stripped build for public sharing (see [Sharing publicly](#sharing-publicly)).     |
| `queries/*.sql`           | Panel SQL, the **source of truth** (see [`queries/README.md`](queries/README.md)). |
| `datasource.example.yaml` | Provisioning example for self-managed Grafana.                                     |

## Setup

### Quick start (local, one command)

On macOS or Linux with `gcloud auth application-default login` already run:

```bash
python3 grafana/run_local.py --project YOUR_PROJECT --dataset YOUR_DATASET
```

That downloads a pinned Grafana (cached and checksum-verified after the
first run), installs the pinned BigQuery plugin, provisions the datasource
against your Application Default Credentials (no service-account key needed
for local evaluation), fills in all six dashboard constants, creates the
prefixed views when `bq-agent-sdk` is on PATH, and prints the dashboard
URL. The instance binds to **127.0.0.1 only**, and the generated datasource
keeps the example file's **100 MB per-query `MaxBytesBilled` cap**
(`--max-bytes-billed` to change it). Datasets outside the `US` multi-region
work by default — the job location is selected automatically; pass
`--processing-location EU` (or a region) to pin it. `--sa-key key.json`
switches to the JWT auth documented below and writes the credential file
with `0600` permissions; `--stop` tears it down; everything generated lives
in the disposable, gitignored `grafana/.local/`. Production setups should
still follow the full steps below with a scoped service account.

> **Known pitfalls if you run Grafana your own way instead:**
> the current plugin requires **Grafana ≥ 11.6.11** (its
> `grafanaDependency` also accepts 12.0.10+/12.1.7+/12.2.5+), and a failed
> background plugin preinstall can break **all** plugin loading with an
> opaque `react/jsx-runtime` 404 on every panel — launch with
> `GF_PLUGINS_PREINSTALL_DISABLED=true` to rule that out. The quick-start
> script handles both.

### 1. Check prerequisites

- A GCP project with the SDK installed (`pip install bigquery-agent-analytics`)
  and **both** the BigQuery API and Cloud Resource Manager API enabled:

  ```bash
  gcloud services enable bigquery.googleapis.com \
    cloudresourcemanager.googleapis.com --project YOUR_PROJECT
  ```

- A Grafana instance ([Grafana Cloud Free](https://grafana.com/products/cloud/)
  works) with the **Google BigQuery** data source plugin
  (`grafana-bigquery-datasource`) installed. BigQuery does not appear as a
  connector until the plugin is there.

### 2. Create a service account

1. **IAM & Admin → Service Accounts → Create Service Account** (e.g.
   `grafana-bqaa-viewer`).
2. Grant `BigQuery Job User` (`roles/bigquery.jobUser`) on the project, and
   `BigQuery Data Viewer` (`roles/bigquery.dataViewer`) **on the BQAA dataset
   only**, not the whole project.
3. **Keys → Add Key → Create new key → JSON**, and download it.

> **Keep the key out of the repo.** `.gitignore` only covers new `*.json`
> files inside `grafana/`. A key saved anywhere else can be committed by
> accident.

### 3. Prepare the data

No real traffic yet? Seed a synthetic dataset:

```bash
bqaa seed-events --scenario retail-returns \
  --project-id YOUR_PROJECT --dataset-id YOUR_DATASET \
  --events-table YOUR_TABLE --sessions 100
```

`YOUR_TABLE` defaults to `agent_events`; pass `--events-table` only if you want
a different name.

Smoke-test the seed — an empty table looks exactly like a broken dashboard:

```bash
bq query --use_legacy_sql=false \
  'SELECT COUNT(*) AS events, MAX(timestamp) AS latest
   FROM `YOUR_PROJECT.YOUR_DATASET.YOUR_TABLE`'
```

Expect a non-zero count. If `latest` falls outside the dashboard's time range,
every panel will read "No data".

Then create the typed views the panels query. These un-nest the JSON columns of
`agent_events` into typed columns:

```bash
bq-agent-sdk views create-all --project-id YOUR_PROJECT --dataset-id YOUR_DATASET \
  --table-id YOUR_TABLE
```

`--table-id` must match the dashboard's `table` constant (step 5), or the views
and the panels will read different tables.

`ViewManager` prefixes them with `adk_` by default. Used a custom prefix? Set
the dashboard's **View prefix** variable to match in step 5.

### 4. Connect Grafana to BigQuery

**Grafana Cloud**

1. **Connections → Add new connection**, search **Google BigQuery**, click
   **Install**.
2. **Add new data source** from that plugin page.
3. Choose **Google JWT File** auth and upload the JSON key from step 2.
4. Set your default project and **Save & test**.

**Self-managed (Docker or bare metal)**

Copy [`datasource.example.yaml`](datasource.example.yaml), inject your
credentials, and provision it at startup. Never commit the real key — mount it
or pull it from a secret manager. For `secureJsonData.privateKey`, use a real
YAML multiline value with real line breaks as shown in the example, not the JSON
form with literal `\n` escapes.

```bash
# Docker
cp grafana/datasource.example.yaml grafana/datasource.yaml
docker run -d -p 3000:3000 \
  -e "GF_INSTALL_PLUGINS=grafana-bigquery-datasource" \
  -v /path/to/your/datasource.yaml:/etc/grafana/provisioning/datasources/datasource.yaml \
  grafana/grafana
```

```bash
# Bare metal
grafana-cli plugins install grafana-bigquery-datasource
cp /path/to/your/datasource.yaml /etc/grafana/provisioning/datasources/
systemctl restart grafana-server
```

(Default login for a fresh local Grafana is `admin` / `admin`.)

### 5. Import the dashboard

1. **Dashboards → New → Import**, upload `bqaa-dashboard.json`.
2. Select your BigQuery data source if prompted.
3. **Dashboard Settings → Variables**: set the hidden `project`, `dataset`,
   `table`, and `view_prefix` constants. Defaults are `agent_events` and `adk_`.
4. While you are there, set the two rates that drive the **Estimated cost**
   panel — see [Cost variables](#cost-variables). They ship as placeholders.

> **Leave all six of those variables set to `Constant`.** Switching
> `project`, `dataset`, `table`, or `view_prefix` to **Textbox** opens the
> dashboard to SQL injection, letting any viewer query arbitrary datasets. The
> two pricing constants are interpolated raw into arithmetic, so a **Textbox**
> there lets a crafted URL inject text into the cost expression.

You should now see four rows: **Overview**, **LLM & FinOps**, **Tools &
Execution**, and **Sessions & Traces**.

## Variables

Use the **Agent**, **User ID**, **Event Type**, and **Session** multi-selects at
the top. All four default to **All** and are independent — they do not cascade.

**This table is the canonical statement of filter scope.** Row titles, panel
tooltips, and `queries/*.sql` headers point back here; when they disagree, this
table wins.

| Filter         | Source                                             | Applies to                                                       | Ignored by      |
| -------------- | -------------------------------------------------- | ---------------------------------------------------------------- | --------------- |
| **Agent**      | [`var_agent.sql`](queries/var_agent.sql)           | All panels                                                       | None            |
| **User ID**    | [`var_user_id.sql`](queries/var_user_id.sql)       | All panels                                                       | None            |
| **Event Type** | [`var_event_type.sql`](queries/var_event_type.sql) | Events over time, Events by agent, Recent sessions, Trace detail | Everything else |
| **Session**    | [`var_session_id.sql`](queries/var_session_id.sql) | All panels                                                       | None            |

**Agent, User ID, and Session cap at 1000 options** each (Event Type is
uncapped — the SDK emits a small fixed set). All three accept custom values, so
anything truncated is still reachable: type or paste it in, or narrow the time
range.

### Reading the panels

- **"No data" is ambiguous.** Check the Overview stats: real numbers mean a
  genuinely clean window, **No matching data** means your filters contradict.
- **Recent sessions** lists 250 sessions; its `*_in_window` columns cover the
  whole session, so they can exceed the Overview stats when a filter is active.
  Click a `session_id` to pin it into **Session** and drive **Trace detail**.
- **Top error messages** only counts errors that carry a message — a subset of
  **Errors over time**.
- **Events by agent** honors **Event Type**; events with no `agent` group under
  `unknown`.
- **Trace detail** is the heaviest query. Pin a session before widening the
  default **Last 24 hours** range.

Every panel filters on `timestamp`, and `agent_events` is partitioned on it
(`bqaa seed-events` sets this up — partition your own table the same way), so
the time picker prunes partitions rather than scanning them. A narrower range is
genuinely cheaper, not just faster.

Per-panel semantics live in the panel tooltips and
[`queries/README.md`](queries/README.md).

### Cost variables

The SDK never records a price, so the **Estimated cost** panel derives dollars
from token counts. Two hidden constants supply the rates, both in **USD per
1,000,000 tokens** — the unit model price lists publish:

| Variable                     | Label                            | Default | Applies to                |
| ---------------------------- | -------------------------------- | ------- | ------------------------- |
| `price_per_1m_input_tokens`  | Input price (USD per 1M tokens)  | `1.25`  | `usage_prompt_tokens`     |
| `price_per_1m_output_tokens` | Output price (USD per 1M tokens) | `5.00`  | `usage_completion_tokens` |

Both defaults are **placeholders**. Change them in **Dashboard Settings →
Variables**, not in [`estimated_cost.sql`](queries/estimated_cost.sql). They are
a single blended rate per direction, so a dashboard spanning several models with
different prices reports an approximation — token counts remain the exact
signal.

Keep them as `constant` variables with `skipUrlSync`. They are interpolated raw
into arithmetic (numbers, not string literals), so only someone who can already
edit the dashboard can change them. A URL cannot.

## Sharing publicly

**Public Dashboard spike:** Grafana's native **Public dashboards** are fully
supported, including on Grafana Cloud Free — Option A is the recommended path.
Both options need a **dedicated demo dataset**, never production telemetry.

### Option A — the public demo dashboard

[`bqaa-public-demo.json`](bqaa-public-demo.json) is a stripped build of the main
dashboard: no template variables, no time-group macros, hardcoded cost rates, a
locked **Last 72 hours** range with the time picker hidden, and no **Trace
detail** panel. Not editable, no auto-refresh, fixed to UTC.

Its queries are static — no variables to interpolate — and hardcode the 72-hour
window in the SQL, not the time picker, so an anonymous viewer cannot widen the
scan from a URL. The window is half-open: `timestamp >=
TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 72 HOUR) AND timestamp <
CURRENT_TIMESTAMP()`. They live in
[`queries/public-demo/`](queries/README.md#the-public-demo-build) and are
CI-checked against the panels, same as the interactive build. The check is a
text-level lint: it catches a dropped bound or a real table path, not SQL
written to slip past it. Read these queries yourself before you point a public
dashboard at real data.

**1. Point it at your data** — with no variables, the target is written into
every panel's SQL:

```bash
sed -e 's/YOUR_PROJECT_ID/my-gcp-project/g' \
    -e 's/YOUR_DATASET_ID/my_demo_dataset/g' \
    -e 's/agent_events/my_demo_table/g' \
    grafana/bqaa-public-demo.json > grafana/bqaa-public-demo.ready.json
```

The third substitution only matters if your events table is not named
`agent_events` — put your real table name in place of `my_demo_table`, or drop
the line. If you generated your typed views using a custom table prefix instead
of the default `adk_`, simply append `-e 's/adk_/my_prefix_/g'` to your command.

Import `bqaa-public-demo.ready.json` and select your BigQuery data source, see the
next few steps for details on setting up the data source.

**2. Give it a demo-only service account.** A public dashboard queries BigQuery
as whatever account the data source holds, for anyone with the link. Do not
reuse the account from [step 2](#2-create-a-service-account): create a separate
one with `BigQuery Job User` on the demo project and `BigQuery Data Viewer`
**on the demo dataset alone**. Then treat its JSON key as a live credential —
rotate it on a schedule, and delete the downloaded file (and any old keys in
**IAM → Service Accounts → Keys**) once Grafana has it.

**3. Be sure to cap the spend.** There are two primary ways, it would be wise to
set **both** as they bound different things:

- **Max bytes billed** (**Connections → your BigQuery data source → Max bytes
  billed**) caps **each query**, not how many run. Size it above what the
  heaviest panel - **Recent sessions** - scans; the query editor prints the
  estimate.
- A [BigQuery custom quota](https://cloud.google.com/bigquery/docs/custom-quotas) on the demo
  project acts as an approximate daily cost safeguard for on-demand pricing
  models, making it essential for limiting surprise daily expenses.

**4. Enable public sharing** in the Grafana UI:

1. Open the dashboard → **Share**.
2. Choose **Public dashboard** (**Share externally** in newer Grafana).
3. Tick the acknowledgements → **Generate public URL**.
4. Copy the link. Pause or revoke it from the same tab whenever you like.

**5. Open the link in an incognito window** before you hand it out. Logged in
you see it as an editor; incognito shows what the internet gets. Check every
panel there — a rejected query still prints its normal empty-state text, so
watch for the small red corner indicator and confirm the Overview stats show
real numbers.

**Pricing:** the **Estimated cost** panel hardcodes `1.25` and `5.00` USD per 1M
tokens in its SQL — same unit as the [cost variables](#cost-variables). Edit the
two literals to match your models.

> **This build is not anonymization.** **Recent sessions** shows session and
> user IDs; **Top error messages** and **Tool errors** show raw error strings.
> Check every panel before you share the link.

### Option B — snapshots

> **PRIVACY WARNING:** A snapshot is a public, point-in-time copy. It strips
> the backend queries but preserves **every visible value and the raw executed
> SQL** in the URL payload: session IDs, error messages, and your plain-text GCP
> project ID and dataset name. Anyone on the internet can read that. **Do not
> snapshot real production telemetry.**

Before sharing you **must**:

1. Seed `bqaa seed-events` into a dedicated, isolated demo dataset.
2. Point the dashboard's `dataset` constant at that demo dataset.
3. Check every visible panel for sensitive prompts, responses, or identifiers.
4. Set a short expiration (an hour, say).
5. Open the link in an incognito window and verify before sharing.

Then: **Share → Snapshot**, set a name and expiration, **Publish to
snapshot.raintank.io** (or Local Snapshot), and copy the link.

## Extending the dashboard

Grafana has no "include SQL from file" mechanism, so each panel embeds a copy of
its query. That makes [`queries/*.sql`](queries/README.md) the source of truth:

1. **Edit the `.sql` file first**, then paste the result into the matching panel
   in `bqaa-dashboard.json`. A change to one without the other is incomplete —
   `scripts/check_grafana_queries_sync.py` diffs them in CI and will fail.
2. **Adding a panel?** Register its panel ID in that script's `PANEL_QUERIES`
   map so its SQL is covered too. If you also port the panel into
   [`bqaa-public-demo.json`](#option-a--the-public-demo-dashboard), save its
   demo SQL under `queries/public-demo/` and register the ID in
   `PUBLIC_DEMO_PANEL_QUERIES`. Both maps are exact in both directions: an
   unregistered panel, or an unregistered `.sql` file, fails CI.
3. **Changing what a filter applies to?** Update the [Variables](#variables)
   table above. It is the canonical statement, and everything else points at it.
4. **Conventions** — the `'___ALL___'` sentinel, the shared error predicate, the
   `HAVING COUNT(*) > 0` no-data contract — are documented in
   [`queries/README.md`](queries/README.md). Follow them so new panels behave
   like the existing ones.
