# Decision Lineage Demo — 5-Minute Leadership Narrative

**Audience.** VP-level stakeholders across product, ops, legal, and
risk. Goal: show that the EU compliance posture for an agent
platform — under the **EU AI Act**, **GDPR**, and **DSA** — is a
queryable BigQuery artifact, not a slide deck.

**Surface.** BigQuery Studio. Two windows side-by-side:

1. The Studio query editor (or the Gemini / Conversational
   Analytics panel for the 5 questions).
2. `bq_studio_queries.gql` and `DEMO_QUESTIONS.md` open in an
   editor so you can copy each block.

**Time.** 5:00 hard. The script below totals ~5:00 with breathing
room; the actual queries each run in 1-3s against the prebuilt
graph.

---

## Pre-flight (do once, not on stage)

```bash
cd examples/decision_lineage_demo
./setup.sh
```

This runs the live ADK agent against six campaign briefs, has the
SDK extract decisions from the resulting traces, and renders the
GQL bundle. ~5-10 minutes total. See **README.md → Setup** for
prerequisites and override env vars.

When setup finishes, confirm:

- `Sessions: 6 succeeded, 0 failed`
- `property_graph_created   True`
- `decision_points_count   <non-zero>`

Open the rendered queries:

```bash
open bq_studio_queries.gql
open DEMO_QUESTIONS.md
```

---

## Section 1 — The opening (~45s)

> "The EU AI Act took full effect this year. For automated systems
> that decide who sees an ad, who gets shown a price, what
> creative gets served — three things are now non-negotiable:
> every decision must be **explainable** on demand; **bias**
> patterns must be detectable post-market; and decisions made with
> **low confidence** must be subject to human oversight. Skipping
> any of these isn't a paperwork problem — fines run up to seven
> percent of global revenue.
>
> Most agent demos produce a recommendation and ask you to trust
> it. We did something different. We took our media-planner
> agent — the one that picks audience, budget, creative, channel,
> and launch date for every campaign — wired its trace data into
> a BigQuery Property Graph, and turned regulator-shaped questions
> into single GQL queries.
>
> In the next four minutes you'll see five questions, each shaped
> by a specific EU AI Act, GDPR, or DSA article, answered live
> against six recent campaign runs."

---

## Section 2 — What's behind the curtain (~45s)

> "Six campaigns. Nike Summer Run, Nike Winter Trail, Adidas Track
> Season, Puma Soccer Cup, Reebok CrossFit Open, Lululemon Yoga
> Flow. Each was a real run of our media-planner agent on Gemini
> 2.5 Pro — different brand, budget, audience, season.
>
> The **BigQuery Agent Analytics Plugin** captured every span the
> agent produced. Twenty-seven events per session, one-sixty-two
> total. Then a single `AI.GENERATE` call extracted the decisions
> the agent made and every alternative it considered, with the
> rejection rationale on each dropped option. Every node in the
> graph you're about to see traces back to a real plugin-written
> span."

**[Open BigQuery Studio. Run Block 2 from `bq_studio_queries.gql`.
Click the Graph tab.]**

> "This is one campaign visualized. Five fan-outs — one per
> decision the agent made. Center of each fan-out is the decision.
> Right side is every option the agent weighed: green for selected,
> red for dropped, the dropped ones carrying the agent's rejection
> reasoning right on the node properties. **This is the audit
> surface.** From here, every regulator question we ask becomes a
> graph traversal."

---

## Section 3 — The five questions (~3:15 total, ~40s each)

Open `DEMO_QUESTIONS.md` in the side editor. For each Q, paste the
explicit GQL into BQ Studio (or use the Gemini / Conversational
Analytics panel and the natural-language prompt — your choice).

### Q1 — Right to explanation (40s)

**[Paste and run Q1 from `DEMO_QUESTIONS.md`.]**

> "Question one. A regulator — or under GDPR Article 22, an
> affected user — asks: 'why did your AI pick the audience it
> picked for Nike Summer Run?'
>
> Three rows. Selected: Serious Runners 18-35 at 0.99 confidence.
> Two alternatives the agent dropped: Casual Runners 25-45 because
> of lower purchase intent on high-performance footwear, Fitness
> Enthusiasts 18-35 because the group was too broad for running
> conversion. **Every reason is in the agent's own words. One
> query. Zero forensic effort.**
>
> EU AI Act Article 86, GDPR Article 22 — both satisfied with the
> data we already have."

### Q2 — Bias audit (40s)

**[Paste and run Q2.]**

> "Question two. EU AI Act Article 10 says: regulators can demand
> evidence that we monitor for bias. The question they ask: 'has
> your AI ever rejected an audience based on age or demographic
> criteria?'
>
> Multiple matches. Notice the rationales — the agent doesn't just
> cite age, it blends age with purchase intent and brand
> alignment. That's the level of detail an auditor needs to judge
> whether the framing is **legitimate ad targeting** — which is
> permitted under DSA Article 26 — or a **proxy for protected
> attributes** — which is not. We're not arguing trust. We're
> handing over the data."

### Q3 — Human oversight (40s)

**[Paste and run Q3.]**

> "Question three. Article 14 requires high-risk AI systems to
> support human oversight. Our internal policy: any decision
> committed below 0.7 confidence triggers human review.
>
> Empty result. Zero rows. Across thirty-plus decisions in six
> campaigns this period, the agent never committed below the
> floor.
>
> **That empty result is the audit artifact.** It's what we hand
> to a regulator to prove the policy is enforced. And the day
> someone tightens the threshold to 0.85 — same query, new
> answers, instantly."

### Q4 — Reproducibility (40s)

**[Paste and run Q4.]**

> "Question four. EU AI Act Article 12 requires high-risk AI
> systems to keep records that reproduce any individual decision.
> Subpoena scenario: 'show us the full audit trail for the Adidas
> creative-theme decision.'
>
> Three rows. One LLM_RESPONSE span — that span ID right there —
> produced the decision. The selected theme. Two alternatives
> weighed. Each with the agent's specific rationale. That span
> lives in `agent_events` with its full content, timestamp, and
> latency_ms profile.
>
> **Article 12 record-keeping turned into one query, not a
> forensic project.**"

### Q5 — Systemic-pattern audit (40s)

**[Paste and run Q5.]**

> "Question five. Articles 17 and 60 — quality management and
> post-market monitoring. The question: 'where in our portfolio
> does the AI reject candidates least decisively?'
>
> Audience Selection. Forty-four rejections at average confidence
> 0.69 — lowest in the portfolio. That's the category to look at
> first for systemic patterns.
>
> Loop back to Q2 with this filter and you have a continuous
> bias-monitoring **loop**, not a one-off audit. That's what
> Article 60 post-market monitoring is asking for."

---

## Section 4 — The close (~30s)

> "Three takeaways.
>
> **One.** The EU compliance posture for our agent platform is now
> a queryable artifact. Not a slide deck. Not a quarterly review.
> A live BigQuery query, on demand.
>
> **Two.** This generalizes. The schema is brand-neutral,
> channel-neutral, and decision-type-neutral. Drop any agent's
> traces in and the same five questions answer the same way.
>
> **Three.** This composes with what we already run. The BQ AA
> Plugin is on our prod path. The SDK extraction is one method
> call. BigQuery is our data warehouse. We didn't need a new
> platform — we made the one we have answer regulator-shaped
> questions.
>
> Open for questions."

---

## Anticipated VP-level Q&A

| If they ask | Short answer |
|---|---|
| "How long to deploy this?" | The plugin is already on our agent path. The SDK pipeline is `mgr.build_context_graph(...)` — one method. The DDL is committed; CI runs it. We're talking days, not quarters. |
| "What about agents we haven't instrumented yet?" | Same plugin, same plug-in point. The graph schema doesn't care which agent wrote the spans, only that the BQ AA Plugin wrote them. |
| "Cost?" | Two `AI.GENERATE` calls per build run, plus standard BQ query cost. A handful of cents per build for our scale. |
| "What happens when AI.GENERATE misses a decision?" | The build script reports per-session counts. If a session is thin we re-run extraction (no need to re-run the agent — traces are already in `agent_events`). The narration is count-agnostic for exactly this reason. |
| "Does this expose PII?" | The graph stores the **agent's own reasoning text** about candidates. Personal data handling is governed by the underlying `agent_events` retention policy, which we already control. |
| "Who owns operating this?" | Same team that owns the agent platform. The plugin write path and the extraction read path both already have on-call coverage. |

## If a query doesn't render

| Symptom | Recovery |
|---|---|
| Block 2 has no Graph tab | Re-run; the tab takes a beat to appear after first execution. |
| Q3 returns rows when you expected empty | Threshold has been crossed; that *is* the audit signal — flag for review. |
| AI.GENERATE has been flaky and counts look thin | Skip ahead to Q3 (negative result) and Q4 (single-decision reproducibility) — they degrade gracefully. |
| BQ Conversational Analytics generates a different query | Compare to the explicit GQL in `DEMO_QUESTIONS.md`. The gap is interpretive ("did CA include all synonyms?") and itself worth narrating. |
