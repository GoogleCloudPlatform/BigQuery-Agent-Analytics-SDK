# BigQuery Agent Analytics Dashboard — User Manual

This guide is for people who **use** the dashboard: you run agents that log to
BigQuery through the [ADK BigQuery Agent Analytics plugin](https://adk.dev/observability/bigquery-agent-analytics/),
and you want charts. You do not need to install anything, write SQL, or read
the rest of this repository. If you want to change or validate the dashboard
itself, see the [contributor README](README.md) instead.

**What you get:** your own copy of an 8-page Looker Studio dashboard — token
consumption, sessions, tool usage, LLM calls, user analytics, latency, tool
errors, and a trace inspector — built on the `agent_events` table your agents
already write. Your copy is private, reads your data with your credentials, and bills
your project. Setting it up takes about five minutes.

---

## Before you start

You need three things:

1. **A BQAA table with data in it.** If your agents run with the ADK BigQuery
   Agent Analytics plugin, this already exists — it is the table the plugin
   writes to, normally named `agent_events`.
2. **A Google account that can read that table _and_ run BigQuery jobs** in
   the project that will pay for queries. These are two separate permissions:
   - *Read:* open the table in the
     [BigQuery console](https://console.cloud.google.com/bigquery) and click
     **Preview**. Seeing rows proves read access — and only read access.
   - *Run jobs:* in that same console window, run a tiny query such as
     `SELECT COUNT(*)` on the table. If it completes, you can run jobs in the
     selected project. If it fails with a permission error, ask your admin
     for the **BigQuery Job User** role in the project that will pay for
     queries.
3. **A desktop browser window at least 1280 pixels wide.** The dashboard is a
   desktop layout; phones and narrow tablets are not supported.

You will be asked for one identifier: the **fully qualified table ID**,
`project.dataset.table`. If you don't know it offhand, open your table in
the BigQuery console — the table header has a copy control for exactly that
full ID.

---

## Create your dashboard in three steps

### Step 1 — Fill in the configurator

Open the configurator:
**<https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/>**

There is one field: the **fully qualified BQAA table ID**. Type it as
`project.dataset.table`, or let one paste do it — paste **any of these**
and the field fills itself:

| What you paste | Example | Where to copy it |
|---|---|---|
| Fully qualified table ID | `my-project.my_dataset.agent_events` | BigQuery console table header → copy table ID |
| The same, with backticks or a trailing `;` or `,` | `` `my-project.my_dataset.agent_events`; `` | Copied out of a SQL editor or an ID list |
| Legacy colon form | `my-project:my_dataset.agent_events` | Older tools and docs |
| BigQuery Console table link | `https://console.cloud.google.com/bigquery?ws=…` | Your browser's address bar while viewing the table |

For the console link: open your table in the BigQuery console so it is the
table you're looking at, then copy the address-bar URL and paste it. The
configurator reads the project, dataset, and table out of the link and
shows the clean dotted ID in the field.

A link is only accepted when it clearly names exactly one table. If it
doesn't — for example your workspace has several different tables open — the
pasted text stays in the field with an error explaining the problem, and
the **Create** and **Copy** buttons stay disabled. Close the extra tabs in
the BigQuery console (or type the dotted ID by hand) and try again.

When the ID is valid, the status line reads
**Ready for `project.dataset.table`.** Editing the value in any way switches
the buttons off again until the new value validates.

### Step 2 — Click "Create my dashboard"

The button opens Looker Studio in a new tab with your copy of the dashboard
template, already pointed at your table. **Building your copy can take about
10 seconds, and the tab may briefly show a loading, not-found, or error page
while Google provisions the report — don't close it.** It resolves on its
own — with one exception: a dialog quoting **"This report isn't shared with
you"** is terminal and will not resolve by waiting. It means the template
itself is unavailable to your signed-in account, not that anything is wrong
with your setup — see [Troubleshooting](#troubleshooting).

When Looker Studio asks, **authorize BigQuery access** — this is Google
asking for your consent, on your account; nothing is shared with the
template's owner.

Once the report itself appears, the charts take longer than the controls.
Allow up to 90 seconds on a cold load before every chart is painted.

### Step 3 — Save your copy, then secure it

In Looker Studio:

1. Select **Edit and share** to save the report to your account.
2. Keep the new report **private** while you configure its data source — and
   until every step below is done.
3. Open **Resource → Manage added data sources → Edit**.
4. Set **Data credentials** to **Viewer**.
5. Before sharing widely, run **both** of these tests:
   - Share with an account that has **no access to the BigQuery table**. It
     must see errors or empty charts, **not data**. If it sees data, the
     report is still on Owner's credentials — go back to step 4.
   - Share with an account that **should** have access (table read plus
     permission to run BigQuery jobs in the billing project — the same two
     permissions from [Before you start](#before-you-start)). It must render
     charts.

Step 4 is the one that matters most: with Viewer's credentials, every person
you share the report with sees data only through *their own* BigQuery access.
With Owner's credentials (which the creation dialog may default to), everyone
you share with would see the data using **your** access — which is exactly why
a viewer test that shows data can be a *failure*, not a success. Step 5's two
checks prove both halves: no-access accounts are locked out, authorized
viewers get charts. The configurator page has a **Copy security checklist**
button with a compact version of these steps, ready to paste into a handoff
note.

That's it — you now have your own dashboard.

---

## Reading the dashboard

### The eight pages

| Page | What it answers |
|---|---|
| **Token Consumption** | How many tokens are my agents using, over time and by agent? |
| **Agent & Sessions** | How many sessions and traces, and which agents are busiest? |
| **Tool Usage** | Which tools are called, how often, completed by which agent? |
| **LLM Interactions** | How many model calls, and how are they trending? |
| **User Analytics** | Who uses the agents most — events, sessions, tokens, traces per user? |
| **Latency** | How slow are LLM and tool calls — averages, p50/p75/p90/p99, trends? |
| **Errors** | How many **tool** errors, and which agents and tools produce them? (LLM errors are not charted in v1.) |
| **Trace Inspector** | Drill into individual events: timestamp, type, agent, user, trace, span, status. |

### The date control

All eight pages share **one date range control**. It defaults to a rolling
90-day window including today, and a change on any page follows you to every
other page, including the Trace Inspector. Shorter windows are cheaper and
faster — pick the shortest range that answers your question.

### Four things worth knowing

- **First paint is not instant.** A cold load or page switch can take up to 90
  seconds before every chart on the page is drawn. The report controls appear
  first; the charts catch up.
- **Collapse the left navigation drawer.** At the 1280-pixel minimum width,
  Looker Studio's expanded drawer overlays the report's left edge. Collapse it
  to see the full page.
- **"Data Last Updated" in the footer is not your data's freshness.** It is
  Looker Studio's connector refresh time. Your newest events may be newer or
  older than that stamp.
- **Total tokens can exceed prompt plus completion.** The dashboard uses the
  provider-reported total token count. Gemini telemetry can include thinking
  tokens in that total, so prompt and completion counts in the source data may
  not add up to the displayed total. The dashboard does not chart thinking
  tokens separately.

---

## Everyday tasks

### Share a ready-to-use setup link with your team

The configurator accepts prefilled identifiers in the URL:

```text
https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/?project=PROJECT_ID&dataset=DATASET_ID&table=agent_events
```

Fill in your values, or click **Copy setup link** on the configurator after
entering them. Teammates open the link, click **Create my dashboard**, and get
their own private copy over the same table — no identifiers to retype.

### Bill queries to a different project

If your team separates data storage from query billing, expand **Advanced
settings** on the configurator and enter a **Billing project ID**. Dashboard
queries then run (and are billed) in that project. You need permission to run
BigQuery jobs there.

### Keep query costs predictable

Every chart reads one date-pruned query over your table, so cost tracks the
date window you select and how much you interact:

- Prefer short date ranges; the 90-day default is a get-started view, not a
  recommendation.
- Keep the table partitioned on its event timestamp (the ADK plugin's default
  setup does this) so the date control prunes what BigQuery scans.
- If costs matter to your team at scale, your admins can find deeper operating
  guidance in the [contributor README](README.md#large-table-operating-guidance).

### Check compatibility before creating (optional, needs a terminal)

If you'd like a preflight — for a non-standard setup, or to validate the table
before rolling the dashboard out — you need the `bq` CLI installed and
authenticated, plus one Python package. Install the package first:

```sh
python3 -m pip install pyyaml
```

Then clone this repository and run the helper, using your dataset's actual
location (shown in the dataset's **Details** panel in the BigQuery console —
for example `US`, `EU`, or `us-central1`; a mismatched location makes
BigQuery reject the job):

```sh
cd dashboard/looker_studio
python3 tools/hydrate_dashboard.py \
  --project YOUR_PROJECT_ID \
  --dataset YOUR_DATASET_ID \
  --table agent_events \
  --location YOUR_DATASET_LOCATION
```

It verifies the required columns and prints the same kind of creation URL the
configurator produces.

---

## Troubleshooting

| What you see | What's happening and what to do |
|---|---|
| The new tab shows loading, not-found, or an error page right after clicking **Create my dashboard** | Google is still provisioning your report copy — this resolves within about 10 seconds. Don't close the tab. If it's still broken after a minute, close it and click the button again. (Exception: the "This report isn't shared with you" dialog below never resolves by waiting or retrying.) |
| The new tab shows **"This report isn't shared with you"** with *Reload* / *Return to report list* / *Go to report template* buttons | The shared dashboard *template* is unavailable to the Google account signed in to that tab — nothing is wrong with your project, dataset, table, access, or the identifiers you entered; they haven't been consulted yet. First check the tab is using the account you intend (Looker Studio uses the browser's default Google account — switch accounts or use a profile signed in to only the right one, then click **Create my dashboard** again). If you're on a work or school account, your own organization's sharing policy can also block receiving Looker Studio assets from outside domains — try a personal account if you can. If the dialog still persists, the template's copy path is blocked: report it on [issue #445](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/445) — say only whether your signed-in account is personal or part of an organization; do not post the account's email address. While it's blocked, the [compatibility check](#check-compatibility-before-creating-optional-needs-a-terminal) still validates your table (it is not a workaround — its URL copies the same template), and the repository's [Grafana dashboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/grafana) works independently of Looker Studio. |
| Charts blank or trickling in after opening a page | Normal on a cold load — allow up to 90 seconds. If a chart is still empty after that, widen the date range: your table may have no events in the selected window. |
| Layout looks cut off on the left | Collapse the Looker Studio navigation drawer, and make the window at least 1280 px wide. Phones and narrow tablets aren't supported. |
| Bottom charts clipped on Token Consumption or Latency | You're on a copy created before 2026-07-29, which keeps the old page geometry. Create a fresh copy from the configurator. |
| Pasted a console link but the field shows an error instead of the dotted ID | The link must name exactly one table. Open the table itself in the BigQuery console (close other table tabs), copy the address-bar URL, and paste again — or just paste the dotted `project.dataset.table` ID from the table header's copy control. |
| The table-ID field shows a red validation error | The error names what to fix. A *segment* error points at one part of `project.dataset.table`: project segments are 6–30 lowercase characters; dataset segments allow letters, digits, and underscores (no hyphens — that's a BigQuery rule); table segments also allow hyphens. Otherwise the value isn't three dot-separated segments — check for a missing dot or an extra one. |
| Looker Studio asks me to sign in or authorize | Expected. The dashboard uses your credentials to read your data. Authorize BigQuery access on your own account. |
| "Not found: Table …" in Looker Studio | One of the ID's three segments is wrong, or your account can't read the table. Open the table in the BigQuery console to confirm the exact ID and your access, then re-create from the configurator. |
| Permission errors on charts | Your account needs to read the table *and* run BigQuery jobs in the billing project (see [Before you start](#before-you-start) for how to check each). If you set an Advanced billing project, you need the **BigQuery Job User** role there. |
| Quota or reservation errors on charts | The billing project has hit a BigQuery limit — the error names which one, and the reset behavior depends on that specific limit (many daily quotas replenish at intervals throughout the day; custom query quotas reset at midnight Pacific). Shortening the date range reduces what each chart scans. If the limit keeps biting, ask the billing project's administrator to raise that quota; if the project uses reservations, capacity is the administrator's dial, not a quota reset. Granting more IAM access will not fix a quota error. |
| Numbers look stale | Check the date range includes today, then use Looker Studio's refresh. Ignore the footer's "Data Last Updated" — it's a connector timestamp, not your latest event. |
| A colleague I shared with sees an error instead of data | Working as intended if they lack BigQuery access — the report uses Viewer's credentials (see Step 3), so each viewer needs read access to the table *and* permission to run BigQuery jobs in the billing project. Grant those, or don't. |

Still stuck? Open an issue:
<https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues>.

---

## Privacy and cost, in plain terms

- **Your data never leaves your control.** The template is public, but your
  copy creates its own data source with your credentials, reads your table,
  and bills your project. The template owner cannot see your data.
- **Viewers bring their own access.** With the Step 3 credentials setting,
  sharing the report never shares the data — each viewer needs their own
  BigQuery table read access and their own permission to run BigQuery jobs
  in the billing project.
- **You pay only for BigQuery queries your charts run.** No services are
  installed, and the dashboard creates no BigQuery objects in your project.
