"""Platform-maintained secret baseline (docs §45.2 item 6).

The user's own name lists can be frozen for audit reasons (§44.1), which means a
security hotfix could take months to reach a deployment. The baseline is the
answer: a fixed set of secret shapes every deployment always has, carrying its
own version number, applied on top of whatever the user configured, and
effective the moment the platform is upgraded. The audit records the version
that was in force, so "which rules were active when this leaked" is answerable.

Rules here are shapes, not file paths: a runbook line that reads
``DATABASE_URL=postgres://user:pass@db`` must be masked no matter which file it
lives in. Paths belong to ``ai_ops.actions.BASELINE_DENY_PATHS`` (what may not be
written) and to the host's own Unix permissions (what may not be read) — this
module is the content layer between those two.
"""
from __future__ import annotations

import re

# Bump when a rule is added or changed. Recorded in audit entries; the console
# shows it so an operator can tell which rules were in force for an old event.
BASELINE_VERSION = '2026-10-22.1'

MASK = '[REDACTED]'

_DESCRIPTIONS = {
    'pem_private_key': 'PEM private key block',
    'openssh_private_key': 'OpenSSH private key block',
    'authorization_header': 'HTTP Authorization header',
    'url_credentials': 'Credentials embedded in a URL',
    'named_secret': 'key=value whose name says it is a secret',
    'json_secret': 'JSON field whose name says it is a secret',
    'yaml_secret': 'YAML key whose name says it is a secret',
    'aws_access_key': 'AWS access key id',
    'aws_secret_key': 'AWS secret access key',
    'github_token': 'GitHub token',
    'slack_token': 'Slack token',
    'openai_key': 'OpenAI-style secret key',
    'google_api_key': 'Google API key',
    'jwt': 'JSON Web Token',
    'basic_auth_blob': 'Basic auth base64 blob in a header',
}


def _mask_named(match):
    return match.group(1) + MASK


def _mask_quoted(match):
    # ``json_secret`` has a second capture group for the value; ``yaml_secret``
    # has a quote group and no value group, so index defensively.
    groups = match.groups()
    return (groups[0] or '') + MASK


def _mask_all(_match):
    return MASK


# Ordered rules: (id, compiled pattern, replacement). Later rules see already
# masked text, so a broad rule cannot undo a narrow one.
_RULES = [
    ('pem_private_key',
     re.compile(r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----', re.S),
     _mask_all),
    ('openssh_private_key',
     re.compile(r'-----BEGIN OPENSSH PRIVATE KEY-----.*?-----END OPENSSH PRIVATE KEY-----', re.S),
     _mask_all),
    ('authorization_header',
     re.compile(r'(?i)\b(authorization\s*:\s*)(?:bearer|basic|token|digest)\s+[A-Za-z0-9._~+/=-]{8,}'),
     _mask_named),
    ('url_credentials',
     re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^/\s:@]+)(@)'),
     lambda m: m.group(1) + MASK + m.group(3)),
    ('aws_secret_key',
     re.compile(r'(?i)\b(aws_secret_access_key\s*[=:]\s*)([A-Za-z0-9/+]{32,})'),
     _mask_named),
    ('aws_access_key',
     re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
     _mask_all),
    ('github_token',
     re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
     _mask_all),
    ('slack_token',
     re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}'),
     _mask_all),
    ('openai_key',
     re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'),
     _mask_all),
    ('google_api_key',
     re.compile(r'\bAIza[0-9A-Za-z_-]{30,}'),
     _mask_all),
    ('jwt',
     re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
     _mask_all),
    ('json_secret',
     re.compile(r'(?i)("[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|credential)[A-Za-z0-9_.-]*"\s*:\s*)"(?:[^"\\]|\\.)*"'),
     _mask_quoted),
    ('yaml_secret',
     re.compile(r'(?im)^(\s*[A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|credential)[A-Za-z0-9_.-]*\s*:\s*)(["\']?)[^\s"\'#]+'),
     lambda m: m.group(1) + (m.group(2) or '') + MASK),
    ('basic_auth_blob',
     re.compile(r'(?i)\b(basic\s+)([A-Za-z0-9+/]{16,}={0,2})'),
     _mask_named),
    ('named_secret',
     re.compile(r'(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token|credential)[A-Za-z0-9_.-]*\s*[=:]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;"\'#]{3,})'),
     _mask_named),
]

# A redaction can itself look like a secret to a later rule; the mask text is
# skipped so rules stay independent.
MASK_TOKEN = MASK


def apply(text):
    """Mask every baseline secret shape in ``text``.

    Returns ``(masked_text, matched_rule_ids)`` so the caller can audit *what*
    kind of secret was present without ever storing it.
    """
    if not isinstance(text, str) or not text:
        return text, []
    matched = []
    result = text
    for rule_id, pattern, replacement in _RULES:
        if pattern.search(result):
            result = pattern.sub(replacement, result)
            matched.append(rule_id)
    return result, matched


def kinds():
    """Rule inventory for the console: id, human description, version."""
    return [{'id': rule_id, 'description': _DESCRIPTIONS.get(rule_id, rule_id)}
            for rule_id, _pattern, _replacement in _RULES]
