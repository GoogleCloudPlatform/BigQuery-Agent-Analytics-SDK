# Intent — PR #486 residual P2: complete NotFound resource tokens

**PR:** https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/486  
**Thread:** https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/486#discussion_r4052121395  
**Branch:** `fix/485-readme-and-not-found` (closes #485)

## Problem
`_not_found_names_source` uses lowercase substring matching. That misclassifies:
- prefix collisions (`agent_events` ⊂ `agent_events_archive`, `ds` ⊂ `ds_archive`)
- case-distinct identifiers in case-sensitive BigQuery datasets

Callers then get `EventsTableNotFoundError` naming the wrong (configured) source when a *different* resource is missing — e.g. a view dependency.

## Goal
Translate `NotFound` → `EventsTableNotFoundError` only when the error names the **exact** queried Table or Dataset token, case-preserved. Keep job-not-found passthrough. No merge.
