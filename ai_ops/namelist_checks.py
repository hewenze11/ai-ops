"""Mechanical checks on name lists (docs §44.2).

What this module deliberately does NOT do
-----------------------------------------
It does not judge semantics. "Should this file count as secret?" and "is it safe
to restart postgres?" are the customer's business decisions, and a tool that
guesses them will be wrong in a way that gets switched off. Everything here is
*mechanical*: set relations over (action, host-scope) pairs, and dry-run counts.

The three checks
----------------
1. **Dead rule** -- a rule whose (action, scope) is already completely covered by
   a wider rule in the same list. It can never be the reason something is
   allowed, so it is harmless but misleading: an operator who adds it believes
   they changed something.
2. **Overlap** -- the same (action, scope intersection) appears in both an allow
   list and the deny list. This is the case where the operator's intent is
   genuinely ambiguous and only they can resolve it. The platform's ruling is
   fixed (deny wins, §44.2), so the check is informational -- but it must be
   visible, because "I allowed it and it still got blocked" is the complaint
   that erodes trust in the whole control.
3. **Dry run** -- given a list and a set of candidate actions, report which rule
   permits each one. Counts and rule ids only; never file contents.

Why scope comparison is exact rather than heuristic
---------------------------------------------------
A rule's scope is a list of host tags, absent meaning "everywhere". There is no
glob over tags, so coverage is a plain subset relation:

  * ``[]`` (everywhere) covers every scope.
  * ``[a, b]`` covers ``[a]``, ``[a, b]``, and ``[]``? No -- ``[a,b]`` applies
    only where *both* tags are present, so it covers a narrower set of hosts
    than ``[a]``.
"""
from __future__ import annotations

import json

from fastapi import Depends


def _scope_set(hosts):
    """A rule's scope as a set of tags. Empty set means 'everywhere'."""
    return frozenset(h for h in (hosts or []) if h)


def _covers(wide, narrow):
    """True when every host ``narrow`` applies to is also covered by ``wide``.

    ``wide`` empty = everywhere, which covers everything.
    Otherwise ``wide`` applies where all its tags are present, so it covers
    ``narrow`` only if ``wide``'s tags are a subset of ``narrow``'s.
    """
    wide, narrow = _scope_set(wide), _scope_set(narrow)
    if not wide:
        return True
    return wide <= narrow


def _describe(hosts):
    scope = sorted(_scope_set(hosts))
    return 'all hosts' if not scope else 'hosts tagged ' + ' + '.join(scope)


def dead_rules(rules):
    """Rules that can never be the admitting rule, because a wider one exists.

    Self-covered duplicates (two rules with the same action and scope) are also
    reported: one of them is redundant, and the audit cannot tell which one the
    operator meant to keep.
    """
    findings = []
    for rule in rules:
        for other in rules:
            if other['rule_id'] == rule['rule_id']:
                continue
            if other['action'] != rule['action']:
                continue
            if not _covers(other.get('hosts'), rule.get('hosts')):
                continue
            # Equal scope in both directions: report only once. Mark the *later*
            # rule id as the redundant one, so a duplicate pair is one finding
            # and the survivor is the rule that was there first.
            if _covers(rule.get('hosts'), other.get('hosts')) and other['rule_id'] > rule['rule_id']:
                continue
            findings.append({
                'rule_id': rule['rule_id'],
                'action': rule['action'],
                'scope': _describe(rule.get('hosts')),
                'covered_by': other['rule_id'],
                'covered_by_scope': _describe(other.get('hosts')),
                'why': 'rule %s on %s already allows %s on %s, which includes this rule'
                       % (other['rule_id'], _describe(other.get('hosts')),
                          other['action'], _describe(rule.get('hosts'))),
            })
            break
    return findings


def overlaps(allow_rules, deny_rules):
    """(action, overlapping scope) pairs present in both an allow and deny list.

    The engine resolves these as deny-wins, which is the correct security
    answer but the wrong *surprise*: the operator explicitly allowed something
    and it still gets blocked. Reporting it is the difference between "secure"
    and "secure and comprehensible".
    """
    findings = []
    for allowed in allow_rules:
        for denied in deny_rules:
            if allowed['action'] != denied['action']:
                continue
            scope = _intersect(allowed.get('hosts'), denied.get('hosts'))
            if scope is None:
                continue
            findings.append({
                'action': allowed['action'],
                'allow_rule': allowed['rule_id'],
                'deny_rule': denied['rule_id'],
                'hosts_affected': sorted(scope),
                'scope': _describe(sorted(scope)),
                'resolution': 'deny wins: %s is blocked on %s despite being allowed'
                              % (allowed['action'], _describe(sorted(scope))),
            })
    return findings


def _intersect(a, b):
    """Hosts where both rules apply, or None when they cannot both apply.

    Empty set on one side means everywhere, so the intersection is the other.
    Two non-empty sets intersect where their union is present -- a host tagged
    both, which is expressible, so this is a real overlap and not hypothetical.
    """
    left, right = _scope_set(a), _scope_set(b)
    if not left and not right:
        return frozenset()
    if not left:
        return right
    if not right:
        return left
    return left | right


def check_list(rules, deny_rules=None):
    """Run the mechanical checks for one list, optionally against the deny list."""
    return {'dead_rules': dead_rules(rules),
            'overlaps': overlaps(rules, deny_rules or []),
            'rule_count': len(rules),
            'note': 'Mechanical checks only: set relations over action and host scope. '
                    'Whether a rule *should* exist is your call.'}


def dry_run(rules, deny_rules, candidates):
    """Which rule decides each candidate action, given these lists.

    ``candidates`` is a list of ``{'action': ..., 'hosts': [...]}`` -- the host
    tag set of the machine the operator is testing against. Returns the ruling
    per candidate plus the rule that produced it, which is what makes the answer
    actionable ("blocked by deny rule d-3", not just "blocked").

    There is no file content in or out: this answers "what would the engine
    decide", not "what is in this file".
    """
    results = []
    for candidate in candidates or []:
        action = str((candidate or {}).get('action') or '')
        host_tags = [h for h in ((candidate or {}).get('hosts') or []) if h]
        if not action:
            continue
        deny_hit = _first_match(deny_rules, action, host_tags)
        allow_hit = _first_match(rules, action, host_tags)
        if deny_hit:
            # Fixed ruling (§44.2): deny covers allow, no adjudication needed.
            verdict, reason = 'denied', 'deny rule %s (%s)' % (deny_hit['rule_id'], _describe(deny_hit.get('hosts')))
        elif allow_hit:
            verdict, reason = 'allowed', 'rule %s (%s)' % (allow_hit['rule_id'], _describe(allow_hit.get('hosts')))
        else:
            verdict, reason = 'not_in_list', 'no rule permits %s on these hosts' % action
        results.append({'action': action, 'hosts': host_tags, 'verdict': verdict,
                        'decided_by': (deny_hit or allow_hit or {}).get('rule_id'),
                        'reason': reason})
    return results


def _first_match(rules, action, host_tags):
    """The first rule that applies to this action on a host with these tags."""
    present = set(host_tags)
    for rule in rules:
        if rule['action'] != action:
            continue
        scope = _scope_set(rule.get('hosts'))
        if not scope or scope <= present:
            return rule
    return None


def install(app, transaction, audit, admin):
    from . import namelists

    @app.post('/api/v1/name-lists/{list_id}/check', dependencies=[Depends(admin)])
    def check(list_id: str, body: dict | None = None):
        """Mechanical check on a saved list. ``body.deny_list`` picks the deny list
        to test overlap against; omitted means the currently bound one."""
        body = body or {}
        with transaction(write=False) as db:
            target = namelists.view(db, list_id)
            deny_id = body.get('deny_list')
            if deny_id is None:
                deny_id = namelists.snapshot_binding(db, 'readonly').get('deny_list')
            deny_rules = namelists.view(db, deny_id)['rules'] if deny_id else []
            report = check_list(target['rules'], deny_rules)
            report['list_id'] = list_id
            report['deny_list'] = deny_id
            return report

    @app.post('/api/v1/name-lists/{list_id}/dry-run', dependencies=[Depends(admin)])
    def dry(list_id: str, body: dict):
        """Dry run: which rule decides each candidate action on a given host."""
        # A dry run writes an audit record, so it needs a writable transaction:
        # "who tested which list against which hosts" is itself worth knowing,
        # even though nothing was executed.
        with transaction() as db:
            target = namelists.view(db, list_id)
            deny_id = (body or {}).get('deny_list')
            if deny_id is None:
                deny_id = namelists.snapshot_binding(db, 'readonly').get('deny_list')
            deny_rules = namelists.view(db, deny_id)['rules'] if deny_id else []
            candidates = (body or {}).get('candidates') or []
            if len(candidates) > 200:
                candidates = candidates[:200]
            results = dry_run(target['rules'], deny_rules, candidates)
            audit(db, 'name_list.dry_run', list_id, 'admin',
                  {'candidates': len(results), 'deny_list': deny_id})
            allowed = sum(1 for r in results if r['verdict'] == 'allowed')
            denied = sum(1 for r in results if r['verdict'] == 'denied')
            return {'list_id': list_id, 'deny_list': deny_id, 'results': results,
                    'summary': {'checked': len(results), 'allowed': allowed,
                                'denied': denied,
                                'not_in_list': len(results) - allowed - denied}}

    @app.get('/api/v1/name-lists/{list_id}/status', dependencies=[Depends(admin)])
    def status(list_id: str):
        """Cheap summary for the console badge: how many findings, no detail."""
        with transaction(write=False) as db:
            target = namelists.view(db, list_id)
            deny_id = namelists.snapshot_binding(db, 'readonly').get('deny_list')
            deny_rules = namelists.view(db, deny_id)['rules'] if deny_id else []
            report = check_list(target['rules'], deny_rules)
            return {'list_id': list_id,
                    'clean': not report['dead_rules'] and not report['overlaps'],
                    'dead_rules': len(report['dead_rules']),
                    'overlaps': len(report['overlaps'])}
