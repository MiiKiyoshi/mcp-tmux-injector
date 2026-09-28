"""What an agent receives: instructions, tool schemas, and argument checking."""
import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_tmux_injector import server


def test_instructions_fit_the_client_cap():
    # Claude Code keeps only the first 2048 characters of server instructions.
    assert len(server.mcp.instructions) <= 1900


def test_schemas_carry_no_titles_or_null_defaults():
    schemas = json.dumps([t.inputSchema for t in asyncio.run(server.mcp.list_tools())])
    assert '"title"' not in schemas
    assert '"default": null' not in schemas


def test_unknown_arguments_are_rejected():
    with pytest.raises(ToolError) as error:
        asyncio.run(server.mcp.call_tool("capture_pane", {"pane": "s:w.0", "v": "x", "lines": 9}))
    assert str(error.value) == "capture_pane does not take lines, v. It takes: pane, tail, grep, C."
