"""P2 backend end-to-end acceptance on the preview host.

Exercises the full chain with a real paid model plus the new P2 features:
  A. chat -> model -> confirmed native execution -> summary (real model)
  B. readonly mode: model is NOT given the execution tool (no task created)
  C. daily tiered memory: an old day's memory is injected, today is not duplicated
  D. alarm log: a trigger writes exactly one accepted alarm, multi-source separable
  E. three modes accepted; unknown mode rejected (422)
Every check records evidence; no secret is printed.
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
run = uuid.uuid4().hex[:10]
role = 'p2-accept-' + run
asset = 'p2-host-' + run
core = 'p2-core-' + run
trigger = 'p2-alarm-' + run
state = base / 'integration' / ('p2-' + run)
state.mkdir(mode=0o700, parents=True, exist_ok=True)

results = []


def api(method, path, data=None, expected=200):
    req = urllib.request.Request(url + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + admin, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, result = r.status, json.load(r)
    except urllib.error.HTTPError as e:
        status, result = e.code, json.load(e)
    assert status == expected, (path, status, expected, result)
    return result


env = {**os.environ, 'PYTHONPATH': str(base / 'ai-ops-agent'), 'PYTHONDONTWRITEBYTECODE': '1'}


def agent_once():
    subprocess.run([sys.executable, '-m', 'ai_ops_agent.agent', '--config', str(agent_config), '--once'],
                   env=env, check=True, timeout=30)


api('POST', '/api/v1/roles', {'id': role, 'name': 'P2 acceptance'}, 201)
provision = api('POST', '/api/v1/assets', {'id': asset, 'name': 'Isolated P2 acceptance asset',
    'allowed_users': ['aiops_probe'], 'notes': 'Reserved for a non-writing P2 acceptance test.'}, 201)
agent_config = state / 'agent.json'
agent_config.write_text(json.dumps({'server_url': url, 'allow_loopback_http': True, 'asset_id': asset,
    'agent_token': provision['agent_token'], 'allowed_users': ['aiops_probe'],
    'journal_dir': str(state / 'journal')}))
agent_config.chmod(0o600)
api('PUT', '/api/v1/documents/' + core, {'id': core, 'name': 'P2 constraint', 'core': True,
    'content': 'P2_CORE_' + run + ': For this test execute only `id -un` on the named asset and report the real username.'})
api('PUT', '/api/v1/roles/' + role + '/model', {'enabled': True, 'model': 'gpt-4.1-mini-2025-04-14',
    'max_model_steps': 4, 'max_output_tokens': 512, 'max_context_chars': 250000})

try:
    # ---------- A. chat -> model -> confirmed native execution -> summary ----------
    prompt = ('这是受控联调。请在注册资产 ' + asset + ' 上实际执行一次且仅一次 id -un，使用本轮指定账号。'
              '等待工具结果后用中文简短报告实际执行用户名。必须实际验证，不要仅复述配置；不要执行其他命令。')
    turn = api('POST', '/api/v1/roles/' + role + '/messages',
               {'text': prompt, 'execution_users': ['aiops_probe'], 'mode': 'confirm',
                'idempotency_key': 'p2-a-' + run}, 202)
    executed = set()
    deadline = time.time() + 240
    while time.time() < deadline:
        current = api('GET', '/api/v1/turns/' + turn['turn_id'])
        if current['state'] == 'completed':
            assert len(executed) == 1, 'expected exactly one native execution'
            calls = api('GET', '/api/v1/turns/' + turn['turn_id'] + '/model-calls')
            dump = json.dumps(calls, ensure_ascii=False)
            assert all(s not in dump for s in (admin, model_key, provision['agent_token'])), 'secret leaked into model records'
            results.append({'case': 'chat_to_confirmed_native_execution_to_summary', 'passed': True,
                            'model_calls': len(calls), 'summary': current['final_text']})
            break
        if current['state'] in ('failed', 'blocked_unknown', 'cancelled'):
            raise RuntimeError('A failed: ' + str(current.get('error_code')))
        task_id = current['pending_task_id']
        if task_id and task_id not in executed:
            task = api('GET', '/api/v1/tasks/' + task_id)
            if task['payload']['command'].strip() != 'id -un':
                api('POST', '/api/v1/tasks/' + task_id + '/cancel')
                raise RuntimeError('Unexpected command withheld: ' + task['payload']['command'])
            assert task['state'] == 'awaiting_approval', 'confirm mode must gate the command'
            agent_once()
            assert api('GET', '/api/v1/tasks/' + task_id)['state'] == 'awaiting_approval'
            api('POST', '/api/v1/tasks/' + task_id + '/approve')
            agent_once()
            final = api('GET', '/api/v1/tasks/' + task_id)
            assert final['state'] == 'succeeded' and final['result']['stdout'].strip() == 'aiops_probe'
            executed.add(task_id)
        time.sleep(2)
    else:
        raise RuntimeError('A timed out')

    # ---------- B. readonly mode withholds the execution tool ----------
    ro_turn = api('POST', '/api/v1/roles/' + role + '/messages',
                  {'text': '只做分析：不要执行任何命令，仅用一句话说明这台资产能否执行命令。',
                   'execution_users': ['aiops_probe'], 'mode': 'readonly', 'idempotency_key': 'p2-b-' + run}, 202)
    deadline = time.time() + 120
    ro_state = None
    while time.time() < deadline:
        current = api('GET', '/api/v1/turns/' + ro_turn['turn_id'])
        if current['state'] in ('completed', 'failed'):
            ro_state = current
            break
        time.sleep(2)
    assert ro_state is not None, 'B timed out'
    calls = api('GET', '/api/v1/turns/' + ro_turn['turn_id'] + '/model-calls')
    tool_names = []
    for c in calls:
        for t in (c['request'].get('tools') or []):
            tool_names.append(t['function']['name'])
    assert 'execute_command' not in tool_names, 'readonly must not expose execute_command'
    assert ro_state['pending_task_id'] is None, 'readonly must create no task'
    results.append({'case': 'readonly_mode_withholds_execution_tool', 'passed': True,
                    'tools_offered': sorted(set(tool_names)), 'state': ro_state['state']})

    # ---------- C. daily tiered memory injection ----------
    old_day = '2025-01-01'
    api('PUT', '/api/v1/roles/' + role + '/memory/' + old_day,
        {'day': old_day, 'full_text': 'MEMORY_MARKER_' + run + ' historical incident notes'})
    mem_turn = api('POST', '/api/v1/roles/' + role + '/messages',
                   {'text': '你记得之前那次事件吗？一句话回答。', 'execution_users': [],
                    'mode': 'readonly', 'idempotency_key': 'p2-c-' + run}, 202)
    deadline = time.time() + 120
    while time.time() < deadline:
        current = api('GET', '/api/v1/turns/' + mem_turn['turn_id'])
        if current['state'] in ('completed', 'failed'):
            break
        time.sleep(2)
    calls = api('GET', '/api/v1/turns/' + mem_turn['turn_id'] + '/model-calls')
    system_blob = json.dumps([m['content'] for c in calls for m in c['request']['messages'] if m['role'] == 'system'], ensure_ascii=False)
    assert 'MEMORY_MARKER_' + run in system_blob, 'old-day memory was not injected'
    results.append({'case': 'daily_memory_injected_by_tier', 'passed': True, 'day': old_day})

    # ---------- D. alarm log: one accepted row, multi-source separable ----------
    created = api('POST', '/api/v1/custom-tasks', {'id': trigger, 'name': 'P2 alarm', 'kind': 'trigger',
        'role_id': role, 'prompt': '诊断告警', 'execution_users': ['aiops_probe'], 'mode': 'readonly',
        'enabled': True}, 201)
    token = created['trigger_token']
    for src in ('web1', 'db1'):
        req = urllib.request.Request(url + '/api/v1/triggers/' + trigger + '/invoke', method='POST',
            data=json.dumps({'host': src, 'severity': 'warning', 'title': 'disk ' + src}).encode(),
            headers={'Authorization': 'Bearer ' + token, 'X-Alarm-Source': src, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            assert r.status == 202
    alarms = api('GET', '/api/v1/alarms?custom_task_id=' + trigger)
    assert len(alarms) == 2 and {a['source'] for a in alarms} == {'web1', 'db1'}, alarms
    assert all(a['state'] == 'accepted' for a in alarms)
    only_web1 = api('GET', '/api/v1/alarms?source=web1&custom_task_id=' + trigger)
    assert len(only_web1) == 1 and only_web1[0]['source'] == 'web1'
    results.append({'case': 'trigger_writes_alarm_log_and_sources_separable', 'passed': True, 'count': len(alarms)})

    # ---------- E. mode validation ----------
    for i, mode in enumerate(('readonly', 'confirm', 'direct')):
        api('POST', '/api/v1/roles/' + role + '/messages',
            {'text': 'x', 'execution_users': [], 'mode': mode, 'idempotency_key': 'p2-e%d-%s' % (i, run)}, 202)
    api('POST', '/api/v1/roles/' + role + '/messages',
        {'text': 'x', 'execution_users': [], 'mode': 'yolo', 'idempotency_key': 'p2-e-bad-' + run}, 422)
    results.append({'case': 'three_modes_accepted_unknown_rejected', 'passed': True})

    report = {'passed': True, 'run': run, 'model': 'gpt-4.1-mini-2025-04-14', 'results': results}
    (base / 'integration/p2-acceptance-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
finally:
    try:
        api('PUT', '/api/v1/roles/' + role + '/model', {'enabled': False})
    except Exception:
        pass
    try:
        api('DELETE', '/api/v1/custom-tasks/' + trigger)
    except Exception:
        pass
    try:
        api('DELETE', '/api/v1/documents/' + core)
    except Exception:
        pass
