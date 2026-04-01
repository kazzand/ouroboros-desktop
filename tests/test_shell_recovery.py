"""Tests for shell tool arg recovery (shlex fallback, ast.literal_eval fallback)."""
import ast
import inspect
import shlex
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from ouroboros.tools.shell import (
    _ensure_claude_cli,
    _format_claude_code_error,
    _run_shell,
    _should_retry_claude_first_run,
)


class TestShellArgRecovery:
    """run_shell should recover from various LLM argument format errors."""

    def test_shlex_recovery_present(self):
        src = inspect.getsource(_run_shell)
        assert "shlex.split" in src

    def test_ast_recovery_present(self):
        src = inspect.getsource(_run_shell)
        assert "ast" in src
        assert "literal_eval" in src

    def test_ast_recovery_logged(self):
        src = inspect.getsource(_run_shell)
        assert "run_shell_cmd_string_ast_recovered" in src

    def test_ast_literal_eval_handles_single_quoted_lists(self):
        """ast.literal_eval can parse Python lists that json.loads rejects."""
        raw = "['git', 'status']"
        result = ast.literal_eval(raw)
        assert result == ['git', 'status']

    def test_ast_literal_eval_handles_invalid_json_escapes(self):
        r"""ast.literal_eval handles strings like \| that break json.loads."""
        import json
        raw = r'["grep", "-E", "pattern\|alt"]'
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)
        result = ast.literal_eval(raw)
        assert len(result) == 3
        assert "pattern" in result[2]


def test_run_shell_rejects_literal_env_refs_in_argv(tmp_path):
    ctx = SimpleNamespace(repo_dir=tmp_path)
    result = _run_shell(ctx, ["curl", "-H", "x-api-key: $ANTHROPIC_API_KEY"])
    assert "SHELL_ENV_ERROR" in result
    assert "$ANTHROPIC_API_KEY" in result


def test_run_shell_allows_shell_expansion_via_sh_c(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    result = _run_shell(ctx, ["sh", "-c", "printf '%s' \"$ANTHROPIC_API_KEY\""])
    assert "SHELL_ENV_ERROR" not in result
    assert "exit_code=0" in result


def test_should_retry_claude_first_run_on_zero_token_invalid_key():
    stdout = """
{"type":"result","subtype":"success","is_error":true,"duration_ms":588,"duration_api_ms":0,
"result":"Invalid API key · Fix external API key","total_cost_usd":0,
"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}
""".strip()
    assert _should_retry_claude_first_run(stdout, freshly_installed=True) is True
    assert _should_retry_claude_first_run(stdout, freshly_installed=False) is False


def test_format_claude_code_error_includes_cli_result():
    res = CompletedProcess(
        args=["claude"],
        returncode=1,
        stdout='{"is_error": true, "result": "Invalid API key · Fix external API key"}',
        stderr="",
    )
    rendered = _format_claude_code_error(res)
    assert "CLAUDE_CODE_ERROR: exit_code=1" in rendered
    assert "CLI result: Invalid API key · Fix external API key" in rendered


def test_run_shell_nonzero_exit_is_reported_as_failure(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 3, "", "permission denied")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    result = _run_shell(ctx, ["npm", "install", "-g", "@anthropic-ai/claude-code"])

    assert result.startswith("⚠️ SHELL_EXIT_ERROR:")
    assert "exit_code=3" in result
    assert "permission denied" in result


def test_run_shell_timeout_uses_settings_timeout(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        raise TimeoutError("wrong exception")

    def fake_timeout(cmd, **kwargs):
        raise __import__("subprocess").TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {"OUROBOROS_TOOL_TIMEOUT_SEC": 42})
    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_timeout)
    result = _run_shell(ctx, ["sleep", "999"])

    assert "TOOL_TIMEOUT (run_shell)" in result
    assert "42s" in result


def test_ensure_claude_cli_prefers_native_installer(tmp_path, monkeypatch):
    progress = []
    ctx = SimpleNamespace(emit_progress_fn=progress.append)
    state = {"installed": False}

    def fake_which(name):
        if name == "claude":
            return "/usr/local/bin/claude" if state["installed"] else None
        if name == "curl":
            return "/usr/bin/curl"
        if name == "brew":
            return None
        return None

    def fake_install(cmd, *, timeout_sec, env):
        state["installed"] = True
        return CompletedProcess(cmd, 0, "installed", "")

    monkeypatch.setattr("ouroboros.tools.shell.IS_WINDOWS", False)
    monkeypatch.setattr("ouroboros.tools.shell.shutil.which", fake_which)
    monkeypatch.setattr("ouroboros.tools.shell._run_claude_install_attempt", fake_install)
    monkeypatch.setattr("ouroboros.tools.shell._install_claude_cli_via_npm", lambda timeout_sec: (_ for _ in ()).throw(AssertionError("npm fallback should not run")))
    monkeypatch.setattr("ouroboros.tools.shell._ensure_path", lambda force_refresh=False: None)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {"OUROBOROS_TOOL_TIMEOUT_SEC": 55})

    err, freshly_installed = _ensure_claude_cli(ctx)

    assert err is None
    assert freshly_installed is True
    assert any("official installer" in msg.lower() for msg in progress)


def test_ensure_claude_cli_falls_back_to_npm(monkeypatch):
    ctx = SimpleNamespace(emit_progress_fn=lambda text: None)
    state = {"installed": False}

    def fake_which(name):
        if name == "claude":
            return "/usr/local/bin/claude" if state["installed"] else None
        if name == "curl":
            return "/usr/bin/curl"
        if name == "brew":
            return None
        return None

    monkeypatch.setattr("ouroboros.tools.shell.IS_WINDOWS", False)
    monkeypatch.setattr("ouroboros.tools.shell.shutil.which", fake_which)
    monkeypatch.setattr("ouroboros.tools.shell._install_claude_cli_native", lambda _ctx, timeout_sec: ["native failed"])

    def fake_npm(timeout_sec):
        state["installed"] = True
        return None

    monkeypatch.setattr("ouroboros.tools.shell._install_claude_cli_via_npm", fake_npm)
    monkeypatch.setattr("ouroboros.tools.shell._ensure_path", lambda force_refresh=False: None)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {"OUROBOROS_TOOL_TIMEOUT_SEC": 60})

    err, freshly_installed = _ensure_claude_cli(ctx)

    assert err is None
    assert freshly_installed is True
