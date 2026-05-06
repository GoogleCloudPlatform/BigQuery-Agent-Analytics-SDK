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
  - ``from bigquery_agent_analytics.extracted_models import
    ExtractedNode, ExtractedEdge, ExtractedProperty``
  - ``from bigquery_agent_analytics.structured_extraction import
    StructuredExtractionResult, StructuredExtractor``
  - Module scope: only the docstring, allowlisted imports, and
    function definitions
  - Pure control flow: ``if`` / ``for`` / comprehensions
  - Literals, f-strings, allowlisted builtins, and method calls on
    parameter objects (e.g., ``event.get('content')``)

Rejected:
  - Imports outside the per-module symbol allowlist
  - ``import x`` (always; bind via ``from x import y`` instead)
  - Imported aliases starting with ``_`` (no hidden dunder smuggling)
  - Dynamic-execution names (``eval``, ``exec``, ``compile``,
    ``__import__``, ``__build_class__``, ``breakpoint``)
  - Introspection (``getattr``, ``setattr``, ``delattr``,
    ``globals``, ``locals``, ``vars``)
  - I/O / process-control builtins (``open``, ``input``,
    ``exit``, ``quit``)
  - Any attribute starting with ``_`` (blocks dunder access like
    ``obj.__class__`` and private-attribute access)
  - Top-level side-effecting statements
  - Decorators (run at definition time)
  - Non-constant default arguments (run at definition time)
  - Async / generators / class definitions / global / nonlocal
  - ``while`` / ``raise`` / ``try`` / ``with`` (halting / flow
    constructs that can hang the smoke runner or escape its
    exception handler via ``SystemExit``)

The allowlist is intentionally narrow for PR 4b.1; extending it as
real templates require it (e.g., adding stdlib helpers) is a
deliberate future PR, not a default expansion.
"""

from __future__ import annotations

import ast
import dataclasses
from typing import Optional

# Per-module symbol allowlist. Keyed by module name; each value is
# the set of *names* importable from that module via
# ``from <module> import <name>``. Anything outside this map fails
# ``disallowed_import``. Adding a new entry is a deliberate decision
# — don't broaden without a concrete template need.
_ALLOWED_IMPORTS_FROM: dict[str, frozenset[str]] = {
    "__future__": frozenset({"annotations"}),
    "bigquery_agent_analytics.extracted_models": frozenset(
        {
            "ExtractedNode",
            "ExtractedEdge",
            "ExtractedProperty",
        }
    ),
    "bigquery_agent_analytics.structured_extraction": frozenset(
        {
            "StructuredExtractionResult",
            "StructuredExtractor",
        }
    ),
}

_FORBIDDEN_NAMES = frozenset(
    {
        # Dynamic execution
        "eval",
        "exec",
        "compile",
        "__import__",
        "__build_class__",
        # Introspection
        "globals",
        "locals",
        "vars",
        "setattr",
        "getattr",
        "delattr",
        # I/O
        "open",
        "input",
        # Process control / debugger
        "exit",
        "quit",
        "breakpoint",
    }
)

# ``ast.TryStar`` exists on Python 3.11+. Build the rejection tuple
# defensively so the validator works on older interpreters too.
_TRY_TYPES: tuple[type, ...] = (ast.Try,)
if hasattr(ast, "TryStar"):
  _TRY_TYPES = _TRY_TYPES + (ast.TryStar,)


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
  """Reject anything at module scope other than the docstring,
  allowlisted imports, and function defs. Top-level assignments
  and expressions are side effects compiled extractors should not
  have."""
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
  """Reject imports outside the per-module symbol allowlist.

  Plain ``import foo`` is rejected even for allowlisted modules:
  the bound name (``foo``) puts the whole module surface in the
  extractor's namespace, defeating the symbol allowlist. Use
  ``from foo import x`` instead. Aliases starting with ``_`` are
  rejected too — no hidden dunder smuggling.
  """
  if isinstance(stmt, ast.Import):
    for alias in stmt.names:
      failures.append(
          AstFailure(
              code="disallowed_import",
              detail=(
                  f"plain 'import {alias.name}' is not allowed in "
                  f"compiled extractors; use 'from <allowlisted-module> "
                  f"import <allowlisted-symbol>' instead"
              ),
              line=stmt.lineno,
          )
      )
    return

  assert isinstance(stmt, ast.ImportFrom)
  module = stmt.module or ""
  allowed_symbols = _ALLOWED_IMPORTS_FROM.get(module)
  if allowed_symbols is None:
    failures.append(
        AstFailure(
            code="disallowed_import",
            detail=(
                f"import from {module!r} is not in the compiled-extractor "
                f"allowlist; allowed modules: "
                f"{sorted(_ALLOWED_IMPORTS_FROM)}"
            ),
            line=stmt.lineno,
        )
    )
    return

  for alias in stmt.names:
    if alias.name == "*":
      failures.append(
          AstFailure(
              code="disallowed_import",
              detail=(
                  f"wildcard 'from {module} import *' is not allowed in "
                  f"compiled extractors; import each symbol explicitly"
              ),
              line=stmt.lineno,
          )
      )
      continue
    if alias.name not in allowed_symbols:
      failures.append(
          AstFailure(
              code="disallowed_import",
              detail=(
                  f"symbol {alias.name!r} is not allowed from module "
                  f"{module!r}; allowed: {sorted(allowed_symbols)}"
              ),
              line=stmt.lineno,
          )
      )
      continue
    bound_name = alias.asname or alias.name
    if bound_name.startswith("_"):
      failures.append(
          AstFailure(
              code="disallowed_import",
              detail=(
                  f"imported alias {bound_name!r} starts with '_'; private "
                  f"and dunder aliases are not allowed (this also blocks "
                  f"smuggling __builtins__-style names through valid "
                  f"modules)"
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
  if isinstance(node, ast.FunctionDef):
    _check_function_def(node, failures)
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
    return
  if isinstance(node, ast.While):
    failures.append(
        AstFailure(
            code="disallowed_while",
            detail=(
                "'while' loops are not allowed in compiled extractors "
                "(can hang the smoke-test runner; use bounded 'for' loops)"
            ),
            line=node.lineno,
        )
    )
    return
  if isinstance(node, ast.Raise):
    failures.append(
        AstFailure(
            code="disallowed_raise",
            detail=(
                "explicit 'raise' is not allowed in compiled extractors; "
                "extractors should return an empty StructuredExtractionResult "
                "for events they cannot handle, not raise (and certainly "
                "not raise SystemExit, which would escape the smoke "
                "runner's exception handler)"
            ),
            line=node.lineno,
        )
    )
    return
  if isinstance(node, _TRY_TYPES):
    failures.append(
        AstFailure(
            code="disallowed_try",
            detail=(
                "'try' / 'try*' is not allowed in compiled extractors; "
                "the smoke-test runner is the only layer that catches "
                "exceptions"
            ),
            line=getattr(node, "lineno", None),
        )
    )
    return
  if isinstance(node, ast.With):
    failures.append(
        AstFailure(
            code="disallowed_with",
            detail=(
                "'with' is not allowed in compiled extractors; "
                "context-manager protocols invoke __enter__/__exit__ "
                "which are dunder methods"
            ),
            line=node.lineno,
        )
    )
    return


def _check_function_def(
    node: ast.FunctionDef, failures: list[AstFailure]
) -> None:
  """Reject decorators and non-constant default arguments.

  Decorators run at definition (import) time and can do arbitrary
  things. Default arguments are evaluated at definition time too —
  ``def f(x=open('/etc/passwd').read())`` would happen at module
  import even though ``open`` is forbidden inside the function
  body. Constraining defaults to constant primitives blocks that
  whole class of smuggling.
  """
  if node.decorator_list:
    for dec in node.decorator_list:
      failures.append(
          AstFailure(
              code="disallowed_decorator",
              detail=(
                  f"function {node.name!r} has a decorator; decorators "
                  f"run at definition time and are not allowed in "
                  f"compiled extractors"
              ),
              line=getattr(dec, "lineno", node.lineno),
          )
      )

  defaults = list(node.args.defaults) + [
      d for d in node.args.kw_defaults if d is not None
  ]
  for d in defaults:
    if not _is_constant_primitive(d):
      failures.append(
          AstFailure(
              code="disallowed_default",
              detail=(
                  f"function {node.name!r} has a non-constant default "
                  f"argument; defaults are evaluated at module-import "
                  f"time and must be primitive constants (str, int, "
                  f"float, bool, None)"
              ),
              line=getattr(d, "lineno", node.lineno),
          )
      )


def _is_constant_primitive(node: ast.AST) -> bool:
  """Return True if *node* is a constant primitive (str, int, float,
  bool, None, bytes, or unary-minus of a numeric constant).

  Defaults restricted to this set cannot invoke any function, so
  they cannot have import-time side effects.
  """
  if isinstance(node, ast.Constant):
    return isinstance(node.value, (str, int, float, bool, type(None), bytes))
  if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
    return isinstance(node.operand, ast.Constant) and isinstance(
        node.operand.value, (int, float)
    )
  return False
