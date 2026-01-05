#!/usr/bin/env python3
"""
MCP Server for tmux command injection (xpy/xtcl/xsh functionality)
Sends commands to Python REPL, TCL-based EDA tools, or shell running in tmux panes.
Supports both blocking and non-blocking (background) execution.

IMPORTANT: Panes must be registered before use with set_working_pane().

USAGE PATTERNS:

0. Register pane first (REQUIRED):
   set_working_pane("t1:1.0", "OpenROAD Python REPL")
   get_working_panes()  # Check registered panes

1. Shell commands (bash pane):
   xsh(pane, "echo hello && pwd")

2. Python REPL (enter and exit):
   xsh_peek(pane, "python3")     # Start Python
   xpy(pane, "print(1+1)")       # Run Python code
   xpy_peek(pane, "exit()")      # Exit Python

3. OpenROAD/TCL (enter and exit):
   xsh_peek(pane, "openroad")    # Start OpenROAD
   xtcl(pane, "puts hello")      # Run TCL code
   xtcl_peek(pane, "exit")       # Exit OpenROAD

4. Interrupt/kill running process:
   send_keys(pane, "C-c C-c C-c")   # Send Ctrl+C multiple times

IMPORTANT:
- Panes MUST be registered with set_working_pane() before use
- If you get "Pane not registered" error, run get_working_panes() to check
- Context may be lost after compaction - always verify with get_working_panes()
"""

import subprocess
import time
import random
import os
import re
import threading
import asyncio
from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = """
This describes how you use tmux-injector tools. This is how the system works.

==========
0. PANE REGISTRATION (REQUIRED)
==========

All panes MUST be registered before use. This prevents accidents after conversation compaction.

Register a pane:
    set_working_pane("t1:1.0", "OpenROAD Python REPL")
    set_working_pane("t1:1.1", "bash for git")

Check registered panes:
    get_working_panes()  # Shows all registered panes with descriptions

Remove a pane:
    remove_working_pane("t1:1.1")

If you get "Pane not registered" error:
    1. Context may have been lost due to compaction
    2. Run get_working_panes() to see what's registered
    3. Ask user for permission before registering new panes

==========
1. TOOL SELECTION
==========

The pane has a current state: bash prompt ($), Python REPL (>>>), TCL interpreter (%), or running process.
The tool you use depends on this state and what you want to do.

CASE A: Short command that completes and returns to prompt
Tool: xsh, xpy, or xtcl (blocking)
Example: xsh(pane, "ls -la") → waits for completion, returns output

CASE B: Long-running task that eventually completes
Tool: xsh_start, xpy_start, or xtcl_start (non-blocking)
Then: task_status(task_id) for status, task_output(task_id) for output, task_wait(task_id) to block
Example: xsh_start(pane, "make build") → returns task_id immediately

CASE C: Starting a process or interpreter (no end marker expected)
Tool: xsh_peek, xpy_peek, or xtcl_peek
Example: xsh_peek(pane, "python3", wait=1) → starts Python, captures prompt
Example: xsh_peek(pane, "openroad -python train.py", wait=2) → starts long process, captures initial output
Example: xpy_peek(pane, "exit()", wait=1) → exits interpreter (no end marker)

CASE D: Responding to a prompt (password, yes/no)
Tool: send_text
Example: send_text(pane, "mypassword") → sends password, presses Enter

CASE E: Sending special keys (Ctrl+C, arrows, Escape)
Tool: send_keys
Example: send_keys(pane, "C-c C-c C-c") → sends Ctrl+C three times

==========
2. CHECKING PANE STATE
==========

Before choosing a tool, check what is running in the pane.
Tool: capture_pane(pane, lines=5)

If you see "$" or bash prompt: use xsh variants
If you see ">>>" or Python prompt: use xpy variants
If you see "%" or TCL prompt: use xtcl variants
If you see running output: use capture_pane to monitor, or send_keys("C-c") to interrupt

==========
3. COMMON PATTERNS
==========

Starting an interpreter and running code:
    xsh_peek(pane, "python3", wait=1)      # start interpreter
    xpy(pane, "print('hello')")            # run code (blocking)
    xpy_peek(pane, "exit()", wait=1)       # exit interpreter

Starting a long-running script (with monitoring):
    xsh_peek(pane, "python3", wait=1)      # start interpreter
    xpy_start(pane, file="train.py")       # returns task_id
    task_status(task_id)                   # "[running] 30s" - quick status check
    task_output(task_id, lines=10)         # last 10 lines of output
    task_output(task_id, grep="Episode")   # filter for Episode lines
    task_wait(task_id, timeout=300)        # block until complete
    # file= parameter converts to: exec(open('/absolute/path/train.py').read())

Starting a long-running process (without monitoring):
    xsh_peek(pane, "python3 train.py", wait=2)  # no task_id
    capture_pane(pane, lines=30)                # manual check only

SSH connection:
    xsh_peek(pane, "ssh user@host", wait=2)  # connect
    send_text(pane, "password")              # if password prompt appears
    capture_pane(pane)                       # verify login
    xsh(pane, "hostname")                    # run commands
    xsh_peek(pane, "exit", wait=1)           # disconnect

Interrupting a process:
    send_keys(pane, "C-c C-c C-c")         # Ctrl+C multiple times
    capture_pane(pane)                      # verify interrupted

Passing variables from REPL to script (globals trick):
    # In REPL, define variables before running script:
    xpy(pane, "model_path = '/path/to/model'")
    xpy(pane, "learning_rate = 0.001")
    xpy_start(pane, file="train.py")       # script can access these variables

    # In train.py, use globals().get() to read REPL variables with defaults:
    # model_path = globals().get('model_path', None)
    # learning_rate = globals().get('learning_rate', 0.01)
    # if model_path: agent.load(model_path)

    # This works because file= uses exec() which shares REPL's globals()

==========
4. WHAT EACH TOOL RETURNS
==========

set_working_pane: "Registered: t1:1.0 (description)"
get_working_panes: List of registered panes with descriptions
remove_working_pane: "Removed: t1:1.0"
xsh/xpy/xtcl: Command output (waits for completion)
xsh_start/xpy_start/xtcl_start: Task ID string
xsh_peek/xpy_peek/xtcl_peek: Output captured after wait seconds
task_status: "[running] Xs" or "[completed] Xs" (status only, lightweight)
task_output: Current output (use lines=N for last N, grep="pattern" for filtering)
task_wait: "[completed] Xs" (blocks until done)
send_text: "Text sent"
send_keys: "Keys sent"
capture_pane: Current pane content (last N lines)

==========
5. ABORT/TIMEOUT DOES NOT MEAN "COMMAND NOT SENT" (CRITICAL)
==========

When xtcl/xpy/xsh returns abort or timeout, the command WAS ALREADY SENT.
The error means "couldn't receive end marker", not "command failed to send".

DANGER CASE: exit commands with blocking tool
    xtcl(pane, "exit")  # WRONG: exit has no end marker, use peek!
                        # Innovus terminates, now bash, no TCL prompt → abort
    xtcl_peek(pane, "exit")  # WRONG: sends exit AGAIN to bash → logout!

CORRECT: Use peek for exit commands (as documented in CASE C above)
    xtcl_peek(pane, "exit")  # Right tool for exit

IF YOU ALREADY MADE THE MISTAKE (used blocking tool for exit):
    xtcl(pane, "exit")  # abort/timeout
    capture_pane(pane)  # MUST check state before doing anything
    # If bash ($), the exit worked. Don't send another exit.

WHY: Blocking tools (xtcl/xpy/xsh) do two things:
    1. Send command to tmux (always succeeds if pane exists)
    2. Wait for end marker (can fail/timeout)

Abort only affects step 2. Step 1 already happened.

==========
6. STATE TRACKING
==========

DO NOT call capture_pane before every command. Track pane state in your memory.

State changes to remember:
- xsh_peek(pane, "python3") → pane is now Python REPL (>>>)
- xpy_peek(pane, "exit()") → pane is now bash ($)
- xsh_peek(pane, "openroad") → pane is now TCL (%)
- xtcl_peek(pane, "exit") → pane is now bash ($)
- send_keys("C-c C-c") → uncertain, use capture_pane to confirm

Only use capture_pane when:
- First time accessing a pane in this conversation
- After Ctrl+C interrupt (state is uncertain)
- After abort/timeout from blocking tools (state is uncertain)
- User explicitly asks to check pane state

WRONG: capture_pane before every xpy/xsh call (wastes tokens)
RIGHT: Remember "I started python3 earlier" → use xpy until I see exit

==========
7. CONVERSATION CONTINUITY (CRITICAL)
==========

Registered panes persist in MCP server memory across conversation compaction.
If you get "Pane not registered" error after compaction:
    1. Run get_working_panes() to see what's still registered
    2. The pane info is NOT lost - it's in the server
    3. Just use the registered panes shown

If no panes are registered (new session or server restart):
    1. Ask user which pane to use
    2. Register with set_working_pane() before proceeding
"""

mcp = FastMCP("tmux-injector", instructions=INSTRUCTIONS)

# Background tasks storage: task_id -> task_info
_tasks: dict[str, dict] = {}

# Pane locks: pane -> Lock (prevents concurrent execution on same pane)
_pane_locks: dict[str, threading.Lock] = {}
_pane_locks_lock = threading.Lock()  # Lock for accessing _pane_locks dict

# Registered working panes: pane -> description
_working_panes: dict[str, str] = {}


def get_registered_panes_message() -> str:
    """Format registered panes for error message."""
    if not _working_panes:
        return "No panes registered."
    lines = ["Registered panes:"]
    for pane, desc in _working_panes.items():
        lines.append(f"  {pane}: {desc}")
    return '\n'.join(lines)


def check_pane_registered(pane: str) -> None:
    """Raise error if pane is not registered."""
    if pane not in _working_panes:
        msg = f"""Pane '{pane}' is not registered.

{get_registered_panes_message()}

Context may have been lost due to compaction.
Run get_working_panes() to check registered panes.
Ask user for permission before registering with set_working_pane()."""
        raise ValueError(msg)


def get_pane_lock(pane: str) -> threading.Lock:
    """Get or create a lock for a specific pane."""
    with _pane_locks_lock:
        if pane not in _pane_locks:
            _pane_locks[pane] = threading.Lock()
        return _pane_locks[pane]


def is_pane_busy(pane: str) -> bool:
    """Check if pane has a running task."""
    lock = get_pane_lock(pane)
    if lock.locked():
        return True
    return False


def run_tmux_cmd(args: list[str], capture: bool = True) -> str:
    """Run a tmux command and return output."""
    result = subprocess.run(
        ["tmux"] + args,
        capture_output=capture,
        text=True
    )
    return result.stdout if capture else ""


def check_session(session: str) -> bool:
    """Check if tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True
    )
    return result.returncode == 0


def generate_marker() -> tuple[str, str]:
    """Generate unique begin/end markers."""
    ts = format(int(time.time()), 'x')
    rnd = format(random.randint(0, 0xFFFF), 'x')
    marker = f"_X{ts}_{rnd}_"
    return f"{marker}B_", f"{marker}E_"


def generate_task_id() -> str:
    """Generate unique task ID."""
    ts = format(int(time.time()), 'x')
    rnd = format(random.randint(0, 0xFFFF), 'x')
    return f"T{ts}_{rnd}"


def send_python_code(session: str, code: str, begin: str, end: str) -> None:
    """Send Python code wrapped in try/except with markers.

    Uses single-line exec approach for compatibility with both Python REPL and innovus_py.
    """
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)

    # Escape backslashes and single quotes
    code_escaped = code.replace('\\', '\\\\').replace("'", "\\'")

    # For display: join lines with \n (will be interpreted as newline by Python)
    code_display = code_escaped.replace('\n', '\\n')

    # For exec: indent each line by 4 spaces, join with \n
    code_indented = '\\n'.join('    ' + line for line in code_escaped.split('\n'))

    # 1. Code preview (before BEGIN, for readability)
    run_tmux_cmd(["send-keys", "-t", session, f"print(); print('{code_display}'); print()"], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)

    # 2. Single-line execution with try/except
    exec_cmd = f"print('{begin}'); print(); exec('try:\\n{code_indented}\\nexcept: __import__(\\'traceback\\').print_exc()'); print(); print('{end}')"
    run_tmux_cmd(["send-keys", "-t", session, exec_cmd], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)


def send_tcl_code(session: str, code: str, begin: str, end: str) -> None:
    """Send TCL code with markers."""
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)

    tcl_cmd = f'puts "{begin}"; if {{[catch {{\n\n{code}\n\n}} err]}} {{puts $err}}; puts "{end}"'
    run_tmux_cmd(["send-keys", "-t", session, tcl_cmd], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)


def send_shell_code(session: str, code: str, begin: str, end: str) -> None:
    """Send shell code with markers."""
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)

    run_tmux_cmd(["send-keys", "-t", session, f"echo '{begin}'; {{"], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)

    for line in code.split('\n'):
        run_tmux_cmd(["send-keys", "-t", session, line], capture=False)
        run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)

    run_tmux_cmd(["send-keys", "-t", session, f"}} 2>&1; echo '{end}'"], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)


def extract_output(raw: str, begin: str, end: str) -> tuple[str, bool]:
    """Extract output between markers. Returns (output, is_complete)."""
    lines = raw.split('\n')
    capturing = False
    result = []
    completed = False

    for line in lines:
        if line == begin:
            capturing = True
            continue
        if line == end:
            completed = True
            break
        if capturing:
            result.append(line)

    return '\n'.join(result), completed


def parse_rel_range(rel_range: str) -> tuple[int, int]:
    """Parse relative range string like '100:50' or '-100:-50'.
    Always returns negative indices (from end)."""
    parts = rel_range.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid rel_range format: {rel_range}. Use 'START:END' (e.g., '100:50')")

    start_str, end_str = parts
    start = -abs(int(start_str)) if start_str.strip() else None
    end = -abs(int(end_str)) if end_str.strip() else None
    return start, end


def apply_dedupe(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate lines."""
    if not lines:
        return lines
    result = [lines[0]]
    for line in lines[1:]:
        if line != result[-1]:
            result.append(line)
    return result


def apply_grep_with_context(lines: list[str], pattern: re.Pattern, before: int = 0, after: int = 0) -> list[str]:
    """Apply grep with context lines (like grep -B/-A)."""
    if before <= 0 and after <= 0:
        return [line for line in lines if pattern.search(line)]

    matches = set()
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, i - before)
            end = min(len(lines), i + after + 1)
            for j in range(start, end):
                matches.add(j)

    return [lines[i] for i in sorted(matches)]


def save_to_file(content: str, file_path: str, append: bool) -> str:
    """Save content to file."""
    mode = 'a' if append else 'w'
    with open(os.path.expanduser(file_path), mode) as f:
        f.write(content)
        if not content.endswith('\n'):
            f.write('\n')
    return file_path


def check_task_output(session: str, begin: str, end: str) -> tuple[str, bool]:
    """Check current output for a task. Returns (output, is_complete)."""
    raw = run_tmux_cmd(["capture-pane", "-t", session, "-p", "-S", "-"])
    return extract_output(raw, begin, end)


async def capture_output_blocking(session: str, begin: str, end: str, timeout: float) -> str:
    """Capture output between markers (blocking)."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        await asyncio.sleep(0.1)
        output, completed = check_task_output(session, begin, end)
        if completed:
            return output

    raise TimeoutError(f"Command did not complete within {timeout}s")


def list_sessions() -> list[dict]:
    """List available tmux sessions."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}:#{session_windows}:#{session_attached}"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return []

    sessions = []
    for line in result.stdout.strip().split('\n'):
        if line:
            parts = line.split(':')
            sessions.append({
                "name": parts[0],
                "windows": int(parts[1]) if len(parts) > 1 else 0,
                "attached": parts[2] == "1" if len(parts) > 2 else False
            })
    return sessions


# =============================================================================
# Blocking tools (original)
# =============================================================================

@mcp.tool()
async def xpy(
    pane: str,
    code: str = None,
    file: str = None,
    timeout: float = 60.0
) -> str:
    """Execute Python code in tmux (blocking). Waits for completion.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Python code to execute
        file: Python file to execute (alternative to code)
        timeout: Timeout in seconds (default: 60)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    if file:
        abs_path = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(open('{abs_path}').read())"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    if "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, end = generate_marker()
        send_python_code(pane, code, begin, end)
        return await capture_output_blocking(pane, begin, end, timeout)
    finally:
        lock.release()


@mcp.tool()
async def xtcl(pane: str, code: str, timeout: float = 60.0) -> str:
    """Execute TCL code in tmux (blocking). For EDA tools like Innovus, OpenROAD.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        timeout: Timeout in seconds (default: 60)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, end = generate_marker()
        send_tcl_code(pane, code, begin, end)
        return await capture_output_blocking(pane, begin, end, timeout)
    finally:
        lock.release()


@mcp.tool()
async def xsh(
    pane: str,
    code: str = None,
    file: str = None,
    timeout: float = 60.0
) -> str:
    """Execute shell command in tmux (blocking). Waits for completion.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        file: Shell script file to execute (alternative to code)
        timeout: Timeout in seconds (default: 60)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    if file:
        abs_path = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, end = generate_marker()
        send_shell_code(pane, code, begin, end)
        return await capture_output_blocking(pane, begin, end, timeout)
    finally:
        lock.release()


# =============================================================================
# Non-blocking tools (background execution)
# =============================================================================

@mcp.tool()
def xpy_start(
    pane: str,
    code: str = None,
    file: str = None
) -> str:
    """Start Python code execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Python code to execute
        file: Python file to execute
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    if file:
        abs_path = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(open('{abs_path}').read())"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    if "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    task_id = generate_task_id()
    begin, end = generate_marker()

    send_python_code(pane, code, begin, end)

    _tasks[task_id] = {
        "pane": pane,
        "begin": begin,
        "end": end,
        "start_time": time.time(),
        "type": "python",
        "lock": lock
    }

    return f"Started task {task_id} on {pane}"


@mcp.tool()
def xtcl_start(pane: str, code: str) -> str:
    """Start TCL code execution (non-blocking). Returns task_id immediately.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: TCL code to execute
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    task_id = generate_task_id()
    begin, end = generate_marker()

    send_tcl_code(pane, code, begin, end)

    _tasks[task_id] = {
        "pane": pane,
        "begin": begin,
        "end": end,
        "start_time": time.time(),
        "type": "tcl",
        "lock": lock
    }

    return f"Started task {task_id} on {pane}"


@mcp.tool()
def xsh_start(
    pane: str,
    code: str = None,
    file: str = None
) -> str:
    """Start shell command execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        file: Shell script file to execute
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    if file:
        abs_path = os.path.abspath(os.path.expanduser(file))
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    task_id = generate_task_id()
    begin, end = generate_marker()

    send_shell_code(pane, code, begin, end)

    _tasks[task_id] = {
        "pane": pane,
        "begin": begin,
        "end": end,
        "start_time": time.time(),
        "type": "shell",
        "lock": lock
    }

    return f"Started task {task_id} on {pane}"


@mcp.tool()
def task_status(task_id: str) -> str:
    """Check task status (non-blocking). Returns status and elapsed time only.

    Args:
        task_id: Task ID from xpy_start, xtcl_start, or xsh_start
    """
    if task_id not in _tasks:
        raise ValueError(f"Task '{task_id}' not found")

    task = _tasks[task_id]
    _, completed = check_task_output(task["pane"], task["begin"], task["end"])
    elapsed = time.time() - task["start_time"]

    status = "completed" if completed else "running"

    # Auto-release lock when completed
    if completed and "lock" in task:
        task["lock"].release()
        del task["lock"]

    return f"[{status}] {elapsed:.1f}s"


@mcp.tool()
def task_output(
    task_id: str,
    tail: int = 0,
    head: int = None,
    range: str = None,
    grep: str = None,
    v: str = None,
    i: bool = False,
    w: bool = False,
    F: bool = False,
    m: int = None,
    A: int = None,
    B: int = None,
    C: int = None,
    n: bool = False,
    uniq: bool = True,
    save: str = None,
    append: bool = True
) -> str:
    """Get task output (non-blocking).

    Args:
        task_id: Task ID from xpy_start, xtcl_start, or xsh_start
        tail: If > 0, return only the last N lines (default: 0 = all)
        head: If provided, return only the first N lines
        range: Line range, e.g., "10:20" (0-indexed, within marker output)
        grep: Filter lines matching this regex pattern
        v: Exclude lines matching this regex pattern (like grep -v)
        i: Case insensitive matching (like grep -i)
        w: Word match - pattern must match whole word (like grep -w)
        F: Fixed string - treat pattern as literal, not regex (like grep -F)
        m: Max count - return at most N matching lines (like grep -m)
        A: Lines after grep match (like grep -A)
        B: Lines before grep match (like grep -B)
        C: Lines before and after grep match (like grep -C)
        n: Show line numbers (like grep -n)
        uniq: Remove consecutive duplicate lines (like uniq, default: True)
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
    """
    if task_id not in _tasks:
        raise ValueError(f"Task '{task_id}' not found")

    task = _tasks[task_id]
    output, completed = check_task_output(task["pane"], task["begin"], task["end"])

    # Auto-release lock when completed
    if completed and "lock" in task:
        task["lock"].release()
        del task["lock"]

    if not output:
        return output

    all_lines = output.split('\n')

    # Apply range, head, or tail
    if range:
        parts = range.split(":")
        start = int(parts[0]) if parts[0].strip() else 0
        end = int(parts[1]) if parts[1].strip() else len(all_lines)
        all_lines = all_lines[start:end]
    elif head is not None and head > 0:
        all_lines = all_lines[:head]
    elif tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]

    # Build regex flags
    flags = re.IGNORECASE if i else 0

    # Apply grep filter with context (A/B/C)
    if grep:
        pat = re.escape(grep) if F else grep
        pat = rf'\b{pat}\b' if w else pat
        pattern = re.compile(pat, flags)
        before = B if B is not None else (C or 0)
        after = A if A is not None else (C or 0)
        all_lines = apply_grep_with_context(all_lines, pattern, before, after)
        if m is not None and m > 0:
            all_lines = all_lines[:m]

    # Apply exclude filter (grep -v)
    if v:
        pat_v = re.escape(v) if F else v
        pat_v = rf'\b{pat_v}\b' if w else pat_v
        exc_pattern = re.compile(pat_v, flags)
        all_lines = [line for line in all_lines if not exc_pattern.search(line)]

    # Apply uniq
    if uniq:
        all_lines = apply_dedupe(all_lines)

    # Apply line numbers (grep -n)
    if n:
        all_lines = [f"{i}: {line}" for i, line in enumerate(all_lines)]

    result = '\n'.join(all_lines)

    # Save to file if requested
    if save:
        save_to_file(result, save, append)

    return result


@mcp.tool()
async def task_wait(task_id: str, timeout: float = 60.0) -> str:
    """Wait for task completion (blocking). Returns status only.

    Args:
        task_id: Task ID from xpy_start, xtcl_start, or xsh_start
        timeout: Max wait time in seconds
    """
    if task_id not in _tasks:
        raise ValueError(f"Task '{task_id}' not found")

    task = _tasks[task_id]
    start_time = time.time()

    while time.time() - start_time < timeout:
        await asyncio.sleep(0.1)
        _, completed = check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            # Release lock
            if "lock" in task:
                task["lock"].release()
                del task["lock"]
            elapsed = time.time() - task["start_time"]
            return f"[completed] {elapsed:.1f}s"

    raise TimeoutError(f"Task did not complete within {timeout}s")


@mcp.tool()
def task_list() -> str:
    """List all running background tasks."""
    if not _tasks:
        return "No running tasks"

    lines = ["Running tasks:"]
    for task_id, task in _tasks.items():
        elapsed = time.time() - task["start_time"]
        output, completed = check_task_output(task["pane"], task["begin"], task["end"])
        status = "completed" if completed else "running"
        lines.append(f"  {task_id}: {task['type']} on {task['pane']} [{status}] {elapsed:.1f}s")

    return '\n'.join(lines)


@mcp.tool()
def task_cancel(task_id: str) -> str:
    """Cancel/forget a background task (does not stop execution in tmux).

    Args:
        task_id: Task ID to cancel
    """
    if task_id not in _tasks:
        raise ValueError(f"Task '{task_id}' not found")

    task = _tasks[task_id]
    # Release lock
    if "lock" in task:
        task["lock"].release()
    del _tasks[task_id]
    return f"Task {task_id} removed"


# =============================================================================
# Utility tools
# =============================================================================

@mcp.tool()
def tmux_sessions() -> str:
    """List available tmux sessions."""
    sessions = list_sessions()

    if not sessions:
        return "No tmux sessions found"

    lines = ["Available tmux sessions:"]
    for s in sessions:
        status = "attached" if s["attached"] else "detached"
        lines.append(f"  {s['name']} ({s['windows']} windows, {status})")

    return '\n'.join(lines)


@mcp.tool()
def set_working_pane(pane: str, description: str) -> str:
    """Register a pane for use. Must be called before using other tools.

    Args:
        pane: tmux target (e.g., t1:1.0, t1:2.1)
        description: What this pane is for (e.g., "OpenROAD Python REPL")
    """
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    _working_panes[pane] = description
    return f"Registered: {pane} ({description})"


@mcp.tool()
def get_working_panes() -> str:
    """Get all registered working panes."""
    if not _working_panes:
        return "No panes registered. Use set_working_pane() to register."

    lines = ["Registered panes:"]
    for pane, desc in _working_panes.items():
        lines.append(f"  {pane}: {desc}")
    return '\n'.join(lines)


@mcp.tool()
def remove_working_pane(pane: str) -> str:
    """Unregister a working pane.

    Args:
        pane: tmux target to unregister
    """
    if pane not in _working_panes:
        raise ValueError(f"Pane '{pane}' is not registered")

    del _working_panes[pane]
    return f"Removed: {pane}"


async def peek_output(pane: str, begin: str, wait: float) -> str:
    """Wait and capture output after begin marker."""
    await asyncio.sleep(wait)

    raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-S", "-"])
    lines = raw.split('\n')

    capturing = False
    result = []
    for line in lines:
        if line == begin:
            capturing = True
            continue
        if capturing:
            result.append(line)

    return '\n'.join(result).rstrip()


@mcp.tool()
async def xpy_peek(
    pane: str,
    code: str,
    wait: float = 1.0
) -> str:
    """Execute Python code and capture output for a short time (no end marker wait).

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Python code to execute
        wait: Seconds to wait before capturing (default: 1.0)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, _ = generate_marker()

        run_tmux_cmd(["send-keys", "-t", pane, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, f'print("{begin}")'], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

        for line in code.split('\n'):
            run_tmux_cmd(["send-keys", "-t", pane, line], capture=False)
            run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

        return await peek_output(pane, begin, wait)
    finally:
        lock.release()


@mcp.tool()
async def xtcl_peek(
    pane: str,
    code: str,
    wait: float = 1.0
) -> str:
    """Execute TCL code and capture output for a short time (no end marker wait).

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        wait: Seconds to wait before capturing (default: 1.0)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, _ = generate_marker()

        run_tmux_cmd(["send-keys", "-t", pane, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, f'puts "{begin}"'], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, code], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

        return await peek_output(pane, begin, wait)
    finally:
        lock.release()


@mcp.tool()
async def xsh_peek(
    pane: str,
    code: str,
    wait: float = 1.0
) -> str:
    """Execute shell command and capture output for a short time (no end marker wait).

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        wait: Seconds to wait before capturing (default: 1.0)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    lock = get_pane_lock(pane)
    if not lock.acquire(blocking=False):
        raise RuntimeError(f"Pane '{pane}' is busy with another task")

    try:
        begin, _ = generate_marker()

        run_tmux_cmd(["send-keys", "-t", pane, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, f"echo '{begin}'"], capture=False)
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

        for line in code.split('\n'):
            run_tmux_cmd(["send-keys", "-t", pane, line], capture=False)
            run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

        return await peek_output(pane, begin, wait)
    finally:
        lock.release()


@mcp.tool()
def send_text(pane: str, text: str, enter: bool = True) -> str:
    """Send text string to pane. For commands, passwords, etc.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        text: Text to send as-is
        enter: Press Enter after text (default: True)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    run_tmux_cmd(["send-keys", "-t", pane, text], capture=False)
    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

    return "Text sent"


@mcp.tool()
def send_keys(pane: str, keys: str, enter: bool = False) -> str:
    """Send special keys to pane. For C-c, Enter, Escape, arrow keys, etc.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        keys: Space-separated special keys (e.g., "C-c C-c C-c", "Up Up Enter")
        enter: Press Enter after keys (default: False)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    for key in keys.split():
        run_tmux_cmd(["send-keys", "-t", pane, key], capture=False)

    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

    return "Keys sent"


@mcp.tool()
def capture_pane(
    pane: str,
    tail: int = 5,
    rel_range: str = None,
    grep: str = None,
    v: str = None,
    i: bool = False,
    w: bool = False,
    F: bool = False,
    m: int = None,
    A: int = None,
    B: int = None,
    C: int = None,
    since_marker: str = None,
    uniq: bool = True,
    n: bool = False,
    save: str = None,
    append: bool = True
) -> str:
    """Capture current pane content.

    Args:
        pane: Registered tmux pane (e.g., t1:1.0)
        tail: Number of lines to capture from end (default: 5)
        rel_range: Relative range from end, e.g., "100:50" (100th from end to 50th from end)
        grep: Filter lines matching this regex pattern
        v: Exclude lines matching this regex pattern (like grep -v)
        i: Case insensitive matching (like grep -i)
        w: Word match - pattern must match whole word (like grep -w)
        F: Fixed string - treat pattern as literal, not regex (like grep -F)
        m: Max count - return at most N matching lines (like grep -m)
        A: Lines after grep match (like grep -A)
        B: Lines before grep match (like grep -B)
        C: Lines before and after grep match (like grep -C)
        since_marker: Only capture lines after this marker
        uniq: Remove consecutive duplicate lines (like uniq, default: True)
        n: Show line numbers as negative indices from end (like nl/cat -n)
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
    """
    check_pane_registered(pane)

    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-S", "-"])
    all_lines = raw.rstrip().split('\n')

    # Apply since_marker filter
    if since_marker:
        marker_idx = None
        for i, line in enumerate(all_lines):
            if since_marker in line:
                marker_idx = i
        if marker_idx is not None:
            all_lines = all_lines[marker_idx + 1:]

    # Apply rel_range or tail
    if rel_range:
        start, end = parse_rel_range(rel_range)
        all_lines = all_lines[start:end]
    elif tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]

    # Build regex flags
    flags = re.IGNORECASE if i else 0

    # Apply grep filter with context (A/B/C)
    if grep:
        pat = re.escape(grep) if F else grep
        pat = rf'\b{pat}\b' if w else pat
        pattern = re.compile(pat, flags)
        before = B if B is not None else (C or 0)
        after = A if A is not None else (C or 0)
        all_lines = apply_grep_with_context(all_lines, pattern, before, after)
        if m is not None and m > 0:
            all_lines = all_lines[:m]

    # Apply exclude filter (grep -v)
    if v:
        pat_v = re.escape(v) if F else v
        pat_v = rf'\b{pat_v}\b' if w else pat_v
        exc_pattern = re.compile(pat_v, flags)
        all_lines = [line for line in all_lines if not exc_pattern.search(line)]

    # Apply uniq
    if uniq:
        all_lines = apply_dedupe(all_lines)

    # Apply line numbers (negative, from end)
    if n:
        total = len(all_lines)
        all_lines = [f"{i - total}: {line}" for i, line in enumerate(all_lines)]

    result = '\n'.join(all_lines)

    # Save to file if requested
    if save:
        save_to_file(result, save, append)

    return result


@mcp.tool()
def task_cancel_all() -> str:
    """Cancel all running background tasks."""
    if not _tasks:
        return "No tasks to cancel"

    count = len(_tasks)
    for task_id in list(_tasks.keys()):
        task = _tasks[task_id]
        if "lock" in task:
            task["lock"].release()
        del _tasks[task_id]

    return f"Cancelled {count} task(s)"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
