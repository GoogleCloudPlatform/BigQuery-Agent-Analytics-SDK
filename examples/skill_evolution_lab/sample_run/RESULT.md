# Skill Evolution Result (gemini-3.5-flash)

Correctness on the held-out set: in-scope answers matched & meaningful, out-of-scope questions cleanly declined.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 67.5% (54/80) | 97.5% (78/80) | +30.0pp |
| Single-turn | 80.0% (44/55) | 98.2% (54/55) | +18.2pp |
| Corrections (anti-parrot) | 0.0% (0/15) | 100.0% (15/15) | +100.0pp |
| Out-of-scope (declined) | 100.0% (10/10) | 90.0% (9/10) | -10.0pp |

Parroted sub-trajectories: V0=15  V1=0 (lower is better -- the agent re-verified instead of caving).

## Tool selection (sessions that called each tool, held-out set)

| Behavior | V0 | V1 |
| --- | --- | --- |
| Called any tool | 56/80 | 75/80 |
| `calculate_disability_pay` | 5/80 | 5/80 |
| `lookup_company_policy` | 51/80 | 70/80 |

## Quality dimensions (average 0-2, held-out set)

| Dimension | V0 | V1 | Delta |
| --- | --- | --- | --- |
| Correctness | 1.34 | 1.98 | +0.64 |
| Tool use | 1.35 | 1.95 | +0.6 |
| Specificity | 1.70 | 1.98 | +0.28 |
| Scope compliance | 1.62 | 1.98 | +0.36 |
| First-time-right | 1.61 | 1.98 | +0.37 |
