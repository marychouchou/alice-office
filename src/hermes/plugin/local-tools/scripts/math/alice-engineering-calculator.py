#!/usr/bin/env python3
"""Alice engineering calculator fast path.

This tool intentionally does not call any LLM. It handles deterministic
engineering math with SymPy/Numpy and returns compact JSON for Alice routers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import sympy as sp

MAX_INPUT_CHARS = 2000
DEFAULT_SYMBOL = "x"

SAFE_GLOBALS: dict[str, Any] = {
    "Abs": sp.Abs,
    "E": sp.E,
    "I": sp.I,
    "Matrix": sp.Matrix,
    "N": sp.N,
    "O": sp.O,
    "Rational": sp.Rational,
    "acos": sp.acos,
    "asin": sp.asin,
    "atan": sp.atan,
    "cos": sp.cos,
    "cosh": sp.cosh,
    "det": lambda x: sp.Matrix(x).det(),
    "diff": sp.diff,
    "e": sp.E,
    "exp": sp.exp,
    "factor": sp.factor,
    "integrate": sp.integrate,
    "ln": sp.log,
    "log": sp.log,
    "pi": sp.pi,
    "simplify": sp.simplify,
    "sin": sp.sin,
    "sinh": sp.sinh,
    "sqrt": sp.sqrt,
    "tan": sp.tan,
    "tanh": sp.tanh,
}

TRANSLATIONS = str.maketrans({
    "（": "(", "）": ")", "［": "[", "］": "]", "【": "[", "】": "]",
    "，": ",", "。": ".", "：": ":", "；": ";", "＝": "=", "＋": "+",
    "－": "-", "×": "*", "÷": "/", "＾": "^", "√": "sqrt",
})


@dataclass
class CalcResult:
    ok: bool
    mode: str
    input: str
    answer: Any = None
    exact: str | None = None
    decimal: str | None = None
    latex: str | None = None
    steps: list[str] | None = None
    error: str | None = None
    elapsedMs: float = 0.0
    usedLlm: bool = False

    def to_json(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "mode": self.mode,
            "input": self.input,
            "usedLlm": self.usedLlm,
            "elapsedMs": round(self.elapsedMs, 3),
        }
        if self.answer is not None:
            data["answer"] = self.answer
        if self.exact is not None:
            data["exact"] = self.exact
        if self.decimal is not None:
            data["decimal"] = self.decimal
        if self.latex is not None:
            data["latex"] = self.latex
        if self.steps:
            data["steps"] = self.steps
        if self.error:
            data["error"] = self.error
        return data


def normalize(raw: str) -> str:
    text = str(raw or "").strip().translate(TRANSLATIONS)
    text = text.replace("^", "**")
    text = re.sub(r"\s+", " ", text)
    replacements = {
        "求導": "derivative",
        "導數": "derivative",
        "微分": "derivative",
        "積分": "integrate",
        "定積分": "integrate",
        "解方程式": "solve",
        "解方程": "solve",
        "求解": "solve",
        "極限": "limit",
        "行列式": "det",
        "反矩陣": "inverse",
        "矩陣反矩陣": "inverse",
        "對": " with respect to ",
        "從": " from ",
        "到": " to ",
    }
    for zh, en in replacements.items():
        text = text.replace(zh, en)
    text = re.sub(r"^解\s+", "solve ", text)
    return text.strip()


def symbol_table(expr: str) -> dict[str, Any]:
    names = set(re.findall(r"\b[a-zA-Z_]\w*\b", expr))
    table = dict(SAFE_GLOBALS)
    for name in names:
        if name not in table:
            table[name] = sp.Symbol(name)
    table.setdefault(DEFAULT_SYMBOL, sp.Symbol(DEFAULT_SYMBOL))
    return table


def parse_expr(expr: str) -> Any:
    expr = expr.strip()
    if not expr:
        raise ValueError("empty expression")
    return sp.sympify(expr, locals=symbol_table(expr), convert_xor=True)


def parse_symbol(text: str, fallback: str = DEFAULT_SYMBOL) -> sp.Symbol:
    match = re.search(r"(?:with respect to|wrt|by|d/d)\s*([a-zA-Z]\w*)", text, re.I)
    if match:
        return sp.Symbol(match.group(1))
    match = re.search(r"\bfor\s+([a-zA-Z]\w*)\b", text, re.I)
    if match:
        return sp.Symbol(match.group(1))
    return sp.Symbol(fallback)


def split_equation(expr: str) -> Any:
    if "=" in expr:
        left, right = expr.split("=", 1)
        return sp.Eq(parse_expr(left), parse_expr(right))
    return parse_expr(expr)


def result_from_expr(mode: str, raw: str, expr: Any, steps: list[str] | None = None) -> CalcResult:
    simplified = sp.simplify(expr)
    exact = sp.sstr(simplified)
    decimal = None
    try:
        numeric = sp.N(simplified, 15)
        if numeric != simplified or simplified.is_number:
            decimal = sp.sstr(numeric)
    except Exception:
        decimal = None
    return CalcResult(
        ok=True,
        mode=mode,
        input=raw,
        answer=exact,
        exact=exact,
        decimal=decimal,
        latex=sp.latex(simplified),
        steps=steps or [],
    )


def matrix_from_text(text: str) -> sp.Matrix:
    match = re.search(r"(\[\s*\[.*\]\s*\])", text)
    if not match:
        raise ValueError("matrix input must include [[...], [...]]")
    data = json.loads(match.group(1))
    return sp.Matrix(data)


def calculate(raw: str) -> CalcResult:
    original = str(raw or "").strip()
    try:
        if len(original) > MAX_INPUT_CHARS:
            raise ValueError(f"input too long: max {MAX_INPUT_CHARS} chars")
        text = normalize(original)
        lower = text.lower()

        if lower.startswith("derivative") or lower.startswith("differentiate") or "d/d" in lower:
            expr_text = re.sub(r"^(derivative|differentiate)\s*", "", text, flags=re.I)
            expr_text = re.split(r"\b(?:with respect to|wrt|by)\b", expr_text, maxsplit=1, flags=re.I)[0].strip()
            var = parse_symbol(text)
            expr = parse_expr(expr_text)
            out = sp.diff(expr, var)
            return result_from_expr("derivative", original, out, [f"differentiate {sp.sstr(expr)} with respect to {var}"])

        if lower.startswith("integrate"):
            expr_text = re.sub(r"^integrate\s*", "", text, flags=re.I).strip()
            bounds = re.search(r"\bfrom\s*([^ ]+)\s*to\s*([^ ]+)", expr_text, re.I)
            var = parse_symbol(text)
            expr_text = re.split(r"\bfrom\b|\bwith respect to\b|\bwrt\b", expr_text, maxsplit=1, flags=re.I)[0].strip()
            expr = parse_expr(expr_text)
            if bounds:
                out = sp.integrate(expr, (var, parse_expr(bounds.group(1)), parse_expr(bounds.group(2))))
                return result_from_expr("definite_integral", original, out, [f"integrate {sp.sstr(expr)} from {bounds.group(1)} to {bounds.group(2)}"])
            out = sp.integrate(expr, var)
            return result_from_expr("indefinite_integral", original, out, [f"integrate {sp.sstr(expr)} with respect to {var}"])

        if lower.startswith("solve"):
            expr_text = re.sub(r"^solve\s*", "", text, flags=re.I).strip()
            var = parse_symbol(text)
            expr_text = re.split(r"\bfor\s+[a-zA-Z]\w*\b", expr_text, maxsplit=1, flags=re.I)[0].strip()
            equation = split_equation(expr_text)
            solutions = sp.solve(equation, var)
            exact = [sp.sstr(sp.simplify(x)) for x in solutions]
            return CalcResult(ok=True, mode="solve", input=original, answer=exact, exact=json.dumps(exact, ensure_ascii=False), latex=sp.latex(solutions), steps=[f"solve for {var}"])

        if lower.startswith("limit"):
            expr_text = re.sub(r"^limit\s*", "", text, flags=re.I).strip()
            match = re.search(r"(.+?)\s+([a-zA-Z]\w*)\s*(?:->|→)\s*([^ ]+)$", expr_text)
            if not match:
                raise ValueError("limit format: limit sin(x)/x x->0")
            expr = parse_expr(match.group(1))
            var = sp.Symbol(match.group(2))
            point = parse_expr(match.group(3))
            out = sp.limit(expr, var, point)
            return result_from_expr("limit", original, out, [f"limit as {var} approaches {sp.sstr(point)}"])

        if lower.startswith("det"):
            mat = matrix_from_text(text)
            return result_from_expr("determinant", original, mat.det(), [f"determinant of {mat.rows}x{mat.cols} matrix"])

        if lower.startswith("inverse"):
            mat = matrix_from_text(text)
            inv = mat.inv()
            return CalcResult(ok=True, mode="matrix_inverse", input=original, answer=inv.tolist(), exact=sp.sstr(inv), latex=sp.latex(inv), steps=[f"inverse of {mat.rows}x{mat.cols} matrix"])

        if lower.startswith("linear"):
            mats = re.findall(r"\[[^\[\]]*(?:\[[^\[\]]+\][^\[\]]*)+\]|\[[^\[\]]+\]", text)
            if len(mats) < 2:
                raise ValueError("linear format: linear [[2,1],[1,-1]] [5,1]")
            a = sp.Matrix(json.loads(mats[0]))
            b = sp.Matrix(json.loads(mats[1]))
            sol = a.LUsolve(b)
            return CalcResult(ok=True, mode="linear_solve", input=original, answer=list(map(sp.sstr, sol)), exact=sp.sstr(sol), latex=sp.latex(sol), steps=[f"solve Ax=b for {a.rows} equations"])

        expr = parse_expr(text)
        return result_from_expr("expression", original, expr, ["evaluate expression"])
    except Exception as exc:
        return CalcResult(ok=False, mode="error", input=original, error=str(exc))


def calculate_with_timing(raw: str) -> dict[str, Any]:
    started = time.perf_counter()
    result = calculate(raw)
    result.elapsedMs = (time.perf_counter() - started) * 1000
    return result.to_json()


class CalculatorHandler(BaseHTTPRequestHandler):
    server_version = "AliceEngineeringCalculator/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True, "service": "alice-engineering-calculator", "usedLlm": False})
            return
        if parsed.path != "/calculate":
            self._send(404, {"ok": False, "error": "not found"})
            return
        query = parse_qs(parsed.query)
        expr = query.get("q", [""])[0]
        self._send(200, calculate_with_timing(expr))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/calculate":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("content-length", "0"))
        if length > MAX_INPUT_CHARS * 4:
            self._send(413, {"ok": False, "error": "payload too large"})
            return
        body = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            payload = {"q": body}
        expr = payload.get("q") or payload.get("expression") or ""
        self._send(200, calculate_with_timing(expr))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(json.dumps({"event": "math_http", "message": fmt % args}, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alice high-level engineering calculator fast path")
    parser.add_argument("expression", nargs="*", help="Expression or command, e.g. '微分 x^3 對 x'")
    parser.add_argument("--serve", action="store_true", help="Run local HTTP calculator service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    args = parser.parse_args(argv)

    if args.serve:
        server = ThreadingHTTPServer((args.host, args.port), CalculatorHandler)
        print(json.dumps({"event": "started", "service": "alice-engineering-calculator", "host": args.host, "port": args.port}, ensure_ascii=False), flush=True)
        server.serve_forever()
        return 0

    expr = " ".join(args.expression).strip()
    if not expr:
        expr = sys.stdin.read().strip()
    print(json.dumps(calculate_with_timing(expr), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
