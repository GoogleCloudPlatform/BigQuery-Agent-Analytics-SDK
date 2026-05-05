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

"""AST safety validator for compiled structured extractors.

Compiled extractors execute as plain Python (per PR 4a). Anything
the LLM-driven template fill (PR 4b.2) emits has to pass this check
before the smoke-test runner ever imports it. The validator is the
trust boundary: AST failures short-circuit compile *before* any
``exec_module`` call.

Allowed:
  - ``from __future__ import annotations``
  - ``from bigquery_agent_analytics.extracted_models import ...``
  - ``from bigquery_agent_analytics.structured_extraction import ...``
  - Module-scope: only the docstring, allowlisted imports, and
    function definitions
  - Pure control flow: ``if`` / ``for`` / ``while`` / comprehensions
  - Literals, f-strings, allowlisted builtins, and method calls on
    parameter objects (e.g., ``event.get('content')``)

Rejected:
  - Imports outside the allowlist
  - Dynamic-execution names (``eval``, ``exec``, ``compile``,
    ``__import__``)
  - Introspection (``getattr``, ``setattr``, ``delattr``,
    ``globals``, ``locals``, ``vars``)
  - I/O builtins (``open``, ``input``)
  - Any attribute starting with ``_`` (blocks dunder access like
    ``obj.__class__`` and private-attribute access)
  - Top-level side-effecting statements
  - ``async`` / ``await`` / generators / ``yield`` / class
    definitions / ``global`` / ``nonlocal``

The allowlist is intentionally narrow for PR 4b.1; extending it as
real templates need it (e.g., adding stdlib helpers) is a deliberate
future PR, not a default expansion.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Optional

_ALLOWED_IMPORTS_FROM = frozenset(
    {
        "__future__",
        "bigquery_agent_analytics.extracted_models",
        "bigquery_agent_analytics.structured_extraction",
    }
)

_FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "setattr",
        "getattr",
        "delattr",
        "open",
        "input",
    }
)


@dataclasses.dataclass(frozen=True)
class AstFailure:
  """One AST-validation failure.

  ``code`` is a stable string identifier callers can switch on
  (mirrors the failure-code convention used by the #76 graph
  validator). ``line`` / ``col`` point at the offending source
  location when the AST node carries them.
  """

  code: str
  detail: str
  line: Optional[int] = None
  col: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class AstReport:
  """Result of :func:`validate_source`."""

  failures: tuple[AstFailure, ...] = ()

  @property
  def ok(self) -> bool:
    return not self.failures


def validate_source(source: str) -> AstReport:
  """Statically validate that *source* is a safe compiled extractor.

  Parses *source*, walks every node, collects every rule violation
  rather than failing fast — callers (templates, LLM fixers) get
  the full list in one pass.
  """
  failures: list[AstFailure] = []
  try:
    tree = ast.parse(source)
  except SyntaxError as e:
    failures.append(
        AstFailure(
            code="syntax_error",
            detail=f"Python syntax error: {e.msg}",
            line=e.lineno,
            col=e.offset,
        )
    )
    return AstReport(failures=tuple(failures))

  _check_module_scope(tree, failures)
  for node in ast.walk(tree):
    _check_node(node, failures)

  return AstReport(failures=tuple(failures))


def _check_module_scope(tree: ast.Module, failures: list[AstFailure]) -> None:
  """Reject anything at module scope other than docstring, imports,
  and function defs. Top-level assignments and expressions are
  side effects that compiled extractors should not have."""
  for stmt in tree.body:
    if _is_module_docstring(stmt):
      continue
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
      _check_import(stmt, failures)
      continue
    if isinstance(stmt, ast.FunctionDef):
      continue
    failures.append(
        AstFailure(
            code="top_level_side_effect",
            detail=(
                "compiled extractors may contain only a module docstring, "
                "allowlisted imports, and function definitions at module "
                f"scope; found {type(stmt).__name__}"
            ),
            line=getattr(stmt, "lineno", None),
        )
    )


def _is_module_docstring(stmt: ast.stmt) -> bool:
  return (
      isinstance(stmt, ast.Expr)
      and isinstance(stmt.value, ast.Constant)
      and isinstance(stmt.value.value, str)
  )


def _check_import(stmt: ast.stmt, failures: list[AstFailure]) -> None:
  """Reject imports outside the ``_ALLOWED_IMPORTS_FROM`` set.

  Plain ``import foo`` is rejected even for allowlisted modules
  because it binds the top-level name (``foo``) into the extractor's
  namespace, complicating the name-allowlist analysis. Use
  ``from foo import x`` instead.
  """
  if isinstance(stmt, ast.Import):
    for alias in stmt.names:
      failures.append(
          AstFailure(
              code="disallowed_import",
              detail=(
                  f"plain 'import {alias.name}' is not allowed in "
                  f"compiled extractors; use 'from <allowlisted-module> "
                  f"import ...' instead"
              ),
              line=stmt.lineno,
          )
      )
    return

  assert isinstance(stmt, ast.ImportFrom)
  module = stmt.module or ""
  if module not in _ALLOWED_IMPORTS_FROM:
    failures.append(
        AstFailure(
            code="disallowed_import",
            detail=(
                f"import from {module!r} is not in the compiled-extractor "
                f"allowlist; allowed: {sorted(_ALLOWED_IMPORTS_FROM)}"
            ),
            line=stmt.lineno,
        )
    )


def _check_node(node: ast.AST, failures: list[AstFailure]) -> None:
  """Per-node rules that apply at any nesting depth."""
  if isinstance(node, ast.Name):
    if isinstance(node.ctx, ast.Load) and node.id in _FORBIDDEN_NAMES:
      failures.append(
          AstFailure(
              code="disallowed_name",
              detail=(
                  f"reference to forbidden name {node.id!r}; this name "
                  f"can subvert compiled-extractor safety"
              ),
              line=node.lineno,
          )
      )
    return
  if isinstance(node, ast.Attribute):
    if node.attr.startswith("_"):
      failures.append(
          AstFailure(
              code="disallowed_attribute",
              detail=(
                  f"dunder/private attribute access {node.attr!r} is not "
                  f"allowed in compiled extractors"
              ),
              line=node.lineno,
          )
      )
    return
  if isinstance(
      node,
      (
          ast.AsyncFunctionDef,
          ast.AsyncWith,
          ast.AsyncFor,
          ast.Await,
      ),
  ):
    failures.append(
        AstFailure(
            code="disallowed_async",
            detail="async constructs are not allowed in compiled extractors",
            line=getattr(node, "lineno", None),
        )
    )
    return
  if isinstance(node, (ast.Yield, ast.YieldFrom)):
    failures.append(
        AstFailure(
            code="disallowed_generator",
            detail=(
                "yield / yield from is not allowed in compiled extractors "
                "(extractors return a StructuredExtractionResult, not a "
                "generator)"
            ),
            line=getattr(node, "lineno", None),
        )
    )
    return
  if isinstance(node, ast.ClassDef):
    failures.append(
        AstFailure(
            code="disallowed_class",
            detail=(
                f"class definitions are not allowed in compiled extractors "
                f"(extractors are pure functions); class {node.name!r}"
            ),
            line=node.lineno,
        )
    )
    return
  if isinstance(node, (ast.Global, ast.Nonlocal)):
    failures.append(
        AstFailure(
            code="disallowed_scope",
            detail=(
                "global / nonlocal declarations are not allowed in "
                "compiled extractors"
            ),
            line=getattr(node, "lineno", None),
        )
    )
