"""Tests for local_model.py — preflight, install, and progress features."""
from __future__ import annotations

import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, call, patch


class TestCheckRuntime(unittest.TestCase):
    """check_runtime() sets _runtime_status and returns bool."""

    def _make_mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager()

    def test_check_runtime_ok(self):
        mgr = self._make_mgr()
        fake_result = MagicMock()
        fake_result.returncode = 0
        with patch("subprocess.run", return_value=fake_result):
            ok = mgr.check_runtime()
        self.assertTrue(ok)
        self.assertEqual(mgr._runtime_status, "ok")

    def test_check_runtime_missing(self):
        mgr = self._make_mgr()
        fake_result = MagicMock()
        fake_result.returncode = 1
        with patch("subprocess.run", return_value=fake_result):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_check_runtime_subprocess_exception(self):
        mgr = self._make_mgr()
        with patch("subprocess.run", side_effect=FileNotFoundError("no python")):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_check_runtime_timeout(self):
        mgr = self._make_mgr()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            ok = mgr.check_runtime()
        self.assertFalse(ok)
        self.assertEqual(mgr._runtime_status, "missing")


class TestInstallRuntime(unittest.TestCase):
    """install_runtime() manages _runtime_status lifecycle and _install_proc."""

    def _make_mgr(self):
        from ouroboros.local_model import LocalModelManager
        return LocalModelManager()

    def test_install_sets_installing_status(self):
        mgr = self._make_mgr()
        events = []

        def fake_run_install():
            events.append(mgr._runtime_status)

        mgr._run_install = fake_run_install
        # Patch threading so _run_install is called synchronously in test
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: fake_run_install()
            mgr.install_runtime()

        self.assertIn("installing", events)

    def test_install_already_installing_noop(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"
        started = []
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: started.append(1)
            mgr.install_runtime()
        # Should not have started another thread
        self.assertEqual(len(started), 0)

    def test_run_install_success(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        # check_runtime returns True after successful install
        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=True):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_ok")

    def test_run_install_pip_failure(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stdout = None
        fake_proc.wait.return_value = 1

        with patch("subprocess.Popen", return_value=fake_proc):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_error")

    def test_run_install_import_still_fails_after_pip(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=False):
            mgr._run_install()

        self.assertEqual(mgr._runtime_status, "install_error")
        self.assertIn("still fails", mgr._runtime_install_log)

    def test_run_install_clears_install_proc(self):
        mgr = self._make_mgr()
        mgr._runtime_status = "installing"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = None
        fake_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch.object(mgr, "check_runtime", return_value=True):
            mgr._run_install()

        self.assertIsNone(mgr._install_proc)

    def test_stop_server_terminates_install_proc(self):
        from ouroboros.local_model import LocalModelManager

        mgr = LocalModelManager()
        fake_install = MagicMock()
        fake_install.pid = 12345
        mgr._install_proc = fake_install

        with patch("ouroboros.local_model.terminate_process_tree") as mock_term, \
             patch("ouroboros.local_model.kill_process_tree"):
            mgr.stop_server()

        mock_term.assert_called_once_with(fake_install)
        self.assertIsNone(mgr._install_proc)


class TestStatusDict(unittest.TestCase):
    """status_dict() exposes runtime_status and download_progress."""

    def test_status_dict_has_runtime_status(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        d = mgr.status_dict()
        self.assertIn("runtime_status", d)
        self.assertIn("download_progress", d)

    def test_runtime_status_propagated(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._runtime_status = "install_ok"
        d = mgr.status_dict()
        self.assertEqual(d["runtime_status"], "install_ok")

    def test_runtime_install_log_truncated_in_status(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._runtime_install_log = "x" * 2000
        d = mgr.status_dict()
        self.assertLessEqual(len(d["runtime_install_log"]), 500)


class TestDownloadProgressCallback(unittest.TestCase):
    """download_model() updates _download_progress via tqdm_class callback."""

    def test_progress_updates_on_download(self):
        from ouroboros.local_model import LocalModelManager

        mgr = LocalModelManager()
        progress_values = []

        # Simulate hf_hub_download calling tqdm update
        def fake_hf_hub_download(repo_id, filename, resume_download, tqdm_class):
            # Simulate progress updates
            bar = tqdm_class(total=100)
            bar.update(50)
            bar.update(50)
            bar.close()
            return "/fake/path/model.gguf"

        tqdm_mod = types.ModuleType("tqdm")
        tqdm_auto_mod = types.ModuleType("tqdm.auto")

        class FakeTqdm:
            def __init__(self, total=None, **kw):
                self.n = 0
                self.total = total

            def update(self, n=1):
                self.n += n
                progress_values.append(self.n / self.total if self.total else 0)

            def close(self):
                pass

        tqdm_auto_mod.tqdm = FakeTqdm
        tqdm_mod.auto = tqdm_auto_mod

        import sys
        sys.modules.setdefault("tqdm", tqdm_mod)
        sys.modules.setdefault("tqdm.auto", tqdm_auto_mod)

        with patch("ouroboros.local_model.LocalModelManager.check_runtime", return_value=True), \
             patch("huggingface_hub.hf_hub_download", side_effect=fake_hf_hub_download, create=True):
            try:
                from ouroboros import local_model as lm
                # Patch within the module
                orig_hf = None
                try:
                    import huggingface_hub
                    orig_hf = huggingface_hub.hf_hub_download
                    huggingface_hub.hf_hub_download = fake_hf_hub_download
                except ImportError:
                    pass
                try:
                    path = mgr.download_model("some/repo", "model.gguf")
                    self.assertEqual(path, "/fake/path/model.gguf")
                    # _download_progress should have been updated to 1.0 (final)
                    self.assertEqual(mgr._download_progress, 1.0)
                finally:
                    if orig_hf is not None:
                        huggingface_hub.hf_hub_download = orig_hf
            except ImportError:
                self.skipTest("huggingface_hub not available")


class TestApiLocalModelStartPreflight(unittest.TestCase):
    """api_local_model_start returns 412 when runtime is missing."""

    def test_returns_412_when_runtime_missing(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_start

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = False
        mock_mgr._runtime_status = "missing"

        async def fake_json():
            return {"source": "some/repo", "filename": "m.gguf"}

        mock_request = MagicMock()
        mock_request.json = fake_json

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            resp = asyncio.get_event_loop().run_until_complete(
                api_local_model_start(mock_request)
            )

        self.assertEqual(resp.status_code, 412)
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["error"], "runtime_missing")
        self.assertIn("hint", body)
        # Download must NOT have been called
        mock_mgr.download_model.assert_not_called()

    def test_proceeds_when_runtime_ok(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_start

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = True

        async def fake_json():
            return {"source": "some/repo", "filename": "m.gguf"}

        mock_request = MagicMock()
        mock_request.json = fake_json

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_mgr.download_model.return_value = "/path/model.gguf"

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr), \
             patch("asyncio.to_thread", side_effect=fake_to_thread):
            resp = asyncio.get_event_loop().run_until_complete(
                api_local_model_start(mock_request)
            )

        self.assertNotEqual(resp.status_code, 412)
        mock_mgr.download_model.assert_called_once()


class TestAutoStartPreflight(unittest.TestCase):
    """auto_start_local_model skips download when runtime missing."""

    def test_skips_download_when_runtime_missing(self):
        from ouroboros.local_model_autostart import auto_start_local_model

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = False

        settings = {
            "LOCAL_MODEL_SOURCE": "some/repo",
            "LOCAL_MODEL_FILENAME": "model.gguf",
            "LOCAL_MODEL_PORT": 8766,
            "LOCAL_MODEL_N_GPU_LAYERS": 0,
            "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
            "LOCAL_MODEL_CHAT_FORMAT": "",
        }

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            auto_start_local_model(settings)

        mock_mgr.download_model.assert_not_called()
        mock_mgr.start_server.assert_not_called()
        # Status should be set to error with a meaningful message
        self.assertEqual(mock_mgr._status, "error")
        self.assertIn("llama-cpp-python", mock_mgr._error)

    def test_proceeds_when_runtime_ok(self):
        from ouroboros.local_model_autostart import auto_start_local_model

        mock_mgr = MagicMock()
        mock_mgr.is_running = False
        mock_mgr.check_runtime.return_value = True
        mock_mgr.download_model.return_value = "/path/model.gguf"

        settings = {
            "LOCAL_MODEL_SOURCE": "some/repo",
            "LOCAL_MODEL_FILENAME": "model.gguf",
            "LOCAL_MODEL_PORT": 8766,
            "LOCAL_MODEL_N_GPU_LAYERS": 0,
            "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
            "LOCAL_MODEL_CHAT_FORMAT": "",
        }

        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            auto_start_local_model(settings)

        mock_mgr.download_model.assert_called_once()
        mock_mgr.start_server.assert_called_once()


class TestInstallCancellationWindow(unittest.TestCase):
    """_run_install respects _install_cancelled flag set before Popen."""

    def test_cancels_before_popen_when_flag_set(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._install_cancelled.set()  # simulate stop_server() before thread starts

        mgr._run_install()

        # No subprocess should have been spawned and status reset to missing
        self.assertIsNone(mgr._install_proc)
        self.assertEqual(mgr._runtime_status, "missing")

    def test_clear_flag_on_new_install_attempt(self):
        from ouroboros.local_model import LocalModelManager
        mgr = LocalModelManager()
        mgr._install_cancelled.set()  # set from a previous stop

        with patch("subprocess.Popen") as mock_popen:
            # Calling install_runtime should clear the flag before starting
            # the thread (we check the flag was cleared, not that Popen ran)
            mgr.install_runtime()
            self.assertFalse(mgr._install_cancelled.is_set())


class TestStatusApiAutoProbe(unittest.TestCase):
    """api_local_model_status probes runtime on first poll when status unknown."""

    def test_status_probes_runtime_when_unknown(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_status

        mock_mgr = MagicMock()
        mock_mgr._runtime_status = "unknown"
        mock_mgr.get_status.return_value = "offline"
        mock_mgr.status_dict.return_value = {"status": "offline", "runtime_status": "missing"}

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        mock_request = MagicMock()
        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr), \
             patch("asyncio.to_thread", side_effect=fake_to_thread):
            asyncio.get_event_loop().run_until_complete(api_local_model_status(mock_request))

        mock_mgr.check_runtime.assert_called_once()

    def test_status_skips_probe_when_already_known(self):
        import asyncio
        from ouroboros.local_model_api import api_local_model_status

        mock_mgr = MagicMock()
        mock_mgr._runtime_status = "ok"  # already probed
        mock_mgr.get_status.return_value = "offline"
        mock_mgr.status_dict.return_value = {"status": "offline", "runtime_status": "ok"}

        mock_request = MagicMock()
        with patch("ouroboros.local_model.get_manager", return_value=mock_mgr):
            import asyncio
            asyncio.get_event_loop().run_until_complete(api_local_model_status(mock_request))

        mock_mgr.check_runtime.assert_not_called()


class TestGetInstallCommand(unittest.TestCase):
    """_get_install_command returns a list starting with sys.executable."""

    def test_command_uses_sys_executable(self):
        from ouroboros.local_model import _get_install_command
        cmd = _get_install_command()
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn("llama-cpp-python[server]", cmd)

    def test_env_has_cmake_args_on_macos(self):
        from ouroboros.local_model import _get_install_env
        with patch("ouroboros.local_model.IS_MACOS", True), \
             patch("ouroboros.local_model.IS_WINDOWS", False):
            env = _get_install_env()
        self.assertIn("CMAKE_ARGS", env)
        self.assertIn("METAL", env["CMAKE_ARGS"].upper())

    def test_env_has_no_cmake_args_on_linux(self):
        from ouroboros.local_model import _get_install_env
        with patch("ouroboros.local_model.IS_MACOS", False), \
             patch("ouroboros.local_model.IS_WINDOWS", False):
            env = _get_install_env()
        self.assertNotIn("CMAKE_ARGS", env)


if __name__ == "__main__":
    unittest.main()
