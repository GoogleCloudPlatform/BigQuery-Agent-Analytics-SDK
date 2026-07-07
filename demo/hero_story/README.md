# Hero demo: Claude Code + Codex telemetry → BigQuery, in your own project

## The demo contract

| | |
|---|---|
| **Audience** | Platform admin running it + engineering director watching |
| **Promise** | In one sitting: real Claude Code **and** Codex telemetry landing in one customer-owned BigQuery schema, with privacy controls demonstrated and a clean teardown |
| **Success screen** | `bqaa-otel verify --smoke` all green **plus** one BigQuery result showing `source_product IN ('claude_code', 'codex')` across logs, metrics, and spans |
| **Clock** | Presenter track: ~12 min against an existing deployment. Fresh install: ≤30 min ([OPERATOR.md](OPERATOR.md)) — Cloud Build accounts for ~10 of them |

**The accurate one-liner:** one command deploys the collector and the
warehouse surface into the customer's own GCP project; two generated
artifacts then enable the sources (Claude Code managed settings, Codex
`config.toml`). Telemetry never touches infrastructure the customer does not
own.

```
Claude Code / Codex --OTLP--> Cloud Run receiver --> Pub/Sub --> consumer --> BigQuery
        (developer laptops)          └────────── customer-owned GCP project ──────────┘
```

Presenter prerequisites: a deployment already bootstrapped by
[OPERATOR.md](OPERATOR.md), `scripts/preflight.sh` green, and one rehearsal.
Never present a first run.

---

## Act 1 — The problem (1 minute)

> "We rolled out Claude Code and Codex to our engineers. Today we cannot
> answer: who uses them, what they cost, which teams get value, or what the
> agents actually did. Each vendor has a dashboard; we have no unified,
> queryable record in **our** warehouse — and security wants proof about
> what leaves developer machines."

That is the whole setup. Do not oversell; the next ten minutes are the
argument.

## Act 2 — The magic moment (5 minutes)

**Beat 1 — the pipeline exists because of one command.** Show (do not
re-run) the command that built everything in this project:

```bash
bqaa-otel bootstrap --project $PROJECT --dataset $DATASET \
  --signals logs,metrics,traces --source claude-code,codex --execute
```

Point at what it created: native OTel tables + dedup views + the BQAA
projection, an authenticated Cloud Run receiver, Pub/Sub with a retained
dead-letter queue, a scheduled projection MERGE — and the two config
artifacts, generated against the real endpoint.

**Beat 2 — privacy is a flag, not a promise.** Show the baseline artifact
(no prompt text, no tool content), then run the refusal live:

```bash
bqaa-otel config --endpoint $URL --source claude-code --privacy replay
# bqaa-otel: error: ... requires acknowledge_content_logging
# (exit code 2 — content capture is impossible to enable by accident)
```

**Beat 3 — real sessions.** Run the scripted sessions (both products, fixed
`demo_run_id`, deterministic prompts — see `scripts/run_sessions.sh`):

```bash
scripts/run_sessions.sh   # prints DEMO_RUN_ID=<id>; keep it for Act 3
```

While they flush (~1 min), run the pipeline proof:

```bash
BQAA_OTLP_TOKEN=... bqaa-otel verify --smoke --signals logs,metrics,traces \
  --endpoint $URL --project $PROJECT --dataset $DATASET
```

Say explicitly: *the smoke rows prove the pipeline; the session rows we
query next are real product telemetry.* Keep the two separate.

## Act 3 — The payoff (5 minutes)

Run the SQL pack in order — each file opens with the leadership question it
answers. Logs/metrics/spans queries filter on this run's `demo_run_id`, so
those numbers are from the sessions the audience just watched; dead-letter
health rows (in `00`/`05`) are deliberately deployment-scoped:

```bash
scripts/run_queries.sh $DEMO_RUN_ID   # writes results to evidence/
```

| Query | The question on the slide |
|---|---|
| `sql/00_health_and_freshness.sql` | Is the pipeline healthy *right now*? |
| `sql/01_adoption_by_product.sql` | Who is using Claude Code vs Codex? |
| `sql/02_token_usage_and_estimated_cost_by_team.sql` | Where is usage going, and what does it roughly cost? *(tokens are measured; dollars are an estimate from the rates CTE)* |
| `sql/03_event_mix_and_workflow.sql` | What are the agents actually doing? |
| `sql/04_latency_from_spans.sql` | How fast — p50/p95 from real spans, per product |
| `sql/05_governance_privacy_dlq.sql` | Is privacy holding and is ingestion clean? *(exact scripted-prompt search returning status rows; note: privacy checks are run-scoped, dead-letter health is deliberately deployment-scoped)* |
| `sql/06_raw_native_escape_hatch.sql` | When a product ships a new event tomorrow, do we lose it? *(no — preserved natively, queryable before any code changes)* |

Close on the success screen: the verify output plus the
both-products-one-schema result. Then hand over `one_pager.md`, regenerated
from this run — that is the artifact that gets forwarded.

---

## After the demo

- Regenerate the forwardable summary: `evidence/` outputs → `one_pager.md`
  (aggregates and hashes only — see redaction rules in
  `evidence/template.md`).
- Tear down if this was a throwaway project: `scripts/teardown.sh`
  (dry-run by default; the dataset contains real telemetry).

## What this demo deliberately does not claim

- **Not replay.** Traces are span structure and timing, not transcripts.
  Codex documents no raw request/response body path; Claude raw-body capture
  exists only behind the explicit replay acknowledgement.
- **Not vendor pricing.** Token counts are measured; dollar figures are
  estimates from an editable rates table with a visible as-of date.
- **Scoped privacy claim.** Baseline proves prompt content is not written
  into this telemetry warehouse by this configuration — it is not a claim
  about other channels.
