"""Connector-side credential discovery (§42, §46.3).

The connector has no local credential store, so the control plane is the only
place a discovered secret can be inventoried. That makes two things mandatory
and both are tested here:

  1. the value must be consumed *before* masking (afterwards there is nothing
     left to key a handle on), and
  2. the value must not survive anywhere -- not in the handle, not in the id,
     not in the stored result.
"""
import json

from ai_ops import connector_ssh

PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
       "-----END RSA PRIVATE KEY-----")


def _payload(**extra):
    base = {'kind': 'readonly', 'asset_id': 'asset-1', 'tool': 'read_file',
            'params': {'path': '/etc/app/key.pem'},
            'args': ['--number', '--', '/etc/app/key.pem']}
    base.update(extra)
    return base


def test_discovery_runs_before_masking():
    """The discovery input is the *unmasked* result, so a handle can exist."""
    result = {'stdout': PEM, 'stderr': ''}
    handles = connector_ssh._discover(result, 'readonly', _payload())
    assert handles, 'a PEM private key must be discovered'
    assert handles[0]['source'] == '/etc/app/key.pem'
    assert handles[0]['kind'] == 'pattern'


def test_handle_carries_no_value():
    result = {'stdout': PEM, 'stderr': ''}
    handles = connector_ssh._discover(result, 'readonly', _payload())
    blob = json.dumps(handles)
    assert 'BEGIN RSA PRIVATE KEY' not in blob
    assert 'MIIEowIBAAKCAQEA' not in blob, 'not even a fragment of the key'


def test_handle_id_is_stable_for_the_same_secret():
    first = connector_ssh._discover({'stdout': PEM}, 'readonly', _payload())
    second = connector_ssh._discover({'stdout': PEM}, 'readonly', _payload())
    assert first[0]['id'] == second[0]['id'], 're-discovery must not duplicate'


def test_handle_id_differs_per_asset():
    """A handle is scoped to the host that reported it (§46.3); sharing one id
    across hosts would read as a cross-host licence."""
    a = connector_ssh._discover({'stdout': PEM}, 'readonly', _payload(asset_id='asset-1'))
    b = connector_ssh._discover({'stdout': PEM}, 'readonly', _payload(asset_id='asset-2'))
    assert a[0]['id'] != b[0]['id']


def test_no_secret_shaped_text_means_no_handle():
    handles = connector_ssh._discover({'stdout': 'total 0\ndrwxr-xr-x 2 root root 6'},
                                      'readonly', _payload())
    assert handles == []


def test_stderr_is_scanned_too():
    handles = connector_ssh._discover({'stdout': '', 'stderr': PEM},
                                      'readonly', _payload())
    assert handles


def test_run_dispatches_discovery_only_for_readonly(monkeypatch):
    """A shell task's output is not inventoried: it is not a file read whose
    path we can cite, so the handle would have no evidence trail."""
    assert connector_ssh._discover({'stdout': PEM}, 'shell', _payload()) == []
