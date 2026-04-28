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

  Single API, no `*_start` or `*_peek` variants.

- **Async completion via Monitor** — `task_wait(task_id)` and `poll_pane(pattern)`
  return a tiny script path. Pass it to Claude Code's `Monitor` tool to get a
  chat notification on completion / first match.
- **Save output to file** — `task_output` and `capture_pane` accept `save=path`
  to dump filtered output for logging, with optional prefix/suffix and
  markdown wrapping.
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

**Claude Code (user-global)**

```bash
claude mcp add tmux-injector --scope user -- mcp-tmux-injector
```

Or with `uv` (no install needed):

```bash
claude mcp add tmux-injector --scope user -- uvx mcp-tmux-injector
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

The agent calls `xsh(pane, "python3", read_after=2)` to start Python, then
`xpy(file="train.py")`. If the script exceeds the 3s timeout it auto-promotes
to a task. The agent calls `task_wait(task_id)` to obtain a shell command and
hands it to `Monitor` — Claude Code delivers a notification when the script
finishes.

**Parallel work across windows**

```
"Run training in each window of the work session with different configs"
```

The agent dispatches to multiple panes simultaneously using `panes=` + `codes=`.

**Monitor session status**

```
"Check the current status of each pane in the work session"
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
