# 🤖 Agent Analytics & Orchestration Dashboard

An interactive Streamlit dashboard for monitoring and diagnosing agent execution logs stored in **Google BigQuery**. This tool provides deep visibility into LLM performance, token consumption, and orchestration reliability, following the **ADK (Agent Development Kit)** standard.

For the published, no-hosting-required Looker Studio template over
BQAA-generated views, see
[`dashboard/looker_studio`](looker_studio/README.md).

---

## 🚀 Getting Started

### Option 1: Google Colab (Cloud)
Ideal for a zero-install setup.
1. Open the provided `agent_analytics_dashboard.ipynb` in Google Colab.
2. Follow the internal instructions to authenticate your GCP account.
3. Run the launch cell to generate a secure **Cloudflare Tunnel** URL.


### Option 2: Local Environment
1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Set up Authentication**:
   Ensure you have [Application Default Credentials (ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc) configured on your machine.

3. **Run the app script**:
    ```bash
   streamlit run app.py
   ```

### ⚠️ Important Notes
Before running the dashboard, please verify the following:

* **Standard ADK Schema:** The BigQuery table **must** follow the standard ADK BigQuery Schema, [ADK BigQuery Schema Reference](https://adk.dev/observability/bigquery-agent-analytics/#schema-reference) .
If your table uses custom column names or a different JSON structure for metadata, the queries in `app.py` may need manual adjustment.

* **Replace Placeholders:** In the dashboard sidebar (or the `app.py` script), ensure you replace the placeholder values for:
  * GCP Project ID
  * Dataset ID
  * Table ID

* **Data Permissions:** Ensure the authenticated user has **BigQuery Data Viewer** and **BigQuery Job User** permissions for the selected project.
