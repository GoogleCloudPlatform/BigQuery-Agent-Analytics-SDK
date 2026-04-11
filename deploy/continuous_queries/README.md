# Continuous Queries Deployment

Run real-time evaluation and alerting as agent events arrive in BigQuery using
continuous queries.

## What It Does

Continuous queries run perpetually against a BigQuery table, processing new rows
as they are inserted. The templates in this directory apply SDK evaluation logic,
error detection, and alerting in real time.

## Prerequisites

- A BigQuery Enterprise or Enterprise Plus reservation with continuous query
  slots — see [setup_reservation.md](setup_reservation.md) for setup
- A BigQuery dataset containing `agent_events`

> **Cost note:** Enterprise reservations are billed per slot-hour (100 slots
> ≈ $6/hour). For a lower-cost alternative, see
> [streaming_evaluation/](../streaming_evaluation/).

## Templates

| Template | Description |
|----------|-------------|
| [session_scoring.sql](session_scoring.sql) | Live session quality scoring |
| [realtime_error_analysis.sql](realtime_error_analysis.sql) | Real-time error detection and classification |
| [pubsub_alerting.sql](pubsub_alerting.sql) | Pub/Sub event alerting |
| [bigtable_dashboard.sql](bigtable_dashboard.sql) | Bigtable metrics aggregation for dashboards |

## Activate a Template

1. Set up the reservation ([setup_reservation.md](setup_reservation.md))
2. Replace `PROJECT`, `DATASET`, and `REGION` placeholders in the chosen SQL file
3. Run with the continuous flag:

```bash
bq query --use_legacy_sql=false --continuous=true < session_scoring.sql
```

## Files

| File | Description |
|------|-------------|
| `setup_reservation.md` | Enterprise reservation setup guide |
| `session_scoring.sql` | Session quality scoring template |
| `realtime_error_analysis.sql` | Error detection template |
| `pubsub_alerting.sql` | Pub/Sub alerting template |
| `bigtable_dashboard.sql` | Bigtable aggregation template |
