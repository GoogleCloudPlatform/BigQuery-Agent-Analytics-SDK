# Source attribution

Copied verbatim from the `knowledge-catalog` repository (Apache-2.0, see
`LICENSE.md`), bundle `okf/bundles/acme_retail`, at commit
`31da799a9aef176df12e91abbd119ea9385b75ec` on 2026-09-05.

| File | SHA-256 |
|---|---|
| `gross-margin-period.md` | `5e96ae11835ad328ccc94d29ae4bc7cc40176758cbcbad63231d0461c1f8f0e7` |
| `sql_equality.py.txt` (verbatim bytes; `.txt` keeps the repo formatter off it) | `79477f129616163080b57fcf0f6824912403e2426aee68cad47fd9cdd0fc9b9b` |

`sql_equality.py` is retained only as the historical, metadata-only attester
for comparison. The spike does **not** use its regex canonicalization; the
executed artifact is compared byte-for-byte after allowlisted compilation.

The Acme Retail bundle is fictional sample data. `acme.*` table identifiers
are remapped by `publication.json` into the synthetic fixture dataset; the
derived publication does not impersonate the original bundle or invent human
approval.
