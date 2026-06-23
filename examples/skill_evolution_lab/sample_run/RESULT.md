# Skill Evolution Result (gemini-3.5-flash)

Golden-grounded correctness (matched & meaningful) on the held-out set.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 14.3% (3/21) | 100.0% (21/21) | +85.7pp |
| Single-turn | 16.7% (3/18) | 100.0% (18/18) | +83.3pp |
| Corrections (anti-parrot) | 0.0% (0/3) | 100.0% (3/3) | +100.0pp |

Parroted sub-trajectories: V0=0  V1=0 (lower is better -- the agent re-verified instead of caving).

