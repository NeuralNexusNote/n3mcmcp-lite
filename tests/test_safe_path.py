"""Safe import path: files in the current folder do not run inside N3MC (spec §3.15, §9). No model."""
import ast
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "n3mc_mcp"


def _entry_modules():
    out = []
    for p in sorted(PKG.glob("*.py")):
        src = p.read_text(encoding="utf-8")
        if src.rstrip().splitlines()[-2:] and 'if __name__ == "__main__":' in "\n".join(src.rstrip().splitlines()[-3:]):
            out.append(p)
    return out


def test_there_are_entry_modules():
    names = [p.name for p in _entry_modules()]
    assert "__main__.py" in names
    assert len(names) >= 3


@pytest.mark.parametrize("path", _entry_modules(), ids=lambda p: p.name)
def test_entry_module_starts_with_the_safe_path_block(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    assert isinstance(body[0], ast.Import) and body[0].names[0].name == "os" and body[0].names[0].asname == "_os"
    assert isinstance(body[1], ast.Import) and body[1].names[0].name == "sys" and body[1].names[0].asname == "_sys"
    block = body[2]
    assert isinstance(block, ast.If)
    src = ast.unparse(block)
    assert "__name__ == '__main__'" in src and "del _sys.path[0]" in src


TRAPS = ("argparse", "shlex", "queue", "fastapi", "pydantic", "redis", "numpy", "uvicorn")


def _plant(folder: pathlib.Path):
    marker = folder / "TRAP_RAN"
    for name in TRAPS:
        (folder / f"{name}.py").write_text(
            f"open(r'{marker}', 'w').write('{name}')\nraise ImportError('trap {name}')\n", encoding="utf-8")
    return marker


def _env(tmp_path):
    env = dict(os.environ)
    env["N3MC_DATA_DIR"] = str(tmp_path / "d")
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "h")
    env.pop("PYTHONPATH", None)
    return env


def test_files_in_the_current_folder_do_not_run(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    marker = _plant(work)
    cp = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=work, env=_env(tmp_path),
                        capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode(errors="replace")
    assert b"usage" in cp.stdout.lower()
    assert not marker.exists()


def test_without_the_block_they_would_run(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    marker = _plant(work)
    subprocess.run([sys.executable, "-c", "import shlex"], cwd=work, env=_env(tmp_path), capture_output=True,
                   timeout=60)
    assert marker.exists()


def test_a_source_checkout_run_in_place_still_works(tmp_path):
    cp = subprocess.run([sys.executable, "-m", "n3mc_mcp.cli", "--help"], cwd=REPO, env=_env(tmp_path),
                        capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode(errors="replace")


def test_children_are_started_with_dash_P_outside_a_checkout(tmp_path, monkeypatch):
    from n3mc_mcp.paths import python_cmd
    monkeypatch.chdir(tmp_path)
    if sys.version_info >= (3, 11):
        assert python_cmd() == [sys.executable, "-P"]
    else:
        assert python_cmd() == [sys.executable]
    monkeypatch.chdir(REPO)
    assert python_cmd() == [sys.executable]


def test_no_child_is_started_without_python_cmd():
    for p in PKG.glob("*.py"):
        assert "[sys.executable," not in p.read_text(encoding="utf-8"), p.name
