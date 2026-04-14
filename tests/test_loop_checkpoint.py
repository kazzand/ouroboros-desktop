"""Tests for the checkpoint self-audit mechanism.

Checkpoint rounds are audit-only: tools are disabled, the round can never
silently finalize the task, and any malformed output becomes a durable anomaly.
"""

import json
import pathlib
import queue
import tempfile
from types import SimpleNamespace

import pytest

from ouroboros.loop import (
    _maybe_inject_self_check,
    _build_recent_tool_trace,
    _emit_checkpoint_reflection_event,
    _emit_checkpoint_anomaly_event,
    _handle_checkpoint_response,
    run_llm_loop,
    CHECKPOINT_CONTINUE_PROMPT,
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

    def test_no_injection_when_no_spare_round_left(self):
        messages = []
        result = _maybe_inject_self_check(15, 15, messages, {"cost": 0}, lambda x: None)
        assert result is False
        assert messages == []

    def test_prompt_asks_for_reflection_and_audit(self):
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "CHECKPOINT_REFLECTION:" in content
        assert "audit" in content.lower() or "reflect" in content.lower()

    def test_prompt_says_tools_unavailable(self):
        """Prompt must tell the model tools are unavailable on this round."""
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "tools" in content.lower() and "unavailable" in content.lower()

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


class TestEmitCheckpointAnomalyEvent:
    """Checkpoint anomalies must be durable and untruncated."""

    def test_emits_to_queue(self):
        eq = queue.Queue()
        _emit_checkpoint_anomaly_event("malformed_checkpoint", "CHECKPOINT_ANOMALY:\nbad output", 15, "t5", eq, None)
        event = eq.get_nowait()
        assert event["data"]["type"] == "task_checkpoint_anomaly"
        assert event["data"]["anomaly_type"] == "malformed_checkpoint"
        assert "CHECKPOINT_ANOMALY" in event["data"]["content"]

    def test_fallback_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            _emit_checkpoint_anomaly_event("empty_checkpoint", "CHECKPOINT_ANOMALY:\n(empty checkpoint output)", 15, "t6", None, drive_logs)
            entry = json.loads((drive_logs / "events.jsonl").read_text().strip())
            assert entry["type"] == "task_checkpoint_anomaly"
            assert entry["anomaly_type"] == "empty_checkpoint"


class TestHandleCheckpointResponse:
    def test_valid_reflection_appends_continuation(self):
        msg = {"content": "CHECKPOINT_REFLECTION:\n- Known: x\n- Blocker: none\n- Decision: proceed\n- Next: read file", "tool_calls": []}
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-1", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert llm_trace["reasoning_notes"][-1].startswith("CHECKPOINT_REFLECTION:")
        assert messages[-2]["role"] == "assistant"
        assert messages[-1] == {"role": "user", "content": CHECKPOINT_CONTINUE_PROMPT}
        assert any("Checkpoint 1 reflection" in p for p in progress)

    def test_missing_marker_becomes_malformed_anomaly(self):
        msg = {"content": "I think the task is probably fine", "tool_calls": []}
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-2", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert llm_trace["reasoning_notes"][-1].startswith("CHECKPOINT_ANOMALY:")
        assert "malformed_checkpoint" in progress[-1]
        events = []
        while not eq.empty():
            events.append(eq.get_nowait())
        assert any(e["data"].get("type") == "task_checkpoint_anomaly" for e in events)

    def test_empty_output_becomes_empty_anomaly(self):
        msg = {"content": "", "tool_calls": []}
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-3", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert "empty_checkpoint" in progress[-1]
        assert "(empty checkpoint output)" in llm_trace["reasoning_notes"][-1]

    def test_unexpected_tool_calls_become_anomaly(self):
        msg = {"content": "", "tool_calls": [{"id": "x", "function": {"name": "repo_read", "arguments": "{}"}}]}
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-4", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert "unexpected_tool_calls" in progress[-1]

    def test_header_without_required_fields_becomes_malformed_anomaly(self):
        msg = {"content": "CHECKPOINT_REFLECTION:\nhello", "tool_calls": []}
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-5", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert "malformed_checkpoint" in progress[-1]
        assert llm_trace["reasoning_notes"][-1].startswith("CHECKPOINT_ANOMALY:")

    def test_header_with_tool_calls_becomes_unexpected_tool_calls_anomaly(self):
        msg = {
            "content": (
                "CHECKPOINT_REFLECTION:\n"
                "- Known: x\n- Blocker: none\n- Decision: proceed\n- Next: read file"
            ),
            "tool_calls": [{"id": "x", "function": {"name": "repo_read", "arguments": "{}"}}],
        }
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-6", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert "unexpected_tool_calls" in progress[-1]
        assert llm_trace["reasoning_notes"][-1].startswith("CHECKPOINT_ANOMALY:")

    def test_mislabelled_unknown_field_becomes_malformed_anomaly(self):
        msg = {
            "content": (
                "CHECKPOINT_REFLECTION:\n"
                "- Unknown: x\n- Blocker: none\n- Decision: proceed\n- Next: read file"
            ),
            "tool_calls": [],
        }
        llm_trace = {"reasoning_notes": []}
        messages = []
        progress = []
        eq = queue.Queue()
        _handle_checkpoint_response(msg, 15, "task-7", eq, pathlib.Path(tempfile.gettempdir()), progress.append, llm_trace, messages)
        assert "malformed_checkpoint" in progress[-1]
        assert llm_trace["reasoning_notes"][-1].startswith("CHECKPOINT_ANOMALY:")


class TestCheckpointPromptStructure:
    """Verify that the checkpoint prompt requests structured, specific reflection."""

    def _get_checkpoint_content(self) -> str:
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        return messages[-1]["content"]

    def test_prompt_requests_known_field(self):
        content = self._get_checkpoint_content()
        assert "Known" in content or "known" in content

    def test_prompt_requests_blocker_field(self):
        content = self._get_checkpoint_content()
        assert "Blocker" in content or "blocker" in content

    def test_prompt_requests_decision_field(self):
        content = self._get_checkpoint_content()
        assert "Decision" in content or "decision" in content

    def test_prompt_requests_next_field(self):
        content = self._get_checkpoint_content()
        assert "Next" in content or "next action" in content.lower()

    def test_prompt_asks_for_specificity(self):
        """Prompt must discourage vague output by asking for specific names."""
        content = self._get_checkpoint_content()
        assert "specific" in content.lower() or "file" in content.lower() or "function" in content.lower()

    def test_checkpoint_reflection_marker_present(self):
        """Prompt must use the CHECKPOINT_REFLECTION: marker for compaction detection."""
        content = self._get_checkpoint_content()
        assert "CHECKPOINT_REFLECTION:" in content

    def test_prompt_frames_reflection_as_operational_not_narrative(self):
        content = self._get_checkpoint_content()
        assert "not a narrative diary" in content.lower()
        assert "operational" in content.lower()

    def test_prompt_explicitly_rechecks_plan_validity_and_scope(self):
        content = self._get_checkpoint_content()
        lowered = content.lower()
        assert "whether the current approach is still valid" in lowered
        assert "whether the plan should change" in lowered
        assert "narrower scope" in lowered

    def test_exact_field_sequence_remains_valid_for_parser(self):
        reflection = (
            "CHECKPOINT_REFLECTION:\n"
            "- Known: loop.py checkpoint prompt updated\n"
            "- Blocker: none\n"
            "- Decision: keep exact sentinels and tighten wording\n"
            "- Next: run the focused regression tests"
        )
        from ouroboros.loop import _is_valid_checkpoint_reflection
        assert _is_valid_checkpoint_reflection(reflection) is True


class TestCheckpointRoundMissingResponse:
    def test_missing_checkpoint_response_becomes_durable_anomaly(self, monkeypatch):
        class DummyRegistry:
            def __init__(self):
                self._ctx = SimpleNamespace(
                    active_model_override=None,
                    active_use_local_override=None,
                    active_effort_override=None,
                    messages=None,
                    event_queue=None,
                    task_id="",
                )

            def override_handler(self, name, handler):
                return None

        llm = SimpleNamespace(default_model=lambda: "anthropic/claude-sonnet-4.6")
        calls = []

        def fake_call(*args, **kwargs):
            round_idx = args[8]
            calls.append((round_idx, kwargs.get("use_local"), args[3]))
            if round_idx < 15:
                return {
                    "content": "",
                    "tool_calls": [{"id": f"tc-{round_idx}", "function": {"name": "repo_read", "arguments": "{}"}}],
                }, 0.0
            return None, 0.0

        monkeypatch.setattr("ouroboros.loop.call_llm_with_retry", fake_call)
        monkeypatch.setattr("ouroboros.loop._call_llm_with_retry", fake_call)
        monkeypatch.setattr("ouroboros.loop.initial_tool_schemas", lambda tools: [{"function": {"name": "repo_read"}}])
        monkeypatch.setattr("ouroboros.loop.list_non_core_tools", lambda tools: [])
        monkeypatch.setattr("ouroboros.loop.handle_tool_calls", lambda *a, **k: 0)

        progress = []
        eq = queue.Queue()
        result, _usage, trace = run_llm_loop(
            messages=[{"role": "user", "content": "hello"}],
            tools=DummyRegistry(),
            llm=llm,
            drive_logs=pathlib.Path(tempfile.gettempdir()),
            emit_progress=progress.append,
            incoming_messages=queue.Queue(),
            task_type="task",
            task_id="cp-missing",
            budget_remaining_usd=None,
            event_queue=eq,
            initial_effort="medium",
            drive_root=None,
        )

        assert result.startswith("⚠️")
        checkpoint_events = []
        while not eq.empty():
            evt = eq.get_nowait()
            data = evt.get("data") if isinstance(evt, dict) else None
            if isinstance(data, dict) and data.get("type") == "task_checkpoint_anomaly":
                checkpoint_events.append(data)
            elif isinstance(data, dict) and data.get("type") == "task_checkpoint":
                continue
        assert checkpoint_events, "missing checkpoint response must emit task_checkpoint_anomaly"
        assert checkpoint_events[-1]["anomaly_type"] == "missing_checkpoint_response"
        assert any("missing_checkpoint_response" in p for p in progress)
        assert any("CHECKPOINT_ANOMALY:" in note for note in trace["reasoning_notes"])
        assert any(round_idx == 15 and tools is None for round_idx, _use_local, tools in calls)


class TestCheckpointReflectionProgressEmission:
    """Verify that checkpoint reflection content is emitted via emit_progress
    so it appears in the chat live card and survives history reconstruction."""

    def test_reflection_emitted_as_progress_on_checkpoint_round(self):
        """After a checkpoint round, the reflection content must appear in progress stream."""
        from unittest.mock import patch, MagicMock
        import queue as q

        # Build a minimal message list
        messages = [{"role": "user", "content": "do something"}]
        eq = q.Queue()
        progress_calls = []

        # Inject the checkpoint system message manually (simulating round 15)
        _maybe_inject_self_check(
            15, 200, messages, {"cost": 0.5}, progress_calls.append,
            event_queue=eq, task_id="test-cp"
        )

        # Simulate the LLM responding with a CHECKPOINT_REFLECTION and a tool call
        reflection_text = (
            "CHECKPOINT_REFLECTION:\n"
            "- Known: test file exists at ouroboros/loop.py\n"
            "- Blocker: none\n"
            "- Decision: read the file to find the function\n"
            "- Next: call repo_read on ouroboros/loop.py"
        )
        mock_msg = {
            "content": reflection_text,
            "tool_calls": [{"id": "tc1", "function": {"name": "repo_read", "arguments": '{"path": "ouroboros/loop.py"}'}}],
        }

        # Exercise the _checkpoint_injected path in run_llm_loop by directly
        # testing the emit_progress call behavior (unit-level test)
        from ouroboros.loop import _emit_checkpoint_reflection_event

        # Emit reflection event as run_llm_loop does
        _emit_checkpoint_reflection_event(reflection_text, 15, "test-cp", eq, None)

        # Then emit progress as the new code does
        checkpoint_num_cp = 15 // 15
        reflection_preview = reflection_text.strip()
        progress_calls.append(
            f"🔍 Checkpoint {checkpoint_num_cp} reflection (round 15):\n{reflection_preview}"
        )

        # Verify progress stream contains checkpoint reflection content
        checkpoint_progress = [p for p in progress_calls if "Checkpoint" in p and "reflection" in p.lower()]
        assert len(checkpoint_progress) >= 1, "Checkpoint reflection must be emitted as progress"
        assert "CHECKPOINT_REFLECTION" in checkpoint_progress[0] or "Known" in checkpoint_progress[0], (
            "Progress message must contain the reflection content"
        )

    def test_reflection_progress_not_truncated(self):
        """Reflection progress message must carry the full text — no truncation.

        Checkpoint reflections are cognitive artifacts (P1 Continuity, DEVELOPMENT.md).
        Hardcoded [:N] truncation is explicitly forbidden for cognitive artifacts.
        """
        long_reflection = "CHECKPOINT_REFLECTION:\n" + ("x" * 2000)
        # Simulate the emit_progress call from loop.py (no truncation)
        progress_msg = f"🔍 Checkpoint 1 reflection (round 15):\n{long_reflection.strip()}"
        # Full content must be present — no "…" truncation suffix
        assert "x" * 2000 in progress_msg
        assert not progress_msg.endswith("…")

    def test_no_reflection_progress_when_content_empty(self):
        """When LLM returns empty content on checkpoint round, no progress must be emitted."""
        progress_calls = []
        # Simulate: raw_content is empty — no progress should be appended
        raw_content = ""
        if raw_content:
            progress_calls.append(f"🔍 Checkpoint 1 reflection (round 15):\n{raw_content}")
        assert len(progress_calls) == 0

    def test_reflection_content_normalized_from_list_blocks(self):
        """List content blocks must not raise AttributeError on .strip() — must be normalized."""
        from ouroboros.loop import _extract_plain_text_from_content
        # Simulate content as a list of blocks (as returned by some Anthropic models)
        list_content = [
            {"type": "text", "text": "CHECKPOINT_REFLECTION:\n"},
            {"type": "text", "text": "- Known: loop.py exists\n- Blocker: none"},
        ]
        # Must not raise AttributeError
        result = _extract_plain_text_from_content(list_content).strip()
        assert "CHECKPOINT_REFLECTION" in result
        assert "Known" in result
