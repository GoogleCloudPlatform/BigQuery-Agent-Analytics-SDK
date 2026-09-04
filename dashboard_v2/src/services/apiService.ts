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

export interface BigQuerySource {
  projectId: string;
  datasetId: string;
  tableId: string;
}

export interface DashboardQueryFilters extends BigQuerySource {
  agentId?: string;
  userId?: string;
  timespan?: string;
  traceId?: string;
  spanId?: string;
}

const requestCache = new Map<string, Promise<any>>();
const AUTH_ERROR_MESSAGE = 'BigQuery authentication is not configured';
const HEALTH_CACHE_KEY = 'dashboard-health';

export type DashboardHealth = {
  ready: boolean;
  missing?: string[];
  // Set when the deployment pins its table via DASHBOARD_BQ_* env vars; the
  // UI uses it to pre-fill the source fields.
  source?: BigQuerySource;
};

export function isBigQueryAuthError(error: unknown): boolean {
  return error instanceof Error && error.message.includes(AUTH_ERROR_MESSAGE);
}

export const fetchDashboardHealth = async (): Promise<DashboardHealth> => {
  const cachedRequest = requestCache.get(HEALTH_CACHE_KEY);
  if (cachedRequest) {
    return cachedRequest;
  }

  const requestPromise = fetch('/api/health', { method: 'GET' })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Failed to load dashboard health (${response.status})`);
      }

      const body = await response.json();
      return body as DashboardHealth;
    })
    .finally(() => {
      requestCache.delete(HEALTH_CACHE_KEY);
    });

  requestCache.set(HEALTH_CACHE_KEY, requestPromise);
  return requestPromise;
};

export const fetchAgentData = async (timespan: string = '24h', source?: Partial<DashboardQueryFilters>) => {
  const credentials = {
    projectId: source?.projectId || localStorage.getItem('user_gcp_project') || '',
    datasetId: source?.datasetId || localStorage.getItem('user_bq_dataset') || '',
    tableId: source?.tableId || localStorage.getItem('user_bq_table') || '',
  };

  const cacheKey = [
    timespan,
    credentials.projectId,
    credentials.datasetId,
    credentials.tableId,
    source?.agentId || 'all',
    source?.userId || 'all',
    source?.traceId || '',
    source?.spanId || '',
  ].join(':');

  const cachedRequest = requestCache.get(cacheKey);
  if (cachedRequest) {
    return cachedRequest;
  }

  if (!credentials.projectId || !credentials.datasetId || !credentials.tableId) {
    throw new Error('Missing Configuration: enter Project ID, Dataset ID, and Table ID to load your dashboard.');
  }

  const query = new URLSearchParams();
  query.set('timespan', timespan);
  if (source?.agentId && source.agentId !== 'all') query.set('agent_id', source.agentId);
  if (source?.userId && source.userId !== 'all') query.set('user_id', source.userId);
  if (source?.traceId) query.set('trace_id', source.traceId);
  if (source?.spanId) query.set('span_id', source.spanId);

  const requestPromise = fetch(`/api?${query.toString()}`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      'x-gcp-project-id': credentials.projectId,
      'x-bq-dataset': credentials.datasetId,
      'x-bq-table': credentials.tableId,
    }
  })
    .then(async (response) => {
      if (!response.ok) {
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
          const errorData = await response.json();
          throw new Error(errorData.error || 'Failed to fetch data');
        }

        const responseText = await response.text();
        throw new Error(
          `Failed to fetch data (${response.status} ${response.statusText}). Expected JSON but received ${contentType || 'an unknown content type'}. ${responseText.slice(0, 120)}`
        );
      }

      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        const responseText = await response.text();
        throw new Error(
          `Expected JSON from /api, but received ${contentType || 'an unknown content type'}. ${responseText.slice(0, 120)}`
        );
      }

      return response.json();
    })
    .finally(() => {
      requestCache.delete(cacheKey);
    });

  requestCache.set(cacheKey, requestPromise);
  return requestPromise;
};
