# mcp-tmux-injector

MCP server that lets AI agents inject commands into tmux panes and read back output — Python REPLs, TCL interpreters, shell sessions.

## Why

CLI agents can't natively talk to a live REPL or a long-running shell. This server bridges the gap: the agent sends code to a tmux pane and reads back output, while a human can watch (or take over) the same pane.

## Features

- **Three execution tools** — `xsh` (shell), `xpy` (Python REPL), `xtcl` (TCL/EDA tools).
  - Default mode runs the command and waits up to 3 seconds (override up to 60). Longer commands turn into a background task; the call returns a `task_id` instead of output.
  - `read_after=N` skips the wait-for-completion logic — sends the code, sleeps N seconds, returns the pane's screen content. Use when the prompt itself is changing (entering a REPL, ssh, exit).
- **Get notified when something finishes** — `task_wait(task_id)` and `poll_pane(pattern)` return a small wrapper script. Run it through your client's command runner (Claude Code's `Monitor` is built for exactly this); when the task completes or the pattern matches, the script prints one line and exits. Then `task_output(task_id)` returns the full body.
- **Save output to a file** — `task_output(save=path)` and `capture_pane(save=path)` write filtered output to disk.
- **Per-pane locking** — only one injected command runs on a pane at a time.
- **Multi-pane dispatch** — `panes=[…]` with either `code=` (same code to all) or `codes=[…]` (different code per pane).
- **Works over ssh** — a pane that is ssh'd into another machine, or running a REPL there, behaves the same as a local one. Code is delivered as keystrokes, so nothing needs to exist on the remote filesystem — `file=` included.

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

The agent runs `xsh(pane, "python3", read_after=2)` then `xpy(pane, file="train.py")`. Long scripts return a `task_id`; the agent passes `task_wait(task_id)`'s wrapper script to `Monitor` (or any client's command runner), gets a one-line completion notice, and calls `task_output(task_id)` for the body.

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

## Code layout

```
mcp_tmux_injector/
  config.py     deny-list, instructions, shared paths
  tmux.py       tmux primitives (run, capture, sessions/windows)
  codec.py      markers, code delivery (keystroke-only, ssh-safe), extraction
  filters.py    output filtering (tqdm/grep/dedupe/save)
  tasks.py      background task registry, pane locks
  registry.py   pane/session registration, ownership, cleanup
  watch_cli.py  standalone watch CLI + poll fingerprints
  server.py     MCP tool definitions, entry point
tests/
  test_pure.py  pure-function tests (no tmux needed): .venv/bin/python tests/test_pure.py
```

## License

MIT
