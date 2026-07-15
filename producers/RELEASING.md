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
   note ready for the GitHub release body (the workflow renders
   the curated customer-first template via
   `scripts/render_release_notes.py`; generic generated notes are
   disabled — the repo-wide tag stream would pull unrelated SDK PRs).
4. PyPI side: a **pending Trusted Publisher** is registered on BOTH
   indexes (for the first release neither project exists yet — the
   pending publisher creates it on the first trusted upload and does
   not reserve the name before then; for later releases the existing
   project publishers suffice). Setup is one-time per project — see
   "PyPI Trusted Publishing setup" below.

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
   a `<version>-candidate.<run_id>.<run_attempt>` tag, and captures the digest.
   Auth is Workload Identity Federation — no stored keys.
3. **`build`** — injects the pinned public image reference
   (`us-docker.pkg.dev/bqaa-releases/bqaa/otlp-receiver:<version>@sha256:…`)
   into `_release.py` **before** `python -m build`, so wheel and sdist
   both embed it; builds the plugin tarball; runs the mechanical
   digest-equality gate (`scripts/release_image_tool.py verify`);
   writes `SHA256SUMS`.
4. **`github-release`** — creates a **draft** release with all
   artifacts + checksums and the pinned image reference in the body.
   Nothing is customer-visible yet. The guard
   (`scripts/guard_existing_release.py`) **never deletes a release**:
   GitHub has no conditional delete, so a GET-then-DELETE pair races
   publication. It first classifies the world: drafts are discovered
   via the authenticated release LIST (the by-tag endpoint returns
   only published releases), and each index is byte-validated against
   the job's own dist files — `absent` (fresh cache-busted explicit
   404; PyPI responses are CDN-cached), `exact` (the index holds
   exactly the current bytes, i.e. an original-attempt rerun), or
   `deviating` (a full rerun's rebuilt bytes, or anything tampered —
   the version is burned). A stale draft with both indexes absent
   BLOCKS the job with instructions to verify the draft is still
   unpublished, delete it manually, and re-run; once an index accepted
   files, the draft is their only byte-identical counterpart, so it is
   preserved and the failed jobs must be re-run from the **original**
   workflow attempt — never a full rerun. The one automatic recovery:
   no release + production absent + TestPyPI byte-exact recreates the
   missing draft (the upload stages are already satisfied).
5. **`publish-testpypi`** — uploads wheel + sdist to TestPyPI via
   Trusted Publishing. No `skip-existing`: if the version already
   exists there with ANY deviation, the job fails loudly — that
   version is burned (see below). Rerun safety: a PR-tested pre-check
   (`scripts/check_index_publication.py`) first compares the index
   against the local files, so a rerun after a lost upload response
   passes without re-uploading when the index already carries the
   EXACT byte-identical wheel + sdist. `publish-pypi` runs the same
   gate against production.
6. **`promote`** — waits on the `release-promote` environment
   (restricted to `tracing-v*` tags; impersonates the promoter SA,
   the only identity that can write the public repo). Run the
   TestPyPI full-lifecycle gate BEFORE approving (see next section).
   On approval: crane-copies the staging digest to the public
   coordinate (idempotent — an existing tag is accepted only with the
   exact expected digest) and asserts public digest == packaged
   constant. It does NOT publish the release.
7. **`publish-pypi`** — PyPI, gated by the `pypi` environment;
   requires `promote`.
8. **`finalize`** — runs `always()` but is gated on `github-release`
   succeeding (when the rerun guard deliberately fails, its "rerun the
   original attempt" advice must stay authoritative — finalize must
   not reconcile the preserved draft against a rebuilt anchor):
   reconciles the PyPI **and** TestPyPI file sets, the four release
   assets, and the release's **visibility** (a draft accidentally
   published during an approval pause is classified
   `premature-publication` — contain it first, never "keep the
   draft"); classifies the exact release object FIRST (an already
   public mutable release fails with burn guidance immediately), then
   verifies the repository's **immutable-releases setting is enabled
   BEFORE publishing a draft** (GitHub does not apply immutability
   retroactively, so a release published while the setting is off
   stays permanently mutable and can only be burned) — that policy
   read requires repository **Administration: read**, which the
   workflow `GITHUB_TOKEN` can never carry, so it uses a dedicated
   GitHub App installation token (see one-time setup below) while
   every other API call keeps the job token;
   publishes ONLY on complete state via a snapshot-bound, **ID-based**
   edit — the exact release object's asset digests AND canonical
   title/body/prerelease are re-verified against the anchor
   immediately before and after the publish (never as the repository's
   Latest); a partial
   publication is surfaced with the yank/version-burn recovery, and a
   partial TestPyPI upload (production absent) is surfaced as
   `testpypi-partial` — the version is burned there, so bump + re-tag
   rather than retrying the same version.

The plugin tarball and `SHA256SUMS` are **never** uploaded to PyPI —
they ship only as GitHub release assets.

## The TestPyPI full-lifecycle gate (before approving `release-promote`)

In a clean venv on a machine with NO repo checkout. Never mix indexes
(pip gives --index-url/--extra-index-url NO priority — a dependency
squatter on TestPyPI could be chosen): download the exact candidate
with --no-deps, checksum it against the release SHA256SUMS, install
dependencies from real PyPI, then install the verified local file.

```bash
GATE=$(mktemp -d) && python -m venv "$GATE/venv" && source "$GATE/venv/bin/activate"
WHEEL="bigquery_agent_analytics_tracing-${VERSION}-py3-none-any.whl"
pip download --no-deps --index-url https://test.pypi.org/simple/ \
  --dest "$GATE" bigquery-agent-analytics-tracing==${VERSION}
# Mechanical integrity gate: compare against the SHA256SUMS the build
# attested to the draft release — not an eyeball check.
gh release download "tracing-v${VERSION}" \
  --repo GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --pattern SHA256SUMS --dir "$GATE"
(cd "$GATE" && grep " ${WHEEL}$" SHA256SUMS | sha256sum -c -)
pip install "$GATE/${WHEEL}"       # deps resolve from real PyPI
bqaa-otel bootstrap --preflight ...
bqaa-otel bootstrap ... --image <staging-ref>@sha256:<digest> --execute
bqaa-otel verify --smoke ...
bqaa-otel teardown ... --confirm   # WITH --confirm: the gate evidence must
                                   # show real deletion + existence PASSes,
                                   # not a dry-run plan
```

(The staging ref + digest are in the `build-image` job output.) All
green → approve `release-promote`, then run the post-promotion smoke
WITHOUT `--image` (embedded public default), then approve `pypi`.
Post evidence on the release issue.

## Version-burn rule

TestPyPI and PyPI must carry **byte-identical artifacts at the same
version**. Not every failure burns the version — distinguish:

- **Burned**: a lifecycle-gate failure caused by defective candidate
  bytes after TestPyPI accepted them, ANY index deviation from the
  anchor (subset/extra/yanked/digest mismatch), or any reconciler
  burn state (`empty-release`, `testpypi-partial`, `partial`). Bump
  the version, rebuild, re-tag. Never re-upload, never re-tag an
  image (staging and public tags are immutable — enforced at the
  repository level).
- **NOT burned**: a transient job failure, a lost upload response, or
  a `finalize` failure while the index carries the EXACT anchor bytes
  — re-run the failed jobs from the **original** workflow attempt
  (the rerun-safe pre-checks recognize the byte-identical publication
  and pass without re-uploading). A full rerun is never the recovery:
  rebuilt bytes cannot match, which is itself a burn.

## Verifying the release

```bash
# TestPyPI verification happens ONLY via the checksum-gated procedure in
# the lifecycle-gate section above (never mixed indexes).

# PyPI install (after publish-pypi + finalize complete)
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
- **`finalize` fails transiently after PyPI accepted the upload** —
  re-run the failed `finalize` job from the **original** workflow
  attempt (Actions → the run → "Re-run failed jobs"). Never trigger a
  full rerun: it rebuilds the wheel/sdist with new timestamps, and the
  guard in `github-release` will refuse to touch the preserved draft
  because the indexes already hold the original bytes.
- **`publish-testpypi`/`publish-pypi` fails after the index accepted
  the upload (lost response)** — re-run the failed job from the
  original attempt: the pre-check verifies the index already carries
  the exact byte-identical files and skips the re-upload.
- **`finalize` refuses to publish because immutable releases are
  disabled** — the GitHub release remains a draft, but PyPI and
  TestPyPI already contain the exact files (the index stages run
  before `finalize`). Enable immutable releases (Settings → General →
  Releases) and re-run `finalize` from the **original** workflow
  attempt; never trigger a full rerun.
- **a release was somehow published while the setting was off** —
  immutability is NOT retroactive: enabling the setting cannot protect
  it, so treat the version as burned (delete the release, yank any
  index files, bump, re-tag) and re-release with the setting enabled.

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
and `release-promote` in the repo settings —

- restrict **all three** environments to tag `tracing-v*`
  (Settings → Environments → Deployment branches and tags → Selected →
  Tag rule `tracing-v*`); the `testpypi` rule matters even without a
  reviewer, because it stops a branch-modified copy of the workflow
  from using the TestPyPI OIDC identity and burning a version;
- required reviewers on `pypi` and `release-promote` ONLY (approving
  `release-promote` asserts the TestPyPI full-lifecycle gate passed);
  `testpypi` gets no reviewer — the manual lifecycle verdict is the
  later `release-promote` approval —

and **enable immutable releases BEFORE the first release**
(Settings → General → Releases):
`finalize` verifies the setting and refuses to publish while it is
off, because GitHub applies immutability only at publish time — a
release published while the setting is off keeps mutable assets and
`SHA256SUMS` forever, replaceable by anything holding
`contents: write`.

Policy-read credential (one-time): the immutable-releases check calls
`GET /repos/{repo}/immutable-releases`, which requires repository
**Administration: read** — a permission the workflow `GITHUB_TOKEN`
can never be granted. Because the App requests repository
Administration permission, installation needs a **GoogleCloudPlatform
organization owner** — a repository admin alone cannot complete this
([installation requirements](https://docs.github.com/en/apps/using-github-apps/installing-a-github-app-from-a-third-party)):

1. An organization owner creates an **organization-owned** GitHub
   App.
2. Registration setting: **"Where can this GitHub App be
   installed?" → Only on this account** — keeps the App private so no
   other account can install it
   ([docs](https://docs.github.com/en/enterprise-cloud@latest/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private)).
3. Grant only repository **Administration: read**; disable webhooks;
   request no organization or account permissions.
4. Generate and download a private key for the App.
5. The organization owner installs it on **only**
   `BigQuery-Agent-Analytics-SDK`.
6. Store the App ID as the repository variable
   `BQAA_RELEASE_POLICY_APP_ID` and the PEM as the repository secret
   `BQAA_RELEASE_POLICY_APP_PRIVATE_KEY`. Then delete the downloaded
   workstation copy of the PEM unless it is retained in an approved
   secret manager, and record who owns future key rotation — an App
   private key can authenticate against every installation of that
   App ([best practices](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app)).

The workflow mints a short-lived installation token from these only
when a DRAFT publication is about to happen — idempotent reruns of an
already-published release perform no policy read and never touch the
App credentials, so a rotated key cannot break them or mask the
mutable-release burn guidance; all other API calls keep the standard
job token. Until they are configured, `finalize` fails at the token
mint with a clear error and the release stays a draft.

Until both publishers are configured, the `publish-testpypi` and
`publish-pypi` jobs will fail with a clear error. The `build` and
`github-release` jobs are independent and will still complete, so
the tag stays valid.
