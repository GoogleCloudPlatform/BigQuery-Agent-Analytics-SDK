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

"""Tests for golden Q&A matching (embedding retry + cosine matching)."""

import math

import pytest

from bigquery_agent_analytics import golden_matching
from bigquery_agent_analytics.golden_matching import match_golden_qa


class TestEmbedTextsRetry:
  """Golden-matching embeddings retry transient 429/503 per batch (#360 U6)."""

  class _Transient(Exception):

    def __init__(self, code):
      super().__init__(f"transient {code}")
      self.code = code

  def _fake_genai(self, monkeypatch, failures):
    import google.genai

    calls = {"n": 0}

    class _Embedding:
      values = [3.0, 4.0]

    class _Models:

      def embed_content(self, **_kwargs):
        calls["n"] += 1
        if failures:
          raise failures.pop(0)
        resp = type("R", (), {})()
        resp.embeddings = [_Embedding()]
        return resp

    class _Client:

      def __init__(self, **_kwargs):
        self.models = _Models()

    monkeypatch.setattr(google.genai, "Client", _Client)
    monkeypatch.setattr(golden_matching.time, "sleep", lambda _s: None)
    return calls

  def test_transient_429_retried_then_succeeds(self, monkeypatch):
    calls = self._fake_genai(
        monkeypatch, [self._Transient(429), self._Transient(503)]
    )
    vectors = golden_matching.embed_texts(["q"])
    assert calls["n"] == 3
    assert vectors == [[0.6, 0.8]]  # L2-normalised [3, 4]

  def test_non_transient_error_raises_immediately(self, monkeypatch):
    calls = self._fake_genai(monkeypatch, [self._Transient(400)])
    with pytest.raises(self._Transient):
      golden_matching.embed_texts(["q"])
    assert calls["n"] == 1

  def test_exhausted_attempts_reraise(self, monkeypatch):
    failures = [self._Transient(429) for _ in range(5)]
    calls = self._fake_genai(monkeypatch, failures)
    with pytest.raises(self._Transient):
      golden_matching.embed_texts(["q"], max_attempts=5)
    assert calls["n"] == 5


class TestEmbedTextsValidation:
  """Invalid public controls raise ValueError before any API call."""

  @pytest.mark.parametrize("batch_size", [0, -1])
  def test_invalid_batch_size_raises(self, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
      golden_matching.embed_texts(["q"], batch_size=batch_size)

  @pytest.mark.parametrize("max_attempts", [0, -3])
  def test_invalid_max_attempts_raises(self, max_attempts):
    with pytest.raises(ValueError, match="max_attempts"):
      golden_matching.embed_texts(["q"], max_attempts=max_attempts)

  def test_boundary_values_accepted(self, monkeypatch):
    # batch_size=1 and max_attempts=1 are the smallest valid controls.
    class _Embedding:
      values = [1.0, 0.0]

    class _Models:

      def embed_content(self, **_kwargs):
        resp = type("R", (), {})()
        resp.embeddings = [_Embedding()]
        return resp

    class _Client:

      def __init__(self, **_kwargs):
        self.models = _Models()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", _Client)
    vectors = golden_matching.embed_texts(["q"], batch_size=1, max_attempts=1)
    assert vectors == [[1.0, 0.0]]


class TestMatchGoldenQA:

  def _fake_embeddings(self, monkeypatch, mapping):
    def fake(texts, **_kw):
      return [mapping[t] for t in texts]

    monkeypatch.setattr(golden_matching, "embed_texts", fake)

  def test_matches_above_threshold_and_flags_out_of_scope(self, monkeypatch):
    self._fake_embeddings(
        monkeypatch,
        {
            "golden pto": [1.0, 0.0],
            "golden stocks": [0.0, 1.0],
            "how much pto do I get": [1.0, 0.0],
            "unrelated": [0.7071, 0.7071],
        },
    )
    golden = [
        {"question": "golden pto", "expected_answer": "20 days"},
        {
            "question": "golden stocks",
            "expected_behavior": "decline",
        },
    ]
    ctx, meta = match_golden_qa(
        {"s1": "how much pto do I get", "s2": "unrelated"},
        golden,
        threshold=0.9,
    )
    assert "20 days" in ctx["s1"]
    assert meta["s1"]["matched"] and meta["s1"]["similarity"] >= 0.9
    assert "s2" not in ctx and meta["s2"]["matched"] is False

  def test_decline_note_for_out_of_scope_golden(self, monkeypatch):
    self._fake_embeddings(
        monkeypatch,
        {"golden stocks": [0.0, 1.0], "which stocks": [0.0, 1.0]},
    )
    golden = [{"question": "golden stocks", "expected_behavior": "decline"}]
    ctx, meta = match_golden_qa({"k": "which stocks"}, golden)
    assert "OUT OF SCOPE" in ctx["k"]
    assert meta["k"]["out_of_scope"] is True

  def test_empty_inputs_return_empty(self):
    assert match_golden_qa({}, [{"question": "q"}]) == ({}, {})
    assert match_golden_qa({"s": "q"}, []) == ({}, {})

  def test_keys_are_opaque(self, monkeypatch):
    # Non-string keys (e.g. ResolvedTraceSelector) pass through untouched.
    self._fake_embeddings(
        monkeypatch, {"golden": [1.0, 0.0], "question": [1.0, 0.0]}
    )
    key = ("identity", "scope")
    ctx, meta = match_golden_qa(
        {key: "question"}, [{"question": "golden", "expected_answer": "A"}]
    )
    assert key in ctx and key in meta

  @pytest.mark.parametrize(
      "threshold",
      [-0.1, 1.1, math.inf, -math.inf, math.nan],
  )
  def test_invalid_threshold_raises(self, threshold):
    with pytest.raises(ValueError, match="threshold"):
      match_golden_qa({"s": "q"}, [{"question": "g"}], threshold=threshold)

  def test_invalid_threshold_raises_even_for_empty_inputs(self):
    # Validation precedes the empty-input early return: a bad threshold
    # never silently succeeds just because there was nothing to match.
    with pytest.raises(ValueError, match="threshold"):
      match_golden_qa({}, [], threshold=math.nan)

  def test_threshold_bounds_accepted(self, monkeypatch):
    self._fake_embeddings(
        monkeypatch, {"golden": [1.0, 0.0], "question": [1.0, 0.0]}
    )
    golden = [{"question": "golden", "expected_answer": "A"}]
    # threshold=0.0 and threshold=1.0 are both valid; an exact-similarity
    # pair matches at either bound (comparison is >=).
    for bound in (0.0, 1.0):
      ctx, meta = match_golden_qa({"s": "question"}, golden, threshold=bound)
      assert meta["s"]["matched"] is True and "s" in ctx
