"""
Ouroboros — Deep self-review module.

Builds a full review pack (all git-tracked code + memory whitelist) and sends it
to a 1M-context model for a single-pass deep review against the Constitution.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

_MAX_FILE_BYTES = 1_048_576  # 1 MB

# Security: skip files that may contain secrets (same pattern as legacy review.py)
_SENSITIVE_EXTENSIONS = {".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.staging",
    "credentials.json", "service-account.json", "secrets.yaml", "secrets.json",
    ".git-credentials", ".netrc", ".npmrc", ".pypirc",
}

_MEMORY_WHITELIST = [
    "memory/identity.md",
    "memory/scratchpad.md",
    "memory/registry.md",
    "memory/WORLD.md",
    "memory/knowledge/index-full.md",
    "memory/knowledge/patterns.md",
]

_SYSTEM_PROMPT = """\
You are conducting a deep self-review of the Ouroboros project — a self-creating AI agent.

Primary directive: The Constitution (BIBLE.md) is your absolute reference.
Every finding must be checked against it.

What to look for: bugs, crashes, race conditions,
BIBLE.md violations (P0–P8), contradictions between code and docs,
security gaps, dead code, missing error handling, architectural issues,
known error patterns from patterns.md that remain unfixed, and ideas how to improve Ouroboros to work better and better comply with the Bible.

How to work: Read every file systematically. Cross-reference interactions
between modules. Prioritize: CRITICAL > IMPORTANT > ADVISORY.

Output: Structured report with prioritized findings, each citing the
specific file, line/section, the problem, and the proposed fix."""


def build_review_pack(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Tuple[str, Dict[str, Any]]:
    """Build the full review pack from git-tracked files + memory whitelist.

    Returns (pack_text, stats) where stats has keys: file_count, total_chars, skipped.
    NO chunking, NO silent truncation.
    """
    parts: list[str] = []
    file_count = 0
    skipped: list[str] = []

    # 1. Git-tracked files (fail closed — no silent degradation)
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git ls-files exited with code {result.returncode}: {result.stderr.strip()}")
        tracked = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        if not tracked:
            raise RuntimeError("git ls-files returned no files — cannot build review pack")
    except Exception as e:
        return "", {"file_count": 0, "total_chars": 0, "skipped": [f"FATAL: {e}"]}

    read_errors: list[str] = []
    for rel_path in tracked:
        full_path = repo_dir / rel_path
        try:
            if not full_path.is_file():
                continue
            # Security: skip sensitive files
            fname = full_path.name.lower()
            fsuffix = full_path.suffix.lower()
            if fname in _SENSITIVE_NAMES or fsuffix in _SENSITIVE_EXTENSIONS:
                skipped.append(f"{rel_path} (sensitive)")
                continue
            size = full_path.stat().st_size
            if size > _MAX_FILE_BYTES:
                skipped.append(f"{rel_path} (>{_MAX_FILE_BYTES // 1024}KB)")
                parts.append(f"## FILE: {rel_path}\n[SKIPPED: file too large ({size} bytes)]\n")
                continue
            content = full_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            parts.append(f"## FILE: {rel_path}\n{content}\n")
            file_count += 1
        except Exception as e:
            read_errors.append(f"{rel_path}: {e}")
            skipped.append(f"{rel_path} (read error: {e})")
            continue

    # Surface read errors prominently (fail-closed: any tracked file unreadable = error)
    if read_errors:
        error_note = f"⚠️ INCOMPLETE PACK: {len(read_errors)} tracked file(s) unreadable:\n"
        error_note += "\n".join(f"  - {e}" for e in read_errors)
        parts.insert(0, error_note + "\n")

    # 2. Memory whitelist files
    for rel_mem in _MEMORY_WHITELIST:
        full_path = drive_root / rel_mem
        try:
            if not full_path.is_file():
                continue
            size = full_path.stat().st_size
            if size > _MAX_FILE_BYTES:
                skipped.append(f"drive/{rel_mem} (>{_MAX_FILE_BYTES // 1024}KB)")
                continue
            content = full_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            parts.append(f"## FILE: drive/{rel_mem}\n{content}\n")
            file_count += 1
        except Exception:
            continue

    pack_text = "\n".join(parts)
    stats = {
        "file_count": file_count,
        "total_chars": len(pack_text),
        "skipped": skipped,
    }
    return pack_text, stats


def is_review_available() -> Tuple[bool, Optional[str]]:
    """Check if a suitable 1M-context model is available.

    Returns (available, model_id).
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True, "openai/gpt-5.4-pro"
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENAI_BASE_URL"):
        return True, "openai::gpt-5.4-pro"
    return False, None


def run_deep_self_review(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    llm: Any,
    emit_progress: Callable[[str], None],
    event_queue: Any,
    model: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Execute a deep self-review of the entire project.

    Returns (review_text, usage_dict). On any error, returns an error string
    with empty usage instead of raising.
    """
    try:
        # 1. Build pack
        emit_progress("Building review pack (reading all tracked files)...")
        pack_text, stats = build_review_pack(repo_dir, drive_root)
        # Check for fatal build failure (fail closed)
        if not pack_text and stats.get("skipped"):
            return f"❌ Failed to build review pack: {stats['skipped'][0]}", {}

        emit_progress(
            f"Review pack built: {stats['file_count']} files, "
            f"{stats['total_chars']:,} chars"
            + (f", {len(stats['skipped'])} skipped" if stats["skipped"] else "")
        )

        # 2. Estimate tokens and check limit
        estimated_tokens = int(stats["total_chars"] / 3.5)
        if estimated_tokens > 900_000:
            return (
                f"❌ Review pack too large: ~{estimated_tokens:,} tokens "
                f"({stats['total_chars']:,} chars, {stats['file_count']} files). "
                f"Maximum is ~900,000 tokens. Reduce codebase size or split review."
            ), {}

        # 3. Determine model
        if not model:
            available, model = is_review_available()
            if not available:
                return "❌ Deep self-review unavailable: no OPENROUTER_API_KEY or OPENAI_API_KEY configured.", {}

        emit_progress(f"Sending to {model} (~{estimated_tokens:,} tokens). This may take several minutes...")

        # 4. Build messages and call LLM
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": pack_text},
        ]

        response, usage = llm.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort="high",
            max_tokens=100_000,
            temperature=None,
        )

        text = response.get("content") or ""
        if not text:
            return "⚠️ Model returned an empty response for the deep self-review.", usage or {}

        emit_progress(f"Deep self-review complete ({len(text):,} chars).")
        return text, usage or {}

    except Exception as e:
        log.error("Deep self-review failed: %s", e, exc_info=True)
        return f"❌ Deep self-review failed: {type(e).__name__}: {e}", {}
