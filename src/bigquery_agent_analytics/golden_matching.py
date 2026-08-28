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

"""Golden Q&A matching -- the producer for golden-grounded judging.

#378 (U4) gave the SDK the CONSUMER side of answer-key grounding:
``Client.evaluate_categorical(per_session_context=...)`` with
``CategoricalContextSource.GOLDEN_EXPECTED_ANSWER``. This module is the
matching PRODUCER, extracted from ``scripts/quality_report.py``: embed the
session questions and the golden questions, match by cosine similarity, and
emit (a) a per-key judge-context mapping ready to pass as
``per_session_context`` and (b) per-key match metadata for reporting.

Keys are opaque to this module: pass exact ``ResolvedTraceSelector`` objects
on the BigQuery path (so context binds to resolved identities) or plain
session-id strings on local-conversation paths.

Embedding batches retry transient 429/503 errors with bounded exponential
backoff -- matching sits on the critical scoring path, and a single
unretried burst error would otherwise abort a whole run.
"""

import logging
import math
import os
import time

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-005")

# Default cosine-similarity threshold for matching a session question to a
# golden-Q&A entry.
DEFAULT_GOLDEN_THRESHOLD = 0.92


def embed_texts(texts, model=None, batch_size=50, max_attempts=5):
  """Embed *texts* for semantic similarity; returns L2-normalised vectors.

  Transient quota/availability errors (429/503) are retried per batch with
  exponential backoff — golden matching sits on the critical scoring path,
  and a single unretried burst error would otherwise abort a whole run.

  Raises:
    ValueError: if ``batch_size`` or ``max_attempts`` is not >= 1.
  """
  if batch_size < 1:
    raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
  if max_attempts < 1:
    raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")

  from google import genai
  from google.genai import types

  model = model or EMBEDDING_MODEL
  client = genai.Client()
  vectors = []
  for i in range(0, len(texts), batch_size):
    batch = texts[i : i + batch_size]
    for attempt in range(1, max_attempts + 1):
      try:
        resp = client.models.embed_content(
            model=model,
            contents=batch,
            config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY"),
        )
        break
      except Exception as exc:  # pylint: disable=broad-except
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code not in (429, 503) or attempt == max_attempts:
          raise
        delay = 2**attempt
        logger.warning(
            "Embedding batch hit transient %s; retrying in %ds (%d/%d)",
            code,
            delay,
            attempt,
            max_attempts - 1,
        )
        time.sleep(delay)
    for e in resp.embeddings:
      v = list(e.values)
      norm = math.sqrt(sum(x * x for x in v)) or 1.0
      vectors.append([x / norm for x in v])
  return vectors


def match_golden_qa(
    question_by_sid, golden_qa, threshold=DEFAULT_GOLDEN_THRESHOLD
):
  """Match session questions to golden Q&A by embedding cosine similarity.

  Args:
    question_by_sid: dict mapping an opaque evaluation key (normally an exact
        ``ResolvedTraceSelector`` on the BigQuery path, or a session id on the
        local-conversation path) to user question text.
    golden_qa: list of dicts with ``question`` and optional
        ``expected_answer``, ``topic``, ``expected_behavior``.
    threshold: minimum cosine similarity (0-1) for a match.

  Returns:
    (per_session_context, golden_metadata):
      - per_session_context preserves each input key and maps it to a
        judge-context string
        (expected answer and/or a "should decline" note).
      - golden_metadata preserves each input key and maps it to match details
        (matched flag,
        matched question, expected answer, topic, out_of_scope, similarity).

  Raises:
    ValueError: if ``threshold`` is not a finite value in [0.0, 1.0].
  """
  if not (math.isfinite(threshold) and 0.0 <= threshold <= 1.0):
    raise ValueError(
        f"threshold must be a finite value in [0.0, 1.0], got {threshold!r}"
    )
  if not golden_qa or not question_by_sid:
    return {}, {}

  sids = [sid for sid, q in question_by_sid.items() if q]
  conv_qs = [question_by_sid[sid] for sid in sids]
  golden_qs = [g["question"] for g in golden_qa]
  if not conv_qs or not golden_qs:
    return {}, {}

  logger.info(
      "Golden matching: embedding %d golden + %d session questions...",
      len(golden_qs),
      len(conv_qs),
  )
  golden_vecs = embed_texts(golden_qs)
  conv_vecs = embed_texts(conv_qs)

  per_session_context = {}
  golden_metadata = {}
  matched = 0
  for sid, cvec in zip(sids, conv_vecs):
    best_idx, best_score = -1, -1.0
    for gi, gvec in enumerate(golden_vecs):
      # Both vectors are L2-normalised, so the dot product is cosine.
      score = sum(a * b for a, b in zip(cvec, gvec))
      if score > best_score:
        best_score, best_idx = score, gi

    if best_score >= threshold:
      g = golden_qa[best_idx]
      is_oos = (
          g.get("expected_behavior") == "decline"
          or g.get("topic") == "out_of_scope"
      )
      ctx = [
          "EXPECTED ANSWER FOR THIS QUESTION "
          "(use to judge factual correctness):",
          f"Q: {g['question']}",
      ]
      if g.get("expected_answer"):
        ctx.append(f"A: {g['expected_answer']}")
      if is_oos:
        ctx.append(
            "NOTE: This question is OUT OF SCOPE — the agent should decline."
            " A polite decline is the correct ('declined') outcome."
        )
      per_session_context[sid] = "\n".join(ctx)
      golden_metadata[sid] = {
          "matched": True,
          "golden_question": g["question"],
          "expected_answer": g.get("expected_answer", ""),
          "topic": g.get("topic", "unknown"),
          "out_of_scope": is_oos,
          "similarity": round(best_score, 4),
      }
      matched += 1
    else:
      golden_metadata[sid] = {
          "matched": False,
          "similarity": round(best_score, 4),
      }

  logger.info(
      "Golden matching: %d/%d sessions matched (threshold=%.2f)",
      matched,
      len(sids),
      threshold,
  )
  return per_session_context, golden_metadata
