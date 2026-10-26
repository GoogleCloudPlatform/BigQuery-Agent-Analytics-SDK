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

import React, { useEffect, useState } from 'react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { TrendingUp, Wallet, Zap, MessageSquare } from 'lucide-react';
import { cn, formatCompactNumber, formatCurrency } from '../lib/utils';
import { useDashboardFilters } from '../hooks/useDashboardFilters';
import { useDashboardHealth } from '../hooks/useDashboardHealth';
import { fetchAgentData, isBigQueryAuthError } from '../services/apiService';

export const FinOpsSummary: React.FC = () => {
  const { filters } = useDashboardFilters();
  const { ready: authReady, loading: healthLoading, missing: missingAuth } = useDashboardHealth();
  const [stats, setStats] = useState<any>(null);
  const [consumptionData, setConsumptionData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [copied, setCopied] = useState(false);
  const sourceReady = Boolean(filters.projectId && filters.datasetId && filters.tableId && authReady);

  useEffect(() => {
    const loadFinOps = async () => {
      if (healthLoading) {
        return;
      }

      if (!sourceReady) {
        setStats(null);
        setConsumptionData([]);
        setError('');
        setLoading(false);
        return;
      }

      setLoading(true);
      setError('');
      try {
        // Fetch raw rows from BigQuery based on selected timespan
        const rawData = await fetchAgentData(filters.timespan, filters);

        /** * DATA TRANSFORMER
         * Since BigQuery returns raw rows, we calculate the summary metrics here.
         * Adjust these keys (total_tokens, cost, etc) based on your BQ column names.
         */
        const totalTokens = rawData.reduce((acc: number, curr: any) => acc + (Number(curr.total_tokens) || 0), 0);
        const totalSessions = new Set(rawData.map((r: any) => r.session_id)).size;

        setStats({
          totalSessions: totalSessions || 0,
          sessionTrend: "+12%", // Mock trend for UI
          totalQuestions: rawData.length,
          questionTrend: "+5%",
          totalTokens: (totalTokens / 1000000).toFixed(2),
          tokenTrend: "+18%",
          totalCost: (totalTokens * 0.000002), // Rough estimate: $2 per 1M tokens
          costTrend: "+14%"
        });

        // Map data for the Area Chart (Grouped by date)
        const trend = rawData.map((row: any) => ({
          date: new Date(row.timestamp).toLocaleDateString([], { month: 'short', day: 'numeric' }),
          input: Math.floor(Number(row.total_tokens) * 0.4), // Mock input/output split
          output: Math.floor(Number(row.total_tokens) * 0.6),
        })).reverse();

        setConsumptionData(trend);
      } catch (err) {
        if (!isBigQueryAuthError(err)) {
          console.error("FinOps Load Failed:", err);
        }
        setError(err instanceof Error ? err.message : 'Failed to load dashboard data');
        setStats(null);
        setConsumptionData([]);
      } finally {
        setLoading(false);
      }
    };
    loadFinOps();
  }, [filters.timespan, filters.projectId, filters.datasetId, filters.tableId, sourceReady]); // Re-run when scope or timespan changes

  if (!sourceReady) {
    return (
      <div className="p-6">
        <div className="rounded-xl border border-dashed border-zinc-700 bg-zinc-950/40 p-8 text-center text-zinc-400">
          <p className="text-sm font-semibold text-white">
            {!authReady ? 'Connect backend BigQuery auth to load your dashboard' : 'Connect a BigQuery source to load your dashboard'}
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            {!authReady
              ? 'Set GOOGLE_APPLICATION_CREDENTIALS or GCP_CLIENT_EMAIL and GCP_PRIVATE_KEY on the dashboard backend.'
              : 'Enter Project ID, Dataset ID, and Table ID in the command bar above. The metrics and traces will refresh automatically.'}
          </p>
          {!authReady && missingAuth.length > 0 && (
            <div className="mt-4 text-xs text-zinc-400">
              <div className="font-medium text-zinc-200">Missing backend variables:</div>
              <ul className="mt-2 space-y-1 list-disc list-inside">
                {missingAuth.map((m) => (
                  <li key={m} className="text-zinc-400">{m}</li>
                ))}
              </ul>
              <div className="mt-4 flex items-center justify-center gap-3">
                <button
                  onClick={async () => {
                    const envSnippet = `# Option A: use a service account JSON file\nexport GOOGLE_APPLICATION_CREDENTIALS="./service-account.json"\n\n# Option B: set individual env vars (replace placeholders)\nexport GCP_CLIENT_EMAIL=\"your-service-account@PROJECT.iam.gserviceaccount.com\"\nexport GCP_PRIVATE_KEY=\"-----BEGIN PRIVATE KEY-----\\nYOUR_PRIVATE_KEY_HERE\\n-----END PRIVATE KEY-----\"\n`;
                    try {
                      await navigator.clipboard.writeText(envSnippet);
                      setCopied(true);
                      setTimeout(() => setCopied(false), 2000);
                    } catch (e) {
                      console.error('Clipboard copy failed', e);
                    }
                  }}
                  className="rounded-md border border-zinc-700 bg-zinc-900/60 px-3 py-1 text-[12px] font-medium text-zinc-200 hover:bg-zinc-900"
                >
                  {copied ? 'Copied!' : 'Copy env snippet'}
                </button>
                <span className="text-[11px] text-zinc-500">Copies a ready-to-edit env snippet to clipboard.</span>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="grid grid-cols-12 gap-6 p-6">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="col-span-12 md:col-span-3 h-32 animate-pulse bg-zinc-900/50 rounded-xl border border-zinc-800" />
        ))}
        <div className="col-span-12 h-80 animate-pulse bg-zinc-900/20 rounded-xl border border-zinc-800" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <div className="rounded-xl border border-red-500/20 bg-red-500/5 p-6 text-sm text-red-200">
          <p className="font-semibold">Unable to load this dashboard view</p>
          <p className="mt-2 text-red-200/80">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-12 gap-6 p-6">
      <StatCard label="Total Sessions" value={stats.totalSessions} trend={stats.sessionTrend} icon={<TrendingUp size={16} />} />
      <StatCard label="Total Agent Hits" value={stats.totalQuestions} trend={stats.questionTrend} icon={<MessageSquare size={16} />} />
      <StatCard label="Total Tokens" value={stats.totalTokens} unit="Mil" trend={stats.tokenTrend} icon={<Zap size={16} />} color="text-brand-primary" />
      <StatCard label="Est. Cost (USD)" value={formatCurrency(stats.totalCost)} trend={stats.costTrend} icon={<Wallet size={16} />} color="text-emerald-500" />

      <div className="col-span-12 rounded-xl border border-brand-border bg-brand-card p-6 shadow-2xl">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h3 className="text-xs font-black uppercase tracking-[0.2em] flex items-center gap-2 text-white">
              <Zap size={14} className="text-brand-primary" /> Token Consumption Trend
            </h3>
            <p className="text-[11px] text-zinc-500 mt-1 font-medium italic">Projected usage based on historical BigQuery logs</p>
          </div>
        </div>

        <div className="h-[320px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={consumptionData}>
              <defs>
                <linearGradient id="colorInput" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#ef4444" stopOpacity={0.15}/>
                  <stop offset="95%" stopColor="#ef4444" stopOpacity={0}/>
                </linearGradient>
                <linearGradient id="colorOutput" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.15}/>
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}/>
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#18181b" vertical={false} />
              <XAxis dataKey="date" stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickMargin={12} />
              <YAxis stroke="#3f3f46" fontSize={10} tickLine={false} axisLine={false} tickFormatter={(val) => formatCompactNumber(val)} />
              <Tooltip content={<CustomTooltip />} cursor={{ stroke: '#27272a', strokeWidth: 1 }} />
              <Area type="monotone" dataKey="input" stroke="#ef4444" strokeWidth={2} fillOpacity={1} fill="url(#colorInput)" animationDuration={1000} />
              <Area type="monotone" dataKey="output" stroke="#3b82f6" strokeWidth={2} fillOpacity={1} fill="url(#colorOutput)" animationDuration={1200} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
};

const StatCard: React.FC<{ label: string; value: string | number; trend: string; icon: React.ReactNode; unit?: string; color?: string; }> = ({ label, value, trend, icon, unit, color = "text-white" }) => (
  <div className="col-span-12 md:col-span-3 rounded-xl border border-brand-border bg-brand-card p-5 transition-all hover:border-zinc-700 group">
    <div className="flex items-center justify-between opacity-40 group-hover:opacity-100 transition-opacity">
      <span className="text-[10px] font-black uppercase tracking-[0.2em] text-zinc-400">{label}</span>
      <div className="text-zinc-500">{icon}</div>
    </div>
    <div className="mt-3 flex items-baseline gap-2">
      <span className={cn("text-3xl font-mono font-bold tracking-tighter", color)}>{value}</span>
      {unit && <span className="text-[10px] font-black opacity-20 uppercase tracking-widest">{unit}</span>}
    </div>
    <div className="mt-4 flex items-center gap-2 text-[9px] font-black uppercase tracking-widest">
      <div className={cn("h-1 w-1 rounded-full", trend.includes('+') ? "bg-emerald-500" : "bg-red-500")} />
      <span className={cn(trend.includes('+') ? "text-emerald-500" : "text-red-500")}>{trend}</span>
    </div>
  </div>
);

const CustomTooltip = ({ active, payload, label }: any) => {
  if (active && payload && payload.length) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-black/95 p-4 shadow-2xl backdrop-blur-md">
        <p className="mb-3 text-[10px] font-black uppercase tracking-widest text-zinc-500 border-b border-zinc-800 pb-2">{label}</p>
        <div className="space-y-2 font-mono text-xs">
          <div className="flex justify-between gap-8"><span className="text-zinc-400">In</span><span className="text-red-500">{formatCompactNumber(payload[0].value)}</span></div>
          <div className="flex justify-between gap-8"><span className="text-zinc-400">Out</span><span className="text-blue-500">{formatCompactNumber(payload[1].value)}</span></div>
        </div>
      </div>
    );
  }
  return null;
};
