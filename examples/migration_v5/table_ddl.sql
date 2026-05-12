CREATE TABLE `test-project-0728-467323.migration_v5_demo.agent_session` (
  id          STRING NOT NULL,
  session_id  STRING,
  started_at  TIMESTAMP,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.decision_point` (
  id             STRING NOT NULL,
  decision_type  STRING,
  decided_at     TIMESTAMP,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.candidate` (
  id               STRING NOT NULL,
  candidate_label  STRING,
  score            FLOAT64,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.selection_outcome` (
  id                     STRING NOT NULL,
  selected_candidate_id  STRING,
  rationale              STRING,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.context_snapshot` (
  id                  STRING NOT NULL,
  snapshot_payload    STRING,
  snapshot_timestamp  TIMESTAMP,
  PRIMARY KEY (id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.contains_decision_point` (
  from_id  STRING NOT NULL,
  to_id    STRING NOT NULL,
  -- TODO: uncomment if (from_id, to_id) is unique per row
  -- PRIMARY KEY (from_id, to_id) NOT ENFORCED,
  FOREIGN KEY (from_id) REFERENCES `test-project-0728-467323.migration_v5_demo.agent_session`(id) NOT ENFORCED,
  FOREIGN KEY (to_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point`(id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.has_candidate` (
  from_id  STRING NOT NULL,
  to_id    STRING NOT NULL,
  -- TODO: uncomment if (from_id, to_id) is unique per row
  -- PRIMARY KEY (from_id, to_id) NOT ENFORCED,
  FOREIGN KEY (from_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point`(id) NOT ENFORCED,
  FOREIGN KEY (to_id) REFERENCES `test-project-0728-467323.migration_v5_demo.candidate`(id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.has_outcome` (
  from_id  STRING NOT NULL,
  to_id    STRING NOT NULL,
  -- TODO: uncomment if (from_id, to_id) is unique per row
  -- PRIMARY KEY (from_id, to_id) NOT ENFORCED,
  FOREIGN KEY (from_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point`(id) NOT ENFORCED,
  FOREIGN KEY (to_id) REFERENCES `test-project-0728-467323.migration_v5_demo.selection_outcome`(id) NOT ENFORCED
);

CREATE TABLE `test-project-0728-467323.migration_v5_demo.has_context` (
  from_id  STRING NOT NULL,
  to_id    STRING NOT NULL,
  -- TODO: uncomment if (from_id, to_id) is unique per row
  -- PRIMARY KEY (from_id, to_id) NOT ENFORCED,
  FOREIGN KEY (from_id) REFERENCES `test-project-0728-467323.migration_v5_demo.decision_point`(id) NOT ENFORCED,
  FOREIGN KEY (to_id) REFERENCES `test-project-0728-467323.migration_v5_demo.context_snapshot`(id) NOT ENFORCED
);
