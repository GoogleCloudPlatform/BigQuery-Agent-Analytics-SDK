# Screenshots for the Conversational Analytics-first guide

Drop the five Conversational Analytics screenshots here, using the exact
filenames below. Each one should show the plain-English question you typed and
CA's answer in the same frame, captured over a dataset seeded with
`bqaa seed-events --scenario decision-realistic --seed 42` and materialized with
`bqaa context-graph`.

| File | Ask Conversational Analytics |
|------|------------------------------|
| `ca-01-sessions-per-agent.png` | "How many decision sessions did each agent run, and how many errored?" |
| `ca-02-low-confidence-options.png` | "Show me the requests that weighed an option below 0.5 confidence" |
| `ca-03-budget-allocator-considered.png` | "What did the budget-allocator agent consider, and how confident was it?" |
| `ca-04-orphaned-requests.png` | "Which requests never reached a committed outcome?" |
| `ca-05-confidence-spread.png` | "What's the spread of confidence across the options agents weighed?" |

Tips:
- Keep the question text visible in the screenshot.
- Crop to the chat answer + any chart CA renders; trim console chrome.
- PNG, roughly 1200–1600px wide reads well in the rendered docs.
