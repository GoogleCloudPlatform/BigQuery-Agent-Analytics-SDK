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
  /* Google-style typography, presentation rhythm, and executive-ready components. */
  section {
    font-family: "Google Sans", "Roboto", -apple-system, "Segoe UI", sans-serif;
    font-size: 27px;
    line-height: 1.35;
    padding: 56px 64px;
    letter-spacing: 0;
  }
  section.hero {
    background-color: #174ea6 !important;
    background-image: linear-gradient(135deg, #1a73e8 0%, #174ea6 58%, #202124 100%) !important;
    color: #ffffff;
    text-align: left;
  }
  section.hero h1 {
    font-size: 66px;
    line-height: 1.1;
    margin-bottom: 16px;
    color: #ffffff;
    border: none;
  }
  section.hero h2 {
    font-size: 29px;
    font-weight: 400;
    color: rgba(255,255,255,0.85);
    border: none;
    max-width: 920px;
  }
  section.hero .footer-note {
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
    background:
      linear-gradient(90deg, #f8fafd 0%, #ffffff 72%);
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
  .kicker {
    color: #5f6368;
    text-transform: uppercase;
    letter-spacing: 2px;
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 10px;
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
    font-size: 17px;
    margin-right: 6px;
    font-weight: 600;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 32px;
    align-items: start;
  }
  .grid-3 {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 18px;
    align-items: stretch;
  }
  .compact {
    font-size: 23px;
    line-height: 1.28;
  }
  .compact li {
    margin: 4px 0;
  }
  .card {
    border: 1px solid #dadce0;
    border-radius: 8px;
    padding: 20px 22px;
    background: #ffffff;
    box-shadow: 0 1px 3px rgba(60,64,67,0.10);
  }
  .card h3 {
    margin-top: 0;
  }
  .soft-card {
    border-radius: 8px;
    padding: 18px 22px;
    background: #f8fafd;
    border: 1px solid #d2e3fc;
  }
  .pipeline {
    display: grid;
    grid-template-columns: 1fr 40px 1fr 40px 1fr 40px 1fr;
    align-items: center;
    gap: 10px;
    margin-top: 22px;
  }
  .pipeline .step {
    border-radius: 8px;
    padding: 18px 16px;
    background: #ffffff;
    border: 1px solid #dadce0;
    min-height: 126px;
  }
  .pipeline .arrow {
    color: #1a73e8;
    font-size: 34px;
    text-align: center;
  }
  .step-num {
    display: inline-flex;
    width: 28px;
    height: 28px;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: #1a73e8;
    color: #ffffff;
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 8px;
  }
  .metric-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin: 18px 0;
  }
  .mini-stat {
    border-left: 5px solid #1a73e8;
    background: #f8fafd;
    padding: 14px 18px;
    border-radius: 6px;
  }
  .mini-stat strong {
    display: block;
    color: #202124;
    font-size: 32px;
    line-height: 1.1;
  }
  .mini-stat span {
    color: #5f6368;
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 1px;
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
  .source-line {
    color: #5f6368;
    font-size: 14px;
    margin-top: 14px;
  }
  .small {
    color: #5f6368;
    font-size: 20px;
  }
  .accent-blue { color: #1a73e8; }
  .accent-green { color: #188038; }
  .accent-yellow { color: #b06000; }
  .accent-red { color: #d93025; }
  footer {
    color: #5f6368;
    font-size: 14px;
  }
  section.hero footer {
    color: rgba(255,255,255,0.70);
  }
footer: "Decision Lineage with BigQuery Context Graphs · examples/decision_lineage_demo"
---

<!--
Copyright 2026 Google LLC
Licensed under the Apache License, Version 2.0.

Decision Lineage with BigQuery Context Graphs — leadership deck.

Render with Marp (https://marp.app):
  marp SLIDES.md --html --pdf     # SLIDES.pdf
  marp SLIDES.md --html --pptx    # SLIDES.pptx
  marp SLIDES.md --html           # SLIDES.html
  marp SLIDES.md --watch --html   # live preview
-->

<!-- _class: hero -->
<!-- _paginate: false -->

<span class="pill" style="background:rgba(255,255,255,0.15);color:#fff">EU AI Act · GDPR · DSA</span>

# Decision Lineage for AI Agents

## Turn live agent behavior into queryable BigQuery evidence: decisions, options, outcomes, and rationale.

<div class="footer-note">
BigQuery Agent Analytics SDK · examples/decision_lineage_demo · Issue #98
</div>

<!--
SPEAKER NOTE — 30s
Open with business pressure, not fear. The point is not "this certifies
compliance"; the point is "we can now show our work with data."
-->

---

# Why this matters now

<div class="grid-3">

<div class="card">

### Regulation
EU AI Act obligations phase in through **2 Aug 2027**; many high-risk and transparency rules start **2 Aug 2026**.

</div>

<div class="card">

### Trust
Most agent demos produce an answer and ask reviewers to trust a transcript. That does not scale to audit.

</div>

<div class="card">

### Operations
Product, legal, and engineering need the same answer: *what happened, why, and where is the evidence?*

</div>

</div>

<div class="soft-card">
<strong>Risk framing:</strong> AI Act fines reach up to <strong>7% of worldwide annual turnover</strong> for prohibited practices, and up to <strong>3%</strong> for many other operator / transparency obligations. The useful response is evidence, not screenshots.
</div>

<div class="source-line">Sources: EU AI Act Service Desk implementation timeline; AI Act Article 99 penalty tiers.</div>

<!--
Source: https://ai-act-service-desk.ec.europa.eu/en/ai-act/eu-ai-act-implementation-timeline
Article 99: 7% applies to prohibited practices; 3% applies to many
non-Article-5 obligations. Keep the wording precise.
-->

---

# What we built

<div class="kicker">Working demo, not mock data</div>

Take a real ADK media-planning agent, attach the **BigQuery Agent Analytics Plugin**, use `AI.GENERATE` to extract the decisions and options present in the traces, then publish the result as a **BigQuery Property Graph** for GQL in BigQuery Studio.

<div class="pipeline">

<div class="step"><span class="step-num">1</span><br/><strong>Live agent</strong><br/><span class="small">Gemini 2.5 Pro campaign planner</span></div>
<div class="arrow">→</div>
<div class="step"><span class="step-num">2</span><br/><strong>Plugin spans</strong><br/><span class="small">162 recorded events in BigQuery</span></div>
<div class="arrow">→</div>
<div class="step"><span class="step-num">3</span><br/><strong>Extraction</strong><br/><span class="small">Entities, decisions, options, rationales</span></div>
<div class="arrow">→</div>
<div class="step"><span class="step-num">4</span><br/><strong>Property graph</strong><br/><span class="small">Query, visualize, audit</span></div>

</div>

<blockquote><strong>Promise:</strong> the graph is derived from real plugin output. The demo does not rely on hand-shaped seed traces.</blockquote>

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

<div class="metric-row">
<div class="mini-stat"><strong>6</strong><span>sessions</span></div>
<div class="mini-stat"><strong>162</strong><span>plugin spans</span></div>
<div class="mini-stat"><strong>31</strong><span>extracted decisions</span></div>
<div class="mini-stat"><strong>92</strong><span>decision options</span></div>
</div>

Each row is one ADK invocation against `gemini-2.5-pro`; each invocation produced **27 plugin-recorded spans** in the verified demo run.

<!--
Talk track: real diversity — different brand, audience, budget, season.
If live extraction changes, keep the exact counts in the metric row aligned
with the latest verified dataset or switch them to approximate language.
-->

---

# Three writers populate seven tables

<div class="grid-3">

<div class="card">
<h3>BQ AA Plugin</h3>
<strong>Writes</strong> `agent_events`<br/>
<span class="small">Raw trace evidence from the live ADK runner.</span>
</div>

<div class="card">
<h3>SDK AI.GENERATE</h3>
<strong>Writes</strong> `extracted_biz_nodes`, `decision_points`, `candidates`<br/>
<span class="small">Typed facts extracted from trace text.</span>
</div>

<div class="card">
<h3>SDK SQL DML</h3>
<strong>Writes</strong> `context_cross_links`, `made_decision_edges`, `candidate_edges`<br/>
<span class="small">Graph edges with stable keys.</span>
</div>

</div>

<blockquote>`AI.GENERATE` runs twice across all sessions: business entities first, then decision options and rationale.</blockquote>

---

# The graph the demo presents

The SDK ships the canonical graph. The demo adds **ads-domain labels** so BigQuery Studio reads like the business process:

<div class="grid-2">

<div class="card">

### Primary audit path
`CampaignRun` → `PlanningDecision` → `DecisionOption` → `OptionOutcome`

`DecisionOption` → `DropReason`

<span class="small">A campaign has planning decisions; each decision weighed options; every option has an outcome and dropped options have rationale.</span>

</div>

<div class="card">

### Evidence path
`CampaignRun` → `AgentStep` → `PlanningDecision`

`AgentStep` → `MediaEntity`

`PlanningDecision` → `DecisionCategory`

<span class="small">The business answer stays tied to the span, entity, and category that produced it.</span>

</div>

</div>

<!--
Explorer vocabulary in the graph pane:
CampaignRun, AgentStep, MediaEntity, PlanningDecision, DecisionOption,
DecisionCategory, OptionOutcome, DropReason; edges CampaignActivity,
NextStep, DecidedAt, ConsideredEntity, CampaignDecision, WeighedOption,
HasOutcome, RejectedBecause, InCategory.
-->

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

**Three rows.** Selected: *Serious Runners 18-35* @ 0.99. Dropped: *Casual Runners 25-45* (lower purchase intent), *Fitness Enthusiasts 18-35* (group too broad). **The extracted rationale stays attached to the option it explains.**

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

**Multiple matches.** *Youth Track & Field (13-15) — outside specified 16-22 range.* *Affluent Hikers (35-55) — age-range mismatch.* The graph surfaces specific rationales for **human review**: proxy risk, legitimate campaign constraint, or extraction artifact?

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

**Three rows.** Selected: *"Built for a New Record"* @ 0.97. Two dropped with rationale, both pointing back to the same `evidence_span_id`. That span lives in `agent_events` with content, timestamp, and latency. **Record-keeping becomes a queryable trail.**

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

<div class="small"><strong>Audience Selection has the lowest average dropped score.</strong> Reuse the Q2 filter for repeatable bias review.</div>

---

<!-- _class: section-divider -->

<div class="eyebrow">Section 3 of 4</div>

# What this unlocks

---

# Three takeaways for leadership

<div class="grid-2">

<div>

### 1. Audit posture as a query

The audit surface for our agent platform is now a **live BigQuery query**, not a slide deck or quarterly review.

### 2. The schema generalizes

Brand-neutral, channel-neutral, decision-type-neutral. Point an instrumented agent at the same pipeline and the same audit patterns apply.

</div>

<div>

### 3. Composes with what we run

- **BQ AA Plugin** — the instrumentation path
- **SDK extraction** — one method call: `build_context_graph(use_ai_generate=True, include_decisions=True)`
- **BigQuery** — the warehouse you already pay for

No new graph service. No separate audit datastore.

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
| **Q5** Systemic-pattern audit | Art. 17, Art. 60 | Art. 35 | Art. 26 |

Five queries against one graph create reusable **evidence hooks** for three EU regulatory conversations. This is not a compliance certification; it is the data substrate a compliance review needs.

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

**Order-of-cents for the verified demo run.** Queries against the prebuilt graph are lightweight.

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
| **How long to deploy?** | Plugin integration, SDK call, committed DDL. **Days, not quarters**, for a pilot. |
| **Other agents we haven't instrumented?** | Same plugin, same plug-in point. Schema doesn't care which agent wrote the spans. |
| **Cost?** | Two `AI.GENERATE` calls per build + standard BQ query cost. **Order-of-cents for this verified demo run.** |
| **What if `AI.GENERATE` misses a decision?** | Build script reports per-session counts. Re-run extraction without re-running the agent. Talk track is count-agnostic by design. |
| **Does this expose PII?** | Stores trace-derived rationale text. PII handling follows the existing `agent_events` retention and access policy. |
| **Who operates this?** | The team that owns the agent platform. Plugin write path + SDK read path both already on-call. |

---

<!-- _class: hero -->
<!-- _paginate: false -->

# Q&A

## From agent behavior to auditable evidence

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
