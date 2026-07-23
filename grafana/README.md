# Grafana Dashboard for BigQuery Agent Analytics

A free, low-friction alternative to the bespoke [`dashboard_v2/`](../dashboard_v2)
React app: visualize BQAA telemetry natively from BigQuery with
Grafana — including the free tier of Grafana Cloud — with nothing to host.

Grafana is a **parallel** visualization option, not a replacement. Both consume
the same data:

```
AI Agent app ──SDK──▶ BigQuery agent_events ──ViewManager──▶ typed views (adk_*)
                              │                                    │
                              ├──────────▶ dashboard_v2 (React)    │
                              └──────────▶ Grafana ◀───────────────┘
```

## What's in this directory

|           File            |                            Purpose                            |
| ------------------------- | ------------------------------------------------------------- |
| `bqaa-dashboard.json`     | The interactive dashboard — import this into Grafana.         |
| `queries/*.sql`           | Panel SQL, the **source of truth** (see `queries/README.md`). |
| `datasource.example.yaml` | Optional provisioning example for self-managed Grafana.       |


## Prerequisites

- A GCP project with BigQuery enabled and the BQAA SDK installed
  (`pip install bigquery-agent-analytics`).
- A Grafana instance ([Grafana Cloud Free](https://grafana.com/products/cloud/)).
  > **Important for new users:** You must install the **Google BigQuery** data source plugin (`grafana-bigquery-datasource`) in Grafana before BigQuery will appear as an available connector.

### 0. Service Account & Auth Setup

Grafana requires Google Cloud credentials to read your BigQuery data, using
either a dedicated service account or Application Default Credentials.

1. In the Google Cloud Console, go to **IAM & Admin → Service Accounts**.
2. Click **Create Service Account** (e.g., name it `grafana-bqaa-viewer`).
3. Grant it the following two roles on your project:
   - `BigQuery Data Viewer` (to read the tables and views)
   - `BigQuery Job User` (to execute the queries)
4. After creating the account, click on it, navigate to the **Keys** tab, and click **Add Key → Create new key**. 
5. Choose **JSON** and download the file. Keep this file secure; you will upload it to Grafana in Step 2.

## 1. Prepare the data

If you don't have real agent traffic yet, seed the standard synthetic corpus
(the multi-agent retail returns scenario):

```bash
bqaa seed-events --scenario retail-returns \
  --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

Then create the typed views the dashboard queries (`adk_llm_responses`,
`adk_tool_completions`, …):

```bash
bq-agent-sdk views create-all --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

The views un-nest the JSON columns of `agent_events` into typed columns, so
dashboard SQL reads `usage_prompt_tokens` instead of
`JSON_VALUE(content, '$.usage.prompt')`. `ViewManager` prefixes view names
with `adk_` by default; if you used a custom prefix, set the dashboard's
**View prefix** variable accordingly.

## 2. Connect Grafana to BigQuery

**Grafana Cloud:**

1. Create a free account at grafana.com and open your stack.
2. Navigate to **Connections → Add new connection**, search for the **Google BigQuery** plugin and click **Install**. (This must be done before adding it as a data source).
3. Once installed, click **Add new data source** from that same plugin page (or go to Connections → Data sources → Add data source).
4. Choose **Google JWT File** authentication and upload (or paste) the service-account JSON key you generated in Step 0.
5. Set your default project and click **Save & test**.

**Self-managed Grafana (Docker or Bare-Metal):**
You can provision the BigQuery data source automatically on startup using a YAML file. Create a copy of [`datasource.example.yaml`](datasource.example.yaml) (never commit your real service account key to version control) and inject your credentials. *(Note: Default login for a fresh local Grafana is `admin` / `admin`).*

- **Docker:** Copy the example, inject your credentials, then pass the plugin
  environment variable and mount your YAML file:
  ```bash
  cp grafana/datasource.example.yaml grafana/datasource.yaml
  docker run -d -p 3000:3000 \
    -e "GF_INSTALL_PLUGINS=grafana-bigquery-datasource" \
    -v /path/to/your/datasource.yaml:/etc/grafana/provisioning/datasources/datasource.yaml \
    grafana/grafana
  ```
- **Bare-Metal (Native):** Install the plugin via the CLI, copy your YAML to the provisioning folder, and restart the service:
  ```bash
  grafana-cli plugins install grafana-bigquery-datasource
  cp /path/to/your/datasource.yaml /etc/grafana/provisioning/datasources/
  systemctl restart grafana-server
  ```

## 3. Import the dashboard

1. **Dashboards → New → Import**.
2. Upload `bqaa-dashboard.json` (or paste its contents).
3. If and when prompted, select your BigQuery data source.
4. Set the dashboard variables at the top:
   - **GCP project** / **BigQuery dataset** / **Events table** - where the SDK
     writes (`agent_events` by default).
   - **View prefix** - `adk_` unless you customized `ViewManager`.
   - **Agent** - multi-select filter, populated from your data.
   - **Session** - drives the *Trace detail* panel.

You should see four rows: 
   - **Overview** (sessions, events, error rate, latency, volumes)
   - **LLM & FinOps** (tokens, latency percentiles, tokens by model, estimated cost)
   - **Tools** (invocations, latency, errors)
   - **Sessions & Traces** (session rollups and a per-session event timeline)

## 4. Sharing Publicly (Snapshots)

Because BigQuery is an enterprise data source that requires underlying authentication, Grafana Cloud Free does **not** allow you to share live, interactive BigQuery dashboards publicly. 

If you want to share your dashboard with external stakeholders who do not have a Grafana account, you must use the **Snapshot** feature:
1. In your loaded dashboard, click the **Share** button at the top right.
2. Select the **Snapshot** tab.
3. Set an expiration time (ranging from 1 hour to never expire) and a Snapshot name.
4. Click **Publish to snapshot.raintank.io** (or Local Snapshot).
5. Copy the generated link. 

This link creates a static, point-in-time image of your dashboard with all the current data hardcoded into it. Viewers will be able to see the charts without needing Grafana accounts or BigQuery credentials, but they will not be able to interact with the dropdown variables.
