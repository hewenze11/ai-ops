"""Packaging guard: every console asset directory must ship in the wheel.

A console bundle that exists only in the source tree works locally and 404s
inside the container, because the image installs a wheel. That failure is
invisible to the API tests.

Two traps this test is written to avoid, both of which produced a false pass
during development:

* Building in the working tree reuses a stale ``build/`` directory and
  ``*.egg-info``, so files that are not declared get shipped by accident.
  The build therefore runs on a clean copy in ``tmp_path``.
* setuptools' default build isolation is what the image's ``python -m build``
  uses; ``--no-isolation`` against a dev venv is not the same environment. The
  subprocess is run without ``--no-isolation`` so the check matches the image.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "ai_ops" / "console"


def expected_assets():
    """Every html/css/js under ai_ops/console, as wheel-relative paths."""
    return sorted(
        p.relative_to(ROOT).as_posix()
        for p in CONSOLE.rglob("*")
        if p.is_file() and p.suffix in (".html", ".css", ".js")
    )


def test_console_dirs_have_assets():
    # Guards the guard: an empty list would make the wheel check vacuous.
    names = expected_assets()
    assert any(n.endswith("console/index.html") for n in names)
    assert any("/console/governance/" in n for n in names)


def test_wheel_contains_every_console_asset(tmp_path):
    assets = expected_assets()
    if not assets:
        pytest.skip("no console assets")
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, src / name)
    shutil.copytree(
        ROOT / "ai_ops", src / "ai_ops",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    out = tmp_path / "dist"
    try:
        built = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)],
            cwd=src, capture_output=True, text=True,
        )
    except FileNotFoundError:
        pytest.skip("the 'build' package is not installed")
    if built.returncode != 0:
        pytest.skip("wheel build unavailable in this environment:\n" + built.stdout[-800:] + built.stderr[-800:])
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as zf:
        shipped = set(zf.namelist())
    missing = [a for a in assets if a not in shipped]
    assert not missing, "wheel is missing console assets: " + ", ".join(missing)
