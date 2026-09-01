# Skill Evolution Algorithm

## Full Loop (N rounds)

### Setup
1. Create a timestamped run directory for the run's artifacts
2. Snapshot current skills to the run directory as "initial" for
   comparison and rollback

### For each round R (1..N):

#### Phase 1: Baseline Trajectories + Scoring
1. Obtain trajectories for the current skill version:
   - **Default (no plugins):** the agent's own production sessions,
     already in BigQuery via the analytics plugin. The quality report
     covers a time window (e.g. the last 7 days), optionally narrowed
     to one app name, agent version, and set of trace labels.
   - **Optional:** a host traffic hook that drives an evaluation set
     against the live agent. Hosts typically define a full set and a
     quick set; the quick set is for per-candidate iteration and the
     full set for round-boundary validation.

2. Score the trajectories:
   - Production sessions are scored by the quality report itself:
     turns are tagged with quality labels, trajectories are read from
     the BigQuery trace tables, and a judge scores them across
     dimensions, extracting correction boundaries from multi-turn
     conversations.
   - Candidate skills that are not deployed yet can only be scored by
     the host scoring hook, which owns the evaluation set and the
     judge. With no hook configured, candidates are not measured and
     selection falls back to the engine's built-in heuristic.
   - Output: a quality report JSON with meaningful_rate and
     unhelpful_rate.

3. Print baseline metrics:
   - Total sessions
   - Meaningful rate (target improvement per round)
   - Unhelpful rate
   - Off-topic, error, incomplete counts

#### Phase 2: T+/T- Partition
1. Partition sessions into T+ (success) and T- (failure):
   - `meaningful` or `declined` → T+
   - `unhelpful` or `partial` → T-
   - **Parroting override**: If a session scored `meaningful` but has
     a sub-trajectory with `outcome == "parroted"`, reclassify it as
     T-. A parroted recovery means the agent echoed the user's
     correction without re-querying a tool — the user did the agent's
     work, so it's not a genuine success.

2. Evolution gate: If T- count < MIN_FAILURES (default 30):
   - STOP the loop — do not evolve, do not snapshot a duplicate
     version, do not run further rounds
   - Rationale: Sparse patch sets produce weaker consolidated skills
   - Proceed directly to Cleanup (comparison table + archive)
3. Otherwise proceed to Phase 3

#### Phase 3: Bottleneck Detection
1. Sample failed conversation traces
2. LLM classifier analyzes each failure:
   - Routing failure: the orchestrating agent sent the query to the
     wrong sub-agent
   - Skill failure: the answering agent has a skill gap or anti-pattern
   - Tool failure: tool unavailable or returned an error
   - Architecture failure: multi-hop reasoning required, context
     overflow, etc.
3. Count failures by source agent
4. Decide which agent(s) to evolve, using the agent registry's names:
   - If >70% skill failures: evolve the answering agent's skill
   - If >70% routing failures: evolve the orchestrating agent's skill
   - If mixed: evolve both (co-evolution)

#### Phase 4: Evolution
1. Run the evolution engine with configuration:
   - `agentic`: Multi-turn error analysts (mandatory)
   - patch scoring: Analyst self-scoring for the prevalence threshold
   - consolidation model: e.g. gemini-2.5-pro
   - `max_workers`: Parallel analyst fleet size
   - `candidates N`: Generate N consolidated candidates (best-of-N)
   - `candidates_dir`: Output directory for candidates

2. Evolution substeps:
   - Analyst fleet: Parallel error/success analysts generate patches from trajectories
     - Error analysts receive T- trajectories with execution sub-trajectories
       formatted as `[-]` (wrong), `[+]` (recovered), `[~]` (parroted) segments
     - Root cause categories: KEYWORD_GAP, MISSING_RULE, AMBIGUITY,
       SCOPE_GAP, HALLUCINATION, PARROTING, CORRECTION_IGNORE
     - PARROTING: Agent accepted user's correction without re-querying
       a tool. The skill should instruct independent verification.
     - Success analysts receive T+ trajectories; sessions with parroted
       sub-trajectories are excluded (output "NO_PATCH: parroted recovery")
   - Patch scoring: Analysts score their own patches (0-10 relevance scale)
   - Prevalence filtering: Retain patches appearing in 3+ independent outputs
   - Consolidation: Generate N candidates from filtered patch pool
   - If N=1: Use single candidate
   - If N>1: Best-of-N selection (see Phase 4.3)

3. Best-of-N selection (if candidates > 1):
   - For each candidate C (1..N):
     - Score it through the host scoring hook (the hook deploys the
       candidate, exercises the quick evaluation set, and judges the
       result)
     - Record meaningful_rate(C)
   - Select the candidate with the highest meaningful_rate
   - Compare against incumbent V(R-1):
     - If regression (meaningful_rate drops): reject the candidate,
       keep V(R-1)
     - Otherwise: accept the candidate as V(R)
   - A candidate whose score is unmeasurable (hook skipped, or a
     report with preflight exclusions) is treated as unscored, never
     as a zero — a flaked measurement must not lower the bar.

4. Snapshot the evolved skill as V(R) in the run directory

5. Optional: Compaction pass
   - If skill size > MAX_SKILL_CHARS (default 25000):
     - Run LLM-based distillation
     - Preserve tool rules, keyword mappings, anti-patterns
     - Target: 10K-15K chars
     - Validate: Score the compacted skill, reject if regression

#### Phase 5: Validation
1. Deploy the evolved skill V(R)
2. Re-measure: a fresh quality report over the new sessions, or the
   host's full/quick evaluation set through the traffic hook
3. Score
4. Report delta from V(R-1):
   - Meaningful rate change
   - Unhelpful rate change
   - Per-dimension breakdown

### Cleanup
1. Print comparison table (all versions):
   ```text
   | Version | Meaningful | Unhelpful | Delta | Elapsed Time |
   |---------|-----------|-----------|-------|--------------|
   | initial | 60.0%     | 35.0%     | -     | 15m 32s      |
   | v1      | 94.0%     | 4.0%      | +34.0pp | 12m 18s    |
   | v2      | 98.0%     | 1.0%      | +4.0pp  | 11m 45s    |
   ```

2. Archive the run directory with all artifacts:
   - Skill snapshots (initial, v1, v2, ...)
   - Quality reports per version
   - Analyst patches and candidate skills

---

## Configuration Defaults

| Parameter | Default | Purpose |
|-----------|---------|---------|
| ROUNDS | 2 | Two-round strategy (initial→v1→v2) |
| CANDIDATES | 3 | Best-of-N selection (optimal cost/quality) |
| MIN_FAILURES | 30 | Evolution gate threshold |
| MAX_SKILL_CHARS | 25000 | Triggers compaction pass |
| AGENTIC | true | Multi-turn error analysts (mandatory) |
| TRAJECTORY_SAMPLES | 100 | Traces sampled from BigQuery |
| QUICK/FULL EVAL SETS | host-defined | Quick set for candidate iteration, full set for round validation |

---

## Key Decision Points

### Why Two Rounds Minimum?
- V1 discovers the failure landscape: weak patches (+1.5pp gain) but identifies root causes
- V2 writes strong fixes: builds on V1 analysis (+33.1pp gain)
- Single-round evolution misses compounding insight from V1 error analysis

### Why Best-of-3?
- Consolidation has 6.9pp variance across identical inputs (Trace2Skill finding)
- Cost: 3x consolidation (cheap, ~30s each) vs 1x analyst fleet (expensive, ~10min)
- Single candidate: 70% success rate
- Best-of-3: 97% success rate
- Optimal trade-off for production systems

### Why Always Agentic?
- Multi-turn investigation outperforms single-pass by 6.8pp (Trace2Skill)
- Biggest gains on complex failures (hallucination, refusal)
- Error analysts need to explore trajectories, test hypotheses
- Marginal cost is low (2-3 LLM calls vs 1), reliability gain is large

### Why Template-Guided for V2+?
- Sequential consolidation compounds errors, produces layout drift
- Use V1 as structural blueprint for V2: preserves sections, prevents bloat
- Improves consistency across rounds without constraining content

### When to Skip Evolution?
- If failures < 30: sparse patch sets produce weaker skills
- Better to accumulate more trajectories and evolve in the next round
- Prevents thrashing on noise

### Why the Baseline Skill Needs Correction Verification

A bare-minimum baseline skill — "answer based on your knowledge, be
brief" — starves the pipeline of the signal it needs most. When a user
corrects such an agent, it simply echoes "you're right" (parroting),
producing a conversation that *looks* correct in the final response
but where the agent added no value.

One paragraph fixes it:

```text
When a user corrects you or disputes your answer, do not simply
accept their correction. Use your available tools to verify the
claim independently, then respond with what you find.
```

With correction verification in place, the agent re-queries its tools,
producing an execution trace that shows whether the recovery was
genuine (tool call after correction) or parroted (no tool call).

The evolution pipeline uses this signal in two ways:
1. **T+/T- partition**: Parroted recoveries are reclassified from T+
   to T-, so the error analyst examines them instead of the success
   analyst trying to extract a non-existent success pattern.
2. **Sub-trajectory rendering**: Parroted segments are marked `[~]`
   in the formatted trajectory, giving the error analyst explicit
   evidence of what went wrong — the agent accepted the correction
   without verification.

Without it the pipeline operates on noisier data: parroted sessions
contaminate T+ and the algorithm tries to learn from cases where the
user did the agent's work.
