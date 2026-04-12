"""Tests for tool-history compaction protection (context_compaction.py)."""
from ouroboros.context_compaction import compact_tool_history, _COMPACTION_PROTECTED_TOOLS


def _make_messages(tool_name: str, result_content: str, num_rounds: int = 8):
    """Build a message list with num_rounds of tool calls, all using the same tool."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
    return messages


def _make_large_arg_messages(tool_name: str, num_rounds: int = 8):
    """Build messages whose old assistant tool-call payloads should compact."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    large_args = '{"content": "' + ("x" * 1000) + '"}'
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": large_args},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": "ok",
        })
    return messages


def test_protected_tool_results_survive_compaction():
    """repo_commit results must not be truncated even in old rounds."""
    original_result = "OK: committed to ouroboros: v3.19.0 review feedback applied"
    msgs = _make_messages("repo_commit", original_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    commit_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == original_result
    ]
    assert len(commit_results) == 10, "All repo_commit results must survive compaction"


def test_warning_results_survive_compaction():
    """Results starting with warning emoji must not be truncated."""
    warn_result = "\u26a0\ufe0f REVIEW_BLOCKED: tests failed, commit rejected. Fix errors first."
    msgs = _make_messages("run_shell", warn_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    warning_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == warn_result
    ]
    assert len(warning_results) == 10, "Warning-prefixed results must survive compaction"


def test_old_assistant_tool_payloads_are_compacted():
    """Fallback compaction should compact oversized old assistant tool-call payloads."""
    msgs = _make_large_arg_messages("repo_write", num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    compacted_assistants = [
        m for m in compacted
        if m.get("role") == "assistant"
        and m.get("tool_calls")
        and "<<CONTENT_OMITTED len=" in m["tool_calls"][0]["function"]["arguments"]
    ]
    assert len(compacted_assistants) >= 4, "Old oversized assistant tool-call payloads should be compacted"


# ── Checkpoint reflection protection ─────────────────────────────────────────


def _make_messages_with_checkpoint(num_rounds: int = 8, checkpoint_round: int = 2):
    """Build a message list where one assistant message contains CHECKPOINT_REFLECTION."""
    messages = [{"role": "system", "content": "system"}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        content = (
            "CHECKPOINT_REFLECTION:\n"
            "- Known: the file exists\n"
            "- Blocker: none\n"
            "- Decision: proceed\n"
            "- Next: repo_read the target file"
        ) if i == checkpoint_round else f"Round {i} reasoning"
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": "repo_read", "arguments": '{"path": "a.py"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": "file content here",
        })
    return messages


def test_checkpoint_reflection_round_survives_compaction():
    """Rounds whose assistant content contains CHECKPOINT_REFLECTION must not be compacted."""
    from ouroboros.context_compaction import compact_tool_history_llm
    msgs = _make_messages_with_checkpoint(num_rounds=10, checkpoint_round=2)

    # Patch the LLM summarizer so compaction runs without a real API call
    import unittest.mock as mock
    fake_summary = {i: f"[summary of round {i}]" for i in range(len(msgs))}

    with mock.patch(
        "ouroboros.context_compaction._summarize_round_batch",
        return_value=(fake_summary, {}),
    ):
        compacted, _ = compact_tool_history_llm(msgs, keep_recent=3)

    # The checkpoint round's assistant content must be present verbatim
    checkpoint_texts = [
        m["content"] for m in compacted
        if m.get("role") == "assistant" and "CHECKPOINT_REFLECTION" in str(m.get("content", ""))
    ]
    assert len(checkpoint_texts) >= 1, (
        "Checkpoint reflection round must survive compact_tool_history_llm"
    )


def test_round_has_protected_content_detects_checkpoint():
    """_round_has_protected_content returns True when assistant content has CHECKPOINT_REFLECTION."""
    from ouroboros.context_compaction import _round_has_protected_content

    # Build a minimal span: [assistant, tool]
    messages = [
        {
            "role": "assistant",
            "content": "CHECKPOINT_REFLECTION:\n- Known: x\n- Blocker: none",
            "tool_calls": [{"id": "c1", "function": {"name": "repo_read", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "ok",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is True


def test_round_has_protected_content_ignores_non_checkpoint():
    """_round_has_protected_content returns False for normal rounds without checkpoint text."""
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "Normal reasoning without any reflection marker",
            "tool_calls": [{"id": "c1", "function": {"name": "repo_read", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "file content",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is False


def test_round_has_protected_content_handles_list_content():
    """_round_has_protected_content must detect CHECKPOINT_REFLECTION in multipart
    Anthropic content blocks (list of {type, text} dicts), not just plain strings.
    Previously this relied on Python's repr() of the list, which worked accidentally;
    now we use explicit plain-text extraction so the detection is robust."""
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "CHECKPOINT_REFLECTION:\n- Known: x\n- Blocker: none\n- Decision: proceed\n- Next: done"}
            ],
            "tool_calls": [{"id": "c1", "function": {"name": "repo_read", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "result",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is True


def test_round_has_protected_content_list_without_marker():
    """List content without CHECKPOINT_REFLECTION should not be protected."""
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Just reasoning, nothing special here."}
            ],
            "tool_calls": [{"id": "c1", "function": {"name": "repo_read", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "result",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is False
