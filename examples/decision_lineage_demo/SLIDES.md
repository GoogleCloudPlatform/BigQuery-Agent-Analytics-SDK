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
  section.dense {
    font-size: 22px;
    padding: 44px 54px;
  }
  section.dense h1 {
    font-size: 34px;
    margin-bottom: 8px;
  }
  section.dense h2 {
    font-size: 22px;
  }
  section.dense h3 {
    font-size: 18px;
  }
  section.dense table {
    font-size: 17px;
    margin: 8px 0;
  }
  section.dense th,
  section.dense td {
    padding: 5px 8px;
  }
  section.dense pre {
    font-size: 12px;
    padding: 12px 14px;
  }
  section.dense .small {
    font-size: 16px;
  }
  section.dense .compact {
    font-size: 18px;
  }
  section.dense footer {
    display: none;
  }
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

`SLIDES.html` and `SLIDES.pptx` are checked in alongside this
source so reviewers can open the deck without installing Marp.
After editing this file, regenerate both with:

  npx -y @marp-team/marp-cli@latest SLIDES.md --html
  npx -y @marp-team/marp-cli@latest SLIDES.md --html --pptx --no-stdin

(or `marp SLIDES.md --html --pptx` if you have Marp installed
globally via `npm install -g @marp-team/marp-cli`).
-->


<!-- _class: hero -->
<!-- _paginate: false -->

<span class="pill" style="background:rgba(255,255,255,0.15);color:#fff">EU AI Act · GDPR · DSA</span>

# Decision Lineage for AI Agents

## From "Systems of Action" to "Systems of Governance" — extracted decisions, options, outcomes, and rationale queryable in BigQuery.

<div class="footer-note">
BigQuery Agent Analytics SDK · examples/decision_lineage_demo
</div>

<!--
SPEAKER NOTE — 30s
Open with business pressure, not fear. Frame: AI agents are no longer
just doing tasks (Systems of Action) — regulators now require we can
prove what they did and why (Systems of Governance). The deck walks
that line, top to bottom.
-->

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 1 · Market context</div>

# The regulatory landscape just changed

---

# Why now — three regulations converging

<div class="grid-3">

<div class="card">

### EU AI Act
**Regulation (EU) 2024/1689**, in force 1 Aug 2024.
Most operator obligations apply from **2 Aug 2026**; full rollout by **2 Aug 2027**.

<div class="small">High-risk-system rules cover transparency, record-keeping, human oversight, post-market monitoring.</div>

</div>

<div class="card">

### GDPR
**Article 22** — protections around solely automated decisions with legal or similarly significant effects.

<div class="small">Access / transparency rights create the "meaningful information about logic" audit expectation.</div>

</div>

<div class="card">

### Digital Services Act
**Article 26** — online platforms presenting ads must disclose that an item is an ad, who paid for it, and the main targeting parameters.

<div class="small">The demo's ad-planning lineage gives teams the upstream evidence behind those disclosures.</div>

</div>

</div>

<div class="source-line">
Sources: <a href="https://ai-act-service-desk.ec.europa.eu/en/ai-act/timeline/timeline-implementation-eu-ai-act">EU AI Act Service Desk timeline</a> · <a href="https://eur-lex.europa.eu/eli/reg/2016/679/oj">GDPR — Reg. 2016/679</a> · <a href="https://eur-lex.europa.eu/eli/reg/2022/2065/oj">DSA — Reg. 2022/2065</a>
</div>

---

<!-- _class: dense -->

# The threat — Article 99 penalty tiers

The AI Act sets fixed-amount maximums **and** turnover thresholds. The fine is the **higher of the two** for non-SMEs.

<table>
<thead>
<tr><th>Article 99 paragraph</th><th>What violates it</th><th>Cap (non-SME)</th></tr>
</thead>
<tbody>
<tr><td><strong>Art 99(3)</strong></td><td>Article 5 — prohibited AI practices (e.g. manipulation that exploits vulnerabilities, social scoring, biometric categorisation by protected attributes)</td><td><strong>€35M</strong> or <strong>7%</strong> of worldwide annual turnover, <em>whichever is higher</em></td></tr>
<tr><td><strong>Art 99(4)</strong></td><td>Most other AI Act obligations — operators of high-risk systems, transparency, record-keeping, post-market monitoring (Arts 16, 22, 23, 24, 26, 31, 33, 34, 50)</td><td><strong>€15M</strong> or <strong>3%</strong> of worldwide annual turnover, <em>whichever is higher</em></td></tr>
<tr><td><strong>Art 99(5)</strong></td><td>Supplying incorrect, incomplete, or misleading information to authorities</td><td><strong>€7.5M</strong> or <strong>1%</strong> of worldwide annual turnover, <em>whichever is higher</em></td></tr>
</tbody>
</table>

<div class="small" style="margin-top:14px">
For an ad-tech buyer with <strong>€20B</strong> worldwide annual turnover, the practical maximum under <strong>Art 99(4)</strong> is <strong>€600M per finding</strong> (3%, not the €15M floor). For SMEs and start-ups, Art 99(6) caps at the <em>lower</em> of the two values.
</div>

<div class="source-line">
Source: <a href="https://eur-lex.europa.eu/eli/reg/2024/1689/oj">Reg. 2024/1689 official journal, Article 99</a>
</div>

---

# The narrative shift

<div class="grid-2">

<div class="soft-card">

### Yesterday — Systems of Action
AI agents that <strong>do tasks</strong> end-to-end:
<ul class="compact">
<li>Pick the audience for a campaign</li>
<li>Allocate the media budget</li>
<li>Choose the creative theme</li>
<li>Set the launch window</li>
</ul>

The agent <em>does</em>. We trust the output.

</div>

<div class="card">

### Today — Systems of Governance
The same agents, plus a <strong>queryable evidence layer</strong>:
<ul class="compact">
<li>The decisions extracted from the agent trace</li>
<li>The options and scores the trace exposes</li>
<li>The rationale attached to dropped options</li>
<li>Linked to the trace span that produced it</li>
</ul>

The agent acts. The graph proves.

</div>

</div>

> The mandate isn't *don't use agents* — it's *be able to show your work, on demand, in audit format*.

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 2 · Business value</div>

# What "Decision Lineage" gives you

---

# Decision Lineage, defined

<div class="kicker">Working definition</div>

<blockquote>
For any agent decision: the ability to retrieve <strong>what was chosen</strong>, <strong>what alternatives were considered</strong>, <strong>what scores or criteria were applied</strong>, <strong>why each alternative was rejected</strong>, and <strong>which trace span produced the decision</strong> — as a single BigQuery query.
</blockquote>

<div class="grid-3" style="margin-top:24px">
<div class="card"><h3>Trust</h3>The data exists at the moment the regulator asks. No reconstruction.</div>
<div class="card"><h3>Transparency</h3>Same answer for product, legal, and engineering — one source of truth.</div>
<div class="card"><h3>Reproducibility</h3>Re-running the audit query a year later returns the same evidence.</div>
</div>

---

# What the demo lets you prove

<div class="metric-row">
<div class="mini-stat"><strong>Right to explanation</strong><span>GDPR Art 22 · AI Act Art 86</span></div>
<div class="mini-stat"><strong>Bias monitoring</strong><span>AI Act Art 10 / 71</span></div>
<div class="mini-stat"><strong>Human oversight</strong><span>AI Act Art 14</span></div>
<div class="mini-stat"><strong>Reproducibility</strong><span>AI Act Art 12 / 13</span></div>
</div>

<div class="grid-2" style="margin-top:14px">
<div class="card">

### What this is
- A queryable record of agent decisions plus alternatives plus reasoning
- The audit substrate regulators ask for under each article above
- Built from real agent traces by an open-source SDK

</div>
<div class="card">

### What this is not
- Not a compliance certification — talk to your legal counsel for that
- Not a replacement for a Data Protection Impact Assessment
- Not a model-quality scorecard — it audits what the agent <em>did</em>, not whether it was <em>right</em>

</div>
</div>

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 3 · Customer proof</div>

# A concrete pattern from real ad-tech buyers

---

# Real-world pattern — programmatic media-buyer

<div class="kicker">Example pattern (anonymised)</div>

<div class="grid-2">

<div class="card">

### The pain
A major brand-side media-buyer running a multi-agent media-planning stack:
<ul class="compact">
<li>Internal review board needed evidence for campaign decisions (audience, placement, creative, schedule)</li>
<li>Compliance team owed regulators a quarterly bias-audit report on demographic targeting</li>
<li>An adjudicator asked <em>"why was this audience excluded from this campaign?"</em> — answer required digging through Slack threads + run logs</li>
</ul>

</div>

<div class="card">

### The shape of the fix
Decision Lineage on BigQuery:
<ul class="compact">
<li>Every agent invocation captured by the BQ AA Plugin</li>
<li>Decisions + alternatives + rationale extracted by <code>AI.GENERATE</code></li>
<li>Property graph queryable by compliance + product without writing Python</li>
<li>Same query reused for the quarterly bias-audit and for one-off subpoena responses</li>
</ul>

</div>

</div>

<div class="small" style="margin-top:18px">
<em>Replace this slide with your customer's pain point and timeline once a reference customer is named — the demo plugs into any agent the BQ AA Plugin already covers.</em>
</div>

---

# Customer voice (placeholder)

<blockquote style="font-size:30px; line-height:1.4">
"Before this, every audit request was a fire drill across three teams. Now we hand the regulator a five-line GQL query and the answer is the same on every run. The compliance posture moved from <em>defensible</em> to <em>queryable</em>."
</blockquote>

<div class="small" style="margin-top:14px">
— Director, Audience Strategy at a major DSP <span class="accent-blue">[placeholder — swap with a real attributable quote before external use]</span>
</div>

<div class="metric-row" style="margin-top:24px">
<div class="mini-stat"><strong>~3 weeks</strong><span>Audit response (before)</span></div>
<div class="mini-stat"><strong>One query</strong><span>Audit response (after)</span></div>
<div class="mini-stat"><strong>5 articles</strong><span>Regulatory hooks mapped</span></div>
<div class="mini-stat"><strong>Low cost</strong><span>BQ query over existing graph</span></div>
</div>

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 4 · Practical demo</div>

# What an auditor actually sees

---

# The auditor persona — five questions, live

A compliance reviewer, equipped with the BigQuery Conversational Analytics panel, asks five questions of one dataset:

<div class="grid-2">
<div>
<ul class="compact">
<li><strong>Q1</strong> — <em>"Why did the agent pick this audience?"</em></li>
<li><strong>Q2</strong> — <em>"Did demographic criteria ever cause a candidate to be dropped?"</em></li>
<li><strong>Q3</strong> — <em>"Were any decisions committed below 0.7 confidence?"</em></li>
<li><strong>Q4</strong> — <em>"Show me the full audit trail for the Adidas creative-theme decision."</em></li>
<li><strong>Q5</strong> — <em>"Which decision categories reject candidates least decisively?"</em></li>
</ul>
</div>
<div>

### Why this format
Ground the demo in the **auditor experience**, not the engineering pipeline. The reviewer starts with a business question; the system returns a reproducible evidence path that legal and engineering can inspect together.

The next slides show the natural-language prompt, the GQL pattern, and the live answer from the demo dataset.
</div>
</div>

---

# Q1 — Right to explanation

<div class="pill">EU AI Act Art 86</div> <div class="pill">GDPR Art 22</div>

> *"For Nike Summer Run, what audience did the agent pick, and why did it reject the alternatives?"*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
      -[:WeighedOption]->(opt:DecisionOption)
WHERE cr.session_id = '<SESSION>'
  AND LOWER(dp.decision_type) LIKE '%audience%'
RETURN DISTINCT opt.status, opt.name, opt.score, opt.rejection_rationale
ORDER BY opt.status DESC, opt.score DESC;
```

**Three rows.** Selected: *Serious Runners 18-35* @ 0.99. Dropped: *Casual Runners 25-45* (lower purchase intent on high-performance footwear), *Fitness Enthusiasts 18-35* (group too broad for running conversion). **Each rationale extracted by `AI.GENERATE` from the LLM_RESPONSE trace text.**

---

# Q2 — Bias / fairness audit

<div class="pill">EU AI Act Art 10</div> <div class="pill">EU AI Act Art 71</div>

> *"Across our 2026 portfolio, did the agent ever reject a candidate based on age or demographic criteria?"*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status = 'DROPPED'
  AND (LOWER(opt.rejection_rationale) LIKE '%age %'
       OR LOWER(opt.rejection_rationale) LIKE '%demographic%'
       OR LOWER(opt.rejection_rationale) LIKE '%youth%')
RETURN DISTINCT dp.decision_type, opt.name, opt.rejection_rationale;
```

Multiple matches. Examples (verbatim from the live extraction):
- *"Youth Track & Field (13-15) — outside specified 16-22 range, less focused on in-season purchase"*
- *"Affluent Hikers (35-55) — significant age-range mismatch with target demo"*

**The graph surfaces specific rationales for human review** — proxy or legitimate ad-targeting? The reviewer judges from data, not trust.

---

# Q3 — Human-oversight trigger

<div class="pill">EU AI Act Art 14</div>

> *"Did the agent ever commit a decision below 0.7 confidence? That should have triggered human review."*

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status = 'SELECTED' AND opt.score < 0.7
RETURN DISTINCT dp.session_id, dp.decision_type, opt.name, opt.score
ORDER BY opt.score ASC;
```

<div style="text-align:center; margin: 22px 0">
<div class="stat">0</div>
<div class="stat-label">rows returned</div>
</div>

**The empty result <em>is</em> the audit artifact.** "We ran the human-oversight predicate against the entire portfolio for this period. The trigger never fired." Tighten to 0.85 → instant new at-risk list, same query.

---

# Q4 + Q5 — Reproducibility and pattern audit

<div class="grid-2">

<div class="card">

### Q4 — Subpoena reproducibility
<div class="pill">Art 12</div> <div class="pill">Art 13</div>

```sql
MATCH (step:AgentStep)-[:DecidedAt]->(dp:PlanningDecision)
      -[:WeighedOption]->(opt:DecisionOption)
WHERE dp.session_id='<S>'
  AND LOWER(dp.decision_type) LIKE '%creative%'
  AND step.event_type='LLM_RESPONSE'
RETURN dp.span_id, opt.status, opt.name, opt.score,
       opt.rejection_rationale;
```

3 rows. All point to one `evidence_span_id` → that span lives in `agent_events` with full content + timestamp + latency.

</div>

<div class="card">

### Q5 — Systemic pattern
<div class="pill">Art 17</div> <div class="pill">Art 60</div>

```sql
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE opt.status='DROPPED'
RETURN dp.decision_type,
       COUNT(opt) AS rejections,
       AVG(opt.score) AS avg_dropped_score
GROUP BY dp.decision_type
ORDER BY rejections DESC;
```

**Audience Selection** rejects with the **lowest avg confidence (0.66)** — the category most worth a fairness loop-back to Q2.

</div>

</div>

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 5 · Technical architecture</div>

# How the evidence is built — every step concrete

---

# End-to-end pipeline

<div class="pipeline">

<div class="step">
<div class="step-num">1</div>
<strong>ADK agent</strong>
<div class="small">Gemini 2.5 Pro · 5 tools · system prompt requires 3-candidate enumeration</div>
</div>
<div class="arrow">▶</div>

<div class="step">
<div class="step-num">2</div>
<strong>BQ AA Plugin</strong>
<div class="small"><code>InMemoryRunner(plugins=[bq_logging_plugin])</code> → spans land in <code>agent_events</code></div>
</div>
<div class="arrow">▶</div>

<div class="step">
<div class="step-num">3</div>
<strong>SDK extraction</strong>
<div class="small">Two <code>AI.GENERATE</code> calls — biz nodes (MERGE) + decisions (load job)</div>
</div>
<div class="arrow">▶</div>

<div class="step">
<div class="step-num">4</div>
<strong>Property graph</strong>
<div class="small">Canonical 4-pillar + ads-domain rich layer queried via GQL</div>
</div>

</div>

<div class="grid-2" style="margin-top:24px">
<div class="card">

### What runs locally (one-time setup, ~5–10 min)
`./setup.sh` does steps 1-4 end-to-end on a fresh GCP project: enables APIs, creates the dataset, runs the live agent, extracts decisions, emits the property-graph DDL.

</div>
<div class="card">

### What runs at audit time (seconds)
Every regulator question is a single GQL query against the property graph. No re-extraction. No agent rerun. **The graph is the audit substrate.**

</div>
</div>

---

<!-- _class: dense -->

# Step 1 — The ADK media-planner agent

<div class="grid-2">

<div>

### `agent/agent.py`
- `google.adk.agents.Agent` instance
- Model: **Gemini 2.5 Pro** (regional, Vertex AI)
- 5 decision-commit tools (one per category)
- BQ AA Plugin attached to `InMemoryRunner`

### `agent/tools.py` — five tools
| Tool | Decision category |
|---|---|
| `select_audience` | audience selection |
| `allocate_budget` | budget allocation |
| `select_creative` | creative theme |
| `define_channel_strategy` | channel strategy |
| `schedule_launch` | launch scheduling |

</div>

<div>

### `agent/prompts.py` — system prompt
The prompt instructs the agent to, for each structured decision:

<div class="soft-card compact">
1. Name <strong>three candidate options</strong><br>
2. Score each on 0.0–1.0 (two decimals)<br>
3. Mark exactly one <strong>SELECTED</strong>, the other two <strong>DROPPED</strong><br>
4. Give an <strong>explicit, specific rejection rationale</strong> for each dropped option<br>
5. End with <code>Decision: …</code> then call the corresponding tool
</div>

<div class="small" style="margin-top:10px">
The prompt structure is the contract that makes downstream extraction reliable. The LLM_RESPONSE text is what <code>AI.GENERATE</code> later parses.
</div>

</div>

</div>

---

# Step 2 — The 6 campaigns × 27 spans

<div class="grid-2">

<div>

### `campaigns.py` — 6 briefs
| Brand | Campaign | Budget |
|---|---|---:|
| Nike | Summer Run 2026 | $360K |
| Nike | Winter Trail 2026 | $500K |
| Adidas | Track Season 2026 | $420K |
| Puma | Soccer Cup 2026 | $280K |
| Reebok | CrossFit Open 2026 | $340K |
| Lululemon | Yoga Flow 2026 | $250K |

### `run_agent.py`
Iterates briefs; one `InMemoryRunner` invocation per brief; awaits `flush()` + `shutdown()` on the plugin so all spans land before extraction starts; writes `campaign_runs` mapping (deterministic).

</div>

<div>

### Per session — 27 plugin-recorded spans
| Event type | Count |
|---|---:|
| `INVOCATION_STARTING` | 1 |
| `AGENT_STARTING` | 1 |
| `USER_MESSAGE_RECEIVED` | 1 |
| `LLM_REQUEST` / `LLM_RESPONSE` | 5 + 5 |
| `TOOL_STARTING` / `TOOL_COMPLETED` | 5 + 5 |
| `HITL_CONFIRMATION_REQUEST` / `_COMPLETED` | 1 + 1 |
| `AGENT_COMPLETED` | 1 |
| `INVOCATION_COMPLETED` | 1 |

**6 sessions × 27 spans = 162 TechNode rows.** Each span carries `span_id`, `parent_span_id`, `session_id`, `event_type`, `agent`, `timestamp`, JSON `content`, `latency_ms`.

</div>

</div>

---

# Step 3a — `AI.GENERATE` extraction (BizNodes)

`build_graph.py` calls `mgr.extract_biz_nodes(session_ids)` which runs **one MERGE** statement against `agent_events`. The MERGE's USING clause invokes `AI.GENERATE` per row:

```sql
MERGE `<P>.<D>.extracted_biz_nodes` AS target
USING (
  SELECT base.span_id, base.session_id,
    JSON_EXTRACT_SCALAR(entity, '$.entity_type')  AS node_type,
    JSON_EXTRACT_SCALAR(entity, '$.entity_value') AS node_value,
    CAST(JSON_EXTRACT_SCALAR(entity, '$.confidence') AS FLOAT64) AS confidence
  FROM `<P>.<D>.agent_events` AS base,
  UNNEST(JSON_EXTRACT_ARRAY(REGEXP_REPLACE(REGEXP_REPLACE(
    AI.GENERATE('Extract business entities (Product, Audience, Channel, …) from this payload. Return JSON array of {entity_type, entity_value, confidence}.\n\nPayload:\n' || payload_text,
                endpoint => 'gemini-2.5-flash').result,
    r'^```(?:json)?\s*',''), r'\s*```$',''))) AS entity
  WHERE base.session_id IN UNNEST(@session_ids)
    AND base.event_type IN ('USER_MESSAGE_RECEIVED','LLM_RESPONSE','TOOL_COMPLETED','AGENT_COMPLETED')
) AS source
ON target.biz_node_id = source.biz_node_id
WHEN MATCHED THEN UPDATE SET …
WHEN NOT MATCHED BY TARGET THEN INSERT …
WHEN NOT MATCHED BY SOURCE AND target.session_id IN UNNEST(@session_ids) THEN DELETE
```

**Per-session idempotent in one statement.** No streaming-buffer pitfall. `MERGE` is the SDK's chosen pattern for the BizNode write path.

---

# Step 3b — `AI.GENERATE` extraction (Decisions + Candidates)

`mgr.extract_decision_points(session_ids)` runs a separate `AI.GENERATE` query whose prompt asks for structured decision data:

```text
Identify decision points in this agent payload. A decision point is where
the agent evaluated multiple candidates and selected or rejected them.
For each decision, return decision_type, description, and all candidates
with name, score (0-1), status (SELECTED or DROPPED), and rejection_rationale
(null if selected, required reason if dropped).
```

The Python side parses each row's JSON, builds `DecisionPoint` + `Candidate` records, then `store_decision_points(...)`:

<div class="grid-3" style="margin-top:14px">

<div class="card">
<div class="step-num">1</div>
<strong>Dedupe in Python</strong>
<div class="small"><code>_dedupe_rows_by_key</code> last-wins on <code>decision_id</code> / <code>candidate_id</code></div>
</div>

<div class="card">
<div class="step-num">2</div>
<strong>DELETE FROM ... WHERE session_id IN (...)</strong>
<div class="small">Per-session reseat — guards against re-running</div>
</div>

<div class="card">
<div class="step-num">3</div>
<strong>load_table_from_json</strong>
<div class="small">Load job to managed storage; visible to the just-issued DELETE (no streaming-buffer trap)</div>
</div>

</div>

---

# Step 3c — SQL-only edge derivation

After the node tables exist, three pure-SQL `INSERT INTO` statements build the edges (no `AI.GENERATE`):

```sql
-- Evaluated edge (BizNode ↔ TechNode lineage)
INSERT INTO context_cross_links (link_id, span_id, biz_node_id, link_type, …)
SELECT b.biz_node_id, b.span_id, b.biz_node_id, 'EVALUATED', …
FROM extracted_biz_nodes b WHERE b.session_id IN UNNEST(@session_ids);

-- MadeDecision edge (TechNode → DecisionPoint)
INSERT INTO made_decision_edges (edge_id, span_id, decision_id, …)
SELECT CONCAT(span_id, ':MADE_DECISION:', decision_id), span_id, decision_id, …
FROM decision_points WHERE session_id IN UNNEST(@session_ids);

-- CandidateEdge (DecisionPoint → CandidateNode, with edge_type)
INSERT INTO candidate_edges (edge_id, decision_id, candidate_id, edge_type, …)
SELECT …, CASE c.status WHEN 'SELECTED' THEN 'SELECTED_CANDIDATE'
                        ELSE 'DROPPED_CANDIDATE' END, …
FROM candidates c WHERE c.session_id IN UNNEST(@session_ids);
```

Three tables, one statement each, all per-session-scoped. The `edge_type` on `candidate_edges` is what powers Block 4's `WHERE ce.edge_type = 'DROPPED_CANDIDATE'` filter.

---

<!-- _class: dense -->

# Step 4 — The 7 SDK backing tables

| Table | Key | Written by | What it holds |
|---|---|---|---|
| `agent_events` | `span_id` | BQ AA Plugin | Plugin-recorded spans (162 rows = 6 sessions × 27 spans) |
| `extracted_biz_nodes` | `biz_node_id` | SDK MERGE | Business entities from trace text |
| `context_cross_links` | `link_id` | SDK DML | Span ↔ BizNode references |
| `decision_points` | `decision_id` | SDK load job | Extracted decisions from `LLM_RESPONSE` text |
| `candidates` | `candidate_id` | SDK load job | Extracted options per decision: selected or dropped |
| `made_decision_edges` | `edge_id` | SDK DML | Span → Decision lineage |
| `candidate_edges` | `edge_id` | SDK DML | Decision → Candidate, with selected / dropped edge type |

Every backing table has `row_count == distinct_keys` after the SDK fix landed in PR #99 — the property-graph KEY contract holds end-to-end.

---

<!-- _class: dense -->

# Step 5 — The rich-graph projection layer

`build_rich_graph.py` adds **demo-only** SQL projections so the BigQuery Studio Explorer reads in business language. **No new AI calls** in this step:

| Derived table | Built from | Purpose |
|---|---|---|
| `campaign_runs` | `run_agent.py` writes directly | One row per agent invocation, joined to campaign metadata in `campaigns.py` |
| `rich_agent_steps` | `agent_events` (DISTINCT) | Deduped span projection — one row per `span_id` (TechNode is multi-event per span by design) |
| `rich_decision_types` | `decision_points` | Normalised decision categories (`audience-selection`, `budget-allocation`, …) |
| `rich_candidate_statuses` | `candidates` | Distinct `OptionOutcome` values (SELECTED, DROPPED) |
| `rich_rejection_reasons` | `candidates` | Distinct rejection-rationale strings as first-class nodes |

Plus five edge projections wiring the new labels back to SDK-owned facts. Schema lives in `rich_property_graph.gql.tpl` and is deterministic across reruns.

---

<!-- _class: dense -->

# Step 6 — The 8 node labels (what each node means)

<table>
<thead>
<tr><th>Label</th><th>Source table</th><th>KEY</th><th>What it represents</th></tr>
</thead>
<tbody>
<tr><td><strong>CampaignRun</strong></td><td>campaign_runs</td><td>session_id</td><td>One agent invocation against one brief — the unit of audit</td></tr>
<tr><td><strong>AgentStep</strong></td><td>rich_agent_steps</td><td>span_id</td><td>One step the agent took (LLM call, tool invocation, HITL check)</td></tr>
<tr><td><strong>MediaEntity</strong></td><td>extracted_biz_nodes</td><td>biz_node_id</td><td>An audience, channel, creative, budget unit, or campaign — extracted from trace text</td></tr>
<tr><td><strong>PlanningDecision</strong></td><td>decision_points</td><td>decision_id</td><td>A moment the agent committed to a choice between options</td></tr>
<tr><td><strong>DecisionOption</strong></td><td>candidates</td><td>candidate_id</td><td>One option weighed at a planning decision (selected or dropped)</td></tr>
<tr><td><strong>DecisionCategory</strong></td><td>rich_decision_types</td><td>decision_type_id</td><td>Normalised decision category (audience selection, budget allocation, …)</td></tr>
<tr><td><strong>OptionOutcome</strong></td><td>rich_candidate_statuses</td><td>status_id</td><td>SELECTED or DROPPED — the outcome of weighing</td></tr>
<tr><td><strong>DropReason</strong></td><td>rich_rejection_reasons</td><td>reason_id</td><td>A distinct rejection rationale (deduplicated across the portfolio)</td></tr>
</tbody>
</table>

---

# Step 7 — The 9 edge labels (how the graph connects)

<table>
<thead>
<tr><th>Edge label</th><th>Source → Destination</th><th>Reads as</th></tr>
</thead>
<tbody>
<tr><td><strong>CampaignActivity</strong></td><td>CampaignRun → AgentStep</td><td>"this run produced this step"</td></tr>
<tr><td><strong>NextStep</strong></td><td>AgentStep → AgentStep (parent_span_id → span_id)</td><td>"this step caused that step" (causal chain)</td></tr>
<tr><td><strong>ConsideredEntity</strong></td><td>AgentStep → MediaEntity</td><td>"this step touched this entity"</td></tr>
<tr><td><strong>DecidedAt</strong></td><td>AgentStep → PlanningDecision</td><td>"this step is where the decision committed"</td></tr>
<tr><td><strong>CampaignDecision</strong></td><td>CampaignRun → PlanningDecision</td><td>"this run made this decision"</td></tr>
<tr><td><strong>InCategory</strong></td><td>PlanningDecision → DecisionCategory</td><td>"this decision is an audience-selection / budget-allocation / …"</td></tr>
<tr><td><strong>WeighedOption</strong></td><td>PlanningDecision → DecisionOption</td><td>"this decision considered this option"</td></tr>
<tr><td><strong>HasOutcome</strong></td><td>DecisionOption → OptionOutcome</td><td>"this option was selected / dropped"</td></tr>
<tr><td><strong>RejectedBecause</strong></td><td>DecisionOption → DropReason</td><td>"this option was dropped for this reason"</td></tr>
</tbody>
</table>

<div class="small" style="margin-top:10px">
Read top to bottom in plain English: <em>"this run produced this step → which decided at this planning decision → which weighed this option → which has this outcome → which was rejected because of this reason."</em> Five edges, one query, full audit trail.
</div>

---

# Step 8 — How a GQL query actually traverses

The visualization GQL (the demo's Block 2):

```sql
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH p = (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision)
          -[:WeighedOption]->(opt:DecisionOption)-[:HasOutcome]->(st:OptionOutcome)
WHERE cr.session_id = '<SESSION>'
RETURN p;
```

What BigQuery does, table by table:

<ol class="compact">
<li><code>campaign_runs</code> → bind <code>cr</code>, filtered by <code>session_id</code> (1 row).</li>
<li><code>rich_campaign_decision_edges</code> → join to <code>decision_points</code> on <code>session_id</code> (~5 decisions per session).</li>
<li><code>candidate_edges</code> → join to <code>candidates</code> on <code>decision_id</code> (~3 options per decision).</li>
<li><code>rich_candidate_status_edges</code> → join to <code>rich_candidate_statuses</code> on <code>status</code> (1 row per option).</li>
<li>Return paths bound to <code>p</code>. <strong>BigQuery Studio renders these as one fan-out per decision in the Graph tab.</strong></li>
</ol>

**5 fan-outs of 3 options each = 15 paths visualized for one session, ~97 paths across all 6 campaigns.** The traversal is deterministic, the rendering is interactive.

---

# Step 9 — The temporal dimension

<div class="grid-2">

<div class="card">

### What's timestamped
- `agent_events.timestamp` — when each plugin-recorded span happened (microsecond precision)
- `agent_events.latency_ms` — `{total_ms, time_to_first_token_ms}`
- `context_cross_links.created_at` — when cross-links were derived
- `candidate_edges.created_at` — when decision edges were derived
- `decision_points.span_id` → traces back to the source span's timestamp

</div>

<div class="card">

### What you can ask over time
- *"Across the last 30 days, which decision categories saw rising rejection rates?"*
- *"Show me the per-day count of decisions made with confidence < 0.7."* (oversight trend)
- *"Compare this quarter's rejection-rationale distribution vs last quarter's."* (drift)
- *"Latency p50/p95 for LLM_RESPONSE spans on the audience-selection decision over the past week."*

Each query is a join of `made_decision_edges` ⋈ `agent_events` plus a `WHERE timestamp BETWEEN ...` filter — no schema changes needed.

</div>

</div>

---

<!-- _class: section-divider -->
<div class="eyebrow">Part 6 · The four-guarantee roadmap</div>

# After #58 / #75 / #76 / #104 / #105 ship — own, validate, extract cheaply, resolve

---

# What changes when the four guarantees ship

<div class="grid-2">

<div class="soft-card">

### Today — one pipeline, one cost curve
The SDK does everything from the binding outward:
<ul class="compact">
<li>Issues <code>CREATE OR REPLACE PROPERTY GRAPH</code> on every build</li>
<li>Runs <code>AI.GENERATE</code> over every relevant span — one call per session, prompt grows with span count</li>
<li>No structured pre-flight; binding drift surfaces as an extraction error mid-build, after BQ has already been billed</li>
<li>User-typed query inputs match by literal label, not canonical concept</li>
</ul>

</div>

<div class="card">

### Tomorrow — four checkpoints, four contracts
Same demo, four explicit failure modes:

<ol class="compact">
<li><strong>Own</strong> — your DDL; SDK populates tables only (#104)</li>
<li><strong>Validate</strong> — binding pre-flight (#105) + post-extract validator (#76)</li>
<li><strong>Extract cheaply</strong> — compiled extractors (#75) prune the AI prompt; AI handles semantic gaps only</li>
<li><strong>Resolve</strong> — concept-index reader (#58) translates user inputs to canonical names</li>
</ol>

</div>

</div>

> Same audit answers. Tighter blast radius at every gate. Real cost reduction on the dominant call path.

<!--
SPEAKER NOTE — 45s
Frame as evolution, not rewrite. The five issues #58 / #75 / #76 / #104 /
#105 ship under the storyboard tracked in #107. Every existing audit
question still works the same; the platform contract gets stricter.
The cost story sits in beat 3 — that's the slide finance teams care about.
-->

---

# What the user authors after the four-guarantee roadmap ships

<div class="kicker">Minimum hand-authored input</div>

<div class="grid-3">

<div class="card">

### One file
Either `ontology.yaml` (hand-authored), or a `.ttl` (OWL/SKOS) source compiled via `gm import-owl`. **That's the entire authored surface.**

</div>

<div class="card">

### Generated by tooling
`binding.yaml` + `table_ddl.sql` come from `gm scaffold --ontology X --dataset Y --project Z --out outdir/`. Hand-author the binding instead when pointing at pre-existing tables.

</div>

<div class="card">

### Generated by the plugin
`agent_events` rows arrive as the BQ AA Plugin records spans. The SDK never asks the user to author trace data.

</div>

</div>

<blockquote style="margin-top:18px">
"Provide one ontology file. Everything else — binding, table DDL, property graph — can be generated, owned by you, or both."
</blockquote>

<div class="small" style="margin-top:8px">
Future option (not on the current roadmap): a packaged <code>@builtin:adk-events</code> ontology covering the standard plugin event types — would drop the floor to <em>zero</em> authored YAML for users who don't need domain extraction. Filed as an input-ergonomics follow-up.
</div>

---

<!-- _class: dense -->

# The four guarantees — one card each

<div class="grid-2">

<div class="card">

### 1 · Own — #104
**Command:** `bq-agent-sdk ontology-build --skip-property-graph …`
**User does:** authors `CREATE PROPERTY GRAPH` once, applies it.
**SDK does:** populates base tables; never issues PG DDL.
**Evidence:** zero rows in `INFORMATION_SCHEMA.JOBS_BY_PROJECT WHERE query LIKE '%CREATE OR REPLACE PROPERTY GRAPH%'` after the build window; same GQL traversal returns the same row count.

</div>

<div class="card">

### 2 · Validate — #105 + #76
**Pre-flight:** `bq-agent-sdk binding-validate` runs in &lt;1 s; structured failures carry `binding_path` and `bq_ref`.
**Post-extract:** every build emits a `ValidationReport` with `FallbackScope` ∈ {FIELD, NODE, EDGE} from #76 (EVENT scope arrives with #75 C2's runtime wrapper).
**Per-field AI fallback** re-extracts FIELD-scope failures; NODE / EDGE bubble up to the operator.

</div>

<div class="card">

### 3 · Extract cheaply — #75 (C1 then C2)
**C1:** `gm compile-extractors --event-schemas Z` produces a deterministic bundle with `compile_fingerprint` (64 hex). Re-compile is byte-identical.
**C2 runtime:** spans whose compiled output validates cleanly leave the `AI.GENERATE` prompt entirely (`fully_handled_span_ids`); partial coverage stays in prompt with a focused hint; validation failures fall back to AI.
**Result:** for sessions composed of structured + validating events, `AI.GENERATE` cost approaches zero.

</div>

<div class="card">

### 4 · Resolve — #58 (reader follow-on)
**Already shipped (#92):** `gm compile --emit-concept-index` writes a SKOS-aware lookup table plus a `__meta` provenance sibling.
**New (#58 reader):** `OntologyRuntime` resolves user-typed inputs (`"Consumer Banking"`) to canonical entity names (`skos_RetailBanking`) via `skos:altLabel` matching before the GQL traversal runs.
**Evidence:** match score + path returned in the runtime API output.

</div>

</div>

---

<!-- _class: dense -->

# End-to-end after all four guarantees ship

```bash
# 1. One file you author. Either form works.
cat ontology.yaml                                   # or: gm import-owl my.ttl --out ontology.yaml

# 2. Scaffold the binding + base-table DDL. (Emits gen/binding.yaml + gen/table_ddl.sql.)
gm scaffold --ontology ontology.yaml --dataset campaigns --project my-proj --out gen/

# 3. Author the property-graph DDL once (separate from scaffold's outputs). Apply it.
bq query --nouse_legacy_sql < ddl/property_graph.sql

# 4. Compile deterministic extractors for structured event types.
gm compile-extractors --ontology ontology.yaml --binding gen/binding.yaml \
                      --event-schemas event_schemas/ --out compiled/

# 5. Pre-flight: catches binding drift before any extraction job runs.
bq-agent-sdk binding-validate --ontology ontology.yaml --binding gen/binding.yaml --project my-proj

# 6. Build: SDK populates tables, runtime loads the compiled bundle, AI.GENERATE
#          fires only for semantic gaps, post-extract validator emits ValidationReport.
bq-agent-sdk ontology-build --ontology ontology.yaml --binding gen/binding.yaml \
                            --skip-property-graph --validate-binding \
                            --compiled-bundle compiled/ \
                            --session-ids @latest

# 7. Query the audit graph. User-typed inputs resolve via the concept index.
#    "Consumer Banking" resolves to skos_RetailBanking before the GQL fires.
GRAPH `<P>.<D>.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(opt:DecisionOption)
WHERE dp.decision_type = @resolved_concept
RETURN opt.status, opt.score, opt.rejection_rationale;
```

<div class="small" style="margin-top:10px">
Steps 5 and 6 each produce a typed report; a failed gate stops the next step from spending budget on a doomed run. Step 7's audit query is identical to today's demo — only the input resolution layer is new.
</div>

---

# Cost story — where the savings actually land

<div class="grid-2">

<div class="soft-card">

### Today's cost driver
<ul class="compact">
<li>One <code>AI.GENERATE</code> per session, not per event</li>
<li>Prompt size grows linearly with structured-event span count</li>
<li>For the demo's 6 sessions × 27 spans, the <code>BkaDecisionEvent</code>-shaped payloads dominate the prompt budget</li>
<li>BQ AI billing is <strong>job-level</strong>; per-row token cost is not exposed</li>
</ul>

</div>

<div class="card">

### After #75 C2's validation-gated prune
<ul class="compact">
<li>Spans with compiled output that <em>validates cleanly</em> drop out of the prompt entirely</li>
<li>Spans with partial coverage stay, with a focused hint instead of full payload</li>
<li>Validator failures (NODE / EDGE / EVENT) fall back to AI; FIELD failures re-extract via per-field AI</li>
<li>For sessions whose events are entirely structured + validating, AI cost approaches zero</li>
</ul>

</div>

</div>

<div class="metric-row" style="margin-top:14px">
<div class="mini-stat"><strong>≈ 0</strong><span>Prompt tokens for fully-compiled sessions</span></div>
<div class="mini-stat"><strong>F1 measured</strong><span>vs hand-written + AI baselines</span></div>
<div class="mini-stat"><strong>Byte-identical</strong><span>recompile reproducibility</span></div>
<div class="mini-stat"><strong>Per-field</strong><span>fallback granularity</span></div>
</div>

<div class="small" style="margin-top:10px">
Per-session token columns in the savings table are <em>prompt-size estimates</em> (the transcript actually sent to <code>AI.GENERATE</code> after pruning). Real billing is job-level via <code>INFORMATION_SCHEMA.JOBS_BY_PROJECT.total_bytes_processed</code>. Per-row usage capture is a future enhancement once <code>AI.GENERATE</code> exposes <code>usage_metadata</code>.
</div>

---

<!-- _class: dense -->

# Issue gating — what blocks what

<table>
<thead>
<tr><th>Beat in #107 storyboard</th><th>Gating issue(s)</th><th>Status</th></tr>
</thead>
<tbody>
<tr><td>Beat 1 — own the graph</td><td>#104 (<code>--skip-property-graph</code>)</td><td>Open</td></tr>
<tr><td>Beat 2a — pre-flight</td><td>#105 (binding-validate)</td><td>Open</td></tr>
<tr><td>Beat 2b — post-extract validator</td><td>#76 (<code>ValidationReport</code>, <code>FallbackScope</code>)</td><td>Open</td></tr>
<tr><td>Beat 3a — compile harness + measurement (C1)</td><td>#75 PR 4b / 4c</td><td>Open</td></tr>
<tr><td>Beat 3b — runtime bundle-loading (C2)</td><td>#75 C2 (Option A — prune compiled-extractable payloads, validation-gated)</td><td>Open</td></tr>
<tr><td>Beat 4 — concept-index reader</td><td>#58 reader follow-on</td><td>Emission shipped (#92); reader open</td></tr>
<tr><td>Storyboard / migration notebook</td><td>#107</td><td>This roadmap</td></tr>
</tbody>
</table>

<div class="small" style="margin-top:14px">
The notebook ships with <strong>conditional cells</strong> per #107 — each beat is gated by a feature flag and falls back to a "skipped: requires #X" placeholder until its underlying issue lands. The storyboard merges before the last issue ships; each new beat unlocks as it lands.
</div>

---

<!-- _class: section-divider -->
<div class="eyebrow">Close</div>

# Where to start

---

# Open source — try it on your project

<div class="grid-2">

<div>

### Repository
[`GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)

### The demo bundle
`examples/decision_lineage_demo/`

### One-shot setup (5–10 min)
```bash
cd examples/decision_lineage_demo
./setup.sh
```

### What ships
- `setup.sh` / `reset.sh` — bootstrap + tear-down
- `agent/` + `campaigns.py` — real ADK agent + 6 briefs
- `run_agent.py` + `build_graph.py` + `build_rich_graph.py`
- `bq_studio_queries.gql` — six paste-and-run GQL blocks
- `property_graph.gql` + `rich_property_graph.gql` — DDL templates

</div>

<div>

### Documentation that ships with the bundle
- [`README.md`](README.md) — orientation
- [`SETUP_NEW_PROJECT.md`](SETUP_NEW_PROJECT.md) — clean-project reproduction
- [`DEMO_NARRATION.md`](DEMO_NARRATION.md) — 5-min talk track
- [`BQ_STUDIO_WALKTHROUGH.md`](BQ_STUDIO_WALKTHROUGH.md) — click-by-click in BQ Studio
- [`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md) — the 5 EU questions with verified GQL
- [`DATA_LINEAGE.md`](DATA_LINEAGE.md) — how the 7 tables produce the graph
- [`SLIDES.md`](SLIDES.md) — this deck

### License
Apache 2.0

</div>

</div>

---

<!-- _class: hero -->
<!-- _paginate: false -->

# Q&A

## Decision Lineage with BigQuery Context Graphs

<div class="footer-note">
Open source · Apache 2.0 · github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
<br/>
examples/decision_lineage_demo
</div>

<!--
SPEAKER NOTE — close
"Three takeaways: (1) AI Act fines are real and the trigger date is
near. (2) Decision Lineage is the audit substrate the regulator asks
for, not a compliance certification. (3) The technical pipeline is
open-source and runs on infrastructure you already pay for. Thanks."
-->
