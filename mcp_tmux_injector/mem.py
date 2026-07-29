"""Per-pane memory accounting — host RAM and GPU.

A tmux pane owns a process tree: the pane's shell, whatever it launched, and
that program's children. `ps` alone reports single processes, so a pane running
an EDA tool or a trainer that forks helpers under-reports badly. These helpers
walk the tree from the pane's shell pid and sum both RSS and GPU memory across
it — a pane can blow up on either one, and which one it is decides what you do
about it.

Used by the `mem_pane` tool (point-in-time reading) and by `watch_mem` /
`mcp-tmux-injector watch mem` (fire once when a pane crosses a cap).
"""
import subprocess
from collections import defaultdict

from .tmux import run_tmux_cmd


def proc_snapshot() -> tuple[dict[int, list[int]], dict[int, tuple[int, str]]]:
    """One `ps` sweep -> (children by ppid, {pid: (rss_kb, comm)}).

    Taken in a single call so a large tree costs one process spawn, not one per
    node, and so the numbers are consistent with each other.
    """
    children: dict[int, list[int]] = defaultdict(list)
    info: dict[int, tuple[int, str]] = {}
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,rss=,comm="],
        capture_output=True, text=True, timeout=10,
    ).stdout
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        info[pid] = (rss, parts[3])
        children[ppid].append(pid)
    return children, info


def pane_pid(pane: str) -> int:
    """tmux pane id -> pid of the pane's shell.

    Raises with the pane name when it does not exist; tmux returns an empty
    string there, and the bare int() failure that produced would tell a caller
    nothing about which pane was wrong.
    """
    out = run_tmux_cmd(
        ["display-message", "-p", "-t", pane, "#{pane_pid}"], raise_on_error=True
    ).strip()
    if not out.isdigit():
        raise ValueError(f"pane not found: {pane}")
    return int(out)


def tree_rss(root_pid: int, snapshot=None) -> tuple[int, list[tuple[int, str, int]]]:
    """Total RSS (kB) of root_pid and all descendants, plus the per-process rows.

    Rows come back sorted heaviest first, which is what you want when a pane
    blows up and you need to know which process did it.
    """
    children, info = snapshot if snapshot else proc_snapshot()
    total = 0
    rows: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in info:
            rss, comm = info[pid]
            total += rss
            rows.append((pid, comm, rss))
        stack.extend(children.get(pid, ()))
    rows.sort(key=lambda r: -r[2])
    return total, rows


def pane_rss(pane: str, snapshot=None) -> tuple[int, list[tuple[int, str, int]]]:
    """Convenience: resolve the pane then sum its tree."""
    return tree_rss(pane_pid(pane), snapshot)


def gpu_by_pid() -> dict[int, int]:
    """{pid: GPU MiB}, summed when a pid holds memory on several devices.

    nvidia-smi rather than gpustat: it ships with the driver, so a watch process
    spawned on any host can read it. Absent GPU / driver -> empty dict, and
    callers treat that as "no GPU usage" rather than an error.
    """
    usage: dict[int, int] = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                pid, mib = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            usage[pid] = usage.get(pid, 0) + mib
    except Exception:
        pass
    return usage


def tree_gpu(root_pid: int, snapshot=None, gpu=None) -> tuple[int, list[tuple[int, str, int]]]:
    """GPU MiB held by root_pid's tree, plus per-process rows (heaviest first).

    Only processes that actually hold GPU memory appear in the rows.
    """
    children, info = snapshot if snapshot else proc_snapshot()
    gpu = gpu if gpu is not None else gpu_by_pid()
    total = 0
    rows: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        mib = gpu.get(pid)
        if mib:
            total += mib
            rows.append((pid, info.get(pid, (0, "?"))[1], mib))
        stack.extend(children.get(pid, ()))
    rows.sort(key=lambda r: -r[2])
    return total, rows


def fmt_kb(kb: int) -> str:
    """kB -> human size. Keeps one decimal from MB up, since these are RSS
    readings people compare against a cap ('3.0 GB' vs '2.9 GB' matters)."""
    if kb < 1024:
        return f"{kb} kB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def host_mem() -> dict[str, float]:
    """Host-wide memory in GB: total / available / swap_used.

    A pane can look fine while the machine is thrashing, so cap checks report
    this alongside the pane's own number.
    """
    vals: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                k, _, v = line.partition(":")
                mi[k] = float(v.strip().split()[0]) / 1048576  # kB -> GB
        vals["total"] = mi.get("MemTotal", 0.0)
        vals["available"] = mi.get("MemAvailable", 0.0)
        vals["swap_used"] = mi.get("SwapTotal", 0.0) - mi.get("SwapFree", 0.0)
    except Exception:
        pass
    return vals
