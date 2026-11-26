"""Layer 10: --init (MCP and hooks), the plugin files, coexistence with Free / Pro (spec §8, §12.8,
§12.9, §14). A temporary HOME only: the real ~/.claude and ~/.claude.json are never touched."""
import glob
import importlib.util
import json
import os
import pathlib
import sys

import pytest

import conftest
from n3mc_mcp import __version__, cli, config, paths

REPO = conftest.REPO_ROOT
PIN = f"n3memorycore-mcp-lite=={__version__}"


@pytest.fixture
def home(isolate):
    return isolate["home"]


def _settings(home):
    return home / ".claude" / "settings.json"


def _cj(home):
    return home / ".claude.json"


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def _hook_cmds(home, event):
    s = _read(_settings(home))
    return [h.get("command") for e in s.get("hooks", {}).get(event, []) for h in e.get("hooks", [])]


def _backups(home):
    return [os.path.join(d, n) for d, _, files in os.walk(home) for n in files if ".bak-n3mc-lite-" in n]


def _load_ip():
    path = os.path.join(REPO, "plugins", "n3mc-workingmemory", "hooks", "install_permissions.py")
    spec = importlib.util.spec_from_file_location("ip_coexist", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestInitMcp:
    def test_entry(self, home):
        _write(_cj(home), {"numStartups": 3, "mcpServers": {"other": {"command": "x"},
                                                             "n3mc-workingmemory": {"command": "uvx", "args": ["--from", "n3memorycore-mcp-lite", "n3mc-workingmemory"]}}})
        assert cli.init() == 0
        cj = _read(_cj(home))
        e = cj["mcpServers"]["n3mc-workingmemory"]
        assert e["type"] == "stdio" and e["args"] == [] and os.path.isabs(e["command"]) and not e["command"].startswith('"')
        assert e["env"] == {"N3MC_AGENT_NAME": "claude-code", "N3MC_DATA_DIR": paths.data_dir()}
        assert cj["numStartups"] == 3 and cj["mcpServers"]["other"] == {"command": "x"}

    def test_rules_and_deny(self, home):
        _write(_settings(home), {"permissions": {"deny": ["mcp__n3mc-workingmemory", "Bash(rm *)"]}})
        cli.init()
        p = _read(_settings(home))["permissions"]
        assert all(r in p["allow"] for r in cli.MCP_PERMISSION_RULES)
        assert not any("delete" in r for r in p["allow"]) and p["deny"] == ["Bash(rm *)"]

    def test_creates_claude_json(self, home):
        cli.init()
        assert "n3mc-workingmemory" in _read(_cj(home))["mcpServers"]

    def test_unreadable_claude_json(self, home):
        _cj(home).write_text("{bad", encoding="utf-8")
        _write(_settings(home), {"a": 1})
        before = _settings(home).read_text(encoding="utf-8")
        assert cli.init() == 1
        assert _cj(home).read_text(encoding="utf-8") == "{bad" and _settings(home).read_text(encoding="utf-8") == before

    def test_rerun_writes_nothing(self, home):
        cli.init()
        snap = (_cj(home).read_bytes(), _settings(home).read_bytes(), len(_backups(home)))
        cli.init()
        assert snap == (_cj(home).read_bytes(), _settings(home).read_bytes(), len(_backups(home)))

    def test_backup(self, home):
        _write(_cj(home), {"x": 1})
        _write(_settings(home), {"y": 1})
        cli.init()
        assert len(_backups(home)) == 2

    def test_other_scopes_reported(self, home, tmp_path, monkeypatch, capsys):
        _write(_cj(home), {"projects": {"C:/p": {"mcpServers": {"n3mc-workingmemory": {}}}}})
        proj = tmp_path / "proj"
        _write(proj / ".mcp.json", {"mcpServers": {"n3mc-workingmemory": {"command": "old"}}})
        monkeypatch.chdir(proj)
        cli.init()
        out = capsys.readouterr().out
        assert "(project C:/p)" in out and str(proj / ".mcp.json") in out
        assert _read(proj / ".mcp.json")["mcpServers"]["n3mc-workingmemory"] == {"command": "old"}

    @pytest.mark.parametrize("plugin", ["n3mc-workingmemory-mcp@neuralnexusnote", "n3mc-workingmemory@neuralnexusnote"])
    def test_plugin_enabled(self, home, plugin, capsys):
        _write(_settings(home), {"enabledPlugins": {plugin: True}})
        _write(_cj(home), {"mcpServers": {"n3mc-workingmemory": {"command": "x"}}})
        cli.init()
        assert "n3mc-workingmemory" not in _read(_cj(home))["mcpServers"]
        assert "registers the N3MC Lite MCP server, so n3mc-lite --init added no MCP server" in capsys.readouterr().out

    def test_hooks_kept(self, home):
        cli.init(hooks=True)
        cli.init()
        assert any("n3mc-lite-hook" in c or "lite_hook" in c for c in _hook_cmds(home, "UserPromptSubmit"))


class TestInitHooksChoice:
    def test_plain_init_no_hooks(self, home):
        _write(_settings(home), {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}})
        cli.init()
        assert _read(_settings(home))["hooks"] == {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}

    def test_init_hooks_both(self, home):
        cli.init(hooks=True)
        assert "n3mc-workingmemory" in _read(_cj(home))["mcpServers"]
        assert len(_hook_cmds(home, "UserPromptSubmit")) == 1 and len(_hook_cmds(home, "Stop")) == 1

    def test_refresh_existing(self, home, capsys):
        _write(_settings(home), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "C:/old/n3mc-lite-hook.exe"}]}]}})
        cli.init()
        out = capsys.readouterr().out
        assert "also refreshed them (MCP + hooks)" in out
        cmds = _hook_cmds(home, "UserPromptSubmit")
        assert len(cmds) == 1 and "C:/old" not in cmds[0] and len(_hook_cmds(home, "Stop")) == 1
        assert "n3mc-workingmemory" in _read(_cj(home))["mcpServers"]

    def test_hooks_without_init(self):
        with pytest.raises(SystemExit) as e:
            cli.main(["--hooks"])
        assert e.value.code == 2


class TestInit:
    def test_hook_commands(self, home):
        cli.init(hooks=True)
        for ev, name in (("UserPromptSubmit", "n3mc-lite-hook"), ("Stop", "n3mc-lite-stop-hook")):
            c = _hook_cmds(home, ev)[0]
            assert "\\" not in c and (name in c or "-m n3mc_mcp" in c)
        allow = _read(_settings(home))["permissions"]["allow"]
        assert all(r in allow for r in cli.permission_rules())
        entry = _read(_settings(home))["hooks"]["Stop"][0]["hooks"][0]
        assert entry["timeout"] == 60 and entry["type"] == "command"

    def test_shell_path(self, monkeypatch):
        monkeypatch.setattr(cli, "_short_path", lambda p: "")
        assert cli.shell_path("C:/Python312/python.exe") == "C:/Python312/python.exe"
        assert cli.shell_path("C:/PROGRA~1/Python/python.exe") == "C:/PROGRA~1/Python/python.exe"
        assert cli.shell_path("C:/Program Files/py.exe") == '"C:/Program Files/py.exe"'
        assert cli.shell_path("C:/O'Brien/py.exe") == '"C:/O\'Brien/py.exe"'
        assert cli.shell_path("C:/A&B/py.exe") == '"C:/A&B/py.exe"'
        assert cli.shell_path("C:/a $b`c/py.exe") == '"C:/a \\$b\\`c/py.exe"'

    def test_obrien(self, home, tmp_path, monkeypatch):
        scripts = tmp_path / "O'Brien" / "Scripts"
        scripts.mkdir(parents=True)
        for n in ("n3mc-lite-hook.exe", "n3mc-lite-stop-hook.exe"):
            (scripts / n).write_text("")
        monkeypatch.setattr(cli.sysconfig, "get_path", lambda *a, **k: str(scripts))
        monkeypatch.setattr(cli, "_short_path", lambda p: "")
        cli.init(hooks=True)
        c = _hook_cmds(home, "UserPromptSubmit")[0]
        assert c.startswith('"') and cli.is_own_hook(c, "UserPromptSubmit")
        cli.init(hooks=True)
        assert len(_hook_cmds(home, "UserPromptSubmit")) == 1

    def test_rerun_and_backup(self, home):
        _write(_settings(home), {"x": 1})
        cli.init(hooks=True)
        n = len(_backups(home))
        snap = _settings(home).read_bytes()
        cli.init(hooks=True)
        assert _settings(home).read_bytes() == snap and len(_backups(home)) == n >= 1

    def test_bad_settings(self, home):
        _settings(home).parent.mkdir()
        _settings(home).write_text("[1]", encoding="utf-8")
        assert cli.init(hooks=True) == 1 and _settings(home).read_text(encoding="utf-8") == "[1]"

    @pytest.mark.parametrize("old", ["C:\\Python\\Scripts\\n3mc-lite-hook.exe", "/usr/bin/n3mc-lite-hook",
                                     "python -m n3mc_mcp.lite_hook", "C:\\Py\\python.exe -m n3mc_mcp.lite_hook",
                                     f"python {os.path.join(cli.PKG_DIR, 'lite_hook.py')}"])
    def test_own_forms_replaced(self, home, old):
        _write(_settings(home), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": old}]}]}})
        cli.init(hooks=True)
        cmds = _hook_cmds(home, "UserPromptSubmit")
        assert len(cmds) == 1 and cmds[0] != old


class TestInitNonStringCommand:
    def test_kept(self, home):
        _write(_settings(home), {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": None},
                                                               {"type": "command", "command": 5}]}]}})
        assert cli.init(hooks=True) == 0
        cmds = _hook_cmds(home, "Stop")
        assert None in cmds and 5 in cmds and len(cmds) == 3


class TestKeepsOtherProducts:
    OTHERS = ["C:/Py/Scripts/n3mc-hook.exe", "n3mc-stop-hook", 'python "C:/pro/n3mc_hook.py"',
              "python -m n3memorycore.n3mc_hook", "echo unrelated"]

    def test_kept_and_reported(self, home, capsys):
        _write(_settings(home), {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": c}]} for c in self.OTHERS]}})
        cli.init(hooks=True)
        cmds = _hook_cmds(home, "UserPromptSubmit")
        assert all(c in cmds for c in self.OTHERS) and len(cmds) == len(self.OTHERS) + 1
        out = capsys.readouterr().out
        assert "Another N3MC hook is registered for UserPromptSubmit and was left in place: n3mc-stop-hook" in out

    @pytest.mark.parametrize("c", ["n3mc-hook", "n3mc-hook.exe", "C:/x/n3mc-stop-hook", "python -m n3memorycore.n3mc_hook",
                                   "python -m n3memorycore.n3mc_stop_hook", "python C:/free/n3memorycore/n3mc_hook.py"])
    def test_free_rules_not_ours(self, c):
        assert not cli.is_own_hook(c, "UserPromptSubmit") and not cli.is_own_hook(c, "Stop")


class TestPluginEnabled:
    def test_init_hooks_with_full_plugin(self, home, capsys):
        _write(_settings(home), {"enabledPlugins": {"n3mc-workingmemory@neuralnexusnote": True}})
        cli.init(hooks=True)
        s = _read(_settings(home))
        assert not any(s.get("hooks", {}).get(ev) for ev in ("UserPromptSubmit", "Stop"))
        assert not _cj(home).exists() or "n3mc-workingmemory" not in _read(_cj(home)).get("mcpServers", {})
        assert all(r in s["permissions"]["allow"] for r in cli.permission_rules())
        out = capsys.readouterr().out
        assert "registers the N3MC Lite hooks, so n3mc-lite --init added no hooks" in out
        assert "registers the N3MC Lite MCP server" in out

    def test_script_reports_init_hooks(self, home, capsys):
        cli.init(hooks=True)
        capsys.readouterr()
        ip = _load_ip()
        ip.main([])
        msg = json.loads(capsys.readouterr().out)["systemMessage"]
        assert "The N3MC Lite hooks are registered twice" in msg
        ip.main(["--mcp"])
        out = capsys.readouterr().out
        assert "hooks are registered twice" not in out

    @pytest.mark.parametrize("argv,tag", [([], "[n3mc-workingmemory]"), (["--mcp"], "[n3mc-workingmemory-mcp]")])
    def test_mcp_twice(self, home, capsys, argv, tag):
        _write(_cj(home), {"mcpServers": {"n3mc-workingmemory": {"command": "x"}}})
        before = _cj(home).read_bytes()
        _load_ip().main(argv)
        msg = json.loads(capsys.readouterr().out)["systemMessage"]
        assert f"{tag} The N3MC Lite MCP server is registered twice in Claude Code" in msg
        assert _cj(home).read_bytes() == before

    def test_full_with_mcp_plugin(self, home, capsys):
        _write(_settings(home), {"enabledPlugins": {"n3mc-workingmemory-mcp@neuralnexusnote": True}})
        _load_ip().main([])
        assert "The n3mc-workingmemory-mcp plugin is enabled too" in json.loads(capsys.readouterr().out)["systemMessage"]
        _load_ip().main(["--mcp"])
        assert "enabled too" not in capsys.readouterr().out

    def test_mcp_plugin_plus_init_hooks_quiet(self, home, capsys, monkeypatch):
        cli.init(hooks=True)
        cj = _read(_cj(home))
        cj["mcpServers"].pop("n3mc-workingmemory")
        _write(_cj(home), cj)
        capsys.readouterr()
        ip = _load_ip()
        monkeypatch.setattr(ip.shutil, "which", lambda n: "uvx")
        ip.main(["--mcp"])
        assert capsys.readouterr().out == ""


class TestClaudeCodePlugins:
    def _load(self, plugin, *parts):
        with open(os.path.join(REPO, "plugins", plugin, *parts), encoding="utf-8") as f:
            return json.load(f)

    def test_mcp_servers(self):
        want = {"n3mc-workingmemory": {"command": "uvx", "args": ["--from", PIN, "n3mc-workingmemory"],
                                       "env": {"N3MC_AGENT_NAME": "claude-code"}}}
        for p in ("n3mc-workingmemory", "n3mc-workingmemory-mcp"):
            m = self._load(p, ".claude-plugin", "plugin.json")
            assert m["mcpServers"] == want and m["name"] == p and m["version"] == __version__

    def test_full_hooks(self):
        h = self._load("n3mc-workingmemory", "hooks", "hooks.json")["hooks"]
        assert set(h) == {"SessionStart", "UserPromptSubmit", "Stop"}
        assert h["UserPromptSubmit"][0]["hooks"][0] == {"type": "command", "command": f"uvx --from {PIN} n3mc-lite-hook --uvx", "timeout": 60}
        assert h["Stop"][0]["hooks"][0] == {"type": "command", "command": f"uvx --from {PIN} n3mc-lite-stop-hook --uvx", "timeout": 60}
        cmd = h["SessionStart"][0]["hooks"][0]["command"]
        assert cmd.startswith('python "${CLAUDE_PLUGIN_ROOT}/hooks/install_permissions.py" || py ') and "--mcp" not in cmd

    def test_mcp_hooks(self):
        h = self._load("n3mc-workingmemory-mcp", "hooks", "hooks.json")["hooks"]
        assert set(h) == {"SessionStart"}
        cmd = h["SessionStart"][0]["hooks"][0]["command"]
        assert 'install_permissions.py" --mcp || py ' in cmd and "[n3mc-workingmemory-mcp] Python was not found" in cmd

    def test_same_script(self):
        a = pathlib.Path(REPO, "plugins", "n3mc-workingmemory", "hooks", "install_permissions.py").read_bytes()
        b = pathlib.Path(REPO, "plugins", "n3mc-workingmemory-mcp", "hooks", "install_permissions.py").read_bytes()
        assert a == b


class TestSeparateNames:
    def test_names(self, monkeypatch):
        monkeypatch.delenv("N3MC_DATA_DIR")
        assert config.DEFAULTS["server_port"] == 18524 not in (18520, 18521)
        assert paths.data_dir().endswith(".n3mc-workingmemory") and not paths.data_dir().endswith(".n3mc")
        assert cli.RULES_NAME == "n3mc-lite-behavior.md" != "n3mc-behavior.md"
        assert paths.PID_NAME == "n3mc-lite.pid"
        assert all(n.startswith("n3mc-lite") for n, _ in cli.OURS.values())


class TestRulesFile:
    def _path(self, proj):
        return proj / ".claude" / "rules" / "n3mc-lite-behavior.md"

    def test_pip_form(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path), uvx=False)
        text = self._path(tmp_path).read_text(encoding="utf-8")
        assert text.startswith("<!-- n3mc-lite-behavior v1 -->\n")
        assert f"{cli.shell_path(sys.executable)} -m n3mc_mcp.cli --search" in text and "{cli}" not in text

    def test_uvx_form_replaces_other(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path), uvx=False)
        cli._ensure_rules_md(str(tmp_path), uvx=True)
        text = self._path(tmp_path).read_text(encoding="utf-8")
        assert f"uvx --from {PIN} n3mc-lite --search" in text
        assert glob.glob(str(self._path(tmp_path)) + ".bak-n3mc-lite-*")

    def test_old_pin_replaced(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path), uvx=True)
        p = self._path(tmp_path)
        p.write_text(p.read_text(encoding="utf-8").replace(PIN, "n3memorycore-mcp-lite==2.0.0"), encoding="utf-8")
        cli._ensure_rules_md(str(tmp_path), uvx=True)
        assert PIN in p.read_text(encoding="utf-8")

    def test_hand_edited(self, tmp_path):
        cli._ensure_rules_md(str(tmp_path), uvx=False)
        p = self._path(tmp_path)
        p.write_text(p.read_text(encoding="utf-8") + "\nmy own note\n", encoding="utf-8")
        cli._ensure_rules_md(str(tmp_path), uvx=True)
        assert "my own note" in p.read_text(encoding="utf-8")
        assert any(k.startswith("rules_md_edited:") for k in json.load(open(paths.notices_path(), encoding="utf-8")))
