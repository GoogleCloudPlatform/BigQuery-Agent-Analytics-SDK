# BigQuery Agent Analytics: a coding-agent mod

A coding-agent **mod** (a plugin whose code is a JavaScript/TypeScript hooks
module that runs inside the host coding-agent CLI; see the
[mod docs](https://code.claude.com/docs/en/plugins/mods/overview)) that
records each session's turns,
model requests, tool calls and subagents as rows in the
[BigQuery Agent Analytics](https://adk.dev/integrations/bigquery-agent-analytics/)
(BQAA) `agent_events` schema, and streams them to BigQuery from the
background.

It produces the same table shape as ADK's BQAA plugin and this repo's
settings-hook plugin, [`bigquery-agent-analytics-tracing`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/plugins/claude_code),
so the [analysis skill](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/tree/main/bigquery-agent-analytics-skill) and its queries work on what it writes.

Status: early. Mods are an early-access feature of the host CLI (v2.1.287 or
later); this mod was built against **v2.1.296** and is tested with 2.1.296,
2.1.299, 2.1.302 and 2.1.304. Its writer is tested against a scripted BigQuery API (see
[Testing](#testing)). It has also run live: a real session (host 2.1.299)
wrote its 14 rows to a test dataset, the last of them through a spill file
that the next session replayed, and the rows were read back with `bq`. On
2.1.302, installed from GitHub, sessions with tool calls, a subagent and a
permission prompt were read back the same way, as was a session started
before its dataset existed: its rows waited, then landed on their own once
`setup.sh` had made the dataset.

**New here?** [QUICKSTART.md](QUICKSTART.md) has three short paths: your own
sandbox, joining what your team set up, and the administrator's steps.

## Why a mod, and not settings hooks

The tracing plugin runs a shell script for each settings-hook event. Those
hooks are foreground processes: the host CLI waits for each one, and PR #6 on
this repo ("Make tracing hooks non-blocking and fail-open", open as of this
writing) reworks them because they could hang the CLI. A mod runs inside the
host CLI, which lets this one keep a hard rule:

* Each capture hook (prompt, turn, model request, tool and subagent events)
  calls `next(e)`, records what it saw into an in-memory queue (synchronous,
  bounded work), and returns what `next` gave it, unchanged. The one other
  call a hook waits on is the host's `$.session.id()`, read for each event
  (no network, no process; see Ids and spans).
* No hook waits on the network or a process; a background writer driven by
  `$.clock` timers does that. Two lifecycle hooks do bounded work of their own
  before passing their event on: `session.start` sets the writer up and
  registers the three commands, and `session.end` writes what is still queued
  to a spill file, waiting at most `BQAA_EXIT_FLUSH_MS` (800 ms), inside the
  host's end-of-session bound, for that write and for any dead-letter file
  still being written. Those writes are the only wait on the disk.
* If the mod's own code throws, the event goes on as if the mod were not
  installed (fail open). The six observers the validator lists as gating
  hooks (`classic.SessionStart`, `classic.SubagentStart`,
  `classic.PermissionRequest`, `prompt.submit`, `telemetry.log`,
  `tool.call`) carry a `.catch(($, e, next) => next(e))`; the other hooks
  run their bookkeeping inside `try` blocks, so it cannot throw into the
  event.
* `prompt.submit` passes every prompt on, first and as it came, whatever
  the credentials do: no prompt waits on a token or on BigQuery. While rows
  cannot be written, a notice says so (see
  [If rows cannot be written](#if-rows-cannot-be-written)).

The test suite holds the mod to that: a test makes BigQuery hang forever and
then runs 25 tool calls through the mod, and a test checks that no hook,
`session.end` included, calls `$.http.fetch` or `$.process.run`. Breaking the
rule on purpose (awaiting the flush inside the `tool.call` hook, or a network
flush in `session.end`) makes those tests fail; the hang test times out, which
is what a person would have seen as the CLI hanging.

## How it works

```
 host CLI (engine)                             bigquery-agent-analytics-mod
 ───────────────────                          ───────────────────────────────────────────────
 classic.SessionStart ─┐
 prompt.submit ────────┤   hook: next(e),             Tracer: event -> BQAA row
 turn.start ───────────┤   then record (sync)  ─────▶ (ids, spans, redaction,
 turn.step (stream) ───┤                              truncation, is_truncated)
 tool.call ────────────┤                                     │ enqueue (sync)
 classic.SubagentStart ┤                                     ▼
 turn.complete ────────┤                          ┌──────────────────────────────┐
 session.end ──────────┘                          │ bounded queue (rows + bytes, │
                                                  │ drops oldest when full)      │
                                                  └──────────────┬───────────────┘
 $.clock.every(flushIntervalMs) ─────────────────▶ Writer (background, one at a time)
                                                         │ token: $.process.run(gcloud …), cached
                                                         │ batch <= 500 rows / 3 MiB (UTF-8)
                                                         ▼
                                       $.http.fetch POST …/tables/{t}/insertAll
                                       (insertId = event_id, client timeout,
                                        30 s host cap)
                                          │ 2xx, valid     │ 408/429/5xx/network/timeout,
                                          │ insertAll      │ or a 2xx answer that does not
                                          │ answer         │ parse as an insertAll response
                                          ▼                ▼
                                       sent          retry with backoff (1 s … 60 s),
                                                     3 retries, then dead-letter
                                                          │ 401: a fresh token, one retry at once
                                                          │ 404 table: create it, retry
                                                          │ 404 still (no dataset, or too new):
                                                          │   notice with the setup.sh command, retry
                                                          │ 400/403/invalid row: refused
                                                          ▼
                     $.fs.write  <deadLetterDir>/<project.dataset.table>/{spill,retry,rejected}-*.jsonl
                                                          │
       /bqaa-backfill, next session start, or recovery ──▶ insertAll again, to that table only
```

At `session.end` (the process is exiting) the mod starts no request: the
rows still queued, and any whose request has not answered yet, go to a spill
file in the table's dead-letter subdirectory, and the hook waits at most
`BQAA_EXIT_FLUSH_MS` for that write and for the files of refused or parked
rows still being written (it never waits for a request). A write not done
by then may end with the process, and its rows with it. The next session
sends the file 15 seconds after it starts (or `/bqaa-backfill` does). A row
whose request was still in flight can arrive twice; `event_id` dedups it.
`/clear` and `/resume` keep sending as usual.

## Install

You need the host CLI at v2.1.296 or later (its `--version` flag prints it;
2.1.296 to 2.1.304 are tested),
a BigQuery dataset (below), and a way to get a Google access token (see
[Authentication](#authentication)). The mod creates the table on first write
if it is missing.

From this repository's marketplace, `bqaa-tracing`. Add it once (the sparse
form fetches only the marketplace and the built plugins), then install:

```bash
claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
  --config project=my-gcp-project --config dataset=agent_analytics
```

If `bqaa-tracing` is already added (for the settings-hook tracing plugin),
run `claude plugin marketplace update bqaa-tracing` and install. Or, with
no marketplace added yet, in one command that adds the whole repository:

```bash
claude plugin install bigquery-agent-analytics-mod \
  --marketplace GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
  --config project=my-gcp-project --config dataset=agent_analytics
```

Moving from the marketplace this mod came from, or from the settings-hook
tracing plugin: see [MIGRATION.md](MIGRATION.md).

Inside a session, `/plugin install bigquery-agent-analytics-mod@bqaa-tracing`
does the same and opens the configuration dialog. If you install or update it
from your shell while a session is open, run `/reload-plugins` there. Run
`/plugin` and look for `1 mod active · bigquery-agent-analytics-mod` under the tabs.

**For one session, from a checkout** (development, or to try it):

```bash
export BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics
claude --plugin-dir ./plugins/claude_code_mod
```

Use this mod **or** the settings-hook tracing plugin against a table, not both:
with both enabled every event is written twice.

**For a whole team**, an administrator can turn the mod on for everyone with
the host's managed settings, pinning the destination, the token command and
every option, and turning off the settings-hook tracer; each person still
signs in to gcloud once. See [Managed settings](docs/managed-settings.md).

### The dataset

[`setup.sh`](setup.sh) creates the rest with the `bq` CLI, and is safe to run
again:

* the dataset, if it is missing;
* the table, from [`sql/agent_events_ddl.sql`](sql/agent_events_ddl.sql);
* a views dataset (default `<dataset>_reporting`);
* the usage views (see [Views](#views)).

```bash
BQAA_PROJECT_ID=my-gcp-project BQAA_DATASET=agent_analytics ./plugins/claude_code_mod/setup.sh --dry-run
```

`--dry-run` prints the commands and the SQL. Without it, they run.
`setup.sh` grants nothing: who may write and who may read is the
bootstrap, [`setup_gcp_prereqs.py`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/main/plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py), which is still in the mod's
source repository, and which `setup.sh` names when it is done (for a team, a writer service account
that may only insert rows, and readers of the views alone; see
[Service-account auth](docs/service-account-auth.md)).

`setup.sh` ships with the mod. Installed from a marketplace, it is in the
mod's own directory, not a checkout: start a session, and while BigQuery
cannot find the dataset the mod prints the whole command, with your project,
dataset, table and location and the script's path (see
[If rows cannot be written](#if-rows-cannot-be-written)). The rows of that
session wait for it. A dataset or table made moments ago can still answer
"Not found" to streaming inserts for a few minutes; rows wait through that
too, and land on their own.

### Once it is installed

The first session after an install, or after an update to a new version,
shows a card above the prompt (terminal and desktop): the mod is on, what it
records, and the table it writes to.

```
╭──────────────────────────────────────────────────────────────────────────╮
│ ✓ BigQuery Agent Analytics mod installed 0.1.0                [ Got it ] │
│ Records sessions, turns, model requests and tool calls to                │
│ my-gcp-project.agent_analytics.agent_events (US).                        │
│ ✓ First rows landed in BigQuery · 1 sent · /bqaa-status shows the counts │
╰──────────────────────────────────────────────────────────────────────────╯
```

Until BigQuery takes the first rows, its last line reads `◌ Waiting for the
first rows, sent every 5 s while you work`. They usually land at the first
send, a few seconds in, and then a toast says so as well, and the count goes
up with each send. The next prompt you type at the terminal after that
closes the card (one from elsewhere, such as a task's notification, leaves
it), and so does **Got it** (ctrl+x tab, then Enter). A transcript line,
which the model does not read, keeps what it said. The card says "updated
to" only for a version known to be newer than the one before it (by its
numbers, a release after its own pre-release); otherwise, back to an older
version or between versions that do not compare, it says "changed to".

* With no project or dataset set, the card says that nothing is recorded
  until they are set in `/config` (or `BQAA_PROJECT_ID` and `BQAA_DATASET`).
  No rows can land, so it stays until Got it.
* In a dry run, or with capture turned off, the card says so. Turned off
  after rows landed, it stays until Got it too.
* While a problem stops rows (see
  [If rows cannot be written](#if-rows-cannot-be-written)), its notice has
  the band. The card comes back once the problem clears (rows land, or
  BigQuery takes a request, whatever it makes of its rows).

The mod notes in its own store (`$.store`) the version it greeted and the
session that showed the card (key `welcome:<version>`), so no later session
shows the card; the versions noted give the next version's card the one it
replaced. What became of the card goes to keys of its own version and
session: when rows first landed, and where (`welcome:<version>:<session>`),
and that it was closed (`…:closed`, a key no other write touches, written
at once on its own, behind no other write). A
session writes only its own landing and close keys, and the greeting key of
the version it greets (a load that reopens the card writes it again where
the store has none), so a session left open never writes over a newer
version another session greeted, and no write
that answers late, from this load of the mod or an earlier one, undoes a
close. The card itself lives in the session's `$.state`, which a reload
(`/reload-plugins`, or an option changed in `/config`) keeps while the
mod's own variables start over; the band draws it from there. Every change
to it is a compare-and-set on the version read, so no load's change, early
or late, overwrites another's, and a close always stays. A reload shows
the card again as it was, its count included (a card rebuilt from the
store alone, with no session state, starts its count over); a load whose
start another load overtook shows nothing and greets no more; and a close
the store has wins over an open card in the state (a close whose state
write was refused included). A load draws the card only once it has read
the store's close for it, once more after taking the card over, so one
whose reads have not answered, or never do, draws none, and a Got it in
another load while it took the card over hides it. That close is read
whether or not the store has the greeting: a session resumed after Got it,
its greeting's write lost, shows no card and greets no more. Each draw reads the
store's close again in the background (one read at a time; one that has
not answered in 10 s stops no other), so a close another load wrote late
(its state write refused) hides the card once it lands, with no reload and
no press. Of two loads, the later one shows the card: each session start
takes the next number of a count the session's state keeps, then waits one
tick of the host's clock, a wait a hot reload cancels for the load it
replaces. A load whose number came back after a later load started takes
nothing over, and of the loads that go on a later one ranks higher,
whatever the clock says; no run replaces a card a later load or run
showed, another version's included; a load whose reads answer after a later
load took the card over leaves it to that one, and a later load whose first write of the card
an earlier one beat takes the card over, with no second greeting; a later
version's (an update in the session) shows its own card over the earlier
version's, with its own greeting. The card
counts the rows of the session start that opened it, or last moved it
where rows go now, and of every one ranked above it, where it counts rows,
and never one's ranked below it, whenever they land (ranks go by when each
run's count write landed: within one load, a run's that landed after the
next run's ranks above it): a run knows its own rank with nothing
to read, so no read or write that answers late, before or after an update,
moves rows from one card to another (a rule that took a delivery's place
from the session's state after its rows landed could be held past the
card's opening, by holding that read or the write before it). A run counts
only once it is past the tick of the clock that a reload cancels for the
load it replaces, so a load whose rank came back after a later load's
counts nothing. The count of runs starts over with the session's state
(`/clear`, `/resume` and `/branch` reset it, and fire no session start, while
a run from before goes on sending): the first run to count afresh names a
new lifetime for the count, and a card counts only runs of its own, so rows
a run from before the reset lands never count on a card opened after it.
The session's state keeps each run's rows landed so far in
each place, with its rank, whatever the card shows (none, closed, or
counting elsewhere), only ever up; the card sums them, so a run that says
so again, or late, counts once, and no run's total stands for another's
rows. The card reads them again when it opens, once its write lands, and
at each delivery: rows its own session start landed as it read the store
count at once. Rows of the version before an update never count on the
updated version's card, whenever they land: the card counts only the runs
from the one it counts from. (In the host a reload stops the load it
replaces, so that load reports no more landings; rows it had already sent
may still reach the table.) If rows now go somewhere else (another
table or location, or BigQuery after a dry run), the card waits again until
rows land there, counting from the run that moved it, and a prompt closes
it only once they have, hiding it and writing the store's close at once, as
Got it does; rows sent under the earlier configuration that land after the
reload do not count, while rows of an earlier load the card counts (the one
that opened it, after a reload took it over) do, and the load that showed
the card says so. A close, or a stored close a takeover
applies, is for the card of its own version and session alone, however
often its write is tried again: a card an update put in its place in the
session stays.
A `-p` run draws no band: it shows nothing and leaves the store and the
session's state alone.
Neither the session's start, a prompt nor the writer waits on the
session's state or the store for the card (the band, drawing it, reads the
state); the card shows once they answer, and with no session state it does
not show. The store makes "once" a best effort:

* if the records cannot be written, the next session shows the card again,
  and a reload in a session that keeps no `$.state` shows it as the store
  last had it (a write that fails goes again when more rows land, a
  landing also when a reload takes the card over, and a close at each of
  that load's flushes until it lands: a reload before it does, the state's
  close refused too, can show the card again);
* if they cannot be read, or the versions greeted before cannot be listed,
  no card shows, after a reload too, nor where no session id is known when
  the session starts (the card's landing and close are kept under it);
* two sessions started at the same moment can both show it;
* a reload in the moment between Got it and its store write landing (its
  state write refused) shows the card again, until the write lands and the
  band draws again (a refused store close is written again at each flush
  of the load that wrote it); a new process in that session, the greeting's write
  lost too, can also write the greeting line once more;
* `/clear`, `/resume` or `/branch` in the session that shows the card ends
  it there (the session's state starts over, and no session start brings
  it back); the version stays greeted; a card still being set up then is
  not shown, and its version not greeted, so a later session shows it;
* reinstalling the same version shows no card while the store keeps the
  record.

## Configure

Options are the plugin's `userConfig` (the install dialog, `/plugin configure`,
`--config key=value`, or `/config`). A string option left empty falls back to
the environment variable the tracing plugin already uses, then to a default.

| Option | Environment fallback | Default | What it does |
| --- | --- | --- | --- |
| `project` | `BQAA_PROJECT_ID`, `GCP_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT` | (required) | Project that holds the dataset |
| `dataset` | `BQAA_DATASET`, `BQ_DATASET` | (required) | Dataset of the events table |
| `table` | `BQAA_TABLE`, `BQ_TABLE` | `agent_events` | Events table |
| `location` | `BQAA_LOCATION` | `US` | Dataset location; shown by `/bqaa-status`, and passed to `setup.sh` by the setup command the mod offers (insertAll itself is addressed by table) |
| `enabled` | `BQAA_TRACE_ENABLED` set to `false`, `0`, `no` or `off` turns it off | `true` | Capture on or off |
| `dryRun` | `BQAA_DRY_RUN` set to `true`, `1`, `yes` or `on` turns it on | `false` | Write rows as JSONL to `<deadLetterDir>/../dry-run/` instead of sending |
| `maxPayloadChars` | | `16384` (1024 to 500000) | Cap per JSON payload column (`content`, `attributes`); past it the value is cut and `is_truncated` set |
| `flushIntervalMs` | | `5000` (1000 to 300000) | How often the background writer sends |
| `deadLetterDir` | `BQAA_DEAD_LETTER_DIR` | `~/.bqaa-mod/dead-letter` | Where unsent rows are kept, in one subdirectory per table (`project.dataset.table`) |
| `agentName` | `BQAA_AGENT_NAME` | `coding-agent` | `agent` column for main-loop rows |
| `accessTokenCommand` | `BQAA_ACCESS_TOKEN_COMMAND` | `gcloud auth application-default print-access-token` | Command (argv, no shell) that prints an access token |
| `userIdFromCredentials` | | `false` | Off by default, since it is personal data. When on, `user_id` is the verified email of the access token's account. See [Identity](#identity). |
| `recordHostname` | | `false` | Off by default, since it is personal data. When on, `session_metadata.host_name` holds the machine's host name. See [Identity](#identity). |

Advanced, environment only:

| Variable | Default | What it does |
| --- | --- | --- |
| `BQAA_ACCESS_TOKEN` | unset | Use this token as is (re-read every 5 minutes) instead of running the command |
| `BQAA_USER_ID` | `$USER` / `$USERNAME` | `user_id` column (with `userIdFromCredentials` on, the token's account takes its place once known) |
| `BQAA_REQUEST_TIMEOUT_MS` | `10000` (1000 to 30000) | Client-side timeout per insertAll request |
| `BQAA_MAX_QUEUE_ROWS` | `5000` (10 to 100000) | Queue bound in rows (the byte bound is 8 MiB of UTF-8) |
| `BQAA_EXIT_FLUSH_MS` | `800` (0 to 1200) | How long session end may wait for its spill file, and any dead-letter file still being written, to be written; `0` starts the write without waiting |
| `BQAA_AUTO_CREATE_TABLE` | `true` | Create the table (BQAA schema v2, partitioned and clustered) when insertAll answers 404 for it |
| `BQAA_AUTO_BACKFILL` | `true` | Replay the table's `spill` and `retry` files at session start and after BigQuery recovers |

Switch values ignore case and surrounding spaces, and
`BQAA_AUTO_CREATE_TABLE` and `BQAA_AUTO_BACKFILL` are turned off by the same
`false`, `0`, `no` or `off`. An empty or unset variable, or any other value,
changes nothing; nor can the environment turn capture on when `enabled` is
off, or a dry run off when `dryRun` is on.

For a `--plugin-dir` session, options live in the host's user settings file under
`pluginConfigs["bigquery-agent-analytics-mod@inline"].options`, or use the
environment variables above.

### Authentication

`$.http.fetch` is plain HTTPS, so the mod needs an OAuth access token and
cannot use a Google client library. The background writer, never a hook, gets
one by running `accessTokenCommand` with `$.process.run` (default
`gcloud auth application-default print-access-token`, so Application Default
Credentials: run `gcloud auth application-default login` once, or set up
service-account impersonation for it). The token is kept in memory only and
reused for 45 minutes. On a 401 the writer gets a fresh token and retries
once at once. The token goes only to `bigquery.googleapis.com`, and, with
`userIdFromCredentials` on, to Google's token information endpoint
(`oauth2.googleapis.com/tokeninfo`) in a POST body. `$.http.fetch` drops the
`Authorization` header on a cross-origin redirect. `BQAA_ACCESS_TOKEN` skips
the command. A
service-account key file cannot be used directly: the mods environment has
`crypto.subtle.digest` but no signing, so wrap it in a command that prints a
token. The type declarations mark `$.process.run` as CLI only; where it is
not available, set `BQAA_ACCESS_TOKEN`.

The identity needs to insert rows into the table. `roles/bigquery.dataEditor`
on the dataset does (it can also create the table), and fits one person's
sandbox; it can also read, change and delete every row, so never grant it to
an identity a team shares. For a shared table, grant `bigquery.tables.get` and
`bigquery.tables.updateData` on the table alone: the `bqaaRowWriter` role that
the bootstrap's `setup_gcp_prereqs.py --writer-role row-writer` makes.
[Service-account auth](docs/service-account-auth.md) says which identity to
use where (a writer service account that people impersonate, with no
long-lived key; group access; Workload Identity Federation for CI; an attached
service account; a key file only as an approved exception), who reads the
views, and offboarding.

### If rows cannot be written

Rows stop being written when no Google credentials work, when the identity
lacks permission on the dataset, or when BigQuery cannot find the dataset or
table. That happens when:

* the token command fails (never signed in, or a sign-in expired);
* BigQuery answers 401 even to a fresh token;
* BigQuery answers 403 access denied;
* insertAll answers 404 for the dataset (not made yet, say the mod was
  installed before `setup.sh` ran, or a name is wrong), or for a table the
  mod may not create (`BQAA_AUTO_CREATE_TABLE=false`) or still does not see
  two minutes after creating it. Streaming inserts can miss a dataset or
  table made minutes ago, so this also comes up just after `setup.sh`.

While that lasts, the mod shows a notice:

* **The band above the prompt** (terminal and desktop) says what is wrong
  and the fix for the token source in use, for example `run: gcloud auth
  application-default login`. For a missing permission it names the role to
  ask for on which dataset. For a dataset or table BigQuery cannot find, a
  second line gives the `setup.sh` command. It sets every name the script
  reads (your project, dataset, table, views dataset `<dataset>_reporting`
  and location), so none comes from a variable already set in your shell,
  then runs the script in the mod's directory with `bash`, its path quoted
  as one shell word. It is offered only for names `setup.sh` takes and a
  POSIX path; otherwise the fix says to create it or correct the options.
  The band also counts the
  rows waiting: queued, in flight, or parked on disk since the problem began.
  It keeps what other mods draw there below it, and gives the band up to a
  survey.
* **A toast** comes once per kind of problem in a session.
* **A dim transcript line** comes once per kind of problem, with the reason
  (redacted) and, for a missing dataset, the command, which goes on a line
  of its own when the redaction would take it with the rest (a project
  named like a credential). A `-p` or SDK host receives it as `ui_log`.
* **The status line** shows the headline and the fix, with the count of
  rows waiting; the command is on the band, the transcript line and
  `/bqaa-status`.
* **`/bqaa-status`** adds a `notice` line, and for a missing dataset a
  `setup` line with the command.

A command too long for the band's line (very long names, or a deep mod
directory) is not cut there or in the transcript line, where a command cut
short would not run: they say to run the one `/bqaa-status` shows whole.
No command is offered when its path holds text the redaction would change
(a path segment shaped like `token=…`), or when it is longer than
`/bqaa-status` can show whole.

The next send or replay that BigQuery takes clears the notice. So does a
400 refusing the rows themselves, which shows that BigQuery found the table,
took the credentials and let the identity write. Recovery is quick:

* While credentials or a missing dataset are the problem, the backoff
  before the next retry is at most 30 seconds. That bounds the pause, not
  the time to the retry: it goes at the next flush tick after the pause,
  so add up to `flushIntervalMs` (5 seconds by default, longer if the
  options set it so).
* A prompt starts a retry at once, from a timer, so signing in from the
  session (`! gcloud auth application-default login`) is enough.
* Rows parked meanwhile follow on their own once BigQuery takes rows again
  (`BQAA_AUTO_BACKFILL`). With nothing new queued (a session left idle),
  the retries replay the parked rows until every one has landed, a replay
  that lands only some included, so they land and the notice clears
  without a prompt. That covers rows parked while a table the mod just
  made was not visible yet. A file another session is replaying, or one
  that could not be listed or read, keeps its rows waiting for the next
  replay. A replay records what became of each of a file's rows before it
  writes what is left (in `$.store`, or in two files beside it that take
  the record's updates in turn when the store does not take it), so when
  what is left cannot all be written, or the file cannot be emptied, the
  next replay of any writer, in this session or another, writes the rest
  under the same names and empties the file, and none of its rows goes
  again. A record that cannot be read leaves its file waiting, nothing of
  what a replay left is written until its record is, and a file of what is
  left is replayed only once the record has it as written. The paced replay
  of an idle session stops between requests for rows queued meanwhile, which
  go first.
* A replay's 401 gets a fresh token and one more try, as a send's does, and
  a second 401 is the credentials notice.
* Rows refused for a missing permission wait for `/bqaa-backfill`, and the
  transcript says so when rows flow again. So do rows BigQuery refuses on a
  replay: they move to a `rejected` file, and no replay that runs by itself
  sends them again.

The notice never holds up a prompt: prompts go to the model as usual, and
the rows wait.

### Identity

Both options are off by default, since each adds personal data to the rows.

* **`userIdFromCredentials`** sets `user_id` to the account of the access
  token each row is sent with.
  * The writer looks up the account of each token it uses, at Google's
    token information endpoint, with the token in a POST body. A new token
    is a new credential and is looked up again: a refresh, the fresh token
    after a 401 (the retry carries the new account), or another
    `BQAA_ACCESS_TOKEN`.
  * A lookup that answers late, for an older token, never replaces the
    account of a newer one.
  * It takes the email only if the endpoint says it is verified. A token
    without the email scope gives none.
  * A failed lookup is retried after ten minutes. Until then, and while the
    current token's account is not known, rows carry the configured
    `user_id`, never an account looked up for another token.
  * A row parked on disk carries the account of the token in use when it was
    parked, and keeps it when replayed. Rows an earlier session parked are
    sent as they were.
  * A token that expires, or that BigQuery refuses, is dropped together with
    the account looked up for it. Rows parked while no fresh token comes
    (at retry exhaustion or at exit) carry the configured `user_id`, never
    the expired token's account.
  * The address goes to the table, and to rows parked on disk. It never
    goes to a log line or a command's output.
* **`recordHostname`** runs the `hostname` command once, from a timer.
  * Rows built after it answers carry `session_metadata.host_name`, from
    the first turn on (`INVOCATION_STARTING`).
  * A name that is not a plain host name is left out.
  * On macOS the default host name often includes the owner's name.

## What it writes

### Event mapping

| Host event (mod hook) | BQAA `event_type` | `content` | Notable `attributes` / `latency_ms` |
| --- | --- | --- | --- |
| `classic.SessionStart` | `STATE_DELTA` | `{"session_start": true}` | `state_delta.start_source` (`startup`, `resume`, `clear`, `compact`, `fork`), `model`, `session_metadata` (project, cwd, `host_version`) |
| `prompt.submit` (after it entered) | `USER_MESSAGE_RECEIVED` | `{"text_summary": prompt}` | `prompt_origin`, `submitted_during_turn` |
| `turn.start` (main loop) | `INVOCATION_STARTING` | `{}` | `turn_number`, `session_metadata` |
| `turn.step`, before the request | `LLM_REQUEST` | `{"prompt": [{"role": "user", "content": …}]}` on a turn's first request, `{"prompt": []}` after | `model`, `llm_config.effort`, `step_index`, `message_count` |
| `turn.step`, response | `LLM_RESPONSE` | `{"tool_uses": [names], "response": text}` | `model`, `usage_metadata`, `finish_reason`; `latency_ms.total_ms`, `time_to_first_token_ms` |
| `turn.step`, no response | `LLM_ERROR` (status `ERROR`) | `null` | `latency_ms.total_ms` |
| `tool.call`, before | `TOOL_STARTING` | `{"tool", "tool_origin", "args"}` | `tool_use_id` |
| `tool.call`, after | `TOOL_COMPLETED` | `{"tool", "tool_origin", "result"}` | `latency_ms.total_ms`; status `ERROR` with `error_message` for a tool error or a hook's deny (`denied: true`); `permission_requested`; `permission_decision`, `permission_decision_source` |
| `classic.SubagentStart` | `AGENT_STARTING` | `{"agent_type", "agent_id"}` | `agent` column = subagent type |
| `turn.complete` (subagent) | `AGENT_COMPLETED` / `AGENT_ERROR` | `{}` | `usage_metadata` (the subagent's totals), `latency_ms.total_ms` |
| `turn.complete` (main loop) | `AGENT_RESPONSE` (if it answered) and `INVOCATION_COMPLETED` / `INVOCATION_ERROR` | `{"response": answer}` / `{}` | `finish_reason`, `is_aborted`, `step_count`, turn `usage_metadata`, `latency_ms.total_ms` |
| `classic.PermissionRequest` | (no row) | | sets `permission_requested` on the waiting tool's `TOOL_COMPLETED` |
| `telemetry.log` (`tool_decision`, a collector record) | (no row) | | sets `permission_decision` (`accept`, `reject`) and `permission_decision_source` (`config`, `hook`, `user_temporary`, `user_permanent`, `user_abort`, `user_reject`) on that call's `TOOL_COMPLETED` |
| `session.end` | `STATE_DELTA` | `{"session_end": true}` | `state_delta.reason`, `trace_count`, `duration_ms` |

The host can raise `classic.SessionStart` before `session.start` has read
the environment and the host version; its row waits until it has, keeping
the event's time, so it carries the same user, agent name, writer mode and
host version as the rows after it. A subagent's run starts with a
`turn.start` of its own that names its loop (`agentId`): it writes no row,
does not count as a turn, and leaves the main turn running.

`usage_metadata` carries the names this repo's tracing plugin and analysis
skill read (`prompt_tokens` = input + cache read + cache write,
`completion_tokens`, `total_tokens`) and the four counts as the API reports
them (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`). Token counts come from the engine's
`turn.step` and `turn.complete` results.

In `content` and `attributes`, values under keys that are not on the
allowlist are redacted, inside `args` and `result` too (see
[Privacy and security](#privacy-and-security)).

The permission decision comes from the host's own `tool_decision`
collector record. That record reaches mods with no telemetry settings
(checked live with host 2.1.299), and is matched to its call by
`tool_use_id`. The mod passes every collector record on unchanged, and
ignores one that a later hook refuses.

* An accept arrives while its call runs.
* A rejection arrives just after its call has returned. So once the host's
  records are arriving, a call that ends without its decision waits up to
  2 seconds for it. Its row keeps the time the call ended.
* A call whose decision never comes is written without one. So is one
  still waiting when the session ends.

A failed tool is `TOOL_COMPLETED` with status `ERROR`, as in the tracing
plugin and the analysis skill's failure-rate query (ADK itself uses
`TOOL_ERROR`). Every row has `attributes.writer`
(`{"plugin": "bigquery-agent-analytics-mod", "version", "label", "agent", "mode"}`),
`root_agent_name` and `source: "coding_agent"`.

### Ids and spans

* `session_id`: the host CLI's session id, read again for each event, so the
  rows after `/clear`, `/resume` or `/branch` carry the new one, even where
  the classic hooks do not run (see Limitations). If the host refuses that
  read after such a change, rows carry no session id rather than the old one.
* `invocation_id`: the engine's turn id; a subagent's rows carry the turn that
  started it.
* `trace_id`: one 32-hex trace per main-loop turn; a subagent started in that
  turn shares it.
* `span_id` / `parent_span_id`: the turn's span is the root; each model
  request is a span under it (its `LLM_REQUEST` and `LLM_RESPONSE` share it);
  a tool call is a span under the model request that asked for it; a subagent
  has its own span under the turn, and its requests and tools nest under that.
  Each run of a subagent's loop (a teammate woken again, say) gets a span of
  its own under the main turn it runs in.
* `event_id`: 32-hex, unique per row, sent as insertAll's `insertId`. It is
  the dedup key: a row sent again (an ambiguous timeout, a backfill) has the
  same one. Use `QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1`,
  as the ADK docs do.

### Table

[`sql/agent_events_ddl.sql`](sql/agent_events_ddl.sql) is the BQAA schema
version 2 (with `event_id`), day-partitioned on `timestamp` and clustered on
`event_type, agent, user_id`, the same as ADK's manual DDL. A table the
tracing plugin created (schema 1, no `event_id`) still takes the rows
(insertAll runs with `ignoreUnknownValues`); add the column with the `ALTER`
in that file to keep the dedup key.

The `JSON` columns (`content`, `attributes`, `latency_ms`) travel to insertAll
as JSON text. That is the representation Google's own Python client sends for
JSON-typed values on `tabledata.insertAll` (its `_json_to_json` converter is
`json.dumps`; [`_helpers.py`](https://github.com/googleapis/python-bigquery/blob/main/google/cloud/bigquery/_helpers.py)), and the one the tracing plugin's
`insert_rows_json` fallback uses.

### Sample rows

[`examples/sample-rows.jsonl`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/examples/sample-rows.jsonl) is the insertAll
payload of a fixture session (`tests/sample-rows.test.ts`): a prompt, four
model requests, Read, Agent (an Explore subagent), Edit and Bash (with a
permission prompt), and session end. It holds the rows the stubbed insertAll
took, then the session-end row, which session end writes to a spill file for
the next session to send. **Sample output from the test
fixture:** the model id `example-model`, the token counts and the turn's
48,211 ms are fixture inputs; the model and tool `latency_ms` values are what
the run measured, a few milliseconds, since nothing really ran. Regenerate with:

```bash
claude plugin test . 2>&1 | sed -n 's/^BQAA_SAMPLE_ROW //p' > examples/sample-rows.jsonl
```

One `LLM_RESPONSE` row from it, JSON columns expanded:

```json
{
  "timestamp": "2026-10-14T02:54:21.137Z",
  "event_id": "d0529c41b9e2e1968859a1305b4a165f",
  "event_type": "LLM_RESPONSE",
  "agent": "coding-agent",
  "session_id": "5c4b1d2e-8f7a-4c3b-9d1e-2a6f0b7c8d9e",
  "invocation_id": "3f0e2a71-9c4d-4b8e-a1f6-52d7c0e9b134",
  "user_id": "dev",
  "trace_id": "a4db76890e3b711d381938f9c05e0935",
  "span_id": "014ed66196c9a931",
  "parent_span_id": "f5ceed92dfb61a76",
  "content": { "tool_uses": ["Read"], "response": "I'll look at the flush loop first." },
  "content_parts": [],
  "attributes": {
    "root_agent_name": "coding-agent",
    "source": "coding_agent",
    "model": "example-model",
    "model_version": "example-model",
    "usage_metadata": {
      "prompt_tokens": 20854, "completion_tokens": 96, "total_tokens": 20950,
      "input_tokens": 2310, "output_tokens": 96,
      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 18544
    },
    "finish_reason": "tool_use",
    "step_index": 0,
    "writer": { "plugin": "bigquery-agent-analytics-mod", "version": "0.1.0", "label": "bigquery-agent-analytics-mod/0.1.0", "agent": "coding-agent", "mode": "insert_all" }
  },
  "latency_ms": { "total_ms": 1, "time_to_first_token_ms": 1 },
  "status": "OK",
  "error_message": null,
  "is_truncated": false
}
```

### Views

`setup.sh` makes two views in the views dataset, from the `LLM_RESPONSE`
rows this mod wrote (`attributes.writer.plugin` is
`bigquery-agent-analytics-mod`): the main loop's and the subagents' model
requests. The table can be shared with ADK agents and the settings-hook
plugin. Other writers can name their token counts differently (ADK's
`prompt_token_count`, for one), so their rows are left out. That way
requests, sessions and tokens always count the same rows.

* [`daily_usage_by_model`](sql/views/daily_usage_by_model.sql): per day and
  model, model requests, sessions and the token counts.
* [`daily_usage_by_user`](sql/views/daily_usage_by_user.sql): per day and
  `user_id`, sessions, model requests and the token counts.

`day` is the UTC date of each request (`DATE(timestamp)`): in the evening
west of Greenwich, today's requests are under tomorrow's date.

The details below were checked on BigQuery itself:

* Each view keeps one row per `(timestamp, event_id)`. A row sent twice has
  the same `event_id` and timestamp.
* A filter on `day` still reaches the table's day partitions: a dry run with
  one day's filter reads 0 bytes of the other days. Deduplicating on
  `event_id` alone would read the whole table.
* Over a fixture shared by several writers, both views count only this
  mod's requests, sessions and tokens. That run read literal rows, with no
  table, and so does [`tests/views.test.py`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/tests/views.test.py) offline.

The rows carry token counts, not prices, so the views report tokens. Keep
the views in a dataset of their own, as `setup.sh` does, so the identities
that write rows need no access to them.

```sql
SELECT day, model, model_requests, input_tokens, cache_read_input_tokens, output_tokens
FROM `my-gcp-project.agent_analytics_reporting.daily_usage_by_model`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
ORDER BY day, model
```

### Queries

[`sql/sample_queries.sql`](sql/sample_queries.sql) has eight, written with
the analysis skill's `{PROJECT}.{DATASET}.{TABLE}` placeholders and `@start` /
`@end` / `@trace_id` parameters so `scripts/run_bq.py` runs them: rows
deduplicated, tokens and cache share per session and model, model latency
(p50/p95 total, time to first token), tool failure rate and latency, one line
per turn, the span tree of a trace, subagents by type, and the mod's own
health. Each reads only this mod's rows (`attributes.writer.plugin`: a table
can also hold the settings-hook tracer's or ADK agents' rows). `QUALIFY` is
evaluated after `GROUP BY`, so a query that aggregates dedups on `event_id`
in a CTE first and aggregates what it returns. For example, tokens per
model:

```sql
WITH responses AS (
  SELECT *
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE event_type = 'LLM_RESPONSE'
    AND timestamp BETWEEN @start AND @end
    AND JSON_VALUE(attributes, '$.writer.plugin') = 'bigquery-agent-analytics-mod'
  QUALIFY event_id IS NULL OR ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY timestamp) = 1
)
SELECT
  JSON_VALUE(attributes, '$.model') AS model,
  COUNT(*) AS model_requests,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.input_tokens') AS INT64)) AS input_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.cache_read_input_tokens') AS INT64)) AS cache_read_tokens,
  SUM(CAST(JSON_VALUE(attributes, '$.usage_metadata.output_tokens') AS INT64)) AS output_tokens
FROM responses
GROUP BY model
```

[`docs/value.md`](docs/value.md) says what a developer, a team lead and a
platform team can learn from the rows, with the questions' queries in
[`sql/value_queries.sql`](sql/value_queries.sql): sessions and turns, adoption
against a roster, an estimated cost, tools, errors over time, and the mod
beside ADK agents. It also covers who can run each query, the denominators,
and the limits.

## Reliability

* **Queue:** in memory, bounded by `BQAA_MAX_QUEUE_ROWS` rows and 8 MiB of UTF-8; when
  full it drops its oldest rows and counts them. While BigQuery is failing and
  the queue passes 80%, the writer moves the oldest half to a `spill` file
  instead of letting rows drop.
* **Flush:** every `flushIntervalMs`, and right after each main-loop turn; one
  flush request at a time for each writer (a replay of parked rows, at
  session start, once rows land again or by `/bqaa-backfill`, can run beside
  it, and so can a batch a writer taken over from still has out); batches of
  at most 500 rows and 3 MiB of UTF-8
  (`$.http.fetch` refuses bodies over 4 MiB, counted in characters, and UTF-8
  bytes are never fewer).
* **Timeouts:** each insertAll is raced against `BQAA_REQUEST_TIMEOUT_MS`
  (a `$.clock.after` timer), under the host's own 30-second cap per
  `$.http.fetch`; the token command has 15 seconds.
* **Retries:** 408, 429, 5xx, quota 403s, network errors, timeouts and a 404
  for a dataset or table BigQuery cannot find back off exponentially (1 s
  doubling to 60 s, with jitter; at most 30 s while no credentials work or
  the dataset cannot be found) and retry a batch up to 3 times with the same
  `insertId`s. A 401 gets a fresh token and one retry at once, on a send or
  a replay, and only a second 401 counts as a failure. A 2xx
  answer counts as delivered only when it is a valid insertAll response: a
  body cut off at the response limit, a non-JSON page or an `insertErrors`
  entry that names no row of the batch leaves the outcome unknown, so the rows
  are retried the same way and parked if the retries run out.
* **Dead letter:** rows that exhaust retries go to `retry-*.jsonl`, rows
  waiting for a dataset or table BigQuery cannot find included; rows
  BigQuery or the configuration refuses (400, 403 access denied, an invalid
  row reported by insertAll) go to `rejected-*.jsonl`;
  rows queued at exit go to `spill-*.jsonl`. Each line keeps the full row,
  the reason (redacted), the attempt count and the table it was meant for.
  Each file ends with a completion record, written after its rows: how many
  lines come before it and their checksum (FNV-1a). Files go to a subdirectory of `deadLetterDir` named after that table
  (`project.dataset.table`, percent-encoded where a name needs it). Each
  failure writes a new file (the mods file API has no append) of at most
  3 MiB of UTF-8, under the 4 MiB the API reads or writes per file; a row too
  large for a file on its own has its payload columns cut to fit and
  `is_truncated` set.
* **Backfill:** `/bqaa-backfill` resends the files in the configured table's
  subdirectory to that table, and checks each line's recorded table first: a
  line for another table is kept as written, and rows parked for another
  table are sent only by a session configured for that table. `spill` and
  `retry` files are also replayed 15 seconds after a session starts and once
  BigQuery recovers in a session that parked rows. While none is queued,
  the paced retries replay them too, until every row parked for the
  configured table (by this session, or left by a replay) has landed: a
  file another session holds, or one that could not be listed or read,
  keeps its rows waiting for the next replay. The paced replay, which a
  flush runs while nothing is queued, stops between requests for rows
  queued meanwhile, which go first; the replay after `session.start` runs
  beside the flushes, which send rows queued meanwhile. A replay that runs
  by itself sends `spill` and `retry` rows only, whatever file holds
  them. Rows BigQuery refuses on any replay (an invalid row, a
  400, 403 access denied) move to `rejected-*.jsonl` and wait for
  `/bqaa-backfill`, as a send's refused rows do. Access denied is the
  notice; the other refusals are on the status line, and `/bqaa-status`
  counts them in its last-backfill line. Each row counts once, in the order
  the requests came: one `/bqaa-backfill` sends again from a `rejected`
  file, and BigQuery refuses again, is not counted twice, and rows that land
  end the count of the refusals before them. A count keeps the ids of
  10,000 rows; past that a row may count again, and the count reads "about
  N". A replay that BigQuery takes
  clears the notice, as a send does. A replay reads a file
  only when its completion record is there and matches every line above it:
  the API's writes are not atomic, so another session can read a file
  part-written, and the record is what marks a file whole. A file without a
  matching record (still being written, or left by a writer that stopped
  part-way) is kept as it is, never read or emptied, and `/bqaa-status`
  lists it. One read whole at another length than the directory listing
  gave (a write that finished between the two) waits for the next replay,
  since what the replay records of it goes by the listed size. A file whose rows all land is truncated to zero bytes (the API
  has no delete; empty files are safe to remove). What a replay leaves (rows
  that fail again, other tables' lines, lines it cannot parse) is written to
  new files first, each kind to files of its own and each file with its
  completion record, and only then is the replayed file truncated. A replay
  whose rows BigQuery only asks for again rewrites nothing. The API has no
  rename either, so a write that fails leaves that file as it was. Before
  it sends any of a file's rows, a replay writes its own mark beside the
  file (`outcome-sending-`, the writer's name, and the file's), a file no
  other writer writes or empties, and reads the marks and the file's record
  back: another writer's mark (unless `/bqaa-backfill force` asked for the
  file afresh), an outcome recorded or the file done meanwhile, and it
  sends none of the file and lets its own mark go. One that cannot write its mark
  sends none of it either: the rows wait. A replay that changes nothing
  (rows only asked for again, or no answer), or that finishes the file,
  lets its own mark go, so each try writes its one small file twice and
  adds no file per try. Each writer (each session, and each load of the mod
  in it) adds one such file beside a file it replays, emptied once it lets
  its mark go, and not read once empty; the API has no delete, so the
  emptied files stay (empty files are safe to remove). A writer that finds
  another writer's mark, whole or cut off part-way, and no outcome recorded
  (that writer still at it, unable to write its record, or stopped after
  sending) sends none of the file, `/bqaa-backfill` included: its rows may
  have been refused, and that writer may yet record what became of them.
  `/bqaa-backfill force` replays it afresh, for a session that stopped
  mid-replay, once no other session's lease on the file stands (a lease
  lasts ten minutes; force passes the mark, not the lease); run while that
  session is in fact still replaying the file, it can send a refused row
  once more, as two replays of one file each keep what they learned.
  Likewise a file of what is left of each of two outcomes of one file
  recorded at once can go, and so can one first replayed more than a day
  after its parent was done (its parent's records swept): a row can arrive
  twice, and `event_id` dedups it. Where `session.start` fires again in
  the same load of the mod (the plugin turned off and on), the writer it
  makes takes over from the one before: it holds the outcomes that one
  held, an outcome that one's replay comes to afterwards included, but not
  its marks: a mark of the one before whose outcome it does not hold keeps
  it from sending the file, as another session's does. It shares that
  one's queue, and what waits for a replay, with every writer before and
  after it, so the rows of a batch any writer before still had out, asked
  for again, go with this writer's own (rows it had queued itself go
  behind the older ones), rows one parks late, a batch out of retries, set
  this writer's paced replays going, the status line is one for them all
  (a line one sets late is cleared by the next landing), and the exit spill
  takes a batch one still has out, its answer not come (rows an answer
  refused or parks are in a file being written already: the exit waits for
  that write, within the same bound, and makes no second copy); after it
  they start no batch (a replay one has under way goes on, its rows on disk
  and fenced by their records). The counts `/bqaa-status` gives, a standing
  notice and the refused count on the status line are each writer's own:
  they start over at the takeover.
  A reload of the
  mod's code is a load afresh, which holds none of that (see the limits
  below). Before
  it writes what is left, a replay records its outcome: one record per
  file, with what became of each row (a letter each, and for a refused row
  a number naming its reason), the refusals' reasons (16 at most: rows
  refused for any reason past the first 15 share one that says so), and
  the names of the files of what is left, each marked once written. A
  writer that resumes an outcome reads the file again first: read
  part-written, or other than as listed, it keeps its record and waits
  until it reads whole, as a file with no record does. The
  record goes to `$.store`, which holds 4 MiB of JSON in all; one over
  64 KiB, or one the store does not take, goes to the files beside the
  replayed one (`outcome-` and its name, then `outcome-b-` and it, which no
  replay takes for rows). Those two take its updates in turn, each
  numbered, and a writer reads the later whole one, so an update cut off
  part-way leaves the one before it whole. A record cut off with none
  whole behind it means nothing of what is left was written: the writer
  that holds the outcome writes it again, any other replay leaves the file
  waiting, and `/bqaa-backfill force` replays it afresh. An empty one of those files beside a file that still
  holds its rows counts as cut off too (a write that stopped before its
  first byte). Once every file of what is left is written, the record is
  written as the mark that the file is done, in a file of the writer's
  own (`outcome-done-`, the writer's name, and the file's), which no write
  of a record goes to and no other writer writes, so no late write of
  another session's, a done mark of its own still being written included,
  takes it away; then the replayed file is emptied, and the store's copy
  of the record goes. A writer that finds a done mark never writes one:
  a late copy of a finished file is emptied with the marks left as they
  were. One cut off part-way, with no other record whole, counts as a
  record cut off (the file may have been done): the file waits, and
  `/bqaa-backfill force` replays it afresh. Of two whole ones, every
  writer goes by the later, by number, then by name. The
  mark is read before any other record, and read again, after the
  sending marks, before a writer takes no record found for an answer
  (another writer may have finished the file while those were read, its
  done mark written before it let the store's copy, or its sending mark,
  go; a writer about to send goes by the sending marks that read found,
  or, beside a record cut off part-way, reads them and then the done marks
  once more): a late write of another session's copy of
  a file it left is then emptied unsent, another session's late outcome
  for it stops there, and a file of what is left that the mark does not
  name is a late copy of another session's outcome, emptied unsent too, as
  it is where the mark could not be written and the record of the emptied
  file has every file it names written. These files, of files emptied more
  than a day ago, are emptied at the next replay: how long ago, by the
  listing's time, or, where it gives none (or 0), by when the done mark
  says the file was done. Nothing of what is left
  is written until the record is: when neither the store nor those files
  take it, the replayed file stays as it was, this session tries again at
  each replay, and its mark keeps every other session from sending the
  file. Each file of what is left names the file it came from, and
  no replay takes it until that file's record has it as written (or names
  other files, or is gone, or its file is emptied): one written whose mark
  the record could not keep waits, and the writer that finishes the
  outcome writes it again over the copy no one took. Before it records
  anything, a writer reads the record, and the file, again: another
  writer's outcome goes on instead; the file done, or emptied with no
  record whole, it writes nothing. Any writer that
  replays the file next, this session's or another's, a later
  session.start's included, reads the record, even one that holds the
  outcome itself: it writes from the file and the record what is still
  missing, under the same names, never a file the record has as written
  (one may since have been replayed and emptied), then empties the file,
  and sends none of its rows again. A record that cannot be read leaves
  its file waiting for the next replay, never taken for no record. A file
  a replay leaves waiting (its record cut off or unreadable, or not
  writable, another writer's mark beside it, or its own mark not written)
  holds up none of the files after it, and `/bqaa-status`,
  like the `/bqaa-backfill` transcript line, says why the first such file
  was left (`left waiting:`), another session's lease on a file, or a file
  of what is left waiting on its parent's record, included. A file
  that cannot be emptied stays on disk, its rows landed or in the newer
  files, and each replay tries to empty it. A file over 4 MiB, which the
  API cannot read, is reported by `/bqaa-status` and the backfill instead
  of being passed over. A file read before its completion record was
  written keeps its table due, so the paced replays read it again and send
  it once it is whole, and so does an empty one (a write can show a file
  empty before any of it lands) with nothing beside it to say a replay
  emptied it, each for a lease's time from its last write (or, where the
  listing gives no time, or 0, from when the writer first saw it so): one
  never finished costs those reads and nothing more. So does one listed
  with its rows and read empty with no done mark beside it. A per-file lease in `$.store` keeps two sessions from replaying the
  same file at once (best effort: of two that read the lease at the same
  moment, one that reads the other's mark back beside its own sends none of
  the file, so at most one sends it, and if each reads the other's,
  neither does, and the next replay sends it; a `/bqaa-backfill force` that
  sends past another writer's mark, and whose answer comes second, finds
  the other's record and writes nothing of its own (that outcome goes on,
  at the next replay), or finds the file already done, and writes nothing, so what is left is written
  once, unless both record theirs at the same moment; a write already under
  way when the other session finishes lands, and the marks above have the
  next replay empty it unsent); a session that finds a file leased tries
  it again at its next replay. Files a replay writes are named with the
  writer's name too, so two writers of one session never make the same
  name.
* **Fail open:** a hook's bookkeeping runs behind `.catch` or inside `try`;
  the writer never throws into a hook; every failure path ends in sent, on
  disk, or counted as dropped. An environment variable a policy refuses reads
  as unset, `BQAA_ACCESS_TOKEN` included (the token command takes over).

## Commands

All three run immediately, even while the agent is working, and print counts,
never payloads or tokens (the agent reads a command's output too).

* `/bqaa-status`: target table, queue depth, rows captured / sent /
  dead-lettered / refused / spilled / dropped since `session.start` (they
  start over where the plugin is turned off and on), retries, backoff,
  the notice if rows cannot be written (with the `setup.sh` command when
  BigQuery cannot find the dataset),
  the last error (redacted), the configured table's dead-letter files, how
  many files elsewhere in the directory belong to other tables, any file too
  large to read or without a completion record, the last backfill.
* `/bqaa-flush`: send the queue now instead of at the next interval.
* `/bqaa-backfill`: resend the configured table's dead-letter files in the
  background; a dim transcript line reports the result, and why any file
  was left waiting. It leaves a file another session's replay holds (its
  mark, or its record cut off part-way); `/bqaa-backfill force` replays
  such a file afresh, for a session that stopped mid-replay, once that
  session's lease on it (ten minutes) has run out.

When BigQuery refuses rows or retries run out, the mod also sets a status line
under the prompt (`⚠ bigquery-agent-analytics-mod: …`) until rows land
again, sent or replayed; its count of refused rows covers every row refused,
and written to a dead-letter file, since rows last landed. Rows whose file
could not be written are lost, and the line says so instead of pointing at
`/bqaa-backfill`. While no credentials work, a permission is missing or
BigQuery cannot find the dataset, that line, the band above the prompt and
`/bqaa-status` give the fix (see
[If rows cannot be written](#if-rows-cannot-be-written)). The host keeps a
status line across a reload of the mod; the reloaded mod clears it.

## Privacy and security

The table receives your prompts, the model's visible answers, tool arguments
and tool results (the text the model read), cut to `maxPayloadChars` per
column. Treat the dataset like the transcripts it mirrors.

* **Allowlist:** in `content`, `attributes` and parsed JSON text, within the
  depth and character budgets, a value is kept only under a key on the list in
  [`hooks/lib/allowlist.ts`](hooks/lib/allowlist.ts); under any other key it
  becomes `[REDACTED]`, whatever it is: a string, a number, a boolean, a list
  or an object (`null` stays). Keys are compared by their words, so
  `inputTokens` and `INPUT_TOKENS` are `input_tokens`. The list:
  * structure and telemetry: `event_type`, `timestamp`, `status`,
    `event_id`, `invocation_id`, `trace_id`, `span_id`, `parent_span_id`,
    `agent`, `agent_id`, `agent_type`, `root_agent_name`, `subagent_type`,
    `subagent_turn_id`, `source_turn_id`, `tool_use_id`, `tool`,
    `tool_name`, `tool_origin`, `tool_uses`, `server_tool_uses`, `model`,
    `model_version`, `llm_config`, `effort`, `usage_metadata`,
    `prompt_tokens`, `completion_tokens`, `total_tokens`, `input_tokens`,
    `output_tokens`, `cache_read_input_tokens`,
    `cache_creation_input_tokens`, `max_tokens`, `latency_ms`, `total_ms`,
    `time_to_first_token_ms`, `duration_ms`, `step_index`, `step_count`,
    `message_count`, `turn_number`, `trace_count`, `session_start`,
    `session_end`, `state_delta`, `source`, `start_source`,
    `permission_mode`, `reason`, `finish_reason`, `is_aborted`, `is_error`,
    `is_read_only`, `denied`, `inferred`, `permission_requested`,
    `permission_decision`, `permission_decision_source`,
    `submitted_during_turn`, `wait`, `prompt_origin`, `attachments`, `role`,
    `type`, `session_metadata`, `project_name`, `cwd`, `host_version`,
    `host_name`, `writer`, `plugin`, `version`, `label`, `mode`, and the
    built-in tools'
    `limit`, `offset`, `timeout`, `run_in_background`, `replace_all`,
    `head_limit`, `multiline` and `output_mode`;
  * free text: `text_summary`, `prompt`, `content`, `response`, `result`,
    `args`, `text`, `message`, `error`, `error_message`, `stdout`, `stderr`,
    `output`, and the built-in tools' `command`, `description`,
    `file_path`, `path`, `notebook_path`, `pattern`, `glob`, `query`, `url`,
    `old_string`, `new_string`, `new_source`, `plan`, `todos`, `edits` and
    `active_form`.

  So `api_keys`, `token2`, `headers`, `env` and most of an MCP tool's own
  arguments lose their values, credential or not. A kept value is still
  checked: its text goes through the two rules below. So is a name that is
  not on the list: one that holds a credential shape, a URL with user-info
  or a credential-named parameter, or a credential-named value
  (`password=…`), or is too long to scan, becomes `[REDACTED_KEY:n]`,
  numbered within its object and never one of that object's own names.
* **JSON in text:** text that looks like JSON (an object; a list that opens
  with a value, whether an object, a list, a string, a number, `true`,
  `false` or `null`, or is empty; or a JSON-encoded string whose text opens
  an object or a list) is parsed and held to the same list within the
  character and depth budgets, then encoded again (compactly). Deeper
  content is dropped and marked. Text that looks like JSON but
  cannot be parsed whole (longer than the 24,064 characters parsed at the
  default `maxPayloadChars`, or not valid JSON, such as a document cut short
  or JSON lines) is dropped: it becomes `[TRUNCATED: N chars of JSON text
  that could not be parsed and checked]`, the row is marked `is_truncated`,
  and the drop counts as a redaction. It is never scrubbed line by line.
  Text that only starts with a bracket or a quote (`[1/3] Building`,
  `[INFO] ready`, `"Hi," she said.`) does not look like JSON; `[1] first`
  does, and is dropped.
* **Free text (best effort):** non-JSON strings are scanned for credential
  patterns. Simple literal forms such as `--password=VALUE`,
  `--password VALUE`, and `https://user:VALUE@example.test/path` are
  redacted. The URL example has exactly one `@` and no shell syntax.
  Plain-list probes include
  `--set-env-vars=VERSION=v,DB_PASSWORD=VALUE`, selected credential names
  and lengths, and nonempty literal values in single shell words made of
  letters, digits, `_`, `.`, `-`, `,` and `=`. The direct name reader has a
  100-character bound; separate tests exercise longer names through the
  list fallback. These examples do not establish coverage of every word
  made from those characters. Names matching a
  provider-token pattern are excluded: that earlier pass can rewrite the
  name before its value is checked. Listed names such as `max_tokens` do
  not trigger redaction themselves; an earlier credential in the same list
  can consume their values too. Quoting, escapes, expansions, other list
  separators and more complex URLs are best effort; see
  [Limitations](#limitations) for concrete misses and
  [#546](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/546)
  for sanitizer hardening. This build additionally checks a filtered matrix
  of 8,454 list commands across five wrappers (direct, both `sh -c` quote
  forms, JSON-string logs and INFO JSON logs) and eight output paths. This
  is a finite regression set, not coverage of arbitrary shell syntax.
  Additional regressions combine selected encoded whitespace and name
  spellings with nested command strings and JSON logs. A backslash-spelled
  name after an earlier quoted space remains a known gap in double-quoted
  command strings and INFO JSON logs. These rules can miss credentials or
  redact ordinary text; see the pinned examples in Limitations.
* **Diagnostics:** error text from BigQuery, the token command or the file
  system goes through the same free-text rules before it is cut (including
  where BigQuery's error messages and per-row insert errors are first held to
  their length), kept or shown: in dead-letter reasons, the status line, the
  band above the prompt, toasts, `/bqaa-status`, backfill reports, the debug
  log and the transcript. A failing token command is named by its program
  only, never its arguments.
* **Truncation:** each payload column is held to `maxPayloadChars`,
  error messages to 4,096 characters; the copy is built within that budget, so
  a multi-megabyte tool input is bounded before scanning (a test checks
  under 100 ms for a 2,760,000-character Write). Timing regression tests
  also cover selected 23,000- and 100,000-character inputs: continuations,
  lists, overlapping letter-delimiter suffixes, long uppercase names,
  literal backslashes and `$(`. Additional cases cover repeated equals and
  255-character delimiter suffixes through 500,000 characters. They assert
  under 100 ms per output path
  at both the default payload cap and a cap that scans the full input.
  These are finite workloads measured on the test machine, not a latency
  guarantee for arbitrary text or hardware. A long string is scanned up
  to 8,192 characters beyond the internal string budget, which reserves
  512 characters below the configured cap for markers. At the default
  cap of 16,384, the scan window is at most 24,064 characters
  (16,384 - 512 + 8,192); it can be smaller when the remaining column
  budget is lower. The margin is scanned only to complete matches near
  the cut and is never kept; anything further is dropped unscanned.
  Cut rows have `is_truncated = true`.
* **Local files:** dead-letter and dry-run files hold the same redacted rows
  as the table; they are written with your user's default permissions.
* **Token:** memory only; never written to rows, files, logs or status text.
* **Identity:** `userIdFromCredentials` and `recordHostname` are off by
  default. When on, the rows carry the token's account email and the
  machine's host name (see [Identity](#identity)). `cwd` and `user_id` (the
  login name by default) are in the rows either way.

## Testing

```bash
claude plugin validate plugins/claude_code_mod
cd plugins/claude_code_mod && claude plugin test
bash tests/setup.test.sh
bash tests/version.test.sh    # plugin.json and PLUGIN_VERSION agree
node tests/reload.test.mjs    # node 24: the welcome card across a fresh load
bash tests/state-contract.test.sh   # the host's $.state, as the reload test models it
HOST_CLI_LINUX=... bash tests/managed-settings.live.sh   # docker: a managed policy in a Linux container (below)
python3 tests/views.test.py   # needs DuckDB: python3 -m pip install duckdb
PROJECT=my-gcp-project RUNNER=path/to/run_bq.py python3 tests/queries.live.py   # live: every query on a synthetic fixture (below)
npx -p typescript tsc -p .    # once the host CLI has loaded the mod (below)
```

`tsc -p .` type-checks `hooks/` and `tests/` (strict, with
`noUncheckedIndexedAccess`) against the declarations the host CLI writes to
`.claude-plugin/types/` when an interactive session loads the mod from a
folder: start one once with `--plugin-dir` pointed at this directory
(`plugin test` and `plugin validate` do not write them). The
mod's `tsconfig.json` leaves out the MCP tools' declarations there, which
depend on the servers that machine has connected.

The test command runs the `tests/*.test.ts` files against the host CLI's own
test kit, with no session, sign-in, GCP or network: `$.http.fetch`,
`$.process.run`, `$.fs` and `$.store` are stubbed, and `mock.clock` drives the
timers; the `$.fs` stubs measure files in UTF-8 bytes and refuse a file over
4 MiB, and the `$.store` stub a store over 4 MiB of JSON in all, as the host
does. The tests cover the captured rows and their spans,
subagent runs in the order the host raises them (one invocation and one
trace for the turn, a span per run, a SubagentStart that comes late or never),
the session-start row raised before or during `session.start`'s setup,
the session id after `/clear` and `/resume` with no classic hooks (a refused
read included), the non-blocking rules (`session.end` included), the queue
bound, retries, timeouts, token refresh, table creation, partial inserts, 2xx
answers that are not valid insertAll responses, dead-letter files,
replay that keeps two tables' rows apart and writes what it leaves before
emptying a file (a failed write included), completion records (a file part
written, cut short or changed is not complete; a writer and a replayer
interleaved lose no row; a file without its record is kept and reported),
backfill, the exit spill and its time bound, multibyte rows held to the
file limit in bytes, dry run,
redaction (allowlist fixtures within the depth budget and dropped deeper
values, scalar/list/object values, selected nested JSON encodings and
JSON-looking openers that are too long or broken to parse, selected
credential-bearing key names in nested objects, JSON text and tool
arguments, and fixtures for credential-name parts, endings, plural and
numbered forms, URLs and webhook URLs),
selected credential-named lines, options and assignments with literal,
quoted, expanded or uncertain value endings (including a generated sweep
of value openers),
credential names in lists of names and values (generated sweeps of
separators, spellings and options, and of expansions, command
substitutions and long values before them; the 8,454-command matrix filters
out combinations split inside a name or value and variable names that
consume the delimiter, and includes short `pass` names after punctuation)
and before an escaped `=`,
selected long continuations, lists (one that each `;` starts again too),
overlapping suffixes, uppercase names, repeated literal `\=` and `$(`
with per-path timing assertions at both payload caps,
selected ordinary text left unchanged (including locale-quoted names),
values past 5,000 characters and past the scanned window, credential
fixtures across a length cap, the exercised diagnostic sinks, and selected
review probes through capture, delivery and the dead-letter file.
Known-gap tests pin missed redaction after a quoted space and over-redaction
of non-`/` substitution patterns after an assignment; passing those tests
records the current gaps rather than successful redaction.
The tests also cover truncation (identifying fields survive a cut), refused environment reads
(`BQAA_ACCESS_TOKEN` included), a re-fired `session.start`, and
configuration.

They also cover what happens when credentials fail:

* the band, drawn through the test kit's `$.ui.mount` (its fix, its count of
  waiting rows, what other mods draw kept, a survey left alone);
* one toast and one transcript line, the status line and `/bqaa-status`;
* the fix for each token source (the default command, gcloud's own account,
  `BQAA_ACCESS_TOKEN`, another command named by its program only);
* the notice clearing on the next send, and, with nothing new queued, the
  rows parked meanwhile replayed by the retries once a sign-in works;
* a 401's immediate retry with a fresh token;
* the role to ask for on a 403;
* the backoff before the next retry capped at 30 s (the retry going at the
  next flush tick after it), and a prompt's retry from a timer;
* `prompt.submit` passing every prompt on with no request and no process
  while it runs, with the token command failing, BigQuery refusing the
  credentials, or either never answering. Each of those tests runs with a
  leftover `onAuthFailure: block` from an earlier version's settings, which
  nothing reads now.

And when BigQuery cannot find the dataset or table:

* the band, toast, transcript line, status line and `/bqaa-status`, with the
  `setup.sh` command, which sets the views dataset too;
* the command's path as one shell word (a quote or a closing backslash in it
  included), and no command for a name `setup.sh` would refuse (its views
  dataset too) or a directory that is not a POSIX path;
* rows retried, and parked as retries, never refused;
* every row landing once it is found, with new rows or with none (a session
  left idle), and a prompt's retry from a timer;
* the backoff before the next retry capped at 30 s (the retry going at the
  next flush tick after it);
* a command longer than the band's line pointed to `/bqaa-status`, which
  shows it whole; a long one that fits shown whole in the band and the
  transcript line; `/bqaa-status`'s command whole beside very long names;
  no command for a path redaction would change, or one too long to show;
  the command on a transcript line of its own for a project the redaction
  mistakes for a credential name;
* a table the mod made that BigQuery still cannot find after two minutes,
  on a send or on a replay with nothing queued, and table creation turned
  off;
* a 400 after the notice, the access-denied one included: the notice clears,
  and the rows are refused;
* a reload clearing the status line the previous load set, and the refused
  count on the status line covering every row since rows last landed;
  session.start fired again in the same load clearing the line, and the
  writer that takes over saying the same line again; a status or debug line
  the surface does not take leaving the flush going (the line written at the
  next try, a clear at session.start included); refused rows, and rows out
  of retries, whose file cannot be written said to be lost, and left out of
  the refused count.

And the replay that runs by itself while nothing is queued:

* it goes on until every parked row lands, a replay that lands only some
  included, and rewrites no file while BigQuery only asks for a row again;
* rows parked while a table the mod just made was not visible yet;
* rows parked for one table stay due while another table is configured;
* a row refused on it (insertAll's row error, a 400, 403 access denied,
  which is the notice) moves to a `rejected` file, with no further request
  or file, and `/bqaa-backfill` still sends it;
* a refused row in a `retry` file, as earlier versions rewrote them, is
  never sent by it;
* a file another session leases, then lets go: its rows land once the
  lease ends; a directory it cannot list: its rows stay due and land once
  it can;
* a file it cannot empty, after its rows land or are refused, or what is
  left of it only partly written: sent once, with no further request or
  file, the rest written under the same names and the file emptied once
  writes work, and the rows left going on; a file another session already
  wrote out, by the record it left, and one this session wrote out, after
  the next session.start: emptied, never sent; a file written out under a
  kept outcome never written again; what is left only partly written,
  finished by the writers of three later session.start runs, and by
  another session's writer, sending none of it and copying no row; an
  outcome recorded before any of what is left was written, written whole
  by the next writer; a file of what is left that `/bqaa-backfill` sent and
  emptied meanwhile never written again; a record that does not fit the
  file (another size, or fewer lines) never trusted, and a file with a
  record read part-written, written again longer or changed in place
  keeping its record and waiting until it reads whole; the writer that
  recorded an outcome, back after another writer finished it and replayed
  what it left, writing none of it again; two writers replaying one file
  at once writing what it left once; a record that cannot be read leaving
  its file waiting and due, with nothing sent; a record the store does not
  take, or one over 64 KiB, kept in the file beside the source and resumed
  from there, and the done mark left beside the emptied source; a record
  the store took
  at first and then did not, where the file's marks count too; a record
  in neither place leaving the file as it was until it can be written; a
  store record that no longer fits not hiding the one beside the file, and
  one beside it that does not fit emptied; 4,500 rows refused for as many
  reasons kept with 16 reasons in a record under 64 KiB, with the store
  holding 4 MiB in all, and none sent again; a record update beside the
  source cut off part-way, the one before it read, nothing sent again,
  and the done mark left once the source is emptied; a first record cut off
  with none whole, written
  again by the writer that holds the outcome, left waiting by any other
  replay, `/bqaa-backfill` included, and replayed afresh by
  `/bqaa-backfill force`; a
  file of what is left written while its record could not be updated, not
  replayed until the record has it, its rows then going once; one replayed
  once its parent's record names other files, and waiting while that
  record is cut off or cannot be read; while the source cannot be emptied,
  one the later record has as written replayed; a replay whose answer
  comes after another session wrote out its outcome going on with that
  one, also when that record cannot be read at first, and writing nothing
  of its own once that session has emptied the file, its mark gone or not;
  a file of what is left whose parent was emptied going whatever an older
  record of the parent says, and one the parent's done mark does not name
  emptied unsent; a record write cut off before its first byte leaving the
  file waiting; a write of a file of what is left that lands after another
  session finished the outcome and replayed that file emptied unsent; an
  outcome whose first record lands after another session finished the file
  stopping at the done mark, and a record of it the store takes after that
  session finished the file let go again; a writer back from reading the file to resume
  writing nothing once another writer finished it, its records in the store
  or beside the file; no record written again when it already holds the
  outcome; a writer that takes over in the same session holding what the
  one before held, and an outcome that one's replay comes to after the
  takeover, sending none of the file again, and sending the rows of a
  batch the one before still had out, asked for again, also through two
  takeovers, under its own queue's bounds; rows a writer parks after the
  next one took over setting that writer's paced replays going, also
  while that writer's own replay is under way; rows a writer had queued
  when it took over going behind the older ones; the exit spill after a
  takeover taking the batch the writer before still had out, that writer
  starting nothing more, and no second copy of rows an answer refused or
  parked whose write is still under way, while it takes the rows a mixed
  answer asked for again; the exit waiting, the process ending once it
  returns, for the files those rows are being written to (with nothing left
  to spill, and for writes the writer before began after the takeover, or
  the writer that took over began before it) and no longer than its bound,
  through session.end too; a status line the writer before sets after a
  takeover cleared by the next landing, and one session.start cleared said
  again by the writer that takes over; rows queued before a takeover held
  to the new writer's own, larger, bounds; records of files emptied more than a day ago emptied;
  a record another session writes late, beside a file finished meanwhile,
  never standing over its done mark, and a replay with nothing left to
  write never writing its done mark over another's; a replay whose mark
  lands after the file was finished, or after another session recorded
  its outcome, or beside another session's, sending none of it, two that
  each read the other's sending none, and the next replay sending it once;
  a replay that cannot write its mark sending none of the file, and a file
  it leaves waiting (its record cut off, unreadable or not writable,
  another writer's mark, whole or cut off, its mark not written) holding up
  none after it, and `/bqaa-backfill`'s transcript line saying why one was
  left; a session whose mark is written late, after another sent
  the file and could record its outcome nowhere, sending none of it, and
  a late or cut-off mark, or a late letting go, never taking another
  writer's mark away;
  a replay that sent rows and can record its outcome nowhere keeping every
  other session from sending the file, its writer finishing it once it
  can, and `/bqaa-backfill force` replaying it afresh once that writer is
  gone, `/bqaa-backfill` waiting as other replays do;
  a replay that changes nothing letting its mark go, and only its own; a
  mark its writer could not let go being that writer's own, its successor
  in the same session waiting on it as another session does; a file
  listed part-written and read whole waiting for the next replay; a late
  copy of a finished file emptied with its done mark left as it was, also
  when every write of that mark is cut off and the emptying fails, and one
  another session writes while a writer writes the same outcome out never
  written again by that writer; a done mark still being written when
  another writer finished the file, landing cut off, in a file of its own,
  taking nothing from that writer's; `/bqaa-backfill` sending none of a
  file another session's replay is sending, saying why in its transcript
  line, and `/bqaa-backfill force` replaying such a file afresh; a file
  another session's lease holds, and one of what is left waiting on its
  parent's record, each saying why; of two whole done marks for one file,
  every writer going by the later, by number and then by name; a writer
  whose read of the record, or of the sending marks, answers after another
  finished the file, put back by a late write, sending none of it, and one
  replaying the file afresh past a done mark cut off part-way, its read of
  the marks answering before that writer empties the file; a writer that
  finds another's sending mark standing leaving the file, whatever a later
  read of the marks would find; where the listing gives no
  times, the records of a file done more than a day ago emptied by its done
  mark's time, and a listing time of 0 taken for none; two writers of one session never giving
  two files one name; a done
  mark cut off part-way, with no other record, leaving the file waiting,
  for `/bqaa-backfill` too, and `/bqaa-backfill force` replaying it
  afresh; a file of what is left that the record
  of its emptied parent does not name emptied unsent, the done mark there
  or not; the done mark letting a later replay empty a file the store's
  record of cannot be read, and reporting the file's rows left and
  refused;
* it stops between requests for a row queued meanwhile, which goes first,
  with no backoff, and then goes on;
* a retry file it read before its completion record was written is sent
  once it is whole, and one never finished makes no request or file over
  ten minutes of paced reads; one listed empty before its write landed, or
  listed with its rows and read empty, is sent once whole, with or without
  times in the listing, and one left empty or part-written long ago, or
  emptied by its replay, keeps nothing going;
* rows refused on a send and then on a replay all counted on the status
  line, which a replay whose rows are only asked for again leaves as it
  was, and a later request of the same replay that lands rows ends; a
  replay's second 401 is the credentials notice; `/bqaa-backfill` run twice
  counts the rows it refuses again once, on the status line and while
  access is denied, and a row an earlier session refused once; a row
  refused again after rows landed, or under a later access-denied notice,
  counted again there; past the 10,000 rows a count keeps, the status line
  saying about; rows with no id each counted once, however often
  `/bqaa-backfill` sends them, and rows alike in two files counted as two; a run that only finishes a recorded outcome
  counted in its report, its refused rows included;
* the status line for rows parked after retries, with automatic replay on
  and off.

And the welcome card:

* its lines for a fresh install, an update, a dry run, capture off and no
  project or dataset, drawn through `$.ui.mount`, in a band drawn before the
  session started too, with its transcript line;
* the first rows landing, from a send or from a replay, with one toast and
  the count going up; a problem notice holding the band until then;
* the next prompt typed at the terminal after rows land, or Got it, closing
  it (a prompt from elsewhere leaves it), and the records that keep later
  sessions from showing it; a reload in its session showing it again as it
  was; no card, line, record or session state in a run with no one at the
  prompt; no card and no line for a session whose id is not known when it
  starts; no card and no line for a session the store has closed, its
  greeting never written, its read of that close answering or not; no card
  and no line while the store cannot list the versions greeted before, and
  the card naming the one it replaced once it can; a landing whose store
  write is refused written once more rows land; a flush interval of no
  whole number of seconds said in milliseconds; an
  update back to an older version greeted as a change;
* a store read or write that never answers holding up nothing, the session's
  start included; rows that land while the store is read; a read that
  answers after `session.start` ran again, or while a later run of it is
  still setting up;
* the writes: a close made while the greeting, or a landing, is still
  being written, written at once and staying closed; the greeting line
  written once by the run that showed the card, when `session.start` runs
  again before the card's state write answers; a reload while either is still being
  written showing the card as it is, its count kept, with no second line; a
  landing an earlier load is still writing landing before or after the
  close a reload's Got it writes, the card staying closed; a newer version
  another session greets, before or while this card writes, kept, and a
  late write of an older version's never greeting this one again; the
  version it replaced, the newest greeted before; "updated" only for a
  newer version, pre-releases part by part; the card's write queue on its
  own, a greeting or landing whose write fails going again at the card's
  next change; rows the writer before a reload lands counting on the card after
  it; a close whose session-state write is refused hiding the card, and
  the store's close keeping it hidden after a reload;
* a reload to another table, or out of a dry run, waiting for rows to land
  there, rows of a send or a dry-run write still in flight across it
  included, and, while its read of the store waits, claiming no landing
  there, a prompt then leaving the card open; a reload to capture off
  keeping the card until Got it; the
  writer saying where rows landed, per replay request before its file is
  emptied, and for a dry run's write at session end;
* its border only where the band has the rows for one, and a survey left
  alone.

And the other additions:

* permission decisions: an accept during the call, a rejection after it, a
  decision that never comes, session end, a record a later hook refuses,
  odd values, a subagent's call;
* the identity options: off by default, the lookup sent in a POST body only,
  an unverified or missing email, a retry after ten minutes, parked and
  replayed rows, the host name;
* a credential change: after a session change, a 401's fresh token for
  another account gives the retry and later rows that account; rows parked
  under the first account keep it when replayed; a new token whose account
  is unknown, or no working token, gives the configured `user_id`; a token
  that expires with no fresh one to follow leaves rows parked then (at retry
  exhaustion and at exit, also when the session ends before any send asked
  for a fresh token) with the configured `user_id`, which replay under
  another account keeps; a token that expires or is refused loses its
  looked-up account, so the same token issued again is looked up again; an
  account that a slow lookup finds after its token was let go is not used
  for the next token; and a lookup that answers late, for an older token,
  does not replace the newer account.

`tests/setup.test.sh` runs `setup.sh` against a stand-in `bq` that logs its
calls. It checks:

* the datasets, table and views it makes, on a first run and on a second;
* the fallback variables;
* that a dry run calls nothing;
* that names which are not plain ones are refused before any call;
* that the rendered views keep to this mod's rows;
* that the command the mod prints for a missing dataset makes the views in
  `<dataset>_reporting` even when the shell already sets `BQAA_VIEWS_DATASET`
  (valid or not), or another project, dataset or table.

`tests/access.live.sh` checks, against a real project, the access that
[Service-account auth](docs/service-account-auth.md) sets up: the writer
service account holds only `bigquery.tables.get` and
`bigquery.tables.updateData` on the table, and is refused a read of its rows
and any query; a reader queries a view, and is refused the table, directly
and in a query; and neither dataset gives the project's Viewers or Editors
access. It changes nothing (`INSERT_PROBE=1` adds one insert).

`tests/state-contract.test.sh` checks, with the host CLI's own test runtime,
what `tests/reload.test.mjs` models of the host's `$.state`, so its stand-in
never sets its own rules: a small mod of its own (`tests/state-contract`,
its check kept as `state.contract.ts`, a name this mod's tests leave alone)
reads one value, waits while another dispatch writes, then writes at the
version it read: the other write is not seen until a write of its own,
landed or missed, brings the whole moment up to date.

`tests/version.test.sh` checks that `plugin.json` and `PLUGIN_VERSION` name
the same version: the welcome card greets an update by the latter.

`tests/managed-settings.live.sh` runs the
[managed-settings](docs/managed-settings.md) guide in a Linux container, as
an ordinary user under a root-owned policy. Offline, with the folder
marketplace, it checks:

* that the policy's options and environment pins hold against the person's
  own settings, a `--settings` file, a repository's settings and the shell:
  destination, capture, live writes, token command, the folder rows wait
  in, agent name and identity;
* that options a policy leaves out are the person's;
* the move from the settings-hook tracer: it keeps writing until the policy
  turns it off (and `allowManagedModsOnly` does not stop it);
* what a person can try: `enabledPlugins`, `plugin disable` and
  `plugin uninstall` change nothing, while safe and bare mode load no
  plugin;
* that the policy works as a drop-in beside an existing
  `managed-settings.json`;
* that a repository's `USER` becomes `user_id`;
* that a repository's settings `env` reaches the token command, and that an
  admin-owned wrapper with a clean environment keeps it out;
* the emergency switches;
* rollback: removing the policy straight after a migration brings the
  tracer back, while a disable policy, kept until the earlier installs are
  gone, does not;
* the rows left on disk.

With `BQAA_CHECK_NETWORK=1` it also checks the GitHub variant pinned to a
tag: the tag's copy, `autoUpdate` off, when a fresh machine starts
recording, the user tier, and a rollback that leaves no copy installed. It
needs docker and the host CLI's Linux
build (`HOST_CLI_LINUX`).

`tests/reload.test.mjs` loads `hooks/register.ts` afresh, as a hot reload
does, which the test kit cannot, under node 24 with a small stand-in for
`$` (its `$.state` held for the session, its `$.store` across loads, store
writes and reads held and let go in any order). Its `$.state` keeps a
version for each value and lands a conditional write only at it, and every
`$.state.get` of one event reads one moment until a write of its own, landed
or missed, brings the whole moment up to date (a new version for each
write that lands; the code compares a value's version only with that
value's own, as the host's test runtime gives no more); its writes can be held or
refused, its reads held. Its `$.clock.sleep` answers only in the newest
load: a hot reload cancels the waits of the load it replaces. A load may be
of another version of the code, its config naming that version, as after
an update in the session (node's module hooks; no file is written). It
checks that a card closed
before a reload stays closed while the close is still being written; that
a greeting still being written gives one transcript line across loads,
also with its state write in flight; that a landing an earlier load is
still writing never undoes the close a later load writes, before or after
it, nor in a new process with no `$.state`; that an earlier load's late
store read or late dry-run write leaves a later load's close standing,
and its late rows count on the card the later load shows, with no toast
from the earlier load; that a close whose state write was refused, but
which the store has, keeps the card closed, and is written back to the
state by the next load where it can be; that state writes answering late,
a close and then an earlier landing, leave it closed; that a greeting
an earlier load never got written is written by the load that reopens the
card; that Got it, while the greeting or a landing is still being written
and with its state close refused, puts the close in the store at once; that
a new load draws the card only once its reads of the store answer, never
when they do not, nor while its read of a close the store has waits; that a
load whose reads answer after a later load took the card over for another
destination leaves it to that load, its rows counting nowhere, and so does
one whose read of the state answers late; that rows a new load lands while
its reads wait count once, over repeated reloads, and once it takes the card
over and moves it where it sends them; that a card moved away and back
counts from the load that moved it back, no row of an earlier load counting
there again; that a new load that cannot read the store
shows no card; that a reload whose takeover write answers after another
load's Got it (its state close refused) shows no card; that of two loads
whose first card writes wait, the later takes the card over when the
earlier's lands first, with one greeting; that a prompt after rows landed,
its state close refused, closes the card here and in the store; that
loads in the same millisecond, or with the clock set back, rank by when
they started; that a load replaced while its read or write of the count is
still out (its cancelled wait never answering, or refused) takes nothing
over, the later load keeping its destination, counting its rows and closing
on the next prompt; that a close an earlier load writes late, its state
close refused, hides the card a later load took over once it lands; that
a session resumed with no state after Got it, the greeting's write lost,
shows no card and greets no more, its read of the close answering or not;
that a run whose read of the state answers after a later load showed
another version's card leaves that card alone, with reads that see what
stands at once too; that a read of the close that never answers stops no
read of it ten seconds on, and one that answers after the next began
leaves that one's turn alone; that its own `$.state` reads one moment an
event, brought up to date whole by a conditional write of its own, landed
or missed; that no row of the version before an update counts on the
updated version's card, whenever it lands or is recorded: after the card
opened, after the earlier card was closed, while the session showed no
card, while the update read the store, recorded late, or with a later total
of its recorded first, also across a reset of the session's state, with
no session start, between the earlier load's rows and the update, its
record's write held from before or after the reset until the update's card
opened; that the
updated version's own rows count once,
whatever order their records land in, also one that landed while its
card's first write waited, its first landing and toast its own when a row
of the version before lands meanwhile; that a load whose rank came back
after a later load took one counts none of its rows; that a run whose card
the session's state was reset under (`/clear`) shows none after it, a card
it was writing then going again, also in a load that never hears of the
reset (its write shows it), which leaves the next update's card, and a card
of this session the next run showed again, as they are; that a card of the
lifetime before a reset, of whatever rank, keeps no update from showing its
card, and is taken over, with no count and no landing of its own lifetime,
by the session's next run; that a card a run wrote as a reset came goes
again even when that run's own rows counted on it meanwhile; that a landing an
earlier load never got written is written by the load that takes the card
over, and a store close whose write was refused goes again at the next
flush; that where no session id is
known no card shows and no one is greeted, after a reload too;
that a Got it, a takeover's stored close or a
prompt in a load of the
version before an update, its write or its reads answering late, never
closes the card of the version after it; that of the first card writes of
the versions before and after an update, landing in either order, the
later version's card shows, with its own greeting; and that draws of the
card while its read of the close waits start no other read of it.

`tests/queries.live.py` runs every query in `sql/value_queries.sql` and
`sql/sample_queries.sql` through the analysis skill's runner (named by
`RUNNER`: it is in the mod's source repository) against a synthetic fixture
in a dataset of its own, which it makes and deletes. The
fixture holds a replayed row, a session with no `user_id`, the settings-hook
tracer's and an ADK agent's rows, a subagent run, both answers to a
permission prompt, and a hook's denial. The test checks the answers, and
prints each query's dry-run bytes. It needs Application Default Credentials
that may create a dataset.

`tests/views.test.py` runs both views in DuckDB. It uses five dialect
substitutions, and each must match. The fixture is a table shared by this
mod, an ADK-style writer, the settings-hook plugin, a duplicate row, another
event type and a row without `event_id`. The test checks requests, sessions
and token totals together. It also checks that without the writer filter
the same views count other producers' requests but not their tokens.

## Limitations

* Mods are early access; the events and `$` methods can change between
  host CLI releases. Tested with 2.1.296, 2.1.299, 2.1.302 and 2.1.304.
* A subagent's run is told from a main-loop turn by the loop id its
  `turn.start` carries (`agentId`, in the host's type declarations from
  2.1.299). A host that raised `turn.start` for a subagent without it would
  have that run recorded as a main-loop turn.
* Writes use `tabledata.insertAll` (the legacy streaming API) because the
  Storage Write API is gRPC only and `$.http.fetch` is HTTP with text bodies.
  insertAll's quotas and pricing apply, and its `insertId` dedup is best
  effort; use `event_id` in queries.
* `LLM_REQUEST` rows carry the user's prompt on a turn's first request and no
  conversation history or system prompt (reading them per request would put
  `$.session.messages()` on the hot path).
* `content_parts` is always empty; attachments on a prompt are listed by type
  only.
* A tool's `total_ms` is the whole `tool.call` chain: permission prompts
  (flagged), `PreToolUse` hooks and the tool. A model request's `total_ms` is
  the stream through the mod's hook; `time_to_first_token_ms` is the first
  text, thinking or tool chunk.
* A subagent's span hangs under its turn, not under the `Agent` tool call that
  started it (the events carry no reliable link between the two).
* No `HITL_*` rows. The answer to a permission prompt is recorded on
  `TOOL_COMPLETED` (`permission_decision`, `permission_decision_source`)
  from the host's `tool_decision` record. A rejection arriving more than
  2 seconds after its call returned is not recorded.
* Rows in the in-memory queue are lost if the hooks module reloads (a
  `--plugin-dir` edit, `/reload-plugins`) or the hooks worker crashes:
  at most one flush interval's worth, plus any batch in backoff.
* Rows queued at exit reach BigQuery when a later session starts (15 seconds
  in) or `/bqaa-backfill` runs; until then they wait in a spill file.
* Rows parked for a table are sent only by a session configured for that
  table: after switching tables, run with the old table's settings to send
  what it left behind.
* `$.fs` cannot append, rename or delete: one file per failure, truncated
  rather than removed after backfill.
* A dead-letter file without a matching completion record is never replayed.
  One left by a writer that stopped part-way, or written by an earlier
  version of this mod, stays on disk, listed by `/bqaa-status`, for a person
  to look at.
* Redaction keeps a value only under a listed key: an MCP tool's own
  arguments, structured results and JSON output, and anything else under a
  key that is not on the list, are recorded as `[REDACTED]`. Free text is
  scanned for credentials on a best-effort basis.
* Free-text parsing is best effort. The direct named-value reader has a
  100-character name bound and the forward shell-name readers a
  200-character bound. The final list-name fallback has no name-length
  bound: it scans each candidate segment before `=` regardless of length,
  including 201-character and longer names. The scan margin extends 8,192
  characters beyond the internal budget, after reserving 512 characters
  from the configured cap for markers: at most 24,064 characters at the
  default 16,384-character cap. Text beyond the window is dropped. The
  word-start lookup looks back at most 4,096 characters. This bounds that
  lookup only: value and name readers can scan farther, and a detected
  credential can preserve later lines when its value ends unambiguously.
  The command-string heuristic reads only the first 400 characters of a word. These bounds do not constitute
  general shell parsing. Known gaps are tracked in
  [#546](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/546):
  * **Known gap — backslash-spelled name after a quoted space:**
    `sh -c "gcloud run deploy svc --set-env-vars=VERSION=\"a b\",DB_PA\\SSWORD=CanaryValue"`
    and `INFO {"command":"gcloud run deploy svc --set-env-vars=VERSION=\"a b\",DB_PA\\SSWORD=CanaryValue"}`
    are returned unchanged by the free-text redactor, retaining
    `CanaryValue` in the eight tested output paths. The backslash/quote
    heuristic restarts at the earlier raw space. Main also misses these
    two inputs; the quoted-name sibling `DB_PASS"WORD"` is a separate
    positive fixture.
  * **Known gap — non-`/` substitution delimiters after an assignment:**
    `x=1 sed 's#pass"word"=.*#x#' config.ini` becomes
    `x=1 sed 's#pass"word"=[REDACTED]'`, losing the pattern suffix and
    filename. The pinned `#`, `:` and `%` examples after `x=1` or `FOO=bar`
    have this over-redaction. The literal-quote guard special-cases `/`;
    tests preserving `/` patterns do not establish other-delimiter coverage.
  * A provider-token-shaped name can be rewritten before the named-value
    pass sees it. For example,
    `--set-env-vars=VERSION=v,github_pat_PRODUCTION_ACCESS_TOKEN=CanaryValue`
    keeps `CanaryValue` after the name becomes a redaction marker.
  * Listed entries after a credential can be swallowed with its value:
    `--set-env-vars=DB_PASSWORD=x,max_tokens=1024` becomes
    `--set-env-vars=DB_PASSWORD=[REDACTED]`.
  * ANSI-C option names containing a decoded NUL are not truncated as the
    shell truncates them: `cli $'--password\0' CanaryValue`,
    `cli $'--password\x00ignored' CanaryValue`,
    `cli --pass$'\0'word CanaryValue` and
    `cli $'--oauth2-bearer\0ignored' CanaryValue` keep the value.
  * URL user-info stops at the first `@`, so
    `curl https://u:prefix@CanaryValue@example.test/path` keeps the password
    suffix `CanaryValue`.
  * URL parameter values containing shell expansions can outlast the URL
    scan: `curl https://example.test/?token=$(printf %s CanaryValue)`,
    `curl https://example.test/?token=$'a CanaryValue'` and
    `curl https://example.test/?token=${VALUE:-a CanaryValue}` can keep a
    suffix. Escaped spaces (`?token=a\ CanaryValue` and
    `?token=$TOKEN\ CanaryValue`) are covered by regression tests and are
    no longer an open limitation.
  * A value after a redaction marker can keep a suffix when its shell end
    is uncertain: `"^q^VERSION=$(printf v)qpass=[REDACTED]&CanaryValue"`,
    also with a provider-shaped token in place of `[REDACTED]`.
  * A delimiter-looking earlier value can hide the declared delimiter:
    `"^#^VERSION=hello ^x^#DB_PASSWORD=first&CanaryValue"` and
    `"^q^VERSION=$(printf ^x^)qpass=CanaryValue"` can keep a suffix.
  * A declared delimiter can split an allowlisted spelling:
    `^.^VERSION=max.tokens=CanaryValue` and
    `^-^VERSION=output-tokens=CanaryValue` can keep the value of `tokens`.
  * Shell-spelled delimiters (`^$'\x71'^VERSION=vqpass=CanaryValue` and
    `$'^\x71^VERSION=vqpass=CanaryValue'`), delimiters after quoted option
    names (`--set-env-"vars"=^q^VERSION=vqpass=CanaryValue`), and delimiters
    of 256 characters or more can be missed. The delimiter reader inspects
    at most 255 characters between the opening and closing `^`.
  * Locale-quoted option names with separate values
    (`cli --pass$"word" CanaryValue`) remain best effort. Locale-quoted list
    names such as `DB_PASS$"WORD"=CanaryValue` and
    `p$"a"ssword=CanaryValue` have explicit regression cases; these finite
    examples do not establish coverage of every locale-quoted spelling.
    ANSI-C names are
    checked by explicit regression cases, including names after earlier
    substitutions or escapes and beyond the forward heuristic's window;
    this is not a guarantee for every `$'…'` spelling or nesting depth.
* Timestamps have millisecond precision.
* An organization's web-fetch policy or `allowManagedModsOnly` can stop the
  mod; `/bqaa-status` and the debug log (start the CLI with `--debug`) say why.
* Where an organization manages the host CLI, `classic.SessionStart`,
  `classic.SubagentStart` and `classic.PermissionRequest` pass over a plugin
  a person installs (`claude plugin validate` says so). Installed that way,
  the mod records no session-start row, records each subagent with an
  inferred `AGENT_STARTING` row typed `subagent`, and flags no permission
  prompts; rows still carry the current session id.

## Files

```
QUICKSTART.md                a sandbox, joining a team's setup, and the administrator's steps
.claude-plugin/plugin.json   manifest and userConfig
hooks/hooks.json             names the hooks module
hooks/register.ts            hooks and $ wiring
hooks/lib/tracer.ts          event -> BQAA row, ids and spans
hooks/lib/rows.ts            row builder, BQAA schema
hooks/lib/allowlist.ts       the keys whose values a row keeps
hooks/lib/sanitize.ts        redaction, truncation
hooks/lib/queue.ts           bounded queue
hooks/lib/writer.ts          background writer, retries, dead letter, backfill, exit spill
hooks/lib/bigquery.ts        insertAll / tables.insert requests, answer classification
hooks/lib/deadletter.ts      dead-letter file format, files sized in UTF-8 bytes
hooks/lib/config.ts          options and environment
hooks/lib/status.ts          /bqaa-status text
hooks/lib/welcome.ts         the welcome card's text, and its records in the store: the version greeted, the card's landing, its close
setup.sh                     creates the dataset, table, views dataset and views (bq CLI)
docs/service-account-auth.md which identity writes where, who reads the views, offboarding
docs/managed-settings.md     turning the mod on for a team with the host's managed settings
docs/value.md                what each persona learns, with its query, denominators and limits
sql/                         DDL, sample queries and the value queries
sql/views/                   the usage views setup.sh makes
examples/sample-rows.jsonl   fixture output
tests/                       plugin-test suites, setup.test.sh for setup.sh, version.test.sh, reload.test.mjs, access.live.sh and queries.live.py (both live)
types/index.d.ts             the values the mod keeps in the session's $.state
```

The credential notice, the identity options, the permission decisions,
`setup.sh` and the views follow ideas from the upstream reference mod
(Apache-2.0). They were reimplemented, and no code was copied.
