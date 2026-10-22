"""Upload the agent package to the test host and run a smoke script there.

Password from SSH_PASSWORD. No shell escaping: files go over SFTP, then the
script is executed by absolute path.
"""
import os
import sys

import paramiko

HOST = os.environ.get('SSH_HOST', '172.236.254.94')
USER = os.environ.get('SSH_USER', 'root')
LOCAL_AGENT = os.environ.get('AGENT_SRC', r'C:\Users\Administrator\.easyclaw\workspace\projects\ai-ops-agent')
REMOTE = os.environ.get('REMOTE_DIR', '/opt/ai-ops-agent-smoke')
SCRIPT = sys.argv[1]


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=os.environ['SSH_PASSWORD'], timeout=30)
    sftp = client.open_sftp()
    for command in (f'mkdir -p {REMOTE}',):
        client.exec_command('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; ' + command)[1].channel.recv_exit_status()
    for name in os.listdir(os.path.join(LOCAL_AGENT, 'ai_ops_agent')):
        if not name.endswith('.py'):
            continue
        sftp.put(os.path.join(LOCAL_AGENT, 'ai_ops_agent', name), f'{REMOTE}/ai_ops_agent/{name}' if False else f'{REMOTE}/{name}')
        # remote layout: <REMOTE>/ai_ops_agent/*.py on sys.path as <REMOTE>
    sftp.put(SCRIPT, f'{REMOTE}/smoke.py')
    sftp.close()
    command = ('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; '
               f'mkdir -p {REMOTE}/ai_ops_agent && cd {REMOTE} && '
               f'for f in {REMOTE}/*.py; do case "$f" in *smoke.py) ;; *) mv -f "$f" {REMOTE}/ai_ops_agent/;; esac; done && '
               f'touch {REMOTE}/ai_ops_agent/__init__.py && '
               f'python3 {REMOTE}/smoke.py {REMOTE}')
    stdin, stdout, stderr = client.exec_command(command, timeout=600)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err.strip():
        sys.stdout.write('\n[stderr]\n' + err)
    client.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
