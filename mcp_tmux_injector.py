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
from mcp.server.fastmcp import FastMCP, Context
from pathlib import Path
from urllib.parse import urlparse

# tqdm progress bar detection patterns
_TQDM_INDICATOR = re.compile(r'\d+%\|')
_TQDM_FRAC = re.compile(r'\|\s*\d+/\d+')
_TQDM_SPEED = re.compile(r'(?:it|s)/(?:s|it)[\]\s]')
_TQDM_BLOCK_ONLY = re.compile(r'^[\s█▏▎▍▌▋▊▉]+$')

_INSTRUCTIONS_FILE = Path(__file__).parent / "instructions.txt"
INSTRUCTIONS = _INSTRUCTIONS_FILE.read_text() if _INSTRUCTIONS_FILE.exists() else ""

mcp = FastMCP("tmux-injector", instructions=INSTRUCTIONS)

# Background tasks storage: task_id -> task_info
_tasks: dict[str, dict] = {}

# Pane locks: pane -> Lock (prevents concurrent execution on same pane)
_pane_locks: dict[str, threading.Lock] = {}
_pane_locks_lock = threading.Lock()  # Lock for accessing _pane_locks dict

# Registered working panes: pane -> description
_working_panes: dict[str, str] = {}

# Client cwd from roots/list (cached after first query)
_client_cwd: str | None = None


async def _get_client_cwd(ctx: Context) -> str | None:
    """Get client's working directory via roots/list, with caching."""
    global _client_cwd
    if _client_cwd is not None:
        return _client_cwd
    try:
        result = await ctx.session.list_roots()
        if result.roots:
            _client_cwd = urlparse(str(result.roots[0].uri)).path
    except Exception:
        pass
    return _client_cwd


def _resolve_file_path(file: str, client_cwd: str | None = None) -> str:
    """Resolve file path using client's cwd for relative paths."""
    expanded = os.path.expanduser(file)
    if os.path.isabs(expanded):
        return expanded
    # Relative path: use client cwd if available
    if client_cwd:
        return os.path.join(client_cwd, expanded)
    return os.path.abspath(expanded)


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
        if _working_panes:
            # Registered panes exist - suggest using one of them
            lines = [f"Pane '{pane}' is not registered.", ""]
            lines.append("Available panes (use one of these):")
            for p, desc in _working_panes.items():
                lines.append(f"  {p}: {desc}")
            lines.append("")
            pane_names = list(_working_panes.keys())
            if len(pane_names) == 1:
                lines.append(f"Hint: Use '{pane_names[0]}' instead.")
            else:
                suggestions = "' or '".join(pane_names[:3])
                lines.append(f"Hint: Did you mean '{suggestions}'?")
            msg = '\n'.join(lines)
        else:
            # No panes registered - ask user
            msg = f"""Pane '{pane}' is not registered.

No panes registered.

Context may have been lost due to compaction.
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


def acquire_pane_lock(pane: str) -> threading.Lock:
    """Acquire pane lock. If busy, check if existing task completed first."""
    lock = get_pane_lock(pane)
    if lock.acquire(blocking=False):
        return lock

    # Check if existing task is actually completed
    for task_id, task in _tasks.items():
        if task['pane'] == pane and 'lock' in task:
            _, completed = check_task_output(pane, task['begin'], task['end'])
            if completed:
                task['lock'].release()
                del task['lock']
                if lock.acquire(blocking=False):
                    return lock

    raise RuntimeError(f"Pane '{pane}' is busy with another task")


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


def generate_task_id_and_marker() -> tuple[str, str, str]:
    """Generate unique task ID and matching markers (same ID for debugging)."""
    ts = format(int(time.time()), 'x')
    rnd = format(random.randint(0, 0xFFFF), 'x')
    task_id = f"T{ts}_{rnd}"
    marker = f"_X{ts}_{rnd}_"
    return task_id, f"{marker}B_", f"{marker}E_"


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
    time.sleep(0.05)  # Ensure tmux processes Enter before next command


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


def _is_tqdm_line(line: str) -> bool:
    """Check if a line is part of tqdm progress output."""
    if _TQDM_INDICATOR.search(line):
        return True
    if _TQDM_FRAC.search(line):
        return True
    if _TQDM_SPEED.search(line):
        return True
    if line.strip() and _TQDM_BLOCK_ONLY.match(line):
        return True
    return False


def _trim_tqdm_group(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """Find the last progress update within a tqdm group.

    In narrow panes, each tqdm update wraps across multiple lines.
    Only the final update (e.g., 100%) matters.
    A new update starts at a line matching _TQDM_INDICATOR (\\d+%\\|).
    """
    last_update_start = start
    for idx in range(start, end):
        if _TQDM_INDICATOR.search(lines[idx]):
            last_update_start = idx
    return last_update_start, end


def _filter_tqdm(lines: list[str]) -> list[str]:
    """Remove tqdm progress lines, keeping only the final state of the last group."""
    is_tqdm = [_is_tqdm_line(line) for line in lines]

    # Find consecutive tqdm groups
    groups = []  # [(start, end), ...]
    group_start = None
    for idx, flag in enumerate(is_tqdm):
        if flag and group_start is None:
            group_start = idx
        elif not flag and group_start is not None:
            groups.append((group_start, idx))
            group_start = None
    if group_start is not None:
        groups.append((group_start, len(lines)))

    if not groups:
        return lines

    # Remove all tqdm groups except the last
    remove = set()
    for start, end in groups[:-1]:
        remove.update(range(start, end))

    # Trim last group to final progress update only
    last_start, last_end = groups[-1]
    trim_start, trim_end = _trim_tqdm_group(lines, last_start, last_end)
    remove.update(range(last_start, trim_start))

    return [line for idx, line in enumerate(lines) if idx not in remove]


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


def _resolve_panes(pane: str | None, panes: list[str] | None) -> list[str] | None:
    """Resolve pane/panes params. Returns None for single mode, list for multi mode."""
    if pane is not None and panes is not None:
        raise ValueError("Use 'pane' or 'panes', not both")
    if panes is not None:
        if not panes:
            raise ValueError("'panes' list is empty")
        for p in panes:
            check_pane_registered(p)
        return panes
    return None


def _format_multi_result(results: dict[str, str]) -> str:
    """Format multi-pane results, grouping panes with identical output."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for pane, output in results.items():
        if output not in groups:
            groups[output] = []
            order.append(output)
        groups[output].append(pane)

    parts = []
    for output in order:
        header = "[" + ", ".join(groups[output]) + "]"
        parts.append(f"{header}\n{output}")

    return "\n\n".join(parts)


def apply_output_filters(
    lines: list[str],
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
    append: bool = True,
    n_negative: bool = False,
    prefix: str = None,
    suffix: str = None,
    strip_tqdm: bool = False
) -> str:
    """Apply common output filters and optionally save to file.

    Args:
        lines: Input lines to filter
        grep: Filter lines matching this regex pattern
        v: Exclude lines matching this regex pattern (like grep -v)
        i: Case insensitive matching (like grep -i)
        w: Word match - pattern must match whole word (like grep -w)
        F: Fixed string - treat pattern as literal, not regex (like grep -F)
        m: Max count - return at most N matching lines (like grep -m)
        A: Lines after grep match (like grep -A)
        B: Lines before grep match (like grep -B)
        C: Lines before and after grep match (like grep -C)
        n: Show line numbers
        uniq: Remove consecutive duplicate lines (like uniq, default: True)
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
        n_negative: If True, show negative line numbers (for capture_pane)
        prefix: Text to prepend when saving (optional)
        suffix: Text to append when saving (optional)
        strip_tqdm: Remove tqdm progress lines, keeping only the last group

    Returns:
        Filtered output as string
    """
    # strip_tqdm: applied before grep
    if strip_tqdm:
        lines = _filter_tqdm(lines)

    # Build regex flags
    flags = re.IGNORECASE if i else 0

    # Apply grep filter with context (A/B/C)
    if grep:
        pat = re.escape(grep) if F else grep
        pat = rf'\b{pat}\b' if w else pat
        pattern = re.compile(pat, flags)
        before = B if B is not None else (C or 0)
        after = A if A is not None else (C or 0)
        lines = apply_grep_with_context(lines, pattern, before, after)
        if m is not None and m > 0:
            lines = lines[:m]

    # Apply exclude filter (grep -v)
    if v:
        pat_v = re.escape(v) if F else v
        pat_v = rf'\b{pat_v}\b' if w else pat_v
        exc_pattern = re.compile(pat_v, flags)
        lines = [line for line in lines if not exc_pattern.search(line)]

    # Apply uniq
    if uniq:
        lines = apply_dedupe(lines)

    # Apply line numbers
    if n:
        if n_negative:
            total = len(lines)
            lines = [f"{idx - total}: {line}" for idx, line in enumerate(lines)]
        else:
            lines = [f"{idx}: {line}" for idx, line in enumerate(lines)]

    result = '\n'.join(lines)

    # Save to file if requested
    if save:
        # Process escape sequences (\n, \t) in prefix/suffix
        eff_prefix = (prefix or '').replace('\\n', '\n').replace('\\t', '\t')
        eff_suffix = (suffix or '').replace('\\n', '\n').replace('\\t', '\t')
        save_content = eff_prefix + result + eff_suffix
        save_to_file(save_content, save, append)
        return f"Saved to {save}"

    return result


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

async def _blocking_on_pane(p: str, code: str, send_fn, timeout: float, filter_kwargs: dict, task_type: str = "shell") -> str:
    """Execute blocking command on a single pane and return filtered output."""
    lock = acquire_pane_lock(p)
    task_id, begin, end = generate_task_id_and_marker()
    start_time = time.time()
    converted = False
    try:
        send_fn(p, code, begin, end)
        output = await capture_output_blocking(p, begin, end, timeout)
        lines = output.split('\n') if output else []
        return apply_output_filters(lines, n_negative=False, **filter_kwargs)
    except TimeoutError:
        _tasks[task_id] = {
            "pane": p, "begin": begin, "end": end,
            "start_time": start_time, "type": task_type,
            "command": code, "lock": lock
        }
        converted = True
        return f"[timeout → task] {task_id}"
    except asyncio.CancelledError:
        _tasks[task_id] = {
            "pane": p, "begin": begin, "end": end,
            "start_time": start_time, "type": task_type,
            "command": code, "lock": lock
        }
        converted = True
        raise
    finally:
        if not converted:
            lock.release()


async def _blocking_multi(target_panes: list[str], code: str, send_fn, timeout: float, fkw: dict, task_type: str = "shell") -> str:
    """Run blocking command on multiple panes with graceful skip for missing panes."""
    results = {}
    coros = {}
    for p in target_panes:
        if not check_session(p):
            results[p] = "not found (skipped)"
        else:
            coros[p] = _blocking_on_pane(p, code, send_fn, timeout, fkw, task_type=task_type)
    if coros:
        results_list = await asyncio.gather(*coros.values(), return_exceptions=True)
        for p, result in zip(coros.keys(), results_list):
            results[p] = str(result) if isinstance(result, Exception) else result
    return _format_multi_result(results)


@mcp.tool()
async def xpy(
    pane: str = None,
    code: str = None,
    file: str = None,
    timeout: float = 60.0,
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
    strip_tqdm: bool = False,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Execute Python code in tmux (blocking). Waits for completion.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Python code to execute
        file: Python file to execute (alternative to code)
        timeout: Timeout in seconds (default: 60)
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
        strip_tqdm: Remove tqdm progress lines, keeping only the last group
        panes: Multiple panes to execute simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if file:
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(open('{abs_path}').read())"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    if "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_python_code, timeout, fkw, task_type="python")

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_python_code, timeout, fkw, task_type="python")


@mcp.tool()
async def xtcl(
    pane: str = None,
    code: str = "",
    timeout: float = 60.0,
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
    strip_tqdm: bool = False,
    panes: list[str] = None
) -> str:
    """Execute TCL code in tmux (blocking). For EDA tools like Innovus, OpenROAD.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        timeout: Timeout in seconds (default: 60)
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
        strip_tqdm: Remove tqdm progress lines, keeping only the last group
        panes: Multiple panes to execute simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if not code:
        raise ValueError("'code' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_tcl_code, timeout, fkw, task_type="tcl")

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_tcl_code, timeout, fkw, task_type="tcl")


@mcp.tool()
async def xsh(
    pane: str = None,
    code: str = None,
    file: str = None,
    timeout: float = 60.0,
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
    strip_tqdm: bool = False,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Execute shell command in tmux (blocking). Waits for completion.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        file: Shell script file to execute (alternative to code)
        timeout: Timeout in seconds (default: 60)
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
        strip_tqdm: Remove tqdm progress lines, keeping only the last group
        panes: Multiple panes to execute simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if file:
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_shell_code, timeout, fkw)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_shell_code, timeout, fkw)


# =============================================================================
# Non-blocking tools (background execution)
# =============================================================================

def _start_on_pane(p: str, code: str, task_type: str, send_fn) -> str:
    """Start a non-blocking task on a single pane. Returns status string."""
    if not check_session(p):
        return f"{p}: not found (skipped)"

    lock = get_pane_lock(p)
    if not lock.acquire(blocking=False):
        return f"{p}: busy (skipped)"

    task_id, begin, end = generate_task_id_and_marker()
    send_fn(p, code, begin, end)

    _tasks[task_id] = {
        "pane": p,
        "begin": begin,
        "end": end,
        "start_time": time.time(),
        "type": task_type,
        "command": code,
        "lock": lock
    }

    return f"{p}: {task_id} (started)"


@mcp.tool()
async def xpy_start(
    pane: str = None,
    code: str = None,
    file: str = None,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Start Python code execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Python code to execute
        file: Python file to execute
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if file:
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(open('{abs_path}').read())"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    if "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    if target_panes is not None:
        results = [_start_on_pane(p, code, "python", send_python_code) for p in target_panes]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_python_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "python", "command": code, "lock": lock
    }
    return f"Started task {task_id} on {pane}"


@mcp.tool()
def xtcl_start(pane: str = None, code: str = "", panes: list[str] = None) -> str:
    """Start TCL code execution (non-blocking). Returns task_id immediately.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if not code:
        raise ValueError("'code' must be provided")

    if target_panes is not None:
        results = [_start_on_pane(p, code, "tcl", send_tcl_code) for p in target_panes]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_tcl_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "tcl", "command": code, "lock": lock
    }
    return f"Started task {task_id} on {pane}"


@mcp.tool()
async def xsh_start(
    pane: str = None,
    code: str = None,
    file: str = None,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Start shell command execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        file: Shell script file to execute
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if file:
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    if target_panes is not None:
        results = [_start_on_pane(p, code, "shell", send_shell_code) for p in target_panes]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_shell_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "shell", "command": code, "lock": lock
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


def _get_single_task_output(
    tid: str, tail: int, head: int, line_range: str,
    save: str, append: bool, prefix: str, suffix: str,
    include_command: bool, command_prefix: str, markdown: bool,
    filter_kwargs: dict
) -> str:
    """Get filtered output for a single task."""
    if tid not in _tasks:
        raise ValueError(f"Task '{tid}' not found")

    task = _tasks[tid]
    output, completed = check_task_output(task["pane"], task["begin"], task["end"])

    if completed and "lock" in task:
        task["lock"].release()
        del task["lock"]

    if not output:
        return output

    all_lines = output.split('\n')

    if line_range:
        parts = line_range.split(":")
        start = int(parts[0]) if parts[0].strip() else 0
        end = int(parts[1]) if parts[1].strip() else len(all_lines)
        all_lines = all_lines[start:end]
    elif head is not None and head > 0:
        all_lines = all_lines[:head]
    elif tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]

    if save and (markdown or include_command):
        lang = task.get("type", "")
        cmd = task.get("command", "")
        if include_command:
            all_lines = [f"{command_prefix}{cmd}"] + all_lines
        if markdown:
            md_prefix = f"```{lang}\n"
            md_suffix = "\n```"
        else:
            md_prefix = ""
            md_suffix = ""
        effective_prefix = (prefix or '') + md_prefix
        effective_suffix = md_suffix + (suffix or '')
    else:
        effective_prefix = prefix
        effective_suffix = suffix

    return apply_output_filters(
        all_lines, n_negative=False,
        save=save, append=append, prefix=effective_prefix, suffix=effective_suffix,
        **filter_kwargs
    )


@mcp.tool()
def task_output(
    task_id: str = None,
    task_ids: list[str] = None,
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
    strip_tqdm: bool = False,
    save: str = None,
    append: bool = True,
    prefix: str = None,
    suffix: str = None,
    include_command: bool = False,
    command_prefix: str = "$ ",
    markdown: bool = False
) -> str:
    """Get task output (non-blocking).

    Args:
        task_id: Single task ID (use task_id OR task_ids, not both)
        task_ids: Multiple task IDs to get output simultaneously
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
        strip_tqdm: Remove tqdm progress lines, keeping only the last group
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
        prefix: Text to prepend before output (when saving)
        suffix: Text to append after output (when saving)
        include_command: If True, prepend command to output (when saving)
        command_prefix: Prefix for include_command (default: "$ "). E.g., "pt_shell> ", ">>> "
        markdown: If True, wrap output in ```lang ... ``` code block (when saving)
    """
    if (task_id is None) == (task_ids is None):
        raise ValueError("Use 'task_id' or 'task_ids', not both")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if task_ids is not None:
        results = {}
        for tid in task_ids:
            results[tid] = _get_single_task_output(
                tid, tail, head, range, save, append, prefix, suffix,
                include_command, command_prefix, markdown, fkw
            )
        return _format_multi_result(results)

    return _get_single_task_output(
        task_id, tail, head, range, save, append, prefix, suffix,
        include_command, command_prefix, markdown, fkw
    )


async def _wait_single_task(task_id: str, timeout: float) -> str:
    """Wait for a single task to complete. Returns status string."""
    task = _tasks[task_id]
    start_time = time.time()

    while time.time() - start_time < timeout:
        await asyncio.sleep(0.1)
        _, completed = check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            if "lock" in task:
                task["lock"].release()
                del task["lock"]
            elapsed = time.time() - task["start_time"]
            return f"[completed] {elapsed:.1f}s"

    return f"[timeout] {timeout}s"


@mcp.tool()
async def task_wait(task_id: str = None, task_ids: list[str] = None, timeout: float = 60.0, race: bool = False) -> str:
    """Wait for task completion (blocking). Returns status only.

    Args:
        task_id: Single task ID (mutually exclusive with task_ids)
        task_ids: Multiple task IDs to wait simultaneously (mutually exclusive with task_id)
        timeout: Max wait time in seconds
        race: If True with task_ids, return when ANY task completes (default: wait for ALL)
    """
    if (task_id is None) == (task_ids is None):
        raise ValueError("Exactly one of task_id or task_ids must be specified")

    if task_id is not None:
        if task_id not in _tasks:
            raise ValueError(f"Task '{task_id}' not found")
        result = await _wait_single_task(task_id, timeout)
        if result.startswith("[timeout]"):
            raise TimeoutError(f"Task did not complete within {timeout}s")
        return result

    not_found = [tid for tid in task_ids if tid not in _tasks]
    if not_found:
        raise ValueError(f"Tasks not found: {', '.join(not_found)}")

    if race:
        async_tasks = {}
        for tid in task_ids:
            async_tasks[asyncio.create_task(_wait_single_task(tid, timeout))] = tid

        done, pending = await asyncio.wait(async_tasks.keys(), return_when=asyncio.FIRST_COMPLETED)

        for t in pending:
            t.cancel()

        lines = []
        for t in done:
            tid = async_tasks[t]
            lines.append(f"{tid}: {t.result()}")
        for t in pending:
            tid = async_tasks[t]
            elapsed = time.time() - _tasks[tid]["start_time"]
            lines.append(f"{tid}: [running] {elapsed:.1f}s")

        return '\n'.join(lines)

    results = await asyncio.gather(*[_wait_single_task(tid, timeout) for tid in task_ids])
    return '\n'.join(f"{tid}: {res}" for tid, res in zip(task_ids, results))


@mcp.tool()
def task_list(all: bool = False) -> str:
    """List background tasks. By default shows running only.

    Args:
        all: If True, include completed tasks (default: running only)
    """
    if not _tasks:
        return "No tasks"

    lines = []
    for task_id, task in _tasks.items():
        elapsed = time.time() - task["start_time"]
        _, completed = check_task_output(task["pane"], task["begin"], task["end"])
        if completed and not all:
            continue
        status = "completed" if completed else "running"
        cmd = task.get("command", "")
        cmd_display = (cmd[:37] + "...") if len(cmd) > 40 else cmd
        cmd_display = cmd_display.replace('\n', ' ')
        lines.append(f"  {task_id} [{task['pane']}] [{task['type']}] [{status}] {elapsed:.1f}s  \"{cmd_display}\"")

    if not lines:
        return "No running tasks"
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


@mcp.tool()
def update_pane_description(pane: str, description: str) -> str:
    """Update the description of a registered pane.

    Args:
        pane: Registered pane name
        description: New description
    """
    if pane not in _working_panes:
        raise ValueError(f"Pane '{pane}' is not registered")

    _working_panes[pane] = description
    return f"Updated: {pane} ({description})"


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


async def _peek_on_pane_py(p: str, code: str, wait: float) -> str:
    """Peek helper for Python on a single pane."""
    lock = acquire_pane_lock(p)
    try:
        begin, _ = generate_marker()
        run_tmux_cmd(["send-keys", "-t", p, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, f'print("{begin}")'], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        for line in code.split('\n'):
            run_tmux_cmd(["send-keys", "-t", p, line], capture=False)
            run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        return await peek_output(p, begin, wait)
    finally:
        lock.release()


async def _peek_on_pane_tcl(p: str, code: str, wait: float) -> str:
    """Peek helper for TCL on a single pane."""
    lock = acquire_pane_lock(p)
    try:
        begin, _ = generate_marker()
        run_tmux_cmd(["send-keys", "-t", p, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, f'puts "{begin}"'], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, code], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        return await peek_output(p, begin, wait)
    finally:
        lock.release()


async def _peek_on_pane_sh(p: str, code: str, wait: float) -> str:
    """Peek helper for shell on a single pane."""
    lock = acquire_pane_lock(p)
    try:
        begin, _ = generate_marker()
        run_tmux_cmd(["send-keys", "-t", p, "-X", "cancel"], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, f"echo '{begin}'"], capture=False)
        run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        for line in code.split('\n'):
            run_tmux_cmd(["send-keys", "-t", p, line], capture=False)
            run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        return await peek_output(p, begin, wait)
    finally:
        lock.release()


async def _peek_multi(target_panes: list[str], code: str, wait: float, peek_fn) -> str:
    """Run peek on multiple panes concurrently and return grouped result."""
    results = {}
    coros = {}
    for p in target_panes:
        if not check_session(p):
            results[p] = "not found (skipped)"
        else:
            coros[p] = peek_fn(p, code, wait)
    if coros:
        results_list = await asyncio.gather(*coros.values(), return_exceptions=True)
        for p, result in zip(coros.keys(), results_list):
            results[p] = str(result) if isinstance(result, Exception) else result
    return _format_multi_result(results)


@mcp.tool()
async def xpy_peek(
    pane: str = None,
    code: str = "",
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute Python code and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Python code to execute
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_py)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _peek_on_pane_py(pane, code, wait)


@mcp.tool()
async def xtcl_peek(
    pane: str = None,
    code: str = "",
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute TCL code and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_tcl)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _peek_on_pane_tcl(pane, code, wait)


@mcp.tool()
async def xsh_peek(
    pane: str = None,
    code: str = "",
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute shell command and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_sh)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _peek_on_pane_sh(pane, code, wait)


@mcp.tool()
def send_text(pane: str = None, text: str = "", enter: bool = True, panes: list[str] = None) -> str:
    """Send text string to pane(s). For commands, passwords, etc.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        text: Text to send as-is
        enter: Press Enter after text (default: True)
        panes: Multiple panes to send simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if target_panes is not None:
        results = {}
        for p in target_panes:
            if not check_session(p):
                results[p] = "not found (skipped)"
                continue
            run_tmux_cmd(["send-keys", "-t", p, text], capture=False)
            if enter:
                run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
            results[p] = "sent"
        return _format_multi_result(results)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    run_tmux_cmd(["send-keys", "-t", pane, text], capture=False)
    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

    return "Text sent"


@mcp.tool()
def send_keys(pane: str = None, keys: str = "", enter: bool = False, panes: list[str] = None) -> str:
    """Send special keys to pane(s). For C-c, Enter, Escape, arrow keys, etc.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        keys: Space-separated special keys (e.g., "C-c C-c C-c", "Up Up Enter")
        enter: Press Enter after keys (default: False)
        panes: Multiple panes to send simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)

    if target_panes is not None:
        results = {}
        for p in target_panes:
            if not check_session(p):
                results[p] = "not found (skipped)"
                continue
            for key in keys.split():
                run_tmux_cmd(["send-keys", "-t", p, key], capture=False)
            if enter:
                run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
            results[p] = "sent"
        return _format_multi_result(results)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    for key in keys.split():
        run_tmux_cmd(["send-keys", "-t", pane, key], capture=False)

    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)

    return "Keys sent"


def _capture_single_pane(p: str, tail: int, rel_range: str, since_marker: str, filter_kwargs: dict) -> str:
    """Capture and filter a single pane."""
    raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-S", "-"])
    all_lines = raw.rstrip().split('\n')

    if since_marker:
        marker_idx = None
        for idx, line in enumerate(all_lines):
            if since_marker in line:
                marker_idx = idx
        if marker_idx is not None:
            all_lines = all_lines[marker_idx + 1:]

    if rel_range:
        start, end = parse_rel_range(rel_range)
        all_lines = all_lines[start:end]
    elif tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]

    return apply_output_filters(all_lines, n_negative=True, **filter_kwargs)


@mcp.tool()
def capture_pane(
    pane: str = None,
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
    strip_tqdm: bool = False,
    save: str = None,
    append: bool = True,
    prefix: str = None,
    suffix: str = None,
    panes: list[str] = None
) -> str:
    """Capture current pane content.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
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
        strip_tqdm: Remove tqdm progress lines, keeping only the last group
        save: File path to save output (optional)
        append: If True, append to file (>>); if False, overwrite (>)
        prefix: Text to prepend when saving (optional)
        suffix: Text to append when saving (optional)
        panes: Multiple panes to capture simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n,
               uniq=uniq, save=save, append=append, prefix=prefix, suffix=suffix,
               strip_tqdm=strip_tqdm)

    if target_panes is not None:
        results = {}
        for p in target_panes:
            if not check_session(p):
                results[p] = "not found (skipped)"
                continue
            results[p] = _capture_single_pane(p, tail, rel_range, since_marker, fkw)
        return _format_multi_result(results)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return _capture_single_pane(pane, tail, rel_range, since_marker, fkw)


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
