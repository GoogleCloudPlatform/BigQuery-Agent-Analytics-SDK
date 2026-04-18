# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SDK-emitted label support for BigQuery jobs.

Every query or load job the SDK submits should carry labels that identify
which SDK feature ran it. Labels surface in
``INFORMATION_SCHEMA.JOBS_BY_PROJECT`` and let operators track SDK usage,
cost, and adoption without a separate telemetry pipeline.

See issue #52 for design context.
"""

from importlib import metadata
import logging
import re

from google.cloud import bigquery

# BigQuery label values accept only lowercase letters, digits, hyphens, and
# underscores, up to 63 characters. Enforcing this locally catches PII leaks
# (emails, uppercase IDs, long opaque strings) before they reach the API.
_LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{1,63}$")

_SDK_NAME = "bigquery-agent-analytics"

# Keys the LabeledBigQueryClient overrides unconditionally at dispatch time.
# ``sdk_feature`` and ``sdk_ai_function`` are also SDK-reserved but are set
# by ``with_sdk_labels`` at the call site, so the client does not treat
# their presence as a user-override conflict.
_SDK_DEFAULT_KEYS = frozenset({"sdk", "sdk_version", "sdk_surface"})

_logger = logging.getLogger("bigquery_agent_analytics.telemetry")


def _read_version():
  """Return an SDK version string that is always a valid BigQuery label.

  PEP 440 versions can contain characters BigQuery labels reject —
  ``+`` (local metadata), ``!`` (epoch), ``.`` (separator). Normalize
  anything outside ``[a-z0-9_-]`` to ``-``, trim to 63 chars, and fall
  back to ``"unknown"`` if the result still fails validation.
  """
  try:
    raw = metadata.version(_SDK_NAME)
  except metadata.PackageNotFoundError:
    return "unknown"
  normalized = re.sub(r"[^a-z0-9_-]", "-", raw.lower())[:63]
  if not _LABEL_VALUE_RE.fullmatch(normalized):
    return "unknown"
  return normalized


_VERSION = _read_version()


def _validate_label_value(name, value):
  if not _LABEL_VALUE_RE.fullmatch(value):
    raise ValueError(
        f"SDK label {name}={value!r} violates BigQuery label format "
        f"[a-z0-9_-]{{1,63}}. Reserve labels for stable, non-PII dimensions."
    )


def _ensure_sdk_defaults(cfg, *, surface):
  """Stamp ``sdk``, ``sdk_version``, ``sdk_surface`` defaults.

  Used by ``LabeledBigQueryClient`` at dispatch time. Preserves any
  ``sdk_feature`` or ``sdk_ai_function`` already set by ``with_sdk_labels``
  at the call site — those are SDK-authored and legitimate.

  Warns (and overrides) if a caller pre-set ``sdk``, ``sdk_version``, or
  ``sdk_surface`` — those are the unconditional defaults that keep
  telemetry trustworthy.
  """
  _validate_label_value("surface", surface)
  cfg = cfg or bigquery.QueryJobConfig()
  existing = dict(cfg.labels or {})

  conflicts = sorted(k for k in existing if k in _SDK_DEFAULT_KEYS)
  if conflicts:
    _logger.warning(
        "Caller set reserved SDK label keys %s; SDK values will override.",
        conflicts,
    )

  cfg.labels = {
      **existing,
      "sdk": _SDK_NAME,
      "sdk_version": _VERSION,
      "sdk_surface": surface,
  }
  return cfg


def with_sdk_labels(cfg, *, feature, ai_function=None):
  """Apply SDK feature labels to a job config, in the caller's thread.

  Call this before dispatching work via ``loop.run_in_executor`` so the
  label values materialize on the config object before it crosses the
  thread boundary. ``sdk_surface`` is added later by
  ``LabeledBigQueryClient`` at dispatch time.

  Args:
    cfg: The job config to enrich. Required (not ``None``) — pass a
      ``bigquery.QueryJobConfig`` for query sites or a
      ``bigquery.LoadJobConfig`` for load sites. The caller owns the
      type so labels always land on a config matching the job about
      to be submitted.
    feature: Stable identifier for the SDK subsystem emitting the job
      (for example ``"trace-read"``, ``"eval-llm-judge"``). Must match
      ``[a-z0-9_-]{1,63}``.
    ai_function: Optional AI/ML function dimension, for queries that
      invoke ``AI.GENERATE`` / ``AI.EMBED`` / etc.

  Returns:
    The same ``cfg`` with ``sdk_feature`` (and optionally
    ``sdk_ai_function``) set.
  """
  if cfg is None:
    raise TypeError(
        "with_sdk_labels() requires a cfg argument; construct a "
        "QueryJobConfig or LoadJobConfig at the call site so the "
        "correct job type is labeled."
    )
  _validate_label_value("feature", feature)
  if ai_function is not None:
    _validate_label_value("ai_function", ai_function)

  existing = dict(cfg.labels or {})
  existing["sdk_feature"] = feature
  if ai_function is not None:
    existing["sdk_ai_function"] = ai_function
  cfg.labels = existing
  return cfg


class LabeledBigQueryClient(bigquery.Client):
  """``bigquery.Client`` that stamps SDK default labels on every job.

  ``sdk_surface`` is stored as an instance attribute so it survives thread
  boundaries — reads from a ``ThreadPoolExecutor`` worker see the value set
  at construction time, unlike a ``ContextVar`` which would not propagate
  through ``loop.run_in_executor``.

  Per-call ``sdk_feature`` and ``sdk_ai_function`` labels are applied by
  call sites via ``with_sdk_labels()`` before the job is dispatched.
  """

  def __init__(self, *args, sdk_surface="python", **kwargs):
    _validate_label_value("surface", sdk_surface)
    super().__init__(*args, **kwargs)
    self._sdk_surface = sdk_surface

  def query(self, query, job_config=None, **kwargs):
    job_config = _ensure_sdk_defaults(job_config, surface=self._sdk_surface)
    return super().query(query, job_config=job_config, **kwargs)

  def query_and_wait(self, query, job_config=None, **kwargs):
    job_config = _ensure_sdk_defaults(job_config, surface=self._sdk_surface)
    return super().query_and_wait(query, job_config=job_config, **kwargs)

  def load_table_from_json(
      self, json_rows, destination, job_config=None, **kwargs
  ):
    job_config = _ensure_sdk_defaults(
        job_config or bigquery.LoadJobConfig(),
        surface=self._sdk_surface,
    )
    return super().load_table_from_json(
        json_rows, destination, job_config=job_config, **kwargs
    )


def make_bq_client(project, location=None, sdk_surface="python", **kwargs):
  """Construct a ``LabeledBigQueryClient`` with SDK defaults applied.

  Args:
    project: GCP project ID.
    location: Optional BigQuery location.
    sdk_surface: Which SDK entry point is constructing this client. One of
      ``"python"``, ``"cli"``, ``"remote-function"``. Drives the
      ``sdk_surface`` label on every job this client submits.
    **kwargs: Forwarded to ``bigquery.Client`` (e.g. ``credentials``).

  Returns:
    A ``LabeledBigQueryClient`` that stamps SDK defaults on every job.
  """
  if location is not None:
    kwargs["location"] = location
  return LabeledBigQueryClient(
      project=project, sdk_surface=sdk_surface, **kwargs
  )
