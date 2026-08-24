#!/usr/bin/env python3
"""Generate transparent rule-based and counterfactual schedule explanations."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path

from evaluate import evaluate, load_vacations, read_schedule
from robustness import REPAIR_WEIGHTS, assignment_set, compare_schedules, solver_reference
from solver import (
    AFTERNOON, MORNING, NIGHT, PEOPLE, SHIFT_NAMES, SHIFTS, Rules,
    allowed, calendar_week_id, minimum_required, month_days, solve_and_export,
)


CODE_TO_SHIFT = {"M": MORNING, "A": AFTERNOON, "EA": AFTERNOON, "N": NIGHT}


def schedule_indexes(rows: list[dict[str, str]]) -> tuple[dict, dict, Counter]:
    by_person_day = {}
    by_person = defaultdict(list)
    staffing = Counter()
    for row in rows:
        day = dt.date.fromisoformat(row["date"])
        code = row["shift"]
        by_person_day[row["person"], day] = code
        by_person[row["person"]].append((day, code))
        staffing[day, CODE_TO_SHIFT[code]] += 1
    return by_person_day, by_person, staffing


def assignment_explanations(
    rows: list[dict[str, str]],
    vacations: dict[str, set[dt.date]],
    rest_hours: dict[str, int],
    rules: Rules = Rules(),
) -> list[dict]:
    by_person_day, by_person, staffing = schedule_indexes(rows)
    first = min(dt.date.fromisoformat(row["date"]) for row in rows).replace(day=1)
    records = []
    for row in sorted(rows, key=lambda item: (item["date"], item["person"])):
        person, code = row["person"], row["shift"]
        day, shift = dt.date.fromisoformat(row["date"]), CODE_TO_SHIFT[code]
        entries = by_person[person]
        weekly_hours = 8 * sum(calendar_week_id(date) == calendar_week_id(day) for date, _ in entries)
        monthly_hours = 8 * len(entries)
        required, actual = minimum_required(day, shift), staffing[day, shift]
        facts = [
            f"{SHIFT_NAMES[shift]} coverage was {actual} for a minimum requirement of {required}.",
            f"{person} was eligible for this shift and was not unavailable on {day}.",
            f"The assignment leaves {person} at {weekly_hours}/{rules.weekly_hours} hours in ISO week {calendar_week_id(day)[1]}.",
            f"Monthly assigned hours are {monthly_hours}/{max(0, (35 * len(month_days(first)) // 7 // 8) * 8 - rest_hours.get(person, 0))} allowed hours under the default 35 h contract assumption.",
        ]
        binding = []
        if code == "EA":
            binding.append("exactly_one_weekday_early_afternoon")
            facts.append("This is the weekday's required single 14:00–22:00 assignment.")
        if code == "N":
            binding.extend(["night_coverage", "night_recovery", "night_spacing", "night_fairness_band"])
            other_nights = sorted(date for date, other_code in entries if other_code == "N" and date != day)
            if other_nights:
                nearest = min(abs((other - day).days) for other in other_nights)
                facts.append(f"The nearest other night assigned to this worker is {nearest} days away.")
        records.append({
            "date": row["date"], "person": person, "shift": code,
            "explanation_type": "factual_rule_trace",
            "facts": facts,
            "relevant_constraints": ["staffing_minimum", "availability", "weekly_hours", "monthly_hours", *binding],
            "optimization_note": (
                "The assignment belongs to the globally optimized solution. These facts verify legality and "
                "coverage, but do not constitute a unique causal proof that this worker was the only possible choice."
            ),
        })
    return records


def non_assignment_explanations(
    rows: list[dict[str, str]],
    vacations: dict[str, set[dt.date]],
    rules: Rules = Rules(),
) -> list[dict]:
    by_person_day, by_person, _ = schedule_indexes(rows)
    first = min(dt.date.fromisoformat(row["date"]) for row in rows).replace(day=1)
    records = []
    for p, person in enumerate(PEOPLE):
        entries = by_person[person]
        for day in month_days(first):
            assigned = by_person_day.get((person, day))
            for code, shift in CODE_TO_SHIFT.items():
                if assigned == code or (assigned == "EA" and code == "A"):
                    continue
                reasons = []
                if day in vacations.get(person, set()):
                    reasons.append({"constraint": "availability", "reason": "Worker is on vacation or unavailable."})
                if not allowed(p, day, shift):
                    reasons.append({"constraint": "shift_eligibility", "reason": "Worker profile does not permit this shift."})
                if assigned:
                    reasons.append({"constraint": "one_shift_per_day", "reason": f"Worker is already assigned to {assigned}."})
                yesterday = by_person_day.get((person, day - dt.timedelta(days=1)))
                if yesterday == "N":
                    reasons.append({"constraint": "night_recovery", "reason": "Worker has a night shift on the previous day."})
                if shift == MORNING and yesterday == "A":
                    reasons.append({"constraint": "afternoon_rest", "reason": "A normal afternoon shift precedes this morning."})
                weekly_hours = 8 * sum(calendar_week_id(date) == calendar_week_id(day) for date, _ in entries)
                if weekly_hours >= rules.weekly_hours and not assigned:
                    reasons.append({"constraint": "weekly_hours", "reason": "The published schedule already reaches the weekly hour cap."})
                if not reasons:
                    reasons.append({
                        "constraint": "global_optimization",
                        "reason": "No direct hard exclusion is visible; another feasible assignment was selected by the global objective.",
                    })
                records.append({"date": day.isoformat(), "person": person, "shift": code, "reasons": reasons})
    return records


def capacity_diagnostics(
    year: int,
    month: int,
    vacations: dict[str, set[dt.date]],
) -> list[dict]:
    diagnostics = []
    for day in month_days(dt.date(year, month, 1)):
        for shift in SHIFTS:
            eligible = [
                person for p, person in enumerate(PEOPLE)
                if day not in vacations.get(person, set()) and allowed(p, day, shift)
            ]
            required = minimum_required(day, shift)
            diagnostics.append({
                "date": day.isoformat(), "shift": SHIFT_NAMES[shift], "required": required,
                "eligible_available": len(eligible), "capacity_margin": len(eligible) - required,
                "status": "capacity_conflict" if len(eligible) < required else "capacity_available",
            })
    return diagnostics


def counterfactual(
    rows: list[dict[str, str]],
    rest: Path,
    config_path: Path,
    department: str,
    output: Path,
    time_limit: float,
    seed: int,
) -> dict:
    target = next(
        row for row in sorted(rows, key=lambda item: (item["date"], item["person"]))
        if row["shift"] == "N" and dt.date.fromisoformat(row["date"]).day >= 10
    )
    day = dt.date.fromisoformat(target["date"])
    first = min(dt.date.fromisoformat(row["date"]) for row in rows)
    baseline_vacations = load_vacations(config_path, department)
    baseline_fairness = evaluate(rows, baseline_vacations)["metrics"]
    directory = output / "counterfactuals" / f"without_{target['person'].replace(' ', '_')}_{day}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        solver_report = solve_and_export(
            first.year, first.month, rest, config_path, department,
            output=directory / "schedule.csv", time_limit=time_limit,
            weights=REPAIR_WEIGHTS, random_seed=seed,
            reference_assignments=solver_reference(rows),
            forbidden_assignments={(target["person"], day, NIGHT)},
        )
        repaired = read_schedule(directory / "schedule.csv")
        repaired_fairness = evaluate(repaired, baseline_vacations)["metrics"]
        changes = compare_schedules(rows, repaired)
        removed = sorted(assignment_set(rows) - assignment_set(repaired), key=str)
        added = sorted(assignment_set(repaired) - assignment_set(rows), key=str)
        result = {
            "question": f"What changes if {target['person']} cannot work night on {day}?",
            "target": target,
            "feasible": True,
            "solver_status": solver_report["status"],
            **changes,
            "removed_assignments": [(name, date.isoformat(), code) for name, date, code in removed],
            "added_assignments": [(name, date.isoformat(), code) for name, date, code in added],
            "fairness_changes": {
                key: round(repaired_fairness[key]["gini"] - baseline_fairness[key]["gini"], 4)
                for key in ("availability_normalized_workload", "availability_normalized_night_burden", "availability_normalized_weekend_burden")
            },
            "explanation": (
                f"Forbidding the assignment remains feasible and requires {changes['assignment_edits']} exact assignment edits "
                f"affecting {changes['affected_workers']} workers. The listed changes are solver-verified counterfactual consequences."
            ),
        }
    except RuntimeError as error:
        result = {
            "question": f"What changes if {target['person']} cannot work night on {day}?",
            "target": target, "feasible": False, "error": str(error),
            "explanation": "No feasible counterfactual schedule was found under the current hard constraints and time limit.",
        }
    (directory / "explanation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def repair_explanations(baseline: list[dict[str, str]], robustness_dir: Path) -> list[dict]:
    explanations = []
    scenarios_dir = robustness_dir / "scenarios"
    if not scenarios_dir.exists():
        return explanations
    before = assignment_set(baseline)
    for directory in sorted(path for path in scenarios_dir.iterdir() if path.is_dir()):
        schedule_path, metadata_path = directory / "schedule.csv", directory / "scenario.json"
        if not schedule_path.exists() or not metadata_path.exists():
            continue
        repaired = read_schedule(schedule_path)
        after = assignment_set(repaired)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        changes = compare_schedules(baseline, repaired)
        explanations.append({
            "scenario": metadata["name"], "trigger": metadata["description"], **changes,
            "removed": [(n, d.isoformat(), s) for n, d, s in sorted(before - after, key=str)],
            "added": [(n, d.isoformat(), s) for n, d, s in sorted(after - before, key=str)],
            "explanation": (
                f"The repair responds to: {metadata['description']} It preserves all hard constraints and changes "
                f"{changes['assignment_edits']} exact assignments across {changes['affected_workers']} workers."
            ),
        })
    return explanations


def write_markdown(assignments: list[dict], repairs: list[dict], counterfactual_result: dict, path: Path) -> None:
    lines = [
        "# Schedule explanation report", "",
        "Explanations separate verified rule facts from optimization inference. A legal assignment is not necessarily the only possible assignment.", "",
        "## Assignment explanations", "",
    ]
    for item in assignments:
        lines.extend([
            f"### {item['date']} — {item['person']} — {item['shift']}", "",
            *[f"- {fact}" for fact in item["facts"]], "",
            f"_Transparency note: {item['optimization_note']}_", "",
        ])
    lines.extend(["## Repair explanations", ""])
    for repair in repairs:
        lines.extend([f"### {repair['scenario']}", "", repair["explanation"], "", "Removed:", ""])
        lines.extend(f"- {name}, {date}, {shift}" for name, date, shift in repair["removed"])
        lines.extend(["", "Added:", ""])
        lines.extend(f"- {name}, {date}, {shift}" for name, date, shift in repair["added"])
        lines.append("")
    lines.extend(["## Counterfactual", "", counterfactual_result["question"], "", counterfactual_result["explanation"], ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def load_rest(path: Path) -> dict[str, int]:
    return {name: int(value) for name, value in json.loads(path.read_text(encoding="utf-8")).items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Explain a generated radiology technician schedule")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--rest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--robustness", type=Path, default=Path("robustness"))
    parser.add_argument("--output", type=Path, default=Path("explanations"))
    parser.add_argument("--time-limit", type=float, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = read_schedule(args.schedule)
    vacations = load_vacations(args.config, args.department)
    rest = load_rest(args.rest)
    assignments = assignment_explanations(rows, vacations, rest)
    non_assignments = non_assignment_explanations(rows, vacations)
    diagnostics = capacity_diagnostics(
        min(dt.date.fromisoformat(row["date"]) for row in rows).year,
        min(dt.date.fromisoformat(row["date"]) for row in rows).month,
        vacations,
    )
    repairs = repair_explanations(rows, args.robustness)
    counterfactual_result = counterfactual(
        rows, args.rest, args.config, args.department, args.output, args.time_limit, args.seed
    )
    audit = {
        "methodology": {
            "assignment": "Rule trace over the published schedule.",
            "non_assignment": "Direct hard exclusions where observable; otherwise labeled global optimization.",
            "counterfactual": "Re-solve while forbidding the selected assignment and minimizing schedule changes.",
            "infeasibility": "Static per-shift capacity screening; not a complete CP-SAT unsatisfiable core.",
        },
        "assignment_explanations": assignments,
        "non_assignment_explanations": non_assignments,
        "capacity_diagnostics": diagnostics,
        "repair_explanations": repairs,
        "counterfactual": counterfactual_result,
    }
    (args.output / "schedule_explanations.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    write_markdown(assignments, repairs, counterfactual_result, args.output / "schedule_explanations.md")
    print(json.dumps({
        "assignments_explained": len(assignments),
        "non_assignments_explained": len(non_assignments),
        "repair_scenarios_explained": len(repairs),
        "capacity_conflicts": sum(item["status"] == "capacity_conflict" for item in diagnostics),
        "counterfactual": counterfactual_result,
    }, indent=2))


if __name__ == "__main__":
    main()
