<img src="docs/logo.png" alt="" width="96">

# mcp-tmux-injector

Let Claude Code or Codex run shell, Python, and TCL tools such as TCL tool in tmux panes you can watch, and hear when a long job ends.

![A training script run through mcp-tmux-injector. Left, the agent's tool calls and the [done] event from a real session, redrawn as text. Right, the tmux pane they ran in, as captured.](docs/hero.png)

## Install

Paste this into Claude Code or Codex:

```
Install mcp-tmux-injector by following https://raw.githubusercontent.com/MiiKiyoshi/mcp-tmux-injector/main/INSTALL.md
```

The agent checks for Python 3.10 or newer and tmux, shows where it will install and which
agents it will register with, and installs once you agree. Start the agent again
afterwards.

## Use

Ask for the work in tmux, in plain words:

```
run make in tmux and tell me when it finishes
```

The agent opens its own tmux session and runs the job there. When the job ends, the agent
hears it and reads the output. `tmux attach -t <session>` shows you the same screen, and
you can type in it.

To work in a pane you already have open, name it:

```
use my pane train:0.0
```

## Your sessions

In a session the agent did not create, it runs commands only in the panes you name. It
cannot add windows there, and it kills or restarts something there only when you ask.

## Over ssh

A pane logged in to another machine, or running a REPL there, works like a local one. The
code is typed in as keystrokes, so nothing needs to exist on the remote side.

## Memory

Ask how much memory a job holds, or to be told when it passes a limit:

```
tell me if the training session goes over 40 GB
```

Memory counts each pane's whole process tree, host RAM and GPU, and sums a session across
its panes.

## Configuration

Optional. Edit `~/.config/mcp-tmux-injector/config.json`, then start the agent again.

```json
{
  "tmux": {"socket_path": "/absolute/path/to/tmux.sock"},
  "deny": {"shell": ["kubectl *"], "python": [], "tcl": [], "send_text": []}
}
```

| Field | Effect |
|---|---|
| `tmux.socket_path` | The tmux server to use. Unset means tmux's default. |
| `deny.shell`, `deny.python`, `deny.tcl`, `deny.send_text` | [fnmatch](https://docs.python.org/3/library/fnmatch.html) patterns. Code with a line that matches one is refused before it is sent. |

## License

MIT. See [`LICENSE`](LICENSE).
