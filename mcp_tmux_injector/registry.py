"""Session/window/pane registries, ownership, and resource cleanup."""
import time

from . import tasks
from .tmux import check_session, list_windows, parse_pane_id

# Ownership constants
MANAGED = "managed"
EXTERNAL = "external"

# Session registry: session_name -> {owner, created_at, windows: {name -> {owner}}}
_sessions: dict[str, dict] = {}

# Registered working panes: pane -> {description, owner}
_working_panes: dict[str, dict] = {}


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


def require_pane(pane: str) -> None:
    """Raise unless pane is registered AND its tmux session exists."""
    check_pane_registered(pane)
    if not check_session(pane):
        raise ValueError(f"Pane '{pane}' not found in tmux")


def auto_register_session_window(pane: str) -> None:
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


def session_info_str(session_name: str) -> str:
    """Format session info for error messages."""
    if session_name not in _sessions:
        return f"Session '{session_name}': not in registry"
    info = _sessions[session_name]
    lines = [f"Session '{session_name}' ({info['owner']}):"]
    windows = list_windows(session_name)
    for w in windows:
        w_owner = info["windows"].get(w, {}).get("owner", "unknown")
        panes_in_window = [p for p in _working_panes if p.startswith(f"{session_name}:{w}.")]
        pane_str = f" [{len(panes_in_window)} pane(s) registered]" if panes_in_window else ""
        lines.append(f"  {w} ({w_owner}){pane_str}")
    return '\n'.join(lines)


def check_ownership(resource_type: str, name: str, owner: str, force: bool) -> None:
    """Raise error if resource is external and force is False."""
    if owner == EXTERNAL and not force:
        session_name = name.split(":")[0] if resource_type == "Window" else name
        info_str = session_info_str(session_name)
        raise ValueError(
            f"{resource_type} '{name}' is {EXTERNAL} (not created by MCP).\n"
            f"Use force=True to override (requires user confirmation).\n\n"
            f"{info_str}"
        )


def cleanup_session_resources(name: str) -> None:
    """Clean up tasks, panes, and registry when a session is deleted."""
    for task_id in list(tasks._tasks.keys()):
        task = tasks._tasks.get(task_id)
        if not task:
            continue
        try:
            session, _, _ = parse_pane_id(task["pane"])
        except (ValueError, IndexError):
            continue
        if session == name:
            tasks.finalize_task(task)
            tasks._tasks.pop(task_id, None)
    for pane in list(_working_panes.keys()):
        try:
            session, _, _ = parse_pane_id(pane)
        except (ValueError, IndexError):
            continue
        if session == name:
            del _working_panes[pane]
    if name in _sessions:
        del _sessions[name]


def cleanup_pane_tasks(pane: str) -> int:
    """Finalize and remove all tasks for a specific pane. Returns count."""
    count = 0
    for task_id in list(tasks._tasks.keys()):
        task = tasks._tasks.get(task_id)
        if not task:
            continue
        if task["pane"] == pane:
            tasks.finalize_task(task)
            tasks._tasks.pop(task_id, None)
            count += 1
    return count


def cleanup_window_resources(session: str, window: str) -> None:
    """Clean up tasks, panes, and registry when a window is deleted."""
    prefix = f"{session}:{window}."
    for task_id in list(tasks._tasks.keys()):
        task = tasks._tasks.get(task_id)
        if not task:
            continue
        if task["pane"].startswith(prefix):
            tasks.finalize_task(task)
            tasks._tasks.pop(task_id, None)
    for pane in list(_working_panes.keys()):
        if pane.startswith(prefix):
            del _working_panes[pane]
    if session in _sessions and window in _sessions[session]["windows"]:
        del _sessions[session]["windows"][window]
