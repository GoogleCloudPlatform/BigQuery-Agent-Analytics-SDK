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

## 0. Service Account & Auth Setup

Grafana requires Google Cloud credentials to read your BigQuery data, using
either a dedicated service account or Application Default Credentials.

1. In the Google Cloud Console, go to **IAM & Admin → Service Accounts**.
2. Click **Create Service Account** (e.g., name it `grafana-bqaa-viewer`).
3. Grant `BigQuery Job User` (`roles/bigquery.jobUser`) on the project so the
   account can execute queries. Grant `BigQuery Data Viewer`
   (`roles/bigquery.dataViewer`) only on the specific BQAA dataset that Grafana
   needs to read, not on the entire project.
4. After creating the account, click on it, navigate to the **Keys** tab, and click **Add Key → Create new key**. 
5. Choose **JSON** and download the file. Keep it outside the repository and
   secure; you will upload it to Grafana in Step 2.
   > **Note:** Because service account key filenames vary wildly depending on how they are generated (e.g., GCP Console vs. `gcloud` CLI), this repository's `.gitignore` globally ignores all new `*.json` files to protect against accidental credential leaks.

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

For `secureJsonData.privateKey`, use the PEM as an actual YAML multiline value
with real line breaks, as shown in the example. Do not paste the JSON
representation containing literal `\n` escape sequences. Prefer mounting the
key or injecting it from a secret manager instead of storing it in the
provisioning file.

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
4. Click **Dashboard Settings** (the gear icon at the top right), then go to
   **Variables**.
5. Select and update the hidden `project`, `dataset`, `table`, and
   `view_prefix` constants to match your environment. The default table is
   `agent_events`, and the default view prefix is `adk_`.
6. Use the visible **Agent** multi-select filter and **Session** selector at the
   top of the dashboard. **Session** drives the *Trace detail* panel.

You should see four rows: 
   - **Overview** (sessions, events, error rate, latency, volumes)
   - **LLM & FinOps** (tokens, latency percentiles, tokens by model, estimated cost)
   - **Tools** (invocations, latency, errors)
   - **Sessions & Traces** (session rollups and a per-session event timeline)

## 4. Sharing Publicly (Snapshots)

Because BigQuery is an enterprise data source that requires underlying authentication, Grafana Cloud Free does **not** allow you to share live, interactive BigQuery dashboards publicly. 

If you want to share your dashboard with external stakeholders who do not have a Grafana account, you must use the **Snapshot** feature. 

> **CRITICAL PRIVACY WARNING:** A snapshot is a public, point-in-time dashboard containing the visible data. While it strips the backend queries to prevent further interaction with the database, it **preserves all visible values AND the raw executed SQL strings** directly in the URL payload (including session IDs, error messages, and even your plain-text GCP Project ID and Dataset name embedded in the queries). While this method does allow you to easily share the dashboard, you must be aware that **any unauthenticated user on the internet can view this production data and infrastructure layout** if you publish it this way. Therefore, the data you snapshot must be strictly curated based on your intent. **DO NOT publish snapshots of real production telemetry.**

Before sharing a snapshot, you **MUST**:
1. Run `bqaa seed-events` into a dedicated, isolated demo dataset.
2. Update your dashboard's `dataset` constant to point strictly to this redacted demo dataset.
3. Verify every visible panel to ensure no sensitive prompts, responses, or identifiers exist.
4. Set a short expiration time (e.g., 1 hour) when creating the snapshot.
5. Open the generated snapshot link in an **Incognito window** and perform a final verification before sharing the link.

To create the snapshot:
1. In your loaded dashboard, click the **Share** button at the top right.
2. Select the **Snapshot** tab.
3. Set a short expiration time and a Snapshot name.
4. Click **Publish to snapshot.raintank.io** (or Local Snapshot).
5. Copy the generated link and verify it in Incognito.
