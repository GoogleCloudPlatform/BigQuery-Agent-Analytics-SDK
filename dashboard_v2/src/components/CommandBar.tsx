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
import {
  Filter,
  Share2,
  Calendar,
  Cpu,
  Check,
  Database,
  Table as TableIcon,
  LayoutGrid,
  User,
  Fingerprint,
  PanelTop,
} from 'lucide-react';
import { useDashboardFilters } from '../hooks/useDashboardFilters';
import { cn } from '../lib/utils';

export const CommandBar: React.FC = () => {
  const { filters, setFilters } = useDashboardFilters();
  const [copied, setCopied] = useState(false);
  const [projectId, setProjectId] = useState(filters.projectId || localStorage.getItem('user_gcp_project') || '');
  const [datasetId, setDatasetId] = useState(filters.datasetId || localStorage.getItem('user_bq_dataset') || '');
  const [tableId, setTableId] = useState(filters.tableId || localStorage.getItem('user_bq_table') || '');

  useEffect(() => {
    setProjectId(filters.projectId || '');
    setDatasetId(filters.datasetId || '');
    setTableId(filters.tableId || '');
  }, [filters.projectId, filters.datasetId, filters.tableId]);

  // Handle sharing the current dashboard URL
  const handleShare = async () => {
    await navigator.clipboard.writeText(window.location.href);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="sticky top-0 z-50 w-full border-b border-zinc-800 bg-black/80 backdrop-blur-md">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center justify-between px-6">

        {/* Left: Branding & Logic Filters */}
        <div className="flex items-center gap-8">
          <div className="flex items-center gap-3">
            <div className="relative h-6 w-1 bg-brand-primary rounded-full overflow-hidden">
                <div className="absolute inset-0 bg-white/40 animate-pulse" />
            </div>
            <h1 className="text-xs font-bold tracking-widest text-white uppercase font-mono">
              AOS <span className="text-zinc-600">v1.0.4</span>
            </h1>
          </div>

          <div className="flex items-center gap-6">
            <FilterSelect
              icon={<Cpu size={14} />}
              label="Agent"
              value={filters.agentId || 'all'}
              options={['all', 'orchestrator', 'billing_agent', 'swot_analyzer', 'research_bot']}
              onChange={(v) => setFilters({ agentId: v })}
            />

            <TextFilter
              icon={<User size={14} />}
              label="User"
              value={filters.userId || 'all'}
              placeholder="user-id"
              onChange={(value) => setFilters({ userId: value || 'all' })}
            />

            <TextFilter
              icon={<Fingerprint size={14} />}
              label="Trace"
              value={filters.traceId || ''}
              placeholder="trace-id"
              onChange={(value) => setFilters({ traceId: value })}
            />

            <TextFilter
              icon={<PanelTop size={14} />}
              label="Span"
              value={filters.spanId || ''}
              placeholder="span-id"
              onChange={(value) => setFilters({ spanId: value })}
            />

            <FilterSelect
              icon={<Calendar size={14} />}
              label="Timespan"
              value={filters.timespan || '24h'}
              options={['1h', '24h', '7d', '30d', '90d', '1y']}
              onChange={(v) => setFilters({ timespan: v })}
            />
          </div>
        </div>

        {/* Right: Customer-owned Billing Inputs */}
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 px-4 border-l border-zinc-800">

            {/* GCP PROJECT ID */}
            <div className="relative group" title="Customer-owned GCP Project ID">
              <LayoutGrid className="absolute left-2 top-1/2 -translate-y-1/2 text-zinc-600 group-focus-within:text-blue-400 transition-colors" size={10} />
              <input
                type="text"
                placeholder="Your Project"
                value={projectId}
                onChange={(e) => {
                  setProjectId(e.target.value);
                  localStorage.setItem('user_gcp_project', e.target.value);
                  setFilters({ projectId: e.target.value });
                }}
                className="h-8 w-28 bg-zinc-900/50 border border-zinc-800 rounded pl-7 pr-2 text-[10px] text-blue-400 focus:border-blue-400/50 outline-none transition-all placeholder:text-zinc-700"
              />
            </div>

            {/* BIGQUERY DATASET */}
            <div className="relative group" title="Customer-owned BigQuery Dataset ID">
              <Database className="absolute left-2 top-1/2 -translate-y-1/2 text-zinc-600 group-focus-within:text-zinc-300 transition-colors" size={10} />
              <input
                type="text"
                placeholder="Your Dataset"
                value={datasetId}
                onChange={(e) => {
                  setDatasetId(e.target.value);
                  localStorage.setItem('user_bq_dataset', e.target.value);
                  setFilters({ datasetId: e.target.value });
                }}
                className="h-8 w-24 bg-zinc-900/50 border border-zinc-800 rounded pl-7 pr-2 text-[10px] text-zinc-300 focus:border-zinc-500/50 outline-none transition-all placeholder:text-zinc-700"
              />
            </div>

            {/* BIGQUERY TABLE */}
            <div className="relative group" title="Customer-owned BigQuery Table ID">
              <TableIcon className="absolute left-2 top-1/2 -translate-y-1/2 text-zinc-600 group-focus-within:text-zinc-300 transition-colors" size={10} />
              <input
                type="text"
                placeholder="Your Table"
                value={tableId}
                onChange={(e) => {
                  setTableId(e.target.value);
                  localStorage.setItem('user_bq_table', e.target.value);
                  setFilters({ tableId: e.target.value });
                }}
                className="h-8 w-24 bg-zinc-900/50 border border-zinc-800 rounded pl-7 pr-2 text-[10px] text-zinc-300 focus:border-zinc-500/50 outline-none transition-all placeholder:text-zinc-700"
              />
            </div>
          </div>

          <div className="hidden xl:flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-950/60 px-3 py-1 text-[10px] font-mono uppercase tracking-[0.18em] text-zinc-500">
            <span className="text-zinc-700">Future:</span> custom labels
          </div>

          <button
            onClick={handleShare}
            className={cn(
              "flex h-8 items-center gap-2 rounded-md border border-zinc-800 bg-brand-card px-3 text-[11px] font-bold uppercase tracking-tight transition-all",
              copied ? "text-emerald-400 border-emerald-500/50 bg-emerald-500/5" : "text-zinc-400 hover:bg-zinc-800 hover:text-white"
            )}
          >
            {copied ? <Check size={13} /> : <Share2 size={13} />}
            {copied ? "Copied" : "Share"}
          </button>
        </div>
      </div>
    </div>
  );
};

/* Reusable Filter Component */
interface FilterSelectProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  options: string[];
  onChange: (val: string) => void;
}

const FilterSelect: React.FC<FilterSelectProps> = ({ icon, label, value, options, onChange }) => (
  <div className="flex items-center gap-2.5">
    <div className="flex items-center gap-1.5 text-[10px] font-black uppercase tracking-tighter text-zinc-500">
      <span className="text-zinc-600">{icon}</span>
      {label}
    </div>
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-7 min-w-[80px] cursor-pointer appearance-none rounded border border-zinc-800 bg-zinc-900/20 px-2 pr-6 text-[11px] font-mono font-medium text-zinc-300 outline-none hover:border-zinc-700 hover:bg-zinc-800/50 transition-all uppercase"
      >
        {options.map(opt => (
          <option key={opt} value={opt} className="bg-zinc-900 text-white uppercase text-[10px]">
            {opt.replace('_', ' ')}
          </option>
        ))}
      </select>
      <div className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-zinc-600">
        <Filter size={10} />
      </div>
    </div>
  </div>
);

interface TextFilterProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  placeholder: string;
  onChange: (val: string) => void;
}

const TextFilter: React.FC<TextFilterProps> = ({ icon, label, value, placeholder, onChange }) => (
  <div className="flex items-center gap-2.5">
    <div className="flex items-center gap-1.5 text-[10px] font-black uppercase tracking-tighter text-zinc-500">
      <span className="text-zinc-600">{icon}</span>
      {label}
    </div>
    <input
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="h-7 w-28 rounded border border-zinc-800 bg-zinc-900/20 px-2 text-[11px] font-mono text-zinc-300 outline-none transition-all placeholder:text-zinc-700 hover:border-zinc-700 focus:border-brand-primary/50"
    />
  </div>
);
