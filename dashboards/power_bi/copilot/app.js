/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * BQAA Copilot preview: a prompt-driven dashboard over `agent_events`.
 *
 * Everything runs in the browser. The prompt parser is rule-based keyword
 * matching, not a language model. The dashboard reads either the built-in
 * synthetic sample or rows the user imports, and never connects to BigQuery:
 * the SQL dialog renders queries for the user to run themselves.
 */
(() => {
  'use strict';

  const DAY_MS = 86400000;

  // ---------------------------------------------------------------------------
  // Theme
  // ---------------------------------------------------------------------------
  const prefersDark = window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches;
  let currentTheme = prefersDark ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', currentTheme);

  // ---------------------------------------------------------------------------
  // Synthetic sample of agent_events rows
  // ---------------------------------------------------------------------------
  const CHART_COLORS = [
    'var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)',
    'var(--chart-4)', 'var(--chart-5)', 'var(--chart-6)',
  ];

  const SAMPLE_AGENTS = [
    'root_agent', 'sql_generator_agent', 'schema_retriever_agent',
    'chart_composer_agent', 'time_semantics_agent', 'eval_judge_agent',
  ];
  const SAMPLE_MODELS = ['llm-pro', 'llm-standard', 'llm-fast', 'llm-lite'];
  const SAMPLE_TOOLS = [
    'run_bigquery_sql', 'get_table_schema', 'dry_run_query',
    'search_glossary', 'render_chart', 'lookup_order',
  ];
  const SAMPLE_END_MS = Date.UTC(2026, 6, 16);  // 2026-07-16

  const MODEL_LATENCY_MS = {
    'llm-pro': 2300, 'llm-standard': 1300, 'llm-fast': 800, 'llm-lite': 600,
  };
  const TOOL_LATENCY_MS = {
    run_bigquery_sql: 1800, get_table_schema: 300, dry_run_query: 600,
    search_glossary: 450, render_chart: 250, lookup_order: 350,
  };
  const TOOL_ERRORS = [
    '403 Access Denied: caller lacks bigquery.tables.getData on the table',
    'DEADLINE_EXCEEDED: tool call exceeded the 30s timeout',
    'Syntax error: Unrecognized name: gross_margin_pct',
    'Not found: Table my-project:retail.returns was not found',
  ];
  const LLM_ERRORS = [
    '429 RESOURCE_EXHAUSTED: model quota exceeded',
    'DEADLINE_EXCEEDED: model response timed out',
  ];

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function() {
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function generateSampleEvents() {
    const rand = mulberry32(20260716);
    const pick = (list, skew) => list[Math.min(
        list.length - 1, Math.floor(Math.pow(rand(), skew) * list.length))];
    const hex = (n) => (n >>> 0).toString(16).padStart(8, '0');
    const rows = [];
    let seq = 0;

    for (let day = 0; day < 30; day++) {
      const dayStart = SAMPLE_END_MS - (29 - day) * DAY_MS;
      const weekday = new Date(dayStart).getUTCDay();
      const weekend = weekday === 0 || weekday === 6;
      const incident = day === 21 || day === 22;
      const sessions = Math.round(
          (14 + day * 0.5) * (weekend ? 0.6 : 1) * (0.85 + rand() * 0.3));

      for (let s = 0; s < sessions; s++) {
        const userId = 'user-' +
            String(1 + Math.floor(Math.pow(rand(), 1.6) * 24)).padStart(3, '0');
        const agent = pick(SAMPLE_AGENTS, 1.2);
        const sessionId = 'sess-' + hex(++seq * 2654435761);
        let clock = dayStart + Math.floor(rand() * (DAY_MS - 3600000));
        const invocations = 1 + Math.floor(rand() * 3);

        for (let inv = 0; inv < invocations; inv++) {
          const invocationId = 'inv-' + hex(++seq * 2246822519);
          const traceId = 'trace-' + hex(++seq * 3266489917);
          const emit = (eventType, fields) => {
            clock += 40 + Math.floor(rand() * 400);
            const iso = new Date(clock).toISOString();
            rows.push(Object.assign({
              timestamp: iso,
              event_date: iso.slice(0, 10),
              event_type: eventType,
              agent,
              model: '',
              tool: '',
              user_id: userId,
              session_id: sessionId,
              invocation_id: invocationId,
              trace_id: traceId,
              span_id: 'span-' + hex(++seq * 2654435761),
              status: 'OK',
              error_message: '',
              is_truncated: false,
              total_ms: null,
              ttft_ms: null,
              prompt_tokens: null,
              cached_tokens: null,
              candidates_tokens: null,
              thoughts_tokens: null,
              total_tokens: null,
            }, fields));
          };

          emit('USER_MESSAGE_RECEIVED', {});
          emit('AGENT_STARTING', {});
          let agentMs = 0;
          const steps = 1 + Math.floor(rand() * 3);

          for (let step = 0; step < steps; step++) {
            const model = pick(SAMPLE_MODELS, 1.1);
            emit('LLM_REQUEST', {model});
            if (rand() < (incident ? 0.08 : 0.03)) {
              const ms = Math.round(4000 + rand() * 8000);
              agentMs += ms;
              emit('LLM_ERROR', {
                model,
                status: 'ERROR',
                error_message: LLM_ERRORS[Math.floor(rand() * LLM_ERRORS.length)],
                total_ms: ms,
              });
              break;
            }
            const agentFactor = agent === 'sql_generator_agent' ? 1.35 :
                agent === 'eval_judge_agent'                    ? 1.25 :
                agent === 'schema_retriever_agent'              ? 0.75 :
                                                                  1.0;
            const totalMs = Math.round(MODEL_LATENCY_MS[model] * agentFactor *
                (incident ? 1.35 : 1) * (0.55 + rand() * 1.1));
            const promptTokens = Math.round((1200 + rand() * 5000) *
                (agent === 'schema_retriever_agent' ? 1.4 : 1));
            const cacheShare = (model === 'llm-pro' || model === 'llm-standard') ?
                0.35 + rand() * 0.35 :
                0.2 + rand() * 0.3;
            const cachedTokens = Math.round(promptTokens * cacheShare);
            const candidatesTokens = Math.round(200 + rand() * 1100);
            const thoughtsTokens = model === 'llm-pro' ?
                Math.round(400 + rand() * 1100) :
                model === 'llm-standard' ? Math.round(100 + rand() * 400) : 0;
            agentMs += totalMs;
            emit('LLM_RESPONSE', {
              model,
              total_ms: totalMs,
              ttft_ms: Math.round(totalMs * (0.18 + rand() * 0.22)),
              prompt_tokens: promptTokens,
              cached_tokens: cachedTokens,
              candidates_tokens: candidatesTokens,
              thoughts_tokens: thoughtsTokens,
              total_tokens: promptTokens + candidatesTokens + thoughtsTokens,
              is_truncated: rand() < 0.03,
            });

            if (step < steps - 1 || rand() < 0.6) {
              const tool = pick(SAMPLE_TOOLS, 1.0);
              emit('TOOL_STARTING', {tool});
              const toolMs = Math.round(TOOL_LATENCY_MS[tool] *
                  (incident ? 1.5 : 1) * (0.5 + rand() * 1.2));
              agentMs += toolMs;
              if (rand() < (incident ? 0.2 : 0.07)) {
                emit('TOOL_ERROR', {
                  tool,
                  status: 'ERROR',
                  error_message:
                      TOOL_ERRORS[Math.floor(rand() * TOOL_ERRORS.length)],
                  total_ms: toolMs + 900,
                });
              } else {
                emit('TOOL_COMPLETED', {tool, total_ms: toolMs});
              }
            }
          }
          emit('AGENT_COMPLETED', {total_ms: agentMs});
          clock += 30000 + Math.floor(rand() * 600000);
        }
      }
    }
    rows.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    return rows;
  }

  // ---------------------------------------------------------------------------
  // Metrics, dimensions, and preset pages
  // ---------------------------------------------------------------------------
  const DIMENSIONS = {
    event_date: 'Date',
    agent: 'Agent',
    model: 'Model',
    tool: 'Tool',
    event_type: 'Event type',
    user_id: 'User',
    status: 'Status',
  };

  // `better` says which direction of the period-over-period change is good.
  const METRICS = {
    events: {label: 'Events', fmt: 'count', sql: 'COUNT(*)'},
    sessions: {
      label: 'Sessions', fmt: 'count', sql: 'COUNT(DISTINCT session_id)',
    },
    users: {label: 'Users', fmt: 'count', sql: 'COUNT(DISTINCT user_id)'},
    traces: {label: 'Traces', fmt: 'count', sql: 'COUNT(DISTINCT trace_id)'},
    llm_calls: {
      label: 'LLM calls',
      fmt: 'count',
      sql: 'COUNTIF(event_type = \'LLM_RESPONSE\')',
    },
    tool_calls: {
      label: 'Tool calls',
      fmt: 'count',
      sql: 'COUNTIF(event_type IN (\'TOOL_COMPLETED\', \'TOOL_ERROR\'))',
    },
    tool_errors: {
      label: 'Tool errors',
      fmt: 'count',
      better: 'down',
      sql: 'COUNTIF(event_type = \'TOOL_ERROR\')',
    },
    error_rate: {
      label: 'Error rate (%)',
      fmt: 'pct',
      better: 'down',
      sql: '100 * SAFE_DIVIDE(' +
          'COUNTIF(event_type IN (\'LLM_ERROR\', \'TOOL_ERROR\')), ' +
          'COUNTIF(event_type IN (\'LLM_RESPONSE\', \'LLM_ERROR\', ' +
          '\'TOOL_COMPLETED\', \'TOOL_ERROR\')))',
    },
    avg_llm_latency: {
      label: 'Avg LLM latency (ms)',
      fmt: 'ms',
      better: 'down',
      sql: 'AVG(IF(event_type = \'LLM_RESPONSE\', total_ms, NULL))',
    },
    p95_llm_latency: {
      label: 'P95 LLM latency (ms)',
      fmt: 'ms',
      better: 'down',
      sql: 'APPROX_QUANTILES(IF(event_type = \'LLM_RESPONSE\', total_ms, ' +
          'NULL), 100)[OFFSET(95)]',
    },
    avg_ttft: {
      label: 'Avg time to first token (ms)',
      fmt: 'ms',
      better: 'down',
      sql: 'AVG(IF(event_type = \'LLM_RESPONSE\', ttft_ms, NULL))',
    },
    avg_tool_latency: {
      label: 'Avg tool latency (ms)',
      fmt: 'ms',
      better: 'down',
      sql: 'AVG(IF(event_type = \'TOOL_COMPLETED\', total_ms, NULL))',
    },
    p95_tool_latency: {
      label: 'P95 tool latency (ms)',
      fmt: 'ms',
      better: 'down',
      sql: 'APPROX_QUANTILES(IF(event_type = \'TOOL_COMPLETED\', total_ms, ' +
          'NULL), 100)[OFFSET(95)]',
    },
    total_tokens: {label: 'Total tokens', fmt: 'count', sql: 'SUM(total_tokens)'},
    cached_tokens: {
      label: 'Cached prompt tokens', fmt: 'count', sql: 'SUM(cached_tokens)',
    },
    thoughts_tokens: {
      label: 'Thinking tokens', fmt: 'count', sql: 'SUM(thoughts_tokens)',
    },
    cache_hit_rate: {
      label: 'Cache hit rate (%)',
      fmt: 'pct',
      better: 'up',
      sql: '100 * SAFE_DIVIDE(SUM(cached_tokens), SUM(prompt_tokens))',
    },
  };

  const KPI_SUBTITLES = {
    events: (s) => `${fmtCount(s.sessions)} sessions, ${fmtCount(s.traces)} traces`,
    sessions: (s) => `${fmtCount(s.users)} users`,
    users: (s) => `${fmtCount(s.sessions)} sessions`,
    llm_calls: (s) => `${fmtCount(s.llm_errors)} LLM errors`,
    tool_calls: (s) => `${fmtCount(s.tool_errors)} tool errors`,
    tool_errors: (s) => `of ${fmtCount(s.tool_calls)} tool calls`,
    error_rate: (s) =>
        `${fmtCount(s.llm_errors + s.tool_errors)} failed LLM and tool calls`,
    p95_llm_latency: (s) => `Average ${fmtMs(s.avg_llm_latency)}`,
    avg_ttft: (s) => `Across ${fmtCount(s.llm_calls)} LLM calls`,
    p95_tool_latency: (s) => `Average ${fmtMs(s.avg_tool_latency)}`,
    total_tokens: (s) => `${fmtCount(s.thoughts_tokens)} thinking tokens`,
    cache_hit_rate: (s) => `${fmtCount(s.cached_tokens)} of ${
        fmtCount(s.prompt_tokens)} prompt tokens`,
  };

  function visual(id, title, type, dim, metric, span, extra) {
    return Object.assign({id, title, type, dim, metric, span}, extra || {});
  }

  const PAGE_BUILDERS = {
    activity: (uid, prompt, type) => ({
      id: uid,
      title: 'Agent Activity Overview',
      subtitle: 'Sessions, users, and LLM and tool calls by agent and day.',
      prompt,
      kpis: ['sessions', 'users', 'llm_calls', 'tool_calls'],
      visuals: [
        visual(uid + '-1', 'Sessions and traces per day', 'line', 'event_date',
            'sessions', 7, {secondaryMetric: 'traces'}),
        visual(uid + '-2', 'Event type mix', 'donut', 'event_type', 'events', 5),
        visual(uid + '-3', 'LLM calls by agent', type || 'hbar', 'agent',
            'llm_calls', 6),
        visual(uid + '-4', 'Top users by sessions', 'bar', 'user_id',
            'sessions', 6),
      ],
    }),
    tokens: (uid, prompt, type) => ({
      id: uid,
      title: 'Token Consumption & Caching',
      subtitle: 'Token usage from attributes.usage_metadata and the share of ' +
          'prompt tokens served from the context cache.',
      prompt,
      kpis: ['total_tokens', 'cache_hit_rate', 'llm_calls', 'avg_ttft'],
      visuals: [
        visual(uid + '-1', 'Total and cached tokens per day', 'line',
            'event_date', 'total_tokens', 7, {secondaryMetric: 'cached_tokens'}),
        visual(uid + '-2', 'Token share by model', 'donut', 'model',
            'total_tokens', 5),
        visual(uid + '-3', 'Total tokens by agent', type || 'hbar', 'agent',
            'total_tokens', 6),
        visual(uid + '-4', 'Cache hit rate by model', 'bar', 'model',
            'cache_hit_rate', 6),
      ],
    }),
    latency: (uid, prompt, type) => ({
      id: uid,
      title: 'Latency',
      subtitle: 'LLM latency and time to first token from latency_ms, and ' +
          'tool latency for completed calls.',
      prompt,
      kpis: ['p95_llm_latency', 'avg_ttft', 'p95_tool_latency', 'error_rate'],
      visuals: [
        visual(uid + '-1', 'Avg LLM latency and time to first token per day',
            'line', 'event_date', 'avg_llm_latency', 7,
            {secondaryMetric: 'avg_ttft'}),
        visual(uid + '-2', 'P95 LLM latency by model', type || 'bar', 'model',
            'p95_llm_latency', 5),
        visual(uid + '-3', 'Avg tool latency by tool', 'hbar', 'tool',
            'avg_tool_latency', 6),
        visual(uid + '-4', 'Slowest LLM and tool calls', 'table', 'span_id',
            'total_ms', 6),
      ],
    }),
    errors: (uid, prompt, type) => ({
      id: uid,
      title: 'Errors',
      subtitle: 'Failed LLM and tool calls, the tools and agents behind them, ' +
          'and their error messages.',
      prompt,
      kpis: ['error_rate', 'tool_errors', 'llm_calls', 'tool_calls'],
      visuals: [
        visual(uid + '-1', 'Tool errors per day', 'line', 'event_date',
            'tool_errors', 7),
        visual(uid + '-2', 'Tool errors by tool', type || 'hbar', 'tool',
            'tool_errors', 5),
        visual(uid + '-3', 'Error rate by agent', 'bar', 'agent', 'error_rate',
            6),
        visual(uid + '-4', 'Recent errors', 'table', 'span_id', 'total_ms', 6,
            {errorsOnly: true}),
      ],
    }),
    tools: (uid, prompt, type) => ({
      id: uid,
      title: 'Tool Usage',
      subtitle: 'Tool calls, their latency, and the agents that make them.',
      prompt,
      kpis: ['tool_calls', 'tool_errors', 'p95_tool_latency', 'error_rate'],
      visuals: [
        visual(uid + '-1', 'Tool calls per day', 'line', 'event_date',
            'tool_calls', 7),
        visual(uid + '-2', 'Tool call mix', type || 'donut', 'tool',
            'tool_calls', 5),
        visual(uid + '-3', 'Tool calls by agent', 'hbar', 'agent', 'tool_calls',
            6),
        visual(uid + '-4', 'Avg tool latency by tool', 'bar', 'tool',
            'avg_tool_latency', 6),
      ],
    }),
  };

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const SAMPLE_ROWS = generateSampleEvents();
  const SAMPLE_LABEL = 'agent_events (synthetic sample, 30 days)';

  const state = {
    rows: SAMPLE_ROWS,
    activeView: 'report',
    slicers: {days: 30, agent: 'ALL', model: 'ALL'},
    crossFilters: {},
    copilotMode: 'new',
    activePageId: 'page-activity',
    pages: [
      PAGE_BUILDERS.activity('page-activity', 'Default page', null),
      PAGE_BUILDERS.latency('page-latency', 'Default page', null),
    ],
    tableId: 'your-project.your_dataset.agent_events',
  };

  // ---------------------------------------------------------------------------
  // Filtering and aggregation
  // ---------------------------------------------------------------------------
  function newestDate(rows) {
    let max = '';
    for (const r of rows) {
      if (r.event_date > max) max = r.event_date;
    }
    return max;
  }

  // Returns rows in the selected window (or the window just before it when
  // `previousWindow` is true), after slicers and cross-filters.
  function filterRows(options) {
    const opts = options || {};
    const newest = newestDate(state.rows);
    if (!newest) return [];
    const days = state.slicers.days;
    const endMs = Date.parse(newest + 'T00:00:00Z') -
        (opts.previousWindow ? days * DAY_MS : 0);
    const startMs = endMs - (days - 1) * DAY_MS;
    return state.rows.filter((r) => {
      const dayMs = Date.parse(r.event_date + 'T00:00:00Z');
      if (Number.isNaN(dayMs) || dayMs < startMs || dayMs > endMs) return false;
      if (state.slicers.agent !== 'ALL' && r.agent !== state.slicers.agent) {
        return false;
      }
      if (state.slicers.model !== 'ALL' && r.model !== state.slicers.model) {
        return false;
      }
      for (const [field, value] of Object.entries(state.crossFilters)) {
        if (field === opts.ignoreField) continue;
        if (String(r[field] ?? '') !== String(value)) return false;
      }
      return true;
    });
  }

  function mean(values) {
    return values.length ?
        values.reduce((a, b) => a + b, 0) / values.length : null;
  }

  // Nearest-rank percentile; BigQuery's APPROX_QUANTILES can differ slightly.
  function percentile(values, p) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
  }

  function isNum(v) {
    return typeof v === 'number' && Number.isFinite(v);
  }

  function summarize(rows) {
    const sessions = new Set();
    const users = new Set();
    const traces = new Set();
    const llmLatency = [];
    const ttft = [];
    const toolLatency = [];
    let llmCalls = 0;
    let llmErrors = 0;
    let toolCalls = 0;
    let toolErrors = 0;
    let promptTokens = 0;
    let cachedTokens = 0;
    let thoughtsTokens = 0;
    let totalTokens = 0;

    for (const r of rows) {
      if (r.session_id) sessions.add(r.session_id);
      if (r.user_id) users.add(r.user_id);
      if (r.trace_id) traces.add(r.trace_id);
      switch (r.event_type) {
        case 'LLM_RESPONSE':
          llmCalls++;
          if (isNum(r.total_ms)) llmLatency.push(r.total_ms);
          if (isNum(r.ttft_ms)) ttft.push(r.ttft_ms);
          break;
        case 'LLM_ERROR':
          llmErrors++;
          break;
        case 'TOOL_COMPLETED':
          toolCalls++;
          if (isNum(r.total_ms)) toolLatency.push(r.total_ms);
          break;
        case 'TOOL_ERROR':
          toolCalls++;
          toolErrors++;
          break;
        default:
          break;
      }
      if (isNum(r.prompt_tokens)) promptTokens += r.prompt_tokens;
      if (isNum(r.cached_tokens)) cachedTokens += r.cached_tokens;
      if (isNum(r.thoughts_tokens)) thoughtsTokens += r.thoughts_tokens;
      if (isNum(r.total_tokens)) totalTokens += r.total_tokens;
    }

    const operations = llmCalls + llmErrors + toolCalls;
    return {
      events: rows.length,
      sessions: sessions.size,
      users: users.size,
      traces: traces.size,
      llm_calls: llmCalls,
      llm_errors: llmErrors,
      tool_calls: toolCalls,
      tool_errors: toolErrors,
      error_rate: operations ? ((llmErrors + toolErrors) / operations) * 100 : null,
      avg_llm_latency: mean(llmLatency),
      p95_llm_latency: percentile(llmLatency, 95),
      avg_ttft: mean(ttft),
      avg_tool_latency: mean(toolLatency),
      p95_tool_latency: percentile(toolLatency, 95),
      prompt_tokens: promptTokens,
      cached_tokens: cachedTokens,
      thoughts_tokens: thoughtsTokens,
      total_tokens: totalTokens,
      cache_hit_rate: promptTokens ? (cachedTokens / promptTokens) * 100 : null,
    };
  }

  function aggregateBy(rows, dim, metric, secondaryMetric) {
    const groups = new Map();
    for (const r of rows) {
      const key = r[dim];
      if (key === '' || key === null || key === undefined) continue;
      const label = String(key);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(r);
    }
    const series = [];
    for (const [label, list] of groups) {
      const s = summarize(list);
      const value = s[metric];
      if (!isNum(value)) continue;
      series.push({
        label,
        value,
        secondary: secondaryMetric ? s[secondaryMetric] : null,
      });
    }
    if (dim === 'event_date') {
      series.sort((a, b) => a.label.localeCompare(b.label));
    } else {
      series.sort((a, b) => b.value - a.value || a.label.localeCompare(b.label));
    }
    return series;
  }

  // ---------------------------------------------------------------------------
  // Formatting and escaping
  // ---------------------------------------------------------------------------
  function fmtCount(n) {
    if (!isNum(n)) return '--';
    const abs = Math.abs(n);
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (abs >= 1e4) return (n / 1e3).toFixed(1) + 'K';
    return Math.round(n).toLocaleString('en-US');
  }

  function fmtMs(n) {
    return isNum(n) ? `${Math.round(n).toLocaleString('en-US')} ms` : '--';
  }

  function fmtPct(n) {
    return isNum(n) ? `${n.toFixed(1)}%` : '--';
  }

  function fmtMetric(metric, value) {
    const kind = METRICS[metric] ? METRICS[metric].fmt : 'count';
    if (kind === 'ms') return fmtMs(value);
    if (kind === 'pct') return fmtPct(value);
    return fmtCount(value);
  }

  function metricLabel(metric) {
    return METRICS[metric] ? METRICS[metric].label : metric;
  }

  function escHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
  }

  function escAttr(s) {
    return escHtml(s).replace(/"/g, '&quot;');
  }

  // Formats one CSV field per RFC 4180: doubles embedded quotes and quotes any
  // field that contains a comma, quote, CR, or LF.
  function csvCell(v) {
    const s = String(v ?? '');
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  // Parses RFC 4180 CSV text into an array of string arrays.
  function parseCsv(text) {
    const records = [];
    let record = [];
    let field = '';
    let quoted = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (quoted) {
        if (c === '"' && text[i + 1] === '"') {
          field += '"';
          i++;
        } else if (c === '"') {
          quoted = false;
        } else {
          field += c;
        }
      } else if (c === '"') {
        quoted = true;
      } else if (c === ',') {
        record.push(field);
        field = '';
      } else if (c === '\n' || c === '\r') {
        if (c === '\r' && text[i + 1] === '\n') i++;
        record.push(field);
        records.push(record);
        record = [];
        field = '';
      } else {
        field += c;
      }
    }
    if (field !== '' || record.length) {
      record.push(field);
      records.push(record);
    }
    return records.filter((r) => r.some((cell) => cell.trim() !== ''));
  }

  // ---------------------------------------------------------------------------
  // Prompt compiler (rule-based keyword matching)
  // ---------------------------------------------------------------------------
  function distinctValues(field) {
    const values = new Set();
    for (const r of state.rows) {
      if (r[field]) values.add(String(r[field]));
    }
    return [...values].sort();
  }

  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function mentions(text, value) {
    const spaced = value.replace(/[_-]+/g, ' ');
    return new RegExp(`\\b${escapeRegex(value.toLowerCase())}\\b`).test(text) ||
        (spaced !== value &&
         new RegExp(`\\b${escapeRegex(spaced.toLowerCase())}\\b`).test(text));
  }

  function detectFilters(ql) {
    const filters = {};
    let days = null;
    if (/\b(7d|7 days|last week|past week)\b/.test(ql)) days = 7;
    else if (/\b(14d|14 days|2 weeks|two weeks)\b/.test(ql)) days = 14;
    else if (/\b(30d|30 days|last month|past month)\b/.test(ql)) days = 30;
    for (const agent of distinctValues('agent')) {
      if (mentions(ql, agent)) filters.agent = agent;
    }
    for (const model of distinctValues('model')) {
      if (mentions(ql, model)) filters.model = model;
    }
    for (const tool of distinctValues('tool')) {
      if (mentions(ql, tool)) filters.tool = tool;
    }
    const user = ql.match(/\buser[-_ ]?(\d{1,6})\b/);
    if (user) {
      const wanted = distinctValues('user_id').find(
          (u) => u.toLowerCase().replace(/[^0-9]/g, '').replace(/^0+/, '') ===
              user[1].replace(/^0+/, ''));
      if (wanted) filters.user_id = wanted;
    }
    if (/\b(only errors|errors only|failed calls only|status error)\b/.test(ql)) {
      filters.status = 'ERROR';
    }
    return {days, filters};
  }

  function detectChartType(ql) {
    if (/\b(donut|pie)\b/.test(ql)) return 'donut';
    if (/\b(line|trend|over time|daily|per day)\b/.test(ql)) return 'line';
    if (/\b(horizontal|hbar|ranked|ranking|top)\b/.test(ql)) return 'hbar';
    if (/\b(table|rows|list)\b/.test(ql)) return 'table';
    if (/\b(bar|column)\b/.test(ql)) return 'bar';
    return null;
  }

  function detectDimension(ql) {
    if (/\bmodels?\b/.test(ql)) return 'model';
    if (/\btools?\b/.test(ql)) return 'tool';
    if (/\bagents?\b/.test(ql)) return 'agent';
    if (/\busers?\b/.test(ql)) return 'user_id';
    if (/\bevent types?\b/.test(ql)) return 'event_type';
    if (/\bstatus\b/.test(ql)) return 'status';
    if (/\b(daily|trend|over time|per day|date)\b/.test(ql)) return 'event_date';
    return 'agent';
  }

  function detectMetric(ql) {
    const tool = /\btools?\b/.test(ql);
    if (/\bp95\b/.test(ql) && /\blatency\b/.test(ql)) {
      return tool ? 'p95_tool_latency' : 'p95_llm_latency';
    }
    if (/\b(ttft|first token)\b/.test(ql)) return 'avg_ttft';
    if (/\b(latency|duration|slow\w*)\b/.test(ql)) {
      return tool ? 'avg_tool_latency' : 'avg_llm_latency';
    }
    if (/\bcache\b/.test(ql)) return 'cache_hit_rate';
    if (/\b(thinking|thoughts?)\b/.test(ql)) return 'thoughts_tokens';
    if (/\btokens?\b/.test(ql)) return 'total_tokens';
    if (/\b(error rate|failure rate)\b/.test(ql)) return 'error_rate';
    if (/\b(errors?|fail\w*)\b/.test(ql)) return 'tool_errors';
    if (/\bsessions?\b/.test(ql)) return 'sessions';
    if (/\btraces?\b/.test(ql)) return 'traces';
    if (/\bllm calls?\b/.test(ql)) return 'llm_calls';
    if (/\btool calls?\b/.test(ql)) return 'tool_calls';
    if (/\busers?\b/.test(ql)) return 'users';
    return 'events';
  }

  function detectTheme(ql) {
    if (/\b(errors?|fail\w*|exceptions?|truncat\w*)\b/.test(ql)) return 'errors';
    if (/\b(latency|ttft|first token|slow\w*|p95|p50|percentile|duration|performance)\b/
            .test(ql)) {
      return 'latency';
    }
    if (/\b(tokens?|cache|cached|caching|thinking)\b/.test(ql)) return 'tokens';
    if (/\b(tools?|function calls?)\b/.test(ql)) return 'tools';
    if (/\b(overview|activity|adoption|usage|sessions?|users?|traces?|summary)\b/
            .test(ql)) {
      return 'activity';
    }
    return null;
  }

  function compilePrompt(rawPrompt) {
    const prompt = rawPrompt.trim();
    const ql = prompt.toLowerCase();
    const steps = [];
    const {days, filters} = detectFilters(ql);
    const chartType = detectChartType(ql);
    if (days) steps.push(`Time window: last ${days} days`);
    if (Object.keys(filters).length) {
      steps.push('Filters: ' +
          Object.entries(filters).map(([k, v]) => `${k} = ${v}`).join(', '));
    }

    const filterOnly = /^(filter|show only|restrict|focus on)\b/.test(ql) &&
        !/(dashboard|chart|plot|create|build|add|compare|breakdown|analy[sz]e)/
             .test(ql);
    if (filterOnly && (days || Object.keys(filters).length)) {
      return {
        action: 'filter', days, filters, steps,
        reply: 'Applied the filters to the current page.',
      };
    }

    if (/\b(switch|change|convert|make)\b/.test(ql) && chartType) {
      steps.push(`Chart type: ${chartType}`);
      return {
        action: 'switch_chart', chartType, days, filters, steps,
        reply: `Switched the charts on this page to <b>${chartType}</b>.`,
      };
    }

    const dim = detectDimension(ql);
    const metric = detectMetric(ql);

    if (/^(add|pin|include|append)\b/.test(ql) || state.copilotMode === 'modify') {
      const type = chartType || (dim === 'event_date' ? 'line' : 'hbar');
      const added = visual('v-' + Date.now().toString(36),
          `${metricLabel(metric)} by ${DIMENSIONS[dim]}`, type, dim, metric, 6,
          {isNew: true});
      steps.push(`New ${type} visual: ${metric} by ${dim}`);
      return {
        action: 'add_visual', visual: added, days, filters, steps,
        reply: `Added <b>${escHtml(added.title)}</b> to this page.`,
      };
    }

    const uid = 'page-' + Date.now().toString(36);
    const theme = detectTheme(ql);
    let page;
    if (theme) {
      page = PAGE_BUILDERS[theme](uid, prompt, chartType);
      steps.push(`Matched the ${theme} template`);
    } else {
      const type = chartType || (dim === 'event_date' ? 'line' : 'bar');
      const altDim = dim === 'agent' ? 'model' : 'agent';
      const kpis = [...new Set([
        KPI_SUBTITLES[metric] ? metric : 'events', 'sessions', 'llm_calls',
        'error_rate',
      ])].slice(0, 4);
      page = {
        id: uid,
        title: `${metricLabel(metric)} by ${DIMENSIONS[dim]}`,
        subtitle: 'Built from your prompt.',
        prompt,
        kpis,
        visuals: [
          visual(uid + '-1', `${metricLabel(metric)} by ${DIMENSIONS[dim]}`,
              type, dim, metric, 6),
          visual(uid + '-2', `${metricLabel(metric)} per day`, 'line',
              'event_date', metric, 6),
          visual(uid + '-3', `${metricLabel(metric)} by ${DIMENSIONS[altDim]}`,
              'donut', altDim, metric, 5),
          visual(uid + '-4', 'Slowest LLM and tool calls', 'table', 'span_id',
              'total_ms', 7),
        ],
      };
      steps.push(`No template matched; built a custom page for ${metric} by ${dim}`);
    }
    steps.push(`${page.kpis.length} KPIs and ${page.visuals.length} visuals`);
    return {
      action: 'create_page', page, days, filters, steps,
      reply: `Created the page <b>${escHtml(page.title)}</b>.`,
    };
  }

  // ---------------------------------------------------------------------------
  // BigQuery SQL
  // ---------------------------------------------------------------------------
  const PROJECT_RE = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/;
  const DATASET_RE = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
  const TABLE_RE = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$/;

  function parseTableId(text) {
    const parts = String(text).trim().split('.');
    if (parts.length !== 3) return null;
    const [project, dataset, table] = parts;
    if (!PROJECT_RE.test(project) || !DATASET_RE.test(dataset) ||
        !TABLE_RE.test(table)) {
      return null;
    }
    return parts;
  }

  // Renders a GoogleSQL string literal.
  function sqlString(value) {
    const escaped = String(value)
                        .replace(/\\/g, '\\\\')
                        .replace(/'/g, '\\\'')
                        .replace(/\r/g, '\\r')
                        .replace(/\n/g, '\\n');
    return `'${escaped}'`;
  }

  function sqlComment(text) {
    return String(text).replace(/[\r\n]+/g, ' ');
  }

  function sqlFilters() {
    const clauses = [];
    if (state.slicers.agent !== 'ALL') {
      clauses.push(`agent = ${sqlString(state.slicers.agent)}`);
    }
    if (state.slicers.model !== 'ALL') {
      clauses.push(`model = ${sqlString(state.slicers.model)}`);
    }
    for (const [field, value] of Object.entries(state.crossFilters)) {
      if (DIMENSIONS[field]) clauses.push(`${field} = ${sqlString(value)}`);
    }
    return clauses;
  }

  function whereSql(extra) {
    const clauses = [...sqlFilters(), ...(extra || [])];
    return clauses.length ? `\nWHERE\n  ${clauses.join('\n  AND ')}` : '';
  }

  function generatePageSql(page, tableParts) {
    const table = tableParts.join('.');
    const lines = [
      `-- BQAA Copilot preview: ${sqlComment(page.title)}`,
      `-- Prompt: ${sqlComment(page.prompt)}`,
      '-- Run as one multi-statement query. The window is relative to now;',
      '-- the dashboard window ends at the newest date in the loaded data.',
      '',
      'CREATE TEMP TABLE events AS',
      'SELECT',
      '  DATE(timestamp) AS event_date,',
      '  timestamp,',
      '  event_type,',
      '  agent,',
      '  user_id,',
      '  session_id,',
      '  trace_id,',
      '  span_id,',
      '  status,',
      '  error_message,',
      '  COALESCE(JSON_VALUE(attributes, \'$.model\'),',
      '           JSON_VALUE(attributes, \'$.model_version\')) AS model,',
      '  COALESCE(JSON_VALUE(content, \'$.tool\'),',
      '           JSON_VALUE(content, \'$.tool_name\')) AS tool,',
      '  SAFE_CAST(JSON_VALUE(latency_ms, \'$.total_ms\') AS FLOAT64) AS total_ms,',
      '  SAFE_CAST(JSON_VALUE(latency_ms, \'$.time_to_first_token_ms\') AS FLOAT64)',
      '    AS ttft_ms,',
      '  SAFE_CAST(JSON_VALUE(attributes,',
      '    \'$.usage_metadata.prompt_token_count\') AS INT64) AS prompt_tokens,',
      '  SAFE_CAST(JSON_VALUE(attributes,',
      '    \'$.usage_metadata.cached_content_token_count\') AS INT64) AS cached_tokens,',
      '  SAFE_CAST(JSON_VALUE(attributes,',
      '    \'$.usage_metadata.thoughts_token_count\') AS INT64) AS thoughts_tokens,',
      '  SAFE_CAST(JSON_VALUE(attributes,',
      '    \'$.usage_metadata.total_token_count\') AS INT64) AS total_tokens',
      `FROM \`${table}\``,
      `WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${
          state.slicers.days} DAY);`,
      '',
      '-- KPIs',
      'SELECT',
      page.kpis.map((k) => `  ${METRICS[k].sql} AS ${k}`).join(',\n'),
      `FROM events${whereSql()};`,
    ];

    for (const v of page.visuals) {
      lines.push('', `-- ${sqlComment(v.title)}`);
      if (v.type === 'table') {
        const types = v.errorsOnly ? '\'LLM_ERROR\', \'TOOL_ERROR\'' :
                                     '\'LLM_RESPONSE\', \'LLM_ERROR\', ' +
                '\'TOOL_COMPLETED\', \'TOOL_ERROR\'';
        lines.push(
            'SELECT timestamp, event_type, agent, model, tool, total_ms,',
            '  total_tokens, status, error_message',
            `FROM events${whereSql([`event_type IN (${types})`])}`,
            `ORDER BY ${v.errorsOnly ? 'timestamp' : 'total_ms'} DESC`,
            'LIMIT 12;');
        continue;
      }
      const select = [`  ${v.dim}`, `  ${METRICS[v.metric].sql} AS ${v.metric}`];
      if (v.secondaryMetric) {
        select.push(`  ${METRICS[v.secondaryMetric].sql} AS ${v.secondaryMetric}`);
      }
      const extra = v.dim === 'event_date' ? [] : [`${v.dim} IS NOT NULL`];
      lines.push(
          'SELECT', select.join(',\n'), `FROM events${whereSql(extra)}`,
          `GROUP BY ${v.dim}`,
          v.dim === 'event_date' ? 'ORDER BY event_date;' :
                                   `ORDER BY ${v.metric} DESC\nLIMIT 10;`);
    }
    return lines.join('\n');
  }

  // ---------------------------------------------------------------------------
  // Charts
  // ---------------------------------------------------------------------------
  const tooltipEl = document.getElementById('chartTooltip');

  function showTooltip(e, html) {
    tooltipEl.innerHTML = html;
    tooltipEl.classList.remove('hidden');
    const pad = 12;
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    const rect = tooltipEl.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 8) x = e.clientX - rect.width - pad;
    if (y + rect.height > window.innerHeight - 8) y = e.clientY - rect.height - pad;
    tooltipEl.style.left = x + 'px';
    tooltipEl.style.top = y + 'px';
  }

  function hideTooltip() {
    tooltipEl.classList.add('hidden');
  }

  function tooltipHtml(d, metric, secondaryMetric, hint) {
    let html = `<div style="font-weight:600;">${escHtml(d.label)}</div>` +
        `<div>${escHtml(metricLabel(metric))}: <b>${
            escHtml(fmtMetric(metric, d.value))}</b></div>`;
    if (secondaryMetric) {
      html += `<div>${escHtml(metricLabel(secondaryMetric))}: <b>${
          escHtml(fmtMetric(secondaryMetric, d.secondary))}</b></div>`;
    }
    if (hint) {
      html += '<div style="color:var(--muted-foreground);margin-top:2px;">' +
          'Click to cross-filter</div>';
    }
    return html;
  }

  function toggleCrossFilter(field, value) {
    if (field === 'event_date' || !DIMENSIONS[field]) return;
    if (state.crossFilters[field] === value) {
      delete state.crossFilters[field];
    } else {
      state.crossFilters[field] = value;
    }
    renderAll();
  }

  function emptyStateHtml() {
    return '<div style="height:175px;display:flex;align-items:center;' +
        'justify-content:center;font-size:12px;color:var(--muted-foreground);">' +
        'No matching rows for the current filters.</div>';
  }

  function renderLineChart(container, series, vis) {
    if (!series.length) {
      container.innerHTML = emptyStateHtml();
      return;
    }
    const W = 540, H = 210, padL = 56, padR = 16, padT = 18, padB = 34;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const hasSec = Boolean(vis.secondaryMetric);
    const maxVal = Math.max(1, ...series.map((d) => Math.max(
        d.value, hasSec && isNum(d.secondary) ? d.secondary : 0))) * 1.1;
    const xFor = (i) => padL +
        (series.length === 1 ? plotW / 2 : (i / (series.length - 1)) * plotW);
    const yFor = (v) => padT + plotH - (v / maxVal) * plotH;
    const points = (pick) => series
        .map((d, i) => `${xFor(i).toFixed(1)},${yFor(pick(d) || 0).toFixed(1)}`)
        .join(' ');
    const pts1 = points((d) => d.value);
    const area = `${padL},${padT + plotH} ${pts1} ${
        xFor(series.length - 1).toFixed(1)},${padT + plotH}`;

    let grid = '';
    for (let g = 0; g <= 4; g++) {
      const y = padT + (g / 4) * plotH;
      grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" ` +
          'stroke="var(--border)" stroke-dasharray="3,3"/>' +
          `<text x="${padL - 6}" y="${y + 4}" text-anchor="end" ` +
          `fill="var(--muted-foreground)" font-size="10">${
              escHtml(fmtMetric(vis.metric, maxVal * (1 - g / 4)))}</text>`;
    }

    const step = Math.max(1, Math.ceil(series.length / 6));
    let xLabels = '';
    series.forEach((d, i) => {
      if (i % step === 0 || i === series.length - 1) {
        xLabels += `<text x="${xFor(i)}" y="${H - 10}" text-anchor="middle" ` +
            `fill="var(--muted-foreground)" font-size="10">${
                escHtml(d.label.slice(5))}</text>`;
      }
    });
    const dots = series
        .map((d, i) => `<circle cx="${xFor(i).toFixed(1)}" cy="${
            yFor(d.value).toFixed(1)}" r="3.5" fill="var(--chart-1)" ` +
            `stroke="var(--card)" stroke-width="1.5" style="cursor:pointer" ` +
            `data-idx="${i}"/>`)
        .join('');

    const swatch = (color, metric) => '<span style="display:flex;' +
        'align-items:center;gap:6px;"><span class="legend-swatch" ' +
        `style="background:${color}"></span>${escHtml(metricLabel(metric))}</span>`;
    container.innerHTML = `<div class="line-legend">${
        swatch('var(--chart-1)', vis.metric)}${
        hasSec ? swatch('var(--chart-2)', vis.secondaryMetric) : ''}</div>` +
        `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:208px;` +
        'overflow:visible;" role="img">' +
        `<defs><linearGradient id="grad-${escAttr(vis.id)}" x1="0" y1="0" ` +
        'x2="0" y2="1"><stop offset="0%" stop-color="var(--chart-1)" ' +
        'stop-opacity="0.28"/><stop offset="100%" stop-color="var(--chart-1)" ' +
        'stop-opacity="0.02"/></linearGradient></defs>' + grid +
        `<polygon points="${area}" fill="url(#grad-${escAttr(vis.id)})"/>` +
        '<polyline fill="none" stroke="var(--chart-1)" stroke-width="2.2" ' +
        `stroke-linejoin="round" points="${pts1}"/>` +
        (hasSec ? '<polyline fill="none" stroke="var(--chart-2)" ' +
                 'stroke-width="2" stroke-dasharray="5,3" ' +
                 `stroke-linejoin="round" points="${
                     points((d) => d.secondary)}"/>` :
                  '') +
        dots + xLabels + '</svg>';

    container.querySelectorAll('circle[data-idx]').forEach((el) => {
      const d = series[Number(el.dataset.idx)];
      el.addEventListener('mousemove',
          (e) => showTooltip(e, tooltipHtml(d, vis.metric, vis.secondaryMetric)));
      el.addEventListener('mouseleave', hideTooltip);
    });
  }

  function bindSeriesEvents(elements, items, vis) {
    elements.forEach((el) => {
      const d = items[Number(el.dataset.idx)];
      el.addEventListener('click', () => toggleCrossFilter(vis.dim, d.label));
      el.addEventListener('mousemove',
          (e) => showTooltip(e, tooltipHtml(d, vis.metric, null, true)));
      el.addEventListener('mouseleave', hideTooltip);
    });
  }

  function renderBarChart(container, series, vis) {
    const items = series.slice(0, 8);
    if (!items.length) {
      container.innerHTML = emptyStateHtml();
      return;
    }
    const W = 460, H = 215, padL = 56, padR = 12, padT = 16, padB = 54;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const maxVal = Math.max(1, ...items.map((d) => d.value)) * 1.12;
    const step = plotW / items.length;
    const barW = Math.min(44, step * 0.64);
    const active = state.crossFilters[vis.dim];

    let grid = '';
    for (let g = 0; g <= 3; g++) {
      const y = padT + (g / 3) * plotH;
      grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" ` +
          'stroke="var(--border)" stroke-dasharray="3,3"/>' +
          `<text x="${padL - 6}" y="${y + 4}" text-anchor="end" ` +
          `fill="var(--muted-foreground)" font-size="10">${
              escHtml(fmtMetric(vis.metric, maxVal * (1 - g / 3)))}</text>`;
    }
    let bars = '';
    items.forEach((d, i) => {
      const bh = Math.max(2, (d.value / maxVal) * plotH);
      const x = padL + i * step + (step - barW) / 2;
      const y = padT + plotH - bh;
      const opacity = active && active !== d.label ? '0.32' : '1';
      const short = d.label.length > 14 ? d.label.slice(0, 13) + '…' : d.label;
      bars += `<g class="chart-bar" data-idx="${i}" opacity="${opacity}">` +
          `<rect x="${x}" y="${y}" width="${barW}" height="${bh}" rx="3" ` +
          `fill="${CHART_COLORS[i % CHART_COLORS.length]}"/>` +
          `<text x="${x + barW / 2}" y="${y - 4}" text-anchor="middle" ` +
          'fill="var(--foreground)" font-size="10" font-weight="600">' +
          `${escHtml(fmtMetric(vis.metric, d.value))}</text>` +
          `<text x="${x + barW / 2}" y="${padT + plotH + 14}" ` +
          'text-anchor="middle" fill="var(--muted-foreground)" font-size="10">' +
          `${escHtml(short)}</text></g>`;
    });
    container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;` +
        `height:208px;overflow:visible;" role="img">${grid}${bars}</svg>`;
    bindSeriesEvents(container.querySelectorAll('.chart-bar'), items, vis);
  }

  function renderHBarChart(container, series, vis) {
    const items = series.slice(0, 7);
    if (!items.length) {
      container.innerHTML = emptyStateHtml();
      return;
    }
    const maxVal = Math.max(1, ...items.map((d) => d.value));
    const active = state.crossFilters[vis.dim];
    container.innerHTML = '<div class="hbar-list">' +
        items.map((d, i) => {
          const pct = Math.max(2, (d.value / maxVal) * 100);
          const opacity = active && active !== d.label ? '0.35' : '1';
          return `<div class="chart-bar" style="opacity:${opacity}" ` +
              `data-idx="${i}"><div class="hbar-meta">` +
              `<span class="truncate" style="font-weight:500;" title="${
                  escAttr(d.label)}">${escHtml(d.label)}</span>` +
              '<span class="mono tabular" style="color:var(--muted-foreground);' +
              `flex-shrink:0;">${escHtml(fmtMetric(vis.metric, d.value))}</span>` +
              '</div><div class="hbar-track"><div class="hbar-fill" ' +
              `style="width:${pct.toFixed(1)}%;background:${
                  CHART_COLORS[i % CHART_COLORS.length]}"></div></div></div>`;
        }).join('') +
        '</div>';
    bindSeriesEvents(container.querySelectorAll('.chart-bar'), items, vis);
  }

  function renderDonutChart(container, series, vis) {
    const items = series.slice(0, 6);
    if (!items.length) {
      container.innerHTML = emptyStateHtml();
      return;
    }
    const total = items.reduce((a, d) => a + Math.max(0, d.value), 0) || 1;
    const active = state.crossFilters[vis.dim];
    const R = 64, r = 40, cx = 82, cy = 82;
    let angle = -Math.PI / 2;

    const slices = items.map((d, i) => {
      const frac = Math.max(0.005, d.value / total);
      const a1 = angle;
      const a2 = angle + frac * Math.PI * 2;
      angle = a2;
      const large = frac > 0.5 ? 1 : 0;
      const path = frac >= 0.995 ?
          `M ${cx} ${cy - R} A ${R} ${R} 0 1 1 ${cx - 0.01} ${cy - R} Z ` +
              `M ${cx} ${cy - r} A ${r} ${r} 0 1 0 ${cx - 0.01} ${cy - r} Z` :
          `M ${cx + R * Math.cos(a1)} ${cy + R * Math.sin(a1)} ` +
              `A ${R} ${R} 0 ${large} 1 ${cx + R * Math.cos(a2)} ${
                  cy + R * Math.sin(a2)} ` +
              `L ${cx + r * Math.cos(a2)} ${cy + r * Math.sin(a2)} ` +
              `A ${r} ${r} 0 ${large} 0 ${cx + r * Math.cos(a1)} ${
                  cy + r * Math.sin(a1)} Z`;
      const opacity = active && active !== d.label ? '0.3' : '1';
      return `<path d="${path}" fill="${CHART_COLORS[i % CHART_COLORS.length]}" ` +
          `opacity="${opacity}" class="chart-slice" data-idx="${i}"/>`;
    }).join('');

    const legend = items.map((d, i) => {
      const opacity = active && active !== d.label ? '0.4' : '1';
      return `<div class="donut-legend-row" style="opacity:${opacity}" ` +
          `data-idx="${i}"><div style="display:flex;align-items:center;gap:6px;` +
          'min-width:0;"><span class="legend-swatch" style="background:' +
          `${CHART_COLORS[i % CHART_COLORS.length]}"></span>` +
          `<span class="truncate" title="${escAttr(d.label)}">${
              escHtml(d.label)}</span></div>` +
          '<span class="mono tabular" style="color:var(--muted-foreground);' +
          `flex-shrink:0;">${((d.value / total) * 100).toFixed(1)}%</span></div>`;
    }).join('');

    const centre = METRICS[vis.metric] && METRICS[vis.metric].fmt === 'count' ?
        `<text x="${cx}" y="${cy - 2}" text-anchor="middle" ` +
            'fill="var(--foreground)" font-size="13" font-weight="700">' +
            `${escHtml(fmtCount(total))}</text><text x="${cx}" y="${cy + 13}" ` +
            'text-anchor="middle" fill="var(--muted-foreground)" ' +
            'font-size="9">Total</text>' :
        '';
    container.innerHTML = '<div class="donut-wrap"><svg viewBox="0 0 164 164" ' +
        'style="width:160px;height:160px;flex-shrink:0;margin:0 auto;" ' +
        `role="img">${slices}${centre}</svg>` +
        `<div class="donut-legend">${legend}</div></div>`;
    bindSeriesEvents(container.querySelectorAll('.chart-slice'), items, vis);
    bindSeriesEvents(container.querySelectorAll('.donut-legend-row'), items, vis);
  }

  function renderTableVisual(container, rows, vis) {
    const errorTypes = new Set(['LLM_ERROR', 'TOOL_ERROR']);
    const callTypes = new Set(
        ['LLM_RESPONSE', 'LLM_ERROR', 'TOOL_COMPLETED', 'TOOL_ERROR']);
    let list = rows.filter((r) => (vis.errorsOnly ? errorTypes : callTypes)
                                      .has(r.event_type));
    if (vis.errorsOnly) {
      list.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    } else {
      list = list.filter((r) => isNum(r.total_ms));
      list.sort((a, b) => b.total_ms - a.total_ms);
    }
    const slice = list.slice(0, 12);
    if (!slice.length) {
      container.innerHTML = emptyStateHtml();
      return;
    }
    const detail = vis.errorsOnly ? 'Error' : 'Tool / model';
    container.innerHTML = '<div class="data-table-wrap custom-scroll">' +
        '<table class="data-table"><thead><tr><th>Agent</th><th>Event</th>' +
        `<th>${detail}</th><th class="text-right">Latency</th></tr></thead>` +
        '<tbody>' +
        slice.map((r) => {
          const info = vis.errorsOnly ? r.error_message : (r.tool || r.model);
          return `<tr style="cursor:pointer;" data-agent="${escAttr(r.agent)}">` +
              `<td class="mono truncate" style="max-width:130px;" title="${
                  escAttr(r.agent)}">${escHtml(r.agent)}</td>` +
              `<td>${escHtml(r.event_type)}</td>` +
              `<td class="truncate" style="max-width:200px;" title="${
                  escAttr(info)}">${escHtml(info)}</td>` +
              `<td class="text-right mono tabular">${escHtml(fmtMs(r.total_ms))}</td>` +
              '</tr>';
        }).join('') +
        '</tbody></table></div>';
    container.querySelectorAll('tr[data-agent]').forEach((tr) => {
      tr.addEventListener('click', () => toggleCrossFilter('agent', tr.dataset.agent));
    });
  }

  function renderVisualInto(container, vis) {
    if (vis.type === 'table') {
      renderTableVisual(container, filterRows(), vis);
      return;
    }
    // A visual ignores its own cross-filter so the other members stay visible.
    const rows = filterRows({ignoreField: vis.dim});
    const series = aggregateBy(rows, vis.dim, vis.metric, vis.secondaryMetric);
    if (vis.type === 'line') renderLineChart(container, series, vis);
    else if (vis.type === 'donut') renderDonutChart(container, series, vis);
    else if (vis.type === 'hbar') renderHBarChart(container, series, vis);
    else renderBarChart(container, series, vis);
  }

  // ---------------------------------------------------------------------------
  // Page rendering
  // ---------------------------------------------------------------------------
  function activePage() {
    return state.pages.find((p) => p.id === state.activePageId) || state.pages[0];
  }

  function changeText(metric, current, previous) {
    if (!isNum(current) || !isNum(previous)) return {text: '--', tone: 'neutral'};
    const kind = METRICS[metric].fmt;
    let delta;
    let text;
    if (kind === 'pct') {
      delta = current - previous;
      text = `${delta >= 0 ? '+' : ''}${delta.toFixed(1)} pts`;
    } else {
      if (previous === 0) return {text: '--', tone: 'neutral'};
      delta = ((current - previous) / previous) * 100;
      text = `${delta >= 0 ? '+' : ''}${delta.toFixed(1)}%`;
    }
    const better = METRICS[metric].better;
    let tone = 'neutral';
    if (better && Math.abs(delta) >= 0.05) {
      tone = (delta > 0) === (better === 'up') ? 'good' : 'bad';
    }
    return {text, tone};
  }

  function buildNarrative(s, rows) {
    const filters = Object.entries(state.crossFilters);
    const scope = filters.length ?
        ` (filtered to ${filters.map(([k, v]) => `<b>${escHtml(DIMENSIONS[k])} = ${
            escHtml(v)}</b>`).join(', ')})` :
        '';
    const busiest = aggregateBy(rows, 'agent', 'llm_calls')[0];
    const slowest = aggregateBy(rows, 'model', 'p95_llm_latency')[0];
    let text = `Over the last <b>${state.slicers.days} days</b>${scope}, agents ` +
        `handled <b>${escHtml(fmtCount(s.sessions))}</b> sessions from ` +
        `<b>${escHtml(fmtCount(s.users))}</b> users, with ` +
        `<b>${escHtml(fmtCount(s.llm_calls))}</b> LLM calls and ` +
        `<b>${escHtml(fmtCount(s.tool_calls))}</b> tool calls. ` +
        `P95 LLM latency is <b>${escHtml(fmtMs(s.p95_llm_latency))}</b> ` +
        `(average time to first token ${escHtml(fmtMs(s.avg_ttft))}), and ` +
        `<b>${escHtml(fmtPct(s.error_rate))}</b> of LLM and tool calls failed. ` +
        `The prompt cache served <b>${escHtml(fmtPct(s.cache_hit_rate))}</b> ` +
        `of prompt tokens.`;
    if (busiest) {
      text += ` The busiest agent is <b>${escHtml(busiest.label)}</b> (${
          escHtml(fmtCount(busiest.value))} LLM calls)`;
      text += slowest ? `, and <b>${escHtml(slowest.label)}</b> has the ` +
              `highest P95 latency (${escHtml(fmtMs(slowest.value))}).` :
                        '.';
    }
    return text;
  }

  function renderAll() {
    const page = activePage();
    const rows = filterRows();
    const summary = summarize(rows);
    const previous = summarize(filterRows({previousWindow: true}));

    const filterKeys = Object.keys(state.crossFilters);
    const filterBar = document.getElementById('filterBar');
    filterBar.classList.toggle('hidden', !filterKeys.length);
    document.getElementById('filterPills').innerHTML = filterKeys.map((k) =>
        `<span class="filter-pill"><span style="color:var(--muted-foreground);">` +
        `${escHtml(DIMENSIONS[k])}:</span><span style="font-weight:500;">${
            escHtml(state.crossFilters[k])}</span>` +
        `<button type="button" data-rm-filter="${escAttr(k)}" aria-label="Remove ${
            escAttr(DIMENSIONS[k])} filter">×</button></span>`).join('');
    document.querySelectorAll('[data-rm-filter]').forEach((btn) => {
      btn.addEventListener('click', () => {
        delete state.crossFilters[btn.dataset.rmFilter];
        renderAll();
      });
    });

    document.getElementById('pageTitle').textContent = page.title;
    document.getElementById('pageSubtitle').textContent = page.subtitle;
    document.getElementById('smartNarrative').innerHTML = buildNarrative(summary, rows);

    const toneColor = {
      good: 'var(--success)', bad: 'var(--error)', neutral: 'var(--muted-foreground)',
    };
    document.getElementById('kpiGrid').innerHTML = page.kpis.map((k) => {
      const change = changeText(k, summary[k], previous[k]);
      const sub = KPI_SUBTITLES[k](summary);
      return '<div class="kpi-card"><div class="kpi-top">' +
          `<span class="kpi-label truncate">${escHtml(metricLabel(k))}</span>` +
          `<span class="mono" title="Change from the previous ${
              state.slicers.days} days" style="font-weight:600;color:${
              toneColor[change.tone]};flex-shrink:0;">${escHtml(change.text)}</span>` +
          `</div><div class="kpi-value tabular">${
              escHtml(fmtMetric(k, summary[k]))}</div>` +
          `<div class="kpi-sub truncate" title="${escAttr(sub)}">${
              escHtml(sub)}</div></div>`;
    }).join('');

    const grid = document.getElementById('visualsGrid');
    grid.innerHTML = '';
    page.visuals.forEach((vis, index) => {
      const card = document.createElement('div');
      const span = vis.span >= 12 ? 'span-12' : vis.span >= 7 ? 'span-7' :
          vis.span === 5 ? 'span-5' : 'span-6';
      card.className = `vis-card ${span}${vis.isNew ? ' widget-highlight' : ''}`;
      delete vis.isNew;
      const types = vis.type === 'table' ? ['table'] :
                                           ['bar', 'hbar', 'line', 'donut'];
      card.innerHTML = '<div class="vis-header">' +
          `<h3 class="vis-title truncate" title="${escAttr(vis.title)}">${
              escHtml(vis.title)}</h3><div class="vis-tools">` +
          `<select class="vis-type-select" aria-label="Chart type">${
              types.map((t) => `<option value="${t}"${
                  vis.type === t ? ' selected' : ''}>${t.toUpperCase()}</option>`)
                  .join('')}</select>` +
          '<button type="button" class="btn btn-sm btn-ghost" data-focus ' +
          'title="Focus" aria-label="Focus">⤢</button>' +
          '<button type="button" class="btn btn-sm btn-ghost" data-remove ' +
          'title="Remove" aria-label="Remove">×</button></div></div>' +
          '<div class="vis-body"></div>';
      grid.appendChild(card);
      renderVisualInto(card.querySelector('.vis-body'), vis);

      card.querySelector('select').addEventListener('change', (e) => {
        vis.type = e.target.value;
        renderAll();
      });
      card.querySelector('[data-remove]').addEventListener('click', () => {
        page.visuals.splice(index, 1);
        renderAll();
      });
      card.querySelector('[data-focus]').addEventListener('click', () => {
        document.getElementById('focusTitle').textContent = vis.title;
        const body = document.getElementById('focusBody');
        body.innerHTML = '';
        renderVisualInto(body, vis);
        document.getElementById('focusModal').classList.remove('hidden');
      });
    });

    const tabs = document.getElementById('pageTabs');
    tabs.innerHTML = state.pages.map((p) =>
        `<div class="page-tab${p.id === page.id ? ' active' : ''}" data-tab="${
            escAttr(p.id)}"><span class="truncate" style="max-width:170px;">${
            escHtml(p.title)}</span>${state.pages.length > 1 ?
            `<button type="button" class="page-tab-close" data-close-tab="${
                escAttr(p.id)}" aria-label="Close page">×</button>` :
            ''}</div>`).join('');
    tabs.querySelectorAll('[data-tab]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.dataset.closeTab) return;
        state.activePageId = el.dataset.tab;
        renderAll();
      });
    });
    tabs.querySelectorAll('[data-close-tab]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.dataset.closeTab;
        state.pages = state.pages.filter((p) => p.id !== id);
        if (state.activePageId === id) state.activePageId = state.pages[0].id;
        renderAll();
      });
    });

    if (state.activeView === 'table') renderExplorer();
  }

  // ---------------------------------------------------------------------------
  // Explorer, schema, and slicer options
  // ---------------------------------------------------------------------------
  const EXPLORER_COLUMNS = [
    'timestamp', 'event_type', 'agent', 'model', 'tool', 'user_id', 'session_id',
    'total_ms', 'ttft_ms', 'total_tokens', 'status', 'error_message',
  ];

  function renderExplorer() {
    const query = document.getElementById('tableSearch').value.trim().toLowerCase();
    let rows = filterRows();
    if (query) {
      rows = rows.filter((r) => EXPLORER_COLUMNS.some(
          (c) => String(r[c] ?? '').toLowerCase().includes(query)));
    }
    document.getElementById('explorerHead').innerHTML = '<tr>' +
        EXPLORER_COLUMNS.map((c) => `<th style="white-space:nowrap;">${c}</th>`)
            .join('') +
        '</tr>';
    const slice = rows.slice(0, 150);
    document.getElementById('explorerBody').innerHTML = slice.map((r) => '<tr>' +
        EXPLORER_COLUMNS.map((c) => `<td class="mono truncate" style="max-width:220px;" ` +
            `title="${escAttr(r[c])}">${escHtml(r[c])}</td>`).join('') +
        '</tr>').join('');
    document.getElementById('explorerFoot').innerHTML =
        `<span>Showing ${slice.length} of ${rows.length} filtered rows</span>` +
        '<span>Source: agent_events</span>';
  }

  function renderSchema() {
    const columns = [
      ['timestamp', 'TIMESTAMP, required'],
      ['event_type', 'STRING, e.g. LLM_RESPONSE'],
      ['agent', 'STRING'],
      ['session_id', 'STRING'],
      ['invocation_id', 'STRING'],
      ['user_id', 'STRING'],
      ['trace_id', 'STRING'],
      ['span_id', 'STRING'],
      ['parent_span_id', 'STRING'],
      ['content', 'JSON: tool, args, response'],
      ['content_parts', 'JSON'],
      ['attributes', 'JSON: model, usage_metadata'],
      ['latency_ms', 'JSON: total_ms, time_to_first_token_ms'],
      ['status', 'STRING'],
      ['error_message', 'STRING'],
      ['is_truncated', 'BOOL'],
    ];
    const item = ([name, desc]) => '<div class="schema-col-item">' +
        `<span class="mono truncate">${escHtml(name)}</span>` +
        '<span class="mono" style="color:var(--muted-foreground);flex-shrink:0;">' +
        `${escHtml(desc)}</span></div>`;
    document.getElementById('schemaEventsList').innerHTML = columns.map(item).join('');
    document.getElementById('metricDefsList').innerHTML = Object.entries(METRICS)
        .map(([id, m]) => '<div class="schema-col-item" style="flex-direction:column;' +
            `align-items:flex-start;"><span class="mono">${escHtml(id)}</span>` +
            '<span class="mono" style="color:var(--muted-foreground);' +
            `white-space:normal;">${escHtml(m.sql)}</span></div>`)
        .join('');
  }

  function fillSelect(id, values, selected) {
    const select = document.getElementById(id);
    select.innerHTML = '<option value="ALL">All</option>' +
        values.map((v) => `<option value="${escAttr(v)}">${escHtml(v)}</option>`)
            .join('');
    select.value = values.includes(selected) ? selected : 'ALL';
    return select.value;
  }

  function refreshSlicerOptions() {
    state.slicers.agent =
        fillSelect('slicerAgent', distinctValues('agent'), state.slicers.agent);
    state.slicers.model =
        fillSelect('slicerModel', distinctValues('model'), state.slicers.model);
  }

  // ---------------------------------------------------------------------------
  // Copilot chat
  // ---------------------------------------------------------------------------
  const STARTER_PROMPTS = [
    {label: 'Latency', prompt: 'Show p95 LLM latency and time to first token by model'},
    {label: 'Tokens and caching', prompt: 'Token usage and cache hit rate by agent'},
    {label: 'Tool errors', prompt: 'Which tools fail most, with recent error messages'},
    {label: 'Tool usage', prompt: 'Tool usage: tool calls by agent and the tool mix'},
    {label: 'Last 7 days', prompt: 'Overview of sessions and users for the last 7 days'},
  ];

  function appendChatMessage(role, html, steps) {
    const box = document.getElementById('chatMessages');
    const row = document.createElement('div');
    row.className = `msg-row ${role}`;
    const bubble = document.createElement('div');
    bubble.className = 'msg-bubble';
    if (role === 'user') {
      bubble.textContent = html;
    } else {
      bubble.innerHTML = html + (steps && steps.length ?
          '<details class="msg-steps"><summary style="cursor:pointer;">How this ' +
              `page was built (${steps.length} steps)</summary><ul>${
                  steps.map((s) => `<li>${escHtml(s)}</li>`).join('')}</ul></details>` :
          '');
    }
    row.appendChild(bubble);
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  }

  function applyPlanFilters(plan) {
    if (plan.days) {
      state.slicers.days = plan.days;
      document.getElementById('slicerDate').value = String(plan.days);
    }
    for (const [field, value] of Object.entries(plan.filters || {})) {
      if (field === 'agent' || field === 'model') {
        state.slicers[field] = value;
      } else {
        state.crossFilters[field] = value;
      }
    }
    refreshSlicerOptions();
  }

  function handlePrompt(text) {
    const prompt = text.trim();
    if (!prompt) return;
    appendChatMessage('user', prompt);
    const plan = compilePrompt(prompt);
    applyPlanFilters(plan);
    if (plan.action === 'create_page') {
      state.pages.push(plan.page);
      state.activePageId = plan.page.id;
    } else if (plan.action === 'add_visual') {
      activePage().visuals.unshift(plan.visual);
    } else if (plan.action === 'switch_chart') {
      activePage().visuals.forEach((v) => {
        if (v.type !== 'table') v.type = plan.chartType;
      });
    }
    switchView('report');
    appendChatMessage('agent', plan.reply, plan.steps);
    renderAll();
  }

  // ---------------------------------------------------------------------------
  // Import
  // ---------------------------------------------------------------------------
  function parseJsonColumn(value) {
    if (value === null || value === undefined || value === '') return {};
    if (typeof value === 'object') return value;
    if (typeof value === 'number') return {total_ms: value};
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function normalizeRow(r, index) {
    let ts = r.timestamp;
    if (ts === null || ts === undefined || ts === '') return null;
    // The BigQuery REST API returns TIMESTAMP values as epoch seconds.
    if (/^\d+(\.\d+)?$/.test(String(ts))) ts = new Date(Number(ts) * 1000).toISOString();
    const ms = Date.parse(ts);
    if (Number.isNaN(ms)) return null;
    const iso = new Date(ms).toISOString();
    const latency = parseJsonColumn(r.latency_ms);
    const attributes = parseJsonColumn(r.attributes);
    const usage = attributes.usage_metadata || {};
    const content = parseJsonColumn(r.content);
    const text = (v) => (v === null || v === undefined ? '' : String(v));
    return {
      timestamp: iso,
      event_date: iso.slice(0, 10),
      event_type: text(r.event_type) || 'UNKNOWN',
      agent: text(r.agent),
      model: text(r.model || attributes.model || attributes.model_version),
      tool: text(r.tool || content.tool || content.tool_name),
      user_id: text(r.user_id),
      session_id: text(r.session_id),
      invocation_id: text(r.invocation_id),
      trace_id: text(r.trace_id || r.invocation_id),
      span_id: text(r.span_id) || `row-${index}`,
      status: (text(r.status) || 'OK').toUpperCase(),
      error_message: text(r.error_message),
      is_truncated: r.is_truncated === true || r.is_truncated === 'true',
      total_ms: toNumber(r.total_ms ?? latency.total_ms),
      ttft_ms: toNumber(r.ttft_ms ?? latency.time_to_first_token_ms),
      prompt_tokens: toNumber(r.prompt_tokens ?? usage.prompt_token_count),
      cached_tokens: toNumber(r.cached_tokens ?? usage.cached_content_token_count),
      candidates_tokens:
          toNumber(r.candidates_tokens ?? usage.candidates_token_count),
      thoughts_tokens: toNumber(r.thoughts_tokens ?? usage.thoughts_token_count),
      total_tokens: toNumber(r.total_tokens ?? usage.total_token_count),
    };
  }

  function parseImport(raw) {
    const text = raw.replace(/^\uFEFF/, '').trim();
    if (!text) throw new Error('Paste rows or choose a file first.');
    let records;
    if (text.startsWith('[')) {
      records = JSON.parse(text);
    } else if (text.startsWith('{')) {
      records = text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
    } else {
      const table = parseCsv(text);
      const header = (table.shift() || []).map((h) => h.trim());
      records = table.map((cells) => Object.fromEntries(
          header.map((h, i) => [h, cells[i] ?? ''])));
    }
    if (!Array.isArray(records) || !records.length) throw new Error('No rows found.');
    const rows = records.map(normalizeRow).filter(Boolean);
    if (!rows.length) {
      throw new Error('No row has a parseable timestamp column.');
    }
    rows.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    return {rows, skipped: records.length - rows.length};
  }

  // ---------------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------------
  function switchView(view) {
    state.activeView = view;
    for (const name of ['report', 'table', 'model']) {
      document.getElementById(`view-${name}`).classList.toggle('hidden', name !== view);
    }
    document.querySelectorAll('.rail-btn[data-view]').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === view);
    });
    if (view === 'table') renderExplorer();
  }

  function download(name, type, content) {
    const url = URL.createObjectURL(new Blob([content], {type}));
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function refreshSql() {
    const input = document.getElementById('tableIdInput');
    const error = document.getElementById('tableIdError');
    const parts = parseTableId(input.value);
    if (!parts) {
      error.textContent = 'Use project.dataset.table, for example ' +
          'my-project.my_dataset.agent_events.';
      return;
    }
    error.textContent = '';
    state.tableId = parts.join('.');
    document.getElementById('sqlModalCode').textContent =
        generatePageSql(activePage(), parts);
  }

  const $ = (id) => document.getElementById(id);

  document.querySelectorAll('.rail-btn[data-view]').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });
  $('btnTheme').addEventListener('click', () => {
    currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', currentTheme);
  });
  $('slicerDate').addEventListener('change', (e) => {
    state.slicers.days = Number(e.target.value);
    renderAll();
  });
  $('slicerAgent').addEventListener('change', (e) => {
    state.slicers.agent = e.target.value;
    renderAll();
  });
  $('slicerModel').addEventListener('change', (e) => {
    state.slicers.model = e.target.value;
    renderAll();
  });
  $('btnClearFilters').addEventListener('click', () => {
    state.crossFilters = {};
    renderAll();
  });
  $('btnResetPage').addEventListener('click', () => {
    state.crossFilters = {};
    state.slicers = {days: 30, agent: 'ALL', model: 'ALL'};
    $('slicerDate').value = '30';
    refreshSlicerOptions();
    renderAll();
  });
  $('btnDuplicatePage').addEventListener('click', () => {
    const copy = JSON.parse(JSON.stringify(activePage()));
    copy.id = 'page-' + Date.now().toString(36);
    copy.title += ' (copy)';
    state.pages.push(copy);
    state.activePageId = copy.id;
    renderAll();
  });
  $('btnToggleCopilot').addEventListener('click', () => {
    $('copilotPane').classList.toggle('hidden');
  });
  $('btnAddBlankPromptPage').addEventListener('click', () => {
    $('copilotPane').classList.remove('hidden');
    $('promptInput').focus();
  });
  $('modeNewPage').addEventListener('click', () => {
    state.copilotMode = 'new';
    $('modeNewPage').classList.add('active');
    $('modeModifyPage').classList.remove('active');
  });
  $('modeModifyPage').addEventListener('click', () => {
    state.copilotMode = 'modify';
    $('modeModifyPage').classList.add('active');
    $('modeNewPage').classList.remove('active');
  });
  $('promptForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const value = $('promptInput').value;
    $('promptInput').value = '';
    handlePrompt(value);
  });
  $('promptInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('promptForm').requestSubmit();
    }
  });
  $('btnClearChat').addEventListener('click', () => {
    $('chatMessages').innerHTML = '';
  });

  $('btnShowSql').addEventListener('click', () => {
    $('tableIdInput').value = state.tableId;
    refreshSql();
    $('sqlModal').classList.remove('hidden');
  });
  $('tableIdInput').addEventListener('input', refreshSql);
  $('btnCloseSql').addEventListener('click', () => $('sqlModal').classList.add('hidden'));
  $('btnCopySql').addEventListener('click', () => {
    if (navigator.clipboard) navigator.clipboard.writeText($('sqlModalCode').textContent);
    $('btnCopySql').textContent = 'Copied';
    setTimeout(() => {
      $('btnCopySql').textContent = 'Copy SQL';
    }, 1200);
  });

  $('btnImport').addEventListener('click', () => $('importModal').classList.remove('hidden'));
  $('btnCloseImport').addEventListener('click', () => $('importModal').classList.add('hidden'));
  $('importFile').addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $('importText').value = reader.result;
    };
    reader.readAsText(file);
  });
  $('btnApplyImport').addEventListener('click', () => {
    const status = $('importStatus');
    try {
      const {rows, skipped} = parseImport($('importText').value);
      state.rows = rows;
      state.crossFilters = {};
      refreshSlicerOptions();
      $('dataSourceBadge').textContent = `agent_events (imported, ${rows.length} rows)`;
      status.style.color = 'var(--success)';
      status.textContent = `Loaded ${rows.length} rows` +
          (skipped ? `; skipped ${skipped} without a parseable timestamp.` : '.');
      renderAll();
    } catch (err) {
      status.style.color = 'var(--error)';
      status.textContent = `Could not import: ${err.message}`;
    }
  });
  $('btnResetSample').addEventListener('click', () => {
    state.rows = SAMPLE_ROWS;
    state.crossFilters = {};
    refreshSlicerOptions();
    $('dataSourceBadge').textContent = SAMPLE_LABEL;
    $('importStatus').style.color = 'var(--muted-foreground)';
    $('importStatus').textContent = 'Restored the synthetic sample.';
    renderAll();
  });
  $('btnCloseFocus').addEventListener('click', () => $('focusModal').classList.add('hidden'));

  $('tableSearch').addEventListener('input', renderExplorer);
  $('btnExportCsv').addEventListener('click', () => {
    const rows = filterRows();
    if (!rows.length) return;
    const columns = Object.keys(rows[0]);
    const csv = [
      columns.map(csvCell).join(','),
      ...rows.map((r) => columns.map((c) => csvCell(r[c])).join(',')),
    ].join('\r\n');
    download('agent_events_export.csv', 'text/csv', csv);
  });
  $('btnExportSpec').addEventListener('click', () => {
    download('bqaa_copilot_dashboard.json', 'application/json', JSON.stringify(
        {pages: state.pages, slicers: state.slicers, crossFilters: state.crossFilters},
        null, 2));
  });

  const chips = $('promptChips');
  chips.innerHTML = STARTER_PROMPTS.map((p, i) =>
      `<button type="button" class="chip-btn" data-chip="${i}">${escHtml(p.label)}</button>`)
      .join('');
  chips.querySelectorAll('[data-chip]').forEach((btn) => {
    btn.addEventListener('click', () => handlePrompt(STARTER_PROMPTS[Number(btn.dataset.chip)].prompt));
  });

  refreshSlicerOptions();
  renderSchema();
  appendChatMessage('agent', 'Describe the dashboard you want, for example ' +
      '<i>"p95 LLM latency by model for the last 7 days"</i> or <i>"which tools ' +
      'fail most"</i>. Prompts are matched to page templates with keyword rules ' +
      'in this browser tab; nothing is sent to a server. Use <b>Import data</b> to ' +
      'load your own <code class="mono">agent_events</code> rows.');
  renderAll();
})();
