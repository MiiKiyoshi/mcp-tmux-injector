"""MCP tool definitions and server entry point."""
import asyncio
import functools
import os
import subprocess
import threading
import time
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import events, registry, tasks, tmux, watch
from .codec import (
    generate_marker,
    generate_task_id_and_marker,
    send_plain,
    send_python_code,
    send_shell_code,
    send_tcl_code,
)
from .config import INSTRUCTIONS, check_deny
from .filters import apply_output_filters
from .registry import EXTERNAL, MANAGED, check_pane_registered, require_pane
from .tmux import check_session, run_tmux_cmd
from . import mem as memmod

def _wait_method(ctx: Context) -> str:
    name = ctx.session.client_params.clientInfo.name.casefold()
    if "claude" in name:
        return (
            "Run the script with Monitor(command=<script>, persistent=true, "
            "timeout_ms=3600000), then end the turn. Keep the monitor for subsequent events."
        )
    if "codex" in name:
        return (
            'Run sh <quoted-script-path> --codex "$CODEX_THREAD_ID" with '
            'exec_command(yield_time_ms=1000, sandbox_permissions="require_escalated", '
            'justification="Allow the tmux event waiter to deliver events to this Codex thread?"). '
            "Once running, end the turn; do not poll. "
            "The script uses codex queue to deliver events as labeled user messages, "
            "including while idle. Delivery may take about 10 seconds. "
            "Requires codex queue on PATH and CODEX_THREAD_ID in the agent shell. "
            "Keep one waiter; stop its process when no longer needed."
        )
    return (
        "Run the script with your shell tool and read its output. If the tool returns a "
        "running session, retain it and use the tool that reads subsequent output. Keep "
        "the turn active while waiting unless your client explicitly supports resuming "
        "a completed turn from background output. After handling an event, resume "
        "waiting on the same process."
    )


class _Server(FastMCP):
    """FastMCP with compact tool schemas that rejects arguments a tool does not
    take. Every client that loads a tool pays for its schema, and FastMCP would
    otherwise drop an unknown argument silently."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.description = " ".join(tool.description.split())
            tool.inputSchema.pop("title", None)
            for prop in tool.inputSchema["properties"].values():
                prop.pop("title", None)
                if "default" in prop and prop["default"] is None:
                    del prop["default"]
        return tools

    async def call_tool(self, name, arguments):
        tool = next((t for t in await self.list_tools() if t.name == name), None)
        if tool is not None:
            accepted = list(tool.inputSchema["properties"])
            unknown = sorted(set(arguments) - set(accepted))
            if unknown:
                raise ToolError(f"{name} does not take {', '.join(unknown)}. "
                                f"It takes: {', '.join(accepted) or 'no arguments'}.")
        return await super().call_tool(name, arguments)


mcp = _Server("tmux-injector", instructions=INSTRUCTIONS)


def _plain_defaults(fn):
    """Make Field(...) defaults usable on direct (non-MCP) calls.

    MCP invocations always pass validated values, but a direct Python call
    (tests, internal reuse) binds the raw FieldInfo object for omitted params.
    This wrapper injects each FieldInfo's real default instead. The original
    signature is preserved (via __wrapped__), so FastMCP schemas are unchanged.
    """
    import inspect
    from pydantic.fields import FieldInfo

    sig = inspect.signature(fn)
    field_defaults = {
        name: p.default.default
        for name, p in sig.parameters.items()
        if isinstance(p.default, FieldInfo)
    }

    def fill(args, kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        for name, dv in field_defaults.items():
            if name not in bound.arguments:
                kwargs[name] = dv
        return kwargs

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await fn(*args, **fill(args, kwargs))
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **fill(args, kwargs))
    return wrapper

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


def _check_not_python(pane: str) -> None:
    """Raise error if pane is running a Python interpreter."""
    try:
        cmd = run_tmux_cmd(["display-message", "-p", "-t", pane, "#{pane_current_command}"])
        if cmd.strip().startswith("python"):
            raise ValueError(
                f"Pane '{pane}' is running {cmd.strip()}. Use xpy instead of xsh."
            )
    except subprocess.CalledProcessError:
        pass


def _get_task(task_id: str) -> dict:
    if task_id not in tasks._tasks:
        raise ValueError(f"Task '{task_id}' not found")
    return tasks._tasks[task_id]


# =============================================================================
# Execution engine (shared by xpy/xtcl/xsh)
# =============================================================================

_LARGE_OUTPUT_THRESHOLD = 200
_LARGE_OUTPUT_PREVIEW = 20


def _output_lines(output: str) -> list[str]:
    """Lines of a task's output. The shell and Python senders print a newline
    before the end marker, so a finished last line would otherwise read as one
    more, empty line."""
    lines = output.split('\n') if output else []
    if lines and lines[-1] == "":
        lines.pop()
    return lines


async def _blocking_on_pane(p: str, code: str, send_fn, timeout: float, task_type: str = "shell", tail: int = 0, force: bool = False) -> str:
    """Execute blocking command on a single pane and return filtered output."""
    lock = tasks.acquire_pane_lock(p)
    task_id, begin, end = generate_task_id_and_marker()
    start_time = time.time()
    converted = False
    try:
        send_fn(p, code, begin, end)
        output = await tasks.capture_output_blocking(p, begin, end, timeout)
        lines = _output_lines(output)
        if tail > 0 and len(lines) > tail:
            lines = lines[-tail:]
        filtered = apply_output_filters(lines)

        filtered_lines = filtered.split('\n') if filtered else []
        if not force and len(filtered_lines) > _LARGE_OUTPUT_THRESHOLD:
            tasks._tasks[task_id] = {
                "pane": p, "begin": begin, "end": end,
                "start_time": start_time, "end_time": time.time(),
                "type": task_type, "command": code,
                "cached_output": output
            }
            tasks._cleanup_completed_tasks()
            n = _LARGE_OUTPUT_PREVIEW
            head_part = '\n'.join(filtered_lines[:n])
            tail_part = '\n'.join(filtered_lines[-n:])
            omitted = len(filtered_lines) - n * 2
            return (
                f"[Large output: {len(filtered_lines)} lines → {task_id}]\n"
                f"task_output(task_id=\"{task_id}\", tail=/head=/grep=) to retrieve.\n\n"
                f"{head_part}\n\n... {omitted} lines omitted ...\n\n{tail_part}"
            )

        return filtered
    except TimeoutError:
        tasks._tasks[task_id] = {
            "pane": p, "begin": begin, "end": end,
            "start_time": start_time, "type": task_type,
            "command": code, "lock": lock,
        }
        converted = True
        threading.Thread(target=tasks.watch_task_completion, args=(task_id,), daemon=True).start()
        return (
            f"[task promoted] {task_id} ({p}, {timeout}s): running, do not resend it. "
            f"[done] arrives on the event stream." + events.waiter_note()
        )
    except asyncio.CancelledError:
        tasks._tasks[task_id] = {
            "pane": p, "begin": begin, "end": end,
            "start_time": start_time, "type": task_type,
            "command": code, "lock": lock,
        }
        converted = True
        threading.Thread(target=tasks.watch_task_completion, args=(task_id,), daemon=True).start()
        raise
    finally:
        if not converted:
            lock.release()


async def _read_after_on_pane(p: str, code: str, lang: str, read_after: float, tail: int) -> str:
    """Send code, sleep, capture from begin marker. No end marker — used for
    prompt-changing commands (entering REPL, ssh, exit) where marker pairs
    don't survive prompt changes.
    """
    lock = tasks.acquire_pane_lock(p)
    try:
        begin, _ = generate_marker()
        send_plain(p, code, lang, begin)

        await asyncio.sleep(min(read_after, tasks.BLOCKING_TIMEOUT_MAX))

        raw = tmux.capture_until(p, lambda r: begin in r)
        lines_full = tmux.split_capture(raw)
        result: list[str] = []
        capturing = False
        for line in lines_full:
            if line == begin:
                capturing = True
                continue
            if capturing:
                result.append(line)

        if tail > 0 and len(result) > tail:
            result = result[-tail:]
        return apply_output_filters(result)
    finally:
        lock.release()


async def _exec_tool(lang: str, send_fn, pane, code, timeout, read_after,
                     tail, force, guard_not_python: bool = False) -> str:
    """Shared body of xpy/xtcl/xsh: mode dispatch."""
    # Field(None, ...) defaults leak FieldInfo when the tool fn is called
    # without those args (e.g. internally) — unwrap to the real default.
    from pydantic.fields import FieldInfo
    if isinstance(timeout, FieldInfo):
        timeout = timeout.default
    if isinstance(read_after, FieldInfo):
        read_after = read_after.default
    if read_after is not None and timeout is not None:
        raise ValueError("timeout and read_after are mutually exclusive")

    require_pane(pane)
    if guard_not_python:
        _check_not_python(pane)
    if read_after is not None:
        return await _read_after_on_pane(pane, code, lang, read_after, tail)
    effective_timeout = timeout if timeout is not None else 3.0
    return await _blocking_on_pane(pane, code, send_fn, effective_timeout,
                                   task_type=lang, tail=tail, force=force)


# =============================================================================
# Execution tools
# =============================================================================

_TIMEOUT = "seconds before the command becomes a task (default 3, max 60). Leave unset for long work."
_READ_AFTER = "for a command that changes the prompt: wait N seconds (max 60) and return the screen"
_TAIL = "last N lines only"
_FORCE = "whole long output, not just its ends"


@mcp.tool()
@_plain_defaults
async def xpy(
    pane: str,
    code: str = None,
    file: str = Field(None, description="local .py file run in the REPL's globals (instead of import or reload), relative to your cwd"),
    timeout: float = Field(None, description=_TIMEOUT),
    read_after: float = Field(None, description=_READ_AFTER),
    tail: int = Field(0, description=_TAIL),
    force: bool = Field(False, description=_FORCE),
    ctx: Context = None
) -> str:
    """Run Python in a pane's REPL. A bare expression prints nothing: use print()."""
    send_py = send_python_code
    if file and code:
        raise ValueError("Use 'code' or 'file', not both")
    if file:
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        content = open(abs_path).read()
        # Embed file content so execution also works in remote (ssh) REPLs;
        # compile(..., path, ...) keeps real filename/line numbers in tracebacks.
        code = f"exec(compile({content!r}, {abs_path!r}, 'exec'))"
        preview = f"# xpy file: {abs_path} ({len(content.splitlines())} lines)"
        send_py = functools.partial(send_python_code, preview=preview)

    if not code:
        raise ValueError("Either 'code' or 'file' must be provided")

    return await _exec_tool("python", send_py, pane, code, timeout, read_after, tail, force)


@mcp.tool()
@_plain_defaults
async def xtcl(
    pane: str,
    code: str,
    timeout: float = Field(None, description=_TIMEOUT),
    read_after: float = Field(None, description=_READ_AFTER),
    tail: int = Field(0, description=_TAIL),
    force: bool = Field(False, description=_FORCE),
) -> str:
    """Run TCL in a pane (TCL tool and other TCL tools)."""
    return await _exec_tool("tcl", send_tcl_code, pane, code, timeout, read_after, tail, force)


@mcp.tool()
@_plain_defaults
async def xsh(
    pane: str,
    code: str,
    timeout: float = Field(None, description=_TIMEOUT),
    read_after: float = Field(None, description=_READ_AFTER),
    tail: int = Field(0, description=_TAIL),
    force: bool = Field(False, description=_FORCE),
) -> str:
    """Run a shell command in a pane."""
    return await _exec_tool("shell", send_shell_code, pane, code, timeout, read_after,
                            tail, force, guard_not_python=True)


# =============================================================================
# Task tools
# =============================================================================

_GREP = "Python regex, keeps matching lines. (?i) ignores case."
_CONTEXT = "lines of context around each grep match"


@mcp.tool()
@_plain_defaults
def task_output(
    task_id: str,
    tail: int = Field(0, description="last N lines"),
    head: int = Field(None, description="first N lines"),
    grep: str = Field(None, description=_GREP),
    C: int = Field(0, description=_CONTEXT),
) -> str:
    """Output of a task so far, without waiting."""
    if head and tail:
        raise ValueError("Use 'head' or 'tail', not both")
    task = _get_task(task_id)
    if "cached_output" in task:
        output = task["cached_output"]
    else:
        output, completed = tasks.check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            task["cached_output"] = output
            tasks.finalize_task(task)

    all_lines = _output_lines(output)
    if head is not None and head > 0:
        all_lines = all_lines[:head]
    elif tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]
    return apply_output_filters(all_lines, grep, C)


@mcp.tool()
@_plain_defaults
def mem_pane(
    pane: str = None,
    session: str = Field(None, description="per-pane table with a total, including unregistered panes. '*' gives one row per session."),
    gpu: bool = Field(True, description="include GPU memory"),
) -> str:
    """Memory held now by a pane's whole process tree (host RSS and GPU), with its
    heaviest processes. Give pane or session. For a job spread over panes, the
    session total is the number that matters."""
    if (pane is None) == (session is None):
        raise ValueError("give exactly one of pane / session")
    if session is not None:
        snap = memmod.proc_snapshot()
        gpu_map = memmod.gpu_by_pid() if gpu else None
        if session == "*":
            rows = memmod.group_by_session(memmod.mem_rows(None, snap, gpu_map))
            table = memmod.fmt_table(rows, "SESSION", gpu=gpu)
        else:
            rows = [(p, rss, mib) for _s, p, rss, mib
                    in memmod.mem_rows(session, snap, gpu_map)]
            table = memmod.fmt_table(rows, "PANE", gpu=gpu)
        hm = memmod.host_mem()
        host = (f"\nhost: {hm['available']:.0f} GB available of {hm['total']:.0f} GB, "
                f"swap used {hm['swap_used']:.1f} GB") if hm else ""
        return table + host

    require_pane(pane)
    snap = memmod.proc_snapshot()
    gpu_map = memmod.gpu_by_pid() if gpu else {}
    root = memmod.pane_pid(pane)
    rss_kb, rss_rows = memmod.tree_rss(root, snap)
    line = f"{pane}: RSS {memmod.fmt_kb(rss_kb)}"
    if gpu:
        gpu_mib, gpu_rows = memmod.tree_gpu(root, snap, gpu_map)
        if gpu_mib:
            line += f" | GPU {gpu_mib / 1024:.2f} GB"
    if rss_rows:
        top = ", ".join(f"{c}({i}) {memmod.fmt_kb(v)}" for i, c, v in rss_rows[:3])
        line += f"\n    top: {top}"
    out = [line]
    hm = memmod.host_mem()
    if hm:
        out.append(f"host: {hm['available']:.0f} GB available of {hm['total']:.0f} GB, "
                   f"swap used {hm['swap_used']:.1f} GB")
    return "\n".join(out)


@mcp.tool()
@_plain_defaults
def watch_mem(
    pane: str = None,
    session: str = Field(None, description="cap the session's combined usage"),
    rss_gb: float = Field(None, description="host RSS cap in GiB"),
    gpu_gb: float = Field(None, description="GPU memory cap in GiB"),
    poll: float = Field(30.0, description="seconds between checks"),
) -> str:
    """Report [cap] on the event stream the first time a pane or session exceeds a
    memory cap, or [gone] when its processes end. Returns at once. Give pane or
    session and at least one cap. Cap the session when a job spans panes: two
    panes at 6 GiB each pass a 10 GiB pane cap while the session holds 12 GiB."""
    if rss_gb is None and gpu_gb is None:
        raise ValueError("give rss_gb and/or gpu_gb")
    if (pane is None) == (session is None):
        raise ValueError("give exactly one of pane / session")
    if session is not None:
        memmod.session_panes(session)   # fail now, not inside the thread
    else:
        require_pane(pane)
        memmod.pane_pid(pane)
    threading.Thread(target=watch.watch_mem, args=(pane, session, rss_gb, gpu_gb, poll), daemon=True).start()
    scope = f"session {session}" if session else f"pane {pane}"
    caps = ", ".join(c for c in [f"rss {rss_gb} GiB" if rss_gb else "", f"gpu {gpu_gb} GiB" if gpu_gb else ""] if c)
    return f"[watching] {scope} ({caps}, every {poll:g}s); [cap] or [gone] arrives on the event stream." + events.waiter_note()


@mcp.tool()
@_plain_defaults
def poll_pane(
    pane: str,
    pattern: str,
    only_new: bool = Field(True, description="match only output that appears after this call, so a match already on screen is missed. False after respawn_pane(cmd=) or create_session(cmd=), whose output may already be there."),
) -> str:
    """Report [match] on the event stream the first time a Python regex matches a
    line in the pane. Returns at once. For output that arrives late or at an
    unknown time. A prompt due within seconds is a prompt change: use read_after."""
    if not pattern:
        raise ValueError("pattern is required")
    require_pane(pane)

    fp_lines, fp_total = watch.build_fingerprint(pane) if only_new else ([], 0)
    threading.Thread(target=watch.watch_pane, args=(pane, pattern, fp_lines, fp_total, only_new), daemon=True).start()
    return f"[watching] {pane} for /{pattern}/; [match] arrives on the event stream." + events.waiter_note()


@mcp.tool()
def wait_events(ctx: Context) -> str:
    """This session's event script and how to wait on it in this client. Start it
    once per connection and keep it for every later event."""
    path = events.write_script()
    if events.waiter_alive():
        return f"[running] the event stream is already being watched; do not start it again.\nscript: {path}"
    pending = events.unread()
    note = f" {pending} event(s) are already waiting and print at once." if pending else ""
    return (
        f"script: {path}\n"
        f"{_wait_method(ctx)}{note}\n"
        "On [done], read task_output(task_id); handle other events as reported. "
        "Do not resend the task command. Resume waiting on the same process after handling "
        "events. Start another copy only after the previous process has ended. If the stream "
        "reports that the server exited, call wait_events() on the new server for its script.\n"
    )


@mcp.tool()
@_plain_defaults
def task_list(all: bool = False) -> str:
    """Tracked tasks with status and next step. Running ones only unless all=True."""
    if not tasks._tasks:
        return "No tasks"

    lines = []
    for task_id, task in list(tasks._tasks.items()):
        completed = tasks.refresh_task(task)
        if completed:
            elapsed = task["end_time"] - task["start_time"]
        else:
            elapsed = time.time() - task["start_time"]
        if completed and not all:
            continue
        status = "error" if "error" in task else ("completed" if completed else "running")
        disp = tasks.cmd_display(task.get("command", ""))
        if completed:
            next_action = f'task_output(task_id="{task_id}")'
        else:
            next_action = "[done] on the event stream"
        lines.append(
            f"  {task_id} [{task['pane']}] [{task['type']}] [{status}] "
            f"{elapsed:.1f}s  \"{disp}\"  next={next_action}"
        )

    if not lines:
        return "No running tasks"
    return '\n'.join(lines)


@mcp.tool()
@_plain_defaults
def task_cancel(task_id: str) -> str:
    """Stop tracking a task. Its command keeps running: send C-c first to stop it."""
    task = _get_task(task_id)
    tasks.finalize_task(task)
    tasks._tasks.pop(task_id, None)
    return f"Task {task_id} removed"


# =============================================================================
# ls
# =============================================================================

def _ls_collect(session: str, window: str) -> tuple[list[tuple] | None, set[str]]:
    """Gather pane info from tmux; returns (pane rows, live pane ids).
    Returns (None, empty set) when tmux has no server/sessions."""
    fmt = "#{session_name}|#{window_index}|#{window_name}|#{automatic-rename}|#{pane_index}|#{pane_tty}|#{pane_current_path}|#{pane_pid}"
    result = subprocess.run(
        tmux.build_tmux_command(["list-panes", "-a", "-F", fmt]),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, set()

    raw_panes = []
    live_pane_ids = set()
    all_ttys = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|')
        if len(parts) < 8:
            continue
        sess, widx, wname, auto_rename, pidx, tty, cwd, ppid = parts[:8]
        if session and sess != session:
            continue
        if window and wname != window and widx != window:
            continue
        live_pane_ids.add(f"{sess}:{wname}.{pidx}")
        live_pane_ids.add(f"{sess}:{widx}.{pidx}")
        raw_panes.append((sess, widx, wname, auto_rename, pidx, tty, cwd, ppid))
        if tty:
            all_ttys.append(tty)

    # Bulk ps call: get foreground processes for all ttys at once
    fg_procs = {}
    if all_ttys:
        try:
            tty_arg = ','.join(t.replace('/dev/', '') for t in all_ttys)
            ps_result = subprocess.run(
                ["ps", "-t", tty_arg, "-o", "pid=,tty=,stat=,args="],
                capture_output=True, text=True, timeout=2
            )
            for ps_line in ps_result.stdout.strip().split('\n'):
                ps_line = ps_line.strip()
                if not ps_line:
                    continue
                ps_parts = ps_line.split(None, 3)
                if len(ps_parts) >= 4 and '+' in ps_parts[2]:
                    fg_procs['/dev/' + ps_parts[1]] = (ps_parts[3], ps_parts[0])
        except Exception:
            pass

    pane_data = []
    for sess, widx, wname, auto_rename, pidx, tty, cwd, ppid in raw_panes:
        fg = fg_procs.get(tty) if tty else None
        proc = fg[0] if fg else "-"
        fg_pid = fg[1] if fg else ppid
        pane_data.append((sess, widx, wname, auto_rename, pidx, proc, cwd, ppid, fg_pid))
    return pane_data, live_pane_ids


def _ls_compact(pane_data: list[tuple], sessions_meta: dict) -> str:
    """Session summary: name, status, owner, window count."""
    sess_summary = {}
    for sess, widx, wname, auto_rename, pidx, proc, cwd, ppid, fg_pid in pane_data:
        if sess not in sess_summary:
            meta = sessions_meta.get(sess, {})
            status = "attached" if meta.get("attached") else "detached"
            owner = registry._sessions.get(sess, {}).get("owner", "untracked")
            sess_summary[sess] = {"status": status, "owner": owner, "windows": set()}
        sess_summary[sess]["windows"].add(wname)
    if not sess_summary:
        return "No tmux sessions found"
    output = []
    for sess, info in sess_summary.items():
        n_win = len(info["windows"])
        output.append(f"{sess} ({info['status']}, {info['owner']}, {n_win}w)")
    return '\n'.join(output)


def _ls_detailed(pane_data: list[tuple], sessions_meta: dict) -> list[str]:
    """Full tree with PID, process, cwd, registration and task annotations."""
    output = []
    prev_sess = None
    prev_widx = None
    for sess, widx, wname, auto_rename, pidx, proc, cwd, ppid, fg_pid in pane_data:
        if sess != prev_sess:
            meta = sessions_meta.get(sess, {})
            status = "attached" if meta.get("attached") else "detached"
            owner = registry._sessions.get(sess, {}).get("owner", "untracked")
            output.append(f"{sess} ({status}, {owner})")
            prev_sess = sess
            prev_widx = None

        if widx != prev_widx:
            w_owner = registry._sessions.get(sess, {}).get("windows", {}).get(wname, {}).get("owner", "")
            w_owner_str = f" ({w_owner})" if w_owner else ""
            w_label = widx if auto_rename == "1" else wname
            output.append(f"  {w_label}:{w_owner_str}")
            prev_widx = widx

        # Check registration (by name or index)
        pane_id_name = f"{sess}:{wname}.{pidx}"
        pane_id_idx = f"{sess}:{widx}.{pidx}"
        reg_info = registry._working_panes.get(pane_id_name) or registry._working_panes.get(pane_id_idx)
        reg_str = f'  [R: "{reg_info["description"]}"]' if reg_info else ""

        # Check active task (find_active_task_on_pane already checks completion)
        task_str = ""
        for pane_key in [pane_id_name, pane_id_idx]:
            active = tasks.find_active_task_on_pane(pane_key)
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
        output.append(f"    {pidx}: [{ppid}] {proc}  \"{cwd}\"{reg_str}{task_str}")
    return output


@mcp.tool()
@_plain_defaults
def ls(session: str = None, window: str = None) -> str:
    """List tmux sessions with status and owner. With session: its windows and panes
    with PID, process, cwd and registration."""
    if window and not session:
        raise ValueError("'window' requires 'session'")

    pane_data, live_pane_ids = _ls_collect(session, window)
    if pane_data is None:
        return "No tmux sessions found"

    # Auto-clean orphaned registrations (only when no filter applied)
    if not session and not window:
        for pane_id in list(registry._working_panes.keys()):
            if pane_id not in live_pane_ids:
                del registry._working_panes[pane_id]

    sessions_meta = {s["name"]: s for s in tmux.list_sessions()}

    if not session:
        return _ls_compact(pane_data, sessions_meta)

    output = _ls_detailed(pane_data, sessions_meta)
    if not output:
        return f"Session '{session}' not found"
    return '\n'.join(output)


# =============================================================================
# Session/window/pane management tools
# =============================================================================

@mcp.tool()
@_plain_defaults
def create_session(name: str, windows: list[str] = None, start_dir: str = None,
                   cmd: str = Field(None, description="command each window starts with")) -> str:
    """Create a session you own, one window per name (default "main"), and register
    its panes as <name>:<window>.0."""
    if check_session(name):
        raise ValueError(
            f"Session '{name}' already exists.\n\n"
            f"{registry.session_info_str(name)}"
        )

    if windows is None:
        windows = ["main"]
    if not windows or len(set(windows)) != len(windows):
        raise ValueError("windows needs distinct names, or leave it unset for one window 'main'")

    args = ["new-session", "-d", "-s", name, "-n", windows[0]]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if cmd:
        args.append(tmux.wrap_cmd(cmd))
    subprocess.run(tmux.build_tmux_command(args), capture_output=True)

    for w_name in windows[1:]:
        w_args = ["new-window", "-t", name, "-n", w_name]
        if start_dir:
            w_args.extend(["-c", os.path.expanduser(start_dir)])
        if cmd:
            w_args.append(tmux.wrap_cmd(cmd))
        subprocess.run(tmux.build_tmux_command(w_args), capture_output=True)

    # The existence check above cached this session as absent (2s TTL) —
    # drop that entry so commands right after creation see the new session.
    tmux.forget_session(name)

    registry._sessions[name] = {
        "owner": MANAGED,
        "created_at": time.time(),
        "windows": {w: {"owner": MANAGED} for w in windows}
    }

    for w_name in windows:
        pane_id = f"{name}:{w_name}.0"
        registry._working_panes[pane_id] = {"description": f"managed ({w_name})", "owner": MANAGED}

    pane_list = ', '.join(f"{name}:{w}.0" for w in windows)
    return f"Created session '{name}' with {len(windows)} window(s).\nRegistered panes: {pane_list}"


@mcp.tool()
@_plain_defaults
def kill_session(name: str, force: bool = Field(False, description="needed for a session you did not create. Pass it only when the user asked.")) -> str:
    """Kill a session."""
    if not check_session(name):
        raise ValueError(f"Session '{name}' does not exist")

    owner = registry._sessions.get(name, {}).get("owner", EXTERNAL)
    registry.check_ownership("Session", name, owner, force)

    registry.cleanup_session_resources(name)
    subprocess.run(tmux.build_tmux_command(["kill-session", "-t", f"={name}"]), capture_output=True)
    tmux.forget_session(name)

    return f"Killed session '{name}'"


@mcp.tool()
@_plain_defaults
def create_window(session: str, name: str, start_dir: str = None, cmd: str = None) -> str:
    """Add a window to a session you created with create_session, and register its pane."""
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")
    if registry._sessions.get(session, {}).get("owner") != MANAGED:
        raise ValueError(
            f"Session '{session}' was not created by create_session on this server, so "
            f"its windows are not yours to add. Create your own session with create_session."
        )

    existing = tmux.list_windows(session)
    if name in existing:
        raise ValueError(f"Window '{name}' already exists in session '{session}'")

    args = ["new-window", "-t", session, "-n", name]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if cmd:
        args.append(tmux.wrap_cmd(cmd))
    subprocess.run(tmux.build_tmux_command(args), capture_output=True)

    registry._sessions[session]["windows"][name] = {"owner": MANAGED}

    pane_id = f"{session}:{name}.0"
    registry._working_panes[pane_id] = {"description": f"managed ({name})", "owner": MANAGED}

    return f"Created window '{name}' in session '{session}'.\nRegistered pane: {pane_id}"


@mcp.tool()
@_plain_defaults
def kill_window(session: str, window: str, force: bool = Field(False, description="needed for a window you did not create. Pass it only when the user asked.")) -> str:
    """Kill a window. Killing the last one ends the session."""
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")

    window_name = tmux.resolve_window(session, window)

    registry.check_ownership("Window", f"{session}:{window_name}",
                             registry.window_owner(session, window_name), force)

    registry.cleanup_window_resources(session, window_name)
    subprocess.run(tmux.build_tmux_command(["kill-window", "-t", f"={session}:{window_name}"]), capture_output=True)
    # Killing the last window ends the session, which the existence cache may still hold.
    tmux.forget_session(session)

    return f"Killed window '{window_name}' in session '{session}'"


@mcp.tool()
@_plain_defaults
def set_pane(pane: str, description: str) -> str:
    """Register an existing pane (session:window.index) for the other tools. Calling
    again updates its description."""
    try:
        tmux.parse_pane_id(pane)
    except ValueError:
        raise ValueError(f"Invalid pane id '{pane}': expected 'session:window.idx' format")
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    session, window, _ = tmux.parse_pane_id(pane)
    owner = registry.window_owner(session, tmux.resolve_window(session, window))
    registry._working_panes[pane] = {"description": description, "owner": owner}
    registry.auto_register_session_window(pane)
    return f"Registered: {pane} ({description})"


@mcp.tool()
@_plain_defaults
def respawn_pane(pane: str, start_dir: str = None,
                 cmd: str = Field(None, description="command to start instead of bash"),
                 force: bool = Field(False, description="needed for a pane you did not create. Pass it only when the user asked.")) -> str:
    """Kill the pane's process and start a fresh shell. Clears its tasks and keeps its
    registration."""
    check_pane_registered(pane)
    session, window, _ = tmux.parse_pane_id(pane)
    registry.check_ownership("Pane", pane, registry.window_owner(session, tmux.resolve_window(session, window)), force)
    try:
        run_tmux_cmd(["list-panes", "-t", pane], raise_on_error=True)
    except RuntimeError as e:
        raise ValueError(f"Pane '{pane}' does not exist: {e}")
    cleaned = registry.cleanup_pane_tasks(pane)
    cmd = cmd or "bash"
    args = ["respawn-pane", "-k", "-t", pane]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    args.append(tmux.wrap_cmd(cmd))
    subprocess.run(tmux.build_tmux_command(args), capture_output=True)
    desc = registry._working_panes[pane]["description"]
    parts = [f"Respawned: {pane} ({desc})"]
    if cleaned:
        parts.append(f"Cleaned {cleaned} task(s)")
    return "\n".join(parts)


# =============================================================================
# Raw input / capture tools
# =============================================================================

@mcp.tool()
@_plain_defaults
def send_text(pane: str, text: str, enter: bool = Field(True, description="press Enter after the text")) -> str:
    """Type text into a pane, such as an answer to a password or yes/no prompt."""
    check_deny(text, "send_text")
    require_pane(pane)
    run_tmux_cmd(["send-keys", "-t", pane, text], capture=False)
    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)
    return "Text sent"


@mcp.tool()
@_plain_defaults
def send_keys(pane: str, keys: str, enter: bool = Field(False, description="press Enter after the keys")) -> str:
    """Send tmux key names separated by spaces, such as C-c, Escape, Up or Enter."""
    require_pane(pane)
    for key in keys.split():
        run_tmux_cmd(["send-keys", "-t", pane, key], capture=False)
    if enter:
        run_tmux_cmd(["send-keys", "-t", pane, "Enter"], capture=False)
    return "Keys sent"


@mcp.tool()
@_plain_defaults
def capture_pane(
    pane: str,
    tail: int = Field(5, description="lines from the end. grep searches only these."),
    grep: str = Field(None, description=_GREP),
    C: int = Field(0, description=_CONTEXT),
) -> str:
    """Read the pane's screen. Use it the first time you use a pane and after C-c.
    Follow a task with task_output instead."""
    require_pane(pane)
    n_capture = max(tail, 100) if tail > 0 else 100
    raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", f"-{n_capture}"])
    all_lines = tmux.split_capture(raw)
    if tail > 0 and len(all_lines) > tail:
        all_lines = all_lines[-tail:]
    return apply_output_filters(all_lines, grep, C)


def main():
    # Strip .venv from PATH so tmux panes don't inherit virtualenv pollution
    os.environ["PATH"] = ":".join(
        p for p in os.environ.get("PATH", "").split(":") if "/.venv/" not in p
    )
    os.environ.pop("VIRTUAL_ENV", None)
    events.init()
    mcp.run(transport="stdio")
