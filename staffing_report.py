#!/usr/bin/env python3
"""Visualise imaging's weekday morning staffing coverage and monthly overtime usage."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

from solver import MORNING, load_holiday_config, maximum_allowed, minimum_required, month_days


def read_schedule(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def morning_coverage(
    rows: list[dict[str, str]], year: int, month: int, holiday_dates: dict[dt.date, str]
) -> list[dict]:
    days = month_days(dt.date(year, month, 1))
    actual_by_day = {day: 0 for day in days}
    for row in rows:
        if row["shift"] != "M":
            continue
        day = dt.date.fromisoformat(row["date"])
        if day in actual_by_day:
            actual_by_day[day] += 1
    coverage = []
    for day in days:
        required = minimum_required(day, MORNING, holiday_dates)
        actual = actual_by_day[day]
        coverage.append({
            "date": day,
            "required": required,
            "ceiling": maximum_allowed(day, MORNING, holiday_dates),
            "actual": actual,
            "shortfall": max(0, required - actual),
        })
    return coverage


def render_morning_coverage(coverage: list[dict], output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    days = [row["date"] for row in coverage]
    x = np.arange(len(days))
    actual = [row["actual"] for row in coverage]
    required = [row["required"] for row in coverage]
    colors = ["#C00000" if row["shortfall"] > 0 else "#4472C4" for row in coverage]

    fig, axis = plt.subplots(figsize=(0.34 * len(days) + 4, 6))
    axis.bar(x, actual, color=colors, width=0.7, zorder=3)
    axis.plot(
        x, required, color="#404040", linestyle="--", linewidth=1.4,
        marker="D", markersize=4, zorder=4,
    )
    axis.set_xticks(x, [f"{day.day:02d}\n{day.strftime('%a')}" for day in days], fontsize=7)
    axis.set_ylabel("People on the morning shift")
    axis.set_title(f"Imaging morning staffing — {days[0].strftime('%B %Y')}")
    axis.grid(axis="y", alpha=0.25, zorder=0)
    handles = [
        Patch(color="#4472C4", label="Staffed at/above target"),
        Patch(color="#C00000", label="Understaffed (signalled shortfall)"),
        Line2D([0], [0], color="#404040", linestyle="--", marker="D", markersize=4, label="Required minimum"),
    ]
    axis.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=3, frameon=False)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def render_overtime(overtime_used: list[dict], output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    if not overtime_used:
        return
    ordered = sorted(overtime_used, key=lambda row: -row["extra_hours"])
    names = [row["person"] for row in ordered]
    hours = [row["extra_hours"] for row in ordered]
    x = np.arange(len(names))

    fig, axis = plt.subplots(figsize=(max(6, 0.5 * len(names)), 5.5))
    axis.axhspan(24, 32, color="#70AD47", alpha=0.15, zorder=0, label="Fair-share band (24-32h)")
    axis.bar(x, hours, color="#4472C4", width=0.6, zorder=3)
    axis.axhline(24, color="#70AD47", linestyle="--", linewidth=1, zorder=2)
    axis.axhline(32, color="#70AD47", linestyle="--", linewidth=1, zorder=2)
    axis.set_xticks(x, names, rotation=45, ha="right", fontsize=8)
    axis.set_ylabel("Extra monthly hours")
    axis.set_title("Monthly overtime usage")
    axis.set_ylim(0, 36)
    axis.grid(axis="y", alpha=0.25, zorder=0)
    axis.legend(loc="upper right", frameon=False)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Visualise imaging morning staffing coverage and overtime usage"
    )
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--report", type=Path, required=True, help="solver_report.json from the same solve")
    parser.add_argument("--holidays", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()

    rows = read_schedule(args.schedule)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    dates = [dt.date.fromisoformat(row["date"]) for row in rows]
    year, month = min(dates).year, min(dates).month
    holiday_dates, _, _ = load_holiday_config(args.holidays, year)

    coverage = morning_coverage(rows, year, month, holiday_dates)
    staffing_chart = args.output_dir / "morning_staffing.png"
    overtime_chart = args.output_dir / "overtime_usage.png"
    render_morning_coverage(coverage, staffing_chart)
    render_overtime(report.get("monthly_overtime_used", []), overtime_chart)
    print(json.dumps({
        "morning_staffing_chart": str(staffing_chart),
        "overtime_chart": str(overtime_chart),
        "shortfall_days": sum(1 for row in coverage if row["shortfall"] > 0),
        "overtime_workers": len(report.get("monthly_overtime_used", [])),
    }, indent=2))


if __name__ == "__main__":
    main()
