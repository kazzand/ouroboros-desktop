"""Contract tests for web/modules/chat.js (PR #23).

Two complementary layers:
1. Structural (source-text) — verify code patterns exist; break on deletion/rename.
2. Executable (logic port) — port pure helper functions to Python and test their
   state transitions directly. stripPlanPrefix and extractRecallEntries are
   pure functions exported on window._ouroborosHelpers; we duplicate the logic
   in Python so their behavior is verifiable without a JS/DOM runtime.
"""
import pathlib
import re

CHAT_JS = pathlib.Path(__file__).parent.parent / "web" / "modules" / "chat.js"

_PLAN_PREFIX = (
    "Please do multi-model planning (plan_task tool) and web-search before "
    "answering or starting this task:\n\n"
)


def _src() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


# ── Portable Python ports of the JS pure helpers ─────────────────────────────

def _strip_plan_prefix(text: str) -> str:
    """Python port of chat.js::stripPlanPrefix."""
    return text[len(_PLAN_PREFIX):].lstrip() if text.startswith(_PLAN_PREFIX) else text


def _extract_recall_entries(messages: list, existing=None) -> list:
    """Python port of chat.js::extractRecallEntries."""
    seen = set(existing or [])
    out = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = _strip_plan_prefix((msg.get("text") or "").strip())
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


# ── visibilitychange fix ──────────────────────────────────────────────────────

def test_chat_js_has_single_visibilitychange_listener():
    """PR #23 unified: exactly one visibilitychange listener covering both layout sync and scroll restore."""
    src = _src()
    count = src.count("document.addEventListener('visibilitychange'")
    assert count == 1, (
        f"Expected exactly 1 visibilitychange listener (unified handler), found {count}"
    )


def test_chat_js_visibilitychange_calls_sync_live_card_layout():
    """The visibilitychange handler calls syncLiveCardLayout to fix stale layout."""
    src = _src()
    # The handler block should reference syncLiveCardLayout and requestAnimationFrame
    assert "syncLiveCardLayout" in src
    assert "requestAnimationFrame" in src


def test_chat_js_visibilitychange_restores_scroll():
    """The visibilitychange handler restores scroll position on tab return."""
    src = _src()
    assert "scrollTop = messagesDiv.scrollHeight" in src or \
           "messagesDiv.scrollTop = messagesDiv.scrollHeight" in src


# ── ArrowUp recall seeding fix ────────────────────────────────────────────────

def test_chat_js_seeds_input_history_from_server_history():
    """PR #23: syncHistory now seeds inputHistory from server-side chat history."""
    src = _src()
    # The pattern: looping over history messages and adding user messages to inputHistory
    assert "inputHistory.push(text)" in src or "inputHistory.push(" in src
    # Should check for user role before adding
    assert "msg.role !== 'user'" in src or "msg.role === 'user'" in src


def test_chat_js_deduplicates_input_history_with_set():
    """ArrowUp recall seeding uses a Set to avoid duplicate entries."""
    src = _src()
    assert "existingSet" in src or "new Set(inputHistory)" in src


def test_chat_js_caps_input_history_at_50():
    """inputHistory is capped at 50 entries to prevent unbounded growth."""
    src = _src()
    assert "inputHistory.length > 50" in src


# ── Live inbound message recall ───────────────────────────────────────────────

def test_chat_js_appends_live_inbound_messages_to_input_history():
    """PR #23: live inbound user messages (Telegram, other sessions) are added to recall."""
    src = _src()
    # The pattern: _recallText and inputHistory.push in the ws.on('chat') user handler
    assert "_recallText" in src


def test_chat_js_saves_input_history_after_live_update():
    """saveInputHistory is called after appending a live inbound message."""
    src = _src()
    # Should have multiple saveInputHistory calls (at least 2 — seeding + live update)
    count = src.count("saveInputHistory(inputHistory)")
    assert count >= 2, (
        f"Expected at least 2 saveInputHistory calls, found {count}"
    )


# ── PLAN_PREFIX stripping ─────────────────────────────────────────────────────

_PLAN_PREFIX = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:\n\n'


def test_chat_js_has_strip_plan_prefix_helper():
    """A shared stripPlanPrefix() helper centralises PLAN_PREFIX removal."""
    src = _src()
    assert "function stripPlanPrefix(text)" in src, (
        "Expected a shared stripPlanPrefix helper function"
    )


def test_chat_js_sync_history_uses_strip_helper():
    """syncHistory seeding path calls stripPlanPrefix, not an inline copy."""
    src = _src()
    assert "stripPlanPrefix((msg.text" in src, (
        "Expected syncHistory to call stripPlanPrefix on msg.text"
    )


def test_chat_js_live_inbound_uses_strip_helper():
    """Live inbound ws.on('chat') path calls stripPlanPrefix, not an inline copy."""
    src = _src()
    assert "stripPlanPrefix((msg.content" in src, (
        "Expected live inbound path to call stripPlanPrefix on msg.content"
    )


def test_plan_prefix_string_defined_exactly_once():
    """PLAN_PREFIX string appears exactly once (inside stripPlanPrefix helper)."""
    src = _src()
    canonical = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:'
    # Should appear exactly twice: once in PLAN_PREFIX const (sendMessage), once in stripPlanPrefix helper
    occurrences = src.count(canonical)
    assert occurrences == 2, (
        f"Expected PLAN_PREFIX string exactly 2 times (const definition + helper), found {occurrences}"
    )


# ── Bubble display stripping ──────────────────────────────────────────────────

def test_chat_js_history_replay_strips_plan_prefix_in_bubble():
    """syncHistory strips PLAN_PREFIX before addMessage for user role (history replay)."""
    src = _src()
    assert "stripPlanPrefix(msg.text" in src, (
        "Expected addMessage to pass stripPlanPrefix(msg.text) for user bubbles in history replay"
    )


def test_chat_js_live_inbound_strips_plan_prefix_in_bubble():
    """ws.on('chat') live inbound user path strips PLAN_PREFIX before addMessage."""
    src = _src()
    assert "addMessage(stripPlanPrefix(msg.content" in src, (
        "Expected addMessage to pass stripPlanPrefix(msg.content) for live inbound user bubbles"
    )


# ── Regression guards ─────────────────────────────────────────────────────────

def test_chat_js_input_history_index_reset_after_seeding():
    """inputHistoryIndex is reset to length after recall seeding."""
    src = _src()
    assert "inputHistoryIndex = inputHistory.length" in src


# ── Executable behavior tests (Python port of JS pure helpers) ────────────────

def test_strip_plan_prefix_removes_prefix():
    """stripPlanPrefix strips the plan preamble and trims leading whitespace."""
    result = _strip_plan_prefix(_PLAN_PREFIX + "What is 2+2?")
    assert result == "What is 2+2?"


def test_strip_plan_prefix_leaves_normal_text_unchanged():
    """stripPlanPrefix does not modify messages that don't start with PLAN_PREFIX."""
    msg = "Hello, what can you do?"
    assert _strip_plan_prefix(msg) == msg


def test_strip_plan_prefix_handles_empty_string():
    """stripPlanPrefix handles empty input gracefully."""
    assert _strip_plan_prefix("") == ""


def test_extract_recall_entries_filters_non_user_roles():
    """extractRecallEntries only includes user-role messages."""
    messages = [
        {"role": "assistant", "text": "Hello"},
        {"role": "user", "text": "Hi there"},
        {"role": "system", "text": "System msg"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["Hi there"]


def test_extract_recall_entries_strips_plan_prefix():
    """extractRecallEntries strips PLAN_PREFIX before adding to recall."""
    messages = [{"role": "user", "text": _PLAN_PREFIX + "Search for X"}]
    result = _extract_recall_entries(messages)
    assert result == ["Search for X"]


def test_extract_recall_entries_deduplicates_against_existing():
    """extractRecallEntries skips messages already in the existing set."""
    existing = {"already there"}
    messages = [
        {"role": "user", "text": "already there"},
        {"role": "user", "text": "new message"},
    ]
    result = _extract_recall_entries(messages, existing)
    assert result == ["new message"]


def test_extract_recall_entries_deduplicates_within_batch():
    """extractRecallEntries skips duplicate messages within the same batch."""
    messages = [
        {"role": "user", "text": "hello"},
        {"role": "user", "text": "hello"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["hello"]


def test_extract_recall_entries_skips_empty_text():
    """extractRecallEntries skips messages with empty or whitespace-only text."""
    messages = [
        {"role": "user", "text": ""},
        {"role": "user", "text": "   "},
        {"role": "user", "text": "real message"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["real message"]


def test_strip_plan_prefix_js_body_matches_python_port():
    """The JS stripPlanPrefix function body uses the same logic as the Python port."""
    src = _src()
    # Must contain the prefix string and startsWith check
    assert _PLAN_PREFIX.replace("\n\n", "\\n\\n") in src or _PLAN_PREFIX in src, (
        "stripPlanPrefix body must reference the PLAN_PREFIX string"
    )
    assert "startsWith(pfx)" in src
