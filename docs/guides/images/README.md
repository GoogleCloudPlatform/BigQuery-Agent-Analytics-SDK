# Screenshots for the Conversational Analytics-first guide

Drop the five Conversational Analytics screenshots here, using the exact
filenames below. Each one should show the plain-English question you typed and
CA's answer in the same frame, captured over a dataset seeded with
`bqaa seed-events --scenario decision-realistic --seed 42` and materialized with
`bqaa context-graph`.

| File | Ask Conversational Analytics |
|------|------------------------------|
| `ca-01-decisions-per-agent.png` | "How many decisions did each agent make, and how many failed?" |
| `ca-02-low-confidence-approvals.png` | "Show me the approvals with confidence below 0.5" |
| `ca-03-budget-allocator-confidence.png` | "What did the budget-allocator agent decide, and how confident was it?" |
| `ca-04-orphaned-requests.png` | "Which requests never reached a decision?" |
| `ca-05-outcome-distribution.png` | "What are the most common decision outcomes?" |

Tips:
- Keep the question text visible in the screenshot.
- Crop to the chat answer + any chart CA renders; trim console chrome.
- PNG, roughly 1200–1600px wide reads well in the rendered docs.
