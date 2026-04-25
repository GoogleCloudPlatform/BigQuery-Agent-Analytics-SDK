# `examples/ci/`

Reference CI artifacts for agent quality gates backed by
BigQuery Agent Analytics.

## `evaluate_thresholds.yml`

Drop-in GitHub Actions workflow that runs four deterministic
budgets (latency, token usage, tool error rate, turn count) on
every PR, scoring the last 24 hours of production traces from an
`agent_events` BigQuery table. Exits non-zero when any session
breaches its budget, so a bad merge lights up the PR status
before code ships.

See the companion Medium post, *Your Agent Events Table Is Also a
Test Suite*, for the narrative, threshold-setting guidance, and
the companion categorical-eval gate that pairs naturally with
this workflow.

### Quick start

1. Copy `evaluate_thresholds.yml` to `.github/workflows/` in
   your agent repo.
2. Set repository variables `PROJECT_ID` and `DATASET_ID` to the
   GCP project + BigQuery dataset where your `agent_events` table
   lives.
3. Set the repository secret `GCP_SA_KEY` to a service-account JSON
   with `bigquery.jobUser` + `bigquery.dataViewer` on the dataset.
4. Replace `calendar_assistant` with your agent's name in all four
   `--agent-id` flags inside the workflow.
5. Tune the four `--threshold` numbers against your own production
   distribution. A defensible starting point for each is "p95 of
   the last 30 days + 10% buffer"; revisit after week one of CI
   gating.

### Requirements

- `bigquery-agent-analytics >= 0.2.2` — earlier releases shipped
  normalized `1.0 - observed/budget` gate scoring with a `0.5`
  pass cutoff, which fires every gate at roughly half the budget
  the user typed. 0.2.2 switched to raw-budget binary gates so
  the `--threshold` value means what it says.
