# What the mod gives you

What a developer, a team lead and a platform team can learn from the rows
the mod writes, which query answers each question, who can run it, and what
the numbers cannot tell. The queries are in
[`sql/value_queries.sql`](../sql/value_queries.sql) (V1 to V7) beside the
README's [`sql/sample_queries.sql`](../sql/sample_queries.sql) (1 to 8);
[`tests/queries.live.py`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/tests/queries.live.py) runs every one of them
on a synthetic fixture and checks the answers.

## Who learns what

| Who | The question | Query | Who can run it | What it cannot tell |
| --- | --- | --- | --- | --- |
| Developer | Where did my sessions' time, tokens and tool failures go? | V1 (per session), V2 (per turn), sample 5 (per turn, with the prompt) | Someone who may read the table: you, in your own sandbox project; in a team, an administrator | In a team set up as the [service-account guide](service-account-auth.md) recommends, you can write rows and read none. Per-day totals by person are in the views (`daily_usage_by_user`), which a reader group may query |
| Team lead or administrator | Who uses it, against who was enrolled? | V3, with a roster | An administrator | Who did not use the agent and whose sessions were not recorded look the same: both have no rows. A day with no rows at all is listed, and flagged |
| Team lead or administrator | What does a session cost? | V4, with your rates | An administrator | An estimate: tokens times the rates you enter, dated. Not a bill. None for a session and model with a response that recorded no usage |
| Team lead or administrator | Which tools do people use, and how often do they fail, prompt, or get refused, by the person, a hook or a permission rule? | V5, sample 4 | An administrator | Each refused call is counted once, by where the refusal came from (`permission_decision_source`): the person, a hook, a rule, or another source (`refused_other`: one the host did not give, recorded as `unknown`, or one the queries do not name). The four add up to all refused calls (`refused_pct`, `refused`). A call that a rule blocks before the host records it leaves no row |
| Team lead or administrator | Are errors going up? | V6, sample 8 | An administrator | Each rate has its own denominator (turns, model requests, tool calls), as V6 shows them |
| Platform team | How do coding-agent sessions compare with our ADK agents? | V7 | An administrator of the shared table | The mod's rows beside ADK agents' only. The settings-hook tracer's are left out: it writes one response per turn and per subagent run, with their usage summed, so its model requests would count turns. Only sessions, model requests, tokens and error rows are compared; other columns differ by producer. An ADK model call counts once, with the largest token count among its responses, however it streamed and when one agent makes several calls at once. A call's tokens come from its own rows only, so a later final, which repeats them, is left out (`later_finals`). A call whose own rows recorded no usage is in `model_requests_without_usage`, and one whose request is not in the table counts by its span (`model_requests_unmatched`) |
| Anyone with the views | Sessions, model requests and tokens per day, by person or model | `daily_usage_by_user`, `daily_usage_by_model` | The reader group | No prompts, tools or errors: counts only |

## Denominators

- **Sessions**: distinct `session_id` among the mod's rows in the window,
  each row counted once by `event_id` (a resent row has the same one).
- **People**: `user_id`, the login name each machine reported. It is an
  attribution, not proof: a repository's settings can set `USER`, and
  `BQAA_USER_ID` is read from the environment. Under impersonation keep
  `userIdFromCredentials` off, or every row says the service account and
  everyone is one person.
- **The cohort**: a roster of the people enrolled (a `team_roster` table of
  login names that you keep, as the [managed-settings](managed-settings.md) pilot does). V3 counts
  each day's enrolled people seen and not seen, and keeps sessions from no
  one on the roster, or with no `user_id`, apart.
- **Errors**: per turn (turns that ended in `INVOCATION_ERROR`), per model
  request (`LLM_ERROR` among answered and failed requests) and per tool call
  (`TOOL_COMPLETED` with status `ERROR`), never one rate over all rows.
- **ADK model requests** (V7): one per model call. ADK writes an
  `LLM_RESPONSE` row for each chunk of a streamed response as well as the
  final one, on the call's own span. With progressive streaming off (it is
  on by default), it writes up to three final responses for a call that
  streams text and then a function call, the later ones under the agent's
  span. And one agent can run several calls at once (`parallel_worker`).
  V7 counts them this way:
  - a call is the responses on its `LLM_REQUEST`'s own span, with the
    largest token count among them, in error if any was;
  - a later final on the agent's span repeats its call's usage, and when
    several calls share that span the rows cannot always tell which call
    wrote it. So it counts in no call's requests or tokens, and as a row
    only when it reports an error (`later_finals` says how many were left
    out);
  - a call whose own rows recorded no usage adds no tokens
    (`model_requests_without_usage`);
  - a response with no request found is counted by its span, or alone
    (`model_requests_unmatched`).

  Requests are looked up from a day before the window, so a call that
  began just before it is still matched; only rows inside the window are
  counted. Each call is one request and one row of the error rows'
  denominator. An agent whose only rows in the window are later finals
  has none, so its `error_row_pct` is empty (NULL).
- **Missing is not zero**:
  - a person with no rows is "not seen";
  - a day with no rows at all is still listed, every enrolled person not
    seen, with `no_rows_that_day` set: absent telemetry, not proof that
    nobody worked;
  - a turn with no recorded end has no duration (NULL), and a turn's model
    or tool time is NULL when one of its requests or calls has no time: it
    recorded no duration, or its end is not in the table by the end of the
    window (it ended later, or the row was lost). A turn with no requests,
    or no tool calls, has 0 for that part, and a failed request's time
    counts as model time. V2 takes each turn with a row in the window
    whole, looking its rows up from a day before the window. A turn whose
    start row is not among them (it began more than a day before the
    window, the mod was loaded mid-turn, or the row was lost) has
    `whole_turn` false: its earlier calls may be missing, so its model and
    tool time and its permission prompts are NULL, and its `started_at`
    comes from its end row (NULL without one);
  - a response that recorded no usage adds no tokens, and is counted in
    `responses_without_usage`;
  - a session and model with such a response, or a model with no rate row,
    has no cost (NULL).

## Running a query

Each query reads the raw table, so it runs as someone who may read it, with
the analysis skill's runner, [`run_bq.py`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/main/bigquery-agent-analytics-skill/scripts/run_bq.py), from a checkout of
the mod's source repository. The runner uses your Application Default
Credentials (`gcloud auth application-default login`), not the mod's
settings, fills `{PROJECT}`, `{DATASET}` and `{TABLE}`, binds `@start` and
`@end`, and refuses a query that would bill more than `--max-gb`:

```bash
python3 bigquery-agent-analytics-skill/scripts/run_bq.py \
  --project my-gcp-project --dataset agent_analytics --table agent_events \
  --start 2026-10-01 --end 2026-10-31 --max-gb 1 --dry-run "<one query>"
```

`--dry-run` prints the bytes the query would scan; drop it to run it. The
table is partitioned by day, so the window decides the bytes; V2 and V7
also read the day before it, to find the starts of turns and calls that
began earlier. On the fixture each query scanned at most 15 KB. V3 also reads `team_roster` in the
same dataset; V4 needs your rates in its `rates` block.

## Cost, as an estimate

V4 multiplies each model's tokens by the rates you put in its `rates`
block: USD per million tokens for uncached input, cache reads, cache writes
and output, with the date and the source of the rates. Cache reads and
writes are priced apart from uncached input, as the tokens are recorded
apart. A model with no rate row shows no cost, and so does a session and
model with a response that recorded no usage (a partial sum would read as
the whole). Discounts or other charges are not counted. Keep the rates dated, and re-check them when they
change.

## A dashboard

The views in `<dataset>_reporting` can feed a dashboard tool directly
(Looker Studio, or the SDK's dashboards), for the reader group. A screenshot
for a write-up must come from synthetic or scrubbed data, with the data
owner's approval recorded: the table holds prompts, answers, tool payloads,
working directories and login names.

## A blurb

For docs, posts and partner write-ups, once the mod has a tagged release
(`mod-vX.Y.Z`); its link goes where the bracket is:

> BigQuery Agent Analytics for coding agents is an early-access mod
> (version 0.1.0) that writes each coding-agent session to a BigQuery table
> in the `agent_events` schema of ADK's BigQuery Agent Analytics: prompts,
> the model's answers, tool calls with their arguments and results, token
> counts and timings. Teams can see adoption against who was enrolled,
> estimated cost, and tool failures and denials, and can query their
> coding-agent sessions beside their ADK agents in one table. Because the
> rows hold prompt and tool content, its setup gives writers insert-only
> access and readers aggregate views. Install it from [the release].
