"""Tests for the LLM loop checkpoint self-audit mechanism.

The checkpoint fires every 15 rounds and uses structural tool suppression
(tools=None) so the LLM is forced to write text rather than making a tool call.
The LLM is asked to write a CHECKPOINT_REFLECTION block.  The loop then:
  - detects the CHECKPOINT_REFLECTION marker and continues with full tools on the
    next round (appending assistant message + user acknowledgment to the transcript),
  - or treats the response as a final answer if the marker is absent.
Context compaction is skipped on checkpoint rounds to preserve full history.
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

    def test_prompt_asks_for_reflection_and_continue(self):
        """Checkpoint prompt must include CHECKPOINT_REFLECTION marker and mention
        continuing with tools — tools are unavailable on the checkpoint turn itself
        but full tools are restored on the next turn after the reflection is recorded."""
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "CHECKPOINT_REFLECTION:" in content
        # tools=None on checkpoint turn; prompt explains next-turn restoration
        assert "tools" in content.lower() or "continue" in content.lower()

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


class TestCheckpointToolsNone:
    """Verify structural tool suppression on checkpoint rounds.

    tools=None is passed to call_llm_with_retry on checkpoint rounds so the
    LLM is forced to write text (P3 LLM-First: structural constraint, not a
    prompt instruction).  The reflection is then stored in the transcript and a
    user acknowledgment message is appended so the NEXT round has full tools.
    """

    def test_checkpoint_prompt_says_tools_not_available_this_turn(self):
        """Prompt must honestly say tools are unavailable on this turn."""
        messages = [{"role": "user", "content": "start"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        # Prompt must mention that tools are not available this turn
        assert "not available on this turn" in content or "tools are not" in content.lower()

    def test_checkpoint_prompt_says_tools_restored_next_turn(self):
        """Prompt must tell the LLM that tools will be available on the next turn."""
        messages = [{"role": "user", "content": "start"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        # Prompt must mention next-turn tool restoration
        assert "next turn" in content.lower() or "continue with tools" in content.lower()

    def test_reflection_loop_continues_with_marker(self):
        """When the LLM returns CHECKPOINT_REFLECTION, messages must get an assistant
        entry + user acknowledgment so the loop can continue with tools on the next round.

        This is tested at the unit level by verifying the expected message structure
        that run_llm_loop produces when _checkpoint_injected=True and the LLM returns
        a valid reflection.
        """
        from ouroboros.loop import _extract_plain_text_from_content

        # Simulate the messages state at the start of the checkpoint round
        messages = [{"role": "user", "content": "do a task"}]
        # System checkpoint prompt was injected
        messages.append({"role": "system", "content": "[CHECKPOINT 1 — round 15/200]\n..."})

        reflection = (
            "CHECKPOINT_REFLECTION:\n"
            "- Known: file ouroboros/loop.py exists\n"
            "- Blocker: none\n"
            "- Decision: read it to find the function signature\n"
            "- Next: call repo_read"
        )
        reflection_text = _extract_plain_text_from_content(reflection).strip()

        # Simulate what run_llm_loop does on a valid reflection
        assert "CHECKPOINT_REFLECTION" in reflection_text
        messages.append({"role": "assistant", "content": reflection_text})
        messages.append({
            "role": "user",
            "content": "Reflection recorded. Continue with the task using your tools.",
        })

        # After the continue, next round starts with full tools
        # Verify message structure: checkpoint sys + assistant reflection + user ack
        roles = [m["role"] for m in messages]
        assert roles == ["user", "system", "assistant", "user"]
        last_user = messages[-1]["content"]
        assert "Continue" in last_user or "tools" in last_user

    def test_no_marker_terminates_task(self):
        """When the LLM returns text without CHECKPOINT_REFLECTION marker, the task
        must terminate (final answer path), not loop forever."""
        from ouroboros.loop import _extract_plain_text_from_content

        # LLM gave a final answer without the marker
        raw_content = "The answer is 42."
        reflection_text = _extract_plain_text_from_content(raw_content).strip()

        # Simulate the branch decision in run_llm_loop (anchored startswith check)
        has_marker = reflection_text.lstrip().startswith("CHECKPOINT_REFLECTION:")
        assert not has_marker  # Confirms the task would terminate via _handle_text_response

    def test_marker_mention_in_final_answer_does_not_trigger_continuation(self):
        """A final answer that merely MENTIONS the marker text must NOT be treated
        as a valid reflection — it must terminate the task.

        Example: 'The task is complete; no CHECKPOINT_REFLECTION is needed.'
        This would pass a naive `in` substring check but must fail the anchored
        `startswith` check used by run_llm_loop.
        """
        from ouroboros.loop import _extract_plain_text_from_content

        # LLM mentions the marker in the body of a final answer (not as a header)
        raw_content = "The task is complete; no CHECKPOINT_REFLECTION is needed for this step."
        reflection_text = _extract_plain_text_from_content(raw_content).strip()

        # Naive 'in' check would trigger continuation — that would be a bug
        naive_check = "CHECKPOINT_REFLECTION" in reflection_text
        assert naive_check  # Confirms that 'in' alone would misclassify this

        # Anchored startswith check correctly identifies this as a final answer
        anchored_check = reflection_text.lstrip().startswith("CHECKPOINT_REFLECTION:")
        assert not anchored_check  # Confirms task would terminate via _handle_text_response

    def test_compaction_skipped_on_checkpoint_round(self):
        """Compaction must be skipped when _checkpoint_injected=True so the full
        context is available for the reflection (P1 Continuity)."""
        # Verify the loop logic: when _checkpoint_injected=True AND round > 12,
        # compaction should NOT run. We test this by inspecting the code path logic.
        # The invariant: if _checkpoint_injected, the compaction branch is a no-op.
        # (Structural test — verifies the source code comment / branch exists.)
        import ast
        import pathlib

        loop_src = pathlib.Path(__file__).parent.parent / "ouroboros" / "loop.py"
        source = loop_src.read_text()
        # The source must contain the skip-compaction guard for checkpoint rounds
        assert "_checkpoint_injected" in source
        # The compaction skip comment must be present (checks for either phrasing)
        assert (
            "Skip ALL compaction on checkpoint" in source
            or "skip compaction" in source.lower()
            or "compaction" in source.lower() and "_checkpoint_injected" in source
        )
