// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Headless smoke test for dashboards/power_bi/copilot/app.js.
//
// Usage: node tests/power_bi_copilot_smoke.js dashboards/power_bi/copilot/app.js
//
// Boots the app against a minimal DOM stub built from the element ids in
// index.html, then drives it through prompts, SQL generation, import, and CSV
// export. Exits non-zero with a list of failures.
'use strict';

const fs = require('fs');
const path = require('path');

const appPath = path.resolve(process.argv[2]);
const html = fs.readFileSync(path.join(path.dirname(appPath), 'index.html'), 'utf8');
const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);

class StubElement {
  constructor(id) {
    this.id = id;
    this.innerHTML = '';
    this.textContent = '';
    this.value = '';
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    const classes = new Set();
    this.classList = {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => ((on === undefined ? !classes.has(c) : on) ?
                              classes.add(c) :
                              classes.delete(c)),
    };
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  fire(type, extra) {
    const event = Object.assign({preventDefault() {}, target: this}, extra || {});
    (this.listeners[type] || []).forEach((fn) => fn(event));
  }
  querySelector() {
    return new StubElement();
  }
  querySelectorAll() {
    return [];
  }
  appendChild(child) {
    return child;
  }
  getBoundingClientRect() {
    return {width: 10, height: 10};
  }
  requestSubmit() {
    this.fire('submit');
  }
  focus() {}
  click() {}
}

const els = Object.fromEntries(ids.map((id) => [id, new StubElement(id)]));
els.slicerDate.value = '30';
const missing = new Set();
const downloads = [];

global.document = {
  documentElement: {setAttribute() {}},
  getElementById(id) {
    if (!els[id]) {
      missing.add(id);
      els[id] = new StubElement(id);
    }
    return els[id];
  },
  querySelectorAll: () => [],
  createElement: () => new StubElement(),
};
global.window = {matchMedia: () => ({matches: false}), innerWidth: 1280, innerHeight: 800};
global.Blob = class {
  constructor(parts) {
    downloads.push(parts.join(''));
  }
};
Object.assign(URL, {createObjectURL: () => 'blob:stub', revokeObjectURL() {}});

require(appPath);

const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

check(missing.size === 0, `app.js reads missing element ids: ${[...missing].join(', ')}`);
check(/kpi-card/.test(els.kpiGrid.innerHTML), 'the default page renders KPI cards');
check(els.pageTitle.textContent === 'Agent Activity Overview', 'default page title');

const prompts = [
  ['Show p95 LLM latency and time to first token by model', 'Latency'],
  ['Token usage and cache hit rate by agent', 'Token Consumption & Caching'],
  ['Which tools fail most, with recent error messages', 'Errors'],
  ['Tool usage: tool calls by agent and the tool mix', 'Tool Usage'],
  ['Compare events by event type as a pie', 'Events by Event type'],
  ['Overview of sessions and users for the last 7 days', 'Agent Activity Overview'],
];
for (const [prompt, title] of prompts) {
  els.promptInput.value = prompt;
  els.promptForm.fire('submit');
  check(els.pageTitle.textContent === title,
      `"${prompt}" built "${els.pageTitle.textContent}", want "${title}"`);
  check(!/NaN|undefined/.test(els.kpiGrid.innerHTML + els.smartNarrative.innerHTML),
      `"${prompt}" renders NaN or undefined`);
}
check(els.slicerDate.value === '7', '"last 7 days" sets the window slicer');

els.btnShowSql.fire('click');
const sql = els.sqlModalCode.textContent;
check(sql.includes('CREATE TEMP TABLE events AS'), 'SQL creates the temp table');
check(sql.includes('FROM `your-project.your_dataset.agent_events`'), 'SQL reads the table');
check(sql.includes('INTERVAL 7 DAY'), 'SQL uses the selected window');
els.tableIdInput.value = 'bad`; DROP TABLE x; --.a.b';
els.tableIdInput.fire('input');
check(els.tableIdError.textContent !== '', 'an invalid table id shows an error');
check(els.sqlModalCode.textContent === sql, 'an invalid table id leaves the SQL unchanged');

const quotedAgent = 'o\'brien\\agent';
els.importText.value = JSON.stringify([
  {
    timestamp: '2026-07-16T10:00:00Z', event_type: 'LLM_RESPONSE', agent: quotedAgent,
    session_id: 's1', user_id: 'u1',
    latency_ms: {total_ms: 1200, time_to_first_token_ms: 300},
    attributes: {model: 'm1', usage_metadata: {prompt_token_count: 100,
      cached_content_token_count: 40, total_token_count: 150}},
  },
  {
    timestamp: '1784196000', event_type: 'TOOL_ERROR',
    agent: '<img src=x onerror=alert(1)>', session_id: 's2', user_id: 'u2',
    content: '{"tool":"run_query"}', latency_ms: '{"total_ms": 900}',
    status: 'ERROR', error_message: 'boom, "quoted"',
  },
  {event_type: 'LLM_RESPONSE'},
]);
els.btnApplyImport.fire('click');
check(/Loaded 2 rows; skipped 1/.test(els.importStatus.textContent),
    `import status: ${els.importStatus.textContent}`);
check(!els.slicerAgent.innerHTML.includes('<img'), 'agent names are escaped in the slicer');

els.slicerAgent.fire('change', {target: {value: quotedAgent}});
els.tableIdInput.value = 'my-project.my_dataset.agent_events';
els.btnShowSql.fire('click');
check(els.sqlModalCode.textContent.includes('agent = \'o\\\'brien\\\\agent\''),
    'agent names are escaped as SQL string literals');

els.slicerAgent.fire('change', {target: {value: 'ALL'}});
els.btnExportCsv.fire('click');
const csv = downloads[downloads.length - 1];
check(csv.includes('"boom, ""quoted"""'), 'CSV export quotes fields per RFC 4180');
els.importText.value = csv;
els.btnApplyImport.fire('click');
check(/Loaded 2 rows\./.test(els.importStatus.textContent),
    `CSV export re-imports: ${els.importStatus.textContent}`);

if (failures.length) {
  console.error(`FAILED:\n- ${failures.join('\n- ')}`);
  process.exit(1);
}
console.log(`ok: ${prompts.length} prompts, ${ids.length} element ids`);
