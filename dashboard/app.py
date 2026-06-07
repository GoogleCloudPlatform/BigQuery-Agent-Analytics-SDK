import streamlit as st
import pandas as pd
from google.cloud import bigquery
import plotly.express as px
import re

# --- 1. Page Configuration ---
st.set_page_config(page_title="Agent Analytics V2", layout="wide")
st.title("🤖 Agent Analytics & Orchestration Dashboard")

# --- 2. Shared Query Fragment (Resolved PR Issues #1 & #2) ---
# NOTE: table_ref is injected via string replacement because BigQuery does not
# support parameterized table names. Security is enforced via strict regex
# validation of the table identifiers in the sidebar.

ENRICHED_EVENTS_CTE = """
    SELECT
        *,
        CASE
            WHEN event_type = 'USER_MESSAGE_RECEIVED' THEN 'user'
            WHEN event_type IN ('INVOCATION_STARTING','INVOCATION_COMPLETED') THEN 'invocation'
            WHEN event_type IN ('AGENT_STARTING','AGENT_COMPLETED') THEN 'agent'
            WHEN event_type IN ('LLM_REQUEST','LLM_RESPONSE','LLM_ERROR') THEN 'llm'
            WHEN event_type IN ('TOOL_STARTING','TOOL_COMPLETED','TOOL_ERROR') THEN 'tool'
            WHEN event_type = 'STATE_DELTA' THEN 'state'
            WHEN event_type LIKE 'HITL_%' THEN 'hitl'
            ELSE 'other'
        END AS event_family,
        (status = 'ERROR' OR error_message IS NOT NULL OR event_type LIKE '%_ERROR') AS is_canonical_error
    FROM {table_ref}
    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @time_hours HOUR)
    AND (@agent_name IS NULL OR agent = @agent_name)
"""

# --- 3. Sidebar Configuration ---
with st.sidebar:
    st.header("Data Source")
    project_id = st.text_input("GCP Project ID", "REPLACE WITH YOUR PROJECT ID")
    dataset_id = st.text_input("Dataset ID", "REPLACE WITH YOUR DATASET ID")
    table_id = st.text_input("Table ID", "REPLACE WITH YOUR TABLE ID")

    # Identifier Validation
    for val in [project_id, dataset_id, table_id]:
        if not re.match(r'^[\w.-]+$', val):
            st.error(f"Invalid identifier: {val}")
            st.stop()

    table_ref = f"`{project_id}.{dataset_id}.{table_id}`"

    st.divider()
    st.header("Global Filters")
    time_hours = st.slider("Time Range (Hours)", 1, 720, 72)
    agent_name = st.text_input("Filter by Agent Name", "")

# --- 4. Data Connector ---
@st.cache_resource
def get_bq_client(p_id):
    return bigquery.Client(project=p_id)

client = get_bq_client(project_id)

def safe_query(sql, params=None):
    try:
        job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
        formatted_sql = sql.replace("{table_ref}", table_ref)
        return client.query(formatted_sql, job_config=job_config).to_dataframe()
    except Exception as e:
        st.error(f"Query Failed: {e}")
        return pd.DataFrame()

# --- 5. Global Params (Resolved 400 Parameter Error) ---

global_params = [
    bigquery.ScalarQueryParameter("time_hours", "INT64", time_hours),
    bigquery.ScalarQueryParameter("agent_name", "STRING", agent_name if agent_name else None)
]

# WHERE clause for non-CTE queries (Executive Summary & Multimodal)
where_clause = "timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @time_hours HOUR)"
if agent_name:
    where_clause += " AND agent = @agent_name"

# --- 6. Executive Summary (Resolved PR Consistency Issue) ---
st.header("📊 Executive Summary")

kpi_data = safe_query(f"""
    WITH enriched AS ({ENRICHED_EVENTS_CTE})
    SELECT
        COUNT(DISTINCT session_id) as sessions,
        COUNT(DISTINCT trace_id) as traces,
        COUNTIF(event_type = 'LLM_REQUEST') as llm_calls,
        SAFE_DIVIDE(COUNTIF(is_canonical_error = TRUE), COUNT(*)) as error_rate
    FROM enriched
""", params=global_params)

if not kpi_data.empty:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sessions", kpi_data['sessions'][0])
    c2.metric("Traces", kpi_data['traces'][0])
    c3.metric("LLM Calls", kpi_data['llm_calls'][0])
    # The rate now includes TOOL_ERROR and LLM_ERROR automatically
    c4.metric("Error Rate", f"{kpi_data['error_rate'][0]*100:.2f}%")
    

# --- 7. Detailed Tabs ---
tabs = st.tabs(["📈 Usage", "⚡ Performance", "🛡️ Reliability", "🖼️ Multimodal", "🤝 HITL", "🔍 Trace Explorer"])

# TAB 0: USAGE & VOLUME
with tabs[0]:
    st.subheader("Token Consumption")
    token_trend = safe_query(f"""
        SELECT TIMESTAMP_TRUNC(timestamp, HOUR) as hour,
               SUM(CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64)) as prompt,
               SUM(CAST(JSON_VALUE(content, '$.usage.completion') AS INT64)) as completion
        FROM {{table_ref}} WHERE event_type = 'LLM_RESPONSE' AND {where_clause}
        GROUP BY 1 ORDER BY 1
    """, params=global_params)
    if not token_trend.empty:
        fig = px.area(token_trend, x="hour", y=["prompt", "completion"], title="Tokens over Time")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No token usage data found.")

# TAB 1: PERFORMANCE
with tabs[1]:
    st.subheader("Latency Metrics")
    lat_data = safe_query(f"""
        WITH enriched AS ({ENRICHED_EVENTS_CTE})
        SELECT
            APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64), 100)[OFFSET(50)] as p50,
            APPROX_QUANTILES(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64), 100)[OFFSET(90)] as p90,
            AVG(CAST(JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS FLOAT64)) as avg_ttft
        FROM enriched WHERE event_family = 'llm'
    """, params=global_params)
    if not lat_data.empty and pd.notnull(lat_data['p50'][0]):
        p1, p2, p3 = st.columns(3)
        p1.metric("p50 Latency", f"{lat_data['p50'][0]:.0f}ms")
        p2.metric("p90 Latency", f"{lat_data['p90'][0]:.0f}ms")
        p3.metric("Avg TTFT", f"{lat_data['avg_ttft'][0]:.0f}ms")
    else:
        st.warning("No performance data found.")

# TAB 2: RELIABILITY
with tabs[2]:
    st.subheader("Error Distribution")
    err_data = safe_query(f"""
        WITH enriched AS ({ENRICHED_EVENTS_CTE})
        SELECT event_family, event_type, COUNT(*) as errors
        FROM enriched WHERE is_canonical_error = TRUE
        GROUP BY 1, 2 ORDER BY errors DESC
    """, params=global_params)
    if not err_data.empty:
        fig = px.bar(err_data, x="event_type", y="errors", color="event_family")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.success("No errors detected.")

# TAB 3: MULTIMODAL
with tabs[3]:
    st.subheader("Media Parts")
    multi_data = safe_query(f"""
        SELECT cp.mime_type, COUNT(*) as count
        FROM {{table_ref}}, UNNEST(content_parts) as cp
        WHERE {where_clause} GROUP BY 1
    """, params=global_params)
    if not multi_data.empty:
        fig = px.pie(multi_data, values='count', names='mime_type')
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No multimodal content found.")

# TAB 4: HITL
with tabs[4]:
    st.subheader("Human-in-the-Loop Activity")
    hitl_data = safe_query(f"""
        WITH enriched AS ({ENRICHED_EVENTS_CTE})
        SELECT event_type, COUNT(*) as count
        FROM enriched WHERE event_family = 'hitl'
        GROUP BY 1
    """, params=global_params)
    if not hitl_data.empty:
        st.table(hitl_data)
    else:
        st.info("No HITL events found.")

# TAB 5: TRACE EXPLORER
with tabs[5]:
    st.subheader("Session Explorer")
    sessions = safe_query(f"""
        SELECT session_id, agent, MIN(timestamp) as ts,
        FORMAT_TIMESTAMP('%m-%d %H:%M', MIN(timestamp)) as f_ts
        FROM {{table_ref}} WHERE {where_clause} GROUP BY 1, 2 ORDER BY ts DESC LIMIT 50
    """, params=global_params)

    if not sessions.empty:
        sessions['label'] = sessions['f_ts'] + " | " + sessions['agent'].fillna("?") + " | " + sessions['session_id'].str[:8]
        label_map = dict(zip(sessions['label'], sessions['session_id']))
        sel_label = st.selectbox("Select Session", sessions['label'])
        sel_id = label_map[sel_label]

        trace_detail = safe_query("""
            SELECT timestamp, event_type, agent, status, error_message
            FROM {table_ref} WHERE session_id = @sid ORDER BY timestamp
        """, params=[bigquery.ScalarQueryParameter("sid", "STRING", sel_id)])
        st.dataframe(trace_detail, use_container_width=True)
