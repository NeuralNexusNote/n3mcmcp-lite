"""Layer 10: --init, coexistence with Free / Pro, separate names. Temporary HOME only."""

import json
import os
import pathlib

from n3mc_mcp import cli, paths
from n3mc_mcp import config as config_mod


def settings_path():
    return pathlib.Path(os.path.expanduser("~")) / ".claude" / "settings.json"


class TestInit:
    def test_writes_hooks_and_rules_idempotently(self, capsys):
        assert cli.cmd_init() == 0
        s = json.loads(settings_path().read_text(encoding="utf-8"))
        cmd = s["hooks"]["UserPromptSubmit"][-1]["hooks"][0]
        assert cmd["timeout"] == 60 and cli.is_own_hook(cmd["command"], "UserPromptSubmit")
        assert "\\" not in cmd["command"]
        assert set(cli.permission_rules()) <= set(s["permissions"]["allow"])
        assert not any("--delete" in r for r in s["permissions"]["allow"])
        before = settings_path().read_text(encoding="utf-8")
        assert cli.cmd_init() == 0
        assert settings_path().read_text(encoding="utf-8") == before
        assert not list(settings_path().parent.glob("settings.json.bak-*"))

    def test_bad_settings_exit_1(self):
        settings_path().parent.mkdir(parents=True)
        settings_path().write_text("{bad", encoding="utf-8")
        assert cli.cmd_init() == 1
        assert settings_path().read_text(encoding="utf-8") == "{bad"

    def test_replaces_own_forms(self):
        settings_path().parent.mkdir(parents=True)
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command",
                                                        "command": "C:\\Py\\Scripts\\n3mc-lite-hook.exe"}]},
                                            {"hooks": [{"type": "command",
                                                        "command": "python -m n3mc_mcp.lite_hook"}]}]}}
        settings_path().write_text(json.dumps(s), encoding="utf-8")
        cli.cmd_init()
        s = json.loads(settings_path().read_text(encoding="utf-8"))
        assert len(s["hooks"]["UserPromptSubmit"]) == 1
        assert list(settings_path().parent.glob("settings.json.bak-n3mc-lite-*"))


class TestKeepsOtherProducts:
    def test_free_and_pro_kept(self, capsys):
        settings_path().parent.mkdir(parents=True)
        other = ["C:/Py/Scripts/n3mc-hook.exe", "python -m n3memorycore.n3mc_hook",
                 'python "C:/x/n3mc_hook.py"', "n3mc-pro-hook"]
        s = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": c}]} for c in other]}}
        settings_path().write_text(json.dumps(s), encoding="utf-8")
        cli.cmd_init()
        s = json.loads(settings_path().read_text(encoding="utf-8"))
        cmds = [h["command"] for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]]
        assert cmds[:4] == other and len(cmds) == 5
        assert capsys.readouterr().out.count("Another N3MC hook is registered") == 4


class TestPluginEnabled:
    def test_no_hooks_added(self, capsys):
        settings_path().parent.mkdir(parents=True)
        settings_path().write_text(json.dumps({"enabledPlugins": {cli.PLUGIN_KEY: True}}), encoding="utf-8")
        cli.cmd_init()
        s = json.loads(settings_path().read_text(encoding="utf-8"))
        assert s["hooks"]["UserPromptSubmit"] == [] and s["permissions"]["allow"]
        assert "plugin is enabled" in capsys.readouterr().out


class TestNoMcpInClaudeCode:
    def test_old_registration_reported_not_fixed(self, capsys):
        cj = pathlib.Path(os.path.expanduser("~")) / ".claude.json"
        cj.write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {"command": "x"}}}), encoding="utf-8")
        cli.cmd_init()
        assert "claude mcp remove n3mc-workingmemory -s user" in capsys.readouterr().out
        assert "n3mc-workingmemory" in json.loads(cj.read_text(encoding="utf-8"))["mcpServers"]


class TestSeparateNames:
    def test_names(self, monkeypatch):
        assert config_mod.DEFAULTS["server_port"] == 18524
        assert paths.pid_path().name == "n3mc-lite.pid"
        assert cli.OURS["UserPromptSubmit"] == ("n3mc-lite-hook", "n3mc_mcp.lite_hook")
        monkeypatch.delenv("N3MC_DATA_DIR")
        assert paths.data_dir().name == ".n3mc-workingmemory"
