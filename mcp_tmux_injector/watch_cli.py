"""Standalone watch CLI (run via `mcp-tmux-injector watch ...`) and the
fingerprint machinery shared with poll_pane.

The CLI runs as a separate process (spawned by the client's Monitor tool), so
it must not depend on server in-memory state.
"""
import json
import random
import re
import shlex
import time
from pathlib import Path

from .codec import check_end_marker
from .config import SERVER_BIN
from .filters import TQDM_PROGRESS_LINE
from .tmux import run_tmux_cmd, split_capture


def find_fingerprint(lines: list[str], fingerprint: list[str]) -> int | None:
    """Find the FIRST occurrence of fingerprint sequence in lines.

    Returns the index of the first line AFTER the fingerprint match,
    or None if not found.
    """
    fp_len = len(fingerprint)
    if fp_len == 0:
        return None
    for i in range(len(lines) - fp_len + 1):
        if lines[i:i + fp_len] == fingerprint:
            return i + fp_len
    return None


def build_fingerprint(p: str) -> tuple[list[str], int]:
    """Snapshot current pane state for only_new mode polling.

    Returns (fingerprint_lines, total_line_count).
    fingerprint_lines: last ≤50 stable (non-progress-bar) lines.
    """
    raw = run_tmux_cmd(["capture-pane", "-t", p, "-p", "-J", "-S", "-200"], raise_on_error=True)
    initial_lines = split_capture(raw)
    stable = [l for l in initial_lines if not TQDM_PROGRESS_LINE.search(l)]
    fp_size = min(50, len(stable))
    return stable[-fp_size:] if fp_size > 0 else [], len(initial_lines)


def get_fresh_lines(lines: list[str], fingerprint: list[str], fingerprint_total: int) -> list[str]:
    """Return lines that appeared after the fingerprint snapshot.

    - Fingerprint found: lines after it.
    - Fingerprint's last line mutated (interactive prompt got a command
      typed onto it: "$" -> "$ cmd"): match without it; the mutated line
      counts as fresh — it IS new content.
    - Fingerprint scrolled out (50+ new lines): all lines (old content gone too).
    - Fingerprint changed by progress bars: empty list (wait more).
    """
    if fingerprint:
        stable_lines = [l for l in lines if not TQDM_PROGRESS_LINE.search(l)]
        fp_end_stable = find_fingerprint(stable_lines, fingerprint)
        if fp_end_stable is None and len(fingerprint) > 1:
            fp_end_stable = find_fingerprint(stable_lines, fingerprint[:-1])
        if fp_end_stable is not None:
            count = 0
            cutoff = len(lines)
            for i, line in enumerate(lines):
                if not TQDM_PROGRESS_LINE.search(line):
                    count += 1
                    if count == fp_end_stable:
                        cutoff = i + 1
                        break
            return lines[cutoff:]
        elif len(lines) >= fingerprint_total + 50:
            return lines
        else:
            return []
    else:
        return lines[fingerprint_total:] if len(lines) > fingerprint_total else []


def write_watch_script(inner_cmd: str, extra_cleanup: list[str] = None) -> str:
    """Write a self-deleting bash wrapper that runs inner_cmd. Returns its path.

    The wrapper traps EXIT (covers normal exit, SIGTERM from Monitor timeout,
    pattern-match exits). On exit it removes itself plus any extra files.
    """
    suffix = f"{int(time.time()*1000):x}_{random.randint(0, 0xFFFF):04x}"
    path = Path("/tmp") / f"tmix_w_{suffix}.sh"
    targets = ['"$0"'] + [shlex.quote(p) for p in (extra_cleanup or [])]
    cleanup = " ".join(targets)
    content = (
        "#!/bin/bash\n"
        f"trap 'rm -f -- {cleanup}' EXIT\n"
        f"{inner_cmd}\n"
    )
    path.write_text(content)
    path.chmod(0o755)
    return str(path)


def build_watch_cmd_task(task_id: str, pane: str, end: str) -> str:
    """Wrap the task watch invocation in a self-deleting script for Monitor."""
    inner = (
        f"{shlex.quote(SERVER_BIN)} watch task "
        f"--task-id {shlex.quote(task_id)} "
        f"--pane {shlex.quote(pane)} "
        f"--end {shlex.quote(end)}"
    )
    return write_watch_script(inner)


def build_watch_cmd_pane(pane: str, pattern: str, fp_path: Path,
                         fp_total: int, only_new: bool, ignore_case: bool, literal: bool) -> str:
    """Wrap the pane watch invocation in a self-deleting script for Monitor.
    Also cleans up the fingerprint file (the CLI also tries; trap is backstop).
    """
    parts = [
        f"{shlex.quote(SERVER_BIN)} watch pane",
        f"--pane {shlex.quote(pane)}",
        f"--pattern {shlex.quote(pattern)}",
        f"--fingerprint-file {shlex.quote(str(fp_path))}",
        f"--fingerprint-total {fp_total}",
    ]
    if only_new:
        parts.append("--only-new")
    if ignore_case:
        parts.append("-i")
    if literal:
        parts.append("-F")
    inner = " ".join(parts)
    return write_watch_script(inner, extra_cleanup=[str(fp_path)])


def build_watch_cmd_mem(pane: str, rss_gb: float | None,
                        gpu_gb: float | None, poll: float) -> str:
    """Wrap the memory watch invocation in a self-deleting script for Monitor."""
    parts = [
        f"{shlex.quote(SERVER_BIN)} watch mem",
        f"--pane {shlex.quote(pane)}",
        f"--poll {poll}",
    ]
    if rss_gb is not None:
        parts.append(f"--rss-gb {rss_gb}")
    if gpu_gb is not None:
        parts.append(f"--gpu-gb {gpu_gb}")
    return write_watch_script(" ".join(parts))


def _cli_watch_mem(pane: str, rss_gb: float | None,
                   gpu_gb: float | None, poll: float) -> int:
    """Watch CLI: poll a pane's process tree, print one line when it crosses a cap.

    Exits on the first breach, so the notification IS the event. Fixed interval
    rather than the backoff the other watchers use: a cap breach is worth
    knowing about promptly, and each check is a single `ps` sweep.

    Also exits if the tree disappears — otherwise a watch on a finished job
    would sit armed until timeout, and silence would read as "still under cap".
    """
    from .mem import (fmt_kb, gpu_by_pid, host_mem, pane_pid, proc_snapshot,
                      tree_gpu, tree_rss)

    try:
        root = pane_pid(pane)
    except Exception as e:
        print(f"[error] pane {pane}: {e}", flush=True)
        return 1

    rss_cap_kb = rss_gb * 1048576 if rss_gb is not None else None
    gpu_cap_mib = gpu_gb * 1024 if gpu_gb is not None else None

    while True:
        try:
            snap = proc_snapshot()
            rss_kb, rss_rows = tree_rss(root, snap)
            if not rss_rows:
                print(f"[gone] pane {pane}: process tree exited", flush=True)
                return 0

            gpu_mib, gpu_rows = 0, []
            if gpu_cap_mib is not None:
                gpu_mib, gpu_rows = tree_gpu(root, snap, gpu_by_pid())

            breach, rows, is_gpu = None, [], False
            if rss_cap_kb is not None and rss_kb > rss_cap_kb:
                breach = f"RSS {fmt_kb(rss_kb)} > cap {rss_gb} GB"
                rows = rss_rows
            elif gpu_cap_mib is not None and gpu_mib > gpu_cap_mib:
                breach = f"GPU {gpu_mib / 1024:.2f} GB > cap {gpu_gb} GB"
                rows, is_gpu = gpu_rows, True

            if breach:
                top = ", ".join(
                    f"{c}({p}) " + (f"{v / 1024:.2f} GB" if is_gpu else fmt_kb(v))
                    for p, c, v in rows[:3]
                )
                hm = host_mem()
                host = (f" | host avail {hm['available']:.0f} GB, "
                        f"swap used {hm['swap_used']:.1f} GB") if hm else ""
                print(f"[cap] {pane}: {breach} | top: {top}{host}", flush=True)
                return 0
        except Exception as e:
            print(f"[error] pane {pane}: {e}", flush=True)
            return 1
        time.sleep(poll)


def _cli_watch_task(pane: str, end: str, task_id: str) -> int:
    """Watch CLI: poll for end marker. Print one line and exit on completion."""
    interval = 0.5
    max_interval = 10.0
    while True:
        try:
            if check_end_marker(pane, end):
                print(f"[done] task {task_id}", flush=True)
                return 0
        except Exception as e:
            print(f"[error] task {task_id}: {e}", flush=True)
            return 1
        time.sleep(interval)
        interval = min(interval * 2, max_interval)


def _cli_watch_pane(pane: str, pattern: str, fp_path: Path, fp_total: int,
                    only_new: bool, ignore_case: bool, literal: bool) -> int:
    """Watch CLI: poll for pattern. Print first matching line and exit."""
    flags = re.IGNORECASE if ignore_case else 0
    pat = re.escape(pattern) if literal else pattern
    regex = re.compile(pat, flags)

    fp_lines: list[str] = []
    if only_new and fp_path.exists():
        fp_lines = json.loads(fp_path.read_text()).get("lines", [])

    interval = 0.5
    max_interval = 10.0
    try:
        while True:
            try:
                raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", "-200"], raise_on_error=True)
                lines = split_capture(raw)
                search = get_fresh_lines(lines, fp_lines, fp_total) if only_new else lines
                for line in search:
                    if regex.search(line):
                        print(f"[match] {line}", flush=True)
                        return 0
            except Exception as e:
                print(f"[error] pane {pane}: {e}", flush=True)
                return 1
            time.sleep(interval)
            interval = min(interval * 2, max_interval)
    finally:
        try:
            fp_path.unlink()
        except (OSError, FileNotFoundError):
            pass


def cli_watch(args: list[str]) -> int:
    """CLI entry point dispatched from main() when argv[1] == 'watch'."""
    import argparse
    p = argparse.ArgumentParser(prog="mcp-tmux-injector watch")
    sub = p.add_subparsers(dest="kind", required=True)

    pt = sub.add_parser("task")
    pt.add_argument("--task-id", required=True)
    pt.add_argument("--pane", required=True)
    pt.add_argument("--end", required=True)

    pp = sub.add_parser("pane")
    pp.add_argument("--pane", required=True)
    pp.add_argument("--pattern", required=True)
    pp.add_argument("--fingerprint-file", required=True)
    pp.add_argument("--fingerprint-total", type=int, default=0)
    pp.add_argument("--only-new", action="store_true")
    pp.add_argument("-i", dest="ignore_case", action="store_true")
    pp.add_argument("-F", dest="literal", action="store_true")

    pm = sub.add_parser("mem")
    pm.add_argument("--pane", required=True)
    pm.add_argument("--rss-gb", type=float, default=None)
    pm.add_argument("--gpu-gb", type=float, default=None)
    pm.add_argument("--poll", type=float, default=30.0)

    ns = p.parse_args(args)
    if ns.kind == "task":
        return _cli_watch_task(ns.pane, ns.end, ns.task_id)
    if ns.kind == "mem":
        return _cli_watch_mem(ns.pane, ns.rss_gb, ns.gpu_gb, ns.poll)
    return _cli_watch_pane(
        ns.pane, ns.pattern, Path(ns.fingerprint_file),
        ns.fingerprint_total, ns.only_new, ns.ignore_case, ns.literal,
    )
