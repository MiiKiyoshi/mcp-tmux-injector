#!/usr/bin/env python3
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
- Panes MUST be registered with set_pane() before use
- If you get "Pane not registered" error, run ls() to check
- Context may be lost after compaction - always verify with ls()
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

# Ownership constants
MANAGED = "managed"
EXTERNAL = "external"

# Session registry: session_name -> {owner, created_at, windows: {name -> {owner}}}
_sessions: dict[str, dict] = {}

# Registered working panes: pane -> {description, owner}
_working_panes: dict[str, dict] = {}

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
    for pane, info in _working_panes.items():
        lines.append(f"  {pane}: {info['description']} ({info['owner']})")
    return '\n'.join(lines)


def check_pane_registered(pane: str) -> None:
    """Raise error if pane is not registered."""
    if pane not in _working_panes:
        if _working_panes:
            # Registered panes exist - suggest using one of them
            lines = [f"Pane '{pane}' is not registered.", ""]
            lines.append("Available panes (use one of these):")
            for p, info in _working_panes.items():
                lines.append(f"  {p}: {info['description']}")
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
Ask user for permission before registering with set_pane()."""
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
    for task_id, task in list(_tasks.items()):
        if task['pane'] == pane and 'lock' in task:
            if "end_time" in task:
                completed = True
            else:
                _, completed = check_task_output(pane, task['begin'], task['end'])
            if completed:
                _finalize_task(task)
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


_session_cache: dict[str, tuple[bool, float]] = {}
_SESSION_CACHE_TTL = 2.0


def check_session(session: str) -> bool:
    """Check if tmux session exists (exact match). Cached for 2s per session."""
    sess_name = session.split(':')[0]
    now = time.time()
    cached = _session_cache.get(sess_name)
    if cached and now - cached[1] < _SESSION_CACHE_TTL:
        return cached[0]
    result = subprocess.run(
        ["tmux", "has-session", "-t", f"={sess_name}"],
        capture_output=True
    )
    exists = result.returncode == 0
    _session_cache[sess_name] = (exists, now)
    return exists


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


def _validate_multi(single, multi, targets_name: str, targets: list) -> None:
    """Validate single/multi mutual exclusivity and length match."""
    if single and multi:
        raise ValueError(f"Use '{targets_name[:-1]}' or '{targets_name}', not both")
    if multi is not None:
        if not targets:
            raise ValueError(f"'{targets_name}' requires 'panes' or 'windows'")
        if len(multi) != len(targets):
            raise ValueError(f"len('{targets_name}')={len(multi)} != len(targets)={len(targets)}")


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
        return f"Saved to {save} ({len(lines)} lines)"

    return result


def check_task_output(session: str, begin: str, end: str) -> tuple[str, bool]:
    """Check current output for a task. Returns (output, is_complete)."""
    for n_lines in [1000, 4000, 16000]:
        raw = run_tmux_cmd(["capture-pane", "-t", session, "-p", "-S", f"-{n_lines}"])
        output, completed = extract_output(raw, begin, end)
        if completed or begin in raw:
            return output, completed
    # fallback: full scrollback
    raw = run_tmux_cmd(["capture-pane", "-t", session, "-p", "-S", "-"])
    return extract_output(raw, begin, end)


async def capture_output_blocking(session: str, begin: str, end: str, timeout: float) -> str:
    """Capture output between markers (blocking)."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        await asyncio.sleep(0.1)
        if not _check_end_marker(session, end):
            continue
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
# Ownership helpers
# =============================================================================

def parse_pane_id(pane: str) -> tuple[str, str, str]:
    """Parse pane ID into (session, window, pane_idx).
    Example: 'bench:nvdla_m.0' → ('bench', 'nvdla_m', '0')
    """
    colon_idx = pane.index(':')
    session = pane[:colon_idx]
    rest = pane[colon_idx + 1:]
    dot_idx = rest.rindex('.')
    window = rest[:dot_idx]
    pane_idx = rest[dot_idx + 1:]
    return session, window, pane_idx


def _auto_register_session_window(pane: str) -> None:
    """Auto-register session and window as external when a pane is registered."""
    session, window, _ = parse_pane_id(pane)
    if session not in _sessions:
        _sessions[session] = {
            "owner": EXTERNAL,
            "created_at": time.time(),
            "windows": {}
        }
    if window not in _sessions[session]["windows"]:
        _sessions[session]["windows"][window] = {"owner": EXTERNAL}


def _session_info_str(session_name: str) -> str:
    """Format session info for error messages."""
    if session_name not in _sessions:
        return f"Session '{session_name}': not in registry"
    info = _sessions[session_name]
    lines = [f"Session '{session_name}' ({info['owner']}):"]
    windows = _list_windows(session_name)
    for w in windows:
        w_owner = info["windows"].get(w, {}).get("owner", "unknown")
        panes_in_window = [p for p in _working_panes if p.startswith(f"{session_name}:{w}.")]
        pane_str = f" [{len(panes_in_window)} pane(s) registered]" if panes_in_window else ""
        lines.append(f"  {w} ({w_owner}){pane_str}")
    return '\n'.join(lines)


def _check_ownership(resource_type: str, name: str, owner: str, force: bool) -> None:
    """Raise error if resource is external and force is False."""
    if owner == EXTERNAL and not force:
        session_name = name.split(":")[0] if resource_type == "Window" else name
        info_str = _session_info_str(session_name)
        raise ValueError(
            f"{resource_type} '{name}' is {EXTERNAL} (not created by MCP).\n"
            f"Use force=True to override (requires user confirmation).\n\n"
            f"{info_str}"
        )


def _list_windows(session: str) -> list[str]:
    """List window names in a tmux session."""
    result = subprocess.run(
        ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_name}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [w.strip() for w in result.stdout.strip().split('\n') if w.strip()]


def _find_active_task_on_pane(pane: str) -> tuple[str, dict] | None:
    """Find task on a pane. Running task takes priority, then most recent completed."""
    latest = None
    latest_time = 0
    for task_id, task in list(_tasks.items()):
        if task["pane"] == pane:
            if "end_time" in task:
                completed = True
            else:
                # Lightweight check: only scan tail for end marker
                completed = _check_end_marker(task["pane"], task["end"])
                if completed:
                    # Full extraction and finalize
                    output, _ = check_task_output(task["pane"], task["begin"], task["end"])
                    task["cached_output"] = output
                    _finalize_task(task)
            if not completed:
                return task_id, task
            if task["start_time"] > latest_time:
                latest_time = task["start_time"]
                latest = (task_id, task)
    return latest


def _resolve_task_id(task_id: str | None, pane: str | None) -> str:
    """Resolve task_id from either direct ID or pane lookup."""
    if task_id is not None and pane is not None:
        raise ValueError("Use 'task_id' or 'pane', not both")
    if task_id is not None:
        return task_id
    if pane is not None:
        check_pane_registered(pane)
        result = _find_active_task_on_pane(pane)
        if result is None:
            raise ValueError(f"No task found on pane '{pane}'")
        return result[0]
    raise ValueError("Either 'task_id' or 'pane' must be provided")


def _resolve_task_ids_from_panes(panes: list[str]) -> list[str]:
    """Resolve task IDs from a list of panes."""
    result = []
    for p in panes:
        check_pane_registered(p)
        active = _find_active_task_on_pane(p)
        if active is None:
            raise ValueError(f"No task found on pane '{p}'")
        result.append(active[0])
    return result


def _cleanup_session_resources(name: str) -> None:
    """Clean up tasks, panes, and registry when a session is deleted."""
    for task_id in list(_tasks.keys()):
        task = _tasks.get(task_id)
        if not task:
            continue
        try:
            session, _, _ = parse_pane_id(task["pane"])
        except (ValueError, IndexError):
            continue
        if session == name:
            _finalize_task(task)
            _tasks.pop(task_id, None)
    for pane in list(_working_panes.keys()):
        try:
            session, _, _ = parse_pane_id(pane)
        except (ValueError, IndexError):
            continue
        if session == name:
            del _working_panes[pane]
    if name in _sessions:
        del _sessions[name]


def _cleanup_pane_tasks(pane: str) -> int:
    """Finalize and remove all tasks for a specific pane. Returns count."""
    count = 0
    for task_id in list(_tasks.keys()):
        task = _tasks.get(task_id)
        if not task:
            continue
        if task["pane"] == pane:
            _finalize_task(task)
            _tasks.pop(task_id, None)
            count += 1
    return count


def _cleanup_window_resources(session: str, window: str) -> None:
    """Clean up tasks, panes, and registry when a window is deleted."""
    prefix = f"{session}:{window}."
    for task_id in list(_tasks.keys()):
        task = _tasks.get(task_id)
        if not task:
            continue
        if task["pane"].startswith(prefix):
            _finalize_task(task)
            _tasks.pop(task_id, None)
    for pane in list(_working_panes.keys()):
        if pane.startswith(prefix):
            del _working_panes[pane]
    if session in _sessions and window in _sessions[session]["windows"]:
        del _sessions[session]["windows"][window]


# =============================================================================
# Blocking tools (original)
# =============================================================================

_LARGE_OUTPUT_THRESHOLD = 200
_LARGE_OUTPUT_PREVIEW = 20


async def _blocking_on_pane(p: str, code: str, send_fn, timeout: float, filter_kwargs: dict, task_type: str = "shell", tail: int = 0, head: int = None, force: bool = False) -> str:
    """Execute blocking command on a single pane and return filtered output."""
    lock = acquire_pane_lock(p)
    task_id, begin, end = generate_task_id_and_marker()
    start_time = time.time()
    converted = False
    try:
        send_fn(p, code, begin, end)
        output = await capture_output_blocking(p, begin, end, timeout)
        lines = output.split('\n') if output else []
        if head is not None and head > 0:
            lines = lines[:head]
        elif tail > 0 and len(lines) > tail:
            lines = lines[-tail:]
        filtered = apply_output_filters(lines, n_negative=False, **filter_kwargs)

        filtered_lines = filtered.split('\n') if filtered else []
        if not force and len(filtered_lines) > _LARGE_OUTPUT_THRESHOLD:
            _tasks[task_id] = {
                "pane": p, "begin": begin, "end": end,
                "start_time": start_time, "end_time": time.time(),
                "type": task_type, "command": code,
                "cached_output": output
            }
            _cleanup_completed_tasks()
            n = _LARGE_OUTPUT_PREVIEW
            head_part = '\n'.join(filtered_lines[:n])
            tail_part = '\n'.join(filtered_lines[-n:])
            omitted = len(filtered_lines) - n * 2
            return (
                f"[Large output: {len(filtered_lines)} lines → {task_id}]\n"
                f"task_output(task_id=\"{task_id}\", tail=/head=/range=) to retrieve.\n\n"
                f"{head_part}\n\n... {omitted} lines omitted ...\n\n{tail_part}"
            )

        return filtered
    except (TimeoutError, asyncio.CancelledError) as exc:
        _tasks[task_id] = {
            "pane": p, "begin": begin, "end": end,
            "start_time": start_time, "type": task_type,
            "command": code, "lock": lock
        }
        converted = True
        threading.Thread(target=_watch_task_completion, args=(task_id,), daemon=True).start()
        if isinstance(exc, asyncio.CancelledError):
            raise
        try:
            partial, _ = check_task_output(p, begin, end)
            n_lines = len(partial.split('\n')) if partial else 0
            progress = f" (output: {n_lines} lines so far)" if n_lines > 0 else ""
        except Exception:
            progress = ""
        return f"[timeout → task] {task_id}{progress}"
    finally:
        if not converted:
            lock.release()


async def _blocking_multi(target_panes: list[str], code: str, send_fn, timeout: float, fkw: dict, task_type: str = "shell", tail: int = 0, head: int = None, force: bool = False, codes: list[str] = None) -> str:
    """Run blocking command on multiple panes with graceful skip for missing panes."""
    results = {}
    coros = {}
    for i, p in enumerate(target_panes):
        if not check_session(p):
            results[p] = "not found (skipped)"
        else:
            c = codes[i] if codes else code
            coros[p] = _blocking_on_pane(p, c, send_fn, timeout, fkw, task_type=task_type, tail=tail, head=head, force=force)
    if coros:
        results_list = await asyncio.gather(*coros.values(), return_exceptions=True)
        for p, result in zip(coros.keys(), results_list):
            results[p] = str(result) if isinstance(result, BaseException) else result
    return _format_multi_result(results)


@mcp.tool()
async def xpy(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = None,
    timeout: float = 60.0,
    tail: int = 0,
    head: int = None,
    force: bool = False,
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
        code: Python code to execute (same code for all panes)
        codes: Different code per pane (1:1 with panes, mutually exclusive with code)
        file: Python file to execute (alternative to code)
        timeout: Timeout in seconds (default: 60)
        tail: If > 0, return only the last N lines (default: 0 = all)
        head: If provided, return only the first N lines
        force: If True, return full output without truncation (default: False)
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
    _validate_multi(code, codes, "codes", target_panes)

    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(compile(open('{abs_path}').read(), '{abs_path}', 'exec'))"

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    if code and "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")
    if codes:
        for c in codes:
            if "\\n" in c:
                raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_python_code, timeout, fkw, task_type="python", tail=tail, head=head, force=force, codes=codes)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_python_code, timeout, fkw, task_type="python", tail=tail, head=head, force=force)


@mcp.tool()
async def xtcl(
    pane: str = None,
    code: str = "",
    codes: list[str] = None,
    timeout: float = 60.0,
    tail: int = 0,
    head: int = None,
    force: bool = False,
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
        code: TCL code to execute (same code for all panes)
        codes: Different code per pane (1:1 with panes, mutually exclusive with code)
        timeout: Timeout in seconds (default: 60)
        tail: If > 0, return only the last N lines (default: 0 = all)
        head: If provided, return only the first N lines
        force: If True, return full output without truncation (default: False)
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
    _validate_multi(code, codes, "codes", target_panes)

    if not code and not codes:
        raise ValueError("'code' or 'codes' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_tcl_code, timeout, fkw, task_type="tcl", tail=tail, head=head, force=force, codes=codes)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_tcl_code, timeout, fkw, task_type="tcl", tail=tail, head=head, force=force)


@mcp.tool()
async def xsh(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = None,
    timeout: float = 60.0,
    tail: int = 0,
    head: int = None,
    force: bool = False,
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
        tail: If > 0, return only the last N lines (default: 0 = all)
        head: If provided, return only the first N lines
        force: If True, return full output without truncation (default: False)
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
        codes: Per-pane shell commands (1:1 with panes). Use code OR codes, not both.
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)

    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return await _blocking_multi(target_panes, code, send_shell_code, timeout, fkw, tail=tail, head=head, force=force, codes=codes)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _blocking_on_pane(pane, code, send_shell_code, timeout, fkw, tail=tail, head=head, force=force)


# =============================================================================
# Non-blocking tools (background execution)
# =============================================================================

_MAX_COMPLETED_TASKS = 20


def _cleanup_completed_tasks():
    """Remove oldest completed tasks when exceeding _MAX_COMPLETED_TASKS."""
    completed = [(tid, t) for tid, t in list(_tasks.items()) if "end_time" in t]
    if len(completed) <= _MAX_COMPLETED_TASKS:
        return
    completed.sort(key=lambda x: x[1]["end_time"])
    for tid, _ in completed[:-_MAX_COMPLETED_TASKS]:
        _tasks.pop(tid, None)


def _finalize_task(task: dict) -> None:
    """Record end_time and release lock. Safe to call from multiple threads."""
    if "end_time" not in task:
        task["end_time"] = time.time()
        _cleanup_completed_tasks()
    lock = task.pop("lock", None)
    if lock is not None:
        lock.release()


def _check_end_marker(pane: str, end: str, tail: int = 200) -> bool:
    """Lightweight completion check: only look for end marker in tail of scrollback."""
    raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-S", f"-{tail}"])
    return end in raw


def _watch_task_completion(task_id: str) -> None:
    """Background thread: poll for end marker and finalize task."""
    task = _tasks.get(task_id)
    if not task:
        return
    interval = 1.0
    max_interval = 30.0
    while task_id in _tasks:
        if "end_time" in task:
            return
        try:
            found = _check_end_marker(task["pane"], task["end"])
        except Exception:
            time.sleep(interval)
            interval = min(interval * 2, max_interval)
            continue
        if found:
            # End marker found — do full extraction once
            output, completed = check_task_output(task["pane"], task["begin"], task["end"])
            if completed:
                task["cached_output"] = output
                _finalize_task(task)
                return
        time.sleep(interval)
        interval = min(interval * 2, max_interval)


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

    threading.Thread(target=_watch_task_completion, args=(task_id,), daemon=True).start()

    return f"{p}: {task_id} (started)"


@mcp.tool()
async def xpy_start(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = None,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Start Python code execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Python code to execute
        codes: Per-pane Python code (1:1 with panes). Use code OR codes, not both.
        file: Python file to execute
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)

    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"exec(compile(open('{abs_path}').read(), '{abs_path}', 'exec'))"

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    if code and "\\n" in code:
        raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")
    if codes:
        for c in codes:
            if "\\n" in c:
                raise ValueError("Code contains \\n which breaks tmux injection. Use print() for blank lines.")

    if target_panes is not None:
        results = [_start_on_pane(p, codes[i] if codes else code, "python", send_python_code) for i, p in enumerate(target_panes)]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_python_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "python", "command": code, "lock": lock
    }
    threading.Thread(target=_watch_task_completion, args=(task_id,), daemon=True).start()
    return f"Started task {task_id} on {pane}"


@mcp.tool()
def xtcl_start(pane: str = None, code: str = "", codes: list[str] = None, panes: list[str] = None) -> str:
    """Start TCL code execution (non-blocking). Returns task_id immediately.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        codes: Per-pane TCL code (1:1 with panes). Use code OR codes, not both.
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)

    if not code and not codes:
        raise ValueError("Either 'code' or 'codes' must be provided")

    if target_panes is not None:
        results = [_start_on_pane(p, codes[i] if codes else code, "tcl", send_tcl_code) for i, p in enumerate(target_panes)]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_tcl_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "tcl", "command": code, "lock": lock
    }
    threading.Thread(target=_watch_task_completion, args=(task_id,), daemon=True).start()
    return f"Started task {task_id} on {pane}"


@mcp.tool()
async def xsh_start(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = None,
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Start shell command execution (non-blocking). Returns task_id immediately.

    Use task_status for status, task_output for output, task_wait to block.

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        codes: Per-pane shell commands (1:1 with panes). Use code OR codes, not both.
        file: Shell script file to execute
        panes: Multiple panes to start simultaneously (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)

    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        code = f"source '{abs_path}'"

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    if target_panes is not None:
        results = [_start_on_pane(p, codes[i] if codes else code, "shell", send_shell_code) for i, p in enumerate(target_panes)]
        return '\n'.join(results)

    check_pane_registered(pane)
    lock = acquire_pane_lock(pane)
    task_id, begin, end = generate_task_id_and_marker()
    send_shell_code(pane, code, begin, end)
    _tasks[task_id] = {
        "pane": pane, "begin": begin, "end": end,
        "start_time": time.time(), "type": "shell", "command": code, "lock": lock
    }
    threading.Thread(target=_watch_task_completion, args=(task_id,), daemon=True).start()
    return f"Started task {task_id} on {pane}"


@mcp.tool()
def task_status(task_id: str = None, pane: str = None) -> str:
    """Check task status (non-blocking). Returns status and elapsed time only.

    Args:
        task_id: Task ID from xpy_start, xtcl_start, or xsh_start
        pane: Pane to look up active task (use task_id OR pane, not both)
    """
    resolved_id = _resolve_task_id(task_id, pane)
    if resolved_id not in _tasks:
        raise ValueError(f"Task '{resolved_id}' not found")

    task = _tasks[resolved_id]
    if "end_time" in task:
        completed = True
    else:
        _, completed = check_task_output(task["pane"], task["begin"], task["end"])

    cmd = task.get("command", "")
    cmd_display = (cmd[:37] + "...") if len(cmd) > 40 else cmd
    cmd_display = cmd_display.replace('\n', ' ')

    if completed:
        _finalize_task(task)
        elapsed = task["end_time"] - task["start_time"]
        return f"[completed] {elapsed:.1f}s  {task['pane']}  \"{cmd_display}\""

    elapsed = time.time() - task["start_time"]
    return f"[running] {elapsed:.1f}s  {task['pane']}  \"{cmd_display}\""


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
    if "cached_output" in task:
        output = task["cached_output"]
    else:
        output, completed = check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            task["cached_output"] = output
            _finalize_task(task)

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
    pane: str = None,
    panes: list[str] = None,
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
        pane: Look up active task on this pane (alternative to task_id)
        panes: Look up active tasks on these panes (alternative to task_ids)
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
    # Resolve pane/panes to task_id/task_ids
    if pane is not None:
        task_id = _resolve_task_id(None, pane)
    if panes is not None:
        task_ids = _resolve_task_ids_from_panes(panes)

    specified = sum(x is not None for x in [task_id, task_ids])
    if specified != 1:
        raise ValueError("Exactly one of task_id/task_ids/pane/panes must be specified")

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

    interval = 0.5
    max_interval = 10.0
    while time.time() - start_time < timeout:
        if "end_time" in task:
            elapsed = task["end_time"] - task["start_time"]
            return f"[completed] {elapsed:.1f}s"
        await asyncio.sleep(interval)
        if _check_end_marker(task["pane"], task["end"]):
            output, completed = check_task_output(task["pane"], task["begin"], task["end"])
            if completed:
                task["cached_output"] = output
                _finalize_task(task)
                elapsed = task["end_time"] - task["start_time"]
                return f"[completed] {elapsed:.1f}s"
        interval = min(interval * 2, max_interval)

    return f"[timeout] {timeout}s"


@mcp.tool()
async def task_wait(
    task_id: str = None, task_ids: list[str] = None,
    pane: str = None, panes: list[str] = None,
    timeout: float = 60.0, race: bool = False
) -> str:
    """Wait for task completion (blocking). Returns status only.

    Args:
        task_id: Single task ID (mutually exclusive with task_ids)
        task_ids: Multiple task IDs to wait simultaneously (mutually exclusive with task_id)
        pane: Look up active task on this pane (alternative to task_id)
        panes: Look up active tasks on these panes (alternative to task_ids)
        timeout: Max wait time in seconds
        race: If True with task_ids, return when ANY task completes (default: wait for ALL)
    """
    # Resolve pane/panes to task_id/task_ids
    if pane is not None:
        task_id = _resolve_task_id(None, pane)
    if panes is not None:
        task_ids = _resolve_task_ids_from_panes(panes)

    specified = sum(x is not None for x in [task_id, task_ids])
    if specified != 1:
        raise ValueError("Exactly one of task_id/task_ids/pane/panes must be specified")

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
    for task_id, task in list(_tasks.items()):
        if "end_time" in task:
            completed = True
        else:
            completed = _check_end_marker(task["pane"], task["end"])
            if completed:
                output, _ = check_task_output(task["pane"], task["begin"], task["end"])
                task["cached_output"] = output
        if completed:
            _finalize_task(task)
            elapsed = task["end_time"] - task["start_time"]
        else:
            elapsed = time.time() - task["start_time"]
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
    _finalize_task(task)
    _tasks.pop(task_id, None)
    return f"Task {task_id} removed"


# =============================================================================
# Utility tools
# =============================================================================

@mcp.tool()
def ls(session: str = None, window: str = None, gpu: bool = False) -> str:
    """List tmux sessions/windows/panes as a tree.

    Without session: compact summary (session name, status, window count).
    With session: detailed tree with PID, process, cwd.
    With gpu=True: adds per-pane GPU device and memory from gpustat.

    Args:
        session: Filter to specific session (enables detailed mode)
        window: Filter to specific window (requires session)
        gpu: Show GPU memory per pane (requires session)
    """
    if window and not session:
        raise ValueError("'window' requires 'session'")
    if gpu and not session:
        raise ValueError("'gpu' requires 'session'")

    # Gather all pane info from tmux
    fmt = "#{session_name}|#{window_index}|#{window_name}|#{automatic-rename}|#{pane_index}|#{pane_tty}|#{pane_current_path}|#{pane_pid}"
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", fmt],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return "No tmux sessions found"

    # Parse tmux data (1st pass: collect pane info and ttys)
    raw_panes = []
    live_pane_ids = set()
    all_ttys = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|')
        if len(parts) < 8:
            continue
        sess, widx, wname, auto_rename, pidx, tty, cwd, ppid = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], parts[7]
        if session and sess != session:
            continue
        if window and wname != window and widx != window:
            continue
        pane_id = f"{sess}:{wname}.{pidx}"
        pane_id_idx = f"{sess}:{widx}.{pidx}"
        live_pane_ids.add(pane_id)
        live_pane_ids.add(pane_id_idx)
        raw_panes.append((sess, widx, wname, auto_rename, pidx, tty, cwd, ppid))
        if tty:
            all_ttys.append(tty)

    # Bulk ps call: get foreground processes for all ttys at once
    fg_procs = {}
    if all_ttys:
        try:
            tty_arg = ','.join(t.replace('/dev/', '') for t in all_ttys)
            ps_result = subprocess.run(
                ["ps", "-t", tty_arg, "-o", "tty=,stat=,args="],
                capture_output=True, text=True, timeout=2
            )
            for ps_line in ps_result.stdout.strip().split('\n'):
                ps_line = ps_line.strip()
                if not ps_line:
                    continue
                ps_parts = ps_line.split(None, 2)
                if len(ps_parts) >= 3 and '+' in ps_parts[1]:
                    fg_procs['/dev/' + ps_parts[0]] = ps_parts[2]
        except Exception:
            pass

    # Build pane_data with process info
    pane_data = []
    for sess, widx, wname, auto_rename, pidx, tty, cwd, ppid in raw_panes:
        proc = fg_procs.get(tty, "-") if tty else "-"
        pane_data.append((sess, widx, wname, auto_rename, pidx, proc, cwd, ppid))

    # Auto-clean orphaned registrations (only when no filter applied)
    if not session and not window:
        for pane_id in list(_working_panes.keys()):
            if pane_id not in live_pane_ids:
                del _working_panes[pane_id]

    # Get session metadata
    sessions_meta = {}
    for s in list_sessions():
        sessions_meta[s["name"]] = s

    # Compact mode: no session filter → session summary only
    if not session:
        sess_summary = {}
        for sess, widx, wname, auto_rename, pidx, proc, cwd, ppid in pane_data:
            if sess not in sess_summary:
                meta = sessions_meta.get(sess, {})
                status = "attached" if meta.get("attached") else "detached"
                owner = _sessions.get(sess, {}).get("owner", "untracked")
                sess_summary[sess] = {"status": status, "owner": owner, "windows": set()}
            sess_summary[sess]["windows"].add(wname)
        if not sess_summary:
            return "No tmux sessions found"
        output = []
        for sess, info in sess_summary.items():
            n_win = len(info["windows"])
            output.append(f"{sess} ({info['status']}, {info['owner']}, {n_win}w)")
        return '\n'.join(output)

    # Detailed mode: session specified → full tree with PID
    # GPU memory: only when gpu=True
    _gpu_by_pid = {}
    if gpu:
        try:
            _gs = subprocess.run(
                ["gpustat", "-p", "--no-header"],
                capture_output=True, text=True, timeout=3
            )
            for _gl in _gs.stdout.strip().split('\n'):
                _gm = re.match(r'\[(\d+)\]', _gl)
                if not _gm:
                    continue
                _gi = int(_gm.group(1))
                for _pm in re.finditer(r'/(\d+)\((\d+)M\)', _gl):
                    _gpu_by_pid[_pm.group(1)] = f"cuda:{_gi} {_pm.group(2)}M"
        except Exception:
            pass

    output = []
    prev_sess = None
    prev_widx = None
    for sess, widx, wname, auto_rename, pidx, proc, cwd, ppid in pane_data:
        if sess != prev_sess:
            meta = sessions_meta.get(sess, {})
            status = "attached" if meta.get("attached") else "detached"
            owner = _sessions.get(sess, {}).get("owner", "untracked")
            output.append(f"{sess} ({status}, {owner})")
            prev_sess = sess
            prev_widx = None

        if widx != prev_widx:
            w_owner = _sessions.get(sess, {}).get("windows", {}).get(wname, {}).get("owner", "")
            w_owner_str = f" ({w_owner})" if w_owner else ""
            w_label = widx if auto_rename == "1" else wname
            output.append(f"  {w_label}:{w_owner_str}")
            prev_widx = widx

        # Check registration (by name or index)
        pane_id_name = f"{sess}:{wname}.{pidx}"
        pane_id_idx = f"{sess}:{widx}.{pidx}"
        reg_info = _working_panes.get(pane_id_name) or _working_panes.get(pane_id_idx)
        reg_str = f'  [R: "{reg_info["description"]}"]' if reg_info else ""

        # Check active task (_find_active_task_on_pane already checks completion)
        task_str = ""
        for pane_key in [pane_id_name, pane_id_idx]:
            active = _find_active_task_on_pane(pane_key)
            if active:
                tid, t = active
                completed = "end_time" in t
                if completed:
                    elapsed = t["end_time"] - t["start_time"]
                else:
                    elapsed = time.time() - t["start_time"]
                st = "completed" if completed else "running"
                task_str = f"  [task: {tid} {st} {elapsed:.0f}s]"
                break

        # Shorten home dir
        home = os.path.expanduser("~")
        cwd = cwd.replace(home, "~")
        proc = proc.replace(home, "~")
        gpu_str = f"  [{_gpu_by_pid[ppid]}]" if ppid in _gpu_by_pid else ""
        output.append(f"    {pidx}: [{ppid}] {proc}  \"{cwd}\"{gpu_str}{reg_str}{task_str}")

    if not output:
        return f"Session '{session}' not found"

    return '\n'.join(output)


@mcp.tool()
def create_session(name: str, windows: list[str] = None, start_dir: str = None, cmd: str = None, cmds: list[str] = None) -> str:
    """Create a new tmux session (managed). Auto-registers all panes.

    Args:
        name: Session name
        windows: Window names to create (default: one window named "main")
        start_dir: Starting directory for the session
        cmd: Shell command to run in each window instead of default shell
        cmds: Per-window commands (1:1 with windows). Use cmd OR cmds, not both.
    """
    if check_session(name):
        raise ValueError(
            f"Session '{name}' already exists.\n\n"
            f"{_session_info_str(name)}"
        )

    if windows is None:
        windows = ["main"]

    _validate_multi(cmd, cmds, "cmds", windows)

    w_cmd = cmds[0] if cmds else cmd
    args = ["tmux", "new-session", "-d", "-s", name, "-n", windows[0]]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if w_cmd:
        args.append(w_cmd)
    subprocess.run(args, capture_output=True)

    for wi, w_name in enumerate(windows[1:], 1):
        w_cmd = cmds[wi] if cmds else cmd
        w_args = ["tmux", "new-window", "-t", name, "-n", w_name]
        if start_dir:
            w_args.extend(["-c", os.path.expanduser(start_dir)])
        if w_cmd:
            w_args.append(w_cmd)
        subprocess.run(w_args, capture_output=True)

    _sessions[name] = {
        "owner": MANAGED,
        "created_at": time.time(),
        "windows": {w: {"owner": MANAGED} for w in windows}
    }

    for w_name in windows:
        pane_id = f"{name}:{w_name}.0"
        _working_panes[pane_id] = {"description": f"managed ({w_name})", "owner": MANAGED}

    pane_list = ', '.join(f"{name}:{w}.0" for w in windows)
    return f"Created session '{name}' with {len(windows)} window(s).\nRegistered panes: {pane_list}"


@mcp.tool()
def kill_session(name: str, force: bool = False) -> str:
    """Kill a tmux session.

    Managed sessions (created by MCP) are killed immediately.
    External sessions require force=True (user confirmation via Claude Code).

    Args:
        name: Session name to kill
        force: Required for external sessions
    """
    if not check_session(name):
        raise ValueError(f"Session '{name}' does not exist")

    owner = _sessions.get(name, {}).get("owner", EXTERNAL)
    _check_ownership("Session", name, owner, force)

    _cleanup_session_resources(name)
    subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
    _session_cache.pop(name, None)

    return f"Killed session '{name}'"


@mcp.tool()
def create_window(session: str, name: str, start_dir: str = None, cmd: str = None) -> str:
    """Create a new window in an existing session (managed). Auto-registers pane.

    Args:
        session: Session name
        name: Window name
        start_dir: Starting directory
        cmd: Shell command to run instead of default shell
    """
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")

    existing = _list_windows(session)
    if name in existing:
        raise ValueError(f"Window '{name}' already exists in session '{session}'")

    args = ["tmux", "new-window", "-t", session, "-n", name]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if cmd:
        args.append(cmd)
    subprocess.run(args, capture_output=True)

    if session not in _sessions:
        _sessions[session] = {
            "owner": EXTERNAL,
            "created_at": time.time(),
            "windows": {}
        }
    _sessions[session]["windows"][name] = {"owner": MANAGED}

    pane_id = f"{session}:{name}.0"
    _working_panes[pane_id] = {"description": f"managed ({name})", "owner": MANAGED}

    return f"Created window '{name}' in session '{session}'.\nRegistered pane: {pane_id}"


@mcp.tool()
def kill_window(session: str, window: str, force: bool = False) -> str:
    """Kill a window in a tmux session.

    Managed windows are killed immediately.
    External windows require force=True.

    Args:
        session: Session name
        window: Window name
        force: Required for external windows
    """
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")

    existing = _list_windows(session)
    if window not in existing:
        raise ValueError(f"Window '{window}' not found in session '{session}'")

    owner = _sessions.get(session, {}).get("windows", {}).get(window, {}).get("owner", EXTERNAL)
    _check_ownership("Window", f"{session}:{window}", owner, force)

    _cleanup_window_resources(session, window)
    subprocess.run(["tmux", "kill-window", "-t", f"={session}:{window}"], capture_output=True)

    return f"Killed window '{window}' in session '{session}'"


@mcp.tool()
def set_pane(pane: str, description: str) -> str:
    """Register a pane for use. Re-calling updates description.

    Args:
        pane: tmux target (e.g., t1:1.0, t1:2.1)
        description: What this pane is for (e.g., "OpenROAD Python REPL")
    """
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    _working_panes[pane] = {"description": description, "owner": EXTERNAL}
    _auto_register_session_window(pane)
    return f"Registered: {pane} ({description})"


@mcp.tool()
def remove_pane(pane: str) -> str:
    """Unregister a pane.

    Args:
        pane: tmux target to unregister
    """
    if pane not in _working_panes:
        raise ValueError(f"Pane '{pane}' is not registered")

    del _working_panes[pane]
    return f"Removed: {pane}"


def _respawn_single(pane: str, start_dir: str = None, cmd: str = None) -> str:
    """Respawn a single registered pane. Returns status string."""
    cleaned = _cleanup_pane_tasks(pane)
    args = ["tmux", "respawn-pane", "-k", "-t", pane]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if cmd:
        args.append(cmd)
    subprocess.run(args, capture_output=True)
    desc = _working_panes[pane]["description"]
    parts = [f"Respawned: {pane} ({desc})"]
    if cleaned:
        parts.append(f"Cleaned {cleaned} task(s)")
    return "\n".join(parts)


@mcp.tool()
def respawn_pane(pane: str = None, panes: list[str] = None, start_dir: str = None, cmd: str = None, cmds: list[str] = None) -> str:
    """Kill the running process in a pane and start a fresh shell.
    Cleans up associated tasks and locks. Registration (description/owner) is preserved.

    Args:
        pane: Single pane to respawn
        panes: Multiple panes to respawn in parallel
        start_dir: Working directory for the new shell
        cmd: Shell command to run instead of default shell
        cmds: Per-pane commands (1:1 with panes). Use cmd OR cmds, not both.
    """
    multi = _resolve_panes(pane, panes)
    _validate_multi(cmd, cmds, "cmds", multi)
    if multi is not None:
        results = {p: _respawn_single(p, start_dir, cmds[i] if cmds else cmd) for i, p in enumerate(multi)}
        return _format_multi_result(results)
    check_pane_registered(pane)
    return _respawn_single(pane, start_dir, cmd)


async def peek_output(pane: str, begin: str, wait: float) -> str:
    """Wait and capture output after begin marker."""
    await asyncio.sleep(wait)

    for n_lines in [1000, 4000, 16000]:
        raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-S", f"-{n_lines}"])
        if begin in raw:
            break
    else:
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


async def _peek_multi(target_panes: list[str], code: str, wait: float, peek_fn, codes: list[str] = None) -> str:
    """Run peek on multiple panes concurrently and return grouped result."""
    results = {}
    coros = {}
    for i, p in enumerate(target_panes):
        if not check_session(p):
            results[p] = "not found (skipped)"
        else:
            c = codes[i] if codes else code
            coros[p] = peek_fn(p, c, wait)
    if coros:
        results_list = await asyncio.gather(*coros.values(), return_exceptions=True)
        for p, result in zip(coros.keys(), results_list):
            results[p] = str(result) if isinstance(result, BaseException) else result
    return _format_multi_result(results)


@mcp.tool()
async def xpy_peek(
    pane: str = None,
    code: str = "",
    codes: list[str] = None,
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute Python code and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Python code to execute
        codes: Per-pane Python code (1:1 with panes). Use code OR codes, not both.
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_py, codes=codes)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _peek_on_pane_py(pane, code, wait)


@mcp.tool()
async def xtcl_peek(
    pane: str = None,
    code: str = "",
    codes: list[str] = None,
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute TCL code and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: TCL code to execute
        codes: Per-pane TCL code (1:1 with panes). Use code OR codes, not both.
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_tcl, codes=codes)

    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")
    return await _peek_on_pane_tcl(pane, code, wait)


@mcp.tool()
async def xsh_peek(
    pane: str = None,
    code: str = "",
    codes: list[str] = None,
    wait: float = 1.0,
    panes: list[str] = None
) -> str:
    """Execute shell command and capture output for a short time (no end marker wait).

    Args:
        pane: Single tmux pane (e.g., t1:1.0)
        code: Shell command to execute
        codes: Per-pane shell commands (1:1 with panes). Use code OR codes, not both.
        wait: Seconds to wait before capturing (default: 1.0)
        panes: Multiple panes (use pane OR panes, not both)
    """
    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)
    if target_panes is not None:
        return await _peek_multi(target_panes, code, wait, _peek_on_pane_sh, codes=codes)

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
    if since_marker:
        # Marker could be far back — use progressive capture
        all_lines = None
        for n_lines in [1000, 4000, 16000]:
            raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-S", f"-{n_lines}"])
            lines = raw.rstrip().split('\n')
            marker_idx = None
            for idx, line in enumerate(lines):
                if since_marker in line:
                    marker_idx = idx
            if marker_idx is not None:
                all_lines = lines[marker_idx + 1:]
                break
        if all_lines is None:
            # fallback: full scrollback
            raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-S", "-"])
            lines = raw.rstrip().split('\n')
            marker_idx = None
            for idx, line in enumerate(lines):
                if since_marker in line:
                    marker_idx = idx
            all_lines = lines[marker_idx + 1:] if marker_idx is not None else lines
    else:
        # No marker — tail-based capture is sufficient
        n_capture = max(tail, 100) if tail > 0 else 100
        if rel_range:
            start_off, end_off = parse_rel_range(rel_range)
            n_capture = max(abs(start_off) + 50, n_capture)
        raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-S", f"-{n_capture}"])
        all_lines = raw.rstrip().split('\n')

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
        task = _tasks.get(task_id)
        if task:
            _finalize_task(task)
            _tasks.pop(task_id, None)

    return f"Cancelled {count} task(s)"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
