"""Layer 7: CLI commands and server management (HTTP sender replaced)."""

import http.server
import json
import os
import threading

from n3mc_mcp import cli, paths
from n3mc_mcp import config as config_mod


class TestServerDecision:
    def test_decisions(self):
        root = str(paths.data_dir())
        cv = config_mod.code_version()
        assert cli._health_decision(None) == "none"
        assert cli._health_decision({}) == "legacy"
        assert cli._health_decision({"root": "/other"}) == "foreign"
        assert cli._health_decision({"root": root, "code_version": "x"}) == "stale"
        assert cli._health_decision({"root": root, "code_version": cv}) == "ok"

    def test_foreign_port_is_not_used(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "_health", lambda port: {"root": "/somewhere/else", "pid": 1})
        assert cli.ensure_server(cfg) is False
        assert "another N3MC folder's server is using port" in cli.last_reason()
        assert "port_conflict" in paths.load_notices()

    def test_failed_marker_blocks_restart(self, cfg, monkeypatch):
        monkeypatch.setattr(cli, "_health", lambda port: None)
        paths.start_failed_path().write_text("x", encoding="utf-8")
        assert cli.ensure_server(cfg) is False and "could not start" in cli.last_reason()


class TestNoProxy:
    def test_proxy_ignored(self, monkeypatch):
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"root": "x"}')

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        assert cli._health(srv.server_address[1]) == {"root": "x"}
        srv.shutdown()
        assert hits == ["/health"]


class TestCommands:
    def _fake(self, monkeypatch):
        sent = []
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: True)

        def post(path, body, port, timeout=30):
            sent.append((path, body))
            return 200, {"text": "TEXT"}
        monkeypatch.setattr(cli, "_post", post)
        return sent

    def test_tool_mapping(self, monkeypatch, capsys):
        sent = self._fake(monkeypatch)
        cli.main(["--buffer", "hello", "--turn-id", "t"])
        cli.main(["--list"])
        cli.main(["--recall-thread", "T", "--before", "1"])
        cli.main(["--delete", "abc"])
        cli.main(["--delete-session", "s"])
        cli.main(["--repair"])
        assert [p for p, _ in sent] == ["/tools/save_memory?nudge=0", "/tools/list_memories?nudge=0",
                                       "/tools/recall_thread?nudge=0", "/tools/delete_memory?nudge=0",
                                       "/tools/delete_memories_by_session?nudge=0", "/tools/repair_memory?nudge=0"]
        assert sent[0][1] == {"content": "hello", "agent_name": "claude-code", "turn_id": "t"}
        assert capsys.readouterr().out.count("TEXT\n") == 6

    def test_search_without_limit_and_bad_since(self, monkeypatch, capsys):
        sent = self._fake(monkeypatch)
        cli.main(["--search", "kw", "--since", "2026-11-13"])
        assert sent[0][0] == "/search" and "max_chars" not in sent[0][1]
        assert cli.main(["--search", "kw", "--since", "yesterday"]) == 2
        assert "Error: since must be" in capsys.readouterr().err

    def test_no_server_outputs(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "ensure_server", lambda cfg, max_wait=60: False)
        cli.main(["--list"])
        assert capsys.readouterr().out.startswith("Error: memory backend unreachable. The N3MC Lite memory server")
        cli.main(["--search", "kw"])
        assert "_(memory search unavailable:" in capsys.readouterr().out


class TestStop:
    def test_rules_cleanup_rotation(self, isolated, monkeypatch, capsys):
        proj = isolated / "proj"
        proj.mkdir()
        old = paths.data_dir() / "turn_id_old.txt"
        old.write_text("x", encoding="utf-8")
        os.utime(old, (0, 0))
        paths.audit_log_path().write_text("x" * 20, encoding="utf-8")
        monkeypatch.setattr(cli, "AUDIT_ROTATE_BYTES", 10)
        monkeypatch.setattr(cli, "_read_stdin_text", lambda: json.dumps({"cwd": str(proj)}))
        assert cli.cmd_stop() == 0
        rules = (proj / ".claude" / "rules" / "n3mc-lite-behavior.md").read_text(encoding="utf-8")
        assert rules.startswith("<!-- n3mc-lite-behavior v1 -->") and "-m n3mc_mcp.cli --search" in rules
        assert not old.exists() and (paths.data_dir() / "audit.log.1").exists()
        assert capsys.readouterr().out == ""

    def test_edited_rules_not_replaced(self, isolated, monkeypatch):
        proj = isolated / "proj2"
        f = proj / ".claude" / "rules" / "n3mc-lite-behavior.md"
        f.parent.mkdir(parents=True)
        f.write_text("my own notes", encoding="utf-8")
        monkeypatch.setattr(cli, "_read_stdin_text", lambda: json.dumps({"cwd": str(proj)}))
        cli.cmd_stop()
        assert f.read_text(encoding="utf-8") == "my own notes"
        assert any(k.startswith("rules_md_edited:") for k in paths.load_notices())

    def test_other_install_form_is_replaced(self, isolated, monkeypatch):
        proj = isolated / "proj3"
        f = proj / ".claude" / "rules" / "n3mc-lite-behavior.md"
        f.parent.mkdir(parents=True)
        f.write_text(cli.rules_text(True), encoding="utf-8")
        monkeypatch.setattr(cli, "_read_stdin_text", lambda: json.dumps({"cwd": str(proj)}))
        cli.cmd_stop(uvx=False)
        assert f.read_text(encoding="utf-8") == cli.rules_text(False)
