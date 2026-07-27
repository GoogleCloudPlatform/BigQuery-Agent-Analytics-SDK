# Viewer rendering and viewport support

This document defines how to validate the published BigQuery Agent Analytics
Data Studio dashboard as a rendered product. It governs issue
[#381](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/381)
and complements the query, parity, and live-template contracts.

## Chart implementation boundary

The canonical report uses Data Studio's native bar, time-series, scorecard, and
table components. It does not use Vega or third-party community
visualizations.

Native charts can still render inside Google-owned `usercontent.goog` frames.
The presence or lifecycle of one of those frames is not enough to classify a
chart as a community visualization. A root-cause claim must be supported by
the report's editor configuration or its **Resource → Manage community
visualizations** inventory.

Do not replace chart types, bindings, or filters in response to a blank frame
until the failure reproduces under the protocol below. The report uses
Viewer's Credentials; generated copies must also pass the credential gate
before they are shared.

## Rendering reliability protocol

A page title, report toolbar, loading indicator, or empty chart container is
not a chart-completion signal. A page passes only after every expected chart
has one of these outcomes:

1. non-degenerate rendered output with dimensions, measures, and labels
   appropriate to the fixture; or
2. an explicit legitimate empty state produced by a scenario whose expected
   result is empty.

For every release-candidate visual pass:

1. Record the browser and version, signed-in state, extensions that can affect
   requests, viewport in CSS pixels, and whether the load is cold or warm.
2. Run at least three fresh report loads.
3. In each run, visit all eight pages in report order, then repeat the complete
   navigation loop three times without reloading.
4. Allow up to 90 seconds per page for non-degenerate chart output. Record the
   observed time to render; do not use disappearance of page chrome or a
   loading indicator as the timer's success condition.
5. Capture every page after it passes. For a failure, capture the page at the
   timeout and record failed network requests and the corresponding BigQuery
   job activity.
6. Verify the native chart bindings in the editor before changing the report.
   An empty frame alone does not establish lost bindings.

A rendering defect is reproducible when the same chart remains blank after the
90-second timeout in at least two fresh runs, or when a deterministic
navigation sequence produces the same failure in at least two loops. A single
mid-render screenshot is not sufficient.

## Viewport support

Version 1 is a freeform, desktop report:

- minimum supported viewport width: 1280 CSS pixels;
- recommended viewport width: 1440 CSS pixels;
- phone and narrow-tablet layouts are not supported.

The configuration website remains responsive, but that does not make the
external Data Studio report responsive. Freeform pages intentionally preserve
the reviewed component geometry.

Data Studio only supports direct freeform-to-responsive conversion when a page
has at most one component. Every dashboard page has multiple components, so
mobile support requires a separately owned responsive report. That report
must independently pass chart parity, Linking API hydration, Viewer
Credentials, query-cost, and phone/tablet visual validation before it can
replace or accompany the v1 template.

## Issue triage

Keep rendering reliability and responsive-layout work separate:

- intermittent blank native charts are a reliability investigation against the
  existing report;
- mobile support is a new template and product-support decision.

Neither workstream should be represented as a Vega/community-visualization
defect without evidence from the report resource inventory.
