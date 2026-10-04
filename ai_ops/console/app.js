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
  let state = { view: "chat", roleId: null, tab: "overview" };

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
  const textToUsers = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);

  async function api(method, path, body) {
    const options = { method, headers: {} };
    if (token) options.headers["Authorization"] = "Bearer " + token;
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const text = await response.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
    if (!response.ok) {
      const message = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : ("HTTP " + response.status);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function say(message, kind) {
    const node = $("status");
    node.textContent = message || "";
    node.className = kind === "error" ? "error" : "muted";
  }

  function modal(title, data) {
    $("modal-title").textContent = title;
    $("modal-body").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    $("modal").hidden = false;
  }
  function modalContent(title, node) {
    $("modal-title").textContent = title;
    $("modal-body").textContent = "";
    $("modal-body").appendChild(node);
    $("modal").hidden = false;
  }
  $("modal-close").onclick = () => { $("modal").hidden = true; };
  $("modal").onclick = (e) => { if (e.target === $("modal")) $("modal").hidden = true; };

  /* ---------- login ---------- */
  $("login-form").onsubmit = async (e) => {
    e.preventDefault();
    const candidate = $("token").value.trim();
    $("login-error").hidden = true;
    try {
      token = candidate;
      await api("GET", "/api/v1/console/overview");
      sessionStorage.setItem("aiops.view", state.view || "chat"); // view only, no secret
      $("login").hidden = true;
      $("app").hidden = false;
      $("token").value = "";
      boot();
    } catch (err) {
      token = "";
      const box = $("login-error");
      box.textContent = err.status === 403 ? "凭据无效。" : ("无法连接：" + err.message);
      box.hidden = false;
    }
  };
  $("lock").onclick = () => {
    token = "";
    state = { view: "chat", roleId: null, tab: "overview" };
    $("app").hidden = true;
    $("login").hidden = false;
    say("");
  };

  /* ---------- navigation ---------- */
  const VIEWS = [["chat", "聊天"], ["console", "控制台"]];
  function renderNav() {
    const nav = $("nav");
    nav.textContent = "";
    VIEWS.forEach(([id, label]) => {
      const button = el("button", state.view === id ? "active" : "", label);
      button.onclick = () => { state.view = id; renderNav(); renderView(); };
      nav.appendChild(button);
    });
  }
  function renderView() {
    $("view-chat").hidden = state.view !== "chat";
    $("view-console").hidden = state.view !== "console";
    if (state.view === "chat") loadChat();
    else renderConsole();
  }

  /* ---------- chat ---------- */
  let chatBusy = false;
  async function loadChat() {
    try {
      const roles = await api("GET", "/api/v1/roles");
      const list = $("chat-roles");
      list.textContent = "";
      if (!roles.length) list.appendChild(el("li", "muted", "尚无角色，请先在控制台创建（当前后端未提供角色创建接口）。"));
      roles.forEach((role) => {
        const item = el("li", state.roleId === role.id ? "active" : "");
        item.appendChild(el("span", "", role.name));
        item.appendChild(el("span", "muted small", role.id));
        item.onclick = () => { state.roleId = role.id; loadChat(); };
        list.appendChild(item);
      });
      if (!state.roleId && roles.length) { state.roleId = roles[0].id; loadChat(); return; }
      $("chat-title").textContent = state.roleId ? ("聊天 · " + state.roleId) : "聊天";
      if (state.roleId) {
        const model = await api("GET", "/api/v1/roles/" + state.roleId + "/model");
        $("chat-model").textContent = model.enabled ? ("模型已启用 · " + (model.model || "默认")) : "模型未启用（轮次会停留在 queued）";
      }
      renderChatLog();
    } catch (err) { say("加载失败：" + err.message, "error"); }
  }

  async function renderChatLog() {
    const log = $("chat-log");
    log.textContent = "";
    if (!state.roleId) return;
    const turns = await api("GET", "/api/v1/roles/" + state.roleId + "/turns?limit=100");
    turns.slice().reverse().forEach((turn) => log.appendChild(turnCard(turn)));
    log.scrollTop = 0;
  }

  function turnCard(turn) {
    const card = el("div", "msg " + (turn.source === "chat" ? "user" : ""));
    const meta = el("div", "meta");
    meta.appendChild(el("span", "", turn.source + " · " + fmt(turn.created_at)));
    meta.appendChild(el("span", "", "轮次 " + shortId(turn.id)));
    meta.appendChild(el("span", pillClass(turn.state), turn.state));
    if (turn.mode) meta.appendChild(el("span", "pill", turn.mode));
    meta.appendChild(el("span", "muted small", "账号: " + (usersToText(turn.execution_users) || "无")));
    card.appendChild(meta);
    card.appendChild(el("pre", "", turn.prompt));

    if (turn.payload && Object.keys(turn.payload).length) {
      card.appendChild(el("div", "tool", "事件数据：" + JSON.stringify(turn.payload)));
    }
    if (turn.error_code) card.appendChild(el("div", "error small", "错误：" + turn.error_code));
    if (turn.state === "awaiting_approval" || (turn.pending_task_id && turn.state === "waiting_tool")) {
      card.appendChild(actionsForTurn(turn));
    }
    const details = el("button", "ghost small", "查看完整轮次 / 模型调用");
    details.style.marginTop = "8px";
    details.onclick = async () => {
      const full = await api("GET", "/api/v1/turns/" + turn.id);
      modal("轮次 " + turn.id, full);
    };
    card.appendChild(details);
    if (turn.final_text) {
      const final = el("div", "");
      final.style.marginTop = "8px";
      final.appendChild(el("div", "muted small", "最终回复"));
      final.appendChild(el("pre", "", turn.final_text));
      card.appendChild(final);
    }
    return card;
  }

  function pillClass(value) {
    if (["completed", "succeeded"].includes(value)) return "pill ok";
    if (["failed", "cancelled", "blocked_unknown"].includes(value)) return "pill bad";
    return "pill warn";
  }

  function actionsForTurn(turn) {
    const box = el("div", "row");
    box.style.marginTop = "8px";
    const taskId = turn.pending_task_id;
    if (taskId) {
      const approve = el("button", "small", "批准并入队");
      approve.onclick = async () => {
        try { await api("POST", "/api/v1/tasks/" + taskId + "/approve"); say("已批准"); await renderChatLog(); }
        catch (err) { say("批准失败：" + err.message, "error"); }
      };
      const reject = el("button", "small danger", "取消该命令");
      reject.onclick = async () => {
        try { await api("POST", "/api/v1/tasks/" + taskId + "/cancel"); say("已取消"); await renderChatLog(); }
        catch (err) { say("取消失败：" + err.message, "error"); }
      };
      box.appendChild(el("span", "muted small", "待处理命令 " + shortId(taskId)));
      box.appendChild(approve);
      box.appendChild(reject);
    }
    const cancelTurn = el("button", "ghost small", "取消整轮");
    cancelTurn.onclick = async () => {
      try { await api("POST", "/api/v1/turns/" + turn.id + "/cancel"); say("已取消轮次"); await renderChatLog(); }
      catch (err) { say("取消失败：" + err.message, "error"); }
    };
    box.appendChild(cancelTurn);
    return box;
  }

  $("chat-refresh").onclick = () => loadChat();
  $("chat-form").onsubmit = async (e) => {
    e.preventDefault();
    if (!state.roleId) { say("请先选择角色", "error"); return; }
    if (chatBusy) return;
    const text = $("chat-text").value.trim();
    if (!text) return;
    const body = {
      text,
      execution_users: textToUsers($("chat-users").value),
      mode: $("chat-mode").value,
      idempotency_key: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, ""),
    };
    if (body.idempotency_key.length < 8) body.idempotency_key = "k" + body.idempotency_key;
    chatBusy = true;
    try {
      const answer = await api("POST", "/api/v1/roles/" + state.roleId + "/messages", body);
      $("chat-text").value = "";
      say(answer.duplicate ? "重复消息，已复用原轮次" : "已入队，等待模型处理…");
      await renderChatLog();
      watchTurn(answer.turn_id);
    } catch (err) { say("发送失败：" + err.message, "error"); }
    finally { chatBusy = false; }
  };

  function watchTurn(turnId, tries) {
    const budget = tries === undefined ? 20 : tries;
    if (budget <= 0 || !turnId) return;
    setTimeout(async () => {
      try {
        const turn = await api("GET", "/api/v1/turns/" + turnId);
        await renderChatLog();
        if (["queued", "ready", "calling", "waiting_tool"].includes(turn.state)) watchTurn(turnId, budget - 1);
      } catch (err) { /* transient; stop watching */ }
    }, 3000);
  }

  /* ---------- console ---------- */
  const TABS = [
    ["overview", "总览"], ["attention", "需人工处置"], ["alarms", "告警日志"],
    ["tasks", "任务"], ["roles", "角色"], ["turns", "角色轮次"], ["custom", "定制任务"],
    ["turns-loop", "触发器事件"], ["channels", "渠道"], ["documents", "文档"], ["assets", "资产"],
    ["skills", "Skills"], ["memory", "记忆"], ["audit", "审计"], ["output", "输出"],
  ];
  function renderConsole() {
    const tabs = $("console-tabs");
    tabs.textContent = "";
    TABS.forEach(([id, label]) => {
      const item = el("li", state.tab === id ? "active" : "", label);
      item.onclick = () => { state.tab = id; renderConsole(); };
      tabs.appendChild(item);
    });
    const body = $("console-body");
    body.textContent = "";
    const head = el("div", "pane-head");
    head.appendChild(el("h2", "", TABS.find((t) => t[0] === state.tab)[1]));
    body.appendChild(head);
    const holder = el("div", "");
    holder.style.overflow = "auto";
    body.appendChild(holder);
    const loaders = {
      overview: loadOverview, attention: loadAttention, alarms: loadAlarms, tasks: loadTasks,
      turns: loadTurns, custom: loadCustom, "turns-loop": loadEvents, documents: loadDocuments,
      assets: loadAssets, channels: loadChannels, skills: loadSkills, memory: loadMemory, audit: loadAudit, output: loadOutput,
      roles: loadRoles,
    };
    loaders[state.tab](holder).catch((err) => holder.appendChild(el("div", "card error", "加载失败：" + err.message)));
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

  async function loadOverview(host) {
    const box = card("服务概况");
    const grid = el("div", "grid");
    const health = await api("GET", "/healthz");
    grid.appendChild(kv("状态", health.status));
    grid.appendChild(kv("执行协议", health.protocol_version));
    const output = await api("GET", "/api/v1/console/overview");
    Object.keys(output.counts).forEach((key) => grid.appendChild(kv(key, output.counts[key])));
    box.appendChild(grid);
    host.appendChild(box);

    const notice = card("当前缺口（如实呈现，不假装完成）");
    notice.appendChild(el("p", "muted small", output.notices.join("；")));
    host.appendChild(notice);

    const attention = await api("GET", "/api/v1/operator/attention");
    const box2 = card("需人工处置");
    const grid2 = el("div", "grid");
    grid2.appendChild(kv("未知执行", attention.unknown_executions.length));
    grid2.appendChild(kv("陈旧租约", attention.stale_leases.length));
    grid2.appendChild(kv("离线资产", attention.offline_assets.length));
    box2.appendChild(grid2);
    host.appendChild(box2);
  }

  async function loadAttention(host) {
    const data = await api("GET", "/api/v1/operator/attention");
    const unknown = data.unknown_executions;
    host.appendChild(card("未知执行（必须人工核实后处置，不能用取消代替）")).appendChild(
      table(["任务", "资产", "角色", "更新时间", "操作"], unknown.map((r) => [shortId(r.id), r.asset_id, r.role_id, fmt(r.updated_at), "处置"]), (row) => resolveDialog(unknown.find((r) => shortId(r.id) === row[0]))));
    host.appendChild(card("陈旧租约（仅观测，未重派）")).appendChild(
      table(["任务", "资产", "状态", "超期秒", "说明"], data.stale_leases.map((r) => [shortId(r.task_id), r.asset_id, r.state, r.expired_for, r.note])));
    host.appendChild(card("离线资产")).appendChild(
      table(["资产", "静默秒"], data.offline_assets.map((r) => [r.asset_id, r.silent_for])));
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
          renderConsole();
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

  async function loadTasks(host) {
    const data = await api("GET", "/api/v1/console/tasks?limit=100");
    host.appendChild(card("任务（最近 100 条）")).appendChild(
      table(["任务", "角色", "资产", "状态", "运行账号", "命令", "更新时间"], data.map((r) => [shortId(r.id), r.role_id, r.asset_id, r.state, (r.payload && r.payload.run_as) || "-", (r.payload && r.payload.command) || "-", fmt(r.updated_at)]), (row) => {
        const record = data.find((r) => shortId(r.id) === row[0]);
        modal("任务 " + record.id, record);
      }));
  }

  async function loadRoles(host) {
    const data = await api("GET", "/api/v1/roles");
    const box = card("角色（创建 / 改名；模型与记忆另行配置）");
    const add = el("button", "small", "新建角色");
    add.onclick = () => roleCreator();
    box.appendChild(add);
    box.appendChild(table(["ID", "名称", "操作"], data.map((r) => [r.id, r.name, "改名"]), (row) => {
      roleRenamer(data.find((r) => r.id === row[0]));
    }));
    host.appendChild(box);
  }

  function roleCreator() {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "id（如 ops）";
    const nameInput = el("input"); nameInput.placeholder = "名称";
    fields.appendChild(idInput); fields.appendChild(nameInput);
    const feedback = el("p", "error small", "");
    const save = el("button", "", "创建");
    save.onclick = async () => {
      const body = { id: idInput.value.trim(), name: nameInput.value.trim() };
      try { await api("POST", "/api/v1/console/roles", body); $("modal").hidden = true; say("角色已创建"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    box.appendChild(el("p", "muted small", "创建只建名称；模型、记忆、文档需分别配置。"));
    box.appendChild(fields); box.appendChild(save); box.appendChild(feedback);
    modalContent("新建角色", box);
  }

  function roleRenamer(record) {
    if (!record) return;
    const box = el("div", "");
    const fields = el("div", "fields");
    const nameInput = el("input"); nameInput.value = record.name; nameInput.placeholder = "名称";
    fields.appendChild(nameInput);
    const feedback = el("p", "error small", "");
    const save = el("button", "", "保存");
    save.onclick = async () => {
      try { await api("PUT", "/api/v1/roles/" + record.id, { name: nameInput.value.trim() }); $("modal").hidden = true; say("角色已改名"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    box.appendChild(fields); box.appendChild(save); box.appendChild(feedback);
    modalContent("角色 " + record.id, box);
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
        modal("轮次 " + turn.id, turn);
      }));
    };
    picker.appendChild(select); picker.appendChild(button); picker.appendChild(holder);
    host.appendChild(picker);
    if (state.roleId) button.onclick();
  }

  async function loadCustom(host) {
    const data = await api("GET", "/api/v1/custom-tasks");
    const box = card("定制任务（触发任务 / 定时任务）");
    const add = el("button", "small", "新建定制任务");
    add.onclick = () => customTaskEditor(null);
    box.appendChild(add);
    box.appendChild(table(["ID", "名称", "类型", "角色", "模式", "启用", "下次触发", "触发路径", "操作"],
      data.map((r) => [r.id, r.name, r.kind, r.role_id, r.mode, r.enabled ? "是" : "否", fmt(r.next_fire_at), r.trigger_path, "编辑"]), (row) => {
      customTaskEditor(data.find((r) => r.id === row[0]));
    }));
    host.appendChild(box);
    const outbox = await api("GET", "/api/v1/schedule-deliveries?limit=50");
    host.appendChild(card("定时投递箱")).appendChild(table(["计划时间", "任务", "状态", "尝试", "最近错误"], outbox.map((o) => [fmt(o.scheduled_for), o.custom_task_id, o.state, o.attempts, o.last_error])));
    const events = await api("GET", "/api/v1/custom-task-events?limit=50");
    host.appendChild(card("最近事件")).appendChild(table(["时间", "来源", "角色", "状态", "轮次"], events.map((e) => [fmt(e.created_at), e.source, e.role_id, e.state, shortId(e.turn_id)])));
  }

  function customTaskEditor(record) {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "id";
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const kindSelect = el("select");
    [["trigger", "trigger（被调用触发）"], ["scheduled", "scheduled（定时）"]].forEach(([v, l]) => { const o = el("option", "", l); o.value = v; kindSelect.appendChild(o); });
    const roleSelect = el("select");
    const modeSelect = el("select");
    ["confirm", "readonly", "direct"].forEach((m) => { const o = el("option", "", m); o.value = m; modeSelect.appendChild(o); });
    const usersInput = el("input"); usersInput.placeholder = "执行账号，逗号分隔";
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(kindSelect); fields.appendChild(roleSelect); fields.appendChild(modeSelect); fields.appendChild(usersInput);
    const prompt = el("textarea"); prompt.rows = 5; prompt.style.width = "100%"; prompt.placeholder = "提示词";
    const schedFields = el("div", "fields");
    const cronInput = el("input"); cronInput.placeholder = "cron（如 0 3 * * *）";
    const tzInput = el("input"); tzInput.placeholder = "时区（默认 Asia/Shanghai）"; tzInput.value = "Asia/Shanghai";
    const enabledInput = el("input"); enabledInput.type = "checkbox"; enabledInput.checked = true;
    const enabledWrap = el("label", "inline"); enabledWrap.appendChild(enabledInput); enabledWrap.appendChild(el("span", "", "启用"));
    schedFields.appendChild(cronInput); schedFields.appendChild(tzInput); schedFields.appendChild(enabledWrap);
    if (record) {
      idInput.value = record.id; idInput.disabled = true;
      nameInput.value = record.name; kindSelect.value = record.kind;
      modeSelect.value = record.mode; usersInput.value = usersToText(record.execution_users);
      prompt.value = record.prompt; cronInput.value = record.cron || ""; tzInput.value = record.timezone;
      enabledInput.checked = !!record.enabled;
    }
    const schedWrap = el("div", "");
    schedWrap.appendChild(schedFields);
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
    save.onclick = async () => {
      const body = { id: idInput.value.trim(), name: nameInput.value.trim(), kind: kindSelect.value,
        role_id: roleSelect.value, prompt: prompt.value, execution_users: textToUsers(usersInput.value),
        mode: modeSelect.value, enabled: enabledInput.checked };
      if (kindSelect.value === "scheduled") { body.cron = cronInput.value.trim(); body.timezone = tzInput.value.trim() || "Asia/Shanghai"; }
      try {
        if (record) {
          await api("PUT", "/api/v1/console/custom-tasks/" + record.id, body);
          feedback.textContent = ""; out.textContent = "已保存（触发令牌不变）。";
          say("定制任务已保存"); renderConsole();
        } else {
          const created = await api("POST", "/api/v1/console/custom-tasks", body);
          feedback.textContent = "";
          out.textContent = "触发令牌（仅此一次显示，请立即保存）：" + created.trigger_token + "\n触发路径：" + created.trigger_path;
          say("定制任务已创建"); renderConsole();
        }
      } catch (err) { feedback.textContent = err.message; }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    box.appendChild(fields); box.appendChild(prompt); box.appendChild(schedWrap);
    box.appendChild(actions); box.appendChild(feedback); box.appendChild(out);
    modalContent(record ? "编辑定制任务 " + record.id : "新建定制任务", box);
  }

  async function loadEvents(host) {    const data = await api("GET", "/api/v1/custom-task-events?limit=100");
    host.appendChild(card("触发器事件")).appendChild(
      table(["时间", "任务", "来源", "角色", "状态", "轮次"], data.map((e) => [fmt(e.created_at), e.custom_task_id, e.source, e.role_id, e.state, shortId(e.turn_id)]), (row) => {
        const record = data.find((e) => fmt(e.created_at) === row[0] && e.custom_task_id === row[1]);
        modal("事件 " + record.id, record);
      }));
  }

  async function loadDocuments(host) {
    const data = await api("GET", "/api/v1/documents");
    const box = card("文档（核心文档强制注入，跨角色共享）");
    const add = el("button", "small", "新建 / 编辑文档");
    add.onclick = () => documentEditor(null);
    box.appendChild(add);
    box.appendChild(table(["ID", "名称", "核心", "角色", "修订", "正文字符", "操作"], data.map((r) => [r.id, r.name, r.core ? "是" : "否", usersToText(r.role_ids), r.revision, (r.content || "").length, "编辑"]), (row) => {
      documentEditor(data.find((r) => r.id === row[0]));
    }));
    host.appendChild(box);
  }

  function documentEditor(record) {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "id";
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
      const body = { id: idInput.value.trim(), name: nameInput.value.trim(), content: content.value, core: coreInput.checked, role_ids: textToUsers(rolesInput.value) };
      try { await api("PUT", "/api/v1/documents/" + body.id, body); $("modal").hidden = true; say("文档已保存"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    box.appendChild(el("p", "muted small", "核心文档强制注入所有角色；非核心文档仅注入指定角色；保存有修订号。"));
    box.appendChild(fields); box.appendChild(content); box.appendChild(actions); box.appendChild(feedback);
    modalContent(record ? "编辑文档 " + record.id : "新建文档", box);
  }

  async function loadSkills(host) {
    const data = await api("GET", "/api/v1/skills");
    const box = card("本地 Skills（作为参考文本注入本角色，不授予任何权限）");
    const add = el("button", "small", "新建 Skill");
    add.onclick = () => skillEditor(null);
    const pull = el("button", "small", "从官网 Skills 库同步");
    pull.onclick = () => skillSyncDialog();
    box.appendChild(add);
    box.appendChild(pull);
    box.appendChild(table(["ID", "名称", "启用", "角色", "修订", "字符", "操作"],
      data.map((r) => [r.id, r.name, r.enabled ? "是" : "否", usersToText(r.role_ids), r.revision, (r.content || "").length, "编辑"]),
      (row) => skillEditor(data.find((r) => r.id === row[0]))));
    host.appendChild(box);
    const note = card("边界");
    note.appendChild(el("p", "muted small", "Skill 是数据不是代码：不能增加账号、改模式或授予工具，只作为参考材料注入模型上下文。"));
    host.appendChild(note);
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
      out.innerHTML = "";
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

  function skillEditor(record) {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "id";
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const rolesInput = el("input"); rolesInput.placeholder = "角色 id，逗号分隔";
    const enabledInput = el("input"); enabledInput.type = "checkbox"; enabledInput.checked = true;
    const enabledWrap = el("label", "inline"); enabledWrap.appendChild(enabledInput); enabledWrap.appendChild(el("span", "", "启用"));
    if (record) { idInput.value = record.id; idInput.disabled = true; nameInput.value = record.name; rolesInput.value = usersToText(record.role_ids); enabledInput.checked = !!record.enabled; }
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(rolesInput); fields.appendChild(enabledWrap);
    const content = el("textarea"); content.rows = 12; content.style.width = "100%"; content.value = record ? record.content : "";
    const feedback = el("p", "error small", "");
    const save = el("button", "", record ? "保存" : "创建");
    const del = el("button", "ghost danger", "删除");
    del.onclick = async () => {
      if (!record) return;
      try { await api("DELETE", "/api/v1/skills/" + record.id); $("modal").hidden = true; say("Skill 已删除"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    save.onclick = async () => {
      const body = { id: idInput.value.trim(), name: nameInput.value.trim(), content: content.value, role_ids: textToUsers(rolesInput.value), enabled: enabledInput.checked };
      try { await api("PUT", "/api/v1/skills/" + body.id, body); $("modal").hidden = true; say("Skill 已保存"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    const actions = el("div", "row"); actions.appendChild(save);
    if (record) actions.appendChild(del);
    box.appendChild(fields); box.appendChild(content); box.appendChild(actions); box.appendChild(feedback);
    modalContent(record ? "编辑 Skill " + record.id : "新建 Skill", box);
  }

  async function loadChannels(host) {
    const identities = await api("GET", "/api/v1/channels/identities");
    const box = card("渠道身份（飞书/企业微信/微信，共享同一个角色的记忆与串行队列）");
    box.appendChild(table(["渠道", "用户", "角色", "模式", "账号", "最近活动", "操作"],
      identities.map((i) => [i.channel, i.user_id, i.role_id, i.mode, usersToText(i.execution_users), fmt(i.last_seen), "解绑"]),
      (row) => {
        const record = identities.find((i) => i.channel === row[0] && i.user_id === row[1]);
        modal("渠道身份 " + record.channel + " / " + record.user_id, record);
      }));
    host.appendChild(box);

    const note = card("边界说明");
    note.appendChild(el("p", "muted small",
      "新身份必须先由管理员签发一次性配对码才能使用；未配对来信只记录不执行。" +
      "微信 / 企业微信的对外推送不在本仓库内置，出站消息以投递形式保存，由外部渠道桥接拉取；我们不会把“已存储”说成“已发送”。"));
    host.appendChild(note);

    const role = card("为某角色签发配对码");
    const roleSelect = el("select");
    const modeSelect = el("select");
    ["confirm", "readonly", "direct"].forEach((m) => { const o = el("option", "", m); o.value = m; modeSelect.appendChild(o); });
    const userInput = el("input"); userInput.placeholder = "执行账号，逗号分隔，如 reader";
    const issue = el("button", "", "签发");
    const out = el("pre", "pre-list", "");
    api("GET", "/api/v1/roles").then((roles) => roles.forEach((r) => {
      const o = el("option", "", r.name + " (" + r.id + ")"); o.value = r.id; roleSelect.appendChild(o);
    }));
    issue.onclick = async () => {
      try {
        const issued = await api("POST", "/api/v1/channels/pairings", {
          role_id: roleSelect.value, mode: modeSelect.value, execution_users: textToUsers(userInput.value),
        });
        out.textContent = "配对码（仅此一次显示）：" + issued.pairing_code;
      } catch (err) { out.textContent = "失败：" + err.message; }
    };
    const row = el("div", "fields");
    row.appendChild(roleSelect); row.appendChild(modeSelect); row.appendChild(userInput); row.appendChild(issue);
    role.appendChild(row); role.appendChild(out);
    host.appendChild(role);
  }

  async function loadAssets(host) {
    const data = await api("GET", "/api/v1/assets");
    const box = card("资产（可注册 / 改名 / 备注 / 允许账号）");
    const add = el("button", "small", "注册资产");
    add.onclick = () => assetCreator();
    box.appendChild(add);
    box.appendChild(table(["ID", "名称", "接入方式", "允许账号", "操作"], data.map((r) => [r.id, r.name, r.connection_type, usersToText(r.allowed_users), "编辑"]), (row) => {
      assetEditor(data.find((r) => r.id === row[0]));
    }));
    host.appendChild(box);
    for (const asset of data) {
      try {
        const status = await api("GET", "/api/v1/agents/" + asset.id + "/status");
        host.appendChild(card("在线状态 · " + asset.id)).appendChild(
          table(["在线", "最近心跳", "未完成任务"], [[status.online ? "在线" : "离线", status.presence ? fmt(status.presence.last_seen) : "-", status.unfinished_tasks.length]]));
      } catch (err) { /* ssh assets have no agent presence */ }
    }
  }

  function assetCreator() {
    const box = el("div", "");
    const fields = el("div", "fields");
    const idInput = el("input"); idInput.placeholder = "id";
    const nameInput = el("input"); nameInput.placeholder = "名称";
    const usersInput = el("input"); usersInput.placeholder = "允许账号，逗号分隔（至少一个）";
    const typeSelect = el("select");
    [["agent", "agent（安装 Agent）"], ["ssh", "ssh（直连）"]].forEach(([v, label]) => { const o = el("option", "", label); o.value = v; typeSelect.appendChild(o); });
    fields.appendChild(idInput); fields.appendChild(nameInput); fields.appendChild(usersInput); fields.appendChild(typeSelect);
    const sshFields = el("div", "fields");
    const hostInput = el("input"); hostInput.placeholder = "ssh_host";
    const portInput = el("input"); portInput.placeholder = "ssh_port（默认 22）";
    const userInput = el("input"); userInput.placeholder = "ssh_user";
    const authSelect = el("select");
    [["key", "key"], ["password", "password"]].forEach(([v, label]) => { const o = el("option", "", label); o.value = v; authSelect.appendChild(o); });
    const refInput = el("input"); refInput.placeholder = "ssh_secret_ref（服务器上的秘密文件路径）";
    const keyInput = el("input"); keyInput.placeholder = "ssh_host_key（预置的主机公钥）";
    sshFields.appendChild(hostInput); sshFields.appendChild(portInput); sshFields.appendChild(userInput);
    sshFields.appendChild(authSelect); sshFields.appendChild(refInput); sshFields.appendChild(keyInput);
    const notes = el("textarea"); notes.rows = 4; notes.style.width = "100%"; notes.placeholder = "备注（会注入模型上下文）";
    const feedback = el("p", "error small", "");
    const out = el("pre", "pre-list", "");
    const sshWrap = el("div", "");
    sshWrap.appendChild(el("p", "muted small", "SSH 需预先在服务器上放置密钥文件；此表单只提交指向路径，不传密钥内容。主机公钥必须预置，不匹配即拒连。"));
    sshWrap.appendChild(sshFields);
    const toggle = () => { sshWrap.hidden = typeSelect.value !== "ssh"; };
    typeSelect.onchange = toggle; toggle();
    const save = el("button", "", "注册");
    save.onclick = async () => {
      const body = { id: idInput.value.trim(), name: nameInput.value.trim(), allowed_users: textToUsers(usersInput.value),
        connection_type: typeSelect.value, notes: notes.value };
      if (typeSelect.value === "ssh") {
        Object.assign(body, { ssh_host: hostInput.value.trim() || null, ssh_port: Number(portInput.value) || 22,
          ssh_user: userInput.value.trim() || null, ssh_auth_kind: authSelect.value,
          ssh_secret_ref: refInput.value.trim() || null, ssh_host_key: keyInput.value.trim() || null });
      }
      try {
        const result = await api("POST", "/api/v1/console/assets", body);
        out.textContent = result.agent_token
          ? "Agent token（仅此一次显示，请立即保存）：" + result.agent_token
          : "资产已注册。";
        feedback.textContent = "";
        say("资产已注册");
      } catch (err) { feedback.textContent = err.message; }
    };
    box.appendChild(fields); box.appendChild(sshWrap); box.appendChild(notes); box.appendChild(save); box.appendChild(feedback); box.appendChild(out);
    modalContent("注册资产", box);
  }

  function assetEditor(record) {
    if (!record) return;
    const box = el("div", "");
    const fields = el("div", "fields");
    const nameInput = el("input"); nameInput.value = record.name; nameInput.placeholder = "名称";
    const usersInput = el("input"); usersInput.value = usersToText(record.allowed_users); usersInput.placeholder = "允许账号，逗号分隔";
    fields.appendChild(nameInput); fields.appendChild(usersInput);
    const notes = el("textarea"); notes.rows = 6; notes.style.width = "100%"; notes.value = record.notes || "";
    notes.placeholder = "备注（会注入模型上下文，作为资产说明）";
    const feedback = el("p", "error small", "");
    const save = el("button", "", "保存");
    save.onclick = async () => {
      const body = { name: nameInput.value.trim(), allowed_users: textToUsers(usersInput.value), notes: notes.value };
      try { await api("PUT", "/api/v1/assets/" + record.id + "/notes", body); $("modal").hidden = true; say("资产已更新"); renderConsole(); }
      catch (err) { feedback.textContent = err.message; }
    };
    box.appendChild(el("p", "muted small", "接入方式与凭据在这里不可改；token 轮换请用管理员 API。"));
    box.appendChild(fields); box.appendChild(notes); box.appendChild(save); box.appendChild(feedback);
    modalContent("资产 " + record.id, box);
  }

  async function loadMemory(host) {
    const roles = await api("GET", "/api/v1/roles");
    if (!roles.length) { host.appendChild(el("div", "card muted", "尚无角色")); return; }
    const box = card("按天分层记忆");
    const select = el("select");
    roles.forEach((role) => { const option = el("option", "", role.name + " (" + role.id + ")"); option.value = role.id; select.appendChild(option); });
    if (state.roleId) select.value = state.roleId;
    const button = el("button", "", "查看");
    const holder = el("div", "");
    button.onclick = async () => {
      holder.textContent = "";
      const policy = await api("GET", "/api/v1/roles/" + select.value + "/memory/policy");
      holder.appendChild(el("p", "muted small", "策略：" + JSON.stringify(policy)));
      const days = await api("GET", "/api/v1/roles/" + select.value + "/memory");
      holder.appendChild(table(["日期", "来源", "轮次", "编辑时间", "全文/压缩/梗概字符"], days.map((d) => [d.day, d.source, d.turn_count, fmt(d.edited_at), (d.full_text || "").length + " / " + (d.compressed || "").length + " / " + (d.summary || "").length]), (row) => {
        const day = days.find((d) => d.day === row[0]);
        modal("记忆 " + day.day, day);
      }));
    };
    box.appendChild(select); box.appendChild(button); box.appendChild(holder);
    host.appendChild(box);
    button.onclick();
  }

  async function loadAudit(host) {
    const data = await api("GET", "/api/v1/audit?limit=100");
    host.appendChild(card("审计（不可编辑）")).appendChild(
      table(["时间", "事件", "对象", "操作者"], data.map((r) => [fmt(r.created_at), r.event, r.entity_id, r.actor]), (row) => {
        const record = data.find((r) => fmt(r.created_at) === row[0] && r.event === row[1]);
        modal("审计 " + record.seq, record);
      }));
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
  function boot() {
    const remembered = sessionStorage.getItem("aiops.view");
    if (remembered === "console" || remembered === "chat") state.view = remembered;
    renderNav();
    renderView();
  }
  window.addEventListener("beforeunload", () => { token = ""; });
})();
