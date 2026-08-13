# BigQuery Console URL Paste Design

## Goal

Extend the Looker Studio configurator's merged #405 paste behavior so a user
can paste a BigQuery table URL copied from either Google Console host and have
the project, dataset, and table fields populated.

Supported hosts are exactly:

- `pantheon.corp.google.com`
- `console.cloud.google.com`

## Accepted URL contract

An accepted URL must:

1. be an absolute HTTPS URL without user information or a non-default port;
2. use one of the two exact hostnames above;
3. use the exact `/bigquery` path;
4. contain exactly one `ws` query parameter; and
5. contain exactly one complete decoded
   `!1m5!1m4!4m3!1sPROJECT!2sDATASET!3sTABLE` table reference in `ws`.

`URL` and `URLSearchParams` perform percent-decoding before the workspace
reference is parsed. The extracted components must satisfy the existing
`PROJECT_RE`, `DATASET_RE`, and `TABLE_RE` validators.

Foreign hosts, HTTP URLs, unrelated paths, missing or repeated `ws`
parameters, incomplete or repeated table-reference markers, and invalid
identifiers are not recognized. An unrecognized URL follows ordinary browser
paste behavior and never fills the other identifier fields.

## Code structure

`docs/configurator.mjs` owns parsing. A pure
`parseBigQueryConsoleTableUrl(value)` function recognizes the URL contract.
`parseTableReference(value)` selects the URL parser for URL-shaped input and
otherwise delegates to the existing `splitQualifiedTableId(value)` behavior.
`parseTableReferenceForInput(value)` applies the existing stricter validation
used by the committed-input fallback.

`docs/app.mjs` uses the unified table-reference functions in the existing
paste and change handlers. Successful URL parsing therefore reuses the same
field assignment, validation, dashboard-link refresh, and success status as a
qualified ID. No fetch, navigation, or new dependency is introduced.

`docs/index.html` tells users that a fully qualified ID or BigQuery Console
table link can be pasted into any of the three identifier fields.

## Verification

`tools/test_web_configurator.mjs` covers:

- the reported Pantheon URL and the public Console equivalent;
- percent-encoded workspace markers;
- paste into each of the three identifier fields;
- the committed-input fallback;
- exact host, protocol, port, path, `ws`, marker, and identifier rejection;
- no partial field mutation for rejected URLs; and
- preservation of every existing qualified-ID and plain-input case.

The focused Node test and `tests/test_looker_studio_dashboard.py` must pass,
followed by the repository formatter/check script relevant to the touched
JavaScript and HTML files.
