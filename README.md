# mcp-tmux-injector

MCP server that lets AI agents (Claude Code, etc.) inject commands into tmux panes and collect output — across Python REPLs, TCL interpreters, and shell sessions.

## Why

CLI agents like Claude Code run in their own process. They can't natively interact with a live Python REPL or a long-running shell session. mcp-tmux-injector bridges that gap: the agent sends code to a tmux pane and reads back the output, while the human can watch (or take over) the same pane at any time.

## Features

- **3 execution modes × 3 languages = 9 tools**

|          | blocking   | background   | peek        |
|----------|------------|--------------|-------------|
| Python   | `xpy`      | `xpy_start`  | `xpy_peek`  |
| TCL      | `xtcl`     | `xtcl_start` | `xtcl_peek` |
| Shell    | `xsh`      | `xsh_start`  | `xsh_peek`  |

- **Pane state tracking** — lock prevents parallel injection into the same pane
- **Task monitoring** — `task_wait`, `task_output`, `task_status` for background jobs
- **Output filtering** — built-in grep, tail, head per tool call
- **Session management** — `create_session`, `create_window`, `respawn_pane`
- **Multi-pane dispatch** — send the same (or different) code to multiple panes in parallel

## Requirements

- Python ≥ 3.10
- tmux (any recent version)
- An MCP-compatible client (Claude Code, etc.)

## Installation

```bash
pip install mcp-tmux-injector
```

Or from source:

```bash
git clone https://github.com/yourusername/mcp-tmux-injector
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

**Running code in a Python REPL**

```
"train.py를 백그라운드로 실행하고 끝나면 알려줘"
```

The agent uses `xsh_peek` to start Python, `xpy_start` to launch the script, and `task_wait` to notify on completion.

**Parallel work across windows**

```
"work 세션 각 윈도우에서 서로 다른 config로 학습 돌려줘"
```

The agent dispatches to multiple panes simultaneously using `panes=` + `codes=`.

**Monitoring a long-running process**

```
"현재 각 pane 상태 확인해줘"
```

`ls(session="work", gpu=True)` shows PID, process, cwd, and GPU memory per pane.

## Tool reference

See [instructions.txt](instructions.txt) for the complete tool selection guide and usage patterns the agent follows.

## License

MIT
