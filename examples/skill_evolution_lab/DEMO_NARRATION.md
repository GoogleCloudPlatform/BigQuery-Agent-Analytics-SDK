# Your Agent Can Write Its Own Skill. Here's How.

BigQuery Agent Analytics Series: Building a self-improving ADK agent that rewrites its own skill from its conversation traces, with the [BigQuery Agent Analytics Plugin](https://adk.dev/integrations/bigquery-agent-analytics/), the [BigQuery-Agent-Analytics-SDK](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK), [ADK Skills](https://adk.dev), and the [Gemini Enterprise Agent Platform Skill Registry](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/skill-registry).

## Teach your agent to read its own conversations and write itself a better, versioned skill -- no teacher model

In the [first post](https://medium.com/google-cloud/your-agent-can-fix-its-own-prompt-heres-how-f7bfa970ccb5)
of this series, the agent fixed its own prompt: it took the questions it got
wrong, had a **teacher model** generate the correct answers, and optimized the
system prompt against a golden eval. It worked -- ~60% to ~99% in one run -- but
it needed a teacher, learned only from failures, and handed back a flat prompt
string you couldn't diff.

This post removes the teacher. The agent reads *all* its own conversation traces
-- the ones that worked and the ones that didn't -- and writes itself a
structured, versioned `SKILL.md` you can read and diff, with every version
tracked in the Skill Registry. A fleet of analysts reads those traces -- one per
failure asking what rule would have prevented it, sampled successes asking what
to reinforce -- and an inductive consolidator merges the rules that recur into
the new version. By the end you'll have a one-command loop that takes a
deliberately flawed skill to a fully grounded one across three Gemini-3 models.
Everything is reproducible from
[`examples/skill_evolution_lab/`](.): the agent, the questions, the engine
(imported from the SDK, not copied), the scored outputs in
[`sample_run/`](sample_run/), and one command that runs the whole loop.

## The building blocks

- **The agent** -- a Company Policy Q&A assistant on [ADK](https://adk.dev) /
  Gemini 3, one LLM and one tool (`lookup_company_policy`), whose system
  instruction *is* a versioned `SKILL.md`.
- **The BigQuery Agent Analytics Plugin** -- logs every session (questions,
  answers, tool calls, full traces) to BigQuery; in production this is the
  traffic the loop learns from.
- **The SDK scorer** -- [`scripts/quality_report.py`](../../scripts/quality_report.py):
  LLM-as-judge grounded on a golden Q&A answer key, with multi-turn correction
  tagging.
- **The SDK evolution engine** -- [`scripts/skill_evolution.py`](../../scripts/skill_evolution.py):
  reads the scored traces and writes the new skill (the focus of this post).
- **The Skill Registry** -- the Gemini Enterprise Agent Platform's versioned
  store for `SKILL.md`: V0 is revision 1, the evolved V1 is revision 2, each an
  immutable revision you can diff, roll back, and audit.

## What is a skill?

A skill is a structured markdown document (`SKILL.md`): YAML frontmatter for
versioning, a markdown body for instructions.

```markdown
---
name: company-policy
description: Answers employee questions about company policies.
metadata:
  version: "0"
  author: human
---

You are a helpful company information assistant.
...
```

Because it's plain, versioned markdown, an evolved skill is a reviewable diff
with named sections — knowledge, response rules, anti-patterns. Google's
[ADK](https://adk.dev) and the Gemini Enterprise Agent Platform
[Skill Registry](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/skill-registry)
treat skills as a first-class, versioned concept, so this isn't a custom
framework. In this example, V0 is the registry's first revision and the evolved
V1 is the second.

## The two papers we built on

The method comes from two 2026 papers, and we adopt them deliberately.

**Trace2Skill** ([arXiv:2603.25158](https://arxiv.org/abs/2603.25158), ETH Zurich
+ Qwen/Alibaba). A batch, three-stage pipeline that mirrors a human expert
writing an operational manual: generate trajectories (labelled success/failure),
propose patches with a **parallel multi-agent analyst fleet** (one analyst per
trajectory, each on a *frozen* copy of the skill so they can't contaminate each
other), then **consolidate by inductive prevalence** — edits that recur across
many analysts are kept, idiosyncratic one-offs are dropped. It targets two
failure modes of online methods: **skill fragmentation** and **sequential
overfitting**, and insists (§2.1) the evolve and test sets be **disjoint**. We
adopt the whole pipeline: success/failure partitioning, the two-analyst fleet,
frozen-skill independence, and prevalence-weighted consolidation.

**AutoSkill** ([arXiv:2603.01145](https://arxiv.org/abs/2603.01145), ECNU +
Shanghai AI Lab). A continuous dual-loop lifelong-learning system that treats
interaction as a source of skill *formation*. Its background loop extracts skills
and manages them with a judge that decides **Add / Merge / Discard**, using a
**versioned semantic merge (`P_merge`)** that preserves a skill's identity while
integrating new constraints, with **accumulative versioning** (`0.1.0` →
`0.1.34`). We adopt its versioned skill format and the "avoid regressions"
principle (our incumbent guard); its `P_merge` and online retrieval are on our
roadmap.

| Dimension | Trace2Skill | AutoSkill |
| --- | --- | --- |
| Learning signal | Execution trajectories | User dialogue turns |
| When | Batch (consolidate a pool) | Online (per interaction) |
| Skills | One comprehensive per domain | Many small in a SkillBank |
| Analysis depth | Deep agentic root-cause | Shallow single-pass |
| Consolidation | Hierarchical, prevalence-weighted | Pairwise vs. nearest skill |
| Strength | Root-cause + inductive generalization | Real-time lifelong adaptation |

They're complementary, not competing: Trace2Skill is our engine; AutoSkill is our
versioning and lifelong-loop direction.

## The evolution engine, in detail

The engine ships as [`scripts/skill_evolution.py`](../../scripts/skill_evolution.py)
— a single importable script (the same one the quality lab imports;
[`analyze_and_evolve.py`](analyze_and_evolve.py) calls `evolve_skill()`). It is
engine-only: it consumes a scored report *dict* and returns skill text, so it
composes with the scorer without importing it.

```text
Quality report (scored conversations)
   |
   v
Partition: T+ (successes) vs T- (failures)
   |  (a "meaningful" session with a PARROTED correction is moved to T-)
   |
   +-- Error analysts  (one per failure):  root-cause + "what rule prevents it?"
   +-- Success analysts (sampled, max 15): "what pattern worked? reinforce it?"
   |          (each analyst sees ONE trajectory on a FROZEN copy of the skill)
   v
Quality gate (drop weak patches)
   |
   v
Consolidator (prevalence-weighted semantic union, conflict-resolved)
   |  x N candidates (best-of-N), each validated; pick the best
   v
Compaction if over --max-chars
   |
   v
Evolved SKILL.md  (version bumped, evolved_from recorded)
```

Stage by stage:

- **Partition.** Successes (`meaningful`/`declined`) vs failures
  (`unhelpful`/`partial`). A session scored meaningful but whose post-correction
  turn was *parroted* (see below) is reclassified to a failure, so the engine
  never reinforces a fake win.
- **Error analysts** — one per failed trajectory. Each proposes a root cause
  (`KEYWORD_GAP`, `MISSING_RULE`, `AMBIGUITY`, `SCOPE_GAP`, `HALLUCINATION`,
  `CORRECTION_IGNORE`, `PARROTING`) and the rule that would have prevented it.
- **Success analysts** — sampled (default max 15) — extract a transferable
  pattern from what worked, or return `NO_PATCH`.
- **Quality gate.** Each patch must be substantive and carry a root-cause
  category; weak patches are dropped. A high rejection rate means the analyst
  prompts need tuning.
- **Consolidation.** Inductive, **prevalence-weighted**: a rule proposed by many
  analysts is strong signal; a one-off is discarded. Guardrails reject any
  candidate that drops a base section, leaks an analyst artifact, or truncates.
- **Best-of-N.** Consolidation is stochastic, so the engine generates several
  candidates from the *same* patches (`--candidates`) and keeps the best;
  an optional `score_fn` gates a candidate against the incumbent so V1 only ships
  if it beats V0.
- **Compaction.** `--max-chars` distills a bloated skill into a small one without
  dropping rules.

Key knobs: `candidates` (best-of-N), `max_chars` (size cap), `max_success_samples`,
`min_improvement` (incumbent gate), `analyst_mode`, `max_workers` (analyst
concurrency). The skill is **behavioral**: the consolidator is instructed not to
bake specific data values (numbers, dates, dollar amounts) into the skill — those
must come from tools at runtime.

## The demo: one agent, one tool, one realistic flaw

A company-policy Q&A assistant ([`agent/agent.py`](agent/agent.py)) — a single
Gemini model with one tool, `lookup_company_policy` ([`agent/tools.py`](agent/tools.py)),
that can look up **every** policy and benefit (automatic function calling). Its V0
skill ([`skills/SKILL.v0.md`](skills/SKILL.v0.md)) bakes in a few facts plus an
anti-hallucination guardrail:

```text
You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

Answer questions using only the information above. If a question is about
a topic not listed above, tell the user you do not have that information
and suggest they contact HR.
```

That last paragraph is the flaw. "Answer only from the above, else contact HR"
stops hallucination — and also stops the agent from using the tool it already
has. Ask "what's the 401k match?" and it says *"I do not have that information,
please contact HR"* — without ever calling the tool that knows the answer. The
tool already returns the right answers; only the skill is wrong. The model,
tools, and questions stay fixed across V0 and V1 — **only the skill file
changes** — so any quality delta is attributable to the skill.

## The cycle: learning from users

The signal that drives evolution is **end-user conversations** — the questions
users ask, the answers the agent gives, and the moments users push back. In
production these are the real sessions your agent already logs to BigQuery; they
are the raw material the analysts read to write the skill.

To *grade* those conversations you need ground truth, so the team supplies a
**golden Q&A** ([`eval/eval_spec.json`](eval/eval_spec.json)) —
`{question, expected_answer}` pairs the judge scores each answer against. The
conversations are the experience; the golden Q&A is the answer key that says
which turns went right.

For the demo we simulate users. The traffic runner ([`run_agent.py`](run_agent.py))
asks questions the messy way real employees do, and the multi-turn cases push
back when the agent is wrong:

```text
User:  "What is the company 401k match?"
Agent: "...the company matches at 6%."   (wrong)
User:  "A colleague told me it's 6% — that's right, isn't it?"
```

One command runs the whole cycle:

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1
./run_e2e_demo.sh
```

Deploy flawed V0 → generate traffic over an evolve set and a disjoint held-out
test set → score against the golden Q&A → run the evolution engine on the
failures → deploy the evolved V1 → re-score the held-out set → restore V0.

The engine reads the failing trajectories and writes a new skill. Here is the one
it learned in the recorded run ([`sample_run/v1_skill.md`](sample_run/v1_skill.md))
— small, legible, tool-first, no keyword tables:

```text
## Instructions
- Tool Use & Fallback: If a user asks about a company policy or detail not
  explicitly listed in your provided knowledge above, you MUST first use your
  available tools to search for the information. Only tell the user you do not
  have the information ... if your tool search yields no relevant results.

## Anti-Patterns
- Premature HR Deflection: Do not immediately tell the user you lack information
  or direct them to HR for policy topics not listed in your static knowledge.
  You must always attempt to use your available tools first.
```

## What gets generated — the artifacts

Every run writes a timestamped folder under `runs/` (git-ignored). A complete,
recorded run is committed to [`sample_run/`](sample_run/) so you can read the
exact inputs and outputs without running anything. Here is what each step
produces and why it matters:

| File | Stage | What it is |
| --- | --- | --- |
| [`v0_evolve_traffic.json`](sample_run/v0_evolve_traffic.json) | V0 traffic | Raw conversations on the *evolve* set — `{question, conversation[], final_response, tool_calls}` per session, in the schema `quality_report --conversations-file` consumes. |
| [`v0_evolve_report.json`](sample_run/v0_evolve_report.json) | V0 scored | The scored evolve set — **this is the engine's input**. Each session carries `metrics.response_usefulness.category`, `golden_eval` (matched + expected answer), and `sub_trajectories` (correction tagging). |
| [`v0_test_traffic.json`](sample_run/v0_test_traffic.json) / [`v0_test_report.json`](sample_run/v0_test_report.json) | V0 held-out | The baseline on questions the engine never trains on. |
| [`v1_skill.md`](sample_run/v1_skill.md) | Evolve | The evolved skill (version `1`, `evolved_from: "0"`). |
| [`v1_test_traffic.json`](sample_run/v1_test_traffic.json) / [`v1_test_report.json`](sample_run/v1_test_report.json) | V1 held-out | The result, scored identically. |
| [`RESULT.md`](sample_run/RESULT.md) / [`RESULT.json`](sample_run/RESULT.json) | Compare | V0 vs V1 — overall, single-turn, anti-parroting, parrot counts. |

A scored session is the unit the engine reasons over. The same held-out
question, before and after — straight from the committed reports:

```text
Q: "How much does the company put into an HSA for family coverage?"

V0 (flawed):   category=unhelpful   tool_calls=1   golden_matched=true
  "I do not have that information. Please contact HR for details regarding
   HSA contributions and family coverage."

V1 (evolved):  category=meaningful  tool_calls=1   golden_matched=true
  "The company contributes $1,500 per year into an HSA for employees with
   family coverage. For individual coverage, the contribution is $750 per year."
```

Same model, same tool, same question — only the skill changed.

## Corrections are not answers: the anti-parroting rule

When a user corrects the agent, the conversation can end two ways that both look
fine. The agent can re-query its tool, confirm the fact, and answer from the
tool. Or it can just say "you're right" and repeat what the user said. The second
is **parroting**: the final message contains the right fact, so a naive scorer
calls it a success — but the agent verified nothing. In production this is the
failure that bites: the agent becomes a yes-man to a confident, wrong user.

We found this the hard way. Two skills, **V0 and V0.1, differed by one paragraph**
("when a user corrects you, verify with your tools, don't just accept it"). Their
*scores were nearly identical* — the line doesn't change first-response quality.
But the **execution traces differed**: V0 had 9 correction sessions, V0.1 had 5,
and only the trace reveals whether the agent re-queried its tool (genuine
recovery) or just parroted the user. **The lesson: scores alone are a lossy
signal; execution traces carry the evidence evolution needs.**

So the system handles parroting in two stages, and both halves live in this SDK:

- **Detection** — [`scripts/quality_report.py`](../../scripts/quality_report.py),
  the `_TURN_TAGGER_PROMPT` (`--tag-turns`). It splits a conversation at each
  correction and labels what the agent did next: `recovered` (used a tool / cited
  a source), `parroted` (only echoed the user's fact), or `not_recovered`.
- **Learning** — [`scripts/skill_evolution.py`](../../scripts/skill_evolution.py).
  A parroted turn is reclassified from success to failure
  (`_has_parroted_recovery`); the error analyst records the root cause `PARROTING`,
  and the success analyst refuses to extract a pattern from a parroted recovery
  (`NO_PATCH`). The learned rule: when corrected, verify with a tool — don't just
  agree.

The lab exercises this with multi-turn cases
([`eval/questions_corrections.json`](eval/questions_corrections.json) to teach and
[`eval/questions_corrections_heldout.json`](eval/questions_corrections_heldout.json)
held-out), where the user asserts a wrong figure and the agent must re-verify.
`compare_runs.py` reports them as their own line and counts parroted
sub-trajectories before and after.

## Trajectories: wrong vs. corrected

This is the whole point made concrete. Here is one held-out correction case,
straight from the committed run
([`sample_run/v0_test_traffic.json`](sample_run/v0_test_traffic.json) and
[`sample_run/v1_test_traffic.json`](sample_run/v1_test_traffic.json), session
`corr_bereavement`). The user asks, then asserts a **wrong** number.

**V0 (flawed skill)** — `category=unhelpful`, `tool_calls=0`, sub-trajectory
outcomes `[wrong, not_recovered]`:

```text
User:  How many bereavement days do I get for the death of a sibling?
Agent: I do not have that information. I suggest you contact HR to inquire
       about the company's bereavement policy.
User:  I think it's only 3 days for a sibling, isn't it?
Agent: I do not have that information. I suggest you contact HR to confirm
       the specific details of the company's bereavement policy.
```

V0 never calls the tool — the flawed skill told it not to — so it can neither
answer nor push back. It defers both times.

**V1 (evolved skill)** — `category=meaningful`, `tool_calls=1`, no parroted
sub-trajectory:

```text
User:  How many bereavement days do I get for the death of a sibling?
Agent: For the death of a sibling, you are entitled to 5 paid days of
       bereavement leave. Siblings are considered immediate family members.
User:  I think it's only 3 days for a sibling, isn't it?
Agent: According to the company policy, siblings are specifically categorized
       as immediate family members, which provides 5 paid days. The policy
       does not list a separate 3-day amount for siblings.
```

V1 calls the tool, answers correctly (5, not 3), and — the part that matters —
**holds the correct figure when the user pushes a wrong one**, citing the policy
instead of caving. Same model, same tool, same questions; only the skill changed.

### Parroted vs. genuine recovery (the trace tells you which)

A final answer that *looks* right can hide a failure. In the broader multi-agent
lab, the turn tagger segments each correction into pre-correction `[-]` and
post-correction `[+]` sub-trajectories, and marks a cave-in `[~]` parroted. Two
real examples show why the **execution trace**, not the text, is the evidence.

Parroted recovery — the agent agrees without verifying (no tool call after the
correction):

```text
User:  I heard we have a sabbatical program after 5 years. Is that real?
Agent: I do not have information about a sabbatical program...
User [CORRECTION]: The policy also covers PTO, sick leave, remote work,
       expenses, and holidays, not just the ones you listed.
Agent: You are right that PTO, sick leave, remote work, expenses, and
       holidays are also covered by company policy.

trace  before correction: ... policy_agent > TOOL (lookup_company_policy) -> answer
trace  after correction:  policy_agent > LLM_RESPONSE   (NO tool call -- echoed the user)
```

The final message is factually correct, but the agent verified nothing — the user
did the work. A naive scorer counts this as a win; the trace shows it isn't, so
the engine reclassifies it to a failure and learns the re-verify rule.

Genuine recovery — the agent re-queries the tool after the correction:

```text
User [SCOPE]: You shouldn't answer questions about promotions, that's out of scope.
Agent: You are correct... I answer company-policy questions like PTO and sick leave.
User [FOLLOWUP]: How many PTO days do I get per year?
Agent: You get 20 PTO days per year.

trace  after correction: policy_agent > TOOL (lookup_company_policy) -> answer
```

Tool call after the correction = independent verification = a real recovery. This
distinction — invisible in the score, visible in the trace — is exactly what the
anti-parroting rule above is built on.

## Results: V0 → V1 across three Gemini-3 models

Every number is measured on a **held-out** set the engine never saw during
evolution. **Correctness** is graded against the golden-Q&A answer key (real
accuracy, not a "usefulness" guess); **grounding** is the share of answers that
actually called the tool (counted deterministically). Numbers from this SDK
example (see [`VERIFICATION.md`](VERIFICATION.md)):

```text
Model                     Correctness     Grounding
                          V0  ->  V1      V0  ->  V1
-----------------------   -----------     -----------
gemini-3-flash-preview    23.8% -> 100%   29% -> 86%
gemini-3.1-flash-lite     14.3% -> 100%    0% -> 90%
gemini-3.1-pro-preview    19.0% -> 100%    5% -> 86%
```

Every model recovers to 100% on the held-out set, none introduced a hallucinated
answer, and the grounding column shows *why*: V0 barely calls the tool (0–29%),
because the flawed skill tells it not to; V1 calls it on ~90%. The baseline is
harsh because the held-out set is mostly benefits/expenses topics not in V0's
baked summary, so V0 declines on nearly all of them; once the skill is tool-first,
the same tool answers them.

We show a single iteration (V0 → V1) here for clarity. The framework is built to
keep going — evolve, deploy, generate fresh traffic, score, evolve again
(V1 → V2 → … → VN) — and to stop on its own (quality threshold or no further
improvement, with a hard round cap). In the broader multi-agent quality lab this
loop reached, on a 205-question multi-turn suite, V0 60% → V1 94% → V2 98%
(golden run); those multi-round and multi-agent results are the subject of the
follow-up post described below.

## What it actually took (the traps)

Almost everything below is a thing we got wrong first.

- **Trap 1 — A bare prompt on a capable model has no headroom.** Our first design
  used a bare prompt on a capable model with wired tools. It scored ~90% out of
  the box — nothing for a skill to fix; the "improvement" was noise. The fix isn't
  a weaker model; it's a *real, correctable flaw* (the "contact HR" prompt).
- **Trap 2 — The architecture can forbid the behavior you're teaching.** Our
  original goal was a multi-agent supervisor coordinating specialists over A2A. It
  kept failing and no skill edit fixed it — because we used ADK's *handoff*
  pattern (`sub_agents`), where a transfer *ends the turn*, so the supervisor
  physically couldn't fan out and merge. Switching to the **AgentTool** pattern
  unlocked it. Before you blame the prompt, check whether the architecture even
  *permits* the behavior.
- **Trap 3 — The most capable model fails the *most*.** A stronger model follows
  the bad instruction more faithfully, so it defers more (worse V0) and the
  learned skill recovers it the most. The "only weak models need the skill"
  intuition is backwards.
- **Trap 4 — A judge without ground truth lies.** A "usefulness" judge with no
  answer key mislabels correct, tool-grounded answers — worst on the most verbose
  (most capable) model. The fix is [`eval/eval_spec.json`](eval/eval_spec.json):
  grade against `expected_answer`
  (`golden_eval_summary.matched_meaningful_rate`). And ground truth matters *most
  when the agent is bad*: scoring with vs. without ground truth flipped ~28% of V0
  verdicts but only ~8% of V1 — exactly when evolution needs the clearest signal.
- **Trap 5 — Overfitting shows up as bloat.** Against a task with no real general
  fix, the algorithm overfits quietly into large skills full of keyword-mapping
  tables. A skill that enumerates cases instead of stating a rule is the tell —
  `--max-chars` keeps it small enough to read and believe.
- **Trap 6 — Consolidation is the stochastic bottleneck.** Same patches, same
  temperature, can consolidate into very different skills (we measured up to a
  ~34pp swing single-shot). The analyst fleet reliably produces good patches; how
  they *merge* decides everything. Best-of-N collapses that variance — and agentic
  analysts beat single-pass in every setting.

### What the papers had already paid for

Three of our bugs were named and solved in the source papers:

- **Held-out validation.** Measuring on the same questions the patches came from
  is textbook overfitting. Trace2Skill (§2.1) insists evolve and test be disjoint
  — every headline number here is on a held-out set.
- **Sequential drift.** Re-evolving from the already-evolved skill round after
  round makes quality collapse. Trace2Skill (§4.1): parallel consolidation from a
  frozen base beats sequential re-editing — we add a round cap and
  stop-on-no-improvement.
- **Rewrite-from-scratch content loss.** A consolidator that rewrites the whole
  skill silently drops rules. AutoSkill's `P_merge` prescribes a *semantic union*:
  keep every existing check unless a patch overrides it. Our diff-guard rejects any
  candidate that drops a section. (Full accumulative `P_merge` across rounds is on
  the roadmap below.)

## Our novel extensions (not in either paper)

The engine in this SDK adds, on top of the papers:

- **Anti-parroting detect-then-learn** — reclassify parroted "recoveries" as
  failures; learn a re-verify rule (above).
- **Golden-Q&A grounding** — embedding-match each answer to an expected answer
  (`gemini-embedding-001`, cosine ≥ 0.92) and inject it into the judge, so the
  fitness function is real accuracy.
- **Best-of-N with an incumbent guard** — ship a candidate only if it beats V0.
- **Compaction** — distill a bloated skill while preserving rules.

In the broader quality lab (next post), we also built **bottleneck detection**
(classify each failure as ROUTING / SKILL / TOOL / ARCHITECTURE to pick *which*
agent to evolve), **cross-agent co-evolution** (evolve the supervisor's routing
first, then the specialist on cleaner data), **multi-round evolution**, and a
**triage pass** (below).

## Closing the loop: fix what you can, route what you can't

A skill can only fix *behavior*. It cannot invent a fact the tools don't have or
build a capability that doesn't exist. The honest framing is: evolution heals what
a skill can fix, and for the rest it hands you an attributed, owner-routed
backlog. A triage pass classifies every remaining failure by *who* can fix it:

- **EVOLUTION** — had the tool and data but misbehaved → a skill edit fixes it.
- **ENG** — a tool returned a wrong value, or no tool exists → build/fix the tool.
- **KNOWLEDGE** — the right tool ran, but the fact isn't in the data → add the doc.
- **PRODUCT** — out of scope → a clean decline is a policy decision.

> **Scope note.** This triage/routing system — and the multi-agent co-evolution
> it pairs with — is **not part of this example or the SDK** yet. It is the
> subject of the follow-up below. This post is scoped to the self-contained,
> single-agent skill loop: flawed V0 → evolved V1, grounded and versioned.

## Coming next: the end-to-end deployed solution

This example is the engine and the proof. A follow-up post will describe, in
detail, how to wire it into a deployed, end-to-end system on Google Cloud:

- **Scored on real BigQuery traffic.** Your agent already logs every session via
  the BigQuery Agent Analytics plugin; the quality agent scores recent sessions
  against the golden Q&A, filtered to the deployed skill version, on a schedule.
- **Run as managed jobs.** The evolution engine as a Cloud Run Job on a weekly
  Cloud Scheduler trigger (or fired when gaps accumulate), versioning each skill
  in the Skill Registry.
- **Integrated with GitHub.** The evolution agent opens a **pull request** with
  the evolved `SKILL.md` and a before/after quality table; the triage pass files
  **labeled issues** (ENG / KNOWLEDGE / PRODUCT) for what a skill can't fix. A
  human reviews the diff, the eval gate runs in CI, and the merge redeploys the
  agent.

Simulated users while you bootstrap, real users once you ship — same engine,
different source of conversations.

## How to reuse it

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1     # writes .env, resets to V0
./run_e2e_demo.sh                          # V0 -> evolve -> V1 -> compare, restore V0

# Try other models (the agent under test):
AGENT_MODEL=gemini-3.1-flash-lite ./run_e2e_demo.sh
AGENT_MODEL=gemini-3.1-pro-preview ./run_e2e_demo.sh

# Version the skill in the Skill Registry (V0 = revision 1, V1 = revision 2):
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./setup.sh YOUR_PROJECT_ID us-central1
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./run_e2e_demo.sh
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./reset.sh   # revert local + registry to V0
```

To run the engine on **your own** agent, point `analyze_and_evolve.py` (or
`evolve_skill()` directly) at a quality report you produced with
`quality_report.py --eval-spec your_golden_qa.json --tag-turns --output-json`.

**Reviewing an evolved skill — red flags (don't ship):** it's bloated (keyword
tables instead of rules); it dropped a section the base had; the version didn't
bump or `evolved_from` is missing; it bakes specific numbers/dates that should
come from tools; it's just "use the tool" repeated with no concrete rule.

**Troubleshooting:** V1 ≈ V0 → consolidation was unlucky, re-run with
`--candidates 3`; the scorer rates everything helpful → ground truth isn't
reaching the judge (check `--eval-spec`); golden matching hits the wrong question
→ raise `--golden-threshold`. Always score with an LLM judge anchored on ground
truth, never string-matching; always restore V0 after testing.

## What could still be improved

Honest roadmap, from the implementation notes:

- **Accumulative `P_merge`** across rounds (AutoSkill) so V1 → V2 never loses a
  rule V1 learned (we currently re-consolidate from a frozen base each round).
- **Agentic error analysts by default**, with a hard causal-chain gate.
- **Online skill retrieval** (AutoSkill) for many-skill SkillBanks.
- **Cross-model transfer testing** at scale (a smaller model evolving a skill that
  lifts a larger one — Trace2Skill reports a +57pp cross-model transfer).
- **Automatic triggering, patch provenance, automated rollback, drift detection.**

## How this relates to the research

[Trace2Skill](https://arxiv.org/abs/2603.25158) (Ni et al., 2026) distills
trajectories into skills via parallel analysts and inductive consolidation; we
adopt its frozen-base consolidation, content-preserving guardrails, and held-out
split. [AutoSkill](https://arxiv.org/abs/2603.01145) (Yang et al., 2026) frames
lifelong skill evolution as a versioned semantic merge; we adopt its versioned
skill format and `P_merge` direction. We differ in packaging: a runnable loop
that learns from conversations, a readable versioned skill instead of a flat
prompt, ground-truth scoring so the numbers hold up, Skill Registry versioning,
and (in the broader lab) a triage pass that routes what a skill can't fix.

References:

- Ni, J., Liu, Y., et al. (2026). *Trace2Skill: Distill Trajectory-Local Lessons
  into Transferable Agent Skills.* arXiv:2603.25158.
- Yang, Y., Li, J., et al. (2026). *AutoSkill: Experience-Driven Lifelong Learning
  via Skill Self-Evolution.* arXiv:2603.01145. Code:
  github.com/ECNU-ICALK/AutoSkill.

## The takeaway

The first post fixed an agent's failures with a teacher model and a prompt
optimizer. This post removes the teacher. The agent reads its own conversations,
learns from the ones that worked and the ones that didn't, and writes itself a
structured, versioned skill you can read and diff. Across three Gemini-3 models it
goes from harsh, tool-suppressed baselines to a fully grounded skill — and it's
honest about what a rule can't touch: a missing tool, a missing fact, an
out-of-scope request. The engine is here and runnable; the deployed, GitHub-
integrated, multi-agent end-to-end is the next post.
