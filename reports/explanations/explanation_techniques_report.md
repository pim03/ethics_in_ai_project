# Explainability: what the system does and what we learned

## Main Conclusions

The system gives evidence that a person could legally work a shift, shows direct reasons why another person could not work it, and reruns the schedule to test alternatives.

The explanations help a human inspect and challenge a schedule, but they do not prove that every assignment was the only or best possible choice.

## Tests made

| test | cases checked | cases with a direct answer | percentage | time (seconds) |
|---|---:|---:|---:|---:|
| Explain a scheduled shift | 340 | 340 | 100.00% | 0.0036 |
| Explain why a worker was not given a shift | 2158 | 2015 | 93.37% | 0.0086 |
| Check whether enough workers were available | 90 | 90 | 100.00% | 0.0003 |
| Explain changes after a disruption | 6 | 6 | 100.00% | 0.0027 |
| Test what happens when one assignment is forbidden | 1 | 1 | 100.00% | 0.2745 |
## The five checks

1. **Scheduled shifts:** For each assignment, we show coverage, eligibility, availability, hours, and relevant rest rules. It means that the assignment is generally valid according to the defined constraints.

2. **Why not this worker?:** We look for a direct reason, such as vacation, lack of eligibility, another shift on the same day, a rest rule, or the weekly hour limit. In some cases there is no single blocking rule; the worker was simply not selected by the overall optimization.

3. **Available staff:** We compare the number of available and eligible workers with the minimum staffing requirement. This is a basic warning check.

4. **Schedule repairs:** After each simulated disruption, we list exactly which assignments were removed and added.

5. **What-if test:** We forbid one assignment, run the solver again, and measure how much of the schedule must change.

## Example: explaining a scheduled shift

The schedule gives Ana Catarina shift M on 2026-09-01.

The report checks the staffing level, whether the worker was allowed and available, their hours, and any relevant rest rules.

Meaning: the assignment follows the rules represented in the program. It does not prove that this worker was the only possible choice.

## Example: why another worker was not selected

Why not Telmo on 2026-09-03 shift M?

- Worker has a night shift on the previous day.
- The published schedule already reaches the weekly hour cap.

## What-if result

What changes if Leonardo cannot work night on 2026-09-10?

The solver found another valid schedule.

Changing this one assignment led to 4 assignment changes involving 2 workers.

Therefore, the original assignment was not strictly necessary, but replacing it had consequences for several people.

Using the published objective weights, the score changed from 23227000 to 23228550 (+1550). Lower is better, so the alternative worsened the objective score.

The changed objective components were:

- isolated rest day: +4 units, contributing +800 points

- isolated work day: +2 units, contributing +600 points

- night ramp mismatch: +1 units, contributing +150 points

## Why were some mornings below the preferred target?

Six morning workers is the mandatory minimum. Ten is the preferred target. Therefore, the following gaps are not violations of the mandatory rule.

The published schedule has a total gap of 24 worker-slots. When the solver ignores all other preferences and minimizes only this gap, the proven minimum is 17. The schedule returned by that diagnostic uses 384 overtime hours, but this is not a proven minimum overtime requirement because the diagnostic does not minimize overtime. Therefore, some gap is unavoidable under the encoded hard constraints, while part of the published gap comes from trading staffing against overtime and other schedule goals.

### 2026-09-01 (Tuesday)

The schedule has 6 morning workers: 4 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-03 (Thursday)

The schedule has 8 morning workers: 2 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-08 (Tuesday)

The schedule has 6 morning workers: 4 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-09 (Wednesday)

The schedule has 9 morning workers: 1 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-10 (Thursday)

The schedule has 6 morning workers: 4 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-11 (Friday)

The schedule has 9 morning workers: 1 below the preferred target of 10.

The solver proved that 10 morning workers are impossible under the encoded constraints.

### 2026-09-15 (Tuesday)

The schedule has 6 morning workers: 4 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-17 (Thursday)

The schedule has 8 morning workers: 2 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

### 2026-09-22 (Tuesday)

The schedule has 8 morning workers: 2 below the preferred target of 10.

A valid alternative with 10 morning workers was found. The original gap was therefore not required by the hard constraints.

## Conclusions

- We can show whether scheduled shifts follow the rules represented in the program.

- For most rejected alternatives, we can show a direct blocking reason.

- We can show the exact changes caused by disruptions and by the tested what-if question.

- We cannot claim that an assignment was the only possible choice or explain every trade-off made by the solver.

- We have not yet tested these explanations with hospital schedulers or workers, so we cannot claim that users find them clear or useful.
