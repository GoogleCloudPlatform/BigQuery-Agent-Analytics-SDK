# Intent — PR #486 residual P2: complete NotFound resource tokens

**PR:** https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/486  
**Threads:**  
- https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/486#discussion_r4052121395 (prefix / case)  
- https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/486#discussion_r4057920520 (spaces in identifiers)  
**Branch:** `fix/485-readme-and-not-found` (closes #485)

## Problem
`_not_found_names_source` previously used lowercase substring matching, then `[^\s,;]+` complete-token matching. Remaining gaps:
- prefix collisions (`agent_events` ⊂ `agent_events_archive`, `ds` ⊂ `ds_archive`)
- case-distinct identifiers in case-sensitive BigQuery datasets
- **spaces inside table identifiers** (`agent_events` ⊂ `agent_events archive`), because `[^\s,;]+` truncates at the first space

Callers then get `EventsTableNotFoundError` naming the wrong (configured) source when a *different* resource is missing — e.g. a view dependency.

## Goal
Translate `NotFound` → `EventsTableNotFoundError` only when the error names the **exact** queried Table or Dataset token, case-preserved, including spaces. Prefer capturing through the BQ terminator ` was not found`. Keep job-not-found passthrough. No merge.
