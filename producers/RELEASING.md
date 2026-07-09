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

The workflow takes over from there. Automated stages run in ~10
minutes; the release then WAITS at two manual approval gates
(`release-promote`, then `pypi`) — see the gate section below.

## What CI does (issue #349 release contract)

1. **`verify`** — confirms the tag matches `pyproject.toml`, then
   runs the producer test suite on Python 3.12.
2. **`build-image`** — builds the receiver image from the repo-root
   Dockerfile, **self-tests it before anything is pushed** (packaged
   version must equal the tag; both Cloud Run entrypoint factories
   must import), pushes it to the **private** staging repo
   (`us-docker.pkg.dev/bqaa-releases/bqaa-staging/otlp-receiver`) with
   a `<version>-candidate.<run_id>` tag, and captures the digest.
   Auth is Workload Identity Federation — no stored keys.
3. **`build`** — injects the pinned public image reference
   (`us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:<version>@sha256:…`)
   into `_release.py` **before** `python -m build`, so wheel and sdist
   both embed it; builds the plugin tarball; runs the mechanical
   digest-equality gate (`scripts/release_image_tool.py verify`);
   writes `SHA256SUMS`.
4. **`github-release`** — creates a **draft** release with all
   artifacts + checksums and the pinned image reference in the body.
   Nothing is customer-visible yet.
5. **`publish-testpypi`** — uploads wheel + sdist to TestPyPI via
   Trusted Publishing. No `skip-existing`: if the version already
   exists there, the upload fails loudly — that version is burned
   (see below).
6. **`promote`** — waits on the `release-promote` environment. Run
   the TestPyPI full-lifecycle gate BEFORE approving (see next
   section). On approval: crane-copies the staging digest to the
   public coordinate (content-addressed — the digest the artifacts
   embed is unchanged), asserts public digest == packaged constant,
   and publishes the draft release.
7. **`publish-pypi`** — PyPI, gated by the `pypi` environment;
   requires `promote`.

The plugin tarball and `SHA256SUMS` are **never** uploaded to PyPI —
they ship only as GitHub release assets.

## The TestPyPI full-lifecycle gate (before approving `release-promote`)

In a clean venv on a machine with NO repo checkout:

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            bigquery-agent-analytics-tracing==${VERSION}
bqaa-otel bootstrap --preflight ...
bqaa-otel bootstrap ... --image <staging-ref>@sha256:<digest> --execute
bqaa-otel verify --smoke ...
bqaa-otel teardown ...
```

(The staging ref + digest are in the `build-image` job output.) All
green → approve `release-promote`, then run the post-promotion smoke
WITHOUT `--image` (embedded public default), then approve `pypi`.
Post evidence on the release issue.

## Version-burn rule

TestPyPI and PyPI must carry **byte-identical artifacts at the same
version**. If a candidate fails the gate, that version is burned
everywhere: bump the version, re-tag, rebuild from scratch. Never
re-upload, never re-tag an image (staging and public tags are
immutable — enforced at the repository level).

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
  not configured on the TestPyPI side. Configure TestPyPI then re-run
  the `publish-testpypi` job.
- **`publish-testpypi` fails with "version already exists"** — that
  version was burned by an earlier candidate. Bump, re-tag, rebuild.
- **`build-image` fails WIF auth** — check the pool/provider and the
  `bqaa-release-publisher` SA bindings in `bqaa-releases` (see the
  workflow `env:` block for the exact resource names).
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

GitHub-side one-time setup: create environments `testpypi`, `pypi`,
and `release-promote` in the repo settings, with required reviewers
on `pypi` and `release-promote` (approving `release-promote` asserts
the TestPyPI full-lifecycle gate passed).

Until both publishers are configured, the `publish-testpypi` and
`publish-pypi` jobs will fail with a clear error. The `build` and
`github-release` jobs are independent and will still complete, so
the tag stays valid.
