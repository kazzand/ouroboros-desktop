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
    # Compiled / archive
    ".so", ".dylib", ".dll", ".pyc", ".whl", ".egg",
    ".zip", ".tar", ".gz", ".bz2",
    # Images / icons (expanded to match _FULL_REPO_BINARY_EXTENSIONS)
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".webp", ".bmp", ".tiff", ".svg",
    # Fonts
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    # Other binary blobs
    ".pdf", ".db", ".sqlite", ".sqlite3",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".exe", ".pyo",
})

SKIP_DIRS = frozenset({
    "__pycache__", ".git", "node_modules", "assets", "dist", "build",
})

_FILE_SIZE_LIMIT = 1_048_576  # 1 MB

# --- Constants for build_full_repo_pack (mirrors deep_self_review.py, DRY) ---
_SENSITIVE_EXTENSIONS = frozenset({".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})
_SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".env.staging",
    "credentials.json", "service-account.json", "secrets.yaml", "secrets.json",
    ".git-credentials", ".netrc", ".npmrc", ".pypirc",
})
_VENDORED_SUFFIXES = frozenset({".min.js", ".min.css", ".min.mjs"})
_VENDORED_NAMES = frozenset({"chart.umd.min.js"})
_FULL_REPO_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".icns", ".webp", ".bmp", ".tiff",
    ".svg", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".bz2",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac",
    ".db", ".sqlite", ".sqlite3",
})
_FULL_REPO_SKIP_DIR_PREFIXES = (".cursor/", ".github/", ".vscode/", ".idea/", "assets/", "webview/")
_MAX_FULL_REPO_FILE_BYTES = 1_048_576  # 1 MB
_BINARY_SNIFF_BYTES = 8192


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
        if result.returncode != 0:
            err = (result.stderr or "").strip()[:200]
            raise RuntimeError(
                f"build_touched_file_pack: git status failed (exit {result.returncode}): {err}"
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
    repo_dir_resolved = repo_dir.resolve()

    for rel in paths:
        fp = repo_dir / rel
        # Security: reject path traversal — symlinks and relative escapes must resolve
        # to a location inside the repository root.
        try:
            fp_resolved = fp.resolve()
        except OSError:
            omitted.append(rel)
            parts.append(f"### {rel}\n\n*(omitted — path resolution error)*\n")
            continue
        try:
            fp_resolved.relative_to(repo_dir_resolved)
            _inside_repo = True
        except ValueError:
            _inside_repo = False
        if not _inside_repo:
            omitted.append(rel)
            parts.append(f"### {rel}\n\n*(omitted — path escapes repository root)*\n")
            continue
        if not fp.is_file():
            continue
        # Sensitive-file guard: never inject .env, credentials, keys, etc. into review prompts
        # Normalize to lowercase so mixed-case variants (.ENV, Credentials.JSON) are caught.
        fname_lower = fp.name.lower()
        if fp.suffix.lower() in _SENSITIVE_EXTENSIONS or fname_lower in _SENSITIVE_NAMES:
            omitted.append(rel)
            parts.append(f"### {rel}\n\n*(omitted — sensitive file)*\n")
            continue
        if fp.suffix.lower() in BINARY_EXTENSIONS or _is_probably_binary(fp):
            omitted.append(rel)
            parts.append(f"### {rel}\n\n*(omitted — binary file)*\n")
            continue
        try:
            size = fp.stat().st_size
            if size > _FILE_SIZE_LIMIT:
                omitted.append(rel)
                parts.append(f"### {rel}\n\n*(omitted — {size:,} bytes exceeds {_FILE_SIZE_LIMIT:,} byte limit)*\n")
                continue
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception as read_exc:
            omitted.append(rel)
            logger.warning("Could not read file: %s", rel, exc_info=True)
            parts.append(f"### {rel}\n\n*(omitted — unreadable file: {read_exc})*\n")
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
    """Read all tracked files except *exclude_paths*, up to *max_chars*.

    .. deprecated::
        Use :func:`build_full_repo_pack` instead — it applies proper binary/sensitive/vendored
        filtering without a hardcoded char cap. Kept for backward compatibility until all callers
        are migrated.
    """
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
# 3b. _is_probably_binary (content sniffer, mirrors deep_self_review.py)
# ---------------------------------------------------------------------------

def _is_probably_binary(path: Path) -> bool:
    """Return True if the file looks like binary content.

    Best-effort heuristic — reads at most _BINARY_SNIFF_BYTES bytes.

    Three checks in order of cheapness:
    1. NUL byte — reliable indicator of non-text data.
    2. High ratio (>30%) of ASCII control characters (< 9 or 14-31, excluding
       common whitespace: tab=9, LF=10, CR=13).  Bytes ≥128 are intentionally
       excluded so valid UTF-8 text (Cyrillic, CJK, etc.) is never misclassified
       by the control-char count alone.
    3. UTF-8 incremental decode failure — catches high-byte blobs (e.g. invalid
       UTF-8 or Latin-1 binary) with no NUL and few control chars.  Uses an
       incremental decoder to avoid false positives from valid multi-byte chars
       split at the 8192-byte sample boundary.

    Returns False on any I/O error.
    """
    import codecs
    try:
        with path.open("rb") as fh:
            sample = fh.read(_BINARY_SNIFF_BYTES)
    except Exception:
        return False
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Count only ASCII control chars (not whitespace, not high bytes)
    # Tab(9), LF(10), CR(13) are valid in text.
    non_text = sum(
        1 for b in sample
        if b < 9 or (13 < b < 32) or b == 127
    )
    if non_text / len(sample) > 0.30:
        return True
    # Incremental UTF-8 decode: passes final=False so a multi-byte char split
    # at the sample boundary does not raise a false UnicodeDecodeError.
    try:
        dec = codecs.getincrementaldecoder("utf-8")("strict")
        dec.decode(sample, final=False)
    except UnicodeDecodeError:
        return True
    return False


# ---------------------------------------------------------------------------
# 3c. build_full_repo_pack (DRY extraction from deep_self_review.py)
# ---------------------------------------------------------------------------

def build_full_repo_pack(
    repo_dir: Path,
    exclude_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Build a comprehensive repo pack of all tracked text files.

    Applies proper filtering: binary, sensitive, vendored, oversized (>1MB),
    and directory-prefix exclusions. NO hardcoded char/token cap — if the result
    is too large, the caller decides what to do.

    Args:
        repo_dir: Path to the git repository root.
        exclude_paths: Optional set of relative paths to exclude (e.g. touched files
            already shown elsewhere).

    Returns:
        (pack_text, omitted) where pack_text is formatted as
        ``### rel_path\\n```ext\\ncontent\\n```\\n\\n`` sections,
        and omitted is a list of skipped relative paths with reasons.
    """
    if exclude_paths is None:
        exclude_paths = set()

    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        err = result.stderr.strip()[:200] if result.stderr else "unknown error"
        raise RuntimeError(
            f"build_full_repo_pack: git ls-files failed (exit {result.returncode}): {err}"
        )
    tracked = result.stdout.splitlines()

    parts: list[str] = []
    omitted: list[str] = []
    repo_dir_resolved = repo_dir.resolve()

    for rel in tracked:
        if rel in exclude_paths:
            continue

        rel_norm = rel.replace("\\", "/")

        # Skip excluded directory prefixes
        if rel_norm.startswith(_FULL_REPO_SKIP_DIR_PREFIXES):
            omitted.append(f"{rel} (excluded dir)")
            continue

        fp = repo_dir / rel

        # Security: reject symlinks that resolve outside the repository root.
        # Git can track symlinks; if the symlink target escapes the repo directory
        # (e.g. points at /etc/passwd or ~/secrets.env), reading it would exfiltrate
        # local secrets into external review-model prompts.
        try:
            fp_resolved = fp.resolve()
            fp_resolved.relative_to(repo_dir_resolved)
        except (OSError, ValueError):
            omitted.append(f"{rel} (path escapes repository root)")
            continue

        if not fp.is_file():
            continue

        fname = fp.name.lower()
        fsuffix = fp.suffix.lower()

        # Security: skip sensitive files
        if fname in _SENSITIVE_NAMES or fsuffix in _SENSITIVE_EXTENSIONS:
            omitted.append(f"{rel} (sensitive)")
            continue

        # Binary/media by extension
        if fsuffix in _FULL_REPO_BINARY_EXTENSIONS:
            omitted.append(f"{rel} (binary/media)")
            continue

        # Vendored/minified
        if fname in _VENDORED_NAMES or any(fname.endswith(s) for s in _VENDORED_SUFFIXES):
            omitted.append(f"{rel} (vendored/minified)")
            continue

        # Size guard before content sniffer
        try:
            size = fp.stat().st_size
        except OSError:
            omitted.append(f"{rel} (stat error)")
            continue

        if size > _MAX_FULL_REPO_FILE_BYTES:
            omitted.append(f"{rel} (>{_MAX_FULL_REPO_FILE_BYTES // 1024}KB)")
            continue

        # Content-based binary sniffer
        if _is_probably_binary(fp):
            omitted.append(f"{rel} (binary content)")
            continue

        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            omitted.append(f"{rel} (read error)")
            logger.warning("Could not read repo file: %s", rel, exc_info=True)
            continue

        ext = fp.suffix.lstrip(".")
        lang = ext if ext else ""
        parts.append(f"### {rel}\n```{lang}\n{content}\n```\n\n")

    return "".join(parts), omitted


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
        fp_rel = Path(rel)
        suffix = fp_rel.suffix.lower()
        # Sensitive-file guard: omit .env, credentials, keys before reading HEAD snapshot
        # Normalize to lowercase so mixed-case variants (.ENV, Credentials.JSON) are caught.
        fname_lower = fp_rel.name.lower()
        if suffix in _SENSITIVE_EXTENSIONS or fname_lower in _SENSITIVE_NAMES:
            parts.append(f"### {rel}\n\n*(HEAD snapshot omitted — sensitive file)*\n")
            continue
        # Skip by extension for known binary types first (fast path)
        if suffix in BINARY_EXTENSIONS:
            parts.append(f"### {rel}\n\n*(HEAD snapshot omitted — binary file ({suffix}))*\n")
            continue
        ext = Path(rel).suffix.lstrip(".")
        lang = ext if ext else ""
        try:
            # Fetch HEAD content as raw bytes only — single subprocess call.
            # Binary detection and size check run on raw bytes before any decode.
            result = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=repo_dir,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                raw_bytes = result.stdout
                # Size guard: raw byte count (not decoded character count)
                if len(raw_bytes) > _FILE_SIZE_LIMIT:
                    parts.append(
                        f"### {rel}\n\n*(HEAD snapshot omitted — {len(raw_bytes):,} bytes exceeds "
                        f"{_FILE_SIZE_LIMIT:,} byte limit)*\n"
                    )
                    continue
                # Full binary sniffer on raw bytes (mirrors _is_probably_binary logic):
                # NUL byte, control-char ratio, or UTF-8 incremental decode failure.
                import codecs as _codecs
                sample = raw_bytes[:_BINARY_SNIFF_BYTES]
                is_binary = False
                if b"\x00" in sample:
                    is_binary = True
                else:
                    non_text = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
                    if non_text / len(sample) > 0.30:
                        is_binary = True
                    else:
                        try:
                            _codecs.getincrementaldecoder("utf-8")("strict").decode(sample, final=False)
                        except UnicodeDecodeError:
                            is_binary = True
                if is_binary:
                    parts.append(f"### {rel}\n\n*(HEAD snapshot omitted — binary content detected)*\n")
                    continue
                # Decode the full raw content for injection into prompt
                content = raw_bytes.decode("utf-8", errors="replace")
                parts.append(f"### {rel}\n\n```{lang}\n{content}\n```\n")
                continue
            if result.returncode != 0:
                # Distinguish "file not in HEAD" (genuinely new file) from real git failures.
                # result.stderr is bytes (no text=True) — decode for comparison.
                raw_stderr = result.stderr or b""
                stderr_str = (
                    raw_stderr.decode("utf-8", errors="replace")
                    if isinstance(raw_stderr, (bytes, bytearray))
                    else str(raw_stderr)
                )
                stderr_lower = stderr_str.lower()
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
                    short_err = stderr_str.strip()[:200]
                    parts.append(f"### {rel}\n\n*(HEAD snapshot error — git exited {result.returncode}: {short_err})*\n")
            elif not result.stdout:
                parts.append(f"### {rel}\n\n*(HEAD snapshot was empty)*\n")
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
