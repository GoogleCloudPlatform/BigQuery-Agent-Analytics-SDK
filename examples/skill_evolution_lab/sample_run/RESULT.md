# Skill Evolution Result (gemini-3.1-flash-lite)

Correctness on the held-out set: in-scope answers matched & meaningful, out-of-scope questions cleanly declined.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 36.2% (29/80) | 97.5% (78/80) | +61.3pp |
| Single-turn | 34.5% (19/55) | 100.0% (55/55) | +65.5pp |
| Corrections (anti-parrot) | 0.0% (0/15) | 100.0% (15/15) | +100.0pp |
| Out-of-scope (declined) | 100.0% (10/10) | 80.0% (8/10) | -20.0pp |

Parroted sub-trajectories: V0=11  V1=0 (lower is better -- the agent re-verified instead of caving).

## Tool selection (sessions that called each tool, held-out set)

| Behavior | V0 | V1 |
| --- | --- | --- |
| Called any tool | 14/80 | 62/80 |
| `calculate_disability_pay` | 5/80 | 5/80 |
| `lookup_company_policy` | 9/80 | 57/80 |

## Quality dimensions (average 0-2, held-out set)

| Dimension | V0 | V1 | Delta |
| --- | --- | --- | --- |
| Correctness | 0.73 | 1.99 | +1.26 |
| Tool use | 0.75 | 2.00 | +1.25 |
| Specificity | 0.94 | 2.00 | +1.06 |
| Scope compliance | 0.84 | 2.00 | +1.16 |
| First-time-right | 0.84 | 1.96 | +1.12 |
