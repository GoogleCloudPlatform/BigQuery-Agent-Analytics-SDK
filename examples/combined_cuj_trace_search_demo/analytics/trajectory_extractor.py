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
import os
from pathlib import Path
import re
import tempfile
from typing import Any

DEFAULT_S3_MAX_CHARS = 4800
_EMBED_TOKEN_LIMIT = 2048

_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEP_RE = re.compile(r"[.\-\s]+")
_SAFE_DIAGNOSTIC_SCALAR_KEYS = frozenset(
    {
        "auth_error",
        "auth_status",
        "auth_reason",
        "auth_mode",
        "auth_method",
        "auth_scheme",
        "auth_provider",
        "auth_type",
        "credential_type",
        "credential_status",
        "credential_error",
        "credential_provider",
        "author",
        "authority",
        "oauth_state",
        "error_code",
        "status_code",
        "tail_code",
    }
)
_SAFE_SCALAR_KEY_SUFFIXES: tuple[str, ...] = (
    "_file",
    "_path",
    "_filename",
    "_filepath",
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:passwords?(?:$|[_\d])|passwd(?:$|[_\d])|(?:^|_)pwd(?:$|[_\d])|"
    r"passphrase(?:$|[_\d])|client_?secrets?(?:$|[_\d])|"
    r"secret_?keys?(?:$|[_\d])|secret_?access_?keys?(?:$|[_\d])|"
    r"aws_?secret_?access_?keys?(?:$|[_\d])|(?:^|_)secrets?(?:$|[_\d])|"
    r"private_?keys?(?:$|[_\d])|api_?keys?(?:$|[_\d])|apikeys?(?:$|[_\d])|"
    r"signing_?keys?(?:$|[_\d])|encryption_?keys?(?:$|[_\d])|"
    r"master_?keys?(?:$|[_\d])|connection_?string(?:$|[_\d])|"
    r"database_?url(?:$|[_\d])|(?:^|_)dsn(?:$|[_\d])|"
    r"(?:^|_)(?:set_?)?cookie(?:$|_)|(?:^|_)jwt(?:$|[_\d])|"
    r"(?:^|_)oauth(?:$|[_\d])|"
    r"(?:auth|access|refresh|id|bearer|session|oauth|csrf|secret|api|github)"
    r"_?tokens?(?:$|[_\d])|^token$|authorization(?:$|[_\d])|"
    r"auth_?headers?(?:$|[_\d])|auth_?keys?(?:$|[_\d])|"
    r"auth_?secrets?(?:$|[_\d])|auth_?codes?(?:$|[_\d])|"
    r"auth_?response_?uri(?:$|[_\d])|auth_?credentials?(?:$|[_\d])|"
    r"(?:^|_)authentications?(?:$|[_\d])|(?:^|_)auth(?:$|[_\d])|"
    r"(?:^|_)credentials?(?:$|[_\d]))",
    re.IGNORECASE,
)

# Step 1: Secret redaction runs first (key-aware + string patterns) so split
# tokens never leak into _ai_index_trajectory or downstream HTML reports.
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)\S+",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}=*\b",
            re.IGNORECASE,
        ),
        r"\1 [REDACTED]",
    ),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
            r"sk-[A-Za-z0-9_-]{16,})\b"
        ),
        "[REDACTED]",
    ),
)

_KV_CANDIDATE_RE = re.compile(
    r"""(?:(?P<dq>"[A-Za-z_][A-Za-z0-9_. -]{0,60}")|"""
    r"""(?P<sq>'[A-Za-z_][A-Za-z0-9_. -]{0,60}')|"""
    r"""(?<![A-Za-z0-9_-])(?:--?)?(?P<bare>[A-Za-z_][A-Za-z0-9_-]*))"""
    r"""(?P<conn>[ \t]*[:=]|[ \t]+(?:is|are)(?:[ \t]*:|(?=\s)))""",
    re.IGNORECASE,
)
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_WORD_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_MULTIWORD_KEY_TAIL_WORDS = frozenset(
    {
        "key",
        "keys",
        "string",
        "url",
        "header",
        "headers",
        "code",
        "codes",
        "uri",
        "data",
        "info",
        "value",
        "payload",
        "result",
    }
)
_SAFE_TOKEN_PREFIX_WORDS = frozenset(
    {
        "prompt",
        "input",
        "output",
        "completion",
        "total",
        "max",
        "cached",
        "thinking",
        "billable",
    }
)
_PY_STR_PREFIX_RE = re.compile(r"""[rRuUbBfF]{1,2}(?=["'])""")
_CTOR_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?=\()")
_BARE_VALUE_RE = re.compile(r"""[^\s"',;)}\]]+""")


def _normalize_key_name(key_name: str) -> str:
  raw = _KEY_SEP_RE.sub("_", str(key_name).strip())
  return _CAMEL_BOUNDARY_RE.sub(r"\1_\2", raw).lower()


def is_sensitive_key(key_name: str, *, is_container: bool = False) -> bool:
  """Returns True when a dict/field name represents a credential or secret."""
  if not key_name:
    return False
  norm = _normalize_key_name(key_name)
  if not norm:
    return False
  if not is_container and (
      norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
      or norm.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
  ):
    return False
  return bool(_SENSITIVE_KEY_RE.search(norm))


_MAX_EMBEDDED_JSON_DEPTH = 32
_MAX_EMBEDDED_JSON_SPAN = 16384
_MAX_EMBEDDED_JSON_DECODES = 32
_MAX_EMBEDDED_JSON_FAIL_BUDGET = 32768


def _find_balanced_container_end(
    text: str,
    start: int,
    *,
    allow_single_quotes: bool = False,
    allow_parens: bool = False,
    max_len: int = _MAX_EMBEDDED_JSON_SPAN,
    max_depth: int = _MAX_EMBEDDED_JSON_DEPTH,
) -> tuple[int | None, int, bool]:
  """Finds balanced closing index for a container starting at ``start``.

  Returns ``(end_idx, scanned_pos, depth_exceeded)``. ``end_idx`` is exclusive
  when balanced within limits, else ``None``.
  """
  limit = min(len(text), start + max_len)
  stack: list[str] = []
  quote_char: str | None = None
  escaped = False
  pos = start
  while pos < limit:
    ch = text[pos]
    if quote_char is not None:
      if escaped:
        escaped = False
      elif ch == "\\":
        escaped = True
      elif ch == quote_char:
        quote_char = None
    else:
      if ch == '"' or (allow_single_quotes and ch == "'"):
        quote_char = ch
      elif ch == "{":
        stack.append("}")
        if len(stack) > max_depth:
          return None, pos + 1, True
      elif ch == "[":
        stack.append("]")
        if len(stack) > max_depth:
          return None, pos + 1, True
      elif allow_parens and ch == "(":
        stack.append(")")
        if len(stack) > max_depth:
          return None, pos + 1, True
      elif ch in ("}", "]") or (allow_parens and ch == ")"):
        if not stack or stack[-1] != ch:
          return None, pos + 1, False
        stack.pop()
        if not stack:
          return pos + 1, pos + 1, False
    pos += 1
  return None, pos, False


def _scrub_embedded_json_containers(text: str) -> str:
  """Scrubs balanced JSON objects/arrays embedded inside free text safely.

  Bounds parsing to depth <= 32, span <= 16 KiB, and <= 32 decoded containers.
  When any parsing budget or structural limit is exceeded on an embedded
  container, redacts the remaining unparsed tail conservatively so secrets
  placed after budget exhaustion never leak.
  """
  if "{" not in text and "[" not in text:
    return text
  decoder = json.JSONDecoder()
  out: list[str] = []
  idx = 0
  n = len(text)
  decoded_count = 0
  fail_scanned = 0
  while idx < n:
    ch = text[idx]
    if ch in "{[":
      if (
          decoded_count >= _MAX_EMBEDDED_JSON_DECODES
          or fail_scanned >= _MAX_EMBEDDED_JSON_FAIL_BUDGET
      ):
        out.append("[REDACTED]")
        break
      end_idx, scanned_pos, depth_exceeded = _find_balanced_container_end(
          text, idx, allow_single_quotes=False
      )
      if depth_exceeded or (
          end_idx is None and (scanned_pos - idx) >= _MAX_EMBEDDED_JSON_SPAN
      ):
        out.append("[REDACTED]")
        break
      if end_idx is None:
        fail_scanned += scanned_pos - idx
        out.append(ch)
        idx += 1
        continue
      snippet = text[idx:end_idx]
      try:
        parsed, local_end = decoder.raw_decode(snippet, 0)
      except (json.JSONDecodeError, RecursionError, ValueError):
        fail_scanned += len(snippet)
        out.append(ch)
        idx += 1
        continue
      decoded_count += 1
      abs_end = idx + local_end
      if isinstance(parsed, (Mapping, list)):
        scrubbed = scrub_sensitive_data(parsed)
        if scrubbed != parsed:
          out.append(
              json.dumps(scrubbed, separators=(",", ":"), ensure_ascii=False)
          )
          idx = abs_end
          continue
      out.append(text[idx:abs_end])
      idx = abs_end
      continue
    out.append(ch)
    idx += 1
  return "".join(out)


def _scan_quoted_value(text: str, quote_pos: int) -> int | None:
  """Scans a quoted string literal from ``quote_pos`` within span limit."""
  n = len(text)
  if quote_pos >= n:
    return None
  quote_char = text[quote_pos]
  limit = min(n, quote_pos + _MAX_EMBEDDED_JSON_SPAN)
  escaped = False
  cursor = quote_pos + 1
  while cursor < limit:
    c = text[cursor]
    if escaped:
      escaped = False
    elif c == "\\":
      escaped = True
    elif c == quote_char:
      return cursor + 1
    cursor += 1
  return None


def _consume_sensitive_kv_value(text: str, pos: int) -> tuple[int, str] | None:
  """Consumes a quoted, escaped, container, repr, or bare value at ``pos``."""
  n = len(text)
  if pos >= n:
    return None
  ch = text[pos]
  if ch in ('"', "'"):
    end_idx = _scan_quoted_value(text, pos)
    if end_idx is not None:
      return end_idx, f"{ch}[REDACTED]{ch}"
    # Unterminated or oversized quoted value: fail closed to end of string so
    # multiline/PEM tails never leak after the first newline.
    return n, "[REDACTED]"
  if text.startswith('\\"', pos) or text.startswith("\\'", pos):
    q = text[pos + 1]
    limit = min(n, pos + _MAX_EMBEDDED_JSON_SPAN)
    cursor = pos + 2
    while cursor < limit:
      if text[cursor] == "\\":
        bs = 0
        while cursor + bs < limit and text[cursor + bs] == "\\":
          bs += 1
        if cursor + bs < limit and text[cursor + bs] == q:
          if bs == 1:
            return cursor + 2, f"\\{q}[REDACTED]\\{q}"
          cursor += bs + 1
          continue
        cursor += max(1, bs)
        continue
      cursor += 1
    return n, "[REDACTED]"
  if ch in ("{", "[", "("):
    end_idx, _, depth_exceeded = _find_balanced_container_end(
        text, pos, allow_single_quotes=True, allow_parens=True
    )
    if end_idx is not None and not depth_exceeded:
      return end_idx, "[REDACTED]"
    return n, "[REDACTED]"
  prefix_match = _PY_STR_PREFIX_RE.match(text, pos)
  if prefix_match is not None:
    end_idx = _scan_quoted_value(text, prefix_match.end())
    if end_idx is not None:
      return end_idx, "[REDACTED]"
    return n, "[REDACTED]"
  ctor_match = _CTOR_PREFIX_RE.match(text, pos)
  if ctor_match is not None:
    end_idx, _, depth_exceeded = _find_balanced_container_end(
        text, ctor_match.end(), allow_single_quotes=True, allow_parens=True
    )
    if end_idx is not None and not depth_exceeded:
      return end_idx, "[REDACTED]"
    return n, "[REDACTED]"
  bare_match = _BARE_VALUE_RE.match(text, pos)
  if bare_match:
    return bare_match.end(), "[REDACTED]"
  return None


def _preceding_same_line_word(
    text: str, end_pos: int
) -> tuple[str, int] | None:
  """Returns the immediately preceding same-line word and its start index."""
  cursor = end_pos
  if cursor <= 0 or text[cursor - 1] not in (" ", "\t"):
    return None
  while cursor > 0 and text[cursor - 1] in (" ", "\t"):
    cursor -= 1
  word_end = cursor
  if word_end <= 0 or text[word_end - 1] not in _WORD_CHARS:
    return None
  word_start = word_end
  while (
      word_start > 0
      and text[word_start - 1] in _WORD_CHARS
      and (word_end - word_start) <= 64
  ):
    word_start -= 1
  if (
      (word_end - word_start) > 64
      or (word_start > 0 and text[word_start - 1] in _WORD_CHARS)
      or text[word_start] not in _WORD_START_CHARS
  ):
    return None
  return text[word_start:word_end], word_start


def _is_matched_kv_sensitive(
    text: str, match: re.Match[str], *, is_container: bool
) -> bool:
  """Classifies a candidate KV regex match dynamically via is_sensitive_key."""
  dq = match.group("dq")
  sq = match.group("sq")
  if dq is not None:
    return is_sensitive_key(dq[1:-1], is_container=is_container)
  if sq is not None:
    return is_sensitive_key(sq[1:-1], is_container=is_container)

  bare = match.group("bare") or ""
  lower_bare = bare.lower()
  if lower_bare == "token" and not is_container:
    prev = _preceding_same_line_word(text, match.start())
    if prev is not None and prev[0].lower() in _SAFE_TOKEN_PREFIX_WORDS:
      return False
  if is_sensitive_key(bare, is_container=is_container):
    return True
  if lower_bare not in _MULTIWORD_KEY_TAIL_WORDS:
    return False

  # Allow 2-to-4 word natural-language phrases (e.g. "api key",
  # "connection string", "aws secret access key") on the same line.
  phrase = bare
  cursor = match.start()
  for _ in range(3):
    prev = _preceding_same_line_word(text, cursor)
    if prev is None:
      break
    prev_word, cursor = prev
    phrase = f"{prev_word} {phrase}"
    norm = _normalize_key_name(phrase)
    if not is_container and (
        norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
        or norm.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
    ):
      return False
    if is_sensitive_key(phrase, is_container=is_container):
      return True
  return False


def _scrub_sensitive_kv_in_text(text: str) -> str:
  """Redacts sensitive key=value / key: value / key is value pairs in text."""
  out: list[str] = []
  idx = 0
  n = len(text)
  while idx < n:
    match = _KV_CANDIDATE_RE.search(text, idx)
    if match is None:
      out.append(text[idx:])
      break
    pos = match.end()
    same_line_pos = pos
    while same_line_pos < n and text[same_line_pos] in (" ", "\t"):
      same_line_pos += 1
    any_ws_pos = same_line_pos
    while any_ws_pos < n and text[any_ws_pos].isspace():
      any_ws_pos += 1
    if any_ws_pos < n and (
        text[any_ws_pos] in ("{", "[", "(", '"', "'")
        or text.startswith('\\"', any_ws_pos)
        or text.startswith("\\'", any_ws_pos)
    ):
      val_start = any_ws_pos
    else:
      val_start = same_line_pos
    next_char = text[val_start] if val_start < n else ""
    is_container = next_char in ("{", "[", "(")
    if not _is_matched_kv_sensitive(text, match, is_container=is_container):
      out.append(text[idx : match.end()])
      idx = match.end()
      continue
    consumed = _consume_sensitive_kv_value(text, val_start)
    if consumed is None:
      out.append(text[idx : match.end()])
      idx = match.end()
      continue
    val_end, replacement = consumed
    out.append(text[idx:val_start])
    out.append(replacement)
    idx = val_end
  return "".join(out)


def scrub_sensitive_text(text: str) -> str:
  """Redacts credential tokens from raw strings while keeping error codes."""
  out = _scrub_embedded_json_containers(str(text))
  for pat, repl in _SECRET_RULES:
    out = pat.sub(repl, out)
  return _scrub_sensitive_kv_in_text(out)


def scrub_sensitive_data(value: Any, *, key_name: str = "") -> Any:
  """Key-aware recursive scrubber for dicts, lists, bytes, and strings."""
  is_container = (
      isinstance(value, Mapping)
      or (
          isinstance(value, Sequence)
          and not isinstance(value, (str, bytes, bytearray))
      )
      or isinstance(value, (set, frozenset))
  )
  if (
      is_sensitive_key(key_name, is_container=is_container)
      and value is not None
      and not isinstance(value, bool)
  ):
    if isinstance(value, (bytes, bytearray)):
      text_val = value.decode("utf-8", errors="replace")
      return "[REDACTED]" if text_val.strip() else ""
    if isinstance(value, str):
      return "[REDACTED]" if value.strip() else value
    if isinstance(value, Mapping):
      return "[REDACTED]" if value else {}
    if isinstance(value, (Sequence, set, frozenset)):
      return "[REDACTED]" if value else []
    return "[REDACTED]"
  if isinstance(value, Mapping):
    return {
        str(k): scrub_sensitive_data(v, key_name=str(k))
        for k, v in value.items()
    }
  if isinstance(value, Sequence) and not isinstance(
      value, (str, bytes, bytearray)
  ):
    return [scrub_sensitive_data(item, key_name=key_name) for item in value]
  if isinstance(value, (bytes, bytearray)):
    text_val = value.decode("utf-8", errors="replace")
    return scrub_sensitive_text(text_val)
  if isinstance(value, str):
    return scrub_sensitive_text(value)
  return value


def ensure_owner_only_dir(dir_path: Path) -> None:
  """Creates missing directory hierarchy with 0700 without chmodding callers."""
  dir_path.mkdir(mode=0o700, parents=True, exist_ok=True)


def write_owner_only_text(output_path: Path, content: str) -> None:
  """Writes text atomically via a pre-0600 temp file without altering caller dir."""
  ensure_owner_only_dir(output_path.parent)
  fd, tmp_name = tempfile.mkstemp(
      prefix=f".{output_path.name}.",
      suffix=".tmp",
      dir=str(output_path.parent),
  )
  try:
    os.chmod(tmp_name, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(content)
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, output_path)
    os.chmod(output_path, 0o600)
  except Exception:
    if os.path.exists(tmp_name):
      os.unlink(tmp_name)
    raise


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
  return scrub_sensitive_text(text)


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


def _flatten_structured_value(
    val: Any, *, depth: int = 0, key_name: str = ""
) -> str:
  """Flattens dicts/lists into compact k=v text with key-aware redaction."""
  val = scrub_sensitive_data(val, key_name=key_name)
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
        f"{k}={_flatten_structured_value(v, depth=depth + 1, key_name=k)}"
        for k, v in ordered
    ]
    return " ".join(parts)
  if isinstance(val, Sequence) and not isinstance(val, (str, bytes, bytearray)):
    items = list(val)
    if len(items) > 4:
      head = [
          _flatten_structured_value(x, depth=depth + 1, key_name=key_name)
          for x in items[:2]
      ]
      tail = _flatten_structured_value(
          items[-1], depth=depth + 1, key_name=key_name
      )
      return " ".join(head + [f"...[{len(items) - 3}_more]...", tail])
    return " ".join(
        _flatten_structured_value(x, depth=depth + 1, key_name=key_name)
        for x in items
    )
  if isinstance(val, (bytes, bytearray)):
    val = val.decode("utf-8", errors="replace")
  if is_sensitive_key(key_name) and str(val).strip():
    return "[REDACTED]"
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
            safe_args = scrub_sensitive_data(fc.get("args") or {})
            raw_args = json.dumps(safe_args, sort_keys=True)
            markers = _record_markers(scrub_sensitive_text(raw_args))
            flat_args = _flatten_structured_value(safe_args)
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
            resp_obj = scrub_sensitive_data(fr.get("response") or {})
            raw_resp = (
                json.dumps(resp_obj, sort_keys=True)
                if isinstance(resp_obj, (Mapping, list))
                else str(resp_obj)
            )
            markers = _record_markers(scrub_sensitive_text(raw_resp))
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
