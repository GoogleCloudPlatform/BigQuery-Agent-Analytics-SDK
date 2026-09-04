# Research Context: Skill Evolution Foundations

This document distills the two foundational papers into actionable decision
guidance for the Skill Evolution Agent. Use these findings to justify
algorithmic choices and interpret results.

## Trace2Skill (arXiv:2603.25158, Ni et al., 2026)

### Formal Definitions

A **skill** S = (M, R) where M is a root markdown document (SKILL.md)
and R = (scripts, references, assets). M encodes procedural knowledge:
when to apply a technique, step-by-step strategies, and known failure modes.

A **trajectory** tau_i = (q_i, (r_1, a_1, o_1), ..., (r_T, a_T, o_T), y_i)
captures: query, reasoning traces, tool calls, observations, and correctness
outcome y_i in (0, 1).

The corpus T is partitioned into:
- T- = all tau where y = 0 (failures)
- T+ = all tau where y = 1 (successes)

**Evolution objective**: construct S* from trajectories on D_evolve such that
P(S*; pi, D_test) > P(S_0; pi, D_test) without updating model parameters.

### Three-Stage Pipeline

**Stage 1: Trajectory Generation**
- Run agent pi with frozen skill S_0 across task set D_evolve
- Classify outcomes into T- and T+
- Practical scale: 200 trajectories with 50+ turns using 122B model < 2 GPU-hours
- Our implementation: production sessions from BigQuery over a time
  window, or a host-defined full/quick evaluation set driven through
  the traffic hook

**Stage 2: Parallel Multi-Agent Patch Proposal**
- Each analyst assigned a single trajectory tau_i, receives frozen copy of S_0
- Produces a skill patch p_i independently (no cross-visibility)
- Two asymmetric analyst types:

  **Error Analyst (A-)** — ReAct-style multi-turn agentic loop:
  1. Understand the task and failure surface
  2. Trace failure to agent behavior — locate the decision or code step that produced mismatch
  3. Validate root cause with a minimal fix — write fixed output and re-evaluate against ground truth
  4. If still failing, return to steps 1-3 and revise diagnosis
  - Output: (1) Failure Cause Items — systematic, causal reasons grounded in observable behavior;
    (2) Failure Memory Items (<=3) — generalizable insights to avoid similar failures
  - Quality gate: patch excluded from pool if neither condition met (successful fix OR valid causal analysis)
  - This quality gate ensures every patch in P- is grounded in a verified failure cause

  **Success Analyst (A+)** — Fixed single-pass workflow:
  1. Clean the trajectory, identify generalizable behavior patterns
  2. Requirements: Broad Coverage (every effective behavior captured), Frequency Awareness
    (high-frequency patterns listed first), Generalization (general mechanisms, not task-specific details)
  - Output: Success Memory Items with title, description, and concrete examples

- Implementation: 128 sub-agents in parallel, merge batch size B=32, interaction turn budget=100

**Stage 3: Conflict-Free Patch Consolidation**
- Hierarchical merging: L = ceil(log_B |P|) levels, groups of up to B_merge patches per level
- Three deterministic guardrails:
  1. Patches referencing non-existent files are rejected
  2. Edits targeting the same line range within same file are flagged as conflicts and withheld
  3. Updated skill S is validated by a skill format checker
- Final patch p* is translated into diff-style edit operations and applied programmatically

**Merge Operator (M) Prompt — Key Guidelines:**
1. Deduplicate: keep the best version (most specific, best worded)
2. Resolve conflicts: choose stronger justification or synthesize both
3. Preserve unique insights: include all unique, non-redundant edits
4. Maintain conciseness: merged patch should have <= sum of unique edits
5. Ensure independence: no two edits may target overlapping lines
6. Atomic create/link pairs: a `create` for references/*.md and the SKILL.md edit that links to it are inseparable — keep both or drop both
7. **Prevalent pattern bias**: when multiple patches independently propose similar edits addressing the same class of failure or success, treat this recurrence as evidence of a *systematic* property. Preserve such prevalent edits with higher priority.

### Key Experimental Results

**Table 1: Cross-Model Transfer (SpreadsheetBench + WikiTQ)**

| Condition | Skill Author | Skill User | Avg Delta |
|-----------|-------------|------------|-----------|
| +Error (Deepening) | 122B | 122B | best in-dist |
| +Error (Creation) | 35B | 122B | **+57.65pp** on WikiTQ |
| +Combined (Deepening) | 122B | 122B | most consistent |
| +Combined (Creation) | 122B | 122B | +17.6pp Avg |

Key findings:
- Skills evolved by 35B model improved 122B model by up to +57.65pp — skill knowledge is model-agnostic
- +Combined is the most consistently strong signal across all settings
- +Error is the most reliable (always positive), +Success is most volatile
- Parametric baseline (LLM-generated skill without trajectories) provides near-zero improvement

**Table 4: Parallel vs Sequential Consolidation**

| Method | Time | Quality (Vrf) |
|--------|------|---------------|
| Seq-B=1 | ~60 min | 61.83 |
| Seq-B=4 | ~15 min | 59.00 |
| Parallel (ours) | **~3 min** | **65.83** |

- Parallel is 20x faster than Seq-B=1 AND 4-6.8pp better in quality
- Sequential editing compounds errors and produces layout drift
- Parallel prevents sequential drift: all patches derive from same frozen S_0

**Table 6: Agentic vs Single-LLM Error Analysis**

| Setting | Agentic +Error | Single-LLM +Error | Gap |
|---------|---------------|-------------------|-----|
| 122B Deepening Avg | 40.75 | 28.58 | **+12.2pp** |
| 35B Deepening Avg | 36.04 | 32.83 | +3.2pp |
| 35B Creation Avg | 39.06 | 25.76 | **+13.3pp** |

- Agentic wins in Avg across ALL four Author-Mode settings
- Single-LLM over-attributes parse failures as root cause in 57% of cases (vs 14% for agentic)
- Agentic analysts can inspect artifacts, query ground truth, and iteratively narrow root cause
- **Decision rule: ALWAYS use agentic error analysis. Never use single-pass.**

**Consolidation Variance**: 6.9pp across identical inputs — motivates best-of-N

**Generalizable SoPs Discovered** (from 323 patches, 122B Deepening +Combined):
1. Formula recalculation + write-back verification (178/323 patches)
2. Tool selection: openpyxl over pandas.to_excel() (177/323 patches)
3. Explicit read-back verification (138/323 patches)
4. Structural-edit safety (53/323 patches)
- Low-support observations routed to references/ subdirectory (not discarded)

### Decision Rules from Trace2Skill

1. **Always use +Combined** (both error and success analysts) for maximum reliability
2. **Always use agentic error analysis** — single-pass is 6.8-13.3pp worse
3. **Parallel consolidation only** — sequential is slower AND worse
4. **Best-of-3 consolidation** — reduces failure rate from 30% to 3%
5. **Prevalence filtering** — patches in 3+ independent analysts = strong signal; 1-2 = noise
6. **Frozen skill independence** — analysts must never see each other's patches
7. **Skills are model-agnostic** — evolved skills transfer across model scales
8. **Low-support observations go to references/**, not discarded

---

## AutoSkill (arXiv:2603.01145, Yang et al., 2026)

### Core Architecture

Dual-loop system: **skill-enhanced response generation** (online, per-query)
coupled with **skill evolution** (background, continuous).

**Skill representation**: s = (n, d, p, tau, gamma, xi, v) where:
- n = name (concise, searchable, intent-explicit)
- d = description (what the skill does and when to use it)
- p = executable instruction prompt (Goal, Constraints & Style, optional Workflow)
- tau = trigger set (phrases that activate the skill)
- gamma = tag set (for categorization and retrieval)
- xi = example set
- v = version (supports lineage tracking)

### Five Prompt Modules

P = (P_rw, P_chat, P_ext, P_judge, P_merge)

1. **P_rw (Query Rewriting)**: Rewrites user query for retrieval — resolves references
   ("it", "this"), preserves topic anchor, keeps only retrieval-relevant constraints

2. **P_chat (Dialogue Generation)**: Response generator with retrieved skill context.
   Rules: use skill only when it directly matches user intent; otherwise ignore and answer normally;
   never mention that skills were retrieved/injected

3. **P_ext (Skill Extraction)**: Extracts reusable skills from user-side interaction signals only
   (not model responses). Key principles:
   - Treat user turns as primary evidence; assistant turns are only context
   - Extract only durable, reusable constraints, policies, workflows, or templates
   - Do NOT extract one-shot requests, generic tasks, or assistant-invented details
   - Capture *how to do* similar tasks, not this-instance facts
   - Remove case-specific entities, preserve only portable rules

4. **P_judge (Skill Management Decision)**: Decides add/merge/discard for each candidate.
   Decision procedure:
   1. Check topic continuity and capability family
   2. Apply discard gate: reject generic, low-signal, non-portable, or library-covered candidates
   3. Compare vs existing skills on 4 axes: job-to-be-done, deliverable type, hard constraints/success criteria, required tools/workflow
   4. Choose merge only when they are the same capability after removing instance details
   5. Choose add when the candidate remains a distinct durable capability

5. **P_merge (Skill Merging)**: Combines existing skill with candidate into improved version.
   Key rules:
   - Preserve original capability identity
   - Perform semantic union, not raw concatenation
   - Import only reusable, non-conflicting additions
   - Avoid regressions: keep important checks from existing skill
   - Remove case-specific entities and one-off business facts
   - Deduplicate sections, bullets, triggers, tags, and examples
   - Prompt structure: # Goal, # Constraints & Style, # Workflow (optional)

### Versioned Skill Identity

Version update rule: v(s'_t) = Bump(v(s*_t))
- Each merge increments the version (patch-level)
- Example: professional_text_rewrite reached v0.1.34 after 34 rounds of iterative refinement
- Skills evolve at different rates based on usage frequency
- Frequently reused productivity skills merge repeatedly; specialized skills stay at early versions

### Skill Bank Update Rule

Skill Bank Update:
- If action = add: B_u(t+1) = B_u(t) union z_t
- If action = merge: B_u(t+1) = (B_u(t) minus s*_t) union s'_t
- If action = discard: B_u(t+1) = B_u(t) (unchanged)

### Hybrid Retrieval

Rel(q_t, s) = lambda * d_hat(q_t, s) + (1-lambda) * b_hat(q_t, s)

Where d_hat is normalized dense semantic similarity and b_hat is normalized BM25 score.
Only skills exceeding threshold eta are injected into context.

### Skill Lifecycle (4 stages)

1. **Experience ingestion** — ingest dialogue messages and behavior traces
2. **Skill extraction** — abstract reusable capability candidates (not memorizing all interactions)
3. **Skill maintenance and versioning** — add/merge/discard decisions with version tracking
4. **Skill reuse** — retrieve, render into context, inject into LLM request

### Key Design Principles

1. **Explicit skill representation** — structured artifacts, not hidden model state
2. **Continuous but controlled evolution** — extract, judge, merge/add/discard; prevents bloat
3. **Low-friction deployment** — SDK, Web UI, OpenAI-compatible reverse proxy
4. **Training-free** — all improvements from explicit skill construction, retrieval, and refinement

### Scale Results (WildChat-1M corpus)

| Corpus | Conversations | Extracted Skills |
|--------|--------------|-----------------|
| English GPT-3.5 | 10,243 | 631 |
| English GPT-4 | 5,211 | 603 |
| Chinese GPT-3.5 | 5,912 | 400 |
| Chinese GPT-4 | 1,145 | 224 |

Top extracted skill categories: Programming (482), Writing (363), Data/AI/ML (354)

---

## Synthesis: How This System Combines Both Papers

### From Trace2Skill (batch evolution):
- Three-stage pipeline: trajectory generation → parallel analyst fleet → hierarchical consolidation
- Frozen skill independence, asymmetric analysts, prevalence-weighted inductive reasoning
- Parallel consolidation (not sequential)
- Best-of-N candidate selection to handle 6.9pp consolidation variance
- Agentic error analysis (mandatory, never single-pass)

### From AutoSkill (continuous evolution):
- Versioned skill identity with lineage tracking (V0→V1→V2)
- Structured skill representation (SKILL.md + references/)
- Add/Merge/Discard decision framework (used for patch filtering)
- Skill lifecycle: extraction → maintenance → versioning → reuse

### Novel Extensions (not in either paper):
- **Bottleneck detection**: LLM classifier identifies the root-cause agent before evolving
- **Multi-agent co-evolution**: evolve several registered agents simultaneously
- **Min-failures gate**: skip evolution when failures < 30 (sparse patches = weaker skills)
- **Two-round minimum**: V1 discovers failure landscape (+1.5pp), V2 writes strong fixes (+33.1pp)
- **Compaction pass**: LLM distillation when skill exceeds 25K chars (45K→10K observed)
- **Template-guided V2+**: use previous version as structural blueprint to prevent layout drift
- **Production trajectories by default**: evolution reads the agent's own
  BigQuery sessions; synthetic adversarial traffic is an optional host hook
- **5-dimension quality scoring**: meaningful, unhelpful, off-topic, error, incomplete
  (with correction boundary extraction for multi-turn conversations)
- **Incumbent comparison**: reject the evolved candidate if it regresses vs the current version
- **Two-phase scoring**: the quality report (turn tags + trajectory sampling from BigQuery)
  for deployed skills, and the host scoring hook for not-yet-published candidates
