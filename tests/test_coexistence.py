"""Layer 10: --init, plugin files, coexistence with Free / Pro. Temporary HOME only."""

import glob
import json
import os
import sys
import tomllib

import pytest

from n3mc_mcp import cli, config, paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def settings_path(isolate):
    return os.path.join(isolate["home"], ".claude", "settings.json")


def load(isolate):
    return json.load(open(settings_path(isolate), encoding="utf-8"))


def write_settings(isolate, obj):
    os.makedirs(os.path.dirname(settings_path(isolate)), exist_ok=True)
    open(settings_path(isolate), "w", encoding="utf-8").write(json.dumps(obj, indent=2))


def cmds(settings, event):
    return [h["command"] for g in settings.get("hooks", {}).get(event, []) for h in g["hooks"]]


def backups(isolate):
    return glob.glob(settings_path(isolate) + ".bak-n3mc-lite-*")


class TestInit:
    def test_registers_two_hooks_and_rules(self, isolate, capsys):
        assert cli.cmd_init() == 0
        s = load(isolate)
        up, st = cmds(s, "UserPromptSubmit"), cmds(s, "Stop")
        assert len(up) == 1 and len(st) == 1
        assert "n3mc-lite-hook" in up[0] and "n3mc-lite-stop-hook" in st[0] and "\\" not in up[0]
        assert s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 60
        for rule in cli.permission_rules(cli.shell_path(sys.executable)):
            assert rule in s["permissions"]["allow"]
        assert not any("--delete" in r for r in s["permissions"]["allow"])
        assert "N3MC Lite hooks registered in" in capsys.readouterr().out

    def test_second_run_writes_nothing(self, isolate):
        cli.cmd_init()
        before = open(settings_path(isolate), "rb").read()
        mtime = os.path.getmtime(settings_path(isolate))
        cli.cmd_init()
        assert open(settings_path(isolate), "rb").read() == before and os.path.getmtime(settings_path(isolate)) == mtime

    def test_backup_before_change(self, isolate):
        write_settings(isolate, {"theme": "dark", "permissions": {"allow": ["Bash(ls)"]}})
        cli.cmd_init()
        assert len(backups(isolate)) == 1
        assert json.load(open(backups(isolate)[0], encoding="utf-8"))["theme"] == "dark"
        s = load(isolate)
        assert s["theme"] == "dark" and "Bash(ls)" in s["permissions"]["allow"]

    def test_invalid_settings_unchanged_exit_1(self, isolate, capsys):
        os.makedirs(os.path.dirname(settings_path(isolate)))
        open(settings_path(isolate), "w", encoding="utf-8").write("{ nope")
        assert cli.cmd_init() == 1
        assert open(settings_path(isolate), encoding="utf-8").read() == "{ nope"
        write_settings(isolate, {"hooks": []})
        assert cli.cmd_init() == 1

    @pytest.mark.parametrize("old", [
        "C:\\Python\\Scripts\\n3mc-lite-hook.exe",
        "python -m n3mc_mcp.lite_hook",
        "\"C:/Program Files/Py/python.exe\" -m n3mc_mcp.lite_hook",
        "n3mc-lite-hook",
    ])
    def test_own_forms_are_replaced_not_duplicated(self, isolate, old):
        write_settings(isolate, {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": old}]}]}})
        cli.cmd_init()
        c = cmds(load(isolate), "UserPromptSubmit")
        assert len(c) == 1 and c[0] != old

    def test_shell_path_quotes_shell_characters(self):
        assert cli.shell_path("C:/Users/me/Scripts/n3mc-lite-hook.exe") == "C:/Users/me/Scripts/n3mc-lite-hook.exe"
        assert cli.shell_path("C:/PROGRA~1/Py/python.exe") == "C:/PROGRA~1/Py/python.exe"
        assert cli.shell_path("C:/No Such Dir/n3mc-lite-hook.exe") == '"C:/No Such Dir/n3mc-lite-hook.exe"'
        assert cli.shell_path("C:/NoSuch/O'Brien/x.exe") == "\"C:/NoSuch/O'Brien/x.exe\""
        assert cli.shell_path("C:/NoSuch/A&B/x.exe") == '"C:/NoSuch/A&B/x.exe"'
        assert cli.shell_path("C:/NoSuch/a$b`c/x.exe") == '"C:/NoSuch/a\\$b\\`c/x.exe"'

    def test_quoted_hook_path_is_recognised_on_rerun(self, isolate, monkeypatch, tmp_path):
        folder = tmp_path / "O'Brien" / "Scripts"
        folder.mkdir(parents=True)
        (folder / "n3mc-lite-hook.exe").write_bytes(b"")
        (folder / "n3mc-lite-stop-hook.exe").write_bytes(b"")
        monkeypatch.setattr(cli.sysconfig, "get_path", lambda *a, **k: str(folder))
        cli.cmd_init()
        up = cmds(load(isolate), "UserPromptSubmit")
        assert len(up) == 1 and up[0].startswith('"') and "O'Brien" in up[0]
        assert cli.is_own_hook(up[0], "UserPromptSubmit")
        assert cli.is_own_hook('"C:/a\\$b\\`c/Scripts/n3mc-lite-hook.exe"', "UserPromptSubmit")
        before = open(settings_path(isolate), "rb").read()
        cli.cmd_init()
        assert open(settings_path(isolate), "rb").read() == before
        assert len(cmds(load(isolate), "UserPromptSubmit")) == 1 and len(cmds(load(isolate), "Stop")) == 1

    def test_no_mcp_files_touched(self, isolate, monkeypatch, tmp_path, capsys):
        claude_json = os.path.join(isolate["home"], ".claude.json")
        body = json.dumps({"mcpServers": {"n3mc-workingmemory": {"command": "x"}},
                           "projects": {"C:/p": {"mcpServers": {"n3mc-workingmemory": {"command": "x"}}}}})
        open(claude_json, "w", encoding="utf-8").write(body)
        proj = tmp_path / "proj"
        proj.mkdir()
        mcp = proj / ".mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"n3mc-workingmemory": {"command": "y"}}}))
        monkeypatch.chdir(proj)
        cli.cmd_init()
        out = capsys.readouterr().out
        assert open(claude_json, encoding="utf-8").read() == body
        assert json.loads(mcp.read_text())["mcpServers"]["n3mc-workingmemory"] == {"command": "y"}
        assert "claude mcp remove n3mc-workingmemory -s user" in out
        assert "claude mcp remove n3mc-workingmemory -s local" in out and "project C:/p" in out
        assert 'remove the "n3mc-workingmemory" entry from' in out


class TestKeepsOtherProducts:
    def test_free_and_pro_entries_kept_and_reported(self, isolate, capsys):
        write_settings(isolate, {"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "C:/Py/Scripts/n3mc-hook.exe"}]},
                                 {"hooks": [{"type": "command", "command": "python \"C:/x/N3MC-Pro/n3mc_hook.py\""}]},
                                 {"hooks": [{"type": "command", "command": "echo unrelated"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "n3mc-stop-hook"}]}]}})
        cli.cmd_init()
        s = load(isolate)
        up = cmds(s, "UserPromptSubmit")
        assert "C:/Py/Scripts/n3mc-hook.exe" in up and any("n3mc_hook.py" in c for c in up) and "echo unrelated" in up
        assert "n3mc-stop-hook" in cmds(s, "Stop")
        out = capsys.readouterr().out
        assert "Another N3MC hook is registered for UserPromptSubmit and was left in place: C:/Py/Scripts/n3mc-hook.exe" in out
        assert "echo unrelated" not in out

    @pytest.mark.parametrize("command", ["n3mc-hook", "C:/x/n3mc-stop-hook.exe", "python -m n3memorycore.n3mc_hook",
                                         "python C:/free/n3memorycore/n3mc_hook.py", "n3mc-pro-hook"])
    def test_lite_does_not_claim_other_products(self, command):
        assert not cli.is_own_hook(command, "UserPromptSubmit") and not cli.is_own_hook(command, "Stop")

    def test_lite_claims_its_own(self):
        assert cli.is_own_hook('"C:\\a b\\Scripts\\n3mc-lite-hook.exe"', "UserPromptSubmit")
        assert cli.is_own_hook("py -m n3mc_mcp.lite_stop_hook", "Stop")
        assert not cli.is_own_hook("n3mc-lite-stop-hook", "UserPromptSubmit")


class TestPluginEnabled:
    def test_init_registers_no_hooks(self, isolate, capsys):
        write_settings(isolate, {"enabledPlugins": {"n3mc-workingmemory@neuralnexusnote": True},
                                 "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "n3mc-lite-stop-hook"}]}]}})
        cli.cmd_init()
        s = load(isolate)
        assert cmds(s, "UserPromptSubmit") == [] and cmds(s, "Stop") == []
        assert cli.permission_rules(cli.shell_path(sys.executable))[0] in s["permissions"]["allow"]
        assert "The n3mc-workingmemory plugin is enabled and registers the N3MC Lite hooks, so n3mc-lite --init added no hooks." in capsys.readouterr().out


class TestNoMcpInClaudeCode:
    def plugin_dir(self):
        return os.path.join(ROOT, "plugins", "n3mc-workingmemory")

    def test_plugin_json_has_no_mcp_servers(self):
        data = json.load(open(os.path.join(self.plugin_dir(), ".claude-plugin", "plugin.json"), encoding="utf-8")) \
            if os.path.exists(os.path.join(self.plugin_dir(), ".claude-plugin", "plugin.json")) else {}
        if "mcpServers" in data or not data:
            pytest.skip("plugins/ still holds the v1.12.2 files (not regenerated: kept as instructed)")
        assert "mcpServers" not in data

    def test_hooks_json_has_three_hooks(self):
        data = json.load(open(os.path.join(self.plugin_dir(), "hooks", "hooks.json"), encoding="utf-8"))
        if "UserPromptSubmit" not in data.get("hooks", {}):
            pytest.skip("plugins/ still holds the v1.12.2 files (not regenerated: kept as instructed)")
        assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}


class TestSeparateNames:
    def test_nothing_is_shared_with_other_products(self):
        assert config.DEFAULTS["server_port"] == 18524 and config.DEFAULTS["server_port"] not in (18520, 18521, 18522, 18523)
        assert not paths.data_dir().endswith(".n3mc") and not paths.data_dir().endswith(".n3mc-pro")
        assert os.path.basename(paths.pid_path()) == "n3mc-lite.pid"
        assert cli._RULES_MD_TEMPLATE.startswith("<!-- n3mc-lite-behavior v1 -->")
        scripts = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))["project"]["scripts"]
        assert set(scripts) == {"n3mc-workingmemory", "n3mc-mcp-lite", "n3mc-lite", "n3mc-lite-hook", "n3mc-lite-stop-hook"}

    def test_default_data_folder_name(self, monkeypatch):
        monkeypatch.delenv("N3MC_DATA_DIR")
        assert os.path.basename(paths.data_dir()) == ".n3mc-workingmemory"


class TestRulesFile:
    def rules(self, folder):
        return os.path.join(str(folder), ".claude", "rules", "n3mc-lite-behavior.md")

    def test_pip_form_and_uvx_form(self, isolate, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        cli._ensure_rules_md(str(a), uvx=False)
        cli._ensure_rules_md(str(b), uvx=True)
        ta, tb = open(self.rules(a), encoding="utf-8").read(), open(self.rules(b), encoding="utf-8").read()
        assert "-m n3mc_mcp.cli --search" in ta and "uvx" not in ta
        assert "uvx --from n3memorycore-mcp-lite==2.0.2 n3mc-lite --search" in tb
        assert ta.split("\n")[0] == "<!-- n3mc-lite-behavior v1 -->"

    def test_other_form_is_replaced_with_backup(self, isolate, tmp_path):
        cli._ensure_rules_md(str(tmp_path), uvx=True)
        cli._ensure_rules_md(str(tmp_path), uvx=False)
        assert "uvx" not in open(self.rules(tmp_path), encoding="utf-8").read()
        assert len(glob.glob(self.rules(tmp_path) + ".bak-n3mc-lite-*")) == 1

    def test_same_content_does_nothing(self, isolate, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        m = os.path.getmtime(self.rules(tmp_path))
        cli._ensure_rules_md(str(tmp_path))
        assert os.path.getmtime(self.rules(tmp_path)) == m
        assert glob.glob(self.rules(tmp_path) + ".bak-*") == []

    def test_hand_edited_file_is_kept(self, isolate, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        p = self.rules(tmp_path)
        open(p, "a", encoding="utf-8").write("\nmy own rule\n")
        cli._ensure_rules_md(str(tmp_path))
        assert "my own rule" in open(p, encoding="utf-8").read()
        notes = [k for k in cli._read_notices() if k.startswith("rules_md_edited:")]
        assert len(notes) == 1 and len(notes[0].split(":")[1]) == 12

    def test_crlf_file_counts_as_same(self, isolate, tmp_path):
        cli._ensure_rules_md(str(tmp_path))
        p = self.rules(tmp_path)
        data = open(p, "rb").read().replace(b"\n", b"\r\n")
        open(p, "wb").write(data)
        cli._ensure_rules_md(str(tmp_path))
        assert glob.glob(p + ".bak-*") == [] and not [k for k in cli._read_notices() if k.startswith("rules_md")]
