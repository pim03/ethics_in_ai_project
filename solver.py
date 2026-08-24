#!/usr/bin/env python3
"""Constraint-aware monthly radiology technician shift scheduler.

Hard constraints guarantee legal/operational feasibility. Soft constraints are
named, weighted preferences whose individual costs are reported after solving.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ortools.sat.python import cp_model


PEOPLE: list[str] = []
"""Current department roster, populated at runtime by `load_department()`.

Mutated in place (never reassigned) so that `from solver import PEOPLE` in
other modules keeps pointing at the live roster.
"""
MORNING, AFTERNOON, NIGHT = range(3)
SHIFTS = (MORNING, AFTERNOON, NIGHT)
SHIFT_NAMES = {MORNING: "M", AFTERNOON: "A", NIGHT: "N"}
SHIFT_TIMES = {
    "M": (8, 16),
    "A": (16, 0),
    "EA": (14, 22),
    "N": (0, 8),
}
SHIFT_DUR = 8
WEEKDAY_MORNING_HARD_MINIMUM = 6


class Availability(dict[str, set[dt.date]]):
    """Merged whole-day absences, retaining true vacation dates for preferences."""

    def __init__(self, *args, vacation_days: Optional[dict[str, set[dt.date]]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vacation_days = vacation_days or {}


@dataclass(frozen=True)
class Rules:
    weekly_hours: int = 40
    min_days_between_nights: int = 6
    max_consecutive_work_days: int = 5
    max_consecutive_afternoons: int = 3


@dataclass(frozen=True)
class Weights:
    workday_balance: int = 100_000
    workload_imbalance: int = 100
    excess_work_streak: int = 500
    isolated_work_day: int = 300
    isolated_rest_day: int = 200
    excess_afternoon_streak: int = 400
    night_ramp_mismatch: int = 150
    night_after_vacation: int = 1_000
    holiday_rotation: int = 1_000
    special_holiday_rotation: int = 2_000
    weekend_imbalance: int = 30
    consecutive_weekends: int = 20
    split_weekend: int = 10
    vacation_adjacent_weekend: int = 5
    schedule_changes: int = 0
    morning_shortfall_key_day: int = 10_000_000
    morning_shortfall_other_day: int = 500
    monthly_overtime: int = 50


@dataclass
class ModelArtifacts:
    model: cp_model.CpModel
    days: list[dt.date]
    assignments: dict[tuple[int, int, int], cp_model.IntVar] = field(default_factory=dict)
    early: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    prevention: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    morning: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    morning_shortfalls: dict[int, cp_model.IntVar] = field(default_factory=dict)
    monthly_overtime: dict[int, cp_model.IntVar] = field(default_factory=dict)
    objective_terms: dict[str, list[cp_model.LinearExpr | cp_model.IntVar]] = field(default_factory=dict)
    constraint_catalog: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HemodinamicaWeights:
    non_thursday_single_coverage: int = 300


HARD_CONSTRAINTS = {
    "one_shift_per_day": "At most one shift per worker per calendar day.",
    "staffing_minimum": "Each shift meets its hard minimum staffing. Weekday mornings have a hard floor of six and a preferred target of ten (see morning_shortfall_key_day/other_day).",
    "availability": "Vacations and shift eligibility are respected.",
    "weekly_hours": "Assigned time does not exceed the weekly legal limit.",
    "monthly_hours": "Monthly hours respect the target cap and carried rest debt, plus an optional 24-32 h fair-shared overtime allowance (see monthly_overtime).",
    "afternoon_rest": "A normal afternoon cannot be followed by a morning.",
    "early_afternoon_night": "An early afternoon cannot be followed by a night shift.",
    "night_rest": "A night shift cannot be followed by work the next day.",
    "night_spacing": "Night shifts have the required spacing.",
    "night_fairness_band": "Eligible workers receive floor/ceiling shares of nights.",
}

HEMODINAMICA_HARD_CONSTRAINTS = {
    "prevention_alternation": "Angelo and Nuno alternate one daily on-call prevention duty.",
    "prevention_compensation": "Sunday or public-holiday prevention earns one compensatory rest day.",
    "morning_oncall_presence": "The on-call holder also works that day's weekday morning shift.",
    "morning_staffing": "Exactly two people work the weekday morning shift; Lina joins whenever only one of Angelo/Nuno is assigned.",
}

HEMODINAMICA_SOFT_PREFERENCES = {
    "non_thursday_single_coverage": "Prefer Thursday as the day only one of Angelo/Nuno works the morning shift, when single coverage happens at all.",
}

SOFT_PREFERENCES = {
    "workday_balance": "Equalise cumulative workdays and carry any difference to the following month.",
    "workload_imbalance": "Minimise the maximum deviation from proportional target hours.",
    "excess_work_streak": "Avoid more than five consecutive workdays.",
    "isolated_work_day": "Avoid a single workday between two rest days.",
    "isolated_rest_day": "Prefer rest days in groups of at least two.",
    "excess_afternoon_streak": "Avoid more than two consecutive afternoon shifts.",
    "night_ramp_mismatch": "Prefer a night shift to follow T/T14/M or T/T/D on the three preceding days.",
    "night_after_vacation": "Prefer not to assign a night shift on the first day after vacation.",
    "holiday_rotation": "Rotate ordinary public-holiday work using carried history.",
    "special_holiday_rotation": "Rotate Christmas, New Year and Easter using carried history.",
    "weekend_imbalance": "Reduce the range of weekend counts among eligible workers.",
    "consecutive_weekends": "Avoid assigning a worker on adjacent weekends.",
    "split_weekend": "Prefer working both weekend days instead of only one.",
    "vacation_adjacent_weekend": "Prefer not to work weekends touching a vacation period.",
    "schedule_changes": "Preserve existing assignments when repairing a published schedule.",
    "morning_shortfall_key_day": "Avoid falling below the preferred weekday-morning target of ten on Monday/Wednesday/Friday; the hard floor of six remains mandatory.",
    "morning_shortfall_other_day": "Avoid falling below the preferred weekday-morning target of ten on Tuesday/Thursday; the hard floor of six remains mandatory.",
    "monthly_overtime": "Avoid monthly overtime; when used at all, each worker takes on 24-32 extra hours, not an arbitrary smaller or larger share.",
}


def month_days(first: dt.date) -> list[dt.date]:
    days = []
    day = first
    while day.month == first.month:
        days.append(day)
        day += dt.timedelta(days=1)
    return days


def calendar_week_id(day: dt.date) -> tuple[int, int]:
    iso = day.isocalendar()
    return iso.year, iso.week


def weekend_id(day: dt.date) -> dt.date:
    """Return the Saturday identifying the weekend containing day."""
    return day + dt.timedelta(days=5 - day.weekday())


def prevention_blocks(days: list[dt.date]) -> list[list[int]]:
    """Group day indices into prevention duty blocks.

    Weekday prevention is a single calendar day, but Friday, Saturday and
    Sunday are held by the same person as one rotating weekend block (per
    hospital feedback). A block is truncated at the edges of the planned
    month if the adjoining weekend day falls outside it.
    """
    blocks: list[list[int]] = []
    used: set[int] = set()
    for d, day in enumerate(days):
        if d in used:
            continue
        block = [d]
        if day.weekday() == 4:  # Friday
            if d + 1 < len(days) and days[d + 1].weekday() == 5:
                block.append(d + 1)
            if d + 2 < len(days) and days[d + 2].weekday() == 6:
                block.append(d + 2)
        elif day.weekday() == 5 and d + 1 < len(days) and days[d + 1].weekday() == 6:
            block.append(d + 1)
        blocks.append(block)
        used.update(block)
    return blocks


def weekends_touching_vacation(vacation_days: set[dt.date]) -> set[dt.date]:
    """Weekends containing, immediately before, or immediately after vacation."""
    touched: set[dt.date] = set()
    for day in vacation_days:
        containing = weekend_id(day)
        touched.update({containing - dt.timedelta(days=7), containing, containing + dt.timedelta(days=7)})
    return touched


def allowed(person: int, date: dt.date, shift: int) -> bool:
    weekday = date.weekday() < 5
    name = PEOPLE[person]
    if name == "Celia L":  # Celia: morning only
        return shift == MORNING
    if name in ("Fernanda", "Sandra", "Cristina", "Ana Martins"):  # no nights
        return shift in (MORNING, AFTERNOON)
    if name == "Celso":  # weekday morning/afternoon
        return weekday and shift in (MORNING, AFTERNOON)
    return True


def _is_reduced_staffing_day(date: dt.date, holiday_dates: Optional[dict[dt.date, str]]) -> bool:
    return date.weekday() >= 5 or date in (holiday_dates or {})


def minimum_required(date: dt.date, shift: int, holiday_dates: Optional[dict[dt.date, str]] = None) -> int:
    if _is_reduced_staffing_day(date, holiday_dates):
        return {MORNING: 4, AFTERNOON: 2, NIGHT: 1}[shift]
    return {MORNING: 10, AFTERNOON: 3, NIGHT: 1}[shift]


def maximum_allowed(date: dt.date, shift: int, holiday_dates: Optional[dict[dt.date, str]] = None) -> Optional[int]:
    """Hard staffing ceiling, currently only weekday (non-holiday) mornings."""
    if shift == MORNING and not _is_reduced_staffing_day(date, holiday_dates):
        return 11
    return None


def _sum(values: Iterable[cp_model.LinearExpr]) -> cp_model.LinearExpr:
    return sum(values, 0)


def build_model(
    first_day: dt.date,
    vacations: dict[str, set[dt.date]],
    rest_hours: dict[str, int],
    previous_assignments: Optional[dict[str, str]] = None,
    rules: Rules = Rules(),
    weights: Weights = Weights(),
    demand_adjustments: Optional[dict[tuple[dt.date, int], int]] = None,
    reference_assignments: Optional[set[tuple[str, dt.date, str]]] = None,
    forbidden_assignments: Optional[set[tuple[str, dt.date, int]]] = None,
    contract_hours: Optional[dict[str, int]] = None,
    workday_history: Optional[dict[str, int]] = None,
    holiday_dates: Optional[dict[dt.date, str]] = None,
    holiday_history: Optional[dict[str, int]] = None,
    special_holiday_history: Optional[dict[str, dict[str, int]]] = None,
) -> ModelArtifacts:
    days = month_days(first_day)
    n_days = len(days)
    date_to_idx = {day: index for index, day in enumerate(days)}
    model = cp_model.CpModel()
    demand_adjustments = demand_adjustments or {}
    forbidden_assignments = forbidden_assignments or set()
    contract_hours = contract_hours or {name: 35 for name in PEOPLE}
    workday_history = workday_history or {name: 0 for name in PEOPLE}
    holiday_dates = holiday_dates or {}
    holiday_history = holiday_history or {name: 0 for name in PEOPLE}
    special_holiday_history = special_holiday_history or {}
    on_vacation = lambda p, day: day in vacations.get(PEOPLE[p], set())

    x = {
        (p, d, s): model.NewBoolVar(f"x_{p}_{d}_{s}")
        for p in range(len(PEOPLE))
        for d, day in enumerate(days)
        if not on_vacation(p, day)
        for s in SHIFTS
        if allowed(p, day, s)
    }

    for p, name in enumerate(PEOPLE):
        for d, day in enumerate(days):
            for s in SHIFTS:
                if (name, day, s) in forbidden_assignments and (p, d, s) in x:
                    model.Add(x[p, d, s] == 0)

    for p in range(len(PEOPLE)):
        for d in range(n_days):
            model.Add(_sum(x[p, d, s] for s in SHIFTS if (p, d, s) in x) <= 1)

    # Every weekday morning must have at least six workers. The operational
    # target remains ten: any gap between six and ten is explicit in the
    # shortfall report and objective. Monday/Wednesday/Friday retain a much
    # higher shortfall weight because they are the busiest exam days.
    morning_shortfalls: dict[int, cp_model.IntVar] = {}
    key_day_morning_shortfall: list[cp_model.IntVar] = []
    other_day_morning_shortfall: list[cp_model.IntVar] = []
    for d, day in enumerate(days):
        for s in SHIFTS:
            adjustment = demand_adjustments.get((day, s), 0)
            required = minimum_required(day, s, holiday_dates) + adjustment
            staffed = _sum(x[p, d, s] for p in range(len(PEOPLE)) if (p, d, s) in x)
            if s == MORNING and not _is_reduced_staffing_day(day, holiday_dates):
                hard_floor = min(required, WEEKDAY_MORNING_HARD_MINIMUM + max(0, adjustment))
                model.Add(staffed >= hard_floor)
                shortfall = model.NewIntVar(0, required, f"morning_shortfall_{d}")
                model.Add(staffed + shortfall >= required)
                morning_shortfalls[d] = shortfall
                if day.weekday() in (0, 2, 4):  # Monday, Wednesday, Friday
                    key_day_morning_shortfall.append(shortfall)
                else:
                    other_day_morning_shortfall.append(shortfall)
            else:
                model.Add(staffed >= required)
            ceiling = maximum_allowed(day, s, holiday_dates)
            if ceiling is not None:
                model.Add(staffed <= ceiling + adjustment)

    early: dict[tuple[int, int], cp_model.IntVar] = {}
    for d, day in enumerate(days):
        if day.weekday() >= 5:
            continue
        candidates = []
        for p in range(len(PEOPLE)):
            if (p, d, AFTERNOON) in x:
                marker = model.NewBoolVar(f"early_{p}_{d}")
                model.Add(marker <= x[p, d, AFTERNOON])
                early[p, d] = marker
                candidates.append(marker)
        model.Add(_sum(candidates) == 1)

    # A normal ("T") afternoon is an afternoon shift that is not the day's
    # single early ("T14"/EA) assignment. Weekends have no EA concept, so any
    # weekend afternoon is definitionally normal.
    normal_afternoon: dict[tuple[int, int], cp_model.IntVar] = {}
    for p in range(len(PEOPLE)):
        for d in range(n_days):
            afternoon_var = x.get((p, d, AFTERNOON))
            if afternoon_var is None:
                continue
            marker = early.get((p, d))
            if marker is None:
                normal_afternoon[p, d] = afternoon_var
                continue
            flag = model.NewBoolVar(f"normal_afternoon_{p}_{d}")
            model.Add(flag <= afternoon_var)
            model.Add(flag + marker == afternoon_var)
            normal_afternoon[p, d] = flag

    # Hospital feedback: staff should not work more than two consecutive
    # afternoons (normal or early), with three as an absolute ceiling.
    excess_afternoon_streak: list[cp_model.IntVar] = []
    hard_window = rules.max_consecutive_afternoons + 1
    for p in range(len(PEOPLE)):
        for start in range(n_days - hard_window + 1):
            keys = [(p, d, AFTERNOON) for d in range(start, start + hard_window)]
            if not all(key in x for key in keys):
                continue
            model.Add(_sum(x[key] for key in keys) <= rules.max_consecutive_afternoons)
        soft_window = rules.max_consecutive_afternoons  # preferred cap is one less than the hard cap
        for start in range(n_days - soft_window + 1):
            keys = [(p, d, AFTERNOON) for d in range(start, start + soft_window)]
            if not all(key in x for key in keys):
                continue
            penalty = model.NewBoolVar(f"excess_afternoon_streak_{p}_{start}")
            model.Add(_sum(x[key] for key in keys) <= soft_window - 1 + penalty)
            excess_afternoon_streak.append(penalty)

    week_ids = sorted({calendar_week_id(day) for day in days})
    for p in range(len(PEOPLE)):
        for week in week_ids:
            indices = [d for d, day in enumerate(days) if calendar_week_id(day) == week]
            model.Add(SHIFT_DUR * _sum(x[p, d, s] for d in indices for s in SHIFTS if (p, d, s) in x) <= rules.weekly_hours)

    weekend_saturdays = sorted({weekend_id(day) for day in days if day.weekday() >= 5})
    work_weekend: dict[tuple[int, int], cp_model.IntVar] = {}
    split_weekend: list[cp_model.IntVar] = []
    adjacent_vacation: list[cp_model.IntVar] = []
    consecutive_weekends: list[cp_model.IntVar] = []
    weekend_eligible: list[int] = []
    weekend_opportunities: dict[int, int] = {}

    for p in range(len(PEOPLE)):
        can_work_weekend = False
        possible_weekends = 0
        touched = weekends_touching_vacation(vacations.get(PEOPLE[p], set()))
        for w, saturday in enumerate(weekend_saturdays):
            day_vars = []
            worked_day_vars = []
            for date in (saturday, saturday + dt.timedelta(days=1)):
                if date not in date_to_idx:
                    continue
                d = date_to_idx[date]
                shifts = [x[p, d, s] for s in SHIFTS if (p, d, s) in x]
                if shifts:
                    can_work_weekend = True
                    worked = model.NewBoolVar(f"weekend_day_{p}_{d}")
                    model.Add(worked == _sum(shifts))
                    worked_day_vars.append(worked)
                    day_vars.extend(shifts)
            wk = model.NewBoolVar(f"weekend_{p}_{w}")
            work_weekend[p, w] = wk
            if day_vars:
                possible_weekends += 1
                model.AddMaxEquality(wk, day_vars)
                worked_days = model.NewIntVar(0, 2, f"weekend_days_{p}_{w}")
                model.Add(worked_days == _sum(worked_day_vars))
                split = model.NewBoolVar(f"split_weekend_{p}_{w}")
                model.Add(worked_days == 1).OnlyEnforceIf(split)
                model.Add(worked_days != 1).OnlyEnforceIf(split.Not())
                split_weekend.append(split)
            else:
                model.Add(wk == 0)
            if saturday in touched:
                adjacent_vacation.append(wk)
        if can_work_weekend:
            weekend_eligible.append(p)
            weekend_opportunities[p] = possible_weekends

    for p in weekend_eligible:
        for w in range(len(weekend_saturdays) - 1):
            penalty = model.NewBoolVar(f"consecutive_weekends_{p}_{w}")
            model.Add(work_weekend[p, w] + work_weekend[p, w + 1] <= 1 + penalty)
            consecutive_weekends.append(penalty)

    weekend_counts: dict[int, cp_model.IntVar] = {}
    for p in weekend_eligible:
        count = model.NewIntVar(0, len(weekend_saturdays), f"weekend_count_{p}")
        model.Add(count == _sum(work_weekend[p, w] for w in range(len(weekend_saturdays))))
        weekend_counts[p] = count
    # Pairwise cross-products compare count/opportunity without rounding or division.
    max_weekend_burden_gap = model.NewIntVar(
        0, len(weekend_saturdays) ** 2, "max_weekend_burden_gap"
    )
    for index, p in enumerate(weekend_eligible):
        for q in weekend_eligible[index + 1:]:
            difference = (
                weekend_counts[p] * weekend_opportunities[q]
                - weekend_counts[q] * weekend_opportunities[p]
            )
            model.Add(difference <= max_weekend_burden_gap)
            model.Add(-difference <= max_weekend_burden_gap)

    for p in range(len(PEOPLE)):
        for d in range(n_days - 1):
            if (p, d, AFTERNOON) in x and (p, d + 1, MORNING) in x:
                allowance = early.get((p, d), 0)
                model.Add(x[p, d, AFTERNOON] + x[p, d + 1, MORNING] <= 1 + allowance)
            if (p, d, AFTERNOON) in x and (p, d + 1, NIGHT) in x and (p, d) in early:
                model.Add(early[p, d] + x[p, d + 1, NIGHT] <= 1)

    previous_assignments = previous_assignments or {}
    for p, name in enumerate(PEOPLE):
        prior = previous_assignments.get(name)
        if prior == "N":
            model.Add(_sum(x[p, 0, s] for s in SHIFTS if (p, 0, s) in x) == 0)
        elif prior == "A" and (p, 0, MORNING) in x:
            model.Add(x[p, 0, MORNING] == 0)

    # Hospital feedback: when short-staffed, monthly overtime is shared fairly
    # rather than concentrated on a few people — anyone who works overtime
    # that month does between 24 and 32 extra hours, never an arbitrary
    # smaller or larger amount. Going further requires separately-authorized
    # overtime pay, which isn't modelled here.
    monthly_overtime: dict[int, cp_model.IntVar] = {}
    for p, name in enumerate(PEOPLE):
        assigned = _sum(x[p, d, s] for d in range(n_days) for s in SHIFTS if (p, d, s) in x)
        weekly_contract = contract_hours.get(name, 35)
        # Prorate the 35 h/40 h contract over the calendar days in this month.
        # Flooring to complete 8 h shifts naturally gives roughly one extra rest
        # day per eight worked shifts for a 35 h rather than 40 h contract.
        monthly_cap = (weekly_contract * n_days // 7 // SHIFT_DUR) * SHIFT_DUR
        base_cap = max(0, monthly_cap - rest_hours.get(name, 0))
        extra = model.NewIntVar(0, 32, f"monthly_overtime_{p}")
        uses_overtime = model.NewBoolVar(f"uses_overtime_{p}")
        model.Add(extra == 0).OnlyEnforceIf(uses_overtime.Not())
        model.Add(extra >= 24).OnlyEnforceIf(uses_overtime)
        monthly_overtime[p] = extra
        model.Add(SHIFT_DUR * assigned <= base_cap + extra)

    for p in range(len(PEOPLE)):
        for d in range(n_days - 1):
            if (p, d, NIGHT) in x:
                next_day = _sum(x[p, d + 1, s] for s in SHIFTS if (p, d + 1, s) in x)
                model.Add(x[p, d, NIGHT] + next_day <= 1)

    night_workers = [p for p in range(len(PEOPLE)) if any((p, d, NIGHT) in x for d in range(n_days))]
    total_nights = sum(minimum_required(day, NIGHT, holiday_dates) for day in days)
    floor_nights, remainder = divmod(total_nights, len(night_workers))
    ceil_nights = floor_nights + bool(remainder)
    for p in night_workers:
        night_vars = [x[p, d, NIGHT] for d in range(n_days) if (p, d, NIGHT) in x]
        model.Add(_sum(night_vars) >= floor_nights)
        model.Add(_sum(night_vars) <= ceil_nights)
        for d in range(n_days):
            if (p, d, NIGHT) not in x:
                continue
            later = [x[p, dd, NIGHT] for dd in range(d + 1, min(d + rules.min_days_between_nights, n_days)) if (p, dd, NIGHT) in x]
            model.Add(x[p, d, NIGHT] + _sum(later) <= 1)

    rest: dict[tuple[int, int], cp_model.IntVar] = {}
    work_day: dict[tuple[int, int], cp_model.LinearExpr] = {}
    excess_work_streak: list[cp_model.IntVar] = []
    isolated_work_day: list[cp_model.IntVar] = []
    isolated_rest_day: list[cp_model.IntVar] = []
    for p in range(len(PEOPLE)):
        for d, day in enumerate(days):
            if on_vacation(p, day):
                continue
            work_day[p, d] = _sum(x[p, d, s] for s in SHIFTS if (p, d, s) in x)
            rest[p, d] = model.NewBoolVar(f"rest_{p}_{d}")
            model.Add(rest[p, d] + work_day[p, d] == 1)

        window = rules.max_consecutive_work_days + 1
        for start in range(n_days - window + 1):
            keys = [(p, d) for d in range(start, start + window)]
            if not all(key in work_day for key in keys):
                continue
            penalty = model.NewBoolVar(f"excess_work_streak_{p}_{start}")
            model.Add(_sum(work_day[key] for key in keys) <= rules.max_consecutive_work_days + penalty)
            excess_work_streak.append(penalty)

        for d in range(1, n_days - 1):
            keys = ((p, d - 1), (p, d), (p, d + 1))
            if not all(key in rest for key in keys):
                continue
            isolated_work = model.NewBoolVar(f"isolated_work_{p}_{d}")
            model.Add(isolated_work <= rest[p, d - 1])
            model.Add(isolated_work <= work_day[p, d])
            model.Add(isolated_work <= rest[p, d + 1])
            model.Add(isolated_work >= rest[p, d - 1] + work_day[p, d] + rest[p, d + 1] - 2)
            isolated_work_day.append(isolated_work)

            isolated_rest = model.NewBoolVar(f"isolated_rest_{p}_{d}")
            model.Add(isolated_rest <= work_day[p, d - 1])
            model.Add(isolated_rest <= rest[p, d])
            model.Add(isolated_rest <= work_day[p, d + 1])
            model.Add(isolated_rest >= work_day[p, d - 1] + rest[p, d] + work_day[p, d + 1] - 2)
            isolated_rest_day.append(isolated_rest)

    # Hospital feedback: staff are eased into a night shift with one of two
    # three-day run-ups: normal afternoon -> early afternoon -> morning
    # ("T, T14, M"), or normal afternoon -> normal afternoon -> rest day
    # ("T, T, D"). Penalise a night preceded by neither, when checkable.
    night_ramp_mismatch: list[cp_model.IntVar] = []
    for p in range(len(PEOPLE)):
        for d in range(3, n_days):
            if (p, d, NIGHT) not in x:
                continue
            d3, d2, d1 = d - 3, d - 2, d - 1
            candidates = []

            afternoon_d3, early_d2, morning_d1 = normal_afternoon.get((p, d3)), early.get((p, d2)), x.get((p, d1, MORNING))
            if afternoon_d3 is not None and early_d2 is not None and morning_d1 is not None:
                pattern_a = model.NewBoolVar(f"night_ramp_a_{p}_{d}")
                model.Add(pattern_a <= afternoon_d3)
                model.Add(pattern_a <= early_d2)
                model.Add(pattern_a <= morning_d1)
                model.Add(pattern_a >= afternoon_d3 + early_d2 + morning_d1 - 2)
                candidates.append(pattern_a)

            afternoon_d3b, afternoon_d2, rest_d1 = normal_afternoon.get((p, d3)), normal_afternoon.get((p, d2)), rest.get((p, d1))
            if afternoon_d3b is not None and afternoon_d2 is not None and rest_d1 is not None:
                pattern_b = model.NewBoolVar(f"night_ramp_b_{p}_{d}")
                model.Add(pattern_b <= afternoon_d3b)
                model.Add(pattern_b <= afternoon_d2)
                model.Add(pattern_b <= rest_d1)
                model.Add(pattern_b >= afternoon_d3b + afternoon_d2 + rest_d1 - 2)
                candidates.append(pattern_b)

            if not candidates:
                continue
            mismatch = model.NewBoolVar(f"night_ramp_mismatch_{p}_{d}")
            model.Add(_sum(candidates) + mismatch >= x[p, d, NIGHT])
            night_ramp_mismatch.append(mismatch)

    # Avoid returning from a vacation block directly into a night shift. This
    # remains soft so that coverage can still be achieved during shortages.
    night_after_vacation: list[cp_model.IntVar] = []
    true_vacations = getattr(vacations, "vacation_days", vacations)
    for p, name in enumerate(PEOPLE):
        person_vacations = true_vacations.get(name, set())
        for d, day in enumerate(days):
            if (p, d, NIGHT) not in x:
                continue
            if day - dt.timedelta(days=1) in person_vacations:
                night_after_vacation.append(x[p, d, NIGHT])

    # Proportional targets account for differing availability/eligibility.
    total_required_hours = SHIFT_DUR * sum(
        minimum_required(day, s, holiday_dates) + demand_adjustments.get((day, s), 0)
        for day in days for s in SHIFTS
    )
    capacities = {
        p: sum(1 for d, day in enumerate(days) if not on_vacation(p, day) and any((p, d, s) in x for s in SHIFTS))
        for p in range(len(PEOPLE))
    }
    total_capacity = sum(capacities.values())
    max_deviation = model.NewIntVar(0, n_days * SHIFT_DUR * total_capacity, "max_scaled_workload_deviation")
    for p in range(len(PEOPLE)):
        hours = SHIFT_DUR * _sum(x[p, d, s] for d in range(n_days) for s in SHIFTS if (p, d, s) in x)
        # Compare hours/capacity without division: |hours*total_capacity-required*capacity|.
        model.Add(hours * total_capacity - total_required_hours * capacities[p] <= max_deviation)
        model.Add(total_required_hours * capacities[p] - hours * total_capacity <= max_deviation)

    # Hospital requirement: cumulative worked days should be as equal as
    # possible, with any unavoidable difference carried into the next month.
    cumulative_workdays = []
    max_history = max(workday_history.values(), default=0)
    for p, name in enumerate(PEOPLE):
        cumulative = model.NewIntVar(0, max_history + n_days, f"cumulative_workdays_{p}")
        model.Add(cumulative == workday_history.get(name, 0) + _sum(
            x[p, d, s] for d in range(n_days) for s in SHIFTS if (p, d, s) in x
        ))
        cumulative_workdays.append(cumulative)
    max_workdays = model.NewIntVar(0, max_history + n_days, "max_cumulative_workdays")
    min_workdays = model.NewIntVar(0, max_history + n_days, "min_cumulative_workdays")
    model.AddMaxEquality(max_workdays, cumulative_workdays)
    model.AddMinEquality(min_workdays, cumulative_workdays)
    workday_range = model.NewIntVar(0, max_history + n_days, "cumulative_workday_range")
    model.Add(workday_range == max_workdays - min_workdays)

    # Public-holiday rotation. Historical counts allow compensation in later
    # months. Christmas, New Year and Easter also have a separate rotation.
    holiday_days_in_month = {
        day: label for day, label in holiday_dates.items() if day in date_to_idx
    }
    holiday_rotation_terms: list[cp_model.IntVar] = []
    if holiday_days_in_month:
        cumulative_holidays = []
        history_max = max(holiday_history.values(), default=0)
        for p, name in enumerate(PEOPLE):
            worked = _sum(
                x[p, date_to_idx[day], s]
                for day in holiday_days_in_month for s in SHIFTS
                if (p, date_to_idx[day], s) in x
            )
            value = model.NewIntVar(0, history_max + len(holiday_days_in_month), f"holiday_count_{p}")
            model.Add(value == holiday_history.get(name, 0) + worked)
            cumulative_holidays.append(value)
        high = model.NewIntVar(0, history_max + len(holiday_days_in_month), "max_holiday_count")
        low = model.NewIntVar(0, history_max + len(holiday_days_in_month), "min_holiday_count")
        model.AddMaxEquality(high, cumulative_holidays)
        model.AddMinEquality(low, cumulative_holidays)
        gap = model.NewIntVar(0, history_max + len(holiday_days_in_month), "holiday_rotation_gap")
        model.Add(gap == high - low)
        holiday_rotation_terms.append(gap)

    special_rotation_terms: list[cp_model.IntVar] = []
    for category in ("natal", "ano_novo", "pascoa"):
        event_days = [day for day, label in holiday_days_in_month.items() if label == category]
        if not event_days:
            continue
        history = special_holiday_history.get(category, {})
        history_max = max(history.values(), default=0)
        counts = []
        for p, name in enumerate(PEOPLE):
            worked = _sum(
                x[p, date_to_idx[day], s] for day in event_days for s in SHIFTS
                if (p, date_to_idx[day], s) in x
            )
            value = model.NewIntVar(0, history_max + len(event_days), f"{category}_count_{p}")
            model.Add(value == history.get(name, 0) + worked)
            counts.append(value)
        high = model.NewIntVar(0, history_max + len(event_days), f"max_{category}_count")
        low = model.NewIntVar(0, history_max + len(event_days), f"min_{category}_count")
        model.AddMaxEquality(high, counts)
        model.AddMinEquality(low, counts)
        gap = model.NewIntVar(0, history_max + len(event_days), f"{category}_rotation_gap")
        model.Add(gap == high - low)
        special_rotation_terms.append(gap)

    schedule_changes: list[cp_model.IntVar] = []
    if reference_assignments is not None:
        for p, name in enumerate(PEOPLE):
            for d, day in enumerate(days):
                current: dict[str, cp_model.LinearExpr | cp_model.IntVar | None] = {
                    "M": x.get((p, d, MORNING)),
                    "N": x.get((p, d, NIGHT)),
                    "EA": early.get((p, d)),
                }
                current["A"] = normal_afternoon.get((p, d))
                for code, variable in current.items():
                    was_assigned = (name, day, code) in reference_assignments
                    if variable is None:
                        if was_assigned:
                            schedule_changes.append(model.NewConstant(1))
                        continue
                    if was_assigned:
                        removed = model.NewBoolVar(f"removed_{p}_{d}_{code}")
                        model.Add(removed + variable == 1)
                        schedule_changes.append(removed)
                    else:
                        schedule_changes.append(variable)

    terms = {
        "workday_balance": [workday_range],
        "workload_imbalance": [max_deviation],
        "excess_work_streak": excess_work_streak,
        "isolated_work_day": isolated_work_day,
        "isolated_rest_day": isolated_rest_day,
        "excess_afternoon_streak": excess_afternoon_streak,
        "night_ramp_mismatch": night_ramp_mismatch,
        "night_after_vacation": night_after_vacation,
        "holiday_rotation": holiday_rotation_terms,
        "special_holiday_rotation": special_rotation_terms,
        "weekend_imbalance": [max_weekend_burden_gap],
        "consecutive_weekends": consecutive_weekends,
        "split_weekend": split_weekend,
        "vacation_adjacent_weekend": adjacent_vacation,
        "schedule_changes": schedule_changes,
        "morning_shortfall_key_day": key_day_morning_shortfall,
        "morning_shortfall_other_day": other_day_morning_shortfall,
        "monthly_overtime": list(monthly_overtime.values()),
    }
    weight_map = vars(weights)
    model.Minimize(_sum(weight_map[name] * _sum(items) for name, items in terms.items()))
    return ModelArtifacts(
        model=model, days=days, assignments=x, early=early, morning_shortfalls=morning_shortfalls,
        monthly_overtime=monthly_overtime,
        objective_terms=terms, constraint_catalog={**HARD_CONSTRAINTS, **SOFT_PREFERENCES},
    )


def build_hemodinamica_model(
    first_day: dt.date,
    vacations: dict[str, set[dt.date]],
    holiday_dates: Optional[dict[dt.date, str]] = None,
    contract_hours: Optional[dict[str, int]] = None,
    rest_hours: Optional[dict[str, int]] = None,
    rules: Rules = Rules(),
    weights: HemodinamicaWeights = HemodinamicaWeights(),
    lina_unavailable_days: Optional[set[dt.date]] = None,
) -> ModelArtifacts:
    """Hemodinâmica's on-call rotation plus its weekday morning shift.

    Angelo and Nuno alternate 24/7 on-call duty exactly as before (unchanged
    from the retired imaging-side logic). Per hospital feedback, the on-call
    holder also works that day's weekday morning shift; the other of the two
    may independently join as well. The morning shift always needs exactly
    two people present: Lina joins whenever only one of Angelo/Nuno is
    assigned, and stays off when both already are. `lina_unavailable_days`
    lets a caller mark days Lina has already committed to imaging
    (cross-department exclusivity) — day-level only; the shared weekly-hour
    cap across departments still needs to be closed by the caller via
    `rest_hours`.
    """
    days = month_days(first_day)
    n_days = len(days)
    model = cp_model.CpModel()
    holiday_dates = holiday_dates or {}
    contract_hours = contract_hours or {name: 35 for name in PEOPLE}
    rest_hours = rest_hours or {name: 0 for name in PEOPLE}
    lina_unavailable_days = lina_unavailable_days or set()
    on_vacation = lambda p, day: day in vacations.get(PEOPLE[p], set())
    angelo, nuno, lina = PEOPLE.index("Angelo"), PEOPLE.index("Nuno"), PEOPLE.index("Lina")

    # On-call prevention: exactly one of Angelo/Nuno every day, held in
    # Friday-Saturday-Sunday blocks, alternating (unchanged from the retired
    # imaging-side logic) — except that alternation only applies between two
    # blocks where BOTH of them are actually free for both blocks. Hospital
    # feedback: when one is on vacation, the other simply covers on-call for
    # as long as needed; there's no one to alternate with, so the switch-
    # every-block rule is dropped for exactly that stretch.
    def can_hold_block(person: int, block: list[int]) -> bool:
        return not any(on_vacation(person, days[d]) for d in block)

    prevention: dict[tuple[int, int], cp_model.IntVar] = {}
    for p in (angelo, nuno):
        for d, day in enumerate(days):
            prevention[p, d] = model.NewBoolVar(f"prevention_{p}_{d}")
            if on_vacation(p, day):
                model.Add(prevention[p, d] == 0)
    for d in range(n_days):
        model.Add(prevention[angelo, d] + prevention[nuno, d] == 1)
    blocks = prevention_blocks(days)
    for p in (angelo, nuno):
        for block in blocks:
            for d in block[1:]:
                model.Add(prevention[p, block[0]] == prevention[p, d])
    for current_block, next_block in zip(blocks, blocks[1:]):
        both_free_both_blocks = all(
            can_hold_block(p, block)
            for p in (angelo, nuno)
            for block in (current_block, next_block)
        )
        if both_free_both_blocks:
            model.Add(prevention[angelo, current_block[0]] + prevention[angelo, next_block[0]] == 1)

    # Weekday morning shift.
    morning: dict[tuple[int, int], cp_model.IntVar] = {}
    non_thursday_single_coverage: list[cp_model.IntVar] = []
    for d, day in enumerate(days):
        if day.weekday() >= 5:
            continue
        for p in (angelo, nuno):
            var = model.NewBoolVar(f"morning_{p}_{d}")
            if on_vacation(p, day):
                model.Add(var == 0)
            model.Add(var >= prevention[p, d])
            morning[p, d] = var
        # Hemodinamica always needs exactly two people present: Lina joins
        # whenever only one of Angelo/Nuno is on the morning shift, and stays
        # off when both of them already are.
        lina_var = model.NewBoolVar(f"morning_lina_{d}")
        if on_vacation(lina, day) or day in lina_unavailable_days:
            model.Add(lina_var == 0)
        model.Add(lina_var + morning[angelo, d] + morning[nuno, d] == 2)
        morning[lina, d] = lina_var

        if day.weekday() != 3:  # Thursday is the preferred single-coverage day
            pair_total = model.NewIntVar(0, 2, f"morning_pair_total_{d}")
            model.Add(pair_total == morning[angelo, d] + morning[nuno, d])
            only_one = model.NewBoolVar(f"single_coverage_{d}")
            model.Add(pair_total == 1).OnlyEnforceIf(only_one)
            model.Add(pair_total != 1).OnlyEnforceIf(only_one.Not())
            non_thursday_single_coverage.append(only_one)

    # Weekly/monthly hour caps, same legal-compliance shape as imaging.
    week_ids = sorted({calendar_week_id(day) for day in days})
    for p, name in ((angelo, "Angelo"), (nuno, "Nuno"), (lina, "Lina")):
        for week in week_ids:
            indices = [d for d, day in enumerate(days) if calendar_week_id(day) == week and (p, d) in morning]
            if indices:
                model.Add(SHIFT_DUR * _sum(morning[p, d] for d in indices) <= rules.weekly_hours)
        assigned = _sum(morning[p, d] for d in range(n_days) if (p, d) in morning)
        weekly_contract = contract_hours.get(name, 35)
        monthly_cap = (weekly_contract * n_days // 7 // SHIFT_DUR) * SHIFT_DUR
        model.Add(SHIFT_DUR * assigned <= max(0, monthly_cap - rest_hours.get(name, 0)))

    # Each Sunday/holiday on-call duty needs a distinct free weekday to be
    # labelled as compensatory rest (same shape as the retired imaging logic).
    for p, name in ((angelo, "Angelo"), (nuno, "Nuno")):
        compensation_count = _sum(
            prevention[p, d] for d, day in enumerate(days) if day.weekday() == 6 or day in holiday_dates
        )
        weekday_assignments = _sum(
            morning[p, d] for d, day in enumerate(days) if day.weekday() < 5 and (p, d) in morning
        )
        available_weekdays = sum(day.weekday() < 5 and not on_vacation(p, day) for day in days)
        model.Add(weekday_assignments + compensation_count <= available_weekdays)

    model.Minimize(weights.non_thursday_single_coverage * _sum(non_thursday_single_coverage))
    return ModelArtifacts(
        model=model, days=days, prevention=prevention, morning=morning,
        objective_terms={"non_thursday_single_coverage": non_thursday_single_coverage},
        constraint_catalog={**HEMODINAMICA_HARD_CONSTRAINTS, **HEMODINAMICA_SOFT_PREFERENCES},
    )


def load_json(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_department(config_path: Path, department: str) -> dict[str, set[dt.date]]:
    """Load a month's department roster and populate PEOPLE.

    Mutates the module-level PEOPLE list in place (see its docstring), then
    returns a name -> unavailable-dates mapping merging that department's
    vacations, consultas and sick leave, all represented as whole-day blocks.
    True vacation dates remain available as metadata for vacation-only soft
    preferences.
    """
    raw = load_json(config_path)
    if department not in raw:
        raise ValueError(f"Unknown department '{department}' in {config_path}")
    dept = raw[department]
    PEOPLE[:] = dept["workers"]
    vacation_days = {
        name: {dt.date.fromisoformat(value) for value in dept.get("vacations", {}).get(name, [])}
        for name in PEOPLE
    }
    unavailable: Availability = Availability(
        {name: set() for name in PEOPLE}, vacation_days=vacation_days
    )
    for source in ("vacations", "consultas", "sick_leave"):
        for name, dates in dept.get(source, {}).items():
            if name not in unavailable:
                raise ValueError(
                    f"Unknown person '{name}' in {source} for department '{department}' of {config_path}"
                )
            unavailable[name].update(dt.date.fromisoformat(value) for value in dates)
    return unavailable


def load_previous_assignments(path: Optional[Path], first_day: dt.date) -> dict[str, str]:
    if path is None:
        return {}
    target = first_day - dt.timedelta(days=1)
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if dt.date.fromisoformat(row["date"]) == target:
                result[row["person"]] = row["shift"]
    return result


def resolved_workday_history(month: int, workday_values: dict[str, int]) -> dict[str, int]:
    """Carry the cumulative-workday balance month to month, resetting every January.

    Hospital feedback: workday equality is targeted per calendar year, not
    indefinitely, so the carried difference starts fresh at each year's first
    month rather than accumulating across year boundaries.
    """
    if month == 1:
        return {name: 0 for name in PEOPLE}
    return {name: int(workday_values.get(name, 0)) for name in PEOPLE}


def easter_sunday(year: int) -> dt.date:
    """Gregorian Easter date (Meeus/Jones/Butcher algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def load_holiday_config(path: Optional[Path], year: int) -> tuple[dict[dt.date, str], dict[str, int], dict[str, dict[str, int]]]:
    raw = load_json(path)
    dates = {dt.date.fromisoformat(day): str(label) for day, label in raw.get("dates", {}).items()}
    dates.setdefault(dt.date(year, 1, 1), "ano_novo")
    dates.setdefault(easter_sunday(year), "pascoa")
    dates.setdefault(dt.date(year, 12, 25), "natal")
    history_values = raw.get("history", raw.get("holiday_history", {}))
    history = {name: int(history_values.get(name, 0)) for name in PEOPLE}
    special = {
        category: {name: int(raw.get("special_history", {}).get(category, {}).get(name, 0)) for name in PEOPLE}
        for category in ("natal", "ano_novo", "pascoa")
    }
    return dates, history, special


def extract_schedule(artifacts: ModelArtifacts, solver: cp_model.CpSolver) -> list[dict[str, str | int]]:
    rows = []
    for (p, d, s), variable in artifacts.assignments.items():
        if not solver.Value(variable):
            continue
        code = "EA" if s == AFTERNOON and (p, d) in artifacts.early and solver.Value(artifacts.early[p, d]) else SHIFT_NAMES[s]
        start, end = SHIFT_TIMES[code]
        rows.append({
            "date": artifacts.days[d].isoformat(),
            "weekday": artifacts.days[d].strftime("%A"),
            "person": PEOPLE[p],
            "shift": code,
            "start_hour": start,
            "end_hour": end,
        })
    return sorted(rows, key=lambda row: (row["date"], row["person"]))


def extract_hemodinamica_schedule(artifacts: ModelArtifacts, solver: cp_model.CpSolver) -> list[dict[str, str | int]]:
    rows = []
    for (p, d), variable in artifacts.morning.items():
        if not solver.Value(variable):
            continue
        start, end = SHIFT_TIMES["M"]
        rows.append({
            "date": artifacts.days[d].isoformat(),
            "weekday": artifacts.days[d].strftime("%A"),
            "person": PEOPLE[p],
            "shift": "M",
            "start_hour": start,
            "end_hour": end,
        })
    return sorted(rows, key=lambda row: (row["date"], row["person"]))


def extract_prevention(
    artifacts: ModelArtifacts,
    solver: cp_model.CpSolver,
    holiday_dates: dict[dt.date, str],
) -> list[dict[str, str | bool]]:
    rows = []
    for (p, d), variable in artifacts.prevention.items():
        if solver.Value(variable):
            day = artifacts.days[d]
            compensatory = day.weekday() == 6 or day in holiday_dates
            rows.append({
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "person": PEOPLE[p],
                "compensatory_rest_earned": compensatory,
            })
    return sorted(rows, key=lambda row: str(row["date"]))


def allocate_compensatory_rest_days(
    schedule_rows: list[dict[str, str | int]],
    prevention_rows: list[dict[str, str | bool]],
    vacations: dict[str, set[dt.date]],
    days: list[dt.date],
) -> list[dict[str, str]]:
    """Label suitable existing rest days that account for earned compensation."""
    worked = {(str(row["person"]), dt.date.fromisoformat(str(row["date"]))) for row in schedule_rows}
    used: set[tuple[str, dt.date]] = set()
    result = []
    for duty in prevention_rows:
        if not duty["compensatory_rest_earned"]:
            continue
        person = str(duty["person"])
        earned = dt.date.fromisoformat(str(duty["date"]))
        candidates = [
            day for day in days
            if day.weekday() < 5
            and day not in vacations.get(person, set())
            and (person, day) not in worked
            and (person, day) not in used
        ]
        candidates.sort(key=lambda day: (day <= earned, abs((day - earned).days)))
        if candidates:
            rest_day = candidates[0]
            used.add((person, rest_day))
            result.append({
                "person": person,
                "earned_on": earned.isoformat(),
                "rest_date": rest_day.isoformat(),
            })
    return sorted(result, key=lambda row: (row["rest_date"], row["person"]))


def validate_prevention_schedule(
    prevention_rows: list[dict[str, str | bool]],
    compensation_rows: list[dict[str, str]],
    schedule_rows: list[dict[str, str | int]],
    days: list[dt.date],
    holiday_dates: dict[dt.date, str],
    vacations: Optional[dict[str, set[dt.date]]] = None,
) -> list[str]:
    errors: list[str] = []
    vacations = vacations or {}
    by_day = {dt.date.fromisoformat(str(row["date"])): str(row["person"]) for row in prevention_rows}
    for day in days:
        if day not in by_day:
            errors.append(f"No prevention assigned on {day}")
        elif by_day[day] not in ("Angelo", "Nuno"):
            errors.append(f"Invalid prevention worker on {day}: {by_day[day]}")
    blocks = prevention_blocks(days)
    for block in blocks:
        holders = {by_day.get(days[d]) for d in block}
        if len(holders) > 1:
            errors.append(f"Prevention changes worker within the Friday-Sunday block starting {days[block[0]]}")
    for current_block, next_block in zip(blocks, blocks[1:]):
        if by_day.get(days[current_block[0]]) == by_day.get(days[next_block[0]]):
            # Not an error if one of the two couldn't have covered either
            # block anyway (on vacation) — there was no one to alternate
            # with, so the same holder covering both blocks is expected.
            both_could_alternate = all(
                day not in vacations.get(name, set())
                for name in ("Angelo", "Nuno")
                for block in (current_block, next_block)
                for day in (days[d] for d in block)
            )
            if both_could_alternate:
                errors.append(f"Prevention does not alternate on {days[next_block[0]]}")
    earned = {
        (str(row["person"]), str(row["date"]))
        for row in prevention_rows
        if dt.date.fromisoformat(str(row["date"])).weekday() == 6
        or dt.date.fromisoformat(str(row["date"])) in holiday_dates
    }
    compensated = {(row["person"], row["earned_on"]) for row in compensation_rows}
    if earned != compensated:
        errors.append("Compensatory-rest records do not match Sunday/holiday prevention duties")
    worked = {(str(row["person"]), str(row["date"])) for row in schedule_rows}
    rest_keys = set()
    for row in compensation_rows:
        key = (row["person"], row["rest_date"])
        if key in worked:
            errors.append(f"Compensatory rest overlaps work for {row['person']} on {row['rest_date']}")
        if key in rest_keys:
            errors.append(f"Duplicate compensatory rest for {row['person']} on {row['rest_date']}")
        rest_keys.add(key)
    return errors


def validate_schedule(
    rows: list[dict[str, str | int]],
    first_day: dt.date,
    vacations: dict[str, set[dt.date]],
    rest_hours: Optional[dict[str, int]] = None,
    previous_assignments: Optional[dict[str, str]] = None,
    rules: Rules = Rules(),
    contract_hours: Optional[dict[str, int]] = None,
    holiday_dates: Optional[dict[dt.date, str]] = None,
) -> list[str]:
    errors: list[str] = []
    by_person_day: dict[tuple[str, dt.date], list[dict]] = {}
    staffing: dict[tuple[dt.date, int], int] = {}
    code_to_shift = {"M": MORNING, "A": AFTERNOON, "EA": AFTERNOON, "N": NIGHT}
    for row in rows:
        day = dt.date.fromisoformat(str(row["date"]))
        person = str(row["person"])
        shift = code_to_shift[str(row["shift"])]
        by_person_day.setdefault((person, day), []).append(row)
        staffing[day, shift] = staffing.get((day, shift), 0) + 1
        if day in vacations.get(person, set()):
            errors.append(f"{person} works during vacation on {day}")
        if not allowed(PEOPLE.index(person), day, shift):
            errors.append(f"{person} is ineligible for {row['shift']} on {day}")
    for (person, day), assigned in by_person_day.items():
        if len(assigned) > 1:
            errors.append(f"{person} has {len(assigned)} shifts on {day}")
    for day in month_days(first_day):
        for shift in SHIFTS:
            actual = staffing.get((day, shift), 0)
            required = minimum_required(day, shift, holiday_dates)
            # Weekday mornings have a hard floor of six and a preferred target
            # of ten. Falling below the target is reported; falling below the
            # floor invalidates the schedule.
            is_soft_morning = shift == MORNING and not _is_reduced_staffing_day(day, holiday_dates)
            if is_soft_morning and actual < min(required, WEEKDAY_MORNING_HARD_MINIMUM):
                errors.append(
                    f"{day} {SHIFT_NAMES[shift]} has {actual}, requires hard minimum "
                    f"{min(required, WEEKDAY_MORNING_HARD_MINIMUM)}"
                )
            if actual < required and not is_soft_morning:
                errors.append(f"{day} {SHIFT_NAMES[shift]} has {actual}, requires {required}")
            ceiling = maximum_allowed(day, shift, holiday_dates)
            if ceiling is not None and actual > ceiling:
                errors.append(f"{day} {SHIFT_NAMES[shift]} has {actual}, exceeds maximum {ceiling}")
        early_count = sum(1 for row in rows if row["date"] == day.isoformat() and row["shift"] == "EA")
        if day.weekday() < 5 and early_count != 1:
            errors.append(f"{day} has {early_count} early-afternoon shifts, requires 1")

    for person in PEOPLE:
        assignments = {
            day: str(items[0]["shift"])
            for (name, day), items in by_person_day.items()
            if name == person
        }
        prior = (previous_assignments or {}).get(person)
        if prior == "N" and first_day in assignments:
            errors.append(f"{person} works {first_day} after a prior-month night")
        if prior == "A" and assignments.get(first_day) == "M":
            errors.append(f"{person} works morning {first_day} after a prior-month afternoon")
        ordered = sorted(assignments)
        for day in ordered:
            tomorrow = day + dt.timedelta(days=1)
            if assignments[day] == "N" and tomorrow in assignments:
                errors.append(f"{person} works {tomorrow} immediately after a night")
            if assignments[day] == "A" and assignments.get(tomorrow) == "M":
                errors.append(f"{person} has a normal afternoon-to-morning transition on {day}")
            if assignments[day] == "EA" and assignments.get(tomorrow) == "N":
                errors.append(f"{person} has an early-afternoon-to-night transition on {day}")
            if assignments[day] == "N":
                for offset in range(1, rules.min_days_between_nights):
                    if assignments.get(day + dt.timedelta(days=offset)) == "N":
                        errors.append(f"{person} has nights less than {rules.min_days_between_nights} days apart")
        afternoon_streak, previous_afternoon_day = 0, None
        for day in ordered:
            if assignments[day] in ("A", "EA"):
                afternoon_streak = afternoon_streak + 1 if previous_afternoon_day == day - dt.timedelta(days=1) else 1
                previous_afternoon_day = day
                if afternoon_streak > rules.max_consecutive_afternoons:
                    errors.append(f"{person} has {afternoon_streak} consecutive afternoon shifts ending {day}")
            else:
                afternoon_streak, previous_afternoon_day = 0, None
        for week in {calendar_week_id(day) for day in ordered}:
            weekly = sum(1 for day in ordered if calendar_week_id(day) == week) * SHIFT_DUR
            if weekly > rules.weekly_hours:
                errors.append(f"{person} has {weekly} hours in ISO week {week}")
        weekly_contract = (contract_hours or {}).get(person, 35)
        cap = (weekly_contract * len(month_days(first_day)) // 7 // SHIFT_DUR) * SHIFT_DUR
        monthly = len(ordered) * SHIFT_DUR
        # A fair-shared overtime allowance (see build_model's monthly_overtime)
        # can add up to 32 h on top of the base cap.
        if monthly > max(0, cap - (rest_hours or {}).get(person, 0)) + 32:
            errors.append(f"{person} has {monthly} monthly hours above their adjusted cap plus overtime allowance")
    return errors


def solve_and_export(
    year: int,
    month: int,
    rest_json: Optional[Path],
    config_json: Path,
    department: str = "imagiologia",
    previous_schedule: Optional[Path] = None,
    output: Path = Path("schedule.csv"),
    time_limit: float = 2100,
    weights: Weights = Weights(),
    random_seed: int = 42,
    demand_adjustments: Optional[dict[tuple[dt.date, int], int]] = None,
    reference_assignments: Optional[set[tuple[str, dt.date, str]]] = None,
    forbidden_assignments: Optional[set[tuple[str, dt.date, int]]] = None,
    contract_hours_json: Optional[Path] = None,
    workday_history_json: Optional[Path] = None,
    holiday_json: Optional[Path] = None,
    state_output: Optional[Path] = None,
    vac_override_json: Optional[Path] = None,
    extra_rest_hours: Optional[dict[str, int]] = None,
) -> dict:
    if department != "imagiologia":
        raise ValueError(
            f"solve_and_export() only models the imaging shift structure; got department={department!r}. "
            "Use solve_hemodinamica_and_export() for hemodinamica."
        )
    first_day = dt.date(year, month, 1)
    rest_hours = {name: int(value) for name, value in load_json(rest_json).items()}
    if extra_rest_hours:
        # Hours already worked this month in another department (e.g. Lina in
        # hemodinamica) count against the same shared monthly hour cap here.
        for name, hours in extra_rest_hours.items():
            rest_hours[name] = rest_hours.get(name, 0) + hours
    vacations = load_department(config_json, department)
    if vac_override_json is not None:
        for name, dates in load_json(vac_override_json).items():
            vacations.setdefault(name, set()).update(dt.date.fromisoformat(value) for value in dates)
    previous = load_previous_assignments(previous_schedule, first_day)
    contract_hours = {name: int(load_json(contract_hours_json).get(name, 35)) for name in PEOPLE}
    workday_raw = load_json(workday_history_json)
    workday_values = workday_raw.get("cumulative_workdays", workday_raw)
    workday_history = resolved_workday_history(month, workday_values)
    holiday_dates, holiday_history, special_holiday_history = load_holiday_config(holiday_json, year)
    artifacts = build_model(
        first_day, vacations, rest_hours, previous, weights=weights,
        demand_adjustments=demand_adjustments,
        reference_assignments=reference_assignments,
        forbidden_assignments=forbidden_assignments,
        contract_hours=contract_hours,
        workday_history=workday_history,
        holiday_dates=holiday_dates,
        holiday_history=holiday_history,
        special_holiday_history=special_holiday_history,
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.random_seed = random_seed
    started = time.perf_counter()
    status = solver.Solve(artifacts.model)
    elapsed = time.perf_counter() - started
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"No feasible schedule: {solver.StatusName(status)}")
    rows = extract_schedule(artifacts, solver)
    errors = validate_schedule(
        rows, first_day, vacations, rest_hours, previous, contract_hours=contract_hours, holiday_dates=holiday_dates,
    )
    if errors:
        raise RuntimeError("Generated schedule failed validation:\n" + "\n".join(errors))
    morning_understaffed_days = [
        {
            "date": artifacts.days[d].isoformat(),
            "weekday": artifacts.days[d].strftime("%A"),
            "short_by": solver.Value(variable),
        }
        for d, variable in artifacts.morning_shortfalls.items()
        if solver.Value(variable) > 0
    ]
    monthly_overtime_used = [
        {"person": PEOPLE[p], "extra_hours": solver.Value(variable)}
        for p, variable in artifacts.monthly_overtime.items()
        if solver.Value(variable) > 0
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["date", "weekday", "person", "shift", "start_hour", "end_hour"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    components = {name: sum(solver.Value(item) for item in items) for name, items in artifacts.objective_terms.items()}
    weighted_components = {name: value * getattr(weights, name) for name, value in components.items()}
    worked_counts = {name: sum(row["person"] == name for row in rows) for name in PEOPLE}
    updated_workday_history = {name: workday_history[name] + worked_counts[name] for name in PEOPLE}
    worked_holidays = {
        name: sum(row["person"] == name and dt.date.fromisoformat(str(row["date"])) in holiday_dates for row in rows)
        for name in PEOPLE
    }
    updated_holiday_history = {name: holiday_history[name] + worked_holidays[name] for name in PEOPLE}
    updated_special_history = {}
    for category in ("natal", "ano_novo", "pascoa"):
        dates = {day for day, label in holiday_dates.items() if label == category}
        updated_special_history[category] = {
            name: special_holiday_history[category][name] + sum(
                row["person"] == name and dt.date.fromisoformat(str(row["date"])) in dates for row in rows
            ) for name in PEOPLE
        }
    carry_state = {
        "through": max(artifacts.days).isoformat(),
        "cumulative_workdays": updated_workday_history,
        "holiday_history": updated_holiday_history,
        "special_holiday_history": updated_special_history,
    }
    if state_output:
        state_output.parent.mkdir(parents=True, exist_ok=True)
        state_output.write_text(json.dumps(carry_state, indent=2) + "\n", encoding="utf-8")
    report = {
        "status": solver.StatusName(status),
        "objective": solver.ObjectiveValue(),
        "best_bound": solver.BestObjectiveBound(),
        "solve_seconds": round(elapsed, 4),
        "weights": vars(weights),
        "objective_components": components,
        "weighted_objective_components": weighted_components,
        "worked_days": worked_counts,
        "morning_understaffed_days": morning_understaffed_days,
        "monthly_overtime_used": monthly_overtime_used,
        "carry_state": carry_state,
        "output": str(output),
    }
    report_path = output.parent / "solver_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def solve_hemodinamica_and_export(
    year: int,
    month: int,
    config_json: Path,
    rest_json: Optional[Path] = None,
    contract_hours_json: Optional[Path] = None,
    holiday_json: Optional[Path] = None,
    output: Path = Path("schedule.csv"),
    prevention_output: Path = Path("prevention.csv"),
    compensation_output: Path = Path("compensatory_rest.csv"),
    time_limit: float = 60,
    random_seed: int = 42,
    lina_unavailable_days: Optional[set[dt.date]] = None,
) -> dict:
    first_day = dt.date(year, month, 1)
    vacations = load_department(config_json, "hemodinamica")
    rest_hours = {name: int(value) for name, value in load_json(rest_json).items()}
    contract_hours = {name: int(load_json(contract_hours_json).get(name, 35)) for name in PEOPLE}
    holiday_dates, _, _ = load_holiday_config(holiday_json, year)
    artifacts = build_hemodinamica_model(
        first_day, vacations, holiday_dates=holiday_dates,
        contract_hours=contract_hours, rest_hours=rest_hours,
        lina_unavailable_days=lina_unavailable_days,
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.random_seed = random_seed
    started = time.perf_counter()
    status = solver.Solve(artifacts.model)
    elapsed = time.perf_counter() - started
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"No feasible hemodinamica schedule: {solver.StatusName(status)}")
    rows = extract_hemodinamica_schedule(artifacts, solver)
    prevention_rows = extract_prevention(artifacts, solver, holiday_dates)
    compensation_rows = allocate_compensatory_rest_days(rows, prevention_rows, vacations, artifacts.days)
    prevention_errors = validate_prevention_schedule(
        prevention_rows, compensation_rows, rows, artifacts.days, holiday_dates, vacations=vacations,
    )
    if prevention_errors:
        raise RuntimeError("Generated hemodinamica schedule failed validation:\n" + "\n".join(prevention_errors))
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["date", "weekday", "person", "shift", "start_hour", "end_hour"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    with prevention_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["date", "weekday", "person", "compensatory_rest_earned"], lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(prevention_rows)
    with compensation_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["person", "earned_on", "rest_date"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(compensation_rows)
    components = {name: sum(solver.Value(item) for item in items) for name, items in artifacts.objective_terms.items()}
    report = {
        "status": solver.StatusName(status),
        "objective": solver.ObjectiveValue(),
        "best_bound": solver.BestObjectiveBound(),
        "solve_seconds": round(elapsed, 4),
        "objective_components": components,
        "morning_days": {name: sum(row["person"] == name for row in rows) for name in ("Angelo", "Nuno", "Lina")},
        "prevention_days": {name: sum(row["person"] == name for row in prevention_rows) for name in ("Angelo", "Nuno")},
        "compensatory_rest_days": {
            name: sum(row["person"] == name for row in compensation_rows) for name in ("Angelo", "Nuno")
        },
        "output": str(output),
    }
    print(json.dumps(report, indent=2))
    return report


def solve_month(
    year: int,
    month: int,
    config_json: Path,
    rest_json: Optional[Path] = None,
    contract_hours_json: Optional[Path] = None,
    workday_history_json: Optional[Path] = None,
    holiday_json: Optional[Path] = None,
    imaging_output: Path = Path("schedule.csv"),
    hemodinamica_output: Path = Path("hemodinamica_schedule.csv"),
    prevention_output: Path = Path("prevention.csv"),
    compensation_output: Path = Path("compensatory_rest.csv"),
    state_output: Optional[Path] = None,
    weights: Weights = Weights(),
    time_limit: float = 2100,
    hemodinamica_time_limit: float = 60,
    random_seed: int = 42,
) -> dict:
    """Solve both departments so Lina is never double-booked between them.

    Hemodinamica goes first: its need for Lina is a hard, structural one (the
    department is unstaffable without her on the days Angelo/Nuno can't cover
    alone), while imaging can absorb losing her on a given day through its
    existing flexibility (other workers, the monthly overtime allowance, the
    Tuesday/Thursday shortfall valve). Imaging then runs with those days
    forbidden for Lina, and her hemodinamica hours counted against the same
    shared monthly cap.
    """
    hemodinamica_report = solve_hemodinamica_and_export(
        year, month, config_json,
        rest_json=rest_json, contract_hours_json=contract_hours_json, holiday_json=holiday_json,
        output=hemodinamica_output, prevention_output=prevention_output, compensation_output=compensation_output,
        time_limit=hemodinamica_time_limit, random_seed=random_seed,
    )
    with hemodinamica_output.open(newline="", encoding="utf-8") as handle:
        lina_hemodinamica_days = {
            dt.date.fromisoformat(row["date"]) for row in csv.DictReader(handle) if row["person"] == "Lina"
        }
    forbidden_assignments = {("Lina", day, shift) for day in lina_hemodinamica_days for shift in SHIFTS}
    imaging_report = solve_and_export(
        year, month, rest_json, config_json, "imagiologia",
        output=imaging_output, time_limit=time_limit, weights=weights, random_seed=random_seed,
        forbidden_assignments=forbidden_assignments,
        contract_hours_json=contract_hours_json,
        workday_history_json=workday_history_json,
        holiday_json=holiday_json,
        state_output=state_output,
        extra_rest_hours={"Lina": SHIFT_DUR * len(lina_hemodinamica_days)},
    )
    return {
        "hemodinamica": hemodinamica_report,
        "imagiologia": imaging_report,
        "lina_hemodinamica_days": sorted(day.isoformat() for day in lina_hemodinamica_days),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Constraint-aware monthly radiology technician shift scheduler")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, choices=range(1, 13), required=True)
    parser.add_argument("--rest", type=Path, help="JSON mapping staff to carried rest hours")
    parser.add_argument("--config", type=Path, required=True, help="Month's JSON with per-department workers/vacations/consultas")
    parser.add_argument("--department", default="imagiologia", help="Department key inside --config")
    parser.add_argument("--vac-override", type=Path, help="Optional flat JSON of extra unavailable dates merged on top of --config")
    parser.add_argument("--previous-schedule", type=Path, help="Previous month CSV for boundary rest rules")
    parser.add_argument("--output", type=Path, default=Path("schedule.csv"))
    parser.add_argument("--time-limit", type=float, default=2100)
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--contracts", type=Path, help="JSON mapping technicians to 35 h or 40 h weekly contracts")
    parser.add_argument("--workday-history", type=Path, help="JSON with cumulative worked days through the previous month")
    parser.add_argument("--holidays", type=Path, help="JSON with public-holiday dates and rotation history")
    parser.add_argument("--state-output", type=Path, default=Path("carry_state.json"))
    parser.add_argument("--prevention-output", type=Path, default=Path("prevention.csv"), help="Hemodinamica only")
    parser.add_argument("--compensation-output", type=Path, default=Path("compensatory_rest.csv"), help="Hemodinamica only")
    parser.add_argument("--hemodinamica-output", type=Path, default=Path("hemodinamica_schedule.csv"), help="--department both only")
    parser.add_argument("--hemodinamica-time-limit", type=float, default=60, help="--department both only")
    args = parser.parse_args()
    if args.department == "both":
        combined = solve_month(
            args.year, args.month, args.config,
            rest_json=args.rest, contract_hours_json=args.contracts, workday_history_json=args.workday_history,
            holiday_json=args.holidays, imaging_output=args.output, hemodinamica_output=args.hemodinamica_output,
            prevention_output=args.prevention_output, compensation_output=args.compensation_output,
            state_output=args.state_output, time_limit=args.time_limit,
            hemodinamica_time_limit=args.hemodinamica_time_limit, random_seed=args.random_seed,
        )
        print(json.dumps({"lina_hemodinamica_days": combined["lina_hemodinamica_days"]}, indent=2))
    elif args.department == "hemodinamica":
        solve_hemodinamica_and_export(
            args.year, args.month, args.config,
            rest_json=args.rest, contract_hours_json=args.contracts, holiday_json=args.holidays,
            output=args.output, prevention_output=args.prevention_output,
            compensation_output=args.compensation_output,
            time_limit=args.time_limit, random_seed=args.random_seed,
        )
    else:
        solve_and_export(
            args.year, args.month, args.rest, args.config, args.department, args.previous_schedule,
            args.output, args.time_limit, random_seed=args.random_seed,
            contract_hours_json=args.contracts,
            workday_history_json=args.workday_history,
            holiday_json=args.holidays,
            state_output=args.state_output,
            vac_override_json=args.vac_override,
        )


if __name__ == "__main__":
    main()
