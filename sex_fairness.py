#!/usr/bin/env python3
"""Descriptive sex-based fairness audit for generated schedules.

This module evaluates schedule outcomes only. It does not use sex in the
scheduler, its constraints, or its objective.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, pstdev
from typing import Iterable

from evaluate import linked_schedule_unavailability, read_schedule
from solver import NIGHT, PEOPLE, SHIFTS, allowed, load_department, load_holiday_config, month_days


def safe_divide(numerator: float, denominator: float) -> float | None:
    """Return None for an undefined ratio instead of a misleading infinity."""
    return numerator / denominator if denominator else None


def finite_mean(values: Iterable[float | None]) -> float | None:
    data = [float(value) for value in values if value is not None]
    return fmean(data) if data else None


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def load_attributes(path: Path) -> dict[str, dict[str, str | None]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Attributes must be a JSON object keyed by worker name")
    return raw


def worker_metrics(
    rows: list[dict[str, str]],
    unavailable: dict[str, set[dt.date]],
    attributes: dict[str, dict[str, str | None]],
    holiday_dates: set[dt.date],
    burden_weights: dict[str, float],
) -> list[dict]:
    if not rows:
        return []
    first = min(dt.date.fromisoformat(row["date"]) for row in rows).replace(day=1)
    days = month_days(first)
    assigned: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        assigned[row["person"]].append(row)

    result = []
    for person_index, name in enumerate(PEOPLE):
        person_rows = assigned[name]
        total = len(person_rows)
        nights = sum(row["shift"] == "N" for row in person_rows)
        weekends = sum(dt.date.fromisoformat(row["date"]).weekday() >= 5 for row in person_rows)
        holidays = sum(dt.date.fromisoformat(row["date"]) in holiday_dates for row in person_rows)
        available = [day for day in days if day not in unavailable.get(name, set())]
        night_opportunities = sum(allowed(person_index, day, NIGHT) for day in available)
        weekend_opportunities = sum(
            day.weekday() >= 5 and any(allowed(person_index, day, shift) for shift in SHIFTS)
            for day in available
        )
        holiday_opportunities = sum(
            day in holiday_dates and any(allowed(person_index, day, shift) for shift in SHIFTS)
            for day in available
        )
        rates = {
            "night": safe_divide(nights, total),
            "weekend": safe_divide(weekends, total),
            "holiday": safe_divide(holidays, total),
        }
        applicable = [(burden_weights[key], rates[key]) for key in burden_weights
                      if rates.get(key) is not None and (key != "holiday" or holiday_dates)]
        burden = safe_divide(sum(weight * rate for weight, rate in applicable),
                             sum(weight for weight, _ in applicable))
        result.append({
            "person": name,
            "sex": attributes.get(name, {}).get("sex"),
            "total_shifts": total,
            "night_shifts": nights,
            "night_shift_rate": rounded(rates["night"]),
            "weekend_shifts": weekends,
            "weekend_shift_rate": rounded(rates["weekend"]),
            "holiday_shifts": holidays,
            "holiday_shift_rate": rounded(rates["holiday"]) if holiday_dates else None,
            "worker_specific_constraint_satisfaction": None,
            "eligible_night_opportunities": night_opportunities,
            "eligible_weekend_opportunities": weekend_opportunities,
            "eligible_holiday_opportunities": holiday_opportunities,
            "eligible_night_assignment_rate": rounded(safe_divide(nights, night_opportunities)),
            "eligible_weekend_assignment_rate": rounded(safe_divide(weekends, weekend_opportunities)),
            "eligible_holiday_assignment_rate": rounded(safe_divide(holidays, holiday_opportunities)),
            "overall_burden_score": rounded(burden),
        })
    return result


def group_summary(workers: list[dict], minimum_group_size: int = 3) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for worker in workers:
        label = worker.get("sex") or "missing"
        groups[str(label)].append(worker)

    fields = {
        "night_shift_rate": ("night_shifts", "total_shifts"),
        "weekend_shift_rate": ("weekend_shifts", "total_shifts"),
        "holiday_shift_rate": ("holiday_shifts", "total_shifts"),
        "worker_specific_constraint_satisfaction": (None, None),
        "eligible_night_assignment_rate": ("night_shifts", "eligible_night_opportunities"),
        "eligible_weekend_assignment_rate": ("weekend_shifts", "eligible_weekend_opportunities"),
        "eligible_holiday_assignment_rate": ("holiday_shifts", "eligible_holiday_opportunities"),
        "overall_burden_score": (None, None),
    }
    group_results = {}
    for label, members in sorted(groups.items()):
        metrics = {}
        for metric, (count_field, denominator_field) in fields.items():
            values = [worker[metric] for worker in members if worker.get(metric) is not None]
            aggregate_rate = None
            if count_field:
                denominator = sum(worker[denominator_field] for worker in members)
                aggregate_rate = safe_divide(sum(worker[count_field] for worker in members), denominator)
            metrics[metric] = {
                "workers_with_data": len(values),
                "mean_worker_rate": rounded(finite_mean(values)),
                "standard_deviation": rounded(pstdev(values)) if values else None,
                "total_assignments": sum(worker[count_field] for worker in members) if count_field else None,
                "total_opportunities": sum(worker[denominator_field] for worker in members) if denominator_field else None,
                "pooled_rate": rounded(aggregate_rate),
            }
        group_results[label] = {
            "workers": len(members),
            "workers_with_assigned_shifts": sum(worker["total_shifts"] > 0 for worker in members),
            "total_shifts": sum(worker["total_shifts"] for worker in members),
            "small_group_warning": len(members) < minimum_group_size,
            "metrics": metrics,
        }

    comparisons = {}
    if "F" in group_results and "M" in group_results:
        for metric in fields:
            female = group_results["F"]["metrics"][metric]["mean_worker_rate"]
            male = group_results["M"]["metrics"][metric]["mean_worker_rate"]
            difference = None if female is None or male is None else female - male
            comparisons[metric] = {
                "female": female,
                "male": male,
                "signed_difference_f_minus_m": rounded(difference),
                "absolute_gap": rounded(abs(difference)) if difference is not None else None,
                "burden_ratio_f_over_m": rounded(safe_divide(female, male)) if female is not None and male is not None else None,
                "greater_burden_group": None if difference is None or difference == 0 else ("F" if difference > 0 else "M"),
            }
    return {"groups": group_results, "female_male_comparisons": comparisons}


def evaluate_schedule(
    schedule: Path,
    config: Path,
    attributes_path: Path,
    department: str = "imagiologia",
    holidays: Path | None = None,
    burden_weights: dict[str, float] | None = None,
    hemodinamica_schedule: Path | None = None,
) -> dict:
    rows = read_schedule(schedule)
    unavailable = load_department(config, department)
    for name, days in linked_schedule_unavailability(hemodinamica_schedule).items():
        unavailable.setdefault(name, set()).update(days)
    attributes = load_attributes(attributes_path)
    first_date = min(dt.date.fromisoformat(row["date"]) for row in rows)
    holiday_map, _, _ = load_holiday_config(holidays, first_date.year)
    schedule_month = {day for day in month_days(first_date.replace(day=1))}
    monthly_holidays = set(holiday_map).intersection(schedule_month)
    weights = burden_weights or {"night": 1.0, "weekend": 1.0, "holiday": 1.0}
    workers = worker_metrics(rows, unavailable, attributes, monthly_holidays, weights)
    return {
        "schedule": str(schedule),
        "sensitive_attribute": "sex",
        "eligibility_definition": (
            "Static opportunity: calendar day is not blocked by vacation, consultation, sick leave, or a linked Hemodynamics assignment, "
            "and solver.allowed() permits the relevant shift. Dynamic rest, spacing, prior assignments, "
            "and global staffing interactions are not reconstructed as counterfactual eligibility."
        ),
        "worker_specific_constraint_satisfaction": {
            "available": False,
            "reason": "Worker-specific availability and eligibility are encoded as hard constraints; this outcome audit does not define a separate satisfaction score for them."
        },
        "overall_burden": {
            "weights": weights,
            "definition": "Weighted mean of available per-worker night, weekend, and holiday shift rates; default weights are neutral and configurable."
        },
        "workers": workers,
        "sex_summary": group_summary(workers),
    }


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def summary_rows(report: dict) -> list[dict]:
    rows = []
    groups = report["sex_summary"]["groups"]
    for metric, values in report["sex_summary"]["female_male_comparisons"].items():
        female_metric = groups["F"]["metrics"][metric]
        male_metric = groups["M"]["metrics"][metric]
        rows.append({
            "metric": metric,
            "female_workers": groups["F"]["workers"],
            "male_workers": groups["M"]["workers"],
            "female_total_shifts": groups["F"]["total_shifts"],
            "male_total_shifts": groups["M"]["total_shifts"],
            "female_undesirable_assignments": female_metric["total_assignments"],
            "male_undesirable_assignments": male_metric["total_assignments"],
            **values,
        })
    return rows


def comparison_rows(reports: dict[str, dict]) -> list[dict]:
    rows = []
    baseline = reports.get("operational_baseline")
    for variant, report in reports.items():
        for metric, current in report["sex_summary"]["female_male_comparisons"].items():
            base = (baseline or report)["sex_summary"]["female_male_comparisons"].get(metric, {})
            current_gap, baseline_gap = current.get("absolute_gap"), base.get("absolute_gap")
            rows.append({
                "variant": variant,
                "metric": metric,
                "signed_gap_f_minus_m": current.get("signed_difference_f_minus_m"),
                "absolute_gap": current_gap,
                "burden_ratio_f_over_m": current.get("burden_ratio_f_over_m"),
                "absolute_gap_change_from_baseline": rounded(current_gap - baseline_gap)
                    if current_gap is not None and baseline_gap is not None else None,
            })
    return rows


def write_markdown(report: dict, comparisons: list[dict], path: Path) -> None:
    groups = report["sex_summary"]["groups"]
    lines = [
        "# Sex-based schedule fairness audit", "",
        "This is a descriptive outcome audit. It does not classify the schedule as fair, discriminatory, or legally compliant.", "",
        "## Scope and definitions", "",
        f"Sensitive attribute: explicit `sex` labels. Groups found: {', '.join(groups)}.", "",
        report["eligibility_definition"], "",
        "Raw undesirable-shift rates divide each worker's assignments by that worker's total shifts. Group values are means of worker rates. "
        "The burden ratio is female rate divided by male rate; 1 denotes parity. Undefined ratios are reported as blank/null.", "",
        report["overall_burden"]["definition"], "",
        "Worker-specific constraint satisfaction is not reported as a separate score: "
        + report["worker_specific_constraint_satisfaction"]["reason"], "",
        "## Current schedule results", "",
        "| Metric | Female | Male | Difference F-M | Absolute gap | Ratio F/M | Greater burden |", "|---|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in summary_rows(report):
        display = lambda value: "—" if value is None else f"{value:.4f}"
        lines.append(f"| {row['metric'].replace('_', ' ')} | {display(row['female'])} | {display(row['male'])} | "
                     f"{display(row['signed_difference_f_minus_m'])} | {display(row['absolute_gap'])} | {display(row['burden_ratio_f_over_m'])} | "
                     f"{row['greater_burden_group'] or '—'} |")
    lines += ["", "Group sample sizes (roster / workers with assigned shifts): " + ", ".join(
        f"{key}={value['workers']} / {value['workers_with_assigned_shifts']}" for key, value in groups.items()
    ) + ". Workers with zero assigned shifts are excluded from shift-rate means.", ""]
    if comparisons:
        lines += ["## Schedule variants", "", "Closer to zero is parity for gaps; closer to one is parity for ratios.", "",
                  "| Variant | Metric | Signed gap F-M | Absolute gap | Ratio F/M | Gap change from baseline |",
                  "|---|---|---:|---:|---:|---:|"]
        for row in comparisons:
            display = lambda value: "—" if value is None else f"{value:.4f}"
            lines.append(f"| {row['variant']} | {row['metric'].replace('_', ' ')} | {display(row['signed_gap_f_minus_m'])} | "
                         f"{display(row['absolute_gap'])} | {display(row['burden_ratio_f_over_m'])} | {display(row['absolute_gap_change_from_baseline'])} |")
    lines += ["", "## Limitations", "", "Results cover one month, are descriptive, and include no significance test. "
              "Static eligibility does not reconstruct schedule-dependent rest and spacing constraints. Holiday metrics are unavailable when no holiday dates are configured. "
              "Longitudinal data and worker feedback would be required for stronger conclusions.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def render_plot(report: dict, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    metrics = ["night_shift_rate", "weekend_shift_rate", "eligible_night_assignment_rate", "eligible_weekend_assignment_rate"]
    comparison = report["sex_summary"]["female_male_comparisons"]
    metrics = [metric for metric in metrics if metric in comparison]
    x = range(len(metrics)); width = .36
    fig, axis = plt.subplots(figsize=(10, 6))
    axis.bar([value - width / 2 for value in x], [comparison[m]["female"] or 0 for m in metrics], width, label="F")
    axis.bar([value + width / 2 for value in x], [comparison[m]["male"] or 0 for m in metrics], width, label="M")
    axis.set_xticks(list(x), [m.replace("_", "\n") for m in metrics])
    axis.set_ylabel("Mean worker rate")
    axis.set_title("Sex-based schedule burden rates")
    axis.grid(axis="y", alpha=.25); axis.legend(); fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight"); plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit sex-based fairness of generated schedules")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--attributes", type=Path, required=True)
    parser.add_argument("--department", default="imagiologia")
    parser.add_argument("--holidays", type=Path)
    parser.add_argument("--variants", type=Path, help="Directory containing profile/schedule.csv variants")
    parser.add_argument("--hemodinamica-schedule", type=Path,
                        help="Linked schedule whose Lina assignments are unavailable Imaging days")
    parser.add_argument("--output", type=Path, default=Path("reports/fairness"))
    parser.add_argument("--night-weight", type=float, default=1.0)
    parser.add_argument("--weekend-weight", type=float, default=1.0)
    parser.add_argument("--holiday-weight", type=float, default=1.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    weights = {"night": args.night_weight, "weekend": args.weekend_weight, "holiday": args.holiday_weight}
    report = evaluate_schedule(
        args.schedule, args.config, args.attributes, args.department, args.holidays, weights,
        args.hemodinamica_schedule,
    )
    reports = {}
    if args.variants:
        for schedule in sorted(args.variants.glob("*/schedule.csv")):
            if schedule.parent.name == "balanced":
                continue
            reports[schedule.parent.name] = evaluate_schedule(
                schedule, args.config, args.attributes, args.department, args.holidays, weights,
                args.hemodinamica_schedule,
            )
        reports["published_schedule"] = report
    comparisons = comparison_rows(reports)
    write_csv(report["workers"], args.output / "worker_fairness_metrics.csv")
    write_csv(summary_rows(report), args.output / "sex_fairness_summary.csv")
    if comparisons:
        write_csv(comparisons, args.output / "schedule_variant_comparison.csv")
    (args.output / "sex_fairness_summary.json").write_text(json.dumps({"current": report, "variants": reports}, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, comparisons, args.output / "sex_fairness_report.md")
    render_plot(report, args.output / "sex_fairness_rates.png")
    print(json.dumps({"output": str(args.output), "groups": list(report["sex_summary"]["groups"]), "variants": list(reports)}, indent=2))


if __name__ == "__main__":
    main()
