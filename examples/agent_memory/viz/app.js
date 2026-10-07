// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Renders the JSON written by export_memory.py. No dependencies; every
// string from the data is inserted with textContent.
(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const state = { data: null, user: null, sessionId: null, compare: 0 };

  const OUTCOME = {
    answered: { label: 'Answered', color: 'var(--good)', icon: '✓' },
    answered_with_errors: {
      label: 'Answered after an error',
      color: 'var(--warning)',
      icon: '!',
    },
    unanswered: { label: 'No answer', color: 'var(--critical)', icon: '✕' },
  };
  const TOOL_STATUS = {
    success: { label: 'ok', color: 'var(--good)', icon: '✓' },
    error: { label: 'failed', color: 'var(--critical)', icon: '✕' },
    pending: {
      label: 'no completion row',
      color: 'var(--warning)',
      icon: '…',
    },
  };
  // Entity types read in the graph; color stays at three hues (people,
  // entities, preferences), so type is carried by shape and caption.
  const TYPE_LABEL = {
    PERSON: 'Person',
    TEAM: 'Team',
    PRODUCT_CATEGORY: 'Product category',
    BRAND: 'Brand',
    DISTRIBUTION_CENTER: 'Distribution center',
    MARKET: 'Market',
    TRAFFIC_SOURCE: 'Traffic source',
    METRIC: 'Metric',
    EVENT: 'Event',
  };
  const TYPE_ORDER = Object.keys(TYPE_LABEL);
  const MAX_RING = 14; // entities drawn around the analyst
  const LITERAL_TYPES = new Set(['DATE', 'VALUE', 'TEXT', 'NUMBER', 'TIME', 'DURATION', 'PERCENTAGE', 'MONEY', 'CURRENCY', '']);

  // ---------------------------------------------------------------- DOM

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat()) {
      if (child === undefined || child === null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function svg(tag, attrs, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // --------------------------------------------------------- formatting

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pad = (n) => String(n).padStart(2, '0');

  function clock(iso) {
    const d = new Date(iso);
    return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
  }

  function when(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${clock(iso)}`;
  }

  function duration(ms) {
    if (ms === null || ms === undefined) return '–';
    if (ms < 1) return '<1 ms';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
  }

  const IRREGULAR = { query: 'queries', entity: 'entities' };
  const plural = (n, word) => {
    const many = IRREGULAR[word.split(' ').pop()] ? word.replace(/\w+$/, (w) => IRREGULAR[w]) : `${word}s`;
    return `${n.toLocaleString('en-US')} ${n === 1 ? word : many}`;
  };
  // The events table a live export read ("<project>.<dataset>.<table>").
  const eventsTable = () => {
    const source = (state.data && state.data.source) || '';
    return /^[^\s.]+\.[^\s.]+\.[\w-]+$/.test(source) ? source.split('.').pop() : 'agent_events';
  };
  const shortId = (id) => (id && id.length > 14 ? `${id.slice(0, 12)}…` : id || '');
  const humanize = (s) => String(s || '').replace(/_/g, ' ');

  // ------------------------------------------------------------- days

  function sessionDay(session) {
    const st = session.state || {};
    if (st.sim_day) {
      return { day: Number(st.sim_day), date: st.sim_date, weekday: st.sim_weekday };
    }
    return { day: null, date: (session.created_at || '').slice(0, 10), weekday: null };
  }

  function dayName(info) {
    if (!info.date) return '';
    const d = new Date(`${info.date}T12:00:00Z`);
    const weekday = (info.weekday || '').slice(0, 3);
    return `${weekday ? `${weekday} ` : ''}${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  }

  function dayLabel(info) {
    return info.day ? `Day ${info.day} · ${dayName(info)}` : dayName(info);
  }

  function findSession(sessionId) {
    for (const user of state.data.users) {
      const session = user.sessions.find((s) => s.session_id === sessionId);
      if (session) return { user, session };
    }
    return null;
  }

  // "Day 3, session 2" within its analyst's week.
  function sessionLabel(sessionId) {
    const found = findSession(sessionId);
    if (!found) return shortId(sessionId);
    const info = sessionDay(found.session);
    const sameDay = found.user.sessions.filter((s) => sessionDay(s).day === info.day && sessionDay(s).date === info.date);
    const index = sameDay.findIndex((s) => s.session_id === sessionId) + 1;
    return info.day ? `Day ${info.day}, session ${index}` : `${dayName(info)}, session ${index}`;
  }

  function userName(user) {
    return user.name || user.user_id;
  }

  // ------------------------------------------------------------ tooltip

  const tooltip = () => document.getElementById('tooltip');

  function showTip(anchor, value, rows, source, event) {
    const tip = tooltip();
    tip.replaceChildren(
      ...[
        el('span', { class: 'tip-value', text: value }),
        ...rows.filter(Boolean).map((row) => el('span', { class: 'tip-row', text: row })),
        source ? el('span', { class: 'tip-source', text: source }) : null,
      ].filter(Boolean),
    );
    tip.hidden = false;
    const box = anchor.getBoundingClientRect();
    const x = event && event.clientX ? event.clientX : box.left + box.width / 2;
    const y = event && event.clientY ? event.clientY : box.top;
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;
    let left = x + 14;
    if (left + width > window.innerWidth - 8) left = x - width - 14;
    let top = y - height - 12;
    if (top < 8) top = y + 18;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${top}px`;
  }

  function hideTip() {
    tooltip().hidden = true;
  }

  function bindTip(node, build) {
    const open = (event) => {
      const [value, rows, source] = build();
      showTip(node, value, rows, source, event);
    };
    node.addEventListener('pointermove', open);
    node.addEventListener('focus', () => open(null));
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('blur', hideTip);
  }

  function activate(node, action) {
    node.addEventListener('click', action);
    node.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        action();
      }
    });
  }

  // -------------------------------------------------------- Markdown-lite

  // Model answers use bold, italics, lists, headings, rules and pipe tables;
  // render those with DOM nodes only.
  function inline(text) {
    const nodes = [];
    // An escaped character, bold italics, bold, code, or italics (which may
    // contain bold and escapes).
    const pattern = /(\\[\\`*_]|\*\*\*[^*]+\*\*\*|\*\*[^*]+\*\*|`[^`]+`|\*(?![*\s])(?:\\.|\*\*[^*]+\*\*|[^*\\])+?\*(?!\*))/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) nodes.push(text.slice(last, match.index));
      const token = match[0];
      if (token.startsWith('\\')) nodes.push(token.slice(1));
      else if (token.startsWith('***')) nodes.push(el('strong', {}, el('em', { text: token.slice(3, -3) })));
      else if (token.startsWith('**')) nodes.push(el('strong', { text: token.slice(2, -2) }));
      else if (token.startsWith('`')) nodes.push(el('code', { text: token.slice(1, -1) }));
      else nodes.push(el('em', {}, ...inline(token.slice(1, -1))));
      last = match.index + token.length;
    }
    if (last < text.length) nodes.push(text.slice(last));
    return nodes;
  }

  const isRow = (line) => line.trim().startsWith('|');
  const isItem = (line) => /^\s*([-*]|\d+\.)\s+/.test(line);
  const isHeading = (line) => /^#{1,6}\s/.test(line.trim());
  const isRule = (line) => /^\s*([-*_])(\s*\1){2,}\s*$/.test(line);

  function markdownTable(rows) {
    const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
    const header = cells(rows[0]);
    const rule = rows.length > 1 && cells(rows[1]).every((c) => /^:?-{2,}:?$/.test(c)) ? cells(rows[1]) : null;
    const align = header.map((_, i) => (rule && /-:$/.test(rule[i] || '') ? 'num' : null));
    const body = rows.slice(rule ? 2 : 1).map(cells);
    return el(
      'div',
      { class: 'md-scroll' },
      el(
        'table',
        {},
        el('thead', {}, el('tr', {}, ...header.map((h, i) => el('th', { class: align[i] }, ...inline(h))))),
        el('tbody', {}, ...body.map((row) => el('tr', {}, ...row.map((c, i) => el('td', { class: align[i] }, ...inline(c)))))),
      ),
    );
  }

  function markdown(text) {
    const root = el('div', { class: 'md' });
    const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        i += 1;
      } else if (isRule(line)) {
        root.append(el('hr'));
        i += 1;
      } else if (isRow(line)) {
        const rows = [];
        while (i < lines.length && isRow(lines[i])) rows.push(lines[i++]);
        root.append(markdownTable(rows));
      } else if (isHeading(line)) {
        root.append(el('p', { class: 'md-h' }, ...inline(line.trim().replace(/^#+\s*/, ''))));
        i += 1;
      } else if (isItem(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const list = el(ordered ? 'ol' : 'ul');
        while (i < lines.length && isItem(lines[i])) {
          list.append(el('li', {}, ...inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ''))));
        }
        root.append(list);
      } else {
        const para = [];
        while (i < lines.length && lines[i].trim() && !isRow(lines[i]) && !isHeading(lines[i]) && !isItem(lines[i]) && !isRule(lines[i])) {
          para.push(lines[i++].trim());
        }
        root.append(el('p', {}, ...inline(para.join(' '))));
      }
    }
    return root;
  }

  // ---------------------------------------------------------- top level

  async function init() {
    const params = new URLSearchParams(window.location.search);
    const source = params.get('data') || 'data/memory_export.json';
    try {
      const response = await fetch(source, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      state.data = await response.json();
    } catch (error) {
      showNotice(source, error);
      return;
    }
    renderRunFacts();
    renderUserSwitch();
    bindToggle('graph-toggle', ['graph-figure'], 'graph-table');
    bindToggle('trace-toggle', ['traces', 'trace-legend'], 'trace-table');
    bindToggle('week-toggle', ['week-figure', 'week-legend'], 'week-table');
    const compare = Number(params.get('compare'));
    state.compare = Number.isInteger(compare) && compare >= 0 ? compare : 0;
    renderComparison();
    const users = state.data.users;
    const user = users.find((u) => u.user_id === params.get('user')) || users[0];
    selectUser(user.user_id, params.get('session'));
    let timer = null;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        renderWeek();
        renderGraph();
        renderTraces();
      }, 150);
    });
  }

  function showNotice(source, error) {
    const app = document.getElementById('app');
    app.replaceChildren(
      el(
        'div',
        { class: 'notice' },
        el('h2', { text: 'The memory export did not load' }),
        el('p', { text: `Tried ${source}: ${error.message}.` }),
        el(
          'p',
          {},
          'Browsers block reading local files from a page opened with file://. Serve this folder and open it over http: ',
          el('code', { text: 'python -m http.server 8000' }),
          ', then visit ',
          el('code', { text: 'http://localhost:8000/' }),
          '.',
        ),
      ),
    );
  }

  function renderRunFacts() {
    const data = state.data;
    const run = data.run;
    const rows = data.users.reduce((sum, u) => sum + u.sessions.reduce((n, s) => n + s.row_count, 0), 0);
    const facts = [['Run', data.label], ['Table', data.source]];
    if (run) {
      const t = run.totals;
      const days = run.days || [];
      if (days.length) {
        facts.push(['Week', `${dayName(days[0])} to ${dayName(days[days.length - 1])}, ${plural(days.length, 'business day')}`]);
      }
      facts.push(['Model', run.model]);
      facts.push(['Sessions', `${plural(t.sessions, 'session')} by ${plural(t.analysts, 'analyst')}, plus ${t.control_sessions} without memory`]);
      facts.push(['Rows', `${t.rows.toLocaleString('en-US')} in ${eventsTable()}, for all ${plural(t.sessions + t.control_sessions, 'session')}`]);
      facts.push(['SQL', `${plural(t.sql_queries, 'query')} in the sessions with memory, ${t.sql_errors} failed`]);
      facts.push(['Memory', `${plural(t.facts, 'fact')}, ${plural(t.entities, 'entity')}, ${plural(t.preference_versions, 'preference version')}`]);
      document.getElementById('lede').textContent =
        `A data-analyst agent's week at TheLook, rebuilt from the ${eventsTable()} rows the ADK BigQuery Agent Analytics plugin wrote. Every item points back to the row it came from.`;
    } else {
      facts.push(['Rows read', `${rows.toLocaleString('en-US')} across ${plural(data.users.length, 'user')}`]);
    }
    facts.push(['Exported', when(data.exported_at)]);
    document.getElementById('run-facts').replaceChildren(
      ...facts.flatMap(([term, value]) => [el('dt', { text: term }), el('dd', { text: value })]),
    );
    document.getElementById('footer').textContent =
      `Rendered from ${data.label} (${data.source}), export schema ${data.schema}. ` +
      'Regenerate with examples/agent_memory/export_memory.py.';
  }

  function renderUserSwitch() {
    const box = document.getElementById('user-switch');
    box.replaceChildren(
      ...state.data.users.map((user) =>
        el('button', {
          type: 'button',
          role: 'radio',
          'data-user': user.user_id,
          'aria-checked': 'false',
          text: userName(user),
          onclick: () => selectUser(user.user_id),
        }),
      ),
    );
  }

  function selectUser(userId, sessionId) {
    state.user = state.data.users.find((u) => u.user_id === userId);
    const valid = state.user.sessions.some((s) => s.session_id === sessionId);
    state.sessionId = valid ? sessionId : state.user.current_session_id;
    for (const button of document.querySelectorAll('#user-switch button')) {
      button.setAttribute('aria-checked', String(button.dataset.user === userId));
    }
    document.getElementById('filter-note').textContent =
      `Loaded with TraceFilter(user_id='${userId}'): no other analyst's rows are read.`;
    renderSessions();
    renderGraph();
    renderGraphTable();
    selectSession(state.sessionId);
  }

  function selectSession(sessionId) {
    state.sessionId = sessionId;
    const params = new URLSearchParams(window.location.search);
    params.set('user', state.user.user_id);
    params.set('session', sessionId);
    window.history.replaceState(null, '', `?${params.toString()}`);
    for (const button of document.querySelectorAll('.session-btn')) {
      button.setAttribute('aria-current', String(button.dataset.session === sessionId));
    }
    renderConversation();
    renderWeek();
    renderWeekTable();
    renderTraces();
    renderTraceTable();
    renderContext();
  }

  function openSession(sessionId) {
    const found = findSession(sessionId);
    if (!found) return;
    if (found.user !== state.user) selectUser(found.user.user_id, sessionId);
    else selectSession(sessionId);
  }

  function bindToggle(buttonId, figureIds, tableId) {
    const button = document.getElementById(buttonId);
    button.addEventListener('click', () => {
      const showTable = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(showTable));
      for (const id of figureIds) document.getElementById(id).hidden = showTable;
      document.getElementById(tableId).hidden = !showTable;
    });
  }

  function table(headers, rows) {
    return el(
      'table',
      {},
      el('thead', {}, el('tr', {}, ...headers.map((h) => el('th', { scope: 'col', text: h })))),
      el('tbody', {}, ...rows.map((row) => el('tr', {}, ...row.map((cell) => el('td', { text: cell }))))),
    );
  }

  // ------------------------------------------------------ before / after

  function renderComparison() {
    const list = state.data.comparisons || [];
    const panel = document.getElementById('compare-panel');
    panel.hidden = list.length === 0;
    if (!list.length) return;
    if (state.compare >= list.length) state.compare = 0;
    const tabs = document.getElementById('compare-tabs');
    tabs.replaceChildren(
      ...list.map((item, i) =>
        el('button', {
          type: 'button',
          role: 'tab',
          'aria-selected': String(i === state.compare),
          'data-compare': i,
          text: `${item.name.split(' ')[0]} · Day ${item.day}${item.control_read_memory ? ' · flawed control' : ''}`,
          onclick: () => {
            state.compare = i;
            renderComparison();
          },
        }),
      ),
    );
    const item = list[state.compare];
    const days = (state.data.run && state.data.run.days) || [];
    const day = days.find((d) => d.number === item.day);
    document.getElementById('compare-question').replaceChildren(
      el('span', { class: 'who', text: `${item.name}, ${day ? dayLabel({ day: day.number, date: day.date, weekday: day.weekday }) : `Day ${item.day}`}` }),
      `“${item.question}”`,
    );
    renderSide(document.getElementById('compare-without'), item.without_memory, false);
    renderSide(document.getElementById('compare-with'), item.with_memory, true);
  }

  function renderSide(box, side, withMemory) {
    if (!side) {
      box.replaceChildren(el('p', { class: 'hint', text: 'No trace recorded.' }));
      return;
    }
    const stats = [
      [plural(side.tool_calls.length, 'tool call'), null],
      [plural(side.sql_queries, 'SQL query'), side.sql_errors ? `${side.sql_errors} failed` : null],
      [duration(side.latency_ms), null],
      [plural(side.total_tokens, 'token'), null],
    ];
    const reads = side.memory_table_reads || [];
    const attempts = side.memory_table_attempts || [];
    const purposes = (queries) => `${queries.map((q) => q.purpose || 'no purpose given').slice(0, 3).join('; ')}${queries.length > 3 ? '; …' : ''}`;
    const recalled = (side.recalled_sessions || []).map((sid) =>
      el('button', { type: 'button', class: 'chip', text: sessionLabel(sid), onclick: () => openSession(sid) }),
    );
    box.replaceChildren(
      ...[
      el(
        'h3',
        {},
        withMemory ? 'With memory' : 'Without memory',
        el('span', { class: 'tag', text: withMemory ? 'memory tools on' : 'no memory tools' }),
      ),
      el(
        'p',
        { class: 'stat-row' },
        ...stats.map(([value, note]) => el('span', {}, el('strong', { text: value }), note ? ` (${note})` : '')),
      ),
      withMemory && recalled.length ? el('p', { class: 'recalled' }, 'Recalled from', ...recalled) : null,
      // A query of the memory tables counts as a read only if it returned
      // rows; one that failed or was refused is noted, not flagged.
      reads.length
        ? el(
          'p',
          { class: 'flag' },
          el('strong', { text: 'Not memory-free. ' }),
          `${plural(reads.length, 'SQL query')} read the memory tables (${purposes(reads)})${attempts.length ? `; ${attempts.length} more failed` : ''}. `
            + 'This run had no memory tools, but run_sql could still read the dataset that holds memory. run_sql now reads only TheLook.',
        )
        : attempts.length
          ? el(
            'p',
            { class: 'attempts' },
            el('strong', { text: 'No memory read. ' }),
            `${plural(attempts.length, 'SQL query')} named the memory tables (${purposes(attempts)}), and none returned rows: ${attempts[0].error || 'the query failed'}`,
          )
          : null,
      side.answer ? markdown(side.answer) : el('p', { class: 'hint', text: 'The turn ended without a text answer.' }),
      ].filter(Boolean),
    );
  }

  // ------------------------------------------------------------ the week

  function weekDays() {
    const run = state.data.run;
    if (run && run.days && run.days.length) {
      return run.days.map((d) => ({ day: d.number, date: d.date, weekday: d.weekday }));
    }
    const seen = new Map();
    for (const user of state.data.users) {
      for (const s of user.sessions) {
        const info = sessionDay(s);
        seen.set(`${info.day}|${info.date}`, info);
      }
    }
    return [...seen.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
  }

  function renderWeekLegend() {
    const items = [
      [(x) => svg('circle', { cx: x, cy: 8, r: 6, fill: 'var(--session)' }), 'Session with memory'],
      [(x) => svg('circle', { cx: x, cy: 8, r: 5.5, fill: 'var(--surface)', stroke: 'var(--muted)', 'stroke-width': 1.75 }), 'Same question, no memory'],
      [(x) => svg('path', { d: `M${x - 9} 11 Q${x} 1 ${x + 9} 11`, fill: 'none', stroke: 'var(--muted)', 'stroke-width': 1.5 }), 'Recall cited an earlier session'],
    ];
    document.getElementById('week-legend').replaceChildren(
      ...items.map(([draw, label]) => {
        const icon = svg('svg', { width: 22, height: 16, viewBox: '0 0 22 16', 'aria-hidden': 'true' });
        icon.append(draw(11));
        return el('span', { class: 'legend-item' }, icon, label);
      }),
    );
  }

  function renderWeek() {
    renderWeekLegend();
    const root = document.getElementById('week');
    root.replaceChildren();
    const users = state.data.users;
    const days = weekDays();
    const width = Math.max(640, root.parentElement.clientWidth);
    const labelW = 130;
    const rowH = 42;
    const top = 30 + rowH * 0.7; // room for the first row's arcs
    const colW = (width - labelW - 8) / Math.max(1, days.length);
    const height = top + users.length * rowH + 6;
    root.setAttribute('viewBox', `0 0 ${width} ${height}`);
    root.setAttribute('height', height);

    const defs = svg('defs');
    const arrow = svg('marker', { id: 'week-arrow', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse' });
    arrow.append(svg('path', { d: 'M0 0 L10 5 L0 10 z', fill: 'var(--muted)' }));
    defs.append(arrow);
    root.append(defs);

    days.forEach((info, i) => {
      const x = labelW + i * colW;
      root.append(svg('text', { x: x + colW / 2, y: 14, 'text-anchor': 'middle', 'font-size': 12, fill: 'var(--ink-2)', 'font-weight': 600 }, dayLabel(info)));
      if (i > 0) root.append(svg('line', { x1: x, y1: 24, x2: x, y2: height - 4, stroke: 'var(--grid)' }));
    });
    const controls = new Map();
    for (const item of state.data.comparisons || []) {
      if (item.without_memory) controls.set(item.with_memory && item.with_memory.session_id, item);
    }

    const pos = new Map();
    const arcs = svg('g');
    const dots = svg('g');
    root.append(arcs, dots);
    users.forEach((user, row) => {
      const y = top + row * rowH + rowH / 2;
      const current = user === state.user;
      root.append(
        svg('text', { x: 0, y, 'dominant-baseline': 'middle', 'font-size': 12.5, fill: current ? 'var(--ink)' : 'var(--ink-2)', 'font-weight': current ? 650 : 500 }, userName(user)),
      );
      root.append(svg('line', { x1: labelW, y1: y, x2: width - 8, y2: y, stroke: 'var(--grid)' }));
      days.forEach((info, col) => {
        const sessions = user.sessions.filter((s) => {
          const d = sessionDay(s);
          return d.day === info.day && d.date === info.date;
        });
        const marks = [];
        for (const s of sessions) {
          marks.push({ session: s, control: false });
          if (controls.has(s.session_id)) marks.push({ session: s, control: true, item: controls.get(s.session_id) });
        }
        const step = 18;
        const start = labelW + col * colW + colW / 2 - ((marks.length - 1) * step) / 2;
        marks.forEach((mark, k) => {
          const x = start + k * step;
          if (!mark.control) pos.set(mark.session.session_id, { x, y, user });
          dots.append(weekDot(mark, x, y, user));
        });
      });
    });

    // Arcs for the selected session: back to each session its recall cited.
    const selected = state.user.sessions.find((s) => s.session_id === state.sessionId);
    const from = selected && pos.get(selected.session_id);
    for (const target of (selected && selected.recalled_sessions) || []) {
      const to = pos.get(target);
      if (!from || !to) continue;
      // The apex of a quadratic arc is half its control lift: keep it in the band.
      const lift = Math.min(rowH * 0.65, 10 + Math.abs(from.x - to.x) / 12);
      arcs.append(
        svg('path', {
          d: `M${from.x} ${from.y - 7} Q${(from.x + to.x) / 2} ${from.y - 7 - lift * 2} ${to.x} ${to.y - 8}`,
          fill: 'none',
          stroke: 'var(--ink-2)',
          'stroke-width': 1.5,
          'marker-end': 'url(#week-arrow)',
        }),
      );
    }
  }

  function weekDot(mark, x, y, user) {
    const s = mark.session;
    const selected = !mark.control && s.session_id === state.sessionId;
    const group = svg('g', { class: 'node', tabindex: 0, role: 'button' });
    group.dataset.session = mark.control ? `${s.session_id}:control` : s.session_id;
    group.append(svg('circle', { class: 'node-hit', cx: x, cy: y, r: 12, fill: 'transparent' }));
    if (selected) group.append(svg('circle', { cx: x, cy: y, r: 10, fill: 'none', stroke: 'var(--session)', 'stroke-width': 1.5 }));
    group.append(
      mark.control
        ? svg('circle', { cx: x, cy: y, r: 5.5, fill: 'var(--surface)', stroke: 'var(--muted)', 'stroke-width': 1.75 })
        : svg('circle', { cx: x, cy: y, r: 6, fill: 'var(--session)', 'fill-opacity': user === state.user ? 1 : 0.45 }),
    );
    const first = s.messages.find((m) => m.role === 'user');
    const label = mark.control ? `${userName(user)}, ${sessionLabel(s.session_id)}, asked without memory` : `${userName(user)}, ${sessionLabel(s.session_id)}`;
    group.setAttribute('aria-label', label);
    bindTip(group, () => [
      mark.control ? 'Same question, no memory' : sessionLabel(s.session_id),
      [
        `${userName(user)}`,
        first ? `“${first.content.length > 140 ? `${first.content.slice(0, 139)}…` : first.content}”` : null,
        mark.control
          ? mark.item.control_read_memory
            ? 'Answered without memory tools, but its SQL read the memory tables; see the comparison above.'
            : 'Answered by the agent without memory tools; see the comparison above.'
          : (s.recalled_sessions || []).length ? `Recall cited ${(s.recalled_sessions || []).map(sessionLabel).join(', ')}` : 'Recall cited no earlier session',
      ],
      mark.control ? (mark.item.without_memory || {}).session_id : `${plural(s.row_count, 'row')} in ${eventsTable()}`,
    ]);
    activate(group, () => {
      if (mark.control) {
        const index = (state.data.comparisons || []).indexOf(mark.item);
        if (index >= 0) {
          state.compare = index;
          renderComparison();
          document.getElementById('compare-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      } else {
        openSession(s.session_id);
      }
    });
    return group;
  }

  function renderWeekTable() {
    const rows = [];
    for (const user of state.data.users) {
      for (const s of user.sessions) {
        const first = s.messages.find((m) => m.role === 'user');
        rows.push([
          userName(user),
          dayLabel(sessionDay(s)),
          s.session_id,
          first ? first.content : '',
          (s.recalled_sessions || []).map(sessionLabel).join(', '),
          String(s.row_count),
        ]);
      }
    }
    document.getElementById('week-table').replaceChildren(
      table(['Analyst', 'Day', 'Session', 'First message', 'Recall cited', 'Rows'], rows),
    );
  }

  // ------------------------------------------------- short-term memory

  function renderSessions() {
    const box = document.getElementById('session-list');
    const groups = new Map();
    for (const s of state.user.sessions) {
      const info = sessionDay(s);
      const key = `${info.day}|${info.date}`;
      if (!groups.has(key)) groups.set(key, { info, sessions: [] });
      groups.get(key).sessions.push(s);
    }
    box.replaceChildren(
      ...[...groups.values()].flatMap(({ info, sessions }) => [
        el('p', { class: 'day-head', text: dayLabel(info) }),
        el(
          'ol',
          { class: 'session-list' },
          ...sessions.map((session, i) => {
            const first = session.messages.find((m) => m.role === 'user');
            const turns = state.user.traces.filter((t) => t.session_id === session.session_id).length;
            return el(
              'li',
              {},
              el(
                'button',
                {
                  type: 'button',
                  class: 'session-btn',
                  'data-session': session.session_id,
                  'aria-current': 'false',
                  onclick: () => selectSession(session.session_id),
                },
                el('span', { class: 'session-dot', 'aria-hidden': 'true', text: String(i + 1) }),
                el('span', { class: 'session-meta', text: `${clock(session.created_at)} · ${plural(turns, 'turn')} · ${plural(session.row_count, 'row')}` }),
                el('span', { class: 'session-preview', text: first ? first.content : '(no user message)' }),
              ),
            );
          }),
        ),
      ]),
    );
  }

  function renderConversation() {
    const session = state.user.sessions.find((s) => s.session_id === state.sessionId);
    document.getElementById('conversation-h').textContent = `${sessionLabel(state.sessionId)}`;
    document.getElementById('conversation-hint').textContent =
      `${session.session_id}: user messages and the text parts of the model's replies.`;
    document.getElementById('messages').replaceChildren(
      ...session.messages.map((message) =>
        el(
          'li',
          { class: `msg ${message.role}` },
          el('span', {
            class: 'msg-role',
            text: `${message.role === 'user' ? userName(state.user) : 'Agent'}${message.complete === false ? ' (reply incomplete: the stream did not finish)' : ''}, ${clock(message.timestamp)}`,
          }),
          message.role === 'user' ? message.content : markdown(message.content),
        ),
      ),
    );
  }

  // -------------------------------------------------- long-term memory

  function entityKey(name) {
    return String(name || '').trim().replace(/\s+/g, ' ').toLowerCase();
  }

  function isSelf(user, name) {
    const key = entityKey(name);
    return key === entityKey(user.name) || key === entityKey(user.user_id) || key === 'the speaker' || key === 'the user';
  }

  // Nodes: the analyst in the middle, entities around. Facts whose object is
  // a literal (a date, a definition) are listed, not drawn as nodes.
  function memoryModel(user) {
    const nodes = new Map();
    const center = { id: 'self', name: userName(user), type: 'PERSON', mentions: [], facts: [], self: true };
    const ensure = (name, type) => {
      if (isSelf(user, name)) return center;
      const key = entityKey(name);
      if (!key) return null;
      if (!nodes.has(key)) nodes.set(key, { id: `e:${key}`, name: String(name).trim(), type: String(type || '').toUpperCase(), mentions: [], facts: [] });
      const node = nodes.get(key);
      if (!TYPE_LABEL[node.type] && TYPE_LABEL[String(type || '').toUpperCase()]) node.type = String(type).toUpperCase();
      return node;
    };
    for (const entity of user.entities) {
      const node = ensure(entity.name, entity.entity_type);
      if (node) node.mentions.push(...entity.mentions);
    }
    const edges = [];
    for (const fact of user.facts) {
      const subject = ensure(fact.subject, fact.subject_type);
      if (!subject) continue;
      subject.facts.push(fact);
      if (LITERAL_TYPES.has(String(fact.object_type || '').toUpperCase())) continue;
      const object = ensure(fact.object, fact.object_type);
      if (!object || object === subject) continue;
      object.facts.push(fact);
      edges.push({ from: subject, to: object, fact });
    }
    // Keep the ring readable: entities in facts first, then the most
    // mentioned. The rest stay in the table view.
    const ranked = [...nodes.values()].sort((a, b) => b.facts.length - a.facts.length || b.mentions.length - a.mentions.length || a.name.localeCompare(b.name));
    const shown = new Set(ranked.slice(0, MAX_RING).map((n) => n.id));
    const kept = edges.filter((e) => (e.from.self || shown.has(e.from.id)) && (e.to.self || shown.has(e.to.id)));
    const linked = new Set(kept.flatMap((e) => [e.from.id, e.to.id]));
    for (const node of ranked) {
      if (shown.has(node.id) && !linked.has(node.id)) kept.push({ from: center, to: node, fact: null });
    }
    const ring = ranked.filter((n) => shown.has(n.id)).sort((a, b) => {
      const ta = TYPE_ORDER.indexOf(a.type);
      const tb = TYPE_ORDER.indexOf(b.type);
      return (ta < 0 ? 99 : ta) - (tb < 0 ? 99 : tb) || a.name.localeCompare(b.name);
    });
    return { center, ring, edges: kept, hidden: ranked.length - ring.length };
  }

  // Splits a long name into lines of at most `width` characters.
  function wrapLabel(name, width) {
    const lines = [];
    for (const word of String(name).split(/\s+/)) {
      const last = lines[lines.length - 1];
      if (last !== undefined && `${last} ${word}`.length <= width) lines[lines.length - 1] = `${last} ${word}`;
      else lines.push(word);
    }
    return lines.slice(0, 3);
  }

  function nodeShape(node, x, y) {
    if (node.self) {
      return svg('circle', { cx: x, cy: y, r: 13, fill: 'var(--session)', stroke: 'var(--surface)', 'stroke-width': 2.5 });
    }
    if (node.type === 'PERSON' || node.type === 'TEAM') {
      return svg('circle', { cx: x, cy: y, r: 7, fill: 'var(--session)', stroke: 'var(--surface)', 'stroke-width': 2 });
    }
    if (node.type === 'EVENT') {
      return svg('rect', { x: x - 6, y: y - 6, width: 12, height: 12, rx: 2, transform: `rotate(45 ${x} ${y})`, fill: 'var(--entity)', stroke: 'var(--surface)', 'stroke-width': 2 });
    }
    return svg('rect', { x: x - 6.5, y: y - 6.5, width: 13, height: 13, rx: 3.5, fill: 'var(--entity)', stroke: 'var(--surface)', 'stroke-width': 2 });
  }

  function renderGraphLegend() {
    const items = [
      [{ self: true }, 'Analyst'],
      [{ type: 'PERSON' }, 'Person or team'],
      [{ type: 'BRAND' }, 'Entity (category, brand, place, metric)'],
      [{ type: 'EVENT' }, 'Event'],
    ];
    document.getElementById('graph-legend').replaceChildren(
      ...items.map(([node, label]) => {
        const icon = svg('svg', { width: 26, height: 26, viewBox: '0 0 26 26', 'aria-hidden': 'true' });
        icon.append(nodeShape(node, 13, 13));
        return el('span', { class: 'legend-item' }, icon, label);
      }),
    );
  }

  function renderGraph() {
    renderGraphLegend();
    const root = document.getElementById('graph');
    root.replaceChildren();
    const user = state.user;
    const model = memoryModel(user);
    const width = Math.max(420, root.parentElement.clientWidth);
    const count = model.ring.length;
    // Labels sit outside the ring, so keep room for them on both sides.
    const radius = Math.max(80, Math.min(width / 2 - 165, 70 + count * 10));
    const height = Math.max(220, 2 * radius + 96);
    const cx = width / 2;
    const cy = height / 2;
    root.setAttribute('viewBox', `0 0 ${width} ${height}`);
    root.setAttribute('height', height);
    model.center.x = cx;
    model.center.y = cy;
    model.ring.forEach((node, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, count);
      node.angle = angle;
      node.x = cx + radius * Math.cos(angle);
      node.y = cy + radius * Math.sin(angle);
    });

    const edgeLayer = svg('g');
    const labelLayer = svg('g');
    const nodeLayer = svg('g');
    root.append(edgeLayer, labelLayer, nodeLayer);
    const edgeEls = [];
    for (const edge of model.edges) {
      const fromCenter = edge.from.self || edge.to.self;
      const path = fromCenter
        ? svg('line', { x1: edge.from.x, y1: edge.from.y, x2: edge.to.x, y2: edge.to.y })
        : svg('path', { d: `M${edge.from.x} ${edge.from.y} Q${(edge.from.x + edge.to.x) / 2 + (cx - (edge.from.x + edge.to.x) / 2) * 0.35} ${(edge.from.y + edge.to.y) / 2 + (cy - (edge.from.y + edge.to.y) / 2) * 0.35} ${edge.to.x} ${edge.to.y}`, fill: 'none' });
      path.setAttribute('stroke', 'var(--axis)');
      path.setAttribute('stroke-width', edge.fact ? 1.5 : 1);
      if (!edge.fact) path.setAttribute('stroke-opacity', 0.6);
      path.dataset.a = edge.from.id;
      path.dataset.b = edge.to.id;
      edgeLayer.append(path);
      edgeEls.push(path);
      if (edge.fact) {
        const lx = (edge.from.x + edge.to.x) / 2;
        const ly = (edge.from.y + edge.to.y) / 2;
        // Along the spoke, kept upright, so it never crosses a node label.
        let angle = (Math.atan2(edge.to.y - edge.from.y, edge.to.x - edge.from.x) * 180) / Math.PI;
        if (angle > 90) angle -= 180;
        if (angle < -90) angle += 180;
        const label = svg('text', {
          x: lx, y: ly - 4, 'text-anchor': 'middle', 'font-size': 10.5, fill: 'var(--ink-2)',
          stroke: 'var(--surface)', 'stroke-width': 3, 'paint-order': 'stroke',
          transform: fromCenter ? `rotate(${angle.toFixed(1)} ${lx} ${ly})` : null,
        }, humanize(edge.fact.predicate));
        label.dataset.a = edge.from.id;
        label.dataset.b = edge.to.id;
        labelLayer.append(label);
        edgeEls.push(label);
      }
    }

    const groups = [];
    const addNode = (node) => {
      const group = svg('g', { class: 'node', tabindex: 0, role: 'img' });
      group.dataset.id = node.id;
      group.append(svg('circle', { class: 'node-hit', cx: node.x, cy: node.y, r: 14, fill: 'transparent' }));
      group.append(nodeShape(node, node.x, node.y));
      const typeLabel = node.self ? 'Analyst' : TYPE_LABEL[node.type] || humanize(node.type).toLowerCase() || 'Entity';
      if (node.self) {
        group.append(svg('text', { x: node.x, y: node.y + 30, 'text-anchor': 'middle', 'font-size': 13, 'font-weight': 650, fill: 'var(--ink)', stroke: 'var(--surface)', 'stroke-width': 3, 'paint-order': 'stroke' }, node.name));
      } else {
        const cos = Math.cos(node.angle);
        const anchor = cos > 0.25 ? 'start' : cos < -0.25 ? 'end' : 'middle';
        const dx = anchor === 'start' ? 12 : anchor === 'end' ? -12 : 0;
        const dy = anchor === 'middle' ? (Math.sin(node.angle) < 0 ? -16 : 20) : -1;
        const lines = wrapLabel(node.name, 20);
        const text = svg('text', { x: node.x + dx, y: node.y + dy, 'text-anchor': anchor, 'font-size': 12.5, fill: 'var(--ink)', stroke: 'var(--surface)', 'stroke-width': 3, 'paint-order': 'stroke' });
        lines.forEach((line, i) => text.append(svg('tspan', { x: node.x + dx, dy: i ? 14 : 0 }, line)));
        group.append(text);
        group.append(svg('text', { x: node.x + dx, y: node.y + dy + 13 + 14 * (lines.length - 1), 'text-anchor': anchor, 'font-size': 10.5, fill: 'var(--muted)' }, `${typeLabel}${node.mentions.length ? ` · ${plural(node.mentions.length, 'mention')}` : ''}`));
      }
      const days = [...new Set(node.mentions.map((m) => sessionLabel(m.session_id).split(',')[0]))];
      group.setAttribute('aria-label', `${node.name}, ${typeLabel}`);
      bindTip(group, () => [
        node.name,
        [
          typeLabel,
          ...node.facts.slice(0, 4).map((f) => f.statement || `${f.subject} ${humanize(f.predicate)} ${f.object}`),
          node.mentions.length ? `Mentioned on ${days.join(', ')}` : null,
        ],
        node.mentions.length ? `First from span ${shortId(node.mentions[0].span_id)}` : null,
      ]);
      group.addEventListener('pointerenter', () => highlight(node.id));
      group.addEventListener('focus', () => highlight(node.id));
      group.addEventListener('pointerleave', () => highlight(null));
      group.addEventListener('blur', () => highlight(null));
      nodeLayer.append(group);
      groups.push(group);
    };
    model.ring.forEach(addNode);
    addNode(model.center);
    if (!model.ring.length) {
      root.append(svg('text', { x: cx, y: cy + 52, 'text-anchor': 'middle', 'font-size': 12.5, fill: 'var(--muted)' }, 'No extracted entities yet'));
    }
    if (model.hidden) {
      root.append(svg('text', { x: width - 4, y: height - 6, 'text-anchor': 'end', 'font-size': 11.5, fill: 'var(--muted)' }, `+${model.hidden} more in the table view`));
    }

    function highlight(id) {
      const linked = new Set(id ? [id] : []);
      if (id) {
        for (const e of edgeEls) {
          if (e.dataset.a === id) linked.add(e.dataset.b);
          if (e.dataset.b === id) linked.add(e.dataset.a);
        }
      }
      for (const e of edgeEls) {
        const hot = id && (e.dataset.a === id || e.dataset.b === id);
        e.classList.toggle('dimmed', Boolean(id) && !hot);
      }
      for (const g of groups) g.classList.toggle('dimmed', Boolean(id) && !linked.has(g.dataset.id));
    }
    renderMemoryLists();
  }

  function sourceButton(text, sessionId, spanId, extra) {
    return el(
      'button',
      { type: 'button', class: 'link', onclick: () => selectSession(sessionId), title: `Open ${sessionLabel(sessionId)}` },
      el('span', { class: 'stmt', text }),
      ' ',
      el('span', { class: 'src', text: `[${shortId(spanId)}]` }),
      extra || null,
    );
  }

  function renderMemoryLists() {
    const user = state.user;
    const facts = [...user.facts];
    const seen = new Set();
    const distinct = [];
    for (const f of facts.reverse()) {
      const key = [f.subject, f.predicate, f.object].map(entityKey).join('|');
      if (seen.has(key)) continue;
      seen.add(key);
      distinct.push(f);
    }
    distinct.reverse();
    const byCategory = new Map();
    for (const p of user.preferences) {
      if (!byCategory.has(p.category)) byCategory.set(p.category, []);
      byCategory.get(p.category).push(p);
    }
    const dayOf = (sessionId) => sessionLabel(sessionId).split(',')[0];
    document.getElementById('memory-lists').replaceChildren(
      el(
        'div',
        {},
        el('h3', { text: `Preferences the agent saved (${user.preferences.length} versions)` }),
        el(
        'ul',
        { class: 'item-list' },
        ...[...byCategory.entries()].map(([category, versions]) =>
          el(
            'li',
            {},
            el('span', { class: 'when', text: dayOf(versions[versions.length - 1].session_id) }),
            el(
              'span',
              { class: 'pref-versions' },
              el('span', { class: 'pref-key', text: `${humanize(category)}:` }),
              ...versions.flatMap((p, i) => {
                const version = sourceButton(String(p.preference), p.session_id, p.span_id);
                // A replaced version is struck through; the last one is current.
                if (p.valid_until) version.querySelector('.stmt').classList.add('pref-old');
                return i ? [el('span', { class: 'src', text: '→' }), version] : [version];
              }),
            ),
          ),
        ),
      ),
      ),
      el(
        'div',
        {},
        el('h3', { text: `Facts extracted from the conversations (${distinct.length})` }),
        distinct.length
          ? el(
              'ul',
              { class: 'item-list' },
              ...distinct.map((f) =>
                el('li', {}, el('span', { class: 'when', text: dayOf(f.session_id) }), sourceButton(f.statement || `${f.subject} ${humanize(f.predicate)} ${f.object}`, f.session_id, f.span_id)),
              ),
            )
          : el('p', { class: 'hint', text: 'None yet: facts are extracted after each day.' }),
      ),
    );
  }

  function renderGraphTable() {
    const user = state.user;
    const rows = [
      ...user.preferences.map((p) => [
        'Preference',
        `${p.category} = ${p.preference}`,
        sessionLabel(p.session_id),
        p.valid_until ? `replaced ${when(p.valid_until)}` : 'current',
        `STATE_DELTA span ${p.span_id}`,
      ]),
      ...user.facts.map((f) => [
        'Fact',
        f.statement || `${f.subject} ${f.predicate} ${f.object}`,
        sessionLabel(f.session_id),
        `${f.subject} → ${humanize(f.predicate)} → ${f.object}`,
        `USER_MESSAGE_RECEIVED span ${f.span_id}`,
      ]),
      ...user.entities.map((e) => [
        `Entity (${TYPE_LABEL[e.entity_type] || e.entity_type})`,
        e.name,
        [...new Set(e.mentions.map((m) => sessionLabel(m.session_id)))].join('; '),
        plural(e.mentions.length, 'mention'),
        `span ${e.mentions[0] ? e.mentions[0].span_id : ''}`,
      ]),
    ];
    document.getElementById('graph-table').replaceChildren(table(['Kind', 'Item', 'Where', 'Detail', 'Source row'], rows));
  }

  // ---------------------------------------------------- reasoning traces

  function renderTraceLegend() {
    const items = [
      ['var(--model)', null, 'Model call'],
      ['var(--good)', '✓', 'Tool call ok'],
      ['var(--critical)', '✕', 'Tool call failed'],
      ['var(--warning)', '…', 'No completion row'],
    ];
    document.getElementById('trace-legend').replaceChildren(
      ...items.map(([color, icon, label]) => {
        const swatch = svg('svg', { width: 22, height: 12, viewBox: '0 0 22 12', 'aria-hidden': 'true' });
        swatch.append(svg('rect', { x: 0, y: 2, width: 22, height: 8, rx: 3, fill: color }));
        return el('span', { class: 'legend-item' }, swatch, icon ? `${icon} ${label}` : label);
      }),
    );
  }

  function niceStep(span) {
    const raw = span / 5;
    const power = 10 ** Math.floor(Math.log10(raw || 1));
    for (const factor of [1, 2, 5, 10]) {
      if (raw <= factor * power) return factor * power;
    }
    return 10 * power;
  }

  function tick(ms) {
    return ms < 1000 ? `${Math.round(ms)} ms` : `${+(ms / 1000).toFixed(1)} s`;
  }

  function renderTraces() {
    renderTraceLegend();
    const box = document.getElementById('traces');
    box.replaceChildren();
    const traces = state.user.traces.filter((t) => t.session_id === state.sessionId);
    document.getElementById('trace-h').textContent = `Reasoning in ${sessionLabel(state.sessionId)}`;
    if (!traces.length) {
      box.append(el('p', { class: 'hint', text: 'No traces recorded for this session.' }));
      return;
    }
    traces.forEach((trace, i) => box.append(renderTrace(trace, i)));
  }

  function renderTrace(trace, index) {
    const outcome = OUTCOME[trace.outcome_status] || OUTCOME.unanswered;
    const tools = trace.timeline.filter((r) => r.kind === 'tool').length;
    const sql = trace.sql_queries ? `, ${plural(trace.sql_queries, 'SQL query')}${trace.sql_errors ? ` (${trace.sql_errors} failed)` : ''}` : '';
    const recalled = trace.recall ? trace.recall.sessions : [];
    const article = el(
      'article',
      { class: 'trace', 'aria-label': `Turn ${index + 1}` },
      el(
        'div',
        { class: 'trace-head' },
        el('span', { class: 'trace-turn', text: `Turn ${index + 1}` }),
        el(
          'span',
          { class: 'status-chip' },
          el('span', { class: 'mark', style: `background:${outcome.color}`, 'aria-hidden': 'true' }),
          `${outcome.icon} ${outcome.label}`,
        ),
        el('span', {
          class: 'trace-facts',
          text: `${duration(trace.latency_ms)}, ${plural(trace.llm_calls, 'model call')}, ${plural(tools, 'tool call')}${sql}, ${plural(trace.total_tokens, 'token')}`,
        }),
        el('p', { class: 'trace-task', text: `“${trace.task || ''}”` }),
        recalled.length
          ? el('p', { class: 'recalled' }, 'Recall cited', ...recalled.map((sid) => el('button', { type: 'button', class: 'chip', text: sessionLabel(sid), onclick: () => openSession(sid) })))
          : null,
      ),
    );
    const figure = el('div', { class: 'figure' });
    article.append(figure);
    const outcomeBox = el('div', { class: 'trace-outcome' }, el('strong', { text: trace.outcome ? 'Answer' : 'No answer' }));
    outcomeBox.append(trace.outcome ? markdown(trace.outcome) : el('p', { text: trace.errors[0] || 'the turn ended without a text reply.' }));
    article.append(outcomeBox);
    const width = Math.max(520, document.getElementById('traces').clientWidth);
    figure.append(waterfall(trace, width));
    return article;
  }

  function toolDetail(row) {
    return row.detail ? (row.detail.length > 260 ? `${row.detail.slice(0, 259)}…` : row.detail) : null;
  }

  function waterfall(trace, width) {
    const rows = trace.timeline;
    const ROW = 24;
    const top = 4;
    const axisH = 24;
    const labelW = Math.min(250, Math.round(width * 0.3));
    const x0 = labelW + 10;
    const x1 = width - 64;
    const end = Math.max(trace.latency_ms || 0, ...rows.map((r) => (r.end_ms === null ? r.start_ms : r.end_ms)), 1);
    const scale = (ms) => x0 + (ms / end) * (x1 - x0);
    const height = top + rows.length * ROW + axisH;
    const root = svg('svg', { viewBox: `0 0 ${width} ${height}`, height, role: 'group', 'aria-label': `Timeline of ${rows.length} calls` });

    const step = niceStep(end);
    for (let v = 0; v <= end + 1e-9; v += step) {
      const x = scale(v);
      root.append(
        svg('line', { x1: x, y1: top, x2: x, y2: top + rows.length * ROW, stroke: 'var(--grid)', 'stroke-width': 1 }),
        svg('text', { x, y: height - 6, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--muted)' }, tick(v)),
      );
    }
    root.append(svg('line', { x1: x0, y1: top + rows.length * ROW, x2: x1, y2: top + rows.length * ROW, stroke: 'var(--axis)' }));

    let parentY = null;
    rows.forEach((row, j) => {
      const y = top + j * ROW + ROW / 2;
      const isTool = row.kind === 'tool';
      const status = isTool ? TOOL_STATUS[row.status] || TOOL_STATUS.pending : null;
      const group = svg('g', { tabindex: 0, role: 'img' });
      if (isTool && parentY !== null) {
        group.append(svg('path', { d: `M8 ${parentY + 7} L8 ${y} L18 ${y}`, fill: 'none', stroke: 'var(--axis)' }));
      }
      if (!isTool) parentY = y;
      const labelX = isTool ? 24 : 0;
      if (isTool) {
        group.append(svg('circle', { cx: labelX + 6, cy: y, r: 6, fill: status.color }));
        group.append(svg('text', { x: labelX + 6, y: y + 0.5, 'text-anchor': 'middle', 'dominant-baseline': 'middle', 'font-size': 8.5, 'font-weight': 700, fill: '#fff' }, status.icon));
      }
      const incomplete = !isTool && row.status === 'incomplete';
      const label = isTool ? row.label : `Model: ${row.label}${incomplete ? ' (incomplete)' : ''}`;
      group.append(
        svg('text', {
          x: labelX + (isTool ? 18 : 0), y, 'dominant-baseline': 'middle', 'font-size': 12,
          fill: isTool ? 'var(--ink)' : 'var(--ink-2)', 'font-weight': isTool ? 600 : null,
        }, label.length > 32 ? `${label.slice(0, 31)}…` : label),
      );
      const start = scale(row.start_ms);
      const stop = scale(row.end_ms === null ? row.start_ms : row.end_ms);
      const barW = Math.max(3, stop - start);
      group.append(svg('rect', {
        x: start, y: y - 6, width: barW, height: 12, rx: 3,
        fill: isTool ? status.color : 'var(--model)',
        'fill-opacity': incomplete ? 0.45 : null,
      }));
      const span = row.end_ms === null ? null : row.end_ms - row.start_ms;
      group.append(svg('text', {
        x: start + barW + 6, y, 'dominant-baseline': 'middle', 'font-size': 11, fill: 'var(--muted)',
      }, row.end_ms === null ? 'no completion row' : duration(span)));
      group.append(svg('rect', { x: 0, y: y - ROW / 2, width, height: ROW, fill: 'transparent' }));
      group.setAttribute('aria-label', `${label}, ${isTool ? status.label : `model call ${row.status}`}, ${duration(span)}`);
      bindTip(group, () => [
        row.end_ms === null ? 'No completion row' : duration(span),
        [
          isTool
            ? `${row.label} ${status.label}`
            : `Model call (${row.label})${incomplete ? ': no terminal response row, so not an answer' : ''}`,
          toolDetail(row),
          `Started ${duration(row.start_ms)} into the turn`,
        ],
        `${isTool ? 'TOOL_*' : 'LLM_*'} rows, span ${shortId(row.span_id)}`,
      ]);
      root.append(group);
    });
    return root;
  }

  function renderTraceTable() {
    const rows = [];
    state.user.traces
      .filter((t) => t.session_id === state.sessionId)
      .forEach((trace, i) => {
        for (const row of trace.timeline) {
          rows.push([
            `Turn ${i + 1}`,
            row.kind === 'tool' ? 'Tool call' : 'Model call',
            row.label,
            duration(row.start_ms),
            row.end_ms === null ? '–' : duration(row.end_ms - row.start_ms),
            row.kind === 'tool' ? (TOOL_STATUS[row.status] || TOOL_STATUS.pending).label : row.status,
            row.detail || '',
            row.span_id || '',
          ]);
        }
      });
    document.getElementById('trace-table').replaceChildren(
      table(['Turn', 'Kind', 'Name', 'Starts at', 'Duration', 'Status', 'Detail', 'Span'], rows),
    );
  }

  // ------------------------------------------------------------ context

  function renderContext() {
    const user = state.user;
    const trace = user.traces.find((t) => t.session_id === state.sessionId && t.recall);
    if (trace) {
      document.getElementById('context-h').textContent = `What the agent read in ${sessionLabel(state.sessionId)}`;
      document.getElementById('context-hint').textContent =
        'What recall_memory returned at the start of this turn, read from BigQuery: saved preferences, extracted facts and entities, similar past analyses with the SQL that answered them, and earlier failures. Each line names the session and span it came from.';
      document.getElementById('context').textContent = trace.recall.text;
      return;
    }
    document.getElementById('context-h').textContent = 'What the agent reads next';
    document.getElementById('context-hint').textContent =
      `The get_context() block for ${sessionLabel(user.current_session_id)}, the latest session: all three layers in one prompt, each line tagged with the session and span of the rows it came from.`;
    document.getElementById('context').textContent = user.context;
  }

  init();
})();
