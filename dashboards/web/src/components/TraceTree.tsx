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

import React, { useMemo, useEffect, useState } from 'react';
import { ReactFlow, Background, Controls, Edge, Node, Position, Handle } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { cn } from '../lib/utils';
import { Activity, Wrench, Bot, AlertCircle, Loader2 } from 'lucide-react';
import { useDashboardFilters } from '../hooks/useDashboardFilters';
import { useDashboardHealth } from '../hooks/useDashboardHealth';
import { fetchAgentData, isBigQueryAuthError } from '../services/apiService';

// Define the Node UI
function CustomNode({ data }: any) {
  const node = data.node;
  const isError = node.status === 'error' || node.error;

  return (
    <div className={cn(
      "relative min-w-[220px] rounded-lg border-2 bg-brand-card p-3 transition-all",
      isError ? "border-red-500/50 shadow-[0_0_20px_rgba(239,68,68,0.1)]" : "border-zinc-800 hover:border-zinc-700"
    )}>
      <Handle type="target" position={Position.Top} className="!bg-zinc-700 !w-2 !h-2" />

      <div className="flex items-center justify-between gap-3">
        <div className={cn(
          "flex h-9 w-9 items-center justify-center rounded-md border",
          node.type === 'orchestrator' ? "bg-amber-500/10 border-amber-500/20 text-amber-500" :
          node.type === 'agent' ? "bg-blue-500/10 border-blue-500/20 text-blue-500" :
          "bg-emerald-500/10 border-emerald-500/20 text-emerald-500"
        )}>
          {node.type === 'orchestrator' && <Activity size={18} />}
          {node.type === 'agent' && <Bot size={18} />}
          {node.type === 'tool' && <Wrench size={18} />}
        </div>

        <div className="flex-1 overflow-hidden">
          <p className="text-[9px] font-black uppercase tracking-tighter opacity-40 mb-0.5">{node.type || 'node'}</p>
          <p className="text-xs font-bold truncate text-zinc-200">{node.label || node.agent || 'Unknown Task'}</p>
        </div>

        {isError && <AlertCircle size={16} className="text-red-500 animate-pulse shrink-0" />}
      </div>

      <div className="mt-3 flex items-center justify-between border-t border-zinc-800/50 pt-2 text-[10px] font-mono font-medium">
        <span className="text-zinc-500">{node.latency || 0}ms</span>
        <span className="text-brand-primary">{(node.tokens || node.total_tokens || 0).toLocaleString()} <span className="opacity-50">TKN</span></span>
      </div>

      <Handle type="source" position={Position.Bottom} className="!bg-zinc-700 !w-2 !h-2" />
    </div>
  );
}

const nodeTypes = { customNode: CustomNode };

export const TraceTree: React.FC = () => {
  const { filters } = useDashboardFilters();
  const { ready: authReady, loading: healthLoading } = useDashboardHealth();
  const [traceData, setTraceData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // compute nodes/edges from traceData with a stable hook order
  const { nodes, edges } = useMemo(() => {
    const flowNodes: Node[] = traceData.map((node, i) => {
      const depth = node.type === 'orchestrator' ? 0 : node.type === 'agent' ? 1 : 2;
      return {
        id: node.id || node.trace_id || `node-${i}`,
        type: 'customNode',
        position: { x: (i % 4) * 260, y: depth * 180 },
        data: { node },
      };
    });

    const flowEdges: Edge[] = traceData
      .filter(n => n.parentId || n.parent_id)
      .map((node, i) => ({
        id: `e-${i}`,
        source: node.parentId || node.parent_id,
        target: node.id || node.trace_id,
        animated: true,
        type: 'smoothstep',
        style: {
          stroke: node.status === 'error' ? '#ef4444' : '#3f3f46',
          strokeWidth: 2,
        },
      }));

    return { nodes: flowNodes, edges: flowEdges };
  }, [traceData]);

  const sourceReady = Boolean(filters.projectId && filters.datasetId && filters.tableId && authReady);

  useEffect(() => {
    const loadTrace = async () => {
      if (healthLoading) {
        return;
      }

      if (!sourceReady) {
        setTraceData([]);
        setError('');
        setLoading(false);
        return;
      }

      setLoading(true);
      setError('');
      try {
        // Fetch raw data using current timespan
        const data = await fetchAgentData(filters.timespan, filters);
        setTraceData(Array.isArray(data) ? data : []);
      } catch (err) {
        if (!isBigQueryAuthError(err)) {
          console.error("Trace load failed", err);
        }
        setError(err instanceof Error ? err.message : 'Failed to load trace data');
        setTraceData([]);
      } finally {
        setLoading(false);
      }
    };
    loadTrace();
  }, [filters.timespan, filters.agentId, filters.userId, filters.traceId, filters.spanId, filters.projectId, filters.datasetId, filters.tableId, sourceReady]);

  if (!sourceReady) {
    return (
      <div className="h-[500px] flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-800 bg-zinc-950/30 text-center">
        <p className="text-sm font-semibold text-white">
          {!authReady ? 'Trace view is waiting for backend BigQuery auth' : 'Trace view is waiting for your BigQuery table'}
        </p>
        <p className="mt-2 text-xs text-zinc-500">
          {!authReady
            ? 'Set GOOGLE_APPLICATION_CREDENTIALS or GCP_CLIENT_EMAIL and GCP_PRIVATE_KEY on the dashboard backend.'
            : 'Enter the project, dataset, and table IDs to reconstruct the reasoning graph.'}
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="h-[500px] flex flex-col items-center justify-center bg-brand-bg/50 rounded-xl">
        <Loader2 className="animate-spin text-brand-primary mb-4" size={32} />
        <p className="text-xs font-mono text-zinc-500 uppercase tracking-widest">Reconstructing Logic Flow...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-[500px] flex items-center justify-center rounded-xl border border-red-500/20 bg-red-500/5 text-sm text-red-200">
        {error}
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h3 className="text-xs font-black uppercase tracking-[0.2em] text-white flex items-center gap-3">
            <span className="h-1.5 w-1.5 rounded-full bg-brand-primary animate-ping" />
            Reasoning Trace Execution
          </h3>
        </div>

        <div className="flex items-center gap-5 p-2 bg-zinc-900/50 rounded-lg border border-zinc-800">
          <LegendItem color="bg-amber-500" label="Orchestrator" />
          <LegendItem color="bg-blue-500" label="Agent" />
          <LegendItem color="bg-emerald-500" label="Tool" />
        </div>
      </div>

      <div className="h-[500px] w-full overflow-hidden rounded-lg bg-zinc-950/20 border border-zinc-800/50 shadow-inner">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          colorMode="dark"
        >
          <Background color="#18181b" gap={25} size={1} />
          <Controls className="!bg-zinc-900 !border-zinc-700 !fill-white" />
        </ReactFlow>
      </div>
    </div>
  );
};

const LegendItem = ({ color, label }: { color: string, label: string }) => (
  <div className="flex items-center gap-2 text-[9px] font-black uppercase tracking-widest text-zinc-400">
    <div className={cn("h-1.5 w-1.5 rounded-full", color)} /> {label}
  </div>
);
