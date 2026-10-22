"""Diagnose tamper detection on the real host."""
import os

import paramiko

HOST = '172.236.254.94'

SCRIPT = r'''
import sys
sys.path.insert(0, '/opt/ai-ops-agent-src')
from ai_ops_agent import changes

s = {'action': 'file.write', 'params': {'path': '/tmp/zz.txt', 'content': 'a'},
     'program': '__builtin_write__', 'args': ['/tmp/zz.txt'], 'identity': 'x'}
t = {'claim_id': 'c' * 24, 'asset_id': 'w', 'run_as': 'aiops-ro',
     'execution_users': ['aiops-ro'], 'timeout_seconds': 30, 'steps': [s]}
t['approval'] = {'payload_sha256': changes.payload_hash('w', 'aiops-ro', [s])}
print('hash before:', t['approval']['payload_sha256'][:16])
t['steps'][0]['params']['content'] = 'TAMPERED'
print('hash now   :', changes.payload_hash('w', 'aiops-ro', t['steps'])[:16])
try:
    changes.validate(t)
    print('VALIDATED - BUG: tampered payload passed')
except changes.Rejected as e:
    print('rejected:', e.code)
'''


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username='root', password=os.environ['SSH_PASSWORD'], timeout=20)
    sftp = client.open_sftp()
    with sftp.file('/tmp/diag_tamper.py', 'w') as handle:
        handle.write(SCRIPT)
    sftp.close()
    stdin, stdout, stderr = client.exec_command(
        'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; python3 /tmp/diag_tamper.py',
        timeout=120)
    print(stdout.read().decode())
    print(stderr.read().decode())
    client.close()


if __name__ == '__main__':
    main()
