"""Safe import path (spec 3.15, 2026-11-21): files in the current folder must not run inside N3MC. No model needed."""

import ast
import glob
import os
import subprocess
import sys

import pytest

import n3mc_mcp
from n3mc_mcp import paths

PKG = os.path.dirname(os.path.abspath(n3mc_mcp.__file__))
REPO = os.path.dirname(PKG)
SHADOWS = ["argparse", "shlex", "queue", "fastapi", "pydantic", "redis", "numpy", "uvicorn"]


ENTRY = [os.path.join(PKG, n) for n in ("cli.py", "lite_hook.py", "lite_stop_hook.py", "server.py", "__main__.py")]


def _env(tmp):
    env = dict(os.environ)
    env["N3MC_DATA_DIR"] = os.path.join(tmp, "data")
    env["HOME"] = env["USERPROFILE"] = os.path.join(tmp, "home")
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)
    return env


def test_there_are_entry_modules():
    mains = []
    for fp in glob.glob(os.path.join(PKG, "*.py")):
        last = ast.parse(open(fp, encoding="utf-8").read()).body[-1]
        if isinstance(last, ast.If) and "__main__" in ast.dump(last.test):
            mains.append(fp)
    assert os.path.join(PKG, "cli.py") in mains
    assert len(mains) >= 3
    assert set(ENTRY) <= set(mains)


@pytest.mark.parametrize("fp", ENTRY)
def test_entry_module_starts_with_the_safe_path_block(fp):
    tree = ast.parse(open(fp, encoding="utf-8").read())
    body = tree.body
    start = 1 if isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) else 0
    assert isinstance(body[start], ast.Import) and body[start].names[0].asname == "_os"
    assert isinstance(body[start + 1], ast.Import) and body[start + 1].names[0].asname == "_sys"
    blk = body[start + 2]
    assert isinstance(blk, ast.If)
    assert "__main__" in ast.dump(blk.test)
    assert "del _sys.path[0]" in ast.unparse(blk)


def _write_shadows(folder):
    os.makedirs(folder, exist_ok=True)
    marker = os.path.join(folder, "MARK.txt")
    for name in SHADOWS:
        with open(os.path.join(folder, name + ".py"), "w", encoding="utf-8") as f:
            f.write("open(%r, 'a').write(%r)\nraise ImportError('planted %s')\n" % (marker, name + "\n", name))
    return marker


def test_files_in_the_current_folder_do_not_run(tmp_path):
    work = str(tmp_path / "work")
    marker = _write_shadows(work)
    r = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=work, env=_env(str(tmp_path)),
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "--search" in r.stdout
    assert not os.path.exists(marker)


def test_without_the_block_they_would_run(tmp_path):
    work = str(tmp_path / "work")
    marker = _write_shadows(work)
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=work, env=_env(str(tmp_path)),
                   capture_output=True)
    assert os.path.exists(marker)


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    r = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=REPO, env=_env(str(tmp_path)),
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expected = [sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable]
    assert paths.python_cmd() == expected
    monkeypatch.chdir(REPO)
    assert paths.python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for fp in glob.glob(os.path.join(PKG, "*.py")):
        assert "[sys.executable," not in open(fp, encoding="utf-8").read(), fp
