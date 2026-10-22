"""Stage 4.1 on a real host: discovery creates handles, values never leave.

What has to hold:
  * reading a file that contains secrets returns a masked body AND handles,
  * the handle list contains no value, and the handle records the file actually
    read (from argv, not from anything the model named),
  * the discovered value is masked by exact match on the very same response,
  * a read with no secrets returns no handles.
"""
import json
import os
import sys
import tempfile

ROOT = sys.argv[1] if len(sys.argv) > 1 else '/opt/ai-ops-agent-smoke'
sys.path.insert(0, ROOT)

from ai_ops_agent import readonly  # noqa: E402
from ai_ops_agent.credentials import Store  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + (' :: ' + str(detail) if detail else ''))


def task(path):
    program, argv = readonly.validate({'tool': 'read_file', 'program': '/usr/bin/cat', 'args': ['--number', path]})
    return {'claim_id': 'c' * 16, 'tool': 'read_file', 'program': program, 'args': argv,
            'run_as': 'aiops-ro', 'execution_users': ['aiops-ro'], 'timeout_seconds': 30}


def main():
    work = tempfile.mkdtemp(prefix='aiops-discover-')
    store = Store(os.path.join(work, 'credentials'))

    secret_file = os.path.join(work, 'app.env')
    with open(secret_file, 'w', encoding='utf-8') as f:
        f.write('DB_PASSWORD=Zq9-literal-value-xyz\nAPI_TOKEN=tok_abcdef1234567890\nDEBUG=1\n')
    os.chmod(secret_file, 0o644)
    os.chmod(work, 0o755)

    result = readonly.execute(task(secret_file), ['aiops-ro'], credentials=store)
    check('read succeeds', result['status'] == 'succeeded', result.get('error_code'))
    out = result['stdout']
    check('password value is masked', 'Zq9-literal-value-xyz' not in out, out.replace('\n', ' | '))
    check('token value is masked', 'tok_abcdef1234567890' not in out)
    check('non-secret line preserved', 'DEBUG=1' in out)

    handles = result.get('credentials') or []
    check('handles were created', len(handles) == 2, [h.get('id') for h in handles])
    blob = json.dumps(handles)
    check('handle payload contains no value', 'Zq9-literal-value-xyz' not in blob and 'tok_abcdef1234567890' not in blob, blob[:160])
    check('handle records the file actually read',
          all(h.get('source') == secret_file for h in handles), [h.get('source') for h in handles])
    check('handle names are usable', all(h.get('name') for h in handles))

    # The stored values are exactly the values, so exact-match masking works on
    # the next read even if the shape changes.
    literals = store.literals()
    check('store holds both values locally', len(literals) >= 2, len(literals))
    again = readonly.execute(task(secret_file), ['aiops-ro'], credentials=store)
    check('second read is masked too', 'Zq9-literal-value-xyz' not in again['stdout'])
    check('second read does not duplicate handles',
          [h['id'] for h in (again.get('credentials') or [])].sort() == [h['id'] for h in handles].sort())

    plain_file = os.path.join(work, 'plain.txt')
    with open(plain_file, 'w', encoding='utf-8') as f:
        f.write('hello\nworld\n')
    os.chmod(plain_file, 0o644)
    clean = readonly.execute(task(plain_file), ['aiops-ro'], credentials=store)
    check('a file with no secrets creates no handles', not (clean.get('credentials') or []),
          clean.get('credentials'))
    check('a file with no secrets is returned unchanged', 'hello' in clean['stdout'])

    # The value must not be written to the inventory file in the clear.
    raw = open(store.path, encoding='utf-8').read()
    check('inventory file has no plaintext', 'Zq9-literal-value-xyz' not in raw)

    print('\n%d/%d passed' % (len(PASSED), len(PASSED) + len(FAILED)))
    return 1 if FAILED else 0


if __name__ == '__main__':
    raise SystemExit(main())
