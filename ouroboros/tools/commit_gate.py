"""commit_gate.py — Advisory freshness gate and commit-attempt recording.

Extracted from git.py to keep that module under the ~1000-line size limit.
Provides:
  _record_commit_attempt(ctx, commit_message, status, ...)
  _invalidate_advisory(ctx)
  _check_advisory_freshness(ctx, commit_message, skip, paths) -> Optional[str]
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext
from ouroboros.utils import (
    truncate_review_artifact as _truncate_review_artifact,
    truncate_review_reason as _truncate_review_reason,
)

log = logging.getLogger(__name__)


def _record_commit_attempt(ctx: ToolContext, commit_message: str, status: str,
                           block_reason: str = "", block_details: str = "",
                           duration_sec: float = 0.0, snapshot_hash: str = "",
                           critical_findings: Optional[List[Dict[str, Any]]] = None) -> None:
    try:
        from ouroboros.review_state import CommitAttemptRecord, load_state, save_state, _utc_now
        dr = pathlib.Path(ctx.drive_root)
        state = load_state(dr)
        attempt = CommitAttemptRecord(
            ts=_utc_now(), commit_message=commit_message[:200], status=status,
            snapshot_hash=snapshot_hash, block_reason=block_reason,
            block_details=_truncate_review_artifact(block_details),
            duration_sec=duration_sec,
            task_id=str(getattr(ctx, "task_id", "") or ""),
            critical_findings=critical_findings or [],
        )
        if status == "blocked":
            state.add_blocking_attempt(attempt)
        elif status == "succeeded":
            state.last_commit_attempt = attempt
            state.on_successful_commit()
        else:
            state.last_commit_attempt = attempt
        save_state(dr, state)
    except Exception as e:
        log.warning("Failed to record commit attempt: %s", e)


def _invalidate_advisory(ctx: ToolContext) -> None:
    try:
        from ouroboros.review_state import mark_advisory_stale_after_edit
        mark_advisory_stale_after_edit(pathlib.Path(ctx.drive_root))
    except Exception:
        pass


def _check_advisory_freshness(ctx: ToolContext, commit_message: str,
                              skip_advisory_pre_review: bool = False,
                              paths: Optional[List[str]] = None) -> Optional[str]:
    from ouroboros.review_state import (
        compute_snapshot_hash, load_state, save_state, _utc_now, AdvisoryRunRecord,
    )
    from ouroboros.utils import append_jsonl
    drive_root = pathlib.Path(ctx.drive_root)
    repo_dir = pathlib.Path(ctx.repo_dir)

    snapshot_hash = compute_snapshot_hash(repo_dir, commit_message, paths=paths)
    state = load_state(drive_root)
    # Pass only when snapshot is fresh AND no open obligations remain.
    if state.is_fresh(snapshot_hash) and not state.get_open_obligations():
        return None

    if skip_advisory_pre_review:
        task_id = str(getattr(ctx, "task_id", "") or "")
        reason = "skip_advisory_pre_review=True passed to repo_commit"
        try:
            append_jsonl(ctx.drive_logs() / "events.jsonl", {
                "ts": _utc_now(), "type": "advisory_pre_review_bypassed",
                "snapshot_hash": snapshot_hash, "commit_message": commit_message[:200],
                "bypass_reason": reason, "task_id": task_id,
            })
        except Exception:
            pass
        state.add_run(AdvisoryRunRecord(snapshot_hash=snapshot_hash,
            commit_message=commit_message, status="bypassed", ts=_utc_now(),
            bypass_reason=reason, bypassed_by_task=task_id, snapshot_paths=paths))
        save_state(drive_root, state)
        return None  # audited bypass

    # Advisory is fresh for this snapshot — check if obligations remain
    open_obs = state.get_open_obligations()
    if state.is_fresh(snapshot_hash) and open_obs:
        lines = [f"⚠️ ADVISORY_PRE_REVIEW_REQUIRED: Advisory is current (hash={snapshot_hash[:12]}) "
                 f"but {len(open_obs)} open obligation(s) from previous blocking rounds must be resolved.\n",
                 "Unresolved obligations:"]
        lines += [f"  [{o.obligation_id}] {o.item}: {_truncate_review_reason(o.reason, limit=80)}"
                  for o in open_obs[:5]]
        if len(open_obs) > 5:
            lines.append(f"  ... and {len(open_obs) - 5} more")
        lines.append("\nFix the flagged issues and re-run advisory_pre_review so it can mark them PASS.")
        lines.append("Or bypass: repo_commit(commit_message='...', skip_advisory_pre_review=True) (audited).")
        return "\n".join(lines)

    latest = state.latest()
    matching_run = state.find_by_hash(snapshot_hash)

    # Explicit parse_failure branch: advisory ran for this snapshot but was unparseable.
    # Must come before the generic stale branch to avoid misleading "snapshot changed" message.
    if matching_run and matching_run.status == "parse_failure":
        obs_section = ""
        if state.get_open_obligations():
            open_obs = state.get_open_obligations()
            obs_lines = [f"\nOpen obligations ({len(open_obs)}):"]
            obs_lines += [f"  [{o.obligation_id}] {o.item}: {_truncate_review_reason(o.reason, limit=80)}"
                          for o in open_obs[:5]]
            if len(open_obs) > 5:
                obs_lines.append(f"  ... and {len(open_obs) - 5} more")
            obs_section = "\n".join(obs_lines)
        return (
            f"⚠️ ADVISORY_PRE_REVIEW_REQUIRED: Last advisory run for this snapshot returned "
            f"parse_failure (hash={snapshot_hash[:12]}, ts={matching_run.ts[:16]}). "
            f"The advisory ran but its output could not be parsed — re-run it.{obs_section}\n"
            "Re-run: advisory_pre_review(commit_message='...')\n"
            "Or bypass: repo_commit(commit_message='...', skip_advisory_pre_review=True) (audited)."
        )

    if latest and latest.status == "stale" and state.last_stale_from_edit_ts:
        stale_reason = (f"Advisory invalidated by worktree edit at "
                        f"{state.last_stale_from_edit_ts[:16]}. Re-run advisory after all edits.")
    elif latest:
        stale_reason = (f"Latest run: status={latest.status}, hash={latest.snapshot_hash[:12]}, "
                        f"ts={latest.ts[:16]}. Snapshot changed (files edited after advisory ran).")
    else:
        stale_reason = "No advisory runs recorded yet."

    obs_section = ""
    if open_obs:
        lines = [f"\nOpen obligations ({len(open_obs)}):"]
        lines += [f"  [{o.obligation_id}] {o.item}: {_truncate_review_reason(o.reason, limit=80)}"
                  for o in open_obs[:5]]
        if len(open_obs) > 5:
            lines.append(f"  ... and {len(open_obs) - 5} more")
        lines.append("  → advisory_pre_review will verify each obligation is resolved.")
        obs_section = "\n".join(lines)

    return (
        f"⚠️ ADVISORY_PRE_REVIEW_REQUIRED: No fresh advisory run found for this snapshot "
        f"(hash={snapshot_hash[:12]}).\n"
        f"{stale_reason}\n"
        f"{obs_section}\n\n"
        "Correct workflow:\n"
        "  1. Finish ALL edits first\n"
        "  2. advisory_pre_review(commit_message='your message')   ← run AFTER all edits\n"
        "  3. repo_commit(commit_message='your message')            ← run IMMEDIATELY after advisory\n\n"
        "⚠️ Any edit after step 2 makes the advisory stale and requires re-running it.\n\n"
        "To bypass (will be durably audited):\n"
        "  repo_commit(commit_message='...', skip_advisory_pre_review=True)"
    )
