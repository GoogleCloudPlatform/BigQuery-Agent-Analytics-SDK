# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI: live ADK observe trace -> BQAA agent_events -> derived OKF bundle.

  python examples/okf_bqaa_adapter/run.py               # committed live export
  python examples/okf_bqaa_adapter/run.py --live        # run agent, export, adapt
  python examples/okf_bqaa_adapter/run.py --session ID  # read BQ; fail closed
  python examples/okf_bqaa_adapter/run.py --lookup REF  # resolve via mapping.json

Default path is stdlib only. ``--live`` / ``--session`` import the ADK and
BigQuery clients. Everything written is derived/demo; nothing is attested.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import adapter  # noqa: E402
import lookup as lookup_mod  # noqa: E402

DEFAULT_OUT = HERE / "out"


def _load_observe_agent():
  import observe_agent  # google.adk / google.cloud; only on live paths

  return observe_agent


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--events",
      default=str(adapter.DEFAULT_EVENTS),
      help="BQAA trace JSON (default: the committed live export)",
  )
  parser.add_argument(
      "--live", action="store_true", help="run the observe agent"
  )
  parser.add_argument(
      "--session", help="read an existing session from BigQuery"
  )
  parser.add_argument(
      "--out", default=str(DEFAULT_OUT), help="output directory"
  )
  parser.add_argument("--lookup", metavar="REF", help="resolve a context_ref")
  parser.add_argument("--manifests", default=str(adapter.DEFAULT_MANIFESTS))
  args = parser.parse_args(argv)
  out = Path(args.out)

  if args.lookup:
    return _lookup(args.lookup, out)
  return _adapt(args, out)


def _lookup(ref: str, out: Path) -> int:
  try:
    result = lookup_mod.lookup(ref, out / "mapping.json")
  except lookup_mod.UnknownContextRefError as exc:
    print(f"FAIL_CLOSED {exc.args[0]}", file=sys.stderr)
    return 2
  violations = lookup_mod.never_emit_violations(result)
  if violations:
    print(f"FAIL_CLOSED never-emit keys present: {violations}", file=sys.stderr)
    return 2
  print(json.dumps(result, indent=2))
  return 0


def _adapt(args, out: Path) -> int:
  if args.live:
    oa = _load_observe_agent()
    session_id = oa.run_observe_agent()
    trace = oa.export_session(session_id)
  elif args.session:
    oa = _load_observe_agent()
    rows = oa.fetch_session_rows(args.session, wait_s=0)
    if not rows:
      print(f"FAIL_CLOSED no rows for session {args.session}", file=sys.stderr)
      return 3
    trace = adapter.build_trace(
        rows,
        table=f"{oa.PROJECT}.{oa.DATASET}.{oa.TABLE}",
        writer=oa.WRITER,
        label=oa.LABEL,
    )
  else:
    trace = adapter.load_trace(args.events)

  try:
    obs = adapter.require_retrieve_shaped(trace)
  except adapter.NotRetrieveShapedError as exc:
    print(f"FAIL_CLOSED not retrieve-shaped: {exc}", file=sys.stderr)
    return 3
  result = adapter.adapt(trace)
  identities = adapter.compute_identities(
      result["files"],
      result["constants"],
      adapter.load_manifests(args.manifests),
  )
  written = adapter.project(result, identities, out)
  print("LABEL", adapter.LABEL)
  print("ADAPTER", adapter.ADAPTER_VERSION)
  print("TABLE", obs["table"])
  print("SESSION", obs["session_id"])
  print("TRACE", obs["trace_id"])
  print("MODEL", (obs.get("agent") or {}).get("model"))
  print("CONTEXT_REF", obs["context_ref"])
  print(
      "RECEIPT", obs["receipt"].get("verdict"), obs["receipt"].get("receipt_id")
  )
  print("OBSERVATION_ID", identities["observation_id"])
  print("SNAPSHOT_ID", identities["snapshot_id"])
  print("PUBLICATION_ID", identities["publication_id"])
  print("FILES", len(result["files"]))
  print("OUT", written["bundle"].parent)
  return 0


if __name__ == "__main__":
  sys.exit(main())
