"""tmux primitives: run commands, capture panes, query sessions/windows."""
import shlex
import subprocess
import time

from .config import TMUX_SOCKET_PATH


def build_tmux_command(args: list[str]) -> list[str]:
    """Build a tmux command using the configured socket when present."""
    command = ["tmux"]
    if TMUX_SOCKET_PATH is not None:
        command.extend(["-S", TMUX_SOCKET_PATH])
    command.extend(args)
    return command


def run_tmux_cmd(args: list[str], capture: bool = True, raise_on_error: bool = False) -> str:
    """Run a tmux command and return output."""
    result = subprocess.run(
        build_tmux_command(args),
        capture_output=capture,
        text=True
    )
    if raise_on_error and result.returncode != 0:
        reason = result.stderr.strip() if capture else ""
        raise RuntimeError(reason or "tmux command failed")
    return result.stdout if capture else ""


def wrap_cmd(cmd: str) -> str:
    """Wrap cmd in bash so pane survives process exit/crash. -i loads ~/.bashrc aliases."""
    return f"bash -ic {shlex.quote(cmd + '; exec bash -i')}"


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
        build_tmux_command(["has-session", "-t", f"={sess_name}"]),
        capture_output=True
    )
    exists = result.returncode == 0
    _session_cache[sess_name] = (exists, now)
    return exists


def forget_session(name: str) -> None:
    """Drop a session from the existence cache (after kill-session)."""
    _session_cache.pop(name, None)


def split_capture(raw: str) -> list[str]:
    """Split capture-pane output into lines, stripping -J trailing spaces."""
    return [line.rstrip() for line in raw.rstrip().split('\n')]


def capture_until(pane: str, found) -> str:
    """Capture pane scrollback progressively (1000 → 4000 → 16000 → full)
    until found(raw) is truthy. Returns the last capture either way."""
    for n_lines in [1000, 4000, 16000]:
        raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", f"-{n_lines}"])
        if found(raw):
            return raw
    return run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", "-"])


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


def list_sessions() -> list[dict]:
    """List available tmux sessions."""
    result = subprocess.run(
        build_tmux_command(["list-sessions", "-F", "#{session_name}:#{session_windows}:#{session_attached}"]),
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


def list_windows(session: str) -> list[str]:
    """List window names in a tmux session."""
    result = subprocess.run(
        build_tmux_command(["list-windows", "-t", f"={session}", "-F", "#{window_name}"]),
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [w.strip() for w in result.stdout.strip().split('\n') if w.strip()]


def resolve_window(session: str, window: str) -> str:
    """Resolve window identifier (name or index) to window name.
    Returns the window name, or raises ValueError if not found."""
    names = list_windows(session)
    if window in names:
        return window
    result = subprocess.run(
        build_tmux_command(["list-windows", "-t", f"={session}", "-F", "#{window_index}|#{window_name}"]),
        capture_output=True, text=True
    )
    if result.returncode == 0:
        for line in result.stdout.strip().split('\n'):
            idx, name = line.strip().split('|', 1)
            if idx == window:
                return name
    raise ValueError(f"Window '{window}' not found in session '{session}'")
