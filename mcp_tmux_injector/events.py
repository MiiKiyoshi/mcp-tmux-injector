"""The session's event stream: one waiter script per server process.

Every event a background thread detects (a promoted task's end marker, a
pattern match, a memory cap breach) is appended as one line to this server's
event file, and a tmux wait-for channel is signalled so the waiter script
wakes. The waiter prints the unread lines, records how far it has read, and
parks again. It is started once per session with the client's persistent
background monitor; nothing per event is ever registered.

The file holds the events and the ack file holds the watermark, so the script
carries no state: a line appended before the waiter starts is printed the
moment it starts, and a line appended while the waiter is between checks is
covered by tmux remembering a signal sent with no waiter parked.
"""
import os
import shlex
import threading
from pathlib import Path

from .config import TMUX_SOCKET_PATH
from .tmux import run_tmux_cmd

EVENT_DIR = Path.home() / ".cache" / "mcp-tmux-injector" / "events"
PID = os.getpid()
CHANNEL = f"tmix-{PID}"
EVENT_FILE = EVENT_DIR / f"{PID}.log"
ACK_FILE = EVENT_DIR / f"{PID}.ack"
WAITER_FILE = EVENT_DIR / f"{PID}.waiter"
SCRIPT_FILE = EVENT_DIR / f"{PID}.sh"

_lock = threading.Lock()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def init() -> None:
    """Start this server's stream empty and drop files left by dead servers."""
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    for f in EVENT_DIR.iterdir():
        stem = f.name.split(".", 1)[0]
        if stem.isdigit() and int(stem) != PID and not _alive(int(stem)):
            f.unlink(missing_ok=True)
    EVENT_FILE.write_text("")
    ACK_FILE.write_text("0\n")
    WAITER_FILE.unlink(missing_ok=True)


def emit(text: str) -> None:
    """Append one event (a multi-line report stays one batch) and wake the waiter."""
    with _lock:
        with EVENT_FILE.open("a", encoding="utf-8") as f:
            f.write(text.rstrip("\n") + "\n")
    run_tmux_cmd(["wait-for", "-S", CHANNEL], capture=False)


def waiter_alive() -> bool:
    try:
        return _alive(int(WAITER_FILE.read_text().strip()))
    except (OSError, ValueError):
        return False


def unread() -> int:
    """Lines appended that no waiter has printed yet."""
    try:
        total = sum(1 for _ in EVENT_FILE.open(encoding="utf-8"))
        acked = int(ACK_FILE.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0
    return max(total - acked, 0)


def waiter_note() -> str:
    """One line to append to a reply that promised an event, when nobody is listening."""
    if waiter_alive():
        return ""
    return "\nNo event stream is running: call wait_events() and start its script."


def write_script() -> str:
    """Write the waiter script and return its path."""
    tmux = "tmux" + (f" -S {shlex.quote(TMUX_SOCKET_PATH)}" if TMUX_SOCKET_PATH else "")
    script = f"""#!/bin/sh
# tmux-injector event stream for server pid {PID}.
# Prints each event as it lands ([done] task, [match] pattern, [cap] memory, [error])
# and keeps waiting for the next. Start it once using the client-specific instructions
# returned by wait_events(). `--once` prints the next batch and exits,
# for a client whose shell tool can only block.
thread=
if [ "${{1-}}" = "--codex" ]; then
  thread=${{2:?Pass the Codex thread ID}}
  command -v codex >/dev/null || exit 1
fi
deliver() {{
  if [ -n "$thread" ]; then
    until codex queue --thread "$thread" --message "[tmux-injector event]
$1"; do
      printf '%s\\n' 'Queue delivery failed; retrying in 5 seconds' >&2
      sleep 5
    done
  else
    printf '%s\\n' "$1"
  fi
}}
f={shlex.quote(str(EVENT_FILE))}
a={shlex.quote(str(ACK_FILE))}
once=0; [ "$1" = "--once" ] && once=1
echo $$ > {shlex.quote(str(WAITER_FILE))}
trap 'rm -f {shlex.quote(str(WAITER_FILE))}' EXIT
while :; do
  n=$(( $(cat "$a" 2>/dev/null || echo 0) ))
  total=$(( $(wc -l 2>/dev/null < "$f" || echo 0) ))
  if [ "$total" -gt "$n" ]; then
    deliver "$(sed -n "$((n + 1)),${{total}}p" "$f")"
    echo "$total" > "$a"
    [ $once -eq 1 ] && exit 0
    continue
  fi
  if ! kill -0 {PID} 2>/dev/null; then
    deliver "[gone] tmux-injector server (pid {PID}) exited; its tasks are gone. Call wait_events() again for the new server."
    exit 0
  fi
  timeout 60 {tmux} wait-for {CHANNEL} >/dev/null 2>&1
done
"""
    staging = SCRIPT_FILE.with_suffix(".sh.new")
    staging.write_text(script, encoding="utf-8")
    staging.chmod(0o755)
    staging.replace(SCRIPT_FILE)
    return str(SCRIPT_FILE)
