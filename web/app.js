// ─────────────────────────────────────────────────────────────────────────────
// Agent Swarm Web UI — 前后端分离，纯静态。
// 后端地址在左下角配置；CORS 已开 *，可从任意位置接入。
// ─────────────────────────────────────────────────────────────────────────────

const state = {
  mode: "copilot",        // "copilot" | "direct"
  backendUrl: localStorage.getItem("swarmBackend") || "http://localhost:9000",
  sessionId: null,        // 当前会话 id（跨多轮复用）
  currentTaskId: null,    // 正在执行的任务
  selectedAgent: null,    // 直聊模式选中的 agent_id
  ws: null,               // WebSocket 连接
  busy: false,
  externalAgents: [],     // /api/v1/agents 返回的外部 agent（直聊候选）
  builtinAgents: [],      // /api/agents 返回的内置 card（Copilot 可用角色）
};

// ── DOM ────────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const els = {
  modeCopilot: $("mode-copilot"), modeDirect: $("mode-direct"),
  roster: $("agent-roster"),
  backendUrl: $("backend-url"), connectBtn: $("connect-btn"), connStatus: $("conn-status"),
  chatTitle: $("chat-title"), sessionIdEl: $("session-id"), newSessionBtn: $("new-session-btn"),
  messages: $("messages"),
  progressBar: $("progress-bar"), progressText: $("progress-text"), cancelBtn: $("cancel-btn"),
  chatForm: $("chat-form"), chatInput: $("chat-input"), sendBtn: $("send-btn"),
  taskMeta: $("task-meta"), artifactsList: $("artifacts-list"),
  refreshArtifacts: $("refresh-artifacts"),
  preview: $("artifact-preview"), previewName: $("preview-name"),
  previewFrame: $("preview-frame"), closePreview: $("close-preview"),
};

// ── 初始化 ──────────────────────────────────────────────────────────────────
function init() {
  els.backendUrl.value = state.backendUrl;
  applyMode();
  bindEvents();
  connect();
  // 每 10 秒刷新一次外部 agent 列表，及时反映在线/离线变化
  setInterval(() => {
    if (state.backendUrl) connect();
  }, 10000);
}

function bindEvents() {
  els.modeCopilot.onclick = () => { state.mode = "copilot"; state.selectedAgent = null; applyMode(); renderRoster(); };
  els.modeDirect.onclick = () => { state.mode = "direct"; applyMode(); renderRoster(); };
  els.connectBtn.onclick = connect;
  els.backendUrl.onkeydown = (e) => { if (e.key === "Enter") connect(); };
  els.newSessionBtn.onclick = newSession;
  els.chatForm.onsubmit = onSubmit;
  els.chatInput.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); els.chatForm.requestSubmit(); }
  };
  els.chatInput.oninput = () => autoGrow(els.chatInput);
  els.cancelBtn.onclick = cancelTask;
  els.refreshArtifacts.onclick = () => state.currentTaskId && loadArtifacts(state.currentTaskId);
  els.closePreview.onclick = () => {
    els.preview.hidden = true;
    els.preview.style.display = "none";
  };
}

function autoGrow(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 120) + "px"; }
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN");
}

// ── 连接后端 ────────────────────────────────────────────────────────────────
async function connect() {
  const url = els.backendUrl.value.trim().replace(/\/$/, "");
  if (!url) return;
  state.backendUrl = url;
  localStorage.setItem("swarmBackend", url);
  els.connectBtn.disabled = true;
  els.connectBtn.textContent = "连接中…";

  try {
    // 拉取两类 agent：内置 card + 外部注册的
    const [builtin, external] = await Promise.all([
      fetch(`${url}/api/agents`).then((r) => r.json()).catch(() => []),
      fetch(`${url}/api/v1/agents`).then((r) => r.json()).catch(() => []),
    ]);
    state.builtinAgents = builtin;
    state.externalAgents = external;
    els.connStatus.textContent = "● 已连接";
    els.connStatus.className = "conn-status connected";
    els.modeDirect.disabled = external.length === 0;
    renderRoster();
  } catch (e) {
    els.connStatus.textContent = "● 连接失败";
    els.connStatus.className = "conn-status disconnected";
  } finally {
    els.connectBtn.disabled = false;
    els.connectBtn.textContent = "连接";
  }
}

function renderRoster() {
  els.roster.innerHTML = "";
  if (state.mode === "copilot") {
    const hint = document.createElement("div");
    hint.className = "roster-hint";
    hint.textContent = `编排器可调度 ${state.builtinAgents.length} 个角色`;
    els.roster.appendChild(hint);
    for (const a of state.builtinAgents) {
      els.roster.appendChild(agentCard(a.id, a.name, a.description, false));
    }
  } else {
    const hint = document.createElement("div");
    hint.className = "roster-hint";
    hint.textContent = state.externalAgents.length
      ? `在线外部 Agent：${state.externalAgents.length} 个`
      : "无在线外部 Agent（已自动过滤离线/过期注册）";
    els.roster.appendChild(hint);
    for (const a of state.externalAgents) {
      const online = a.status === "online";
      const badge = `<span class="status-dot ${online ? "online" : "offline"}"></span>`;
      const meta = online
        ? `心跳 ${fmtTime(a.last_heartbeat)}`
        : `离线`;
      const card = agentCard(
        a.id,
        `${badge} ${a.name}`,
        `${a.description || a.endpoint || ""}<br><small>${escapeHtml(meta)}</small>`,
        a.id === state.selectedAgent
      );
      card.classList.toggle("offline", !online);
      card.onclick = () => {
        if (!online) return;
        state.selectedAgent = a.id;
        renderRoster();
        els.chatTitle.textContent = `💬 直聊：${a.name}`;
      };
      els.roster.appendChild(card);
    }
  }
}

function agentCard(id, name, desc, selected) {
  const div = document.createElement("div");
  div.className = "agent-card" + (selected ? " selected" : "");
  div.innerHTML = `<div class="agent-name">${name}</div><div class="agent-desc">${desc || ""}</div>`;
  return div;
}

function applyMode() {
  els.modeCopilot.classList.toggle("active", state.mode === "copilot");
  els.modeDirect.classList.toggle("active", state.mode === "direct");
  if (state.mode === "copilot") els.chatTitle.textContent = "🧭 Copilot 模式";
  else if (!state.selectedAgent) els.chatTitle.textContent = "💬 直聊（请选择 Agent）";
}

// ── 会话 ────────────────────────────────────────────────────────────────────
function newSession() {
  state.sessionId = null;
  els.sessionIdEl.textContent = "（新建）";
  els.messages.innerHTML = "";
  showEmpty();
}

function showEmpty() {
  els.messages.innerHTML = `<div class="empty-state"><div class="bee">🐝</div>
    <p>${state.mode === "copilot" ? "发送消息，Copilot 会自动拆解并分派 Agent。" : "选一个 Agent，发消息开始 1:1 对话。"}</p></div>`;
}

// ── 发送 ────────────────────────────────────────────────────────────────────
async function onSubmit(e) {
  e.preventDefault();
  const text = els.chatInput.value.trim();
  if (!text || state.busy) return;

  addMessage("user", "你", text);
  els.chatInput.value = "";
  autoGrow(els.chatInput);
  setBusy(true);

  try {
    let resp;
    if (state.mode === "copilot") {
      resp = await fetch(`${state.backendUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: state.sessionId }),
      });
    } else {
      if (!state.selectedAgent) { addMessage("agent", "系统", "请先在左侧选择一个 Agent。"); setBusy(false); return; }
      resp = await fetch(`${state.backendUrl}/api/v1/agents/${state.selectedAgent}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: text, session_id: state.sessionId }),
      });
    }
    const data = await resp.json();
    state.currentTaskId = data.task_id;
    state.sessionId = data.session_id || state.sessionId;
    els.sessionIdEl.textContent = state.sessionId || "—";
    els.taskMeta.textContent = `task: ${data.task_id}`;
    openWebSocket(data.task_id);
  } catch (err) {
    addMessage("agent", "系统", `请求失败：${err}`);
    setBusy(false);
  }
}

function setBusy(busy) {
  state.busy = busy;
  els.sendBtn.disabled = busy;
  els.progressBar.hidden = !busy;
  els.progressText.textContent = busy ? "执行中…" : "";
}

// ── WebSocket 流式 ──────────────────────────────────────────────────────────
function openWebSocket(taskId) {
  if (state.ws) { try { state.ws.close(); } catch (_) {} }
  const wsUrl = state.backendUrl.replace(/^http/, "ws") + `/ws/tasks/${taskId}`;
  state.ws = new WebSocket(wsUrl);
  state.ws.onmessage = (ev) => {
    const event = JSON.parse(ev.data);
    handleEvent(event);
  };
  state.ws.onerror = () => { setBusy(false); };
}

function handleEvent(event) {
  const t = event.type;
  if (t === "status") {
    // 初始状态推送
    return;
  }
  if (t === "orchestrator_thinking") {
    els.progressText.textContent = `编排器思考中…（迭代 ${event.data?.iteration ?? "?"}）`;
    return;
  }
  if (t === "tool_call") {
    addEvent("🔧", `${event.data.tool}(${summarizeArgs(event.data.args)})`);
    return;
  }
  if (t === "tool_result") {
    // 工具结果静默或淡显示
    return;
  }
  if (t === "agent_progress") {
    const snap = event.data || {};
    const prog = snap.progress;
    if (Array.isArray(prog) && prog.length) {
      const last = prog[prog.length - 1];
      addEvent("⚙️", `${event.agent}: ${last.tool ? last.tool + " → " : ""}${(last.content || last.result || snap.state || "").slice(0, 80)}`);
    } else {
      els.progressText.textContent = `${event.agent}: ${snap.state || ""} ${snap.message || ""}`.trim();
    }
    return;
  }
  if (t === "agent_dispatched") {
    addEvent("📤", `分派 ${event.data.agent_type}`);
    return;
  }
  if (t === "agent_completed") {
    addEvent(event.data.success ? "✅" : "❌", `${event.data.agent_type} ${event.data.success ? "完成" : "失败"}`);
    return;
  }
  if (t === "finalized" || t === "completed") {
    addEvent("🎉", "任务完成");
    setBusy(false);
    loadArtifacts(state.currentTaskId);
    return;
  }
  if (t === "cancelled") {
    addEvent("🚫", "已取消");
    setBusy(false);
    return;
  }
  if (t === "error" || t === "failed") {
    addEvent("⚠️", `错误：${event.data?.error || event.data?.summary || ""}`);
    setBusy(false);
    return;
  }
  // 兜底：未知事件
  if (event.data && typeof event.data === "object" && event.data.summary) {
    addMessage("agent", "编排器", event.data.summary);
  }
}

function summarizeArgs(args) {
  if (!args) return "";
  try {
    const s = JSON.stringify(args);
    return s.length > 60 ? s.slice(0, 60) + "…" : s;
  } catch { return ""; }
}

function addEvent(icon, text) {
  const div = document.createElement("div");
  div.className = "msg event";
  div.innerHTML = `<div class="event-row"><span class="ev-icon">${icon}</span><span>${escapeHtml(text)}</span></div>`;
  els.messages.appendChild(div);
  scrollToBottom();
}

function addMessage(role, who, text) {
  // 清掉空状态
  if (els.messages.querySelector(".empty-state")) els.messages.innerHTML = "";
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="role">${escapeHtml(who)}</div><div class="bubble">${escapeHtml(text)}</div>`;
  els.messages.appendChild(div);
  scrollToBottom();
}

function scrollToBottom() { els.messages.scrollTop = els.messages.scrollHeight; }

async function cancelTask() {
  if (!state.currentTaskId || !state.ws) return;
  state.ws.send(JSON.stringify({ action: "cancel" }));
}

// ── 产物 ────────────────────────────────────────────────────────────────────
async function loadArtifacts(taskId) {
  if (!taskId) return;
  els.artifactsList.innerHTML = '<div class="empty-hint">加载中…</div>';
  try {
    const resp = await fetch(`${state.backendUrl}/api/tasks/${taskId}/artifacts`);
    const arts = await resp.json();
    if (!arts.length) { els.artifactsList.innerHTML = '<div class="empty-hint">无产物文件。</div>'; return; }
    els.artifactsList.innerHTML = "";
    for (const a of arts) {
      const item = document.createElement("div");
      item.className = "artifact-item";
      item.innerHTML = `<span class="fname">${escapeHtml(a.name)}</span><span class="fsize">${humanSize(a.size)}</span>`;
      item.onclick = () => previewArtifact(taskId, a.name);
      els.artifactsList.appendChild(item);
    }
  } catch {
    els.artifactsList.innerHTML = '<div class="empty-hint">加载失败。</div>';
  }
}

async function previewArtifact(taskId, name) {
  try {
    const resp = await fetch(`${state.backendUrl}/api/tasks/${taskId}/artifacts/${encodeURI(name)}`);
    if (!resp.ok) { throw new Error(`HTTP ${resp.status}`); }
    const data = await resp.json();
    els.previewName.textContent = name;
    const isHtml = name.endsWith(".html") || name.endsWith(".htm");
    // HTML 直接 srcdoc 渲染；其他用 <pre> 文本展示
    if (isHtml) {
      els.previewFrame.srcdoc = data.content;
    } else {
      els.previewFrame.srcdoc = `<pre style="padding:12px;font-family:monospace;font-size:12px;white-space:pre-wrap;background:#1e222b;color:#e6e9ef;margin:0;height:100vh;">${escapeHtml(data.content)}</pre>`;
    }
    els.preview.hidden = false;
    els.preview.style.display = "flex";
  } catch (e) {
    els.previewFrame.srcdoc = `<pre>预览失败：${e}</pre>`;
    els.preview.hidden = false;
    els.preview.style.display = "flex";
  }
}

function humanSize(n) {
  if (!n) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

// ── 工具 ────────────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
