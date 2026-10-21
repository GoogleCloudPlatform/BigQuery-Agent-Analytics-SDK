// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// The keys whose values a row keeps, in one place. In a row's `content` and
// `attributes`, at any depth, and in JSON found in their text, the value
// under any other key is replaced by "[REDACTED]", whatever it holds (a
// string, a number, a list or an object; null stays null). Keys are compared
// by their words, so `inputTokens`, `INPUT_TOKENS` and `input-tokens` are all
// `input_tokens`; `api_keys` and `token2` are simply not on the list.
//
// A kept value is still checked: every string in it goes through the
// free-text credential rules, and text in it that looks like JSON is parsed
// and held to this same list, or dropped whole when it cannot be parsed
// (sanitize.ts).

// Structure and telemetry: the fields this mod writes (event names, ids,
// token counts, timings, model and tool names, flags, session and writer
// metadata) and the number and flag arguments of the host CLI's built-in
// tools.
export const TELEMETRY_KEYS: readonly string[] = [
  // Row fields and ids.
  'event_type',
  'timestamp',
  'status',
  'event_id',
  'invocation_id',
  'trace_id',
  'span_id',
  'parent_span_id',
  'agent',
  'agent_id',
  'agent_type',
  'root_agent_name',
  'subagent_type',
  'subagent_turn_id',
  'source_turn_id',
  'tool_use_id',
  // Tools and models.
  'tool',
  'tool_name',
  'tool_origin',
  'tool_uses',
  'server_tool_uses',
  'model',
  'model_version',
  'llm_config',
  'effort',
  // Token counts.
  'usage_metadata',
  'prompt_tokens',
  'completion_tokens',
  'total_tokens',
  'input_tokens',
  'output_tokens',
  'cache_read_input_tokens',
  'cache_creation_input_tokens',
  'max_tokens',
  // Timings and counts.
  'latency_ms',
  'total_ms',
  'time_to_first_token_ms',
  'duration_ms',
  'step_index',
  'step_count',
  'message_count',
  'turn_number',
  'trace_count',
  // States and flags.
  'session_start',
  'session_end',
  'state_delta',
  'source',
  'start_source',
  'permission_mode',
  'reason',
  'finish_reason',
  'is_aborted',
  'is_error',
  'is_read_only',
  'denied',
  'inferred',
  'permission_requested',
  'permission_decision',
  'permission_decision_source',
  'submitted_during_turn',
  'wait',
  'prompt_origin',
  'attachments',
  'role',
  'type',
  // Session and writer metadata.
  'session_metadata',
  'project_name',
  'cwd',
  'host_version',
  'host_name',
  'writer',
  'plugin',
  'version',
  'label',
  'mode',
  // Built-in tools' number and flag arguments.
  'limit',
  'offset',
  'timeout',
  'run_in_background',
  'replace_all',
  'head_limit',
  'multiline',
  'output_mode',
]

// Free text: the prompt, the model's answer, a tool's arguments and result,
// error text, and the text arguments of the host CLI's built-in tools. Kept
// rather than redacted by key, and scanned whole by the free-text rules.
export const TEXT_KEYS: readonly string[] = [
  // The row's own payload fields.
  'text_summary',
  'prompt',
  'content',
  'response',
  'result',
  'args',
  // Text inside a tool's result.
  'text',
  'message',
  'error',
  'error_message',
  'stdout',
  'stderr',
  'output',
  // Built-in tools' text arguments.
  'command',
  'description',
  'file_path',
  'path',
  'notebook_path',
  'pattern',
  'glob',
  'query',
  'url',
  'old_string',
  'new_string',
  'new_source',
  'plan',
  'todos',
  'edits',
  'active_form',
]

// A name's words, lower case: split on punctuation and camelCase
// (`db_password`, `X-Api-Key`, `sessionCookie`, `MY_PWD`).
export const nameParts = (name: string): string[] =>
  name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(part => part !== '')

const LISTED = new Set([...TELEMETRY_KEYS, ...TEXT_KEYS].map(key => nameParts(key).join('_')))

export const isListedKey = (name: string): boolean => LISTED.has(nameParts(name).join('_'))
