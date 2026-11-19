"""
Packaging smoke tests.

They check that the web assets are resolved relative to the package (never the
current folder) and that a built wheel really contains templates/, static/ and
fonts/ plus the `trimly` command. The wheel test is skipped if hatchling is not
installed, so a plain source checkout can still run the rest.
"""
import importlib.util
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from app import paths

ROOT = Path(__file__).resolve().parent.parent

HAS_HATCHLING = importlib.util.find_spec("hatchling") is not None


def test_runtime_assets_exist_without_using_the_current_folder(tmp_path, monkeypatch):
    """Templates, static and fonts must be found even from another directory."""
    monkeypatch.chdir(tmp_path)

    assert (paths.TEMPLATES_DIR / "index.html").is_file()
    assert (paths.TEMPLATES_DIR / "editor.html").is_file()
    assert (paths.STATIC_DIR / "style.css").is_file()
    assert any(paths.FONTS_DIR.glob("*.ttf"))
    assert (paths.FONTS_DIR / "licenses").is_dir()


def test_writable_files_are_never_inside_the_package():
    """Installed or source mode: no jobs, logs or config inside app/."""
    for path in (paths.DATA_DIR, paths.JOBS_DIR, paths.LOGS_DIR, paths.CONFIG_FILE, paths.LOG_FILE):
        assert paths.PACKAGE_DIR not in path.parents


def test_pyproject_declares_command_and_bundled_assets():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "trimly"
    assert data["project"]["requires-python"] == ">=3.11,<3.14"
    assert data["project"]["scripts"]["trimly"] == "app.cli:main"

    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["templates"] == "app/templates"
    assert force_include["static"] == "app/static"
    assert force_include["fonts"] == "app/fonts"


def test_cli_version(capsys):
    from app import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])

    assert exit_info.value.code == 0
    assert "trimly 0.1.0" in capsys.readouterr().out


@pytest.mark.skipif(not HAS_HATCHLING, reason="hatchling is needed to build a wheel")
def test_wheel_contains_assets_and_works_from_another_folder(tmp_path):
    """Build a real wheel, unpack it and import it like an installed package."""
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            # The test runner's Python may be newer than the package supports;
            # this check is about assets, not about picking an interpreter.
            "--ignore-requires-python",
            "-w",
            str(wheel_dir),
            str(ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    wheel = next(wheel_dir.glob("trimly-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "app/templates/index.html" in names
        assert "app/templates/editor.html" in names
        assert "app/static/style.css" in names
        assert any(name.startswith("app/fonts/") and name.endswith(".ttf") for name in names)
        assert any(name.startswith("app/fonts/licenses/") for name in names)

        entry_points = archive.read("trimly-0.1.0.dist-info/entry_points.txt").decode("utf-8")
        assert "trimly = app.cli:main" in entry_points

        site = tmp_path / "site"
        archive.extractall(site)

    # Import the unpacked copy from a folder that has no app/ checkout nearby.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site)
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.paths as p;"
            "assert p.IS_INSTALLED;"
            "assert (p.TEMPLATES_DIR / 'index.html').is_file();"
            "assert (p.STATIC_DIR / 'style.css').is_file();"
            "assert list(p.FONTS_DIR.glob('*.ttf'));"
            "assert p.PACKAGE_DIR not in p.JOBS_DIR.parents;"
            "print(p.JOBS_DIR)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Trimly" in result.stdout
