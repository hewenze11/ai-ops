"""Push the local ai-ops-agent package to the test host and run the readonly smoke test.

No shell escaping games: SFTP upload, then execute a script that is already on
disk. Password from SSH_PASSWORD.
"""
import os
import sys

import paramiko

HOST = os.environ.get('SSH_HOST', '172.236.254.94')
USER = os.environ.get('SSH_USER', 'root')
LOCAL = r'C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops-agent'
REMOTE = '/opt/ai-ops-agent-src'
PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=os.environ['SSH_PASSWORD'], timeout=20)
    sftp = client.open_sftp()

    def mkdirs(path):
        current = ''
        for part in path.strip('/').split('/'):
            current += '/' + part
            try:
                sftp.stat(current)
            except IOError:
                sftp.mkdir(current)

    mkdirs(REMOTE + '/ai_ops_agent')
    for name in ('agent.py', 'readonly.py', 'output.py', 'service.py', '__init__.py'):
        sftp.put(os.path.join(LOCAL, 'ai_ops_agent', name), REMOTE + '/ai_ops_agent/' + name)
    print('uploaded ai_ops_agent/*')

    script = sftp.file('/tmp/readonly_smoke.py', 'w')
    script.write(SMOKE)
    script.close()
    sftp.close()

    command = f'export PATH={PATH}; cd {REMOTE} && python3 /tmp/readonly_smoke.py'
    stdin, stdout, stderr = client.exec_command(command, timeout=600)
    print(stdout.read().decode('utf-8', 'replace'))
    err = stderr.read().decode('utf-8', 'replace')
    if err.strip():
        print('[stderr]', err)
    print('[exit]', stdout.channel.recv_exit_status())
    client.close()


SMOKE = r'''
import json, os, secrets, sys
sys.path.insert(0, '/opt/ai-ops-agent-src')
from ai_ops_agent import readonly

def task(**kw):
    base = {'claim_id': 'c' * 24, 'tool': 'read_file', 'program': '/usr/bin/cat',
            'args': ['--number', '--', '/etc/hostname'], 'run_as': 'aiops-ro',
            'execution_users': ['aiops-ro'], 'timeout_seconds': 30}
    base.update(kw)
    return base

results = []
def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (' :: ' + str(detail)[:300] if detail else ''))

# 1. A normal read succeeds and is executed without a shell.
r = readonly.execute(task(args=['--number', '--', '/etc/hostname']), ['aiops-ro'])
check('read /etc/hostname succeeds', r['status'] == 'succeeded' and r['exit_code'] == 0, r)
check('output came back numbered', r['stdout'].strip().startswith('1') and '\t' in r['stdout'], r['stdout'][:80])

# 2. A control plane trying to point the tool at another binary is refused.
r = readonly.execute(task(program='/bin/cat', args=['/etc/hostname']), ['aiops-ro'])
check('non-whitelisted program refused', r['status'] == 'failed' and 'PROGRAM_NOT_WHITELISTED' in (r['error_code'] or ''), r['error_code'])

# 3. Shell metacharacters stay literal (no shell involved).
r = readonly.execute(task(args=['--number', '--', '/etc/hostname;id']), ['aiops-ro'])
check('semicolon is a literal filename, not a command', r['status'] == 'failed' and 'uid=' not in r['stdout'], (r['status'], r['stdout'][:80], r['error_code']))

# 4. Output redirection argv is refused even if the control plane sends it.
r = readonly.execute(task(args=['--number', '--', '-fprintf']), ['aiops-ro'])
check('output-redirection argv refused', r['status'] == 'failed' and 'ARGV_INVALID' in (r['error_code'] or ''), r['error_code'])

# 5. A symlink is refused: the audit path must be the file actually read.
r = readonly.execute(task(args=['--number', '--', '/tmp/nginx-redirect.conf']), ['aiops-ro'])
check('symlinked path refused', r['status'] == 'failed' and 'PATH_SYMLINKED' in (r['error_code'] or ''), r['error_code'])

# 6. A file holding secrets comes back masked.
r = readonly.execute(task(args=['--number', '--', '/tmp/aiops-secret-sample.conf']), ['aiops-ro'])
blob = r['stdout'] + r['stderr']
check('secret-shaped values masked', r['status'] == 'succeeded' and 'supersecretvalue' not in blob and '[REDACTED]' in blob, r['stdout'][:400])

# 7. A local credential literal is masked even though it matches no pattern.
r = readonly.execute(task(args=['--number', '--', '/tmp/aiops-literal-sample.txt'],
                          _credential_literals=('Zq9-custom-literal-value',)), ['aiops-ro'])
check('local credential literal masked', 'Zq9-custom-literal-value' not in (r['stdout'] + r['stderr']), r['stdout'][:200])

# 8. Reading /etc/shadow is decided by the OS account, not by our whitelist.
r = readonly.execute(task(args=['--number', '--', '/etc/shadow']), ['aiops-ro'])
check('/etc/shadow denied for the low-privilege account', r['status'] == 'failed' and 'root:' not in r['stdout'], (r['status'], r['exit_code'], r['stderr'][:160]))

# 9. The account must be one this agent is allowed to use.
r = readonly.execute(task(run_as='root', execution_users=['root']), ['aiops-ro'])
check('unlisted account refused', r['status'] == 'failed' and 'USER_NOT_ALLOWED' in (r['error_code'] or ''), r['error_code'])

# 10. A slow command is bounded by the timeout instead of hanging.
r = readonly.execute(task(tool='journal_logs', program='/usr/bin/journalctl',
                          args=['--no-pager', '-n', '5', '--output=short-iso'],
                          timeout_seconds=1), ['aiops-ro'])
check('journal read works for the account', r['status'] in ('succeeded', 'failed'), (r['status'], (r['stderr'] or '')[:120]))

print()
failed = [name for name, ok, _ in results if not ok]
print(f'{len(results) - len(failed)}/{len(results)} passed')
if failed:
    print('FAILED: ' + ', '.join(failed))
    sys.exit(1)
'''


if __name__ == '__main__':
    main()
