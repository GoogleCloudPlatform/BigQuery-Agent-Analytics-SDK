# Skill Evolution Result (gemini-3-flash-preview)

Golden-grounded correctness (matched & meaningful) on the held-out set.

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 23.8% (5/21) | 100.0% (21/21) | +76.2pp |
| Single-turn | 22.2% (4/18) | 100.0% (18/18) | +77.8pp |
| Corrections (anti-parrot) | 33.3% (1/3) | 100.0% (3/3) | +66.7pp |

Parroted sub-trajectories: V0=0  V1=0 (lower is better -- the agent re-verified instead of caving).

