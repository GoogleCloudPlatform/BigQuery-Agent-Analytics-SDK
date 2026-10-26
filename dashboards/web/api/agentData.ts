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

import { BigQuery } from '@google-cloud/bigquery';
import { randomUUID } from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';

const TIME_SPANS: Record<string, string> = {
  '1h': '1 HOUR',
  '24h': '24 HOUR',
  '7d': '7 DAY',
  '30d': '30 DAY',
  '90d': '90 DAY',
  '1y': '365 DAY',
};

const PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,61}[A-Za-z0-9]$/;
const BQ_ID_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,1023}$/;
const DEFAULT_SERVICE_ACCOUNT_PATH = path.resolve(process.cwd(), 'service-account.json');
const APPLICATION_DEFAULT_CREDENTIALS_PATH = path.join(
  os.homedir(),
  '.config',
  'gcloud',
  'application_default_credentials.json'
);
const DASHBOARD_PROJECT_ID = process.env.DASHBOARD_BQ_PROJECT_ID?.trim();
const DASHBOARD_DATASET_ID = process.env.DASHBOARD_BQ_DATASET_ID?.trim();
const DASHBOARD_TABLE_ID = process.env.DASHBOARD_BQ_TABLE_ID?.trim();

export type AgentDataRequest = {
  method?: string;
  headers: Record<string, string | string[] | undefined>;
  query?: Record<string, string | string[] | undefined>;
};

export type AgentDataResponse = {
  status: number;
  body: unknown;
};

function getHeader(req: AgentDataRequest, name: string): string {
  const value = req.headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] || '' : String(value || '').trim();
}

function normalizePrivateKey(value: string): string {
  return value
    .replace(/^"|"$/g, '')
    .replace(/\\n/g, '\n');
}

function readServiceAccountFile(filePath: string): {
  projectId?: string;
  clientEmail: string;
  privateKey: string;
} | null {
  if (!fs.existsSync(filePath)) {
    return null;
  }

  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const parsed = JSON.parse(raw) as {
      project_id?: string;
      client_email?: string;
      private_key?: string;
    };

    if (!parsed.client_email || !parsed.private_key) {
      return null;
    }

    return {
      projectId: parsed.project_id,
      clientEmail: parsed.client_email,
      privateKey: parsed.private_key,
    };
  } catch {
    return null;
  }
}

function loadServiceAccountCredentials(): {
  projectId?: string;
  clientEmail: string;
  privateKey: string;
} | null {
  const configuredPath = process.env.GOOGLE_APPLICATION_CREDENTIALS;
  const candidatePaths = new Set<string>([
    configuredPath ? path.resolve(process.cwd(), configuredPath) : '',
    DEFAULT_SERVICE_ACCOUNT_PATH,
  ]);

  for (const candidatePath of candidatePaths) {
    if (!candidatePath) {
      continue;
    }

    const credentials = readServiceAccountFile(candidatePath);
    if (credentials) {
      return credentials;
    }
  }

  return null;
}

function hasApplicationDefaultCredentials(): boolean {
  return fs.existsSync(APPLICATION_DEFAULT_CREDENTIALS_PATH);
}

function isProductionDeployment(): boolean {
  return Boolean(process.env.VERCEL) || process.env.NODE_ENV === 'production';
}

function getConfiguredDashboardTableRef(): { projectId: string; datasetId: string; tableId: string } | null {
  if (!DASHBOARD_PROJECT_ID || !DASHBOARD_DATASET_ID || !DASHBOARD_TABLE_ID) {
    return null;
  }

  return {
    projectId: DASHBOARD_PROJECT_ID,
    datasetId: DASHBOARD_DATASET_ID,
    tableId: DASHBOARD_TABLE_ID,
  };
}

function resolveDashboardTableRef(req: AgentDataRequest):
  | { projectId: string; datasetId: string; tableId: string }
  | { status: number; error: string } {
  const configuredTableRef = getConfiguredDashboardTableRef();

  if (configuredTableRef) {
    const userProject = getHeader(req, 'x-gcp-project-id');
    const userDataset = getHeader(req, 'x-bq-dataset');
    const userTable = getHeader(req, 'x-bq-table');

    if (
      userProject !== configuredTableRef.projectId
      || userDataset !== configuredTableRef.datasetId
      || userTable !== configuredTableRef.tableId
    ) {
      return {
        status: 403,
        error: 'Dashboard table is not authorized for this deployment.',
      };
    }

    return configuredTableRef;
  }

  if (isProductionDeployment()) {
    return {
      status: 500,
      error: 'Dashboard table configuration is missing. Set DASHBOARD_BQ_PROJECT_ID, DASHBOARD_BQ_DATASET_ID, and DASHBOARD_BQ_TABLE_ID.',
    };
  }

  const userProject = getHeader(req, 'x-gcp-project-id');
  const userDataset = getHeader(req, 'x-bq-dataset');
  const userTable = getHeader(req, 'x-bq-table');

  if (!userProject || !userDataset || !userTable) {
    return {
      status: 400,
      error: 'Missing Configuration: Ensure Project, Dataset, and Table IDs are entered.',
    };
  }

  return {
    projectId: userProject,
    datasetId: userDataset,
    tableId: userTable,
  };
}

function hasBigQueryAuthConfigured(): boolean {
  if (process.env.GCP_CLIENT_EMAIL && process.env.GCP_PRIVATE_KEY) {
    return true;
  }

  return Boolean(loadServiceAccountCredentials()) || hasApplicationDefaultCredentials();
}

export function getDashboardRuntimeStatus() {
  // The pinned table ref is not a secret: it is the table this deployment
  // exists to display. Reporting it lets the UI pre-fill the source fields.
  const source = getConfiguredDashboardTableRef() || undefined;
  const ready = hasBigQueryAuthConfigured() && (!isProductionDeployment() || Boolean(source));
  if (ready) {
    return { ready, missing: [], source };
  }

  const missing: string[] = [];

  const clientEmail = process.env.GCP_CLIENT_EMAIL;
  const privateKey = process.env.GCP_PRIVATE_KEY;
  const gac = process.env.GOOGLE_APPLICATION_CREDENTIALS;

  if (!clientEmail) missing.push('GCP_CLIENT_EMAIL');
  if (!privateKey) missing.push('GCP_PRIVATE_KEY');

  if (gac) {
    const gacPath = path.resolve(process.cwd(), gac);
    if (!fs.existsSync(gacPath)) {
      missing.push('GOOGLE_APPLICATION_CREDENTIALS (file not found)');
    }
  } else if (!fs.existsSync(DEFAULT_SERVICE_ACCOUNT_PATH) && !hasApplicationDefaultCredentials()) {
    missing.push('GOOGLE_APPLICATION_CREDENTIALS or service-account.json');
  }

  if (isProductionDeployment() && !getConfiguredDashboardTableRef()) {
    missing.push('DASHBOARD_BQ_PROJECT_ID');
    missing.push('DASHBOARD_BQ_DATASET_ID');
    missing.push('DASHBOARD_BQ_TABLE_ID');
  }

  return { ready: false, missing, source };
}

function getBigQueryClient(): BigQuery {
  const projectId = process.env.GCP_PROJECT_ID;
  const clientEmail = process.env.GCP_CLIENT_EMAIL;
  const privateKey = process.env.GCP_PRIVATE_KEY;

  if (clientEmail && privateKey) {
    return new BigQuery({
      projectId,
      credentials: {
        client_email: clientEmail,
        private_key: normalizePrivateKey(privateKey),
      },
    });
  }

  const serviceAccountCredentials = loadServiceAccountCredentials();

  if (serviceAccountCredentials) {
    return new BigQuery({
      projectId: projectId || serviceAccountCredentials.projectId,
      credentials: {
        client_email: serviceAccountCredentials.clientEmail,
        private_key: normalizePrivateKey(serviceAccountCredentials.privateKey),
      },
    });
  }

  return new BigQuery({ projectId });
}

function assertValidTableRef(projectId: string, datasetId: string, tableId: string) {
  if (!PROJECT_ID_PATTERN.test(projectId)) {
    throw new Error('Invalid Project ID. Use only letters, numbers, and hyphens.');
  }
  if (!BQ_ID_PATTERN.test(datasetId)) {
    throw new Error('Invalid Dataset ID. Use a BigQuery dataset id, not a full path.');
  }
  if (!BQ_ID_PATTERN.test(tableId)) {
    throw new Error('Invalid Table ID. Use a table id without project/dataset prefixes.');
  }
}

function parseJson(value: unknown): any {
  if (!value || typeof value !== 'string') return value || {};
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

function timestampValue(value: any): string | null {
  if (!value) return null;
  if (typeof value === 'string') return value;
  if (value.value) return value.value;
  return String(value);
}

function latencyValue(value: unknown): number {
  const parsed = parseJson(value);
  if (typeof parsed === 'number') return parsed;
  return Number(parsed?.total_ms || parsed?.latency_ms || 0);
}

function tokenValue(content: any, attributes: any): number {
  const usage = content?.usage || attributes?.usage_metadata || {};
  return Number(
    usage.total
      || usage.total_token_count
      || usage.total_tokens
      || attributes?.total_tokens
      || content?.total_tokens
      || 0
  );
}

function classifyEvent(row: any): string {
  const eventType = String(row.event_type || '').toUpperCase();
  if (eventType.includes('TOOL')) return 'tool';
  if (eventType.includes('AGENT') || eventType.includes('INVOCATION')) {
    return 'agent';
  }
  return 'orchestrator';
}

function eventLabel(row: any, content: any): string {
  return (
    content?.tool
    || content?.tool_name
    || row.agent
    || row.event_type
    || row.span_id
    || row.session_id
    || 'Unknown event'
  );
}

function normalizeRow(row: any) {
  const content = parseJson(row.content);
  const attributes = parseJson(row.attributes);
  const timestamp = timestampValue(row.timestamp);

  return {
    ...row,
    id: row.span_id || row.event_id || `${row.session_id || 'session'}:${row.timestamp || ''}`,
    parent_id: row.parent_span_id || null,
    type: classifyEvent(row),
    label: eventLabel(row, content),
    timestamp,
    latency: latencyValue(row.latency_ms),
    total_tokens: tokenValue(content, attributes),
    content,
    attributes,
  };
}

function firstQueryValue(value: string | string[] | undefined): string {
  if (Array.isArray(value)) {
    return value[0] || '';
  }

  return String(value || '').trim();
}

function matchesFilter(candidate: unknown, expected: string): boolean {
  if (!expected) {
    return true;
  }

  return String(candidate || '').toLowerCase().includes(expected.toLowerCase());
}

function applyDashboardFilters(rows: any[], req: AgentDataRequest): any[] {
  const agentId = firstQueryValue(req.query?.agent_id);
  const userId = firstQueryValue(req.query?.user_id);
  const traceId = firstQueryValue(req.query?.trace_id);
  const spanId = firstQueryValue(req.query?.span_id);

  return rows.filter((row) => {
    const agentValue = row.agent_id || row.agent || row.attributes?.agent_id || row.attributes?.agent || '';
    const userValue = row.user_id || row.attributes?.user_id || '';
    const traceValue = row.trace_id || row.attributes?.trace_id || '';
    const spanValue = row.span_id || row.id || row.event_id || '';

    return matchesFilter(agentValue, agentId)
      && matchesFilter(userValue, userId)
      && matchesFilter(traceValue, traceId)
      && matchesFilter(spanValue, spanId);
  });
}

export async function handleAgentDataRequest(req: AgentDataRequest): Promise<AgentDataResponse> {
  if (req.method !== 'GET') {
    return {
      status: 405,
      body: { error: 'Method not allowed' },
    };
  }

  const timespan = firstQueryValue(req.query?.timespan);

  const tableRef = resolveDashboardTableRef(req);
  if ('status' in tableRef) {
    return {
      status: tableRef.status,
      body: { error: tableRef.error },
    };
  }

  try {
    assertValidTableRef(tableRef.projectId, tableRef.datasetId, tableRef.tableId);
  } catch (error: any) {
    return {
      status: 400,
      body: { error: error.message },
    };
  }

  const interval = TIME_SPANS[String(timespan || '24h')] || TIME_SPANS['24h'];
  const tablePath = `\`${tableRef.projectId}.${tableRef.datasetId}.${tableRef.tableId}\``;
  const query = `
    SELECT * FROM ${tablePath}
    WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL ${interval})
    ORDER BY timestamp DESC
    LIMIT 1000
  `;

  try {
    const [rows] = await getBigQueryClient().query({ query });
    return {
      status: 200,
      body: applyDashboardFilters(rows.map(normalizeRow), req),
    };
  } catch (error: any) {
    const requestId = randomUUID();
    console.error('BigQuery connector error', { requestId, error });
    return {
      status: 500,
      body: {
        error: 'Failed to query BigQuery. Include the request ID when reporting this error.',
        requestId,
      },
    };
  }
}