"""
AST-based guard: platform-specific APIs must live in platform_layer.py only.

Scans all .py files under ouroboros/ and supervisor/ (except platform_layer.py)
for direct use of forbidden platform-specific calls and imports.
Runs on every `make test` on all platforms.
"""

import ast
import os
import pathlib
import sys
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The ONE file allowed to contain platform-specific code
ALLOWED_FILE = REPO_ROOT / "ouroboros" / "platform_layer.py"

# Directories to scan
SCAN_DIRS = [
    REPO_ROOT / "ouroboros",
    REPO_ROOT / "supervisor",
]

# Also scan server.py at the root
SCAN_FILES = [
    REPO_ROOT / "server.py",
]

# ── Forbidden patterns ──────────────────────────────────────────────────

# Platform-specific modules that must not be imported outside platform_layer.py
FORBIDDEN_IMPORTS: Set[str] = {
    "fcntl",
    "msvcrt",
    "winreg",
    "resource",
}

# os.* calls that are platform-specific
FORBIDDEN_OS_ATTRS: Set[str] = {
    "kill",
    "killpg",
    "setsid",
    "getpgid",
}

# signal.* constants/calls that are platform-specific
FORBIDDEN_SIGNAL_ATTRS: Set[str] = {
    "SIGKILL",
    "SIGTERM",
}


class PlatformViolation:
    """A single detected violation."""

    def __init__(self, filepath: str, lineno: int, description: str):
        self.filepath = filepath
        self.lineno = lineno
        self.description = description

    def __repr__(self):
        return f"{self.filepath}:{self.lineno}: {self.description}"


class PlatformGuardVisitor(ast.NodeVisitor):
    """AST visitor that detects platform-specific API usage."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.violations: List[PlatformViolation] = []
        # Track what names are imported as aliases
        self._os_aliases: Set[str] = set()
        self._signal_aliases: Set[str] = set()

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.name.split(".")[0]
            if name in FORBIDDEN_IMPORTS:
                # Check if guarded by platform check — we allow guarded imports
                # inside if/try blocks but not at module level
                if not self._is_inside_guard(node):
                    self.violations.append(PlatformViolation(
                        self.filepath, node.lineno,
                        f"Unguarded import of platform-specific module '{name}'"
                    ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            top_module = node.module.split(".")[0]
            if top_module in FORBIDDEN_IMPORTS:
                if not self._is_inside_guard(node):
                    self.violations.append(PlatformViolation(
                        self.filepath, node.lineno,
                        f"Unguarded import from platform-specific module '{node.module}'"
                    ))
            # Track os/signal imports for attribute checking
            if node.module == "os":
                for alias in node.names:
                    if alias.name in FORBIDDEN_OS_ATTRS:
                        if not self._is_inside_guard(node):
                            self.violations.append(PlatformViolation(
                                self.filepath, node.lineno,
                                f"Direct import of platform-specific os.{alias.name}"
                            ))
            if node.module == "signal":
                for alias in node.names:
                    if alias.name in FORBIDDEN_SIGNAL_ATTRS:
                        if not self._is_inside_guard(node):
                            self.violations.append(PlatformViolation(
                                self.filepath, node.lineno,
                                f"Direct import of platform-specific signal.{alias.name}"
                            ))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        # Detect os.kill, os.setsid, etc.
        if isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                if not self._is_inside_guard(node):
                    self.violations.append(PlatformViolation(
                        self.filepath, node.lineno,
                        f"Direct use of platform-specific os.{node.attr}"
                    ))
            if node.value.id == "signal" and node.attr in FORBIDDEN_SIGNAL_ATTRS:
                if not self._is_inside_guard(node):
                    self.violations.append(PlatformViolation(
                        self.filepath, node.lineno,
                        f"Direct use of platform-specific signal.{node.attr}"
                    ))
        self.generic_visit(node)

    def _is_inside_guard(self, node: ast.AST) -> bool:
        """Check if the node is inside an if/try block (platform guard).

        We use a simple heuristic: if the node is NOT at module-level
        (i.e., it's inside an if/try/function), we consider it potentially
        guarded. This avoids false positives on code like:
            if IS_WINDOWS:
                import msvcrt
        """
        # This is checked via the parent_map approach
        return hasattr(node, '_platform_guard_parent_is_guarded')


def _collect_python_files() -> List[pathlib.Path]:
    """Collect all .py files to scan."""
    files = []
    for scan_dir in SCAN_DIRS:
        if scan_dir.exists():
            for py_file in scan_dir.rglob("*.py"):
                if py_file.resolve() != ALLOWED_FILE.resolve():
                    files.append(py_file)
    for f in SCAN_FILES:
        if f.exists():
            files.append(f)
    return sorted(set(files))


def _scan_file_simple(filepath: pathlib.Path) -> List[PlatformViolation]:
    """Scan a single file for platform-specific API violations.

    Uses a simpler approach: parse the AST and walk it, checking
    only top-level (module body) statements for forbidden imports.
    For attribute access (os.kill etc.), check everywhere.
    """
    violations = []
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return violations

    rel_path = str(filepath.relative_to(REPO_ROOT))

    # Check top-level imports (not inside if/try/def/class)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name.split(".")[0] for a in node.names]
            elif node.module:
                modules = [node.module.split(".")[0]]
            for mod in modules:
                if mod in FORBIDDEN_IMPORTS:
                    violations.append(PlatformViolation(
                        rel_path, node.lineno,
                        f"Top-level import of platform-specific module '{mod}'"
                    ))

    # Check ALL attribute access for os.kill, signal.SIGKILL, etc.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in FORBIDDEN_OS_ATTRS:
                violations.append(PlatformViolation(
                    rel_path, node.lineno,
                    f"Direct use of platform-specific os.{node.attr}"
                ))
            if node.value.id == "signal" and node.attr in FORBIDDEN_SIGNAL_ATTRS:
                violations.append(PlatformViolation(
                    rel_path, node.lineno,
                    f"Direct use of platform-specific signal.{node.attr}"
                ))

    return violations


def test_no_platform_specific_apis_outside_platform_layer():
    """All platform-specific API usage must be in platform_layer.py.

    This test scans all .py files under ouroboros/ and supervisor/ (plus server.py)
    for direct use of platform-specific APIs. Any usage outside of
    ouroboros/platform_layer.py is a violation.
    """
    all_violations = []
    files = _collect_python_files()

    for filepath in files:
        violations = _scan_file_simple(filepath)
        all_violations.extend(violations)

    if all_violations:
        report = "\n".join(f"  {v}" for v in all_violations)
        pytest.fail(
            f"Found {len(all_violations)} platform-specific API violation(s) "
            f"outside ouroboros/platform_layer.py:\n{report}\n\n"
            f"All platform-specific code must go through platform_layer.py. "
            f"See docs/DEVELOPMENT.md 'Platform Abstraction Rule'."
        )


def test_platform_layer_exists():
    """platform_layer.py must exist (it's the SSOT for platform abstraction)."""
    assert ALLOWED_FILE.exists(), (
        f"ouroboros/platform_layer.py not found at {ALLOWED_FILE}. "
        f"This file is required — it contains all platform-specific code."
    )


def test_platform_layer_exports_core_symbols():
    """platform_layer.py must export the core cross-platform symbols."""
    # Just verify the module imports cleanly and has key symbols
    from ouroboros.platform_layer import (
        IS_WINDOWS,
        IS_MACOS,
        IS_LINUX,
        kill_process_tree,
        terminate_process_tree,
        force_kill_pid,
        kill_pid_tree,
        kill_process_on_port,
        pid_lock_acquire,
        pid_lock_release,
        get_system_memory,
        get_cpu_info,
        git_install_hint,
    )
    # Smoke check: flags are booleans
    assert isinstance(IS_WINDOWS, bool)
    assert isinstance(IS_MACOS, bool)
    assert isinstance(IS_LINUX, bool)
    # Exactly one should be True (or none on exotic platforms)
    assert sum([IS_WINDOWS, IS_MACOS, IS_LINUX]) <= 1
