"""Layer 10: --init, plugin files, rules file, coexistence with Free / Pro (spec §9 Layer 10). Temporary HOME only."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from n3mc_mcp import cli, paths

REPO = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "n3mc-workingmemory"


def _settings(isolate):
    return isolate["home"] / ".claude" / "settings.json"


def _read(isolate):
    return json.loads(_settings(isolate).read_text(encoding="utf-8"))


def _hook_cmds(s, event):
    return [h.get("command") for e in s.get("hooks", {}).get(event, []) for h in e.get("hooks", [])]


class TestInit:
    def test_writes_hooks_and_rules(self, isolate, capsys):
        assert cli.main(["--init"]) == 0
        s = _read(isolate)
        up, stop = _hook_cmds(s, "UserPromptSubmit"), _hook_cmds(s, "Stop")
        assert len(up) == 1 and cli.is_own_hook(up[0], "UserPromptSubmit") and "\\" not in up[0]
        assert len(stop) == 1 and cli.is_own_hook(stop[0], "Stop")
        assert all(h["timeout"] == 60 for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"])
        for r in cli.permission_rules():
            assert r in s["permissions"]["allow"]
        assert not any("--delete" in r for r in s["permissions"]["allow"])
        assert "N3MC Lite hooks registered in" in capsys.readouterr().out

    def test_shell_path(self):
        assert cli.shell_path("C:/Python312/python.exe") == "C:/Python312/python.exe"
        assert cli.shell_path("C:/PROGRA~1/x.exe") == "C:/PROGRA~1/x.exe"
        if os.name != "nt":
            assert cli.shell_path("/a b/c") == '"/a b/c"'
            assert cli.shell_path("/a$b/c") == '"/a\\$b/c"'

    def test_obrien(self, tmp_path, monkeypatch):
        folder = tmp_path / "O'Brien" / "Scripts"
        folder.mkdir(parents=True)
        exe = folder / ("n3mc-lite-hook.exe" if os.name == "nt" else "n3mc-lite-hook")
        exe.write_text("")
        monkeypatch.setattr(cli.sysconfig, "get_path", lambda *a, **k: str(folder))
        cmd = cli.resolve_cmd("n3mc-lite-hook")
        if cmd.startswith('"'):
            assert "O'Brien" in cmd
        assert cli.is_own_hook(cmd, "UserPromptSubmit")

    def test_second_run_writes_nothing(self, isolate):
        cli.main(["--init"])
        before = _settings(isolate).read_bytes()
        baks = list(_settings(isolate).parent.glob("settings.json.bak-n3mc-lite-*"))
        cli.main(["--init"])
        assert _settings(isolate).read_bytes() == before
        assert list(_settings(isolate).parent.glob("settings.json.bak-n3mc-lite-*")) == baks

    def test_backup_before_change(self, isolate):
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text("{}", encoding="utf-8")
        cli.main(["--init"])
        assert list(_settings(isolate).parent.glob("settings.json.bak-n3mc-lite-*"))

    def test_invalid_settings(self, isolate):
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text("{oops", encoding="utf-8")
        assert cli.main(["--init"]) == 1
        assert _settings(isolate).read_text(encoding="utf-8") == "{oops"

    @pytest.mark.parametrize("old", ["C:\\old\\Scripts\\n3mc-lite-hook.exe", "python -m n3mc_mcp.lite_hook",
                                     "/usr/bin/n3mc-lite-hook"])
    def test_replaces_own_forms(self, isolate, old):
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": old}]}]}}), encoding="utf-8")
        cli.main(["--init"])
        cmds = _hook_cmds(_read(isolate), "UserPromptSubmit")
        assert len(cmds) == 1 and cmds[0] != old

    def test_own_script_path(self, isolate):
        script = os.path.join(cli.PKG_DIR, "lite_hook.py")
        assert cli.is_own_hook(f"python {script}", "UserPromptSubmit")
        assert not cli.is_own_hook("python /elsewhere/lite_hook.py", "UserPromptSubmit")


class TestInitNonStringCommand:
    def test_null_command(self, isolate):
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": None}, {"type": "command", "command": 3}]}]}}), encoding="utf-8")
        assert cli.main(["--init"]) == 0
        cmds = _hook_cmds(_read(isolate), "Stop")
        assert None in cmds and 3 in cmds and any(isinstance(c, str) and cli.is_own_hook(c, "Stop") for c in cmds)


class TestKeepsOtherProducts:
    def test_free_and_pro_kept(self, isolate, capsys):
        others = ["C:/Py/Scripts/n3mc-hook.exe", "python -m n3memorycore.n3mc_hook",
                  'python "C:/x/n3memorycore/n3mc_hook.py"', "n3mc-pro-hook", "echo hi"]
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": c}]} for c in others]}}), encoding="utf-8")
        cli.main(["--init"])
        cmds = _hook_cmds(_read(isolate), "UserPromptSubmit")
        for c in others:
            assert c in cmds
        out = capsys.readouterr().out
        assert out.count("Another N3MC hook is registered for UserPromptSubmit") == 4

    @pytest.mark.parametrize("cmd", ["n3mc-hook", "n3mc-stop-hook.exe", "python -m n3memorycore.n3mc_hook",
                                     "python -m n3memorycore.n3mc_stop_hook", "python /p/n3memorycore/n3mc_hook.py"])
    def test_free_not_matched(self, cmd):
        assert not cli.is_own_hook(cmd, "UserPromptSubmit") and not cli.is_own_hook(cmd, "Stop")


class TestPluginEnabled:
    def test_init_adds_no_hooks(self, isolate, capsys):
        _settings(isolate).parent.mkdir(parents=True)
        _settings(isolate).write_text(json.dumps({"enabledPlugins": {"n3mc-workingmemory@neuralnexusnote": True}}),
                                      encoding="utf-8")
        cli.main(["--init"])
        s = _read(isolate)
        assert not _hook_cmds(s, "UserPromptSubmit") and s["permissions"]["allow"]
        assert "The n3mc-workingmemory plugin is enabled" in capsys.readouterr().out

    def test_plugin_reports_init_hooks(self, isolate):
        cli.main(["--init"])
        out = _run_plugin(isolate)
        assert "registered twice" in json.loads(out)["systemMessage"]


def _run_plugin(isolate, path_env=None):
    env = dict(os.environ)
    env["HOME"] = env["USERPROFILE"] = str(isolate["home"])
    if path_env is not None:
        env["PATH"] = path_env
    cp = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "install_permissions.py")], capture_output=True,
                        env=env, timeout=60)
    assert cp.returncode == 0
    return cp.stdout.decode("ascii")


class TestPluginHook:
    def test_rules_written(self, isolate):
        _run_plugin(isolate)
        allow = _read(isolate)["permissions"]["allow"]
        assert "Bash(uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --search *)" in allow
        assert not any("--delete" in r for r in allow)
        assert not any(r.startswith("mcp__") for r in allow)

    def test_atomic_replace_and_reread(self):
        src = (PLUGIN / "hooks" / "install_permissions.py").read_text(encoding="utf-8")
        assert "os.replace" in src and "read_settings(path)" in src

    def test_fallback_text(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert "or the permission script failed" in cmd
        assert cmd.startswith('python "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || py ')

    def test_uvx_missing(self, isolate, tmp_path):
        out = _run_plugin(isolate, path_env=str(tmp_path))
        msg = json.loads(out)["systemMessage"]
        assert "uvx was not found on PATH" in msg and "uv を入れるまで" in msg


class TestPluginHookAsciiStdout:
    def test_ascii_one_line(self, isolate, tmp_path):
        out = _run_plugin(isolate, path_env=str(tmp_path))
        assert out.count("\n") == 1 and out.isascii()
        assert "記憶のフック" in json.loads(out)["systemMessage"]


class TestNoMcpInClaudeCode:
    def test_plugin_files(self):
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert "mcpServers" not in plugin and plugin["version"] == "2.0.2"
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
        assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == \
            "uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite-hook --uvx"
        assert hooks["Stop"][0]["hooks"][0]["command"] == "uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite-stop-hook --uvx"

    def test_init_adds_no_mcp(self, isolate, tmp_path, monkeypatch, capsys):
        cj = isolate["home"] / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {}},
                                  "projects": {"/p": {"mcpServers": {"n3mc-workingmemory": {}}}}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {}}}), encoding="utf-8")
        before = cj.read_bytes(), (tmp_path / ".mcp.json").read_bytes()
        cli.main(["--init"])
        assert (cj.read_bytes(), (tmp_path / ".mcp.json").read_bytes()) == before
        out = capsys.readouterr().out
        assert "claude mcp remove n3mc-workingmemory -s user" in out
        assert "-s local (run it in that project)" in out
        assert 'remove the "n3mc-workingmemory" entry from' in out


class TestSeparateNames:
    def test_names(self, cfg):
        from n3mc_mcp.config import DEFAULTS
        assert DEFAULTS["server_port"] == 18524 and paths.DATA_DIR_NAME == ".n3mc-workingmemory"
        assert paths.PID_NAME == "n3mc-lite.pid"
        assert paths.DATA_DIR_NAME != ".n3mc" and DEFAULTS["server_port"] not in (18520, 18521)


class TestRulesFile:
    def test_pip_form(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        text = (tmp_path / ".claude" / "rules" / "n3mc-lite-behavior.md").read_text(encoding="utf-8")
        assert text.splitlines()[0] == "<!-- n3mc-lite-behavior v1 -->"
        assert f"{cli.shell_path(sys.executable)} -m n3mc_mcp.cli --search" in text and text.endswith("audit.log`.\n")

    def test_uvx_form_replaces_other(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        assert cli._ensure_rules_md(str(tmp_path), uvx=True) == "replaced"
        p = tmp_path / ".claude" / "rules" / "n3mc-lite-behavior.md"
        assert "uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --search" in p.read_text(encoding="utf-8")
        assert list(p.parent.glob("n3mc-lite-behavior.md.bak-n3mc-lite-*"))
        assert cli._ensure_rules_md(str(tmp_path), uvx=True) == "same"

    def test_hand_edited(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        p = tmp_path / ".claude" / "rules" / "n3mc-lite-behavior.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nmy own note\n", encoding="utf-8")
        assert cli._ensure_rules_md(str(tmp_path)) == "edited"
        assert "my own note" in p.read_text(encoding="utf-8")
        n = json.load(open(paths.notices_path(), encoding="utf-8"))
        assert any(k.startswith("rules_md_edited:") and len(k.split(":")[1]) == 12 for k in n)
