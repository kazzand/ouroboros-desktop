"""Tests verifying the uv migration: pyproject.toml as SSOT, uv.lock present,
requirements files removed, CI/Docker/build scripts use uv."""

import pathlib

REPO = pathlib.Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class TestPyprojectIsSSoT:
    """pyproject.toml is the single source of truth for dependencies."""

    def test_requirements_txt_removed(self):
        assert not (REPO / "requirements.txt").exists(), (
            "requirements.txt should be removed — pyproject.toml is the SSOT"
        )

    def test_requirements_launcher_removed(self):
        assert not (REPO / "requirements-launcher.txt").exists(), (
            "requirements-launcher.txt should be removed — desktop extras live in pyproject.toml"
        )

    def test_uv_lock_exists(self):
        assert (REPO / "uv.lock").exists(), (
            "uv.lock must be committed for deterministic builds"
        )

    def test_pyproject_has_desktop_extra(self):
        src = _read("pyproject.toml")
        assert "desktop" in src, "pyproject.toml must have a 'desktop' optional-dependency group"
        assert "pywebview" in src, "desktop extra must include pywebview"

    def test_pyproject_has_dev_group(self):
        src = _read("pyproject.toml")
        assert "[dependency-groups]" in src, "pyproject.toml must have [dependency-groups]"
        assert '"pytest"' in src, "dev dependency group must include pytest"


class TestCIUsesUv:
    """CI workflow must use uv for dependency management."""

    def test_ci_uses_setup_uv(self):
        src = _read(".github/workflows/ci.yml")
        assert "astral-sh/setup-uv" in src, (
            "CI must use astral-sh/setup-uv action"
        )

    def test_ci_uses_uv_sync(self):
        src = _read(".github/workflows/ci.yml")
        assert "uv sync --frozen" in src, (
            "CI must use 'uv sync --frozen' for deterministic dependency install"
        )

    def test_ci_uses_uv_run_pytest(self):
        src = _read(".github/workflows/ci.yml")
        assert "uv run pytest" in src, (
            "CI must use 'uv run pytest' to run tests"
        )

    def test_ci_no_pip_install(self):
        src = _read(".github/workflows/ci.yml")
        # Build job may still use uv pip install --system
        lines = src.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "pip install" in stripped and "uv pip install" not in stripped:
                assert False, (
                    f"CI should not use bare 'pip install' — use 'uv pip install' or 'uv sync': {stripped}"
                )


class TestDockerUsesUv:
    """Dockerfile must use uv for dependency management."""

    def test_dockerfile_has_uv(self):
        src = _read("Dockerfile")
        assert "uv" in src, "Dockerfile must reference uv"

    def test_dockerfile_copies_lockfile(self):
        src = _read("Dockerfile")
        assert "uv.lock" in src or "COPY . ." in src, (
            "Dockerfile must COPY uv.lock (explicitly or via COPY . .)"
        )

    def test_dockerfile_copies_pyproject(self):
        src = _read("Dockerfile")
        assert "pyproject.toml" in src or "COPY . ." in src, (
            "Dockerfile must COPY pyproject.toml (explicitly or via COPY . .)"
        )

    def test_dockerfile_no_requirements_txt(self):
        src = _read("Dockerfile")
        assert "requirements.txt" not in src, (
            "Dockerfile should not reference requirements.txt"
        )


class TestBuildScriptsUseUv:
    """Build scripts must use uv for dependency installation."""

    def test_build_sh_checks_uv(self):
        src = _read("build.sh")
        assert "command -v uv" in src, "build.sh must check for uv availability"

    def test_build_sh_uses_uv_pip(self):
        src = _read("build.sh")
        assert "uv pip install" in src, "build.sh must use uv pip install"

    def test_build_sh_no_requirements_txt(self):
        src = _read("build.sh")
        assert "requirements.txt" not in src and "requirements-launcher.txt" not in src

    def test_build_linux_checks_uv(self):
        src = _read("build_linux.sh")
        assert "command -v uv" in src, "build_linux.sh must check for uv availability"

    def test_build_linux_uses_uv_pip(self):
        src = _read("build_linux.sh")
        assert "uv pip install" in src, "build_linux.sh must use uv pip install"

    def test_build_linux_no_requirements_txt(self):
        src = _read("build_linux.sh")
        assert "requirements.txt" not in src and "requirements-launcher.txt" not in src

    def test_build_windows_checks_uv(self):
        src = _read("build_windows.ps1")
        assert "Get-Command uv" in src or "command -v uv" in src, (
            "build_windows.ps1 must check for uv availability"
        )

    def test_build_windows_uses_uv_pip(self):
        src = _read("build_windows.ps1")
        assert "uv pip install" in src, "build_windows.ps1 must use uv pip install"

    def test_build_windows_no_requirements_txt(self):
        src = _read("build_windows.ps1")
        assert "requirements.txt" not in src and "requirements-launcher.txt" not in src


class TestBrowserExtraContract:
    """All install paths that precede 'playwright install' must include the
    browser extra so playwright is actually installed."""

    def test_dockerfile_installs_browser_extra(self):
        src = _read("Dockerfile")
        # The install command must use browser extra before any playwright invocation
        # Accept both uv sync --extra browser and uv pip install .[browser]
        assert ("--extra browser" in src or '".[browser]"' in src
                or "'.[browser]'" in src), (
            "Dockerfile install must include browser extra for playwright"
        )

    def test_build_sh_installs_browser_extra(self):
        src = _read("build.sh")
        assert '".[browser]"' in src or "'.[browser]'" in src or ".[browser]" in src, (
            "build.sh must install with browser extra for playwright"
        )

    def test_build_linux_installs_browser_extra(self):
        src = _read("build_linux.sh")
        assert '".[browser]"' in src or "'.[browser]'" in src or ".[browser]" in src, (
            "build_linux.sh must install with browser extra for playwright"
        )

    def test_build_windows_installs_browser_extra(self):
        src = _read("build_windows.ps1")
        assert '".[browser]"' in src or "'.[browser]'" in src or ".[browser]" in src, (
            "build_windows.ps1 must install with browser extra for playwright"
        )

    def test_git_ops_sync_uses_browser_extra(self):
        src = _read("supervisor/git_ops.py")
        # Find the sync_runtime_dependencies function and check it uses [browser]
        func_start = src.find("def sync_runtime_dependencies")
        assert func_start != -1, "sync_runtime_dependencies not found"
        func_section = src[func_start:func_start + 500]
        assert "[browser]" in func_section, (
            "sync_runtime_dependencies must install with [browser] extra"
        )

    def test_launcher_bootstrap_uses_browser_extra(self):
        src = _read("ouroboros/launcher_bootstrap.py")
        func_start = src.find("def install_deps")
        assert func_start != -1, "install_deps not found"
        func_section = src[func_start:func_start + 500]
        assert "[browser]" in func_section, (
            "launcher_bootstrap.install_deps must install with [browser] extra"
        )

    def test_download_scripts_use_browser_extra(self):
        for script in ("scripts/download_python_standalone.sh",
                       "scripts/download_python_standalone.ps1"):
            src = _read(script)
            assert "[browser]" in src, (
                f"{script} must install with [browser] extra for playwright"
            )

    def test_dockerfile_copies_source_before_install(self):
        """The Dockerfile must COPY the source tree before running the
        install — the project needs the actual package dirs."""
        src = _read("Dockerfile")
        copy_all_pos = src.find("COPY . .")
        # Accept uv sync, uv pip install, or pip install
        install_pos = src.find("uv sync")
        if install_pos == -1:
            install_pos = src.find("uv pip install")
        if install_pos == -1:
            install_pos = src.find("pip install")
        assert copy_all_pos != -1, "Dockerfile must have 'COPY . .'"
        assert install_pos != -1, "Dockerfile must have an install step"
        assert copy_all_pos < install_pos, (
            "Dockerfile must COPY source tree BEFORE running project install "
            f"(COPY at {copy_all_pos}, install at {install_pos})"
        )

    def test_uv_lock_version_matches_pyproject(self):
        """uv.lock must record the same version as pyproject.toml."""
        import re
        pyproject_src = _read("pyproject.toml")
        m = re.search(r'version\s*=\s*"([^"]+)"', pyproject_src)
        assert m, "pyproject.toml must have a version field"
        pyproject_version = m.group(1)

        lock_src = _read("uv.lock")
        # uv.lock stores version in the [[package]] entry for the root
        lock_m = re.search(r'name\s*=\s*"ouroboros"\s*\nversion\s*=\s*"([^"]+)"', lock_src)
        assert lock_m, "uv.lock must contain a version entry for ouroboros"
        lock_version = lock_m.group(1)

        assert lock_version == pyproject_version, (
            f"uv.lock version ({lock_version}) must match pyproject.toml ({pyproject_version})"
        )


class TestBundleManifest:
    """PyInstaller spec and launcher bootstrap must reference uv.lock, not requirements files."""

    def test_spec_includes_uv_lock(self):
        spec_path = REPO / "Ouroboros.spec"
        if not spec_path.exists():
            return  # skip in environments without spec
        src = spec_path.read_text(encoding="utf-8")
        assert "uv.lock" in src, "Ouroboros.spec must bundle uv.lock"
        assert "requirements.txt" not in src, (
            "Ouroboros.spec must not reference requirements.txt"
        )
        assert "requirements-launcher.txt" not in src, (
            "Ouroboros.spec must not reference requirements-launcher.txt"
        )

    def test_managed_bundle_paths_includes_uv_lock(self):
        bootstrap_path = REPO / "ouroboros" / "launcher_bootstrap.py"
        if not bootstrap_path.exists():
            return
        src = bootstrap_path.read_text(encoding="utf-8")
        # Extract just the MANAGED_BUNDLE_PATHS tuple
        start = src.find("MANAGED_BUNDLE_PATHS = (")
        assert start != -1, "MANAGED_BUNDLE_PATHS not found"
        end = src.find(")", start) + 1
        bundle_section = src[start:end]
        assert '"uv.lock"' in bundle_section, (
            "MANAGED_BUNDLE_PATHS must include uv.lock"
        )
        assert '"requirements.txt"' not in bundle_section, (
            "MANAGED_BUNDLE_PATHS must not include requirements.txt"
        )
        assert '"requirements-launcher.txt"' not in bundle_section, (
            "MANAGED_BUNDLE_PATHS must not include requirements-launcher.txt"
        )
