# Service-account auth for team rollouts

How a team writes to one shared table so that nobody needs read access to it
to write, nobody keeps a long-lived service-account key, and people who read
the results never touch the raw rows. The mod gets each access token by
running its `accessTokenCommand` option, a command with no shell that prints
a token (the README's [Authentication](../README.md#authentication)); each
setup below is a different command, or a different grant.

## Choose a setup

| Where the agent runs | Setup | Each person or machine needs | BigQuery sees |
| --- | --- | --- | --- |
| People's workstations | [1. Impersonate a writer service account](#1-impersonation-workstations) (recommended) | The Google Cloud CLI, signed in once with `gcloud auth login`; membership of the group allowed to impersonate the writer | the writer service account |
| People's workstations, when BigQuery should see each person | [2. Direct group access](#2-direct-group-access) | The Google Cloud CLI, signed in once with `gcloud auth application-default login`; membership of the writer group | each person |
| CI (GitHub Actions and the like) | [3. Workload Identity Federation](#3-ci-workload-identity-federation) | The job's auth step and the Google Cloud CLI; nothing stored | the writer service account |
| A VM, container or service with its own service account | [4. The attached service account](#4-the-attached-service-account) | The Google Cloud CLI; nothing stored | that service account |
| None of the above works | [5. A key file](#5-a-key-file-an-approved-exception) (an approved exception) | The key file, a long-lived secret | a service account of its own |

None of setups 1 to 4 keeps a long-lived service-account key. Setups 1 and
2 keep the person's own Google sign-in on the machine: `gcloud auth login`
and `gcloud auth application-default login` store a refresh token for the
person in gcloud's configuration folder (`~/.config/gcloud`), which lasts
until it is revoked (`gcloud auth revoke`, or by an administrator) or the
organization's session policy ends it. Setups 3 and 4 store nothing.

In every setup the person's sessions keep working when a token cannot be
had: the mod shows a notice and keeps the rows on disk until a token works.

## Who can do what

| Who | Can | Cannot |
| --- | --- | --- |
| Writers: the writer service account, and the people or CI jobs that may get its tokens | Insert rows into the events table | Read rows, run a query, change or delete the table, as long as none of them may edit the project's custom roles: one who may could add a reading permission to `bqaaRowWriter`, so the bootstrap refuses such a token creator |
| Readers: a group you choose | Query the views (`<dataset>_reporting`): per day, by person or by model, sessions, model requests and token counts | Read the events table, directly or in a query |
| Administrators: a small group, and everyone with a project role that reads every table (Owner, BigQuery Admin, BigQuery Data Owner, Editor or Viewer on the project), and every Role Administrator (any role with `iam.roles.update` on the project), who can widen `bqaaRowWriter` | Read the raw rows: prompts, answers, tool arguments and results | |

Never grant the writer service account a role that can read rows (one with
`bigquery.tables.getData`, such as `roles/bigquery.dataEditor` or
`roles/bigquery.dataViewer`): everyone who can get its tokens would read
everyone's prompts and tool output.

### Project roles reach the table too

A dataset made by `setup.sh` (or `bq mk`) gives the project's roles access
by default: `projectReaders` (project Viewers) read every row,
`projectWriters` (project Editors) change and delete tables and views, and
`projectOwners` (project Owners) own it. When this guide was checked, a
project Viewer read the events table through that entry, and a project
Editor held `getData`, `updateData`, `update` and `delete` on it, views or
not. A reader group that also holds Viewer on the project reads the raw
rows.

So the bootstrap, with `--writer-role row-writer` or `--reader`:

- takes `projectReaders` and `projectWriters` off the events dataset, and
  `projectWriters` off the views dataset, so Editors cannot rewrite an
  authorized view (`--keep-project-groups` leaves them). A project Viewer,
  and a project Editor, granted after the change held no right on the
  table. The change took a few minutes to apply: a minute after it, a
  Viewer still read the table;
- lists everyone who still reads every row: through a project role that
  reads tables (Owner, and BigQuery Admin, Data Owner, Data Editor or Data
  Viewer granted on the project, which read every table whatever a
  dataset's access says, and any other role, custom roles included, whose
  permissions hold `bigquery.tables.getData` or `bigquery.tables.export`,
  as `gcloud iam roles describe` lists them), or through any grant on the
  events dataset or table;
- stops, before anything is granted, when everyone may read the table, and
  when a grant reaches a `--reader` or `--token-creator` member, or may:
  - it reaches them when it is to the member itself, to everyone
    (`allUsers`, `allAuthenticatedUsers`), or to the domain of a person's
    address;
  - it may when it is to another domain (a domain's identity customer can
    hold other domains, and a group's address says nothing of its
    members'); to a person, a service account or a group while the member
    is a group, or to a group while the member is a person, unless Cloud
    Identity answers that they are not in it
    (`gcloud identity groups memberships check-transitive-membership`,
    which needs a caller who may see the group's members); when it goes
    through a set of identity-pool principals; and when it is of a role
    whose permissions could not be read.

  It also stops when a `--token-creator` member may edit the project's
  custom roles (`iam.roles.update`, in Role Administrator), with the same
  reach and Cloud Identity rules: as the writer, such a member could add a
  reading permission to `bqaaRowWriter` and read every row. It lists those
  holders beside the ones who read every row.

  The two kinds stop the run apart, and each flag keeps only its own: a
  grant known to reach a member, or everyone, needs
  `--allow-existing-grants`; one the bootstrap could not rule out needs
  `--accept-unverified`. Moving a team off `roles/bigquery.dataEditor`
  takes those grants off first (see
  [Moving from dataEditor](#moving-from-dataeditor)).
- does the same for the writer: a grant to a group it may be in counts,
  and Cloud Identity is asked. Every grant that reaches the writer counts,
  whatever its role, through everyone, a domain or a group as well as its
  own: the writer must hold nothing but its `bqaaRowWriter` binding on the
  table. A role that seems harmless can still widen that binding's role
  (`iam.roles.update`, in Role Administrator) or delete the project, so
  none is passed over; `--allow-existing-grants` keeps one you have
  audited. The writer's own binding is not counted, so a second run does
  not stop on it.

`--reader` and `--token-creator` take `user:`, `group:` or
`serviceAccount:` members, not `domain:`: Token Creator for a domain would
let everyone in it mint the writer's tokens.

Cloud Identity's membership check answers only for Google Workspace
Enterprise Standard, Enterprise Plus and Enterprise for Education, and
Cloud Identity Premium, accounts; elsewhere it answers 403, and so it does
for a group the caller cannot see. Then every person's and service
account's grant that reads the events data counts as not ruled out,
including the project's Owners, BigQuery's `OWNER` entry for whoever made
the dataset, and the Google service agents that hold BigQuery data roles.
Check the grants the bootstrap lists, then pass `--accept-unverified`:
grants known to reach a reader, a token creator or everyone still stop the
run. Roles held on a folder or the organization are not looked at. The
simplest way to keep the audience small is a project of its own for the
events dataset, where only the administrators hold project roles.

## What this protects, and what it does not

- **A leaked writer token** inserts rows until it expires (an hour at most),
  and reads nothing.
- **A writer, or anyone who can get the writer's tokens, can add misleading
  rows**: any content, any `user_id`, and BigQuery cannot tell. Rows that
  cannot be forged need a server that authenticates each caller and writes
  for it (a write proxy that stamps the verified caller into `user_id`); that
  is a separate design, not this one.
- **The rows hold prompts, answers and tool output** (the README's
  [Privacy and security](../README.md#privacy-and-security)): only
  administrators read the table; readers see the views, which hold counts.
- **A copied key file** (setup 5) works from anywhere until the key is
  deleted, and tokens issued from it until they expire. Disabling the
  account stops both, but only while it stays disabled: enabling it again
  makes the tokens issued before work again until they expire.
- **Capture is not enforced on a machine its user controls**: they can turn
  the mod off, change its options, or insert rows of their own. A managed
  policy pins what it can ([managed settings](managed-settings.md)).

## Set it up

1. **The dataset, table and views**: the mod's [`setup.sh`](../setup.sh)
   (the README's [The dataset](../README.md#the-dataset)).
2. **The writer and the readers**: the bootstrap,
   [`setup_gcp_prereqs.py`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/main/plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py),
   which is still in the mod's source repository: run it from a checkout of
   that repository. It prints its plan; read it, then run it again with
   `--execute`:

   ```bash
   python3 plugins/bigquery-agent-analytics-tracing/scripts/setup_gcp_prereqs.py \
     --project my-gcp-project --dataset agent_analytics \
     --no-create-dataset --no-create-table \
     --service-account bqaa-row-writer --writer-role row-writer \
     --token-creator group:bqaa-writers@example.com \
     --reader group:bqaa-readers@example.com
   ```

   With `--writer-role row-writer` it:
   - creates, or finds, the service account
     `bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com`. Give the
     writer an account of its own: the tracing plugin's guide makes a
     `bqaa-writer` with `roles/bigquery.dataEditor`;
   - stops, before it grants anything, if that account already holds any
     role on the project, the events or views dataset, or the table, other
     than the row-writer role on the table; a grant to everyone, or to its
     domain, counts. `--allow-existing-grants` keeps them, for an account
     someone audited. Roles granted to the account on a
     folder, the organization, other datasets or through a group are not
     looked at;
   - creates, or finds, the project's custom role `bqaaRowWriter`, with two
     permissions (see [What a writer needs](#what-a-writer-needs)), and
     stops if the role holds any other, is disabled, or cannot be read (only
     "not found" means there is none). It grants the role to the service
     account on the events table alone: no `roles/bigquery.dataEditor`, no
     `roles/bigquery.jobUser`. With `--no-grant-iam` it grants nothing
     there, and still checks the account and the role before any
     `--token-creator`;
   - takes the project's Viewer and Editor groups off the events dataset,
     and Editors off the views dataset, lists who still reads every row
     (a project role, or a grant on the events dataset or table), and stops
     if a reader or token creator does (see
     [Project roles reach the table too](#project-roles-reach-the-table-too));
   - enables the IAM Service Account Credentials API
     (`iamcredentials.googleapis.com`), which issues the impersonated
     tokens, and grants `roles/iam.serviceAccountTokenCreator` on the
     service account to each `--token-creator` (a group, so access ends with
     group membership). With `--no-enable-apis`, it checks that the API is
     on instead. It takes `--token-creator` only with `--writer-role
     row-writer`, and refuses a `--principal` other than the service
     account: whoever may impersonate an account gets everything it holds;
   - for each `--reader`, grants `roles/bigquery.dataViewer` on the views
     dataset and `roles/bigquery.jobUser` on the project, and authorizes the
     two views `setup.sh` makes (`daily_usage_by_user`,
     `daily_usage_by_model`) on the events dataset, so readers need no
     access to the table. No other view is authorized: one someone adds
     there could select any column. It refuses a views dataset that is the
     events dataset.

   It needs `gcloud`, `bq` and the Python BigQuery client
   (`python3 -m venv .venv && .venv/bin/pip install google-cloud-bigquery`,
   then run the script with `.venv/bin/python`; it checks the client before
   it changes anything), signed in (with `gcloud auth login`, and
   `gcloud auth application-default login` for the steps that change the
   datasets' access) as someone who may enable services,
   create service accounts and custom roles, and set IAM on them, the
   project, the table and the datasets: project Owner, or Service Usage
   Admin, Service Account Admin, Role Administrator, Project IAM Admin,
   BigQuery Data Owner and BigQuery Job User. New grants can take a minute
   or more to apply.
3. **Check the access**: [`tests/access.live.sh`](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/blob/main/plugins/claude_code_mod/tests/access.live.sh),
   as the writer and a reader, asks BigQuery which rights each holds on the
   table and tries what each must be refused (nothing is written or changed):

   ```bash
   PROJECT=my-gcp-project DATASET=agent_analytics \
     WRITER_SA=bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com \
     READER_SA=a-reader@my-gcp-project.iam.gserviceaccount.com \
     bash plugins/claude_code_mod/tests/access.live.sh
   ```

   It expects the writer to hold exactly `bigquery.tables.get` and
   `bigquery.tables.updateData` on the table, and to be refused a read of its
   rows and any query; the reader to query a view, and to be refused the
   table, directly and in a query; and neither dataset to give the project's
   Viewers or Editors access. Its caller needs Token Creator on both
   service accounts (here `READER_SA` stands in for a member of the readers'
   group). With `INSERT_PROBE=1` it also inserts one row as the writer.

## 1. Impersonation (workstations)

Each person:

1. Installs the Google Cloud CLI and signs in once, as themselves:
   `gcloud auth login`. Impersonation uses gcloud's own sign-in, not the
   Application Default Credentials of `gcloud auth application-default login`.
2. Sets the mod's `accessTokenCommand` to
   `gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com`.
   At install time that is one more `--config` (the value is one argument):

   ```bash
   claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
     --config project=my-gcp-project --config dataset=agent_analytics \
     --config "accessTokenCommand=gcloud auth print-access-token --impersonate-service-account=bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com"
   ```

   (after adding the marketplace as the README's Install shows). An
   installed copy takes it from `/plugin configure`, or an administrator
   sets it for everyone with [managed settings](managed-settings.md).
3. Checks it: `/bqaa-status` shows `enabled, target ...`, no `notice` line,
   and after the first send (every 5 seconds) `requests: 1 ok` or more.

## 2. Direct group access

Grant the row-writer role on the table to a group, and keep the default
token command. Each person installs the Google Cloud CLI and signs in once
with `gcloud auth application-default login`. Rows are inserted as each
person.

```bash
bq --project_id=my-gcp-project add-iam-policy-binding \
  --member=group:bqaa-writers@example.com \
  --role=projects/my-gcp-project/roles/bqaaRowWriter \
  my-gcp-project:agent_analytics.agent_events
```

(The role is the one `setup_gcp_prereqs.py --writer-role row-writer` makes.)
Keep that group free of anyone who may edit the project's custom roles
(`iam.roles.update`, in Role Administrator): they could add a reading
permission to `bqaaRowWriter` and read every row through the group's
binding.
Avoid `roles/bigquery.dataEditor` for a shared table: it can also read every
row, everyone's prompts and tool output included, and change or delete the
table. It fits one person's sandbox.

## 3. CI: Workload Identity Federation

The CI system's own short-lived identity token is exchanged for the writer
service account's token: no key. For GitHub Actions (the IAM Service
Account Credentials API must be on, as the bootstrap leaves it):

```bash
gcloud iam workload-identity-pools create bqaa-ci --project=my-gcp-project --location=global
gcloud iam workload-identity-pools providers create-oidc github \
  --project=my-gcp-project --location=global --workload-identity-pool=bqaa-ci \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == 'my-org/my-repo' && assertion.ref == 'refs/heads/main'"
gcloud iam service-accounts add-iam-policy-binding \
  bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com --project=my-gcp-project \
  --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/bqaa-ci/subject/repo:my-org/my-repo:ref:refs/heads/main"
```

Three things bound who can get the writer's tokens:

- **The audience.** The provider accepts only tokens issued for it: with no
  `--allowed-audiences`, its own resource name, which the auth action below
  asks GitHub for.
- **The repository and branch**, in the attribute condition: a token from
  any other repository or branch is refused at the exchange.
- **The subject**, in the binding: one `principal://.../subject/...`
  (GitHub's `sub` claim, `repo:OWNER/REPO:ref:refs/heads/BRANCH` for a push
  or a manual run), not the whole pool. Jobs that use a GitHub environment
  carry `repo:OWNER/REPO:environment:NAME` instead.

In the workflow:

```yaml
permissions:
  contents: read
  id-token: write
steps:
  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/bqaa-ci/providers/github
      service_account: bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com
  - uses: google-github-actions/setup-gcloud@v2
```

After those steps, `gcloud auth print-access-token`, the mod's token command
there, prints the writer's token.

## 4. The attached service account

On a VM, a container platform or a service that runs as its own service
account, grant that account the row-writer role on the table and set the
token command to `gcloud auth print-access-token`: gcloud gets the attached
account's token from the metadata server, with no sign-in and no key.

## 5. A key file: an approved exception

A service-account key is a long-lived secret: anyone who copies it can insert
rows until the key is deleted. Use one only where none of the setups above
works, as an exception someone approved, with a named owner for rotation and
for revocation, and an end date. Your organization may forbid keys outright
(the `iam.disableServiceAccountKeyCreation` policy); then use 3 or 4.

Give the exception a service account of its own, with the row-writer role
on the table and nothing else. Never create a key for the writer service
account people impersonate: the key would bypass the group that controls
who may impersonate it.

The mod cannot use a key file directly (it cannot sign the token request).
Wrap it in a token command that reads the key for that command alone:

```text
env GOOGLE_APPLICATION_CREDENTIALS=/secure/path/bqaa-exception-key.json gcloud auth application-default print-access-token
```

Keep the file readable by the machine's account only, and never commit it.

- **To rotate** (on a schedule): create a new key
  (`gcloud iam service-accounts keys create`), put it in place, then delete
  the old one (`gcloud iam service-accounts keys delete KEY_ID
  --iam-account=...`). Deleting a key stops new tokens from it; tokens
  already issued from it last until they expire, up to an hour.
- **If the key leaks**, either:
  - retire the account: disable it (`gcloud iam service-accounts disable
    ...`), which refuses its tokens, those already issued included; delete
    the key; and give the exception a new service account, with the
    row-writer role on the table and nothing else, and a new key; or
  - disable the account, delete the key, and keep the account disabled
    until every token issued before the disable has expired: an hour by
    default, longer where your organization allows longer token lifetimes
    (`iam.allowServiceAccountCredentialLifetimeExtension`, up to 12 hours).
    Enabling it sooner brings those tokens back: when this guide was
    checked, a token refused (`401`) while the account was disabled was
    accepted again 20 seconds after it was enabled. Then create a new key,
    enable the account, and check with a token you saved before the
    disable: an `insertAll` with it must answer `401`.

## Moving from dataEditor

Teams that followed earlier instructions gave writers
`roles/bigquery.dataEditor` on the dataset: a group, or each person (the
tracing plugin's default setup grants it per developer). Those grants read
every row. Before the writers' group becomes a token creator, and the
readers' group a reader, take them off:

1. **See who holds what**: the bootstrap lists every grant on the project,
   the events dataset and the table when it checks the readers and token
   creators. Without it:
   `bq show --format=prettyjson my-gcp-project:agent_analytics` (the
   dataset's `access`), `bq get-iam-policy --format=prettyjson
   my-gcp-project:agent_analytics.agent_events`, and
   `gcloud projects get-iam-policy my-gcp-project`.
2. **Take each grant off**, group or person:

   ```bash
   bq --project_id=my-gcp-project query --use_legacy_sql=false      'REVOKE `roles/bigquery.dataEditor` ON SCHEMA `my-gcp-project.agent_analytics` FROM "group:old-writers@example.com"'
   bq remove-iam-policy-binding --member=user:dev@example.com      --role=roles/bigquery.dataViewer my-gcp-project:agent_analytics.agent_events
   gcloud projects remove-iam-policy-binding my-gcp-project      --member=group:old-writers@example.com --role=roles/bigquery.dataEditor
   ```

   (A dataset grant made with `roles/bigquery.dataEditor` shows as `WRITER`
   in the dataset's access; the `REVOKE` above takes it off.)
3. **Switch people's token command** to the impersonation of setup 1, and
   run the bootstrap. It stops while a reader or token creator still holds
   a grant there, or may. With readers or token creators that are groups,
   a person's grant there stops it unless Cloud Identity answers that the
   person is in neither group. A domain's grant stops it until you have
   checked who the domain holds and pass `--accept-unverified`.

## What a writer needs

Streaming inserts (`insertAll`, which the mod uses) take two permissions on
the table:

- `bigquery.tables.updateData`, to insert;
- `bigquery.tables.get`, which streaming inserts also check: without it
  BigQuery answers `403 ... Permission bigquery.tables.get denied on table`.

`bqaaRowWriter` holds exactly these two, granted on the events table alone.
Google's documentation also lists `bigquery.datasets.get` on the dataset for
streaming inserts; inserts worked without it when this guide was checked. If
BigQuery ever asks for it, grant `roles/bigquery.metadataViewer` on the
dataset (names and schemas, no rows).

The writer cannot create the table. If it is missing, the mod's attempt is
refused, and the mod shows "BigQuery cannot find the table" with the
`setup.sh` command, while rows wait. Set `BQAA_AUTO_CREATE_TABLE=false` to
skip that attempt.

## What `user_id` holds

| Setup | `user_id`, by default | With `userIdFromCredentials` on |
| --- | --- | --- |
| 1. Impersonation | The person's login name (`$USER`), or `BQAA_USER_ID` | The writer service account's address, the same for everyone |
| 2. Direct group access | The login name, or `BQAA_USER_ID` | The person's verified email |
| 3, 4, 5 | The machine's login name (on CI, the runner's), or `BQAA_USER_ID` | The service account's address |

- `userIdFromCredentials` is off by default. Under impersonation leave it
  off: otherwise every row says the service account.
- The login name and `BQAA_USER_ID` are what the machine says. `BQAA_USER_ID`
  is read only from the environment, and a managed setting gives everyone
  the same value, so it cannot name each person from one central policy.
- BigQuery checks who may insert, not what a row holds: `user_id` is an
  attribution, not proof.

## What the logs show

- BigQuery's audit logs do not record streaming inserts row by row, so they
  cannot say who inserted which row.
- Under impersonation, each token request is a call to the IAM Service
  Account Credentials API, which records the caller in its Data Access audit
  logs when those logs are turned on for that API (they are not by default).
  That names who could insert in a given hour, not row by row.

## Offboarding

1. **Take the person out** of the group that has Token Creator (setup 1) or
   the row-writer role (setup 2). The change applies as IAM propagates:
   usually within minutes, not at once.
2. **Tokens already issued** stay valid until they expire, up to an hour
   after they were issued; the mod keeps one for 45 minutes. Their own
   Google sign-in on the machine ends with their account (or
   `gcloud auth revoke` there).
3. **To cut every token off at once**, disable the writer service account:
   `gcloud iam service-accounts disable bqaa-row-writer@my-gcp-project.iam.gserviceaccount.com`.
   When this guide was checked, a token issued before the disable was
   refused (`401`) 15 seconds after it, and no new token was issued.
   Everyone's rows wait while it is disabled. Keep it disabled until every
   token issued before the disable has expired (an hour by default; longer
   where your organization allows longer lifetimes) and the group change of
   step 1 has propagated: enabling it sooner brings those tokens back (in
   the same check, the refused token was accepted again 20 seconds after
   the account was enabled). Then enable it
   (`gcloud iam service-accounts enable ...`), and check with a token saved
   before the disable that it is refused. For a key file (setup 5), see
   [If the key leaks](#5-a-key-file-an-approved-exception).
4. **Check that access is gone**: the group no longer lists the person
   (`gcloud identity groups memberships list --group-email=...`), and the
   service account's policy names no one else
   (`gcloud iam service-accounts get-iam-policy ...`). As the person, a
   token command that still works means a grant was missed.
5. **Rows still on the person's machine** (its dead-letter folder) stay
   there once no token works; delete the folder (`~/.bqaa-mod/dead-letter`
   by default) with the machine.

## Organization policies that can get in the way

- `iam.disableServiceAccountKeyCreation` stops key files (setup 5): use
  setup 3 or 4.
- `iam.allowedPolicyMemberDomains` (domain-restricted sharing): every
  `group:` and `user:` member granted above must be in an allowed domain.
- VPC Service Controls: with BigQuery and the IAM Service Account
  Credentials API inside a perimeter, requests from workstations and CI need
  an access level or an ingress rule.
- `iam.workloadIdentityPoolProviders` limits the identity providers a pool
  may trust (setup 3).

## If rows are not written

- **"no working Google credentials"**: the token command fails. Run it by
  hand: not signed in (`gcloud auth login` for setup 1,
  `gcloud auth application-default login` for setup 2), no Token Creator on
  the service account, the IAM Service Account Credentials API off, or a
  grant made moments ago.
- **"BigQuery denied the identity access to the dataset"**: the identity
  (the service account, in setups 1, 3, 4 and 5) lacks the row-writer role on
  the table. Run the bootstrap again with `--writer-role row-writer`, which
  grants it, then run `/bqaa-backfill` to send the refused rows. Do not
  grant a role that reads rows instead.
- **"BigQuery cannot find the table"**: run the `setup.sh` command the notice
  shows.
