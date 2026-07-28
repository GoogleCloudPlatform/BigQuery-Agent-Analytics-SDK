# Grafana Dashboard for BigQuery Agent Analytics

Visualize BQAA telemetry straight from BigQuery in Grafana, with nothing to
host. Works on the free tier of Grafana Cloud, and runs in parallel with the
[`dashboard_v2/`](../dashboard_v2) React app rather than replacing it. Both read
the same data.

```
AI Agent app ──SDK──▶ BigQuery agent_events ──ViewManager──▶ typed views (adk_*)
                              │                                    │
                              ├──────────▶ dashboard_v2 (React)    │
                              └──────────▶ Grafana ◀───────────────┘
```

## What's in this directory

|           File            |                          Purpose                          |
| ------------------------- | --------------------------------------------------------- |
| `bqaa-dashboard.json`     | The dashboard. Import this into Grafana.                  |
| `queries/*.sql`           | Panel SQL, the **source of truth** (see `queries/README.md`). |
| `datasource.example.yaml` | Provisioning example for self-managed Grafana.            |

## Prerequisites

- A GCP project with the SDK installed (`pip install bigquery-agent-analytics`)
  and **both** of these APIs enabled:
  - **BigQuery API** (`bigquery.googleapis.com`) — runs the panel queries.
  - **Cloud Resource Manager API** (`cloudresourcemanager.googleapis.com`) — the
    BigQuery plugin calls it to enumerate the projects the service account can
    reach. Leave it disabled and **Save & test** fails with a permission error
    and an empty project dropdown, even though the key and the IAM roles are
    correct.

  ```bash
  gcloud services enable bigquery.googleapis.com \
    cloudresourcemanager.googleapis.com --project YOUR_PROJECT
  ```
- A Grafana instance ([Grafana Cloud Free](https://grafana.com/products/cloud/)) with the
  **Google BigQuery** data source plugin (`grafana-bigquery-datasource`) installed.
  BigQuery will not appear as a connector until that plugin is installed.

## 0. Create a service account

1. Google Cloud Console: **IAM & Admin → Service Accounts → Create Service
   Account** (for example, `grafana-bqaa-viewer`).
2. Grant `BigQuery Job User` (`roles/bigquery.jobUser`) on the project, and
   `BigQuery Data Viewer` (`roles/bigquery.dataViewer`) on the BQAA dataset only,
   not on the whole project.
3. Open the account, go to **Keys → Add Key → Create new key**, choose **JSON**,
   and download it. You upload this in Step 2.

> **Keep the key out of the repo.** `.gitignore` only ignores new `*.json` files
> inside `grafana/`, so a key saved anywhere else can be committed by accident.

## 1. Prepare the data

No real traffic yet? Seed the synthetic corpus:

```bash
bqaa seed-events --scenario retail-returns \
  --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

Then create the typed views the dashboard queries:

```bash
bq-agent-sdk views create-all --project-id YOUR_PROJECT --dataset-id YOUR_DATASET
```

These views un-nest the JSON columns of `agent_events` into typed columns.
`ViewManager` prefixes them with `adk_` by default. If you used a custom prefix,
set the dashboard's **View prefix** variable to match.

## 2. Connect Grafana to BigQuery

**Grafana Cloud**

1. Create a free account at grafana.com and open your stack.
2. **Connections → Add new connection**, search for **Google BigQuery**, click
   **Install**.
3. Click **Add new data source** from that plugin page.
4. Choose **Google JWT File** auth and upload the JSON key from Step 0.
5. Set your default project and click **Save & test**.

**Self-managed (Docker or bare metal)**

Copy [`datasource.example.yaml`](datasource.example.yaml), inject your
credentials, and provision it at startup. Never commit the real key: prefer
mounting it or pulling it from a secret manager. For
`secureJsonData.privateKey`, use a real YAML multiline value with real line
breaks as shown in the example, not the JSON form with literal `\n` escapes.
(Default login for a fresh local Grafana is `admin` / `admin`.)

- **Docker:**
  ```bash
  cp grafana/datasource.example.yaml grafana/datasource.yaml
  docker run -d -p 3000:3000 \
    -e "GF_INSTALL_PLUGINS=grafana-bigquery-datasource" \
    -v /path/to/your/datasource.yaml:/etc/grafana/provisioning/datasources/datasource.yaml \
    grafana/grafana
  ```
- **Bare metal:**
  ```bash
  grafana-cli plugins install grafana-bigquery-datasource
  cp /path/to/your/datasource.yaml /etc/grafana/provisioning/datasources/
  systemctl restart grafana-server
  ```

## 3. Import the dashboard

1. **Dashboards → New → Import**.
2. Upload `bqaa-dashboard.json` (or paste its contents).
3. Select your BigQuery data source if prompted.
4. Open **Dashboard Settings → Variables** and set the hidden `project`,
   `dataset`, `table`, and `view_prefix` constants for your environment.
   Defaults: table `agent_events`, view prefix `adk_`.
5. While you are there, set the two pricing constants that drive the
   **Estimated cost** panel — see [Cost variables](#cost-variables). They ship
   with placeholder rates.

> **Security:** keep all six of these variables as **Constant**. Switching
> `project`, `dataset`, `table`, or `view_prefix` to **Textbox** opens the
> dashboard to SQL injection, letting viewers query arbitrary datasets; the two
> pricing constants are interpolated raw into arithmetic, so a **Textbox** there
> lets a crafted URL inject text into the cost expression.

You should now see four rows:

- **Overview:** sessions, events, error rate, latency, volumes
- **LLM & FinOps:** tokens, latency percentiles, tokens by model, estimated cost
- **Tools & Execution:** invocations, latency, errors
- **Sessions & Traces:** session rollups and a per-session event timeline

## Variables

Use the **Agent**, **User ID**, **Event Type**, and **Session** multi-selects at
the top. All four default to **All**, and they are independent: they do not
cascade.

> **This table is the canonical statement of filter scope.** Row titles, panel
> tooltips, and `queries/*.sql` headers repeat it as pointers. When they
> disagree, this table wins.

| Filter         | Source                                             | Applies to                                      | Ignored by      |
| -------------- | -------------------------------------------------- | ----------------------------------------------- | --------------- |
| **Agent**      | [`var_agent.sql`](queries/var_agent.sql)           | All panels                                      | None            |
| **User ID**    | [`var_user_id.sql`](queries/var_user_id.sql)       | All panels                                      | None            |
| **Event Type** | [`var_event_type.sql`](queries/var_event_type.sql) | Events over time, Recent sessions, Trace detail | Everything else |
| **Session**    | [`var_session_id.sql`](queries/var_session_id.sql) | All panels                                      | None            |

Things worth knowing:

- **Agent, User ID, and Session are capped at 1000 options** each. Session and
  User ID keep the most recently active in range; Agent keeps the
  alphabetically first 1000, a defensive cap a real deployment should never
  reach. Event Type is uncapped: the SDK emits a small fixed set. All three
  capped pickers **accept custom values**, so anything truncated is still
  reachable — type or paste it straight in, or narrow the time range until the
  window holds fewer than 1000.
- **Recent sessions** uses Agent, User ID, and Event Type only to pick *which*
  sessions are listed. A session is listed when it contains **a match for each
  of the three filters somewhere in the window — not necessarily the same
  event** — that is what the `HAVING LOGICAL_OR(a) AND LOGICAL_OR(b) AND
  LOGICAL_OR(c)` in [`recent_sessions.sql`](queries/recent_sessions.sql) means.
  One event matching only Agent plus a *different* event matching only Event
  Type **does** qualify the session. The `*_in_window` columns then cover
  every event in that session, so they will exceed the Overview stats whenever a
  filter is active. Use **Trace detail** to attribute events within a session.
  The panel lists the 250 most recently active sessions in range.
- **Clicking a `session_id` in Recent sessions pins that session** into the
  **Session** variable, which is what drives the **Trace detail** panel below
  it. The link carries the current time range and the Agent, User ID and Event
  Type selections over with it, and it *replaces* the Session selection rather
  than adding to it. The dropdown is still there if you would rather type or
  paste an id.
- **`session_users_in_window` can name more than one user.** It is a
  `STRING_AGG(DISTINCT user_id)`, not a single value, because nothing stops two
  user ids from appearing in one session — read a multi-value cell as a signal
  about the session, not as a rendering bug.
- **`session_input_tokens_in_window` / `session_output_tokens_in_window`** are
  the same `$.usage.prompt` / `$.usage.completion` counters the LLM & FinOps row
  charts as prompt / completion tokens, summed over the session's
  `LLM_RESPONSE` events; they are named for the direction the
  `price_per_1m_*_tokens` variables price. Sessions that made no LLM call read
  `0`.
- **Contradictory filters render "No data"** (for example, an agent and session
  that never co-occurred). Widen the others to **All** to tell that apart from
  missing telemetry. Stat panels show **No matching data**, not `0`.
- **Errors over time** shows **No errors in range** both when the window is
  genuinely clean and when the filters match nothing. Check the Overview stats
  to tell which: real numbers mean a clean window, **No matching data** means the
  filters contradict.
- **Trace detail** is the heaviest query when **Session** is **All**, and it
  returns only the 500 most recent events. Pin a session before widening the
  default **Last 24 hours** range.

### Cost variables

The **Estimated cost** panel derives dollars from token counts; the SDK never
records a price. Two hidden constants supply the rates, both read as **USD per
1,000,000 tokens** — the unit model price lists publish:

| Variable                      | Label                            | Default | Applies to                          |
| ----------------------------- | -------------------------------- | ------- | ----------------------------------- |
| `price_per_1m_input_tokens`   | Input price (USD per 1M tokens)  | `1.25`  | `usage_prompt_tokens`               |
| `price_per_1m_output_tokens`  | Output price (USD per 1M tokens) | `5.00`  | `usage_completion_tokens`           |

Both defaults are **placeholders**. Edit them in **Dashboard Settings →
Variables** to match the model(s) you actually run — do not edit
[`estimated_cost.sql`](queries/estimated_cost.sql), which reads them. They are a
single blended rate per direction, so a dashboard spanning several models with
different prices reports an approximation; token counts remain the exact signal.

They are `constant` variables with `skipUrlSync`, and must stay that way:
`estimated_cost.sql` interpolates them raw (no `:sqlstring`) because they are
numbers in arithmetic, not string literals, so only someone who can already edit
the dashboard can change them. A URL cannot.

## 4. Sharing publicly (snapshots)

BigQuery needs backend auth, so Grafana Cloud Free cannot publicly share a live
BigQuery dashboard. Use **Snapshots** instead.

> **PRIVACY WARNING:** A snapshot is a public, point-in-time copy. It strips the
> backend queries, but it preserves **every visible value and the raw executed
> SQL** in the URL payload: session IDs, error messages, and your plain-text GCP
> project ID and dataset name. Anyone on the internet can read that.
> **DO NOT snapshot real production telemetry.**

Before sharing, you **MUST**:

1. Seed `bqaa seed-events` into a dedicated, isolated demo dataset.
2. Point the dashboard's `dataset` constant at that demo dataset.
3. Check every visible panel for sensitive prompts, responses, or identifiers.
4. Set a short expiration (for example, 1 hour).
5. Open the snapshot link in an incognito window and verify before sharing.

To create one: **Share → Snapshot** tab, set a name and expiration, click
**Publish to snapshot.raintank.io** (or Local Snapshot), then copy and verify the
link.
