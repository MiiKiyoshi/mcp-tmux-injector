# mcp-tmux-injector

MCP server that lets AI agents inject commands into tmux panes and read back output: Python REPLs, TCL interpreters, shell sessions.

## Why

CLI agents can't natively talk to a live REPL or a long-running shell. This server bridges the gap: the agent sends code to a tmux pane and reads back output, while a human can watch (or take over) the same pane.

## Features

- **Three execution tools**: `xsh` (shell), `xpy` (Python REPL), `xtcl` (TCL/HPC tools).
  - Default mode waits up to 3 seconds, then turns unfinished work into a background task and returns its `task_id`. Known-slow work leaves the inline timeout unset so this response returns before the MCP client request expires.
  - `read_after=N` skips the wait-for-completion logic: sends the code, sleeps N seconds, returns the pane's screen content. Use when the prompt itself is changing (entering a REPL, ssh, exit).
- **One event stream per connection**: At the start of every new MCP connection, including after a client or server restart, call `wait_events()` once and start its returned script exactly once using its instructions. Do not poll, start a duplicate, or assume an earlier waiter survived. Events queued before the waiter starts and unread events carried from a dead server are delivered when the new waiter starts. A promoted task reports `[done]`, `poll_pane(pattern)` reports `[match]`, and `task_output(task_id)` returns the full body.
- **Memory, per pane or per session**: `mem_pane` sums whole process trees (host RSS + GPU), so a tool that forks helpers is accounted for. `mem_pane(session=…)` gives a per-pane table with a total, `session="*"` one row per session. `watch_mem(pane=… | session=…, rss_gb=…, gpu_gb=…)` is quiet under the cap and puts one breach report with the table on the event stream.
- **Per-pane locking**: only one injected command runs on a pane at a time.
- **Your sessions stay yours**: in a session the agent did not create, it can run commands in the panes you let it register, but adding windows is refused, and killing or respawning anything takes `force=True`, which the agent passes only when you ask.
- **Strict arguments**: a call with a parameter the tool does not take is refused, and the error lists the ones it does take.
- **Works over ssh**: a pane that is ssh'd into another machine, or running a REPL there, behaves the same as a local one. Code is delivered as keystrokes, so nothing needs to exist on the remote filesystem: `file=` included.

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

The agent runs `xsh(pane, "python3", read_after=2)` then `xpy(pane, file="train.py")`. Long scripts return a `task_id`; the `[done]` line lands on the session's event stream, and the agent calls `task_output(task_id)` for the body.

**Parallel work across windows**

```
"Run training in each window of the work session with different configs"
```

The agent sends one `xpy` or `xsh` call per window, each with its own config.

**Check session state**

```
"Show the current status of each pane in the work session"
```

`ls(session="work")` shows PID, process, and cwd per pane. For memory, `mem_pane` sums whole process trees:

```
mem_pane(session="*")        mem_pane(session="work_4")
SESSION  CPU       GPU       PANE           CPU       GPU
work_1  10.3 GiB  -         work_4:inn.0  6.6 GiB   -
work_4  10.9 GiB  -         work_4:py.0   4.3 GiB   -
Total    21.2 GiB  -         Total          10.9 GiB  -
```

**Catch a runaway before it takes the host down**

```
"Tell me if the training session goes over 40 GB"
```

`watch_mem(session="work", rss_gb=40)` stays quiet under the cap and puts the table above on the event stream on the first breach, plus what the host has left. Watch the session rather than a pane when a job spans several — two panes at 6 GiB each pass a 10 GiB per-pane cap while the session sits at 12 GiB.

## Configuration

Optional settings at `~/.config/mcp-tmux-injector/config.json`:

```json
{
  "tmux": {
    "socket_path": "/absolute/path/to/tmux.sock"
  },
  "deny": {
    "shell": ["kubectl *", "rm -rf /*"],
    "python": [],
    "tcl": [],
    "send_text": ["kubectl *"]
  }
}
```

When `tmux.socket_path` is set, every tmux operation uses that socket. When it
is omitted, tmux uses its default socket selection.

Patterns use [fnmatch](https://docs.python.org/3/library/fnmatch.html) and match per line of code being sent. If the file is missing, nothing is blocked.

## Tool reference

Each tool describes its own parameters to the agent. [INSTRUCTIONS.md](INSTRUCTIONS.md) holds the rules the agent loads in every session, kept under the 2048 characters Claude Code shows.

## Code layout

```
mcp_tmux_injector/
  config.py     deny-list, instructions, shared paths
  tmux.py       tmux primitives (run, capture, sessions/windows)
  codec.py      markers, code delivery (keystroke-only, ssh-safe), extraction
  filters.py    output filtering (grep with context, dedupe)
  tasks.py      background task registry, pane locks
  registry.py   pane/session registration, ownership, cleanup
  mem.py        per-pane process-tree memory (host RSS + GPU), host totals
  events.py     the session's event stream + waiter script
  watch.py      poll_pane / watch_mem threads + poll fingerprints
  server.py     MCP tool definitions, entry point
tests/
  test_pure.py              pure functions, no tmux needed
  test_surface.py           instructions size, schemas, argument checking
  test_tools.py             every tool against a private tmux server on its own socket
  test_wait_instructions.py event stream and client-specific waiting
```

Run them with `pip install -e ".[dev]"` and then `python -m pytest -q`.

## License

MIT
