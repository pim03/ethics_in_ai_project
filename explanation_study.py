#!/usr/bin/env python3
"""Generate and compare the project's constraint-system explanation techniques."""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from explanations import (
    assignment_explanations, capacity_diagnostics, counterfactual,
    load_rest, non_assignment_explanations, repair_explanations, shortfall_explanations,
    system_shortfall_analysis,
)
from evaluate import load_vacations, read_schedule


def timed(callable_, *args):
    started = time.perf_counter()
    result = callable_(*args)
    return result, time.perf_counter() - started


def direct_non_assignment(record: dict) -> bool:
    return any(reason["constraint"] != "global_optimization" for reason in record["reasons"])


def comparison_rows(assignments, assignment_seconds, non_assignments, non_assignment_seconds,
                    diagnostics, diagnostic_seconds, repairs, repair_seconds, counterfactual_result,
                    counterfactual_seconds) -> list[dict]:
    direct = sum(direct_non_assignment(record) for record in non_assignments)
    conflicts = sum(record["status"] == "capacity_conflict" for record in diagnostics)
    counterfactual_ok = bool(counterfactual_result.get("feasible"))
    return [
        {
            "technique": "factual_rule_trace",
            "questions_evaluated": len(assignments),
            "specific_answers": len(assignments),
            "specific_answer_rate": 1.0 if assignments else None,
            "runtime_seconds": round(assignment_seconds, 4),
            "verification_basis": "Facts recomputed from the published schedule and declared rules",
            "main_strength": "Fast, complete coverage of assigned shifts",
            "main_limitation": "Legality and relevance are not a unique causal explanation",
        },
        {
            "technique": "contrastive_why_not",
            "questions_evaluated": len(non_assignments),
            "specific_answers": direct,
            "specific_answer_rate": round(direct / len(non_assignments), 4) if non_assignments else None,
            "runtime_seconds": round(non_assignment_seconds, 4),
            "verification_basis": "Direct hard exclusions observable in the published schedule",
            "main_strength": "Identifies actionable availability, eligibility, rest, and hour exclusions",
            "main_limitation": "Falls back to global optimization when no direct exclusion is visible",
        },
        {
            "technique": "capacity_diagnostic",
            "questions_evaluated": len(diagnostics),
            "specific_answers": len(diagnostics),
            "specific_answer_rate": 1.0 if diagnostics else None,
            "runtime_seconds": round(diagnostic_seconds, 4),
            "verification_basis": "Static availability and shift-eligibility counts versus required coverage",
            "main_strength": f"Makes staffing pressure visible; {conflicts} static conflicts detected",
            "main_limitation": "Not a complete dynamic feasibility proof or unsatisfiable core",
        },
        {
            "technique": "repair_explanation",
            "questions_evaluated": len(repairs),
            "specific_answers": len(repairs),
            "specific_answer_rate": 1.0 if repairs else None,
            "runtime_seconds": round(repair_seconds, 4),
            "verification_basis": "Exact difference between baseline and solver-generated repaired schedules",
            "main_strength": "Connects an operational trigger to concrete schedule changes",
            "main_limitation": "Explains the repair outcome, not every optimization trade-off within it",
        },
        {
            "technique": "counterfactual_resolve",
            "questions_evaluated": 1,
            "specific_answers": int(counterfactual_ok),
            "specific_answer_rate": float(counterfactual_ok),
            "runtime_seconds": round(counterfactual_seconds, 4),
            "verification_basis": "CP-SAT re-solve after forbidding the selected assignment",
            "main_strength": "Strongest evidence about feasibility and downstream consequences",
            "main_limitation": "Slower and conditional on model, weights, seed, and time limit",
        },
    ]


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def write_report(comparison: list[dict], counterfactual_result: dict, assignments: list[dict],
                 non_assignments: list[dict], repairs: list[dict], shortfalls: list[dict],
                 system_shortfall: dict, path: Path) -> None:
    labels = {
        "factual_rule_trace": "Explain a scheduled shift",
        "contrastive_why_not": "Explain why a worker was not given a shift",
        "capacity_diagnostic": "Check whether enough workers were available",
        "repair_explanation": "Explain changes after a disruption",
        "counterfactual_resolve": "Test what happens when one assignment is forbidden",
    }
    lines = [
        "# Explainability: what the system does and what we learned", "",
        "## Main Conclusions", "",
        "The system gives evidence that a person could legally work a shift, "
        "shows direct reasons why another person could not work it, and reruns the schedule to test alternatives.", "",
        "The explanations help a human inspect and challenge a schedule, but they do not prove that every assignment was the only or best possible choice.", "",
        "## Tests made", "",
        "| test | cases checked | cases with a direct answer | percentage | time (seconds) |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in comparison:
        rate = "—" if row["specific_answer_rate"] is None else f"{100 * row['specific_answer_rate']:.2f}%"
        lines.append(f"| {labels[row['technique']]} | {row['questions_evaluated']} | {row['specific_answers']} | {rate} | {row['runtime_seconds']:.4f} |")
    lines += [
        "## The five checks", "",
        "1. **Scheduled shifts:** For each assignment, we show coverage, eligibility, availability, hours, and relevant rest rules. It means that the assignment is generally valid according to the defined constraints.", "",
        "2. **Why not this worker?:** We look for a direct reason, such as vacation, lack of eligibility, another shift on the same day, a rest rule, or the weekly hour limit. In some cases there is no single blocking rule; the worker was simply not selected by the overall optimization.", "",
        "3. **Available staff:** We compare the number of available and eligible workers with the minimum staffing requirement. This is a basic warning check.", "",
        "4. **Schedule repairs:** After each simulated disruption, we list exactly which assignments were removed and added.", "",
        "5. **What-if test:** We forbid one assignment, run the solver again, and measure how much of the schedule must change.", "",
    ]
    if assignments:
        example = assignments[0]
        lines += ["## Example: explaining a scheduled shift", "",
                  f"The schedule gives {example['person']} shift {example['shift']} on {example['date']}.", "",
                  "The report checks the staffing level, whether the worker was allowed and available, their hours, and any relevant rest rules.", "",
                  "Meaning: the assignment follows the rules represented in the program. It does not prove that this worker was the only possible choice.", ""]
    # Prefer a temporary, schedule-dependent exclusion over an obvious permanent
    # eligibility restriction, because it better demonstrates the explanation.
    direct_example = next((
        item for item in non_assignments
        if item["shift"] == "M"
        and any(reason["constraint"] == "night_recovery" for reason in item["reasons"])
        and all(reason["constraint"] != "shift_eligibility" for reason in item["reasons"])
    ), None)
    if direct_example is None:
        direct_example = next((item for item in non_assignments if direct_non_assignment(item)), None)
    if direct_example:
        lines += ["## Example: why another worker was not selected", "",
                  f"Why not {direct_example['person']} on {direct_example['date']} shift {direct_example['shift']}?", ""]
        lines += [f"- {reason['reason']}" for reason in direct_example["reasons"]]
        lines.append("")
    lines += ["## What-if result", "", counterfactual_result["question"], ""]
    if counterfactual_result.get("feasible"):
        lines += ["The solver found another valid schedule.", "",
                  f"Changing this one assignment led to {counterfactual_result['assignment_edits']} assignment changes "
                  f"involving {counterfactual_result['affected_workers']} workers.", "",
                  "Therefore, the original assignment was not strictly necessary, but replacing it had consequences for several people.", ""]
        objective = counterfactual_result.get("objective_comparison")
        if objective:
            lines += [f"Using the published objective weights, the score changed from {objective['baseline_score']} to "
                      f"{objective['alternative_score']} ({objective['score_change']:+d}). Lower is better, so the "
                      f"alternative {objective['interpretation']} the objective score.", ""]
            lines += ["The changed objective components were:", ""]
            for name, change in objective["component_changes"].items():
                label = name.replace("_", " ")
                lines += [f"- {label}: {change['raw_change']:+d} units, contributing {change['weighted_change']:+d} points", ""]
    else:
        lines += ["The solver did not find another valid schedule within the time allowed. This does not prove that no alternative exists.", ""]
    lines += ["## Why were some mornings below the preferred target?", "",
              "Six morning workers is the mandatory minimum. Ten is the preferred target. Therefore, the following gaps are not violations of the mandatory rule.", ""]
    lines += [system_shortfall["conclusion"], ""]
    for item in shortfalls:
        lines += [f"### {item['date']} ({item['weekday']})", "",
                  f"The schedule has {item['scheduled']} morning workers: {item['gap_to_preferred_target']} below the preferred target of 10.", "",
                  item["conclusion"], ""]
    lines += ["## Conclusions", "",
              "- We can show whether scheduled shifts follow the rules represented in the program.", "",
              "- For most rejected alternatives, we can show a direct blocking reason.", "",
              "- We can show the exact changes caused by disruptions and by the tested what-if question.", "",
              "- We cannot claim that an assignment was the only possible choice or explain every trade-off made by the solver.", "",
              "- We have not yet tested these explanations with hospital schedulers or workers, so we cannot claim that users find them clear or useful.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare schedule explanation techniques")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--rest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--department", default="imagiologia")
    parser.add_argument("--robustness", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/explanations"))
    parser.add_argument("--time-limit", type=float, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--contracts", type=Path)
    parser.add_argument("--workday-history", type=Path)
    parser.add_argument("--holidays", type=Path)
    parser.add_argument("--hemodinamica-schedule", type=Path)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    rows = read_schedule(args.schedule)
    vacations = load_vacations(args.config, args.department)
    rest = load_rest(args.rest)
    assignments, t_assignment = timed(assignment_explanations, rows, vacations, rest)
    non_assignments, t_non_assignment = timed(non_assignment_explanations, rows, vacations)
    dates = [item["date"] for item in rows]
    year, month = int(min(dates)[:4]), int(min(dates)[5:7])
    diagnostics, t_diagnostic = timed(capacity_diagnostics, year, month, vacations)
    repairs, t_repair = timed(repair_explanations, rows, args.robustness)
    shortfalls, _ = timed(
        shortfall_explanations, rows, vacations, args.rest, args.config, args.department,
        args.output, args.time_limit, args.seed, 10, args.contracts, args.workday_history,
        args.holidays, args.hemodinamica_schedule
    )
    system_shortfall, _ = timed(
        system_shortfall_analysis, rows, args.rest, args.config, args.department,
        args.output, args.time_limit, args.seed, args.contracts, args.workday_history,
        args.holidays, args.hemodinamica_schedule
    )
    counterfactual_result, t_counterfactual = timed(
        counterfactual, rows, args.rest, args.config, args.department, args.output, args.time_limit, args.seed,
        args.baseline_report or args.schedule.parent / "solver_report.json", args.contracts,
        args.workday_history, args.holidays, args.hemodinamica_schedule
    )
    comparison = comparison_rows(assignments, t_assignment, non_assignments, t_non_assignment,
                                 diagnostics, t_diagnostic, repairs, t_repair,
                                 counterfactual_result, t_counterfactual)
    audit = {
        "comparison": comparison,
        "assignment_explanations": assignments,
        "non_assignment_explanations": non_assignments,
        "capacity_diagnostics": diagnostics,
        "shortfall_explanations": shortfalls,
        "system_shortfall_analysis": system_shortfall,
        "repair_explanations": repairs,
        "counterfactual": counterfactual_result,
    }
    (args.output / "explanation_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    write_csv(comparison, args.output / "technique_comparison.csv")
    write_report(comparison, counterfactual_result, assignments, non_assignments, repairs, shortfalls,
                 system_shortfall,
                 args.output / "explanation_techniques_report.md")
    print(json.dumps({"output": str(args.output), "techniques": len(comparison),
                      "assignments": len(assignments), "non_assignments": len(non_assignments),
                      "repairs": len(repairs), "counterfactual_feasible": counterfactual_result.get("feasible")}, indent=2))


if __name__ == "__main__":
    main()
