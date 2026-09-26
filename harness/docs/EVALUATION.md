# Evaluation

Global score weights:

| Dimension | Weight |
|---|---:|
| Issue understanding | 10 |
| Context relevance | 10 |
| Context completeness | 10 |
| Plan correctness | 10 |
| Implementation correctness | 20 |
| Test/verification evidence | 15 |
| Rule compliance | 10 |
| Regression safety | 5 |
| Efficiency | 5 |
| Recovery quality | 5 |

Thresholds: 90 verified, 80 review, 70 iterate, else re-plan.

Primary KPI: **verified issue resolution rate per 1M tokens**.

Fixture benchmark: `tests/fixtures/mini_repo` (wrong `add()`, unittest expects sum).
