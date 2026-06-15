# Self-Evolving Agent Demo Narration

## 30-second version

This demo starts with a basketball analytics agent that answers correctly but
wastes work. It logs every run to BigQuery through the analytics
plugin. The SDK reads the traces, finds that the agent keeps calling a
broad reference tool and spending excess tokens, generates a tighter V2
prompt, reruns the same questions, and proves that quality stayed flat
while token and tool usage dropped.

## Walkthrough

1. Run `./setup.sh`.
2. Run `./run_e2e_demo.sh`.
3. Watch the V1 run call broad and narrow sample tools.
4. Watch `analyze_and_evolve.py` print the SDK-backed finding:
   broad reference lookups were used on narrow tasks.
5. Open `prompt_diff.md` to inspect the exact V1 -> generated V2 diff.
6. Watch the V2 run use narrow tools directly.
7. Open `comparison.md` for the final quality/token/tool diff.

## Demo Message

The important idea is not "save tokens" in isolation. The agent uses
its own production-shaped traces as feedback. Token tracking gives the
loop a measurable signal, but the goal is a self-evolving agent that
gets cheaper or cleaner without losing answer quality.
