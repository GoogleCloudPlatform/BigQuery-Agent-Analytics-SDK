#!/usr/bin/env python3
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

"""Live: runs every query in sql/value_queries.sql and sql/sample_queries.sql
through the analysis skill's runner (its scripts/run_bq.py, which is in the
mod's source repository, haiyuan-eng-google/bigquery_agent_analytics_skill:
name it with RUNNER) against a synthetic fixture, and checks the answers.

The fixture holds what the queries must count right:
  - a row the mod sent twice (a replay: the same event_id);
  - a session with no user_id;
  - other producers in the same table: the settings-hook tracer's rows (a
    turn with three model requests among them, which it writes as one
    response) and
    an ADK agent's, the latter with no event_id and its tokens under
    usage_metadata.total_token_count, with two streamed model calls (a row
    for each chunk and the last; one call's final row marked by cache_type
    and finish_reason, the other's not), each to be counted once; and an
    ADK agent with progressive streaming off, whose rows are those ADK
    2.11's own aggregator and plugin wrote for a call that streamed text
    and then a function call (a partial and a final response under the
    call's span, two more final responses under the agent's), followed by
    a failed tool and a second call, also counted from a window that
    starts after the first call's request; and an ADK agent making
    concurrent calls (three with progressive streaming, two without), in
    the order ADK 2.11 wrote them; and two concurrent calls with
    progressive streaming off whose later finals interleave, under one
    agent span the calls share and under an agent span per worker, as ADK
    2.11 wrote them; and a window that holds only one agent's later finals,
    beside another agent's calls;
  - a subagent run;
  - each kind of refusal: a person's at the prompt, a hook's, a permission
    rule's, and two from elsewhere (a source the queries do not name, and
    none), each to be counted once, the kinds adding up to all refusals;
  - a failed model request with its duration, and a response that
    recorded neither usage nor a duration;
  - a turn whose second model request and tool call end after a window
    that closes before them, seen from that window, from one after the
    turn and from one that starts after the turn's first request ended,
    and a turn with no requests or tool calls;
  - a turn longer than a day, a tool call with a permission wait in it,
    seen from a window whose day of lookback misses the turn's start, and
    from one that covers it;
  - a window with a day of no rows, and a window with none at all.

It makes a dataset of its own (bqaa_query_check_<random>, in US), with the
events table and a team_roster table, prints each query's dry-run bytes,
and deletes the dataset when it is done. It needs the Python BigQuery
client and Application Default Credentials that may create a dataset:

  PROJECT=my-gcp-project python3 tests/queries.live.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
RUNNER = Path(os.environ.get("RUNNER", ""))
START, END = "2026-10-19", "2026-10-20"

MOD_WRITER = {"plugin": "bigquery-agent-analytics-mod", "version": "0.1.0", "label": "bigquery-agent-analytics-mod/0.1.0"}
TRACER_WRITER = {"plugin": "bqaa-coding-agent-plugin", "version": "0.1.0", "label": "bqaa-coding-agent-plugin/0.1.0"}


def usage(input_tokens, output_tokens, cache_read=0, cache_write=0):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
            "prompt_tokens": input_tokens + cache_read + cache_write,
            "total_tokens": input_tokens + output_tokens + cache_read + cache_write}


def row(event_id, ts, event_type, session, *, user="alice", invocation=None, agent="coding-agent", trace=None,
        span=None, parent=None, content=None, attributes=None, latency=None, status="OK", writer=MOD_WRITER):
    attrs = dict(attributes or {})
    if writer is not None:
        attrs["writer"] = writer
    return {"event_id": event_id, "timestamp": ts, "event_type": event_type, "agent": agent, "session_id": session,
            "invocation_id": invocation, "user_id": user, "trace_id": trace, "span_id": span or event_id,
            "parent_span_id": parent, "content": content or {}, "attributes": attrs, "latency_ms": latency,
            "status": status, "is_truncated": False}


D1, D2 = "2026-10-19T10:00:", "2026-10-20T11:00:"
I1 = dict(invocation="I1", trace="trace-1")
FIXTURE = [
    # S1: alice, day 1, one turn with two model requests, a tool that asked
    # for permission and was allowed, and a tool that failed.
    row("e01", D1 + "00Z", "STATE_DELTA", "S1", content={"session_start": True}),
    row("e02", D1 + "01Z", "USER_MESSAGE_RECEIVED", "S1", **I1, content={"text_summary": "list the files"}),
    row("e03", D1 + "01Z", "INVOCATION_STARTING", "S1", **I1, span="inv-1"),
    row("e04", D1 + "02Z", "LLM_REQUEST", "S1", **I1, span="req-1", parent="inv-1", attributes={"model": "model-a"}),
    row("e05", D1 + "03Z", "LLM_RESPONSE", "S1", **I1, span="req-1", parent="inv-1",
        attributes={"model": "model-a", "usage_metadata": usage(100, 20, 50, 10)},
        latency={"total_ms": 1000, "time_to_first_token_ms": 300}),
    row("e06", D1 + "04Z", "TOOL_STARTING", "S1", **I1, span="tool-1", parent="req-1", content={"tool": "Bash"}),
    row("e07", D1 + "05Z", "TOOL_COMPLETED", "S1", **I1, span="tool-1", parent="req-1",
        content={"tool": "Bash", "tool_origin": "builtin"},
        attributes={"permission_requested": True, "permission_decision": "accept", "permission_decision_source": "user_temporary"},
        latency={"total_ms": 500}),
    row("e08", D1 + "06Z", "TOOL_COMPLETED", "S1", **I1, span="tool-2", parent="req-1",
        content={"tool": "Edit", "tool_origin": "builtin"}, latency={"total_ms": 200}, status="ERROR"),
    row("e09", D1 + "07Z", "LLM_RESPONSE", "S1", **I1, span="req-2", parent="inv-1",
        attributes={"model": "model-a", "usage_metadata": usage(10, 5)},
        latency={"total_ms": 400, "time_to_first_token_ms": 100}),
    row("e10", D1 + "08Z", "AGENT_RESPONSE", "S1", **I1, parent="inv-1"),
    row("e11", D1 + "08Z", "INVOCATION_COMPLETED", "S1", **I1, span="inv-1",
        attributes={"usage_metadata": {"total_tokens": 195}, "finish_reason": "end_turn"}, latency={"total_ms": 7000}),
    # S2: bob, day 2: a rejected prompt, a hook's denial, a subagent run, a
    # failed model request and a failed turn.
    row("e12", D2 + "00Z", "USER_MESSAGE_RECEIVED", "S2", user="bob", invocation="I2"),
    row("e13", D2 + "00Z", "INVOCATION_STARTING", "S2", user="bob", invocation="I2"),
    row("e14", D2 + "01Z", "LLM_RESPONSE", "S2", user="bob", invocation="I2",
        attributes={"model": "model-b", "usage_metadata": usage(200, 40)},
        latency={"total_ms": 1500, "time_to_first_token_ms": 500}),
    row("e15", D2 + "02Z", "TOOL_COMPLETED", "S2", user="bob", invocation="I2", content={"tool": "Bash", "tool_origin": "builtin"},
        attributes={"permission_requested": True, "permission_decision": "reject", "permission_decision_source": "user_reject"},
        latency={"total_ms": 300},
        status="ERROR"),
    row("e16", D2 + "03Z", "TOOL_COMPLETED", "S2", user="bob", invocation="I2", content={"tool": "WebFetch", "tool_origin": "builtin"},
        attributes={"denied": True, "permission_decision": "reject", "permission_decision_source": "hook"},
        latency={"total_ms": 0}, status="ERROR"),
    # A permission rule's refusal.
    row("e24", D2 + "03Z", "TOOL_COMPLETED", "S2", user="bob", invocation="I2", content={"tool": "Write", "tool_origin": "builtin"},
        attributes={"permission_decision": "reject", "permission_decision_source": "config"}, latency={"total_ms": 0},
        status="ERROR"),
    # Refusals from elsewhere: a source the queries do not name (the mod
    # records `unknown` for a source the host did not give) and none at all.
    row("e25", D2 + "03Z", "TOOL_COMPLETED", "S2", user="bob", invocation="I2", content={"tool": "Glob", "tool_origin": "builtin"},
        attributes={"permission_requested": True, "permission_decision": "reject", "permission_decision_source": "unknown"},
        latency={"total_ms": 0}, status="ERROR"),
    row("e26", D2 + "03Z", "TOOL_COMPLETED", "S2", user="bob", invocation="I2", content={"tool": "Glob", "tool_origin": "builtin"},
        attributes={"permission_decision": "reject"}, latency={"total_ms": 0}, status="ERROR"),
    row("e17", D2 + "04Z", "AGENT_STARTING", "S2", user="bob", invocation="I2", agent="general-purpose"),
    row("e18", D2 + "05Z", "LLM_RESPONSE", "S2", user="bob", invocation="I2", agent="general-purpose",
        attributes={"model": "model-a", "usage_metadata": usage(30, 20)},
        latency={"total_ms": 800, "time_to_first_token_ms": 200}),
    row("e19", D2 + "06Z", "AGENT_COMPLETED", "S2", user="bob", invocation="I2", agent="general-purpose",
        attributes={"usage_metadata": {"total_tokens": 50}}, latency={"total_ms": 2000}),
    # A request that failed after 6 s: its time is model time too.
    row("e20", D2 + "07Z", "LLM_ERROR", "S2", user="bob", invocation="I2", attributes={"model": "model-a"},
        latency={"total_ms": 6000}, status="ERROR"),
    row("e21", D2 + "08Z", "INVOCATION_ERROR", "S2", user="bob", invocation="I2", latency={"total_ms": 9000},
        status="ERROR"),
    # S3: no user_id.
    row("e22", "2026-10-20T12:00:00Z", "LLM_RESPONSE", "S3", user=None, invocation="I3",
        attributes={"model": "model-a", "usage_metadata": usage(20, 10)}, latency={"total_ms": 600}),
    # A response that recorded no usage and no duration: unknown, not zero.
    row("e23", "2026-10-20T12:00:01Z", "LLM_RESPONSE", "S3", user=None, invocation="I3",
        content={"response": "ok"}, attributes={"model": "model-a"}),
    # The settings-hook tracer, writing beside the mod.
    row("t01", "2026-10-20T13:00:00Z", "LLM_RESPONSE", "T1", writer=TRACER_WRITER,
        attributes={"model": "model-a", "usage_metadata": {"input_tokens": 900, "output_tokens": 99, "total_tokens": 999}}),
    row("t02", "2026-10-20T13:00:01Z", "TOOL_COMPLETED", "T1", writer=TRACER_WRITER, content={"tool": "Bash"},
        status="ERROR"),
    # An ADK agent: no writer, no event_id, tokens as total_token_count.
    row(None, "2026-10-20T14:00:00Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None,
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 77}}),
    row(None, "2026-10-20T14:00:01Z", "TOOL_COMPLETED", "A1", user="carol", agent="adk-agent", writer=None,
        content={"tool": "search"}, status="ERROR"),
    # Two streamed ADK model calls: a row for each chunk and one for the
    # final response, under the call's span_id. The first call's final row
    # has cache_type and finish_reason (and shares its second with a chunk);
    # the second call's rows have neither, as an older plugin writes them.
    row(None, "2026-10-20T14:00:02Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None, span="adk-call-2",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 5}}),
    row(None, "2026-10-20T14:00:04Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None, span="adk-call-2",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 12}}),
    row(None, "2026-10-20T14:00:04Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None, span="adk-call-2",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 40}, "cache_type": "none", "finish_reason": "STOP"}),
    row(None, "2026-10-20T14:00:05Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None, span="adk-call-3",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 3}}),
    row(None, "2026-10-20T14:00:06Z", "LLM_RESPONSE", "A1", user="carol", agent="adk-agent", writer=None, span="adk-call-3",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 30}}),
    # Progressive streaming off: one call that streamed text, then a
    # function call carrying the call's usage. ADK 2.11 wrote a partial and a
    # final response under the call's span, then two more final responses
    # under the agent's span, each with the call's 100 tokens; a failed
    # tool and a second, unstreamed call follow.
    row(None, "2026-10-20T15:00:00Z", "LLM_REQUEST", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-req-1", parent="adk-agent-1", attributes={"model": "model-c"}),
    row(None, "2026-10-20T15:00:01Z", "LLM_RESPONSE", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-req-1", parent="adk-agent-1", attributes={"model": "model-c"}),
    row(None, "2026-10-20T15:00:02Z", "LLM_RESPONSE", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-req-1", parent="adk-agent-1",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 100}, "cache_type": "none", "finish_reason": "STOP"}),
    row(None, "2026-10-20T15:00:02Z", "LLM_RESPONSE", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-agent-1", attributes={"model": "model-c", "usage_metadata": {"total_token_count": 100}, "cache_type": "none", "finish_reason": "STOP"}),
    row(None, "2026-10-20T15:00:02Z", "LLM_RESPONSE", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-agent-1", attributes={"model": "model-c", "usage_metadata": {"total_token_count": 100}, "cache_type": "none", "finish_reason": "STOP"}),
    row(None, "2026-10-20T15:00:03Z", "TOOL_COMPLETED", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-tool-1", parent="adk-agent-1", content={"tool": "search"}, status="ERROR"),
    row(None, "2026-10-20T15:00:04Z", "LLM_REQUEST", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-req-2", parent="adk-agent-1", attributes={"model": "model-c"}),
    row(None, "2026-10-20T15:00:05Z", "LLM_RESPONSE", "A2", user="carol", agent="adk-legacy", writer=None, invocation="IA2",
        span="adk-req-2", parent="adk-agent-1",
        attributes={"model": "model-c", "usage_metadata": {"total_token_count": 50}, "cache_type": "none", "finish_reason": "STOP"}),
]
# Concurrent calls of one ADK agent, as ADK 2.11's aggregator and plugin
# wrote them for workers each in their own task under one agent span: every
# request first, then each call's responses on its own span (and, with
# progressive streaming off, two more finals under the agent's span).
def fanout(session, agent, calls, start_minute, legacy):
    out, second = [], 0
    def at():
        nonlocal second
        second += 1
        return f"2026-10-20T{start_minute}:{second:02d}Z"
    agent_span = f"{agent}-span"
    for i, _ in enumerate(calls, 1):
        out.append(row(None, at(), "LLM_REQUEST", session, user="carol", agent=agent, writer=None, invocation=f"I{session}",
                       span=f"{agent}-req-{i}", parent=agent_span, attributes={"model": "model-c"}))
    final = {"cache_type": "none", "finish_reason": "STOP"}
    for i, tokens in enumerate(calls, 1):
        usage = {"usage_metadata": {"total_token_count": tokens}}
        out.append(row(None, at(), "LLM_RESPONSE", session, user="carol", agent=agent, writer=None, invocation=f"I{session}",
                       span=f"{agent}-req-{i}", parent=agent_span, attributes={"model": "model-c", **({} if legacy else usage)}))
        closed = at()
        out.append(row(None, closed, "LLM_RESPONSE", session, user="carol", agent=agent, writer=None, invocation=f"I{session}",
                       span=f"{agent}-req-{i}", parent=agent_span, attributes={"model": "model-c", **usage, **final}))
        if legacy:
            for _ in range(2):
                out.append(row(None, closed, "LLM_RESPONSE", session, user="carol", agent=agent, writer=None, invocation=f"I{session}",
                               span=agent_span, attributes={"model": "model-c", **usage, **final}))
    return out


FIXTURE += fanout("A3", "adk-fanout", [15, 15, 15], "16:00", legacy=False)
FIXTURE += fanout("A4", "adk-fanout-legacy", [100, 40], "17:00", legacy=True)


# Two concurrent calls with progressive streaming off whose callbacks
# interleave (both texts, both own finals, then each call's two later
# finals), as ADK 2.11 wrote them: under the one agent span the calls share,
# and under an agent span per worker.
def interleaved(session, agent, per_worker):
    out, second = [], 0
    def r(event_type, span, parent, tokens=None, final=False):
        nonlocal second
        second += 1
        attributes = {"model": "model-c"}
        if tokens is not None:
            attributes["usage_metadata"] = {"total_token_count": tokens}
        if final:
            attributes.update(cache_type="none", finish_reason="STOP")
        out.append(row(None, f"2026-10-20T18:00:{second:02d}Z", event_type, session, user="carol", agent=agent, writer=None,
                       invocation=f"I{session}", span=span, parent=parent, attributes=attributes))
    shared = f"{agent}-span"
    spans = {i: (f"{agent}-worker-{i}" if per_worker else shared) for i in (1, 2)}
    for i in (1, 2):
        r("LLM_REQUEST", f"{agent}-req-{i}", spans[i])
    for i in (1, 2):
        r("LLM_RESPONSE", f"{agent}-req-{i}", spans[i])
    for i, tokens in ((1, 100), (2, 40)):
        r("LLM_RESPONSE", f"{agent}-req-{i}", spans[i], tokens, final=True)
    for i, tokens in ((1, 100), (2, 40)):
        for _ in range(2):
            r("LLM_RESPONSE", spans[i], shared if per_worker else None, tokens, final=True)
    return out


FIXTURE += interleaved("A5", "adk-shared-interleaved", per_worker=False)
FIXTURE += interleaved("A6", "adk-workers-interleaved", per_worker=True)


# Staggered orders under one shared agent span (progressive streaming off):
# a call is requested and closes between an earlier call's own final and its
# later finals. R is a request, F a call's own final, L a later final on the
# agent span; the digits are the calls.
def staggered(session, agent, order, tokens, error_in=None):
    out, second = [], 0
    agent_span = f"{agent}-span"
    for step in order:
        kind, call = step[0], int(step[1])
        second += 1
        at = f"2026-10-20T19:{second // 60:02d}:{second % 60:02d}Z"
        common = dict(user="carol", agent=agent, writer=None, invocation=f"I{session}")
        if kind == "R":
            out.append(row(None, at, "LLM_REQUEST", session, span=f"{agent}-req-{call}", parent=agent_span,
                           attributes={"model": "model-c"}, **common))
        else:
            attributes = {"model": "model-c", "usage_metadata": {"total_token_count": tokens[call]},
                          "cache_type": "none", "finish_reason": "STOP"}
            span, parent = (f"{agent}-req-{call}", agent_span) if kind == "F" else (agent_span, None)
            status = "ERROR" if error_in == step else "OK"
            out.append(row(None, at, "LLM_RESPONSE", session, span=span, parent=parent, attributes=attributes,
                           status=status, **common))
    return out


FIXTURE += staggered("A7", "adk-staggered", ["R1", "F1", "R2", "F2", "L1", "L1", "L2", "L2"], {1: 100, 2: 40})
FIXTURE += staggered("A8", "adk-staggered-three",
                     ["R1", "R2", "F1", "R3", "F2", "L1", "L1", "F3", "L2", "L2", "L3", "L3"], {1: 100, 2: 40, 3: 30})
# A later final that reports an error is kept as an error row.
FIXTURE += staggered("A9", "adk-later-error", ["R1", "F1", "L1"], {1: 50}, error_in="L1")
# A turn of the settings-hook tracer's with three model requests and two
# tool calls (300 tokens), as it writes one: a request at the prompt, the
# tool calls, and one response at Stop with the turn's usage summed.
TRACER_TURN = dict(writer=TRACER_WRITER, invocation="T2-1")
FIXTURE += [
    row("t03", "2026-10-20T13:10:00Z", "LLM_REQUEST", "T2", **TRACER_TURN, attributes={"model": "model-a"}),
    row("t04", "2026-10-20T13:10:01Z", "TOOL_STARTING", "T2", **TRACER_TURN, content={"tool": "Bash"}),
    row("t05", "2026-10-20T13:10:02Z", "TOOL_COMPLETED", "T2", **TRACER_TURN, content={"tool": "Bash"},
        latency={"total_ms": 900}),
    row("t06", "2026-10-20T13:10:03Z", "TOOL_STARTING", "T2", **TRACER_TURN, content={"tool": "Read"}),
    row("t07", "2026-10-20T13:10:04Z", "TOOL_COMPLETED", "T2", **TRACER_TURN, content={"tool": "Read"},
        latency={"total_ms": 100}),
    row("t08", "2026-10-20T13:10:05Z", "LLM_RESPONSE", "T2", **TRACER_TURN,
        attributes={"model": "model-a", "usage_metadata": usage(200, 100)}, latency={"total_ms": 5000}),
]
# S9, on a day outside the other windows: a turn whose second model request
# and tool call end at 20:00:20 and 20:00:21, after a window that closes at
# 20:00:10; then a turn with no requests or tool calls.
D9 = "2026-10-22T20:00:"
I9 = dict(invocation="I9", trace="trace-9")
FIXTURE += [
    row("e91", D9 + "00Z", "INVOCATION_STARTING", "S9", **I9, span="inv-9"),
    row("e92", D9 + "01Z", "LLM_REQUEST", "S9", **I9, span="req-91", parent="inv-9", attributes={"model": "model-a"}),
    row("e93", D9 + "02Z", "LLM_RESPONSE", "S9", **I9, span="req-91", parent="inv-9",
        attributes={"model": "model-a", "usage_metadata": usage(10, 5)}, latency={"total_ms": 1000}),
    row("e94", D9 + "03Z", "LLM_REQUEST", "S9", **I9, span="req-92", parent="inv-9", attributes={"model": "model-a"}),
    row("e95", D9 + "04Z", "TOOL_STARTING", "S9", **I9, span="tool-91", parent="req-91", content={"tool": "Bash"}),
    row("e96", D9 + "20Z", "LLM_RESPONSE", "S9", **I9, span="req-92", parent="inv-9",
        attributes={"model": "model-a", "usage_metadata": usage(10, 5)}, latency={"total_ms": 17000}),
    row("e97", D9 + "21Z", "TOOL_COMPLETED", "S9", **I9, span="tool-91", parent="req-91",
        content={"tool": "Bash", "tool_origin": "builtin"}, latency={"total_ms": 17000}),
    row("e98", D9 + "22Z", "INVOCATION_COMPLETED", "S9", **I9, span="inv-9", latency={"total_ms": 22000}),
    row("e99", D9 + "23Z", "INVOCATION_STARTING", "S9", invocation="I9b", trace="trace-9b", span="inv-9b"),
    row("e9a", D9 + "24Z", "INVOCATION_COMPLETED", "S9", invocation="I9b", trace="trace-9b", span="inv-9b",
        latency={"total_ms": 1000}),
]
# SL, on days outside the other windows: a turn of 25 hours. Its tool call,
# which asked for permission, waited from 2026-10-23 10:00:03 to 2026-10-24
# 11:00:03.
IL = dict(invocation="IL", trace="trace-l")
FIXTURE += [
    row("l01", "2026-10-23T10:00:00Z", "INVOCATION_STARTING", "SL", **IL, span="inv-l"),
    row("l02", "2026-10-23T10:00:01Z", "LLM_REQUEST", "SL", **IL, span="req-l1", parent="inv-l", attributes={"model": "model-a"}),
    row("l03", "2026-10-23T10:00:02Z", "LLM_RESPONSE", "SL", **IL, span="req-l1", parent="inv-l",
        attributes={"model": "model-a", "usage_metadata": usage(10, 5)}, latency={"total_ms": 1000}),
    row("l04", "2026-10-23T10:00:03Z", "TOOL_STARTING", "SL", **IL, span="tool-l1", parent="req-l1", content={"tool": "Bash"}),
    row("l05", "2026-10-24T11:00:03Z", "TOOL_COMPLETED", "SL", **IL, span="tool-l1", parent="req-l1",
        content={"tool": "Bash", "tool_origin": "builtin"},
        attributes={"permission_requested": True, "permission_decision": "accept", "permission_decision_source": "user_temporary"},
        latency={"total_ms": 90000000}),
    row("l06", "2026-10-24T11:00:04Z", "INVOCATION_COMPLETED", "SL", **IL, span="inv-l", latency={"total_ms": 90004000}),
]
# The replay: e05 sent twice.
FIXTURE.append(dict(FIXTURE[4]))
ROSTER = ["alice", "bob", "dave"]
RATES = """SELECT * FROM UNNEST([
    STRUCT('model-a' AS model, DATE '2026-10-01' AS rates_as_of, 'fixture' AS rates_source,
           2.0 AS input_usd, 0.5 AS cache_read_usd, 4.0 AS cache_write_usd, 10.0 AS output_usd)
  ])"""


def by(rows, *keys):
    return {tuple(r[k] for k in keys) if len(keys) > 1 else r[keys[0]]: r for r in rows}


def near(a, b):
    return a is not None and abs(float(a) - b) < 1e-9


def checks(name: str, rows: list[dict]):
    """(description, passed) for a query's answer on the fixture."""
    if name == "V1":
        s = by(rows, "session_id")
        yield "the mod's sessions, and no other producer's", set(s) == {"S1", "S2", "S3"}
        yield "S1: 2 model requests and 195 tokens (the replayed row counted once)", s["S1"]["model_requests"] == 2 and s["S1"]["tokens"] == 195
        yield "S1: 2 tool calls, 1 failed; 1 prompt, 1 turn", (s["S1"]["tool_calls"], s["S1"]["failed_tool_calls"], s["S1"]["prompts"], s["S1"]["turns"]) == (2, 1, 1, 1)
        yield "S2: 290 tokens with the subagent's, 5 tool calls, all failed", (s["S2"]["tokens"], s["S2"]["tool_calls"], s["S2"]["failed_tool_calls"]) == (290, 5, 5)
        yield "S3: kept with no user_id; 2 responses, 1 without usage, 30 tokens recorded", "S3" in s and s["S3"]["user_id"] is None and (s["S3"]["model_requests"], s["S3"]["responses_without_usage"], s["S3"]["tokens"]) == (2, 1, 30)
    elif name == "V2":
        t = by(rows, "invocation_id")
        yield "I1: turn 7000 ms, models 1400 ms, tools 700 ms, 1 permission prompt", (t["I1"]["turn_ms"], t["I1"]["model_ms"], t["I1"]["tool_ms"], t["I1"]["permission_prompts"]) == (7000, 1400, 700, 1)
        yield "I2: turn 9000 ms, models 8300 ms (with the subagent's and the 6000 ms failed request), tools 300 ms", (t["I2"]["turn_ms"], t["I2"]["model_ms"], t["I2"]["tool_ms"]) == (9000, 8300, 300)
        yield "I3: no end of turn, and a response with no duration: turn_ms and model_ms are NULL, not 0", t["I3"]["turn_ms"] is None and t["I3"]["model_ms"] is None
        yield "I3, with no start row read, may be missing earlier calls: its tool time and prompts are NULL too", (t["I3"]["tool_ms"], t["I3"]["permission_prompts"]) == (None, None)
        yield "whole_turn: I1 and I2 (their start rows were read), not I3", (t["I1"].get("whole_turn"), t["I2"].get("whole_turn"), t["I3"].get("whole_turn")) == (True, True, False)
    elif name == "V2 ends mid-turn":
        t = by(rows, "invocation_id")
        yield "a window that ends before a model request and a tool call end: that turn's model and tool time are NULL, not 1000 and 0 ms", "I9" in t and (t["I9"]["turn_ms"], t["I9"]["model_ms"], t["I9"]["tool_ms"]) == (None, None, None)
    elif name == "V2 after the turn":
        t = by(rows, "invocation_id")
        yield "a window after both ended: turn 22000 ms, models 18000 ms, tools 17000 ms", "I9" in t and (t["I9"]["turn_ms"], t["I9"]["model_ms"], t["I9"]["tool_ms"]) == (22000, 18000, 17000)
        yield "a turn with no requests or tool calls: 0 ms for both, not NULL", "I9b" in t and (t["I9b"]["turn_ms"], t["I9b"]["model_ms"], t["I9b"]["tool_ms"]) == (1000, 0, 0)
    elif name == "V2 starts mid-turn":
        t = by(rows, "invocation_id")
        yield "a window that starts after the turn's first request ended: the turn is taken whole, from when it started: models 18000 ms, not 17000", "I9" in t and (t["I9"]["turn_ms"], t["I9"]["model_ms"], t["I9"]["tool_ms"], t["I9"]["started_at"].replace("T", " ")[:19]) == (22000, 18000, 17000, "2026-10-22 20:00:00")
    elif name == "V2 past the lookback":
        t = by(rows, "invocation_id")
        yield "a turn that began more than a day before the window: NULL model and tool time and prompts, not 0 and 90000000 ms", "IL" in t and (t["IL"]["turn_ms"], t["IL"]["model_ms"], t["IL"]["tool_ms"], t["IL"]["permission_prompts"]) == (90004000, None, None, None)
        yield "its start from its end row, 2026-10-23 10:00:00, not the first row read", "IL" in t and str(t["IL"]["started_at"]).replace("T", " ")[:19] == "2026-10-23 10:00:00"
        yield "and whole_turn says it is not whole", "IL" in t and t["IL"].get("whole_turn") is False
    elif name == "V2 whole long turn":
        t = by(rows, "invocation_id")
        yield "the same turn from a window whose lookback reaches its start: models 1000 ms, tools 90000000 ms, 1 permission prompt, from 2026-10-23 10:00:00", "IL" in t and (str(t["IL"]["started_at"]).replace("T", " ")[:19], t["IL"]["turn_ms"], t["IL"]["model_ms"], t["IL"]["tool_ms"], t["IL"]["permission_prompts"]) == ("2026-10-23 10:00:00", 90004000, 1000, 90000000, 1)
        yield "and it is whole", "IL" in t and t["IL"].get("whole_turn") is True
    elif name == "V3":
        d = by(rows, "day")
        yield "day 1: 3 enrolled, 1 seen, 2 not seen, 1 session, 1 turn", (d["2026-10-19"]["enrolled"], d["2026-10-19"]["enrolled_seen"], d["2026-10-19"]["enrolled_not_seen"], d["2026-10-19"]["enrolled_sessions"], d["2026-10-19"]["enrolled_turns"]) == (3, 1, 2, 1, 1)
        yield "day 2: 1 enrolled seen (bob); the session with no user_id is apart", (d["2026-10-20"]["enrolled_seen"], d["2026-10-20"]["enrolled_sessions"], d["2026-10-20"]["other_sessions"]) == (1, 1, 1)
        yield "the tracer's and ADK's people are not counted", all(r["enrolled_sessions"] + r["other_sessions"] <= 2 for r in rows)
        yield "both days have rows", [r["no_rows_that_day"] for r in rows] == [False, False]
    elif name == "V3 silent day":
        d = by(rows, "day")
        yield "a day with no rows is listed: 3 enrolled, 0 seen, 3 not seen, no rows that day", set(d) == {"2026-10-19", "2026-10-20", "2026-10-21"} and (d["2026-10-21"]["enrolled"], d["2026-10-21"]["enrolled_seen"], d["2026-10-21"]["enrolled_not_seen"], d["2026-10-21"]["enrolled_sessions"], d["2026-10-21"]["no_rows_that_day"]) == (3, 0, 3, 0, True)
    elif name == "V3 empty window":
        yield "a window with no rows at all lists each day, everyone not seen", len(rows) == 2 and all((r["enrolled"], r["enrolled_not_seen"], r["no_rows_that_day"]) == (3, 3, True) for r in rows)
    elif name == "V4":
        c = by(rows, "session_id", "model")
        yield "S1 model-a: 2 requests; 110 input, 50 cache reads, 10 cache writes, 25 output", (c[("S1", "model-a")]["model_requests"], c[("S1", "model-a")]["input_tokens"], c[("S1", "model-a")]["cache_read_tokens"], c[("S1", "model-a")]["cache_write_tokens"], c[("S1", "model-a")]["output_tokens"]) == (2, 110, 50, 10, 25)
        yield "S1 model-a: estimated 0.000535 USD at the fixture's rates, dated", near(c[("S1", "model-a")]["estimated_usd"], 0.000535) and c[("S1", "model-a")]["rates_as_of"] == "2026-10-01"
        yield "S2 model-b: no rate row, so no cost (NULL, not 0)", c[("S2", "model-b")]["estimated_usd"] is None
        yield "S2 model-a (the subagent's request): 0.00026 USD", near(c[("S2", "model-a")]["estimated_usd"], 0.00026)
        yield "S3 model-a: a response without usage makes the estimate unknown (NULL), at a matching rate", (c[("S3", "model-a")]["model_requests"], c[("S3", "model-a")]["responses_without_usage"], c[("S3", "model-a")]["estimated_usd"]) == (2, 1, None)
        yield "the tracer's model-a response is not counted", ("T1", "model-a") not in c
    elif name == "V5":
        t = by(rows, "tool")
        yield "tools: Bash, Edit, Glob, WebFetch, Write (not the tracer's or ADK's)", set(t) == {"Bash", "Edit", "Glob", "WebFetch", "Write"}
        yield "Bash: 2 people, 2 calls, 50% failed, 100% asked, 50% refused, all at the prompt", (t["Bash"]["people"], t["Bash"]["calls"], t["Bash"]["error_pct"], t["Bash"]["asked_pct"], t["Bash"]["refused_pct"], t["Bash"]["rejected_at_prompt_pct"], t["Bash"]["denied_by_hook_pct"], t["Bash"]["denied_by_rule_pct"], t["Bash"]["refused_other_pct"]) == (2, 2, 50.0, 100.0, 50.0, 50.0, 0.0, 0.0, 0.0)
        yield "Glob: refusals from an unnamed source and from none, both as other: 100% refused", (t["Glob"]["calls"], t["Glob"]["asked_pct"], t["Glob"]["refused_pct"], t["Glob"]["refused_other_pct"], t["Glob"]["rejected_at_prompt_pct"], t["Glob"]["denied_by_hook_pct"], t["Glob"]["denied_by_rule_pct"]) == (2, 50.0, 100.0, 100.0, 0.0, 0.0, 0.0)
        yield "WebFetch: a hook's refusal (source hook, denied), counted once, as the hook's", (t["WebFetch"]["denied_by_hook_pct"], t["WebFetch"]["rejected_at_prompt_pct"], t["WebFetch"]["denied_by_rule_pct"]) == (100.0, 0.0, 0.0)
        yield "Write: a rule's refusal (source config), not a person's", (t["Write"]["denied_by_rule_pct"], t["Write"]["rejected_at_prompt_pct"], t["Write"]["denied_by_hook_pct"]) == (100.0, 0.0, 0.0)
        yield "each tool's refusal kinds add up to its refused share", all(near(r["rejected_at_prompt_pct"] + r["denied_by_hook_pct"] + r["denied_by_rule_pct"] + r["refused_other_pct"], r["refused_pct"]) for r in rows)
    elif name == "V6":
        d = by(rows, "day")
        yield "day 1: 1 turn, 0 failed; 2 model requests; 2 tool calls, 1 failed", (d["2026-10-19"]["turns"], d["2026-10-19"]["failed_turns"], d["2026-10-19"]["model_requests"], d["2026-10-19"]["failed_model_requests"], d["2026-10-19"]["tool_calls"], d["2026-10-19"]["failed_tool_calls"]) == (1, 0, 2, 0, 2, 1)
        yield "day 2: 1 failed turn; 5 model requests, 1 failed; 5 tool calls failed, all refused: 1 at the prompt, 1 by a hook, 1 by a rule, 2 from elsewhere", (d["2026-10-20"]["failed_turns"], d["2026-10-20"]["model_requests"], d["2026-10-20"]["failed_model_requests"], d["2026-10-20"]["tool_calls"], d["2026-10-20"]["failed_tool_calls"], d["2026-10-20"]["refused_tool_calls"], d["2026-10-20"]["rejected_at_prompt"], d["2026-10-20"]["denied_by_hook"], d["2026-10-20"]["denied_by_rule"], d["2026-10-20"]["refused_other"]) == (1, 5, 1, 5, 5, 5, 1, 1, 1, 2)
        yield "each day's refusal kinds add up to its refused calls (5 distinct refused calls on day 2)", all(r["rejected_at_prompt"] + r["denied_by_hook"] + r["denied_by_rule"] + r["refused_other"] == r["refused_tool_calls"] for r in rows) and d["2026-10-20"]["refused_tool_calls"] == len({x["event_id"] for x in FIXTURE if x["event_type"] == "TOOL_COMPLETED" and x["timestamp"].startswith("2026-10-20") and x["attributes"].get("writer") == MOD_WRITER and (x["attributes"].get("permission_decision") == "reject" or x["attributes"].get("denied"))})
    elif name == "V7":
        p = by(rows, "producer", "agent")
        mod = p[("bigquery-agent-analytics-mod", "coding-agent")]
        yield "the mod (main loop): 3 sessions, 5 model requests, 465 tokens, 34.8% rows in error", (mod["sessions"], mod["model_requests"], mod["tokens"], mod["error_row_pct"]) == (3, 5, 465, 34.8)
        yield "the mod's subagent: 1 request, 50 tokens", (p[("bigquery-agent-analytics-mod", "general-purpose")]["model_requests"], p[("bigquery-agent-analytics-mod", "general-purpose")]["tokens"]) == (1, 50)
        yield "the settings-hook tracer is left out, its three-request turn too: it writes one response per turn", all(r["producer"] != "bqaa-coding-agent-plugin" for r in rows)
        adk = p[("adk (no writer)", "adk-agent")]
        yield "ADK, normalized: each streamed call once, with its largest token count: 3 model requests, 147 tokens (77 from total_token_count), 25.0% of 4 rows in error, rows with no event_id kept", (adk["sessions"], adk["model_requests"], adk["tokens"], adk["error_row_pct"]) == (1, 3, 147, 25.0)
        legacy = p[("adk (no writer)", "adk-legacy")]
        yield "ADK with progressive streaming off, sequential calls: each counted from its own span's rows, the later finals under the agent's span left out: 2 model requests, 150 tokens (100 once, then 50), 20.0% of 5 rows in error", (legacy["sessions"], legacy["model_requests"], legacy["model_requests_unmatched"], legacy["tokens"], legacy["error_row_pct"]) == (1, 2, 0, 150, 20.0)
        yield "ADK's streamed calls with no request rows: counted by their span, and said so (3 unmatched)", adk["model_requests_unmatched"] == 3
        fan = p[("adk (no writer)", "adk-fanout")]
        yield "ADK, three concurrent calls of one agent: 3 model requests, 45 tokens", (fan["model_requests"], fan["model_requests_unmatched"], fan["tokens"], fan["error_row_pct"]) == (3, 0, 45, 0.0)
        fan_legacy = p[("adk (no writer)", "adk-fanout-legacy")]
        yield "ADK, two concurrent calls with progressive streaming off: 2 model requests, 140 tokens (100 and 40), their 4 later finals left out", (fan_legacy["model_requests"], fan_legacy["model_requests_unmatched"], fan_legacy["tokens"], fan_legacy["later_finals"]) == (2, 0, 140, 4)
        shared = p[("adk (no writer)", "adk-shared-interleaved")]
        yield "ADK, interleaved later finals under a shared agent span: 2 model requests, 140 tokens, not 200", (shared["model_requests"], shared["tokens"], shared["later_finals"]) == (2, 140, 4)
        workers = p[("adk (no writer)", "adk-workers-interleaved")]
        yield "ADK, the same with an agent span per worker (ADK's layout): 2 model requests, 140 tokens", (workers["model_requests"], workers["tokens"], workers["later_finals"]) == (2, 140, 4)
        staggered_two = p[("adk (no writer)", "adk-staggered")]
        yield "ADK, a call requested and closed between another's own final and its later finals, under one span: 2 model requests, 140 tokens, not 200", (staggered_two["model_requests"], staggered_two["tokens"], staggered_two["later_finals"]) == (2, 140, 4)
        staggered_three = p[("adk (no writer)", "adk-staggered-three")]
        yield "ADK, three staggered calls of 100, 40 and 30 under one span: 3 model requests, 170 tokens, not 230", (staggered_three["model_requests"], staggered_three["tokens"], staggered_three["later_finals"]) == (3, 170, 6)
        later_error = p[("adk (no writer)", "adk-later-error")]
        yield "ADK, a later final that reports an error is kept as an error row: 1 model request, 50 tokens, 33.3% of 3 rows", (later_error["model_requests"], later_error["tokens"], later_error["later_finals"], later_error["error_row_pct"]) == (1, 50, 1, 33.3)
        yield "sequential calls leave out their later finals (2) and keep 20.0% of 5 rows; unstreamed calls have none", (legacy["later_finals"], legacy["error_row_pct"], fan["later_finals"], adk["later_finals"]) == (2, 20.0, 0, 0)
        yield "a call whose own rows recorded no usage adds no tokens and is shown: the mod's main loop has 1", (mod["model_requests_without_usage"], adk["model_requests_without_usage"]) == (1, 0)
        yield "the mod's rows are never unmatched", all(r["model_requests_unmatched"] == 0 for r in rows if r["producer"] != "adk (no writer)")
    elif name == "V7 later finals only":
        p = by(rows, "producer", "agent")
        only = p.get(("adk (no writer)", "adk-staggered"))
        yield "a window holding only an agent's later finals: 0 model requests, 4 later finals, 0 tokens, no error rows' share (NULL)", only is not None and (only["model_requests"], only["later_finals"], only["tokens"], only["error_row_pct"]) == (0, 4, 0, None)
        three = p.get(("adk (no writer)", "adk-staggered-three"))
        yield "beside it, another agent's calls requested before the window: 2 model requests, matched, 70 tokens, 2 later finals left out, 0.0% in error", three is not None and (three["model_requests"], three["model_requests_unmatched"], three["tokens"], three["later_finals"], three["error_row_pct"]) == (2, 0, 70, 2, 0.0)
    elif name == "V7 clipped":
        p = by(rows, "producer", "agent")
        legacy = p[("adk (no writer)", "adk-legacy")]
        yield "from just after the first call's request: that call is still one, matched from before the window: 2 model requests, 150 tokens, 25.0% of the 4 rows inside", (legacy["model_requests"], legacy["model_requests_unmatched"], legacy["tokens"], legacy["error_row_pct"]) == (2, 0, 150, 25.0)
    elif name == "1":
        yield "the mod's rows, each once: 26", len(rows) == 26
    elif name == "2":
        c = by(rows, "session_id", "model")
        yield "S1 model-a: 2 requests, 25 output tokens, cache-read share 0.294", (c[("S1", "model-a")]["model_requests"], c[("S1", "model-a")]["output_tokens"], c[("S1", "model-a")]["cache_read_share"]) == (2, 25, 0.294)
        yield "the tracer's response is not counted", ("T1", "model-a") not in c
    elif name == "3":
        m = by(rows, "model")
        yield "model-a: 5 requests (the replay once, no tracer's); model-b: 1", (m["model-a"]["requests"], m["model-b"]["requests"]) == (5, 1) and "model-c" not in m
    elif name == "4":
        t = by(rows, "tool_name")
        yield "Bash: 2 calls, 1 failure, 1 refused (at the prompt), 0 denied by a hook, 2 asked", (t["Bash"]["calls"], t["Bash"]["failures"], t["Bash"]["refused"], t["Bash"]["rejected_at_prompt"], t["Bash"]["denied_by_hook"], t["Bash"]["refused_other"], t["Bash"]["asked_permission"]) == (2, 1, 1, 1, 0, 0, 2)
        yield "Glob: 2 refused from elsewhere (an unnamed source, and none)", (t["Glob"]["calls"], t["Glob"]["refused"], t["Glob"]["refused_other"], t["Glob"]["rejected_at_prompt"], t["Glob"]["denied_by_hook"], t["Glob"]["denied_by_rule"]) == (2, 2, 2, 0, 0, 0)
        yield "each tool's refusal kinds add up to its refused calls", all(r["rejected_at_prompt"] + r["denied_by_hook"] + r["denied_by_rule"] + r["refused_other"] == r["refused"] for r in rows)
        yield "WebFetch: 1 denied by a hook, not rejected at a prompt; no ADK tool", (t["WebFetch"]["denied_by_hook"], t["WebFetch"]["rejected_at_prompt"]) == (1, 0) and "search" not in t
        yield "Write: 1 denied by a rule", (t["Write"]["denied_by_rule"], t["Write"]["rejected_at_prompt"], t["Write"]["denied_by_hook"]) == (1, 0, 0)
    elif name == "5":
        t = by(rows, "invocation_id")
        yield "I1: the prompt, 2 model requests, 2 tool calls, 195 tokens", (t["I1"]["prompt"], t["I1"]["model_requests"], t["I1"]["tool_calls"], t["I1"]["turn_tokens"]) == ("list the files", 2, 2, 195)
        yield "I2: 1 subagent, and its 7 error rows", t["I2"]["subagents"] == 1 and t["I2"]["error_rows"] == 7
    elif name == "6":
        yield "trace-1: 10 rows, each once", len(rows) == 10
        bash = [r for r in rows if r["event_type"] == "TOOL_COMPLETED" and r["detail"] == "Bash"]
        yield "the Bash call hangs under its model request", len(bash) == 1 and bash[0]["parent_event_type"] == "LLM_REQUEST"
    elif name == "7":
        a = by(rows, "subagent_type")
        yield "general-purpose: 1 run, 2.0 s, 50 tokens", set(a) == {"general-purpose"} and (a["general-purpose"]["runs"], a["general-purpose"]["avg_seconds"], a["general-purpose"]["total_tokens"]) == (1, 2.0, 50)
    elif name == "8":
        d = by(rows, "day")
        yield "day 1: 1 session, 11 rows (the replay once), 1 error", (d["2026-10-19"]["sessions"], d["2026-10-19"]["rows_written"], d["2026-10-19"]["error_rows"]) == (1, 11, 1)
        yield "day 2: 2 sessions, 15 rows, 7 errors", (d["2026-10-20"]["sessions"], d["2026-10-20"]["rows_written"], d["2026-10-20"]["error_rows"]) == (2, 15, 7)


def queries(path: Path) -> dict[str, str]:
    """Each query of a file, by its number (`-- 4.` or `-- V4.`)."""
    found, name, lines = {}, None, []
    for line in path.read_text().splitlines():
        m = re.match(r"^-- (V?\d+)\. ", line)
        if m:
            if name:
                found[name] = "\n".join(lines).strip()
            name, lines = m.group(1), []
        if name:
            lines.append(line)
    if name:
        found[name] = "\n".join(lines).strip()
    return found


def literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    text = value if isinstance(value, str) else json.dumps(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main() -> int:
    from google.cloud import bigquery

    project = os.environ.get("PROJECT")
    if not project:
        sys.exit("set PROJECT")
    if not RUNNER.is_file():
        sys.exit("set RUNNER to the analysis skill's scripts/run_bq.py, from a checkout of "
                 "haiyuan-eng-google/bigquery_agent_analytics_skill")
    dataset = f"bqaa_query_check_{secrets.token_hex(4)}"
    client = bigquery.Client(project=project)
    made = bigquery.Dataset(f"{project}.{dataset}")
    made.location = "US"
    client.create_dataset(made)
    passed = failed = 0
    try:
        # The CREATE statement alone (its descriptions hold semicolons).
        ddl = re.search(r"CREATE TABLE .*?CLUSTER BY [^;]*;", (MOD / "sql" / "agent_events_ddl.sql").read_text(), re.S).group(0)
        ddl = ddl.replace("{PROJECT}", project).replace("{DATASET}", dataset).replace("{TABLE}", "agent_events")
        client.query(ddl).result()
        values = ",\n".join(
            "(TIMESTAMP {ts}, {eid}, {et}, {agent}, {sid}, {inv}, {uid}, {trace}, {span}, {parent}, "
            "PARSE_JSON({content}), PARSE_JSON({attrs}), PARSE_JSON({lat}), {status}, {trunc})".format(
                ts=literal(r["timestamp"].replace("T", " ").replace("Z", " UTC")), eid=literal(r["event_id"]), et=literal(r["event_type"]),
                agent=literal(r["agent"]), sid=literal(r["session_id"]), inv=literal(r["invocation_id"]),
                uid=literal(r["user_id"]), trace=literal(r["trace_id"]), span=literal(r["span_id"]),
                parent=literal(r["parent_span_id"]), content=literal(r["content"]), attrs=literal(r["attributes"]),
                lat=literal(r["latency_ms"] if r["latency_ms"] is not None else {}), status=literal(r["status"]),
                trunc=literal(r["is_truncated"]))
            for r in FIXTURE)
        client.query(
            f"INSERT INTO `{project}.{dataset}.agent_events` (timestamp, event_id, event_type, agent, session_id, "
            "invocation_id, user_id, trace_id, span_id, parent_span_id, content, attributes, latency_ms, status, "
            f"is_truncated) VALUES\n{values}").result()
        client.query(f"CREATE TABLE `{project}.{dataset}.team_roster` AS "
                     f"SELECT user_id FROM UNNEST({json.dumps(ROSTER)}) AS user_id").result()
        print(f"# fixture: {len(FIXTURE)} rows in {project}.{dataset}.agent_events")
        runs = []
        for path in (MOD / "sql" / "value_queries.sql", MOD / "sql" / "sample_queries.sql"):
            for name, sql in queries(path).items():
                if name == "V4":
                    sql = re.sub(r"-- rates: begin.*?-- rates: end", RATES, sql, flags=re.S)
                runs.append((f"{path.name} {name}", name, sql, START, END))
                if name == "V7":
                    runs.append((f"{path.name} V7 (from just after a call's request)", "V7 clipped", sql, "2026-10-20T15:00:01Z", END))
                    runs.append((f"{path.name} V7 (a window of later finals)", "V7 later finals only", sql,
                                 "2026-10-20T19:00:05Z", "2026-10-20T19:00:08Z"))
                if name == "V2":
                    for case, start, end in (("V2 ends mid-turn", "2026-10-22T20:00:00Z", "2026-10-22T20:00:10Z"),
                                             ("V2 after the turn", "2026-10-22T20:00:00Z", "2026-10-22T20:00:30Z"),
                                             ("V2 starts mid-turn", "2026-10-22T20:00:15Z", "2026-10-22T20:00:30Z"),
                                             ("V2 past the lookback", "2026-10-24T11:00:00Z", "2026-10-24T11:00:10Z"),
                                             ("V2 whole long turn", "2026-10-24T09:00:00Z", "2026-10-24T11:00:10Z")):
                        runs.append((f"{path.name} {case}", case, sql, start, end))
                if name == "V3":
                    runs.append((f"{path.name} V3 (through a day with no rows)", "V3 silent day", sql, START, "2026-10-21"))
                    runs.append((f"{path.name} V3 (a window with no rows)", "V3 empty window", sql, "2026-09-01", "2026-09-02"))
        for label, name, sql, start, end in runs:
            base = [sys.executable, str(RUNNER), "--project", project, "--dataset", dataset,
                    "--table", "agent_events", "--start", start, "--end", end]
            if name == "6":
                base += ["--trace-id", "trace-1"]
            dry = subprocess.run(base + ["--dry-run", sql], capture_output=True, text=True)
            run = subprocess.run(base + [sql], capture_output=True, text=True)
            if run.returncode != 0 or dry.returncode != 0:
                failed += 1
                bad, step = (dry, "dry run") if dry.returncode != 0 else (run, "run")
                # The runner's own ERROR line, past any warnings.
                errors = [line for line in bad.stderr.splitlines() if line.startswith("ERROR")] or [bad.stderr.strip()]
                print(f"not ok - {label} {step} fails: {errors[-1][:300]}")
                continue
            scanned = json.loads(dry.stdout)["total_bytes_processed"]
            rows = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
            print(f"# {label}: dry run {scanned} bytes, {len(rows)} rows")
            results = checks(name, rows)
            while True:
                try:
                    what, ok = next(results)
                except StopIteration:
                    break
                except Exception as error:  # a column or row the query lacks
                    failed += 1
                    print(f"not ok - {label}: the answer lacks {error!r}")
                    break
                if ok:
                    passed += 1
                    print(f"ok - {label}: {what}")
                else:
                    failed += 1
                    print(f"not ok - {label}: {what}")
    finally:
        client.delete_dataset(f"{project}.{dataset}", delete_contents=True, not_found_ok=True)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
