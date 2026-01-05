# mcp-tmux-injector

MCP server for injecting commands into tmux sessions. Control Python REPLs, TCL/OpenROAD, shell, and SSH sessions through Claude.

## Requirements

- Python 3.10+
- tmux
- Claude Desktop or Claude Code

## Installation

```bash
# Clone repository
git clone https://github.com/kiyoshi/mcp-tmux-injector.git
cd mcp-tmux-injector

# Install
pip install -e .

# Or run installer
./install.sh
```

## Claude Desktop Configuration

Add to `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tmux-injector": {
      "command": "mcp-tmux-injector"
    }
  }
}
```

## Claude Code Configuration

```bash
claude mcp add tmux-injector -- mcp-tmux-injector
```

## Tools

### Shell Commands
- `xsh(session, code)` - Execute shell command (blocking)
- `xsh_start(session, code)` - Execute shell command (background)
- `xsh_peek(session, code, wait)` - Execute without waiting for end marker

### Python REPL
- `xpy(session, code)` - Execute Python code (blocking)
- `xpy_start(session, code)` - Execute Python code (background)
- `xpy_peek(session, code, wait)` - Execute without waiting for end marker

### TCL/OpenROAD
- `xtcl(session, code)` - Execute TCL code (blocking)
- `xtcl_start(session, code)` - Execute TCL code (background)
- `xtcl_peek(session, code, wait)` - Execute without waiting for end marker

### Utilities
- `send_keys(session, keys, enter)` - Send raw keys (for passwords, prompts)
- `capture_pane(session, lines)` - Capture pane content
- `tmux_sessions()` - List available tmux sessions
- `task_status(task_id)` - Check background task status
- `task_result(task_id, timeout)` - Get background task result
- `task_list()` - List all background tasks
- `task_cancel(task_id)` - Cancel a background task
- `task_cancel_all()` - Cancel all background tasks

## Usage Patterns

### Shell Commands
```
xsh("t1", "ls -la && pwd")
```

### Python REPL (enter and exit)
```
xsh_peek("t1", "python3")           # Start Python
xpy("t1", "print(1+1)")             # Run Python code
xpy_peek("t1", "import os; os._exit(0)")  # Exit Python
xsh("t1", "echo back to bash")      # Back to shell
```

### OpenROAD/TCL (enter and exit)
```
xsh_peek("t1", "openroad")          # Start OpenROAD
xtcl("t1", "puts hello")            # Run TCL code
xtcl_peek("t1", "exit")             # Exit OpenROAD
xsh("t1", "echo back")              # Back to shell
```

### SSH with Password
```
xsh_peek("t1", "ssh user@host")     # Get password prompt
send_keys("t1", "mypassword")       # Send password
capture_pane("t1")                  # Verify login
xsh("t1", "hostname")               # Run remote commands
xsh_peek("t1", "exit")              # Disconnect
```

## Target Format

tmux target format: `session:window.pane`

- `t1` - Current window and pane
- `t1:0` - Window 0
- `t1:0.1` - Window 0, pane 1
- `t1:.1` - Current window, pane 1

Press `Ctrl+b q` in tmux to display pane numbers.

## License

MIT
