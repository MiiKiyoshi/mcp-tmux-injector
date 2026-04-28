# mcp-tmux-injector

MCP server that lets AI agents inject commands into tmux panes and read back output — Python REPLs, TCL interpreters, shell sessions.

## Why

CLI agents can't natively talk to a live REPL or a long-running shell. This server bridges the gap: the agent sends code to a tmux pane and reads back output, while a human can watch (or take over) the same pane.

## Features

- **`xsh` / `xpy` / `xtcl`** — three execution tools, one per language.
  - **default mode**: wait up to 3s (capped 60s) for completion. Longer commands auto-promote to a background task.
  - **`read_after=N`**: skip end-marker detection, sleep N seconds, return the pane capture. Use for prompt-changing commands (entering REPL, ssh, exit).
- **Async completion** — `task_wait(task_id)` and `poll_pane(pattern)` return a wrapper script that signals when run. Plug it into the client's subprocess primitive (in Claude Code, `Monitor` fits this natively).
- **Save output** — `task_output` and `capture_pane` accept `save=path` to write filtered output to a file.
- **Per-pane lock** — two injected commands can't race on the same pane.
- **Multi-pane dispatch** — `panes=` with `code=` (same code) or `codes=` (per-pane code).

## Requirements

- Python ≥ 3.10
- tmux
- An MCP-compatible client (Claude Code, Cursor, Cline, Zed, …)

## Installation

```bash
git clone https://github.com/MiiKiyoshi/mcp-tmux-injector
cd mcp-tmux-injector
pip install -e .
```

## Setup

Below is Claude Code's CLI. For other clients, follow their "add MCP server" docs and use `mcp-tmux-injector` (or `uv run --directory <repo> mcp-tmux-injector`) as the launch command.

```bash
# After `pip install -e .` puts the binary on PATH:
claude mcp add tmux-injector --scope user -- mcp-tmux-injector

# Or run directly out of the repo (no install):
claude mcp add tmux-injector --scope user -- \
  uv run --directory /absolute/path/to/mcp-tmux-injector mcp-tmux-injector
```

## Usage

Register a pane, then talk to the agent in natural language.

```python
set_pane("mysession:main.0", "description")        # existing pane
create_session("work", windows=["train", "eval"])  # or a new managed session
```

**Run a script and get notified when it's done**

```
"Run train.py and let me know when it's done"
```

The agent enters Python with `xsh(pane, "python3", read_after=2)`, then `xpy(pane, file="train.py")` — if it doesn't finish in 3s, it auto-promotes to a task. `task_wait(task_id)` returns a wrapper script that signals when run; `task_output(task_id)` returns the body.

**Parallel work across windows**

```
"Run training in each window of the work session with different configs"
```

The agent dispatches to multiple panes via `panes=` + `codes=`.

**Check session state**

```
"Show the current status of each pane in the work session"
```

`ls(session="work", gpu=True)` shows PID, process, cwd, and GPU memory per pane.

## Configuration

Optional deny-list at `~/.config/mcp-tmux-injector/config.json`:

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

Patterns use [fnmatch](https://docs.python.org/3/library/fnmatch.html) and match per line of code being sent. If the file is missing, nothing is blocked.

## Tool reference

See [instructions.txt](instructions.txt).

## License

MIT
