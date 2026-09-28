"""Tools against a private tmux server on its own socket, so the user's sessions
are never touched. Event files go to tmp_path."""
import asyncio
import shutil
import subprocess
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_tmux_injector import events, registry, server, tasks, tmux

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

A, B = "work:a.0", "work:b.0"


def call(tool, **args):
    result = asyncio.run(server.mcp.call_tool(tool, args))
    return (result[0] if isinstance(result, tuple) else result)[0].text


def refused(tool, **args):
    with pytest.raises(ToolError) as error:
        call(tool, **args)
    return str(error.value)


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _clear_state():
    for store in (registry._sessions, registry._working_panes, tasks._tasks, tmux._session_cache):
        store.clear()


@pytest.fixture
def stream(tmp_path, monkeypatch):
    """A managed session 'work' with windows a and b. Returns a reader of the event stream."""
    sock = str(tmp_path / "tmux.sock")
    monkeypatch.setattr(tmux, "TMUX_SOCKET_PATH", sock)
    monkeypatch.setattr(events, "TMUX_SOCKET_PATH", sock)
    monkeypatch.setattr(events, "EVENT_DIR", tmp_path)
    for name, suffix in (("EVENT_FILE", "log"), ("ACK_FILE", "ack"),
                         ("WAITER_FILE", "waiter"), ("SCRIPT_FILE", "sh")):
        monkeypatch.setattr(events, name, tmp_path / f"events.{suffix}")
    _clear_state()
    call("create_session", name="work", windows=["a", "b"])
    yield lambda: events.EVENT_FILE.read_text() if events.EVENT_FILE.exists() else ""
    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
    _clear_state()


def test_run_promote_and_read_back(stream):
    assert call("xsh", pane=A, code="echo a") == "a"
    promoted = call("xsh", pane=A, code="sleep 4; echo slept")
    assert promoted.startswith("[task promoted] T") and "do not resend" in promoted
    task_id = promoted.split()[2]
    assert wait_for(lambda: f"[done] {task_id}" in stream())
    assert task_id in call("task_list", all=True)
    assert call("task_output", task_id=task_id) == "slept"


def test_long_output_is_kept_as_a_task(stream):
    preview = call("xsh", pane=A, code="seq 300")
    assert preview.startswith("[Large output: 300 lines") and "tail=/head=/grep=" in preview
    task_id = preview.split("→ ")[1].split("]")[0]
    assert call("task_output", task_id=task_id, tail=2) == "299\n300"
    assert call("task_output", task_id=task_id, head=3) == "1\n2\n3"
    assert call("task_output", task_id=task_id, grep="^1[0-9]$") == "\n".join(map(str, range(10, 20)))
    assert call("task_output", task_id=task_id, grep="^150$", C=1) == "149\n150\n151"
    assert "not both" in refused("task_output", task_id=task_id, head=1, tail=1)


def test_python_repl(stream):
    call("xsh", pane=B, code="python3", read_after=2)
    assert call("xpy", pane=B, code="print(6*7)") == "42"
    assert call("xpy", pane=B, code="print('a')\nprint('b')") == "a\nb"
    assert "not both" in refused("xpy", pane=B, code="1", file=__file__)
    call("xpy", pane=B, code="exit()", read_after=1)


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="needs tclsh")
def test_tcl(stream):
    call("xsh", pane=B, code="tclsh", read_after=1)
    assert call("xtcl", pane=B, code="puts [expr 6*7]") == "42"
    assert call("xtcl", pane=B, code="proc f {} {\n  return 7\n}\nputs [f]") == "7"
    call("xtcl", pane=B, code="exit", read_after=1)


def test_screen_watch_and_keys(stream):
    call("xsh", pane=A, code="echo marker-line")
    assert "marker-line" in call("capture_pane", pane=A, tail=20, grep="marker")
    assert call("capture_pane", pane=A, tail=20, grep="(?i)^MARKER-LINE$") == "marker-line"
    assert "[watching]" in call("poll_pane", pane=B, pattern=r"READY\d")
    assert call("send_text", pane=B, text="echo READY7") == "Text sent"
    # The match is the output line, not the typed command that contains the pattern.
    assert wait_for(lambda: f"[match] {B}: READY7\n" in stream())
    promoted = call("xsh", pane=B, code="sleep 30")
    task_id = promoted.split()[2]
    assert call("send_keys", pane=B, keys="C-c") == "Keys sent"
    assert "removed" in call("task_cancel", task_id=task_id)


def test_memory(stream):
    assert call("mem_pane", pane=A).startswith(f"{A}: RSS")
    assert "Total" in call("mem_pane", session="work")
    assert "exactly one" in refused("mem_pane")
    assert "[watching]" in call("watch_mem", session="work", rss_gb=0.000001, poll=1)
    assert wait_for(lambda: "[cap]" in stream())


def test_own_session_structure(stream):
    assert "work:c.0" in call("create_window", session="work", name="c")
    assert "Killed window" in call("kill_window", session="work", window="c")
    assert "Respawned" in call("respawn_pane", pane=B)
    assert call("set_pane", pane=A, description="probe").startswith("Registered")
    assert '[R: "probe"]' in call("ls", session="work")
    assert "Killed session" in call("kill_session", name="work")


def test_session_names_and_the_last_window(stream):
    assert "distinct names" in refused("create_session", name="empty", windows=[])
    assert "distinct names" in refused("create_session", name="twice", windows=["x", "x"])
    call("create_session", name="solo")
    call("kill_window", session="solo", window="main")
    assert "solo:main.0" in call("create_session", name="solo")


def test_describing_an_own_pane_keeps_it_own(stream):
    call("set_pane", pane=A, description="renamed")
    assert "Respawned" in call("respawn_pane", pane=A)
    index = tmux.run_tmux_cmd(["display-message", "-p", "-t", A, "#{window_index}"]).strip()
    call("set_pane", pane=f"work:{index}.0", description="same pane by index")
    assert "Respawned" in call("respawn_pane", pane=f"work:{index}.0")
    assert "work:c.0" in call("create_window", session="work", name="c")
    assert "Killed window" in call("kill_window", session="work", window="c")


def test_user_session_structure_needs_the_user(stream):
    subprocess.run(tmux.build_tmux_command(["new-session", "-d", "-s", "user", "-n", "main"]), check=True)
    call("set_pane", pane="user:main.0", description="user's terminal")
    assert "create_session" in refused("create_window", session="user", name="x")
    assert "force=True" in refused("respawn_pane", pane="user:main.0")
    assert "force=True" in refused("kill_window", session="user", window="main")
    assert "force=True" in refused("kill_session", name="user")
    assert "Respawned" in call("respawn_pane", pane="user:main.0", force=True)
    assert "Killed session" in call("kill_session", name="user", force=True)
