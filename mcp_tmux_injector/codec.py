"""Marker generation, code delivery to panes, and output extraction.

All delivery travels as keystrokes (tmux send-keys), so it works identically
for local panes and panes running remote (ssh) shells/REPLs — no shared
filesystem is assumed.
"""
import base64
import random
import textwrap
import time

from .config import check_deny
from .tmux import run_tmux_cmd, split_capture

# Max chars per physical line sent to a REPL. A tty in canonical mode (python
# without readline, e.g. minimal remote/container builds) silently truncates
# input lines at 4095 bytes — stay well under.
_REPL_LINE_MAX = 3000


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


def extract_output(raw: str, begin: str, end: str) -> tuple[str, bool]:
    """Extract output between markers. Returns (output, is_complete)."""
    lines = split_capture(raw)
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


def check_end_marker(pane: str, end: str, tail: int = 200) -> bool:
    """Lightweight completion check: only look for end marker in tail of scrollback."""
    raw = run_tmux_cmd(["capture-pane", "-t", pane, "-p", "-J", "-S", f"-{tail}"], raise_on_error=True)
    return any(line == end for line in split_capture(raw))


def send_python_code(session: str, code: str, begin: str, end: str, preview: str = None) -> None:
    """Send Python code as a base64-encoded exec one-liner.

    The decoded payload prints the original code first (pane readability),
    then the begin marker, the try/except-wrapped execution, and the end
    marker.

    Payloads longer than _REPL_LINE_MAX are split across continuation lines
    inside the b64decode(...) call (adjacent string literals concatenate), so
    every physical line stays under the canonical-tty limit.
    """
    check_deny(code, "python")
    if preview is None:
        preview = code
    # Blank lines around the preview set it apart from the echoed b64 input
    # above and the begin marker below when a human reads the pane.
    payload = (
        "print()\n"
        f"print({preview!r})\n"
        "print()\n"
        f"print('{begin}')\n"
        "print()\n"
        f"try:\n{textwrap.indent(code, '    ')}\nexcept:\n    __import__('traceback').print_exc()\n"
        "print()\n"
        f"print('{end}')"
    )
    b64 = base64.b64encode(payload.encode()).decode()
    chunks = [b64[i:i + _REPL_LINE_MAX] for i in range(0, len(b64), _REPL_LINE_MAX)]

    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)
    if len(chunks) == 1:
        lines = [f"exec(__import__('base64').b64decode('{chunks[0]}').decode())"]
    else:
        lines = (
            ["exec(__import__('base64').b64decode("]
            + [f"'{c}'" for c in chunks]
            + [").decode())"]
        )
    for line in lines:
        run_tmux_cmd(["send-keys", "-t", session, "-l", "--", line], capture=False)
        run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)
    time.sleep(0.05)


def send_tcl_code(session: str, code: str, begin: str, end: str) -> None:
    """Send TCL code with markers."""
    check_deny(code, "tcl")
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)

    tcl_cmd = f'puts "{begin}"; if {{[catch {{\n\n{code}\n\n}} __r]}} {{puts $__r}} elseif {{$__r ne ""}} {{puts $__r}}; puts "{end}"'
    run_tmux_cmd(["send-keys", "-t", session, tcl_cmd], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)


def send_shell_code(session: str, code: str, begin: str, end: str) -> None:
    """Send shell code with markers."""
    check_deny(code, "shell")
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)

    run_tmux_cmd(["send-keys", "-t", session, f"echo '{begin}'; {{"], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)

    for line in code.split('\n'):
        run_tmux_cmd(["send-keys", "-t", session, line], capture=False)
        run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)

    run_tmux_cmd(["send-keys", "-t", session, f"}} 2>&1; echo; echo '{end}'"], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)


def send_plain(session: str, code: str, lang: str, begin: str) -> None:
    """Send code with only a begin marker, no wrapping or end marker.

    Used by read_after mode for prompt-changing commands (entering/exiting
    REPLs, ssh) where marker pairs don't survive prompt changes.
    """
    check_deny(code, lang)
    run_tmux_cmd(["send-keys", "-t", session, "-X", "cancel"], capture=False)
    if lang == "python":
        marker_cmd = f'print("{begin}")'
    elif lang == "tcl":
        marker_cmd = f'puts "{begin}"'
    else:
        marker_cmd = f"echo '{begin}'"
    run_tmux_cmd(["send-keys", "-t", session, marker_cmd], capture=False)
    run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)
    if lang == "tcl":
        run_tmux_cmd(["send-keys", "-t", session, code], capture=False)
        run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)
    else:
        for line in code.split('\n'):
            run_tmux_cmd(["send-keys", "-t", session, line], capture=False)
            run_tmux_cmd(["send-keys", "-t", session, "Enter"], capture=False)
