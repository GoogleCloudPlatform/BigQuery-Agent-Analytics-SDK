# Viewer rendering and viewport support

This document defines how to validate the published BigQuery Agent Analytics
Looker Studio dashboard as a rendered product. It governs issue
[#381](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/381)
and complements the query, parity, and live-template contracts.

## Chart implementation boundary

The canonical report uses Looker Studio's native bar, time-series, scorecard,
and table components. It does not use Vega or third-party community
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

### Observed baseline

The 2026-07-27 live pass at a 1568 CSS-pixel viewport did not reproduce the
blank-chart defect:

- the cold load was still blank at 40 seconds and fully rendered by 70 seconds;
- navigating away and returning rendered within 10 seconds;
- SPA navigation to a second page rendered within 18 seconds; and
- two complete render cycles made zero requests to `usercontent.goog` or a
  community-visualization resource.

The cold-load analytics beacon reported a cache view miss. These values are an
environment-specific comparison baseline, not an SLA and not a substitute for
the repeated-load acceptance protocol.

## Viewport support

Version 1 is a freeform, desktop report:

- minimum supported viewport width: 1280 CSS pixels;
- recommended viewport width: 1440 CSS pixels;
- phone and narrow-tablet layouts are not supported.

The configuration website remains responsive, but that does not make the
external Looker Studio report responsive. Freeform pages intentionally
preserve the reviewed component geometry.

Looker Studio only supports direct freeform-to-responsive conversion when a
page has at most one component. Every dashboard page has multiple components,
so mobile support requires a separately owned responsive report. That report
must independently pass chart parity, Linking API hydration, Viewer
Credentials, query-cost, and phone/tablet visual validation before it can
replace or accompany the v1 template.

The 1280-pixel minimum is a documented product-support boundary. The latest
live pass ran at 1568 pixels, so targeted visual validation at 1280 pixels
remains pending.

Viewport width does not prove that freeform components remain inside the page
canvas. Release-candidate validation must also capture the editor's page height
and every component's top and height, then verify:

```text
component top + component height <= page height - 24 px
```

Issue
[#388](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/388)
identified a containment defect on Token Consumption and Latency. Both pages
were resized and republished on 2026-07-29, and a published-version probe
verified the rule holds (31 px and 30 px of bottom padding). Linking API
copies created before that date retain the old geometry; a fresh copy is
required to pick up the fix. Because the report is mutable, later geometry
edits require reverification under this protocol.

## Issue triage

Keep rendering reliability and responsive-layout work separate:

- intermittent blank native charts are a reliability investigation against the
  existing report;
- mobile support is a new template and product-support decision.

Neither workstream should be represented as a Vega/community-visualization
defect without evidence from the report resource inventory.
