"""Tools available to agents, exposed to models in OpenAI function-calling format.

Each tool is a plain function ``(args, ctx) -> str``. A tool signals failure by raising
:class:`ToolError`; the runtime turns that into an ``ERROR: ...`` tool message so the model can
see the failure and decide whether to retry, exactly as it would with a real flaky API.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.clients.search import SearchClient, SearchError


class ToolError(Exception):
    """A tool failed; the message is shown to the model."""


@dataclass
class ToolContext:
    search: SearchClient
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    search_route: str | None = None
    """How the last web search went out (``rest``, ``bifrost-mcp``, ``mock``): shown on the trace."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[dict[str, Any], ToolContext], str]

    def definition(self) -> dict[str, Any]:
        """OpenAI ``tools`` entry."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


# ------------------------------------------------------------------------------------ web_search
def _web_search(args: dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("missing required argument 'query'")
    try:
        res = ctx.search.search(query, max_results=4, topic="general", include_answer=True)
    except SearchError as exc:
        raise ToolError(f"search failed: {exc.message}") from exc
    ctx.search_route = res.route
    if not res.results and not res.answer:
        return "No results found."
    lines = []
    if res.answer:
        lines.append(f"Summary: {res.answer}")
    for i, r in enumerate(res.results, 1):
        date = f" ({r.published_date})" if r.published_date else ""
        lines.append(f"[{i}] {r.title}{date} - {r.url}\n    {r.content[:400]}")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------ calculator
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_MAX_EXPONENT = 1000
_MAX_MAGNITUDE = 1e100


def safe_eval(expression: str) -> float | int:
    """Evaluate an arithmetic expression without ``eval``: numbers, + - * / // % ** (or ^),
    parentheses, a few math functions and the constants ``pi`` and ``e``."""
    expr = expression.replace("^", "**").replace("×", "*").replace("÷", "/")
    expr = _THOUSANDS.sub("", expr)  # "1,000,000" -> "1000000", but keep "round(x, 2)"
    if len(expr) > 300:
        raise ToolError("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"invalid expression: {expression!r}") from exc

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
        ):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
                raise ToolError("exponent too large")
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
        ):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ToolError(f"unsupported syntax in expression: {ast.dump(node)[:60]}")

    try:
        value = ev(tree)
    except ZeroDivisionError as exc:
        raise ToolError("division by zero") from exc
    except (ValueError, OverflowError, TypeError) as exc:
        raise ToolError(f"math error: {exc}") from exc
    if isinstance(value, float) and (math.isnan(value) or abs(value) > _MAX_MAGNITUDE):
        raise ToolError("result out of range")
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def _calculator(args: dict[str, Any], ctx: ToolContext) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        raise ToolError("missing required argument 'expression'")
    value = safe_eval(expression)
    if isinstance(value, float):
        return f"{expression} = {round(value, 10)}"
    return f"{expression} = {value}"


# ------------------------------------------------------------------------------------ current_date
def _current_date(args: dict[str, Any], ctx: ToolContext) -> str:
    now = ctx.now()
    return f"Today is {now.strftime('%A')}, {now.date().isoformat()} (UTC time {now.strftime('%H:%M')})."


# ------------------------------------------------------------------------------------ registry
TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            name="web_search",
            description="Search the live web for up-to-date information. Returns a summary and sources "
            "with publication dates. Use for current events, recent facts or anything you are unsure of.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
            fn=_web_search,
        ),
        Tool(
            name="calculator",
            description="Evaluate an arithmetic expression exactly. Supports + - * / // % ** "
            "parentheses, sqrt, abs, round, min, max, log, log10, exp, pi, e.",
            parameters={
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "e.g. (17 * 23) + 4"}},
                "required": ["expression"],
            },
            fn=_calculator,
        ),
        Tool(
            name="current_date",
            description="Get today's date and day of the week (UTC).",
            parameters={"type": "object", "properties": {}},
            fn=_current_date,
        ),
    )
}


def tool_definitions(names: list[str]) -> list[dict[str, Any]]:
    """OpenAI tool definitions for the given tool names (unknown names are ignored)."""
    return [TOOLS[n].definition() for n in names if n in TOOLS]


def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    """Execute a tool by name; raises :class:`ToolError` on any failure."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool '{name}'. Available: {', '.join(TOOLS)}")
    if "_raw" in args:
        raise ToolError(f"arguments were not valid JSON: {str(args['_raw'])[:100]}")
    return tool.fn(args, ctx)
