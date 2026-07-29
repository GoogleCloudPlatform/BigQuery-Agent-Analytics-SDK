# Grafana Dashboard for BigQuery Agent Analytics

Visualize BQAA telemetry straight from BigQuery in Grafana, with nothing to
host. Works on the free tier of Grafana Cloud. It runs alongside the
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
  --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

Then create the typed views the panels query. These un-nest the JSON columns of
`agent_events` into typed columns:

```bash
bq-agent-sdk views create-all --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

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

**1. Point it at your data** — with no variables, the target is written into
every panel's SQL:

```bash
sed -e 's/YOUR_PROJECT_ID/my-gcp-project/g' \
    -e 's/YOUR_DATASET_ID/my_demo_dataset/g' \
    grafana/bqaa-public-demo.json > grafana/bqaa-public-demo.ready.json
```

Import `bqaa-public-demo.ready.json` and select your BigQuery data source. On a
custom view prefix, add `-e 's/adk_/my_prefix_/g'`.

**2. Be sure to cap the spend.** Set a [BigQuery custom
quota](https://cloud.google.com/bigquery/docs/custom-quotas) on the demo
project. 

**3. Enable public sharing** in the Grafana UI:

1. Open the dashboard → **Share**.
2. Choose **Public dashboard** (**Share externally** in newer Grafana).
3. Tick the acknowledgements → **Generate public URL**.
4. Copy the link. Pause or revoke it from the same tab whenever you like.

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
   map so its SQL is covered too.
3. **Changing what a filter applies to?** Update the [Variables](#variables)
   table above. It is the canonical statement, and everything else points at it.
4. **Conventions** — the `'___ALL___'` sentinel, the shared error predicate, the
   `HAVING COUNT(*) > 0` no-data contract — are documented in
   [`queries/README.md`](queries/README.md). Follow them so new panels behave
   like the existing ones.
