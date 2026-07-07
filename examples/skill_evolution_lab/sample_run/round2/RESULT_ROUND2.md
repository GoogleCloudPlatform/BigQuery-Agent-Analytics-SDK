# Skill Evolution Result (gemini-3.5-flash)

Correctness on the held-out set: in-scope answers matched & meaningful, out-of-scope questions cleanly declined.

| Metric | V1 (evolved) | V2 (round 2) | Delta |
| --- | --- | --- | --- |
| Overall | 97.5% (78/80) | 97.5% (78/80) | +0.0pp |
| Single-turn | 98.2% (54/55) | 100.0% (55/55) | +1.8pp |
| Corrections (anti-parrot) | 100.0% (15/15) | 100.0% (15/15) | +0.0pp |
| Out-of-scope (declined) | 90.0% (9/10) | 80.0% (8/10) | -10.0pp |

Parroted sub-trajectories: V1=0  V2=0 (lower is better -- the agent re-verified instead of caving).

## Tool selection (sessions that called each tool, held-out set)

| Behavior | V1 | V2 |
| --- | --- | --- |
| Called any tool | 75/80 | 76/80 |
| `calculate_disability_pay` | 5/80 | 5/80 |
| `get_current_date` | 0/80 | 1/80 |
| `lookup_company_policy` | 70/80 | 71/80 |

## Quality dimensions (average 0-2, held-out set)

| Dimension | V1 | V2 | Delta |
| --- | --- | --- | --- |
| Correctness | 1.98 | 1.99 | +0.01 |
| Tool use | 1.95 | 2.00 | +0.05 |
| Specificity | 1.98 | 2.00 | +0.02 |
| Scope compliance | 1.98 | 2.00 | +0.02 |
| First-time-right | 1.98 | 1.99 | +0.01 |
