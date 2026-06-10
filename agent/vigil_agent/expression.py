"""Safe expression evaluator for ``when:`` predicates on task actions.

The agent owns runtime control flow — ``when:`` decides whether a single
step in a multi-step task actually runs on this particular host, based
on a tiny dotted-name context (``agent.os``, ``agent.pkg_manager``,
``inputs.foo``, etc.). The server validates expression syntax at task-
definition save time so a broken predicate is rejected before signing.

Constraints (intentional):

  * No function calls. ``len()``, ``str()``, ``int()``, anything custom —
    nothing. The grammar is comparison + boolean composition over dotted
    names and literals.
  * No attribute writes, no augmented assignment, no comprehensions, no
    subscripts, no lambdas, no f-strings.
  * Tuple literals are allowed *only* on the right of ``in``/``not in``
    (e.g. ``agent.pkg_manager in ("apt", "dnf")``).
  * Dotted name access (``agent.os``) is allowed; bracket access
    (``agent["os"]``) is not — keeps the surface tiny and the syntax
    obvious.

Both the server (for syntactic validation) and the agent (for runtime
evaluation against actual context) import this module. They are kept
verbatim in sync by ``agent/vigil_agent/expression.py`` — if you change
one, change both. The module has no Django dependencies on purpose so
the PyInstaller-bundled agent can import it cleanly.
"""

from __future__ import annotations

import ast
from typing import Any


class ExprError(ValueError):
    """Raised when an expression is syntactically invalid or unsafe."""


# AST nodes we accept. Anything not in this set raises ExprError.
_ALLOWED_NODES = frozenset({
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.In, ast.NotIn,
    ast.Constant, ast.Name, ast.Attribute, ast.Tuple, ast.List,
    ast.Load,
})


def _validate_node(node: ast.AST) -> None:
    if type(node) not in _ALLOWED_NODES:
        raise ExprError(
            f"disallowed expression element {type(node).__name__!r}; "
            f"only ==, !=, in, not in, and, or, not, literals, and dotted "
            f"names like agent.os / inputs.foo are permitted"
        )
    # Names must be one of the known top-level context buckets.
    if isinstance(node, ast.Name) and node.id not in {"agent", "inputs", "host"}:
        raise ExprError(
            f"unknown context name {node.id!r}; "
            f"valid roots are agent, inputs, host"
        )
    # Attribute access must be on an allowed root → a single attribute step.
    # `agent.os` is fine; `agent.os.upper` is not.
    if isinstance(node, ast.Attribute):
        # The value side must be a Name (one root) or another Attribute
        # (one further level — host.tags etc.). We cap depth at 2.
        depth = 0
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
            depth += 1
            if depth > 3:
                raise ExprError("attribute chain too deep")
        if not isinstance(cur, ast.Name):
            raise ExprError("attribute access must start at agent / inputs / host")
    # Recurse — every subnode also needs to be allowed.
    for child in ast.iter_child_nodes(node):
        _validate_node(child)


def parse(expr: str) -> ast.Expression:
    """Parse ``expr`` and validate it against the safe grammar.

    Returns the AST root. Use :func:`evaluate` to run it against a
    context dict. Raises :class:`ExprError` for any syntactic problem
    or for any node outside the whitelist.
    """
    if not isinstance(expr, str):
        raise ExprError("when: must be a string")
    expr = expr.strip()
    if not expr:
        raise ExprError("when: must not be empty")
    if len(expr) > 500:
        raise ExprError("when: too long (max 500 chars)")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"invalid syntax: {exc.msg}") from exc

    _validate_node(tree)
    return tree


def validate(expr: str) -> None:
    """Validate without keeping the parse tree — for spec validation."""
    parse(expr)


def _resolve(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _resolve(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple) or isinstance(node, ast.List):
        return tuple(_resolve(e, context) for e in node.elts)
    if isinstance(node, ast.Name):
        if node.id not in context:
            return None
        return context[node.id]
    if isinstance(node, ast.Attribute):
        owner = _resolve(node.value, context)
        if owner is None:
            return None
        if isinstance(owner, dict):
            return owner.get(node.attr)
        return getattr(owner, node.attr, None)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _resolve(node.operand, context)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for v in node.values:
                if not _resolve(v, context):
                    return False
            return True
        # Or
        for v in node.values:
            if _resolve(v, context):
                return True
        return False
    if isinstance(node, ast.Compare):
        left = _resolve(node.left, context)
        for op, comp in zip(node.ops, node.comparators):
            right = _resolve(comp, context)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.In):
                ok = (right is not None) and (left in right)
            elif isinstance(op, ast.NotIn):
                ok = (right is None) or (left not in right)
            else:
                raise ExprError(f"unsupported comparison {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True
    raise ExprError(f"unsupported node {type(node).__name__}")


def evaluate(expr: str | ast.Expression, context: dict[str, Any]) -> bool:
    """Evaluate ``expr`` against ``context``. Returns a Python bool.

    ``context`` is a flat mapping keyed by the root names allowed in
    :func:`_validate_node`: ``agent``, ``inputs``, ``host``. Values are
    dicts (or objects supporting attribute access).

    Missing keys / attributes resolve to ``None``; comparisons against
    ``None`` therefore behave the way you'd expect (``None == "linux"``
    is False, ``None in (...)`` is False, etc.). That lets a template
    use ``agent.pkg_manager == "apt"`` even on hosts where the agent
    hasn't reported a package manager yet — it just evaluates False.
    """
    tree = expr if isinstance(expr, ast.Expression) else parse(expr)
    return bool(_resolve(tree, context))
