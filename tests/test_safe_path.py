"""Safe import path (spec §3.15): files in the current folder do not run inside N3MC. No model."""
import ast
import glob
import os
import subprocess
import sys

import pytest

import conftest
from n3mc_mcp import paths

PKG = os.path.join(conftest.REPO_ROOT, "n3mc_mcp")
SHADOWS = ["argparse", "shlex", "queue", "fastapi", "pydantic", "redis", "numpy", "uvicorn"]


def _entry_modules():
    """The package modules whose last top-level statement is `if __name__ == "__main__":`."""
    out = []
    for p in sorted(glob.glob(os.path.join(PKG, "*.py"))):
        body = ast.parse(open(p, encoding="utf-8").read()).body
        if body and isinstance(body[-1], ast.If) and ast.unparse(body[-1].test) == "__name__ == '__main__'":
            out.append(p)
    return out


ENTRIES = _entry_modules()


def test_there_are_entry_modules():
    names = {os.path.basename(p) for p in ENTRIES}
    assert "cli.py" in names and len(names) >= 3
    assert names >= {"cli.py", "lite_hook.py", "lite_stop_hook.py", "server.py", "__main__.py"}


@pytest.mark.parametrize("path", ENTRIES, ids=[os.path.basename(p) for p in ENTRIES])
def test_entry_module_starts_with_the_safe_path_block(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    body = tree.body
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)  # docstring
    first = [ast.unparse(n) for n in body[1:4]]
    assert first[0] == "import os as _os" and first[1] == "import sys as _sys"
    assert first[2].startswith("if __name__ == '__main__':") and "del _sys.path[0]" in first[2]


def _shadow_folder(tmp_path):
    marker = tmp_path / "MARKER"
    for name in SHADOWS:
        (tmp_path / f"{name}.py").write_text(
            f"open({str(marker)!r}, 'w').write('{name}')\nraise ImportError('shadowed {name}')\n", encoding="utf-8")
    return marker


def test_files_in_the_current_folder_do_not_run(tmp_path):
    marker = _shadow_folder(tmp_path)
    p = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=str(tmp_path), capture_output=True,
                       text=True, timeout=120)
    assert p.returncode == 0 and "usage" in p.stdout.lower() and not marker.exists()


def test_without_the_block_they_would_run(tmp_path):
    marker = _shadow_folder(tmp_path)
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=str(tmp_path), capture_output=True, timeout=60)
    assert marker.exists()


def test_a_source_checkout_run_in_place_still_works():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    p = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=conftest.REPO_ROOT, env=env,
                       capture_output=True, timeout=120)
    assert p.returncode == 0


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    want = [sys.executable, "-P"] if sys.version_info >= (3, 11) else [sys.executable]
    assert paths.python_cmd() == want
    monkeypatch.chdir(conftest.REPO_ROOT)
    assert paths.python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for p in glob.glob(os.path.join(PKG, "*.py")):
        assert "[sys.executable," not in open(p, encoding="utf-8").read(), p
