<!--
Copyright 2026 Google LLC
Licensed under the Apache License, Version 2.0.

Decision Lineage with BigQuery Context Graphs — leadership deck.

Render with Marp (https://marp.app):
  marp SLIDES.md --pdf            # SLIDES.pdf
  marp SLIDES.md --pptx           # SLIDES.pptx
  marp SLIDES.md --html           # SLIDES.html
  marp SLIDES.md --watch --html   # live preview
-->
---
marp: true
title: Decision Lineage with BigQuery Context Graphs
description: A leadership-pitched deck that mirrors examples/decision_lineage_demo/.
author: BigQuery Agent Analytics SDK
theme: gaia
size: 16:9
paginate: true
backgroundColor: "#ffffff"
color: "#202124"
style: |
  /* Google-ish typography + accent palette */
  section {
    font-family: "Google Sans", "Roboto", -apple-system, "Segoe UI", sans-serif;
    font-size: 28px;
    line-height: 1.35;
    padding: 56px 64px;
  }
  section.lead {
    background: linear-gradient(135deg, #1a73e8 0%, #174ea6 100%);
    color: #ffffff;
    text-align: left;
  }
  section.lead h1 {
    font-size: 60px;
    line-height: 1.1;
    margin-bottom: 16px;
    color: #ffffff;
  }
  section.lead h2 {
    font-size: 26px;
    font-weight: 400;
    color: rgba(255,255,255,0.85);
    border: none;
  }
  section.lead .footer-note {
    font-size: 18px;
    color: rgba(255,255,255,0.7);
    margin-top: 48px;
  }
  section h1 {
    color: #1a73e8;
    border-bottom: 2px solid #e8eaed;
    padding-bottom: 8px;
    font-size: 38px;
  }
  section h2 {
    color: #202124;
    font-size: 26px;
    margin-top: 0;
  }
  section h3 {
    color: #1a73e8;
    font-size: 22px;
    margin-bottom: 4px;
  }
  section.section-divider {
    background: #f8f9fa;
  }
  section.section-divider h1 {
    color: #1a73e8;
    border: none;
    font-size: 56px;
  }
  section.section-divider .eyebrow {
    color: #5f6368;
    text-transform: uppercase;
    letter-spacing: 4px;
    font-size: 16px;
    margin-bottom: 16px;
  }
  code {
    background: #f1f3f4;
    color: #202124;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.9em;
  }
  pre {
    background: #202124;
    color: #e8eaed;
    border-radius: 8px;
    padding: 18px 22px;
    font-size: 18px;
    line-height: 1.4;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12);
  }
  pre code { background: transparent; color: inherit; padding: 0; }
  table {
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 22px;
  }
  th {
    background: #1a73e8;
    color: #ffffff;
    padding: 10px 14px;
    text-align: left;
  }
  td {
    border-bottom: 1px solid #e8eaed;
    padding: 8px 14px;
  }
  blockquote {
    border-left: 4px solid #1a73e8;
    background: #e8f0fe;
    color: #174ea6;
    padding: 14px 22px;
    margin: 12px 0;
    font-style: normal;
    font-size: 24px;
  }
  .pill {
    display: inline-block;
    background: #e8f0fe;
    color: #1a73e8;
    border-radius: 999px;
    padding: 4px 14px;
    font-size: 18px;
    margin-right: 6px;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 32px;
    align-items: start;
  }
  .stat {
    font-size: 64px;
    color: #1a73e8;
    font-weight: 700;
    line-height: 1;
  }
  .stat-label {
    color: #5f6368;
    font-size: 18px;
    text-transform: uppercase;
    letter-spacing: 2px;
  }
  footer {
    color: #5f6368;
    font-size: 14px;
  }
footer: "Decision Lineage with BigQuery Context Graphs · examples/decision_lineage_demo"
---

<!-- _class: lead -->
<!-- _paginate: false -->

<span class="pill" style="background:rgba(255,255,255,0.15);color:#fff">EU AI Act · GDPR · DSA</span>

# Decision Lineage with BigQuery Context Graphs

## Unboxing the AI agent — every decision, every alternative, every reason, queryable in BigQuery.

<div class="footer-note">
BigQuery Agent Analytics SDK · examples/decision_lineage_demo · Issue #98
</div>

<!--
SPEAKER NOTE — 30s
Open with the EU AI Act framing. The penalty headline does work for VPs:
up to 7% of global revenue. Then pivot: today's demo isn't a product
pitch, it's a working artifact in BigQuery.
-->

---

# Why this matters now

<div class="grid-2">

<div>

### The regulatory pressure
- **EU AI Act** obligations are phasing in (high-risk-system rules from **2 Aug 2026**, full rollout **2 Aug 2027**)
- **GDPR Art. 22** — automated decisions affecting users must be explainable
- **DSA Art. 26** — advertising transparency

</div>

<div>

### The product pressure
- Most agent demos produce an answer and ask you to **trust** it
- Compliance reviewers can't audit a screenshot
- Engineering can't reproduce a decision from chat logs

</div>

</div>

> **Penalty ceiling: up to 7% of global revenue.** That's not a paperwork problem — that's a finance question.

<!--
Source: https://ai-act-service-desk.ec.europa.eu/en/ai-act/eu-ai-act-implementation-timeline
Don't claim the act has "taken full effect" — it hasn't. Use phasing-in language.
-->

---

# What we built

<div class="grid-2">

<div>

### One sentence
Take a real ADK agent, attach the **BigQuery Agent Analytics Plugin**, let `AI.GENERATE` extract every decision and every alternative the agent considered, and serve it as a **BigQuery Property Graph** you query with **GQL** in BigQuery Studio.

### One promise
Every node and every edge in the graph traces back to a real plugin span. **No hand-baked seed data anywhere.**

</div>

<div>

```text
┌──────────────────────┐
│  Live ADK agent      │
│  (Gemini 2.5 Pro)    │
│  + BQ AA Plugin      │
└─────────┬────────────┘
          │ spans
          ▼
┌──────────────────────┐
│  agent_events        │
│  (BigQuery)          │
└─────────┬────────────┘
          │ AI.GENERATE x2
          ▼
┌──────────────────────┐
│  Property Graph      │
│  agent_context_graph │
│  + rich layer        │
└─────────┬────────────┘
          │ GQL
          ▼
   BigQuery Studio
```

</div>

</div>

---

<!-- _class: section-divider -->

<div class="eyebrow">Section 1 of 4</div>

# The data behind today's demo

---

# Six campaigns, six live agent runs

| Brand | Campaign | Budget | Audience |
|---|---|---:|---|
| Nike | Summer Run 2026 | $360K | Serious runners 18-35 |
| Nike | Winter Trail 2026 | $500K | Trail-runners & hikers 25-45 |
| Adidas | Track Season 2026 | $420K | NCAA & HS sprinters 16-22 |
| Puma | Soccer Cup 2026 | $280K | Soccer fans 18-30 |
| Reebok | CrossFit Open 2026 | $340K | Fitness pros 25-40 |
| Lululemon | Yoga Flow 2026 | $250K | Yoga practitioners 22-45 |

Each row is one ADK invocation against `gemini-2.5-pro`. Each invocation produces **27 plugin-recorded spans**.

<!--
Talk track: real diversity — different brand, audience, budget, season.
The talk track stays count-agnostic; the live numbers below are illustrative.
-->

---

# Three writers populate seven tables

```text
┌──────────────────────────────────────────────────────────────────────┐
│  WHO writes        WHEN          WHICH TABLE                         │
├──────────────────────────────────────────────────────────────────────┤
│  BQ AA Plugin     run_agent.py   agent_events                        │
│  (live runner)                                                        │
│                                                                      │
│  SDK AI.GENERATE  build_graph.py extracted_biz_nodes                 │
│  (MERGE)                                                              │
│                                                                      │
│  SDK AI.GENERATE  build_graph.py decision_points + candidates        │
│  (load job; idem-                                                    │
│   potent on session)                                                 │
│                                                                      │
│  SDK SQL DML      build_graph.py context_cross_links                 │
│                                  made_decision_edges                 │
│                                  candidate_edges                     │
└──────────────────────────────────────────────────────────────────────┘
```

`AI.GENERATE` runs **twice** across all sessions — once for business entities, once for decisions and the alternatives the agent considered.

---

# The graph the demo presents

The SDK ships a canonical 4-pillar graph. The demo adds a presentation layer with **ads-domain labels** so the BigQuery Studio Explorer reads in business language:

<div class="grid-2">

<div>

### Nodes (8 labels)
- `CampaignRun` — one per agent invocation
- `AgentStep` — every plugin-recorded span
- `MediaEntity` — audiences, channels, creatives, budgets
- `PlanningDecision` — moments the agent committed
- `DecisionOption` — every alternative weighed
- `DecisionCategory` — audience / budget / creative / channel / schedule
- `OptionOutcome` — selected / dropped
- `DropReason` — distinct rejection rationales

</div>

<div>

### Edges (9 labels)
- `CampaignActivity` — campaign → its spans
- `NextStep` — parent span → child span
- `DecidedAt` — span → decision it produced
- `ConsideredEntity` — span → entity it touched
- `CampaignDecision` — campaign → decision
- `WeighedOption` — decision → option
- `HasOutcome` — option → its outcome
- `RejectedBecause` — option → its reason
- `InCategory` — decision → its category

</div>

</div>

---

<!-- _class: section-divider -->

<div class="eyebrow">Section 2 of 4</div>

# Five regulator-shaped questions, answered live

---

# Q1 — Right to explanation

<div class="pill">EU AI Act Art. 86</div> <div class="pill">GDPR Art. 22</div>

> *"For the Nike Summer Run campaign, what audience did the AI pick, what alternatives did it consider, and why did it reject the others?"*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
      -[:WeighedOption]->(opt:DecisionOption)
WHERE cr.session_id = '<SESSION>'
  AND LOWER(dp.decision_type) LIKE '%audience%'
RETURN DISTINCT opt.status, opt.name, opt.score, opt.rejection_rationale
ORDER BY opt.status DESC, opt.score DESC;
```

**Three rows.** Selected: *Serious Runners 18-35* @ 0.99. Dropped: *Casual Runners 25-45* (lower purchase intent), *Fitness Enthusiasts 18-35* (group too broad). **Every reason in the agent's own words.**

---

# Q2 — Bias / fairness audit

<div class="pill">EU AI Act Art. 10</div> <div class="pill">EU AI Act Art. 71</div>

> *"Across our 2026 ad portfolio, did the AI ever reject a candidate based on age or demographic criteria?"*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status = 'DROPPED'
  AND (LOWER(opt.rejection_rationale) LIKE '%age %'
       OR LOWER(opt.rejection_rationale) LIKE '%demographic%'
       OR LOWER(opt.rejection_rationale) LIKE '%youth%')
RETURN DISTINCT dp.decision_type, opt.name, opt.rejection_rationale;
```

**Multiple matches.** *Youth Track & Field (13-15) — outside specified 16-22 range.* *Affluent Hikers (35-55) — age-range mismatch.* The graph surfaces specific rationales for **human review** — proxy or legitimate? Reviewer judges from data, not trust.

---

# Q3 — Human-oversight trigger

<div class="pill">EU AI Act Art. 14</div>

> *"Did the agent ever commit a decision below 0.7 confidence? Those should have triggered human review."*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status = 'SELECTED' AND opt.score < 0.7
RETURN DISTINCT dp.session_id, dp.decision_type, opt.name, opt.score
ORDER BY opt.score ASC;
```

<div style="text-align:center; margin-top: 24px">
<div class="stat">0</div>
<div class="stat-label">rows returned</div>
</div>

**The empty result is the audit artifact.** *"We ran the human-oversight predicate against the entire portfolio for this period. The trigger never fired."* Tighten the threshold to 0.85 → instant new at-risk list.

---

# Q4 — Decision reproducibility

<div class="pill">EU AI Act Art. 12</div> <div class="pill">EU AI Act Art. 13</div>

> *"Subpoena: produce the full audit trail for the Adidas creative-theme decision."*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (step:AgentStep)-[:DecidedAt]->(dp:PlanningDecision)
      -[:WeighedOption]->(opt:DecisionOption)
WHERE dp.session_id = '<SESSION>'
  AND LOWER(dp.decision_type) LIKE '%creative%'
  AND step.event_type = 'LLM_RESPONSE'
RETURN DISTINCT dp.span_id, opt.status, opt.name, opt.score, opt.rejection_rationale
ORDER BY opt.status DESC, opt.score DESC;
```

**Three rows.** Selected: *"Built for a New Record"* @ 0.97. Two dropped with rationale, both pointing back to the same `evidence_span_id`. That span lives in `agent_events` with full content + timestamp + latency. **Article 12 record-keeping → one query.**

---

# Q5 — Systemic / pattern audit

<div class="pill">EU AI Act Art. 17</div> <div class="pill">EU AI Act Art. 60</div>

> *"Where in our portfolio does the AI reject candidates least decisively?"*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status = 'DROPPED'
RETURN dp.decision_type, COUNT(opt) AS rejections, AVG(opt.score) AS avg_dropped_score
GROUP BY dp.decision_type
ORDER BY rejections DESC LIMIT 5;
```

| decision_type | rejections | avg_dropped_score |
|---|---:|---:|
| Creative Theme Selection | 12 | 0.79 |
| Audience Selection | 12 | **0.66** ← lowest |
| Channel Strategy Selection | 6 | 0.77 |
| Placement Selection | 7 | 0.74 |

**Audience Selection rejects with lowest confidence.** Loop to Q2 with this filter → continuous bias-monitoring **loop**, not a one-off.

---

<!-- _class: section-divider -->

<div class="eyebrow">Section 3 of 4</div>

# What this unlocks

---

# Three takeaways for leadership

<div class="grid-2">

<div>

### 1. Compliance posture as a query

The audit surface for our agent platform is now a **live BigQuery query**, not a slide deck or quarterly review.

### 2. The schema generalizes

Brand-neutral, channel-neutral, decision-type-neutral. Drop **any** agent's traces in and the same five questions answer the same way.

</div>

<div>

### 3. Composes with what we run

- **BQ AA Plugin** — already on our prod path
- **SDK extraction** — one method call: `build_context_graph(use_ai_generate=True, include_decisions=True)`
- **BigQuery** — the warehouse you already pay for

No new platform. No new on-call.

</div>

</div>

---

# Compliance-anchor map

| Question | EU AI Act | GDPR | DSA |
|---|---|---|---|
| **Q1** Right to explanation | Art. 86 | Art. 22 | Art. 26 |
| **Q2** Bias / fairness audit | Art. 10, Art. 71 | Art. 5(1)(d), Art. 22 | Art. 26 |
| **Q3** Human oversight | Art. 14 | Art. 22 | — |
| **Q4** Reproducibility | Art. 12, Art. 13 | Art. 30 | Art. 26 |
| **Q5** Systemic-pattern audit | Art. 17, Art. 60 | Art. 35 (DPIA) | Art. 26 |

Five queries against one graph addresses the operative articles in **three** EU regulations — not as a compliance certification, but as the **audit evidence each article asks for**.

---

# How fast can we ship this?

<div class="grid-2">

<div>

### Setup, on a clean GCP project
```bash
cd examples/decision_lineage_demo
./setup.sh
```

| Phase | Wall time |
|---|---|
| Tooling + APIs + venv | ~30s |
| Live agent (6 sessions) | 3-7 min |
| `AI.GENERATE` extraction | 30-90s |
| Rich-graph projection | ~10s |
| Render BQ Studio queries | <1s |

</div>

<div>

### Cost per setup run
- 6 live `gemini-2.5-pro` invocations
- 2 `AI.GENERATE` extraction queries
- A few hundred BQ rows + one property graph

**Order of cents.** Demo queries against the prebuilt graph are near-free.

### From zero to leadership demo
**Under 10 minutes**, fully reproducible, on any GCP project.

</div>

</div>

---

<!-- _class: section-divider -->

<div class="eyebrow">Section 4 of 4</div>

# Where to go next

---

# Open source — try it on your project

<div class="grid-2">

<div>

### Repository
[`GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)

### The demo bundle
`examples/decision_lineage_demo/`

### What ships in the bundle
- `setup.sh` / `reset.sh` — one-shot bootstrap + tear-down
- `agent/` + `campaigns.py` — real ADK agent + 6 briefs
- `run_agent.py` + `build_graph.py` + `build_rich_graph.py`
- `bq_studio_queries.gql` (rendered) — six GQL blocks
- `property_graph.gql` (rendered) — recreate-from-tables DDL

</div>

<div>

### Documentation
- [`README.md`](README.md) — orientation
- [`SETUP_NEW_PROJECT.md`](SETUP_NEW_PROJECT.md) — clean-project reproduction
- [`DEMO_NARRATION.md`](DEMO_NARRATION.md) — 5-min leadership talk track
- [`BQ_STUDIO_WALKTHROUGH.md`](BQ_STUDIO_WALKTHROUGH.md) — click-by-click in BQ Studio
- [`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md) — the 5 EU questions with verified GQL
- [`DATA_LINEAGE.md`](DATA_LINEAGE.md) — how the 7 tables produce the graph
- [`SLIDES.md`](SLIDES.md) — this deck

### License
Apache 2.0

</div>

</div>

---

# Anticipated questions

| Q | Short answer |
|---|---|
| **How long to deploy?** | Plugin is on our prod path. SDK call is one method. DDL is committed. **Days, not quarters.** |
| **Other agents we haven't instrumented?** | Same plugin, same plug-in point. Schema doesn't care which agent wrote the spans. |
| **Cost?** | Two `AI.GENERATE` calls per build + standard BQ query cost. **Cents per build at our scale.** |
| **What if `AI.GENERATE` misses a decision?** | Build script reports per-session counts. Re-run extraction without re-running the agent. Talk track is count-agnostic by design. |
| **Does this expose PII?** | Stores the **agent's own reasoning text** about candidates. PII handling = `agent_events` retention policy, which we already control. |
| **Who operates this?** | The team that owns the agent platform. Plugin write path + SDK read path both already on-call. |

---

<!-- _class: lead -->
<!-- _paginate: false -->

# Q&A

## Decision Lineage with BigQuery Context Graphs

<div class="footer-note">
Open source · Apache 2.0 · github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
<br/>
examples/decision_lineage_demo · Issue #98
</div>

<!--
Wrap with: "Three things to take with you. (1) Compliance posture as
a query. (2) The schema generalizes. (3) Composes with what we run.
Open for questions."
-->
