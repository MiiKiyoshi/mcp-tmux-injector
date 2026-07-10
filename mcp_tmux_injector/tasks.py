"""Background task registry, pane locks, and completion tracking."""
import asyncio
import threading
import time

from .codec import check_end_marker, extract_output
from .tmux import capture_until

# Background tasks storage: task_id -> task_info
_tasks: dict[str, dict] = {}

# Pane locks: pane -> Lock (prevents concurrent execution on same pane)
_pane_locks: dict[str, threading.Lock] = {}
_pane_locks_lock = threading.Lock()  # Lock for accessing _pane_locks dict

_MAX_COMPLETED_TASKS = 20
BLOCKING_TIMEOUT_MAX = 60.0


def get_pane_lock(pane: str) -> threading.Lock:
    """Get or create a lock for a specific pane."""
    with _pane_locks_lock:
        if pane not in _pane_locks:
            _pane_locks[pane] = threading.Lock()
        return _pane_locks[pane]


def is_pane_busy(pane: str) -> bool:
    """Check if pane has a running task."""
    return get_pane_lock(pane).locked()


def acquire_pane_lock(pane: str) -> threading.Lock:
    """Acquire pane lock. If busy, check if existing task completed first."""
    lock = get_pane_lock(pane)
    if lock.acquire(blocking=False):
        return lock

    for task_id, task in list(_tasks.items()):
        if task['pane'] == pane and 'lock' in task:
            if refresh_task(task):
                if lock.acquire(blocking=False):
                    return lock

    raise RuntimeError(f"Pane '{pane}' is busy with another task")


def _cleanup_completed_tasks():
    """Remove oldest completed tasks when exceeding _MAX_COMPLETED_TASKS."""
    completed = [(tid, t) for tid, t in list(_tasks.items()) if "end_time" in t]
    if len(completed) <= _MAX_COMPLETED_TASKS:
        return
    completed.sort(key=lambda x: x[1]["end_time"])
    for tid, _ in completed[:-_MAX_COMPLETED_TASKS]:
        _tasks.pop(tid, None)


def finalize_task(task: dict) -> None:
    """Record end_time and release lock. Safe to call from multiple threads."""
    if "end_time" not in task:
        task["end_time"] = time.time()
        _cleanup_completed_tasks()
    lock = task.pop("lock", None)
    if lock is not None:
        lock.release()


def check_task_output(session: str, begin: str, end: str) -> tuple[str, bool]:
    """Check current output for a task. Returns (output, is_complete)."""
    def found(raw: str) -> bool:
        output, completed = extract_output(raw, begin, end)
        return completed or begin in raw

    raw = capture_until(session, found)
    return extract_output(raw, begin, end)


def refresh_task(task: dict) -> bool:
    """Check whether a task has completed; cache output and finalize if so.

    A dead pane (capture RuntimeError) marks the task as errored+completed.
    Returns True if the task is complete (successfully or with error).
    """
    if "end_time" in task:
        return True
    try:
        if not check_end_marker(task["pane"], task["end"]):
            return False
        output, completed = check_task_output(task["pane"], task["begin"], task["end"])
        if completed:
            task["cached_output"] = output
            finalize_task(task)
        return completed
    except RuntimeError as e:
        task["error"] = str(e)
        task["cached_output"] = f"[error] {e}"
        finalize_task(task)
        return True


async def capture_output_blocking(session: str, begin: str, end: str, timeout: float) -> str:
    """Capture output between markers (blocking). Timeout is capped to 60s."""
    timeout = min(timeout, BLOCKING_TIMEOUT_MAX)
    start_time = time.time()

    while time.time() - start_time < timeout:
        await asyncio.sleep(0.1)
        if not check_end_marker(session, end):
            continue
        output, completed = check_task_output(session, begin, end)
        if completed:
            return output

    raise TimeoutError(f"Command did not complete within {timeout}s")


def find_active_task_on_pane(pane: str) -> tuple[str, dict] | None:
    """Find task on a pane. Running task takes priority, then most recent completed."""
    latest = None
    latest_time = 0
    for task_id, task in list(_tasks.items()):
        if task["pane"] == pane:
            if not refresh_task(task):
                return task_id, task
            if task["start_time"] > latest_time:
                latest_time = task["start_time"]
                latest = (task_id, task)
    return latest


def watch_task_completion(task_id: str) -> None:
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
            found = check_end_marker(task["pane"], task["end"])
        except RuntimeError as e:
            task["error"] = str(e)
            task["cached_output"] = f"[error] {e}"
            finalize_task(task)
            return
        except Exception:
            time.sleep(interval)
            interval = min(interval * 2, max_interval)
            continue
        if found:
            # End marker found — do full extraction once
            output, completed = check_task_output(task["pane"], task["begin"], task["end"])
            if completed:
                task["cached_output"] = output
                finalize_task(task)
                return
        time.sleep(interval)
        interval = min(interval * 2, max_interval)


def cmd_display(cmd: str, width: int = 40) -> str:
    """Shorten a command for one-line status display."""
    short = (cmd[:width - 3] + "...") if len(cmd) > width else cmd
    return short.replace('\n', ' ')
