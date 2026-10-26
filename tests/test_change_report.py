"""The Agent's per-step change report is bookkeeping, never authority.

Background: the Agent posts one result per task, and for a change set it also
sends `change_report` — its own account of which step did what. The service had
no such field, so a strictly-typed Result rejected the whole report with 422 and
the Agent retried that same rejected body forever, wedging the asset: no task
could be claimed afterwards because the previous one never left `claimed`.

These tests pin the field's meaning so accepting it does not quietly turn into
trusting it.
"""
import pytest
from pydantic import ValidationError

from ai_ops.app import Result


BASE = {
    "claim_id": "c" * 32,
    "status": "succeeded",
    "exit_code": 0,
    "stdout": "done",
    "stderr": "",
}


def test_a_change_report_is_accepted_and_kept_verbatim():
    report = [{"step": 1, "action": "disk.clean_temp", "status": "succeeded",
               "exit_code": 0, "output": ""}]
    result = Result(**BASE, change_report=report)
    assert result.change_report == report


def test_a_result_without_a_change_report_still_parses():
    assert Result(**BASE).change_report == []


def test_a_report_that_contradicts_the_overall_status_is_refused():
    # Claiming "succeeded" while reporting a failed step would let an Agent
    # launder a failure into a success.
    report = [{"step": 1, "action": "service.restart", "status": "failed", "exit_code": 1}]
    with pytest.raises(ValidationError):
        Result(**BASE, change_report=report)


def test_a_failed_overall_status_may_still_carry_a_failed_step():
    report = [{"step": 1, "action": "service.restart", "status": "failed", "exit_code": 1}]
    result = Result(**{**BASE, "status": "failed", "exit_code": 1}, change_report=report)
    assert result.change_report[0]["status"] == "failed"


def test_succeeded_still_requires_a_zero_exit_code():
    with pytest.raises(ValidationError):
        Result(**{**BASE, "exit_code": 2})


def test_a_report_longer_than_a_change_set_is_refused():
    from ai_ops.actions import MAX_ACTIONS
    too_long = [{"step": i, "status": "succeeded"} for i in range(MAX_ACTIONS + 1)]
    with pytest.raises(ValidationError):
        Result(**BASE, change_report=too_long)


def test_unknown_fields_are_still_refused():
    # Accepting one new field must not turn the model permissive.
    with pytest.raises(ValidationError):
        Result(**BASE, something_invented="x")
