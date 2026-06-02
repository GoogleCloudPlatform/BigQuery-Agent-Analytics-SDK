# Trace AI Agent Decisions with BigQuery Property Graphs

**Byline**:


*BigQuery property graphs, BigQuery Conversational Analytics, and the BigQuery Agent Analytics SDK are currently in Preview on Google Cloud. The BigQuery Agent Analytics Plugin is Generally Available (GA). Examples in this post use synthetic data.*

As autonomous AI agents take on more operational responsibilities—such as evaluating loan applications, managing marketing budgets, or approving access requests—organizations must be able to audit and explain their decisions. Understanding the exact context, alternatives considered, and final rationale of an agent's decision is critical for compliance, risk management, and operational trust.

While capturing raw agent event logs is a straightforward first step, querying those logs to reconstruct a complex decision path can be difficult and time-consuming. Traditionally, analyzing these relationships required exporting log data into external, specialized graph databases via complex ETL pipelines.

To simplify this process, we are introducing scheduled, native graph materialization in BigQuery. Using the new `bqaa context-graph` command in the BigQuery Agent Analytics SDK, you can continuously convert agent event logs into an active [BigQuery property graph](https://cloud.google.com/bigquery/docs/reference/standard-sql/graph-query-language)—without setting up a separate database or leaving your BigQuery environment.

---

## Why use BigQuery for agent decision traces?

Three core architectural advantages distinguish this approach from traditional external graph database setups:

* **BigQuery-Native Architecture**: There is no new database infrastructure to provision, manage, or pay for. The raw events, materialized property graph, identity and access management (IAM), billing, and analytical queries all remain natively within BigQuery.  
* **Governed by Design**: The architecture supports a strong security posture. The event log dataset remains read-only to the materialization engine, while the graph dataset serves as the write target. The execution service account uses least-privilege access, and every run is logged to a state table, providing a complete audit trail of the materialization history.  
* **Deterministic and AI-Driven Extraction**: You can choose how data is extracted from your events. By default, the system can use LLM-based extraction (`AI.GENERATE`) for flexible onboarding against variable log structures. For regulated workloads requiring strict determinism, you can use a compiled extraction mode to run custom, auditor-verifiable Python parser modules without any external AI dependencies.

---

## How scheduled graph materialization works

Transforming flat agent logs into a queryable property graph relies on four key operational phases:

### 1\. Ingesting Agent Events

First, the Generally Available BigQuery Agent Analytics Plugin captures agent activities—such as tool calls, LLM requests, and human approvals—the moment your agent runs. It stream-writes these events into a structured, sixteen-column `agent_events` table in BigQuery using the BigQuery Storage Write API.

```py
from google.adk.plugins import BigQueryAgentAnalyticsPlugin

plugin = BigQueryAgentAnalyticsPlugin(
    project_id="your-project-id",
    dataset_id="agent_analytics",
)
runner = Runner(agent=root_agent, plugins=[plugin])
```

### 2\. Defining the Property Graph

Next, you define the property graph schema directly in BigQuery. This schema represents your domain-specific decision model: the agent's context, decision points, alternatives evaluated, and selected outcomes. You define this model once using standard SQL Data Definition Language (DDL).

```sql
CREATE OR REPLACE PROPERTY GRAPH graph.agent_decisions_graph
NODE TABLES (
  graph.DecisionExecution
    KEY (decision_execution_id) LABEL DecisionExecution,
  graph.DecisionPoint
    KEY (decision_point_id) LABEL DecisionPoint,
  graph.Candidate
    KEY (candidate_id) LABEL Candidate,
  graph.SelectionOutcome
    KEY (selection_outcome_id) LABEL SelectionOutcome
)
EDGE TABLES (
  graph.ExecutedAt
    SOURCE KEY (decision_execution_id) REFERENCES DecisionExecution (decision_execution_id)
    DESTINATION KEY (decision_point_id) REFERENCES DecisionPoint (decision_point_id)
    LABEL executedAtDecisionPoint,
  graph.EvaluatesCandidate
    SOURCE KEY (decision_point_id) REFERENCES DecisionPoint (decision_point_id)
    DESTINATION KEY (candidate_id) REFERENCES Candidate (candidate_id)
    LABEL evaluatesCandidate,
  graph.HasSelectionOutcome
    SOURCE KEY (decision_execution_id) REFERENCES DecisionExecution (decision_execution_id)
    DESTINATION KEY (selection_outcome_id) REFERENCES SelectionOutcome (selection_outcome_id)
    LABEL hasSelectionOutcome,
  graph.SelectedCandidate
    SOURCE KEY (selection_outcome_id) REFERENCES SelectionOutcome (selection_outcome_id)
    DESTINATION KEY (candidate_id) REFERENCES Candidate (candidate_id)
    LABEL selectedCandidate
);
```

### 3\. Executing the Materializer

To keep the graph updated, you run the `bqaa context-graph` CLI command on a schedule (e.g., every few hours). The SDK provides a deployment script to set this up as a Cloud Run Job triggered by Cloud Scheduler.

During each run, the materializer:

* Identifies completed agent sessions from the raw event dataset within the specified time window.  
* Extracts node and edge entities based on your schema.  
* Populates the corresponding graph tables in your read/write dataset.  
* Updates a persistent execution state table to ensure exactly-once processing.

```shell
./deploy_cloud_run_job.sh \
    --project your-project-id \
    --region us-central1 \
    --events-dataset agent_analytics \
    --graph-dataset graph \
    --schedule "0 */6 * * *"
```

---

## Developer Walkthrough: Querying a Decision Trace

Once the materialization job runs, your property graph is ready for analysis. Consider a scenario where a compliance team needs to audit why an underwriting agent declined a specific loan application.

Using standard Graph Query Language (GQL) syntax in BigQuery, you can traverse the decision graph to pull the decision, the options evaluated, the final choice, and the system's recorded rationale.

```sql
SELECT *
FROM GRAPH_TABLE (
  graph.agent_decisions_graph
  MATCH (de:DecisionExecution) -[:executedAtDecisionPoint]-> (dp:DecisionPoint),
        (dp) -[:evaluatesCandidate]-> (option:Candidate),
        (de) -[:hasSelectionOutcome]-> (so:SelectionOutcome),
        (so) -[:selectedCandidate]-> (chosen:Candidate)
  WHERE de.business_entity_id = 'customer-4029-7'
  COLUMNS (
    de.decision_execution_id AS decision_id,
    dp.decision_point_id      AS decision_point,
    option.candidate_id       AS option_evaluated,
    chosen.candidate_id       AS chosen_option,
    so.rationale              AS rationale
  )
);
```

The query returns a flat, queryable representation of the decision graph, detailing every candidate option considered alongside the selected outcome and the structured reasoning:

| decision\_id | decision\_point | option\_evaluated | chosen\_option | rationale |
| :---- | :---- | :---- | :---- | :---- |
| de-9c2e | dp-mortgage-approval | cand-decline | **cand-decline** | *"DTI ratio of 42% exceeds the 40% maximum threshold; recent late payments fall inside the 90-day risk window."* |
| de-9c2e | dp-mortgage-approval | cand-refer-to-human | **cand-decline** | *"DTI ratio of 42% exceeds the 40% maximum threshold; recent late payments fall inside the 90-day risk window."* |
| de-9c2e | dp-mortgage-approval | cand-approve | **cand-decline** | *"DTI ratio of 42% exceeds the 40% maximum threshold; recent late payments fall inside the 90-day risk window."* |

By joining these relational and graph operations natively within BigQuery, audit teams can quickly pinpoint the exact factors that influenced any specific agent action.

BigQuery Studio can also render the property graph visually. A `GRAPH … MATCH p = (a)-[e]->(b) RETURN TO_JSON(p)` query over the materialized graph draws the full decision web — here, 527 nodes and 438 edges across the seeded corpus:

![The materialized decision graph (527 nodes, 438 edges) visualized in BigQuery Studio from a GQL query](./images/graph-visualization.png)

---

## Production-Grade Capabilities

The latest 0.3.2 release of the BigQuery Agent Analytics SDK includes several features designed to support enterprise-grade deployments:

* **Least-Privilege Split Service Accounts**: The deployment scripts separate execution privileges. One service account runs the materializer job with restricted BigQuery and Vertex AI permissions, while a separate, highly restricted service account is used by Cloud Scheduler solely to trigger the job.  
* **Deterministic Parsing Options**: For highly regulated industries, you can pass the `--extraction-mode=compiled-only` flag. This disables LLM calls entirely and uses predefined Python parsing logic to guarantee deterministic, reproducible graph builds.  
* **Outage Resiliency and Backfills**: If network disruptions occur, the materializer handles late-arriving events through configurable lookback windows. You can also run the tool in `--backfill` mode to reprocess historical data without affecting your active schedule's progress markers.  
* **Infrastructure-as-Code Integration**: To align with enterprise deployment standards, the SDK includes a complete [Terraform module](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization/terraform) to provision all necessary GCP resources with consistent IAM configurations.

---

## Get Started

To begin building scheduled decision graphs for your agent workloads, check out the resources below:

* **Code Repository**: Visit the [BigQuery Agent Analytics SDK on GitHub](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK).  
* **Setup Guide**: Review the [Periodic Materialization Guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/migration_v5/periodic_materialization) to configure IAM, jobs, and schedulers.  
* **Hands-on Codelab**: Follow the step-by-step *Periodic Materialization for BigQuery Agent Analytics* codelab to deploy a local test environment from scratch.
* **Ask in plain English**: The [Conversational Analytics-first guide](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/docs/guides/conversational-analytics-first.md) shows business readers how to query the decision graph without writing SQL, then drop to GQL for exact lineage.

![Conversational Analytics summarizing the materialized decision graph in plain English: every recorded decision request reached a committed outcome (orphaned sessions are not materialized as graph nodes)](./images/ca-conversation.png)
