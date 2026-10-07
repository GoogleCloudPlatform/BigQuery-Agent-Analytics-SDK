---
label: Recorded live run: TheLook analyst week
voice: Samantha
rate: 180
---

# Narration: the recorded analyst week

The voiceover of `demo.mp4`. `viz/record_demo.py --narration` speaks every
line below with macOS `say`, keeps each scene on screen until its lines end,
and writes the lines to `demo.srt` as captions, one per line. The scenes are
the ones `record_demo.py` plans for `viz/data/memory_export.json`, in order;
every number is from that export and `live_run.json`.

## intro
- Six analysts at an online clothing store shared one ADK data-analyst agent for a simulated week: 36 sessions, logged to BigQuery by the Agent Analytics plugin.
- Its memory is built only from those rows.

## compare-1
- On Monday, Maya asks: how did my categories do last month?
- Without memory, the agent reports on all 26 categories.
- With memory, it recalls her categories and her net revenue rule, and answers with one query.

## compare-2
- On Wednesday, Diego asks how his markets are trending.
- Without memory, he gets every country by quarter; with memory, Brazil and Spain, week by week.
- Three other runs without memory read the memory tables through SQL, so the page flags them.

## week
- Arcs link the selected session to the earlier sessions its recall drew on.

## graph
- Each night, AI.GENERATE extracts entities and facts from the day's messages, inside BigQuery.
- Each fact keeps the span of the message it came from.

## preferences
- Preferences are saved as ADK user state. When Raj changed his exchange rate, the old version stayed in the history.

## reasoning
- On Tuesday, Lena asked for the same lead-time check, this month so far.
- Recall returned her Thursday analysis with its SQL, and the agent adapted it to October.

## recall
- This is exactly what the agent read: preferences, facts and similar past analyses with their SQL, each with its source row.

## other-user
- Each analyst's memory is read with their own user filter, so nobody sees anyone else's rows.
