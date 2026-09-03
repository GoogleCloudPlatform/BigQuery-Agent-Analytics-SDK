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

"""BQAA ``agent_events`` -> derived OKF v0.2 bundle (one-way, observer-only).

Python port of the github.io ``adapter.js`` (observe + adapt +
computeIdentities) with the stdlib hashing from ``derived_vectors.py``.
Input is a BQAA trace: either the committed live export written by
``observe_agent.py`` or the SYNTHETIC germany fixture. Output is a derived
bundle (path -> text), its identity chain, and a ``context_ref ->
publication_id`` mapping. The authored ``cymbal-finance-core`` bundle is
never read or written. Everything emitted is labelled derived/demo; nothing
is attested.

stdlib only: no ``google.*`` imports here so the hermetic tests never touch
GCP.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

ADAPTER_VERSION = "okf-bqaa-adapter:v0"
LABEL = "derived/demo, observer-only, nothing attested"
NEVER_EMIT = [
    "concept_version_id",
    "bundle_path",
    "source_path",
    "principal",
    "user_id",
    "query_text",
    "sql",
    "parameter_values",
    "destination_table",
]
TYPE_DIRS = {
    "Metric": "metrics",
    "Attested Computation": "computations",
    "Business Concept": "concepts",
    "Policy": "policies",
    "BigQuery Table": "tables",
}
KIND_RETRIEVE = "okf-context:retrieve"
KIND_RECEIPT = "okf-context:attested-computation"
MANIFEST_NAMES = (
    "canonicalization-manifest",
    "semantic-config",
    "resolver-manifest",
    "vocabulary-manifest",
)
BUNDLE_KEY = "bqaa-derived-cymbal-demo"
DEPLOYMENT_KEY = "cymbal-finance-prod/eu/bqaa-derived-demo"
COMPILER_SEMANTICS_VERSION = "okf-context-compiler:v0.1"
PROFILE_CONTRACT_VERSION = "okf-context/1"

HERE = Path(__file__).resolve().parent
DEFAULT_EVENTS = HERE / "fixtures" / "live_observe_agent_events.json"
DEFAULT_MANIFESTS = HERE / "fixtures" / "manifests"


class NotRetrieveShapedError(ValueError):
  """The trace has no OK retrieve + attested-computation tool rows."""


# --------------------------------------------------------------------------
# PROFILE.md hashing (canonical CBOR, domain-separated SHA-256, canon:v1)
# --------------------------------------------------------------------------


def _head(major: int, arg: int) -> bytes:
  if arg < 24:
    return bytes([(major << 5) | arg])
  for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
    if arg < (1 << (8 * size)):
      return bytes([(major << 5) | ai]) + arg.to_bytes(size, "big")
  raise ValueError("length too large")


def cbor(obj: Any) -> bytes:
  """Canonical CBOR (RFC 8949 4.2.1) over the profile-restricted types."""
  if obj is False:
    return b"\xf4"
  if obj is True:
    return b"\xf5"
  if obj is None:
    return b"\xf6"
  if isinstance(obj, int):
    if obj < 0:
      raise TypeError("negative")
    return _head(0, obj)
  if isinstance(obj, bytes):
    return _head(2, len(obj)) + obj
  if isinstance(obj, str):
    b = unicodedata.normalize("NFC", obj).encode("utf-8")
    return _head(3, len(b)) + b
  if isinstance(obj, (list, tuple)):
    return _head(4, len(obj)) + b"".join(cbor(x) for x in obj)
  if isinstance(obj, dict):
    items = sorted((cbor(k), cbor(v)) for k, v in obj.items())
    return _head(5, len(items)) + b"".join(k + v for k, v in items)
  raise TypeError(type(obj))


def h(domain: str, obj: Any) -> bytes:
  """Domain-separated SHA-256: ASCII domain + 0x00 + canonical CBOR."""
  return hashlib.sha256(domain.encode("ascii") + b"\x00" + cbor(obj)).digest()


def hexid(digest: bytes) -> str:
  return "sha256:" + digest.hex()


def normalize_text(s: str) -> str:
  s = unicodedata.normalize("NFC", s.replace("\r\n", "\n").replace("\r", "\n"))
  return "\n".join(ln.rstrip(" \t") for ln in s.split("\n")).rstrip("\n") + "\n"


def split_frontmatter(text: str) -> tuple[str, str]:
  lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
  if lines[0] != "---":
    raise ValueError("no frontmatter block")
  close = lines[1:].index("---") + 1
  return "\n".join(lines[1:close]), "\n".join(lines[close + 1 :])


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def slug(title: str) -> str:
  return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def path_for(item: dict) -> str:
  return (
      TYPE_DIRS.get(item["type"], "concepts")
      + "/"
      + slug(item["title"])
      + ".md"
  )


def rel_link(from_path: str, to_path: str) -> str:
  from_dir = "/".join(from_path.split("/")[:-1])
  to_dir = "/".join(to_path.split("/")[:-1])
  if from_dir == to_dir:
    return to_path.split("/")[-1]
  return "../" + to_path


# --------------------------------------------------------------------------
# trace access: fixture shape (attributes.tool.kind) or live plugin shape
# (content.result.kind); BigQuery JSON columns may arrive as strings.
# --------------------------------------------------------------------------


def _as_obj(v: Any) -> Any:
  if isinstance(v, str):
    try:
      return json.loads(v)
    except ValueError:
      return v
  return v


def _dict(v: Any) -> dict:
  v = _as_obj(v)
  return v if isinstance(v, dict) else {}


def _content(e: dict) -> dict:
  return _dict(e.get("content"))


def _attributes(e: dict) -> dict:
  return _dict(e.get("attributes"))


def _tool_result(e: dict) -> dict:
  return _dict(_content(e).get("result"))


def tool_kind(e: dict) -> str | None:
  """OKF tool kind from ``attributes.tool.kind`` or ``content.result.kind``."""
  kind = _dict(_attributes(e).get("tool")).get("kind")
  return kind or _tool_result(e).get("kind")


def _envelope(e: dict) -> dict:
  return _dict(_attributes(e).get("okf")) or _dict(_tool_result(e).get("okf"))


def _context_ref(e: dict) -> str | None:
  return _attributes(e).get("context_ref") or _tool_result(e).get("context_ref")


def _is_tool_ok(e: dict, kind: str) -> bool:
  return (
      e.get("event_type") == "TOOL_COMPLETED"
      and e.get("status") == "OK"
      and tool_kind(e) == kind
  )


def _question(events: list[dict]) -> str | None:
  for e in events:
    c = _content(e)
    if e.get("event_type") == "LLM_REQUEST" and c.get("role") == "user":
      return c.get("text")
    if e.get("event_type") == "USER_MESSAGE_RECEIVED" and c.get("text_summary"):
      return c["text_summary"]
  for e in events:
    if e.get("event_type") != "LLM_REQUEST":
      continue
    for turn in _as_obj(_content(e).get("prompt")) or []:
      if isinstance(turn, dict) and turn.get("role") == "user":
        return turn.get("content")
  return None


def load_trace(path: str | Path = DEFAULT_EVENTS) -> dict:
  return json.loads(Path(path).read_text("utf-8"))


def build_trace(
    rows: list[dict],
    *,
    table: str,
    writer: dict,
    label: str,
    agent: dict | None = None,
    extra: dict | None = None,
) -> dict:
  """Wrap exported ``agent_events`` rows in the trace container shape."""
  rows = sorted(rows, key=lambda r: str(r.get("timestamp")))
  if agent is None:
    model = None
    for r in rows:
      if r.get("event_type") == "LLM_REQUEST" and _attributes(r).get("model"):
        model = _attributes(r)["model"]
        break
    agent = {
        "name": rows[0].get("agent") if rows else None,
        "framework": "google-adk",
        "model": model,
    }
  trace = {
      "_fixture": (
          "LIVE BQAA export: rows read back from the BigQuery agent_events"
          " table written by BigQueryAgentAnalyticsPlugin while the ADK"
          " observe agent ran. JSON columns parsed; content_parts dropped."
      ),
      "label": label,
      "table": table,
      "writer": writer,
      "agent": agent,
      "session_id": rows[0].get("session_id") if rows else None,
      "trace_id": rows[0].get("trace_id") if rows else None,
      "never_emit": list(NEVER_EMIT),
  }
  trace.update(extra or {})
  trace["events"] = rows
  return trace


# --------------------------------------------------------------------------
# observe: pull only what an observer is allowed to see
# --------------------------------------------------------------------------


def observe(trace: dict) -> dict:
  ev = trace["events"]
  if not ev:
    raise NotRetrieveShapedError("trace has no events")
  retrieve = next((e for e in ev if _is_tool_ok(e, KIND_RETRIEVE)), None)
  receipts = [e for e in ev if _is_tool_ok(e, KIND_RECEIPT)]
  errors = [e for e in ev if e.get("status") == "ERROR"]
  receipt = receipts[-1] if receipts else None
  okf = _envelope(retrieve) if retrieve else {}
  return {
      "table": trace.get("table"),
      "writer": trace.get("writer") or {},
      "agent": trace.get("agent") or {},
      "session_id": ev[0].get("session_id"),
      "trace_id": ev[0].get("trace_id"),
      "invocation_id": ev[0].get("invocation_id"),
      "event_count": len(ev),
      "span": {
          "first": ev[0].get("timestamp"),
          "last": ev[-1].get("timestamp"),
      },
      "question": _question(ev),
      "context_ref": _context_ref(retrieve) if retrieve else None,
      "observed_publication_id": okf.get("publication_id"),
      "observed_publication_note": okf.get("publication_id_note"),
      "mode": okf.get("mode"),
      "items": okf.get("items") or [],
      "excluded": okf.get("excluded") or [],
      "links": okf.get("links") or [],
      "receipt": _envelope(receipt) if receipt else None,
      "receipt_context_ref": _context_ref(receipt) if receipt else None,
      "error_codes": [
          _dict(_attributes(e).get("okf")).get("error_code")
          or e.get("error_message")
          for e in errors
      ],
  }


def require_retrieve_shaped(trace: dict) -> dict:
  """Fail closed unless the trace has OK retrieve + receipt tool rows."""
  obs = observe(trace)
  kinds = sorted({tool_kind(e) or "" for e in trace["events"]} - {""})
  if obs["context_ref"] is None or not obs["items"]:
    raise NotRetrieveShapedError(
        f"no OK TOOL_COMPLETED with kind {KIND_RETRIEVE!r} carrying"
        f" context_ref + items (tool kinds seen: {kinds or 'none'})"
    )
  if obs["receipt"] is None:
    raise NotRetrieveShapedError(
        f"no OK TOOL_COMPLETED with kind {KIND_RECEIPT!r}"
        f" (tool kinds seen: {kinds})"
    )
  return obs


# --------------------------------------------------------------------------
# adapt: derived bundle text (port of stubDoc / logDoc)
# --------------------------------------------------------------------------


def _short_id(value: Any) -> str:
  if isinstance(value, str) and value.startswith("sha256:"):
    return value[:23] + "…"
  return "(none observed)" if value is None else str(value)


def stub_doc(
    obs: dict, item: dict, all_items: list[dict], bundle_key: str
) -> dict:
  path = path_for(item)
  is_excluded = bool(item.get("excluded"))
  status = "deprecated" if is_excluded else "draft"
  receipt = obs.get("receipt") or {}
  is_computation = item["type"] == "Attested Computation" and bool(receipt)
  fm = [f"type: {item['type']}", f"title: {item['title']}"]
  if is_excluded:
    detail = (
        "Observed as excluded from current-mode retrieval"
        f" ({item.get('reason')})."
    )
  else:
    detail = (
        f"Observed at rank {item['rank']} of {len(obs['items'])} in retrieval"
        f" envelope {obs['context_ref']}."
    )
  fm.append(
      f"description: Derived from BQAA observation, not authored. {detail}"
  )
  fm.append(f"status: {status}")
  fm.append(f"tags: [bqaa-derived, observer-only, {slug(item['type'])}]")

  if not is_excluded and item["type"] == "Metric":
    superseded = [
        o
        for o in all_items
        if o.get("excluded")
        and o["type"] == "Metric"
        and "superseded" in (o.get("reason") or "")
    ]
    if superseded:
      fm.append("supersedes:")
      fm.extend(f"  - {path_for(o)}" for o in superseded)

  links = [l for l in obs["links"] if l.get("from") == item["title"]]
  targets = []
  for l in links:
    target = next((o for o in all_items if o["title"] == l.get("to")), None)
    if target:
      targets.append((l, target))
  if links:
    fm.append("links:")
    for l, target in targets:
      fm.append(f"  - target: {path_for(target)}")
      fm.append(f"    rel: {l['rel']}")
      fm.append("    confidence: inferred")

  if is_computation:
    fm.append(f"runtime: {receipt.get('runtime')}")
    fm.append("parameters:")
    for p in receipt.get("parameter_schema") or []:
      req = "true" if p.get("required") else "false"
      fm.append(
          f"  - {{ name: {p['name']}, type: {p['type']}, required: {req} }}"
      )
    if receipt.get("receipt_fields"):
      fm.append("executor:")
      fm.append("  receipt:")
      fm.extend(f"    - {f}" for f in receipt["receipt_fields"])

  fm.append("sources:")
  fm.append(
      f"  - resource: bqaa://{obs['table']}?session_id={obs['session_id']}"
  )
  fm.append(
      f"    title: BQAA observer trace {obs['trace_id']}"
      f" ({obs['writer'].get('label')})"
  )
  if is_computation and receipt.get("computation_version_id"):
    fm.append(
        f"  - resource: okf:computation-version:{receipt['computation_version_id']}"
    )
    fm.append(
        "    title: Sanctioned artifact in authored publication"
        f" {_short_id(receipt.get('publication_id'))} (observed via receipt"
        f" {receipt.get('receipt_id')})"
    )

  body = [
      f"# {item['title']}",
      "",
      "**Derived from BQAA observation, not authored.** This stub was emitted by",
      f"`{ADAPTER_VERSION}` from `{obs['event_count']}` observer events in",
      f"`{obs['table']}` (session `{obs['session_id']}`). The observer",
      "sees titles, types, ranks, edges and receipts — never authored text,",
      "bundle paths, `concept_version_id`, SQL, parameter values or the principal.",
      "",
  ]
  if is_excluded:
    current = all_items[0]
    body.append("## Observed exclusion")
    body.append("")
    body.append(
        f"Excluded from `{obs['mode']}`-mode retrieval: {item.get('reason')}."
    )
    body.append(
        f"The current definition is [{current['title']}]"
        f"({rel_link(path, path_for(current))})."
    )
  else:
    body.append("## Observed retrieval")
    body.append("")
    body.append(
        f"- context_ref `{obs['context_ref']}`, rank {item['rank']} of"
        f" {len(obs['items'])}, mode `{obs['mode']}`."
    )
    body.append(
        f"- Observed type `{item['type']}`; authored body not observed."
    )
    for l, target in targets:
      body.append(
          f"- Edge `{l['rel']}` → [{l['to']}]({rel_link(path, path_for(target))})"
          " (inferred from envelope attributes)."
      )
  if is_computation:
    params = receipt.get("parameter_schema") or []
    body.append("")
    body.append("## Observed execution contract")
    body.append("")
    body.append(
        f"- Runtime `{receipt.get('runtime')}`; {len(params)} declared"
        " parameters (names and types only; values are never observed)."
    )
    if receipt.get("computation_version_id"):
      body.append(
          "- No `computation:` artifact is declared here: the observer never"
          " sees SQL. The sanctioned artifact lives in the authored publication"
          " and is referenced by its `computation_version_id` under `sources`."
      )
    else:
      body.append(
          "- No `computation:` artifact is declared here: the observer never"
          " sees SQL, and this receipt carries no computation version because"
          " nothing was executed or attested."
      )
    body.append(
        f"- Last observed verdict `{receipt.get('verdict')}`"
        f" (`{receipt.get('verdict_reason')}`), receipt"
        f" `{receipt.get('receipt_id')}`."
    )
    if obs["error_codes"]:
      codes = ", ".join(f"`{c}`" for c in obs["error_codes"])
      body.append(f"- Observed fail-closed errors before success: {codes}.")
  body.append("")
  body.append(
      f"Authored counterpart: `{_short_id(obs['observed_publication_id'])}`"
      " (publication_id observed on the tool span). This derived bundle"
      f" (`{bundle_key}`) never writes back to it."
  )
  if obs.get("observed_publication_note"):
    body.append(
        "Observed publication note: " f"{obs['observed_publication_note']}."
    )
  text = "---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body) + "\n"
  return {"path": path, "text": text}


def log_doc(obs: dict, docs: list[dict], bundle_key: str) -> dict:
  day = str(obs["span"]["last"])[:10]
  first = obs["items"][0]
  lines = [
      "# Log",
      "",
      f"## {day}",
      "",
      "- Derived from BQAA observation, not authored."
      f" `{ADAPTER_VERSION}` read {obs['event_count']} observer events"
      f" (session `{obs['session_id']}`, trace `{obs['trace_id']}`) from"
      f" `{obs['table']}` and emitted {len(docs)} stubs into `{bundle_key}`."
      " The authored bundle was not read and was not modified.",
      f"- Observed: [{first['title']}]({path_for(first)}) retrieved at rank 1"
      f" for context_ref `{obs['context_ref']}` ({obs['mode']} mode).",
  ]
  for x in obs["excluded"]:
    lines.append(
        f"- Observed (not affirmed here): [{x['title']}]({path_for(x)})"
        f" excluded from retrieval — {x.get('reason')}."
    )
  receipt = obs.get("receipt")
  if receipt:
    lines.append(
        f"- Observed receipt `{receipt.get('receipt_id')}` on"
        f" `{obs['receipt_context_ref']}`: verdict `{receipt.get('verdict')}`"
        f" (`{receipt.get('verdict_reason')}`). Nothing was executed or"
        " attested."
    )
  return {"path": "log.md", "text": "\n".join(lines) + "\n"}


def adapt(trace: dict) -> dict:
  obs = require_retrieve_shaped(trace)
  all_items = list(obs["items"])
  for x in obs["excluded"]:
    all_items.append(
        {
            "type": x["type"],
            "title": x["title"],
            "reason": x.get("reason"),
            "excluded": True,
        }
    )
  docs = [stub_doc(obs, item, all_items, BUNDLE_KEY) for item in all_items]
  docs.append(log_doc(obs, docs, BUNDLE_KEY))
  files = {d["path"]: d["text"] for d in docs}
  constants = {
      "bundle_key": BUNDLE_KEY,
      "source_uri": f"bqaa://{obs['table']}?session_id={obs['session_id']}",
      "revision": f"bqaa-trace:{obs['trace_id']}",
      "deployment_key": DEPLOYMENT_KEY,
      "compiler_semantics_version": COMPILER_SEMANTICS_VERSION,
      "profile_contract_version": PROFILE_CONTRACT_VERSION,
      "adapter_version": ADAPTER_VERSION,
  }
  return {
      "observation": obs,
      "constants": constants,
      "files": files,
      "docs": docs,
      "bundle_key": BUNDLE_KEY,
  }


# --------------------------------------------------------------------------
# identity chain (PROFILE.md; mirrors vectors_gen.py steps 1-6)
# --------------------------------------------------------------------------


def load_manifests(directory: str | Path = DEFAULT_MANIFESTS) -> dict:
  directory = Path(directory)
  return {n: (directory / f"{n}.json").read_bytes() for n in MANIFEST_NAMES}


def compute_identities(files: dict, constants: dict, manifests: dict) -> dict:
  paths = sorted(files, key=lambda p: p.encode("utf-8"))
  file_hashes = {}
  for p in paths:
    data = files[p]
    if isinstance(data, str):
      data = data.encode("utf-8")
    file_hashes[p] = hashlib.sha256(data).digest()
  pairs = [[p, file_hashes[p]] for p in paths]
  source_manifest_hash = h("okf-context:source-manifest:v1", pairs)

  mh = {}
  for n in MANIFEST_NAMES:
    if n not in manifests:
      raise ValueError(f"missing compile manifest: {n}")
    mh[n] = hashlib.sha256(manifests[n]).digest()

  observation_id = h(
      "okf-context:observation:v1",
      {
          "bundle_key": constants["bundle_key"],
          "revision": constants["revision"],
          "source_uri": constants["source_uri"],
      },
  )
  snapshot_id = h(
      "okf-context:snapshot:v1",
      {
          "bundle_key": constants["bundle_key"],
          "source_manifest_hash": source_manifest_hash,
          "canonicalization_manifest_hash": mh["canonicalization-manifest"],
          "compiler_semantics_version": constants["compiler_semantics_version"],
          "semantic_config_hash": mh["semantic-config"],
          "vocabulary_manifest_hash": mh["vocabulary-manifest"],
          "resolver_manifest_hash": mh["resolver-manifest"],
      },
  )
  publication_id = h(
      "okf-context:publication:v1",
      {
          "deployment_key": constants["deployment_key"],
          "observation_id": observation_id,
          "snapshot_id": snapshot_id,
          "profile_contract_version": constants["profile_contract_version"],
      },
  )

  concept_versions = {}
  for p in paths:
    if not p.endswith(".md") or p in ("index.md", "log.md"):
      continue
    text = files[p]
    if isinstance(text, bytes):
      text = text.decode("utf-8")
    fm, body = split_frontmatter(text)
    key = f"{constants['bundle_key']}#{p[:-3]}"
    concept_versions[p] = hexid(
        h(
            "okf-context:concept-version:v1",
            [key, normalize_text(fm), normalize_text(body)],
        )
    )

  return {
      "observation_id": hexid(observation_id),
      "snapshot_id": hexid(snapshot_id),
      "publication_id": hexid(publication_id),
      "source_manifest_hash": hexid(source_manifest_hash),
      "manifest_hashes": {n: hexid(d) for n, d in mh.items()},
      "file_sha256": {p: file_hashes[p].hex() for p in paths},
      "concept_version_ids": concept_versions,
  }


def demo_envelope_id(publication_id: str) -> str:
  """Opaque demo envelope id (labelled as minted by the demo, not random)."""
  return "env-" + h("okf-demo:envelope-id:v0", publication_id).hex()[:16]


# --------------------------------------------------------------------------
# project: write the derived bundle + identities + mapping (no Catalog)
# --------------------------------------------------------------------------


def mapping_for(obs: dict, identities: dict) -> dict:
  refs = [obs["context_ref"]]
  if obs.get("receipt_context_ref") and obs["receipt_context_ref"] not in refs:
    refs.append(obs["receipt_context_ref"])
  return {ref: identities["publication_id"] for ref in refs if ref}


def identities_document(result: dict, identities: dict) -> dict:
  obs = result["observation"]
  doc = {
      "_comment": (
          "DERIVED / DEMO. Computed by okf-bqaa-adapter:v0 from a live BQAA"
          " agent_events export (PROFILE.md rules). Not canonical authoring;"
          " distinct bundle_key from cymbal-finance-core. Nothing attested."
      ),
      "label": LABEL,
      "inputs": {
          **result["constants"],
          "session_id": obs["session_id"],
          "trace_id": obs["trace_id"],
          "observed_publication_id": obs["observed_publication_id"],
      },
  }
  doc.update(identities)
  doc["demo_envelope_id"] = demo_envelope_id(identities["publication_id"])
  return doc


def project(result: dict, identities: dict, out_dir: str | Path) -> dict:
  out = Path(out_dir)
  bundle = out / "bundle"
  bundle.mkdir(parents=True, exist_ok=True)
  for path, text in result["files"].items():
    target = bundle / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, "utf-8")
  obs = result["observation"]
  mapping = {
      "adapter_version": ADAPTER_VERSION,
      "label": LABEL,
      "source": {
          "table": obs["table"],
          "session_id": obs["session_id"],
          "trace_id": obs["trace_id"],
      },
      "mapping": mapping_for(obs, identities),
  }
  written = {
      "bundle": bundle,
      "identities": out / "identities.json",
      "observation": out / "observation.json",
      "mapping": out / "mapping.json",
  }
  _write_json(written["identities"], identities_document(result, identities))
  _write_json(written["observation"], {"label": LABEL, **obs})
  _write_json(written["mapping"], mapping)
  return written


def _write_json(path: Path, obj: Any) -> None:
  path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", "utf-8")
