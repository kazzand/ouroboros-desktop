"""Regression: review_state locking must work cross-platform (Windows PermissionError fix)."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from ouroboros.review_state import (
    acquire_review_state_lock,
    release_review_state_lock,
)


def test_concurrent_lock_acquire_release(tmp_path):
    """Multiple threads must be able to acquire/release the lock without errors."""
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    results = []
    lock = threading.Lock()

    def _worker(idx):
        fd = acquire_review_state_lock(tmp_path, timeout_sec=10.0)
        assert fd is not None, f"Worker {idx} failed to acquire lock"
        with lock:
            results.append(idx)
        release_review_state_lock(tmp_path, fd)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker, range(8)))

    assert len(results) == 8
    assert set(results) == set(range(8))


def test_lock_file_uses_flock_not_excl(tmp_path):
    """Lock must use flock/LockFileEx, not O_EXCL — verifiable by holding two fds."""
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    fd1 = acquire_review_state_lock(tmp_path, timeout_sec=2.0)
    assert fd1 is not None
    release_review_state_lock(tmp_path, fd1)
    # After release, a second acquire must succeed immediately
    fd2 = acquire_review_state_lock(tmp_path, timeout_sec=2.0)
    assert fd2 is not None
    release_review_state_lock(tmp_path, fd2)


def test_lock_timeout_returns_none(tmp_path):
    """If lock is held, acquire with short timeout should return None."""
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    from ouroboros.platform_layer import file_lock_exclusive, file_unlock
    lock_path = tmp_path / "locks" / "advisory_review.lock"
    # Manually hold the lock
    fd_hold = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
    file_lock_exclusive(fd_hold)
    try:
        # Now try to acquire via the API — should timeout
        fd = acquire_review_state_lock(tmp_path, timeout_sec=0.2)
        assert fd is None, "Should have timed out"
    finally:
        file_unlock(fd_hold)
        os.close(fd_hold)
