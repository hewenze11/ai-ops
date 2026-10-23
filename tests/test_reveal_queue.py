"""Two structural guards around the reveal path.

Both check source shape rather than behaviour, which is unusual and deliberate:
the properties are "the model cannot set the task kind" and "a reveal never
jumps the queue", and neither is observable from an API call today. Pinning
them here means a refactor that quietly breaks them fails a test instead of a
reviewer's memory.
"""


def test_model_cannot_smuggle_a_kind_into_params():
    """`params` is copied from model arguments, so the model writes to it.

    The task kind is hardcoded by the dispatcher today, which is why this is not
    exploitable yet. But a model-supplied `kind` sitting in the payload is
    exactly what a later "just merge the payload" refactor would honour, so the
    reserved names are dropped at the boundary instead of being trusted to stay
    harmless.
    """
    from ai_ops import turns
    source = open(turns.__file__, encoding='utf-8').read()
    assert '_RESERVED' in source
    block = source.split('_RESERVED = (')[1].split(')')[0]
    for name in ('kind', 'reveal', 'hit_id', 'source_turn_id'):
        assert name in block, name
    # And the filter is actually used, not just declared.
    assert 'if k not in _RESERVED' in source


def test_reveal_task_never_jumps_the_queue():
    """A human staring at an unlock card must not stall the diagnosis.

    Reveal tasks have no parent turn, so they are only claimed when nothing else
    is executing. Checked at the source level because the rule lives in the
    claim query, not in a helper that could be called elsewhere.
    """
    from ai_ops import app as app_module
    source = open(app_module.__file__, encoding='utf-8').read()
    claim = source.split('def claim(asset_id')[1].split('@app.post')[0]
    assert 'reveal_row' in claim, 'the reveal claim branch is gone'
    # The branch is inside `if row is None`: model work always wins.
    assert 'if row is None:' in claim
    assert claim.index('if row is None:') < claim.index('reveal_row')
    # And it refuses while anything is in flight.
    assert 'busy is None' in claim
    assert 'awaiting_approval' in claim and 'claimed' in claim
