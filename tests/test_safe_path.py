"""Safe import path (spec §3.15): files in the current folder never run inside N3MC. No model."""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from n3mc_mcp import paths

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "n3mc_mcp"
SHADOWS = ["argparse", "shlex", "queue", "fastapi", "pydantic", "redis", "numpy", "uvicorn"]


def ends_with_main(p):
    tree = ast.parse(p.read_text(encoding="utf-8"))
    last = tree.body[-1]
    return isinstance(last, ast.If) and ast.unparse(last.test) == "__name__ == '__main__'"


ENTRIES = [p for p in sorted(PKG.glob("*.py")) if ends_with_main(p)]


def child_env(tmp_path):
    env = dict(os.environ)
    env["N3MC_DATA_DIR"] = str(tmp_path / "data")
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "home")
    env.pop("PYTHONPATH", None)
    return env


def test_there_are_entry_modules():
    names = {p.name for p in ENTRIES}
    assert "__main__.py" in names and len(names) >= 3
    assert {"cli.py", "lite_hook.py", "lite_stop_hook.py", "server.py"} <= names


@pytest.mark.parametrize("path", ENTRIES, ids=lambda p: p.name)
def test_entry_module_starts_with_the_safe_path_block(path):
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    assert ast.unparse(body[0]) == "import os as _os"
    assert ast.unparse(body[1]) == "import sys as _sys"
    block = body[2]
    assert isinstance(block, ast.If) and ast.unparse(block.test) == "__name__ == '__main__'"
    assert "del _sys.path[0]" in ast.unparse(block)


@pytest.fixture
def trap_folder(tmp_path):
    d = tmp_path / "trap"
    d.mkdir()
    marker = tmp_path / "ran.txt"
    for name in SHADOWS:
        (d / f"{name}.py").write_text(
            f"open({str(marker)!r}, 'a').write({name!r} + '\\n')\nraise ImportError('trap {name}')\n",
            encoding="utf-8")
    return d, marker


def test_files_in_the_current_folder_do_not_run(trap_folder, tmp_path):
    d, marker = trap_folder
    r = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=str(d), capture_output=True,
                       text=True, env=child_env(tmp_path), timeout=120)
    assert r.returncode == 0 and "usage: n3mc-lite" in r.stdout, r.stderr
    assert not marker.exists()


def test_without_the_block_they_would_run(trap_folder, tmp_path):
    d, marker = trap_folder
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=str(d), capture_output=True,
                   env=child_env(tmp_path), timeout=60)
    assert marker.exists()


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    r = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=str(ROOT), capture_output=True,
                       text=True, env=child_env(tmp_path), timeout=120)
    assert r.returncode == 0, r.stderr


def test_children_are_started_with_dash_P_outside_a_checkout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    expected = [sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable]
    assert paths.python_cmd() == expected
    monkeypatch.chdir(ROOT)
    assert paths.python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for p in PKG.glob("*.py"):
        assert "[sys.executable," not in p.read_text(encoding="utf-8"), p.name
