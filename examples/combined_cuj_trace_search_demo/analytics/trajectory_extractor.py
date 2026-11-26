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
import functools
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
_KEY_SEP_RE = re.compile(r"[^A-Za-z0-9_]+")
_KEY_SEGMENT_SEP_RE = re.compile(r"[./:\s]+")
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
_SAFE_SMALL_COUNT_KEYS = frozenset(
    {
        "cookies",
        "cookie_count",
        "credential_count",
        "credentials_count",
        "auth_count",
        "auth_retry_count",
        "auth_failure_count",
        "token_count",
    }
)
_MAX_SAFE_COUNT_VALUE = 999
_SAFE_AUTH_STATUS_VALUES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "denied",
        "rejected",
        "expired",
        "invalid",
        "missing",
        "required",
        "timeout",
        "timed_out",
        "unauthorized",
        "unauthenticated",
        "forbidden",
        "ok",
        "success",
        "succeeded",
        "passed",
        "valid",
        "verified",
        "enabled",
        "disabled",
        "skipped",
        "none",
        "null",
        "true",
        "false",
        "pending",
        "unknown",
    }
)
_PROSE_PREDICATE_WORDS = _SAFE_AUTH_STATUS_VALUES | frozenset(
    {
        "bad",
        "wrong",
        "incorrect",
        "empty",
        "blank",
        "weak",
        "not",
        "too",
        "a",
        "an",
        "the",
    }
)
_SAFE_SCALAR_KEY_SUFFIXES: tuple[str, ...] = (
    "_file",
    "_path",
    "_filename",
    "_filepath",
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:pass_?words?(?:$|[_\d])|passwd(?:$|[_\d])|pwd(?:$|[_\d])|"
    r"passphrase(?:$|[_\d])|client_?secrets?(?:$|[_\d])|"
    r"secret_?keys?(?:$|[_\d])|secret_?access_?keys?(?:$|[_\d])|"
    r"aws_?secret_?access_?keys?(?:$|[_\d])|secrets?(?:$|[_\d])|"
    r"private_?keys?(?:$|[_\d])|api_?keys?(?:$|[_\d])|apikeys?(?:$|[_\d])|"
    r"signing_?keys?(?:$|[_\d])|encryption_?keys?(?:$|[_\d])|"
    r"master_?keys?(?:$|[_\d])|connection_?string(?:$|[_\d])|"
    r"database_?url(?:$|[_\d])|(?:^|_)dsn(?:$|[_\d])|"
    r"(?:set_?)?cookies?(?:$|[_\d])|(?:^|_)jwt(?:$|[_\d])|"
    r"(?:^|_)oauth(?:$|[_\d])|"
    r"(?:auth|access|refresh|id|bearer|session|oauth|csrf|secret|api|github|"
    r"request)_?tokens?(?:$|[_\d])|^token(?:_\d+|\d+)?$|"
    r"authorization(?:$|[_\d])|"
    r"auth_?headers?(?:$|[_\d])|auth_?keys?(?:$|[_\d])|"
    r"auth_?secrets?(?:$|[_\d])|auth_?codes?(?:$|[_\d])|"
    r"auth_?response_?uri(?:$|[_\d])|auth_?credentials?(?:$|[_\d])|"
    r"(?:^|_)authentications?(?:$|[_\d])|(?:^|_)auth(?:$|[_\d])|"
    r"credentials?(?:$|[_\d]))",
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

_QUOTED_STRUCTURAL_CONN_RE = re.compile(r"(?:[ \t\r\n]|\\[trn])*[:=]")
_QUOTED_CONN_RE = re.compile(
    r"(?:[ \t\r\n]|\\[trn])*[:=]|"
    r"(?:[ \t]|\\[trn])+(?:is|are)(?:(?:[ \t]|\\[trn])*:|(?=\s|\\[trn]))",
    re.IGNORECASE,
)
_JSON_SCALAR_START_RE = re.compile(
    r"(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)"
    r"(?=$|[\s,;)}\]\"']|\\[trn])"
)
_BARE_KV_RE = re.compile(
    r"(?:(?<=\\[trn])|(?<![A-Za-z0-9_./-]))[./-]*(?P<bare>[A-Za-z0-9_]"
    r"[A-Za-z0-9_./-]*)"
    r"(?P<conn>(?:[ \t]|\\[tr])*[:=]|"
    r"(?:[ \t]|\\[trn])+(?:is|are)(?:(?:[ \t]|\\[tr])*:|(?=\s|\\[trn])))",
    re.IGNORECASE,
)
_UNANCHORED_BARE_KV_RE = re.compile(
    r"[./-]*(?P<bare>[A-Za-z0-9_][A-Za-z0-9_./-]*)"
    r"(?P<conn>(?:[ \t]|\\[tr])*[:=]|"
    r"(?:[ \t]|\\[trn])+(?:is|are)(?:(?:[ \t]|\\[tr])*:|(?=\s|\\[trn])))",
    re.IGNORECASE,
)
_KV_SCAN_NEXT_RE = re.compile(
    r"""\\["'trn]|["']|"""
    r"(?:(?<=\\[trn])|(?<![A-Za-z0-9_./-]))[./-]*(?P<bare>[A-Za-z0-9_]"
    r"[A-Za-z0-9_./-]*)"
    r"(?P<conn>(?:[ \t]|\\[tr])*[:=]|"
    r"(?:[ \t]|\\[trn])+(?:is|are)(?:(?:[ \t]|\\[tr])*:|(?=\s|\\[trn])))",
    re.IGNORECASE,
)
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_PATH_SEG_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
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
_QUERY_PARAM_AFTER_AMP_RE = re.compile(r"&(?P<param>[A-Za-z_][A-Za-z0-9_.-]*)=")
_URL_DELIM_KV_RE = re.compile(r"[&,;][A-Za-z_][A-Za-z0-9_.-]*=")
_URL_STOP_CHARS = frozenset("/?#&|<>*\"',;}])")
_URL_AUTH_HARD_STOP_CHARS = frozenset('/?#"<>|{}[]')
_COLON_CHAIN_CONTAINER_KEYS = frozenset(
    {
        "auth",
        "authentication",
        "authentications",
        "oauth",
        "oauth2",
        "credentials",
        "credential",
        "secrets",
        "secret",
        "authorization",
    }
)
_CANONICAL_SENSITIVE_KEY_EXACT = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "secrets",
        "client_secret",
        "client_secrets",
        "secret_key",
        "secret_keys",
        "secret_access_key",
        "secret_access_keys",
        "aws_secret_access_key",
        "aws_secret_access_keys",
        "private_key",
        "private_keys",
        "api_key",
        "api_keys",
        "apikey",
        "apikeys",
        "signing_key",
        "signing_keys",
        "encryption_key",
        "encryption_keys",
        "master_key",
        "master_keys",
        "connection_string",
        "database_url",
        "dsn",
        "cookie",
        "cookies",
        "set_cookie",
        "set_cookies",
        "jwt",
        "oauth",
        "token",
        "tokens",
        "auth_token",
        "auth_tokens",
        "access_token",
        "access_tokens",
        "refresh_token",
        "refresh_tokens",
        "id_token",
        "id_tokens",
        "bearer_token",
        "bearer_tokens",
        "session_token",
        "session_tokens",
        "oauth_token",
        "oauth_tokens",
        "csrf_token",
        "csrf_tokens",
        "secret_token",
        "secret_tokens",
        "api_token",
        "api_tokens",
        "github_token",
        "github_tokens",
        "request_token",
        "request_tokens",
        "authorization",
        "auth_header",
        "auth_headers",
        "auth_key",
        "auth_keys",
        "auth_secret",
        "auth_secrets",
        "auth_code",
        "auth_codes",
        "auth_response_uri",
        "auth_credentials",
        "authentication",
        "authentications",
        "auth",
        "credential",
        "credentials",
    }
)
_COMPOUND_SENSITIVE_KEY_TAILS = frozenset(
    {
        "key",
        "keys",
        "access_key",
        "access_keys",
        "code",
        "codes",
        "header",
        "headers",
        "response_uri",
        "credentials",
        "credential",
        "string",
        "url",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwords",
    }
)
_CANONICAL_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    "_password",
    "_passwords",
    "_passwd",
    "_pwd",
    "_passphrase",
    "_passphrases",
    "_secret",
    "_secrets",
    "_token",
    "_tokens",
    "_api_key",
    "_api_keys",
    "_apikey",
    "_apikeys",
    "_private_key",
    "_private_keys",
    "_access_key",
    "_access_keys",
    "_secret_key",
    "_secret_keys",
    "_signing_key",
    "_signing_keys",
    "_encryption_key",
    "_encryption_keys",
    "_master_key",
    "_master_keys",
    "_database_url",
    "_connection_string",
    "_cookie",
    "_cookies",
    "_dsn",
    "_jwt",
    "_authorization",
    "_auth_header",
    "_auth_headers",
    "_auth_code",
    "_auth_codes",
    "_auth_key",
    "_auth_keys",
    "_auth_secret",
    "_auth_secrets",
    "_auth_response_uri",
    "_auth_credentials",
    "_credentials",
    "_credential",
)
_BARE_VAL_STOP_CHARS = frozenset("\"',;)}]")
_COOKIE_NAME_STOP_CHARS = frozenset(";)}]=\"',:")
_COOKIE_VAL_STOP_CHARS = frozenset(';)}]"' + "'")
_COOKIE_KV_CONN_RE = re.compile(
    r"[ \t]*[:=]|[ \t]+(?:is|are)(?:[ \t]*:|(?=\s|\\[trn]))",
    re.IGNORECASE,
)
_HTTP_COOKIE_DATE_RE = re.compile(
    r"[A-Za-z]{3},[ \t]+\d{1,2}[ \t-]+[A-Za-z]{3}[ \t-]+\d{2,4}"
    r"[ \t]+\d{2}:\d{2}:\d{2}[ \t]+GMT"
)


def _normalize_key_name_impl(key_name: str) -> str:
  raw = _KEY_SEP_RE.sub("_", key_name.strip()).strip("_")
  return _CAMEL_BOUNDARY_RE.sub(r"\1_\2", raw).lower()


@functools.lru_cache(maxsize=2048)
def _normalize_key_name_cached(key_name: str) -> str:
  return _normalize_key_name_impl(key_name)


def _normalize_key_name(key_name: str) -> str:
  if isinstance(key_name, str):
    if len(key_name) <= 128:
      return _normalize_key_name_cached(key_name)
    return _normalize_key_name_impl(key_name)
  key_str = str(key_name)
  if len(key_str) <= 128:
    return _normalize_key_name_cached(key_str)
  return _normalize_key_name_impl(key_str)


def _is_numeric_count_scalar(key_name: str, value: Any) -> bool:
  """Returns True for small non-negative integer counts on allowlisted keys."""
  if not key_name:
    return False
  norm = _normalize_key_name(key_name)
  if norm not in _SAFE_SMALL_COUNT_KEYS:
    return False
  if isinstance(value, bool):
    return False
  if isinstance(value, int):
    return 0 <= value <= _MAX_SAFE_COUNT_VALUE
  if isinstance(value, (bytes, bytearray)):
    text_val = value.decode("utf-8", errors="replace").strip()
  elif isinstance(value, str):
    text_val = value.strip()
  else:
    return False
  return (
      bool(text_val)
      and len(text_val) <= 3
      and text_val.isascii()
      and text_val.isdigit()
      and not (len(text_val) > 1 and text_val.startswith("0"))
      and int(text_val) <= _MAX_SAFE_COUNT_VALUE
  )


def _is_safe_auth_status_scalar(key_name: str, value: Any) -> bool:
  """Returns True for safe status scalars on authentication keys."""
  if not key_name:
    return False
  if isinstance(value, (bytes, bytearray)):
    text_val = value.decode("utf-8", errors="replace")
  elif isinstance(value, str):
    text_val = value
  else:
    return False
  norm = _normalize_key_name(key_name)
  if norm not in ("authentication", "authentication_result") and not (
      norm.endswith(("_authentication", "_authentication_result"))
  ):
    return False
  return text_val.strip().lower() in _SAFE_AUTH_STATUS_VALUES


def _is_sensitive_key_impl(key_name: str, is_container: bool) -> bool:
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
  segments = [seg for seg in _KEY_SEGMENT_SEP_RE.split(key_name.strip()) if seg]
  if len(segments) >= 2:
    norm_tail = _normalize_key_name(segments[-1])
    if norm_tail:
      if not is_container and (
          norm_tail in _SAFE_DIAGNOSTIC_SCALAR_KEYS
          or norm_tail.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
      ):
        return False
      if _SENSITIVE_KEY_RE.search(norm):
        return True
      if (
          not is_container
          and norm_tail == "token"
          and _normalize_key_name(segments[-2]) in _SAFE_TOKEN_PREFIX_WORDS
      ):
        return False
      if _SENSITIVE_KEY_RE.search(norm_tail):
        return True
  return bool(_SENSITIVE_KEY_RE.search(norm))


@functools.lru_cache(maxsize=2048)
def _is_sensitive_key_cached(key_name: str, is_container: bool) -> bool:
  return _is_sensitive_key_impl(key_name, is_container)


def is_sensitive_key(key_name: str, *, is_container: bool = False) -> bool:
  """Returns True when a dict/field name represents a credential or secret."""
  if not key_name:
    return False
  if isinstance(key_name, str) and len(key_name) <= 128:
    return _is_sensitive_key_cached(key_name, is_container)
  return _is_sensitive_key_impl(str(key_name), is_container)


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
  Non-object/non-string arrays (such as ``[1]`` or ``[INFO]``) and trivial
  ``{}`` are skipped without consuming budgets. When any budget or structural
  limit is reached, JSON decoding stops cleanly and defers the remaining text to
  linear-time secret rules and KV scrubbing so large ordinary JSON payloads
  and trailing error codes stay intact while secrets are still redacted.
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
        out.append(text[idx:])
        break
      end_idx, scanned_pos, depth_exceeded = _find_balanced_container_end(
          text, idx, allow_single_quotes=False
      )
      if depth_exceeded or end_idx is None:
        fail_scanned += scanned_pos - idx
        if fail_scanned >= _MAX_EMBEDDED_JSON_FAIL_BUDGET:
          out.append(text[idx:])
          break
        out.append(ch)
        idx += 1
        continue
      snippet = text[idx:end_idx]
      if snippet == "{}" or (
          ch == "[" and "{" not in snippet and '"' not in snippet
      ):
        out.append(snippet)
        idx = end_idx
        continue
      try:
        parsed, local_end = decoder.raw_decode(snippet, 0)
      except (json.JSONDecodeError, RecursionError, ValueError):
        fail_scanned += len(snippet)
        if fail_scanned >= _MAX_EMBEDDED_JSON_FAIL_BUDGET:
          out.append(text[idx:])
          break
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


def _scan_bare_value_end(text: str, pos: int) -> int:
  """Scans a bare KV value stopping before whitespace, delimiters, or JSON \\t/\\r/\\n."""
  n = len(text)
  is_url_val = text.find("://", pos, min(n, pos + 16)) != -1
  p = pos
  while p < n and not text[p].isspace() and text[p] not in _BARE_VAL_STOP_CHARS:
    if not is_url_val and text[p] == "&" and p > pos:
      amp_m = _QUERY_PARAM_AFTER_AMP_RE.match(text, p)
      if amp_m is not None:
        param = amp_m.group("param")
        if _normalize_key_name(
            param
        ) in _SAFE_DIAGNOSTIC_SCALAR_KEYS or is_sensitive_key(
            param, is_container=False
        ):
          break
    if text[p] == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if p + bs < n and text[p + bs] in ("t", "r", "n") and bs % 2 == 1:
        p += bs - 1
        break
      p += bs
    else:
      p += 1
  return p


def _starts_with_odd_escaped_quote(text: str, pos: int) -> bool:
  """Returns True when ``pos`` starts with an odd backslash run before a quote."""
  n = len(text)
  bs = 0
  while pos + bs < n and text[pos + bs] == "\\":
    bs += 1
  return (
      bs > 0 and bs % 2 == 1 and pos + bs < n and text[pos + bs] in ('"', "'")
  )


def _escaped_quote_mod_and_period(lead_bs: int) -> tuple[int, int, int]:
  """Returns (close_mod, period, low_zero) for an odd backslash run.

  Lowest 0-bit of lead_bs identifies the JSON escaping depth d:
  - d=1 (lead_bs=1, 5, 9, ...):  low_zero=2, close_mod=1, period=4  (skips 3, 7)
  - d=2 (lead_bs=3, 11, 19, ...): low_zero=4, close_mod=3, period=8  (skips 7, 15)
  - d=3 (lead_bs=7, 23, ...):    low_zero=8, close_mod=7, period=16 (skips 15, 31)
  Any bs with ((bs + 1) & -(bs + 1)) < low_zero is a strictly shallower
  unescaped outer closing quote (including all even bs where it equals 1).
  """
  low_zero = (lead_bs + 1) & -(lead_bs + 1)
  return low_zero - 1, low_zero * 2, low_zero


def _consume_sensitive_kv_value(text: str, pos: int) -> tuple[int, str] | None:
  """Consumes a quoted, escaped, container, repr, or bare value at ``pos``."""
  n = len(text)
  if pos >= n:
    return None
  ch = text[pos]
  if ch in ('"', "'"):
    end_idx = _scan_quoted_value(text, pos)
    if end_idx is not None:
      close_pos = end_idx - 1
      if _quote_opens_kv_at(text, close_pos, escaped_dq=False):
        inner = text[pos + 1 : close_pos]
        if not any(c.isalnum() for c in inner):
          return None
        return close_pos, "[REDACTED]"
      return end_idx, f"{ch}[REDACTED]{ch}"
    # Unterminated or oversized quoted value: fail closed to end of string so
    # multiline/PEM tails never leak after the first newline.
    return n, "[REDACTED]"
  lead_bs = 0
  while pos + lead_bs < n and text[pos + lead_bs] == "\\":
    lead_bs += 1
  if (
      lead_bs > 0
      and lead_bs % 2 == 1
      and pos + lead_bs < n
      and text[pos + lead_bs] in ('"', "'")
  ):
    q = text[pos + lead_bs]
    close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
    delim = "\\" * lead_bs + q
    limit = min(n, pos + _MAX_EMBEDDED_JSON_SPAN)
    cursor = pos + lead_bs + 1
    while cursor < limit:
      if text[cursor] == '"' and q == '"':
        return cursor, "[REDACTED]"
      if text[cursor] == "\\":
        bs = 0
        while cursor + bs < limit and text[cursor + bs] == "\\":
          bs += 1
        if cursor + bs < limit and text[cursor + bs] == q:
          if bs % period == close_mod:
            if q == '"' and _quote_opens_kv_at(
                text, cursor + bs - 1, escaped_dq=True
            ):
              inner = text[pos + lead_bs + 1 : cursor]
              if not any(c.isalnum() for c in inner):
                return None
              return cursor, "[REDACTED]"
            return cursor + bs + 1, f"{delim}[REDACTED]{delim}"
          if ((bs + 1) & -(bs + 1)) < low_zero:
            return cursor + bs, "[REDACTED]"
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
  val_end = _scan_bare_value_end(text, pos)
  if val_end > pos:
    return val_end, "[REDACTED]"
  return None


def _preceding_same_line_word(
    text: str, end_pos: int
) -> tuple[str, int] | None:
  """Returns the immediately preceding same-line word or path key and start."""
  cursor = end_pos
  if cursor <= 0 or text[cursor - 1] not in (" ", "\t"):
    return None
  while cursor > 0 and text[cursor - 1] in (" ", "\t"):
    cursor -= 1
  word_end = cursor
  if word_end <= 0 or text[word_end - 1] not in _WORD_CHARS:
    return None
  word_start = word_end
  while word_start > 0 and (word_end - word_start) <= 64:
    ch = text[word_start - 1]
    if ch in _WORD_CHARS or ch == ".":
      word_start -= 1
    elif ch == "/":
      if word_start >= 2 and text[word_start - 2] == ":":
        break
      word_start -= 1
    elif (
        ch == ":"
        and word_start < word_end
        and text[word_start] in _PATH_SEG_START_CHARS
        and word_start >= 2
        and text[word_start - 2] in _WORD_CHARS
    ):
      word_start -= 1
    else:
      break
  if (word_end - word_start) > 64:
    return None
  if word_start > 0 and text[word_start - 1] in _WORD_CHARS:
    return None
  word = text[word_start:word_end]
  if "://" in word:
    return None
  stripped = word.lstrip("./-")
  if not stripped or stripped[0] not in _WORD_START_CHARS:
    return None
  return word, word_start


def _decode_quoted_kv_key(quoted_key: str) -> str:
  """Decodes a double-quoted JSON KV key, handling standard JSON escapes."""
  try:
    decoded = json.loads(quoted_key)
    if isinstance(decoded, str):
      return decoded
  except ValueError:
    pass
  return quoted_key[1:-1].replace("\\/", "/")


def _decode_escaped_quoted_kv_key(raw_inner: str) -> str:
  """Decodes the inner text of an escaped double-quoted KV key."""
  cur = raw_inner
  for _ in range(3):
    if "\\" not in cur:
      break
    try:
      decoded = json.loads(f'"{cur}"')
      if isinstance(decoded, str) and decoded != cur:
        cur = decoded
        continue
    except ValueError:
      pass
    nxt = cur.replace("\\\\", "\\")
    if nxt == cur:
      break
    cur = nxt
  return cur.replace("\\/", "/")


def _extract_bare_kv_key(
    text: str, match: re.Match[str], *, is_container: bool = False
) -> str | None:
  """Returns the matched sensitive bare key/phrase, or ``None`` if benign."""
  bare = match.group("bare") or ""
  if not bare or (
      not bare[0].isalpha() and not any(ch.isalpha() for ch in bare)
  ):
    return None
  lower_bare = bare.lower()
  if lower_bare == "token" and not is_container:
    prev = _preceding_same_line_word(text, match.start())
    if prev is not None:
      prev_segs = [seg for seg in _KEY_SEGMENT_SEP_RE.split(prev[0]) if seg]
      if (
          prev_segs
          and not is_sensitive_key(prev[0], is_container=False)
          and _normalize_key_name(prev_segs[-1]) in _SAFE_TOKEN_PREFIX_WORDS
      ):
        return None
  if is_sensitive_key(bare, is_container=is_container):
    return bare
  if lower_bare not in _MULTIWORD_KEY_TAIL_WORDS:
    return None

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
    if is_sensitive_key(phrase, is_container=is_container):
      return phrase
  return None


def _might_contain_kv(s: str) -> bool:
  """Fast substring pre-check before recursing into quoted text."""
  if ":" in s or "=" in s:
    return True
  low = s.lower()
  return any(
      tok in low
      for tok in (
          " is ",
          " are ",
          " is\t",
          " are\t",
          "\tis",
          "\tare",
          " is\n",
          " are\n",
          "\nis",
          "\nare",
          "\\tis",
          "\\nis",
          "\\ris",
          " is\\",
          "\\tare",
          "\\nare",
          "\\rare",
          " are\\",
      )
  )


def _scan_same_line_quote_end(text: str, start: int) -> int | None:
  """Scans a same-line single- or double-quoted token starting at ``start``."""
  n = len(text)
  quote_char = text[start]
  p = start + 1
  while p < n:
    ch = text[p]
    if ch == "\\" and p + 1 < n and text[p + 1] not in ("\r", "\n"):
      p += 2
    elif ch == quote_char:
      return p + 1
    elif ch in ("\r", "\n"):
      return None
    else:
      p += 1
  return None


def _scan_same_line_escaped_dq_end(
    text: str, start: int, *, lead_bs: int = 1
) -> tuple[int | None, int, int]:
  """Scans a same-line escaped double-quoted token starting at ``start``.

  Returns ``(end_tok, stop_pos, close_start)`` where ``end_tok`` is the
  exclusive end index when closed on the same line, ``close_start`` is the
  index of the first backslash in the closing delimiter run, and ``stop_pos``
  marks where an unclosed scan terminated so unclosed runs are skipped in O(1).
  """
  close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
  n = len(text)
  p = start + 2
  while p < n:
    ch = text[p]
    if ch in ("\r", "\n"):
      return None, p, p
    if ch == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if p + bs < n and text[p + bs] == '"':
        if bs % period == close_mod:
          return p + bs + 1, p + bs + 1, p
        if ((bs + 1) & -(bs + 1)) < low_zero:
          return None, p + bs, p
        p += bs + 1
      else:
        p += bs
    elif ch == '"':
      return None, p, p
    else:
      p += 1
  return None, n, n


def _count_backslashes_ending_at(text: str, pos: int) -> int:
  """Counts consecutive backslashes ending inclusively at ``pos``."""
  bs = 1
  k = pos - 1
  while k >= 0 and text[k] == "\\":
    bs += 1
    k -= 1
  return bs


def _inner_ends_with_kv_connector(raw_inner: str) -> bool:
  """Returns True when ``raw_inner`` ends with a KV connector before a quote."""
  i = len(raw_inner)
  while i > 0:
    if raw_inner[i - 1] in (" ", "\t"):
      i -= 1
    elif (
        i >= 2
        and raw_inner[i - 1] in ("t", "r", "n")
        and raw_inner[i - 2] == "\\"
        and _count_backslashes_ending_at(raw_inner, i - 2) % 2 == 1
    ):
      i -= 2
    else:
      break
  if i == 0:
    return False
  if raw_inner[i - 1] in (":", "="):
    return True
  for word in ("is", "are"):
    wlen = len(word)
    if i >= wlen and raw_inner[i - wlen : i].lower() == word:
      before = i - wlen
      if before == 0 or raw_inner[before - 1].isspace():
        return True
      if (
          before >= 2
          and raw_inner[before - 1] in ("t", "r", "n")
          and raw_inner[before - 2] == "\\"
          and _count_backslashes_ending_at(raw_inner, before - 2) % 2 == 1
      ):
        return True
  return False


def _quote_opens_kv_at(text: str, pos: int, *, escaped_dq: bool) -> bool:
  """Returns True when a quote token at ``pos`` opens a quoted KV key."""
  n = len(text)
  if pos < 0 or pos >= n:
    return False
  limit = min(n, pos + 1024)
  if escaped_dq:
    if not text.startswith('\\"', pos):
      return False
    lead_bs = _count_backslashes_ending_at(text, pos)
    if lead_bs % 2 == 0:
      return False
    close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)

    def _edq_kv_matches(
        inner: str, next_end: int, *, require_sensitive: bool = False
    ) -> bool:
      if (
          not inner
          or inner[0].isspace()
          or inner[0] in ";,)}]"
          or ";" in inner
          or inner.rstrip().endswith("=")
      ):
        return False
      if (
          not require_sensitive
          and _QUOTED_STRUCTURAL_CONN_RE.match(text, next_end) is not None
      ):
        return True
      conn_m = _QUOTED_CONN_RE.match(text, next_end)
      if conn_m is not None:
        decoded_key = _decode_escaped_quoted_kv_key(inner)
        val_start, _ = _skip_kv_whitespace(text, conn_m.end())
        is_container = val_start < n and text[val_start] in ("{", "[", "(")
        return is_sensitive_key(decoded_key, is_container=is_container)
      return False

    p = pos + 2
    while p < limit:
      ch = text[p]
      if ch in ("\r", "\n"):
        return False
      if ch == '"':
        return _edq_kv_matches(text[pos + 2 : p], p + 1)
      if ch == "'" and _edq_kv_matches(
          text[pos + 2 : p], p + 1, require_sensitive=True
      ):
        return True
      if ch == "\\":
        bs = 0
        while p + bs < limit and text[p + bs] == "\\":
          bs += 1
        if p + bs < limit and text[p + bs] == '"':
          if bs % period == close_mod or ((bs + 1) & -(bs + 1)) < low_zero:
            return _edq_kv_matches(text[pos + 2 : p], p + bs + 1)
          p += bs + 1
        else:
          p += bs
      else:
        p += 1
    return False
  quote_char = text[pos]
  if quote_char not in ('"', "'"):
    return False
  p = pos + 1
  while p < limit:
    ch = text[p]
    if ch == "\\" and p + 1 < limit and text[p + 1] not in ("\r", "\n"):
      p += 2
    elif ch == quote_char:
      inner = text[pos + 1 : p]
      if (
          not inner
          or inner[0].isspace()
          or inner[0] in ";,)}]"
          or ";" in inner
          or inner.rstrip().endswith("=")
      ):
        return False
      next_end = p + 1
      if _QUOTED_STRUCTURAL_CONN_RE.match(text, next_end) is not None:
        return True
      conn_m = _QUOTED_CONN_RE.match(text, next_end)
      if conn_m is not None:
        decoded_key = (
            _decode_quoted_kv_key(text[pos:next_end])
            if quote_char == '"'
            else inner
        )
        val_start, _ = _skip_kv_whitespace(text, conn_m.end())
        is_container = val_start < n and text[val_start] in ("{", "[", "(")
        return is_sensitive_key(decoded_key, is_container=is_container)
      return False
    elif ch in ("\r", "\n"):
      return False
    else:
      p += 1
  return False


def _is_json_colon_connector(conn_text: str) -> bool:
  """Returns True when ``conn_text`` is a structural JSON-style colon."""
  return _QUOTED_STRUCTURAL_CONN_RE.fullmatch(
      conn_text
  ) is not None and conn_text.endswith(":")


def _is_in_json_object_before(text: str, quote_start: int) -> bool:
  """Returns True when the nearest non-whitespace char before ``quote_start`` is ``{`` or ``,``."""
  prev_idx = quote_start - 1
  while prev_idx >= 0:
    ch = text[prev_idx]
    if ch in (" ", "\t", "\r", "\n"):
      prev_idx -= 1
    elif (
        prev_idx >= 1
        and ch in ("t", "r", "n")
        and text[prev_idx - 1] == "\\"
        and _count_backslashes_ending_at(text, prev_idx - 1) % 2 == 1
    ):
      prev_idx -= 2
    else:
      break
  return prev_idx >= 0 and text[prev_idx] in ("{", ",")


def _scan_mismatched_quote_key_kv(
    text: str, inner_start: int, *, max_key_len: int = 1024
) -> tuple[int, int, re.Match[str]] | None:
  """Scans a bounded same-line key up to the first quote delimiter and KV connector.

  Returns ``(close_end, close_start, conn_m)`` when a valid non-empty key ends
  at a quote (optionally preceded by an odd backslash run) followed immediately
  by a KV connector, else ``None``. Never scans past the first quote delimiter.
  """
  n = len(text)
  limit = min(n, inner_start + max_key_len)
  p = inner_start
  while p < limit:
    ch = text[p]
    if ch in ("\r", "\n", ";"):
      return None
    if ch == "\\":
      bs = 0
      while p + bs < limit and text[p + bs] == "\\":
        bs += 1
      if p + bs < limit and text[p + bs] in ('"', "'"):
        close_start = p if bs % 2 == 1 else p + bs
        close_end = p + bs + 1
        inner = text[inner_start:close_start]
        if (
            not inner
            or inner[0].isspace()
            or inner[0] in ",)}]"
            or inner.rstrip().endswith("=")
        ):
          return None
        conn_m = _QUOTED_CONN_RE.match(text, close_end)
        return (close_end, close_start, conn_m) if conn_m is not None else None
      p += bs
      continue
    if ch in ('"', "'"):
      close_start = p
      close_end = p + 1
      inner = text[inner_start:close_start]
      if (
          not inner
          or inner[0].isspace()
          or inner[0] in ",)}]"
          or inner.rstrip().endswith("=")
      ):
        return None
      conn_m = _QUOTED_CONN_RE.match(text, close_end)
      return (close_end, close_start, conn_m) if conn_m is not None else None
    p += 1
  return None


def _unescape_safe_ascii_unicode_escapes(text: str) -> str:
  """Unescapes odd-backslash ASCII ``\\u0020``-``\\u007e`` except ``"``/``\\`` in O(N)."""
  pos = text.find("\\u00")
  if pos == -1:
    return text
  n = len(text)
  out: list[str] = []
  emit_idx = 0
  idx = pos
  while idx != -1:
    if idx + 6 <= n:
      h1 = text[idx + 4]
      h2 = text[idx + 5]
      if "2" <= h1 <= "7" and h2 in "0123456789abcdefABCDEF":
        code = int(text[idx + 4 : idx + 6], 16)
        if (
            0x20 <= code <= 0x7E
            and code not in (0x22, 0x5C)
            and _count_backslashes_ending_at(text, idx) % 2 == 1
        ):
          out.append(text[emit_idx:idx])
          out.append(chr(code))
          emit_idx = idx + 6
          idx = text.find("\\u00", idx + 6)
          continue
    idx = text.find("\\u00", idx + 4)
  if not out:
    return text
  out.append(text[emit_idx:])
  return "".join(out)


def _might_contain_secret_or_kv(s: str) -> bool:
  """Fast substring check for KV connectors, containers, unicode, or tokens."""
  if _might_contain_kv(s) or "{" in s or "[" in s or "\\u" in s:
    return True
  low = s.lower()
  return any(
      tok in low
      for tok in (
          "bearer",
          "basic",
          "ya29.",
          "aiza",
          "sk-",
          "ghp_",
          "gho_",
          "ghu_",
          "ghs_",
          "ghr_",
          "github_pat_",
          "private key",
      )
  )


def _scrub_quoted_inner(
    raw_inner: str,
    *,
    quote_kind: str,
    depth: int,
    lead_bs: int = 1,
) -> str:
  """Scrubs inner text of a quoted or escaped-quoted token at bounded depth."""
  if depth >= 3:
    return raw_inner
  has_escape = "\\" in raw_inner
  needs_decode = "\\u" in raw_inner or (
      has_escape and _might_contain_secret_or_kv(raw_inner)
  )
  if quote_kind == '"' and needs_decode:
    try:
      decoded_str = json.loads('"' + raw_inner + '"')
    except ValueError:
      decoded_str = None
    if isinstance(decoded_str, str):
      scrubbed_decoded = _scrub_text_at_depth(decoded_str, _depth=depth + 1)
      if scrubbed_decoded != decoded_str:
        return json.dumps(scrubbed_decoded, ensure_ascii=raw_inner.isascii())[
            1:-1
        ]
      return raw_inner
  elif (
      quote_kind == '\\"' and lead_bs >= 1 and lead_bs % 2 == 1 and needs_decode
  ):
    max_layers = 3 if lead_bs >= 3 else 2
    layers: list[str] = [raw_inner]
    cur = raw_inner
    for _ in range(max_layers):
      try:
        nxt = json.loads('"' + cur + '"')
      except ValueError:
        break
      if not isinstance(nxt, str) or nxt == cur:
        break
      layers.append(nxt)
      cur = nxt
    if len(layers) > 1:
      scrubbed_decoded = _scrub_text_at_depth(cur, _depth=depth + 1)
      if scrubbed_decoded != cur:
        reencoded = scrubbed_decoded
        ascii_only = raw_inner.isascii()
        for _ in range(len(layers) - 1):
          reencoded = json.dumps(reencoded, ensure_ascii=ascii_only)[1:-1]
        return reencoded
      return raw_inner
  if _might_contain_kv(raw_inner):
    return _scrub_sensitive_kv_in_text(raw_inner, _depth=depth + 1)
  return raw_inner


def _inspect_quoted_kv_token(
    text: str,
    start: int,
    end_tok: int,
    conn_m: re.Match[str],
    *,
    quote_kind: str,
    depth: int,
    close_start: int | None = None,
    lead_bs: int = 1,
) -> tuple[str, str, int, int, bool, bool]:
  """Inspects a quoted KV key token followed by a connector match.

  Returns ``(decoded_key, emitted_key, val_start, nl_count, is_sensitive_outer,
  in_json_object)``.
  """
  n = len(text)
  if quote_kind == '\\"':
    inner_end = close_start if close_start is not None else end_tok - 2
    raw_inner = text[start + 2 : inner_end]
    closing_delim = text[inner_end:end_tok]
    decoded_key = _decode_escaped_quoted_kv_key(raw_inner)
    prev_idx = start - lead_bs
  else:
    raw_inner = text[start + 1 : end_tok - 1]
    closing_delim = quote_kind
    decoded_key = (
        _decode_quoted_kv_key(text[start:end_tok])
        if quote_kind == '"'
        else raw_inner
    )
    prev_idx = start - 1
  scrubbed_inner = _scrub_quoted_inner(
      raw_inner, quote_kind=quote_kind, depth=depth, lead_bs=lead_bs
  )
  inner_changed = scrubbed_inner != raw_inner
  emitted_key = quote_kind + scrubbed_inner + closing_delim
  val_start, nl_count = _skip_kv_whitespace(text, conn_m.end())
  next_char = text[val_start] if val_start < n else ""
  is_container = next_char in ("{", "[", "(")
  outer_is_structured_or_quoted = next_char in (
      '"',
      "'",
      "{",
      "[",
      "(",
  ) or _starts_with_odd_escaped_quote(text, val_start)
  saw_newline = False
  while prev_idx >= 0:
    ch = text[prev_idx]
    if ch in (" ", "\t", "\r", "\n"):
      if ch == "\n":
        saw_newline = True
      prev_idx -= 1
    elif (
        prev_idx >= 1
        and ch in ("t", "r", "n")
        and text[prev_idx - 1] == "\\"
        and _count_backslashes_ending_at(text, prev_idx - 1) % 2 == 1
    ):
      if ch == "n":
        saw_newline = True
      prev_idx -= 2
    else:
      break
  in_json_object = prev_idx >= 0 and text[prev_idx] in ("{", ",")
  at_key_boundary = prev_idx < 0 or saw_newline or in_json_object
  should_check_outer = (
      not inner_changed or outer_is_structured_or_quoted or at_key_boundary
  )
  is_sensitive_outer = should_check_outer and is_sensitive_key(
      decoded_key, is_container=is_container
  )
  return (
      decoded_key,
      emitted_key,
      val_start,
      nl_count,
      is_sensitive_outer,
      in_json_object,
  )


def _is_quoted_header_value_prose(
    text: str, conn_text: str, val_start: int
) -> bool:
  """Returns True when a quoted ``is``/``are`` header is followed by prose."""
  if ":" in conn_text or "=" in conn_text:
    return False
  n = len(text)
  if val_start >= n:
    return True
  ch = text[val_start]
  if ch in ('"', "'", "{", "[", "(") or _starts_with_odd_escaped_quote(
      text, val_start
  ):
    return False
  val_end = _scan_bare_value_end(text, val_start)
  next_word = text[val_start:val_end].rstrip(".,!?").lower()
  return next_word in _PROSE_PREDICATE_WORDS


def _has_colon_value_sep(text: str, pos: int) -> bool:
  """Returns True when ``pos`` begins whitespace, quote, container, or escape."""
  n = len(text)
  return (
      pos >= n
      or text[pos].isspace()
      or text[pos] in ('"', "'", "{", "[", "(")
      or _starts_with_odd_escaped_quote(text, pos)
      or (
          text[pos] == "\\" and pos + 1 < n and text[pos + 1] in ("t", "r", "n")
      )
  )


def _is_canonical_colon_key_segment(
    prev_seg: str, seg_bare: str, *, allow_suffix: bool = False
) -> bool:
  """Returns True when ``seg_bare`` is a canonical config key name (not a password)."""
  if not seg_bare:
    return False
  if "/" in seg_bare or "." in seg_bare:
    parts = [p for p in _KEY_SEGMENT_SEP_RE.split(seg_bare) if p]
    if len(parts) >= 2 and all(
        _is_namespace_colon_segment(p, allow_upper=p.isupper())
        for p in parts[:-1]
    ):
      return _is_canonical_colon_key_segment(
          parts[-2], parts[-1], allow_suffix=True
      )
    return False
  if any(ch.isdigit() for ch in seg_bare):
    if not allow_suffix or ("_" not in seg_bare and "-" not in seg_bare):
      return False
    seg_norm = _normalize_key_name(seg_bare)
    if not seg_norm:
      return False
    is_uniform_case = seg_bare.islower() or seg_bare.isupper()
    is_lower_camel = seg_bare[0].islower()
    if not (is_uniform_case or is_lower_camel):
      return False
    for suffix in _CANONICAL_SENSITIVE_KEY_SUFFIXES:
      if seg_norm.endswith(suffix):
        prefix_norm = seg_norm[: -len(suffix)]
        if prefix_norm and _is_namespace_colon_segment(
            prefix_norm, allow_upper=False
        ):
          return True
    return False
  seg_norm = _normalize_key_name(seg_bare)
  if not seg_norm:
    return False
  is_uniform_case = seg_bare.islower() or seg_bare.isupper()
  is_lower_camel = seg_bare[0].islower()
  if seg_norm in _COMPOUND_SENSITIVE_KEY_TAILS:
    prev_norm = _normalize_key_name(prev_seg)
    if (
        prev_norm
        and _SENSITIVE_KEY_RE.fullmatch(f"{prev_norm}_{seg_norm}") is not None
    ):
      is_title_compound = all(
          not part or (part[0].isupper() and part[1:].islower())
          for part in _KEY_SEP_RE.split(seg_bare.replace("_", " "))
      )
      if is_uniform_case or is_lower_camel or is_title_compound:
        return True
  if not (is_uniform_case or is_lower_camel):
    return False
  if seg_norm in _CANONICAL_SENSITIVE_KEY_EXACT:
    return True
  if allow_suffix and seg_norm.endswith(_CANONICAL_SENSITIVE_KEY_SUFFIXES):
    return True
  return False


def _is_container_colon_head(bare: str) -> bool:
  """Returns True when ``bare`` is a container key or container/namespace path."""
  tail_norm = _normalize_key_name(bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1])
  if tail_norm in _COLON_CHAIN_CONTAINER_KEYS:
    return True
  if "/" in bare or "." in bare:
    segs = [s for s in _KEY_SEGMENT_SEP_RE.split(bare) if s]
    for i, seg in enumerate(segs[:-1]):
      if _normalize_key_name(seg) in _COLON_CHAIN_CONTAINER_KEYS:
        rest = segs[i + 1 :]
        if all(
            not is_sensitive_key(s, is_container=False)
            and _is_namespace_colon_segment(s, allow_upper=s.isupper())
            for s in rest
        ):
          return True
  return False


def _is_namespace_colon_segment(seg: str, *, allow_upper: bool = False) -> bool:
  """Returns True for short namespace words (``[a-z][a-z0-9_-]*``, or upper)."""
  if not seg or len(seg) > 24:
    return False
  cleaned = seg.replace("_", "").replace("-", "")
  if not cleaned or not cleaned.isalnum():
    return False
  if seg[0].islower() and seg.islower():
    return True
  if allow_upper and seg[0].isupper() and seg.isupper():
    return True
  return False


def _scan_url_authority_and_safe_path(
    text: str, scheme_colon_end: int
) -> tuple[int, tuple[int, int] | None, bool]:
  """Scans URL authority (redacting ``user:pass@``) and safe non-KV path segments.

  Returns ``(new_url_pos, auth_redact_span, stopped_at_kv_seg)`` where
  ``auth_redact_span`` is ``(pass_start, pass_end)`` when embedded userinfo
  password is present, and ``stopped_at_kv_seg`` is True when ``new_url_pos``
  points directly at the start of a URL path segment containing a sensitive
  KV assignment or colon segment.
  """
  n = len(text)
  if text.startswith("//", scheme_colon_end):
    p = scheme_colon_end + 2
  elif text.startswith("\\/\\/", scheme_colon_end):
    p = scheme_colon_end + 4
  else:
    return scheme_colon_end, None, False

  auth_start = p
  hard_p = auth_start
  colon_pos = -1
  last_colon_pos = -1
  last_at_pos = -1
  last_dot_pos = -1
  while (
      hard_p < n
      and not text[hard_p].isspace()
      and text[hard_p] not in _URL_AUTH_HARD_STOP_CHARS
  ):
    ch = text[hard_p]
    if ch == "\\":
      bs = 0
      while hard_p + bs < n and text[hard_p + bs] == "\\":
        bs += 1
      if (
          hard_p + bs < n
          and text[hard_p + bs] in ("/", "t", "r", "n", '"', "'")
          and bs % 2 == 1
      ):
        hard_p += bs - 1
        break
      hard_p += bs
      continue
    if colon_pos == -1:
      if ch in _URL_STOP_CHARS or ch == "=":
        break
      if ch == ".":
        last_dot_pos = hard_p
      elif ch == ":":
        colon_pos = hard_p
        last_colon_pos = hard_p
    else:
      if (
          ch in ("&", ",", ";")
          and _URL_DELIM_KV_RE.match(text, hard_p) is not None
      ):
        break
      if ch in ("&", ",", ";", ")", "'"):
        port_len = hard_p - last_colon_pos - 1
        if 0 < port_len <= 5:
          port_s = text[last_colon_pos + 1 : hard_p]
          if port_s.isascii() and port_s.isdigit() and int(port_s) <= 65535:
            host_start = last_at_pos + 1 if last_at_pos != -1 else auth_start
            if (host_start <= last_dot_pos < last_colon_pos) or (
                last_colon_pos - host_start == 9
                and text[host_start:last_colon_pos].lower() == "localhost"
            ):
              break
      if ch == ".":
        last_dot_pos = hard_p
      elif ch == ":":
        last_colon_pos = hard_p
      elif ch == "@":
        last_at_pos = hard_p
    hard_p += 1

  auth_redact_span: tuple[int, int] | None = None
  at_pos = text.rfind("@", auth_start, hard_p)
  if at_pos != -1:
    username_end = (
        colon_pos if (colon_pos != -1 and colon_pos < at_pos) else at_pos
    )
    if any(
        c in _URL_STOP_CHARS or c == "=" for c in text[auth_start:username_end]
    ):
      at_pos = -1
  if at_pos != -1:
    if colon_pos != -1 and colon_pos + 1 < at_pos:
      auth_redact_span = (colon_pos + 1, at_pos)
    p = at_pos + 1
  else:
    p = auth_start

  while p < n and not text[p].isspace() and text[p] not in _URL_STOP_CHARS:
    if text[p] == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if (
          p + bs < n
          and text[p + bs] in ("/", "t", "r", "n", '"', "'")
          and bs % 2 == 1
      ):
        p += bs - 1
        break
      p += bs
    else:
      p += 1
  auth_end = p

  url_pos = auth_end
  while url_pos < n:
    if text[url_pos] == "/":
      sep_len = 1
    elif text.startswith("\\/", url_pos):
      sep_len = 2
    else:
      break
    seg_start = url_pos + sep_len
    q = seg_start
    has_eq = False
    colon_pos = -1
    colon_count = 0
    while q < n and not text[q].isspace() and text[q] not in _URL_STOP_CHARS:
      ch = text[q]
      if ch == "=":
        has_eq = True
        q += 1
      elif ch == ":":
        colon_count += 1
        if colon_pos == -1:
          colon_pos = q
        q += 1
      elif ch == "\\":
        bs = 0
        while q + bs < n and text[q + bs] == "\\":
          bs += 1
        if (
            q + bs < n
            and text[q + bs] in ("/", "t", "r", "n", '"', "'")
            and bs % 2 == 1
        ):
          q += bs - 1
          break
        q += bs
      else:
        q += 1
    seg_end = q
    if has_eq:
      key_before_eq = text[seg_start:seg_end].split("=", 1)[0].lstrip("./-")
      if is_sensitive_key(key_before_eq, is_container=False) or colon_count > 0:
        return seg_start, auth_redact_span, True
      url_pos = seg_end
      continue
    if colon_count > 0:
      trailing_colon = (
          colon_count == 1
          and colon_pos == seg_end - 1
          and not (
              seg_end < n
              and (text[seg_end] == "/" or text.startswith("\\/", seg_end))
          )
      )
      if not trailing_colon:
        return seg_start, auth_redact_span, True
      key_seg = text[seg_start : seg_end - 1]
      val_start, _ = _skip_kv_whitespace(text, seg_end)
      is_container = val_start < n and text[val_start] in ("{", "[", "(")
      if is_sensitive_key(key_seg, is_container=is_container):
        return seg_start, auth_redact_span, True
      url_pos = seg_end
      break
    url_pos = seg_end
  return url_pos, auth_redact_span, False


def _resolve_bare_or_path_kv(
    text: str, match: re.Match[str]
) -> tuple[str | None, str, int, int, int]:
  """Resolves a bare or colon/slash-chained KV header from ``match``.

  Returns ``(matched_key, conn_text, val_start, nl_count, match_end)``.
  """
  n = len(text)
  match_end = match.end()
  conn_text = match.group("conn")
  head_bare = match.group("bare") or ""
  norm_head = _normalize_key_name(head_bare)
  is_cookie_head = norm_head in (
      "cookie",
      "cookies",
      "set_cookie",
      "set_cookies",
  ) or norm_head.endswith(("_cookie", "_cookies"))
  if (
      not is_cookie_head
      and conn_text == ":"
      and match_end < n
      and text[match_end] in _PATH_SEG_START_CHARS
  ):
    bare_start = match.start("bare")
    acc_sensitive = is_sensitive_key(head_bare, is_container=False)
    acc_is_container = _is_container_colon_head(head_bare)
    prev_seg = head_bare
    last_m: re.Match[str] = match
    chained = False
    cur = match_end
    last_seen_end = match_end
    last_seen_conn = conn_text
    while cur < n and text[cur] in _PATH_SEG_START_CHARS:
      seg_m = _BARE_KV_RE.match(text, cur)
      if seg_m is None or seg_m.start("bare") != cur:
        break
      seg_bare = seg_m.group("bare") or ""
      seg_conn = seg_m.group("conn")
      seg_end = seg_m.end()
      if (
          seg_end < n
          and not text[seg_end].isspace()
          and text[seg_end] not in "{[(\\"
      ):
        seg_is_container = False
      else:
        seg_val_start, _ = _skip_kv_whitespace(text, seg_end)
        seg_is_container = seg_val_start < n and text[seg_val_start] in (
            "{",
            "[",
            "(",
        )
      if acc_sensitive:
        norm_seg = _normalize_key_name(seg_bare)
        seg_is_safe_tail = not seg_is_container and (
            norm_seg in _SAFE_DIAGNOSTIC_SCALAR_KEYS
            or norm_seg.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
        )
        if (
            seg_is_safe_tail
            and seg_conn == ":"
            and seg_end < n
            and (
                text[seg_end].isspace()
                or text[seg_end] in ('"', "'", "{", "[", "(")
            )
            and not is_sensitive_key(
                f"{prev_seg}:{seg_bare}", is_container=seg_is_container
            )
        ):
          return None, seg_conn, seg_end, 0, seg_end
        if not acc_is_container:
          break
        if norm_seg == _normalize_key_name(prev_seg):
          break
        if seg_conn == "=":
          break
        has_val_sep = seg_conn != ":" or _has_colon_value_sep(text, seg_end)
        if _is_canonical_colon_key_segment(
            prev_seg, seg_bare, allow_suffix=has_val_sep
        ):
          can_adopt = has_val_sep
          if (
              not can_adopt
              and norm_seg in _COLON_CHAIN_CONTAINER_KEYS
              and seg_end < n
              and text[seg_end] in _PATH_SEG_START_CHARS
          ):
            peek_m = _BARE_KV_RE.match(text, seg_end, min(n, seg_end + 256))
            if (
                peek_m is not None
                and peek_m.start("bare") == seg_end
                and peek_m.group("conn") != "="
                and _normalize_key_name(peek_m.group("bare") or "") != norm_seg
                and _is_canonical_colon_key_segment(
                    seg_bare, peek_m.group("bare") or "", allow_suffix=True
                )
            ):
              can_adopt = True
          if not can_adopt:
            break
          last_m = seg_m
          prev_seg = seg_bare
          acc_is_container = norm_seg in _COLON_CHAIN_CONTAINER_KEYS
          chained = True
          cur = seg_end
          last_seen_end = seg_end
          last_seen_conn = seg_conn
          if seg_conn != ":":
            break
          continue
        # Bounded 1-to-2 hop namespace lookahead from container keys (e.g.
        # "auth:v1:password: S3CR3T", "SECRETS:PROD:DB_PASSWORD: hunter2").
        allow_upper_ns = prev_seg.isupper()
        if seg_conn == ":" and _is_namespace_colon_segment(
            seg_bare, allow_upper=allow_upper_ns
        ):
          ahead_cur = seg_end
          ahead_prev = seg_bare
          adopted_ahead: re.Match[str] | None = None
          for _ in range(2):
            if ahead_cur >= n or text[ahead_cur] not in _PATH_SEG_START_CHARS:
              break
            ahead_m = _BARE_KV_RE.match(
                text, ahead_cur, min(n, ahead_cur + 256)
            )
            if ahead_m is None or ahead_m.start("bare") != ahead_cur:
              break
            ahead_bare = ahead_m.group("bare") or ""
            ahead_conn = ahead_m.group("conn")
            ahead_end = ahead_m.end()
            if (
                ahead_conn != "="
                and _normalize_key_name(ahead_bare)
                != _normalize_key_name(ahead_prev)
                and _is_canonical_colon_key_segment(
                    ahead_prev, ahead_bare, allow_suffix=True
                )
                and (ahead_conn != ":" or _has_colon_value_sep(text, ahead_end))
            ):
              adopted_ahead = ahead_m
              break
            if ahead_conn == ":" and _is_namespace_colon_segment(
                ahead_bare, allow_upper=allow_upper_ns
            ):
              ahead_prev = ahead_bare
              ahead_cur = ahead_end
              continue
            break
          if adopted_ahead is not None:
            last_m = adopted_ahead
            prev_seg = adopted_ahead.group("bare") or ""
            acc_is_container = (
                _normalize_key_name(prev_seg) in _COLON_CHAIN_CONTAINER_KEYS
            )
            chained = True
            cur = adopted_ahead.end()
            last_seen_end = cur
            last_seen_conn = adopted_ahead.group("conn")
            if last_seen_conn != ":":
              break
            continue
        break
      else:
        if is_sensitive_key(
            f"{prev_seg}:{seg_bare}", is_container=seg_is_container
        ):
          acc_sensitive = True
          acc_is_container = _is_container_colon_head(seg_bare)
          last_m = seg_m
          prev_seg = seg_bare
          chained = True
          cur = seg_end
          last_seen_end = seg_end
          last_seen_conn = seg_conn
          if seg_conn != ":":
            break
        else:
          prev_seg = seg_bare
          cur = seg_end
          last_seen_end = seg_end
          last_seen_conn = seg_conn
          if seg_conn != ":":
            break
    if acc_sensitive and chained:
      full_key = text[bare_start : last_m.end("bare")]
      final_conn = last_m.group("conn")
      final_end = last_m.end()
      chain_val_start, chain_nl_count = _skip_kv_whitespace(text, final_end)
      return (
          full_key,
          final_conn,
          chain_val_start,
          chain_nl_count,
          final_end,
      )
    if not acc_sensitive and last_seen_end > match_end:
      return None, last_seen_conn, last_seen_end, 0, last_seen_end
  if (
      match_end < n
      and not text[match_end].isspace()
      and text[match_end] not in "{[(\\"
  ):
    val_start = match_end
    nl_count = 0
    is_container = False
  else:
    val_start, nl_count = _skip_kv_whitespace(text, match_end)
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
  matched_key = _extract_bare_kv_key(text, match, is_container=is_container)
  return matched_key, conn_text, val_start, nl_count, match_end


def _is_sensitive_header_at(text: str, pos: int) -> bool:
  """Returns True when ``pos`` begins a sensitive KV header on the next line."""
  n = len(text)
  if pos >= n:
    return False
  lead_bs = 0
  while pos + lead_bs < n and text[pos + lead_bs] == "\\":
    lead_bs += 1
  if (
      lead_bs > 0
      and lead_bs % 2 == 1
      and pos + lead_bs < n
      and text[pos + lead_bs] == '"'
  ):
    edq_start = pos + lead_bs - 1
    end_tok, stop_pos, close_start = _scan_same_line_escaped_dq_end(
        text, edq_start, lead_bs=lead_bs
    )
    conn_m: re.Match[str] | None = None
    if end_tok is not None:
      conn_m = _QUOTED_CONN_RE.match(text, end_tok)
    else:
      if (
          stop_pos < n
          and text[stop_pos] == '"'
          and close_start > edq_start + 2
          and close_start - (edq_start + 2) <= 1024
      ):
        conn_m = _QUOTED_CONN_RE.match(text, stop_pos + 1)
      if conn_m is None:
        found = _scan_mismatched_quote_key_kv(text, edq_start + 2)
        if found is not None:
          _, close_start, conn_m = found
    if conn_m is None:
      return False
    decoded_key = _decode_escaped_quoted_kv_key(
        text[edq_start + 2 : close_start]
    )
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    return not _is_quoted_header_value_prose(text, conn_m.group(0), val_start)
  if (
      lead_bs > 0
      and lead_bs % 2 == 1
      and pos + lead_bs < n
      and text[pos + lead_bs] == "'"
  ):
    inner_start = pos + lead_bs + 1
    found = _scan_mismatched_quote_key_kv(text, inner_start)
    if found is None:
      return False
    _, close_start, conn_m = found
    decoded_key = _decode_escaped_quoted_kv_key(text[inner_start:close_start])
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    return not _is_quoted_header_value_prose(text, conn_m.group(0), val_start)
  if text[pos] in ('"', "'"):
    ch = text[pos]
    end_tok = _scan_same_line_quote_end(text, pos)
    conn_m = (
        _QUOTED_CONN_RE.match(text, end_tok) if end_tok is not None else None
    )
    if conn_m is not None:
      decoded_key = (
          _decode_quoted_kv_key(text[pos:end_tok])
          if ch == '"'
          else text[pos + 1 : end_tok - 1]
      )
    else:
      found = _scan_mismatched_quote_key_kv(text, pos + 1)
      if found is None:
        return False
      _, close_start, conn_m = found
      raw_inner = text[pos + 1 : close_start]
      decoded_key = (
          _decode_quoted_kv_key(f'"{raw_inner}"') if ch == '"' else raw_inner
      )
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    return not _is_quoted_header_value_prose(text, conn_m.group(0), val_start)
  if (
      pos >= 2
      and text[pos - 2 : pos] in ("\\t", "\\r", "\\n")
      and _count_backslashes_ending_at(text, pos - 2) % 2 == 0
  ):
    return False
  bare_m = _BARE_KV_RE.match(text, pos)
  if bare_m is not None:
    matched_key, _, _, _, _ = _resolve_bare_or_path_kv(text, bare_m)
    if matched_key is not None:
      return True
  # Check up to 3 leading same-line words (including slash/colon path words)
  # for multi-word headers (e.g. "api key: ...", "request/api key: ...",
  # "request:api key: ...", "aws secret access key: ...").
  word_cursor = pos
  first_word = ""
  for words_before in range(1, 4):
    w_start = word_cursor
    if words_before == 1:
      while (
          word_cursor < n
          and text[word_cursor] in ("/", ".", "-")
          and (word_cursor - w_start) <= 64
      ):
        word_cursor += 1
    if word_cursor >= n or text[word_cursor] not in _WORD_START_CHARS:
      break
    while word_cursor < n and (word_cursor - w_start) <= 64:
      ch = text[word_cursor]
      if ch in _WORD_CHARS or ch in ("/", "."):
        word_cursor += 1
      elif (
          ch == ":"
          and word_cursor + 1 < n
          and text[word_cursor + 1] in _PATH_SEG_START_CHARS
      ):
        word_cursor += 1
      else:
        break
    if (
        (word_cursor - w_start) > 64
        or word_cursor <= w_start
        or text[word_cursor - 1] not in _WORD_CHARS
        or "://" in text[w_start:word_cursor]
        or (word_cursor < n and text[word_cursor] in _WORD_CHARS)
    ):
      break
    if words_before == 1:
      first_word = text[w_start:word_cursor]
    if word_cursor >= n or text[word_cursor] not in (" ", "\t"):
      break
    while word_cursor < n and text[word_cursor] in (" ", "\t"):
      word_cursor += 1
    cand_m = _BARE_KV_RE.match(text, word_cursor)
    if cand_m is not None:
      matched_key, _, next_val_start, _, _ = _resolve_bare_or_path_kv(
          text, cand_m
      )
      next_is_container = next_val_start < n and text[next_val_start] in (
          "{",
          "[",
          "(",
      )
      if matched_key is not None and (
          matched_key.count(" ") == words_before
          or (
              words_before == 1
              and (cand_m.group("bare") or "").lower() == "token"
              and is_sensitive_key(
                  f"{first_word} {cand_m.group('bare')}",
                  is_container=next_is_container,
              )
          )
          or (
              words_before == 3
              and matched_key.lower() == "secret access key"
              and (
                  first_word.lower() == "aws"
                  or first_word.lower().endswith(("/aws", ":aws"))
              )
          )
      ):
        return True
  return False


def _strip_matching_quotes(raw_val: str) -> str:
  """Strips matching outer quotes (or escaped quotes) from a scalar string."""
  n = len(raw_val)
  if n >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in ('"', "'"):
    return raw_val[1:-1]
  if n >= 4 and raw_val[0] == "\\" and raw_val[-1] in ('"', "'"):
    q = raw_val[-1]
    lead_bs = 0
    while lead_bs < n and raw_val[lead_bs] == "\\":
      lead_bs += 1
    if (
        lead_bs % 2 == 1
        and lead_bs < n
        and raw_val[lead_bs] == q
        and n >= 2 * (lead_bs + 1)
    ):
      delim = "\\" * lead_bs + q
      if raw_val.endswith(delim):
        trail_start = n - (lead_bs + 1)
        if trail_start == lead_bs + 1 or raw_val[trail_start - 1] != "\\":
          return raw_val[lead_bs + 1 : trail_start]
  return raw_val


def _skip_kv_whitespace(text: str, pos: int) -> tuple[int, int]:
  """Advances past whitespace and JSON \\t/\\r/\\n escapes after a KV connector.

  Returns ``(new_pos, newline_count)`` counting both literal ``\\n`` and
  JSON-escaped ``\\\\n`` line breaks.
  """
  n = len(text)
  newlines = 0
  while pos < n:
    ch = text[pos]
    if ch.isspace():
      if ch == "\n":
        newlines += 1
      pos += 1
    elif ch == "\\":
      bs = 0
      while pos + bs < n and text[pos + bs] == "\\":
        bs += 1
      if pos + bs < n and text[pos + bs] in ("t", "r", "n"):
        after_esc = pos + bs + 1
        if bs % 2 == 1 or (
            after_esc < n
            and (
                text[after_esc].isspace()
                or (
                    text[after_esc] == "\\"
                    and after_esc + 1 < n
                    and text[after_esc + 1] in ("t", "r", "n", "\\")
                )
            )
        ):
          if text[pos + bs] == "n":
            newlines += 1
          pos = after_esc
          continue
      break
    else:
      break
  return pos, newlines


def _starts_cookie_eq_pair(text: str, pos: int) -> bool:
  """Returns True when ``pos`` begins a ``<valid_cookie_name>=`` pair."""
  n = len(text)
  k = pos
  while (
      k < n and not text[k].isspace() and text[k] not in _COOKIE_NAME_STOP_CHARS
  ):
    if text[k] == "\\":
      bs = 0
      while k + bs < n and text[k + bs] == "\\":
        bs += 1
      if k + bs < n and text[k + bs] in ("t", "r", "n") and bs % 2 == 1:
        return False
      k += bs
    else:
      k += 1
  return k > pos and k < n and text[k] == "="


def _consume_cookie_header_value(text: str, pos: int) -> tuple[int, str] | None:
  """Consumes a single-line ``;``-separated Cookie header value in O(N)."""
  n = len(text)
  p = pos
  while p < n and text[p] in (";", " ", "\t"):
    p += 1
  last_end = pos
  found_pair = False
  seen_eq_pair = False
  no_semi_until = 0
  while p < n:
    stop_cookie_loop = False
    pair_start = p
    while (
        p < n
        and not text[p].isspace()
        and text[p] not in _COOKIE_NAME_STOP_CHARS
    ):
      if text[p] == "\\":
        bs = 0
        while p + bs < n and text[p + bs] == "\\":
          bs += 1
        if p + bs < n and text[p + bs] in ("t", "r", "n") and bs % 2 == 1:
          p += bs - 1
          break
        p += bs
      else:
        p += 1
    pair_name = text[pair_start:p]
    has_name = p > pair_start
    has_eq = p < n and text[p] == "="
    if not has_name and not has_eq:
      break
    if not has_eq:
      if p < n and (
          text[p] == ":" or _COOKIE_KV_CONN_RE.match(text, p) is not None
      ):
        if found_pair:
          break
        if is_sensitive_key(pair_name):
          return None
        if text[p] == ":" and p + 1 < n and not text[p + 1].isspace():
          p = _scan_bare_value_end(text, pair_start)
        stop_cookie_loop = True
    if has_eq:
      seen_eq_pair = True
      p += 1
      date_m = (
          _HTTP_COOKIE_DATE_RE.match(text, p)
          if pair_name.lower() == "expires"
          else None
      )
      if date_m is not None:
        p = date_m.end()
      else:
        lead_bs = 0
        while p + lead_bs < n and text[p + lead_bs] == "\\":
          lead_bs += 1
        if (
            lead_bs > 0
            and lead_bs % 2 == 1
            and p + lead_bs < n
            and text[p + lead_bs] in ('"', "'")
        ):
          val_start = p
          q = text[p + lead_bs]
          close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
          val_open_end = p + lead_bs + 1
          p = val_open_end
          stopped_at_outer_q = False
          while p < n:
            ch = text[p]
            if ch in ("\r", "\n"):
              break
            if ch == '"' or ch == q:
              stopped_at_outer_q = True
              break
            if ch == "\\":
              bs = 0
              while p + bs < n and text[p + bs] == "\\":
                bs += 1
              if p + bs < n and text[p + bs] == q:
                if ((bs + 1) & -(bs + 1)) < low_zero:
                  p += bs
                  stopped_at_outer_q = True
                  break
                if bs % period == close_mod:
                  if q == '"' and _quote_opens_kv_at(
                      text, p + bs - 1, escaped_dq=True
                  ):
                    stopped_at_outer_q = True
                    stop_cookie_loop = True
                    if not any(c.isalnum() for c in text[val_open_end:p]):
                      p = val_start
                    break
                  p += bs + 1
                  break
                p += bs + 1
                continue
              if p + bs < n and text[p + bs] == '"':
                if ((bs + 1) & -(bs + 1)) < low_zero:
                  p += bs
                  stopped_at_outer_q = True
                  break
                p += bs + 1
                continue
              if p + bs < n and text[p + bs] in ("r", "n") and bs % 2 == 1:
                p += bs - 1
                stopped_at_outer_q = True
                break
              p += bs
              continue
            p += 1
          if not stopped_at_outer_q:
            while p < n and not text[p].isspace() and text[p] not in ";)}]\r\n":
              if text[p] == '"' or text[p] == q:
                break
              if text[p] == "\\":
                bs = 0
                while p + bs < n and text[p + bs] == "\\":
                  bs += 1
                if (
                    p + bs < n
                    and text[p + bs] in ("t", "r", "n")
                    and bs % 2 == 1
                ):
                  p += bs - 1
                  break
                if p + bs < n and text[p + bs] in ('"', "'"):
                  if bs % 2 == 1:
                    p += bs + 1
                    continue
                  p += bs
                  break
                p += bs
                continue
              p += 1
        elif (
            lead_bs > 0
            and lead_bs % 2 == 0
            and p + lead_bs < n
            and text[p + lead_bs] == "'"
            and p + lead_bs + 1 < n
            and text[p + lead_bs + 1] not in ",}]) \t\r\n"
        ):
          val_start = p
          val_open_end = p + lead_bs + 1
          scan = val_open_end
          close_end: int | None = None
          while scan < n:
            ch = text[scan]
            if ch in ("\r", "\n", '"'):
              break
            if ch == "'":
              if _quote_opens_kv_at(text, scan, escaped_dq=False):
                stop_cookie_loop = True
                close_end = (
                    val_start
                    if not any(c.isalnum() for c in text[val_open_end:scan])
                    else scan
                )
              else:
                close_end = scan + 1
              break
            if ch == "\\":
              bs = 0
              while scan + bs < n and text[scan + bs] == "\\":
                bs += 1
              if scan + bs < n and text[scan + bs] == "'":
                if bs % 2 == 0:
                  q_pos = scan + bs
                  if _quote_opens_kv_at(text, q_pos, escaped_dq=False):
                    stop_cookie_loop = True
                    close_end = (
                        val_start
                        if not any(
                            c.isalnum() for c in text[val_open_end:q_pos]
                        )
                        else q_pos
                    )
                  else:
                    close_end = q_pos + 1
                  break
                scan += bs + 1
                continue
              if scan + bs < n and text[scan + bs] == '"':
                if bs % 2 == 0:
                  break
                scan += bs + 1
                continue
              if (
                  scan + bs < n
                  and text[scan + bs] in ("r", "n")
                  and bs % 2 == 1
              ):
                break
              scan += bs
              continue
            scan += 1
          if close_end is not None:
            p = close_end
          else:
            p += lead_bs + 1
            while (
                p < n and not text[p].isspace() and text[p] not in ';)}]\r\n"'
            ):
              if text[p] == "\\":
                bs = 0
                while p + bs < n and text[p + bs] == "\\":
                  bs += 1
                if (
                    p + bs < n
                    and text[p + bs] in ("t", "r", "n")
                    and bs % 2 == 1
                ):
                  p += bs - 1
                  break
                if p + bs < n and text[p + bs] == '"':
                  if bs % 2 == 1:
                    p += bs + 1
                    continue
                  p += bs
                  break
                p += bs
              else:
                p += 1
        elif p < n and text[p] in ('"', "'"):
          val_start = p
          end_q = _scan_same_line_quote_end(text, p)
          if end_q is not None:
            if _quote_opens_kv_at(text, end_q - 1, escaped_dq=False):
              stop_cookie_loop = True
              if any(c.isalnum() for c in text[val_start + 1 : end_q - 1]):
                p = end_q - 1
            else:
              p = end_q
          else:
            consumed_to_semi = False
            if p >= no_semi_until:
              probe = p
              while probe < n and text[probe] not in ";)}]\r\n":
                if text[probe] == "\\":
                  bs = 0
                  while probe + bs < n and text[probe + bs] == "\\":
                    bs += 1
                  if (
                      probe + bs < n
                      and text[probe + bs] in ("t", "r", "n")
                      and bs % 2 == 1
                  ):
                    probe += bs - 1
                    break
                  probe += bs
                else:
                  probe += 1
              if probe < n and text[probe] == ";":
                p = probe
                consumed_to_semi = True
              else:
                no_semi_until = probe
            if not consumed_to_semi:
              while (
                  p < n and not text[p].isspace() and text[p] not in ";)}]\r\n"
              ):
                if text[p] == "\\":
                  bs = 0
                  while p + bs < n and text[p + bs] == "\\":
                    bs += 1
                  if (
                      p + bs < n
                      and text[p + bs] in ("t", "r", "n")
                      and bs % 2 == 1
                  ):
                    p += bs - 1
                    break
                  p += bs
                else:
                  p += 1
        else:
          while (
              p < n
              and not text[p].isspace()
              and text[p] not in _COOKIE_VAL_STOP_CHARS
          ):
            ch = text[p]
            if ch == ",":
              if p + 1 >= n or text[p + 1].isspace():
                break
              peek = p + 1
              has_kv_sep = False
              while (
                  peek < n
                  and not text[peek].isspace()
                  and text[peek] not in ",;)}]\"'"
              ):
                if text[peek] in (":", "="):
                  has_kv_sep = True
                  break
                if text[peek] == "\\":
                  bs = 0
                  while peek + bs < n and text[peek + bs] == "\\":
                    bs += 1
                  if (
                      peek + bs < n
                      and text[peek + bs] in ("t", "r", "n")
                      and bs % 2 == 1
                  ):
                    break
                  peek += bs
                else:
                  peek += 1
              if has_kv_sep or (peek == p + 1 and text[p + 1] != ","):
                break
              p += 1
              continue
            if ch == "\\":
              bs = 0
              while p + bs < n and text[p + bs] == "\\":
                bs += 1
              if p + bs < n and text[p + bs] in ("t", "r", "n") and bs % 2 == 1:
                p += bs - 1
                break
              if p + bs < n and text[p + bs] in ('"', "'"):
                if bs % 2 == 1:
                  p += bs + 1
                  continue
                p += bs
                break
              p += bs
              continue
            p += 1
    found_pair = True
    last_end = p
    if stop_cookie_loop:
      break
    sep_p = p
    saw_semi = False
    while sep_p < n and text[sep_p] in (";", " ", "\t"):
      if text[sep_p] == ";":
        saw_semi = True
      sep_p += 1
    if saw_semi:
      p = sep_p
    elif seen_eq_pair and p < n and text[p] == ",":
      after_comma = p + 1
      while after_comma < n and text[after_comma] in (" ", "\t"):
        after_comma += 1
      if _starts_cookie_eq_pair(text, after_comma):
        p = after_comma
      else:
        break
    else:
      break
  return (last_end, "[REDACTED]") if found_pair and last_end > pos else None


def _try_redact_sensitive_kv(
    text: str,
    matched_key: str,
    conn_text: str,
    val_start: int,
    nl_count: int,
    *,
    is_quoted_key: bool = False,
    in_json_object: bool = False,
) -> tuple[int, str | None] | None:
  """Consumes a sensitive KV value at ``val_start``.

  Returns ``(val_end, replacement)`` where ``replacement`` is ``None`` if the
  value is a safe diagnostic scalar to keep verbatim, or ``None`` if no value
  should be consumed at ``val_start``.
  """
  n = len(text)
  next_char = text[val_start] if val_start < n else ""
  is_container = next_char in ("{", "[", "(")
  allow_multiline_json_scalar = (
      is_quoted_key
      and _is_json_colon_connector(conn_text)
      and (
          in_json_object
          or _JSON_SCALAR_START_RE.match(text, val_start) is not None
      )
  )
  if (
      nl_count >= 2
      and not allow_multiline_json_scalar
      and next_char not in ('"', "'", "{", "[", "(")
      and not _starts_with_odd_escaped_quote(text, val_start)
  ):
    return None
  if (
      nl_count > 0
      and not is_container
      and _is_sensitive_header_at(text, val_start)
  ):
    return None
  norm_key = _normalize_key_name(matched_key)
  is_cookie_header = (
      not is_quoted_key
      and (
          norm_key in ("cookie", "cookies", "set_cookie", "set_cookies")
          or norm_key.endswith(("_cookie", "_cookies"))
      )
      and ":" in conn_text
      and next_char not in ("{", "[", "(", '"', "'")
      and not _starts_with_odd_escaped_quote(text, val_start)
      and _PY_STR_PREFIX_RE.match(text, val_start) is None
      and _CTOR_PREFIX_RE.match(text, val_start) is None
  )
  if is_cookie_header:
    consumed = _consume_cookie_header_value(text, val_start)
  else:
    consumed = _consume_sensitive_kv_value(text, val_start)
  if consumed is None:
    return None
  val_end, replacement = consumed
  if not is_container:
    raw_val = text[val_start:val_end].strip()
    unquoted_val = _strip_matching_quotes(raw_val)
    if _is_numeric_count_scalar(
        matched_key, unquoted_val
    ) or _is_safe_auth_status_scalar(matched_key, unquoted_val):
      return val_end, None
  return val_end, replacement


def _try_mismatched_kv_redaction(
    text: str,
    quote_start: int,
    inner_start: int,
    *,
    decode_escaped: bool,
) -> tuple[int, int, str | None] | None:
  """Attempts bounded KV redaction when a key's opening/closing quotes mismatch."""
  found = _scan_mismatched_quote_key_kv(text, inner_start)
  if found is None:
    return None
  _, close_start, conn_m = found
  raw_inner = text[inner_start:close_start]
  decoded_key = (
      _decode_escaped_quoted_kv_key(raw_inner)
      if decode_escaped
      else _decode_quoted_kv_key(f'"{raw_inner}"')
  )
  val_start, nl_count = _skip_kv_whitespace(text, conn_m.end())
  is_container = val_start < len(text) and text[val_start] in ("{", "[", "(")
  if not is_sensitive_key(decoded_key, is_container=is_container):
    return None
  in_json_object = _is_in_json_object_before(text, quote_start)
  redacted = _try_redact_sensitive_kv(
      text,
      decoded_key,
      conn_m.group(0),
      val_start,
      nl_count,
      is_quoted_key=True,
      in_json_object=in_json_object,
  )
  if redacted is None:
    return None
  val_end, replacement = redacted
  return val_start, val_end, replacement


def _scrub_sensitive_kv_in_text(text: str, *, _depth: int = 0) -> str:
  """Redacts sensitive key=value / key: value / key is value pairs in O(N)."""
  out: list[str] = []
  emit_idx = 0
  scan_idx = 0
  n = len(text)
  unclosed_edq_until: dict[int, int] = {}
  while scan_idx < n:
    match = _KV_SCAN_NEXT_RE.search(text, scan_idx)
    if match is None:
      break
    start = match.start()

    if match.group("bare") is None:
      # Branch A: escaped single quote or JSON \t/\r/\n escape sequence.
      if text[start : start + 2] in ("\\'", "\\t", "\\r", "\\n"):
        lead_bs = _count_backslashes_ending_at(text, start)
        if lead_bs % 2 == 0:
          scan_idx = start + 1
        else:
          if text[start + 1] == "'":
            mismatched = _try_mismatched_kv_redaction(
                text,
                start - lead_bs + 1,
                start + 2,
                decode_escaped=True,
            )
            if mismatched is not None:
              val_start, val_end, replacement = mismatched
              if replacement is not None:
                out.append(text[emit_idx:val_start])
                out.append(replacement)
                emit_idx = val_end
              scan_idx = val_end
              continue
          scan_idx = start + 2
        continue

      # Branch B: escaped double-quoted token \"...\".
      if text[start : start + 2] == '\\"':
        lead_bs = _count_backslashes_ending_at(text, start)
        if lead_bs % 2 == 0:
          scan_idx = start + 1
          continue
        close_mod, _, _ = _escaped_quote_mod_and_period(lead_bs)
        if start < unclosed_edq_until.get(close_mod, 0):
          end_tok = None
          stop_pos = start + 2
          close_start = start + 2
        else:
          end_tok, stop_pos, close_start = _scan_same_line_escaped_dq_end(
              text, start, lead_bs=lead_bs
          )
          if end_tok is None and stop_pos > unclosed_edq_until.get(
              close_mod, 0
          ):
            unclosed_edq_until[close_mod] = stop_pos
        if end_tok is None:
          if (
              stop_pos < n
              and text[stop_pos] == '"'
              and close_start > start + 2
              and close_start - (start + 2) <= 1024
          ):
            mismatched_end = stop_pos + 1
            conn_m = _QUOTED_CONN_RE.match(text, mismatched_end)
            if conn_m is not None:
              (
                  decoded_key,
                  emitted_key,
                  val_start,
                  nl_count,
                  is_sensitive_outer,
                  in_json_object,
              ) = _inspect_quoted_kv_token(
                  text,
                  start,
                  mismatched_end,
                  conn_m,
                  quote_kind='\\"',
                  depth=_depth,
                  close_start=close_start,
                  lead_bs=lead_bs,
              )
              key_changed = emitted_key != text[start:mismatched_end]
              if is_sensitive_outer:
                redacted = _try_redact_sensitive_kv(
                    text,
                    decoded_key,
                    conn_m.group(0),
                    val_start,
                    nl_count,
                    is_quoted_key=True,
                    in_json_object=in_json_object,
                )
                if redacted is None:
                  if key_changed:
                    out.append(
                        text[emit_idx:start]
                        + emitted_key
                        + text[mismatched_end : conn_m.end()]
                    )
                    emit_idx = conn_m.end()
                  scan_idx = conn_m.end()
                else:
                  val_end, replacement = redacted
                  if replacement is None:
                    if key_changed:
                      out.append(
                          text[emit_idx:start]
                          + emitted_key
                          + text[mismatched_end:val_end]
                      )
                      emit_idx = val_end
                  else:
                    if key_changed:
                      out.append(
                          text[emit_idx:start]
                          + emitted_key
                          + text[mismatched_end:val_start]
                      )
                    else:
                      out.append(text[emit_idx:val_start])
                    out.append(replacement)
                    emit_idx = val_end
                  scan_idx = val_end
                continue
              raw_mismatch_inner = text[start + 2 : close_start]
              if (
                  raw_mismatch_inner
                  and not raw_mismatch_inner[0].isspace()
                  and ";" not in raw_mismatch_inner
              ):
                if key_changed:
                  out.append(
                      text[emit_idx:start]
                      + emitted_key
                      + text[mismatched_end : conn_m.end()]
                  )
                  emit_idx = conn_m.end()
                scan_idx = conn_m.end()
                continue
          mismatched = _try_mismatched_kv_redaction(
              text,
              start - lead_bs + 1,
              start + 2,
              decode_escaped=True,
          )
          if mismatched is not None:
            val_start, val_end, replacement = mismatched
            if replacement is not None:
              out.append(text[emit_idx:val_start])
              out.append(replacement)
              emit_idx = val_end
            scan_idx = val_end
            continue
          scan_idx = start + 2
          continue
        raw_inner = text[start + 2 : close_start]
        closing_delim = text[close_start:end_tok]
        conn_m = _QUOTED_CONN_RE.match(text, end_tok)
        if conn_m is not None:
          (
              decoded_key,
              emitted_key,
              val_start,
              nl_count,
              is_sensitive_outer,
              in_json_object,
          ) = _inspect_quoted_kv_token(
              text,
              start,
              end_tok,
              conn_m,
              quote_kind='\\"',
              depth=_depth,
              close_start=close_start,
              lead_bs=lead_bs,
          )
          key_changed = emitted_key != text[start:end_tok]
          if is_sensitive_outer:
            redacted = _try_redact_sensitive_kv(
                text,
                decoded_key,
                conn_m.group(0),
                val_start,
                nl_count,
                is_quoted_key=True,
                in_json_object=in_json_object,
            )
            if redacted is None:
              if key_changed:
                out.append(
                    text[emit_idx:start]
                    + emitted_key
                    + text[end_tok : conn_m.end()]
                )
                emit_idx = conn_m.end()
              scan_idx = conn_m.end()
            else:
              val_end, replacement = redacted
              if replacement is None:
                if key_changed:
                  out.append(
                      text[emit_idx:start] + emitted_key + text[end_tok:val_end]
                  )
                  emit_idx = val_end
              else:
                if key_changed:
                  out.append(
                      text[emit_idx:start]
                      + emitted_key
                      + text[end_tok:val_start]
                  )
                else:
                  out.append(text[emit_idx:val_start])
                out.append(replacement)
                emit_idx = val_end
              scan_idx = val_end
            continue
          if key_changed:
            out.append(
                text[emit_idx:start]
                + emitted_key
                + text[end_tok : conn_m.end()]
            )
            emit_idx = conn_m.end()
          scan_idx = conn_m.end()
          continue
        if "\\" in raw_inner or "'" in raw_inner:
          mismatched = _try_mismatched_kv_redaction(
              text,
              start - lead_bs + 1,
              start + 2,
              decode_escaped=True,
          )
          if mismatched is not None:
            val_start, val_end, replacement = mismatched
            outer_close_start = close_start
            outer_is_balanced_close = (
                val_start < outer_close_start
                and (end_tok >= n or text[end_tok] in " \t\r\n,;)}]")
                and not _quote_opens_kv_at(text, end_tok - 2, escaped_dq=True)
            )
            if outer_is_balanced_close:
              val_end = min(val_end, outer_close_start)
              if replacement is not None:
                out.append(text[emit_idx:val_start])
                out.append(replacement)
                if val_end < outer_close_start:
                  tail = text[val_end:outer_close_start]
                  out.append(
                      _scrub_quoted_inner(
                          tail, quote_kind='\\"', depth=_depth, lead_bs=lead_bs
                      )
                  )
                out.append(text[outer_close_start:end_tok])
                emit_idx = end_tok
              elif val_end < outer_close_start:
                tail = text[val_end:outer_close_start]
                scrubbed_tail = _scrub_quoted_inner(
                    tail, quote_kind='\\"', depth=_depth, lead_bs=lead_bs
                )
                if scrubbed_tail != tail:
                  out.append(text[emit_idx:val_end])
                  out.append(scrubbed_tail)
                  out.append(text[outer_close_start:end_tok])
                  emit_idx = end_tok
              scan_idx = end_tok
              continue
            if replacement is not None:
              out.append(text[emit_idx:val_start])
              out.append(replacement)
              emit_idx = val_end
            scan_idx = val_end
            continue
        if _quote_opens_kv_at(text, end_tok - 2, escaped_dq=True):
          scan_idx = start + 2
          continue
        if _inner_ends_with_kv_connector(raw_inner):
          after_ws = end_tok
          while after_ws < n and text[after_ws] in (" ", "\t"):
            after_ws += 1
          if (
              after_ws < n
              and text[after_ws] not in ',;)}]\r\n"'
              and not (
                  after_ws > end_tok
                  and _BARE_KV_RE.match(text, after_ws) is not None
              )
          ):
            scan_idx = start + 2
            continue
        scrubbed = _scrub_quoted_inner(
            raw_inner, quote_kind='\\"', depth=_depth, lead_bs=lead_bs
        )
        if scrubbed != raw_inner:
          out.append(text[emit_idx:start] + '\\"' + scrubbed + closing_delim)
          emit_idx = end_tok
        scan_idx = end_tok
        continue

      # Branch C: plain double- or single-quoted token "..." / '...'.
      if text[start] in ('"', "'"):
        ch = text[start]
        if ch == "'" and start > 0 and text[start - 1].isalnum():
          scan_idx = start + 1
          continue
        end_tok = _scan_same_line_quote_end(text, start)
        if end_tok is None:
          mismatched = _try_mismatched_kv_redaction(
              text, start, start + 1, decode_escaped=False
          )
          if mismatched is not None:
            val_start, val_end, replacement = mismatched
            if replacement is not None:
              out.append(text[emit_idx:val_start])
              out.append(replacement)
              emit_idx = val_end
            scan_idx = val_end
            continue
          scan_idx = start + 1
          continue
        raw_inner = text[start + 1 : end_tok - 1]
        conn_m = _QUOTED_CONN_RE.match(text, end_tok)
        if conn_m is not None:
          (
              decoded_key,
              emitted_key,
              val_start,
              nl_count,
              is_sensitive_outer,
              in_json_object,
          ) = _inspect_quoted_kv_token(
              text, start, end_tok, conn_m, quote_kind=ch, depth=_depth
          )
          key_changed = emitted_key != text[start:end_tok]
          if is_sensitive_outer:
            redacted = _try_redact_sensitive_kv(
                text,
                decoded_key,
                conn_m.group(0),
                val_start,
                nl_count,
                is_quoted_key=True,
                in_json_object=in_json_object,
            )
            if redacted is None:
              if key_changed:
                out.append(
                    text[emit_idx:start]
                    + emitted_key
                    + text[end_tok : conn_m.end()]
                )
                emit_idx = conn_m.end()
              scan_idx = conn_m.end()
            else:
              val_end, replacement = redacted
              if replacement is None:
                if key_changed:
                  out.append(
                      text[emit_idx:start] + emitted_key + text[end_tok:val_end]
                  )
                  emit_idx = val_end
              else:
                if key_changed:
                  out.append(
                      text[emit_idx:start]
                      + emitted_key
                      + text[end_tok:val_start]
                  )
                else:
                  out.append(text[emit_idx:val_start])
                out.append(replacement)
                emit_idx = val_end
              scan_idx = val_end
            continue
          if key_changed:
            out.append(
                text[emit_idx:start]
                + emitted_key
                + text[end_tok : conn_m.end()]
            )
            emit_idx = conn_m.end()
          scan_idx = conn_m.end()
          continue
        if "\\" in raw_inner or ("'" if ch == '"' else '"') in raw_inner:
          mismatched = _try_mismatched_kv_redaction(
              text,
              start,
              start + 1,
              decode_escaped=(ch == '"'),
          )
          if mismatched is not None:
            val_start, val_end, replacement = mismatched
            outer_close_start = end_tok - 1
            outer_is_balanced_close = (
                val_start < outer_close_start
                and (end_tok >= n or text[end_tok] in " \t\r\n,;)}]")
                and not _quote_opens_kv_at(
                    text, outer_close_start, escaped_dq=False
                )
            )
            if outer_is_balanced_close:
              val_end = min(val_end, outer_close_start)
              if replacement is not None:
                out.append(text[emit_idx:val_start])
                out.append(replacement)
                if val_end < outer_close_start:
                  tail = text[val_end:outer_close_start]
                  out.append(
                      _scrub_quoted_inner(tail, quote_kind=ch, depth=_depth)
                  )
                out.append(text[outer_close_start:end_tok])
                emit_idx = end_tok
              elif val_end < outer_close_start:
                tail = text[val_end:outer_close_start]
                scrubbed_tail = _scrub_quoted_inner(
                    tail, quote_kind=ch, depth=_depth
                )
                if scrubbed_tail != tail:
                  out.append(text[emit_idx:val_end])
                  out.append(scrubbed_tail)
                  out.append(text[outer_close_start:end_tok])
                  emit_idx = end_tok
              scan_idx = end_tok
              continue
            if replacement is not None:
              out.append(text[emit_idx:val_start])
              out.append(replacement)
              emit_idx = val_end
            scan_idx = val_end
            continue
        if _quote_opens_kv_at(text, end_tok - 1, escaped_dq=False):
          scan_idx = start + 1
          continue
        if _inner_ends_with_kv_connector(raw_inner):
          after_ws = end_tok
          while after_ws < n and text[after_ws] in (" ", "\t"):
            after_ws += 1
          if (
              after_ws < n
              and text[after_ws] not in ",;)}]\r\n"
              and not (
                  after_ws > end_tok
                  and _BARE_KV_RE.match(text, after_ws) is not None
              )
          ):
            scan_idx = start + 1
            continue
        scrubbed = _scrub_quoted_inner(raw_inner, quote_kind=ch, depth=_depth)
        if scrubbed != raw_inner:
          out.append(text[emit_idx:start] + ch + scrubbed + ch)
          emit_idx = end_tok
        scan_idx = end_tok
        continue

    # Branch D: bare or slash/colon-chained KV match.
    match_end = match.end()
    if (
        start >= 2
        and text[start - 2] == "\\"
        and text[start - 1] in ("t", "r", "n")
        and _count_backslashes_ending_at(text, start - 2) % 2 == 0
    ):
      scan_idx = match_end
      continue
    matched_key, conn_text, val_start, nl_count, match_end = (
        _resolve_bare_or_path_kv(text, match)
    )
    if matched_key is None:
      if match.group("conn") == ":" and (
          text.startswith("//", match_end)
          or text.startswith("\\/\\/", match_end)
      ):
        url_pos, auth_redact_span, stopped_at_kv_seg = (
            _scan_url_authority_and_safe_path(text, match_end)
        )
        if auth_redact_span is not None:
          pass_start, pass_end = auth_redact_span
          out.append(text[emit_idx:pass_start])
          out.append("[REDACTED]")
          emit_idx = pass_end
        if stopped_at_kv_seg:
          seg_m = _UNANCHORED_BARE_KV_RE.match(text, url_pos)
          if seg_m is not None:
            seg_key, seg_conn, seg_val_start, seg_nl_count, seg_match_end = (
                _resolve_bare_or_path_kv(text, seg_m)
            )
            if seg_key is not None:
              seg_redacted = _try_redact_sensitive_kv(
                  text,
                  seg_key,
                  seg_conn,
                  seg_val_start,
                  seg_nl_count,
                  is_quoted_key=False,
              )
              if seg_redacted is not None:
                seg_val_end, seg_replacement = seg_redacted
                if seg_replacement is not None:
                  out.append(text[emit_idx:seg_val_start])
                  out.append(seg_replacement)
                  emit_idx = seg_val_end
                scan_idx = seg_val_end
                continue
            scan_idx = max(match_end, seg_match_end)
            continue
        scan_idx = max(match_end, url_pos)
      else:
        scan_idx = match_end
      continue
    redacted = _try_redact_sensitive_kv(
        text,
        matched_key,
        conn_text,
        val_start,
        nl_count,
        is_quoted_key=False,
    )
    if redacted is None:
      scan_idx = match_end
      continue
    val_end, replacement = redacted
    if replacement is not None:
      out.append(text[emit_idx:val_start])
      out.append(replacement)
      emit_idx = val_end
    scan_idx = val_end
  if not out:
    return text
  out.append(text[emit_idx:])
  return "".join(out)


def _scrub_text_at_depth(text: str, *, _depth: int = 0) -> str:
  """Redacts embedded JSON, secret patterns, and KV pairs at bounded depth."""
  out = (
      _scrub_embedded_json_containers(text)
      if _depth < 3 and ("{" in text or "[" in text)
      else text
  )
  for pat, repl in _SECRET_RULES:
    out = pat.sub(repl, out)
  out = _scrub_sensitive_kv_in_text(out, _depth=_depth)
  if _depth == 0 and "\\u00" in out:
    decoded_ascii = _unescape_safe_ascii_unicode_escapes(out)
    if decoded_ascii != out:
      rescrubbed = _scrub_sensitive_kv_in_text(decoded_ascii, _depth=1)
      if rescrubbed != decoded_ascii:
        return rescrubbed
  return out


def scrub_sensitive_text(text: str) -> str:
  """Redacts credential tokens from raw strings while keeping error codes."""
  return _scrub_text_at_depth(str(text), _depth=0)


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
    if not is_container and (
        _is_numeric_count_scalar(key_name, value)
        or _is_safe_auth_status_scalar(key_name, value)
    ):
      if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
      return value
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
  if (
      is_sensitive_key(key_name)
      and str(val).strip()
      and not (
          _is_numeric_count_scalar(key_name, val)
          or _is_safe_auth_status_scalar(key_name, val)
      )
  ):
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
