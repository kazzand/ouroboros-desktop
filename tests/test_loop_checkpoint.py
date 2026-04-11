"""Tests for LLM loop checkpoint self-audit mechanism."""

import json
import queue
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.loop import _maybe_inject_self_check, _build_recent_tool_trace


class TestBuildRecentToolTrace:
    """Tests for _build_recent_tool_trace helper (P3 LLM-First: factual trace, no classification)."""

    def test_empty_messages(self):
        assert _build_recent_tool_trace([]) == ""

    def test_no_tool_calls(self):
        messages = [{"role": "user", "content": "hello"}]
        assert _build_recent_tool_trace(messages) == ""

    def test_builds_trace_from_tool_calls(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "repo_read", "arguments": '{"path": "a.py"}'}}
            ]},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "code_search", "arguments": '{"query": "foo"}'}}
            ]},
        ]
        result = _build_recent_tool_trace(messages)
        assert "repo_read" in result
        assert "code_search" in result
        assert "Recent tool calls" in result

    def test_repeated_calls_shown_factually(self):
        """Repeated identical calls should appear in the trace — LLM decides if it's a problem."""
        tc = {"function": {"name": "repo_read", "arguments": '{"path": "same.py"}'}}
        messages = [{"role": "assistant", "tool_calls": [tc]}] * 5
        result = _build_recent_tool_trace(messages)
        assert result.count("repo_read") == 5  # All calls shown, no classification

    def test_dict_args_serialized(self):
        tc = {"function": {"name": "run_shell", "arguments": {"cmd": ["ls"], "cwd": "/tmp"}}}
        messages = [{"role": "assistant", "tool_calls": [tc]}]
        result = _build_recent_tool_trace(messages)
        assert "run_shell" in result

    def test_long_args_truncated(self):
        long_args = json.dumps({"path": "a" * 200})
        tc = {"function": {"name": "repo_read", "arguments": long_args}}
        messages = [{"role": "assistant", "tool_calls": [tc]}]
        result = _build_recent_tool_trace(messages)
        # Args should be truncated to 80 chars in the trace
        assert "repo_read(" in result

    def test_window_limit(self):
        messages = []
        for i in range(30):
            messages.append({"role": "assistant", "tool_calls": [
                {"function": {"name": f"tool_{i}", "arguments": "{}"}}
            ]})
        result = _build_recent_tool_trace(messages, window=5)
        # Should only show last 5 calls
        assert result.count("tool_") == 5

    def test_preserves_chronological_order(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "first_tool", "arguments": "{}"}}
            ]},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "second_tool", "arguments": "{}"}}
            ]},
        ]
        result = _build_recent_tool_trace(messages)
        assert result.index("first_tool") < result.index("second_tool")

    def test_returns_string(self):
        messages = [{"role": "assistant", "tool_calls": [
            {"function": {"name": "repo_read", "arguments": "{}"}}
        ]}]
        result = _build_recent_tool_trace(messages)
        assert isinstance(result, str)


class TestMaybeInjectSelfCheck:
    """Tests for the checkpoint injection mechanism."""

    def test_no_injection_on_early_rounds(self):
        messages = []
        usage = {"cost": 0.5}
        result = _maybe_inject_self_check(3, 200, messages, usage, lambda x: None)
        assert result is False
        assert len(messages) == 0

    def test_injection_at_round_15(self):
        messages = [{"role": "user", "content": "test"}]
        usage = {"cost": 1.5}
        progress_msgs = []
        result = _maybe_inject_self_check(
            15, 200, messages, usage, progress_msgs.append
        )
        assert result is True
        assert len(messages) == 2  # original + checkpoint
        assert "CHECKPOINT" in messages[-1]["content"]
        assert len(progress_msgs) == 1
        assert "Checkpoint" in progress_msgs[0]

    def test_injection_at_round_30(self):
        messages = [{"role": "user", "content": "test"}]
        usage = {"cost": 3.0}
        result = _maybe_inject_self_check(30, 200, messages, usage, lambda x: None)
        assert result is True
        assert "CHECKPOINT 2" in messages[-1]["content"]

    def test_no_injection_at_round_1(self):
        messages = []
        usage = {"cost": 0}
        result = _maybe_inject_self_check(1, 200, messages, usage, lambda x: None)
        assert result is False

    def test_event_emission_with_queue(self):
        messages = [{"role": "user", "content": "test"}]
        usage = {"cost": 2.0}
        eq = queue.Queue()
        result = _maybe_inject_self_check(
            15, 200, messages, usage, lambda x: None,
            event_queue=eq, task_id="test-task",
        )
        assert result is True
        assert not eq.empty()
        event = eq.get_nowait()
        assert event["data"]["type"] == "task_checkpoint"
        assert event["data"]["task_id"] == "test-task"
        # Only one event on the queue — no duplicate via direct append
        assert eq.empty(), "checkpoint event must not be emitted twice"

    def test_event_no_duplicate_when_queue_present(self):
        """When queue is available, _emit_checkpoint_event must NOT also write
        to events.jsonl directly — the supervisor pipeline handles persistence."""
        import importlib
        loop_mod = importlib.import_module("ouroboros.loop")
        import queue as q
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            eq = q.Queue()
            loop_mod._emit_checkpoint_event(
                event_queue=eq,
                task_id="dup-test",
                drive_logs=drive_logs,
                data={"checkpoint": 1},
            )
            # Queue received the event
            assert not eq.empty()
            eq.get_nowait()
            # Direct file write must NOT have happened
            events_file = drive_logs / "events.jsonl"
            assert not events_file.exists(), (
                "_emit_checkpoint_event must not write events.jsonl when queue is present"
            )

    def test_event_fallback_to_file_when_no_queue(self):
        """When queue is None, checkpoint falls back to direct events.jsonl write."""
        import importlib, json, pathlib, tempfile
        loop_mod = importlib.import_module("ouroboros.loop")
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            loop_mod._emit_checkpoint_event(
                event_queue=None,
                task_id="fallback-test",
                drive_logs=drive_logs,
                data={"checkpoint": 1},
            )
            events_file = drive_logs / "events.jsonl"
            assert events_file.exists()
            line = events_file.read_text().strip()
            entry = json.loads(line)
            assert entry["type"] == "task_checkpoint"
            assert entry["task_id"] == "fallback-test"

    def test_tool_trace_included_in_checkpoint(self):
        """When tool calls exist, the factual trace should be in the checkpoint — LLM decides."""
        tc = {"function": {"name": "repo_read", "arguments": '{"path": "same.py"}'}}
        messages = [{"role": "assistant", "tool_calls": [tc]}] * 20
        usage = {"cost": 5.0}
        _maybe_inject_self_check(15, 200, messages, usage, lambda x: None)
        checkpoint = [m for m in messages if m.get("role") == "system" and "CHECKPOINT" in m.get("content", "")]
        assert len(checkpoint) == 1
        # Tool trace should be present (P3: LLM sees the facts, no Python classification)
        assert "Recent tool calls" in checkpoint[0]["content"]
        assert "repo_read" in checkpoint[0]["content"]

    def test_no_python_classification_in_checkpoint(self):
        """Checkpoint must NOT contain Python-side 'REPETITION DETECTED' — P3 LLM-First."""
        tc = {"function": {"name": "repo_read", "arguments": '{"path": "same.py"}'}}
        messages = [{"role": "assistant", "tool_calls": [tc]}] * 20
        usage = {"cost": 5.0}
        _maybe_inject_self_check(15, 200, messages, usage, lambda x: None)
        checkpoint = [m for m in messages if m.get("role") == "system" and "CHECKPOINT" in m.get("content", "")]
        assert len(checkpoint) == 1
        assert "REPETITION" not in checkpoint[0]["content"]
        assert "STRONG signal" not in checkpoint[0]["content"]

    def test_no_injection_at_non_multiple_of_15(self):
        """Checkpoint should only fire at multiples of 15."""
        for round_idx in [14, 16, 29, 31, 44, 46]:
            messages = []
            usage = {"cost": 0}
            result = _maybe_inject_self_check(round_idx, 200, messages, usage, lambda x: None)
            assert result is False, f"Should not inject at round {round_idx}"

    def test_checkpoint_includes_cost_info(self):
        """Checkpoint message should include cost and round count for observability."""
        messages = [{"role": "user", "content": "test"}]
        usage = {"cost": 7.42}
        _maybe_inject_self_check(15, 200, messages, usage, lambda x: None)
        checkpoint_content = messages[-1]["content"]
        assert "7.42" in checkpoint_content
        assert "15" in checkpoint_content
