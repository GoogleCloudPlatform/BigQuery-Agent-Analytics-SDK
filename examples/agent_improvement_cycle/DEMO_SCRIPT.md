# Agent Improvement Cycle - Demo Script

**Duration:** ~6 minutes (single cycle), ~18 minutes (3 cycles)
**Format:** Live terminal walkthrough

---

## Introduction (30s)

A well-designed agent should learn from its own mistakes. This demo
implements that paradigm: a continuous self-improvement cycle where the
agent's real-world failures become the training data for its next
version.

The cycle uses the
[BigQuery Agent Analytics SDK](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK)
and the
[BigQuery Agent Analytics plugin for ADK](https://adk.dev/integrations/bigquery-agent-analytics/).
The agent runs, logs every session to BigQuery, evaluates its own
quality, and uses the **Vertex AI Prompt Optimizer** to fix what
failed. Prompts are versioned automatically in the **Vertex AI Prompt
Registry** with every improvement.

---

## The Demo Agent (30s)

The demo uses a company policy Q&A assistant, built with Google ADK.
It is deliberately simple: a single LLM agent with two tools:

- **`lookup_company_policy(topic)`** — retrieves detailed policy data
  on topics such as PTO, sick leave, expenses, benefits, and holidays.
- **`get_current_date()`** — returns today's date and day of the week,
  so the agent can answer date-relative questions.

The agent's job is to answer employee questions — "How many PTO days
do I get?", "What's the meal reimbursement limit?", "When is the next
company holiday?", and so on.

---

## High-Level Overview of the Cycle

The improvement cycle proceeds through five steps:

1. Run the initial **eval test cases** — the ground truth and base for
   regression tests.
2. **Generate traffic.** In production, this comes from real users; for
   the demo, Gemini generates diverse user questions and runs them
   against the agent.
3. Every session is logged to BigQuery via the BigQuery Agent Analytics
   plugin (token usage, latency, request/response, tool usage,
   trajectories). **Evaluate** each session for usefulness and
   grounding.
4. **Improve** the agent by fine-tuning its instructions via the Vertex
   AI Prompt Optimizer.
5. **Measure** the improvement against fresh, unseen traffic — and
   iterate if needed.

At each evaluation step (3 and 5), the SDK's **deterministic
evaluators** also check latency, token efficiency, and turn count.
Step 3 establishes the operational baseline; Step 5 shows the
before/after comparison to verify the improved prompt didn't regress
on cost or performance.

---

## Setup (1 min)

**Commands:**
```shell
export PROJECT_ID=my-project-id
./setup.sh
```

The setup script performs six checks:

1. **Python version:** Verifies Python 3.10+ is installed.
2. **Google Cloud auth:** Confirms `gcloud` is authenticated and a
   project is set.
3. **APIs:** Enables BigQuery and Vertex AI APIs if not already active.
4. **Dependencies:** Installs Python packages (`google-cloud-aiplatform`,
   `google-adk`, `google-genai`, `pandas`, `python-dotenv`).
5. **Configuration:** Creates the `.env` file with project ID,
   BigQuery dataset, and table name. Creates the BQ dataset if needed.
6. **Vertex AI prompt:** Creates the V1 prompt in the Vertex AI Prompt
   Registry and writes the prompt ID to `.env` and `config.json`.

---

## Show the Config (30s)

**Commands:**
```shell
cat .env
cat config.json
```

The `.env` file contains the environment configuration created by
setup. The `config.json` is the declarative interface that drives the
entire cycle. Key fields:

- `prompt_storage: "vertex"` — the prompt lives in Vertex AI, not a
  local file
- `use_vertex_optimizer: true` — improvements use the Vertex AI Prompt
  Optimizer with synthetic ground truth from a teacher model
- `vertex_prompt_id` — the Vertex AI prompt resource (auto-filled by
  setup)

The same config works for any ADK agent. Swap the `agent_module` and
paths to point at a different agent.

---

## Show the V1 Prompt (30s)

**Command:**
```shell
cat agent/prompts.py
```

The V1 seed prompt instructs the agent to "answer only from the
knowledge above" and to say "I don't know, contact HR" for anything
not listed. This is a common pattern to restrict the model to known
facts and prevent hallucinations.

The prompt mentions PTO, sick leave, and remote work by name in its
inline knowledge section, but says nothing about expenses, holidays,
or parental leave — topics the agent's tools can answer.

The agent reads this prompt from the Vertex AI Prompt Registry at
startup (not from the file directly).

---

## Show the Golden Eval Set (30s)

**Command:**
```shell
cat eval/eval_cases.json
```

The golden eval set is the regression gate. It starts with three cases
that the V1 prompt handles correctly: PTO days, sick leave, and remote
work. The golden set starts small and grows each cycle as failed
synthetic cases are extracted into it.

No prompt change is accepted unless every golden case still passes.

---

## Run One Cycle (~6 min)

**Command:**
```shell
./run_cycle.sh
```

### Pre-flight: Golden Eval (~25s)

The script starts by running the golden eval set against the current
prompt. All 3 cases should pass with V1.

An interesting detail: the model calls `lookup_company_policy` for
these questions even with V1's prompt. Because the prompt mentions
PTO, sick leave, and remote work by name in its inline knowledge, the
model recognizes these topics as valid and explores the available tools
for more detail. As the synthetic traffic will reveal, this does not
happen for topics that are not mentioned in the prompt.

### Step 1: Generate Synthetic Traffic (~20s)

Gemini generates 10 diverse employee questions — varied phrasing,
situational questions, covering all six policy topics. These are
intentionally different from the golden set and simulate real-world
traffic the agent has not been tuned for.

### Step 2: Run Traffic Through Agent (~30-40s)

The generated questions are sent to the agent using ADK's
`InMemoryRunner`. Every session is automatically logged to BigQuery by
the BigQuery Agent Analytics plugin. The full trace is captured: user
question, tool calls, LLM responses. No extra logging code required.

Observe the responses: for questions about topics mentioned in V1's
prompt — like PTO rollover — the agent answers correctly. But for
topics not in the prompt — parental leave, expenses, holidays — the
agent says "I don't have that information, contact HR." It has the
tools to answer, but the V1 prompt's "contact HR" instruction blocks
it from even trying.

This is where the anti-hallucination pattern reveals its downside.
The instruction to "answer only from knowledge above" prevents
hallucination for known topics, but also prevents the agent from
discovering answers through its own tools for topics not baked into
the prompt. The model has no hint that the tool could help, so it
obeys the refusal instruction.

### Step 3: Evaluate Quality (~25s)

The SDK's quality report reads those sessions back from BigQuery and
an LLM judge scores each one on two dimensions:

- **Response usefulness:** Was the answer meaningful, partial, or
  unhelpful?
- **Task grounding:** Was the answer based on tool output, or did the
  agent fabricate information?

The baseline score lands around 50% meaningful. Several sessions are
marked unhelpful — the agent deflected instead of using its tools.

Below the quality score, the SDK's deterministic `CodeEvaluator` runs
on the same sessions — average latency, total tokens per session, and
turn count. These are the **operational baselines**. No LLM calls
needed, just math on the data already in BigQuery. These numbers will
be compared against V2 sessions after the improvement.

### Step 4: Improve Prompt (~1-2 min)

This is the core of the cycle.

#### 4a. Extract Failed Cases

Failed cases from the quality report are extracted into the golden
eval set, growing it from 3 to ~8 cases. These become the regression
gate — any future prompt must pass all of them. This is how the eval
set grows organically from real failures rather than from what was
imagined users would ask.

#### 4b. Generate Ground Truth via Teacher Agent

The Vertex AI Prompt Optimizer operates in `target_response` mode — it
needs (input, expected_output) pairs to optimize against. The failed
questions provide the inputs and the bad outputs, but the ground truth
is missing: what should the agent have responded?

Curating reference answers manually is impractical — these are
questions discovered through synthetic traffic, not ones anticipated
in advance. Ground truth must be generated programmatically.

This is where the **teacher agent** comes in. The concept borrows from
**knowledge distillation** in ML — a technique where a capable
"teacher" model generates labeled outputs that are then used to train
or optimize a "student" model. In classical distillation, the teacher
is typically a larger, more expensive model and the student is smaller
and cheaper. Here the idea is adapted: the teacher and student share
the same model and the same tools — the difference is the prompt. The
V1 prompt constrains the agent to inline knowledge and blocks tool
usage. The teacher's prompt removes that constraint and explicitly
mandates tool usage for every query. The teacher produces correct,
tool-grounded outputs that serve as the labeled data the optimizer
needs.

The teacher is instantiated from the same `agent_factory` with the
same model and the same tools — only the system instruction differs.

The result: for each failed question, the V1 response ("contact HR")
is shown alongside the teacher's response (a tool-grounded, factual
answer). These pairs become the optimization signal.

Note that the teacher prompt is deliberately minimal — "always call
tools, never defer." It is not a production-grade prompt. Its sole
purpose is to generate correct, tool-grounded outputs that serve as
optimization targets. The Prompt Optimizer takes these targets and
synthesizes a production prompt that captures the domain logic, tool
mappings, and response patterns the agent needs.

**Production note:** This demo is a simplification. The teacher works
trivially here — the failure mode is obvious (the prompt blocks tool
usage), and removing that constraint is enough to produce correct
outputs. In production, the gap between teacher and student is
typically more nuanced. The agent might have complex multi-tool
routing across dozens of tools, domain-specific reasoning chains, or
failure modes that cannot be fixed by a single generic instruction.
The teacher itself may need to be a more capable model, use curated
few-shot examples, or be specialized per failure category. The
architecture — capture failures, generate ground truth, optimize,
validate — remains the same. What changes is the sophistication of
each component.

#### 4c. Optimize and Validate

The triples — (question, V1 response, teacher response) — are passed
to the Vertex AI Prompt Optimizer. The optimizer analyzes the gap
between the bad and correct responses and generates an improved system
instruction.

When the candidate prompt returns, the regression gate validates it
against all golden eval cases. Every case must pass. The prompt is
promoted from V1 to V2.

### Step 5: Measure Improvement (~2-3 min)

Step 5 mirrors Steps 1–3 but with the improved prompt:

1. **Fresh traffic:** Gemini generates a NEW batch of 10 questions.
   Re-running the Step 1 traffic would be circular — the prompt was
   specifically fixed to handle those questions.

2. **Run through agent:** The fresh questions are sent to the V2
   agent and logged to BigQuery — exactly like Step 2.

3. **Score from BigQuery:** The SDK's quality report reads the new
   sessions from BigQuery and scores them — exactly like Step 3.

Observe the responses: questions about vision coverage, parental leave
for secondary caregivers, and the company holiday list now receive
direct, tool-grounded answers. No more "contact HR."

```
  Before (V1):   50% meaningful  ( 5/10 sessions)
  After  (V2):  100% meaningful (10/10 sessions)
```

From 50% to 100% in one automated cycle, scored from BigQuery on
entirely new questions.

Right below the quality results, the operational metrics comparison
appears — the same deterministic evaluators from the Step 3 baseline,
now run on the V2 sessions and shown side by side:

```
  Metric                    V1              V2          Budget    Status
  ──────────────────  ────  ──────────────  ──────────────  ──────────────  ────────
  Avg latency            ↓    3200.0 ms       2800.0 ms     10000 ms        PASS
  Total tokens           ↓    4500 tokens     3200 tokens   50000 tokens    PASS
  Turn count             =    2 turns         2 turns       10 turns        PASS
```

This verifies the improved prompt didn't trade quality for cost. A
prompt that makes the agent chattier, triggers more retries, or
produces longer responses would show up here even if quality scored
100%. These are the same `CodeEvaluator` metrics that power the
`bq-agent-sdk evaluate` CLI command — the one described in the blog
post
*[Your Agent Events Table Is Also a Test Suite](https://medium.com/google-cloud/your-agent-events-table-is-also-a-test-suite-999fbef885ed)*.
In the demo they serve as a post-improvement sanity check; in CI they
become a hard gate on every PR.

---

## Show the V2 Prompt (30s)

**Command:**
```shell
cat agent/prompts.py  # or view in Vertex AI console
```

Compare the optimized V2 prompt to V1: it explicitly requires tool
usage for every question, provides a topic-mapping strategy so the
agent knows to look up "benefits" when asked about 401k, and includes
a critical rule — never say "I don't have the information," always use
the tools first.

This was not hand-written. It was generated by the Vertex AI Prompt
Optimizer, validated by the regression gate, and measured against
fresh traffic.

---

## Multi-Cycle Run (optional, ~18 min)

To show the full loop with prompt refinement across cycles:

```shell
./reset.sh
./run_cycle.sh --cycles 3
```

Each cycle generates fresh synthetic traffic, evaluates, improves, and
measures. The golden eval set grows with each cycle as new edge cases
are discovered and locked in.

---

## Wrap-Up (30s)

The full cycle completed in about six minutes. The golden eval set
grew from 3 to ~8 cases, and all artifacts — quality reports,
synthetic traffic, and ground truth — are saved for inspection.

The key idea: capture sessions with the BigQuery Agent Analytics
plugin, evaluate quality with the SDK, optimize prompts with Vertex
AI, and measure the results — all automated, all repeatable. The
golden eval set grows with every cycle, so failures discovered today
become regression tests for tomorrow.

To reset and run again:
```shell
./reset.sh
```
