"""Background watchers for poll_pane and watch_mem, and the poll fingerprints.

Each watcher is a daemon thread inside the server. It polls, and when its
condition is met it appends one report to the session's event stream
(events.emit) and ends. Nothing runs outside the server process.
"""
import re
import time

from . import events
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


def watch_mem(pane: str | None, session: str | None, rss_gb: float | None,
              gpu_gb: float | None, poll: float) -> None:
    """Thread: poll a pane or a whole session, report once when it crosses a cap.

    Fixed interval rather than the backoff the other watchers use: a cap breach
    is worth knowing about promptly, and each check is a single `ps` sweep.

    Session scope exists because a cap on one pane measures the wrong thing
    whenever a job spans several: a fold running Solver in one pane and the
    trainer in another sat at 6.4 + 4.2 GiB and never tripped a 10 GB per-pane
    cap, while the number a human reads off the session was 10.6 GiB. The
    breach report is a per-pane table so the total is immediately attributable.

    Also reports if the tree disappears, so silence never reads as "still
    under cap".
    """
    from .mem import (fmt_gib, fmt_table, gpu_by_pid, host_mem, mem_rows,
                      pane_pid, proc_snapshot, tree_gpu, tree_rss)

    scope = f"session {session}" if session else f"pane {pane}"
    want_gpu = gpu_gb is not None
    rss_cap_kb = rss_gb * 1048576 if rss_gb is not None else None
    gpu_cap_mib = gpu_gb * 1024 if gpu_gb is not None else None

    root = None
    if session is None:
        try:
            root = pane_pid(pane)
        except Exception as e:
            events.emit(f"[error] {scope}: {e}")
            return

    while True:
        try:
            snap = proc_snapshot()
            gpu_map = gpu_by_pid() if want_gpu else None

            if session is not None:
                rows = [(p, rss, mib) for _s, p, rss, mib
                        in mem_rows(session, snap, gpu_map)]
            else:
                rss_kb, _ = tree_rss(root, snap)
                mib = tree_gpu(root, snap, gpu_map)[0] if want_gpu else 0
                rows = [(pane, rss_kb, mib)]

            total_kb = sum(r[1] for r in rows)
            total_mib = sum(r[2] for r in rows)
            if total_kb == 0:
                events.emit(f"[gone] {scope}: process tree exited")
                return

            breach = None
            if rss_cap_kb is not None and total_kb > rss_cap_kb:
                breach = f"CPU {fmt_gib(total_kb)} > cap {rss_gb} GiB"
            elif gpu_cap_mib is not None and total_mib > gpu_cap_mib:
                breach = f"GPU {total_mib / 1024:.1f} GiB > cap {gpu_gb} GiB"

            if breach:
                hm = host_mem()
                host = (f"host avail {hm['available']:.0f} GB, "
                        f"swap used {hm['swap_used']:.1f} GB") if hm else ""
                table = fmt_table(rows, "PANE", gpu=want_gpu)
                events.emit(f"[cap] {scope}: {breach}\n{table}\n{host}")
                return
        except Exception as e:
            events.emit(f"[error] {scope}: {e}")
            return
        time.sleep(poll)


def watch_pane(pane: str, pattern: str, fp_lines: list[str], fp_total: int,
               only_new: bool, ignore_case: bool, literal: bool) -> None:
    """Thread: poll for a pattern, report the first matching line."""
    flags = re.IGNORECASE if ignore_case else 0
    pat = re.escape(pattern) if literal else pattern
    regex = re.compile(pat, flags)

    interval = 0.5
    max_interval = 10.0
    while True:
        try:
            raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", "-200"], raise_on_error=True)
            lines = split_capture(raw)
            search = get_fresh_lines(lines, fp_lines, fp_total) if only_new else lines
            for line in search:
                if regex.search(line):
                    events.emit(f"[match] {pane}: {line}")
                    return
        except Exception as e:
            events.emit(f"[error] pane {pane}: {e}")
            return
        time.sleep(interval)
        interval = min(interval * 2, max_interval)
