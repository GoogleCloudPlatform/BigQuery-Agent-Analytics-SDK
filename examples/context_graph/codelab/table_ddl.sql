-- Node and edge table DDL for the BQAA codelab.
--
-- The materializer writes into these tables on every run.
-- ``session_id`` and ``extracted_at`` are SDK metadata columns the
-- materializer fills automatically; they are required on every
-- bound table.
--
-- Apply with:
--   envsubst < table_ddl.sql | bq query --use_legacy_sql=false
--
-- Required shell variables:
--   PROJECT_ID  : your GCP project ID
--   DATASET     : the BigQuery dataset that holds both raw agent_events
--                 and the materialized graph tables
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET}.decision_request` (
  request_id STRING, request_text STRING, requested_at TIMESTAMP,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET}.decision_option` (
  option_id STRING, option_label STRING, confidence FLOAT64,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET}.decision_outcome` (
  outcome_id STRING, status STRING, rationale STRING, decided_at TIMESTAMP,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET}.evaluates_option` (
  request_id STRING, option_id STRING,
  session_id STRING, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${DATASET}.resulted_in` (
  request_id STRING, outcome_id STRING,
  session_id STRING, extracted_at TIMESTAMP
);
