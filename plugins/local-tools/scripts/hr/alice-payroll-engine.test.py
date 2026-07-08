#!/usr/bin/env python3
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "hr" / "alice-payroll-engine.py"
EMP = ROOT / "scripts" / "hr" / "sample-employees.csv"
ATT = ROOT / "scripts" / "hr" / "sample-attendance.csv"


def main():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "payroll.xlsx"
        raw = subprocess.check_output([
            sys.executable, str(TOOL), "generate",
            "--employees", str(EMP),
            "--attendance", str(ATT),
            "--out", str(out),
        ], text=True)
        data = json.loads(raw)
        assert data["ok"] is True, data
        assert data["employees"] == 4, data
        assert data["riskRows"] >= 1, data
        assert out.exists() and out.stat().st_size > 1000, out
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "xl/workbook.xml" in names
            assert "xl/worksheets/sheet1.xml" in names
            sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            assert "Payroll" not in sheet
            assert "DECLARED_LABOR_INSURANCE_BELOW_COMPUTED_GRADE" in zf.read("xl/worksheets/sheet2.xml").decode("utf-8")
    print("ALICE_PAYROLL_TESTS_OK")


if __name__ == "__main__":
    main()
