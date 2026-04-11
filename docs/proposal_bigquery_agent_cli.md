# Proposal: `bqx` — An Agent-Native BigQuery CLI with Skills

**Status:** Proposal
**Date:** 2026-03-08
**Related:** [gws CLI](https://github.com/googleworkspace/cli),
[Agent Skills](https://agentskills.io),
[BigQuery Conversational Analytics](https://cloud.google.com/bigquery/docs/conversational-analytics),
[BigQuery Agent Analytics SDK](https://github.com/haiyuan-eng-google/BigQuery-Agent-Analytics-SDK)

---

## 1. Problem Statement

BigQuery is the most common data platform for AI agent analytics, but its
CLI tooling (`bq`) was designed in 2012 for human operators. It is:

- **Not extensible** — monolithic Python binary, no plugin or skill system
- **Not agent-friendly** — inconsistent output formats, no structured JSON
  default, verbose help text that wastes context tokens
- **Not AI-aware** — no integration with Conversational Analytics, AI
  functions, or agent evaluation workflows

Meanwhile, AI agents are becoming the primary consumers of CLI tools.
Community benchmarks show CLIs achieve **35x token efficiency** over MCP
schemas and **28% higher task completion** for identical tasks. But agents
need CLIs designed for them: structured output, progressive disclosure,
and discoverable skills.

The Google Workspace CLI (`gws`) has proven this model works — 100+ skills,
dynamic command generation, JSON-first output, and adoption across Claude
Code, Gemini CLI, Cursor, and others. BigQuery needs the same.

---

## 2. Proposal: `bqx` (BigQuery Extended)

A new agent-native CLI for BigQuery that combines:

1. **Dynamic command generation** from BigQuery APIs (like `gws`)
2. **Agent Skills** for discoverability (SKILL.md format)
3. **Conversational Analytics** integration (natural language queries)
4. **BigQuery Agent Analytics SDK** capabilities (evaluation, traces, drift)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         bqx CLI                                     │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │  BigQuery     │  │  Agent       │  │  Conversational│              │
│  │  API          │  │  Analytics   │  │  Analytics    │              │
│  │  (dynamic)    │  │  SDK         │  │  API          │              │
│  │              │  │              │  │              │              │
│  │  query, mk,  │  │  evaluate,   │  │  ask,         │              │
│  │  ls, load,   │  │  get-trace,  │  │  create-agent,│              │
│  │  show, rm    │  │  drift,      │  │  list-agents  │              │
│  │              │  │  insights    │  │              │              │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         │                 │                 │                       │
│  ┌──────┴─────────────────┴─────────────────┴───────┐              │
│  │              Shared Core                          │              │
│  │  Auth · JSON output · Model Armor · Pagination    │              │
│  └───────────────────────────────────────────────────┘              │
│                                                                     │
│  ┌──────────────────────────────────────────────────┐              │
│  │              Skills (SKILL.md)                     │              │
│  │  89 skills: service · helper · persona · recipe   │              │
│  └──────────────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────────────┘
```

### Why `bqx`, not extending `bq`

| Factor | `bq` (existing) | `bqx` (proposed) |
|--------|-----------------|-------------------|
| Language | Python | Rust (fast startup, single binary) |
| Extensibility | None | Skills + dynamic command generation |
| Output format | Mixed text/JSON | JSON-first (+ table, yaml, csv) |
| Agent consumption | Not designed for agents | Progressive disclosure, SKILL.md |
| Release cycle | Coupled to gcloud SDK | Independent releases |
| AI integration | None | Conversational Analytics, AI functions, Agent Analytics |
| Discovery | Static commands | Dynamic from BigQuery REST API |

---

## 3. Architecture

### 3.1 Dynamic Command Generation (from `gws` pattern)

Like `gws`, `bqx` uses two-phase argument parsing:

1. `argv[1]` identifies the service module (`analytics`, `ca`, or falls
   through to BigQuery API resource names)
2. For BigQuery API commands, fetch the
   [BigQuery Discovery Document](https://www.googleapis.com/discovery/v1/apis/bigquery/v2/rest),
   cache it (24h TTL), and build a `clap::Command` tree dynamically

```bash
# Dynamic commands (generated from BigQuery REST API Discovery Document)
bqx datasets list --project-id=myproject
bqx tables get --project-id=myproject --dataset-id=analytics --table-id=agent_events
bqx jobs query --query="SELECT 1" --use-legacy-sql=false

# Static commands (Agent Analytics SDK — compiled in)
bqx analytics evaluate --evaluator=latency --threshold=5000 --last=1h
bqx analytics get-trace --session-id=sess-001
bqx analytics drift --golden-dataset=golden_qs

# Static commands (Conversational Analytics API)
bqx ca ask "What were the top errors yesterday?" --agent=my-data-agent
bqx ca create-agent --name=agent-analytics --tables=agent_events
```

### 3.2 Three Command Domains

#### Domain 1: `bqx <resource> <method>` — BigQuery API (dynamic)

Generated from the BigQuery v2 Discovery Document, covering datasets,
tables, jobs, routines, connections, models, and row-access policies.

```bash
# List datasets
bqx datasets list --project-id=myproject

# Run a query (structured output)
bqx jobs query \
  --query="SELECT session_id, agent FROM analytics.agent_events LIMIT 5" \
  --use-legacy-sql=false

# Create a view
bqx tables insert \
  --project-id=myproject \
  --dataset-id=analytics \
  --json='{"tableReference":{"tableId":"v_errors"},"view":{"query":"SELECT ..."}}'

# Show table schema
bqx tables get --project-id=myproject --dataset-id=analytics --table-id=agent_events
```

#### Domain 2: `bqx analytics <command>` — Agent Analytics (static)

Wraps the BigQuery Agent Analytics SDK. Commands are compiled into the
binary (not dynamically generated) since they don't come from a Discovery
Document.

```bash
# Evaluate agent performance
bqx analytics evaluate \
  --evaluator=latency \
  --threshold=5000 \
  --agent-id=support_bot \
  --last=1h

# Retrieve a session trace
bqx analytics get-trace --session-id=sess-001

# Health check
bqx analytics doctor

# Drift detection
bqx analytics drift \
  --golden-dataset=golden_questions \
  --agent-id=support_bot \
  --last=7d

# LLM-as-judge evaluation
bqx analytics evaluate \
  --evaluator=llm-judge \
  --criterion=correctness \
  --threshold=0.7 \
  --last=24h \
  --exit-code

# Create event-type views
bqx analytics views create-all --prefix=adk_

# Generate insights report
bqx analytics insights --agent-id=support_bot --last=24h
```

#### Domain 3: `bqx ca <command>` — Conversational Analytics (static)

Wraps the BigQuery Conversational Analytics API, bringing natural language
queries to the terminal.

```bash
# Ask a natural language question
bqx ca ask "Show me the top 5 agents by error rate this week" \
  --agent=agent-analytics-data-agent

# Ask with a specific table context
bqx ca ask "What's the p95 latency trend for support_bot?" \
  --tables=myproject.analytics.agent_events

# Create a data agent with verified queries
bqx ca create-agent \
  --name=agent-analytics \
  --tables=myproject.analytics.agent_events,myproject.analytics.adk_llm_responses \
  --verified-queries=./deploy/ca/verified_queries.yaml \
  --instructions="This agent helps analyze AI agent performance metrics."

# List data agents
bqx ca list-agents --project-id=myproject

# Add a verified query to an existing agent
bqx ca add-verified-query \
  --agent=agent-analytics \
  --question="What is the error rate for agent X?" \
  --query="SELECT COUNT(CASE WHEN status='ERROR' THEN 1 END) / COUNT(*) FROM ..."
```

### 3.3 Output Format

All output is JSON by default, with alternative formats via `--format`:

```bash
# Default: structured JSON (agent-consumable)
bqx analytics evaluate --evaluator=latency --threshold=5000 --last=1h
{
  "evaluator": "latency",
  "threshold_ms": 5000,
  "total_sessions": 10,
  "passed": 7,
  "failed": 3,
  "pass_rate": 0.70,
  "aggregate_scores": {
    "avg_latency_ms": 3200,
    "p95_latency_ms": 6100
  }
}

# Table format (human-readable)
bqx analytics evaluate --evaluator=latency --threshold=5000 --last=1h --format=table
SESSION_ID   PASSED  LATENCY_MS  SCORE
sess-001     true    2340        0.85
sess-002     false   7800        0.32
sess-003     true    1850        0.91

# Dry-run mode (shows what would happen)
bqx jobs query --query="SELECT 1" --dry-run
{
  "dry_run": true,
  "url": "https://bigquery.googleapis.com/bigquery/v2/projects/myproject/queries",
  "method": "POST",
  "body": {"query": "SELECT 1", "useLegacySql": false},
  "estimated_bytes_processed": 0
}
```

### 3.4 Authentication

Five methods, same priority model as `gws`:

| Priority | Method | Use Case |
|----------|--------|----------|
| 1 (highest) | `BQX_TOKEN` env var | Pre-obtained access token |
| 2 | `BQX_CREDENTIALS_FILE` env var | Service account JSON path |
| 3 | `bqx auth login` (encrypted) | Interactive OAuth, AES-256-GCM at rest |
| 4 | `GOOGLE_APPLICATION_CREDENTIALS` | Standard ADC fallback |
| 5 | `gcloud auth application-default` | Implicit gcloud credentials |

```bash
# Quick start (uses existing gcloud credentials)
bqx datasets list --project-id=myproject

# Explicit login with scope selection
bqx auth login -s bigquery,cloud-platform

# Service account (CI/CD)
export BQX_CREDENTIALS_FILE=/path/to/sa-key.json
bqx analytics evaluate --evaluator=latency --last=24h --exit-code
```

### 3.5 Security

- **Model Armor integration:** `--sanitize <template>` screens API responses
  for prompt injection. `BQX_SANITIZE_TEMPLATE` env var for global default.
- **Credential encryption:** AES-256-GCM at rest, key in OS keyring.
- **Destructive operation guards:** Write/delete commands require `--confirm`
  flag or interactive confirmation. Skill generator blocks destructive
  methods by default.
- **Least-privilege defaults:** `bqx auth login` requests only BigQuery
  scopes, not broad cloud-platform.

---

## 4. Skills Architecture

### 4.1 Overview

Skills follow the [Agent Skills](https://agentskills.io) open standard:
declarative `SKILL.md` files that any compatible agent (Claude Code, Gemini
CLI, Cursor, Copilot, Codex) can discover and use.

```
skills/
├── bqx-shared/SKILL.md                       # Auth, global flags, security rules
│
├── bqx-datasets/SKILL.md                     # Service: dataset operations
├── bqx-tables/SKILL.md                       # Service: table operations
├── bqx-jobs/SKILL.md                         # Service: query execution
├── bqx-routines/SKILL.md                     # Service: UDFs, remote functions
├── bqx-models/SKILL.md                       # Service: ML models
├── bqx-connections/SKILL.md                  # Service: external connections
│
├── bqx-analytics/SKILL.md                    # Service: Agent Analytics SDK
├── bqx-analytics-evaluate/SKILL.md           # Helper: run evaluations
├── bqx-analytics-trace/SKILL.md              # Helper: retrieve traces
├── bqx-analytics-drift/SKILL.md              # Helper: drift detection
├── bqx-analytics-views/SKILL.md              # Helper: manage event views
│
├── bqx-ca/SKILL.md                           # Service: Conversational Analytics
├── bqx-ca-ask/SKILL.md                       # Helper: ask questions in NL
├── bqx-ca-create-agent/SKILL.md              # Helper: create data agents
│
├── bqx-query/SKILL.md                        # Helper: shortcut for bqx jobs query
├── bqx-schema/SKILL.md                       # Helper: inspect table schemas
│
├── persona-agent-developer/SKILL.md          # Persona: agent developer workflows
├── persona-data-analyst/SKILL.md             # Persona: SQL analyst workflows
├── persona-sre/SKILL.md                      # Persona: SRE/on-call workflows
│
├── recipe-eval-pipeline/SKILL.md             # Recipe: CI/CD eval gate setup
├── recipe-quality-dashboard/SKILL.md         # Recipe: Looker dashboard via remote fn
├── recipe-error-alerting/SKILL.md            # Recipe: CQ + AI.GENERATE_TEXT alerting
├── recipe-drift-monitoring/SKILL.md          # Recipe: weekly drift detection
├── recipe-self-diagnostic-agent/SKILL.md     # Recipe: agent self-correction loop
└── recipe-ca-data-agent-setup/SKILL.md       # Recipe: CA data agent creation
```

### 4.2 Example Skills

#### Service Skill: `bqx-analytics/SKILL.md`

```markdown
---
name: bqx-analytics
version: 1.0.0
description: "BigQuery Agent Analytics: Evaluate, trace, and monitor AI agent sessions."
metadata:
  category: "analytics"
  requires:
    bins: ["bqx"]
  cliHelp: "bqx analytics --help"
---

# analytics

> **PREREQUISITE:** Read `../bqx-shared/SKILL.md` for auth, global flags,
> and security rules.

```bash
bqx analytics <command> [flags]
```

## Commands

| Command | Description |
|---------|-------------|
| `doctor` | Run diagnostic health check on BigQuery table and configuration |
| `evaluate` | Run code-based or LLM evaluation over agent session traces |
| `get-trace` | Retrieve and display a single session trace |
| `list-traces` | List recent traces matching filter criteria |
| `insights` | Generate comprehensive agent insights report |
| `drift` | Run drift detection against a golden question set |
| `distribution` | Analyze question distribution patterns |
| `hitl-metrics` | Show human-in-the-loop interaction metrics |
| `views` | Create per-event-type BigQuery views (18 event types) |

## Helper Skills

For common tasks, use the shortcut helper skills:

| Helper | Description |
|--------|-------------|
| [`bqx-analytics-evaluate`](../bqx-analytics-evaluate/SKILL.md) | Quick evaluation commands |
| [`bqx-analytics-trace`](../bqx-analytics-trace/SKILL.md) | Trace retrieval and analysis |
| [`bqx-analytics-drift`](../bqx-analytics-drift/SKILL.md) | Drift detection workflows |
| [`bqx-analytics-views`](../bqx-analytics-views/SKILL.md) | Manage per-event-type views |

## Global Flags

| Flag | Description |
|------|-------------|
| `--project-id TEXT` | GCP project ID [env: `BQX_PROJECT`] |
| `--dataset-id TEXT` | BigQuery dataset [env: `BQX_DATASET`] |
| `--last TEXT` | Time window: `1h`, `24h`, `7d`, `30d` |
| `--agent-id TEXT` | Filter by agent name |
| `--format TEXT` | Output: `json` (default), `table`, `text` |
| `--exit-code` | Return exit code 1 on evaluation failure |
```

#### Helper Skill: `bqx-analytics-evaluate/SKILL.md`

```markdown
---
name: bqx-analytics-evaluate
version: 1.0.0
description: "Evaluate AI agent sessions for latency, error rate, or correctness."
metadata:
  category: "analytics"
  requires:
    bins: ["bqx"]
  cliHelp: "bqx analytics evaluate --help"
---

# analytics evaluate

> **PREREQUISITE:** Read `../bqx-shared/SKILL.md` for auth and global flags.

Evaluate agent sessions against a threshold. Returns pass/fail per session.

## Usage

```bash
bqx analytics evaluate --evaluator=<TYPE> --threshold=<N> [flags]
```

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--evaluator` | Yes | — | `latency`, `error_rate`, `turn_count`, `token_efficiency`, `llm-judge` |
| `--threshold` | Yes | — | Pass/fail threshold (ms for latency, 0-1 for rates/scores) |
| `--criterion` | If llm-judge | — | `correctness`, `hallucination`, `sentiment`, `custom` |
| `--custom-prompt` | If custom | — | Custom LLM judge prompt |
| `--exit-code` | No | false | Return exit code 1 on failure (for CI/CD) |

## Examples

```bash
# Check latency compliance (agent self-diagnostic)
bqx analytics evaluate --evaluator=latency --threshold=5000 --agent-id=support_bot --last=1h

# CI/CD gate: fail if correctness drops below 0.7
bqx analytics evaluate --evaluator=llm-judge --criterion=correctness \
  --threshold=0.7 --last=24h --exit-code

# Custom evaluation
bqx analytics evaluate --evaluator=llm-judge --criterion=custom \
  --custom-prompt="Rate how well the agent handled PII. Score 0-1." \
  --threshold=0.9 --last=24h
```

> [!NOTE]
> This is a **read-only** command. Safe to run without confirmation.
```

#### Persona Skill: `persona-sre/SKILL.md`

```markdown
---
name: persona-sre
version: 1.0.0
description: "On-call SRE workflows for monitoring and triaging AI agent issues."
metadata:
  category: "persona"
  requires:
    bins: ["bqx"]
    skills: ["bqx-analytics", "bqx-ca", "bqx-query"]
---

# SRE / On-Call Engineer

> **PREREQUISITE:** Load the following skills: `bqx-analytics`, `bqx-ca`,
> `bqx-query`

Monitor AI agent health, triage incidents, and validate fixes.

## Incident Triage Workflow

1. Check overall health:
   `bqx analytics doctor`
2. Look for error spikes:
   `bqx analytics evaluate --evaluator=error_rate --threshold=0.05 --last=1h`
3. Identify failing sessions:
   `bqx analytics evaluate --evaluator=latency --threshold=5000 --last=1h --format=table`
4. Inspect a specific failure:
   `bqx analytics get-trace --session-id=<ID_FROM_STEP_3>`
5. Ask follow-up in natural language:
   `bqx ca ask "What tools failed most in the last hour?" --agent=agent-analytics`

## Daily Health Check

```bash
bqx analytics doctor && \
bqx analytics evaluate --evaluator=error_rate --threshold=0.05 --last=24h && \
bqx analytics evaluate --evaluator=latency --threshold=5000 --last=24h
```

## Tips

- Use `--format=table` for quick visual scans during incidents.
- Pipe `--format=json` output to `jq` for scripted analysis.
- Set `BQX_PROJECT` and `BQX_DATASET` env vars to avoid repetitive flags.
```

#### Recipe Skill: `recipe-eval-pipeline/SKILL.md`

```markdown
---
name: recipe-eval-pipeline
version: 1.0.0
description: "Set up a CI/CD evaluation pipeline that gates agent deployment on quality metrics."
metadata:
  category: "recipe"
  domain: "devops"
  requires:
    bins: ["bqx"]
    skills: ["bqx-analytics"]
---

# CI/CD Evaluation Pipeline

> **PREREQUISITE:** Load `bqx-analytics` skill.

Set up a GitHub Actions workflow that blocks deployment when agent quality
drops below thresholds.

## Steps

1. Install `bqx` in CI:
   `npm install -g @bigquery/bqx`

2. Authenticate with Workload Identity Federation:
   ```yaml
   - uses: google-github-actions/auth@v2
     with:
       workload_identity_provider: ${{ vars.WIF_PROVIDER }}
       service_account: ${{ vars.SA_EMAIL }}
   ```

3. Add evaluation gates:
   ```bash
   bqx analytics evaluate --evaluator=latency --threshold=5000 --last=24h --exit-code
   bqx analytics evaluate --evaluator=error_rate --threshold=0.05 --last=24h --exit-code
   bqx analytics drift --golden-dataset=golden_qs --min-coverage=0.85 --exit-code
   ```

4. Upload reports as artifacts:
   ```bash
   bqx analytics insights --last=24h > insights.json
   ```

> [!CAUTION]
> Ensure the CI service account has `bigquery.dataViewer` and
> `bigquery.jobUser` roles only. Never grant `dataEditor` to CI.
```

### 4.3 Skill Generation

Like `gws generate-skills`, `bqx` auto-generates skills from the BigQuery
Discovery Document:

```bash
# Generate all skills from BigQuery API + Agent Analytics commands
bqx generate-skills --output-dir=./skills

# Regenerate only analytics skills
bqx generate-skills --filter=bqx-analytics --output-dir=./skills
```

The generator:
- Fetches the BigQuery v2 Discovery Document
- Creates one `SKILL.md` per API resource (datasets, tables, jobs, etc.)
- Creates helper skills for common operations
- Blocks destructive methods (delete, drop) in skill descriptions
- Includes flag tables, usage examples, and cross-references

### 4.4 Skill Distribution

```bash
# npm (all skills)
npx skills add https://github.com/bigquery/bqx

# Individual skill
npx skills add https://github.com/bigquery/bqx/tree/main/skills/bqx-analytics

# OpenClaw
ln -s $(pwd)/skills/bqx-* ~/.openclaw/skills/

# Gemini CLI
gemini extensions install https://github.com/bigquery/bqx

# Claude Code (auto-discover from project)
# Just clone the repo — Claude Code reads SKILL.md files automatically
```

---

## 5. Conversational Analytics Integration

### 5.1 Why This Matters

BigQuery Conversational Analytics lets users ask natural language questions
over their data. Today it's only accessible in the BigQuery Cloud Console.
`bqx ca` brings it to the terminal and to agents.

### 5.2 Data Agent for Agent Analytics

The SDK ships a pre-built data agent configuration with verified queries
tuned for agent analytics:

```bash
# Create the agent-analytics data agent (one-time setup)
bqx ca create-agent \
  --name=agent-analytics \
  --tables=myproject.analytics.agent_events \
  --views=myproject.analytics.adk_llm_responses,myproject.analytics.adk_tool_completions \
  --verified-queries=./deploy/ca/verified_queries.yaml \
  --instructions="You help analyze AI agent performance. The agent_events
    table stores traces from ADK agents. Key event types: LLM_REQUEST,
    LLM_RESPONSE, TOOL_STARTING, TOOL_COMPLETED, TOOL_ERROR.
    Error detection: event_type ends with _ERROR OR error_message IS NOT NULL
    OR status = 'ERROR'."
```

#### Verified Queries (shipped with SDK)

```yaml
# deploy/ca/verified_queries.yaml
verified_queries:
  - question: "What is the error rate for {agent}?"
    query: |
      SELECT
        COUNT(CASE WHEN ENDS_WITH(event_type, '_ERROR')
                     OR error_message IS NOT NULL
                     OR status = 'ERROR' THEN 1 END) AS errors,
        COUNT(DISTINCT session_id) AS sessions,
        SAFE_DIVIDE(
          COUNT(CASE WHEN ENDS_WITH(event_type, '_ERROR')
                       OR error_message IS NOT NULL
                       OR status = 'ERROR' THEN 1 END),
          COUNT(DISTINCT session_id)
        ) AS error_rate
      FROM `{project}.{dataset}.agent_events`
      WHERE agent = @agent
        AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)

  - question: "What is the p95 latency for {agent}?"
    query: |
      SELECT
        APPROX_QUANTILES(
          CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64), 100
        )[OFFSET(95)] AS p95_latency_ms
      FROM `{project}.{dataset}.agent_events`
      WHERE agent = @agent
        AND event_type = 'LLM_RESPONSE'
        AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)

  - question: "Which tools fail most often?"
    query: |
      SELECT
        JSON_VALUE(content, '$.tool') AS tool_name,
        COUNT(*) AS error_count
      FROM `{project}.{dataset}.agent_events`
      WHERE event_type = 'TOOL_ERROR'
        AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
      GROUP BY tool_name
      ORDER BY error_count DESC
      LIMIT 10

  - question: "Show me the sessions with highest latency"
    query: |
      SELECT
        session_id,
        agent,
        MAX(CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64)) AS max_latency_ms,
        COUNT(*) AS event_count,
        MIN(timestamp) AS started_at
      FROM `{project}.{dataset}.agent_events`
      WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
      GROUP BY session_id, agent
      ORDER BY max_latency_ms DESC
      LIMIT 10
```

### 5.3 Usage

```bash
# Natural language query via terminal
$ bqx ca ask "What were the top errors for support_bot yesterday?" \
    --agent=agent-analytics

{
  "question": "What were the top errors for support_bot yesterday?",
  "sql": "SELECT JSON_VALUE(content, '$.tool') AS tool, error_message, COUNT(*) ...",
  "results": [
    {"tool": "database_query", "error_message": "Connection timeout", "count": 15},
    {"tool": "search_api", "error_message": "Rate limit exceeded", "count": 8}
  ],
  "explanation": "The most common errors for support_bot in the last 24 hours were..."
}

# Compose with analytics commands
$ bqx ca ask "Which agent had the worst performance today?" --agent=agent-analytics \
  | jq -r '.results[0].agent' \
  | xargs -I{} bqx analytics evaluate --agent-id={} --evaluator=latency --threshold=5000 --last=24h
```

---

## 6. How the Three Domains Compose

The power of `bqx` is that its three domains — BigQuery API, Agent
Analytics, and Conversational Analytics — compose through Unix pipes and
agent reasoning:

### Scenario: Agent Investigates Its Own Performance

```
Agent thinks: "User asked a complex question. Let me check if I've been
              performing well recently before I commit to an expensive
              tool call."

Step 1: Quick health check
  $ bqx analytics evaluate --evaluator=latency --threshold=5000 --last=1h
  → pass_rate: 0.70 (borderline)

Step 2: Natural language drill-down
  $ bqx ca ask "What's causing high latency in the last hour?" --agent=agent-analytics
  → "The database_query tool has p95 latency of 12s due to 3 timeout events"

Step 3: Check specific trace
  $ bqx analytics get-trace --session-id=sess-042
  → Shows TOOL_ERROR: "Connection timeout after 30s"

Agent decides: Switch to cached data source for this query.
```

### Scenario: SRE Triages an Alert

```bash
# 1. What's the overall health?
bqx analytics doctor

# 2. Which agents are failing?
bqx ca ask "Which agents have error rate above 5% in the last hour?"

# 3. Deep dive into the worst one
bqx analytics evaluate --agent-id=support_bot --evaluator=error_rate --last=1h --format=table

# 4. Get the specific traces
bqx analytics get-trace --session-id=sess-042 --format=tree

# 5. Run raw SQL for custom analysis
bqx jobs query --query="
  SELECT event_type, COUNT(*)
  FROM analytics.agent_events
  WHERE session_id = 'sess-042'
  GROUP BY event_type"
```

---

## 7. Implementation Roadmap

### Phase 1: Core CLI + Analytics (v0.1) — 4 weeks

- [ ] Rust CLI scaffold with `clap` (auth, global flags, `--format`)
- [ ] `bqx analytics` commands: `doctor`, `evaluate`, `get-trace`
- [ ] `--exit-code` for CI/CD
- [ ] JSON/table/text output formatting
- [ ] Auth: ADC + service account + `bqx auth login`
- [ ] npm distribution (`npx bqx`)
- [ ] 5 core skills: `bqx-shared`, `bqx-analytics`, `bqx-analytics-evaluate`,
  `bqx-analytics-trace`, `bqx-query`

**Exit criteria:** `npx bqx analytics evaluate --last=1h --exit-code` works
in GitHub Actions; 5 skills installable via `npx skills add`.

### Phase 2: Dynamic BigQuery API + Skills (v0.2) — 4 weeks

- [ ] Discovery Document fetching + caching
- [ ] Dynamic `clap::Command` tree generation for BigQuery v2 API
- [ ] `bqx generate-skills` command
- [ ] Full skill set: 6 service, 6 helper, 3 persona, 5 recipe skills
- [ ] Model Armor integration (`--sanitize`)
- [ ] Gemini CLI extension registration

**Exit criteria:** `bqx datasets list` works without any hardcoded command
definition; `bqx generate-skills` produces valid SKILL.md files;
`gemini extensions install` succeeds.

### Phase 3: Conversational Analytics + Polish (v0.3) — 3 weeks

- [ ] `bqx ca ask` — natural language query via CA API
- [ ] `bqx ca create-agent` — create data agents
- [ ] `bqx ca add-verified-query` — add verified queries
- [ ] Ship `deploy/ca/verified_queries.yaml` with SDK
- [ ] Remaining analytics commands: `insights`, `drift`, `distribution`,
  `views`, `hitl-metrics`, `list-traces`
- [ ] Completion scripts (bash, zsh, fish)
- [ ] Documentation and examples

**Exit criteria:** `bqx ca ask "error rate for support_bot?"` returns
structured JSON with SQL and results; all analytics commands pass
integration tests.

---

## 8. Relationship to Existing Tools

| Tool | Role | Relationship to `bqx` |
|------|------|----------------------|
| `bq` CLI | Legacy BigQuery CLI | `bqx` is a successor, not a wrapper. Coexists — users can migrate gradually. |
| `gcloud` | Google Cloud CLI | `bqx` handles BigQuery-specific workflows; delegates to `gcloud` for IAM, projects. |
| `gws` CLI | Google Workspace CLI | Architectural template. Same skills format, same output patterns, different domain. |
| `bq-agent-sdk` (from PRD) | Python CLI from current PRD | `bqx analytics` subsumes this. The Python SDK remains as a library; the CLI moves to Rust. |
| BigQuery Console | Web UI | `bqx ca ask` brings CA to terminal; `bqx analytics` brings SDK to terminal. |

---

## 9. Open Questions

1. **Language choice:** Rust (like `gws`) vs Go (like `gcloud`/`bq`)?
   **Recommendation:** Rust — faster startup, smaller binary, proven by `gws`.

2. **Naming:** `bqx` vs `bqai` vs `bq2`?
   **Recommendation:** `bqx` — short, clearly extends `bq`, no conflict.

3. **BigQuery API coverage scope:** Full Discovery Document or curated subset?
   **Recommendation:** Start with curated (datasets, tables, jobs, routines,
   models, connections); add more resources via `generate-skills` as needed.

4. **CA API availability:** The Conversational Analytics API is in preview.
   **Mitigation:** Phase 3 depends on API stability; Phase 1-2 deliver
   value independently.

5. **Relationship to `bq-agent-sdk` CLI in current PRD:**
   **Recommendation:** The current PRD's Python CLI (§4) becomes a
   stepping-stone. v1.0 ships as `bq-agent-sdk` (Python/typer); once `bqx`
   reaches v0.2, analytics commands migrate to `bqx analytics` and the
   Python CLI is deprecated.
