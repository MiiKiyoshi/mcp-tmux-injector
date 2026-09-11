import asyncio
from types import SimpleNamespace

from mcp_tmux_injector import server


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
        assert ("write_stdin" in text) == (name == "codex-mcp-client")
        assert "task_output(task_id)" in text
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "wait_events")
    assert "ctx" not in tool.inputSchema["properties"]
