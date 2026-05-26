# Releasing `bigquery-agent-analytics-tracing`

This document covers cutting a release of the producer package and the
Claude Code plugin tarball. Both ride the same version on the same
tag, so users always know which wheel and which plugin go together.

Release pipeline lives in
[`.github/workflows/release-tracing.yml`](../.github/workflows/release-tracing.yml).
The tag namespace (`tracing-vX.Y.Z`) is distinct from the root SDK's
`vX.Y.Z` tags so the two pipelines never collide.

## Pre-flight

1. `producers/pyproject.toml` `[project].version` is the version
   you're about to release.
2. `producers-ci.yml` is green on `main` for the commit you're
   tagging.
3. Any user-facing changes since the prior release have a one-line
   note ready for the GitHub release body (the workflow uses
   `generate_release_notes: true`; supplement with handwritten notes
   only if needed).
4. PyPI side: project + Trusted Publisher are already configured.
   Setup is one-time per project — see "PyPI Trusted Publishing
   setup" below.

## Cut the release

Run from a clean checkout of `main`:

```bash
git checkout main
git pull --ff-only
VERSION=$(python -c "import tomllib; print(tomllib.load(open('producers/pyproject.toml','rb'))['project']['version'])")
echo "Tagging tracing-v${VERSION}"
git tag -a "tracing-v${VERSION}" -m "tracing ${VERSION}"
git push origin "tracing-v${VERSION}"
```

The workflow takes over from there. Total wall-clock is ~5 minutes.

## What CI does

1. **`verify`** — confirms the tag matches `pyproject.toml`, then
   runs the producer test suite on Python 3.12.
2. **`build`** — `python -m build` for wheel + sdist, then
   `python scripts/build_claude_plugin.py` for the plugin tarball.
   Verifies all three expected artifacts landed in `dist/`.
3. **`github-release`** — creates the GitHub release for the tag with
   auto-generated notes and attaches all three artifacts.
4. **`publish-testpypi`** — uploads wheel + sdist to TestPyPI via
   Trusted Publishing (no secrets). The plugin tarball is stripped
   from the upload set before this step.
5. **`publish-pypi`** — same, to PyPI. Gated by the `pypi`
   environment, so maintainers can require manual approval here
   without blocking the GitHub release.

The plugin tarball is **never** uploaded to PyPI — it ships only as a
GitHub release asset.

## Verifying the release

```bash
# TestPyPI install
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            bigquery-agent-analytics-tracing==${VERSION}

# PyPI install (after publish-pypi job completes)
pip install bigquery-agent-analytics-tracing==${VERSION}

# Sanity check
python -c "from bigquery_agent_analytics_tracing import __version__; print(__version__)"
# → ${VERSION}

# Claude Code plugin tarball
curl -L https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/tracing-v${VERSION}/bigquery-agent-analytics-tracing-claude-code-${VERSION}.tar.gz \
  -o /tmp/plugin.tar.gz
tar -tzf /tmp/plugin.tar.gz | head
```

## If something goes wrong

- **`verify` fails on version mismatch** — the tag must equal
  `tracing-v$(python -c "import tomllib; print(tomllib.load(open('producers/pyproject.toml','rb'))['project']['version'])")`.
  Re-tag and re-push.
- **`build` fails on missing plugin tarball** — the build script's
  `importlib.metadata.version` lookup probably hit
  `PackageNotFoundError`; the `Install built wheel` step should have
  prevented this. Check that step's logs and rebuild.
- **`publish-testpypi` fails with OIDC error** — Trusted Publisher is
  not configured on the TestPyPI side. The GitHub release is still
  good; configure TestPyPI then re-run the `publish-testpypi` job.
- **`publish-pypi` fails the same way** — same fix on the PyPI side.

If a release ships a broken artifact, do **not** delete the tag.
Yank the PyPI release and ship the next patch version
(e.g. `tracing-v0.1.0` → `tracing-v0.1.1`). Avoid PEP 440 local
version identifiers (the `+local` suffix) — PyPI rejects them on
upload.

## PyPI Trusted Publishing setup (one-time)

Open <https://pypi.org/manage/account/publishing/> (and the same on
TestPyPI) and add a publisher with:

| Field | Value |
|---|---|
| PyPI project name | `bigquery-agent-analytics-tracing` |
| Owner | `GoogleCloudPlatform` |
| Repository | `BigQuery-Agent-Analytics-SDK` |
| Workflow filename | `release-tracing.yml` |
| Environment | `pypi` (or `testpypi` on TestPyPI) |

These names must match exactly — the `environment:` blocks in the
workflow are the binding contract.

Until both publishers are configured, the `publish-testpypi` and
`publish-pypi` jobs will fail with a clear error. The `build` and
`github-release` jobs are independent and will still complete, so
the tag stays valid.
