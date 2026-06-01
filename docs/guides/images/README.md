# Screenshot for the Conversational Analytics-first guide

The guide uses a single representative Conversational Analytics screenshot,
captured over a dataset seeded with
`bqaa seed-events --scenario decision-realistic --seed 42` and materialized with
`bqaa context-graph`.

| File | Conversational Analytics question |
|------|-----------------------------------|
| `ca-committed-outcomes.png` | "Which requests never reached a committed outcome?" |

The shot shows the question and CA's answer in the same frame: over the
materialized graph, every recorded request reached a committed outcome (the
orphaned sessions live in `agent_events`, not the graph).

To refresh it: crop to the conversation/answer panel (not the agent editor),
keep the question visible, export PNG ~1200px wide.
