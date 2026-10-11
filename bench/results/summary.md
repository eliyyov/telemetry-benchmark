# Vector vs OpenTelemetry Collector - run summary

runs in runs.csv: 4

## Per level and arm (valid + lower-bound runs)

| level | arm | n | CPU mean % | CPU median % | mem peak MiB (mean) | offered eps | consumed eps |
|---|---|---|---|---|---|---|---|
| normal | otel | 2 | 1.34 | 1.34 | 54.7 | 5056 | 5000 |
| normal | vector | 2 | 5.58 | 5.58 | 18.8 | 5058 | 4998 |

## Ratios and exact Mann-Whitney U (ratio = otel/vector, two-sided, per level)

| level | CPU ratio | CPU test | mem ratio | mem test |
|---|---|---|---|---|
| normal | 0.24x | U test p = 0.3333 (n=2 vs 2) | 2.91x | U test p = 0.3333 (n=2 vs 2) |

## V5/V6 cross-check: cgroup sampler vs cAdvisor (tolerance 5%)

| arm | level | rep | CPU diff | mem diff | verdict |
|---|---|---|---|---|---|
| otel | normal | run1 | 2.3% | 0.3% | pass |
| otel | normal | run2 | 0.0% | 2.9% | pass |
| vector | normal | run1 | 4.8% | 1.0% | pass |
| vector | normal | run2 | 0.0% | 5.4% | FAIL (repeat <=2x, then exclude) |

## Validity (V1 offered rate, V3 >= 0.98 consumed, V4 saturated, V10 no throttle, V11 no OOM)

- normal/otel: n=2  V1 2  V3 2  saturated(V4) 0  V10 2  V11 2
- normal/vector: n=2  V1 2  V3 2  saturated(V4) 0  V10 2  V11 2

excluded runs: 0

cross-check failures: 1
