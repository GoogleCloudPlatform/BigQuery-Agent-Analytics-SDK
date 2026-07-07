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

import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CommandBar } from './components/CommandBar';
import { AnalyticsOverview } from './components/AnalyticsOverview';
import { TraceTree } from './components/TraceTree';
import { AuditLog } from './components/AuditLog';
import { ShieldCheck } from 'lucide-react';
import { useDashboardFilters } from './hooks/useDashboardFilters';
import { useDashboardHealth } from './hooks/useDashboardHealth';

const queryClient = new QueryClient();

function Dashboard() {
  const { filters, setFilters } = useDashboardFilters();
  const {
    ready: authReady,
    loading: healthLoading,
    missing: missingAuth,
    source: pinnedSource,
  } = useDashboardHealth();

  // When the deployment pins its table via DASHBOARD_BQ_* env vars, the API
  // only serves that table — adopt it so the source fields are pre-filled and
  // stale localStorage/URL values can't produce 403s.
  useEffect(() => {
    if (!pinnedSource) return;
    if (
      filters.projectId !== pinnedSource.projectId
      || filters.datasetId !== pinnedSource.datasetId
      || filters.tableId !== pinnedSource.tableId
    ) {
      setFilters(pinnedSource);
    }
  }, [pinnedSource, filters.projectId, filters.datasetId, filters.tableId, setFilters]);
  const sourceLabel = filters.projectId && filters.datasetId && filters.tableId
    ? `${filters.projectId}.${filters.datasetId}.${filters.tableId}`
    : 'Awaiting project.dataset.table';
  const setupLabel = healthLoading
    ? 'Checking backend auth...'
    : authReady
      ? sourceLabel
      : `Backend auth not configured${missingAuth.length ? `: ${missingAuth.join(', ')}` : ''}`;

  return (
    <div className="min-h-screen bg-brand-bg flex flex-col selection:bg-brand-primary/30">
      {/* CommandBar manages its own filter state internally
          using the useDashboardFilters hook.
      */}
      <CommandBar />

      <main className="flex-1 overflow-auto bg-[radial-gradient(circle_at_50%_0%,rgba(239,68,68,0.03)_0%,transparent_50%)]">
        <div className="mx-auto max-w-[1600px] pb-20">

          {/* Dashboard Title Section */}
          <div className="px-6 py-10">
            <div className="flex items-center gap-3 text-zinc-500 mb-2">
              <ShieldCheck size={14} className="text-emerald-500/70" />
              <span className="text-[10px] font-bold uppercase tracking-[0.2em]">Verified Secure Environment</span>
            </div>
            <h1 className="text-4xl font-medium tracking-tighter text-white">
              Agent Intelligence <span className="text-zinc-600">Dashboard</span>
            </h1>
            <p className="mt-2 text-sm text-zinc-500 max-w-2xl leading-relaxed">
              Monitor multi-agent systems with trace-level diagnostics. Analyze token economics,
              orchestration latency, and cross-agent tool handoffs in real-time.
            </p>
            <div className="mt-4 inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-950/60 px-3 py-1 text-[10px] font-mono uppercase tracking-[0.2em] text-zinc-400">
              Active Source: <span className="text-white normal-case tracking-normal">{setupLabel}</span>
            </div>
          </div>

          {/* 1. FinOps & Token Economics */}
          <section className="mb-12">
            <AnalyticsOverview />
          </section>

          {/* 2. Forensic Reasoning Traces */}
          <section className="px-6 mb-12">
            <div className="border-l-2 border-brand-primary pl-4 mb-6">
              <h2 className="text-xl font-semibold tracking-tight text-white">Technical Traces</h2>
              <p className="text-xs text-zinc-500 uppercase tracking-widest font-bold mt-1">
                Forensic Reasoning Analysis
              </p>
            </div>
            <div className="bg-brand-card/20 rounded-xl border border-brand-border p-1">
              <TraceTree />
            </div>
          </section>

          {/* 3. Session & Context Audit */}
          <section className="px-6">
            <div className="border-l-2 border-zinc-700 pl-4 mb-6">
              <h2 className="text-xl font-semibold tracking-tight text-white">Context Inflation Audit</h2>
              <p className="text-xs text-zinc-500 uppercase tracking-widest font-bold mt-1">
                Session History & Forensic Analysis
              </p>
            </div>
            <AuditLog />
          </section>

        </div>
      </main>

      {/* Footer / System Status Bar */}
      <footer className="border-t border-brand-border bg-black px-6 py-3 flex items-center justify-between text-[10px] font-mono text-zinc-500">
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <div className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
            SYSTEM: ONLINE
          </div>
          <div className="hidden md:block text-zinc-700">|</div>
          <div className="uppercase">Region: Global-Edge</div>
          <div className="hidden md:block text-zinc-700">|</div>
          <div className="uppercase text-brand-primary/80">Protocol: AOS-v1</div>
        </div>
        <div className="flex items-center gap-4 opacity-50 hover:opacity-100 transition-opacity">
          <span>LATENCY: 42ms</span>
          <span>UPTIME: 99.9%</span>
        </div>
      </footer>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <Routes>
          <Route path="/" element={<Dashboard />} />
        </Routes>
      </Router>
    </QueryClientProvider>
  );
}
