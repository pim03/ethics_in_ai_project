#!/usr/bin/env python3
"""Generate report visualizations from existing September artifacts."""
from __future__ import annotations

import calendar
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "report_visualizations"
BASE = ROOT / "reports" / "2026-09"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save(fig: plt.Figure, name: str) -> None:
    fig.savefig(OUT / name, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def tradeoff() -> None:
    points = []
    for report_path in sorted((ROOT / "experiments" / "2026-09").glob("*/solver_report.json")):
        if report_path.parent.name == "balanced":
            continue
        report = load_json(report_path)
        gap = sum(x["short_by"] for x in report["morning_understaffed_days"])
        overtime = sum(x["extra_hours"] for x in report["monthly_overtime_used"])
        points.append((gap, overtime, report_path.parent.name.replace("_", " ").title(), "profile"))
    report = load_json(BASE / "solver_report.json")
    points.append((sum(x["short_by"] for x in report["morning_understaffed_days"]),
                   sum(x["extra_hours"] for x in report["monthly_overtime_used"]),
                   "Published schedule", "published"))
    diagnostic = load_json(ROOT / "reports" / "explanations" / "explanation_audit.json")["system_shortfall_analysis"]
    points.append((diagnostic["minimum_total_gap"], diagnostic["overtime_hours_in_minimum_gap_schedule"],
                   "Minimum-gap diagnostic", "diagnostic"))

    fig, ax = plt.subplots(figsize=(10, 7))
    styles = {"profile": ("#4C78A8", 70), "published": ("#E45756", 150), "diagnostic": ("#54A24B", 150)}
    label_offsets = {
        "Published schedule": (7, 18), "Preference Focused": (7, 5),
        "Balanced": (7, -10), "Workload Focused": (7, 18),
        "Weekend Focused": (7, 5),
    }
    offsets = defaultdict(int)
    for gap, overtime, label, kind in points:
        color, size = styles[kind]
        ax.scatter(gap, overtime, s=size, color=color, edgecolor="white", linewidth=1.2, zorder=3)
        key = (gap, overtime)
        offset = offsets[key] * 13
        offsets[key] += 1
        ax.annotate(label, (gap, overtime), xytext=label_offsets.get(label, (7, 6 + offset)),
                    textcoords="offset points", fontsize=8)
    ax.set_xlabel("Total gap from preferred weekday-morning target (worker-slots; lower is better)")
    ax.set_ylabel("Monthly overtime used (hours; lower is better)")
    ax.set_title("Observed staffing–overtime outcomes", pad=34)
    ax.text(0.5, 1.015, "Existing objective-profile solutions plus the proven minimum-gap diagnostic — not a Pareto frontier",
            transform=ax.transAxes, ha="center", va="bottom", fontsize=9, color="#555555")
    ax.grid(alpha=.25)
    ax.legend(handles=[Patch(color=v[0], label=k.title()) for k, v in styles.items()], loc="best")
    save(fig, "01_staffing_overtime_tradeoff.png")


def objective_contributions() -> None:
    report = load_json(BASE / "solver_report.json")
    values = [(name.replace("_", " ").title(), value) for name, value in report["weighted_objective_components"].items() if value]
    values.sort(key=lambda item: item[1])
    total = sum(value for _, value in values)
    fig, ax = plt.subplots(figsize=(11, 8))
    labels, amounts = zip(*values)
    colors = ["#E45756" if "Shortfall" in label else "#4C78A8" for label in labels]
    bars = ax.barh(labels, amounts, color=colors)
    ax.set_xscale("log")
    ax.set_xlabel("Weighted contribution to objective (log scale)")
    ax.set_title("What determines the published objective score?")
    for bar, amount in zip(bars, amounts):
        ax.text(amount * 1.08, bar.get_y() + bar.get_height()/2,
                f"{amount:,}  ({100*amount/total:.1f}%)", va="center", fontsize=8)
    ax.grid(axis="x", alpha=.25)
    ax.text(0, -0.12, "Zero-valued components are omitted. Bar length uses a log scale so small contributions remain visible.",
            transform=ax.transAxes, fontsize=9, color="#555555")
    save(fig, "02_objective_contributions.png")


def availability_matrix() -> None:
    config = load_json(ROOT / "config" / "2026-09.json")["imagiologia"]
    workers = config["workers"]
    assignments = {(row["person"], int(row["date"][-2:])): row["shift"] for row in read_csv(BASE / "schedule.csv")}
    hemo = {(row["person"], int(row["date"][-2:])) for row in read_csv(BASE / "hemodinamica_schedule.csv")}
    unavailable = {}
    for category, key in [("Vacation", "vacations"), ("Consultation", "consultas"), ("Sick leave", "sick_leave")]:
        for worker, dates in config.get(key, {}).items():
            for date in dates:
                unavailable[(worker, int(date[-2:]))] = category
    categories = ["Available, not assigned", "Assigned Imaging", "Hemo duty", "Vacation", "Consultation", "Sick leave"]
    codes = {name: i for i, name in enumerate(categories)}
    matrix = np.zeros((len(workers), 30), dtype=int)
    annotations = [["" for _ in range(30)] for _ in workers]
    for i, worker in enumerate(workers):
        for day in range(1, 31):
            key = (worker, day)
            category = unavailable.get(key, "Available, not assigned")
            if key in hemo:
                category = "Hemo duty"
            if key in assignments:
                category = "Assigned Imaging"
                annotations[i][day - 1] = assignments[key]
            matrix[i, day - 1] = codes[category]
    cmap = ListedColormap(["#F1F1F1", "#4C78A8", "#72B7B2", "#F2CF5B", "#B279A2", "#E45756"])
    fig, ax = plt.subplots(figsize=(16, 9))
    ax.imshow(matrix, aspect="auto", cmap=cmap, norm=BoundaryNorm(np.arange(-.5, len(categories)+.5), len(categories)))
    for i in range(len(workers)):
        for j in range(30):
            if annotations[i][j]:
                ax.text(j, i, annotations[i][j], ha="center", va="center", fontsize=6, color="white", fontweight="bold")
    ax.set_xticks(np.arange(30), np.arange(1, 31))
    ax.set_yticks(np.arange(len(workers)), workers)
    ax.set_xlabel("September day")
    ax.set_title("Imaging assignments and declared availability constraints")
    ax.legend(handles=[Patch(color=cmap(i), label=name) for i, name in enumerate(categories)],
              ncol=3, loc="upper center", bbox_to_anchor=(.5, -0.08))
    ax.set_xticks(np.arange(-.5, 30, 1), minor=True)
    ax.set_yticks(np.arange(-.5, len(workers), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=.5)
    save(fig, "03_availability_assignment_matrix.png")


def shortfall_calendar() -> None:
    report = load_json(BASE / "solver_report.json")
    gaps = {int(row["date"][-2:]): row["short_by"] for row in report["morning_understaffed_days"]}
    cal = calendar.Calendar(firstweekday=0).monthdayscalendar(2026, 9)
    data = np.full((len(cal), 7), np.nan)
    labels = [["" for _ in range(7)] for _ in cal]
    for week_i, week in enumerate(cal):
        for dow, day in enumerate(week):
            if day:
                value = gaps.get(day, 0) if dow < 5 else np.nan
                data[week_i, dow] = value
                labels[week_i][dow] = f"{day}\n" + (f"gap {value}" if value else "target met" if dow < 5 else "weekend")
    fig, ax = plt.subplots(figsize=(12, 5.5))
    cmap = ListedColormap(["#D9EAD3", "#FFF2CC", "#FCE5CD", "#F8CBAD", "#E45756"])
    ax.imshow(data, cmap=cmap, vmin=0, vmax=4, aspect="auto")
    for i in range(len(cal)):
        for j in range(7):
            if labels[i][j]:
                ax.text(j, i, labels[i][j], ha="center", va="center", fontsize=9)
    ax.set_xticks(range(7), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    ax.set_yticks([])
    ax.set_title("Where the published roster falls below the preferred morning target of 10")
    ax.text(.5, -.10, "Green weekdays meet the preferred target. Coloured gaps remain above the mandatory minimum of six.",
            transform=ax.transAxes, ha="center", fontsize=9, color="#555555")
    save(fig, "04_shortfall_calendar.png")


def counterfactual_changes() -> None:
    cf = load_json(ROOT / "reports" / "explanations" / "explanation_audit.json")["counterfactual"]
    target = cf["target"]
    removed = {(p, int(d[-2:])): s for p, d, s in cf["removed_assignments"]}
    added = {(p, int(d[-2:])): s for p, d, s in cf["added_assignments"]}
    workers = sorted({p for p, _ in removed} | {p for p, _ in added})
    days = sorted({d for _, d in removed} | {d for _, d in added})
    fig, axes = plt.subplots(2, 1, figsize=(11, 4.8), sharex=True, constrained_layout=True)
    for ax, records, title, color in [(axes[0], removed, "Removed from published schedule", "#E45756"),
                                      (axes[1], added, "Added in counterfactual schedule", "#54A24B")]:
        for yi, worker in enumerate(workers):
            for day in days:
                shift = records.get((worker, day))
                if shift:
                    ax.scatter(day, yi, s=500, marker="s", color=color)
                    ax.text(day, yi, shift, ha="center", va="center", color="white", fontweight="bold")
        ax.set_yticks(range(len(workers)), workers)
        ax.set_title(title, loc="left", fontsize=11)
        ax.grid(axis="x", alpha=.2)
    axes[1].set_xticks(days, [f"Sep {day}" for day in days])
    target_date = target["date"]
    day = int(target_date[-2:])
    score_change = cf["objective_comparison"]["score_change"]
    fig.suptitle(
        f"Counterfactual: {target['person']} cannot work the night of {day} September",
        fontsize=14,
    )
    fig.text(.5, -.04,
             f"{cf['assignment_edits']} assignment edits across {cf['affected_workers']} workers; "
             f"published-weight objective {score_change:+,}",
             ha="center", fontsize=10)
    save(fig, "05_counterfactual_changes.png")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tradeoff()
    objective_contributions()
    availability_matrix()
    shortfall_calendar()
    counterfactual_changes()
    print(OUT)


if __name__ == "__main__":
    main()
