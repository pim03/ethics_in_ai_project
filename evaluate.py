#!/usr/bin/env python3
"""Evaluate and visualise fairness in a radiology technician schedule."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
from statistics import fmean, pstdev
from typing import Iterable

from solver import NIGHT, PEOPLE, SHIFTS, allowed, load_department, month_days, weekend_id


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def gini(values: Iterable[float]) -> float:
    data = sorted(max(0.0, float(value)) for value in values)
    if not data or sum(data) == 0:
        return 0.0
    n = len(data)
    return sum((2 * index - n - 1) * value for index, value in enumerate(data, 1)) / (n * sum(data))


def jain_index(values: Iterable[float]) -> float:
    data = [max(0.0, float(value)) for value in values]
    denominator = len(data) * sum(value * value for value in data)
    return safe_ratio(sum(data) ** 2, denominator) if data else 1.0


def distribution(values: Iterable[float]) -> dict[str, float | int]:
    data = [float(value) for value in values]
    if not data:
        return {"count": 0, "mean": 0.0, "minimum": 0.0, "maximum": 0.0, "range": 0.0,
                "standard_deviation": 0.0, "gini": 0.0, "jain_index": 1.0}
    return {
        "count": len(data),
        "mean": round(fmean(data), 4),
        "minimum": round(min(data), 4),
        "maximum": round(max(data), 4),
        "range": round(max(data) - min(data), 4),
        "standard_deviation": round(pstdev(data), 4),
        "gini": round(gini(data), 4),
        "jain_index": round(jain_index(data), 4),
    }


def load_vacations(config_path: Path, department: str = "imagiologia") -> dict[str, set[dt.date]]:
    return load_department(config_path, department)


def read_schedule(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("Schedule is empty")
    required = {"date", "person", "shift"}
    if not required.issubset(rows[0]):
        raise ValueError(f"Schedule must contain: {', '.join(sorted(required))}")
    return rows


def evaluate(rows: list[dict[str, str]], vacations: dict[str, set[dt.date]]) -> dict:
    dates = [dt.date.fromisoformat(row["date"]) for row in rows]
    first = min(dates).replace(day=1)
    days = month_days(first)
    assigned: dict[str, list[tuple[dt.date, str]]] = {name: [] for name in PEOPLE}
    for row in rows:
        if row["person"] not in assigned:
            raise ValueError(f"Unknown person in schedule: {row['person']}")
        assigned[row["person"]].append((dt.date.fromisoformat(row["date"]), row["shift"]))

    workers = []
    for p, name in enumerate(PEOPLE):
        entries = assigned[name]
        available_days = sum(
            day not in vacations.get(name, set()) and any(allowed(p, day, shift) for shift in SHIFTS)
            for day in days
        )
        night_opportunities = sum(
            day not in vacations.get(name, set()) and allowed(p, day, NIGHT)
            for day in days
        )
        weekend_opportunities = len({
            weekend_id(day) for day in days
            if day.weekday() >= 5 and day not in vacations.get(name, set())
            and any(allowed(p, day, shift) for shift in SHIFTS)
        })
        nights = sum(code == "N" for _, code in entries)
        worked_weekends = len({weekend_id(day) for day, _ in entries if day.weekday() >= 5})
        shifts = len(entries)
        workers.append({
            "person": name,
            "assigned_shifts": shifts,
            "assigned_hours": shifts * 8,
            "morning_shifts": sum(code == "M" for _, code in entries),
            "afternoon_shifts": sum(code in ("A", "EA") for _, code in entries),
            "night_shifts": nights,
            "weekends_worked": worked_weekends,
            "available_days": available_days,
            "night_opportunities": night_opportunities,
            "weekend_opportunities": weekend_opportunities,
            "workload_ratio": round(safe_ratio(shifts, available_days), 4),
            "night_burden": round(safe_ratio(nights, night_opportunities), 4),
            "weekend_burden": round(safe_ratio(worked_weekends, weekend_opportunities), 4),
        })

    night_eligible = [worker for worker in workers if worker["night_opportunities"]]
    weekend_eligible = [worker for worker in workers if worker["weekend_opportunities"]]
    metrics = {
        "assigned_hours": distribution(worker["assigned_hours"] for worker in workers),
        "availability_normalized_workload": distribution(worker["workload_ratio"] for worker in workers),
        "night_shifts_eligible_workers": distribution(worker["night_shifts"] for worker in night_eligible),
        "availability_normalized_night_burden": distribution(worker["night_burden"] for worker in night_eligible),
        "weekends_eligible_workers": distribution(worker["weekends_worked"] for worker in weekend_eligible),
        "availability_normalized_weekend_burden": distribution(worker["weekend_burden"] for worker in weekend_eligible),
    }
    return {
        "period": {"year": first.year, "month": first.month, "days": len(days)},
        "population": {
            "workers": len(workers),
            "night_eligible_workers": len(night_eligible),
            "weekend_eligible_workers": len(weekend_eligible),
        },
        "interpretation": {
            "gini": "0 is perfectly equal; larger values indicate greater inequality.",
            "jain_index": "1 is perfectly equal; smaller values indicate greater inequality.",
            "normalized_burdens": "Assignments divided by opportunities; comparisons exclude ineligible workers.",
        },
        "metrics": metrics,
        "workers": workers,
    }


def write_worker_csv(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workers = report["workers"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(workers[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(workers)


def render_fairness(report: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    workers = report["workers"]
    names = [worker["person"] for worker in workers]
    x = np.arange(len(names))
    fig, axes = plt.subplots(2, 2, figsize=(16, 10), constrained_layout=True)

    axes[0, 0].bar(x, [worker["assigned_hours"] for worker in workers], color="#4472C4")
    axes[0, 0].axhline(fmean(worker["assigned_hours"] for worker in workers), color="#C00000", linestyle="--", label="Mean")
    axes[0, 0].set_title("Assigned workload")
    axes[0, 0].set_ylabel("Hours")
    axes[0, 0].legend()

    axes[0, 1].bar(x, [worker["workload_ratio"] for worker in workers], color="#70AD47")
    axes[0, 1].set_title("Availability-normalized workload")
    axes[0, 1].set_ylabel("Assigned shifts / available days")

    width = 0.38
    axes[1, 0].bar(x - width / 2, [worker["night_shifts"] for worker in workers], width, label="Nights", color="#C55A11")
    axes[1, 0].bar(x + width / 2, [worker["weekends_worked"] for worker in workers], width, label="Weekends", color="#8064A2")
    axes[1, 0].set_title("Undesirable-shift burden")
    axes[1, 0].set_ylabel("Assignments")
    axes[1, 0].legend()

    metric_names = ["Hours", "Norm. workload", "Nights", "Weekends"]
    metric_keys = ["assigned_hours", "availability_normalized_workload", "night_shifts_eligible_workers", "weekends_eligible_workers"]
    jain = [report["metrics"][key]["jain_index"] for key in metric_keys]
    gini_values = [report["metrics"][key]["gini"] for key in metric_keys]
    metric_x = np.arange(len(metric_names))
    axes[1, 1].bar(metric_x - width / 2, jain, width, label="Jain (higher is fairer)", color="#5B9BD5")
    axes[1, 1].bar(metric_x + width / 2, gini_values, width, label="Gini (lower is fairer)", color="#ED7D31")
    axes[1, 1].set_ylim(0, 1.05)
    axes[1, 1].set_xticks(metric_x, metric_names)
    axes[1, 1].set_title("Fairness indices")
    axes[1, 1].legend(fontsize=8)

    for axis in (axes[0, 0], axes[0, 1], axes[1, 0]):
        axis.set_xticks(x, names, rotation=65, ha="right", fontsize=8)
        axis.grid(axis="y", alpha=0.25)
    axes[1, 1].grid(axis="y", alpha=0.25)
    fig.suptitle(f"Radiology Technician Schedule Fairness — {report['period']['year']}-{report['period']['month']:02d}", fontsize=16)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate fairness in a radiology technician schedule")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--json", type=Path, default=Path("reports/fairness_report.json"))
    parser.add_argument("--csv", type=Path, default=Path("reports/fairness_workers.csv"))
    parser.add_argument("--chart", type=Path, default=Path("reports/fairness_dashboard.png"))
    args = parser.parse_args()
    report = evaluate(read_schedule(args.schedule), load_vacations(args.config, args.department))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_worker_csv(report, args.csv)
    render_fairness(report, args.chart)
    print(json.dumps({"report": str(args.json), "workers": str(args.csv), "chart": str(args.chart), "metrics": report["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
