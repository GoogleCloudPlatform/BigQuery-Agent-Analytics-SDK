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

"""Shared GCS upload/download utilities for the skill-evolution job."""

from __future__ import annotations

import logging
import os

from . import config

logger = logging.getLogger(__name__)


def upload_dir_to_gcs(
    local_dir: str,
    bucket_name: str | None = None,
    prefix: str = "runs",
) -> dict:
  """Upload a local directory to GCS.

  Controlled by GCS_UPLOAD env var (default: false). When false, upload
  is skipped. The bucket comes from EVOLUTION_GCS_BUCKET, falling back
  to GCS_BUCKET.

  Args:
      local_dir: Local directory to upload.
      bucket_name: GCS bucket name. Defaults to the configured bucket.
      prefix: GCS path prefix.

  Returns:
      Dict with status, gcs_uri, and files_uploaded count.
  """
  if not os.path.isdir(local_dir):
    return {"error": f"Directory not found: {local_dir}"}

  gcs_upload = os.getenv("GCS_UPLOAD", "false").lower() in (
      "true",
      "1",
      "yes",
  )
  if not gcs_upload:
    return {
        "status": "skipped",
        "reason": "GCS_UPLOAD is not enabled. Set GCS_UPLOAD=true to enable.",
    }

  if bucket_name is None:
    bucket_name = config.get_config().evolution_gcs_bucket
  if not bucket_name:
    return {"error": "EVOLUTION_GCS_BUCKET (or GCS_BUCKET) not configured"}

  try:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    dir_name = os.path.basename(local_dir.rstrip("/"))
    gcs_prefix = f"{prefix}/{dir_name}"

    uploaded = 0
    for root, _dirs, files in os.walk(local_dir):
      for fname in files:
        local_path = os.path.join(root, fname)
        rel_path = os.path.relpath(local_path, local_dir)
        blob_path = f"{gcs_prefix}/{rel_path}"
        blob = bucket.blob(blob_path)
        blob.upload_from_filename(local_path)
        uploaded += 1

    gcs_uri = f"gs://{bucket_name}/{gcs_prefix}/"
    logger.info("Uploaded %d files to %s", uploaded, gcs_uri)

    return {
        "status": "success",
        "gcs_uri": gcs_uri,
        "bucket": bucket_name,
        "files_uploaded": uploaded,
    }

  except ImportError:
    return {
        "status": "error",
        "error": (
            "google-cloud-storage not installed. "
            "Install with: pip install google-cloud-storage"
        ),
    }
  except Exception as e:  # noqa: BLE001
    logger.error("GCS upload failed: %s", e, exc_info=True)
    return {"status": "error", "error": str(e)}


def download_from_gcs(gcs_uri: str, local_path: str) -> dict:
  """Download a file from GCS.

  Args:
      gcs_uri: GCS URI (gs://bucket/path/to/file).
      local_path: Local path to save the file.

  Returns:
      Dict with status and local_path.
  """
  if not gcs_uri.startswith("gs://"):
    return {"error": f"Invalid GCS URI: {gcs_uri}"}

  parts = gcs_uri[5:].split("/", 1)
  if len(parts) != 2:
    return {"error": f"Invalid GCS URI format: {gcs_uri}"}

  bucket_name, blob_path = parts

  try:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)

    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    blob.download_to_filename(local_path)

    logger.info("Downloaded %s to %s", gcs_uri, local_path)
    return {
        "status": "success",
        "gcs_uri": gcs_uri,
        "local_path": local_path,
        "size_bytes": os.path.getsize(local_path),
    }

  except ImportError:
    return {
        "status": "error",
        "error": "google-cloud-storage not installed.",
    }
  except Exception as e:  # noqa: BLE001
    logger.error("GCS download failed: %s", e, exc_info=True)
    return {"status": "error", "error": str(e)}
