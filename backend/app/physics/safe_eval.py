"""A tiny, allow-listed expression evaluator for rule packs. Never eval().

Allowed: numbers, input names, + - * / **, unary - + not, comparisons (chained), and/or, and the
functions abs, min, max, sqrt, plus delta(<input name>) for transition rules. Everything else
(attributes, subscripts, strings, lambdas, comprehensions, other calls) raises UnsafeExpression.
"""

import ast
import math
import operator

MAX_EXPRESSION_CHARS = 500
MAX_EXPONENT = 10

_FUNCS = {"abs": abs, "min": min, "max": max, "sqrt": math.sqrt}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow}
_COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.Eq: operator.eq, ast.NotEq: operator.ne}


class UnsafeExpression(ValueError):
    """The expression uses something outside the allow-list (a bug in a rule pack, never user input)."""


class MissingInput(KeyError):
    """A name has no value (or a division by zero): the rule can't be evaluated, so it is skipped."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def safe_eval(expression: str, names: dict, deltas: dict | None = None):
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise UnsafeExpression("expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"syntax error: {exc.msg}") from exc
    return _eval(tree.body, names, deltas or {})


def _number(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsafeExpression(f"{name} is not a number")
    return value


def _eval(node, names: dict, deltas: dict):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
            return node.value
        raise UnsafeExpression(f"constant {node.value!r} is not allowed")
    if isinstance(node, ast.Name):
        if node.id not in names or names[node.id] is None:
            raise MissingInput(node.id)
        return _number(names[node.id], node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left, names, deltas), _eval(node.right, names, deltas)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise UnsafeExpression("exponent too large")
        if isinstance(node.op, ast.Div) and right == 0:
            raise MissingInput("<division by zero>")
        return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, names, deltas)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.Not):
            return not value
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval(v, names, deltas) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval(v, names, deltas) for v in node.values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names, deltas)
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _COMPARE:
                raise UnsafeExpression(f"comparison {type(op).__name__} is not allowed")
            right = _eval(comparator, names, deltas)
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise UnsafeExpression("starred arguments are not allowed")
        if node.func.id == "delta":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Name):
                raise UnsafeExpression("delta() takes exactly one input name")
            name = node.args[0].id
            if name not in deltas or deltas[name] is None:
                raise MissingInput(f"delta({name})")
            return deltas[name]
        if node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*(_eval(a, names, deltas) for a in node.args))
    raise UnsafeExpression(f"{type(node).__name__} is not allowed in rule expressions")
