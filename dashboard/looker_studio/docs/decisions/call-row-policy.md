# Decision: call_row_policy = raw_row (frozen, M0; divergence corrected)

## Question

Multiple `agent_events` rows can share the count-distinct key
`trace_id|span_id`: ADK logs streaming **partial** `LLM_RESPONSE` events
without popping the span, then the final response on the same span. How must
token sums, latency aggregates, and call counts treat repeated keys?

## Evidence

ADK 1.27.0 `bigquery_agent_analytics_plugin.py` L3074–3125: partial chunks
are logged as `LLM_RESPONSE` rows carrying `usage_metadata` and a computed
elapsed `latency_ms`, on the same un-popped span as the final row. The seed
generator emits deterministic partial latencies accordingly.

The pinned block's **explore** joins `agent_events` to `v_llm_response` on
`trace_id, span_id, event_type` and declares the join `one_to_one`.
Repeated keys violate that declaration and **fan out n×n** in the join
shape (measured as a BigQuery simulation of the pinned explore join).
Measured on the committed scenario: direct view 6,985 rows / 17,568,787
tokens vs join shape 9,257 rows / 23,269,595 tokens; distinct calls
identical (6,323).

## Decision

**`raw_row` over the direct view**: SUM/AVG/percentile aggregate every
view row (partials included); call counts use `COUNT(DISTINCT
trace_id|span_id)`. The dashboard's union and the oracle share these
semantics by independent construction.

This is **not** exact block reproduction on the repeated-key population.
The earlier claim ("reproduces block behavior by construction") was wrong
and is retracted: the pinned join's n×n fan-out inflates SUM/AVG measures
in ways `raw_row` deliberately does not copy — reproducing a join-integrity
artifact would corrupt the metrics it exists to report.

## M4 consequences (per the contract's divergence rule)

Parity evidence records three result classes:

1. exact parity over rows satisfying the one-to-one key assumption;
2. `raw_row` behavior on repeated-key fixtures (committed expected results);
3. the pinned-join fan-out expectation, recorded separately as the
   **documented intentional divergence** with the measured numbers above.

`terminal_row_per_key` (dedup to the final row) remains a v1.1 candidate,
alongside offering the block an upstream fix for the fan-out itself.
