"""Regression tests for platform build scripts.

These tests ensure each build script contains the critical Playwright Chromium
install step with the correct env-var flag and that the install step appears
BEFORE the actual PyInstaller command-line invocation — so Chromium is always
bundled inside the ``python-standalone`` data tree before packaging.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _find_pyinstaller_cmd_pos(src: str) -> int:
    """Return the character position of the first line that actually *runs*
    PyInstaller (i.e. contains 'PyInstaller' outside a comment/echo line).

    Build scripts have comment lines and echo lines mentioning 'PyInstaller'
    before the actual invocation; we need the real command line.
    """
    for match in re.finditer(r"PyInstaller", src):
        # Find the start of the line containing this match.
        line_start = src.rfind("\n", 0, match.start()) + 1
        line = src[line_start: src.find("\n", match.start())]
        stripped = line.strip()
        # Skip comment lines (bash: '#', PowerShell: '#') and echo/Write-Host.
        if stripped.startswith("#") or stripped.lower().startswith("echo") or stripped.lower().startswith("write-host"):
            continue
        return match.start()
    return -1


# ---------------------------------------------------------------------------
# build.sh  (macOS)
# ---------------------------------------------------------------------------

class TestBuildSh:
    """build.sh must install the Chromium headless shell before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build.sh")
        assert "playwright install --only-shell chromium" in src, (
            "build.sh must call 'playwright install --only-shell chromium' on macOS"
        )

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build.sh")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src, (
            "build.sh must set PLAYWRIGHT_BROWSERS_PATH=0 for the playwright install step"
        )

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build.sh")
        pw_pos = src.find("playwright install --only-shell chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1, "playwright install --only-shell chromium not found in build.sh"
        assert pi_pos != -1, "PyInstaller command not found in build.sh"
        assert pw_pos < pi_pos, (
            "playwright install --only-shell chromium must appear BEFORE PyInstaller in build.sh "
            f"(found at char {pw_pos}, PyInstaller cmd at {pi_pos})"
        )

    def test_symlink_normalizer_skips_playwright_browser_bundles(self):
        src = _read("build.sh")
        assert "_should_skip_symlink" in src, (
            "build.sh should centralize the macOS symlink-skip guard for bundled "
            "browser bundles"
        )
        assert ".local-browsers" in src, (
            "build.sh must skip symlink normalization inside Playwright's bundled "
            "browser tree on macOS"
        )
        assert ".app" in src and ".framework" in src, (
            "build.sh must preserve nested macOS app/framework bundles during "
            "symlink normalization"
        )


# ---------------------------------------------------------------------------
# build_linux.sh  (Linux)
# ---------------------------------------------------------------------------

class TestBuildLinuxSh:
    """build_linux.sh must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build_linux.sh")
        assert "playwright install chromium" in src

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build_linux.sh")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build_linux.sh")
        pw_pos = src.find("playwright install chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1
        assert pi_pos != -1
        assert pw_pos < pi_pos, (
            "playwright install chromium must appear BEFORE PyInstaller in build_linux.sh"
        )


# ---------------------------------------------------------------------------
# build_windows.ps1  (Windows / PowerShell)
# ---------------------------------------------------------------------------

class TestBuildWindowsPs1:
    """build_windows.ps1 must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build_windows.ps1")
        assert "playwright install chromium" in src

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build_windows.ps1")
        # PowerShell syntax: $env:PLAYWRIGHT_BROWSERS_PATH = "0"
        assert 'PLAYWRIGHT_BROWSERS_PATH' in src and '"0"' in src, (
            "build_windows.ps1 must set PLAYWRIGHT_BROWSERS_PATH to '0'"
        )

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build_windows.ps1")
        pw_pos = src.find("playwright install chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1
        assert pi_pos != -1
        assert pw_pos < pi_pos, (
            "playwright install chromium must appear BEFORE PyInstaller in build_windows.ps1"
        )


# ---------------------------------------------------------------------------
# Dockerfile  (Docker / web runtime)
# ---------------------------------------------------------------------------

class TestDockerfile:
    """Dockerfile must install Playwright Chromium binary so browser tools work
    out of the box in the container without additional setup."""

    def test_playwright_install_chromium_present(self):
        src = _read("Dockerfile")
        assert "playwright install chromium" in src, (
            "Dockerfile must call 'playwright install chromium' to bundle the browser"
        )

    def test_playwright_browsers_path_zero_set(self):
        src = _read("Dockerfile")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src, (
            "Dockerfile must set PLAYWRIGHT_BROWSERS_PATH=0 so Chromium installs "
            "inside the pip package tree (not into a user cache that won't survive "
            "image layer boundaries)"
        )

    def test_playwright_install_deps_present(self):
        """Dockerfile must use 'playwright install-deps chromium' (the authoritative
        Playwright dependency resolver) rather than a hand-curated apt library list.
        This ensures all runtime native libs required by Chromium are present."""
        src = _read("Dockerfile")
        assert "playwright install-deps chromium" in src, (
            "Dockerfile must call 'playwright install-deps chromium' to install all "
            "native system libraries required by Chromium via Playwright's authoritative "
            "dependency resolver"
        )

    def test_install_deps_before_install_chromium(self):
        """Native system dependencies must be installed BEFORE the Chromium binary
        is downloaded, so the binary can find its runtime libraries on first launch."""
        src = _read("Dockerfile")
        deps_pos = src.find("playwright install-deps chromium")
        binary_pos = src.find("playwright install chromium")
        # binary_pos must not match the install-deps line itself
        # find the standalone 'playwright install chromium' (not install-deps)
        import re as _re
        binary_match = _re.search(r"(?<!install-deps )playwright install chromium", src)
        assert deps_pos != -1, "playwright install-deps chromium not found in Dockerfile"
        assert binary_match is not None, "standalone playwright install chromium not found in Dockerfile"
        assert deps_pos < binary_match.start(), (
            "playwright install-deps must appear BEFORE playwright install chromium in Dockerfile"
        )

    def test_dep_install_before_playwright_install_deps(self):
        """Dependency install (uv sync, uv pip install, or pip install) must appear BEFORE
        playwright install-deps chromium — the playwright Python package must
        be importable when install-deps runs."""
        src = _read("Dockerfile")
        # Accept uv sync, uv pip install, and plain pip install
        pip_pos = src.find("uv sync")
        if pip_pos == -1:
            pip_pos = src.find("uv pip install")
        if pip_pos == -1:
            pip_pos = src.find("pip install")
        deps_pos = src.find("playwright install-deps chromium")
        assert pip_pos != -1, "dependency install step not found in Dockerfile"
        assert deps_pos != -1, "playwright install-deps chromium not found in Dockerfile"
        assert pip_pos < deps_pos, (
            "dependency install must appear BEFORE playwright install-deps chromium in Dockerfile "
            f"(install at char {pip_pos}, install-deps at {deps_pos})"
        )

    def test_dep_install_before_all_playwright_invocations(self):
        """Dependency install (uv sync, uv pip install, or pip install) must appear BEFORE
        every ``python3 -m playwright ...`` invocation in the Dockerfile —
        both ``install-deps`` and ``install chromium``.
        If *any* playwright invocation precedes the install, ModuleNotFoundError occurs."""
        src = _read("Dockerfile")
        # Accept uv sync, uv pip install, and plain pip install
        pip_pos = src.find("uv sync")
        if pip_pos == -1:
            pip_pos = src.find("uv pip install")
        if pip_pos == -1:
            pip_pos = src.find("pip install")
        assert pip_pos != -1, "dependency install step not found in Dockerfile"

        import re as _re
        # Match both "python3 -m playwright" and "python -m playwright" (including "uv run python -m playwright")
        playwright_invocations = [
            m.start() for m in _re.finditer(r"python3? -m playwright", src)
        ]
        assert playwright_invocations, "No 'python(3) -m playwright' invocations found in Dockerfile"

        earliest_playwright = min(playwright_invocations)
        assert pip_pos < earliest_playwright, (
            "dependency install must appear BEFORE the earliest 'python -m playwright' invocation "
            f"in the Dockerfile (install at char {pip_pos}, earliest playwright at {earliest_playwright}). "
            f"Found {len(playwright_invocations)} playwright invocation(s) at positions: "
            f"{playwright_invocations}"
        )


# ──────────────────────────────────────────────────────────────────────
# macOS signing contracts (v4.47.0)
# ──────────────────────────────────────────────────────────────────────

class TestMacOSSigning:
    """Verify that build.sh and CI workflow support conditional signing."""

    def test_build_sh_sign_identity_from_env(self):
        """SIGN_IDENTITY must accept an env-var override (not just hardcoded)."""
        src = _read("build.sh")
        assert "${SIGN_IDENTITY:-" in src, (
            "build.sh must use ${SIGN_IDENTITY:-...} so CI can override the identity"
        )

    def test_build_sh_notarytool_conditional(self):
        """Notarization must only run when APPLE_ID is set."""
        src = _read("build.sh")
        assert "xcrun notarytool" in src, "build.sh must contain notarytool invocation"
        assert "APPLE_ID" in src, "build.sh notarization must check for APPLE_ID"
        assert "APPLE_APP_SPECIFIC_PASSWORD" in src, (
            "build.sh notarization must use APPLE_APP_SPECIFIC_PASSWORD"
        )

    def test_build_sh_notarization_summary_uses_flag(self):
        """Notarization summary must use a flag set after successful notarization, not APPLE_ID presence."""
        src = _read("build.sh")
        assert "NOTARIZED=1" in src, "build.sh must set NOTARIZED=1 after successful notarization"
        assert 'NOTARIZED" = "1"' in src or "NOTARIZED\" = \"1\"" in src, (
            "build.sh summary must check the NOTARIZED flag, not just APPLE_ID presence"
        )

    def test_build_sh_stapler(self):
        """After notarization, the DMG must be stapled."""
        src = _read("build.sh")
        assert "xcrun stapler staple" in src, "build.sh must staple after notarization"

    def test_ci_keychain_import_step(self):
        """CI must have a certificate import step for macOS."""
        src = _read(".github/workflows/ci.yml")
        assert "Import Apple signing certificate" in src
        assert "security create-keychain" in src
        assert "security import" in src
        assert "BUILD_CERTIFICATE_BASE64" in src

    def test_ci_base64_decode_portable(self):
        """Certificate decode must use a portable method (not GNU base64 flags)."""
        src = _read(".github/workflows/ci.yml")
        # Must NOT use GNU-style --decode -o (broken on macOS BSD base64)
        assert "base64 --decode -o" not in src, (
            "CI must not use GNU-style 'base64 --decode -o' — macOS uses BSD base64"
        )

    def test_ci_identity_no_double_prefix(self):
        """SIGN_IDENTITY must not prepend 'Developer ID Application:' on top of sed output."""
        src = _read(".github/workflows/ci.yml")
        # The sed extraction already returns the full identity string including prefix.
        # Wrapping it in "Developer ID Application: $(...)" would double the prefix.
        assert 'SIGN_IDENTITY="Developer ID Application: $(' not in src, (
            "CI must not prepend 'Developer ID Application:' — sed already extracts the full identity"
        )

    def test_ci_identity_empty_check(self):
        """CI must fail explicitly if identity extraction returns empty."""
        src = _read(".github/workflows/ci.yml")
        assert 'if [ -z "$SIGN_IDENTITY" ]' in src, (
            "CI must check for empty SIGN_IDENTITY after extraction"
        )

    def test_ci_signing_conditional(self):
        """macOS build step must be conditional: sign if secrets present, skip otherwise."""
        src = _read(".github/workflows/ci.yml")
        assert "OUROBOROS_SIGN=0" in src, (
            "CI must fall back to unsigned build when secrets are absent"
        )
        assert "Signing certificate detected" in src or "codesign" in src.lower(), (
            "CI must have a signing path when secrets are present"
        )

    def test_ci_uses_env_context_for_condition(self):
        """Certificate import step must use env.* (not secrets.*) in its if condition.

        GitHub Actions does not allow secrets.* in step-level if expressions —
        the workflow file fails to parse with 'Unrecognized named-value: secrets'.
        Signing secrets are mapped at the job level, then env.* is used in step if.
        """
        src = _read(".github/workflows/ci.yml")
        assert "env.BUILD_CERTIFICATE_BASE64" in src, (
            "CI must use env.BUILD_CERTIFICATE_BASE64 in the step if condition"
        )
        # Verify secrets are NOT used directly in if conditions.
        # Parse full if-blocks including multiline continuations (indented lines
        # following an if: that don't start a new YAML key).
        lines = src.splitlines()
        in_if = False
        if_block = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("if:"):
                # Flush previous if-block
                if in_if and if_block:
                    full = " ".join(if_block)
                    assert "secrets." not in full, (
                        f"secrets.* must not appear in step if-conditions: {full[:200]}"
                    )
                in_if = True
                if_block = [stripped]
            elif in_if:
                # Continuation: indented and not a new YAML key
                if stripped and not stripped.endswith(":") and not stripped.startswith("- "):
                    if_block.append(stripped)
                else:
                    full = " ".join(if_block)
                    assert "secrets." not in full, (
                        f"secrets.* must not appear in step if-conditions: {full[:200]}"
                    )
                    in_if = False
                    if_block = []
        # Flush final block
        if in_if and if_block:
            full = " ".join(if_block)
            assert "secrets." not in full, (
                f"secrets.* must not appear in step if-conditions: {full[:200]}"
            )

    def test_ci_import_gates_on_full_secret_set(self):
        """Import step must gate on ALL required signing secrets, not just the certificate."""
        src = _read(".github/workflows/ci.yml")
        # Find the import step's if condition
        import_idx = src.find("Import Apple signing certificate")
        assert import_idx != -1, "Import step not found"
        # The if: line is nearby — check that it gates on all four secrets
        region = src[import_idx:import_idx + 500]
        for env_var in ["env.BUILD_CERTIFICATE_BASE64", "env.P12_PASSWORD",
                       "env.KEYCHAIN_PASSWORD", "env.APPLE_TEAM_ID"]:
            assert env_var in region, (
                f"Import step if-condition must gate on {env_var} to prevent "
                f"partial-secret failures"
            )

    def test_ci_signing_secrets_at_job_level(self):
        """Signing secrets must be in the build job's env block, not step-level.

        Step-level env vars are NOT available to that step's if: condition
        in GitHub Actions. Secrets must be mapped at job level.
        """
        src = _read(".github/workflows/ci.yml")
        # Find the build job section and its env block (before steps:)
        build_idx = src.find("  build:")
        assert build_idx != -1, "build job not found"
        steps_idx = src.find("    steps:", build_idx)
        assert steps_idx != -1, "steps: not found in build job"
        job_header = src[build_idx:steps_idx]
        for secret_name in ["BUILD_CERTIFICATE_BASE64", "P12_PASSWORD",
                            "KEYCHAIN_PASSWORD", "APPLE_TEAM_ID"]:
            assert secret_name in job_header, (
                f"Signing secret {secret_name} must be in build job env block "
                f"(before steps:) so step if: conditions can read env.*"
            )

    def test_ci_build_step_gates_on_full_secret_set(self):
        """Build macOS app signing branch must gate on ALL required secrets."""
        src = _read(".github/workflows/ci.yml")
        build_idx = src.find("Build macOS app")
        assert build_idx != -1, "Build macOS app step not found"
        # The shell if-branch is within ~800 chars after the step name
        region = src[build_idx:build_idx + 800]
        for var in ["BUILD_CERTIFICATE_BASE64", "P12_PASSWORD",
                    "KEYCHAIN_PASSWORD", "APPLE_TEAM_ID"]:
            assert var in region, (
                f"Build macOS app signing branch must check {var} to prevent "
                f"entering the signing path when the keychain was never created"
            )

    def test_ci_keychain_cleanup(self):
        """CI must clean up the temporary keychain unconditionally."""
        src = _read(".github/workflows/ci.yml")
        assert "Cleanup keychain" in src
        assert "security delete-keychain" in src
        assert "always()" in src

    def test_ci_notarization_secrets_passed(self):
        """CI macOS build step must pass notarization env vars."""
        src = _read(".github/workflows/ci.yml")
        assert "APPLE_ID" in src
        assert "APPLE_TEAM_ID" in src
        assert "APPLE_APP_SPECIFIC_PASSWORD" in src
