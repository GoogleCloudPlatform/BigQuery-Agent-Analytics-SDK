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
  const state = { data: null, user: null, sessionId: null };

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

  const plural = (n, word) => `${n.toLocaleString('en-US')} ${word}${n === 1 ? '' : 's'}`;
  const shortId = (id) => (id && id.length > 14 ? `${id.slice(0, 12)}…` : id || '');

  function sessionLabel(sessionId) {
    const index = state.user.sessions.findIndex((s) => s.session_id === sessionId);
    return index < 0 ? sessionId : `Session ${index + 1}`;
  }

  // ------------------------------------------------------------ tooltip

  const tooltip = () => document.getElementById('tooltip');

  function showTip(anchor, value, rows, source, event) {
    const tip = tooltip();
    tip.replaceChildren(
      el('span', { class: 'tip-value', text: value }),
      ...rows.filter(Boolean).map((row) => el('span', { class: 'tip-row', text: row })),
      source ? el('span', { class: 'tip-source', text: source }) : null,
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
    bindToggle('graph-toggle', ['graph-figure', 'graph-legend'], 'graph-table');
    bindToggle('trace-toggle', ['traces', 'trace-legend'], 'trace-table');
    const users = state.data.users;
    const user = users.find((u) => u.user_id === params.get('user')) || users[0];
    selectUser(user.user_id, params.get('session'));
    let timer = null;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
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
    const rows = data.users.reduce(
      (sum, u) => sum + u.sessions.reduce((n, s) => n + s.row_count, 0),
      0,
    );
    const facts = [
      ['Run', data.label],
      ['Table', data.source],
      ['Exported', when(data.exported_at)],
      ['Rows read', `${rows.toLocaleString('en-US')} across ${plural(data.users.length, 'user')}`],
    ];
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
          text: user.user_id,
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
      `Loaded with TraceFilter(user_id='${userId}'): no other user's rows are read.`;
    renderSessions();
    renderGraph();
    renderGraphTable();
    renderContext();
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
    renderGraph();
    renderTraces();
    renderTraceTable();
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

  // ------------------------------------------------- short-term memory

  function renderSessions() {
    const list = document.getElementById('session-list');
    list.replaceChildren(
      ...state.user.sessions.map((session, i) => {
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
            el(
              'span',
              {},
              el('span', { class: 'session-title', text: `Session ${i + 1}` }),
              ' ',
              el('span', {
                class: 'session-meta',
                text: `${clock(session.created_at)}, ${plural(turns, 'turn')}, ${plural(session.row_count, 'row')}`,
              }),
            ),
            el('span', { class: 'session-preview', text: first ? first.content : '(no user message)' }),
          ),
        );
      }),
    );
  }

  function renderConversation() {
    const session = state.user.sessions.find((s) => s.session_id === state.sessionId);
    document.getElementById('conversation-h').textContent = `${sessionLabel(state.sessionId)} conversation`;
    document.getElementById('conversation-hint').textContent =
      `${session.session_id}: user messages and the text parts of the model's replies.`;
    document.getElementById('messages').replaceChildren(
      ...session.messages.map((message) =>
        el(
          'li',
          { class: `msg ${message.role}` },
          el('span', {
            class: 'msg-role',
            text: `${message.role === 'user' ? 'User' : 'Agent'}${message.complete === false ? ' (reply incomplete: the stream did not finish)' : ''}, ${clock(message.timestamp)}`,
          }),
          message.content,
        ),
      ),
    );
  }

  // -------------------------------------------------- long-term memory

  function marker(kind, x, y, hollow) {
    if (kind === 'session') {
      return svg('circle', { cx: x, cy: y, r: 7, fill: 'var(--session)', stroke: 'var(--surface)', 'stroke-width': 2.5 });
    }
    if (kind === 'pref') {
      return svg('rect', {
        x: x - 6, y: y - 6, width: 12, height: 12, rx: 3,
        fill: hollow ? 'var(--surface)' : 'var(--pref)',
        stroke: hollow ? 'var(--pref)' : 'var(--surface)',
        'stroke-width': hollow ? 2 : 2.5,
      });
    }
    return svg('rect', {
      x: x - 5.5, y: y - 5.5, width: 11, height: 11, rx: 2,
      transform: `rotate(45 ${x} ${y})`,
      fill: 'var(--entity)', stroke: 'var(--surface)', 'stroke-width': 2.5,
    });
  }

  function renderGraphLegend() {
    const items = [
      ['session', false, 'Session'],
      ['pref', false, 'Saved preference (current)'],
      ['pref', true, 'Replaced preference'],
      ['entity', false, 'Entity from tool arguments'],
    ];
    document.getElementById('graph-legend').replaceChildren(
      ...items.map(([kind, hollow, label]) => {
        const icon = svg('svg', { width: 16, height: 16, viewBox: '0 0 16 16', 'aria-hidden': 'true' });
        icon.append(marker(kind, 8, 8, hollow));
        return el('span', { class: 'legend-item' }, icon, label);
      }),
    );
  }

  function renderGraph() {
    const root = document.getElementById('graph');
    root.replaceChildren();
    renderGraphLegend();
    const user = state.user;
    const sessions = user.sessions;
    const width = Math.max(560, root.parentElement.clientWidth);
    const padL = 150;
    const padR = 200;
    const xs = new Map();
    sessions.forEach((session, i) => {
      const span = width - padL - padR;
      const x = sessions.length === 1 ? padL + span / 2 : padL + (i * span) / (sessions.length - 1);
      xs.set(session.session_id, x);
    });

    const ROW = 30;
    const prefSlots = new Map();
    const prefs = user.preferences.map((pref, i) => {
      const slot = prefSlots.get(pref.session_id) || 0;
      prefSlots.set(pref.session_id, slot + 1);
      return { id: `p${i}`, kind: 'pref', pref, slot, x: xs.get(pref.session_id), links: [`s:${pref.session_id}`] };
    });
    const entSlots = new Map();
    const entities = user.entities.map((entity, i) => {
      const first = entity.mentions[0].session_id;
      const slot = entSlots.get(first) || 0;
      entSlots.set(first, slot + 1);
      const linked = [...new Set(entity.mentions.map((m) => m.session_id))];
      return { id: `e${i}`, kind: 'entity', entity, slot, x: xs.get(first), links: linked.map((s) => `s:${s}`) };
    });
    const prefRows = Math.max(1, ...prefSlots.values());
    const entRows = Math.max(1, ...entSlots.values());
    const spineY = 34 + prefRows * ROW + 52;
    for (const node of prefs) node.y = spineY - 64 - node.slot * ROW;
    for (const node of entities) node.y = spineY + 64 + node.slot * ROW;
    const height = spineY + 64 + entRows * ROW + 6;
    root.setAttribute('viewBox', `0 0 ${width} ${height}`);
    root.setAttribute('height', height);

    const defs = svg('defs');
    const arrow = svg('marker', {
      id: 'arrow', viewBox: '0 0 10 10', refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse',
    });
    arrow.append(svg('path', { d: 'M0 0 L10 5 L0 10 z', fill: 'var(--muted)' }));
    defs.append(arrow);
    root.append(defs);

    const bandLabel = (y, text) =>
      root.append(svg('text', { x: 0, y, fill: 'var(--muted)', 'font-size': 12, 'dominant-baseline': 'middle' }, text));
    bandLabel(prefs.length ? (Math.min(...prefs.map((p) => p.y)) + spineY - 64) / 2 : spineY - 64, 'Saved preferences');
    bandLabel(spineY, 'Sessions');
    bandLabel(spineY + 64 + ((entRows - 1) * ROW) / 2, 'Entities');

    const edges = svg('g');
    const nodesLayer = svg('g');
    root.append(edges, nodesLayer);
    const edgeList = [];
    const addEdge = (a, b, d, extra) => {
      const path = svg('path', {
        d, fill: 'none', stroke: 'var(--axis)', 'stroke-width': 1.25, ...extra,
      });
      path.dataset.a = a;
      path.dataset.b = b;
      edges.append(path);
      edgeList.push(path);
    };

    // The spine: sessions in time order.
    const firstX = xs.get(sessions[0].session_id);
    const lastX = xs.get(sessions[sessions.length - 1].session_id);
    edges.append(svg('line', { x1: firstX, y1: spineY, x2: lastX, y2: spineY, stroke: 'var(--axis)', 'stroke-width': 2 }));

    for (const node of prefs) {
      addEdge(node.id, node.links[0], `M${node.x} ${node.y + 7} L${node.x} ${spineY - 10}`);
    }
    for (const node of entities) {
      for (const link of node.links) {
        const sx = xs.get(link.slice(2));
        addEdge(node.id, link, `M${node.x} ${node.y - 8} L${sx} ${spineY + 10}`);
      }
    }
    const nodeGroups = [];
    const addNode = (node, label, sublabel, build) => {
      const group = svg('g', { class: 'node', tabindex: 0, role: 'button', 'aria-label': `${label}${sublabel ? `, ${sublabel}` : ''}` });
      group.dataset.id = node.id;
      group.append(svg('circle', { class: 'node-hit', cx: node.x, cy: node.y, r: 15, fill: 'transparent' }));
      group.append(marker(node.kind, node.x, node.y, node.hollow));
      const text = svg('text', {
        x: node.x + 13, y: node.y, 'dominant-baseline': 'middle', 'font-size': 13,
        fill: node.hollow ? 'var(--muted)' : 'var(--ink)',
        'text-decoration': node.hollow ? 'line-through' : null,
        'font-weight': node.selected ? 650 : null,
      }, label);
      if (sublabel) text.append(svg('tspan', { fill: 'var(--muted)', 'font-size': 11.5, dx: 6 }, sublabel));
      group.append(text);
      bindTip(group, build);
      group.addEventListener('pointerenter', () => highlight(node.id));
      group.addEventListener('focus', () => highlight(node.id));
      group.addEventListener('pointerleave', () => highlight(null));
      group.addEventListener('blur', () => highlight(null));
      nodesLayer.append(group);
      nodeGroups.push(group);
      return group;
    };

    sessions.forEach((session, i) => {
      const x = xs.get(session.session_id);
      const node = { id: `s:${session.session_id}`, kind: 'session', x, y: spineY, selected: session.session_id === state.sessionId };
      if (node.selected) {
        nodesLayer.append(svg('circle', { cx: x, cy: spineY, r: 12, fill: 'none', stroke: 'var(--session)', 'stroke-width': 1.5 }));
      }
      const turns = user.traces.filter((t) => t.session_id === session.session_id);
      const group = addNode(node, '', null, () => [
        `Session ${i + 1}`,
        [
          session.session_id,
          `Started ${when(session.created_at)}`,
          `${plural(turns.length, 'turn')}, ${plural(session.messages.length, 'message')}`,
        ],
        `${plural(session.row_count, 'row')} in agent_events`,
      ]);
      group.setAttribute('aria-label', `Session ${i + 1}, started ${clock(session.created_at)}`);
      group.querySelector('text').remove();
      // Edges reach a session from above (preferences, vertical) and from
      // the lower left (entities first seen earlier), so the labels sit in
      // the two free quadrants on the right.
      group.append(
        svg('text', { x: x + 12, y: spineY - 12, 'dominant-baseline': 'middle', 'font-size': 12.5, fill: 'var(--ink)', 'font-weight': node.selected ? 650 : 500 }, `Session ${i + 1}`),
        svg('text', { x: x + 12, y: spineY + 14, 'dominant-baseline': 'middle', 'font-size': 11, fill: 'var(--muted)' }, clock(session.created_at)),
      );
      group.addEventListener('click', () => selectSession(session.session_id));
      group.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          selectSession(session.session_id);
        }
      });
    });

    for (const node of prefs) {
      const pref = node.pref;
      node.hollow = Boolean(pref.valid_until);
      const replacedBy = prefs.find(
        (other) => other.pref.category === pref.category && other.pref.valid_from === pref.valid_until,
      );
      const replaced = prefs.find(
        (other) => other.pref.category === pref.category && other.pref.valid_until === pref.valid_from,
      );
      node.group = addNode(node, `${pref.category} = ${pref.preference}`, null, () => [
        `${pref.category} = ${pref.preference}`,
        [
          `Saved in ${sessionLabel(pref.session_id)}, ${when(pref.valid_from)}`,
          replacedBy
            ? `Replaced by ${replacedBy.pref.preference}, ${when(pref.valid_until)}`
            : 'Current value',
          replaced ? `Replaced ${replaced.pref.preference}` : null,
        ],
        `STATE_DELTA row, span ${shortId(pref.span_id)}`,
      ]);
    }

    // Replacements: a short arc from the end of each superseded version's
    // label to the version that replaced it (measured after layout).
    for (const node of prefs) {
      const next = prefs.find(
        (other) => other.pref.category === node.pref.category && other.pref.valid_from === node.pref.valid_until,
      );
      if (!next) continue;
      const label = node.group.querySelector('text');
      const startX = label.getBBox().x + label.getBBox().width + 8;
      const endX = next.x - 12;
      if (endX - startX < 24) continue;
      const peak = Math.min(node.y, next.y) - 16;
      addEdge(
        node.id,
        next.id,
        `M${startX} ${node.y} Q${(startX + endX) / 2} ${peak - 8} ${endX} ${next.y}`,
        { 'marker-end': 'url(#arrow)', stroke: 'var(--muted)' },
      );
      nodesLayer.append(
        svg('text', {
          x: (startX + endX) / 2, y: peak - 8, 'text-anchor': 'middle',
          fill: 'var(--muted)', 'font-size': 11.5,
        }, `replaced ${when(node.pref.valid_until)}`),
      );
    }

    for (const node of entities) {
      const entity = node.entity;
      const calls = entity.mentions.length;
      addNode(node, entity.name, plural(calls, 'call'), () => [
        entity.name,
        [
          `${entity.entity_type}, named in tool arguments`,
          `${plural(calls, 'tool call')} in ${node.links.map((l) => sessionLabel(l.slice(2))).join(', ')}`,
          ...entity.mentions.slice(0, 4).map((m) => `${m.tool_name}(${m.argument}=…), ${clock(m.timestamp)}`),
        ],
        `TOOL_STARTING rows, first span ${shortId(entity.mentions[0].span_id)}`,
      ]);
    }

    function highlight(id) {
      const linked = new Set(id ? [id] : []);
      if (id) {
        for (const edge of edgeList) {
          if (edge.dataset.a === id) linked.add(edge.dataset.b);
          if (edge.dataset.b === id) linked.add(edge.dataset.a);
        }
      }
      for (const edge of edgeList) {
        const hot = id && (edge.dataset.a === id || edge.dataset.b === id);
        edge.classList.toggle('dimmed', Boolean(id) && !hot);
        edge.setAttribute('stroke-width', hot ? 2 : 1.25);
      }
      for (const group of nodeGroups) {
        group.classList.toggle('dimmed', Boolean(id) && !linked.has(group.dataset.id));
      }
    }
  }

  function renderGraphTable() {
    const user = state.user;
    const rows = [
      ...user.sessions.map((s, i) => [
        'Session', `Session ${i + 1} (${s.session_id})`, `Session ${i + 1}`, when(s.created_at), '', `${plural(s.row_count, 'row')}`,
      ]),
      ...user.preferences.map((p) => [
        'Preference',
        `${p.category} = ${p.preference}`,
        sessionLabel(p.session_id),
        when(p.valid_from),
        p.valid_until ? when(p.valid_until) : 'current',
        `STATE_DELTA span ${p.span_id}`,
      ]),
      ...user.entities.map((e) => [
        `Entity (${e.entity_type})`,
        e.name,
        [...new Set(e.mentions.map((m) => sessionLabel(m.session_id)))].join(', '),
        when(e.mentions[0].timestamp),
        '',
        `${plural(e.mentions.length, 'TOOL_STARTING row')}`,
      ]),
    ];
    document.getElementById('graph-table').replaceChildren(
      table(['Kind', 'Item', 'Sessions', 'From', 'Until', 'Source rows'], rows),
    );
  }

  function table(headers, rows) {
    return el(
      'table',
      {},
      el('thead', {}, el('tr', {}, ...headers.map((h) => el('th', { scope: 'col', text: h })))),
      el('tbody', {}, ...rows.map((row) => el('tr', {}, ...row.map((cell) => el('td', { text: cell }))))),
    );
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
          text: `${duration(trace.latency_ms)}, ${plural(trace.llm_calls, 'model call')}, ${plural(tools, 'tool call')}, ${plural(trace.total_tokens, 'token')}`,
        }),
        el('p', { class: 'trace-task', text: `“${trace.task || ''}”` }),
      ),
    );
    const figure = el('div', { class: 'figure' });
    article.append(figure);
    article.append(
      el(
        'p',
        { class: 'trace-outcome' },
        el('strong', { text: trace.outcome ? 'Answer: ' : 'No answer: ' }),
        trace.outcome || trace.errors[0] || 'the turn ended without a text reply.',
      ),
    );
    // Size against the panel, since the article is not in the DOM yet.
    const width = Math.max(520, document.getElementById('traces').clientWidth);
    figure.append(waterfall(trace, width));
    return article;
  }

  function waterfall(trace, width) {
    const rows = trace.timeline;
    const ROW = 26;
    const top = 4;
    const axisH = 24;
    const labelW = Math.min(260, Math.round(width * 0.34));
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
      // Tree: tool calls hang off the model call that requested them.
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
          x: labelX + (isTool ? 18 : 0), y, 'dominant-baseline': 'middle', 'font-size': 12.5,
          fill: isTool ? 'var(--ink)' : 'var(--ink-2)', 'font-weight': isTool ? 600 : null,
        }, label.length > 34 ? `${label.slice(0, 33)}…` : label),
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
        x: start + barW + 6, y, 'dominant-baseline': 'middle', 'font-size': 11.5, fill: 'var(--muted)',
      }, row.end_ms === null ? 'no completion row' : duration(span)));
      group.append(svg('rect', { x: 0, y: y - ROW / 2, width, height: ROW, fill: 'transparent' }));
      group.setAttribute('aria-label', `${label}, ${isTool ? status.label : `model call ${row.status}`}, ${duration(span)}`);
      bindTip(group, () => [
        row.end_ms === null ? 'No completion row' : duration(span),
        [
          isTool
            ? `${row.label} ${status.label}`
            : `Model call (${row.label})${incomplete ? ': no terminal response row, so not an answer' : ''}`,
          row.detail ? (row.detail.length > 220 ? `${row.detail.slice(0, 219)}…` : row.detail) : null,
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
    document.getElementById('context-hint').textContent =
      `The get_context() block for ${sessionLabel(user.current_session_id)}, the latest session: all three layers in one prompt, each line tagged with the session and span of the rows it came from.`;
    document.getElementById('context').textContent = user.context;
  }

  init();
})();
