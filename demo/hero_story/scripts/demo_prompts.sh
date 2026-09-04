#!/usr/bin/env bash
# Single source of truth for the scripted demo prompts.
# sql/05_governance_privacy_dlq.sql searches for BOTH strings VERBATIM as
# the privacy proof — any change here flows to run_sessions.sh and
# run_queries.sh automatically because both source this file.
#
# The Claude prompt is deliberately non-trivial: the response must take
# long enough that in-session metric/span export intervals elapse
# (Claude's shutdown flush is unreliable in very short sessions).
export CLAUDE_PROMPT="Explain in about 150 words why unified telemetry matters for platform teams, covering adoption, cost attribution, and incident response."
export CODEX_PROMPT="Summarize in one sentence why unified telemetry matters for platform teams (codex)."
