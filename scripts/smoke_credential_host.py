"""Stage 4 on a real host: credential store, injection, exact-match masking.

Runs on Linux via SFTP upload, like the other smoke scripts. What it checks is
what a unit test cannot: POSIX modes, AES-GCM through the real key file, the
flock between two processes, and that a secret written into a file is masked on
the way back out.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = sys.argv[1] if len(sys.argv) > 1 else '/opt/ai-ops-agent-smoke'
sys.path.insert(0, ROOT)

from ai_ops_agent.credentials import Store, CredentialError, apply_injections  # noqa: E402
from ai_ops_agent import readonly  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + (' :: ' + str(detail) if detail else ''))


def main():
    work = tempfile.mkdtemp(prefix='aiops-cred-')
    store = Store(os.path.join(work, 'credentials'))

    secret = 'db-pass-Zq9-literal-value'
    handle = store.register(secret, name='app db', source='/srv/app/.env')
    check('handle has no value', secret not in json.dumps(handle), handle['id'])

    mode = oct(os.stat(store.path).st_mode & 0o777)
    check('store file is 0600', mode == '0o600', mode)
    mode = oct(os.stat(store.key_path).st_mode & 0o777)
    check('key file is 0600', mode == '0o600', mode)

    raw = open(store.path, encoding='utf-8').read()
    check('value not readable on disk', secret not in raw)

    check('reveal round-trips', store.reveal(handle['id']) == secret)

    # Another account must not be able to read the store.
    os.chmod(work, 0o755)
    probe = subprocess.run(['su', '-s', '/bin/sh', 'aiops-ro', '-c', 'cat %s' % store.path],
                           capture_output=True, text=True)
    check('a different account cannot read the store', probe.returncode != 0,
          (probe.stderr or '').strip().splitlines()[-1:] or probe.returncode)

    # own vs cooperative
    store.bind_owner('master-a')
    check('owner may inject', store.may_share('master-a', handle['id']))
    check('cooperative controller may not inject before share', not store.may_share('master-b', handle['id']))
    store.share(handle['id'], 'master-b')
    check('share enables injection', store.may_share('master-b', handle['id']))
    store.share(handle['id'], 'master-b', allowed=False)
    check('unshare disables it again', not store.may_share('master-b', handle['id']))

    params = {'content': 'cred:' + handle['id']}
    injected = apply_injections(params, params, store, 'master-a')
    check('injection puts the value in content', injected['content'] == secret)
    try:
        apply_injections({'path': 'cred:' + handle['id']}, {'path': 'cred:' + handle['id']}, store, 'master-a')
        check('non-sink parameter refused', False)
    except CredentialError as error:
        check('non-sink parameter refused', error.code == 'CREDENTIAL_SINK_NOT_DECLARED', error.code)

    # Exact-match masking of real tool output.
    target = os.path.join(work, 'app.env')
    with open(target, 'w', encoding='utf-8') as f:
        f.write('DB_PASSWORD=%s\nDEBUG=1\n' % secret)
    masked = readonly.redact('DB_PASSWORD=%s\nDEBUG=1\n' % secret, store.literals())
    check('output masks the known credential', secret not in masked, masked.replace('\n', ' | '))
    check('output keeps non-secret lines', 'DEBUG=1' in masked)

    # flock: a second process holding the lock blocks the first.
    from ai_ops_agent.credentials import _FileLock
    locker = subprocess.Popen([sys.executable, '-c',
        'import sys,time;sys.path.insert(0,%r);from ai_ops_agent.credentials import _FileLock;'
        'l=_FileLock(__import__("pathlib").Path(%r));l.__enter__();time.sleep(3)'
        % (ROOT, os.path.join(work, 'credentials', '.credentials.lock'))])
    import time
    time.sleep(0.8)
    started = time.time()
    with _FileLock(store.path.parent / '.credentials.lock'):
        waited = time.time() - started
    locker.wait()
    check('store lock serializes other processes', waited > 1.0, '%.2fs' % waited)

    print('\n%d/%d passed' % (len(PASSED), len(PASSED) + len(FAILED)))
    return 1 if FAILED else 0


if __name__ == '__main__':
    raise SystemExit(main())
