"""Claude Code advisory pre-review gate.

Runs a read-only Claude Code review of the current worktree BEFORE the unified
multi-model pre-commit review. Advisory findings are non-blocking by themselves;
only the *absence* of a fresh matching advisory run blocks repo_commit.

Workflow:
  edit files
  -> advisory_pre_review(commit_message="...")   ← this module
  -> fix obvious issues
  -> repo_commit(...)                            ← existing unified review still runs

Tool surface:
  advisory_pre_review   run a fresh advisory review
  review_status         show recent advisory run history (read-only diagnostic)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.review_state import (
    AdvisoryRunRecord,
    AdvisoryReviewState,
    compute_snapshot_hash,
    format_status_section,
    load_state,
    save_state,
    _utc_now,
)
from ouroboros.tools.review_helpers import (
    load_checklist_section,
    build_touched_file_pack,
    build_goal_section,
    build_scope_section,
)
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 80_000        # Explicit omission note beyond this


def _load_doc(repo_dir: pathlib.Path, relpath: str, fallback: str = "") -> str:
    try:
        p = repo_dir / relpath
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return fallback


def _get_staged_diff(repo_dir: pathlib.Path) -> str:
    """Return staged diff + unstaged diff of changed files.

    If the combined diff exceeds _MAX_DIFF_CHARS, an explicit omission note
    is appended (no silent truncation).
    """
    try:
        staged = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        ).stdout or ""
        unstaged = subprocess.run(
            ["git", "diff"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        ).stdout or ""
        combined = (staged + unstaged).strip()
        if len(combined) > _MAX_DIFF_CHARS:
            total_len = len(combined)
            combined = combined[:_MAX_DIFF_CHARS] + (
                f"\n\n⚠️ OMISSION NOTE: diff truncated at {_MAX_DIFF_CHARS:,} of {total_len:,} chars. "
                "Use Read/Grep tools to inspect the omitted portions if needed."
            )
        return combined or "(no unstaged/staged changes found)"
    except Exception:
        return "(failed to retrieve diff)"


def _get_changed_file_list(repo_dir: pathlib.Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            return "\n".join(lines) if lines else "(clean — no changed files)"
        return "(git status failed)"
    except Exception:
        return "(git status error)"


def _build_blocking_history_section(drive_root: pathlib.Path) -> str:
    """Build a section summarizing recent blocking commit findings.

    Reads the last commit attempt from durable state. If it was blocked with
    critical_findings, includes those findings so the advisory reviewer can
    catch the same issues proactively.
    """
    try:
        state = load_state(drive_root)
    except Exception:
        return ""

    sections: List[str] = []

    # 1. Last commit attempt — if blocked with critical findings
    ca = state.last_commit_attempt
    if ca and ca.status == "blocked" and ca.block_reason in (
        "critical_findings", "scope_blocked", "parse_failure"
    ) and ca.block_details:
        lines = [
            "## Previous blocking review findings",
            "",
            "The last `repo_commit` was BLOCKED by the downstream blocking reviewers.",
            "Your advisory review MUST catch these same issues — a false PASS here",
            "wastes an entire blocking review cycle.",
            "",
            f"Block reason: {ca.block_reason}",
            f"Commit message: \"{ca.commit_message}\"",
            "",
            "Findings from blocking reviewers:",
            "",
        ]
        # Extract individual CRITICAL lines from block_details
        for line in ca.block_details.split("\n"):
            stripped = line.strip()
            if stripped.startswith("CRITICAL:") or stripped.startswith("WARN:"):
                lines.append(f"- {stripped}")
            elif stripped.startswith("⚠️ SCOPE_REVIEW_BLOCKED") or stripped.startswith("⚠️ REVIEW_BLOCKED"):
                lines.append(f"- {stripped[:200]}")
        # If we didn't extract any structured lines, include a preview
        if not any(l.startswith("- ") for l in lines):
            preview = ca.block_details[:500].replace("\n", "\n  ")
            lines.append(f"  {preview}")
        lines.append("")
        lines.append(
            "IMPORTANT: Verify that ALL of the above issues have been fixed in the "
            "current working tree. If any remain, FAIL the corresponding checklist item."
        )
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def _build_advisory_prompt(
    repo_dir: pathlib.Path,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    paths: Optional[List[str]] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> str:
    """Build the read-only advisory review prompt.

    Includes: BIBLE.md, CHECKLISTS.md, DEVELOPMENT.md, git status, staged diff,
    touched file pack, goal/scope sections, and blocking history.
    Does NOT include raw chat/task context.
    """
    bible = _load_doc(repo_dir, "BIBLE.md", "(BIBLE.md not found)")
    try:
        checklists = load_checklist_section("Repo Commit Checklist")
    except Exception:
        checklists = _load_doc(repo_dir, "docs/CHECKLISTS.md", "(CHECKLISTS.md not found)")
    dev_guide = _load_doc(repo_dir, "docs/DEVELOPMENT.md", "(DEVELOPMENT.md not found)")
    diff = _get_staged_diff(repo_dir)
    changed_files = _get_changed_file_list(repo_dir)

    touched_pack, _omitted = build_touched_file_pack(repo_dir, paths)
    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)

    # Build blocking history section if drive_root is available
    blocking_history = ""
    if drive_root:
        blocking_history = _build_blocking_history_section(drive_root)

    arch_doc = _load_doc(repo_dir, "docs/ARCHITECTURE.md", "(ARCHITECTURE.md not found)")

    prompt = f"""\
You are performing a pre-commit review of an Ouroboros self-modifying AI agent codebase.

## Your role — NON-NEGOTIABLE REQUIREMENTS
- Review the current working tree changes with the SAME RIGOR as the downstream blocking reviewers.
  A false PASS here wastes an entire blocking review cycle ($10+).
- Use ONLY Read, Grep, Glob tools. Do NOT edit or execute any files.
- Read the FULL CONTENT of every changed file listed below using the Read tool.
  Do NOT evaluate security, bible compliance, or code quality from path listings or diff hunks alone.
- Return ONLY a JSON array. No prose, no markdown fences — only the JSON array.

## Thoroughness requirements
- Do NOT stop after finding the first issue. Check EVERY item in the checklist.
- Report ALL problems you find. If there are 5 bugs, list all 5 — each as a separate entry.
- Do NOT summarize multiple distinct problems into one finding.
- For PASS: brief reason is fine. For FAIL: cite the specific file, line/symbol, what is wrong,
  and provide a CONCRETE fix suggestion so the developer knows exactly what to change.

## Severity thresholds — treat as blocking reviewers do
- bible_compliance (item 1): ANY violation of BIBLE.md principles is CRITICAL.
- security_issues (item 5): ANY path traversal, secret leakage, or unsafe operation is CRITICAL.
- development_compliance (item 2): naming, entity type rules, module size, no ad-hoc LLM calls,
  no hardcoded [:N] truncation of cognitive artifacts — all CRITICAL when violated.
- self_consistency (item 13): if a concrete stale artifact exists (specific file + line), CRITICAL.

## Output format
Return ONLY a JSON array. Each element:
{{
  "item": "<checklist item name>",
  "verdict": "PASS" | "FAIL",
  "severity": "critical" | "advisory",
  "reason": "<for FAIL: file, line/symbol, what is wrong, how to fix>"
}}

## CHECKLISTS.md (What to review)

{checklists}

{scope_section}

{goal_section}

## DEVELOPMENT.md (Engineering standards)

{dev_guide}

## BIBLE.md (Constitutional context — top priority)

{bible}

## ARCHITECTURE.md (System structure reference)

{arch_doc}

{blocking_history}

## Commit message

{commit_message}

## Changed files (git status --porcelain)

{changed_files}

## Current touched files (full content — read these with the Read tool for deeper inspection)

{touched_pack}

## Staged diff

{diff}

## Step-by-step instructions
1. Read the FULL content of every changed file using the Read tool. Do not skip any file.
2. Check EVERY item from the "Repo Commit Checklist" — do not stop after the first issue.
3. Pay equal attention to ALL 13 checklist items. bible_compliance and security_issues must be
   evaluated at the same strictness as the downstream blocking reviewers.
4. Look for ALL bugs, logic errors, regressions, race conditions, and violations of BIBLE.md or DEVELOPMENT.md.
5. Cross-check: do tool descriptions in prompts match actual get_tools() exports?
   Does ARCHITECTURE.md header version match the VERSION file?
6. If previous blocking review findings are listed above, verify that each one has been addressed.
   If any remain, FAIL the corresponding checklist item.
7. Output ONLY the JSON array — no markdown fences, no commentary outside the JSON.
"""
    return prompt


def _run_claude_advisory(
    repo_dir: pathlib.Path,
    commit_message: str,
    ctx: ToolContext,
    goal: str = "",
    scope: str = "",
    paths: Optional[List[str]] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> tuple[list, str]:
    """Run the advisory review via Claude Agent SDK (read-only).

    Returns (items: list, raw_result: str).
    items is a list of review finding dicts (may be empty on error).
    raw_result is the raw output string.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return [], "⚠️ ADVISORY_ERROR: ANTHROPIC_API_KEY not set."

    prompt = _build_advisory_prompt(repo_dir, commit_message, goal=goal, scope=scope, paths=paths, drive_root=drive_root)

    try:
        from ouroboros.gateways.claude_code import run_readonly

        result = run_readonly(
            prompt=prompt,
            cwd=str(repo_dir),
            model="opus",
            max_turns=8,
        )

        if not result.success:
            import sys
            sdk_version = "(unknown)"
            try:
                import importlib.metadata
                sdk_version = importlib.metadata.version("claude-agent-sdk")
            except Exception:
                pass
            return [], (
                f"⚠️ ADVISORY_ERROR: {result.error}\n"
                f"Diagnostic: sdk_version={sdk_version}, python={sys.executable}"
            )

        raw_text = result.result_text
        items = _parse_advisory_output(raw_text)
        return items, raw_text

    except ImportError:
        return [], (
            "⚠️ ADVISORY_ERROR: claude-agent-sdk not installed. "
            "Install: pip install 'ouroboros[claude-sdk]'"
        )
    except Exception as e:
        import sys
        sdk_version = "(unknown)"
        try:
            import importlib.metadata
            sdk_version = importlib.metadata.version("claude-agent-sdk")
        except Exception:
            pass
        return [], (
            f"⚠️ ADVISORY_ERROR: SDK call failed: {type(e).__name__}: {e}\n"
            f"Diagnostic: sdk_version={sdk_version}, python={sys.executable}"
        )


def _parse_advisory_output(stdout: str) -> list:
    """Extract the JSON findings array from Claude CLI output."""
    # Try direct parse first
    text = stdout.strip()

    # Unwrap Claude Code JSON envelope: {"result": "...", ...}
    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and "result" in outer:
            text = str(outer["result"]).strip()
        elif isinstance(outer, list):
            return outer
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Try direct parse of the inner result
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Find embedded JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    return []


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _audit_bypass(ctx: ToolContext, snapshot_hash: str, commit_message: str,
                  bypass_reason: str, task_id: str) -> None:
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "advisory_pre_review_bypassed",
            "snapshot_hash": snapshot_hash,
            "commit_message": commit_message[:200],
            "bypass_reason": bypass_reason,
            "task_id": task_id,
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_advisory_pre_review(
    ctx: ToolContext,
    commit_message: str = "",
    skip_advisory_pre_review: bool = False,
    goal: str = "",
    scope: str = "",
    paths: Optional[List[str]] = None,
) -> str:
    """Run an advisory pre-commit review via Claude Agent SDK (read-only).

    Stores result in durable state so repo_commit can verify freshness.
    Returns structured JSON with findings + status.
    """
    repo_dir = pathlib.Path(ctx.repo_dir)
    drive_root = pathlib.Path(ctx.drive_root)

    snapshot_hash = compute_snapshot_hash(repo_dir, commit_message, paths=paths)
    state = load_state(drive_root)
    task_id = str(getattr(ctx, "task_id", "") or "")

    # Auto-bypass if Anthropic key is absent — audit it transparently
    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        reason = "ANTHROPIC_API_KEY not set — auto-bypassed"
        _audit_bypass(ctx, snapshot_hash, commit_message, reason, task_id)
        run = AdvisoryRunRecord(
            snapshot_hash=snapshot_hash,
            commit_message=commit_message,
            status="bypassed",
            ts=_utc_now(),
            bypass_reason=reason,
            bypassed_by_task=task_id,
        )
        state.add_run(run)
        save_state(drive_root, state)
        return json.dumps({
            "status": "bypassed",
            "snapshot_hash": snapshot_hash,
            "bypass_reason": reason,
            "message": (
                "⚠️ ANTHROPIC_API_KEY is not set — advisory review skipped automatically. "
                "Bypass has been durably audited in events.jsonl. "
                "Set ANTHROPIC_API_KEY in Settings to enable Claude Code advisory reviews."
            ),
        }, ensure_ascii=False, indent=2)

    # Handle explicit bypass
    if skip_advisory_pre_review:
        reason = "explicit skip_advisory_pre_review=True"
        _audit_bypass(ctx, snapshot_hash, commit_message, reason, task_id)
        run = AdvisoryRunRecord(
            snapshot_hash=snapshot_hash,
            commit_message=commit_message,
            status="bypassed",
            ts=_utc_now(),
            bypass_reason=reason,
            bypassed_by_task=task_id,
        )
        state.add_run(run)
        save_state(drive_root, state)
        return json.dumps({
            "status": "bypassed",
            "snapshot_hash": snapshot_hash,
            "bypass_reason": reason,
            "message": "Advisory review bypassed. Bypass has been durably audited.",
        }, ensure_ascii=False, indent=2)

    # Check if we already have a fresh run for this snapshot
    existing = state.find_by_hash(snapshot_hash)
    if existing and existing.status in ("fresh", "bypassed"):
        return json.dumps({
            "status": "already_fresh",
            "snapshot_hash": snapshot_hash,
            "ts": existing.ts,
            "items": existing.items,
            "message": "A fresh advisory run already exists for this snapshot. Proceed with repo_commit.",
        }, ensure_ascii=False, indent=2)

    # Run the advisory review
    ctx.emit_progress_fn("Running advisory pre-review (Claude Code, read-only)...")
    changed_files = _get_changed_file_list(repo_dir)
    items, raw_result = _run_claude_advisory(repo_dir, commit_message, ctx, goal=goal, scope=scope, paths=paths, drive_root=drive_root)

    # Handle errors from the CLI
    if raw_result.startswith("⚠️ ADVISORY_ERROR"):
        return json.dumps({
            "status": "error",
            "snapshot_hash": snapshot_hash,
            "error": raw_result,
            "message": (
                "Advisory review failed to run. Fix the error and retry, "
                "or use skip_advisory_pre_review=True to bypass (will be audited)."
            ),
        }, ensure_ascii=False, indent=2)

    # Classify findings
    critical_fails = [i for i in items if isinstance(i, dict)
                      and str(i.get("verdict", "")).upper() == "FAIL"
                      and str(i.get("severity", "")).lower() == "critical"]
    advisory_fails = [i for i in items if isinstance(i, dict)
                      and str(i.get("verdict", "")).upper() == "FAIL"
                      and str(i.get("severity", "")).lower() != "critical"]

    snapshot_summary = f"{changed_files.count(chr(10)) + 1} file(s) changed"

    run = AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status="fresh",
        ts=_utc_now(),
        items=items,
        snapshot_summary=snapshot_summary,
        raw_result=raw_result,
    )
    state.add_run(run)
    save_state(drive_root, state)

    # Build human-readable summary
    findings_summary: List[str] = []
    for item in critical_fails:
        findings_summary.append(f"  CRITICAL [{item.get('item','?')}]: {item.get('reason','')}")
    for item in advisory_fails:
        findings_summary.append(f"  ADVISORY [{item.get('item','?')}]: {item.get('reason','')}")

    result = {
        "status": "fresh",
        "snapshot_hash": snapshot_hash,
        "ts": run.ts,
        "items": items,
        "critical_count": len(critical_fails),
        "advisory_count": len(advisory_fails),
        "snapshot_summary": snapshot_summary,
        "message": (
            f"Advisory review complete. {len(critical_fails)} critical, "
            f"{len(advisory_fails)} advisory findings. "
            "Fix issues and run repo_commit when ready."
        ),
    }
    if findings_summary:
        result["findings"] = findings_summary

    return json.dumps(result, ensure_ascii=False, indent=2)


def _handle_review_status(ctx: ToolContext) -> str:
    """Show recent advisory pre-review run history AND last commit attempt state."""
    drive_root = pathlib.Path(ctx.drive_root)
    state = load_state(drive_root)

    runs_data = []
    for run in reversed(state.runs[-5:]):
        findings = [i for i in (run.items or []) if isinstance(i, dict)
                    and str(i.get("verdict", "")).upper() == "FAIL"]
        critical = [i for i in findings if str(i.get("severity", "")).lower() == "critical"]
        runs_data.append({
            "snapshot_hash": run.snapshot_hash[:12],
            "commit_message": run.commit_message[:80],
            "status": run.status,
            "ts": run.ts[:16],
            "critical_findings": len(critical),
            "total_findings": len(findings),
            "snapshot_summary": run.snapshot_summary,
            "bypass_reason": run.bypass_reason or None,
        })

    latest = state.latest()

    # Build commit attempt section
    commit_attempt_data = None
    if state.last_commit_attempt:
        ca = state.last_commit_attempt
        commit_attempt_data = {
            "status": ca.status,
            "commit_message": ca.commit_message[:80],
            "ts": ca.ts[:16],
            "duration_sec": round(ca.duration_sec, 1),
            "block_reason": ca.block_reason or None,
            "block_details_preview": ca.block_details[:300] if ca.block_details else None,
        }

    # Build actionable message
    if not state.runs and not state.last_commit_attempt:
        msg = (
            "No advisory runs or commit attempts recorded. "
            "Run advisory_pre_review(commit_message='...') before repo_commit."
        )
    elif state.last_commit_attempt and state.last_commit_attempt.status in ("blocked", "failed"):
        ca = state.last_commit_attempt
        reason_map = {
            "no_advisory": "No fresh advisory pre-review found. Run advisory_pre_review first.",
            "critical_findings": "Reviewers found critical issues. Fix the issues listed in block_details.",
            "review_quorum": "Not enough review models responded. Retry — usually transient.",
            "parse_failure": "Review models could not produce parseable output. Retry the commit.",
            "infra_failure": "Infrastructure failure (git lock, git command, or review API). Check block_details and retry.",
            "scope_blocked": "Scope reviewer blocked the commit. Address scope review findings.",
            "preflight": "Preflight check failed (missing VERSION/README). Stage all related files.",
        }
        action = reason_map.get(ca.block_reason, f"{ca.status}: {ca.block_reason or 'unknown'}. Check block_details.")
        label = "BLOCKED" if ca.status == "blocked" else "FAILED"
        msg = f"Last commit {label} ({ca.block_reason or 'unclassified'}): {action}"
    else:
        msg = (
            f"Latest advisory run: {latest.status if latest else 'none'}. "
            "Use advisory_pre_review(commit_message='...') to run a fresh review."
        )

    return json.dumps({
        "latest_advisory_status": latest.status if latest else "none",
        "latest_advisory_hash": latest.snapshot_hash[:12] if latest else None,
        "advisory_runs": runs_data,
        "last_commit_attempt": commit_attempt_data,
        "message": msg,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def get_tools() -> list:
    return [
        ToolEntry(
            name="advisory_pre_review",
            schema={
                "name": "advisory_pre_review",
                "description": (
                    "Run an advisory pre-commit review via Claude Agent SDK (read-only: Read, Grep, Glob only). "
                    "MUST be called before repo_commit. Returns structured JSON findings. "
                    "Findings are advisory (non-blocking), but the absence of a fresh matching "
                    "advisory run will block repo_commit. "
                    "Workflow: edit -> advisory_pre_review(...) -> fix issues -> repo_commit(...). "
                    "Use skip_advisory_pre_review=True to bypass (bypass is durably audited)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "commit_message": {
                            "type": "string",
                            "description": "Intended commit message. Used to bind the advisory run to this specific commit.",
                        },
                        "skip_advisory_pre_review": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Explicitly bypass the advisory review. "
                                "Bypass is durably audited in events.jsonl. "
                                "Default: False."
                            ),
                        },
                        "goal": {
                            "type": "string",
                            "description": "High-level goal of this change. Used to judge completeness.",
                        },
                        "scope": {
                            "type": "string",
                            "description": "Declared scope boundary. Issues outside scope are advisory-only.",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Explicit list of changed file paths. Auto-detected from git status if omitted.",
                        },
                    },
                    "required": ["commit_message"],
                },
            },
            handler=_handle_advisory_pre_review,
        ),
        ToolEntry(
            name="review_status",
            schema={
                "name": "review_status",
                "description": (
                    "Show recent advisory pre-review run history. "
                    "Read-only diagnostic \u2014 use to check if a fresh advisory run exists "
                    "before calling repo_commit. Also shows last commit attempt state "
                    "(reviewing/blocked/succeeded/failed) with block reason and actionable guidance."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            handler=_handle_review_status,
        ),
    ]
