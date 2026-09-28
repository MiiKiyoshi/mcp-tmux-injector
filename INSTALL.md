# Installing mcp-tmux-injector

This file is for the agent the user asked to install mcp-tmux-injector. Fetch it with
`curl -fsSL`, since a summarizing fetch drops the commands. Inspect first, show one summary,
install after the user agrees. On a machine that already has it, the same steps update it.

## 1. Inspect (change nothing)

- Python 3.10 or newer (the `mcp` package has no release for older ones): `python3 --version`.
  When older, `command -v python3.13 python3.12 python3.11 python3.10`. None: stop and tell
  the user. Use the one found as `python3` below.
- tmux: `command -v tmux`. Missing: tell the user tmux is needed and stop. Do not install it.
- Install directory: `$HOME/.local/share/mcp-tmux-injector`, unless the user named another.
  Note whether it already holds a checkout.
- Agents: `command -v claude` and `command -v codex`. Register with each one found.
- Existing registration: `claude mcp get tmux-injector`, `codex mcp get tmux-injector`. Note
  a command that differs from the one below.

## 2. Confirm

Show one summary, in the user's language: install directory (new or update), Python, tmux,
the agents to register with, and any existing registration that will be replaced.
Registration is user-level, available in every folder. Do not ask about scope. Ask once.

## 3. Install

    DIR="$HOME/.local/share/mcp-tmux-injector"
    git clone https://github.com/MiiKiyoshi/mcp-tmux-injector.git "$DIR"   # update: git -C "$DIR" pull --ff-only
    python3 -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q -U pip                                     # editable installs need a recent pip
    "$DIR/.venv/bin/pip" install -e "$DIR"
    "$DIR/.venv/bin/mcp-tmux-injector" --check                               # lists the tools

## 4. Register

Remove a registration the user agreed to replace (`claude mcp remove --scope user tmux-injector`,
`codex mcp remove tmux-injector`), then:

    claude mcp add --scope user tmux-injector -- "$DIR/.venv/bin/mcp-tmux-injector"
    codex mcp add tmux-injector -- "$DIR/.venv/bin/mcp-tmux-injector"

Pass no PATH. The panes it opens must see the user's own PATH.

## 5. Tell the user

The server loads when an agent session starts, so this session cannot use it yet. Start
the agent again, then ask for work in tmux in plain words.
