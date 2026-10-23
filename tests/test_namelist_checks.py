"""Mechanical name-list checks (§44.2).

The scope-covering rules are the part most likely to be subtly wrong, so they
are pinned explicitly. Two rules both apply only where *all* their tags are
present, which means a rule scoped to ``[a, b]`` is *narrower* than one scoped
to ``[a]`` -- the opposite of what "more tags = more specific = covered by
nothing" intuition suggests in a glob world.
"""
import pytest
from fastapi.testclient import TestClient

from ai_ops.app import create_app
from ai_ops.namelist_checks import (
    _covers, _intersect, check_list, dead_rules, dry_run, overlaps,
)

TOKEN = 't0ken-' + 'x' * 32


def _rule(rule_id, action, hosts=None):
    return {'rule_id': rule_id, 'action': action, 'hosts': hosts or [], 'note': ''}


# --- scope algebra ----------------------------------------------------------

def test_empty_scope_covers_everything():
    assert _covers([], ['prod'])
    assert _covers([], [])
    assert _covers(None, ['a', 'b'])


def test_narrower_scope_covered_by_wider_one():
    """[prod] applies on more hosts than [prod, db], so [prod] covers [prod, db]."""
    assert _covers(['prod'], ['prod', 'db'])
    assert not _covers(['prod', 'db'], ['prod'])


def test_unrelated_scopes_do_not_cover_each_other():
    assert not _covers(['prod'], ['staging'])
    assert not _covers(['prod', 'a'], ['prod', 'b'])


def test_intersection_of_two_scopes_requires_both_tags():
    assert _intersect(['prod'], ['db']) == {'prod', 'db'}
    assert _intersect([], ['prod']) == {'prod'}
    assert _intersect(['prod'], []) == {'prod'}
    assert _intersect([], []) == frozenset()


# --- dead rules -------------------------------------------------------------

def test_globally_scoped_rule_makes_scoped_rule_dead():
    rules = [_rule('wide', 'service.restart'), _rule('narrow', 'service.restart', ['prod'])]
    found = dead_rules(rules)
    assert [f['rule_id'] for f in found] == ['narrow']
    assert found[0]['covered_by'] == 'wide'


def test_wider_scope_makes_narrower_rule_dead():
    rules = [_rule('a', 'x', ['prod']), _rule('b', 'x', ['prod', 'db'])]
    found = dead_rules(rules)
    assert [f['rule_id'] for f in found] == ['b']


def test_different_actions_never_cover():
    rules = [_rule('a', 'service.restart'), _rule('b', 'service.reload', ['prod'])]
    assert dead_rules(rules) == []


def test_duplicate_rule_reported_once():
    """Two identical (action, scope) rules: one finding, not two."""
    rules = [_rule('a', 'x'), _rule('b', 'x')]
    found = dead_rules(rules)
    assert len(found) == 1
    assert found[0]['rule_id'] == 'b'
    assert found[0]['covered_by'] == 'a'


def test_uncovered_scoped_rule_is_alive():
    """A scoped rule with no global counterpart is the only one allowing that
    action on that host, so it must not be reported."""
    rules = [_rule('a', 'x', ['prod'])]
    assert dead_rules(rules) == []


# --- overlaps ---------------------------------------------------------------

def test_allow_and_deny_on_same_action_overlap():
    allow = [_rule('a1', 'file.write')]
    deny = [_rule('d1', 'file.write')]
    found = overlaps(allow, deny)
    assert len(found) == 1
    assert found[0]['allow_rule'] == 'a1'
    assert found[0]['deny_rule'] == 'd1'
    assert 'deny wins' in found[0]['resolution']


def test_scoped_allow_and_deny_overlap_on_shared_tag():
    """Different tags still overlap: a host can carry both."""
    allow = [_rule('a1', 'x', ['prod'])]
    deny = [_rule('d1', 'x', ['db'])]
    found = overlaps(allow, deny)
    assert len(found) == 1
    assert found[0]['hosts_affected'] == ['db', 'prod']


def test_different_actions_do_not_overlap():
    assert overlaps([_rule('a', 'x')], [_rule('d', 'y')]) == []


# --- dry run ----------------------------------------------------------------

def test_dry_run_reports_deny_as_the_decider():
    results = dry_run([_rule('a1', 'file.write')], [_rule('d1', 'file.write')],
                      [{'action': 'file.write', 'hosts': ['prod']}])
    assert results[0]['verdict'] == 'denied'
    assert results[0]['decided_by'] == 'd1'


def test_dry_run_respects_host_scope():
    """A rule scoped to prod must not permit the same action on staging."""
    results = dry_run([_rule('a1', 'x', ['prod'])], [],
                      [{'action': 'x', 'hosts': ['staging']}])
    assert results[0]['verdict'] == 'not_in_list'


def test_dry_run_allows_when_scope_matches():
    results = dry_run([_rule('a1', 'x', ['prod'])], [],
                      [{'action': 'x', 'hosts': ['prod', 'db']}])
    assert results[0]['verdict'] == 'allowed'
    assert results[0]['decided_by'] == 'a1'


def test_dry_run_requires_all_tags_of_a_multi_tag_rule():
    """A rule scoped to prod+db applies only where both tags are present."""
    rules = [_rule('a1', 'x', ['prod', 'db'])]
    assert dry_run(rules, [], [{'action': 'x', 'hosts': ['prod']}])[0]['verdict'] == 'not_in_list'
    assert dry_run(rules, [], [{'action': 'x', 'hosts': ['prod', 'db']}])[0]['verdict'] == 'allowed'


def test_dry_run_skips_candidates_without_action():
    assert dry_run([], [], [{'hosts': ['prod']}, {}]) == []


def test_check_list_is_pure_mechanical():
    """No semantic verdict is claimed anywhere in the report."""
    report = check_list([_rule('a', 'x')], [_rule('d', 'x')])
    assert 'should' not in report['note'].lower().split('whether')[0]
    assert report['rule_count'] == 1


# --- HTTP surface -----------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / 'control.db'), TOKEN, 'test-model')
    yield TestClient(app)


def _admin():
    return {'Authorization': 'Bearer ' + TOKEN}


def _make_list(client, kind, name, rules):
    response = client.post('/api/v1/name-lists', headers=_admin(),
                           json={'kind': kind, 'name': name, 'rules': rules})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_check_endpoint_reports_dead_rules(env):
    list_id = _make_list(env, 'readonly', 't', [
        _rule('wide', 'read_file'), _rule('narrow', 'read_file', ['prod'])])
    response = env.post('/api/v1/name-lists/%s/check' % list_id, headers=_admin(), json={})
    assert response.status_code == 200
    body = response.json()
    assert [f['rule_id'] for f in body['dead_rules']] == ['narrow']
    assert body['list_id'] == list_id


def test_check_endpoint_finds_overlap_with_bound_deny_list(env):
    deny_id = _make_list(env, 'deny', 'deny', [_rule('d1', 'file.write')])
    allowed = _make_list(env, 'write', 'write', [_rule('a1', 'file.write')])
    env.put('/api/v1/name-list-bindings/readonly', headers=_admin(),
            json={'deny_list': deny_id})
    body = env.post('/api/v1/name-lists/%s/check' % allowed, headers=_admin(), json={}).json()
    assert len(body['overlaps']) == 1
    assert body['deny_list'] == deny_id


def test_dry_run_endpoint_summarises(env):
    list_id = _make_list(env, 'readonly', 't', [_rule('a1', 'read_file')])
    body = env.post('/api/v1/name-lists/%s/dry-run' % list_id, headers=_admin(),
                    json={'candidates': [{'action': 'read_file', 'hosts': ['prod']},
                                         {'action': 'list_dir', 'hosts': ['prod']}]}).json()
    assert body['summary'] == {'checked': 2, 'allowed': 1, 'denied': 0, 'not_in_list': 1}
    assert body['results'][0]['decided_by'] == 'a1'


def test_dry_run_never_returns_file_content(env):
    """The dry run answers 'what would the engine decide', never 'what is in
    this file' -- so no field may carry text."""
    list_id = _make_list(env, 'readonly', 't', [_rule('a1', 'read_file')])
    body = env.post('/api/v1/name-lists/%s/dry-run' % list_id, headers=_admin(),
                    json={'candidates': [{'action': 'read_file', 'hosts': []}]}).json()
    assert set(body) == {'list_id', 'deny_list', 'results', 'summary'}
    assert set(body['results'][0]) == {'action', 'hosts', 'verdict', 'decided_by', 'reason'}


def test_status_endpoint_gives_a_clean_flag(env):
    list_id = _make_list(env, 'readonly', 't', [_rule('a1', 'read_file')])
    body = env.get('/api/v1/name-lists/%s/status' % list_id, headers=_admin()).json()
    assert body['clean'] is True
    assert body['dead_rules'] == 0


def test_checks_require_admin(env):
    list_id = _make_list(env, 'readonly', 't', [_rule('a1', 'read_file')])
    assert env.post('/api/v1/name-lists/%s/check' % list_id, json={}).status_code in (401, 403)
