# Demo Narration Transcript

## [OPENING]

A well-designed agent should learn from its own mistakes. 
That's the paradigm this demo implements: a continuous self-improvement cycle where the agent's real-world failures
become the training data for its next version.

For this demo we use a company policy **Q&A assistant**, built with **Google ADK** and the **BigQuery Agent Analytics Plugin**.

It's deliberately simple: a single LLM agent with just two tools:
- `lookup_company_policy(topic)` — retrieves detailed policy data on a set of topics such as PTO, sick leave, 
- expenses, benefits, and holidays.
- `get_current_date()` — returns today's date and day of the week, so the agent can answer date-relative questions.

The agent's job is to answer employee questions — "How many PTO days do I get?", "What's the meal reimbursement limit?",
"When is the next company holiday?", and so on.

The prompt tells the agent to "answer only from the knowledge above" and to say "I don't know, contact HR" for anything not listed. 
A common pattern to restrict the model to known facts to prevent hallucinations. 

We will run it through the improvement cycle and see how it performs.

---
## [High Level Overview of the Cycle Steps]

The improvement cycle goes through the following actions and I will go into the more details when we actually execute it.

1. Run the initial  **eval test cases**. Our ground truth and base for regression tests
2. Then we **generate** traffic. In production, these come from the real users; for the demo,
   we use Gemini to come up with the possible user questions and run them against the agent.
3. Every session along with its metadata (token usage, latency, request/response, tool usage, trajectories and many more) is being logged to BigQuery via BigQuery Analytics plugin
   We **evaluate** each session quality — for usefulness and grounding
4. * Then we work on **improving** the agent, by fine-tuning its instructions via the prompt optimizer.
5. * Finally, **measure** the improvement against fresh, unseen traffic — and iterate if needed.
6. At each evaluation step, the SDK's **deterministic evaluators** also check latency, token usage, and turn count — so we have an operational baseline from the start and can compare before and after.

---

## [GCP CLOUD SHELL - setup.sh]
> Navigate to [Quick Start](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/examples/agent_improvement_cycle#quick-start)

We will start from scratch in a new Google Cloud project. 

- We have repo checked out and navigated inside the `examples/agent_improvement_cycle` directory
- We have set the `Project ID` into environment variable using `export`
- We have run the setup script `./setup.sh` that:
  - checks Python and authentication, 
  - enables the BigQuery and Vertex AI APIs, 
  - installs dependencies,  
  - creates the initial prompt in the Vertex AI Prompt Registry and
  - Creates `.env` and updates `config.json` files that are input for the flow.

---

## [cat .env and cat config.json]

Here's the `.env` environment configuration created from our setup. 

And here is the `config.json` — the declarative interface. So you could later swap it with another agent of your own.

---

## [GCP CLOUD SHELL - run_cycle.sh]
> Navigate to [Solution](https://github.com/evekhm/BigQuery-Agent-Analytics-SDK/tree/feat/agent-improvement-cycle-demo/examples/agent_improvement_cycle#the-solution-learn-from-the-field)

We will trigger execution of the improvement cycle and we will catch up with it diving deeper into it steps.

### [STARTING PROMPT displayed]

Here's the V1 prompt. 
---

### [PRE-FLIGHT check]

An interesting detail here: the model actually calls `lookup_company_policy` for these questions even with V1's having a flaw in its prompt.
Not sure if everyone noticed it. Lets look at it again. The prompt actually discourages the agent to use its own tools and asks to rely only on the baked in information. 
However, because it mentions PTO, sick leave, and remote work by name in its inline knowledge it acts as a signal — the model recognizes the topic is valid and explores the available tools for more detail.
However as we will see later, it does not happen for the topics which are not baked into the prompt. And for those un-known subhects it would fallback to the "I do not know, contact HR", while its tools have all of the available information.

The model has no hint that the tool could help, so it obeys the refusal instruction.

---

### [STEP 1 — Generate Synthetic Traffic]

Gemini generates ten diverse employee questions — things like "Do I need a doctor's note for four sick days?" and "What are the core hours for remote work?" These are intentionally different from the three golden test cases. They cover all six policy topics, including the ones V1's prompt doesn't mention.

---

### [STEP 2 — Run Traffic Through Agent]

Step two sends those ten questions to the agent. Every session is logged to BigQuery through the BigQuery Agent Analytics Plugin. Watch the responses: for questions about topics mentioned in V1's prompt — like PTO rollover — the agent answers correctly. But for topics not in the prompt — parental leave, expenses, holidays — the agent says "I don't have that information, contact HR." It has the tools to answer, but the V1 prompt's "contact HR" instruction blocks it from even trying.

---

### [STEP 3 — Evaluate Session Quality]

Step three is where the SDK earns its keep. The quality report script reads those sessions back from BigQuery and an LLM judge scores each one. Four sessions are marked unhelpful — the agent deflected instead of using its tools. One is partial. Five are meaningful. The baseline score: fifty percent meaningful. That's our starting point.

Right below the quality score, the SDK's deterministic CodeEvaluator runs on the same sessions — average latency, total tokens per session,  turn count and error_rate. These are the operational baselines. No LLM needed, just math on the data already in BigQuery. We'll compare against these numbers after the improvement to make sure the new prompt didn't trade quality for cost.

---

### [STEP 4 — Improve Prompt]

Step four is the core of the cycle. Let's break it down.

#### 4a. Extract failed cases

First, the failed cases are extracted into the golden eval set. These become the regression gate — any future prompt must pass all eight. This is how the eval set grows organically from real failures rather than from what we imagined users would ask.

#### 4b. Generating ground truth via teacher agent

The Vertex AI Prompt Optimizer operates in `target_response` mode — it needs (input, expected_output) pairs to optimize against. We have the inputs (the failed questions) and the bad outputs, but we're missing the ground truth: what should the agent have responded?

Curating reference answers manually is impractical — these are questions we discovered through synthetic traffic, not ones we anticipated. We need to generate ground truth programmatically.

This is where the teacher agent comes in. The concept borrows from `knowledge distillation` in ML — a technique where a capable "teacher" model generates labeled outputs that are then used to train or optimize a "student" model. In classical distillation, the teacher is typically a larger, more expensive model and the student is smaller and cheaper. Here we adapt the idea: the teacher and student share the same model and the same tools — the difference is the prompt.
The teacher's prompt is generic tools first approach, but it is missing any possible domain knowledge, response formatting, vocabulary. 
Its sole purpose is to generate correct, tool-grounded outputs that serve as optimization targets. 
The Prompt Optimizer takes these targets and synthesizes a "production" prompt that captures the domain logic, tool mappings, and response patterns the agent needs.

In real life, the gap between teacher and student is typically more nuanced. The teacher model is more sophisticated, but the concept is the same,  


#### 4c. Optimize and validate

The triples — (question, V1 response, teacher response) — are passed to the Vertex AI Prompt Optimizer. 
The optimizer analyzes the gap between the bad and correct responses and generates an improved system instruction.

When the candidate prompt returns, the regression gate validates it against all eight golden eval cases. Every case must pass, or the Optimizer needs to re-try. 
The prompt then is promoted from V1 to V2.

---

## [STEP 5 — Measure Improvement]

Step five is the moment of truth. Ten fresh, never-before-seen questions are generated and run through the agent with the new V2 prompt. The quality report scores them from BigQuery.

We can see that previously failing type of questions are being answered now. 
The agent uses its tools and gives direct, grounded answers. No more "contact HR."

Ten out of ten sessions scored as helpful and grounded — in a single cycle.

And now the operational comparison — the same deterministic metrics we captured as a baseline in Step 3, run on the V2 sessions and shown side by side.
Latency stayed flat or improved — the V2 prompt routes to tools directly instead of deliberating. Token usage is comparable. 
Turn count unchanged. The improved prompt didn't trade quality for cost.


---

## [FINAL PROMPT displayed]

Here's the optimized V2 prompt. It explicitly requires tool usage for every question, provides a topic-mapping strategy so the agent knows to look up "benefits" when asked about 401k.
We have verified the new prompt against the test cases and also against the performance metrics.

---

## [DONE summary]

Let's recap what just happened:

1. We started with a **V1 prompt** that had an anti-hallucination pattern — "answer only from knowledge above, otherwise contact HR." The agent had tools with all the answers, but the prompt blocked it from using them on topics not baked in.

2. We ran **three golden eval cases** as a pre-flight — PTO, sick leave, remote work — all passed because those topics were mentioned in the prompt.

3. We **generated ten synthetic questions** covering all six policy topics and ran them through the agent. The agent deflected on expenses, benefits, and holidays — topics it could answer but the prompt told it not to try.

4. The **SDK's quality report** read those sessions from BigQuery and an LLM judge scored them. Baseline: roughly fifty percent meaningful. Right below, the **SDK's CodeEvaluator** established operational baselines — latency, tokens, turns, tool error rate — all from the same BigQuery data, no extra LLM calls.

5. We **extracted the failures** into the golden eval set — growing it from three to about eight cases. A **teacher agent** — same model, same tools, different prompt — generated ground truth for each failed question. The **Vertex AI Prompt Optimizer** used those triples to generate an improved prompt, and the **regression gate** validated it against all golden cases before promoting it to V2.

6. We ran **ten fresh questions** through V2. Quality went from fifty percent to one hundred percent. The **operational metrics comparison** confirmed latency, tokens, turns, and error rate all stayed within budget — the new prompt didn't trade quality for cost.

The golden eval set grew organically from real failures. The prompt was optimized automatically. And every metric — quality and operational — was measured from data already in BigQuery.

---

## [Reset and run again]

To reset everything back to V1 and start over:
```shell
./reset.sh
```

This reverts the prompt in the Vertex AI Prompt Registry to V1, restores the original three golden eval cases,
and clears reports. 

There are multiple options of how to run the flow:
```shell
./run_cycle.sh -h
```

```text
Options:
  --agent-config F   Path to agent's config.json
                     (default: config.json)
  --cycles N         Run N improvement cycles (default: 1)
  --no-auto          Always run all N cycles even if 100%
                     meaningful (default: stop early)
  --eval-only        Only run evaluation (Steps 1-3), skip improvement
  --app-name X       Override agent app name for BQ filtering
  --traffic-count N  Number of synthetic questions per cycle (default: 10)
  -h, --help         Show this help message
```

You can run again with multiple cycles to see iterative refinement:
```shell
./run_cycle.sh --cycles 3
```
By default, the cycle stops early once all synthetic traffic scores 100% meaningful. Each cycle generates fresh traffic, evaluates, improves, and measures. The golden eval set grows with each cycle as new edge cases are discovered.

---

## [CLOSING]

That's the agent improvement cycle. Capture sessions with the BigQuery Agent Analytics Plugin, evaluate quality with the SDK's LLM judge,
check operational metrics with the SDK's CodeEvaluator, optimize prompts with Vertex AI, and measure the results — all automated, all repeatable. 
The golden eval set grows with every cycle, so failures you discover today become regression tests for tomorrow.
