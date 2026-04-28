# mcp-tmux-injector

MCP server that lets AI agents (Claude Code, etc.) inject commands into tmux panes and collect output — across Python REPLs, TCL interpreters, and shell sessions.

## Why

CLI agents like Claude Code run in their own process. They can't natively interact with a live Python REPL or a long-running shell session. mcp-tmux-injector bridges that gap: the agent sends code to a tmux pane and reads back the output, while the human can watch (or take over) the same pane at any time.

## Features

- **3 unified tools — `xsh` / `xpy` / `xtcl`**

  Each one has two modes selected by parameter:

  - **default mode** (no `read_after`): wait briefly for the command's end
    marker (default 3s, capped at 60s). If the command exceeds the timeout,
    it auto-promotes to a background task and returns a `task_id`.
  - **read_after mode** (`read_after=N`): no marker detection — send the
    code, sleep N seconds, return the pane capture from the begin marker.
    Use for prompt-changing commands (entering/exiting REPL, ssh, etc.)
    where end-marker pairing breaks.

- **Async completion via wrapper script** — `task_wait(task_id)` and
  `poll_pane(pattern)` return a path to a self-deleting bash script that
  blocks until the condition fires, then prints one line (`[done] task X`
  or `[match] <line>`) on stdout and exits. In Claude Code, pass the path
  to the `Monitor` tool for chat-notification ergonomics; with any other
  MCP client, run the script through whatever subprocess primitive that
  client offers.
- **Save output to file** — `task_output` and `capture_pane` accept `save=path`
  to dump filtered output for logging, with optional `prefix`/`suffix`
  (`task_output` also offers markdown code-fence wrapping).
- **Pane state tracking** — lock prevents parallel injection into the same pane
- **Task management** — `task_output`, `task_status`, `task_list`, `task_cancel`
- **Output filtering** — built-in grep, tail, head per tool call
- **Session management** — `create_session`, `create_window`, `respawn_pane`
- **Multi-pane dispatch** — send the same (or different) code to multiple panes in parallel

## Requirements

- Python ≥ 3.10
- tmux (any recent version)
- An MCP-compatible client (Claude Code, etc.)

## Installation

```bash
git clone https://github.com/MiiKiyoshi/mcp-tmux-injector
cd mcp-tmux-injector
pip install -e .
```

## Setup

Works with any MCP-compatible client. The commands below are Claude Code's
CLI; for other clients (Cursor, Cline, Zed, …) follow their own "add MCP
server" docs and use `mcp-tmux-injector` (or the `uv run` form) as the
launch command.

**Claude Code (user-global)**

After `pip install -e .` puts `mcp-tmux-injector` on PATH:

```bash
claude mcp add tmux-injector --scope user -- mcp-tmux-injector
```

Or run directly out of the repo with `uv` (no install of the binary needed):

```bash
claude mcp add tmux-injector --scope user -- \
  uv run --directory /absolute/path/to/mcp-tmux-injector mcp-tmux-injector
```

## Usage

Register a pane, then talk to the agent in natural language.

```python
# Register an existing pane
set_pane("mysession:main.0", "description")

# Or create a managed session
create_session("work", windows=["train", "eval"])
```

**Run a script in the background and get notified on completion**

```
"Run train.py and let me know when it's done"
```

The agent enters Python with `xsh(pane, "python3", read_after=2)`, then
`xpy(pane, file="train.py")`. If the script exceeds the 3s timeout it
auto-promotes to a task. `task_wait(task_id)` returns a wrapper script
that prints a one-line completion signal on stdout when run; the agent
runs it through whatever subprocess primitive the client offers (in
Claude Code, the `Monitor` tool surfaces the line as a chat
notification). On signal, `task_output(task_id)` returns the full body.

**Parallel work across windows**

```
"Run training in each window of the work session with different configs"
```

The agent dispatches to multiple panes simultaneously using `panes=` + `codes=`.

**Check session state**

```
"Show the current status of each pane in the work session"
```

`ls(session="work", gpu=True)` shows PID, process, cwd, and GPU memory per pane.

## Configuration

Optional deny-list to block specific commands from being sent to panes.

Create `~/.config/mcp-tmux-injector/config.json`:

```json
{
  "deny": {
    "shell": ["kubectl *", "rm -rf /*"],
    "python": [],
    "tcl": [],
    "send_text": ["kubectl *"]
  }
}
```

Each category corresponds to the tool type. Patterns use [fnmatch](https://docs.python.org/3/library/fnmatch.html) syntax and are matched per line of the code being sent. Matched commands are rejected before reaching the pane.

If the file does not exist, no commands are blocked.

## Tool reference

See [instructions.txt](instructions.txt) for the complete tool selection guide and usage patterns the agent follows.

## License

MIT
