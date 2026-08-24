#!/usr/bin/env python3
"""Run and compare fairness/preference objective configurations."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

from evaluate import evaluate, load_vacations, read_schedule, write_worker_csv
from solver import Weights, solve_and_export


PROFILES: dict[str, Weights] = {
    # Staffing and legal rules remain hard in every profile.
    "operational_baseline": Weights(
        workday_balance=1,
        workload_imbalance=1,
        excess_work_streak=0,
        isolated_work_day=0,
        isolated_rest_day=0,
        holiday_rotation=0,
        special_holiday_rotation=0,
        weekend_imbalance=0,
        consecutive_weekends=0,
        split_weekend=0,
        vacation_adjacent_weekend=0,
        schedule_changes=0,
    ),
    "workload_focused": Weights(
        workday_balance=100_000,
        workload_imbalance=1000,
        excess_work_streak=10,
        isolated_work_day=10,
        isolated_rest_day=10,
        holiday_rotation=100,
        special_holiday_rotation=200,
        weekend_imbalance=1,
        consecutive_weekends=1,
        split_weekend=1,
        vacation_adjacent_weekend=1,
        schedule_changes=0,
    ),
    "weekend_focused": Weights(
        workday_balance=100_000,
        workload_imbalance=100,
        excess_work_streak=20,
        isolated_work_day=20,
        isolated_rest_day=20,
        holiday_rotation=100,
        special_holiday_rotation=200,
        weekend_imbalance=1000,
        consecutive_weekends=250,
        split_weekend=100,
        vacation_adjacent_weekend=50,
        schedule_changes=0,
    ),
    "preference_focused": Weights(
        workday_balance=100_000,
        workload_imbalance=100,
        excess_work_streak=1000,
        isolated_work_day=500,
        isolated_rest_day=500,
        holiday_rotation=1000,
        special_holiday_rotation=2000,
        weekend_imbalance=100,
        consecutive_weekends=200,
        split_weekend=200,
        vacation_adjacent_weekend=100,
        schedule_changes=0,
    ),
    "balanced": Weights(),
}


def preference_cost(components: dict[str, int]) -> int:
    """Unweighted count of preference violations, excluding scaled workload."""
    return sum(value for name, value in components.items() if name != "workload_imbalance")


def flatten_result(name: str, solver_report: dict, fairness_report: dict) -> dict:
    metrics = fairness_report["metrics"]
    components = solver_report["objective_components"]
    return {
        "profile": name,
        "status": solver_report["status"],
        "solve_seconds": solver_report["solve_seconds"],
        "objective": solver_report["objective"],
        "best_bound": solver_report["best_bound"],
        "workload_gini": metrics["assigned_hours"]["gini"],
        "normalized_workload_gini": metrics["availability_normalized_workload"]["gini"],
        "normalized_workload_jain": metrics["availability_normalized_workload"]["jain_index"],
        "night_gini": metrics["availability_normalized_night_burden"]["gini"],
        "weekend_gini": metrics["availability_normalized_weekend_burden"]["gini"],
        "weekend_jain": metrics["availability_normalized_weekend_burden"]["jain_index"],
        "preference_cost": preference_cost(components),
        **{f"penalty_{key}": value for key, value in components.items()},
    }


def write_comparison(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def render_comparison(rows: list[dict], output_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    labels = [row["profile"].replace("_", "\n") for row in rows]
    x = np.arange(len(rows))
    width = 0.24
    fig, axis = plt.subplots(figsize=(13, 7))
    axis.bar(x - width, [row["normalized_workload_gini"] for row in rows], width, label="Workload Gini")
    axis.bar(x, [row["night_gini"] for row in rows], width, label="Night Gini")
    axis.bar(x + width, [row["weekend_gini"] for row in rows], width, label="Weekend Gini")
    axis.set_xticks(x, labels)
    axis.set_ylabel("Gini coefficient (lower is fairer)")
    axis.set_title("Fairness comparison by objective profile")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "fairness_comparison.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(9, 7))
    grouped: dict[tuple[float, float], list[str]] = {}
    for row in rows:
        point = (row["weekend_gini"], row["preference_cost"])
        grouped.setdefault(point, []).append(row["profile"].replace("_", " "))
    for (weekend_gini, cost), names in grouped.items():
        axis.scatter(weekend_gini, cost, s=90)
        axis.annotate("\n".join(names), (weekend_gini, cost),
                      xytext=(7, 5), textcoords="offset points", fontsize=9)
    axis.set_xlabel("Availability-normalized weekend Gini (lower is fairer)")
    axis.set_ylabel("Unweighted soft-preference violations (lower is better)")
    axis.set_title("Weekend fairness vs. preference satisfaction")
    axis.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "tradeoff_plot.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def run_experiments(
    year: int,
    month: int,
    rest: Path,
    config_path: Path,
    department: str,
    output_dir: Path,
    time_limit: float,
    seed: int,
    contract_hours: Path | None = None,
    workday_history: Path | None = None,
    holidays: Path | None = None,
    forbidden_assignments: set[tuple[str, dt.date, int]] | None = None,
    extra_rest_hours: dict[str, int] | None = None,
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    vacations = load_vacations(config_path, department)
    comparison = []
    for name, weights in PROFILES.items():
        profile_dir = output_dir / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        schedule_path = profile_dir / "schedule.csv"
        solver_report = solve_and_export(
            year, month, rest, config_path, department, output=schedule_path,
            time_limit=time_limit, weights=weights, random_seed=seed,
            contract_hours_json=contract_hours,
            workday_history_json=workday_history,
            holiday_json=holidays,
            forbidden_assignments=forbidden_assignments,
            extra_rest_hours=extra_rest_hours,
        )
        fairness_report = evaluate(read_schedule(schedule_path), vacations)
        (profile_dir / "solver_report.json").write_text(json.dumps(solver_report, indent=2) + "\n", encoding="utf-8")
        (profile_dir / "fairness_report.json").write_text(json.dumps(fairness_report, indent=2) + "\n", encoding="utf-8")
        write_worker_csv(fairness_report, profile_dir / "fairness_workers.csv")
        comparison.append(flatten_result(name, solver_report, fairness_report))
    write_comparison(comparison, output_dir / "comparison.csv")
    (output_dir / "profiles.json").write_text(
        json.dumps({name: asdict(weights) for name, weights in PROFILES.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    render_comparison(comparison, output_dir)
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare scheduling objective profiles")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--rest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--output", type=Path, default=Path("experiments"))
    parser.add_argument("--time-limit", type=float, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--contracts", type=Path)
    parser.add_argument("--workday-history", type=Path)
    parser.add_argument("--holidays", type=Path)
    args = parser.parse_args()
    results = run_experiments(
        args.year, args.month, args.rest, args.config, args.department, args.output, args.time_limit, args.seed,
        contract_hours=args.contracts, workday_history=args.workday_history, holidays=args.holidays,
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
