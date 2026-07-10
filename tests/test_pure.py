#!/usr/bin/env python3
"""Pure-function tests: filters, codec extraction, watch fingerprints.

No tmux required. Run directly:
    .venv/bin/python tests/test_pure.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_tmux_injector.codec import extract_output, generate_marker, generate_task_id_and_marker
from mcp_tmux_injector.filters import (
    apply_dedupe,
    apply_output_filters,
    filter_tqdm,
    parse_rel_range,
)
from mcp_tmux_injector.tasks import cmd_display
from mcp_tmux_injector.watch_cli import find_fingerprint, get_fresh_lines

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        FAILURES.append(name)


# --- markers / extraction ---
b, e = generate_marker()
check("marker pair shares stem", b[:-2] == e[:-2] and b.endswith("B_") and e.endswith("E_"))

tid, tb, te = generate_task_id_and_marker()
check("task id embedded in marker", tid[1:] in tb)

raw = f"prompt$ cmd\n{b}\nhello\nworld\n{e}\nprompt$"
out, done = extract_output(raw, b, e)
check("extract between markers", out == "hello\nworld" and done)

out, done = extract_output(f"{b}\npartial output", b, e)
check("extract incomplete (no end marker)", out == "partial output" and not done)

out, done = extract_output(f"echoed cmd containing {b} inline\n{b}\nx\n{e}", b, e)
check("echo line with marker substring not matched", out == "x" and done)

# --- rel_range ---
check("parse_rel_range '100:50'", parse_rel_range("100:50") == (-100, -50))
check("parse_rel_range '100:' open end", parse_rel_range("100:") == (-100, None))
check("parse_rel_range '100:0' end zero -> None", parse_rel_range("100:0") == (-100, None))
try:
    parse_rel_range("100")
    check("parse_rel_range invalid raises", False)
except ValueError:
    check("parse_rel_range invalid raises", True)

# --- dedupe ---
check("dedupe consecutive", apply_dedupe(["a", "a", "b", "a"]) == ["a", "b", "a"])
check("dedupe empty", apply_dedupe([]) == [])

# --- tqdm filtering ---
lines = [
    "start",
    " 10%|█         | 10/100 [00:01<00:09, 10.0it/s]",
    " 50%|█████     | 50/100 [00:05<00:05, 10.0it/s]",
    "100%|██████████| 100/100 [00:10<00:00, 10.0it/s]",
    "done",
]
check("tqdm keeps only final update", filter_tqdm(lines) == ["start", "100%|██████████| 100/100 [00:10<00:00, 10.0it/s]", "done"])
check("tqdm no-op without bars", filter_tqdm(["a", "b"]) == ["a", "b"])

# --- apply_output_filters ---
src = ["error: one", "ok", "error: two", "ok", "warn"]
check("grep", apply_output_filters(src, grep="error") == "error: one\nerror: two")
check("grep -v", apply_output_filters(src, v="error") == "ok\nwarn")
check("grep -m", apply_output_filters(src, grep="error", m=1) == "error: one")
check("grep -A", apply_output_filters(src, grep="error: two", A=1) == "error: two\nok")
check("grep -F literal", apply_output_filters(["a.b", "axb"], grep="a.b", F=True) == "a.b")
check("grep -w word", apply_output_filters(["err", "error"], grep="err", w=True) == "err")
check("grep -i case", apply_output_filters(["ERROR"], grep="error", i=True) == "ERROR")
check("line numbers", apply_output_filters(["x", "y"], n=True) == "0: x\n1: y")
check("negative line numbers", apply_output_filters(["x", "y"], n=True, n_negative=True) == "-2: x\n-1: y")

# --- fingerprints ---
fp = ["l2", "l3"]
check("find_fingerprint", find_fingerprint(["l1", "l2", "l3", "l4"], fp) == 3)
check("find_fingerprint missing", find_fingerprint(["a", "b"], fp) is None)
check("find_fingerprint empty", find_fingerprint(["a"], []) is None)

check("fresh lines after fingerprint",
      get_fresh_lines(["l1", "l2", "l3", "new1", "new2"], fp, 3) == ["new1", "new2"])
check("fresh lines fp scrolled out",
      get_fresh_lines([f"x{i}" for i in range(60)], fp, 3) == [f"x{i}" for i in range(60)])
check("fresh lines fp changed -> wait",
      get_fresh_lines(["a", "b", "c"], fp, 3) == [])
check("fresh lines no fp -> beyond total",
      get_fresh_lines(["a", "b", "c"], [], 2) == ["c"])

# --- cmd_display ---
check("cmd_display short", cmd_display("ls") == "ls")
check("cmd_display truncates", cmd_display("x" * 50) == "x" * 37 + "...")
check("cmd_display flattens newlines", cmd_display("a\nb") == "a b")

print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nall passed")
sys.exit(1 if FAILURES else 0)
