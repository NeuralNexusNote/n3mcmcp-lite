"""Layer 10: --init, the plugin files, coexistence with Free / Pro (spec §8, §12.8, §12.9, §14).
Temporary HOME only."""
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

from n3mc_mcp import __version__, cli, config, paths

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "n3mc-workingmemory"


def settings_path():
    return Path(os.path.expanduser("~")) / ".claude" / "settings.json"


def write_settings(obj):
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return p


def read_settings():
    return json.loads(settings_path().read_text(encoding="utf-8"))


def commands(s, event):
    return [h.get("command") for e in s.get("hooks", {}).get(event, []) for h in e.get("hooks", [])]


def init(capsys):
    code = cli.main(["--init"])
    return code, capsys.readouterr().out


class TestInit:
    def test_registers_two_hooks_and_rules(self, capsys):
        code, out = init(capsys)
        s = read_settings()
        assert code == 0 and "N3MC Lite hooks registered in" in out
        for event, exe in (("UserPromptSubmit", "n3mc-lite-hook"), ("Stop", "n3mc-lite-stop-hook")):
            cmds = commands(s, event)
            assert len(cmds) == 1 and "\\" not in cmds[0] and cli.is_own_hook(cmds[0], event)
            assert s["hooks"][event][0]["hooks"][0]["timeout"] == 60
        assert all(r in s["permissions"]["allow"] for r in cli.PERMISSION_RULES)
        assert any(r.endswith("-m n3mc_mcp.cli --search *)") for r in s["permissions"]["allow"])
        assert not any("--delete" in r for r in cli.PERMISSION_RULES)

    def test_shell_path(self, tmp_path):
        assert cli.shell_path("C:/Python312/python.exe") == "C:/Python312/python.exe"
        assert cli.shell_path("C:/PROGRA~1/Python/python.exe") == "C:/PROGRA~1/Python/python.exe"
        assert cli.shell_path(str(tmp_path / "no such" / "py.exe")).startswith('"')
        assert cli.shell_path("/opt/a$b`c/py") == '"/opt/a\\$b\\`c/py"'
        assert cli.shell_path("/opt/O'Brien&x/py") == '"/opt/O\'Brien&x/py"'

    def test_obrien_scripts_folder(self, capsys, monkeypatch, tmp_path):
        scripts = tmp_path / "O'Brien" / "Scripts"
        scripts.mkdir(parents=True)
        for name in ("n3mc-lite-hook.exe", "n3mc-lite-stop-hook.exe"):
            (scripts / name).write_bytes(b"")
        monkeypatch.setattr(cli.sysconfig, "get_path", lambda *a, **k: str(scripts))
        init(capsys)
        cmd = commands(read_settings(), "UserPromptSubmit")[0]
        assert cli.is_own_hook(cmd, "UserPromptSubmit")
        assert cmd.startswith('"') or "'" not in cmd
        mtime = settings_path().stat().st_mtime_ns
        init(capsys)
        assert settings_path().stat().st_mtime_ns == mtime

    def test_second_run_writes_nothing(self, capsys):
        init(capsys)
        mtime = settings_path().stat().st_mtime_ns
        init(capsys)
        assert settings_path().stat().st_mtime_ns == mtime
        assert not list(settings_path().parent.glob("settings.json.bak-n3mc-lite-*"))

    def test_backup_when_changing(self, capsys):
        write_settings({"theme": "dark"})
        init(capsys)
        assert list(settings_path().parent.glob("settings.json.bak-n3mc-lite-*"))
        assert read_settings()["theme"] == "dark"

    @pytest.mark.parametrize("text", ["{broken", "[1, 2]", '{"hooks": []}', '{"permissions": {"allow": "x"}}'])
    def test_bad_settings_unchanged(self, capsys, text):
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        code, _out = init(capsys)
        assert code == 1 and p.read_text(encoding="utf-8") == text

    def test_own_forms_replaced(self, capsys):
        write_settings({"hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "C:\\Python312\\Scripts\\n3mc-lite-hook.exe"}]},
                {"hooks": [{"type": "command", "command": "python -m n3mc_mcp.lite_hook"}]},
                {"hooks": [{"type": "command",
                            "command": f"python \"{Path(cli.PKG_DIR) / 'lite_hook.py'}\""}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/n3mc-lite-stop-hook --uvx"}]}]}})
        init(capsys)
        s = read_settings()
        assert len(commands(s, "UserPromptSubmit")) == 1 and len(commands(s, "Stop")) == 1


class TestInitNonStringCommand:
    def test_null_command_kept(self, capsys):
        write_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": None},
                                                                  {"type": "command", "command": 5}]}]}})
        code, _ = init(capsys)
        cmds = commands(read_settings(), "UserPromptSubmit")
        assert code == 0 and None in cmds and 5 in cmds and len(cmds) == 3


FREE_HOOKS = ["C:/Py/Scripts/n3mc-hook.exe", "C:/Py/Scripts/n3mc-stop-hook.exe",
              'python "C:/x/n3memorycore_pro/n3mc_hook.py"', "echo other"]


def free_is_own(command, event):
    """N3MC-Free's own matcher, as described in spec §9 Layer 10."""
    names = {"UserPromptSubmit": ("n3mc-hook", "n3memorycore.n3mc_hook", "n3mc_hook.py"),
             "Stop": ("n3mc-stop-hook", "n3memorycore.n3mc_stop_hook", "n3mc_stop_hook.py")}[event]
    toks = shlex.split(command.replace("\\", "/"))
    first = os.path.basename(toks[0]).lower()
    first = first[:-4] if first.endswith(".exe") else first
    if first == names[0]:
        return True
    if any(t == "-m" and toks[i + 1] == names[1] for i, t in enumerate(toks[:-1])):
        return True
    return any(os.path.basename(t) == names[2] and "n3memorycore/" in t for t in toks[1:])


class TestKeepsOtherProducts:
    def test_other_hooks_kept_and_reported(self, capsys):
        write_settings({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": c}]}
                                                       for c in FREE_HOOKS],
                                  "Stop": [{"hooks": [{"type": "command", "command": FREE_HOOKS[1]}]}]}})
        _c, out = init(capsys)
        cmds = commands(read_settings(), "UserPromptSubmit")
        assert all(c in cmds for c in FREE_HOOKS) and len(cmds) == 5
        assert "Another N3MC hook is registered for UserPromptSubmit and was left in place: C:/Py/Scripts/n3mc-hook.exe" in out
        assert "echo other" not in out

    def test_free_matcher_ignores_lite(self, capsys):
        init(capsys)
        s = read_settings()
        for event in ("UserPromptSubmit", "Stop"):
            for c in commands(s, event):
                assert not free_is_own(c, event)
        assert not cli.is_own_hook("C:/Py/Scripts/n3mc-hook.exe", "UserPromptSubmit")


class TestPluginEnabled:
    def test_no_hooks_rules_added(self, capsys):
        write_settings({"enabledPlugins": {cli.PLUGIN_KEY: True}, "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "n3mc-lite-hook"}]}]}})
        code, out = init(capsys)
        s = read_settings()
        assert code == 0 and "plugin is enabled and registers the N3MC Lite hooks" in out
        assert commands(s, "UserPromptSubmit") == [] and commands(s, "Stop") == []
        assert all(r in s["permissions"]["allow"] for r in cli.PERMISSION_RULES)

    def test_plugin_script_reports_init_hooks(self, capsys, monkeypatch):
        init(capsys)
        spec = importlib.util.spec_from_file_location("ip_test", PLUGIN / "hooks" / "install_permissions.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod.shutil, "which", lambda n: "uvx")
        mod.main()
        msg = json.loads(capsys.readouterr().out)["systemMessage"]
        assert "registered twice" in msg


class TestNoMcpInClaudeCode:
    def test_plugin_files(self):
        plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        assert "mcpServers" not in plugin and plugin["version"] == __version__
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
        assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"] == \
            f"uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite-hook --uvx"
        assert hooks["Stop"][0]["hooks"][0]["command"] == \
            f"uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite-stop-hook --uvx"
        start = hooks["SessionStart"][0]["hooks"][0]["command"]
        assert start.startswith('python "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || py ')

    def test_init_adds_no_mcp_and_reports_old(self, capsys, monkeypatch, tmp_path):
        home = Path(os.path.expanduser("~"))
        cj = home / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {"command": "x"}},
                                  "projects": {"C:/p": {"mcpServers": {"n3mc-workingmemory": {}}}}}), encoding="utf-8")
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {}}}), encoding="utf-8")
        before = (cj.read_text(encoding="utf-8"), (proj / ".mcp.json").read_text(encoding="utf-8"))
        monkeypatch.chdir(proj)
        _c, out = init(capsys)
        assert "claude mcp remove n3mc-workingmemory -s user" in out
        assert "-s local (run it in that project)" in out and 'remove the "n3mc-workingmemory" entry from' in out
        assert before == (cj.read_text(encoding="utf-8"), (proj / ".mcp.json").read_text(encoding="utf-8"))


class TestSeparateNames:
    def test_names(self):
        assert config.DEFAULTS["server_port"] == 18524
        assert paths.DATA_DIR_NAME == ".n3mc-workingmemory" and paths.PID_FILE_NAME == "n3mc-lite.pid"
        assert cli.RULES_MD_NAME == "n3mc-lite-behavior.md"
        assert config.DEFAULTS["server_port"] not in (18520, 18521)
        assert paths.DATA_DIR_NAME != ".n3mc" and cli.RULES_MD_NAME != "n3mc-behavior.md"
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for name in re.findall(r"^(n3mc[\w-]*) = ", text, re.M):
            assert name not in ("n3mc", "n3mc-hook", "n3mc-stop-hook", "n3mc-pro")


class TestRulesFile:
    def _write(self, proj, uvx=False):
        cli._ensure_rules_md(proj, uvx)
        return (proj / ".claude" / "rules" / "n3mc-lite-behavior.md")

    def test_pip_and_uvx_forms(self, tmp_path):
        p = self._write(tmp_path)
        text = p.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "<!-- n3mc-lite-behavior v1 -->" and text.endswith("kept only in `audit.log`.\n")
        assert f"`{cli.shell_path(sys.executable)} -m n3mc_mcp.cli --search" in text
        p = self._write(tmp_path, uvx=True)
        assert f"`uvx --from n3memorycore-mcp-lite=={__version__} n3mc-lite --search" in p.read_text(encoding="utf-8")
        assert list(p.parent.glob("n3mc-lite-behavior.md.bak-n3mc-lite-*"))

    def test_other_python_and_old_pin_replaced(self, tmp_path):
        p = tmp_path / ".claude" / "rules" / "n3mc-lite-behavior.md"
        p.parent.mkdir(parents=True)
        p.write_text(cli._RULES_MD_TEMPLATE.replace("{cli}", "C:/Old/Python311/python.exe -m n3mc_mcp.cli"),
                     encoding="utf-8")
        self._write(tmp_path)
        assert "C:/Old/Python311" not in p.read_text(encoding="utf-8")
        p.write_text(cli._RULES_MD_TEMPLATE.replace("{cli}", "uvx --from n3memorycore-mcp-lite==2.0.0 n3mc-lite"),
                     encoding="utf-8")
        self._write(tmp_path)
        assert "==2.0.0" not in p.read_text(encoding="utf-8")

    def test_edited_file_kept(self, tmp_path):
        p = self._write(tmp_path)
        p.write_text(p.read_text(encoding="utf-8") + "\nMy own note.\n", encoding="utf-8")
        self._write(tmp_path)
        assert "My own note." in p.read_text(encoding="utf-8")
        notices = json.loads(paths.notices_path().read_text(encoding="utf-8"))
        key = [k for k in notices if k.startswith("rules_md_edited:")][0]
        assert re.fullmatch(r"rules_md_edited:[0-9a-f]{12}", key)
        assert "has been edited by hand" in notices[key]["message"]
