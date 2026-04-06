"""Durable state store for advisory pre-review runs.

Each advisory run is identified by a deterministic snapshot_hash built from:
  - the set of changed files and their content digests

State persists across task boundaries and restarts in
~/Ouroboros/data/state/advisory_review.json.

Public API:
  load_state(drive_root)        -> AdvisoryReviewState
  save_state(drive_root, state) -> None
  compute_snapshot_hash(repo_dir, commit_message, paths) -> str
  format_status_section(state)  -> str (for context injection)
  mark_advisory_stale_after_edit(drive_root) -> None
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from ouroboros.utils import (
    truncate_review_artifact as _truncate_review_artifact,
    truncate_review_reason as _truncate_review_reason,
)

log = logging.getLogger(__name__)

_STATE_RELPATH = "state/advisory_review.json"
_MAX_RUN_HISTORY = 10  # Keep the last N advisory runs
_MAX_BLOCKING_HISTORY = 10  # Keep the last N blocking commit attempts for obligation tracking


@dataclass
class ObligationItem:
    """A single unresolved obligation extracted from a blocking commit attempt."""

    obligation_id: str   # stable hash of (item_name, reason_prefix)
    item: str            # checklist item name, e.g. "tests_affected"
    severity: str        # "critical" | "advisory"
    reason: str          # full reason text from the blocking review
    source_attempt_ts: str  # when the blocking attempt occurred
    source_attempt_msg: str  # commit message of the blocking attempt
    status: str = "still_open"  # "still_open" | "resolved"
    resolved_by: str = ""       # free-text: how it was resolved


@dataclass
class AdvisoryRunRecord:
    """A single completed advisory pre-review run."""

    snapshot_hash: str
    commit_message: str
    status: str            # "fresh" | "stale" | "bypassed" | "parse_failure"
    ts: str                # ISO timestamp
    items: List[Dict[str, Any]] = field(default_factory=list)
    snapshot_summary: str = ""
    raw_result: str = ""
    bypass_reason: str = ""
    bypassed_by_task: str = ""
    snapshot_paths: Optional[List[str]] = field(default=None)  # paths-scoped hash scope


@dataclass
class CommitAttemptRecord:
    """Tracks a single repo_commit / repo_write_commit attempt."""

    ts: str
    commit_message: str
    status: str  # "pending" | "reviewing" | "blocked" | "succeeded" | "failed"
    snapshot_hash: str = ""
    block_reason: str = ""  # "no_advisory" | "review_quorum" | "critical_findings" | "parse_failure" | "infra_failure" | "scope_blocked" | "preflight"
    block_details: str = ""  # the full blocking message
    duration_sec: float = 0.0
    task_id: str = ""
    # Structured critical findings extracted from block_details (populated on block)
    critical_findings: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AdvisoryReviewState:
    """Top-level state container."""

    runs: List[AdvisoryRunRecord] = field(default_factory=list)
    last_commit_attempt: Optional[CommitAttemptRecord] = field(default=None)
    # Bounded history of all blocking attempts — used to build obligation checklist
    blocking_history: List[CommitAttemptRecord] = field(default_factory=list)
    # Explicit obligation register: unresolved issues across attempts
    open_obligations: List[ObligationItem] = field(default_factory=list)
    # Timestamp of last advisory invalidation from a worktree write
    last_stale_from_edit_ts: str = ""

    def latest(self) -> Optional[AdvisoryRunRecord]:
        """Return the most recent run, or None."""
        return self.runs[-1] if self.runs else None

    def find_by_hash(self, snapshot_hash: str) -> Optional[AdvisoryRunRecord]:
        """Return the most recent run matching snapshot_hash."""
        for run in reversed(self.runs):
            if run.snapshot_hash == snapshot_hash:
                return run
        return None

    def is_fresh(self, snapshot_hash: str) -> bool:
        """True iff there is a fresh (or bypassed) run matching snapshot_hash."""
        run = self.find_by_hash(snapshot_hash)
        return run is not None and run.status in ("fresh", "bypassed")

    def add_run(self, run: AdvisoryRunRecord) -> None:
        self.mark_all_stale_except(run.snapshot_hash)
        self.runs.append(run)
        if len(self.runs) > _MAX_RUN_HISTORY:
            self.runs = self.runs[-_MAX_RUN_HISTORY:]
        # Clear stale-from-edit flag when a new review runs for the current snapshot.
        # This prevents review_status from falsely reporting stale-after-edit
        # when the user already ran an advisory review (even parse_failure) since the last edit.
        # parse_failure runs are recorded for the current snapshot, so the stale-edit marker
        # is no longer accurate — the user must re-run advisory, not fix the edit.
        if run.status in ("fresh", "bypassed", "parse_failure"):
            self.last_stale_from_edit_ts = ""

    def mark_stale(self, snapshot_hash: str) -> None:
        """Mark all runs with this hash as stale."""
        for run in self.runs:
            if run.snapshot_hash == snapshot_hash:
                run.status = "stale"

    def mark_all_stale_except(self, snapshot_hash: str) -> None:
        """Mark all runs NOT matching snapshot_hash as stale (fresh and bypassed)."""
        for run in self.runs:
            if run.snapshot_hash != snapshot_hash and run.status in ("fresh", "bypassed"):
                run.status = "stale"

    def mark_all_stale(self, reason_ts: str = "") -> None:
        """Mark ALL fresh and bypassed runs as stale (e.g. after a worktree write)."""
        for run in self.runs:
            if run.status in ("fresh", "bypassed"):
                run.status = "stale"
        if reason_ts:
            self.last_stale_from_edit_ts = reason_ts

    def add_blocking_attempt(self, attempt: CommitAttemptRecord) -> None:
        """Add a blocking commit attempt to bounded history and update obligations."""
        self.last_commit_attempt = attempt
        if attempt.status == "blocked":
            self.blocking_history.append(attempt)
            if len(self.blocking_history) > _MAX_BLOCKING_HISTORY:
                self.blocking_history = self.blocking_history[-_MAX_BLOCKING_HISTORY:]
            self._update_obligations_from_attempt(attempt)

    def _make_obligation_id(self, item: str, reason: str) -> str:
        """Stable ID for an obligation: hash of item name + first 80 chars of reason."""
        key = f"{item}:{reason[:80]}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def _update_obligations_from_attempt(self, attempt: CommitAttemptRecord) -> None:
        """Extract critical findings from a blocking attempt and add/merge into obligations.

        One obligation per checklist item — if an obligation for the same item already
        exists (still_open), update it with the latest reason and source rather than
        creating a duplicate. This ensures obligation resolution (which matches by item
        name) is unambiguous: one PASS for item X closes exactly one obligation.
        """
        if not attempt.critical_findings:
            return
        # Build index by item name for upsert logic
        existing_by_item = {
            ob.item.lower(): ob
            for ob in self.open_obligations
            if ob.status == "still_open"
        }
        for finding in attempt.critical_findings:
            if not isinstance(finding, dict):
                continue
            if str(finding.get("verdict", "")).upper() != "FAIL":
                continue
            if str(finding.get("severity", "")).lower() != "critical":
                continue
            item = str(finding.get("item", "unknown"))
            reason = str(finding.get("reason", ""))
            item_key = item.lower()
            if item_key in existing_by_item:
                # Update existing obligation with the latest reason/source
                ob = existing_by_item[item_key]
                ob.reason = reason
                ob.source_attempt_ts = attempt.ts
                ob.source_attempt_msg = attempt.commit_message[:200]
            else:
                ob_id = self._make_obligation_id(item, reason)
                new_ob = ObligationItem(
                    obligation_id=ob_id,
                    item=item,
                    severity=str(finding.get("severity", "critical")),
                    reason=reason,
                    source_attempt_ts=attempt.ts,
                    source_attempt_msg=attempt.commit_message[:200],
                    status="still_open",
                )
                self.open_obligations.append(new_ob)
                existing_by_item[item_key] = new_ob

    def resolve_obligations(self, resolved_ids: List[str], resolved_by: str = "") -> int:
        """Mark specific obligations as resolved. Returns count resolved."""
        count = 0
        for ob in self.open_obligations:
            if ob.obligation_id in resolved_ids and ob.status == "still_open":
                ob.status = "resolved"
                ob.resolved_by = resolved_by
                count += 1
        return count

    def clear_resolved_obligations(self) -> None:
        """Remove fully resolved obligations from the list."""
        self.open_obligations = [ob for ob in self.open_obligations if ob.status == "still_open"]

    def get_open_obligations(self) -> List[ObligationItem]:
        """Return only still-open obligations."""
        return [ob for ob in self.open_obligations if ob.status == "still_open"]

    def on_successful_commit(self) -> None:
        """Called after a successful commit: clears obligations and blocking history."""
        self.open_obligations = []
        self.blocking_history = []
        self.last_stale_from_edit_ts = ""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _obligation_from_dict(d: Dict[str, Any]) -> ObligationItem:
    return ObligationItem(
        obligation_id=str(d.get("obligation_id", "")),
        item=str(d.get("item", "")),
        severity=str(d.get("severity", "critical")),
        reason=str(d.get("reason", "")),
        source_attempt_ts=str(d.get("source_attempt_ts", "")),
        source_attempt_msg=str(d.get("source_attempt_msg", "")),
        status=str(d.get("status", "still_open")),
        resolved_by=str(d.get("resolved_by", "")),
    )


def _record_from_dict(d: Dict[str, Any]) -> AdvisoryRunRecord:
    raw_paths = d.get("snapshot_paths")
    return AdvisoryRunRecord(
        snapshot_hash=str(d.get("snapshot_hash", "")),
        commit_message=str(d.get("commit_message", "")),
        status=str(d.get("status", "stale")),
        ts=str(d.get("ts", "")),
        items=list(d.get("items") or []),
        snapshot_summary=str(d.get("snapshot_summary", "")),
        raw_result=str(d.get("raw_result", "")),
        bypass_reason=str(d.get("bypass_reason", "")),
        bypassed_by_task=str(d.get("bypassed_by_task", "")),
        snapshot_paths=list(raw_paths) if isinstance(raw_paths, list) else None,
    )


def _commit_attempt_from_dict(d: Dict[str, Any]) -> CommitAttemptRecord:
    return CommitAttemptRecord(
        ts=str(d.get("ts", "")),
        commit_message=str(d.get("commit_message", "")),
        status=str(d.get("status", "failed")),
        snapshot_hash=str(d.get("snapshot_hash", "")),
        block_reason=str(d.get("block_reason", "")),
        block_details=str(d.get("block_details", "")),
        duration_sec=float(d.get("duration_sec", 0.0)),
        task_id=str(d.get("task_id", "")),
        critical_findings=list(d.get("critical_findings") or []),
    )


def load_state(drive_root: pathlib.Path) -> AdvisoryReviewState:
    """Load advisory review state from disk. Returns empty state on any error."""
    path = drive_root / _STATE_RELPATH
    try:
        if not path.exists():
            return AdvisoryReviewState()
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        runs = [_record_from_dict(r) for r in (data.get("runs") or [])]
        last_commit = None
        if data.get("last_commit_attempt"):
            last_commit = _commit_attempt_from_dict(data["last_commit_attempt"])
        blocking_history = [
            _commit_attempt_from_dict(r)
            for r in (data.get("blocking_history") or [])
        ]
        open_obligations = [
            _obligation_from_dict(o)
            for o in (data.get("open_obligations") or [])
        ]
        last_stale_ts = str(data.get("last_stale_from_edit_ts", ""))
        return AdvisoryReviewState(
            runs=runs,
            last_commit_attempt=last_commit,
            blocking_history=blocking_history,
            open_obligations=open_obligations,
            last_stale_from_edit_ts=last_stale_ts,
        )
    except Exception as e:
        log.warning("Failed to load advisory review state from %s: %s", path, e)
        return AdvisoryReviewState()


def save_state(drive_root: pathlib.Path, state: AdvisoryReviewState) -> None:
    """Persist advisory review state atomically."""
    path = drive_root / _STATE_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {
            "runs": [asdict(r) for r in state.runs],
            "last_commit_attempt": asdict(state.last_commit_attempt) if state.last_commit_attempt else None,
            "blocking_history": [asdict(r) for r in state.blocking_history],
            "open_obligations": [asdict(o) for o in state.open_obligations],
            "last_stale_from_edit_ts": state.last_stale_from_edit_ts,
            "saved_at": _utc_now(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning("Failed to save advisory review state to %s: %s", path, e)


# ---------------------------------------------------------------------------
# Snapshot hash
# ---------------------------------------------------------------------------

_SNAPSHOT_EXCLUDE_PATHS = frozenset({
    # Exclude our own state file — it changes on every save and would
    # cause the hash to drift after advisory_pre_review stores its result.
    "state/advisory_review.json",
    # Also exclude lock files and temp files that change independently
    "state/queue_snapshot.json",
})


def compute_snapshot_hash(
    repo_dir: pathlib.Path,
    commit_message: str = "",
    paths: list[str] | None = None,
) -> str:
    """Build a deterministic hash for the current worktree snapshot.

    Hash inputs:
      - sorted list of (relpath, sha256_of_content) for changed files
        (excludes advisory state files that change independently)

    commit_message is accepted for backward compatibility but NOT included
    in the hash — the hash reflects code state only, making freshness less
    brittle when only the message changes.

    When *paths* is provided, only those files are considered instead of
    the full git status output.

    An empty list is normalized to None (whole-repo scope) to prevent a
    trivially-fresh advisory over zero files from bypassing the gate.
    """
    # Normalize empty list → whole-repo scope
    if isinstance(paths, list) and len(paths) == 0:
        paths = None

    changed_digests: List[tuple] = []

    if paths is not None:
        # Only consider the explicitly provided paths
        for relpath in paths:
            relpath = relpath.strip()
            if not relpath:
                continue
            if relpath in _SNAPSHOT_EXCLUDE_PATHS:
                continue
            file_path = repo_dir / relpath
            try:
                if file_path.is_file():
                    content = file_path.read_bytes()
                    digest = hashlib.sha256(content).hexdigest()[:16]
                else:
                    digest = "deleted"
                changed_digests.append((relpath, digest))
            except Exception:
                changed_digests.append((relpath, "unreadable"))
    else:
        # Get list of changed files (staged + unstaged) from git
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    # Format: "XY path" or "XY old -> new"
                    relpath = line[3:].strip().split(" -> ")[-1].strip()
                    if not relpath:
                        continue
                    if relpath in _SNAPSHOT_EXCLUDE_PATHS:
                        continue
                    file_path = repo_dir / relpath
                    try:
                        if file_path.is_file():
                            content = file_path.read_bytes()
                            digest = hashlib.sha256(content).hexdigest()[:16]
                        else:
                            digest = "deleted"
                        changed_digests.append((relpath, digest))
                    except Exception:
                        changed_digests.append((relpath, "unreadable"))
        except Exception as e:
            log.debug("compute_snapshot_hash: git status failed: %s", e)

    # Build hash from file content digests only
    h = hashlib.sha256()
    for relpath, digest in sorted(changed_digests):
        h.update(f"{relpath}:{digest}\n".encode())
    return h.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Advisory staleness from worktree edits
# ---------------------------------------------------------------------------

def mark_advisory_stale_after_edit(drive_root: pathlib.Path) -> None:
    """Mark all fresh advisory runs as stale because the worktree was modified.

    Called from write tools (_repo_write, _str_replace_editor) after a
    successful file write. This ensures the snapshot hash will differ on the
    next advisory_pre_review call, preventing false-fresh gate passes.
    """
    try:
        state = load_state(drive_root)
        has_invalidatable = any(r.status in ("fresh", "bypassed") for r in state.runs)
        if not has_invalidatable:
            return  # nothing to invalidate
        ts = _utc_now()
        state.mark_all_stale(reason_ts=ts)
        save_state(drive_root, state)
        log.debug("Advisory state marked stale after worktree edit at %s", ts)
    except Exception as e:
        log.debug("mark_advisory_stale_after_edit failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------

def format_status_section(state: AdvisoryReviewState,
                          repo_dir: Optional[pathlib.Path] = None) -> str:
    """Render a compact section for LLM context injection.

    Shows the last 3 advisory runs with their HISTORICAL status, hash, and key findings.
    NOTE: Statuses shown here reflect persisted state, not live worktree freshness.
    Use review_status tool (which computes the live snapshot hash) for gate-accurate
    freshness. A run showing FRESH here may be stale if worktree was edited via
    paths that don't auto-stale (e.g. claude_code_edit, manual edits).

    Plus the last commit attempt if blocked/failed, plus any open obligations.
    """
    if not state.runs and not state.last_commit_attempt:
        return "## Advisory Pre-Review Status\n\nNo advisory runs recorded yet."

    lines = ["## Advisory Pre-Review Status",
             "(Historical — run `review_status` for gate-accurate live freshness)"]

    for run in state.runs[-3:]:
        status_icon = {
            "fresh": "✅",
            "stale": "⚠️",
            "bypassed": "⏭️",
            "parse_failure": "🔴",
        }.get(run.status, "❓")

        ts_short = run.ts[:16] if len(run.ts) >= 16 else run.ts
        hash_short = run.snapshot_hash[:12]
        msg_short = run.commit_message[:60] + ("..." if len(run.commit_message) > 60 else "")

        lines.append(f"\n{status_icon} **{run.status.upper()}** | hash={hash_short} | {ts_short}")
        lines.append(f"   Commit: {msg_short}")

        if run.bypass_reason:
            lines.append(f"   Bypassed: {run.bypass_reason}")

        if run.snapshot_summary:
            lines.append(f"   Scope: {run.snapshot_summary}")

        findings = [i for i in (run.items or []) if isinstance(i, dict) and str(i.get("verdict", "")).upper() == "FAIL"]
        if findings:
            lines.append(f"   Findings ({len(findings)}):")
            for item in findings[:5]:
                sev = str(item.get("severity", "advisory")).upper()
                name = item.get("item", "?")
                reason = _truncate_review_reason(item.get("reason", ""))
                lines.append(f"     [{sev}] {name}: {reason}")
            if len(findings) > 5:
                lines.append(f"     ... and {len(findings) - 5} more")
        elif run.status in ("fresh", "bypassed", "parse_failure"):
            lines.append("   No findings recorded.")

    # Show staleness note from worktree edit
    if state.last_stale_from_edit_ts:
        lines.append(f"\n⚠️ Advisory marked stale after worktree edit at {state.last_stale_from_edit_ts[:16]}.")
        lines.append("   Run advisory_pre_review again before repo_commit.")

    # Show last commit attempt if blocked or failed
    ca = state.last_commit_attempt
    if ca and ca.status in ("blocked", "failed"):
        icon = "🚫" if ca.status == "blocked" else "❌"
        ts_short = ca.ts[:16] if len(ca.ts) >= 16 else ca.ts
        msg_short = ca.commit_message[:60] + ("..." if len(ca.commit_message) > 60 else "")
        lines.append(f"\n{icon} **Last commit {ca.status.upper()}** | {ts_short}")
        lines.append(f"   Commit: {msg_short}")
        if ca.block_reason:
            lines.append(f"   Reason: {ca.block_reason}")
        if ca.block_details:
            preview = _truncate_review_artifact(ca.block_details, limit=200).replace("\n", " ")
            lines.append(f"   Details: {preview}")
        if ca.duration_sec > 0:
            lines.append(f"   Duration: {ca.duration_sec:.1f}s")

    # Show open obligations
    open_obs = state.get_open_obligations()
    if open_obs:
        lines.append(f"\n📋 **Open obligations from previous blocking rounds ({len(open_obs)}):**")
        for ob in open_obs[:6]:
            _r = _truncate_review_reason(ob.reason)
            _m = (ob.source_attempt_msg if len(ob.source_attempt_msg) <= 60
                  else ob.source_attempt_msg[:60] + "...")
            lines.append(f"   [{ob.obligation_id}] [{ob.severity.upper()}] {ob.item}: {_r}")
            lines.append(f"      Source: {ob.source_attempt_ts[:16]} — \"{_m}\"")
        if len(open_obs) > 6:
            lines.append(f"   ... and {len(open_obs) - 6} more")
        lines.append("   Advisory MUST verify each obligation is resolved before PASS.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """ISO UTC timestamp."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
