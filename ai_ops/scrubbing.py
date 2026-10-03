"""Generic secret redaction for text that becomes durable or model-visible.

Scope, and why it is drawn here:

* The raw stdout/stderr archive (``output_chunks``) is the operator's
  byte-exact evidence and is digest-verified; it is deliberately *not* rewritten.
  Redacting it would silently destroy evidence and break its sha256 contract.
* Everything derived from a command's output that is *read back* — the inline
  ``stdout``/``stderr`` echoed into ``tasks.result`` (which is also handed to the
  model as tool output), and the text captured in ``audit`` details — is
  redacted before it is written. This is the text that leaks into model context,
  audit views and any log derived from the database.

Redaction is pattern based and therefore best-effort: it recognises common secret
shapes and operator-declared literals, but this preview does not claim to catch
every secret. The honest position stands: do not put secrets in commands, and do
not let low-privilege users read the database.
"""
import re

MASK = "[REDACTED]"

# Each entry: (pattern, replacement function). Keeping the field name or the
# URL user part preserves context so the reader knows *what* was redacted.
def _mask_named(match):
    return match.group(1) + MASK


def _mask_url(match):
    return match.group(1) + MASK + match.group(3)


def _mask_json_field(match):
    return match.group(1) + '"' + MASK + '"'


def _mask_all(match):
    return MASK


_PATTERNS = [
    # PEM private keys (any type): redact the whole block.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S), _mask_all),
    # HTTP Authorization headers, with or without a scheme.
    (re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer|basic|token)?\s*[A-Za-z0-9._~+/=-]{8,}"), _mask_named),
    # Credentials embedded in a URL: scheme://user:pass@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^/\s:@]+)(@)"), _mask_url),
    # JSON string fields whose name looks like a secret (checked before the
    # generic key=value rule so the quotes are handled correctly).
    (re.compile(r"(?i)(\"[A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_]*\"\s*:\s*)\"[^\"]*\""), _mask_json_field),
    # key=value / key: value where the key name looks like a secret.
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token)[A-Za-z0-9_]*\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"), _mask_named),
    # Provider-shaped tokens with a recognisable prefix.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})"), _mask_all),
]


def _redact_pattern(text):
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_text(text, literals=()):
    """Redact secret-shaped spans and any operator-declared literal values.

    ``literals`` are exact values the operator knows to be secret (for example a
    token handed to a command). They are replaced even when they do not match a
    known shape.
    """
    if not text:
        return text
    for literal in literals:
        if literal and len(literal) >= 6:
            text = text.replace(literal, MASK)
    return _redact_pattern(text)


_SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|auth[_-]?token)")


def _redact_value(value, literals):
    if isinstance(value, str):
        return redact_text(value, literals)
    if isinstance(value, list):
        return [_redact_value(item, literals) for item in value]
    if isinstance(value, dict):
        # A structured value is redacted by BOTH its key name and its content:
        # {"password": "..."} holds no secret-shaped string on its own, so a
        # key-name match is what protects nested payloads (alarm/audit details).
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                result[key] = MASK
            else:
                result[key] = _redact_value(item, literals)
        return result
    return value


def redact_structure(value, literals=()):
    """Recursively redact a JSON-like structure (dict/list/str leaves)."""
    return _redact_value(value, literals)


# Keys in a task result whose text is raw command output and must be scrubbed.
RESULT_TEXT_FIELDS = ("stdout", "stderr")


def redact_result(result, literals=()):
    """Scrub the model-visible inline output of a task result dict.

    Only stdout/stderr carry raw command text; other fields (exit_code,
    status, error_code, digests) are left untouched.
    """
    if not isinstance(result, dict):
        return result
    clean = dict(result)
    for field in RESULT_TEXT_FIELDS:
        if isinstance(clean.get(field), str):
            clean[field] = redact_text(clean[field], literals)
    return clean


def find_secret_kinds(text):
    """Return the names of secret patterns that match, for warnings only.

    Used to flag an archive as possibly secret-bearing without altering it.
    """
    if not text:
        return []
    names = ["private_key", "authorization_header", "url_credentials",
             "json_secret", "named_secret", "provider_token"]
    found = []
    for name, (pattern, _fn) in zip(names, _PATTERNS):
        if pattern.search(text):
            found.append(name)
    return found
