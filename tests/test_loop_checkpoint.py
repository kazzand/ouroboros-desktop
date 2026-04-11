"""Tests for the simplified LLM loop checkpoint self-audit mechanism.

The checkpoint fires every 15 rounds, asks the LLM to write a free-form
reflection and continue with a tool call.  No parsing is involved —
the reflection lives in the transcript as natural assistant content.
"""

import json
import pathlib
import queue
import tempfile

import pytest

from ouroboros.loop import (
    _maybe_inject_self_check,
    _build_recent_tool_trace,
    _emit_checkpoint_reflection_event,
)


class TestBuildRecentToolTrace:
    """Tests for _build_recent_tool_trace helper (P3 LLM-First: factual trace)."""

    def test_empty_messages(self):
        assert _build_recent_tool_trace([]) == ""

    def test_no_tool_calls(self):
        assert _build_recent_tool_trace([{"role": "user", "content": "hello"}]) == ""

    def test_builds_trace(self):
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
        tc = {"function": {"name": "repo_read", "arguments": '{"path": "same.py"}'}}
        messages = [{"role": "assistant", "tool_calls": [tc]}] * 5
        result = _build_recent_tool_trace(messages)
        assert result.count("repo_read") == 5  # all shown, no Python-side classification

    def test_window_limit(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": f"tool_{i}", "arguments": "{}"}}
            ]} for i in range(30)
        ]
        result = _build_recent_tool_trace(messages, window=5)
        assert result.count("tool_") == 5


class TestMaybeInjectSelfCheck:
    """Checkpoint injection cadence and content."""

    def test_injection_at_round_15(self):
        messages = [{"role": "user", "content": "test"}]
        usage = {"cost": 1.5}
        progress = []
        result = _maybe_inject_self_check(15, 200, messages, usage, progress.append)
        assert result is True
        assert len(messages) == 2
        assert "CHECKPOINT" in messages[-1]["content"]
        assert len(progress) == 1

    def test_injection_at_round_30(self):
        messages = [{"role": "user", "content": "test"}]
        result = _maybe_inject_self_check(30, 200, messages, {"cost": 3.0}, lambda x: None)
        assert result is True
        assert "CHECKPOINT 2" in messages[-1]["content"]

    def test_no_injection_on_early_rounds(self):
        for r in [1, 2, 14, 16, 29, 31]:
            messages = []
            result = _maybe_inject_self_check(r, 200, messages, {"cost": 0}, lambda x: None)
            assert result is False, f"Should not inject at round {r}"
            assert len(messages) == 0

    def test_prompt_asks_for_reflection_and_tool_call(self):
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "CHECKPOINT_REFLECTION:" in content
        assert "tool call" in content.lower()

    def test_prompt_allows_final_answer(self):
        """Prompt must explicitly allow finishing instead of forcing a tool call.

        The runtime treats text-only checkpoint responses as task completion, so
        the prompt must tell the model this is valid — otherwise the model would be
        confused about why its final answer was accepted on a checkpoint round.
        """
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "genuinely complete" in content or "final answer" in content

    def test_prompt_contains_cost_and_round(self):
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 7.42}, lambda x: None)
        content = messages[-1]["content"]
        assert "7.42" in content
        assert "15" in content

    def test_event_emitted_to_queue(self):
        messages = [{"role": "user", "content": "test"}]
        eq = queue.Queue()
        _maybe_inject_self_check(
            15, 200, messages, {"cost": 2.0}, lambda x: None,
            event_queue=eq, task_id="t1",
        )
        assert not eq.empty()
        event = eq.get_nowait()
        assert event["data"]["type"] == "task_checkpoint"
        assert event["data"]["task_id"] == "t1"
        assert eq.empty(), "must not emit twice"

    def test_event_fallback_to_file_when_no_queue(self):
        messages = [{"role": "user", "content": "test"}]
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            _maybe_inject_self_check(
                15, 200, messages, {"cost": 1.0}, lambda x: None,
                event_queue=None, task_id="t2", drive_logs=drive_logs,
            )
            ef = drive_logs / "events.jsonl"
            assert ef.exists()
            entry = json.loads(ef.read_text().strip())
            assert entry["type"] == "task_checkpoint"


class TestEmitCheckpointReflectionEvent:
    """Reflection event emission — full content, no truncation."""

    def test_emits_to_queue(self):
        eq = queue.Queue()
        _emit_checkpoint_reflection_event("My reflection text", 15, "t1", eq, None)
        assert not eq.empty()
        event = eq.get_nowait()
        assert event["data"]["type"] == "task_checkpoint_reflection"
        assert event["data"]["reflection"] == "My reflection text"
        assert event["data"]["round"] == 15

    def test_fallback_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            _emit_checkpoint_reflection_event("Full reflection", 30, "t2", None, drive_logs)
            ef = drive_logs / "events.jsonl"
            assert ef.exists()
            entry = json.loads(ef.read_text().strip())
            assert entry["type"] == "task_checkpoint_reflection"
            assert entry["reflection"] == "Full reflection"
            assert entry["round"] == 30

    def test_full_content_no_truncation(self):
        """Reflection must be stored in full (P1 Continuity — no silent truncation)."""
        long_text = "x" * 5000
        eq = queue.Queue()
        _emit_checkpoint_reflection_event(long_text, 15, "t3", eq, None)
        event = eq.get_nowait()
        assert event["data"]["reflection"] == long_text

    def test_empty_content_graceful(self):
        """Empty content must not raise and must still emit a valid event."""
        eq = queue.Queue()
        _emit_checkpoint_reflection_event("", 15, "t4", eq, None)
        assert not eq.empty()
        event = eq.get_nowait()
        assert event["data"]["reflection"] == ""
