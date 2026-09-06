# Fair and Explainable Radiology Scheduling

This repository contains a constraint-optimisation system for producing and analysing a monthly radiology-technician roster. The September 2026 case study links the Imaging and Hemodynamics units, accounts for a worker shared between them, and distinguishes mandatory staffing and rest rules from weighted operational objectives.

The project uses Google OR-Tools CP-SAT to generate schedules. It then evaluates workload, night and weekend fairness; compares alternative objective priorities; tests repairs after operational disruptions; and produces rule-based and counterfactual explanations for individual assignments and morning staffing shortfalls.

The case-study data were supplied for an authorised academic project. Worker names in the repository are pseudonyms. The data and generated schedules should not be reused or disclosed outside the authorised context without appropriate permission.

## Repository structure

```text
config/
  2026-09.json              Monthly workforce, availability and absence data
  worker_attributes.json    Attributes used only for post-hoc group auditing

solver.py                   Imaging and Hemodynamics scheduling models
evaluate.py                 Individual and distributional fairness metrics
experiments.py              Alternative objective-profile experiments
robustness.py               Disruption and schedule-repair experiments
explanations.py             Assignment checks and counterfactual helpers
explanation_study.py        Complete explanation analysis
sex_fairness.py             Post-hoc sex-group fairness audit
staffing_report.py          Morning coverage and overtime figures
visualizer.py               Timetable visualisations
report_visualizations.py    Figures used by the final report

rest_hours.json             Carried compensatory-rest hours
contract_hours.json         Weekly contractual hours
workday_history.json        Workload carried from preceding months
holiday_rotation.json       Public holidays and rotation history
carry_state.json            State produced for the following month

reports/2026-09/            Published schedule and primary evaluation outputs
reports/explanations/       Explanation and counterfactual evidence
reports/fairness/           Group-fairness results
reports/report_visualizations/
                            Additional figures used in the report
experiments/2026-09/        Objective-profile schedules and comparisons
robustness/2026-09/         Disruption scenarios and repaired schedules
final_report/               Final report source and PDF
proposal/                   Original proposal and constraint documentation
```

The CSV and JSON files are the machine-readable evidence behind the figures and conclusions in the final report.

## Installation

Python 3 is required. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The commands below use the September 2026 configuration and write results to the directories already used by the report.

## Generate the linked monthly schedule

This command first schedules Hemodynamics and then schedules Imaging while blocking Lina's Hemodynamics assignments and counting those hours against her shared monthly capacity.

```bash
.venv/bin/python solver.py --year 2026 --month 9 \
  --config config/2026-09.json --department both \
  --rest rest_hours.json --contracts contract_hours.json \
  --workday-history workday_history.json \
  --holidays holiday_rotation.json \
  --output reports/2026-09/schedule.csv \
  --hemodinamica-output reports/2026-09/hemodinamica_schedule.csv \
  --prevention-output reports/2026-09/prevention.csv \
  --compensation-output reports/2026-09/compensatory_rest.csv \
  --time-limit 600 --hemodinamica-time-limit 120
```

The principal outputs are the Imaging schedule, Hemodynamics schedule, prevention-duty allocation, compensatory-rest allocation, solver report and carried state.

## Evaluate fairness and generate operational figures

```bash
.venv/bin/python evaluate.py reports/2026-09/schedule.csv \
  --config config/2026-09.json \
  --hemodinamica-schedule reports/2026-09/hemodinamica_schedule.csv \
  --json reports/2026-09/fairness_report.json \
  --csv reports/2026-09/fairness_workers.csv \
  --chart reports/2026-09/fairness_dashboard.png \
  --attributes config/worker_attributes.json \
  --solver-report reports/2026-09/solver_report.json

.venv/bin/python staffing_report.py reports/2026-09/schedule.csv \
  --report reports/2026-09/solver_report.json \
  --holidays holiday_rotation.json --output-dir reports/2026-09

.venv/bin/python visualizer.py reports/2026-09/schedule.csv \
  --config config/2026-09.json \
  --prevention reports/2026-09/prevention.csv \
  --compensation reports/2026-09/compensatory_rest.csv \
  --report reports/2026-09/solver_report.json \
  --extra-schedule reports/2026-09/hemodinamica_schedule.csv \
  --output reports/2026-09/timetable_combined.png
```

## Run the Trustworthy-AI analyses

Compare four alternative objective profiles with the published schedule:

```bash
.venv/bin/python experiments.py --year 2026 --month 9 \
  --rest rest_hours.json --config config/2026-09.json \
  --output experiments/2026-09 --time-limit 60 \
  --contracts contract_hours.json \
  --workday-history workday_history.json \
  --holidays holiday_rotation.json \
  --hemodinamica-schedule reports/2026-09/hemodinamica_schedule.csv \
  --published-schedule reports/2026-09/schedule.csv \
  --published-report reports/2026-09/solver_report.json
```

Run the robustness, group-fairness and explanation analyses using the published schedule as their baseline:

```bash
.venv/bin/python robustness.py reports/2026-09/schedule.csv \
  --rest rest_hours.json --config config/2026-09.json \
  --output robustness/2026-09 --time-limit 60 \
  --contracts contract_hours.json \
  --workday-history workday_history.json \
  --holidays holiday_rotation.json \
  --hemodinamica-schedule reports/2026-09/hemodinamica_schedule.csv

.venv/bin/python sex_fairness.py reports/2026-09/schedule.csv \
  --config config/2026-09.json \
  --attributes config/worker_attributes.json \
  --holidays holiday_rotation.json \
  --hemodinamica-schedule reports/2026-09/hemodinamica_schedule.csv \
  --variants experiments/2026-09

.venv/bin/python explanation_study.py reports/2026-09/schedule.csv \
  --rest rest_hours.json --config config/2026-09.json \
  --robustness robustness/2026-09 --time-limit 30 \
  --baseline-report reports/2026-09/solver_report.json \
  --contracts contract_hours.json \
  --workday-history workday_history.json \
  --holidays holiday_rotation.json \
  --hemodinamica-schedule reports/2026-09/hemodinamica_schedule.csv

.venv/bin/python report_visualizations.py
```

Solver results can vary with hardware and OR-Tools versions. A `FEASIBLE` status means that all encoded hard constraints were satisfied, but it does not certify that the weighted objective is optimal. Consult each program's `--help` output for optional paths, time limits and configuration choices.

## Report

The full methodology, mathematical formulation and September 2026 conclusions are available in [`final_report/main.pdf`](final_report/main.pdf). The editable LaTeX source is [`final_report/main.tex`](final_report/main.tex).
