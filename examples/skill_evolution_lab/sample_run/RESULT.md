# Skill Evolution Result (gemini-3.5-flash)

Golden-grounded correctness (matched & meaningful) on the held-out set.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 21.5% (14/65) | 100.0% (65/65) | +78.5pp |
| Single-turn | 18.0% (9/50) | 100.0% (50/50) | +82.0pp |
| Corrections (anti-parrot) | 33.3% (5/15) | 100.0% (15/15) | +66.7pp |

Parroted sub-trajectories: V0=0  V1=0 (lower is better -- the agent re-verified instead of caving).
