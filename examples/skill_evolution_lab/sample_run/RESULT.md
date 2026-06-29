# Skill Evolution Result (gemini-3.5-flash)

Golden-grounded correctness (matched & meaningful) on the held-out set.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 18.2% (10/55) | 100.0% (55/55) | +81.8pp |
| Single-turn | 20.0% (10/50) | 100.0% (50/50) | +80.0pp |
| Corrections (anti-parrot) | 0.0% (0/5) | 100.0% (5/5) | +100.0pp |

Parroted sub-trajectories: V0=0  V1=0 (lower is better -- the agent re-verified instead of caving).
