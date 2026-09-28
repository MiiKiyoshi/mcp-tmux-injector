import asyncio
import os
import signal
import subprocess
import time
from types import SimpleNamespace

from mcp_tmux_injector import events, server


def test_restart_carries_only_unread_events_from_dead_servers(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "EVENT_DIR", tmp_path)
    monkeypatch.setattr(events, "PID", 300)
    monkeypatch.setattr(events, "EVENT_FILE", tmp_path / "300.log")
    monkeypatch.setattr(events, "ACK_FILE", tmp_path / "300.ack")
    monkeypatch.setattr(events, "WAITER_FILE", tmp_path / "300.waiter")
    monkeypatch.setattr(events, "SCRIPT_FILE", tmp_path / "300.sh")
    monkeypatch.setattr(events, "_alive", lambda pid: pid == 200)
    (tmp_path / "100.log").write_text("read\nunread\n")
    (tmp_path / "100.ack").write_text("1\n")
    (tmp_path / "100.waiter").write_text("999\n")
    (tmp_path / "101.log").write_text("no watermark\n")
    (tmp_path / "200.log").write_text("live\n")
    (tmp_path / "200.ack").write_text("0\n")

    events.init()

    assert events.EVENT_FILE.read_text() == "unread\nno watermark\n"
    assert events.ACK_FILE.read_text() == "0\n"
    assert not (tmp_path / "100.log").exists()
    assert (tmp_path / "200.log").read_text() == "live\n"
    delivered = subprocess.run(["sh", events.write_script(), "--once"], capture_output=True,
                               text=True, check=True)
    assert delivered.stdout == "unread\nno watermark\n"
    assert events.ACK_FILE.read_text() == "2\n"


def test_wait_events_selects_client_instructions(monkeypatch):
    monkeypatch.setattr(server.events, "write_script", lambda: "/example/events.sh")
    monkeypatch.setattr(server.events, "waiter_alive", lambda: False)
    monkeypatch.setattr(server.events, "unread", lambda: 0)
    context = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        clientInfo=SimpleNamespace(name="claude-code"))))
    monkeypatch.setattr(server.mcp, "get_context", lambda: context)
    for name in ("claude-code", "codex-mcp-client", "other-client"):
        context.session.client_params.clientInfo.name = name
        result = asyncio.run(server.mcp.call_tool("wait_events", {}))
        text = result[0][0].text
        assert ("Monitor" in text) == (name == "claude-code")
        assert ("codex queue" in text) == (name == "codex-mcp-client")
        assert ('sandbox_permissions="require_escalated"' in text) == (name == "codex-mcp-client")
        assert "task_output(task_id)" in text
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "wait_events")
    assert "ctx" not in tool.inputSchema["properties"]
    for needed in ("every new", "exactly once", "Do not poll", "Unread events"):
        assert needed in server.mcp.instructions, needed


def test_queue_before_ack(tmp_path, monkeypatch):
    for name, suffix in (("EVENT_FILE", "log"), ("ACK_FILE", "ack"),
                         ("WAITER_FILE", "waiter"), ("SCRIPT_FILE", "sh")):
        monkeypatch.setattr(events, name, tmp_path / f"events.{suffix}")
    events.EVENT_FILE.write_text('[done] task "quoted"\n[cap] $(not-a-command)\n')
    events.ACK_FILE.write_text("0\n")
    stub = tmp_path / "codex"
    stub.write_text("#!/bin/sh\n"
                    '[ "$1" = queue ] && [ "$2" = --thread ] && [ "$3" = test-thread ] && [ "$4" = --message ] || exit 2\n'
                    'touch "$TEST_QUEUE_DIR/attempt"\n'
                    '[ -f "$TEST_QUEUE_DIR/allow" ] || exit 1\n'
                    'printf "%s\\n" "$5" > "$TEST_QUEUE_DIR/payload"\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("TEST_QUEUE_DIR", str(tmp_path))
    # Keep the test independent of a running tmux server.
    timeout = tmp_path / "timeout"
    timeout.write_text("#!/bin/sh\nsleep 1\n")
    timeout.chmod(0o755)
    script = events.write_script()
    subprocess.run(["sh", "-n", script], check=True)
    process = subprocess.Popen(["sh", script, "--codex", "test-thread"],
                               start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / "attempt").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "attempt").exists()
        assert events.ACK_FILE.read_text().strip() == "0"
        (tmp_path / "allow").touch()
        deadline = time.monotonic() + 8
        while events.ACK_FILE.read_text().strip() != "2" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert events.ACK_FILE.read_text().strip() == "2"
        assert (tmp_path / "payload").read_text() == "[tmux-injector event]\n" + events.EVENT_FILE.read_text()
        assert process.poll() is None
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


def test_instructions_fit_the_client_cap():
    # Claude Code keeps only the first 2048 characters of server instructions.
    assert len(server.mcp.instructions) <= 1900
