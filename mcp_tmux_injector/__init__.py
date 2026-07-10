"""
MCP Server for tmux command injection (xpy/xtcl/xsh functionality)
Sends commands to Python REPL, TCL-based EDA tools, or shell running in tmux panes.
Supports both blocking and non-blocking (background) execution.

IMPORTANT: Panes must be registered before use with set_pane().

USAGE PATTERNS:

0. Register pane first (REQUIRED):
   set_pane("t1:1.0", "OpenROAD Python REPL")
   ls()  # Check sessions and registered panes

1. Shell commands (bash pane):
   xsh(pane, "echo hello && pwd")

2. Python REPL (enter and exit):
   xsh(pane, "python3", read_after=2)   # Start Python
   xpy(pane, "print(1+1)")              # Run Python code
   xpy(pane, "exit()", read_after=1)    # Exit Python

3. OpenROAD/TCL (enter and exit):
   xsh(pane, "openroad", read_after=2)  # Start OpenROAD
   xtcl(pane, "puts hello")             # Run TCL code
   xtcl(pane, "exit", read_after=1)     # Exit OpenROAD

4. Interrupt/kill running process:
   send_keys(pane, "C-c C-c C-c")   # Send Ctrl+C multiple times

Module layout:
   config.py     deny-list, instructions, shared paths
   tmux.py       tmux primitives (run, capture, sessions/windows)
   codec.py      markers, code delivery (keystroke-only, ssh-safe), extraction
   filters.py    output filtering (tqdm/grep/dedupe/save)
   tasks.py      background task registry, pane locks
   registry.py   pane/session registration, ownership, cleanup
   watch_cli.py  standalone watch CLI + poll fingerprints
   server.py     MCP tool definitions, entry point
"""
from .server import main

__all__ = ["main"]
