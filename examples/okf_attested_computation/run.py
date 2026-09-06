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

"""CLI: run one receipt case hermetically (default) or live (``--live``).

    python examples/okf_attested_computation/run.py --case approved
    python examples/okf_attested_computation/run.py --case sql-substitution --live

Exit status is 0 only when the trusted consumer releases a VERIFIED value.
Every blocked case exits nonzero and prints no governed number. The
per-case JSON written under ``--evidence-dir`` is a private diagnostic,
not the answer surface. No flag skips verification.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
  # Append (not insert) so this example never shadows a sibling's modules.
  sys.path.append(str(HERE))

import attacks  # noqa: E402
import broker  # noqa: E402
import consume as consume_mod  # noqa: E402
import contracts  # noqa: E402
import execute as execute_mod  # noqa: E402
import hermetic  # noqa: E402
import publication as publication_mod  # noqa: E402
import receipt_store  # noqa: E402
import verify as verify_mod  # noqa: E402

CASES = (
    "approved",
    "sql-substitution",
    "parameter-substitution",
    "display-substitution",
    "missing-evidence",
    "tamper",
    "replay",
)
JAN = {"period_start": "2026-01-01", "period_end": "2026-01-31"}
JAN_FEB = {"period_start": "2026-01-01", "period_end": "2026-02-28"}
LIVE_PROJECT = "test-project-0728-467323"


class World:
  """Bundle of trusted components for one run (hermetic or live)."""

  def __init__(self, live: bool, key_dir: Path, registry_path: Path):
    self.live = live
    self.pub = publication_mod.load_fixture_publication(HERE)
    self.keys = receipt_store.KeyStore(key_dir)
    self.registry = receipt_store.Registry(registry_path)
    if live:
      if os.environ.get("GOOGLE_CLOUD_PROJECT") != LIVE_PROJECT:
        raise SystemExit(f"--live requires GOOGLE_CLOUD_PROJECT={LIVE_PROJECT}")
      if self.pub["project"] != LIVE_PROJECT:
        raise SystemExit("publication project is not the live project")
      self.session = broker.open_live_session(
          self.pub["project"], self.pub["location"]
      )
      self.fake = None
    else:
      self.fake, _ = hermetic.fixture_world("requester@example.test")
      fake = self.fake
      self.session = broker.open_hermetic_session(
          "requester@example.test",
          lambda: hermetic.HermeticCallerClient(
              fake, "requester@example.test", self.pub
          ),
      )

  def caller(self) -> Any:
    client = self.session.delegated_client()
    if self.live:
      return execute_mod.BigQueryCallerClient(client)
    return client

  def evidence(self, mode: str = "full") -> Any:
    if self.live:
      inner = verify_mod.BigQueryEvidenceClient(self.session.delegated_client())
      if mode == "metadata_only":
        return verify_mod.MetadataOnlyEvidenceClient(inner)
      return inner
    return hermetic.HermeticEvidenceClient(
        self.fake, self.session["authenticated_requester"], mode
    )

  def approve(self, params: dict) -> dict:
    return broker.approve_request(
        self.session,
        self.pub,
        params,
        contracts.AUDIENCE,
        int(time.time()),
        self.registry,
    )


def _now() -> int:
  return int(time.time())


def _issue_then_consume(
    world: World, req: dict, handle: dict, claim: dict, mode: str = "full"
) -> tuple[dict, dict]:
  """Trusted two-step path: independent verifier seals, then consumer gates.

  The clock is read fresh at each step so expiry is judged at the moment
  of each check, never against a timestamp captured before approval.
  """
  reg = world.registry
  issued = verify_mod.verify(
      req["request_id"],
      handle["receipt_id"],
      claim,
      world.evidence(mode),
      reg,
      reg,
      _now(),
      keys=world.keys,
  )
  out = consume_mod.consume(
      req["request_id"],
      handle["receipt_id"],
      claim,
      world.evidence(mode),
      reg,
      reg,
      _now(),
      keys=world.keys,
  )
  return issued, out


def run_case(case: str, world: World) -> dict:
  """Execute one case; returns the consumer output plus private diagnostics."""
  reg = world.registry
  good_claim = {"field": "gross_margin_usd", "value": "400", "unit": "USD"}
  diag: dict[str, Any] = {
      "case": case,
      "live": world.live,
      "synthetic_fixture": True,
  }

  if case == "approved":
    req = world.approve(JAN)
    handle = execute_mod.execute(req, world.pub, world.caller(), reg)
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, good_claim, "full"
    )
  elif case == "sql-substitution":
    req = world.approve(JAN)
    wrong = hermetic.product_cost_only_sql(req["compiled_sql"])
    handle = attacks.adversarial_execute(req, wrong, JAN, world.caller(), reg)
    diag["attack"] = "product-cost-only formula; agent claims 600"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, dict(good_claim, value="600"), "full"
    )
  elif case == "parameter-substitution":
    req = world.approve(JAN)
    handle = attacks.adversarial_execute(
        req, req["compiled_sql"], JAN_FEB, world.caller(), reg
    )
    diag["attack"] = "approved SQL with period_end=2026-02-28; agent claims 515"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, dict(good_claim, value="515"), "full"
    )
  elif case == "display-substitution":
    req = world.approve(JAN)
    handle = execute_mod.execute(req, world.pub, world.caller(), reg)
    diag["attack"] = "valid approved run; agent claims 600"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, dict(good_claim, value="600"), "full"
    )
  elif case == "missing-evidence":
    req = world.approve(JAN)
    handle = attacks.register_invented_job(req, reg)
    diag["attack"] = "invented job id never submitted"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, good_claim, "full"
    )
    req2 = world.approve(JAN)
    handle2 = execute_mod.execute(req2, world.pub, world.caller(), reg)
    diag["issue_out2"], out2 = _issue_then_consume(
        world, req2, handle2, good_claim, "metadata_only"
    )
    diag["metadata_only"] = {
        k: out2[k] for k in ("verdict", "execution_match", "reason_codes")
    }
  elif case == "tamper":
    req = world.approve(JAN)
    handle = execute_mod.execute(req, world.pub, world.caller(), reg)
    first = verify_mod.verify(
        req["request_id"],
        handle["receipt_id"],
        good_claim,
        world.evidence(),
        reg,
        reg,
        _now(),
        keys=world.keys,
    )
    diag["verify_before_tamper"] = first["verdict"]
    tampered = dict(first["receipt"])
    tampered["verdict"] = contracts.VERIFIED
    tampered["job"] = dict(tampered["job"], job_id="okf_rcpt_forged")
    reg.put_receipt(handle["receipt_id"], tampered)
    diag["attack"] = "stored receipt job_id mutated after sealing"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, good_claim, "full"
    )
  elif case == "replay":
    req = world.approve(JAN)
    handle = execute_mod.execute(req, world.pub, world.caller(), reg)
    diag["issue_first"], first = _issue_then_consume(
        world, req, handle, good_claim, "full"
    )
    diag["first_consumption"] = first["verdict"]
    diag["attack"] = "same receipt consumed twice"
    diag["issue_out"], out = _issue_then_consume(
        world, req, handle, good_claim, "full"
    )
  else:
    raise SystemExit(f"unknown case {case}")

  diag["request_id"] = req["request_id"]
  diag["job"] = reg.load_job(req["request_id"])
  diag["output"] = {
      k: v for k, v in out.items() if k not in ("display", "value")
  }
  diag["released"] = "display" in out
  return {"out": out, "diag": diag}


def main(argv: list[str] | None = None) -> int:
  ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  ap.add_argument("--case", choices=CASES, required=True)
  ap.add_argument("--evidence-dir", default=str(HERE / "evidence" / "receipt"))
  ap.add_argument(
      "--live", action="store_true", help="use real BigQuery under ADC"
  )
  ap.add_argument("--key-dir", default=os.environ.get("OKF_RECEIPT_KEY_DIR"))
  ap.add_argument(
      "--registry", default=None, help="private SQLite path (default: temp)"
  )
  args = ap.parse_args(argv)

  key_dir = (
      Path(args.key_dir)
      if args.key_dir
      else Path(tempfile.mkdtemp(prefix="okf-receipt-keys-"))
  )
  registry_path = (
      Path(args.registry)
      if args.registry
      else Path(tempfile.mkdtemp(prefix="okf-receipt-reg-")) / "registry.sqlite"
  )
  world = World(args.live, key_dir, registry_path)
  result = run_case(args.case, world)
  out, diag = result["out"], result["diag"]

  evidence_dir = Path(args.evidence_dir)
  evidence_dir.mkdir(parents=True, exist_ok=True)
  suffix = "live" if args.live else "hermetic"
  (evidence_dir / f"case_{args.case}_{suffix}.json").write_text(
      json.dumps(diag, indent=2, sort_keys=True, default=str), encoding="utf-8"
  )
  mode = "LIVE" if args.live else "HERMETIC (synthetic fixture)"
  if out["verdict"] == contracts.VERIFIED and "display" in out:
    print(f"[{mode}] {out['display']}")
    return 0
  print(
      f"[{mode}] BLOCKED verdict={out['verdict']} execution_match={out['execution_match']}"
      f" reasons={','.join(out['reason_codes'])}"
  )
  return 2


if __name__ == "__main__":
  sys.exit(main())
