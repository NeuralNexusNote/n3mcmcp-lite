"""§A3 — uv が PATH に無い状態で、Claude Code の MCP launcher が
プラグイン manifest 経由で行う spawn を再現する。

Claude Code (Node) は child_process.spawn(command, args, {shell: false})
で MCP サーバを起動する。command がパス上に解決できない場合、Node は
'error' イベントで ENOENT を返し、/mcp パネルは failed 状態を表示する。

注: Python subprocess は Windows で CreateProcess の親プロセス PATH を
使うため、env= だけでは検索パスを変えられない。本テストは os.environ
自体を書き換えて検索パス・子プロセス環境の両方を統一する。"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time

PY312_SCRIPTS = r"C:\Users\ttake\AppData\Local\Programs\Python\Python312\Scripts"
PY312_BASE    = r"C:\Users\ttake\AppData\Local\Programs\Python\Python312"

# Mutate os.environ so CreateProcess (which reads parent's PATH for search) sees
# the stripped value. Process-local; does not leak.
clean_path_entries = []
for entry in os.environ.get("PATH", "").split(os.pathsep):
    norm = entry.rstrip("\\/").lower()
    if norm == PY312_SCRIPTS.lower() or norm == PY312_BASE.lower():
        continue
    clean_path_entries.append(entry)

# Remove any case-variant of PATH first
for k in list(os.environ.keys()):
    if k.upper() == "PATH":
        del os.environ[k]
os.environ["PATH"] = os.pathsep.join(clean_path_entries)

print(f"PATH entries after stripping: {len(clean_path_entries)}")
resolved = shutil.which("uvx")
print(f"shutil.which('uvx') = {resolved!r}")

command = "uvx"
args = ["--from", "n3memorycore-mcp-lite", "n3mc-workingmemory"]

result = {"command": command, "args": args, "shell": False,
          "stripped_path_resolves_uvx": resolved}

t0 = time.perf_counter()
try:
    proc = subprocess.Popen(
        [command] + args,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False,
    )
except FileNotFoundError as e:
    result["spawn_error_class"] = type(e).__name__
    result["spawn_error_winerror"] = getattr(e, "winerror", None)
    result["spawn_error_errno"] = getattr(e, "errno", None)
    result["spawn_error_strerror"] = str(getattr(e, "strerror", e))
    result["spawn_error_message"] = str(e)
    result["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
    result["mcp_panel_visible_failure"] = True
    result["interpretation"] = (
        "Node child_process.spawn would emit an 'error' event with code='ENOENT'. "
        "Claude Code's /mcp panel surfaces this as 'failed' with the spawn error message."
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)
except OSError as e:
    result["spawn_error_class"] = type(e).__name__
    result["spawn_error_winerror"] = getattr(e, "winerror", None)
    result["spawn_error_errno"] = getattr(e, "errno", None)
    result["spawn_error_message"] = str(e)
    result["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
    result["mcp_panel_visible_failure"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0)

try:
    stdout, stderr = proc.communicate(timeout=8)
    result["spawn_succeeded"] = True
    result["exit_code"] = proc.returncode
    result["stdout"] = stdout.decode("utf-8", errors="replace")[:500]
    result["stderr"] = stderr.decode("utf-8", errors="replace")[:1000]
    result["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
except subprocess.TimeoutExpired:
    proc.kill()
    try:
        stdout, stderr = proc.communicate(timeout=2)
        result["stdout"] = stdout.decode("utf-8", errors="replace")[:500]
        result["stderr"] = stderr.decode("utf-8", errors="replace")[:1000]
    except Exception:
        pass
    result["spawn_succeeded"] = True
    result["exit_code"] = "TIMEOUT_KILLED"
    result["elapsed_seconds"] = round(time.perf_counter() - t0, 3)

print(json.dumps(result, ensure_ascii=False, indent=2))
