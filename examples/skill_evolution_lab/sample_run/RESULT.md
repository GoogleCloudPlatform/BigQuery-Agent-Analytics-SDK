# Skill Evolution Result (gemini-3.5-flash)

Golden-grounded correctness (matched & meaningful) on the held-out set.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 18.6% (13/70) | 98.6% (69/70) | +80.0pp |
| Single-turn | 16.4% (9/55) | 98.2% (54/55) | +81.8pp |
| Corrections (anti-parrot) | 26.7% (4/15) | 100.0% (15/15) | +73.3pp |

Parroted sub-trajectories: V0=0  V1=0 (lower is better -- the agent re-verified instead of caving).

## Quality dimensions (average 0-2, held-out set)

| Dimension | V0 | V1 | Delta |
| --- | --- | --- | --- |
| Correctness | 0.38 | 2.00 | +1.62 |
| Tool use | 0.38 | 2.00 | +1.62 |
| Specificity | 0.38 | 2.00 | +1.62 |
| Scope compliance | 0.38 | 2.00 | +1.62 |
| First-time-right | 0.38 | 2.00 | +1.62 |
