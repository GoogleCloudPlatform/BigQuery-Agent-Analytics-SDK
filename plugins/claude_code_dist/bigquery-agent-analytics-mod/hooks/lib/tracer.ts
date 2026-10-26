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

// Maps the host CLI's events to BQAA rows. Holds the trace and span structure:
// one trace per main-loop turn, LLM and tool spans under it, a subagent's
// spans under its own agent span. Pure: time, ids and the sink are injected,
// and nothing here waits on anything.

import { type Config, PLUGIN_NAME, PLUGIN_VERSION, WRITER_LABEL } from './config.ts'
import type { IdSource } from './ids.ts'
import { type BqaaRow, buildRow } from './rows.ts'

export type TracerDeps = {
  now: () => number
  ids: IdSource
  config: () => Config
  emit: (row: BqaaRow, redactions: number) => void
  // A timer, once the host's clock is at hand (null before): how a finished
  // tool call waits briefly for its permission decision.
  after?: (ms: number, fn: () => void) => { cancel: () => void } | null
}

// How the permission check answered a tool call, from the host's
// `tool_decision` record: `accept` or `reject`, and where the answer came
// from (`config`, `hook`, `user_temporary`, `user_permanent`, `user_abort`,
// `user_reject`).
export type ToolDecision = { decision: string; source: string }

// A rejection's record can come just after its call returned (an accept's
// comes while the call runs): how long a finished call waits for its own.
export const DECISION_WAIT_MS = 2000
const DECISION_VALUE = /^[a-z][a-z0-9_]{0,31}$/

// A finished call waiting for its decision, and its timer.
type AwaitingDecision = { write: (decision: ToolDecision | undefined) => void; timer: { cancel: () => void } | null }

// The token counts the engine reports (`ModelUsage` plus the model's id).
export type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
  model?: string
}

type TurnCtx = {
  turnId: string
  traceId: string
  spanId: string
  startedAt: number
  prompt: string
  lastStepSpan: string | null
  steps: number
}

// One run of a subagent's (or teammate's) loop: its span under the main turn
// it runs in.
type LoopCtx = {
  agentId: string
  agentType: string
  // Named by classic.SubagentStart, not guessed.
  typeKnown: boolean
  traceId: string
  invocationId: string | null
  spanId: string
  parentSpanId: string | null
  lastStepSpan: string | null
  // Its AGENT_STARTING row is written; its run's turn.complete was seen.
  started: boolean
  ended: boolean
}

export type StepCtx = {
  spanId: string
  parentSpanId: string | null
  traceId: string | null
  invocationId: string | null
  agent: string
  agentId?: string
  index: number
  model: string
  startedAt: number
}

export type ToolCtx = {
  spanId: string
  parentSpanId: string | null
  traceId: string | null
  invocationId: string | null
  agent: string
  agentId?: string
  tool: string
  toolUseId: string | null
  origin: string
  args: Record<string, unknown>
  fingerprint: string
  startedAt: number
  permissionRequested: boolean
}

export type StepResult = {
  answer: string
  toolUses: readonly { name: string }[]
  serverToolUses?: readonly { name: string }[]
  stopReason: string | null
  usage: Usage | null
}

export type ToolOutcome =
  | { deny: string }
  | { isError: true; text?: string; result?: unknown }
  | { result?: unknown; text?: string; isReadOnly?: true; isError?: undefined }

export type TurnEnd = {
  turnId: string
  agentId?: string
  answer: string
  durationMs: number
  isAborted: boolean
  reason: string
  refusal?: { category: string | null; explanation: string | null }
  usage?: Usage
}

const RESERVED_TOOL_KEYS = new Set(['tool', 'tool_use_id', 'agentId', 'requestMeta', 'consent'])

// Payload objects list their small identifying fields first: the copy is
// built within one character budget, so a long `args`, `result` or
// `response` is what gets cut, never `tool` or `tool_origin`.
const MAX_TRACKED = 500

export const toolOrigin = (tool: string): string => {
  if (tool.startsWith('mcp__')) return 'MCP'
  if (tool === 'Agent' || tool === 'Task') return 'SUB_AGENT'
  return 'LOCAL'
}

export function usageMetadata(usage: Usage | null | undefined): Record<string, number> | null {
  if (!usage) return null
  const input = usage.input_tokens || 0
  const cacheRead = usage.cache_read_input_tokens || 0
  const cacheWrite = usage.cache_creation_input_tokens || 0
  const output = usage.output_tokens || 0
  const prompt = input + cacheRead + cacheWrite
  return {
    // The names this repo's tracing plugin and analysis skill use.
    prompt_tokens: prompt,
    completion_tokens: output,
    total_tokens: prompt + output,
    // As the API reports them.
    input_tokens: input,
    output_tokens: output,
    cache_read_input_tokens: cacheRead,
    cache_creation_input_tokens: cacheWrite,
  }
}

const baseName = (path: string): string => {
  const trimmed = path.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return cut >= 0 ? trimmed.slice(cut + 1) : trimmed
}

const fingerprintOf = (tool: string, args: unknown): string => {
  let json = ''
  try {
    json = JSON.stringify(args) ?? ''
  } catch {
    json = ''
  }
  return `${tool}:${json.slice(0, 2000)}`
}

const remember = <K, V>(map: Map<K, V>, key: K, value: V): void => {
  map.set(key, value)
  if (map.size > MAX_TRACKED) {
    const oldest = map.keys().next()
    if (!oldest.done) map.delete(oldest.value)
  }
}

export class Tracer {
  private deps: TracerDeps
  sessionId: string | null = null
  private cwd: string | null = null
  private engineVersion: string | null = null
  private sessionStartedAt: number | null = null
  private turnCount = 0
  private main: TurnCtx | null = null
  // Turns seen only through their model requests (this module loaded after
  // their turn.start), kept apart so they never replace the current turn.
  private others = new Map<string, TurnCtx>()
  private loops = new Map<string, LoopCtx>()
  private inflightTools = new Set<ToolCtx>()
  // With recordHostname: the machine's name, once looked up.
  private hostName: string | null = null
  // Permission decisions by tool_use_id that came before their call ended,
  // and calls that ended before theirs came. Once any collector record has
  // come, the host sends decisions too, so a call that ends without its
  // decision waits for it.
  private decisions = new Map<string, ToolDecision>()
  private awaitingDecision = new Map<string, AwaitingDecision>()
  private recordsArrive = false

  constructor(deps: TracerDeps) {
    this.deps = deps
  }

  setSession(info: { sessionId?: string | null; cwd?: string | null; engineVersion?: string | null; hostName?: string | null }): void {
    if (info.sessionId) this.sessionId = info.sessionId
    if (info.cwd) this.cwd = info.cwd
    if (info.engineVersion) this.engineVersion = info.engineVersion
    if (info.hostName) this.hostName = info.hostName
    if (this.sessionStartedAt === null) this.sessionStartedAt = this.deps.now()
  }

  // classic.SessionStart (startup, resume, clear, compact or fork), at
  // `atMs`, the time the event came: its row can be written later, once the
  // configuration is loaded.
  sessionStarted(e: { session_id?: string; source: string; model?: string; permission_mode?: string; cwd?: string }, atMs?: number): void {
    if (e.session_id) this.sessionId = e.session_id
    if (e.cwd) this.cwd = e.cwd
    const at = atMs ?? this.deps.now()
    if (e.source !== 'compact') {
      this.sessionStartedAt = at
      this.turnCount = 0
    }
    this.emit({
      eventType: 'STATE_DELTA',
      timestampMs: at,
      ids: this.sessionIds(),
      content: { session_start: true },
      attributes: {
        state_delta: {
          source: 'coding_agent_session_start',
          start_source: e.source,
          model: e.model ?? null,
          permission_mode: e.permission_mode ?? null,
        },
        session_metadata: this.sessionMetadata(),
      },
    })
  }

  sessionEnded(e: { reason: string; sessionId?: string }): void {
    if (e.sessionId) this.sessionId = e.sessionId
    // Calls still waiting for their decision are written first, without it.
    this.writeAwaiting()
    const now = this.deps.now()
    this.emit({
      eventType: 'STATE_DELTA',
      ids: this.sessionIds(),
      content: { session_end: true },
      attributes: {
        state_delta: {
          source: 'coding_agent_session_end',
          reason: e.reason,
          trace_count: this.turnCount,
          duration_ms: this.sessionStartedAt === null ? null : Math.max(0, now - this.sessionStartedAt),
        },
      },
    })
    this.main = null
    this.others.clear()
    this.loops.clear()
    this.inflightTools.clear()
    this.decisions.clear()
    // After /clear, /resume or /branch the process goes on under another id:
    // until it is read, rows carry none rather than the old one.
    if (e.reason === 'clear' || e.reason === 'resume') this.sessionId = null
  }

  // prompt.submit, once the prompt entered (after `next` resolved).
  promptSubmitted(e: {
    text: string
    submittedAt: number
    originKind: string
    duringTurnId?: string
    wait?: boolean
    attachments?: readonly { type: string }[]
  }): void {
    const turn = this.main && (e.duringTurnId === undefined || this.main.turnId === e.duringTurnId) ? this.main : null
    this.emit({
      eventType: 'USER_MESSAGE_RECEIVED',
      timestampMs: e.submittedAt,
      ids: {
        traceId: turn?.traceId ?? null,
        spanId: turn?.spanId ?? this.deps.ids.spanId(),
        parentSpanId: null,
        invocationId: turn?.turnId ?? e.duringTurnId ?? null,
      },
      content: { text_summary: e.text },
      attributes: {
        prompt_origin: e.originKind,
        submitted_during_turn: e.duringTurnId !== undefined,
        wait: e.wait ?? false,
        attachments: e.attachments && e.attachments.length > 0 ? e.attachments.map(a => a.type) : undefined,
      },
    })
  }

  turnStarted(e: { turnId: string; text: string; agentId?: string }): void {
    // A subagent's or teammate's run (`agentId`, its loop): a turn of that
    // loop inside the main turn, which it neither replaces nor counts as a
    // turn of its own. No INVOCATION_STARTING.
    if (e.agentId) {
      this.loopRunStarted(e.agentId, e.turnId)
      return
    }
    this.main = {
      turnId: e.turnId,
      traceId: this.deps.ids.traceId(),
      spanId: this.deps.ids.spanId(),
      startedAt: this.deps.now(),
      prompt: e.text,
      lastStepSpan: null,
      steps: 0,
    }
    this.turnCount += 1
    this.emit({
      eventType: 'INVOCATION_STARTING',
      ids: { traceId: this.main.traceId, spanId: this.main.spanId, parentSpanId: null, invocationId: e.turnId },
      content: {},
      attributes: { turn_number: this.turnCount, session_metadata: this.sessionMetadata() },
    })
  }

  stepStarted(e: { turnId: string; index: number; model: string; effort?: string | number; messageCount: number; agentId?: string }): StepCtx {
    const spanId = this.deps.ids.spanId()
    const startedAt = this.deps.now()
    let ctx: StepCtx
    let prompt: { role: string; content: string }[] = []
    if (e.agentId) {
      const loop = this.loopFor(e.agentId, e.turnId)
      loop.lastStepSpan = spanId
      ctx = {
        spanId,
        parentSpanId: loop.spanId,
        traceId: loop.traceId,
        invocationId: loop.invocationId ?? e.turnId,
        agent: loop.agentType,
        agentId: e.agentId,
        index: e.index,
        model: e.model,
        startedAt,
      }
    } else {
      const turn = this.turnFor(e.turnId)
      turn.lastStepSpan = spanId
      turn.steps += 1
      if (e.index === 0 && turn.prompt !== '') prompt = [{ role: 'user', content: turn.prompt }]
      ctx = {
        spanId,
        parentSpanId: turn.spanId,
        traceId: turn.traceId,
        invocationId: turn.turnId,
        agent: this.deps.config().agentName,
        index: e.index,
        model: e.model,
        startedAt,
      }
    }
    this.emit({
      eventType: 'LLM_REQUEST',
      agent: ctx.agent,
      ids: ctx,
      content: { prompt },
      attributes: {
        model: e.model,
        llm_config: e.effort === undefined ? undefined : { effort: e.effort },
        step_index: e.index,
        message_count: e.messageCount,
        agent_id: e.agentId,
      },
    })
    return ctx
  }

  stepFinished(ctx: StepCtx, result: StepResult, firstContentAt?: number): void {
    const now = this.deps.now()
    const latency = {
      total_ms: now - ctx.startedAt,
      time_to_first_token_ms: firstContentAt === undefined ? undefined : firstContentAt - ctx.startedAt,
    }
    const noResponse = result.stopReason === null && result.usage === null && result.answer === '' && result.toolUses.length === 0
    if (noResponse) {
      this.emit({
        eventType: 'LLM_ERROR',
        agent: ctx.agent,
        ids: ctx,
        content: null,
        attributes: { model: ctx.model, step_index: ctx.index, agent_id: ctx.agentId },
        latencyMs: { total_ms: latency.total_ms },
        status: 'ERROR',
        errorMessage: 'no response: the request failed or was interrupted before a message arrived',
      })
      return
    }
    let note: string | null = null
    if (result.stopReason === 'refusal') note = 'the model refused (stop_reason refusal)'
    if (result.stopReason === 'model_context_window_exceeded') note = 'the model context window was exceeded'
    this.emit({
      eventType: 'LLM_RESPONSE',
      agent: ctx.agent,
      ids: ctx,
      content: {
        tool_uses: result.toolUses.length > 0 ? result.toolUses.map(t => t.name) : undefined,
        response: result.answer,
      },
      attributes: {
        model: result.usage?.model ?? ctx.model,
        model_version: result.usage?.model,
        usage_metadata: usageMetadata(result.usage) ?? undefined,
        finish_reason: result.stopReason,
        step_index: ctx.index,
        server_tool_uses: result.serverToolUses && result.serverToolUses.length > 0 ? result.serverToolUses.map(t => t.name) : undefined,
        agent_id: ctx.agentId,
      },
      latencyMs: latency,
      errorMessage: note,
    })
  }

  stepFailed(ctx: StepCtx, error: unknown): void {
    this.emit({
      eventType: 'LLM_ERROR',
      agent: ctx.agent,
      ids: ctx,
      content: null,
      attributes: { model: ctx.model, step_index: ctx.index, agent_id: ctx.agentId },
      latencyMs: { total_ms: this.deps.now() - ctx.startedAt },
      status: 'ERROR',
      errorMessage: errorText(error),
    })
  }

  toolStarted(e: Record<string, unknown>): ToolCtx {
    const tool = String(e.tool ?? 'unknown')
    const agentId = typeof e.agentId === 'string' && e.agentId !== '' ? e.agentId : undefined
    const args: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(e)) if (!RESERVED_TOOL_KEYS.has(key)) args[key] = value
    let parentSpanId: string | null = null
    let traceId: string | null = null
    let invocationId: string | null = null
    let agent = this.deps.config().agentName
    if (agentId) {
      const loop = this.loopFor(agentId, null)
      parentSpanId = loop.lastStepSpan ?? loop.spanId
      traceId = loop.traceId
      invocationId = loop.invocationId
      agent = loop.agentType
    } else if (this.main) {
      parentSpanId = this.main.lastStepSpan ?? this.main.spanId
      traceId = this.main.traceId
      invocationId = this.main.turnId
    }
    const ctx: ToolCtx = {
      spanId: this.deps.ids.spanId(),
      parentSpanId,
      traceId,
      invocationId,
      agent,
      agentId,
      tool,
      toolUseId: typeof e.tool_use_id === 'string' ? e.tool_use_id : null,
      origin: toolOrigin(tool),
      args,
      fingerprint: fingerprintOf(tool, args),
      startedAt: this.deps.now(),
      permissionRequested: false,
    }
    this.inflightTools.add(ctx)
    if (this.inflightTools.size > MAX_TRACKED) {
      const oldest = this.inflightTools.values().next()
      if (!oldest.done) this.inflightTools.delete(oldest.value)
    }
    this.emit({
      eventType: 'TOOL_STARTING',
      agent,
      ids: ctx,
      content: { tool, tool_origin: ctx.origin, args },
      attributes: { tool_use_id: ctx.toolUseId, agent_id: agentId },
    })
    return ctx
  }

  // A tool call ended: its TOOL_COMPLETED row, with the permission decision
  // when it is known. Once decisions arrive, a call whose decision has not
  // come yet waits up to DECISION_WAIT_MS for it, keeping the time it ended.
  toolFinished(ctx: ToolCtx, outcome: ToolOutcome): void {
    this.inflightTools.delete(ctx)
    const endedAt = this.deps.now()
    const id = ctx.toolUseId
    const known = id === null ? undefined : this.decisions.get(id)
    if (id !== null) this.decisions.delete(id)
    const timer = known === undefined && id !== null && this.recordsArrive ? this.deps.after : undefined
    if (id === null || timer === undefined) {
      this.toolCompleted(ctx, outcome, endedAt, known)
      return
    }
    const waiting: AwaitingDecision = { write: decision => this.toolCompleted(ctx, outcome, endedAt, decision), timer: null }
    this.awaitingDecision.set(id, waiting)
    // Past the bound, the oldest waiting call is written without its decision.
    if (this.awaitingDecision.size > MAX_TRACKED) {
      const [oldestId, oldest] = this.awaitingDecision.entries().next().value as [string, AwaitingDecision]
      this.awaitingDecision.delete(oldestId)
      oldest.timer?.cancel()
      oldest.write(undefined)
    }
    waiting.timer = timer(DECISION_WAIT_MS, () => {
      if (this.awaitingDecision.get(id) !== waiting) return
      this.awaitingDecision.delete(id)
      waiting.write(undefined)
    })
    // No clock yet: written now.
    if (waiting.timer === null && this.awaitingDecision.get(id) === waiting) {
      this.awaitingDecision.delete(id)
      waiting.write(undefined)
    }
  }

  // A collector record of the host's. Only `tool_decision` is used: matched
  // to its call by tool_use_id, its values kept when they are short
  // lowercase tokens. Any record shows that decisions will come.
  recordSeen(event: string, attributes: Readonly<Record<string, unknown>>): void {
    this.recordsArrive = true
    if (event !== 'tool_decision') return
    const id = attributes.tool_use_id
    const decision = attributes.decision
    const source = attributes.source
    if (typeof id !== 'string' || id === '' || typeof decision !== 'string' || !DECISION_VALUE.test(decision)) return
    const answer: ToolDecision = { decision, source: typeof source === 'string' && DECISION_VALUE.test(source) ? source : 'unknown' }
    const waiting = this.awaitingDecision.get(id)
    if (waiting) {
      this.awaitingDecision.delete(id)
      waiting.timer?.cancel()
      waiting.write(answer)
      return
    }
    remember(this.decisions, id, answer)
  }

  private writeAwaiting(): void {
    const all = [...this.awaitingDecision.values()]
    this.awaitingDecision.clear()
    for (const waiting of all) {
      waiting.timer?.cancel()
      waiting.write(undefined)
    }
  }

  private toolCompleted(ctx: ToolCtx, outcome: ToolOutcome, endedAt: number, decision: ToolDecision | undefined): void {
    const latencyMs = { total_ms: endedAt - ctx.startedAt }
    const attributes: Record<string, unknown> = {
      tool_use_id: ctx.toolUseId,
      agent_id: ctx.agentId,
      permission_requested: ctx.permissionRequested || undefined,
      permission_decision: decision?.decision,
      permission_decision_source: decision?.source,
    }
    if ('deny' in outcome && typeof outcome.deny === 'string') {
      this.emit({
        eventType: 'TOOL_COMPLETED',
        agent: ctx.agent,
        timestampMs: endedAt,
        ids: ctx,
        content: { tool: ctx.tool, tool_origin: ctx.origin, result: null },
        attributes: { ...attributes, denied: true },
        latencyMs,
        status: 'ERROR',
        errorMessage: outcome.deny,
      })
      return
    }
    if ('isError' in outcome && outcome.isError === true) {
      this.emit({
        eventType: 'TOOL_COMPLETED',
        agent: ctx.agent,
        timestampMs: endedAt,
        ids: ctx,
        content: { tool: ctx.tool, tool_origin: ctx.origin, result: null },
        attributes,
        latencyMs,
        status: 'ERROR',
        errorMessage: outcome.text ?? errorText(outcome.result),
      })
      return
    }
    const ok = outcome as { result?: unknown; text?: string; isReadOnly?: true }
    this.emit({
      eventType: 'TOOL_COMPLETED',
      agent: ctx.agent,
      timestampMs: endedAt,
      ids: ctx,
      content: { tool: ctx.tool, tool_origin: ctx.origin, result: ok.text ?? ok.result ?? null },
      attributes: { ...attributes, is_read_only: ok.isReadOnly === true ? true : undefined },
      latencyMs,
    })
  }

  toolFailed(ctx: ToolCtx, error: unknown): void {
    this.toolFinished(ctx, { isError: true, text: errorText(error) })
  }

  // classic.PermissionRequest: marks the in-flight call that is waiting on
  // the person, so its latency can be read as including that wait.
  permissionRequested(e: { tool_name?: string; tool_input?: unknown; agent_id?: string }): void {
    const tool = String(e.tool_name ?? '')
    const fingerprint = fingerprintOf(tool, e.tool_input)
    let fallback: ToolCtx | null = null
    for (const ctx of this.inflightTools) {
      if (ctx.tool !== tool || ctx.permissionRequested) continue
      if ((ctx.agentId ?? undefined) !== (e.agent_id || undefined)) continue
      if (ctx.fingerprint === fingerprint) {
        ctx.permissionRequested = true
        return
      }
      fallback ??= ctx
    }
    if (fallback) fallback.permissionRequested = true
  }

  // classic.SubagentStart: names the subagent's type. Its run usually began
  // a moment before (turn.start with its loop id), and its AGENT_STARTING
  // row waited for this; otherwise this starts the run's span.
  subagentStarted(e: { agent_id: string; agent_type: string }): void {
    const known = this.loops.get(e.agent_id)
    let loop = known
    if (!loop || loop.ended) {
      loop = this.newLoop(e.agent_id, null, known)
      remember(this.loops, e.agent_id, loop)
    }
    loop.agentType = e.agent_type || loop.agentType
    loop.typeKnown = true
    if (!loop.started) this.emitAgentStarting(loop, false)
  }

  turnCompleted(e: TurnEnd): void {
    const isFailure = e.reason === 'error' || e.reason === 'refusal'
    const refusal = e.refusal ? ['model refused', e.refusal.category, e.refusal.explanation].filter(Boolean).join(': ') : 'model refused'
    const errorMessage = !isFailure ? null : e.reason === 'refusal' ? refusal : 'the turn ended on an API error'
    const usage = usageMetadata(e.usage) ?? undefined
    if (e.agentId) {
      const loop = this.loopFor(e.agentId, e.turnId)
      this.emit({
        eventType: isFailure ? 'AGENT_ERROR' : 'AGENT_COMPLETED',
        agent: loop.agentType,
        ids: { traceId: loop.traceId, spanId: loop.spanId, parentSpanId: loop.parentSpanId, invocationId: loop.invocationId ?? e.turnId },
        content: {},
        attributes: { agent_id: e.agentId, finish_reason: e.reason, is_aborted: e.isAborted, usage_metadata: usage, model: e.usage?.model, subagent_turn_id: e.turnId },
        latencyMs: { total_ms: e.durationMs },
        status: isFailure ? 'ERROR' : 'OK',
        errorMessage,
      })
      // The run is over: a later run of the loop gets a span of its own,
      // under the main turn it runs in.
      loop.ended = true
      return
    }
    const turn = this.turnFor(e.turnId)
    const ids = { traceId: turn.traceId, spanId: turn.spanId, parentSpanId: null, invocationId: turn.turnId }
    if (e.answer !== '') {
      this.emit({
        eventType: 'AGENT_RESPONSE',
        ids,
        content: { response: e.answer },
        attributes: { source_turn_id: e.turnId },
      })
    }
    this.emit({
      eventType: isFailure ? 'INVOCATION_ERROR' : 'INVOCATION_COMPLETED',
      ids,
      content: isFailure ? { reason: e.reason } : {},
      attributes: {
        finish_reason: e.reason,
        is_aborted: e.isAborted,
        step_count: turn.steps,
        usage_metadata: usage,
        model: e.usage?.model,
      },
      latencyMs: { total_ms: e.durationMs },
      status: isFailure ? 'ERROR' : 'OK',
      errorMessage,
    })
    if (this.main === turn) this.main = null
    this.others.delete(e.turnId)
  }

  // ---- internals -------------------------------------------------------

  private turnFor(turnId: string): TurnCtx {
    if (this.main && this.main.turnId === turnId) return this.main
    const known = this.others.get(turnId)
    if (known) return known
    // A turn whose turn.start this module did not see (it loaded mid-turn):
    // continue under a fresh trace, with no INVOCATION_STARTING row. It is
    // the current turn only when there is no other.
    const turn: TurnCtx = {
      turnId,
      traceId: this.deps.ids.traceId(),
      spanId: this.deps.ids.spanId(),
      startedAt: this.deps.now(),
      prompt: '',
      lastStepSpan: null,
      steps: 0,
    }
    if (this.main === null) this.main = turn
    else remember(this.others, turnId, turn)
    return turn
  }

  // The run a subagent's row belongs to. A run whose start this module did
  // not see begins here; one whose SubagentStart never came (a managed
  // policy can keep classic hooks from a person's plugin) gets its
  // AGENT_STARTING row here, inferred unless its type is known.
  private loopFor(agentId: string, turnId: string | null): LoopCtx {
    const known = this.loops.get(agentId)
    let loop = known
    if (!loop || loop.ended) {
      loop = this.newLoop(agentId, turnId, known)
      remember(this.loops, agentId, loop)
    }
    if (!loop.started) this.emitAgentStarting(loop, !loop.typeKnown)
    return loop
  }

  // turn.start with a loop id: a run of that loop begins under the current
  // main turn. Its AGENT_STARTING row waits for the classic SubagentStart
  // that names its type, or for the run's first row if that never comes.
  private loopRunStarted(agentId: string, turnId: string): void {
    const known = this.loops.get(agentId)
    if (known && !known.ended) return
    remember(this.loops, agentId, this.newLoop(agentId, turnId, known))
  }

  // A run's span, under the main turn now running: its trace, its
  // invocation and its span as parent. A later run of a loop keeps the type
  // an earlier one was named.
  private newLoop(agentId: string, turnId: string | null, previous: LoopCtx | undefined): LoopCtx {
    return {
      agentId,
      agentType: previous?.agentType ?? 'subagent',
      typeKnown: previous?.typeKnown ?? false,
      traceId: this.main?.traceId ?? this.deps.ids.traceId(),
      invocationId: this.main?.turnId ?? turnId,
      spanId: this.deps.ids.spanId(),
      parentSpanId: this.main?.spanId ?? null,
      lastStepSpan: null,
      started: false,
      ended: false,
    }
  }

  private emitAgentStarting(loop: LoopCtx, inferred: boolean): void {
    loop.started = true
    this.emit({
      eventType: 'AGENT_STARTING',
      agent: loop.agentType,
      ids: { traceId: loop.traceId, spanId: loop.spanId, parentSpanId: loop.parentSpanId, invocationId: loop.invocationId },
      content: { agent_type: loop.agentType, agent_id: loop.agentId },
      attributes: { agent_id: loop.agentId, inferred: inferred || undefined },
    })
  }

  private sessionIds() {
    return {
      traceId: this.main?.traceId ?? null,
      spanId: this.deps.ids.spanId(),
      parentSpanId: null,
      invocationId: this.main?.turnId ?? null,
    }
  }

  private sessionMetadata(): Record<string, unknown> {
    return {
      project_name: this.cwd ? baseName(this.cwd) : null,
      cwd: this.cwd,
      host_version: this.engineVersion,
      host_name: this.hostName ?? undefined,
    }
  }

  private emit(args: {
    eventType: string
    agent?: string
    timestampMs?: number
    ids: { traceId: string | null; spanId: string; parentSpanId: string | null; invocationId: string | null }
    content: unknown
    attributes: Record<string, unknown>
    latencyMs?: Record<string, number | undefined>
    status?: 'OK' | 'ERROR'
    errorMessage?: string | null
  }): void {
    const config = this.deps.config()
    if (!config.enabled) return
    const built = buildRow({
      eventType: args.eventType,
      timestampMs: args.timestampMs ?? this.deps.now(),
      eventId: this.deps.ids.eventId(),
      agent: args.agent ?? config.agentName,
      sessionId: this.sessionId,
      invocationId: args.ids.invocationId,
      userId: config.userId,
      traceId: args.ids.traceId,
      spanId: args.ids.spanId,
      parentSpanId: args.ids.parentSpanId,
      content: args.content,
      attributes: {
        root_agent_name: config.agentName,
        source: 'coding_agent',
        ...args.attributes,
        writer: {
          plugin: PLUGIN_NAME,
          version: PLUGIN_VERSION,
          label: WRITER_LABEL,
          agent: config.agentName,
          mode: config.dryRun ? 'dry_run' : 'insert_all',
        },
      },
      latencyMs: args.latencyMs,
      status: args.status,
      errorMessage: args.errorMessage,
      maxPayloadChars: config.maxPayloadChars,
    })
    this.deps.emit(built.row, built.redactions)
  }
}

export function errorText(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`
  if (typeof error === 'string') return error
  try {
    return JSON.stringify(error) ?? String(error)
  } catch {
    return String(error)
  }
}
