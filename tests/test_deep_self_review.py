"""Tests for ouroboros.deep_self_review module."""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest

from ouroboros.deep_self_review import (
    _is_probably_binary,
    build_review_pack,
    is_review_available,
    run_deep_self_review,
)


def _make_dulwich_mock(file_list: list[str]):
    """Return a mock for dulwich.repo.Repo that yields the given file list from open_index()."""
    mock_index = mock.Mock()
    mock_index.__iter__ = mock.Mock(return_value=iter(f.encode() for f in file_list))
    mock_repo = mock.Mock()
    mock_repo.open_index.return_value = mock_index
    mock_repo_cls = mock.Mock(return_value=mock_repo)
    return mock_repo_cls


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
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: main.py" in pack
        assert "## FILE: lib.py" in pack
        assert "print('hello')" in pack
        assert stats["file_count"] >= 2

    def test_includes_memory_whitelist(self, tmp_repo, tmp_drive):
        """Memory whitelist files from drive_root are included."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: drive/memory/identity.md" in pack
        assert "I am Ouroboros." in pack
        assert "## FILE: drive/memory/scratchpad.md" in pack
        assert "## FILE: drive/memory/knowledge/patterns.md" in pack

    def test_skips_missing_memory(self, tmp_repo, tmp_drive):
        """Missing memory files are silently skipped."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
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


class TestVendoredFilesExcluded:
    def test_minified_js_skipped(self, tmp_repo, tmp_drive):
        """Files with .min.js suffix are excluded from the review pack."""
        (tmp_repo / "lib.min.js").write_text("!function(){var a=1;}()\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.min.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "lib.min.js" not in pack or "vendored/minified" in str(stats["skipped"])
        assert "## FILE: lib.min.js" not in pack

    def test_chart_umd_skipped(self, tmp_repo, tmp_drive):
        """chart.umd.min.js (vendored Chart.js) is excluded by name and appears in OMITTED section."""
        (tmp_repo / "chart.umd.min.js").write_text("!function(t,e){/* chart.js minified */}()\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "chart.umd.min.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: chart.umd.min.js" not in pack
        assert any("chart.umd.min.js" in s for s in stats["skipped"])
        # Omission section must be present and mention the file
        assert "## OMITTED FILES" in pack
        assert "chart.umd.min.js" in pack

    def test_min_css_skipped(self, tmp_repo, tmp_drive):
        """Files with .min.css suffix are excluded."""
        (tmp_repo / "style.min.css").write_text("body{margin:0}a{color:red}\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "style.min.css"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: style.min.css" not in pack
        assert any("style.min.css" in s for s in stats["skipped"])

    def test_regular_js_included(self, tmp_repo, tmp_drive):
        """Regular (non-minified) JS files are NOT excluded."""
        (tmp_repo / "app.js").write_text("console.log('hello');\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "app.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: app.js" in pack
        assert not any("app.js" in s for s in stats["skipped"])

    def test_omission_section_after_memory_whitelist(self, tmp_repo, tmp_drive):
        """OMITTED FILES section is appended after both repo and memory passes, capturing all skips.

        Simulates a memory-whitelist read error by patching pathlib.Path.read_text so that
        identity.md raises PermissionError, ensuring it lands in skipped and the OMITTED section.
        """
        (tmp_repo / "lib.min.js").write_text("minified\n")
        (tmp_drive / "memory" / "identity.md").write_text("I am Ouroboros.\n")
        target_path = str(tmp_drive / "memory" / "identity.md")

        original_read_text = pathlib.Path.read_text

        def patched_read_text(self, encoding="utf-8", errors="replace"):
            if str(self) == target_path:
                raise PermissionError("mocked read error")
            return original_read_text(self, encoding=encoding, errors=errors)

        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.min.js"])):
            with mock.patch("pathlib.Path.read_text", patched_read_text):
                pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## OMITTED FILES" in pack
        omitted_section_pos = pack.index("## OMITTED FILES")
        # Vendored file listed in omitted section
        assert "lib.min.js" in pack[omitted_section_pos:]
        # Memory read error captured in skipped
        memory_errors = [s for s in stats["skipped"] if "identity.md" in s and "read error" in s]
        assert memory_errors, "identity.md read error should appear in skipped"
        # And it appears in the OMITTED section too
        assert "identity.md" in pack[omitted_section_pos:]


class TestIsProbablyBinary:
    def test_nul_byte_is_binary(self, tmp_path):
        """File containing a NUL byte is detected as binary."""
        f = tmp_path / "blob.bin"
        f.write_bytes(b"some text\x00more text")
        assert _is_probably_binary(f) is True

    def test_plain_text_is_not_binary(self, tmp_path):
        """Plain text file is not detected as binary."""
        f = tmp_path / "script.py"
        f.write_text("def hello():\n    return 'world'\n")
        assert _is_probably_binary(f) is False

    def test_high_non_printable_ratio_is_binary(self, tmp_path):
        """File with >30% non-printable bytes (ASCII control range) is detected as binary."""
        # 40% non-printable (bytes 1–8 range, ASCII control chars)
        payload = bytes(range(1, 9)) * 10 + b"normal text" * 3
        f = tmp_path / "data.unknown"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_high_byte_ratio_is_binary(self, tmp_path):
        """File with >30% bytes >= 128 (non-UTF-8/mixed-encoding blobs) is detected as binary."""
        # 50% bytes >= 128, typical of non-UTF-8 binary blobs
        payload = bytes(range(128, 256)) * 5 + b"ascii text" * 5
        f = tmp_path / "data.blob"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_only_first_sniff_bytes_read(self, tmp_path):
        """_is_probably_binary only reads _BINARY_SNIFF_BYTES bytes, not the whole file."""
        from ouroboros.deep_self_review import _BINARY_SNIFF_BYTES
        # File is mostly text but has NUL in the first 8KB window
        payload = b"text data\x00more" + b"a" * (_BINARY_SNIFF_BYTES * 2)
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        # Should detect NUL in the first chunk and return True
        assert _is_probably_binary(f) is True

    def test_empty_file_is_not_binary(self, tmp_path):
        """Empty file does not crash and returns False."""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _is_probably_binary(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        """Missing file returns False (let caller handle read failure)."""
        f = tmp_path / "does_not_exist.bin"
        assert _is_probably_binary(f) is False

    def test_unlisted_extension_binary_excluded_from_pack(self, tmp_repo, tmp_drive):
        """Binary file with unlisted extension (.bin) is excluded via content sniffer."""
        (tmp_repo / "model.bin").write_bytes(b"GGUF\x00" + b"\x00\xff" * 100)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "model.bin"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: model.bin" not in pack
        assert any("model.bin" in s for s in stats["skipped"])


class TestBinaryFilesExcluded:
    def test_png_skipped(self, tmp_repo, tmp_drive):
        """PNG images are excluded — reading them produces garbage replacement chars."""
        (tmp_repo / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "screenshot.png"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: screenshot.png" not in pack
        assert any("screenshot.png" in s for s in stats["skipped"])

    def test_jpg_skipped(self, tmp_repo, tmp_drive):
        """JPEG images are excluded."""
        (tmp_repo / "logo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "logo.jpg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: logo.jpg" not in pack
        assert any("logo.jpg" in s for s in stats["skipped"])

    def test_svg_skipped(self, tmp_repo, tmp_drive):
        """SVG files are excluded (provider icons can be large XML)."""
        (tmp_repo / "icon.svg").write_text("<svg><circle r='10'/></svg>\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "icon.svg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: icon.svg" not in pack
        assert any("icon.svg" in s for s in stats["skipped"])

    def test_ico_skipped(self, tmp_repo, tmp_drive):
        """ICO files are excluded."""
        (tmp_repo / "favicon.ico").write_bytes(b"\x00\x00\x01\x00" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "favicon.ico"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: favicon.ico" not in pack
        assert any("favicon.ico" in s for s in stats["skipped"])

    def test_python_source_not_skipped(self, tmp_repo, tmp_drive):
        """Python source files (.py) are NOT excluded by the binary filter."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: main.py" in pack


class TestSkipDirPrefixes:
    def test_assets_dir_excluded(self, tmp_repo, tmp_drive):
        """Files under assets/ are excluded (README screenshots, app icons)."""
        assets = tmp_repo / "assets"
        assets.mkdir()
        (assets / "chat.png").write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)
        (assets / "logo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "assets/chat.png", "assets/logo.jpg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: assets/chat.png" not in pack
        assert "## FILE: assets/logo.jpg" not in pack
        assert any("assets/chat.png" in s for s in stats["skipped"])
        assert any("assets/logo.jpg" in s for s in stats["skipped"])
        assert "## FILE: main.py" in pack  # non-assets file still present

    def test_webview_dir_excluded(self, tmp_repo, tmp_drive):
        """Files under webview/ are excluded (legacy PyWebView JS helpers)."""
        wv = tmp_repo / "webview" / "js"
        wv.mkdir(parents=True)
        (wv / "polyfill.js").write_text("/* polyfill */\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "webview/js/polyfill.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: webview/js/polyfill.js" not in pack
        assert any("webview/js/polyfill.js" in s for s in stats["skipped"])
        assert "## FILE: main.py" in pack

    def test_web_dir_not_excluded(self, tmp_repo, tmp_drive):
        """Files under web/ (SPA modules) are NOT excluded."""
        web = tmp_repo / "web" / "modules"
        web.mkdir(parents=True)
        (web / "chat.js").write_text("// chat module\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "web/modules/chat.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: web/modules/chat.js" in pack
        assert not any("web/modules/chat.js" in s for s in stats["skipped"])


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
