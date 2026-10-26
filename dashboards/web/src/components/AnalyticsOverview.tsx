/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Clock3,
  DollarSign,
  Gauge,
  MessagesSquare,
  ShieldAlert,
  Sparkles,
  TimerReset,
  Users,
  Wrench,
} from 'lucide-react';
import { cn, formatCompactNumber, formatCurrency } from '../lib/utils';
import { useDashboardFilters } from '../hooks/useDashboardFilters';
import { useDashboardHealth } from '../hooks/useDashboardHealth';
import { fetchAgentData, isBigQueryAuthError } from '../services/apiService';

type DashboardRow = {
  timestamp: string;
  total_tokens?: number;
  session_id?: string;
  user_id?: string;
  trace_id?: string;
  span_id?: string;
  agent?: string;
  agent_id?: string;
  status?: string;
  event_type?: string;
  latency?: number;
  content?: {
    tool?: string;
    tool_name?: string;
  };
  attributes?: Record<string, unknown>;
  [key: string]: unknown;
};

type RankRow = { name: string; value: number };
type Quantiles = { p50: number; p90: number; p99: number };

export function AnalyticsOverview() {
  const { filters } = useDashboardFilters();
  const { ready: authReady, loading: healthLoading, missing: missingAuth } = useDashboardHealth();
  const [rows, setRows] = useState<DashboardRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const sourceReady = Boolean(filters.projectId && filters.datasetId && filters.tableId && authReady);

  useEffect(() => {
    const loadData = async () => {
      if (healthLoading) {
        return;
      }

      if (!sourceReady) {
        setRows([]);
        setError('');
        setLoading(false);
        return;
      }

      setLoading(true);
      setError('');

      try {
        const data = await fetchAgentData(filters.timespan, filters);
        setRows(Array.isArray(data) ? data : []);
      } catch (loadError) {
        if (!isBigQueryAuthError(loadError)) {
          console.error('Analytics load failed:', loadError);
        }
        setError(loadError instanceof Error ? loadError.message : 'Failed to load dashboard data');
        setRows([]);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [filters.timespan, filters.agentId, filters.userId, filters.traceId, filters.spanId, filters.projectId, filters.datasetId, filters.tableId, sourceReady, healthLoading]);

  const filteredRows = useMemo(() => applyFilters(rows, filters), [rows, filters]);
  const bucketSize = useMemo(() => timeBucketForSpan(filters.timespan), [filters.timespan]);

  const totals = useMemo(() => {
    const totalTokens = sumBy(filteredRows, (row) => Number(row.total_tokens || 0));

    return {
      totalTokens,
      totalCost: totalTokens * 0.000002,
      totalTraces: uniqueCount(filteredRows, (row) => row.trace_id || row.id),
      totalSessions: uniqueCount(filteredRows, (row) => row.session_id),
      totalUsers: uniqueCount(filteredRows, (row) => row.user_id),
      totalTools: filteredRows.filter(isToolEvent).length,
      totalLlmCalls: filteredRows.filter(isLlmRequest).length,
      totalToolErrors: filteredRows.filter(isToolError).length,
    };
  }, [filteredRows]);

  const tokenTrend = useMemo(() => buildSeries(filteredRows, bucketSize, (row) => Number(row.total_tokens || 0)), [filteredRows, bucketSize]);
  const traceTrend = useMemo(() => buildDistinctSeries(filteredRows, bucketSize, (row) => row.trace_id || row.id), [filteredRows, bucketSize]);
  const sessionTrend = useMemo(() => buildDistinctSeries(filteredRows, bucketSize, (row) => row.session_id), [filteredRows, bucketSize]);
  const toolTrend = useMemo(() => buildCountSeries(filteredRows, bucketSize, isToolEvent), [filteredRows, bucketSize]);
  const llmTrend = useMemo(() => buildCountSeries(filteredRows, bucketSize, isLlmRequest), [filteredRows, bucketSize]);
  const userTrend = useMemo(() => buildDistinctSeries(filteredRows, bucketSize, (row) => row.user_id), [filteredRows, bucketSize]);
  const errorTrend = useMemo(() => buildCountSeries(filteredRows, bucketSize, isToolError), [filteredRows, bucketSize]);
  const sessionLengthTrend = useMemo(() => buildSessionLengthTrend(filteredRows, bucketSize), [filteredRows, bucketSize]);

  const topAgentsByTokens = useMemo(() => rankBy(filteredRows, (row) => row.agent || row.agent_id || 'unknown', (row) => Number(row.total_tokens || 0)), [filteredRows]);
  const topUsersByTokens = useMemo(() => rankBy(filteredRows, (row) => row.user_id || 'unknown', (row) => Number(row.total_tokens || 0)), [filteredRows]);
  const topAgentsByTraces = useMemo(() => rankBy(filteredRows, (row) => row.agent || row.agent_id || 'unknown', () => 1), [filteredRows]);
  const topAgentsBySessions = useMemo(() => rankBy(filteredRows, (row) => row.agent || row.agent_id || 'unknown', () => 1, (row) => row.session_id), [filteredRows]);
  const topTools = useMemo(() => rankBy(filteredRows.filter(isToolEvent), (row) => String(row.content?.tool || row.content?.tool_name || row.attributes?.tool || 'unknown'), () => 1), [filteredRows]);
  const topAgentsByTools = useMemo(() => rankBy(filteredRows.filter(isToolEvent), (row) => row.agent || row.agent_id || 'unknown', () => 1), [filteredRows]);
  const topAgentsByLlm = useMemo(() => rankBy(filteredRows.filter(isLlmRequest), (row) => row.agent || row.agent_id || 'unknown', () => 1), [filteredRows]);
  const topUsersBySessions = useMemo(() => rankBy(filteredRows, (row) => row.user_id || 'unknown', () => 1, (row) => row.session_id), [filteredRows]);
  const topAgentsByUsers = useMemo(() => rankBy(filteredRows, (row) => row.agent || row.agent_id || 'unknown', () => 1, (row) => row.user_id), [filteredRows]);
  const topErrorAgents = useMemo(() => rankBy(filteredRows.filter(isToolError), (row) => row.agent || row.agent_id || 'unknown', () => 1), [filteredRows]);
  const topErrorTools = useMemo(() => rankBy(filteredRows.filter(isToolError), (row) => String(row.content?.tool || row.content?.tool_name || row.attributes?.tool || 'unknown'), () => 1), [filteredRows]);

  const toolLatency = useMemo(() => latencyStats(filteredRows.filter((row) => String(row.event_type || '').toUpperCase().includes('TOOL_COMPLETED'))), [filteredRows]);
  const llmLatency = useMemo(() => latencyStats(filteredRows.filter((row) => String(row.event_type || '').toUpperCase().includes('LLM_RESPONSE'))), [filteredRows]);
  const sessionLatency = useMemo(() => latencyStats(filteredRows.filter((row) => String(row.event_type || '').toUpperCase().includes('SESSION'))), [filteredRows]);
  const sessionLengthStatsValue = useMemo(() => sessionLengthStats(filteredRows), [filteredRows]);

  if (!sourceReady) {
    return (
      <div className="px-6 pb-10">
        <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950/30 p-8 text-center text-zinc-400">
          <p className="text-sm font-semibold text-white">
            {!authReady ? 'Connect backend BigQuery auth to load analytics' : 'Connect a BigQuery source to load analytics'}
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            {!authReady
              ? 'Set GOOGLE_APPLICATION_CREDENTIALS or GCP_CLIENT_EMAIL and GCP_PRIVATE_KEY on the dashboard backend.'
              : 'Enter Project ID, Dataset ID, and Table ID in the command bar above. The new analytics sections will refresh automatically.'}
          </p>
          {!authReady && missingAuth.length > 0 && (
            <div className="mt-4 text-xs text-zinc-400">
              <div className="font-medium text-zinc-200">Missing backend variables:</div>
              <ul className="mt-2 space-y-1 list-disc list-inside">
                {missingAuth.map((missing) => (
                  <li key={missing} className="text-zinc-400">{missing}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-6 p-6">
        <div className="grid grid-cols-12 gap-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index} className="col-span-12 md:col-span-6 xl:col-span-3 h-28 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/50" />
          ))}
        </div>
        <div className="h-96 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/20" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="px-6 pb-10">
        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-6 text-sm text-red-200">
          <p className="font-semibold">Unable to load analytics</p>
          <p className="mt-2 text-red-200/80">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="grid grid-cols-12 gap-4">
        <MetricCard label="Tokens" value={formatCompactNumber(totals.totalTokens)} icon={<Sparkles size={16} />} />
        <MetricCard label="Cost" value={formatCurrency(totals.totalCost)} icon={<DollarSign size={16} />} accent="text-emerald-500" />
        <MetricCard label="Traces" value={formatCompactNumber(totals.totalTraces)} icon={<Gauge size={16} />} />
        <MetricCard label="Sessions" value={formatCompactNumber(totals.totalSessions)} icon={<TimerReset size={16} />} />
        <MetricCard label="Users" value={formatCompactNumber(totals.totalUsers)} icon={<Users size={16} />} />
        <MetricCard label="Tool Calls" value={formatCompactNumber(totals.totalTools)} icon={<Wrench size={16} />} />
        <MetricCard label="LLM Calls" value={formatCompactNumber(totals.totalLlmCalls)} icon={<MessagesSquare size={16} />} />
        <MetricCard label="Tool Errors" value={formatCompactNumber(totals.totalToolErrors)} icon={<ShieldAlert size={16} />} accent="text-red-400" />
      </div>

      <AnalyticsSection
        title="Costs"
        subtitle="Token consumption by time, agents, and users"
        primary={<AreaPanel data={tokenTrend} valueKey="value" label="Tokens" color="#ef4444" />}
        sidePanels={[
          <RankingPanel title="Top 5 agents" rows={topAgentsByTokens} metricLabel="tokens" />,
          <RankingPanel title="Top 5 users" rows={topUsersByTokens} metricLabel="tokens" />,
        ]}
      />

      <AnalyticsSection
        title="Agent Usage"
        subtitle="Trace and session volume by period"
        primary={<LinePanel data={mergeSeries(traceTrend, sessionTrend)} firstKey="value" secondKey="value2" firstLabel="Traces" secondLabel="Sessions" firstColor="#3b82f6" secondColor="#22c55e" />}
        sidePanels={[
          <RankingPanel title="Top 5 agents by traces" rows={topAgentsByTraces} metricLabel="traces" />,
        ]}
      />

      <AnalyticsSection
        title="Tool Usage"
        subtitle="Tool calls over time and the busiest tools / agents"
        primary={<LinePanel data={toolTrend} firstKey="value" firstLabel="Tool calls" firstColor="#f59e0b" />}
        sidePanels={[
          <RankingPanel title="Top 5 tools" rows={topTools} metricLabel="calls" />,
          <RankingPanel title="Top 5 agents by tools" rows={topAgentsByTools} metricLabel="calls" />,
        ]}
      />

      <AnalyticsSection
        title="LLM Usage"
        subtitle="LLM request volume and the agents driving it"
        primary={<LinePanel data={llmTrend} firstKey="value" firstLabel="LLM calls" firstColor="#a855f7" />}
        sidePanels={[
          <RankingPanel title="Top 5 agents by LLM calls" rows={topAgentsByLlm} metricLabel="calls" />,
        ]}
      />

      <AnalyticsSection
        title="Users"
        subtitle="User reach and session concentration"
        primary={<LinePanel data={userTrend} firstKey="value" firstLabel="Users" firstColor="#14b8a6" />}
        sidePanels={[
          <RankingPanel title="Top 5 users by sessions" rows={topUsersBySessions} metricLabel="sessions" />,
          <RankingPanel title="Top 5 agents by users" rows={topAgentsByUsers} metricLabel="users" />,
        ]}
      />

      <AnalyticsSection
        title="Performance"
        subtitle="P50, P90, and P99 latency for tool, LLM, and session completion"
        primary={<LatencyGrid toolLatency={toolLatency} llmLatency={llmLatency} sessionLatency={sessionLatency} />}
        sidePanels={[]}
      />

      <AnalyticsSection
        title="Errors"
        subtitle="Tool errors by agent, tool, and period"
        primary={<LinePanel data={errorTrend} firstKey="value" firstLabel="Tool errors" firstColor="#ef4444" />}
        sidePanels={[
          <RankingPanel title="Top 5 agents with errors" rows={topErrorAgents} metricLabel="errors" />,
          <RankingPanel title="Top 5 tools with errors" rows={topErrorTools} metricLabel="errors" />,
        ]}
      />

      <AnalyticsSection
        title="Sessions"
        subtitle="Session volume and duration distribution"
        primary={<SessionPanel stats={sessionLengthStatsValue} data={sessionLengthTrend} />}
        sidePanels={[
          <RankingPanel title="Top 5 agents by sessions" rows={topAgentsBySessions} metricLabel="sessions" />,
        ]}
      />
    </div>
  );
}

function MetricCard({ label, value, icon, accent = 'text-white' }: { label: string; value: string; icon: React.ReactNode; accent?: string; }) {
  return (
    <div className="col-span-12 md:col-span-6 xl:col-span-3 rounded-xl border border-brand-border bg-brand-card p-5 transition-all hover:border-zinc-700 group">
      <div className="flex items-center justify-between opacity-40 group-hover:opacity-100 transition-opacity">
        <span className="text-[10px] font-black uppercase tracking-[0.2em] text-zinc-400">{label}</span>
        <div className="text-zinc-500">{icon}</div>
      </div>
      <div className="mt-3 text-3xl font-mono font-bold tracking-tighter">
        <span className={accent}>{value}</span>
      </div>
    </div>
  );
}

function AnalyticsSection({ title, subtitle, primary, sidePanels }: { title: string; subtitle: string; primary: React.ReactNode; sidePanels: React.ReactNode[]; }) {
  return (
    <section className="rounded-2xl border border-brand-border bg-brand-card/80 p-5 shadow-2xl">
      <div className="mb-5 flex items-end justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold tracking-tight text-white">{title}</h3>
          <p className="mt-1 text-[11px] uppercase tracking-[0.18em] text-zinc-500">{subtitle}</p>
        </div>
      </div>
      <div className="grid grid-cols-12 gap-4">
        <div className={sidePanels.length > 0 ? 'col-span-12 xl:col-span-8' : 'col-span-12'}>
          <div className="rounded-xl border border-zinc-800 bg-black/20 p-4">{primary}</div>
        </div>
        {sidePanels.length > 0 && (
          <div className="col-span-12 xl:col-span-4 space-y-4">
            {sidePanels.map((panel, index) => (
              <div key={index} className="rounded-xl border border-zinc-800 bg-black/20 p-4">{panel}</div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function RankingPanel({ title, rows, metricLabel }: { title: string; rows: RankRow[]; metricLabel: string; }) {
  return (
    <div>
      <h4 className="mb-3 text-[10px] font-black uppercase tracking-[0.2em] text-zinc-400">{title}</h4>
      <div className="space-y-3">
        {rows.length === 0 ? (
          <div className="rounded-lg border border-dashed border-zinc-800 p-4 text-center text-[10px] uppercase tracking-widest text-zinc-600">No data</div>
        ) : rows.map((row) => (
          <div key={row.name} className="flex items-center justify-between gap-4 rounded-lg border border-zinc-800/70 bg-zinc-950/40 px-3 py-2">
            <span className="truncate text-[11px] text-zinc-300">{row.name}</span>
            <span className="text-[11px] font-mono text-zinc-400">{formatCompactNumber(row.value)} {metricLabel}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AreaPanel({ data, valueKey, label, color }: { data: Array<{ bucket: string; value: number }>; valueKey: string; label: string; color: string; }) {
  return (
    <div className="h-[280px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data}>
          <defs>
            <linearGradient id={`grad-${label}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={color} stopOpacity={0.25} />
              <stop offset="95%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#18181b" vertical={false} />
          <XAxis dataKey="bucket" stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickMargin={10} />
          <YAxis stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickFormatter={(value) => formatCompactNumber(Number(value))} />
          <Tooltip content={<SeriesTooltip />} cursor={{ stroke: '#27272a', strokeWidth: 1 }} />
          <Area type="monotone" dataKey={valueKey} stroke={color} strokeWidth={2} fillOpacity={1} fill={`url(#grad-${label})`} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function LinePanel({
  data,
  firstKey,
  firstLabel,
  firstColor,
  secondKey,
  secondLabel,
  secondColor,
}: {
  data: Array<Record<string, number | string>>;
  firstKey: string;
  firstLabel: string;
  firstColor: string;
  secondKey?: string;
  secondLabel?: string;
  secondColor?: string;
}) {
  return (
    <div className="h-[280px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke="#18181b" vertical={false} />
          <XAxis dataKey="bucket" stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickMargin={10} />
          <YAxis stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickFormatter={(value) => formatCompactNumber(Number(value))} />
          <Tooltip content={<SeriesTooltip />} cursor={{ stroke: '#27272a', strokeWidth: 1 }} />
          <Line type="monotone" dataKey={firstKey} name={firstLabel} stroke={firstColor} strokeWidth={2} dot={false} />
          {secondKey && secondLabel && secondColor && (
            <Line type="monotone" dataKey={secondKey} name={secondLabel} stroke={secondColor} strokeWidth={2} dot={false} />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function LatencyGrid({ toolLatency, llmLatency, sessionLatency }: { toolLatency: Quantiles; llmLatency: Quantiles; sessionLatency: Quantiles; }) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <LatencyCard title="Tool completion" stats={toolLatency} color="#f59e0b" />
      <LatencyCard title="LLM completion" stats={llmLatency} color="#a855f7" />
      <LatencyCard title="Session completion" stats={sessionLatency} color="#14b8a6" />
    </div>
  );
}

function LatencyCard({ title, stats, color }: { title: string; stats: Quantiles; color: string; }) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-black/20 p-4">
      <h4 className="mb-4 text-[10px] font-black uppercase tracking-[0.2em] text-zinc-400">{title}</h4>
      <div className="grid grid-cols-3 gap-3">
        {[
          ['P50', stats.p50],
          ['P90', stats.p90],
          ['P99', stats.p99],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-lg border border-zinc-800 bg-zinc-950/50 px-3 py-4 text-center">
            <div className="text-[10px] font-black uppercase tracking-[0.18em] text-zinc-500">{label}</div>
            <div className="mt-2 text-2xl font-mono font-bold" style={{ color }}>{Math.round(Number(value))}</div>
            <div className="mt-1 text-[10px] uppercase tracking-[0.18em] text-zinc-500">ms</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SessionPanel({ stats, data }: { stats: Quantiles; data: Array<{ bucket: string; value: number }>; }) {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-3 gap-4">
        {[
          ['P50', stats.p50],
          ['P90', stats.p90],
          ['P99', stats.p99],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-xl border border-zinc-800 bg-black/20 px-4 py-5 text-center">
            <div className="text-[10px] font-black uppercase tracking-[0.2em] text-zinc-500">{label}</div>
            <div className="mt-2 text-3xl font-mono font-bold text-brand-primary">{Math.round(Number(value))}</div>
            <div className="mt-1 text-[10px] uppercase tracking-[0.18em] text-zinc-500">ms</div>
          </div>
        ))}
      </div>
      <div className="h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#18181b" vertical={false} />
            <XAxis dataKey="bucket" stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickMargin={10} />
            <YAxis stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickFormatter={(value) => formatCompactNumber(Number(value))} />
            <Tooltip content={<SeriesTooltip />} cursor={{ stroke: '#27272a', strokeWidth: 1 }} />
            <Line type="monotone" dataKey="value" name="Session length" stroke="#14b8a6" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function SeriesTooltip({ active, payload, label }: any) {
  if (!active || !payload || !payload.length) {
    return null;
  }

  return (
    <div className="rounded-lg border border-zinc-800 bg-black/95 p-4 shadow-2xl backdrop-blur-md">
      <p className="mb-3 border-b border-zinc-800 pb-2 text-[10px] font-black uppercase tracking-widest text-zinc-500">{label}</p>
      <div className="space-y-2 font-mono text-xs">
        {payload.map((entry: any) => (
          <div key={String(entry.dataKey)} className="flex justify-between gap-8">
            <span className="text-zinc-400">{entry.name || entry.dataKey}</span>
            <span className="text-white">{formatCompactNumber(Number(entry.value || 0))}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function applyFilters(rows: DashboardRow[], filters: any) {
  return rows.filter((row) => {
    const agent = String(row.agent || row.agent_id || '').toLowerCase();
    const user = String(row.user_id || '').toLowerCase();
    const trace = String(row.trace_id || '').toLowerCase();
    const span = String(row.span_id || row.id || '').toLowerCase();

    return matches(filters.agentId, agent)
      && matches(filters.userId, user)
      && matches(filters.traceId, trace)
      && matches(filters.spanId, span);
  });
}

function matches(filterValue: string, candidate: string) {
  return !filterValue || filterValue === 'all' || candidate.includes(filterValue.toLowerCase());
}

function sumBy(rows: DashboardRow[], selector: (row: DashboardRow) => number) {
  return rows.reduce((total, row) => total + Number(selector(row) || 0), 0);
}

function uniqueCount(rows: DashboardRow[], selector: (row: DashboardRow) => unknown) {
  return new Set(rows.map(selector).filter(Boolean)).size;
}

function isToolEvent(row: DashboardRow) {
  return String(row.event_type || '').toUpperCase().includes('TOOL');
}

function isLlmRequest(row: DashboardRow) {
  return String(row.event_type || '').toUpperCase().includes('LLM_REQUEST');
}

function isToolError(row: DashboardRow) {
  return String(row.status || '').toUpperCase() === 'ERROR' && isToolEvent(row);
}

function rankBy(
  rows: DashboardRow[],
  labelFn: (row: DashboardRow) => string,
  valueFn: (row: DashboardRow) => number,
  distinctKeyFn?: (row: DashboardRow) => unknown,
): RankRow[] {
  const totals = new Map<string, number>();
  const distinctTracker = new Map<string, Set<string>>();

  rows.forEach((row) => {
    const label = labelFn(row) || 'unknown';
    if (distinctKeyFn) {
      const key = String(distinctKeyFn(row) || '');
      if (!key) return;
      const tracker = distinctTracker.get(label) || new Set<string>();
      tracker.add(key);
      distinctTracker.set(label, tracker);
      totals.set(label, tracker.size);
      return;
    }

    totals.set(label, (totals.get(label) || 0) + Number(valueFn(row) || 0));
  });

  return Array.from(totals.entries())
    .map(([name, value]) => ({ name, value }))
    .sort((left, right) => right.value - left.value)
    .slice(0, 5);
}

function latencyStats(rows: DashboardRow[]): Quantiles {
  const values = rows
    .map((row) => Number(row.latency || 0))
    .filter((value) => Number.isFinite(value) && value > 0)
    .sort((left, right) => left - right);

  return {
    p50: percentile(values, 0.5),
    p90: percentile(values, 0.9),
    p99: percentile(values, 0.99),
  };
}

function sessionLengthStats(rows: DashboardRow[]): Quantiles {
  const perSession = new Map<string, { min: number; max: number }>();

  rows.forEach((row) => {
    const sessionId = String(row.session_id || '');
    const timestamp = new Date(row.timestamp).getTime();
    if (!sessionId || !Number.isFinite(timestamp)) {
      return;
    }

    const entry = perSession.get(sessionId) || { min: timestamp, max: timestamp };
    entry.min = Math.min(entry.min, timestamp);
    entry.max = Math.max(entry.max, timestamp);
    perSession.set(sessionId, entry);
  });

  const durations = Array.from(perSession.values())
    .map((entry) => entry.max - entry.min)
    .sort((left, right) => left - right);

  return {
    p50: percentile(durations, 0.5),
    p90: percentile(durations, 0.9),
    p99: percentile(durations, 0.99),
  };
}

function buildSeries(rows: DashboardRow[], bucketSize: string, selector: (row: DashboardRow) => number) {
  const buckets = new Map<number, { bucket: string; value: number }>();

  rows.forEach((row) => {
    const timestamp = new Date(row.timestamp).getTime();
    if (!Number.isFinite(timestamp)) {
      return;
    }

    const bucketStart = getBucketStart(timestamp, bucketSize);
    const existing = buckets.get(bucketStart) || { bucket: formatBucket(bucketStart, bucketSize), value: 0 };
    existing.value += Number(selector(row) || 0);
    buckets.set(bucketStart, existing);
  });

  return Array.from(buckets.entries())
    .sort(([left], [right]) => left - right)
    .map(([, entry]) => entry);
}

function buildCountSeries(rows: DashboardRow[], bucketSize: string, predicate: (row: DashboardRow) => boolean) {
  return buildSeries(rows, bucketSize, (row) => (predicate(row) ? 1 : 0));
}

function buildDistinctSeries(rows: DashboardRow[], bucketSize: string, selector: (row: DashboardRow) => unknown) {
  const buckets = new Map<number, { bucket: string; value: number; seen: Set<string> }>();

  rows.forEach((row) => {
    const timestamp = new Date(row.timestamp).getTime();
    if (!Number.isFinite(timestamp)) {
      return;
    }

    const key = String(selector(row) || '');
    if (!key) {
      return;
    }

    const bucketStart = getBucketStart(timestamp, bucketSize);
    const existing = buckets.get(bucketStart) || { bucket: formatBucket(bucketStart, bucketSize), value: 0, seen: new Set<string>() };
    existing.seen.add(key);
    existing.value = existing.seen.size;
    buckets.set(bucketStart, existing);
  });

  return Array.from(buckets.entries())
    .sort(([left], [right]) => left - right)
    .map(([, entry]) => ({ bucket: entry.bucket, value: entry.value }));
}

function buildSessionLengthTrend(rows: DashboardRow[], bucketSize: string) {
  const perSession = new Map<string, { min: number; max: number; timestamp: number }>();

  rows.forEach((row) => {
    const sessionId = String(row.session_id || '');
    const timestamp = new Date(row.timestamp).getTime();
    if (!sessionId || !Number.isFinite(timestamp)) {
      return;
    }

    const entry = perSession.get(sessionId) || { min: timestamp, max: timestamp, timestamp };
    entry.min = Math.min(entry.min, timestamp);
    entry.max = Math.max(entry.max, timestamp);
    entry.timestamp = Math.min(entry.timestamp, timestamp);
    perSession.set(sessionId, entry);
  });

  return buildSeries(
    Array.from(perSession.values()).map((entry) => ({ timestamp: new Date(entry.timestamp).toISOString(), value: entry.max - entry.min })),
    bucketSize,
    (row) => Number((row as { value?: number }).value || 0),
  );
}

function mergeSeries(firstSeries: Array<{ bucket: string; value: number }>, secondSeries: Array<{ bucket: string; value: number }>) {
  const merged = new Map<string, { bucket: string; value: number; value2: number }>();

  firstSeries.forEach((entry) => {
    merged.set(entry.bucket, { bucket: entry.bucket, value: entry.value, value2: 0 });
  });

  secondSeries.forEach((entry) => {
    const existing = merged.get(entry.bucket) || { bucket: entry.bucket, value: 0, value2: 0 };
    existing.value2 = entry.value;
    merged.set(entry.bucket, existing);
  });

  return Array.from(merged.values()).sort((left, right) => left.bucket.localeCompare(right.bucket));
}

function getBucketStart(timestamp: number, bucketSize: string) {
  const date = new Date(timestamp);
  if (bucketSize === 'minute') {
    date.setSeconds(0, 0);
  } else if (bucketSize === 'hour') {
    date.setMinutes(0, 0, 0);
  } else {
    date.setHours(0, 0, 0, 0);
  }

  return date.getTime();
}

function formatBucket(timestamp: number, bucketSize: string) {
  const date = new Date(timestamp);
  if (bucketSize === 'minute') {
    return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  if (bucketSize === 'hour') {
    return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' });
  }

  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function timeBucketForSpan(timespan: string) {
  if (timespan === '1h') return 'minute';
  if (timespan === '24h') return 'hour';
  return 'day';
}

function percentile(values: number[], fraction: number) {
  if (values.length === 0) {
    return 0;
  }

  const index = Math.min(values.length - 1, Math.max(0, Math.floor((values.length - 1) * fraction)));
  return values[index];
}
