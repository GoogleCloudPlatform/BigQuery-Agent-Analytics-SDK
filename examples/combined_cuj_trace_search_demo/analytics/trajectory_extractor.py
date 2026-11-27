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
import contextvars
import functools
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, NamedTuple

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
_SAFE_CONTAINER_METADATA_KEYS = frozenset(
    {
        "client_id",
        "http_method",
        "httpmethod",
        "http_status",
        "httpstatus",
        "region",
        "zone",
        "trace_id",
        "span_id",
        "request_id",
        "correlation_id",
        "project_id",
        "environment",
        "env",
        "service_name",
        "api_version",
        "sdk_version",
    }
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
_HTTP_REQUEST_METHODS = frozenset(
    {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
        "TRACE",
        "CONNECT",
    }
)
_PY_STR_PREFIX_RE = re.compile(r"""[rRuUbBfF]{1,2}(?=["'])""")
_CTOR_PREFIX_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?=\()")
_QUERY_PARAM_AFTER_AMP_RE = re.compile(r"&(?P<param>[A-Za-z_][A-Za-z0-9_.-]*)=")
_URL_DELIM_KV_RE = re.compile(r"[&,;][A-Za-z_][A-Za-z0-9_.-]*=")
_URL_STOP_CHARS = frozenset("/?#&|<>*\"',;}])")
_URL_AUTH_HARD_STOP_CHARS = frozenset('/?#"<>|{}[]')
_COMMON_URL_HOST_TLDS = frozenset(
    {
        "com",
        "org",
        "net",
        "edu",
        "gov",
        "io",
        "co",
        "dev",
        "app",
        "ai",
        "cloud",
        "local",
        "internal",
        "corp",
        "lan",
        "home",
        "test",
        "example",
        "invalid",
    }
)
_URL_EMAIL_PARAM_KEYS = frozenset(
    {
        "email",
        "mail",
        "user_email",
        "contact",
        "recipient",
        "sender",
        "to",
        "from",
        "cc",
        "bcc",
        "reply_to",
    }
)
_URL_EMAIL_OR_REDIRECT_PARAM_KEYS = _URL_EMAIL_PARAM_KEYS | frozenset(
    {
        "next",
        "redirect",
        "callback",
        "return_to",
        "continue",
    }
)
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
_BARE_END_SCAN_RE = re.compile(r"[^\s,;)}\]\"'\\&]+")
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
_NON_CONTAINER_VERSION_SEG_RE = re.compile(r"(?i)^v\d+$")
_AUTH_STATUS_CONTAINER_TOKS = (
    "auth",
    "oauth",
    "credential",
)
_SENSITIVE_CONTAINER_CTX_TOKS = (
    "auth",
    "secret",
    "credential",
    "token",
    "key",
)
_SENSITIVE_CHAIN_CTX_TOKS = (
    *_SENSITIVE_CONTAINER_CTX_TOKS,
    "pass",
    "pwd",
    "jwt",
    "dsn",
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


def _is_valid_colon_path_segment(seg: str) -> bool:
  """Returns True when ``seg`` is a sensitive container or known namespace."""
  if not seg:
    return False
  if "/" in seg or "." in seg:
    if _classify_colon_namespace_seg(seg, _normalize_key_name(seg))[0]:
      return True
    parts = [p.strip() for p in seg.split("/" if "/" in seg else ".")]
    return bool(parts) and all(_is_valid_colon_path_segment(p) for p in parts)
  if is_sensitive_key(seg, is_container=True) or _extract_container_colon_head(
      seg
  ):
    return True
  return _classify_colon_namespace_seg(seg, _normalize_key_name(seg))[0]


@functools.lru_cache(maxsize=2048)
def _has_unknown_path_segment_cached(path: str) -> bool:
  """Cached check for unknown segments in ``/`` or ``.``-delimited paths."""
  return any(
      not _is_valid_colon_path_segment(part)
      for raw_part in _KEY_SEGMENT_SEP_RE.split(path.strip())
      if (part := raw_part.strip())
  )


def _has_unknown_path_segment(path: str) -> bool:
  """Returns True when a ``/`` or ``.``-delimited path has an unknown seg."""
  if "/" not in path and "." not in path:
    return False
  if len(path) <= 128:
    return _has_unknown_path_segment_cached(path)
  return any(
      not _is_valid_colon_path_segment(part)
      for raw_part in _KEY_SEGMENT_SEP_RE.split(path.strip())
      if (part := raw_part.strip())
  )


@functools.lru_cache(maxsize=2048)
def _extract_valid_scalar_path_leaf_norm_cached(key_name: str) -> str:
  """Cached implementation of _extract_valid_scalar_path_leaf_norm."""
  colon_segs = [
      s for raw_s in key_name.strip().split(":") if (s := raw_s.strip())
  ]
  if not colon_segs:
    return ""
  if any(not _is_valid_colon_path_segment(seg) for seg in colon_segs[:-1]):
    return ""
  leaf_seg = colon_segs[-1]
  if "/" in leaf_seg or "." in leaf_seg or any(ch.isspace() for ch in leaf_seg):
    parts = [
        p
        for raw_p in _KEY_SEGMENT_SEP_RE.split(leaf_seg)
        if (p := raw_p.strip())
    ]
    if not parts:
      return ""
    if any(not _is_valid_colon_path_segment(part) for part in parts[:-1]):
      return ""
    return _normalize_key_name(parts[-1])
  return _normalize_key_name(leaf_seg)


def _extract_valid_scalar_path_leaf_norm(key_name: str) -> str:
  """Returns normalized leaf key if all path prefixes are safe or known."""
  if isinstance(key_name, str) and len(key_name) <= 128:
    return _extract_valid_scalar_path_leaf_norm_cached(key_name)
  return _extract_valid_scalar_path_leaf_norm_cached.__wrapped__(key_name)


def _is_numeric_count_scalar(key_name: str, value: Any) -> bool:
  """Returns True for small non-negative integer counts on allowlisted keys."""
  if not key_name:
    return False
  if isinstance(value, bool):
    return False
  if isinstance(value, int):
    if not (0 <= value <= _MAX_SAFE_COUNT_VALUE):
      return False
  else:
    if isinstance(value, (bytes, bytearray)):
      text_val = value.decode("utf-8", errors="replace").strip()
    elif isinstance(value, str):
      text_val = value.strip()
    else:
      return False
    if not (
        bool(text_val)
        and len(text_val) <= 3
        and text_val.isascii()
        and text_val.isdigit()
        and not (len(text_val) > 1 and text_val.startswith("0"))
        and int(text_val) <= _MAX_SAFE_COUNT_VALUE
    ):
      return False
  norm = _normalize_key_name(key_name)
  if norm in _SAFE_SMALL_COUNT_KEYS:
    return True
  if isinstance(key_name, str) and any(
      sep in key_name for sep in (":", "/", ".")
  ):
    leaf_norm = _extract_valid_scalar_path_leaf_norm(key_name)
    return leaf_norm in _SAFE_SMALL_COUNT_KEYS
  return False


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
  if text_val.strip().lower() not in _SAFE_AUTH_STATUS_VALUES:
    return False
  norm = _normalize_key_name(key_name)
  if norm in (
      "authentication",
      "authentication_result",
  ) or norm.endswith(("_authentication", "_authentication_result")):
    return True
  if isinstance(key_name, str) and any(
      sep in key_name for sep in (":", "/", ".")
  ):
    leaf_norm = _extract_valid_scalar_path_leaf_norm(key_name)
    return leaf_norm in ("status", "reason", "result", "state")
  return False


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
  if not is_url_val:
    m = _BARE_END_SCAN_RE.match(text, pos)
    if m is not None:
      end_p = m.end()
      if (
          end_p >= n
          or text[end_p].isspace()
          or text[end_p] in _BARE_VAL_STOP_CHARS
      ):
        return end_p
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


def _find_first_escaped_nl(text: str, start: int, end_limit: int) -> int:
  """Returns backslash index of the first odd-backslash \\r or \\n, or -1."""
  idx = start
  while idx < end_limit:
    bs_start = text.find("\\", idx, end_limit)
    if bs_start == -1:
      return -1
    bs_end = bs_start + 1
    while bs_end < end_limit and text[bs_end] == "\\":
      bs_end += 1
    if (
        (bs_end - bs_start) % 2 == 1
        and bs_end < end_limit
        and text[bs_end] in ("n", "r")
    ):
      return bs_end - 1
    idx = bs_end + 1
  return -1


def _is_trailing_error_boundary_tail(
    text: str,
    pos: int,
    end_limit: int,
    *,
    include_escaped_nl: bool = False,
) -> bool:
  """Returns True if ``text[pos:end_limit]`` has only whitespace/closers."""
  p = pos
  while p < end_limit:
    ch = text[p]
    if ch in (" ", "\t", "\r", "\n", "}", "]", '"', "'"):
      p += 1
      continue
    if include_escaped_nl and ch == "\\":
      bs = 0
      while p + bs < end_limit and text[p + bs] == "\\":
        bs += 1
      if (
          bs % 2 == 1
          and p + bs < end_limit
          and text[p + bs] in ("n", "r", "t", '"', "'")
      ):
        p += bs + 1
        continue
    return False
  return True


_SAFE_ERROR_LINE_PREFIX_LABELS = frozenset(
    {
        "error",
        "fatal",
        "warn",
        "warning",
        "status",
        "code",
        "reason",
        "err",
        "error_code",
        "status_code",
    }
)


def _skip_error_prefix_ws_and_quotes(
    text: str,
    pos: int,
    end: int,
    *,
    include_escaped_nl: bool,
) -> int:
  """Skips spaces, tabs, optional quotes, and escaped quote/tab tokens."""
  p = pos
  while p < end:
    ch = text[p]
    if ch in (" ", "\t", '"', "'"):
      p += 1
      continue
    if ch == "\\":
      bs = 0
      while p + bs < end and text[p + bs] == "\\":
        bs += 1
      if bs % 2 == 1 and p + bs < end:
        nxt = text[p + bs]
        if nxt in ('"', "'") or (include_escaped_nl and nxt == "t"):
          p += bs + 1
          continue
    break
  return p


def _is_safe_error_line_prefix(
    text: str,
    prefix_start: int,
    err_start: int,
    *,
    include_escaped_nl: bool,
) -> bool:
  """Returns True if ``text[prefix_start:err_start]`` has no payload text."""
  p = _skip_error_prefix_ws_and_quotes(
      text, prefix_start, err_start, include_escaped_nl=include_escaped_nl
  )
  if p == err_start:
    return True
  label_start = p
  while (
      p < err_start
      and text[p].isascii()
      and (text[p].isalpha() or text[p] == "_")
  ):
    p += 1
  if text[label_start:p].lower() not in _SAFE_ERROR_LINE_PREFIX_LABELS:
    return False
  p = _skip_error_prefix_ws_and_quotes(
      text, p, err_start, include_escaped_nl=include_escaped_nl
  )
  if p >= err_start or text[p] not in (":", "="):
    return False
  p = _skip_error_prefix_ws_and_quotes(
      text, p + 1, err_start, include_escaped_nl=include_escaped_nl
  )
  return p == err_start


def _unterminated_quote_error_stop(
    text: str,
    start: int,
    end_limit: int,
    *,
    include_escaped_nl: bool = False,
) -> int:
  """Stops an unterminated quote before a trailing-line error marker."""
  if text.find("-----BEGIN ", start, end_limit) != -1:
    return end_limit
  best = end_limit
  nl_pos = text.find("\n", start, end_limit)
  cr_pos = text.find("\r", start, end_limit)
  if nl_pos == -1:
    first_nl = cr_pos
  elif cr_pos == -1:
    first_nl = nl_pos
  else:
    first_nl = min(nl_pos, cr_pos)
  if first_nl != -1:
    err_m = _search_valid_critical_error_marker(text, first_nl + 1, end_limit)
    if err_m is not None:
      last_nl = text.rfind("\n", first_nl, err_m.start())
      last_cr = text.rfind("\r", first_nl, err_m.start())
      cut = max(last_nl, last_cr)
      prefix_start = cut + 1
      if cut > start and text[cut] == "\n" and text[cut - 1] == "\r":
        cut -= 1
      if _is_safe_error_line_prefix(
          text,
          prefix_start,
          err_m.start(),
          include_escaped_nl=include_escaped_nl,
      ) and _is_trailing_error_boundary_tail(
          text,
          err_m.end(),
          end_limit,
          include_escaped_nl=include_escaped_nl,
      ):
        best = cut
  if include_escaped_nl:
    first_esc_nl = _find_first_escaped_nl(text, start, best)
    if first_esc_nl != -1:
      err_m = _search_valid_critical_error_marker(text, first_esc_nl + 2, best)
      if err_m is not None:
        err_start = err_m.start()
        last_esc_nl = first_esc_nl
        idx = first_esc_nl + 2
        while idx < err_start:
          nxt = _find_first_escaped_nl(text, idx, err_start)
          if nxt == -1:
            break
          last_esc_nl = nxt
          idx = nxt + 2
        orig_last_esc_nl = last_esc_nl
        prefix_start = orig_last_esc_nl + 2
        if (
            last_esc_nl >= start + 2
            and text[last_esc_nl + 1] == "n"
            and text[last_esc_nl - 1] == "r"
            and text[last_esc_nl - 2] == "\\"
            and _count_backslashes_ending_at(text, last_esc_nl - 2) % 2 == 1
        ):
          last_esc_nl -= 2
        if _is_safe_error_line_prefix(
            text, prefix_start, err_start, include_escaped_nl=True
        ) and _is_trailing_error_boundary_tail(
            text, err_m.end(), best, include_escaped_nl=True
        ):
          return last_esc_nl
  return best


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
    # Unterminated or oversized quoted value: fail closed to end of string
    # while preserving trailing-line critical error markers.
    return (
        _unterminated_quote_error_stop(
            text, pos + 1, n, include_escaped_nl=True
        ),
        "[REDACTED]",
    )
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
    nl_bs = max(1, low_zero // 2)
    delim = "\\" * lead_bs + q
    limit = min(n, pos + _MAX_EMBEDDED_JSON_SPAN)
    cursor = pos + lead_bs + 1
    while cursor < limit:
      if text[cursor] == '"' and q == '"':
        return (
            _unterminated_quote_error_stop(
                text, pos + lead_bs + 1, cursor, include_escaped_nl=True
            ),
            "[REDACTED]",
        )
      if (
          q == "'"
          and text[cursor] == "'"
          and not (cursor + 1 < n and text[cursor + 1].isalpha())
          and _is_escaped_json_close_tail(text, cursor + 1, n, nl_bs=nl_bs)
      ):
        return cursor + 1, "[REDACTED]"
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
            return (
                _unterminated_quote_error_stop(
                    text,
                    pos + lead_bs + 1,
                    cursor + bs,
                    include_escaped_nl=True,
                ),
                "[REDACTED]",
            )
          cursor += bs + 1
          continue
        cursor += max(1, bs)
        continue
      cursor += 1
    if limit == n:
      return (
          _unterminated_quote_error_stop(
              text, pos + lead_bs + 1, n, include_escaped_nl=True
          ),
          "[REDACTED]",
      )
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
  if "/" in bare and text[match.start() : match.start("bare")].startswith("/"):
    prev = _preceding_same_line_word(text, match.start())
    if prev is not None and prev[0].upper() in _HTTP_REQUEST_METHODS:
      tail_seg = bare.rstrip("/").rsplit("/", 1)[-1]
      return (
          bare
          if is_sensitive_key(tail_seg, is_container=is_container)
          else None
      )
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
  bs = 0
  k = pos
  while k >= 0 and text[k] == "\\":
    bs += 1
    k -= 1
  return bs


def _stripped_inner_end(raw_inner: str) -> int:
  """Returns the index in ``raw_inner`` after trailing spaces and escapes."""
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
  return i


def _inner_ends_with_kv_connector(raw_inner: str) -> bool:
  """Returns True when ``raw_inner`` ends with a KV connector before a quote."""
  i = _stripped_inner_end(raw_inner)
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


def _inner_ends_with_member_sep(raw_inner: str) -> bool:
  """Returns True when ``raw_inner`` ends with a comma or closing delimiter."""
  i = _stripped_inner_end(raw_inner)
  return i > 0 and raw_inner[i - 1] in (",", "}", "]")


def _can_try_mismatched_quote_before(
    text: str,
    raw_inner: str,
    after_quote_pos: int,
    quote_open_pos: int,
    *,
    escaped_dq: bool,
    allow_error_marker_tail: bool = False,
) -> bool:
  """Returns True when text before a quote warrants a mismatched-quote scan."""
  if not _has_same_line_value_after(text, after_quote_pos):
    return (
        allow_error_marker_tail
        and _CRITICAL_ERROR_MARKERS_RE.search(raw_inner) is not None
    )
  if _inner_ends_with_kv_connector(raw_inner):
    return True
  return _inner_ends_with_member_sep(raw_inner) and _quote_opens_kv_at(
      text, quote_open_pos, escaped_dq=escaped_dq
  )


_QUOTE_KV_FAIL_UNTIL: contextvars.ContextVar[
    dict[tuple[bool, int, str], int] | None
] = contextvars.ContextVar("_QUOTE_KV_FAIL_UNTIL", default=None)

_JSON_OBJ_CLOSE_CACHE: contextvars.ContextVar[
    dict[tuple[bool, int], dict[int, int]] | None
] = contextvars.ContextVar("_JSON_OBJ_CLOSE_CACHE", default=None)
_ENCLOSING_CONT_MEMO: contextvars.ContextVar[
    dict[tuple[bool, int, str, int, bool], tuple[bool, bool]] | None
] = contextvars.ContextVar("_ENCLOSING_CONT_MEMO", default=None)
_UNQUOTED_SENS_HDR_CACHE: contextvars.ContextVar[dict[int, bool] | None] = (
    contextvars.ContextVar("_UNQUOTED_SENS_HDR_CACHE", default=None)
)


def _quote_opens_kv_at(text: str, pos: int, *, escaped_dq: bool) -> bool:
  """Returns True when a quote token at ``pos`` opens a quoted KV key."""
  n = len(text)
  if pos < 0 or pos >= n:
    return False
  limit = n
  fail_cache = _QUOTE_KV_FAIL_UNTIL.get()
  if escaped_dq:
    if not text.startswith('\\"', pos):
      return False
    lead_bs = _count_backslashes_ending_at(text, pos)
    if lead_bs % 2 == 0:
      return False
    close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
    fail_key = (True, close_mod, '"')
    if fail_cache is not None and pos < fail_cache.get(fail_key, -1):
      return False

    def _edq_kv_matches(
        inner: str, next_end: int, *, require_sensitive: bool = False
    ) -> bool:
      if (
          not inner
          or not inner.strip()
          or inner[0] in ";,)}]"
          or ";" in inner
          or inner.rstrip().endswith("=")
      ):
        return False
      if (
          not inner[0].isspace()
          and not require_sensitive
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

    first_sq_pos = -1
    p = pos + 2
    while p < limit:
      ch = text[p]
      if ch in ("\r", "\n"):
        break
      if ch == '"':
        return _edq_kv_matches(text[pos + 2 : p], p + 1)
      if ch == "'":
        if first_sq_pos == -1:
          if _edq_kv_matches(text[pos + 2 : p], p + 1, require_sensitive=True):
            return True
          first_sq_pos = p
        p += 1
        continue
      if ch == "\\":
        bs = 0
        while p + bs < limit and text[p + bs] == "\\":
          bs += 1
        if p + bs < limit and text[p + bs] == '"':
          if bs % period == close_mod or ((bs + 1) & -(bs + 1)) < low_zero:
            return _edq_kv_matches(text[pos + 2 : p], p + bs + 1)
          p += bs + 1
        elif p + bs < limit and text[p + bs] == "'":
          if first_sq_pos == -1:
            sq_key_end = p if bs % 2 == 1 else p + bs
            if _edq_kv_matches(
                text[pos + 2 : sq_key_end], p + bs + 1, require_sensitive=True
            ):
              return True
            first_sq_pos = p + bs
          p += bs + 1
          continue
        else:
          p += bs
      else:
        p += 1
    if fail_cache is not None:
      fail_stop = first_sq_pos if first_sq_pos != -1 else p
      if fail_stop > fail_cache.get(fail_key, -1):
        fail_cache[fail_key] = fail_stop
    return False
  quote_char = text[pos]
  if quote_char not in ('"', "'"):
    return False
  fail_key = (False, 0, quote_char)
  if fail_cache is not None and pos < fail_cache.get(fail_key, -1):
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
          or not inner.strip()
          or inner[0] in ";,)}]"
          or ";" in inner
          or inner.rstrip().endswith("=")
      ):
        return False
      next_end = p + 1
      if (
          not inner[0].isspace()
          and _QUOTED_STRUCTURAL_CONN_RE.match(text, next_end) is not None
      ):
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
      break
    else:
      p += 1
  if fail_cache is not None and p > fail_cache.get(fail_key, -1):
    fail_cache[fail_key] = p
  return False


def _is_json_colon_connector(conn_text: str) -> bool:
  """Returns True when ``conn_text`` is a structural JSON-style colon."""
  return _QUOTED_STRUCTURAL_CONN_RE.fullmatch(
      conn_text
  ) is not None and conn_text.endswith(":")


def _preceded_by_colon(text: str, idx: int) -> bool:
  """Returns True when skipping whitespace/escaped whitespace backward reaches ``:``."""
  while idx >= 0:
    ch = text[idx]
    if ch in (" ", "\t", "\r", "\n"):
      idx -= 1
    elif (
        idx >= 1
        and ch in ("t", "r", "n")
        and text[idx - 1] == "\\"
        and _count_backslashes_ending_at(text, idx - 1) % 2 == 1
    ):
      idx -= 2
    else:
      break
  return idx >= 0 and text[idx] == ":"


def _inspect_json_context_before(
    text: str, quote_start: int
) -> tuple[bool, bool, bool]:
  """Inspects context before ``quote_start`` for JSON object & line boundaries.

  Returns ``(in_json_object, saw_newline, at_start)``. Skips an even backslash
  run directly preceding ``quote_start`` (e.g. ``{\\"password": ...}``) and
  requires a preceding ``,`` to follow a JSON member value rather than prose.
  """
  prev_idx = quote_start - 1
  if prev_idx >= 0 and text[prev_idx] == "\\":
    bs = _count_backslashes_ending_at(text, prev_idx)
    if bs % 2 == 0:
      prev_idx -= bs
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
  if prev_idx < 0:
    return False, saw_newline, True
  if text[prev_idx] == "{":
    return True, saw_newline, False
  if text[prev_idx] != ",":
    return False, saw_newline, False
  before_comma = prev_idx - 1
  while before_comma >= 0:
    ch = text[before_comma]
    if ch in (" ", "\t", "\r", "\n"):
      before_comma -= 1
    elif (
        before_comma >= 1
        and ch in ("t", "r", "n")
        and text[before_comma - 1] == "\\"
        and _count_backslashes_ending_at(text, before_comma - 1) % 2 == 1
    ):
      before_comma -= 2
    else:
      break
  if before_comma < 0:
    return False, saw_newline, False
  bc = text[before_comma]
  if bc in ("}", "]"):
    return True, saw_newline, False
  if bc in ('"', "'"):
    bs_close = (
        _count_backslashes_ending_at(text, before_comma - 1)
        if before_comma > 0
        else 0
    )
    scan_min = max(0, before_comma - 256)
    if bc == '"' and bs_close % 2 == 1:
      close_low_zero = (bs_close + 1) & -(bs_close + 1)
      scan = before_comma - bs_close - 1
      while scan >= scan_min:
        if text[scan] == '"':
          bs_open = (
              _count_backslashes_ending_at(text, scan - 1) if scan > 0 else 0
          )
          if ((bs_open + 1) & -(bs_open + 1)) == close_low_zero:
            return (
                _preceded_by_colon(text, scan - bs_open - 1),
                saw_newline,
                False,
            )
          scan = scan - bs_open - 1
        else:
          scan -= 1
      return True, saw_newline, False
    scan = before_comma - 1
    while scan >= scan_min:
      if text[scan] == bc:
        bs_open = (
            _count_backslashes_ending_at(text, scan - 1) if scan > 0 else 0
        )
        if bs_open % 2 == 0:
          return (
              _preceded_by_colon(text, scan - bs_open - 1),
              saw_newline,
              False,
          )
        scan = scan - bs_open - 1
      else:
        scan -= 1
    return True, saw_newline, False
  if bc.isdigit():
    num_min = max(0, before_comma - 64)
    num_start = before_comma
    while num_start > num_min and text[num_start - 1] in "0123456789.eE+-":
      num_start -= 1
    if num_start > 0 and (
        text[num_start - 1].isalnum() or text[num_start - 1] == "_"
    ):
      return False, saw_newline, False
    return _preceded_by_colon(text, num_start - 1), saw_newline, False
  for lit in ("true", "false", "null"):
    lit_len = len(lit)
    lit_start = before_comma + 1 - lit_len
    if (
        lit_start >= 0
        and text[lit_start : before_comma + 1] == lit
        and (
            lit_start == 0
            or not (text[lit_start - 1].isalnum() or text[lit_start - 1] == "_")
        )
    ):
      return _preceded_by_colon(text, lit_start - 1), saw_newline, False
  return False, saw_newline, False


def _is_in_json_object_before(text: str, quote_start: int) -> bool:
  """Returns True when ``quote_start`` opens a JSON object member."""
  in_json_object, _, _ = _inspect_json_context_before(text, quote_start)
  return in_json_object


def _scan_mismatched_quote_key_kv(
    text: str, inner_start: int
) -> tuple[int, int, re.Match[str]] | None:
  """Scans a same-line key up to the first quote delimiter and KV connector.

  Returns ``(close_end, close_start, conn_m)`` when a valid non-empty key ends
  at a quote (optionally preceded by an odd backslash run) followed immediately
  by a KV connector, else ``None``. Never scans past the first quote delimiter.
  """
  n = len(text)
  p = inner_start
  while p < n:
    ch = text[p]
    if ch in ("\r", "\n", ";"):
      return None
    if ch == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if p + bs < n and text[p + bs] in ('"', "'"):
        close_start = p if bs % 2 == 1 else p + bs
        close_end = p + bs + 1
        stripped = text[inner_start:close_start].strip()
        if (
            not stripped
            or stripped[0] in ",)}]"
            or stripped.rstrip().endswith("=")
        ):
          return None
        conn_m = _QUOTED_CONN_RE.match(text, close_end)
        return (close_end, close_start, conn_m) if conn_m is not None else None
      p += bs
      continue
    if ch in ('"', "'"):
      close_start = p
      close_end = p + 1
      stripped = text[inner_start:close_start].strip()
      if (
          not stripped
          or stripped[0] in ",)}]"
          or stripped.rstrip().endswith("=")
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
    quote_start = start - lead_bs + 1
  else:
    raw_inner = text[start + 1 : end_tok - 1]
    closing_delim = quote_kind
    decoded_key = (
        _decode_quoted_kv_key(text[start:end_tok])
        if quote_kind == '"'
        else raw_inner
    )
    quote_start = start
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
  in_json_object, saw_newline, at_start = _inspect_json_context_before(
      text, quote_start
  )
  at_key_boundary = at_start or saw_newline or in_json_object
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


def _is_canonical_colon_key_segment_impl(
    prev_seg: str,
    seg_bare: str,
    allow_suffix: bool,
    container_seg: str,
) -> bool:
  if not seg_bare:
    return False
  if "/" in seg_bare or "." in seg_bare:
    parts = [p for p in _KEY_SEGMENT_SEP_RE.split(seg_bare) if p]
    if len(parts) >= 2 and all(
        _is_namespace_colon_segment(p, allow_upper=p.isupper())
        for p in parts[:-1]
    ):
      return _is_canonical_colon_key_segment(
          parts[-2], parts[-1], allow_suffix=True, container_seg=container_seg
      )
    return False
  if any(ch.isdigit() for ch in seg_bare):
    seg_norm = _normalize_key_name(seg_bare)
    if seg_norm in _COLON_CHAIN_CONTAINER_KEYS and (
        seg_bare.islower() or seg_bare.isupper()
    ):
      return True
    if not allow_suffix or ("_" not in seg_bare and "-" not in seg_bare):
      return False
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
    cont_norm = _normalize_key_name(container_seg) if container_seg else ""
    if (
        prev_norm
        and _SENSITIVE_KEY_RE.fullmatch(f"{prev_norm}_{seg_norm}") is not None
    ) or (
        cont_norm
        and _SENSITIVE_KEY_RE.fullmatch(f"{cont_norm}_{seg_norm}") is not None
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


@functools.lru_cache(maxsize=2048)
def _is_canonical_colon_key_segment_cached(
    prev_seg: str,
    seg_bare: str,
    allow_suffix: bool,
    container_seg: str,
) -> bool:
  return _is_canonical_colon_key_segment_impl(
      prev_seg, seg_bare, allow_suffix, container_seg
  )


def _is_canonical_colon_key_segment(
    prev_seg: str,
    seg_bare: str,
    *,
    allow_suffix: bool = False,
    container_seg: str = "",
) -> bool:
  """Returns True when ``seg_bare`` is a canonical config key name (not a password)."""
  if (
      len(prev_seg) <= 128
      and len(seg_bare) <= 128
      and len(container_seg) <= 128
  ):
    return _is_canonical_colon_key_segment_cached(
        prev_seg, seg_bare, allow_suffix, container_seg
    )
  return _is_canonical_colon_key_segment_impl(
      prev_seg, seg_bare, allow_suffix, container_seg
  )


def _extract_container_colon_head_impl(bare: str) -> str:
  tail = bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
  if _normalize_key_name(tail) in _COLON_CHAIN_CONTAINER_KEYS:
    return tail
  if "/" in bare or "." in bare:
    segs = [s for s in _KEY_SEGMENT_SEP_RE.split(bare) if s]
    for idx in range(len(segs) - 1, -1, -1):
      seg = segs[idx]
      if _normalize_key_name(seg) in _COLON_CHAIN_CONTAINER_KEYS:
        return seg if idx < len(segs) - 1 else ""
      if is_sensitive_key(
          seg, is_container=False
      ) or not _is_namespace_colon_segment(seg, allow_upper=seg.isupper()):
        return ""
  return ""


@functools.lru_cache(maxsize=2048)
def _extract_container_colon_head_cached(bare: str) -> str:
  return _extract_container_colon_head_impl(bare)


def _extract_container_colon_head(bare: str) -> str:
  """Returns the active container key token in ``bare``, or empty string."""
  if isinstance(bare, str) and len(bare) <= 128:
    return _extract_container_colon_head_cached(bare)
  return _extract_container_colon_head_impl(bare)


def _has_eq_value(text: str, pos: int) -> bool:
  """Returns True when ``=`` at ``pos`` is followed by a non-empty value."""
  n = len(text)
  if pos >= n or text[pos] in ",;)}]=":
    return False
  if not text[pos].isspace() and text[pos] != "\\":
    return True
  val_start, nl_count = _skip_kv_whitespace(text, pos)
  if val_start >= n or text[val_start] in ",;)}]" or nl_count >= 2:
    return False
  if nl_count == 0:
    return True
  next_ch = text[val_start]
  if next_ch in ('"', "'", "{", "[", "(") or _starts_with_odd_escaped_quote(
      text, val_start
  ):
    return True
  if _is_sensitive_header_at(text, val_start, allow_mismatched_quote=False):
    return False
  return _BARE_KV_RE.match(text, val_start) is None


def _is_valid_tight_ident_path(
    seg: str,
    *,
    max_len: int = 64,
    require_alpha: bool = True,
) -> bool:
  """Returns True for a valid identifier or ``.``/``/``-delimited path seg."""
  if (
      not seg
      or len(seg) > max_len
      or seg.startswith((".", "/"))
      or seg.endswith((".", "/"))
  ):
    return False
  parts = re.split(r"[./]", seg)
  for part in parts:
    if (
        not part
        or not (part[0].isalnum() or part[0] == "_")
        or not all(c.isalnum() or c in "_-" for c in part)
    ):
      return False
  return (not require_alpha) or any(ch.isalpha() for ch in parts[-1])


def _is_explicit_container_eq_leaf(
    seg_bare: str,
    seg_conn: str,
    text: str,
    seg_end: int,
    prev_norm: str = "",
) -> bool:
  """Returns True for an explicit ``=`` leaf assignment under a container key."""
  del prev_norm
  if seg_conn.strip() != "=" or not _has_eq_value(text, seg_end):
    return False
  if not _is_valid_tight_ident_path(seg_bare, max_len=128):
    return False
  seg_norm = _normalize_key_name(seg_bare)
  if (
      not seg_norm
      or seg_norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
      or seg_norm in _SAFE_CONTAINER_METADATA_KEYS
      or seg_norm.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
  ):
    return False
  if is_sensitive_key(seg_bare, is_container=False):
    return True
  n = len(text)
  val_start, _ = _skip_kv_whitespace(text, seg_end)
  is_quoted_or_container_val = val_start < n and (
      text[val_start] in ('"', "'", "{", "[", "(")
      or _starts_with_odd_escaped_quote(text, val_start)
  )
  if (
      seg_conn == "="
      and seg_end < n
      and text[seg_end].isspace()
      and not is_quoted_or_container_val
  ):
    return False
  if _is_namespace_colon_segment(seg_bare, allow_upper=seg_bare.isupper()):
    return True
  if seg_conn[0].isspace():
    return True
  if seg_end < n and not text[seg_end].isspace():
    return True
  return is_quoted_or_container_val


def _has_same_line_value_after(text: str, pos: int) -> bool:
  """Returns True when same-line value text follows a quote at ``pos``."""
  n = len(text)
  p = pos
  while p < n and text[p] in (" ", "\t"):
    p += 1
  if p >= n:
    return False
  ch = text[p]
  if ch.isspace() or ch in ",;)}]":
    return False
  if ch == "\\":
    bs = 0
    while p + bs < n and text[p + bs] == "\\":
      bs += 1
    if bs % 2 == 1 and p + bs < n and text[p + bs] in ("t", "r", "n"):
      return False
  return True


def _is_valid_dns_host_syntax(host: str) -> bool:
  """Returns True when ``host`` has valid DNS hostname or IPv4 syntax."""
  if not host or len(host) > 253 or not host.isascii():
    return False
  parts = host.split(".")
  if not all(
      1 <= len(part) <= 63
      and not part.startswith("-")
      and not part.endswith("-")
      and all(c.isalnum() or c == "-" for c in part)
      for part in parts
  ):
    return False
  if len(parts) > 1 and parts[-1].isdigit():
    return len(parts) == 4 and all(
        len(part) <= 3 and part.isdigit() and int(part) <= 255 for part in parts
    )
  return True


def _is_likely_url_hostname(host: str) -> bool:
  """Returns True when ``host`` looks like a real hostname or IPv4 address."""
  if not _is_valid_dns_host_syntax(host):
    return False
  lower = host.lower()
  if lower == "localhost":
    return True
  if "." not in lower:
    return False
  parts = lower.split(".")
  if len(parts) == 4 and all(
      len(part) <= 3 and part.isdigit() and int(part) <= 255 for part in parts
  ):
    return True
  tld = parts[-1]
  return (
      tld in _COMMON_URL_HOST_TLDS
      or (len(tld) == 2 and tld.isascii() and tld.isalpha())
      or (
          len(parts) >= 3
          and 2 <= len(tld) <= 24
          and tld.isascii()
          and tld.isalpha()
      )
  )


@functools.lru_cache(maxsize=2048)
def _is_namespace_colon_segment_cached(seg: str, allow_upper: bool) -> bool:
  cleaned = seg.replace("_", "").replace("-", "")
  if not cleaned or not cleaned.isalnum():
    return False
  if seg[0].islower() and seg.islower():
    return True
  if allow_upper and seg[0].isupper() and seg.isupper():
    return True
  return False


def _is_namespace_colon_segment(seg: str, *, allow_upper: bool = False) -> bool:
  """Returns True for short namespace words (``[a-z][a-z0-9_-]*``, or upper)."""
  if not isinstance(seg, str) or not seg or len(seg) > 24:
    return False
  return _is_namespace_colon_segment_cached(seg, allow_upper)


@functools.lru_cache(maxsize=2048)
def _is_namespace_colon_path_cached(seg: str, allow_upper: bool) -> bool:
  if "/" in seg or "." in seg:
    parts = seg.replace(".", "/").split("/")
    if len(parts) > 6:
      return False
    return all(
        part
        and not is_sensitive_key(part, is_container=False)
        and _is_namespace_colon_segment(
            part, allow_upper=allow_upper or part.isupper()
        )
        for part in parts
    )
  return not is_sensitive_key(
      seg, is_container=False
  ) and _is_namespace_colon_segment(
      seg, allow_upper=allow_upper or seg.isupper()
  )


def _is_namespace_colon_path(seg: str, *, allow_upper: bool = True) -> bool:
  """Returns True for a non-sensitive namespace segment or slash/dot path."""
  if not isinstance(seg, str) or not seg or len(seg) > 64:
    return False
  return _is_namespace_colon_path_cached(seg, allow_upper)


_SAFE_VERSION_NS_PREFIXES = frozenset(
    {
        "api",
        "tenant",
        "service",
        "app",
        "cluster",
        "namespace",
        "ns",
        "svc",
        "project",
        "org",
        "sdk",
        "server",
        "client",
        "host",
        "node",
        "pod",
        "web",
        "grpc",
        "http",
        "rest",
    }
)


def _is_safe_version_or_numeric_ns_path(seg: str) -> bool:
  """Returns True for safe IPv4 or version/numeric-tail namespace paths."""
  if not seg or len(seg) > 64 or ("/" not in seg and "." not in seg):
    return False
  parts = seg.replace(".", "/").split("/")
  if not (2 <= len(parts) <= 6) or any(not p for p in parts):
    return False
  if any(is_sensitive_key(p, is_container=False) for p in parts):
    return False
  if (
      "/" not in seg
      and len(parts) == 4
      and all(
          p.isascii()
          and p.isdigit()
          and (len(p) == 1 or not p.startswith("0"))
          and 0 <= int(p) <= 255
          for p in parts
      )
  ):
    return True
  if (
      not parts[-1].isascii()
      or not parts[-1].isdigit()
      or len(parts[-1]) > 4
      or parts[0].isdigit()
  ):
    return False
  for p in parts[:-1]:
    if _NON_CONTAINER_VERSION_SEG_RE.fullmatch(p):
      continue
    if p.isascii() and p.isdigit() and len(p) <= 4:
      continue
    if p.lower() in _SAFE_VERSION_NS_PREFIXES:
      continue
    p_norm = _normalize_key_name(p)
    if p_norm in (
        "prod",
        "production",
        "staging",
        "stage",
        "dev",
        "development",
        "qa",
        "uat",
        "sandbox",
        "local",
    ):
      continue
    if (
        p_norm in _SAFE_CONTAINER_METADATA_KEYS
        and not is_sensitive_key(p, is_container=False)
        and p_norm not in _SAFE_DIAGNOSTIC_SCALAR_KEYS
    ):
      continue
    return False
  return True


@functools.lru_cache(maxsize=2048)
def _classify_colon_namespace_seg_cached(
    seg_bare: str, norm_seg: str
) -> tuple[bool, bool]:
  """Cached implementation of _classify_colon_namespace_seg."""
  is_version_ns = bool(_NON_CONTAINER_VERSION_SEG_RE.fullmatch(seg_bare))
  is_env_ns = norm_seg in (
      "prod",
      "production",
      "staging",
      "stage",
      "dev",
      "development",
      "qa",
      "uat",
      "sandbox",
      "local",
  )
  is_upper_tag_ns = (
      seg_bare.isupper() and seg_bare.isalpha() and 2 <= len(seg_bare) <= 12
  )
  seg_is_diag_exact = norm_seg in _SAFE_DIAGNOSTIC_SCALAR_KEYS
  is_safe_meta_ns = (
      norm_seg in _SAFE_CONTAINER_METADATA_KEYS
      and not is_sensitive_key(seg_bare, is_container=False)
      and not seg_is_diag_exact
  )
  is_known_tight_ns = (
      is_version_ns or is_env_ns or is_upper_tag_ns or is_safe_meta_ns
  )
  if not is_known_tight_ns and ("/" in seg_bare or "." in seg_bare):
    sep = "/" if "/" in seg_bare else "."
    parts = seg_bare.split(sep)
    if 2 <= len(parts) <= 6 and all(
        part
        and not is_sensitive_key(part, is_container=False)
        and _classify_colon_namespace_seg(part, _normalize_key_name(part))[0]
        for part in parts
    ):
      is_known_tight_ns = True
  if not is_known_tight_ns and _is_safe_version_or_numeric_ns_path(seg_bare):
    is_known_tight_ns = True
  is_title_ns = (
      2 <= len(seg_bare) <= 24
      and seg_bare[0].isupper()
      and seg_bare[1:].islower()
      and seg_bare.isalpha()
  )
  return is_known_tight_ns, is_title_ns


def _classify_colon_namespace_seg(
    seg_bare: str, norm_seg: str
) -> tuple[bool, bool]:
  """Returns ``(is_known_tight_ns, is_title_ns)`` for a colon namespace seg."""
  if len(seg_bare) <= 128:
    return _classify_colon_namespace_seg_cached(seg_bare, norm_seg)
  return _classify_colon_namespace_seg_cached.__wrapped__(seg_bare, norm_seg)


@functools.lru_cache(maxsize=2048)
def _has_auth_status_container_tok_cached(container_name: str) -> bool:
  norm = _normalize_key_name(container_name)
  return any(tok in norm for tok in _AUTH_STATUS_CONTAINER_TOKS)


def _has_auth_status_container_tok(container_name: str) -> bool:
  """Returns True if container_name contains an auth status container token."""
  if len(container_name) <= 128:
    return _has_auth_status_container_tok_cached(container_name)
  return _has_auth_status_container_tok_cached.__wrapped__(container_name)


def _is_tight_colon_ident(seg: str) -> bool:
  """Returns True for a tight colon-chain identifier segment."""
  return _is_valid_tight_ident_path(seg, require_alpha=False)


def _has_tight_eq_leaf_ahead(
    text: str,
    pos: int,
    prev_seg: str,
    *,
    max_hops: int = 12,
) -> bool:
  """Returns True when tight colon-chained segments end in an ``=`` leaf."""
  n = len(text)
  cur = pos
  prev_norm = _normalize_key_name(prev_seg)
  for _ in range(max_hops):
    if cur >= n or text[cur] not in _PATH_SEG_START_CHARS:
      return False
    seg_m = _BARE_KV_RE.match(text, cur, min(n, cur + 256))
    if seg_m is None or seg_m.start("bare") != cur:
      return False
    seg_bare = seg_m.group("bare") or ""
    if not _is_tight_colon_ident(seg_bare):
      return False
    seg_conn = seg_m.group("conn")
    seg_end = seg_m.end()
    if seg_conn.strip() == "=" and _has_eq_value(text, seg_end):
      seg_norm = _normalize_key_name(seg_bare)
      if (
          seg_norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
          or seg_norm in _SAFE_SMALL_COUNT_KEYS
          or seg_norm in ("status", "reason", "result", "state")
      ):
        return False
      return is_sensitive_key(
          seg_bare, is_container=False
      ) or _is_explicit_container_eq_leaf(
          seg_bare, seg_conn, text, seg_end, prev_norm
      )
    if seg_conn == ":":
      cur = seg_end
      prev_norm = _normalize_key_name(seg_bare)
      continue
    return False
  return False


def _scan_url_authority_and_safe_path(
    text: str, scheme_colon_end: int
) -> tuple[int, tuple[int, int] | None, bool]:
  """Scans URL authority and safe non-KV path segments.

  Redacts embedded ``user:pass@`` userinfo passwords. Returns
  ``(new_url_pos, auth_redact_span, stopped_at_kv_seg)`` where
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
  first_at_pos = -1
  last_at_pos = -1
  at_count = 0
  first_host_break_pos = -1
  first_host_break_ch = ""
  first_host_break_was_valid_port = False
  first_host_break_key = ""
  last_kv_key_before_at = ""
  cur_cand_break_pos = -1
  cur_cand_break_ch = ""
  cur_cand_had_valid_port = False
  cur_cand_had_valid_host = False
  cur_cand_had_valid_host_syntax = False
  cur_cand_first_key = ""
  cur_cand_last_key = ""
  confirmed_post_at_break_pos = -1
  pending_bare_break_pos = -1
  first_passwordless_break_pos = -1
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
    if colon_pos == -1 and last_at_pos == -1:
      if ch in _URL_STOP_CHARS or ch == "=":
        break
      if ch == ":":
        colon_pos = hard_p
        last_colon_pos = hard_p
      elif ch == "@":
        first_at_pos = hard_p
        last_at_pos = hard_p
        at_count = 1
    else:
      if ch in ("&", ",", ";"):
        if last_at_pos == -1:
          kv_m = _URL_DELIM_KV_RE.match(text, hard_p)
          if kv_m is not None:
            kv_name = text[hard_p + 1 : kv_m.end() - 1]
            kv_name_norm = _normalize_key_name(kv_name)
            last_kv_key_before_at = kv_name_norm
            if first_host_break_pos == -1:
              kv_port_len = hard_p - last_colon_pos - 1
              is_valid_port = (
                  0 < kv_port_len <= 5
                  and text[last_colon_pos + 1 : hard_p].isascii()
                  and text[last_colon_pos + 1 : hard_p].isdigit()
                  and int(text[last_colon_pos + 1 : hard_p]) <= 65535
              )
              if is_valid_port and last_colon_pos == colon_pos:
                first_host_break_pos = hard_p
                first_host_break_ch = ch
                first_host_break_was_valid_port = True
                first_host_break_key = kv_name_norm
              elif (
                  is_sensitive_key(kv_name, is_container=False)
                  or kv_name_norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
              ):
                first_host_break_pos = hard_p
                first_host_break_ch = ch
                first_host_break_key = kv_name_norm
        else:
          kv_m = _URL_DELIM_KV_RE.match(text, hard_p)
          if kv_m is not None:
            kv_name = text[hard_p + 1 : kv_m.end() - 1]
            kv_name_norm = _normalize_key_name(kv_name)
            cur_cand_last_key = kv_name_norm
            if cur_cand_break_pos == -1:
              kv_port_len = (
                  hard_p - last_colon_pos - 1
                  if last_colon_pos > last_at_pos
                  else 0
              )
              is_valid_port = (
                  0 < kv_port_len <= 5
                  and text[last_colon_pos + 1 : hard_p].isascii()
                  and text[last_colon_pos + 1 : hard_p].isdigit()
                  and int(text[last_colon_pos + 1 : hard_p]) <= 65535
              )
              has_port = is_valid_port and last_colon_pos > last_at_pos
              host_end = last_colon_pos if has_port else hard_p
              host_slice = text[last_at_pos + 1 : host_end]
              cur_cand_break_pos = hard_p
              cur_cand_break_ch = ch
              cur_cand_had_valid_port = has_port
              cur_cand_had_valid_host_syntax = len(
                  host_slice
              ) <= 253 and _is_valid_dns_host_syntax(host_slice)
              cur_cand_had_valid_host = len(
                  host_slice
              ) <= 253 and _is_likely_url_hostname(host_slice)
              cur_cand_first_key = kv_name_norm
              if (
                  first_passwordless_break_pos == -1
                  and colon_pos == -1
                  and at_count == 1
                  and cur_cand_had_valid_host_syntax
              ):
                first_passwordless_break_pos = hard_p
      if ch in ("&", ",", ";", ")", "'"):
        if last_at_pos == -1 and first_host_break_pos == -1:
          port_len = (
              hard_p - last_colon_pos - 1
              if (last_colon_pos != -1 and last_colon_pos > last_at_pos)
              else 0
          )
          if 0 < port_len <= 5:
            port_s = text[last_colon_pos + 1 : hard_p]
            if port_s.isascii() and port_s.isdigit() and int(port_s) <= 65535:
              host_slice = text[auth_start:last_colon_pos]
              if (
                  len(host_slice) <= 253
                  and _is_valid_dns_host_syntax(host_slice)
                  and last_colon_pos == colon_pos
              ):
                first_host_break_pos = hard_p
                first_host_break_ch = ch
                first_host_break_was_valid_port = True
        elif last_at_pos != -1 and cur_cand_break_pos == -1:
          port_len = (
              hard_p - last_colon_pos - 1 if last_colon_pos > last_at_pos else 0
          )
          has_valid_port = False
          host_slice = ""
          if 0 < port_len <= 5:
            port_s = text[last_colon_pos + 1 : hard_p]
            if port_s.isascii() and port_s.isdigit() and int(port_s) <= 65535:
              cand_host = text[last_at_pos + 1 : last_colon_pos]
              if len(cand_host) <= 253 and _is_valid_dns_host_syntax(cand_host):
                has_valid_port = True
                host_slice = cand_host
          if has_valid_port:
            cur_cand_break_pos = hard_p
            cur_cand_break_ch = ch
            cur_cand_had_valid_port = True
            cur_cand_had_valid_host_syntax = True
            cur_cand_had_valid_host = _is_likely_url_hostname(host_slice)
            cur_cand_first_key = ""
          else:
            host_slice = text[last_at_pos + 1 : hard_p]
            if len(host_slice) <= 253 and _is_likely_url_hostname(host_slice):
              cur_cand_break_pos = hard_p
              cur_cand_break_ch = ch
              cur_cand_had_valid_port = False
              cur_cand_had_valid_host_syntax = True
              cur_cand_had_valid_host = True
              cur_cand_first_key = ""
          if (
              cur_cand_break_pos != -1
              and first_passwordless_break_pos == -1
              and colon_pos == -1
              and at_count == 1
              and cur_cand_had_valid_host_syntax
          ):
            first_passwordless_break_pos = hard_p
      if ch == ":":
        last_colon_pos = hard_p
        if (
            colon_pos == -1
            and first_at_pos != -1
            and first_host_break_pos == -1
            and cur_cand_break_pos == -1
            and confirmed_post_at_break_pos == -1
            and first_passwordless_break_pos == -1
        ):
          colon_pos = hard_p
      elif ch == "@":
        if cur_cand_break_pos != -1 and confirmed_post_at_break_pos == -1:
          cand_last_is_sens_or_email = bool(cur_cand_last_key) and (
              is_sensitive_key(cur_cand_last_key, is_container=False)
              or cur_cand_last_key in _URL_EMAIL_OR_REDIRECT_PARAM_KEYS
          )
          if (
              cur_cand_had_valid_host
              or (cur_cand_had_valid_port and cur_cand_had_valid_host_syntax)
          ) and cand_last_is_sens_or_email:
            confirmed_post_at_break_pos = cur_cand_break_pos
          elif (
              pending_bare_break_pos == -1
              and cur_cand_break_ch != "&"
              and cur_cand_had_valid_host
              and not cur_cand_first_key
              and not cur_cand_last_key
          ):
            pending_bare_break_pos = cur_cand_break_pos
        at_count += 1
        last_at_pos = hard_p
        cur_cand_break_pos = -1
        cur_cand_break_ch = ""
        cur_cand_had_valid_port = False
        cur_cand_had_valid_host = False
        cur_cand_had_valid_host_syntax = False
        cur_cand_first_key = ""
        cur_cand_last_key = ""
    hard_p += 1

  if last_at_pos == -1:
    if first_host_break_pos != -1:
      hard_p = first_host_break_pos
  else:
    final_end = cur_cand_break_pos if cur_cand_break_pos != -1 else hard_p
    post_at_port_len = (
        final_end - last_colon_pos - 1
        if (last_colon_pos > last_at_pos and last_colon_pos < final_end)
        else 0
    )
    has_port_after_at = (
        0 < post_at_port_len <= 5
        and text[last_colon_pos + 1 : final_end].isascii()
        and text[last_colon_pos + 1 : final_end].isdigit()
        and int(text[last_colon_pos + 1 : final_end]) <= 65535
    )
    post_at_host_end = last_colon_pos if has_port_after_at else final_end
    post_at_is_valid_host = post_at_host_end - (
        last_at_pos + 1
    ) <= 253 and _is_likely_url_hostname(
        text[last_at_pos + 1 : post_at_host_end]
    )
    has_path_after_at = (
        cur_cand_break_pos == -1
        and hard_p < n
        and (text[hard_p] in ("/", "?") or text.startswith("\\/", hard_p))
    )
    should_rewind_pre_at = False
    if (
        first_host_break_pos != -1
        and first_host_break_pos < last_at_pos
        and first_host_break_was_valid_port
    ):
      prefix = text[auth_start:colon_pos]
      prefix_is_host_syntax = len(prefix) <= 253 and _is_valid_dns_host_syntax(
          prefix
      )
      prefix_is_host = len(prefix) <= 253 and _is_likely_url_hostname(prefix)
      last_key_is_sensitive = bool(last_kv_key_before_at) and is_sensitive_key(
          last_kv_key_before_at, is_container=False
      )
      last_key_is_email_or_redirect = (
          last_kv_key_before_at in _URL_EMAIL_OR_REDIRECT_PARAM_KEYS
      )
      pre_is_bare_break = (
          first_host_break_ch != "&"
          and not first_host_break_key
          and not last_kv_key_before_at
      )
      should_rewind_pre_at = prefix_is_host_syntax and (
          (
              not post_at_is_valid_host
              and (last_key_is_sensitive or last_key_is_email_or_redirect)
          )
          or (
              prefix_is_host
              and (
                  last_key_is_sensitive
                  or last_key_is_email_or_redirect
                  or (pre_is_bare_break and not has_path_after_at)
              )
          )
      )
    if should_rewind_pre_at:
      hard_p = first_host_break_pos
    elif first_passwordless_break_pos != -1:
      hard_p = first_passwordless_break_pos
    elif confirmed_post_at_break_pos != -1:
      hard_p = confirmed_post_at_break_pos
    elif (
        pending_bare_break_pos != -1
        and not has_path_after_at
        and post_at_is_valid_host
    ):
      hard_p = pending_bare_break_pos
    elif cur_cand_break_pos != -1:
      hard_p = cur_cand_break_pos

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


def _resolve_container_next_line_child(
    text: str,
    bare_start: int,
    next_val_start: int,
    prev_seg: str,
    active_container: str,
    *,
    max_nl_hops: int = 1,
    head_has_path_sep: bool = False,
    seen_containers: frozenset[str] | None = None,
    nl_depth: int = 0,
    out_sensitive_hit: list[bool] | None = None,
) -> tuple[str | None, str, int, int, int] | None:
  """Resolves a single next-line child KV under a container header."""
  n = len(text)
  if next_val_start >= n or text[next_val_start] not in _PATH_SEG_START_CHARS:
    return None
  next_m = _BARE_KV_RE.match(text, next_val_start, min(n, next_val_start + 256))
  skip_sensitive_check = (
      next_m is not None
      and next_m.start("bare") == next_val_start
      and next_m.group("conn").strip() == ":"
      and (next_m.end() >= n or text[next_m.end()] not in _PATH_SEG_START_CHARS)
      and not is_sensitive_key(next_m.group("bare") or "", is_container=False)
  )
  if not skip_sensitive_check:
    is_sens = _is_sensitive_header_at(
        text,
        next_val_start,
        allow_mismatched_quote=True,
    )
    if out_sensitive_hit is not None:
      out_sensitive_hit.append(is_sens)
    if is_sens:
      return None
  if next_m is None or next_m.start("bare") != next_val_start:
    return None
  seg_bare = next_m.group("bare") or ""
  seg_conn = next_m.group("conn")
  seg_conn_stripped = seg_conn.strip()
  seg_end = next_m.end()
  if seg_conn_stripped not in (":", "="):
    return None
  seg_norm = _normalize_key_name(seg_bare)
  prev_norm = _normalize_key_name(prev_seg)
  seg_is_metadata = seg_norm in _SAFE_CONTAINER_METADATA_KEYS
  seg_is_diag_exact = seg_norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
  seg_is_diag_suffix = seg_norm.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
  seg_is_diag = seg_is_diag_exact or seg_is_diag_suffix
  seg_allows_eq_safe_tail = (
      seg_is_diag_exact
      or seg_is_metadata
      or (seg_is_diag_suffix and head_has_path_sep)
  )
  if (
      (
          seg_allows_eq_safe_tail
          and seg_conn_stripped == "="
          and _has_eq_value(text, seg_end)
      )
      or (
          seg_is_diag
          and seg_conn_stripped == ":"
          and _has_colon_value_sep(text, seg_end)
      )
  ) and not is_sensitive_key(
      seg_bare if seg_is_metadata else f"{active_container}:{seg_bare}",
      is_container=False,
  ):
    return None, seg_conn, seg_end, 0, seg_end
  if seg_conn_stripped == ":" and (
      bool(_extract_container_colon_head(seg_bare))
      or _is_namespace_colon_path(seg_bare, allow_upper=True)
  ):
    return _resolve_container_continuation(
        text,
        bare_start,
        next_val_start,
        prev_seg,
        active_container,
        max_nl_hops=max_nl_hops,
        head_has_path_sep=True,
        seen_containers=seen_containers,
        nl_depth=nl_depth,
    )
  if _is_explicit_container_eq_leaf(
      seg_bare,
      seg_conn,
      text,
      seg_end,
      prev_norm,
  ) or (
      seg_norm != prev_norm
      and (seg_conn_stripped != "=" or _has_eq_value(text, seg_end))
      and (seg_conn_stripped != ":" or _has_colon_value_sep(text, seg_end))
      and _is_canonical_colon_key_segment(
          prev_seg,
          seg_bare,
          allow_suffix=True,
          container_seg=active_container,
      )
  ):
    if (
        seg_end < n
        and not text[seg_end].isspace()
        and text[seg_end] not in "{[(\\"
    ):
      leaf_val_start, leaf_nl_count = seg_end, 0
    else:
      leaf_val_start, leaf_nl_count = _skip_kv_whitespace(text, seg_end)
    return (
        text[bare_start : next_m.end("bare")],
        seg_conn,
        leaf_val_start,
        leaf_nl_count,
        seg_end,
    )
  return None


def _is_next_line_raw_or_b64_eq(text: str, pos: int) -> bool:
  """Returns True for raw text or an unquoted ``=``-padded token at ``pos``."""
  n = len(text)
  bare_m = _BARE_KV_RE.match(text, pos, min(n, pos + 256))
  if bare_m is None:
    return True
  conn = bare_m.group("conn")
  if conn.strip() != "=":
    return False
  end = bare_m.end()
  if not _has_eq_value(text, end):
    return True
  if conn == "=" and end < n and text[end].isspace():
    val_s, _ = _skip_kv_whitespace(text, end)
    if val_s < n and text[val_s] not in ('"', "'", "{", "[", "("):
      return True
  return False


def _is_cookie_header_key_norm(norm_key: str) -> bool:
  """Returns True if a normalized key name denotes a cookie header."""
  return norm_key in (
      "cookie",
      "cookies",
      "set_cookie",
      "set_cookies",
  ) or norm_key.endswith(("_cookie", "_cookies"))


class _ColonSegFacts(NamedTuple):
  """Static classification facts for a colon-chain segment."""

  seg_norm: str
  seg_is_metadata: bool
  seg_is_diag_exact: bool
  seg_is_diag_suffix: bool
  nested_cont: str
  nested_cont_norm: str
  seg_is_sens: bool
  is_known_tight_ns: bool
  is_title_ns: bool
  is_ns_path: bool
  is_tight_ident: bool
  nested_has_auth_tok: bool
  seg_tail: str
  seg_tail_norm: str
  canon_leaf_static: bool | None


class _BareHeadFacts(NamedTuple):
  """Static classification facts for a bare KV head."""

  norm_head: str
  is_cookie_head: bool
  head_cont: str
  head_has_unknown_ns: bool
  is_sens_false: bool
  is_sens_true: bool
  lower_bare: str
  is_multiword_tail: bool
  has_sep: bool
  tail: str


@functools.lru_cache(maxsize=2048)
def _get_colon_seg_facts_cached(seg_bare: str) -> _ColonSegFacts:
  seg_norm = _normalize_key_name(seg_bare)
  seg_is_metadata = seg_norm in _SAFE_CONTAINER_METADATA_KEYS
  seg_is_diag_exact = seg_norm in _SAFE_DIAGNOSTIC_SCALAR_KEYS
  seg_is_diag_suffix = seg_norm.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
  nested_cont = _extract_container_colon_head(seg_bare)
  nested_cont_norm = _normalize_key_name(nested_cont) if nested_cont else ""
  seg_is_sens = is_sensitive_key(seg_bare, is_container=False)
  is_known_tight_ns, is_title_ns = _classify_colon_namespace_seg(
      seg_bare, seg_norm
  )
  is_ns_path = _is_namespace_colon_path(seg_bare, allow_upper=True)
  is_tight_ident = _is_tight_colon_ident(seg_bare)
  nested_has_auth_tok = (
      _has_auth_status_container_tok(nested_cont_norm)
      if nested_cont_norm
      else False
  )
  seg_tail = seg_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
  seg_tail_norm = _normalize_key_name(seg_tail)
  canon_leaf_static = (
      None
      if (
          "/" in seg_bare
          or "." in seg_bare
          or seg_norm in _COMPOUND_SENSITIVE_KEY_TAILS
      )
      else _is_canonical_colon_key_segment_impl(
          "", seg_bare, allow_suffix=True, container_seg=""
      )
  )
  return _ColonSegFacts(
      seg_norm=seg_norm,
      seg_is_metadata=seg_is_metadata,
      seg_is_diag_exact=seg_is_diag_exact,
      seg_is_diag_suffix=seg_is_diag_suffix,
      nested_cont=nested_cont,
      nested_cont_norm=nested_cont_norm,
      seg_is_sens=seg_is_sens,
      is_known_tight_ns=is_known_tight_ns,
      is_title_ns=is_title_ns,
      is_ns_path=is_ns_path,
      is_tight_ident=is_tight_ident,
      nested_has_auth_tok=nested_has_auth_tok,
      seg_tail=seg_tail,
      seg_tail_norm=seg_tail_norm,
      canon_leaf_static=canon_leaf_static,
  )


def _get_colon_seg_facts(seg_bare: str) -> _ColonSegFacts:
  """Returns cached static classification facts for a colon-chain segment."""
  if len(seg_bare) <= 128:
    return _get_colon_seg_facts_cached(seg_bare)
  return _get_colon_seg_facts_cached.__wrapped__(seg_bare)


@functools.lru_cache(maxsize=2048)
def _get_bare_head_facts_cached(head_bare: str) -> _BareHeadFacts:
  norm_head = _normalize_key_name(head_bare)
  is_cookie_head = _is_cookie_header_key_norm(norm_head)
  head_cont = "" if is_cookie_head else _extract_container_colon_head(head_bare)
  head_has_unknown_ns = _has_unknown_path_segment(head_bare)
  is_sens_false = is_sensitive_key(head_bare, is_container=False)
  is_sens_true = is_sensitive_key(head_bare, is_container=True)
  lower_bare = head_bare.lower()
  has_sep = "/" in head_bare or "." in head_bare
  tail = (
      head_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1] if has_sep else head_bare
  )
  is_multiword_tail = lower_bare in _MULTIWORD_KEY_TAIL_WORDS
  return _BareHeadFacts(
      norm_head=norm_head,
      is_cookie_head=is_cookie_head,
      head_cont=head_cont,
      head_has_unknown_ns=head_has_unknown_ns,
      is_sens_false=is_sens_false,
      is_sens_true=is_sens_true,
      lower_bare=lower_bare,
      is_multiword_tail=is_multiword_tail,
      has_sep=has_sep,
      tail=tail,
  )


def _get_bare_head_facts(head_bare: str) -> _BareHeadFacts:
  """Returns cached static classification facts for a bare KV head."""
  if len(head_bare) <= 128:
    return _get_bare_head_facts_cached(head_bare)
  return _get_bare_head_facts_cached.__wrapped__(head_bare)


def _resolve_container_continuation(
    text: str,
    bare_start: int,
    start_pos: int,
    prev_seg: str,
    active_container: str,
    *,
    max_nl_hops: int = 3,
    head_has_path_sep: bool = False,
    head_has_unknown_ns: bool = False,
    seen_containers: frozenset[str] | None = None,
    nl_depth: int = 0,
    initial_seg_m: re.Match[str] | None = None,
) -> tuple[str | None, str, int, int, int] | None:
  """Resolves bounded same-line tight or spaced container continuations."""
  n = len(text)
  pos = start_pos
  cur_prev = prev_seg
  cur_container = active_container
  saw_unknown_ns = head_has_unknown_ns
  saw_colon_unknown_ns = False
  has_auth_status_ctx = not saw_unknown_ns and _has_auth_status_container_tok(
      cur_container
  )
  cont_norm = _normalize_key_name(active_container)
  prev_norm = (
      cont_norm
      if cur_prev == active_container
      else _normalize_key_name(cur_prev)
  )
  seen_containers_set = (
      set(seen_containers) if seen_containers is not None else set()
  )
  seen_containers_set.add(cont_norm)
  container_hops = 0
  last_container_m: re.Match[str] | None = None
  last_colon_m: re.Match[str] | None = None
  last_container_can_fallback = False
  hop_idx = 0
  while hop_idx < 16:
    saw_ws_before_seg = False
    while pos < n and text[pos] in (" ", "\t"):
      pos += 1
      saw_ws_before_seg = True
    seg_m: re.Match[str] | None = None
    if (
        hop_idx == 0
        and initial_seg_m is not None
        and initial_seg_m.start() == pos
    ):
      seg_m = initial_seg_m
    elif pos < n and text[pos] in _PATH_SEG_START_CHARS:
      seg_m = _BARE_KV_RE.match(text, pos, min(n, pos + 256))
    if seg_m is not None and seg_m.start("bare") == pos:
      seg_bare = seg_m.group("bare") or ""
      seg_conn = seg_m.group("conn")
      seg_conn_stripped = (
          seg_conn if seg_conn in (":", "=") else seg_conn.strip()
      )
      seg_end = seg_m.end()
      if seg_conn_stripped not in (":", "="):
        return None
      facts = _get_colon_seg_facts(seg_bare)
      (
          seg_norm,
          seg_is_metadata,
          seg_is_diag_exact,
          seg_is_diag_suffix,
          seg_nested_cont,
          nested_cont_norm,
          seg_is_sens_leaf,
          is_known_tight_ns,
          is_title_ns,
          is_ns_path,
          is_tight_ident,
          nested_has_auth_tok,
          seg_tail,
          seg_tail_norm,
          canon_leaf_static,
      ) = facts
      is_tight_no_ws_head = (
          not head_has_path_sep and hop_idx == 0 and not saw_ws_before_seg
      )
      seg_is_diag = seg_is_diag_exact or seg_is_diag_suffix
      if (
          not saw_unknown_ns
          and (
              (
                  seg_conn_stripped == "="
                  and (
                      seg_is_diag_exact
                      or seg_is_metadata
                      or (seg_is_diag_suffix and not is_tight_no_ws_head)
                  )
                  and _has_eq_value(text, seg_end)
              )
              or (
                  seg_conn_stripped == ":"
                  and seg_is_diag
                  and (
                      (seg_end < n and text[seg_end] == " ")
                      or _has_colon_value_sep(text, seg_end)
                  )
              )
          )
          and not is_sensitive_key(
              seg_bare if seg_is_metadata else f"{cur_container}:{seg_bare}",
              is_container=False,
          )
      ):
        return None, seg_conn, seg_end, 0, seg_end
      if (
          not saw_unknown_ns
          and seg_conn_stripped == "="
          and "." in seg_bare
          and _has_eq_value(text, seg_end)
      ):
        dot_parts = seg_bare.split(".")
        if (
            len(dot_parts) == 2
            and _normalize_key_name(dot_parts[0])
            in ("status", "reason", "result", "state")
            and dot_parts[1].isdigit()
        ):
          sv, snl = _skip_kv_whitespace(text, seg_end)
          if snl == 0 and sv < n:
            raw_sv = text[sv : _scan_bare_value_end(text, sv)]
            if (
                _strip_matching_quotes(raw_sv).strip().lower()
                in _SAFE_AUTH_STATUS_VALUES
            ):
              return None, seg_conn, seg_end, 0, seg_end
      nested_cont = seg_nested_cont if seg_conn_stripped == ":" else ""
      is_nested_container = bool(nested_cont)
      is_leaf_match = False
      if not is_nested_container:
        if seg_conn_stripped == "=":
          seg_is_safe_eq_leaf = not seg_is_sens_leaf and (
              seg_is_diag
              or seg_norm in _SAFE_SMALL_COUNT_KEYS
              or seg_norm in ("status", "reason", "result", "state")
          )
          allow_eq_leaf = (
              not saw_unknown_ns
              or (not saw_ws_before_seg and not seg_is_safe_eq_leaf)
              or (seg_is_sens_leaf and not saw_colon_unknown_ns)
          )
          if allow_eq_leaf and (
              _is_explicit_container_eq_leaf(
                  seg_bare,
                  seg_conn,
                  text,
                  seg_end,
                  prev_norm,
              )
              or (
                  seg_norm != prev_norm
                  and _has_eq_value(text, seg_end)
                  and (
                      canon_leaf_static
                      if canon_leaf_static is not None
                      else _is_canonical_colon_key_segment(
                          cur_prev,
                          seg_bare,
                          allow_suffix=True,
                          container_seg=cur_container,
                      )
                  )
              )
          ):
            is_leaf_match = True
        else:
          allow_colon_leaf = (
              not saw_colon_unknown_ns and not saw_unknown_ns
          ) or (
              not saw_ws_before_seg
              and seg_is_sens_leaf
              and not seg_is_diag
              and seg_norm not in _SAFE_SMALL_COUNT_KEYS
          )
          if (
              allow_colon_leaf
              and seg_norm != prev_norm
              and (
                  (seg_end < n and text[seg_end] == " ")
                  or _has_colon_value_sep(text, seg_end)
              )
              and (
                  canon_leaf_static
                  if canon_leaf_static is not None
                  else _is_canonical_colon_key_segment(
                      cur_prev,
                      seg_bare,
                      allow_suffix=True,
                      container_seg=cur_container,
                  )
              )
          ):
            is_leaf_match = True
      if is_leaf_match:
        if (
            seg_end < n
            and not text[seg_end].isspace()
            and text[seg_end] not in "{[(\\"
        ):
          val_start, nl_count = seg_end, 0
        else:
          val_start, nl_count = _skip_kv_whitespace(text, seg_end)
        return (
            text[bare_start : seg_m.end("bare")],
            seg_conn,
            val_start,
            nl_count,
            seg_end,
        )
      tight_eq_ahead = (
          not saw_ws_before_seg
          and seg_conn == ":"
          and _has_tight_eq_leaf_ahead(text, seg_end, seg_bare, max_hops=12)
      )
      if is_nested_container:
        if saw_colon_unknown_ns and not tight_eq_ahead:
          return None
        if (
            nl_depth > 0
            and nested_cont_norm in seen_containers_set
            and "/" not in seg_bare
            and "." not in seg_bare
        ):
          return None
        has_sep_after_cont = seg_norm != prev_norm and (
            (seg_end < n and text[seg_end] == " ")
            or _has_colon_value_sep(text, seg_end)
        )
        if container_hops < (12 if tight_eq_ahead else 3):
          last_container_can_fallback = (
              not saw_colon_unknown_ns
              and nested_cont_norm not in seen_containers_set
              and has_sep_after_cont
          )
          seen_containers_set.add(nested_cont_norm)
          container_hops += 1
          cur_container = nested_cont
          has_auth_status_ctx = not saw_unknown_ns and nested_has_auth_tok
          cur_prev = seg_tail
          prev_norm = seg_tail_norm
          last_container_m = None if saw_colon_unknown_ns else seg_m
          last_colon_m = None if saw_colon_unknown_ns else seg_m
          pos = seg_end
          hop_idx += 1
          continue
        return None
      if seg_conn_stripped == ":":
        is_auth_status_tail = (
            not saw_unknown_ns
            and has_auth_status_ctx
            and seg_norm
            in (
                "status",
                "reason",
                "result",
                "state",
            )
        )
        is_known_count_tail = (
            not saw_unknown_ns and seg_norm in _SAFE_SMALL_COUNT_KEYS
        )
        if saw_ws_before_seg:
          can_hop_ns = (
              not saw_colon_unknown_ns
              and (
                  is_known_tight_ns
                  or is_auth_status_tail
                  or is_known_count_tail
              )
              and (is_known_tight_ns or is_ns_path or is_title_ns)
          )
        else:
          can_hop_ns = (
              is_known_tight_ns
              or is_ns_path
              or (tight_eq_ahead and is_tight_ident)
          )
        if can_hop_ns:
          if not (
              is_known_tight_ns or is_auth_status_tail or is_known_count_tail
          ):
            if not saw_ws_before_seg:
              if nl_depth > 0 and not saw_unknown_ns:
                seg_parts = seg_bare.replace(".", "/").split("/")
                is_sub_header_seg = bool(seg_parts) and all(
                    p
                    and not is_sensitive_key(p, is_container=False)
                    and (
                        _is_valid_colon_path_segment(p)
                        or _normalize_key_name(p) in ("metadata", "user")
                    )
                    for p in seg_parts
                )
                if is_sub_header_seg and "/" in seg_bare:
                  slash_tail = seg_bare.rsplit("/", 1)[-1]
                  is_sub_header_seg = _classify_colon_namespace_seg(
                      slash_tail, _normalize_key_name(slash_tail)
                  )[0] or _normalize_key_name(slash_tail) in (
                      "metadata",
                      "user",
                  )
                if is_sub_header_seg:
                  c_val, c_nl = _skip_kv_whitespace(text, seg_end)
                  if (
                      c_nl == 0
                      and c_val < n
                      and text[c_val] in ('"', "'", "\\")
                      and _is_sensitive_header_at(
                          text, c_val, allow_mismatched_quote=True
                      )
                  ):
                    return (
                        text[bare_start : seg_m.end("bare")],
                        seg_conn,
                        c_val,
                        c_nl,
                        seg_end,
                    )
              saw_unknown_ns = True
              saw_colon_unknown_ns = True
              last_container_m = None
              last_colon_m = None
              last_container_can_fallback = False
            has_auth_status_ctx = False
          elif not is_known_tight_ns:
            has_auth_status_ctx = False
          cur_prev = seg_tail
          prev_norm = seg_tail_norm
          last_container_m = None
          if not saw_unknown_ns:
            last_colon_m = seg_m
          last_container_can_fallback = False
          pos = seg_end
          hop_idx += 1
          continue
      if seg_conn_stripped != "=" or (
          last_colon_m is None
          and not (last_container_m is not None and last_container_can_fallback)
      ):
        return None
      break
    else:
      break
  colon_val_nl: tuple[int, int] | None = None
  colon_sens_hit: list[bool] = []

  def _is_colon_sens_at(target_pos: int) -> bool:
    if (
        colon_val_nl is not None
        and target_pos == colon_val_nl[0]
        and colon_sens_hit
    ):
      return colon_sens_hit[0]
    res = _is_sensitive_header_at(
        text,
        target_pos,
        allow_mismatched_quote=True,
    )
    if colon_val_nl is not None and target_pos == colon_val_nl[0]:
      colon_sens_hit.append(res)
    return res

  if last_colon_m is not None:
    colon_end = last_colon_m.end()
    colon_val, colon_nl = _skip_kv_whitespace(text, colon_end)
    colon_val_nl = (colon_val, colon_nl)
    if max_nl_hops > 0 and colon_nl == 1:
      nl_child = _resolve_container_next_line_child(
          text,
          bare_start,
          colon_val,
          cur_prev,
          cur_container,
          max_nl_hops=max_nl_hops - 1,
          head_has_path_sep=True,
          seen_containers=frozenset(seen_containers_set),
          nl_depth=nl_depth + 1,
          out_sensitive_hit=colon_sens_hit,
      )
      if nl_child is not None:
        return nl_child
    if (
        last_container_m is None
        and _has_colon_value_sep(text, colon_end)
        and colon_val < n
    ):
      if (
          colon_nl == 0
          and _CRITICAL_ERROR_MARKERS_RE.match(text, colon_val) is None
      ):
        return (
            text[bare_start : last_colon_m.end("bare")],
            last_colon_m.group("conn"),
            colon_val,
            colon_nl,
            colon_end,
        )
      if colon_nl == 1 and max_nl_hops > 0:
        if text[colon_val] in ("{", "[", "("):
          return (
              text[bare_start : last_colon_m.end("bare")],
              last_colon_m.group("conn"),
              colon_val,
              colon_nl,
              colon_end,
          )
        if _is_colon_sens_at(colon_val):
          if (
              nl_depth == 0
              and colon_val < n
              and text[colon_val] not in ('"', "'", "\\", "{", "[", "(")
          ):
            return (
                None,
                last_colon_m.group("conn"),
                colon_val,
                1,
                colon_end,
            )
          return (
              text[bare_start : last_colon_m.end("bare")],
              last_colon_m.group("conn"),
              colon_val,
              colon_nl,
              colon_end,
          )
        if _is_next_line_raw_or_b64_eq(text, colon_val):
          return (
              text[bare_start : last_colon_m.end("bare")],
              last_colon_m.group("conn"),
              colon_val,
              colon_nl,
              colon_end,
          )
    if (
        last_container_m is not last_colon_m
        and max_nl_hops > 0
        and colon_nl == 1
    ):
      if colon_val < n and text[colon_val] in ("{", "[", "("):
        return (
            text[bare_start : last_colon_m.end("bare")],
            last_colon_m.group("conn"),
            colon_val,
            colon_nl,
            colon_end,
        )
      if _is_colon_sens_at(colon_val):
        if (
            nl_depth == 0
            and colon_val < n
            and text[colon_val] not in ('"', "'", "\\", "{", "[", "(")
        ):
          return (
              None,
              last_colon_m.group("conn"),
              colon_val,
              1,
              colon_end,
          )
        return (
            text[bare_start : last_colon_m.end("bare")],
            last_colon_m.group("conn"),
            colon_val,
            colon_nl,
            colon_end,
        )
  if last_container_m is not None and last_container_can_fallback:
    c_end = last_container_m.end()
    if last_container_m is last_colon_m and colon_val_nl is not None:
      c_val, c_nl = colon_val_nl
    else:
      c_val, c_nl = _skip_kv_whitespace(text, c_end)
    if (
        c_nl == 0
        and c_val < n
        and _CRITICAL_ERROR_MARKERS_RE.match(text, c_val) is None
    ) or (c_val < n and text[c_val] in ("{", "[", "(")):
      return (
          text[bare_start : last_container_m.end("bare")],
          last_container_m.group("conn"),
          c_val,
          c_nl,
          c_end,
      )
    if max_nl_hops > 0 and _is_colon_sens_at(c_val):
      if (
          nl_depth == 0
          and c_nl == 1
          and c_val < n
          and text[c_val] not in ('"', "'", "\\", "{", "[", "(")
      ):
        return (
            None,
            last_container_m.group("conn"),
            c_val,
            1,
            c_end,
        )
      return (
          text[bare_start : last_container_m.end("bare")],
          last_container_m.group("conn"),
          c_val,
          c_nl,
          c_end,
      )
    if (
        c_nl == 1
        and max_nl_hops > 0
        and c_val < n
        and (
            text[c_val] in ('"', "'")
            or _starts_with_odd_escaped_quote(text, c_val)
            or _is_next_line_raw_or_b64_eq(text, c_val)
        )
    ):
      return (
          text[bare_start : last_container_m.end("bare")],
          last_container_m.group("conn"),
          c_val,
          c_nl,
          c_end,
      )
  return None


def _resolve_bare_or_path_kv(
    text: str,
    match: re.Match[str],
    *,
    max_nl_hops: int = 3,
) -> tuple[str | None, str, int, int, int]:
  """Resolves a bare or colon/slash-chained KV header from ``match``.

  Returns ``(matched_key, conn_text, val_start, nl_count, match_end)``.
  """
  n = len(text)
  match_end = match.end()
  conn_text = match.group("conn")
  head_bare = match.group("bare") or ""
  head_facts = _get_bare_head_facts(head_bare)
  conn_is_colon = conn_text.strip() == ":"
  head_cont = head_facts.head_cont if conn_is_colon else ""
  is_head_container_colon = bool(head_cont)
  if (
      is_head_container_colon
      and match_end < n
      and text[match_end] not in ("\n", "\r")
  ):
    init_seg_pos = -1
    if text[match_end] in _PATH_SEG_START_CHARS:
      init_seg_pos = match_end
    elif (
        text[match_end] == " "
        and match_end + 1 < n
        and text[match_end + 1] in _PATH_SEG_START_CHARS
    ):
      init_seg_pos = match_end + 1
    elif text[match_end] in (" ", "\t"):
      ws_val_start, ws_nl_count = _skip_kv_whitespace(text, match_end)
      if (
          ws_nl_count == 0
          and ws_val_start < n
          and text[ws_val_start] in _PATH_SEG_START_CHARS
      ):
        init_seg_pos = ws_val_start
    if init_seg_pos >= 0:
      init_seg_m = _BARE_KV_RE.match(
          text, init_seg_pos, min(n, init_seg_pos + 256)
      )
      if init_seg_m is not None and init_seg_m.start("bare") == init_seg_pos:
        cont_res = _resolve_container_continuation(
            text,
            match.start("bare"),
            match_end,
            head_facts.tail,
            head_cont,
            max_nl_hops=max_nl_hops,
            head_has_path_sep=head_facts.has_sep or conn_text != ":",
            head_has_unknown_ns=head_facts.head_has_unknown_ns,
            initial_seg_m=init_seg_m,
        )
        if cont_res is not None:
          return cont_res
  elif (
      not head_facts.is_cookie_head
      and not head_cont
      and conn_is_colon
      and not head_facts.is_sens_false
      and not head_facts.is_multiword_tail
      and head_facts.lower_bare != "token"
      and not (
          conn_text == ":"
          and match_end < n
          and text[match_end] in _PATH_SEG_START_CHARS
      )
  ):
    if (
        match_end + 1 < n
        and text[match_end] == " "
        and not text[match_end + 1].isspace()
        and text[match_end + 1] != "\\"
    ):
      val_start = match_end + 1
      nl_count = 0
    elif (
        match_end < n
        and not text[match_end].isspace()
        and text[match_end] not in "{[(\\"
    ):
      val_start = match_end
      nl_count = 0
    else:
      val_start, nl_count = _skip_kv_whitespace(text, match_end)
    if not (
        val_start < n
        and text[val_start] in ("{", "[", "(")
        and head_facts.is_sens_true
    ):
      return None, conn_text, val_start, nl_count, match_end
  elif (
      not head_facts.is_cookie_head
      and not is_head_container_colon
      and conn_is_colon
      and match_end < n
      and text[match_end] not in ("\n", "\r")
  ):
    acc_sensitive = is_sensitive_key(head_bare, is_container=False)
    can_chain = conn_text == ":" and text[match_end] in _PATH_SEG_START_CHARS
    if not can_chain and acc_sensitive:
      if text[match_end] in _PATH_SEG_START_CHARS:
        can_chain = True
      elif text[match_end] in (" ", "\t"):
        ws_val_start, ws_nl_count = _skip_kv_whitespace(text, match_end)
        can_chain = (
            ws_nl_count == 0
            and ws_val_start < n
            and text[ws_val_start] in _PATH_SEG_START_CHARS
        )
    if can_chain:
      bare_start = match.start("bare")
      prev_seg = head_bare
      last_m: re.Match[str] = match
      chained = False
      saw_ns_hop = False
      saw_tight_unknown_ns = _has_unknown_path_segment(head_bare)
      ns_hops = 0
      orig_head_has_container_ctx = acc_sensitive and any(
          tok in head_facts.norm_head for tok in _SENSITIVE_CONTAINER_CTX_TOKS
      )
      has_auth_status_ctx = (
          not saw_tight_unknown_ns
          and acc_sensitive
          and any(
              tok in head_facts.norm_head for tok in _AUTH_STATUS_CONTAINER_TOKS
          )
      )
      ctx_container = (
          "auth"
          if acc_sensitive
          and any(
              tok in head_facts.norm_head for tok in _SENSITIVE_CHAIN_CTX_TOKS
          )
          else ""
      )
      cur = match_end
      last_seen_end = match_end
      last_seen_conn = conn_text
      while cur < n:
        saw_ws_before_seg = False
        if acc_sensitive:
          while cur < n and text[cur] in (" ", "\t"):
            cur += 1
            saw_ws_before_seg = True
        if cur >= n or text[cur] not in _PATH_SEG_START_CHARS:
          break
        seg_m = _BARE_KV_RE.match(text, cur, min(n, cur + 256))
        if seg_m is None or seg_m.start("bare") != cur:
          break
        seg_bare = seg_m.group("bare") or ""
        seg_conn = seg_m.group("conn")
        seg_conn_stripped = seg_conn.strip()
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
          head_has_sep = (
              "/" in prev_seg
              or "." in prev_seg
              or last_seen_conn != ":"
              or chained
              or saw_ns_hop
          )
          is_tight_no_ws_head = not head_has_sep and not saw_ws_before_seg
          seg_is_metadata = norm_seg in _SAFE_CONTAINER_METADATA_KEYS
          seg_is_diag_exact = norm_seg in _SAFE_DIAGNOSTIC_SCALAR_KEYS
          seg_is_diag_suffix = norm_seg.endswith(_SAFE_SCALAR_KEY_SUFFIXES)
          seg_is_diag = seg_is_diag_exact or seg_is_diag_suffix
          seg_allows_eq_safe_tail = (
              seg_is_diag_exact
              or seg_is_metadata
              or (seg_is_diag_suffix and not is_tight_no_ws_head)
          )
          seg_is_safe_tail = not seg_is_container and (
              (
                  seg_is_diag
                  and seg_conn_stripped == ":"
                  and _has_colon_value_sep(text, seg_end)
              )
              or (
                  seg_allows_eq_safe_tail
                  and seg_conn_stripped == "="
                  and _has_eq_value(text, seg_end)
              )
          )
          if (
              not saw_tight_unknown_ns
              and seg_is_safe_tail
              and not is_sensitive_key(
                  seg_bare if seg_is_metadata else f"{prev_seg}:{seg_bare}",
                  is_container=seg_is_container,
              )
          ):
            return None, seg_conn, seg_end, 0, seg_end
          prev_tail = prev_seg.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
          prev_tail_norm = _normalize_key_name(prev_tail)
          if seg_conn_stripped == "=" and _has_eq_value(text, seg_end):
            seg_is_sens_leaf = is_sensitive_key(seg_bare, is_container=False)
            seg_bare_norm = _normalize_key_name(seg_bare)
            seg_is_safe_eq_leaf = not seg_is_sens_leaf and (
                seg_is_diag
                or seg_bare_norm in _SAFE_SMALL_COUNT_KEYS
                or seg_bare_norm in ("status", "reason", "result", "state")
            )
            if not (saw_tight_unknown_ns and seg_is_safe_eq_leaf) and (
                seg_is_sens_leaf
                or _is_canonical_colon_key_segment(
                    prev_tail,
                    seg_bare,
                    allow_suffix=True,
                    container_seg=ctx_container,
                )
                or (
                    (not saw_tight_unknown_ns or not saw_ws_before_seg)
                    and _is_explicit_container_eq_leaf(
                        seg_bare,
                        seg_conn,
                        text,
                        seg_end,
                        prev_tail_norm,
                    )
                )
            ):
              last_m = seg_m
              chained = True
              last_seen_end = seg_end
              last_seen_conn = seg_conn
            break
          tight_eq_ahead = (
              not saw_ws_before_seg
              and seg_conn == ":"
              and _has_tight_eq_leaf_ahead(text, seg_end, seg_bare, max_hops=12)
          )
          if (
              (not saw_tight_unknown_ns or tight_eq_ahead)
              and seg_conn_stripped == ":"
              and (orig_head_has_container_ctx or tight_eq_ahead)
          ):
            nested_cont = _extract_container_colon_head(seg_bare)
            if nested_cont:
              cont_res = _resolve_container_continuation(
                  text,
                  bare_start,
                  seg_end,
                  seg_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1],
                  nested_cont,
                  max_nl_hops=max_nl_hops,
                  head_has_path_sep=True,
                  head_has_unknown_ns=(
                      saw_tight_unknown_ns
                      or _has_unknown_path_segment(seg_bare)
                  ),
              )
              if cont_res is not None:
                return cont_res
              if (
                  not saw_tight_unknown_ns
                  and orig_head_has_container_ctx
                  and _has_colon_value_sep(text, seg_end)
              ):
                nc_val, _ = _skip_kv_whitespace(text, seg_end)
                if (
                    nc_val < n
                    and _CRITICAL_ERROR_MARKERS_RE.match(text, nc_val) is None
                ):
                  last_m = seg_m
                  chained = True
                  last_seen_end = seg_end
                  last_seen_conn = seg_conn
                  break
          seg_is_sens_leaf = is_sensitive_key(seg_bare, is_container=False)
          if (
              not saw_tight_unknown_ns
              and seg_conn_stripped == ":"
              and _has_colon_value_sep(text, seg_end)
              and (
                  seg_is_sens_leaf
                  or _is_canonical_colon_key_segment(
                      prev_tail,
                      seg_bare,
                      allow_suffix=True,
                      container_seg=ctx_container,
                  )
              )
          ):
            val_s, val_nl = _skip_kv_whitespace(text, seg_end)
            if val_s < n and (
                head_has_sep
                or saw_ws_before_seg
                or (seg_is_sens_leaf and val_nl == 1)
            ):
              is_quoted_or_container_val = text[val_s] in (
                  '"',
                  "'",
                  "{",
                  "[",
                  "(",
              ) or _starts_with_odd_escaped_quote(text, val_s)
              can_adopt_colon_leaf = False
              if val_nl == 0:
                can_adopt_colon_leaf = is_quoted_or_container_val or (
                    (saw_ns_hop or orig_head_has_container_ctx)
                    and _CRITICAL_ERROR_MARKERS_RE.match(text, val_s) is None
                )
              elif (
                  val_nl == 1
                  and max_nl_hops > 0
                  and (saw_ws_before_seg or ctx_container != "")
              ):
                can_adopt_colon_leaf = (
                    is_quoted_or_container_val
                    or _is_sensitive_header_at(
                        text, val_s, allow_mismatched_quote=True
                    )
                    or _is_next_line_raw_or_b64_eq(text, val_s)
                )
              if can_adopt_colon_leaf:
                last_m = seg_m
                chained = True
                last_seen_end = seg_end
                last_seen_conn = seg_conn
                break
          is_known_tight_ns, is_title_ns = _classify_colon_namespace_seg(
              seg_bare, norm_seg
          )
          is_auth_status_tail = (
              not saw_tight_unknown_ns
              and has_auth_status_ctx
              and norm_seg
              in (
                  "status",
                  "reason",
                  "result",
                  "state",
              )
          )
          is_tight_eq_hop = tight_eq_ahead and _is_tight_colon_ident(seg_bare)
          if (
              ns_hops < (12 if tight_eq_ahead else 4)
              and not (saw_tight_unknown_ns and saw_ws_before_seg)
              and seg_conn_stripped == ":"
              and (is_tight_eq_hop or not seg_is_sens_leaf)
              and (
                  is_tight_eq_hop
                  or is_known_tight_ns
                  or (is_auth_status_tail and saw_ws_before_seg)
                  or (
                      not saw_ws_before_seg
                      and acc_sensitive
                      and (seg_bare.islower() or is_title_ns)
                  )
              )
              and (
                  is_tight_eq_hop
                  or is_known_tight_ns
                  or _is_namespace_colon_path(seg_bare, allow_upper=True)
                  or (
                      is_title_ns
                      and (
                          is_known_tight_ns
                          or is_auth_status_tail
                          or (not saw_ws_before_seg and ctx_container != "")
                      )
                  )
              )
          ):
            is_known_count_tail = (
                not saw_tight_unknown_ns
                and _normalize_key_name(seg_bare) in _SAFE_SMALL_COUNT_KEYS
            )
            if not (
                is_known_tight_ns or is_auth_status_tail or is_known_count_tail
            ):
              has_auth_status_ctx = False
              if not saw_ws_before_seg:
                saw_tight_unknown_ns = True
                last_m = match
                chained = False
            elif not is_known_tight_ns:
              has_auth_status_ctx = False
            prev_seg = seg_bare
            cur = seg_end
            last_seen_end = seg_end
            last_seen_conn = seg_conn
            saw_ns_hop = True
            ns_hops += 1
            if is_known_tight_ns and not saw_tight_unknown_ns:
              if seg_end < n and text[seg_end] in (" ", "\t"):
                val_s, val_nl = _skip_kv_whitespace(text, seg_end)
                if (
                    val_nl == 0
                    and val_s < n
                    and _CRITICAL_ERROR_MARKERS_RE.match(text, val_s) is None
                ):
                  last_m = seg_m
                  chained = True
              elif max_nl_hops > 0:
                val_s, val_nl = _skip_kv_whitespace(text, seg_end)
                if (
                    val_nl == 1
                    and val_s < n
                    and (
                        text[val_s] in ('"', "'", "{", "[", "(")
                        or _starts_with_odd_escaped_quote(text, val_s)
                        or _is_sensitive_header_at(
                            text, val_s, allow_mismatched_quote=True
                        )
                        or (
                            _is_next_line_raw_or_b64_eq(text, val_s)
                            and _CRITICAL_ERROR_MARKERS_RE.match(text, val_s)
                            is None
                        )
                    )
                ):
                  last_m = seg_m
                  chained = True
            elif (
                (is_auth_status_tail or is_known_count_tail)
                and not saw_tight_unknown_ns
                and seg_end < n
                and text[seg_end] in (" ", "\t")
            ):
              val_s, val_nl = _skip_kv_whitespace(text, seg_end)
              if (
                  val_nl == 0
                  and val_s < n
                  and _CRITICAL_ERROR_MARKERS_RE.match(text, val_s) is None
              ):
                last_m = seg_m
                chained = True
            continue
          break
        seg_norm_full = _normalize_key_name(seg_bare)
        allow_tight_sens_leaf = (
            not saw_ws_before_seg
            and not seg_is_container
            and seg_norm_full not in _SAFE_SMALL_COUNT_KEYS
            and is_sensitive_key(seg_bare, is_container=False)
            and _is_canonical_colon_key_segment(
                prev_seg,
                seg_bare,
                allow_suffix=True,
                container_seg=ctx_container,
            )
        )
        if (
            not saw_tight_unknown_ns
            and is_sensitive_key(
                f"{prev_seg}:{seg_bare}", is_container=seg_is_container
            )
        ) or allow_tight_sens_leaf:
          acc_sensitive = True
          active_container = _extract_container_colon_head(seg_bare)
          orig_head_has_container_ctx = any(
              tok in seg_norm_full for tok in _SENSITIVE_CONTAINER_CTX_TOKS
          )
          has_auth_status_ctx = any(
              tok in seg_norm_full for tok in _AUTH_STATUS_CONTAINER_TOKS
          )
          ctx_container = (
              "auth"
              if any(tok in seg_norm_full for tok in _SENSITIVE_CHAIN_CTX_TOKS)
              else ""
          )
          last_m = seg_m
          prev_seg = seg_bare
          chained = True
          cur = seg_end
          last_seen_end = seg_end
          last_seen_conn = seg_conn
          if active_container and seg_conn_stripped == ":":
            prev_tail = seg_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
            cont_res = _resolve_container_continuation(
                text,
                bare_start,
                seg_end,
                prev_tail,
                active_container,
                max_nl_hops=max_nl_hops,
                head_has_path_sep=True,
                head_has_unknown_ns=_has_unknown_path_segment(seg_bare),
            )
            if cont_res is not None:
              return cont_res
            if max_nl_hops > 0:
              c_val, c_nl = _skip_kv_whitespace(text, seg_end)
              if c_nl == 1:
                nl_child = _resolve_container_next_line_child(
                    text,
                    bare_start,
                    c_val,
                    prev_tail,
                    active_container,
                    max_nl_hops=max_nl_hops - 1,
                    head_has_path_sep=True,
                    seen_containers=frozenset(
                        {_normalize_key_name(active_container)}
                    ),
                    nl_depth=1,
                )
                if nl_child is not None:
                  return nl_child
                if c_val < n and (
                    text[c_val] in ("{", "[", "(")
                    or _is_sensitive_header_at(
                        text,
                        c_val,
                        allow_mismatched_quote=True,
                    )
                ):
                  return (
                      text[bare_start : seg_m.end("bare")],
                      seg_conn,
                      c_val,
                      c_nl,
                      seg_end,
                  )
            break
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
      match_end + 1 < n
      and text[match_end] == " "
      and not text[match_end + 1].isspace()
      and text[match_end + 1] not in "\\{[("
  ):
    val_start = match_end + 1
    nl_count = 0
    is_container = False
  elif (
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
  if (
      not is_container
      and nl_count == 0
      and len(head_bare) <= 128
      and head_facts.is_sens_false
      and head_facts.lower_bare != "token"
      and not (
          head_facts.has_sep
          and text[match.start() : match.start("bare")].startswith("/")
      )
  ):
    return head_bare, conn_text, val_start, 0, match_end
  matched_key = _extract_bare_kv_key(text, match, is_container=is_container)
  if max_nl_hops > 0 and matched_key is not None and nl_count == 1:
    if is_head_container_colon:
      prev_tail = head_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
      active_container = _extract_container_colon_head(head_bare)
      nl_child = _resolve_container_next_line_child(
          text,
          match.start("bare"),
          val_start,
          prev_tail,
          active_container,
          max_nl_hops=max_nl_hops - 1,
          head_has_path_sep=(
              "." in head_bare or "/" in head_bare or conn_text != ":"
          ),
          seen_containers=frozenset({_normalize_key_name(active_container)}),
          nl_depth=1,
      )
      if nl_child is not None:
        return nl_child
    elif (
        conn_text == ":"
        and is_sensitive_key(head_bare, is_container=True)
        and val_start < n
        and text[val_start] in _PATH_SEG_START_CHARS
    ):
      nl_m = _BARE_KV_RE.match(text, val_start, min(n, val_start + 256))
      prev_tail = head_bare.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
      if (
          nl_m is not None
          and nl_m.start("bare") == val_start
          and _is_explicit_container_eq_leaf(
              nl_m.group("bare") or "",
              nl_m.group("conn"),
              text,
              nl_m.end(),
              _normalize_key_name(prev_tail),
          )
      ):
        nl_child = _resolve_container_next_line_child(
            text,
            match.start("bare"),
            val_start,
            prev_tail,
            head_bare,
            max_nl_hops=max_nl_hops - 1,
            head_has_path_sep="." in head_bare or "/" in head_bare,
            seen_containers=frozenset({_normalize_key_name(head_bare)}),
            nl_depth=1,
        )
        if nl_child is not None:
          return nl_child
  return matched_key, conn_text, val_start, nl_count, match_end


def _has_closed_quoted_or_container_value_at(text: str, val_start: int) -> bool:
  """Returns True when ``val_start`` starts a container or closed quoted val."""
  n = len(text)
  if val_start >= n:
    return False
  ch0 = text[val_start]
  if ch0 in ("{", "[", "("):
    return True
  if ch0 in ('"', "'"):
    return _scan_same_line_quote_end(text, val_start) is not None
  lead_bs = 0
  while val_start + lead_bs < n and text[val_start + lead_bs] == "\\":
    lead_bs += 1
  if lead_bs > 0 and lead_bs % 2 == 1 and val_start + lead_bs < n:
    quote_ch = text[val_start + lead_bs]
    if quote_ch == '"':
      end_tok, _, _ = _scan_same_line_escaped_dq_end(
          text, val_start + lead_bs - 1, lead_bs=lead_bs
      )
      return end_tok is not None
    if quote_ch == "'":
      close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
      idx = val_start + lead_bs + 1
      while idx < n:
        ch = text[idx]
        if ch in ("\r", "\n"):
          return False
        if ch == "\\":
          bs = 0
          while idx + bs < n and text[idx + bs] == "\\":
            bs += 1
          if idx + bs < n and text[idx + bs] == "'":
            if bs % period == close_mod:
              return True
            if ((bs + 1) & -(bs + 1)) < low_zero:
              return False
            idx += bs + 1
            continue
          idx += bs
          continue
        if ch == "'":
          return False
        idx += 1
  return False


def _is_valid_mismatched_quoted_header_key(decoded_key: str) -> bool:
  """Returns True when a mismatched-quote decoded key is a canonical header."""
  stripped = decoded_key.strip()
  if not stripped:
    return False
  parts: list[str] = []
  seg_start = 0
  for idx, ch in enumerate(stripped):
    if ch in (":", "/", "."):
      if idx == seg_start:
        return False
      parts.append(stripped[seg_start:idx])
      seg_start = idx + 1
  if seg_start == len(stripped):
    return False
  parts.append(stripped[seg_start:])
  if len(parts) >= 2:
    if not all(
        (p[0].isalnum() or p[0] == "_")
        and all(c.isalnum() or c in "_-" for c in p)
        for p in parts[:-1]
    ):
      return False
    leaf = parts[-1]
  else:
    leaf = parts[0]
  words = leaf.split(" ")
  if not (1 <= len(words) <= 8) or not all(words):
    return False
  if not (words[0][0].isalpha() or words[0][0] == "_"):
    return False
  if not any(c.isalpha() for c in leaf):
    return False
  for w in words:
    if not (
        (w[0].isalnum() or w[0] == "_")
        and all(c.isalnum() or c in "_-" for c in w)
    ):
      return False
  return True


def _is_structural_mismatched_lookalike(
    text: str,
    val_start: int,
    *,
    inner_end_tok: int | None = None,
    inner_close_start: int | None = None,
) -> bool:
  """Returns True when a mismatched-quote value is a structural lookalike."""
  n = len(text)
  if (
      inner_end_tok is not None
      and inner_close_start is not None
      and val_start < inner_close_start
  ):
    end_scan = min(inner_close_start, n)
    depth = 0
    in_single = False
    has_sep = False
    i = val_start
    while i < end_scan:
      ch = text[i]
      if ch == "\\":
        i += 2
        continue
      if in_single:
        if ch == "'":
          in_single = False
      elif ch == "'":
        in_single = True
      elif ch in ("(", "[", "{"):
        depth += 1
      elif ch in (")", "]", "}"):
        if depth == 0:
          if ch in ("]", "}"):
            has_sep = True
            break
        else:
          depth -= 1
      elif ch == "," and depth == 0:
        has_sep = True
        break
      i += 1
    if (
        not has_sep
        and _CRITICAL_ERROR_MARKERS_RE.search(text, val_start, end_scan) is None
    ):
      return True
  if (
      inner_end_tok is not None
      and val_start == inner_close_start
      and val_start < n
      and (
          text[val_start] in ('"', "'")
          or _starts_with_odd_escaped_quote(text, val_start)
      )
      and not _has_closed_quoted_or_container_value_at(text, val_start)
  ):
    if text[val_start] in ('"', "'"):
      q_after = val_start + 1
    else:
      bs = 0
      while val_start + bs < n and text[val_start + bs] == "\\":
        bs += 1
      q_after = val_start + bs + 1
    r = q_after
    while r < n and text[r] in (" ", "\t"):
      r += 1
    if r >= n or text[r] in ("\r", "\n"):
      return True
    tok_end = r
    while tok_end < n and (text[tok_end].isalnum() or text[tok_end] == "_"):
      tok_end += 1
    tok_r = text[r:tok_end]
    if tok_r and _CRITICAL_ERROR_MARKERS_RE.fullmatch(tok_r) is not None:
      after = tok_end
      while after < n and text[after] in (" ", "\t"):
        after += 1
      if after >= n or text[after] in ("\r", "\n", ",", "}", "]", '"'):
        return True
  return False


def _has_plain_json_string_continuation_close(
    text: str,
    line_end: int,
    *,
    quote_char: str = '"',
) -> tuple[bool, bool]:
  """Returns ``(found_close, reached_eof)`` for a JSON string continuation."""
  n = len(text)
  cont_memo = _ENCLOSING_CONT_MEMO.get()
  visited: list[tuple[int, bool]] = []
  seen_sensitive_hdr = False

  def _finish_cont(found: bool, eof: bool) -> tuple[bool, bool]:
    if cont_memo is not None:
      for l_pos, l_hdr in visited:
        cont_memo[(False, 0, quote_char, l_pos, l_hdr)] = (found, eof)
    return found, eof

  q = line_end
  while q < n:
    while q < n and text[q] in ("\r", "\n", " ", "\t"):
      q += 1
    if q >= n:
      return _finish_cont(False, not seen_sensitive_hdr)
    ckey = (False, 0, quote_char, q, seen_sensitive_hdr)
    if cont_memo is not None and ckey in cont_memo:
      return _finish_cont(*cont_memo[ckey])
    visited.append((q, seen_sensitive_hdr))
    if (
        (
            text[q] in ("}", "]")
            and _is_outer_json_after_brace(text, q + 1, is_escaped=False)
        )
        or (
            text[q] == ","
            and _is_json_comma_member_sep(text, q, is_escaped_dq=False)
        )
        or (
            text[q] == quote_char
            and _quote_opens_kv_at(text, q, escaped_dq=False)
        )
    ):
      return _finish_cont(False, False)
    seen_sensitive_hdr = seen_sensitive_hdr or _is_sensitive_header_at(
        text, q, allow_mismatched_quote=False
    )
    nl_pos = text.find("\n", q)
    if nl_pos != -1:
      cr_pos = text.find("\r", q, nl_pos)
      seg_end = cr_pos if cr_pos != -1 else nl_pos
    else:
      cr_pos = text.find("\r", q)
      seg_end = cr_pos if cr_pos != -1 else n
    search_from = q
    while True:
      q_pos = text.find(quote_char, search_from, seg_end)
      if q_pos == -1:
        break
      if (
          q_pos > 0
          and text[q_pos - 1] == "\\"
          and _count_backslashes_ending_at(text, q_pos - 1) % 2 == 1
      ):
        search_from = q_pos + 1
        continue
      r = q_pos + 1
      if not seen_sensitive_hdr:
        while r < n and text[r] in (" ", "\t"):
          r += 1
        if r >= n or text[r] in (",", "}", "]", "\r", "\n"):
          return _finish_cont(True, False)
      else:
        while r < n and text[r] in (" ", "\t", "\r", "\n"):
          r += 1
        if r < n and (
            (
                text[r] == ","
                and _is_json_comma_member_sep(text, r, is_escaped_dq=False)
            )
            or (
                text[r] in ("}", "]")
                and _is_outer_json_after_brace(text, r + 1, is_escaped=False)
            )
        ):
          return _finish_cont(True, False)
        if r > seg_end:
          break
      search_from = r
    if seg_end >= n:
      return _finish_cont(False, not seen_sensitive_hdr)
    q = seg_end + 1
  return _finish_cont(False, not seen_sensitive_hdr)


def _is_escaped_json_close_tail(
    text: str, pos: int, n: int, *, nl_bs: int
) -> bool:
  """Returns True if ``pos`` begins a valid tail after a closing quote."""
  r = pos
  r_limit = min(n, pos + 256)
  while r < r_limit and text[r] in (" ", "\t"):
    r += 1
  if r >= n:
    return True
  if r < r_limit and text[r] in (",", "}", "]", "\r", "\n"):
    return True
  if r + nl_bs < n and text[r] == "\\":
    bs = 0
    while r + bs < r_limit and text[r + bs] == "\\":
      bs += 1
    if (bs == 1 or bs == nl_bs) and r + bs < n and text[r + bs] in ("n", "r"):
      return True
  return False


def _is_outer_json_after_brace(
    text: str,
    pos: int,
    *,
    is_escaped: bool,
    allow_sensitive_header: bool = False,
    allow_open_brace: bool = True,
    nl_bs: int = 1,
    allow_open_bracket: bool = True,
) -> bool:
  """Returns True if ``pos`` after ``}``/``]`` looks like outer JSON."""
  n = len(text)
  p = pos
  while p < n:
    ch = text[p]
    if ch in (" ", "\t", "\r", "\n"):
      p += 1
      continue
    if is_escaped and ch == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if (
          (bs % 2 == 1 or bs == nl_bs)
          and p + bs < n
          and text[p + bs] in ("n", "r")
      ):
        p += bs + 1
        continue
    break
  if p >= n or text[p] in ("}", "]", ","):
    return True
  if allow_open_bracket and text[p] == "[":
    return True
  if allow_open_brace and text[p] == "{":
    return True
  return allow_sensitive_header and _is_sensitive_header_at(
      text, p, allow_mismatched_quote=False
  )


def _is_json_comma_member_sep(
    text: str, comma_pos: int, *, is_escaped_dq: bool, nl_bs: int = 1
) -> bool:
  """Returns True when a comma at ``comma_pos`` separates JSON/dict members."""
  n = len(text)
  k = comma_pos + 1
  while k < n and text[k] in (" ", "\t"):
    k += 1
  if k < n and text[k] in ("\r", "\n"):
    if text[k] == "\r" and k + 1 < n and text[k + 1] == "\n":
      k += 2
    else:
      k += 1
  elif is_escaped_dq and k < n and text[k] == "\\":
    bs = 0
    while k + bs < n and text[k + bs] == "\\":
      bs += 1
    if (
        (bs % 2 == 1 or bs == nl_bs)
        and k + bs < n
        and text[k + bs] in ("n", "r")
    ):
      ch_nl = text[k + bs]
      k += bs + 1
      if ch_nl == "r" and k < n and text[k] == "\\":
        bs2 = 0
        while k + bs2 < n and text[k + bs2] == "\\":
          bs2 += 1
        if (
            (bs2 % 2 == 1 or bs2 == nl_bs)
            and k + bs2 < n
            and text[k + bs2] == "n"
        ):
          k += bs2 + 1
  while k < n and text[k] in (" ", "\t"):
    k += 1
  if k >= n:
    return False
  if text[k] in ("}", "]"):
    return _is_outer_json_after_brace(
        text, k + 1, is_escaped=is_escaped_dq, nl_bs=nl_bs
    )
  if is_escaped_dq:
    if text[k] == "\\":
      bs = 0
      while k + bs < n and text[k + bs] == "\\":
        bs += 1
      if bs % 2 == 1 and k + bs < n:
        if text[k + bs] == '"':
          return _quote_opens_kv_at(text, k + bs - 1, escaped_dq=True)
        if text[k + bs] == "'":
          found = _scan_mismatched_quote_key_kv(text, k + bs + 1)
          return (
              found is not None
              and text[found[1]] == "\\"
              and found[2].start() - found[1] == 2
          )
    if text[k] in ('"', "'") and _quote_opens_kv_at(text, k, escaped_dq=False):
      return True
    return False
  return text[k] in ('"', "'") and _quote_opens_kv_at(text, k, escaped_dq=False)


def _scan_same_line_escaped_sq_end(
    text: str,
    after_open: int,
    *,
    close_mod: int,
    period: int,
    low_zero: int,
    nl_bs: int,
) -> int | None:
  """Returns index after matching-level escaped single quote on the same line."""
  n = len(text)
  nl_pos = text.find("\n", after_open)
  if nl_pos != -1:
    cr_pos = text.find("\r", after_open, nl_pos)
    line_end = cr_pos if cr_pos != -1 else nl_pos
  else:
    cr_pos = text.find("\r", after_open)
    line_end = cr_pos if cr_pos != -1 else n
  cur = after_open
  bs_pos = text.find("\\", cur, line_end)
  q_pos = text.find("'", cur, line_end)
  while cur < line_end:
    if bs_pos != -1 and bs_pos < cur:
      bs_pos = text.find("\\", cur, line_end)
    if q_pos != -1 and q_pos < cur:
      q_pos = text.find("'", cur, line_end)
    if bs_pos == -1 and q_pos == -1:
      return None
    if q_pos != -1 and (bs_pos == -1 or q_pos < bs_pos):
      return None
    bs = 0
    while bs_pos + bs < line_end and text[bs_pos + bs] == "\\":
      bs += 1
    if bs_pos + bs < line_end and text[bs_pos + bs] == "'":
      if bs % period == close_mod:
        return bs_pos + bs + 1
      if ((bs + 1) & -(bs + 1)) < low_zero:
        return None
      cur = bs_pos + bs + 1
      continue
    if (
        (bs == 1 or bs == nl_bs)
        and bs_pos + bs < line_end
        and text[bs_pos + bs] in ("n", "r")
    ):
      return None
    cur = min(line_end, bs_pos + (bs if bs % 2 == 0 else bs + 1))
  return None


def _is_enclosing_json_string_literal(
    text: str,
    val_start: int,
    *,
    is_escaped_dq: bool,
    lead_bs: int = 0,
    quote_char: str = '"',
    enclosing_fail_from: dict[tuple[bool, int, str], int] | None = None,
    allow_same_line: bool = True,
) -> bool:
  """Returns True when a mismatched header sits inside a JSON string value."""
  n = len(text)
  if val_start >= n:
    return False
  key = (
      is_escaped_dq,
      lead_bs if allow_same_line else -(lead_bs + 1),
      quote_char,
  )
  if enclosing_fail_from is not None and val_start >= enclosing_fail_from.get(
      key, n + 1
  ):
    return False
  alt_q = "'" if quote_char == '"' else '"'
  if not is_escaped_dq:
    nl_pos = text.find("\n", val_start)
    if nl_pos != -1:
      cr_pos = text.find("\r", val_start, nl_pos)
      line_end = cr_pos if cr_pos != -1 else nl_pos
    else:
      cr_pos = text.find("\r", val_start)
      line_end = cr_pos if cr_pos != -1 else n
    p = val_start
    if text[val_start] == quote_char:
      r = val_start + 1
      while r < line_end and text[r] in (" ", "\t"):
        r += 1
      if allow_same_line and r < line_end and text[r] in (",", "}", "]"):
        return True
      v_end = _scan_same_line_quote_end(text, val_start)
      if v_end is not None and v_end <= line_end:
        p = v_end
        while p < line_end and text[p] in (" ", "\t"):
          p += 1
        if p == line_end:
          if line_end >= n:
            return False
        elif text[p] in ("}", "]"):
          if allow_same_line and _is_outer_json_after_brace(
              text, p + 1, is_escaped=False
          ):
            return False
          p += 1
        elif text[p] == ",":
          if allow_same_line and _is_json_comma_member_sep(
              text, p, is_escaped_dq=False
          ):
            return False
          p += 1
        else:
          p = v_end
      else:
        if r >= line_end:
          if allow_same_line:
            return True
          if line_end >= n:
            return False
          p = line_end
        else:
          p = val_start + 1
    line1_start = p
    in_alt_q = False
    saw_alt_q = False
    depth = 0
    while p < line_end:
      ch = text[p]
      if ch == "\\":
        p += 2
        continue
      if ch == alt_q:
        in_alt_q = not in_alt_q
        saw_alt_q = True
        p += 1
        continue
      if not in_alt_q:
        if ch == quote_char:
          if allow_same_line:
            return True
          r = p + 1
          while r < line_end and text[r] in (" ", "\t"):
            r += 1
          if r >= line_end:
            return False
          p += 1
          continue
        if ch in ("(", "[", "{"):
          depth += 1
        elif ch in (")", "]", "}"):
          if depth > 0:
            depth -= 1
          elif (
              allow_same_line
              and ch in ("]", "}")
              and _is_outer_json_after_brace(text, p + 1, is_escaped=False)
          ):
            return False
        elif allow_same_line and depth == 0 and ch == ",":
          if _is_json_comma_member_sep(text, p, is_escaped_dq=False):
            return False
      p += 1
    if line_end < n and text[line_end] in ("\r", "\n"):
      found_close, reached_eof = _has_plain_json_string_continuation_close(
          text, line_end, quote_char=quote_char
      )
      if reached_eof and enclosing_fail_from is not None:
        prev = enclosing_fail_from.get(key)
        if prev is None or line_end < prev:
          enclosing_fail_from[key] = line_end
      return found_close
    if not saw_alt_q and enclosing_fail_from is not None:
      prev = enclosing_fail_from.get(key)
      if prev is None or line1_start < prev:
        enclosing_fail_from[key] = line1_start
    return False
  if lead_bs <= 0:
    return False
  close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
  nl_bs = max(1, low_zero // 2)
  p = val_start
  bs_at_val = 0
  while val_start + bs_at_val < n and text[val_start + bs_at_val] == "\\":
    bs_at_val += 1
  if (
      bs_at_val > 0
      and val_start + bs_at_val < n
      and text[val_start + bs_at_val] == quote_char
      and bs_at_val % period == close_mod
  ):
    if quote_char == '"':
      v_end, _, _ = _scan_same_line_escaped_dq_end(
          text, val_start + bs_at_val - 1, lead_bs=lead_bs
      )
    else:
      v_end = _scan_same_line_escaped_sq_end(
          text,
          val_start + bs_at_val + 1,
          close_mod=close_mod,
          period=period,
          low_zero=low_zero,
          nl_bs=nl_bs,
      )
    if v_end is not None:
      r = val_start + bs_at_val + 1
      while r < n and text[r] in (" ", "\t"):
        r += 1
      if allow_same_line and r < n and text[r] in (",", "}", "]"):
        return True
      p = v_end
      while p < n and text[p] in (" ", "\t"):
        p += 1
      if p >= n:
        return False
      if text[p] in ("}", "]"):
        if allow_same_line and _is_outer_json_after_brace(
            text, p + 1, is_escaped=True, nl_bs=nl_bs
        ):
          return False
        p += 1
      else:
        is_eol_after = text[p] in ("\r", "\n")
        if not is_eol_after and text[p] == "\\":
          bs_after = 0
          while p + bs_after < n and text[p + bs_after] == "\\":
            bs_after += 1
          if (
              (bs_after == 1 or bs_after == nl_bs)
              and p + bs_after < n
              and text[p + bs_after] in ("n", "r")
          ):
            is_eol_after = True
        if not is_eol_after:
          if text[p] == ",":
            if allow_same_line and _is_json_comma_member_sep(
                text, p, is_escaped_dq=True, nl_bs=nl_bs
            ):
              return False
            p += 1
          else:
            p = v_end
    else:
      after_q = val_start + bs_at_val + 1
      if allow_same_line and _is_escaped_json_close_tail(
          text, after_q, n, nl_bs=nl_bs
      ):
        return True
      p = after_q
  line1_start = p
  on_first_line = True
  in_alt_q = False
  saw_alt_q = False
  depth = 0
  while p < n:
    ch = text[p]
    if ch in ("\r", "\n"):
      p += 1
      on_first_line = False
      break
    if ch == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if p + bs < n and text[p + bs] == quote_char:
        if bs % period == close_mod or ((bs + 1) & -(bs + 1)) < low_zero:
          close_end = p + bs + 1
          if allow_same_line:
            return _is_escaped_json_close_tail(text, close_end, n, nl_bs=nl_bs)
          r = close_end
          r_limit = min(n, close_end + 256)
          while r < r_limit and text[r] in (" ", "\t"):
            r += 1
          if r >= n:
            return False
          p = close_end
          continue
        p += bs + 1
        continue
      if (bs == 1 or bs == nl_bs) and p + bs < n and text[p + bs] in ("n", "r"):
        p += bs + 1
        on_first_line = False
        break
      p += bs if bs % 2 == 0 else bs + 1
      continue
    if ch == quote_char:
      close_end = p + 1
      if allow_same_line:
        return _is_escaped_json_close_tail(text, close_end, n, nl_bs=nl_bs)
      r = close_end
      r_limit = min(n, close_end + 256)
      while r < r_limit and text[r] in (" ", "\t"):
        r += 1
      if r >= n:
        return False
      p = close_end
      continue
    if ch == alt_q:
      in_alt_q = not in_alt_q
      saw_alt_q = True
      p += 1
      continue
    if not in_alt_q:
      if ch in ("(", "[", "{"):
        depth += 1
      elif ch in (")", "]", "}"):
        if depth > 0:
          depth -= 1
        elif (
            allow_same_line
            and ch in ("]", "}")
            and _is_outer_json_after_brace(
                text, p + 1, is_escaped=True, nl_bs=nl_bs
            )
        ):
          return False
      elif allow_same_line and depth == 0 and ch == ",":
        if _is_json_comma_member_sep(text, p, is_escaped_dq=True, nl_bs=nl_bs):
          return False
    p += 1
  if on_first_line:
    if not saw_alt_q and enclosing_fail_from is not None:
      prev = enclosing_fail_from.get(key)
      if prev is None or line1_start < prev:
        enclosing_fail_from[key] = line1_start
    return False
  cont_start = p
  cont_memo = _ENCLOSING_CONT_MEMO.get()
  esc_visited: list[tuple[int, bool]] = []
  at_line_start = True
  seen_sensitive_hdr = False

  def _finish_esc_cont(ans: bool, eof: bool = False) -> bool:
    if cont_memo is not None:
      for l_pos, l_hdr in esc_visited:
        cont_memo[(True, low_zero, quote_char, l_pos, l_hdr)] = (ans, eof)
    if eof and not ans and enclosing_fail_from is not None:
      prev = enclosing_fail_from.get(key)
      if prev is None or cont_start < prev:
        enclosing_fail_from[key] = cont_start
    return ans

  def _escaped_hdr_quote_closes_at(close_end: int) -> tuple[bool, int, bool]:
    r = close_end
    crossed_nl = False
    while r < n:
      ch_r = text[r]
      if ch_r in (" ", "\t", "\r", "\n"):
        if ch_r in ("\r", "\n"):
          crossed_nl = True
        r += 1
        continue
      if ch_r == "\\":
        bs_r = 0
        while r + bs_r < n and text[r + bs_r] == "\\":
          bs_r += 1
        if (
            (bs_r == 1 or bs_r == nl_bs)
            and r + bs_r < n
            and text[r + bs_r] in ("n", "r", "t")
        ):
          if text[r + bs_r] in ("n", "r"):
            crossed_nl = True
          r += bs_r + 1
          continue
      break
    closes = r < n and (
        (
            text[r] == ","
            and _is_json_comma_member_sep(
                text, r, is_escaped_dq=True, nl_bs=nl_bs
            )
        )
        or (
            text[r] in ("}", "]")
            and _is_outer_json_after_brace(
                text, r + 1, is_escaped=True, nl_bs=nl_bs
            )
        )
    )
    return closes, r, crossed_nl

  while p < n:
    if at_line_start:
      while p < n and text[p] in ("\r", "\n", " ", "\t"):
        p += 1
      if p >= n:
        return _finish_esc_cont(False, not seen_sensitive_hdr)
      ckey = (True, low_zero, quote_char, p, seen_sensitive_hdr)
      if cont_memo is not None and ckey in cont_memo:
        c_ans, c_eof = cont_memo[ckey]
        return _finish_esc_cont(c_ans, c_eof)
      esc_visited.append((p, seen_sensitive_hdr))
      if (
          text[p] in ("}", "]")
          and _is_outer_json_after_brace(
              text, p + 1, is_escaped=True, nl_bs=nl_bs
          )
      ) or (
          text[p] == ","
          and _is_json_comma_member_sep(
              text, p, is_escaped_dq=True, nl_bs=nl_bs
          )
      ):
        return _finish_esc_cont(False, False)
      if text[p] in ('"', "'") and _quote_opens_kv_at(
          text, p, escaped_dq=False
      ):
        return _finish_esc_cont(False, False)
      if text[p] == "\\":
        bs_ls = 0
        while p + bs_ls < n and text[p + bs_ls] == "\\":
          bs_ls += 1
        if (
            bs_ls % 2 == 1
            and p + bs_ls < n
            and text[p + bs_ls] == '"'
            and _quote_opens_kv_at(text, p + bs_ls - 1, escaped_dq=True)
        ):
          return _finish_esc_cont(False, False)
      seen_sensitive_hdr = seen_sensitive_hdr or _is_sensitive_header_at(
          text, p, allow_mismatched_quote=False
      )
      at_line_start = False
    nl_pos = text.find("\n", p)
    if nl_pos != -1:
      cr_pos = text.find("\r", p, nl_pos)
      seg_end = cr_pos if cr_pos != -1 else nl_pos
    else:
      cr_pos = text.find("\r", p)
      seg_end = cr_pos if cr_pos != -1 else n
    cur = p
    bs_pos = text.find("\\", cur, seg_end)
    q_pos = text.find(quote_char, cur, seg_end)
    while cur < seg_end:
      if bs_pos != -1 and bs_pos < cur:
        bs_pos = text.find("\\", cur, seg_end)
      if q_pos != -1 and q_pos < cur:
        q_pos = text.find(quote_char, cur, seg_end)
      if bs_pos == -1 and q_pos == -1:
        break
      if q_pos != -1 and (bs_pos == -1 or q_pos < bs_pos):
        if not seen_sensitive_hdr:
          return _finish_esc_cont(
              _is_escaped_json_close_tail(text, q_pos + 1, n, nl_bs=nl_bs),
              False,
          )
        closes, next_r, crossed_nl = _escaped_hdr_quote_closes_at(q_pos + 1)
        if closes:
          return _finish_esc_cont(True, False)
        if crossed_nl:
          p = next_r
          at_line_start = True
          break
        if next_r > seg_end:
          break
        cur = next_r
        continue
      bs = 0
      while bs_pos + bs < seg_end and text[bs_pos + bs] == "\\":
        bs += 1
      if bs_pos + bs < seg_end and text[bs_pos + bs] == quote_char:
        if bs % period == close_mod or ((bs + 1) & -(bs + 1)) < low_zero:
          close_end = bs_pos + bs + 1
          if not seen_sensitive_hdr:
            return _finish_esc_cont(
                _is_escaped_json_close_tail(text, close_end, n, nl_bs=nl_bs),
                False,
            )
          closes, next_r, crossed_nl = _escaped_hdr_quote_closes_at(close_end)
          if closes:
            return _finish_esc_cont(True, False)
          if crossed_nl:
            p = next_r
            at_line_start = True
            break
          if next_r > seg_end:
            break
          cur = next_r
          continue
        cur = bs_pos + bs + 1
        continue
      if (
          (bs == 1 or bs == nl_bs)
          and bs_pos + bs < seg_end
          and text[bs_pos + bs] in ("n", "r")
      ):
        p = bs_pos + bs + 1
        at_line_start = True
        break
      cur = min(seg_end, bs_pos + (bs if bs % 2 == 0 else bs + 1))
    if at_line_start:
      continue
    if seg_end >= n:
      return _finish_esc_cont(False, not seen_sensitive_hdr)
    p = seg_end + 1
    at_line_start = True
  return _finish_esc_cont(False, not seen_sensitive_hdr)


def _is_sensitive_header_bare_at(text: str, pos: int) -> bool:
  """Returns True when ``pos`` begins an unquoted bare sensitive KV header."""
  n = len(text)
  if (
      pos >= 2
      and text[pos - 2 : pos] in ("\\t", "\\r", "\\n")
      and _count_backslashes_ending_at(text, pos - 2) % 2 == 0
  ):
    return False
  bare_m = _BARE_KV_RE.match(text, pos)
  if bare_m is not None:
    matched_key, _, _, _, _ = _resolve_bare_or_path_kv(
        text, bare_m, max_nl_hops=0
    )
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
          text, cand_m, max_nl_hops=0
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


def _is_sensitive_header_at(
    text: str,
    pos: int,
    *,
    allow_mismatched_quote: bool = True,
    is_quoted_parent: bool = False,
    check_enclosing_quote: bool | None = None,
    enclosing_fail_from: dict[tuple[bool, int, str], int] | None = None,
    out_enclosed_match: list[bool] | None = None,
) -> bool:
  """Returns True when ``pos`` begins a sensitive KV header on the next line."""
  n = len(text)
  if pos >= n:
    return False
  if not is_quoted_parent and text[pos] not in ('"', "'", "\\"):
    hdr_cache = _UNQUOTED_SENS_HDR_CACHE.get()
    if hdr_cache is not None:
      cached_hit = hdr_cache.get(pos)
      if cached_hit is not None:
        return cached_hit
      res = _is_sensitive_header_bare_at(text, pos)
      if len(hdr_cache) >= 32768:
        hdr_cache.clear()
      hdr_cache[pos] = res
      return res
    return _is_sensitive_header_bare_at(text, pos)
  check_enclosing = (
      is_quoted_parent
      if check_enclosing_quote is None
      else check_enclosing_quote
  )
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
    inner_end_tok = end_tok
    inner_close_start: int | None = close_start if end_tok is not None else None
    conn_m: re.Match[str] | None = None
    used_mismatched = False
    if end_tok is not None:
      conn_m = _QUOTED_CONN_RE.match(text, end_tok)
      if (
          conn_m is None
          and allow_mismatched_quote
          and _can_try_mismatched_quote_before(
              text,
              text[edq_start + 2 : close_start],
              end_tok,
              end_tok - 2,
              escaped_dq=True,
              allow_error_marker_tail=not is_quoted_parent,
          )
      ):
        found = _scan_mismatched_quote_key_kv(text, edq_start + 2)
        if found is not None:
          _, close_start, conn_m = found
          used_mismatched = True
    elif allow_mismatched_quote:
      closed_on_line = (
          stop_pos < n and text[stop_pos] == '"' and close_start > edq_start + 2
      )
      if closed_on_line:
        inner_end_tok = stop_pos + 1
        inner_close_start = stop_pos
        conn_m = _QUOTED_CONN_RE.match(text, stop_pos + 1)
        if conn_m is not None:
          used_mismatched = True
      if conn_m is None:
        if closed_on_line:
          is_escaped_close = close_start < stop_pos
          raw_end = close_start if is_escaped_close else stop_pos
          can_try_mismatched = _can_try_mismatched_quote_before(
              text,
              text[edq_start + 2 : raw_end],
              stop_pos + 1,
              stop_pos - 1 if is_escaped_close else stop_pos,
              escaped_dq=is_escaped_close,
              allow_error_marker_tail=not is_quoted_parent,
          )
        else:
          can_try_mismatched = True
        if can_try_mismatched:
          found = _scan_mismatched_quote_key_kv(text, edq_start + 2)
          if found is not None:
            _, close_start, conn_m = found
            used_mismatched = True
    if conn_m is None:
      return False
    decoded_key = _decode_escaped_quoted_kv_key(
        text[edq_start + 2 : close_start]
    )
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    if used_mismatched:
      if not _is_valid_mismatched_quoted_header_key(decoded_key):
        return False
      if _is_structural_mismatched_lookalike(
          text,
          val_start,
          inner_end_tok=inner_end_tok,
          inner_close_start=inner_close_start,
      ):
        return False
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    if _is_quoted_header_value_prose(text, conn_m.group(0), val_start):
      return False
    if (
        used_mismatched
        and check_enclosing
        and _is_enclosing_json_string_literal(
            text,
            val_start,
            is_escaped_dq=True,
            lead_bs=lead_bs,
            quote_char='"',
            enclosing_fail_from=enclosing_fail_from,
            allow_same_line=is_quoted_parent,
        )
    ):
      if out_enclosed_match is not None:
        out_enclosed_match.append(True)
      return False
    return True
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
    is_matched_esq = (
        text[close_start] == "\\" and conn_m.start() - close_start == 2
    )
    if not is_matched_esq and not allow_mismatched_quote:
      return False
    decoded_key = _decode_escaped_quoted_kv_key(text[inner_start:close_start])
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    if not is_matched_esq:
      if not _is_valid_mismatched_quoted_header_key(decoded_key):
        return False
      if _is_structural_mismatched_lookalike(text, val_start):
        return False
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    if _is_quoted_header_value_prose(text, conn_m.group(0), val_start):
      return False
    if (
        not is_matched_esq
        and check_enclosing
        and _is_enclosing_json_string_literal(
            text,
            val_start,
            is_escaped_dq=True,
            lead_bs=lead_bs,
            quote_char="'",
            enclosing_fail_from=enclosing_fail_from,
            allow_same_line=is_quoted_parent,
        )
    ):
      if out_enclosed_match is not None:
        out_enclosed_match.append(True)
      return False
    return True
  if text[pos] in ('"', "'"):
    ch = text[pos]
    end_tok = _scan_same_line_quote_end(text, pos)
    conn_m = (
        _QUOTED_CONN_RE.match(text, end_tok) if end_tok is not None else None
    )
    used_mismatched = False
    if conn_m is not None:
      decoded_key = (
          _decode_quoted_kv_key(text[pos:end_tok])
          if ch == '"'
          else text[pos + 1 : end_tok - 1]
      )
    elif allow_mismatched_quote and (
        end_tok is None
        or _can_try_mismatched_quote_before(
            text,
            text[pos + 1 : end_tok - 1],
            end_tok,
            end_tok - 1,
            escaped_dq=False,
            allow_error_marker_tail=not is_quoted_parent,
        )
    ):
      found = _scan_mismatched_quote_key_kv(text, pos + 1)
      if found is None:
        return False
      _, close_start, conn_m = found
      raw_inner = text[pos + 1 : close_start]
      decoded_key = (
          _decode_quoted_kv_key(f'"{raw_inner}"') if ch == '"' else raw_inner
      )
      used_mismatched = True
    else:
      return False
    val_start, _ = _skip_kv_whitespace(text, conn_m.end())
    if used_mismatched:
      if not _is_valid_mismatched_quoted_header_key(decoded_key):
        return False
      if _is_structural_mismatched_lookalike(
          text,
          val_start,
          inner_end_tok=end_tok,
          inner_close_start=end_tok - 1 if end_tok is not None else None,
      ):
        return False
    is_container = val_start < n and text[val_start] in ("{", "[", "(")
    if not is_sensitive_key(decoded_key, is_container=is_container):
      return False
    if _is_quoted_header_value_prose(text, conn_m.group(0), val_start):
      return False
    if (
        used_mismatched
        and check_enclosing
        and _is_enclosing_json_string_literal(
            text,
            val_start,
            is_escaped_dq=False,
            quote_char=ch,
            enclosing_fail_from=enclosing_fail_from,
            allow_same_line=is_quoted_parent,
        )
    ):
      if out_enclosed_match is not None:
        out_enclosed_match.append(True)
      return False
    return True
  return _is_sensitive_header_bare_at(text, pos)


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


def _looks_like_json_object_open(
    text: str,
    pos: int,
    *,
    is_escaped: bool,
    nl_bs: int = 1,
) -> bool:
  """Returns True when ``{`` at ``pos`` opens a JSON object."""
  n = len(text)
  k = pos + 1
  while k < n:
    ch = text[k]
    if ch in (" ", "\t", "\r", "\n"):
      k += 1
      continue
    if is_escaped and ch == "\\":
      bs = 0
      while k + bs < n and text[k + bs] == "\\":
        bs += 1
      if (
          (bs == 1 or bs == nl_bs)
          and k + bs < n
          and text[k + bs] in ("t", "r", "n")
      ):
        k += bs + 1
        continue
    break
  if k >= n:
    return False
  if text[k] == "}":
    return True
  if is_escaped:
    if text[k] == "\\":
      bs = 0
      while k + bs < n and text[k + bs] == "\\":
        bs += 1
      if bs % 2 == 1 and k + bs < n and text[k + bs] == '"':
        return _quote_opens_kv_at(text, k + bs - 1, escaped_dq=True)
    if text[k] in ('"', "'"):
      return _quote_opens_kv_at(text, k, escaped_dq=False)
    return False
  return text[k] in ('"', "'") and _quote_opens_kv_at(text, k, escaped_dq=False)


def _resolve_json_object_close_pos(
    text: str,
    brace_pos: int,
    *,
    is_escaped: bool,
    low_zero: int,
    nl_bs: int,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
) -> int | None:
  """Returns the closing brace index for a JSON object starting at brace_pos."""
  close_pos: int | None = None
  if brace_index_cache is not None:
    cache_key = (is_escaped, low_zero)
    cached = brace_index_cache.get(cache_key)
    if cached is None:
      cached = _build_json_brace_index(
          text, is_escaped=is_escaped, low_zero=low_zero
      )
      brace_index_cache[cache_key] = cached
    close_pos = cached[1].get(brace_pos)
  if close_pos is None and _looks_like_json_object_open(
      text, brace_pos, is_escaped=is_escaped, nl_bs=nl_bs
  ):
    return _find_balanced_json_object_close_at(
        text,
        brace_pos,
        is_escaped=is_escaped,
        low_zero=low_zero,
        nl_bs=nl_bs,
    )
  return close_pos


def _is_balanced_outer_prose_after_brace(
    text: str,
    pos: int,
    *,
    quote_char: str,
    is_escaped: bool,
    lead_bs: int = 0,
    nl_bs: int = 1,
    prose_memo: dict[tuple[bool, int, str], dict[int, bool]] | None = None,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
) -> bool:
  """Returns True when prose after ``}``/``]`` has no lone closing quote."""
  n = len(text)
  close_mod = period = low_zero = 0
  if is_escaped:
    if lead_bs <= 0:
      return False
    close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)

  def _memo_key(idx: int, saw_esc: bool) -> int:
    return (idx << 1) | int(saw_esc)

  memo: dict[int, bool] | None = None
  start_key = _memo_key(pos, False)
  if prose_memo is not None:
    mkey = (is_escaped, low_zero, quote_char)
    memo = prose_memo.setdefault(mkey, {})
    if start_key in memo:
      return memo[start_key]
  checkpoints: list[int] = []
  active_brace_cache = (
      brace_index_cache if brace_index_cache is not None else {}
  )

  def _finish(ans: bool) -> bool:
    if memo is not None:
      memo[start_key] = ans
      for cp_key in checkpoints:
        memo[cp_key] = ans
    return ans

  in_quote = False
  saw_outer_escaped_q = False
  p = pos
  while p < n:
    ch = text[p]
    if ch in ("\r", "\n"):
      in_quote = False
      saw_outer_escaped_q = False
      p += 1
      continue
    if not in_quote:
      if ch in ("}", "]"):
        if memo is not None:
          ck = _memo_key(p + 1, saw_outer_escaped_q)
          if ck in memo:
            return _finish(memo[ck])
          checkpoints.append(ck)
      elif ch == "{" and _is_plausible_json_container_start(
          text, p, is_escaped=is_escaped, nl_bs=nl_bs
      ):
        m_close = _resolve_json_object_close_pos(
            text,
            p,
            is_escaped=is_escaped,
            low_zero=low_zero,
            nl_bs=nl_bs,
            brace_index_cache=active_brace_cache,
        )
        if m_close is not None:
          cp = m_close + 1
          if memo is not None:
            ck = _memo_key(cp, saw_outer_escaped_q)
            if ck in memo:
              return _finish(memo[ck])
            checkpoints.append(ck)
          p = cp
          continue
        if _looks_like_json_object_open(
            text, p, is_escaped=is_escaped, nl_bs=nl_bs
        ):
          return _finish(True)
    if not is_escaped:
      if ch == "\\":
        bs = 0
        while p + bs < n and text[p + bs] == "\\":
          bs += 1
        if p + bs < n and text[p + bs] == quote_char:
          if bs % 2 == 1:
            if not in_quote:
              saw_outer_escaped_q = True
            p += bs + 1
          else:
            p += bs
          continue
        if p + bs < n and text[p + bs] in ("\r", "\n"):
          in_quote = False
          saw_outer_escaped_q = False
          p += bs + 1
          continue
        p += bs if bs % 2 == 0 else min(n - p, bs + 1)
        continue
      if ch == quote_char:
        if (not in_quote or saw_outer_escaped_q) and _is_valid_json_member_end(
            text,
            p + 1,
            is_escaped=False,
            nl_bs=nl_bs,
            brace_index_cache=active_brace_cache,
        ):
          return _finish(False)
        if not in_quote and _quote_opens_kv_at(text, p, escaped_dq=False):
          saw_outer_escaped_q = False
        in_quote = not in_quote
      p += 1
      continue
    if ch == '"' and quote_char == '"':
      return _finish(True)
    if ch == "\\":
      bs = 0
      while p + bs < n and text[p + bs] == "\\":
        bs += 1
      if p + bs < n and text[p + bs] == quote_char:
        if bs % period == close_mod:
          after_q = p + bs + 1
          if (
              not in_quote or saw_outer_escaped_q
          ) and _is_valid_json_member_end(
              text,
              after_q,
              is_escaped=True,
              nl_bs=nl_bs,
              lead_bs=lead_bs,
              brace_index_cache=active_brace_cache,
          ):
            return _finish(False)
          if not in_quote and _quote_opens_kv_at(
              text, p + bs - 1, escaped_dq=True
          ):
            saw_outer_escaped_q = False
          in_quote = not in_quote
        elif ((bs + 1) & -(bs + 1)) < low_zero:
          return _finish(True)
        elif not in_quote:
          saw_outer_escaped_q = True
        p += bs + 1
        continue
      if (bs == 1 or bs == nl_bs) and p + bs < n and text[p + bs] in ("n", "r"):
        in_quote = False
        saw_outer_escaped_q = False
        p += bs + 1
        continue
      p += max(1, bs)
      continue
    p += 1
  return _finish(True)


def _is_valid_json_member_end(
    text: str,
    pos: int,
    *,
    is_escaped: bool,
    nl_bs: int = 1,
    allow_prose_after_brace: bool = False,
    quote_char: str = '"',
    lead_bs: int = 0,
    prose_memo: dict[tuple[bool, int, str], dict[int, bool]] | None = None,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
) -> bool:
  """Returns True when ``pos`` terminates a JSON string member value."""
  n = len(text)
  q = pos
  saw_raw_nl = False
  while q < n:
    ch = text[q]
    if ch in (" ", "\t", "\r", "\n"):
      if ch in ("\r", "\n"):
        saw_raw_nl = True
      q += 1
      continue
    if is_escaped and ch == "\\":
      bs = 0
      while q + bs < n and text[q + bs] == "\\":
        bs += 1
      if (bs == 1 or bs == nl_bs) and q + bs < n and text[q + bs] in ("n", "r"):
        q += bs + 1
        continue
    break
  if q >= n:
    return True
  ch = text[q]
  if ch in ("}", "]"):
    if _is_outer_json_after_brace(
        text,
        q + 1,
        is_escaped=is_escaped,
        allow_sensitive_header=True,
        allow_open_brace=not allow_prose_after_brace,
        nl_bs=nl_bs,
    ):
      return True
    if allow_prose_after_brace:
      return _is_balanced_outer_prose_after_brace(
          text,
          q + 1,
          quote_char=quote_char,
          is_escaped=is_escaped,
          lead_bs=lead_bs,
          nl_bs=nl_bs,
          prose_memo=prose_memo,
          brace_index_cache=brace_index_cache,
      )
    return False
  if ch == ",":
    return _is_json_comma_member_sep(
        text, q, is_escaped_dq=is_escaped, nl_bs=nl_bs
    )
  if ch == "{" and saw_raw_nl:
    matched_obj = False
    obj_is_esc = is_escaped
    if is_escaped and _looks_like_json_object_open(
        text, q, is_escaped=True, nl_bs=nl_bs
    ):
      matched_obj = True
      obj_is_esc = True
    elif _looks_like_json_object_open(text, q, is_escaped=False, nl_bs=nl_bs):
      matched_obj = True
      obj_is_esc = False
    if matched_obj:
      obj_low_zero = (
          max(
              2,
              _escaped_quote_mod_and_period(lead_bs)[2] if lead_bs > 0 else 2,
          )
          if obj_is_esc
          else 0
      )
      m_close = _resolve_json_object_close_pos(
          text,
          q,
          is_escaped=obj_is_esc,
          low_zero=obj_low_zero,
          nl_bs=nl_bs,
          brace_index_cache=brace_index_cache,
      )
      if m_close is None:
        return True
      if not allow_prose_after_brace:
        return False
      return _is_balanced_outer_prose_after_brace(
          text,
          m_close + 1,
          quote_char=quote_char,
          is_escaped=is_escaped,
          lead_bs=lead_bs,
          nl_bs=nl_bs,
          prose_memo=prose_memo,
          brace_index_cache=brace_index_cache,
      )
  return False


def _is_plausible_json_container_start(
    text: str,
    pos: int,
    *,
    is_escaped: bool,
    nl_bs: int = 1,
) -> bool:
  """Returns True when ``{`` or ``[`` at ``pos`` starts a JSON container."""
  n = len(text)
  if pos >= n or text[pos] not in ("{", "["):
    return False
  ch = text[pos]
  k = pos + 1
  while k < n:
    c = text[k]
    if c in (" ", "\t", "\r", "\n"):
      k += 1
      continue
    if is_escaped and c == "\\":
      bs = 0
      while k + bs < n and text[k + bs] == "\\":
        bs += 1
      if (
          (bs == 1 or bs == nl_bs)
          and k + bs < n
          and text[k + bs] in ("t", "r", "n")
      ):
        k += bs + 1
        continue
      return bs % 2 == 1 and k + bs < n and text[k + bs] in ('"', "'")
    break
  if k >= n:
    return False
  nc = text[k]
  if ch == "{":
    return nc in ("}", "{", "[", '"', "'")
  return (
      nc in ("]", "{", "[", '"', "'", "-")
      or nc.isdigit()
      or text.startswith(("true", "false", "null"), k)
  )


def _is_json_multiline_str_boundary(
    text: str,
    ls: int,
    *,
    is_escaped: bool,
    nl_bs: int = 1,
) -> bool:
  """Returns True when line start ``ls`` starts a JSON structural boundary."""
  n = len(text)
  if ls >= n:
    return True
  lch = text[ls]
  if lch in ("}", "]"):
    return _is_outer_json_after_brace(
        text,
        ls + 1,
        is_escaped=is_escaped,
        nl_bs=nl_bs,
        allow_open_brace=False,
        allow_open_bracket=False,
    )
  if lch in ('"', "'") and _quote_opens_kv_at(text, ls, escaped_dq=False):
    return True
  if is_escaped and lch == "\\":
    bs = 0
    while ls + bs < n and text[ls + bs] == "\\":
      bs += 1
    if (
        bs % 2 == 1
        and ls + bs < n
        and text[ls + bs] == '"'
        and _quote_opens_kv_at(text, ls + bs - 1, escaped_dq=True)
    ):
      return True
  if lch == "{":
    if _looks_like_json_object_open(
        text, ls, is_escaped=False, nl_bs=nl_bs
    ) or (
        is_escaped
        and _looks_like_json_object_open(text, ls, is_escaped=True, nl_bs=nl_bs)
    ):
      return True
  return False


def _build_json_brace_index(
    text: str,
    *,
    is_escaped: bool,
    low_zero: int = 0,
) -> tuple[list[int], dict[int, int]]:
  """Builds per-index brace depth and matching close positions in O(N)."""
  n = len(text)
  depth_at = [0] * n
  close_brace: dict[int, int] = {}
  brace_stack: list[int] = []
  bracket_stack: list[int] = []
  unclosed_dq_until = 0
  unclosed_sq_until = 0
  unclosed_ml_dq_until = 0
  i = 0
  if not is_escaped:
    while i < n:
      depth = len(brace_stack) + len(bracket_stack)
      depth_at[i] = depth
      c = text[i]
      if c == "\\":
        if i + 1 < n and text[i + 1] not in ("\r", "\n"):
          depth_at[i + 1] = depth
          i += 2
        else:
          i += 1
        continue
      if c == "'":
        if i == 0 or not text[i - 1].isalnum():
          if i >= unclosed_sq_until:
            p = i + 1
            q_end = -1
            while p < n:
              ch = text[p]
              if ch in ("\r", "\n", '"'):
                break
              if ch == "\\":
                if p + 1 < n and text[p + 1] not in ("\r", "\n"):
                  p += 2
                else:
                  p += 1
                continue
              if ch == "'":
                q_end = p + 1
                break
              p += 1
            if q_end != -1:
              depth_at[i:q_end] = [depth] * (q_end - i)
              i = q_end
              continue
            unclosed_sq_until = p
        i += 1
        continue
      if c == '"':
        if depth >= 2 or (depth == 1 and _preceded_by_colon(text, i - 1)):
          if i >= unclosed_ml_dq_until:
            p = i + 1
            q_end = -1
            stop_pos = n
            while p < n:
              ch = text[p]
              if ch in ("\r", "\n"):
                ls = p + 1
                while ls < n and text[ls] in (" ", "\t", "\r", "\n"):
                  ls += 1
                if ls >= n or _is_json_multiline_str_boundary(
                    text, ls, is_escaped=False
                ):
                  stop_pos = ls
                  break
                p = ls
                continue
              if ch == "\\":
                if p + 1 < n and text[p + 1] not in ("\r", "\n"):
                  p += 2
                else:
                  p += 1
                continue
              if ch == '"':
                q_end = p + 1
                break
              p += 1
            if q_end != -1:
              depth_at[i:q_end] = [depth] * (q_end - i)
              i = q_end
              continue
            unclosed_ml_dq_until = stop_pos
          i += 1
          continue
        if i >= unclosed_dq_until:
          p = i + 1
          q_end = -1
          while p < n:
            ch = text[p]
            if ch in ("\r", "\n"):
              break
            if ch == "\\":
              if p + 1 < n and text[p + 1] not in ("\r", "\n"):
                p += 2
              else:
                p += 1
              continue
            if ch == '"':
              q_end = p + 1
              break
            p += 1
          if q_end != -1:
            depth_at[i:q_end] = [depth] * (q_end - i)
            i = q_end
            continue
          unclosed_dq_until = p
        i += 1
        continue
      if c == "{":
        brace_stack.append(i)
      elif c == "}" and brace_stack:
        close_brace[brace_stack.pop()] = i
      elif c == "[":
        bracket_stack.append(i)
      elif c == "]" and bracket_stack:
        close_brace[bracket_stack.pop()] = i
      i += 1
    return depth_at, close_brace
  close_mod = low_zero - 1
  period = low_zero * 2
  nl_bs_est = max(1, low_zero // 2)

  def _skip_escaped_ws(pos: int) -> int:
    ls = pos
    while ls < n:
      c_ws = text[ls]
      if c_ws in (" ", "\t", "\r", "\n"):
        ls += 1
        continue
      if c_ws == "\\":
        bs_ws = 0
        while ls + bs_ws < n and text[ls + bs_ws] == "\\":
          bs_ws += 1
        if (
            bs_ws in (1, nl_bs_est, low_zero)
            and ls + bs_ws < n
            and text[ls + bs_ws] in ("n", "r", "t")
        ):
          ls += bs_ws + 1
          continue
      break
    return ls

  while i < n:
    depth = len(brace_stack) + len(bracket_stack)
    depth_at[i] = depth
    c = text[i]
    if c == "\\":
      bs = 0
      while i + bs < n and text[i + bs] == "\\":
        bs += 1
      if i + bs < n and text[i + bs] == '"':
        if low_zero >= 2 and bs % period == close_mod:
          if depth >= 2 or (depth == 1 and _preceded_by_colon(text, i - 1)):
            if i >= unclosed_ml_dq_until:
              p = i + bs + 1
              q_end = -1
              stop_pos = n
              while p < n:
                ch = text[p]
                if ch == '"':
                  stop_pos = p
                  break
                if ch in ("\r", "\n"):
                  ls = _skip_escaped_ws(p + 1)
                  if ls >= n or _is_json_multiline_str_boundary(
                      text, ls, is_escaped=True, nl_bs=nl_bs_est
                  ):
                    stop_pos = ls
                    break
                  p = ls
                  continue
                if ch == "\\":
                  bs_c = 0
                  while p + bs_c < n and text[p + bs_c] == "\\":
                    bs_c += 1
                  if p + bs_c < n and text[p + bs_c] == '"':
                    if bs_c % period == close_mod:
                      q_end = p + bs_c + 1
                      break
                    if ((bs_c + 1) & -(bs_c + 1)) < low_zero:
                      stop_pos = p
                      break
                    p += bs_c + 1
                    continue
                  if (
                      bs_c in (1, nl_bs_est, low_zero)
                      and p + bs_c < n
                      and text[p + bs_c] in ("n", "r")
                  ):
                    ls = _skip_escaped_ws(p + bs_c + 1)
                    if ls >= n or _is_json_multiline_str_boundary(
                        text, ls, is_escaped=True, nl_bs=nl_bs_est
                    ):
                      stop_pos = ls
                      break
                    p = ls
                    continue
                  p += max(1, bs_c)
                  continue
                p += 1
              if q_end != -1:
                depth_at[i:q_end] = [depth] * (q_end - i)
                i = q_end
                continue
              unclosed_ml_dq_until = stop_pos
          elif i >= unclosed_dq_until:
            p = i + bs + 1
            q_end = -1
            while p < n:
              ch = text[p]
              if ch in ("\r", "\n", '"'):
                break
              if ch == "\\":
                bs_c = 0
                while p + bs_c < n and text[p + bs_c] == "\\":
                  bs_c += 1
                if p + bs_c < n and text[p + bs_c] == '"':
                  if bs_c % period == close_mod:
                    q_end = p + bs_c + 1
                    break
                  if ((bs_c + 1) & -(bs_c + 1)) < low_zero:
                    break
                  p += bs_c + 1
                  continue
                p += max(1, bs_c)
                continue
              p += 1
            if q_end != -1:
              depth_at[i:q_end] = [depth] * (q_end - i)
              i = q_end
              continue
            unclosed_dq_until = p
        end = i + bs + 1
        depth_at[i:end] = [depth] * (end - i)
        i = end
        continue
      if i + bs < n and text[i + bs] == "'":
        if low_zero >= 2 and (
            bs % period == close_mod or (low_zero == 2 and bs == 1)
        ):
          if i >= unclosed_sq_until:
            p = i + bs + 1
            q_end = -1
            while p < n:
              ch = text[p]
              if ch in ("\r", "\n", '"'):
                break
              if ch == "\\":
                bs_c = 0
                while p + bs_c < n and text[p + bs_c] == "\\":
                  bs_c += 1
                if p + bs_c < n and text[p + bs_c] == "'":
                  if bs_c % period == close_mod or (
                      low_zero == 2 and bs_c == 1
                  ):
                    q_end = p + bs_c + 1
                    break
                  p += bs_c + 1
                  continue
                if p + bs_c < n and text[p + bs_c] == '"':
                  if ((bs_c + 1) & -(bs_c + 1)) <= low_zero:
                    break
                  p += bs_c + 1
                  continue
                p += max(1, bs_c)
                continue
              p += 1
            if q_end != -1:
              depth_at[i:q_end] = [depth] * (q_end - i)
              i = q_end
              continue
            unclosed_sq_until = p
        end = i + bs + 1
        depth_at[i:end] = [depth] * (end - i)
        i = end
        continue
      end = i + max(1, bs)
      depth_at[i:end] = [depth] * (end - i)
      i = end
      continue
    if c == "'":
      if i == 0 or not text[i - 1].isalnum():
        if i >= unclosed_sq_until:
          p = i + 1
          q_end = -1
          while p < n:
            ch = text[p]
            if ch in ("\r", "\n", '"'):
              break
            if ch == "\\":
              bs_c = 0
              while p + bs_c < n and text[p + bs_c] == "\\":
                bs_c += 1
              if p + bs_c < n and text[p + bs_c] == '"':
                break
              if p + bs_c < n and text[p + bs_c] == "'":
                if bs_c % 2 == 1:
                  break
                q_end = p + bs_c + 1
                break
              p += max(1, bs_c)
              continue
            if ch == "'":
              q_end = p + 1
              break
            p += 1
          if q_end != -1:
            depth_at[i:q_end] = [depth] * (q_end - i)
            i = q_end
            continue
          unclosed_sq_until = p
      i += 1
      continue
    if c == "{":
      brace_stack.append(i)
    elif c == "}" and brace_stack:
      close_brace[brace_stack.pop()] = i
    elif c == "[":
      bracket_stack.append(i)
    elif c == "]" and bracket_stack:
      close_brace[bracket_stack.pop()] = i
    i += 1
  return depth_at, close_brace


def _find_balanced_json_object_close_at(
    text: str,
    obj_start: int,
    *,
    is_escaped: bool,
    low_zero: int,
    nl_bs: int,
) -> int | None:
  """Finds the matching close brace for a JSON object at ``obj_start``."""
  cache = _JSON_OBJ_CLOSE_CACHE.get()
  sub: dict[int, int] | None = None
  if cache is not None:
    cache_key = (is_escaped, low_zero)
    sub = cache.get(cache_key)
    if sub is None:
      sub = {}
      cache[cache_key] = sub
    cached = sub.get(obj_start)
    if cached is not None:
      return None if cached < 0 else cached
  n = len(text)
  brace_stack = [obj_start]
  unclosed_dq_until = -1
  unclosed_ml_dq_until = -1
  unclosed_sq_until = -1
  unclosed_esq_until = -1
  after_raw_nl = False
  close_mod = low_zero - 1 if low_zero >= 2 else -1
  period = low_zero * 2 if low_zero >= 2 else 0

  def _stops_string_scan_at(pos: int) -> bool:
    ch = text[pos]
    if ch == "{":
      return _looks_like_json_object_open(
          text, pos, is_escaped=False, nl_bs=nl_bs
      ) or (
          is_escaped
          and _looks_like_json_object_open(
              text, pos, is_escaped=True, nl_bs=nl_bs
          )
      )
    if ch == ",":
      arr_pos = pos + 1
      while arr_pos < n and text[arr_pos] in (" ", "\t", "\r", "\n"):
        arr_pos += 1
      return (
          arr_pos < n
          and text[arr_pos] == "["
          and _is_plausible_json_container_start(
              text, arr_pos, is_escaped=is_escaped, nl_bs=nl_bs
          )
      )
    return False

  i = obj_start + 1
  while i < n:
    c = text[i]
    if c in ("\r", "\n"):
      after_raw_nl = True
      i += 1
      continue
    if c in (" ", "\t"):
      i += 1
      continue
    was_after_raw_nl = after_raw_nl
    after_raw_nl = False
    if c == "{":
      if was_after_raw_nl and (
          _looks_like_json_object_open(text, i, is_escaped=False, nl_bs=nl_bs)
          or (
              is_escaped
              and _looks_like_json_object_open(
                  text, i, is_escaped=True, nl_bs=nl_bs
              )
          )
      ):
        if sub is not None:
          for open_i in brace_stack:
            sub[open_i] = -1
        return None
      brace_stack.append(i)
      i += 1
      continue
    if c == "}":
      open_i = brace_stack.pop()
      if sub is not None:
        sub[open_i] = i
      if not brace_stack:
        return i
      i += 1
      continue
    if c == "'" and (i == 0 or not text[i - 1].isalnum()):
      if i < unclosed_sq_until:
        i += 1
        continue
      j = i + 1
      closed = False
      while j < n:
        cj = text[j]
        if cj in ("\r", "\n") or (is_escaped and cj == '"'):
          break
        if cj in ("{", ",") and _stops_string_scan_at(j):
          break
        if cj == "\\":
          if is_escaped and low_zero >= 2:
            bs_c = 0
            while j + bs_c < n and text[j + bs_c] == "\\":
              bs_c += 1
            if j + bs_c < n and text[j + bs_c] == '"':
              if (
                  bs_c % period == close_mod
                  or ((bs_c + 1) & -(bs_c + 1)) < low_zero
              ):
                break
              j += bs_c + 1
              continue
            if j + bs_c < n and text[j + bs_c] == "'":
              j += bs_c + 1 if bs_c % 2 == 1 else bs_c
              continue
            j += max(1, bs_c)
            continue
          j += 2 if j + 1 < n and text[j + 1] not in ("\r", "\n") else 1
          continue
        if cj == "'":
          i = j + 1
          closed = True
          break
        j += 1
      if closed:
        continue
      unclosed_sq_until = j
      i += 1
      continue
    if not is_escaped:
      if c == "\\":
        i += 2 if i + 1 < n and text[i + 1] not in ("\r", "\n") else 1
        continue
      if c == '"':
        if i < unclosed_dq_until:
          i += 1
          continue
        j = i + 1
        closed = False
        hit_nl = False
        while j < n:
          cj = text[j]
          if cj in ("\r", "\n"):
            hit_nl = True
            break
          if cj in ("{", ",") and _stops_string_scan_at(j):
            break
          if cj == "\\":
            j += 2 if j + 1 < n and text[j + 1] not in ("\r", "\n") else 1
            continue
          if cj == '"':
            i = j + 1
            closed = True
            break
          j += 1
        if closed:
          continue
        unclosed_dq_until = j
        if hit_nl and i >= unclosed_ml_dq_until:
          ml_after_nl = True
          j += 1
          while j < n:
            cj = text[j]
            if cj in ("\r", "\n"):
              ml_after_nl = True
              j += 1
              continue
            if ml_after_nl:
              if cj in (" ", "\t"):
                j += 1
                continue
              ml_after_nl = False
              if _is_json_multiline_str_boundary(
                  text, j, is_escaped=is_escaped, nl_bs=nl_bs
              ):
                break
            if cj in ("{", ",") and _stops_string_scan_at(j):
              break
            if cj == "\\":
              j += 2 if j + 1 < n and text[j + 1] not in ("\r", "\n") else 1
              continue
            if cj == '"':
              i = j + 1
              closed = True
              break
            j += 1
          if closed:
            continue
          unclosed_ml_dq_until = j
        i += 1
        continue
    elif c == "\\" and low_zero >= 2:
      bs = 0
      while i + bs < n and text[i + bs] == "\\":
        bs += 1
      if i + bs < n and text[i + bs] == '"' and bs % period == close_mod:
        if i < unclosed_dq_until:
          i += bs + 1
          continue
        j = i + bs + 1
        closed = False
        hit_nl = False
        while j < n:
          cj = text[j]
          if cj in ("\r", "\n"):
            hit_nl = True
            break
          if cj == '"':
            break
          if cj in ("{", ",") and _stops_string_scan_at(j):
            break
          if cj == "\\":
            bs_c = 0
            while j + bs_c < n and text[j + bs_c] == "\\":
              bs_c += 1
            if j + bs_c < n and text[j + bs_c] == '"':
              if bs_c % period == close_mod:
                i = j + bs_c + 1
                closed = True
                break
              if ((bs_c + 1) & -(bs_c + 1)) < low_zero:
                break
              j += bs_c + 1
              continue
            j += max(1, bs_c)
            continue
          j += 1
        if closed:
          continue
        unclosed_dq_until = j
        if hit_nl and i >= unclosed_ml_dq_until:
          ml_after_nl = True
          j += 1
          while j < n:
            cj = text[j]
            if cj in ("\r", "\n"):
              ml_after_nl = True
              j += 1
              continue
            if ml_after_nl:
              if cj in (" ", "\t"):
                j += 1
                continue
              ml_after_nl = False
              if _is_json_multiline_str_boundary(
                  text, j, is_escaped=is_escaped, nl_bs=nl_bs
              ):
                break
            if cj == '"':
              break
            if cj in ("{", ",") and _stops_string_scan_at(j):
              break
            if cj == "\\":
              bs_c = 0
              while j + bs_c < n and text[j + bs_c] == "\\":
                bs_c += 1
              if j + bs_c < n and text[j + bs_c] == '"':
                if bs_c % period == close_mod:
                  i = j + bs_c + 1
                  closed = True
                  break
                if ((bs_c + 1) & -(bs_c + 1)) < low_zero:
                  break
                j += bs_c + 1
                continue
              j += max(1, bs_c)
              continue
            j += 1
          if closed:
            continue
          unclosed_ml_dq_until = j
        i += bs + 1
        continue
      if i + bs < n and text[i + bs] == "'" and bs % period == close_mod:
        if i < unclosed_esq_until:
          i += bs + 1
          continue
        j = i + bs + 1
        closed = False
        while j < n:
          cj = text[j]
          if cj in ("\r", "\n", '"', "'"):
            break
          if cj in ("{", ",") and _stops_string_scan_at(j):
            break
          if cj == "\\":
            bs_c = 0
            while j + bs_c < n and text[j + bs_c] == "\\":
              bs_c += 1
            if j + bs_c < n and text[j + bs_c] == "'":
              if bs_c % period == close_mod:
                i = j + bs_c + 1
                closed = True
                break
              if ((bs_c + 1) & -(bs_c + 1)) < low_zero:
                break
              j += bs_c + 1
              continue
            if j + bs_c < n and text[j + bs_c] == '"':
              if (
                  bs_c % period == close_mod
                  or ((bs_c + 1) & -(bs_c + 1)) < low_zero
              ):
                break
              j += bs_c + 1
              continue
            j += max(1, bs_c)
            continue
          j += 1
        if closed:
          continue
        unclosed_esq_until = j
        i += bs + 1
        continue
      if i + bs < n and text[i + bs] == '"':
        i += bs + 1
      elif i + bs < n and text[i + bs] == "'" and bs % 2 == 1:
        i += bs + 1
      else:
        i += max(1, bs)
      continue
    i += 1
  if sub is not None:
    for open_i in brace_stack:
      sub[open_i] = -1
  return None


def _find_json_member_string_end(
    text: str,
    val_start: int,
    val_end: int,
    replacement: str,
    *,
    json_close_fail_from: dict[tuple[bool, int, str, int], int] | None = None,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
    prose_memo: dict[tuple[bool, int, str], dict[int, bool]] | None = None,
    allow_comma_obj_fallback: bool = False,
    out_state: dict[str, int | bool] | None = None,
) -> int | None:
  """Extends a prematurely closed JSON string value to its true member end."""
  n = len(text)
  if val_start >= n or val_end >= n:
    return None
  first_ch = text[val_start]
  if first_ch in ('"', "'"):
    if replacement != f"{first_ch}[REDACTED]{first_ch}":
      return None
    is_escaped = False
    lead_bs = 0
    quote_char = first_ch
    close_mod = period = low_zero = 0
    nl_bs = 1
  elif first_ch == "\\":
    lead_bs = 0
    while val_start + lead_bs < n and text[val_start + lead_bs] == "\\":
      lead_bs += 1
    if (
        lead_bs % 2 != 1
        or val_start + lead_bs >= n
        or text[val_start + lead_bs] not in ('"', "'")
    ):
      return None
    quote_char = text[val_start + lead_bs]
    delim = "\\" * lead_bs + quote_char
    if replacement != f"{delim}[REDACTED]{delim}":
      return None
    is_escaped = True
    close_mod, period, low_zero = _escaped_quote_mod_and_period(lead_bs)
    nl_bs = max(1, low_zero // 2)
  else:
    return None
  brace_key = (is_escaped, low_zero)
  if brace_index_cache is None:
    brace_index_cache = {}
  brace_index = brace_index_cache.get(brace_key)
  if brace_index is None:
    brace_index = _build_json_brace_index(
        text, is_escaped=is_escaped, low_zero=low_zero
    )
    brace_index_cache[brace_key] = brace_index
  depth_at, close_brace = brace_index
  if _is_valid_json_member_end(
      text,
      val_end,
      is_escaped=is_escaped,
      nl_bs=nl_bs,
      allow_prose_after_brace=True,
      quote_char=quote_char,
      lead_bs=lead_bs,
      prose_memo=prose_memo,
      brace_index_cache=brace_index_cache,
  ):
    return None
  if out_state is not None:
    out_state["extension_attempted"] = True
  cur_depth = depth_at[val_end]
  dkey = (is_escaped, low_zero, quote_char, cur_depth)
  if json_close_fail_from is not None and val_end < json_close_fail_from.get(
      dkey, 0
  ):
    return None
  segments: list[tuple[tuple[bool, int, str, int], int]] = []

  def _record_fail(stop: int) -> None:
    if json_close_fail_from is None:
      return
    for seg_key, seg_stop in segments:
      if seg_stop <= stop:
        if seg_stop > json_close_fail_from.get(seg_key, 0):
          json_close_fail_from[seg_key] = seg_stop
      else:
        if stop > json_close_fail_from.get(seg_key, 0):
          json_close_fail_from[seg_key] = stop
        return
    if stop > json_close_fail_from.get(dkey, 0):
      json_close_fail_from[dkey] = stop

  def _sync_depth(pos: int) -> bool:
    nonlocal cur_depth, dkey
    if pos >= n:
      return True
    next_depth = depth_at[pos]
    if next_depth == cur_depth:
      return True
    segments.append((dkey, pos))
    cur_depth = next_depth
    dkey = (is_escaped, low_zero, quote_char, cur_depth)
    if json_close_fail_from is not None and pos < json_close_fail_from.get(
        dkey, 0
    ):
      _record_fail(pos)
      return False
    return True

  last_comma_obj_cand_end: int | None = None

  def _aborts_at_outer_brace(pos: int, *, brace_is_escaped: bool) -> bool:
    if _is_outer_json_after_brace(
        text,
        pos + 1,
        is_escaped=brace_is_escaped,
        allow_sensitive_header=True,
        allow_open_brace=False,
        nl_bs=nl_bs if brace_is_escaped else 1,
    ):
      return True
    if _is_outer_json_after_brace(
        text,
        pos + 1,
        is_escaped=brace_is_escaped,
        allow_sensitive_header=False,
        allow_open_brace=True,
        nl_bs=nl_bs if brace_is_escaped else 1,
    ):
      return _is_balanced_outer_prose_after_brace(
          text,
          pos + 1,
          quote_char=quote_char,
          is_escaped=is_escaped,
          lead_bs=lead_bs,
          nl_bs=nl_bs,
          prose_memo=prose_memo,
          brace_index_cache=brace_index_cache,
      )
    return False

  def _is_comma_before_json_object(pos: int) -> bool:
    p = pos
    while p < n and text[p] in (" ", "\t", "\r", "\n"):
      p += 1
    if p >= n or text[p] != ",":
      return False
    p += 1
    while p < n:
      ch = text[p]
      if ch in (" ", "\t", "\r", "\n"):
        p += 1
        continue
      if is_escaped and ch == "\\":
        bs = 0
        while p + bs < n and text[p + bs] == "\\":
          bs += 1
        if bs % 2 == 1 and p + bs < n and text[p + bs] in ("n", "r"):
          p += bs + 1
          continue
      break
    if p >= n:
      return True
    if text[p] != "{":
      return False
    return _looks_like_json_object_open(
        text, p, is_escaped=is_escaped, nl_bs=nl_bs
    ) or (
        is_escaped
        and _looks_like_json_object_open(text, p, is_escaped=False, nl_bs=nl_bs)
    )

  err_checked_until: int | None = None
  err_marker_found = False

  def _has_critical_error_up_to(end_pos: int) -> bool:
    nonlocal err_checked_until, err_marker_found
    if err_marker_found:
      return True
    scan_from = val_start if err_checked_until is None else err_checked_until
    if end_pos <= scan_from:
      return False
    if (
        _search_valid_critical_error_marker(text, scan_from, end_pos)
        is not None
    ):
      err_marker_found = True
    err_checked_until = end_pos
    return err_marker_found

  if val_end is not None:
    comma_pos = val_end
    while comma_pos < len(text) and text[comma_pos] in (
        " ",
        "\t",
        "\r",
        "\n",
    ):
      comma_pos += 1
    if (
        comma_pos < len(text)
        and text[comma_pos] == ","
        and _is_comma_before_json_object(comma_pos)
    ):
      last_comma_obj_cand_end = val_end
      if (
          not allow_comma_obj_fallback
          and out_state is not None
          and not _has_critical_error_up_to(val_end)
      ):
        out_state["comma_fallback"] = val_end

  def _comma_obj_fallback() -> int | None:
    if (
        allow_comma_obj_fallback
        and last_comma_obj_cand_end is not None
        and _search_valid_critical_error_marker(
            text, val_end, last_comma_obj_cand_end
        )
        is None
    ):
      return last_comma_obj_cand_end
    return None

  if not is_escaped:
    cursor = val_end
    after_raw_nl = False
    while cursor < n:
      c = text[cursor]
      if c in ("\r", "\n"):
        after_raw_nl = True
        cursor += 1
        continue
      if c in (" ", "\t"):
        cursor += 1
        continue
      was_after_raw_nl = after_raw_nl
      after_raw_nl = False
      if c == "\\":
        cursor += (
            2 if cursor + 1 < n and text[cursor + 1] not in ("\r", "\n") else 1
        )
        continue
      if c in ("{", "["):
        if (
            c == "{"
            and was_after_raw_nl
            and _looks_like_json_object_open(
                text, cursor, is_escaped=False, nl_bs=nl_bs
            )
        ):
          m_close = close_brace.get(cursor)
          if m_close is None:
            m_close = _find_balanced_json_object_close_at(
                text,
                cursor,
                is_escaped=False,
                low_zero=low_zero,
                nl_bs=nl_bs,
            )
          if m_close is not None and not _is_balanced_outer_prose_after_brace(
              text,
              m_close + 1,
              quote_char=quote_char,
              is_escaped=is_escaped,
              lead_bs=lead_bs,
              nl_bs=nl_bs,
              prose_memo=prose_memo,
              brace_index_cache=brace_index_cache,
          ):
            cursor = m_close + 1
            if not _sync_depth(cursor):
              return None
            continue
          fallback_end = _comma_obj_fallback()
          if fallback_end is not None:
            return fallback_end
          _record_fail(cursor)
          return None
        if _is_plausible_json_container_start(
            text, cursor, is_escaped=False, nl_bs=nl_bs
        ):
          match_close = close_brace.get(cursor)
          if match_close is not None:
            cursor = match_close + 1
            if not _sync_depth(cursor):
              return None
            continue
          if c == "{" and _looks_like_json_object_open(
              text, cursor, is_escaped=False, nl_bs=nl_bs
          ):
            fallback_close = _find_balanced_json_object_close_at(
                text,
                cursor,
                is_escaped=is_escaped,
                low_zero=low_zero,
                nl_bs=nl_bs,
            )
            if fallback_close is not None:
              cursor = fallback_close + 1
              if not _sync_depth(cursor):
                return None
              continue
            fallback_end = _comma_obj_fallback()
            if fallback_end is not None:
              return fallback_end
            _record_fail(cursor)
            return None
        cursor += 1
        if not _sync_depth(cursor):
          return None
        continue
      if c in ("}", "]"):
        if _aborts_at_outer_brace(cursor, brace_is_escaped=False):
          _record_fail(cursor)
          return None
        cursor += 1
        if not _sync_depth(cursor):
          return None
        continue
      if c == "," and _is_json_comma_member_sep(
          text, cursor, is_escaped_dq=is_escaped
      ):
        _record_fail(cursor)
        return None
      if c == quote_char:
        cand_end = cursor + 1
        if _is_valid_json_member_end(
            text,
            cand_end,
            is_escaped=False,
            brace_index_cache=brace_index_cache,
            allow_prose_after_brace=True,
            quote_char=quote_char,
            lead_bs=lead_bs,
            prose_memo=prose_memo,
        ):
          err_m = _search_valid_critical_error_marker(text, val_end, cand_end)
          if err_m is not None:
            _record_fail(err_m.start())
            return None
          return cand_end
        if _is_comma_before_json_object(cand_end):
          last_comma_obj_cand_end = cand_end
          if (
              not allow_comma_obj_fallback
              and out_state is not None
              and not _has_critical_error_up_to(cand_end)
          ):
            out_state["comma_fallback"] = cand_end
      cursor += 1
    fallback_end = _comma_obj_fallback()
    if fallback_end is not None:
      return fallback_end
    _record_fail(n)
    return None
  cursor = val_end
  after_raw_nl = False
  while cursor < n:
    c = text[cursor]
    if c in ("\r", "\n"):
      after_raw_nl = True
      cursor += 1
      continue
    if c in (" ", "\t"):
      cursor += 1
      continue
    was_after_raw_nl = after_raw_nl
    after_raw_nl = False
    if c == '"' and quote_char == '"':
      _record_fail(cursor)
      return None
    if c in ("{", "["):
      if (
          c == "{"
          and was_after_raw_nl
          and (
              _looks_like_json_object_open(
                  text, cursor, is_escaped=True, nl_bs=nl_bs
              )
              or _looks_like_json_object_open(text, cursor, is_escaped=False)
          )
      ):
        m_close = close_brace.get(cursor)
        if m_close is None:
          m_close = _find_balanced_json_object_close_at(
              text,
              cursor,
              is_escaped=(
                  is_escaped
                  and _looks_like_json_object_open(
                      text, cursor, is_escaped=True, nl_bs=nl_bs
                  )
              ),
              low_zero=low_zero,
              nl_bs=nl_bs,
          )
        if m_close is not None and not _is_balanced_outer_prose_after_brace(
            text,
            m_close + 1,
            quote_char=quote_char,
            is_escaped=is_escaped,
            lead_bs=lead_bs,
            nl_bs=nl_bs,
            prose_memo=prose_memo,
            brace_index_cache=brace_index_cache,
        ):
          cursor = m_close + 1
          if not _sync_depth(cursor):
            return None
          continue
        fallback_end = _comma_obj_fallback()
        if fallback_end is not None:
          return fallback_end
        _record_fail(cursor)
        return None
      if _is_plausible_json_container_start(
          text, cursor, is_escaped=True, nl_bs=nl_bs
      ):
        match_close = close_brace.get(cursor)
        if match_close is not None:
          cursor = match_close + 1
          if not _sync_depth(cursor):
            return None
          continue
        if c == "{" and _looks_like_json_object_open(
            text, cursor, is_escaped=True, nl_bs=nl_bs
        ):
          fallback_close = _find_balanced_json_object_close_at(
              text,
              cursor,
              is_escaped=is_escaped,
              low_zero=low_zero,
              nl_bs=nl_bs,
          )
          if fallback_close is not None:
            cursor = fallback_close + 1
            if not _sync_depth(cursor):
              return None
            continue
          fallback_end = _comma_obj_fallback()
          if fallback_end is not None:
            return fallback_end
          _record_fail(cursor)
          return None
      cursor += 1
      if not _sync_depth(cursor):
        return None
      continue
    if c in ("}", "]"):
      if _aborts_at_outer_brace(cursor, brace_is_escaped=True):
        _record_fail(cursor)
        return None
      cursor += 1
      if not _sync_depth(cursor):
        return None
      continue
    if c == "," and _is_json_comma_member_sep(
        text, cursor, is_escaped_dq=is_escaped, nl_bs=nl_bs
    ):
      _record_fail(cursor)
      return None
    if c == "\\":
      bs = 0
      while cursor + bs < n and text[cursor + bs] == "\\":
        bs += 1
      if cursor + bs < n and text[cursor + bs] == quote_char:
        if bs % period == close_mod:
          cand_end = cursor + bs + 1
          if _is_valid_json_member_end(
              text,
              cand_end,
              is_escaped=True,
              nl_bs=nl_bs,
              brace_index_cache=brace_index_cache,
              allow_prose_after_brace=True,
              quote_char=quote_char,
              lead_bs=lead_bs,
              prose_memo=prose_memo,
          ):
            err_m = _search_valid_critical_error_marker(text, val_end, cand_end)
            if err_m is not None:
              _record_fail(err_m.start())
              return None
            return cand_end
          if _is_comma_before_json_object(cand_end):
            last_comma_obj_cand_end = cand_end
            if (
                not allow_comma_obj_fallback
                and out_state is not None
                and not _has_critical_error_up_to(cand_end)
            ):
              out_state["comma_fallback"] = cand_end
        elif ((bs + 1) & -(bs + 1)) < low_zero:
          _record_fail(cursor + bs)
          return None
        cursor += bs + 1
        continue
      cursor += max(1, bs)
      continue
    cursor += 1
  fallback_end = _comma_obj_fallback()
  if fallback_end is not None:
    return fallback_end
  _record_fail(n)
  return None


def _try_redact_sensitive_kv(
    text: str,
    matched_key: str,
    conn_text: str,
    val_start: int,
    nl_count: int,
    *,
    is_quoted_key: bool = False,
    in_json_object: bool = False,
    enclosing_fail_from: dict[tuple[bool, int, str], int] | None = None,
    json_close_fail_from: dict[tuple[bool, int, str, int], int] | None = None,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
    prose_memo: dict[tuple[bool, int, str], dict[int, bool]] | None = None,
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
  is_same_line_quoted_child = (
      nl_count == 0
      and not is_quoted_key
      and conn_text.strip() == ":"
      and (
          next_char in ('"', "'")
          or _starts_with_odd_escaped_quote(text, val_start)
      )
  )
  enclosed_child_match: list[bool] = []
  if (
      (nl_count > 0 or is_same_line_quoted_child)
      and not is_container
      and _is_sensitive_header_at(
          text,
          val_start,
          allow_mismatched_quote=True,
          is_quoted_parent=is_quoted_key,
          check_enclosing_quote=(
              is_quoted_key
              or next_char in ('"', "'")
              or _starts_with_odd_escaped_quote(text, val_start)
          ),
          enclosing_fail_from=enclosing_fail_from,
          out_enclosed_match=enclosed_child_match,
      )
  ):
    return None
  is_cookie_key = (
      "cookie" in matched_key.lower()
      and _is_cookie_header_key_norm(_normalize_key_name(matched_key))
  )
  if (
      not is_quoted_key
      and nl_count == 0
      and val_start < n
      and next_char not in ("{", "[", "(", '"', "'", "\\")
      and not is_cookie_key
      and _PY_STR_PREFIX_RE.match(text, val_start) is None
      and _CTOR_PREFIX_RE.match(text, val_start) is None
  ):
    val_end = _scan_bare_value_end(text, val_start)
    if val_end <= val_start:
      return None
    raw_val = text[val_start:val_end].strip()
    if (
        len(raw_val) <= 3
        and raw_val.isascii()
        and raw_val.isdigit()
        and _is_numeric_count_scalar(matched_key, raw_val)
    ) or (
        len(raw_val) <= 24
        and raw_val.lower() in _SAFE_AUTH_STATUS_VALUES
        and _is_safe_auth_status_scalar(matched_key, raw_val)
    ):
      return val_end, None
    return val_end, "[REDACTED]"
  is_cookie_header = (
      not is_quoted_key
      and is_cookie_key
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
  if (
      is_quoted_key
      and _is_json_colon_connector(conn_text)
      and replacement is not None
      and (nl_count > 0 or is_same_line_quoted_child)
      and not is_container
      and bool(enclosed_child_match)
      and _search_valid_critical_error_marker(text, val_start, val_end)
      is not None
  ):
    return None
  if (
      is_quoted_key
      and _is_json_colon_connector(conn_text)
      and val_end < n
      and replacement is not None
      and (
          in_json_object
          or (
              val_end > val_start
              and len(replacement) >= 2
              and replacement != "[REDACTED]"
              and not text[val_end].isspace()
              and text[val_end] not in ",;)}]"
              and (
                  "'" in text[val_start:val_end]
                  or '"' in text[val_start:val_end]
              )
          )
      )
  ):
    err_scan = val_end
    while err_scan < n and text[err_scan] == " ":
      err_scan += 1
    starts_with_err_marker = (
        _CRITICAL_ERROR_MARKERS_RE.match(text, err_scan) is not None
    )
    is_child_delegatable = not starts_with_err_marker and bool(
        enclosed_child_match
    )
    json_probe_state: dict[str, int | bool] = {}
    json_end = _find_json_member_string_end(
        text,
        val_start,
        val_end,
        replacement,
        json_close_fail_from=json_close_fail_from,
        brace_index_cache=brace_index_cache,
        prose_memo=prose_memo,
        allow_comma_obj_fallback=not is_child_delegatable,
        out_state=json_probe_state,
    )
    if json_end is not None:
      val_end = json_end
    elif is_child_delegatable and json_probe_state.get("extension_attempted"):
      cf = json_probe_state.get("comma_fallback")
      if isinstance(cf, int) and not isinstance(cf, bool):
        lead_bs = 0
        while val_start + lead_bs < n and text[val_start + lead_bs] == "\\":
          lead_bs += 1
        mm = _try_mismatched_kv_redaction(
            text,
            val_start,
            val_start + lead_bs + 1,
            decode_escaped=(lead_bs > 0),
            enclosing_fail_from=enclosing_fail_from,
            json_close_fail_from=json_close_fail_from,
            brace_index_cache=brace_index_cache,
            prose_memo=prose_memo,
        )
        if mm is not None and mm[1] <= cf and not text[mm[1] : cf].strip():
          return None
        val_end = cf
      else:
        return None
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
    enclosing_fail_from: dict[tuple[bool, int, str], int] | None = None,
    json_close_fail_from: dict[tuple[bool, int, str, int], int] | None = None,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
    prose_memo: dict[tuple[bool, int, str], dict[int, bool]] | None = None,
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
      enclosing_fail_from=enclosing_fail_from,
      json_close_fail_from=json_close_fail_from,
      brace_index_cache=brace_index_cache,
      prose_memo=prose_memo,
  )
  if redacted is None:
    return None
  val_end, replacement = redacted
  return val_start, val_end, replacement


def _scrub_multiline_nonsensitive_json_value(
    text: str,
    conn_end: int,
    quote_char: str,
    *,
    is_escaped: bool,
    lead_bs: int = 0,
    depth: int = 0,
    brace_index_cache: (
        dict[tuple[bool, int], tuple[list[int], dict[int, int]]] | None
    ) = None,
) -> tuple[int, int, str, bool] | None:
  """Scrubs a multiline nonsensitive JSON string value if well-formed."""
  if depth >= 3:
    return None
  n = len(text)
  ml_val_start = conn_end
  while ml_val_start < n and text[ml_val_start] in (" ", "\t", "\r", "\n"):
    ml_val_start += 1
  if ml_val_start >= n:
    return None

  if is_escaped:
    low_zero = (lead_bs + 1) & -(lead_bs + 1)
    expected_bs = low_zero - 1
    if expected_bs <= 0:
      return None
    val_bs = 0
    while ml_val_start + val_bs < n and text[ml_val_start + val_bs] == "\\":
      val_bs += 1
    if (
        val_bs != expected_bs
        or ml_val_start + val_bs >= n
        or text[ml_val_start + val_bs] != quote_char
    ):
      return None
    if (
        _scan_same_line_escaped_dq_end(
            text, ml_val_start + val_bs - 1, lead_bs=val_bs
        )[0]
        is not None
    ):
      return None
    ml_inner_start = ml_val_start + val_bs + 1
    period = low_zero * 2
    close_mod = val_bs % period
    ml_end: int | None = None
    ml_inner_end = -1
    q = ml_inner_start
    while q < n:
      q = text.find(quote_char, q)
      if q == -1:
        break
      bs = 0
      p = q - 1
      while p >= ml_inner_start and text[p] == "\\":
        bs += 1
        p -= 1
      if bs % period == close_mod:
        ml_end = q + 1
        ml_inner_end = q - val_bs
        break
      if ((bs + 1) & -(bs + 1)) < low_zero:
        break
      q += 1
    ml_nl_bs = max(1, low_zero // 2)
    if ml_end is None or not _is_valid_json_member_end(
        text,
        ml_end,
        is_escaped=True,
        lead_bs=val_bs,
        nl_bs=ml_nl_bs,
        brace_index_cache=brace_index_cache,
    ):
      return None
  else:
    ml_nl_bs = 1
    if (
        text[ml_val_start] != quote_char
        or _scan_same_line_quote_end(text, ml_val_start) is not None
    ):
      return None
    ml_end = _scan_quoted_value(text, ml_val_start)
    if ml_end is None or not _is_valid_json_member_end(
        text,
        ml_end,
        is_escaped=False,
        brace_index_cache=brace_index_cache,
    ):
      return None
    ml_inner_start = ml_val_start + 1
    ml_inner_end = ml_end - 1

  ml_pos = ml_inner_start
  while ml_pos < ml_inner_end:
    nl_p = text.find("\n", ml_pos, ml_inner_end)
    cr_p = text.find("\r", ml_pos, ml_inner_end)
    if nl_p == -1 and cr_p == -1:
      break
    if nl_p != -1 and (cr_p == -1 or nl_p < cr_p):
      ls = nl_p + 1
    elif cr_p + 1 < ml_inner_end and text[cr_p + 1] == "\n":
      ls = cr_p + 2
    else:
      ls = cr_p + 1
    while ls < ml_inner_end and text[ls] in (" ", "\t"):
      ls += 1
    if ls < ml_inner_end and _is_json_multiline_str_boundary(
        text, ls, is_escaped=is_escaped, nl_bs=ml_nl_bs
    ):
      return None
    ml_pos = ls

  inner_val = text[ml_inner_start:ml_inner_end]
  scrubbed_val = _scrub_quoted_inner(
      inner_val,
      quote_kind='\\"' if is_escaped else quote_char,
      depth=depth,
      lead_bs=val_bs if is_escaped else 1,
  )
  replacement = (
      text[ml_val_start:ml_inner_start]
      + scrubbed_val
      + text[ml_inner_end:ml_end]
  )
  return ml_val_start, ml_end, replacement, scrubbed_val != inner_val


def _scrub_sensitive_kv_in_text(text: str, *, _depth: int = 0) -> str:
  """Redacts sensitive key=value / key: value / key is value pairs in O(N)."""
  token = _QUOTE_KV_FAIL_UNTIL.set({})
  obj_cache_token = _JSON_OBJ_CLOSE_CACHE.set({})
  cont_token = _ENCLOSING_CONT_MEMO.set({})
  hdr_token = _UNQUOTED_SENS_HDR_CACHE.set({})
  try:
    return _scrub_sensitive_kv_in_text_impl(text, _depth=_depth)
  finally:
    _UNQUOTED_SENS_HDR_CACHE.reset(hdr_token)
    _ENCLOSING_CONT_MEMO.reset(cont_token)
    _JSON_OBJ_CLOSE_CACHE.reset(obj_cache_token)
    _QUOTE_KV_FAIL_UNTIL.reset(token)


def _scrub_sensitive_kv_in_text_impl(text: str, *, _depth: int = 0) -> str:
  """Redacts sensitive key=value / key: value / key is value pairs in O(N)."""
  out: list[str] = []
  emit_idx = 0
  scan_idx = 0
  n = len(text)
  unclosed_edq_until: dict[int, int] = {}
  enclosing_fail_from: dict[tuple[bool, int, str], int] = {}
  json_close_fail_from: dict[tuple[bool, int, str, int], int] = {}
  brace_index_cache: dict[
      tuple[bool, int], tuple[list[int], dict[int, int]]
  ] = {}
  prose_memo: dict[tuple[bool, int, str], dict[int, bool]] = {}
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
                enclosing_fail_from=enclosing_fail_from,
                json_close_fail_from=json_close_fail_from,
                brace_index_cache=brace_index_cache,
                prose_memo=prose_memo,
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
          if stop_pos < n and text[stop_pos] == '"' and close_start > start + 2:
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
                    enclosing_fail_from=enclosing_fail_from,
                    json_close_fail_from=json_close_fail_from,
                    brace_index_cache=brace_index_cache,
                    prose_memo=prose_memo,
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
              enclosing_fail_from=enclosing_fail_from,
              json_close_fail_from=json_close_fail_from,
              brace_index_cache=brace_index_cache,
              prose_memo=prose_memo,
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
                enclosing_fail_from=enclosing_fail_from,
                json_close_fail_from=json_close_fail_from,
                brace_index_cache=brace_index_cache,
                prose_memo=prose_memo,
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
          if not is_sensitive_outer and _is_json_colon_connector(
              conn_m.group(0)
          ):
            ml_res = _scrub_multiline_nonsensitive_json_value(
                text,
                conn_m.end(),
                '"',
                is_escaped=True,
                lead_bs=lead_bs,
                depth=_depth,
                brace_index_cache=brace_index_cache,
            )
            if ml_res is not None:
              ml_val_start, ml_end, ml_repl, ml_changed = ml_res
              if key_changed or ml_changed:
                prefix = (
                    text[emit_idx:start]
                    + emitted_key
                    + text[end_tok:ml_val_start]
                    if key_changed
                    else text[emit_idx:ml_val_start]
                )
                out.append(prefix + ml_repl)
                emit_idx = ml_end
              scan_idx = ml_end
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
              enclosing_fail_from=enclosing_fail_from,
              json_close_fail_from=json_close_fail_from,
              brace_index_cache=brace_index_cache,
              prose_memo=prose_memo,
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
              text,
              start,
              start + 1,
              decode_escaped=False,
              enclosing_fail_from=enclosing_fail_from,
              json_close_fail_from=json_close_fail_from,
              brace_index_cache=brace_index_cache,
              prose_memo=prose_memo,
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
                enclosing_fail_from=enclosing_fail_from,
                json_close_fail_from=json_close_fail_from,
                brace_index_cache=brace_index_cache,
                prose_memo=prose_memo,
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
          if not is_sensitive_outer and _is_json_colon_connector(
              conn_m.group(0)
          ):
            ml_res = _scrub_multiline_nonsensitive_json_value(
                text,
                conn_m.end(),
                ch,
                is_escaped=False,
                depth=_depth,
                brace_index_cache=brace_index_cache,
            )
            if ml_res is not None:
              ml_val_start, ml_end, ml_repl, ml_changed = ml_res
              if key_changed or ml_changed:
                prefix = (
                    text[emit_idx:start]
                    + emitted_key
                    + text[end_tok:ml_val_start]
                    if key_changed
                    else text[emit_idx:ml_val_start]
                )
                out.append(prefix + ml_repl)
                emit_idx = ml_end
              scan_idx = ml_end
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
              enclosing_fail_from=enclosing_fail_from,
              json_close_fail_from=json_close_fail_from,
              brace_index_cache=brace_index_cache,
              prose_memo=prose_memo,
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
                  enclosing_fail_from=enclosing_fail_from,
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
        enclosing_fail_from=enclosing_fail_from,
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
    r"(?:\b|(?<=\\[nr]))(?:"
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


def _iter_valid_critical_error_markers(
    text: str, start: int = 0, end: int | None = None
):
  """Yields critical error matches, skipping even-backslash \\n/\\r prefixes."""
  stop = len(text) if end is None else end
  for match in _CRITICAL_ERROR_MARKERS_RE.finditer(text, start, stop):
    pos = match.start()
    if (
        pos >= 2
        and text[pos - 2] == "\\"
        and text[pos - 1] in ("n", "r")
        and _count_backslashes_ending_at(text, pos - 2) % 2 == 0
    ):
      continue
    yield match


def _search_valid_critical_error_marker(
    text: str, start: int, end: int
) -> re.Match[str] | None:
  """Returns the first valid critical error match in ``text[start:end]``."""
  for match in _iter_valid_critical_error_markers(text, start, end):
    return match
  return None


def _extract_error_markers(text: str) -> list[str]:
  seen: set[str] = set()
  ordered: list[str] = []
  for match in _iter_valid_critical_error_markers(text):
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
