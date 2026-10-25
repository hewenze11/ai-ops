"""Every static bundle the app serves must also survive being packaged.

The failure this guards against is invisible from a source checkout: tests import
``ai_ops`` from the working tree, where every file exists by definition, so a
missing ``package-data`` entry passes the entire suite and only shows up as a 503
on a real install. It happened once with ``ai_ops/console/governance/``.

So instead of asking the app "can you read this file", ask the packaging
metadata "will this file be in the wheel". That is the question that actually
breaks.
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each directory served over HTTP, and the extensions that must ship with it.
SERVED = {
    "ai_ops/console": {".html", ".css", ".js"},
    "ai_ops/console/governance": {".html", ".css", ".js"},
}


def _package_data():
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["setuptools"]["package-data"]


def test_every_served_directory_is_declared_for_packaging():
    declared = _package_data()
    for directory, suffixes in SERVED.items():
        package = directory.replace("/", ".")
        found = {p.suffix for p in (ROOT / directory).iterdir() if p.is_file()}
        expected = found & suffixes
        assert expected, f"{directory} serves nothing; update SERVED in this test"

        patterns = declared.get(package, [])
        for suffix in sorted(expected):
            assert f"*{suffix}" in patterns, (
                f"{directory}/*{suffix} is served but {package} does not declare "
                f"it in [tool.setuptools.package-data]; the file will be missing "
                f"from the wheel and the route will 503 on a real install"
            )


def test_governance_bundle_is_declared_distinctly_from_the_workbench():
    # A wildcard on the parent package does not reach subdirectories in
    # setuptools; the two must be listed separately or the deeper bundle is
    # silently dropped.
    declared = _package_data()
    assert "ai_ops.console" in declared
    assert "ai_ops.console.governance" in declared
