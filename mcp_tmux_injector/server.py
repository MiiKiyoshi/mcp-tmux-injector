"""MCP tool definitions and server entry point."""
import asyncio
import functools
import json
import os
import random
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP, Context
from pydantic import Field

from . import registry, tasks, tmux
from .codec import (
    generate_marker,
    generate_task_id_and_marker,
    send_plain,
    send_python_code,
    send_shell_code,
    send_tcl_code,
)
from .config import FINGERPRINT_DIR, INSTRUCTIONS, check_deny
from .filters import apply_output_filters, parse_rel_range
from .registry import EXTERNAL, MANAGED, check_pane_registered, require_pane
from .tmux import check_session, run_tmux_cmd
from .watch_cli import (
    build_fingerprint,
    build_watch_cmd_pane,
    build_watch_cmd_task,
    cli_watch,
)

mcp = FastMCP("tmux-injector", instructions=INSTRUCTIONS)


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


# =============================================================================
# Parameter resolution helpers
# =============================================================================

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


def _resolve_task_id(task_id: str | None, pane: str | None) -> str:
    """Resolve task_id from either direct ID or pane lookup."""
    if task_id is not None and pane is not None:
        raise ValueError("Use 'task_id' or 'pane', not both")
    if task_id is not None:
        return task_id
    if pane is not None:
        check_pane_registered(pane)
        result = tasks.find_active_task_on_pane(pane)
        if result is None:
            raise ValueError(f"No task found on pane '{pane}'")
        return result[0]
    raise ValueError("Either 'task_id' or 'pane' must be provided")


def _resolve_task_ids_from_panes(panes: list[str]) -> list[str]:
    """Resolve task IDs from a list of panes."""
    result = []
    for p in panes:
        check_pane_registered(p)
        active = tasks.find_active_task_on_pane(p)
        if active is None:
            raise ValueError(f"No task found on pane '{p}'")
        result.append(active[0])
    return result


def _get_task(task_id: str) -> dict:
    if task_id not in tasks._tasks:
        raise ValueError(f"Task '{task_id}' not found")
    return tasks._tasks[task_id]


# =============================================================================
# Execution engine (shared by xpy/xtcl/xsh)
# =============================================================================

_LARGE_OUTPUT_THRESHOLD = 200
_LARGE_OUTPUT_PREVIEW = 20


async def _blocking_on_pane(p: str, code: str, send_fn, timeout: float, filter_kwargs: dict, task_type: str = "shell", tail: int = 0, head: int = None, force: bool = False) -> str:
    """Execute blocking command on a single pane and return filtered output."""
    lock = tasks.acquire_pane_lock(p)
    task_id, begin, end = generate_task_id_and_marker()
    start_time = time.time()
    converted = False
    try:
        send_fn(p, code, begin, end)
        output = await tasks.capture_output_blocking(p, begin, end, timeout)
        lines = output.split('\n') if output else []
        if head is not None and head > 0:
            lines = lines[:head]
        elif tail > 0 and len(lines) > tail:
            lines = lines[-tail:]
        filtered = apply_output_filters(lines, n_negative=False, **filter_kwargs)

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
                f"task_output(task_id=\"{task_id}\", tail=/head=/range=) to retrieve.\n\n"
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
            f"[task promoted] {task_id} ({p}, {timeout}s)\n"
            f"(prompt-changing cmd (python3/ssh/exit)? it will never complete — C-c, retry with read_after)"
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


async def _read_after_on_pane(p: str, code: str, lang: str, read_after: float,
                              fkw: dict, tail: int, head: int) -> str:
    """Send code, sleep, capture from begin marker. No end marker — used for
    prompt-changing commands (entering REPL, ssh, exit) where marker pairs
    don't survive prompt changes.
    """
    if not check_session(p):
        return f"{p}: not found (skipped)"
    if lang == "shell":
        _check_not_python(p)
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

        if head is not None and head > 0:
            result = result[:head]
        elif tail > 0 and len(result) > tail:
            result = result[-tail:]
        return apply_output_filters(result, n_negative=False, **fkw)
    finally:
        lock.release()


async def _gather_panes(target_panes: list[str], code: str, codes: list[str] | None, make_coro) -> str:
    """Run make_coro(pane, code) concurrently over panes; skip missing panes."""
    results = {}
    coros = {}
    for i, p in enumerate(target_panes):
        if not check_session(p):
            results[p] = "not found (skipped)"
        else:
            c = codes[i] if codes else code
            coros[p] = make_coro(p, c)
    if coros:
        results_list = await asyncio.gather(*coros.values(), return_exceptions=True)
        for p, result in zip(coros.keys(), results_list):
            results[p] = str(result) if isinstance(result, BaseException) else result
    return _format_multi_result(results)


async def _exec_tool(lang: str, send_fn, pane, code, codes, timeout, read_after,
                     tail, head, force, fkw, panes, guard_not_python: bool = False) -> str:
    """Shared body of xpy/xtcl/xsh: mode dispatch and single/multi routing."""
    # Field(None, ...) defaults leak FieldInfo when the tool fn is called
    # without those args (e.g. internally) — unwrap to the real default.
    from pydantic.fields import FieldInfo
    if isinstance(timeout, FieldInfo):
        timeout = timeout.default
    if isinstance(read_after, FieldInfo):
        read_after = read_after.default
    if read_after is not None and timeout is not None:
        raise ValueError("timeout and read_after are mutually exclusive")

    target_panes = _resolve_panes(pane, panes)
    _validate_multi(code, codes, "codes", target_panes)

    if read_after is not None:
        if target_panes is not None:
            return await _gather_panes(
                target_panes, code, codes,
                lambda p, c: _read_after_on_pane(p, c, lang, read_after, fkw, tail, head))
        require_pane(pane)
        if guard_not_python:
            _check_not_python(pane)
        return await _read_after_on_pane(pane, code, lang, read_after, fkw, tail, head)

    effective_timeout = timeout if timeout is not None else 3.0
    if target_panes is not None:
        if guard_not_python:
            for p in target_panes:
                _check_not_python(p)
        return await _gather_panes(
            target_panes, code, codes,
            lambda p, c: _blocking_on_pane(p, c, send_fn, effective_timeout, fkw,
                                           task_type=lang, tail=tail, head=head, force=force))
    require_pane(pane)
    if guard_not_python:
        _check_not_python(pane)
    return await _blocking_on_pane(pane, code, send_fn, effective_timeout, fkw,
                                   task_type=lang, tail=tail, head=head, force=force)


# =============================================================================
# Execution tools
# =============================================================================

@mcp.tool()
@_plain_defaults
async def xpy(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = Field(None, description="Execute a .py file via exec(). Path resolved relative to agent cwd. Use instead of code='exec(open(...).read())'"),
    timeout: float = Field(None, description="default mode: max seconds to wait for end marker before auto-promoting to background task (default 3s, capped at 60s). Mutually exclusive with read_after."),
    read_after: float = Field(None, description="read_after mode: skip end-marker detection, send code, sleep N seconds, return pane capture from begin marker. For prompt-changing commands (entering/exiting REPL, ssh). Capped at 60s. Mutually exclusive with timeout."),
    tail: int = 0,
    head: int = None,
    force: bool = Field(False, description="return full output without truncation"),
    grep: str = None,
    v: str = Field(None, description="exclude matching (grep -v)"),
    i: bool = Field(False, description="case insensitive (grep -i)"),
    w: bool = Field(False, description="whole word match (grep -w)"),
    F: bool = Field(False, description="literal string, not regex (grep -F)"),
    m: int = Field(None, description="max matching lines (grep -m)"),
    A: int = Field(None, description="lines after match (grep -A)"),
    B: int = Field(None, description="lines before match (grep -B)"),
    C: int = Field(None, description="context lines around match (grep -C)"),
    n: bool = Field(False, description="show line numbers (grep -n)"),
    uniq: bool = True,
    strip_tqdm: bool = Field(False, description="remove tqdm lines, keep last group"),
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Execute Python code in tmux. Default mode auto-promotes to a task on
    timeout. read_after mode skips marker detection (use for entering/exiting
    REPLs, ssh). On abort/timeout, code was already sent — do NOT resend."""
    send_py = send_python_code
    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
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

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)
    return await _exec_tool("python", send_py, pane, code, codes, timeout, read_after,
                            tail, head, force, fkw, panes)


@mcp.tool()
@_plain_defaults
async def xtcl(
    pane: str = None,
    code: str = "",
    codes: list[str] = None,
    timeout: float = Field(None, description="default mode: max seconds to wait for end marker before auto-promoting to background task (default 3s, capped at 60s). Mutually exclusive with read_after."),
    read_after: float = Field(None, description="read_after mode: skip end-marker detection, send code, sleep N seconds, return pane capture from begin marker. For prompt-changing commands (entering/exiting REPL, ssh). Capped at 60s. Mutually exclusive with timeout."),
    tail: int = 0,
    head: int = None,
    force: bool = Field(False, description="return full output without truncation"),
    grep: str = None,
    v: str = Field(None, description="exclude matching (grep -v)"),
    i: bool = Field(False, description="case insensitive (grep -i)"),
    w: bool = Field(False, description="whole word match (grep -w)"),
    F: bool = Field(False, description="literal string, not regex (grep -F)"),
    m: int = Field(None, description="max matching lines (grep -m)"),
    A: int = Field(None, description="lines after match (grep -A)"),
    B: int = Field(None, description="lines before match (grep -B)"),
    C: int = Field(None, description="context lines around match (grep -C)"),
    n: bool = Field(False, description="show line numbers (grep -n)"),
    uniq: bool = True,
    strip_tqdm: bool = Field(False, description="remove tqdm lines, keep last group"),
    panes: list[str] = None
) -> str:
    """Execute TCL code in tmux. Default mode auto-promotes to a task on
    timeout. read_after mode skips marker detection (use for entering/exiting
    REPLs). On abort/timeout, code was already sent — do NOT resend."""
    if not code and not codes:
        raise ValueError("'code' or 'codes' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)
    return await _exec_tool("tcl", send_tcl_code, pane, code, codes, timeout, read_after,
                            tail, head, force, fkw, panes)


@mcp.tool()
@_plain_defaults
async def xsh(
    pane: str = None,
    code: str = None,
    codes: list[str] = None,
    file: str = None,
    timeout: float = Field(None, description="default mode: max seconds to wait for end marker before auto-promoting to background task (default 3s, capped at 60s). Mutually exclusive with read_after."),
    read_after: float = Field(None, description="read_after mode: skip end-marker detection, send code, sleep N seconds, return pane capture from begin marker. For prompt-changing commands (entering/exiting REPL, ssh). Capped at 60s. Mutually exclusive with timeout."),
    tail: int = 0,
    head: int = None,
    force: bool = Field(False, description="return full output without truncation"),
    grep: str = None,
    v: str = Field(None, description="exclude matching (grep -v)"),
    i: bool = Field(False, description="case insensitive (grep -i)"),
    w: bool = Field(False, description="whole word match (grep -w)"),
    F: bool = Field(False, description="literal string, not regex (grep -F)"),
    m: int = Field(None, description="max matching lines (grep -m)"),
    A: int = Field(None, description="lines after match (grep -A)"),
    B: int = Field(None, description="lines before match (grep -B)"),
    C: int = Field(None, description="context lines around match (grep -C)"),
    n: bool = Field(False, description="show line numbers (grep -n)"),
    uniq: bool = True,
    strip_tqdm: bool = Field(False, description="remove tqdm lines, keep last group"),
    panes: list[str] = None,
    ctx: Context = None
) -> str:
    """Execute shell command in tmux. Default mode auto-promotes to a task on
    timeout. read_after mode skips marker detection (use for entering/exiting
    REPLs, ssh). On abort/timeout, code was already sent — do NOT resend."""
    if file:
        if codes:
            raise ValueError("Use 'file' or 'codes', not both")
        client_cwd = await _get_client_cwd(ctx) if ctx else _client_cwd
        abs_path = _resolve_file_path(file, client_cwd)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: {abs_path}")
        # Send file content as keystrokes so it also works on remote (ssh) shells.
        # Runs in the current shell (inside the { } group), same as `source`.
        code = open(abs_path).read().rstrip('\n')

    if not code and not codes:
        raise ValueError("Either 'code', 'codes', or 'file' must be provided")

    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n, uniq=uniq, strip_tqdm=strip_tqdm)
    return await _exec_tool("shell", send_shell_code, pane, code, codes, timeout, read_after,
                            tail, head, force, fkw, panes, guard_not_python=True)


# =============================================================================
# Task tools
# =============================================================================

@mcp.tool()
@_plain_defaults
def task_status(task_id: str = None, pane: str = None) -> str:
    """Check task status (non-blocking). Returns status and elapsed time only."""
    task = _get_task(_resolve_task_id(task_id, pane))
    completed = tasks.refresh_task(task)
    disp = tasks.cmd_display(task.get("command", ""))

    if completed:
        status = "error" if "error" in task else "completed"
        elapsed = task["end_time"] - task["start_time"]
        return f"[{status}] {elapsed:.1f}s  {task['pane']}  \"{disp}\""

    elapsed = time.time() - task["start_time"]
    return f"[running] {elapsed:.1f}s  {task['pane']}  \"{disp}\""


def _get_single_task_output(
    tid: str, tail: int, head: int, line_range: str,
    save: str, append: bool, prefix: str, suffix: str,
    include_command: bool, command_prefix: str, markdown: bool,
    filter_kwargs: dict
) -> str:
    """Get filtered output for a single task."""
    task = _get_task(tid)
    if "cached_output" in task:
        output = task["cached_output"]
    else:
        output, completed = tasks.check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            task["cached_output"] = output
            tasks.finalize_task(task)

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
@_plain_defaults
def task_output(
    task_id: str = None,
    task_ids: list[str] = None,
    pane: str = None,
    panes: list[str] = None,
    tail: int = 0,
    head: int = None,
    range: str = None,
    grep: str = None,
    v: str = Field(None, description="exclude matching (grep -v)"),
    i: bool = Field(False, description="case insensitive (grep -i)"),
    w: bool = Field(False, description="whole word match (grep -w)"),
    F: bool = Field(False, description="literal string, not regex (grep -F)"),
    m: int = Field(None, description="max matching lines (grep -m)"),
    A: int = Field(None, description="lines after match (grep -A)"),
    B: int = Field(None, description="lines before match (grep -B)"),
    C: int = Field(None, description="context lines around match (grep -C)"),
    n: bool = Field(False, description="show line numbers (grep -n)"),
    uniq: bool = True,
    strip_tqdm: bool = Field(False, description="remove tqdm lines, keep last group"),
    save: str = None,
    append: bool = True,
    prefix: str = None,
    suffix: str = None,
    include_command: bool = False,
    command_prefix: str = "$ ",
    markdown: bool = False
) -> str:
    """Get task output (non-blocking)."""
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


@mcp.tool()
@_plain_defaults
def task_wait(task_id: str = None, pane: str = None) -> str:
    """Return a shell command for Monitor that emits one line on task completion.

    Pass the returned string directly to Monitor's `command` parameter. Monitor
    runs it in the background; when the task ends (or shows a failure
    signature), Monitor delivers a notification line. Then call task_output to
    read the body.

    Either task_id or pane must be provided.
    """
    resolved_id = _resolve_task_id(task_id, pane)
    task = _get_task(resolved_id)
    if "end_time" in task:
        return f"echo '[done] task {resolved_id} (already complete)'"
    return build_watch_cmd_task(resolved_id, task["pane"], task["end"])


@mcp.tool()
@_plain_defaults
def poll_pane(
    pane: str,
    pattern: str,
    only_new: bool = Field(True, description="True (default): match only output produced AFTER this call (fingerprint snapshot taken NOW). False: also match pre-existing content — required after respawn_pane(cmd=) / create_session(cmd=) where the trigger ran before poll_pane."),
    i: bool = Field(False, description="case insensitive match"),
    F: bool = Field(False, description="literal string, not regex"),
) -> str:
    """Return a shell command for Monitor that emits one line on first pattern match.

    Pass the returned string directly to Monitor's `command` parameter. Monitor
    runs it in the background; when the pattern first appears in the pane,
    Monitor delivers a notification line of the form '[match] <line>'.

    only_new=True (default): only matches output produced AFTER this call. The
    fingerprint snapshot is taken NOW.
    only_new=False: also matches pre-existing content.

    For multi-pane race, spawn multiple Monitors with separate poll_pane calls.
    """
    if not pattern:
        raise ValueError("pattern is required")
    require_pane(pane)

    fp_lines, fp_total = build_fingerprint(pane) if only_new else ([], 0)
    FINGERPRINT_DIR.mkdir(parents=True, exist_ok=True)
    fp_path = FINGERPRINT_DIR / f"fp_{int(time.time()*1000)}_{random.randint(0, 0xFFFF):04x}.json"
    fp_path.write_text(json.dumps({"lines": fp_lines, "total": fp_total}))

    return build_watch_cmd_pane(pane, pattern, fp_path, fp_total, only_new, i, F)


@mcp.tool()
@_plain_defaults
def task_list(all: bool = False) -> str:
    """List background tasks. By default shows running only."""
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
        lines.append(f"  {task_id} [{task['pane']}] [{task['type']}] [{status}] {elapsed:.1f}s  \"{disp}\"")

    if not lines:
        return "No running tasks"
    return '\n'.join(lines)


@mcp.tool()
@_plain_defaults
def task_cancel(task_id: str) -> str:
    """Remove task tracking. Does NOT stop the running process."""
    task = _get_task(task_id)
    tasks.finalize_task(task)
    tasks._tasks.pop(task_id, None)
    return f"Task {task_id} removed"


@mcp.tool()
@_plain_defaults
def task_cancel_all() -> str:
    """Remove all task tracking. Does NOT stop running processes."""
    if not tasks._tasks:
        return "No tasks to cancel"

    count = len(tasks._tasks)
    for task_id in list(tasks._tasks.keys()):
        task = tasks._tasks.get(task_id)
        if task:
            tasks.finalize_task(task)
            tasks._tasks.pop(task_id, None)

    return f"Cancelled {count} task(s)"


# =============================================================================
# ls
# =============================================================================

def _ls_collect(session: str, window: str) -> tuple[list[tuple] | None, set[str]]:
    """Gather pane info from tmux; returns (pane rows, live pane ids).
    Returns (None, empty set) when tmux has no server/sessions."""
    fmt = "#{session_name}|#{window_index}|#{window_name}|#{automatic-rename}|#{pane_index}|#{pane_tty}|#{pane_current_path}|#{pane_pid}"
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", fmt],
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
        fg_pid = fg[1] if fg else ppid  # foreground process PID for GPU lookup
        pane_data.append((sess, widx, wname, auto_rename, pidx, proc, cwd, ppid, fg_pid))
    return pane_data, live_pane_ids


def _gpu_by_pid() -> dict[str, str]:
    """Map PID -> 'cuda:N <mem>M' via gpustat."""
    gpu_map = {}
    try:
        gs = subprocess.run(
            ["gpustat", "-p", "--no-header"],
            capture_output=True, text=True, timeout=3
        )
        for gl in gs.stdout.strip().split('\n'):
            gm = re.match(r'\[(\d+)\]', gl)
            if not gm:
                continue
            gi = int(gm.group(1))
            for pm in re.finditer(r'/(\d+)\((\d+)M\)', gl):
                gpu_map[pm.group(1)] = f"cuda:{gi} {pm.group(2)}M"
    except Exception:
        pass
    return gpu_map


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


def _ls_detailed(pane_data: list[tuple], sessions_meta: dict, gpu: bool) -> list[str]:
    """Full tree with PID, process, cwd, registration and task annotations."""
    gpu_map = _gpu_by_pid() if gpu else {}
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
        gpu_str = f"  [{gpu_map[fg_pid]}]" if fg_pid in gpu_map else ""
        output.append(f"    {pidx}: [{ppid}] {proc}  \"{cwd}\"{gpu_str}{reg_str}{task_str}")
    return output


@mcp.tool()
@_plain_defaults
def ls(session: str = None, window: str = None, gpu: bool = False) -> str:
    """List tmux sessions/windows/panes as a tree.

    Without session: compact summary (session name, status, window count).
    With session: detailed tree with PID, process, cwd.
    With gpu=True: adds per-pane GPU device and memory from gpustat."""
    if window and not session:
        raise ValueError("'window' requires 'session'")
    if gpu and not session:
        raise ValueError("'gpu' requires 'session'")

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

    output = _ls_detailed(pane_data, sessions_meta, gpu)
    if not output:
        return f"Session '{session}' not found"
    return '\n'.join(output)


# =============================================================================
# Session/window/pane management tools
# =============================================================================

@mcp.tool()
@_plain_defaults
def create_session(name: str, windows: list[str] = None, start_dir: str = None, cmd: str = None, cmds: list[str] = None) -> str:
    """Create a new tmux session (managed). Auto-registers all panes."""
    if check_session(name):
        raise ValueError(
            f"Session '{name}' already exists.\n\n"
            f"{registry.session_info_str(name)}"
        )

    if windows is None:
        windows = ["main"]

    _validate_multi(cmd, cmds, "cmds", windows)

    w_cmd = cmds[0] if cmds else cmd
    args = ["tmux", "new-session", "-d", "-s", name, "-n", windows[0]]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if w_cmd:
        args.append(tmux.wrap_cmd(w_cmd))
    subprocess.run(args, capture_output=True)

    for wi, w_name in enumerate(windows[1:], 1):
        w_cmd = cmds[wi] if cmds else cmd
        w_args = ["tmux", "new-window", "-t", name, "-n", w_name]
        if start_dir:
            w_args.extend(["-c", os.path.expanduser(start_dir)])
        if w_cmd:
            w_args.append(tmux.wrap_cmd(w_cmd))
        subprocess.run(w_args, capture_output=True)

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
def kill_session(name: str, force: bool = Field(False, description="force kill external (non-managed) session")) -> str:
    """Kill a tmux session.

    Managed sessions (created by MCP) are killed immediately.
    External sessions require force=True (user confirmation via Claude Code)."""
    if not check_session(name):
        raise ValueError(f"Session '{name}' does not exist")

    owner = registry._sessions.get(name, {}).get("owner", EXTERNAL)
    registry.check_ownership("Session", name, owner, force)

    registry.cleanup_session_resources(name)
    subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
    tmux.forget_session(name)

    return f"Killed session '{name}'"


@mcp.tool()
@_plain_defaults
def create_window(session: str, name: str, start_dir: str = None, cmd: str = None) -> str:
    """Create a window in a managed session. Do NOT add windows to user's external sessions — use create_session instead."""
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")

    existing = tmux.list_windows(session)
    if name in existing:
        raise ValueError(f"Window '{name}' already exists in session '{session}'")

    args = ["tmux", "new-window", "-t", session, "-n", name]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    if cmd:
        args.append(tmux.wrap_cmd(cmd))
    subprocess.run(args, capture_output=True)

    if session not in registry._sessions:
        registry._sessions[session] = {
            "owner": EXTERNAL,
            "created_at": time.time(),
            "windows": {}
        }
    registry._sessions[session]["windows"][name] = {"owner": MANAGED}

    pane_id = f"{session}:{name}.0"
    registry._working_panes[pane_id] = {"description": f"managed ({name})", "owner": MANAGED}

    return f"Created window '{name}' in session '{session}'.\nRegistered pane: {pane_id}"


@mcp.tool()
@_plain_defaults
def kill_window(session: str, window: str, force: bool = Field(False, description="force kill external (non-managed) window")) -> str:
    """Kill a window. Killing the last window destroys the session — use create_session to recreate.
    Managed windows are killed immediately. External windows require force=True."""
    if not check_session(session):
        raise ValueError(f"Session '{session}' does not exist")

    window_name = tmux.resolve_window(session, window)

    owner = registry._sessions.get(session, {}).get("windows", {}).get(window_name, {}).get("owner", EXTERNAL)
    registry.check_ownership("Window", f"{session}:{window_name}", owner, force)

    registry.cleanup_window_resources(session, window_name)
    subprocess.run(["tmux", "kill-window", "-t", f"={session}:{window_name}"], capture_output=True)

    return f"Killed window '{window_name}' in session '{session}'"


@mcp.tool()
@_plain_defaults
def set_pane(pane: str, description: str) -> str:
    """Register a pane for use. Re-calling updates description."""
    try:
        tmux.parse_pane_id(pane)
    except ValueError:
        raise ValueError(f"Invalid pane id '{pane}': expected 'session:window.idx' format")
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")

    registry._working_panes[pane] = {"description": description, "owner": EXTERNAL}
    registry.auto_register_session_window(pane)
    return f"Registered: {pane} ({description})"


@mcp.tool()
@_plain_defaults
def remove_pane(pane: str) -> str:
    """Unregister a pane."""
    if pane not in registry._working_panes:
        raise ValueError(f"Pane '{pane}' is not registered")

    del registry._working_panes[pane]
    return f"Removed: {pane}"


def _respawn_single(pane: str, start_dir: str = None, cmd: str = None) -> str:
    """Respawn a single registered pane. Returns status string."""
    try:
        run_tmux_cmd(["list-panes", "-t", pane], raise_on_error=True)
    except RuntimeError as e:
        raise ValueError(f"Pane '{pane}' does not exist: {e}")
    cleaned = registry.cleanup_pane_tasks(pane)
    cmd = cmd or "bash"
    args = ["tmux", "respawn-pane", "-k", "-t", pane]
    if start_dir:
        args.extend(["-c", os.path.expanduser(start_dir)])
    args.append(tmux.wrap_cmd(cmd))
    subprocess.run(args, capture_output=True)
    desc = registry._working_panes[pane]["description"]
    parts = [f"Respawned: {pane} ({desc})"]
    if cleaned:
        parts.append(f"Cleaned {cleaned} task(s)")
    return "\n".join(parts)


@mcp.tool()
@_plain_defaults
def respawn_pane(pane: str = None, panes: list[str] = None, start_dir: str = None, cmd: str = None, cmds: list[str] = None) -> str:
    """Kill the running process in a pane and start a fresh shell.
    Cleans up associated tasks and locks. Registration (description/owner) is preserved."""
    multi = _resolve_panes(pane, panes)
    _validate_multi(cmd, cmds, "cmds", multi)
    if multi is not None:
        results = {p: _respawn_single(p, start_dir, cmds[i] if cmds else cmd) for i, p in enumerate(multi)}
        return _format_multi_result(results)
    check_pane_registered(pane)
    return _respawn_single(pane, start_dir, cmd)


# =============================================================================
# Raw input / capture tools
# =============================================================================

def _map_panes(target_panes: list[str], fn) -> str:
    """Apply fn to each pane (skipping missing ones) and format grouped result."""
    results = {}
    for p in target_panes:
        results[p] = fn(p) if check_session(p) else "not found (skipped)"
    return _format_multi_result(results)


@mcp.tool()
@_plain_defaults
def send_text(pane: str = None, text: str = "", enter: bool = Field(True, description="press Enter after text"), panes: list[str] = None) -> str:
    """Send text string to pane(s). For commands, passwords, etc."""
    check_deny(text, "send_text")
    target_panes = _resolve_panes(pane, panes)

    def send(p):
        run_tmux_cmd(["send-keys", "-t", p, text], capture=False)
        if enter:
            run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        return "sent"

    if target_panes is not None:
        return _map_panes(target_panes, send)

    require_pane(pane)
    send(pane)
    return "Text sent"


@mcp.tool()
@_plain_defaults
def send_keys(pane: str = None, keys: str = "", enter: bool = Field(False, description="press Enter after keys"), panes: list[str] = None) -> str:
    """Send special keys to pane(s). For C-c, Enter, Escape, arrow keys, etc."""
    target_panes = _resolve_panes(pane, panes)

    def send(p):
        for key in keys.split():
            run_tmux_cmd(["send-keys", "-t", p, key], capture=False)
        if enter:
            run_tmux_cmd(["send-keys", "-t", p, "Enter"], capture=False)
        return "sent"

    if target_panes is not None:
        return _map_panes(target_panes, send)

    require_pane(pane)
    send(pane)
    return "Keys sent"


def _capture_single_pane(p: str, tail: int, rel_range: str, since_marker: str, filter_kwargs: dict) -> str:
    """Capture and filter a single pane."""
    if since_marker:
        # Marker could be far back — use progressive capture
        raw = tmux.capture_until(p, lambda r: since_marker in r)
        lines = tmux.split_capture(raw)
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
        raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-J", "-S", f"-{n_capture}"])
        all_lines = tmux.split_capture(raw)

    if rel_range:
        start, end = parse_rel_range(rel_range)
        all_lines = all_lines[start:end]
    else:
        if tail > 0 and len(all_lines) > tail:
            all_lines = all_lines[-tail:]

    return apply_output_filters(all_lines, n_negative=True, **filter_kwargs)


@mcp.tool()
@_plain_defaults
def capture_pane(
    pane: str = None,
    tail: int = Field(5, description="lines from end (grep searches within this)"),
    rel_range: str = Field(None, description="relative range from end, e.g. '100:50'"),
    grep: str = Field(None, description="filter within tail range, does NOT expand it"),
    v: str = Field(None, description="exclude matching (grep -v)"),
    i: bool = Field(False, description="case insensitive (grep -i)"),
    w: bool = Field(False, description="whole word match (grep -w)"),
    F: bool = Field(False, description="literal string, not regex (grep -F)"),
    m: int = Field(None, description="max matching lines (grep -m)"),
    A: int = Field(None, description="lines after match (grep -A)"),
    B: int = Field(None, description="lines before match (grep -B)"),
    C: int = Field(None, description="context lines around match (grep -C)"),
    since_marker: str = Field(None, description="only capture after this marker"),
    uniq: bool = True,
    n: bool = Field(False, description="show line numbers (grep -n)"),
    strip_tqdm: bool = Field(False, description="remove tqdm lines, keep last group"),
    save: str = None,
    append: bool = True,
    prefix: str = None,
    suffix: str = None,
    panes: list[str] = None
) -> str:
    """Capture pane content. tail= sets capture range, grep= filters within it."""
    target_panes = _resolve_panes(pane, panes)
    fkw = dict(grep=grep, v=v, i=i, w=w, F=F, m=m, A=A, B=B, C=C, n=n,
               uniq=uniq, save=save, append=append, prefix=prefix, suffix=suffix,
               strip_tqdm=strip_tqdm)

    if target_panes is not None:
        return _map_panes(target_panes, lambda p: _capture_single_pane(p, tail, rel_range, since_marker, fkw))

    require_pane(pane)
    return _capture_single_pane(pane, tail, rel_range, since_marker, fkw)


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        sys.exit(cli_watch(sys.argv[2:]))
    # Strip .venv from PATH so tmux panes don't inherit virtualenv pollution
    os.environ["PATH"] = ":".join(
        p for p in os.environ.get("PATH", "").split(":") if "/.venv/" not in p
    )
    os.environ.pop("VIRTUAL_ENV", None)
    mcp.run(transport="stdio")
