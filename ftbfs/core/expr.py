"""Restricted evaluator for pipeline `when` expressions.

Supports literals, names, attribute and string subscript access on dicts,
comparisons (== != < <= > >= in, not in, is None), and/or/not.
Anything else (calls, lambdas, comprehensions, ...) is rejected at parse
time, so config files can never execute code.

Missing names or keys evaluate to None rather than raising, so
`triage.fixable != 'no'` is simply true when triage has no such field.
"""

from __future__ import annotations

import ast
import operator
from functools import lru_cache
from typing import Any

_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: b is not None and a in b,
    ast.NotIn: lambda a, b: b is None or a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


class ExprError(ValueError):
    pass


@lru_cache(maxsize=256)
def compile_expr(src: str) -> ast.Expression:
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"bad expression {src!r}: {e.msg}") from e
    for node in ast.walk(tree):
        ok = isinstance(
            node,
            ast.Expression | ast.BoolOp | ast.And | ast.Or | ast.UnaryOp
            | ast.Not | ast.Compare | ast.Name | ast.Load | ast.Attribute
            | ast.Subscript | ast.Constant | ast.List | ast.Tuple,
        ) or type(node) in _CMP
        if not ok:
            raise ExprError(
                f"{type(node).__name__} not allowed in {src!r}"
            )
        if isinstance(node, ast.Subscript) and not (
            isinstance(node.slice, ast.Constant)
        ):
            raise ExprError(f"only constant subscripts allowed in {src!r}")
    return tree


def _get(obj: Any, key: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    if isinstance(obj, list | tuple) and isinstance(key, int):
        return obj[key] if -len(obj) <= key < len(obj) else None
    return None


def _eval(node: ast.AST, env: dict) -> Any:
    match node:
        case ast.Expression(body=body):
            return _eval(body, env)
        case ast.Constant(value=v):
            return v
        case ast.Name(id=name):
            return env.get(name)
        case ast.Attribute(value=v, attr=attr):
            return _get(_eval(v, env), attr)
        case ast.Subscript(value=v, slice=ast.Constant(value=k)):
            return _get(_eval(v, env), k)
        case ast.List(elts=elts) | ast.Tuple(elts=elts):
            return [_eval(e, env) for e in elts]
        case ast.UnaryOp(op=ast.Not(), operand=o):
            return not _eval(o, env)
        case ast.BoolOp(op=ast.And(), values=values):
            return all(_eval(v, env) for v in values)
        case ast.BoolOp(op=ast.Or(), values=values):
            return any(_eval(v, env) for v in values)
        case ast.Compare(left=left, ops=ops, comparators=comps):
            a = _eval(left, env)
            for op, c in zip(ops, comps, strict=True):
                b = _eval(c, env)
                try:
                    if not _CMP[type(op)](a, b):
                        return False
                except TypeError:
                    return False  # e.g. None < 3
                a = b
            return True
    raise ExprError(f"unsupported node {type(node).__name__}")


def evaluate(src: str, env: dict) -> bool:
    return bool(_eval(compile_expr(src), env))
