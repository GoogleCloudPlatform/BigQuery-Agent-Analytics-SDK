# Skill Evolution Result (gemini-3.1-flash-lite)

Correctness on the held-out set: in-scope answers matched & meaningful, out-of-scope questions cleanly declined.

| Metric | V1 (evolved) | V2 (round 2) | Delta |
| --- | --- | --- | --- |
| Overall | 98.8% (79/80) | 93.8% (75/80) | -5.0pp |
| Single-turn | 98.2% (54/55) | 100.0% (55/55) | +1.8pp |
| Corrections (anti-parrot) | 100.0% (15/15) | 80.0% (12/15) | -20.0pp |
| Out-of-scope (declined) | 100.0% (10/10) | 80.0% (8/10) | -20.0pp |

Parroted sub-trajectories: V1=0  V2=0 (lower is better -- the agent re-verified instead of caving).

## Tool selection (sessions that called each tool, held-out set)

| Behavior | V1 | V2 |
| --- | --- | --- |
| Called any tool | 61/80 | 70/80 |
| `calculate_disability_pay` | 5/80 | 5/80 |
| `lookup_company_policy` | 56/80 | 65/80 |

## Quality dimensions (average 0-2, held-out set)

| Dimension | V1 | V2 | Delta |
| --- | --- | --- | --- |
| Correctness | 1.98 | 1.90 | -0.08 |
| Tool use | 1.99 | 1.95 | -0.04 |
| Specificity | 1.98 | 1.95 | -0.03 |
| Scope compliance | 1.98 | 1.95 | -0.03 |
| First-time-right | 1.95 | 1.90 | -0.05 |
