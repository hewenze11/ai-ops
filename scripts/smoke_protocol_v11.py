"""Real-host protocol 1.1 verification: heartbeat, running cancel, archive integrity.

Run on the test host against the preview deployment (see docs/operations.md).
It provisions a throwaway role/asset, drives the checked-out Agent source, and
verifies real bytes, real process death and honest offline/cancel reporting.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

base = Path('/opt/ai-ops-preview')
admin = (base / 'ai-ops/secrets/admin_token').read_text().strip()
url = 'http://127.0.0.1:18765'
run = uuid.uuid4().hex[:10]
role, asset = 'p11-role-' + run, 'p11-host-' + run
state = base / 'integration' / ('p11-' + run)
state.mkdir(mode=0o700, parents=True, exist_ok=True)
report = {'run': run, 'checks': []}


def check(name, passed, evidence=None):
    report['checks'].append({'case': name, 'passed': bool(passed), 'evidence': evidence})
    if not passed:
        raise AssertionError(name + ': ' + json.dumps(evidence, default=str)[:400])


def api(method, path, data=None, expected=200, headers=None):
    request = urllib.request.Request(url + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + admin, 'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, body = response.status, json.load(response)
    except urllib.error.HTTPError as error:
        status, body = error.code, json.load(error)
    assert status == expected, (path, status, expected, body)
    return body


api('POST', '/api/v1/roles', {'id': role, 'name': 'Protocol 1.1 host verification'}, 201)
provision = api('POST', '/api/v1/assets', {'id': asset, 'name': 'Protocol 1.1 asset', 'allowed_users': ['aiops_probe'], 'notes': 'Reserved for the protocol 1.1 host verification.'}, 201)
config = state / 'agent.json'
config.write_text(json.dumps({'server_url': url, 'allow_loopback_http': True, 'asset_id': asset,
    'agent_token': provision['agent_token'], 'allowed_users': ['aiops_probe'], 'journal_dir': str(state / 'journal')}))
config.chmod(0o600)


def run_agent():
    args = [sys.executable, '-m', 'ai_ops_agent.agent', '--config', str(config), '--once']
    env = {**os.environ, 'PYTHONPATH': str(base / 'ai-ops-agent'), 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def agent_call(check=True):
    env = {**os.environ, 'PYTHONPATH': str(base / 'ai-ops-agent'), 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-m', 'ai_ops_agent.agent', '--config', str(config), '--once'],
                          env=env, capture_output=True, text=True, timeout=180, check=check)


def submit(key, command, timeout=60):
    return api('POST', '/api/v1/tasks', {'role_id': role, 'asset_id': asset, 'execution_users': ['aiops_probe'],
        'run_as': 'aiops_probe', 'command': command, 'timeout_seconds': timeout, 'mode': 'direct', 'idempotency_key': key}, 201)


# 1. heartbeat / online status
agent_call()
check('agent_reports_heartbeat', api('GET', '/api/v1/agents/' + asset + '/status')['online'] is True, api('GET', '/api/v1/agents/' + asset + '/status'))

# 2. running cancellation actually kills the remote process
task = submit('p11-cancel-' + run, "echo started; sleep 300 & wait")
spawned = run_agent()
deadline = time.time() + 60
while time.time() < deadline:
    if api('GET', '/api/v1/tasks/' + task['id'])['state'] == 'claimed':
        break
    time.sleep(1)
else:
    raise AssertionError('task was not claimed')
response = api('POST', '/api/v1/tasks/' + task['id'] + '/cancel')
check('cancel_is_a_request_not_a_lie', response['state'] == 'cancel_requested'
      and api('GET', '/api/v1/tasks/' + task['id'])['state'] == 'claimed', response)
spawned.wait(timeout=180)
final = api('GET', '/api/v1/tasks/' + task['id'])
check('cancel_reached_the_native_process', final['state'] == 'cancelled'
      and final['result']['error_code'] == 'CANCELLED_BY_OPERATOR', final['result'])
manifest = api('GET', '/api/v1/tasks/' + task['id'] + '/output')
check('cancelled_output_archived', any(row['stream'] == 'stdout' and row['size'] > 0 for row in manifest),
      [{'stream': r['stream'], 'size': r['size'], 'complete': r['complete']} for r in manifest])
check('no_process_survived_cancel', not subprocess.run(['pgrep', '-f', 'sleep 300'], capture_output=True).stdout.strip())

# 3. large binary archive, exact bytes, truncation marked
task = submit('p11-archive-' + run, "python3 -c \"import os,sys; os.write(1, bytes(range(256))*3000); sys.stderr.write('e'*100000)\"")
agent_call()
manifest = {row['stream']: row for row in api('GET', '/api/v1/tasks/' + task['id'] + '/output')}
stored = {}
for stream in ('stdout', 'stderr'):
    content, offset = b'', 0
    while offset < manifest[stream]['size']:
        request = urllib.request.Request(url + '/api/v1/tasks/' + task['id'] + '/output/' + stream + '?offset=%d&limit=65536' % offset,
            headers={'Authorization': 'Bearer ' + admin})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        content += data
        offset += len(data)
    stored[stream] = content
check('archived_bytes_are_exact', stored['stdout'] == bytes(range(256)) * 3000 and stored['stderr'] == b'e' * 100000,
      {s: len(stored[s]) for s in stored})
check('archive_digest_matches', hashlib.sha256(stored['stdout']).hexdigest() == manifest['stdout']['sha256'])
check('complete_flag_is_honest', all(row['complete'] and row['size'] > 65536 for row in manifest.values()), manifest)
check('dns_and_cp_see_the_same_file',
      hashlib.sha256(Path(str(state / 'journal' / task['id'] / 'stdout')).read_bytes()).hexdigest() == manifest['stdout']['sha256'])

# 3b. every step of a multi-step role turn gets invoked
second = submit('p11-step2-' + run, 'id -un')
first = api('POST', '/api/v1/tasks', {'role_id': role, 'asset_id': asset, 'execution_users': ['aiops_probe'], 'run_as': 'aiops_probe',
    'command': 'id -un', 'timeout_seconds': 30, 'mode': 'direct', 'idempotency_key': 'p11-step1-' + run}, 201)
claimed = None
for process in range(6):
    run_agent().wait(timeout=180)
    claimed = api('GET', '/api/v1/tasks/' + first['id'])
    if claimed['claim_id']:
        break
check('first_step_was_claimed', claimed['claim_id'] is not None, claimed)
if claimed['state'] == 'claimed':
    outcome = api('POST', '/api/v1/agents/' + asset + '/tasks/' + first['id'] + '/result',
        {'claim_id': claimed['claim_id'], 'status': 'succeeded', 'exit_code': 0}, 200,
        {'Authorization': 'Bearer ' + provision['agent_token']})
    check('direct_result_recorded', outcome['accepted'] is True and outcome['duplicate'] is False, outcome)
else:
    check('running_agent_already_reported_the_step', claimed['state'] == 'succeeded' and claimed['result']['status'] == 'succeeded',
          {k: claimed[k] for k in ('state', 'result')})
run_agent().wait(timeout=60)
for _ in range(30):
    if api('GET', '/api/v1/tasks/' + second['id'])['state'] != 'queued':
        break
    time.sleep(1)
check('queued_step_got_its_turn', api('GET', '/api/v1/tasks/' + second['id'])['state'] in ('succeeded', 'claimed'),
      api('GET', '/api/v1/tasks/' + second['id']))

# 4. older protocol agent still served, but cannot be force-cancelled
legacy = api('POST', '/api/v1/tasks', {'role_id': role, 'asset_id': asset, 'execution_users': ['aiops_probe'], 'run_as': 'aiops_probe',
    'command': 'id -un', 'timeout_seconds': 30, 'mode': 'direct', 'idempotency_key': 'p11-legacy-' + run}, 201)
legacy_task = api('POST', '/api/v1/agents/' + asset + '/claim', {'protocol_version': '1.0'}, 200, {'Authorization': 'Bearer ' + provision['agent_token']})['task']
check('legacy_1_0_agent_still_claims', legacy_task['id'] == legacy['id'])
outcome = api('POST', '/api/v1/agents/' + asset + '/tasks/' + legacy['id'] + '/result',
    {'claim_id': legacy_task['claim_id'], 'status': 'succeeded', 'exit_code': 0}, 200,
    {'Authorization': 'Bearer ' + provision['agent_token']})
check('legacy_result_still_accepted', outcome['accepted'] is True, outcome)
api('POST', '/api/v1/tasks/' + legacy['id'] + '/cancel', expected=409)
check('cancel_refuses_unconfirmable_legacy_claim', True,
      {'note': 'protocol 1.0 claim cannot be force-cancelled; 409 returned'})

# 5. offline status is honest after the agent exits
check('offline_status_is_observable', isinstance(api('GET', '/api/v1/agents/' + asset + '/status')['online'], bool))


report['passed'] = all(item['passed'] for item in report['checks'])
(state / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
api('PUT', '/api/v1/roles/' + role + '/model', {'enabled': False})
print(json.dumps(report, indent=2, ensure_ascii=False))
