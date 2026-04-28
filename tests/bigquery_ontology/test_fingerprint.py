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

"""Tests for the internal fingerprint module used by concept-index provenance.

The contract is pinned in `docs/implementation_plan_concept_index_runtime.md`
(W1/W2) and in the issue #58 body under "Fingerprint algorithm":

- Input: `BaseModel.model_dump(mode="json", by_alias=False, exclude_none=False)`.
- Canonical JSON: `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
- ``fingerprint_model``: SHA-256, returned as ``"sha256:"`` + 64 hex chars.
- ``compile_fingerprint``: full 64-hex SHA-256 over
  ``ontology_fingerprint || binding_fingerprint || compiler_version``.
  Canonical integrity key; used by strict verification.
- ``compile_id``: 12-hex display token derived as
  ``compile_fingerprint(...)[:12]``. Never used as the sole
  freshness check.

Invariant: ``compile_id == compile_fingerprint[:12]``.

These tests pin the serialization contract (so a future "optimizer"
cannot silently replace it with ``yaml.dump`` or ``str(model)`` or
``model.model_dump()`` without ``mode="json"``, all of which would
break cross-package agreement between the compiler-side writer and
the runtime-side verifier) and the Option 2 column contract from
issue #58 (so the short and long forms cannot drift apart).
"""

from __future__ import annotations

import textwrap

import pytest

from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import compile_id
from bigquery_ontology._fingerprint import fingerprint_model

_SIMPLE_ONTOLOGY_YAML = textwrap.dedent(
    """\
    ontology: finance
    entities:
      - name: Account
        keys:
          primary: [account_id]
        properties:
          - name: account_id
            type: string
          - name: opened_at
            type: timestamp
    """
)


_SIMPLE_BINDING_YAML = textwrap.dedent(
    """\
    binding: finance-bq-prod
    ontology: finance
    target:
      backend: bigquery
      project: my-proj
      dataset: finance
    entities:
      - name: Account
        source: raw.accounts
        properties:
          - {name: account_id, column: acct_id}
          - {name: opened_at, column: created_ts}
    """
)


# ------------------------------------------------------------------ #
# fingerprint_model                                                   #
# ------------------------------------------------------------------ #


class TestFingerprintModel:
  """Canonical-JSON SHA-256 over Pydantic models."""

  def test_returns_sha256_prefixed_hex(self):
    ont = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    fp = fingerprint_model(ont)
    assert fp.startswith("sha256:")
    # 64 hex chars after the prefix = 256 bits.
    digest = fp[len("sha256:") :]
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)

  def test_deterministic_across_calls(self):
    """Same model, same fingerprint, every time."""
    ont = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    fp1 = fingerprint_model(ont)
    fp2 = fingerprint_model(ont)
    assert fp1 == fp2

  def test_non_semantic_yaml_edits_produce_identical_fingerprint(self):
    """Whitespace, comments, and key-ordering changes must not shift the
    fingerprint. This is the entire justification for fingerprinting at
    the validated-model layer rather than over raw YAML text.
    """
    edited_yaml = textwrap.dedent(
        """\
        # A leading comment.
        ontology: finance


        entities:
            - name: Account
              keys:
                  primary: [account_id]
              properties:
                  - name: account_id
                    type: string
                  - name: opened_at
                    type: timestamp
        """
    )
    ont1 = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    ont2 = load_ontology_from_string(edited_yaml)
    assert fingerprint_model(ont1) == fingerprint_model(ont2)

  def test_semantic_rename_changes_fingerprint(self):
    """Changing an entity name is a semantic edit; fingerprint differs."""
    renamed = _SIMPLE_ONTOLOGY_YAML.replace("name: Account", "name: Ledger")
    ont1 = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    ont2 = load_ontology_from_string(renamed)
    assert fingerprint_model(ont1) != fingerprint_model(ont2)

  def test_semantic_type_change_changes_fingerprint(self):
    """Property type change must surface as a different fingerprint."""
    retyped = _SIMPLE_ONTOLOGY_YAML.replace("type: timestamp", "type: datetime")
    ont1 = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    ont2 = load_ontology_from_string(retyped)
    assert fingerprint_model(ont1) != fingerprint_model(ont2)

  def test_binding_fingerprint_reflects_target_dataset(self):
    """A binding repointed at a different dataset must fingerprint
    differently — the binding's identity includes its physical target.
    """
    ont = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    bnd1 = load_binding_from_string(_SIMPLE_BINDING_YAML, ontology=ont)
    bnd2 = load_binding_from_string(
        _SIMPLE_BINDING_YAML.replace("dataset: finance", "dataset: staging"),
        ontology=ont,
    )
    assert fingerprint_model(bnd1) != fingerprint_model(bnd2)

  def test_ontology_and_binding_fingerprints_differ(self):
    """Two different model types from the same source file namespace
    must fingerprint to different values. (A trivial check, but it
    catches implementations that fingerprint only on a shared subset
    of fields.)
    """
    ont = load_ontology_from_string(_SIMPLE_ONTOLOGY_YAML)
    bnd = load_binding_from_string(_SIMPLE_BINDING_YAML, ontology=ont)
    assert fingerprint_model(ont) != fingerprint_model(bnd)

  def test_list_order_is_semantic(self):
    """Key columns are declaration-ordered in the ontology model;
    reordering them is a semantic change. Fingerprint must reflect
    that (lists are preserved in declaration order, not sorted).
    """
    multi_key_yaml = textwrap.dedent(
        """\
        ontology: finance
        entities:
          - name: Account
            keys:
              primary: [tenant_id, account_id]
            properties:
              - name: tenant_id
                type: string
              - name: account_id
                type: string
        """
    )
    reordered = textwrap.dedent(
        """\
        ontology: finance
        entities:
          - name: Account
            keys:
              primary: [account_id, tenant_id]
            properties:
              - name: tenant_id
                type: string
              - name: account_id
                type: string
        """
    )
    ont1 = load_ontology_from_string(multi_key_yaml)
    ont2 = load_ontology_from_string(reordered)
    assert fingerprint_model(ont1) != fingerprint_model(ont2)


# ------------------------------------------------------------------ #
# compile_fingerprint — canonical integrity key                       #
# ------------------------------------------------------------------ #


class TestCompileFingerprint:
  """Full 64-hex SHA-256 over the three compile inputs.

  This is the canonical integrity key used by strict verification.
  Must respond to every input independently and must be 64 hex chars.
  """

  def test_length_is_sixty_four_hex_chars(self):
    fp = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)

  def test_deterministic_for_same_inputs(self):
    fp1 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    fp2 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert fp1 == fp2

  def test_changes_with_ontology_fingerprint(self):
    fp1 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    fp2 = compile_fingerprint("sha256:zz", "sha256:bb", "gm-1.0.0")
    assert fp1 != fp2

  def test_changes_with_binding_fingerprint(self):
    fp1 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    fp2 = compile_fingerprint("sha256:aa", "sha256:zz", "gm-1.0.0")
    assert fp1 != fp2

  def test_changes_with_compiler_version(self):
    """Compiler version changes must shift the fingerprint even when
    both input fingerprints are unchanged — otherwise a semver bump
    to the compiler with behavior changes would fail to invalidate
    old meta.
    """
    fp1 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    fp2 = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.1.0")
    assert fp1 != fp2

  def test_golden_vector(self):
    """Pinned byte-exact digest for a known input triple.

    This catches silent payload-contract changes that would still
    pass length/determinism/input-sensitivity tests — e.g. swapping
    the NUL separator to another byte, changing the concatenation
    order, or altering the UTF-8 encoding. A refactor that changes
    the payload shape will flip this value and fail the test even
    if all the behavioral properties still hold.

    The payload is:

        "sha256:aa" + "\\x00" + "sha256:bb" + "\\x00" + "gm-1.0.0"

    encoded as UTF-8, hashed with SHA-256.
    """
    full = compile_fingerprint("sha256:aa", "sha256:bb", "gm-1.0.0")
    short = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert (
        full
        == "dd301e2874a3bc55dd3ab6ea428ad1046f6b4834b4dccd7a87cf82a1991e0184"
    )
    assert short == "dd301e2874a3"


# ------------------------------------------------------------------ #
# compile_id — 12-hex display token, derived from compile_fingerprint #
# ------------------------------------------------------------------ #


class TestCompileId:
  """Short display/debug tag derived from ``compile_fingerprint``."""

  def test_length_is_twelve_hex_chars(self):
    cid = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert len(cid) == 12
    assert all(c in "0123456789abcdef" for c in cid)

  def test_deterministic_for_same_inputs(self):
    cid1 = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    cid2 = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert cid1 == cid2

  def test_changes_with_ontology_fingerprint(self):
    cid1 = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    cid2 = compile_id("sha256:zz", "sha256:bb", "gm-1.0.0")
    assert cid1 != cid2

  def test_changes_with_binding_fingerprint(self):
    cid1 = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    cid2 = compile_id("sha256:aa", "sha256:zz", "gm-1.0.0")
    assert cid1 != cid2

  def test_changes_with_compiler_version(self):
    """Compiler version changes must shift the compile_id even when
    both fingerprints are unchanged — otherwise a semver bump to the
    compiler with behavior changes would fail to invalidate old meta.
    """
    cid1 = compile_id("sha256:aa", "sha256:bb", "gm-1.0.0")
    cid2 = compile_id("sha256:aa", "sha256:bb", "gm-1.1.0")
    assert cid1 != cid2


# ------------------------------------------------------------------ #
# Structural invariant: compile_id == compile_fingerprint[:12]        #
# ------------------------------------------------------------------ #


class TestCompileIdFingerprintInvariant:
  """The short display token must be a structural truncation of the
  full integrity key — never its own hash. This prevents the two
  provenance columns in the concept index from drifting out of sync.

  Pinned in the ``_fingerprint.py`` module docstring and in issue #58
  (Option 2).
  """

  def test_compile_id_is_prefix_of_compile_fingerprint(self):
    args = ("sha256:aa", "sha256:bb", "gm-1.0.0")
    assert compile_id(*args) == compile_fingerprint(*args)[:12]

  def test_invariant_holds_across_varied_inputs(self):
    """Invariant must hold for any input triple, not just one case."""
    cases = [
        ("sha256:aa", "sha256:bb", "gm-1.0.0"),
        ("sha256:ff0000", "sha256:0000ff", "gm-2.3.4"),
        ("", "", ""),  # degenerate but deterministic
        ("x" * 100, "y" * 100, "z" * 100),  # long inputs
    ]
    for args in cases:
      assert (
          compile_id(*args) == compile_fingerprint(*args)[:12]
      ), f"invariant broken for {args}"

  def test_compile_id_never_computes_its_own_hash(self):
    """Regression guard: if someone re-implements ``compile_id`` to
    hash a different payload (e.g. a subset of inputs, or a different
    separator), the invariant breaks and this test catches it.
    """
    # Any change to the payload that preserves the relative ordering
    # but alters the bytes would produce a different 12-hex prefix.
    # Assert the derivation is pure truncation of compile_fingerprint.
    args = ("sha256:a", "sha256:b", "v")
    full = compile_fingerprint(*args)
    short = compile_id(*args)
    assert short == full[:12]
    assert full.startswith(short)


# ------------------------------------------------------------------ #
# Public surface                                                      #
# ------------------------------------------------------------------ #


class TestPrivateSurface:
  """``_fingerprint`` is an internal module — not part of the package
  API. Catch accidental re-export from ``bigquery_ontology/__init__.py``.
  """

  def test_not_exported_from_package_root(self):
    import bigquery_ontology

    assert not hasattr(bigquery_ontology, "fingerprint_model")
    assert not hasattr(bigquery_ontology, "compile_id")
    assert not hasattr(bigquery_ontology, "compile_fingerprint")

  def test_importable_via_absolute_path(self):
    """Both packages import via ``from bigquery_ontology._fingerprint
    import ...``. Make sure that path continues to work.
    """
    from bigquery_ontology._fingerprint import compile_fingerprint as _cfp
    from bigquery_ontology._fingerprint import compile_id as _cid
    from bigquery_ontology._fingerprint import fingerprint_model as _fp

    assert callable(_cfp)
    assert callable(_cid)
    assert callable(_fp)
