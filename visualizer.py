#!/usr/bin/env python3
"""Render a schedule CSV as a saved timetable grid."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch, Rectangle

from solver import load_department


SHIFT_TO_IDX = {"M": 0, "A": 1, "EA": 1, "N": 2}
VACATION_IDX = 3
CONFLICT_IDX = 4
COLORS = ["#8BC34A", "#FFEB3B", "#E57373", "#64B5F6", "#212121"]
WEEKDAYS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def render(
    csv_path: Path,
    output: Path = Path("reports/timetable.png"),
    vacations: dict[str, set[dt.date]] | None = None,
    prevention_path: Path | None = None,
    compensation_path: Path | None = None,
    shortfall_days: dict[dt.date, int] | None = None,
    extra_schedule_path: Path | None = None,
) -> Path:
    frame = pd.read_csv(csv_path, parse_dates=["date"])
    if frame.empty:
        raise ValueError("CSV is empty")
    if extra_schedule_path is not None and extra_schedule_path.exists():
        extra = pd.read_csv(extra_schedule_path, parse_dates=["date"])
        frame = pd.concat([frame, extra], ignore_index=True)
    unknown = set(frame["shift"]) - set(SHIFT_TO_IDX)
    if unknown:
        raise ValueError(f"Unknown shift codes: {', '.join(sorted(unknown))}")

    persons = list(frame["person"].drop_duplicates())
    special = ["Nuno", "Angelo"]
    persons = [p for p in persons if p not in special] + [
        p for p in special if p in persons
    ]
    person_index = {person: index for index, person in enumerate(persons)}
    days = pd.date_range(frame["date"].min(), frame["date"].max(), freq="D")
    grid = np.full((len(persons), len(days)), np.nan)
    labels = np.full((len(persons), len(days)), "", dtype=object)
    # Two departments solved independently can double-book a shared worker
    # (same person, same day, two different shifts) — surface that as a
    # conflict cell instead of silently keeping whichever row came last.
    cell_shifts: dict[tuple[int, int], list[str]] = {}
    for _, row in frame.iterrows():
        r = person_index[row["person"]]
        c = (row["date"] - days[0]).days
        cell_shifts.setdefault((r, c), []).append(row["shift"])
    conflict_cells: set[tuple[int, int]] = set()
    for (r, c), shifts in cell_shifts.items():
        if len(shifts) > 1:
            grid[r, c] = CONFLICT_IDX
            labels[r, c] = "/".join(shifts)
            conflict_cells.add((r, c))
        else:
            grid[r, c] = SHIFT_TO_IDX[shifts[0]]
            labels[r, c] = shifts[0]

    if vacations is not None:
        first_day, last_day = days[0].date(), days[-1].date()
        for person, dates in vacations.items():
            if person not in person_index:
                continue
            for vacation_day in dates:
                if first_day <= vacation_day <= last_day:
                    r = person_index[person]
                    c = (vacation_day - first_day).days
                    grid[r, c] = VACATION_IDX
                    labels[r, c] = "F"

    prevention_cells: set[tuple[int, int]] = set()
    if prevention_path is not None and prevention_path.exists():
        prevention = pd.read_csv(prevention_path, parse_dates=["date"])
        for _, row in prevention.iterrows():
            if row["person"] not in person_index or row["date"] not in days:
                continue
            r = person_index[row["person"]]
            c = (row["date"] - days[0]).days
            labels[r, c] = f"{labels[r, c]}/P" if labels[r, c] else "P"
            prevention_cells.add((r, c))

    if compensation_path is not None and compensation_path.exists():
        compensation = pd.read_csv(compensation_path, parse_dates=["rest_date"])
        for _, row in compensation.iterrows():
            if row["person"] not in person_index or row["rest_date"] not in days:
                continue
            r = person_index[row["person"]]
            c = (row["rest_date"] - days[0]).days
            if not labels[r, c]:
                labels[r, c] = "FC"

    shortfall_cols: dict[int, int] = {}
    if shortfall_days:
        first_day, last_day = days[0].date(), days[-1].date()
        for day, amount in shortfall_days.items():
            if amount > 0 and first_day <= day <= last_day:
                shortfall_cols[(day - first_day).days] = amount

    cmap = ListedColormap(COLORS)
    cmap.set_bad("white")
    fig, axis = plt.subplots(figsize=(0.48 * len(days) + 4, 0.48 * len(persons) + 2.5))
    for c in shortfall_cols:
        axis.axvspan(c - 0.5, c + 0.5, color="#C00000", alpha=0.16, zorder=0.5)
    axis.imshow(np.ma.masked_invalid(grid), cmap=cmap, vmin=-0.5, vmax=4.5, aspect="auto")
    axis.set_xticks(np.arange(-0.5, len(days), 1), minor=True)
    axis.set_yticks(np.arange(-0.5, len(persons), 1), minor=True)
    axis.grid(which="minor", color="#BFBFBF", linewidth=0.6)
    axis.tick_params(which="minor", bottom=False, left=False)
    axis.set_xticks(
        range(len(days)),
        [
            f"{day.day:02d}\n{WEEKDAYS_PT[day.weekday()]}"
            + (f"\n-{shortfall_cols[c]}" if c in shortfall_cols else "")
            for c, day in enumerate(days)
        ],
        fontsize=8,
    )
    for c, tick in zip(range(len(days)), axis.get_xticklabels()):
        if c in shortfall_cols:
            tick.set_color("#C00000")
            tick.set_fontweight("bold")
    axis.set_yticks(range(len(persons)), persons)
    axis.set_xlabel(f"{days[0].strftime('%B %Y')}")
    axis.set_title("Escala Mensal dos Técnicos de Radiologia")
    for r in range(len(persons)):
        for c in range(len(days)):
            if labels[r, c]:
                text_color = "white" if (r, c) in conflict_cells else "black"
                fontsize = 6 if (r, c) in conflict_cells else 7
                axis.text(c, r, labels[r, c], ha="center", va="center", fontsize=fontsize,
                          fontweight="bold", color=text_color)
    for r, c in prevention_cells:
        axis.add_patch(Rectangle(
            (c - 0.47, r - 0.47), 0.94, 0.94,
            fill=False, edgecolor="#7B1FA2", linewidth=2.0,
        ))
    for r, c in conflict_cells:
        axis.add_patch(Rectangle(
            (c - 0.5, r - 0.5), 1.0, 1.0,
            fill=False, edgecolor="#C00000", linewidth=2.5,
        ))
    handles = [
        Patch(color=COLORS[0], label="M — Manhã 08–16"),
        Patch(color=COLORS[1], label="A — Tarde 16–00"),
        Patch(color=COLORS[1], label="EA — Tarde antecipada 14–22"),
        Patch(color=COLORS[2], label="N — Noite 00–08"),
        Patch(color=COLORS[VACATION_IDX], label="F — Férias"),
        Patch(facecolor="white", edgecolor="#BFBFBF", label="Folga"),
        Patch(facecolor="white", edgecolor="#7B1FA2", linewidth=2, label="P — Prevenção"),
        Patch(facecolor="white", edgecolor="#BFBFBF", label="FC — Folga compensatória"),
    ]
    if shortfall_cols:
        handles.append(Patch(facecolor="#C00000", alpha=0.3, edgecolor="#C00000", label="Manhã com falta de pessoal"))
    if conflict_cells:
        handles.append(Patch(facecolor=COLORS[CONFLICT_IDX], edgecolor="#C00000", linewidth=2, label="Conflito: escalado em ambos os serviços"))
    axis.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.10), ncol=8, frameon=False)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a radiology technician schedule grid")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--prevention", type=Path, default=Path("prevention.csv"))
    parser.add_argument("--compensation", type=Path, default=Path("compensatory_rest.csv"))
    parser.add_argument("--report", type=Path, help="solver_report.json to flag understaffed morning shifts")
    parser.add_argument("--extra-schedule", type=Path, help="Another department's schedule.csv to merge into the same grid")
    parser.add_argument("--extra-department", default="hemodinamica", help="Department key inside --config for --extra-schedule's roster/vacations")
    parser.add_argument("--output", type=Path, default=Path("reports/timetable.png"))
    args = parser.parse_args()
    vacations = load_department(args.config, args.department)
    if args.extra_schedule is not None:
        vacations = {**vacations, **load_department(args.config, args.extra_department)}
    shortfall_days = None
    if args.report is not None:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        shortfall_days = {
            dt.date.fromisoformat(row["date"]): row["short_by"]
            for row in report.get("morning_understaffed_days", [])
        }
    print(render(
        args.schedule, args.output, vacations, args.prevention, args.compensation,
        shortfall_days, args.extra_schedule,
    ))


if __name__ == "__main__":
    main()
