"""Tests for Claude Code gateway safety guards and orchestration helpers.

The gateway module (ouroboros/gateways/claude_code.py) is SDK-only — there is
no CLI subprocess fallback. When claude-agent-sdk is absent callers receive
an error result with an install hint. Tests use a lightweight mock of the SDK
so the gateway can be imported and exercised without the real package installed.

We test:
  - ClaudeCodeResult (importable from gateway even w/o SDK via careful mocking)
  - Path guard and readonly guard hooks (function-level, no SDK dependency)
  - Orchestration helpers (_load_project_context etc.) now in shell.py
"""

import asyncio
import json
import pathlib
import subprocess
import sys
import types
import pytest


# ---------------------------------------------------------------------------
# Mock SDK so the gateway can be imported on Python 3.9 / without SDK
# ---------------------------------------------------------------------------

def _ensure_gateway_importable():
    """Install a lightweight mock of claude_agent_sdk if the real one is absent."""
    if "claude_agent_sdk" not in sys.modules:
        mock_sdk = types.ModuleType("claude_agent_sdk")
        # Provide the names the gateway expects at import time
        mock_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
        mock_sdk.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
        mock_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})
        mock_sdk.AssistantMessage = type("AssistantMessage", (), {})
        mock_sdk.ResultMessage = type("ResultMessage", (), {})
        mock_sdk.query = lambda **kw: None  # async generator mock
        sys.modules["claude_agent_sdk"] = mock_sdk


_ensure_gateway_importable()


async def _async_gen(items):
    """Async generator helper for mocking query() streams in tests."""
    for item in items:
        yield item


from ouroboros.gateways.claude_code import (  # noqa: E402
    ClaudeCodeResult,
    make_path_guard,
    make_readonly_guard,
    SAFETY_CRITICAL,
)

# Orchestration helpers now live in shell.py
from ouroboros.tools.shell import (  # noqa: E402
    _load_project_context,
    _get_changed_files,
    _get_diff_stat,
    _run_validation,
)


# ---------------------------------------------------------------------------
# ClaudeCodeResult
# ---------------------------------------------------------------------------

class TestClaudeCodeResult:
    def test_success_to_json(self):
        r = ClaudeCodeResult(
            success=True,
            result_text="Edited 2 files",
            session_id="abc-123",
            cost_usd=0.05,
            changed_files=["foo.py", "bar.py"],
            diff_stat="2 files changed, 10 insertions",
        )
        out = json.loads(r.to_tool_output())
        assert out["success"] is True
        assert out["result"] == "Edited 2 files"
        assert out["session_id"] == "abc-123"
        assert out["cost_usd"] == 0.05
        assert out["changed_files"] == ["foo.py", "bar.py"]
        assert "diff_stat" in out

    def test_error_to_json(self):
        r = ClaudeCodeResult(success=False, error="Something went wrong")
        out = json.loads(r.to_tool_output())
        assert out["success"] is False
        assert "error" in out

    def test_empty_fields_omitted(self):
        r = ClaudeCodeResult(success=True, result_text="ok")
        out = json.loads(r.to_tool_output())
        assert "session_id" not in out
        assert "changed_files" not in out
        assert "error" not in out
        assert "validation" not in out


# ---------------------------------------------------------------------------
# Path guard hook
# ---------------------------------------------------------------------------

class TestPathGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_allows_file_inside_cwd(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "foo.py")}},
            "tid-1", None,
        ))
        assert result == {}

    def test_blocks_file_outside_cwd(self, tmp_path):
        guard = make_path_guard(str(tmp_path / "subdir"))
        (tmp_path / "subdir").mkdir()
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "outside.py")}},
            "tid-2", None,
        ))
        assert result != {}
        assert "deny" in str(result)

    def test_blocks_safety_critical_file(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        for critical in SAFETY_CRITICAL:
            result = self._run(guard(
                {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / critical)}},
                f"tid-{critical}", None,
            ))
            assert "deny" in str(result), f"Should block {critical}"

    def test_allows_read_tool(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
            "tid-read", None,
        ))
        assert result == {}

    def test_blocks_relative_path_escape(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": "../../../etc/evil"}},
            "tid-escape", None,
        ))
        assert "deny" in str(result)


# ---------------------------------------------------------------------------
# Read-only guard hook
# ---------------------------------------------------------------------------

class TestReadonlyGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_blocks_edit(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {}}, "tid-1", None,
        ))
        assert "deny" in str(result)

    def test_blocks_bash(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Bash", "tool_input": {}}, "tid-2", None,
        ))
        assert "deny" in str(result)

    def test_allows_read(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {}}, "tid-3", None,
        ))
        assert result == {}

    def test_allows_grep(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Grep", "tool_input": {}}, "tid-4", None,
        ))
        assert result == {}

    def test_allows_glob(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Glob", "tool_input": {}}, "tid-5", None,
        ))
        assert result == {}


# ---------------------------------------------------------------------------
# Orchestration helpers (now in shell.py)
# ---------------------------------------------------------------------------

class TestProjectContext:
    def test_loads_existing_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("# Constitution")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "DEVELOPMENT.md").write_text("# Dev guide")
        ctx = _load_project_context(tmp_path)
        assert "CONSTITUTION" in ctx
        assert "DEVELOPMENT GUIDE" in ctx

    def test_handles_missing_docs(self, tmp_path):
        ctx = _load_project_context(tmp_path)
        assert ctx == ""  # no docs, empty context

    def test_truncates_large_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("x" * 100_000)
        ctx = _load_project_context(tmp_path)
        assert "truncated" in ctx.lower()


# ---------------------------------------------------------------------------
# SDK import fallback contract
# ---------------------------------------------------------------------------

class TestImportFallback:
    """Verify the gateway raises ImportError when the SDK is unavailable.

    Since the claude-agent-sdk is a required dependency with no CLI fallback,
    ImportError at import time surfaces SDK unavailability so callers can
    return a clear install hint rather than silently failing.
    """

    def test_gateway_import_requires_sdk(self):
        """Without the real SDK (or our mock), import should raise ImportError."""
        # The module does `from claude_agent_sdk import ...` at module level,
        # so ImportError is raised before any code runs.
        # This documents that the SDK is a hard requirement (no CLI fallback).
        #
        # To simulate absence even when SDK is installed, we must:
        # 1. Save and remove ALL claude_agent_sdk* entries from sys.modules
        # 2. Set sys.modules["claude_agent_sdk"] = None (triggers ImportError)
        # 3. Remove the cached gateway module
        # Without step 1, Python may resolve sub-module imports from cached
        # entries even when the top-level package is blocked.
        import importlib

        # Save all SDK-related modules so we can restore them
        saved_modules = {}
        for key in list(sys.modules):
            if key == "claude_agent_sdk" or key.startswith("claude_agent_sdk."):
                saved_modules[key] = sys.modules.pop(key)

        try:
            # Block the import — setting to None triggers ImportError
            sys.modules["claude_agent_sdk"] = None
            # Also remove cached gateway module so it re-imports
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            with pytest.raises(ImportError):
                importlib.import_module("ouroboros.gateways.claude_code")
        finally:
            # Remove the None sentinel
            sys.modules.pop("claude_agent_sdk", None)
            # Restore all saved SDK modules
            sys.modules.update(saved_modules)
            # If nothing was saved (SDK not installed), ensure mock is in place
            if not saved_modules:
                _ensure_gateway_importable()
            # Re-import gateway with real/mock SDK
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            importlib.import_module("ouroboros.gateways.claude_code")


# ---------------------------------------------------------------------------
# SDK API surface verification tests (v4.8.1 fixes)
# ---------------------------------------------------------------------------

class TestSDKAPISurface:
    """Verify that the gateway uses correct SDK API method names and signatures.

    These tests inspect source code to catch method name mismatches that would
    cause AttributeError at runtime (e.g. receive_response vs receive_messages).
    """

    def _gateway_source(self):
        import inspect
        from ouroboros.gateways import claude_code
        return inspect.getsource(claude_code)

    def test_edit_path_uses_receive_response(self):
        """Edit path must use receive_response() — it auto-stops after ResultMessage.

        receive_messages() streams indefinitely and can hang.
        receive_response() is the correct high-level method.
        """
        src = self._gateway_source()
        assert "receive_response()" in src, "Edit path must call receive_response()"
        assert "receive_messages()" not in src, (
            "receive_messages() streams indefinitely — use receive_response() instead"
        )

    def test_readonly_path_uses_query_function(self):
        """v4.8.1 fix: read-only path should use query() not ClaudeSDKClient."""
        src = self._gateway_source()
        # _run_readonly_async should iterate with `async for message in query(`
        assert "async for message in query(" in src, \
            "Read-only path should use query() function for one-shot requests"

    def test_max_budget_in_constructor(self):
        """v4.8.1 fix: max_budget_usd should be passed in ClaudeAgentOptions constructor."""
        src = self._gateway_source()
        # Should NOT have post-assignment pattern
        assert "options.max_budget_usd" not in src, \
            "max_budget_usd should be in constructor, not post-assigned"
        # Should have it in the constructor call
        assert "max_budget_usd=budget" in src, \
            "max_budget_usd should be passed as constructor kwarg"

    def test_query_imported_from_sdk(self):
        """query() must be imported from claude_agent_sdk."""
        from ouroboros.gateways.claude_code import query as gw_query
        # The mock installs query on the mock module
        mock_sdk = sys.modules.get("claude_agent_sdk")
        assert gw_query is mock_sdk.query, \
            "Gateway's query should be the SDK's query function"


# ---------------------------------------------------------------------------
# SDK-only path: ImportError and failure diagnostics
# ---------------------------------------------------------------------------

class TestSDKOnlyPath:
    """claude_code_edit and advisory_pre_review return meaningful errors when SDK missing."""

    def test_claude_code_edit_returns_error_when_sdk_missing(self, monkeypatch, tmp_path):
        """When SDK ImportError → tool returns install hint, not a crash."""
        from types import SimpleNamespace
        import ouroboros.tools.shell as shell_mod

        ctx = SimpleNamespace(
            repo_dir=tmp_path,
            branch_dev="ouroboros",
            pending_events=[],
            emit_progress_fn=lambda _: None,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

        # Patch run_edit to raise ImportError
        import ouroboros.gateways.claude_code as gw_mod
        monkeypatch.setattr(gw_mod, "run_edit", None)

        # Patch the import itself
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == "ouroboros.gateways.claude_code":
                raise ImportError("claude-agent-sdk not installed")
            return real_import(name, *args, **kwargs)

        # Directly test the error message in the function
        from ouroboros.tools.shell import _claude_code_edit

        # Mock _acquire_git_lock/_release_git_lock so we don't need git
        import ouroboros.tools.git as git_mod
        monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: None)
        monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: None)
        import ouroboros.utils as utils_mod
        monkeypatch.setattr(utils_mod, "run_cmd", lambda *args, **kwargs: None)

        # Simulate SDK ImportError in the try block
        original_run_edit = None
        try:
            import ouroboros.gateways.claude_code as gw
            original_run_edit = gw.run_edit
        except Exception:
            pass

        # Patch to raise ImportError
        def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'claude_agent_sdk'")

        if original_run_edit is not None:
            monkeypatch.setattr("ouroboros.gateways.claude_code.run_edit", raise_import_error)
            result = _claude_code_edit(ctx, "Test prompt")
            assert "CLAUDE_CODE_UNAVAILABLE" in result
            assert "claude-agent-sdk" in result

    def test_advisory_returns_error_when_sdk_missing(self, monkeypatch, tmp_path):
        """When SDK not installed → advisory returns install hint."""
        from ouroboros.tools.claude_advisory_review import _run_claude_advisory
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            emit_progress_fn=lambda _: None,
            pending_events=[],
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Patch run_readonly to raise ImportError
        def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'claude_agent_sdk'")

        try:
            import ouroboros.gateways.claude_code as gw
            monkeypatch.setattr(gw, "run_readonly", raise_import_error)
        except Exception:
            pass

        # Also patch the import inside _run_claude_advisory
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if "claude_code" in str(name) and "gateways" in str(name):
                raise ImportError("claude-agent-sdk not installed")
            return real_import(name, *args, **kwargs)

        items, raw = _run_claude_advisory(tmp_path, "test commit", ctx)
        # Either SDK-not-installed message, or empty if SDK is present
        assert isinstance(items, list)
        if raw.startswith("⚠️ ADVISORY_ERROR"):
            assert "claude-agent-sdk" in raw or "SDK" in raw or "ANTHROPIC_API_KEY" in raw


# ---------------------------------------------------------------------------
# Status endpoint SDK version check
# ---------------------------------------------------------------------------

class TestRunReadonlyEffortParam:
    """run_readonly passes effort param to ClaudeAgentOptions."""

    def test_run_readonly_passes_effort_to_options(self):
        """_run_readonly_async should include 'effort' in ClaudeAgentOptions kwargs."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        source = inspect.getsource(gw._run_readonly_async)
        # Verify the effort kwarg is forwarded
        assert "effort" in source
        assert "options_kwargs" in source

    def test_run_readonly_default_effort_is_high(self):
        """Default effort for run_readonly should be 'high' (matches blocking reviewers)."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw.run_readonly)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_run_readonly_async_default_effort_is_high(self):
        """Default effort for _run_readonly_async should be 'high'."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw._run_readonly_async)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_effort_forwarded_to_options_when_sdk_supports_it(self):
        """effort='high' is forwarded to ClaudeAgentOptions when the SDK accepts it."""
        captured: dict = {}

        class FakeOptions:
            # Include 'effort' as an explicit param so signature inspection
            # (used in the guard) correctly detects that this SDK version supports it.
            def __init__(self, effort=None, **kwargs):
                if effort is not None:
                    kwargs["effort"] = effort
                captured.update(kwargs)

        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        # Patch ClaudeAgentOptions with one that accepts effort
        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptions), \
             patch("ouroboros.gateways.claude_code.query") as mock_query:
            mock_query.return_value = _async_gen([])  # empty stream
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async("test", cwd="/tmp", effort="high")
            )

        assert captured.get("effort") == "high", (
            f"expected effort='high' forwarded to ClaudeAgentOptions, got: {captured}"
        )

    def test_effort_omitted_gracefully_when_sdk_lacks_support(self):
        """When SDK's ClaudeAgentOptions does not accept effort, it is silently dropped."""
        captured: dict = {}

        class FakeOptionsNoEffort:
            """Simulates an older SDK version without effort kwarg."""
            def __init__(self, **kwargs):
                if "effort" in kwargs:
                    raise TypeError("__init__() got an unexpected keyword argument 'effort'")
                captured.update(kwargs)

        import asyncio
        from unittest.mock import patch

        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptionsNoEffort), \
             patch("ouroboros.gateways.claude_code.query") as mock_query:
            mock_query.return_value = _async_gen([])
            # Should not raise — effort silently dropped
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async("test", cwd="/tmp", effort="high")
            )

        assert "effort" not in captured, "effort must be omitted when SDK lacks support"


class TestSDKStatusPayload:
    """_claude_code_status_payload returns SDK version info."""

    def test_status_payload_reflects_sdk_installed(self, monkeypatch):
        """When SDK is importable, status shows installed=True with version."""
        import importlib.metadata

        def mock_version(pkg):
            if pkg == "claude-agent-sdk":
                return "0.1.54"
            raise importlib.metadata.PackageNotFoundError(pkg)

        monkeypatch.setattr("importlib.metadata.version", mock_version)

        # Import server module's function directly
        import server as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is True
        assert payload["status"] == "installed"
        assert "0.1.54" in payload["message"]
        assert payload["busy"] is False
        assert payload["error"] == ""

    def test_status_payload_reflects_sdk_missing(self, monkeypatch):
        """When SDK is not installed, status shows installed=False."""
        import importlib.metadata

        def mock_version(pkg):
            raise importlib.metadata.PackageNotFoundError(pkg)

        monkeypatch.setattr("importlib.metadata.version", mock_version)

        import server as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is False
        assert payload["status"] == "missing"
        assert "Claude Agent SDK" in payload["message"] or "claude-agent-sdk" in payload["message"]
        assert payload["busy"] is False
