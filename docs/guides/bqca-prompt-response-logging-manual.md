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

When you enable **Prompt and Response Logging** in **BigQuery Conversational Analytics (BQCA)**, BigQuery streams your users' conversation events into an `agent_events` table in your BigQuery dataset using the 17-column schema consumed by the open-source [BigQuery Agent Analytics SDK](../../README.md) (`bigquery-agent-analytics`).

This manual shows your data platform, BI, and analytics engineering teams how to:
1. **Launch and self-host** the starter notebook in **Google Colab**, **BigQuery Studio**, **Vertex AI Colab Enterprise**, **Vertex AI Workbench / Local JupyterLab**, or **headless CI/CD schedules** in under 5 minutes.
2. **Configure IAM permissions and Vertex AI Cloud Resource Connections** with copy-paste `gcloud` and `bq` CLI commands.
3. **Customize thresholds, taxonomies, and Golden Q&A answer keys** across all 8 evaluation and FinOps workflows (using **`gemini-3.5-flash`** as the default BigQuery ML AI endpoint).
4. **Self-debug any schema, permission, or SDK behavior question** using the **10-Issue Troubleshooting Runbook** and **5 Copy-Paste SQL/Python Diagnostic Checks**.

---

## Table of Contents
1. [5-Minute Quick-Start Checklist & End-to-End Architecture](#1-5-minute-quick-start-checklist--end-to-end-architecture)
2. [Step-by-Step Self-Hosting Setup (APIs, Connection, IAM & Runtimes)](#2-step-by-step-self-hosting-setup-apis-connection-iam--runtimes)
3. [Understanding the 8-Workflow Playbook & Capability Matrix](#3-understanding-the-8-workflow-playbook--capability-matrix)
4. [Customizing the Playbook for Your Own Business Domain](#4-customizing-the-playbook-for-your-own-business-domain)
5. [Self-Debugging & Troubleshooting Runbook (10 Common Issues)](#5-self-debugging--troubleshooting-runbook-10-common-issues)
6. [Copy-Paste Diagnostic SQL & Python One-Liners (5 Live Checks)](#6-copy-paste-diagnostic-sql--python-one-liners-5-live-checks)
7. [Production Scheduling, Looker Studio BI & Data Governance](#7-production-scheduling-looker-studio-bi--data-governance)
8. [FAQ & Quick-Reference Cheat Sheet](#8-faq--quick-reference-cheat-sheet)

---

## 1. 5-Minute Quick-Start Checklist & End-to-End Architecture

### 1.1 3-Step Quick-Start Checklist

- [ ] **Step 1 — Launch the Notebook**: Click **[Run in Colab](https://colab.research.google.com/github/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, **[Open in BQ Studio](https://console.cloud.google.com/bigquery/import?url=https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, or open [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) in JupyterLab / Vertex AI Workbench.
- [ ] **Step 2 — Fill in 3 Parameters in Cell `1.1 Customer Configuration Parameters` (`Cell [4]`)**:
  - `PROJECT_ID = "your-gcp-project-id"`
  - `DATASET_ID = "your_bqca_logs_dataset"`
  - `VERTEX_CONNECTION_ID = "projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection"`
  - *(Leave `RAW_TABLE_ID = "agent_events"` — the default table created by BQCA Prompt & Response Logging — and `JUDGE_MODEL = "gemini-3.5-flash"`.)*
- [ ] **Step 3 — Click `Runtime -> Run all`**:
  - **Section 1** installs `bigquery-agent-analytics==0.5.4` if missing, creates two zero-copy BigQuery views (`v_bqca_customer_sdk_events` and `v_bqca_customer_turns`), runs `client.doctor()`, and runs a 1-row `AI.GENERATE` health ping.
  - **Sections 2, 3, 4, 6B, 7, and 8** run directly on your `agent_events` logs with **zero external tables required**.
  - **Sections 5 and 6A** automatically bootstrap an unverified starter `bqca_golden_qa` template table (`CREATE TABLE IF NOT EXISTS`) if you do not have one yet, so all 32 cells run end-to-end on your first click.

### 1.2 End-to-End Architecture

```text
+-----------------------------------------------------------------------------------+
| 1. BigQuery Conversational Analytics (BQCA) Runtime                               |
|    Streams Prompt & Response events to your BigQuery dataset                      |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| 2. Raw BQCA Events Table: `PROJECT_ID.DATASET_ID.agent_events`                    |
|    - 17-column BigQuery Agent Analytics schema                                    |
|    - 9 standard BQCA event types (prompts, responses, LLM telemetry, errors)      |
+-----------------------------------------------------------------------------------+
                                         |
            +----------------------------+----------------------------+
            | (Provisioned automatically by Cell 1.3 of Notebook)     |
            v                                                         v
+-------------------------------------------+   +-----------------------------------+
| 3A. SDK Compatibility View                |   | 3B. Flat Conversation Turns View  |
|     `v_bqca_customer_sdk_events`          |   |     `v_bqca_customer_turns`       |
| - Filters to standard BQCA event types    |   | - 1 row per session / turn        |
|   within `EVAL_LOOKBACK_DAYS`             |   | - Pairs `user_prompt` with        |
| - Normalizes `AGENT_RESPONSE` JSON object |   |   `agent_response`, latency,      |
|   `$.response` & `$.text_summary` into    |   |   tokens, `thinking_tokens`,      |
|   scalar strings required by SDK judges   |   |   `cost_sdk_usd`,                 |
| - Preserves nested JSON in `$.raw_response`|   |   `cost_incl_thinking_usd`,       |
|                                           |   |   `is_fast_path`, & error flags   |
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

BigQuery Conversational Analytics Prompt & Response Logging emits 9 standard prompt, model-response, agent-response, lifecycle, and error event types (`BQCA_EVENT_TYPES`), while low-level ADK tool and agent spans (`AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR`) are not emitted to customer tables:

| Event Category | `event_type` | What BQCA Populates | Where the Starter Notebook Uses It |
| :--- | :--- | :--- | :--- |
| **User Input** | `USER_MESSAGE_RECEIVED` | `content.text_summary` contains the user's natural-language prompt. | Sections 1–6 (Conversation Explorer, Sentiment, Taxonomy, Drift, Quality Grading) |
| **Agent Output** | `AGENT_RESPONSE` | `content.response` contains `{parts: [{markdown: "..."}], clarifying_question: "..."}` (normalized to scalar string in `v_bqca_customer_sdk_events`). `attributes.fast_path` marks Verified Queries fast-path turns. | Sections 1–8 (All answer quality, sentiment, refusal, and fast-path workflows) |
| **Turn Lifecycle** | `INVOCATION_STARTING`<br>`INVOCATION_COMPLETED` | `INVOCATION_COMPLETED.latency_ms.total_ms` records true end-to-end turn latency; `attributes` carries `data_agent_id`, `conversation_id`, and `custom_labels`. | Sections 1, 2, 7 (`v_bqca_customer_turns` and end-to-end latency P50/P90) |
| **Model Telemetry** | `LLM_RESPONSE` | `content` is `NULL` by design; `LLM_RESPONSE.attributes.usage_metadata` logs `prompt_token_count`, `candidates_token_count`, `thoughts_token_count`, `cached_content_token_count`, and `total_token_count`; `attributes.finish_reason` logs `STOP`, `MAX_TOKENS`, or `SAFETY`; `latency_ms` logs `total_ms` and `time_to_first_token_ms`. | Sections 2, 7, 8 (Token & thinking-token FinOps, cache hit rate, TTFT, and `finish_reason` cut-off alerts) |
| **Suggestions** | `EMBEDDING_SUGGESTION` | Logs embedding-based query/question suggestion metadata when triggered. | Section 1 (`client.doctor()` coverage check) |
| **Error Events** | `INVOCATION_ERROR`<br>`AGENT_ERROR`<br>`LLM_ERROR` | `status = 'ERROR'`, `content IS NULL`, and `error_message` contains a sanitised diagnostic message (`[REDACTED]` for SQL/table identifiers). | Sections 2, 8 (`bqaa.ERROR_SQL_PREDICATE` operational health monitor) |

> **Note on Non-BQCA Event Types in `client.doctor()`:** Because `AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, `TOOL_COMPLETED`, and `TOOL_ERROR` are not emitted to customer BQCA tables, `client.doctor()` will list those ADK event types under `No events for types: ...` in its `warnings` array. That is **expected on BQCA logs**.

---

## 2. Step-by-Step Self-Hosting Setup (APIs, Connection, IAM & Runtimes)

Before running the notebook against your GCP project for the first time, run Steps **2.1–2.3** in Cloud Shell or your local terminal.

### Step 2.1 Enable Required Google Cloud APIs

```bash
export PROJECT_ID="your-gcp-project-id"
export DATASET_ID="your_bqca_logs_dataset"
export LOCATION="US"                 # BigQuery dataset location (e.g., US, EU, us-central1)
export CONN_LOCATION="us"            # Connection location matching your dataset (us, eu, us-central1)
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

> **Note:** IAM propagation typically takes 30–60 seconds. Your full `VERTEX_CONNECTION_ID` string for Cell `1.1` of the notebook is:
> `projects/${PROJECT_ID}/locations/${CONN_LOCATION}/connections/${CONN_ID}`

### Step 2.4 Required IAM Roles for the Notebook Runner

Grant the user (or service account) executing the notebook the following roles:

| Resource Scope | Required IAM Role | Why the Notebook Needs It |
| :--- | :--- | :--- |
| **BigQuery Dataset (`DATASET_ID`)** | `roles/bigquery.dataEditor` | Allows Cell `1.3` to run `CREATE OR REPLACE VIEW` for `v_bqca_customer_sdk_events` and `v_bqca_customer_turns`, allows Cell `5.1` to bootstrap `bqca_golden_qa`, and reads `agent_events`. |
| **GCP Project (`PROJECT_ID`)** | `roles/bigquery.jobUser` | Allows the notebook to submit BigQuery SQL queries and SDK evaluation jobs. |
| **BigQuery Connection (`VERTEX_CONNECTION_ID`)** | `roles/bigquery.connectionUser` | Allows the notebook's queries to invoke `AI.GENERATE`, `AI.CLASSIFY`, and `AI.EMBED` via the Cloud Resource Connection. |
| **GCP Project (`PROJECT_ID`)** | `roles/aiplatform.user` | Required by Cell `6B` when `GraderPipeline` and `TrialRunner` call Vertex AI Gemini (`google-genai`) directly. |

### Step 2.5 Choose Your Execution Environment (3 Ways to Run)

#### Option A — Google Colab, BigQuery Studio, or Vertex AI Colab Enterprise (Fastest: 5-Minute Setup)
1. **In Google Colab**: Click **[Run in Colab](https://colab.research.google.com/github/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)** and optionally **File -> Save a copy in Drive**.
2. **In BigQuery Studio / Vertex AI Colab Enterprise (Recommended for VPC Service Controls)**:
   - Click **[Open in BQ Studio](https://console.cloud.google.com/bigquery/import?url=https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/examples/bqca_prompt_response_logging_customer_notebook.ipynb)**, or download [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) and import it under **Vertex AI -> Colab Enterprise -> Notebooks**.
3. **Edit Cell `1.1 Customer Configuration Parameters` (`Cell [4]`)**: Update `PROJECT_ID`, `DATASET_ID`, and `VERTEX_CONNECTION_ID`.
4. **Run**: Click **Runtime -> Run all**. Cell `1.0` automatically installs `bigquery-agent-analytics[llm,bigframes]==0.5.4` in any runtime where it is not yet installed.

#### Option B — Self-Hosted JupyterLab, Vertex AI Workbench, or VS Code
We recommend **Python 3.11 or 3.12**:

```bash
# 1. Create and activate a clean Python 3.11+ virtual environment
python3.11 -m venv .bqca_nb_venv
source .bqca_nb_venv/bin/activate
pip install --upgrade pip

# 2. Install the BigQuery Agent Analytics SDK v0.5.4+ and notebook dependencies
pip install "bigquery-agent-analytics[llm,bigframes]>=0.5.4" \
  google-cloud-bigquery pandas pyarrow db-dtypes ipykernel jupyterlab nbclient nbformat

# 3. Authenticate Application Default Credentials (ADC)
gcloud auth application-default login --project="${PROJECT_ID}"

# 4. Launch JupyterLab and open the starter notebook
jupyter lab examples/bqca_prompt_response_logging_customer_notebook.ipynb
```

#### Option C — Headless Scheduled Execution (CI/CD, Cloud Run Jobs, or Vertex AI Pipelines)

Because Cell `1.1` reads `BQCA_PROJECT_ID`, `BQCA_DATASET_ID`, `BQCA_RAW_TABLE_ID`, `BQCA_LOCATION`, `BQCA_VERTEX_LOCATION`, and `BQCA_VERTEX_CONNECTION_ID` from environment variables when set, you can execute the notebook headlessly in CI/CD without editing the `.ipynb` file:

```bash
export BQCA_PROJECT_ID="your-gcp-project-id"
export BQCA_DATASET_ID="your_bqca_logs_dataset"
export BQCA_RAW_TABLE_ID="agent_events"
export BQCA_LOCATION="US"
export BQCA_VERTEX_CONNECTION_ID="projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection"

# Execute all 32 cells headlessly and save the populated notebook with fresh outputs:
jupyter nbconvert --to notebook --execute \
  --ExecutePreprocessor.timeout=600 \
  --output bqca_customer_report_$(date +%F).ipynb \
  examples/bqca_prompt_response_logging_customer_notebook.ipynb

# Optional: Export the executed notebook directly to a standalone HTML report for stakeholders:
jupyter nbconvert --to html bqca_customer_report_$(date +%F).ipynb
```

---

## 3. Understanding the 8-Workflow Playbook & Capability Matrix

The table below maps every section in [`examples/bqca_prompt_response_logging_customer_notebook.ipynb`](../../examples/bqca_prompt_response_logging_customer_notebook.ipynb) (`32` cells: `18` code cells with `#@title` headers `1.0`–`8.3` and `14` markdown cells) to the customer question it answers, what it computes, and example output from a 25-session, 5-cohort sample dataset (your metrics will reflect your own dataset):

| Workflow Section | Notebook Code Cells (`#@title`) | Customer Question Answered & What It Computes | Key Knobs You Can Customize | Example Output (25-Session Sample Dataset) |
| :--- | :--- | :--- | :--- | :--- |
| **Section 1: Setup, Views & `doctor()`** | `1.0 Install & Authenticate`<br>`1.1 Customer Configuration Parameters`<br>`1.2 Shared BigQuery Helpers`<br>`1.3 Provision Customer Views`<br>`1.4 Run client.doctor() & Ping` | *Is my dataset and connection ready?* Installs SDK if missing, validates `#@param` config, creates `v_bqca_customer_sdk_events` and `v_bqca_customer_turns` over `RAW_TABLE_ID`, runs `client.doctor()`, and executes a 1-row `AI.GENERATE` ping (`gemini-3.5-flash`). | `PROJECT_ID`, `DATASET_ID`, `RAW_TABLE_ID`, `VERTEX_CONNECTION_ID`, `EVAL_LOOKBACK_DAYS` | Schema `ok` (`17` cols); `137` events across `25` sessions and `8` active event types; `23/23` `AGENT_RESPONSE` rows normalized; `AI.GENERATE` ping `'OK'`. |
| **Section 2: Conversation Explorer** | `2.1 Conversation Explorer & Cohort Summary` | *What are users asking and how often does the agent answer?* Summarizes sessions, answered turns, clarifying questions, fast-path usage, errors, median latency, and tokens per `data_agent_id` and user cohort. | Group by `data_agent_id`, `user_id`, or `persona` / `persona_role` / `scenario_tag`. | `23/25` sessions delivered an answer (`1` clarifying question); `3` hit Verified Queries fast path; `2` ended in an error. |
| **Section 3: Sentiment & Frustrated-Turn Triage** | `3.1 Sentiment Evaluation`<br>`3.2 UX & Tone Categorical Rubric` | *Which users are frustrated or confused?* Runs `LLMAsJudge.sentiment(threshold=JUDGE_THRESHOLD)` and `client.evaluate_categorical()` with `customer_ux_tone`, joining scores back to `v_bqca_customer_turns`. | Adjust `JUDGE_THRESHOLD` (default `0.6`) or edit categories in `ux_tone_metric`. | `22/25` sessions passed the `0.6` sentiment bar (`ai_generate`); UX rubric flagged `1` `Frustrated_Impatient` and `1` `Clarification_Needed`. |
| **Section 4: Question Topic Taxonomy** | `4.1 Question Topic Taxonomy (AI.CLASSIFY vs AI.GENERATE)` | *What topics do users ask about most?* Classifies user questions into 5 topic buckets using both `AI.CLASSIFY` (`include_justification=False`, fast bulk mode) and `AI.GENERATE` (`include_justification=True`, audited mode). | Replace the `CategoricalMetricCategory` list in `topic_metric` with your own business domains. | `AI.CLASSIFY` and `AI.GENERATE` (`gemini-3.5-flash`) agreed on `25/25` sessions (`Ranking_TopN: 9`, `Out_of_Scope_Drift: 7`, `Cohort_Comparison: 4`, `Aggregation_KPI: 3`, `Schema_Metadata: 2`). |
| **Section 5: Semantic Drift & Golden Coverage** | `5.1 Bootstrap or Load Golden Question Bank`<br>`5.2 Production-Centric Semantic Drift Query` | *Which incoming questions are outside our Golden Question Bank?* Auto-bootstraps `bqca_golden_qa` if absent, runs `client.drift_detection()`, and ranks novel user prompts with `cosine_distance > DRIFT_THRESHOLD`. | Tune `DRIFT_THRESHOLD = 0.15` (tighter) vs. `0.30` (broader). | `88.2%` Golden Question Bank coverage (`15/17` covered); `9` production sessions drifted beyond cosine distance `0.15` (median `0.564` on out-of-domain cohort). |
| **Section 6: Response-Quality Evaluation** | `6A. Golden Q&A Answer Key Grading`<br>`6B. Reference-Free Judges, GraderPipeline & TrialRunner` | *Are the agent's numbers and entities accurate?* **6A**: Matches in-domain sessions (`question_distance <= DRIFT_THRESHOLD`) to `bqca_golden_qa` and grades factual consistency with `AI.GENERATE` (`PASS`/`FAIL`).<br>**6B**: Runs `LLMAsJudge.correctness()`, `LLMAsJudge.hallucination()`, `GraderPipeline`, and 3-trial `TrialRunner` judge consistency (`pass@3` / `pass^3`). | Tune `DRIFT_THRESHOLD`, `PIPELINE_WEIGHTS`, and `num_trials=3`. | Golden Q&A SQL grading: `15 PASS` and `1 FAIL` across `16` in-domain sessions (caught `session_hallucinated_retrieval_demo`); `GraderPipeline` (`1.00` vs. `0.69`) and `TrialRunner pass@3` (`1.00` vs. `0.00`) cleanly separated accurate vs. hallucinated answers. |
| **Section 7: FinOps, Thinking Tokens & Fast-Path ROI** | `7.1 Automated Session SLO Checks`<br>`7.2 Thinking-Token Cost Accounting & Fast-Path ROI` | *What is our token-equivalent spend and Fast-Path ROI?* Runs `SystemEvaluator` SLO checks (`latency`, `token_efficiency`, `cost_per_session`, `context_cache_hit_rate`, `turn_count`, `ttft`), estimates `thoughts_token_count` cost impact, and compares Verified Queries Fast-Path vs. standard NL2SQL. | Edit `BUDGETS` (`latency_ms: 3000`, `total_tokens: 8000`, `cost_usd: 0.02`, etc.) and `COST_RATES` (`input_per_1k`, `output_per_1k`). | Latency SLO `22/25`, token SLO `22/25`, token-weighted cache hit rate `0.63`; including `thoughts_token_count` adds `+43%` to token-equivalent SDK cost (`$0.318` -> `$0.455`); Fast-Path median latency `1,080 ms` (`0` tokens) vs. `3,215 ms`. |
| **Section 8: Operational Health Monitor & Scorecard** | `8.1 Model Finish Reasons & Sanitised Error Events`<br>`8.2 Delivery Outcome Classification`<br>`8.3 Customer Production Health Summary Table` | *Did any session fail, get truncated, or refuse a query?* Combines `finish_reason != 'STOP'`, error rows (`bqaa.ERROR_SQL_PREDICATE`), and `delivery_outcome` classification into an Operational Attention Queue and renders the live **Customer Production Health Summary Table**. | Customize `classify_error_message()` or `outcome_metric` categories. | `6` error rows in `2` sessions (`content IS NULL`: `True`); `2` non-STOP `finish_reason` sessions (`MAX_TOKENS`, `SAFETY`); `6` polite out-of-scope refusals. |

---

## 4. Customizing the Playbook for Your Own Business Domain

### 4.1 Complete `#@param` Configuration Reference (`Cell 1.1` / `Cell [4]`)

| Parameter | Default Value in Notebook | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | `"your-gcp-project-id"` | GCP project ID hosting your BigQuery dataset and Vertex AI connection (or set `BQCA_PROJECT_ID`). |
| `DATASET_ID` | `"your_bqca_logs_dataset"` | BigQuery dataset where BQCA writes `agent_events` (or set `BQCA_DATASET_ID`). |
| `RAW_TABLE_ID` | `"agent_events"` | Raw table written by BQCA Prompt & Response Logging (or set `BQCA_RAW_TABLE_ID`). |
| `COMPAT_VIEW_ID` | `"v_bqca_customer_sdk_events"` | Auto-provisioned SDK compatibility view created by Cell `1.3`. |
| `TURNS_VIEW_ID` | `"v_bqca_customer_turns"` | Auto-provisioned 1-row-per-session flat view created by Cell `1.3`. |
| `GOLDEN_QA_TABLE_ID` | `"bqca_golden_qa"` | Golden Question & Answer table used by Sections 5 and 6A (auto-bootstrapped if absent). |
| `LOCATION` | `"US"` | BigQuery dataset location (`"US"`, `"EU"`, `"us-central1"`, etc.; or set `BQCA_LOCATION`). Must match the region of `DATASET_ID` and `VERTEX_CONNECTION_ID`. |
| `VERTEX_LOCATION` | `"us-central1"` | Vertex AI region used by direct `google-genai` calls in Section 6B (`GraderPipeline` and `TrialRunner`; or set `BQCA_VERTEX_LOCATION`). |
| `VERTEX_CONNECTION_ID` | `"projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection"` | BigQuery Cloud Resource Connection used by `AI.GENERATE`, `AI.CLASSIFY`, and `AI.EMBED` (or set `BQCA_VERTEX_CONNECTION_ID`). |
| `JUDGE_MODEL` | `"gemini-3.5-flash"` | Default BigQuery ML `AI.GENERATE` judge endpoint (`LLMAsJudge` and Section 6A Golden Q&A grading). Can also be set to `"gemini-2.5-flash"`. |
| `CATEGORICAL_MODEL` | `"gemini-3.5-flash"` | Default BigQuery ML endpoint used by `client.evaluate_categorical()` (`AI.CLASSIFY` and `AI.GENERATE`). Can also be set to `"gemini-2.5-flash-lite"`. |
| `EMBEDDING_MODEL` | `"text-embedding-005"` | Vertex AI embedding model used by `AI.EMBED` in Sections 5 and 6A. |
| `DIRECT_JUDGE_MODEL` | `"gemini-3.5-flash"` | Direct `google-genai` Vertex AI model used by `GraderPipeline` and `TrialRunner` in Section 6B. |
| `JUDGE_THRESHOLD` | `0.6` | Minimum score (`[0.0, 1.0]`) required for a session to pass `LLMAsJudge` evaluations. |
| `DRIFT_THRESHOLD` | `0.15` | Cosine distance threshold (`ML.DISTANCE(..., 'COSINE')`) for Golden Question Bank coverage and semantic drift detection. |
| `EVAL_LOOKBACK_DAYS` | `90` | Lookback window in days applied to `v_bqca_customer_sdk_events` and `v_bqca_customer_turns` to bound scan costs on large tables. |
| `MAX_EVAL_SESSIONS` | `100` | Maximum number of sessions evaluated per `client.evaluate()`, `client.evaluate_categorical()`, and `AI.EMBED` batch run. |

### 4.2 How `bqca_golden_qa` Auto-Bootstrapping Works (and How to Curate Your Own Answer Key)

When you run **Section 5 (`Cell 5.1` / `Cell [18]`)**, the notebook runs `CREATE TABLE IF NOT EXISTS` to bootstrap `bqca_golden_qa` from up to 50 clean, answered turns in `v_bqca_customer_turns` if the table does not exist yet:

```sql
-- Executed automatically by Cell 5.1 if `bqca_golden_qa` does not exist yet:
CREATE TABLE IF NOT EXISTS `your-gcp-project-id.your_bqca_logs_dataset.bqca_golden_qa` AS
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
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_turns`
WHERE user_prompt IS NOT NULL
  AND agent_response IS NOT NULL
  AND NOT has_error
  AND clarifying_question IS NULL
ORDER BY started_at ASC, session_id ASC
LIMIT 50;
```

> [!IMPORTANT]
> **Why `verified = FALSE` on auto-seeded rows matters:** Auto-seeded rows copy the agent's own historical answers so Sections 5 and 6A run on your first click as a pipeline smoke test, and Cell `5.1` / Cell `6A` print an explicit `[UNVERIFIED AUTO-SEEDED TEMPLATE]` notice whenever `verified = FALSE` rows are present. Before using Section 6A as a ground-truth accuracy KPI, review or insert SME-verified answers with `verified = TRUE`.

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

> **Schema Tip:** Why does `bqca_golden_qa` include both `(golden_question, golden_answer)` and `(question, expected_answer)`? Because `client.drift_detection()` reads `question` (or `golden_question`), while custom SQL graders reference `golden_question` / `golden_answer` or `expected_answer`. Defining both aliases (and supporting existing 6-column tables automatically) makes your table work seamlessly with every SDK and SQL helper.

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

## 5. Self-Debugging & Troubleshooting Runbook (10 Common Issues)

Use this runbook whenever a query, judge, or metric behaves unexpectedly on your logs.

---

### Issue #1: `LLMAsJudge` (`sentiment`, `correctness`, `hallucination`) returns `NULL`, `0.0`, or empty answers

- **Symptom:** Running `client.evaluate(LLMAsJudge.sentiment())` or `LLMAsJudge.correctness()` directly against your raw `agent_events` table returns `0.0` or `NaN` scores, or `final_response` is `NULL` in the judge query.
- **Root Cause:** In `bigquery-agent-analytics` v0.5.4, `LLMAsJudge` extracts the agent's final answer using `JSON_VALUE(content, '$.response')` (which only returns scalar JSON strings). On raw BQCA `AGENT_RESPONSE` rows, `content.response` is a nested JSON object (`{"parts": [{"markdown": "..."}], "clarifying_question": "..."}`), so `JSON_VALUE(content, '$.response')` returns SQL `NULL`.
- **Copy-Paste Fix:** Always initialize `Client(..., table_id=COMPAT_VIEW_ID)` using `v_bqca_customer_sdk_events` created in Cell `1.3` (`Cell [7]`), which flattens `$.response` and `$.text_summary` to scalar strings while preserving the original nested object in `$.raw_response`:

```python
# Always point the SDK Client at v_bqca_customer_sdk_events (NOT raw agent_events):
client = Client(
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
  2. Use the default `CATEGORICAL_MODEL = "gemini-3.5-flash"` (or `"gemini-2.5-flash-lite"`), and check `report.details.get("execution_mode")` to confirm whether the evaluation ran via `"ai_generate"` / `"ai_classify"` or fell back to `"gemini_fallback"`:

```python
CATEGORICAL_MODEL = "gemini-3.5-flash"  # or "gemini-2.5-flash-lite"

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
- **Root Cause:** Stock `PerformanceEvaluator` in `bigquery-agent-analytics` v0.5.4 reads `SessionTrace.final_response` from the last `LLM_RESPONSE` event and does not include `AGENT_RESPONSE` in `_DEFAULT_EVENT_TYPES`. On BQCA logs, `LLM_RESPONSE.content` is `NULL` by design and the user-visible answer is stored in `AGENT_RESPONSE`.
- **Copy-Paste Fix:** Use the drop-in `BQCAPerformanceEvaluator` subclass from Cell `6B` (`Cell [23]`):

```python
class BQCAPerformanceEvaluator(PerformanceEvaluator):
    """Adapter for BQCA logs: populates SessionTrace.final_response from AGENT_RESPONSE."""

    async def get_session_trace(self, session_id, **kwargs):
        session_trace = await super().get_session_trace(session_id, **kwargs)
        for event in reversed(session_trace.events):
            if event.event_type == "AGENT_RESPONSE" and isinstance(event.content, dict):
                session_trace.final_response = (
                    event.content.get("text_summary")
                    or event.content.get("response")
                    or session_trace.final_response
                )
                break
        return session_trace


bqca_evaluator = BQCAPerformanceEvaluator(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=COMPAT_VIEW_ID,
    client=bq,
    llm_judge_model=DIRECT_JUDGE_MODEL,
    include_event_types=list(PerformanceEvaluator._DEFAULT_EVENT_TYPES) + ["AGENT_RESPONSE"],
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

- **Symptom:** `client.doctor()["warnings"]` notes `No events for types: AGENT_COMPLETED, AGENT_STARTING, LLM_REQUEST, TOOL_COMPLETED, TOOL_STARTING`, and `trace.tool_calls` is `[]`.
- **Root Cause:** By design, BQCA Prompt & Response Logging emits the 9 standard prompt, response, lifecycle, and error event types (`BQCA_EVENT_TYPES`). Low-level ADK agent/tool spans (`AGENT_STARTING`, `AGENT_COMPLETED`, `LLM_REQUEST`, `TOOL_STARTING`, `TOOL_COMPLETED`, `TOOL_ERROR`) are not written to customer `agent_events` tables.
- **Copy-Paste Fix:** No fix is needed for `doctor()` — that warning is expected on BQCA logs. To evaluate answer accuracy without `TOOL_*` events, use **Section 6A (Golden Q&A Answer Key Grading via `AI.EMBED` + `AI.GENERATE`)** and **Section 6B (`LLMAsJudge` + `GraderPipeline` + `BQCAPerformanceEvaluator`)**.

---

### Issue #6: `SystemEvaluator.cost_per_session()` is lower than total token-equivalent consumption when thinking tokens are generated

- **Symptom:** The USD estimate from `SystemEvaluator.cost_per_session()` accounts for prompt and candidate tokens but excludes `thoughts_token_count`.
- **Root Cause:** `SystemEvaluator.cost_per_session()` prices only `prompt_token_count` and `candidates_token_count`. On Gemini reasoning models, `attributes.usage_metadata.thoughts_token_count` (thinking tokens) are also generated (satisfying `prompt_tokens + candidate_tokens + thinking_tokens == total_tokens`; adding **+43%** to the token-equivalent estimate on the 25-session sample dataset: `$0.318` -> `$0.455`).
- **Copy-Paste Fix:** Use the `cost_sdk_usd` and `cost_incl_thinking_usd` columns materialized in `v_bqca_customer_turns` (or the 3-term token-equivalent cost formula in Cell `7.2` / `Cell [26]` at your configured `COST_RATES`):

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

- **Symptom:** `SystemEvaluator.context_cache_hit_rate()` reports a higher cache hit rate (`0.70`) than your actual cached-token share (`0.63`).
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
- **Root Cause:** Either (a) the BigQuery Cloud Resource Connection's service account (`bqcx-...`) is missing `roles/aiplatform.user`, or (b) the user/service account running the notebook is missing `roles/bigquery.connectionUser`.
- **Copy-Paste Fix:**

```bash
# 1. Grant roles/aiplatform.user to the connection's service account:
export CONN_SA=$(bq show --connection --format=json "${PROJECT_ID}.${CONN_LOCATION}.${CONN_ID}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${CONN_SA}" \
  --role="roles/aiplatform.user"

# 2. Grant roles/bigquery.connectionUser to the user running the notebook:
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="user:your-email@your-domain.com" \
  --role="roles/bigquery.connectionUser"
```

---

### Issue #9: `404 Not found: Dataset / Table / Connection was not found in location`

- **Symptom:** `google.api_core.exceptions.NotFound: 404 Not found: Dataset ... was not found in location US` or connection location mismatch.
- **Root Cause:** `LOCATION` in Cell `1.1` (`Cell [4]`) does not match the actual region of your BigQuery dataset or your Cloud Resource Connection. BigQuery requires the dataset and the connection to reside in the same region/multi-region.
- **Copy-Paste Fix:** Check the exact location of your dataset and connection in the terminal, then set `LOCATION` and `VERTEX_CONNECTION_ID` to match:

```bash
bq show --format=prettyjson "${PROJECT_ID}:${DATASET_ID}" | grep '"location"'
bq ls --connection --project_id="${PROJECT_ID}" --location="${LOCATION}"
```

---

### Issue #10: `stderr` warnings (`FutureWarning` on Python 3.10 or `google-genai` API key errors in fallback mode)

- **Symptom:** Running under Python 3.10 prints a `FutureWarning` from `google.api_core`, or `google-genai` raises an authentication error during a fallback retry if `GOOGLE_CLOUD_PROJECT` was previously set to another project in the same Python kernel.
- **Root Cause:** Python 3.10 reaches `google-api-core` end-of-support in October 2026, and `google-genai` requires `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION` set via direct assignment (`os.environ[...] = ...`) so re-running Cell `1.1` after editing `PROJECT_ID` always updates the active Vertex AI client configuration.
- **Copy-Paste Fix:** Cell `1.1` (`Cell [4]`) of the starter notebook configures this automatically while routing `bigquery_agent_analytics` `WARNING` logs to `stdout`:

```python
import logging, os, sys, warnings

warnings.filterwarnings("ignore")
for _noisy in ("google_genai", "google_genai.models", "google.genai", "absl", "urllib3", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)
_bqaa_logger = logging.getLogger("bigquery_agent_analytics")
_bqaa_logger.handlers = [logging.StreamHandler(sys.stdout)]
_bqaa_logger.setLevel(logging.WARNING)
_bqaa_logger.propagate = False

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = VERTEX_LOCATION
```

---

## 6. Copy-Paste Diagnostic SQL & Python One-Liners (5 Live Checks)

Replace `your-gcp-project-id.your_bqca_logs_dataset` (and the connection ID in Check #2 and Check #5) with your own `PROJECT_ID.DATASET_ID` to self-diagnose your environment in seconds.

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
  COUNTIF(event_type = 'AGENT_RESPONSE' AND JSON_QUERY(content, '$.raw_response') IS NOT NULL) AS raw_response_preserved
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_sdk_events`;
```

**Example output from the 25-session sample dataset (your row counts will match your own dataset; `scalar_response_ok` and `text_summary_ok` should equal `agent_response_rows`):**
`raw_rows = 266 | sdk_view_rows = 137 | distinct_sessions = 25 | agent_response_rows = 23 | scalar_response_ok = 23 | text_summary_ok = 23 | raw_response_preserved = 23`

---

### Check #2: Ping Your Vertex AI Cloud Resource Connection (`AI.GENERATE` & `AI.EMBED`)

Tests IAM permissions and Vertex AI endpoint reachability (`gemini-3.5-flash` and `text-embedding-005`) on a single row before running full-table evaluations:

```sql
SELECT
  (AI.GENERATE(
    prompt => 'Reply with OK if Vertex AI connection is healthy.',
    connection_id => 'projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection',
    endpoint => 'gemini-3.5-flash',
    model_params => JSON '{"generationConfig": {"temperature": 0.0, "maxOutputTokens": 64}}',
    output_schema => 'verdict STRING'
  )).verdict AS ai_generate_status,
  ARRAY_LENGTH(
    AI.EMBED(
      'Health check embedding test',
      connection_id => 'projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection',
      endpoint => 'text-embedding-005'
    ).result
  ) AS embedding_dimensions;
```

**Expected Healthy Output:**
`ai_generate_status = 'OK' | embedding_dimensions = 768`

---

### Check #3: Audit Token Accounting Identity & Thinking-Token Share (`v_bqca_customer_turns`)

Verifies `prompt_tokens + candidate_tokens + thinking_tokens = total_tokens` across all LLM-calling sessions and computes your token-weighted prompt cache hit rate:

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

**Example output from the 25-session sample dataset (`token_identity_ok` should equal `llm_sessions`):**
`total_sessions = 25 | llm_sessions = 20 | fast_path_sessions = 3 | token_identity_ok = 20 | sum_prompt_tokens = 104620 | sum_candidate_tokens = 18731 | sum_thinking_tokens = 13685 | sum_cached_tokens = 65536 | token_weighted_cache_hit_rate = 0.626`

---

### Check #4: Instant Error & Non-`STOP` Finish Reason Triage

Surfaces every error row (`INVOCATION_ERROR`, `AGENT_ERROR`, `LLM_ERROR`) and every `LLM_RESPONSE` cut off by `MAX_TOKENS` or `SAFETY`:

```sql
SELECT
  session_id,
  user_id,
  event_type,
  status,
  COALESCE(JSON_VALUE(attributes, '$.finish_reason'), '-') AS finish_reason,
  error_message
FROM `your-gcp-project-id.your_bqca_logs_dataset.v_bqca_customer_sdk_events`
WHERE (status = 'ERROR' OR error_message IS NOT NULL OR ENDS_WITH(event_type, '_ERROR'))
   OR (event_type = 'LLM_RESPONSE' AND COALESCE(JSON_VALUE(attributes, '$.finish_reason'), 'STOP') != 'STOP')
ORDER BY session_id, timestamp;
```

**Expected Healthy Output:**
Returns `0` rows when all sessions completed cleanly with `finish_reason = 'STOP'`, or lists exact `session_id`, `finish_reason` (`MAX_TOKENS`, `SAFETY`), and `error_message` values for triage.

---

### Check #5: 1-Cell Python Smoke Test (`client.doctor()` + `v_bqca_customer_turns`)

Paste this into any Python notebook cell to verify your SDK installation, ADC credentials, and both customer views in under 5 seconds:

```python
import bigquery_agent_analytics as bqaa

PROJECT_ID = "your-gcp-project-id"
DATASET_ID = "your_bqca_logs_dataset"
LOCATION = "US"
VERTEX_CONNECTION_ID = "projects/your-gcp-project-id/locations/us/connections/bqca_vertex_connection"

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

**Example output from the 25-session sample dataset:**
`Schema: ok (17 cols) | SDK view events: 137 | Turns view rows: 25`

---

## 7. Production Scheduling, Looker Studio BI & Data Governance

### 7.1 Connecting `v_bqca_customer_turns` to Looker Studio or BI Dashboards
Because Cell `1.3` creates `v_bqca_customer_turns` as a standard BigQuery view with 1 row per session (including materialized `cost_sdk_usd` and `cost_incl_thinking_usd` columns), you can connect **Looker Studio**, **Looker**, or **Connected Sheets** directly to `PROJECT_ID.DATASET_ID.v_bqca_customer_turns` to chart:
- Daily sessions, active users (`user_id`), and user cohorts (`persona`, `persona_role`, `scenario_tag`, `data_agent_id`) over `started_at`.
- Answer delivery rate (`agent_response IS NOT NULL`), clarifying-question rate (`clarifying_question IS NOT NULL`), and error rate (`has_error = TRUE`).
- End-to-end latency P50/P90 (`e2e_latency_ms`) and Verified Queries Fast-Path share (`is_fast_path = TRUE`).
- Token consumption (`prompt_tokens`, `candidate_tokens`, `thinking_tokens`, `cached_tokens`, `total_tokens`) and estimated token cost (`cost_sdk_usd`, `cost_incl_thinking_usd`).

For the full 37-chart observability template over `v_bqca_customer_sdk_events`, see the [Looker Studio Dashboard User Manual](../../dashboard/looker_studio/USER_MANUAL.md).

### 7.2 Recommended Evaluation Cadence & Cost Control
- **Real-time / Hourly (Zero LLM Cost)**: Query `v_bqca_customer_turns`, `SystemEvaluator` SLO checks (Section 7), and `bqaa.ERROR_SQL_PREDICATE` + `finish_reason != 'STOP'` (Section 8). These are pure BigQuery SQL queries with zero Vertex AI model invocations.
- **Daily Batch (`AI.CLASSIFY` & `AI.EMBED`)**: Run Question Topic Taxonomy in fast bulk mode (`include_justification=False`, Section 4) and Semantic Drift (`AI.EMBED` + `ML.DISTANCE`, Section 5) on new sessions from the past 24 hours (`TraceFilter(start_time=...)`).
- **Weekly or Pre-Release Audit (`AI.GENERATE` Judges & `TrialRunner`)**: Run `LLMAsJudge` sentiment/correctness/hallucination and Golden Q&A Answer Key Grading (Sections 3 and 6) over a weekly sample or after updating agent instructions and Verified Queries.

### 7.3 Data Governance, PII & Access Control

> [!CAUTION]
> **Privacy, PII & Vertex AI Model Evaluations:**
> - Raw user prompts (`user_prompt` / `content`) and agent answers (`agent_response`) logged in `agent_events` may contain sensitive business questions or user-supplied PII.
> - Keep `v_bqca_customer_sdk_events`, `v_bqca_customer_turns`, and `bqca_golden_qa` in the same BigQuery dataset (`DATASET_ID`) as `agent_events` so dataset-level IAM policies, row/column-level security, and VPC Service Controls perimeter rules apply uniformly.
> - When Cell `5.1` auto-bootstraps `bqca_golden_qa`, it copies up to 50 historical `user_prompt` and `agent_response` values into `DATASET_ID.bqca_golden_qa`, and Sections 3–6 send `user_prompt` and `agent_response` text to Vertex AI (`AI.GENERATE`, `AI.CLASSIFY`, `AI.EMBED`, and `google-genai`) inside your GCP project (`PROJECT_ID`). Restrict `agent_events`, `v_bqca_customer_sdk_events`, `v_bqca_customer_turns`, and `bqca_golden_qa` to authorized data stewards, and grant broader BI viewers access only to aggregated metrics views.

---

## 8. FAQ & Quick-Reference Cheat Sheet

### 8.1 Frequently Asked Questions (FAQ)

- **Q1: Can I run the notebook before creating a curated `bqca_golden_qa` table?**
  **Yes.** Sections 1, 2, 3, 4, 6B, 7, and 8 require only `agent_events`, and Cell `5.1` (`Cell [18]`) automatically runs `CREATE TABLE IF NOT EXISTS` to bootstrap an unverified starter `bqca_golden_qa` template (`verified = FALSE`) from your clean production turns so Sections 5 and 6A also run immediately. Replace or update those rows with SME-verified answers (`SET verified = TRUE`) before treating Section 6A as a ground-truth accuracy KPI.
- **Q2: Why does `v_bqca_customer_turns` show `0` tokens (`llm_calls = 0`) on some answered sessions?**
  Those sessions were answered by the **Verified Queries Fast Path** (`attributes.fast_path = true` / `is_fast_path = TRUE`), which matches a verified query template without invoking an LLM generation call.
- **Q3: Why should I point `Client` at `v_bqca_customer_sdk_events` instead of raw `agent_events`?**
  Because raw BQCA `AGENT_RESPONSE` events store `content.response` as a nested JSON object (`{"parts": [{"markdown": "..."}]}`), whereas `LLMAsJudge` in `bigquery-agent-analytics` v0.5.4 reads scalar `JSON_VALUE(content, '$.response')`. `v_bqca_customer_sdk_events` flattens `$.response` and `$.text_summary` to scalar strings while preserving the original object in `$.raw_response`.

### 8.2 Quick-Reference Cheat Sheet

| Goal | SDK / SQL Surface to Use | Target Table / View |
| :--- | :--- | :--- |
| **Verify schema & connection health** | `client.doctor()` + 1-row `AI.GENERATE` ping (`gemini-3.5-flash`) | `v_bqca_customer_sdk_events` |
| **Explore prompt/response pairs & latency** | `SELECT * FROM v_bqca_customer_turns` | `v_bqca_customer_turns` |
| **Find frustrated or confused users** | `client.evaluate(LLMAsJudge.sentiment())` + `client.evaluate_categorical()` | `v_bqca_customer_sdk_events` |
| **Classify user questions by business topic** | `client.evaluate_categorical()` (`include_justification=False` for `AI.CLASSIFY`, `True` for `AI.GENERATE`) | `v_bqca_customer_sdk_events` |
| **Detect new / drifted user questions** | `client.drift_detection()` + `AI.EMBED` / `ML.DISTANCE` | `v_bqca_customer_turns` + `bqca_golden_qa` |
| **Grade answer accuracy against ground truth** | Section 6A `AI.EMBED` + `AI.GENERATE` Golden Q&A SQL Grader | `v_bqca_customer_turns` + `bqca_golden_qa` |
| **Run multi-trial judge consistency (`pass@3`) & composite grading** | `BQCAPerformanceEvaluator` + `TrialRunner` + `GraderPipeline` | `v_bqca_customer_sdk_events` |
| **Audit token spend (incl. thinking tokens) & Fast-Path ROI** | `SystemEvaluator` + Section 7 3-term token cost query (`cost_incl_thinking_usd`) | `v_bqca_customer_sdk_events` & `v_bqca_customer_turns` |
| **Monitor errors, `MAX_TOKENS`/`SAFETY` cut-offs & refusals** | `bqaa.ERROR_SQL_PREDICATE` + `finish_reason != 'STOP'` + `delivery_outcome` | `v_bqca_customer_sdk_events` |
