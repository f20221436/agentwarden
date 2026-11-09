"""Safe arithmetic: an AST allowlist evaluator. Never uses eval()."""

from __future__ import annotations

import ast
import math
import operator

from pydantic import BaseModel, ConfigDict, Field

from agentwarden.gateway.spec import ToolContext, ToolOutput, ToolSpec
from agentwarden.reliability.errors import PermanentToolError

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "abs": abs,
          "round": round, "min": min, "max": max}
MAX_ABS = 1e15


class CalcError(PermanentToolError):
    error_type = "calculation_error"


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 1e6):
            raise CalcError("exponent or base too large")
        result = _BIN[type(node.op)](left, right)
        if isinstance(result, complex) or abs(result) > MAX_ABS:
            raise CalcError("result out of range")
        return result
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        if len(node.args) > 8:
            raise CalcError("too many arguments")
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise CalcError(f"unsupported expression element: {type(node).__name__}")


def evaluate(expression: str) -> float:
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except SyntaxError as e:
        raise CalcError(f"syntax error: {e.msg}") from e
    try:
        return _eval(tree)
    except ZeroDivisionError as e:
        raise CalcError("division by zero") from e
    except (ValueError, OverflowError, TypeError) as e:
        raise CalcError(str(e)) from e


class CalcArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expression: str = Field(..., min_length=1, max_length=300,
                            description="Arithmetic expression, e.g. '(137/1976)/(210/2024) - 1'. "
                                        "Supports + - * / // % ** and sqrt, log, exp, abs, round, min, max.")


def make_calculator_tool() -> ToolSpec:
    def run(args: CalcArgs, ctx: ToolContext) -> ToolOutput:
        value = evaluate(args.expression)
        shown = f"{value:.10g}" if isinstance(value, float) else str(value)
        return ToolOutput(content=f"{args.expression} = {shown}", data=value)

    return ToolSpec(
        name="calculator",
        description="Evaluate an arithmetic expression exactly. Use it for rates, ratios, lifts and percentages.",
        args_model=CalcArgs,
        fn=run,
        required_capabilities=frozenset({"math"}),
        timeout_s=2.0,
        max_calls_per_execution=10,
    )
