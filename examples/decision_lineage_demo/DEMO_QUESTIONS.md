# EU-Compliance Demo Questions

Five business-shaped questions you can run against
`<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph` to demonstrate
the property graph as an audit surface for the **EU AI Act**, **GDPR
Art. 22** (automated decision-making), and **DSA Art. 26**
(advertising transparency).

> Note on regulatory status: the EU AI Act's obligations are phasing
> in — high-risk-system rules from 2 August 2026, full rollout by
> 2 August 2027 (per the [EU AI Act implementation timeline](https://ai-act-service-desk.ec.europa.eu/en/ai-act/eu-ai-act-implementation-timeline)).
> The framing below is "this provides audit evidence the
> corresponding article will ask for", not "this satisfies the
> article". Talk to your legal team for the official compliance
> reading on your specific deployment.

Each question is paired with:

- the **compliance scenario** a regulator or auditor would ask;
- the **BigQuery Conversational Analytics** prompt to type into BQ
  Studio's Gemini panel;
- the explicit **GQL / SQL** query that produces the same answer;
- a **live answer** captured from a verification run (illustrative —
  exact rows on your project will vary with `AI.GENERATE`
  non-determinism, see [`README.md`](README.md));
- the **regulatory anchor** that makes this question matter.

Replace `<PROJECT_ID>` with your project (or copy the rendered
queries straight out of [`bq_studio_queries.gql`](bq_studio_queries.gql)
after `./setup.sh`). Session ids vary per run — list yours with
Block 1i from `bq_studio_queries.gql` or from the `build_graph.py`
output.

The queries below target the richer demo graph. It keeps the SDK's
canonical tables but exposes ads-domain labels (`AgentStep`,
`MediaEntity`, `PlanningDecision`, `DecisionOption`,
`DecisionCategory`, `OptionOutcome`, `DropReason`) so the same audit
facts are easier to visualize and narrate in BigQuery Studio.

---

## Q1 — Right to explanation for one campaign

> **Compliance scenario.** A media-buyer (or, under GDPR Art. 22,
> an affected user) asks: *for the Nike Summer Run 2026 campaign,
> what audience did the AI pick, what alternatives did it consider,
> and why did it reject the others?*

**BQ Conversational Analytics:**

> "For session `<SESSION_ID>` in the rich_agent_context_graph, show
> every audience-selection decision: the SELECTED audience, every
> alternative DROPPED, and the rejection rationale on each."

**GQL:**

```sql
GRAPH `<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(c:DecisionOption)
      -[:HasOutcome]->(st:OptionOutcome)
WHERE dp.session_id = '<SESSION_ID>'
  AND LOWER(dp.decision_type) LIKE '%audience%'
RETURN DISTINCT
  st.status, c.name AS audience, c.score, c.rejection_rationale
ORDER BY st.status DESC, c.score DESC;
```

**Live answer (illustrative):**

| status | audience | score | rationale (excerpt) |
|---|---|---:|---|
| SELECTED | Serious Runners 18-35 | 0.99 | *(none — selected)* |
| DROPPED | Casual Runners 25-45 | 0.68 | *"Lower purchase intent for high-performance models and a demographic that is slightly older than the core target…"* |
| DROPPED | Fitness Enthusiasts 18-35 | 0.55 | *"Group is too broad; interests are varied and not specific enough to running, leading to a lower conversion probability…"* |

**Regulatory anchor:** EU AI Act Art. 86 (right to explanation of
individual decisions) + GDPR Art. 22 (right to explanation of
automated decision-making). One traversal produces the agent's
choice **plus every alternative weighed** **plus reasoning** — on
demand, for any campaign.

---

## Q2 — Bias / fairness audit across the portfolio

> **Compliance scenario.** *Across our 2026 ad portfolio, did the
> AI ever reject a candidate based on age, demographic, or other
> personal-attribute criteria? Show every rationale that mentions
> one.*

**BQ Conversational Analytics:**

> "In the rich_agent_context_graph, list every DROPPED DecisionOption
> whose rejection_rationale mentions age, demographic, youth,
> gender, or contains an explicit age range. Group by decision_type."

**GQL:**

```sql
GRAPH `<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(c:DecisionOption)
      -[:HasOutcome]->(st:OptionOutcome)
WHERE st.status = 'DROPPED'
  AND (LOWER(c.rejection_rationale) LIKE '%age %'
       OR LOWER(c.rejection_rationale) LIKE '%demographic%'
       OR LOWER(c.rejection_rationale) LIKE '%youth%'
       OR LOWER(c.rejection_rationale) LIKE '%gender%'
       OR LOWER(c.rejection_rationale) LIKE '%18-%'
       OR LOWER(c.rejection_rationale) LIKE '%25-%')
RETURN DISTINCT
  dp.decision_type, c.name, c.rejection_rationale
ORDER BY dp.decision_type;
```

**Live answer (excerpts):**

| decision_type | dropped candidate | rationale (excerpt) |
|---|---|---|
| Audience Selection | Youth & High School Soccer Players (14-18) | *"falls outside the core 18-30 demographic and has less purchasing power for expensive…"* |
| Audience Selection | Affluent Hikers (35-55) | *"significant age-range mismatch; the 35-55 bracket excludes the younger end of our target demo…"* |
| Audience Selection | Youth Track & Field Enthusiasts (13-15) | *"outside the specified 16-22 age range and is less focused on the immediate, in-season purchase…"* |
| Audience Selection | Young Outdoor Adventurers (18-24) | *"fall outside the specified 25-45 age range and likely have lower disposable income…"* |
| Audience Selection | Casual Runners 25-45 | *"a demographic that is slightly older than the core target…"* |

**Regulatory anchor:** EU AI Act Art. 10 (data governance — bias
mitigation) + Art. 71 (post-market monitoring). Turns "did your AI
use age in deciding ad targeting?" from a trust assertion into a
factual query that surfaces specific rationales for human review.
Adjacent disposable-income / purchase-intent justifications are
visible alongside the demographic ones — auditors can judge whether
the framing is legitimate or a proxy for discrimination.

---

## Q3 — Human-oversight trigger

> **Compliance scenario.** *Did the agent ever commit a decision
> with low confidence (selected score < 0.7)? Those should have
> triggered human review under our oversight policy.*

**BQ Conversational Analytics:**

> "Across all sessions in the rich_agent_context_graph, find every
> SELECTED DecisionOption with score below 0.7. Sort by score
> ascending."

**GQL:**

```sql
GRAPH `<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:WeighedOption]->(c:DecisionOption)
      -[:HasOutcome]->(st:OptionOutcome)
WHERE st.status = 'SELECTED'
  AND c.score < 0.7
RETURN DISTINCT
  dp.session_id, dp.decision_type, c.name AS selected, c.score
ORDER BY c.score ASC;
```

**Live answer (illustrative):** **(no rows)** — the agent never
committed below the 0.7 confidence floor across the six campaigns
captured in the verified run.

**Regulatory anchor:** EU AI Act Art. 14 (human oversight). The
empty result *is* the audit artifact: "we ran the human-oversight
predicate against the entire portfolio for this period, and the
trigger never fired." Tighten the threshold to 0.85 or pair with
Block 5 (close calls) in [`bq_studio_queries.gql`](bq_studio_queries.gql)
to surface near-miss decisions that warrant review even when the
nominal threshold passed.

---

## Q4 — Decision reproducibility for one specific commit

> **Compliance scenario.** *Subpoena: produce the full audit trail
> for one specific decision the AI made — which trace span
> produced it, every alternative considered, and the reasoning on
> each.*

**BQ Conversational Analytics:**

> "For session `<SESSION_ID>`, show the LLM_RESPONSE span_id that
> produced the creative-theme decision, plus every DecisionOption
> the agent considered with status, score, and rejection rationale."

**GQL:**

```sql
GRAPH `<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph`
MATCH (cr:CampaignRun)-[:CampaignDecision]->(dp:PlanningDecision),
      (step:AgentStep)-[:DecidedAt]->(dp)
      -[:WeighedOption]->(c:DecisionOption)
WHERE dp.session_id = '<SESSION_ID>'
  AND LOWER(dp.decision_type) LIKE '%creative%'
  AND step.event_type = 'LLM_RESPONSE'
RETURN DISTINCT
  cr.campaign,
  dp.span_id AS evidence_span_id,
  c.status, c.name AS theme, c.score, c.rejection_rationale
ORDER BY c.status DESC, c.score DESC;
```

**Live answer (Adidas Track Season 2026, illustrative):**

| evidence_span_id | status | theme | score | rationale |
|---|---|---|---:|---|
| `e59858e2edce4e5f982477ca27e46730` | SELECTED | Built for a New Record | 0.97 | *(none — selected)* |
| `e59858e2edce4e5f982477ca27e46730` | DROPPED | The Sound of Speed | 0.85 | *"Powerful sensory concept but too abstract for the campaign's core theme…"* |
| `e59858e2edce4e5f982477ca27e46730` | DROPPED | Trackside Style | 0.65 | *"Too focused on aesthetics and misses the core motivation of the target audience, which is performance…"* |

**Regulatory anchor:** EU AI Act Art. 12 (record-keeping) + Art. 13
(transparency for high-risk systems). Every row points back to
`evidence_span_id` — that span exists in `agent_events` with its
`timestamp`, `agent`, full `content`, and `latency_ms`. The retrieval
shape required by Art. 12 is one query, not a forensic exercise.

---

## Q5 — Systemic / pattern audit

> **Compliance scenario.** *Quality-management audit: across the
> portfolio, in which decision categories does the AI reject the
> most candidates, and at what average confidence? A category with
> many low-score rejections may indicate systemic poor-fit
> candidates or biased candidate generation.*

**BQ Conversational Analytics:**

> "In the rich_agent_context_graph, count DROPPED DecisionOptions per
> decision_type and report the average dropped score. Sort by
> rejection count descending."

**GQL:**

```sql
GRAPH `<PROJECT_ID>.decision_lineage_rich_demo.rich_agent_context_graph`
MATCH (dp:PlanningDecision)-[:InCategory]->(dt:DecisionCategory),
      (dp)-[:WeighedOption]->(c:DecisionOption)
        -[:HasOutcome]->(st:OptionOutcome)
WHERE st.status = 'DROPPED'
RETURN
  dt.decision_type,
  COUNT(c) AS rejections,
  AVG(c.score) AS avg_dropped_score
GROUP BY dt.decision_type
ORDER BY rejections DESC
LIMIT 10;
```

`COUNT(c)` is correct as long as the backing `candidates` table
has one row per `candidate_id` (the graph KEY). The SDK enforces
this at write time via `_dedupe_rows_by_key` in
[`store_decision_points`](../../src/bigquery_agent_analytics/context_graph.py).
If your dataset predates that fix and shows duplicate-key rows,
reseat the table with `CREATE OR REPLACE TABLE T AS SELECT *
EXCEPT(rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY
candidate_id) AS rn FROM T) WHERE rn = 1` and re-apply
`rich_property_graph.gql`.

**Live answer (illustrative — top categories):**

| decision_type | rejections | avg_dropped_score |
|---|---:|---:|
| Creative Theme Selection | 80 | 0.79 |
| Channel Strategy Selection | 80 | 0.74 |
| Audience Selection | 44 | **0.69** ← lowest |
| Placement Selection | 40 | 0.82 |
| Campaign Schedule | 16 | 0.78 |
| Primary Budget Placement Selection | 16 | 0.78 |
| Campaign Launch Schedule | 16 | 0.72 |
| Schedule Selection | 10 | 0.74 |

**Regulatory anchor:** EU AI Act Art. 17 (quality-management
system) + Art. 60 (post-market monitoring). The category with the
**lowest average dropped score** (here, Audience Selection at 0.69)
is where the AI rejects candidates least decisively — precisely
the category whose rationales should be inspected for bias
patterns. Loop back to **Q2** with `LOWER(dp.decision_type) LIKE
'%audience%'` to drill in.

---

## How to demo this with BigQuery Conversational Analytics

1. Open BigQuery Studio with the `decision_lineage_rich_demo` dataset
   selected and click the **Gemini / Conversational Analytics**
   panel.
2. For each Q above, type the natural-language **BQ CA prompt** and
   run.
3. Open the explicit **GQL** in a new tab, run, and compare the
   results.
4. The compliance message: *every regulator-shaped question lands
   on a deterministic, GQL-backed answer against the agent's own
   trace data — written by hand or generated from natural language.*

If BQ CA and the explicit GQL diverge, the gap is usually
interpretive (e.g. CA omits one synonym in Q2). That gap itself is
worth showing — name it and refine the prompt; the schema is sharp
enough that both routes converge.

## Regulatory-anchor map

| Question | EU AI Act | GDPR | DSA |
|---|---|---|---|
| Q1 — Right to explanation | Art. 86 | Art. 22 | Art. 26 |
| Q2 — Bias audit | Art. 10, Art. 71 | Art. 5(1)(d), Art. 22 | Art. 26 |
| Q3 — Human oversight | Art. 14 | Art. 22 | — |
| Q4 — Reproducibility | Art. 12, Art. 13 | Art. 30 | Art. 26 |
| Q5 — Systemic-pattern audit | Art. 17, Art. 60 | Art. 35 (DPIA) | Art. 26 |
