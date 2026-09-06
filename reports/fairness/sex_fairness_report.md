# Sex-based schedule fairness audit

This is a descriptive outcome audit. It does not classify the schedule as fair, discriminatory, or legally compliant.

## Scope and definitions

Sensitive attribute: explicit `sex` labels. Groups found: F, M.

Static opportunity: calendar day is not blocked by vacation, consultation, sick leave, or a linked Hemodynamics assignment, and solver.allowed() permits the relevant shift. Dynamic rest, spacing, prior assignments, and global staffing interactions are not reconstructed as counterfactual eligibility.

Raw undesirable-shift rates divide each worker's assignments by that worker's total shifts. Group values are means of worker rates. The burden ratio is female rate divided by male rate; 1 denotes parity. Undefined ratios are reported as blank/null.

Weighted mean of available per-worker night, weekend, and holiday shift rates; default weights are neutral and configurable.

Worker-specific constraint satisfaction is not reported as a separate score: Worker-specific availability and eligibility are encoded as hard constraints; this outcome audit does not define a separate satisfaction score for them.

## Current schedule results

| Metric | Female | Male | Difference F-M | Absolute gap | Ratio F/M | Greater burden |
|---|---:|---:|---:|---:|---:|:---:|
| night shift rate | 0.0881 | 0.1123 | -0.0242 | 0.0242 | 0.7845 | M |
| weekend shift rate | 0.1727 | 0.1565 | 0.0162 | 0.0162 | 1.1035 | F |
| holiday shift rate | — | — | — | — | — | — |
| worker specific constraint satisfaction | — | — | — | — | — | — |
| eligible night assignment rate | 0.1112 | 0.1011 | 0.0101 | 0.0101 | 1.0999 | F |
| eligible weekend assignment rate | 0.4757 | 0.5347 | -0.0590 | 0.0590 | 0.8897 | M |
| eligible holiday assignment rate | — | — | — | — | — | — |
| overall burden score | 0.1304 | 0.1344 | -0.0040 | 0.0040 | 0.9702 | M |

Group sample sizes (roster / workers with assigned shifts): F=14 / 12, M=7 / 7. Workers with zero assigned shifts are excluded from shift-rate means.

## Schedule variants

Closer to zero is parity for gaps; closer to one is parity for ratios.

| Variant | Metric | Signed gap F-M | Absolute gap | Ratio F/M | Gap change from baseline |
|---|---|---:|---:|---:|---:|
| operational_baseline | night shift rate | 0.0043 | 0.0043 | 1.0440 | 0.0000 |
| operational_baseline | weekend shift rate | 0.0397 | 0.0397 | 1.2844 | 0.0000 |
| operational_baseline | holiday shift rate | — | — | — | — |
| operational_baseline | worker specific constraint satisfaction | — | — | — | — |
| operational_baseline | eligible night assignment rate | 0.0389 | 0.0389 | 1.4376 | 0.0000 |
| operational_baseline | eligible weekend assignment rate | 0.0069 | 0.0069 | 1.0134 | 0.0000 |
| operational_baseline | eligible holiday assignment rate | — | — | — | — |
| operational_baseline | overall burden score | 0.0220 | 0.0220 | 1.1853 | 0.0000 |
| preference_focused | night shift rate | -0.0147 | 0.0147 | 0.8698 | 0.0104 |
| preference_focused | weekend shift rate | 0.0365 | 0.0365 | 1.2369 | -0.0032 |
| preference_focused | holiday shift rate | — | — | — | — |
| preference_focused | worker specific constraint satisfaction | — | — | — | — |
| preference_focused | eligible night assignment rate | 0.0175 | 0.0175 | 1.1736 | -0.0214 |
| preference_focused | eligible weekend assignment rate | -0.0903 | 0.0903 | 0.8505 | 0.0834 |
| preference_focused | eligible holiday assignment rate | — | — | — | — |
| preference_focused | overall burden score | 0.0109 | 0.0109 | 1.0816 | -0.0111 |
| weekend_focused | night shift rate | -0.0069 | 0.0069 | 0.9345 | 0.0026 |
| weekend_focused | weekend shift rate | 0.0700 | 0.0700 | 1.5486 | 0.0303 |
| weekend_focused | holiday shift rate | — | — | — | — |
| weekend_focused | worker specific constraint satisfaction | — | — | — | — |
| weekend_focused | eligible night assignment rate | 0.0287 | 0.0287 | 1.3040 | -0.0102 |
| weekend_focused | eligible weekend assignment rate | 0.0799 | 0.0799 | 1.1692 | 0.0730 |
| weekend_focused | eligible holiday assignment rate | — | — | — | — |
| weekend_focused | overall burden score | 0.0316 | 0.0316 | 1.2712 | 0.0096 |
| workload_focused | night shift rate | -0.0069 | 0.0069 | 0.9345 | 0.0026 |
| workload_focused | weekend shift rate | 0.0497 | 0.0497 | 1.3478 | 0.0100 |
| workload_focused | holiday shift rate | — | — | — | — |
| workload_focused | worker specific constraint satisfaction | — | — | — | — |
| workload_focused | eligible night assignment rate | 0.0267 | 0.0267 | 1.2770 | -0.0122 |
| workload_focused | eligible weekend assignment rate | 0.0000 | 0.0000 | 1.0000 | -0.0069 |
| workload_focused | eligible holiday assignment rate | — | — | — | — |
| workload_focused | overall burden score | 0.0215 | 0.0215 | 1.1732 | -0.0005 |
| published_schedule | night shift rate | -0.0242 | 0.0242 | 0.7845 | 0.0199 |
| published_schedule | weekend shift rate | 0.0162 | 0.0162 | 1.1035 | -0.0235 |
| published_schedule | holiday shift rate | — | — | — | — |
| published_schedule | worker specific constraint satisfaction | — | — | — | — |
| published_schedule | eligible night assignment rate | 0.0101 | 0.0101 | 1.0999 | -0.0288 |
| published_schedule | eligible weekend assignment rate | -0.0590 | 0.0590 | 0.8897 | 0.0521 |
| published_schedule | eligible holiday assignment rate | — | — | — | — |
| published_schedule | overall burden score | -0.0040 | 0.0040 | 0.9702 | -0.0180 |

## Limitations

Results cover one month, are descriptive, and include no significance test. Static eligibility does not reconstruct schedule-dependent rest and spacing constraints. Holiday metrics are unavailable when no holiday dates are configured. Longitudinal data and worker feedback would be required for stronger conclusions.
