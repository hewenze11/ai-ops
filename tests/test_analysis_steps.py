"""分析步骤 derivation: the transcript becomes a user-facing step list.

These tests pin the *derivation* contract (turns.py: derive_steps). The stored
transcript stays the single source of truth; steps are never persisted.
"""
import json

from ai_ops.turns import derive_steps


def _assistant(text=None, tool=None, call_id="call-1"):
    message = {"role": "assistant", "content": text}
    if tool:
        message["tool_calls"] = [{"id": call_id, "type": "function",
                                  "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]
    return message


def test_thought_then_command_then_result_is_ordered():
    messages = [
        _assistant("先确认注册资产上的执行账号。", tool=("execute_command",
                   {"asset_id": "agent-1", "run_as": "ops_read", "command": "id -un"}), call_id="c1"),
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"status": "succeeded", "exit_code": 0, "stdout": "ops_read\n"})},
    ]
    steps = derive_steps(messages)
    assert [s["kind"] for s in steps] == ["thought", "command"]
    thought, command = steps
    assert thought["text"].startswith("先确认")
    assert command["command"] == "id -un" and command["run_as"] == "ops_read"
    assert command["status"] == "succeeded" and command["exit_code"] == 0
    assert "ops_read" in command["output_excerpt"]


def test_assistant_text_is_surfaced_but_empty_is_skipped():
    messages = [
        {"role": "assistant", "content": "   "},                       # blank -> skipped
        _assistant("第二步：查看日志。", tool=("execute_command", {"command": "tail -n 5 app.log"}), call_id="c1"),
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "succeeded"})},
    ]
    steps = derive_steps(messages)
    # A blank assistant turn contributes no empty thought step.
    assert [s["kind"] for s in steps] == ["thought", "command"]
    assert steps[0]["text"] == "第二步：查看日志。"


def test_truncated_output_is_flagged():
    messages = [
        _assistant(None, tool=("execute_command", {"command": "cat huge.log"}), call_id="c1"),
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"status": "succeeded", "output_truncated": True})},
    ]
    steps = derive_steps(messages)
    assert steps[0]["truncated"] is True


def test_unknown_task_result_keeps_its_status_and_error():
    messages = [
        _assistant(None, tool=("execute_command", {"command": "rm -rf /tmp/x"}), call_id="c1"),
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"status": "unknown", "error": "EXECUTION_UNKNOWN"})},
    ]
    steps = derive_steps(messages)
    assert steps[0]["status"] == "unknown"
    assert steps[0]["error"] == "EXECUTION_UNKNOWN"


def test_search_tools_become_search_steps():
    messages = [
        _assistant("先查一下这个报错。", tool=("web_search", {"query": "OOMKilled nginx"}), call_id="c1"),
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"results": []})},
    ]
    steps = derive_steps(messages)
    assert steps[1]["kind"] == "search" and steps[1]["query"] == "OOMKilled nginx"
    assert steps[1]["status"] == "done"


def test_proposed_command_without_result_stays_proposed():
    # In confirm mode the command is proposed and awaiting approval: no tool
    # message yet, so the step must not claim it ran.
    messages = [_assistant("需要改配置。", tool=("execute_command", {"command": "systemctl restart x"}), call_id="c1")]
    steps = derive_steps(messages)
    command = [s for s in steps if s["kind"] == "command"][0]
    assert command["status"] == "proposed" and command["output_excerpt"] is None


def test_malformed_tool_arguments_do_not_crash():
    messages = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "execute_command", "arguments": "not json"}}]}]
    steps = derive_steps(messages)
    assert steps and steps[0]["kind"] == "command" and steps[0]["command"] is None


def test_empty_transcript_yields_no_steps():
    assert derive_steps([]) == []
    assert derive_steps(None) == []
