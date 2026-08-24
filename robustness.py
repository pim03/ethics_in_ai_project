#!/usr/bin/env python3
"""Evaluate schedule resilience under operational perturbations."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from evaluate import evaluate, load_vacations, read_schedule
from solver import AFTERNOON, MORNING, NIGHT, Weights, solve_and_export


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    unavailable: tuple[tuple[str, dt.date], ...] = ()
    demand: tuple[tuple[dt.date, int, int], ...] = ()


REPAIR_WEIGHTS = Weights(
    workday_balance=100_000,
    workload_imbalance=1000,
    excess_work_streak=500,
    isolated_work_day=300,
    isolated_rest_day=200,
    holiday_rotation=1000,
    special_holiday_rotation=2000,
    weekend_imbalance=30,
    consecutive_weekends=20,
    split_weekend=10,
    vacation_adjacent_weekend=5,
    schedule_changes=100_000,
)


def assignment_set(rows: list[dict[str, str]]) -> set[tuple[str, dt.date, str]]:
    return {(row["person"], dt.date.fromisoformat(row["date"]), row["shift"]) for row in rows}


def solver_reference(rows: list[dict[str, str]]) -> set[tuple[str, dt.date, str]]:
    return {(row["person"], dt.date.fromisoformat(row["date"]), row["shift"]) for row in rows}


def scenario_catalog(rows: list[dict[str, str]]) -> list[Scenario]:
    """Build deterministic, data-backed perturbations from the baseline roster."""
    ordered = sorted(rows, key=lambda row: (row["date"], row["person"]))
    middle = sorted({dt.date.fromisoformat(row["date"]) for row in rows})[len({row["date"] for row in rows}) // 2]
    middle_rows = [row for row in ordered if dt.date.fromisoformat(row["date"]) == middle]
    single = middle_rows[0]
    night = next(row for row in ordered if row["shift"] == "N" and dt.date.fromisoformat(row["date"]).day >= 10)
    counts = Counter(row["person"] for row in rows)
    three_day_person = counts.most_common(1)[0][0]
    demand_day = middle + dt.timedelta(days=4)
    simultaneous = middle_rows[:2]
    return [
        Scenario(
            "single_day_absence",
            f"{single['person']} becomes unavailable on {middle}.",
            ((single["person"], middle),),
        ),
        Scenario(
            "night_worker_absence",
            f"Night worker {night['person']} becomes unavailable on {night['date']}.",
            ((night["person"], dt.date.fromisoformat(night["date"])),),
        ),
        Scenario(
            "three_day_absence",
            f"{three_day_person} becomes unavailable for three consecutive days.",
            tuple((three_day_person, middle + dt.timedelta(days=offset)) for offset in (-1, 0, 1)),
        ),
        Scenario(
            "morning_demand_increase",
            f"Morning demand increases by one on {demand_day}.",
            demand=((demand_day, MORNING, 1),),
        ),
        Scenario(
            "night_demand_increase",
            f"Night demand increases by one on {demand_day}.",
            demand=((demand_day, NIGHT, 1),),
        ),
        Scenario(
            "simultaneous_absences",
            f"Two scheduled workers become unavailable on {middle}.",
            tuple((row["person"], middle) for row in simultaneous),
        ),
    ]


def fairness_snapshot(report: dict) -> dict[str, float]:
    metrics = report["metrics"]
    return {
        "workload_gini": metrics["availability_normalized_workload"]["gini"],
        "night_gini": metrics["availability_normalized_night_burden"]["gini"],
        "weekend_gini": metrics["availability_normalized_weekend_burden"]["gini"],
    }


def compare_schedules(baseline: list[dict[str, str]], repaired: list[dict[str, str]]) -> dict[str, int]:
    before, after = assignment_set(baseline), assignment_set(repaired)
    difference = before.symmetric_difference(after)
    return {
        "assignment_edits": len(difference),
        "replacement_equivalents": (len(difference) + 1) // 2,
        "affected_workers": len({item[0] for item in difference}),
    }


def write_vacations(path: Path, vacations: dict[str, set[dt.date]], scenario: Scenario) -> dict[str, set[dt.date]]:
    adjusted = {name: set(days) for name, days in vacations.items()}
    for name, day in scenario.unavailable:
        adjusted.setdefault(name, set()).add(day)
    path.write_text(
        json.dumps({name: sorted(day.isoformat() for day in days) for name, days in adjusted.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return adjusted


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def render_charts(rows: list[dict], output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    labels = [row["scenario"].replace("_", "\n") for row in rows]
    x = np.arange(len(rows))
    feasible = [row["feasible"] for row in rows]

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), constrained_layout=True)
    axes[0].bar(x, [row.get("replacement_equivalents") or 0 for row in rows], color="#4472C4")
    axes[0].set_title("Schedule disruption after repair")
    axes[0].set_ylabel("Replacement-equivalent changes")
    axes[1].bar(x, [row.get("affected_workers") or 0 for row in rows], color="#ED7D31")
    axes[1].set_title("Workers affected by repair")
    axes[1].set_ylabel("Workers")
    for axis in axes:
        axis.set_xticks(x, labels, fontsize=8)
        axis.grid(axis="y", alpha=0.25)
        for index, ok in enumerate(feasible):
            if not ok:
                axis.text(index, 0, "INFEASIBLE", rotation=90, ha="center", va="bottom", color="red")
    fig.suptitle("Robustness and schedule stability")
    fig.savefig(output / "stability_chart.png", dpi=180, bbox_inches="tight")
    plt.close(fig)

    width = 0.25
    fig, axis = plt.subplots(figsize=(13, 7))
    axis.bar(x - width, [row.get("workload_gini_delta") or 0 for row in rows], width, label="Workload Gini Δ")
    axis.bar(x, [row.get("night_gini_delta") or 0 for row in rows], width, label="Night Gini Δ")
    axis.bar(x + width, [row.get("weekend_gini_delta") or 0 for row in rows], width, label="Weekend Gini Δ")
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_xticks(x, labels, fontsize=8)
    axis.set_ylabel("Change from baseline (negative is fairer)")
    axis.set_title("Fairness impact of operational perturbations")
    axis.legend()
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output / "fairness_impact.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def run_robustness(
    baseline_path: Path,
    rest_path: Path,
    config_path: Path,
    department: str,
    output: Path,
    time_limit: float,
    seed: int,
    contract_hours: Path | None = None,
    workday_history: Path | None = None,
    holidays: Path | None = None,
    forbidden_assignments: set[tuple[str, dt.date, int]] | None = None,
    extra_rest_hours: dict[str, int] | None = None,
) -> list[dict]:
    baseline_rows = read_schedule(baseline_path)
    baseline_dates = [dt.date.fromisoformat(row["date"]) for row in baseline_rows]
    year, month = min(baseline_dates).year, min(baseline_dates).month
    baseline_vacations = load_vacations(config_path, department)
    baseline_fairness = fairness_snapshot(evaluate(baseline_rows, baseline_vacations))
    reference = solver_reference(baseline_rows)
    output.mkdir(parents=True, exist_ok=True)
    results = []

    for scenario in scenario_catalog(baseline_rows):
        directory = output / "scenarios" / scenario.name
        directory.mkdir(parents=True, exist_ok=True)
        scenario_vac_path = directory / "vacations.json"
        adjusted_vacations = write_vacations(scenario_vac_path, baseline_vacations, scenario)
        demand = {(day, shift): increase for day, shift, increase in scenario.demand}
        metadata = {
            "name": scenario.name,
            "description": scenario.description,
            "unavailable": [(name, day.isoformat()) for name, day in scenario.unavailable],
            "demand": [(day.isoformat(), shift, value) for day, shift, value in scenario.demand],
        }
        (directory / "scenario.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        row = {"scenario": scenario.name, "description": scenario.description}
        try:
            solver_report = solve_and_export(
                year, month, rest_path, config_path, department,
                output=directory / "schedule.csv", time_limit=time_limit,
                weights=REPAIR_WEIGHTS, random_seed=seed,
                demand_adjustments=demand, reference_assignments=reference,
                vac_override_json=scenario_vac_path,
                contract_hours_json=contract_hours,
                workday_history_json=workday_history,
                holiday_json=holidays,
                forbidden_assignments=forbidden_assignments,
                extra_rest_hours=extra_rest_hours,
            )
            repaired = read_schedule(directory / "schedule.csv")
            repaired_report = evaluate(repaired, adjusted_vacations)
            repaired_fairness = fairness_snapshot(repaired_report)
            changes = compare_schedules(baseline_rows, repaired)
            row.update({
                "feasible": 1,
                "solve_seconds": solver_report["solve_seconds"],
                "status": solver_report["status"],
                "staffing_shortfalls": sum(item["short_by"] for item in solver_report["morning_understaffed_days"]),
                "objective": solver_report["objective"],
                **changes,
                **{name: value for name, value in repaired_fairness.items()},
                **{f"{name}_delta": round(value - baseline_fairness[name], 4) for name, value in repaired_fairness.items()},
            })
            (directory / "solver_report.json").write_text(json.dumps(solver_report, indent=2) + "\n", encoding="utf-8")
            (directory / "fairness_report.json").write_text(json.dumps(repaired_report, indent=2) + "\n", encoding="utf-8")
        except RuntimeError as error:
            row.update({
                "feasible": 0, "solve_seconds": time_limit, "status": "INFEASIBLE_OR_UNKNOWN",
                "staffing_shortfalls": "unknown", "assignment_edits": "", "replacement_equivalents": "",
                "affected_workers": "", "error": str(error),
            })
        results.append(row)

    # Stable columns despite infeasible scenarios.
    columns = [
        "scenario", "description", "feasible", "status", "solve_seconds", "staffing_shortfalls",
        "assignment_edits", "replacement_equivalents", "affected_workers",
        "workload_gini", "workload_gini_delta", "night_gini", "night_gini_delta",
        "weekend_gini", "weekend_gini_delta", "objective", "error",
    ]
    normalized = [{column: row.get(column, "") for column in columns} for row in results]
    write_csv(normalized, output / "scenarios.csv")
    summary = {
        "scenario_count": len(results),
        "feasible_count": sum(row["feasible"] for row in results),
        "feasibility_rate": sum(row["feasible"] for row in results) / len(results),
        "baseline_fairness": baseline_fairness,
        "repair_weights": asdict(REPAIR_WEIGHTS),
        "scenarios": normalized,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    render_charts(normalized, output)
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Run schedule robustness experiments")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--rest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--output", type=Path, default=Path("robustness"))
    parser.add_argument("--time-limit", type=float, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--contracts", type=Path)
    parser.add_argument("--workday-history", type=Path)
    parser.add_argument("--holidays", type=Path)
    args = parser.parse_args()
    results = run_robustness(
        args.baseline, args.rest, args.config, args.department, args.output, args.time_limit, args.seed,
        contract_hours=args.contracts, workday_history=args.workday_history, holidays=args.holidays,
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
