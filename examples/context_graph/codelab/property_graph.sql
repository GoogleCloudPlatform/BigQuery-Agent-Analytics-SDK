-- Property-graph DDL for the BQAA codelab.
--
-- Models a generic agent decision flow:
--   DecisionRequest -> evaluatesOption -> DecisionOption
--   DecisionRequest -> resultedIn       -> DecisionOutcome
--
-- Apply with:
--   envsubst < property_graph.sql | bq query --use_legacy_sql=false
--
-- Required shell variables:
--   PROJECT_ID  : your GCP project ID
--   DATASET     : the BigQuery dataset that holds both raw agent_events
--                 and the materialized graph tables (single-dataset shape)
CREATE OR REPLACE PROPERTY GRAPH `${PROJECT_ID}.${DATASET}.agent_decisions_graph`
  NODE TABLES (
    `${PROJECT_ID}.${DATASET}.decision_request` AS decision_request
      KEY (request_id)
      LABEL DecisionRequest PROPERTIES (request_id, request_text, requested_at),
    `${PROJECT_ID}.${DATASET}.decision_option` AS decision_option
      KEY (option_id)
      LABEL DecisionOption PROPERTIES (option_id, option_label, confidence),
    `${PROJECT_ID}.${DATASET}.decision_outcome` AS decision_outcome
      KEY (outcome_id)
      LABEL DecisionOutcome PROPERTIES (outcome_id, status, rationale, decided_at)
  )
  EDGE TABLES (
    `${PROJECT_ID}.${DATASET}.evaluates_option` AS evaluates_option
      KEY (request_id, option_id)
      SOURCE KEY (request_id) REFERENCES decision_request (request_id)
      DESTINATION KEY (option_id) REFERENCES decision_option (option_id)
      LABEL evaluatesOption,
    `${PROJECT_ID}.${DATASET}.resulted_in` AS resulted_in
      KEY (request_id, outcome_id)
      SOURCE KEY (request_id) REFERENCES decision_request (request_id)
      DESTINATION KEY (outcome_id) REFERENCES decision_outcome (outcome_id)
      LABEL resultedIn
  );
