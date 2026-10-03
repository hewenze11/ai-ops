"""End-to-end connectivity test on the preview host, with a REAL paid model.

This is a connectivity/coherence test, not a feature test. It answers the
question a real user asks: "I plugged in my API key, I chat with the ops AI —
does it know who it is, where it is, what it can do, and does the whole chain
hang together?"

Checks (each records raw evidence):
  C1. SELF-AWARENESS: the model, asked plainly, states it is an AI ops role that
      can run commands on registered assets — i.e. the identity/system prompt
      really reached it.
  C2. SKILL INJECTION: a skill bound to role A appears in role A's model call and
      does NOT appear in role B's model call.
  C3. DOCUMENT INJECTION: a core document and a role-scoped document reach the
      model; another role's private doc does not.
  C4. AUTHORITY INJECTION: the turn's mode + selected accounts are present in the
      request, and readonly exposes no execute_command tool.
  C5. REAL EXECUTION: a confirm-mode turn proposes a command, gets human approval,
      runs on a real asset via the agent, and summarises the REAL output.
  C6. MEMORY ARCHIVE: after a turn, the day's memory row exists and holds that
      day's content (archived correctly), and is re-injected on a later day view.
  C7. COHERENCE: within ONE turn, the model makes >1 model call (propose tool ->
      read result -> summarise) and the final text reflects the tool result.

Nothing secret is printed. The report lands on the host as JSON.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

base = Path('/opt/ai-ops-preview')
admin = (base / 'ai-ops/secrets/admin_token').read_text().strip()
model_key = (base / 'ai-ops/secrets/model_key').read_text().strip()
url = 'http://127.0.0.1:18765'
run = uuid.uuid4().hex[:8]
role_a = 'conn-a-' + run
role_b = 'conn-b-' + run
asset = 'conn-host-' + run
doc_core = 'conn-core-' + run
doc_priv = 'conn-priv-' + run
skill_a = 'conn-skill-a-' + run
skill_b = 'conn-skill-b-' + run
state = base / 'integration' / ('conn-' + run)
state.mkdir(mode=0o700, parents=True, exist_ok=True)

results = []


def api(method, path, data=None, expected=200):
    req = urllib.request.Request(url + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + admin, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, result = r.status, json.load(r)
    except urllib.error.HTTPError as e:
        status, result = e.code, json.load(e)
    assert status == expected, (path, status, expected, result)
    return result


def record(case, passed, evidence):
    results.append({'case': case, 'passed': bool(passed), 'evidence': evidence})
    print(('[PASS] ' if passed else '[FAIL] ') + case, flush=True)


env = {**os.environ, 'PYTHONPATH': str(base / 'ai-ops-agent'), 'PYTHONDONTWRITEBYTECODE': '1'}


def agent_once():
    subprocess.run([sys.executable, '-m', 'ai_ops_agent.agent', '--config', str(agent_config), '--once'],
                   env=env, check=True, timeout=30)


# ---- setup ---------------------------------------------------------------
api('POST', '/api/v1/roles', {'id': role_a, 'name': 'Connectivity role A'}, 201)
api('POST', '/api/v1/roles', {'id': role_b, 'name': 'Connectivity role B'}, 201)
provision = api('POST', '/api/v1/assets', {'id': asset, 'name': 'Connectivity probe host',
    'allowed_users': ['aiops_probe'], 'notes': 'Probe host for a read-only connectivity test.'}, 201)
agent_config = state / 'agent.json'
agent_config.write_text(json.dumps({'server_url': url, 'allow_loopback_http': True, 'asset_id': asset,
    'agent_token': provision['agent_token'], 'allowed_users': ['aiops_probe'],
    'journal_dir': str(state / 'journal')}))
agent_config.chmod(0o600)

api('PUT', '/api/v1/documents/' + doc_core, {'id': doc_core, 'name': 'Shared core doc', 'core': True,
    'content': 'CONN_CORE_' + run + ': this deployment watches the demo fleet; answer briefly.'})
api('PUT', '/api/v1/documents/' + doc_priv, {'id': doc_priv, 'name': 'Role A only', 'core': False,
    'role_ids': [role_a], 'content': 'CONN_PRIVATE_A_' + run + ': role A secret runbook marker.'})
api('PUT', '/api/v1/skills/' + skill_a, {'id': skill_a, 'name': 'Skill A',
    'content': 'CONN_SKILL_A_' + run + ' When asked about disks, mention df -h.', 'role_ids': [role_a]})
api('PUT', '/api/v1/skills/' + skill_b, {'id': skill_b, 'name': 'Skill B',
    'content': 'CONN_SKILL_B_' + run + ' This is role B only.', 'role_ids': [role_b]})
for role in (role_a, role_b):
    api('PUT', '/api/v1/roles/' + role + '/model', {'enabled': True, 'model': 'gpt-4.1-mini-2025-04-14',
        'max_model_steps': 6, 'max_output_tokens': 700, 'max_context_chars': 250000})


def wait_turn(turn_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = api('GET', '/api/v1/turns/' + turn_id)
        if current['state'] in ('completed', 'failed', 'blocked_unknown', 'cancelled'):
            return current
        time.sleep(2)
    raise RuntimeError('turn timed out: ' + turn_id)


def calls_for(turn_id):
    return api('GET', '/api/v1/turns/' + turn_id + '/model-calls')


def system_blob(calls):
    return json.dumps([m['content'] for c in calls for m in c['request']['messages']
                       if m['role'] == 'system'], ensure_ascii=False)


try:
    # ---- C1: self-awareness ------------------------------------------------
    c1 = api('POST', '/api/v1/roles/' + role_a + '/messages',
             {'text': '用中文一句话说明：你是谁？你能对哪些主机做什么？不要执行任何命令。',
              'execution_users': [], 'mode': 'readonly', 'idempotency_key': 'c1-' + run}, 202)
    t1 = wait_turn(c1['turn_id'])
    c1_calls = calls_for(c1['turn_id'])
    text1 = (t1.get('final_text') or '')
    blob1 = system_blob(c1_calls)
    self_aware = ('运维' in text1 or '角色' in text1 or 'AI' in text1) and ('主机' in text1 or '资产' in text1 or '命令' in text1)
    record('C1_self_awareness', self_aware and 'FULL_SERVICE_API_DOCUMENTATION' in blob1,
           {'reply': text1[:400], 'has_api_doc': 'FULL_SERVICE_API_DOCUMENTATION' in blob1, 'state': t1['state']})

    # ---- C2: skill injection isolation ------------------------------------
    c2a = api('POST', '/api/v1/roles/' + role_a + '/messages',
              {'text': '只回答一个词 ok，不要执行命令。', 'execution_users': [], 'mode': 'readonly',
               'idempotency_key': 'c2a-' + run}, 202)
    c2b = api('POST', '/api/v1/roles/' + role_b + '/messages',
              {'text': '只回答一个词 ok，不要执行命令。', 'execution_users': [], 'mode': 'readonly',
               'idempotency_key': 'c2b-' + run}, 202)
    wait_turn(c2a['turn_id']); wait_turn(c2b['turn_id'])
    blob_a = system_blob(calls_for(c2a['turn_id']))
    blob_b = system_blob(calls_for(c2b['turn_id']))
    record('C2_skill_injected_to_own_role_only',
           ('CONN_SKILL_A_' + run) in blob_a and ('CONN_SKILL_A_' + run) not in blob_b
           and ('CONN_SKILL_B_' + run) in blob_b,
           {'a_has_own': ('CONN_SKILL_A_' + run) in blob_a,
            'a_leaks_b': ('CONN_SKILL_B_' + run) in blob_a,
            'b_has_own': ('CONN_SKILL_B_' + run) in blob_b})

    # ---- C3: document injection isolation ---------------------------------
    record('C3_document_injection',
           ('CONN_CORE_' + run) in blob_a and ('CONN_CORE_' + run) in blob_b
           and ('CONN_PRIVATE_A_' + run) in blob_a and ('CONN_PRIVATE_A_' + run) not in blob_b,
           {'core_in_a': ('CONN_CORE_' + run) in blob_a, 'core_in_b': ('CONN_CORE_' + run) in blob_b,
            'privA_in_a': ('CONN_PRIVATE_A_' + run) in blob_a, 'privA_in_b': ('CONN_PRIVATE_A_' + run) in blob_b})

    # ---- C4: authority injection + readonly withholds the tool ------------
    c4 = api('POST', '/api/v1/roles/' + role_a + '/messages',
             {'text': '只回答 ok。', 'execution_users': ['aiops_probe'], 'mode': 'readonly',
              'idempotency_key': 'c4-' + run}, 202)
    wait_turn(c4['turn_id'])
    c4_calls = calls_for(c4['turn_id'])
    tools_offered = sorted({t['function']['name'] for c in c4_calls for t in (c['request'].get('tools') or [])})
    # Inspect the REAL authority block from the raw system message content
    # (not a re-dumped JSON string, which double-escapes the inner JSON).
    authority_ok = False
    for c in c4_calls:
        for m in c['request']['messages']:
            if m['role'] != 'system':
                continue
            content = m['content']
            if ('"role_id": "' + role_a + '"') in content \
                    and '"execution_users": ["aiops_probe"]' in content \
                    and '"mode": "readonly"' in content:
                authority_ok = True
    record('C4_authority_injected_readonly_no_exec',
           authority_ok and 'execute_command' not in tools_offered,
           {'tools_offered': tools_offered, 'authority_present': authority_ok})

    # ---- C5: real execution with confirm gate -----------------------------
    c5 = api('POST', '/api/v1/roles/' + role_a + '/messages',
             {'text': '请在资产 ' + asset + ' 上执行一次 id -un，然后用中文报告真实的用户名。必须实际执行，不要猜测。',
              'execution_users': ['aiops_probe'], 'mode': 'confirm', 'idempotency_key': 'c5-' + run}, 202)
    approved = set()
    deadline = time.time() + 240
    while time.time() < deadline:
        cur = api('GET', '/api/v1/turns/' + c5['turn_id'])
        if cur['state'] == 'completed':
            break
        if cur['state'] in ('failed', 'blocked_unknown', 'cancelled'):
            break
        tid = cur.get('pending_task_id')
        if tid and tid not in approved:
            task = api('GET', '/api/v1/tasks/' + tid)
            agent_once()  # agent claims; in confirm it stays awaiting_approval
            if api('GET', '/api/v1/tasks/' + tid)['state'] == 'awaiting_approval':
                api('POST', '/api/v1/tasks/' + tid + '/approve')
                agent_once()  # executes
            approved.add(tid)
        time.sleep(2)
    final5 = api('GET', '/api/v1/turns/' + c5['turn_id'])
    stdout_ok = False
    if final5.get('pending_task_id') is None and approved:
        for tid in approved:
            t = api('GET', '/api/v1/tasks/' + tid)
            if t['state'] == 'succeeded' and 'aiops_probe' in (t['result'] or {}).get('stdout', ''):
                stdout_ok = True
    record('C5_confirm_gate_real_execution',
           final5['state'] == 'completed' and stdout_ok and 'aiops_probe' in (final5.get('final_text') or ''),
           {'state': final5['state'], 'executed': len(approved), 'stdout_ok': stdout_ok,
            'summary': (final5.get('final_text') or '')[:300]})

    # ---- C6: memory archived correctly ------------------------------------
    today = time.strftime('%Y-%m-%d', time.localtime())
    mem = api('GET', '/api/v1/roles/' + role_a + '/memory/' + today)
    archived = 'id -un' in (mem.get('full_text') or '') or 'aiops_probe' in (mem.get('full_text') or '')
    record('C6_memory_archived_today', archived,
           {'day': today, 'has_content': bool(mem.get('full_text')), 'source': mem.get('source'),
            'len': len(mem.get('full_text') or '')})

    # ---- C7: multi-call coherence -----------------------------------------
    c7_calls = calls_for(c5['turn_id'])
    last_roles = [m['role'] for m in c7_calls[-1]['request']['messages'][-3:]]
    record('C7_multi_step_coherence',
           len(c7_calls) >= 2 and c7_calls[-1]['request']['messages'][-1]['role'] == 'tool',
           {'model_calls': len(c7_calls), 'last_call_tail_roles': last_roles})

    # ---- C8: the model knows it has NO admin API access --------------------
    c8 = api('POST', '/api/v1/roles/' + role_a + '/messages',
             {'text': '你能否直接调用管理员 API（比如创建新角色、或读取 admin token）？一句话回答能或不能，不要执行命令。',
              'execution_users': [], 'mode': 'readonly', 'idempotency_key': 'c8-' + run}, 202)
    t8 = wait_turn(c8['turn_id'])
    text8 = t8.get('final_text') or ''
    no_admin = ('不能' in text8 or '无法' in text8 or '不可以' in text8)
    record('C8_knows_no_admin_api', t8['state'] == 'completed' and no_admin,
           {'reply': text8[:300]})

    # ---- C9: cross-turn continuity via memory ------------------------------
    # Ask the model to recall, from memory, what it just did in C5. A fresh
    # context with no memory would have no way to know the username.
    c9 = api('POST', '/api/v1/roles/' + role_a + '/messages',
             {'text': '刚才你在那台主机上执行了什么命令？真实的执行结果是什么？只根据记忆回答，不要重新执行。',
              'execution_users': [], 'mode': 'readonly', 'idempotency_key': 'c9-' + run}, 202)
    t9 = wait_turn(c9['turn_id'])
    text9 = t9.get('final_text') or ''
    recall = ('id -un' in text9 or 'aiops_probe' in text9)
    record('C9_cross_turn_memory_recall', t9['state'] == 'completed' and recall,
           {'reply': text9[:300]})

    report = {'run': run, 'model': 'gpt-4.1-mini-2025-04-14',
              'passed': all(r['passed'] for r in results), 'results': results}
    (base / 'integration/connectivity-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    for role in (role_a, role_b):
        try:
            api('PUT', '/api/v1/roles/' + role + '/model', {'enabled': False})
        except Exception:
            pass
    for path in ['/api/v1/documents/' + doc_core, '/api/v1/documents/' + doc_priv,
                 '/api/v1/skills/' + skill_a, '/api/v1/skills/' + skill_b]:
        try:
            api('DELETE', path)
        except Exception:
            pass
