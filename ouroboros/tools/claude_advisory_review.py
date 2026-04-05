"""Claude Code advisory pre-review gate.

Runs a read-only Claude Code review of the current worktree BEFORE the unified
multi-model pre-commit review. Advisory findings are non-blocking by themselves;
only the *absence* of a fresh matching advisory run blocks repo_commit.

Correct workflow:
  1. Finish ALL edits first
  2. advisory_pre_review(commit_message="...")   ← run AFTER all edits are done
  3. repo_commit(commit_message="...")           ← run IMMEDIATELY after advisory

⚠️ Any edit (repo_write / str_replace_editor) after step 2 automatically marks
   the advisory as stale — you must re-run advisory_pre_review before repo_commit.

Tool surface:
  advisory_pre_review   run a fresh advisory review
  review_status         show advisory history, open obligations, staleness state
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

# Shared review-artifact helpers (DEVELOPMENT.md item 2(f) compliance)
from ouroboros.utils import (  # noqa: E402
    truncate_review_artifact as _truncate_review_artifact,
    truncate_review_reason as _truncate_review_reason,
)


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
    """Build a section summarizing unresolved obligations from all blocking rounds.

    Reads the full blocking_history and open_obligations from durable state.
    The advisory reviewer must explicitly address every open obligation.
    Returns an empty string when there are no blocking obligations.
    """
    try:
        state = load_state(drive_root)
    except Exception:
        return ""

    open_obs = state.get_open_obligations()
    blocking_history = state.blocking_history

    # Only emit when there are actual open obligations — if all are resolved,
    # the section is no longer needed and its "issues NOT fixed" language is false.
    if not open_obs:
        return ""

    lines = [
        "## Unresolved obligations from previous blocking rounds",
        "",
        "Previous `repo_commit` calls were BLOCKED. The issues below are still unresolved.",
        "Your advisory review should explicitly address EACH obligation:",
        "  - If fixed: state WHAT in the current snapshot closes it.",
        "  - If not fixed: FAIL the corresponding checklist item.",
        "A generic PASS without addressing these obligations is a weak signal — "
        "addressing each one individually is expected but not enforced at the code level.",
        "",
    ]

    if open_obs:
        lines.append(f"### Open obligations ({len(open_obs)} unresolved):")
        lines.append("")
        for i, ob in enumerate(open_obs, 1):
            lines.append(f"**Obligation {i}** [id={ob.obligation_id}]")
            lines.append(f"  Checklist item: `{ob.item}` (severity: {ob.severity})")
            lines.append(f"  Issue: {ob.reason}")
            lines.append(f"  Source: blocking attempt at {ob.source_attempt_ts[:16]}")
            lines.append(f"    commit: \"{ob.source_attempt_msg[:80]}\"")
            lines.append("")

    # Also include a deduplicated summary of blocking_history for full context
    if blocking_history:
        lines.append("### Blocking history summary (most recent first):")
        lines.append("")
        seen_reasons: set = set()
        for attempt in reversed(blocking_history[-4:]):  # last 4 blocking attempts
            ts = attempt.ts[:16]
            reason = attempt.block_reason
            key = f"{reason}:{attempt.commit_message[:40]}"
            if key in seen_reasons:
                continue
            seen_reasons.add(key)
            lines.append(f"- [{ts}] block_reason={reason} | \"{attempt.commit_message[:80]}\"")
            # Show critical findings from this attempt
            for finding in (attempt.critical_findings or [])[:4]:
                if isinstance(finding, dict):
                    reason_txt = _truncate_review_reason(str(finding.get('reason', '')))
                    lines.append(f"    CRITICAL [{finding.get('item','?')}]: {reason_txt}")
            # Fallback: parse block_details if no structured findings
            if not attempt.critical_findings and attempt.block_details:
                for detail_line in attempt.block_details.split("\n"):
                    stripped = detail_line.strip()
                    if stripped.startswith("CRITICAL:") or stripped.startswith("  CRITICAL:"):
                        lines.append(f"    {_truncate_review_reason(stripped)}")
        lines.append("")

    lines.append(
        "REQUIRED: For each obligation above, your JSON output MUST include an entry "
        "for the corresponding checklist item — either PASS (with evidence of the fix) "
        "or FAIL (if the issue persists). Do NOT omit these items."
    )

    return "\n".join(lines)


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
6. **MANDATORY — Prior obligations:** If an "Unresolved obligations" section appears above,
   address EVERY listed obligation explicitly in your output:
   a. Include a separate JSON entry per obligation for the corresponding checklist item.
   b. If fixed: verdict=PASS, reason must state WHAT closes it (file, line, symbol, change).
   c. If not fixed: verdict=FAIL, severity=critical, reason must name the specific stale artifact.
   d. A generic PASS that ignores listed obligations is a weak signal — addressing each
      obligation individually is strongly expected but not enforced at the code level.
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


def _record_bypass(ctx: ToolContext, state: "AdvisoryReviewState", snapshot_hash: str,
                   commit_message: str, reason: str, task_id: str,
                   drive_root: pathlib.Path,
                   snapshot_paths: Optional[List[str]] = None) -> str:
    """Audit, record, and save a bypassed advisory run. Returns JSON response."""
    _audit_bypass(ctx, snapshot_hash, commit_message, reason, task_id)
    run = AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status="bypassed",
        ts=_utc_now(),
        bypass_reason=reason,
        bypassed_by_task=task_id,
        snapshot_paths=snapshot_paths,
    )
    state.add_run(run)
    save_state(drive_root, state)
    if "ANTHROPIC_API_KEY" in reason:
        msg = (
            "⚠️ ANTHROPIC_API_KEY is not set — advisory review skipped automatically. "
            "Bypass has been durably audited in events.jsonl. "
            "Set ANTHROPIC_API_KEY in Settings to enable Claude Code advisory reviews."
        )
    else:
        msg = "Advisory review bypassed. Bypass has been durably audited."
    return json.dumps({"status": "bypassed", "snapshot_hash": snapshot_hash,
                       "bypass_reason": reason, "message": msg},
                      ensure_ascii=False, indent=2)


def _resolve_matching_obligations(state: "AdvisoryReviewState", items: list,
                                   snapshot_hash: str) -> None:
    """Resolve open obligations whose checklist item appears in PASS but NOT in FAIL.

    An obligation is only resolved when the advisory emits PASS for that item
    and does not also emit a contradictory FAIL for the same item.  Conflicting
    entries (both PASS and FAIL for the same item) leave the obligation open so
    the agent is forced to re-examine and produce a clean, unambiguous result.
    """
    if not items:
        return
    # Build per-item verdict sets to detect contradictions
    item_verdicts: dict[str, set[str]] = {}
    for i in items:
        if not isinstance(i, dict):
            continue
        item_name = str(i.get("item", "")).lower().strip()
        verdict = str(i.get("verdict", "")).upper().strip()
        if not item_name or not verdict:
            continue
        item_verdicts.setdefault(item_name, set()).add(verdict)

    # Only PASS items that have no FAIL entry for the same item
    unambiguous_pass = {
        item_name
        for item_name, verdicts in item_verdicts.items()
        if "PASS" in verdicts and "FAIL" not in verdicts
    }

    open_obs = state.get_open_obligations()
    resolved = [o.obligation_id for o in open_obs if o.item.lower() in unambiguous_pass]
    if resolved:
        state.resolve_obligations(resolved, resolved_by=f"advisory run {snapshot_hash[:12]}")


def _next_step_guidance(latest: Optional["AdvisoryRunRecord"], state: "AdvisoryReviewState",
                        stale_from_edit: bool, stale_from_edit_ts: Optional[str],
                        open_obs: list, effective_is_fresh: bool = False) -> str:
    """Return a concrete next-step string based on current advisory state.

    Uses effective_is_fresh (derived from live snapshot hash) rather than
    stored run status to give gate-accurate guidance.

    parse_failure guidance is only emitted when the current matching snapshot run
    is a parse_failure (i.e. effective_is_fresh is not true but the hash matches).
    If the worktree has changed since a parse_failure run, the stale path takes
    precedence — the advisory needs to be re-run from scratch anyway.
    """
    # If not effectively fresh (stale stored status OR live hash mismatch), advisory must re-run.
    # Check this BEFORE parse_failure so a stale worktree-change is reported correctly even
    # if the most recent advisory run happened to be a parse_failure on an old snapshot.
    if not effective_is_fresh:
        # Special case: the matching run for the CURRENT snapshot is parse_failure.
        # (effective_is_fresh is false because parse_failure is not counted as fresh,
        #  but the advisory did run for this exact snapshot.)
        if latest and latest.status == "parse_failure" and not stale_from_edit:
            return (
                "Last advisory run produced unparseable output (parse_failure) "
                "for the current snapshot. "
                "Re-run: advisory_pre_review(commit_message='...'), "
                "or bypass: repo_commit(skip_advisory_pre_review=True) (audited)."
            )

    # If not effectively fresh (generic stale), advisory must re-run
    if not effective_is_fresh:
        if stale_from_edit:
            return (
                f"Advisory was invalidated by a worktree edit at {stale_from_edit_ts}. "
                "Complete ALL remaining edits, then run: "
                "advisory_pre_review(commit_message='...')"
            )
        if not state.runs:
            return "No advisory run yet. Run: advisory_pre_review(commit_message='...')"
        return "Advisory is stale (snapshot changed). Run: advisory_pre_review(commit_message='...')"

    # Advisory is effectively fresh — check obligations and findings
    if open_obs:
        return (
            f"Advisory is current but {len(open_obs)} open obligation(s) remain from "
            "previous blocking rounds. repo_commit will be blocked until obligations are "
            "cleared. Fix the issues, re-run advisory_pre_review so it marks them PASS, "
            "or bypass: repo_commit(skip_advisory_pre_review=True) (audited)."
        )

    if latest and latest.status == "bypassed":
        return (
            "Advisory was bypassed (audited). "
            "No open obligations — repo_commit should proceed. "
            "Consider running advisory_pre_review for a proper review."
        )

    fresh_critical = [
        i for i in (latest.items if latest else []) or []
        if isinstance(i, dict) and str(i.get("verdict", "")).upper() == "FAIL"
        and str(i.get("severity", "")).lower() == "critical"
    ]
    if fresh_critical:
        return (
            f"Advisory found {len(fresh_critical)} critical issue(s). "
            "Fix ALL critical findings, then re-run advisory_pre_review. "
            "Do NOT call repo_commit until advisory is fresh with 0 critical findings."
        )
    return (
        "Advisory is fresh with no critical findings. "
        "Proceed with: repo_commit(commit_message='...'). "
        "⚠️ Do NOT make any further edits — any edit will make advisory stale."
    )


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
        return _record_bypass(ctx, state, snapshot_hash, commit_message,
                               "ANTHROPIC_API_KEY not set — auto-bypassed", task_id, drive_root,
                               snapshot_paths=paths)

    # Handle explicit bypass
    if skip_advisory_pre_review:
        return _record_bypass(ctx, state, snapshot_hash, commit_message,
                               "explicit skip_advisory_pre_review=True", task_id, drive_root,
                               snapshot_paths=paths)

    # Check if we already have a fresh run for this snapshot.
    # BUT: if there are open obligations from a blocked commit, force a re-run
    # even on the same snapshot hash so obligations are explicitly verified.
    existing = state.find_by_hash(snapshot_hash)
    open_obligations = state.get_open_obligations()
    if existing and existing.status in ("fresh", "bypassed") and not open_obligations:
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

    # If items is empty but raw_result is non-empty, the advisory ran but failed to parse.
    # Treat this as a parse_failure to avoid silently treating it as an all-clear.
    run_status = "fresh" if items else "parse_failure"
    run = AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status=run_status,
        ts=_utc_now(),
        items=items,
        snapshot_summary=snapshot_summary,
        raw_result=raw_result,
        snapshot_paths=paths,
    )
    state.add_run(run)

    # Surface parse failures as explicit errors (not silent all-clears)
    if run_status == "parse_failure":
        save_state(drive_root, state)
        return json.dumps({
            "status": "parse_failure",
            "snapshot_hash": snapshot_hash,
            "error": "Advisory ran but returned no parseable checklist items.",
            "raw_result": _truncate_review_artifact(raw_result),
            "message": (
                "Advisory output could not be parsed. Re-run advisory_pre_review, "
                "or use skip_advisory_pre_review=True to bypass (will be audited)."
            ),
        }, ensure_ascii=False, indent=2)

    # Always try to resolve open obligations from parseable advisory results.
    # _resolve_matching_obligations only resolves when PASS is unambiguous (no concurrent FAIL
    # for the same item), so it is safe to call even when critical_fails is non-empty.
    # An obligation whose checklist item now passes should be resolved regardless of whether
    # *other* unrelated items still fail — leaving it open would turn unrelated criticals into
    # a perpetual hard gate on closed obligations.
    if items:
        _resolve_matching_obligations(state, items, snapshot_hash)

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
    """Show recent advisory pre-review run history AND last commit attempt state.

    Includes: advisory run history, staleness from edits, open obligations from
    blocking rounds, and a concrete next-step recommendation.
    """
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

    # Compute current snapshot hash using the same paths scope as the latest run
    # so path-scoped advisories don't appear falsely stale.
    repo_dir = pathlib.Path(ctx.repo_dir)
    try:
        latest_paths = latest.snapshot_paths if latest else None
        current_hash = compute_snapshot_hash(repo_dir, "", paths=latest_paths)
        hash_mismatch = bool(
            latest and latest.status in ("fresh", "bypassed", "parse_failure")
            and latest.snapshot_hash != current_hash
        )
    except Exception:
        current_hash = None
        hash_mismatch = False

    # Gate-accurate freshness: look up the run matching the CURRENT hash,
    # not just `latest` — handles restored snapshots where an older fresh run exists.
    open_obs = state.get_open_obligations()
    matching_run = state.find_by_hash(current_hash) if current_hash else None
    effective_is_fresh = bool(
        state.is_fresh(current_hash) if current_hash else False
    )
    # Use matching_run for guidance; fall back to latest for history display
    guidance_run = matching_run or latest

    # Staleness: either explicit edit-invalidation OR live hash mismatch
    stale_from_edit = bool(state.last_stale_from_edit_ts) or hash_mismatch
    stale_from_edit_ts = (
        state.last_stale_from_edit_ts[:16] if state.last_stale_from_edit_ts
        else ("now (hash mismatch)" if hash_mismatch else None)
    )

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
            "block_details_preview": _truncate_review_artifact(ca.block_details, limit=300) if ca.block_details else None,
        }

    # Open obligations (already computed above as open_obs for effective_is_fresh)
    obligations_data = []
    for ob in open_obs:
        obligations_data.append({
            "obligation_id": ob.obligation_id,
            "item": ob.item,
            "severity": ob.severity,
            "reason": _truncate_review_artifact(ob.reason, limit=200),
            "status": ob.status,
            "source_ts": ob.source_attempt_ts[:16],
            "source_commit": ob.source_attempt_msg if len(ob.source_attempt_msg) <= 60 else ob.source_attempt_msg[:60] + "...",
        })

    # Determine readiness and actionable next step (via module-level helper)

    # Build human-readable summary
    ca = state.last_commit_attempt
    if ca and ca.status in ("blocked", "failed"):
        reason_map = {
            "no_advisory": "No fresh advisory pre-review found. Run advisory_pre_review first.",
            "critical_findings": "Reviewers found critical issues. Fix all issues listed, then re-run advisory.",
            "review_quorum": "Not enough review models responded. Retry — usually transient.",
            "parse_failure": "Review models could not produce parseable output. Retry the commit.",
            "infra_failure": "Infrastructure failure (git lock, git command, or review API). Check block_details.",
            "scope_blocked": "Scope reviewer blocked the commit. Address scope review findings.",
            "preflight": "Preflight check failed (missing VERSION/README). Stage all related files.",
        }
        block_action = reason_map.get(
            ca.block_reason,
            f"{ca.status}: {ca.block_reason or 'unknown'}. Check block_details."
        )
        label = "BLOCKED" if ca.status == "blocked" else "FAILED"
        status_msg = f"Last commit {label} ({ca.block_reason or 'unclassified'}): {block_action}"
    else:
        # Use effective (gate-accurate) status derived from live snapshot, not latest run
        pass  # status_msg set below after effective_status is computed

    next_step_msg = _next_step_guidance(guidance_run, state, stale_from_edit, stale_from_edit_ts,
                                         open_obs, effective_is_fresh=effective_is_fresh)
    # Derive primary status fields from current snapshot (gate-accurate), not from latest run.
    # matching_run is the advisory that matches the live worktree hash (may differ from latest).
    # If a matching run exists for this hash, use its actual status (including "parse_failure")
    # rather than collapsing to "stale" — that would hide the real gate-relevant state.
    # Only fall back to "stale"/"none" when there is NO matching run for the current snapshot.
    if matching_run:
        effective_status = matching_run.status
    elif latest:
        effective_status = "stale"
    else:
        effective_status = "none"
    effective_hash = (
        matching_run.snapshot_hash[:12] if matching_run and matching_run.snapshot_hash else None
    )
    # status_summary / message MUST be derived from the effective (gate-accurate) state only.
    # Never show "latest run status" here — that can be from a different snapshot and is
    # confusing / internally contradictory.  If a blocking commit attempt is recorded we
    # prepend that context, but the current-snapshot status always closes the sentence.
    if state.last_commit_attempt and state.last_commit_attempt.status in ("blocked", "failed"):
        # Keep the block-action sentence from above but append the live advisory state so
        # they remain consistent even when the worktree has changed since the last block.
        status_msg = f"{status_msg}  |  Current advisory: {effective_status}"
    else:
        status_msg = f"Current advisory: {effective_status}"

    return json.dumps({
        "latest_advisory_status": effective_status,
        "latest_advisory_hash": effective_hash,
        "stale_from_edit": stale_from_edit,
        "stale_from_edit_ts": stale_from_edit_ts,
        "advisory_runs": runs_data,
        "last_commit_attempt": commit_attempt_data,
        "open_obligations": obligations_data,
        "open_obligations_count": len(obligations_data),
        "status_summary": status_msg,
        "message": status_msg,  # backward-compat alias for status_summary
        "next_step": next_step_msg,
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
                    "Correct workflow: finish edits -> advisory_pre_review(...) -> repo_commit(...) immediately. "
                    "WARNING: any edit (repo_write/str_replace_editor) after advisory_pre_review "
                    "automatically marks advisory as stale and requires re-running it. "
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
                    "Read-only diagnostic — use to check if a fresh advisory run exists "
                    "before calling repo_commit. Also shows: last commit attempt state "
                    "(reviewing/blocked/succeeded/failed) with block reason and actionable guidance; "
                    "whether advisory is stale because of a worktree edit; "
                    "open obligations from previous blocking rounds; "
                    "and a concrete next_step recommendation."
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
