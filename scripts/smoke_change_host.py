"""Run the change-set executor against the real test host.

Verifies that an approved change set actually performs a real, harmless system
action, that the approval hash is enforced at the endpoint, and that a refused
action never runs.
"""
import os

import paramiko

HOST = os.environ.get('SSH_HOST', '172.236.254.94')
LOCAL = r'C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops-agent'
REMOTE = '/opt/ai-ops-agent-src'
PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=os.environ.get('SSH_USER', 'root'),
                   password=os.environ['SSH_PASSWORD'], timeout=20)
    sftp = client.open_sftp()
    for name in ('agent.py', 'readonly.py', 'changes.py', 'output.py', '__init__.py'):
        sftp.put(os.path.join(LOCAL, 'ai_ops_agent', name), REMOTE + '/ai_ops_agent/' + name)
    handle = sftp.file('/tmp/change_smoke.py', 'w')
    handle.write(SMOKE)
    handle.close()
    sftp.close()
    stdin, stdout, stderr = client.exec_command(
        f'export PATH={PATH}; cd {REMOTE} && python3 /tmp/change_smoke.py', timeout=600)
    print(stdout.read().decode('utf-8', 'replace'))
    err = stderr.read().decode('utf-8', 'replace')
    if err.strip():
        print('[stderr]', err)
    print('[exit]', stdout.channel.recv_exit_status())
    client.close()


SMOKE = r'''
import json, os, sys
sys.path.insert(0, '/opt/ai-ops-agent-src')
from ai_ops_agent import changes

results = []
def check(name, ok, detail=''):
    results.append(ok)
    print(('PASS ' if ok else 'FAIL ') + name + (' :: ' + str(detail)[:280] if detail else ''))

def task(steps, **kw):
    t = {'claim_id': 'c' * 24, 'asset_id': 'web1', 'run_as': 'aiops-ro',
         'execution_users': ['aiops-ro'], 'timeout_seconds': 60, 'steps': steps}
    t['approval'] = {'payload_sha256': changes.payload_hash('web1', 'aiops-ro', steps)}
    t.update(kw)
    return t

def step(action, program, args, params, identity=None):
    return {'action': action, 'params': params, 'program': program,
            'args': args, 'identity': identity or action}

# 1. A real file write lands, with a real receipt.
s = step('file.write', '__builtin_write__', ['/tmp/aiops-change-target.conf'],
         {'path': '/tmp/aiops-change-target.conf', 'content': 'worker_processes 4;\n'})
r = changes.execute(task([s]), ['aiops-ro'])
written = open('/tmp/aiops-change-target.conf').read() if os.path.exists('/tmp/aiops-change-target.conf') else ''
check('file.write actually writes', r['status'] == 'succeeded' and written == 'worker_processes 4;\n', r['error_code'])

# 2. A unit reload that the account may not perform is refused by the OS, and
#    the failure is reported (not swallowed).
s = step('service.reload', '/usr/bin/systemctl', ['reload', 'cron'], {'unit': 'cron'})
r = changes.execute(task([s]), ['aiops-ro'])
check('service.reload as a low-privilege account fails honestly',
      r['status'] == 'failed' and r['change_report'][0]['status'] == 'failed', r['change_report'][0].get('output', '')[:140])

# 3. A tampered payload never runs: the approval hash no longer matches.
s = step('file.write', '__builtin_write__', ['/tmp/aiops-should-not-exist.txt'],
         {'path': '/tmp/aiops-should-not-exist.txt', 'content': 'nope'})
t = task([s])
t['steps'][0]['params']['content'] = 'tampered'
r = changes.execute(t, ['aiops-ro'])
check('tampered payload refused', r['status'] == 'failed' and 'APPROVAL_HASH_MISMATCH' in (r['error_code'] or ''), r['error_code'])
check('tampered payload did not write', not os.path.exists('/tmp/aiops-should-not-exist.txt'))

# 4. /etc/shadow is refused even with a matching hash.
s = step('file.write', '__builtin_write__', ['/etc/shadow'], {'path': '/etc/shadow', 'content': 'x'})
r = changes.execute(task([s]), ['aiops-ro'])
check('/etc/shadow write refused at the endpoint', r['status'] == 'failed' and 'ACTION_PATH_DENIED' in (r['error_code'] or ''), r['error_code'])

# 5. A failing step stops the set: the second step is skipped, not run anyway.
s1 = step('service.reload', '/usr/bin/systemctl', ['reload', 'cron'], {'unit': 'cron'})
s2 = step('file.write', '__builtin_write__', ['/tmp/aiops-skipped.txt'],
          {'path': '/tmp/aiops-skipped.txt', 'content': 'should not exist'})
r = changes.execute(task([s1, s2]), ['aiops-ro'])
check('failure stops the remaining steps',
      r['status'] == 'failed' and r['change_report'][1]['status'] == 'skipped'
      and not os.path.exists('/tmp/aiops-skipped.txt'), [x['status'] for x in r['change_report']])

# 6. k8s action shape is built correctly (kubectl may be absent; we only check
#    the argv that would run, via the validator).
s = step('k8s.rollout_restart', '/usr/bin/kubectl',
         ['rollout', 'restart', 'deployment/web', '-n', 'prod'],
         {'kind': 'deployment', 'name': 'web', 'namespace': 'prod'})
try:
    changes.validate(task([s]))
    check('k8s rollout argv accepted', True)
except changes.Rejected as e:
    check('k8s rollout argv accepted', False, e.code)

for path in ('/tmp/aiops-change-target.conf',):
    if os.path.exists(path):
        os.unlink(path)
print()
print('%d/%d passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
'''


if __name__ == '__main__':
    main()
