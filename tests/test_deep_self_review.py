"""Tests for ouroboros.deep_self_review module."""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest

from ouroboros.deep_self_review import (
    build_review_pack,
    is_review_available,
    run_deep_self_review,
)


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal git repo with tracked files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n")
    (repo / "lib.py").write_text("def add(a, b): return a + b\n")
    return repo


@pytest.fixture
def tmp_drive(tmp_path):
    """Create a drive root with some memory files."""
    drive = tmp_path / "drive"
    drive.mkdir()
    mem = drive / "memory"
    mem.mkdir()
    (mem / "identity.md").write_text("I am Ouroboros.\n")
    (mem / "scratchpad.md").write_text("Working notes.\n")
    know = mem / "knowledge"
    know.mkdir()
    (know / "patterns.md").write_text("## Patterns\n- Error class A\n")
    return drive


class TestBuildReviewPack:
    def test_reads_tracked_files(self, tmp_repo, tmp_drive):
        """git ls-files output determines which repo files are included."""
        git_output = "main.py\nlib.py\n"
        with mock.patch("ouroboros.deep_self_review.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout=git_output, returncode=0)
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: main.py" in pack
        assert "## FILE: lib.py" in pack
        assert "print('hello')" in pack
        assert stats["file_count"] >= 2

    def test_includes_memory_whitelist(self, tmp_repo, tmp_drive):
        """Memory whitelist files from drive_root are included."""
        with mock.patch("ouroboros.deep_self_review.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="main.py\n", returncode=0)
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: drive/memory/identity.md" in pack
        assert "I am Ouroboros." in pack
        assert "## FILE: drive/memory/scratchpad.md" in pack
        assert "## FILE: drive/memory/knowledge/patterns.md" in pack

    def test_skips_missing_memory(self, tmp_repo, tmp_drive):
        """Missing memory files are silently skipped."""
        with mock.patch("ouroboros.deep_self_review.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(stdout="main.py\n", returncode=0)
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        # registry.md, WORLD.md, index-full.md don't exist — should not appear
        assert "registry.md" not in pack
        assert "WORLD.md" not in pack
        assert "index-full.md" not in pack


class TestIsReviewAvailable:
    def test_openrouter(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
            available, model = is_review_available()
        assert available is True
        assert model == "openai/gpt-5.4-pro"

    def test_openai(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env, clear=False):
            # Ensure OPENROUTER_API_KEY and OPENAI_BASE_URL are not set
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            available, model = is_review_available()
        assert available is True
        assert model == "openai::gpt-5.4-pro"

    def test_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            available, model = is_review_available()
        assert available is False
        assert model is None


class TestRequestToolEmitsEvent:
    def test_emits_correct_event(self):
        """_request_deep_self_review emits a deep_self_review_request event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.is_review_available",
            return_value=(True, "openai/gpt-5.4-pro"),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 1
        evt = ctx.pending_events[0]
        assert evt["type"] == "deep_self_review_request"
        assert evt["reason"] == "test reason"
        assert evt["model"] == "openai/gpt-5.4-pro"
        assert "Deep self-review" in result

    def test_unavailable_returns_error(self):
        """When no API key is available, returns error without emitting event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.is_review_available",
            return_value=(False, None),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 0
        assert "unavailable" in result


class TestReviewPackOverflow:
    def test_explicit_error_on_overflow(self, tmp_repo, tmp_drive):
        """When pack exceeds ~900K tokens, run_deep_self_review returns an error."""
        # Create a pack that's way too large (> 3.15M chars ≈ 900K tokens)
        huge_pack = "x" * 4_000_000
        mock_llm = mock.Mock()

        with mock.patch(
            "ouroboros.deep_self_review.build_review_pack",
            return_value=(huge_pack, {"file_count": 100, "total_chars": 4_000_000, "skipped": []}),
        ):
            result, usage = run_deep_self_review(
                repo_dir=tmp_repo,
                drive_root=tmp_drive,
                llm=mock_llm,
                emit_progress=lambda x: None,
                event_queue=None,
                model="test-model",
            )

        assert "too large" in result
        assert "900,000" in result
        assert usage == {}
        mock_llm.chat.assert_not_called()
