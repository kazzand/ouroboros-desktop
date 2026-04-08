"""Regression checks for packaging asset completeness.

These tests read files that exist only in the app bundle (launcher.py,
Ouroboros.spec) and are skipped when running from a bare repo checkout.
"""

import os
import pathlib

import pytest

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BUNDLE_FILES_PRESENT = (REPO / "Ouroboros.spec").exists() and (REPO / "launcher.py").exists()
_SKIP_REASON = "Bundle-only files (Ouroboros.spec, launcher.py) not present in repo"

def _launcher_has_bootstrap() -> bool:
    launcher = REPO / "launcher.py"
    bootstrap = REPO / "ouroboros" / "launcher_bootstrap.py"
    if not launcher.exists() or not bootstrap.exists():
        return False
    launcher_src = launcher.read_text(encoding="utf-8")
    bootstrap_src = bootstrap.read_text(encoding="utf-8")
    return (
        "from ouroboros.launcher_bootstrap import" in launcher_src
        and "MANAGED_BUNDLE_PATHS = (" in bootstrap_src
        and '"server.py"' in bootstrap_src
        and '"web"' in bootstrap_src
        and '"webview"' in bootstrap_src
        and '"assets"' in bootstrap_src
    )

_LAUNCHER_HAS_BOOTSTRAP = _launcher_has_bootstrap()


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_spec_bundles_assets_and_icon():
    source = _read("Ouroboros.spec")
    assert "('assets', 'assets')" in source
    assert "icon='assets/icon.icns'" in source


@pytest.mark.skipif(
    not _LAUNCHER_HAS_BOOTSTRAP,
    reason="launcher.py does not import launcher_bootstrap (may be a newer version without bootstrap bridge)",
)
def test_launcher_does_not_exclude_assets_on_bootstrap():
    launcher_source = _read("launcher.py")
    bootstrap_source = _read("ouroboros/launcher_bootstrap.py")
    assert '"python-standalone", "assets"' not in launcher_source
    assert "from ouroboros.launcher_bootstrap import" in launcher_source
    assert "MANAGED_BUNDLE_PATHS = (" in bootstrap_source
    assert '"server.py"' in bootstrap_source
    assert '"web"' in bootstrap_source
    assert '"webview"' in bootstrap_source
    assert '"assets"' in bootstrap_source
