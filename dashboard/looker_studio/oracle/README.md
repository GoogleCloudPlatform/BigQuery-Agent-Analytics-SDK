# Independent dashboard query oracle

The 37 SQL files under `queries/` map one-to-one to the chart IDs in
`../spec/chart_manifest.yaml`. They are independent translations of the
pinned Looker block semantics and do not import or render the production
`events_v1.sql.tmpl`.

They serve two purposes:

1. make every chart's metric, filter, sort, and limit contract reviewable;
2. let `../tools/validate_live_bqaa.py` compile and execute all chart
   contracts against a caller's BQAA-generated views without collecting
   result values.

The dashboard itself has one embedded custom-query data source. These files
are validation artifacts, not 37 additional Looker Studio data sources.
