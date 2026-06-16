/* Swarm Dashboard — 纯前端 SPA */

const API_BASE = '';
let refreshTimer = null;
let refreshInterval = 3;

const state = {
  health: null,
  agents: [],
  tasks: [],
  sessions: [],
  connected: false,
};

// ── 工具函数 ───────────────────────────────────────────────────────────────

function $(sel) { return document.querySelector(sel); }
function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN');
}
function fmtBytes(b) {
  if (!b) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${units[i]}`;
}
function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function apiGet(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

function setConnection(ok, msg) {
  state.connected = ok;
  const dot = $('#conn-dot');
  const txt = $('#conn-text');
  dot.className = 'dot ' + (ok ? 'ok' : 'err');
  txt.textContent = msg || (ok ? '已连接' : '断开');
}

async function refreshData() {
  try {
    const [health, agents, tasks, sessions] = await Promise.all([
      apiGet('/api/health'),
      apiGet('/api/agents'),
      apiGet('/api/tasks?limit=100'),
      apiGet('/api/sessions?limit=100'),
    ]);
    state.health = health;
    state.agents = agents;
    state.tasks = tasks;
    state.sessions = sessions;
    setConnection(true, '已连接');
    renderCurrentPage();
  } catch (e) {
    console.error('refresh failed', e);
    setConnection(false, '连接失败');
  }
}

// ── 路由 ───────────────────────────────────────────────────────────────────

const routes = {
  'home': renderOverview,
  'agents': renderAgents,
  'sessions': renderSessions,
  'session': renderSessionDetail,
};

function parseRoute() {
  const hash = location.hash.slice(1) || '/';
  if (hash === '/' || hash === '') return { name: 'home' };
  if (hash === '/agents') return { name: 'agents' };
  if (hash === '/sessions') return { name: 'sessions' };
  const m = hash.match(/^\/sessions\/(.+)$/);
  if (m) return { name: 'session', id: m[1] };
  return { name: 'home' };
}

function setActiveNav(name) {
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.route === name);
  });
}

function renderCurrentPage() {
  const route = parseRoute();
  setActiveNav(route.name === 'session' ? 'sessions' : route.name);
  const titles = { home: '概览', agents: 'Agents', sessions: 'Sessions', session: 'Session 详情' };
  $('#page-title').textContent = titles[route.name] || '概览';
  const renderer = routes[route.name] || renderOverview;
  renderer(route);
}

// ── 页面渲染 ───────────────────────────────────────────────────────────────

function renderOverview(route) {
  const health = state.health || { status: 'unknown', pool_available: 0, pool_total: 0, active_tasks: 0 };
  const running = state.tasks.filter(t => t.status === 'running').length;
  const recentSessions = [...state.sessions].slice(0, 8);
  const recentFailed = state.tasks.filter(t => t.status === 'failed').slice(0, 5);

  $('#content').innerHTML = `
    <div class="cards">
      <div class="card">
        <div class="card-label">集群状态</div>
        <div class="card-value" style="color:${health.status === 'ok' ? 'var(--success)' : 'var(--danger)'}">
          ${health.status === 'ok' ? '健康' : health.status}
        </div>
        <div class="card-meta">/api/health</div>
      </div>
      <div class="card">
        <div class="card-label">Agent 类型</div>
        <div class="card-value">${state.agents.length}</div>
        <div class="card-meta">已注册角色</div>
      </div>
      <div class="card">
        <div class="card-label">运行中任务</div>
        <div class="card-value" style="color:${running > 0 ? 'var(--warning)' : 'var(--text)'}">${running}</div>
        <div class="card-meta">共 ${state.tasks.length} 个任务</div>
      </div>
      <div class="card">
        <div class="card-label">容器池</div>
        <div class="card-value">${health.pool_available}/${health.pool_total}</div>
        <div class="card-meta">idle / total</div>
      </div>
    </div>

    <div class="section">
      <div class="section-header"><h3 class="section-title">最近 Session</h3></div>
      <div class="section-body">
        ${recentSessions.length ? `
          <div class="table-wrap">
            <table>
              <thead><tr><th>Session ID</th><th>事件数</th><th>最后事件</th><th>时间</th></tr></thead>
              <tbody>
                ${recentSessions.map(s => `
                  <tr class="clickable" onclick="location.hash='#/sessions/${s.session_id}'">
                    <td><span class="badge badge-blue">${s.session_id}</span></td>
                    <td>${s.event_count}</td>
                    <td>${s.last_event_type || '-'}</td>
                    <td>${fmtTime(s.last_event_at || s.created_at)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        ` : '<div class="empty">暂无 Session</div>'}
      </div>
    </div>

    ${recentFailed.length ? `
      <div class="section">
        <div class="section-header"><h3 class="section-title">最近失败任务</h3></div>
        <div class="section-body">
          <div class="table-wrap">
            <table>
              <thead><tr><th>Task ID</th><th>状态</th><th>消息</th></tr></thead>
              <tbody>
                ${recentFailed.map(t => `
                  <tr>
                    <td><span class="badge badge-gray">${t.task_id}</span></td>
                    <td><span class="badge badge-red">${t.status}</span></td>
                    <td>${escapeHtml(t.message || '').slice(0, 120)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    ` : ''}
  `;
}

function renderAgents(route) {
  $('#content').innerHTML = `
    <div class="agent-grid">
      ${state.agents.map(a => `
        <div class="agent-card">
          <h3>${escapeHtml(a.name)} <span class="badge badge-gray">${a.id}</span></h3>
          <p>${escapeHtml(a.description)}</p>
          <div class="skill-tags">
            ${(a.skills || []).flatMap(s => (s.tags || []).map(tag => `<span class="skill-tag">${escapeHtml(tag)}</span>`)).join('')}
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

function renderSessions(route) {
  const rows = state.sessions;
  $('#content').innerHTML = `
    <div class="section">
      <div class="section-header"><h3 class="section-title">所有 Sessions</h3><span class="badge badge-gray">${rows.length}</span></div>
      <div class="section-body">
        ${rows.length ? `
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>Session ID</th><th>租户</th><th>事件数</th><th>最后事件</th><th>创建时间</th></tr>
              </thead>
              <tbody>
                ${rows.map(s => `
                  <tr class="clickable" onclick="location.hash='#/sessions/${s.session_id}'">
                    <td><span class="badge badge-blue">${s.session_id}</span></td>
                    <td>${s.tenant_id}</td>
                    <td>${s.event_count}</td>
                    <td>${s.last_event_type || '-'}</td>
                    <td>${fmtTime(s.created_at)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        ` : '<div class="empty">暂无 Session</div>'}
      </div>
    </div>
  `;
}

async function renderSessionDetail(route) {
  const sid = route.id;
  $('#content').innerHTML = '<div class="loading"><div class="spinner"></div>加载中...</div>';
  try {
    const [session, eventsResp, tasks] = await Promise.all([
      apiGet(`/api/sessions/${sid}`),
      apiGet(`/api/sessions/${sid}/events`),
      apiGet('/api/tasks?limit=200'),
    ]);
    const sessionTasks = tasks.filter(t => t.session_id === sid);
    const artifacts = sessionTasks.flatMap(t => t.artifacts || []);

    $('#content').innerHTML = `
      <div class="breadcrumb">
        <a href="#/sessions">Sessions</a>
        <span>/</span>
        <span>${sid}</span>
      </div>

      <div class="session-meta">
        <div class="meta-item">
          <div class="meta-key">Session ID</div>
          <div class="meta-value">${sid}</div>
        </div>
        <div class="meta-item">
          <div class="meta-key">Tenant</div>
          <div class="meta-value">${session.tenant_id}</div>
        </div>
        <div class="meta-item">
          <div class="meta-key">创建时间</div>
          <div class="meta-value">${fmtTime(session.created_at)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-key">事件数</div>
          <div class="meta-value">${session.events.length}</div>
        </div>
      </div>

      <div class="section">
        <div class="section-header"><h3 class="section-title">消息 / 事件时间线</h3></div>
        <div class="section-body">
          ${session.events.length ? renderTimeline(session.events) : '<div class="empty">暂无事件</div>'}
        </div>
      </div>

      ${artifacts.length ? `
        <div class="section">
          <div class="section-header"><h3 class="section-title">产物文件</h3></div>
          <div class="section-body">
            <div class="artifact-list">
              ${artifacts.map(a => `
                <div class="artifact-item">
                  <span class="artifact-name">${escapeHtml(a.name)}</span>
                  <span class="artifact-size">${fmtBytes(a.size)}</span>
                </div>
              `).join('')}
            </div>
          </div>
        </div>
      ` : ''}

      ${sessionTasks.length ? `
        <div class="section">
          <div class="section-header"><h3 class="section-title">关联任务</h3></div>
          <div class="section-body">
            <div class="table-wrap">
              <table>
                <thead><tr><th>Task ID</th><th>状态</th></tr></thead>
                <tbody>
                  ${sessionTasks.map(t => `
                    <tr>
                      <td><span class="badge badge-gray">${t.task_id}</span></td>
                      <td><span class="badge ${statusBadgeClass(t.status)}">${t.status}</span></td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ` : ''}
    `;
    bindTimelineToggles();
  } catch (e) {
    $('#content').innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderTimeline(events) {
  return `
    <div class="timeline">
      ${events.map((e, idx) => {
        const type = e.type || 'unknown';
        const cls = eventClass(type);
        const summary = eventSummary(e);
        return `
          <div class="event ${cls}">
            <div class="event-dot"></div>
            <div class="event-header">
              <span class="event-type">${type}</span>
              <span class="event-time">${fmtTime(e.timestamp)}</span>
            </div>
            ${summary ? `<div class="event-summary">${summary}</div>` : ''}
            <div class="event-toggle" data-idx="${idx}">展开原始事件 ▼</div>
            <pre class="event-raw" id="event-raw-${idx}">${escapeHtml(JSON.stringify(e, null, 2))}</pre>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function eventClass(type) {
  if (type === 'user_message') return 'user';
  if (type.startsWith('agent_')) return 'agent';
  if (type.startsWith('orchestrator_')) return 'orchestrator';
  if (type === 'orchestrator_fallback') return 'fallback';
  return '';
}

function eventSummary(e) {
  const type = e.type;
  if (type === 'user_message') return escapeHtml(e.text || '').slice(0, 300);
  if (type === 'plan_created') return `子任务数: ${e.subtask_count || '-'}`;
  if (type === 'agent_dispatched') return `agent: ${e.agent_type} / dispatch: ${e.dispatch_id}`;
  if (type === 'agent_completed') return `agent: ${e.agent_type} / success: ${e.success}${e.error ? ' / error: ' + e.error : ''}`;
  if (type === 'orchestrator_started') return `provider: ${e.provider || '-'} / endpoint: ${e.endpoint || '-'}`;
  if (type === 'orchestrator_failed') return `error: ${escapeHtml(e.error || '').slice(0, 200)}`;
  if (type === 'orchestrator_completed') return `result: ${escapeHtml((e.result || '')).slice(0, 200)}`;
  if (type === 'orchestrator_fallback') return `reason: ${escapeHtml((e.data && e.data.reason) || '').slice(0, 200)}`;
  return '';
}

function bindTimelineToggles() {
  document.querySelectorAll('.event-toggle').forEach(el => {
    el.addEventListener('click', () => {
      const idx = el.dataset.idx;
      const raw = $(`#event-raw-${idx}`);
      raw.classList.toggle('open');
      el.textContent = raw.classList.contains('open') ? '收起原始事件 ▲' : '展开原始事件 ▼';
    });
  });
}

function statusBadgeClass(status) {
  if (status === 'completed') return 'badge-green';
  if (status === 'running') return 'badge-orange';
  if (status === 'failed') return 'badge-red';
  return 'badge-gray';
}

// ── 初始化 ─────────────────────────────────────────────────────────────────

async function init() {
  // 读取后端 dashboard 配置（标题、刷新间隔）
  try {
    const cfg = await apiGet('/api/dashboard/config');
    if (cfg.title) {
      $('#dash-title').textContent = cfg.title;
      document.title = cfg.title;
    }
    if (cfg.refresh_interval) {
      refreshInterval = cfg.refresh_interval;
      $('#refresh-interval').textContent = refreshInterval;
    }
  } catch (e) {
    console.warn('failed to load dashboard config', e);
  }

  $('#refresh-btn').addEventListener('click', () => {
    refreshData();
  });

  window.addEventListener('hashchange', renderCurrentPage);

  await refreshData();
  refreshTimer = setInterval(refreshData, refreshInterval * 1000);
}

init();
