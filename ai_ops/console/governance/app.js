/* 治理控制台 —— 无依赖单文件。
 * 原则：
 *   1. 所有来自后端的数据只经 textContent 进入 DOM（h() 构造），从不拼 innerHTML。
 *   2. 轮询只重绘"队列"，不重绘正在阅读/输入的详情区，避免打断人的决定。
 *   3. 凭证库不提供任何明文/复制入口——控制面本来就拿不到明文。
 */
'use strict';

(function () {
  // ---------------------------------------------------------------- 基础
  const TOKEN_KEY = 'aiops.gov.token';
  const API_KEY = 'aiops.gov.api';
  const POLL_INBOX_MS = 5000;
  const POLL_ATTENTION_MS = 15000;
  const POLL_AUDIT_MS = 10000;
  const MAX_SEQ = 9007199254740991;

  const $ = (sel, root) => (root || document).querySelector(sel);

  // 预览用：?api=http://127.0.0.1:8765 指向另一个源（需后端允许跨域）。
  (function captureApiBase() {
    const q = new URLSearchParams(location.search);
    if (q.has('api')) {
      const v = q.get('api').replace(/\/+$/, '');
      if (v) sessionStorage.setItem(API_KEY, v); else sessionStorage.removeItem(API_KEY);
    }
  })();
  const API_BASE = sessionStorage.getItem(API_KEY) || '';

  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'style') el.style.cssText = v;
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden') el[k] = v;
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, kid) {
    if (kid == null || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(k => add(el, k)); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  // 仅用于本文件内的常量 SVG，不承载任何后端数据。
  function icon(svg) { const s = document.createElement('span'); s.innerHTML = svg; s.style.display = 'inline-flex'; return s; }
  const ICON = {
    lock: '<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/><path d="M12 14.5v2.5"/></svg>',
    check: '<svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
  };

  // ---------------------------------------------------------------- 时间
  function toDate(sec) { return new Date(Number(sec) * 1000); }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmtAbs(sec) {
    if (sec == null) return '—';
    const d = toDate(sec);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }
  function fmtClock(sec) { const d = toDate(sec); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function fmtDay(sec) {
    const d = toDate(sec), now = new Date();
    const same = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    const base = (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
    if (same(d, now)) return '今天 · ' + base;
    if (same(d, y)) return '昨天 · ' + base;
    return d.getFullYear() + ' 年 ' + base;
  }
  function fmtAgo(sec) {
    if (sec == null) return '—';
    const s = Math.max(0, Date.now() / 1000 - Number(sec));
    if (s < 45) return '刚刚';
    if (s < 3600) return Math.round(s / 60) + ' 分钟前';
    if (s < 86400) return Math.round(s / 3600) + ' 小时前';
    if (s < 86400 * 30) return Math.round(s / 86400) + ' 天前';
    return fmtAbs(sec).slice(0, 10);
  }
  function fmtWait(sec) {
    const s = Math.max(0, Date.now() / 1000 - Number(sec));
    if (s < 60) return '刚到';
    if (s < 3600) return '已等 ' + Math.floor(s / 60) + ' 分';
    if (s < 86400) return '已等 ' + Math.floor(s / 3600) + ' 小时';
    return '已等 ' + Math.floor(s / 86400) + ' 天';
  }
  function timeEl(sec, mode) {
    return h('time', { title: fmtAbs(sec), datetime: sec != null ? toDate(sec).toISOString() : null }, mode === 'abs' ? fmtAbs(sec) : fmtAgo(sec));
  }

  // ---------------------------------------------------------------- 文案
  const MODE = {
    readonly: { name: '只读排查', short: '只读', level: 1, desc: 'AI 只能运行只读名单里的工具，不会提出任何变更。' },
    confirm: { name: '提议 · 人批', short: '人批', level: 2, desc: 'AI 可以提出变更；每一个变更都进收件箱，等你点头。' },
    direct: { name: '白名单直行', short: '直行', level: 3, desc: '写名单内的动作自动执行；名单之外的仍进收件箱等你。' }
  };
  const MODES = ['readonly', 'confirm', 'direct'];
  const SLOT = {
    readonly_list: { label: '只读', kind: 'readonly', hint: 'AI 可以随意调用的只读工具' },
    write_list: { label: '写', kind: 'write', hint: '允许被提出（或直行）的变更动作' },
    deny_list: { label: '禁止', kind: 'deny', hint: '任何模式下都直接拒绝' }
  };
  const SLOTS = ['readonly_list', 'write_list', 'deny_list'];
  const LIST_KIND = {
    readonly: { name: '只读名单', cls: 'info' },
    write: { name: '写名单', cls: 'wait' },
    deny: { name: '禁止名单', cls: 'stop' }
  };
  const CRED_KIND = {
    pattern: { name: '片段匹配', cls: 'outline', hint: '正则命中的片段，可能误报（例如 git 提交哈希）' },
    path: { name: '敏感路径', cls: 'info', hint: '路径规则命中，整文件按密钥处理' },
    file: { name: '凭证文件', cls: 'violet', hint: '识别为凭证文件' },
    connector: { name: '经过控制面', cls: 'stop', hint: '无 Agent 的降级路径：值曾经过控制面传输，请重点关注' }
  };
  const CALLER = { web: '网页对话', feishu: '飞书' };
  function callerName(c) {
    if (!c) return '未知来源';
    if (CALLER[c]) return CALLER[c];
    if (String(c).indexOf('trigger:') === 0) return '触发器 ' + String(c).slice(8);
    return c;
  }
  const EVENT_HUMAN = {
    'task.approved': '管理员批准了变更', 'task.rejected': '管理员拒绝了变更', 'task.result': 'Agent 回报执行结果',
    'task.cancelled': '任务被取消', 'task.created': '任务已创建', 'task.claimed': 'Agent 领取任务',
    'question.answered': '管理员回答了问题', 'question.asked': 'AI 提出了问题',
    'credential.discovered': '发现疑似密钥并托管', 'credential.annotated': '凭证句柄被标注', 'credential.dismissed': '凭证被否决为误报',
    'name_list.created': '创建名单', 'name_list.forked': 'Fork 出新版本', 'name_list.bound': '模式绑定变更', 'name_list.retired': '名单退役',
    'asset.tags': '主机标签变更'
  };
  function eventTone(ev) {
    const e = String(ev || '');
    if (/(approved|answered|succeeded|completed|granted|\.ok$)/.test(e)) return 'go';
    if (/(rejected|denied|dismissed|failed|cancel|unknown|error|revoked|blocked)/.test(e)) return 'stop';
    if (/(await|request|propos|discover|asked|pending|created)/.test(e)) return 'wait';
    return 'info';
  }

  // ---------------------------------------------------------------- API
  class ApiError extends Error {
    constructor(status, detail) { super(detail || ('HTTP ' + status)); this.status = status; this.detail = detail; }
  }
  function getToken() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
  async function api(method, path, body, opts) {
    const headers = { Authorization: 'Bearer ' + getToken() };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetch(API_BASE + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, cache: 'no-store' });
    } catch (e) {
      setSync('err', '无法连接控制面');
      throw new ApiError(0, '网络不可达：' + (e && e.message ? e.message : e));
    }
    if (res.status === 401 && !(opts && opts.probe)) { lock('令牌无效或已失效，请重新输入。'); throw new ApiError(401, '未授权'); }
    let data = null;
    const text = await res.text();
    if (text) { try { data = JSON.parse(text); } catch (_) { data = text; } }
    if (!res.ok) {
      let detail = data && typeof data === 'object' ? data.detail : data;
      if (Array.isArray(detail)) detail = detail.map(d => (d.loc ? d.loc.slice(1).join('.') + '：' : '') + (d.msg || JSON.stringify(d))).join('；');
      else if (detail && typeof detail === 'object') detail = JSON.stringify(detail);
      throw new ApiError(res.status, detail || ('HTTP ' + res.status));
    }
    setSync('ok');
    return data;
  }
  const enc = encodeURIComponent;

  // ---------------------------------------------------------------- 提示
  function toast(msg, tone, ms) {
    const t = h('div', { class: 'toast ' + (tone || '') }, h('div', null, msg), h('button', { class: 'toast-x', type: 'button', 'aria-label': '关闭', onclick: () => t.remove() }, '✕'));
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), ms || 4200);
  }
  function errText(e) {
    if (!e) return '未知错误';
    if (e.status === 409) return '这一项已经被处理或已过期。';
    if (e.status === 404) return '对象不存在（可能已被处理或删除）。';
    if (e.status === 422) return '提交内容不被接受：' + (e.detail || '');
    return e.message || String(e);
  }
  let syncTimer = null;
  function setSync(state, text) {
    const s = $('#sync'); if (!s) return;
    s.className = 'sync ' + state;
    $('.sync-text', s).textContent = text || (state === 'ok' ? '已同步 · ' + fmtClock(Date.now() / 1000) : '连接中…');
    if (syncTimer) clearTimeout(syncTimer);
  }

  // ---------------------------------------------------------------- 闸门
  let started = false;
  function lock(message) {
    sessionStorage.removeItem(TOKEN_KEY);
    stopPolling();
    closeDrawer();
    $('#shell').hidden = true;
    $('#gate').hidden = false;
    const err = $('#gate-error');
    if (message) { err.textContent = message; err.hidden = false; } else err.hidden = true;
    document.title = '治理控制台';
    setTimeout(() => $('#gate-token').focus(), 30);
  }
  async function tryToken(tok) {
    sessionStorage.setItem(TOKEN_KEY, tok);
    try {
      await api('GET', '/api/v1/inbox', undefined, { probe: true });
      return true;
    } catch (e) {
      if (e.status === 401 || e.status === 403) sessionStorage.removeItem(TOKEN_KEY);
      throw e;
    }
  }
  function initGate() {
    $('#gate-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const input = $('#gate-token'), btn = $('#gate-submit'), err = $('#gate-error');
      const tok = input.value.trim();
      if (!tok) return;
      btn.disabled = true; btn.textContent = '验证中…'; err.hidden = true;
      try {
        await tryToken(tok);
        input.value = '';
        unlock();
      } catch (e) {
        err.textContent = e.status === 401 || e.status === 403 ? '令牌不正确。' : errText(e);
        err.hidden = false;
        input.select();
      } finally { btn.disabled = false; btn.textContent = '进入'; }
    });
    $('#logout').addEventListener('click', () => lock());
  }
  function unlock() {
    $('#gate').hidden = true;
    $('#shell').hidden = false;
    if (!started) { started = true; window.addEventListener('hashchange', route); }
    startPolling();
    route();
  }

  // ---------------------------------------------------------------- 轮询
  const timers = [];
  function startPolling() {
    stopPolling();
    refreshInbox();
    refreshAttention();
    timers.push(setInterval(() => { if (!document.hidden) refreshInbox(); }, POLL_INBOX_MS));
    timers.push(setInterval(() => { if (!document.hidden) refreshAttention(); }, POLL_ATTENTION_MS));
    timers.push(setInterval(() => { if (!document.hidden && view === 'audit') pollAuditNew(); }, POLL_AUDIT_MS));
    timers.push(setInterval(tickRelativeTimes, 30000));
  }
  function stopPolling() { while (timers.length) clearInterval(timers.pop()); }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && getToken()) { refreshInbox(); refreshAttention(); } });
  function tickRelativeTimes() { if (view === 'inbox') renderQueue(); }

  // ---------------------------------------------------------------- 共享缓存
  const cache = { assets: null, assetsAt: 0, roles: null };
  async function loadAssets(force) {
    if (!force && cache.assets && Date.now() - cache.assetsAt < 30000) return cache.assets;
    const list = await api('GET', '/api/v1/assets');
    cache.assets = Array.isArray(list) ? list : [];
    cache.assetsAt = Date.now();
    return cache.assets;
  }
  function assetName(id) {
    const a = cache.assets && cache.assets.find(x => x.id === id);
    return a ? a.name || id : id;
  }

  async function refreshAttention() {
    try {
      const a = await api('GET', '/api/v1/operator/attention');
      const box = clear($('#signals'));
      const n = k => Array.isArray(a && a[k]) ? a[k].length : 0;
      if (n('unknown_executions')) box.appendChild(h('div', { class: 'signal', title: '执行端失联或结果未知的任务，需要人工核实主机实际状态' }, h('b', null, n('unknown_executions')), ' 个执行结果未知'));
      if (n('stale_leases')) box.appendChild(h('div', { class: 'signal soft' }, h('b', null, n('stale_leases')), ' 个执行租约过期'));
      if (n('offline_assets')) box.appendChild(h('div', { class: 'signal soft' }, h('b', null, n('offline_assets')), ' 台主机离线'));
    } catch (_) { /* 侧栏信号失败不打扰 */ }
  }

  // ---------------------------------------------------------------- 路由
  let view = null;
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, qs] = raw.split('?');
    const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
    return { name: parts[0] || 'inbox', arg: parts[1] || null, q: new URLSearchParams(qs || '') };
  }
  function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
  function route() {
    if (!getToken()) return;
    const r = parseHash();
    const names = ['inbox', 'lists', 'vault', 'audit'];
    const name = names.indexOf(r.name) >= 0 ? r.name : 'inbox';
    document.querySelectorAll('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.view === name));
    const prev = view;
    view = name;
    if (name === 'inbox') {
      if (prev !== 'inbox' || !$('#inbox-root')) mountInbox();
      if (r.arg) selectItem(r.arg, true);
    } else if (name === 'lists') mountLists(r);
    else if (name === 'vault') mountVault(r);
    else if (name === 'audit') mountAudit(r);
  }

  // ================================================================ 收件箱
  const inbox = { items: [], loaded: false, selected: null, filter: 'all', resolved: {}, detailKey: null, composer: null, answer: null, otherText: '' };
  function keyOf(it) {
    if (it.kind === 'approval') return 'a-' + it.task_id;
    if (it.kind === 'question') return 'q-' + it.question_id;
    return 'r-' + it.request_id;
  }
  const KIND_NAME = { approval: '变更审批', question: '业务问题', account_request: '账号申请' };

  async function refreshInbox() {
    try {
      const d = await api('GET', '/api/v1/inbox');
      const items = [].concat(d.approvals || [], d.questions || [], d.account_requests || []);
      items.forEach(it => { if (!it.kind) it.kind = it.task_id ? 'approval' : it.question_id ? 'question' : 'account_request'; });
      items.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
      inbox.items = items;
      const firstLoad = !inbox.loaded;
      inbox.loaded = true;
      updateBadge();
      if (view === 'inbox') {
        renderQueue();
        if (firstLoad) renderDetail(true);
        // 选中项被别处处理掉：只在详情区给出提示，不清空正在读的内容。
        if (inbox.selected && !findItem(inbox.selected) && !inbox.resolved[inbox.selected]) {
          inbox.resolved[inbox.selected] = { tone: '', text: '这一项已在别处被处理或已过期' };
          renderDetail(true);
        }
        if (!inbox.selected || (!findItem(inbox.selected) && inbox.detailKey == null)) autoSelect();
      }
    } catch (e) {
      if (e.status !== 401) setSync('err', '刷新失败 · ' + fmtClock(Date.now() / 1000));
    }
  }
  function updateBadge() {
    const n = inbox.items.length;
    const b = $('#badge-inbox');
    b.textContent = n; b.hidden = n === 0;
    document.title = (n ? '(' + n + ') ' : '') + '治理控制台';
  }
  function findItem(key) { return inbox.items.find(it => keyOf(it) === key) || null; }
  function visibleItems() { return inbox.items.filter(it => inbox.filter === 'all' || it.kind === inbox.filter); }
  function autoSelect() {
    const list = visibleItems();
    if (!list.length) { inbox.selected = null; renderQueue(); renderDetail(); return; }
    selectItem(keyOf(list[0]));
  }
  function selectItem(key, fromRoute) {
    if (inbox.selected !== key) { inbox.composer = null; inbox.answer = null; inbox.otherText = ''; }
    inbox.selected = key;
    renderQueue();
    renderDetail();
    if (!fromRoute && view === 'inbox') history.replaceState(null, '', '#/inbox/' + enc(key));
    const sel = $('.qi.sel');
    if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
  }

  function mountInbox() {
    const main = clear($('#main'));
    main.appendChild(h('div', { class: 'inbox', id: 'inbox-root' },
      h('section', { class: 'queue', 'aria-label': '待决定队列' },
        h('div', { class: 'queue-head' },
          h('h1', null, '收件箱', h('span', { class: 'big', id: 'q-count' }, '')),
          h('p', null, 'AI 停在这里，等你点头。最早到的排在最上面。'),
          h('div', { class: 'queue-filter', id: 'q-filter' })),
        h('ul', { class: 'queue-list', id: 'q-list' }),
        h('div', { class: 'queue-foot' },
          h('span', null, h('kbd', null, 'J'), ' ', h('kbd', null, 'K'), ' 切换'),
          h('span', null, h('kbd', null, 'A'), ' 批准'),
          h('span', null, h('kbd', null, 'R'), ' 拒绝'),
          h('span', null, h('kbd', null, 'C'), ' 看对话'))),
      h('section', { class: 'detail', id: 'detail', 'aria-live': 'polite' })));
    inbox.detailKey = null;
    renderQueue();
    renderDetail();
    if (inbox.loaded && !inbox.selected) autoSelect();
  }

  function renderQueue() {
    const list = $('#q-list'); if (!list) return;
    const counts = { all: inbox.items.length, approval: 0, question: 0, account_request: 0 };
    inbox.items.forEach(it => counts[it.kind]++);
    $('#q-count').textContent = inbox.loaded ? String(counts.all) : '';
    const f = clear($('#q-filter'));
    [['all', '全部'], ['approval', '审批'], ['question', '问题'], ['account_request', '账号']].forEach(([k, label]) => {
      f.appendChild(h('button', { type: 'button', class: 'chip' + (inbox.filter === k ? ' on' : ''), onclick: () => { inbox.filter = k; renderQueue(); if (!visibleItems().some(it => keyOf(it) === inbox.selected)) autoSelect(); } },
        label, h('span', { class: 'n' }, counts[k])));
    });
    clear(list);
    if (!inbox.loaded) {
      for (let i = 0; i < 3; i++) list.appendChild(h('li', { class: 'qi' }, h('div', { class: 'skeleton', style: 'width:40%;margin-bottom:8px' }), h('div', { class: 'skeleton', style: 'width:85%' })));
      return;
    }
    const items = visibleItems();
    // 刚处理掉的选中项保留一个淡出占位，避免列表在手下跳动。
    if (inbox.selected && inbox.resolved[inbox.selected] && !findItem(inbox.selected) && inbox.resolved[inbox.selected].item) {
      const ghost = inbox.resolved[inbox.selected].item;
      if (inbox.filter === 'all' || ghost.kind === inbox.filter) items.unshift(Object.assign({ _ghost: true }, ghost));
    }
    if (!items.length) {
      list.appendChild(h('li', { class: 'muted', style: 'padding:24px 12px;text-align:center;font-size:13px' }, inbox.filter === 'all' ? '没有待决定的事项' : '这一类目前为空'));
      return;
    }
    items.forEach(it => {
      const k = keyOf(it);
      const waitedLong = Date.now() / 1000 - (it.created_at || 0) > 600;
      list.appendChild(h('li', {
        class: 'qi k-' + it.kind + (k === inbox.selected ? ' sel' : '') + (it._ghost ? ' gone' : ''),
        tabindex: '0', role: 'button',
        onclick: () => selectItem(k),
        onkeydown: (e) => { if (e.key === 'Enter') selectItem(k); }
      },
        h('div', { class: 'qi-top' }, h('span', { class: 'qi-kind' }, KIND_NAME[it.kind]), it.role_name || it.role_id ? h('span', null, '· ' + (it.role_name || it.role_id)) : null,
          h('span', { class: 'qi-time', style: waitedLong && !it._ghost ? 'color:var(--wait);font-weight:600' : null, title: fmtAbs(it.created_at) }, it._ghost ? '已处理' : fmtWait(it.created_at))),
        queueBody(it)));
    });
  }
  function queueBody(it) {
    if (it.kind === 'approval') {
      const s = Array.isArray(it.steps) ? it.steps : [];
      const cmd = s.length ? [s[0].program].concat(s[0].args || []).join(' ') + (s.length > 1 ? '  … +' + (s.length - 1) + ' 步' : '') : (it.command || '');
      return [
        h('div', { class: 'qi-title' }, it.reason || '（AI 未给出理由）'),
        h('div', { class: 'qi-cmd' }, cmd),
        h('div', { class: 'qi-meta' }, h('span', null, it.asset_name || it.asset_id), h('span', null, '·'), h('span', { class: 'mono' }, it.run_as || '—'))
      ];
    }
    if (it.kind === 'question') {
      return [h('div', { class: 'qi-title' }, it.question), h('div', { class: 'qi-meta' }, (it.options || []).length + ' 个选项 + 其他')];
    }
    return [
      h('div', { class: 'qi-title' }, it.reason || '申请账号执行权'),
      h('div', { class: 'qi-meta' }, h('span', { class: 'mono' }, it.username), h('span', null, '@'), h('span', null, it.asset_name || it.asset_id))
    ];
  }

  function renderDetail(force) {
    const box = $('#detail'); if (!box) return;
    const key = inbox.selected;
    // 同一项且无强制：保持原 DOM（不打断阅读和输入）。
    if (!force && key === inbox.detailKey && box.firstChild) return;
    inbox.detailKey = key;
    clear(box);
    if (!inbox.loaded) { box.appendChild(h('div', { class: 'detail-inner' }, h('div', { class: 'skeleton', style: 'width:30%;margin:8px 0 16px' }), h('div', { class: 'skeleton', style: 'width:70%;height:24px;margin-bottom:24px' }), h('div', { class: 'skeleton', style: 'height:120px' }))); return; }
    const res = key && inbox.resolved[key];
    const it = key ? (findItem(key) || (res && res.item)) : null;
    if (!it) {
      box.appendChild(h('div', { class: 'empty empty-big' },
        h('div', { class: 'empty-mark' }, icon(ICON.check)),
        h('div', { class: 'empty-title' }, inbox.items.length ? '从左侧选一项' : '没有需要你决定的事'),
        h('div', null, inbox.items.length ? '' : 'AI 正在只读排查或空闲中。一旦它需要你点头，会出现在这里（每 5 秒刷新）。'),
        res ? h('div', { class: 'muted', style: 'margin-top:10px' }, res.text) : null));
      return;
    }
    const inner = h('div', { class: 'detail-inner' });
    box.appendChild(inner);
    if (res) inner.appendChild(h('div', { class: 'resolved ' + (res.tone || '') }, h('b', null, res.text), res.sub ? h('span', null, res.sub) : null,
      h('span', { class: 'spacer' }), nextButton()));
    if (it.kind === 'approval') renderApproval(inner, it, !!res);
    else if (it.kind === 'question') renderQuestion(inner, it, !!res);
    else renderAccount(inner, it, !!res);
  }
  function nextButton() {
    const nxt = visibleItems()[0];
    return nxt ? h('button', { class: 'btn btn-sm', type: 'button', onclick: () => selectItem(keyOf(nxt)) }, '下一项 →') : h('span', { class: 'muted' }, '队列已清空');
  }
  function kicker(it, extra) {
    return h('div', { class: 'd-kicker' },
      h('span', { class: 'kind', style: 'color:' + (it.kind === 'approval' ? 'var(--wait)' : it.kind === 'question' ? 'var(--info)' : 'var(--violet)') }, KIND_NAME[it.kind]),
      h('span', null, '·'), h('span', null, '来自角色「' + (it.role_name || it.role_id || '未知') + '」'),
      it.mode && MODE[it.mode] ? h('span', { class: 'pill outline', title: MODE[it.mode].desc }, MODE[it.mode].name + '模式') : null,
      extra,
      h('span', { class: 'spacer' }),
      h('span', { title: fmtAbs(it.created_at) }, fmtAgo(it.created_at)));
  }

  // ---- 审批卡
  function execBlock(steps, command, sha) {
    const n = Array.isArray(steps) ? steps.length : 0;
    const box = h('div', { class: 'exec' });
    box.appendChild(h('div', { class: 'exec-head' },
      h('b', null, '将要执行'), h('span', null, n ? n + ' 个步骤 · 直接 execve，不经 shell' : '旧格式命令'),
      n > 1 ? h('span', { class: 'atomic' }, '◆ 原子执行：整体批准或整体拒绝') : null));
    if (n) {
      steps.forEach((s, i) => {
        const argv = h('div', { class: 'argv' }, h('span', { class: 'tok prog', title: '程序（argv[0]）' }, s.program));
        (s.args || []).forEach(a => argv.appendChild(h('span', { class: 'tok arg' + (a === '' ? ' empty-arg' : '') }, a === '' ? '""' : a)));
        const params = s.params && typeof s.params === 'object' ? Object.keys(s.params) : [];
        box.appendChild(h('div', { class: 'step' },
          h('div', { class: 'step-n' }, String(i + 1)),
          h('div', null,
            h('div', { class: 'step-action' }, h('span', { class: 'act' }, s.action || '—'), s.timeout_seconds != null ? h('span', null, '超时 ' + s.timeout_seconds + 's') : null),
            argv,
            params.length ? h('div', { class: 'step-params' }, params.map((k, j) => [j ? '  ' : '', k + '=', h('span', null, typeof s.params[k] === 'string' ? s.params[k] : JSON.stringify(s.params[k]))])) : null)));
      });
    } else if (command) {
      box.appendChild(h('div', { class: 'legacy-cmd' }, command));
      box.appendChild(h('div', { class: 'legacy-note' }, '旧格式任务：只有一条命令文本，没有结构化步骤。'));
    } else {
      box.appendChild(h('div', { class: 'legacy-cmd', style: 'color:var(--term-dim)' }, '（没有步骤内容）'));
    }
    if (sha) {
      const short = sha.slice(0, 12) + '…' + sha.slice(-8);
      const hs = h('span', { class: 'hash' }, short);
      let full = false;
      box.appendChild(h('div', { class: 'seal' }, h('span', null, '封印 SHA-256'), hs,
        h('button', { type: 'button', onclick: (e) => { full = !full; hs.textContent = full ? sha : short; e.target.textContent = full ? '收起' : '展开'; } }, '展开')));
    }
    return box;
  }
  function renderApproval(root, it, done) {
    const steps = Array.isArray(it.steps) ? it.steps : [];
    root.appendChild(kicker(it));
    root.appendChild(h('h2', { class: 'd-title' }, it.reason || '（AI 未给出理由）'));
    root.appendChild(h('div', { class: 'who' },
      h('div', null, h('div', { class: 'who-label' }, '在哪台机'), h('div', { class: 'who-val' }, it.asset_name || it.asset_id || '—'), h('div', { class: 'who-sub' }, it.asset_id)),
      h('div', null, h('div', { class: 'who-label' }, '以哪个账号'), h('div', { class: 'who-val mono', style: 'font-family:var(--mono)' }, it.run_as || '—'), h('div', { class: 'who-sub' }, 'OS 用户')),
      h('div', null, h('div', { class: 'who-label' }, '跑什么'), (function () { const acts = steps.map(s => s.action).filter(Boolean); const txt = !steps.length ? '旧格式命令' : acts.length > 1 ? acts[0] + ' 等 ' + steps.length + ' 步' : (acts[0] || steps.length + ' 步'); return h('div', { class: 'who-val mono', style: 'font-family:var(--mono);font-size:14px', title: acts.join('\n') }, txt); })(), h('div', { class: 'who-sub' }, steps.length ? steps.map(s => s.program ? s.program.split('/').pop() : '').filter(Boolean).join(' · ') : ''))));
    root.appendChild(execBlock(steps, it.command, it.payload_sha256));
    root.appendChild(h('p', { class: 'exec-explain' }, '你批准的就是上面这串字节。执行端会重新计算哈希，与封印不一致就拒绝执行。'));

    const ctx = h('div', { class: 'ctx' });
    if (it.prompt) ctx.appendChild(h('div', { class: 'quote user' }, h('div', { class: 'quote-label' }, '触发这轮对话的原话 · ' + callerName(it.caller)), h('div', { class: 'quote-body' }, it.prompt)));
    ctx.appendChild(h('div', { class: 'row', style: 'gap:14px;font-size:13px' },
      h('button', { class: 'link-btn', style: 'font-size:13px;color:var(--info)', type: 'button', onclick: () => openTurn(it.turn_id) }, '查看完整对话 →'),
      h('a', { href: auditHash({ entity: it.task_id }) }, '这个任务的审计时间线'),
      h('a', { href: auditHash({ entity: it.turn_id }) }, '这一轮的审计时间线')));
    root.appendChild(ctx);
    if (done) return;

    const decide = h('div', { class: 'decide' });
    const bar = h('div', { class: 'decide-bar' },
      h('button', { class: 'btn btn-go', type: 'button', id: 'act-approve', onclick: () => openComposer('approve') }, '批准执行', h('span', { class: 'kbd-hint' }, 'A')),
      h('button', { class: 'btn btn-ghost-stop', type: 'button', id: 'act-reject', onclick: () => openComposer('reject') }, '拒绝并告诉 AI 原因', h('span', { class: 'kbd-hint' }, 'R')),
      h('span', { class: 'hint' }, steps.length > 1 ? '这 ' + steps.length + ' 步不能分开批准' : '409 表示已被他处处理，会自动刷新'));
    decide.appendChild(bar);
    const slot = h('div', { id: 'composer-slot' });
    decide.appendChild(slot);
    root.appendChild(decide);

    function openComposer(kind) {
      inbox.composer = kind;
      clear(slot);
      const go = kind === 'approve';
      const ta = h('textarea', { rows: '3', maxlength: '2000', placeholder: go ? '备注（可选，只进审计日志）' : '例如：不要 reload，先跑 nginx -t 校验配置，再把两步合成一次提给我。' });
      const submit = h('button', { class: 'btn ' + (go ? 'btn-go' : 'btn-stop'), type: 'button' }, go ? '确认批准，排队执行' : '发送拒绝理由给 AI');
      const cancel = h('button', { class: 'btn', type: 'button', onclick: () => { clear(slot); inbox.composer = null; } }, '取消');
      const fire = async () => {
        const reason = ta.value.trim();
        if (!go && !reason) { ta.focus(); ta.style.borderColor = 'var(--stop)'; toast('拒绝需要写理由——AI 要靠它改方案。', 'stop'); return; }
        submit.disabled = cancel.disabled = true;
        submit.textContent = '提交中…';
        try {
          await api('POST', '/api/v1/tasks/' + enc(it.task_id) + '/' + (go ? 'approve' : 'reject'), go ? (reason ? { reason } : {}) : { reason });
          markResolved(it, go ? 'go' : 'stop', go ? '已批准 · 进入执行队列' : '已拒绝 · 理由已回给 AI', go ? null : '「' + reason.slice(0, 60) + (reason.length > 60 ? '…' : '') + '」');
          toast(go ? '已批准，任务进入执行队列。' : '已拒绝，AI 会根据你的理由调整方案。', go ? 'go' : '');
        } catch (e) {
          if (e.status === 409) { markResolved(it, '', '这一项已被处理或已过期'); toast(errText(e) + '已刷新列表。', 'wait'); }
          else { toast(errText(e), 'stop'); submit.disabled = cancel.disabled = false; submit.textContent = go ? '确认批准，排队执行' : '发送拒绝理由给 AI'; }
        }
      };
      submit.addEventListener('click', fire);
      ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); fire(); } if (e.key === 'Escape') cancel.click(); });
      slot.appendChild(h('div', { class: 'composer ' + (go ? 'go' : 'stop') },
        h('div', { class: 'composer-head' }, go ? '批准这次变更' : '拒绝，并告诉 AI 为什么'),
        h('div', { class: 'composer-sub' }, go
          ? '将在「' + (it.asset_name || it.asset_id) + '」上以 ' + (it.run_as || '—') + ' 身份执行 ' + (steps.length || 1) + ' 个步骤。'
          : '这段话会原样发回给 AI，它会据此修改方案再来找你。写给 AI 看，不是写给日志的。'),
        ta,
        h('div', { class: 'composer-actions' }, submit, cancel, h('span', { class: 'hint' }, h('kbd', null, 'Ctrl'), ' + ', h('kbd', null, 'Enter'), ' 提交 · ', h('kbd', null, 'Esc'), ' 取消'))));
      ta.focus();
      slot.scrollIntoView && slot.scrollIntoView({ block: 'nearest' });
    }
    root._open = openComposer;
  }

  // ---- 问题卡
  function renderQuestion(root, it, done) {
    root.appendChild(kicker(it));
    root.appendChild(h('h2', { class: 'd-title' }, it.question));
    root.appendChild(h('div', { class: 'callout', style: 'margin-bottom:16px' }, h('span', { class: 'callout-ico' }, 'i'),
      h('span', null, '这是业务信息，不是授权。你的回答只告诉 AI 怎么继续；之后真要动机器，仍会单独来请你批准。')));
    const opts = h('div', { class: 'options' });
    const options = Array.isArray(it.options) ? it.options : [];
    const submit = h('button', { class: 'btn btn-primary', type: 'button', disabled: true }, '提交回答');
    const otherTa = h('textarea', { rows: '2', maxlength: '2000', placeholder: '用你自己的话回答…', value: inbox.otherText });
    otherTa.addEventListener('input', () => { inbox.otherText = otherTa.value; sync(); });
    otherTa.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submit.click(); } });
    const nodes = [];
    function choose(id) { if (done) return; inbox.answer = id; sync(); if (id === 'other') setTimeout(() => otherTa.focus(), 0); }
    function sync() {
      nodes.forEach(n => n.el.classList.toggle('on', n.id === inbox.answer));
      otherTa.hidden = inbox.answer !== 'other';
      submit.disabled = done || !inbox.answer || (inbox.answer === 'other' && !otherTa.value.trim());
    }
    options.forEach((o, i) => {
      const el = h('button', { class: 'opt', type: 'button', disabled: done, onclick: () => choose(o.id) },
        h('span', { class: 'opt-key' }, i < 9 ? String(i + 1) : '·'),
        h('span', null, h('div', { class: 'opt-label' }, o.label || o.id, h('span', { class: 'opt-id' }, o.id)), o.description ? h('div', { class: 'opt-desc' }, o.description) : null));
      nodes.push({ id: o.id, el });
      opts.appendChild(el);
    });
    const otherEl = h('div', { class: 'opt opt-other', role: 'button', tabindex: '0', onclick: (e) => { if (e.target !== otherTa) choose('other'); } },
      h('span', { class: 'opt-key' }, '0'),
      h('span', null, h('div', { class: 'opt-label' }, '其他'), h('div', { class: 'opt-desc' }, '以上都不对？直接写下你的答案。'), otherTa));
    nodes.push({ id: 'other', el: otherEl });
    opts.appendChild(otherEl);
    root.appendChild(opts);
    root.appendChild(h('div', { class: 'row', style: 'font-size:13px;margin-bottom:8px' },
      h('button', { class: 'link-btn', style: 'font-size:13px;color:var(--info)', type: 'button', onclick: () => openTurn(it.turn_id) }, '查看完整对话 →'),
      h('a', { href: auditHash({ entity: it.turn_id }) }, '这一轮的审计时间线')));
    sync();
    if (done) return;
    submit.addEventListener('click', async () => {
      const id = inbox.answer; if (!id) return;
      const body = id === 'other' ? { option_id: 'other', text: otherTa.value.trim() } : { option_id: id };
      if (id === 'other' && !body.text) return;
      submit.disabled = true; submit.textContent = '提交中…';
      try {
        await api('POST', '/api/v1/turns/' + enc(it.turn_id) + '/questions/' + enc(it.question_id) + '/answer', body);
        const label = id === 'other' ? '其他：' + body.text : ((options.find(o => o.id === id) || {}).label || id);
        markResolved(it, 'go', '已回答', '「' + label.slice(0, 60) + '」，AI 继续工作');
        toast('已回答，AI 会继续这一轮。', 'go');
      } catch (e) {
        if (e.status === 409) { markResolved(it, '', '这个问题已被回答或轮次已结束'); toast(errText(e), 'wait'); }
        else { toast(errText(e), 'stop'); submit.disabled = false; submit.textContent = '提交回答'; }
      }
    });
    root.appendChild(h('div', { class: 'decide' }, h('div', { class: 'decide-bar' }, submit,
      h('span', { class: 'hint' }, '按 ', h('kbd', null, '1'), '–', h('kbd', null, String(Math.min(9, options.length) || 1)), ' 选择，', h('kbd', null, '0'), ' 其他'))));
    root._choose = (n) => { if (n === 0) choose('other'); else if (options[n - 1]) choose(options[n - 1].id); };
    root._submit = () => submit.click();
  }

  // ---- 账号申请卡
  function renderAccount(root, it, done) {
    root.appendChild(kicker(it));
    root.appendChild(h('h2', { class: 'd-title' }, 'AI 申请以 ', h('span', { class: 'mono', style: 'font-size:20px' }, it.username), ' 身份在 ', h('span', { class: 'where' }, it.asset_name || it.asset_id), ' 上执行'));
    root.appendChild(h('div', { class: 'grant' },
      h('div', { class: 'grant-badge' }, (it.username || '?').slice(0, 2).toUpperCase()),
      h('div', null, h('div', { class: 'grant-user' }, it.username), h('div', { class: 'grant-at' }, '@ ' + (it.asset_name || it.asset_id) + '（' + it.asset_id + '）'))));
    if (it.reason) root.appendChild(h('div', { class: 'quote ai', style: 'margin-bottom:18px' }, h('div', { class: 'quote-label' }, 'AI 给出的理由'), h('div', { class: 'quote-body' }, it.reason)));
    root.appendChild(h('div', { class: 'section-title' }, '批准意味着什么'));
    root.appendChild(h('ul', { class: 'scope-list' },
      h('li', null, h('span', { class: 'y' }, '✓'), h('span', null, '本轮对话里，AI 可以选用这个账号来提出或运行命令。')),
      h('li', null, h('span', { class: 'n' }, '✕'), h('span', null, '不批准任何具体命令——命令仍然按「' + (MODE[it.mode] ? MODE[it.mode].name : '当前') + '」模式和名单走，需要时另起审批卡。')),
      h('li', null, h('span', { class: 'n' }, '✕'), h('span', null, '这一轮结束即失效，不会延续到之后的对话。'))));
    root.appendChild(h('div', { class: 'row', style: 'font-size:13px;margin-bottom:8px' },
      h('button', { class: 'link-btn', style: 'font-size:13px;color:var(--info)', type: 'button', onclick: () => openTurn(it.turn_id) }, '查看完整对话 →'),
      h('a', { href: auditHash({ entity: it.turn_id }) }, '这一轮的审计时间线')));
    if (done) return;
    const yes = h('button', { class: 'btn btn-go', type: 'button' }, '允许本轮使用', h('span', { class: 'kbd-hint' }, 'A'));
    const no = h('button', { class: 'btn btn-ghost-stop', type: 'button' }, '不允许', h('span', { class: 'kbd-hint' }, 'R'));
    async function answer(decision) {
      yes.disabled = no.disabled = true;
      try {
        await api('POST', '/api/v1/turns/' + enc(it.turn_id) + '/account-requests/' + enc(it.request_id) + '/answer', { decision, revision: it.revision });
        markResolved(it, decision === 'approve' ? 'go' : 'stop', decision === 'approve' ? '已允许 · 仅本轮有效' : '已拒绝账号申请', null);
        toast(decision === 'approve' ? '已允许 ' + it.username + ' 在本轮使用。' : '已拒绝。', decision === 'approve' ? 'go' : '');
      } catch (e) {
        if (e.status === 409) { markResolved(it, '', '申请已过期或已被处理'); toast(errText(e) + '已刷新。', 'wait'); }
        else { toast(errText(e), 'stop'); yes.disabled = no.disabled = false; }
      }
    }
    yes.addEventListener('click', () => answer('approve'));
    no.addEventListener('click', () => answer('deny'));
    root.appendChild(h('div', { class: 'decide' }, h('div', { class: 'decide-bar' }, yes, no, h('span', { class: 'hint' }, '只针对这一轮'))));
    root._open = (k) => answer(k === 'approve' ? 'approve' : 'deny');
  }

  function markResolved(it, tone, text, sub) {
    const k = keyOf(it);
    inbox.resolved[k] = { tone, text, sub, item: it };
    inbox.items = inbox.items.filter(x => keyOf(x) !== k);
    updateBadge();
    renderQueue();
    renderDetail(true);
    refreshInbox();
  }

  // 键盘
  document.addEventListener('keydown', (e) => {
    if (view !== 'inbox' || $('#shell').hidden) return;
    if (e.key === 'Escape' && $('#drawer').classList.contains('open')) return;
    const t = e.target;
    if (e.key === 'Enter' && t && (t.tagName === 'BUTTON' || t.tagName === 'A' || t.getAttribute('role') === 'button' || t.tagName === 'LI')) return;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if ($('#drawer').classList.contains('open')) return;
    const list = visibleItems();
    const idx = list.findIndex(it => keyOf(it) === inbox.selected);
    const inner = $('#detail .detail-inner');
    const it = inbox.selected ? findItem(inbox.selected) : null;
    if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); const n = list[Math.min(list.length - 1, idx + 1)] || list[0]; if (n) selectItem(keyOf(n)); }
    else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); const n = list[Math.max(0, idx - 1)] || list[0]; if (n) selectItem(keyOf(n)); }
    else if (!it || !inner) return;
    else if ((e.key === 'a' || e.key === 'A') && inner._open) { e.preventDefault(); inner._open('approve'); }
    else if ((e.key === 'r' || e.key === 'R') && inner._open) { e.preventDefault(); inner._open('reject'); }
    else if (e.key === 'c' || e.key === 'C') { e.preventDefault(); openTurn(it.turn_id); }
    else if (/^[0-9]$/.test(e.key) && inner._choose) { e.preventDefault(); inner._choose(Number(e.key)); }
    else if (e.key === 'Enter' && inner._submit) { e.preventDefault(); inner._submit(); }
  });

  // ================================================================ 抽屉
  let drawerReturnFocus = null;
  function openDrawer(title, sub, body) {
    drawerReturnFocus = document.activeElement;
    const t = clear($('#drawer-title'));
    t.appendChild(document.createTextNode(title));
    if (sub) t.appendChild(h('span', { class: 'sub' }, sub));
    const b = clear($('#drawer-body'));
    add(b, body);
    $('#drawer-scrim').hidden = false;
    const d = $('#drawer');
    d.classList.add('open'); d.setAttribute('aria-hidden', 'false');
    $('#drawer-close').focus();
    return b;
  }
  function closeDrawer() {
    const d = $('#drawer'); if (!d) return;
    d.classList.remove('open'); d.setAttribute('aria-hidden', 'true');
    $('#drawer-scrim').hidden = true;
    if (drawerReturnFocus && drawerReturnFocus.focus) drawerReturnFocus.focus();
  }
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#drawer').classList.contains('open')) { e.preventDefault(); closeDrawer(); } });

  function jsonView(value) {
    const pre = h('pre', { class: 'json' });
    (function walk(v, indent, parent) {
      const sp = '  '.repeat(indent);
      if (v === null) { parent.appendChild(h('span', { class: 'b' }, 'null')); return; }
      if (Array.isArray(v)) {
        if (!v.length) { add(parent, '[]'); return; }
        add(parent, '[\n');
        v.forEach((x, i) => { add(parent, sp + '  '); walk(x, indent + 1, parent); add(parent, (i < v.length - 1 ? ',' : '') + '\n'); });
        add(parent, sp + ']'); return;
      }
      if (typeof v === 'object') {
        const ks = Object.keys(v);
        if (!ks.length) { add(parent, '{}'); return; }
        add(parent, '{\n');
        ks.forEach((k, i) => { add(parent, sp + '  '); parent.appendChild(h('span', { class: 'k' }, JSON.stringify(k))); add(parent, ': '); walk(v[k], indent + 1, parent); add(parent, (i < ks.length - 1 ? ',' : '') + '\n'); });
        add(parent, sp + '}'); return;
      }
      if (typeof v === 'string') parent.appendChild(h('span', { class: 's' }, JSON.stringify(v)));
      else if (typeof v === 'number') parent.appendChild(h('span', { class: 'n' }, String(v)));
      else parent.appendChild(h('span', { class: 'b' }, String(v)));
    })(value, 0, pre);
    return pre;
  }
  function tryParse(s) { if (typeof s !== 'string') return s; try { return JSON.parse(s); } catch (_) { return s; } }

  async function openTurn(turnId) {
    if (!turnId) return;
    const body = openDrawer('对话上下文', turnId, h('div', { class: 'skeleton', style: 'height:80px' }));
    try {
      const t = await api('GET', '/api/v1/turns/' + enc(turnId));
      clear(body);
      const kv = h('dl', { class: 'kv' });
      [['状态', t.state], ['模式', t.mode && MODE[t.mode] ? MODE[t.mode].name : t.mode], ['角色', t.role_name || t.role_id], ['开始于', t.created_at != null ? fmtAbs(t.created_at) : null]]
        .forEach(([k, v]) => { if (v != null && v !== '') { kv.appendChild(h('dt', null, k)); kv.appendChild(h('dd', null, String(v))); } });
      body.appendChild(kv);
      if (t.prompt) body.appendChild(h('div', { class: 'drawer-section' }, h('div', { class: 'quote user' }, h('div', { class: 'quote-label' }, '用户原话'), h('div', { class: 'quote-body' }, t.prompt))));
      if (Array.isArray(t.steps) && t.steps.length) body.appendChild(h('div', { class: 'drawer-section' }, h('div', { class: 'section-title' }, '本轮步骤'), execBlock(t.steps, null, null)));
      const msgs = Array.isArray(t.messages) ? t.messages : [];
      const sec = h('div', { class: 'drawer-section' }, h('div', { class: 'section-title' }, '消息', h('span', { class: 'count' }, msgs.length + ' 条')));
      msgs.forEach((m, i) => sec.appendChild(messageView(m, i === msgs.length - 1)));
      if (!msgs.length) sec.appendChild(h('div', { class: 'muted' }, '没有消息记录。'));
      body.appendChild(sec);
      const extra = {};
      ['questions', 'account_requests', 'execution_grants'].forEach(k => { if (t[k] != null && !(Array.isArray(t[k]) && !t[k].length)) extra[k] = t[k]; });
      if (Object.keys(extra).length) body.appendChild(h('details', { class: 'msg drawer-section' }, h('summary', null, h('span', { class: 'msg-role' }, 'meta'), h('span', { class: 'msg-peek' }, '问题、账号申请与执行授权')), h('div', { class: 'msg-body' }, jsonView(extra))));
      body.appendChild(h('details', { class: 'msg' }, h('summary', null, h('span', { class: 'msg-role' }, 'raw'), h('span', { class: 'msg-peek' }, '完整原始响应')), h('div', { class: 'msg-body' }, jsonView(t))));
      body.appendChild(h('p', null, h('a', { href: auditHash({ entity: turnId }), onclick: closeDrawer }, '查看这一轮的审计时间线 →')));
    } catch (e) {
      clear(body).appendChild(h('div', { class: 'callout stop' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, errText(e))));
    }
  }
  function messageView(m, open) {
    const role = m.role || '?';
    const content = typeof m.content === 'string' ? m.content : m.content == null ? '' : JSON.stringify(m.content);
    const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
    let peek = content.replace(/\s+/g, ' ').slice(0, 120);
    if (!peek && calls.length) peek = '调用 ' + calls.map(c => c.function && c.function.name).filter(Boolean).join('、');
    const label = { system: '系统', user: '用户', assistant: 'AI', tool: '工具结果' }[role] || role;
    const body = h('div', { class: 'msg-body' });
    if (content) {
      if (role === 'tool') { const p = tryParse(content); body.appendChild(typeof p === 'string' ? h('div', { class: 'msg-text' }, p) : jsonView(p)); }
      else body.appendChild(h('div', { class: 'msg-text' }, content));
    }
    calls.forEach(c => {
      const fn = c.function || {};
      body.appendChild(h('div', { class: 'toolcall' }, h('div', { class: 'toolcall-name' }, '→ ' + (fn.name || '?')), jsonView(tryParse(fn.arguments))));
    });
    return h('details', { class: 'msg r-' + role, open: !!open }, h('summary', null, h('span', { class: 'msg-role' }, label), h('span', { class: 'msg-peek' }, peek || '（空）')), body);
  }

  // ================================================================ 名单与模式
  const lists = { all: [], bindings: {}, draft: {}, sel: null, tab: 'rules', compare: null, extra: {} };
  async function mountLists(r) {
    const main = clear($('#main'));
    const page = h('div', { class: 'page' });
    main.appendChild(page);
    page.appendChild(h('div', { class: 'page-head' }, h('div', null,
      h('h1', { class: 'page-title' }, '名单与模式'),
      h('p', { class: 'page-sub' }, '模式决定 AI 能走多远，名单决定具体能做什么。名单是不可变快照：要改就 fork 一个新版本，再把模式绑过去——正在执行的任务始终用它开始时的那一份。'))));
    const modesBox = h('div', { class: 'modes', id: 'modes' }, MODES.map(() => h('div', { class: 'card mode' }, h('div', { class: 'skeleton', style: 'width:50%' }), h('div', { class: 'skeleton' }), h('div', { class: 'skeleton' }))));
    page.appendChild(modesBox);
    const layout = h('div', { class: 'lists-layout' }, h('div', { class: 'card catalog', id: 'catalog' }), h('div', { class: 'card list-view', id: 'list-view' }));
    page.appendChild(h('div', { class: 'section-title' }, '名单快照'));
    page.appendChild(layout);
    const tagsBox = h('div', { id: 'tags-box', style: 'margin-top:34px' });
    page.appendChild(tagsBox);
    try {
      const [all, b] = await Promise.all([api('GET', '/api/v1/name-lists'), api('GET', '/api/v1/name-list-bindings'), loadAssets().catch(() => [])]);
      lists.all = Array.isArray(all) ? all : [];
      lists.bindings = b || {};
      lists.draft = {};
      if (r.arg) lists.sel = r.arg;
      if (!lists.sel || !getList(lists.sel)) lists.sel = lists.all[0] ? lists.all[0].id : null;
      if (r.arg && !getList(r.arg)) { try { lists.extra[r.arg] = await api('GET', '/api/v1/name-lists/' + enc(r.arg)); lists.sel = r.arg; } catch (_) { } }
      renderModes(); renderCatalog(); renderListView(); renderTags();
    } catch (e) {
      clear(page).appendChild(h('div', { class: 'callout stop' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, '加载失败：' + errText(e))));
    }
  }
  function getList(id) { return lists.all.find(l => l.id === id) || lists.extra[id] || null; }
  async function fetchList(id) {
    const l = getList(id); if (l) return l;
    const got = await api('GET', '/api/v1/name-lists/' + enc(id));
    lists.extra[id] = got; return got;
  }
  function listLabel(l) { return l ? (l.name || l.id) + ' · v' + l.version : '—'; }
  function boundModes(id) {
    const out = [];
    MODES.forEach(m => { const b = lists.bindings[m]; if (b) SLOTS.forEach(s => { if (b[s] === id) out.push(m); }); });
    return out;
  }

  function renderModes() {
    const box = clear($('#modes'));
    MODES.forEach(mode => {
      const cur = lists.bindings[mode] || null;
      const draft = lists.draft[mode] || (lists.draft[mode] = cur ? Object.assign({}, cur) : { readonly_list: null, write_list: null, deny_list: null });
      const changed = SLOTS.some(s => (draft[s] || null) !== ((cur && cur[s]) || null));
      const meter = h('span', { class: 'mode-meter', title: '自主程度' }, [1, 2, 3].map(i => h('i', { class: i <= MODE[mode].level ? 'on' : '' })));
      const card = h('div', { class: 'card mode' },
        h('div', { class: 'mode-head' }, h('div', null, h('div', { class: 'mode-name' }, MODE[mode].name), h('div', { class: 'mode-code' }, mode)), meter),
        h('div', { class: 'mode-desc' }, MODE[mode].desc));
      SLOTS.forEach(slot => {
        const sel = h('select', { 'aria-label': MODE[mode].name + ' · ' + SLOT[slot].label + '名单', title: SLOT[slot].hint });
        sel.appendChild(h('option', { value: '' }, '（不绑定）'));
        const cands = lists.all.filter(l => l.kind === SLOT[slot].kind);
        const curId = draft[slot];
        if (curId && !cands.some(l => l.id === curId)) sel.appendChild(h('option', { value: curId }, '已退役 · ' + curId));
        cands.forEach(l => sel.appendChild(h('option', { value: l.id }, listLabel(l))));
        sel.value = curId || '';
        sel.addEventListener('change', () => { draft[slot] = sel.value || null; renderModes(); });
        const isChanged = (draft[slot] || null) !== ((cur && cur[slot]) || null);
        card.appendChild(h('div', { class: 'bind-row' + (isChanged ? ' changed' : '') }, h('label', { title: SLOT[slot].hint }, SLOT[slot].label), sel));
      });
      const foot = h('div', { class: 'mode-foot' });
      if (changed) {
        const save = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, '保存绑定');
        save.addEventListener('click', async () => {
          save.disabled = true;
          try {
            lists.bindings = await api('PUT', '/api/v1/name-list-bindings/' + mode, { readonly_list: draft.readonly_list || null, write_list: draft.write_list || null, deny_list: draft.deny_list || null }) || {};
            lists.draft[mode] = null;
            toast(MODE[mode].name + '的绑定已更新。只影响之后开始的新任务。', 'go');
            renderModes(); renderCatalog();
          } catch (e) { toast(errText(e), 'stop'); save.disabled = false; }
        });
        foot.appendChild(save);
        foot.appendChild(h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { lists.draft[mode] = null; renderModes(); } }, '还原'));
        foot.appendChild(h('span', { class: 'hint' }, '仅影响新任务'));
      } else {
        foot.appendChild(h('span', { class: 'default-note' }, cur ? '进行中的任务冻结着各自开始时的名单' : '尚未显式绑定 · 使用平台默认'));
      }
      card.appendChild(foot);
      box.appendChild(card);
    });
  }

  function renderCatalog() {
    const box = clear($('#catalog'));
    const byId = {}; lists.all.forEach(l => { byId[l.id] = l; });
    ['readonly', 'write', 'deny'].forEach(kind => {
      const group = lists.all.filter(l => l.kind === kind);
      box.appendChild(h('div', { class: 'catalog-group' }, h('span', null, LIST_KIND[kind].name), h('span', null, group.length)));
      if (!group.length) { box.appendChild(h('div', { class: 'faint', style: 'padding:4px 10px 10px;font-size:12px' }, '暂无')); return; }
      // 以 parent 关系组织成谱系树
      const roots = group.filter(l => !l.parent_id || !byId[l.parent_id] || byId[l.parent_id].kind !== kind);
      const kids = id => group.filter(l => l.parent_id === id).sort((a, b) => a.version - b.version);
      const walk = (l, depth) => {
        const bm = boundModes(l.id);
        box.appendChild(h('button', { class: 'li' + (l.id === lists.sel ? ' sel' : ''), type: 'button', style: depth ? 'padding-left:' + (10 + depth * 14) + 'px' : null, onclick: () => { lists.sel = l.id; lists.tab = 'rules'; lists.compare = null; history.replaceState(null, '', '#/lists/' + enc(l.id)); renderCatalog(); renderListView(); } },
          h('div', { class: 'li-name' }, depth ? h('span', { class: 'tree' }, '└') : null, h('span', null, l.name || l.id), h('span', { class: 'v' }, 'v' + l.version)),
          h('div', { class: 'li-meta' }, h('span', null, (l.rules || []).length + ' 条规则'), bm.map(m => h('span', { class: 'bound-tag' }, MODE[m].short)))));
        kids(l.id).forEach(c => walk(c, depth + 1));
      };
      roots.sort((a, b) => (a.created_at || 0) - (b.created_at || 0)).forEach(l => walk(l, 0));
    });
  }

  function hostsView(hosts) {
    if (!Array.isArray(hosts) || !hosts.length) return h('span', { class: 'all' }, '全部主机');
    return h('span', { class: 'hosts' }, hosts.map(x => {
      let title = null, n = null;
      if (cache.assets && String(x).indexOf('tag:') === 0) {
        const tag = String(x).slice(4);
        const hit = cache.assets.filter(a => Array.isArray(a.tags) && a.tags.indexOf(tag) >= 0);
        n = hit.length; title = hit.length ? '当前匹配：' + hit.map(a => a.name || a.id).join('、') : '当前没有主机带这个标签';
      }
      return h('span', { class: 'pill mono' + (n === 0 ? ' stop' : ''), title }, x, n != null ? h('span', { class: 'faint' }, ' · ' + n + ' 台') : null);
    }));
  }
  function rulesTable(rules) {
    if (!rules || !rules.length) return h('div', { class: 'empty' }, '这张名单没有规则。');
    return h('table', { class: 't' },
      h('thead', null, h('tr', null, h('th', null, '规则 ID'), h('th', null, '动作'), h('th', null, '生效主机'), h('th', null, '说明'))),
      h('tbody', null, rules.map(r => h('tr', null, h('td', { class: 'mono' }, r.rule_id), h('td', { class: 'mono' }, r.action), h('td', null, hostsView(r.hosts)), h('td', { class: 'muted' }, r.note || '')))));
  }

  async function renderListView() {
    const box = clear($('#list-view'));
    const l = getList(lists.sel);
    if (!l) { box.appendChild(h('div', { class: 'empty' }, '没有名单。')); return; }
    const k = LIST_KIND[l.kind] || { name: l.kind, cls: '' };
    const bm = boundModes(l.id);
    const parent = l.parent_id ? getList(l.parent_id) : null;
    box.appendChild(h('div', { class: 'lv-head' },
      h('div', null,
        h('h2', { class: 'lv-title' }, l.name || l.id, h('span', { class: 'pill mono' }, 'v' + l.version), h('span', { class: 'pill ' + k.cls }, k.name), l.retired_at ? h('span', { class: 'pill stop' }, '已退役') : null),
        h('div', { class: 'lv-meta' },
          h('span', { class: 'mono' }, l.id),
          h('span', null, '创建于 ', timeEl(l.created_at)),
          l.parent_id ? h('span', null, 'fork 自 ', h('button', { class: 'link-btn', style: 'font-size:12.5px;color:var(--info)', type: 'button', onclick: async () => { try { await fetchList(l.parent_id); lists.sel = l.parent_id; lists.tab = 'rules'; renderCatalog(); renderListView(); } catch (e) { toast(errText(e), 'stop'); } } }, parent ? listLabel(parent) : l.parent_id)) : h('span', null, '根版本'),
          bm.length ? h('span', null, '绑定于 ', bm.map(m => MODE[m].name).join('、')) : h('span', { class: 'faint' }, '未被任何模式绑定'))),
      h('div', { class: 'row' },
        h('a', { class: 'btn btn-sm', href: auditHash({ entity: l.id }) }, '审计事件'),
        h('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => { lists.tab = 'fork'; renderListView(); } }, 'Fork 新版本'))));
    const tabs = [['rules', '规则 ' + (l.rules || []).length], ['diff', '对比']];
    tabs.push(['fork', 'Fork']);
    box.appendChild(h('div', { class: 'lv-tabs' }, tabs.map(([id, label]) => h('button', { class: 'tab' + (lists.tab === id ? ' on' : ''), type: 'button', onclick: () => { lists.tab = id; renderListView(); } }, label))));
    const pane = h('div');
    box.appendChild(pane);
    if (lists.tab === 'rules') pane.appendChild(rulesTable(l.rules));
    else if (lists.tab === 'diff') renderDiffPane(pane, l);
    else renderForkPane(pane, l);
  }

  function diffRules(a, b) {
    const A = {}, B = {};
    (a || []).forEach(r => { A[r.rule_id] = r; });
    (b || []).forEach(r => { B[r.rule_id] = r; });
    const ids = Array.from(new Set(Object.keys(A).concat(Object.keys(B)))).sort();
    const same = (x, y) => x.action === y.action && JSON.stringify(x.hosts || []) === JSON.stringify(y.hosts || []) && (x.note || '') === (y.note || '');
    return ids.map(id => {
      if (!A[id]) return { t: 'add', id, now: B[id] };
      if (!B[id]) return { t: 'del', id, was: A[id] };
      return { t: same(A[id], B[id]) ? 'same' : 'mod', id, was: A[id], now: B[id] };
    });
  }
  function diffTable(rows, showSame) {
    const cnt = { add: 0, del: 0, mod: 0, same: 0 };
    rows.forEach(r => cnt[r.t]++);
    const hostsTxt = hs => (hs && hs.length ? hs.join(', ') : '全部主机');
    const cell = (r, f, fmt) => {
      fmt = fmt || (x => x || '');
      if (r.t === 'mod' && fmt(r.was[f]) !== fmt(r.now[f])) return h('td', null, h('span', { class: 'was mono' }, fmt(r.was[f]) || '（空）'), h('span', { class: 'now mono' }, fmt(r.now[f]) || '（空）'));
      const src = r.now || r.was;
      return h('td', { class: f === 'note' ? 'muted' : 'mono' }, fmt(src[f]));
    };
    const visible = rows.filter(r => showSame || r.t !== 'same');
    const sign = { add: '+', del: '−', mod: '~', same: ' ' };
    return h('div', null,
      h('div', { class: 'diff-summary' },
        h('span', { class: 'pill go' }, '+' + cnt.add + ' 新增'), h('span', { class: 'pill stop' }, '−' + cnt.del + ' 移除'), h('span', { class: 'pill wait' }, '~' + cnt.mod + ' 修改'), h('span', { class: 'pill' }, cnt.same + ' 未变')),
      visible.length ? h('table', { class: 't diff' },
        h('thead', null, h('tr', null, h('th', null, ''), h('th', null, '规则 ID'), h('th', null, '动作'), h('th', null, '生效主机'), h('th', null, '说明'))),
        h('tbody', null, visible.map(r => h('tr', { class: r.t }, h('td', { class: 'sign' }, sign[r.t]), h('td', { class: 'mono' }, r.id), cell(r, 'action'), cell(r, 'hosts', hostsTxt), cell(r, 'note')))))
        : h('div', { class: 'empty' }, '两份规则完全一致。'));
  }
  async function renderDiffPane(pane, l) {
    const others = lists.all.filter(x => x.id !== l.id && x.kind === l.kind);
    if (!lists.compare) lists.compare = l.parent_id || (others[0] && others[0].id) || null;
    const sel = h('select', { style: 'max-width:340px' });
    if (l.parent_id && !others.some(o => o.id === l.parent_id)) sel.appendChild(h('option', { value: l.parent_id }, '父版本 · ' + listLabel(getList(l.parent_id)) ));
    others.forEach(o => sel.appendChild(h('option', { value: o.id }, (o.id === l.parent_id ? '父版本 · ' : '') + listLabel(o))));
    if (!sel.options.length) { pane.appendChild(h('div', { class: 'empty' }, '没有可对比的同类名单。')); return; }
    sel.value = lists.compare;
    const showSame = h('input', { type: 'checkbox' });
    const out = h('div');
    pane.appendChild(h('div', { class: 'row', style: 'margin-bottom:14px' }, h('span', { class: 'muted' }, '以'), sel, h('span', { class: 'muted' }, '为基准，看本版本 v' + l.version + ' 改了什么'), h('span', { class: 'spacer' }), h('label', { class: 'check' }, showSame, '显示未变规则')));
    pane.appendChild(out);
    const draw = async () => {
      lists.compare = sel.value;
      clear(out).appendChild(h('div', { class: 'skeleton', style: 'height:60px' }));
      try { const base = await fetchList(sel.value); clear(out).appendChild(diffTable(diffRules(base.rules, l.rules), showSame.checked)); }
      catch (e) { clear(out).appendChild(h('div', { class: 'callout stop' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, errText(e)))); }
    };
    sel.addEventListener('change', draw);
    showSame.addEventListener('change', draw);
    draw();
  }

  function renderForkPane(pane, src) {
    const rows = (src.rules || []).map(r => ({ rule_id: r.rule_id, action: r.action, hosts: (r.hosts || []).join(', '), note: r.note || '' }));
    const name = h('input', { type: 'text', maxlength: '200', placeholder: src.name || '' });
    const edBox = h('div', { class: 'rules-ed' });
    const preview = h('div');
    const submit = h('button', { class: 'btn btn-primary', type: 'button' }, '创建新快照');
    const parseHosts = s => s.split(/[,，\s]+/).map(x => x.trim()).filter(Boolean);
    const toRules = () => rows.map(r => ({ rule_id: r.rule_id.trim(), action: r.action.trim(), hosts: parseHosts(r.hosts), note: r.note.trim() }));
    function validate() {
      const rs = toRules(); const seen = {}; const dups = {};
      rs.forEach(r => { if (seen[r.rule_id]) dups[r.rule_id] = 1; seen[r.rule_id] = 1; });
      const bad = rs.some(r => !r.rule_id || !r.action) || Object.keys(dups).length > 0;
      return { rs, dups, bad };
    }
    function drawPreview() {
      const v = validate();
      Array.from(edBox.querySelectorAll('.rule-ed[data-i]')).forEach(row => {
        const i = Number(row.dataset.i), r = v.rs[i];
        row.classList.toggle('bad', !!(r && v.dups[r.rule_id]));
        const id = row.querySelector('input'); id.classList.toggle('dup', !!(r && v.dups[r.rule_id]));
      });
      clear(preview).appendChild(diffTable(diffRules(src.rules, v.rs), false));
      submit.disabled = v.bad;
      submit.title = v.bad ? '每条规则都需要规则 ID 和动作，且规则 ID 不可重复' : '';
    }
    function drawRows() {
      clear(edBox);
      edBox.appendChild(h('div', { class: 'rule-ed head' }, h('span', null, '规则 ID'), h('span', null, '动作'), h('span', null, '生效主机'), h('span', null, '说明'), h('span', null, '')));
      rows.forEach((r, i) => {
        const inp = (f, ph, cls) => { const x = h('input', { type: 'text', class: cls || '', value: r[f], placeholder: ph }); x.addEventListener('input', () => { r[f] = x.value; drawPreview(); }); return x; };
        edBox.appendChild(h('div', { class: 'rule-ed', dataset: { i: String(i) } },
          inp('rule_id', 'service.reload', 'mono'), inp('action', 'service.reload', 'mono'), inp('hosts', '留空 = 全部主机', 'mono'), inp('note', '为什么允许'),
          h('button', { class: 'icon-btn', type: 'button', title: '移除这条', 'aria-label': '移除', onclick: () => { rows.splice(i, 1); drawRows(); } }, '✕')));
      });
      edBox.appendChild(h('div', null, h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { rows.push({ rule_id: '', action: '', hosts: '', note: '' }); drawRows(); const ins = edBox.querySelectorAll('.rule-ed[data-i] input'); if (ins.length) ins[ins.length - 4].focus(); } }, '+ 添加规则')));
      drawPreview();
    }
    submit.addEventListener('click', async () => {
      const v = validate(); if (v.bad) return;
      submit.disabled = true; submit.textContent = '创建中…';
      const body = { rules: v.rs };
      if (name.value.trim()) body.name = name.value.trim();
      try {
        const created = await api('POST', '/api/v1/name-lists/' + enc(src.id) + '/fork', body);
        lists.all = await api('GET', '/api/v1/name-lists') || [];
        if (created && created.id) { lists.sel = created.id; lists.extra[created.id] = created; }
        lists.tab = 'diff'; lists.compare = src.id;
        history.replaceState(null, '', '#/lists/' + enc(lists.sel));
        renderModes(); renderCatalog(); renderListView();
        toast('新快照 v' + (created && created.version != null ? created.version : '?') + ' 已创建，尚未绑定——到上方模式卡片里切换绑定后才会生效。', 'go', 7000);
      } catch (e) { toast(errText(e), 'stop'); submit.disabled = false; submit.textContent = '创建新快照'; }
    });
    pane.appendChild(h('div', { class: 'fork-form' },
      h('div', { class: 'callout' }, h('span', { class: 'callout-ico' }, 'i'), h('span', null, '这里编辑的是一份新快照的草稿。原名单 v' + src.version + ' 保持不变；新版本创建后不会自动生效，需要你在模式卡片里绑定。')),
      h('label', { class: 'field' }, h('span', { class: 'field-label' }, '新名单名称（可选，留空沿用原名）'), name),
      h('div', null, h('div', { class: 'field-label' }, '规则'), edBox, h('div', { class: 'field-hint' }, '生效主机：用逗号分隔，例如 tag:prod, tag:web。留空表示全部主机。标签在页面底部「主机标签」里维护。')),
      h('div', null, h('div', { class: 'field-label' }, '相对 v' + src.version + ' 的变化'), preview),
      h('div', { class: 'row' }, submit, h('button', { class: 'btn', type: 'button', onclick: () => { lists.tab = 'rules'; renderListView(); } }, '放弃'))));
    drawRows();
  }

  function healthView(hv) {
    const s = hv && typeof hv === 'object' ? hv.state : hv;
    if (!s) return h('span', { class: 'health unk' }, h('span', { class: 'dot' }), '未知');
    const ok = /^(healthy|ok|online)$/i.test(s);
    const label = { healthy: '在线', offline: '离线' }[s] || s;
    return h('span', { class: 'health ' + (ok ? 'ok' : 'bad') }, h('span', { class: 'dot' }), label);
  }
  function renderTags() {
    const box = clear($('#tags-box'));
    const assets = cache.assets || [];
    box.appendChild(h('div', { class: 'section-title' }, '主机标签', h('span', { class: 'count' }, '规则里的 tag:xxx 会匹配带 xxx 标签的主机')));
    if (!assets.length) { box.appendChild(h('div', { class: 'empty' }, '还没有注册主机。')); return; }
    const tbody = h('tbody');
    assets.forEach(a => {
      const orig = (a.tags || []).join(', ');
      const inp = h('input', { type: 'text', class: 'tag-input', value: orig, placeholder: '例如 prod, web' });
      const save = h('button', { class: 'btn btn-sm btn-primary', type: 'button', hidden: true }, '保存');
      inp.addEventListener('input', () => { save.hidden = inp.value.trim() === orig; });
      inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !save.hidden) save.click(); if (e.key === 'Escape') { inp.value = orig; save.hidden = true; } });
      save.addEventListener('click', async () => {
        save.disabled = true;
        const tags = inp.value.split(/[,，\s]+/).map(x => x.trim().replace(/^tag:/, '')).filter(Boolean);
        try {
          const res = await api('PUT', '/api/v1/assets/' + enc(a.id) + '/tags', { tags });
          a.tags = res && Array.isArray(res.tags) ? res.tags : tags;
          toast('「' + (a.name || a.id) + '」的标签已更新。', 'go');
          renderTags(); if (lists.tab === 'rules') renderListView();
        } catch (e) { toast(errText(e), 'stop'); save.disabled = false; }
      });
      tbody.appendChild(h('tr', null,
        h('td', null, h('div', { style: 'font-weight:600' }, a.name || a.id), h('div', { class: 'faint mono', style: 'font-size:11.5px' }, a.id)),
        h('td', null, healthView(a.health)),
        h('td', { class: 'muted' }, a.connection_type || '—'),
        h('td', { class: 'mono muted', style: 'font-size:12px' }, (a.allowed_users || []).join(', ') || '—'),
        h('td', { style: 'width:34%' }, h('div', { class: 'row', style: 'flex-wrap:nowrap' }, inp, save))));
    });
    box.appendChild(h('div', { class: 'card', style: 'padding:6px 10px' }, h('table', { class: 't tags-table' },
      h('thead', null, h('tr', null, h('th', null, '主机'), h('th', null, '状态'), h('th', null, '连接'), h('th', null, '可用账号'), h('th', null, '标签'))), tbody)));
  }

  // ================================================================ 凭证库
  const vault = { asset: '', dismissed: false, data: null };
  async function mountVault(r) {
    vault.asset = r.q.get('asset') || '';
    vault.dismissed = r.q.get('dismissed') === '1';
    const main = clear($('#main'));
    const page = h('div', { class: 'page', id: 'vault-page' });
    main.appendChild(page);
    page.appendChild(h('div', { class: 'card vault-hero' }, h('div', { id: 'vault-headline' }, h('div', { class: 'skeleton', style: 'width:60%;height:26px' })), h('div', { class: 'vault-lock' }, icon(ICON.lock))));
    page.appendChild(h('div', { id: 'vault-body' }));
    try { await loadAssets(); } catch (_) { }
    loadVault();
  }
  async function loadVault() {
    const qs = new URLSearchParams();
    if (vault.asset) qs.set('asset_id', vault.asset);
    qs.set('include_dismissed', vault.dismissed ? 'true' : 'false');
    try {
      vault.data = await api('GET', '/api/v1/credentials?' + qs.toString());
      renderVault();
    } catch (e) {
      const b = $('#vault-body'); if (b) clear(b).appendChild(h('div', { class: 'callout stop' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, '加载失败：' + errText(e))));
    }
  }
  function vaultHash() {
    const q = new URLSearchParams();
    if (vault.asset) q.set('asset', vault.asset);
    if (vault.dismissed) q.set('dismissed', '1');
    const s = q.toString();
    return '#/vault' + (s ? '?' + s : '');
  }
  function renderVault() {
    const d = vault.data || {};
    const items = Array.isArray(d.items) ? d.items : [];
    const counts = d.counts_by_asset && typeof d.counts_by_asset === 'object' ? d.counts_by_asset : {};
    const hosts = Object.keys(counts).filter(k => counts[k] > 0);
    const total = hosts.reduce((s, k) => s + Number(counts[k] || 0), 0);
    const head = clear($('#vault-headline'));
    if (total) head.appendChild(h('h2', null, '在 ', h('em', null, hosts.length), ' 台主机上发现 ', h('em', null, total), ' 处明文密钥，已原地托管。'));
    else head.appendChild(h('h2', null, '还没有发现明文密钥。'));
    head.appendChild(h('p', null, 'Agent 读到像密钥的内容时，会在那台主机本地加密存下，只把一个句柄（编号、名字、发现位置）报上来。这里是清单，不是保险箱——控制面从来没有拿到过值本身。'));

    const body = clear($('#vault-body'));
    const chips = h('div', { class: 'asset-chips' });
    const chip = (id, label, n) => h('button', { class: 'chip' + (vault.asset === id ? ' on' : ''), type: 'button', onclick: () => { vault.asset = id; history.replaceState(null, '', vaultHash()); loadVault(); } }, label, n != null ? h('span', { class: 'n' }, n) : null);
    chips.appendChild(chip('', '全部主机', total));
    const ids = Array.from(new Set(hosts.concat(vault.asset ? [vault.asset] : [])));
    ids.sort((a, b) => (counts[b] || 0) - (counts[a] || 0)).forEach(id => chips.appendChild(chip(id, assetName(id), counts[id] || 0)));
    const dis = h('input', { type: 'checkbox', checked: vault.dismissed });
    dis.addEventListener('change', () => { vault.dismissed = dis.checked; history.replaceState(null, '', vaultHash()); loadVault(); });
    body.appendChild(h('div', { class: 'vault-bar' }, chips, h('span', { class: 'spacer' }),
      vault.asset ? h('a', { class: 'btn btn-sm', href: auditHash({ entity: vault.asset }) }, '该主机的审计时间线') : null,
      h('label', { class: 'check' }, dis, '显示已否决的误报')));
    body.appendChild(h('div', { class: 'kind-legend' }, Object.keys(CRED_KIND).map(k => h('span', { title: CRED_KIND[k].hint }, h('span', { class: 'pill ' + CRED_KIND[k].cls }, CRED_KIND[k].name), ' ', CRED_KIND[k].hint))));
    if (items.some(i => i.kind === 'connector' && !i.dismissed)) body.appendChild(h('div', { class: 'callout stop', style: 'margin-bottom:12px' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, '有句柄来自无 Agent 的连接器路径：这些值在发现时经过了控制面。建议在源头轮换这些密钥，或给对应主机部署 Agent。')));
    if (!items.length) { body.appendChild(h('div', { class: 'empty' }, vault.asset ? '这台主机上没有记录。' : '暂无凭证句柄。')); return; }
    const sorted = items.slice().sort((a, b) => (b.last_seen_at || b.created_at || 0) - (a.last_seen_at || a.created_at || 0));
    body.appendChild(h('div', { class: 'card', style: 'padding:4px 10px' }, h('table', { class: 't' },
      h('thead', null, h('tr', null, h('th', null, '句柄'), h('th', null, '主机'), h('th', null, '发现位置'), h('th', null, '类型'), h('th', null, '最近一次看到'))),
      h('tbody', null, sorted.map(c => h('tr', { class: 'click' + (c.dismissed ? ' dismissed' : ''), tabindex: '0', onclick: () => openCred(c), onkeydown: e => { if (e.key === 'Enter') openCred(c); } },
        h('td', null, h('div', { class: 'cred-name' }, c.name || c.id), c.note ? h('div', { class: 'cred-note' }, c.note) : null, h('div', { class: 'faint mono', style: 'font-size:11px' }, c.id)),
        h('td', null, assetName(c.asset_id)),
        h('td', null, h('span', { class: 'src' }, c.source || '—')),
        h('td', null, h('span', { class: 'pill ' + ((CRED_KIND[c.kind] || {}).cls || ''), title: (CRED_KIND[c.kind] || {}).hint }, (CRED_KIND[c.kind] || {}).name || c.kind), c.dismissed ? h('div', { class: 'faint', style: 'font-size:11.5px;margin-top:3px' }, '已否决') : null),
        h('td', { class: 'nowrap muted' }, timeEl(c.last_seen_at || c.created_at))))))));
  }

  async function openCred(c) {
    const body = openDrawer(c.name || c.id, c.id + ' · ' + assetName(c.asset_id), []);
    const kd = CRED_KIND[c.kind] || { name: c.kind, cls: '', hint: '' };
    const kv = h('dl', { class: 'kv' });
    [['主机', assetName(c.asset_id) + '（' + c.asset_id + '）'], ['发现位置', h('span', { class: 'src' }, c.source || '—')], ['类型', h('span', null, h('span', { class: 'pill ' + kd.cls }, kd.name), ' ', h('span', { class: 'muted', style: 'font-size:12px' }, kd.hint))],
      ['首次发现', fmtAbs(c.created_at)], ['最近看到', fmtAbs(c.last_seen_at)], ['状态', c.dismissed ? '已否决为误报' + (c.dismissed_reason ? '：' + c.dismissed_reason : '') : '托管中']]
      .forEach(([k, v]) => { kv.appendChild(h('dt', null, k)); kv.appendChild(h('dd', null, v)); });
    body.appendChild(h('div', { class: 'callout', style: 'margin-bottom:16px' }, h('span', { class: 'callout-ico' }, icon('<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>')),
      h('span', null, '值只存在于这台主机本地的加密存储里。发现位置、类型和编号是证据，不可修改；你可以给它起个好认的名字、写备注，或在确认不是密钥时否决它。')));
    body.appendChild(kv);

    // 标注
    const nm = h('input', { type: 'text', value: c.name || '', maxlength: '200' });
    const nt = h('textarea', { rows: '2', maxlength: '2000', value: c.note || '', placeholder: '例如：支付网关回调签名密钥，负责人 张三' });
    const saveBtn = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, '保存标注');
    saveBtn.addEventListener('click', async () => {
      const patch = {};
      if (nm.value.trim() !== (c.name || '')) patch.name = nm.value.trim();
      if (nt.value.trim() !== (c.note || '')) patch.note = nt.value.trim();
      if (!Object.keys(patch).length) { toast('没有改动。'); return; }
      saveBtn.disabled = true;
      try {
        const u = await api('PUT', '/api/v1/assets/' + enc(c.asset_id) + '/credentials/' + enc(c.id), patch);
        Object.assign(c, u || patch);
        toast('标注已保存。', 'go');
        loadVault(); loadHist();
      } catch (e) { toast(errText(e), 'stop'); }
      saveBtn.disabled = false;
    });
    body.appendChild(h('div', { class: 'drawer-section' }, h('div', { class: 'section-title' }, '标注'),
      h('div', { style: 'display:grid;gap:10px' }, h('label', { class: 'field' }, h('span', { class: 'field-label' }, '名字'), nm), h('label', { class: 'field' }, h('span', { class: 'field-label' }, '备注'), nt), h('div', null, saveBtn))));

    // 否决
    if (!c.dismissed) {
      const why = h('input', { type: 'text', maxlength: '500', placeholder: '例如：这是 git 提交哈希，不是密钥' });
      const dBtn = h('button', { class: 'btn btn-sm btn-ghost-stop', type: 'button' }, '否决为误报');
      dBtn.addEventListener('click', async () => {
        const reason = why.value.trim();
        if (!reason) { why.focus(); toast('请写下否决理由，它会进入审计记录。', 'wait'); return; }
        dBtn.disabled = true;
        try {
          await api('POST', '/api/v1/assets/' + enc(c.asset_id) + '/credentials/' + enc(c.id) + '/dismiss', { reason });
          toast('已否决。之后在这个位置不会再上报。', 'go');
          closeDrawer(); loadVault();
        } catch (e) { toast(errText(e), 'stop'); dBtn.disabled = false; }
      });
      body.appendChild(h('div', { class: 'drawer-section' }, h('div', { class: 'section-title' }, '这不是密钥？'),
        h('div', { class: 'muted', style: 'font-size:13px;margin-bottom:8px' }, '片段匹配可能把哈希、随机 ID 当成密钥。否决后同一处不再上报。'),
        h('div', { class: 'row', style: 'flex-wrap:nowrap' }, why, dBtn)));
    }

    const hist = h('div', { class: 'drawer-section' }, h('div', { class: 'section-title' }, '历史'), h('div', { class: 'skeleton' }));
    body.appendChild(hist);
    body.appendChild(h('p', null, h('a', { href: auditHash({ entity: c.asset_id, prefix: 'credential.' }), onclick: closeDrawer }, '这台主机的凭证审计事件 →')));
    async function loadHist() {
      try {
        const evs = await api('GET', '/api/v1/assets/' + enc(c.asset_id) + '/credentials/' + enc(c.id) + '/history');
        const ul = h('ul', { class: 'hist' });
        const name = { discovered: '发现', annotated: '标注', dismissed: '否决' };
        (Array.isArray(evs) ? evs : []).forEach(ev => {
          const det = tryParse(ev.detail);
          ul.appendChild(h('li', { class: 'e-' + ev.event },
            h('div', { class: 'hist-top' }, h('b', null, name[ev.event] || ev.event), h('span', { class: 'pill outline actor' }, ev.actor || '—'), h('span', { class: 'muted', title: fmtAbs(ev.created_at) }, fmtAgo(ev.created_at))),
            det && !(typeof det === 'object' && !Object.keys(det).length) ? h('div', { class: 'hist-detail' }, typeof det === 'object' ? jsonView(det) : h('span', { class: 'muted' }, String(det))) : null));
        });
        clear(hist).appendChild(h('div', { class: 'section-title' }, '历史'));
        hist.appendChild(ul.childNodes.length ? ul : h('div', { class: 'muted' }, '没有历史记录。'));
      } catch (e) { clear(hist).appendChild(h('div', { class: 'muted' }, '历史加载失败：' + errText(e))); }
    }
    loadHist();
  }

  // ================================================================ 审计
  const audit = { entity: '', prefix: '', actor: '', events: [], done: false, loading: false, open: {} };
  function auditHash(o) {
    const q = new URLSearchParams();
    if (o.entity) q.set('entity', o.entity);
    if (o.prefix) q.set('prefix', o.prefix);
    if (o.actor) q.set('actor', o.actor);
    const s = q.toString();
    return '#/audit' + (s ? '?' + s : '');
  }
  const PREFIXES = [['', '全部'], ['task.', '任务'], ['change.', '变更'], ['question.', '问题'], ['authority.', '授权'], ['credential.', '凭证'], ['name_list.', '名单']];
  function mountAudit(r) {
    audit.entity = r.q.get('entity') || '';
    audit.prefix = r.q.get('prefix') || '';
    audit.actor = r.q.get('actor') || '';
    audit.events = []; audit.done = false; audit.open = {};
    const main = clear($('#main'));
    const page = h('div', { class: 'page' });
    main.appendChild(page);
    page.appendChild(h('div', { class: 'page-head' }, h('div', null, h('h1', { class: 'page-title' }, '审计时间线'), h('p', { class: 'page-sub' }, '平台上发生的每一件事，按时间从新到旧。点任意事件展开细节，点实体 ID 只看它的来龙去脉。'))));
    const ent = h('input', { type: 'search', class: 'mono', placeholder: '实体 ID：task / turn / asset / 名单 id，精确匹配', value: audit.entity });
    const act = h('input', { type: 'search', class: 'mono', placeholder: '执行者：admin、policy-engine、agent:web1…', value: audit.actor });
    const apply = () => go(auditHash({ entity: ent.value.trim(), prefix: audit.prefix, actor: act.value.trim() }));
    [ent, act].forEach(i => i.addEventListener('keydown', e => { if (e.key === 'Enter') apply(); }));
    page.appendChild(h('div', { class: 'card audit-bar' },
      h('div', { class: 'row' }, PREFIXES.map(([p, label]) => h('button', { class: 'chip' + (audit.prefix === p ? ' on' : ''), type: 'button', onclick: () => go(auditHash({ entity: audit.entity, prefix: p, actor: audit.actor })) }, label, p ? h('span', { class: 'n mono' }, p) : null))),
      h('div', { class: 'inputs' }, ent, act, h('button', { class: 'btn', type: 'button', onclick: apply }, '筛选'))));
    if (audit.entity || audit.actor) {
      page.appendChild(h('div', { class: 'focus-banner' }, h('span', null, '只看'),
        audit.entity ? h('span', { class: 'pill info mono' }, audit.entity) : null,
        audit.actor ? h('span', { class: 'pill violet mono' }, '执行者 ' + audit.actor) : null,
        h('span', { class: 'spacer' }), h('a', { href: auditHash({ prefix: audit.prefix }) }, '清除聚焦')));
    }
    page.appendChild(h('div', { id: 'tl' }));
    page.appendChild(h('div', { class: 'more', id: 'tl-more' }));
    loadAuditPage();
  }
  function auditQuery(extra) {
    const q = new URLSearchParams();
    q.set('limit', '100');
    if (audit.entity) q.set('entity_id', audit.entity);
    if (audit.prefix) q.set('event_prefix', audit.prefix);
    if (audit.actor) q.set('actor', audit.actor);
    Object.keys(extra).forEach(k => q.set(k, extra[k]));
    return '/api/v1/audit?' + q.toString();
  }
  async function loadAuditPage() {
    if (audit.loading || audit.done) return;
    audit.loading = true;
    const more = $('#tl-more');
    if (more) clear(more).appendChild(h('span', { class: 'muted' }, '加载中…'));
    try {
      const before = audit.events.length ? audit.events[audit.events.length - 1].seq : MAX_SEQ;
      const page = await api('GET', auditQuery({ before: String(before) }));
      const arr = Array.isArray(page) ? page : [];
      audit.events = audit.events.concat(arr);
      if (arr.length < 100) audit.done = true;
      renderTimeline();
    } catch (e) {
      const tl = $('#tl'); if (tl) clear(tl).appendChild(h('div', { class: 'callout stop' }, h('span', { class: 'callout-ico' }, '!'), h('span', null, '加载失败：' + errText(e))));
    } finally { audit.loading = false; renderMore(); }
  }
  async function pollAuditNew() {
    if (audit.loading) return;
    const top = audit.events.length ? audit.events[0].seq : 0;
    try {
      const arr = await api('GET', auditQuery({ after: String(top) }));
      if (Array.isArray(arr) && arr.length) {
        arr.reverse().forEach(e => { e._fresh = true; });
        audit.events = arr.concat(audit.events);
        renderTimeline();
      }
    } catch (_) { }
  }
  function renderMore() {
    const more = $('#tl-more'); if (!more) return;
    clear(more);
    if (audit.done) more.appendChild(h('span', { class: 'faint' }, audit.events.length ? '— 已到最早的记录 —' : ''));
    else more.appendChild(h('button', { class: 'btn', type: 'button', onclick: loadAuditPage }, '加载更早的事件'));
  }
  function summarize(details) {
    if (!details || typeof details !== 'object') return details == null ? '' : String(details);
    return Object.keys(details).slice(0, 4).map(k => {
      let v = details[k];
      v = typeof v === 'string' ? v : JSON.stringify(v);
      if (v && v.length > 48) v = v.slice(0, 48) + '…';
      return k + '=' + v;
    }).join('  ');
  }
  function renderTimeline() {
    const tl = clear($('#tl')); if (!tl) return;
    if (!audit.events.length) { tl.appendChild(h('div', { class: 'empty' }, audit.entity || audit.prefix || audit.actor ? '这个范围内没有审计事件。' : '还没有任何审计事件。')); return; }
    let day = null, ul = null;
    audit.events.forEach(ev => {
      const d = fmtDay(ev.created_at);
      if (d !== day) { day = d; tl.appendChild(h('div', { class: 'day' }, d)); ul = h('ol', { class: 'tl' }); tl.appendChild(ul); }
      const open = !!audit.open[ev.seq];
      const human = EVENT_HUMAN[ev.event];
      const li = h('li', { class: 'ev c-' + eventTone(ev.event) + (open ? ' open' : ''), style: ev._fresh ? 'animation:rise .3s ease-out' : null });
      const entLink = ev.entity_id ? h('span', { class: 'ent', title: '只看这个实体', onclick: (e) => { e.stopPropagation(); go(auditHash({ entity: ev.entity_id })); } }, ev.entity_id) : null;
      const actorLink = ev.actor ? h('span', { class: 'ent', title: '只看这个执行者', onclick: (e) => { e.stopPropagation(); go(auditHash({ actor: ev.actor, prefix: audit.prefix })); } }, ev.actor) : null;
      const body = h('div', { class: 'ev-body', tabindex: '0', onclick: () => { audit.open[ev.seq] = !audit.open[ev.seq]; renderTimeline(); }, onkeydown: e => { if (e.key === 'Enter') { audit.open[ev.seq] = !audit.open[ev.seq]; renderTimeline(); } } },
        h('div', { class: 'ev-line' }, h('span', { class: 'ev-name' }, ev.event), human ? h('span', { class: 'ev-human' }, human) : null),
        h('div', { class: 'ev-sub' }, actorLink ? h('span', null, '由 ', actorLink) : null, entLink ? h('span', null, '对象 ', entLink) : null, h('span', { class: 'faint' }, '#' + ev.seq)),
        h('div', { class: 'ev-summary' }, summarize(ev.details)),
        open ? jsonView(ev.details) : null);
      li.appendChild(h('div', { class: 'ev-time', title: fmtAbs(ev.created_at) }, fmtClock(ev.created_at)));
      li.appendChild(h('div', { class: 'ev-rail' }));
      li.appendChild(body);
      ul.appendChild(li);
      delete ev._fresh;
    });
  }

  // ================================================================ 启动
  initGate();
  if (getToken()) {
    // 网络暂不可达时保留令牌并进入（侧栏会显示连接状态），只有 401/403 才回到闸门。
    tryToken(getToken()).then(unlock).catch(e => { if (e.status === 401 || e.status === 403) lock('令牌已失效，请重新输入。'); else unlock(); });
  } else lock();
})();
