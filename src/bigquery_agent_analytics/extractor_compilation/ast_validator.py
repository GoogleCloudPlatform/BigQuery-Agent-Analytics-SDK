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
  - ``while`` / ``raise`` / ``try`` / ``with`` / ``match``
    (halting / flow / pattern-binding constructs that can hang
    the smoke runner, escape its exception handler via
    ``SystemExit``, or smuggle name bindings past the shadowing
    check)
  - Lambda expressions (``disallowed_lambda``) — anonymous
    callables defeat the static call-target allowlist
  - Calls whose target isn't a Name or Attribute
    (``Call(func=Lambda/Call/IfExp/...)``) — caught by
    ``disallowed_call``
  - Method calls outside the method-name allowlist
    (``disallowed_method``)
  - Local rebindings of a name in the call-target allowlist —
    via assignment, AugAssign, AnnAssign, walrus, for-target,
    comprehension target, function arg, or nested function def
    (``disallowed_shadowing``)

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
        # Open-ended iteration: ``iter(callable, sentinel)`` is the
        # documented two-argument form for unbounded iteration.
        # Block by name; for-loop iter is also Call-to-Name-blocked
        # below (catches ``range``, user-defined helpers, etc.).
        "iter",
    }
)

# Allowlist of names that may appear as the *target* of a call,
# i.e. ``func`` in ``Call(func=Name(...))``. The complement
# (``Call(func=Attribute(...))``) is method-style and bounded by
# the receiver's runtime size — those still pass. Anything outside
# this set fails ``disallowed_call``, blocking shapes like
# ``list(range(10**100))`` or ``sum(...)`` that would otherwise
# allocate heavily even though the names themselves aren't in
# ``_FORBIDDEN_NAMES``.
_ALLOWED_CALL_TARGETS = frozenset(
    {
        # Allowlisted ontology / extraction model constructors.
        # Must match ``_ALLOWED_IMPORTS_FROM`` above.
        "ExtractedNode",
        "ExtractedEdge",
        "ExtractedProperty",
        "StructuredExtractionResult",
        # Primitive type constructors. ``int(x)``, ``str(x)``, etc.
        # are bounded by their input.
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        # Bounded container constructors. Empty calls (``set()``,
        # ``dict()``) and conversions of bounded inputs are safe;
        # iteration shape is bounded separately by the for-iter
        # rule, which still rejects ``for _ in list(...):`` because
        # the for-iter rule is stricter than the call rule.
        "set",
        "frozenset",
        "dict",
        "list",
        "tuple",
        # Safe builtins.
        "isinstance",
        "len",
    }
)

# Allowlist of *method* names — i.e. ``attr`` in
# ``Call(func=Attribute(value=..., attr=<method>))``. Without this,
# any method-style call passes (``event.repeat_forever()``,
# ``event.clear()``, etc.). The list is intentionally small and
# matches what the BKA fixture and likely 4b.2-emitted templates
# need: read-only dict/list access plus list-building. Adding a
# new entry should be a deliberate decision, not a default
# expansion.
_ALLOWED_METHOD_NAMES = frozenset(
    {
        # Read-only dict access
        "get",
        "items",
        "keys",
        "values",
        # List building used by the BKA fixture
        "append",
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
    # No import-alias shadowing check: the per-module symbol
    # allowlist above already constrains *what* can be imported.
    # Importing the allowlisted constructors (``ExtractedNode``,
    # etc.) by their canonical names is the intended pattern, and
    # an attacker can't import an unsafe symbol *as* an allowlist
    # name because the symbol allowlist would reject the original.


def _check_node(node: ast.AST, failures: list[AstFailure]) -> None:
  """Per-node rules that apply at any nesting depth."""
  if isinstance(node, ast.Call):
    _check_call(node, failures)
    # Don't return — the inner ``func`` Name will still be visited
    # by ``ast.walk`` and the Name rule applies there too. That's
    # intentional layered defense (forbidden Name + disallowed Call
    # both fire on e.g. ``eval(...)``).
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
    if isinstance(node.ctx, ast.Store) and node.id in _ALLOWED_CALL_TARGETS:
      failures.append(_shadowing_failure(node.id, node.lineno))
    return
  if isinstance(node, ast.arg):
    if node.arg in _ALLOWED_CALL_TARGETS:
      failures.append(
          _shadowing_failure(node.arg, getattr(node, "lineno", None))
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
  if isinstance(node, ast.Lambda):
    failures.append(
        AstFailure(
            code="disallowed_lambda",
            detail=(
                "lambda expressions are not allowed in compiled "
                "extractors; the call-target allowlist relies on "
                "static names, and lambdas defeat that"
            ),
            line=getattr(node, "lineno", None),
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
  # ``ast.Match`` (Python 3.10+) is rejected outright. The accepted
  # control-flow set is if / bounded for / comprehensions, and
  # match-case carries pattern *captures* (``case {"x": len}``)
  # that bind names without going through ``Name(ctx=Store)`` —
  # so the shadowing check would miss them and the call-target
  # allowlist could be bypassed.
  if hasattr(ast, "Match") and isinstance(node, ast.Match):
    failures.append(
        AstFailure(
            code="disallowed_match",
            detail=(
                "'match' statements are not allowed in compiled "
                "extractors; pattern captures bind names outside the "
                "shadowing check's reach. Use 'if' / 'elif' branches "
                "instead."
            ),
            line=node.lineno,
        )
    )
    return
  if isinstance(node, ast.For):
    _check_for_iter(node.iter, failures, lineno=node.lineno)
    return
  if isinstance(
      node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
  ):
    for gen in node.generators:
      _check_for_iter(gen.iter, failures, lineno=getattr(node, "lineno", None))
    return


def _shadowing_failure(name: str, line: Optional[int]) -> AstFailure:
  """Build an ``AstFailure`` for a local binding that shadows a
  name in ``_ALLOWED_CALL_TARGETS``. Without this rule the
  call-target allowlist is bypassable: ``len = event.get('cb');
  len()`` would slip past static analysis because ``len`` is in
  the allowlist *as a name* even though the local rebinding has
  made it point at something else."""
  return AstFailure(
      code="disallowed_shadowing",
      detail=(
          f"name {name!r} is in the call-target allowlist; binding "
          f"it locally would let unsafe callables slip past the "
          f"static check. Choose a different identifier."
      ),
      line=line,
  )


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
  if node.name in _ALLOWED_CALL_TARGETS:
    failures.append(_shadowing_failure(node.name, node.lineno))

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


def _check_call(node: ast.Call, failures: list[AstFailure]) -> None:
  """Reject Call targets outside the allowlist for both the
  ``Call(func=Name(...))`` and ``Call(func=Attribute(...))`` forms.

  Without the method-name check, ``event.repeat_forever()`` or
  ``event.clear()`` would pass purely because the receiver is a
  parameter. The dunder-attribute rule already blocks
  ``__class__``-style smuggling, but it doesn't help with
  arbitrary plain-name methods.
  """
  if isinstance(node.func, ast.Name):
    name = node.func.id
    if name in _ALLOWED_CALL_TARGETS:
      return
    failures.append(
        AstFailure(
            code="disallowed_call",
            detail=(
                f"call target {name!r} is not in the compiled-extractor "
                f"allowlist; allowed: {sorted(_ALLOWED_CALL_TARGETS)}. "
                f"Use method-style calls on bounded receivers "
                f"(e.g., dict.items(), event.get(...)) or pre-imported "
                f"constructors instead."
            ),
            line=node.lineno,
        )
    )
    return

  if isinstance(node.func, ast.Attribute):
    method_name = node.func.attr
    if method_name in _ALLOWED_METHOD_NAMES:
      return
    failures.append(
        AstFailure(
            code="disallowed_method",
            detail=(
                f"method {method_name!r} is not in the compiled-extractor "
                f"method allowlist; allowed: "
                f"{sorted(_ALLOWED_METHOD_NAMES)}. Receivers are bounded "
                f"by their runtime size, but unknown method calls can "
                f"still mutate state, allocate, or never return."
            ),
            line=node.lineno,
        )
    )
    return

  # Catch-all: ``func`` is neither a ``Name`` (allowlist of plain
  # callable targets) nor an ``Attribute`` (allowlist of method
  # names). The remaining shapes are
  # ``Call(func=Lambda(...))`` (anonymous function), nested
  # ``Call`` (chained calls), ``IfExp`` (conditional callable),
  # ``Subscript``, ``BoolOp``, etc. None of these can be
  # allowlisted by static name; reject the call itself.
  failures.append(
      AstFailure(
          code="disallowed_call",
          detail=(
              f"call target is a {type(node.func).__name__} expression, "
              f"not a name or method-style attribute access; the "
              f"compiled-extractor allowlist only covers plain "
              f"callable names and method calls (lambdas, chained "
              f"calls, conditional callables and similar are rejected)"
          ),
          line=node.lineno,
      )
  )


def _check_for_iter(
    iter_node: ast.AST, failures: list[AstFailure], *, lineno: Optional[int]
) -> None:
  """Reject open-ended iterables at ``for`` / comprehension positions.

  ``while`` is already rejected outright. The remaining hazard is a
  ``for`` loop or comprehension whose iterable is unbounded —
  ``for _ in range(10**100):`` is the canonical example, and any
  call to a name (``range``, ``iter``, a user-defined helper) is
  the form that produces it. We allow:

  - Literal containers: ``for k in ('a', 'b'):``
  - Constants: ``for c in 'abc':``
  - Names: ``for x in event_list:`` (size bounded by event payload)
  - Attribute access: ``for x in event.items_list``
  - Method calls: ``for k, v in content.items():`` — ``Call`` whose
    ``func`` is an ``Attribute``. The receiver is parameter-bound
    so the size is realistically bounded.
  - Subscripts: ``for x in event['list']``

  Anything else — particularly ``Call(func=Name(...))`` — fails.
  """
  if isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Name):
    name = iter_node.func.id
    failures.append(
        AstFailure(
            code="disallowed_for_iter",
            detail=(
                f"for/comprehension iterates over a call to {name!r}; "
                f"name-call iterables (range, iter, user-defined "
                f"helpers) can be unbounded and would hang the smoke "
                f"runner. Iterate over a literal tuple/list, an event "
                f"payload, or a method call (e.g., dict.items())"
            ),
            line=getattr(iter_node, "lineno", lineno),
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
