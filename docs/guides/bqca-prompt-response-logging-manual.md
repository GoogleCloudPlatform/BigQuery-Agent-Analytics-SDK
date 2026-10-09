# BigQuery Conversational Analytics (BQCA) Prompt & Response Logging
## Customer Self-Hosting, Customization & Self-Debugging Manual (`bigquery-agent-analytics` v0.5.4)

<table align="left">
  <td>
    <a href="https://colab.research.google.com/github/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb">
      <img src="https://raw.githubusercontent.com/googleapis/python-bigquery-dataframes/refs/heads/main/third_party/logo/colab-logo.png" alt="Colab logo"> Run in Colab
    </a>
  </td>
  <td>
    <a href="https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb">
      <img src="https://raw.githubusercontent.com/googleapis/python-bigquery-dataframes/refs/heads/main/third_party/logo/github-logo.png" width="32" alt="GitHub logo"> View Notebook on GitHub
    </a>
  </td>
  <td>
    <a href="https://console.cloud.google.com/vertex-ai/workbench/deploy-notebook?download_url=https://raw.githubusercontent.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb">
      <img src="https://www.gstatic.com/images/branding/product/1x/google_cloud_48dp.png" alt="Vertex AI logo" width="32"> Open in Vertex AI Workbench
    </a>
  </td>
  <td>
    <a href="https://console.cloud.google.com/bigquery/import?url=https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb">
      <img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTW1gvOovVlbZAIZylUtf5Iu8-693qS1w5NJw&s" alt="BQ logo" width="35"> Open in BQ Studio
    </a>
  </td>
</table>

<br clear="all"/>

Welcome to the customer engineering manual for the **[BQCA Prompt & Response Logging Customer Starter Notebook](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb)**.

When you enable **Prompt and Response Logging** *(Preview)* in **[BigQuery Conversational Analytics (BQCA)](https://cloud.google.com/bigquery/docs/conversational-analytics)**, BigQuery streams your users' conversation events into an `agent_events` table in your BigQuery dataset using the 16 required columns of the open-source [BigQuery Agent Analytics SDK](../../README.md) (`bigquery-agent-analytics`) plus a BQCA `event_id` column (`17` columns total).

This manual shows your data platform, BI, and analytics engineering teams how to:
1. **Launch and self-host** the starter notebook in **Google Colab**, **BigQuery Studio**, **Vertex AI Colab Enterprise**, **Vertex AI Workbench / Local JupyterLab**, or **headless CI/CD schedules** in under 5 minutes.
2. **Configure IAM permissions, Vertex AI Cloud Resource Connections, and cost/quota controls** with copy-paste `gcloud`, `bq`, and SQL commands.
3. **Customize thresholds, taxonomies, and Golden Q&A answer keys** across all 8 evaluation and FinOps workflows (using **`gemini-3.5-flash`** as the default BigQuery ML AI and Vertex AI judge endpoint with `VERTEX_LOCATION = "global"`).
4. **Self-debug any schema, permission, or SDK behavior question** using the **12-Issue Troubleshooting Runbook** and **5 Copy-Paste SQL/Python Diagnostic Checks**.

---

## Table of Contents
1. [5-Minute Quick-Start Checklist & End-to-End Architecture](#1-5-minute-quick-start-checklist--end-to-end-architecture)
2. [Step-by-Step Self-Hosting Setup (APIs, Connection, IAM, Runtimes & Costs)](#2-step-by-step-self-hosting-setup-apis-connection-iam-runtimes--costs)
3. [Understanding the 8-Workflow Playbook & Capability Matrix](#3-understanding-the-8-workflow-playbook--capability-matrix)
4. [Customizing the Playbook for Your Own Business Domain](#4-customizing-the-playbook-for-your-own-business-domain)
5. [Self-Debugging & Troubleshooting Runbook (12 Common Issues)](#5-self-debugging--troubleshooting-runbook-12-common-issues)
6. [Copy-Paste Diagnostic SQL & Python One-Liners (5 Live Checks)](#6-copy-paste-diagnostic-sql--python-one-liners-5-live-checks)
7. [Production Scheduling, Looker Studio BI & Data Governance](#7-production-scheduling-looker-studio-bi--data-governance)
   - [7.4 Visualizing Logs with the BQCA Analytics Dashboards (Looker Studio & Streamlit)](#74-visualizing-logs-with-the-bqca-analytics-dashboards-looker-studio--streamlit)
8. [FAQ & Quick-Reference Cheat Sheet](#8-faq--quick-reference-cheat-sheet)

---

## 1. 5-Minute Quick-Start Checklist & End-to-End Architecture

### 1.1 3-Step Quick-Start Checklist

- [ ] **Step 1 — Launch the Notebook**: Click **[Run in Colab](https://colab.research.google.com/github/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, **[Open in BQ Studio](https://console.cloud.google.com/bigquery/import?url=https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, or open [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) in JupyterLab / Vertex AI Workbench.
- [ ] **Step 2 — Fill in 3 Parameters in Cell `1.1 Customer Configuration Parameters` (`Cell [4]`)** (this 3-parameter quick start assumes a **US multi-region** dataset; for an EU or single-region dataset also make the extra edits listed below):
  - `PROJECT_ID = "your-gcp-project-id"`
  - `DATASET_ID = "your_bqca_logs_dataset"`
  - `VERTEX_CONNECTION_ID = "your-gcp-project-id.us.bqca_vertex_connection"`
  - *(For a US multi-region dataset, leave `RAW_TABLE_ID = "agent_events"` — the default table created by BQCA Prompt & Response Logging — `LOCATION = "US"`, `VERTEX_LOCATION = "global"`, and `JUDGE_MODEL = "gemini-3.5-flash"` unchanged.)*
  - **EU multi-region dataset — also set:** `LOCATION = "EU"` and `VERTEX_CONNECTION_ID = "your-gcp-project-id.eu.bqca_vertex_connection"` (a connection created in the `EU` location). Optionally set `VERTEX_LOCATION = "eu"` to keep the direct judge calls on the EU multi-region endpoint (the default `"global"` also works).
  - **Single-region dataset (for example `us-central1`) — also set:** `LOCATION = "us-central1"` and `VERTEX_CONNECTION_ID = "your-gcp-project-id.us-central1.bqca_vertex_connection"` (a connection created in that region). Distinguish the BigQuery query/connection region from the Vertex AI inference endpoint: for in-warehouse `AI.GENERATE` / `AI.CLASSIFY`, BigQuery executes the query job in `LOCATION` and automatically routes short `gemini-3.5-flash` model names from US single regions (such as `us-central1`) to the `us` multi-region endpoint, from eligible EU single regions to the `eu` multi-region endpoint, and from other locations to `global` (in `asia-south1`, a fully qualified `global` endpoint URL is required; see [BigQuery generative AI locations](https://cloud.google.com/bigquery/docs/generative-ai-overview#locations)), so `JUDGE_MODEL` and `CATEGORICAL_MODEL` can remain `"gemini-3.5-flash"` unless your policy requires single-region warehouse inference inside `us-central1` (in which case set them to `"gemini-2.5-flash"`). For direct `google-genai` calls (Section 6B and `api_fallback`), which target `VERTEX_LOCATION` directly without BigQuery's multi-region routing, either keep `VERTEX_LOCATION = "global"` (or `"us"` / `"eu"`) with `DIRECT_JUDGE_MODEL = "gemini-3.5-flash"`, or if you set `VERTEX_LOCATION = "us-central1"`, change `DIRECT_JUDGE_MODEL` to `"gemini-2.5-flash"` (model names are plain Cell `1.1` variables, not `BQCA_*` environment variables).
- [ ] **Step 3 — Click `Runtime -> Run all`**:
  - **Section 1** installs or upgrades to `bigquery-agent-analytics==0.5.4` if needed, creates two zero-copy BigQuery views (`v_bqca_customer_sdk_events` and `v_bqca_customer_turns`), runs `client.doctor()`, and runs a 1-row `AI.GENERATE` health ping (checking both `.verdict` and `.status`).
  - **Sections 2, 3, 4, 6B, 7, and 8** run directly on your `agent_events` logs with **zero external tables required**.
  - **Sections 5 and 6A** automatically bootstrap an unverified starter `bqca_golden_qa` template table (`CREATE TABLE IF NOT EXISTS`) from single-turn sessions if you do not have one yet, so all 32 cells run end-to-end on your first click.

### 1.2 End-to-End Architecture

```text
+-----------------------------------------------------------------------------------+
| 1. BigQuery Conversational Analytics (BQCA) Runtime (Preview)                     |
|    Streams Prompt & Response events to your BigQuery dataset                      |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 2. Raw BQCA Events Table: `PROJECT_ID.DATASET_ID.agent_events`                    |
|    - 16 required BigQuery Agent Analytics SDK columns + BQCA `event_id` (17 cols) |
|    - 9 standard BQCA event types (prompts, responses, LLM telemetry, errors)      |
+-----------------------------------------------------------------------------------+
                                         |
            +----------------------------+----------------------------+
            | (Provisioned automatically by Cell 1.3 of Notebook)     |
            v                                                         v
+-------------------------------------------+   +-----------------------------------+
| 3A. SDK Compatibility View                |   | 3B. Flat Conversation Turns View  |
|     `v_bqca_customer_sdk_events`          |   |     `v_bqca_customer_turns`       |
| - Filters to standard BQCA event types    |   | - 1 row per session               |
|   within `EVAL_LOOKBACK_DAYS`             |   | - Pairs initial `user_prompt` &   |
| - Normalizes `AGENT_RESPONSE` JSON object |   |   earliest non-clarifying answer  |
|   (`$.text_summary`, ordered              |   |   tokens, `thinking_tokens`,      |
|   `$.response.parts[*].markdown`,         |   |   `cost_sdk_usd`,                 |
|   `$.response.clarifying_question`,       |   |   `cost_incl_thinking_usd`,       |
|   `$.parts[0].text`, scalar `$.response`) |   |   `is_fast_path`, & error flags   |
| - Keeps nested JSON in `$.raw_response`   |   | - Supports `ANONYMIZE_USER_ID`    |
| - Backfills `$.root_agent_name`           |   |   SHA-256 cohort pseudonymization |
+-------------------------------------------+   +-----------------------------------+
            |                                                         |
            +----------------------------+----------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 4. `bigquery-agent-analytics` SDK v0.5.4 + BigQuery AI (`AI.GENERATE`,            |
|    `AI.CLASSIFY`, `AI.EMBED`, `ML.DISTANCE`, default `gemini-3.5-flash`)          |
|    -> Section 1: Schema & Connection Health (`client.doctor()` + 1-row AI ping)   |
|    -> Section 2: Prompt & Response Conversation Explorer                          |
|    -> Section 3: Sentiment Scoring (`LLMAsJudge.sentiment`) & UX Tone Triage      |
|    -> Section 4: Question Topic Taxonomy (`AI.CLASSIFY` & `AI.GENERATE`)          |
|    -> Section 5: Semantic Drift & Golden Question Bank Coverage                   |
|    -> Section 6: Golden Q&A Grading, `GraderPipeline` & `TrialRunner` (`pass@3`)  |
|    -> Section 7: SLOs (`SystemEvaluator`), Thinking-Token FinOps & Fast-Path ROI  |
|    -> Section 8: 3-Signal Operational Health Monitor & Executive Scorecard        |
+-----------------------------------------------------------------------------------+
```

### 1.3 What BQCA Logs (Standard 9-Event Contract)

On BigQuery Conversational Analytics Prompt & Response Logging *(Preview)* `agent_events` tables (see the [BigQuery Conversational Analytics documentation](https://cloud.google.com/bigquery/docs/conversational-analytics)), the observed event stream uses 9 prompt, model-response, agent-response, lifecycle, and error event types (`BQCA_EVENT_TYPES`), while low-level ADK tool and agent spans (`AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR`) are not emitted to customer tables:

| Event Category | `event_type` | What BQCA Populates | Where the Starter Notebook Uses It |
| :--- | :--- | :--- | :--- |
| **User Input** | `USER_MESSAGE_RECEIVED` | `content.text_summary` contains the user's natural-language prompt (extracted via `NULLIF(TRIM(JSON_VALUE(content, '$.text_summary')), '')` in `v_bqca_customer_turns` as `user_prompt`). | Sections 1–6 (Conversation Explorer, Sentiment, Taxonomy, Drift, Quality Grading) |
| **Agent Output** | `AGENT_RESPONSE` | `content.response` contains `{parts: [{markdown: "..."}], clarifying_question: "..."}` (normalized to scalar string in `v_bqca_customer_sdk_events`, with fallback for `$.parts[0].text` and scalar `$.response`). `attributes.fast_path` marks Verified Queries fast-path turns. | Sections 1–8 (All answer quality, sentiment, refusal, and fast-path workflows) |
| **Turn Lifecycle** | `INVOCATION_STARTING`<br>`INVOCATION_COMPLETED` | `INVOCATION_COMPLETED.latency_ms.total_ms` records cumulative invocation latency (summed across distinct `invocation_id`s using the per-invocation `MAX(total_ms)` among `INVOCATION_COMPLETED` rows in `v_bqca_customer_turns` as `e2e_latency_ms` — partitioned by `(session_id, event_type = 'INVOCATION_COMPLETED', invocation_id)` so non-completion events never compete and duplicate completion events within an invocation are not double-counted); `attributes.session_metadata.state` carries `"data-agent-id"`, `"conversation-id"`, and `custom_labels.*`. | Sections 1, 2, 7 (`v_bqca_customer_turns` and end-to-end latency P50/P90) |
| **Model Telemetry** | `LLM_RESPONSE` | `content` is `NULL` by design; `LLM_RESPONSE.attributes.usage_metadata` logs `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `cached_content_token_count`, and `total_token_count`; `attributes.finish_reason` logs `STOP`, `MAX_TOKENS`, or `SAFETY`; `latency_ms` logs `total_ms` and `time_to_first_token_ms`. | Sections 2, 7, 8 (Token & thinking-token FinOps, cache hit rate, TTFT, and `finish_reason` cut-off alerts) |
| **Suggestions** | `EMBEDDING_SUGGESTION` | Logs embedding-based query/question suggestion metadata when triggered. | Section 1 (`client.doctor()` coverage check) |
| **Error Events** | `INVOCATION_ERROR`<br>`AGENT_ERROR`<br>`LLM_ERROR` | `status = 'ERROR'`, `content IS NULL`, and `error_message` contains a sanitised diagnostic message (`[REDACTED]` for SQL/table identifiers). | Sections 2, 8 (`bqaa.ERROR_SQL_PREDICATE` operational health monitor) |

> **Note on Non-BQCA Event Types in `client.doctor()`:** Because `AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, and `TOOL_COMPLETED` are not emitted to customer BQCA tables, `client.doctor()` will list those 5 ADK event types under `No events for types: ...` in its `warnings` array. That is **expected on BQCA logs**.

---

## 2. Step-by-Step Self-Hosting Setup (APIs, Connection, IAM, Runtimes & Costs)

Before running the notebook against your GCP project for the first time, run Steps **2.1–2.3** in Cloud Shell or your local terminal.

### Step 2.1 Enable Required Google Cloud APIs

```bash
export PROJECT_ID="your-gcp-project-id"
export DATASET_ID="your_bqca_logs_dataset"
export LOCATION="US"                 # BigQuery dataset location: US or EU multi-region, or a single region (e.g., us-central1)
export CONN_LOCATION="us"            # Connection location, must match LOCATION (us, eu, or the same single region; single regions need extra notebook edits, see Section 1.1, Step 2)
export CONN_ID="bqca_vertex_connection"

gcloud config set project "${PROJECT_ID}"

gcloud services enable \
  bigquery.googleapis.com \
  bigqueryconnection.googleapis.com \
  aiplatform.googleapis.com \
  geminidataanalytics.googleapis.com
```

### Step 2.2 Create (or Inspect) Your BigQuery Cloud Resource Connection

BigQuery's in-warehouse AI functions (`AI.GENERATE`, `AI.CLASSIFY`, and `AI.EMBED`) use a **BigQuery Cloud Resource Connection** to call Vertex AI Gemini (`gemini-3.5-flash`) and text-embedding (`text-embedding-005`) models:

```bash
# Create the Cloud Resource Connection if you do not have one yet:
bq mk --connection \
  --location="${CONN_LOCATION}" \
  --project_id="${PROJECT_ID}" \
  --connection_type=CLOUD_RESOURCE \
  "${CONN_ID}"

# Inspect the connection and copy its serviceAccountId (e.g., bqcx-<project-number>-<id>@...):
bq show --connection --format=prettyjson "${PROJECT_ID}.${CONN_LOCATION}.${CONN_ID}"
```

### Step 2.3 Grant `Vertex AI User` (`roles/aiplatform.user`) to the Connection Service Account

```bash
# Extract the connection's service account email automatically:
export CONN_SA=$(bq show --connection --format=json "${PROJECT_ID}.${CONN_LOCATION}.${CONN_ID}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")

echo "Granting roles/aiplatform.user to: ${CONN_SA}"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CONN_SA}" \
  --role="roles/aiplatform.user"
```

> **Note:** Expect **~1–3 minutes** for IAM propagation after granting the connection service account `roles/aiplatform.user` before `AI.GENERATE` works end-to-end (the Section 1.4 ping fails with a permission error until propagation completes — wait and re-run the cell). Your standard `VERTEX_CONNECTION_ID` string for Cell `1.1` of the notebook is:
> `${PROJECT_ID}.${CONN_LOCATION}.${CONN_ID}`.

### Step 2.4 Required IAM Roles for the Notebook Runner

Grant the user (or service account) executing the notebook the following roles:

| Resource Scope | Required IAM Role | Why the Notebook Needs It |
| :--- | :--- | :--- |
| **BigQuery Dataset (`DATASET_ID`)** | `roles/bigquery.dataEditor` *(plus `bigquery.rowAccessPolicies.list` via `roles/bigquery.dataOwner` or `roles/bigquery.admin` when first-run auto-seeding `bqca_golden_qa`)* | Allows Cell `1.3` to run `CREATE OR REPLACE VIEW` for `v_bqca_customer_sdk_events` and `v_bqca_customer_turns`, allows Cell `5.1` to verify `0` row access policies on `RAW_TABLE_ID` via `tables.rowAccessPolicies.list` before auto-bootstrapping `bqca_golden_qa` (or pre-create `bqca_golden_qa` if running with `dataEditor` only), and reads `agent_events`. |
| **GCP Project (`PROJECT_ID`)** | `roles/bigquery.jobUser` | Allows the notebook to submit BigQuery SQL queries and SDK evaluation jobs. |
| **BigQuery Connection (`VERTEX_CONNECTION_ID`)** | `roles/bigquery.connectionUser` | Allows the notebook's queries to invoke `AI.GENERATE`, `AI.CLASSIFY`, and `AI.EMBED` via the Cloud Resource Connection. |
| **GCP Project (`PROJECT_ID`)** | `roles/aiplatform.user` | Required by **Section 5.1** (`client.drift_detection()`, whose `AI.EMBED` query executes on the caller's credentials) and **Section 6B** (`GraderPipeline` and `TrialRunner` direct `google-genai` calls). |

### Step 2.5 Choose Your Execution Environment (3 Ways to Run)

#### Option A — Google Colab, BigQuery Studio, or Vertex AI Colab Enterprise (Fastest: 5-Minute Setup)
1. **In Google Colab**: Click **[Run in Colab](https://colab.research.google.com/github/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)** and optionally **File -> Save a copy in Drive**.
2. **In BigQuery Studio / Vertex AI Colab Enterprise (Recommended for VPC Service Controls)**:
   - Click **[Open in BQ Studio](https://console.cloud.google.com/bigquery/import?url=https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, or download [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) and import it under **Vertex AI -> Colab Enterprise -> Notebooks**.
3. **Edit Cell `1.1 Customer Configuration Parameters` (`Cell [4]`)**: Update `PROJECT_ID`, `DATASET_ID`, and `VERTEX_CONNECTION_ID`. This 3-parameter quick start assumes a US multi-region dataset; for an EU or single-region dataset also set `LOCATION` and `VERTEX_LOCATION` and, for single regions, the three model names (see Section 1.1, Step 2, or the Region guide at the top of Cell `1.1`).
4. **Run**: Click **Runtime -> Run all**. Cell `1.0` automatically installs or upgrades to `bigquery-agent-analytics[llm]==0.5.4` in any runtime where it is missing or older than `0.5.4`.

#### Option B — Self-Hosted JupyterLab, Vertex AI Workbench, or VS Code
We recommend **Python 3.11 or 3.12**. On Apple Silicon Macs use a native **arm64** Python (for example `arch -arm64 python3.11 -m venv .bqca_nb_venv`, or an arm64 build of Python). When the interpreter runs as an x86_64 process under Rosetta, or when `pip` is old, `pip` may skip the prebuilt `cryptography` wheel (a transitive dependency) and fall back to a source build, which needs a Rust toolchain and OpenSSL headers and fails without them. Upgrading `pip` and `wheel` (`pip install --upgrade pip wheel`) or switching to an arm64 Python installs prebuilt wheels instead.

```bash
# 1. Create and activate a clean Python 3.11+ virtual environment
python3.11 -m venv .bqca_nb_venv
source .bqca_nb_venv/bin/activate
pip install --upgrade pip

# 2. Install the BigQuery Agent Analytics SDK v0.5.4+ and notebook dependencies
pip install "bigquery-agent-analytics[llm]>=0.5.4" \
  google-cloud-bigquery pandas pyarrow db-dtypes ipykernel jupyterlab nbclient nbformat

# 3. Authenticate Application Default Credentials (ADC)
gcloud auth application-default login --project="${PROJECT_ID}"

# 4. Launch JupyterLab and open the starter notebook
jupyter lab examples/bqca_prompt_response_logging_customer_notebook.ipynb
```

#### Option C — Headless Scheduled Execution (CI/CD, Cloud Run Jobs, or Vertex AI Pipelines)

Because Cells `1.1`, `1.4`, `5.1`, and `6A` read `BQCA_PROJECT_ID`, `BQCA_DATASET_ID`, `BQCA_RAW_TABLE_ID`, `BQCA_LOCATION`, `BQCA_VERTEX_LOCATION`, `BQCA_VERTEX_CONNECTION_ID`, `BQCA_ANONYMIZE_USER_ID`, `BQCA_MAX_BYTES_BILLED`, `BQCA_AI_SQL_TIMEOUT_SEC`, `BQCA_AUTO_SEED_GOLDEN_TABLE`, `BQCA_ASSUME_BYO_GOLDEN_VERIFIED`, `BQCA_PING_API_FALLBACK`, and `BQCA_GOLDEN_GRADING_API_FALLBACK` (13 environment variables in total) when set, you can execute the notebook headlessly in CI/CD without editing the `.ipynb` file. All other Cell `1.1` settings (model names, thresholds, lookback and session caps) are plain notebook variables: edit them in the `.ipynb` copy you run, for example the three model names on a single-region dataset (Section 1.1, Step 2):

```bash
export BQCA_PROJECT_ID="your-gcp-project-id"
export BQCA_DATASET_ID="your_bqca_logs_dataset"
export BQCA_RAW_TABLE_ID="agent_events"
export BQCA_LOCATION="US"
export BQCA_VERTEX_LOCATION="global"
export BQCA_VERTEX_CONNECTION_ID="your-gcp-project-id.us.bqca_vertex_connection"
# Optional governance, scan-cap & AI SQL timeout overrides for headless runs:
# export BQCA_ANONYMIZE_USER_ID="true"
# export BQCA_MAX_BYTES_BILLED="1000000000"
# export BQCA_AI_SQL_TIMEOUT_SEC="180"             # Client-side wait timeout (seconds) before a best-effort job.cancel() & api_fallback (not a spend cap)
# export BQCA_AUTO_SEED_GOLDEN_TABLE="false"       # "true" (default), "false", or "force" (scratch only)
# export BQCA_ASSUME_BYO_GOLDEN_VERIFIED="false"   # "true" only if a custom table without `verified` is SME-verified
# export BQCA_PING_API_FALLBACK="false"            # "true" (default); "false" disables the direct Vertex AI ping fallback in Cell 1.4
# export BQCA_GOLDEN_GRADING_API_FALLBACK="false"  # "true" (default); "false" disables the direct Vertex AI grading fallback in Cell 6A

# Execute all 32 cells headlessly and write the populated notebook to /tmp (outside the git checkout):
jupyter nbconvert --to notebook --execute \
  --ExecutePreprocessor.timeout=600 \
  --output-dir /tmp \
  --output bqca_customer_report_$(date +%F).ipynb \
  examples/bqca_prompt_response_logging_customer_notebook.ipynb

# Optional: Export the executed notebook to a standalone HTML report (contains sample prompts/answers — share only with authorized viewers):
jupyter nbconvert --to html \
  --output-dir /tmp \
  /tmp/bqca_customer_report_$(date +%F).ipynb
```

### Step 2.6 Costs, Quotas & Resource Cleanup

Running all cells in the starter notebook ("Runtime -> Run all") executes billable queries and model calls in your `PROJECT_ID`:

- **Billed Google Cloud services**:
  1. **BigQuery SQL queries**: All view queries are scoped to `EVAL_LOOKBACK_DAYS` (default `90` days), Section 2.1 caps the pandas conversation explorer dataframe (`turns_df`) at `MAX_EVAL_SESSIONS * 10` rows, and every direct SQL query in the notebook (`run_sql()`, the Cell `5.1` CTAS bootstrap, and Cell `6B` `SESSION_SUMMARY_QUERY`; SDK evaluators manage their own query jobs) is capped at `MAX_BYTES_BILLED` bytes billed (default 1 GB; a query estimated above the cap fails instead of scanning — raise `MAX_BYTES_BILLED` in Cell `1.1` or narrow `EVAL_LOOKBACK_DAYS` on large tables).
  2. **BigQuery ML remote model calls (`AI.GENERATE`, `AI.CLASSIFY`, `AI.EMBED`, `ML.GENERATE_TEXT`, `ML.GENERATE_EMBEDDING`)**: Executed via `VERTEX_CONNECTION_ID` against `JUDGE_MODEL` / `CATEGORICAL_MODEL` (default `gemini-3.5-flash`) and `EMBEDDING_MODEL` (default `text-embedding-005`). `AI_SQL_TIMEOUT_SEC` (default `180` seconds) is a **per-query client-side wait timeout**, not a spend cap: when a warehouse AI query is still running after that many seconds, the notebook stops waiting and requests a best-effort `job.cancel()`. Note the SDK's intermediate warehouse fallback tiers: in `client.evaluate(LLMAsJudge.*)`, if `AI.GENERATE` fails or times out, the SDK first checks for a legacy BQML model `<project>.<dataset>.gemini_text_model` (`ML.GENERATE_TEXT`, `execution_mode = "ml_generate_text"`) before falling back to direct Vertex AI (`execution_mode = "api_fallback"`); in `client.evaluate_categorical(..., include_justification=False)`, if `AI.CLASSIFY` fails or times out, the SDK tries in-warehouse `AI.GENERATE` (`execution_mode = "ai_generate"`) before falling back to direct Vertex AI (`execution_mode = "api_fallback"`; categorical evaluation does not use `ML.GENERATE_TEXT`, so at most 2 warehouse queries run before `api_fallback`, vs. up to 2 warehouse queries (`AI.GENERATE -> ML.GENERATE_TEXT`) before `api_fallback` in `client.evaluate(LLMAsJudge.*)`). If an intermediate warehouse tier succeeds, execution finishes in that warehouse mode; if multiple warehouse tiers stall and time out sequentially, each attempted warehouse query can wait up to `AI_SQL_TIMEOUT_SEC` before reaching `api_fallback`. BigQuery cancels asynchronously, so work performed before cancellation takes effect is still billed, and any fallback calls are billed on top. If the cancel request cannot be confirmed (the call raises or returns `False`), the notebook prints a `WARNING` with the BigQuery `job_id` and a ready-to-run `bq cancel` command so you can check the job yourself (see Issue #11).
  3. **Direct Vertex AI Gemini calls (`google-genai`)**: Executed in Section 6B (`GraderPipeline` and `TrialRunner`, plus `api_fallback` when warehouse `AI.GENERATE` / `AI.CLASSIFY` jobs time out or are cancelled) against `DIRECT_JUDGE_MODEL` (default `gemini-3.5-flash` in `VERTEX_LOCATION = "global"`).
- **Approximate model call counts per full run**:
  - At the default `MAX_EVAL_SESSIONS = 100` and `MAX_GOLDEN_QUESTIONS = 200`: `1` connection ping (Section 1.4), **`7`** batch `AI.GENERATE` / `AI.CLASSIFY` queries over `<= 100` sessions each (Sections 3.1, 3.2, 4.1×2, 6B×2, and 8.2; if a batch `AI.GENERATE` or `AI.CLASSIFY` query fails, is cancelled, or exceeds the `AI_SQL_TIMEOUT_SEC` client-side wait timeout (the notebook then requests a best-effort `job.cancel()` for the BigQuery job), `client.evaluate()` / `client.evaluate_categorical()` fall back to per-session direct Gemini calls with `execution_mode = "api_fallback"` — `client.evaluate(LLMAsJudge.*)` via `BQCAClient._run_api_judge` from Cell `1.2` and `client.evaluate_categorical()` via `_categorical_api_fallback` over `_trace_to_categorical_transcript`), `1` `AI.GENERATE` answer-key grading query over `<= 100` pre-matched in-domain sessions (Section 6A, with automatic direct Vertex AI `api_fallback` via `_grade_in_domain_via_api` if the warehouse query times out or is cancelled), `2` `AI.EMBED` queries (Section 5.1 SDK `client.drift_detection()` over `<= 100` prompts + all rows in `bqca_golden_qa`, and Section 5.2 SQL over `<= 100` single-turn prompts + `<= MAX_GOLDEN_QUESTIONS` (`200`) golden questions — `<= 50` on a freshly bootstrapped golden table; Section 6A reuses Section 5.2's matched pairs with **zero** extra `AI.EMBED` calls), and **`10–16`** direct Gemini judge calls in Section 6B before any transient-error retries (`2` target comparison sessions × `2` `GraderPipeline` calls + `3` trials × `1–2` `TrialRunner` rubric calls: `10` in `reference_free_sentiment` mode, `16` when both targets have `verified = TRUE` golden answers).
  - **Lowering cost and runtime on a first test run**: Runtime grows with `MAX_EVAL_SESSIONS`, conversation length, Vertex AI concurrency, and quota; a full run at the default `MAX_EVAL_SESSIONS = 100` can take several minutes, and longer when quota errors trigger retries or when warehouse AI queries hit the `AI_SQL_TIMEOUT_SEC` wait timeout and fall back to direct calls (there is no guaranteed runtime). Set `MAX_EVAL_SESSIONS = 25` and `EVAL_LOOKBACK_DAYS = 7` in Cell `1.1` to cut the rows sent to the model in each batch query by roughly 4× and scan only the past week of logs; expect a proportionally shorter run.
- **Quotas & `429` handling**: Model calls are governed by your project's Vertex AI API requests-per-minute (QPM) quota for `gemini-3.5-flash` and `text-embedding-005`. The notebook's `run_sql()` helper automatically retries job-level `429 TooManyRequests`, `500 InternalServerError`, and `503 ServiceUnavailable` errors with backoff, while per-row `AI.GENERATE` / `AI.EMBED` quota errors are reported in the `.status` column (`ERROR` in Section 6A). Direct judge calls retry only **transient** failures (`429`, `5xx`, `DEADLINE_EXCEEDED`, timeouts; 2 s, 4 s backoff) in the Cell `1.2` wrappers around `LLMAsJudge.evaluate_session` (used by `client.evaluate()` in `api_fallback` mode and by `GraderPipeline`) and `PerformanceEvaluator.llm_judge_evaluate` (used by `TrialRunner`). Permanent errors (`401` / `403` / `404`, `PERMISSION_DENIED`, `NOT_FOUND`) fail fast without retries, and an empty, malformed, or non-numeric judge reply is **never retried and never scored**: it is returned as an explicit `JUDGE_ERROR: ...` and treated as unevaluated. Unevaluated sessions become `NaN` (`unevaluated: judge errors`) in `score_frame()`. `_recompute_eval_report_counts()` keeps `EvaluationReport.summary()` on the evaluated denominator (`Sessions` / `Passed` / `Failed` plus a separate `Unevaluated (judge error)` line), and Cell `6B` records `pipeline_status` / `trial_status = unavailable (...)` instead of a `0.20` latency-only composite or a partial `pass@3` / `pass^3`: if any `TrialRunner` trial is unevaluated, `pass@3` and `pass^3` are left empty and only the partial counts appear in `trial_status` (for example `unavailable (2/3 trials evaluated, 1/2 passed: judge error — all 3 trials required for pass@3/pass^3)`). Quota and judge-format errors therefore never silently count as `0.0` failed scores.
- **Resource cleanup SQL**: To remove the BigQuery views (and, if you have not added SME-curated `verified = TRUE` rows, the `bqca_golden_qa` table) created by the notebook:

```sql
DROP VIEW IF EXISTS `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_sdk_events`;
DROP VIEW IF EXISTS `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_turns`;
-- CAUTION: Only uncomment and run DROP TABLE below if you have NOT curated SME-verified (verified = TRUE) rows in bqca_golden_qa:
-- DROP TABLE IF EXISTS `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`;
```

---

## 3. Understanding the 8-Workflow Playbook & Capability Matrix

The table below maps every section in [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) (`32` cells: `18` code cells with `#@title` headers `1.0`–`8.3` and `14` markdown cells) to the customer question it answers, what it computes, and an **illustrative** example output. The example numbers are sample values from a small synthetic 25-session, 5-cohort reference dataset used while documenting the playbook; they are not embedded execution logs (the committed notebook ships with cleared outputs) and are not performance guarantees, and your metrics will reflect your own dataset and model runs:

| Workflow Section | Notebook Code Cells (`#@title`) | Customer Question Answered & What It Computes | Key Knobs You Can Customize | Illustrative Output (Synthetic 25-Session Reference Dataset) |
| :--- | :--- | :--- | :--- | :--- |
| **Section 1: Setup, Views & `doctor()`** | `1.0 Install & Authenticate`<br>`1.1 Customer Configuration Parameters`<br>`1.2 Shared BigQuery Helpers`<br>`1.3 Provision Customer Views`<br>`1.4 Run client.doctor() & Ping` | *Is my dataset and connection ready?* Installs/upgrades SDK if needed, validates `#@param` config, registers `BQCAClient` (`Trace.final_response` + `_run_api_judge` adapter for `AGENT_RESPONSE`), creates `v_bqca_customer_sdk_events` and `v_bqca_customer_turns` over `RAW_TABLE_ID`, runs `client.doctor()`, and executes a 1-row `AI.GENERATE` ping (`gemini-3.5-flash`, checking `.verdict` and `.status`, recording failure state if interrupted, and probing direct Vertex AI fallback on warehouse stall). | `PROJECT_ID`, `DATASET_ID`, `RAW_TABLE_ID`, `VERTEX_CONNECTION_ID`, `EVAL_LOOKBACK_DAYS`, `AI_SQL_TIMEOUT_SEC` | Schema `ok` (`17` cols); `137` events across `25` sessions and `8` active event types; `23/23` `AGENT_RESPONSE` rows normalized; `AI.GENERATE` ping `'OK'` (`status=''`). |
| **Section 2: Conversation Explorer** | `2.1 Conversation Explorer & Cohort Summary` | *What are users asking and how often does the agent answer?* Summarizes sessions (capped at `MAX_EVAL_SESSIONS * 10`), single-turn vs. multi-turn counts, answered turns, clarifying questions, fast-path usage, errors, median latency, and tokens per `data_agent_id` and user cohort. | Group by `data_agent_id`, `user_id`, or `persona` / `persona_role` / `scenario_tag`. | `23/25` sessions delivered an answer (`1` clarifying question); `3` hit Verified Queries fast path; `2` ended in an error. |
| **Section 3: Sentiment & Frustrated-Turn Triage** | `3.1 Sentiment Evaluation`<br>`3.2 UX & Tone Categorical Rubric` | *Which users are frustrated or confused?* Runs `LLMAsJudge.sentiment(threshold=JUDGE_THRESHOLD)` and `client.evaluate_categorical()` with `customer_ux_tone`, joining scores back to `v_bqca_customer_turns`. | Adjust `JUDGE_THRESHOLD` (default `0.6`) or edit categories in `ux_tone_metric`. | `22/25` sessions passed the `0.6` sentiment bar (`ai_generate`); UX rubric flagged `1` `Frustrated_Impatient` and `1` `Clarification_Needed`. |
| **Section 4: Question Topic Taxonomy** | `4.1 Question Topic Taxonomy (AI.CLASSIFY vs AI.GENERATE)` | *What topics do users ask about most?* Classifies user questions into 5 topic buckets using both `AI.CLASSIFY` (`include_justification=False`, bulk SQL classification without rationale tokens) and `AI.GENERATE` (`include_justification=True`, structured rationale mode). | Replace the `CategoricalMetricCategory` list in `topic_metric` with your own business domains. | `AI.CLASSIFY` and `AI.GENERATE` (`gemini-3.5-flash`) agreed on `24/25` (`96%`) sessions (`Ranking_TopN: 9`, `Out_of_Scope_Drift: 7`, `Cohort_Comparison: 5`, `Schema_Metadata: 2`, `Aggregation_KPI: 2`; illustrative — LLM classifications can vary slightly across runs). |
| **Section 5: Semantic Drift & Golden Coverage** | `5.1 Bootstrap or Load Golden Question Bank`<br>`5.2 Production-Centric Semantic Drift Query` | *Which incoming questions are outside our Golden Question Bank?* Auto-bootstraps deduplicated `bqca_golden_qa` rows if absent (`LOGICAL_AND` verification check), runs `client.drift_detection()`, and ranks novel single-turn user prompts with `cosine_distance > DRIFT_THRESHOLD`. | Curate matching questions in `bqca_golden_qa` first; cautiously tune `DRIFT_THRESHOLD = 0.15` (e.g. `0.20`–`0.25`; higher thresholds risk matching unrelated questions). | `88.2%` Golden Question Bank coverage (`15/17` covered); `9` production sessions drifted beyond cosine distance `0.15` (median `0.564` on out-of-domain cohort). |
| **Section 6: Response-Quality Evaluation** | `6A. Golden Q&A Answer Key Grading`<br>`6B. Reference-Free Judges, GraderPipeline & TrialRunner` | *Are the agent's numbers and entities accurate?* **6A**: Grades the pre-matched in-domain pairs from Section 5.2 (`question_distance <= DRIFT_THRESHOLD`, zero extra `AI.EMBED` calls) against `bqca_golden_qa` with `AI.GENERATE` (`PASS`/`FAIL`/`ERROR`, falling back to direct Vertex AI grading via `_grade_in_domain_via_api` if the warehouse job times out or is cancelled, setting `GOLDEN_GRADING_COMPLETED` only after grading succeeds, and computing `in_domain_matched` / `out_of_domain` counts directly from `question_distance`).<br>**6B**: Runs `LLMAsJudge.correctness()`, `LLMAsJudge.hallucination()`, `GraderPipeline`, and 3-trial `TrialRunner` judge consistency (`pass@3` / `pass^3`). | Tune `DRIFT_THRESHOLD`, `PIPELINE_WEIGHTS`, and `num_trials=3`. | Golden Q&A SQL grading: `15 PASS` and `1 FAIL` across `16` verified in-domain sessions (flagged `session_hallucinated_retrieval_demo`); `GraderPipeline` (`0.73` vs. `0.44`, illustrative — LLM judge scores can vary slightly across runs) and `TrialRunner pass@3` (`1.00` vs. `0.00`) separated the reference and hallucinated benchmark sessions. |
| **Section 7: FinOps, Thinking Tokens & Fast-Path ROI** | `7.1 Automated Session SLO Checks`<br>`7.2 Thinking-Token Cost Accounting & Fast-Path ROI` | *What is our token-equivalent spend and Fast-Path ROI?* Runs `SystemEvaluator` SLO checks (`latency`, `token_efficiency`, `cost_per_session`, `context_cache_hit_rate`, `turn_count`, `ttft`), estimates `thoughts_token_count` cost impact, and compares Verified Queries Fast-Path turns vs. standard NL2SQL turns (attributing latency and tokens per invocation and separating mixed fast-path + standard sessions). | Edit `BUDGETS` (`latency_ms: 3000`, `total_tokens: 8000`, `cost_usd: 0.02`, etc.) and `COST_RATES` (`input_per_1k`, `output_per_1k`). | Latency SLO `22/25`, token SLO `22/25`, token-weighted cache hit rate `0.63`; including `thoughts_token_count` adds `+43%` to token-equivalent SDK cost (`$0.318` -> `$0.455`); Fast-Path median latency `1,080 ms` (`0` tokens) vs. `3,215 ms`. |
| **Section 8: Operational Health Monitor & Scorecard** | `8.1 Model Finish Reasons & Sanitised Error Events`<br>`8.2 Delivery Outcome Classification`<br>`8.3 Customer Production Health Summary Table` | *Did any session fail, get truncated, or refuse a query?* Combines `finish_reason != 'STOP'`, error rows (`bqaa.ERROR_SQL_PREDICATE`), and `delivery_outcome` classification into an Operational Attention Queue and renders the live **Customer Production Health Summary Table**. | Customize `classify_error_message()` or `outcome_metric` categories. | `6` error rows in `2` sessions (`content IS NULL`: `True`); `2` non-STOP `finish_reason` sessions (`MAX_TOKENS`, `SAFETY`); `6` polite out-of-scope refusals. |

---

## 4. Customizing the Playbook for Your Own Business Domain

### 4.1 Complete `#@param` Configuration Reference (`Cell 1.1` / `Cell [4]`)

| Parameter | Default Value in Notebook | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | `"your-gcp-project-id"` | GCP project ID hosting your BigQuery dataset and Vertex AI connection (or set `BQCA_PROJECT_ID`; validated by `_PROJECT_ID_RE`, including domain-scoped `example.com:project-id`). |
| `DATASET_ID` | `"your_bqca_logs_dataset"` | BigQuery dataset where BQCA writes `agent_events` (or set `BQCA_DATASET_ID`; alphanumeric/underscore/hyphen validated by `_IDENTIFIER_RE`). |
| `RAW_TABLE_ID` | `"agent_events"` | Raw table written by BQCA Prompt & Response Logging (or set `BQCA_RAW_TABLE_ID`). |
| `COMPAT_VIEW_ID` | `"v_bqca_customer_sdk_events"` | Auto-provisioned SDK compatibility view created by Cell `1.3`. |
| `TURNS_VIEW_ID` | `"v_bqca_customer_turns"` | Auto-provisioned 1-row-per-session flat view created by Cell `1.3`. |
| `GOLDEN_QA_TABLE_ID` | `"bqca_golden_qa"` | Golden Question & Answer table used by Sections 5 and 6A (auto-bootstrapped if absent; controlled by `BQCA_AUTO_SEED_GOLDEN_TABLE` and `BQCA_ASSUME_BYO_GOLDEN_VERIFIED`). |
| `LOCATION` | `"US"` | BigQuery dataset and query job location (`"US"`, `"EU"`, `"us-central1"`, etc.; or set `BQCA_LOCATION`). Must match the location of `DATASET_ID` and of the `VERTEX_CONNECTION_ID` connection. The default `"US"` (with `VERTEX_LOCATION = "global"`) is what the 3-parameter quick start assumes: for `"EU"` also use an EU connection, and for a single region such as `"us-central1"` set `LOCATION` and `VERTEX_CONNECTION_ID` to that region (see Section 1.1, Step 2 for how BigQuery routes in-warehouse `gemini-3.5-flash` vs. direct `VERTEX_LOCATION` calls). |
| `VERTEX_LOCATION` | `"global"` | Vertex AI endpoint location used by direct `google-genai` calls (Section 6B `GraderPipeline` and `TrialRunner`, and the `api_fallback` paths; or set `BQCA_VERTEX_LOCATION`). It does **not** control in-warehouse `AI.GENERATE` / `AI.CLASSIFY` / `AI.EMBED` jobs, which execute in `LOCATION` using `VERTEX_CONNECTION_ID` (where BigQuery automatically routes short `gemini-3.5-flash` model names from US single regions to the `us` multi-region endpoint, from eligible EU single regions to `eu`, and from other locations to `global`; see [BigQuery generative AI locations](https://cloud.google.com/bigquery/docs/generative-ai-overview#locations)). Because direct `google-genai` calls hit `VERTEX_LOCATION` without that automatic routing and `gemini-3.5-flash` is served only on `"global"`, `"us"`, and `"eu"` (direct calls to a single region such as `"us-central1"` with `gemini-3.5-flash` return `404`; see Issue #10), keep `VERTEX_LOCATION = "global"` (or `"us"` / `"eu"`) with `DIRECT_JUDGE_MODEL = "gemini-3.5-flash"`, or if you set `VERTEX_LOCATION` to a single region such as `"us-central1"`, set `DIRECT_JUDGE_MODEL = "gemini-2.5-flash"`. |
| `VERTEX_CONNECTION_ID` | `"your-gcp-project-id.us.bqca_vertex_connection"` | BigQuery Cloud Resource Connection used by `AI.GENERATE`, `AI.CLASSIFY`, and `AI.EMBED` in `PROJECT_ID.LOCATION.CONNECTION_ID` format (or set `BQCA_VERTEX_CONNECTION_ID`). The `LOCATION` segment is the location of your dataset (`us`, `eu`, or a single region such as `us-central1`) and must match `LOCATION` above. |
| `JUDGE_MODEL` | `"gemini-3.5-flash"` | Default BigQuery ML `AI.GENERATE` judge endpoint (`LLMAsJudge` and Section 6A Golden Q&A grading). Not a `BQCA_*` environment variable. In US/EU multi-regions and single regions that BigQuery routes to `us`/`eu`/`global`, `"gemini-3.5-flash"` works out of the box (in `asia-south1`, pass a fully qualified `global` endpoint URL); switch to `"gemini-2.5-flash"` if you require single-region warehouse inference inside a single region. |
| `CATEGORICAL_MODEL` | `"gemini-3.5-flash"` | Default BigQuery ML endpoint used by `client.evaluate_categorical()` (`AI.CLASSIFY` and `AI.GENERATE`). As with `JUDGE_MODEL`, BigQuery routes `"gemini-3.5-flash"` from US/EU single regions to `us`/`eu` multi-region endpoints (or `global` elsewhere); switch to `"gemini-2.5-flash"` if you require single-region warehouse inference. |
| `EMBEDDING_MODEL` | `"text-embedding-005"` | Vertex AI embedding model used by `AI.EMBED` in Section 5. |
| `DIRECT_JUDGE_MODEL` | `"gemini-3.5-flash"` | Direct `google-genai` Vertex AI model used by `GraderPipeline` and `TrialRunner` in Section 6B (and direct `api_fallback`). Use `"gemini-3.5-flash"` when `VERTEX_LOCATION` is `"global"`, `"us"`, or `"eu"`; change to `"gemini-2.5-flash"` if you set `VERTEX_LOCATION` to a single region such as `"us-central1"`. |
| `JUDGE_THRESHOLD` | `0.6` | Minimum score (`[0.0, 1.0]`) required for a session to pass `LLMAsJudge` evaluations. |
| `DRIFT_THRESHOLD` | `0.15` | Cosine distance threshold (`ML.DISTANCE(..., 'COSINE')`) for Section 5.2 production session semantic drift flagging (`question_distance > DRIFT_THRESHOLD`) and Section 6A in-domain golden answer pairing (`GOLDEN_MATCH_MAX_DISTANCE = DRIFT_THRESHOLD`). Note: Section 5.1 headline Golden Question Bank coverage (`client.drift_detection()`) uses the SDK's built-in `similarity_threshold = 0.30` (`SDK_DRIFT_THRESHOLD`). |
| `EVAL_LOOKBACK_DAYS` | `90` | Lookback window in days applied to `v_bqca_customer_sdk_events` and `v_bqca_customer_turns` to bound scan costs on large tables. |
| `MAX_EVAL_SESSIONS` | `100` | Maximum number of sessions evaluated per `client.evaluate()`, `client.evaluate_categorical()`, and `AI.EMBED` batch run (pinned across sections via `EVAL_FILTER` / `EVAL_SESSION_IDS`). |
| `MAX_GOLDEN_QUESTIONS` | `200` | Maximum number of golden questions embedded in Section 5.2's SQL `AI.EMBED` query (prioritizing `verified = TRUE` rows) to bound embedding costs on large curated banks (note: SDK `client.drift_detection()` in Section 5.1 embeds all rows in `bqca_golden_qa`). |
| `MAX_BYTES_BILLED` | `1000000000` | Conservative per-query scan cap (bytes billed, 1 GB; or set `BQCA_MAX_BYTES_BILLED`) applied to direct notebook SQL queries (`run_sql()`, the Cell `5.1` CTAS bootstrap, and Cell `6B` `SESSION_SUMMARY_QUERY`; SDK evaluators manage their own query jobs): a query estimated above this cap fails instead of scanning (if BigQuery raises `Query exceeded limit for bytes billed`, raise `MAX_BYTES_BILLED` in Cell `1.1` or narrow `EVAL_LOOKBACK_DAYS`). |
| `AI_SQL_TIMEOUT_SEC` | `180` | Per-query client-side wait timeout in seconds (or set `BQCA_AI_SQL_TIMEOUT_SEC`) for in-warehouse BigQuery `AI.GENERATE`, `AI.CLASSIFY`, `AI.EMBED`, `ML.GENERATE_TEXT`, and `ML.GENERATE_EMBEDDING` SQL jobs. When a warehouse AI query is still running after this many seconds, the notebook stops waiting and requests a best-effort `job.cancel()`. In `client.evaluate(LLMAsJudge.*)` the SDK tries `AI.GENERATE -> ML.GENERATE_TEXT -> api_fallback` (up to 2 warehouse queries before `api_fallback`), and in `client.evaluate_categorical(..., include_justification=False)` it tries `AI.CLASSIFY -> AI.GENERATE -> api_fallback` (categorical evaluation does not use `ML.GENERATE_TEXT`, so at most 2 warehouse queries run before `api_fallback`; each attempted warehouse query can wait up to `AI_SQL_TIMEOUT_SEC` before reaching `api_fallback`). This is **not a spend cap**: BigQuery cancels asynchronously, work done before the cancel is still billed, and any fallback calls are billed on top. If the cancel request cannot be confirmed, the notebook prints a `WARNING` with the BigQuery `job_id` and a `bq cancel` command (see Issue #11). |
| `ANONYMIZE_USER_ID` | `False` | When `True` (or `BQCA_ANONYMIZE_USER_ID=true`), `v_bqca_customer_turns` and Section 8.1 SHA-256 pseudonymize `user_id`, `user_id`-derived cohort fallback labels, and email-like explicit `custom_labels.persona` labels as `user_<8hex>` (32-bit display pseudonym; ~39% collision probability at 65,536 distinct users and ~50% around ~77,000 distinct users). Masking covers the turns view and Section 8.1 cohort labels only: prompts, answers, other explicit persona labels, and `v_bqca_customer_sdk_events` still carry raw text. |
| `COST_RATES` | `{"input_per_1k": 0.00125, "output_per_1k": 0.01}` | Illustrative placeholder token rates (USD per 1K tokens; not Gemini Flash list prices) interpolated into `v_bqca_customer_turns` and used in Section 7. Replace with your organization's Gemini rate card. |

### 4.2 How `bqca_golden_qa` Auto-Bootstrapping Works (and How to Curate Your Own Answer Key)

When you run **Section 5 (`Cell 5.1` / `Cell [18]`)**, the notebook runs `CREATE TABLE IF NOT EXISTS` to bootstrap `bqca_golden_qa` from up to 50 distinct, clean, single-turn answered questions (`turns = 1`, deduplicated by `LOWER(TRIM(user_prompt))`) in `v_bqca_customer_turns` if the table does not exist yet:

```sql
-- Executed automatically by Cell 5.1 if `bqca_golden_qa` does not exist yet:
CREATE TABLE IF NOT EXISTS `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa` AS
WITH eligible AS (
  SELECT
    session_id,
    started_at,
    persona_role,
    user_prompt,
    agent_response
  FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_turns`
  WHERE user_prompt IS NOT NULL
    AND agent_response IS NOT NULL
    AND NOT has_error
    AND NULLIF(TRIM(clarifying_question), '') IS NULL
    AND turns = 1
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY LOWER(TRIM(user_prompt))
    ORDER BY started_at ASC, session_id ASC
  ) = 1
)
SELECT
  FORMAT('G%02d', ROW_NUMBER() OVER (ORDER BY started_at ASC, session_id ASC)) AS golden_id,
  COALESCE(NULLIF(persona_role, ''), 'Auto-Seeded Template') AS category,
  user_prompt AS golden_question,
  agent_response AS golden_answer,
  user_prompt AS question,
  agent_response AS expected_answer,
  session_id AS source_session_id,
  'auto_bootstrap_template' AS source,
  FALSE AS verified
FROM eligible
ORDER BY started_at ASC, session_id ASC
LIMIT 50;
```

> [!IMPORTANT]
> **Why `verified = FALSE` on auto-seeded rows matters (and CTAS RLS/CLS caveat):** Auto-seeded rows copy the agent's own historical answers so Sections 5 and 6A run on your first click as a pipeline smoke test, and Cell `5.1` prints an explicit `[UNVERIFIED AUTO-SEEDED TEMPLATE]` notice (or `[UNVERIFIED BYO GOLDEN TABLE — missing 'verified' column]` when a custom table omits `verified`) while Cell `6A` prints a template smoke-test notice whenever `verified = FALSE` rows are matched. In Section 6B, `TrialRunner` only passes `golden_response` (`trial_gate_mode = "verified_golden_answer"`, gating directly on `llm_judge_final_answer_correct >= JUDGE_THRESHOLD` while keeping `llm_judge_correctness`, `llm_judge_tool_usage_correct`, and `llm_judge_sound_reasoning` informational at `0.0` because BQCA omits intermediate tool and reasoning trajectories) when `verified = TRUE` and `session_id != source_session_id`; otherwise `TrialRunner` runs in `reference_free_sentiment` mode (gating on `llm_judge_sentiment >= JUDGE_THRESHOLD`). Before using Section 6A or `TrialRunner` as a ground-truth accuracy KPI, review or insert SME-verified answers with `verified = TRUE` (and `source_session_id = NULL`).
>
> **Row/Column-Level Security (`bqca_golden_qa` CTAS caveat):** Because `CREATE TABLE IF NOT EXISTS ... AS SELECT` materializes a new physical table from whatever rows the executing principal can see, BigQuery does **not** automatically copy source-table Row-Level Security (RLS) policies or Column-Level Security (CLS / policy tags) from `agent_events` onto `bqca_golden_qa`. Cell `5.1` inspects `RAW_TABLE_ID` via the BigQuery REST `tables.rowAccessPolicies.list` endpoint and recursive `SchemaField.policy_tags` and **fails closed** by default if any row access policy or column policy tag is present **or if the caller lacks `bigquery.rowAccessPolicies.list` to verify row access policies** (note: `bigquery.rowAccessPolicies.list` is included in `roles/bigquery.dataOwner`, `roles/bigquery.admin`, and basic `Viewer`/`Editor`/`Owner` roles, not `roles/bigquery.dataEditor`; policies on tables underneath a view are not inspected): pre-create `bqca_golden_qa` in `DATASET_ID` with matching row access policies / policy tags, or set `BQCA_AUTO_SEED_GOLDEN_TABLE=false` to disable automatic CTAS materialization (which stops `Runtime -> Run all` at Cell `5.1` if `bqca_golden_qa` is missing; only set `BQCA_AUTO_SEED_GOLDEN_TABLE=force` in an isolated scratch dataset). `CREATE TABLE IF NOT EXISTS` never refreshes an existing `bqca_golden_qa` (and Cell `5.1` raises a clear `RuntimeError` if an existing `bqca_golden_qa` has `0` rows) — drop or curate the table to re-seed it.

To insert **SME-verified business Q&A pairs**, run:

```sql
INSERT INTO `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`
  (golden_id, category, golden_question, golden_answer, question, expected_answer, source_session_id, source, verified)
VALUES
  (
    'G_REV_01',
    'Revenue_KPI',
    'What was total net revenue in Q4 across North America?',
    'Total net revenue in Q4 across North America was $42.8M (+11.4% YoY).',
    'What was total net revenue in Q4 across North America?',
    'Total net revenue in Q4 across North America was $42.8M (+11.4% YoY).',
    NULL,
    'sme_curated',
    TRUE
  );
```

**Keep golden questions unique (case- and whitespace-insensitive).** Golden-question coverage in Sections 5.1 and 5.2 is computed over **distinct** questions keyed by `LOWER(TRIM(question))`: `'Revenue?'` and `' revenue? '` are the same question. The notebook collapses such duplicates when it computes the headline coverage (so coverage can never exceed `100%`, and `total_golden` counts distinct questions), and Cell `5.1` prints a duplicate-row warning, but duplicate rows still add `AI.EMBED` and Section 6A grading cost, so remove them from the table. To add SME rows idempotently, skip any question that is already present:

```sql
-- Idempotent insert: skips a question that already exists (compared as LOWER(TRIM(...))).
INSERT INTO `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`
  (golden_id, category, golden_question, golden_answer, question, expected_answer, source_session_id, source, verified)
SELECT
  'G_REV_02', 'Revenue_KPI', q, a, q, a, CAST(NULL AS STRING), 'sme_curated', TRUE
FROM (
  SELECT
    'What was total net revenue in Q3 across Europe?' AS q,
    'Total net revenue in Q3 across Europe was $31.2M (+8.9% YoY).' AS a
)
WHERE TRIM(q) != ''
  AND LOWER(TRIM(q)) NOT IN (
    SELECT LOWER(TRIM(question))
    FROM `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`
    WHERE question IS NOT NULL
  );
```

> **Schema Tip:** Why does `bqca_golden_qa` include `golden_id`, `category`, `(golden_question, golden_answer)`, and `(question, expected_answer)`? Because Sections `5.1` and `5.2` group and join by `golden_id` and `category`, `client.drift_detection()` requires `question`, and custom SQL graders often reference `golden_question` / `golden_answer` or `expected_answer`. Defining both aliases (and dynamically inspecting the table schema in Cells `5.1` and `5.2` so existing 4-column or 6-column BYO tables with `golden_id`, `category`, `question`, and `expected_answer` without `verified` or `source_session_id` also work automatically — defaulting `golden_verified = FALSE` for safe template smoke-test gating unless a `verified BOOL` column is present or `BQCA_ASSUME_BYO_GOLDEN_VERIFIED=true` is set) ensures compatibility across all SDK and SQL helpers.

### 4.3 Customizing Your Question Topic Taxonomy (Section 4)

In `Cell 4.1` (`Cell [16]`), edit the `categories` list inside `topic_metric` to reflect your organization's analytics domains:

```python
topic_metric = CategoricalMetricDefinition(
    name="question_topic",
    definition="Primary business domain of the user's question in this session.",
    categories=[
        CategoricalMetricCategory(name="Revenue_And_Bookings", definition="Questions about ARR, bookings, pipeline, or billing."),
        CategoricalMetricCategory(name="Customer_Retention", definition="Questions about churn, NRR, cohort retention, or renewals."),
        CategoricalMetricCategory(name="Product_Adoption", definition="Questions about active users, feature usage, or funnel conversion."),
        CategoricalMetricCategory(name="Schema_Discovery", definition="Questions about available tables, column definitions, or data freshness."),
        CategoricalMetricCategory(name="Out_of_Scope_Drift", definition="Questions outside the connected dataset or restricted by policy."),
    ],
)
```

---

## 5. Self-Debugging & Troubleshooting Runbook (12 Common Issues)

Use this runbook whenever a query, judge, or metric behaves unexpectedly on your logs.

---

### Issue #1: `LLMAsJudge` (`sentiment`, `correctness`, `hallucination`) returns `NULL`, `0.0`, `0.1`, or empty answers

- **Symptom:** Running `client.evaluate(LLMAsJudge.sentiment())` or `LLMAsJudge.correctness()` directly against your raw `agent_events` table returns `0.0` or `NaN` scores, or when an in-warehouse `AI.GENERATE` judge query falls back to direct Gemini calls (`execution_mode = "api_fallback"`), the judge scores `correctness = 0.1` (`"The agent failed to provide a final response"`) or counts `429 RESOURCE_EXHAUSTED` quota errors and empty, malformed, or non-numeric judge replies as `0.0` failed sessions.
- **Root Cause:** Four things happen in `bigquery-agent-analytics` v0.5.4 on BQCA logs:
  1. **In-warehouse SQL judge (`ai_generate`)**: `LLMAsJudge` extracts the agent's final answer using `JSON_VALUE(content, '$.response')` (which only returns scalar JSON strings). On raw BQCA `AGENT_RESPONSE` rows, `content.response` is a nested JSON object (`{"parts": [{"markdown": "..."}], "clarifying_question": "..."}`), so `JSON_VALUE(content, '$.response')` returns SQL `NULL`.
  2. **Direct Gemini API fallback (`execution_mode = "api_fallback"`)**: `Trace.final_response` only inspects `LLM_RESPONSE` and `AGENT_COMPLETED` spans, ignoring `AGENT_RESPONSE` (`LLM_RESPONSE.content` is `NULL` by design on BQCA logs). In addition, `Client._run_api_judge` builds `trace_text` from `span.summary` (which reads `text_summary` on the compatibility view or falls through to the nested `response` dict on raw rows, truncating at 120 characters via `text[:117] + "..."`) and passes `final = trace.final_response or ""` (empty string) to `evaluator.evaluate_session(trace_text, final)`.
  3. **Per-session API errors and unusable replies in `LLMAsJudge._judge_criterion`**: When a direct Gemini API judge call fails with `429 RESOURCE_EXHAUSTED` or `503 UNAVAILABLE`, stock `_judge_criterion` catches the exception and returns `(0.0, str(e))`, which records a `0.0` float score instead of omitting the score key (`NaN`). The same happens when the model replies with empty text, a reply that is not JSON or lacks the score key, or a score that is boolean, non-numeric (for example `"N/A"`), non-finite, or outside `0–10`: stock `_judge_criterion` returns `(0.0, <reply or error text>)`, so an unusable reply is recorded as a **measured** `0.0` failure.
  4. **Per-trial API errors in `PerformanceEvaluator.llm_judge_evaluate`** (the direct Gemini path used by `TrialRunner` / `BQCAPerformanceEvaluator`): a failed rubric call appends `One-sided LLM evaluation failed: 429 ...` to the feedback and returns **no** scores, so `evaluate_session` records `LLM judge missing required metrics` and the trial counts as a `0.0` failure in `pass@k` / `pass^k`; likewise an errored `LLMAsJudge` grader inside `GraderPipeline` returns empty scores, which `WeightedStrategy` folds in as `0.0` (a `0.20` latency-only composite that looks like a measured quality score). `EvaluationReport` also counts every session that returned a `SessionScore` object, so one quota error on a 25-session run prints `Passed: 18 (72%) / Failed: 7` instead of `Passed: 18 (75%) / Failed: 6 / Unevaluated: 1`.
- **Copy-Paste Fix:** Always (a) point the client at `v_bqca_customer_sdk_events` created in Cell `1.3` (`Cell [7]`), and (b) use `BQCAClient` (plus the `Trace.final_response` adapter and `score_frame()` helper defined in Cell `1.2` / `Cell [5]` and instantiated in Cell `1.4` / `Cell [9]`), which extracts full `AGENT_RESPONSE` text for both `Trace.final_response` and `Client._run_api_judge`. Cell `1.2` also installs idempotent wrappers (safe to re-run) so a failed or unusable judge call is reported as **unevaluated** instead of a measured `0.0`:
  - **`LLMAsJudge._judge_criterion`:** valid numeric `0–10` JSON scores return a tagged `_ValidatedJudgeScore(raw / 10.0)` with the justification preserved verbatim (so a genuine `0.0` score is never confused with a judge failure even if its justification is empty or mentions an agent `429` / `503` error), whereas an empty reply, a reply that is not JSON or lacks the score key, or a score that is boolean, non-numeric, non-finite, or outside `0–10` is returned as `(_ErroredJudgeScore(0.0), "JUDGE_ERROR: ...")`.
  - **`LLMAsJudge.evaluate_session`** (used by `client.evaluate()` in `api_fallback` mode and by `GraderPipeline`): records validated vs. errored criteria in `score.details` and uses start-anchored error matching, retrying a criterion whose feedback reports a **transient** judge error (`429 RESOURCE_EXHAUSTED`, `5xx` / `503 UNAVAILABLE`, `DEADLINE_EXCEEDED`, timeout) up to 2 more times (`2s`, `4s` backoff); permanent errors (`401` / `403` / `404`, `PERMISSION_DENIED`), malformed/empty `JUDGE_ERROR:` replies, and valid `0.0` scores are never retried. A criterion that still fails is removed from `score.scores` (so it renders as `NaN`, never `0.0`), `passed` is set to `False`, and `details["evaluation_error"]` is recorded.
  - **`GraderPipeline`:** the Cell `1.2` wrapper around the SDK's per-grader runner (`AggregateGrader._run_grader`) returns a `GraderResult` subclass whose `evaluation_error` field is set when its judge could not be evaluated, so Cell `6B` reports `pipeline_status = unavailable (judge error: ...)` instead of a `0.20` latency-only composite.
  - **`PerformanceEvaluator.llm_judge_evaluate`** (used by `TrialRunner` / `BQCAPerformanceEvaluator`): each rubric call retries only transient errors with the same `2s`, `4s` backoff (a persistent `429` with `num_trials=3` costs exactly 3 judge calls per trial, 9 total) and skips the side-by-side rubric when the one-sided rubric is unavailable. A trial whose judge errored is recorded in `details["errors"]`.
  - **`_recompute_eval_report_counts()`** (also applied inside `EvaluationReport.summary()`): re-derives the counts over the evaluated denominator for any `EvaluationReport` with per-session scores (`ai_generate` and `api_fallback` alike), so `sentiment_report.summary()` prints `Sessions: 24` / `Passed: 18 (75%)` / `Failed: 6` / `Unevaluated (judge error): 1 (of 25 total sessions)`.
  - **Cell `6B` (`pass@3` / `pass^3`):** if **any** of the `3` trials has a judge error, `trial_pass@3` and `trial_pass^3` are **left empty (unavailable)**: they are never recomputed over fewer trials and still labelled `pass@3` / `pass^3`. `trial_status` carries the partial counts, for example `unavailable (2/3 trials evaluated, 1/2 passed: judge error — all 3 trials required for pass@3/pass^3)` (a fully evaluated run shows `evaluated (3/3 trials)`). The Section 6 Takeaway and the Section 8.3 scorecard print `unavailable (judge error)` and name the affected surface instead of claiming the evaluators "did not separate" the twin sessions.
  - **Cells `3.1`, `3.2`, `6B`, and `8.3`:** `score_frame()` renders unevaluated sessions as `NaN`, and these cells report **measured** `passed` / `failed` counts over the `evaluated` denominator with a separate `not_evaluated` count (for example `18/24 evaluated sessions passed (75% of evaluated, failed=6, not_evaluated=1)` on a 25-session run with one quota error). Section 8.3 Row 3 shows the sentiment result and the UX-tone result independently, so a failed sentiment judge never hides a healthy tone result (or the reverse).

  Instantiate the client over the compatibility view (Cell `1.4` does this for you):

```python
# Defined in Cell 1.2 and instantiated in Cell 1.4 over v_bqca_customer_sdk_events:
client = BQCAClient(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=COMPAT_VIEW_ID,
    location=LOCATION,
    bq_client=bq,
    endpoint=JUDGE_MODEL,
    connection_id=VERTEX_CONNECTION_ID,
)
```

---

### Issue #2: `client.evaluate_categorical()` is slow or fails with a BigQuery `500 Internal Error`

- **Symptom:** Calling `client.evaluate_categorical(cfg)` with `include_justification=True` takes several minutes or logs `google.api_core.exceptions.InternalServerError: 500 An internal error occurred` during `AI.GENERATE`.
- **Root Cause:** Two things can trigger a transient BigQuery `AI.GENERATE` error on single-column structured output schemas (`output_schema => 'classifications STRING'`):
  1. Creating `v_bqca_customer_sdk_events` as a **view on top of another view** that already contains `UNNEST(JSON_QUERY_ARRAY(...))` + `JSON_SET(...)`.
  2. Using an older endpoint when evaluating single-column JSON string schemas over nested views.
- **Copy-Paste Fix:**
  1. Ensure `RAW_TABLE_ID` points to your **base table** (`agent_events`), so `v_bqca_customer_sdk_events` is a single-level view over the base table.
  2. Use the default `CATEGORICAL_MODEL = "gemini-3.5-flash"`, and check `report.details.get("execution_mode")` to confirm whether the evaluation ran via `"ai_generate"` / `"ai_classify"` or fell back to `"api_fallback"`:

```python
CATEGORICAL_MODEL = "gemini-3.5-flash"

cfg = CategoricalEvaluationConfig(
    metrics=[ux_tone_metric],
    endpoint=CATEGORICAL_MODEL,
    connection_id=VERTEX_CONNECTION_ID,
    include_justification=True,
)
report = client.evaluate_categorical(cfg, filters=TraceFilter(limit=MAX_EVAL_SESSIONS))
print("Execution mode:", report.details.get("execution_mode"))
```

---

### Issue #3: `TrialRunner` (`pass@k` / `pass^k`) scores `0.0` or reports no final response on BQCA logs

- **Symptom:** Running `TrialRunner(PerformanceEvaluator(...))` on BQCA logs yields `0.0` accuracy or empty `final_response` strings across all trials.
- **Root Cause:** Stock `PerformanceEvaluator` in `bigquery-agent-analytics` v0.5.4 reads `SessionTrace.final_response` from the last `LLM_RESPONSE` event and does not include `AGENT_RESPONSE` in `_DEFAULT_EVENT_TYPES`. On BQCA logs, `LLM_RESPONSE.content` is `NULL` by design and the user-visible answer is stored in `AGENT_RESPONSE`. (In addition, `v_bqca_customer_sdk_events` backfills a missing `attributes.root_agent_name` from the session's unique explicitly-logged root — falling back to `agent` when the session has a single agent value — so the common case where telemetry rows (e.g. `LLM_RESPONSE`) omit `root_agent_name` resolves in `client.get_session_trace()`. Sessions whose events still carry conflicting explicit `root_agent_name` values raise `AmbiguousSessionError`; disambiguate with an explicit selector such as `client.get_session_trace(session_id, root_agent_name="...")`, or fix the source telemetry.)
- **Copy-Paste Fix:** Use the drop-in `BQCAPerformanceEvaluator` subclass from Cell `6B` (`Cell [23]`), which delegates to `_extract_bqca_final_response(session_trace.events)` from Cell `1.2`:

```python
class BQCAPerformanceEvaluator(PerformanceEvaluator):
    """Adapter for BQCA logs: populates SessionTrace.final_response from AGENT_RESPONSE."""

    async def get_session_trace(self, session_id, **kwargs):
        session_trace = await super().get_session_trace(session_id, **kwargs)
        session_trace.final_response = (
            session_trace.final_response or _extract_bqca_final_response(session_trace.events)
        )
        return session_trace


_DEFAULT_TRACE_EVENT_TYPES = getattr(
    PerformanceEvaluator,
    "_DEFAULT_EVENT_TYPES",
    (
        "USER_MESSAGE_RECEIVED",
        "AGENT_STARTING",
        "AGENT_COMPLETED",
        "TOOL_STARTING",
        "TOOL_COMPLETED",
        "TOOL_ERROR",
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "LLM_ERROR",
        "INVOCATION_STARTING",
        "INVOCATION_COMPLETED",
        "STATE_DELTA",
        "HITL_CONFIRMATION_REQUEST",
        "HITL_CONFIRMATION_REQUEST_COMPLETED",
        "HITL_CREDENTIAL_REQUEST",
        "HITL_CREDENTIAL_REQUEST_COMPLETED",
        "HITL_INPUT_REQUEST",
        "HITL_INPUT_REQUEST_COMPLETED",
        "AGENT_TRANSFER",
        "EVENT_COMPACTION",
        "AGENT_STATE_CHECKPOINT",
        "TOOL_PAUSED",
        "WORKFLOW_NODE_STARTING",
        "WORKFLOW_NODE_COMPLETED",
    ),
)

bqca_evaluator = BQCAPerformanceEvaluator(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=COMPAT_VIEW_ID,
    client=bq,
    llm_judge_model=DIRECT_JUDGE_MODEL,
    include_event_types=list(_DEFAULT_TRACE_EVENT_TYPES) + ["AGENT_RESPONSE"],
)
```

---

### Issue #4: `SystemEvaluator.error_rate()` reports `0` errors (`100% PASS`) even when queries or models failed

- **Symptom:** `client.evaluate(SystemEvaluator.error_rate())` reports `0` errors on sessions that failed with `429 RESOURCE_EXHAUSTED` or SQL errors.
- **Root Cause:** Stock `SystemEvaluator.error_rate()` in v0.5.4 only counts `event_type = 'TOOL_ERROR'`. Because BQCA logs `INVOCATION_ERROR`, `AGENT_ERROR`, and `LLM_ERROR` (with `status = 'ERROR'` and `content IS NULL`) rather than `TOOL_ERROR`, `SystemEvaluator.error_rate()` does not count BQCA error events.
- **Copy-Paste Fix:** Query `bqaa.ERROR_SQL_PREDICATE` (`status = 'ERROR' OR error_message IS NOT NULL OR ENDS_WITH(event_type, '_ERROR')`) and check `LLM_RESPONSE.attributes.finish_reason != 'STOP'` as implemented in Cell `8.1` (`Cell [28]`):

```python
errors_df = run_sql(f"""
SELECT
  session_id,
  event_type,
  status,
  content IS NULL AS content_is_null,
  error_message
FROM {T(COMPAT_VIEW_ID)}
WHERE {bqaa.ERROR_SQL_PREDICATE}
ORDER BY session_id, timestamp
""")
```

---

### Issue #5: `client.doctor()` warns that `AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, and `TOOL_COMPLETED` have 0 events

- **Symptom:** `client.doctor()["warnings"]` notes `No events for types: ['AGENT_COMPLETED', 'AGENT_STARTING', 'LLM_REQUEST', 'TOOL_COMPLETED', 'TOOL_STARTING']`, and `trace.tool_calls` is `[]`.
- **Root Cause:** By design, BQCA Prompt & Response Logging emits the 9 standard prompt, response, lifecycle, and error event types (`BQCA_EVENT_TYPES`). Low-level ADK agent/tool spans (`AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR`) are not written to customer `agent_events` tables.
- **Copy-Paste Fix:** No fix is needed for `doctor()` — that warning is expected on BQCA logs. To evaluate answer accuracy without `TOOL_*` events, use **Section 6A (Golden Q&A Answer Key Grading via `AI.GENERATE`)** and **Section 6B (`LLMAsJudge` + `GraderPipeline` + `BQCAPerformanceEvaluator`)**.

---

### Issue #6: `SystemEvaluator.cost_per_session()` is lower than total token-equivalent consumption when thinking tokens are generated (or `total_token_count` slightly exceeds the 3-counter sum)

- **Symptom:** The USD estimate from `SystemEvaluator.cost_per_session()` accounts for prompt and candidate tokens but excludes `thoughts_token_count` (and on some sessions with tool-use or system-prompt overhead tokens, `total_token_count` is slightly larger than `prompt_tokens + candidate_tokens + thinking_tokens`).
- **Root Cause:** `SystemEvaluator.cost_per_session()` prices only `prompt_token_count` and `candidates_token_count`. On Gemini reasoning models, `attributes.usage_metadata.thoughts_token_count` (thinking tokens) are also generated (adding **+43%** to the token-equivalent estimate in the illustrative synthetic 25-session reference dataset: `$0.318` -> `$0.455`; your uplift depends on your agent's thinking-token share). While `prompt_tokens + candidate_tokens + thinking_tokens == total_tokens` holds exactly on standard Gemini calls (`20/20` in that reference dataset), sessions that include tool-use or system-prompt overhead tokens can have a small positive residual in `total_tokens`.
- **Copy-Paste Fix:** Use the `cost_sdk_usd` and `cost_incl_thinking_usd` columns in `v_bqca_customer_turns` (which interpolate `COST_RATES` from Cell `1.1`) or the 3-term token-equivalent cost formula in Cell `7.2` (`Cell [26]`):

```python
finops_df["cost_sdk_usd"] = (
    finops_df.prompt_tokens / 1000.0 * COST_RATES["input_per_1k"]
    + finops_df.candidate_tokens / 1000.0 * COST_RATES["output_per_1k"]
)
finops_df["cost_incl_thinking_usd"] = (
    finops_df.cost_sdk_usd
    + finops_df.thinking_tokens / 1000.0 * COST_RATES["output_per_1k"]
)
```

---

### Issue #7: `SystemEvaluator.context_cache_hit_rate()` looks higher than actual cached-token share when Verified Queries Fast Path is used

- **Symptom:** `SystemEvaluator.context_cache_hit_rate()` reports a higher unweighted per-session average (for example, illustrative `0.68` when fast-path sessions are included) than your actual token-weighted cached-token share (`0.63` in the illustrative synthetic 25-session reference dataset).
- **Root Cause:** Stock `context_cache_hit_rate()` averages unweighted per-session ratios and assigns `1.0` (100% cache hit) to 0-LLM-call sessions such as **Verified Queries Fast-Path** turns (`attributes.fast_path = true`).
- **Copy-Paste Fix:** Compute the **token-weighted cache hit rate** across LLM-calling turns (`llm_calls > 0`) as shown in Cell `7.2` (`Cell [26]`):

```python
llm_turns_df = finops_df[finops_df.llm_calls > 0]
prompt_sum = float(llm_turns_df.prompt_tokens.sum())
weighted_cache_hit_rate = float(llm_turns_df.cached_tokens.sum() / prompt_sum) if prompt_sum > 0 else 0.0
```

---

### Issue #8: `403 Permission denied` when calling `AI.GENERATE`, `AI.CLASSIFY`, or `AI.EMBED`

- **Symptom:** BigQuery returns `403 Access Denied: BigQuery BigQuery: Permission denied while calling Vertex AI` or `Permission bigquery.connections.use denied`.
- **Root Cause:** Either (a) the BigQuery Cloud Resource Connection's service account (`bqcx-...`) is missing `roles/aiplatform.user`, or (b) the user/service account running the notebook is missing `roles/bigquery.connectionUser` (or `roles/aiplatform.user` for Section 5.1 `client.drift_detection()`).
- **Copy-Paste Fix:**

```bash
# 1. Grant roles/aiplatform.user to the connection's service account:
export CONN_SA=$(bq show --connection --format=json "${PROJECT_ID}.${CONN_LOCATION}.${CONN_ID}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CONN_SA}" \
  --role="roles/aiplatform.user"

# 2. Grant roles/bigquery.connectionUser and roles/aiplatform.user to the user running the notebook:
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:your-email@your-domain.com" \
  --role="roles/bigquery.connectionUser"

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:your-email@your-domain.com" \
  --role="roles/aiplatform.user"
```

---

### Issue #9: `404 Not found: Dataset / Table / Connection was not found in location` (or `Query exceeded limit for bytes billed`)

- **Symptom:** `google.api_core.exceptions.NotFound: 404 Not found: Dataset ... was not found in location US`, connection location mismatch, or `500 Query exceeded limit for bytes billed: 1000000000 ...; reason: bytesBilledLimitExceeded` (surfaced by `run_sql()` as `RuntimeError: Query exceeded MAX_BYTES_BILLED ...`).
- **Root Cause:** `LOCATION` in Cell `1.1` (`Cell [4]`) does not match the actual region of your BigQuery dataset or your Cloud Resource Connection (BigQuery requires the dataset and the connection to reside in the same region/multi-region), or your `agent_events` table scan over `EVAL_LOOKBACK_DAYS` exceeds the 1 GB `MAX_BYTES_BILLED` safety cap.
- **Copy-Paste Fix:** Check the exact location of your dataset and connection in the terminal, then set `LOCATION` and `VERTEX_CONNECTION_ID` to match (and if hitting the scan cap on a large production table, increase `MAX_BYTES_BILLED` or lower `EVAL_LOOKBACK_DAYS` in Cell `1.1`):

```bash
bq show --format=prettyjson "${PROJECT_ID}:${DATASET_ID}" | grep '"location"'
bq ls --connection --project_id="${PROJECT_ID}" --location="${LOCATION}"
```

---

### Issue #10: Direct Vertex AI judge calls (`GraderPipeline` / `TrialRunner`) fail with `404 Not Found` for `gemini-3.5-flash`, or `FutureWarning` prints on Python 3.10

- **Symptom:** Section 6B direct `google-genai` judge calls (or `api_fallback`) log `404 Publisher Model ... gemini-3.5-flash was not found` when `VERTEX_LOCATION` is set to a single-region endpoint such as `us-central1` (or in-warehouse `AI.GENERATE` in `asia-south1` fails when given a short `gemini-3.5-flash` model name instead of a fully qualified `global` endpoint), or Python 3.10 prints a `FutureWarning` from `google.api_core`.
- **Root Cause:** Distinguish the BigQuery query/connection region (`LOCATION` and `VERTEX_CONNECTION_ID`) from the Vertex AI inference endpoint: in-warehouse BigQuery `AI.GENERATE` / `AI.CLASSIFY` executes the query job in `LOCATION` but automatically routes short `gemini-3.5-flash` model names from US single regions (such as `us-central1`) to the **`us`** multi-region endpoint, from eligible EU single regions to the **`eu`** multi-region endpoint, and from other locations to **`global`** (in `asia-south1`, BigQuery requires a fully qualified `global` endpoint URL; see [BigQuery generative AI locations](https://cloud.google.com/bigquery/docs/generative-ai-overview#locations)). By contrast, direct `google-genai` SDK calls (Section 6B `GraderPipeline` / `TrialRunner` and `api_fallback`) send requests straight to `VERTEX_LOCATION` without BigQuery's automatic multi-region routing. Because `gemini-3.5-flash` is served on **`global`**, **`us`**, and **`eu`** — not on single-region endpoints like `us-central1` — setting `VERTEX_LOCATION = "us-central1"` with `DIRECT_JUDGE_MODEL = "gemini-3.5-flash"` returns `404 Not Found`. In addition, `google-genai` requires `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION` set via direct assignment (`os.environ[...] = ...`).
- **Copy-Paste Fix:** For US/EU multi-region datasets keep the Cell `1.1` (`Cell [4]`) default `VERTEX_LOCATION = "global"` (or set `"us"` / `"eu"` for US/EU multi-region residency). For a **single-region** dataset (for example `us-central1`), set `LOCATION = "us-central1"` and `VERTEX_CONNECTION_ID` to a connection in `us-central1`, and for direct `google-genai` calls either (a) keep `VERTEX_LOCATION = "global"` (or `"us"` / `"eu"`) with `DIRECT_JUDGE_MODEL = "gemini-3.5-flash"`, or (b) if you set `VERTEX_LOCATION = "us-central1"` to keep direct calls in that single region, change `DIRECT_JUDGE_MODEL` to `"gemini-2.5-flash"` (and optionally set `JUDGE_MODEL` and `CATEGORICAL_MODEL` to `"gemini-2.5-flash"` if your policy also requires single-region warehouse inference rather than BigQuery's automatic `us`/`eu`/`global` routing; the Region guide at the top of Cell `1.1` lists the exact edits). Cell `1.1` also configures targeted warning filters (`FutureWarning` and `google.*` `UserWarning`) while preserving `DeprecationWarning` and routing `bigquery_agent_analytics` `WARNING` logs to `stdout`:

```python
import logging, os, sys, warnings

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module=r"google\..*")
for _noisy in ("google_genai", "google_genai.models", "google.genai", "absl", "urllib3", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
_bqaa_logger = logging.getLogger("bigquery_agent_analytics")
_bqaa_logger.handlers = [logging.StreamHandler(sys.stdout)]
_bqaa_logger.setLevel(logging.WARNING)
_bqaa_logger.propagate = False

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = VERTEX_LOCATION  # "global" (or "us" / "eu") for gemini-3.5-flash; the region itself with gemini-2.5-flash on single-region datasets
```

---

### Issue #11: In-warehouse `AI.GENERATE` or `AI.CLASSIFY` batch job hangs or runs for many minutes

- **Symptom:** An in-warehouse BigQuery ML `AI.GENERATE` or `AI.CLASSIFY` query (in Cell `1.4`, `3.1`, `3.2`, `4.1`, `6A`, `6B`, or `8.2`) stays `RUNNING` for several minutes during regional Vertex AI connection queue congestion, or if a notebook cell is interrupted/timed out client-side (`nbconvert --ExecutePreprocessor.timeout`), the BigQuery job remains `RUNNING` in the background.
- **Root Cause:** BigQuery's `QueryJob.result()` has no default client timeout, and client-side process termination (such as `nbconvert` cell timeout) does not automatically cancel an active server-side BigQuery job.
- **Copy-Paste Fix:** Cell `1.1` (`Cell [4]`) and Cell `1.2` (`Cell [5]`) configure `AI_SQL_TIMEOUT_SEC = 180` (or set `export BQCA_AI_SQL_TIMEOUT_SEC="120"`) and wrap `bq.query` and `run_sql()` across `AI.GENERATE`, `AI.CLASSIFY`, `AI.EMBED`, `ML.GENERATE_TEXT`, and `ML.GENERATE_EMBEDDING` so a warehouse AI query that is still running after `AI_SQL_TIMEOUT_SEC` seconds stops blocking the notebook: the notebook raises a timeout and **requests a best-effort `job.cancel()`**. Note the SDK's per-query fallback sequence: in `client.evaluate(LLMAsJudge.*)` the SDK tries `AI.GENERATE -> ML.GENERATE_TEXT (<project>.<dataset>.gemini_text_model, execution_mode = "ml_generate_text") -> direct Vertex AI (execution_mode = "api_fallback")`; in `client.evaluate_categorical(..., include_justification=False)` it tries `AI.CLASSIFY -> AI.GENERATE (execution_mode = "ai_generate") -> direct Vertex AI (execution_mode = "api_fallback")` (categorical evaluation does not use `ML.GENERATE_TEXT`, so at most 2 warehouse queries are attempted before `api_fallback`); and Cell `1.4` and Cell `6A` (`_grade_in_domain_via_api`) fall back directly to Vertex AI (`api_fallback`). Because `AI_SQL_TIMEOUT_SEC` applies **per warehouse SQL query**, if an intermediate warehouse tier succeeds execution completes in that warehouse mode, whereas if multiple warehouse tiers stall and time out sequentially each attempted warehouse query can wait up to `AI_SQL_TIMEOUT_SEC` before reaching `api_fallback`. `AI_SQL_TIMEOUT_SEC` is a **client-side wait timeout, not a spend cap and not a guarantee that the job has stopped**: BigQuery processes the cancel request asynchronously, work done before it takes effect is still billed, and any fallback calls are billed on top. If the cancel request fails, the notebook prints `⚠️ WARNING: Could not confirm cancellation of BigQuery job_id=<JOB_ID> ...` together with a ready-to-run `bq cancel` command, and the cell continues with the fallback, so a job that may still be `RUNNING` can keep billing until you inspect and cancel it. To inspect or manually cancel any long-running warehouse AI jobs in your project:

```sql
-- 1. List currently RUNNING AI.GENERATE / AI.CLASSIFY / AI.EMBED / ML.GENERATE_* jobs submitted by your user
--    (replace `region-us` with your dataset's region, for example `region-eu` or `region-us-central1`):
SELECT
  job_id,
  creation_time,
  TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), creation_time, SECOND) AS running_seconds,
  LEFT(query, 160) AS query_prefix
FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_USER
WHERE state != 'DONE'
  AND REGEXP_CONTAINS(UPPER(query), r'\b(AI\.(GENERATE|CLASSIFY|EMBED)|ML\.GENERATE_(TEXT|EMBEDDING))\b')
ORDER BY creation_time DESC;
```

```bash
# 2. Cancel a stuck BigQuery job from the terminal (replace <JOB_ID> and --location):
bq --project_id="${PROJECT_ID}" --location="${LOCATION}" cancel <JOB_ID>
```

---

### Issue #12: `client.drift_detection()` silently falls back to `method='keyword_overlap'` (or Cell `5.2` reports `INVALID_ARGUMENT: The text content is empty`)

- **Symptom:** Cell `5.1` prints `drift_detection method: keyword_overlap` instead of `semantic_embedding`, and/or Cell `5.2` prints `⚠️ Warning: 1/N golden questions had failed/empty AI.EMBED vectors (G18:INVALID_ARGUMENT: The text content is empty.)`, even though IAM and Vertex AI quota are healthy.
- **Root Cause:** One or more rows in `bqca_golden_qa` have a blank `question` (`''`, whitespace, or `NULL`) or an explicitly empty / whitespace-only `golden_question`. The SDK embeds the `question` column directly (it does not fall back to `golden_question`), so a blank `question` breaks the drift query. Section 5.2 resolves `COALESCE(golden_question, question)`, so a `NULL` `golden_question` next to a usable `question` is fine (and is **not** flagged or deleted by the statements below), but an empty-string `golden_question` is not. `AI.EMBED('')` returns an empty vector with status `INVALID_ARGUMENT: The text content is empty`, and the SDK's `ML.DISTANCE(golden_embedding, prompt_embedding, 'COSINE')` query then fails on the empty vector, so `client.drift_detection()` catches the error and degrades to keyword overlap (which is **not** a cosine-distance measurement).
- **Copy-Paste Fix:** Cell `5.1` (`Cell [18]`) now pre-checks the golden table (`blank_golden_count`, plus `dup_golden_count` for case/whitespace duplicates, see Section 4.2) and both Cells `5.1` and `5.2` print the exact repair statement instead of generic quota/IAM advice. Delete (or fill in) the blank rows and re-run Section 5:

```sql
-- Find blank golden questions (a NULL golden_question next to a usable question is not blank):
SELECT golden_id, category, source, verified
FROM `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`
WHERE TRIM(COALESCE(question, '')) = '' OR TRIM(golden_question) = '';

-- Delete them (or UPDATE golden_question / question with the real SME question text):
DELETE FROM `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa`
WHERE TRIM(COALESCE(question, '')) = '' OR TRIM(golden_question) = '';
```

> **Tip:** When inserting SME-curated rows (Section 4.2), add `WHERE TRIM(question) != '' AND TRIM(golden_question) != ''` guards to your `INSERT ... SELECT` statements so empty strings never reach `AI.EMBED`.

---

## 6. Copy-Paste Diagnostic SQL & Python One-Liners (5 Live Checks)

Replace `your-gcp-project-id.your_bqca_logs_dataset` (and the connection ID in Check #2 and Check #5) with your own `PROJECT_ID.DATASET_ID` to self-diagnose your environment in seconds. For an EU dataset also change the `us` location in those connection IDs (and `LOCATION` in Check #5) to `eu`; for a single-region dataset also replace `us` / `LOCATION` with that region (and if you set `VERTEX_LOCATION` in Check #5 to a single region such as `us-central1` rather than `global` / `us` / `eu`, replace `gemini-3.5-flash` in Check #5 with `gemini-2.5-flash`; see Section 1.1, Step 2).

### Check #1: Verify View Provisioning & Scalar `AGENT_RESPONSE` Normalization

Confirms that `v_bqca_customer_sdk_events` is populated and that **100%** of `AGENT_RESPONSE` rows have non-null scalar `$.response` and `$.text_summary` alongside preserved `$.raw_response`:

```sql
SELECT
  (SELECT COUNT(*) FROM `your-gcp-project-id.your_bqca_logs_dataset.agent_events`) AS raw_rows,
  COUNT(*) AS sdk_view_rows,
  COUNT(DISTINCT session_id) AS distinct_sessions,
  COUNTIF(event_type = 'AGENT_RESPONSE') AS agent_response_rows,
  COUNTIF(event_type = 'AGENT_RESPONSE' AND JSON_VALUE(content, '$.response') IS NOT NULL) AS scalar_response_ok,
  COUNTIF(event_type = 'AGENT_RESPONSE' AND JSON_VALUE(content, '$.text_summary') IS NOT NULL) AS text_summary_ok,
  COUNTIF(event_type = 'AGENT_RESPONSE' AND COALESCE(TO_JSON_STRING(JSON_QUERY(content, '$.raw_response')), 'null') != 'null') AS raw_response_preserved
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_sdk_events`;
```

**Illustrative output from the synthetic 25-session reference dataset (not an execution log; your row counts will match your own dataset, and `scalar_response_ok` and `text_summary_ok` should equal `agent_response_rows`):**
`raw_rows = 266 | sdk_view_rows = 137 | distinct_sessions = 25 | agent_response_rows = 23 | scalar_response_ok = 23 | text_summary_ok = 23 | raw_response_preserved = 23`
*(Note: The synthetic multi-persona reference table behind these illustrative numbers also included 129 synthetic non-BQCA `TOOL_*`/`AGENT_*` spans for side-by-side comparison, which `v_bqca_customer_sdk_events` filters out; on a standard customer BQCA table within `EVAL_LOOKBACK_DAYS`, `sdk_view_rows` will equal `raw_rows`.)*

---

### Check #2: Ping Your Vertex AI Cloud Resource Connection (`AI.GENERATE` & `AI.EMBED`)

Tests IAM permissions and Vertex AI endpoint reachability (`gemini-3.5-flash` and `text-embedding-005`) on a single row — checking both `.verdict` and `.status` — before running full-table evaluations:

```sql
SELECT
  gen.verdict AS ai_generate_ping,
  gen.status AS ai_generate_status,
  ARRAY_LENGTH(
    AI.EMBED(
      'Health check embedding test',
      connection_id => 'your-gcp-project-id.us.bqca_vertex_connection',
      endpoint => 'text-embedding-005'
    ).result
  ) AS embedding_dimensions
FROM (
  SELECT AI.GENERATE(
    prompt => 'Reply with OK if Vertex AI connection is healthy.',
    connection_id => 'your-gcp-project-id.us.bqca_vertex_connection',
    endpoint => 'gemini-3.5-flash',
    model_params => JSON '{"generationConfig": {"temperature": 0.0, "maxOutputTokens": 256}}',
    output_schema => 'verdict STRING'
  ) AS gen
);
```

**Expected Healthy Output:**
`ai_generate_ping = 'OK' | ai_generate_status = '' | embedding_dimensions = 768`

---

### Check #3: Audit Token Accounting Identity & Thinking-Token Share (`v_bqca_customer_turns`)

Checks `prompt_tokens + candidate_tokens + thinking_tokens = total_tokens` across LLM-calling sessions (exact on standard Gemini calls; sessions with tool-use or system-prompt overhead tokens may have a small positive residual in `total_tokens`) and computes your token-weighted prompt cache hit rate:

```sql
SELECT
  COUNT(*) AS total_sessions,
  COUNTIF(llm_calls > 0) AS llm_sessions,
  COUNTIF(is_fast_path) AS fast_path_sessions,
  COUNTIF(llm_calls > 0 AND prompt_tokens + candidate_tokens + thinking_tokens = total_tokens) AS token_identity_ok,
  SUM(prompt_tokens) AS sum_prompt_tokens,
  SUM(candidate_tokens) AS sum_candidate_tokens,
  SUM(thinking_tokens) AS sum_thinking_tokens,
  SUM(cached_tokens) AS sum_cached_tokens,
  ROUND(SAFE_DIVIDE(SUM(cached_tokens), SUM(prompt_tokens)), 3) AS token_weighted_cache_hit_rate
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_turns`;
```

**Illustrative output from the synthetic 25-session reference dataset (`token_identity_ok` equals `llm_sessions` on standard Gemini calls without tool-use token overhead):**
`total_sessions = 25 | llm_sessions = 20 | fast_path_sessions = 3 | token_identity_ok = 20 | sum_prompt_tokens = 104620 | sum_candidate_tokens = 18731 | sum_thinking_tokens = 13685 | sum_cached_tokens = 65536 | token_weighted_cache_hit_rate = 0.626`

---

### Check #4: Instant Error & Non-`STOP` Finish Reason Triage

Surfaces every error row (`INVOCATION_ERROR`, `AGENT_ERROR`, `LLM_ERROR`) and every `LLM_RESPONSE` cut off by `MAX_TOKENS`, blocked by `SAFETY`, or missing `finish_reason` telemetry (`'(missing)'` — a telemetry gap, not a model cut-off; the notebook tracks it separately and excludes it from the Section 8.2 attention queue):

```sql
SELECT
  session_id,
  user_id,
  event_type,
  status,
  COALESCE(JSON_VALUE(attributes, '$.finish_reason'), '(missing)') AS finish_reason,
  error_message
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_sdk_events`
WHERE (status = 'ERROR' OR error_message IS NOT NULL OR ENDS_WITH(event_type, '_ERROR'))
   OR (event_type = 'LLM_RESPONSE' AND COALESCE(JSON_VALUE(attributes, '$.finish_reason'), '(missing)') != 'STOP')
ORDER BY session_id, timestamp;
```

**Expected Healthy Output:**
Returns `0` rows when all sessions completed cleanly with `finish_reason = 'STOP'`, or lists exact `session_id`, `finish_reason` (`MAX_TOKENS`, `SAFETY`, `(missing)`), and `error_message` values for triage.

---

### Check #5: 1-Cell Python Smoke Test (`client.doctor()` + `v_bqca_customer_turns`)

Paste this into any Python notebook cell to verify your SDK installation, ADC credentials, and both customer views in under 5 seconds:

```python
import bigquery_agent_analytics as bqaa

PROJECT_ID = "your-gcp-project-id"
DATASET_ID = "your_bqca_logs_dataset"
LOCATION = "US"
VERTEX_CONNECTION_ID = "your-gcp-project-id.us.bqca_vertex_connection"

bq = bqaa.make_bq_client(PROJECT_ID, location=LOCATION)
client = bqaa.Client(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id="v_bqca_customer_sdk_events",
    location=LOCATION,
    bq_client=bq,
    endpoint="gemini-3.5-flash",
    connection_id=VERTEX_CONNECTION_ID,
)

doc = client.doctor()
cols = doc["schema"].get("columns") or doc["schema"].get("present", [])
cov = doc.get("event_coverage", {})
if "error" in cov:
    raise RuntimeError(f"doctor() query error: {cov['error']}")
turns_count = list(
    bq.query(f"SELECT COUNT(*) AS n FROM `{PROJECT_ID}.{DATASET_ID}.v_bqca_customer_turns`").result()
)[0].n
print(
    f"Schema: {doc['schema']['status']} ({len(cols)} cols) | "
    f"SDK view events: {sum(v for v in cov.values() if isinstance(v, (int, float)))} | "
    f"Turns view rows: {turns_count}"
)
```

**Illustrative output from the synthetic 25-session reference dataset:**
`Schema: ok (17 cols) | SDK view events: 137 | Turns view rows: 25`

---

## 7. Production Scheduling, Looker Studio BI & Data Governance

### 7.1 Connecting `v_bqca_customer_turns` to Looker Studio or BI Dashboards
Because Cell `1.3` creates `v_bqca_customer_turns` as a standard BigQuery view with 1 row per session (including `cost_sdk_usd` and `cost_incl_thinking_usd` computed at the `COST_RATES` configured in Cell `1.1`), you can connect **Looker Studio**, **Looker**, or **Connected Sheets** directly to `PROJECT_ID.DATASET_ID.v_bqca_customer_turns` to chart:
- Daily sessions, active users (`user_id`), and user cohorts (`persona`, `persona_role`, `scenario_tag`, `data_agent_id`) over `started_at`.
- Answer delivery rate (`agent_response IS NOT NULL`), clarifying-question rate (`clarifying_question IS NOT NULL`), and error rate (`has_error = TRUE`).
- End-to-end latency P50/P90 (`e2e_latency_ms`) and Verified Queries Fast-Path share (`is_fast_path = TRUE`).
- Token consumption (`prompt_tokens`, `candidate_tokens`, `thinking_tokens`, `cached_tokens`, `total_tokens`) and estimated token cost (`cost_sdk_usd`, `cost_incl_thinking_usd`).

For the full 37-chart observability template over `v_bqca_customer_sdk_events`, see the [Looker Studio Dashboard User Manual](../../dashboard/looker_studio/USER_MANUAL.md). To drill down turn by turn on the raw logging table (prompts, responses, generated SQL, latency, tokens, and errors per data agent) with the BQCA Looker Studio profile or the Streamlit dashboard, see [Section 7.4](#74-visualizing-logs-with-the-bqca-analytics-dashboards-looker-studio--streamlit).

### 7.2 Recommended Evaluation Cadence & Cost Control
- **Real-time / Hourly (Zero LLM Cost)**: Query `v_bqca_customer_turns`, `SystemEvaluator` SLO checks (Section 7), and `bqaa.ERROR_SQL_PREDICATE` + `finish_reason != 'STOP'` (Section 8). These are pure BigQuery SQL queries with zero Vertex AI model invocations.
- **Daily Batch (`AI.CLASSIFY` & `AI.EMBED`)**: Run Question Topic Taxonomy in bulk mode (`include_justification=False`, Section 4) and Semantic Drift (`AI.EMBED` + `ML.DISTANCE`, Section 5) on new sessions from the past 24 hours (`TraceFilter(start_time=...)`).
- **Weekly or Pre-Release Audit (`AI.GENERATE` Judges & `TrialRunner`)**: Run `LLMAsJudge` sentiment/correctness/hallucination and Golden Q&A Answer Key Grading (Sections 3 and 6) over a weekly sample or after updating agent instructions and Verified Queries.

### 7.3 Data Governance, PII & Access Control

> [!CAUTION]
> **Privacy, PII & Vertex AI Model Evaluations:**
> - Raw user prompts (`user_prompt` / `content`) and agent answers (`agent_response`) logged in `agent_events` may contain sensitive business questions or user-supplied PII.
> - Keep `v_bqca_customer_sdk_events` and `v_bqca_customer_turns` in the same BigQuery dataset (`DATASET_ID`) as `agent_events` so dataset-level IAM policies and VPC Service Controls perimeter rules apply uniformly, and base-table Row-Level Security (RLS) and Column-Level Security (CLS / policy tags) on `agent_events` remain enforced when querying those views. **Important (`bqca_golden_qa` CTAS security caveat):** Because `bqca_golden_qa` is a derived physical table materialized via `CREATE TABLE IF NOT EXISTS ... AS SELECT` (Cell `5.1`) from whatever rows the executing principal can see, BigQuery does **not** automatically copy source-table row access policies or column policy tags onto `bqca_golden_qa`. Cell `5.1` inspects `RAW_TABLE_ID` via the BigQuery REST `tables.rowAccessPolicies.list` endpoint and recursive `SchemaField.policy_tags` and **fails closed** by default if any row access policy or column policy tag is present **or if the caller lacks `bigquery.rowAccessPolicies.list` to verify row access policies** (included in `roles/bigquery.dataOwner`, `roles/bigquery.admin`, and basic `Viewer`/`Editor`/`Owner` roles, not `roles/bigquery.dataEditor`; policies on tables underneath a view are not inspected): pre-create `bqca_golden_qa` in `DATASET_ID` with matching row access policies / policy tags, or set `BQCA_AUTO_SEED_GOLDEN_TABLE=false` before running Section 5 (only set `BQCA_AUTO_SEED_GOLDEN_TABLE=force` in an isolated scratch dataset).
> - When Cell `5.1` auto-bootstraps `bqca_golden_qa`, it copies up to 50 distinct historical `user_prompt` and `agent_response` values into `DATASET_ID.bqca_golden_qa`, and Sections 3–6 send `user_prompt` and `agent_response` text to Vertex AI (`AI.GENERATE`, `AI.CLASSIFY`, `AI.EMBED`, and `google-genai`) inside your GCP project (`PROJECT_ID`). Set `ANONYMIZE_USER_ID = True` in Cell `1.1` (or `BQCA_ANONYMIZE_USER_ID=true`) if you want `v_bqca_customer_turns` and Section 8.1 to SHA-256 pseudonymize `user_id`, `user_id`-derived cohort fallback labels, and email-like explicit `custom_labels.persona` labels as `user_<8hex>` (note that unsalted 8-hex SHA-256 is a 32-bit display pseudonym with ~39% birthday collision probability at 65,536 distinct users and ~50% around ~77,000 distinct users, rather than cryptographic anonymization). Masking covers the turns view and Section 8.1 cohort labels only: prompts, answers, other explicit persona labels/emails, and the `v_bqca_customer_sdk_events` compatibility view are outside its scope and still carry raw text. Restrict `agent_events`, `v_bqca_customer_sdk_events`, `v_bqca_customer_turns`, and `bqca_golden_qa` (as well as any executed notebooks or HTML reports exported via Option C, which embed sample prompts, responses, and `user_id`-derived cohort labels unless `ANONYMIZE_USER_ID = True`) to authorized data stewards, and grant broader BI viewers access only to aggregated metrics views.
> - **Endpoint data residency:** Distinguish the BigQuery query/connection region (`LOCATION` and `VERTEX_CONNECTION_ID`) from the Vertex AI inference endpoint. In-warehouse `AI.GENERATE` / `AI.CLASSIFY` executes the BigQuery job at `LOCATION`, while BigQuery automatically routes short `gemini-3.5-flash` model names from US single regions (e.g. `us-central1`) to the `us` multi-region endpoint, from eligible EU single regions to the `eu` multi-region endpoint, and from other locations to `global` (in `asia-south1`, pass a fully qualified `global` endpoint URL; see [BigQuery generative AI locations](https://cloud.google.com/bigquery/docs/generative-ai-overview#locations)). By contrast, direct `google-genai` calls in Section 6B and `api_fallback` use `VERTEX_LOCATION` (default `"global"`) directly without BigQuery's multi-region routing. For US or EU multi-region data residency with `gemini-3.5-flash`, keep your BigQuery dataset in `US` or `EU` and set `VERTEX_LOCATION = "us"` or `"eu"`; if you require strict single-region inference in a region such as `us-central1` (for both warehouse `AI.*` and direct `google-genai` calls), set `LOCATION = "us-central1"`, `VERTEX_LOCATION = "us-central1"`, and switch `JUDGE_MODEL`, `CATEGORICAL_MODEL`, and `DIRECT_JUDGE_MODEL` to a single-region model such as `"gemini-2.5-flash"` (see [Vertex AI generative AI locations](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/locations)).

### 7.4 Visualizing Logs with the BQCA Analytics Dashboards (Looker Studio & Streamlit)

Sections 7.1–7.3 chart the session-level `v_bqca_customer_turns` view. For turn-level drill-down straight on the raw logging table, the SDK ships two dashboards for BQCA Prompt & Response Logging. Use either one, or both: they read the same table, only the nine event types BQCA logs, and they attribute each turn to its data agent through the `data-agent-id` label.

| | **Looker Studio** | **Streamlit** |
|---|---|---|
| Runs as | A report you copy into your own Looker Studio account | A self-hosted Python app (laptop, VM, or Cloud Run) |
| Setup | One CLI command or a configurator link; no servers | `pip install -e '.[streamlit]'`, then `streamlit run` |
| Best for | Sharing charts with BI viewers | Reading prompts, responses, and generated SQL turn by turn, with a per-query cost cap |
| Reference | [Looker Studio README](../../dashboard/looker_studio/README.md), [User Manual](../../dashboard/looker_studio/USER_MANUAL.md) | [Streamlit README](../../dashboards/streamlit/README.md) |

**Before you start.** Both dashboards read the raw logging table: `agent_events` in this manual's notebook (`RAW_TABLE_ID`), while the dashboards default to `bqca_prompt_response_logs`, so pass your own table name as shown below. Whoever runs a dashboard needs `roles/bigquery.jobUser` on the project and `roles/bigquery.dataViewer` on the dataset that holds the table. Neither dashboard needs the notebook's views.

#### Looker Studio

```bash
# From the repository root. Validates your table, then prints a link that
# copies the dashboard template into your Looker Studio account.
cd dashboard/looker_studio
python3 tools/hydrate_dashboard.py \
  --profile bqca \
  --project YOUR_PROJECT_ID \
  --dataset YOUR_DATASET_ID \
  --table YOUR_LOGS_TABLE \
  --location US \
  --custom-sql-out /tmp/bqca_events.sql
```

Set `--location` to the location of your dataset. Prefer a browser? Open the web configurator's BQCA page, enter the same project, dataset, and table, and follow the link it builds:

```text
https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/bqca/
```

The Linking API URL opens the **dedicated 7-page tool-free BQCA Looker Studio template** (`1ffb0888-20ea-451f-aeb8-69fc37973335`, alias `ds0`) backed by `sql/bqca_events_v1.template.sql` — covering Token Consumption, Data Agents & Turns, LLM Interactions & Embedding Suggestions, User & Persona Analytics, Latency & Fast-Path ROI, Errors (BQCA 3-Condition), and the Prompt, Response & SQL Inspector, with all tool-related pages and charts omitted. `--custom-sql-out` also writes the standalone BQCA reporting query bound to your table. The [Looker Studio README](../../dashboard/looker_studio/README.md) describes the BQCA profile and its derived columns.

> [!NOTE]
> **External public-sharing status (`1ffb0888-20ea-451f-aeb8-69fc37973335`):** Anonymous external (`allUsers`) link sharing for the hosted 1-click Looker Studio template is currently pending organization public-sharing allowlist approval (`link_access: PENDING_PUBLIC_SHARING_ALLOWLIST` in `dashboard/looker_studio/bindings/bqca_report_template.yaml`). Until anonymous link sharing is active, external accounts that see a Looker Studio permission prompt on the 1-click link should either use the self-hosted **Streamlit dashboard** below (`dashboards/streamlit/`) or generate the hydrated Custom SQL query with `hydrate_dashboard.py --profile bqca ... --custom-sql-out /tmp/bqca_events.sql` and connect it directly as a BigQuery Custom Query data source in Looker Studio.

#### Streamlit

```bash
# From the repository root.
pip install -e '.[streamlit]'
gcloud auth application-default login   # or set GOOGLE_APPLICATION_CREDENTIALS

cd dashboards/streamlit
BQ_PROJECT_ID=YOUR_PROJECT_ID \
BQ_DATASET_ID=YOUR_DATASET_ID \
BQCA_TABLE_ID=YOUR_LOGS_TABLE \
BQAA_PROFILE=bqca \
streamlit run app.py
```

The app opens on **BQCA Prompt & Response Logging**. Adding `?profile=bqca` to the URL (for example `http://localhost:8501/?profile=bqca`) or choosing it under the sidebar's **Dashboard Surface** opens the same surface. Run from `dashboards/streamlit`, the app binds to `127.0.0.1` only, which is the right default for a tool that shows prompts and responses.

It shows nine KPI tiles and five tabs:

| Tab | Answers |
|---|---|
| Overview & Latency | How many turns, how many failed, and how fast, split by fast path and standard NL2SQL |
| Data Agents & Personas | Which data agents and personas drive the traffic |
| Prompt, Response & SQL Explorer | What a user asked, what was answered (the last response of the turn), and which SQL that answer contained |
| Tokens & Embedding Suggestions | Token spend over time and by model, which embedding suggestions the agent made and why, and how many columns each proposed |
| Error Attribution | Which data agents and event types produce errors, and the most frequent error messages |

The **Embedding Suggestion Coverage** tile is the share of turns that received at least one embedding suggestion with suggested columns. A line under the tiles says how many turns completed (reached `INVOCATION_COMPLETED`): a turn that is still running, failed before completing, or was cut off by the time range counts toward **Total Turns** and the error rate, but the latency percentiles and the fast-path rate cover completed turns only.

Filters (data agent, persona, event type, fast path, session / conversation, prompt text, and errors only) take effect when you press **Apply filters**. The [Streamlit README](../../dashboards/streamlit/README.md#6-bqca-prompt--response-logging-dashboard) lists which panels each filter narrows.

#### Troubleshooting

| Symptom | Fix |
|---|---|
| Every tile shows `—`, or a panel reports a missing table | Check the table name (**Events table** in Streamlit, `--table` for Looker Studio): the dashboards default to `bqca_prompt_response_logs`, your logs may be in `agent_events`. In Streamlit, `BQCA_TABLE_ID` (or the shared `BQ_TABLE_ID`) overrides the default |
| Streamlit opens on the wrong table | **Events table** starts from `BQCA_TABLE_ID`, else `BQ_TABLE_ID`, else the `bqca_prompt_response_logs` default. A `BQ_TABLE_ID` in your environment or `.env` is shared with the ADK surface and wins over the default: set `BQCA_TABLE_ID` to override it for BQCA only, or edit **Events table** and press **Connect** |
| A Streamlit panel refuses to run because of the scan cap | Narrow the time range or raise **Per-query scan cap**. The Prompt, Response & SQL Explorer reads full prompts and responses, so it scans the most |
| Looker Studio opened an 8-page ADK template with empty tool pages | You opened the ADK surface instead of the BQCA surface: use `hydrate_dashboard.py --profile bqca` or the `/bqca/` web configurator (`?profile=bqca`) to open the dedicated 7-page tool-free BQCA template |
| Looker Studio asks to request access on the 1-click template link | External (`allUsers`) link sharing for `1ffb0888-20ea-451f-aeb8-69fc37973335` is pending allowlist approval (`PENDING_PUBLIC_SHARING_ALLOWLIST`); use the self-hosted Streamlit dashboard (`dashboards/streamlit/`) or pass `--custom-sql-out /tmp/bqca_events.sql` to `hydrate_dashboard.py` and connect your own Looker Studio report to that BigQuery Custom Query |

> [!CAUTION]
> Both dashboards show raw prompts and responses, so the guidance in Section 7.3 applies to them as well: grant dataset access only to authorized data stewards. The Streamlit app runs every query as the server's own BigQuery identity and has no login of its own, so keep it on `127.0.0.1` or put an authenticating reverse proxy such as IAP in front of it before you share a URL.

---

## 8. FAQ & Quick-Reference Cheat Sheet

### 8.1 Frequently Asked Questions (FAQ)

- **Q1: Can I run the notebook before creating a curated `bqca_golden_qa` table?**
  **Yes.** Sections 1, 2, 3, 4, 6B, 7, and 8 require only `agent_events`, and Cell `5.1` (`Cell [18]`) automatically runs `CREATE TABLE IF NOT EXISTS` to bootstrap an unverified starter `bqca_golden_qa` template (`verified = FALSE`) from your clean single-turn production sessions (`turns = 1`) so Sections 5 and 6A also run immediately. Replace or update those rows with SME-verified answers (`SET verified = TRUE`) before treating Section 6A as a ground-truth accuracy KPI.
- **Q2: Why does `v_bqca_customer_turns` show `0` tokens (`llm_calls = 0`) on some answered sessions?**
  Those sessions were answered by the **Verified Queries Fast Path** (`attributes.fast_path = true` / `is_fast_path = TRUE`), which matches a verified query template without invoking an LLM generation call.
- **Q3: Why should I point `Client` at `v_bqca_customer_sdk_events` instead of raw `agent_events`?**
  Because raw BQCA `AGENT_RESPONSE` events store `content.response` as a nested JSON object (`{"parts": [{"markdown": "..."}]}`), whereas `LLMAsJudge` in `bigquery-agent-analytics` v0.5.4 reads scalar `JSON_VALUE(content, '$.response')`. `v_bqca_customer_sdk_events` flattens `$.response` and `$.text_summary` to scalar strings while preserving the original object in `$.raw_response`.

### 8.2 Quick-Reference Cheat Sheet

| Goal | SDK / SQL Surface to Use | Target Table / View |
| :--- | :--- | :--- |
| **Verify schema & connection health** | `client.doctor()` + 1-row `AI.GENERATE` ping (`gemini-3.5-flash`, checking `.verdict` & `.status`) | `v_bqca_customer_sdk_events` |
| **Explore prompt/response pairs & latency** | `SELECT * FROM v_bqca_customer_turns` | `v_bqca_customer_turns` |
| **Find frustrated or confused users** | `client.evaluate(LLMAsJudge.sentiment())` + `client.evaluate_categorical()` | `v_bqca_customer_sdk_events` |
| **Classify user questions by business topic** | `client.evaluate_categorical()` (`include_justification=False` for `AI.CLASSIFY`, `True` for `AI.GENERATE`) | `v_bqca_customer_sdk_events` |
| **Detect new / drifted user questions** | `client.drift_detection()` + `AI.EMBED` / `ML.DISTANCE` | `v_bqca_customer_turns` + `bqca_golden_qa` |
| **Grade answer accuracy against ground truth** | Section 6A `AI.GENERATE` Golden Q&A SQL Grader (over Section 5.2 matched pairs) | `v_bqca_customer_turns` + `bqca_golden_qa` |
| **Run multi-trial judge consistency (`pass@3`) & composite grading** | `BQCAPerformanceEvaluator` + `TrialRunner` + `GraderPipeline` (`VERTEX_LOCATION="global"`) | `v_bqca_customer_sdk_events` |
| **Audit token spend (incl. thinking tokens) & Fast-Path ROI** | `SystemEvaluator` + Section 7 3-term token cost query (`cost_incl_thinking_usd`) | `v_bqca_customer_sdk_events` & `v_bqca_customer_turns` |
| **Monitor errors, `MAX_TOKENS`/`SAFETY` cut-offs & refusals** | `bqaa.ERROR_SQL_PREDICATE` + `finish_reason != 'STOP'` + `delivery_outcome` | `v_bqca_customer_sdk_events` |
