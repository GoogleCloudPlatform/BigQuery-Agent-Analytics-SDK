# Decision: oracle determinism additions (frozen intentional divergences)

The pinned block leaves two query behaviors engine-ordered, which makes
reproducible expected results impossible. The oracle (and the M2 report,
which must match it) freezes both as **intentional divergences from the
pinned query contract**, each an upstream candidate for the block:

1. **Unsorted LIMIT gets an explicit sort.** "Top 5 users with most Tokens
   consumption" has `LIMIT 5` and no `sorts` in the pinned LookML — a live
   render selects five engine-ordered rows. The oracle orders by the
   tile's measures descending (matching the visible "top N" intent), then
   the dimension tie-breakers below.
2. **Deterministic tie-breakers.** Every dimension not already in a tile's
   sort is appended to its ORDER BY. When ties straddle a top-N cutoff,
   the selected set can differ from a live block render.

Both are recorded in the query headers and encoded in explicit `ORDER BY`
clauses, making the validation result order deterministic.
