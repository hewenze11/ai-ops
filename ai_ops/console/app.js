"use strict";
/* Minimal hand-written console. No external dependencies, no build step.
 *
 * Security posture, deliberately conservative for an operations surface:
 *  - The admin token lives ONLY in a module variable in this tab's memory. It is
 *    never written to localStorage/sessionStorage/cookies/URL, so a reload or a
 *    closed tab drops it and a second tab cannot read it.
 *  - All dynamic content is inserted with textContent, never innerHTML, so alarm
 *    payloads and command output cannot inject markup into the console.
 *  - The UI never fabricates execution outcomes: it renders exactly the states
 *    the backend reports, including unknown and awaiting_approval.
 */
(function () {
  let token = "";
  const questionDrafts = new Map();
  const questionSubmissions = new Set();
  const accountSubmissions = new Set(), assetLifecyclePending = new Set();
  const chatSnapshots = new Map(), chatScroll = new Map(), chatHistory = new Map();
  const imageDrafts = new Map(), imageURLs = new Set();
  let currentActiveTurn = null;
  const selectedGrants = (node) => Array.from(node.selectedOptions || []).map((o) => JSON.parse(o.value));
  async function accountOptions(node, selected = []) {
    const generation = authGeneration, roleId = state.roleId;
    const assets = await api('GET', '/api/v1/assets');
    if (generation !== authGeneration || (node.id === 'chat-users' && roleId !== state.roleId)) return;
    const activeGrants = node.id === 'chat-users' && currentActiveTurn && currentActiveTurn.role_id === roleId ? currentActiveTurn.execution_grants || [] : selected;
    const chosen = new Set(activeGrants.map((g) => JSON.stringify({asset_id: g.asset_id, username: g.username})));
    node.textContent = ''; node.multiple = true;
    assets.forEach((a) => (a.allowed_users || []).forEach((username) => {
      const o = el('option', '', a.name + '.' + username); o.value = JSON.stringify({asset_id: a.id, username}); o.selected = chosen.has(o.value); node.appendChild(o);
    }));
  }
  let state = { view: "chat", roleId: null, tab: "chat" };
  let authGeneration = 0;
  const readCache = new Map(), inflight = new Map(), controllers = new Set();
  const CACHE_TTL = 15000;
  // JSON APIs only; works on the existing HTTP test entry point too.
  const cloneData = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
  const changedLists = new Set();
  const cacheable = (path) => ['/api/v1/roles', '/api/v1/skills', '/api/v1/documents', '/api/v1/custom-tasks'].includes(path);
  function resetSession() {
    changedLists.clear(); authGeneration += 1; controllers.forEach((c) => c.abort()); controllers.clear(); readCache.clear(); inflight.clear();
    chatSnapshots.clear(); chatScroll.clear(); chatHistory.clear(); currentActiveTurn = null;
    $('chat-log').dataset.roleId = ''; $('chat-log').dataset.fresh = 'false';
    imageDrafts.clear(); imageURLs.forEach((url) => URL.revokeObjectURL(url)); imageURLs.clear();
    $('image-drafts').textContent = ''; $('image-files').value = '';
    questionDrafts.clear(); questionSubmissions.clear(); accountSubmissions.clear(); assetLifecyclePending.clear(); chatRenderVersion += 1; chatLoadVersion += 1;
  }
  function invalidate(path) {
    const keys = [];
    if (/\/roles|\/console\/roles/.test(path)) keys.push('/api/v1/roles');
    if (/\/skills/.test(path)) keys.push('/api/v1/skills');
    if (/\/documents|\/assets|\/local-connector/.test(path)) keys.push('/api/v1/documents');
    if (/\/custom-tasks/.test(path)) keys.push('/api/v1/custom-tasks');
    // Also invalidate noncached in-flight detail reads after a related write.
    for (const key of inflight.keys()) {
      if (key === path || key.startsWith(path + '/') || (/\/assets|\/local-connector/.test(path) && (key.startsWith('/api/v1/assets') || key === '/api/v1/documents/system-asset-inventory'))) keys.push(key);
    }
    keys.forEach((key) => { readCache.delete(key); inflight.delete(key); });
  }

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const fmt = (epoch) => {
    if (!epoch) return "-";
    const d = new Date(epoch * 1000);
    return d.toLocaleString("zh-CN", { hour12: false });
  };
  const shortId = (id, n) => (id && id.length > (n || 10) ? id.slice(0, n || 10) + "…" : id || "-");
  const usersToText = (users) => (Array.isArray(users) ? users.join(", ") : "");
  const textToUsers = (text) => text.split(',').map((s) => s.trim()).filter(Boolean);
  function parseAccounts(text) {
    const lines = text.split(/\n/).map((v) => v.trim()).filter(Boolean), users = [], notes = {};
    lines.forEach((line) => { const i = line.indexOf('|'), user = (i < 0 ? line : line.slice(0, i)).trim(); if (users.includes(user)) throw new Error('同一资产账号重复：' + user); users.push(user); notes[user] = i < 0 ? '' : line.slice(i + 1).trim(); });
    return {allowed_users: users, account_notes: notes};
  }

  async function api(method, path, body, fresh = false) {
    const generation = authGeneration;
    if (method === 'GET') {
      const entry = readCache.get(path);
      if (!fresh && cacheable(path) && entry) {
        if (Date.now() - entry.at >= CACHE_TTL) api('GET', path, undefined, true).catch((err) => { if (generation === authGeneration && token) say('后台更新失败：' + err.message, 'error'); });
        return cloneData(entry.data);
      }
      if (inflight.has(path)) return cloneData(await inflight.get(path));
    }
    const pending = request();
    if (method === 'GET') inflight.set(path, pending);
    try { return cloneData(await pending); }
    finally { if (inflight.get(path) === pending) inflight.delete(path); }
    async function request() {
    const controller = new AbortController(); controllers.add(controller);
    const options = { method, headers: {}, signal: controller.signal, cache: 'no-store' };
    if (token) options.headers["Authorization"] = "Bearer " + token;
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response, text;
    try { response = await fetch(path, options); text = await response.text(); }
    finally { controllers.delete(controller); }
    if (generation !== authGeneration) throw new Error('登录已切换，请重新操作');
    if (method === 'GET' && inflight.get(path) !== pending) throw new Error('数据已变更，请重新打开');
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
    if (!response.ok) {
      const message = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : ("HTTP " + response.status);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (method === 'GET' && cacheable(path)) {
      if (inflight.get(path) === pending) {
        const old = readCache.get(path);
        if (old && JSON.stringify(old.data) !== JSON.stringify(data)) changedLists.add(path);
        readCache.set(path, { at: Date.now(), data: cloneData(data) });
      }
    } else if (method !== 'GET') invalidate(path);
    return data;
    }
  }

  let statusTimer = null;
  function say(message, kind) {
    clearTimeout(statusTimer);
    const node = $("status");
    node.textContent = message || "";
    node.className = kind === "error" ? "error" : "muted";
    if (message && kind !== "error") statusTimer = setTimeout(() => { node.textContent = ""; }, 4000);
  }

  let modalGeneration = 0;
  function modal(title, data) {
    modalGeneration += 1;
    $("modal-title").textContent = title;
    $("modal-body").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    $("modal").hidden = false;
  }
  function modalContent(title, node) {
    modalGeneration += 1;
    $("modal-title").textContent = title;
    $("modal-body").textContent = "";
    $("modal-body").appendChild(node);
    $("modal").hidden = false;
  }
  const closeModal = () => { modalGeneration += 1; $("modal").hidden = true; $("modal-body").textContent = ""; };
  $("modal-close").onclick = closeModal;
  $("modal").onclick = (e) => { if (e.target === $("modal")) closeModal(); };

  /* ---------- login ---------- */
  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    const candidate = $("token").value.trim();
    $("login-error").hidden = true;
    try {
      resetSession(); token = candidate;
      await api("GET", "/api/v1/roles");
      $("login").hidden = true;
      $("app").hidden = false;
      $("token").value = "";
      boot();
    } catch (err) {
      token = ""; resetSession();
      const box = $("login-error");
      box.textContent = err.status === 403 ? "凭据无效。" : ("无法连接：" + err.message);
      box.hidden = false;
    }
  };
  $("lock").onclick = () => {
    token = ""; resetSession();
    clearTimeout(refreshTimer); chatRenderVersion += 1; chatTasks.clear(); stepExpansion.clear();
    $("modal").hidden = true; $("modal-body").textContent = ""; $("chat-log").textContent = ""; $("console-body").textContent = "";
    state = { view: "chat", roleId: null, tab: "chat" };
    $("chat-roles").textContent = ''; $("chat-text").value = ''; $("chat-users").value = ''; $("chat-mode").value = 'confirm';
    $("app").hidden = true;
    $("login").hidden = false;
    say("");
  };

  /* ---------- navigation ---------- */
  const VIEWS = [["chat", "聊天"], ["assets", "资产"], ["skills", "Skills"], ["documents", "文档"], ["logs", "日志"], ["custom", "定制任务"]];
  let refreshTimer = null;
  let chatRenderVersion = 0;
  const stepExpansion = new Map();
  let chatTasks = new Map();
  function renderNav() {
    const nav = $("nav");
    nav.textContent = "";
    VIEWS.forEach(([id, label]) => {
      const button = el("button", state.view === id ? "active" : "", label);
      button.onclick = () => { state.view = id; state.tab = id; sessionStorage.setItem("aiops.view", id); renderNav(); renderView(); }; 
      nav.appendChild(button);
    });
  }
  function renderView() {
    closeModal();
    chatLoadVersion += 1; chatRenderVersion += 1;
    say("");
    $("view-chat").hidden = state.view !== "chat";
    $("view-console").hidden = state.view === "chat";
    clearTimeout(refreshTimer);
    if (state.view === "chat") loadChat();
    else { state.tab = state.view; renderConsole(); }
    scheduleRefresh();
  }

  /* ---------- chat ---------- */
  let chatBusy = false, chatLoadVersion = 0, lastRoleRefresh = 0;
  async function loadChat() {
    const version = ++chatLoadVersion;
    try {
      drawImageDrafts();
      const roles = await api("GET", "/api/v1/roles");
      if (version !== chatLoadVersion || !token || state.view !== 'chat') return;
      const list = $("chat-roles");
      list.textContent = "";
      const roleSearch = el("input"); roleSearch.placeholder = "搜索角色名称 / ID"; roleSearch.value = state.roleQuery || ""; roleSearch.oninput = () => { state.roleQuery = roleSearch.value; Array.from(list.children).filter((n) => n.tagName.toLowerCase() === 'li').forEach((n) => { n.hidden = !(n.dataset.search || '').includes(roleSearch.value.toLowerCase()); }); }; list.appendChild(roleSearch);
      if (!roles.length) list.appendChild(el("li", "muted", "尚无角色，点击上方“新建”开始。"));
      roles.forEach((role) => {
        const item = el("li", "role-item" + (state.roleId === role.id ? " active" : ""));
        const avatar = el("button", "role-avatar", role.name.slice(0, 1));
        avatar.title = "配置 " + role.name + " 的角色、模型与渠道";
        avatar.onclick = (e) => { e.stopPropagation(); roleSettings(role).catch((err) => say(err.message, "error")); };
        item.appendChild(avatar);
        const info = el("button", "role-select ghost");
        info.appendChild(el("strong", "", role.name));
        info.appendChild(el("span", "sub", "运维角色"));
        item.dataset.search = (role.name + ' ' + role.id + ' 角色').toLowerCase(); item.hidden = !item.dataset.search.includes((state.roleQuery || '').toLowerCase());
        info.onclick = () => {
          if (state.roleId === role.id) return;
          say('');
          if (state.roleId) chatScroll.set(state.roleId, $('chat-log').scrollTop);
          state.roleId = role.id; currentActiveTurn = null; chatRenderVersion += 1;
          $('chat-log').textContent = ''; $('chat-log').dataset.roleId = ''; $('chat-log').dataset.fresh = 'false';
          $('chat-users').textContent = ''; $('chat-send').disabled = true; $('chat-send').textContent = '加载中'; $('chat-model').textContent = '';
          loadChat();
        };
        item.appendChild(info);
        item.dataset.roleId = role.id;
        list.appendChild(item);
      });
      if (!state.roleId && roles.length) { state.roleId = roles[0].id; return loadChat(); }
      $("chat-title").textContent = state.roleId ? ("聊天 · " + ((roles.find((r) => r.id === state.roleId) || {}).name || state.roleId)) : "聊天";
      if (state.roleId) {
        renderChatLog().catch((err) => say(err.message, 'error'));
        if (!$('chat-users').options.length) accountOptions($('chat-users')).catch((err) => say(err.message, 'error'));
        const [model, runtime] = await Promise.all([api("GET", "/api/v1/roles/" + state.roleId + "/model"), api("GET", "/api/v1/runtime/model")]);
        if (version !== chatLoadVersion || !token || state.view !== 'chat') return;
        $("chat-model").textContent = !model.enabled ? "模型未启用 · 点击角色头像配置" : !runtime.worker_running ? "模型服务未运行 · 请检查服务与角色接口配置" : "模型已启用 · " + (model.model || runtime.default_model || "未设置默认模型");
      }
    } catch (err) { if (version === chatLoadVersion && token && state.view === 'chat') say("加载失败：" + err.message, "error"); }
  }

  async function renderChatLog() {
    const roleId = state.roleId;
    if (!roleId || !token) return;
    const version = ++chatRenderVersion;
    try {
    const cached = chatSnapshots.get(roleId);
    if (cached && $('chat-log').dataset.roleId !== roleId) drawChatLog(roleId, cached.turns, cached.tasks, cached.attention, true);
    const [turns, tasks, attention, waiting] = await Promise.all([
      api("GET", "/api/v1/roles/" + encodeURIComponent(roleId) + "/turns?limit=100&latest=true"),
      api("GET", "/api/v1/console/tasks?limit=100"),
      api("GET", "/api/v1/operator/attention"),
      api("GET", "/api/v1/roles/" + encodeURIComponent(roleId) + "/turns?waiting_user=true")
    ]);
    if (version !== chatRenderVersion || roleId !== state.roleId || state.view !== 'chat' || !token) return;
    let history = chatHistory.get(roleId);
    if (!history) { history = {before: turns.length ? Math.min(...turns.map(t => t.seq)) : null, hasMore: turns.length === 100, busy: false}; chatHistory.set(roleId, history); }
    if (history.before == null && turns.length) { history.before = Math.min(...turns.map(t => t.seq)); history.hasMore = turns.length === 100; }
    // A burst larger than the recent window must not leave an invisible hole in retained history.
    const gapTurns = [];
    const firstRecent = turns.length ? Math.min(...turns.map(t => t.seq)) : 0;
    let after = cached && cached.turns.length ? Math.max(...cached.turns.map(t => t.seq)) : 0;
    if (after && turns.length === 100 && firstRecent > after) {
      while (after < firstRecent - 1) {
        const gap = await api('GET', '/api/v1/roles/' + encodeURIComponent(roleId) + '/turns?limit=500&after=' + after + '&before=' + firstRecent);
        if (version !== chatRenderVersion || roleId !== state.roleId || state.view !== 'chat' || !token) return;
        gapTurns.push(...gap);
        if (gap.length < 500) break;
        after = gap[gap.length - 1].seq;
      }
    }
    const latestIds = new Set(turns.map(t => t.id));
    const retained = (cached ? cached.turns : []).filter(t => !latestIds.has(t.id));
    const refreshed = await Promise.all(retained.map(t => ['completed', 'failed', 'cancelled'].includes(t.state) ? t : api('GET', '/api/v1/turns/' + encodeURIComponent(t.id))));
    const merged = new Map(refreshed.concat(gapTurns, turns, waiting).map(t => [t.id, t]));
    turns.splice(0, turns.length, ...Array.from(merged.values()).sort((a,b) => a.seq-b.seq));
    const taskIds = new Set(tasks.map((task) => task.id));
    await Promise.all(turns.filter((turn) => turn.pending_task_id && !taskIds.has(turn.pending_task_id)).map(async (turn) => {
      try { tasks.push(await api("GET", "/api/v1/tasks/" + turn.pending_task_id)); } catch (err) { if (version === chatRenderVersion && roleId === state.roleId && state.view === 'chat' && token) say("待执行命令详情加载失败：" + err.message, "error"); }
    }));
    if (version !== chatRenderVersion || roleId !== state.roleId || state.view !== "chat" || !token) return;
    const signature = JSON.stringify([turns, tasks, attention]);
    if (cached && cached.signature === signature && $('chat-log').dataset.roleId === roleId && $('chat-log').dataset.fresh === 'true') return;
    chatSnapshots.set(roleId, {turns, tasks, attention, signature});
    if (chatSnapshots.size > 8) { const oldest = chatSnapshots.keys().next().value; chatSnapshots.delete(oldest); chatHistory.delete(oldest); chatScroll.delete(oldest); }
    drawChatLog(roleId, turns, tasks, attention, false);
    } catch (err) { if (version === chatRenderVersion && roleId === state.roleId && state.view === 'chat' && token) say('聊天加载失败，可稍后重试：' + err.message, 'error'); }
  }

  async function loadOlderChat(roleId) {
    const history = chatHistory.get(roleId), generation = authGeneration;
    if (!history || history.busy || !history.hasMore || state.roleId !== roleId || !token) return;
    history.busy = true;
    const button = $('chat-log').querySelector('.chat-older');
    if (button) { button.disabled = true; button.textContent = '加载中…'; }
    try {
      const older = await api('GET', '/api/v1/roles/' + encodeURIComponent(roleId) + '/turns?limit=100&latest=true&before=' + history.before);
      if (generation !== authGeneration || chatHistory.get(roleId) !== history) return;
      const current = chatSnapshots.get(roleId);
      if (!current) return;
      if (state.roleId === roleId && state.view === 'chat') say('');
      history.hasMore = older.length === 100;
      if (older.length) history.before = Math.min(...older.map(t => t.seq));
      const turns = Array.from(new Map(older.concat(current.turns).map(t => [t.id, t])).values()).sort((a,b) => a.seq-b.seq);
      chatSnapshots.set(roleId, {...current, turns, signature: null});
      if (state.roleId === roleId && state.view === 'chat') {
        chatRenderVersion += 1;
        drawChatLog(roleId, turns, current.tasks, current.attention, true, true);
        renderChatLog();
      }
    } catch (err) { if (generation === authGeneration && state.roleId === roleId && state.view === 'chat') say('历史加载失败，可重试：' + err.message, 'error'); }
    finally {
      history.busy = false;
      if (generation === authGeneration && state.roleId === roleId && state.view === 'chat') {
        const retry = $('chat-log').querySelector('.chat-older');
        if (retry) { retry.disabled = !history.hasMore; retry.textContent = history.hasMore ? '加载更早消息' : '已到最早消息'; }
      }
    }
  }

  function drawChatLog(roleId, turns, tasks, attention, cached, preserveHistory = false) {
    const log = $('chat-log');
    const same = log.dataset.roleId === roleId;
    const scrollTop = same ? log.scrollTop : (chatScroll.get(roleId) || 0);
    const follow = !preserveHistory && (same ? log.scrollHeight - log.clientHeight - log.scrollTop < 80 : !chatScroll.has(roleId));
    const logTop = log.getBoundingClientRect ? log.getBoundingClientRect().top : 0;
    const anchor = same && !follow && log.getBoundingClientRect ? Array.from(log.querySelectorAll('[data-turn-id]')).find(n => n.getBoundingClientRect().bottom > logTop) : null;
    const anchorId = anchor && anchor.dataset.turnId, anchorOffset = anchor && anchor.getBoundingClientRect().top - logTop;
    log.dataset.roleId = roleId; log.dataset.fresh = String(!cached);
    chatTasks = new Map(tasks.map((task) => [task.id, task]));
    const linked = new Set(turns.map((turn) => turn.pending_task_id).filter(Boolean));
    log.textContent = "";
    const history = chatHistory.get(roleId);
    if (history && history.before != null) {
      const older = el('button', 'ghost small chat-older', history.busy ? '加载中…' : history.hasMore ? '加载更早消息' : '已到最早消息');
      older.disabled = history.busy || !history.hasMore; older.onclick = () => loadOlderChat(roleId); log.appendChild(older);
    }
    const pendingAttention = (attention.unknown_executions || []).map((task) => ({ ...task, state: "unknown" })).concat((attention.awaiting_approvals || []).map((task) => ({ ...task, state: "awaiting_approval" })));
    const unresolved = new Map(pendingAttention.filter((task) => task.role_id === roleId).map((task) => [task.id, task]));
    tasks.filter((task) => task.role_id === roleId && task.state === "awaiting_approval" && !linked.has(task.id)).forEach((task) => unresolved.set(task.id, task));
    unresolved.forEach((task) => log.appendChild(taskActionCard(chatTasks.get(task.id) || task)));
    (attention.stale_leases || []).filter((task) => !task.role_id || task.role_id === roleId).forEach((task) => {
      const warning = el("div", "msg attention-card");
      warning.appendChild(el("strong", "", "执行租约超期 · " + task.asset_id));
      warning.appendChild(el("p", "muted small", "任务 " + task.task_id + " · " + task.state + " · 仅观测，不自动重派。" + (task.note || "")));
      log.appendChild(warning);
    });
    turns.slice().sort((a, b) => a.seq - b.seq).forEach((turn) => log.appendChild(turnCard(turn)));
    if (!turns.length && !unresolved.size) log.appendChild(el("div", "empty-state", "描述目标、选择执行账号，与角色开始一次运维对话。"));
    log.scrollTop = follow ? log.scrollHeight : scrollTop;
    if (anchorId) {
      const restored = Array.from(log.querySelectorAll('[data-turn-id]')).find(n => n.dataset.turnId === anchorId);
      if (restored) log.scrollTop += restored.getBoundingClientRect().top - logTop - anchorOffset;
    }
    if (cached) log.querySelectorAll('button').forEach((b) => { if (!b.className.includes('chat-older')) b.disabled = true; });
    $('chat-send').disabled = !!cached;
    const active = turns.find((t) => !['completed', 'failed', 'cancelled'].includes(t.state));
    if (!active && currentActiveTurn && currentActiveTurn.role_id === roleId) {
      const initial = new Set((currentActiveTurn.initial_execution_grants || []).map((g) => JSON.stringify(g)));
      Array.from($('chat-users').options).forEach((o) => { o.selected = initial.has(o.value); });
    }
    currentActiveTurn = active || null;
    if (active) {
      const granted = new Set((active.execution_grants || []).map((g) => JSON.stringify({asset_id:g.asset_id, username:g.username})));
      Array.from($('chat-users').options).forEach((o) => { o.selected = granted.has(o.value); });
    }
    $('chat-send').textContent = active ? '停止' : '发送';
    $('chat-send').title = active ? '请求停止本轮；不表示远端已停止，未知结果须核实' : '发送消息';
    $('chat-users').disabled = !!active;
    document.querySelectorAll('.role-item').forEach((item) => { if (item.dataset.roleId === roleId) { item.dataset.status = active ? active.state : (turns.length ? turns[turns.length - 1].state : 'idle'); const sub = item.querySelector('.sub'); if (sub) sub.textContent = active ? active.state === 'waiting_user' ? '等待回答 / 授权' : active.state === 'waiting_tool' ? '等待执行 / 审批' : active.state === 'blocked_unknown' ? '执行结果待核实' : '工作中' : turns.some((t) => t.state === 'failed') ? '最近记录有失败' : '空闲'; } });
  }

  function taskActionCard(task, turn) {
    if (typeof task.payload === "string") { try { task = { ...task, payload: JSON.parse(task.payload) }; } catch (err) { task = { ...task, payload: {} }; } }
    const box = el("div", "task-action attention-card");
    box.appendChild(el("strong", "", task.state === "unknown" ? "执行结果未决 · 必须人工核实" : task.state === "awaiting_approval" ? "命令确认 · 服务端等待审批" : "命令执行 · " + task.state));
    box.appendChild(el("p", "muted small", "任务 " + task.id + " · 资产 " + task.asset_id + " · 账号 " + ((task.payload || {}).run_as || "未提供") + " · 模式 " + ((task.payload || {}).mode || (turn || {}).mode || "服务端未返回")));
    if (task.payload && task.payload.command) box.appendChild(el("pre", "step-cmd", task.payload.command));
    if (task.state === "unknown") {
      box.appendChild(el("p", "error small", "未知不等于失败。先核实目标机实际状态，再标记成功、失败或搁置；禁止以取消代替核实。"));
      const resolve = el("button", "small", "核实并处置"); resolve.onclick = () => resolveDialog(task); box.appendChild(resolve);
    } else if (task.state === "awaiting_approval") {
      const actions = el("div", "row");
      [["approve", "确认执行", "small"], ["cancel", "取消命令", "ghost small"]].forEach(([action, label, cls]) => {
        const button = el("button", cls, label);
        button.onclick = async () => {
          button.disabled = true;
          try { await api("POST", "/api/v1/tasks/" + task.id + "/" + action); await renderChatLog(); say(action === "approve" ? "已批准入队，尚不代表执行成功" : "已取消命令"); }
          catch (err) { say(err.message, "error"); button.disabled = false; }
        };
        actions.appendChild(button);
      });
      box.appendChild(actions);
    }
    return box;
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (!token) return;
    refreshTimer = setTimeout(async () => {
      try {
        if (!document.hidden && $("modal").hidden) {
          if (state.view === "chat") {
            if (Date.now() - lastRoleRefresh > 30000 && !$("chat-roles").contains(document.activeElement)) { lastRoleRefresh = Date.now(); await loadChat(); }
            else await renderChatLog();
          }
          await refreshListCache();
          if (state.view === "assets") await refreshAssetCards();
        }
      } catch (err) { say("刷新失败：" + err.message, "error"); }
      finally { scheduleRefresh(); }
    }, 5000);
  }

  function questionCard(turn, question) {
    const box = el('section', 'decision-card'); box.dataset.questionId = question.id;
    const pending = question.state === 'pending' && turn.state === 'waiting_user';
    box.appendChild(el('strong', '', pending ? '需要你选择 · 等待回答' : question.state === 'answered' ? '已回答的选择' : '选择已结束'));
    box.appendChild(el('p', 'decision-question', question.question));
    if (!pending) {
      if (question.answer) box.appendChild(el('p', '', '你的回答：' + question.answer.label + (question.answer.text ? ' · ' + question.answer.text : '')));
      else box.appendChild(el('p', 'muted small', '本轮已结束，不能再提交此选择。'));
      questionDrafts.delete(question.id);
      questionSubmissions.delete(question.id);
      return box;
    }
    box.appendChild(el('p', 'muted small', '可以稍后回答，等待期间不调用模型。同角色后续消息会排队。选择不是命令执行审批，不会改变本轮权限。'));
    const draft = questionDrafts.get(question.id) || { option_id: '', text: '' }; questionDrafts.set(question.id, draft);
    const fields = [];
    const custom = el('textarea'); custom.placeholder = '请输入你的其他方案或补充信息'; custom.maxLength = 4000; custom.value = draft.text; custom.hidden = draft.option_id !== 'other'; custom.setAttribute('aria-label', '其他自定义回答');
    custom.oninput = () => { draft.text = custom.value; };
    const choices = (question.options || []).concat([{ id: 'other', label: '其他', description: '自定义回答' }]);
    choices.forEach((option) => {
      const label = el('label', 'decision-option'); const radio = el('input'); radio.type = 'radio'; radio.name = 'decision-' + question.id; radio.value = option.id; radio.checked = draft.option_id === option.id;
      radio.onchange = () => { if (radio.checked) { draft.option_id = option.id; custom.hidden = option.id !== 'other'; } };
      fields.push(radio); label.appendChild(radio); const text = el('span'); text.appendChild(el('strong', '', option.label)); if (option.description) text.appendChild(el('span', 'sub', option.description)); label.appendChild(text); box.appendChild(label);
    });
    box.appendChild(custom); fields.push(custom);
    const error = el('p', 'error small'); error.setAttribute('role', 'alert'); box.appendChild(error);
    const actions = el('div', 'row'); const submit = el('button', 'small', '提交选择并继续'); const cancel = el('button', 'ghost small', '取消整轮任务');
    const lock = (value) => { fields.concat([submit, cancel]).forEach((n) => { n.disabled = value; }); }; lock(questionSubmissions.has(question.id));
    submit.onclick = async () => {
      if (questionSubmissions.has(question.id)) return;
      const text = draft.option_id === 'other' ? draft.text.trim() : '';
      if (!choices.some((o) => o.id === draft.option_id)) { error.textContent = '请先选择一项'; return; }
      if (draft.option_id === 'other' && !text) { error.textContent = '请填写其他回答'; return; }
      if (text.length > 4000) { error.textContent = '其他回答最多4000字'; return; }
      questionSubmissions.add(question.id); lock(true); error.textContent = ''; let accepted = false;
      try { await api('POST', '/api/v1/turns/' + encodeURIComponent(turn.id) + '/questions/' + encodeURIComponent(question.id) + '/answer', { option_id: draft.option_id, text }); accepted = true; questionDrafts.delete(question.id); submit.textContent = '已提交'; await renderChatLog(); }
      catch (err) { error.textContent = err.message; say(err.message, 'error'); }
      finally { if (!accepted) questionSubmissions.delete(question.id); lock(accepted); }
    };
    cancel.onclick = async () => {
      if (questionSubmissions.has(question.id) || !confirm('取消整轮任务？这会结束原任务，不是把“取消方案”作为答案交给 AI。')) return;
      questionSubmissions.add(question.id); lock(true);
      try { await api('POST', '/api/v1/turns/' + encodeURIComponent(turn.id) + '/cancel'); questionDrafts.delete(question.id); await renderChatLog(); }
      catch (err) { error.textContent = err.message; }
      finally { questionSubmissions.delete(question.id); lock(false); }
    };
    actions.appendChild(submit); box.appendChild(actions);
    box.appendChild(el('p', 'muted small', '普通回答不会授予执行权限。需结束本轮时，使用输入框旁的停止按钮。'));
    return box;
  }

  function accountRequestCard(turn, request) {
    const box = el('section', 'decision-card'); box.dataset.accountRequestId = request.id;
    box.appendChild(el('strong', '', '本轮账号授权 · ' + request.asset_id + '.' + request.username));
    box.appendChild(el('p', '', request.reason));
    box.appendChild(el('p', 'muted small', '仅当前轮次；不替代命令审批。账号未登记时请先在资产中配置，不会自动创建 OS 账号。批准者仅能归因共享管理 Token 信任域。'));
    if (request.state !== 'pending' || turn.state !== 'waiting_user') { box.appendChild(el('p', '', request.answer || request.state)); return box; }
    const buttons = [];
    const lock = value => buttons.forEach(button => { button.disabled = value; });
    ['approve', 'reject'].forEach((decision) => {
      const button = el('button', decision === 'approve' ? 'small' : 'ghost small', decision === 'approve' ? '授权本轮使用' : '拒绝');
      button.onclick = async () => {
        if (accountSubmissions.has(request.id)) return;
        const generation = authGeneration;
        accountSubmissions.add(request.id); lock(true);
        try { await api('POST', '/api/v1/turns/' + turn.id + '/account-requests/' + request.id + '/answer', {decision, revision: request.revision});
          if (generation === authGeneration && state.roleId === turn.role_id) { await renderChatLog(); await accountOptions($('chat-users')); }
        } catch (err) { if (generation === authGeneration) say('账号授权操作未完成：' + err.message, 'error'); }
        finally { if (generation === authGeneration) {
          accountSubmissions.delete(request.id); lock(false);
          document.querySelectorAll('.decision-card').forEach(card => { if (card.dataset.accountRequestId === request.id) card.querySelectorAll('button').forEach(button => { button.disabled = false; }); });
        } }
      }; buttons.push(button); box.appendChild(button);
    }); lock(accountSubmissions.has(request.id)); return box;
  }

  function turnCard(turn) {
    const card = el("div", "conversation-turn");
    const meta = el("div", "meta");
    meta.appendChild(el("span", "", turn.source + " · " + fmt(turn.created_at)));
    meta.appendChild(el("span", "", "轮次 " + shortId(turn.id)));
    meta.appendChild(el("span", pillClass(turn.state), turn.state));
    if (turn.mode) meta.appendChild(el("span", "pill", turn.mode));
    meta.appendChild(el('span', 'muted small', '账号: ' + ((turn.execution_grants || []).map((g) => g.asset_id + '.' + g.username).join(', ') || '无') + ' · 授权版本 ' + (turn.authorization_revision || 0)));
    const detailBox = el('details', 'turn-details');
    const detailsKey = turn.id + (['completed', 'failed', 'cancelled'].includes(turn.state) ? ':detail-done' : ':detail-live');
    detailBox.open = stepExpansion.has(detailsKey) ? stepExpansion.get(detailsKey) : !['completed', 'failed', 'cancelled'].includes(turn.state);
    detailBox.ontoggle = () => stepExpansion.set(detailsKey, detailBox.open);
    detailBox.appendChild(el("summary", "muted small", "运行详情 · " + turn.state)); detailBox.appendChild(meta);
    card.dataset.turnId = turn.id;
    const user = el("div", "chat-bubble user-bubble"); user.appendChild(el("span", "bubble-label", turn.source === "chat" ? "你" : "事件 · " + turn.source)); user.appendChild(el("pre", "", turn.prompt)); card.appendChild(user);

    (turn.attachments || []).forEach((attachment) => user.appendChild(attachmentPreview(attachment, turn.role_id)));
    if (turn.payload && Object.keys(turn.payload).length) {
      detailBox.appendChild(el("div", "tool", "事件数据：" + JSON.stringify(turn.payload)));
    }
    if (turn.error_code) card.appendChild(el("div", "error small", "错误：" + turn.error_code));
    (turn.questions || []).forEach((question) => card.appendChild(questionCard(turn, question)));
    (turn.account_requests || []).forEach((request) => card.appendChild(accountRequestCard(turn, request)));
    const task = chatTasks.get(turn.pending_task_id);
    if (task) (['unknown', 'awaiting_approval'].includes(task.state) ? card : detailBox).appendChild(taskActionCard(task, turn));
    else if (turn.state === "blocked_unknown") card.appendChild(el("p", "error", "本轮存在未决执行；请在聊天上方未决卡片核实，不可直接重跑。"));
    if (turn.steps && turn.steps.length) {
      detailBox.appendChild(stepsPanel(turn));
    } else if (["queued", "ready", "calling", "waiting_tool"].includes(turn.state)) {
      card.appendChild(el("p", "muted small", "处理中 · " + turn.state));
    }
    const details = el("button", "ghost small", "查看完整轮次 / 模型调用");
    details.style.marginTop = "8px";
    details.onclick = async () => {
      const full = await api("GET", "/api/v1/turns/" + turn.id);
      modal("轮次 " + turn.id, full);
    };
    detailBox.appendChild(details);
    if (['failed', 'cancelled', 'blocked_unknown'].includes(turn.state)) card.appendChild(el("p", "error small", "本轮状态 · " + turn.state));
    if (turn.final_text) {
      const final = el("div", "chat-bubble assistant-bubble");
      final.style.marginTop = "8px";
      final.appendChild(el("div", "bubble-label", "助手"));
      final.appendChild(el("pre", "", turn.final_text));
      card.appendChild(final);
    }
    card.appendChild(detailBox);
    return card;
  }

  // "分析步骤": the model's own intent line before each tool call, plus the
  // command it proposed and the result excerpt. Derived from the transcript, so
  // it is exactly what the model saw and did. Collapsed by default: a user who
  // wants the process can open it; a user who trusts the result can ignore it,
  // and either way it is visible proof of whether the turn is stuck or moving.
  function stepsPanel(turn) {
    const box = el("div", "steps");
    box.style.marginTop = "8px";
    const terminal = ["completed", "failed", "cancelled"].includes(turn.state);
    const expansionKey = turn.id + (terminal ? ":done" : ":live");
    let open = stepExpansion.has(expansionKey) ? stepExpansion.get(expansionKey) : !terminal;
    const head = el("button", "ghost small", "执行流水线 · " + turn.steps.length + " 步 · " + turn.state);
    head.setAttribute("aria-expanded", String(open));
    const body = el("div", "steps-body");
    body.hidden = !open;
    turn.steps.forEach((step, index) => body.appendChild(stepRow(step, index + 1)));
    head.onclick = () => { open = !open; stepExpansion.set(expansionKey, open); body.hidden = !open; head.setAttribute("aria-expanded", String(open)); }; 
    box.appendChild(head);
    box.appendChild(body);
    return box;
  }

  function stepRow(step, n) {
    const row = el("div", "step step-" + step.kind);
    const head = el("div", "step-head");
    head.appendChild(el("span", "step-no", String(n)));
    if (step.kind === "thought") {
      head.appendChild(el("span", "step-label", "执行说明"));
      row.appendChild(head);
      row.appendChild(el("div", "step-text", step.text));
      return row;
    }
    if (step.kind === "command") {
      head.appendChild(el("span", "step-label", "执行"));
      head.appendChild(el("span", "pill " + stepPill(step.status), step.status));
      if (step.run_as) head.appendChild(el("span", "muted small", "账号 " + step.run_as));
      if (step.exit_code !== null && step.exit_code !== undefined) {
        head.appendChild(el("span", "muted small", "exit " + step.exit_code));
      }
      if (step.truncated) head.appendChild(el("span", "muted small", "输出已截断"));
      row.appendChild(head);
      row.appendChild(el("pre", "step-cmd", "$ " + (step.command || "")));
      if (step.output_excerpt) row.appendChild(el("pre", "step-out", step.output_excerpt));
      if (step.error) row.appendChild(el("div", "error small", "失败：" + step.error));
      return row;
    }
    if (step.kind === "search") {
      head.appendChild(el("span", "step-label", "检索"));
      head.appendChild(el("span", "pill " + stepPill(step.status), step.status));
      row.appendChild(head);
      row.appendChild(el("div", "step-text", step.query ? ("搜索：" + step.query) : ("抓取：" + (step.url || ""))));
      if (step.output_excerpt) row.appendChild(el("pre", "step-out", step.output_excerpt));
      return row;
    }
    head.appendChild(el("span", "step-label", "工具"));
    row.appendChild(head);
    if (step.preview) row.appendChild(el("pre", "step-out", step.preview));
    return row;
  }

  function stepPill(status) {
    if (status === "done" || status === "succeeded") return "ok";
    if (status === "failed" || status === "unknown") return "bad";
    return "warn";
  }

  function pillClass(value) {
    if (["completed", "succeeded"].includes(value)) return "pill ok";
    if (["failed", "cancelled", "blocked_unknown", "unknown"].includes(value)) return "pill bad";
    return "pill warn";
  }

  $("chat-new-role").onclick = () => roleCreator();

  // Advanced options stay collapsed until needed: the default path is just
  // "type what you want and send". Defaults (confirm mode) are the safe ones.
  $("opts-toggle").onclick = () => {
    const body = $("opts-body");
    const open = body.hidden;
    body.hidden = !open;
    $("opts-toggle").textContent = open ? "选项 ▴" : "选项 ▾";
    $("opts-toggle").setAttribute("aria-expanded", String(open));
  };
  $("chat-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  });

  $("chat-form").onsubmit = async (e) => {
    e.preventDefault();
    if (!state.roleId) { say("请先选择角色", "error"); return; }
    if (chatBusy) return;
    if ($('chat-send').disabled) { say('正在核对本轮最新状态，请稍后发送或停止', 'error'); return; }
    if (currentActiveTurn && currentActiveTurn.role_id === state.roleId) {
      const active = currentActiveTurn; chatBusy = true;
      try { const answer = await api('POST', '/api/v1/turns/' + active.id + '/cancel'); say('停止请求状态：' + answer.state + '。不代表远端已停止；未知结果须核实。'); await renderChatLog(); }
      catch (err) { say(err.message, 'error'); } finally { chatBusy = false; }
      return;
    }
    const roleId = state.roleId, generation = authGeneration;
    const text = $("chat-text").value.trim();
    const drafts = (imageDrafts.get(roleId) || []).slice();
    if (!text && !drafts.length) return;
    const body = {
      text,
      execution_grants: selectedGrants($('chat-users')),
      mode: $("chat-mode").value,
      idempotency_key: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, ""),
    };
    if (body.idempotency_key.length < 8) body.idempotency_key = "k" + body.idempotency_key;
    chatBusy = true; drawImageDrafts();
    try {
      if (drafts.length) body.attachment_ids = await uploadImages(roleId, drafts);
      if (generation !== authGeneration || !token) throw new Error('登录已切换');
      const answer = await api("POST", "/api/v1/roles/" + roleId + "/messages", body);
      drafts.forEach((draft) => { URL.revokeObjectURL(draft.preview); imageURLs.delete(draft.preview); });
      imageDrafts.delete(roleId);
      if (state.roleId === roleId) { $("chat-text").value = ""; drawImageDrafts(); }
      say(answer.duplicate ? "重复消息，已复用原轮次" : "已入队，等待模型处理…");
      await renderChatLog();
      scheduleRefresh();
    } catch (err) { say("发送失败：" + err.message, "error"); }
    finally { chatBusy = false; drawImageDrafts(); }
  };

  /* ---------- screenshot attachments: authenticated, never public URLs ---------- */
  function drawImageDrafts() {
    const roleId = state.roleId;
    const box = $('image-drafts'); box.textContent = ''; $('image-add').disabled = chatBusy;
    (imageDrafts.get(roleId) || []).forEach((draft) => {
      const item = el('div', 'image-draft'); const image = el('img'); image.src = draft.preview; image.alt = '待发送截图'; item.appendChild(image);
      const remove = el('button', 'ghost small', '移除'); remove.type = 'button'; remove.disabled = chatBusy;
      remove.onclick = () => { if (chatBusy || state.roleId !== roleId) return; const list = imageDrafts.get(roleId) || []; if (!list.includes(draft)) return; imageDrafts.set(roleId, list.filter((x) => x !== draft)); URL.revokeObjectURL(draft.preview); imageURLs.delete(draft.preview); drawImageDrafts(); };
      item.appendChild(remove); box.appendChild(item);
    });
  }
  function addImages(files) {
    if (!state.roleId || !token) { say('请先登录并选择角色', 'error'); return; }
    if (chatBusy) { say('请等待当前提交完成', 'error'); return; }
    const list = imageDrafts.get(state.roleId) || [];
    for (const file of Array.from(files || [])) {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { say('仅支持 PNG、JPEG、WebP 图片', 'error'); continue; }
      if (file.size > 8 * 1024 * 1024 || list.length >= 4) { say('每轮最多4张图片，单张不超过8MiB', 'error'); break; }
      const preview = URL.createObjectURL(file); imageURLs.add(preview); list.push({file, preview, id: null});
    }
    imageDrafts.set(state.roleId, list); drawImageDrafts();
  }
  $('image-add').onclick = () => $('image-files').click();
  $('image-files').onchange = () => { addImages($('image-files').files); $('image-files').value = ''; };
  $('chat-text').addEventListener('paste', (event) => {
    const files = Array.from((event.clipboardData || {}).files || []);
    if (files.length) { event.preventDefault(); addImages(files); }
  });
  $('chat-form').addEventListener('dragover', (event) => { event.preventDefault(); });
  $('chat-form').addEventListener('drop', (event) => { event.preventDefault(); addImages((event.dataTransfer || {}).files); });
  async function uploadImages(roleId, drafts) {
    const generation = authGeneration;
    for (const draft of drafts) {
      if (draft.id) continue;
      const control = new AbortController(); controllers.add(control);
      try {
        const response = await fetch('/api/v1/attachments?role_id=' + encodeURIComponent(roleId), {method: 'POST',
          headers: {Authorization: 'Bearer ' + token, 'Content-Type': draft.file.type}, body: draft.file, signal: control.signal, cache: 'no-store'});
        const result = await response.json();
        if (generation !== authGeneration || !token) throw new Error('登录已切换');
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '图片上传失败');
        draft.id = result.id;
      } finally { controllers.delete(control); }
    }
    return drafts.map((x) => x.id);
  }
  function attachmentPreview(attachment, roleId) {
    const box = el('div', 'image-message'); const load = el('button', 'ghost small', '查看截图 · ' + attachment.width + '×' + attachment.height); load.type = 'button'; box.appendChild(load);
    load.onclick = async () => {
      const generation = authGeneration; const control = new AbortController(); controllers.add(control); load.disabled = true;
      try {
        const response = await fetch('/api/v1/attachments/' + encodeURIComponent(attachment.id) + '?role_id=' + encodeURIComponent(roleId), {headers: {Authorization: 'Bearer ' + token}, cache: 'no-store', signal: control.signal});
        if (!response.ok) throw new Error('图片无法读取：HTTP ' + response.status);
        const blob = await response.blob();
        if (generation !== authGeneration || !token || !box.isConnected) return;
        const url = URL.createObjectURL(blob); imageURLs.add(url);
        const image = el('img'); image.alt = '用户提供的截图'; image.src = url; box.appendChild(image); load.hidden = true;
      } catch (err) { load.disabled = false; say(err.message, 'error'); }
      finally { controllers.delete(control); }
    };
    return box;
  }

  /* ---------- console ---------- */
  let consoleDirty = false;
  async function refreshListCache() {
    const paths = { skills: '/api/v1/skills', documents: '/api/v1/documents', custom: '/api/v1/custom-tasks' };
    const path = paths[state.view], view = state.view;
    if (!path || consoleDirty || $('console-body').contains(document.activeElement)) return;
    const old = readCache.get(path);
    if (!old || Date.now() - old.at >= CACHE_TTL) await api('GET', path, undefined, true);
    if (state.view === view && changedLists.has(path) && !consoleDirty) { changedLists.delete(path); renderConsole(); }
  }
  function pageHelp(host, title, paragraphs) {
    const heading = host.querySelector ? host.querySelector('h3') : host.children.find((n) => n.tagName === 'h3');
    const help = el('span', 'page-help');
    const button = el('button', 'ghost small help-button', '▤'); button.type = 'button'; button.setAttribute('aria-label', title); button.setAttribute('aria-expanded', 'false');
    const panel = el('span', 'help-panel'); panel.hidden = true; panel.setAttribute('role', 'note');
    paragraphs.forEach((text) => panel.appendChild(el('p', '', text)));
    const show = (open) => { panel.hidden = !open; button.setAttribute('aria-expanded', String(open)); };
    let pinned = false;
    button.onfocus = () => show(true); help.onmouseenter = () => show(true);
    help.onmouseleave = () => { if (!pinned && document.activeElement !== button) show(false); };
    button.onclick = () => { pinned = !pinned; show(pinned); };
    help.onfocusout = (event) => { if (!help.contains(event.relatedTarget)) { pinned = false; show(false); } };
    help.onkeydown = (event) => { if (event.key === 'Escape') { pinned = false; show(false); event.preventDefault(); } };
    help.appendChild(button); help.appendChild(panel); (heading || host).appendChild(help); return help;
  }
  function renderConsole() {
    if (state.view === "chat") { loadChat(); return; }
    const body = $("console-body");
    consoleDirty = false; body.oninput = () => { consoleDirty = true; }; body.onchange = () => { consoleDirty = true; };
    body.textContent = "";
    const head = el("div", "pane-head");
    head.appendChild(el("h2", "", (VIEWS.find(([id]) => id === state.tab) || ["", "资产"])[1]));
    const refresh = el("button", "ghost small", "刷新");
    refresh.onclick = () => { readCache.clear(); renderConsole(); }; head.appendChild(refresh);
    body.appendChild(head);
    const holder = el("div", "workspace-content");
    body.appendChild(holder);
    const loaders = { assets: loadAssets, custom: loadCustom, skills: loadSkills, memory: loadMemory, documents: loadDocuments, logs: loadLogs };
    (loaders[state.tab] || loadAssets)(holder).catch((err) => holder.appendChild(el("div", "card error", "加载失败：" + err.message)));
  }

  function resourceIdentity(id) {
    const detail = el("details", "resource-id"); detail.appendChild(el("summary", "muted small", "资源 ID / 复制")); detail.appendChild(el("code", "", id));
    const copy = el("button", "ghost small", "复制 ID"); copy.onclick = async () => { try { await navigator.clipboard.writeText(id); copy.textContent = "已复制"; } catch (err) { modal("资源 ID（可选中复制）", id); } }; detail.appendChild(copy); return detail;
  }
  function resourceSearch(host, target, label) {
    const search = el("input"); search.placeholder = label || "搜索名称、类型或 ID"; search.setAttribute("aria-label", search.placeholder);
    search.oninput = () => { const q = search.value.trim().toLowerCase(); Array.from(target.children).forEach((node) => { node.hidden = !!q && !(node.dataset.search || node.textContent).toLowerCase().includes(q); }); };
    host.appendChild(search); return search;
  }
  function card(title) {
    const box = el("div", "card");
    if (title) box.appendChild(el("h3", "", title));
    return box;
  }
  function kv(key, value) {
    const box = el("div", "kv");
    box.appendChild(el("div", "k", key));
    box.appendChild(el("div", "v", value === undefined || value === null ? "-" : value));
    return box;
  }
  function table(columns, rows, onRow) {
    const wrap = el("div", "");
    const node = el("table");
    const search = el("input"); search.placeholder = "搜索名称、类型或 ID（当前列表）"; wrap.appendChild(search);
    search.oninput = () => { const query = search.value.trim().toLowerCase(); Array.from(node.children).slice(1).forEach((row) => { row.hidden = !row.textContent.toLowerCase().includes(query); }); };
    const head = el("tr");
    columns.forEach((c) => head.appendChild(el("th", "", c)));
    node.appendChild(head);
    rows.forEach((row) => {
      const tr = el("tr", onRow ? "click" : "");
      row.forEach((cell) => tr.appendChild(el("td", "", cell === undefined || cell === null ? "-" : cell)));
      if (onRow) tr.onclick = () => onRow(row);
      node.appendChild(tr);
    });
    wrap.appendChild(node);
    if (!rows.length) wrap.appendChild(el("p", "muted small", "无数据"));
    return wrap;
  }

  function resolveDialog(row) {
    if (!row) return;
    const box = el("div", "");
    box.appendChild(el("p", "muted small", "任务 " + row.id + " @ " + row.asset_id + " · 角色 " + row.role_id));
    const note = el("input", "");
    note.placeholder = "至少 3 个字符的核实说明";
    note.style.margin = "8px 0";
    box.appendChild(note);
    const feedback = el("p", "error small", "");
    const actions = [["confirm_succeeded", "核实：成功"], ["confirm_failed", "核实：失败"], ["abandon", "无法查明，搁置"]];
    const buttons = el("div", "row");
    actions.forEach(([action, label]) => {
      const button = el("button", action === "abandon" ? "ghost" : "small", label);
      button.onclick = async () => {
        try {
          await api("POST", "/api/v1/tasks/" + row.id + "/resolve", { action, note: note.value, confirm_task_id: row.id });
          $("modal").hidden = true;
          say("已处置");
          if (state.view === "chat") await renderChatLog(); else renderConsole();
        } catch (err) { feedback.textContent = err.message; }
      };
      buttons.appendChild(button);
    });
    box.appendChild(buttons);
    box.appendChild(feedback);
    $("modal-title").textContent = "处置 " + shortId(row.id);
    $("modal-body").textContent = "";
    $("modal-body").appendChild(box);
    $("modal").hidden = false;
  }

  async function loadAlarms(host) {
    const data = await api("GET", "/api/v1/alarms?limit=100");
    const sources = await api("GET", "/api/v1/alarms/sources");
    const summary = card("按来源计数");
    summary.appendChild(table(["来源", "条数", "最近序号"], sources.map((s) => [s.source, s.count, s.last_seq])));
    host.appendChild(summary);
    host.appendChild(card("告警日志（最近 100 条，payload 已脱敏）")).appendChild(
      table(["时间", "状态", "来源", "严重度", "标题", "任务"], data.map((r) => [fmt(r.received_at), r.state, r.source, r.severity, r.title, shortId(r.custom_task_id)]), (row) => {
        const record = data.find((r) => fmt(r.received_at) === row[0] && r.title === row[4]);
        modal("告警 " + shortId(record.id), record);
      }));
  }

  async function roleCreator() {
    const box = el("div"); modalContent("新建角色", box);
    const name = el("input"); name.placeholder = "角色名称"; box.appendChild(name);
    const provider = roleModelFields(box, { enabled: true, provider_mode: 'independent', max_model_steps: null, new_role: true });
    const runtimeText = el("p", "muted small", "正在检查全局模型服务…"); box.appendChild(runtimeText);
    let runtime;
    try { runtime = await api("GET", "/api/v1/runtime/model"); runtimeText.textContent = "全局默认模型：" + (runtime.default_model || "未设置") + " · " + (runtime.worker_running ? "服务运行中" : "服务未运行") + "。" + runtime.configuration_help; }
    catch (err) { runtimeText.textContent = "无法读取全局模型状态：" + err.message; }
    box.appendChild(el("p", "muted small", "ID 由系统生成。请为新角色填写独立接口、API Key 与模型名称。未就绪时可关闭模型后保存角色；不会把模型未就绪的聊天静默入队。创建后在角色设置中管理消息渠道。"));
    const feedback = el("p", "error small"); const save = el("button", "", "创建角色与模型设置");
    save.onclick = async () => {
      save.disabled = true;
      try { const created = await api("POST", "/api/v1/console/roles", { name: name.value.trim(), model: provider.body() }); provider.clearSecret(); $("modal-body").textContent = ""; $("modal").hidden = true; state.roleId = created.id; say("角色与模型设置已创建"); await loadChat(); }
      catch (err) { provider.clearSecret(); feedback.textContent = "创建失败（若填写过密钥，请重新输入）。"; save.disabled = false; }
    };
    box.appendChild(save); box.appendChild(feedback);
  }

  async function loadTurns(host) {
    const roles = await api("GET", "/api/v1/roles");
    if (!roles.length) { host.appendChild(el("div", "card muted", "尚无角色")); return; }
    const picker = card("选择角色");
    const select = el("select");
    roles.forEach((role) => { const option = el("option", "", role.name + " (" + role.id + ")"); option.value = role.id; select.appendChild(option); });
    if (state.roleId) select.value = state.roleId;
    const button = el("button", "", "查看");
    const holder = el("div", "");
    button.onclick = async () => {
      holder.textContent = "";
      const turns = await api("GET", "/api/v1/roles/" + select.value + "/turns?limit=100");
      holder.appendChild(table(["时间", "来源", "状态", "模式", "账号", "提示词"], turns.map((t) => [fmt(t.created_at), t.source, t.state, t.mode, usersToText(t.execution_users), t.prompt]), (row) => {
        const turn = turns.find((t) => fmt(t.created_at) === row[0] && t.prompt === row[5]);
        api("GET", "/api/v1/turns/" + turn.id).then((full) => modal("轮次 " + turn.id, full)).catch((err) => say(err.message, "error"));
      }));
    };
    picker.appendChild(select); picker.appendChild(button); picker.appendChild(holder);
    host.appendChild(picker);
    if (state.roleId) await button.onclick();
  }

  async function loadCustom(host) {
    const data = await api("GET", "/api/v1/custom-tasks");
    const box = card("定制任务（触发任务 / 定时任务）");
    const add = el("button", "small", "新建定制任务");
    add.onclick = () => customTaskEditor(null);
    box.appendChild(add);
    box.appendChild(table(["名称", "类型", "角色", "模式", "启用", "下次触发", "ID", "操作"],
      data.map((r) => [r.name, r.kind === 'trigger' ? '触发任务 trigger' : '定时任务 scheduled', r.role_id, r.mode, r.enabled ? "是" : "否", fmt(r.next_fire_at), r.id, "编辑"]), (row) => {
      customTaskEditor(data.find((r) => r.id === row[6]));
    }));
    host.appendChild(box);
    pageHelp(box, '定制任务说明', ['每个定制任务可单独配置结果通知；执行、触发和投递记录统一在“日志”查看。']);

  }

  function customTaskEditor(record) {
    const box = el("div", ""); if (record) box.appendChild(resourceIdentity(record.id));
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "系统自动生成 ID"; idInput.hidden = true;
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const kindSelect = el("select");
    [["trigger", "trigger（被调用触发）"], ["scheduled", "scheduled（定时）"]].forEach(([v, l]) => { const o = el("option", "", l); o.value = v; kindSelect.appendChild(o); });
    const roleSelect = el("select");
    const modeSelect = el("select");
    ["confirm", "readonly", "direct"].forEach((m) => { const o = el("option", "", m); o.value = m; modeSelect.appendChild(o); });
    const usersInput = el('select'); usersInput.multiple = true; usersInput.setAttribute('aria-label', '资产执行账号'); accountOptions(usersInput, record ? record.execution_grants || [] : []).catch((err) => say(err.message, 'error'));
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(kindSelect); fields.appendChild(roleSelect); fields.appendChild(modeSelect); fields.appendChild(usersInput);
    const prompt = el("textarea"); prompt.rows = 5; prompt.style.width = "100%"; prompt.placeholder = "提示词";
    const schedFields = el("div", "fields");
    const cronInput = el("input"); cronInput.placeholder = "cron（如 0 3 * * *）";
    const tzInput = el("input"); tzInput.placeholder = "时区（默认 Asia/Shanghai）"; tzInput.value = "Asia/Shanghai";
    const enabledInput = el("input"); enabledInput.type = "checkbox"; enabledInput.checked = true;
    const enabledWrap = el("label", "inline"); enabledWrap.appendChild(enabledInput); enabledWrap.appendChild(el("span", "", "启用"));
    schedFields.appendChild(cronInput); schedFields.appendChild(tzInput); fields.appendChild(enabledWrap);
    if (record) {
      idInput.value = record.id; idInput.disabled = true;
      nameInput.value = record.name; kindSelect.value = record.kind;
      modeSelect.value = record.mode;
      prompt.value = record.prompt; cronInput.value = record.cron || ""; tzInput.value = record.timezone;
      enabledInput.checked = !!record.enabled;
    }
    const notify = el("section", "notification-options"); notify.appendChild(el("h3", "", "完成后通知 · Webhook"));
    const notification = (record && record.notification) || {};
    let notificationChanged = false, clearNotification = false;
    const notifyMode = {value: !record ? 'off' : record.notification == null ? 'inherit' : notification.enabled ? 'custom' : 'off'};
    if (record && record.notification == null) notify.appendChild(el('p', 'muted small', '旧任务沿用原全局通知路由：不修改则保持；填写新地址将替换，删除地址将明确关闭。'));
    const notifyUrl = el("input"); notifyUrl.type = "password"; notifyUrl.autocomplete = "off"; notifyUrl.placeholder = notification.url ? "已配置（留空保留原地址）" : "https://… Webhook 地址";
    const notifyChannel = el("select"); ["auto", "feishu", "dingtalk", "wecom", "slack", "discord", "generic"].forEach((value) => { const option = el("option", "", value); option.value = value; notifyChannel.appendChild(option); }); notifyChannel.value = notification.channel || "auto";
    const notifyConsole = el('input'); notifyConsole.placeholder = '结果详情链接入口（高级覆盖，可用私网地址）'; notifyConsole.value = notification.console_url || window.location.origin; notifyConsole.setAttribute('aria-label', '通知结果详情链接');
    const notifyFields = el("div", "fields"); notifyFields.appendChild(notifyUrl); notifyFields.appendChild(notifyChannel); notify.appendChild(notifyFields);
    const notifyAdvanced = el('details', 'section-details'); notifyAdvanced.appendChild(el('summary', '', '高级：结果详情链接'));
    const consoleLabel = el('label', 'field'); consoleLabel.appendChild(el('span', '', '结果详情链接入口')); consoleLabel.appendChild(notifyConsole); notifyAdvanced.appendChild(consoleLabel);
    notifyAdvanced.appendChild(el('p', 'muted small', '默认使用当前控制台入口，可覆盖为接收者能访问的地址。这是通知中的结果链接，不是 Webhook 地址或任务触发 URL；展开查看不会修改已保存配置。')); notify.appendChild(notifyAdvanced);
    const test = el("button", "ghost small", "测试连通（发送一条测试消息）"); const testOut = el("p", "small"); testOut.setAttribute('role', 'status'); testOut.setAttribute('aria-live', 'polite');
    let testingNotification = false;
    notify.appendChild(test); notify.appendChild(testOut);
    const clear = el('button', 'ghost danger small', '删除地址并关闭通知'); clear.hidden = !record; notify.appendChild(clear);
    const toggleNotify = () => { test.disabled = testingNotification || clearNotification || (!notifyUrl.value.trim() && !notification.url && notifyMode.value !== 'inherit'); };
    notifyUrl.oninput = () => { notificationChanged = true; clearNotification = false; if (notifyUrl.value.trim()) notifyMode.value = 'custom'; toggleNotify(); };
    notifyChannel.onchange = notifyConsole.onchange = () => { notificationChanged = true; };
    clear.onclick = () => { if (confirm('删除本任务通知地址并关闭通知？不会回退到全局配置。')) { notificationChanged = true; clearNotification = true; notifyMode.value = 'off'; notifyUrl.value = ''; notifyUrl.placeholder = '保存后删除地址并关闭'; toggleNotify(); } }; toggleNotify();
    test.onclick = async () => {
      if (testingNotification) return;
      testingNotification = true; toggleNotify(); testOut.className = 'small'; testOut.textContent = '正在发送测试消息…';
      try {
        const result = await api("POST", "/api/v1/custom-tasks/notification-test", {mode: notifyMode.value === "inherit" ? "inherit" : "custom", ...(record ? {custom_task_id: record.id} : {}), ...(notifyUrl.value.trim() ? {url: notifyUrl.value.trim()} : {}), channel: notifyChannel.value});
        const http = Number.isInteger(result.http_status) ? String(result.http_status) : '状态未知';
        if (result.http_delivered !== true) { testOut.className = 'error small'; testOut.textContent = 'HTTP 投递失败 · ' + http + '；未确认通知送达，请检查地址、网络或目标服务。'; }
        else if (result.business_accepted === false || result.error === 'WEBHOOK_BUSINESS_REJECTED') { testOut.className = 'error small'; testOut.textContent = 'HTTP 已送达 · ' + http + '，但机器人业务拒绝或响应校验未通过；未确认通知成功。请检查机器人配置与服务端返回。'; }
        else if (result.business_accepted === true) { testOut.textContent = 'HTTP 已送达 · ' + http + '；机器人业务校验通过（不代表收件人已收到或阅读）。'; }
        else { testOut.textContent = 'HTTP 已送达 · ' + http + '；未确认业务接收结果（不代表收件人已收到或阅读）。'; }
      }
      catch (err) { testOut.className = 'error small'; testOut.textContent = '测试请求失败' + (Number.isInteger(err.status) ? ' · HTTP ' + err.status : '') + '；未确认投递结果，请检查配置与网络。错误详情不回显通知地址。'; }
      finally { testingNotification = false; toggleNotify(); }
    };
    api("GET", "/api/v1/runtime/notifications").then((runtime) => notify.appendChild(el("p", runtime.worker_running ? "muted small" : "error small", runtime.worker_running ? "后台通知线程运行中" : "后台通知线程未运行：自动结果通知当前不可用。测试仍可手动发送；不会启用或补发历史队列。"))).catch((err) => notify.appendChild(el("p", "error small", "无法确认通知线程状态：" + err.message)));
    notify.appendChild(el("p", "muted small", "新任务不填地址就不发送结果通知；已有地址留空保持，点击删除后保存才关闭。通知仅发送本任务结果，不自动订阅资产失联；地址不传入模型上下文。"));
    const schedWrap = el("div", "");
    schedWrap.appendChild(schedFields);
    const previewCron = el("button", "ghost small", "校验计划 / 预览未来5次（不执行）");
    const previewOut = el("pre", "pre-list");
    previewCron.onclick = async () => {
      try { const result = await api("POST", "/api/v1/custom-tasks/cron-preview", { cron: cronInput.value.trim(), timezone: tzInput.value.trim() || "Asia/Shanghai", count: 5 }); previewOut.textContent = (result.occurrences || []).map((item) => item.local_time).join("\n"); }
      catch (err) { previewOut.textContent = "校验失败：" + err.message; }
    };
    schedWrap.appendChild(previewCron); schedWrap.appendChild(previewOut);
    api("GET", "/api/v1/roles").then((roles) => {
      roles.forEach((r) => { const o = el("option", "", r.name + " (" + r.id + ")"); o.value = r.id; roleSelect.appendChild(o); });
      if (record) roleSelect.value = record.role_id;
    });
    const toggle = () => { schedWrap.hidden = kindSelect.value !== "scheduled"; };
    kindSelect.onchange = toggle; toggle();
    const feedback = el("p", "error small", "");
    const out = el("pre", "pre-list", "");
    const save = el("button", "", record ? "保存" : "创建");
    const del = el("button", "ghost danger", "删除");
    if (record) {
      del.onclick = async () => {
        try { await api("DELETE", "/api/v1/custom-tasks/" + record.id); $("modal").hidden = true; say("定制任务已删除（历史保留）"); renderConsole(); }
        catch (err) { feedback.textContent = err.message; }
      };
    }
    let createdSuccessfully = false;
    save.onclick = async () => {
      if (save.disabled) return;
      save.disabled = true;
      const body = { ...(record ? {id: record.id} : {}), name: nameInput.value.trim(), kind: kindSelect.value,
        role_id: roleSelect.value, prompt: prompt.value, execution_grants: selectedGrants(usersInput),
        mode: modeSelect.value, enabled: enabledInput.checked,
        notification: notifyMode.value === "inherit" ? null : notifyMode.value === "off" ? { enabled: false } : { enabled: true, channel: notifyChannel.value, console_url: notifyConsole.value.trim() || null } };
      if (record && !notificationChanged) delete body.notification;
      else if (clearNotification) body.notification = {enabled: false, url: ''};
      else if (notifyUrl.value.trim()) body.notification = {enabled: true, url: notifyUrl.value.trim(), channel: notifyChannel.value, console_url: notifyConsole.value.trim() || window.location.origin};
      if (kindSelect.value === "scheduled") { body.cron = cronInput.value.trim(); body.timezone = tzInput.value.trim() || "Asia/Shanghai"; }
      try {
        if (record) {
          await api("PUT", "/api/v1/console/custom-tasks/" + record.id, body);
          feedback.textContent = ""; out.textContent = "已保存（触发令牌不变）。";
          say("定制任务已保存"); renderConsole();
        } else {
          const created = await api("POST", "/api/v1/console/custom-tasks", body);
          createdSuccessfully = true;
          feedback.textContent = '';
          out.textContent = '定制任务已创建。接入信息见上方；修改配置请关闭后在任务列表中编辑。';
          triggerHost.textContent = '';
          if (created.id && created.trigger_path) triggerHost.appendChild(triggerTokenPanel({...body, id: created.id, trigger_path: created.trigger_path, trigger_token_active: true}, created.trigger_token));
          else out.textContent = '服务端已返回创建成功，但接入信息不完整。请到任务列表核实，不要重复创建。';
          say("定制任务已创建"); renderConsole();
        }
      } catch (err) { feedback.textContent = err.message; }
      finally { save.disabled = createdSuccessfully; }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    if (record) {
      const run = el("button", "ghost small", "手动触发 / 联调运行");
      run.onclick = () => customTaskRunDialog(record); actions.appendChild(run);
    }
    box.appendChild(fields); box.appendChild(prompt); box.appendChild(schedWrap); box.appendChild(notify);
    const triggerHost = el('div'); box.appendChild(triggerHost);
    if (record) triggerHost.appendChild(triggerTokenPanel(record));
    box.appendChild(el("p", "muted small", "服务端模式：readonly 不提供命令执行工具；confirm 所有命令先审批；direct 直接入队，不自动识别重大/写操作。执行账号仍受资产允许列表与系统权限约束。"));
    box.appendChild(actions); box.appendChild(feedback); box.appendChild(out);
    modalContent(record ? "编辑定制任务 " + record.id : "新建定制任务", box);
  }

  function triggerTokenPanel(record, issuedToken) {
    const box = el('section', 'notification-options'); box.appendChild(el('h3', '', '外部触发 Token / 接入文档'));
    const status = el('p', '', record.trigger_token_active ? 'Token 已配置；原明文不可读回' : 'Token 已撤销，外部触发关闭'); box.appendChild(status);
    const output = el('pre', 'pre-list'); const issue = el('button', 'small', '申请新 Token（替换旧 Token）'); const revoke = el('button', 'danger small', '撤销 Token');
    const copyStatus = el('p', 'small'); copyStatus.setAttribute('aria-live', 'polite');
    const copy = (label, value) => { const button = el('button', 'ghost small', label); button.type = 'button'; button.onclick = async () => { if (button.disabled) return; button.disabled = true; try { await navigator.clipboard.writeText(typeof value === 'function' ? value() : value); copyStatus.textContent = '已复制：' + label; } catch (err) { copyStatus.textContent = '无法自动复制，请手动选中下方内容复制（浏览器可能要求 HTTPS 或剪贴板权限）。'; } finally { button.disabled = false; } }; return button; };
    const copyToken = copy('复制本次 Token', () => output.textContent); copyToken.hidden = true;
    const clearToken = () => { output.textContent = ''; copyToken.hidden = true; copyStatus.textContent = ''; };
    const showToken = value => { output.textContent = value || ''; copyToken.hidden = !value; };
    showToken(issuedToken);
    let changing = false;
    const changeToken = async action => {
      if (changing) return;
      if (!confirm(action === 'POST' ? '申请后旧 Token 立即失效，新 Token 仅显示一次。继续？' : '立即拒绝旧 Token 新触发？已受理任务和内部定时调度不受影响。')) return;
      changing = true; issue.disabled = revoke.disabled = true; clearToken();
      try { const result = await api(action, '/api/v1/custom-tasks/' + encodeURIComponent(record.id) + '/trigger-token'); if (action === 'POST') { showToken(result.trigger_token); status.textContent = result.trigger_token ? '新 Token 已生效，旧 Token 已失效；明文仅本次显示' : '接口返回成功但未返回明文，请重新打开核实'; } else status.textContent = '已撤销；恢复须重新申请'; }
      catch (err) { status.textContent = 'Token 操作未确认，请重新打开核实状态；不回显敏感错误详情。'; }
      finally { changing = false; issue.disabled = revoke.disabled = false; }
    };
    issue.onclick = () => changeToken('POST'); revoke.onclick = () => changeToken('DELETE');
    box.appendChild(issue); box.appendChild(revoke); box.appendChild(el('p', 'muted small', 'Token 仅创建或申请时显示，关闭后不可读回。仅在需要时复制：内容会进入系统剪贴板。下面请求示例只使用占位符，不嵌入真实 Token。'));
    box.appendChild(output); box.appendChild(copyToken);
    const url = window.location.origin + (record.trigger_path || '/api/v1/triggers/' + encodeURIComponent(record.id) + '/invoke');
    box.appendChild(el('p', 'small', '完整触发 URL（按当前控制台入口生成；调用方需能访问）：')); box.appendChild(el('pre', 'pre-list', url)); box.appendChild(copy('复制触发 URL', url));
    const headers = 'Authorization: Bearer <TASK_TOKEN>\nContent-Type: application/json\nX-Event-ID: alert-unique-001';
    const payload = '{"message":"检查告警","labels":{"service":"demo"}}';
    const bash = 'curl --fail-with-body -X POST ' + JSON.stringify(url) + ' -H "Authorization: Bearer <TASK_TOKEN>" -H "Content-Type: application/json" -H "X-Event-ID: alert-unique-001" --data \'' + payload + '\'';
    const powershell = '$headers = @{ Authorization = \'Bearer <TASK_TOKEN>\'; \'X-Event-ID\' = \'alert-unique-001\' }\n$body = \'' + payload + '\'\n$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)\nInvoke-RestMethod -Method Post -Uri \'' + url.replace(/'/g, "''") + '\' -Headers $headers -ContentType \'application/json; charset=utf-8\' -Body $bytes';
    const help = el('details'); help.appendChild(el('summary', '', '标准调用文档（点击展开）'));
    [['请求头', headers], ['JSON 请求体', payload], ['Bash / POSIX curl', bash], ['PowerShell', powershell]].forEach(([label, text]) => { help.appendChild(el('h4', '', label)); help.appendChild(el('pre', 'pre-list', text)); help.appendChild(copy('复制' + label, text)); });
    help.appendChild(el('p', 'muted small', '示例执行会真实触发任务，不是连通性探测。替换 <TASK_TOKEN> 后再用；同一事件重试沿用 X-Event-ID，新事件换新 ID。PowerShell 使用 Invoke-RestMethod，避免 curl 别名和中文编码差异。'));
    box.appendChild(copyStatus);
    help.appendChild(el('p', 'muted small', '202 仅表示入队，不是执行成功。401 缺 Bearer；403 无效/撤销/跨任务 Token；409 停用/删除/重复事件正文冲突/旧权限需重选；413 超过256KiB；422 JSON或参数无效。X-Event-ID 在同任务内同正文去重，不传每次新建。鉴权先于去重，旧 Token 重试也被拒绝。一个URL只触发一个任务、不广播；Alertmanager 按 receiver 分发到对应URL，原生 alerts 数组不拆分、不自动按 groupKey 去重。管理 Token 不能替代外部 Token；内部定时仅凭管理身份加持久化 outbox ID触发。结果详情链接不是此触发URL。'));
    box.appendChild(help); return box;
  }

  function customTaskRunDialog(record) {
    const box = el("div");
    box.appendChild(el("p", "error small", "这是真实运行，不是 dry-run。使用服务端已保存的任务配置；未保存编辑不生效。confirm 仍逐条审批，direct 将直接入队。"));
    const payload = el("textarea"); payload.rows = 8; payload.style.width = "100%"; payload.value = "{}"; payload.setAttribute("aria-label", "触发事件 JSON");
    const eventId = (crypto.randomUUID ? crypto.randomUUID() : "console-" + Date.now());
    const feedback = el("p", "small"); const run = el("button", "small", "确认触发真实任务");
    run.onclick = async () => {
      run.disabled = true;
      try {
        const body = JSON.parse(payload.value);
        if (!body || Array.isArray(body) || typeof body !== "object") throw new Error("事件必须为 JSON 对象");
        const response = await fetch("/api/v1/triggers/" + encodeURIComponent(record.id) + "/invoke", { method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json", "X-Event-ID": eventId }, body: JSON.stringify(body) });
        const result = await response.json(); if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "HTTP " + response.status);
        feedback.textContent = (result.duplicate ? "已复用同一事件" : "已接收并创建轮次") + "；尚不代表执行成功。";
        const go = el("button", "ghost small", "到聊天查看 / 审批"); go.onclick = () => { $("modal").hidden = true; state.roleId = record.role_id; state.view = "chat"; renderNav(); renderView(); }; box.appendChild(go);
      } catch (err) { feedback.textContent = "触发失败：" + err.message; run.disabled = false; }
    };
    box.appendChild(payload); box.appendChild(run); box.appendChild(feedback); modalContent("运行已保存任务 · " + record.name, box);
  }

  async function loadDocuments(host) {
    const [documents, inventory] = await Promise.all([api("GET", "/api/v1/documents"), api("GET", "/api/v1/documents/system-asset-inventory")]);
    const data = [inventory, ...documents.filter((r) => r.id !== inventory.id)];
    const box = card("文档（核心文档强制注入，跨角色共享）");
    pageHelp(box, '文档说明', ['系统资产清单实时派生、不可编辑，作为核心文档对全部角色可见；当前没有角色级资产读 ACL。available_selected_users 仅表示本轮所选账号与资产允许账号的交集；未选账号不隐藏资产，只是没有可用执行账号。']);
    const add = el("button", "small", "新建 / 编辑文档");
    add.onclick = () => documentEditor(null);
    box.appendChild(add);
    box.appendChild(table(["名称", "类型", "角色", "修订", "正文字符", "ID", "操作"], data.map((r) => [r.name, r.core ? "核心文档" : "角色文档", usersToText(r.role_ids), r.revision, (r.content || "").length, r.id, r.read_only || r.derived ? "查看（只读）" : "编辑"]), (row) => {
      documentEditor(data.find((r) => r.id === row[5]));
    }));
    host.appendChild(box);
  }

  function inventoryDocumentView(record) {
    const box = el("div");
    let parsed = null;
    try { parsed = JSON.parse(record.content || ""); } catch (err) { /* preserve non-JSON content below */ }
    const assets = Array.isArray(record.assets) ? record.assets : Array.isArray(parsed) ? parsed : null;
    box.appendChild(el("p", "muted small", "系统实时派生 · 只读；名称与备注只有资产这一份真源，修改后核心清单自动更新。"));
    const editSource = el("button", "small", "到资产编辑名称 / 备注"); editSource.onclick = () => { $("modal").hidden = true; state.view = "assets"; state.tab = "assets"; renderNav(); renderView(); }; box.appendChild(editSource);
    if (assets) {
      const inventoryTable = table(["资产 ID", "名称", "备注", "连接方式", "账号（清单字段）", "执行范围"], assets.map((asset) => [
        asset.id, asset.name, asset.notes || "—", { agent: "Agent", ssh: "SSH", local: "本机 / 容器内" }[asset.connection_type] || asset.connection_type,
        usersToText(asset.available_selected_users) || "无", asset.connection_type === "local" ? "控制服务运行环境，非 Docker 宿主机" : asset.execution_scope || "—"
      ]));
      inventoryTable.style.whiteSpace = "pre-wrap";
      box.appendChild(inventoryTable);
    }
    const raw = el("details", "section-details"); raw.appendChild(el("summary", "", "查看文档内容（格式化）"));
    raw.appendChild(el("pre", "pre-list", parsed !== null ? JSON.stringify(parsed, null, 2) : record.content || ""));
    if (!assets) raw.open = true;
    box.appendChild(raw); modalContent("系统核心文档（实时派生，只读）", box);
  }

  function documentEditor(record) {
    if (record && record.id === "system-asset-inventory") {
      const generation = ++modalGeneration;
      return api("GET", "/api/v1/documents/system-asset-inventory")
        .then((latest) => { if (generation === modalGeneration && token) inventoryDocumentView(latest); })
        .catch(() => say("核心资产清单读取失败，请重新打开重试。", "error"));
    }
    if (record && (record.readonly || record.read_only || record.system)) { modal("系统核心文档（实时派生，只读）", record.content); return; }
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "系统自动生成 ID"; idInput.hidden = true;
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const rolesInput = el("input"); rolesInput.placeholder = "角色 id，逗号分隔（留空=仅核心）";
    const coreInput = el("input"); coreInput.type = "checkbox"; coreInput.checked = true;
    const coreWrap = el("label", "inline"); coreWrap.appendChild(coreInput); coreWrap.appendChild(el("span", "", "核心文档"));
    if (record) { idInput.value = record.id; idInput.disabled = true; nameInput.value = record.name; rolesInput.value = usersToText(record.role_ids); coreInput.checked = !!record.core; }
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(rolesInput); fields.appendChild(coreWrap);
    const content = el("textarea"); content.rows = 12; content.style.width = "100%"; content.value = record ? record.content : "";
    const feedback = el("p", "error small", "");
    const save = el("button", "", record ? "保存" : "创建");
    const del = el("button", "ghost danger", "删除");
    del.onclick = async () => {
      if (!record) return;
      try { await api("DELETE", "/api/v1/documents/" + record.id); $("modal").hidden = true; say("文档已删除（审计保留）"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    save.onclick = async () => {
      const body = { ...(record ? {id: record.id} : {}), name: nameInput.value.trim(), content: content.value, core: coreInput.checked, role_ids: textToUsers(rolesInput.value) };
      try { await api(record ? "PUT" : "POST", "/api/v1/documents" + (record ? "/" + record.id : ""), body); $("modal").hidden = true; say("文档已保存"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    box.appendChild(el("p", "muted small", "核心文档强制注入所有角色；非核心文档仅注入指定角色；保存有修订号。"));
    box.appendChild(fields); box.appendChild(content); box.appendChild(actions); box.appendChild(feedback);
    if (record) box.appendChild(resourceIdentity(record.id));
    modalContent(record ? "编辑文档 · " + record.name : "新建文档", box);
  }

  async function loadSkills(host) {
    const [data, roles] = await Promise.all([api("GET", "/api/v1/skills"), api("GET", "/api/v1/roles")]);
    const box = card("Skills 书签库");
    const add = el("button", "small", "＋ 新建 Skill"); add.onclick = () => skillEditor(null);
    const pull = el("button", "ghost small", "从官网同步"); pull.onclick = () => skillSyncDialog();
    const local = el('button', 'ghost small', '本地导入 .md / JSON'); local.onclick = skillImportDialog;
    const actions = el('div', 'row'); actions.appendChild(add); actions.appendChild(local); actions.appendChild(pull); box.appendChild(actions);
    pageHelp(box, 'Skills 说明', ['先编辑内容，再在卡片下绑定角色并启用。只有绑定角色会获得这份参考材料；Skill 不增加执行账号、权限或工具。']);
    host.appendChild(box);
    const grid = el("div", "skill-grid"); resourceSearch(box, grid); host.appendChild(grid);
    if (!data.length) grid.appendChild(el("div", "empty-state", "还没有 Skill，创建第一份运维操作参考。"));
    data.forEach((record) => {
      const item = el("article", "skill-card"); item.appendChild(el("div", "bookmark", "▰"));
      item.appendChild(el("h3", "", record.name));
      item.dataset.search = record.name + ' ' + record.id + ' Skill';
      const revisionLabel = el("p", "muted small", "Skill · 修订 " + record.revision); item.appendChild(revisionLabel); item.appendChild(resourceIdentity(record.id));
      item.appendChild(el("p", "skill-preview", (record.content || "暂无内容").slice(0, 160)));
      const edit = el("button", "ghost small", "编辑内容"); edit.onclick = () => skillEditor(record); item.appendChild(edit);
      const bindings = el("div", "skill-bindings"); bindings.appendChild(el("strong", "small", "绑定角色"));
      const choices = roles.map((role) => {
        const label = el("label", "inline"); const check = el("input"); check.type = "checkbox"; check.checked = (record.role_ids || []).includes(role.id);
        label.appendChild(check); label.appendChild(el("span", "", role.name)); bindings.appendChild(label); return { id: role.id, check };
      });
      if (!roles.length) bindings.appendChild(el("p", "muted small", "请先在聊天中新建角色"));
      const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = record.enabled;
      const label = el("label", "inline"); label.appendChild(enabled); label.appendChild(el("span", "", "启用此 Skill")); bindings.appendChild(label);
      const save = el("button", "small", "保存绑定与启用状态");
      const feedback = el("p", "small");
      save.onclick = async () => {
        if (save.disabled) return;
        save.disabled = edit.disabled = enabled.disabled = true; choices.forEach(choice => { choice.check.disabled = true; });
        try {
          const role_ids = choices.filter((choice) => choice.check.checked).map((choice) => choice.id);
          const updated = await api("PUT", "/api/v1/skills/" + record.id, { id: record.id, name: record.name, content: record.content, enabled: enabled.checked, role_ids });
          Object.assign(record, updated); revisionLabel.textContent = 'Skill · 修订 ' + record.revision;
          feedback.textContent = role_ids.length ? "已保存，仅绑定角色可用" : "已保存；尚未绑定角色，任何角色都不会注入此 Skill";
        } catch (err) { feedback.textContent = "保存失败：" + err.message; }
        finally { save.disabled = edit.disabled = enabled.disabled = false; choices.forEach(choice => { choice.check.disabled = false; }); }
      };
      bindings.appendChild(save); bindings.appendChild(feedback); item.appendChild(bindings); grid.appendChild(item);
    });
  }

  // Pull Skills from the website hub. The pull key is sent once to our own
  // /api/v1/skills/sync endpoint and never stored in the browser beyond this
  // dialog's inputs; it is not written to localStorage or the URL.
  function skillSyncDialog() {
    const box = el("div", "");
    const fields = el("div", "fields");
    const hubInput = el("input"); hubInput.placeholder = "官网 Skills 库地址，如 https://你的官网/api/v1/skills/repo";
    hubInput.value = "https://";
    const keyInput = el("input"); keyInput.type = "password"; keyInput.placeholder = "拉取 Key（aiops-sk-…）";
    keyInput.autocomplete = "off";
    const roleInput = el("input"); roleInput.placeholder = "绑定到本地角色 id（可空；逗号分隔，留空则用 Skill 自带的角色）";
    const dryWrap = el("label", "inline");
    const dryInput = el("input"); dryInput.type = "checkbox";
    dryWrap.appendChild(dryInput); dryWrap.appendChild(el("span", "", "仅预览（dry-run，不写入）"));
    fields.appendChild(hubInput); fields.appendChild(keyInput); fields.appendChild(roleInput); fields.appendChild(dryWrap);
    const feedback = el("p", "error small", "");
    const out = el("div", "");
    const run = el("button", "", "开始同步");
    run.onclick = async () => {
      feedback.textContent = "";
      out.textContent = "";
      const body = { source_url: hubInput.value.trim(), pull_key: keyInput.value.trim(), dry_run: dryInput.checked };
      const roles = roleInput.value.trim();
      if (roles) body.role_ids = roles.split(",").map((s) => s.trim()).filter(Boolean);
      if (!body.source_url || !body.pull_key) { feedback.textContent = "请填写官网地址与拉取 Key"; return; }
      run.disabled = true;
      try {
        const res = await api("POST", "/api/v1/skills/sync", body);
        const imported = (res.imported || []);
        const skipped = (res.skipped || []);
        out.appendChild(el("p", "small", (res.dry_run ? "预览" : "已导入") + " " + imported.length + " 个，跳过 " + skipped.length + " 个（bundle 修订 " + (res.bundle_revision ?? "-") + "）"));
        if (imported.length) {
          out.appendChild(table(["ID", "名称", "绑定角色"],
            imported.map((s) => [s.id, s.name, usersToText(s.role_ids)]), () => {}));
        }
        if (skipped.length) {
          out.appendChild(el("p", "muted small", "跳过原因：" + skipped.map((s) => (s.id || "?") + "（" + s.reason + "）").join("；")));
        }
        if (!res.dry_run && imported.length) renderConsole();
      } catch (err) {
        feedback.textContent = err.message;
      } finally {
        run.disabled = false;
      }
    };
    const actions = el("div", "row"); actions.appendChild(run);
    box.appendChild(fields);
    box.appendChild(el("p", "muted small", "拉取 Key 由官网在订阅后生成；只用于这次对官网的出站请求，不会存到浏览器。"));
    box.appendChild(actions); box.appendChild(feedback); box.appendChild(out);
    modalContent("从官网 Skills 库同步", box);
  }

  function skillImportDialog() {
    const box = el('div'); const file = el('input'); file.type = 'file'; file.accept = '.md,.json';
    const preview = el('div'); const feedback = el('p', 'small'); feedback.setAttribute('role', 'status');
    box.appendChild(file); box.appendChild(el('p', 'muted small', '仅导入正文，支持 Markdown / SKILL.md 名称描述与本平台 JSON bundle；不安装 ZIP/脚本，不执行代码、不自动授权或联网。预览后逐项确认绑定角色。')); box.appendChild(feedback); box.appendChild(preview);
    let revision = 0;
    file.onchange = async () => {
      const current = ++revision, generation = authGeneration; const picked = file.files[0];
      const isCurrent = () => current === revision && generation === authGeneration && box.isConnected;
      preview.textContent = ''; feedback.textContent = ''; feedback.className = 'small';
      if (!picked) return;
      if (picked.size > 800000) { feedback.className = 'error small'; feedback.textContent = '文件过大（最大800KB），未生成预览'; return; }
      feedback.textContent = '正在预览：' + picked.name;
      try {
        const content = await picked.text(); if (!isCurrent()) return;
        const result = await api('POST', '/api/v1/skills/import-preview', {filename: picked.name, content});
        if (!isCurrent()) return;
        feedback.textContent = '当前文件：' + picked.name + ' · ' + result.skills.length + ' 项 · 尚未保存';
        result.skills.forEach((skill) => { const item = el('section'); item.appendChild(el('h3', '', skill.name)); item.appendChild(el('p', '', skill.description)); item.appendChild(el('pre', 'pre-list', skill.content)); const confirm = el('button', 'small', '使用此正文并选择角色'); confirm.onclick = () => { if (isCurrent()) skillEditor(null, skill); }; item.appendChild(confirm); preview.appendChild(item); });
      } catch (err) { if (isCurrent()) { preview.textContent = ''; feedback.className = 'error small'; feedback.textContent = '预览失败：' + err.message; } }
    }; modalContent('本地 Skill 导入预览', box);
  }

  function skillEditor(record, draft) {
    const box = el("div", ""); if (record) box.appendChild(resourceIdentity(record.id));
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "系统自动生成 ID"; idInput.hidden = true;
    const nameInput = el('input'); nameInput.placeholder = '名称'; if (draft) nameInput.value = draft.name;
    const rolesInput = el('input'); rolesInput.placeholder = '角色 id，逗号分隔（导入不自动绑定）';
    const enabledInput = el("input"); enabledInput.type = "checkbox"; enabledInput.checked = true;
    const enabledWrap = el("label", "inline"); enabledWrap.appendChild(enabledInput); enabledWrap.appendChild(el("span", "", "启用"));
    if (record) { idInput.value = record.id; idInput.disabled = true; nameInput.value = record.name; rolesInput.value = usersToText(record.role_ids); enabledInput.checked = !!record.enabled; }
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(rolesInput); fields.appendChild(enabledWrap);
    const content = el('textarea'); content.rows = 12; content.style.width = '100%'; content.value = record ? record.content : draft ? draft.content : '# 用途\n检查服务运行状态并提供有证据的结论。\n\n## 适用条件\n用户明确指定目标资产与服务；先核实环境。\n\n## 权限\n仅用本轮授权账号。缺权限用专用账号授权工具，不自行扩权。\n\n## 步骤\n1. 核实资产与服务名。\n2. 读取状态和近期日志。\n3. 提出最小变更，遵守命令审批。\n\n## 验证\n记录命令、退出码及健康检查，不以已入队当成功。\n\n## 失败处理\n保留错误；结果未知时停止重试并请人工核实。';
    const feedback = el("p", "error small", "");
    const save = el("button", "", record ? "保存" : "创建");
    const del = el("button", "ghost danger", "删除");
    let submitting = false, committed = false;
    const lock = value => { save.disabled = del.disabled = value; };
    del.onclick = async () => {
      if (!record || submitting || committed) return;
      const generation = authGeneration; submitting = true; lock(true);
      try { await api("DELETE", "/api/v1/skills/" + record.id); committed = true; if (generation === authGeneration && box.isConnected) { closeModal(); say("Skill 已删除"); renderConsole(); } }
      catch (err) { if (generation === authGeneration) feedback.textContent = err.message; }
      finally { submitting = false; lock(committed); }
    };
    save.onclick = async () => {
      if (submitting || committed) return;
      const generation = authGeneration; submitting = true; lock(true); feedback.textContent = '';
      const body = { ...(record ? {id: record.id} : {}), name: nameInput.value.trim(), content: content.value, role_ids: textToUsers(rolesInput.value), enabled: enabledInput.checked };
      try { await api(record ? "PUT" : "POST", "/api/v1/skills" + (record ? "/" + record.id : ""), body); committed = true; if (generation === authGeneration && box.isConnected) { closeModal(); say("Skill 已保存"); renderConsole(); } }
      catch (err) { if (generation === authGeneration) feedback.textContent = err.message; }
      finally { submitting = false; lock(committed); }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    box.appendChild(fields); box.appendChild(content); box.appendChild(actions); box.appendChild(feedback);
    modalContent(record ? "编辑 Skill " + record.id : "新建 Skill", box);
  }

  function roleModelFields(host, model) {
    const box = card("模型接口 · 当前角色"); host.appendChild(box);
    const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = !!model.enabled;
    const enabledLabel = el("label", "inline"); enabledLabel.appendChild(enabled); enabledLabel.appendChild(el("span", "", "启用模型")); box.appendChild(enabledLabel);
    const mode = el("select"); mode.setAttribute("aria-label", "模型接口来源");
    [["inherit", "继承默认接口"], ["independent", "本角色独立接口"]].forEach(([value, label]) => { const option = el("option", "", label); option.value = value; mode.appendChild(option); }); mode.value = model.provider_mode || 'inherit'; mode.hidden = !!model.new_role; box.appendChild(mode);
    const field = (parent, label, key, type, value) => { const wrap = el("label", "field"); wrap.appendChild(el("span", "", label)); const input = el("input"); input.type = type; input.value = value; input.setAttribute("aria-label", key); wrap.appendChild(input); parent.appendChild(wrap); return input; };
    const independent = el("div", "provider-fields"); box.appendChild(independent);
    const base = field(independent, "Base URL · OpenAI 兼容接口地址", "base_url", "url", model.base_url || "");
    const secret = field(independent, model.api_key_configured ? "API Key · 已配置，留空保留原密钥" : "API Key · 尚未配置", "api_key", "password", ""); secret.autocomplete = "new-password"; secret.spellcheck = false;
    const name = field(box, model.new_role ? '模型名称' : '模型名称（旧继承配置可留空）', "model", "text", model.model || "");
    const vision = el('input'); vision.type = 'checkbox'; vision.checked = !!model.vision_enabled; vision.setAttribute('aria-label', 'vision_enabled');
    const visionLabel = el('label', 'inline'); visionLabel.appendChild(vision); visionLabel.appendChild(el('span', '', '启用图片理解（请确认此模型支持视觉；上传图片会发送给该模型服务商）')); box.appendChild(visionLabel);
    let clearKey = false;
    const clear = el("button", "ghost danger small", "清除已保存的 API Key"); clear.type = "button"; clear.setAttribute("aria-label", "clear_api_key"); clear.hidden = !model.api_key_configured; independent.appendChild(clear);
    const clearNotice = el("p", "muted small"); independent.appendChild(clearNotice);
    const toggle = () => {
      independent.hidden = mode.value !== "independent"; base.disabled = independent.hidden;
      if (independent.hidden) { secret.value = ""; clearKey = false; }
      secret.disabled = independent.hidden || clearKey; clear.disabled = independent.hidden; enabled.disabled = clearKey;
      clear.textContent = clearKey ? "取消清除 API Key" : "清除已保存的 API Key";
      clearNotice.textContent = clearKey ? "待保存：将清除独立密钥并关闭本角色模型。点击保存模型设置后生效；取消清除不会自动重新启用模型。" : "移除当前密钥引用并关闭模型，不撤销供应商侧 Key，历史秘密文件仍保留；留空不等于清除。";
    };
    clear.onclick = () => {
      if (!clearKey && !confirm("清除已保存的独立 API Key，并关闭本角色模型？点击保存后才生效。")) return;
      clearKey = !clearKey; secret.value = ""; if (clearKey) enabled.checked = false; toggle();
    }; mode.onchange = toggle; toggle();
    box.appendChild(el("p", "muted small", "独立接口仅支持公网 HTTPS（443端口）的 OpenAI 兼容接口，不支持内网或其他端口。必须填写模型名称；启用模型时必须有 API Key。密钥不回显、不写入浏览器存储或角色文档；留空保留原密钥，切回继承不会删除原独立配置。"));
    const advanced = el("details", "section-details"); advanced.appendChild(el("summary", "", "高级：运行上限")); box.appendChild(advanced);
    const steps = field(advanced, "每轮最多模型步骤（留空=不限）", "max_model_steps", "number", model.max_model_steps == null ? "" : model.max_model_steps); steps.min = "1"; steps.max = "1000";
    const output = field(advanced, "单次模型最大输出 tokens（仍有限）", "max_output_tokens", "number", model.max_output_tokens ?? 2048); output.min = "64"; output.max = "32768";
    const context = field(advanced, "单次最大上下文字符", "max_context_chars", "number", model.max_context_chars ?? 250000); context.min = "1000"; context.max = "2000000";
    advanced.appendChild(el("p", "muted small", "新角色默认不限制轮次步骤；不限不代表无限输出或免费运行。单次输出、上下文和服务商限制仍有效，可能持续消耗费用。存量角色显式限额保持原值。"));
    return { clearSecret: () => { secret.value = ""; clearKey = false; toggle(); }, body: () => {
      const result = { enabled: enabled.checked, provider_mode: mode.value, vision_enabled: vision.checked, model: name.value.trim(), max_model_steps: String(steps.value).trim() === "" ? null : Number(steps.value), max_output_tokens: Number(output.value), max_context_chars: Number(context.value) };
      if (mode.value === "independent") { result.base_url = base.value.trim(); if (clearKey) { result.clear_api_key = true; result.enabled = false; } else if (secret.value.trim()) result.api_key = secret.value.trim(); }
      return result;
    } };
  }

  async function roleSettings(role) {
    const box = el("div", "role-settings"); modalContent("角色与渠道 · " + role.name, box);
    const basics = card("角色名称"); const name = el("input"); name.value = role.name; name.setAttribute("aria-label", "角色名称");
    const saveName = el("button", "small", "保存名称"); saveName.onclick = async () => {
      try { await api("PUT", "/api/v1/roles/" + role.id, { name: name.value.trim() }); say("名称已保存"); await loadChat(); } catch (err) { say(err.message, "error"); }
    }; basics.appendChild(name); basics.appendChild(saveName); box.appendChild(basics);
    const [model, runtime] = await Promise.all([
      api("GET", "/api/v1/roles/" + role.id + "/model"),
      api("GET", "/api/v1/runtime/model")
    ]);
    box.appendChild(el("p", "muted small", "默认模型：" + (runtime.default_model || "未设置") + " · " + (runtime.worker_running ? "模型服务运行中" : "模型服务未运行")));
    box.appendChild(resourceIdentity(role.id));
    const provider = roleModelFields(box, model);
    const saveModel = el("button", "small", "保存模型设置"); saveModel.onclick = async () => {
      saveModel.disabled = true;
      try { await api("PUT", "/api/v1/roles/" + role.id + "/model", provider.body()); provider.clearSecret(); say("模型设置已保存"); await roleSettings(role); await loadChat(); }
      catch (err) { provider.clearSecret(); say("模型设置保存失败（若填写过密钥，请重新输入）。", "error"); }
      finally { saveModel.disabled = false; }
    }; box.appendChild(saveModel);
    const memory = el('button', 'ghost small', '角色记忆'); memory.onclick = async () => { const host = el('div'); modalContent('角色记忆 · ' + role.name, host); await loadMemory(host, role.id); }; box.appendChild(memory);
    const retire = el('button', 'danger small', '删除角色（归档保留历史）'); retire.onclick = async () => {
      if (!confirm('归档此角色？历史与审计保留；须先结束在途轮次、解绑渠道并停用关联定制任务。')) return;
      try { await api('DELETE', '/api/v1/roles/' + role.id); if (state.roleId === role.id) state.roleId = null; chatSnapshots.delete(role.id); closeModal(); await loadChat(); say('角色已归档，历史保留'); } catch (err) { say(err.message, 'error'); }
    }; box.appendChild(retire);
    await loadChannels(box, role.id);
  }

  async function loadChannels(host, selectedRoleId) {
    const allIdentities = await api("GET", "/api/v1/channels/identities");
    const identities = allIdentities.filter((identity) => !selectedRoleId || identity.role_id === selectedRoleId);
    const box = card("消息渠道 · 归属当前角色");
    box.appendChild(el("p", "muted small", "飞书 / 企业微信 / 微信身份共享当前角色的记忆与串行队列；不是资产的连接设置。"));
    const qr = el("section", "channel-placeholder"); qr.appendChild(el("strong", "", "扫码连接 · 待接入")); qr.appendChild(el("p", "muted small", "二维码接入位置已预留，当前没有可扫码登录的功能，也没有二维码。接入方案确认后再启用。")); box.appendChild(qr);
    box.appendChild(table(["渠道", "用户", "角色", "模式", "账号", "最近活动", "操作"],
      identities.map((i) => [i.channel, i.user_id, i.role_id, i.mode, usersToText(i.execution_users), fmt(i.last_seen), "解绑"]),
      (row) => {
        const record = identities.find((i) => i.channel === row[0] && i.user_id === row[1]);
        const detail = el("div"); detail.appendChild(el("pre", "pre-list", JSON.stringify(record, null, 2)));
        const unbind = el("button", "danger small", "解绑此身份");
        unbind.onclick = async () => {
          if (!confirm("确认解绑 " + record.channel + " / " + record.user_id + "？")) return;
          try { await api("DELETE", "/api/v1/channels/identities/" + encodeURIComponent(record.channel) + "/" + encodeURIComponent(record.user_id)); $("modal").hidden = true; say("身份已解绑，历史保留"); } catch (err) { say(err.message, "error"); }
        }; detail.appendChild(unbind); modalContent("渠道身份", detail);
      }));
    host.appendChild(box);

    const advanced = el("details", "section-details"); advanced.appendChild(el("summary", "", "高级：配对码与渠道边界")); box.appendChild(advanced);
    const note = card("配对不是扫码登录");
    note.appendChild(el("p", "muted small",
      "新身份必须先由管理员签发一次性配对码才能使用；未配对来信只记录不执行。" +
      "微信 / 企业微信的对外推送不在本仓库内置，出站消息以投递形式保存，由外部渠道桥接拉取；我们不会把“已存储”说成“已发送”。"));
    note.appendChild(el("p", "muted small", "配对码是管理员发给用户的一次性绑定凭证，用来把已接入渠道中的用户身份绑定到本角色及执行权限；它不负责登录微信、创建机器人或建立渠道连接。"));
    advanced.appendChild(note);

    const role = card("为当前角色签发配对码");
    const roleSelect = el("select");
    const modeSelect = el("select");
    ["confirm", "readonly", "direct"].forEach((m) => { const o = el("option", "", m); o.value = m; modeSelect.appendChild(o); });
    const userInput = el('select'); userInput.multiple = true; accountOptions(userInput).catch((err) => say(err.message, 'error'));
    const issue = el("button", "", "签发");
    const out = el("pre", "pre-list", "");
    api("GET", "/api/v1/roles").then((roles) => roles.filter((r) => !selectedRoleId || r.id === selectedRoleId).forEach((r) => {
      const o = el("option", "", r.name + " (" + r.id + ")"); o.value = r.id; roleSelect.appendChild(o);
    })).catch((err) => { out.textContent = err.message; });
    issue.onclick = async () => {
      try {
        const issued = await api("POST", "/api/v1/channels/pairings", {
          role_id: roleSelect.value, mode: modeSelect.value, execution_grants: selectedGrants(userInput),
        });
        out.textContent = "配对码（仅此一次显示）：" + issued.pairing_code;
      } catch (err) { out.textContent = "失败：" + err.message; }
    };
    const row = el("div", "fields");
    row.appendChild(roleSelect); row.appendChild(modeSelect); row.appendChild(userInput); row.appendChild(issue);
    role.appendChild(row); role.appendChild(el("p", "muted small", "readonly 不提供命令执行工具；confirm 每条命令审批；direct 直接入队。配对设置由服务端保存，能力仍受资产账号和操作系统权限限制。")); role.appendChild(out);
    advanced.appendChild(role);
  }

  let assetGrid = null;
  let assetQuery = "";
  async function loadAssets(host) {
    const toolbar = card("服务器资产");
    pageHelp(toolbar, '指标与权限说明', [
      "状态：绿表示 Agent 心跳健康或本机工作线程存活，红表示失联，灰表示未探测或不适用；颜色不代表命令执行成功。SSH 按需连接，没有 Agent 心跳，未探测不等于失联。占用灯以服务端状态为准。",
      "心跳口径：新 Agent 按 controller 与启动会话分开，取最后收到的最高发送序号向前最多10次；缺号仅表示尚未收到，迟到可更正。失联期间最新尾部尝试不可知，只展示最后可观测窗口与更新时间。已接收与 Agent 获得 ACK 分开：未获 ACK 不等于服务端未收到。均非网络丢包率；记录保留24小时。旧 Agent 降级为最近10个10秒时间窗心跳缺失率（非实际尝试丢失率、非网络丢包率），实际发送尝试次数不可用；10秒不是发送周期。", 
      "资产权限：当前没有角色级资产读 ACL，核心资产清单对全部角色可见。available_selected_users 仅为本轮所选账号与资产允许账号的交集，不隐藏资产；命令执行还受操作系统实际权限约束。",
      "本机范围：local 在线仅表示控制服务运行环境的 worker 存活；Docker 部署通常在控制服务容器内，不代表宿主机可控。管理宿主机须在宿主机部署 Agent 或配置 SSH。",
      "本机权限：执行可能使用控制服务账号，能接触控制面凭据及数据库，并非安全隔离账号。应最小化允许账号并使用 confirm 审批；审批不等于操作系统权限隔离。readonly 禁止命令执行，confirm 所有命令逐条审批，direct 直接执行，不自动识别重大或写操作。"
    ]);
    const add = el("button", "small", "＋ 注册资产"); add.onclick = () => assetCreator(); toolbar.appendChild(add);
    host.appendChild(toolbar);
    assetGrid = el("div", "asset-grid"); host.appendChild(assetGrid);
    const search = resourceSearch(toolbar, assetGrid); search.value = assetQuery; const applySearch = search.oninput; search.oninput = () => { assetQuery = search.value; applySearch(); };
    await refreshAssetCards();
  }

  async function refreshAssetCards() {
    const grid = assetGrid;
    if (!grid || !grid.isConnected || state.view !== "assets") return;
    const assets = await api("GET", "/api/v1/assets?include_retired=true&include_disabled_local=true");
    if (!grid.isConnected || !token) return;
    grid.textContent = "";
    if (!assets.length) grid.appendChild(el("div", "empty-state", "尚未注册资产。可注册 Agent / SSH 服务器，或在本机卡设置中启用纳管。"));
    assets.forEach((asset) => { const node = assetCard(asset, asset.health || { health_state: "unknown", error: "资产列表未返回健康状态" }); node.hidden = !node.dataset.search.toLowerCase().includes(assetQuery.trim().toLowerCase()); grid.appendChild(node); });
    if (!assets.some((a) => a.connection_type === "local")) {
      // A settings entry, not a fabricated asset or invented worker status.
      const local = el("article", "asset-card"); local.dataset.search = "本机 控制服务运行环境 local 设置";
      local.appendChild(el("h3", "", "本机 / 控制服务运行环境")); local.appendChild(el("p", "muted small", "尚未列出本机资产；打开设置读取真实纳管状态。Docker 下通常是容器，不代表宿主机。"));
      const settings = el("button", "small", "本机设置"); settings.onclick = async () => { const box = el("div"); modalContent("本机纳管设置", box); await loadLocalConnector(box); }; local.appendChild(settings); local.hidden = !local.dataset.search.toLowerCase().includes(assetQuery.trim().toLowerCase()); grid.appendChild(local);
    }
  }

  function assetCard(asset, status) {
    const box = el("article", "asset-card"); box.dataset.search = asset.name + ' ' + asset.id + ' ' + asset.connection_type;
    const retired = !!(asset.retired || asset.retired_at);
    const health = retired ? "retired" : status.health_state || (asset.connection_type === "agent" && status.presence ? (status.online ? "healthy" : "offline") : "unknown");
    const labels = { healthy: "在线", offline: "失联", unknown: "未探测", disabled: "已停用", retired: "已注销" };
    const top = el("div", "row"); top.appendChild(el("span", "server-icon", "▤"));
    top.appendChild(el("h3", "grow", asset.name));
    top.appendChild(el("span", "health-state " + health, "● " + (labels[health] || health))); box.appendChild(top);
    box.appendChild(el("p", "muted small", ({ agent: "Agent 心跳接入", ssh: "SSH 按需连接", local: "控制服务运行环境（本机/容器内）" }[asset.connection_type] || asset.connection_type)));
    box.appendChild(el("p", "small", "允许账号 · " + (usersToText(asset.allowed_users) || "无")));
    box.appendChild(statePulse(asset, status));
    const hb = status.heartbeat;
    if (asset.connection_type === "agent") {
      box.appendChild(el("p", "muted small", "最近心跳 · " + fmt(status.presence && status.presence.last_seen)));
      const sampled = hb && hb.applicable && hb.observed_slots > 0 && Number.isFinite(hb.successful_slots) && Number.isFinite(hb.missed_ratio) && Number.isFinite(hb.observation_window_seconds) && Number.isFinite(hb.window_size);
      if (hb && hb.metric === "heartbeat_attempt_receipt") {
        (hb.streams || [hb]).forEach((stream) => {
          box.appendChild(el("p", "small", stream.controller + " · 最后可观测" + stream.attempt_count + "次：已收到 " + stream.received_count + "/" + stream.attempt_count + "；尚未收到 " + stream.not_received_count + (stream.unknown_count ? "；证据过期 " + stream.unknown_count : "")));
          box.appendChild(el("p", "muted small", (stream.stale ? "失联 · " : "") + "窗口更新 " + fmt(stream.updated_at)));
        });
      } else box.appendChild(el("p", "small", sampled ? "旧版降级 · 最近" + hb.window_size + "个" + hb.observation_window_seconds + "秒窗：" + hb.successful_slots + "/" + hb.observed_slots + "有心跳；缺失" + (100 * hb.missed_ratio).toFixed(0) + "%（非发送丢包率）" : "旧版观察窗 · 待采样"));
    }
    if (asset.connection_type === "local") box.appendChild(el("p", "muted small", "Docker 下执行于控制服务容器，不代表宿主机可控。"));
    if (status.error) box.appendChild(el("p", "error small", "状态查询失败：" + status.error));
    const actions = el("div", "row");
    const edit = el("button", "ghost small", "编辑名称 / 备注"); edit.onclick = () => assetEditor(asset); actions.appendChild(edit);
    const connection = el("button", "small", asset.connection_type === "local" ? "本机设置" : "连接设置"); connection.onclick = () => assetConnectionSettings(asset).catch((err) => say(err.message, "error")); actions.appendChild(connection);
    const danger = el('div', 'asset-danger'); danger.setAttribute('aria-label', '资产危险操作');
    const retire = el("button", retired ? "ghost small" : "ghost danger small", retired ? "恢复" : "注销"); retire.onclick = () => assetLifecycleAction(asset, retired ? "unretire" : "retire"); (retired ? actions : danger).appendChild(retire);
    const purge = el("button", "ghost danger small", "删除"); purge.onclick = () => assetLifecycleAction(asset, "purge"); danger.appendChild(purge);
    box.appendChild(resourceIdentity(asset.id));
    [retire, purge].forEach(button => { button.dataset.lifecycleAsset = asset.id; button.disabled = assetLifecyclePending.has(asset.id); });
    box.appendChild(actions); box.appendChild(danger); return box;
  }

  function statePulse(asset, status) {
    const box = el("div", "state-pulse");
    if (asset.connection_type !== "agent") { box.appendChild(el("p", "muted small", asset.connection_type === "local" ? "本机 worker 状态（非 Agent 心跳）" : "SSH 按需连接 · 无持续心跳，脉波不适用")); return box; }
    const labels = {1: "1 · 在线空闲", 2: "2 · 本主服务执行", 3: "3 · 其他主服务执行", 4: "4 · 心跳断联（TTL 观测）"};
    box.appendChild(el("strong", "small", labels[status.execution_state] || "状态未知 · 旧协议或身份/占用证据不足"));
    const points = status.state_history || [];
    if (!points.length) { box.appendChild(el("p", "muted small", "待采样 · 不补造历史")); return box; }
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 320 68"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "真实心跳状态阶梯图；断联为30秒TTL观测推导");
    const first = points[0].at, span = Math.max(1, points[points.length - 1].at - first);
    const x = (p) => 6 + 308 * (p.at - first) / span, y = (p) => 10 + (4 - p.state) * 15;
    let previous = null;
    points.forEach((point) => {
      if (!point.state) { previous = null; return; }
      if (previous) { const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", "M" + x(previous) + "," + y(previous) + " H" + x(point) + " V" + y(point)); path.setAttribute("class", "pulse-line state-" + point.state); path.setAttribute("fill", "none"); svg.appendChild(path); }
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle"); dot.setAttribute("cx", x(point)); dot.setAttribute("cy", y(point)); dot.setAttribute("r", "2"); const title = document.createElementNS("http://www.w3.org/2000/svg", "title"); title.textContent = fmt(point.at) + " · " + labels[point.state] + " · " + point.source; dot.appendChild(title); svg.appendChild(dot); previous = point;
    });
    box.appendChild(svg); box.appendChild(el("p", "muted small", "1空闲 / 2本方 / 3他方 / 4断联 · 最近100条真实样本，断联按30秒TTL推导")); return box;
  }

  function sshCredentialFields(host, editing = false, initialKind = 'key') {
    const panel = el('div', 'ssh-credentials'); host.appendChild(panel);
    const label = (title, input, name) => { const wrap = el('label', 'field'); wrap.appendChild(el('span', '', title)); input.setAttribute('aria-label', name); wrap.appendChild(input); panel.appendChild(wrap); return wrap; };
    const auth = el('select'); [['key', '私钥'], ['password', '密码']].forEach(([value, text]) => { const o = el('option', '', text); o.value = value; auth.appendChild(o); }); auth.value = initialKind;
    label('SSH 认证方式', auth, 'ssh_auth_kind');
    const source = el('select');
    (editing ? [['keep', '保留已有凭据'], ['managed', '输入新密码 / 私钥'], ['file', '高级：服务端文件路径']] : [['managed', '输入密码 / 私钥'], ['file', '高级：服务端文件路径']]).forEach(([value, text]) => { const o = el('option', '', text); o.value = value; source.appendChild(o); });
    source.value = editing ? 'keep' : 'managed'; label('凭据来源', source, 'ssh_credential_source');
    const password = el('input'); password.type = 'password'; password.autocomplete = 'new-password'; password.maxLength = 65536;
    const passwordWrap = label('SSH 密码（仅写入，不回显）', password, 'ssh_password');
    const privateKey = el('textarea'); privateKey.rows = 5; privateKey.maxLength = 65536; privateKey.autocomplete = 'off'; privateKey.spellcheck = false; privateKey.setAttribute('autocapitalize', 'off');
    privateKey.placeholder = '粘贴完整私钥（保留 BEGIN / END 行与换行）';
    const keyWrap = label('SSH 私钥正文（仅写入，不回显）', privateKey, 'ssh_private_key');
    const ref = el('input'); ref.autocomplete = 'off'; ref.maxLength = 512; ref.placeholder = '控制服务可读取的凭据文件路径';
    const refWrap = label('高级：已有服务端文件路径', ref, 'ssh_secret_ref');
    const clear = () => { password.value = ''; privateKey.value = ''; ref.value = ''; };
    const sync = () => { passwordWrap.hidden = source.value !== 'managed' || auth.value !== 'password'; keyWrap.hidden = source.value !== 'managed' || auth.value !== 'key'; refWrap.hidden = source.value !== 'file'; };
    source.onchange = () => { clear(); sync(); }; auth.onchange = () => { clear(); sync(); }; sync();
    panel.appendChild(el('p', 'muted small', '输入的凭据仅提交到控制服务加密保存，不进入备注或模型上下文；不保存在浏览器本地存储，保存后清空。请通过 HTTPS 访问，公网 HTTP 会暴露传输内容。当前私钥连接仅支持无口令 Ed25519；私钥正文可见，请避开录屏和截图。'));
    return {
      body() {
        const data = {ssh_auth_kind: auth.value};
        if (source.value === 'keep') { if (auth.value !== initialKind) throw new Error('变更认证方式时，请同时输入对应的新凭据或文件路径'); return data; }
        if (source.value === 'file') { if (!ref.value.trim()) throw new Error('请填写服务端凭据文件路径'); data.ssh_secret_ref = ref.value.trim(); }
        else { const value = auth.value === 'password' ? password.value : privateKey.value; if (!value.length) throw new Error('请填写 SSH 密码或完整私钥'); data.ssh_secret = value; }
        return data;
      },
      clear,
      committed() { initialKind = auth.value; clear(); if (editing) source.value = 'keep'; sync(); }
    };
  }

  function sshSaveError(error) {
    return ({401: '登录已失效，请重新登录', 403: '没有保存连接的权限', 409: '配置冲突：资产可能已存在，或有在途 / 未知任务，请先核实', 422: '连接字段不完整或格式不正确，请核对地址、端口、账号、凭据与主机公钥', 503: '凭据存储暂不可用，配置未保存'})[error.status] || '保存未确认，请检查网络并重新打开连接设置核实；错误详情不回显凭据';
  }

  async function assetConnectionSettings(asset) {
    const box = el("div"); modalContent("连接设置 · " + asset.name, box); box.appendChild(resourceIdentity(asset.id));
    box.appendChild(el("p", "muted small", "这里配置机器连接。微信/飞书等消息渠道归属角色，请到聊天 → 角色旁“设置” → 渠道配对；资产不绑定聊天渠道。"));
    const out = el("p", "small");
    if (asset.connection_type === "local") { await loadLocalConnector(box); return; }
    if (asset.connection_type === "agent") {
      box.appendChild(el("p", "muted small", "Agent 使用资产 ID、控制服务 HTTPS 地址与配对 token。现有 token 不可读回；若遗失可轮换，旧 token 会立即失效，需同步更新 Agent 配置并重启。"));
      const rotate = el("button", "ghost small", "轮换 Agent 配对 token"); rotate.onclick = async () => {
        if (rotate.disabled || !confirm("旧 token 将立即失效，必须同步更新 Agent。确认轮换？")) return;
        rotate.disabled = true; out.textContent = '';
        try { const result = await api("POST", "/api/v1/assets/" + asset.id + "/rotate-token", {grace_seconds: 0}); out.textContent = "新 token（仅此一次）：" + result.agent_token; }
        catch (err) { out.textContent = '轮换未确认，请核实后再操作：' + err.message; }
        finally { rotate.disabled = false; }
      }; box.appendChild(rotate); box.appendChild(out); return;
    }
    const config = await api("GET", "/api/v1/assets/" + asset.id + "/connection"); const inputs = {};
    [['ssh_host','SSH 地址'],['ssh_port','端口'],['ssh_user','登录账号'],['ssh_host_key','固定主机公钥（留空保留）']].forEach(([key,label]) => { const wrap = el('label','field'); wrap.appendChild(el('span','',label)); const input = el('input'); input.value = String(config[key] || ''); input.setAttribute('aria-label', key); inputs[key] = input; wrap.appendChild(input); box.appendChild(wrap); });
    const credentials = sshCredentialFields(box, true, config.ssh_auth_kind || 'key');
    box.appendChild(el('p', 'muted small', '已有凭据、文件路径和固定主机公钥不回填；默认保留已有凭据。更换认证方式必须同时提供对应凭据。主机公钥留空保留，不会自动信任新主机。'));
    const save = el('button', 'small', '保存连接设置'); save.onclick = async () => {
      if (save.disabled) return;
      let body; try { body = credentials.body(); } catch (err) { out.textContent = err.message; return; }
      Object.entries(inputs).forEach(([key,input]) => { if (input.value.trim()) body[key] = key === 'ssh_port' ? Number(input.value) : input.value.trim(); });
      save.disabled = true;
      try { await api('PUT', '/api/v1/assets/' + asset.id + '/connection', body); credentials.committed(); inputs.ssh_host_key.value = ''; out.textContent = '连接设置已保存（未发起连接）'; }
      catch (err) { out.textContent = sshSaveError(err); }
      finally { credentials.clear(); delete body.ssh_secret; save.disabled = false; }
    };
    const check = el("button", "ghost small", "测试已保存 SSH 连接（不执行命令）"); check.onclick = async () => { try { const result = await api('POST','/api/v1/assets/' + asset.id + '/ssh/check'); out.textContent = result.reachable ? 'SSH 握手成功（未执行命令）' : '连接失败：' + (result.error_type || '未知'); } catch (err) { out.textContent = err.message; } }; box.appendChild(save); box.appendChild(check); box.appendChild(out);
  }

  function heartbeatDialog(asset) {
    const box = el("div");
    const exact = asset.health && asset.health.heartbeat && asset.health.heartbeat.metric === "heartbeat_attempt_receipt";
    box.appendChild(el("p", "muted small", exact ? "最近24小时发送序号证据；服务端接收与 Agent ACK 分列。每页100条。" : "旧版降级：最近24小时10秒观察窗记录，并非逐次发送日志。每页100条。"));
    const detail = el("details"); detail.appendChild(el("summary", "", "窗口与确认详情"));
    detail.appendChild(el("p", "muted small", "窗口截止于最高已收到序号，不代表失联期间最新十次；尾部未知。未获 ACK 可能是请求失败或响应丢失，不等于未送达。迟到包及后续 ACK 回报可更正记录；刷新日志可查看更正。"));
    const summary = el("div"); detail.appendChild(summary); box.appendChild(detail);
    const rows = el("div"); const more = el("button", "small", "加载心跳记录"); let after = 0;
    more.onclick = async () => {
      more.disabled = true;
      try {
        const result = await api("GET", "/api/v1/assets/" + encodeURIComponent(asset.id) + "/heartbeats?limit=100&after=" + after + (exact ? "&view=attempts" : ""));
        const ackLabel = (value) => ({ acknowledged: "Agent 已确认", unconfirmed: "Agent 未获 ACK", unknown: "ACK 未知" }[value] || "ACK 未知");
        if (exact && result.summary) {
          summary.textContent = "";
          result.summary.streams.forEach((s) => {
            summary.appendChild(el("p", "small", s.controller + " · 会话 " + s.session_id + " · #" + s.from_seq + "–#" + s.through_seq + " · 更新 " + fmt(s.updated_at) + (s.stale ? " · 失联，尾部未知" : "")));
            summary.appendChild(table(["发送序号", "接收证据", "ACK"], s.attempts.map((a) => [a.attempt_seq, ({received:"已收到",not_received_yet:"尚未收到",unknown_expired:"证据过期"}[a.receipt]), ackLabel(a.ack_status)])));
          });
        }
        rows.appendChild(exact ? table(["记录ID", "controller / 会话", "发送序号", "服务端接收时间", "ACK"], result.items.map((r) => [r.seq, r.controller + " / " + r.session_id, r.attempt_seq, r.received_at === null ? "尚无接收证据" : fmt(r.received_at), ackLabel(r.ack_status)])) : table(["观察窗记录ID", "服务端接收时间"], result.items.map((r) => [r.seq, fmt(r.received_at)])));
        after = result.next_after; more.textContent = result.items.length < 100 ? "已到末尾" : "继续加载"; more.disabled = result.items.length < 100;
      } catch (err) { rows.appendChild(el("p", "error", err.message)); more.disabled = false; }
    };
    box.appendChild(rows); box.appendChild(more); modalContent("心跳日志 · " + asset.name, box); more.onclick();
  }

  async function loadLocalConnector(host) {
    let config = { enabled: false, asset_id: null };
    try { config = await api("GET", "/api/v1/local-connector"); }
    catch (err) { host.appendChild(el("p", "error small", "运行环境纳管状态获取失败（不能判定已停用）：" + err.message)); return; }
    const box = card("控制服务运行环境（本机/容器内）");
    box.appendChild(el("p", "muted small", "Docker 下执行于控制服务容器，不是宿主机；权限边界见页级“指标与权限说明”。"));
    box.appendChild(table(["状态", "资产 ID", "允许账号", "默认执行账号"], [[
      config.enabled ? "已启用" : "已停用", config.asset_id || "-",
      config.allowed_users ? config.allowed_users.join(", ") : "-", config.run_as || "-",
    ]]));
    const actions = el("div", "fields");
    if (config.enabled) {
      const off = el("button", "small", "停用本机纳管");
      off.onclick = async () => {
        if (!confirm("停用本机纳管？原卡与历史保留，可重新启用。")) return;
        try { await api("POST", "/api/v1/local-connector/disable"); host.textContent = ""; await loadLocalConnector(host); await refreshAssetCards(); say("已停用本机纳管"); }
        catch (err) { alert("失败：" + err.message); }
      };
      actions.appendChild(off);
      actions.appendChild(el("p", "muted small", "停用后保留本机卡，可在卡内重新启用；历史任务与审计记录仍保留。"));
    } else {
      const on = el("button", "small", "启用本机纳管");
      on.onclick = async () => {
        if (!confirm("启用本机纳管？命令将在控制服务运行环境执行，Docker 下通常是容器；服务账号可能接触控制面凭据。")) return;
        try { await api("POST", "/api/v1/local-connector/enable"); host.textContent = ""; await loadLocalConnector(host); await refreshAssetCards(); say("已启用本机纳管"); }
        catch (err) { alert("失败：" + err.message); }
      };
      actions.appendChild(on);
    }
    box.appendChild(actions);
    host.appendChild(box);
  }

  function assetCreator() {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "系统自动生成 ID"; idInput.hidden = true;
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const usersInput = el('textarea'); usersInput.rows = 3; usersInput.placeholder = '每行一个用户名 | 用途备注（至少一个）\nops_read | 只读排查';
    const typeSelect = el("select");
    [["agent", "agent（安装 Agent）"], ["ssh", "ssh（直连）"]].forEach(([v, label]) => { const o = el("option", "", label); o.value = v; typeSelect.appendChild(o); });
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(usersInput); fields.appendChild(typeSelect);
    const sshFields = el("div", "fields");
    const hostInput = el("input"); hostInput.placeholder = "ssh_host";
    const portInput = el("input"); portInput.placeholder = "ssh_port（默认 22）";
    const userInput = el("input"); userInput.placeholder = "ssh_user";
    const keyInput = el("input"); keyInput.placeholder = "ssh_host_key（预置的主机公钥）";
    sshFields.appendChild(hostInput); sshFields.appendChild(portInput); sshFields.appendChild(userInput); sshFields.appendChild(keyInput);
    const credentials = sshCredentialFields(sshFields);
    const notes = el("textarea"); notes.rows = 4; notes.style.width = "100%"; notes.placeholder = "备注（会注入模型上下文）";
    const feedback = el("p", "error small", "");
    const out = el("pre", "pre-list", "");
    const sshWrap = el("div", "");
    sshWrap.appendChild(el("p", "muted small", "SSH 支持输入密码或私钥，也可在高级选项中引用服务端文件。必须填写经可信渠道核实的目标主机公钥，不匹配即拒连。登录账号不是本轮命令执行授权。"));
    sshWrap.appendChild(sshFields);
    const toggle = () => { sshWrap.hidden = typeSelect.value !== "ssh"; };
    typeSelect.onchange = () => { credentials.clear(); toggle(); }; toggle();
    const save = el("button", "", "注册");
    save.onclick = async () => {
      if (save.disabled) return;
      let accounts; try { accounts = parseAccounts(usersInput.value); } catch (err) { feedback.textContent = err.message; return; }
      const body = { name: nameInput.value.trim(), ...accounts, connection_type: typeSelect.value, notes: notes.value };
      if (typeSelect.value === 'ssh') {
        try { Object.assign(body, credentials.body()); } catch (err) { feedback.textContent = err.message; return; }
        Object.assign(body, {ssh_host: hostInput.value.trim() || null, ssh_port: portInput.value.trim() ? Number(portInput.value) : 22,
          ssh_user: userInput.value.trim() || null, ssh_host_key: keyInput.value.trim() || null});
      }
      save.disabled = true;
      try {
        const result = await api("POST", "/api/v1/console/assets", body);
        out.textContent = "资产 ID：" + result.asset_id + "\n" + (result.agent_token
          ? "Agent token（仅此一次显示，请立即保存）：" + result.agent_token
          : "资产已注册。");
        save.disabled = true;
        feedback.textContent = "";
        say("资产已注册");
      } catch (err) { feedback.textContent = body.connection_type === 'ssh' ? sshSaveError(err) : err.message; save.disabled = false; }
      finally { credentials.clear(); delete body.ssh_secret; }
    };
    box.appendChild(fields); box.appendChild(sshWrap); box.appendChild(notes); box.appendChild(save); box.appendChild(feedback); box.appendChild(out);
    modalContent("注册资产", box);
  }

  function assetEditor(record) {
    if (!record) return;
    const box = el("div", "");
    const fields = el("div", "fields");
    const nameInput = el("input"); nameInput.value = record.name; nameInput.placeholder = "名称";
    const usersInput = el('textarea'); usersInput.rows = 3; usersInput.value = record.allowed_users.map((u) => u + ' | ' + ((record.account_notes || {})[u] || '')).join('\n'); usersInput.placeholder = '每行一个用户名 | 用途备注';
    fields.appendChild(nameInput); fields.appendChild(usersInput);
    const notes = el("textarea"); notes.rows = 6; notes.style.width = "100%"; notes.value = record.notes || "";
    notes.placeholder = "备注（会注入模型上下文，作为资产说明）";
    const feedback = el("p", "error small", "");
    const save = el("button", "", "保存");
    save.onclick = async () => {
      if (save.disabled) return;
      let accounts; try { accounts = parseAccounts(usersInput.value); } catch (err) { feedback.textContent = err.message; return; }
      const body = { name: nameInput.value.trim(), ...accounts, notes: notes.value };
      const generation = authGeneration, modalVersion = modalGeneration;
      const current = () => generation === authGeneration && modalVersion === modalGeneration && !$("modal").hidden && box.isConnected;
      save.disabled = true;
      try {
        await api("PUT", "/api/v1/assets/" + record.id + "/notes", body);
        if (generation !== authGeneration) return;
        if (current()) { closeModal(); say("资产已更新"); }
        if (state.view === 'assets') await refreshAssetCards();
      } catch (err) { if (current()) feedback.textContent = err.message; }
      finally { save.disabled = false; }
    };
    box.appendChild(el("p", "muted small", "名称与备注保存到资产真源，核心资产清单自动更新。连接与凭据请使用卡内连接设置。"));
    box.appendChild(fields); box.appendChild(notes); box.appendChild(save); box.appendChild(feedback);
    modalContent("编辑资产 · " + record.name, box);
  }

  async function assetLifecycleAction(asset, action) {
    if (assetLifecyclePending.has(asset.id)) return;
    const generation = authGeneration;
    let body;
    if (action === "purge") {
      const typed = prompt("彻底删除不可恢复，会清除资产相关历史。存在执行中或未决任务时服务端会拒绝删除；请先核实，不要用删除绕过审批。\n请输入资产 ID 以确认：" + asset.id);
      if (typed !== asset.id) { if (typed !== null) say("确认 ID 不匹配，已取消。", "error"); return; }
      body = { confirm_asset_id: asset.id };
    } else {
      if (!confirm(action === "retire" ? "注销该资产？原卡仍保留并标记已注销，停止新任务接入；历史保留，可恢复。" : "恢复该资产？历史保留，接入凭据与本机开关仍按当前设置生效。")) return;
      if (action === "retire") body = { note: "from console" };
    }
    assetLifecyclePending.add(asset.id);
    const lock = value => document.querySelectorAll('button').forEach(button => { if (button.dataset.lifecycleAsset === asset.id) button.disabled = value; });
    lock(true);
    try {
      await api("POST", "/api/v1/assets/" + encodeURIComponent(asset.id) + "/" + action, body);
      if (generation !== authGeneration) return;
      await refreshAssetCards();
      if (state.view === 'assets') say(action === "purge" ? "资产已彻底删除" : action === "retire" ? "资产已注销，原卡与历史保留" : "资产已恢复");
    } catch (err) { if (generation === authGeneration && state.view === 'assets') say("操作未完成：" + err.message, "error"); }
    finally { if (generation === authGeneration) { assetLifecyclePending.delete(asset.id); lock(false); } }
  }

  async function assetMemoryEditor(asset) {
    const box = el('div'); modalContent('资产记忆 · ' + asset.name, box);
    const record = await api('GET', '/api/v1/assets/' + encodeURIComponent(asset.id) + '/memory');
    if (!box.isConnected || $('modal').hidden) return;
    box.appendChild(el('p', 'muted small', '每资产唯一记忆 · ' + asset.id + ' · 修订 ' + record.revision + '。模型按精确资产 ID 读取；当前由管理员维护，不自动归档对话，旧角色记忆未迁移。最多20,000字符。'));
    const content = el('textarea'); content.rows = 14; content.maxLength = 20000; content.value = record.content; content.setAttribute('aria-label', '资产记忆内容'); box.appendChild(content);
    const feedback = el('p', 'small'); box.appendChild(feedback);
    const save = el('button', '', '保存资产记忆'); box.appendChild(save);
    save.onclick = async () => { save.disabled = true; try {
      const updated = await api('PUT', '/api/v1/assets/' + encodeURIComponent(asset.id) + '/memory', { content: content.value, expected_revision: record.revision });
      record.revision = updated.revision; feedback.textContent = '已保存 · 修订 ' + updated.revision;
    } catch (err) { feedback.className = 'error'; feedback.textContent = err.status === 409 ? '记忆已在其他窗口修改，请重新打开后编辑，避免覆盖。' : err.message; } finally { save.disabled = false; } };
  }

  async function loadMemory(host, selectedRoleId = state.roleId) {
    const generation = authGeneration;
    const roles = await api("GET", "/api/v1/roles");
    if (generation !== authGeneration || !host.isConnected) return;
    if (!roles.length) { host.appendChild(el("div", "card muted", "尚无角色")); return; }
    const box = card("按天分层记忆");
    const select = el("select"); select.setAttribute('aria-label', '记忆所属角色');
    roles.forEach((role) => { const option = el("option", "", role.name + " (" + role.id + ")"); option.value = role.id; select.appendChild(option); });
    if (selectedRoleId) select.value = selectedRoleId;
    const button = el("button", "", "查看");
    const holder = el("div", ""); let revision = 0;
    button.onclick = async () => {
      const current = ++revision, roleId = select.value;
      const currentRequest = () => current === revision && generation === authGeneration && host.isConnected && select.value === roleId;
      holder.textContent = '正在加载所选角色记忆…'; button.disabled = true;
      try {
        if (!roles.some(role => role.id === roleId)) throw new Error('所选角色已不可用，请重新打开角色设置');
        const [policy, days] = await Promise.all([
          api("GET", "/api/v1/roles/" + encodeURIComponent(roleId) + "/memory/policy"),
          api("GET", "/api/v1/roles/" + encodeURIComponent(roleId) + "/memory")
        ]);
        if (!currentRequest()) return;
        holder.textContent = '';
        holder.appendChild(el('p', 'muted small', '当前记忆角色：' + roleId));
        holder.appendChild(el("p", "muted small", "策略：" + JSON.stringify(policy)));
        holder.appendChild(table(["日期", "来源", "轮次", "编辑时间", "全文/压缩/梗概字符"], days.map((d) => [d.day, d.source, d.turn_count, fmt(d.edited_at), (d.full_text || "").length + " / " + (d.compressed || "").length + " / " + (d.summary || "").length]), (row) => {
          if (!currentRequest()) return;
          const day = days.find((d) => d.day === row[0]);
          modal("记忆 · " + roleId + " · " + day.day, day);
        }));
      } catch (err) { if (currentRequest()) holder.textContent = '角色记忆加载失败：' + err.message; }
      finally { if (currentRequest()) button.disabled = false; }
    };
    select.onchange = () => button.onclick();
    box.appendChild(select); box.appendChild(button); box.appendChild(holder);
    host.appendChild(box);
    await button.onclick();
  }

  async function loadHeartbeatRecords(host) {
    const section = el("details", "section-details"); section.appendChild(el("summary", "", "心跳记录")); host.appendChild(section);
    section.appendChild(el("p", "muted small", "连续记录 · 最近24小时，最新在前。1=在线空闲，2=本主服务执行，3=其他主服务执行，4=服务端30秒TTL断联观测（推导，不是收到信号）。旧协议或证据不足显示未知；不补造历史。"));
    const assets = await api("GET", "/api/v1/assets?include_retired=true&include_disabled_local=true");
    const select = el("select"); select.setAttribute("aria-label", "心跳资产筛选"); const all = el("option", "", "全部 Agent 资产"); all.value = ""; select.appendChild(all);
    assets.filter((a) => a.connection_type === "agent").forEach((a) => { const option = el("option", "", a.name + " · " + a.id); option.value = a.id; select.appendChild(option); });
    const refresh = el("button", "small", "刷新心跳记录"); section.appendChild(select); section.appendChild(refresh);
    const rows = el("div", "heartbeat-records"); section.appendChild(rows);
    const feedback = el("p", "small"); section.appendChild(feedback);
    const more = el("button", "ghost small", "加载更早心跳"); section.appendChild(more);
    const advanced = el("details", "section-details"); advanced.appendChild(el("summary", "", "高级：接收与 ACK 证据"));
    advanced.appendChild(el("p", "muted small", "先选择单个资产再查看。服务端收到心跳与 Agent 获得 ACK 是两件事；未获 ACK 不等于未送达。"));
    const ack = el("button", "ghost small", "查看所选资产 ACK 详情"); ack.disabled = true; ack.onclick = () => { const asset = assets.find((a) => a.id === select.value); if (asset) heartbeatDialog(asset); }; advanced.appendChild(ack); section.appendChild(advanced);
    let records = [], cursor = null, revision = 0, loaded = false, scope = "";
    const load = async (reset) => {
      if (reset) { records = []; cursor = null; scope = select.value; revision++; }
      const current = revision; more.disabled = true; feedback.textContent = "正在加载心跳记录…";
      const params = new URLSearchParams({ limit: "100" }); if (scope) params.set("asset_id", scope); if (cursor) params.set("before", cursor);
      try {
        const result = await api("GET", "/api/v1/logs/heartbeats?" + params.toString()); if (current !== revision) return;
        const ids = new Set(records.map((r) => r.id)); records.push(...result.items.filter((r) => !ids.has(r.id))); cursor = result.next_before;
        rows.textContent = "";
        rows.appendChild(table(scope ? ["时间", "信号"] : ["时间", "资产", "信号"], records.map((r) => {
          const signal = [1,2,3,4].includes(r.state_code) ? String(r.state_code) + (r.state_code === 4 ? " · TTL观测推导" : "") : "未知";
          return scope ? [fmt(r.timestamp), signal] : [fmt(r.timestamp), r.asset_name || r.asset_id, signal];
        })));
        feedback.textContent = records.length ? "已加载 " + records.length + " 条连续记录" : "暂无心跳记录（不等于资产失联）"; more.textContent = cursor ? "加载更早心跳" : "心跳已到末尾"; more.disabled = !cursor; loaded = true;
      } catch (err) { if (current === revision) { feedback.textContent = "心跳记录加载失败：" + err.message; more.disabled = false; } }
    };
    select.onchange = () => { ack.disabled = !select.value; return load(true); }; refresh.onclick = () => load(true); more.onclick = () => load(false);
    section.ontoggle = () => { if (section.open && !loaded) return load(true); };
  }

  async function loadLogs(host) {
    const controls = card('分组运行日志'); host.appendChild(controls);
    pageHelp(controls, '分组日志说明', ['默认按轮次聚合审计证据；没有轮次的任务单独成组，其他平台事件按北京时间日期聚合。每页30组，组内每页100条，均由服务端分页。列表与组内证据固定在本次查询快照，刷新后查看新事件；轮次状态与授权详情另行读取实时数据。关键词仅筛选已加载组的摘要，不是全文检索。告警、输出归档及其他来源仍可在下方逐条日志入口查看。']);
    const role = el('input'); role.placeholder = '角色 ID（服务端精确筛选）'; role.setAttribute('aria-label', '分组日志角色');
    const day = el('input'); day.type = 'date'; day.setAttribute('aria-label', '分组日志日期');
    const search = el('input'); search.placeholder = '在已加载组摘要中搜索';
    const refresh = el('button', 'small', '应用分组筛选 / 刷新');
    const fields = el('div', 'fields'); [role, day, search, refresh].forEach(n => fields.appendChild(n)); controls.appendChild(fields);
    const feedback = el('p', 'small'); feedback.setAttribute('aria-live', 'polite'); controls.appendChild(feedback);
    const list = el('div', 'log-groups'); host.appendChild(list);
    const more = el('button', 'ghost small', '加载更早分组'); host.appendChild(more);
    const empty = el('p', 'muted small', '已加载分组中没有匹配记录'); host.appendChild(empty); empty.hidden = true;
    let revision = 0, cursor = null, busy = false, scope = {}, entries = new Map();
    const matches = () => { const q = search.value.trim().toLowerCase(); let shown = 0; entries.forEach(({group,node}) => { node.hidden = !!q && ![group.summary, group.role_id, group.turn_id, group.task_id, group.day].filter(Boolean).join(' ').toLowerCase().includes(q); if (!node.hidden) shown++; }); empty.hidden = shown > 0 || busy; };
    const groupCard = (group, generation) => {
      const node = el('details', 'log-group section-details');
      node.appendChild(el('summary', '', (group.day || '') + ' · ' + (group.role_id || '平台') + ' · ' + (group.summary || '审计事件') + ' · ' + group.event_count + '条'));
      const body = el('div'); node.appendChild(body);
      body.appendChild(el('p', 'muted small', '证据范围：' + fmt(group.started_at) + ' ～ ' + fmt(group.updated_at) + (group.turn_id ? ' · 轮次 ' + group.turn_id : group.task_id ? ' · 任务 ' + group.task_id : '')));
      if (group.turn_id) { const turn = el('button', 'ghost small', '查看轮次 / 授权（实时）'); turn.onclick = async () => { turn.disabled = true; try { const data = await api('GET', '/api/v1/turns/' + encodeURIComponent(group.turn_id)); if (generation === revision) modal('轮次与授权（实时数据）', data); } catch (err) { if (generation === revision) say('轮次详情加载失败：' + err.message, 'error'); } finally { turn.disabled = false; } }; body.appendChild(turn); }
      if (group.role_id) { const chat = el('button', 'ghost small', '进入角色聊天'); chat.onclick = () => { state.roleId = group.role_id; state.view = 'chat'; renderNav(); renderView(); }; body.appendChild(chat); }
      const rows = el('div', 'group-events'); const status = el('p', 'small'); status.setAttribute('aria-live', 'polite');
      const next = el('button', 'ghost small', '加载组内记录'); body.appendChild(rows); body.appendChild(status); body.appendChild(next);
      let after = 0, loading = false, started = false, finished = false; const seen = new Set();
      const load = async () => {
        if (loading || finished || generation !== revision) return; loading = true; next.disabled = true; status.textContent = '正在加载原始审计证据…';
        try {
          const result = await api('GET', '/api/v1/logs/groups/' + encodeURIComponent(group.id) + '/events?after=' + after + '&limit=100');
          if (generation !== revision) return;
          result.items.forEach(event => { if (seen.has(event.seq)) return; seen.add(event.seq);
            const record = el('details', 'log-entry'); record.appendChild(el('summary', '', '#' + event.seq + ' · ' + fmt(event.created_at) + ' · ' + event.event));
            record.appendChild(el('pre', 'pre-list', JSON.stringify(event, null, 2)));
            if (event.task_id) { const task = el('button', 'ghost small', '查看任务 / 输出归档'); task.onclick = async () => { task.disabled = true; try { const data = await api('GET', '/api/v1/tasks/' + encodeURIComponent(event.task_id)); if (generation === revision) taskLogDialog(data); } catch (err) { if (generation === revision) say('任务详情加载失败：' + err.message, 'error'); } finally { task.disabled = false; } }; record.appendChild(task); }
            rows.appendChild(record);
          });
          started = true; finished = result.next_after == null; if (!finished) after = result.next_after;
          status.textContent = '已加载 ' + seen.size + ' / ' + group.event_count + ' 条快照证据'; next.textContent = finished ? '本组记录已到末尾' : '继续加载组内记录'; next.disabled = finished;
        } catch (err) { if (generation === revision) { status.textContent = '组内记录加载失败：' + err.message; next.textContent = '重试组内记录'; next.disabled = false; } }
        finally { loading = false; }
      };
      node.ontoggle = () => { if (node.open && !started) return load(); }; next.onclick = load; return node;
    };
    const load = async (reset) => {
      if (!reset && busy) return;
      if (reset) { revision++; cursor = null; scope = {role_id: role.value.trim(), day: day.value}; entries = new Map(); list.textContent = ''; }
      const generation = revision; busy = true; more.disabled = true; empty.hidden = true; feedback.textContent = '正在加载日志分组…';
      const params = new URLSearchParams({limit:'30'}); if (scope.role_id) params.set('role_id',scope.role_id); if (scope.day) params.set('day',scope.day); if (cursor) params.set('before',cursor);
      try {
        const result = await api('GET', '/api/v1/logs/groups?' + params.toString()); if (generation !== revision) return;
        result.items.forEach(group => { if (entries.has(group.id)) return; const node = groupCard(group,generation); entries.set(group.id,{group,node}); list.appendChild(node); }); cursor = result.next_before;
        feedback.textContent = '已加载 ' + entries.size + ' 组 · 北京时间 · 快照序号 ' + result.snapshot_seq;
        more.textContent = cursor ? '加载更早分组' : '分组已到末尾'; more.disabled = !cursor;
      } catch (err) { if (generation === revision) { feedback.textContent = '分组加载失败：' + err.message; more.textContent = '重试加载分组'; more.disabled = false; } }
      finally { if (generation === revision) { busy = false; matches(); } }
    };
    refresh.onclick = () => load(true); more.onclick = () => load(false); search.oninput = matches;
    const raw = el('details', 'section-details'); raw.appendChild(el('summary', '', '逐条日志 / 原始来源与输出归档')); const rawBody = el('div'); raw.appendChild(rawBody); host.appendChild(raw);
    let rawLoading = false, rawLoaded = false;
    raw.ontoggle = async () => { if (!raw.open || rawLoading || rawLoaded) return; rawLoading = true; rawBody.textContent = ''; try { await loadRawLogs(rawBody, false); rawLoaded = true; } catch (err) { rawBody.appendChild(el('p','error small','逐条日志加载失败：' + err.message)); } finally { rawLoading = false; } };
    await Promise.all([load(true), loadHeartbeatRecords(host)]);
  }

  async function loadRawLogs(host, includeHeartbeat = true) {
    const controls = card("统一运行日志");
    pageHelp(controls, '日志说明', ['服务端统一日志按来源事件时间倒序，每页100条，可继续加载更早记录。关键词仅筛选已加载记录。命令输出为归档元数据/预览，不是 AI 聊天输出；审批与未决核实请进入角色聊天。']);
    const labels = { all: "全部业务日志（不含心跳）", task: "任务", alarm: "告警", trigger: "触发", audit: "审计", output: "命令输出" };
    const filter = el("select"); filter.setAttribute("aria-label", "日志来源"); Object.entries(labels).forEach(([value, label]) => { const option = el("option", "", label); option.value = value; filter.appendChild(option); }); filter.value = "all";
    const search = el("input"); search.placeholder = "在已加载日志中搜索关键词";
    const role = el("input"); role.placeholder = "角色 ID（服务端精确筛选）";
    const asset = el("input"); asset.placeholder = "资产 ID（服务端精确筛选）";
    const refresh = el("button", "small", "应用筛选 / 刷新");

    const fields = el("div", "fields"); [filter, role, asset, search, refresh].forEach((node) => fields.appendChild(node)); controls.appendChild(fields); host.appendChild(controls);
    const feedback = el("p", "small"); feedback.setAttribute("aria-live", "polite"); controls.appendChild(feedback);
    const heartbeatLoading = includeHeartbeat ? loadHeartbeatRecords(host) : Promise.resolve();
    const timeline = el("div", "log-timeline"); host.appendChild(timeline);
    const more = el("button", "ghost small", "加载更早日志"); host.appendChild(more);
    let records = [], before = null, revision = 0;
    let applied = { kind: "all", role_id: "", asset_id: "" };
    const render = () => {
      timeline.textContent = ""; const query = search.value.trim().toLowerCase();
      const matching = records.filter((entry) => entry.kind !== "heartbeat" && (!query || JSON.stringify(entry).toLowerCase().includes(query)));
      matching.forEach((entry) => {
        const row = el("article", "log-entry"); const top = el("div", "row"); top.appendChild(el("span", "source-tag source-" + entry.kind, labels[entry.kind] || entry.kind)); top.appendChild(el("span", "muted small", fmt(entry.timestamp)));
        top.appendChild(el("span", pillClass(entry.event), entry.event || "已记录")); row.appendChild(top);
        const title = entry.summary || entry.entity_id;
        if (title && title.trim() !== (entry.event || "").trim()) row.appendChild(el("p", "log-title", title));
        row.appendChild(el("p", "muted small", [entry.role_id, entry.asset_id, entry.id].filter(Boolean).join(" · ")));
        const detail = el("button", "ghost small", entry.kind === "output" ? "查看输出 / 归档" : "查看记录");
        detail.onclick = async () => {
          if (entry.kind !== "task" && entry.kind !== "output") { modal(labels[entry.kind] || entry.kind, entry); return; }
          detail.disabled = true;
          try { taskLogDialog(await api("GET", "/api/v1/tasks/" + encodeURIComponent(entry.entity_id))); }
          catch (err) { modal("任务详情加载失败", { error: err.message, log: entry }); }
          finally { detail.disabled = false; }
        }; row.appendChild(detail);
        if (entry.role_id) { const go = el("button", "ghost small", "进入角色聊天"); go.onclick = () => { state.roleId = entry.role_id; state.view = "chat"; renderNav(); renderView(); }; row.appendChild(go); }
        timeline.appendChild(row);
      });
      if (!matching.length) timeline.appendChild(el("div", "empty-state", "已加载记录中没有匹配日志"));
    };
    const loadPage = async (reset) => {
      if (reset) { revision++; before = null; records = []; applied = { kind: filter.value, role_id: role.value.trim(), asset_id: asset.value.trim() }; }
      const current = revision;
      const params = new URLSearchParams({ kind: applied.kind, limit: "100" });
      if (applied.role_id) params.set("role_id", applied.role_id);
      if (applied.asset_id) params.set("asset_id", applied.asset_id);
      if (before) params.set("before", before);
      more.disabled = true; feedback.textContent = "正在加载日志…";
      if (reset) timeline.textContent = "正在加载日志…";
      try {
        const result = await api("GET", "/api/v1/logs?" + params.toString());
        if (current !== revision) return;
        const seen = new Set(records.map((entry) => entry.id));
        records.push(...result.items.filter((entry) => !seen.has(entry.id))); before = result.next_before;
        render(); feedback.textContent = "已加载 " + records.length + " 条日志";
        more.textContent = before ? "加载更早日志" : "已到末尾"; more.disabled = !before;
      } catch (err) { if (current === revision) { feedback.textContent = "日志加载失败：" + err.message; if (reset) timeline.textContent = "未能加载日志，请重试"; more.textContent = "重试加载"; more.disabled = false; } }
    };
    refresh.onclick = () => loadPage(true); filter.onchange = () => loadPage(true); more.onclick = () => loadPage(false); search.oninput = render;
    await Promise.all([loadPage(true), heartbeatLoading]);
    const deliveries = el("details", "section-details"); deliveries.appendChild(el("summary", "", "兼容投递记录（各来源最近100条，非统一日志分页）")); host.appendChild(deliveries);
    let deliveriesLoaded = false;
    deliveries.ontoggle = async () => {
      if (!deliveries.open || deliveriesLoaded) return; deliveriesLoaded = true;
      for (const [label, path] of [["通知投递", "/api/v1/alert-notify/deliveries?limit=100"], ["定时投递", "/api/v1/schedule-deliveries?limit=100"]]) {
        try { const data = await api("GET", path); const box = card(label); box.appendChild(table(["ID", "状态", "详情"], data.map((r) => [r.id, r.state || r.status || "-", "查看"]), (row) => modal(label, data.find((r) => r.id === row[0])))); deliveries.appendChild(box); }
        catch (err) { deliveries.appendChild(el("p", "error small", label + "加载失败：" + err.message)); deliveriesLoaded = false; }
      }
    };
    const output = el("details", "section-details"); output.appendChild(el("summary", "", "输出归档容量与保留策略（原“输出”页）")); host.appendChild(output);
    try { await loadOutput(output); } catch (err) { output.appendChild(el("p", "error", err.message)); }
    const turns = el("details", "section-details"); turns.appendChild(el("summary", "", "按角色查看轮次 / 模型调用记录")); host.appendChild(turns); await loadTurns(turns);
    const alarms = el("details", "section-details"); alarms.appendChild(el("summary", "", "告警来源统计与详情")); host.appendChild(alarms); await loadAlarms(alarms);
  }

  function taskLogDialog(task) {
    const box = el("div"); box.appendChild(el("pre", "pre-list", JSON.stringify(task, null, 2)));
    ["stdout", "stderr"].forEach((stream) => {
      const output = el("pre", "step-out"); let offset = 0; const decoder = new TextDecoder();
      const button = el("button", "ghost small", "读取 " + stream + " 归档（分块）");
      button.onclick = async () => {
        button.disabled = true;
        try {
          const response = await fetch("/api/v1/tasks/" + task.id + "/output/" + stream + "?offset=" + offset, { headers: { Authorization: "Bearer " + token } });
          if (!response.ok) throw new Error("HTTP " + response.status);
          const next = Number(response.headers.get("X-Next-Offset")); const size = Number(response.headers.get("X-Output-Size"));
          const bytes = await response.arrayBuffer(); output.textContent += decoder.decode(bytes, { stream: next < size }); offset = next;
          button.textContent = next < size ? "继续读取 " + stream + "（" + next + "/" + size + " 字节）" : stream + " 当前归档读取完成（" + size + " 字节）";
          button.disabled = next >= size;
        } catch (err) { output.textContent += "\n归档读取失败：" + err.message; button.disabled = false; }
      }; box.appendChild(button); box.appendChild(output);
    });
    modalContent("任务及输出 · " + task.id, box);
  }

  async function loadOutput(host) {
    const data = await api("GET", "/api/v1/output/usage");
    const grid = el("div", "grid");
    grid.appendChild(kv("原始字节总量", data.total_bytes));
    grid.appendChild(kv("分块数", data.chunks));
    grid.appendChild(kv("已冻结归档", data.archives));
    grid.appendChild(kv("最旧归档", fmt(data.oldest_archive_at)));
    host.appendChild(card("输出归档用量")).appendChild(grid);
    const policy = card("生效策略（环境变量配置）");
    policy.appendChild(el("pre", "pre-list", JSON.stringify(data.policy, null, 2)));
    host.appendChild(policy);
  }

  /* ---------- boot ---------- */
  // A pushed alert conclusion deep-links here as ``#turn=<id>``. Tapping it on a
  // phone should land the operator on the chat view with that turn highlighted,
  // so the notification is a doorway, not a dead end.
  function focusTurnFromHash() {
    const hash = (location.hash || "").replace(/^#/, "");
    if (!hash) return null;
    const params = new URLSearchParams(hash);
    const turnId = params.get("turn");
    return turnId ? turnId : null;
  }
  function highlightTurn(turnId) {
    const cards = document.querySelectorAll("#chat-log .msg");
    let found = null;
    cards.forEach((card) => {
      const meta = card.querySelector(".meta");
      if (card.dataset.turnId === turnId) { found = card; }
    });
    if (found) {
      found.classList.add("turn-focus");
      found.scrollIntoView({ block: "center" });
    }
    return found;
  }
  function boot() {
    const remembered = sessionStorage.getItem("aiops.view");
    const deepTurn = focusTurnFromHash();
    if (deepTurn) {
      // A deep link is an explicit intent: show chat, then highlight the turn.
      state.view = "chat";
    } else if (remembered === 'memory') {
      state.view = 'chat'; state.tab = 'chat';
    } else if (VIEWS.some(([id]) => id === remembered)) {
      state.view = remembered; state.tab = remembered;
    }
    renderNav();
    renderView();
    Promise.all(['/api/v1/skills', '/api/v1/documents', '/api/v1/custom-tasks'].map((path) => api('GET', path))).catch((err) => { if (token) say('列表预取失败：' + err.message, 'error'); });
    if (deepTurn) {
      api("GET", "/api/v1/turns/" + encodeURIComponent(deepTurn)).then(async (turn) => {
        if (!token) return;
        state.roleId = turn.role_id; await loadChat();
        if (!highlightTurn(deepTurn)) { $("chat-log").appendChild(turnCard(turn)); highlightTurn(deepTurn); }
      }).catch((err) => say("轮次链接加载失败：" + err.message, "error"));
    }
  }
  window.addEventListener("beforeunload", () => { token = ""; });
})();
