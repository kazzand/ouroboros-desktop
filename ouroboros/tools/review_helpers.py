"""Shared helpers for the review stack (advisory, triad, scope reviews).

No imports from other ouroboros.tools modules to avoid circular deps.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

BINARY_EXTENSIONS = frozenset({
    ".so", ".dylib", ".dll", ".pyc", ".whl", ".egg",
    ".png", ".jpg", ".gif", ".ico",
    ".zip", ".tar", ".gz",
    ".woff", ".woff2", ".ttf", ".eot",
})

SKIP_DIRS = frozenset({
    "__pycache__", ".git", "node_modules", "assets", "dist", "build",
})

_FILE_SIZE_LIMIT = 100_000  # 100 KB


# ---------------------------------------------------------------------------
# 1. load_checklist_section
# ---------------------------------------------------------------------------

def load_checklist_section(section_name: str) -> str:
    """Extract one ``## Header`` section from docs/CHECKLISTS.md.

    Raises ValueError if the section is not found.
    """
    checklist_path = REPO_ROOT / "docs" / "CHECKLISTS.md"
    text = checklist_path.read_text(encoding="utf-8")

    header = f"## {section_name}"
    start = text.find(header)
    if start == -1:
        raise ValueError(
            f"Section {header!r} not found in {checklist_path}"
        )

    # Find the next ## header or EOF
    next_header = text.find("\n## ", start + len(header))
    if next_header == -1:
        return text[start:]
    return text[start:next_header]


# ---------------------------------------------------------------------------
# 2. build_touched_file_pack
# ---------------------------------------------------------------------------

def build_touched_file_pack(
    repo_dir: Path,
    paths: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Read full disk content of changed files, formatted as a code pack.

    Returns (formatted_text, omitted_file_paths).
    """
    if paths is None:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        paths = []
        for line in result.stdout.splitlines():
            # porcelain format: XY filename  (or XY old -> new for renames)
            entry = line[3:]
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            paths.append(entry)

    parts: list[str] = []
    omitted: list[str] = []

    for rel in paths:
        fp = repo_dir / rel
        if not fp.is_file():
            continue
        if fp.suffix.lower() in BINARY_EXTENSIONS:
            omitted.append(rel)
            logger.warning("Skipping binary file: %s", rel)
            continue
        try:
            size = fp.stat().st_size
            if size > _FILE_SIZE_LIMIT:
                omitted.append(rel)
                parts.append(f"### {rel}\n\n*(omitted — {size:,} bytes exceeds 100 KB limit)*\n")
                continue
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            omitted.append(rel)
            logger.warning("Could not read file: %s", rel, exc_info=True)
            continue

        ext = fp.suffix.lstrip(".")
        lang = ext if ext else ""
        parts.append(f"### {rel}\n```{lang}\n{content}\n```\n")

    return "\n".join(parts), omitted


# ---------------------------------------------------------------------------
# 3. build_broader_repo_pack
# ---------------------------------------------------------------------------

def build_broader_repo_pack(
    repo_dir: Path,
    exclude_paths: set[str],
    max_chars: int = 500_000,
) -> str:
    """Read all tracked files except *exclude_paths*, up to *max_chars*."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    tracked = result.stdout.splitlines()

    parts: list[str] = []
    total = 0

    for rel in tracked:
        if rel in exclude_paths:
            continue
        fp = repo_dir / rel

        # Skip files inside non-code dirs
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue

        if fp.suffix.lower() in BINARY_EXTENSIONS:
            continue
        if not fp.is_file():
            continue

        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.warning("Could not read repo file: %s", rel, exc_info=True)
            continue

        chunk = f"### {rel}\n```{fp.suffix.lstrip('.')}\n{content}\n```\n\n"
        if total + len(chunk) > max_chars:
            parts.append(
                f"\n*(broader repo pack truncated at {max_chars:,} chars — "
                f"remaining files omitted)*\n"
            )
            break
        parts.append(chunk)
        total += len(chunk)

    return "".join(parts)


# ---------------------------------------------------------------------------
# 4. resolve_intent
# ---------------------------------------------------------------------------

def resolve_intent(
    goal: str = "",
    scope: str = "",
    commit_message: str = "",
) -> tuple[str, str]:
    """Return (resolved_text, source) with precedence goal > scope > commit_message > fallback."""
    if goal.strip():
        return goal.strip(), "goal"
    if scope.strip():
        return scope.strip(), "scope"
    if commit_message.strip():
        return commit_message.strip(), "commit message"
    return (
        "No explicit goal provided. Review the diff on its own merits.",
        "fallback",
    )


# ---------------------------------------------------------------------------
# 5. build_goal_section
# ---------------------------------------------------------------------------

def build_goal_section(
    goal: str = "",
    scope: str = "",
    commit_message: str = "",
) -> str:
    """Format the 'Intended transformation' section."""
    resolved_text, source = resolve_intent(goal, scope, commit_message)
    return (
        f"## Intended transformation\n\n"
        f"Source: {source}\n\n"
        f"{resolved_text}\n\n"
        f"Use this to judge whether the change actually completed the intended work,\n"
        f"including tests, prompts, docs, architecture touchpoints, and adjacent surfaces\n"
        f"that may have been forgotten."
    )


# ---------------------------------------------------------------------------
# 6. build_scope_section
# ---------------------------------------------------------------------------

def build_head_snapshot_section(
    repo_dir: Path,
    paths: list[str],
) -> str:
    """Build a section with pre-change (HEAD) content of touched files.

    For each path:
    - If the file is new (no HEAD version): notes it as new.
    - If the file was deleted: shows the old content from HEAD.
    - If the file was modified: shows the old content from HEAD.

    Returns formatted text ready for injection into a scope review prompt.
    """
    if not paths:
        return "(no touched files)"

    parts: list[str] = []
    for rel in paths:
        suffix = Path(rel).suffix.lower()
        # Skip binary files — git show with text=True can produce garbage or UnicodeDecodeError
        if suffix in BINARY_EXTENSIONS:
            parts.append(f"### {rel}\n\n*(HEAD snapshot omitted — binary file ({suffix}))*\n")
            continue
        ext = Path(rel).suffix.lstrip(".")
        lang = ext if ext else ""
        try:
            result = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode != 0:
                # Distinguish "file not in HEAD" (genuinely new file) from real git failures.
                # Only match known messages that mean the path is absent from HEAD.
                stderr_lower = result.stderr.lower() if result.stderr else ""
                is_new_file = (
                    "does not exist" in stderr_lower
                    or "exists on disk" in stderr_lower
                    or "path not in" in stderr_lower
                    or "not in 'head'" in stderr_lower
                )
                if is_new_file:
                    parts.append(f"### {rel}\n\n*(File is new — no HEAD snapshot)*\n")
                else:
                    # Real git failure — emit explicit error so reviewer knows the snapshot is missing
                    short_err = (result.stderr or "").strip()[:200]
                    parts.append(f"### {rel}\n\n*(HEAD snapshot error — git exited {result.returncode}: {short_err})*\n")
            else:
                content = result.stdout
                if len(content) > _FILE_SIZE_LIMIT:
                    parts.append(
                        f"### {rel}\n\n"
                        f"*(HEAD snapshot omitted — {len(content):,} bytes exceeds 100 KB limit)*\n"
                    )
                elif not content.strip():
                    parts.append(f"### {rel}\n\n*(HEAD snapshot was empty)*\n")
                else:
                    parts.append(f"### {rel}\n```{lang}\n{content}\n```\n")
        except subprocess.TimeoutExpired:
            parts.append(f"### {rel}\n\n*(HEAD snapshot timeout)*\n")
        except Exception as exc:
            parts.append(f"### {rel}\n\n*(HEAD snapshot error: {exc})*\n")

    return "\n".join(parts)


def build_scope_section(scope: str = "") -> str:
    """Format the 'Scope of this change' section. Empty string if no scope."""
    if not scope.strip():
        return ""
    return (
        f"## Scope of this change\n\n"
        f"{scope.strip()}\n\n"
        f"IMPORTANT: All issues in the staged diff itself remain subject to full review.\n"
        f"Scope affects only pre-existing unchanged code outside the diff.\n"
        f"Issues in untouched legacy code outside the declared scope are advisory at most."
    )
