# Moving to this marketplace

For people who installed the mod from the marketplace it came from
(`bigquery-agent-analytics-plugin`, in the
`haiyuan-eng-google/bigquery_agent_analytics_skill` repository), or who use
the settings-hook tracing plugin, `bigquery-agent-analytics-tracing`.

Run one tracer per table. The mod and the settings-hook tracing plugin
write the same events, so with both on, every event is written twice.

## From the earlier marketplace

1. See what you have: `claude plugin list`.
2. Remove the earlier install, so only one copy of the mod is installed:

   ```bash
   claude plugin uninstall bigquery-agent-analytics-mod@bigquery-agent-analytics-plugin
   ```

3. Install from this marketplace with the same options (the README's
   [Install](README.md#install)):

   ```bash
   claude plugin marketplace add GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK \
     --sparse .claude-plugin plugins/claude_code_dist
   claude plugin install bigquery-agent-analytics-mod@bqaa-tracing \
     --config project=my-gcp-project --config dataset=agent_analytics
   ```

   Rows the earlier install left on disk (`~/.bqaa-mod/dead-letter`) are
   sent by this one: the folder and its format are the same.
4. Check: `/bqaa-status` names your table, with no `notice` line, and
   `/plugin` lists one mod.

## From the settings-hook tracing plugin

Turn it off for the table the mod writes to, from whichever marketplace it
came from:

```bash
claude plugin disable bigquery-agent-analytics-tracing@bqaa-tracing
claude plugin disable bigquery-agent-analytics-tracing@bigquery-agent-analytics-plugin
```

Hooks pasted into your own `~/.claude/settings.json` that run the tracer's
scripts have no plugin to disable: take them out.

## With managed settings

A policy has to register the marketplace as well as turn the plugin on: an
`enabledPlugins` entry for a marketplace a machine does not know installs
nothing, and with the earlier ids already `false`, that machine then records
nothing. Below are two ways to deliver the mod. Both cut over in two steps,
checking the new mod on a pilot group before the earlier ids go off.

### The organization's folder (recommended)

A folder that device management puts on each machine, owned by an
administrator, holding the release archive and a catalog with a name of its
own, `org-tools` here, so the id is `bigquery-agent-analytics-mod@org-tools`.
Not `bqaa-tracing`: under this repository's marketplace name, a machine
where the person had added that marketplace ran the person's own copy, in
the user tier, in its first session under the policy (checked).
The host reads it in place and runs the mod as the organization's: from the
first session, ahead of anything a person installed, under
`allowManagedModsOnly` and `allowManagedHooksOnly` too, and with every event
(the README's Limitations list what a mod a person installs misses).

1. Build the folder as root from a published `mod-vX.Y.Z` release, checked
   against its `SHA256SUMS`. For a later release, run it again: it swaps
   the whole folder, and the next session runs the new version (checked:
   `claude plugin list` shows it).

   ```bash
   set -euo pipefail
   VERSION=X.Y.Z
   BASE="https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/mod-v${VERSION}"
   NAME="bigquery-agent-analytics-mod-${VERSION}"
   DEST=/opt/org/agent-plugins
   WORK=$(mktemp -d)
   curl -fsSL "$BASE/$NAME.tar.gz" -o "$WORK/$NAME.tar.gz"
   curl -fsSL "$BASE/SHA256SUMS" -o "$WORK/SHA256SUMS"
   (cd "$WORK" && sha256sum -c SHA256SUMS)  # on macOS: shasum -a 256 -c SHA256SUMS
   tar -xzf "$WORK/$NAME.tar.gz" -C "$WORK"
   rm -rf "$DEST.new" "$DEST.old"
   mkdir -p "$DEST.new/.claude-plugin" "$DEST.new/plugins"
   cp -R "$WORK/$NAME" "$DEST.new/plugins/bigquery-agent-analytics-mod"
   cat >"$DEST.new/.claude-plugin/marketplace.json" <<'JSON'
   { "name": "org-tools", "owner": { "name": "Your organization" },
     "plugins": [ { "name": "bigquery-agent-analytics-mod", "source": "./plugins/bigquery-agent-analytics-mod" } ] }
   JSON
   chmod -R go-w "$DEST.new"
   if [ -d "$DEST" ]; then mv "$DEST" "$DEST.old"; fi
   mv "$DEST.new" "$DEST"
   ```

2. Register it and turn the mod on, with the earlier ids as they are:

   ```json
   {
     "extraKnownMarketplaces": {
       "org-tools": { "source": { "source": "directory", "path": "/opt/org/agent-plugins" } }
     },
     "enabledPlugins": { "bigquery-agent-analytics-mod@org-tools": true },
     "pluginConfigs": { "bigquery-agent-analytics-mod@org-tools": { "options": { "...": "every option" } } }
   }
   ```

   Move the policy's `pluginConfigs` to the new id: options under the old id
   no longer apply. On a pilot machine, `claude -p /bqaa-status` names the
   policy's table, and `claude -p --debug-file /tmp/bqaa.log /bqaa-status`
   logs `hooks module bigquery-agent-analytics-mod@org-tools loaded (...
   tier prepend)`. The mod under its earlier ids does not load beside it, but
   the settings-hook tracer keeps running: on a machine that has it, every
   event is written twice until step 3, so keep the pilot short.
3. Then turn the earlier ways in off, which also stops them from being
   installed again:

   ```json
   "enabledPlugins": {
     "bigquery-agent-analytics-mod@org-tools": true,
     "bigquery-agent-analytics-mod@bqaa-tracing": false,
     "bigquery-agent-analytics-mod@bigquery-agent-analytics-plugin": false,
     "bigquery-agent-analytics-tracing@bigquery-agent-analytics-plugin": false,
     "bigquery-agent-analytics-tracing@bqaa-tracing": false
   }
   ```

### This repository as the marketplace

Where only settings can be delivered. The first policy registers the
repository at a `mod-dist-vX.Y.Z` tag, with the same two folders the sparse
add fetches, and turns the mod on, with the earlier ids as they are. That
tag marks the commit whose served copy and catalog entry are at X.Y.Z. The
`mod-vX.Y.Z` release tag is cut before the copy is synced, so it still holds
the previous release's copy. And `ref` takes a branch or a tag, not a commit
(checked: a commit is "not found in upstream origin").

```json
{
  "extraKnownMarketplaces": {
    "bqaa-tracing": {
      "source": {
        "source": "github",
        "repo": "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK",
        "ref": "mod-dist-vX.Y.Z",
        "sparsePaths": [".claude-plugin", "plugins/claude_code_dist"]
      },
      "autoUpdate": false
    }
  },
  "enabledPlugins": { "bigquery-agent-analytics-mod@bqaa-tracing": true },
  "pluginConfigs": { "bigquery-agent-analytics-mod@bqaa-tracing": { "options": { "...": "every option" } } }
}
```

If your policy sets `strictKnownMarketplaces`, add an entry with the same
`ref`: `{ "source": "github", "repo": "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK", "ref": "mod-dist-vX.Y.Z" }`.
An entry without the `ref` does not allow a source that names one.

A mod installed this way is a mod the person installed. It runs in the user
tier, so `allowManagedModsOnly` refuses it, `allowManagedHooksOnly` keeps it
from loading, and it misses what the README's Limitations list. Each machine
installs it in the background and records from its second or third session.

The cutover takes the same two steps. While the mod under its earlier id is
enabled on a machine, it keeps recording, and the new one, once installed,
does not load: the host loads one plugin of a name, and the earlier install
comes first. The new mod is installed on a pilot machine when
`claude plugin list` lists `bigquery-agent-analytics-mod@bqaa-tracing` with
`Version: X.Y.Z`, and its debug log, from `claude -p --debug-file /tmp/bqaa.log /bqaa-status`,
says
`hooks module bigquery-agent-analytics-mod@bqaa-tracing not loaded: another plugin of that name loads first`.
On a machine that had only the
settings-hook tracer, the check is `/bqaa-status` naming the policy's table,
and both write until the next step. Then set the earlier ids to `false`, as
in step 3 above: the new mod loads from the next session. A machine that
started no session between the two steps records from its second or third
session after it.

To move to a later release, change `ref` to its `mod-dist-v` tag in both
entries, for the pilot group first. Then, on each machine, right after the
new policy arrives, fetch the marketplace at that tag with the policy's
sparse paths, then update. Run them as the person (or a script run as
them), and require both to succeed:

```bash
claude plugin marketplace add "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#mod-dist-vX.Y.Z" \
  --sparse .claude-plugin plugins/claude_code_dist
claude plugin update bigquery-agent-analytics-mod@bqaa-tracing
```

Until then the copy does not load, because the marketplace no longer
matches the policy, and `plugin update` alone fails ("not found"). Without
`--sparse`, the add itself is refused: its source would not match the
policy's entry. Each release's tag carries its own version, which is what
`plugin update` compares and `plugin list` shows. Check by commit instead
of by version. Look up the tag's commit once:

```bash
git ls-remote https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK "refs/tags/mod-dist-vX.Y.Z" "refs/tags/mod-dist-vX.Y.Z^{}" | awk '{ c = $1 } /\^\{\}$/ { p = $1 } END { if (!c) { print "no commit found" > "/dev/stderr"; exit 1 } print (p ? p : c) }'
```

Release tags are annotated (`git tag -a`), so the `refs/tags/mod-dist-vX.Y.Z`
line, the only one `git ls-remote … mod-dist-vX.Y.Z` prints, is the tag
object's id, not a commit. The commit, which the host records, is on the
`^{}` line, and the command prints it. If it prints "no commit found"
instead, the tag is missing or the lookup failed: stop. On each machine,
every `gitCommitSha` the host recorded for the copy in
`~/.claude/plugins/installed_plugins.json` must be that commit:

```bash
python3 -c 'import json, os; d = json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))); print([e.get("gitCommitSha") for e in d["plugins"]["bigquery-agent-analytics-mod@bqaa-tracing"]])'
```

The same two commands, at the policy's tag, are needed on a machine where
the person added this marketplace themselves (the README's sparse add)
before the policy arrived: until then its copy does not load. If that copy
already had the tag's version, `plugin update` says "already at the latest
version" and the copy keeps its own commit. Its files are still the
release's, since Mod CI holds the served copy to its release tag. To have
the tag's commit as well, deliver a policy that sets the id to `false`, run
`claude plugin uninstall bigquery-agent-analytics-mod@bqaa-tracing`, then
deliver the policy at the tag again and run
`claude plugin install bigquery-agent-analytics-mod@bqaa-tracing`.

Checked in a container, with a test repository whose policy pins sparse
paths, and `autoUpdate` off:

- right after the ref moved, `plugin update` alone failed;
- `marketplace add` at the new tag without `--sparse` was refused;
- with the policy's sparse paths, `marketplace add` and then
  `plugin update` both succeeded ("updated from ... for scope managed").
  The recorded commit was the tag's, and the next session ran it;
- a copy installed from another ref of the same marketplace did not load
  once the policy arrived, until the same two commands.

## The earlier location

In the earlier repository, only the mod's folder moves: it is replaced by a
pointer to this one. Its settings-hook tracing plugin and its analysis
skill stay where they are.
