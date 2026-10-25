/* Governance console.
 *
 * The page has one job: when the AI stops and waits for a human, show that
 * human exactly what is being asked, on which host, as which account, and what
 * bytes will run -- then let them settle it without leaving the screen.
 *
 * Two rules shaped almost every function below:
 *
 * 1. Never invent state. If a request failed, say it failed. If a reveal did not
 *    arrive, say it did not arrive. A console that guesses is worse than one
 *    that admits ignorance, because the operator will act on the guess.
 * 2. Every action is idempotent-safe against a concurrent operator: a 409 is a
 *    normal outcome (someone else answered first), not an error to apologise
 *    for. It triggers a refresh and moves on.
 */
'use strict';

/* ── constants ───────────────────────────────────────────────────────────── */

const POLL_MS = 5000;
const TOKEN_KEY = 'aiops.governance.token';

const MODE_TEXT = {
  readonly: '只读排查：AI 只能调用受控的只读工具，看不到命令执行能力。',
  confirm: '提议变更：AI 提出变更集，由你批准后才执行。',
  direct: '白名单内自动执行：命中写名单的动作直接执行，其余仍需你批准。',
};

const ACTOR_TEXT = {
  admin: '管理员',
  service: '平台服务',
  'model-tool': '模型工具',
  'policy-engine': '策略引擎',
};

const EVENT_TONE = {
  'task.rejected': 'bad', 'task.failed': 'bad', 'change.rejected': 'bad',
  'credential.dismissed': 'warn', 'authority.denied': 'warn',
  'task.approved': 'ok', 'task.succeeded': 'ok', 'change.applied': 'ok',
  'credential.discovered': 'info', 'name_list.forked': 'info',
};

/* ── state ───────────────────────────────────────────────────────────────── */

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || '',
  view: 'inbox',
  inbox: null,
  inboxError: null,
  inboxLoading: false,
  lists: null,
  bindings: null,
  listsError: null,
  creds: null,
  credsError: null,
  credFilter: '',
  audit: null,
  auditError: null,
  auditFilter: { event_prefix: '', entity_id: '' },
  pollTimer: null,
  lastOk: 0,
  /* Per-card transient UI state, keyed by a stable card id. Kept out of the
     polled data so a refresh does not wipe a half-typed rejection reason. */
  draft: new Map(),
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ── small helpers ───────────────────────────────────────────────────────── */

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fmtAgo(seconds) {
  if (!seconds) return '—';
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 60) return `${Math.floor(delta)} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

function fmtTime(seconds) {
  if (!seconds) return '—';
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function toast(message, kind = '') {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast ${kind}`;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { node.hidden = true; }, 3400);
}

/* ── api ─────────────────────────────────────────────────────────────────── */

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = { Authorization: `Bearer ${state.token}` };
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, {
    method, headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401) {
    lock('凭据被服务端拒绝，请重新输入 Token。');
    throw new ApiError(401, 'unauthorized');
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }

  if (!response.ok) {
    const detail = payload && typeof payload === 'object'
      ? (payload.detail || payload.message || JSON.stringify(payload))
      : (payload || response.statusText);
    throw new ApiError(response.status, String(detail));
  }
  return payload;
}

/* ── gate / lock ─────────────────────────────────────────────────────────── */

function unlock(token) {
  state.token = token;
  sessionStorage.setItem(TOKEN_KEY, token);
  $('#gate').hidden = true;
  $('#shell').hidden = false;
  switchView(state.view === 'inbox' ? 'inbox' : state.view);
  startPolling();
}

function lock(reason = '') {
  stopPolling();
  state.token = '';
  state.inbox = null;
  sessionStorage.removeItem(TOKEN_KEY);
  $('#shell').hidden = true;
  $('#gate').hidden = false;
  const error = $('#gate-error');
  if (reason) { error.textContent = reason; error.hidden = false; }
  else error.hidden = true;
  $('#token').value = '';
  $('#token').focus();
}

$('#gate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const token = $('#token').value.trim();
  const error = $('#gate-error');
  error.hidden = true;

  if (token.length < 32) {
    error.textContent = '管理员 Token 至少 32 个字符。';
    error.hidden = false;
    return;
  }

  state.token = token;
  try {
    await api('/api/v1/inbox');
    unlock(token);
  } catch (failure) {
    state.token = '';
    if (failure.status === 401) {
      error.textContent = 'Token 无效或已轮换，请确认后重试。';
    } else {
      error.textContent = `无法连接服务：${failure.detail}`;
    }
    error.hidden = false;
  }
});

$('#lock').addEventListener('click', () => lock());

/* ── polling ─────────────────────────────────────────────────────────────── */

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshCurrent({ quiet: true });
  }, POLL_MS);
  refreshCurrent();
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.token) refreshCurrent({ quiet: true });
});

function renderSync(status, note = '') {
  const node = $('#sync');
  if (status === 'ok') {
    node.className = 'sync';
    node.textContent = `已同步 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}${note}`;
  } else if (status === 'stale') {
    node.className = 'sync stale';
    node.textContent = `上次成功 ${fmtAgo(state.lastOk)}${note}`;
  } else {
    node.className = 'sync failed';
    node.textContent = `同步失败${note}`;
  }
}

/* ── routing ─────────────────────────────────────────────────────────────── */

function switchView(view) {
  state.view = view;
  for (const tab of $$('.tab')) {
    tab.setAttribute('aria-selected', String(tab.dataset.view === view));
  }
  for (const name of ['inbox', 'lists', 'creds', 'audit']) {
    $(`#view-${name}`).hidden = name !== view;
  }
  refreshCurrent();
}

for (const tab of $$('.tab')) {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
}

$('#refresh').addEventListener('click', () => refreshCurrent({ force: true }));

function refreshCurrent({ quiet = false, force = false } = {}) {
  if (!state.token) return;
  if (state.view === 'inbox') return loadInbox({ quiet, force });
  if (state.view === 'lists') return loadLists({ quiet, force });
  if (state.view === 'creds') return loadCreds({ quiet, force });
  if (state.view === 'audit') return loadAudit({ quiet, force });
}

/* ── inbox ───────────────────────────────────────────────────────────────── */

async function loadInbox({ quiet = false } = {}) {
  if (!quiet) {
    state.inboxLoading = true;
    if (!state.inbox) renderInbox();
  }
  try {
    state.inbox = await api('/api/v1/inbox');
    state.inboxError = null;
    state.inboxLoading = false;
    state.lastOk = Date.now() / 1000;
    renderSync('ok');
    renderInbox();
  } catch (failure) {
    if (failure.status === 401) return;
    state.inboxLoading = false;
    state.inboxError = failure.detail;
    renderSync(state.inbox ? 'stale' : 'failed');
    /* A failed poll must not blank the list: the operator may be mid-decision
       on a card, and losing it would be worse than showing slightly old data
       with a visible warning. */
    if (!quiet || !state.inbox) renderInbox();
  }
}

function allCards(inbox) {
  if (!inbox) return [];
  return [
    ...(inbox.approvals || []),
    ...(inbox.questions || []),
    ...(inbox.account_requests || []),
    ...(inbox.sensitive_hits || []),
  ].sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
}

function cardKey(card) {
  if (card.kind === 'approval') return `task:${card.task_id}`;
  if (card.kind === 'question') return `q:${card.question_id}`;
  if (card.kind === 'account_request') return `acct:${card.request_id}`;
  return `hit:${card.hit_id}`;
}

function renderInbox() {
  const root = $('#view-inbox');
  root.replaceChildren();

  const inbox = state.inbox;
  const cards = allCards(inbox);
  const total = inbox ? (inbox.total ?? cards.length) : 0;

  const badge = $('#tab-badge');
  if (total > 0) { badge.textContent = String(total); badge.hidden = false; }
  else badge.hidden = true;

  root.append(el('div', { class: 'sec-head' },
    el('div', { class: 'sec-title' }, el('h1', { text: '收件箱' })),
    el('span', { class: 'sec-sub', text: 'AI 停下来等你决定的事' })));

  if (state.inboxError) {
    root.append(el('div', { class: 'banner banner-bad' },
      el('span', { text: `读取失败：${state.inboxError}` }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm', onclick: () => loadInbox() }, '重试')));
  }

  if (state.inboxLoading && !inbox) {
    root.append(el('div', { class: 'skeleton' }), el('div', { class: 'skeleton' }));
    return;
  }

  if (!inbox) return;

  if (cards.length === 0) {
    root.append(el('div', { class: 'empty' },
      el('div', { class: 'empty-mark', text: '✓' }),
      el('h3', { text: '没有待处理的事' }),
      el('p', { text: 'AI 现在没有在等你的决定。这里会自动出现审批、提问、账号申请和敏感解锁请求——出现时你不需要刷新页面。' })));
    return;
  }

  /* Counts by kind: a mixed list of four decision types needs a summary or the
     operator has to read every card to know what is in front of them. */
  const counts = {
    approval: (inbox.approvals || []).length,
    question: (inbox.questions || []).length,
    account_request: (inbox.account_requests || []).length,
    sensitive_hit: (inbox.sensitive_hits || []).length,
  };
  const labels = { approval: '审批', question: '提问', account_request: '账号申请', sensitive_hit: '敏感解锁' };
  const chips = Object.entries(counts).filter(([, n]) => n > 0)
    .map(([kind, n]) => el('span', { class: 'count-chip', dataset: { kind } },
      el('span', { class: 'n', text: String(n) }),
      el('span', { text: labels[kind] })));
  if (chips.length) root.append(el('div', { class: 'count-row' }, ...chips));

  for (const card of cards) {
    root.append(renderCard(card));
  }
}

function renderCard(card) {
  if (card.kind === 'approval') return renderApproval(card);
  if (card.kind === 'question') return renderQuestion(card);
  if (card.kind === 'account_request') return renderAccountRequest(card);
  return renderSensitiveHit(card);
}

/* ── approval card ───────────────────────────────────────────────────────── */

function renderApproval(card) {
  const key = cardKey(card);
  const draft = draftOf(key);

  const node = el('article', { class: 'card', dataset: { kind: 'approval' } });

  node.append(el('div', { class: 'card-head' },
    el('span', { class: 'kind-tag', text: '审批' }),
    el('span', { class: 'target' },
      card.asset_name || card.asset_id,
      el('span', { class: 'as', text: ` · 以 ${card.run_as || '未知账号'} 执行` })),
    el('span', { class: 'card-meta' },
      el('span', { text: roleLabel(card) }),
      el('span', { text: fmtAgo(card.created_at) }))));

  const body = el('div', { class: 'card-body' });

  /* The reason first, in the AI's own words. The operator is deciding whether
     the *intent* makes sense before deciding whether the *bytes* are right. */
  if (card.reason) {
    body.append(el('div', { class: 'reason' },
      el('span', { class: 'reason-label', text: 'AI 的理由' }),
      el('span', { text: card.reason })));
  }

  if (card.prompt) {
    body.append(el('div', { class: 'prompt-quote' },
      el('b', { text: '触发这一轮的原始请求' }),
      card.prompt));
  }

  body.append(renderSteps(card));

  const facts = el('dl', { class: 'facts' },
    fact('模式', modeName(card.mode)),
    fact('主机', card.asset_name || card.asset_id),
    fact('执行账号', card.run_as || '—'),
    card.caller ? fact('来源', card.caller) : null);
  body.append(facts);

  if (card.payload_sha256) body.append(renderDigest(card.payload_sha256));

  node.append(body);

  /* Actions. Reject is not a secondary button hidden in a menu: refusing a
     change is a first-class outcome and its reason goes back to the model. */
  const foot = el('div', { class: 'card-foot' });
  foot.append(
    el('span', { class: 'atomic-note', text: '整组一起执行或一起拒绝，不能只批准其中一步。' }),
    el('span', { class: 'spacer' }),
    el('button', {
      class: 'btn', onclick: () => openTranscript(card.turn_id), title: '查看触发这一轮的完整对话',
    }, '查看对话'),
    el('button', {
      class: 'btn btn-danger', disabled: draft.busy, onclick: () => toggleReject(key),
    }, draft.rejecting ? '取消拒绝' : '拒绝'),
    el('button', {
      class: 'btn btn-primary', disabled: draft.busy,
      onclick: () => approveTask(key, card.task_id),
    }, draft.busy ? '处理中…' : '批准执行'));
  node.append(foot);

  if (draft.rejecting) node.append(renderRejectForm(key, card));
  return node;
}

function renderSteps(card) {
  const steps = Array.isArray(card.steps) ? card.steps : [];

  /* Legacy task: predates the argv-only protocol, so all we have is a string
     that would have gone through a shell. Flagged, not hidden -- an operator
     approving one deserves to know it is the weaker kind. */
  if (steps.length === 0 && card.command) {
    return el('pre', { class: 'exec exec-legacy', text: card.command });
  }

  if (steps.length === 0) {
    return el('div', { class: 'banner banner-warn', text: '这个任务没有可显示的执行内容，请先查看对话再决定。' });
  }

  const pre = el('pre', { class: 'exec' });
  steps.forEach((step, index) => {
    if (steps.length > 1) {
      pre.append(el('span', { class: 'stepno', text: String(index + 1) }));
    }
    pre.append(el('span', { class: 'prg', text: step.program || '' }));
    for (const arg of (step.args || [])) {
      pre.append(el('span', { class: 'sep', text: ' ' }));
      pre.append(el('span', {
        class: String(arg).startsWith('-') ? 'flag' : 'arg',
        text: String(arg),
      }));
    }
    if (index < steps.length - 1) pre.append(document.createTextNode('\n'));
  });
  return pre;
}

function renderDigest(sha) {
  const node = el('div', { class: 'digest' });
  const code = el('code', { text: sha.slice(0, 20) + '…', title: sha });
  node.append(
    el('span', { text: '执行端校验' }),
    code,
    el('button', {
      class: 'expand',
      onclick: (event) => {
        const short = sha.slice(0, 20) + '…';
        const full = sha;
        const showing = code.textContent === full;
        code.textContent = showing ? short : full;
        event.target.textContent = showing ? '展开' : '收起';
      },
    }, '展开'));
  return node;
}

function renderRejectForm(key, card) {
  const draft = draftOf(key);
  const textarea = el('textarea', {
    placeholder: '告诉 AI 为什么不行。它会读到这段话并据此改方案——写具体原因比写“不同意”有用得多。',
    oninput: (event) => { draft.reason = event.target.value; },
  });
  textarea.value = draft.reason || '';

  const form = el('div', { class: 'reject-form' });
  form.append(
    textarea,
    el('div', { class: 'reject-hint' },
      el('b', { text: '这段话会原样发给 AI' }),
      el('span', { text: '——不是只写进日志。' })),
    el('div', { class: 'reject-acts' },
      el('button', { class: 'btn btn-sm', onclick: () => toggleReject(key) }, '取消'),
      el('button', {
        class: 'btn btn-sm btn-danger',
        disabled: draft.busy,
        onclick: () => rejectTask(key, card.task_id),
      }, draft.busy ? '提交中…' : '确认拒绝')));
  setTimeout(() => textarea.focus(), 0);
  return form;
}

function draftOf(key) {
  if (!state.draft.has(key)) {
    state.draft.set(key, { rejecting: false, busy: false, reason: '', revealing: false, reveal: null, error: null, answer: null, other: '' });
  }
  return state.draft.get(key);
}

function toggleReject(key) {
  const draft = draftOf(key);
  draft.rejecting = !draft.rejecting;
  renderInbox();
}

async function approveTask(key, taskId) {
  const draft = draftOf(key);
  if (draft.busy) return;
  draft.busy = true;
  renderInbox();
  try {
    await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/approve`, { method: 'POST', body: {} });
    state.draft.delete(key);
    toast('已批准，任务进入执行队列。', 'ok');
    await loadInbox();
  } catch (failure) {
    draft.busy = false;
    if (failure.status === 409) {
      toast('这件事已经被处理了（可能在别的标签页）。列表已刷新。', 'bad');
      state.draft.delete(key);
      await loadInbox();
    } else {
      toast(`批准失败：${failure.detail}`, 'bad');
      renderInbox();
    }
  }
}

async function rejectTask(key, taskId) {
  const draft = draftOf(key);
  if (draft.busy) return;
  const reason = (draft.reason || '').trim();
  if (!reason) {
    toast('请写一句拒绝理由——它会发给 AI，空着 AI 只能猜。', 'bad');
    return;
  }
  draft.busy = true;
  renderInbox();
  try {
    await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/reject`, {
      method: 'POST', body: { reason },
    });
    state.draft.delete(key);
    toast('已拒绝，理由已回给 AI。', 'ok');
    await loadInbox();
  } catch (failure) {
    draft.busy = false;
    if (failure.status === 409) {
      toast('这件事已经被处理了。列表已刷新。', 'bad');
      state.draft.delete(key);
      await loadInbox();
    } else {
      toast(`拒绝失败：${failure.detail}`, 'bad');
      renderInbox();
    }
  }
}

/* ── question card ───────────────────────────────────────────────────────── */

function renderQuestion(card) {
  const key = cardKey(card);
  const draft = draftOf(key);
  const options = Array.isArray(card.options) ? card.options : [];

  const node = el('article', { class: 'card', dataset: { kind: 'question' } });
  node.append(el('div', { class: 'card-head' },
    el('span', { class: 'kind-tag', text: '提问' }),
    el('span', { class: 'target', text: card.question || 'AI 需要你确认一件事' }),
    el('span', { class: 'card-meta' },
      el('span', { text: roleLabel(card) }),
      el('span', { text: fmtAgo(card.created_at) }))));

  const body = el('div', { class: 'card-body' });

  if (options.length) {
    const list = el('div', { class: 'options' });
    options.forEach((option, index) => {
      const pressed = draft.answer === option.id;
      list.append(el('button', {
        class: 'opt', type: 'button',
        'aria-pressed': String(pressed),
        onclick: () => {
          draft.answer = option.id;
          draft.other = '';
          renderInbox();
        },
      },
        el('span', { class: 'opt-key', text: String(index + 1) }),
        el('span', { class: 'opt-main' },
          el('span', { class: 'opt-label', text: option.label || option.id }),
          option.description ? el('span', { class: 'opt-desc', text: option.description }) : null)));
    });
    body.append(list);
  }

  /* "Other" is always offered: a question with a fixed list is a question the
     AI could not answer itself, so the real answer may be off-list. */
  const otherPressed = draft.answer === 'other';
  body.append(el('button', {
    class: 'opt opt-other', type: 'button', 'aria-pressed': String(otherPressed),
    onclick: () => { draft.answer = 'other'; renderInbox(); },
  },
    el('span', { class: 'opt-key', text: '✎' }),
    el('span', { class: 'opt-main' },
      el('span', { class: 'opt-label', text: '其他（自己填）' }))));

  if (otherPressed) {
    const input = el('input', {
      class: 'input other-input', type: 'text',
      placeholder: '直接回答 AI 的问题',
      oninput: (event) => { draft.other = event.target.value; },
    });
    input.value = draft.other || '';
    body.append(input);
    setTimeout(() => input.focus(), 0);
  }

  node.append(body);

  const disabled = !draft.answer || (draft.answer === 'other' && !(draft.other || '').trim()) || draft.busy;
  node.append(el('div', { class: 'card-foot' },
    el('span', { class: 'foot-note', text: '这是业务问题，不是授权。回答后 AI 继续这一轮。' }),
    el('span', { class: 'spacer' }),
    el('button', {
      class: 'btn btn-primary', disabled,
      onclick: () => answerQuestion(key, card),
    }, draft.busy ? '提交中…' : '回答')));

  return node;
}

async function answerQuestion(key, card) {
  const draft = draftOf(key);
  if (draft.busy) return;
  const payload = draft.answer === 'other'
    ? { option_id: 'other', text: (draft.other || '').trim() }
    : { option_id: draft.answer };

  draft.busy = true;
  renderInbox();
  try {
    await api(`/api/v1/turns/${encodeURIComponent(card.turn_id)}/questions/${encodeURIComponent(card.question_id)}/answer`,
      { method: 'POST', body: payload });
    state.draft.delete(key);
    toast('已回答。', 'ok');
    await loadInbox();
  } catch (failure) {
    draft.busy = false;
    if (failure.status === 409) {
      toast('这个问题已经答过了，或轮次已结束。', 'bad');
      state.draft.delete(key);
      await loadInbox();
    } else {
      toast(`回答失败：${failure.detail}`, 'bad');
      renderInbox();
    }
  }
}

/* ── account request card ────────────────────────────────────────────────── */

function renderAccountRequest(card) {
  const key = cardKey(card);
  const draft = draftOf(key);

  const node = el('article', { class: 'card', dataset: { kind: 'account_request' } });
  node.append(el('div', { class: 'card-head' },
    el('span', { class: 'kind-tag', text: '账号申请' }),
    el('span', { class: 'target' },
      card.asset_name || card.asset_id,
      el('span', { class: 'as', text: ` · 申请使用 ${card.username}` })),
    el('span', { class: 'card-meta' },
      el('span', { text: roleLabel(card) }),
      el('span', { text: fmtAgo(card.created_at) }))));

  const body = el('div', { class: 'card-body' });
  if (card.reason) {
    body.append(el('div', { class: 'reason' },
      el('span', { class: 'reason-label', text: 'AI 的理由' }),
      el('span', { text: card.reason })));
  }
  body.append(el('div', { class: 'scope-note' },
    el('b', { text: '批准的范围：' }),
    `仅在「${roleLabel(card)}」这一轮对话里，允许 AI 以 ${card.username} 的身份在这台主机上执行。`,
    el('span', {
      class: 'scope-why',
      text: '这不是批准任何命令。命令另有审批流程，那一步你还得单独点。',
    })));
  node.append(body);

  node.append(el('div', { class: 'card-foot' },
    el('span', { class: 'foot-note', text: '只在本轮有效，轮次结束后自动失效。' }),
    el('span', { class: 'spacer' }),
    el('button', {
      class: 'btn btn-danger', disabled: draft.busy,
      onclick: () => answerAccount(key, card, 'deny'),
    }, '不允许'),
    el('button', {
      class: 'btn btn-primary', disabled: draft.busy,
      onclick: () => answerAccount(key, card, 'approve'),
    }, draft.busy ? '提交中…' : '允许本轮使用')));

  return node;
}

async function answerAccount(key, card, decision) {
  const draft = draftOf(key);
  if (draft.busy) return;
  draft.busy = true;
  renderInbox();
  try {
    /* revision must be echoed exactly: the server uses it to reject an answer
       to a request that was withdrawn or superseded while the card was shown. */
    await api(`/api/v1/turns/${encodeURIComponent(card.turn_id)}/account-requests/${encodeURIComponent(card.request_id)}/answer`,
      { method: 'POST', body: { decision, revision: card.revision } });
    state.draft.delete(key);
    toast(decision === 'approve' ? '已允许本轮使用该账号。' : '已拒绝该账号申请。', 'ok');
    await loadInbox();
  } catch (failure) {
    draft.busy = false;
    if (failure.status === 409) {
      toast('这个申请已过期或被处理。', 'bad');
      state.draft.delete(key);
      await loadInbox();
    } else {
      toast(`提交失败：${failure.detail}`, 'bad');
      renderInbox();
    }
  }
}

/* ── sensitive hit card ──────────────────────────────────────────────────── */

function renderSensitiveHit(card) {
  const key = cardKey(card);
  const draft = draftOf(key);

  const node = el('article', { class: 'card', dataset: { kind: 'sensitive_hit' } });
  node.append(el('div', { class: 'card-head' },
    el('span', { class: 'kind-tag', text: '敏感解锁' }),
    el('span', { class: 'target', text: card.asset_name || card.asset_id }),
    el('span', { class: 'card-meta' },
      el('span', { text: fmtAgo(card.created_at) }))));

  const body = el('div', { class: 'card-body' });

  body.append(el('div', { class: 'secret-path' },
    el('span', { text: '路径' }),
    el('code', { text: card.path || '—' })));

  if (Array.isArray(card.rule_ids) && card.rule_ids.length) {
    body.append(el('div', { class: 'secret-rules' },
      ...card.rule_ids.map((id) => el('span', { class: 'chip', text: id }))));
  }

  /* Server-authored scope text, shown verbatim. Rewriting it would be the one
     place where a friendly paraphrase could drop "once" or "only you". */
  body.append(el('div', { class: 'scope-note' },
    el('b', { text: '解锁会做什么：' }),
    card.unlock_scope || '在这台主机上重新读取一次同一路径，结果只回给你。',
    el('span', {
      class: 'scope-why',
      text: `授权 ${Math.round((card.expires_in_seconds || 300) / 60)} 分钟内有效，一次性使用。AI 看不到这段原文，它仍然只拿到掩码。`,
    })));

  if (draft.error) {
    body.append(el('div', { class: 'banner banner-bad', text: draft.error }));
  }

  if (draft.reveal) {
    body.append(renderRevealBox(draft));
  } else if (draft.revealing) {
    body.append(el('div', { class: 'reveal-box' },
      el('div', { class: 'reveal-pending' },
        el('span', { class: 'spin' }),
        el('span', { text: '正在让主机重读这一处…' }))));
  }

  node.append(body);

  const foot = el('div', { class: 'card-foot' });
  foot.append(
    el('span', { class: 'foot-note', text: 'AI 已被告知这里有敏感内容但看不到值。' }),
    el('span', { class: 'spacer' }));

  if (draft.reveal) {
    foot.append(el('button', { class: 'btn', onclick: () => dismissReveal(key) }, '收起'));
  } else if (draft.revealing) {
    foot.append(el('button', { class: 'btn', disabled: true }, '读取中…'));
  } else {
    foot.append(el('button', {
      class: 'btn btn-primary', disabled: draft.busy,
      onclick: () => revealHit(key, card),
    }, '让主机重读并给我看'));
  }
  node.append(foot);

  return node;
}

function renderRevealBox(draft) {
  const box = el('div', { class: 'reveal-box' });
  box.append(el('div', { class: 'reveal-head' },
    el('span', { text: '原文（仅你可见）' }),
    el('span', { class: 'spacer', text: draft.reveal.at ? fmtTime(draft.reveal.at) : '' })));
  box.append(el('pre', { class: 'reveal-body', text: draft.reveal.text || '（空）' }));
  return box;
}

function dismissReveal(key) {
  const draft = draftOf(key);
  draft.reveal = null;
  renderInbox();
}

async function revealHit(key, card) {
  const draft = draftOf(key);
  if (draft.busy || draft.revealing) return;

  draft.busy = true;
  draft.error = null;
  renderInbox();

  try {
    await api(`/api/v1/sensitive-hits/${encodeURIComponent(card.hit_id)}/unlock`, { method: 'POST', body: {} });
  } catch (failure) {
    draft.busy = false;
    draft.error = failure.status === 409
      ? '这个请求已失效或已被处理，列表已刷新。'
      : `解锁失败：${failure.detail}`;
    if (failure.status === 409) { state.draft.delete(key); await loadInbox(); return; }
    renderInbox();
    return;
  }

  draft.busy = false;
  draft.revealing = true;
  renderInbox();

  /* Fetching is a separate step, by design: the unlock dispatches a re-read on
     the host, and the bytes are only available after that task runs. We poll a
     bounded number of times and then say plainly that nothing came back --
     inventing a placeholder here would be the worst possible outcome. */
  const deadline = Date.now() + ((card.expires_in_seconds || 300) * 1000);
  let attempt = 0;

  while (Date.now() < deadline) {
    await sleep(attempt === 0 ? 900 : 1800);
    attempt += 1;
    try {
      const result = await api(`/api/v1/sensitive-hits/${encodeURIComponent(card.hit_id)}/revealed`);
      if (result && result.revealed) {
        draft.revealing = false;
        draft.reveal = { text: result.text || result.content || '', at: Date.now() / 1000 };
        renderInbox();
        return;
      }
    } catch (failure) {
      if (failure.status === 404 || failure.status === 410) break;
      if (failure.status === 401) return;
      /* Transient error: keep trying until the deadline rather than giving up
         on a slow host. */
    }
  }

  draft.revealing = false;
  draft.error = '主机没有返回内容（可能读取失败或超时）。授权仍然保留，可以再试一次。';
  renderInbox();
}

/* ── transcript drawer ───────────────────────────────────────────────────── */

async function openTranscript(turnId) {
  if (!turnId) return;
  openDrawer('对话');
  $('#drawer-body').replaceChildren(el('div', { class: 'reveal-pending' },
    el('span', { class: 'spin' }), el('span', { text: '加载中…' })));

  try {
    const turn = await api(`/api/v1/turns/${encodeURIComponent(turnId)}`);
    const body = $('#drawer-body');
    body.replaceChildren();

    body.append(el('div', { class: 'facts' },
      fact('状态', turn.state || '—'),
      fact('模式', modeName(turn.mode)),
      fact('来源', turn.source || '—'),
      turn.model_steps !== undefined ? fact('模型步数', String(turn.model_steps)) : null));

    if (turn.prompt) {
      body.append(el('div', { class: 'msg msg-user' },
        el('div', { class: 'msg-role', text: '用户' }),
        el('pre', { text: turn.prompt })));
    }

    const messages = Array.isArray(turn.messages) ? turn.messages : [];
    for (const message of messages) body.append(renderMessage(message));

    if (!messages.length && !turn.prompt) {
      body.append(el('div', { class: 'banner banner-info', text: '这一轮还没有对话内容。' }));
    }
  } catch (failure) {
    $('#drawer-body').replaceChildren(
      el('div', { class: 'banner banner-bad', text: `加载失败：${failure.detail}` }));
  }
}

function renderMessage(message) {
  const role = message.role || 'unknown';
  const node = el('div', { class: `msg msg-${role}` });
  node.append(el('div', { class: 'msg-role', text: roleLabel({ role_id: role }) || role }));

  const content = message.content;
  if (typeof content === 'string' && content) {
    node.append(el('pre', { text: content }));
  } else if (content && typeof content === 'object') {
    node.append(el('pre', { text: JSON.stringify(content, null, 2) }));
  }

  for (const call of (message.tool_calls || [])) {
    const fn = call.function || {};
    const box = el('div', { class: 'tool-call' });
    box.append(el('span', { class: 'tc-name', text: fn.name || '?' }));
    let args = fn.arguments;
    try { args = JSON.stringify(JSON.parse(args), null, 2); } catch { /* keep raw */ }
    box.append(document.createTextNode('\n' + args));
    node.append(box);
  }
  return node;
}

function openDrawer(title) {
  $('#drawer-title').textContent = title;
  $('#drawer').hidden = false;
}

function closeDrawer() { $('#drawer').hidden = true; }
$('#drawer-close').addEventListener('click', closeDrawer);
$('#drawer').addEventListener('click', (event) => {
  if (event.target.id === 'drawer') closeDrawer();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('#drawer').hidden) closeDrawer();
});

/* ── name lists ──────────────────────────────────────────────────────────── */

async function loadLists({ quiet = false } = {}) {
  const root = $('#view-lists');
  if (!quiet && !state.lists) {
    root.replaceChildren(el('div', { class: 'skeleton' }));
  }
  try {
    const [lists, bindings] = await Promise.all([
      api('/api/v1/name-lists'),
      api('/api/v1/name-list-bindings'),
    ]);
    state.lists = lists;
    state.bindings = bindings;
    state.listsError = null;
    state.lastOk = Date.now() / 1000;
    renderSync('ok');
    renderLists();
  } catch (failure) {
    if (failure.status === 401) return;
    state.listsError = failure.detail;
    renderSync(state.lists ? 'stale' : 'failed');
    if (!quiet || !state.lists) renderLists();
  }
}

function renderLists() {
  const root = $('#view-lists');
  root.replaceChildren();

  root.append(el('div', { class: 'sec-head' },
    el('div', { class: 'sec-title' }, el('h1', { text: '名单与模式' })),
    el('span', { class: 'sec-sub', text: '每轮对话开始时固定，改绑定只影响之后的新任务' })));

  if (state.listsError) {
    root.append(el('div', { class: 'banner banner-bad' },
      el('span', { text: `读取失败：${state.listsError}` }),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm', onclick: () => loadLists() }, '重试')));
  }

  if (!state.lists) return;

  /* Mode bindings first: "which rulebook is in force" is the question an
     operator actually arrives with, not "what lists exist". */
  const bindings = state.bindings || {};
  const grid = el('div', { class: 'bindings' });
  for (const mode of ['readonly', 'confirm', 'direct']) {
    grid.append(renderModeCard(mode, bindings[mode]));
  }
  root.append(grid);

  const drift = el('div', { class: 'banner banner-warn' },
    el('span', { text: '平台默认名单可能随版本更新，与你的快照不再一致。' }),
    el('span', { class: 'spacer' }),
    el('button', { class: 'btn btn-sm', onclick: checkDrift }, '查看差异'));
  root.append(drift);

  const byId = new Map(state.lists.map((list) => [list.id, list]));
  const table = el('table', { class: 'list-table' },
    el('thead', {},
      el('tr', {},
        el('th', { text: '名单' }),
        el('th', { text: '类型' }),
        el('th', { text: '版本' }),
        el('th', { text: '规则数' }),
        el('th', { text: '父版本' }))),
    el('tbody', {}, ...state.lists.map((list) => el('tr', {
      class: 'clickable',
      onclick: () => openList(list.id),
    },
      el('td', {},
        el('div', { class: 'list-name', text: list.name || list.id }),
        el('div', { class: 'list-id', text: list.id })),
      el('td', {}, el('span', { class: 'chip', text: listKindName(list.kind) })),
      el('td', { text: `v${list.version}` }),
      el('td', { text: String((list.rules || []).length) }),
      el('td', { class: 'small muted', text: list.parent_id ? (byId.get(list.parent_id)?.name || list.parent_id) : '—' })))));

  root.append(table);
  root.append(el('p', { class: 'small muted', text: '名单是不可变快照：想改就 fork 出新版本再绑定。这样“审批时看到的”和“执行时用的”永远是同一份。' }));
}

function renderModeCard(mode, binding) {
  const lists = state.lists || [];
  const options = (kind) => {
    const relevant = lists.filter((list) => list.kind === kind);
    return [
      el('option', { value: '', text: '（平台默认）' }),
      ...relevant.map((list) => el('option', {
        value: list.id, text: `${list.name || list.id} · v${list.version}`,
      })),
    ];
  };

  const card = el('div', { class: 'mode-card', dataset: { mode } });
  card.append(el('h4', {},
    el('span', { class: 'mode-badge', text: mode }),
    el('span', { class: 'mode-title', text: modeName(mode) })));
  /* Fixed height for the description: the three cards must line their shared
     rows up, and a two-line blurb in one card would push its selects down. */
  card.append(el('p', { class: 'mode-desc', text: MODE_TEXT[mode] }));

  const current = binding || {};
  const rows = [
    ['只读名单', 'readonly_list', 'readonly'],
    ['写名单', 'write_list', 'write'],
    ['禁止名单', 'deny_list', 'deny'],
  ];
  const selects = {};
  for (const [label, key, kind] of rows) {
    const select = el('select', {}, ...options(kind));
    select.value = current[key] || '';
    selects[key] = select;
    card.append(el('div', { class: 'mode-row' },
      el('label', { text: label }), select));
  }

  /* Save sits at the bottom right of every card, unconditionally. A control
     that moves between cards makes the operator hunt for it in the one place
     they are about to change something. */
  const foot = el('div', { class: 'mode-foot' });
  const status = el('span', { class: 'mode-status' });
  foot.append(status, el('button', {
    class: 'btn btn-sm',
    onclick: () => saveBinding(mode, selects, status),
  }, '保存绑定'));
  card.append(foot);

  return card;
}

async function saveBinding(mode, selects, status) {
  const body = {};
  for (const [key, select] of Object.entries(selects)) {
    body[key] = select.value || null;
  }
  try {
    /* Impact is fetched before saving, not after: the operator should learn
       which scheduled tasks will re-resolve under the new binding *before*
       they commit, since running turns froze their list already. */
    const impact = await api('/api/v1/name-list-bindings/impact');
    const affected = (impact && impact.scheduled_tasks) || (impact && impact.affected_scheduled_tasks) || [];
    if (Array.isArray(affected) && affected.length) {
      const names = affected.slice(0, 5).map((task) => task.name || task.id).join('、');
      const more = affected.length > 5 ? ` 等 ${affected.length} 个` : '';
      const proceed = window.confirm(
        `改动会影响 ${affected.length} 个定时任务：${names}${more}。\n\n` +
        `定时任务在触发那一刻才解析绑定，所以会按新名单执行。运行中的对话不受影响（它们已固化当时的名单）。\n\n继续保存？`);
      if (!proceed) return;
    }
  } catch {
    /* Impact is advisory. Failing to read it should not block a deliberate
       rebind -- but the operator is told the check did not run. */
    toast('未能读取影响面检查结果，继续保存。', 'bad');
  }

  try {
    state.bindings = await api(`/api/v1/name-list-bindings/${encodeURIComponent(mode)}`,
      { method: 'PUT', body });
    toast('绑定已保存，只影响之后开始的新任务。', 'ok');
    renderLists();
  } catch (failure) {
    toast(`保存失败：${failure.detail}`, 'bad');
    if (status) { status.textContent = '保存失败'; status.className = 'mode-status bad'; }
  }
}

async function checkDrift() {
  try {
    const drift = await api('/api/v1/name-lists/platform-defaults/drift');
    openDrawer('平台默认名单差异');
    const body = $('#drawer-body');
    body.replaceChildren();

    const entries = Array.isArray(drift) ? drift : (drift && drift.drift) || [];
    if (!entries.length) {
      body.append(el('div', { class: 'banner banner-info', text: '你的快照与平台默认一致，没有需要处理的差异。' }));
      return;
    }

    body.append(el('div', { class: 'banner banner-warn', text: '平台默认名单已更新。接受变更会 fork 出新版本再改绑，旧版本保留，审计可回溯。' }));
    for (const entry of entries) {
      body.append(el('pre', { class: 'exec', text: JSON.stringify(entry, null, 2) }));
    }
    body.append(el('p', { class: 'small muted', text: '平台不会自动改绑——自动打安全补丁和自动改权限是两件事，不能捆在一起。' }));
  } catch (failure) {
    toast(`读取差异失败：${failure.detail}`, 'bad');
  }
}

async function openList(listId) {
  openDrawer('名单详情');
  const body = $('#drawer-body');
  body.replaceChildren(el('div', { class: 'reveal-pending' },
    el('span', { class: 'spin' }), el('span', { text: '加载中…' })));

  try {
    const list = await api(`/api/v1/name-lists/${encodeURIComponent(listId)}`);
    body.replaceChildren();
    body.append(el('div', { class: 'facts' },
      fact('ID', list.id),
      fact('类型', listKindName(list.kind)),
      fact('版本', `v${list.version}`)));
    body.append(el('p', { class: 'small muted', text: list.note || '规则命中规则：先按名单过滤，再按主机标签裁决。名单只能收窄，不能放宽。' }));

    const rules = Array.isArray(list.rules) ? list.rules : [];
    if (!rules.length) {
      body.append(el('div', { class: 'banner banner-info', text: '这张名单没有规则。' }));
      return;
    }

    const table = el('table', { class: 'rules' },
      el('thead', {}, el('tr', {},
        el('th', { text: '规则' }),
        el('th', { text: '动作' }),
        el('th', { text: '适用主机' }),
        el('th', { text: '说明' }))),
      el('tbody', {}, ...rules.map((rule) => el('tr', {},
        el('td', { class: 'mono', text: rule.rule_id || '' }),
        el('td', { class: 'mono', text: rule.action || '' }),
        el('td', {},
          (rule.hosts || []).length
            ? el('span', { class: 'rule-hosts' }, ...rule.hosts.map((host) => el('span', { class: 'chip', text: host })))
            : el('span', { class: 'muted small', text: '全部主机' })),
        el('td', { class: 'small muted', text: rule.note || '' })))));

    body.append(table);

    body.append(el('div', { class: 'card-foot', style: 'margin-top:14px;background:transparent;border-top:0;padding:0' },
      el('button', { class: 'btn btn-sm', onclick: () => checkList(listId) }, '检查死规则与冲突'),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm btn-primary', onclick: () => forkList(list) }, 'Fork 新版本')));
  } catch (failure) {
    body.replaceChildren(el('div', { class: 'banner banner-bad', text: `加载失败：${failure.detail}` }));
  }
}

async function checkList(listId) {
  try {
    const result = await api(`/api/v1/name-lists/${encodeURIComponent(listId)}/check`, {
      method: 'POST', body: {},
    });
    const body = $('#drawer-body');
    const dead = (result && result.dead_rules) || [];
    const conflicts = (result && result.conflicts) || [];

    if (!dead.length && !conflicts.length) {
      toast('没有发现死规则或跨名单冲突。', 'ok');
      return;
    }

    const box = el('div', { style: 'margin-top:14px' });
    if (dead.length) {
      box.append(el('div', { class: 'banner banner-warn', text: `${dead.length} 条死规则：被更宽的规则完全覆盖，永远不会被裁定。` }));
      box.append(el('pre', { class: 'exec', text: JSON.stringify(dead, null, 2) }));
    }
    if (conflicts.length) {
      box.append(el('div', { class: 'banner banner-bad', text: `${conflicts.length} 处冲突：同一动作同时被允许和禁止。引擎固定“禁止优先”，所以是“我明明允许了还是被拦”，不是漏放。` }));
      box.append(el('pre', { class: 'exec', text: JSON.stringify(conflicts, null, 2) }));
    }
    body.append(box);
  } catch (failure) {
    toast(`检查失败：${failure.detail}`, 'bad');
  }
}

async function forkList(list) {
  const name = window.prompt('新版本的名字（旧版本会保留）：', `${list.name || list.id} v${list.version + 1}`);
  if (name === null) return;
  try {
    const created = await api(`/api/v1/name-lists/${encodeURIComponent(list.id)}/fork`, {
      method: 'POST', body: { name: name || undefined },
    });
    toast(`已创建新版本 ${created.name || created.id}（v${created.version}）。要生效请到上方改绑定。`, 'ok');
    closeDrawer();
    await loadLists();
  } catch (failure) {
    toast(`Fork 失败：${failure.detail}`, 'bad');
  }
}

/* ── credentials ─────────────────────────────────────────────────────────── */

async function loadCreds({ quiet = false } = {}) {
  if (!quiet && !state.creds) {
    $('#view-creds').replaceChildren(el('div', { class: 'skeleton' }));
  }
  try {
    const query = state.credFilter ? `?asset_id=${encodeURIComponent(state.credFilter)}` : '';
    state.creds = await api(`/api/v1/credentials${query}`);
    state.credsError = null;
    state.lastOk = Date.now() / 1000;
    renderSync('ok');
    renderCreds();
  } catch (failure) {
    if (failure.status === 401) return;
    state.credsError = failure.detail;
    renderSync(state.creds ? 'stale' : 'failed');
    if (!quiet || !state.creds) renderCreds();
  }
}

function renderCreds() {
  const root = $('#view-creds');
  root.replaceChildren();

  root.append(el('div', { class: 'sec-head with-act' },
    el('div', {},
      el('div', { class: 'sec-title' }, el('h1', { text: '凭证库' })),
      el('span', { class: 'sec-sub', text: 'Agent 在主机上发现并托管的密钥位置' })),
    el('span', { class: 'sec-act' },
      el('button', { class: 'btn btn-sm', onclick: () => loadCreds() }, '刷新'))));

  if (state.credsError) {
    root.append(el('div', { class: 'banner banner-bad', text: `读取失败：${state.credsError}` }));
  }
  if (!state.creds) return;

  const items = state.creds.items || [];
  const active = items.filter((item) => !item.dismissed);

  /* Said once, plainly, at the top: this page is an inventory. Repeating it per
     row would read as fine print and stop being read at all. */
  root.append(el('div', { class: 'no-secret-note' },
    el('b', { text: '这里只有位置，没有值。' }),
    el('span', { text: '密钥内容加密保存在对应的受管主机上，控制面的任何接口都取不回明文。这里回答的是“哪些机器上有明文密钥、在哪”，不是“值是什么”。' })));

  const byAsset = state.creds.counts_by_asset || {};
  const assetCount = Object.keys(byAsset).length;
  root.append(el('div', { class: 'cred-summary' },
    el('div', { class: 'stat' },
      el('div', { class: 'stat-num', text: String(active.length) }),
      el('div', { class: 'stat-lbl', text: '个有效句柄' })),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-num', text: String(assetCount) }),
      el('div', { class: 'stat-lbl', text: '台主机' })),
    el('div', { class: 'stat' },
      el('div', { class: 'stat-num', text: String(items.length - active.length) }),
      el('div', { class: 'stat-lbl', text: '已否决的误报' }))));

  if (!items.length) {
    root.append(el('div', { class: 'empty' },
      el('div', { class: 'empty-mark', text: '✓' }),
      el('h3', { text: '还没有发现任何句柄' }),
      el('p', { text: 'Agent 在只读排查时如果读到密钥样式的内容，会把它托管在主机本地，然后在这里列出位置。目前没有这类记录。' })));
    return;
  }

  const grouped = new Map();
  for (const item of items) {
    if (!grouped.has(item.asset_id)) grouped.set(item.asset_id, []);
    grouped.get(item.asset_id).push(item);
  }

  for (const [assetId, rows] of grouped) {
    const section = el('div', { class: 'cred-asset' });
    section.append(el('div', { class: 'cred-asset-head' },
      el('h3', { text: rows[0].asset_name || assetId }),
      el('span', { class: 'small muted', text: `${rows.filter((r) => !r.dismissed).length} 个有效` })));
    for (const row of rows) section.append(renderCredRow(row));
    root.append(section);
  }
}

function renderCredRow(item) {
  const row = el('div', { class: `cred-row${item.dismissed ? ' dismissed' : ''}` });

  row.append(el('span', { class: 'cred-name', text: item.name || item.id }));
  row.append(el('span', { class: 'cred-src', text: item.source || '—', title: item.source || '' }));
  row.append(el('span', { class: 'cred-kind' },
    el('span', { class: 'chip', text: credKindName(item.kind) })));
  if (item.note) row.append(el('span', { class: 'cred-note', text: item.note, title: item.note }));

  const acts = el('span', { class: 'cred-acts' });
  if (!item.dismissed) {
    acts.append(
      el('button', { class: 'btn btn-quiet btn-sm', onclick: () => annotateCred(item) }, '改名'),
      el('button', { class: 'btn btn-quiet btn-sm', onclick: () => dismissCred(item) }, '误报'),
      el('button', { class: 'btn btn-quiet btn-sm', onclick: () => showCredHistory(item) }, '历史'));
  } else {
    acts.append(el('span', { class: 'small faint', text: item.dismissed_reason || '已否决' }));
  }
  row.append(acts);
  return row;
}

async function annotateCred(item) {
  const name = window.prompt('给这个句柄起个好认的名字（填路径或用途，未来的你才看得懂）：', item.name || '');
  if (name === null) return;
  const note = window.prompt('备注（可留空）：', item.note || '');
  if (note === null) return;
  try {
    await api(`/api/v1/assets/${encodeURIComponent(item.asset_id)}/credentials/${encodeURIComponent(item.id)}`,
      { method: 'PUT', body: { name: name || undefined, note } });
    toast('已更新。', 'ok');
    await loadCreds();
  } catch (failure) {
    toast(`更新失败：${failure.detail}`, 'bad');
  }
}

async function dismissCred(item) {
  const reason = window.prompt('为什么这不是密钥？（否决后不再上报）', '不是密钥');
  if (reason === null) return;
  try {
    await api(`/api/v1/assets/${encodeURIComponent(item.asset_id)}/credentials/${encodeURIComponent(item.id)}/dismiss`,
      { method: 'POST', body: { reason: reason || '不是密钥' } });
    toast('已标记为误报。', 'ok');
    await loadCreds();
  } catch (failure) {
    toast(`操作失败：${failure.detail}`, 'bad');
  }
}

async function showCredHistory(item) {
  openDrawer(`${item.name || item.id} · 历史`);
  const body = $('#drawer-body');
  body.replaceChildren(el('div', { class: 'reveal-pending' },
    el('span', { class: 'spin' }), el('span', { text: '加载中…' })));
  try {
    const history = await api(`/api/v1/assets/${encodeURIComponent(item.asset_id)}/credentials/${encodeURIComponent(item.id)}/history`);
    body.replaceChildren();
    if (!history.length) {
      body.append(el('div', { class: 'banner banner-info', text: '没有历史记录。' }));
      return;
    }
    for (const entry of history) {
      body.append(el('div', { class: 'tl-item' },
        el('div', { class: 'tl-head' },
          el('span', { class: 'tl-event', text: entry.event }),
          el('span', { class: 'tl-actor', text: ACTOR_TEXT[entry.actor] || entry.actor || '—' }),
          el('span', { class: 'tl-time', text: fmtTime(entry.created_at) })),
        el('pre', { class: 'exec tl-detail', text: prettyJSON(entry.detail) })));
    }
  } catch (failure) {
    body.replaceChildren(el('div', { class: 'banner banner-bad', text: `加载失败：${failure.detail}` }));
  }
}

/* ── audit ───────────────────────────────────────────────────────────────── */

async function loadAudit({ quiet = false } = {}) {
  if (!quiet && !state.audit) {
    $('#view-audit').replaceChildren(el('div', { class: 'skeleton' }));
  }
  try {
    const params = new URLSearchParams({ limit: '100' });
    if (state.auditFilter.event_prefix) params.set('event_prefix', state.auditFilter.event_prefix);
    if (state.auditFilter.entity_id) params.set('entity_id', state.auditFilter.entity_id);
    if (state.auditCursor) params.set('before', String(state.auditCursor));

    state.audit = await api(`/api/v1/audit?${params}`);
    state.auditError = null;
    state.lastOk = Date.now() / 1000;
    renderSync('ok');
    renderAudit();
  } catch (failure) {
    if (failure.status === 401) return;
    state.auditError = failure.detail;
    renderSync(state.audit ? 'stale' : 'failed');
    if (!quiet || !state.audit) renderAudit();
  }
}

function renderAudit() {
  const root = $('#view-audit');
  root.replaceChildren();

  root.append(el('div', { class: 'sec-head' },
    el('div', { class: 'sec-title' }, el('h1', { text: '审计时间线' })),
    el('span', { class: 'sec-sub', text: '每一步谁做的、做了什么，都在这' })));

  const prefix = el('select', {
    onchange: (event) => { state.auditFilter.event_prefix = event.target.value; state.auditCursor = null; loadAudit(); },
  }, ...[
    ['', '全部事件'],
    ['task.', 'task. 任务'],
    ['change.', 'change. 变更'],
    ['credential.', 'credential. 凭证'],
    ['name_list.', 'name_list. 名单'],
    ['authority.', 'authority. 授权'],
    ['question.', 'question. 提问'],
  ].map(([value, label]) => el('option', { value, text: label })));
  prefix.value = state.auditFilter.event_prefix;

  const entityInput = el('input', {
    type: 'text', placeholder: '按 entity_id 精确筛选（task / turn / asset / list 的 id）',
    onchange: (event) => { state.auditFilter.entity_id = event.target.value.trim(); state.auditCursor = null; loadAudit(); },
  });
  entityInput.value = state.auditFilter.entity_id;

  root.append(el('div', { class: 'audit-filters' },
    prefix, entityInput,
    el('button', { class: 'btn btn-sm', onclick: () => { state.auditCursor = null; loadAudit(); } }, '查询')));

  if (state.auditError) {
    root.append(el('div', { class: 'banner banner-bad', text: `读取失败：${state.auditError}` }));
  }
  if (!state.audit) return;

  const events = Array.isArray(state.audit) ? state.audit : (state.audit.items || []);
  if (!events.length) {
    root.append(el('div', { class: 'empty' },
      el('div', { class: 'empty-mark', text: '·' }),
      el('h3', { text: '没有匹配的事件' }),
      el('p', { text: '换一个筛选条件试试，或清空 entity_id。' })));
    return;
  }

  const timeline = el('div', { class: 'timeline' });
  for (const event of events) {
    const tone = EVENT_TONE[event.event] || '';
    const item = el('div', { class: 'tl-item', dataset: { tone } });
    const head = el('div', { class: 'tl-head' },
      el('span', { class: 'tl-event', text: event.event }),
      el('span', { class: 'tl-actor', text: ACTOR_TEXT[event.actor] || event.actor || '—' }),
      event.entity_id ? el('span', { class: 'tl-entity', text: event.entity_id }) : null,
      el('span', { class: 'tl-time', text: fmtTime(event.created_at) }));
    item.append(head);

    const detail = el('pre', { class: 'exec tl-detail', text: prettyJSON(event.details), hidden: true });
    head.addEventListener('click', () => { detail.hidden = !detail.hidden; });
    item.append(detail);
    timeline.append(item);
  }
  root.append(timeline);

  if (events.length >= 100) {
    root.append(el('div', { style: 'margin-top:16px;text-align:center' },
      el('button', {
        class: 'btn',
        onclick: () => { state.auditCursor = events[events.length - 1].seq; loadAudit(); },
      }, '加载更早的记录')));
  }
}

/* ── tiny shared renderers ───────────────────────────────────────────────── */

function fact(label, value) {
  return el('div', { class: 'fact' },
    el('dt', { text: label }),
    el('dd', { text: value }));
}

function modeName(mode) {
  return { readonly: 'readonly 只读', confirm: 'confirm 需审批', direct: 'direct 白名单自动' }[mode] || mode || '—';
}

function roleLabel(card) {
  if (card.role_name) return card.role_name;
  if (card.role_id) return card.role_id;
  return '';
}

function listKindName(kind) {
  return { readonly: '只读', write: '写', deny: '禁止' }[kind] || kind || '—';
}

function credKindName(kind) {
  return {
    pattern: '正则命中', path: '路径规则', file: '文件', connector: 'connector 通道',
  }[kind] || kind || '—';
}

function prettyJSON(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

/* ── boot ────────────────────────────────────────────────────────────────── */

(function boot() {
  if (state.token) {
    unlock(state.token);
  } else {
    $('#token').focus();
  }
})();
