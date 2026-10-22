"""Remove leftover smoke-test files via SFTP (coreutils are missing on this host)."""
import os

import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('172.236.254.94', username='root', password=os.environ['SSH_PASSWORD'], timeout=20)
sftp = client.open_sftp()
for name in ('aiops-should-not-exist.txt', 'aiops-skipped.txt', 'aiops-change-target.conf',
             'readonly_smoke.py', 'change_smoke.py', 'diag_tamper.py', 't.py'):
    path = '/tmp/' + name
    try:
        sftp.remove(path)
        print('removed', path)
    except IOError as error:
        print('absent ', path, error.errno)
sftp.close()
client.close()
