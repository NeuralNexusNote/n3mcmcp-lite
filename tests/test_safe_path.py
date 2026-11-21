"""Safe import path: a Python file in the folder the user has open must not run inside N3MC.

`python -m <package>.<module>` puts the current folder first on sys.path. Every entry module drops
that entry before it imports anything else, and children are started with -P where Python has it.
"""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

PKG = "n3mc_mcp"
MAIN_FILE = "cli.py"
MAIN_MODULE = f"{PKG}.{MAIN_FILE[:-3]}"
HOME_ENV = "N3MC_DATA_DIR"
REPO = pathlib.Path(__file__).resolve().parent.parent
PKG_DIR = REPO / PKG
# modules the main module imports at its top: the standard library and the dependencies
SHADOWS = ("argparse", "shlex", "queue", "fastapi", "pydantic", "redis", "numpy", "uvicorn")


def entry_modules():
    return sorted(p for p in PKG_DIR.glob("*.py")
                  if any(isinstance(n, ast.If) and ast.unparse(n.test) == "__name__ == '__main__'"
                         for n in ast.parse(p.read_text(encoding="utf-8")).body))


def test_there_are_entry_modules():
    assert PKG_DIR / MAIN_FILE in entry_modules() and len(entry_modules()) >= 3


@pytest.mark.parametrize("path", entry_modules(), ids=lambda p: p.name)
def test_entry_module_starts_with_the_safe_path_block(path):
    """Nothing is imported before the block: its two imports are of modules Python has already loaded."""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]                                   # the docstring
    if isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__":
        body = body[1:]
    assert ast.unparse(body[0]) == "import os as _os"
    assert ast.unparse(body[1]) == "import sys as _sys"
    assert isinstance(body[2], ast.If) and ast.unparse(body[2].test) == "__name__ == '__main__'"
    assert "del _sys.path[0]" in ast.unparse(body[2])


def _hostile_folder(tmp_path):
    """A folder as a cloned repository could be: files named like modules N3MC imports."""
    folder = tmp_path / "cloned-repo"
    folder.mkdir()
    marker = tmp_path / "MARKER.txt"
    for name in SHADOWS:
        (folder / f"{name}.py").write_text(
            "import os\n"
            f"open(os.environ['SHADOW_MARKER'], 'a').write('{name} ')\n"
            f"raise ImportError('the project folder shadowed {name}')\n", encoding="utf-8")
    return folder, marker


def _env(tmp_path, marker):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, SHADOW_MARKER=str(marker), HOME=str(home), USERPROFILE=str(home),
               PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    env[HOME_ENV] = str(tmp_path / "data")
    return env


def test_files_in_the_current_folder_do_not_run(tmp_path):
    folder, marker = _hostile_folder(tmp_path)
    p = subprocess.run([sys.executable, "-m", MAIN_MODULE, "--help"], cwd=folder, env=_env(tmp_path, marker),
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert not marker.exists(), f"ran from the project folder: {marker.read_text()}"
    assert p.returncode == 0 and "usage:" in p.stdout, p.stderr[-400:]


def test_without_the_block_they_would_run(tmp_path):
    """The same folder, entered through `python -c` (no block runs): shows the test above is real."""
    folder, marker = _hostile_folder(tmp_path)
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=folder, env=_env(tmp_path, marker),
                   capture_output=True, timeout=120)
    assert marker.exists() and "shlex" in marker.read_text()


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    """The package itself is in the current folder: that entry is kept, or the package would be lost."""
    env = _env(tmp_path, tmp_path / "MARKER.txt")
    env.pop("PYTHONPATH")
    p = subprocess.run([sys.executable, "-m", MAIN_MODULE, "--help"], cwd=REPO, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0 and "usage:" in p.stdout, p.stderr[-400:]


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    from importlib import import_module
    paths = import_module(f"{PKG}.paths")
    monkeypatch.chdir(tmp_path)
    assert paths.python_cmd() == ([sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable])
    monkeypatch.chdir(REPO)
    assert paths.python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for p in PKG_DIR.glob("*.py"):
        assert "[sys.executable," not in p.read_text(encoding="utf-8"), p.name
