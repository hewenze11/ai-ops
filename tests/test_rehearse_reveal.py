"""Runs the end-to-end reveal rehearsal as part of the suite.

The rehearsal script exists for humans (clear printed steps), but the property
it checks -- the plaintext reaches the operator and nowhere else -- is exactly
the kind of thing a refactor breaks silently. Running it here means the suite
fails instead of the operator discovering it during an incident.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import rehearse_reveal  # noqa: E402


def test_reveal_path_end_to_end(tmp_path, capsys):
    assert rehearse_reveal.rehearse(tmp_path) is True, capsys.readouterr().out


def test_rehearsal_actually_detects_a_leak(tmp_path, monkeypatch, capsys):
    """The rehearsal must fail when the thing it guards against happens.

    A green run only means something if the checks can go red. This reproduces
    the original bug: the reveal result is treated like any other result, so the
    unmasked bytes are scrubbed on the way in and the endpoint can only ever
    hand back a masked copy.
    """
    from ai_ops import sensitive, scrubbing
    calls = []
    original_stash = sensitive.stash_reveal

    def scrub_first(db, *, task_id, hit_id, stdout='', stderr=''):
        # What the first cut did: store only the masked text, so "reveal" hands
        # back [REDACTED].
        calls.append(stdout)
        original_stash(db, task_id=task_id, hit_id=hit_id,
                       stdout=scrubbing.redact_text(stdout), stderr=stderr)

    monkeypatch.setattr(sensitive, 'stash_reveal', scrub_first)
    assert rehearse_reveal.rehearse(tmp_path) is False, 'the rehearsal did not notice the leak'
    assert calls, 'the leak simulation never ran'
