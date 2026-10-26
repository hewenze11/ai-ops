"""A read is not the thing being approved, so it must run in every mode.

This file exists because the same bug was fixed twice in two different places:
first the tool was withheld from confirm/direct turns, then (after that was
fixed) the claim check still refused to authorise the read task those turns
produced. The join between "the model may ask for a read" and "the agent may
claim that read" is where the two halves of the pipeline disagreed, so it gets
its own tests rather than living inside the mode or the claim suite.
"""
import json
import time

import pytest

from ai_ops.authority import claim_allowed
from ai_ops.turns import READONLY_TOOLS, tool_spec


def names(body):
    return [t["function"]["name"] for t in (body.get("tools") or [])]


def test_read_tools_are_offered_in_every_mode():
    for mode in ("readonly", "confirm", "direct"):
        offered = set(names({"tools": tool_spec(["ops_read"], None, mode)}))
        assert set(READONLY_TOOLS) <= offered, mode
        assert "read_many" in offered, mode


def test_only_write_modes_offer_a_change_tool():
    assert "propose_change" not in names({"tools": tool_spec(["ops_read"], None, "readonly")})
    assert "propose_change" not in names({"tools": tool_spec([], None, "confirm")})
    for mode in ("confirm", "direct"):
        assert "propose_change" in names({"tools": tool_spec(["ops_read"], None, mode)}), mode


class _Row(dict):
    """sqlite3.Row behaves like a mapping with attribute access in these tests."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class FakeDb:
    def __init__(self, asset, turn):
        self.asset = asset
        self.turn = turn

    def execute(self, sql, params=()):
        if "FROM assets" in sql:
            return _Result(self.asset)
        if "FROM role_turns" in sql:
            return _Result(self.turn)
        return _Result(None)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def make(asset_users=("ops_read",), turn_users=("ops_read",), mode="confirm", asset_mode=None):
    asset = _Row({"id": "web1", "retired_at": None, "connection_type": "agent",
                  "allowed_users": json.dumps(list(asset_users))})
    turn = _Row({"id": "t1", "mode": mode, "execution_users": json.dumps(list(turn_users))})
    db = FakeDb(asset, turn)
    task = _Row({"id": "task1", "asset_id": "web1", "turn_id": "t1", "created_at": time.time(),
                 "payload": json.dumps({"kind": "readonly", "run_as": "ops_read",
                                        "mode": asset_mode or mode})})
    return db, task


@pytest.fixture(autouse=True)
def allow_permits(monkeypatch):
    """`permits` reads turn_authority, which these minimal rows do not model.

    The claim check's own responsibility is the mode/account join; the authority
    snapshot has its own suite.
    """
    import ai_ops.account_grants as grants
    monkeypatch.setattr(grants, "permits", lambda db, turn_id, asset_id, run_as: True)


@pytest.mark.parametrize("mode", ["readonly", "confirm", "direct"])
def test_a_read_is_claimable_in_every_mode(mode):
    db, task = make(mode=mode)
    assert claim_allowed(db, task) is True, mode


def test_a_read_is_refused_when_the_account_was_not_granted_for_the_turn():
    db, task = make(mode="confirm", turn_users=())
    assert claim_allowed(db, task) is False


def test_a_read_is_refused_when_the_account_is_not_allowed_on_the_asset():
    db, task = make(mode="confirm", asset_users=("someone_else",))
    assert claim_allowed(db, task) is False


def test_a_read_is_refused_for_a_retired_asset():
    db, task = make(mode="confirm")
    db.asset["retired_at"] = 1.0
    assert claim_allowed(db, task) is False


@pytest.mark.parametrize("mode", ["confirm", "direct"])
def test_a_change_is_claimable_in_its_own_write_mode(mode):
    db, task = make(mode=mode)
    task["payload"] = json.dumps({"kind": "shell", "run_as": "ops_read", "mode": mode})
    assert claim_allowed(db, task) is True, mode


def test_a_change_is_never_claimable_from_a_readonly_turn():
    db, task = make(mode="readonly")
    task["payload"] = json.dumps({"kind": "shell", "run_as": "ops_read", "mode": "readonly"})
    assert claim_allowed(db, task) is False


def test_a_change_cannot_run_under_a_different_mode_than_it_was_proposed_in():
    # Approved under direct, the turn later resolves to confirm: the payload's
    # recorded mode no longer matches, so the agent must not claim it.
    db, task = make(mode="confirm")
    task["payload"] = json.dumps({"kind": "shell", "run_as": "ops_read", "mode": "direct"})
    assert claim_allowed(db, task) is False
