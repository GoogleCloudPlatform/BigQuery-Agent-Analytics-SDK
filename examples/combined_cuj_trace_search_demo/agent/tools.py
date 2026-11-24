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

"""Deterministic enterprise customer-operations tools and ADK classifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parents[1]
if str(_DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(_DEMO_DIR))

from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: E402
from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: E402


@dataclass(frozen=True)
class _FallbackToolClassification:
  """Fallback classification used when google.adk is unavailable."""

  status: str
  error_message: str | None = None


def scrub_secrets(text: str) -> str:
  """Redacts credential tokens while preserving uppercase error codes."""
  return scrub_sensitive_text(text)


def lookup_order(order_id: str) -> dict[str, Any]:
  """Looks up order metadata and payment/fulfillment status by order ID.

  Args:
      order_id: Order identifier such as 'ORD-9012', 'ORD-8841', 'ORD-7710',
        'ORD-6100', or 'VIP-0001'.

  Returns:
      Dictionary describing order status, tier, amount, and error info if
      access is denied or the order is missing.
  """
  normalized = str(order_id).strip().upper()
  orders: dict[str, dict[str, Any]] = {
      "ORD-9012": {
          "ok": True,
          "order_id": "ORD-9012",
          "customer_tier": "gold",
          "status": "delivered",
          "amount_usd": 79.0,
          "order_age_days": 12,
          "payment_method": "card_ending_4242",
      },
      "ORD-8841": {
          "ok": True,
          "order_id": "ORD-8841",
          "customer_tier": "standard",
          "status": "delivered",
          "amount_usd": 120.0,
          "order_age_days": 5,
          "payment_method": "card_expired_08_25",
      },
      "ORD-7710": {
          "ok": True,
          "order_id": "ORD-7710",
          "customer_tier": "silver",
          "status": "delayed_in_transit",
          "amount_usd": 54.5,
          "order_age_days": 18,
          "warehouse_region": "us-east",
      },
      "ORD-6100": {
          "ok": True,
          "order_id": "ORD-6100",
          "customer_tier": "gold",
          "status": "dispatched",
          "amount_usd": 149.0,
          "order_age_days": 1,
          "tracking_id": "TRK-6100-Z",
      },
  }
  if normalized in orders:
    return dict(orders[normalized])
  if normalized.startswith("VIP"):
    return {
        "ok": False,
        "order_id": normalized,
        "error_code": "PERMISSION_DENIED",
        "message": (
            "PERMISSION_DENIED: caller lacks restricted_vip_orders.read scope"
            f" for {normalized}."
        ),
    }
  return {
      "ok": False,
      "order_id": normalized,
      "error_code": "ORDER_NOT_FOUND",
      "message": f"ORDER_NOT_FOUND: no order record found for {normalized}.",
  }


def issue_refund(
    order_id: str,
    reason: str,
    amount_usd: float,
) -> dict[str, Any]:
  """Issues a refund to the original payment method for an order.

  Args:
      order_id: Target order identifier (for example 'ORD-9012' or 'ORD-8841').
      reason: Short justification string supplied by support policy or user.
      amount_usd: Positive dollar amount to refund.

  Returns:
      Dictionary with ok=True and refund_id on success, or ok=False with an
      explicit error_code such as CARD_EXPIRED or PERMISSION_DENIED.
  """
  normalized = str(order_id).strip().upper()
  if amount_usd <= 0:
    return {
        "ok": False,
        "order_id": normalized,
        "amount_usd": float(amount_usd),
        "error_code": "INVALID_AMOUNT",
        "message": "INVALID_AMOUNT: refund amount must be strictly positive.",
    }
  if normalized == "ORD-8841":
    return {
        "ok": False,
        "order_id": "ORD-8841",
        "reason": reason,
        "amount_usd": float(amount_usd),
        "error_code": "CARD_EXPIRED",
        "message": (
            "Issuer rejected refund: CARD_EXPIRED (card expired 08/25)."
        ),
    }
  if normalized.startswith("VIP"):
    return {
        "ok": False,
        "order_id": normalized,
        "amount_usd": float(amount_usd),
        "error_code": "PERMISSION_DENIED",
        "message": (
            "PERMISSION_DENIED: refunds on restricted VIP accounts require"
            " supervisor approval."
        ),
    }
  if normalized == "ORD-9012":
    return {
        "ok": True,
        "order_id": "ORD-9012",
        "reason": reason,
        "amount_usd": float(amount_usd),
        "refund_id": "REF-3310",
        "status": "refunded",
    }
  suffix = sum(ord(ch) for ch in normalized) % 9000 + 1000
  return {
      "ok": True,
      "order_id": normalized,
      "reason": reason,
      "amount_usd": float(amount_usd),
      "refund_id": f"REF-{suffix}",
      "status": "refunded",
  }


def search_knowledge_base(
    query: str,
    max_results: int = 5,
) -> dict[str, Any]:
  """Searches internal customer-operations policy articles.

  Args:
      query: Natural-language or keyword search query over policy articles.
      max_results: Maximum number of policy snippets to return.

  Returns:
      Dictionary containing matching policy articles, or DEADLINE_EXCEEDED if
      the query triggers the simulated warehouse-timeout condition.
  """
  lowered = str(query).lower()
  if "timeout" in lowered or "ord-7710" in lowered or "deadline" in lowered:
    return {
        "ok": False,
        "query": query,
        "error_code": "DEADLINE_EXCEEDED",
        "retryable": True,
        "message": (
            "DEADLINE_EXCEEDED: knowledge-base backend timed out after 5000ms."
        ),
    }
  articles = [
      {
          "article_id": "KB-101",
          "title": "Standard Refund & Expired-Card Escalation",
          "summary": (
              "Refunds return to the original card. If CARD_EXPIRED occurs,"
              " ask the customer to add a replacement payment method."
          ),
      },
      {
          "article_id": "KB-204",
          "title": "Shipping Delay & Tier Credit Policy",
          "summary": (
              "Gold and Silver tier orders delayed > 48h qualify for up to $15"
              " courtesy shipping credit within 60 days."
          ),
      },
      {
          "article_id": "KB-309",
          "title": "Post-Dispatch Cancellation Restrictions",
          "summary": (
              "Once carrier pickup completes (SHIPMENT_ALREADY_DISPATCHED),"
              " agents must advise waiting for delivery to start a return."
          ),
      },
  ]
  limit = max(1, min(int(max_results), len(articles)))
  return {
      "ok": True,
      "query": query,
      "articles": articles[:limit],
  }


def export_audit_blob(
    account_id: str,
    include_html_snapshot: bool = True,
) -> dict[str, Any]:
  """Exports a verbose diagnostic audit payload with HTML/hex trace blocks.

  Intentionally emits >10 KB of structured HTML/hex boilerplate ending in a
  terminal AUDIT_SYNC_WARNING / CHECKSUM_MISMATCH marker to benchmark
  deterministic S1/S2/S3 trajectory compression.

  Args:
      account_id: Account identifier to audit (e.g. 'acct-992').
      include_html_snapshot: Whether to attach the verbose HTML table dump.

  Returns:
      Verbose diagnostic audit dictionary exceeding 10 KB when HTML is enabled.
  """
  entries = [
      {
          "seq": idx,
          "timestamp": f"2026-11-24T00:{idx // 60:02d}:{idx % 60:02d}.000000Z",
          "shard": f"shard-{idx % 4:02d}",
          "hex_digest": f"0x{idx:08x}{'a5f0c319' * 4}",
          "status": "SYNCED" if idx < 44 else "CHECKSUM_MISMATCH",
      }
      for idx in range(45)
  ]
  html_snapshot = ""
  if include_html_snapshot:
    rows = "".join(
        "<div class='audit-row' data-shard='{shard}'>"
        "<span>{timestamp}</span><span>{hex_digest}</span>"
        "<span>{status}</span></div>".format(**entry)
        for entry in entries
    )
    html_snapshot = (
        f"<html><body><div class='audit-report' data-account='{account_id}'>"
        f"{rows}</div></body></html>"
    )
  return {
      "ok": True,
      "account_id": account_id,
      "ledger_entries": entries,
      "html_snapshot": html_snapshot,
      "warning_code": "AUDIT_SYNC_WARNING",
      "tail_code": "CHECKSUM_MISMATCH",
      "message": (
          "AUDIT_SYNC_WARNING: audit snapshot completed with trailing"
          " CHECKSUM_MISMATCH on shard-04."
      ),
  }


def check_policy_eligibility(
    customer_tier: str,
    order_age_days: int,
) -> dict[str, Any]:
  """Evaluates refund and courtesy shipping-credit eligibility by tier and age.

  Args:
      customer_tier: Membership tier ('gold', 'silver', or 'standard').
      order_age_days: Days elapsed since order placement.

  Returns:
      Dictionary containing eligibility booleans and maximum credit allowed.
  """
  tier = str(customer_tier).strip().lower()
  tier_windows = {
      "gold": (60, 15.0),
      "silver": (30, 10.0),
      "standard": (14, 0.0),
  }
  max_days, credit_usd = tier_windows.get(tier, (14, 0.0))
  eligible = 0 <= int(order_age_days) <= max_days
  return {
      "ok": True,
      "customer_tier": tier,
      "order_age_days": int(order_age_days),
      "max_window_days": max_days,
      "eligible": eligible,
      "courtesy_shipping_credit_usd": credit_usd if eligible else 0.0,
  }


def cancel_order(order_id: str, reason: str) -> dict[str, Any]:
  """Attempts to cancel an order prior to carrier dispatch.

  Args:
      order_id: Target order identifier (e.g. 'ORD-6100').
      reason: Cancellation reason supplied by the customer.

  Returns:
      Dictionary indicating cancellation success or SHIPMENT_ALREADY_DISPATCHED
      if carrier pickup has already occurred.
  """
  normalized = str(order_id).strip().upper()
  if normalized == "ORD-6100":
    return {
        "ok": False,
        "order_id": "ORD-6100",
        "reason": reason,
        "error_code": "SHIPMENT_ALREADY_DISPATCHED",
        "message": (
            "SHIPMENT_ALREADY_DISPATCHED: carrier pickup completed"
            " (TRK-6100-Z); order cannot be cancelled."
        ),
    }
  return {
      "ok": True,
      "order_id": normalized,
      "reason": reason,
      "status": "cancelled",
  }


def sanitized_tool_classifier(
    *,
    tool: Any,
    tool_args: dict[str, Any],
    tool_context: Any,
    result: Any,
) -> Any:
  """Classifies dict-returning tools for BigQueryAgentAnalyticsPlugin safely.

  Scrubs Bearer/OAuth/API-key/password values while keeping error codes such
  as CARD_EXPIRED or DEADLINE_EXCEEDED intact in error_message.
  """
  del tool, tool_args, tool_context
  if not isinstance(result, dict):
    return None
  is_err = (
      result.get("ok") is False
      or bool(result.get("error_code"))
      or bool(result.get("error"))
  )
  if not is_err:
    return None
  safe_result = scrub_sensitive_data(result)
  parts: list[str] = []
  error_code = safe_result.get("error_code") or safe_result.get("error")
  if error_code:
    parts.append(str(error_code))
  message = safe_result.get("message")
  if message:
    parts.append(str(message))
  raw_msg = ": ".join(parts) if parts else "Tool reported failure"
  sanitized = scrub_secrets(raw_msg)[:512]
  try:
    from google.adk.plugins.bigquery_agent_analytics_plugin import ToolResultClassification  # noqa: PLC0415

    return ToolResultClassification(status="ERROR", error_message=sanitized)
  except ImportError:
    return _FallbackToolClassification(status="ERROR", error_message=sanitized)
