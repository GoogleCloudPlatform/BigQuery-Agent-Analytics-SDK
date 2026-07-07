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

import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';

export interface DashboardFilters {
  agentId: string;
  userId: string;
  timespan: string;
  traceId: string;
  spanId: string;
  projectId: string;
  datasetId: string;
  tableId: string;
}

export function useDashboardFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  // Memoize filters to prevent unnecessary re-renders in components
  const filters = useMemo(() => ({
    agentId: searchParams.get('agent_id') || 'all',
    userId: searchParams.get('user_id') || 'all',
    timespan: searchParams.get('timespan') || '24h',
    traceId: searchParams.get('trace_id') || '',
    spanId: searchParams.get('span_id') || '',
    projectId: searchParams.get('project_id') || localStorage.getItem('user_gcp_project') || '',
    datasetId: searchParams.get('dataset_id') || localStorage.getItem('user_bq_dataset') || '',
    tableId: searchParams.get('table_id') || localStorage.getItem('user_bq_table') || '',
  }), [searchParams]);

  /**
   * Updates URL search parameters based on filter changes.
   * If a value is 'all' or empty, the parameter is removed for a cleaner URL.
   */
  const setFilters = useCallback((newFilters: Partial<DashboardFilters>) => {
    const params = new URLSearchParams(searchParams);

    // Helper to handle parameter updates
    const updateParam = (key: string, value: string | undefined) => {
      if (!value || value === 'all') {
        params.delete(key);
      } else {
        params.set(key, value);
      }
    };

    if (newFilters.agentId !== undefined) updateParam('agent_id', newFilters.agentId);
    if (newFilters.userId !== undefined) updateParam('user_id', newFilters.userId);
    if (newFilters.timespan !== undefined) updateParam('timespan', newFilters.timespan);
    if (newFilters.spanId !== undefined) {
      if (newFilters.spanId) params.set('span_id', newFilters.spanId);
      else params.delete('span_id');
    }
    if (newFilters.projectId !== undefined) updateParam('project_id', newFilters.projectId);
    if (newFilters.datasetId !== undefined) updateParam('dataset_id', newFilters.datasetId);
    if (newFilters.tableId !== undefined) updateParam('table_id', newFilters.tableId);

    if (newFilters.traceId !== undefined) {
      if (newFilters.traceId) params.set('trace_id', newFilters.traceId);
      else params.delete('trace_id');
    }

    setSearchParams(params, { replace: true });
  }, [searchParams, setSearchParams]);

  return { filters, setFilters };
}
