#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "scripts" / "math" / "alice-engineering-calculator.py"


def run(expr: str):
    out = subprocess.check_output([sys.executable, str(CALC), expr], text=True)
    return json.loads(out)


def assert_ok(expr: str):
    data = run(expr)
    assert data["ok"], data
    assert data["usedLlm"] is False
    assert data["elapsedMs"] < 500, data
    return data


def test_arithmetic():
    data = assert_ok("2+3*4")
    assert data["exact"] == "14"


def test_derivative_chinese():
    data = assert_ok("微分 x^3 + 2*x 對 x")
    assert data["exact"] == "3*x**2 + 2"


def test_definite_integral_chinese():
    data = assert_ok("積分 x^2 從 0 到 1")
    assert data["exact"] == "1/3"


def test_solve_equation():
    data = assert_ok("解 x^2-5*x+6=0")
    assert json.loads(data["exact"]) == ["2", "3"]


def test_limit():
    data = assert_ok("limit sin(x)/x x->0")
    assert data["exact"] == "1"


def test_matrix_determinant():
    data = assert_ok("det [[1,2],[3,4]]")
    assert data["exact"] == "-2"


def main():
    for fn in [test_arithmetic, test_derivative_chinese, test_definite_integral_chinese, test_solve_equation, test_limit, test_matrix_determinant]:
        fn()
    print("MATH_CALCULATOR_TESTS_OK")


if __name__ == "__main__":
    main()
