"""Blocking scope reviewer for Ouroboros commit pipeline.

Runs AFTER the triad diff review. Single-model (configurable via OUROBOROS_SCOPE_REVIEW_MODEL,
fail-closed: timeout, parse error, API failure, or incomplete context all block.

Role: completeness, forgotten touchpoints, cross-surface consistency,
incomplete migrations, intent mismatch. NOT a duplicate of line-by-line diff review.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from dataclasses import dataclass, field
from typing import List, Optional

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.review_helpers import (
    build_full_repo_pack,
    build_goal_section,
    build_head_snapshot_section,
    build_scope_section,
    build_touched_file_pack,
    load_checklist_section,
)
from ouroboros.utils import run_cmd, utc_now_iso, append_jsonl

log = logging.getLogger(__name__)

_SCOPE_MODEL_DEFAULT = "anthropic/claude-opus-4.6"
_SCOPE_MAX_TOKENS = 65536


@dataclass
class ScopeReviewResult:
    """Structured outcome from run_scope_review.

    blocked: True if the commit is blocked.
    block_message: Human-readable block string (non-empty when blocked=True).
    critical_findings: List of structured finding dicts (same schema as triad review):
        {"verdict": "FAIL", "severity": "critical", "item": <real_item_name>,
         "reason": <str>, "model": "scope_reviewer"}
    advisory_findings: Advisory (non-blocking) finding dicts.
    """
    blocked: bool = False
    block_message: str = ""
    critical_findings: List[dict] = field(default_factory=list)
    advisory_findings: List[dict] = field(default_factory=list)


def _get_scope_model() -> str:
    """Return the configured scope review model (env → settings default)."""
    return (
        os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL", "").strip()
        or _SCOPE_MODEL_DEFAULT
    )

_SCOPE_PREAMBLE = (
    "You are a pre-commit reviewer for Ouroboros, a self-modifying AI agent.\n"
    "Its Constitution is BIBLE.md. Its engineering handbook is DEVELOPMENT.md.\n"
)


def _load_dev_guide(repo_dir: pathlib.Path) -> str:
    try:
        p = repo_dir / "docs" / "DEVELOPMENT.md"
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return "(DEVELOPMENT.md not found)"


def _build_review_history_section(history: list) -> str:
    if not history:
        return ""
    lines = ["## Previous triad review rounds\n"]
    for entry in history[-3:]:
        lines.append(f"### Round {entry.get('attempt', '?')}")
        if entry.get("critical"):
            for f in entry["critical"]:
                lines.append(f"- CRITICAL: {f}")
        if entry.get("advisory"):
            for f in entry["advisory"][:5]:
                lines.append(f"- Advisory: {f}")
        lines.append("")
    return "\n".join(lines)


def _parse_staged_name_status(repo_dir: pathlib.Path) -> list:
    """Parse staged changes with name-status for rename/delete/copy awareness.

    Returns list of (status_char, current_path, head_lookup_path) tuples:
    - status_char: A=added, M=modified, D=deleted, R=renamed, C=copied
    - current_path: path in current working tree (new path for renames)
    - head_lookup_path: path to use for git show HEAD (old path for renames)
    """
    try:
        name_status_raw = run_cmd(
            ["git", "diff", "--cached", "--name-status"], cwd=repo_dir
        )
    except Exception:
        name_status_raw = ""

    entries = []
    for line in name_status_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        status_char = parts[0][0].upper()
        if status_char in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[-1]
            entries.append((status_char, new_path, old_path))
        elif len(parts) >= 2:
            path = parts[1]
            entries.append((status_char, path, path))
        else:
            entries.append(("M", parts[0], parts[0]))

    # Fallback to --name-only if --name-status produced nothing
    if not entries:
        try:
            changed = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=repo_dir)
            for p in changed.strip().splitlines():
                p = p.strip()
                if p:
                    entries.append(("M", p, p))
        except Exception:
            pass

    return entries


def _add_deletion_placeholders(current_files_section: str, deleted_paths: list) -> str:
    """Append deletion placeholders to the current-files section for reviewer awareness."""
    if not deleted_paths:
        return current_files_section
    notes = [
        f"### {dp}\n\n*(File was DELETED — see HEAD snapshot section above for its content)*\n"
        for dp in deleted_paths
    ]
    joint = "\n".join(notes)
    if current_files_section.strip():
        return current_files_section + "\n\n" + joint
    return joint


def _compute_omission_signal(
    current_files_section: str,
    deleted_paths: list,
    omitted: list,
    current_paths: list,
) -> Optional[str]:
    """Return omission signal string for fail-closed check, or None if OK.

    Deletion-only diffs are valid (HEAD snapshot provides context).
    Block only when the current-files section is truly empty with no deletions,
    or when readable non-deleted files couldn't be read.
    """
    if not current_files_section.strip() and not deleted_paths:
        return "__empty__"
    if omitted and current_paths:
        return ", ".join(omitted)
    return None


def _build_scope_prompt(
    repo_dir: pathlib.Path,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
) -> tuple:
    """Build the scope review prompt with full context packs.

    Returns (prompt_str, touched_omitted) where touched_omitted is:
    - None if all touched files were read successfully
    - "__empty__" if no touched files could be read at all
    - comma-separated string of omitted filenames otherwise
    """
    try:
        scope_checklist = load_checklist_section("Intent / Scope Review Checklist")
    except Exception:
        scope_checklist = "(Intent / Scope Review Checklist not found in docs/CHECKLISTS.md)"

    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)
    dev_guide = _load_dev_guide(repo_dir)

    rebuttal_section = ""
    if review_rebuttal:
        rebuttal_section = (
            "\n## Developer's rebuttal to previous review feedback\n\n"
            f"{review_rebuttal}\n\n"
            "Reconsider previous FAIL verdict(s) in light of this argument.\n"
        )

    history_section = _build_review_history_section(review_history or [])

    # Get diff and changed files
    try:
        diff_text = run_cmd(["git", "diff", "--cached"], cwd=repo_dir)
    except Exception:
        diff_text = "(failed to get staged diff)"

    # Parse staged changes using name-status for rename/delete/copy awareness
    touched_entries = _parse_staged_name_status(repo_dir)
    current_paths = [ep[1] for ep in touched_entries if ep[0] != "D"]
    deleted_paths = [ep[1] for ep in touched_entries if ep[0] == "D"]
    head_snapshot_paths = [ep[2] for ep in touched_entries]
    all_touched_paths = [ep[1] for ep in touched_entries]

    # Build current-file pack (non-deleted files from working tree)
    current_files_section, omitted = build_touched_file_pack(repo_dir, current_paths)
    current_files_section = _add_deletion_placeholders(current_files_section, deleted_paths)

    # Best-effort HEAD snapshots for before/after context
    head_snapshots_section = build_head_snapshot_section(repo_dir, head_snapshot_paths)

    # Compute fail-closed omission signal (deletion-only diffs are valid, not empty)
    touched_omitted = _compute_omission_signal(current_files_section, deleted_paths, omitted, current_paths)

    # Build full repo pack — fail-closed: if git ls-files fails, block scope review
    # rather than proceeding with an incomplete (empty) repo context.
    exclude_set = set(all_touched_paths)
    try:
        full_pack, _repo_omitted = build_full_repo_pack(repo_dir, exclude_paths=exclude_set)
        repo_pack_section = full_pack
        if _repo_omitted:
            repo_pack_section += f"\n\n*(Omitted {len(_repo_omitted)} file(s): binary, vendored, sensitive, or >1MB)*\n"
        if not repo_pack_section.strip():
            repo_pack_section = "(no additional repo files)"
    except RuntimeError as exc:
        # git ls-files hard failure — propagate so run_scope_review can block.
        raise
    except Exception as exc:
        # Unexpected error — also propagate (fail-closed).
        raise RuntimeError(f"build_full_repo_pack error: {exc}") from exc

    return f"""\
{_SCOPE_PREAMBLE}

## Your role

You are the supplemental blocking scope reviewer for Ouroboros.

The diff reviewers already perform the strict line-by-line commit gate.
Do NOT merely repeat generic diff review. Focus on completeness, forgotten
touchpoints, stale adjacent surfaces, incomplete migrations, and intent mismatch.

## Your task

Judge whether the intended transformation is actually complete across:
code, tests, prompts, docs, configs, architecture surfaces, and sibling flows.

Use the richer context pack below.

## Output format

Output ONLY a valid JSON array.

Each element must have:
- "item"
- "verdict": "PASS" or "FAIL"
- "severity": "critical" or "advisory"
- "reason"

Severity rules:
- Use "critical" only when you can cite a concrete missing file, symbol, test, prompt, doc, config, or sibling path and explain why the transformation is incomplete or inconsistent.
- If you cannot point to an exact touchpoint, use "advisory".
- Scope affects only unchanged legacy code outside the diff. The diff itself is always fully reviewable.

{scope_checklist}
{scope_section}

{goal_section}

## DEVELOPMENT.md

{dev_guide}

{rebuttal_section}{history_section}

## Pre-change snapshots (HEAD versions — before this diff)

These are the versions of each touched file AS THEY EXISTED IN HEAD before this change.
Use them to judge the correctness of the transformation: what was there before vs. what is there now.
For new files (status A), the note says "File is new — no HEAD snapshot".

{head_snapshots_section}

## Current touched files (post-change — what the file looks like NOW)

{current_files_section}

## Staged diff

{diff_text}

## Wider repository context

{repo_pack_section}
""", touched_omitted


def _parse_scope_json(raw: str) -> Optional[list]:
    """Best-effort extraction of a JSON array from model output."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _emit_usage(ctx: ToolContext, model: str, usage: dict) -> None:
    """Emit a standard llm_usage event for cost tracking."""
    from ouroboros.pricing import infer_api_key_type, infer_model_category
    event = {
        "type": "llm_usage", "ts": utc_now_iso(),
        "task_id": getattr(ctx, "task_id", "") or "",
        "model": model,
        "api_key_type": infer_api_key_type(model, "openrouter"),
        "model_category": infer_model_category(model),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "cost": usage.get("cost", 0),
        },
        "provider": "openrouter",
        "source": "scope_review",
        "category": "review",
    }
    eq = getattr(ctx, "event_queue", None)
    if eq:
        try:
            eq.put_nowait(event)
        except Exception:
            pass


def run_scope_review(
    ctx: ToolContext,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
) -> ScopeReviewResult:
    """Run the blocking scope review. Returns a ScopeReviewResult.

    result.blocked is True if the commit must not proceed.
    result.critical_findings contains structured dicts with real checklist item
    names (not synthetic strings), so callers can pass them directly into
    obligation tracking without any string parsing.
    """
    repo_dir = pathlib.Path(ctx.repo_dir)

    try:
        prompt, touched_omitted = _build_scope_prompt(
            repo_dir, commit_message,
            goal=goal, scope=scope,
            review_rebuttal=review_rebuttal,
            review_history=review_history,
        )
    except RuntimeError as exc:
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context — commit blocked.\n"
                f"Error: {exc}\n"
                "Ensure git is available and the repository is in a valid state."
            ),
        )

    def _blocked(msg: str) -> ScopeReviewResult:
        return ScopeReviewResult(blocked=True, block_message=msg)

    # Fail-closed: incomplete touched-file context blocks immediately
    if touched_omitted is not None:
        if touched_omitted == "__empty__":
            return _blocked(
                "⚠️ SCOPE_REVIEW_BLOCKED: Could not read any touched files — "
                "scope review requires direct file context. Commit blocked."
            )
        return _blocked(
            f"⚠️ SCOPE_REVIEW_BLOCKED: Some touched file(s) could not be included "
            f"in direct context (binary/oversize/unreadable): {touched_omitted}.\n"
            "Scope review requires complete touched-file context. Commit blocked.\n"
            "Possible fixes: reduce file size, commit binary files separately, "
            "or ensure all touched files are readable text."
        )

    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": "Review the staged change and context above. Output ONLY a JSON array.",
        },
    ]

    # Call the LLM
    from ouroboros.config import resolve_effort as _resolve_effort
    scope_model = _get_scope_model()
    scope_effort = _resolve_effort("scope_review")
    llm = LLMClient()
    try:
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                msg, usage = pool.submit(
                    asyncio.run,
                    llm.chat_async(
                        messages=messages,
                        model=scope_model,
                        reasoning_effort=scope_effort,
                        max_tokens=_SCOPE_MAX_TOKENS,
                        temperature=0.2,
                    ),
                ).result(timeout=180)
        except RuntimeError:
            msg, usage = asyncio.run(
                llm.chat_async(
                    messages=messages,
                    model=scope_model,
                    reasoning_effort=scope_effort,
                    max_tokens=_SCOPE_MAX_TOKENS,
                    temperature=0.2,
                )
            )
    except Exception as e:
        return _blocked(
            f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
            f"Error: {type(e).__name__}: {e}\n"
            "Retry the commit, or check API key and network connectivity."
        )

    if usage:
        _emit_usage(ctx, scope_model, usage or {})

    raw_text = str(msg.get("content") or "")
    if not raw_text.strip():
        return _blocked(
            "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer returned empty response — commit blocked.\n"
            "Retry the commit."
        )

    items = _parse_scope_json(raw_text)
    if items is None:
        return _blocked(
            "⚠️ SCOPE_REVIEW_BLOCKED: Could not parse scope reviewer output as JSON — commit blocked.\n"
            f"Raw preview: {raw_text[:500]}"
        )

    # Classify findings into structured dicts (real item names, same schema as triad review)
    critical_findings: List[dict] = []
    advisory_findings: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).upper()
        severity = str(item.get("severity", "advisory")).lower()
        if verdict != "FAIL":
            continue
        finding = {
            "verdict": "FAIL",
            "severity": severity,
            "item": str(item.get("item", "scope_review")),
            "reason": str(item.get("reason", "")),
            "model": "scope_reviewer",
        }
        if severity == "critical":
            critical_findings.append(finding)
        else:
            advisory_findings.append(finding)

    # Log scope review result
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(), "type": "scope_review_complete",
            "task_id": getattr(ctx, "task_id", "") or "",
            "model": scope_model,
            "critical_count": len(critical_findings),
            "advisory_count": len(advisory_findings),
        })
    except Exception:
        pass

    if critical_findings:
        from ouroboros import config as _cfg
        review_enforcement = _cfg.get_review_enforcement()
        if review_enforcement == "blocking":
            crit_lines = "\n".join(
                f"  CRITICAL: [scope:{f['item']}] {f['reason']}" for f in critical_findings
            )
            adv_section = ""
            if advisory_findings:
                adv_lines = "\n".join(
                    f"  WARN: [scope:{f['item']}] {f['reason']}" for f in advisory_findings
                )
                adv_section = f"\n\nAdvisory warnings:\n{adv_lines}"
            block_msg = (
                "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer found critical completeness issues.\n"
                "Commit has NOT been created. Fix the issues and try again.\n\n"
                + crit_lines + adv_section
            )
            return ScopeReviewResult(
                blocked=True,
                block_message=block_msg,
                critical_findings=critical_findings,
                advisory_findings=advisory_findings,
            )
        # Advisory mode: surface but don't block
        for f in critical_findings:
            ctx._review_advisory.append(f"SCOPE CRITICAL (advisory mode): [scope:{f['item']}] {f['reason']}")
        for f in advisory_findings:
            ctx._review_advisory.append(f"SCOPE WARN: [scope:{f['item']}] {f['reason']}")
    else:
        for f in advisory_findings:
            ctx._review_advisory.append(f"SCOPE WARN: [scope:{f['item']}] {f['reason']}")

    return ScopeReviewResult(
        blocked=False,
        critical_findings=critical_findings,
        advisory_findings=advisory_findings,
    )
