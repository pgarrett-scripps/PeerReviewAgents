"""Restricted manuscript access and bounded deterministic arithmetic checks."""

import ast
import math
import operator

FUNCTIONS = {"sum": sum, "min": min, "max": max, "len": len, "abs": abs,
             "round": round, "sqrt": math.sqrt, "log": math.log, "exp": math.exp,
             "mean": lambda xs: sum(xs) / len(xs)}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
          ast.Pow: operator.pow}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def calculate(expression):
    if not isinstance(expression, str) or len(expression) > 4000:
        raise ValueError("Expression must contain at most 4000 characters")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 600:
        raise ValueError("Expression is too large")

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float, bool):
            value = node.value
        elif isinstance(node, (ast.List, ast.Tuple)):
            value = [visit(x) for x in node.elts]
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub, ast.Not):
            operand = visit(node.operand)
            value = not operand if isinstance(node.op, ast.Not) else operand if isinstance(node.op, ast.UAdd) else -operand
        elif isinstance(node, ast.BinOp) and type(node.op) in BINARY:
            left, right = visit(node.left), visit(node.right)
            if type(left) not in (int, float) or type(right) not in (int, float):
                raise ValueError("Arithmetic operands must be numbers")
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent exceeds bound")
            value = BINARY[type(node.op)](left, right)
        elif isinstance(node, ast.Compare) and all(type(op) in COMPARE for op in node.ops):
            values = [visit(node.left)] + [visit(x) for x in node.comparators]
            value = all(COMPARE[type(op)](a, b) for op, a, b in zip(node.ops, values, values[1:]))
        elif isinstance(node, ast.BoolOp) and type(node.op) in (ast.And, ast.Or):
            values = [bool(visit(x)) for x in node.values]
            value = all(values) if isinstance(node.op, ast.And) else any(values)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS and not node.keywords:
            value = FUNCTIONS[node.func.id](*[visit(x) for x in node.args])
        else:
            raise ValueError("Only bounded numeric, list, Boolean expressions and documented functions are allowed")
        if isinstance(value, (int, float)) and (abs(value) > 1e100 or not math.isfinite(value)):
            raise ValueError("Numeric result exceeds bound")
        if not isinstance(value, (int, float, bool, list)):
            raise ValueError("Unsupported result type")
        return value

    return visit(tree.body)

