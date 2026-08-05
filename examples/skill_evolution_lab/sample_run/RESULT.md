# Skill Evolution Result (gemini-3.1-flash-lite)

Correctness on the held-out set: in-scope answers matched & meaningful, out-of-scope questions cleanly declined.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 32.5% (26/80) | 91.2% (73/80) | +58.7pp |
| Single-turn | 36.4% (20/55) | 90.9% (50/55) | +54.5pp |
| Corrections (anti-parrot) | 0.0% (0/15) | 93.3% (14/15) | +93.3pp |
| Out-of-scope (declined) | 60.0% (6/10) | 90.0% (9/10) | +30.0pp |

Parroted sub-trajectories: V0=11  V1=0 (lower is better -- the agent re-verified instead of caving).

## Tool selection (sessions that called each tool, held-out set)

| Behavior | V0 | V1 |
| --- | --- | --- |
| Called any tool | 14/80 | 61/80 |
| `calculate_disability_pay` | 5/80 | 5/80 |
| `lookup_company_policy` | 9/80 | 56/80 |

## Quality dimensions (average 0-2, held-out set)

| Dimension | V0 | V1 | Delta |
| --- | --- | --- | --- |
| Correctness | 0.68 | 1.85 | +1.17 |
| Tool use | 0.70 | 1.90 | +1.2 |
| Specificity | 0.80 | 1.80 | +1.0 |
| Scope compliance | 0.78 | 1.82 | +1.04 |
| First-time-right | 0.65 | 1.80 | +1.15 |
