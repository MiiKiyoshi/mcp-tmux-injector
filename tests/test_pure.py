"""Pure-function tests: tmux commands, markers, filters, fingerprints. No tmux needed."""
from mcp_tmux_injector import tmux
from mcp_tmux_injector.codec import extract_output, generate_marker, generate_task_id_and_marker
from mcp_tmux_injector.filters import apply_dedupe, apply_output_filters
from mcp_tmux_injector.server import _output_lines
from mcp_tmux_injector.tasks import cmd_display
from mcp_tmux_injector.watch import find_fingerprint, get_fresh_lines


def test_tmux_command_uses_the_configured_socket(monkeypatch):
    monkeypatch.setattr(tmux, "TMUX_SOCKET_PATH", None)
    assert tmux.build_tmux_command(["list-sessions"]) == ["tmux", "list-sessions"]
    monkeypatch.setattr(tmux, "TMUX_SOCKET_PATH", "/tmp/custom-tmux.sock")
    assert tmux.build_tmux_command(["list-sessions"]) == ["tmux", "-S", "/tmp/custom-tmux.sock", "list-sessions"]


def test_markers_and_extraction():
    b, e = generate_marker()
    assert b[:-2] == e[:-2] and b.endswith("B_") and e.endswith("E_")
    tid, tb, _ = generate_task_id_and_marker()
    assert tid[1:] in tb
    assert extract_output(f"prompt$ cmd\n{b}\nhello\nworld\n{e}\nprompt$", b, e) == ("hello\nworld", True)
    assert extract_output(f"{b}\npartial output", b, e) == ("partial output", False)
    # An echoed command line that merely contains the marker is not the marker.
    assert extract_output(f"echoed cmd containing {b} inline\n{b}\nx\n{e}", b, e) == ("x", True)


def test_output_lines_drop_only_the_final_newline():
    assert _output_lines("a\nb\n") == ["a", "b"]
    assert _output_lines("a\n\n") == ["a", ""]
    assert _output_lines("a") == ["a"]
    assert _output_lines("\n") == [""]  # the output of a bare `echo`
    assert _output_lines("") == []


def test_filters():
    assert apply_dedupe(["a", "a", "b", "a"]) == ["a", "b", "a"]
    assert apply_dedupe([]) == []
    src = ["error: one", "ok", "error: two", "ok", "warn"]
    assert apply_output_filters(src, grep="error") == "error: one\nerror: two"
    assert apply_output_filters(src, grep="error: two", C=1) == "ok\nerror: two\nok"
    assert apply_output_filters(["ERROR"], grep="(?i)error") == "ERROR"
    assert apply_output_filters(src, grep=r"one\|warn") == "error: one\nwarn"
    assert apply_output_filters(["x", "x", "y"]) == "x\ny"


def test_fingerprints():
    fp = ["l2", "l3"]
    assert find_fingerprint(["l1", "l2", "l3", "l4"], fp) == 3
    assert find_fingerprint(["a", "b"], fp) is None
    assert find_fingerprint(["a"], []) is None
    assert get_fresh_lines(["l1", "l2", "l3", "new1", "new2"], fp, 3) == ["new1", "new2"]
    assert get_fresh_lines([f"x{i}" for i in range(60)], fp, 3) == [f"x{i}" for i in range(60)]
    assert get_fresh_lines(["a", "b", "c"], fp, 3) == []
    assert get_fresh_lines(["a", "b", "c"], [], 2) == ["c"]
    # An interactive prompt line "$" that became "$ echo hi" still anchors.
    assert get_fresh_lines(["l1", "l2", "$ echo hi", "hi"], ["l1", "l2", "$"], 3) == ["$ echo hi", "hi"]
    assert get_fresh_lines(["$ echo hi", "hi"], ["$"], 1) == ["$ echo hi", "hi"]
    # The prompt is drawn again after the command. That later exact copy is not the snapshot.
    prompt = ["", "status", "$"]
    after = ["", "status", "$ echo READY7", "READY7", "", "status", "$"]
    assert get_fresh_lines(after, prompt, 3) == ["$ echo READY7", "READY7", "", "status", "$"]
    # A blank last line is matched exactly, never as a prefix of any line.
    assert get_fresh_lines(["x", "", "y"], ["x", ""], 2) == ["y"]


def test_cmd_display():
    assert cmd_display("ls") == "ls"
    assert cmd_display("x" * 50) == "x" * 37 + "..."
    assert cmd_display("a\nb") == "a b"
