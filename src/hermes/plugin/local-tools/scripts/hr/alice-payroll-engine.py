#!/usr/bin/env python3
"""Alice HR payroll engine.

Reads employee master data + attendance CSV and emits a payroll workbook.
No LLM is used for deterministic payroll math.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import time
import zipfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "alice-payroll-config.2026.tw.json"


def money(value: Any) -> int:
    try:
        d = Decimal(str(value or 0))
    except Exception:
        d = Decimal("0")
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def text(value: Any) -> str:
    return str(value or "").strip()


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def bracket(amount: int, grades: list[int]) -> int:
    if not grades:
        return amount
    for grade in grades:
        if amount <= int(grade):
            return int(grade)
    return int(grades[-1])


def risk(flags: list[str], code: str, condition: bool) -> None:
    if condition:
        flags.append(code)


@dataclass
class PayrollRow:
    employee_id: str
    name: str
    plan: str
    plan_label: str
    base_salary: int
    meal_allowance: int
    taxable_meal_allowance: int
    tax_exempt_meal_allowance: int
    other_recurring_allowance: int
    bonus: int
    overtime_pay: int
    absence_deduction: int
    unpaid_leave_deduction: int
    late_deduction: int
    gross_pay: int
    taxable_income_estimate: int
    regular_wage_for_insurance: int
    computed_labor_insurance_salary: int
    declared_labor_insurance_salary: int
    computed_nhi_salary: int
    declared_nhi_salary: int
    labor_insurance_employee: int
    employment_insurance_employee: int
    nhi_employee: int
    pension_employer: int
    net_pay_estimate: int
    employer_cost_estimate: int
    risk_flags: str


def calculate_employee(emp: dict[str, str], att: dict[str, str], config: dict[str, Any]) -> PayrollRow:
    plan_id = text(emp.get("plan")) or "compliant_standard"
    plan = config["payrollPlans"].get(plan_id, config["payrollPlans"]["compliant_standard"])
    base = money(emp.get("base_salary"))
    meal = money(emp.get("meal_allowance"))
    recurring = money(emp.get("other_recurring_allowance"))
    bonus = money(emp.get("bonus"))
    pension_rate = num(emp.get("pension_rate")) or float(config["laborPension"]["defaultEmployerRate"])

    hourly_base = base / 240 if base else 0
    overtime_pay = money(
        hourly_base * 1.34 * num(att.get("overtime_1_34_hours"))
        + hourly_base * 1.67 * num(att.get("overtime_1_67_hours"))
        + hourly_base * 2.67 * num(att.get("overtime_2_67_hours"))
    )
    unpaid_leave_deduction = money((base / 30 if base else 0) * num(att.get("unpaid_leave_days")))
    absence_deduction = money(hourly_base * num(att.get("absence_hours")))
    late_deduction = money((hourly_base / 60) * num(att.get("late_minutes")))

    meal_cap = money(config["tax"]["mealAllowanceMonthlyExemptCap"])
    if plan.get("mealAllowanceMode") == "tax_exempt_cap_then_taxable":
        tax_exempt_meal = min(meal, meal_cap)
        taxable_meal = max(0, meal - meal_cap)
    else:
        tax_exempt_meal = 0
        taxable_meal = meal

    gross = base + meal + recurring + bonus + overtime_pay - unpaid_leave_deduction - absence_deduction - late_deduction
    taxable_income = base + taxable_meal + recurring + bonus + overtime_pay - unpaid_leave_deduction - absence_deduction - late_deduction
    regular_wage = base + meal + recurring

    computed_labor_grade = bracket(regular_wage, config["laborInsurance"]["salaryGrades"])
    computed_nhi_grade = bracket(regular_wage, config["nhi"]["salaryGrades"])
    declared_labor = money(emp.get("declared_labor_insurance_salary")) or computed_labor_grade
    declared_nhi = money(emp.get("declared_nhi_salary")) or computed_nhi_grade

    flags: list[str] = []
    risk(flags, "MEAL_ALLOWANCE_OVER_TAX_EXEMPT_CAP", meal > meal_cap)
    risk(flags, "DECLARED_LABOR_INSURANCE_BELOW_COMPUTED_GRADE", declared_labor < computed_labor_grade)
    risk(flags, "DECLARED_NHI_BELOW_COMPUTED_GRADE", declared_nhi < computed_nhi_grade)
    risk(flags, "GROSS_BELOW_MINIMUM_WAGE", gross < money(config["minimumWage"]["monthly"]))
    risk(flags, "HIGH_RISK_PLAN_REVIEW_REQUIRED", plan.get("riskLevel") == "high")

    labor_rate = Decimal(str(config["laborInsurance"]["ordinaryAccidentRate"]))
    employment_rate = Decimal(str(config["laborInsurance"]["employmentInsuranceRate"]))
    employee_share = Decimal(str(config["laborInsurance"]["employeeShare"]))
    nhi_rate = Decimal(str(config["nhi"]["generalPremiumRate"]))
    nhi_employee_share = Decimal(str(config["nhi"]["employeeShareRatio"]))
    dependents = max(0, money(emp.get("dependents")))
    nhi_people = 1 + dependents

    labor_employee = money(Decimal(declared_labor) * labor_rate * employee_share)
    employment_employee = money(Decimal(declared_labor) * employment_rate * employee_share)
    nhi_employee = money(Decimal(declared_nhi) * nhi_rate * nhi_employee_share * Decimal(nhi_people))
    pension_employer = money(Decimal(bracket(regular_wage, config["laborPension"]["wageGrades"])) * Decimal(str(pension_rate)))
    net = gross - labor_employee - employment_employee - nhi_employee
    employer_cost = gross + pension_employer

    return PayrollRow(
        employee_id=text(emp.get("employee_id")),
        name=text(emp.get("name")),
        plan=plan_id,
        plan_label=plan.get("label", plan_id),
        base_salary=base,
        meal_allowance=meal,
        taxable_meal_allowance=taxable_meal,
        tax_exempt_meal_allowance=tax_exempt_meal,
        other_recurring_allowance=recurring,
        bonus=bonus,
        overtime_pay=overtime_pay,
        absence_deduction=absence_deduction,
        unpaid_leave_deduction=unpaid_leave_deduction,
        late_deduction=late_deduction,
        gross_pay=gross,
        taxable_income_estimate=taxable_income,
        regular_wage_for_insurance=regular_wage,
        computed_labor_insurance_salary=computed_labor_grade,
        declared_labor_insurance_salary=declared_labor,
        computed_nhi_salary=computed_nhi_grade,
        declared_nhi_salary=declared_nhi,
        labor_insurance_employee=labor_employee,
        employment_insurance_employee=employment_employee,
        nhi_employee=nhi_employee,
        pension_employer=pension_employer,
        net_pay_estimate=net,
        employer_cost_estimate=employer_cost,
        risk_flags=";".join(flags),
    )


def rows_to_table(rows: list[Any]) -> list[list[Any]]:
    if not rows:
        return []
    headers = list(rows[0].__dataclass_fields__.keys())
    table = [headers]
    for row in rows:
        table.append([getattr(row, h) for h in headers])
    return table


def sheet_xml(rows: list[list[Any]]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    out.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    out.append("<sheetData>")
    for r_idx, row in enumerate(rows, start=1):
        out.append(f'<row r="{r_idx}">')
        for c_idx, value in enumerate(row, start=1):
            ref = f"{col_name(c_idx)}{r_idx}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                out.append(f'<c r="{ref}" t="inlineStr"><is><t>{html.escape(str(value or ""))}</t></is></c>')
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def col_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def write_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook_sheets = []
    rels = []
    content_overrides = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        for idx, (name, rows) in enumerate(sheets.items(), start=1):
            safe_name = name[:31].replace("/", "-")
            workbook_sheets.append(f'<sheet name="{html.escape(safe_name)}" sheetId="{idx}" r:id="rId{idx}"/>')
            rels.append(f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
            content_overrides.append(f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml(rows))
        zf.writestr("[Content_Types].xml", f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{"".join(content_overrides)}</Types>')
        zf.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{"".join(workbook_sheets)}</sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(rels)}</Relationships>')


def generate(args: argparse.Namespace) -> dict[str, Any]:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    employees = load_csv(args.employees)
    attendance_rows = load_csv(args.attendance)
    attendance = {text(row.get("employee_id")): row for row in attendance_rows}
    payroll = [calculate_employee(emp, attendance.get(text(emp.get("employee_id")), {}), config) for emp in employees]
    risk_rows = [["employee_id", "name", "risk_flags"]]
    for row in payroll:
        if row.risk_flags:
            risk_rows.append([row.employee_id, row.name, row.risk_flags])
    sheets = {
        "Payroll": rows_to_table(payroll),
        "RiskFlags": risk_rows,
        "Employees": [list(employees[0].keys())] + [[r.get(k, "") for k in employees[0].keys()] for r in employees] if employees else [],
        "Attendance": [list(attendance_rows[0].keys())] + [[r.get(k, "") for k in attendance_rows[0].keys()] for r in attendance_rows] if attendance_rows else [],
        "Parameters": [["key", "value"], ["config_version", config.get("version")], ["generated_at", time.strftime("%Y-%m-%dT%H:%M:%S%z")], ["note", "本表為薪資試算與作業用，不協助規避投保或稅務申報。"]],
    }
    write_xlsx(args.out, sheets)
    return {
        "ok": True,
        "employees": len(payroll),
        "riskRows": max(0, len(risk_rows) - 1),
        "out": str(args.out),
        "usedLlm": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alice HR payroll engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="Generate payroll workbook from employee and attendance CSV")
    gen.add_argument("--employees", type=Path, required=True)
    gen.add_argument("--attendance", type=Path, required=True)
    gen.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    gen.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "generate":
        print(json.dumps(generate(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
