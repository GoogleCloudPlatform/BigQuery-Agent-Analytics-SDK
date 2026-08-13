# BigQuery Agent Analytics Dashboard — User Manual

This guide is for people who **use** the dashboard: you run agents that log to
BigQuery through the [ADK BigQuery Agent Analytics plugin](https://adk.dev/observability/bigquery-agent-analytics/),
and you want charts. You do not need to install anything, write SQL, or read
the rest of this repository. If you want to change or validate the dashboard
itself, see the [contributor README](README.md) instead.

**What you get:** your own copy of an 8-page Looker Studio dashboard — token
consumption, sessions, tool usage, LLM calls, user analytics, latency, errors,
and a trace inspector — built on the `agent_events` table your agents already
write. Your copy is private, reads your data with your credentials, and bills
your project. Setting it up takes about five minutes.

---

## Before you start

You need three things:

1. **A BQAA table with data in it.** If your agents run with the ADK BigQuery
   Agent Analytics plugin, this already exists — it is the table the plugin
   writes to, normally named `agent_events`.
2. **A Google account that can read that table and run BigQuery jobs** in the
   project that will pay for queries. If you can open the table in the
   [BigQuery console](https://console.cloud.google.com/bigquery) and click
   **Preview**, you are set.
3. **A desktop browser window at least 1280 pixels wide.** The dashboard is a
   desktop layout; phones and narrow tablets are not supported.

You will be asked to know three identifiers: your **project ID**, **dataset
ID**, and **table ID**. If you don't know them offhand, open your table in the
BigQuery console — the breadcrumb at the top reads
`project / dataset / table`, and the table header has a copy control for the
full ID.

---

## Create your dashboard in three steps

### Step 1 — Fill in the configurator

Open the configurator:
**<https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/>**

You can fill the three fields by hand, or let one paste do it. Paste **any of
these into any of the three fields** and all three fill at once:

| What you paste | Example | Where to copy it |
|---|---|---|
| Fully qualified table ID | `my-project.my_dataset.agent_events` | BigQuery console table header → copy table ID |
| The same, with backticks or a trailing `;` | `` `my-project.my_dataset.agent_events`; `` | Copied out of a SQL editor |
| Legacy colon form | `my-project:my_dataset.agent_events` | Older tools and docs |
| BigQuery Console table link | `https://console.cloud.google.com/bigquery?ws=…` | Your browser's address bar while viewing the table |

For the console link: open your table in the BigQuery console so it is the
table you're looking at, then copy the address-bar URL and paste it. The
configurator reads the project, dataset, and table out of the link and fills
the fields — you'll see a confirmation like *Split
"my-project.my_dataset.agent_events" into the three fields.*

A link is only accepted when it clearly names exactly one table. If it
doesn't — for example your workspace has several different tables open — the
paste lands as ordinary text in the one field and shows a validation error.
Nothing else is overwritten; close the extra tabs in the BigQuery console (or
type the IDs by hand) and try again.

When every field is valid, the status line reads
**Ready for `project.dataset.table`.**

### Step 2 — Click "Create my dashboard"

The button opens Looker Studio with your copy of the dashboard template,
already pointed at your table. When Looker Studio asks, **authorize BigQuery
access** — this is Google asking for your consent, on your account; nothing is
shared with the template's owner.

The first screen can take a moment. Allow up to 90 seconds on a cold load
before every chart is painted.

### Step 3 — Save your copy, then secure it

In Looker Studio:

1. Select **Edit and share** to save the report to your account.
2. Keep the new report **private** for now.
3. Open **Resource → Manage added data sources → Edit**.
4. Set **Data credentials** to **Viewer** before you share the report with
   anyone.

That last step matters: with Viewer's credentials, every person you share the
report with sees data only if *they* can read the underlying BigQuery table.
With Owner's credentials (which the creation dialog may default to), everyone
you share with would see the data using **your** access. The configurator page
has a **Copy security checklist** button with these same steps, ready to paste
into a handoff note.

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
| **Errors** | How many errors, and which agents and tools produce them? |
| **Trace Inspector** | Drill into individual events: timestamp, type, agent, user, trace, span, status. |

### The date control

All eight pages share **one date range control**. It defaults to a rolling
90-day window including today, and a change on any page follows you to every
other page, including the Trace Inspector. Shorter windows are cheaper and
faster — pick the shortest range that answers your question.

### Three things worth knowing

- **First paint is not instant.** A cold load or page switch can take up to 90
  seconds before every chart on the page is drawn. The report controls appear
  first; the charts catch up.
- **Collapse the left navigation drawer.** At the 1280-pixel minimum width,
  Looker Studio's expanded drawer overlays the report's left edge. Collapse it
  to see the full page.
- **"Data Last Updated" in the footer is not your data's freshness.** It is
  Looker Studio's connector refresh time. Your newest events may be newer or
  older than that stamp.

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
before rolling the dashboard out — clone this repository and run:

```sh
cd dashboard/looker_studio
python3 tools/hydrate_dashboard.py \
  --project YOUR_PROJECT_ID \
  --dataset YOUR_DATASET_ID \
  --table agent_events \
  --location US
```

It verifies the required columns and prints the same kind of creation URL the
configurator produces. Requires the `bq` CLI, authenticated.

---

## Troubleshooting

| What you see | What's happening and what to do |
|---|---|
| Charts blank or trickling in after opening a page | Normal on a cold load — allow up to 90 seconds. If a chart is still empty after that, widen the date range: your table may have no events in the selected window. |
| Layout looks cut off on the left | Collapse the Looker Studio navigation drawer, and make the window at least 1280 px wide. Phones and narrow tablets aren't supported. |
| Bottom charts clipped on Token Consumption or Latency | You're on a copy created before 2026-07-29, which keeps the old page geometry. Create a fresh copy from the configurator. |
| Pasted a console link but the fields didn't fill | The link must name exactly one table. Open the table itself in the BigQuery console (close other table tabs), copy the address-bar URL, and paste again — or just paste the dotted `project.dataset.table` ID from the table header's copy control. |
| A field shows a red validation error | Fix just that field: project IDs are 6–30 lowercase characters; dataset IDs allow letters, digits, and underscores (no hyphens — that's a BigQuery rule); table IDs also allow hyphens. |
| Looker Studio asks me to sign in or authorize | Expected. The dashboard uses your credentials to read your data. Authorize BigQuery access on your own account. |
| "Not found: Table …" in Looker Studio | One of the three identifiers is wrong, or your account can't read the table. Open the table in the BigQuery console to confirm the exact IDs and your access, then re-create from the configurator. |
| Permission or quota errors on charts | Your account needs to read the table *and* run BigQuery jobs in the billing project. If you set an Advanced billing project, check your access there. |
| Numbers look stale | Check the date range includes today, then use Looker Studio's refresh. Ignore the footer's "Data Last Updated" — it's a connector timestamp, not your latest event. |
| A colleague I shared with sees an error instead of data | Working as intended if they lack BigQuery access to the table — the report uses Viewer's credentials (see Step 3). Grant them read access to the table, or don't. |

Still stuck? Open an issue:
<https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues>.

---

## Privacy and cost, in plain terms

- **Your data never leaves your control.** The template is public, but your
  copy creates its own data source with your credentials, reads your table,
  and bills your project. The template owner cannot see your data.
- **Viewers bring their own access.** With the Step 3 credentials setting,
  sharing the report never shares the data — each viewer needs their own
  BigQuery read access.
- **You pay only for BigQuery queries your charts run.** No services are
  installed, and the dashboard creates no BigQuery objects in your project.
