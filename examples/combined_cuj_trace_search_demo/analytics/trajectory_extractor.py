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

"""Deterministic S1/S2/S3 trajectory extractors with zero LLM calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import html
import json
import re
from typing import Any

DEFAULT_S3_MAX_CHARS = 4800
_EMBED_TOKEN_LIMIT = 2048

# Step 1: Secret redaction runs first so split tokens never leak.
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"(Authorization\s*:\s*Bearer\s+)\S+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{6,}\b", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
    (
        re.compile(
            r"""(["']?(?:api_key|password|client_secret|access_token|"""
            r"""refresh_token|private_key)["']?\s*[:=]\s*["']?)[^\s"',}\]]+""",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
)

# Step 2: Critical error markers captured before boilerplate stripping/clamping.
_CRITICAL_ERROR_MARKERS_RE = re.compile(
    r"\b(?:"
    r"CARD_EXPIRED|DEADLINE_EXCEEDED|PERMISSION_DENIED|"
    r"SHIPMENT_ALREADY_DISPATCHED|AUDIT_SYNC_WARNING|CHECKSUM_MISMATCH|"
    r"ORDER_NOT_FOUND|INVALID_AMOUNT|"
    r"[A-Z][A-Z0-9]{1,20}(?:_[A-Z0-9]{2,20})*"
    r"_(?:ERROR|EXPIRED|EXCEEDED|DENIED|FAILED|DISPATCHED|TIMEOUT|"
    r"MISMATCH|WARNING|REJECTED)"
    r")\b"
)

# Step 3: HTML, base64, hex dump, UUID, and stack trace strippers.
_HTML_BLOCK_RE = re.compile(
    r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.IGNORECASE
)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*?)?/?>")

_DATA_URI_B64_RE = re.compile(
    r"data:[A-Za-z0-9/+.-]+;base64,[A-Za-z0-9+/=]{24,}"
)
_BARE_B64_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])[A-Za-z0-9+/]{64,}={0,2}(?![A-Za-z0-9_/-])"
)
_HEX_DUMP_SEQ_RE = re.compile(
    r"(?:\b[0-9a-fA-F]{2}\b[\s:,-]+){8,}\b[0-9a-fA-F]{2}\b"
)
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")
_LONG_HEX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[0-9a-fA-F]{16,}(?![A-Za-z0-9_-])"
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_PY_STACK_RE = re.compile(r'(?:File\s+"[^"]+",\s+line\s+\d+,\s+in\s+\S+\s*)+')
_JAVA_STACK_RE = re.compile(r"(?:\bat\s+[\w.$]+\([^)]*\)\s*){2,}")

# Step 4: Compact embedded JSON boilerplate while keeping URLs/timestamps/underscores.
_JSON_SIMPLE_QUOTED_RE = re.compile(r"""["']([A-Za-z0-9_./@-]+)["']""")
_JSON_KEY_COLON_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s+(?!//)")
_JSON_BRACES_RE = re.compile(r"[{}\[\]]+")
_MULTI_PUNCT_RE = re.compile(r"[,;]{2,}")
_WS_RE = re.compile(r"\s+")

_PRIORITY_TAIL_KEYS = frozenset(
    {"error", "error_code", "message", "status", "tail_code", "ok"}
)


def estimate_tokens(text: str) -> int:
  """Deterministic token estimate (~4 ASCII chars per token)."""
  if not text:
    return 0
  return max(1, (len(text) + 3) // 4)


def _to_ascii_single_line(text: str) -> str:
  """Folds smart punctuation and collapses newlines/spaces to single ASCII."""
  folded = (
      str(text)
      .replace("\u2212", "-")
      .replace("\u2013", "--")
      .replace("\u2014", "--")
      .replace("\u00a0", " ")
      .replace("\u2018", "'")
      .replace("\u2019", "'")
      .replace("\u201c", '"')
      .replace("\u201d", '"')
  )
  ascii_only = "".join(ch if ord(ch) < 128 else "?" for ch in folded)
  return _WS_RE.sub(" ", ascii_only).strip()


def _redact_secrets_first(text: str) -> str:
  out = str(text)
  for pattern, repl in _SECRET_RULES:
    out = pattern.sub(repl, out)
  return out


def _extract_error_markers(text: str) -> list[str]:
  seen: set[str] = set()
  ordered: list[str] = []
  for match in _CRITICAL_ERROR_MARKERS_RE.finditer(text):
    token = match.group(0)
    if token not in seen:
      seen.add(token)
      ordered.append(token)
  return ordered


def _strip_boilerplate(text: str) -> str:
  """Strips HTML, hex dumps, base64, UUIDs, and redundant JSON punctuation."""
  out = _redact_secrets_first(text)
  out = _HTML_BLOCK_RE.sub(" ", out)
  out = _HTML_COMMENT_RE.sub(" ", out)
  if "<" in out and ">" in out:
    out = _HTML_TAG_RE.sub(" ", out)
  out = html.unescape(out)

  out = _DATA_URI_B64_RE.sub("[b64]", out)
  out = _BARE_B64_RE.sub("[b64]", out)
  out = _PY_STACK_RE.sub(" [stack] ", out)
  out = _JAVA_STACK_RE.sub(" [stack] ", out)
  out = _HEX_DUMP_SEQ_RE.sub("[hex]", out)
  out = _HEX_ADDR_RE.sub("[hex]", out)
  out = _LONG_HEX_RE.sub("[hex]", out)
  out = _UUID_RE.sub("[uuid]", out)

  # Shield bracketed placeholders before stripping JSON braces/brackets.
  shields = {
      "[REDACTED]": "__SHIELD_REDACTED__",
      "[b64]": "__SHIELD_B64__",
      "[hex]": "__SHIELD_HEX__",
      "[uuid]": "__SHIELD_UUID__",
      "[stack]": "__SHIELD_STACK__",
  }
  for src, token in shields.items():
    out = out.replace(src, token)

  out = _JSON_SIMPLE_QUOTED_RE.sub(r"\1", out)
  out = _JSON_KEY_COLON_RE.sub(r"\1=", out)
  out = _JSON_BRACES_RE.sub(" ", out)
  out = _MULTI_PUNCT_RE.sub(",", out)

  for src, token in shields.items():
    out = out.replace(token, src)

  return _to_ascii_single_line(out)


def _flatten_structured_value(val: Any, *, depth: int = 0) -> str:
  """Flattens dicts/lists into compact k=v text, hoisting error keys last."""
  if isinstance(val, Mapping):
    if depth >= 4:
      return _strip_boilerplate(json.dumps(val, separators=(",", ":")))
    normal_items: list[tuple[str, Any]] = []
    tail_items: list[tuple[str, Any]] = []
    for key in sorted(val.keys(), key=str):
      key_str = str(key)
      if key_str in _PRIORITY_TAIL_KEYS:
        tail_items.append((key_str, val[key]))
      else:
        normal_items.append((key_str, val[key]))
    ordered = normal_items + tail_items
    parts = [
        f"{k}={_flatten_structured_value(v, depth=depth + 1)}"
        for k, v in ordered
    ]
    return " ".join(parts)
  if isinstance(val, Sequence) and not isinstance(val, (str, bytes, bytearray)):
    items = list(val)
    if len(items) > 4:
      head = [_flatten_structured_value(x, depth=depth + 1) for x in items[:2]]
      tail = _flatten_structured_value(items[-1], depth=depth + 1)
      return " ".join(head + [f"...[{len(items) - 3}_more]...", tail])
    return " ".join(
        _flatten_structured_value(x, depth=depth + 1) for x in items
    )
  if isinstance(val, (bytes, bytearray)):
    val = val.decode("utf-8", errors="replace")
  return _strip_boilerplate(str(val))


def _head_tail_clamp(
    text: str,
    *,
    max_len: int,
    head_len: int,
    tail_len: int,
    required_markers: Sequence[str] = (),
) -> str:
  """Clamps long text with head+tail preservation and error-marker pinning."""
  if len(text) <= max_len:
    return text
  sep = " ...[truncated]... "
  avail = max(16, max_len - len(sep))
  h = min(head_len, max(8, avail // 2))
  t = min(tail_len, max(8, avail - h))
  head = text[:h].rstrip()
  tail = text[-t:].lstrip()
  combined = f"{head}{sep}{tail}"

  missing = [m for m in required_markers if m and m not in combined]
  if missing:
    pin = " " + " ".join(missing)
    if len(pin) < avail:
      rem = max(8, avail - len(pin))
      h2 = max(8, min(h, rem // 2))
      t2 = max(0, rem - h2)
      tail_part = text[-t2:].lstrip() if t2 > 0 else ""
      combined = f"{text[:h2].rstrip()}{sep}{tail_part}{pin}"
  return combined[:max_len]


def extract_s1_raw_trace(trace_obj: Mapping[str, Any]) -> str:
  """S1: Canonical compact JSON of full AgentData trace (includes agents)."""
  if not isinstance(trace_obj, Mapping):
    return "{}"
  return json.dumps(trace_obj, separators=(",", ":"), ensure_ascii=False)


def extract_s2_subfields(trace_obj: Mapping[str, Any]) -> str:
  """S2: Drops trace['agents'] and extracts turn events verbatim."""
  if not isinstance(trace_obj, Mapping):
    return ""
  turns = trace_obj.get("turns")
  if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
    return ""
  lines: list[str] = []
  for turn in turns:
    if not isinstance(turn, Mapping):
      continue
    events = turn.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
      continue
    for event in events:
      if not isinstance(event, Mapping):
        continue
      ev_time = str(event.get("event_time") or "")
      author = str(event.get("author") or "unknown")
      prefix = f"[{ev_time}] {author} " if ev_time else f"{author} "
      content = event.get("content")
      if not isinstance(content, Mapping):
        if event:
          lines.append(
              prefix
              + json.dumps(event, separators=(",", ":"), ensure_ascii=False)
          )
        continue
      role = str(content.get("role") or "")
      parts = content.get("parts")
      if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
        continue
      for part in parts:
        if not isinstance(part, Mapping):
          continue
        if part.get("text") is not None:
          label = "USER" if role == "user" or author == "user" else "AGENT"
          lines.append(f"{prefix}{label}: {part['text']}")
        elif isinstance(part.get("function_call"), Mapping):
          fc = part["function_call"]
          args_json = json.dumps(
              fc.get("args") or {}, separators=(",", ":"), ensure_ascii=False
          )
          lines.append(f"{prefix}TOOL_CALL: {fc.get('name', '')}({args_json})")
        elif isinstance(part.get("function_response"), Mapping):
          fr = part["function_response"]
          resp_json = json.dumps(
              fr.get("response") or {},
              separators=(",", ":"),
              ensure_ascii=False,
          )
          lines.append(f"{prefix}TOOL_RES: {fr.get('name', '')} -> {resp_json}")
  return "\n".join(lines)


def _infer_metrics_from_trace(
    trace_obj: Mapping[str, Any],
    metrics: Mapping[str, Any] | None,
) -> dict[str, Any]:
  """Resolves [OUTCOME: ...] metadata from explicit metrics or trace fallback."""
  m = dict(metrics) if isinstance(metrics, Mapping) else {}
  root_agent = str(m.get("root_agent") or "").strip()
  if not root_agent and isinstance(trace_obj.get("agents"), Mapping):
    agents = trace_obj["agents"]
    if agents:
      root_agent = str(next(iter(agents.keys())))

  tool_calls = 0
  tool_errors = 0
  has_error = bool(m.get("has_error", False))
  consecutive = 0
  max_consecutive = 0
  prev_sig: tuple[str, str] | None = None

  turns = trace_obj.get("turns") if isinstance(trace_obj, Mapping) else None
  if isinstance(turns, Sequence) and not isinstance(turns, (str, bytes)):
    for turn in turns:
      if not isinstance(turn, Mapping):
        continue
      for ev in turn.get("events") or []:
        if not isinstance(ev, Mapping):
          continue
        author = str(ev.get("author") or "")
        if not root_agent and author and author != "user":
          root_agent = author
        content = ev.get("content")
        if not isinstance(content, Mapping):
          continue
        for part in content.get("parts") or []:
          if not isinstance(part, Mapping):
            continue
          if isinstance(part.get("function_call"), Mapping):
            fc = part["function_call"]
            tool_calls += 1
            sig = (
                str(fc.get("name") or ""),
                json.dumps(fc.get("args") or {}, sort_keys=True),
            )
            if sig == prev_sig:
              consecutive += 1
            else:
              prev_sig = sig
              consecutive = 1
            if consecutive > max_consecutive:
              max_consecutive = consecutive
          elif isinstance(part.get("function_response"), Mapping):
            fr = part["function_response"]
            resp = fr.get("response")
            if isinstance(resp, Mapping) and (
                resp.get("ok") is False
                or bool(resp.get("error_code"))
                or bool(resp.get("error"))
            ):
              tool_errors += 1
              has_error = True

  if m.get("tool_call_count") is not None:
    tool_calls = int(m["tool_call_count"])
  if m.get("tool_error_count") is not None:
    tool_errors = int(m["tool_error_count"])
  if "has_tool_loop" in m:
    has_loop = bool(m["has_tool_loop"])
  else:
    has_loop = max_consecutive >= 3
  if tool_errors > 0 or bool(str(m.get("error_summary") or "").strip()):
    has_error = True
  latency_ms = int(m.get("turn_latency_ms") or 0)
  return {
      "root_agent": _to_ascii_single_line(root_agent or "customer_ops_agent"),
      "has_error": has_error,
      "tool_calls": tool_calls,
      "tool_errors": tool_errors,
      "loop": has_loop,
      "latency_ms": latency_ms,
  }


def extract_s3_outcome_first(
    trace_obj: Mapping[str, Any],
    *,
    metrics: Mapping[str, Any] | None = None,
    max_chars: int = DEFAULT_S3_MAX_CHARS,
) -> str:
  """S3: Outcome-first deterministic extraction bounded under max_chars.

  Drops system prompt and tool schema boilerplate, redacts credentials first,
  strips HTML/hex/base64/stack dumps, collapses retry loops, preserves
  trailing error codes, and keeps single-line ASCII labels (`USER:`,
  `TOOL_CALL:`, `TOOL_RES:`, `AGENT:`).
  """
  if not isinstance(trace_obj, Mapping):
    trace_obj = {}
  meta = _infer_metrics_from_trace(trace_obj, metrics)
  outcome_header = (
      f"[OUTCOME: root_agent={meta['root_agent']} "
      f"has_error={meta['has_error']} tool_calls={meta['tool_calls']} "
      f"tool_errors={meta['tool_errors']} loop={meta['loop']} "
      f"latency_ms={meta['latency_ms']}]"
  )

  all_markers: list[str] = []
  seen_markers: set[str] = set()

  def _record_markers(raw_text: str) -> list[str]:
    found = _extract_error_markers(raw_text)
    for tok in found:
      if tok not in seen_markers:
        seen_markers.add(tok)
        all_markers.append(tok)
    return found

  error_summary = str((metrics or {}).get("error_summary") or "").strip()
  if error_summary:
    _record_markers(_redact_secrets_first(error_summary))

  user_lines: list[str] = []
  agent_lines: list[str] = []
  middle_items: list[tuple[str, str]] = []

  turns = trace_obj.get("turns")
  if isinstance(turns, Sequence) and not isinstance(turns, (str, bytes)):
    for turn in turns:
      if not isinstance(turn, Mapping):
        continue
      for event in turn.get("events") or []:
        if not isinstance(event, Mapping):
          continue
        author = str(event.get("author") or "")
        content = event.get("content")
        if not isinstance(content, Mapping):
          continue
        role = str(content.get("role") or "")
        for part in content.get("parts") or []:
          if not isinstance(part, Mapping):
            continue
          if part.get("thought") is True:
            continue
          if "inline_data" in part and isinstance(part["inline_data"], Mapping):
            mime = str(part["inline_data"].get("mime_type") or "binary")
            middle_items.append(("TEXT", f"[inline_data:{mime}]"))
            continue
          if part.get("text") is not None:
            raw_txt = _redact_secrets_first(str(part["text"]))
            markers = _record_markers(raw_txt)
            clean_txt = _strip_boilerplate(raw_txt)
            if not clean_txt:
              continue
            is_user = role == "user" or author == "user"
            if is_user:
              clamped = _head_tail_clamp(
                  clean_txt,
                  max_len=320,
                  head_len=160,
                  tail_len=130,
                  required_markers=markers,
              )
              user_lines.append(f"USER: {clamped}")
            else:
              clamped = _head_tail_clamp(
                  clean_txt,
                  max_len=400,
                  head_len=200,
                  tail_len=170,
                  required_markers=markers,
              )
              agent_lines.append(f"AGENT: {clamped}")
          elif isinstance(part.get("function_call"), Mapping):
            fc = part["function_call"]
            tname = _to_ascii_single_line(str(fc.get("name") or "tool"))
            raw_args = json.dumps(fc.get("args") or {}, sort_keys=True)
            markers = _record_markers(_redact_secrets_first(raw_args))
            flat_args = _flatten_structured_value(fc.get("args") or {})
            clamped_args = _head_tail_clamp(
                flat_args,
                max_len=240,
                head_len=110,
                tail_len=105,
                required_markers=markers,
            )
            call_line = f"TOOL_CALL: {tname}({clamped_args})"
            middle_items.append((f"CALL:{call_line}", call_line))
          elif isinstance(part.get("function_response"), Mapping):
            fr = part["function_response"]
            tname = _to_ascii_single_line(str(fr.get("name") or "tool"))
            resp_obj = fr.get("response") or {}
            raw_resp = (
                json.dumps(resp_obj, sort_keys=True)
                if isinstance(resp_obj, (Mapping, list))
                else str(resp_obj)
            )
            markers = _record_markers(_redact_secrets_first(raw_resp))
            flat_resp = _flatten_structured_value(resp_obj)
            clamped_resp = _head_tail_clamp(
                flat_resp,
                max_len=360,
                head_len=140,
                tail_len=180,
                required_markers=markers,
            )
            res_line = f"TOOL_RES: {tname} -> {clamped_resp}"
            middle_items.append((f"RES:{res_line}", res_line))

  # Collapse consecutive identical (TOOL_CALL, TOOL_RES) or single calls.
  collapsed_middle: list[str] = []
  idx = 0
  n_mid = len(middle_items)
  while idx < n_mid:
    if (
        idx + 1 < n_mid
        and middle_items[idx][0].startswith("CALL:")
        and middle_items[idx + 1][0].startswith("RES:")
    ):
      pair_sig = (middle_items[idx][1], middle_items[idx + 1][1])
      repeat = 1
      cursor = idx + 2
      while (
          cursor + 1 < n_mid
          and middle_items[cursor][1] == pair_sig[0]
          and middle_items[cursor + 1][1] == pair_sig[1]
      ):
        repeat += 1
        cursor += 2
      if repeat >= 2:
        collapsed_middle.append(f"{pair_sig[0]} [x{repeat}]")
        collapsed_middle.append(pair_sig[1])
      else:
        collapsed_middle.append(pair_sig[0])
        collapsed_middle.append(pair_sig[1])
      idx = cursor
    else:
      repeat = 1
      sig = middle_items[idx][1]
      cursor = idx + 1
      while cursor < n_mid and middle_items[cursor][1] == sig:
        repeat += 1
        cursor += 1
      if repeat >= 2:
        collapsed_middle.append(f"{sig} [x{repeat}]")
      else:
        collapsed_middle.append(sig)
      idx = cursor

  # 3-Zone max_chars budgeting: Zone 1 (Outcome + first User), Zone 3 (final Agent),
  # Zone 2 (Middle tool calls/results).
  zone1 = [outcome_header]
  if user_lines:
    zone1.append(user_lines[0])
  zone3: list[str] = []
  if agent_lines:
    zone3.append(agent_lines[-1])

  def _assemble(mid: Sequence[str], footer: str = "") -> str:
    parts = list(zone1) + list(mid) + list(zone3)
    if footer:
      parts.append(footer)
    return "\n".join(parts)

  candidate = _assemble(collapsed_middle)
  missing_in_outer = [m for m in all_markers if m not in candidate]
  footer_line = (
      f"[ERRORS: {' '.join(missing_in_outer)}]" if missing_in_outer else ""
  )
  candidate = _assemble(collapsed_middle, footer_line)

  if len(candidate) <= max_chars:
    return candidate

  # Fit middle lines inside remaining budget, preferring final tool outcomes.
  zone1_str = "\n".join(zone1)
  zone3_str = "\n".join(zone3)
  outer_overhead = len(zone1_str) + len(zone3_str) + 4
  if all_markers:
    # Reserve space for [ERRORS: ...] if middle lines get dropped.
    potential_footer = f"[ERRORS: {' '.join(all_markers)}]"
    outer_overhead += len(potential_footer) + 1

  mid_budget = max(0, max_chars - outer_overhead)
  kept_head: list[str] = []
  kept_tail: list[str] = []
  if mid_budget > 40 and collapsed_middle:
    left = 0
    right = len(collapsed_middle) - 1
    used = 0
    take_tail_first = True
    while left <= right:
      omit_notice = f"... [{right - left + 1} events omitted] ..."
      if take_tail_first:
        item = collapsed_middle[right]
        cost = len(item) + 1
        if used + cost + len(omit_notice) + 2 <= mid_budget:
          kept_tail.append(item)
          used += cost
          right -= 1
          take_tail_first = False
          continue
      else:
        item = collapsed_middle[left]
        cost = len(item) + 1
        if used + cost + len(omit_notice) + 2 <= mid_budget:
          kept_head.append(item)
          used += cost
          left += 1
          take_tail_first = True
          continue
      break
    kept_tail.reverse()
    omitted_count = right - left + 1
    if omitted_count > 0:
      mid_lines = (
          kept_head + [f"... [{omitted_count} events omitted] ..."] + kept_tail
      )
    else:
      mid_lines = kept_head + kept_tail
  else:
    mid_lines = []

  pre_footer = _assemble(mid_lines)
  missing = [m for m in all_markers if m not in pre_footer]
  footer = f"[ERRORS: {' '.join(missing)}]" if missing else ""
  final = _assemble(mid_lines, footer)

  if len(final) > max_chars:
    # Drop middle completely, then clamp USER/AGENT while pinning header+errors.
    pre_footer = _assemble([])
    missing = [m for m in all_markers if m not in pre_footer]
    footer = f"[ERRORS: {' '.join(missing)}]" if missing else ""
    final = _assemble([], footer)
    if len(final) > max_chars:
      # Shrink USER or AGENT lines while retaining outcome_header and footer.
      tail_pin = footer if footer else ""
      header_only = outcome_header
      budget = max(0, max_chars - len(header_only) - len(tail_pin) - 2)
      compact_parts = [header_only]
      if agent_lines and budget > 16:
        compact_parts.append(agent_lines[-1][:budget].rstrip())
      if tail_pin:
        compact_parts.append(tail_pin)
      final = "\n".join(compact_parts)[:max_chars]
  return final


def compute_ablation_metrics(
    trace_obj: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
    *,
    max_chars: int = DEFAULT_S3_MAX_CHARS,
) -> dict[str, Any]:
  """Computes S1/S2/S3 byte, token, and 2048-token cap flags for one turn."""
  s1 = extract_s1_raw_trace(trace_obj)
  s2 = extract_s2_subfields(trace_obj)
  s3 = extract_s3_outcome_first(trace_obj, metrics=metrics, max_chars=max_chars)

  s1_tok = estimate_tokens(s1)
  s2_tok = estimate_tokens(s2)
  s3_tok = estimate_tokens(s3)
  return {
      "s1_bytes": len(s1.encode("utf-8")),
      "s1_tokens_est": s1_tok,
      "s1_exceeds_2048": s1_tok > _EMBED_TOKEN_LIMIT,
      "s2_bytes": len(s2.encode("utf-8")),
      "s2_tokens_est": s2_tok,
      "s2_exceeds_2048": s2_tok > _EMBED_TOKEN_LIMIT,
      "s3_bytes": len(s3.encode("utf-8")),
      "s3_tokens_est": s3_tok,
      "s3_exceeds_2048": s3_tok > _EMBED_TOKEN_LIMIT,
      "s3_text": s3,
  }


def summarize_ablation(
    turn_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
  """Aggregates S1/S2/S3 byte/token metrics across executed eval_turns rows."""
  count = len(turn_rows)
  if count == 0:
    return {
        "turn_count": 0,
        "s1_avg_bytes": 0,
        "s1_avg_tokens": 0,
        "s1_over_2048_count": 0,
        "s2_avg_bytes": 0,
        "s2_avg_tokens": 0,
        "s2_over_2048_count": 0,
        "s3_avg_bytes": 0,
        "s3_avg_tokens": 0,
        "s3_over_2048_count": 0,
        "ingest_llm_cost_usd": 0.0,
    }

  ablations = [dict(r.get("ablation") or {}) for r in turn_rows]
  return {
      "turn_count": count,
      "s1_avg_bytes": round(
          sum(int(a.get("s1_bytes") or 0) for a in ablations) / count
      ),
      "s1_avg_tokens": round(
          sum(int(a.get("s1_tokens_est") or 0) for a in ablations) / count
      ),
      "s1_over_2048_count": sum(
          1 for a in ablations if bool(a.get("s1_exceeds_2048"))
      ),
      "s2_avg_bytes": round(
          sum(int(a.get("s2_bytes") or 0) for a in ablations) / count
      ),
      "s2_avg_tokens": round(
          sum(int(a.get("s2_tokens_est") or 0) for a in ablations) / count
      ),
      "s2_over_2048_count": sum(
          1 for a in ablations if bool(a.get("s2_exceeds_2048"))
      ),
      "s3_avg_bytes": round(
          sum(int(a.get("s3_bytes") or 0) for a in ablations) / count
      ),
      "s3_avg_tokens": round(
          sum(int(a.get("s3_tokens_est") or 0) for a in ablations) / count
      ),
      "s3_over_2048_count": sum(
          1 for a in ablations if bool(a.get("s3_exceeds_2048"))
      ),
      "ingest_llm_cost_usd": 0.0,
  }
