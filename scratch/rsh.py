"""Minimal SSH runner for the 172.236.254.94 test host (no ssh.exe on this box).

Usage:
  python scratch/rsh.py <<'EOF'
  command one
  command two
  EOF
Each stdin line is one command; output is streamed with a `$ command` header.
Password comes from SSH_PASSWORD in the environment.
"""
import os
import sys

import paramiko

HOST = os.environ.get('SSH_HOST', '172.236.254.94')
USER = os.environ.get('SSH_USER', 'root')


def run(client, command):
    # Non-interactive exec arrives with an empty PATH; set it explicitly.
    command = 'export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; ' + command
    stdin, stdout, stderr = client.exec_command(command, timeout=600, get_pty=False)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(f'\n$ {command}\n')
    if out.strip():
        sys.stdout.write(out if out.endswith('\n') else out + '\n')
    if err.strip():
        sys.stdout.write('[stderr] ' + (err if err.endswith('\n') else err + '\n'))
    sys.stdout.write(f'[exit {code}]\n')
    sys.stdout.flush()
    return code


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=os.environ['SSH_PASSWORD'], timeout=20)
    try:
        for line in sys.stdin.read().splitlines():
            command = line.strip()
            if command and not command.startswith('#'):
                run(client, command)
    finally:
        client.close()


if __name__ == '__main__':
    main()
